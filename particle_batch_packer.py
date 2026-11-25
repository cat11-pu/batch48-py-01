"""Deterministic particle burst packing and batch bookkeeping."""

from __future__ import annotations

import copy
import threading
from typing import Any


_BLEND_MODES = frozenset({"alpha", "additive", "multiply"})
_SNAPSHOT_CODE = "PARTICLE_SNAPSHOT_INVALID"


class ParticleError(ValueError):
    """Raised when particles cannot be packed into instance batches."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _is_real_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _particle_seq(particle: dict) -> object:
    return particle["seq"] if "seq" in particle else -1


class ParticleBatchPacker:
    """Merge particle bursts into instance batches per material and blend."""

    def __init__(self, *, max_materials: int = 4096,
                 max_burst_particles: int = 100000,
                 max_batches: int = 1024) -> None:
        self.max_materials = max_materials
        self.max_burst_particles = max_burst_particles
        self.max_batches = max_batches
        self._lock = threading.RLock()
        self._reset_state_locked()

    def _reset_state_locked(self) -> None:
        self._bursts: dict[str, list[dict]] = {}
        self._material_refs: dict[str, int] = {}
        self._batches: list[dict[str, Any]] = []
        self._instances: dict[str, list[dict[str, Any]]] = {}
        self._splits: dict[str, int] = {}
        self._overflow = 0

    @staticmethod
    def _error(code: str, detail: object = None) -> ParticleError:
        return ParticleError(code, detail)

    @classmethod
    def _validate_particle(cls, particle: object) -> dict:
        if not isinstance(particle, dict):
            raise cls._error(
                "PARTICLE_PARTICLE_INVALID", "particle must be a dictionary")

        material = particle.get("material")
        if not isinstance(material, str) or not material:
            raise cls._error(
                "PARTICLE_PARTICLE_INVALID", "material must be non-empty")

        blend = particle.get("blend")
        if blend not in _BLEND_MODES:
            raise cls._error(
                "PARTICLE_PARTICLE_INVALID",
                f"blend must be one of {sorted(_BLEND_MODES)}")

        return particle

    @classmethod
    def _normalize_emitter_id(cls, emitter_id: object) -> str:
        if not isinstance(emitter_id, str):
            raise cls._error(
                "PARTICLE_EMITTER_INVALID", "emitter_id must be a string")

        normalized = emitter_id.strip()
        if not normalized:
            raise cls._error(
                "PARTICLE_EMITTER_INVALID", "emitter_id must not be blank")
        return normalized

    def add_burst(self, emitter_id: str, particles) -> dict:
        emitter = self._normalize_emitter_id(emitter_id)

        try:
            particle_list = list(particles)
        except TypeError as exc:
            raise self._error(
                "PARTICLE_BURST_INVALID", "particles must be a list") from exc

        if not particle_list:
            raise self._error(
                "PARTICLE_BURST_INVALID", "particles must not be empty")
        if len(particle_list) > self.max_burst_particles:
            raise self._error(
                "PARTICLE_BURST_LIMIT",
                {
                    "count": len(particle_list),
                    "limit": self.max_burst_particles,
                },
            )

        for particle in particle_list:
            self._validate_particle(particle)

        materials = {particle["material"] for particle in particle_list}

        with self._lock:
            candidate_refs = copy.copy(self._material_refs)
            previous = self._bursts.get(emitter)
            if previous is not None:
                for particle in previous:
                    material = particle["material"]
                    candidate_refs[material] -= 1
                    if candidate_refs[material] == 0:
                        del candidate_refs[material]

            for particle in particle_list:
                material = particle["material"]
                candidate_refs[material] = (
                    candidate_refs.get(material, 0) + 1)

            if len(candidate_refs) > self.max_materials:
                raise self._error(
                    "PARTICLE_MATERIAL_LIMIT",
                    {
                        "materials": len(candidate_refs),
                        "limit": self.max_materials,
                    },
                )

            self._bursts[emitter] = copy.deepcopy(particle_list)
            self._material_refs = candidate_refs

            return {
                "emitter_id": emitter,
                "count": len(particle_list),
                "materials": sorted(materials),
            }

    def build(self, max_instances: int = 65536) -> list[dict]:
        valid_range = _is_real_int(max_instances) and 1 <= max_instances <= 65536
        whole_burst_hint = (
            _is_real_int(max_instances)
            and max_instances > 65536
            and max_instances == self.max_burst_particles
        )
        if not valid_range and not whole_burst_hint:
            raise self._error(
                "PARTICLE_MAX_INSTANCES_INVALID",
                {"max_instances": max_instances},
            )

        chunk_size = min(max_instances, 65536)

        with self._lock:
            groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
            total_instances = 0

            for emitter in sorted(self._bursts):
                particles = self._bursts[emitter]
                total_instances += len(particles)
                for particle in particles:
                    key = (particle["material"], particle["blend"])
                    instance = {
                        "emitter_id": emitter,
                        "seq": _particle_seq(particle),
                    }
                    groups.setdefault(key, []).append(instance)

            batches: list[dict[str, Any]] = []
            instances_by_batch: dict[str, list[dict[str, Any]]] = {}
            first_instance = 0
            base_batch_count = 0

            for material, blend in sorted(groups):
                group_instances = groups[(material, blend)]

                for chunk_index, start in enumerate(
                        range(0, len(group_instances), chunk_size)):
                    if base_batch_count >= self.max_batches:
                        break

                    chunk = group_instances[start:start + chunk_size]
                    batch_id = f"{material}|{blend}|{chunk_index}"
                    batch: dict[str, Any] = {
                        "batch_id": batch_id,
                        "material": material,
                        "blend": blend,
                        "count": len(chunk),
                        "first_instance": first_instance,
                        "emitters": self._emitter_order(chunk),
                        "_instances": chunk,
                    }

                    final_batches = self._apply_splits(batch)
                    for final_batch in final_batches:
                        batch_instances = final_batch.pop("_instances")
                        instances_by_batch[final_batch["batch_id"]] = (
                            batch_instances)
                        batches.append(final_batch)

                    first_instance += len(chunk)
                    base_batch_count += 1

                if base_batch_count >= self.max_batches:
                    break

            if len(batches) > self.max_batches:
                batches = batches[:self.max_batches]
                kept_ids = {batch["batch_id"] for batch in batches}
                instances_by_batch = {
                    batch_id: batch_instances
                    for batch_id, batch_instances
                    in instances_by_batch.items()
                    if batch_id in kept_ids
                }

            included_instances = sum(batch["count"] for batch in batches)
            overflow = total_instances - included_instances

            self._batches = batches
            self._instances = instances_by_batch
            self._overflow = overflow

            return copy.deepcopy([
                self._public_batch(batch) for batch in self._batches
            ])

    def split(self, batch_id: str, parts: int) -> list:
        with self._lock:
            batch_index = self._find_batch_index_locked(batch_id)
            if batch_index is None:
                raise self._error(
                    "PARTICLE_BATCH_UNKNOWN", {"batch_id": batch_id})

            batch = self._batches[batch_index]
            if not _is_real_int(parts) or parts < 2 or parts > batch["count"]:
                raise self._error(
                    "PARTICLE_SPLIT_INVALID",
                    {"batch_id": batch_id, "parts": parts},
                )

            batch_instances = self._instances[batch_id]
            sizes = self._partition_sizes(len(batch_instances), parts)

            child_batches: list[dict[str, Any]] = []
            child_instances: dict[str, list[dict[str, Any]]] = {}
            start = 0
            offset = batch["first_instance"]

            for index, size in enumerate(sizes):
                end = start + size
                child_slice = batch_instances[start:end]
                child_id = f"{batch_id}#{index}"
                child = {
                    "batch_id": child_id,
                    "material": batch["material"],
                    "blend": batch["blend"],
                    "count": size,
                    "first_instance": offset,
                    "emitters": self._emitter_order(child_slice),
                }
                child_batches.append(child)
                child_instances[child_id] = child_slice
                start = end
                offset += size

            self._splits[batch_id] = parts
            self._batches[batch_index:batch_index + 1] = child_batches
            self._instances.pop(batch_id, None)
            self._instances.update(child_instances)

            return copy.deepcopy([
                self._public_batch(batch) for batch in child_batches
            ])

    def order_key(self, particle: dict) -> tuple:
        self._validate_particle(particle)
        return (
            particle["material"],
            particle["blend"],
            _particle_seq(particle),
        )

    def instances(self, batch_id: str) -> list:
        with self._lock:
            if batch_id not in self._instances:
                raise self._error(
                    "PARTICLE_BATCH_UNKNOWN", {"batch_id": batch_id})
            return copy.deepcopy(self._instances[batch_id])

    def batches(self) -> list:
        with self._lock:
            return copy.deepcopy([
                self._public_batch(batch) for batch in self._batches
            ])

    def remove_burst(self, emitter_id: str) -> bool:
        emitter = self._normalize_emitter_id(emitter_id)

        with self._lock:
            previous = self._bursts.pop(emitter, None)
            if previous is None:
                return False

            for particle in previous:
                material = particle["material"]
                self._material_refs[material] -= 1
                if self._material_refs[material] == 0:
                    del self._material_refs[material]

            return True

    def stats(self) -> dict:
        with self._lock:
            return {
                "batches": len(self._batches),
                "instances": sum(batch["count"] for batch in self._batches),
                "materials": len(self._material_refs),
                "overflow": self._overflow,
            }

    def snapshot(self) -> dict:
        with self._lock:
            batches = []
            for batch in self._batches:
                snapshot_batch = copy.deepcopy(self._public_batch(batch))
                snapshot_batch["instances"] = copy.deepcopy(
                    self._instances[batch["batch_id"]])
                batches.append(snapshot_batch)

            return {
                "emitters": copy.deepcopy(self._bursts),
                "batches": batches,
                "splits": copy.deepcopy(self._splits),
                "overflow": self._overflow,
            }

    def restore(self, state: dict) -> None:
        if not isinstance(state, dict):
            raise self._error(_SNAPSHOT_CODE, "state must be a dictionary")

        with self._lock:
            try:
                restored = copy.deepcopy(state)
                (bursts, material_refs, batches, instances,
                 splits, overflow) = self._validate_snapshot(restored)
            except ParticleError:
                raise
            except Exception as exc:
                raise self._error(_SNAPSHOT_CODE, str(exc)) from exc

            self._bursts = bursts
            self._material_refs = material_refs
            self._batches = batches
            self._instances = instances
            self._splits = splits
            self._overflow = overflow

    def reset(self) -> None:
        with self._lock:
            self._reset_state_locked()

    @staticmethod
    def _emitter_order(instances: list[dict[str, Any]]) -> list[str]:
        emitters: list[str] = []
        seen: set[str] = set()
        for instance in instances:
            emitter = instance["emitter_id"]
            if emitter not in seen:
                seen.add(emitter)
                emitters.append(emitter)
        return emitters

    @staticmethod
    def _partition_sizes(count: int, parts: int) -> list[int]:
        quotient, remainder = divmod(count, parts)
        return [
            quotient + (1 if index < remainder else 0)
            for index in range(parts)
        ]

    @staticmethod
    def _public_batch(batch: dict[str, Any]) -> dict[str, Any]:
        return {
            "batch_id": batch["batch_id"],
            "material": batch["material"],
            "blend": batch["blend"],
            "count": batch["count"],
            "first_instance": batch["first_instance"],
            "emitters": list(batch["emitters"]),
        }

    def _find_batch_index_locked(self, batch_id: object) -> int | None:
        if not isinstance(batch_id, str):
            return None
        for index, batch in enumerate(self._batches):
            if batch["batch_id"] == batch_id:
                return index
        return None

    def _apply_splits(
            self, batch: dict[str, Any]) -> list[dict[str, Any]]:
        parts = self._splits.get(batch["batch_id"])
        count = batch["count"]

        if parts is None:
            return [batch]
        if not _is_real_int(parts) or parts < 2 or parts > count:
            return [batch]

        instances = batch.pop("_instances")
        sizes = self._partition_sizes(count, parts)
        result: list[dict[str, Any]] = []
        start = 0

        for index, size in enumerate(sizes):
            child_instances = instances[start:start + size]
            child = {
                "batch_id": f'{batch["batch_id"]}#{index}',
                "material": batch["material"],
                "blend": batch["blend"],
                "count": size,
                "first_instance": batch["first_instance"] + start,
                "emitters": self._emitter_order(child_instances),
                "_instances": child_instances,
            }
            result.extend(self._apply_splits(child))
            start += size

        return result

    def _validate_snapshot(self, state: dict[str, Any]):
        emitters = state.get("emitters")
        raw_batches = state.get("batches")
        raw_splits = state.get("splits")
        overflow = state.get("overflow")

        if not isinstance(emitters, dict):
            raise self._error(_SNAPSHOT_CODE, "emitters must be a dictionary")
        if not isinstance(raw_batches, list):
            raise self._error(_SNAPSHOT_CODE, "batches must be a list")
        if not isinstance(raw_splits, dict):
            raise self._error(_SNAPSHOT_CODE, "splits must be a dictionary")
        if not _is_real_int(overflow) or overflow < 0:
            raise self._error(_SNAPSHOT_CODE, "overflow must be non-negative")

        bursts: dict[str, list[dict]] = {}
        material_refs: dict[str, int] = {}
        for emitter, particles in emitters.items():
            if not isinstance(emitter, str):
                raise self._error(_SNAPSHOT_CODE, "invalid emitter key")
            normalized_emitter = self._validate_snapshot_emitter(emitter)
            if normalized_emitter in bursts:
                raise self._error(_SNAPSHOT_CODE, "duplicate emitter key")
            if not isinstance(particles, list) or not particles:
                raise self._error(
                    _SNAPSHOT_CODE,
                    "emitter particles must be a non-empty list")

            for particle in particles:
                self._validate_snapshot_particle(particle)
                material = particle["material"]
                material_refs[material] = material_refs.get(material, 0) + 1

            bursts[normalized_emitter] = particles

        batches: list[dict[str, Any]] = []
        instances: dict[str, list[dict[str, Any]]] = {}
        seen_batch_ids: set[str] = set()
        expected_offset = 0
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        cursors: dict[tuple[str, str], int] = {}

        for emitter in sorted(bursts):
            for particle in bursts[emitter]:
                key = (particle["material"], particle["blend"])
                groups.setdefault(key, []).append({
                    "emitter_id": emitter,
                    "seq": _particle_seq(particle),
                })

        for raw_batch in raw_batches:
            batch = self._validate_snapshot_batch(
                raw_batch, seen_batch_ids, expected_offset)
            batch_id = batch["batch_id"]

            if "instances" in raw_batch:
                batch_instances = raw_batch["instances"]
                if not isinstance(batch_instances, list):
                    raise self._error(
                        _SNAPSHOT_CODE, "batch instances must be a list")
                if len(batch_instances) != batch["count"]:
                    raise self._error(
                        _SNAPSHOT_CODE, "batch instance count mismatch")
            else:
                key = (batch["material"], batch["blend"])
                group_instances = groups.get(key, [])
                start = cursors.get(key, 0)
                end = start + batch["count"]
                if end > len(group_instances):
                    raise self._error(
                        _SNAPSHOT_CODE, "batch instances do not match emitters")
                batch_instances = group_instances[start:end]
                cursors[key] = end

            for instance in batch_instances:
                if not isinstance(instance, dict):
                    raise self._error(
                        _SNAPSHOT_CODE, "instance must be a dictionary")
                instance_emitter = instance.get("emitter_id")
                if not isinstance(instance_emitter, str) \
                        or not instance_emitter:
                    raise self._error(
                        _SNAPSHOT_CODE, "invalid instance emitter")
                if "seq" not in instance:
                    raise self._error(
                        _SNAPSHOT_CODE, "instance sequence is missing")

            if self._emitter_order(batch_instances) != batch["emitters"]:
                raise self._error(
                    _SNAPSHOT_CODE, "batch emitter order mismatch")

            instances[batch_id] = batch_instances
            seen_batch_ids.add(batch_id)
            expected_offset += batch["count"]
            batches.append(batch)

        splits: dict[str, int] = {}
        for batch_id, parts in raw_splits.items():
            if not isinstance(batch_id, str) or not batch_id:
                raise self._error(_SNAPSHOT_CODE, "invalid split batch id")
            if not _is_real_int(parts) or parts < 2:
                raise self._error(_SNAPSHOT_CODE, "invalid split part count")
            splits[batch_id] = parts

        return bursts, material_refs, batches, instances, splits, overflow

    def _validate_snapshot_emitter(self, emitter: object) -> str:
        if not isinstance(emitter, str):
            raise self._error(_SNAPSHOT_CODE, "emitter must be a string")
        normalized = emitter.strip()
        if not normalized or normalized != emitter:
            raise self._error(_SNAPSHOT_CODE, "emitter must be normalized")
        return normalized

    def _validate_snapshot_particle(self, particle: object) -> None:
        try:
            self._validate_particle(particle)
        except ParticleError as exc:
            raise self._error(_SNAPSHOT_CODE, exc.detail) from exc

    def _validate_snapshot_batch(
            self, raw_batch: object, seen_ids: set[str],
            expected_offset: int) -> dict[str, Any]:
        if not isinstance(raw_batch, dict):
            raise self._error(_SNAPSHOT_CODE, "batch must be a dictionary")

        required = (
            "batch_id", "material", "blend", "count",
            "first_instance", "emitters",
        )
        if any(key not in raw_batch for key in required):
            raise self._error(_SNAPSHOT_CODE, "batch field is missing")

        batch_id = raw_batch["batch_id"]
        material = raw_batch["material"]
        blend = raw_batch["blend"]
        count = raw_batch["count"]
        first_instance = raw_batch["first_instance"]
        emitters = raw_batch["emitters"]

        if not isinstance(batch_id, str) or not batch_id:
            raise self._error(_SNAPSHOT_CODE, "invalid batch id")
        if batch_id in seen_ids:
            raise self._error(_SNAPSHOT_CODE, "duplicate batch id")
        if not isinstance(material, str) or not material:
            raise self._error(_SNAPSHOT_CODE, "invalid batch material")
        if blend not in _BLEND_MODES:
            raise self._error(_SNAPSHOT_CODE, "invalid batch blend")
        if not _is_real_int(count) or count <= 0:
            raise self._error(_SNAPSHOT_CODE, "invalid batch count")
        if not _is_real_int(first_instance) or first_instance < 0:
            raise self._error(_SNAPSHOT_CODE, "invalid first instance")
        if first_instance != expected_offset:
            raise self._error(_SNAPSHOT_CODE, "non-contiguous batch offset")
        if not isinstance(emitters, list) or not emitters:
            raise self._error(_SNAPSHOT_CODE, "invalid batch emitters")
        if not all(isinstance(emitter, str) and emitter
                   for emitter in emitters):
            raise self._error(_SNAPSHOT_CODE, "invalid batch emitter id")
        if len(emitters) != len(set(emitters)):
            raise self._error(_SNAPSHOT_CODE, "duplicate batch emitter")

        return {
            "batch_id": batch_id,
            "material": material,
            "blend": blend,
            "count": count,
            "first_instance": first_instance,
            "emitters": list(emitters),
        }
