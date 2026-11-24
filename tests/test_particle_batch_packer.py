import threading

import pytest

import particle_batch_packer as pbp


def particle(material="rain", blend="alpha", seq=0):
    return {"material": material, "blend": blend, "seq": seq}


def make(**kwargs):
    return pbp.ParticleBatchPacker(**kwargs)


def test_add_burst_reports_count_and_material_keys():
    packer = make()
    out = packer.add_burst(
        "e1",
        [particle("rain"), particle("rain", seq=1), particle("mist")],
    )
    assert out["emitter_id"] == "e1"
    assert out["count"] == 3
    assert out["materials"] == ["mist", "rain"]
    assert packer.stats()["materials"] == 2
    assert packer.stats()["instances"] == 0


def test_add_burst_rejects_blank_emitter_id():
    packer = make()
    with pytest.raises(pbp.ParticleError) as err:
        packer.add_burst("   ", [particle()])
    assert err.value.code == "PARTICLE_EMITTER_INVALID"
    assert packer.stats()["materials"] == 0


def test_add_burst_rejects_particle_without_material_or_bad_blend():
    packer = make()
    with pytest.raises(pbp.ParticleError) as err:
        packer.add_burst("e1", [{"blend": "alpha", "seq": 0}])
    assert err.value.code == "PARTICLE_PARTICLE_INVALID"

    with pytest.raises(pbp.ParticleError) as err:
        packer.add_burst("e1", [particle(blend="screen")])
    assert err.value.code == "PARTICLE_PARTICLE_INVALID"
    assert packer.stats()["materials"] == 0


def test_add_burst_enforces_per_burst_particle_limit():
    packer = make(max_burst_particles=3)
    with pytest.raises(pbp.ParticleError) as err:
        packer.add_burst("e1", [particle(seq=i) for i in range(4)])
    assert err.value.code == "PARTICLE_BURST_LIMIT"
    assert packer.stats()["materials"] == 0

    out = packer.add_burst("e1", [particle(seq=i) for i in range(3)])
    assert out["count"] == 3


def test_material_limit_rejects_without_touching_existing_batches():
    packer = make(max_materials=2)
    packer.add_burst("e1", [particle("rain")])
    packer.add_burst("e2", [particle("mist")])

    with pytest.raises(pbp.ParticleError) as err:
        packer.add_burst("e3", [particle("dust")])
    assert err.value.code == "PARTICLE_MATERIAL_LIMIT"

    batches = packer.build(max_instances=8)
    assert [(b["material"], b["blend"]) for b in batches] == [
        ("mist", "alpha"),
        ("rain", "alpha"),
    ]
    assert packer.stats()["materials"] == 2


def test_build_keeps_blend_modes_in_separate_batches():
    packer = make()
    packer.add_burst(
        "e1",
        [particle("rain", "alpha"), particle("rain", "additive"),
         particle("mist", "alpha")],
    )
    batches = packer.build(max_instances=16)
    assert [(b["material"], b["blend"]) for b in batches] == [
        ("mist", "alpha"),
        ("rain", "additive"),
        ("rain", "alpha"),
    ]
    assert [b["count"] for b in batches] == [1, 1, 1]
    assert [b["batch_id"] for b in batches] == [
        "mist|alpha|0",
        "rain|additive|0",
        "rain|alpha|0",
    ]


def test_build_orders_emitters_ascending_inside_one_batch():
    packer = make()
    packer.add_burst("e2", [particle(seq=0), particle(seq=1)])
    packer.add_burst("e1", [particle(seq=0)])
    batches = packer.build(max_instances=16)
    assert len(batches) == 1
    assert batches[0]["emitters"] == ["e1", "e2"]
    assert batches[0]["count"] == 3


def test_build_preserves_particle_order_within_an_emitter():
    packer = make()
    packer.add_burst("e1", [particle(seq=3), particle(seq=1), particle(seq=2)])
    batches = packer.build(max_instances=16)
    assert len(batches) == 1
    order = packer.instances(batches[0]["batch_id"])
    assert [item["seq"] for item in order] == [3, 1, 2]
    assert [item["emitter_id"] for item in order] == ["e1", "e1", "e1"]


