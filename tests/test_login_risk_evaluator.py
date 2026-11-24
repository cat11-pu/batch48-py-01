import copy
import threading

import pytest

import login_risk_evaluator as lr

KEYS = ["new_device", "geo_velocity", "failures", "odd_hour",
        "source_reputation"]
WEIGHTS = {"new_device": 30.0, "geo_velocity": 25.0, "failures": 20.0,
           "odd_hour": 10.0, "source_reputation": 15.0}
THRESHOLDS = {"allow": 30.0, "step_up": 70.0}


def evaluator(**kwargs):
    kwargs.setdefault("weights", dict(WEIGHTS))
    kwargs.setdefault("thresholds", dict(THRESHOLDS))
    return lr.LoginRiskEvaluator(**kwargs)


def test_register_signal_stores_value_and_counts():
    risk = evaluator()
    out = risk.register_signal("u1", "failures", 4, now=0)
    assert out == {"account": "u1", "key": "failures", "value": 4.0,
                   "stored": True}
    assert risk.stats()["signals"] == 1
    scored = risk.score_login("u1", now=0)
    assert scored["contributions"] == {"failures": 8.0}
    assert scored["score"] == 8.0
    assert scored["history"] == 0.0


def test_score_login_combines_weighted_saturating_signals():
    risk = evaluator()
    scored = risk.score_login("u1", signals={"new_device": 10,
                                             "geo_velocity": 5,
                                             "failures": 2}, now=0)
    assert scored["contributions"] == {"failures": 4.0, "geo_velocity": 12.5,
                                       "new_device": 30.0}
    assert scored["score"] == 46.5
    assert scored["matched"] == ["new_device", "geo_velocity", "failures"]


def test_signal_value_is_clamped_at_cap():
    risk = evaluator()
    big = risk.score_login("u1", signals={"new_device": 50}, now=0)
    at_cap = risk.score_login("u2", signals={"new_device": 10}, now=0)
    assert big["contributions"] == {"new_device": 30.0}
    assert big["score"] == at_cap["score"] == 30.0
    zero = risk.score_login("u3", signals={"new_device": 0}, now=0)
    assert zero["contributions"] == {}
    assert zero["score"] == 0.0


def test_bad_signal_inputs_are_refused():
    risk = evaluator()
    cases = [
        ("u1", "banana", 1, "LOGIN_RISK_UNKNOWN_SIGNAL"),
        ("u1", "failures", -1, "LOGIN_RISK_INVALID"),
        ("", "failures", 1, "LOGIN_RISK_INVALID"),
        ("u1", "failures", True, "LOGIN_RISK_INVALID"),
        ("u1", "failures", "many", "LOGIN_RISK_INVALID"),
    ]
    for account, key, value, code in cases:
        with pytest.raises(lr.LoginRiskError) as exc:
            risk.register_signal(account, key, value)
        assert exc.value.code == code
    assert risk.stats()["rejected"] == 5
    assert risk.stats()["signals"] == 0
    assert risk.score_login("u1")["score"] == 0.0


def test_decide_maps_scores_onto_threshold_bands():
    risk = evaluator()
    clean = risk.decide("u1", now=0)
    assert clean == {"account": "u1", "score": 0.0, "decision": "allow",
                     "reason": "below_threshold", "matched": []}

    boundary = risk.decide("u2", signals={"new_device": 10}, now=0)
    assert boundary["score"] == 30.0
    assert boundary["decision"] == "step_up"
    assert boundary["reason"] == "step_up_band"

    heavy = risk.decide("u3", signals={"new_device": 10, "geo_velocity": 10,
                                       "source_reputation": 10}, now=0)
    assert heavy["score"] == 70.0
    assert heavy["decision"] == "deny"
    assert heavy["reason"] == "above_threshold"
    assert heavy["matched"] == ["new_device", "geo_velocity",
                                "source_reputation"]


def test_allowlisted_account_is_always_allowed():
    risk = evaluator()
    out = risk.set_list("vip", "allow")
    assert out == {"account": "vip", "list": "allow"}
    heavy = {"new_device": 10, "geo_velocity": 10, "failures": 10,
             "odd_hour": 10, "source_reputation": 10}
    decided = risk.decide("vip", signals=heavy, now=0)
    assert decided["decision"] == "allow"
    assert decided["reason"] == "allowlist"
    assert risk.explain("vip", signals=heavy, now=0)["list"] == "allow"


def test_denylisted_account_is_denied_before_scoring_band():
    risk = evaluator()
    risk.set_list("bad", "deny")
    out = risk.decide("bad", now=0)
    assert out["decision"] == "deny"
    assert out["reason"] == "denylist"

    risk.set_list("bad", None)
    assert risk.explain("bad", now=0)["list"] is None
    assert risk.decide("bad", now=0)["decision"] == "allow"


def test_record_outcome_accumulates_history_for_the_account():
    risk = evaluator()
    first = risk.record_outcome("u1", "failure", now=0)
    assert first["outcome"] == "failure"
    assert first["history"] > 0.0
    second = risk.record_outcome("u1", "failure", now=0)["history"]
    assert second == first["history"] * 2
    third = risk.record_outcome("u1", "success", now=0)["history"]
    assert 0.0 < third < second
    assert risk.history_score("u1", now=0) == third
    assert risk.stats()["outcomes"] == 3
    with pytest.raises(lr.LoginRiskError) as exc:
        risk.record_outcome("u1", "maybe", now=0)
    assert exc.value.code == "LOGIN_RISK_INVALID"


