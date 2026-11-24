import hashlib
import threading

import pytest

import dkim_signature_guard as dg

T0 = 1_700_000_000
DOMAIN = "shop.example"
SELECTOR = "s1"


def make():
    guard = dg.DkimSignatureGuard()
    guard.add_selector_key(DOMAIN, SELECTOR, b"key-material-1", now=T0)
    return guard


def mail():
    return {
        "headers": [
            ["From", "ops@shop.example"],
            ["Subject", "Your order"],
            ["Date", "Mon, 24 Nov 2026 09:00:00 +0000"],
        ],
        "body": b"Hello there\r\n",
    }


def test_relaxed_header_canonicalization_folds_whitespace():
    guard = dg.DkimSignatureGuard()
    assert guard.canonicalize_header("SUBJECT", "  Hello   World  ", "relaxed") == \
        b"subject:Hello World\r\n"
    assert guard.canonicalize_header("X-Trace", "a\r\n\t b", "relaxed") == \
        b"x-trace:a b\r\n"


def test_simple_header_canonicalization_is_verbatim():
    guard = dg.DkimSignatureGuard()
    assert guard.canonicalize_header("SUBJECT", "  Hello   World  ", "simple") == \
        b"SUBJECT:  Hello   World  \r\n"
    with pytest.raises(dg.DkimError) as exc:
        guard.canonicalize_header("Subject", "x", "folded")
    assert exc.value.code == "DKIM_CANON_INVALID"


def test_body_canonicalization_modes():
    guard = dg.DkimSignatureGuard()
    assert guard.canonicalize_body(b"ab  cd \r\n\r\n\r\n", mode="relaxed") == \
        b"ab cd\r\n"
    assert guard.canonicalize_body(b"a\r\nb\r\n", mode="simple") == b"a\r\nb\r\n"
    assert guard.canonicalize_body(b"", mode="relaxed") == b"\r\n"
    assert guard.canonicalize_body(b"", mode="simple") == b"\r\n"


def test_body_hash_truncation_covers_prefix_only():
    guard = dg.DkimSignatureGuard()
    short = guard.body_hash(b"abcdef", length=6, mode="simple")
    longer = guard.body_hash(b"abcdefXYZ", length=6, mode="simple")
    assert short == longer == hashlib.sha256(b"abcdef").hexdigest()
    assert guard.body_hash(b"abcdefXYZ", mode="simple") == \
        hashlib.sha256(b"abcdefXYZ").hexdigest()


def test_sign_and_verify_round_trip():
    guard = make()
    message = mail()
    block = guard.sign(domain=DOMAIN, selector=SELECTOR,
                       headers=message["headers"], body=message["body"],
                       signed_headers=["from", "subject"], now=T0)
    assert block["domain"] == DOMAIN
    assert block["selector"] == SELECTOR
    assert block["canonicalization"] == "relaxed"
    assert block["signed_headers"] == ["from", "subject"]
    assert len(block["signature"]) == 64
    assert len(block["body_hash"]) == 64

    message["dkim"] = block
    out = guard.verify_message(message, now=T0)
    assert out["status"] == "ok"
    assert out["domain"] == DOMAIN
    assert out["selector"] == SELECTOR
    assert out["generation"] == block["generation"]


def test_tampered_body_is_detected():
    guard = make()
    message = mail()
    message["dkim"] = guard.sign(domain=DOMAIN, selector=SELECTOR,
                                 headers=message["headers"],
                                 body=message["body"],
                                 signed_headers=["from", "subject"], now=T0)
    message["body"] = b"Hello there, click http://evil.example/\r\n"
    with pytest.raises(dg.DkimError) as exc:
        guard.verify_message(message, now=T0)
    assert exc.value.code == "DKIM_BODY_HASH_MISMATCH"


def test_tampered_signed_header_is_detected():
    guard = make()
    message = mail()
    message["dkim"] = guard.sign(domain=DOMAIN, selector=SELECTOR,
                                 headers=message["headers"],
                                 body=message["body"],
                                 signed_headers=["from", "subject"], now=T0)
    message["headers"][1] = ["Subject", "Your order #99"]
    with pytest.raises(dg.DkimError) as exc:
        guard.verify_message(message, now=T0)
    assert exc.value.code == "DKIM_INVALID_SIGNATURE"


def test_unsigned_header_change_is_allowed():
    guard = make()
    message = mail()
    message["dkim"] = guard.sign(domain=DOMAIN, selector=SELECTOR,
                                 headers=message["headers"],
                                 body=message["body"],
                                 signed_headers=["from", "subject"], now=T0)
    message["headers"].append(["X-Spam-Score", "0.1"])
    assert guard.verify_message(message, now=T0)["status"] == "ok"


