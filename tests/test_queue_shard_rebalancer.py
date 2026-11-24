import copy
import threading

import pytest

import queue_shard_rebalancer as qs


def make(**kwargs):
    return qs.QueueShardRebalancer(**kwargs)


def seed_scores(alloc, scores):
    for index, score in enumerate(scores, start=1):
        alloc.add_ticket(f"p{index}", score=score, at=index * 10)


def test_add_ticket_places_ticket_and_reports_shard_state():
    alloc = make(shard_capacity=4)
    out = alloc.add_ticket("p1", score=10, at=3)
    assert out == {"player": "p1", "score": 10, "seq": 1, "at": 3, "shard": "s1"}
    alloc.add_ticket("p2", score=20, at=5)

    assert alloc.shard_state("s1") == {
        "shard": "s1",
        "count": 2,
        "players": ["p1", "p2"],
        "tickets": [
            {"player": "p1", "score": 10, "seq": 1, "at": 3},
            {"player": "p2", "score": 20, "seq": 2, "at": 5},
        ],
        "in_flight": False,
        "min_score": 10,
        "max_score": 20,
    }
    assert alloc.ticket_info("p2") == {
        "player": "p2", "score": 20, "seq": 2, "at": 5, "shard": "s1",
    }
    assert alloc.shard_of("p1") == "s1"
    assert alloc.locate("p1") == {"player": "p1", "shard": "s1", "copies": 1}
    assert alloc.stats()["added"] == 2


def test_add_ticket_rejects_duplicate_bad_inputs_and_full_queue():
    alloc = make(max_shards=1, shard_capacity=2)
    alloc.add_ticket("a1", score=10)
    alloc.add_ticket("a2", score=20)

    with pytest.raises(qs.ShardError) as exc:
        alloc.add_ticket("a3", score=30)
    assert exc.value.code == "SHARD_QUEUE_FULL"

    with pytest.raises(qs.ShardError) as exc:
        alloc.add_ticket("a1", score=40)
    assert exc.value.code == "SHARD_DUPLICATE_TICKET"

    with pytest.raises(qs.ShardError) as exc:
        alloc.add_ticket("a4", score="high")
    assert exc.value.code == "SHARD_INVALID_SCORE"

    with pytest.raises(qs.ShardError) as exc:
        alloc.add_ticket("a4", score=10, at=-1)
    assert exc.value.code == "SHARD_INVALID_TIME"

    assert alloc.shard_state("s1")["count"] == 2
    assert alloc.stats()["rejected"] == 4


def test_remove_ticket_frees_shard_and_unknown_lookups_fail():
    alloc = make(shard_capacity=4)
    alloc.add_ticket("p1", score=10, at=1)
    alloc.add_ticket("p2", score=20, at=2)

    out = alloc.remove_ticket("p1")
    assert out == {"player": "p1", "shard": "s1", "score": 10, "seq": 1}
    assert alloc.shard_state("s1")["players"] == ["p2"]

    with pytest.raises(qs.ShardError) as exc:
        alloc.remove_ticket("p1")
    assert exc.value.code == "SHARD_UNKNOWN_TICKET"

    with pytest.raises(qs.ShardError) as exc:
        alloc.shard_of("ghost")
    assert exc.value.code == "SHARD_UNKNOWN_TICKET"

    with pytest.raises(qs.ShardError) as exc:
        alloc.ticket_info("ghost")
    assert exc.value.code == "SHARD_UNKNOWN_TICKET"

    assert alloc.stats()["removed"] == 1
    assert alloc.stats()["rejected"] == 3


def test_split_shard_divides_by_score_and_keeps_sequence_order():
    alloc = make(max_shards=6, shard_capacity=8)
    seed_scores(alloc, [10, 20, 30, 40, 50])

    out = alloc.split_shard("s1")
    assert out == {"shard": "s1", "left": "s1", "right": "s2", "moved": 3}
    assert alloc.shard_state("s1")["players"] == ["p1", "p2"]
    assert alloc.shard_state("s2")["players"] == ["p3", "p4", "p5"]
    assert alloc.shard_state("s2")["min_score"] == 30
    assert alloc.shard_state("s2")["max_score"] == 50
    assert alloc.shard_of("p4") == "s2"
    assert alloc.ticket_info("p3")["shard"] == "s2"
    assert alloc.locate("p5") == {"player": "p5", "shard": "s2", "copies": 1}
    assert alloc.stats()["splits"] == 1
    assert alloc.stats()["migrations"] == 0


def test_split_shard_rejects_small_shard_limit_and_unknown_id():
    small = make(shard_capacity=8)
    small.add_ticket("p1", score=10)
    with pytest.raises(qs.ShardError) as exc:
        small.split_shard("s1")
    assert exc.value.code == "SHARD_TOO_SMALL"

    limited = make(max_shards=1, shard_capacity=8)
    limited.add_ticket("p1", score=10)
    limited.add_ticket("p2", score=20)
    with pytest.raises(qs.ShardError) as exc:
        limited.split_shard("s1")
    assert exc.value.code == "SHARD_LIMIT"

    with pytest.raises(qs.ShardError) as exc:
        limited.split_shard("nope")
    assert exc.value.code == "SHARD_UNKNOWN_SHARD"


def test_merge_shards_orders_by_sequence_and_drops_absorbed_shard():
    alloc = make(max_shards=6, shard_capacity=8)
    seed_scores(alloc, [10, 20, 30, 40, 50])
    alloc.split_shard("s1")

    out = alloc.merge_shards("s1", "s2")
    assert out == {
        "survivor": "s1",
        "absorbed": "s2",
        "count": 5,
        "tickets": ["p1", "p2", "p3", "p4", "p5"],
    }
    state = alloc.shard_state("s1")
    assert state["players"] == ["p1", "p2", "p3", "p4", "p5"]
    assert state["count"] == 5
    assert alloc.ticket_info("p5")["shard"] == "s1"

    with pytest.raises(qs.ShardError) as exc:
        alloc.shard_state("s2")
    assert exc.value.code == "SHARD_UNKNOWN_SHARD"
    assert alloc.stats()["merges"] == 1


def test_merge_shards_rejects_same_unknown_and_overflow():
    alloc = make(shard_capacity=4)
    seed_scores(alloc, [10, 20, 30, 40, 50])

    with pytest.raises(qs.ShardError) as exc:
        alloc.merge_shards("s1", "s1")
    assert exc.value.code == "SHARD_SAME_SHARD"

    with pytest.raises(qs.ShardError) as exc:
        alloc.merge_shards("s1", "nope")
    assert exc.value.code == "SHARD_UNKNOWN_SHARD"

    with pytest.raises(qs.ShardError) as exc:
        alloc.merge_shards("s1", "s2")
    assert exc.value.code == "SHARD_FULL"

    assert alloc.shard_state("s1")["count"] == 4
    assert alloc.shard_state("s2")["count"] == 1
    assert alloc.stats()["merges"] == 0
    assert alloc.stats()["rejected"] == 3


def test_migrate_ticket_moves_between_shards_atomically():
    alloc = make(shard_capacity=4)
    seed_scores(alloc, [10, 20, 30, 40, 50])

    out = alloc.migrate_ticket("p1", "s2")
    assert out == {"player": "p1", "from": "s1", "to": "s2", "seq": 1}
    assert alloc.shard_state("s1")["count"] == 3
    assert alloc.shard_state("s2")["count"] == 2
    assert alloc.shard_state("s2")["players"] == ["p1", "p5"]
    assert alloc.shard_of("p1") == "s2"
    assert alloc.locate("p1") == {"player": "p1", "shard": "s2", "copies": 1}
    assert alloc.stats()["migrations"] == 1


