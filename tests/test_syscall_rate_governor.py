import threading

import pytest

import syscall_rate_governor as rg


def make(**kwargs):
    return rg.SyscallRateGovernor(**kwargs)


def test_configure_validates_and_echoes():
    gov = make()
    info = gov.configure("web", rate=2.0, burst=2.0, now=0)
    assert info["group"] == "web"
    assert info["rate"] == 2.0
    assert info["burst"] == 2.0
    assert info["tokens"] == 2.0
    assert info["calls"] is None

    with pytest.raises(rg.RateGovernorError) as exc:
        gov.configure("", rate=1.0, burst=1.0)
    assert exc.value.code == "RATE_GROUP_INVALID"

    with pytest.raises(rg.RateGovernorError) as exc:
        gov.configure("api", rate=0.0, burst=1.0)
    assert exc.value.code == "RATE_CONFIG_INVALID"

    with pytest.raises(rg.RateGovernorError) as exc:
        gov.configure("api", rate=1.0, burst=0.0)
    assert exc.value.code == "RATE_CONFIG_INVALID"


def test_configure_limits_and_unknown_group():
    gov = make()
    gov.configure("web", rate=1.0, burst=1.0)
    with pytest.raises(rg.RateGovernorError) as exc:
        gov.configure("bad", rate=1.0, burst=1.0, calls=["Read"])
    assert exc.value.code == "RATE_CONFIG_INVALID"

    with pytest.raises(rg.RateGovernorError) as exc:
        gov.configure("api", rate=1.0, burst=1.0, calls=[])
    assert exc.value.code == "RATE_CONFIG_INVALID"

    with pytest.raises(rg.RateGovernorError) as exc:
        gov.admit("nope", "read", now=0)
    assert exc.value.code == "RATE_UNKNOWN_GROUP"

    small = make(max_groups=1)
    small.configure("one", rate=1.0, burst=1.0)
    with pytest.raises(rg.RateGovernorError) as exc:
        small.configure("two", rate=1.0, burst=1.0)
    assert exc.value.code == "RATE_GROUP_LIMIT"


def test_admit_consumes_tokens_then_denies():
    gov = make()
    gov.configure("web", rate=2.0, burst=2.0, now=0)

    first = gov.admit("web", "read", now=0)
    assert first["allowed"] is True
    assert first["group"] == "web"
    assert first["call"] == "read"
    assert first["reason"] == "ok"
    assert first["remaining"] == 1.0

    second = gov.admit("web", "read", now=0)
    assert second["allowed"] is True
    assert second["remaining"] == 0.0

    third = gov.admit("web", "read", now=0)
    assert third["allowed"] is False
    assert third["reason"] == "limit"
    assert third["remaining"] == 0.0

    stats = gov.stats("web")
    assert stats["group"] == "web"
    assert stats["admitted"] == 2
    assert stats["denied"] == 1


def test_fractional_refill_without_second_rounding():
    gov = make()
    gov.configure("web", rate=2.0, burst=2.0, now=0)
    assert gov.admit("web", "read", now=0)["allowed"] is True
    assert gov.admit("web", "read", now=0)["allowed"] is True

    half = gov.admit("web", "read", now=0.5)
    assert half["allowed"] is True
    assert half["remaining"] == 0.0

    assert gov.admit("web", "read", now=0.75)["allowed"] is False
    assert gov.admit("web", "read", now=0.9)["allowed"] is False
    assert gov.admit("web", "read", now=1.0)["allowed"] is True


def test_burst_cap_after_long_idle():
    gov = make()
    gov.configure("web", rate=1.0, burst=3.0, now=0)
    decision = gov.admit("web", "read", now=1000)
    assert decision["allowed"] is True
    assert decision["remaining"] == 2.0

    refilled = gov.refill("web", now=2000)
    assert refilled["tokens"] == 3.0
    assert refilled["added"] == 1.0


def test_denied_calls_do_not_consume_or_count_admitted():
    gov = make()
    gov.configure("web", rate=1.0, burst=1.0, now=0)
    assert gov.admit("web", "read", now=0)["allowed"] is True

    denied = gov.admit("web", "read", now=0)
    assert denied["allowed"] is False
    assert denied["remaining"] == 0.0

    stats = gov.stats("web")
    assert stats["admitted"] == 1
    assert stats["denied"] == 1
    assert gov.refill("web", now=0)["added"] == 0.0

    assert gov.admit("web", "read", now=1)["allowed"] is True
    assert gov.stats("web")["admitted"] == 2


def test_unknown_call_rejected_by_allowlist():
    gov = make()
    gov.configure("web", rate=1.0, burst=2.0, calls=["read", "write"], now=0)

    denied = gov.admit("web", "exec", now=0)
    assert denied["allowed"] is False
    assert denied["reason"] == "unknown_call"
    assert denied["remaining"] == 2.0

    assert gov.admit("web", "read", now=0)["allowed"] is True
    assert gov.admit("web", "write", now=0)["allowed"] is True
    assert gov.admit("web", "read", now=0)["allowed"] is False


