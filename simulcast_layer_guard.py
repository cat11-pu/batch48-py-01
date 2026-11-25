"""Simulcast layer subscription switching guard."""

from __future__ import annotations

import copy
import math
import threading
import time


class SimulcastError(ValueError):
    """Rejection raised by the layer guard."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


DEFAULT_LAYERS = ("low", "mid", "high")


class SimulcastLayerGuard:
    """Switch between simulcast layers while waiting for a keyframe."""

    def __init__(self, *, layers=DEFAULT_LAYERS, initial: str = "low",
                 switch_timeout_ms: int = 2000,
                 upgrade_confirm_samples: int = 3,
                 quality_thresholds=(40, 75), clock=None) -> None:
        self.layers = tuple(layers)
        self.initial = initial
        self.switch_timeout_ms = switch_timeout_ms
        self.upgrade_confirm_samples = upgrade_confirm_samples
        self.quality_thresholds = tuple(quality_thresholds)
        self._clock = clock
        self._lock = threading.RLock()
        self._published: set[str] = set()
        self._active: str | None = None
        self._pending: dict | None = None
        self._paused = False
        self._requires_keyframe = False
        self._quality_candidate: str | None = None
        self._quality_confirmations = 0
        self._stats = self._fresh_stats()

    @staticmethod
    def _fresh_stats() -> dict:
        return {"frames_forwarded": 0, "fallbacks": 0}

    def _now_ms(self) -> int:
        if self._clock is not None:
            return int(self._clock())
        return int(time.monotonic() * 1000)

    def _resolve_now_ms(self, now_ms: int | None) -> int:
        return self._now_ms() if now_ms is None else now_ms

    def _require_known_layer_locked(self, layer: object) -> None:
        if not isinstance(layer, str) or layer not in self.layers:
            raise SimulcastError(
                "SIMULCAST_UNKNOWN_LAYER",
                {"layer": layer, "layers": list(self.layers)},
            )

    def _expire_pending_locked(self, now_ms: int) -> None:
        pending = self._pending
        if pending is None:
            return
        if now_ms - pending["requested_ms"] >= self.switch_timeout_ms:
            self._pending = None
            self._stats["fallbacks"] += 1

    def _switch_locked(self, target: str, now_ms: int) -> dict:
        self._expire_pending_locked(now_ms)
        self._require_known_layer_locked(target)

        if target == self._active:
            self._pending = None
            return {"status": "active", "target": target, "active": self._active}

        if target not in self._published:
            if self._pending is not None and self._pending["target"] == target:
                self._pending = None
            return {
                "status": "unavailable",
                "target": target,
                "active": self._active,
            }

        if self._pending is not None and self._pending["target"] == target:
            return {
                "status": "pending",
                "target": target,
                "active": self._active,
            }

        self._pending = {
            "target": target,
            "from": self._active,
            "requested_ms": now_ms,
        }
        return {"status": "pending", "target": target, "active": self._active}

    def _desired_layer_locked(self, score: object) -> str:
        index = 0
        for threshold in self.quality_thresholds:
            if score >= threshold:
                index += 1
        index = max(0, min(index, len(self.layers) - 1))
        return self.layers[index]

    @staticmethod
    def _is_number(value: object) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )

    def _validate_snapshot_locked(self, state: object) -> dict:
        if not isinstance(state, dict):
            raise SimulcastError(
                "SIMULCAST_SNAPSHOT_INVALID",
                {"reason": "snapshot must be a dict"},
            )

        for key in ("published", "active", "paused", "requires_keyframe",
                    "pending", "stats"):
            if key not in state:
                raise SimulcastError(
                    "SIMULCAST_SNAPSHOT_INVALID",
                    {"reason": f"missing field: {key}"},
                )

        published_value = state["published"]
        if not isinstance(published_value, (list, tuple)):
            raise SimulcastError(
                "SIMULCAST_SNAPSHOT_INVALID",
                {"reason": "published must be a list"},
            )
        published = []
        for layer in published_value:
            self._require_known_layer_locked(layer)
            if layer in published:
                raise SimulcastError(
                    "SIMULCAST_SNAPSHOT_INVALID",
                    {"reason": f"duplicate published layer: {layer}"},
                )
            published.append(layer)

        active = state["active"]
        if active is not None:
            self._require_known_layer_locked(active)

        paused = state["paused"]
        requires_keyframe = state["requires_keyframe"]
        if not isinstance(paused, bool) or not isinstance(requires_keyframe, bool):
            raise SimulcastError(
                "SIMULCAST_SNAPSHOT_INVALID",
                {"reason": "pause state must be boolean"},
            )

        pending = state["pending"]
        if pending is not None:
            if not isinstance(pending, dict):
                raise SimulcastError(
                    "SIMULCAST_SNAPSHOT_INVALID",
                    {"reason": "pending must be a dict or None"},
                )
            for key in ("target", "from", "requested_ms"):
                if key not in pending:
                    raise SimulcastError(
                        "SIMULCAST_SNAPSHOT_INVALID",
                        {"reason": f"pending missing field: {key}"},
                    )
            self._require_known_layer_locked(pending["target"])
            source = pending["from"]
            if source is not None:
                self._require_known_layer_locked(source)
            requested_ms = pending["requested_ms"]
            if not self._is_number(requested_ms):
                raise SimulcastError(
                    "SIMULCAST_SNAPSHOT_INVALID",
                    {"reason": "pending requested_ms must be numeric"},
                )
            if pending["target"] == source:
                raise SimulcastError(
                    "SIMULCAST_SNAPSHOT_INVALID",
                    {"reason": "pending target and source must differ"},
                )
            if source != active:
                raise SimulcastError(
                    "SIMULCAST_SNAPSHOT_INVALID",
                    {"reason": "pending source must equal active layer"},
                )

        stats = state["stats"]
        if not isinstance(stats, dict):
            raise SimulcastError(
                "SIMULCAST_SNAPSHOT_INVALID",
                {"reason": "stats must be a dict"},
            )
        for key, value in stats.items():
            if not isinstance(key, str) or not self._is_number(value) or value < 0:
                raise SimulcastError(
                    "SIMULCAST_SNAPSHOT_INVALID",
                    {"reason": f"invalid stat: {key}"},
                )

        quality = state.get("quality")
        quality_candidate = None
        quality_confirmations = 0
        if quality is not None:
            if not isinstance(quality, dict):
                raise SimulcastError(
                    "SIMULCAST_SNAPSHOT_INVALID",
                    {"reason": "quality must be a dict or None"},
                )
            quality_candidate = quality.get("candidate")
            if quality_candidate is not None:
                self._require_known_layer_locked(quality_candidate)
            quality_confirmations = quality.get("confirmations", 0)
            if (not isinstance(quality_confirmations, int)
                    or isinstance(quality_confirmations, bool)
                    or quality_confirmations < 0):
                raise SimulcastError(
                    "SIMULCAST_SNAPSHOT_INVALID",
                    {"reason": "quality confirmations must be a non-negative int"},
                )

        return {
            "published": published,
            "active": active,
            "paused": paused,
            "requires_keyframe": requires_keyframe,
            "pending": copy.deepcopy(pending),
            "stats": copy.deepcopy(stats),
            "quality_candidate": quality_candidate,
            "quality_confirmations": quality_confirmations,
        }

    def publish_layer(self, layer: str, *, now_ms: int | None = None) -> dict:
        with self._lock:
            self._resolve_now_ms(now_ms)
            self._require_known_layer_locked(layer)
            self._published.add(layer)
            if self._active is None and layer == self.initial:
                self._active = layer
            return {
                "layer": layer,
                "published": True,
                "active": self._active,
            }

    def unpublish_layer(self, layer: str, *, now_ms: int | None = None) -> dict:
        with self._lock:
            self._resolve_now_ms(now_ms)
            self._require_known_layer_locked(layer)
            self._published.discard(layer)
            return {
                "layer": layer,
                "published": False,
                "active": self._active,
            }

    def switch(self, target: str, *, now_ms: int | None = None) -> dict:
        with self._lock:
            resolved_now = self._resolve_now_ms(now_ms)
            return self._switch_locked(target, resolved_now)

    def on_frame(self, layer: str, *, keyframe: bool = False,
                 now_ms: int | None = None) -> dict:
        with self._lock:
            resolved_now = self._resolve_now_ms(now_ms)
            self._expire_pending_locked(resolved_now)
            self._require_known_layer_locked(layer)
            if layer not in self._published:
                raise SimulcastError(
                    "SIMULCAST_NOT_PUBLISHED",
                    {"layer": layer},
                )

            pending = self._pending
            if pending is not None and layer == pending["target"]:
                if not keyframe:
                    return {
                        "layer": layer,
                        "forward": False,
                        "switched": False,
                        "reason": "waiting_keyframe",
                    }

                self._active = layer
                self._pending = None
                if self._paused:
                    self._requires_keyframe = True
                    return {
                        "layer": layer,
                        "forward": False,
                        "switched": True,
                        "reason": "paused",
                    }

                self._requires_keyframe = False
                self._stats["frames_forwarded"] += 1
                return {
                    "layer": layer,
                    "forward": True,
                    "switched": True,
                    "reason": "keyframe",
                }

            if layer != self._active:
                return {
                    "layer": layer,
                    "forward": False,
                    "switched": False,
                    "reason": "inactive",
                }

            forward = False
            reason: str | None = None
            if self._paused:
                reason = "paused"
            elif self._requires_keyframe:
                if keyframe:
                    self._requires_keyframe = False
                    forward = True
                    reason = "resumed"
                else:
                    reason = "waiting_keyframe"
            else:
                forward = True

            if forward:
                self._stats["frames_forwarded"] += 1

            return {
                "layer": layer,
                "forward": forward,
                "switched": False,
                "reason": reason,
            }

    def active_layer(self) -> str | None:
        with self._lock:
            return self._active

    def pending(self, *, now_ms: int | None = None) -> dict | None:
        with self._lock:
            resolved_now = self._resolve_now_ms(now_ms)
            self._expire_pending_locked(resolved_now)
            return copy.deepcopy(self._pending)

    def pause(self, *, now_ms: int | None = None) -> dict:
        with self._lock:
            self._resolve_now_ms(now_ms)
            self._paused = True
            return {"paused": True, "layer": self._active}

    def resume(self, *, now_ms: int | None = None) -> dict:
        with self._lock:
            self._resolve_now_ms(now_ms)
            self._paused = False
            self._requires_keyframe = True
            return {
                "paused": False,
                "requires_keyframe": True,
                "layer": self._active,
            }

    def report_quality(self, score: int, *, now_ms: int | None = None) -> dict:
        with self._lock:
            resolved_now = self._resolve_now_ms(now_ms)
            desired = self._desired_layer_locked(score)
            if self._active is not None:
                current = self._active
            elif self.initial in self.layers:
                current = self.initial
            else:
                current = self.layers[0]
            current_index = self.layers.index(current)
            desired_index = self.layers.index(desired)

            if desired_index == current_index:
                self._quality_candidate = None
                self._quality_confirmations = 0
                return {
                    "desired": desired,
                    "requested": False,
                    "confirmations": 0,
                    "direction": "hold",
                }

            if desired_index < current_index:
                self._quality_candidate = None
                self._quality_confirmations = 0
                result = self._switch_locked(desired, resolved_now)
                return {
                    "desired": desired,
                    "requested": result["status"] == "pending",
                    "confirmations": 0,
                    "direction": "down",
                }

            if self._quality_candidate != desired:
                self._quality_candidate = desired
                self._quality_confirmations = 1
            else:
                self._quality_confirmations += 1

            confirmations = self._quality_confirmations
            requested = False
            if confirmations >= self.upgrade_confirm_samples:
                result = self._switch_locked(desired, resolved_now)
                requested = result["status"] == "pending"
                self._quality_candidate = None
                self._quality_confirmations = 0

            return {
                "desired": desired,
                "requested": requested,
                "confirmations": confirmations,
                "direction": "up",
            }

    def layer_order(self) -> list[str]:
        with self._lock:
            return list(self.layers)

    def snapshot(self) -> dict:
        with self._lock:
            state = {
                "layers": list(self.layers),
                "initial": self.initial,
                "published": [
                    layer for layer in self.layers if layer in self._published
                ],
                "active": self._active,
                "paused": self._paused,
                "requires_keyframe": self._requires_keyframe,
                "pending": copy.deepcopy(self._pending),
                "quality": {
                    "candidate": self._quality_candidate,
                    "confirmations": self._quality_confirmations,
                },
                "stats": copy.deepcopy(self._stats),
            }
            return copy.deepcopy(state)

    def restore(self, state: dict) -> None:
        with self._lock:
            try:
                copied_state = copy.deepcopy(state)
                normalized = self._validate_snapshot_locked(copied_state)
            except SimulcastError as exc:
                if exc.code == "SIMULCAST_SNAPSHOT_INVALID":
                    raise
                raise SimulcastError(
                    "SIMULCAST_SNAPSHOT_INVALID",
                    {"reason": exc.code, "detail": exc.detail},
                ) from exc
            except Exception as exc:
                raise SimulcastError(
                    "SIMULCAST_SNAPSHOT_INVALID",
                    {"reason": "invalid snapshot structure"},
                ) from exc

            self._published = set(normalized["published"])
            self._active = normalized["active"]
            self._paused = normalized["paused"]
            self._requires_keyframe = normalized["requires_keyframe"]
            self._pending = normalized["pending"]
            self._stats = normalized["stats"]
            self._quality_candidate = normalized["quality_candidate"]
            self._quality_confirmations = normalized["quality_confirmations"]

    def stats(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            for key in self._stats:
                self._stats[key] = 0
