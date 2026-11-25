"""Cooldown and charge tracking with haste, pause and snapshot support."""

from __future__ import annotations

import math
import threading
from numbers import Real


CD_DUPLICATE_SKILL = "CD_DUPLICATE_SKILL"
CD_INVALID_ARGUMENT = "CD_INVALID_ARGUMENT"
CD_UNKNOWN_SKILL = "CD_UNKNOWN_SKILL"
CD_NO_CHARGES = "CD_NO_CHARGES"
CD_GLOBAL_COOLDOWN = "CD_GLOBAL_COOLDOWN"
CD_STALE_TIME = "CD_STALE_TIME"
CD_ALREADY_PAUSED = "CD_ALREADY_PAUSED"
CD_NOT_PAUSED = "CD_NOT_PAUSED"
CD_SNAPSHOT_INVALID = "CD_SNAPSHOT_INVALID"

_STAT_KEYS = ("uses", "denied", "recovered", "pauses")


class CooldownError(ValueError):
    """Domain failure carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _invalid(detail: object) -> CooldownError:
    return CooldownError(CD_INVALID_ARGUMENT, detail)


def _positive_int(value: object, field: str) -> int:
    if not _is_int(value) or value <= 0:
        raise _invalid({"field": field, "value": value})
    return value


def _non_negative_int(value: object, field: str) -> int:
    if not _is_int(value) or value < 0:
        raise _invalid({"field": field, "value": value})
    return value


def _finite_non_negative_float(value: object, field: str, *,
                                positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise _invalid({"field": field, "value": value})
    result = float(value)
    if not math.isfinite(result) or result < 0 or (positive and result == 0):
        raise _invalid({"field": field, "value": value})
    return result


class CooldownTracker:
    """Track per-skill charges, recovery timers, GCD, haste and pause state."""

    def __init__(self, *, gcd_ms: int = 1500, haste_cap: float = 0.5) -> None:
        self.gcd_ms = _non_negative_int(gcd_ms, "gcd_ms")
        self.haste_cap = _finite_non_negative_float(
            haste_cap, "haste_cap"
        )

        self._lock = threading.RLock()
        self._skills: dict[object, dict] = {}
        self._haste = 0.0
        self._paused_at: int | None = None
        self._gcd_ready_at: int | None = None
        self._last_now: int | None = None
        self._stats = {key: 0 for key in _STAT_KEYS}

    def register(self, skill_id: str, *, cooldown_ms: int, charges: int = 1,
                 recovery_ms: int | None = None,
                 gcd_ms: int | None = None) -> dict:
        with self._lock:
            try:
                hash(skill_id)
            except TypeError as exc:
                raise _invalid({"field": "skill_id", "value": skill_id}) from exc

            if skill_id in self._skills:
                raise CooldownError(
                    CD_DUPLICATE_SKILL, {"skill": skill_id}
                )

            cooldown = _positive_int(cooldown_ms, "cooldown_ms")
            max_charges = _positive_int(charges, "charges")
            recovery = (
                cooldown
                if recovery_ms is None
                else _positive_int(recovery_ms, "recovery_ms")
            )
            skill_gcd = (
                self.gcd_ms
                if gcd_ms is None
                else _non_negative_int(gcd_ms, "gcd_ms")
            )

            self._skills[skill_id] = {
                "cooldown_ms": cooldown,
                "max_charges": max_charges,
                "charges": max_charges,
                "recovery_ms": recovery,
                "gcd_ms": skill_gcd,
                "recovery_at": None,
            }

            return {
                "skill": skill_id,
                "cooldown_ms": cooldown,
                "charges": max_charges,
                "recovery_ms": recovery,
                "gcd_ms": skill_gcd,
            }

    def use(self, skill_id: str, now: int) -> dict:
        with self._lock:
            try:
                return self._use_locked(skill_id, now)
            except CooldownError:
                self._stats["denied"] += 1
                raise

    def ready(self, skill_id: str, now: int) -> bool:
        with self._lock:
            skill = self._require_skill(skill_id)
            self._check_clock_locked(now)
            effective_now = self._effective_now_locked(now)

            gcd_ready = (
                self._gcd_ready_at is None
                or effective_now >= self._gcd_ready_at
            )
            return (
                self._available_charges_locked(skill, effective_now) > 0
                and gcd_ready
            )

    def charges(self, skill_id: str) -> int:
        with self._lock:
            return self._require_skill(skill_id)["charges"]

    def remaining(self, skill_id: str, now: int) -> int:
        with self._lock:
            skill = self._require_skill(skill_id)
            self._check_clock_locked(now)
            effective_now = self._effective_now_locked(now)
            return self._remaining_locked(skill, effective_now)

    def apply_haste(self, pct: float) -> dict:
        if isinstance(pct, bool) or not isinstance(pct, Real):
            raise _invalid({"field": "pct", "value": pct})
        pct_value = float(pct)
        if not math.isfinite(pct_value):
            raise _invalid({"field": "pct", "value": pct})
        with self._lock:
            self._haste = max(0.0, min(self.haste_cap, self._haste + pct_value))
            return {"haste": self._haste, "cap": self.haste_cap}

    def pause(self, now: int) -> dict:
        with self._lock:
            if self._paused_at is not None:
                raise CooldownError(
                    CD_ALREADY_PAUSED, {"paused_at": self._paused_at}
                )
            self._check_clock_locked(now)
            self._advance_all_locked(now)
            self._last_now = now
            self._paused_at = now
            self._stats["pauses"] += 1
            return {"paused_at": now}

    def resume(self, now: int) -> dict:
        with self._lock:
            if self._paused_at is None:
                raise CooldownError(CD_NOT_PAUSED, None)
            self._check_clock_locked(now)

            paused_at = self._paused_at
            if now < paused_at:
                raise CooldownError(
                    CD_STALE_TIME,
                    {"now": now, "paused_at": paused_at},
                )

            paused_duration = now - paused_at
            for skill in self._skills.values():
                if skill["recovery_at"] is not None:
                    skill["recovery_at"] += paused_duration
            if self._gcd_ready_at is not None:
                self._gcd_ready_at += paused_duration

            self._paused_at = None
            self._last_now = now
            self._advance_all_locked(now)
            return {"resumed_at": now}

    def advance(self, now: int) -> dict:
        with self._lock:
            self._check_clock_locked(now)
            self._last_now = now

            recovered: dict[object, int] = {}
            if self._paused_at is None:
                recovered = self._advance_all_locked(now)
            return {"recovered": recovered}

    def snapshot(self) -> dict:
        with self._lock:
            effective_now: int | None
            if self._paused_at is not None:
                effective_now = self._paused_at
            else:
                effective_now = self._last_now

            skills = {}
            for skill_id, skill in self._skills.items():
                remaining = (
                    0
                    if effective_now is None
                    else self._remaining_locked(skill, effective_now)
                )
                skills[skill_id] = {
                    "skill": skill_id,
                    "cooldown_ms": skill["cooldown_ms"],
                    "max_charges": skill["max_charges"],
                    "charges": skill["charges"],
                    "recovery_ms": skill["recovery_ms"],
                    "gcd_ms": skill["gcd_ms"],
                    "recovery_at": skill["recovery_at"],
                    "remaining_ms": remaining,
                    "remaining": remaining,
                }

            return {
                "version": 1,
                "gcd_ms": self.gcd_ms,
                "haste_cap": self.haste_cap,
                "haste": self._haste,
                "paused_at": self._paused_at,
                "last_now": self._last_now,
                "gcd_ready_at": self._gcd_ready_at,
                "stats": dict(self._stats),
                "skills": skills,
            }

    def restore(self, state: dict) -> None:
        with self._lock:
            try:
                parsed = self._parse_snapshot(state)
            except CooldownError as exc:
                if exc.code == CD_SNAPSHOT_INVALID:
                    raise
                raise CooldownError(
                    CD_SNAPSHOT_INVALID, exc.detail
                ) from exc
            except Exception as exc:
                raise CooldownError(
                    CD_SNAPSHOT_INVALID, {"reason": str(exc)}
                ) from exc

            self.gcd_ms = parsed["gcd_ms"]
            self.haste_cap = parsed["haste_cap"]
            self._haste = parsed["haste"]
            self._paused_at = parsed["paused_at"]
            self._last_now = parsed["last_now"]
            self._gcd_ready_at = parsed["gcd_ready_at"]
            self._stats = parsed["stats"]
            self._skills = parsed["skills"]

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            for key in _STAT_KEYS:
                self._stats[key] = 0

    def _require_skill(self, skill_id: object) -> dict:
        try:
            skill = self._skills[skill_id]
        except (KeyError, TypeError) as exc:
            raise CooldownError(
                CD_UNKNOWN_SKILL, {"skill": skill_id}
            ) from exc
        return skill

    def _check_clock_locked(self, now: object) -> None:
        if not _is_int(now):
            raise CooldownError(CD_INVALID_ARGUMENT, {"field": "now"})
        if self._last_now is not None and now < self._last_now:
            raise CooldownError(
                CD_STALE_TIME,
                {"now": now, "last_now": self._last_now},
            )

    def _effective_now_locked(self, now: int) -> int:
        return self._paused_at if self._paused_at is not None else now

    def _scaled_duration_locked(self, base_ms: int) -> int:
        haste = min(max(self._haste, 0.0), self.haste_cap)
        factor = max(0.0, 1.0 - haste)
        if factor == 0:
            return 0
        return math.ceil(base_ms * factor - 1e-9)

    def _remaining_locked(self, skill: dict, now: int) -> int:
        if skill["charges"] >= skill["max_charges"]:
            return 0
        recovery_at = skill["recovery_at"]
        if recovery_at is None:
            return 0
        return max(0, recovery_at - now)

    def _available_charges_locked(self, skill: dict, now: int) -> int:
        charges = skill["charges"]
        if charges >= skill["max_charges"]:
            return charges
        recovery_at = skill["recovery_at"]
        if recovery_at is not None and recovery_at <= now:
            missing = skill["max_charges"] - charges
            period = self._scaled_duration_locked(skill["recovery_ms"])
            if period == 0:
                return skill["max_charges"]
            if recovery_at <= now:
                first = 1
                extra = (now - recovery_at) // period
                possible = first + extra
                return charges + min(missing, possible)
        return charges

    def _settle_skill_locked(self, skill: dict, now: int) -> int:
        max_charges = skill["max_charges"]
        if skill["charges"] >= max_charges:
            skill["recovery_at"] = None
            return 0

        recovery_at = skill["recovery_at"]
        if recovery_at is None or recovery_at > now:
            return 0

        missing = max_charges - skill["charges"]
        period = self._scaled_duration_locked(skill["recovery_ms"])
        if period == 0:
            recovered = missing
            skill["charges"] = max_charges
            skill["recovery_at"] = None
        else:
            extra = (now - recovery_at) // period
            recovered = min(missing, 1 + extra)
            skill["charges"] += recovered
            if skill["charges"] >= max_charges:
                skill["charges"] = max_charges
                skill["recovery_at"] = None
            else:
                skill["recovery_at"] = recovery_at + recovered * period

        self._stats["recovered"] += recovered
        return recovered

    def _advance_all_locked(self, now: int) -> dict[object, int]:
        recovered = {}
        for skill_id, skill in self._skills.items():
            count = self._settle_skill_locked(skill, now)
            if count:
                recovered[skill_id] = count
        return recovered

    def _use_locked(self, skill_id: object, now: int) -> dict:
        skill = self._require_skill(skill_id)
        self._check_clock_locked(now)
        self._last_now = now

        effective_now = self._effective_now_locked(now)
        self._advance_all_locked(effective_now)

        gcd_ready_at = self._gcd_ready_at
        if gcd_ready_at is not None and effective_now < gcd_ready_at:
            raise CooldownError(
                CD_GLOBAL_COOLDOWN,
                {"gcd_ready_at": gcd_ready_at, "now": effective_now},
            )

        if skill["charges"] <= 0:
            raise CooldownError(
                CD_NO_CHARGES,
                {"skill": skill_id, "recovery_at": skill["recovery_at"]},
            )

        skill["charges"] -= 1

        if skill["charges"] < skill["max_charges"]:
            if skill["recovery_at"] is None:
                period = self._scaled_duration_locked(skill["recovery_ms"])
                skill["recovery_at"] = effective_now + period
        else:
            skill["recovery_at"] = None

        self._gcd_ready_at = effective_now + skill["gcd_ms"]
        self._stats["uses"] += 1

        return {
            "skill": skill_id,
            "charges": skill["charges"],
            "recovery_at": skill["recovery_at"],
            "gcd_ready_at": self._gcd_ready_at,
        }

    def _parse_snapshot(self, state: object) -> dict:
        if not isinstance(state, dict):
            raise CooldownError(CD_SNAPSHOT_INVALID, "state must be a dict")

        if "skills" not in state or not isinstance(state["skills"], dict):
            raise CooldownError(CD_SNAPSHOT_INVALID, "skills must be a dict")

        haste_cap = self.haste_cap
        if "haste_cap" in state:
            haste_cap = _finite_non_negative_float(
                state["haste_cap"], "haste_cap"
            )

        gcd_ms = self.gcd_ms
        if "gcd_ms" in state:
            gcd_ms = _non_negative_int(state["gcd_ms"], "gcd_ms")

        haste = 0.0
        if "haste" in state:
            haste = _finite_non_negative_float(state["haste"], "haste")
        if haste > haste_cap:
            raise CooldownError(
                CD_SNAPSHOT_INVALID,
                {"field": "haste", "haste": haste, "cap": haste_cap},
            )

        paused_at = state.get("paused_at")
        if paused_at is not None and not _is_int(paused_at):
            raise CooldownError(CD_SNAPSHOT_INVALID, "paused_at must be int or None")

        last_now = state.get("last_now")
        if "last_now" not in state:
            last_now = paused_at
        if last_now is not None and not _is_int(last_now):
            raise CooldownError(CD_SNAPSHOT_INVALID, "last_now must be int or None")

        gcd_ready_at = state.get("gcd_ready_at")
        if gcd_ready_at is not None and not _is_int(gcd_ready_at):
            raise CooldownError(
                CD_SNAPSHOT_INVALID, "gcd_ready_at must be int or None"
            )

        stats_state = state.get("stats")
        if not isinstance(stats_state, dict):
            raise CooldownError(CD_SNAPSHOT_INVALID, "stats must be a dict")
        stats = {}
        for key in _STAT_KEYS:
            value = stats_state.get(key)
            if not _is_int(value) or value < 0:
                raise CooldownError(
                    CD_SNAPSHOT_INVALID, {"field": f"stats.{key}"}
                )
            stats[key] = value

        skills = {}
        for skill_id, raw_skill in state["skills"].items():
            if not isinstance(raw_skill, dict):
                raise CooldownError(
                    CD_SNAPSHOT_INVALID, {"skill": skill_id}
                )

            cooldown_ms = _positive_int(
                raw_skill.get("cooldown_ms"), "cooldown_ms"
            )
            recovery_ms = raw_skill.get("recovery_ms", cooldown_ms)
            recovery_ms = _positive_int(recovery_ms, "recovery_ms")
            skill_gcd = raw_skill.get("gcd_ms", gcd_ms)
            skill_gcd = _non_negative_int(skill_gcd, "gcd_ms")

            max_charges = raw_skill.get("max_charges")
            if max_charges is None:
                max_charges = raw_skill.get("charge_limit")
            if max_charges is None:
                max_charges = raw_skill.get("charges_max")
            max_charges = _positive_int(max_charges, "max_charges")

            charges = raw_skill.get("charges")
            if not _is_int(charges) or charges < 0 or charges > max_charges:
                raise CooldownError(
                    CD_SNAPSHOT_INVALID, {"skill": skill_id, "field": "charges"}
                )

            recovery_at = raw_skill.get("recovery_at", "missing")
            if recovery_at == "missing":
                remaining = raw_skill.get("remaining_ms")
                if remaining is None:
                    remaining = raw_skill.get("remaining")
                if _is_int(remaining) and last_now is not None:
                    recovery_at = last_now + max(0, remaining)
                elif charges < max_charges:
                    raise CooldownError(
                        CD_SNAPSHOT_INVALID,
                        {"skill": skill_id, "field": "recovery_at"},
                    )
                else:
                    recovery_at = None
            elif recovery_at is not None and not _is_int(recovery_at):
                raise CooldownError(
                    CD_SNAPSHOT_INVALID,
                    {"skill": skill_id, "field": "recovery_at"},
                )

            if charges >= max_charges:
                recovery_at = None
            elif recovery_at is None:
                raise CooldownError(
                    CD_SNAPSHOT_INVALID,
                    {"skill": skill_id, "field": "recovery_at"},
                )

            skills[skill_id] = {
                "cooldown_ms": cooldown_ms,
                "max_charges": max_charges,
                "charges": charges,
                "recovery_ms": recovery_ms,
                "gcd_ms": skill_gcd,
                "recovery_at": recovery_at,
            }

        return {
            "gcd_ms": gcd_ms,
            "haste_cap": haste_cap,
            "haste": haste,
            "paused_at": paused_at,
            "last_now": last_now,
            "gcd_ready_at": gcd_ready_at,
            "stats": stats,
            "skills": skills,
        }
