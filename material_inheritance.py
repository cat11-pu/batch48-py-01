"""Material inheritance chain resolution for layered material instances."""

from __future__ import annotations

import threading


class MaterialError(ValueError):
    """Raised for every rejected material graph operation."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class MaterialInheritanceResolver:
    """Resolve parameter overrides across a material inheritance chain."""

    MAX_DEPTH = 32
    MAX_PARAM_NAME = 256
    MAX_MATERIALS = 20000

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def define(self, material_id: str, parent_id: str | None, params: dict) -> None:
        raise NotImplementedError

    def reparent(self, material_id: str, parent_id: str) -> None:
        raise NotImplementedError

    def override(self, material_id: str, name: str, value) -> None:
        raise NotImplementedError

    def override_many(self, material_id: str, values: dict) -> None:
        raise NotImplementedError

    def clear_override(self, material_id: str, name: str) -> None:
        raise NotImplementedError

    def resolve(self, material_id: str) -> dict:
        raise NotImplementedError

    def chain(self, material_id: str) -> list:
        raise NotImplementedError

    def detach(self, material_id: str) -> None:
        raise NotImplementedError

    def coerce(self, material_id: str, name: str, value):
        raise NotImplementedError

    def type_of(self, material_id: str, name: str) -> str:
        raise NotImplementedError

    def descendants(self, material_id: str) -> list:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
