"""Texture compression block alignment and mip chain planning."""

from __future__ import annotations

import copy
import re
import threading
from collections import OrderedDict


class TextureError(ValueError):
    """Raised for rejected texture compression plans."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class TextureCompressionPlanner:
    """Plan block aligned texture levels and mip chains per format."""

    MAX_DIMENSION = 16384
    MAX_LEVELS = 16
    CACHE_VERSION = 1
    MAX_CACHE_ENTRIES = 128
    BLOCK_SIZES = (1, 4, 6, 8)

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cache: "OrderedDict[str, dict]" = OrderedDict()

    def block_size(self, format: str) -> int:
        return self._format_info(format)["block"]

    def format_info(self, format: str) -> dict:
        return copy.deepcopy(self._format_info(format))

    def pad_to_block(self, width: int, height: int, block: int) -> dict:
        self._validate_dimensions(width, height)
        self._validate_block(block)
        geometry = self._geometry(width, height, block)
        return {
            "padded_width": geometry["padded_width"],
            "padded_height": geometry["padded_height"],
            "valid_width": geometry["valid_width"],
            "valid_height": geometry["valid_height"],
        }

    def mip_count(self, width: int, height: int, block: int) -> int:
        return len(self._mip_dimensions(width, height, block))

    def level_blocks(self, width: int, height: int, block: int) -> dict:
        self._validate_dimensions(width, height)
        self._validate_block(block)
        geometry = self._geometry(width, height, block)
        return {
            "blocks_x": geometry["blocks_x"],
            "blocks_y": geometry["blocks_y"],
            "blocks": geometry["blocks"],
        }

    def plan(self, width: int, height: int, format: str) -> dict:
        info = self._format_info(format)
        self._validate_dimensions(width, height)
        key = self._make_key(width, height, info)

        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return copy.deepcopy(cached)

            result = self._build_plan(width, height, info, key)
            if result["level_count"] > self.MAX_LEVELS:
                raise TextureError("TEXTURE_LEVEL_TOO_MANY")

            self._cache[key] = result
            self._cache.move_to_end(key)
            while len(self._cache) > self.MAX_CACHE_ENTRIES:
                self._cache.popitem(last=False)
            return copy.deepcopy(result)

    def validate(self, levels: list) -> list:
        if not isinstance(levels, (list, tuple)) or len(levels) == 0:
            return [0]

        problems = []
        first_block = None

        for index, level in enumerate(levels):
            bad = False

            if not isinstance(level, dict):
                problems.append(index)
                continue

            required = (
                "level", "width", "height", "block", "padded_width",
                "padded_height", "blocks_x", "blocks_y", "blocks",
            )
            if any(key not in level for key in required):
                problems.append(index)
                continue

            block = level.get("block")
            dimension_keys = ("width", "height", "padded_width", "padded_height")
            count_keys = ("blocks_x", "blocks_y", "blocks")
            if (
                level.get("level") != index
                or not all(self._is_strict_int(level.get(key)) for key in dimension_keys)
                or not all(self._is_strict_int(level.get(key)) for key in count_keys)
                or not self._is_strict_int(block)
                or block not in self.BLOCK_SIZES
            ):
                bad = True
            else:
                width = level["width"]
                height = level["height"]
                padded_width = level["padded_width"]
                padded_height = level["padded_height"]
                blocks_x = level["blocks_x"]
                blocks_y = level["blocks_y"]
                blocks = level["blocks"]

                if first_block is None:
                    first_block = block
                elif block != first_block:
                    bad = True

                if width <= 0 or height <= 0 or blocks_x <= 0 or blocks_y <= 0:
                    bad = True
                else:
                    expected = self._geometry(width, height, block)
                    if (
                        padded_width != expected["padded_width"]
                        or padded_height != expected["padded_height"]
                        or blocks_x != expected["blocks_x"]
                        or blocks_y != expected["blocks_y"]
                        or blocks != expected["blocks"]
                    ):
                        bad = True

                is_last = index == len(levels) - 1
                if is_last and (width != 1 or height != 1):
                    bad = True

                if index > 0:
                    previous = levels[index - 1]
                    if isinstance(previous, dict):
                        previous_width = previous.get("width")
                        previous_height = previous.get("height")
                        if (
                            self._is_strict_int(previous_width)
                            and self._is_strict_int(previous_height)
                            and previous_width > 0
                            and previous_height > 0
                        ):
                            expected_width = max(1, previous_width // 2)
                            expected_height = max(1, previous_height // 2)
                            final_pixel_jump = is_last and width == 1 and height == 1
                            if not final_pixel_jump and (
                                width != expected_width or height != expected_height
                            ):
                                bad = True
                        else:
                            bad = True

            if bad:
                problems.append(index)

        return problems

    def cache_key(self, width: int, height: int, format: str) -> str:
        info = self._format_info(format)
        return self._make_key(width, height, info)

    def cache_size(self) -> int:
        with self._lock:
            return len(self._cache)

    def invalidate_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": self.CACHE_VERSION,
                "cache": copy.deepcopy(dict(self._cache)),
            }

    def restore(self, state) -> None:
        try:
            restored_state = copy.deepcopy(state)
            new_cache = self._validated_snapshot(restored_state)
        except TextureError:
            raise
        except Exception as exc:
            raise TextureError("TEXTURE_SNAPSHOT_INVALID") from exc

        with self._lock:
            self._cache.clear()
            for key, value in new_cache.items():
                self._cache[key] = copy.deepcopy(value)

    @classmethod
    def _format_info(cls, format: str) -> dict:
        if not isinstance(format, str) or not format:
            raise TextureError("TEXTURE_FORMAT_INVALID")

        srgb = "SRGB" in format

        if format == "RGBA8" or re.fullmatch(r"RGBA8_[A-Z0-9_]+", format):
            block = 1
        elif format in cls._bc_formats():
            block = 4
        elif re.fullmatch(r"ASTC_6x6_[A-Z0-9_]+", format):
            block = 6
        elif re.fullmatch(r"ASTC_8x8_[A-Z0-9_]+", format):
            block = 8
        elif re.fullmatch(r"DXT[135](?:_[A-Z0-9_]+)?", format):
            block = 4
        else:
            raise TextureError("TEXTURE_FORMAT_INVALID")

        return {
            "format": format,
            "block": block,
            "srgb": srgb,
            "kind": "linear" if block == 1 else "block",
        }

    @staticmethod
    def _bc_formats() -> set[str]:
        formats = set()
        for number in (1, 2, 3, 7):
            formats.add(f"BC{number}_UNORM")
            formats.add(f"BC{number}_SRGB")
            formats.add(f"BC{number}_UNORM_SRGB")
        for number in (4, 5):
            formats.add(f"BC{number}_UNORM")
            formats.add(f"BC{number}_SNORM")
        for token in ("UF16", "SF16", "FLOAT"):
            formats.add(f"BC6H_{token}")
        return formats

    @staticmethod
    def _is_strict_int(value) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    @classmethod
    def _validate_dimensions(cls, width: int, height: int) -> None:
        if not cls._is_strict_int(width) or not cls._is_strict_int(height):
            raise TextureError("TEXTURE_SIZE_INVALID")
        if width <= 0 or height <= 0:
            raise TextureError("TEXTURE_SIZE_INVALID")
        if width > cls.MAX_DIMENSION or height > cls.MAX_DIMENSION:
            raise TextureError("TEXTURE_SIZE_TOO_LARGE")

    @classmethod
    def _validate_block(cls, block: int) -> None:
        if not cls._is_strict_int(block) or block not in cls.BLOCK_SIZES:
            raise TextureError("TEXTURE_BLOCK_INVALID")

    @staticmethod
    def _geometry(width: int, height: int, block: int) -> dict:
        padded_width = ((width + block - 1) // block) * block
        padded_height = ((height + block - 1) // block) * block
        blocks_x = padded_width // block
        blocks_y = padded_height // block
        return {
            "valid_width": width,
            "valid_height": height,
            "padded_width": padded_width,
            "padded_height": padded_height,
            "blocks_x": blocks_x,
            "blocks_y": blocks_y,
            "blocks": blocks_x * blocks_y,
        }

    @classmethod
    def _mip_stop_extent(cls, block: int) -> int:
        if block == 1:
            return 1
        return 1 << (block - 1).bit_length()

    @classmethod
    def _mip_dimensions(
        cls, width: int, height: int, block: int
    ) -> list[tuple[int, int]]:
        cls._validate_dimensions(width, height)
        cls._validate_block(block)

        stop_extent = cls._mip_stop_extent(block)
        dimensions = []
        current_width, current_height = width, height

        while True:
            dimensions.append((current_width, current_height))

            if block == 1:
                if current_width == 1 and current_height == 1:
                    break
            elif current_width <= stop_extent and current_height <= stop_extent:
                if (current_width, current_height) != (1, 1):
                    dimensions.append((1, 1))
                break

            next_width = max(1, current_width // 2)
            next_height = max(1, current_height // 2)
            if (next_width, next_height) == (current_width, current_height):
                if (current_width, current_height) != (1, 1):
                    dimensions.append((1, 1))
                break
            current_width, current_height = next_width, next_height

        if len(dimensions) > cls.MAX_LEVELS:
            raise TextureError("TEXTURE_LEVEL_TOO_MANY")
        return dimensions

    @classmethod
    def _make_key(cls, width: int, height: int, info: dict) -> str:
        cls._validate_dimensions(width, height)
        return (
            f"texture:v{cls.CACHE_VERSION}:{info['format']}:"
            f"{width}x{height}:b{info['block']}:srgb{int(info['srgb'])}"
        )

    @classmethod
    def _build_plan(cls, width: int, height: int, info: dict, key: str) -> dict:
        block = info["block"]
        levels = []

        dimensions = cls._mip_dimensions(width, height, block)
        if len(dimensions) > cls.MAX_LEVELS:
            raise TextureError("TEXTURE_LEVEL_TOO_MANY")

        for index, (level_width, level_height) in enumerate(dimensions):
            geometry = cls._geometry(level_width, level_height, block)
            levels.append({
                "level": index,
                "width": level_width,
                "height": level_height,
                "padded_width": geometry["padded_width"],
                "padded_height": geometry["padded_height"],
                "blocks_x": geometry["blocks_x"],
                "blocks_y": geometry["blocks_y"],
                "blocks": geometry["blocks"],
                "block": block,
            })

        return {
            "format": info["format"],
            "block": block,
            "level_count": len(levels),
            "levels": levels,
            "cache_key": key,
        }

    @classmethod
    def _validated_snapshot(cls, state) -> "OrderedDict[str, dict]":
        if not isinstance(state, dict):
            raise TextureError("TEXTURE_SNAPSHOT_INVALID")

        version = state.get("version")
        if type(version) is not int or version != cls.CACHE_VERSION:
            raise TextureError("TEXTURE_SNAPSHOT_INVALID")

        cache = state.get("cache")
        if not isinstance(cache, dict):
            raise TextureError("TEXTURE_SNAPSHOT_INVALID")
        if len(cache) > cls.MAX_CACHE_ENTRIES:
            raise TextureError("TEXTURE_SNAPSHOT_INVALID")

        new_cache = OrderedDict()
        try:
            for key, entry in cache.items():
                if not isinstance(key, str) or not isinstance(entry, dict):
                    raise TextureError("TEXTURE_SNAPSHOT_INVALID")

                format_name = entry["format"]
                block = entry["block"]
                levels = entry["levels"]
                cache_key = entry["cache_key"]
                level_count = entry["level_count"]
                if not isinstance(levels, (list, tuple)) or not levels:
                    raise KeyError("levels")

                first_level = levels[0]
                width = first_level["width"]
                height = first_level["height"]

                info = cls._format_info(format_name)
                if (
                    not cls._is_strict_int(block)
                    or block != info["block"]
                    or not isinstance(cache_key, str)
                    or cache_key != cls._make_key(width, height, info)
                    or not cls._is_strict_int(level_count)
                    or level_count != len(levels)
                    or level_count > cls.MAX_LEVELS
                ):
                    raise TextureError("TEXTURE_SNAPSHOT_INVALID")

                canonical = cls._build_plan(width, height, info, cache_key)
                if entry != canonical:
                    raise TextureError("TEXTURE_SNAPSHOT_INVALID")
                new_cache[cache_key] = copy.deepcopy(canonical)
        except TextureError as exc:
            if exc.code == "TEXTURE_SNAPSHOT_INVALID":
                raise
            raise TextureError("TEXTURE_SNAPSHOT_INVALID") from exc
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise TextureError("TEXTURE_SNAPSHOT_INVALID") from exc

        return new_cache
