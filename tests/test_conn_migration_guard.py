import copy
import threading

import pytest

import conn_migration_guard as mg

ADDR = "10.0.0.1:443"
NEW_ADDR = "10.0.0.9:443"


def guard(**overrides):
    return mg.ConnMigrationGuard(**overrides)


def session_with_path(guard_obj, name="s1", now=0):
    guard_obj.create_session(name, addr=ADDR, now=now)
    return guard_obj.path_state(name)["paths"][0]["path"]


def test_create_session_and_ticket_bookkeeping():
    g = guard()
    out = g.create_session("s1", addr=ADDR, now=0)
    assert out["session"] == "s1"
    assert out["active_path"] == "s1/p0"
    assert out["pending_path"] is None
    assert out["epoch"] == 0
    assert out["rtt_ms"] is None
    assert out["migrations"] == 0
    assert [item["path"] for item in out["paths"]] == ["s1/p0"]
    assert out["paths"][0]["state"] == "active"
    assert out["paths"][0]["epoch"] == 0
    assert g.stats()["sessions"] == 1

    with pytest.raises(mg.MigrationError) as exc:
        g.create_session("s1", addr=ADDR)
    assert exc.value.code == "MIGRATION_SESSION_EXISTS"
    with pytest.raises(mg.MigrationError) as exc:
        g.create_session("", addr=ADDR)
    assert exc.value.code == "MIGRATION_INVALID"
    assert g.create_session("s2", addr=ADDR)["session"] == "s2"

    first = g.issue_ticket("s1", ttl_ms=100, now=10)
    assert first["ticket"] == "tk-0001"
    assert first["session"] == "s1"
    assert first["expires_at"] == 110
    second = g.issue_ticket("s1", ttl_ms=50, now=20)
    assert second["ticket"] == "tk-0002"
    assert second["expires_at"] == 70
    with pytest.raises(mg.MigrationError) as exc:
        g.issue_ticket("nope")
    assert exc.value.code == "MIGRATION_UNKNOWN_SESSION"
    with pytest.raises(mg.MigrationError) as exc:
        g.issue_ticket("s1", ttl_ms=0)
    assert exc.value.code == "MIGRATION_INVALID"


def test_probe_rejects_unknown_mismatched_and_expired_tickets():
    g = guard()
    session_with_path(g, "s1")
    g.create_session("s2", addr=ADDR)

    foreign = g.issue_ticket("s2", ttl_ms=500, now=0)
    with pytest.raises(mg.MigrationError) as exc:
        g.probe_path("s1", NEW_ADDR, ticket=foreign["ticket"], now=10)
    assert exc.value.code == "MIGRATION_TICKET_MISMATCH"
    with pytest.raises(mg.MigrationError) as exc:
        g.probe_path("s1", NEW_ADDR, ticket="tk-9999", now=10)
    assert exc.value.code == "MIGRATION_UNKNOWN_TICKET"

    stale = g.issue_ticket("s1", ttl_ms=100, now=0)
    with pytest.raises(mg.MigrationError) as exc:
        g.probe_path("s1", NEW_ADDR, ticket=stale["ticket"], now=100)
    assert exc.value.code == "MIGRATION_TICKET_EXPIRED"
    assert len(g.list_paths("s1")) == 1
    assert g.path_state("s1")["epoch"] == 0


def test_probe_opens_new_epoch_without_touching_active_path():
    g = guard()
    active = session_with_path(g, "s1")
    ticket = g.issue_ticket("s1", ttl_ms=1000, now=0)
    out = g.probe_path("s1", NEW_ADDR, ticket=ticket["ticket"], now=10)
    assert out["session"] == "s1"
    assert out["path"] == "s1/p1"
    assert out["addr"] == NEW_ADDR
    assert out["epoch"] == 1
    assert out["state"] == "candidate"

    state = g.path_state("s1")
    assert state["active_path"] == active
    assert state["pending_path"] == "s1/p1"
    assert state["epoch"] == 1
    paths = g.list_paths("s1")
    assert [item["path"] for item in paths] == ["s1/p0", "s1/p1"]
    assert paths[1]["state"] == "candidate"
    assert g.stats()["probes"] == 1