def test_migrate_ticket_rejects_unknown_same_and_full_target():
    alloc = make(shard_capacity=2)
    alloc.add_ticket("p1", score=10)
    alloc.add_ticket("p2", score=20)
    alloc.add_ticket("p3", score=30)

    with pytest.raises(qs.ShardError) as exc:
        alloc.migrate_ticket("ghost", "s1")
    assert exc.value.code == "SHARD_UNKNOWN_TICKET"

    with pytest.raises(qs.ShardError) as exc:
        alloc.migrate_ticket("p1", "nope")
    assert exc.value.code == "SHARD_UNKNOWN_SHARD"

    with pytest.raises(qs.ShardError) as exc:
        alloc.migrate_ticket("p1", "s1")
    assert exc.value.code == "SHARD_ALREADY_THERE"

    with pytest.raises(qs.ShardError) as exc:
        alloc.migrate_ticket("p3", "s1")
    assert exc.value.code == "SHARD_FULL"

    assert alloc.shard_of("p3") == "s2"
    assert alloc.stats()["migrations"] == 0
    assert alloc.stats()["rejected"] == 4


def test_in_flight_match_blocks_split_merge_and_migration():
    alloc = make(max_shards=6, shard_capacity=8)
    seed_scores(alloc, [10, 20, 30, 40])
    alloc.split_shard("s1")
    assert alloc.begin_match("s2") == {"shard": "s2", "in_flight": True}
    assert alloc.shard_state("s2")["in_flight"] is True

    with pytest.raises(qs.ShardError) as exc:
        alloc.split_shard("s2")
    assert exc.value.code == "SHARD_BUSY"

    with pytest.raises(qs.ShardError) as exc:
        alloc.merge_shards("s1", "s2")
    assert exc.value.code == "SHARD_BUSY"

    with pytest.raises(qs.ShardError) as exc:
        alloc.migrate_ticket("p1", "s2")
    assert exc.value.code == "SHARD_BUSY"

    with pytest.raises(qs.ShardError) as exc:
        alloc.begin_match("s2")
    assert exc.value.code == "SHARD_ALREADY_IN_FLIGHT"

    assert alloc.end_match("s2") == {"shard": "s2", "in_flight": False}
    assert alloc.split_shard("s2") == {"shard": "s2", "left": "s2", "right": "s3", "moved": 1}

    with pytest.raises(qs.ShardError) as exc:
        alloc.end_match("s2")
    assert exc.value.code == "SHARD_NOT_IN_FLIGHT"

    with pytest.raises(qs.ShardError) as exc:
        alloc.begin_match("nope")
    assert exc.value.code == "SHARD_UNKNOWN_SHARD"

    assert alloc.stats()["matches_started"] == 1
    assert alloc.stats()["rejected"] == 6


def test_rebalance_merges_underfilled_shards():
    alloc = make(max_shards=6, shard_capacity=8)
    seed_scores(alloc, [10, 20, 30, 40])
    alloc.split_shard("s1")

    out = alloc.rebalance()
    assert out == {
        "actions": [{"op": "merge", "survivor": "s1", "absorbed": "s2"}],
        "shards": 1,
    }
    assert alloc.shard_state("s1")["players"] == ["p1", "p2", "p3", "p4"]
    with pytest.raises(qs.ShardError) as exc:
        alloc.shard_state("s2")
    assert exc.value.code == "SHARD_UNKNOWN_SHARD"
    assert alloc.stats()["merges"] == 1
    assert alloc.stats()["rebalances"] == 1


def test_rebalance_migrates_from_largest_to_smallest_shard():
    alloc = make(max_shards=6, shard_capacity=4)
    seed_scores(alloc, [50, 10, 40, 20, 30])
    assert alloc.shard_state("s1")["count"] == 4
    assert alloc.shard_state("s2")["count"] == 1

    out = alloc.rebalance()
    assert out == {
        "actions": [{"op": "migrate", "player": "p4", "from": "s1", "to": "s2"}],
        "shards": 2,
    }
    assert alloc.shard_state("s1")["count"] == 3
    assert alloc.shard_state("s2")["count"] == 2
    assert alloc.shard_of("p4") == "s2"
    assert alloc.rebalance() == {"actions": [], "shards": 2}
    assert alloc.stats()["rebalances"] == 2
    assert alloc.stats()["migrations"] == 1


