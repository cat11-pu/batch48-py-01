"""Login risk scoring with weighted signals, thresholds and score decay."""

from __future__ import annotations

import copy
import math
import threading
from collections.abc import Mapping
from numbers import Real


class LoginRiskError(ValueError):
    """Raised when a risk evaluation operation is refused."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


SIGNAL_KEYS = (
    "new_device",
    "geo_velocity",
    "failures",
    "odd_hour",
    "source_reputation",
)

UNKNOWN_SIGNAL = "LOGIN_RISK_UNKNOWN_SIGNAL"
INVALID = "LOGIN_RISK_INVALID"
SNAPSHOT_INVALID = "LOGIN_RISK_SNAPSHOT_INVALID"
LOGIN_RISK_UNKNOWN_SIGNAL = UNKNOWN_SIGNAL
LOGIN_RISK_INVALID = INVALID
LOGIN_RISK_SNAPSHOT_INVALID = SNAPSHOT_INVALID

DEFAULT_WEIGHTS = {
    "new_device": 30.0,
    "geo_velocity": 25.0,
    "failures": 20.0,
    "odd_hour": 10.0,
    "source_reputation": 15.0,
}
DEFAULT_THRESHOLDS = {"allow": 30.0, "step_up": 70.0}

FAILURE_HISTORY_DELTA = 10.0
SUCCESS_HISTORY_DELTA = -5.0


def _real_number(value, *, non_negative: bool = False, positive: bool = False):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise LoginRiskError(INVALID, {"value": value})
    number = float(value)
    if not math.isfinite(number):
        raise LoginRiskError(INVALID, {"value": value})
    if positive and number <= 0.0:
        raise LoginRiskError(INVALID, {"value": value})
    if non_negative and number < 0.0:
        raise LoginRiskError(INVALID, {"value": value})
    return value


def _snapshot_number(value, *, non_negative: bool = False,
                     positive: bool = False) -> float:
    number = _real_number(
        value, non_negative=non_negative, positive=positive
    )
    return float(number)


class LoginRiskEvaluator:
    """Score logins from weighted signals and map scores onto decisions."""

    def __init__(self, *, weights=None, thresholds=None, half_life: int = 3600,
                 window: int = 600, signal_cap: int = 10, clock=None) -> None:
        self.half_life = half_life
        self.window = window
        self.signal_cap = signal_cap
        self._clock = clock
        self._lock = threading.RLock()
        self.weights = self._init_weights(weights)
        self.thresholds = self._init_thresholds(thresholds)
        self.half_life = _real_number(half_life, positive=True)
        self.window = _real_number(window, non_negative=True)
        self.signal_cap = _real_number(signal_cap, positive=True)

        self._signals: dict[str, dict[str, dict[str, object]]] = {}
        self._history: dict[str, dict[str, object]] = {}
        self._lists: dict[str, str] = {}
        self._stats = {
            "signals": 0,
            "scored": 0,
            "decided": 0,
            "outcomes": 0,
            "rejected": 0,
        }

    @staticmethod
    def _init_weights(weights) -> dict[str, float]:
        source = DEFAULT_WEIGHTS if weights is None else weights
        if not isinstance(source, Mapping):
            raise LoginRiskError(INVALID, {"field": "weights"})
        result = {}
        for key in SIGNAL_KEYS:
            value = source.get(key, DEFAULT_WEIGHTS[key])
            result[key] = _snapshot_number(value, non_negative=True)
        return result

    @staticmethod
    def _init_thresholds(thresholds) -> dict[str, float]:
        source = DEFAULT_THRESHOLDS if thresholds is None else thresholds
        if not isinstance(source, Mapping):
            raise LoginRiskError(INVALID, {"field": "thresholds"})
        result = {
            "allow": _snapshot_number(
                source.get("allow", DEFAULT_THRESHOLDS["allow"]),
                non_negative=True,
            ),
            "step_up": _snapshot_number(
                source.get("step_up", DEFAULT_THRESHOLDS["step_up"]),
                non_negative=True,
            ),
        }
        if result["allow"] > result["step_up"]:
            raise LoginRiskError(INVALID, {"field": "thresholds"})
        return result

    def _now(self, now) -> Real:
        if now is None:
            now = self._clock() if callable(self._clock) else 0
        return _real_number(now)

    @staticmethod
    def _validate_account(account) -> None:
        if not isinstance(account, str) or not account:
            raise LoginRiskError(INVALID, {"field": "account",
                                          "value": account})

    @staticmethod
    def _validate_signal_value(value) -> float:
        return _snapshot_number(value, non_negative=True)

    def _factor(self, elapsed: Real) -> float:
        if self.half_life <= 0:
            return 1.0
        return 2.0 ** (-max(0.0, float(elapsed)) / float(self.half_life))

    def _trim_all_locked(self, now: Real) -> None:
        for account in list(self._signals):
            table = self._signals[account]
            for key in list(table):
                recorded_at = table[key].get("at", 0)
                age = float(now) - float(recorded_at)
                if age > float(self.window):
                    del table[key]
            if not table:
                del self._signals[account]

    def _history_score_locked(self, account: str, now: Real) -> float:
        item = self._history.get(account)
        if not isinstance(item, Mapping):
            return 0.0
        base = max(0.0, float(item.get("score", 0.0)))
        recorded_at = float(item.get("at", 0))
        value = base * self._factor(float(now) - recorded_at)
        return round(max(0.0, value), 6)

    def _evaluate_locked(self, account: str, signals, now: Real) -> dict:
        self._validate_account(account)
        self._trim_all_locked(now)

        values: dict[str, float] = {}
        stored = self._signals.get(account, {})
        for key in SIGNAL_KEYS:
            record = stored.get(key)
            if isinstance(record, Mapping):
                values[key] = float(record["value"])

        if signals is not None:
            if not isinstance(signals, Mapping):
                raise LoginRiskError(INVALID, {"field": "signals"})
            for key, value in signals.items():
                if key not in SIGNAL_KEYS:
                    raise LoginRiskError(UNKNOWN_SIGNAL, {"key": key})
                values[key] = self._validate_signal_value(value)

        contributions = {}
        for key in SIGNAL_KEYS:
            if key not in values:
                continue
            normalized = min(values[key], float(self.signal_cap))
            contribution = (
                float(self.weights[key])
                * normalized
                / float(self.signal_cap)
            )
            if contribution > 0.0:
                contributions[key] = contribution

        history = self._history_score_locked(account, now)
        score = sum(contributions.values(), 0.0) + history
        matched = sorted(
            contributions,
            key=lambda key: -contributions[key],
        )
        return {
            "contributions": contributions,
            "matched": matched,
            "history": history,
            "score": float(score),
        }

    def _decision_locked(self, account: str, evaluation: dict) -> tuple[str, str]:
        list_kind = self._lists.get(account)
        if list_kind == "deny":
            return "deny", "denylist"
        if list_kind == "allow":
            return "allow", "allowlist"

        score = evaluation["score"]
        if score < float(self.thresholds["allow"]):
            return "allow", "below_threshold"
        if score < float(self.thresholds["step_up"]):
            return "step_up", "step_up_band"
        return "deny", "above_threshold"

    def register_signal(self, account: str, key: str, value, *,
                        now=None) -> dict:
        with self._lock:
            try:
                current_now = self._now(now)
                self._validate_account(account)
                if key not in SIGNAL_KEYS:
                    raise LoginRiskError(UNKNOWN_SIGNAL, {"key": key})
                clean_value = self._validate_signal_value(value)

                self._trim_all_locked(current_now)
                table = self._signals.setdefault(account, {})
                table[key] = {"value": clean_value, "at": current_now}
                self._stats["signals"] += 1
                return {
                    "account": account,
                    "key": key,
                    "value": clean_value,
                    "stored": True,
                }
            except LoginRiskError:
                self._stats["rejected"] += 1
                raise

    def score_login(self, account: str, *, signals=None, now=None) -> dict:
        with self._lock:
            try:
                current_now = self._now(now)
                evaluation = self._evaluate_locked(
                    account, signals, current_now
                )
                self._stats["scored"] += 1
                return {
                    "account": account,
                    "score": evaluation["score"],
                    "history": evaluation["history"],
                    "contributions": evaluation["contributions"],
                    "matched": evaluation["matched"],
                }
            except LoginRiskError:
                self._stats["rejected"] += 1
                raise

    def set_list(self, account: str, kind) -> dict:
        with self._lock:
            try:
                self._validate_account(account)
                aliases = {
                    "allow": "allow",
                    "allowlist": "allow",
                    "whitelist": "allow",
                    "deny": "deny",
                    "denylist": "deny",
                    "blacklist": "deny",
                }
                if kind is None or kind == "none":
                    normalized = None
                else:
                    normalized = aliases.get(kind)
                    if normalized is None:
                        raise LoginRiskError(INVALID, {"field": "list",
                                                      "value": kind})

                if normalized is None:
                    self._lists.pop(account, None)
                else:
                    self._lists[account] = normalized
                return {"account": account, "list": normalized}
            except LoginRiskError:
                self._stats["rejected"] += 1
                raise

    def decide(self, account: str, *, signals=None, now=None) -> dict:
        with self._lock:
            try:
                current_now = self._now(now)
                evaluation = self._evaluate_locked(
                    account, signals, current_now
                )
                decision, reason = self._decision_locked(account, evaluation)
                self._stats["decided"] += 1
                return {
                    "account": account,
                    "score": evaluation["score"],
                    "decision": decision,
                    "reason": reason,
                    "matched": evaluation["matched"],
                }
            except LoginRiskError:
                self._stats["rejected"] += 1
                raise

    def explain(self, account: str, *, signals=None, now=None) -> dict:
        with self._lock:
            try:
                current_now = self._now(now)
                evaluation = self._evaluate_locked(
                    account, signals, current_now
                )
                decision, reason = self._decision_locked(account, evaluation)
                return {
                    "account": account,
                    "list": self._lists.get(account),
                    "contributions": evaluation["contributions"],
                    "matched": evaluation["matched"],
                    "history": evaluation["history"],
                    "score": evaluation["score"],
                    "decision": decision,
                    "reason": reason,
                }
            except LoginRiskError:
                self._stats["rejected"] += 1
                raise

    def record_outcome(self, account: str, outcome: str, *,
                       now=None) -> dict:
        with self._lock:
            try:
                current_now = self._now(now)
                self._validate_account(account)
                aliases = {
                    "failure": "failure",
                    "fail": "failure",
                    "failed": "failure",
                    "success": "success",
                    "succeeded": "success",
                    "successful": "success",
                }
                normalized_outcome = aliases.get(outcome)
                if normalized_outcome is None:
                    raise LoginRiskError(INVALID, {"field": "outcome",
                                                  "value": outcome})

                current = self._history_score_locked(account, current_now)
                delta = (
                    FAILURE_HISTORY_DELTA
                    if normalized_outcome == "failure"
                    else SUCCESS_HISTORY_DELTA
                )
                history_score = round(max(0.0, current + delta), 6)
                self._history[account] = {
                    "score": history_score,
                    "at": current_now,
                }
                self._stats["outcomes"] += 1
                return {
                    "account": account,
                    "outcome": normalized_outcome,
                    "history": history_score,
                }
            except LoginRiskError:
                self._stats["rejected"] += 1
                raise

    def history_score(self, account: str, *, now=None) -> float:
        with self._lock:
            try:
                current_now = self._now(now)
                self._validate_account(account)
                return self._history_score_locked(account, current_now)
            except LoginRiskError:
                self._stats["rejected"] += 1
                raise

    def decay(self, account: str, *, now=None) -> dict:
        with self._lock:
            try:
                current_now = self._now(now)
                self._validate_account(account)
                item = self._history.get(account)
                if not isinstance(item, Mapping):
                    return {"account": account, "before": 0.0, "after": 0.0}

                before = float(item.get("score", 0.0))
                recorded_at = float(item.get("at", 0))
                after = round(
                    max(0.0, before * self._factor(current_now - recorded_at)),
                    6,
                )
                item["score"] = after
                if float(current_now) >= recorded_at:
                    item["at"] = current_now
                return {
                    "account": account,
                    "before": before,
                    "after": float(after),
                }
            except LoginRiskError:
                self._stats["rejected"] += 1
                raise

    def stats(self) -> dict:
        with self._lock:
            return {key: int(self._stats[key]) for key in (
                "signals",
                "scored",
                "decided",
                "outcomes",
                "rejected",
            )}

    def snapshot(self) -> dict:
        with self._lock:
            state = {
                "version": 1,
                "weights": dict(self.weights),
                "thresholds": dict(self.thresholds),
                "half_life": self.half_life,
                "window": self.window,
                "signal_cap": self.signal_cap,
                "signals": self._signals,
                "history": self._history,
                "lists": self._lists,
                "stats": dict(self._stats),
            }
            return copy.deepcopy(state)

    @classmethod
    def _parse_snapshot(cls, state) -> dict:
        try:
            safe_state = copy.deepcopy(state)
            if not isinstance(safe_state, Mapping):
                raise LoginRiskError(SNAPSHOT_INVALID, "snapshot is not an object")

            required = (
                "version",
                "weights",
                "thresholds",
                "half_life",
                "window",
                "signals",
                "history",
                "lists",
                "stats",
            )
            for key in required:
                if key not in safe_state:
                    raise LoginRiskError(SNAPSHOT_INVALID,
                                         {"missing": key})

            version = safe_state["version"]
            if isinstance(version, bool) or not isinstance(version, int):
                raise LoginRiskError(SNAPSHOT_INVALID, {"field": "version"})
            if version != 1:
                raise LoginRiskError(SNAPSHOT_INVALID, {"field": "version"})

            raw_weights = safe_state["weights"]
            if not isinstance(raw_weights, Mapping):
                raise LoginRiskError(SNAPSHOT_INVALID, {"field": "weights"})
            weights = {}
            for key in SIGNAL_KEYS:
                if key not in raw_weights:
                    raise LoginRiskError(SNAPSHOT_INVALID,
                                         {"field": "weights", "key": key})
                weights[key] = _snapshot_number(
                    raw_weights[key], non_negative=True
                )

            raw_thresholds = safe_state["thresholds"]
            if not isinstance(raw_thresholds, Mapping):
                raise LoginRiskError(SNAPSHOT_INVALID,
                                     {"field": "thresholds"})
            thresholds = {
                "allow": _snapshot_number(
                    raw_thresholds["allow"], non_negative=True
                ),
                "step_up": _snapshot_number(
                    raw_thresholds["step_up"], non_negative=True
                ),
            }
            if thresholds["allow"] > thresholds["step_up"]:
                raise LoginRiskError(SNAPSHOT_INVALID,
                                     {"field": "thresholds"})

            half_life = safe_state["half_life"]
            window = safe_state["window"]
            signal_cap = safe_state.get("signal_cap", 10)
            half_life = _real_number(half_life, positive=True)
            window = _real_number(window, non_negative=True)
            signal_cap = _real_number(signal_cap, positive=True)

            signals = cls._parse_signal_snapshot(safe_state["signals"])
            history = cls._parse_history_snapshot(safe_state["history"])
            lists = cls._parse_list_snapshot(safe_state["lists"])
            stats = cls._parse_stats_snapshot(safe_state["stats"])

            return {
                "weights": weights,
                "thresholds": thresholds,
                "half_life": half_life,
                "window": window,
                "signal_cap": signal_cap,
                "signals": signals,
                "history": history,
                "lists": lists,
                "stats": stats,
            }
        except LoginRiskError as exc:
            if exc.code == SNAPSHOT_INVALID:
                raise
            raise LoginRiskError(SNAPSHOT_INVALID, exc.detail) from exc
        except Exception as exc:
            raise LoginRiskError(SNAPSHOT_INVALID, str(exc)) from exc

    @classmethod
    def _parse_signal_snapshot(cls, raw_signals) -> dict:
        if not isinstance(raw_signals, Mapping):
            raise LoginRiskError(SNAPSHOT_INVALID, {"field": "signals"})

        signals = {}
        for account, raw_table in raw_signals.items():
            LoginRiskEvaluator._validate_account(account)
            if not isinstance(raw_table, Mapping):
                raise LoginRiskError(SNAPSHOT_INVALID,
                                     {"account": account})
            table = {}
            for key, raw_record in raw_table.items():
                if key not in SIGNAL_KEYS:
                    raise LoginRiskError(SNAPSHOT_INVALID, {"key": key})
                if isinstance(raw_record, Mapping):
                    if "value" not in raw_record:
                        raise LoginRiskError(SNAPSHOT_INVALID,
                                             {"key": key})
                    value = _snapshot_number(
                        raw_record["value"], non_negative=True
                    )
                    recorded_at = 0
                    for time_key in ("at", "ts", "timestamp", "time", "now"):
                        if time_key in raw_record:
                            recorded_at = _real_number(raw_record[time_key])
                            break
                elif isinstance(raw_record, (list, tuple)) and raw_record:
                    value = _snapshot_number(
                        raw_record[0], non_negative=True
                    )
                    recorded_at = (
                        _real_number(raw_record[1])
                        if len(raw_record) > 1
                        else 0
                    )
                else:
                    value = _snapshot_number(
                        raw_record, non_negative=True
                    )
                    recorded_at = 0
                table[key] = {"value": value, "at": recorded_at}
            signals[account] = table
        return signals

    @classmethod
    def _parse_history_snapshot(cls, raw_history) -> dict:
        if not isinstance(raw_history, Mapping):
            raise LoginRiskError(SNAPSHOT_INVALID, {"field": "history"})

        history = {}
        for account, raw_item in raw_history.items():
            LoginRiskEvaluator._validate_account(account)
            if isinstance(raw_item, Mapping):
                score = None
                for score_key in ("score", "history", "value"):
                    if score_key in raw_item:
                        score = raw_item[score_key]
                        break
                if score is None:
                    raise LoginRiskError(SNAPSHOT_INVALID,
                                         {"account": account})
                recorded_at = 0
                for time_key in ("at", "updated_at", "ts", "timestamp",
                                 "time", "now"):
                    if time_key in raw_item:
                        recorded_at = _real_number(raw_item[time_key])
                        break
            else:
                score = raw_item
                recorded_at = 0

            clean_score = _snapshot_number(score, non_negative=True)
            history[account] = {"score": clean_score, "at": recorded_at}
        return history

    @classmethod
    def _parse_list_snapshot(cls, raw_lists) -> dict:
        if not isinstance(raw_lists, Mapping):
            raise LoginRiskError(SNAPSHOT_INVALID, {"field": "lists"})

        aliases = {
            "allow": "allow",
            "allowlist": "allow",
            "whitelist": "allow",
            "deny": "deny",
            "denylist": "deny",
            "blacklist": "deny",
        }
        lists = {}
        for account, kind in raw_lists.items():
            LoginRiskEvaluator._validate_account(account)
            if kind is None:
                continue
            normalized = aliases.get(kind)
            if normalized is None:
                raise LoginRiskError(SNAPSHOT_INVALID,
                                     {"account": account, "list": kind})
            lists[account] = normalized
        return lists

    @classmethod
    def _parse_stats_snapshot(cls, raw_stats) -> dict:
        if not isinstance(raw_stats, Mapping):
            raise LoginRiskError(SNAPSHOT_INVALID, {"field": "stats"})

        stats = {}
        for key in ("signals", "scored", "decided", "outcomes", "rejected"):
            if key not in raw_stats:
                raise LoginRiskError(SNAPSHOT_INVALID, {"missing": key})
            value = raw_stats[key]
            if isinstance(value, bool) or not isinstance(value, int):
                raise LoginRiskError(SNAPSHOT_INVALID, {"field": key})
            if value < 0:
                raise LoginRiskError(SNAPSHOT_INVALID, {"field": key})
            stats[key] = value
        return stats

    def restore(self, state: dict) -> None:
        candidate = self._parse_snapshot(state)
        if "signal_cap" not in state:
            candidate["signal_cap"] = self.signal_cap
        with self._lock:
            self.weights = candidate["weights"]
            self.thresholds = candidate["thresholds"]
            self.half_life = candidate["half_life"]
            self.window = candidate["window"]
            self.signal_cap = candidate["signal_cap"]
            self._signals = candidate["signals"]
            self._history = candidate["history"]
            self._lists = candidate["lists"]
            self._stats = candidate["stats"]
