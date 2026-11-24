import copy
import threading

import pytest

import netcode_negotiation_guard as net


def make(**kwargs):
    settings = {
        "server_min": 5,
        "server_max": 9,
        "supported_caps": 0b10101,
    }
    settings.update(kwargs)
    return net.NetcodeNegotiator(**settings)


def test_compat_window_defaults_to_the_server_range():
    negotiator = make()
    assert negotiator.compat_window() == {"min": 5, "max": 9}
    assert negotiator.is_compatible(7) is True
    assert negotiator.is_compatible(9) is True
    assert negotiator.is_compatible(2) is False
    assert negotiator.is_compatible(12) is False


def test_negotiate_picks_the_highest_common_version():
    negotiator = make()
    result = negotiator.negotiate("conn-a", client_min=3, client_max=7,
                                  client_caps=0b00111)
    assert result == {
        "connection": "conn-a",
        "version": 7,
        "caps": 0b00101,
        "profile": "profile-1",
        "degraded": True,
        "reused": False,
    }
    newest = negotiator.negotiate("conn-b", client_min=8, client_max=12,
                                  client_caps=0b10101)
    assert newest == {
        "connection": "conn-b",
        "version": 9,
        "caps": 0b10101,
        "profile": "profile-2",
        "degraded": False,
        "reused": False,
    }
    assert negotiator.profiles() == 2
    assert negotiator.stats()["negotiations"] == 2


def test_disjoint_version_ranges_are_rejected():
    negotiator = make()
    with pytest.raises(net.NetcodeError) as exc:
        negotiator.negotiate("conn-a", client_min=10, client_max=12,
                             client_caps=0b1)
    assert exc.value.code == "NET_VERSION_UNREACHABLE"
    assert exc.value.detail == {"client": [10, 12], "server": [5, 9]}
    assert negotiator.profiles() == 0
    assert negotiator.stats()["rejected"] == 1


def test_capabilities_are_intersected_and_named_in_bit_order():
    negotiator = make()
    result = negotiator.negotiate("conn-a", client_min=5, client_max=9,
                                  client_caps=0b01110)
    assert result["caps"] == 0b00100
    assert negotiator.capability_names(result["caps"]) == ["prediction"]
    assert negotiator.capability_names(0b10101) == [
        "delta_input",
        "prediction",
        "compression",
    ]
    assert negotiator.capability_names(0) == []


def test_no_shared_capability_is_rejected():
    negotiator = make()
    with pytest.raises(net.NetcodeError) as exc:
        negotiator.negotiate("conn-a", client_min=5, client_max=9,
                             client_caps=0b01010)
    assert exc.value.code == "NET_CAP_NONE_SHARED"
    assert negotiator.profiles() == 0
    assert negotiator.stats()["rejected"] == 1


def test_invalid_arguments_and_configuration_are_refused():
    negotiator = make()
    cases = [
        lambda: negotiator.negotiate("", client_min=5, client_max=9,
                                     client_caps=1),
        lambda: negotiator.negotiate("conn", client_min=8, client_max=6,
                                     client_caps=1),
        lambda: negotiator.negotiate("conn", client_min=0, client_max=6,
                                     client_caps=1),
        lambda: negotiator.negotiate("conn", client_min=5, client_max=9,
                                     client_caps=-1),
        lambda: negotiator.is_compatible(0),
    ]
    for call in cases:
        with pytest.raises(net.NetcodeError) as exc:
            call()
        assert exc.value.code == "NET_INVALID_ARGUMENT"
    for settings in (
        {"server_min": 9, "server_max": 5},
        {"server_min": 0, "server_max": 5},
        {"server_min": 1, "server_max": 5, "supported_caps": 1 << 5},
        {"server_min": 1, "server_max": 5, "profile_limit": 0},
    ):
        with pytest.raises(net.NetcodeError) as exc:
            make(**settings)
        assert exc.value.code == "NET_INVALID_ARGUMENT"
    assert negotiator.stats()["negotiations"] == 0


def test_grace_window_lowers_the_compatible_floor():
    negotiator = make()
    assert negotiator.set_grace_window(3) == {"min": 3, "max": 9}
    assert negotiator.compat_window() == {"min": 3, "max": 9}
    result = negotiator.negotiate("conn-a", client_min=4, client_max=4,
                                  client_caps=0b1)
    assert result["version"] == 4
    assert result["degraded"] is True
    assert negotiator.is_compatible(6) is True


def test_grace_window_rejects_impossible_bounds():
    negotiator = make()
    for low in (0, 10, -4):
        with pytest.raises(net.NetcodeError) as exc:
            negotiator.set_grace_window(low)
        assert exc.value.code == "NET_GRACE_INVALID"
    assert negotiator.compat_window() == {"min": 5, "max": 9}
    negotiator.set_grace_window(2)
    assert negotiator.compat_window() == {"min": 2, "max": 9}
    assert negotiator.clear_grace_window() == {"min": 5, "max": 9}


