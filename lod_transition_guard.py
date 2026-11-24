"""LOD selection with hysteresis, cross-fade and shadow offset."""

from __future__ import annotations


class LodError(ValueError):
    """Raised when an LOD decision or transition state is invalid."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class LodTransitionGuard:
    """Pick LOD levels, cross-fade between them and track transitions."""

    def __init__(self, *, levels: int = 4, thresholds=(0.5, 0.2, 0.05),
                 hysteresis: float = 0.1, transition_frames: int = 4,
                 transition_budget: int = 2, shadow_offset: int = 1) -> None:
        self.levels = levels
        self.thresholds = tuple(thresholds)
        self.hysteresis = hysteresis
        self.transition_frames = transition_frames
        self.transition_budget = transition_budget
        self.shadow_offset = shadow_offset

    def select_lod(self, entity: str, screen_ratio: float) -> int:
        raise NotImplementedError

    def advance(self, entity: str, frames: int = 1) -> dict:
        raise NotImplementedError

    def draw_state(self, entity: str) -> list[dict]:
        raise NotImplementedError

    def transition_of(self, entity: str) -> dict:
        raise NotImplementedError

    def force_lod(self, entity: str, level: int) -> int:
        raise NotImplementedError

    def clear_force(self, entity: str) -> None:
        raise NotImplementedError

    def budget_left(self) -> int:
        raise NotImplementedError

    def reset_budget(self, amount: int | None = None) -> int:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
