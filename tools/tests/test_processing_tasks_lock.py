"""Regression tests: _processing_tasks dict is thread-safe (US-010 / ralph 2026-05-13).

Audit finding: tools/app/server.py has 7 _processing_tasks.pop() calls and the
_evict_stale_tasks() sweep's .items() iteration that historically happened
WITHOUT _processing_lock. Race window: scheduler eviction could pop a key in
the same millisecond a webhook handler was processing the dedup check.

May 14-15 night will exercise concurrent recording pipelines for the first
time (Group #3 and Group #4 webhooks may co-fire). These tests assert that
high-contention concurrent access to the dict raises no RuntimeError
("dictionary changed size during iteration") or KeyError.

The tests are threading-only (threading.Thread + Barrier). They do NOT use
asyncio — the lock under test is threading.Lock, not asyncio.Lock.

Run with:
    pytest tools/tests/test_processing_tasks_lock.py -v
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta

import pytest

# Pop stubs for packages we need real implementations of (matches the
# pattern used by test_server.py — see tools/tests/conftest.py header).
for _mod_name in list(sys.modules):
    if _mod_name.startswith(("fastapi", "slowapi", "httpx", "pydantic", "tools.app.server")):
        sys.modules.pop(_mod_name, None)

from tools.app import server  # noqa: E402
from tools.core.config import TBILISI_TZ  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_processing_tasks():
    """Reset the module-level dict before and after every test."""
    with server._processing_lock:
        server._processing_tasks.clear()
    yield
    with server._processing_lock:
        server._processing_tasks.clear()


def _seed_tasks(count: int, prefix: str = "seed") -> list[str]:
    """Populate _processing_tasks with `count` keys, return the key list."""
    keys = [f"{prefix}_{i}" for i in range(count)]
    now = datetime.now(tz=TBILISI_TZ)
    with server._processing_lock:
        for k in keys:
            server._processing_tasks[k] = now
    return keys


def test_concurrent_pop_does_not_raise():
    """N threads pop keys from _processing_tasks simultaneously.

    Asserts no RuntimeError ("dictionary changed size during iteration")
    and no KeyError. The pop pattern in production uses pop(key, None) so
    missing keys are silently ignored — these tests rely on that.
    """
    n_threads = 32
    n_keys_per_thread = 20
    total_keys = n_threads * n_keys_per_thread
    all_keys = _seed_tasks(total_keys, prefix="popper")

    barrier = threading.Barrier(n_threads)
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def worker(idx: int) -> None:
        my_keys = all_keys[idx * n_keys_per_thread : (idx + 1) * n_keys_per_thread]
        try:
            barrier.wait(timeout=10)
            for k in my_keys:
                # Production pattern: lock-guarded pop with default None.
                with server._processing_lock:
                    server._processing_tasks.pop(k, None)
                # Hammer with a redundant pop of the SAME key to exercise
                # the missing-key path under contention.
                with server._processing_lock:
                    server._processing_tasks.pop(k, None)
        except BaseException as exc:  # noqa: BLE001
            with errors_lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors, f"Concurrent pop raised: {errors[:3]}"
    with server._processing_lock:
        remaining = dict(server._processing_tasks)
    assert remaining == {}, f"Dict should be empty after all pops, got {len(remaining)} keys"


def test_eviction_during_active_processing():
    """Eviction sweep races with webhook handlers adding/popping keys.

    Simulates the real-world race: _evict_stale_tasks() iterates the dict
    while multiple webhook-style threads are concurrently inserting new
    keys and popping their own keys. No corruption should occur.
    """
    # Seed with stale entries (older than STALE_TASK_HOURS) so eviction has
    # work to do, plus fresh entries that must NOT be evicted.
    stale_cutoff = datetime.now(tz=TBILISI_TZ) - timedelta(
        hours=server.STALE_TASK_HOURS + 1
    )
    fresh_now = datetime.now(tz=TBILISI_TZ)
    with server._processing_lock:
        for i in range(50):
            server._processing_tasks[f"stale_{i}"] = stale_cutoff
        for i in range(50):
            server._processing_tasks[f"fresh_{i}"] = fresh_now

    n_webhook_threads = 16
    n_eviction_threads = 4
    total_threads = n_webhook_threads + n_eviction_threads
    barrier = threading.Barrier(total_threads)
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def webhook_worker(idx: int) -> None:
        try:
            barrier.wait(timeout=10)
            for i in range(30):
                key = f"webhook_{idx}_{i}"
                # Insert under lock (matches production handlers).
                with server._processing_lock:
                    server._processing_tasks[key] = datetime.now(tz=TBILISI_TZ)
                # Pop under lock (matches production finally blocks).
                with server._processing_lock:
                    server._processing_tasks.pop(key, None)
        except BaseException as exc:  # noqa: BLE001
            with errors_lock:
                errors.append(exc)

    def eviction_worker() -> None:
        try:
            barrier.wait(timeout=10)
            for _ in range(20):
                # Production eviction path — must use the snapshot pattern
                # internally so iteration never sees a mutating dict.
                server._evict_stale_tasks()
        except BaseException as exc:  # noqa: BLE001
            with errors_lock:
                errors.append(exc)

    threads: list[threading.Thread] = []
    for i in range(n_webhook_threads):
        threads.append(threading.Thread(target=webhook_worker, args=(i,)))
    for _ in range(n_eviction_threads):
        threads.append(threading.Thread(target=eviction_worker))

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert not errors, f"Concurrent eviction+webhook raised: {errors[:3]}"

    # All stale_* keys should be gone; webhook_* keys popped by their own
    # workers; only fresh_* keys (never popped, never stale) survive.
    with server._processing_lock:
        remaining = dict(server._processing_tasks)
    leftover_stale = [k for k in remaining if k.startswith("stale_")]
    leftover_webhook = [k for k in remaining if k.startswith("webhook_")]
    fresh_kept = [k for k in remaining if k.startswith("fresh_")]
    assert leftover_stale == [], f"Stale keys not evicted: {leftover_stale[:5]}"
    assert leftover_webhook == [], f"Webhook keys not cleaned up: {leftover_webhook[:5]}"
    assert len(fresh_kept) == 50, f"Fresh keys mistakenly evicted: kept {len(fresh_kept)}/50"


def test_evict_iterates_snapshot_not_live_dict():
    """The eviction sweep must NOT iterate the live dict.

    Regression for the specific bug: iterating _processing_tasks.items()
    while another thread mutates it raises RuntimeError. Stress the sweep
    while a mutator thread churns the dict at high frequency.
    """
    stale_cutoff = datetime.now(tz=TBILISI_TZ) - timedelta(
        hours=server.STALE_TASK_HOURS + 1
    )
    with server._processing_lock:
        for i in range(200):
            server._processing_tasks[f"churn_stale_{i}"] = stale_cutoff

    stop = threading.Event()
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def churner() -> None:
        i = 0
        try:
            while not stop.is_set():
                with server._processing_lock:
                    server._processing_tasks[f"churn_fresh_{i}"] = datetime.now(
                        tz=TBILISI_TZ
                    )
                with server._processing_lock:
                    server._processing_tasks.pop(f"churn_fresh_{i}", None)
                i += 1
        except BaseException as exc:  # noqa: BLE001
            with errors_lock:
                errors.append(exc)

    def evictor() -> None:
        try:
            for _ in range(30):
                server._evict_stale_tasks()
        except BaseException as exc:  # noqa: BLE001
            with errors_lock:
                errors.append(exc)

    churn_threads = [threading.Thread(target=churner) for _ in range(4)]
    for t in churn_threads:
        t.start()
    evict_threads = [threading.Thread(target=evictor) for _ in range(2)]
    for t in evict_threads:
        t.start()
    for t in evict_threads:
        t.join(timeout=15)
    stop.set()
    for t in churn_threads:
        t.join(timeout=15)

    assert not errors, (
        "Eviction iteration must not race with mutators "
        f"(snapshot pattern broken): {errors[:3]}"
    )
