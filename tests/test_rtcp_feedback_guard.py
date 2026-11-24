import threading

import pytest

import rtcp_feedback_guard as rtcp


def make(**kwargs):
    return rtcp.RtcpFeedbackGuard(**kwargs)


def test_first_packet_initialises_stream_counters():
    guard = make()
    out = guard.on_packet(7, 65530, arrival_ms=0)
    assert out == {
        "ssrc": 7,
        "extended_seq": 65530,
        "duplicate": False,
        "received": 1,
        "cumulative_lost": 0,
    }
    assert guard.highest_seq(7) == 65530
    assert guard.ssrcs() == [7]
    assert guard.cumulative_lost(7) == 0


def test_sequence_wrap_extends_highest_sequence():
    guard = make()
    for seq, arrival in ((65534, 0), (65535, 20), (0, 40), (1, 60)):
        guard.on_packet(7, seq, arrival_ms=arrival)
    assert guard.highest_seq(7) == 65537
    assert guard.cumulative_lost(7) == 0


def test_repeated_sequence_is_not_counted_twice():
    guard = make()
    guard.on_packet(7, 10, arrival_ms=0)
    guard.on_packet(7, 11, arrival_ms=20)
    out = guard.on_packet(7, 11, arrival_ms=40)
    assert out["duplicate"] is True
    assert out["received"] == 2
    assert out["cumulative_lost"] == 0


def test_cumulative_lost_counts_missing_sequences():
    guard = make()
    guard.on_packet(7, 100, arrival_ms=0)
    guard.on_packet(7, 101, arrival_ms=20)
    guard.on_packet(7, 104, arrival_ms=60)
    assert guard.highest_seq(7) == 104
    assert guard.cumulative_lost(7) == 2


def test_fraction_lost_uses_current_interval_scaled_by_256():
    guard = make()
    for offset, seq in enumerate((100, 101, 102, 103, 104)):
        guard.on_packet(7, seq, arrival_ms=offset * 20)
    guard.on_packet(7, 107, arrival_ms=120)
    assert guard.fraction_lost(7) == 64


def test_report_block_resets_fraction_interval_but_keeps_totals():
    guard = make()
    for offset, seq in enumerate((100, 101, 102, 103, 104)):
        guard.on_packet(7, seq, arrival_ms=offset * 20)
    guard.on_packet(7, 107, arrival_ms=120)
    block = guard.report_block(7, now_ms=500)
    assert block["ssrc"] == 7
    assert block["fraction_lost"] == 64
    assert block["cumulative_lost"] == 2
    assert block["highest_seq"] == 107
    assert guard.fraction_lost(7) == 0
    assert guard.cumulative_lost(7) == 2


def test_interarrival_jitter_smooths_by_sixteenth():
    guard = make()
    for seq, arrival in ((1, 0), (2, 20), (3, 60)):
        guard.on_packet(7, seq, arrival_ms=arrival)
    assert guard.interarrival_jitter(7) == 1
    guard.on_packet(7, 4, arrival_ms=130)
    assert guard.interarrival_jitter(7) == 2


def test_sender_report_sets_lsr_and_dlsr_in_report_block():
    guard = make()
    guard.on_packet(7, 1, arrival_ms=4000)
    guard.on_sender_report(7, ntp_seconds=0x1234, ntp_fraction=0xABCD0000,
                           arrival_ms=5000)
    block = guard.report_block(7, now_ms=6000)
    assert block["lsr"] == 0x1234ABCD
    assert block["dlsr"] == 65536


def test_arrival_time_must_not_move_backwards():
    guard = make()
    guard.on_packet(7, 10, arrival_ms=100)
    with pytest.raises(rtcp.RtcpFeedbackError) as exc:
        guard.on_packet(7, 11, arrival_ms=50)
    assert exc.value.code == "RTCP_ARRIVAL_INVALID"


def test_sequence_outside_sixteen_bits_is_rejected():
    guard = make()
    for bad in (65536, -1):
        with pytest.raises(rtcp.RtcpFeedbackError) as exc:
            guard.on_packet(7, bad, arrival_ms=0)
        assert exc.value.code == "RTCP_SEQ_INVALID"


def test_query_for_unknown_ssrc_is_rejected():
    guard = make()
    with pytest.raises(rtcp.RtcpFeedbackError) as exc:
        guard.cumulative_lost(99)
    assert exc.value.code == "RTCP_UNKNOWN_SSRC"


def test_stream_table_respects_max_streams():
    guard = make(max_streams=2)
    guard.on_packet(1, 0, arrival_ms=0)
    guard.on_packet(2, 0, arrival_ms=0)
    with pytest.raises(rtcp.RtcpFeedbackError) as exc:
        guard.on_packet(3, 0, arrival_ms=0)
    assert exc.value.code == "RTCP_TOO_MANY_STREAMS"
    assert guard.ssrcs() == [1, 2]


def test_build_report_caps_blocks_and_reports_omitted_count():
    guard = make(max_report_blocks=2)
    guard.on_packet(1, 0, arrival_ms=1000)
    guard.on_packet(2, 0, arrival_ms=2000)
    guard.on_packet(3, 0, arrival_ms=3000)
    report = guard.build_report(reporter_ssrc=9)
    assert report["reporter_ssrc"] == 9
    assert [block["ssrc"] for block in report["blocks"]] == [2, 3]
    assert report["omitted"] == 1


def test_snapshot_restore_keeps_loss_state_and_is_a_deep_copy():
    guard = make()
    guard.on_packet(7, 10, arrival_ms=0)
    guard.on_packet(7, 12, arrival_ms=20)
    snap = guard.snapshot()
    other = make()
    other.restore(snap)
    assert other.highest_seq(7) == 12
    assert other.cumulative_lost(7) == 1
    snap["streams"].clear()
    assert guard.cumulative_lost(7) == 1


def test_concurrent_streams_count_every_packet_once():
    guard = make()
    errors = []

    def worker(ssrc):
        try:
            for seq in range(25):
                guard.on_packet(ssrc, seq, arrival_ms=1000 + seq * 10)
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(100 + i,))
               for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(guard.ssrcs()) == 8
    assert guard.stats()["packets"] == 200
