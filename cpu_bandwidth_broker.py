"""CPU bandwidth broker: per-group quotas with short-lived idle borrowing."""

from __future__ import annotations

import copy
import threading


class CpuBrokerError(ValueError):
    """Rejection raised by the CPU bandwidth broker."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class CpuBandwidthBroker:
    """Hand out fixed quotas, lend idle shares and reclaim loans at period end."""

    _SNAPSHOT_VERSION = 1
    _LOAN_OPEN = "open"
    _LOAN_RETURNED = "returned"
    _LOAN_EXPIRED = "expired"
    _LOAN_STATES = {_LOAN_OPEN, _LOAN_RETURNED, _LOAN_EXPIRED}

    def __init__(self, *, global_shares: int = 800, min_granularity: int = 10,
                 max_loans: int = 32, max_period: int = 1000, max_groups: int = 8,
                 clock=None) -> None:
        self.global_shares = global_shares
        self.min_granularity = min_granularity
        self.max_loans = max_loans
        self.max_period = max_period
        self.max_groups = max_groups
        self._clock = clock
        self._lock = threading.RLock()
        self._groups: dict[str, dict] = {}
        self._loans: dict[str, dict] = {}
        self._charges: list[dict] = []
        self._counter = 0
        self._last_now = 0

    @staticmethod
    def _is_int(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    @staticmethod
    def _fail(code: str, detail: object = None):
        raise CpuBrokerError(code, detail)

    def _validate_group_name(self, group: object) -> str:
        if not isinstance(group, str) or not group or group.strip() != group:
            self._fail("CPU_GROUP_INVALID", {"group": group})
        return group

    def _validate_quota(self, quota: object) -> int:
        if not self._is_int(quota):
            self._fail("CPU_QUOTA_INVALID", {"quota": quota})
        if quota < self.min_granularity:
            self._fail("CPU_QUOTA_TOO_SMALL", {"quota": quota})
        if quota % self.min_granularity != 0:
            self._fail("CPU_QUOTA_INVALID", {"quota": quota})
        return quota

    def _validate_period(self, period: object) -> int:
        if (not self._is_int(period) or period <= 0
                or period > self.max_period):
            self._fail("CPU_PERIOD_INVALID", {"period": period})
        return period

    def _validate_shares(self, shares: object) -> int:
        if (not self._is_int(shares) or shares <= 0
                or shares % self.min_granularity != 0):
            self._fail("CPU_SHARES_INVALID", {"shares": shares})
        return shares

    def _resolve_now(self, now: int | None) -> int:
        if now is None:
            now = self._clock() if self._clock is not None else self._last_now
        if not self._is_int(now):
            self._fail("CPU_WINDOW_INVALID", {"now": now})
        if now > self._last_now:
            self._last_now = now
        return now

    def _loan_order(self, loan: dict) -> tuple:
        suffix = loan["loan_id"].rpartition("-")[2]
        order = int(suffix) if suffix.isdigit() else 0
        return (loan["expire"], order)

    def _sweep(self, now: int, group: str | None = None) -> list[dict]:
        expired = []
        for loan in self._loans.values():
            if loan["status"] != self._LOAN_OPEN:
                continue
            if group is not None and loan["group"] != group:
                continue
            if loan["expire"] <= now:
                loan["status"] = self._LOAN_EXPIRED
                expired.append(copy.deepcopy(loan))
        expired.sort(key=self._loan_order)
        return expired

    def _open_loans(self, group: str | None = None) -> list[dict]:
        return [
            loan for loan in self._loans.values()
            if loan["status"] == self._LOAN_OPEN
            and (group is None or loan["group"] == group)
        ]

    def _group_borrowed(self, group: str) -> int:
        return sum(loan["shares"] for loan in self._open_loans(group))

    def _committed_quota(self, exclude: str | None = None) -> int:
        return sum(
            data["quota"] for name, data in self._groups.items()
            if name != exclude
        )

    def _public_loan(self, loan: dict) -> dict:
        return {
            "loan_id": loan["loan_id"],
            "group": loan["group"],
            "shares": loan["shares"],
            "start": loan["start"],
            "expire": loan["expire"],
            "status": loan["status"],
        }

    def _require_group(self, group: object) -> dict:
        self._validate_group_name(group)
        data = self._groups.get(group)
        if data is None:
            self._fail("CPU_GROUP_UNKNOWN", {"group": group})
        return data

    def _require_loan(self, loan_id: object) -> dict:
        if not isinstance(loan_id, str):
            self._fail("CPU_LOAN_UNKNOWN", {"loan_id": loan_id})
        loan = self._loans.get(loan_id)
        if loan is None:
            self._fail("CPU_LOAN_UNKNOWN", {"loan_id": loan_id})
        return loan

    def set_quota(self, group: str, quota: int, period: int) -> dict:
        with self._lock:
            self._validate_group_name(group)
            quota = self._validate_quota(quota)
            period = self._validate_period(period)
            self._sweep(self._last_now)

            existing = self._groups.get(group)
            if existing is None and len(self._groups) >= self.max_groups:
                self._fail("CPU_GROUP_LIMIT", {"max_groups": self.max_groups})

            committed = self._committed_quota(exclude=group) + quota
            committed += sum(loan["shares"] for loan in self._open_loans())
            if committed > self.global_shares:
                self._fail(
                    "CPU_QUOTA_OVERCOMMIT",
                    {"committed": committed, "global_shares": self.global_shares},
                )

            epoch = 1 if existing is None else existing["epoch"] + 1
            self._groups[group] = {
                "group": group,
                "quota": quota,
                "period": period,
                "epoch": epoch,
            }
            return {
                "group": group,
                "quota": quota,
                "period": period,
                "epoch": epoch,
            }

    def borrow(self, group: str, shares: int, now: int) -> dict:
        with self._lock:
            self._require_group(group)
            shares = self._validate_shares(shares)
            now = self._resolve_now(now)
            self._sweep(now)

            if len(self._open_loans()) >= self.max_loans:
                self._fail("CPU_LOAN_LIMIT", {"max_loans": self.max_loans})

            data = self._groups[group]
            if self._group_borrowed(group) + shares > data["quota"]:
                self._fail(
                    "CPU_LOAN_LIMIT",
                    {"group": group, "quota": data["quota"]},
                )

            free = (self.global_shares
                    - self._committed_quota()
                    - sum(loan["shares"] for loan in self._open_loans()))
            if shares > free:
                self._fail(
                    "CPU_GLOBAL_EXHAUSTED",
                    {"requested": shares, "free": free},
                )

            self._counter += 1
            loan_id = f"loan-{self._counter}"
            period = data["period"]
            expire = (now // period + 1) * period
            loan = {
                "loan_id": loan_id,
                "group": group,
                "shares": shares,
                "start": now,
                "expire": expire,
                "status": self._LOAN_OPEN,
            }
            self._loans[loan_id] = loan
            return self._public_loan(loan)

    def return_loan(self, loan_id: str, now: int | None = None) -> dict:
        with self._lock:
            loan = self._require_loan(loan_id)
            now = self._resolve_now(now)
            self._sweep(now)
            if loan["status"] != self._LOAN_OPEN:
                self._fail(
                    "CPU_LOAN_CLOSED",
                    {"loan_id": loan_id, "status": loan["status"]},
                )
            loan["status"] = self._LOAN_RETURNED
            return {
                "loan_id": loan["loan_id"],
                "group": loan["group"],
                "shares": loan["shares"],
                "status": loan["status"],
            }

    def reclaim(self, group: str, now: int | None = None) -> list:
        with self._lock:
            self._require_group(group)
            now = self._resolve_now(now)
            return self._sweep(now, group=group)

    def charge(self, group: str, shares: int, now: int) -> dict:
        with self._lock:
            data = self._require_group(group)
            shares = self._validate_shares(shares)
            now = self._resolve_now(now)
            self._sweep(now)

            period_start = (now // data["period"]) * data["period"]
            period_end = period_start + data["period"]
            used = sum(
                row["shares"] for row in self._charges
                if row["group"] == group
                and period_start <= row["time"] < period_end
            )
            borrowed = self._group_borrowed(group)
            if used + shares > data["quota"] + borrowed:
                self._fail(
                    "CPU_USAGE_EXCEEDED",
                    {
                        "group": group,
                        "used": used,
                        "requested": shares,
                        "capacity": data["quota"] + borrowed,
                    },
                )

            used += shares
            self._charges.append({
                "group": group,
                "time": now,
                "shares": shares,
                "period": period_start,
            })
            return {
                "group": group,
                "period": period_start,
                "used": used,
                "quota": data["quota"],
                "borrowed": borrowed,
            }

    def usage(self, group: str, window) -> dict:
        with self._lock:
            data = self._require_group(group)
            if (not isinstance(window, (tuple, list)) or len(window) != 2
                    or not all(self._is_int(value) for value in window)):
                self._fail("CPU_WINDOW_INVALID", {"window": window})
            start, end = window
            if start < 0 or start >= end:
                self._fail("CPU_WINDOW_INVALID", {"window": window})

            period = data["period"]
            if start // period != (end - 1) // period:
                self._fail("CPU_WINDOW_INVALID", {"window": window})

            used = sum(
                row["shares"] for row in self._charges
                if row["group"] == group and start <= row["time"] < end
            )
            return {
                "group": group,
                "start": start,
                "end": end,
                "used": used,
                "quota": data["quota"],
            }

    def global_free(self, now: int | None = None) -> int:
        with self._lock:
            now = self._resolve_now(now)
            self._sweep(now)
            return (self.global_shares
                    - self._committed_quota()
                    - sum(loan["shares"] for loan in self._open_loans()))

    def loan_status(self, loan_id: str, now: int | None = None) -> dict:
        with self._lock:
            loan = self._require_loan(loan_id)
            now = self._resolve_now(now)
            self._sweep(now)
            return self._public_loan(loan)

    def snapshot(self) -> dict:
        with self._lock:
            groups = {
                name: {
                    "quota": data["quota"],
                    "period": data["period"],
                    "epoch": data["epoch"],
                }
                for name, data in self._groups.items()
            }
            loans = [self._public_loan(loan) for loan in self._loans.values()]
            return {
                "version": self._SNAPSHOT_VERSION,
                "groups": groups,
                "loans": loans,
                "charges": copy.deepcopy(self._charges),
                "counter": self._counter,
                "last_now": self._last_now,
            }

    def _invalid_snapshot(self, detail: object):
        self._fail("CPU_SNAPSHOT_INVALID", detail)

    def restore(self, state: dict) -> None:
        with self._lock:
            if not isinstance(state, dict):
                self._invalid_snapshot("state must be a mapping")

            version = state.get("version")
            groups_in = state.get("groups")
            loans_in = state.get("loans")
            charges_in = state.get("charges")
            counter = state.get("counter")
            last_now = state.get("last_now")
            if (not self._is_int(version)
                    or version != self._SNAPSHOT_VERSION
                    or not isinstance(groups_in, dict)
                    or not isinstance(loans_in, list)
                    or not isinstance(charges_in, list)
                    or not self._is_int(counter)
                    or counter < 0
                    or not self._is_int(last_now)):
                self._invalid_snapshot("invalid top-level state")

            if len(groups_in) > self.max_groups:
                self._invalid_snapshot("group limit exceeded")

            groups: dict[str, dict] = {}
            for name, data in groups_in.items():
                try:
                    self._validate_group_name(name)
                    if not isinstance(data, dict):
                        raise CpuBrokerError("CPU_SNAPSHOT_INVALID")
                    quota = self._validate_quota(data.get("quota"))
                    period = self._validate_period(data.get("period"))
                    epoch = data.get("epoch")
                    if not self._is_int(epoch) or epoch < 1:
                        raise CpuBrokerError("CPU_SNAPSHOT_INVALID")
                except CpuBrokerError as exc:
                    self._invalid_snapshot(
                        {"group": name, "reason": exc.code}
                    )
                groups[name] = {
                    "group": name,
                    "quota": quota,
                    "period": period,
                    "epoch": epoch,
                }

            loans: dict[str, dict] = {}
            max_loan_number = 0
            for item in loans_in:
                try:
                    if not isinstance(item, dict):
                        raise CpuBrokerError("CPU_SNAPSHOT_INVALID")
                    loan_id = item.get("loan_id")
                    group = item.get("group")
                    if (not isinstance(loan_id, str)
                            or not loan_id.startswith("loan-")
                            or not loan_id[5:].isdigit()
                            or loan_id in loans
                            or not isinstance(group, str)
                            or group not in groups):
                        raise CpuBrokerError("CPU_SNAPSHOT_INVALID")
                    shares = self._validate_shares(item.get("shares"))
                    start = item.get("start")
                    expire = item.get("expire")
                    status = item.get("status")
                    if (not self._is_int(start) or not self._is_int(expire)
                            or expire <= start
                            or not isinstance(status, str)
                            or status not in self._LOAN_STATES):
                        raise CpuBrokerError("CPU_SNAPSHOT_INVALID")
                except CpuBrokerError as exc:
                    self._invalid_snapshot(
                        {"loan": item, "reason": exc.code}
                    )
                loan = {
                    "loan_id": loan_id,
                    "group": group,
                    "shares": shares,
                    "start": start,
                    "expire": expire,
                    "status": status,
                }
                loans[loan_id] = loan
                max_loan_number = max(max_loan_number, int(loan_id[5:]))

            if counter < max_loan_number:
                self._invalid_snapshot("counter precedes loan ids")
            if sum(
                loan["status"] == self._LOAN_OPEN for loan in loans.values()
            ) > self.max_loans:
                self._invalid_snapshot("loan limit exceeded")

            committed = sum(data["quota"] for data in groups.values())
            committed += sum(
                loan["shares"] for loan in loans.values()
                if loan["status"] == self._LOAN_OPEN
            )
            if committed > self.global_shares:
                self._invalid_snapshot("snapshot exceeds global shares")

            charges: list[dict] = []
            for item in charges_in:
                try:
                    if not isinstance(item, dict):
                        raise CpuBrokerError("CPU_SNAPSHOT_INVALID")
                    group = item.get("group")
                    if not isinstance(group, str) or group not in groups:
                        raise CpuBrokerError("CPU_SNAPSHOT_INVALID")
                    shares = self._validate_shares(item.get("shares"))
                    time = item.get("time")
                    period = item.get("period")
                    if not self._is_int(time) or not self._is_int(period):
                        raise CpuBrokerError("CPU_SNAPSHOT_INVALID")
                except CpuBrokerError as exc:
                    self._invalid_snapshot(
                        {"charge": item, "reason": exc.code}
                    )
                charges.append({
                    "group": group,
                    "time": time,
                    "shares": shares,
                    "period": period,
                })

            self._groups = groups
            self._loans = loans
            self._charges = charges
            self._counter = counter
            self._last_now = last_now

    def stats(self) -> dict:
        with self._lock:
            open_loans = self._open_loans()
            return {
                "groups": len(self._groups),
                "open_loans": len(open_loans),
                "borrowed": sum(loan["shares"] for loan in open_loans),
                "used": sum(row["shares"] for row in self._charges),
                "returned": sum(
                    loan["status"] == self._LOAN_RETURNED
                    for loan in self._loans.values()
                ),
                "expired": sum(
                    loan["status"] == self._LOAN_EXPIRED
                    for loan in self._loans.values()
                ),
            }