def test_clock_backwards_rejected():
    gov = make()
    gov.configure("web", rate=1.0, burst=1.0, now=10)
    assert gov.admit("web", "read", now=10)["allowed"] is True

    with pytest.raises(rg.RateGovernorError) as exc:
        gov.admit("web", "read", now=9)
    assert exc.value.code == "RATE_CLOCK_BACKWARDS"

    with pytest.raises(rg.RateGovernorError) as exc:
        gov.refill("web", now=9)
    assert exc.value.code == "RATE_CLOCK_BACKWARDS"


def test_explicit_refill_is_idempotent():
    gov = make()
    gov.configure("web", rate=1.0, burst=2.0, now=0)
    assert gov.admit("web", "read", now=0)["remaining"] == 1.0

    first = gov.refill("web", now=10)
    assert first["tokens"] == 2.0
    assert first["added"] == 1.0
    assert first["last_refill"] == 10

    second = gov.refill("web", now=10)
    assert second["tokens"] == 2.0
    assert second["added"] == 0.0


def test_fair_pick_prefers_least_recent():
    gov = make()
    gov.configure("noisy", rate=1.0, burst=5.0, now=0)
    gov.configure("quiet", rate=1.0, burst=5.0, now=0)
    gov.configure("other", rate=1.0, burst=5.0, now=0)

    gov.admit("noisy", "read", now=5)
    assert gov.pick(["noisy", "quiet"], now=6) == "quiet"

    gov.admit("quiet", "read", now=7)
    assert gov.pick(["noisy", "quiet"], now=8) == "noisy"
    assert gov.pick(["quiet", "noisy", "other"], now=9) == "other"

    with pytest.raises(rg.RateGovernorError) as exc:
        gov.pick([], now=10)
    assert exc.value.code == "RATE_PICK_EMPTY"


def test_groups_are_isolated():
    gov = make()
    gov.configure("a", rate=1.0, burst=1.0, now=0)
    gov.configure("b", rate=1.0, burst=1.0, now=0)

    assert gov.admit("a", "read", now=0)["allowed"] is True
    assert gov.admit("b", "read", now=0)["allowed"] is True
    assert gov.admit("a", "read", now=0)["allowed"] is False

    assert gov.stats("a")["admitted"] == 1
    assert gov.stats("a")["denied"] == 1
    assert gov.stats("b")["admitted"] == 1
    assert gov.stats("b")["denied"] == 0


def test_concurrent_admit_grants_single_token():
    gov = make()
    gov.configure("web", rate=1.0, burst=1.0, now=0)
    allowed = []
    refused = []
    guard = threading.Lock()

    def worker():
        try:
            decision = gov.admit("web", "read", now=10)
            with guard:
                (allowed if decision["allowed"] else refused).append(decision)
        except Exception as exc:  # noqa: BLE001 - recorded as a failed admission
            with guard:
                refused.append(type(exc).__name__)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(allowed) == 1
    assert len(refused) == 7
    stats = gov.stats("web")
    assert stats["admitted"] == 1
    assert stats["denied"] == 7


def test_reset_restores_bucket_and_clears_counters():
    gov = make()
    gov.configure("web", rate=1.0, burst=2.0, now=0)
    gov.admit("web", "read", now=0)
    gov.admit("web", "read", now=0)
    assert gov.admit("web", "read", now=0)["allowed"] is False

    reset = gov.reset("web", now=0)
    assert reset["group"] == "web"
    assert reset["tokens"] == 2.0

    stats = gov.stats("web")
    assert stats["admitted"] == 0
    assert stats["denied"] == 0
    assert stats["tokens"] == 2.0
    assert stats["rate"] == 1.0

    again = gov.admit("web", "read", now=0)
    assert again["allowed"] is True
    assert again["remaining"] == 1.0


def test_snapshot_restore_preserves_tokens():
    gov = make()
    gov.configure("web", rate=2.0, burst=2.0, now=0)
    gov.admit("web", "read", now=0)
    gov.admit("web", "read", now=0)
    snap = gov.snapshot()
    assert snap["version"] == 1

    restored = make()
    restored.restore(snap)
    assert restored.tokens("web", now=0) == 0.0
    assert restored.admit("web", "read", now=0)["allowed"] is False
    assert restored.admit("web", "read", now=0.5)["allowed"] is True

    snap["groups"]["web"]["tokens"] = 99.0
    assert restored.tokens("web", now=0.5) == 0.0


def test_restore_rejects_malformed_and_remove_group():
    gov = make()
    gov.configure("a", rate=1.0, burst=1.0, now=0)
    with pytest.raises(rg.RateGovernorError) as exc:
        gov.restore({"version": 99})
    assert exc.value.code == "RATE_SNAPSHOT_INVALID"
    assert gov.groups() == ["a"]

    removed = gov.remove("a")
    assert removed["group"] == "a"
    assert gov.groups() == []
    with pytest.raises(rg.RateGovernorError) as exc:
        gov.admit("a", "read", now=0)
    assert exc.value.code == "RATE_UNKNOWN_GROUP"

    with pytest.raises(rg.RateGovernorError) as exc:
        gov.remove("ghost")
    assert exc.value.code == "RATE_UNKNOWN_GROUP"
