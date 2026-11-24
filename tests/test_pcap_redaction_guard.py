import pytest

import pcap_redaction_guard as pg

T0 = 1_700_000_000


def pack(payload=b"hello", *, protocol="http", frame_length=None, **overrides):
    base = {
        "protocol": protocol,
        "src_ip": "10.1.1.1",
        "dst_ip": "10.1.1.2",
        "payload": payload,
        "frame_length": frame_length if frame_length is not None else len(payload) + 54,
        "ip_total_length": len(payload) + 40,
        "tcp_payload_length": len(payload),
        "l2_fcs": 0x1234,
        "l4_checksum": 0xBEEF,
    }
    base.update(overrides)
    return base


def test_begin_file_starts_empty():
    guard = pg.PcapRedactionGuard()
    info = guard.begin_file("/var/cap/run-1.pcap", now=T0)
    assert info["file_id"] == "cap-1"
    assert info["path"] == "/var/cap/run-1.pcap"
    assert info["packet_count"] == 0
    assert info["byte_range"] == [0, 0]
    assert info["digest"] is None
    assert info["closed"] is False
    assert guard.stats()["files"] == 1


def test_append_packets_counts_and_byte_range():
    guard = pg.PcapRedactionGuard()
    guard.begin_file("/var/cap/run-1.pcap", now=T0)
    for frame in (60, 100, 140):
        guard.append_packet("cap-1", pack(frame_length=frame), now=T0)

    counters = guard.stats()
    assert counters["packets"] == 3
    assert counters["redactions"] == 0

    closed = guard.close_file("cap-1", now=T0 + 5)
    assert closed["packet_count"] == 3
    assert closed["byte_range"] == [0, 300]
    assert closed["closed"] is True


def test_http_bearer_token_removed():
    guard = pg.PcapRedactionGuard()
    guard.begin_file("/var/cap/run-2.pcap", now=T0)
    payload = (b"GET /v1/orders HTTP/1.1\r\n"
               b"Authorization: Bearer tok_LIVE_7788\r\n\r\n")
    packet = pack(payload=payload, frame_length=200)

    out = guard.redact_packet(packet)
    assert out["redactions"] == 1
    body = out["packet"]["payload"]
    assert b"tok_LIVE_7788" not in body
    assert body.startswith(b"GET /v1/orders HTTP/1.1\r\nAuthorization: ")
    assert b"tok_LIVE_7788" in packet["payload"]

    appended = guard.append_packet("cap-1", packet, now=T0)
    assert appended["redactions"] == 1
    closed = guard.close_file("cap-1", now=T0 + 1)
    assert closed["redactions"] == 1


def test_http_cookie_and_basic_auth_removed():
    guard = pg.PcapRedactionGuard()
    payload = (b"POST /login HTTP/1.1\r\n"
               b"Host: shop.example\r\n"
               b"Cookie: sid=abc123def456; theme=dark\r\n"
               b"Authorization: Basic dXNlcjpwYXNz\r\n\r\n")
    found = guard.detect_credentials(payload, protocol="http")
    assert [item["field"] for item in found] == ["cookie", "authorization"]

    out = guard.redact_packet(pack(payload=payload))
    assert out["redactions"] == 2
    body = out["packet"]["payload"]
    assert b"abc123def456" not in body
    assert b"dXNlcjpwYXNz" not in body


def test_redis_auth_command_removed():
    guard = pg.PcapRedactionGuard()
    payload = b"*2\r\n$4\r\nAUTH\r\n$7\r\nhunter2\r\n"
    found = guard.detect_credentials(payload, protocol="redis")
    assert len(found) == 1
    assert found[0]["field"] == "auth"

    out = guard.redact_packet(pack(payload=payload, protocol="redis"))
    assert out["redactions"] == 1
    assert b"hunter2" not in out["packet"]["payload"]


def test_smtp_auth_plain_removed():
    guard = pg.PcapRedactionGuard()
    payload = b"EHLO mail.example\r\nAUTH PLAIN dXNlcjpwYXNz\r\n"
    found = guard.detect_credentials(payload, protocol="smtp")
    assert len(found) == 1
    assert found[0]["field"] == "auth_plain"

    out = guard.redact_packet(pack(payload=payload, protocol="smtp"))
    assert out["redactions"] == 1
    assert b"dXNlcjpwYXNz" not in out["packet"]["payload"]


def test_clean_payload_is_untouched():
    guard = pg.PcapRedactionGuard()
    payload = b"GET /health HTTP/1.1\r\nHost: probe\r\n\r\n"
    out = guard.redact_packet(pack(payload=payload))
    assert out["redactions"] == 0
    assert out["packet"]["payload"] == payload


