import json
import threading

import pytest

import vuln_priority_ranker as vp

NOW = "2026-11-24T00:00:00+00:00"
DAY90 = "2026-08-26T00:00:00+00:00"
DAY180 = "2026-05-28T00:00:00+00:00"


def ranker(**kwargs):
    return vp.VulnPriorityRanker(**kwargs)


def one_link(vuln="v1", **vuln_kwargs):
    r = ranker()
    r.upsert_asset("a", criticality=4, exposure="internet")
    r.upsert_vuln(vuln, published_at=DAY180, cvss=8.0, epss=0.4, kev=False,
                  **vuln_kwargs)
    r.link("a", vuln)
    return r


def test_upsert_records_signals_and_missing_defaults():
    r = ranker()
    asset = r.upsert_asset("a1", criticality=3, exposure="partner")
    assert asset == {
        "asset_id": "a1", "criticality": 3, "exposure": "partner", "weight": 3.6,
    }
    vuln = r.upsert_vuln("v1", cvss=7.5, epss=0.2, kev=True, published_at=NOW)
    assert vuln == {
        "vuln_id": "v1", "cvss": 7.5, "epss": 0.2, "kev": True,
        "published_at": NOW, "missing": [],
    }
    blank = r.upsert_vuln("v2")
    assert blank["cvss"] == 10.0
    assert blank["epss"] == 1.0
    assert blank["kev"] is False
    assert blank["published_at"] is None
    assert blank["missing"] == ["cvss", "epss", "kev", "published_at"]
    assert r.stats() == {"assets": 1, "vulns": 2, "links": 0, "rank_calls": 0}


def test_score_uses_documented_weights():
    r = one_link()
    assert r.score("v1", "a", now=NOW) == pytest.approx(174.0)
    info = r.explain("v1", "a", now=NOW)
    assert info["vuln_id"] == "v1"
    assert info["asset_id"] == "a"
    assert info["score"] == pytest.approx(174.0)
    assert info["components"] == {
        "cvss": 48.0, "epss": 10.0, "kev": 0.0, "asset_weight": 6.0, "decay": 0.5,
    }
    assert info["missing"] == []


def test_asset_weight_multiplies_score():
    r = ranker()
    r.upsert_asset("low", criticality=2, exposure="internal")
    r.upsert_asset("high", criticality=5, exposure="internet")
    r.upsert_vuln("v", cvss=6.0, epss=0.2, kev=False, published_at=DAY180)
    r.link("low", "v")
    r.link("high", "v")

    low = r.score("v", "low", now=NOW)
    high = r.score("v", "high", now=NOW)
    assert low == pytest.approx(41.0)
    assert high == pytest.approx(153.75)
    assert high / low == pytest.approx(3.75)


def test_decay_halves_at_half_life():
    r = ranker()
    r.upsert_asset("a", criticality=1, exposure="internal")
    for vuln, published in (("vnow", NOW), ("v90", DAY90), ("v180", DAY180)):
        r.upsert_vuln(vuln, cvss=10.0, epss=0.0, kev=False, published_at=published)
        r.link("a", vuln)

    assert r.score("vnow", "a", now=NOW) == pytest.approx(60.0)
    assert r.score("v180", "a", now=NOW) == pytest.approx(30.0)
    assert r.score("v90", "a", now=NOW) == pytest.approx(60.0 * 0.5 ** 0.5)
    assert r.explain("v90", "a", now=NOW)["components"]["decay"] == pytest.approx(
        0.5 ** 0.5
    )


def test_kev_flag_adds_fixed_boost_before_asset_weight():
    r = ranker()
    r.upsert_asset("a", criticality=3, exposure="internal")
    r.upsert_vuln("plain", cvss=5.0, epss=0.1, kev=False, published_at=NOW)
    r.upsert_vuln("exploited", cvss=5.0, epss=0.1, kev=True, published_at=NOW)
    r.link("a", "plain")
    r.link("a", "exploited")

    delta = r.score("exploited", "a", now=NOW) - r.score("plain", "a", now=NOW)
    assert delta == pytest.approx(90.0)
    assert r.explain("exploited", "a", now=NOW)["components"]["kev"] == 30.0


