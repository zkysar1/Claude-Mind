"""test_candidate_transition_gate.py — B1c: the §2 candidate transition table,
enforced in BOTH status-update paths (g-353-136).

THE HOLE. goal-intake-management.md §2's transition table names five
candidate rows and says, for the last one, "validation error". Until this
goal, NOTHING in either write path enforced it: a direct
`update-goal <id> status in-progress` on an admitted candidate (g-353-82
made candidates selectable — the "candidate is invisible" premise is stale)
landed, and an intake row became an ordinary work item with no grooming
verdict and no ledger row.

WHAT THIS SUITE PINS:

  1. ALL FIVE §2 ROWS, on BOTH entry points (guard-742 twin):
       candidate -> pending     ALLOWED, ledgered (verdict "promote")
       candidate -> superseded  ALLOWED, ledgered (verdict "merge")
       candidate -> skipped     ALLOWED, ledgered (verdict "close-moot")
       candidate -> in-progress FORBIDDEN, refusal names the rule
       candidate -> completed   FORBIDDEN, refusal names the rule
  2. The §5 LEDGER: one row per allowed candidate transition, shaped
     {ts, agent, goal_id, verdict, evidence}, in world/
     candidate-grooming-ledger.jsonl, appended ONLY after the store write
     commits (a refusal leaves the file absent — no row for a write that
     never happened).
  3. THE CARVE-OUT, NOT A LOOSENING: the pre-existing blanket
     "no direct status=superseded" refusal (g-353-62) is narrowed for
     candidate prev-status ONLY. pending -> superseded and blocked ->
     superseded are still refused with the ORIGINAL direct-set message —
     not the table message — and still leave no ledger row.
  4. TWIN PARITY, STRUCTURAL (guard-742, the g-306-230 shape): the gate
     import, the in-lock evaluate call, the candidate carve-out condition,
     and the commit-site ledger append must all exist on BOTH sides —
     a file-wide grep passes on a deleted guard, so each assertion is
     scoped to the function span.
  5. THE PRODUCTION DOOR (guard-920): aspirations-update-goal.sh is
     daemon-only (2026-05-14 cutover), so one allowed transition is driven
     through the WRAPPER against a fixture daemon, not a bare HTTP call.
  6. PURE-MODULE ROWS: the five-row table, the fail-closed UNDEFINED row
     (candidate -> blocked is not in the table; the spec table is closed,
     and an undefined row silently passing is the same class of hole),
     the non-candidate abstention, and the §5 row shape from a direct
     append_ledger call.

Test-hygiene seams (why this file looks the way it does; guard-955, g-115-5753):

  * GATE_LOG_ALLOW_PYTEST=1 is exported at module import: without it the
    gate's own pytest suppression (g-115-5753, the guard-1041 pattern)
    correctly WAIVES ledger writes for this very test process — and the
    waiver would read as "no ledger row" on a transition that did commit.
    The suppression itself gets its own test (test_pytest_suppression)
    that runs with the flag UNSET via monkeypatch.
  * STORAGE_BACKEND=local is MANDATORY, not hygiene (guard-955): under
    own-cloud the S3 key ignores the tmp-world override and a fixture
    write lands on the PRODUCTION key.
  * The fixture repo (DaemonFixture) is not a git repository, so the
    pre-completion uncommitted-work gate (status=completed only) fail-opens
    on `git status` rc!=0 — the candidate -> completed refusal we assert
    is the TABLE refusal, which fires earlier anyway.
  * The fixture goals are UNCLAIMED and unrouted so the cross-lane
    takeover guard (CLI pre-lock, daemon in-lock) abstains and the table
    gate — not a stranger — owns every refusal asserted here.

Run: py -3 -m pytest core/scripts/tests/test_candidate_transition_gate.py -q
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
CLI_PATH = CORE_SCRIPTS / "aspirations.py"
DAEMON_PATH = (PROJECT_ROOT / "mind_api" / "src" / "endpoints"
               / "aspirations_write.py")
UPDATE_WRAPPER = CORE_SCRIPTS / "aspirations-update-goal.sh"
LEDGER_NAME = "candidate-grooming-ledger.jsonl"

# This suite ASSERTS on ledger rows, so it opts in to the gate's writer
# under pytest (see module docstring). Tests that need the suppression
# ACTIVE remove the flag via monkeypatch.
os.environ["GATE_LOG_ALLOW_PYTEST"] = "1"

sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(CORE_SCRIPTS))

from _bash_helpers import BASH  # noqa: E402  (guard-580: never a bare "bash")
from _daemon_fixture import DaemonFixture  # noqa: E402
from gates import candidate_transition as ct  # noqa: E402

AGENT = "alpha"
MY_SID = "99999999-0000-4000-8000-999999999999"

# The refusal must NAME the rule — the goal's explicit requirement. One
# substring that appears in BOTH forbidden messages and in the
# fail-closed undefined-row message (they share the RULE text).
RULE_MARKER = "goal-intake-management.md"
# The carve-out's NEGATIVE control: the pre-existing direct-set refusal,
# which must NOT be replaced by the table message when it still applies.
DIRECT_SET_MARKER = "Cannot set status=superseded directly"


# ---------------------------------------------------------------------------
# Shared fixture shape
# ---------------------------------------------------------------------------

def _goal(goal_id: str, status: str = "candidate", **extra) -> dict:
    g = {
        "id": goal_id,
        "title": f"candidate-table fixture {goal_id}",
        "description": "seeded by test_candidate_transition_gate (g-353-136)",
        "status": status,
        "priority": "MEDIUM",
        "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "spark",
        "participants": ["agent"],
    }
    g.update(extra)
    return g


def _make_world(tmp: Path, goal: dict) -> Path:
    world = tmp / "world"
    world.mkdir(parents=True, exist_ok=True)
    asp = {
        "id": "asp-995",
        "title": "candidate transition table fixtures",
        "motivation": "g-353-136 B1c regression",
        "scope": "project",
        "priority": "MEDIUM",
        "status": "active",
        "created": "2026-09-24T00:00:00",
        "goals": [goal],
    }
    (world / "aspirations.jsonl").write_text(
        json.dumps(asp, ensure_ascii=False) + "\n", encoding="utf-8")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world


def _goal_in(world: Path, goal_id: str) -> dict | None:
    for line in (world / "aspirations.jsonl").read_text(
            encoding="utf-8").splitlines():
        if not line.strip():
            continue
        for g in json.loads(line).get("goals", []):
            if g.get("id") == goal_id:
                return g
    return None


def _ledger_rows(world: Path, goal_id: str) -> list:
    path = world / LEDGER_NAME
    if not path.exists():
        return []
    return [json.loads(x) for x in
            path.read_text(encoding="utf-8").splitlines() if x.strip()
            and goal_id in x]


# ---------------------------------------------------------------------------
# Drivers
# ---------------------------------------------------------------------------

def _cli_update(world: Path, goal_id: str, value: str,
                extra_args: list | None = None) -> subprocess.CompletedProcess:
    """Drive the CLI twin directly — real argparse, real guard, same shape
    as test_update_goal_takeover_guard's Part B (not a transliteration,
    guard-1220)."""
    env = dict(os.environ)
    env["STORAGE_BACKEND"] = "local"
    env["MIND_WORLD"] = str(world)
    env["MIND_AGENT"] = AGENT
    env["MIND_SID"] = MY_SID
    cmd = [sys.executable, str(CLI_PATH), "update-goal", goal_id, "status",
           value]
    cmd += extra_args or []
    return subprocess.run(
        cmd,
        capture_output=True, text=True, env=env, cwd=str(PROJECT_ROOT),
        timeout=120)


def _daemon_update(port: int, goal_id: str, value: str,
                   headers: dict | None = None) -> tuple:
    url = (f"http://127.0.0.1:{port}/v1/aspirations/update-goal"
           f"?id={urllib.parse.quote(goal_id)}"
           f"&field={urllib.parse.quote('status')}"
           f"&source=world")
    data = json.dumps(value).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Mind-Agent", AGENT)
    req.add_header("X-Mind-Override-All", "test-fixture")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"raw": body}


# ---------------------------------------------------------------------------
# 1 + 2. THE FIVE ROWS — CLI path
# ---------------------------------------------------------------------------

ALLOWED_ROWS = [
    ("pending", "promote"),
    ("superseded", "merge"),
    ("skipped", "close-moot"),
]
FORBIDDEN_ROWS = ["in-progress", "completed"]


@pytest.mark.parametrize("new_status,verdict", ALLOWED_ROWS)
def test_cli_allowed_candidate_transitions_ledgered(new_status, verdict):
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), _goal("g-995-01"))
        res = _cli_update(world, "g-995-01", new_status)
        assert res.returncode == 0, (
            f"candidate -> {new_status} is ALLOWED by the §2 table; "
            f"rc={res.returncode} stderr={res.stderr!r}")
        g = _goal_in(world, "g-995-01")
        assert g is not None and g["status"] == new_status, (
            f"the write committed but the store does not agree: {g!r}")
        rows = _ledger_rows(world, "g-995-01")
        assert len(rows) == 1, (
            f"exactly ONE §5 row is due for a committed candidate "
            f"transition; got {len(rows)}: {rows!r}")
        row = rows[0]
        assert row["goal_id"] == "g-995-01"
        assert row["verdict"] == verdict, (
            f"§5 maps {new_status} -> {verdict!r}; got {row['verdict']!r}")
        assert row["evidence"]["from"] == "candidate"
        assert row["evidence"]["to"] == new_status
        assert "ts" in row and "agent" in row


@pytest.mark.parametrize("new_status", FORBIDDEN_ROWS)
def test_cli_forbidden_candidate_transitions_refused_and_name_the_rule(
        new_status):
    # candidate -> completed passes through the CLI's pre-completion
    # uncommitted-work gate before reaching the table: this repo is dirty
    # while this goal is in flight, so the gate's refusal would mask the
    # table's. The gate is BYPASSED for this one row with the canonical
    # --override-uncommitted (audited to the TMP world, never the live
    # one) so the TABLE refusal — the one under test — is the message we
    # see. The daemon half of this row needs no override: the fixture repo
    # is not a git repository, so its gate fail-opens.
    extra = (["--override-uncommitted",
              "g-353-136 test: unblock the table refusal under test"]
             if new_status == "completed" else None)
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), _goal("g-995-02"))
        res = _cli_update(world, "g-995-02", new_status, extra_args=extra)
        assert res.returncode != 0, (
            f"candidate -> {new_status} is FORBIDDEN by the §2 table and "
            f"must be refused; stderr={res.stderr!r}")
        assert RULE_MARKER in res.stderr, (
            f"the refusal must NAME the rule; stderr={res.stderr!r}")
        assert "BLOCKED" in res.stderr
        g = _goal_in(world, "g-995-02")
        assert g is not None and g["status"] == "candidate", (
            f"a refused transition must not mutate the store: {g!r}")
        assert _ledger_rows(world, "g-995-02") == [], (
            "no store write committed, so no ledger row may exist")


# ---------------------------------------------------------------------------
# 1 + 2. THE FIVE ROWS — daemon path (the live one)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("new_status,verdict", ALLOWED_ROWS)
def test_daemon_allowed_candidate_transitions_ledgered(new_status, verdict):
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), _goal("g-995-03"))
        with DaemonFixture(world, agent=AGENT) as df:
            code, body = _daemon_update(df.port, "g-995-03", new_status)
            assert code == 200, (
                f"candidate -> {new_status} is ALLOWED by the §2 table; "
                f"code={code} body={body!r}")
            g = _goal_in(world, "g-995-03")
            assert g is not None and g["status"] == new_status, (
                f"the write committed but the store does not agree: {g!r}")
            rows = _ledger_rows(world, "g-995-03")
            assert len(rows) == 1, (
                f"exactly ONE §5 row is due; got {len(rows)}: {rows!r}")
            row = rows[0]
            assert row["verdict"] == verdict
            assert row["evidence"]["from"] == "candidate"
            assert row["evidence"]["to"] == new_status
            # attribution travels as a header on this path
            assert row["agent"] == AGENT


@pytest.mark.parametrize("new_status", FORBIDDEN_ROWS)
def test_daemon_forbidden_candidate_transitions_refused_and_name_the_rule(
        new_status):
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), _goal("g-995-04"))
        with DaemonFixture(world, agent=AGENT) as df:
            code, body = _daemon_update(df.port, "g-995-04", new_status)
            assert code == 400, (
                f"candidate -> {new_status} is FORBIDDEN by the §2 table; "
                f"code={code} body={body!r}")
            detail = str(body.get("detail", ""))
            assert RULE_MARKER in detail, (
                f"the refusal must NAME the rule; body={body!r}")
            g = _goal_in(world, "g-995-04")
            assert g is not None and g["status"] == "candidate", (
                f"a refused transition must not mutate the store: {g!r}")
            assert _ledger_rows(world, "g-995-04") == [], (
                "no store write committed, so no ledger row may exist")


# ---------------------------------------------------------------------------
# 3. THE CARVE-OUT — non-candidate prev-status keeps the ORIGINAL refusal
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("prev_status", ["pending", "blocked"])
def test_cli_non_candidate_superseded_keeps_the_original_refusal(prev_status):
    """The carve-out narrows the  guard for candidate prev-status
    ONLY. A regression that replaced the guard with the table (or deleted
    it) would let this write land — or refuse it with the WRONG message,
    which would misroute the caller to grooming."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), _goal("g-995-05", status=prev_status))
        res = _cli_update(world, "g-995-05", "superseded")
        assert res.returncode != 0, (
            f"{prev_status} -> superseded is still refused by the "
            f"pre-existing direct-set guard; stderr={res.stderr!r}")
        assert DIRECT_SET_MARKER in res.stderr, (
            f"the ORIGINAL direct-set refusal must fire, not the table "
            f"message; stderr={res.stderr!r}")
        assert RULE_MARKER not in res.stderr, (
            f"the table owns the candidate row ONLY; a table message here "
            f"means the carve-out over-reached: {res.stderr!r}")
        g = _goal_in(world, "g-995-05")
        assert g["status"] == prev_status
        assert _ledger_rows(world, "g-995-05") == []


