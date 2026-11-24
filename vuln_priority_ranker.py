"""Multi-signal vulnerability priority ranker."""

from __future__ import annotations

import threading

HALF_LIFE_DAYS = 180.0
EXPOSURE_FACTORS = {"internal": 1.0, "partner": 1.2, "internet": 1.5}
DEFAULT_CVSS = 10.0
DEFAULT_EPSS = 1.0
KEV_BOOST = 30.0


class RankerError(ValueError):
    """Refusal raised by the ranker; ``code`` is a stable string."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class VulnPriorityRanker:
    """Combine CVSS, EPSS, KEV and asset weight into a stable order."""

    def __init__(self, *, max_links_per_asset: int = 20000,
                 max_links: int = 200000,
                 half_life_days: float = HALF_LIFE_DAYS) -> None:
        self._max_links_per_asset = max_links_per_asset
        self._max_links = max_links
        self._half_life_days = half_life_days
        self._lock = threading.RLock()

    def upsert_asset(self, asset_id: str, *, criticality: int,
                     exposure: str) -> dict:
        raise NotImplementedError

    def upsert_vuln(self, vuln_id: str, *, cvss=None, epss=None, kev=None,
                    published_at=None) -> dict:
        raise NotImplementedError

    def link(self, asset_id: str, vuln_id: str) -> bool:
        raise NotImplementedError

    def unlink(self, asset_id: str, vuln_id: str) -> bool:
        raise NotImplementedError

    def rank(self, now=None, *, limit=None) -> list[dict]:
        raise NotImplementedError

    def score(self, vuln_id: str, asset_id: str, now=None) -> float:
        raise NotImplementedError

    def explain(self, vuln_id: str, asset_id: str, now=None) -> dict:
        raise NotImplementedError

    def state(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError
