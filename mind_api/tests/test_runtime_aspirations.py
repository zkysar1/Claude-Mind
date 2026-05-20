"""GET /v1/aspirations/read — daemon endpoint tests.

Tests the daemon read endpoint directly (daemon-only, no CLI fallback).
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request


def _get(port: int, path: str, query: dict, *, agent: str) -> tuple[int, str]:
    qs = urllib.parse.urlencode(query)
    url = f"http://127.0.0.1:{port}{path}?{qs}"
    req = urllib.request.Request(url)
    req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


def test_aspirations_active_compact(running_daemon):
    """Daemon returns parseable JSON list for --active-compact."""
    _, port = running_daemon
    status, body = _get(
        port, "/v1/aspirations/read",
        {"active_compact": "1", "source": "world"},
        agent="alpha",
    )
    assert status == 200
    data = json.loads(body)
    assert isinstance(data, list)


def test_aspirations_summary(running_daemon):
    """Daemon returns non-empty plain-text for --summary."""
    _, port = running_daemon
    status, body = _get(
        port, "/v1/aspirations/read",
        {"summary": "1", "source": "world"},
        agent="alpha",
    )
    assert status == 200
    assert len(body.strip()) > 0


def test_aspirations_id_present(running_daemon):
    project_root, port = running_daemon
    status, body = _get(
        port, "/v1/aspirations/read",
        {"id": "asp-001", "source": "world"},
        agent="alpha",
    )
    assert status == 200
    data = json.loads(body)
    assert data["id"] == "asp-001"


def test_aspirations_id_missing_404(running_daemon):
    _, port = running_daemon
    import urllib.error
    try:
        _get(
            port, "/v1/aspirations/read",
            {"id": "asp-999", "source": "world"},
            agent="alpha",
        )
    except urllib.error.HTTPError as e:
        assert e.code == 404
        err = json.loads(e.read().decode("utf-8"))
        assert err["error"] == "not_found"
    else:
        raise AssertionError("expected 404 for missing aspiration")


def test_aspirations_agent_source_routes_to_agent_dir(running_daemon):
    """source=agent must read from <agent>/aspirations.jsonl, not world/."""
    _, port = running_daemon
    status, body = _get(
        port, "/v1/aspirations/read",
        {"active_compact": "1", "source": "agent"},
        agent="alpha",
    )
    assert status == 200
    data = json.loads(body)
    # The conftest fixture wrote asp-100 to <agent>/aspirations.jsonl and
    # asp-001 to world/aspirations.jsonl. source=agent must yield asp-100.
    ids = [a["id"] for a in data]
    assert "asp-100" in ids
    assert "asp-001" not in ids


def test_aspirations_source_agent_requires_agent_header(running_daemon):
    """source=agent without an X-Mind-Agent header must 400."""
    _, port = running_daemon
    import urllib.error
    try:
        _get(
            port, "/v1/aspirations/read",
            {"active_compact": "1", "source": "agent"},
            agent="",   # no header
        )
    except urllib.error.HTTPError as e:
        assert e.code == 400
        err = json.loads(e.read().decode("utf-8"))
        assert err["error"] == "agent_unset"
    else:
        raise AssertionError("expected 400 for missing agent header")


def test_aspirations_missing_flag_400(running_daemon):
    _, port = running_daemon
    import urllib.error
    try:
        _get(
            port, "/v1/aspirations/read",
            {"source": "world"},   # no flag
            agent="alpha",
        )
    except urllib.error.HTTPError as e:
        assert e.code == 400
        err = json.loads(e.read().decode("utf-8"))
        assert err["error"] == "missing_flag"
    else:
        raise AssertionError("expected 400 when no flag is given")
