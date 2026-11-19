"""Locking and slice metadata storage.

RedisStore: a distributed lock (redis.lock.Lock) so concurrent API requests, the monitor and
several orchestrator replicas cannot interleave `tc` changes. MemoryStore: same interface for
tests and dry-run (single process only).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager

LOCK_NAME = "orchestrator:tc-lock"
SLICES_KEY = "orchestrator:slices"
LOCK_TIMEOUT_S = 10  # lock auto-expires if the holder dies
LOCK_WAIT_S = 5  # how long a caller waits for the lock


class LockTimeout(RuntimeError):
    pass


class MemoryStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._slices: dict = {}

    @contextmanager
    def lock(self) -> Iterator[None]:
        if not self._lock.acquire(timeout=LOCK_WAIT_S):
            raise LockTimeout("could not get the kernel lock")
        try:
            yield
        finally:
            self._lock.release()

    def save_slices(self, slices: dict) -> None:
        self._slices = json.loads(json.dumps(slices))

    def load_slices(self) -> dict:
        return self._slices


class RedisStore:
    def __init__(self, url: str) -> None:
        import redis  # imported here so dry-run/tests do not need a Redis server

        self._redis = redis
        self.client = redis.Redis.from_url(url, decode_responses=True)

    @contextmanager
    def lock(self) -> Iterator[None]:
        lk = self.client.lock(LOCK_NAME, timeout=LOCK_TIMEOUT_S, blocking_timeout=LOCK_WAIT_S)
        if not lk.acquire():
            raise LockTimeout("could not get the kernel lock")
        try:
            yield
        finally:
            try:
                lk.release()
            except self._redis.exceptions.LockError:
                pass  # expired while we held it; nothing to release

    def save_slices(self, slices: dict) -> None:
        self.client.set(SLICES_KEY, json.dumps(slices))

    def load_slices(self) -> dict:
        raw = self.client.get(SLICES_KEY)
        return json.loads(raw) if raw else {}


def make_store(redis_url: str) -> MemoryStore | RedisStore:
    return RedisStore(redis_url) if redis_url else MemoryStore()
