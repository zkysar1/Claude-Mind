"""Regression pins for the  imp@k per-close idempotency key.

DEFECT: meta-impk `snapshot` appended unconditionally and then recomputed the
5/10/20 rolling averages from `entries[-w:]`. A SECOND snapshot for the same
close therefore double-weighted that goal in three windows at once. The only
defence was a comment in iteration-close.sh:2113 ("Do NOT run it after a
MEASURED close") — honour-system, and the post-hoc recovery command the
advisory prints is hand-typed by an LLM at exactly the moment it is easiest to
run twice.

WHY NOT goal_id AS THE KEY (measured on the live store 2026-08-09, 7,814
entries): 5,748 distinct goal_ids, 332 repeating, **2,066 repeat rows**,
dominated by asp-001 recurring-cadence goals (g-001-01 alone n=205). A repeat
snapshot is overwhelmingly LEGITIMATE — a recurring goal closes many times and
each close is real learning. Deduping on goal_id would have destroyed 2,066
valid rows. A time-window substitute was measured and rejected too: the store
is fleet-shared, so consecutive snapshots for DIFFERENT goals come as close as
1s (375 pairs under 60s) while legitimate same-goal recurring re-closes reach
down to 31s — the populations overlap, so no threshold separates them.

WHY NOT completed_at: falsified before it was written. Recurring goals carry
`completed_at: None` and advance `lastAchievedAt` instead, so a completed_at
key would collapse every recurring close to one key — reintroducing the exact
2,066-row destruction above.

THE KEY: `{goal_id}:{selected_at}` read from the iteration checkpoint. One
selection produces one close, so it names the close-EVENT rather than the goal.
It is derived inside state-update-audit's cmd_velocity from a LOCAL file
(`agents/<agent>/session/iteration-checkpoint.json`) — no daemon round-trip and
no caller changes, so the hand-typed post-hoc command is covered without
needing a new flag. The checkpoint is cleared by iteration-close's
do_productivity_check(), which runs AFTER do_state_update(), so it is live for
both the normal close and the post-hoc recovery window.

FAIL-OPEN EVERYWHERE: absent checkpoint, unreadable JSON, or a checkpoint
naming a DIFFERENT goal all yield "" -> no --close-key -> byte-identical to the
pre-fix behaviour. The fix can never suppress a row it was not certain about.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent.parent
IMPK_PY = SCRIPTS_DIR / "meta-impk.py"
AUDIT_PY = SCRIPTS_DIR / "state-update-audit.py"


def _import_audit():
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location("state_update_audit", AUDIT_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["state_update_audit"] = mod
    spec.loader.exec_module(mod)
    return mod


def _run_impk(meta: Path, args, check_rc=True):
    """Invoke the CLI meta-impk.py against a tmp META dir.

    STORAGE_BACKEND=local is MANDATORY, not tidiness (guard-955 / rb-2983):
    on an own-cloud box OwnCloudBackend._s3_key derives the key from
    customer_prefix+env_id+filename and IGNORES the MIND_META tmp override, so
    an unpinned test write lands on the PRODUCTION improvement-velocity.yaml.
    """
    env = dict(os.environ)
    env["STORAGE_BACKEND"] = "local"
    env["MIND_META"] = str(meta)
    env["MIND_WORLD"] = str(meta.parent / "world")
    env["MIND_AGENT"] = "alpha"
    p = subprocess.run(
        [sys.executable, str(IMPK_PY)] + list(args),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(REPO_ROOT), env=env, timeout=60,
    )
    if check_rc:
        assert p.returncode == 0, f"rc={p.returncode}\nstdout={p.stdout}\nstderr={p.stderr}"
    return p


def _entries(meta: Path):
    path = meta / "improvement-velocity.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("entries") or []


def _windows(meta: Path):
    path = meta / "improvement-velocity.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("rolling_averages") or {}


# ───────────────────────── CLI snapshot idempotency ─────────────────────────

def test_second_snapshot_same_close_key_is_suppressed(tmp_path):
    """THE pin. Two snapshots, one close key -> exactly one row."""
    meta = tmp_path / "meta"
    meta.mkdir()
    key = "g-115-4542:2026-08-09T18:26:55"

    first = _run_impk(meta, ["snapshot", "--goal-id", "g-115-4542",
                             "--learning-value", "0.80", "--close-key", key])
    assert json.loads(first.stdout)["status"] == "recorded"
    assert len(_entries(meta)) == 1

    second = _run_impk(meta, ["snapshot", "--goal-id", "g-115-4542",
                              "--learning-value", "0.80", "--close-key", key])
    assert json.loads(second.stdout)["status"] == "duplicate_suppressed"
    assert len(_entries(meta)) == 1, "second snapshot for the same close must not append"


def test_suppression_exits_zero(tmp_path):
    """Suppression is rc=0, not an error.

    iteration-close.sh prints 'WARN: state-update-audit.sh failed rc=N
    (velocity/backpressure snapshot not recorded)' on any non-zero rc. A
    correctly-suppressed duplicate is the system working, so a non-zero exit
    would manufacture a false alarm on every recovery run.
    """
    meta = tmp_path / "meta"
    meta.mkdir()
    key = "g-1-01:2026-08-09T10:00:00"
    _run_impk(meta, ["snapshot", "--goal-id", "g-1-01", "--learning-value", "0.5",
                     "--close-key", key])
    p = _run_impk(meta, ["snapshot", "--goal-id", "g-1-01", "--learning-value", "0.5",
                         "--close-key", key], check_rc=False)
    assert p.returncode == 0, f"suppression must exit 0, got {p.returncode}: {p.stderr}"


def test_suppression_leaves_rolling_averages_untouched(tmp_path):
    """The whole point: the windows must not absorb the duplicate.

    Asserts the DERIVED values directly rather than only the row count — the
    defect being fixed is a double-WEIGHTING, and a row-count assertion alone
    would pass through a variant that skipped the append but still recomputed.
    """
    meta = tmp_path / "meta"
    meta.mkdir()
    for i in range(5):
        _run_impk(meta, ["snapshot", "--goal-id", f"g-9-{i:02d}",
                         "--learning-value", "0.20",
                         "--close-key", f"g-9-{i:02d}:2026-08-09T09:0{i}:00"])
    dup_key = "g-9-99:2026-08-09T09:30:00"
    _run_impk(meta, ["snapshot", "--goal-id", "g-9-99", "--learning-value", "1.00",
                     "--close-key", dup_key])
    before = dict(_windows(meta))
    assert before["window_5"] == pytest.approx(0.36)  # (0.2*4 + 1.0)/5

    _run_impk(meta, ["snapshot", "--goal-id", "g-9-99", "--learning-value", "1.00",
                     "--close-key", dup_key])
    assert _windows(meta) == before, "rolling averages must not move on a suppressed duplicate"


def test_recurring_goal_closes_are_not_suppressed(tmp_path):
    """The 2,066-row protection, pinned.

    Same goal_id, DIFFERENT closes -> both rows must survive. This is the case
    a goal_id-keyed dedup would have destroyed; it is pinned so a future
    "simplification" to goal_id fails loudly here.
    """
    meta = tmp_path / "meta"
    meta.mkdir()
    _run_impk(meta, ["snapshot", "--goal-id", "g-001-01", "--learning-value", "0.4",
                     "--close-key", "g-001-01:2026-08-09T08:00:00"])
    _run_impk(meta, ["snapshot", "--goal-id", "g-001-01", "--learning-value", "0.6",
                     "--close-key", "g-001-01:2026-08-09T14:00:00"])
    rows = _entries(meta)
    assert len(rows) == 2, "two genuine closes of a recurring goal must both record"
    assert [r["learning_value"] for r in rows] == [0.4, 0.6]


def test_no_close_key_is_byte_identical_legacy_behaviour(tmp_path):
    """Absent --close-key -> unconditional append, exactly as before the fix.

    Guarantees the fail-open path can never lose a row, and that every caller
    that does not pass the flag is completely unaffected.
    """
    meta = tmp_path / "meta"
    meta.mkdir()
    for _ in range(3):
        out = _run_impk(meta, ["snapshot", "--goal-id", "g-7-07",
                               "--learning-value", "0.30"])
        assert json.loads(out.stdout)["status"] == "recorded"
    rows = _entries(meta)
    assert len(rows) == 3
    assert all("close_key" not in r for r in rows), \
        "no close_key supplied -> no close_key field written"


def test_close_key_is_persisted_on_the_entry(tmp_path):
    """The key must be stored, not just compared.

    Nothing else in the record identifies the close: `date` is write time, and
    the merge handler unions on the canonical form of the whole entry, so a
    key held only in memory could not survive a cross-box merge or a later
    re-run.
    """
    meta = tmp_path / "meta"
    meta.mkdir()
    key = "g-306-01:2026-08-09T12:00:00"
    _run_impk(meta, ["snapshot", "--goal-id", "g-306-01", "--learning-value", "0.7",
                     "--category", "framework", "--close-key", key])
    rows = _entries(meta)
    assert rows[0]["close_key"] == key


def test_distinct_goals_sharing_a_timestamp_are_not_confused(tmp_path):
    """The key is (goal, close), not a bare timestamp.

    The store is fleet-shared and different-goal snapshots land as close as 1s
    apart, so a timestamp-only key would suppress unrelated agents' rows.
    """
    meta = tmp_path / "meta"
    meta.mkdir()
    ts = "2026-08-09T15:00:00"
    _run_impk(meta, ["snapshot", "--goal-id", "g-a-01", "--learning-value", "0.5",
                     "--close-key", f"g-a-01:{ts}"])
    _run_impk(meta, ["snapshot", "--goal-id", "g-b-02", "--learning-value", "0.5",
                     "--close-key", f"g-b-02:{ts}"])
    assert len(_entries(meta)) == 2


# ─────────────────── close-key derivation from the checkpoint ───────────────────

def _write_checkpoint(agent_dir: Path, payload):
    sess = agent_dir / "session"
    sess.mkdir(parents=True, exist_ok=True)
    (sess / "iteration-checkpoint.json").write_text(
        json.dumps(payload), encoding="utf-8")


def test_close_key_derived_from_checkpoint(tmp_path, monkeypatch):
    mod = _import_audit()
    agent = tmp_path / "alpha"
    _write_checkpoint(agent, {"goal_id": "g-115-4542", "aspiration_id": "asp-115",
                              "source": "world", "phase": "selected",
                              "selected_at": "2026-08-09T18:26:55"})
    monkeypatch.setattr(mod, "AGENT_DIR", agent, raising=False)
    assert mod._close_key("g-115-4542") == "g-115-4542:2026-08-09T18:26:55"


def test_close_key_empty_when_checkpoint_names_another_goal(tmp_path, monkeypatch):
    """A stale checkpoint must not mint a key for the goal being closed.

    Fail-open: a wrong key could suppress a legitimate row, which is strictly
    worse than the double-count this fix exists to prevent.
    """
    mod = _import_audit()
    agent = tmp_path / "alpha"
    _write_checkpoint(agent, {"goal_id": "g-999-99",
                              "selected_at": "2026-08-09T18:26:55"})
    monkeypatch.setattr(mod, "AGENT_DIR", agent, raising=False)
    assert mod._close_key("g-115-4542") == ""


def test_close_key_empty_when_checkpoint_absent(tmp_path, monkeypatch):
    mod = _import_audit()
    agent = tmp_path / "alpha"
    (agent / "session").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "AGENT_DIR", agent, raising=False)
    assert mod._close_key("g-115-4542") == ""


def test_close_key_empty_on_unreadable_checkpoint(tmp_path, monkeypatch):
    mod = _import_audit()
    agent = tmp_path / "alpha"
    sess = agent / "session"
    sess.mkdir(parents=True, exist_ok=True)
    (sess / "iteration-checkpoint.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(mod, "AGENT_DIR", agent, raising=False)
    assert mod._close_key("g-115-4542") == ""


def test_close_key_empty_when_selected_at_missing(tmp_path, monkeypatch):
    """A checkpoint with no selected_at cannot name a close event."""
    mod = _import_audit()
    agent = tmp_path / "alpha"
    _write_checkpoint(agent, {"goal_id": "g-115-4542", "phase": "selected"})
    monkeypatch.setattr(mod, "AGENT_DIR", agent, raising=False)
    assert mod._close_key("g-115-4542") == ""


def test_close_key_empty_for_empty_goal_id(tmp_path, monkeypatch):
    mod = _import_audit()
    agent = tmp_path / "alpha"
    _write_checkpoint(agent, {"goal_id": "", "selected_at": "2026-08-09T18:26:55"})
    monkeypatch.setattr(mod, "AGENT_DIR", agent, raising=False)
    assert mod._close_key("") == ""


def test_wrapper_forwards_close_key_flag():
    """meta-impk.sh must parse --close-key and forward it as a query param.

    The wrapper is DAEMON-ONLY: its snapshot parser ends in `*) shift;;`, so an
    unrecognised flag is silently discarded and the daemon never sees it. A
    dedup that is dropped at the wrapper is a dedup that does not exist — this
    is the same class as the `--exploration-mode` flag state-update-audit
    already passes and this wrapper already swallows.

    VACUOUS on the substring form. This assertion was originally
    `"--close-key" in src` + `"close_key=" in src`, and a fresh-eyes mutation
    proved it PASSES on a mutant with the parse branch deleted: the WARNING
    COMMENT above that branch itself contains the literal `--close-key`, and
    the forwarding line supplies `close_key=` independently. So the test
    survived exactly the deletion it exists to catch. Two changes fix it:
    comments are stripped before matching (a comment can no longer satisfy a
    code assertion), and each half is matched as STRUCTURE rather than as a
    free-floating token. The two-way control at the bottom is what proves the
    predicate discriminates — an assertion nobody has seen fail is an
    assertion nobody has tested (guard-1220).
    """
    raw = (SCRIPTS_DIR / "meta-impk.sh").read_text(encoding="utf-8")
    # Strip whole-line comments — the defect above was a comment satisfying a
    # code assertion, so comments must not be visible to the match at all.
    code = "\n".join(ln for ln in raw.splitlines()
                     if not ln.lstrip().startswith("#"))

    parse_re = re.compile(r'--close-key\)\s*CLOSE_KEY=')
    fwd_re = re.compile(r'\[\s*-n\s*"\$CLOSE_KEY"\s*\]\s*&&\s*_append_q\s*"close_key=')

    assert parse_re.search(code), (
        "wrapper has no `--close-key) CLOSE_KEY=` parse branch. The snapshot "
        "parser ends in `*) shift;;`, so an unparsed flag is SILENTLY "
        "discarded and the daemon never sees it.")
    assert fwd_re.search(code), (
        "wrapper does not forward CLOSE_KEY as a close_key= query param "
        "guarded by a non-empty check")

    # Two-way control: delete ONLY the parse branch and assert the predicate
    # flips. Without this, a future edit that re-weakens the regex into a
    # substring match would go unnoticed — the test would still pass, which is
    # precisely how the original version read green through the real defect.
    mutant = re.sub(r'\n\s*--close-key\)\s*CLOSE_KEY=.*?;;', '', code, count=1)
    assert mutant != code, "mutation did not apply — control is inconclusive"
    assert not parse_re.search(mutant), (
        "PREDICATE IS VACUOUS: the parse-branch assertion still passes after "
        "the parse branch is deleted, so it cannot detect the defect it "
        "exists to catch.")


# ---------------------------------------------------------------------------
# cmd_velocity surfaces the daemon's suppression verdict ( fresh-eyes)
#
# The daemon returns duplicate_suppressed at rc=0 BY DESIGN, so rc alone cannot
# distinguish a dedup that fired from a _close_key that has silently degraded to
# "". These tests pin that the verdict reaches the audit record's flags, and the
# recorded-path test is the two-way control that proves the flag discriminates
# rather than always appearing.
# ---------------------------------------------------------------------------

def _velocity_args(goal_id):
    import types
    return types.SimpleNamespace(
        goal=goal_id, tree_updated=True, artifacts_count=2,
        encoding_score=0.7, findings_count=1, exploration=False,
        category=None,
    )


def _patch_run(mod, monkeypatch, impk_stdout, impk_rc=0):
    """Stub _run: backpressure returns empty, impk returns the given payload."""
    def fake_run(argv, *a, **kw):
        if argv and "meta-impk.sh" in argv[0]:
            return impk_stdout, "", impk_rc
        return "", "", 0
    monkeypatch.setattr(mod, "_run", fake_run, raising=True)


def _audit_with_checkpoint(tmp_path, monkeypatch, goal_id):
    mod = _import_audit()
    agent = tmp_path / "alpha"
    _write_checkpoint(agent, {"goal_id": goal_id,
                              "selected_at": "2026-08-09T18:53:48"})
    monkeypatch.setattr(mod, "AGENT_DIR", agent, raising=False)
    return mod


def test_velocity_flags_duplicate_suppression(tmp_path, monkeypatch):
    mod = _audit_with_checkpoint(tmp_path, monkeypatch, "g-001-01")
    _patch_run(mod, monkeypatch, json.dumps({
        "status": "duplicate_suppressed", "goal_id": "g-001-01",
        "learning_value": 0.7, "close_key": "g-001-01:2026-08-09T18:53:48"}))
    out = mod.cmd_velocity(_velocity_args("g-001-01"))
    assert "impk_duplicate_suppressed" in out["flags"]
    assert out["impk_rc"] == 0, "suppression must stay rc=0 (not an error)"


def test_velocity_no_flag_on_recorded(tmp_path, monkeypatch):
    """Two-way control: the flag must be ABSENT on a normal recorded close."""
    mod = _audit_with_checkpoint(tmp_path, monkeypatch, "g-001-01")
    _patch_run(mod, monkeypatch, json.dumps({
        "status": "recorded", "goal_id": "g-001-01", "learning_value": 0.7}))
    out = mod.cmd_velocity(_velocity_args("g-001-01"))
    assert "impk_duplicate_suppressed" not in out["flags"], (
        "FLAG IS VACUOUS: it appears on a recorded close too, so it cannot "
        "distinguish a firing dedup from a silently-inert one.")
    assert out["flags"] == []


def test_velocity_suppression_parse_is_fail_open(tmp_path, monkeypatch):
    """An unreadable payload adds no flag and never breaks the audit record."""
    mod = _audit_with_checkpoint(tmp_path, monkeypatch, "g-001-01")
    _patch_run(mod, monkeypatch, "not json at all")
    out = mod.cmd_velocity(_velocity_args("g-001-01"))
    assert out["flags"] == []
    assert out["learning_value"] is not None


def test_velocity_suppression_flag_is_not_hard_fail(tmp_path, monkeypatch):
    """The flag is informational — it must not drive a non-zero exit."""
    mod = _import_audit()
    assert not mod._has_hard_failure(["impk_duplicate_suppressed"])