def test_stats_counters_across_mixed_operations_and_reset():
    alloc = make(max_shards=6, shard_capacity=8)
    seed_scores(alloc, [10, 20, 30, 40, 50])
    alloc.split_shard("s1")
    alloc.migrate_ticket("p1", "s2")
    alloc.merge_shards("s1", "s2")
    alloc.remove_ticket("p2")
    alloc.begin_match("s1")
    alloc.end_match("s1")
    with pytest.raises(qs.ShardError):
        alloc.remove_ticket("ghost")

    assert alloc.stats() == {
        "added": 5,
        "removed": 1,
        "splits": 1,
        "merges": 1,
        "migrations": 1,
        "rebalances": 0,
        "matches_started": 1,
        "rejected": 1,
    }
    alloc.reset_stats()
    assert alloc.stats() == {key: 0 for key in qs.STATS_KEYS}


def test_snapshot_restore_roundtrip_and_rejects_corrupt_state():
    alloc = make(max_shards=6, shard_capacity=8)
    seed_scores(alloc, [10, 20, 30, 40, 50])
    alloc.split_shard("s1")
    alloc.begin_match("s2")

    snap = alloc.snapshot()
    assert snap["version"] == 1
    corrupted = copy.deepcopy(snap)
    corrupted["shards"]["s1"][0]["score"] = 999
    assert alloc.ticket_info("p1")["score"] == 10

    fresh = make(max_shards=6, shard_capacity=8)
    fresh.restore(copy.deepcopy(snap))
    assert fresh.shard_state("s1") == alloc.shard_state("s1")
    assert fresh.shard_state("s2") == alloc.shard_state("s2")
    assert fresh.shard_state("s2")["in_flight"] is True
    assert fresh.shard_of("p5") == "s2"
    assert fresh.locate("p3") == {"player": "p3", "shard": "s2", "copies": 1}
    assert fresh.stats() == alloc.stats()

    bad_states = [
        None,
        {},
        {**copy.deepcopy(snap), "version": 2},
        {**copy.deepcopy(snap), "shards": "nope"},
        {**copy.deepcopy(snap), "shard_order": ["s1"]},
        {**copy.deepcopy(snap), "in_flight": ["s9"]},
        {**copy.deepcopy(snap), "stats": {"added": 1}},
        {**copy.deepcopy(snap), "seq": 0},
    ]
    duplicate = copy.deepcopy(snap)
    duplicate["shards"]["s2"].append(
        {"player": "p1", "score": 10, "seq": 6, "at": 9})
    bad_states.append(duplicate)
    broken_ticket = copy.deepcopy(snap)
    broken_ticket["shards"]["s1"][0].pop("score")
    bad_states.append(broken_ticket)
    overflow = copy.deepcopy(snap)
    overflow["shards"]["s1"] = [
        {"player": f"x{i}", "score": i, "seq": 100 + i, "at": 0} for i in range(9)
    ]
    bad_states.append(overflow)

    for state in bad_states:
        with pytest.raises(qs.ShardError) as exc:
            fresh.restore(state)
        assert exc.value.code == "SHARD_SNAPSHOT_INVALID"

    assert fresh.shard_state("s1")["count"] == 2
    assert fresh.shard_state("s2")["count"] == 3
    assert fresh.shard_state("s2")["in_flight"] is True
    assert fresh.stats()["splits"] == 1


def test_concurrent_migrations_keep_single_ownership_and_exact_counts():
    alloc = make(max_shards=8, shard_capacity=8)
    seed_scores(alloc, [60, 50, 40, 30, 20, 10])
    alloc.split_shard("s1")
    names = [f"p{index}" for index in range(1, 7)]
    errors = []

    def migrator(name):
        try:
            for _ in range(30):
                current = alloc.shard_of(name)
                target = "s2" if current == "s1" else "s1"
                alloc.migrate_ticket(name, target)
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append(repr(exc))

    def reader():
        try:
            for _ in range(100):
                for name in names:
                    if alloc.locate(name)["copies"] != 1:
                        errors.append(f"duplicate copy for {name}")
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append(repr(exc))

    threads = [threading.Thread(target=migrator, args=(name,)) for name in names]
    threads += [threading.Thread(target=reader) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert alloc.shard_state("s1")["count"] + alloc.shard_state("s2")["count"] == 6
    for name in names:
        assert alloc.locate(name)["copies"] == 1
    assert alloc.stats()["migrations"] == 180
    assert alloc.stats()["rejected"] == 0
