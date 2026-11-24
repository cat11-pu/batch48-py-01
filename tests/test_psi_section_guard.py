import threading

import pytest

import psi_section_guard as psg


def mpeg_crc(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            if crc & 0x80000000:
                crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
            else:
                crc = (crc << 1) & 0xFFFFFFFF
    return crc


def build(table_id: int, version: int, body: bytes) -> bytes:
    head = bytes([table_id]) + (len(body) + 5).to_bytes(2, "big")
    payload = head + bytes([version]) + body
    return payload + mpeg_crc(payload).to_bytes(4, "big")


def pat_body(programs) -> bytes:
    out = b""
    for number, pid in programs:
        out += number.to_bytes(2, "big") + pid.to_bytes(2, "big")
    return out


def pmt_body(pcr_pid: int, streams) -> bytes:
    out = pcr_pid.to_bytes(2, "big")
    for pid in streams:
        out += pid.to_bytes(2, "big")
    return out


def guard(**kwargs):
    return psg.PsiSectionGuard(**kwargs)


def test_new_guard_reports_empty_tables():
    g = guard()
    assert g.tables() == {"pat": None, "pmt": {}}
    assert g.pending_bytes(0) == 0
    assert g.stats()["sections_parsed"] == 0
    assert g.stats()["crc_errors"] == 0
    with pytest.raises(psg.PsiError) as exc:
        g.version_of("pat")
    assert exc.value.code == "PSI_UNKNOWN_TABLE"


def test_pat_section_installs_program_map():
    g = guard()
    body = pat_body([(1, 0x1000), (2, 0x1100)])
    out = g.feed_section(0x0000, build(0x00, 3, body))
    assert out == {"table": "pat", "version": 3, "pid": 0x0000}
    assert g.tables()["pat"]["version"] == 3
    assert g.tables()["pat"]["programs"] == {1: 0x1000, 2: 0x1100}
    assert g.version_of("pat") == 3
    assert g.stats()["sections_parsed"] == 1


def test_crc_failure_discards_section_and_counts():
    g = guard()
    good = build(0x00, 1, pat_body([(1, 0x1000)]))
    broken = good[:-1] + bytes([good[-1] ^ 0xFF])
    with pytest.raises(psg.PsiError) as exc:
        g.feed_section(0x0000, broken)
    assert exc.value.code == "PSI_CRC_MISMATCH"
    assert g.tables()["pat"] is None
    assert g.stats()["crc_errors"] == 1


def test_truncated_or_mislabelled_section_is_malformed():
    g = guard()
    good = build(0x00, 1, pat_body([(1, 0x1000)]))
    with pytest.raises(psg.PsiError) as exc:
        g.feed_section(0x0000, good[:-2])
    assert exc.value.code == "PSI_MALFORMED_SECTION"
    bad_len = bytes([0x00]) + (99).to_bytes(2, "big") + good[3:]
    with pytest.raises(psg.PsiError) as exc:
        g.feed_section(0x0000, bad_len)
    assert exc.value.code == "PSI_MALFORMED_SECTION"
    assert g.stats()["rejected"] == 2


def test_pmt_section_records_pcr_and_streams():
    g = guard()
    body = pmt_body(0x1000, [0x1001, 0x1002])
    g.feed_section(0x1000, build(0x02, 4, body))
    entry = g.tables()["pmt"][0x1000]
    assert entry["version"] == 4
    assert entry["pcr_pid"] == 0x1000
    assert entry["streams"] == [0x1001, 0x1002]
    assert g.version_of("pmt", pid=0x1000) == 4


def test_pmt_with_duplicate_stream_pid_is_rejected():
    g = guard()
    body = pmt_body(0x1000, [0x1001, 0x1001])
    with pytest.raises(psg.PsiError) as exc:
        g.feed_section(0x1000, build(0x02, 1, body))
    assert exc.value.code == "PSI_DUPLICATE_PID"
    assert g.tables()["pmt"] == {}
    assert g.stats()["rejected"] == 1


def test_new_version_replaces_the_whole_table():
    g = guard()
    g.feed_section(0x0000, build(0x00, 0, pat_body([(1, 0x1000), (2, 0x1100)])))
    g.feed_section(0x0000, build(0x00, 1, pat_body([(3, 0x1200)])))
    assert g.version_of("pat") == 1
    assert g.tables()["pat"]["programs"] == {3: 0x1200}


def test_older_version_is_rejected_and_table_kept():
    g = guard()
    g.feed_section(0x0000, build(0x00, 5, pat_body([(9, 0x1900)])))
    with pytest.raises(psg.PsiError) as exc:
        g.feed_section(0x0000, build(0x00, 3, pat_body([(1, 0x1000)])))
    assert exc.value.code == "PSI_STALE_VERSION"
    assert g.version_of("pat") == 5
    assert g.tables()["pat"]["programs"] == {9: 0x1900}


def test_section_split_across_payloads_is_reassembled():
    g = guard()
    programs = [(i, 0x1000 + i) for i in range(1, 51)]
    section = build(0x00, 7, pat_body(programs))
    assert len(section) == 208
    first = g.feed_payload(0x0000, b"\x00" + section[:99], unit_start=True)
    assert first["parsed"] == 0
    assert g.pending_bytes(0x0000) == 99
    assert g.tables()["pat"] is None
    second = g.feed_payload(0x0000, section[99:], unit_start=False)
    assert second["parsed"] == 1
    assert g.pending_bytes(0x0000) == 0
    assert g.tables()["pat"]["programs"] == dict(programs)
    assert g.stats()["sections_parsed"] == 1


def test_pointer_field_is_skipped_on_unit_start():
    g = guard()
    section = build(0x00, 2, pat_body([(4, 0x1400)]))
    out = g.feed_payload(0x0000, b"\x00" + section, unit_start=True)
    assert out["parsed"] == 1
    assert out["buffered"] == 0
    assert g.tables()["pat"]["version"] == 2


def test_section_larger_than_limit_overflows_buffer():
    g = guard(max_section_bytes=64)
    section = build(0x00, 1, pat_body([(i, 0x1000 + i) for i in range(1, 41)]))
    with pytest.raises(psg.PsiError) as exc:
        g.feed_payload(0x0000, b"\x00" + section, unit_start=True)
    assert exc.value.code == "PSI_BUFFER_OVERFLOW"
    assert g.pending_bytes(0x0000) == 0
    assert g.stats()["overflows"] == 1


def test_pes_chunks_are_rejoined_and_drained_by_pts():
    g = guard()
    g.push_pes("e0", b"a" * 100, pts=900)
    g.push_pes("e1", b"b" * 50, pts=300)
    g.push_pes("e0", b"c" * 20, pts=900)
    done = g.flush_pes()
    assert len(done) == 2
    out = g.drain_pes()
    assert [u["pts"] for u in out] == [300, 900]
    assert [u["stream_id"] for u in out] == ["e1", "e0"]
    assert out[1]["length"] == 120
    assert g.drain_pes() == []
    assert g.stats()["pes_units"] == 2


def test_pes_over_limit_is_dropped_and_counted():
    g = guard(max_pes_bytes=64)
    with pytest.raises(psg.PsiError) as exc:
        g.push_pes("e0", b"x" * 100, pts=1)
    assert exc.value.code == "PSI_PES_OVERFLOW"
    assert g.flush_pes() == []
    assert g.stats()["overflows"] == 1


def test_snapshot_restore_preserves_tables_and_versions():
    g = guard()
    g.feed_section(0x0000, build(0x00, 6, pat_body([(1, 0x1000)])))
    g.feed_section(0x1000, build(0x02, 2, pmt_body(0x1000, [0x1001])))
    snap = g.snapshot()
    g.feed_section(0x0000, build(0x00, 9, pat_body([(2, 0x1100)])))
    g.restore(snap)
    assert g.version_of("pat") == 6
    assert g.tables()["pat"]["programs"] == {1: 0x1000}
    assert g.version_of("pmt", pid=0x1000) == 2
    with pytest.raises(psg.PsiError) as exc:
        g.restore({"version": 1, "pat": "nope"})
    assert exc.value.code == "PSI_SNAPSHOT_INVALID"
    assert g.version_of("pat") == 6


def test_concurrent_pmt_feeds_are_serialized():
    g = guard()
    base = 0x1010

    def worker(offset):
        try:
            pid = base + offset
            section = build(0x02, 1, pmt_body(pid, [pid + 1]))
            for _ in range(5):
                g.feed_section(pid, section)
        except Exception as exc:  # pragma: no cover - surfaced by assert below
            errors.append(exc)

    errors = []
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(g.tables()["pmt"]) == 3
    assert g.stats()["sections_parsed"] == 15
    for i in range(3):
        assert g.version_of("pmt", pid=base + i) == 1
