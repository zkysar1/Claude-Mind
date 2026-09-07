"""Grant store SCRIPT API (, asp-368 P3).

`test_grants.py` pins the PURE policy module. This file pins the half that
touches a store: id allocation, in-lock validation, refusal codes, and the
membership resolution a base world answers from its own grants.

Every refusal test asserts the store is UNCHANGED as well as the exit code. A
refusal that still wrote the row would pass a code-only assertion while
corrupting the store, and for an AUTHORIZATION record that is the expensive
direction.
"""
import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import _grants as G  # noqa: E402
import grants as CLI  # noqa: E402


def _run(argv, stdin_text=None, capsys=None):
    """Invoke the CLI in-process. Returns (rc, parsed_stdout_or_None)."""
    if stdin_text is not None:
        sys.stdin = io.StringIO(stdin_text)
    try:
        rc = CLI.main(argv)
    finally:
        sys.stdin = sys.__stdin__
    out = capsys.readouterr().out.strip()
    try:
        return rc, json.loads(out) if out else None
    except json.JSONDecodeError:
        return rc, out


def _store(tmp_path):
    return str(tmp_path / "grants.jsonl")


def _rows(path):
    if not os.path.exists(path):
        return []
    return [json.loads(l) for l in open(path, encoding="utf-8")
            if l.strip() and not l.startswith("#")]


FIRST = {"from_env": "alpha-base", "to_env": "vinheim-env",
         "approved_by": "zachary", "approved_at": "2026-09-06T22:00:00",
         "scope": "/"}


# ── creation ────────────────────────────────────────────────────────────────

def test_creates_a_grant_binding_a_base_world_to_an_environment(tmp_path, capsys):
    """Outcome[0]: the record exists, and it names the edge it authorizes."""
    p = _store(tmp_path)
    rc, rec = _run(["--store", p, "add"], json.dumps(FIRST), capsys)
    assert rc == 0
    assert rec["from_env"] == "alpha-base" and rec["to_env"] == "vinheim-env"
    # Defaults the caller did not have to supply, but which the schema requires.
    assert rec["status"] == "active"
    assert rec["origin_env"] == "alpha-base"      # G5 default == from_env
    assert rec["scope"] == G.ROOT_SCOPE
    assert rec["grant_id"]
    assert len(_rows(p)) == 1


def test_a_second_grant_gets_a_distinct_id(tmp_path, capsys):
    """REGRESSION (found live while building this API, 2026-09-06).

    next_id_for_prefix defaults to id_field="id"; a grant's key is `grant_id`.
    With the default it matched nothing and returned "grant-1" for EVERY call,
    so the second grant into a store silently carried the first one's id. It
    fails quietly — a plausible id, rc=0, the row lands — so nothing but a
    MULTI-ROW test catches it. One-row tests all passed against the defect.
    """
    p = _store(tmp_path)
    _run(["--store", p, "add"], json.dumps(FIRST), capsys)
    second = {"from_env": "alpha-base", "to_env": "lodestar-env"}
    rc, rec = _run(["--store", p, "add"], json.dumps(second), capsys)
    assert rc == 0
    ids = [r["grant_id"] for r in _rows(p)]
    assert len(ids) == 2
    assert len(set(ids)) == 2, "allocator minted a duplicate grant_id: %r" % ids


def test_an_explicit_duplicate_grant_id_is_refused(tmp_path, capsys):
    p = _store(tmp_path)
    rc, rec = _run(["--store", p, "add"], json.dumps(FIRST), capsys)
    dup = dict(FIRST, to_env="lodestar-env", grant_id=rec["grant_id"])
    rc, out = _run(["--store", p, "add"], json.dumps(dup), capsys)
    assert rc == 2
    assert any("already exists" in v for v in out["violations"])
    assert len(_rows(p)) == 1, "a refused grant must not reach the store"


# ── rejection: malformed and unauthorized ───────────────────────────────────

def test_a_malformed_grant_is_refused_and_writes_nothing(tmp_path, capsys):
    p = _store(tmp_path)
    rc, out = _run(["--store", p, "add"], json.dumps({"to_env": "vinheim-env"}),
                   capsys)
    assert rc == 2
    assert any("missing required field" in v for v in out["violations"])
    assert _rows(p) == []


def test_a_non_json_body_is_refused(tmp_path, capsys):
    rc, out = _run(["--store", _store(tmp_path), "add"], "not json at all", capsys)
    assert rc == 2


def test_an_agent_may_not_approve_its_own_first_grant(tmp_path, capsys):
    """G3: the whole point of the guardrail — an agent cannot consent on its
    owner's behalf. Positive control is the FIRST test above, which creates the
    same edge with a human approver and succeeds."""
    p = _store(tmp_path)
    bad = dict(FIRST, approved_by="agent:alpha")
    rc, out = _run(["--store", p, "add"], json.dumps(bad), capsys)
    assert rc == 2
    assert any(v.startswith("G3") for v in out["violations"])
    assert _rows(p) == []


def test_a_cycle_is_refused_at_creation(tmp_path, capsys):
    """G4 — and the positive control is that the FORWARD edge was accepted."""
    p = _store(tmp_path)
    rc, _ = _run(["--store", p, "add"], json.dumps(FIRST), capsys)
    assert rc == 0
    back = {"from_env": "vinheim-env", "to_env": "alpha-base",
            "approved_by": "zachary", "approved_at": "2026-09-06T22:00:00"}
    rc, out = _run(["--store", p, "add"], json.dumps(back), capsys)
    assert rc == 2
    assert any(v.startswith("G4") for v in out["violations"])
    assert len(_rows(p)) == 1


# ── membership resolution ───────────────────────────────────────────────────