def test_unknown_protocol_passes_through():
    guard = pg.PcapRedactionGuard()
    payload = b"AUTH hunter2\r\n"
    assert guard.detect_credentials(payload, protocol="dns") == []
    out = guard.redact_packet(pack(payload=payload, protocol="dns"))
    assert out["redactions"] == 0
    assert out["packet"]["payload"] == payload


def test_detect_reports_exact_credential_span():
    guard = pg.PcapRedactionGuard()
    payload = b"HEAD /a HTTP/1.1\r\nAuthorization: Bearer abc.def.ghi\r\n\r\n"
    found = guard.detect_credentials(payload, protocol="http")
    assert len(found) == 1
    span = found[0]
    assert payload[span["start"]:span["end"]] == b"abc.def.ghi"


def test_closed_file_rejects_append_and_keeps_digest():
    guard = pg.PcapRedactionGuard()
    guard.begin_file("/var/cap/run-3.pcap", now=T0)
    guard.append_packet("cap-1", pack(payload=b"one"), now=T0)
    closed = guard.close_file("cap-1", now=T0 + 3)
    assert len(closed["digest"]) == 64
    assert closed["packet_count"] == 1

    with pytest.raises(pg.PcapGuardError) as exc:
        guard.append_packet("cap-1", pack(payload=b"two"), now=T0 + 4)
    assert exc.value.code == "PCAP_FILE_CLOSED"


def test_unknown_file_and_out_of_range_read():
    guard = pg.PcapRedactionGuard()
    guard.authorize_reader("analyst")
    guard.begin_file("/var/cap/run-4.pcap", now=T0)
    guard.append_packet("cap-1", pack(payload=b"abcdef", frame_length=60), now=T0)
    guard.close_file("cap-1", now=T0 + 1)

    with pytest.raises(pg.PcapGuardError) as exc:
        guard.append_packet("cap-99", pack(), now=T0)
    assert exc.value.code == "PCAP_UNKNOWN_FILE"

    with pytest.raises(pg.PcapGuardError) as exc:
        guard.open_for_read("cap-1", requester="analyst", start=0, end=61, now=T0 + 2)
    assert exc.value.code == "PCAP_REQUEST_OUT_OF_RANGE"

    out = guard.open_for_read("cap-1", requester="analyst", start=0, end=60,
                              now=T0 + 2)
    assert out["bytes_read"] == 60
    assert out["range"] == [0, 60]


def test_read_requires_authorized_requester():
    guard = pg.PcapRedactionGuard()
    guard.begin_file("/var/cap/run-5.pcap", now=T0)
    guard.append_packet("cap-1", pack(payload=b"abc", frame_length=30), now=T0)
    guard.close_file("cap-1", now=T0 + 1)

    with pytest.raises(pg.PcapGuardError) as exc:
        guard.open_for_read("cap-1", requester="stranger", start=0, end=30,
                            now=T0 + 2)
    assert exc.value.code == "PCAP_UNAUTHORIZED"
    assert guard.audit_log() == []

    guard.authorize_reader("analyst")
    guard.open_for_read("cap-1", requester="analyst", start=0, end=30, now=T0 + 2)
    assert len(guard.audit_log()) == 1


def test_rotation_by_duration_opens_new_file():
    guard = pg.PcapRedactionGuard(rotation_seconds=60)
    guard.begin_file("/var/cap/run-6.pcap", now=T0)
    guard.append_packet("cap-1", pack(payload=b"a"), now=T0)
    assert guard.maybe_rotate(now=T0 + 59) is None

    new_file = guard.maybe_rotate(now=T0 + 60)
    assert new_file["file_id"] == "cap-2"
    assert new_file["packet_count"] == 0
    assert guard.stats()["files"] == 2


def test_purge_before_retention_keeps_files():
    guard = pg.PcapRedactionGuard(retention_seconds=3600)
    guard.begin_file("/var/cap/run-7.pcap", now=T0)
    guard.append_packet("cap-1", pack(payload=b"a"), now=T0)
    guard.close_file("cap-1", now=T0 + 1)
    assert guard.purge_expired(now=T0 + 3599) == []
    assert guard.stats()["files"] == 1


def test_snapshot_restore_keeps_index_and_validates():
    guard = pg.PcapRedactionGuard()
    guard.authorize_reader("analyst")
    guard.begin_file("/var/cap/run-8.pcap", now=T0)
    guard.append_packet("cap-1", pack(payload=b"abc", frame_length=30), now=T0)
    guard.close_file("cap-1", now=T0 + 1)

    state = guard.snapshot()
    revived = pg.PcapRedactionGuard()
    revived.restore(state)
    out = revived.open_for_read("cap-1", requester="analyst", start=0, end=30,
                                now=T0 + 2)
    assert out["bytes_read"] == 30

    with pytest.raises(pg.PcapGuardError) as exc:
        revived.restore({"files": "oops"})
    assert exc.value.code == "PCAP_SNAPSHOT_INVALID"
