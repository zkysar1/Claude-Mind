"""Field precondition on update-goal ().

X-Mind-Expect-Field-Sha256 is a compare-and-swap precondition: the daemon
refuses the write (409 field_precondition_failed, NOTHING written) unless the
field's CURRENT text hashes to the given sha256, and the compare runs INSIDE
the update lock, the only place it is atomic with the write.

Why it exists: a caller that composes a new value from its own earlier read
(read -> transform -> replace) cannot close the window between that read and
the write from its side. goal-field-append.py's note-history rotation did
exactly that, and a peer append landing in the window was overwritten. No read
taken afterwards could see the loss, because the clobbered store is
byte-identical to the value the caller meant to write, so the check has to
happen before the write, under the lock.

Two lanes, both here: the daemon endpoint directly, and the production wrapper
driven with the literal argument shape the rotation sends (guard-920). The
wrapper lane also pins the confirmation echo the caller relies on to tell a
checked write from a stale daemon's unchecked 200 (guard-5505).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATE_WRAPPER = REPO_ROOT / "core" / "scripts" / "aspirations-update-goal.sh"
HEADER = "X-Mind-Expect-Field-Sha256"

# Multi-block prose over the field-shrink guard's 2000-char floor, so the
# combined case below exercises both gates on one write.
_V = "\n\n".join(f"block {i}: " + ("older history " * 30) for i in range(8))
_PEER = "\n\nblock P: a peer's append that must survive"
_REDUCED = "NOTE HISTORY ROTATED: the older blocks moved.\n\nblock 7: newest"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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


def _update_goal(port, goal_id, field, value, **kwargs):
    return _post(port, "/v1/aspirations/update-goal",
                 {"id": goal_id, "field": field, "source": "world"},
                 json.dumps(value).encode("utf-8"), **kwargs)


def _seed(port, *, title="Carries note history", description=_V, extras=None):
    # Distinct titles when one test seeds twice: an identical title is refused
    # by the duplication gate before the precondition is ever reached.
    goal = {"title": title, "status": "pending",
            "origin_signal": "user_directive", "description": description}
    goal.update(extras or {})
    code, body = _post(port, "/v1/aspirations/add-goal",
                       {"asp_id": "asp-001", "source": "world"},
                       json.dumps(goal).encode("utf-8"))
    assert code == 200, f"fixture seed failed: {code} {body}"
    return json.loads(body)["goal_id"]


def _stored(project_root: Path, goal_id: str) -> dict:
    """The goal as it sits in the fixture store, read without the daemon."""
    path = project_root / "world" / "aspirations.jsonl"
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            for g in json.loads(line).get("goals", []):
                if g.get("id") == goal_id:
                    return g
    raise AssertionError(f"{goal_id} not found in {path}")


# ---------------------------------------------------------------------------
# Daemon lane
# ---------------------------------------------------------------------------

def test_matching_hash_writes_and_echoes_the_check(running_daemon):
    project_root, port = running_daemon
    goal_id = _seed(port)

    code, body = _update_goal(port, goal_id, "description", _V + " appended",
                              headers={HEADER: _sha(_V)})

    assert code == 200, f"{code} {body}"
    assert json.loads(body)["precondition"] == {
        "checked": True, "field": "description", "sha256": _sha(_V)}
    assert _stored(project_root, goal_id)["description"] == _V + " appended"


def test_stale_hash_refuses_and_writes_nothing(running_daemon):
    """Refusal CONSTRUCTION is in scope: a bug while composing a deny silently
    converts it into an approval, and only a test that runs the branch sees it
    (guard-3803)."""
    project_root, port = running_daemon
    goal_id = _seed(port)

    code, body = _update_goal(port, goal_id, "description", _V + " appended",
                              headers={HEADER: _sha("some other text")})

    assert code == 409, f"a stale precondition was ALLOWED: {code} {body}"
    err = json.loads(body)
    assert err["error"] == "field_precondition_failed"
    assert "NOTHING WAS WRITTEN" in err["detail"]
    assert _stored(project_root, goal_id)["description"] == _V


def test_the_rotation_race_keeps_the_peer_append(running_daemon):
    """The incident, end to end: the caller reads V, a peer appends, and the
    caller then writes a value composed from V. Without the precondition that
    write lands and the peer's block is gone with nothing to show for it."""
    project_root, port = running_daemon
    goal_id = _seed(port)
    composed_from = _sha(_V)  # the caller's read

    peer_code, peer_body = _update_goal(port, goal_id, "description", _V + _PEER)
    assert peer_code == 200, f"{peer_code} {peer_body}"

    code, body = _update_goal(
        port, goal_id, "description", _REDUCED,
        headers={HEADER: composed_from,
                 "X-Mind-Override-Shrink": "note-history rotation (test)"})

    assert code == 409, f"the stale rotation was ALLOWED: {code} {body}"
    assert _stored(project_root, goal_id)["description"] == _V + _PEER


def test_malformed_hash_is_refused_before_anything_runs(running_daemon):
    project_root, port = running_daemon
    goal_id = _seed(port)

    code, body = _update_goal(port, goal_id, "description", _V + " appended",
                              headers={HEADER: "not-a-sha256"})

    assert code == 400, f"{code} {body}"
    assert json.loads(body)["error"] == "invalid_expect_sha256"
    assert _stored(project_root, goal_id)["description"] == _V


def test_hash_comparison_ignores_hex_case(running_daemon):
    project_root, port = running_daemon
    goal_id = _seed(port)

    code, body = _update_goal(port, goal_id, "description", _V + " appended",
                              headers={HEADER: _sha(_V).upper()})

    assert code == 200, f"{code} {body}"
    assert json.loads(body)["precondition"]["sha256"] == _sha(_V)


