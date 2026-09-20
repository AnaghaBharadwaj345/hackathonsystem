"""
In-memory registry of live simulation runs.

A hackathon does not need Postgres, but it does need two things this gives you:
a run must not be mutated by two requests at once, and abandoned runs must not
leak memory. Swap the dict for Redis later and the rest of the app is unchanged.
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Dict, List, Optional

from .sim import Sim

MAX_RUNS = 200
TTL_SECONDS = 60 * 60  # an untouched run is discarded after an hour


class RunHandle:
    """A simulation plus its own lock, so concurrent requests cannot interleave."""

    __slots__ = ("run_id", "sim", "created_at", "touched_at", "lock")

    def __init__(self, run_id: str, sim: Sim) -> None:
        self.run_id = run_id
        self.sim = sim
        self.created_at = time.time()
        self.touched_at = self.created_at
        self.lock = threading.Lock()

    def touch(self) -> None:
        self.touched_at = time.time()

    def summary(self) -> Dict:
        return {
            "run_id": self.run_id,
            "seed": self.sim.seed,
            "policy": self.sim.policy,
            "load_pct": round(self.sim.load * 100),
            "t": self.sim.t,
            "clock": self.sim.clock(self.sim.t),
            "horizon_min": self.sim.horizon,
            "finished": self.sim.t >= self.sim.horizon,
            "created_at": self.created_at,
            "capacity": self.sim.capacity,
        }


class RunStore:
    def __init__(self) -> None:
        self._runs: Dict[str, RunHandle] = {}
        self._guard = threading.Lock()

    def _evict(self) -> None:
        now = time.time()
        stale = [k for k, h in self._runs.items() if now - h.touched_at > TTL_SECONDS]
        for k in stale:
            self._runs.pop(k, None)
        if len(self._runs) > MAX_RUNS:
            oldest = sorted(self._runs.items(), key=lambda kv: kv[1].touched_at)
            for k, _ in oldest[: len(self._runs) - MAX_RUNS]:
                self._runs.pop(k, None)

    def create(self, sim: Sim) -> RunHandle:
        with self._guard:
            self._evict()
            run_id = uuid.uuid4().hex[:12]
            handle = RunHandle(run_id, sim)
            self._runs[run_id] = handle
            return handle

    def get(self, run_id: str) -> Optional[RunHandle]:
        with self._guard:
            handle = self._runs.get(run_id)
        if handle:
            handle.touch()
        return handle

    def delete(self, run_id: str) -> bool:
        with self._guard:
            return self._runs.pop(run_id, None) is not None

    def list(self) -> List[Dict]:
        with self._guard:
            handles = list(self._runs.values())
        return sorted((h.summary() for h in handles),
                      key=lambda s: s["created_at"], reverse=True)

    def count(self) -> int:
        with self._guard:
            return len(self._runs)


store = RunStore()