def test_profile_lookup_matches_the_negotiation_record():
    negotiator = make(profile_limit=4)
    result = negotiator.negotiate("conn-a", client_min=5, client_max=9,
                                  client_caps=0b00101)
    assert negotiator.profile("conn-a") == {
        "connection": "conn-a",
        "version": 9,
        "caps": 0b00101,
        "profile": result["profile"],
        "degraded": False,
    }
    assert negotiator.profiles() == 1
    with pytest.raises(net.NetcodeError) as exc:
        negotiator.profile("conn-ghost")
    assert exc.value.code == "NET_UNKNOWN_CONNECTION"


def test_reconnect_with_the_same_window_reuses_the_profile():
    negotiator = make()
    first = negotiator.negotiate("conn-a", client_min=1, client_max=7,
                                 client_caps=0b00101)
    again = negotiator.reconnect("conn-a", client_min=1, client_max=7,
                                 client_caps=0b00101)
    assert again["reused"] is True
    assert again["version"] == first["version"] == 7
    assert again["profile"] == first["profile"]
    assert again["caps"] == first["caps"]
    assert negotiator.profiles() == 1
    assert negotiator.stats()["reconnects"] == 1
    assert negotiator.stats()["reused"] == 1
    with pytest.raises(net.NetcodeError) as exc:
        negotiator.reconnect("conn-ghost", client_min=5, client_max=9,
                             client_caps=1)
    assert exc.value.code == "NET_UNKNOWN_CONNECTION"


def test_dropped_connections_free_their_profile():
    negotiator = make()
    negotiator.negotiate("conn-a", client_min=5, client_max=9, client_caps=1)
    negotiator.drop("conn-a")
    assert negotiator.profiles() == 0
    assert negotiator.stats()["dropped"] == 1
    with pytest.raises(net.NetcodeError) as exc:
        negotiator.drop("conn-a")
    assert exc.value.code == "NET_UNKNOWN_CONNECTION"


def test_profile_cache_evicts_the_oldest_connection():
    negotiator = make(profile_limit=2)
    for name in ("conn-a", "conn-b", "conn-c"):
        negotiator.negotiate(name, client_min=5, client_max=9, client_caps=1)
    assert negotiator.profiles() == 2
    with pytest.raises(net.NetcodeError) as exc:
        negotiator.profile("conn-a")
    assert exc.value.code == "NET_UNKNOWN_CONNECTION"
    assert negotiator.profile("conn-c")["profile"] == "profile-3"


def test_stats_track_outcomes_and_reset_keeps_profiles():
    negotiator = make(profile_limit=4)
    negotiator.negotiate("conn-a", client_min=3, client_max=7,
                         client_caps=0b00101)
    negotiator.reconnect("conn-a", client_min=3, client_max=7,
                         client_caps=0b00101)
    with pytest.raises(net.NetcodeError):
        negotiator.negotiate("conn-b", client_min=20, client_max=30,
                             client_caps=1)
    assert negotiator.stats() == {
        "negotiations": 1,
        "reconnects": 1,
        "reused": 1,
        "rejected": 1,
        "degraded": 1,
        "dropped": 0,
    }
    negotiator.reset_stats()
    assert negotiator.stats() == {
        "negotiations": 0,
        "reconnects": 0,
        "reused": 0,
        "rejected": 0,
        "degraded": 0,
        "dropped": 0,
    }
    assert negotiator.profiles() == 1


def test_snapshot_restore_roundtrip_and_validation():
    negotiator = make(profile_limit=4)
    negotiator.negotiate("conn-a", client_min=5, client_max=9,
                         client_caps=0b00101)
    negotiator.set_grace_window(3)
    state = negotiator.snapshot()
    assert state["version"] == 1
    frozen = copy.deepcopy(state)
    state["profiles"].clear()
    state["stats"]["negotiations"] = 99
    assert negotiator.profiles() == 1
    assert negotiator.stats()["negotiations"] == 1
    twin = make(profile_limit=4)
    twin.restore(frozen)
    assert twin.compat_window() == {"min": 3, "max": 9}
    assert twin.profile("conn-a")["version"] == 9
    assert twin.profiles() == 1
    fresh = twin.negotiate("conn-b", client_min=5, client_max=9,
                           client_caps=1)
    assert fresh["profile"] == "profile-2"
    for bad in (None, [], {}, {"version": 2}):
        with pytest.raises(net.NetcodeError) as exc:
            twin.restore(bad)
        assert exc.value.code == "NET_SNAPSHOT_INVALID"
    assert twin.profiles() == 2


def test_concurrent_negotiations_allocate_distinct_profiles():
    negotiator = make(profile_limit=16)
    results = []
    errors = []

    def worker(index):
        try:
            results.append(negotiator.negotiate(
                f"conn-{index}", client_min=5, client_max=9, client_caps=1))
        except Exception as exc:  # pragma: no cover - collected below
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(index,))
               for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(results) == 8
    assert sorted(item["profile"] for item in results) == [
        f"profile-{index}" for index in range(1, 9)
    ]
    assert negotiator.profiles() == 8
    assert negotiator.stats()["negotiations"] == 8
