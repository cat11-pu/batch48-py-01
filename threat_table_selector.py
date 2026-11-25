"""Threat table maintenance with taunt, distance filtering and time decay."""

from __future__ import annotations

import math
import threading


class ThreatError(ValueError):
    """Domain failure carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class ThreatSelector:
    """Maintains one threat table per hostile mob and picks its current target."""

    def __init__(self, *, heal_factor: float = 0.5, melee_range: float = 5.0,
                 swap_margin: float = 0.15, decay_per_second: float = 10.0) -> None:
        self.heal_factor = float(heal_factor)
        self.melee_range = float(melee_range)
        self.swap_margin = float(swap_margin)
        self.decay_per_second = float(decay_per_second)
        self._lock = threading.RLock()
        self._mobs: dict = {}
        self._stats = self._empty_stats()

    @staticmethod
    def _empty_stats() -> dict:
        return {
            "threats_added": 0,
            "heals": 0,
            "selects": 0,
            "taunts": 0,
            "decays": 0,
        }

    @staticmethod
    def _is_number(value: object) -> bool:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
        try:
            return math.isfinite(float(value))
        except (OverflowError, ValueError):
            return False

    @classmethod
    def _parse_position(cls, position: object) -> tuple[float, float]:
        if not isinstance(position, (tuple, list)) or len(position) != 2:
            raise ThreatError("TH_INVALID_POSITION", position)
        x, y = position
        if not cls._is_number(x) or not cls._is_number(y):
            raise ThreatError("TH_INVALID_POSITION", position)
        return float(x), float(y)

    @classmethod
    def _parse_time(cls, now: object) -> float:
        if not cls._is_number(now):
            raise ThreatError("TH_INVALID_TIME", now)
        return float(now)

    @staticmethod
    def _stable_key(value: object) -> tuple[str, str, str]:
        value_type = type(value)
        return (
            getattr(value_type, "__module__", ""),
            getattr(value_type, "__qualname__", value_type.__name__),
            repr(value),
        )

    @classmethod
    def _distance(cls, first: tuple[float, float],
                  second: tuple[float, float]) -> float:
        return math.hypot(first[0] - second[0], first[1] - second[1])

    def _mob_state(self, mob: object) -> dict:
        try:
            return self._mobs[mob]
        except (KeyError, TypeError) as exc:
            raise ThreatError("TH_UNKNOWN_MOB", mob) from exc

    @staticmethod
    def _source_state(state: dict, source: object) -> dict:
        try:
            return state["sources"][source]
        except (KeyError, TypeError) as exc:
            raise ThreatError("TH_UNKNOWN_SOURCE", source) from exc

    def _eligible(self, state: dict, source: object) -> bool:
        entry = state["sources"][source]
        return (
            entry["alive"]
            and entry["threat"] > 0.0
            and self._distance(state["position"], entry["position"]) <= self.melee_range
        )

    def _ordered_sources(self, state: dict) -> list:
        return sorted(
            state["sources"],
            key=lambda source: (-state["sources"][source]["threat"],
                                self._stable_key(source)),
        )

    def add_mob(self, mob: str, *, position=(0.0, 0.0)) -> dict:
        position = self._parse_position(position)
        with self._lock:
            if mob in self._mobs:
                raise ThreatError("TH_DUPLICATE_MOB", mob)
            self._mobs[mob] = {
                "position": position,
                "sources": {},
                "taunt": None,
                "target": None,
                "last_decay": 0.0,
                "removed": set(),
            }
            return {"mob": mob, "position": position, "sources": 0}

    def add_source(self, mob: str, source: str, *, position=(0.0, 0.0)) -> dict:
        with self._lock:
            state = self._mob_state(mob)
            position = self._parse_position(position)
            if source in state["sources"] or source in state["removed"]:
                raise ThreatError("TH_DUPLICATE_SOURCE", source)
            state["sources"][source] = {
                "position": position,
                "alive": True,
                "threat": 0.0,
            }
            return {
                "mob": mob,
                "source": source,
                "position": position,
                "alive": True,
            }

    def add_threat(self, mob: str, source: str, amount: float, kind: str,
                   *, now: float = 0.0) -> dict:
        with self._lock:
            state = self._mob_state(mob)
            entry = self._source_state(state, source)
            if not self._is_number(amount) or float(amount) <= 0.0:
                raise ThreatError("TH_INVALID_AMOUNT", amount)
            if kind not in ("damage", "heal"):
                raise ThreatError("TH_INVALID_KIND", kind)

            applied = float(amount)
            if kind == "heal":
                applied *= self.heal_factor
            entry["threat"] += applied
            self._stats["threats_added"] += 1
            if kind == "heal":
                self._stats["heals"] += 1
            total = sum(item["threat"] for item in state["sources"].values())
            return {
                "mob": mob,
                "source": source,
                "amount": applied,
                "kind": kind,
                "threat": entry["threat"],
                "total": total,
            }

    def select(self, mob: str, now: float) -> dict:
        now = self._parse_time(now)
        with self._lock:
            state = self._mob_state(mob)
            self._stats["selects"] += 1

            taunt = state["taunt"]
            if taunt is not None:
                if now >= taunt["until"]:
                    state["taunt"] = None
                    taunt = None
                else:
                    source = taunt["source"]
                    entry = state["sources"].get(source)
                    if (
                        entry is not None
                        and entry["alive"]
                        and self._distance(state["position"], entry["position"])
                        <= self.melee_range
                    ):
                        state["target"] = source
                        return {
                            "mob": mob,
                            "target": source,
                            "threat": entry["threat"],
                            "reason": "taunt",
                        }

            eligible = [
                source for source in self._ordered_sources(state)
                if self._eligible(state, source)
            ]
            if not eligible:
                state["target"] = None
                return {
                    "mob": mob,
                    "target": None,
                    "threat": 0.0,
                    "reason": "none",
                }

            best = eligible[0]
            current = state["target"]
            if current in eligible:
                current_threat = state["sources"][current]["threat"]
                best_threat = state["sources"][best]["threat"]
                if best_threat <= current_threat * (1.0 + self.swap_margin):
                    state["target"] = current
                    return {
                        "mob": mob,
                        "target": current,
                        "threat": current_threat,
                        "reason": "hysteresis",
                    }

            state["target"] = best
            return {
                "mob": mob,
                "target": best,
                "threat": state["sources"][best]["threat"],
                "reason": "highest",
            }

    def taunt(self, mob: str, source: str, duration: float,
              *, now: float = 0.0) -> dict:
        now = self._parse_time(now)
        with self._lock:
            state = self._mob_state(mob)
            self._source_state(state, source)
            if not self._is_number(duration) or float(duration) <= 0.0:
                raise ThreatError("TH_INVALID_DURATION", duration)
            until = now + float(duration)
            state["taunt"] = {"source": source, "until": until}
            state["target"] = source
            self._stats["taunts"] += 1
            return {"mob": mob, "source": source, "until": until}

    def decay(self, mob: str, now: float) -> dict:
        now = self._parse_time(now)
        with self._lock:
            state = self._mob_state(mob)
            elapsed = now - state["last_decay"]
            if elapsed < 0.0:
                raise ThreatError("TH_STALE_TIME", now)

            reduction = elapsed * self.decay_per_second
            decayed = {}
            for source, entry in state["sources"].items():
                change = min(entry["threat"], reduction)
                entry["threat"] -= change
                if entry["threat"] < 0.0:
                    entry["threat"] = 0.0
                decayed[source] = change
            state["last_decay"] = now
            self._stats["decays"] += 1
            return {"mob": mob, "decayed": decayed, "now": now}

    def kill(self, mob: str, source: str) -> dict:
        with self._lock:
            state = self._mob_state(mob)
            if source in state["sources"]:
                del state["sources"][source]
                state["removed"].add(source)
                if state["taunt"] is not None and state["taunt"]["source"] == source:
                    state["taunt"] = None
                if state["target"] == source:
                    state["target"] = None
                removed = True
            elif source in state["removed"]:
                removed = False
            else:
                raise ThreatError("TH_UNKNOWN_SOURCE", source)
            return {
                "mob": mob,
                "source": source,
                "removed": removed,
                "sources": len(state["sources"]),
            }

    def table(self, mob: str) -> list:
        with self._lock:
            state = self._mob_state(mob)
            return [
                {
                    "source": source,
                    "threat": state["sources"][source]["threat"],
                    "distance": self._distance(
                        state["position"], state["sources"][source]["position"]
                    ),
                    "alive": state["sources"][source]["alive"],
                }
                for source in self._ordered_sources(state)
            ]

    def threat(self, mob: str, source: str) -> float:
        with self._lock:
            state = self._mob_state(mob)
            return float(self._source_state(state, source)["threat"])

    def snapshot(self) -> dict:
        with self._lock:
            mobs = {}
            for mob, state in self._mobs.items():
                mobs[mob] = {
                    "position": list(state["position"]),
                    "sources": {
                        source: {
                            "position": list(entry["position"]),
                            "alive": entry["alive"],
                            "threat": entry["threat"],
                        }
                        for source, entry in state["sources"].items()
                    },
                    "taunt": (
                        None
                        if state["taunt"] is None
                        else {
                            "source": state["taunt"]["source"],
                            "until": state["taunt"]["until"],
                        }
                    ),
                    "target": state["target"],
                    "last_decay": state["last_decay"],
                    "removed": sorted(state["removed"], key=self._stable_key),
                }
            return {"mobs": mobs, "stats": dict(self._stats)}

    def restore(self, state: dict) -> None:
        with self._lock:
            try:
                mobs, stats = self._parse_snapshot(state)
            except ThreatError:
                raise
            except Exception as exc:
                raise ThreatError("TH_SNAPSHOT_INVALID", str(exc)) from exc
            self._mobs = mobs
            self._stats = stats

    @classmethod
    def _invalid_snapshot(cls, detail: object = None) -> None:
        raise ThreatError("TH_SNAPSHOT_INVALID", detail)

    @classmethod
    def _parse_snapshot(cls, state: object) -> tuple[dict, dict]:
        if not isinstance(state, dict):
            cls._invalid_snapshot(state)
        if "mobs" not in state or "stats" not in state:
            cls._invalid_snapshot("missing mobs or stats")
        raw_mobs = state["mobs"]
        raw_stats = state["stats"]
        if not isinstance(raw_mobs, dict) or not isinstance(raw_stats, dict):
            cls._invalid_snapshot("mobs and stats must be mappings")

        expected_stat_keys = set(cls._empty_stats())
        if set(raw_stats) != expected_stat_keys:
            cls._invalid_snapshot("invalid stats fields")
        stats = {}
        for key in expected_stat_keys:
            value = raw_stats[key]
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
            ):
                cls._invalid_snapshot("invalid stat value")
            stats[key] = value

        mobs = {}
        for mob, raw_state in raw_mobs.items():
            if not isinstance(raw_state, dict):
                cls._invalid_snapshot("mob state must be a mapping")
            for required in ("position", "sources", "taunt", "target",
                             "last_decay", "removed"):
                if required not in raw_state:
                    cls._invalid_snapshot(f"missing mob field {required}")
            position = cls._parse_snapshot_position(raw_state["position"])
            if not isinstance(raw_state["sources"], dict):
                cls._invalid_snapshot("sources must be a mapping")
            if not cls._is_number(raw_state["last_decay"]):
                cls._invalid_snapshot("invalid last_decay")

            sources = {}
            for source, raw_source in raw_state["sources"].items():
                if not isinstance(raw_source, dict):
                    cls._invalid_snapshot("source state must be a mapping")
                for required in ("position", "alive", "threat"):
                    if required not in raw_source:
                        cls._invalid_snapshot(f"missing source field {required}")
                source_position = cls._parse_snapshot_position(
                    raw_source["position"]
                )
                alive = raw_source["alive"]
                threat = raw_source["threat"]
                if not isinstance(alive, bool):
                    cls._invalid_snapshot("alive must be boolean")
                if not cls._is_number(threat) or float(threat) < 0.0:
                    cls._invalid_snapshot("invalid threat")
                sources[source] = {
                    "position": source_position,
                    "alive": alive,
                    "threat": float(threat),
                }

            raw_removed = raw_state["removed"]
            if not isinstance(raw_removed, (list, tuple)):
                cls._invalid_snapshot("removed must be a sequence")
            removed = []
            for source in raw_removed:
                try:
                    duplicate = source in removed
                    registered = source in sources
                except TypeError as exc:
                    raise ThreatError("TH_SNAPSHOT_INVALID", "invalid source") from exc
                if duplicate or registered:
                    cls._invalid_snapshot("invalid removed source")
                removed.append(source)

            raw_taunt = raw_state["taunt"]
            if raw_taunt is not None:
                if not isinstance(raw_taunt, dict):
                    cls._invalid_snapshot("taunt must be a mapping or null")
                if set(raw_taunt) != {"source", "until"}:
                    cls._invalid_snapshot("invalid taunt fields")
                if raw_taunt["source"] not in sources:
                    cls._invalid_snapshot("taunt source is unknown")
                if not cls._is_number(raw_taunt["until"]):
                    cls._invalid_snapshot("invalid taunt until")
                taunt = {
                    "source": raw_taunt["source"],
                    "until": float(raw_taunt["until"]),
                }
            else:
                taunt = None

            target = raw_state["target"]
            if target is not None and target not in sources:
                cls._invalid_snapshot("target source is unknown")

            mobs[mob] = {
                "position": position,
                "sources": sources,
                "taunt": taunt,
                "target": target,
                "last_decay": float(raw_state["last_decay"]),
                "removed": set(removed),
            }
        return mobs, stats

    @classmethod
    def _parse_snapshot_position(cls, position: object) -> tuple[float, float]:
        try:
            return cls._parse_position(position)
        except ThreatError as exc:
            raise ThreatError("TH_SNAPSHOT_INVALID", exc.detail) from exc

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            self._stats = self._empty_stats()