def test_decay_halves_history_over_one_half_life():
    risk = evaluator()
    risk.record_outcome("u1", "failure", now=0)
    first = risk.decay("u1", now=3600)
    assert first["account"] == "u1"
    assert first["after"] == round(first["before"] / 2.0, 6)
    second = risk.decay("u1", now=7200)
    assert second["before"] == first["after"]
    assert second["after"] == round(second["before"] / 2.0, 6)
    assert risk.decay("ghost", now=0) == {"account": "ghost", "before": 0.0,
                                          "after": 0.0}


def test_history_component_joins_the_login_score():
    risk = evaluator()
    risk.record_outcome("u1", "failure", now=0)
    scored = risk.score_login("u1", now=3600)
    assert scored["history"] > 0.0
    assert scored["score"] == scored["history"]
    assert scored["contributions"] == {}
    assert risk.decide("u1", now=3600)["decision"] == "allow"


def test_explain_orders_contributions_and_reports_band():
    risk = evaluator()
    out = risk.explain("u1", signals={"failures": 2, "odd_hour": 5,
                                      "new_device": 10}, now=0)
    assert out["contributions"] == {"failures": 4.0, "new_device": 30.0,
                                    "odd_hour": 5.0}
    assert out["matched"] == ["new_device", "odd_hour", "failures"]
    assert out["score"] == 39.0
    assert out["history"] == 0.0
    assert out["decision"] == "step_up"
    assert out["reason"] == "step_up_band"


def test_signals_outside_the_window_are_ignored():
    risk = evaluator()
    risk.register_signal("u1", "failures", 4, now=0)
    inside = risk.score_login("u1", now=600)
    assert inside["contributions"] == {"failures": 8.0}
    assert inside["score"] == 8.0

    outside = risk.score_login("u1", now=601)
    assert outside["contributions"] == {}
    assert outside["score"] == 0.0


def test_snapshot_restore_keeps_account_state():
    risk = evaluator()
    risk.register_signal("u1", "failures", 4, now=0)
    risk.record_outcome("u1", "failure", now=0)
    risk.set_list("u2", "deny")

    snap = risk.snapshot()
    pristine = copy.deepcopy(snap)
    clone = evaluator()
    clone.restore(pristine)
    assert clone.stats() == risk.stats()
    assert clone.score_login("u1", now=0) == risk.score_login("u1", now=0)
    assert clone.decide("u2", now=0)["reason"] == "denylist"

    snap["signals"]["u1"]["failures"]["value"] = 99
    snap["lists"]["u2"] = "allow"
    assert risk.score_login("u1", now=0)["contributions"] == {"failures": 8.0}
    assert risk.decide("u2", now=0)["decision"] == "deny"

    empty_stats = {"signals": 0, "scored": 0, "decided": 0, "outcomes": 0,
                   "rejected": 0}
    bad_states = [
        None,
        {},
        {"version": 2, "weights": {}, "thresholds": {"allow": 30,
                                                     "step_up": 70},
         "half_life": 3600, "window": 600, "signals": {}, "history": {},
         "lists": {}, "stats": empty_stats},
        {"version": 1, "weights": {}, "thresholds": {"allow": 30,
                                                     "step_up": 70},
         "half_life": 3600, "window": 600, "signals": {"u1": 5},
         "history": {}, "lists": {}, "stats": empty_stats},
        {"version": 1, "weights": {}, "thresholds": {"allow": 30,
                                                     "step_up": 70},
         "half_life": 3600, "window": 600, "signals": {}, "history": {},
         "lists": {"u1": "maybe"}, "stats": empty_stats},
        {"version": 1, "weights": {}, "thresholds": {"allow": 30,
                                                     "step_up": 70},
         "half_life": 3600, "window": 600, "signals": {}, "history": {},
         "lists": {}, "stats": {"signals": 1}},
    ]
    for bad in bad_states:
        with pytest.raises(lr.LoginRiskError) as exc:
            risk.restore(bad)
        assert exc.value.code == "LOGIN_RISK_SNAPSHOT_INVALID"
    assert risk.history_score("u1", now=0) > 0.0


def test_stats_track_each_operation_family():
    risk = evaluator()
    risk.register_signal("u1", "failures", 2)
    risk.register_signal("u1", "new_device", 10)
    risk.score_login("u1")
    risk.decide("u1")
    risk.record_outcome("u1", "failure")
    with pytest.raises(lr.LoginRiskError) as exc:
        risk.register_signal("u1", "nope", 1)
    assert exc.value.code == "LOGIN_RISK_UNKNOWN_SIGNAL"
    assert risk.stats() == {"signals": 2, "scored": 1, "decided": 1,
                            "outcomes": 1, "rejected": 1}


def test_concurrent_logins_read_the_complete_signal_set():
    risk = evaluator()
    errors = []

    def writer(key):
        try:
            risk.register_signal("u1", key, 10, now=0)
        except Exception as exc:  # pragma: no cover - reported through errors
            errors.append(repr(exc))

    def reader():
        try:
            for _ in range(20):
                scored = risk.score_login("u1", now=0)
                assert isinstance(scored["score"], float)
                assert set(scored["contributions"]).issubset(set(KEYS))
                for value in scored["contributions"].values():
                    assert value >= 0.0
        except Exception as exc:  # pragma: no cover - reported through errors
            errors.append(repr(exc))

    threads = [threading.Thread(target=writer, args=(key,)) for key in KEYS]
    threads += [threading.Thread(target=reader) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert risk.stats()["signals"] == 5
    final = risk.score_login("u1", now=0)
    assert final["score"] == 100.0
    assert final["matched"] == ["new_device", "geo_velocity", "failures",
                                "source_reputation", "odd_hour"]
    assert final["contributions"] == {"new_device": 30.0, "geo_velocity": 25.0,
                                      "failures": 20.0, "odd_hour": 10.0,
                                      "source_reputation": 15.0}
