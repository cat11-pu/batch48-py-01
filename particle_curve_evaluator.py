"""Particle module curve evaluation and deterministic emission budgets."""

from __future__ import annotations

import threading


class ParticleError(ValueError):
    """Raised for rejected curves, budgets and snapshots."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class ParticleCurveEvaluator:
    """Evaluate particle curves and accumulate per-emitter emission budgets."""

    MAX_KEYS = 256
    MAX_MODULES = 64
    MAX_REMAINDER = 100.0

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def set_curve(self, module_id: str, keys: list) -> None:
        raise NotImplementedError

    def remove_curve(self, module_id: str) -> bool:
        raise NotImplementedError

    def curve_info(self, module_id: str) -> dict:
        raise NotImplementedError

    def list_modules(self) -> list:
        raise NotImplementedError

    def eval(self, module_id: str, t) -> float:
        raise NotImplementedError

    def emission_budget(self, emitter: str, rate, dt) -> int:
        raise NotImplementedError

    def budget_state(self, emitter: str) -> dict:
        raise NotImplementedError

    def reset_budget(self, emitter: str) -> None:
        raise NotImplementedError

    def random_stream(self, seed: int, index: int) -> float:
        raise NotImplementedError

    def remap_time(self, t, start, end, mode: str) -> float:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
