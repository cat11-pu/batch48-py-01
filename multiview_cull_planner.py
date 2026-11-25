"""Multi-view frustum culling with per-object view masks."""

from __future__ import annotations

import copy
import json
import math
import numbers
import threading


_CAMERA_CODES = "CULL_CAMERA_INVALID"


class CullError(ValueError):
    """Raised when a view, object or bounds cannot be registered or culled."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _finite_number(value: object, code: str, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise CullError(code, f"{field} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise CullError(code, f"{field} must be a finite number") from None
    if not math.isfinite(number):
        raise CullError(code, f"{field} must be finite")
    if number == 0.0:
        number = 0.0
    return number


def _vec3(value: object, code: str, field: str) -> tuple[float, float, float]:
    if not isinstance(value, (tuple, list)) or len(value) != 3:
        raise CullError(code, f"{field} must be a three-element sequence")
    return (
        _finite_number(value[0], code, f"{field}[0]"),
        _finite_number(value[1], code, f"{field}[1]"),
        _finite_number(value[2], code, f"{field}[2]"),
    )


def _nonnegative_int(value: object, code: str, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CullError(code, f"{field} must be a non-negative integer")
    if value < 0:
        raise CullError(code, f"{field} must be a non-negative integer")
    return value


def _normalize_id(value: object) -> str:
    if not isinstance(value, str):
        raise CullError("CULL_ID_INVALID", "identifier must be a string")
    identifier = value.strip()
    if not identifier:
        raise CullError("CULL_ID_INVALID", "identifier must not be empty")
    return identifier


def _sub(a: tuple[float, float, float],
         b: tuple[float, float, float]) -> tuple[float, float, float]:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a: tuple[float, float, float],
         b: tuple[float, float, float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: tuple[float, float, float],
           b: tuple[float, float, float]) -> tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _length(v: tuple[float, float, float]) -> float:
    return math.sqrt(_dot(v, v))


def _normalize(v: tuple[float, float, float]) -> tuple[float, float, float]:
    length = _length(v)
    if length <= 1e-15 or not math.isfinite(length):
        raise CullError(_CAMERA_CODES, "camera orientation is degenerate")
    return (v[0] / length, v[1] / length, v[2] / length)


def _point_plane_d(normal: tuple[float, float, float],
                   point: tuple[float, float, float]) -> float:
    return -_dot(normal, point)


def _validate_camera(camera: object) -> dict:
    if not isinstance(camera, dict):
        raise CullError(_CAMERA_CODES, "camera must be a dictionary")

    kind = camera.get("kind")
    if kind not in ("perspective", "ortho"):
        raise CullError(_CAMERA_CODES, "unknown camera kind")

    required = ("position", "target", "up", "aspect", "near", "far")
    for field in required:
        if field not in camera:
            raise CullError(_CAMERA_CODES, f"missing camera field {field}")

    position = _vec3(camera["position"], _CAMERA_CODES, "position")
    target = _vec3(camera["target"], _CAMERA_CODES, "target")
    up = _vec3(camera["up"], _CAMERA_CODES, "up")
    aspect = _finite_number(camera["aspect"], _CAMERA_CODES, "aspect")
    near = _finite_number(camera["near"], _CAMERA_CODES, "near")
    far = _finite_number(camera["far"], _CAMERA_CODES, "far")

    if aspect <= 0.0:
        raise CullError(_CAMERA_CODES, "aspect must be positive")
    if near == far:
        raise CullError(_CAMERA_CODES, "near and far must differ")

    layers = _nonnegative_int(camera.get("layers", 0xFFFF),
                              _CAMERA_CODES, "layers")

    forward = _sub(target, position)
    if _length(forward) <= 1e-15:
        raise CullError(_CAMERA_CODES, "target must differ from position")
    forward = _normalize(forward)
    # This also rejects a zero or parallel up vector.
    right = _normalize(_cross(forward, up))
    true_up = _cross(right, forward)
    if _length(true_up) <= 1e-15:
        raise CullError(_CAMERA_CODES, "up vector is invalid")
    true_up = _normalize(true_up)

    normalized: dict = {
        "kind": kind,
        "position": position,
        "target": target,
        "up": up,
        "aspect": aspect,
        "near": near,
        "far": far,
        "layers": layers,
    }

    if kind == "perspective":
        if "fov_deg" not in camera:
            raise CullError(_CAMERA_CODES, "missing camera field fov_deg")
        fov = _finite_number(camera["fov_deg"], _CAMERA_CODES, "fov_deg")
        if fov <= 0.0 or fov >= 180.0:
            raise CullError(_CAMERA_CODES, "fov_deg must be between 0 and 180")
        normalized["fov_deg"] = fov
        tan_half = math.tan(math.radians(fov) * 0.5)
        if not math.isfinite(tan_half) or tan_half <= 0.0:
            raise CullError(_CAMERA_CODES, "fov_deg is invalid")
        normalized["_tan_half_fov"] = tan_half
        normalized["_right"] = right
        normalized["_up"] = true_up
        normalized["_forward"] = forward
    else:
        if "half_height" not in camera:
            raise CullError(_CAMERA_CODES, "missing camera field half_height")
        half_height = _finite_number(camera["half_height"],
                                     _CAMERA_CODES, "half_height")
        if half_height <= 0.0:
            raise CullError(_CAMERA_CODES, "half_height must be positive")
        normalized["half_height"] = half_height
        normalized["_right"] = right
        normalized["_up"] = true_up
        normalized["_forward"] = forward

    return normalized


def _camera_key(camera: dict) -> str:
    common = [
        camera["kind"],
        list(camera["position"]),
        list(camera["target"]),
        list(camera["up"]),
        camera["aspect"],
        camera["near"],
        camera["far"],
        camera["layers"],
    ]
    if camera["kind"] == "perspective":
        common.append(camera["fov_deg"])
    else:
        common.append(camera["half_height"])
    return json.dumps(common, separators=(",", ":"), allow_nan=False)


def _depth_range(near: float, far: float) -> tuple[float, float, bool]:
    low = min(near, far)
    high = max(near, far)
    if high < 0.0:
        return 0.0, 0.0, True
    low = max(low, 0.0)
    if high <= low:
        return low, high, True
    return low, high, False


def _build_planes(
        camera: dict,
) -> tuple[tuple[tuple[float, float, float, float], ...], bool]:
    position = camera["position"]
    forward = camera["_forward"]
    right = camera["_right"]
    up = camera["_up"]
    low, high, empty = _depth_range(camera["near"], camera["far"])

    if empty:
        return (), True

    planes: list[tuple[float, float, float, float]] = []

    def add(normal: tuple[float, float, float],
            point: tuple[float, float, float]) -> None:
        unit = _normalize(normal)
        planes.append((unit[0], unit[1], unit[2],
                       _point_plane_d(unit, point)))

    if camera["kind"] == "perspective":
        tan_v = camera["_tan_half_fov"]
        tan_h = tan_v * camera["aspect"]
        add((right[0] + forward[0] * tan_h,
             right[1] + forward[1] * tan_h,
             right[2] + forward[2] * tan_h), position)
        add((-right[0] + forward[0] * tan_h,
             -right[1] + forward[1] * tan_h,
             -right[2] + forward[2] * tan_h), position)
        add((up[0] + forward[0] * tan_v,
             up[1] + forward[1] * tan_v,
             up[2] + forward[2] * tan_v), position)
        add((-up[0] + forward[0] * tan_v,
             -up[1] + forward[1] * tan_v,
             -up[2] + forward[2] * tan_v), position)
    else:
        half_height = camera["half_height"]
        half_width = half_height * camera["aspect"]
        add(right, (position[0] - right[0] * half_width,
                    position[1] - right[1] * half_width,
                    position[2] - right[2] * half_width))
        add((-right[0], -right[1], -right[2]),
            (position[0] + right[0] * half_width,
             position[1] + right[1] * half_width,
             position[2] + right[2] * half_width))
        add(up, (position[0] - up[0] * half_height,
                 position[1] - up[1] * half_height,
                 position[2] - up[2] * half_height))
        add((-up[0], -up[1], -up[2]),
            (position[0] + up[0] * half_height,
             position[1] + up[1] * half_height,
             position[2] + up[2] * half_height))

    near_point = (position[0] + forward[0] * low,
                  position[1] + forward[1] * low,
                  position[2] + forward[2] * low)
    far_point = (position[0] + forward[0] * high,
                 position[1] + forward[1] * high,
                 position[2] + forward[2] * high)
    add(forward, near_point)
    add((-forward[0], -forward[1], -forward[2]), far_point)
    return tuple(planes), False


def _make_view(view_id: str, camera: object) -> dict:
    normalized = _validate_camera(camera)
    planes, empty = _build_planes(normalized)
    return {
        "view_id": view_id,
        "kind": normalized["kind"],
        "key": _camera_key(normalized),
        "camera": normalized,
        "planes": planes,
        "empty": empty,
    }


def _validate_bounds(bounds: object) -> tuple[
        tuple[float, float, float], tuple[float, float, float]]:
    code = "CULL_BOUNDS_INVALID"
    if not isinstance(bounds, dict):
        raise CullError(code, "bounds must be a dictionary")
    if "min" not in bounds or "max" not in bounds:
        raise CullError(code, "bounds requires min and max")
    minimum = _vec3(bounds["min"], code, "min")
    maximum = _vec3(bounds["max"], code, "max")
    for axis in range(3):
        if minimum[axis] > maximum[axis]:
            raise CullError(code, f"min exceeds max on axis {axis}")
    return minimum, maximum


def _aabb_outside(bounds: tuple[tuple[float, float, float],
                                tuple[float, float, float]],
                  planes: tuple[tuple[float, float, float, float], ...],
                  empty: bool) -> bool:
    if empty:
        return True

    minimum, maximum = bounds
    center = (
        (minimum[0] + maximum[0]) * 0.5,
        (minimum[1] + maximum[1]) * 0.5,
        (minimum[2] + maximum[2]) * 0.5,
    )
    extent = (
        (maximum[0] - minimum[0]) * 0.5,
        (maximum[1] - minimum[1]) * 0.5,
        (maximum[2] - minimum[2]) * 0.5,
    )

    for nx, ny, nz, d in planes:
        distance = nx * center[0] + ny * center[1] + nz * center[2] + d
        radius = abs(nx) * extent[0] + abs(ny) * extent[1] + abs(nz) * extent[2]
        scale = (abs(center[0]) + abs(center[1]) + abs(center[2])
                 + radius)
        tolerance = 1e-9 + 1e-12 * scale
        if distance + radius < -tolerance:
            return True
    return False


def _camera_out(camera: dict) -> dict:
    result = {
        "kind": camera["kind"],
        "position": list(camera["position"]),
        "target": list(camera["target"]),
        "up": list(camera["up"]),
        "aspect": camera["aspect"],
        "near": camera["near"],
        "far": camera["far"],
        "layers": camera["layers"],
    }
    if camera["kind"] == "perspective":
        result["fov_deg"] = camera["fov_deg"]
    else:
        result["half_height"] = camera["half_height"]
    return result


class MultiviewCullPlanner:
    """Cull a scene once for several cameras and expose per-view visibility."""

    def __init__(self, *, max_views: int = 8,
                 max_objects: int = 200000) -> None:
        self.max_views = max_views
        self.max_objects = max_objects
        self._lock = threading.RLock()
        self._views: dict[str, dict] = {}
        self._objects: dict[str, tuple[
            tuple[float, float, float],
            tuple[float, float, float],
            int,
        ]] = {}
        self._culls = 0
        self._instances = 0

    def add_view(self, view_id: str, camera: dict) -> dict:
        view_id = _normalize_id(view_id)
        with self._lock:
            if view_id in self._views:
                raise CullError("CULL_VIEW_DUPLICATE", view_id)
            if len(self._views) >= self.max_views:
                raise CullError("CULL_VIEW_LIMIT", len(self._views))
            view = _make_view(view_id, camera)
            self._views[view_id] = view
            return {
                "view_id": view_id,
                "kind": view["kind"],
                "key": view["key"],
            }

    def remove_view(self, view_id: str) -> bool:
        view_id = _normalize_id(view_id)
        with self._lock:
            if view_id not in self._views:
                raise CullError("CULL_VIEW_UNKNOWN", view_id)
            del self._views[view_id]
            return True

    def set_object(self, obj_id: str, bounds: dict, layers: int = 1) -> dict:
        obj_id = _normalize_id(obj_id)
        minimum, maximum = _validate_bounds(bounds)
        layers = _nonnegative_int(layers, "CULL_BOUNDS_INVALID", "layers")
        with self._lock:
            replaced = obj_id in self._objects
            if not replaced and len(self._objects) >= self.max_objects:
                raise CullError("CULL_OBJECT_LIMIT", len(self._objects))
            self._objects[obj_id] = (minimum, maximum, layers)
            return {
                "obj_id": obj_id,
                "layers": layers,
                "replaced": replaced,
            }

    def remove_object(self, obj_id: str) -> bool:
        obj_id = _normalize_id(obj_id)
        with self._lock:
            if obj_id not in self._objects:
                raise CullError("CULL_OBJECT_UNKNOWN", obj_id)
            del self._objects[obj_id]
            return True

    def views(self) -> list:
        with self._lock:
            return [
                {
                    "view_id": view_id,
                    "kind": self._views[view_id]["kind"],
                    "key": self._views[view_id]["key"],
                }
                for view_id in sorted(self._views)
            ]

    def objects(self) -> list:
        with self._lock:
            result = []
            for obj_id in sorted(self._objects):
                minimum, maximum, layers = self._objects[obj_id]
                result.append({
                    "obj_id": obj_id,
                    "bounds": {
                        "min": list(minimum),
                        "max": list(maximum),
                    },
                    "layers": layers,
                })
            return result

    def cull(self) -> dict:
        with self._lock:
            views = sorted(self._views.values(),
                           key=lambda view: view["view_id"])
            object_ids = sorted(self._objects)
            masks = {obj_id: 0 for obj_id in object_ids}

            unique_views: dict[str, dict] = {}
            for view in views:
                unique_views.setdefault(view["key"], view)

            visible_by_key: dict[str, list[str]] = {}
            for key, view in unique_views.items():
                camera_layers = view["camera"]["layers"]
                planes = view["planes"]
                empty = view["empty"]
                visible: list[str] = []
                for obj_id in object_ids:
                    minimum, maximum, layers = self._objects[obj_id]
                    if layers & camera_layers == 0:
                        continue
                    bounds = (minimum, maximum)
                    if not _aabb_outside(bounds, planes, empty):
                        visible.append(obj_id)
                visible_by_key[key] = visible

            output_views = []
            total_visible = 0
            for index, view in enumerate(views):
                visible = list(visible_by_key[view["key"]])
                output_views.append({
                    "view_id": view["view_id"],
                    "objects": visible,
                })
                bit = 1 << index
                for obj_id in visible:
                    masks[obj_id] |= bit
                total_visible += len(visible)

            self._culls += 1
            self._instances += total_visible
            return {
                "views": output_views,
                "masks": masks,
                "visible": total_visible,
            }

    def lod_for(self, obj_id: str, view_id: str) -> int:
        obj_id = _normalize_id(obj_id)
        view_id = _normalize_id(view_id)
        with self._lock:
            if obj_id not in self._objects:
                raise CullError("CULL_OBJECT_UNKNOWN", obj_id)
            if view_id not in self._views:
                raise CullError("CULL_VIEW_UNKNOWN", view_id)
            minimum, maximum, _ = self._objects[obj_id]
            camera = self._views[view_id]["camera"]

        radius = 0.5 * math.sqrt(
            (maximum[0] - minimum[0]) ** 2
            + (maximum[1] - minimum[1]) ** 2
            + (maximum[2] - minimum[2]) ** 2
        )

        if camera["kind"] == "perspective":
            center = (
                (minimum[0] + maximum[0]) * 0.5,
                (minimum[1] + maximum[1]) * 0.5,
                (minimum[2] + maximum[2]) * 0.5,
            )
            distance = _length(_sub(center, camera["position"]))
            denominator = distance * camera["_tan_half_fov"]
            if denominator <= 0.0:
                ratio = math.inf
            else:
                ratio = radius / denominator
        else:
            ratio = radius / camera["half_height"]

        if ratio >= 0.5:
            return 0
        if ratio >= 0.2:
            return 1
        if ratio >= 0.05:
            return 2
        return 3

    def stable_views(self) -> list:
        with self._lock:
            groups: dict[str, list[str]] = {}
            for view_id, view in self._views.items():
                groups.setdefault(view["key"], []).append(view_id)
            result = [sorted(group) for group in groups.values()]
            result.sort(key=lambda group: group[0])
            return result

    def view_key(self, view_id: str) -> str:
        view_id = _normalize_id(view_id)
        with self._lock:
            if view_id not in self._views:
                raise CullError("CULL_VIEW_UNKNOWN", view_id)
            return self._views[view_id]["key"]

    def stats(self) -> dict:
        with self._lock:
            return {
                "views": len(self._views),
                "objects": len(self._objects),
                "culls": self._culls,
                "instances": self._instances,
            }

    def snapshot(self) -> dict:
        with self._lock:
            cameras = {
                view_id: _camera_out(self._views[view_id]["camera"])
                for view_id in sorted(self._views)
            }
            objects = []
            for obj_id in sorted(self._objects):
                minimum, maximum, layers = self._objects[obj_id]
                objects.append({
                    "obj_id": obj_id,
                    "bounds": {
                        "min": list(minimum),
                        "max": list(maximum),
                    },
                    "layers": layers,
                })
            state = {
                "views": self.views(),
                "cameras": cameras,
                "objects": objects,
                "culls": self._culls,
                "instances": self._instances,
            }
            return copy.deepcopy(state)

    def restore(self, state: dict) -> None:
        try:
            if not isinstance(state, dict):
                raise ValueError("snapshot must be a dictionary")

            views_data = state.get("views")
            objects_data = state.get("objects")
            if (not isinstance(views_data, list)
                    or not isinstance(objects_data, list)):
                raise ValueError("views and objects must be lists")

            cameras_data = state.get("cameras", {})
            if cameras_data is None:
                cameras_data = {}
            if not isinstance(cameras_data, dict):
                raise ValueError("cameras must be a dictionary")

            new_views: dict[str, dict] = {}
            if len(views_data) > self.max_views:
                raise ValueError("too many views")

            for entry in views_data:
                if not isinstance(entry, dict):
                    raise ValueError("view entry must be a dictionary")
                view_id = _normalize_id(entry.get("view_id"))
                if view_id in new_views:
                    raise ValueError("duplicate view id")
                if "camera" in entry:
                    camera_data = entry["camera"]
                elif view_id in cameras_data:
                    camera_data = cameras_data[view_id]
                else:
                    raise ValueError("missing camera data")
                view = _make_view(view_id, camera_data)
                if "kind" in entry and entry["kind"] != view["kind"]:
                    raise ValueError("view kind does not match camera")
                if "key" in entry and entry["key"] != view["key"]:
                    raise ValueError("view key does not match camera")
                new_views[view_id] = view

            new_objects: dict[str, tuple[
                tuple[float, float, float],
                tuple[float, float, float],
                int,
            ]] = {}
            if len(objects_data) > self.max_objects:
                raise ValueError("too many objects")

            for entry in objects_data:
                if not isinstance(entry, dict):
                    raise ValueError("object entry must be a dictionary")
                obj_id = _normalize_id(entry.get("obj_id"))
                if obj_id in new_objects:
                    raise ValueError("duplicate object id")
                if "bounds" not in entry:
                    raise ValueError("object bounds are missing")
                minimum, maximum = _validate_bounds(entry["bounds"])
                layers = _nonnegative_int(entry.get("layers", 1),
                                          "CULL_BOUNDS_INVALID", "layers")
                new_objects[obj_id] = (minimum, maximum, layers)

            culls = state.get("culls", 0)
            instances = state.get("instances", 0)
            if (isinstance(culls, bool) or not isinstance(culls, int)
                    or culls < 0):
                raise ValueError("culls must be a non-negative integer")
            if (isinstance(instances, bool) or not isinstance(instances, int)
                    or instances < 0):
                raise ValueError("instances must be a non-negative integer")

            with self._lock:
                self._views = new_views
                self._objects = new_objects
                self._culls = culls
                self._instances = instances
        except CullError as exc:
            raise CullError("CULL_SNAPSHOT_INVALID", exc.detail) from None
        except Exception as exc:
            raise CullError("CULL_SNAPSHOT_INVALID", str(exc)) from None

    def reset(self) -> None:
        with self._lock:
            self._views.clear()
            self._objects.clear()
            self._culls = 0
            self._instances = 0
