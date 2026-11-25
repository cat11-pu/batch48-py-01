"""Account recovery codes: hashed batches, one-time redemption, epochs."""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import string
import threading

DEFAULT_CODE_COUNT = 10
DEFAULT_CODE_LENGTH = 10
DEFAULT_MAX_FAILURES = 5
DEFAULT_LOCK_WINDOW = 300
DEFAULT_HISTORY_LIMIT = 32

MIN_CODE_COUNT = 1
MAX_CODE_COUNT = 32
MIN_CODE_LENGTH = 8
MAX_CODE_LENGTH = 32
MAX_ACCOUNTS = 1024
MAX_HISTORY_LIMIT = 256
MAX_CODE_INPUT_LENGTH = 128
SNAPSHOT_VERSION = 1

CODE_ALPHABET = string.ascii_uppercase + string.digits
_DASHES = frozenset(
    "-\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d"
)
_DUMMY_DIGESTS = tuple(
    hashlib.sha256(("recovery-code-dummy-%d" % index).encode("ascii")).hexdigest()
    for index in range(MAX_CODE_COUNT)
)


class RecoveryCodeError(ValueError):
    """Raised for every rejected recovery code operation."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class RecoveryCodeGuard:
    """Issue, redeem and invalidate one-time account recovery codes."""

    MAX_ACCOUNTS = MAX_ACCOUNTS
    MAX_CODE_COUNT = MAX_CODE_COUNT
    MAX_CODE_LENGTH = MAX_CODE_LENGTH
    MAX_HISTORY_LIMIT = MAX_HISTORY_LIMIT

    def __init__(self, *, code_count=DEFAULT_CODE_COUNT,
                 code_length=DEFAULT_CODE_LENGTH, code_ttl=None,
                 max_failures=DEFAULT_MAX_FAILURES,
                 lock_window=DEFAULT_LOCK_WINDOW,
                 history_limit=DEFAULT_HISTORY_LIMIT):
        self.code_count = code_count
        self.code_length = code_length
        self.code_ttl = code_ttl
        self.max_failures = max_failures
        self.lock_window = lock_window
        self.history_limit = history_limit
        self._lock = threading.RLock()
        self._accounts = {}

    def generate(self, account, *, now=0):
        self._require_account(account)
        self._validate_config()

        with self._lock:
            if account in self._accounts:
                raise RecoveryCodeError(
                    "RC_BATCH_EXISTS", {"account": account}
                )
            if len(self._accounts) >= self.MAX_ACCOUNTS:
                raise RecoveryCodeError(
                    "RC_ACCOUNT_LIMIT",
                    {"limit": self.MAX_ACCOUNTS},
                )

            codes, records, expires_at = self._make_batch(now)
            self._accounts[account] = {
                "epoch": 1,
                "expires_at": expires_at,
                "codes": records,
                "stale": [],
                "failures": 0,
                "locked_until": None,
            }
            return {
                "account": account,
                "epoch": 1,
                "expires_at": expires_at,
                "codes": codes,
            }

    def regenerate(self, account, *, now=0):
        self._require_account(account)
        self._validate_config()

        with self._lock:
            record = self._accounts.get(account)
            if record is None:
                raise RecoveryCodeError(
                    "RC_ACCOUNT_UNKNOWN", {"account": account}
                )

            codes, records, expires_at = self._make_batch(now)
            stale = record["stale"]
            stale.extend(item["digest"] for item in record["codes"])
            if len(stale) > self.history_limit:
                del stale[:len(stale) - self.history_limit]

            record["epoch"] += 1
            record["expires_at"] = expires_at
            record["codes"] = records
            record["failures"] = 0
            record["locked_until"] = None
            return {
                "account": account,
                "epoch": record["epoch"],
                "expires_at": expires_at,
                "codes": codes,
            }

    def redeem(self, account, code, *, now=0):
        self._require_account(account)
        normalized = self.normalize(code)
        digest = self._digest(normalized)

        with self._lock:
            record = self._accounts.get(account)
            if record is None:
                self._constant_time_unknown_check(digest)
                return self._result(
                    account, "invalid", 0, False, 0, 0
                )

            self._release_expired_lock(record, now)
            if record["locked_until"] is not None:
                raise RecoveryCodeError(
                    "RC_LOCKED",
                    {
                        "account": account,
                        "attempts": record["failures"],
                        "locked_until": record["locked_until"],
                    },
                )

            match = None
            for item in record["codes"]:
                if hmac.compare_digest(digest, item["digest"]):
                    match = item
                    break

            if match is not None:
                if match["used"]:
                    raise RecoveryCodeError(
                        "RC_CODE_USED", {"account": account}
                    )
                if self._is_expired(record, now):
                    raise RecoveryCodeError(
                        "RC_CODE_EXPIRED", {"account": account}
                    )

                match["used"] = True
                record["failures"] = 0
                record["locked_until"] = None
                return self._result(
                    account,
                    "ok",
                    0,
                    False,
                    record["epoch"],
                    self._remaining(record),
                )

            for stale_digest in record["stale"]:
                if hmac.compare_digest(digest, stale_digest):
                    raise RecoveryCodeError(
                        "RC_CODE_STALE",
                        {"account": account, "epoch": record["epoch"]},
                    )

            if self._is_expired(record, now):
                raise RecoveryCodeError(
                    "RC_CODE_EXPIRED", {"account": account}
                )

            record["failures"] += 1
            if record["failures"] >= self.max_failures:
                record["locked_until"] = now + self.lock_window
                raise RecoveryCodeError(
                    "RC_LOCKED",
                    {
                        "account": account,
                        "attempts": record["failures"],
                        "locked_until": record["locked_until"],
                    },
                )

            return self._result(
                account,
                "invalid",
                record["failures"],
                False,
                record["epoch"],
                self._remaining(record),
            )

    def status(self, account, *, now=0):
        self._require_account(account)
        with self._lock:
            record = self._accounts.get(account)
            if record is None:
                raise RecoveryCodeError(
                    "RC_ACCOUNT_UNKNOWN", {"account": account}
                )
            self._release_expired_lock(record, now)
            locked = record["locked_until"] is not None
            return {
                "account": account,
                "epoch": record["epoch"],
                "remaining": self._remaining(record),
                "used": self._used(record),
                "failures": record["failures"],
                "locked": locked,
            }

    def epoch_of(self, account):
        self._require_account(account)
        with self._lock:
            record = self._accounts.get(account)
            if record is None:
                raise RecoveryCodeError(
                    "RC_ACCOUNT_UNKNOWN", {"account": account}
                )
            return record["epoch"]

    def remaining(self, account):
        self._require_account(account)
        with self._lock:
            record = self._accounts.get(account)
            if record is None:
                raise RecoveryCodeError(
                    "RC_ACCOUNT_UNKNOWN", {"account": account}
                )
            return self._remaining(record)

    def unlock(self, account):
        self._require_account(account)
        with self._lock:
            record = self._accounts.get(account)
            if record is not None:
                record["failures"] = 0
                record["locked_until"] = None

    @staticmethod
    def normalize(code):
        if not isinstance(code, str):
            raise RecoveryCodeError(
                "RC_CODE_INVALID", {"reason": "code must be a string"}
            )
        cleaned = "".join(
            character
            for character in code
            if not character.isspace() and character not in _DASHES
        ).upper()
        if not cleaned or len(cleaned) > MAX_CODE_INPUT_LENGTH:
            raise RecoveryCodeError(
                "RC_CODE_INVALID",
                {"reason": "code is empty or too long"},
            )
        return cleaned

    def snapshot(self):
        with self._lock:
            accounts = {}
            for account, record in self._accounts.items():
                accounts[account] = {
                    "epoch": record["epoch"],
                    "expires_at": record["expires_at"],
                    "codes": [
                        {"digest": item["digest"], "used": item["used"]}
                        for item in record["codes"]
                    ],
                    "stale": list(record["stale"]),
                    "failures": record["failures"],
                    "locked_until": record["locked_until"],
                }
            return {
                "version": SNAPSHOT_VERSION,
                "accounts": accounts,
                "config": {
                    "code_count": self.code_count,
                    "code_length": self.code_length,
                    "code_ttl": self.code_ttl,
                    "max_failures": self.max_failures,
                    "lock_window": self.lock_window,
                    "history_limit": self.history_limit,
                },
            }

    def restore(self, state):
        try:
            accounts, config = self._parse_snapshot(state)
        except RecoveryCodeError:
            raise
        except Exception as exc:
            raise RecoveryCodeError(
                "RC_SNAPSHOT_INVALID", {"reason": str(exc)}
            ) from exc

        with self._lock:
            if config is not None:
                self.code_count = config["code_count"]
                self.code_length = config["code_length"]
                self.code_ttl = config["code_ttl"]
                self.max_failures = config["max_failures"]
                self.lock_window = config["lock_window"]
                self.history_limit = config["history_limit"]
            self._accounts = accounts

    def stats(self):
        with self._lock:
            remaining = 0
            used = 0
            failures = 0
            locked = 0
            stale = 0
            for record in self._accounts.values():
                remaining += self._remaining(record)
                used += self._used(record)
                failures += record["failures"]
                locked += int(record["locked_until"] is not None)
                stale += len(record["stale"])
            return {
                "accounts": len(self._accounts),
                "codes": remaining + used,
                "remaining": remaining,
                "used": used,
                "failures": failures,
                "locked": locked,
                "stale": stale,
            }

    @staticmethod
    def _require_account(account):
        if not isinstance(account, str) or not account.strip():
            raise RecoveryCodeError(
                "RC_ACCOUNT_INVALID",
                {"reason": "account must be a non-empty string"},
            )

    def _validate_config(self):
        if (type(self.code_count) is not int
                or not MIN_CODE_COUNT <= self.code_count <= MAX_CODE_COUNT):
            raise RecoveryCodeError(
                "RC_COUNT_INVALID", {"code_count": self.code_count}
            )
        if (type(self.code_length) is not int
                or not MIN_CODE_LENGTH <= self.code_length <= MAX_CODE_LENGTH):
            raise RecoveryCodeError(
                "RC_COUNT_INVALID", {"code_length": self.code_length}
            )
        if self.code_ttl is not None and (
                not self._is_number(self.code_ttl) or self.code_ttl < 0):
            raise RecoveryCodeError(
                "RC_COUNT_INVALID", {"code_ttl": self.code_ttl}
            )
        if (type(self.max_failures) is not int
                or self.max_failures < 1):
            raise RecoveryCodeError(
                "RC_COUNT_INVALID", {"max_failures": self.max_failures}
            )
        if (not self._is_number(self.lock_window)
                or self.lock_window < 0):
            raise RecoveryCodeError(
                "RC_COUNT_INVALID", {"lock_window": self.lock_window}
            )
        if (type(self.history_limit) is not int
                or not 0 <= self.history_limit <= MAX_HISTORY_LIMIT):
            raise RecoveryCodeError(
                "RC_COUNT_INVALID",
                {"history_limit": self.history_limit},
            )

    def _make_batch(self, now):
        codes = []
        seen_codes = set()
        seen_digests = set()
        while len(codes) < self.code_count:
            code = "".join(
                secrets.choice(CODE_ALPHABET)
                for _ in range(self.code_length)
            )
            digest = self._digest(code)
            if code in seen_codes or digest in seen_digests:
                continue
            seen_codes.add(code)
            seen_digests.add(digest)
            codes.append(code)

        records = [
            {"digest": self._digest(code), "used": False}
            for code in codes
        ]
        expires_at = (
            None if self.code_ttl is None else now + self.code_ttl
        )
        return codes, records, expires_at

    @staticmethod
    def _digest(normalized_code):
        return hashlib.sha256(
            normalized_code.encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _is_number(value):
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return True
        if isinstance(value, float):
            return math.isfinite(value)
        return False

    @staticmethod
    def _is_expired(record, now):
        expires_at = record["expires_at"]
        return expires_at is not None and now >= expires_at

    @staticmethod
    def _release_expired_lock(record, now):
        locked_until = record["locked_until"]
        if locked_until is not None and now >= locked_until:
            record["locked_until"] = None
            record["failures"] = 0

    @staticmethod
    def _remaining(record):
        return sum(
            int(not item["used"]) for item in record["codes"]
        )

    @staticmethod
    def _used(record):
        return sum(int(item["used"]) for item in record["codes"])

    @staticmethod
    def _result(account, status, attempts, locked, epoch, remaining):
        return {
            "account": account,
            "status": status,
            "attempts": attempts,
            "locked": locked,
            "epoch": epoch,
            "remaining": remaining,
        }

    def _constant_time_unknown_check(self, digest):
        matched = False
        for candidate in _DUMMY_DIGESTS:
            if hmac.compare_digest(digest, candidate):
                matched = True
        return matched

    def _parse_snapshot(self, state):
        if not isinstance(state, dict):
            raise self._snapshot_error("snapshot must be an object")
        if (type(state.get("version")) is not int
                or state["version"] != SNAPSHOT_VERSION):
            raise self._snapshot_error("unsupported snapshot version")

        raw_accounts = state.get("accounts")
        if not isinstance(raw_accounts, dict):
            raise self._snapshot_error("accounts must be an object")
        if len(raw_accounts) > self.MAX_ACCOUNTS:
            raise self._snapshot_error("too many accounts")

        config = self._parse_snapshot_config(state.get("config"))
        history_limit = (
            config["history_limit"]
            if config is not None
            else self.history_limit
        )

        accounts = {}
        for account, raw_record in raw_accounts.items():
            if not isinstance(account, str) or not account.strip():
                raise self._snapshot_error("invalid account name")
            if not isinstance(raw_record, dict):
                raise self._snapshot_error("invalid account record")

            epoch = raw_record.get("epoch")
            if type(epoch) is not int or epoch < 1:
                raise self._snapshot_error("invalid epoch")

            expires_at = raw_record.get("expires_at")
            if expires_at is not None and not self._is_number(expires_at):
                raise self._snapshot_error("invalid expiry")

            failures = raw_record.get("failures", 0)
            if type(failures) is not int or failures < 0:
                raise self._snapshot_error("invalid failure count")

            locked_until = raw_record.get("locked_until")
            if locked_until is not None and not self._is_number(locked_until):
                raise self._snapshot_error("invalid lock deadline")

            raw_codes = raw_record.get("codes")
            if (not isinstance(raw_codes, list)
                    or not MIN_CODE_COUNT <= len(raw_codes) <= MAX_CODE_COUNT):
                raise self._snapshot_error("invalid code table")

            codes = []
            digests = set()
            for raw_code in raw_codes:
                if isinstance(raw_code, str):
                    digest = raw_code
                    used = False
                elif isinstance(raw_code, dict):
                    digest = raw_code.get("digest")
                    used = raw_code.get("used", False)
                else:
                    raise self._snapshot_error("invalid code entry")

                if not self._valid_digest(digest) or digest in digests:
                    raise self._snapshot_error("invalid code digest")
                if type(used) is not bool:
                    raise self._snapshot_error("invalid used marker")
                digests.add(digest)
                codes.append({"digest": digest, "used": used})

            raw_stale = raw_record.get("stale", [])
            if not isinstance(raw_stale, list):
                raise self._snapshot_error("invalid stale history")
            if len(raw_stale) > history_limit:
                raise self._snapshot_error("stale history too long")

            stale = []
            for digest in raw_stale:
                if not self._valid_digest(digest) or digest in digests:
                    raise self._snapshot_error("invalid stale digest")
                digests.add(digest)
                stale.append(digest)

            accounts[account] = {
                "epoch": epoch,
                "expires_at": expires_at,
                "codes": codes,
                "stale": stale,
                "failures": failures,
                "locked_until": locked_until,
            }

        return accounts, config

    def _parse_snapshot_config(self, raw_config):
        if raw_config is None:
            return None
        if not isinstance(raw_config, dict):
            raise self._snapshot_error("config must be an object")

        code_count = raw_config.get("code_count", self.code_count)
        code_length = raw_config.get("code_length", self.code_length)
        code_ttl = raw_config.get("code_ttl", self.code_ttl)
        max_failures = raw_config.get("max_failures", self.max_failures)
        lock_window = raw_config.get("lock_window", self.lock_window)
        history_limit = raw_config.get(
            "history_limit", self.history_limit
        )

        if (type(code_count) is not int
                or not MIN_CODE_COUNT <= code_count <= MAX_CODE_COUNT):
            raise self._snapshot_error("invalid code_count")
        if (type(code_length) is not int
                or not MIN_CODE_LENGTH <= code_length <= MAX_CODE_LENGTH):
            raise self._snapshot_error("invalid code_length")
        if code_ttl is not None and (
                not self._is_number(code_ttl) or code_ttl < 0):
            raise self._snapshot_error("invalid code_ttl")
        if type(max_failures) is not int or max_failures < 1:
            raise self._snapshot_error("invalid max_failures")
        if not self._is_number(lock_window) or lock_window < 0:
            raise self._snapshot_error("invalid lock_window")
        if (type(history_limit) is not int
                or not 0 <= history_limit <= MAX_HISTORY_LIMIT):
            raise self._snapshot_error("invalid history_limit")

        return {
            "code_count": code_count,
            "code_length": code_length,
            "code_ttl": code_ttl,
            "max_failures": max_failures,
            "lock_window": lock_window,
            "history_limit": history_limit,
        }

    @staticmethod
    def _valid_digest(digest):
        if not isinstance(digest, str) or len(digest) != 64:
            return False
        return all(
            character in "0123456789abcdef" for character in digest
        )

    @staticmethod
    def _snapshot_error(reason):
        return RecoveryCodeError(
            "RC_SNAPSHOT_INVALID", {"reason": reason}
        )
