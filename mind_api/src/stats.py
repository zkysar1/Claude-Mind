"""Reservoir-sampled latency stats per endpoint.

For each (method, path) the daemon sees, store up to `RESERVOIR_K` samples
(default 1024) drawn uniformly at random from the full stream. Use those
samples to report p50/p95/p99 via `/v1/admin/stats`.

Why reservoir vs. sliding window:
    - Reservoir gives uniformly-distributed samples over the full lifetime
      of the daemon — useful for detecting slow drift.
    - A sliding window would track RECENT behavior only.
    - For ops, both are useful; reservoir is what the handoff specified.

Memory: 1024 floats × ~50 endpoints × 8 bytes ≈ 400 KB. Bounded.

Thread safety: single lock on the collector; per-endpoint Reservoir is
NOT thread-safe on its own. All writes go through the collector.
"""
from __future__ import annotations

import random
import threading
from typing import Any, Dict, List


RESERVOIR_K = 1024


class Reservoir:
    """Algorithm-R reservoir sampling. NOT thread-safe; use via StatsCollector."""

    __slots__ = ("k", "samples", "count")

    def __init__(self, k: int = RESERVOIR_K) -> None:
        self.k = k
        self.samples: List[float] = []
        self.count: int = 0  # total seen (not stored count)

    def add(self, value: float) -> None:
        self.count += 1
        if len(self.samples) < self.k:
            self.samples.append(value)
        else:
            # Standard Algorithm R: sample i (0-indexed = self.count-1)
            # replaces a uniformly random slot with probability k/(i+1).
            j = random.randint(0, self.count - 1)
            if j < self.k:
                self.samples[j] = value

    def snapshot(self) -> Dict[str, Any]:
        if not self.samples:
            return {"count": self.count, "samples_in_window": 0}
        s = sorted(self.samples)
        n = len(s)
        return {
            "count": self.count,
            "samples_in_window": n,
            "min_ms": round(s[0], 3),
            "p50_ms": round(s[_pct_index(n, 50)], 3),
            "p95_ms": round(s[_pct_index(n, 95)], 3),
            "p99_ms": round(s[_pct_index(n, 99)], 3),
            "max_ms": round(s[-1], 3),
            "avg_ms": round(sum(s) / n, 3),
        }


def _pct_index(n: int, p: int) -> int:
    """Nearest-rank percentile index, clamped to valid range."""
    # k = ceil(p/100 * n) - 1, matching bench/bench.py's `percentile`.
    return max(0, min(n - 1, int(round(p / 100.0 * n - 0.5))))


class StatsCollector:
    """Aggregates latency reservoirs per (method, path) key. Thread-safe."""

    def __init__(self, k: int = RESERVOIR_K) -> None:
        self.k = k
        self._reservoirs: Dict[str, Reservoir] = {}
        self._lock = threading.Lock()

    def record(self, endpoint_key: str, duration_ms: float) -> None:
        with self._lock:
            r = self._reservoirs.get(endpoint_key)
            if r is None:
                r = Reservoir(k=self.k)
                self._reservoirs[endpoint_key] = r
            r.add(duration_ms)

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {k: r.snapshot() for k, r in self._reservoirs.items()}

    def reset(self) -> None:
        """Clear all reservoirs. Used by tests."""
        with self._lock:
            self._reservoirs.clear()


_GLOBAL = StatsCollector()


def collector() -> StatsCollector:
    """Module-singleton collector. Server's access-log path calls record();
    /v1/admin/stats reads snapshot()."""
    return _GLOBAL
