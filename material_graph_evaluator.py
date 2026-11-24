"""Material node graph topological evaluation with versioned caching."""

from __future__ import annotations


class MaterialGraphError(ValueError):
    """Raised when a material graph cannot be built or evaluated."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class MaterialGraphEvaluator:
    """Evaluate scalar and vector material nodes with constant folding."""

    def __init__(self, *, max_nodes: int = 4096, max_inputs: int = 16,
                 max_depth: int = 512) -> None:
        self.max_nodes = max_nodes
        self.max_inputs = max_inputs
        self.max_depth = max_depth

    def add_node(self, node_id: str, op: str, inputs: dict | None = None) -> dict:
        raise NotImplementedError

    def connect(self, node_id: str, port: str, source_node: str,
                source_port: str = "out") -> dict:
        raise NotImplementedError

    def set_input(self, node_id: str, name: str, value) -> dict:
        raise NotImplementedError

    def evaluate(self, node_id: str):
        raise NotImplementedError

    def evaluate_all(self) -> dict:
        raise NotImplementedError

    def fold_constants(self) -> int:
        raise NotImplementedError

    def invalidate(self, node_id: str) -> int:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
