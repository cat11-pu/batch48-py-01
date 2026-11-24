import threading

import pytest

import esp_replay_guard as eg

MAX_SEQ = (1 << 64) - 1


def make(**kwargs):
    base = {"default_window": 64, "max_report_records": 256}
    base.update(kwargs)
    return eg.EspReplayGuard(**base)


def with_sa(window_size=64, **kwargs):
    guard = make()
    guard.add_sa("sa-1", spi=0x1001, window_size=window_size, now=0.0, **kwargs)
    return guard


def test_add_sa_returns_descriptor_and_lists_in_order():
    guard = make()
    descriptor = guard.add_sa("sa-1", spi=0x1001, window_size=8, now=0.0)
    assert descriptor["sa_id"] == "sa-1"
    assert descriptor["spi"] == 0x1001
    assert descriptor["window_size"] == 8
    guard.add_sa("sa-2", spi=0x1002, window_size=16, now=0.0)
    assert guard.list_sas() == ["sa-1", "sa-2"]


def test_in_order_sequence_updates_state_and_counters():
    guard = with_sa(window_size=8)
    assert guard.accept("sa-1", 1, now=1.0) is True
    assert guard.accept("sa-1", 2, now=1.0) is True
    assert guard.accept("sa-1", 3, now=1.0) is True
    state = guard.sa_state("sa-1")
    assert state["highest_seq"] == 3
    assert state["accepted"] == 3
    assert state["replayed"] == 0
    assert state["too_old"] == 0
    assert guard.stats()["accepted"] == 3


def test_duplicate_sequence_is_counted_as_replay():
    guard = with_sa()
    assert guard.accept("sa-1", 5, now=1.0) is True
    assert guard.accept("sa-1", 5, now=1.0) is False
    state = guard.sa_state("sa-1")
    assert state["replayed"] == 1
    assert state["accepted"] == 1
    assert guard.stats()["replayed"] == 1


def test_out_of_order_sequence_inside_window_is_accepted():
    guard = with_sa(window_size=8)
    assert guard.accept("sa-1", 10, now=1.0) is True
    assert guard.accept("sa-1", 8, now=1.0) is True
    assert guard.accept("sa-1", 9, now=1.0) is True
    assert guard.stats()["accepted"] == 3
    assert guard.accept("sa-1", 9, now=1.0) is False
    assert guard.stats()["replayed"] == 1


def test_below_left_edge_is_counted_as_too_old():
    guard = with_sa(window_size=4)
    assert guard.accept("sa-1", 10, now=1.0) is True
    assert guard.accept("sa-1", 6, now=1.0) is False
    assert guard.sa_state("sa-1")["too_old"] == 1
    assert guard.accept("sa-1", 7, now=1.0) is True
    assert guard.accept("sa-1", 8, now=1.0) is True
    assert guard.stats()["accepted"] == 3
    assert guard.stats()["too_old"] == 1


def test_window_advance_moves_left_edge_and_keeps_seen_marks():
    guard = with_sa(window_size=8)
    for seq in range(1, 11):
        assert guard.accept("sa-1", seq, now=1.0) is True
    assert guard.accept("sa-1", 2, now=1.0) is False
    assert guard.accept("sa-1", 3, now=1.0) is False
    state = guard.sa_state("sa-1")
    assert state["too_old"] == 1
    assert state["replayed"] == 1
    assert guard.accept("sa-1", 11, now=1.0) is True
    assert guard.accept("sa-1", 3, now=1.0) is False
    assert guard.sa_state("sa-1")["too_old"] == 2


def test_window_shrink_does_not_reaccept_seen_sequence():
    guard = with_sa(window_size=64)
    assert guard.accept("sa-1", 100, now=1.0) is True
    assert guard.accept("sa-1", 101, now=1.0) is True
    guard.set_window_size("sa-1", 2)
    assert guard.sa_state("sa-1")["window_size"] == 2
    assert guard.accept("sa-1", 100, now=1.0) is False
    assert guard.accept("sa-1", 99, now=1.0) is False
    assert guard.sa_state("sa-1")["replayed"] == 1
    assert guard.sa_state("sa-1")["too_old"] == 1


def test_window_growth_widens_reordering_tolerance():
    guard = with_sa(window_size=2)
    assert guard.accept("sa-1", 10, now=1.0) is True
    assert guard.accept("sa-1", 11, now=1.0) is True
    assert guard.accept("sa-1", 5, now=1.0) is False
    assert guard.sa_state("sa-1")["too_old"] == 1
    guard.set_window_size("sa-1", 8)
    assert guard.accept("sa-1", 5, now=1.0) is True
    assert guard.stats()["accepted"] == 3


