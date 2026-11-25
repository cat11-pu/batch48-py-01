"""Cluster findings by root cause with manual merge and split traces."""

from __future__ import annotations

import copy
import threading

DEFAULT_MAX_FINDINGS = 200000
DEFAULT_MAX_MEMBERS = 4096
SNAPSHOT_VERSION = 1
_MISSING = object()


class ClusterError(ValueError):
    """Raised for every rejected finding or cluster operation."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


def _root_key(finding):
    return "{}|{}|{}".format(
        finding["rule_id"], finding["sink"], finding["source"]
    )


def _member_sort_key(finding):
    return finding["path"], finding["line"], str(finding["id"])


def _positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _non_negative_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_finding(finding):
    if not isinstance(finding, dict):
        raise ClusterError(
            "FINDING_INVALID", {"reason": "finding must be a dictionary"}
        )

    for field_name in ("rule_id", "path", "sink", "source"):
        value = finding.get(field_name, _MISSING)
        if value is _MISSING:
            raise ClusterError(
                "FINDING_INVALID", {"field": field_name, "reason": "required"}
            )
        if not isinstance(value, str) or not value.strip():
            raise ClusterError(
                "FINDING_INVALID",
                {"field": field_name, "reason": "must be a non-empty string"},
            )

    line = finding.get("line", _MISSING)
    if line is _MISSING or not _positive_int(line):
        raise ClusterError(
            "FINDING_INVALID",
            {"field": "line", "reason": "must be a positive integer"},
        )

    finding_id = finding.get("id", _MISSING)
    if finding_id is not _MISSING and finding_id is not None:
        if not isinstance(finding_id, str) or not finding_id.strip():
            raise ClusterError(
                "FINDING_INVALID",
                {"field": "id", "reason": "must be a non-empty string"},
            )

    try:
        return copy.deepcopy(finding)
    except Exception as exc:
        raise ClusterError(
            "FINDING_INVALID", {"reason": "finding is not copyable"}
        ) from exc


class FindingClusterer:
    """Group findings by root key and keep manual merge/split decisions."""

    def __init__(self, *, max_findings=DEFAULT_MAX_FINDINGS,
                 max_members=DEFAULT_MAX_MEMBERS):
        self.max_findings = max_findings
        self.max_members = max_members
        self._lock = threading.RLock()
        self._findings = {}
        self._finding_seq = 1
        self._clusters = {}
        self._cluster_seq = 1
        self._root_index = {}
        self._root_primary = {}
        self._audit = []
        self._op_seq = 0
        self._merges = 0
        self._splits = 0

    def add(self, finding):
        clean = _validate_finding(finding)

        with self._lock:
            finding_id = clean.get("id")
            if finding_id is not None and finding_id in self._findings:
                raise ClusterError(
                    "FINDING_DUPLICATE", {"finding_id": finding_id}
                )

            if len(self._findings) >= self.max_findings:
                raise ClusterError(
                    "FINDING_CAPACITY_EXCEEDED",
                    {
                        "max_findings": self.max_findings,
                        "findings": len(self._findings),
                    },
                )

            if finding_id is None:
                finding_id = self._next_finding_id_locked()

            clean["id"] = finding_id
            self._findings[finding_id] = clean
            self._assign_finding_locked(finding_id)
            return finding_id

    def cluster(self):
        with self._lock:
            items = []
            for record in self._active_records_locked():
                dto = self._cluster_dto_locked(record)
                items.append((record["seq"], dto))
            items.sort(key=lambda item: (tuple(item[1]["roots"]), item[0]))
            return [dto for _, dto in items]

    def merge(self, cluster_a, cluster_b):
        with self._lock:
            record_a = self._active_record_for_argument_locked(cluster_a)
            record_b = self._active_record_for_argument_locked(cluster_b)

            if record_a is None:
                raise ClusterError(
                    "CLUSTER_UNKNOWN", {"cluster_id": cluster_a}
                )
            if record_b is None:
                raise ClusterError(
                    "CLUSTER_UNKNOWN", {"cluster_id": cluster_b}
                )
            if cluster_a == cluster_b:
                raise ClusterError(
                    "MERGE_INVALID",
                    {"cluster_id": cluster_a, "reason": "cannot merge cluster with itself"},
                )

            self._op_seq += 1
            event = {
                "op": "merge",
                "seq": self._op_seq,
                "kept": cluster_a,
                "retired": cluster_b,
                "member_count": len(record_b["member_ids"]),
                "roots": sorted(set(record_a["roots"]) | set(record_b["roots"])),
            }

            record_a["member_ids"].update(record_b["member_ids"])
            record_b["member_ids"].clear()
            record_a["roots"] = event["roots"]
            record_a["origin"] = "merge"
            record_a["lineage"] = self._unique_strings(
                list(record_a["lineage"]) + list(record_b["lineage"])
            )
            record_a["merged_from"] = [
                cluster_id
                for cluster_id in record_a["lineage"]
                if cluster_id != cluster_a
            ]
            record_a["history"].append(copy.deepcopy(event))
            record_a["history"].sort(key=lambda item: item.get("seq", 0))

            record_b["status"] = "retired"
            record_b["merged_into"] = cluster_a
            self._remove_cluster_from_root_index_locked(record_b)
            self._add_cluster_to_root_index_locked(record_a)
            for root in record_a["roots"]:
                self._root_primary[root] = cluster_a

            self._audit.append(copy.deepcopy(event))
            self._merges += 1
            return cluster_a

    def split(self, cluster_id, finding_ids):
        with self._lock:
            record = self._active_record_for_argument_locked(cluster_id)
            if record is None:
                raise ClusterError(
                    "CLUSTER_UNKNOWN", {"cluster_id": cluster_id}
                )

            requested_ids = self._normalise_id_list_argument(finding_ids)
            if requested_ids is None:
                raise ClusterError(
                    "SPLIT_INVALID",
                    {"cluster_id": cluster_id, "reason": "finding_ids must be a list of finding ids"},
                )
            if not requested_ids:
                raise ClusterError(
                    "SPLIT_INVALID",
                    {"cluster_id": cluster_id, "reason": "finding_ids must not be empty"},
                )

            try:
                requested_set = set(requested_ids)
            except TypeError:
                raise ClusterError(
                    "SPLIT_INVALID",
                    {
                        "cluster_id": cluster_id,
                        "reason": "finding_ids must contain only finding id strings",
                    },
                )
            if any(not isinstance(finding_id, str) for finding_id in requested_set):
                raise ClusterError(
                    "SPLIT_INVALID",
                    {
                        "cluster_id": cluster_id,
                        "invalid_finding_ids": sorted(
                            repr(finding_id)
                            for finding_id in requested_set
                            if not isinstance(finding_id, str)
                        ),
                    },
                )

            foreign_ids = requested_set.difference(record["member_ids"])
            if foreign_ids:
                raise ClusterError(
                    "SPLIT_INVALID",
                    {
                        "cluster_id": cluster_id,
                        "foreign_finding_ids": sorted(foreign_ids),
                    },
                )

            child = self._create_cluster_locked([], origin="split", split_from=cluster_id)
            child_id = child["id"]

            self._op_seq += 1
            event = {
                "op": "split",
                "seq": self._op_seq,
                "source": cluster_id,
                "split_id": child_id,
                "finding_ids": sorted(requested_set),
            }

            for finding_id in requested_set:
                record["member_ids"].discard(finding_id)
                child["member_ids"].add(finding_id)

            self._refresh_cluster_roots_locked(record)
            self._refresh_cluster_roots_locked(child)
            for root in set(record["roots"]) | set(child["roots"]):
                self._select_primary_for_root_locked(root)
            record["split_children"].append(child_id)
            record["history"].append(copy.deepcopy(event))
            child["history"].append(copy.deepcopy(event))
            self._audit.append(copy.deepcopy(event))
            self._splits += 1
            return child_id

    def members(self, cluster_id):
        with self._lock:
            record = self._require_active_cluster_locked(cluster_id)
            members, _, _ = self._public_members_locked(record)
            return members

    def explain(self, cluster_id):
        with self._lock:
            record = self._require_active_cluster_locked(cluster_id)
            members, overflow, total = self._public_members_locked(record)
            lineage = set(record["lineage"])
            events = [
                copy.deepcopy(event)
                for event in self._audit
                if self._event_touches_lineage(event, lineage)
            ]
            trace = {
                "origin": record["origin"],
                "history": copy.deepcopy(record["history"]),
                "events": events,
            }
            return {
                "id": record["id"],
                "origin": record["origin"],
                "roots": list(record["roots"]),
                "root_keys": list(record["roots"]),
                "members": members,
                "member_ids": list(members),
                "member_count": total,
                "total_members": total,
                "visible_member_count": len(members),
                "overflow": overflow,
                "lineage": list(record["lineage"]),
                "merged_from": list(record["merged_from"]),
                "split_from": record["split_from"],
                "split_children": list(record["split_children"]),
                "history": copy.deepcopy(record["history"]),
                "events": events,
                "trace": trace,
            }

    def findings(self):
        with self._lock:
            return copy.deepcopy(list(self._findings.values()))

    def stats(self):
        with self._lock:
            by_rule = {}
            for record in self._active_records_locked():
                rules = {root.split("|", 1)[0] for root in record["roots"]}
                for rule_id in rules:
                    by_rule[rule_id] = by_rule.get(rule_id, 0) + 1

            return {
                "findings": len(self._findings),
                "clusters": len(self._active_records_locked()),
                "by_rule": {key: by_rule[key] for key in sorted(by_rule)},
                "merges": self._merges,
                "splits": self._splits,
            }

    def snapshot(self):
        with self._lock:
            state = {
                "version": SNAPSHOT_VERSION,
                "max_findings": self.max_findings,
                "max_members": self.max_members,
                "finding_seq": self._finding_seq,
                "cluster_seq": self._cluster_seq,
                "op_seq": self._op_seq,
                "root_primary": copy.deepcopy(self._root_primary),
                "findings": copy.deepcopy(list(self._findings.values())),
                "clusters": [],
                "audit": copy.deepcopy(self._audit),
                "counters": {
                    "merges": self._merges,
                    "splits": self._splits,
                },
                "merges": self._merges,
                "splits": self._splits,
            }
            for record in self._clusters.values():
                item = {
                    key: copy.deepcopy(value)
                    for key, value in record.items()
                    if key != "member_ids"
                }
                item["member_ids"] = sorted(record["member_ids"])
                state["clusters"].append(item)
            return state

    def restore(self, state):
        try:
            bundle = self._parse_snapshot(state)
        except ClusterError:
            raise
        except Exception as exc:
            raise ClusterError(
                "SNAPSHOT_INVALID", {"reason": "malformed snapshot structure"}
            ) from exc
        with self._lock:
            root_index = {}
            for record in bundle["clusters"].values():
                cluster_id = record["id"]
                for root in record["roots"]:
                    root_index.setdefault(root, set()).add(cluster_id)

            self.max_findings = bundle["max_findings"]
            self.max_members = bundle["max_members"]
            self._findings = bundle["findings"]
            self._finding_seq = max(bundle["finding_seq"], self._finding_seq)
            self._cluster_seq = max(bundle["cluster_seq"], self._cluster_seq)
            self._op_seq = max(bundle["op_seq"], self._op_seq)
            self._clusters = bundle["clusters"]
            self._root_index = root_index
            self._root_primary = bundle["root_primary"]
            self._audit = bundle["audit"]
            self._merges = bundle["merges"]
            self._splits = bundle["splits"]

    def reset_stats(self):
        with self._lock:
            self._merges = 0
            self._splits = 0

    def _next_finding_id_locked(self):
        while True:
            finding_id = "f-{:06d}".format(self._finding_seq)
            self._finding_seq += 1
            if finding_id not in self._findings:
                return finding_id

    def _next_cluster_id_locked(self):
        while True:
            cluster_id = "rk-{:06d}".format(self._cluster_seq)
            self._cluster_seq += 1
            if cluster_id not in self._clusters:
                return cluster_id

    def _create_cluster_locked(self, roots, *, origin, split_from):
        cluster_id = self._next_cluster_id_locked()
        record = {
            "id": cluster_id,
            "seq": self._cluster_seq - 1,
            "roots": sorted(set(roots)),
            "member_ids": set(),
            "origin": origin,
            "status": "active",
            "split_from": split_from,
            "split_children": [],
            "merged_from": [],
            "lineage": [cluster_id],
            "history": [],
            "merged_into": None,
        }
        self._clusters[cluster_id] = record
        self._add_cluster_to_root_index_locked(record)
        for root in roots:
            self._root_primary[root] = cluster_id
        return record

    def _assign_finding_locked(self, finding_id):
        root = _root_key(self._findings[finding_id])
        cluster_id = self._select_primary_for_root_locked(root)
        if cluster_id is not None:
            record = self._clusters[cluster_id]
        else:
            record = self._create_cluster_locked(
                [root], origin="auto", split_from=None
            )
            cluster_id = record["id"]

        record["member_ids"].add(finding_id)
        if root not in record["roots"]:
            record["roots"] = self._sorted_roots(
                list(record["roots"]) + [root]
            )
            self._root_index.setdefault(root, set()).add(cluster_id)
        return cluster_id

    def _select_primary_for_root_locked(self, root):
        holders = [
            cluster_id
            for cluster_id in self._root_index.get(root, set())
            if self._clusters[cluster_id]["status"] == "active"
            and root in self._clusters[cluster_id]["roots"]
        ]
        if not holders:
            self._root_primary.pop(root, None)
            return None

        current = self._root_primary.get(root)
        if current in holders:
            return current

        chosen = min(holders, key=lambda item: self._clusters[item]["seq"])
        self._root_primary[root] = chosen
        return chosen

    def _public_members_locked(self, record):
        all_member_ids = list(record["member_ids"])
        total = len(all_member_ids)
        limit = self.max_members
        if not _non_negative_int(limit):
            limit = DEFAULT_MAX_MEMBERS

        capped_by_id = sorted(all_member_ids)[:limit]
        members = sorted(
            capped_by_id,
            key=lambda finding_id: _member_sort_key(
                self._findings[finding_id]
            ),
        )
        return members, total > limit, total

    def _cluster_dto_locked(self, record):
        members, overflow, _ = self._public_members_locked(record)
        return {
            "id": record["id"],
            "roots": list(record["roots"]),
            "members": members,
            "overflow": overflow,
        }

    def _active_records_locked(self):
        return [
            record
            for record in self._clusters.values()
            if record["status"] == "active"
        ]

    def _active_record_for_argument_locked(self, cluster_id):
        if not isinstance(cluster_id, str):
            return None
        record = self._clusters.get(cluster_id)
        if record is None or record.get("status") != "active":
            return None
        return record

    def _require_active_cluster_locked(self, cluster_id):
        record = self._active_record_for_argument_locked(cluster_id)
        if record is None:
            raise ClusterError(
                "CLUSTER_UNKNOWN", {"cluster_id": cluster_id}
            )
        return record

    def _add_cluster_to_root_index_locked(self, record):
        for root in record["roots"]:
            self._root_index.setdefault(root, set()).add(record["id"])

    def _remove_cluster_from_root_index_locked(self, record):
        cluster_id = record["id"]
        for root in record["roots"]:
            holders = self._root_index.get(root)
            if holders is None:
                continue
            holders.discard(cluster_id)
            if not holders:
                del self._root_index[root]

    def _refresh_cluster_roots_locked(self, record):
        cluster_id = record["id"]
        old_roots = set(record["roots"])
        current_roots = {
            _root_key(self._findings[finding_id])
            for finding_id in record["member_ids"]
            if finding_id in self._findings
        }
        new_roots = (
            self._sorted_roots(current_roots)
            if current_roots
            else list(record["roots"])
        )
        new_root_set = set(new_roots)

        for root in old_roots - new_root_set:
            holders = self._root_index.get(root)
            if holders is not None:
                holders.discard(cluster_id)
                if not holders:
                    del self._root_index[root]
        for root in new_root_set - old_roots:
            self._root_index.setdefault(root, set()).add(cluster_id)

        record["roots"] = new_roots
        for root in old_roots | new_root_set:
            self._select_primary_for_root_locked(root)

    @staticmethod
    def _sorted_roots(roots):
        return sorted(set(roots))

    @staticmethod
    def _unique_strings(values):
        result = []
        seen = set()
        for value in values:
            if value not in seen:
                seen.add(value)
                result.append(value)
        return result

    @staticmethod
    def _normalise_id_list_argument(value):
        if isinstance(value, (str, bytes)) or value is None:
            return None
        if not isinstance(value, (list, tuple, set, frozenset)):
            try:
                return list(value)
            except TypeError:
                return None
        return list(value)

    @staticmethod
    def _event_touches_lineage(event, lineage):
        candidate_ids = {
            event.get("cluster_id"),
            event.get("kept"),
            event.get("retired"),
            event.get("source"),
            event.get("split_id"),
        }
        return any(
            isinstance(candidate_id, str) and candidate_id in lineage
            for candidate_id in candidate_ids
        )

    @classmethod
    def _parse_snapshot(cls, state):
        if not isinstance(state, dict):
            raise ClusterError(
                "SNAPSHOT_INVALID", {"reason": "snapshot must be a dictionary"}
            )

        version = state.get("version", _MISSING)
        if isinstance(version, bool) or version != SNAPSHOT_VERSION:
            raise ClusterError(
                "SNAPSHOT_INVALID",
                {"reason": "unsupported version", "version": version},
            )

        required_keys = (
            "max_findings",
            "max_members",
            "finding_seq",
            "cluster_seq",
            "findings",
            "clusters",
            "counters",
        )
        missing = [key for key in required_keys if key not in state]
        if missing:
            raise ClusterError(
                "SNAPSHOT_INVALID", {"missing_keys": missing}
            )

        max_findings = state["max_findings"]
        max_members = state["max_members"]
        if not _positive_int(max_findings) or not _positive_int(max_members):
            raise ClusterError(
                "SNAPSHOT_INVALID", {"reason": "invalid limits"}
            )

        finding_seq = state["finding_seq"]
        cluster_seq = state["cluster_seq"]
        op_seq = state.get("op_seq", 0)
        if (
            not _positive_int(finding_seq)
            or not _positive_int(cluster_seq)
            or not _non_negative_int(op_seq)
        ):
            raise ClusterError(
                "SNAPSHOT_INVALID", {"reason": "invalid sequence counters"}
            )

        counters = state["counters"]
        if not isinstance(counters, dict):
            raise ClusterError(
                "SNAPSHOT_INVALID", {"reason": "counters must be an object"}
            )
        merges = counters.get("merges", _MISSING)
        splits = counters.get("splits", _MISSING)
        if not _non_negative_int(merges) or not _non_negative_int(splits):
            raise ClusterError(
                "SNAPSHOT_INVALID", {"reason": "invalid operation counters"}
            )

        findings = {}
        findings_raw = state["findings"]
        if not isinstance(findings_raw, list):
            raise ClusterError(
                "SNAPSHOT_INVALID", {"field": "findings", "reason": "must be a list"}
            )
        for item in findings_raw:
            clean = _validate_finding(item)
            finding_id = clean.get("id")
            if not isinstance(finding_id, str) or not finding_id.strip():
                raise ClusterError(
                    "SNAPSHOT_INVALID",
                    {"reason": "snapshot finding is missing an id"},
                )
            if finding_id in findings:
                raise ClusterError(
                    "SNAPSHOT_INVALID",
                    {"reason": "duplicate finding id", "finding_id": finding_id},
                )
            findings[finding_id] = clean

        clusters = {}
        assigned = {}
        clusters_raw = state["clusters"]
        if not isinstance(clusters_raw, list):
            raise ClusterError(
                "SNAPSHOT_INVALID", {"field": "clusters", "reason": "must be a list"}
            )

        for item in clusters_raw:
            if not isinstance(item, dict):
                raise ClusterError(
                    "SNAPSHOT_INVALID", {"reason": "cluster snapshot must be an object"}
                )

            cluster_id = item.get("id", _MISSING)
            if not isinstance(cluster_id, str) or not cluster_id.strip():
                raise ClusterError(
                    "SNAPSHOT_INVALID", {"reason": "cluster is missing an id"}
                )
            if cluster_id in clusters:
                raise ClusterError(
                    "SNAPSHOT_INVALID",
                    {"reason": "duplicate cluster id", "cluster_id": cluster_id},
                )

            seq = item.get("seq", _MISSING)
            if not _positive_int(seq):
                raise ClusterError(
                    "SNAPSHOT_INVALID",
                    {"cluster_id": cluster_id, "reason": "invalid cluster sequence"},
                )

            roots_raw = item.get("roots", _MISSING)
            if (
                not isinstance(roots_raw, list)
                or not roots_raw
                or any(not isinstance(root, str) or not root for root in roots_raw)
            ):
                raise ClusterError(
                    "SNAPSHOT_INVALID",
                    {"cluster_id": cluster_id, "reason": "roots must be a non-empty list of strings"},
                )

            member_ids_raw = item.get("member_ids", _MISSING)
            if not isinstance(member_ids_raw, list):
                raise ClusterError(
                    "SNAPSHOT_INVALID",
                    {"cluster_id": cluster_id, "reason": "member_ids must be a list"},
                )

            member_ids = set()
            for finding_id in member_ids_raw:
                if not isinstance(finding_id, str) or finding_id not in findings:
                    raise ClusterError(
                        "SNAPSHOT_INVALID",
                        {"cluster_id": cluster_id, "unknown_finding_id": finding_id},
                    )
                if finding_id in member_ids:
                    raise ClusterError(
                        "SNAPSHOT_INVALID",
                        {"cluster_id": cluster_id, "duplicate_finding_id": finding_id},
                    )
                if finding_id in assigned:
                    raise ClusterError(
                        "SNAPSHOT_INVALID",
                        {
                            "reason": "finding belongs to multiple clusters",
                            "finding_id": finding_id,
                        },
                    )
                member_ids.add(finding_id)
                assigned[finding_id] = cluster_id

            origin = item.get("origin", "auto")
            if origin not in ("auto", "merge", "split"):
                raise ClusterError(
                    "SNAPSHOT_INVALID",
                    {"cluster_id": cluster_id, "reason": "invalid origin"},
                )

            status = item.get("status", "active")
            if status not in ("active", "retired"):
                raise ClusterError(
                    "SNAPSHOT_INVALID",
                    {"cluster_id": cluster_id, "reason": "invalid status"},
                )

            record = {
                "id": cluster_id,
                "seq": seq,
                "roots": sorted(set(roots_raw)),
                "member_ids": member_ids,
                "origin": origin,
                "status": status,
                "split_from": item.get("split_from"),
                "split_children": list(item.get("split_children", [])),
                "merged_from": list(item.get("merged_from", [])),
                "lineage": list(item.get("lineage", [cluster_id])),
                "history": copy.deepcopy(item.get("history", [])),
                "merged_into": item.get("merged_into"),
            }
            if cluster_id not in record["lineage"]:
                record["lineage"].insert(0, cluster_id)
            clusters[cluster_id] = record

        if len(assigned) != len(findings):
            missing_findings = sorted(set(findings) - set(assigned))
            raise ClusterError(
                "SNAPSHOT_INVALID",
                {
                    "reason": "findings without exactly one cluster assignment",
                    "finding_ids": missing_findings,
                },
            )

        for record in clusters.values():
            cls._validate_snapshot_reference(record["id"], record["split_from"], clusters, "split_from")
            cls._validate_snapshot_reference(record["id"], record["merged_into"], clusters, "merged_into")
            for child_id in record["split_children"]:
                cls._validate_snapshot_reference(record["id"], child_id, clusters, "split_children")
            for source_id in record["merged_from"]:
                cls._validate_snapshot_reference(record["id"], source_id, clusters, "merged_from")
            for lineage_id in record["lineage"]:
                cls._validate_snapshot_reference(record["id"], lineage_id, clusters, "lineage")

        audit = state.get("audit", [])
        if not isinstance(audit, list):
            raise ClusterError(
                "SNAPSHOT_INVALID", {"field": "audit", "reason": "must be a list"}
            )

        root_primary_raw = state.get("root_primary", {})
        if not isinstance(root_primary_raw, dict):
            raise ClusterError(
                "SNAPSHOT_INVALID",
                {"field": "root_primary", "reason": "must be an object"},
            )
        root_primary = {}
        for root, primary_id in root_primary_raw.items():
            primary = clusters.get(primary_id) if isinstance(primary_id, str) else None
            if (
                not isinstance(root, str)
                or not root
                or primary is None
                or primary["status"] != "active"
                or root not in primary["roots"]
            ):
                raise ClusterError(
                    "SNAPSHOT_INVALID",
                    {"field": "root_primary", "invalid_reference": primary_id},
                )
            root_primary[root] = primary_id

        return {
            "max_findings": max_findings,
            "max_members": max_members,
            "finding_seq": finding_seq,
            "cluster_seq": cluster_seq,
            "op_seq": op_seq,
            "findings": findings,
            "clusters": clusters,
            "audit": copy.deepcopy(audit),
            "root_primary": root_primary,
            "merges": merges,
            "splits": splits,
        }

    @staticmethod
    def _validate_snapshot_reference(cluster_id, reference, clusters, field_name):
        if reference is None:
            return
        if not isinstance(reference, str) or reference not in clusters:
            raise ClusterError(
                "SNAPSHOT_INVALID",
                {
                    "cluster_id": cluster_id,
                    "field": field_name,
                    "invalid_reference": reference,
                },
            )
