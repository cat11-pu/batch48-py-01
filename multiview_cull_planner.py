"""Multi-view frustum culling with per-object view masks."""

from __future__ import annotations


class CullError(ValueError):
    """Raised when a view, object or bounds cannot be registered or culled."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class MultiviewCullPlanner:
    """Cull a scene once for several cameras and expose per-view visibility."""

    def __init__(self, *, max_views: int = 8,
                 max_objects: int = 200000) -> None:
        self.max_views = max_views
        self.max_objects = max_objects

    def add_view(self, view_id: str, camera: dict) -> dict:
        raise NotImplementedError

    def remove_view(self, view_id: str) -> bool:
        raise NotImplementedError

    def set_object(self, obj_id: str, bounds: dict, layers: int = 1) -> dict:
        raise NotImplementedError

    def remove_object(self, obj_id: str) -> bool:
        raise NotImplementedError

    def views(self) -> list:
        raise NotImplementedError

    def objects(self) -> list:
        raise NotImplementedError

    def cull(self) -> dict:
        raise NotImplementedError

    def lod_for(self, obj_id: str, view_id: str) -> int:
        raise NotImplementedError

    def stable_views(self) -> list:
        raise NotImplementedError

    def view_key(self, view_id: str) -> str:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def reset(self) -> None:
        raise NotImplementedError
