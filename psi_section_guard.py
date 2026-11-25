"""PSI section reassembly and PES rejoining for a transport stream demuxer."""

from __future__ import annotations

import copy
import threading


STAT_KEYS = (
    "sections_parsed",
    "crc_errors",
    "rejected",
    "pes_units",
    "overflows",
    "scrambled",
)

PAT_TABLE_ID = 0x00
PMT_TABLE_ID = 0x02

SNAPSHOT_VERSION = 1

PSI_MALFORMED_SECTION = "PSI_MALFORMED_SECTION"
PSI_CRC_MISMATCH = "PSI_CRC_MISMATCH"
PSI_DUPLICATE_PID = "PSI_DUPLICATE_PID"
PSI_BUFFER_OVERFLOW = "PSI_BUFFER_OVERFLOW"
PSI_STALE_VERSION = "PSI_STALE_VERSION"
PSI_PES_OVERFLOW = "PSI_PES_OVERFLOW"
PSI_SNAPSHOT_INVALID = "PSI_SNAPSHOT_INVALID"
PSI_UNKNOWN_TABLE = "PSI_UNKNOWN_TABLE"


class PsiError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _build_crc_table() -> tuple[int, ...]:
    table = []
    for value in range(256):
        crc = value << 24
        for _ in range(8):
            if crc & 0x80000000:
                crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
            else:
                crc = (crc << 1) & 0xFFFFFFFF
        table.append(crc)
    return tuple(table)


_CRC_TABLE = _build_crc_table()


def mpeg_crc32(data: bytes) -> int:
    """CRC-32/MPEG-2 using polynomial 0x04C11DB7, init all ones and no reflection."""
    crc = 0xFFFFFFFF
    for byte in data:
        index = ((crc >> 24) ^ byte) & 0xFF
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC_TABLE[index]
    return crc & 0xFFFFFFFF


