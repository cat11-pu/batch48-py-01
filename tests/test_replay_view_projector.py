import copy
import threading

import pytest

import replay_view_projector as rp


def projector(**kwargs):
    return rp.ReplayViewProjector(**kwargs)


def player_view(proj, view_id="v1", team="red", owner="p1"):
    return proj.register_view(view_id, kind="player", team=team, owner=owner)


def basic(**kwargs):
    proj = projector(**kwargs)
    player_view(proj)
    proj.register_entity("e1", team="red")
    proj.register_entity("e2", team="blue")
    proj.record_vision("v1", at=100, visible=["e1", "e2"])
    return proj


def unit_event(seq, at, entity, payload=None, etype="move"):
    event = {"seq": seq, "at": at, "type": etype, "entity": entity}
    if payload is not None:
        event["payload"] = payload
    return event


def test_register_view_returns_metadata_and_duplicate():
    proj = projector()
    meta = player_view(proj)
    assert meta == {"view_id": "v1", "kind": "player", "team": "red",
                    "owner": "p1", "version": 1, "status": "registered"}
    again = player_view(proj)
    assert again["status"] == "duplicate"
    assert again["view_id"] == "v1"
    observer = proj.register_view("obs", kind="observer", version=3)
    assert observer["status"] == "registered"
    assert observer["team"] is None
    assert observer["version"] == 3
    assert proj.stats()["views"] == 2
    assert proj.stats()["duplicates"] == 1


def test_register_view_conflict_validation_and_limit():
    proj = projector(view_limit=2)
    player_view(proj, "v1")
    with pytest.raises(rp.ReplayError) as exc:
        proj.register_view("v1", kind="player", team="red", owner="p2")
    assert exc.value.code == "REPLAY_VIEW_CONFLICT"
    bad = [
        dict(kind="admin", team="red", owner="p1"),
        dict(kind="player", team=None, owner="p1"),
        dict(kind="player", team="red", owner=None),
        dict(kind="player", team="red", owner="p1", version=0),
    ]
    for kwargs in bad:
        with pytest.raises(rp.ReplayError) as exc:
            proj.register_view("vx", **kwargs)
        assert exc.value.code == "REPLAY_INVALID_VIEW"
    with pytest.raises(rp.ReplayError) as exc:
        proj.register_view("")
    assert exc.value.code == "REPLAY_INVALID_VIEW"
    player_view(proj, "v2")
    with pytest.raises(rp.ReplayError) as exc:
        proj.register_view("v3", kind="observer")
    assert exc.value.code == "REPLAY_VIEW_LIMIT"
    assert proj.stats()["views"] == 2
    assert proj.stats()["rejected"] == 6


def test_register_entity_duplicate_conflict_and_limit():
    proj = projector(entity_limit=2)
    first = proj.register_entity("e1", team="red")
    assert first == {"entity_id": "e1", "team": "red", "category": "unit",
                     "status": "registered"}
    assert proj.register_entity("e1", team="red")["status"] == "duplicate"
    with pytest.raises(rp.ReplayError) as exc:
        proj.register_entity("e1", team="blue")
    assert exc.value.code == "REPLAY_ENTITY_CONFLICT"
    with pytest.raises(rp.ReplayError) as exc:
        proj.register_entity("e2", team="red", category="tower")
    assert exc.value.code == "REPLAY_INVALID_ENTITY"
    with pytest.raises(rp.ReplayError) as exc:
        proj.register_entity("", team="red")
    assert exc.value.code == "REPLAY_INVALID_ENTITY"
    proj.register_entity("e2", team="blue", category="structure")
    with pytest.raises(rp.ReplayError) as exc:
        proj.register_entity("e3", team="red")
    assert exc.value.code == "REPLAY_ENTITY_LIMIT"
    assert proj.stats()["entities"] == 2


def test_record_vision_sorts_counts_and_rejects_unknown_entities():
    proj = projector()
    player_view(proj)
    proj.register_entity("e1", team="red")
    proj.register_entity("e2", team="blue")
    out = proj.record_vision("v1", at=100, visible=["e2", "e1", "e2"])
    assert out == {"view_id": "v1", "at": 100, "visible": ["e1", "e2"],
                   "samples": 1, "status": "recorded"}
    with pytest.raises(rp.ReplayError) as exc:
        proj.record_vision("v1", at=200, visible=["e1", "ghost"])
    assert exc.value.code == "REPLAY_UNKNOWN_ENTITY"
    assert exc.value.detail == {"entity": "ghost"}
    with pytest.raises(rp.ReplayError) as exc:
        proj.record_vision("nope", at=200, visible=["e1"])
    assert exc.value.code == "REPLAY_UNKNOWN_VIEW"
    assert proj.stats()["vision_samples"] == 1
    assert proj.stats()["rejected"] == 2