def test_pending_candidate_and_path_limit_are_hard_stops():
    g = guard()
    session_with_path(g, "s1")
    g.probe_path("s1", NEW_ADDR, ticket=g.issue_ticket("s1", now=0)["ticket"])
    with pytest.raises(mg.MigrationError) as exc:
        g.probe_path("s1", "10.0.0.10:443",
                     ticket=g.issue_ticket("s1", now=1)["ticket"], now=2)
    assert exc.value.code == "MIGRATION_PENDING_EXISTS"

    limited = guard(max_paths_per_session=2)
    session_with_path(limited, "s2")
    limited.probe_path("s2", NEW_ADDR,
                       ticket=limited.issue_ticket("s2", now=0)["ticket"])
    with pytest.raises(mg.MigrationError) as exc:
        limited.probe_path("s2", "10.0.0.10:443",
                           ticket=limited.issue_ticket("s2", now=1)["ticket"],
                           now=2)
    assert exc.value.code == "MIGRATION_PATH_LIMIT"
    assert len(limited.list_paths("s2")) == 2
    assert limited.stats()["rejections"] == 1


def test_migration_limit_and_cooldown_reject_without_moving_active_path():
    capped = guard(max_migrations=1, cooldown_ms=0)
    session_with_path(capped, "s1")
    capped.probe_path("s1", NEW_ADDR,
                      ticket=capped.issue_ticket("s1", now=0)["ticket"])
    capped.complete_migration("s1", now=10)
    with pytest.raises(mg.MigrationError) as exc:
        capped.probe_path("s1", "10.0.0.10:443",
                          ticket=capped.issue_ticket("s1", now=11)["ticket"],
                          now=12)
    assert exc.value.code == "MIGRATION_LIMIT_REACHED"

    cooling = guard(cooldown_ms=1000, max_migrations=4, max_paths_per_session=4)
    session_with_path(cooling, "s1")
    cooling.probe_path("s1", NEW_ADDR,
                       ticket=cooling.issue_ticket("s1", now=0)["ticket"])
    cooling.complete_migration("s1", now=0)
    before = cooling.path_state("s1")
    with pytest.raises(mg.MigrationError) as exc:
        cooling.probe_path("s1", "10.0.0.10:443",
                           ticket=cooling.issue_ticket("s1", now=0)["ticket"],
                           now=400)
    assert exc.value.code == "MIGRATION_COOLDOWN"
    after = cooling.path_state("s1")
    assert after["active_path"] == before["active_path"]
    assert after["epoch"] == before["epoch"]
    assert len(after["paths"]) == 2

    allowed = cooling.probe_path("s1", "10.0.0.10:443",
                                 ticket=cooling.issue_ticket("s1", now=0)["ticket"],
                                 now=1000)
    assert allowed["epoch"] == 2
    assert allowed["path"] == "s1/p2"


def test_complete_migration_promotes_candidate_and_starts_grace():
    g = guard(grace_ms=500)
    session_with_path(g, "s1")
    g.probe_path("s1", NEW_ADDR, ticket=g.issue_ticket("s1", now=0)["ticket"])
    out = g.complete_migration("s1", now=20)
    assert out["active_path"] == "s1/p1"
    assert out["previous_path"] == "s1/p0"
    assert out["epoch"] == 1
    assert out["grace_until"] == 520
    assert out["migrations"] == 1

    state = g.path_state("s1")
    assert state["active_path"] == "s1/p1"
    assert state["pending_path"] is None
    paths = {item["path"]: item for item in state["paths"]}
    assert paths["s1/p0"]["state"] == "retiring"
    assert paths["s1/p0"]["grace_until"] == 520
    assert paths["s1/p1"]["state"] == "active"
    with pytest.raises(mg.MigrationError) as exc:
        g.complete_migration("s1", now=30)
    assert exc.value.code == "MIGRATION_NO_PENDING"