@pytest.mark.parametrize("prev_status", ["pending", "blocked"])
def test_daemon_non_candidate_superseded_keeps_the_original_refusal(prev_status):
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), _goal("g-995-06", status=prev_status))
        with DaemonFixture(world, agent=AGENT) as df:
            code, body = _daemon_update(df.port, "g-995-06", "superseded")
            assert code == 400, (
                f"{prev_status} -> superseded is still refused; "
                f"code={code} body={body!r}")
            assert body.get("error") == "invalid_status_transition", (
                f"the ORIGINAL guard's error code must survive the "
                f"carve-out; body={body!r}")
            detail = str(body.get("detail", ""))
            assert DIRECT_SET_MARKER in detail, detail
            assert RULE_MARKER not in detail, (
                f"a table message here means the carve-out over-reached: "
                f"{detail!r}")
            g = _goal_in(world, "g-995-06")
            assert g["status"] == prev_status
            assert _ledger_rows(world, "g-995-06") == []


def test_daemon_non_candidate_status_flips_pass_without_ledger():
    """Negative control that the table abstains for non-candidate rows:
    blocked -> pending is an ordinary status write. It must succeed AND
    leave no §5 row — a row for a non-candidate write would pollute the
    grooming census B2 reads."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), _goal("g-995-07", status="blocked"))
        with DaemonFixture(world, agent=AGENT) as df:
            code, body = _daemon_update(df.port, "g-995-07", "pending")
            assert code == 200, (
                f"blocked -> pending is not a table row and must pass; "
                f"code={code} body={body!r}")
            g = _goal_in(world, "g-995-07")
            assert g["status"] == "pending"
            assert _ledger_rows(world, "g-995-07") == [], (
                "the table owns the candidate row ONLY — a non-candidate "
                "transition must not ledger")


# ---------------------------------------------------------------------------
# 5. THE PRODUCTION DOOR — the daemon-only wrapper (guard-920)
# ---------------------------------------------------------------------------

def test_wrapper_path_candidate_promote_is_ledgered():
    """aspirations-update-goal.sh is daemon-only (2026-05-14), so every
    real status write lands on the daemon. Driving the WRAPPER proves the
    gate on the road, not beside it (guard-920: the literal production
    call shape)."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), _goal("g-995-08"))
        with DaemonFixture(world, agent=AGENT) as df:
            env = dict(os.environ)
            env["STORAGE_BACKEND"] = "local"
            env["MIND_WORLD"] = str(world)
            env["MIND_AGENT"] = AGENT
            env["MIND_SID"] = MY_SID
            env["RT_DIR"] = str(df.runtime_dir)   # the seam that holds
            res = subprocess.run(
                [BASH, UPDATE_WRAPPER.as_posix(), "g-995-08", "status",
                 "pending"],
                capture_output=True, text=True, env=env,
                cwd=str(PROJECT_ROOT), timeout=120)
            assert res.returncode == 0, (
                f"the wrapper's candidate -> pending must land; "
                f"rc={res.returncode} "
                f"out={(res.stderr or '') + (res.stdout or '')!r}")
            g = _goal_in(world, "g-995-08")
            assert g is not None and g["status"] == "pending", (
                f"the wrapper reported success but the store disagrees: "
                f"{g!r}")
            rows = _ledger_rows(world, "g-995-08")
            assert len(rows) == 1, (
                f"the promotion committed, so its §5 row must exist: "
                f"{rows!r}")
            assert rows[0]["verdict"] == "promote"