class PsiSectionGuard:
    """Reassemble PAT and PMT sections, guard table versions and rejoin PES."""

    def __init__(self, *, max_section_bytes: int = 4096,
                 max_pes_bytes: int = 1048576) -> None:
        self.max_section_bytes = max_section_bytes
        self.max_pes_bytes = max_pes_bytes
        self._lock = threading.RLock()
        self._pat: dict | None = None
        self._pmt: dict[int, dict] = {}
        self._section_buffers: dict[int, bytearray] = {}
        self._section_wait_start: set[int] = set()
        self._pes_open: dict[tuple[object, int], bytearray] = {}
        self._pes_ready: list[dict] = []
        self._stats = {key: 0 for key in STAT_KEYS}

    def _reject(self, code: str, detail: object = None) -> None:
        self._stats["rejected"] += 1
        raise PsiError(code, detail)

    def _reject_scrambled(self, scrambling: int) -> None:
        self._stats["scrambled"] += 1
        raise PsiError(
            PSI_MALFORMED_SECTION,
            {"reason": "scrambled", "scrambling": scrambling},
        )

    def _check_scrambling_locked(self, scrambling: int) -> None:
        if scrambling != 0:
            self._reject_scrambled(scrambling)

    def feed_section(self, pid: int, section: bytes, *,
                     scrambling: int = 0) -> dict:
        with self._lock:
            self._check_scrambling_locked(scrambling)
            table_name, version, entry = self._parse_section_locked(pid, section)
            current = self._pat if table_name == "pat" else self._pmt.get(pid)
            if current is not None and version < current["version"]:
                self._reject(
                    PSI_STALE_VERSION,
                    {
                        "pid": pid,
                        "table": table_name,
                        "current_version": current["version"],
                        "received_version": version,
                    },
                )

            if table_name == "pat":
                self._pat = entry
            else:
                self._pmt[pid] = entry
            self._stats["sections_parsed"] += 1
            return {"table": table_name, "version": version, "pid": pid}

    def _parse_section_locked(
        self, pid: int, section: bytes
    ) -> tuple[str, int, dict]:
        if not isinstance(section, (bytes, bytearray, memoryview)):
            self._reject(
                PSI_MALFORMED_SECTION,
                {"reason": "section_type", "type": type(section).__name__},
            )

        raw = bytes(section)
        if len(raw) < 3:
            self._reject(
                PSI_MALFORMED_SECTION,
                {"reason": "short_section_header", "length": len(raw)},
            )

        section_length = int.from_bytes(raw[1:3], "big")
        total_length = section_length + 3
        if section_length < 5 or len(raw) != total_length:
            self._reject(
                PSI_MALFORMED_SECTION,
                {
                    "reason": "length_mismatch",
                    "declared": section_length,
                    "expected_total": total_length,
                    "actual": len(raw),
                },
            )

        if len(raw) > self.max_section_bytes:
            self._section_buffers.pop(pid, None)
            self._section_wait_start.add(pid)
            self._stats["overflows"] += 1
            raise PsiError(
                PSI_BUFFER_OVERFLOW,
                {
                    "pid": pid,
                    "section_bytes": len(raw),
                    "max_section_bytes": self.max_section_bytes,
                },
            )

        version = raw[3]
        if not 0 <= version <= 31:
            self._reject(
                PSI_MALFORMED_SECTION,
                {"reason": "version_range", "version": version},
            )

        table_id = raw[0]
        body = raw[4:-4]

        if table_id == PAT_TABLE_ID and len(body) % 4 != 0:
            self._reject(
                PSI_MALFORMED_SECTION,
                {"reason": "pat_body_alignment", "body_length": len(body)},
            )

        if table_id == PMT_TABLE_ID and (len(body) < 2 or len(body) % 2 != 0):
            self._reject(
                PSI_MALFORMED_SECTION,
                {"reason": "pmt_body_alignment", "body_length": len(body)},
            )

        expected_crc = mpeg_crc32(raw[:-4])
        actual_crc = int.from_bytes(raw[-4:], "big")
        if expected_crc != actual_crc:
            self._stats["crc_errors"] += 1
            raise PsiError(
                PSI_CRC_MISMATCH,
                {
                    "pid": pid,
                    "expected": expected_crc,
                    "actual": actual_crc,
                },
            )

        if table_id == PAT_TABLE_ID:
            programs = {}
            for offset in range(0, len(body), 4):
                program_number = int.from_bytes(body[offset:offset + 2], "big")
                program_pid = int.from_bytes(body[offset + 2:offset + 4], "big")
                programs[program_number] = program_pid
            return "pat", version, {"version": version, "programs": programs}

        if table_id == PMT_TABLE_ID:
            pcr_pid = int.from_bytes(body[0:2], "big")
            streams = [
                int.from_bytes(body[offset:offset + 2], "big")
                for offset in range(2, len(body), 2)
            ]
            seen: set[int] = set()
            duplicates: set[int] = set()
            for elementary_pid in streams:
                if elementary_pid in seen:
                    duplicates.add(elementary_pid)
                seen.add(elementary_pid)
            if duplicates:
                self._reject(
                    PSI_DUPLICATE_PID,
                    {"pid": pid, "duplicates": sorted(duplicates)},
                )
            return (
                "pmt",
                version,
                {
                    "version": version,
                    "pcr_pid": pcr_pid,
                    "streams": streams,
                },
            )

        self._reject(PSI_UNKNOWN_TABLE, {"table_id": table_id, "pid": pid})

    def feed_payload(self, pid: int, payload: bytes, *,
                     unit_start: bool = False, scrambling: int = 0) -> dict:
        with self._lock:
            self._check_scrambling_locked(scrambling)
            data = bytes(payload)

            if unit_start:
                self._section_wait_start.discard(pid)
                if not data:
                    self._section_buffers.pop(pid, None)
                    return {"parsed": 0, "buffered": 0}

                pointer_field = data[0]
                start = 1 + pointer_field
                if start > len(data):
                    self._section_buffers.pop(pid, None)
                    self._section_wait_start.add(pid)
                    self._reject(
                        PSI_MALFORMED_SECTION,
                        {
                            "reason": "pointer_field_out_of_range",
                            "pointer_field": pointer_field,
                            "payload_length": len(data),
                        },
                    )
                self._section_buffers[pid] = bytearray(data[start:])
            else:
                if pid in self._section_wait_start:
                    return {"parsed": 0, "buffered": 0}
                buffer = self._section_buffers.setdefault(pid, bytearray())
                buffer.extend(data)

            parsed = 0
            while True:
                buffer = self._section_buffers.get(pid)
                if buffer is None:
                    break
                if not buffer:
                    self._section_buffers.pop(pid, None)
                    break

                # table_id 0xFF is MPEG-TS stuffing at a section boundary.
                if buffer[0] == 0xFF:
                    self._section_buffers.pop(pid, None)
                    break

                if len(buffer) < 3:
                    if len(buffer) > self.max_section_bytes:
                        self._section_buffers.pop(pid, None)
                        self._section_wait_start.add(pid)
                        self._stats["overflows"] += 1
                        raise PsiError(
                            PSI_BUFFER_OVERFLOW,
                            {
                                "pid": pid,
                                "buffered_bytes": len(buffer),
                                "max_section_bytes": self.max_section_bytes,
                            },
                        )
                    break

                section_length = int.from_bytes(buffer[1:3], "big")
                total_length = section_length + 3
                if section_length < 5:
                    self._section_buffers.pop(pid, None)
                    self._section_wait_start.add(pid)
                    self._reject(
                        PSI_MALFORMED_SECTION,
                        {
                            "reason": "invalid_section_length",
                            "declared": section_length,
                        },
                    )

                if total_length > self.max_section_bytes:
                    self._section_buffers.pop(pid, None)
                    self._section_wait_start.add(pid)
                    self._stats["overflows"] += 1
                    raise PsiError(
                        PSI_BUFFER_OVERFLOW,
                        {
                            "pid": pid,
                            "section_bytes": total_length,
                            "max_section_bytes": self.max_section_bytes,
                        },
                    )

                if len(buffer) < total_length:
                    break

                section = bytes(buffer[:total_length])
                del buffer[:total_length]
                if not buffer:
                    self._section_buffers.pop(pid, None)

                self.feed_section(pid, section)
                parsed += 1

            buffered = len(self._section_buffers.get(pid, b""))
            return {"parsed": parsed, "buffered": buffered}

    def tables(self) -> dict:
        with self._lock:
            return copy.deepcopy({"pat": self._pat, "pmt": self._pmt})

    def version_of(self, table: str, *, pid: int | None = None) -> int:
        with self._lock:
            table_name = table.lower() if isinstance(table, str) else table
            if table_name == "pat":
                if self._pat is None:
                    raise PsiError(PSI_UNKNOWN_TABLE, {"table": table})
                return self._pat["version"]

            if table_name == "pmt":
                entry = self._pmt.get(pid) if pid is not None else None
                if entry is None:
                    raise PsiError(
                        PSI_UNKNOWN_TABLE,
                        {"table": table, "pid": pid},
                    )
                return entry["version"]

            raise PsiError(
                PSI_UNKNOWN_TABLE, {"table": table_name, "pid": pid}
            )

    def pending_bytes(self, pid: int) -> int:
        with self._lock:
            return len(self._section_buffers.get(pid, b""))

    def discard(self, pid: int) -> int:
        with self._lock:
            buffer = self._section_buffers.pop(pid, None)
            self._section_wait_start.discard(pid)
            return len(buffer) if buffer is not None else 0

    def push_pes(self, stream_id: str, chunk: bytes, *, pts: int) -> dict:
        with self._lock:
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise PsiError(
                    PSI_PES_OVERFLOW,
                    {"reason": "chunk_type", "type": type(chunk).__name__},
                )

            data = bytes(chunk)
            key = (stream_id, pts)
            current = self._pes_open.get(key)
            current_length = len(current) if current is not None else 0
            if current_length + len(data) > self.max_pes_bytes:
                self._pes_open.pop(key, None)
                self._stats["overflows"] += 1
                raise PsiError(
                    PSI_PES_OVERFLOW,
                    {
                        "stream_id": stream_id,
                        "pts": pts,
                        "buffered_bytes": current_length,
                        "chunk_bytes": len(data),
                        "max_pes_bytes": self.max_pes_bytes,
                    },
                )

            if current is None:
                current = bytearray()
                self._pes_open[key] = current
            current.extend(data)
            return {
                "stream_id": stream_id,
                "pts": pts,
                "length": len(current),
            }

    def flush_pes(self) -> list:
        with self._lock:
            flushed = []
            for (stream_id, pts), data in self._pes_open.items():
                record = {
                    "stream_id": stream_id,
                    "pts": pts,
                    "length": len(data),
                }
                self._pes_ready.append(record)
                flushed.append(copy.deepcopy(record))
                self._stats["pes_units"] += 1
            self._pes_open.clear()
            return flushed

    def drain_pes(self) -> list:
        with self._lock:
            units = sorted(self._pes_ready, key=lambda unit: unit["pts"])
            self._pes_ready = []
            return copy.deepcopy(units)

    def snapshot(self) -> dict:
        with self._lock:
            buffers = {
                pid: bytes(buffer)
                for pid, buffer in self._section_buffers.items()
            }
            pes_buffers = [
                {
                    "stream_id": stream_id,
                    "pts": pts,
                    "length": len(data),
                    "data": bytes(data),
                }
                for (stream_id, pts), data in self._pes_open.items()
            ]
            return {
                "version": SNAPSHOT_VERSION,
                "pat": copy.deepcopy(self._pat),
                "pmt": copy.deepcopy(self._pmt),
                "versions": {
                    "pat": None if self._pat is None else self._pat["version"],
                    "pmt": {
                        pid: entry["version"]
                        for pid, entry in self._pmt.items()
                    },
                },
                "buffers": copy.deepcopy(buffers),
                "section_buffers": copy.deepcopy(buffers),
                "wait_start": sorted(self._section_wait_start),
                "pes_buffers": copy.deepcopy(pes_buffers),
                "pes_ready": copy.deepcopy(self._pes_ready),
                "stats": copy.deepcopy(self._stats),
            }

    @staticmethod
    def _snapshot_invalid(detail: object = None) -> None:
        raise PsiError(PSI_SNAPSHOT_INVALID, detail)

    @classmethod
    def _pid_key_from_snapshot(cls, key: object) -> int:
        if _is_int(key):
            return int(key)
        if isinstance(key, str):
            try:
                return int(key)
            except ValueError:
                pass
        cls._snapshot_invalid({"reason": "pid_key", "key": key})
        raise AssertionError("unreachable")

    @classmethod
    def _validated_table_entry(
        cls, value: object, *, table: str
    ) -> dict | None:
        if table == "pat" and value is None:
            return None
        if not isinstance(value, dict):
            cls._snapshot_invalid(
                {"reason": table, "value_type": type(value).__name__}
            )

        version = value.get("version")
        if not _is_int(version) or not 0 <= version <= 31:
            cls._snapshot_invalid(
                {"reason": f"{table}_version", "version": version}
            )

        if table == "pat":
            programs = value.get("programs")
            if not isinstance(programs, dict):
                cls._snapshot_invalid(
                    {"reason": "pat_programs", "value": programs}
                )
            normalized_programs = {}
            for program_number, program_pid in programs.items():
                if not _is_int(program_number) or not _is_int(program_pid):
                    cls._snapshot_invalid(
                        {
                            "reason": "pat_program_entry",
                            "entry": (program_number, program_pid),
                        }
                    )
                normalized_programs[int(program_number)] = int(program_pid)
            return {
                "version": int(version),
                "programs": normalized_programs,
            }

        pcr_pid = value.get("pcr_pid")
        streams = value.get("streams")
        if not _is_int(pcr_pid):
            cls._snapshot_invalid(
                {"reason": "pmt_pcr_pid", "pcr_pid": pcr_pid}
            )
        if not isinstance(streams, (list, tuple)):
            cls._snapshot_invalid(
                {"reason": "pmt_streams", "streams": streams}
            )

        normalized_streams = []
        seen = set()
        for elementary_pid in streams:
            if not _is_int(elementary_pid):
                cls._snapshot_invalid(
                    {"reason": "pmt_stream_pid", "pid": elementary_pid}
                )
            elementary_pid = int(elementary_pid)
            if elementary_pid in seen:
                cls._snapshot_invalid(
                    {"reason": "pmt_duplicate_pid", "pid": elementary_pid}
                )
            seen.add(elementary_pid)
            normalized_streams.append(elementary_pid)

        return {
            "version": int(version),
            "pcr_pid": int(pcr_pid),
            "streams": normalized_streams,
        }

    def _validated_snapshot(self, state: object) -> dict:
        if not isinstance(state, dict):
            self._snapshot_invalid({"reason": "state_type"})
        snapshot_version = state.get("version")
        if (
            not _is_int(snapshot_version)
            or snapshot_version != SNAPSHOT_VERSION
        ):
            self._snapshot_invalid(
                {"reason": "version", "got": snapshot_version}
            )

        tables = state.get("tables")
        if not isinstance(tables, dict):
            tables = {}

        raw_pat = state["pat"] if "pat" in state else tables.get("pat")
        raw_pmt = state["pmt"] if "pmt" in state else tables.get("pmt")

        pat = self._validated_table_entry(raw_pat, table="pat")

        if not isinstance(raw_pmt, dict):
            self._snapshot_invalid(
                {"reason": "pmt", "value_type": type(raw_pmt).__name__}
            )
        pmt = {}
        for raw_pid, raw_entry in raw_pmt.items():
            pid = self._pid_key_from_snapshot(raw_pid)
            pmt[pid] = self._validated_table_entry(raw_entry, table="pmt")

        raw_versions = state.get("versions")
        if raw_versions is not None:
            if not isinstance(raw_versions, dict):
                self._snapshot_invalid({"reason": "versions"})
            pat_version = raw_versions.get("pat")
            if pat_version is not None and (
                pat is None or pat_version != pat["version"]
            ):
                self._snapshot_invalid({"reason": "pat_version_mismatch"})
            pmt_versions = raw_versions.get("pmt", {})
            if not isinstance(pmt_versions, dict):
                self._snapshot_invalid({"reason": "pmt_versions"})
            for raw_pid, version in pmt_versions.items():
                pid = self._pid_key_from_snapshot(raw_pid)
                if pid not in pmt or version != pmt[pid]["version"]:
                    self._snapshot_invalid(
                        {"reason": "pmt_version_mismatch", "pid": pid}
                    )

        raw_buffers = None
        for key in (
            "buffers",
            "section_buffers",
            "pending",
            "pending_buffers",
        ):
            if key in state:
                raw_buffers = state[key]
                break
        if raw_buffers is None:
            raw_buffers = {}
        if not isinstance(raw_buffers, dict):
            self._snapshot_invalid({"reason": "buffers"})

        section_buffers = {}
        for raw_pid, raw_buffer in raw_buffers.items():
            pid = self._pid_key_from_snapshot(raw_pid)
            if not isinstance(raw_buffer, (bytes, bytearray, memoryview)):
                self._snapshot_invalid({"reason": "buffer_value", "pid": pid})
            buffer_bytes = bytes(raw_buffer)
            if len(buffer_bytes) > self.max_section_bytes:
                self._snapshot_invalid({"reason": "buffer_too_large", "pid": pid})
            section_buffers[pid] = bytearray(buffer_bytes)

        raw_wait_start = state.get("wait_start", [])
        if not isinstance(raw_wait_start, (list, tuple, set, frozenset)):
            self._snapshot_invalid({"reason": "wait_start"})
        wait_start = {
            self._pid_key_from_snapshot(raw_pid) for raw_pid in raw_wait_start
        }

        raw_pes_buffers = None
        for key in ("pes_buffers", "pes_open"):
            if key in state:
                raw_pes_buffers = state[key]
                break
        if raw_pes_buffers is None:
            raw_pes_buffers = []
        if not isinstance(raw_pes_buffers, list):
            self._snapshot_invalid({"reason": "pes_buffers"})

        pes_open = {}
        for item in raw_pes_buffers:
            if not isinstance(item, dict):
                self._snapshot_invalid({"reason": "pes_buffer_entry"})
            stream_id = item.get("stream_id")
            pts = item.get("pts")
            data = item.get("data", b"")
            if not isinstance(stream_id, (str, int)) or isinstance(stream_id, bool):
                self._snapshot_invalid(
                    {"reason": "pes_stream_id", "stream_id": stream_id}
                )
            if not _is_int(pts):
                self._snapshot_invalid({"reason": "pes_pts", "pts": pts})
            if not isinstance(data, (bytes, bytearray, memoryview)):
                self._snapshot_invalid({"reason": "pes_data"})
            data_bytes = bytes(data)
            if len(data_bytes) > self.max_pes_bytes:
                self._snapshot_invalid({"reason": "pes_too_large"})
            if "length" in item and item["length"] != len(data_bytes):
                self._snapshot_invalid({"reason": "pes_length_mismatch"})
            key = (stream_id, int(pts))
            if key in pes_open:
                self._snapshot_invalid({"reason": "duplicate_pes_key", "key": key})
            pes_open[key] = bytearray(data_bytes)

        raw_pes_ready = state.get("pes_ready", state.get("pes_records", []))
        if not isinstance(raw_pes_ready, list):
            self._snapshot_invalid({"reason": "pes_ready"})
        pes_ready = []
        for record in raw_pes_ready:
            if not isinstance(record, dict):
                self._snapshot_invalid({"reason": "pes_ready_entry"})
            stream_id = record.get("stream_id")
            pts = record.get("pts")
            length = record.get("length")
            if not isinstance(stream_id, (str, int)) or isinstance(stream_id, bool):
                self._snapshot_invalid({"reason": "pes_ready_stream_id"})
            if not _is_int(pts):
                self._snapshot_invalid({"reason": "pes_ready_pts"})
            if not _is_int(length) or length < 0:
                self._snapshot_invalid({"reason": "pes_ready_length"})
            pes_ready.append(
                {
                    "stream_id": stream_id,
                    "pts": int(pts),
                    "length": int(length),
                }
            )

        raw_stats = state.get("stats")
        stats = None
        if raw_stats is not None:
            if not isinstance(raw_stats, dict):
                self._snapshot_invalid({"reason": "stats"})
            stats = {}
            for key in STAT_KEYS:
                value = raw_stats.get(key)
                if not _is_int(value) or value < 0:
                    self._snapshot_invalid(
                        {"reason": f"stats_{key}", "value": value}
                    )
                stats[key] = int(value)

        return {
            "pat": pat,
            "pmt": pmt,
            "section_buffers": section_buffers,
            "wait_start": wait_start,
            "pes_open": pes_open,
            "pes_ready": pes_ready,
            "stats": stats,
        }

    def restore(self, state: dict) -> None:
        with self._lock:
            restored = self._validated_snapshot(state)
            self._pat = restored["pat"]
            self._pmt = restored["pmt"]
            self._section_buffers = restored["section_buffers"]
            self._section_wait_start = restored["wait_start"]
            self._pes_open = restored["pes_open"]
            self._pes_ready = restored["pes_ready"]
            if restored["stats"] is not None:
                self._stats = restored["stats"]

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            for key in STAT_KEYS:
                self._stats[key] = 0