def test_unknown_selector_is_refused():
    guard = make()
    message = mail()
    block = guard.sign(domain=DOMAIN, selector=SELECTOR,
                       headers=message["headers"], body=message["body"],
                       signed_headers=["from"], now=T0)
    block = dict(block)
    block["selector"] = "s9"
    message["dkim"] = block
    with pytest.raises(dg.DkimError) as exc:
        guard.verify_message(message, now=T0)
    assert exc.value.code == "DKIM_UNKNOWN_SELECTOR"


def test_unsupported_algorithm_is_refused():
    guard = dg.DkimSignatureGuard()
    with pytest.raises(dg.DkimError) as exc:
        guard.add_selector_key(DOMAIN, SELECTOR, b"k", algorithm="md5", now=T0)
    assert exc.value.code == "DKIM_UNSUPPORTED_ALGORITHM"


def test_invalid_canonicalization_is_refused_on_sign():
    guard = make()
    message = mail()
    with pytest.raises(dg.DkimError) as exc:
        guard.sign(domain=DOMAIN, selector=SELECTOR,
                   headers=message["headers"], body=message["body"],
                   signed_headers=["from"], canonicalization="compact", now=T0)
    assert exc.value.code == "DKIM_CANON_INVALID"


def test_signed_header_order_and_from_requirement():
    guard = make()
    message = mail()
    block = guard.sign(domain=DOMAIN, selector=SELECTOR,
                       headers=message["headers"], body=message["body"],
                       signed_headers=["subject", "from"], now=T0)
    assert block["signed_headers"] == ["subject", "from"]
    message["dkim"] = block
    out = guard.verify_message(message, now=T0)
    assert out["signed_headers"] == ["subject", "from"]

    second = mail()
    with pytest.raises(dg.DkimError) as exc:
        guard.sign(domain=DOMAIN, selector=SELECTOR,
                   headers=second["headers"], body=second["body"],
                   signed_headers=["subject"], now=T0)
    assert exc.value.code == "DKIM_FROM_REQUIRED"


def test_rotate_selector_advances_generation():
    guard = make()
    before = guard.selector_state(DOMAIN, SELECTOR, now=T0)
    assert before["generation"] == 1

    rotated = guard.rotate_selector(DOMAIN, SELECTOR, b"key-material-2",
                                    grace_seconds=3600, now=T0 + 10)
    assert rotated["generation"] == 2
    assert rotated["grace_until"] == T0 + 3610

    after = guard.selector_state(DOMAIN, SELECTOR, now=T0 + 10)
    assert after["generation"] == 2

    message = mail()
    block = guard.sign(domain=DOMAIN, selector=SELECTOR,
                       headers=message["headers"], body=message["body"],
                       signed_headers=["from"], now=T0 + 10)
    assert block["generation"] == 2
    message["dkim"] = block
    assert guard.verify_message(message, now=T0 + 10)["status"] == "ok"


def test_concurrent_signing_is_consistent():
    guard = make()
    message = mail()
    signatures = []
    errors = []
    start = threading.Barrier(8)
    lock = threading.Lock()

    def work():
        start.wait()
        try:
            block = guard.sign(domain=DOMAIN, selector=SELECTOR,
                               headers=message["headers"],
                               body=message["body"],
                               signed_headers=["from", "subject"], now=T0)
            with lock:
                signatures.append(block)
        except Exception as err:  # noqa: BLE001
            with lock:
                errors.append(repr(err))

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(signatures) == 8
    assert len({item["signature"] for item in signatures}) == 1
    for block in signatures:
        message["dkim"] = block
        assert guard.verify_message(message, now=T0)["status"] == "ok"
    assert guard.stats()["signed"] == 8


def test_snapshot_restore_keeps_selectors_and_validates():
    guard = make()
    guard.rotate_selector(DOMAIN, SELECTOR, b"key-material-2", now=T0 + 5)
    state = guard.snapshot()

    revived = dg.DkimSignatureGuard()
    revived.restore(state)
    message = mail()
    block = revived.sign(domain=DOMAIN, selector=SELECTOR,
                         headers=message["headers"], body=message["body"],
                         signed_headers=["from"], now=T0 + 10)
    assert block["generation"] == 2
    message["dkim"] = block
    assert revived.verify_message(message, now=T0 + 10)["status"] == "ok"

    with pytest.raises(dg.DkimError) as exc:
        revived.restore({"selectors": 0})
    assert exc.value.code == "DKIM_SNAPSHOT_INVALID"
