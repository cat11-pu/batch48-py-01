"""Reliable game event channels with TTL, cumulative acks and retransmit budgets."""

from __future__ import annotations

import copy
import threading


_STAT_KEYS = (
    "published",
    "sent_once",
    "merged",
    "duplicates",
    "acked",
    "retransmits",
    "expired",
    "dead_letters",
    "collects",
)


class EventError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_integer(value: object, *, minimum: int, code: str) -> int:
    if not _is_integer(value) or value < minimum:
        raise EventError(code)
    return value


def _require_name(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise EventError("EVENT_INVALID_EVENT")
    return value


class ReliableEventGuard:
    """Grades event delivery, acknowledges cumulatively and bounds retransmits."""

    def __init__(self, *, max_retransmits: int = 3, backoff_ticks: int = 2,
                 bandwidth_quota: int = 8, dead_letter_limit: int = 64) -> None:
        self.max_retransmits = _require_integer(
            max_retransmits, minimum=0, code="EVENT_INVALID_ARGUMENT"
        )
        self.backoff_ticks = _require_integer(
            backoff_ticks, minimum=0, code="EVENT_INVALID_ARGUMENT"
        )
        self.bandwidth_quota = _require_integer(
            bandwidth_quota, minimum=0, code="EVENT_INVALID_ARGUMENT"
        )
        self.dead_letter_limit = _require_integer(
            dead_letter_limit, minimum=0, code="EVENT_INVALID_ARGUMENT"
        )
        self._lock = threading.RLock()
        self._channels: dict[str, dict] = {}
        self._dead_letters: list[dict] = []
        self._stats = {key: 0 for key in _STAT_KEYS}

    def register_channel(self, channel: str, *, reliable: bool,
                         mergeable: bool = False) -> dict:
        _require_name(channel)
        if not isinstance(reliable, bool) or not isinstance(mergeable, bool):
            raise EventError("EVENT_INVALID_ARGUMENT")

        with self._lock:
            if channel in self._channels:
                raise EventError("EVENT_DUPLICATE_CHANNEL")
            self._channels[channel] = {
                "reliable": reliable,
                "mergeable": mergeable,
                "next_seq": 1,
                "watermark": 0,
                "events": {},
                "identities": {},
            }
            return {
                "channel": channel,
                "reliable": reliable,
                "mergeable": mergeable,
            }

    def publish(self, channel: str, event_id: str, payload: object = None, *,
                tick: int = 0, ttl_ticks: int = 6) -> dict:
        _require_name(channel)
        _require_integer(tick, minimum=0, code="EVENT_INVALID_ARGUMENT")
        _require_integer(ttl_ticks, minimum=1, code="EVENT_INVALID_ARGUMENT")
        _require_name(event_id)

        with self._lock:
            state = self._known_channel(channel)
            payload_copy = copy.deepcopy(payload)
            previous_seq = state["identities"].get(event_id)

            if event_id in state["identities"]:
                active_event = None
                if previous_seq is not None:
                    active_event = state["events"].get(previous_seq)

                if state["reliable"] and state["mergeable"] and active_event is not None:
                    active_event["payload"] = payload_copy
                    active_event["publish_tick"] = tick
                    active_event["ttl_ticks"] = ttl_ticks
                    active_event["expire_tick"] = tick + ttl_ticks
                    self._stats["merged"] += 1
                    return {
                        "channel": channel,
                        "event_id": event_id,
                        "seq": previous_seq,
                        "status": "merged",
                    }

                self._stats["duplicates"] += 1
                return {
                    "channel": channel,
                    "event_id": event_id,
                    "seq": previous_seq,
                    "status": "duplicate",
                }

            self._stats["published"] += 1
            state["identities"][event_id] = None

            if not state["reliable"]:
                self._stats["sent_once"] += 1
                return {
                    "channel": channel,
                    "event_id": event_id,
                    "seq": None,
                    "status": "fire_and_forget",
                }

            seq = state["next_seq"]
            state["next_seq"] = seq + 1
            state["identities"][event_id] = seq
            state["events"][seq] = {
                "seq": seq,
                "event_id": event_id,
                "payload": payload_copy,
                "publish_tick": tick,
                "ttl_ticks": ttl_ticks,
                "expire_tick": tick + ttl_ticks,
                "next_retry_tick": tick + self.backoff_ticks,
                "retransmits": 0,
            }
            return {
                "channel": channel,
                "event_id": event_id,
                "seq": seq,
                "status": "queued",
            }

    def ack(self, channel: str, upto_seq: int) -> dict:
        _require_name(channel)
        _require_integer(upto_seq, minimum=0, code="EVENT_INVALID_ARGUMENT")

        with self._lock:
            state = self._known_channel(channel)
            if not state["reliable"]:
                raise EventError("EVENT_UNRELIABLE_CHANNEL")

            acked: list[int] = []
            for seq in sorted(state["events"]):
                if seq > upto_seq:
                    break
                acked.append(seq)
                del state["events"][seq]

            self._stats["acked"] += len(acked)
            highest_seq = state["next_seq"] - 1
            acknowledged_watermark = min(upto_seq, highest_seq)
            if acknowledged_watermark > state["watermark"]:
                state["watermark"] = acknowledged_watermark

            return {
                "channel": channel,
                "acked": acked,
                "watermark": state["watermark"],
            }

    def pending(self, channel: str) -> list:
        _require_name(channel)
        with self._lock:
            state = self._known_channel(channel)
            if not state["reliable"]:
                return []
            return sorted(state["events"])

    def pending_count(self) -> int:
        with self._lock:
            return sum(len(state["events"]) for state in self._channels.values())

    def collect(self, tick: int) -> dict:
        _require_integer(tick, minimum=0, code="EVENT_INVALID_ARGUMENT")

        with self._lock:
            self._stats["collects"] += 1
            result = {
                "tick": tick,
                "resend": [],
                "expired": [],
                "dead": [],
                "bandwidth_used": 0,
            }

            expired_items = []
            for channel_name, state in self._channels.items():
                if not state["reliable"]:
                    continue
                for seq, event in state["events"].items():
                    if tick >= event["expire_tick"]:
                        expired_items.append(
                            (event["expire_tick"], seq, channel_name, event)
                        )

            expired_items.sort(key=lambda item: (item[0], item[1]))
            for _, seq, channel_name, event in expired_items:
                state = self._channels[channel_name]
                if state["events"].get(seq) is not event:
                    continue
                del state["events"][seq]
                self._stats["expired"] += 1
                result["expired"].append(seq)

            over_limit_items = []
            candidate_items = []
            for channel_name, state in self._channels.items():
                if not state["reliable"]:
                    continue
                for seq, event in state["events"].items():
                    if event["next_retry_tick"] > tick:
                        continue
                    item = (event["next_retry_tick"], seq, channel_name, event)
                    if event["retransmits"] >= self.max_retransmits:
                        over_limit_items.append(item)
                    else:
                        candidate_items.append(item)

            over_limit_items.sort(key=lambda item: (item[0], item[1]))
            for _, seq, channel_name, event in over_limit_items:
                state = self._channels[channel_name]
                if state["events"].get(seq) is not event:
                    continue
                self._move_to_dead_letter(channel_name, state, seq, event, tick)
                result["dead"].append(seq)

            candidate_items.sort(key=lambda item: (item[0], item[1]))
            for _, seq, channel_name, event in candidate_items:
                if result["bandwidth_used"] >= self.bandwidth_quota:
                    break

                state = self._channels[channel_name]
                if state["events"].get(seq) is not event:
                    continue

                result["resend"].append(seq)
                result["bandwidth_used"] += 1
                self._stats["retransmits"] += 1
                event["retransmits"] += 1
                event["next_retry_tick"] = tick + self.backoff_ticks

                if event["retransmits"] >= self.max_retransmits:
                    self._move_to_dead_letter(channel_name, state, seq, event, tick)
                    result["dead"].append(seq)

            return result

    def dead_letters(self, channel: str | None = None) -> list:
        if channel is not None:
            _require_name(channel)

        with self._lock:
            if channel is not None:
                self._known_channel(channel)
                records = [
                    record for record in self._dead_letters
                    if record["channel"] == channel
                ]
            else:
                records = self._dead_letters
            return copy.deepcopy(records)

    def watermark(self, channel: str) -> int:
        _require_name(channel)
        with self._lock:
            state = self._known_channel(channel)
            return state["watermark"] if state["reliable"] else 0

    def next_seq(self, channel: str) -> int:
        _require_name(channel)
        with self._lock:
            state = self._known_channel(channel)
            return state["next_seq"] if state["reliable"] else 1

    def channels(self) -> dict:
        with self._lock:
            return {
                name: {
                    "reliable": state["reliable"],
                    "mergeable": state["mergeable"],
                }
                for name, state in self._channels.items()
            }

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            for key in _STAT_KEYS:
                self._stats[key] = 0

    def snapshot(self) -> dict:
        with self._lock:
            state = {
                "version": 1,
                "channels": {
                    name: {
                        "reliable": channel["reliable"],
                        "mergeable": channel["mergeable"],
                        "next_seq": channel["next_seq"],
                        "watermark": channel["watermark"],
                        "identities": channel["identities"],
                        "events": [
                            channel["events"][seq]
                            for seq in sorted(channel["events"])
                        ],
                    }
                    for name, channel in self._channels.items()
                },
                "dead_letters": self._dead_letters,
                "stats": self._stats,
            }
            return copy.deepcopy(state)

    def restore(self, state: dict) -> None:
        with self._lock:
            try:
                restored_channels, restored_dead_letters, restored_stats = (
                    self._parse_snapshot(state)
                )
            except Exception as exc:
                if isinstance(exc, EventError) and exc.code == "EVENT_SNAPSHOT_INVALID":
                    raise
                raise EventError("EVENT_SNAPSHOT_INVALID") from exc

            self._channels = restored_channels
            self._dead_letters = restored_dead_letters
            self._stats = restored_stats

    def _known_channel(self, channel: str) -> dict:
        try:
            return self._channels[channel]
        except KeyError:
            raise EventError("EVENT_UNKNOWN_CHANNEL") from None

    def _move_to_dead_letter(self, channel_name: str, state: dict, seq: int,
                             event: dict, tick: int) -> None:
        state["events"].pop(seq, None)
        record = {
            "channel": channel_name,
            "seq": seq,
            "event_id": event["event_id"],
            "payload": copy.deepcopy(event.get("payload")),
            "status": "dead",
            "reason": "max_retransmits",
            "dead_tick": tick,
            "tick": tick,
            "retransmits": event["retransmits"],
            "publish_tick": event["publish_tick"],
            "ttl_ticks": event["ttl_ticks"],
            "expire_tick": event["expire_tick"],
            "next_retry_tick": event["next_retry_tick"],
        }
        self._dead_letters.append(record)

        overflow = len(self._dead_letters) - self.dead_letter_limit
        if self.dead_letter_limit == 0:
            self._dead_letters.clear()
        elif overflow > 0:
            del self._dead_letters[:overflow]

        self._stats["dead_letters"] += 1

    def _parse_snapshot(self, raw_state: object) -> tuple[dict, list, dict]:
        if not isinstance(raw_state, dict):
            raise EventError("EVENT_SNAPSHOT_INVALID")

        data = copy.deepcopy(raw_state)
        if data.get("version") != 1 or not _is_integer(data.get("version")):
            raise EventError("EVENT_SNAPSHOT_INVALID")
        if not isinstance(data.get("channels"), dict):
            raise EventError("EVENT_SNAPSHOT_INVALID")
        if not isinstance(data.get("dead_letters"), list):
            raise EventError("EVENT_SNAPSHOT_INVALID")
        if not isinstance(data.get("stats"), dict):
            raise EventError("EVENT_SNAPSHOT_INVALID")

        stats = self._parse_stats(data["stats"])
        channels: dict[str, dict] = {}

        for channel_name, raw_channel in data["channels"].items():
            _require_name(channel_name)
            if not isinstance(raw_channel, dict):
                raise EventError("EVENT_SNAPSHOT_INVALID")

            reliable = raw_channel.get("reliable")
            mergeable = raw_channel.get("mergeable")
            if not isinstance(reliable, bool) or not isinstance(mergeable, bool):
                raise EventError("EVENT_SNAPSHOT_INVALID")

            next_seq = raw_channel.get("next_seq")
            watermark = raw_channel.get("watermark")
            if not _is_integer(next_seq) or next_seq < 1:
                raise EventError("EVENT_SNAPSHOT_INVALID")
            if not _is_integer(watermark) or watermark < 0 or watermark >= next_seq:
                raise EventError("EVENT_SNAPSHOT_INVALID")

            raw_identities = raw_channel.get("identities")
            if not isinstance(raw_identities, dict):
                raise EventError("EVENT_SNAPSHOT_INVALID")

            identities = {}
            used_seqs = set()
            highest_identity_seq = 0
            for event_id, identity_seq in raw_identities.items():
                _require_name(event_id)
                if identity_seq is None:
                    if reliable:
                        raise EventError("EVENT_SNAPSHOT_INVALID")
                else:
                    if not _is_integer(identity_seq) or identity_seq < 1:
                        raise EventError("EVENT_SNAPSHOT_INVALID")
                    if identity_seq in used_seqs or identity_seq >= next_seq:
                        raise EventError("EVENT_SNAPSHOT_INVALID")
                    used_seqs.add(identity_seq)
                    highest_identity_seq = max(highest_identity_seq, identity_seq)
                identities[event_id] = identity_seq

            if highest_identity_seq + 1 != next_seq:
                raise EventError("EVENT_SNAPSHOT_INVALID")

            raw_events = raw_channel.get("events")
            if not isinstance(raw_events, list):
                raise EventError("EVENT_SNAPSHOT_INVALID")
            if not reliable and raw_events:
                raise EventError("EVENT_SNAPSHOT_INVALID")
            if not reliable and (next_seq != 1 or watermark != 0):
                raise EventError("EVENT_SNAPSHOT_INVALID")

            events = {}
            event_seqs = set()
            for raw_event in raw_events:
                event = self._parse_event(raw_event, next_seq, watermark)
                seq = event["seq"]
                event_id = event["event_id"]
                if seq in event_seqs:
                    raise EventError("EVENT_SNAPSHOT_INVALID")
                if identities.get(event_id) != seq:
                    raise EventError("EVENT_SNAPSHOT_INVALID")
                event_seqs.add(seq)
                events[seq] = event

            channels[channel_name] = {
                "reliable": reliable,
                "mergeable": mergeable,
                "next_seq": next_seq,
                "watermark": watermark,
                "events": events,
                "identities": identities,
            }

        dead_letters = self._parse_dead_letters(
            data["dead_letters"], channels
        )
        if self.dead_letter_limit == 0:
            dead_letters = []
        elif len(dead_letters) > self.dead_letter_limit:
            dead_letters = dead_letters[-self.dead_letter_limit:]

        return channels, dead_letters, stats

    def _parse_stats(self, raw_stats: dict) -> dict:
        if set(raw_stats) != set(_STAT_KEYS):
            raise EventError("EVENT_SNAPSHOT_INVALID")
        stats = {}
        for key in _STAT_KEYS:
            value = raw_stats[key]
            if not _is_integer(value) or value < 0:
                raise EventError("EVENT_SNAPSHOT_INVALID")
            stats[key] = value
        return stats

    def _parse_event(self, raw_event: object, next_seq: int,
                     watermark: int) -> dict:
        if not isinstance(raw_event, dict):
            raise EventError("EVENT_SNAPSHOT_INVALID")

        seq = raw_event.get("seq")
        event_id = raw_event.get("event_id")
        if not _is_integer(seq) or seq < 1 or seq >= next_seq:
            raise EventError("EVENT_SNAPSHOT_INVALID")
        if seq <= watermark:
            raise EventError("EVENT_SNAPSHOT_INVALID")
        _require_name(event_id)

        publish_tick = raw_event.get("publish_tick")
        ttl_ticks = raw_event.get("ttl_ticks")
        expire_tick = raw_event.get("expire_tick")
        next_retry_tick = raw_event.get("next_retry_tick")
        retransmits = raw_event.get("retransmits")

        if not _is_integer(publish_tick) or publish_tick < 0:
            raise EventError("EVENT_SNAPSHOT_INVALID")
        if not _is_integer(ttl_ticks) or ttl_ticks < 1:
            raise EventError("EVENT_SNAPSHOT_INVALID")
        if not _is_integer(expire_tick) or expire_tick < 0:
            raise EventError("EVENT_SNAPSHOT_INVALID")
        if expire_tick != publish_tick + ttl_ticks:
            raise EventError("EVENT_SNAPSHOT_INVALID")
        if not _is_integer(next_retry_tick) or next_retry_tick < 0:
            raise EventError("EVENT_SNAPSHOT_INVALID")
        if not _is_integer(retransmits) or retransmits < 0:
            raise EventError("EVENT_SNAPSHOT_INVALID")

        return {
            "seq": seq,
            "event_id": event_id,
            "payload": copy.deepcopy(raw_event.get("payload")),
            "publish_tick": publish_tick,
            "ttl_ticks": ttl_ticks,
            "expire_tick": expire_tick,
            "next_retry_tick": next_retry_tick,
            "retransmits": retransmits,
        }

    def _parse_dead_letters(self, raw_records: list,
                            channels: dict[str, dict]) -> list[dict]:
        records = []
        for raw_record in raw_records:
            if not isinstance(raw_record, dict):
                raise EventError("EVENT_SNAPSHOT_INVALID")

            channel_name = raw_record.get("channel")
            seq = raw_record.get("seq")
            event_id = raw_record.get("event_id")
            if channel_name not in channels:
                raise EventError("EVENT_SNAPSHOT_INVALID")
            channel = channels[channel_name]
            if not channel["reliable"]:
                raise EventError("EVENT_SNAPSHOT_INVALID")
            if not _is_integer(seq) or seq < 1 or seq >= channel["next_seq"]:
                raise EventError("EVENT_SNAPSHOT_INVALID")
            if channel["identities"].get(event_id) != seq:
                raise EventError("EVENT_SNAPSHOT_INVALID")
            _require_name(event_id)

            records.append(copy.deepcopy(raw_record))
        return records