def test_rank_orders_by_score_and_honours_limit():
    r = ranker()
    r.upsert_asset("a", criticality=5, exposure="internet")
    r.upsert_vuln("v-low", cvss=2.0, epss=0.05, kev=False, published_at=NOW)
    r.upsert_vuln("v-mid", cvss=6.0, epss=0.2, kev=False, published_at=NOW)
    r.upsert_vuln("v-high", cvss=9.8, epss=0.9, kev=True, published_at=NOW)
    for vuln in ("v-low", "v-mid", "v-high"):
        r.link("a", vuln)

    ranked = r.rank(NOW)
    assert [item["vuln_id"] for item in ranked] == ["v-high", "v-mid", "v-low"]
    assert ranked[0]["score"] > ranked[1]["score"] > ranked[2]["score"]
    assert set(ranked[0]) == {"vuln_id", "asset_id", "score", "cvss", "epss", "kev"}
    assert [item["vuln_id"] for item in ranked] == ["v-high", "v-mid", "v-low"]
    assert [item["vuln_id"] for item in r.rank(NOW, limit=2)] == ["v-high", "v-mid"]
    with pytest.raises(vp.RankerError) as exc:
        r.rank(NOW, limit=0)
    assert exc.value.code == "ranker.invalid_limit"


def test_explain_marks_missing_signal_and_uses_default():
    r = ranker()
    r.upsert_asset("a", criticality=5, exposure="internal")
    r.upsert_vuln("v", epss=0.4, kev=False, published_at=NOW)
    r.link("a", "v")

    info = r.explain("v", "a", now=NOW)
    assert info["missing"] == ["cvss"]
    assert info["components"]["cvss"] == 60.0
    assert info["score"] == pytest.approx(350.0)


def test_upsert_vuln_replaces_signals_and_keeps_links():
    r = one_link()
    before = r.score("v1", "a", now=NOW)
    updated = r.upsert_vuln("v1", cvss=9.8, epss=0.4, kev=False, published_at=DAY180)
    assert updated["cvss"] == 9.8
    assert r.score("v1", "a", now=NOW) > before
    assert r.stats()["vulns"] == 1
    assert r.stats()["links"] == 1


def test_link_and_unlink_are_idempotent():
    r = ranker()
    r.upsert_asset("a", criticality=1, exposure="internal")
    r.upsert_vuln("v", cvss=1.0, epss=0.0, kev=False, published_at=NOW)
    assert r.link("a", "v") is True
    assert r.link("a", "v") is False
    assert r.stats()["links"] == 1
    assert r.unlink("a", "v") is True
    assert r.unlink("a", "v") is False
    assert r.stats()["links"] == 0
    assert r.rank(NOW) == []


def test_unknown_identifiers_are_refused():
    r = ranker()
    r.upsert_asset("a", criticality=1, exposure="internal")
    r.upsert_vuln("v", cvss=1.0, epss=0.0, kev=False, published_at=NOW)

    with pytest.raises(vp.RankerError) as exc:
        r.link("missing", "v")
    assert exc.value.code == "ranker.unknown_asset"
    with pytest.raises(vp.RankerError) as exc:
        r.link("a", "missing")
    assert exc.value.code == "ranker.unknown_vuln"
    with pytest.raises(vp.RankerError) as exc:
        r.score("missing", "a", now=NOW)
    assert exc.value.code == "ranker.unknown_vuln"
    with pytest.raises(vp.RankerError) as exc:
        r.score("v", "missing", now=NOW)
    assert exc.value.code == "ranker.unknown_asset"
    with pytest.raises(vp.RankerError) as exc:
        r.explain("missing", "a", now=NOW)
    assert exc.value.code == "ranker.unknown_vuln"


def test_per_asset_link_limit_is_enforced():
    r = ranker(max_links_per_asset=2)
    r.upsert_asset("a", criticality=1, exposure="internal")
    for index in range(3):
        r.upsert_vuln(f"v{index}", cvss=1.0, epss=0.0, kev=False, published_at=NOW)
    r.link("a", "v0")
    r.link("a", "v1")

    with pytest.raises(vp.RankerError) as exc:
        r.link("a", "v2")
    assert exc.value.code == "ranker.asset_limit"
    assert r.stats()["links"] == 2