def test_build_chunks_oversized_group_and_rejects_bad_max_instances():
    packer = make()
    packer.add_burst("e1", [particle(seq=i) for i in range(5)])
    batches = packer.build(max_instances=2)
    assert [b["count"] for b in batches] == [2, 2, 1]
    assert [b["batch_id"] for b in batches] == [
        "rain|alpha|0",
        "rain|alpha|1",
        "rain|alpha|2",
    ]
    assert {b["material"] for b in batches} == {"rain"}

    with pytest.raises(pbp.ParticleError) as err:
        packer.build(max_instances=0)
    assert err.value.code == "PARTICLE_MAX_INSTANCES_INVALID"

    with pytest.raises(pbp.ParticleError) as err:
        packer.build(max_instances=65537)
    assert err.value.code == "PARTICLE_MAX_INSTANCES_INVALID"


def test_build_accumulates_first_instance_across_batches():
    packer = make()
    packer.add_burst("e1", [particle("aaa", seq=i) for i in range(2)])
    packer.add_burst("e2", [particle("bbb", seq=i) for i in range(3)])
    batches = packer.build(max_instances=16)
    assert [b["material"] for b in batches] == ["aaa", "bbb"]
    assert [b["count"] for b in batches] == [2, 3]
    assert [b["first_instance"] for b in batches] == [0, 2]


def test_split_partitions_batch_and_replaces_parent():
    packer = make()
    packer.add_burst("e1", [particle(seq=i) for i in range(5)])
    batches = packer.build(max_instances=16)
    parent = batches[0]["batch_id"]
    children = packer.split(parent, 2)
    assert [c["batch_id"] for c in children] == [parent + "#0", parent + "#1"]
    assert [c["count"] for c in children] == [3, 2]
    assert [c["material"] for c in children] == ["rain", "rain"]
    assert [c["count"] for c in packer.batches()] == [3, 2]

    with pytest.raises(pbp.ParticleError) as err:
        packer.split(parent, 2)
    assert err.value.code == "PARTICLE_BATCH_UNKNOWN"

    with pytest.raises(pbp.ParticleError) as err:
        packer.split(parent + "#0", 1)
    assert err.value.code == "PARTICLE_SPLIT_INVALID"


def test_order_key_reports_material_blend_and_sequence():
    packer = make()
    assert packer.order_key(particle("mist", "additive", 7)) == (
        "mist", "additive", 7)
    assert packer.order_key({"material": "rain", "blend": "alpha"}) == (
        "rain", "alpha", -1)
    with pytest.raises(pbp.ParticleError) as err:
        packer.order_key({"material": "", "blend": "alpha"})
    assert err.value.code == "PARTICLE_PARTICLE_INVALID"


def test_stats_counts_overflow_when_batch_cap_is_reached():
    packer = make(max_batches=2)
    for idx, material in enumerate(("aaa", "bbb", "ccc")):
        packer.add_burst(f"e{idx}", [particle(material)])
    batches = packer.build(max_instances=16)
    assert [b["material"] for b in batches] == ["aaa", "bbb"]
    assert packer.stats() == {
        "batches": 2,
        "instances": 2,
        "materials": 3,
        "overflow": 1,
    }


def test_snapshot_restore_keeps_batches_and_offsets():
    packer = make()
    packer.add_burst("e1", [particle("rain", seq=0), particle("rain", seq=1)])
    packer.add_burst("e2", [particle("mist", seq=0)])
    before = packer.build(max_instances=16)
    state = packer.snapshot()

    packer.add_burst("e9", [particle("dust")])
    assert len(packer.build(max_instances=16)) == 3
    packer.restore(state)

    after = packer.build(max_instances=16)
    assert after == before
    assert [b["first_instance"] for b in after] == [0, 1]
    assert packer.stats() == {"batches": 2, "instances": 3,
                              "materials": 2, "overflow": 0}

    with pytest.raises(pbp.ParticleError) as err:
        packer.restore({"emitters": "not-a-list"})
    assert err.value.code == "PARTICLE_SNAPSHOT_INVALID"


def test_concurrent_bursts_are_all_counted():
    packer = make()
    errors = []

    def worker(index):
        try:
            out = packer.add_burst(
                f"e{index}", [particle(seq=step) for step in range(50)])
            assert out["count"] == 50
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    batches = packer.build(max_instances=100000)
    assert len(batches) == 1
    assert batches[0]["count"] == 400
    assert batches[0]["emitters"] == [f"e{i}" for i in range(8)]
    assert packer.stats()["instances"] == 400