# ---------------------------------------------------------------------------
# 4. TWIN PARITY — structural (guard-742, the  shape)
# ---------------------------------------------------------------------------

def _func_span(path: Path, pattern: str) -> str:
    """Source text of the first top-level def matching `pattern`, terminated
    by the NEXT top-level def. Anchored to `def ` — a file-wide grep is
    the check a reader would naturally reach for, and it is exactly the
    check that cannot see a guard deleted from one function and left in
    another (the g-306-230 defect, this file's Part A pattern)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(pattern, line):
            start = i
            break
    assert start is not None, f"no def matching {pattern!r} in {path.name}"
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(r"^(async )?def ", lines[j]):
            end = j
            break
    return "\n".join(lines[start:end])


# Markers present on BOTH sides (the shared contract), plus one side-only
# marker per entry point: the daemon's 400 error CODE and the CLI's
# BLOCKED-to-stderr shape. A shared marker that only one side could carry
# would make the parity test pass on a deleted guard.
GATE_MARKERS = {
    "gate import": "gates.candidate_transition",
    "table evaluation": '_ct_eval(goal.get("status"), value)',
    "refusal on forbidden": 'if not _ct["allowed"]:',
    "candidate carve-out": 'goal.get("status") != "candidate"',
    "commit-site ledger": "_ct_row_due is not None",
}
CLI_EXTRA = "BLOCKED"          # CLI refusal shape: message to stderr
DAEMON_EXTRA = "candidate_transition_forbidden"  # daemon error code


def test_both_status_update_paths_carry_the_candidate_table_gate():
    """guard-742 parity. Either side drifting alone is the whole failure
    mode: the daemon is the live path, the CLI is the twin — a guard on
    one side only is a guard that does not exist for half the callers."""
    for path, pattern, extra in (
            (CLI_PATH, r"^def cmd_update_goal\(", CLI_EXTRA),
            (DAEMON_PATH, r"^(async )?def update_goal\(", DAEMON_EXTRA)):
        body = _func_span(path, pattern)
        missing = [label for label, marker in GATE_MARKERS.items()
                   if marker not in body]
        assert not missing, (
            f"{path.name}::{pattern} is missing candidate-table wiring "
            f"{missing} — the gate is half-applied.")
        assert extra in body, (
            f"{path.name} lost its side-specific refusal marker {extra!r}")


def test_cli_ledger_append_sits_after_the_store_write():
    """Ordering half of parity: the §5 row appends ONLY after the store
    write committed. In cmd_update_goal the commit is
    _write_live_under_lock; the _ct_row_due consumption must follow it,
    not precede it."""
    body = _func_span(CLI_PATH, r"^def cmd_update_goal\(")
    commit = body.index("_write_live_under_lock")
    ledger = body.index("_ct_row_due is not None")
    assert ledger > commit, (
        "the ledger append precedes the store write — a refused or failed "
        "write would still leave a row. Commit first, then audit.")


def test_daemon_ledger_append_sits_after_the_store_write():
    body = _func_span(DAEMON_PATH, r"^(async )?def update_goal\(")
    commit = body.index("_atomic_write_jsonl(live_path, items)")
    ledger = body.index("_ct_row_due is not None")
    assert ledger > commit, (
        "the ledger append precedes the store write — a refused or failed "
        "write would still leave a row. Commit first, then audit.")


# ---------------------------------------------------------------------------
# 6. PURE-MODULE ROWS
# ---------------------------------------------------------------------------

def test_pure_table_allows_the_three_grooming_rows():
    assert ct.evaluate("candidate", "pending")["allowed"]
    assert ct.evaluate("candidate", "pending")["verdict"] == "promote"
    assert ct.evaluate("candidate", "superseded")["verdict"] == "merge"
    assert ct.evaluate("candidate", "skipped")["verdict"] == "close-moot"
    assert ct.evaluate("candidate", "skipped")["ledger"]


def test_pure_table_forbids_the_two_work_rows():
    for new_status in ("in-progress", "completed"):
        out = ct.evaluate("candidate", new_status)
        assert out["allowed"] is False
        assert "BLOCKED" in out["message"]
        assert RULE_MARKER in out["message"], (
            "the refusal must name the rule")
        assert out["ledger"] is False


def test_pure_table_fails_closed_on_an_undefined_row():
    """candidate -> blocked is NOT in the §2 table. The table is closed:
    an undefined row silently passing is the same class of hole g-353-136
    closes, so undefined refuses."""
    out = ct.evaluate("candidate", "blocked")
    assert out["allowed"] is False
    assert out["ledger"] is False
    assert RULE_MARKER in out["message"]


def test_pure_table_abstains_for_non_candidate_prev_status():
    """The carve-out's module-level half: for any non-candidate prev the
    table must not vote — the other guards own that write."""
    for prev in ("pending", "blocked", "in-progress", "superseded"):
        out = ct.evaluate(prev, "superseded")
        assert out["allowed"] is True, f"{prev} -> superseded abstained?"
        assert out["ledger"] is False
        assert out["candidate"] is False


def test_pure_ledger_row_shape_and_fail_open(tmp_path):
    ok = ct.append_ledger(tmp_path, goal_id="g-995-09", new_status="pending",
                          agent=AGENT)
    assert ok is True
    lines = (tmp_path / LEDGER_NAME).read_text(
        encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    # §5: {ts, agent, goal_id, verdict, evidence} — the exact key set.
    assert set(row) == {"ts", "agent", "goal_id", "verdict", "evidence"}
    assert row["verdict"] == "promote"
    assert row["evidence"]["from"] == "candidate"
    assert row["evidence"]["to"] == "pending"
    assert row["evidence"]["rule"]

    # fail-open half: an unwritable world_dir must not raise. The parent
    # path is an existing FILE, so the ledger's directory cannot exist —
    # (a merely-absent dir is NOT a failure: locked_append_jsonl creates
    # missing parents, and the world dir is supposed to exist anyway.)
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir", encoding="utf-8")
    assert ct.append_ledger(blocker / "deeper", goal_id="g-995-09",
                            new_status="pending") is False


def test_pytest_suppression_waives_ledger_writes(tmp_path, monkeypatch):
    """ (the guard-1041 writer-side pattern): under pytest, with
    the opt-in flag UNSET, an allowed transition's ledger write is WAIVED
    so incidental code paths cannot append live rows — and the waiver
    reads as success, not failure (the transition stands either way)."""
    monkeypatch.delenv("GATE_LOG_ALLOW_PYTEST", raising=False)
    assert "PYTEST_CURRENT_TEST" in os.environ, (
        "this test only makes sense under pytest")
    assert ct.append_ledger(tmp_path, goal_id="g-995-10",
                            new_status="pending") is True
    assert not (tmp_path / LEDGER_NAME).exists(), (
        "suppression must waive the write, not merely fail it")


# ---------------------------------------------------------------------------
# The untouched-parity pin (the goal names it explicitly)
# ---------------------------------------------------------------------------

def test_terminal_goal_states_parity_untouched():
    """The goal requires test_terminal_goal_states_parity.py to remain
    UNTOUCHED: B1c is a status-UPDATE-path gate and must not re-shape the
    terminal-set SSOT parity tests. This pin fails (loudly, here) if that
    file starts importing the candidate gate — i.e. if someone widens B1c
    into the terminal-set machinery instead of filing a new goal for it."""
    parity = (SCRIPT_DIR / "test_terminal_goal_states_parity.py")
    text = parity.read_text(encoding="utf-8")
    assert "candidate_transition" not in text, (
        "test_terminal_goal_states_parity.py now references the candidate "
        "gate — B1c is the status-update path, not the terminal-set SSOT; "
        "if that coupling is deliberate, it is a new goal, not this one.")
    assert "candidate-grooming" not in text
