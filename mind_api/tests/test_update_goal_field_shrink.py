"""Daemon lane of the field-shrink guard ().

THE DAEMON IS THE HOT PATH. `aspirations-update-goal.sh` is daemon-only (no
CLI fallback since the 2026-05-14 cutover), so every wrapper write lands HERE.
A guard wired only into `aspirations.py::cmd_update_goal` would be inert on all
real traffic — the exact wired-the-wrong-lane defect that let the takeover guard
sit CLI-only for ~3 months (g-306-230) and that
test_credential_enum_both_doors.py was written to catch.

The CLI twin of these assertions lives in
core/scripts/tests/test_field_shrink_guard.py, together with the predicate's
branch coverage and the structural both-doors pins. This file exercises the
daemon's own refusal path end to end against the hermetic tmp-world fixture.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


# Exact sizes from the incident: a 36,904-char description replaced by 3,467
# chars of UNRELATED goal prose (ratio 0.09). Sliced rather than computed from
# a repeat count — the first draft of the CLI fixture got that arithmetic wrong.
_LONG = ("ORIGINAL " * 4200)[:36904]
_SHORT = ("unrelated goal prose " * 200)[:3467]


def _post(port, path, query, body, *, agent="alpha", headers=None):
    qs = urllib.parse.urlencode(query)
    url = f"http://127.0.0.1:{port}{path}?{qs}" if qs else f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if agent:
        req.add_header("X-Mind-Agent", agent)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _add_goal(port, goal):
    return _post(port, "/v1/aspirations/add-goal",
                 {"asp_id": "asp-001", "source": "world"},
                 json.dumps(goal).encode("utf-8"))


def _update_goal(port, goal_id, field, value, **kwargs):
    return _post(port, "/v1/aspirations/update-goal",
                 {"id": goal_id, "field": field, "source": "world"},
                 json.dumps(value).encode("utf-8"), **kwargs)


def _seed(port):
    """A goal carrying long-lived prose. Returns its id."""
    code, body = _add_goal(port, {
        "title": "Carries long prose",
        "status": "pending",
        "origin_signal": "user_directive",
        "description": _LONG,
    })
    assert code == 200, f"fixture seed failed: {code} {body}"
    return json.loads(body)["goal"]["id"]


def _read_desc(port, goal_id):
    url = (f"http://127.0.0.1:{port}/v1/aspirations/read"
           f"?{urllib.parse.urlencode({'source': 'world', 'id': 'asp-001'})}")
    req = urllib.request.Request(url, method="GET")
    req.add_header("X-Mind-Agent", "alpha")
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    items = data if isinstance(data, list) else data.get("aspirations", [data])
    for asp in items:
        for g in (asp or {}).get("goals", []):
            if g.get("id") == goal_id:
                return g.get("description")
    raise AssertionError(f"{goal_id} not found in read response")


def test_daemon_refuses_the_catastrophic_shrink(running_daemon):
    """The incident's exact shape, through the endpoint that carries real
    traffic. Uses the REAL gate (no monkeypatch), so this verifies import +
    invocation + refusal construction, not just plumbing.

    Refusal-message CONSTRUCTION is deliberately in scope: guard-3803 — a bug
    while composing a deny silently converts it into an approval, and only a
    test that actually RUNS the branch can catch that."""
    _, port = running_daemon
    goal_id = _seed(port)

    code, body = _update_goal(port, goal_id, "description", _SHORT)

    assert code == 400, f"the catastrophic shrink was ALLOWED: {code} {body}"
    err = json.loads(body)
    assert err["error"] == "field_shrink_blocked"
    assert err["gate"] == "field-shrink-guard"
    assert err["old_len"] == 36904 and err["new_len"] == 3467
    assert "--override-shrink" in err["detail"]


def test_daemon_refusal_does_not_write(running_daemon):
    """A non-200 that still mutated the record would be the worst outcome —
    data lost AND the caller told it failed. Read the prose back after."""
    _, port = running_daemon
    goal_id = _seed(port)

    assert _update_goal(port, goal_id, "description", _SHORT)[0] == 400
    assert _read_desc(port, goal_id) == _LONG, \
        "the daemon refused but the write landed anyway"


def test_daemon_override_header_lets_it_through(running_daemon):
    """X-Mind-Override-Shrink is what `--override-shrink` becomes on the wire.
    A refusal whose escape hatch is unreachable on the hot path is a wedge."""
    _, port = running_daemon
    goal_id = _seed(port)

    code, body = _update_goal(
        port, goal_id, "description", _SHORT,
        headers={"X-Mind-Override-Shrink": "deliberate condense, verified"},
    )
    assert code == 200, f"{code} {body}"
    assert _read_desc(port, goal_id) == _SHORT


def test_daemon_lets_ordinary_writes_through(running_daemon):
    """Anti-vacuity on the hot path. Growth is what essentially every real
    description update does; refusing it would wedge the whole fleet."""
    _, port = running_daemon
    goal_id = _seed(port)

    code, body = _update_goal(port, goal_id, "description", _LONG + " appended")
    assert code == 200, f"{code} {body}"
    assert _read_desc(port, goal_id).endswith(" appended")


def test_daemon_ignores_unguarded_fields(running_daemon):
    """A title is MEANT to be replaced wholesale. The guard must be invisible
    to every field outside its list."""
    _, port = running_daemon
    goal_id = _seed(port)

    code, body = _update_goal(port, goal_id, "title", "x")
    assert code == 200, f"{code} {body}"