def test_vision_samples_enforce_order_and_conflicts():
    proj = projector()
    player_view(proj)
    proj.register_entity("e1", team="red")
    proj.register_entity("e2", team="blue")
    proj.record_vision("v1", at=100, visible=["e1"])
    with pytest.raises(rp.ReplayError) as exc:
        proj.record_vision("v1", at=50, visible=["e1"])
    assert exc.value.code == "REPLAY_VISION_ORDER"
    assert proj.record_vision("v1", at=100, visible=["e1"])["status"] == "duplicate"
    with pytest.raises(rp.ReplayError) as exc:
        proj.record_vision("v1", at=100, visible=["e1", "e2"])
    assert exc.value.code == "REPLAY_VISION_CONFLICT"
    second = proj.record_vision("v1", at=150, visible=["e1", "e2"])
    assert second["samples"] == 2
    assert second["visible"] == ["e1", "e2"]


def test_project_event_visible_entity_records_payload():
    proj = basic()
    out = proj.project_event("v1", unit_event(1, 150, "e1", {"x": 3, "y": 4}))
    assert out == {"seq": 1, "at": 150, "type": "move", "entity": "e1",
                   "team": "red", "status": "visible", "reason": "",
                   "payload": {"x": 3, "y": 4}}
    stored = proj.content_of("v1", 1)
    assert stored == out
    assert proj.event_count("v1") == 1
    assert proj.stats()["projected"] == 1
    assert proj.stats()["visible"] == 1


def test_project_event_hidden_when_entity_leaves_latest_sample():
    proj = basic()
    proj.record_vision("v1", at=300, visible=["e2"])
    before = proj.project_event("v1", unit_event(1, 200, "e1", {"x": 1}))
    after = proj.project_event("v1", unit_event(2, 400, "e1", {"x": 1}))
    assert before["status"] == "visible"
    assert after["status"] == "hidden"
    assert after["reason"] == "not_visible"
    assert after["entity"] == "e1"
    assert after["payload"] == {}
    assert proj.event_count("v1") == 2
    assert proj.stats()["visible"] == 1
    assert proj.stats()["hidden"] == 1


def test_project_event_rejects_unknown_view_entity_and_invalid_type():
    proj = basic()
    with pytest.raises(rp.ReplayError) as exc:
        proj.project_event("ghost", unit_event(1, 100, "e1"))
    assert exc.value.code == "REPLAY_UNKNOWN_VIEW"
    with pytest.raises(rp.ReplayError) as exc:
        proj.project_event("v1", unit_event(1, 100, "nobody"))
    assert exc.value.code == "REPLAY_UNKNOWN_ENTITY"
    with pytest.raises(rp.ReplayError) as exc:
        proj.project_event("v1", unit_event(1, 100, "e1", etype="explode"))
    assert exc.value.code == "REPLAY_INVALID_EVENT"
    with pytest.raises(rp.ReplayError) as exc:
        proj.project_event("v1", {"seq": 0, "at": 10, "type": "move", "entity": "e1"})
    assert exc.value.code == "REPLAY_INVALID_EVENT"
    assert proj.event_count("v1") == 0
    assert proj.stats()["rejected"] == 4


def test_chat_visibility_scopes():
    proj = basic()
    chat = lambda seq, team, payload: {"seq": seq, "at": 150, "type": "chat",
                                       "team": team, "payload": payload}
    everyone = proj.project_event(
        "v1", chat(1, "blue", {"scope": "all", "text": "hi"}))
    own_team = proj.project_event(
        "v1", chat(2, "red", {"scope": "team", "text": "push"}))
    other_team = proj.project_event(
        "v1", chat(3, "blue", {"scope": "team", "text": "secret"}))
    direct = proj.project_event(
        "v1", chat(4, "blue", {"scope": "private", "to": ["p1"], "text": "psst"}))
    indirect = proj.project_event(
        "v1", chat(5, "blue", {"scope": "private", "to": ["p2"], "text": "psst"}))
    assert everyone["status"] == "visible"
    assert everyone["payload"]["text"] == "hi"
    assert own_team["status"] == "visible"
    assert other_team["status"] == "cropped"
    assert other_team["reason"] == "team"
    assert other_team["payload"] == {}
    assert direct["status"] == "visible"
    assert indirect["status"] == "cropped"
    assert indirect["reason"] == "private"
    assert proj.stats()["cropped"] == 2