def test_membership_resolves_multiple_environments_from_the_base(tmp_path, capsys):
    """Outcome[1], end to end through the script API."""
    p = _store(tmp_path)
    _run(["--store", p, "add"], json.dumps(FIRST), capsys)
    _run(["--store", p, "add"],
         json.dumps({"from_env": "alpha-base", "to_env": "lodestar-env",
                     "scope": "intelligence/ayoai-architecture"}), capsys)
    rc, out = _run(["--store", p, "memberships", "--base-env", "alpha-base"],
                   None, capsys)
    assert rc == 0
    assert out["environments"] == ["lodestar-env", "vinheim-env"]
    assert out["count"] == 2                      # anti-vacuity floor
    assert out["scopes"]["vinheim-env"] == ["/"]
    assert out["scopes"]["lodestar-env"] == ["intelligence/ayoai-architecture"]


def test_membership_output_carries_the_gate_verdict_beside_each_edge(tmp_path, capsys):
    """Outcome[2]: membership and authorization are answered from the same rows,
    so a membership the gate denies is VISIBLE rather than silent."""
    p = _store(tmp_path)
    _run(["--store", p, "add"], json.dumps(FIRST), capsys)
    _run(["--store", p, "add"],
         json.dumps({"from_env": "alpha-base", "to_env": "lodestar-env"}), capsys)
    rc, out = _run(["--store", p, "memberships", "--base-env", "alpha-base"],
                   None, capsys)
    assert rc == 0
    assert set(out["environments"]) == {"lodestar-env", "vinheim-env"}
    # The second grant needed no approver at CREATION (G3 is first-grant-only)
    # but check_influence requires one at USE, so it is a member the gate denies.
    assert out["influence"]["vinheim-env"]["verdict"] == G.ALLOW
    assert out["influence"]["lodestar-env"]["verdict"] == G.DENY
    assert out["authorized"] == ["vinheim-env"]


def test_membership_of_a_base_with_no_grants_is_empty(tmp_path, capsys):
    """G1 default-private through the API. An absent store is zero grants, not
    an error — that distinction is what keeps default-private from becoming
    default-public."""
    rc, out = _run(["--store", _store(tmp_path), "memberships",
                    "--base-env", "nobody"], None, capsys)
    assert rc == 0
    assert out["environments"] == [] and out["count"] == 0


# ── check ───────────────────────────────────────────────────────────────────

def test_check_exit_codes_separate_allow_deny_and_scope_denial(tmp_path, capsys):
    p = _store(tmp_path)
    _run(["--store", p, "add"], json.dumps(dict(
        FIRST, to_env="lodestar-env", scope="intelligence/ayoai-architecture")),
        capsys)
    rc, out = _run(["--store", p, "check", "--from-env", "alpha-base",
                    "--to-env", "lodestar-env",
                    "--node", "intelligence/ayoai-architecture/platform-capabilities"],
                   None, capsys)
    assert rc == 0 and out["verdict"] == G.ALLOW
    rc, out = _run(["--store", p, "check", "--from-env", "alpha-base",
                    "--to-env", "lodestar-env", "--node", "system/unrelated"],
                   None, capsys)
    assert rc == 3 and out["verdict"] == G.DENY
    # An edge that was never granted at all — G1, and a DIFFERENT rc from an
    # unreadable store (4), which is the guard-142-vs-G1 split.
    rc, out = _run(["--store", p, "check", "--from-env", "alpha-base",
                    "--to-env", "never-granted"], None, capsys)
    assert rc == 3 and out["guardrail"] == "G1"


def test_add_refuses_fast_on_a_tty_instead_of_blocking_forever(tmp_path, capsys,
                                                               monkeypatch):
    """guard-979 / guard-2037: a $(cat) reader handed its payload as a flag
    blocks FOREVER on an inherited-open stdin (measured 14+ min). Refusing a TTY
    stdin turns that wedge into an immediate, self-describing error."""
    class _Tty(io.StringIO):
        def isatty(self):
            return True
    monkeypatch.setattr(sys, "stdin", _Tty(""))
    rc = CLI.main(["--store", _store(tmp_path), "add"])
    assert rc == 2
    assert "STDIN" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# reach subcommand (, P4)
# ---------------------------------------------------------------------------


def test_reach_reports_the_seed_list_and_counts_it(tmp_path, capsys):
    """`seed_required` is the number world-contract.md's "seed the grants that
    describe existing relationships, THEN arm" order keys on."""
    p = _store(tmp_path)
    _run(["--store", p, "add"], json.dumps(FIRST), capsys)
    rc, out = _run(["--store", p, "reach", "--base-env", "alpha-base",
                    "--registered-env", "vinheim-env",
                    "--registered-env", "lodestar-env"], None, capsys)
    assert rc == 0
    assert out["member_of"] == ["vinheim-env"]
    assert out["ungranted"] == ["lodestar-env"]
    assert out["seed_required"] == 1
    # The granted edge is also AUTHORIZED here, so member_of and authorized
    # agree — the control that keeps the next assertion meaningful.
    assert out["authorized"] == ["vinheim-env"]


def test_reach_registry_override_keeps_the_test_off_this_box_registry(tmp_path, capsys):
    """--registered-env mirrors --store: a test must not depend on THIS
    deployment's core/config/environments, or it changes meaning per box."""
    p = _store(tmp_path)
    rc, out = _run(["--store", p, "reach", "--base-env", "alpha-base",
                    "--registered-env", "only-this-one"], None, capsys)
    assert rc == 0
    assert out["registry_source"] == "flag"
    assert out["registered_count"] == 1
    assert out["ungranted"] == ["only-this-one"]
