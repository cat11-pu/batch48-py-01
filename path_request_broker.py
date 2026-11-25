"""Budgeted path request broker with versioned cache and region invalidation."""

from __future__ import annotations

import threading
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any


class PathError(ValueError):
    """Domain failure carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


_STAT_NAMES = (
    "requests",
    "steps",
    "cache_hits",
    "cache_misses",
    "completed",
    "cancelled",
)
_TERMINAL_STATUSES = {"done", "unreachable", "cancelled"}
_ALL_STATUSES = {"pending"} | _TERMINAL_STATUSES
_NEIGHBORS = ((0, -1), (1, 0), (0, 1), (-1, 0))
_SNAPSHOT_FORMAT = "path-request-broker"
_SNAPSHOT_VERSION = 1


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _cell(value: object, code: str, detail: object = None):
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise PathError(code, detail if detail is not None else value)
    x, y = value
    if not _is_int(x) or not _is_int(y):
        raise PathError(code, detail if detail is not None else value)
    return (x, y)


@dataclass(frozen=True)
class _Grid:
    width: int
    height: int
    blocked: frozenset


@dataclass
class _Ticket:
    ticket: str
    start: tuple
    goal: tuple
    grid_version: Any
    status: str = "pending"
    expanded: int = 0
    worker: Any = None
    worker_ident: int | None = None
    queue: deque = field(default_factory=deque)
    visited: set = field(default_factory=set)
    parents: dict = field(default_factory=dict)
    path: tuple | None = None


class PathBroker:
    """Queues path requests and advances them within a per call node budget."""

    def __init__(self, *, max_open: int = 64, cache_limit: int = 128) -> None:
        self.max_open = max(0, int(max_open))
        self.cache_limit = max(0, int(cache_limit))
        self._lock = threading.RLock()
        self._grids: dict = {}
        self._tickets: OrderedDict[str, _Ticket] = OrderedDict()
        self._pending: list[str] = []
        self._cache: OrderedDict = OrderedDict()
        self._next_ticket = 1
        self._stats = {name: 0 for name in _STAT_NAMES}

    def set_grid(self, version: int, *, width: int, height: int,
                 blocked=()) -> dict:
        if not _is_int(width) or not _is_int(height) or width <= 0 or height <= 0:
            raise PathError("PB_INVALID_GRID", (width, height))
        try:
            raw_blocked = list(blocked)
        except TypeError as exc:
            raise PathError("PB_INVALID_GRID", blocked) from exc
        blocked_cells = set()
        for item in raw_blocked:
            cell = _cell(item, "PB_INVALID_GRID", item)
            if not (0 <= cell[0] < width and 0 <= cell[1] < height):
                raise PathError("PB_INVALID_GRID", cell)
            blocked_cells.add(cell)
        try:
            hash(version)
        except TypeError as exc:
            raise PathError("PB_INVALID_GRID", version) from exc

        grid = _Grid(width, height, frozenset(blocked_cells))
        with self._lock:
            current = self._grids.get(version)
            if current is not None:
                if current != grid:
                    raise PathError("PB_GRID_CONFLICT", version)
                return {
                    "version": version,
                    "width": width,
                    "height": height,
                    "blocked": len(blocked_cells),
                }
            self._grids[version] = grid
        return {
            "version": version,
            "width": width,
            "height": height,
            "blocked": len(blocked_cells),
        }

    def request(self, start, goal, grid_version: int, *, region=None) -> dict:
        with self._lock:
            grid = self._grid_for(grid_version)
            start_cell = _cell(start, "PB_INVALID_CELL", start)
            goal_cell = _cell(goal, "PB_INVALID_CELL", goal)
            self._check_cell_bounds(start_cell, grid)
            self._check_cell_bounds(goal_cell, grid)
            key = (start_cell, goal_cell, grid_version)

            if key in self._cache:
                path = self._cache.pop(key)
                self._cache[key] = path
                ticket = self._new_cached_ticket(
                    start_cell, goal_cell, grid_version, path
                )
                self._stats["requests"] += 1
                self._stats["cache_hits"] += 1
                self._stats["completed"] += 1
                return self._request_response(ticket, True)

            if len(self._pending) >= self.max_open:
                raise PathError("PB_QUEUE_FULL", len(self._pending))

            ticket = self._new_pending_ticket(start_cell, goal_cell, grid_version)
            self._stats["requests"] += 1
            self._stats["cache_misses"] += 1
            return self._request_response(ticket, False)

    def step(self, ticket: str, budget_nodes: int, *, worker: str = "main") -> dict:
        with self._lock:
            current = self._ticket_for(ticket)
            if not _is_int(budget_nodes) or budget_nodes < 0:
                raise PathError("PB_INVALID_BUDGET", budget_nodes)
            if current.status == "cancelled":
                raise PathError("PB_CANCELLED", ticket)

            if current.status in ("done", "unreachable"):
                self._stats["steps"] += 1
                return self._step_response(current)

            ident = threading.get_ident()
            if current.worker is None:
                current.worker = worker
                current.worker_ident = ident
            elif current.worker != worker:
                raise PathError("PB_BUSY", ticket)
            elif current.worker_ident is None:
                current.worker_ident = ident
            elif current.worker_ident != ident:
                raise PathError("PB_BUSY", ticket)

            self._stats["steps"] += 1
            if budget_nodes == 0:
                return self._step_response(current)

            grid = self._grids[current.grid_version]
            used = 0
            while used < budget_nodes:
                if not current.queue:
                    self._mark_unreachable(current)
                    return self._step_response(current)

                cell = current.queue.popleft()
                current.expanded += 1
                used += 1

                if cell == current.goal:
                    self._mark_done(current)
                    return self._step_response(current)

                x, y = cell
                for dx, dy in _NEIGHBORS:
                    neighbor = (x + dx, y + dy)
                    if not (
                        0 <= neighbor[0] < grid.width
                        and 0 <= neighbor[1] < grid.height
                    ):
                        continue
                    if neighbor in grid.blocked or neighbor in current.visited:
                        continue
                    current.visited.add(neighbor)
                    current.parents[neighbor] = cell
                    current.queue.append(neighbor)

            if not current.queue:
                self._mark_unreachable(current)
            return self._step_response(current)

    def cancel(self, ticket: str) -> dict:
        with self._lock:
            current = self._ticket_for(ticket)
            if current.status == "cancelled":
                return {"ticket": ticket, "status": "cancelled"}
            if current.status in ("done", "unreachable"):
                return {"ticket": ticket, "status": current.status}
            current.status = "cancelled"
            current.path = None
            self._remove_pending(ticket)
            self._stats["cancelled"] += 1
            return {"ticket": ticket, "status": "cancelled"}

    def result(self, ticket: str) -> dict:
        with self._lock:
            current = self._ticket_for(ticket)
            if current.status == "cancelled":
                raise PathError("PB_CANCELLED", ticket)
            if current.status == "unreachable":
                raise PathError("PB_UNREACHABLE", ticket)
            if current.status != "done":
                raise PathError("PB_NOT_READY", ticket)
            return {
                "status": "done",
                "path": list(current.path or ()),
                "expanded": current.expanded,
            }

    def invalidate(self, region) -> int:
        area = self._region_cells(region)
        with self._lock:
            removed = 0
            for key, path in list(self._cache.items()):
                if any(cell in area for cell in path):
                    del self._cache[key]
                    removed += 1
            return removed

    def pending(self) -> list:
        with self._lock:
            return list(self._pending)

    def ticket_state(self, ticket: str) -> dict:
        with self._lock:
            current = self._ticket_for(ticket)
            return {"status": current.status, "expanded": current.expanded}

    def cache_info(self) -> dict:
        with self._lock:
            return {"entries": len(self._cache)}

    def snapshot(self) -> dict:
        with self._lock:
            grids = {}
            for version, grid in self._grids.items():
                grids[version] = {
                    "width": grid.width,
                    "height": grid.height,
                    "blocked": [list(cell) for cell in sorted(grid.blocked)],
                }
            cache = []
            for (start, goal, version), path in self._cache.items():
                cache.append(
                    {
                        "start": list(start),
                        "goal": list(goal),
                        "grid_version": version,
                        "path": [list(cell) for cell in path],
                    }
                )
            return {
                "format": _SNAPSHOT_FORMAT,
                "snapshot_version": _SNAPSHOT_VERSION,
                "grids": grids,
                "next_ticket": self._next_ticket,
                "tickets": [
                    self._snapshot_ticket(current)
                    for current in self._tickets.values()
                ],
                "pending": list(self._pending),
                "cache": cache,
                "stats": dict(self._stats),
            }

    def restore(self, state: dict) -> None:
        parsed = self._parse_snapshot(state)
        tickets = parsed[1]
        for current in tickets.values():
            if current.worker is not None:
                current.worker_ident = None
        with self._lock:
            (
                self._grids,
                self._tickets,
                self._pending,
                self._cache,
                self._next_ticket,
                self._stats,
            ) = parsed

    def stats(self) -> dict:
        with self._lock:
            return {name: int(self._stats[name]) for name in _STAT_NAMES}

    def reset_stats(self) -> None:
        with self._lock:
            self._stats = {name: 0 for name in _STAT_NAMES}

    def _grid_for(self, version) -> _Grid:
        try:
            grid = self._grids.get(version)
        except TypeError as exc:
            raise PathError("PB_UNKNOWN_GRID", version) from exc
        if grid is None:
            raise PathError("PB_UNKNOWN_GRID", version)
        return grid

    def _check_cell_bounds(self, cell: tuple, grid: _Grid) -> None:
        if not (0 <= cell[0] < grid.width and 0 <= cell[1] < grid.height):
            raise PathError("PB_INVALID_CELL", cell)

    def _ticket_for(self, ticket: str) -> _Ticket:
        if not isinstance(ticket, str):
            raise PathError("PB_UNKNOWN_TICKET", ticket)
        current = self._tickets.get(ticket)
        if current is None:
            raise PathError("PB_UNKNOWN_TICKET", ticket)
        return current

    def _new_ticket_id(self) -> str:
        while True:
            ticket = f"t{self._next_ticket}"
            self._next_ticket += 1
            if ticket not in self._tickets:
                return ticket

    def _new_pending_ticket(self, start: tuple, goal: tuple, version) -> _Ticket:
        ticket = _Ticket(
            ticket=self._new_ticket_id(),
            start=start,
            goal=goal,
            grid_version=version,
            queue=deque([start]),
            visited={start},
            parents={start: None},
        )
        self._tickets[ticket.ticket] = ticket
        self._pending.append(ticket.ticket)
        return ticket

    def _new_cached_ticket(self, start: tuple, goal: tuple, version,
                           path: tuple) -> _Ticket:
        ticket = _Ticket(
            ticket=self._new_ticket_id(),
            start=start,
            goal=goal,
            grid_version=version,
            status="done",
            path=tuple(path),
        )
        self._tickets[ticket.ticket] = ticket
        return ticket

    @staticmethod
    def _request_response(ticket: _Ticket, cache_hit: bool) -> dict:
        return {
            "ticket": ticket.ticket,
            "start": ticket.start,
            "goal": ticket.goal,
            "grid_version": ticket.grid_version,
            "status": ticket.status,
            "cache_hit": cache_hit,
        }

    @staticmethod
    def _step_response(ticket: _Ticket) -> dict:
        return {
            "status": ticket.status,
            "path": list(ticket.path or ()),
            "expanded": ticket.expanded,
        }

    def _mark_done(self, ticket: _Ticket) -> None:
        path = []
        cell = ticket.goal
        while cell is not None:
            path.append(cell)
            cell = ticket.parents.get(cell)
        path.reverse()
        ticket.path = tuple(path)
        ticket.status = "done"
        self._remove_pending(ticket.ticket)
        self._cache_store(ticket)
        self._stats["completed"] += 1

    def _mark_unreachable(self, ticket: _Ticket) -> None:
        ticket.path = None
        ticket.status = "unreachable"
        self._remove_pending(ticket.ticket)

    def _cache_store(self, ticket: _Ticket) -> None:
        if self.cache_limit <= 0:
            return
        key = (ticket.start, ticket.goal, ticket.grid_version)
        self._cache.pop(key, None)
        self._cache[key] = tuple(ticket.path or ())
        while len(self._cache) > self.cache_limit:
            self._cache.popitem(last=False)

    def _remove_pending(self, ticket: str) -> None:
        try:
            self._pending.remove(ticket)
        except ValueError:
            pass

    def _region_cells(self, region) -> frozenset:
        if region is None or isinstance(region, (str, bytes, bytearray, dict)):
            raise PathError("PB_INVALID_REGION", region)
        try:
            items = list(region)
        except TypeError as exc:
            raise PathError("PB_INVALID_REGION", region) from exc

        if not items:
            return frozenset()
        if len(items) == 2 and all(_is_int(item) for item in items):
            return frozenset({(items[0], items[1])})
        if len(items) == 4 and all(_is_int(item) for item in items):
            x1, y1, x2, y2 = items
            left, right = sorted((x1, x2))
            top, bottom = sorted((y1, y2))
            return frozenset(
                (x, y)
                for x in range(left, right + 1)
                for y in range(top, bottom + 1)
            )

        cells = set()
        for item in items:
            cells.add(_cell(item, "PB_INVALID_REGION", item))
        return frozenset(cells)

    def _snapshot_ticket(self, ticket: _Ticket) -> dict:
        parents = []
        for cell, parent in sorted(ticket.parents.items()):
            parents.append(
                {
                    "cell": list(cell),
                    "parent": list(parent) if parent is not None else None,
                }
            )
        return {
            "ticket": ticket.ticket,
            "start": list(ticket.start),
            "goal": list(ticket.goal),
            "grid_version": ticket.grid_version,
            "status": ticket.status,
            "expanded": ticket.expanded,
            "worker": ticket.worker,
            "worker_ident": ticket.worker_ident,
            "queue": [list(cell) for cell in ticket.queue],
            "visited": [list(cell) for cell in sorted(ticket.visited)],
            "parents": parents,
            "path": [list(cell) for cell in ticket.path]
            if ticket.path is not None
            else None,
        }

    def _parse_snapshot(self, state: dict):
        invalid = "PB_SNAPSHOT_INVALID"
        if not isinstance(state, dict):
            raise PathError(invalid, state)
        if (
            state.get("format") != _SNAPSHOT_FORMAT
            or state.get("snapshot_version") != _SNAPSHOT_VERSION
        ):
            raise PathError(invalid, "format")

        raw_grids = state.get("grids")
        if not isinstance(raw_grids, dict):
            raise PathError(invalid, "grids")
        grids = {}
        for version, raw_grid in raw_grids.items():
            try:
                hash(version)
            except TypeError as exc:
                raise PathError(invalid, "grid version") from exc
            if not isinstance(raw_grid, dict):
                raise PathError(invalid, version)
            width = raw_grid.get("width")
            height = raw_grid.get("height")
            raw_blocked = raw_grid.get("blocked")
            if (
                not _is_int(width)
                or not _is_int(height)
                or width <= 0
                or height <= 0
                or not isinstance(raw_blocked, (list, tuple))
            ):
                raise PathError(invalid, version)
            blocked = set()
            for raw_cell in raw_blocked:
                cell = _cell(raw_cell, invalid, raw_cell)
                if not (0 <= cell[0] < width and 0 <= cell[1] < height):
                    raise PathError(invalid, raw_cell)
                blocked.add(cell)
            grids[version] = _Grid(width, height, frozenset(blocked))

        next_ticket = state.get("next_ticket")
        if not _is_int(next_ticket) or next_ticket < 1:
            raise PathError(invalid, "next_ticket")

        raw_tickets = state.get("tickets")
        if not isinstance(raw_tickets, (list, tuple)):
            raise PathError(invalid, "tickets")
        tickets: OrderedDict[str, _Ticket] = OrderedDict()
        for raw in raw_tickets:
            ticket = self._parse_snapshot_ticket(raw, grids)
            if ticket.ticket in tickets:
                raise PathError(invalid, ticket.ticket)
            tickets[ticket.ticket] = ticket

        raw_pending = state.get("pending")
        if not isinstance(raw_pending, (list, tuple)):
            raise PathError(invalid, "pending")
        pending = []
        for ticket_id in raw_pending:
            if (
                not isinstance(ticket_id, str)
                or ticket_id in pending
                or ticket_id not in tickets
                or tickets[ticket_id].status != "pending"
            ):
                raise PathError(invalid, ticket_id)
            pending.append(ticket_id)
        actual_pending = [
            ticket_id
            for ticket_id, ticket in tickets.items()
            if ticket.status == "pending"
        ]
        if pending != actual_pending:
            raise PathError(invalid, "pending")

        raw_cache = state.get("cache")
        if not isinstance(raw_cache, (list, tuple)):
            raise PathError(invalid, "cache")
        cache: OrderedDict = OrderedDict()
        for raw_entry in raw_cache:
            if not isinstance(raw_entry, dict):
                raise PathError(invalid, "cache entry")
            start = _cell(raw_entry.get("start"), invalid, "cache start")
            goal = _cell(raw_entry.get("goal"), invalid, "cache goal")
            version = raw_entry.get("grid_version")
            try:
                grid = grids.get(version)
            except TypeError as exc:
                raise PathError(invalid, "cache grid version") from exc
            if grid is None:
                raise PathError(invalid, "cache grid version")
            self._check_snapshot_cell(start, grid)
            self._check_snapshot_cell(goal, grid)
            raw_path = raw_entry.get("path")
            if not isinstance(raw_path, (list, tuple)) or not raw_path:
                raise PathError(invalid, "cache path")
            path = tuple(
                _cell(raw_cell, invalid, raw_cell) for raw_cell in raw_path
            )
            for cell in path:
                self._check_snapshot_cell(cell, grid)
            if path[0] != start or path[-1] != goal:
                raise PathError(invalid, "cache path")
            key = (start, goal, version)
            if key in cache:
                raise PathError(invalid, "duplicate cache key")
            cache[key] = path
        while len(cache) > self.cache_limit:
            cache.popitem(last=False)

        raw_stats = state.get("stats")
        if not isinstance(raw_stats, dict):
            raise PathError(invalid, "stats")
        stats = {}
        for name in _STAT_NAMES:
            value = raw_stats.get(name)
            if not _is_int(value) or value < 0:
                raise PathError(invalid, name)
            stats[name] = value

        return grids, tickets, pending, cache, next_ticket, stats

    def _parse_snapshot_ticket(self, raw, grids: dict) -> _Ticket:
        invalid = "PB_SNAPSHOT_INVALID"
        if not isinstance(raw, dict):
            raise PathError(invalid, "ticket")
        ticket_id = raw.get("ticket")
        if not isinstance(ticket_id, str) or not ticket_id:
            raise PathError(invalid, "ticket id")
        start = _cell(raw.get("start"), invalid, "start")
        goal = _cell(raw.get("goal"), invalid, "goal")
        version = raw.get("grid_version")
        try:
            grid = grids.get(version)
        except TypeError as exc:
            raise PathError(invalid, "grid version") from exc
        if grid is None:
            raise PathError(invalid, "grid version")
        self._check_snapshot_cell(start, grid)
        self._check_snapshot_cell(goal, grid)

        status = raw.get("status")
        if status not in _ALL_STATUSES:
            raise PathError(invalid, "status")
        expanded = raw.get("expanded")
        if not _is_int(expanded) or expanded < 0:
            raise PathError(invalid, "expanded")
        worker = raw.get("worker")
        worker_ident = raw.get("worker_ident")
        if worker is None:
            if worker_ident is not None:
                raise PathError(invalid, "worker")
        elif not _is_int(worker_ident):
            raise PathError(invalid, "worker ident")

        raw_queue = raw.get("queue")
        raw_visited = raw.get("visited")
        raw_parents = raw.get("parents")
        if not isinstance(raw_queue, (list, tuple)):
            raise PathError(invalid, "queue")
        if not isinstance(raw_visited, (list, tuple)):
            raise PathError(invalid, "visited")
        if not isinstance(raw_parents, (list, tuple)):
            raise PathError(invalid, "parents")

        queue = deque()
        for raw_cell in raw_queue:
            cell = _cell(raw_cell, invalid, raw_cell)
            self._check_snapshot_cell(cell, grid)
            queue.append(cell)

        visited = set()
        for raw_cell in raw_visited:
            cell = _cell(raw_cell, invalid, raw_cell)
            self._check_snapshot_cell(cell, grid)
            if cell in visited:
                raise PathError(invalid, "duplicate visited")
            visited.add(cell)

        parents = {}
        for entry in raw_parents:
            if not isinstance(entry, dict):
                raise PathError(invalid, "parent")
            cell = _cell(entry.get("cell"), invalid, "parent cell")
            self._check_snapshot_cell(cell, grid)
            raw_parent = entry.get("parent")
            parent = (
                None
                if raw_parent is None
                else _cell(raw_parent, invalid, "parent value")
            )
            if parent is not None:
                self._check_snapshot_cell(parent, grid)
            if cell in parents:
                raise PathError(invalid, "duplicate parent")
            parents[cell] = parent
        if any(cell not in visited for cell in parents):
            raise PathError(invalid, "parent not visited")
        if any(
            parent is not None and parent not in visited
            for parent in parents.values()
        ):
            raise PathError(invalid, "parent not visited")

        raw_path = raw.get("path")
        path = None
        if raw_path is not None:
            if not isinstance(raw_path, (list, tuple)) or not raw_path:
                raise PathError(invalid, "path")
            path = tuple(_cell(raw_cell, invalid, raw_cell) for raw_cell in raw_path)
            for cell in path:
                self._check_snapshot_cell(cell, grid)
            if status != "done" or path[0] != start or path[-1] != goal:
                raise PathError(invalid, "path")
        elif status == "done":
            raise PathError(invalid, "missing path")

        return _Ticket(
            ticket=ticket_id,
            start=start,
            goal=goal,
            grid_version=version,
            status=status,
            expanded=expanded,
            worker=worker,
            worker_ident=worker_ident,
            queue=queue,
            visited=visited,
            parents=parents,
            path=path,
        )

    @staticmethod
    def _check_snapshot_cell(cell: tuple, grid: _Grid) -> None:
        if not (0 <= cell[0] < grid.width and 0 <= cell[1] < grid.height):
            raise PathError("PB_SNAPSHOT_INVALID", cell)