def test_projection_is_idempotent_and_conflict_per_seq():
    proj = basic()
    event = unit_event(1, 150, "e1", {"x": 1})
    first = proj.project_event("v1", event)
    again = proj.project_event("v1", dict(event))
    assert first["status"] == "visible"
    assert again["status"] == "duplicate"
    assert proj.event_count("v1") == 1
    with pytest.raises(rp.ReplayError) as exc:
        proj.project_event("v1", unit_event(1, 150, "e1", {"x": 2}))
    assert exc.value.code == "REPLAY_EVENT_CONFLICT"
    assert proj.event_count("v1") == 1
    assert proj.stats()["duplicates"] == 1
    assert proj.stats()["conflicts"] == 1


def test_event_limit_per_view():
    proj = basic(event_limit=2)
    proj.project_event("v1", unit_event(1, 120, "e1"))
    proj.project_event("v1", unit_event(2, 130, "e1"))
    with pytest.raises(rp.ReplayError) as exc:
        proj.project_event("v1", unit_event(3, 140, "e1"))
    assert exc.value.code == "REPLAY_EVENT_LIMIT"
    assert proj.event_count("v1") == 2
    assert proj.stats()["projected"] == 2
    assert proj.stats()["rejected"] == 1


def test_project_range_filters_sorts_and_truncates():
    proj = basic()
    for seq, at in ((5, 500), (1, 100), (3, 300), (7, 700)):
        proj.project_event("v1", unit_event(seq, at, "e1"))
    out = proj.project_range("v1", start=100, end=600)
    assert [e["seq"] for e in out["events"]] == [1, 3, 5]
    assert out["count"] == 3
    assert out["truncated"] is False
    limited = proj.project_range("v1", start=0, end=800, limit=2)
    assert [e["seq"] for e in limited["events"]] == [1, 3]
    assert limited["count"] == 2
    assert limited["truncated"] is True
    assert proj.project_range("v1", start=500)["count"] == 2
    with pytest.raises(rp.ReplayError) as exc:
        proj.project_range("v1", start=-1)
    assert exc.value.code == "REPLAY_INVALID_RANGE"
    with pytest.raises(rp.ReplayError) as exc:
        proj.project_range("v1", start=500, end=100)
    assert exc.value.code == "REPLAY_INVALID_RANGE"
    with pytest.raises(rp.ReplayError) as exc:
        proj.project_range("ghost")
    assert exc.value.code == "REPLAY_UNKNOWN_VIEW"


def test_seek_and_verify_view():
    proj = basic()
    proj.project_event("v1", unit_event(1, 100, "e1"))
    proj.project_event("v1", unit_event(2, 200, "e2"))
    proj.project_event("v1", unit_event(3, 300, "e1"))
    assert proj.seek("v1", 200) == {"view_id": "v1", "cursor": 200,
                                    "events_before": 2}
    assert proj.seek("v1", 150)["events_before"] == 1
    with pytest.raises(rp.ReplayError) as exc:
        proj.seek("v1", -5)
    assert exc.value.code == "REPLAY_INVALID_RANGE"
    report = proj.verify_view("v1")
    assert report == {"view_id": "v1", "version": 1, "events": 3,
                      "visible": 3, "hidden": 0, "cropped": 0,
                      "consistent": True}


def test_snapshot_restore_and_corruption():
    proj = basic()
    proj.project_event("v1", unit_event(1, 150, "e1", {"x": 1}))
    snap = proj.snapshot()
    assert snap["version"] == 1
    pristine = copy.deepcopy(snap)
    snap["journal"]["v1"][1]["status"] = "hidden"
    assert proj.content_of("v1", 1)["status"] == "visible"

    restored = projector()
    restored.restore(pristine)
    assert restored.event_count("v1") == 1
    assert restored.content_of("v1", 1)["payload"] == {"x": 1}
    assert restored.seek("v1", 200)["events_before"] == 1
    assert restored.stats() == proj.stats()
    restored.project_event("v1", unit_event(2, 160, "e1"))
    assert restored.event_count("v1") == 2
    assert proj.event_count("v1") == 1

    for bad in (None, {}, {"version": 2}, {"version": 1, "views": []}):
        with pytest.raises(rp.ReplayError) as exc:
            restored.restore(bad)
        assert exc.value.code == "REPLAY_SNAPSHOT_INVALID"
    assert restored.event_count("v1") == 2


def test_concurrent_projection_of_same_seq_applies_once():
    proj = basic()
    event = unit_event(1, 150, "e1", {"x": 1})
    outcomes = []
    lock = threading.Lock()

    def worker():
        try:
            tag = proj.project_event("v1", dict(event))["status"]
        except Exception as exc:  # pragma: no cover - surfaced as a failure
            tag = repr(exc)
        with lock:
            outcomes.append(tag)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count("visible") == 1
    assert outcomes.count("duplicate") == 7
    assert proj.event_count("v1") == 1
    assert proj.stats()["visible"] == 1
    assert proj.stats()["duplicates"] == 7
