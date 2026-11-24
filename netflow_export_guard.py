"""Flow export guard for a NetFlow collector."""

from __future__ import annotations

import threading


class ExportGuardError(ValueError):
    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class NetflowExportGuard:
    """Age flows, manage export templates and drain an export queue."""

    def __init__(self, *, active_timeout: int = 30, interim_interval: int = 60,
                 max_queue: int = 512, max_flows: int = 10000,
                 sampling_rate: int = 1, clock=None) -> None:
        self.active_timeout = active_timeout
        self.interim_interval = interim_interval
        self.max_queue = max_queue
        self.max_flows = max_flows
        self.sampling_rate = sampling_rate
        self._clock = clock
        self._lock = threading.RLock()

    def add_packet(self, *, src: str, dst: str, src_port: int, dst_port: int,
                   protocol: str, length: int, tcp_flags: int = 0,
                   now=None) -> dict:
        raise NotImplementedError

    def close_flow(self, flow_key: str, *, now=None) -> dict:
        raise NotImplementedError

    def set_template(self, template_id: int, fields: list[str]) -> bool:
        raise NotImplementedError

    def export_due(self, *, now=None) -> list[dict]:
        raise NotImplementedError

    def flush(self, *, now=None) -> list[dict]:
        raise NotImplementedError

    def active_flows(self, *, now=None) -> list[dict]:
        raise NotImplementedError

    def queue_depth(self) -> int:
        raise NotImplementedError

    def dropped_count(self) -> int:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