def test_invalid_arguments_are_rejected():
    guard = make()
    with pytest.raises(eg.EspError) as exc:
        guard.add_sa("", spi=1, now=0.0)
    assert exc.value.code == "ESP_INVALID_ARGUMENT"
    with pytest.raises(eg.EspError) as exc:
        guard.add_sa("sa-x", spi=-1, now=0.0)
    assert exc.value.code == "ESP_INVALID_ARGUMENT"
    with pytest.raises(eg.EspError) as exc:
        guard.add_sa("sa-x", spi=1 << 32, now=0.0)
    assert exc.value.code == "ESP_INVALID_ARGUMENT"
    with pytest.raises(eg.EspError) as exc:
        guard.add_sa("sa-x", spi=1, window_size=0, now=0.0)
    assert exc.value.code == "ESP_INVALID_ARGUMENT"
    with pytest.raises(eg.EspError) as exc:
        guard.add_sa("sa-x", spi=1, soft_lifetime=100.0, hard_lifetime=50.0, now=0.0)
    assert exc.value.code == "ESP_INVALID_ARGUMENT"
    with pytest.raises(eg.EspError) as exc:
        guard.add_sa("sa-x", spi=1, max_bytes=0, now=0.0)
    assert exc.value.code == "ESP_INVALID_ARGUMENT"
    with pytest.raises(eg.EspError) as exc:
        guard.accept("missing-sa", 1, now=1.0)
    assert exc.value.code == "ESP_UNKNOWN_SA"
    guard.add_sa("sa-1", spi=7, window_size=8, now=0.0)
    for bad_seq in (-1, 1 << 64):
        with pytest.raises(eg.EspError) as exc:
            guard.accept("sa-1", bad_seq, now=1.0)
        assert exc.value.code == "ESP_INVALID_SEQ"
    with pytest.raises(eg.EspError) as exc:
        guard.accept("sa-1", 1, length=-1, now=1.0)
    assert exc.value.code == "ESP_INVALID_ARGUMENT"


def test_remove_sa_makes_later_packets_unknown():
    guard = with_sa()
    assert guard.remove_sa("sa-1") is True
    assert guard.remove_sa("sa-1") is False
    assert guard.list_sas() == []
    with pytest.raises(eg.EspError) as exc:
        guard.accept("sa-1", 1, now=1.0)
    assert exc.value.code == "ESP_UNKNOWN_SA"


def test_byte_budget_deletes_the_sa():
    guard = with_sa(max_bytes=1000)
    assert guard.accept("sa-1", 1, length=600, now=1.0) is True
    assert guard.sa_state("sa-1")["bytes_used"] == 600
    with pytest.raises(eg.EspError) as exc:
        guard.accept("sa-1", 2, length=500, now=1.0)
    assert exc.value.code == "ESP_SA_BYTE_LIMIT"
    assert guard.list_sas() == []
    assert guard.stats()["byte_limited"] == 1


def test_hard_lifetime_expiry_deletes_the_sa():
    guard = with_sa(hard_lifetime=100.0)
    assert guard.accept("sa-1", 1, now=50.0) is True
    with pytest.raises(eg.EspError) as exc:
        guard.accept("sa-1", 2, now=100.0)
    assert exc.value.code == "ESP_SA_EXPIRED"
    assert guard.list_sas() == []
    assert guard.stats()["expired"] == 1


def test_sequence_exhaustion_after_64_bit_top():
    guard = with_sa(window_size=16)
    assert guard.accept("sa-1", MAX_SEQ, now=1.0) is True
    with pytest.raises(eg.EspError) as exc:
        guard.accept("sa-1", 1, now=1.0)
    assert exc.value.code == "ESP_SEQ_EXHAUSTED"
    assert guard.stats()["exhausted"] == 1


def test_snapshot_restore_keeps_window_and_counters():
    guard = with_sa(window_size=8)
    for seq in range(1, 6):
        assert guard.accept("sa-1", seq, now=1.0) is True
    state = guard.snapshot()
    assert state["version"] == 1
    restored = make()
    restored.restore(state)
    assert restored.stats() == guard.stats()
    assert restored.sa_state("sa-1")["highest_seq"] == 5
    assert restored.sa_state("sa-1")["bytes_used"] == 0
    assert restored.accept("sa-1", 5, now=2.0) is False
    assert restored.accept("sa-1", 6, now=2.0) is True
    with pytest.raises(eg.EspError) as exc:
        restored.restore({"version": 9})
    assert exc.value.code == "ESP_SNAPSHOT_INVALID"
    assert restored.sa_state("sa-1")["highest_seq"] == 6


def test_concurrent_duplicates_accept_exactly_once_per_sequence():
    guard = with_sa(window_size=64)
    accepted = {}
    guard_lock = threading.Lock()
    failures = []

    def worker(_index):
        try:
            for seq in range(1, 11):
                ok = guard.accept("sa-1", seq, now=1.0)
                with guard_lock:
                    accepted[seq] = accepted.get(seq, 0) + (1 if ok else 0)
        except Exception as exc:  # pragma: no cover - surfaced through failures
            failures.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert sorted(accepted) == list(range(1, 11))
    assert all(count == 1 for count in accepted.values())
    assert guard.stats()["accepted"] == 10
    assert guard.stats()["replayed"] == 70