def test_global_link_limit_is_enforced():
    r = ranker(max_links=3)
    r.upsert_asset("a", criticality=1, exposure="internal")
    r.upsert_asset("b", criticality=1, exposure="internal")
    for index in range(4):
        r.upsert_vuln(f"v{index}", cvss=1.0, epss=0.0, kev=False, published_at=NOW)
    r.link("a", "v0")
    r.link("a", "v1")
    r.link("b", "v2")

    with pytest.raises(vp.RankerError) as exc:
        r.link("b", "v3")
    assert exc.value.code == "ranker.global_limit"
    assert r.stats()["links"] == 3


def test_invalid_signals_and_assets_are_refused():
    r = ranker()
    r.upsert_asset("a", criticality=1, exposure="internal")
    base = {"cvss": 5.0, "epss": 0.1, "kev": False, "published_at": NOW}
    for override in ({"cvss": 11.0}, {"epss": -0.1}, {"kev": "yes"},
                     {"published_at": "not-a-date"}):
        payload = dict(base)
        payload.update(override)
        with pytest.raises(vp.RankerError) as exc:
            r.upsert_vuln("v", **payload)
        assert exc.value.code == "ranker.invalid_signal"

    for kwargs in ({"criticality": 0, "exposure": "internal"},
                   {"criticality": 3, "exposure": "dmz"}):
        with pytest.raises(vp.RankerError) as exc:
            r.upsert_asset("bad", **kwargs)
        assert exc.value.code == "ranker.invalid_asset"

    assert r.stats() == {"assets": 1, "vulns": 0, "links": 0, "rank_calls": 0}


def test_state_restore_roundtrip_and_validation():
    r = ranker()
    r.upsert_asset("a", criticality=4, exposure="internet")
    r.upsert_asset("b", criticality=2, exposure="internal")
    r.upsert_vuln("v1", cvss=8.0, epss=0.4, kev=False, published_at=DAY180)
    r.upsert_vuln("v2", cvss=5.0, epss=0.2, kev=True, published_at=DAY90)
    r.link("a", "v1")
    r.link("a", "v2")
    r.link("b", "v1")

    before = r.rank(NOW)
    score_before = r.score("v1", "a", now=NOW)
    state = r.state()
    assert json.loads(json.dumps(state)) == state
    state["vulns"]["v1"]["cvss"] = 0.0
    assert r.score("v1", "a", now=NOW) == pytest.approx(score_before)

    restored = ranker()
    restored.restore(r.state())
    assert restored.rank(NOW) == before
    assert restored.stats()["links"] == 3

    for bad in (None, {}, {"version": 2, "assets": {}, "vulns": {}, "links": {}}):
        with pytest.raises(vp.RankerError) as exc:
            restored.restore(bad)
        assert exc.value.code == "ranker.state_invalid"
    assert restored.rank(NOW) == before


def test_concurrent_link_and_rank_keep_counts_exact():
    r = ranker(max_links=1000)
    for index in range(4):
        r.upsert_asset(f"a{index}", criticality=1 + index, exposure="internal")
    for index in range(20):
        r.upsert_vuln(f"v{index}", cvss=float(index % 10),
                      epss=(index % 10) / 10.0, kev=(index % 3 == 0),
                      published_at=NOW)

    errors = []
    snapshots = []
    guard = threading.Lock()

    def worker(index):
        try:
            for step in range(10):
                r.link(f"a{index % 4}", f"v{(index * 7 + step) % 20}")
                result = r.rank(NOW)
                with guard:
                    snapshots.append(result)
        except Exception as exc:  # pragma: no cover - reported through errors
            errors.append((index, repr(exc)))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for _ in range(20):
        r.rank(NOW)
    for thread in threads:
        thread.join()

    expected = set()
    for index in range(8):
        for step in range(10):
            expected.add((f"a{index % 4}", f"v{(index * 7 + step) % 20}"))

    assert errors == []
    assert r.stats()["links"] == len(expected)
    assert r.stats()["rank_calls"] == 100
    assert len(snapshots) == 80
    for snapshot in snapshots:
        assert all(snapshot[i]["score"] >= snapshot[i + 1]["score"]
                   for i in range(len(snapshot) - 1))
