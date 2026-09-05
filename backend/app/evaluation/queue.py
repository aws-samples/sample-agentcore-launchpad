"""Bounded-concurrency queue for batch evaluations / insights analyses.

AgentCore allows 5 active batch evaluations per account (hard quota), so runs
beyond the configured cap (`eval_max_concurrent_runs`, default 3 — headroom is
deliberate, other consumers in the account share the quota) QUEUE instead of
failing. Worker threads are persistent daemons created lazily up to the cap;
positions are exposed for the UI ("QUEUED · waiting for a slot").
"""

import queue
import threading
from collections.abc import Callable
from typing import Any

from app.core.config import get_settings


class EvalRunQueue:
    def __init__(self, max_concurrency: int = 1) -> None:
        self._max_concurrency = max(1, max_concurrency)
        self._queue: queue.Queue[tuple[str, Callable[[], None]]] = queue.Queue()
        self._lock = threading.Lock()
        self._pending: list[str] = []
        self._running: set[str] = set()
        # Cancelled while still pending: the worker drops these on dequeue
        # instead of calling the run's callable.
        self._cancelled: set[str] = set()
        self._workers: list[threading.Thread] = []

    @property
    def max_concurrency(self) -> int:
        return self._max_concurrency

    def submit(self, run_id: str, fn: Callable[[], None]) -> int:
        """Enqueue a run; returns its queue position (0 = will run next/now)."""
        with self._lock:
            self._pending.append(run_id)
            # Runs that must finish before a slot frees up for this one.
            position = max(
                0, len(self._pending) - 1 + len(self._running) - (self._max_concurrency - 1)
            )
        self._queue.put((run_id, fn))
        self._ensure_workers()
        return position

    def cancel(self, run_id: str) -> bool:
        """Drop a run that has not been dequeued yet. Returns True when the run
        was still pending (its callable will never execute — the worker skips
        it on dequeue); False when it is already running or unknown, in which
        case the caller has to stop the in-flight work itself."""
        with self._lock:
            if run_id not in self._pending:
                return False
            self._pending.remove(run_id)
            self._cancelled.add(run_id)
            return True

    def _ensure_workers(self) -> None:
        with self._lock:
            if len(self._workers) >= self._max_concurrency:
                return
            worker = threading.Thread(target=self._drain, daemon=True)
            self._workers.append(worker)
            worker.start()

    def _drain(self) -> None:
        # Persistent worker: blocking get, lives for the process. Dying on an
        # idle timeout would race submit() and strand a just-queued run.
        while True:
            run_id, fn = self._queue.get()
            with self._lock:
                cancelled = run_id in self._cancelled
                self._cancelled.discard(run_id)
                if cancelled:
                    self._queue.task_done()
                    continue
                if run_id in self._pending:
                    self._pending.remove(run_id)
                self._running.add(run_id)
            try:
                fn()
            except Exception:
                pass  # run status carries the failure; the queue must survive
            finally:
                with self._lock:
                    self._running.discard(run_id)
                self._queue.task_done()

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": sorted(self._running),
                "queued": list(self._pending),
                # "locked" = at capacity: one more run would have to wait.
                "locked": len(self._running) >= self._max_concurrency,
                "max_concurrency": self._max_concurrency,
            }

    def position(self, run_id: str) -> int | None:
        with self._lock:
            if run_id in self._running:
                return 0
            if run_id in self._pending:
                return self._pending.index(run_id) + 1
        return None


run_queue = EvalRunQueue(max_concurrency=get_settings().eval_max_concurrent_runs)