def test_packets_are_accepted_out_of_order_inside_reorder_window():
    g = guard()
    active = session_with_path(g, "s1")
    first = g.on_packet(active, 0, 10, payload="a", now=1)
    assert first["accepted"] is True
    assert first["reason"] is None
    assert first["high_water"] == 10
    second = g.on_packet(active, 0, 8, payload="b", now=2)
    assert second["accepted"] is True
    assert second["high_water"] == 10
    third = g.on_packet(active, 0, 300, payload="c", now=3)
    assert third["accepted"] is True
    assert third["high_water"] == 300
    assert g.stats()["packets"] == 3
    assert g.stats()["accepted"] == 3
    assert g.stats()["dropped"] == 0


def test_duplicate_packets_are_dropped():
    g = guard()
    active = session_with_path(g, "s1")
    assert g.on_packet(active, 0, 5, payload="x")["accepted"] is True
    duplicate = g.on_packet(active, 0, 5, payload="x")
    assert duplicate["accepted"] is False
    assert duplicate["reason"] == "duplicate"
    assert g.on_packet(active, 0, 6, payload="x")["accepted"] is True
    assert g.stats()["packets"] == 3
    assert g.stats()["accepted"] == 2
    assert g.stats()["dropped"] == 1


def test_packets_older_than_reorder_window_are_dropped():
    g = guard(reorder_window=64)
    active = session_with_path(g, "s1")
    assert g.on_packet(active, 0, 100, payload="a")["accepted"] is True
    old = g.on_packet(active, 0, 20, payload="b")
    assert old["accepted"] is False
    assert old["reason"] == "too_old"
    edge = g.on_packet(active, 0, 40, payload="c")
    assert edge["accepted"] is True
    assert edge["high_water"] == 100


def test_retiring_path_is_honoured_only_inside_grace():
    g = guard(grace_ms=100)
    session_with_path(g, "s1")
    g.probe_path("s1", NEW_ADDR, ticket=g.issue_ticket("s1", now=0)["ticket"])
    g.complete_migration("s1", now=0)

    inside = g.on_packet("s1/p0", 0, 1, payload="a", now=50)
    assert inside["accepted"] is True
    outside = g.on_packet("s1/p0", 0, 2, payload="b", now=200)
    assert outside["accepted"] is False
    assert outside["reason"] == "stale_epoch"
    assert g.stats()["dropped"] == 1


def test_samples_feed_estimates_and_keep_previous_value():
    g = guard()
    active = session_with_path(g, "s1")
    with pytest.raises(mg.MigrationError) as exc:
        g.reset_estimates("s1")
    assert exc.value.code == "MIGRATION_NO_SAMPLES"

    g.record_sample(active, 40, now=1)
    g.record_sample(active, 50, now=2)
    g.record_sample(active, 60, now=3)
    first = g.reset_estimates("s1", now=4)
    assert first["path"] == active
    assert first["rtt_ms"] == 50
    assert first["previous_rtt_ms"] is None
    assert first["samples"] == [40, 50, 60]
    assert g.path_state("s1")["rtt_ms"] == 50

    g.record_sample(active, 80, now=5)
    g.record_sample(active, 101, now=6)
    second = g.reset_estimates("s1", now=7)
    assert second["samples"] == [40, 50, 60, 80, 101]
    assert second["rtt_ms"] == 66
    assert second["previous_rtt_ms"] == 50
    assert g.path_state("s1")["previous_rtt_ms"] == 50

    with pytest.raises(mg.MigrationError) as exc:
        g.record_sample("s1/p9", 10)
    assert exc.value.code == "MIGRATION_UNKNOWN_PATH"
    with pytest.raises(mg.MigrationError) as exc:
        g.record_sample(active, 0)
    assert exc.value.code == "MIGRATION_INVALID"


