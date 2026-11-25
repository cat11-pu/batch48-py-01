"""Fishing minigame casting, bite window and settlement guard."""

from __future__ import annotations

import copy
import random
import threading


class FishingError(Exception):
    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class FishingMinigameGuard:
    """Track one active fishing line per player and settle landings once."""

    _ACTIVE_STATES = ("waiting", "biting")
    _STAT_KEYS = ("casts", "bites", "landed", "escaped", "released")

    def __init__(self, *, bite_window_ms: int = 1200, max_tension: int = 100,
                 line_durability: int = 3, tables=None, clock=None) -> None:
        self.bite_window_ms = bite_window_ms
        self.max_tension = max_tension
        self.line_durability = line_durability
        self.tables = copy.deepcopy(dict(tables or {}))
        self._clock = clock
        self._lock = threading.RLock()
        self._stats = {key: 0 for key in self._STAT_KEYS}
        self._next_cast_id = 1
        self._players = {}

    def Cast(self, player, spot, now):
        with self._lock:
            record = self._players.get(player)
            if record is not None and self._is_active(record.get("line")):
                raise FishingError("FISH_LINE_BUSY", player)

            if self._spot_is_taken(spot):
                raise FishingError("FISH_SPOT_TAKEN", spot)

            if record is None:
                record = {
                    "durability": self.line_durability,
                    "line": None,
                }
                self._players[player] = record

            cast_id = self._next_cast_id
            self._next_cast_id += 1
            record["line"] = {
                "cast_id": cast_id,
                "spot": spot,
                "state": "waiting",
                "tension": 0,
                "progress": 0,
                "bite_at": None,
                "deadline": None,
                "bite_output": None,
                "settlement_type": None,
                "settlement": None,
            }
            self._stats["casts"] += 1
            return self._state_for(player, record)

    def Bite(self, player, now):
        with self._lock:
            record, line = self._require_active(player)
            if line["state"] == "biting":
                bite_output = line.get("bite_output")
                if not isinstance(bite_output, dict):
                    bite_output = {
                        "state": "biting",
                        "bite_at": line["bite_at"],
                        "deadline": line["deadline"],
                        "window_ms": self.bite_window_ms,
                    }
                return copy.deepcopy(bite_output)

            deadline = now + self.bite_window_ms
            line["state"] = "biting"
            line["bite_at"] = now
            line["deadline"] = deadline
            line["bite_output"] = {
                "state": "biting",
                "bite_at": now,
                "deadline": deadline,
                "window_ms": self.bite_window_ms,
            }
            self._stats["bites"] += 1
            return copy.deepcopy(line["bite_output"])

    def Reel(self, player, dt_ms):
        with self._lock:
            if not isinstance(dt_ms, int) or isinstance(dt_ms, bool) or dt_ms <= 0:
                raise FishingError("FISH_INTERVAL_INVALID", dt_ms)

            record, line = self._require_active(player)
            progress = min(100, line["progress"] + dt_ms // 50)
            tension = line["tension"] + dt_ms // 100
            line["progress"] = progress
            line["tension"] = tension

            if tension > self.max_tension:
                return self._settle_snap(player, record, line)

            return {
                "progress": line["progress"],
                "tension": line["tension"],
                "durability": record["durability"],
                "state": line["state"],
                "result": None,
            }

    def SetTension(self, player, delta):
        with self._lock:
            record, line = self._require_active(player)
            line["tension"] = max(0, line["tension"] + delta)

            if line["tension"] > self.max_tension:
                return self._settle_snap(player, record, line)

            return {
                "progress": line["progress"],
                "tension": line["tension"],
                "durability": record["durability"],
                "state": line["state"],
                "result": None,
            }

    def Land(self, player, now):
        with self._lock:
            record = self._players.get(player)
            line = record.get("line") if record is not None else None
            if line is not None and line["state"] == "settled":
                raise FishingError("FISH_ALREADY_SETTLED", line["cast_id"])
            if line is None or line["state"] not in self._ACTIVE_STATES:
                raise FishingError("FISH_ALREADY_SETTLED", None)

            if line["state"] == "biting" and now >= line["deadline"]:
                return self._settle_window_closed(record, line)

            if line["progress"] < 100:
                raise FishingError("FISH_NOT_READY", line["progress"])

            species = self._choose_species(line["spot"])
            result = {
                "result": "landed",
                "cause": None,
                "species": species,
                "weight_g": random.randint(200, 999),
                "durability": record["durability"],
                "progress": line["progress"],
            }
            self._stats["landed"] += 1
            self._mark_settled(line, "landed", result)
            return copy.deepcopy(result)

    def Escaped(self, player):
        with self._lock:
            record = self._players.get(player)
            line = record.get("line") if record is not None else None
            if line is None:
                raise FishingError("FISH_ALREADY_SETTLED", None)

            if line["state"] == "settled":
                if line["settlement_type"] in ("escaped", "snapped"):
                    return copy.deepcopy(line["settlement"])
                raise FishingError("FISH_ALREADY_SETTLED", line["cast_id"])

            result = {
                "result": "escaped",
                "cause": "player_escaped",
                "species": None,
                "weight_g": None,
                "durability": record["durability"],
                "progress": line["progress"],
                "state": "idle",
                "cast_id": line["cast_id"],
            }
            self._stats["escaped"] += 1
            self._mark_settled(line, "escaped", result)
            return copy.deepcopy(result)

    def Release(self, player):
        with self._lock:
            record = self._players.get(player)
            line = record.get("line") if record is not None else None
            if line is None:
                return {"state": "idle", "released": False, "cast_id": 0}

            if line["state"] == "settled":
                if line["settlement_type"] == "released":
                    return copy.deepcopy(line["settlement"])
                return {
                    "state": "idle",
                    "released": False,
                    "cast_id": line["cast_id"],
                }

            result = {
                "state": "idle",
                "released": True,
                "cast_id": line["cast_id"],
            }
            self._stats["released"] += 1
            self._mark_settled(line, "released", result)
            return copy.deepcopy(result)

    def State(self, player):
        with self._lock:
            record = self._players.get(player)
            if record is None:
                return {
                    "player": player,
                    "state": "idle",
                    "spot": None,
                    "cast_id": 0,
                    "tension": 0,
                    "progress": 0,
                    "durability": self.line_durability,
                    "deadline": None,
                }
            return self._state_for(player, record)

    def SpeciesOdds(self, spot, now=None):
        with self._lock:
            if spot not in self.tables:
                raise FishingError("FISH_UNKNOWN_SPOT", spot)
            return copy.deepcopy(self.tables[spot])

    def Snapshot(self):
        with self._lock:
            return {
                "version": 1,
                "stats": copy.deepcopy(self._stats),
                "next_cast_id": self._next_cast_id,
                "players": copy.deepcopy(self._players),
            }

    def Restore(self, state):
        with self._lock:
            parsed = self._validate_snapshot(state)
            self._stats = parsed["stats"]
            self._next_cast_id = parsed["next_cast_id"]
            self._players = parsed["players"]

    def Stats(self):
        with self._lock:
            return copy.deepcopy(self._stats)

    def _state_for(self, player, record):
        line = record.get("line")
        if line is not None and line["settlement_type"] == "snapped":
            return {
                "player": player,
                "state": "settled",
                "spot": line["spot"],
                "cast_id": line["cast_id"],
                "tension": line["tension"],
                "progress": line["progress"],
                "durability": record["durability"],
                "deadline": line["deadline"],
            }
        if line is None or line["state"] == "settled":
            return {
                "player": player,
                "state": "idle",
                "spot": None,
                "cast_id": line["cast_id"] if line is not None else 0,
                "tension": 0,
                "progress": 0,
                "durability": record["durability"],
                "deadline": None,
            }
        return {
            "player": player,
            "state": line["state"],
            "spot": line["spot"],
            "cast_id": line["cast_id"],
            "tension": line["tension"],
            "progress": line["progress"],
            "durability": record["durability"],
            "deadline": line["deadline"],
        }

    @classmethod
    def _is_active(cls, line):
        return line is not None and line["state"] in cls._ACTIVE_STATES

    def _spot_is_taken(self, spot):
        for record in self._players.values():
            line = record.get("line")
            if self._is_active(line) and line["spot"] == spot:
                return True
        return False

    def _require_active(self, player):
        record = self._players.get(player)
        line = record.get("line") if record is not None else None
        if line is None or line["state"] not in self._ACTIVE_STATES:
            raise FishingError(
                "FISH_ALREADY_SETTLED",
                line["cast_id"] if line is not None else None,
            )
        return record, line

    def _settle_snap(self, player, record, line):
        record["durability"] = max(0, record["durability"] - 1)
        self._stats["escaped"] += 1
        result = {
            "progress": line["progress"],
            "tension": line["tension"],
            "durability": record["durability"],
            "state": "settled",
            "result": "snapped",
            "cause": "tension_exceeded",
        }
        self._mark_settled(line, "snapped", result)
        return copy.deepcopy(result)

    def _settle_window_closed(self, record, line):
        record["durability"] = max(0, record["durability"] - 1)
        self._stats["escaped"] += 1
        result = {
            "result": "escaped",
            "cause": "window_closed",
            "species": None,
            "weight_g": None,
            "durability": record["durability"],
            "progress": line["progress"],
        }
        self._mark_settled(line, "escaped", result)
        return copy.deepcopy(result)

    @staticmethod
    def _mark_settled(line, settlement_type, result):
        line["state"] = "settled"
        line["settlement_type"] = settlement_type
        line["settlement"] = copy.deepcopy(result)

    def _choose_species(self, spot):
        if spot not in self.tables:
            raise FishingError("FISH_UNKNOWN_SPOT", spot)
        weights = self.tables[spot]
        species = list(weights)
        if not species:
            raise FishingError("FISH_UNKNOWN_SPOT", spot)
        return random.choices(species, weights=[weights[name] for name in species], k=1)[0]

    def _validate_snapshot(self, state):
        try:
            if not isinstance(state, dict):
                raise ValueError("snapshot must be a dict")

            stats = state.get("stats")
            if not isinstance(stats, dict):
                raise ValueError("stats must be a dict")
            clean_stats = {}
            for key in self._STAT_KEYS:
                value = stats.get(key)
                if not self._is_int(value) or value < 0:
                    raise ValueError("invalid stat")
                clean_stats[key] = value

            next_cast_id = state.get("next_cast_id")
            if not self._is_int(next_cast_id) or next_cast_id < 1:
                raise ValueError("invalid next_cast_id")

            players = state.get("players")
            if not isinstance(players, dict):
                raise ValueError("players must be a dict")

            clean_players = {}
            max_cast_id = 0
            for player, record in players.items():
                if not isinstance(record, dict):
                    raise ValueError("invalid player record")
                durability = record.get("durability")
                if not self._is_int(durability) or durability < 0:
                    raise ValueError("invalid durability")
                if "line" not in record:
                    raise ValueError("missing line")
                line = record.get("line")
                if line is None:
                    clean_players[player] = {
                        "durability": durability,
                        "line": None,
                    }
                    continue
                if not isinstance(line, dict):
                    raise ValueError("invalid line")
                max_cast_id = max(max_cast_id, self._validate_line(line))
                clean_players[player] = {
                    "durability": durability,
                    "line": copy.deepcopy(line),
                }

            if next_cast_id <= max_cast_id:
                raise ValueError("next_cast_id behind existing casts")

            return {
                "stats": clean_stats,
                "next_cast_id": next_cast_id,
                "players": clean_players,
            }
        except Exception:
            raise FishingError("FISH_SNAPSHOT_INVALID", "invalid snapshot") from None

    def _validate_line(self, line):
        cast_id = line.get("cast_id")
        if not self._is_int(cast_id) or cast_id < 1:
            raise ValueError("invalid cast_id")

        if "spot" not in line:
            raise ValueError("missing spot")
        line_state = line.get("state")
        if line_state not in ("waiting", "biting", "settled"):
            raise ValueError("invalid line state")

        tension = line.get("tension")
        progress = line.get("progress")
        if not self._is_int(tension) or tension < 0:
            raise ValueError("invalid tension")
        if not self._is_int(progress) or not 0 <= progress <= 100:
            raise ValueError("invalid progress")

        for key in ("bite_at", "deadline"):
            value = line.get(key)
            if value is not None and not self._is_int(value):
                raise ValueError("invalid time")

        settlement_type = line.get("settlement_type")
        settlement = line.get("settlement")
        if line_state == "waiting":
            if line.get("bite_at") is not None or line.get("deadline") is not None:
                raise ValueError("waiting line has a bite window")
            if settlement_type is not None or settlement is not None:
                raise ValueError("waiting line already settled")
        elif line_state == "biting":
            if not self._is_int(line.get("bite_at")):
                raise ValueError("missing bite_at")
            if not self._is_int(line.get("deadline")):
                raise ValueError("missing deadline")
            if settlement_type is not None or settlement is not None:
                raise ValueError("biting line already settled")
        else:
            if settlement_type not in ("landed", "escaped", "snapped", "released"):
                raise ValueError("invalid settlement")
            if not isinstance(settlement, dict):
                raise ValueError("missing settlement")

        return cast_id

    @staticmethod
    def _is_int(value):
        return isinstance(value, int) and not isinstance(value, bool)
