"""Multi-signal vulnerability priority ranker."""

from __future__ import annotations

import copy
import math
import threading
from datetime import datetime, timezone
from typing import Any


HALF_LIFE_DAYS = 180.0
EXPOSURE_FACTORS = {"internal": 1.0, "partner": 1.2, "internet": 1.5}
DEFAULT_CVSS = 10.0
DEFAULT_EPSS = 1.0
KEV_BOOST = 30.0
STATE_VERSION = 1
MISSING_FIELDS = ("cvss", "epss", "kev", "published_at")


class RankerError(ValueError):
    """Refusal raised by the ranker; ``code`` is a stable string."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _bounded_number(value: Any, minimum: float, maximum: float,
                    field: str) -> float:
    if not _finite_number(value) or value < minimum or value > maximum:
        raise RankerError(
            "ranker.invalid_signal",
            {"field": field, "value": value},
        )
    return float(value)


def _asset_weight(criticality: Any, exposure: Any) -> float:
    if (
        isinstance(criticality, bool)
        or not isinstance(criticality, int)
        or not 1 <= criticality <= 5
    ):
        raise RankerError(
            "ranker.invalid_asset",
            {"field": "criticality", "value": criticality},
        )
    if not isinstance(exposure, str) or exposure not in EXPOSURE_FACTORS:
        raise RankerError(
            "ranker.invalid_asset",
            {"field": "exposure", "value": exposure},
        )
    return round(criticality * EXPOSURE_FACTORS[exposure], 6)


def _parse_iso_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise RankerError(
            "ranker.invalid_signal",
            {"field": "published_at", "value": value},
        )

    text = value.strip()
    if not text:
        raise RankerError(
            "ranker.invalid_signal",
            {"field": "published_at", "value": value},
        )
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise RankerError(
            "ranker.invalid_signal",
            {"field": "published_at", "value": value},
        ) from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _resolve_now(now: str | datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, datetime):
        if now.tzinfo is None:
            return now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)
    try:
        return _parse_iso_time(now)
    except RankerError as exc:
        if isinstance(exc.detail, dict):
            exc.detail["field"] = "now"
        raise


class VulnPriorityRanker:
    """Combine CVSS, EPSS, KEV and asset weight into a stable order."""

    def __init__(self, *, max_links_per_asset: int = 20000,
                 max_links: int = 200000,
                 half_life_days: float = HALF_LIFE_DAYS) -> None:
        self._max_links_per_asset = max_links_per_asset
        self._max_links = max_links
        self._half_life_days = half_life_days
        self._lock = threading.RLock()
        self._assets: dict[Any, dict[str, Any]] = {}
        self._vulns: dict[Any, dict[str, Any]] = {}
        self._links: set[tuple[Any, Any]] = set()
        self._links_by_asset: dict[Any, set[Any]] = {}
        self._rank_calls = 0

    def upsert_asset(self, asset_id: str, *, criticality: int,
                     exposure: str) -> dict:
        weight = _asset_weight(criticality, exposure)
        record = {
            "asset_id": asset_id,
            "criticality": criticality,
            "exposure": exposure,
            "weight": weight,
        }
        with self._lock:
            self._assets[asset_id] = record
            self._links_by_asset.setdefault(asset_id, set())
        return dict(record)

    def upsert_vuln(self, vuln_id: str, *, cvss=None, epss=None, kev=None,
                    published_at=None) -> dict:
        missing: set[str] = set()

        if cvss is None:
            missing.add("cvss")
            cvss_value = DEFAULT_CVSS
        else:
            cvss_value = _bounded_number(cvss, 0.0, 10.0, "cvss")

        if epss is None:
            missing.add("epss")
            epss_value = DEFAULT_EPSS
        else:
            epss_value = _bounded_number(epss, 0.0, 1.0, "epss")

        if kev is None:
            missing.add("kev")
            kev_value = False
        elif not isinstance(kev, bool):
            raise RankerError(
                "ranker.invalid_signal",
                {"field": "kev", "value": kev},
            )
        else:
            kev_value = kev

        if published_at is None:
            missing.add("published_at")
            published_value = None
        else:
            _parse_iso_time(published_at)
            published_value = published_at

        missing_list = sorted(missing)
        record = {
            "vuln_id": vuln_id,
            "cvss": cvss_value,
            "epss": epss_value,
            "kev": kev_value,
            "published_at": published_value,
            "missing": missing_list,
        }
        with self._lock:
            self._vulns[vuln_id] = record
        return {
            "vuln_id": vuln_id,
            "cvss": cvss_value,
            "epss": epss_value,
            "kev": kev_value,
            "published_at": published_value,
            "missing": list(missing_list),
        }

    def link(self, asset_id: str, vuln_id: str) -> bool:
        with self._lock:
            if asset_id not in self._assets:
                raise RankerError(
                    "ranker.unknown_asset",
                    {"asset_id": asset_id},
                )
            if vuln_id not in self._vulns:
                raise RankerError(
                    "ranker.unknown_vuln",
                    {"vuln_id": vuln_id},
                )

            pair = (asset_id, vuln_id)
            if pair in self._links:
                return False

            asset_links = self._links_by_asset.setdefault(asset_id, set())
            if len(asset_links) >= self._max_links_per_asset:
                raise RankerError(
                    "ranker.asset_limit",
                    {
                        "asset_id": asset_id,
                        "limit": self._max_links_per_asset,
                    },
                )
            if len(self._links) >= self._max_links:
                raise RankerError(
                    "ranker.global_limit",
                    {"limit": self._max_links},
                )

            asset_links.add(vuln_id)
            self._links.add(pair)
            return True

    def unlink(self, asset_id: str, vuln_id: str) -> bool:
        with self._lock:
            pair = (asset_id, vuln_id)
            if pair not in self._links:
                return False

            self._links.remove(pair)
            asset_links = self._links_by_asset.get(asset_id)
            if asset_links is not None:
                asset_links.discard(vuln_id)
                if not asset_links:
                    self._links_by_asset.pop(asset_id, None)
            return True

    def rank(self, now=None, *, limit=None) -> list[dict]:
        with self._lock:
            self._rank_calls += 1
            if limit is not None and (
                isinstance(limit, bool)
                or not isinstance(limit, int)
                or limit <= 0
            ):
                raise RankerError(
                    "ranker.invalid_limit",
                    {"limit": limit},
                )

            now_dt = _resolve_now(now)
            entries: list[dict] = []
            for asset_id, vuln_id in sorted(self._links):
                score, _components, _missing = self._score_pair_locked(
                    vuln_id,
                    asset_id,
                    now_dt,
                )
                vuln = self._vulns[vuln_id]
                entries.append({
                    "vuln_id": vuln_id,
                    "asset_id": asset_id,
                    "score": score,
                    "cvss": vuln["cvss"],
                    "epss": vuln["epss"],
                    "kev": vuln["kev"],
                })

            entries.sort(key=lambda item: (
                -item["score"],
                item["vuln_id"],
                item["asset_id"],
            ))
            if limit is not None:
                entries = entries[:limit]
            return entries

    def score(self, vuln_id: str, asset_id: str, now=None) -> float:
        with self._lock:
            now_dt = _resolve_now(now)
            score, _components, _missing = self._score_pair_locked(
                vuln_id,
                asset_id,
                now_dt,
            )
            return score

    def explain(self, vuln_id: str, asset_id: str, now=None) -> dict:
        with self._lock:
            now_dt = _resolve_now(now)
            score, components, missing = self._score_pair_locked(
                vuln_id,
                asset_id,
                now_dt,
            )
            return {
                "vuln_id": vuln_id,
                "asset_id": asset_id,
                "score": score,
                "components": components,
                "missing": missing,
            }

    def state(self) -> dict:
        with self._lock:
            return {
                "version": STATE_VERSION,
                "assets": copy.deepcopy(self._assets),
                "vulns": copy.deepcopy(self._vulns),
                "links": [
                    {"asset_id": asset_id, "vuln_id": vuln_id}
                    for asset_id, vuln_id in sorted(self._links)
                ],
            }

    def restore(self, state: dict) -> None:
        if not isinstance(state, dict):
            raise RankerError(
                "ranker.state_invalid",
                {"reason": "state_must_be_object"},
            )

        version = state.get("version")
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version != STATE_VERSION
        ):
            raise RankerError(
                "ranker.state_invalid",
                {"field": "version", "value": version},
            )

        for key in ("assets", "vulns", "links"):
            if key not in state:
                raise RankerError(
                    "ranker.state_invalid",
                    {"field": key, "reason": "missing"},
                )

        assets_data = state["assets"]
        vulns_data = state["vulns"]
        links_data = state["links"]
        if not isinstance(assets_data, dict):
            raise RankerError("ranker.state_invalid", {"field": "assets"})
        if not isinstance(vulns_data, dict):
            raise RankerError("ranker.state_invalid", {"field": "vulns"})

        candidate_assets: dict[Any, dict[str, Any]] = {}
        for asset_id, data in assets_data.items():
            candidate_assets[asset_id] = self._restore_asset(asset_id, data)

        candidate_vulns: dict[Any, dict[str, Any]] = {}
        for vuln_id, data in vulns_data.items():
            candidate_vulns[vuln_id] = self._restore_vuln(vuln_id, data)

        candidate_links = self._restore_links(
            links_data,
            candidate_assets,
            candidate_vulns,
        )
        links_by_asset: dict[Any, set[Any]] = {}
        for asset_id, vuln_id in candidate_links:
            links_by_asset.setdefault(asset_id, set()).add(vuln_id)

        with self._lock:
            self._assets = candidate_assets
            self._vulns = candidate_vulns
            self._links = candidate_links
            self._links_by_asset = links_by_asset

    def stats(self) -> dict:
        with self._lock:
            return {
                "assets": len(self._assets),
                "vulns": len(self._vulns),
                "links": len(self._links),
                "rank_calls": self._rank_calls,
            }

    def _score_pair_locked(
        self,
        vuln_id: str,
        asset_id: str,
        now_dt: datetime,
    ) -> tuple[float, dict[str, float], list[str]]:
        vuln = self._vulns.get(vuln_id)
        if vuln is None:
            raise RankerError(
                "ranker.unknown_vuln",
                {"vuln_id": vuln_id},
            )
        asset = self._assets.get(asset_id)
        if asset is None:
            raise RankerError(
                "ranker.unknown_asset",
                {"asset_id": asset_id},
            )

        if vuln["published_at"] is None:
            published_dt = now_dt
        else:
            published_dt = _parse_iso_time(vuln["published_at"])

        age_days = (now_dt - published_dt).total_seconds() / 86400.0
        if age_days < 0:
            age_days = 0.0
        decay = 0.5 ** (age_days / float(self._half_life_days))

        cvss_component = vuln["cvss"] * 6.0
        epss_component = vuln["epss"] * 25.0
        kev_component = KEV_BOOST if vuln["kev"] else 0.0
        raw_score = cvss_component + epss_component + kev_component
        score = round(raw_score * float(asset["weight"]) * decay, 6)

        components = {
            "cvss": cvss_component,
            "epss": epss_component,
            "kev": kev_component,
            "asset_weight": asset["weight"],
            "decay": decay,
        }
        return score, components, list(vuln["missing"])

    @staticmethod
    def _invalid_state(detail: object) -> RankerError:
        return RankerError("ranker.state_invalid", detail)

    @classmethod
    def _restore_asset(cls, asset_id: Any, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise cls._invalid_state({"field": f"assets.{asset_id}"})
        if "asset_id" in data and data["asset_id"] != asset_id:
            raise cls._invalid_state({
                "field": "asset_id",
                "key": asset_id,
                "value": data["asset_id"],
            })

        criticality = data.get("criticality")
        exposure = data.get("exposure")
        try:
            weight = _asset_weight(criticality, exposure)
        except RankerError as exc:
            raise cls._invalid_state(exc.detail) from exc

        if "weight" in data:
            weight_value = data["weight"]
            if not _finite_number(weight_value):
                raise cls._invalid_state({"field": "weight"})
            if round(float(weight_value), 6) != weight:
                raise cls._invalid_state({
                    "field": "weight",
                    "value": weight_value,
                })

        return {
            "asset_id": asset_id,
            "criticality": criticality,
            "exposure": exposure,
            "weight": weight,
        }

    @classmethod
    def _restore_vuln(cls, vuln_id: Any, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise cls._invalid_state({"field": f"vulns.{vuln_id}"})
        if "vuln_id" in data and data["vuln_id"] != vuln_id:
            raise cls._invalid_state({
                "field": "vuln_id",
                "key": vuln_id,
                "value": data["vuln_id"],
            })

        missing_data = data.get("missing", [])
        if missing_data is None:
            missing_set: set[str] = set()
        elif (
            not isinstance(missing_data, list)
            or not all(isinstance(item, str) for item in missing_data)
            or any(item not in MISSING_FIELDS for item in missing_data)
        ):
            raise cls._invalid_state({
                "field": "missing",
                "value": missing_data,
            })
        else:
            missing_set = set(missing_data)

        cvss_raw = data.get("cvss")
        if "cvss" in missing_set or cvss_raw is None:
            missing_set.add("cvss")
            cvss = DEFAULT_CVSS
        else:
            try:
                cvss = _bounded_number(cvss_raw, 0.0, 10.0, "cvss")
            except RankerError as exc:
                raise cls._invalid_state(exc.detail) from exc

        epss_raw = data.get("epss")
        if "epss" in missing_set or epss_raw is None:
            missing_set.add("epss")
            epss = DEFAULT_EPSS
        else:
            try:
                epss = _bounded_number(epss_raw, 0.0, 1.0, "epss")
            except RankerError as exc:
                raise cls._invalid_state(exc.detail) from exc

        kev_raw = data.get("kev")
        if "kev" in missing_set or kev_raw is None:
            missing_set.add("kev")
            kev = False
        elif not isinstance(kev_raw, bool):
            raise cls._invalid_state({"field": "kev", "value": kev_raw})
        else:
            kev = kev_raw

        published_raw = data.get("published_at")
        if "published_at" in missing_set or published_raw is None:
            missing_set.add("published_at")
            published_at = None
        else:
            try:
                _parse_iso_time(published_raw)
            except RankerError as exc:
                raise cls._invalid_state(exc.detail) from exc
            published_at = published_raw

        return {
            "vuln_id": vuln_id,
            "cvss": cvss,
            "epss": epss,
            "kev": kev,
            "published_at": published_at,
            "missing": sorted(missing_set),
        }

    @classmethod
    def _restore_links(
        cls,
        links_data: Any,
        assets: dict[Any, dict[str, Any]],
        vulns: dict[Any, dict[str, Any]],
    ) -> set[tuple[Any, Any]]:
        pairs: set[tuple[Any, Any]] = set()

        def add_pair(asset_id: Any, vuln_id: Any) -> None:
            try:
                hash(asset_id)
                hash(vuln_id)
            except TypeError as exc:
                raise cls._invalid_state({"field": "links"}) from exc
            if asset_id not in assets:
                raise cls._invalid_state({
                    "field": "links.asset_id",
                    "value": asset_id,
                })
            if vuln_id not in vulns:
                raise cls._invalid_state({
                    "field": "links.vuln_id",
                    "value": vuln_id,
                })
            pairs.add((asset_id, vuln_id))

        if isinstance(links_data, list):
            for item in links_data:
                if isinstance(item, dict):
                    if "asset_id" not in item or "vuln_id" not in item:
                        raise cls._invalid_state({
                            "field": "links",
                            "value": item,
                        })
                    add_pair(item["asset_id"], item["vuln_id"])
                elif isinstance(item, (list, tuple)) and len(item) == 2:
                    add_pair(item[0], item[1])
                else:
                    raise cls._invalid_state({
                        "field": "links",
                        "value": item,
                    })
        elif isinstance(links_data, dict):
            for asset_id, vuln_values in links_data.items():
                if isinstance(vuln_values, dict):
                    iterator = vuln_values.keys()
                elif isinstance(vuln_values, (list, tuple, set, frozenset)):
                    iterator = vuln_values
                else:
                    raise cls._invalid_state({
                        "field": "links",
                        "asset_id": asset_id,
                    })
                for vuln_id in iterator:
                    add_pair(asset_id, vuln_id)
        else:
            raise cls._invalid_state({"field": "links"})

        return pairs