def test_absent_field_hashes_as_the_empty_string(running_daemon):
    """A caller that read an absent field composed from "", so that is what it
    sends; a non-empty expectation on an absent field is stale."""
    project_root, port = running_daemon
    goal_id = _seed(port)

    code, body = _update_goal(port, goal_id, "outcome_note", "first note",
                              headers={HEADER: _sha("stale expectation")})
    assert code == 409, f"{code} {body}"
    assert "outcome_note" not in _stored(project_root, goal_id)

    code, body = _update_goal(port, goal_id, "outcome_note", "first note",
                              headers={HEADER: _sha("")})
    assert code == 200, f"{code} {body}"
    assert _stored(project_root, goal_id)["outcome_note"] == "first note"


def test_non_text_field_is_refused(running_daemon):
    project_root, port = running_daemon
    goal_id = _seed(port, extras={"participants": ["agent"]})

    code, body = _update_goal(port, goal_id, "participants", ["agent"],
                              headers={HEADER: _sha("")})

    assert code == 409, f"{code} {body}"
    assert "non-text" in json.loads(body)["detail"]


def test_combined_with_override_shrink(running_daemon):
    """The rotation sends BOTH headers. A matching hash must still let the
    deliberate shrink through, and a stale one must be refused as stale (409),
    not reported as a shrink (400), with nothing written either way."""
    project_root, port = running_daemon
    shrink = {"X-Mind-Override-Shrink": "note-history rotation (test)"}

    ok_id = _seed(port)
    code, body = _update_goal(port, ok_id, "description", _REDUCED,
                              headers={HEADER: _sha(_V), **shrink})
    assert code == 200, f"{code} {body}"
    assert _stored(project_root, ok_id)["description"] == _REDUCED

    stale_id = _seed(port, title="Carries a second note history")
    code, body = _update_goal(port, stale_id, "description", _REDUCED,
                              headers={HEADER: _sha("stale"), **shrink})
    assert code == 409, f"{code} {body}"
    assert _stored(project_root, stale_id)["description"] == _V


def test_no_header_behaves_exactly_as_before(running_daemon):
    """The header is opt-in. Without it nothing is compared and nothing is
    echoed, so every existing caller is untouched."""
    project_root, port = running_daemon
    goal_id = _seed(port)

    code, body = _update_goal(port, goal_id, "description", _V + " appended")

    assert code == 200, f"{code} {body}"
    assert "precondition" not in json.loads(body)
    assert _stored(project_root, goal_id)["description"] == _V + " appended"


# ---------------------------------------------------------------------------
# Wrapper lane: the literal argument shape rotate_oversize sends (guard-920)
# ---------------------------------------------------------------------------

def _wrapper(args, *, project_root: Path, stdin: str = ""):
    env = os.environ.copy()
    env["MIND_AGENT"] = "alpha"
    env["MSYS_NO_PATHCONV"] = "1"
    env["RT_DIR"] = str(project_root / "mind_api" / "state")
    proc = subprocess.run(
        [shutil.which("bash") or "bash", UPDATE_WRAPPER.as_posix(), *args],
        env=env, input=stdin, capture_output=True, text=True, check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _rotation_args(goal_id, expect):
    return ["--source", "world", "--value-stdin",
            "--override-narrative-replace", "note-history rotation (test)",
            "--override-shrink", "note-history rotation (test)",
            "--expect-sha256", expect,
            goal_id, "description"]


def test_wrapper_confirms_a_checked_write(running_daemon):
    project_root, port = running_daemon
    goal_id = _seed(port)

    rc, _out, err = _wrapper(_rotation_args(goal_id, _sha(_V)),
                             project_root=project_root, stdin=_REDUCED)

    assert rc == 0, f"wrapper exit {rc}: {err}"
    assert f"[update-goal] precondition_checked field-sha256={_sha(_V)}" in err, err
    assert _stored(project_root, goal_id)["description"] == _REDUCED


def test_wrapper_refuses_a_stale_hash_and_keeps_the_peer_append(running_daemon):
    project_root, port = running_daemon
    goal_id = _seed(port)
    assert _update_goal(port, goal_id, "description", _V + _PEER)[0] == 200

    rc, _out, err = _wrapper(_rotation_args(goal_id, _sha(_V)),
                             project_root=project_root, stdin=_REDUCED)

    assert rc == 1, f"wrapper exit {rc}: {err}"
    assert "field_precondition_failed" in err, err
    assert "precondition_checked" not in err, err
    assert _stored(project_root, goal_id)["description"] == _V + _PEER


def test_wrapper_refuses_an_empty_expectation(running_daemon):
    """An empty value would send no header at all, turning the safety flag
    into a silent no-op, so the wrapper refuses it outright."""
    project_root, port = running_daemon
    goal_id = _seed(port)

    rc, _out, err = _wrapper(_rotation_args(goal_id, ""),
                             project_root=project_root, stdin=_REDUCED)

    assert rc == 2, f"wrapper exit {rc}: {err}"
    assert "--expect-sha256 needs a value" in err, err
    assert _stored(project_root, goal_id)["description"] == _V


def test_wrapper_prints_no_confirmation_when_nothing_was_checked(running_daemon):
    """Anti-vacuity for the echo: if it appeared on unchecked writes too, the
    caller's confirmation test would pass against a daemon that never ran it."""
    project_root, port = running_daemon
    goal_id = _seed(port)

    rc, _out, err = _wrapper([goal_id, "priority", "HIGH"],
                             project_root=project_root)

    assert rc == 0, f"wrapper exit {rc}: {err}"
    assert "precondition_checked" not in err, err
