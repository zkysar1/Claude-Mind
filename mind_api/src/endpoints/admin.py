"""GET /v1/admin/stats — per-endpoint latency distribution.

Returns the current reservoir snapshot for every (method, path) the daemon
has served since startup. Useful for ops dashboards and drift detection
against the bench harness.

Schema:
    {
      "stats": {
        "GET /v1/aspirations/read": {
          "count": 1234,                  # total observed since start
          "samples_in_window": 1024,      # reservoir occupancy
          "min_ms": 1.2,
          "p50_ms": 15.3,
          "p95_ms": 78.1,
          "p99_ms": 134.0,
          "max_ms": 5482.1,
          "avg_ms": 22.4
        },
        ...
      }
    }
"""
from __future__ import annotations

from ..stats import collector


def stats(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    return Response.json({"stats": collector().snapshot()})


def register(routes) -> None:
    routes[("GET", "/v1/admin/stats")] = stats
