"""Weighted loot table roller with guaranteed drops and pity counters."""

from __future__ import annotations

import copy
import random
import threading


class DropTableError(Exception):
    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class DropRoller:
    """Register loot tables, settle kills once and track pity per table."""

    def __init__(self, *, seed: int = 1337, max_rolls_per_kill: int = 6,
                 pity_threshold: int = 8) -> None:
        self.seed = int(seed)
        self.max_rolls_per_kill = int(max_rolls_per_kill)
        self.pity_threshold = int(pity_threshold)
        self._rng = random.Random(self.seed)
        self._lock = threading.RLock()
        self._tables = {}
        self._pity = {}
        self._kills = {}
        self._cache = {}
        self._stats = {
            "kills": 0,
            "drops": 0,
            "guaranteed": 0,
            "pity_triggers": 0,
        }

    def _invalid(self, detail):
        return DropTableError("DROP_TABLE_INVALID", detail)

    def _validate_spec(self, spec):
        if not isinstance(spec, dict):
            raise self._invalid("table spec must be a mapping")
        for field in ("guaranteed", "pools"):
            if field not in spec or not isinstance(spec[field], list):
                raise self._invalid("missing or non-list field: %s" % field)

        guaranteed = []
        for item in spec["guaranteed"]:
            if not isinstance(item, dict):
                raise self._invalid("guaranteed entry must be a mapping")
            for field in ("key", "min", "max"):
                if field not in item:
                    raise self._invalid("guaranteed entry missing field: %s" % field)
            lo, hi = item["min"], item["max"]
            if not isinstance(lo, int) or not isinstance(hi, int) or lo > hi or lo < 0:
                raise self._invalid("guaranteed entry has invalid min/max: %r" % item.get("key"))
            guaranteed.append({"key": item["key"], "min": lo, "max": hi})

        total_picks = 0
        pools = []
        for pool in spec["pools"]:
            if not isinstance(pool, dict):
                raise self._invalid("pool must be a mapping")
            for field in ("name", "picks", "entries"):
                if field not in pool:
                    raise self._invalid("pool missing field: %s" % field)
            picks = pool["picks"]
            if not isinstance(picks, int) or picks < 0:
                raise self._invalid("pool picks must be a non-negative integer")
            if not isinstance(pool["entries"], list):
                raise self._invalid("pool entries must be a list")

            entries = []
            for entry in pool["entries"]:
                if not isinstance(entry, dict):
                    raise self._invalid("pool entry must be a mapping")
                for field in ("key", "weight", "min", "max"):
                    if field not in entry:
                        raise self._invalid("pool entry missing field: %s" % field)
                weight = entry["weight"]
                lo, hi = entry["min"], entry["max"]
                if not isinstance(weight, int) or weight < 0:
                    raise self._invalid("entry weight must be a non-negative integer: %r" % entry.get("key"))
                if not isinstance(lo, int) or not isinstance(hi, int) or lo > hi or lo < 0:
                    raise self._invalid("entry has invalid min/max: %r" % entry.get("key"))
                if weight == 0:
                    continue
                entries.append({"key": entry["key"], "weight": weight, "min": lo, "max": hi})

            if picks > 0 and not entries:
                raise self._invalid("pool %r requires positive-weight entries" % pool["name"])
            total_picks += picks
            pools.append({"name": pool["name"], "picks": picks, "entries": entries})

        pity = None
        if "pity" in spec and spec["pity"] is not None:
            pity_spec = spec["pity"]
            if not isinstance(pity_spec, dict) or "key" not in pity_spec:
                raise self._invalid("pity must define a key")
            threshold = pity_spec.get("threshold", self.pity_threshold)
            if not isinstance(threshold, int) or threshold <= 0:
                raise self._invalid("pity threshold must be a positive integer")
            pity = {"key": pity_spec["key"], "threshold": threshold}

        return {"guaranteed": guaranteed, "pools": pools, "pity": pity}, total_picks

    def RegisterTable(self, table_id, spec):
        with self._lock:
            if table_id in self._tables:
                raise DropTableError("DROP_TABLE_ALREADY_REGISTERED", table_id)
            normalized, total_picks = self._validate_spec(spec)
            if total_picks > self.max_rolls_per_kill:
                raise DropTableError(
                    "DROP_BUDGET_EXCEEDED",
                    {"table": table_id, "budget": total_picks,
                     "max": self.max_rolls_per_kill},
                )
            self._tables[table_id] = normalized
            self._pity[table_id] = {}
            self._kills[table_id] = set()
            self._cache[table_id] = {}
            if normalized["pity"] is not None:
                self._pity[table_id][normalized["pity"]["key"]] = 0
            return {"table_id": table_id, "budget": total_picks}

    def Roll(self, table_id, kill_context, rng=None):
        with self._lock:
            if not isinstance(kill_context, dict) or not kill_context.get("kill_id"):
                raise DropTableError("DROP_KILL_INVALID", kill_context)
            if table_id not in self._tables:
                raise DropTableError("DROP_TABLE_UNKNOWN", table_id)

            kill_id = kill_context["kill_id"]
            cached = self._cache[table_id].get(kill_id)
            if cached is not None:
                return copy.deepcopy(cached)

            table = self._tables[table_id]
            rng = rng if rng is not None else self._rng
            drops = []

            for item in table["guaranteed"]:
                amount = rng.randint(item["min"], item["max"])
                drops.append({"key": item["key"], "amount": amount, "source": "guaranteed"})
                self._stats["guaranteed"] += 1

            for pool in table["pools"]:
                keys = [entry["key"] for entry in pool["entries"]]
                weights = [entry["weight"] for entry in pool["entries"]]
                bounds = {entry["key"]: (entry["min"], entry["max"]) for entry in pool["entries"]}
                for _ in range(pool["picks"]):
                    key = rng.choices(keys, weights=weights, k=1)[0]
                    lo, hi = bounds[key]
                    amount = rng.randint(lo, hi)
                    drops.append({"key": key, "amount": amount,
                                  "source": "pool:%s" % pool["name"]})

            pity = table["pity"]
            if pity is not None:
                counters = self._pity[table_id]
                counters[pity["key"]] += 1
                if counters[pity["key"]] >= pity["threshold"]:
                    drops.append({"key": pity["key"], "amount": 1, "source": "pity"})
                    counters[pity["key"]] = 0
                    self._stats["pity_triggers"] += 1

            self._kills[table_id].add(kill_id)
            self._cache[table_id][kill_id] = drops
            self._stats["kills"] += 1
            self._stats["drops"] += len(drops)
            return copy.deepcopy(drops)

    def Guarantee(self, table_id, key):
        with self._lock:
            if table_id not in self._tables:
                raise DropTableError("DROP_TABLE_UNKNOWN", table_id)
            return any(item["key"] == key for item in self._tables[table_id]["guaranteed"])

    def Pity(self, table_id, key):
        with self._lock:
            if table_id not in self._tables:
                raise DropTableError("DROP_TABLE_UNKNOWN", table_id)
            if key not in self._pity[table_id]:
                raise DropTableError("DROP_PITY_UNKNOWN", key)
            return self._pity[table_id][key]

    def Budget(self, table_id):
        with self._lock:
            if table_id not in self._tables:
                raise DropTableError("DROP_TABLE_UNKNOWN", table_id)
            return sum(pool["picks"] for pool in self._tables[table_id]["pools"])

    def Settled(self, table_id, kill_id):
        with self._lock:
            if table_id not in self._tables:
                raise DropTableError("DROP_TABLE_UNKNOWN", table_id)
            return kill_id in self._kills[table_id]

    def ResetPity(self, table_id, key):
        with self._lock:
            if table_id not in self._tables:
                raise DropTableError("DROP_TABLE_UNKNOWN", table_id)
            if key not in self._pity[table_id]:
                raise DropTableError("DROP_PITY_UNKNOWN", key)
            previous = self._pity[table_id][key]
            self._pity[table_id][key] = 0
            return previous

    def Snapshot(self):
        with self._lock:
            return {
                "seed": self.seed,
                "max_rolls_per_kill": self.max_rolls_per_kill,
                "pity_threshold": self.pity_threshold,
                "rng_state": self._rng.getstate(),
                "tables": copy.deepcopy(self._tables),
                "pity": copy.deepcopy(self._pity),
                "kills": {table_id: set(kill_ids) for table_id, kill_ids in self._kills.items()},
                "cache": copy.deepcopy(self._cache),
                "stats": copy.deepcopy(self._stats),
            }

    def Restore(self, state):
        with self._lock:
            required = ("rng_state", "tables", "pity", "kills", "cache", "stats")
            if not isinstance(state, dict) or any(key not in state for key in required):
                raise DropTableError("DROP_SNAPSHOT_INVALID", "missing snapshot fields")
            try:
                probe = random.Random()
                probe.setstate(state["rng_state"])
            except (TypeError, ValueError):
                raise DropTableError("DROP_SNAPSHOT_INVALID", "invalid rng state")
            if not isinstance(state["tables"], dict) or not isinstance(state["pity"], dict):
                raise DropTableError("DROP_SNAPSHOT_INVALID", "invalid table or pity state")
            if not isinstance(state["kills"], dict) or not isinstance(state["cache"], dict):
                raise DropTableError("DROP_SNAPSHOT_INVALID", "invalid kill cache state")
            if not isinstance(state["stats"], dict):
                raise DropTableError("DROP_SNAPSHOT_INVALID", "invalid stats state")
            if set(state["stats"]) != set(("kills", "drops", "guaranteed", "pity_triggers")):
                raise DropTableError("DROP_SNAPSHOT_INVALID", "invalid stats keys")
            if any(not isinstance(value, int) for value in state["stats"].values()):
                raise DropTableError("DROP_SNAPSHOT_INVALID", "invalid stats values")
            if any(not isinstance(counters, dict) for counters in state["pity"].values()):
                raise DropTableError("DROP_SNAPSHOT_INVALID", "invalid pity counters")
            if any(not isinstance(entries, (set, list, tuple)) for entries in state["kills"].values()):
                raise DropTableError("DROP_SNAPSHOT_INVALID", "invalid settled kills")
            if any(not isinstance(entries, dict) for entries in state["cache"].values()):
                raise DropTableError("DROP_SNAPSHOT_INVALID", "invalid kill cache entries")

            tables = copy.deepcopy(state["tables"])
            pity = copy.deepcopy(state["pity"])
            kills = {table_id: set(kill_ids) for table_id, kill_ids in state["kills"].items()}
            cache = copy.deepcopy(state["cache"])
            stats = copy.deepcopy(state["stats"])

            self._rng.setstate(copy.deepcopy(state["rng_state"]))
            self._tables = tables
            self._pity = pity
            self._kills = kills
            self._cache = cache
            self._stats = stats
            if "seed" in state:
                self.seed = state["seed"]
            if "max_rolls_per_kill" in state:
                self.max_rolls_per_kill = state["max_rolls_per_kill"]
            if "pity_threshold" in state:
                self.pity_threshold = state["pity_threshold"]

    def Stats(self):
        with self._lock:
            return dict(self._stats)
