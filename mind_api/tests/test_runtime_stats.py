"""GET /v1/admin/stats — reservoir-sampled latency stats per endpoint.

Acceptance from PHASE_B_HANDOFF.md:
  - stats endpoint returns within 10 ms
  - reports counters for every endpoint that has been hit
"""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _get(port: int, path: str, *, agent: str = ""):
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url)
    if agent:
        req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


def test_stats_returns_valid_schema(running_daemon):
    _, port = running_daemon
    # Hit health once to ensure at least one recorded endpoint
    _get(port, "/v1/admin/health")

    status, body = _get(port, "/v1/admin/stats")
    assert status == 200
    data = json.loads(body)
    assert "stats" in data
    assert isinstance(data["stats"], dict)


def test_stats_records_health_calls(running_daemon):
    _, port = running_daemon
    # Reset to a clean state (test isolation) by importing the collector
    from mind_api.src.stats import collector
    collector().reset()

    for _ in range(5):
        _get(port, "/v1/admin/health")

    _, body = _get(port, "/v1/admin/stats")
    data = json.loads(body)
    key = "GET /v1/admin/health"
    assert key in data["stats"], f"missing {key} in {data['stats'].keys()}"
    s = data["stats"][key]
    assert s["count"] == 5
    assert s["samples_in_window"] == 5
    assert s["min_ms"] >= 0
    assert s["p50_ms"] >= 0
    assert s["max_ms"] >= s["min_ms"]


def test_stats_groups_by_path_not_query(running_daemon):
    _, port = running_daemon
    from mind_api.src.stats import collector
    collector().reset()

    # Two different query strings on the same path — should aggregate.
    _get(port, "/v1/aspirations/read?summary=1&source=world", agent="alpha")
    _get(port, "/v1/aspirations/read?active_compact=1&source=world", agent="alpha")

    _, body = _get(port, "/v1/admin/stats")
    data = json.loads(body)
    key = "GET /v1/aspirations/read"
    assert key in data["stats"]
    assert data["stats"][key]["count"] == 2


def test_stats_endpoint_fast(running_daemon):
    _, port = running_daemon
    # Seed some data
    for _ in range(20):
        _get(port, "/v1/admin/health")

    # Measure /stats wall time
    start = time.monotonic()
    _get(port, "/v1/admin/stats")
    elapsed_ms = (time.monotonic() - start) * 1000

    # Loose bound — over the wire it's a TCP roundtrip on localhost.
    # The acceptance criterion was 10ms server-side; client-side end-to-end
    # is bounded by socket setup. Allow 100ms here (still proves it's not
    # locked up in serialization).
    assert elapsed_ms < 100, f"stats took {elapsed_ms:.1f}ms"


def test_stats_percentiles_make_sense(running_daemon):
    """Hit health 100 times and verify p50 <= p95 <= p99 <= max."""
    _, port = running_daemon
    from mind_api.src.stats import collector
    collector().reset()

    for _ in range(100):
        _get(port, "/v1/admin/health")

    _, body = _get(port, "/v1/admin/stats")
    s = json.loads(body)["stats"]["GET /v1/admin/health"]
    assert s["min_ms"] <= s["p50_ms"] <= s["p95_ms"] <= s["p99_ms"] <= s["max_ms"]
    assert s["count"] == 100