def test_rollback_returns_to_previous_path_and_retires_candidate():
    g = guard(cooldown_ms=0)
    session_with_path(g, "s1")
    g.probe_path("s1", NEW_ADDR, ticket=g.issue_ticket("s1", now=0)["ticket"])
    g.complete_migration("s1", now=10)
    g.record_sample("s1/p1", 90, now=11)
    assert g.reset_estimates("s1", now=12)["rtt_ms"] == 90

    out = g.rollback("s1", now=20)
    assert out["rolled_back"] is True
    assert out["active_path"] == "s1/p0"
    assert out["epoch"] == 0
    assert out["rtt_ms"] == 90

    state = g.path_state("s1")
    paths = {item["path"]: item for item in state["paths"]}
    assert paths["s1/p0"]["state"] == "active"
    assert paths["s1/p1"]["state"] == "abandoned"
    assert state["pending_path"] is None
    abandoned = g.on_packet("s1/p1", 1, 1, payload="a", now=30)
    assert abandoned["accepted"] is False
    assert abandoned["reason"] == "stale_epoch"
    with pytest.raises(mg.MigrationError) as exc:
        g.rollback("s1", now=40)
    assert exc.value.code == "MIGRATION_NO_ROLLBACK"


def test_snapshot_restore_continues_epoch_deduplication():
    g = guard(cooldown_ms=0)
    session_with_path(g, "s1")
    g.probe_path("s1", NEW_ADDR, ticket=g.issue_ticket("s1", now=0)["ticket"])
    g.complete_migration("s1", now=10)
    assert g.on_packet("s1/p1", 1, 1, payload="p", now=20)["accepted"] is True

    state = g.snapshot()
    assert state["version"] == 1
    pristine = copy.deepcopy(state)
    state["stats"]["packets"] = 999
    assert g.stats()["packets"] == 1

    restored = guard()
    restored.restore(pristine)
    assert restored.path_state("s1")["active_path"] == "s1/p1"
    replay = restored.on_packet("s1/p1", 1, 1, payload="p", now=30)
    assert replay["accepted"] is False
    assert replay["reason"] == "duplicate"
    assert restored.on_packet("s1/p1", 1, 2, payload="q", now=31)["accepted"] is True

    bad_states = [
        None,
        {},
        {"version": 2, "sessions": {}, "tickets": {}, "stats": {},
         "seq": 0, "ticket_seq": 0},
        {"version": 1, "sessions": "nope", "tickets": {}, "stats": {},
         "seq": 0, "ticket_seq": 0},
        {"version": 1, "sessions": {}, "tickets": "nope", "stats": {},
         "seq": 0, "ticket_seq": 0},
    ]
    for bad in bad_states:
        with pytest.raises(mg.MigrationError) as exc:
            restored.restore(bad)
        assert exc.value.code == "MIGRATION_SNAPSHOT_INVALID"
    assert restored.stats()["packets"] == 3


def test_concurrent_sessions_keep_packet_counts_exact():
    g = guard()
    for index in range(6):
        g.create_session(f"s{index}", addr=ADDR)
    errors = []

    def worker(index):
        try:
            path = f"s{index}/p0"
            for seq in range(1, 21):
                g.on_packet(path, 0, seq, payload=f"{index}-{seq}",
                            now=seq)
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append((index, repr(exc)))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert g.stats()["sessions"] == 6
    assert g.stats()["packets"] == 120
    assert g.stats()["accepted"] == 120
    assert g.stats()["dropped"] == 0


def test_unknown_path_and_epoch_mismatch_are_reported():
    g = guard()
    active = session_with_path(g, "s1")
    with pytest.raises(mg.MigrationError) as exc:
        g.on_packet("s1/p9", 0, 1, payload="a")
    assert exc.value.code == "MIGRATION_UNKNOWN_PATH"
    mismatch = g.on_packet(active, 5, 1, payload="a", now=1)
    assert mismatch["accepted"] is False
    assert mismatch["reason"] == "epoch_mismatch"
    assert g.stats()["packets"] == 1
    assert g.stats()["dropped"] == 1
