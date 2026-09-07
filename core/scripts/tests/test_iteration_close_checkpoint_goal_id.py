#!/usr/bin/env python3
"""test_iteration_close_checkpoint_goal_id.py —  regression tests.

`iteration-close.sh::_checkpoint_refresh` took ONE argument (the phase name) and
called `loop-state-save.sh update` with no goal identity at all, so it stamped
`phase_completed`/`last_updated` onto whatever checkpoint happened to be on
disk. Measured on the coach deployment 2026-09-02 (g-025-01, asp-025): status
was hand-set to completed at 23:30:14, autocompact landed at 23:34, and
`iteration-close.sh --phase verify` ran late at 23:43 — with no Phase 2.95
anchor for g-025-01, the only record on disk was a STALE checkpoint for
g-023-01 (selected 18:06), and the refresh advanced THAT goal's record. A
different goal then read as freshly progressed to every downstream reader while
g-025-01's close chain had never run.

The defect was a PATH ASYMMETRY inside the writer, not a missing check in the
shell: `cmd_clear` already validated goal identity via `--if-goal`, and
`cmd_update` — the path the refresh actually uses — did not. The fix reuses that
same flag rather than adding a second comparison.

WHY REFUSE RATHER THAN REPLACE. The goal's outcome sanctioned either. Replacing
would fabricate a Phase-2.95 anchor that never existed, which is the same
"invent state rather than surface the miss" step the missing-checkpoint branch
of cmd_update already declines to take. Refusing leaves the stale record stale
(it was already wrong) instead of making it actively false.

WHY THE REFUSAL EXITS 0. cmd_update's own docstring pins this: the
iteration-close.sh call sites read a nonzero rc as an ITERATION failure. A
wrong-goal refresh must not abort a close, so observability is carried by the
stderr WARN plus the durable ledger — not by the exit code. `|| true` therefore
stays at the call sites, and dropping it would have been the wrong fix.

METHOD NOTE, inherited from test_loop_state_clear_if_goal.py and load-bearing:
these tests drive `cmd_update` IN-PROCESS with `_checkpoint_path` monkeypatched.
The shell-level version LOOKS hermetic and is NOT — `_agent_dir()` reads
MIND_AGENT and resolves through `_paths.agent_dir(name)`, so a tmp-dir env
override never reaches it and the "tmp" test would seed and overwrite the REAL
session's live checkpoint while reporting green.

Pure unit test: tmpdir + monkeypatch. No S3, no daemon, no world I/O.
"""
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]  # core/scripts
PROJECT_ROOT = SCRIPTS.parents[1]
for _p in (str(SCRIPTS), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load(mod_name, file_name):
    spec = importlib.util.spec_from_file_location(mod_name, str(SCRIPTS / file_name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_lss = _load("loop_state_save_g35784", "loop-state-save.py")

ITERATION_CLOSE = SCRIPTS / "iteration-close.sh"

# The three phases whose close path calls _checkpoint_refresh. Named here so a
# fourth call site added later without a goal id fails the COUNT assertion below
# rather than sliding in silently (guard-2921).
REFRESH_PHASES = ("verify", "state_update", "learning_gate")


class _Args:
    """Stand-in for the argparse namespace cmd_update receives.

    `set` carries the LITERAL two --set pairs the production call site passes
    (guard-920: a test exercising only a contract-ideal single-key shape cannot
    catch a caller that passes something else).
    """

    def __init__(self, if_goal=None, set_pairs=None):
        self.if_goal = if_goal
        self.set = list(set_pairs) if set_pairs is not None else [
            "phase_completed=verify",
            "last_updated=2026-09-02T23:43:00",
        ]


@pytest.fixture()
def ckpt(tmp_path, monkeypatch):
    path = tmp_path / "session" / "iteration-checkpoint.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_lss, "_checkpoint_path", lambda: path)
    return path


def _seed(path, goal_id):
    path.write_text(json.dumps({
        "goal_id": goal_id, "aspiration_id": "asp-023",
        "source": "world", "phase": "selected",
        "selected_at": "2026-09-02T18:06:00",
    }), encoding="utf-8")


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


# ── behaviour ────────────────────────────────────────────────────────────────

def test_wrong_goal_refresh_is_refused_in_the_production_arg_shape(ckpt):
    """The coach incident, replayed with its real ids and the real arg shape."""
    _seed(ckpt, "g-023-01")
    rc = _lss.cmd_update(_Args(if_goal="g-025-01"))

    assert rc == 0, "refusal must not abort the close (nonzero rc = iteration failure)"
    after = _read(ckpt)
    assert "phase_completed" not in after, (
        "the stale checkpoint for g-023-01 was stamped by a refresh belonging to "
        "g-025-01 — this is the g-357-84 defect")
    assert after["goal_id"] == "g-023-01", "the anchored goal must be left alone"
    assert after["phase"] == "selected", "no field of the other goal's record may move"


def test_matching_goal_refresh_is_applied(ckpt):
    """The guard must not break the case that is supposed to work."""
    _seed(ckpt, "g-025-01")
    rc = _lss.cmd_update(_Args(if_goal="g-025-01"))

    assert rc == 0
    after = _read(ckpt)
    assert after["phase_completed"] == "verify"
    assert after["last_updated"] == "2026-09-02T23:43:00"


@pytest.mark.parametrize("if_goal", ["", None], ids=["empty", "absent"])
def test_no_if_goal_stays_inert(ckpt, if_goal):
    """Back-compat: _checkpoint_refresh forwards an EMPTY --if-goal when the
    caller has no GOAL_ID, and every pre-existing caller passes none at all.
    Both must behave exactly as before the guard existed."""
    _seed(ckpt, "g-023-01")
    rc = _lss.cmd_update(_Args(if_goal=if_goal))

    assert rc == 0
    assert _read(ckpt)["phase_completed"] == "verify", (
        "an absent/empty --if-goal must mean 'no compare', not 'refuse everything'")


def test_refusal_is_recorded_in_the_durable_ledger(ckpt):
    """stderr is invisible from a backgrounded subprocess (guard-772), so the
    JSONL half is what makes the refusal auditable after the fact."""
    _seed(ckpt, "g-023-01")
    _lss.cmd_update(_Args(if_goal="g-025-01"))

    ledger = ckpt.parent / "checkpoint-miss.jsonl"
    assert ledger.exists(), "a silent refusal is not observable"
    rows = [json.loads(ln) for ln in ledger.read_text(encoding="utf-8").splitlines() if ln.strip()]
    hits = [r for r in rows if r.get("event") == "update_against_wrong_goal"]
    assert len(hits) == 1, f"expected exactly one refusal row, got {len(hits)}"
    assert hits[0]["anchored_goal_id"] == "g-023-01"
    assert hits[0]["requested_goal_id"] == "g-025-01"


def test_a_missing_checkpoint_still_fails_open(ckpt):
    """The guard sits AFTER the exists() branch; passing --if-goal must not turn
    a legitimate outside-an-iteration no-op into a refusal path."""
    assert not ckpt.exists()
    assert _lss.cmd_update(_Args(if_goal="g-025-01")) == 0


# ── source pins over the shell call sites (guard-2921: COUNT, not per-item) ──

def _refresh_call_lines():
    text = ITERATION_CLOSE.read_text(encoding="utf-8")
    return [ln.strip() for ln in text.splitlines()
            if re.match(r"^\s*_checkpoint_refresh\s+\S", ln)]


def test_all_three_call_sites_forward_the_goal_id():
    """A per-item loop alone passes forever on N-1 sites while reading as though
    it covers all of them (guard-2921, measured on this same file). The count
    assertion is what makes this pin fail loud."""
    calls = _refresh_call_lines()
    assert len(calls) == len(REFRESH_PHASES), (
        f"expected {len(REFRESH_PHASES)} _checkpoint_refresh call sites, found "
        f"{len(calls)}: {calls}")
    for ln in calls:
        assert '"$GOAL_ID"' in ln, f"call site does not forward the goal id: {ln}"
    for phase in REFRESH_PHASES:
        assert any(f"_checkpoint_refresh {phase} " in ln for ln in calls), (
            f"no call site for phase {phase}")


def test_definition_forwards_if_goal_to_the_writer():
    """The wiring the in-process tests above cannot reach."""
    text = ITERATION_CLOSE.read_text(encoding="utf-8")
    body = text.split("_checkpoint_refresh() {", 1)[1].split("\n}", 1)[0]
    assert 'local goal_id="${2-}"' in body, "the goal id argument is not captured"
    assert '--if-goal "$goal_id"' in body, "the writer is not given the goal id"
    assert "|| true" in body, (
        "|| true must stay — the writer's refusal path exits 0 by contract, and "
        "a nonzero rc from update means ITERATION failure")


# ── : the pin above counted the WRONG THING ─────────────────────────
# test_all_three_call_sites_forward_the_goal_id has a COUNT assertion, but its
# predicate is `^\s*_checkpoint_refresh\s+\S` -- it counts REFRESH calls, not
# `loop-state-save.sh update` invocations.  guarded ONE of SIX update
# sites and this pin reported full coverage of all three of the sites it knew
# about, forever. That is guard-1802's shape (a pin narrower than the population
# it guards) turning on the fix itself. The pins below count the WRITER's
# invocations instead, which is the population that can actually stamp a
# checkpoint.


def _raw_update_invocations():
    """Every raw `loop-state-save.sh" update` invocation in iteration-close.sh,
    each flattened across its line continuations so a trailing --if-goal on a
    continued line is visible to a substring test."""
    lines = ITERATION_CLOSE.read_text(encoding="utf-8").splitlines()
    out = []
    for i, ln in enumerate(lines):
        if 'loop-state-save.sh" update' not in ln:
            continue
        buf, j = [ln], i
        while lines[j].rstrip().endswith("\\") and j + 1 < len(lines):
            j += 1
            buf.append(lines[j])
        out.append(" ".join(x.strip() for x in buf))
    return out


def _function_body(name):
    text = ITERATION_CLOSE.read_text(encoding="utf-8")
    return text.split(f"\n{name}() {{", 1)[1].split("\n}", 1)[0]


def test_every_raw_update_invocation_forwards_an_identity():
    """THE widened pin. A new `loop-state-save.sh update` added to this file
    without --if-goal must fail HERE -- that is the whole protection, because
    the guard in cmd_update is inert unless the caller passes the flag."""
    invocations = _raw_update_invocations()
    assert len(invocations) == 2, (
        "expected exactly 2 raw `loop-state-save.sh update` invocations in "
        "iteration-close.sh (_checkpoint_refresh and _checkpoint_update); found "
        f"{len(invocations)}. A new raw call site must route through "
        "_checkpoint_update instead, so it cannot omit the goal identity:\n"
        + "\n".join(invocations))
    for inv in invocations:
        assert "--if-goal" in inv, (
            f"raw update invocation does not forward a goal identity: {inv}")


def test_checkpoint_update_takes_its_identity_as_a_mandatory_positional():
    """The identity must be positional, not an optional flag: an optional
    identity reproduces the defect the moment a caller forgets it."""
    body = _function_body("_checkpoint_update")
    assert 'local goal_id="${1-}"' in body, "the identity is not captured from $1"
    assert "shift" in body, "the remaining args are not forwarded"
    assert '--if-goal "$goal_id"' in body, "the writer is not given the identity"
    assert "|| true" in body, (
        "|| true must stay -- the writer's refusal path exits 0 by contract, and "
        "a nonzero rc from update means ITERATION failure")


def test_all_checkpoint_update_call_sites_pass_a_shell_expanded_identity():
    """Every caller passes a variable, never a literal or an empty string."""
    lines = [ln.strip() for ln in ITERATION_CLOSE.read_text(encoding="utf-8").splitlines()
             if ln.strip().startswith("_checkpoint_update ")]
    assert len(lines) == 5, (
        f"expected 5 _checkpoint_update call sites, found {len(lines)}: {lines}")
    for ln in lines:
        arg = ln.split("_checkpoint_update ", 1)[1].split()[0]
        assert arg.startswith('"$') and arg.endswith('"'), (
            f"call site does not pass a shell-expanded identity: {ln}")


def test_do_recover_scopes_to_the_checkpoint_gid_not_the_iteration_goal():
    """guard-2373 mutation pin for a DELIBERATE split -- do not 'harmonize' it.

    do_recover derives its goal entirely from the checkpoint ($_gid) and never
    reads $GOAL_ID, which per g-306-161 "is empty or belongs to some other
    iteration" when --phase recover runs. So the tempting single-identity fix
    (one env var every site inherits) would REFUSE do_recover's own legitimate
    writes and break recovery. This test goes red if someone rewrites either
    do_recover site to "$GOAL_ID".
    """
    body = _function_body("do_recover")
    calls = [ln.strip() for ln in body.splitlines()
             if ln.strip().startswith("_checkpoint_update ")]
    assert len(calls) == 2, f"expected 2 checkpoint writes in do_recover, got {calls}"
    for ln in calls:
        assert '_checkpoint_update "$_gid"' in ln, (
            "do_recover must scope its checkpoint write to the goal it read OUT "
            f"of the checkpoint, not to the iteration's $GOAL_ID: {ln}")
    assert '_checkpoint_update "$GOAL_ID"' not in body, (
        "do_recover must never scope a checkpoint write to $GOAL_ID")


def test_do_verify_intent_complete_is_scoped():
    """The site that motivated : intent_state=complete is what
    do_recover keys on to commit or ROLL BACK, so an unscoped write here lets a
    stale checkpoint's goal be rolled back on a later --phase recover."""
    body = _function_body("do_verify")
    assert '_checkpoint_update "$GOAL_ID" \\' in body or \
           '_checkpoint_update "$GOAL_ID"' in body, "do_verify writes are unscoped"
    assert 'loop-state-save.sh" update' not in body, (
        "do_verify must not call the writer raw -- route through _checkpoint_update")
    assert "intent_state=complete" in body, "the intent marker write vanished"
