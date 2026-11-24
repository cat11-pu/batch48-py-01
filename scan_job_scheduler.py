"""Lease based scan job scheduler with priority and quota accounting."""

import threading

PRIORITIES = ("critical", "normal", "low")
PRIORITY_RANK = {"critical": 0, "normal": 1, "low": 2}


class SchedulerError(ValueError):
    """Raised for every rejected scheduling operation."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class ScanJobScheduler:
    """Dispatch scan jobs to workers under per-target and global quotas."""

    def __init__(self, *, per_target_limit=2, global_limit=256, worker_limit=32,
                 max_attempts=5):
        self.per_target_limit = per_target_limit
        self.global_limit = global_limit
        self.worker_limit = worker_limit
        self.max_attempts = max_attempts
        self._lock = threading.RLock()

    def submit(self, job):
        raise NotImplementedError

    def lease(self, worker, now, ttl=60.0):
        raise NotImplementedError

    def heartbeat(self, job_id, now, ttl=60.0):
        raise NotImplementedError

    def complete(self, job_id, now):
        raise NotImplementedError

    def fail(self, job_id, now, reason):
        raise NotImplementedError

    def requeue_expired(self, now):
        raise NotImplementedError

    def job_state(self, job_id):
        raise NotImplementedError

    def dead_letters(self):
        raise NotImplementedError

    def pending(self):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, state):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
