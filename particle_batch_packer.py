"""Particle burst packing by material and blend mode."""

from __future__ import annotations


class ParticleError(ValueError):
    """Raised when particles cannot be packed into instance batches."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class ParticleBatchPacker:
    """Merge particle bursts into instance batches per material and blend."""

    def __init__(self, *, max_materials: int = 4096,
                 max_burst_particles: int = 100000,
                 max_batches: int = 1024) -> None:
        self.max_materials = max_materials
        self.max_burst_particles = max_burst_particles
        self.max_batches = max_batches

    def add_burst(self, emitter_id: str, particles) -> dict:
        raise NotImplementedError

    def build(self, max_instances: int = 65536) -> list:
        raise NotImplementedError

    def split(self, batch_id: str, parts: int) -> list:
        raise NotImplementedError

    def order_key(self, particle: dict) -> tuple:
        raise NotImplementedError

    def instances(self, batch_id: str) -> list:
        raise NotImplementedError

    def batches(self) -> list:
        raise NotImplementedError

    def remove_burst(self, emitter_id: str) -> bool:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def reset(self) -> None:
        raise NotImplementedError
