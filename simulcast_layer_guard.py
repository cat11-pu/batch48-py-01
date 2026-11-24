"""Simulcast layer subscription switching guard."""

from __future__ import annotations

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

    def _now_ms(self) -> int:
        if self._clock is not None:
            return int(self._clock())
        return int(time.monotonic() * 1000)

    def publish_layer(self, layer: str, *, now_ms: int | None = None) -> dict:
        raise NotImplementedError

    def unpublish_layer(self, layer: str, *, now_ms: int | None = None) -> dict:
        raise NotImplementedError

    def switch(self, target: str, *, now_ms: int | None = None) -> dict:
        raise NotImplementedError

    def on_frame(self, layer: str, *, keyframe: bool = False,
                 now_ms: int | None = None) -> dict:
        raise NotImplementedError

    def active_layer(self) -> str | None:
        raise NotImplementedError

    def pending(self, *, now_ms: int | None = None) -> dict | None:
        raise NotImplementedError

    def pause(self, *, now_ms: int | None = None) -> dict:
        raise NotImplementedError

    def resume(self, *, now_ms: int | None = None) -> dict:
        raise NotImplementedError

    def report_quality(self, score: int, *, now_ms: int | None = None) -> dict:
        raise NotImplementedError

    def layer_order(self) -> list[str]:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
