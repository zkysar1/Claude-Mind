"""postcompact-restore must not tell the model to resume a TERMINAL goal ().

The incident: the SessionStart:compact hook emitted an IN-FLIGHT GOAL block
naming a goal whose live status was `skipped`, wrapped in wording that forbade
the two actions that would have caught it ("Do NOT re-run goal-selector.sh...
Do NOT substitute a different goal based on narrative context"). Obeying it
would have abandoned a finished-but-uncommitted deep goal and "resumed" a
terminal one.

BOTH AXES (guard-2319): a suite that only pins the refusal is passed perfectly
by an implementation that refuses everything — which would be a worse bug, since
it would suppress every legitimate resume anchor. So the live-goal case is
pinned just as hard as the terminal case.

The SURFACE-vs-SWALLOW axis is pinned too, because the fix's own risk is that a
read-side check quietly drops the block and leaves the writer free to keep
producing stale anchors with nothing left to notice them. Every branch must
still print the goal_id and the full block.

Hermetic: `_paths.AGENT_DIR` / `_paths.WORLD_DIR` are patched to tmp dirs BEFORE
the module under test is imported, so its `from _paths import ...` binds the tmp
values and no live queue is ever read. Env mutation happens inside fixtures, not
at module level (guard-1165).
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

import _paths  # noqa: E402

TARGET = CORE_SCRIPTS / "postcompact-restore.py"


def _load_module(monkeypatch, tmp_path):
    """Import postcompact-restore.py bound to a tmp agent + world tree."""
    agent_dir = tmp_path / "agents" / "anchoragent"
    world_dir = tmp_path / "world"
    (agent_dir / "session").mkdir(parents=True, exist_ok=True)
    world_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("MIND_AGENT", "anchoragent")
    monkeypatch.delenv("MIND_SID", raising=False)
    monkeypatch.setattr(_paths, "AGENT_DIR", agent_dir)
    monkeypatch.setattr(_paths, "WORLD_DIR", world_dir)

    spec = importlib.util.spec_from_file_location("pcr_under_test", TARGET)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, agent_dir, world_dir


def _write_queue(path, goal_id, status, asp_id="asp-001"):
    rec = {"id": asp_id, "title": "t", "status": "active",
           "goals": [{"id": goal_id, "title": "g", "status": status}]}
    path.write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _ckpt(goal_id="g-001-01", source="world", phase="selected"):
    return {"goal_id": goal_id, "aspiration_id": "asp-001", "source": source,
            "phase": phase, "selected_at": "2026-08-05T06:07:07"}


RESUME_IMPERATIVE = "Do NOT re-run goal-selector.sh"


# --- axis 1: terminal anchor is refused -------------------------------------

@pytest.mark.parametrize("status", ["completed", "skipped", "expired"])
def test_terminal_anchor_refuses_resume(monkeypatch, tmp_path, status):
    mod, _agent, world = _load_module(monkeypatch, tmp_path)
    _write_queue(world / "aspirations.jsonl", "g-001-01", status)

    out = "\n".join(mod._format_iteration_ckpt_block(_ckpt()))

    assert "STALE ANCHOR" in out
    assert f"'{status}'" in out
    # The imperative that made the incident unrecoverable must be GONE.
    assert RESUME_IMPERATIVE not in out


# --- axis 2: a live anchor still resumes (the refuse-everything guard) ------

@pytest.mark.parametrize("status", ["pending", "in-progress", "blocked"])
def test_live_anchor_still_emits_resume_imperative(monkeypatch, tmp_path, status):
    mod, _agent, world = _load_module(monkeypatch, tmp_path)
    _write_queue(world / "aspirations.jsonl", "g-001-01", status)

    out = "\n".join(mod._format_iteration_ckpt_block(_ckpt()))

    assert RESUME_IMPERATIVE in out
    assert "STALE ANCHOR" not in out
    # The check ran, so it must NOT claim to be unverified.
    assert "did NOT run" not in out


# --- axis 2b: a DEFERRED anchor is refused too () -----------------
#
# The terminal branch above covers completed/skipped/expired. A DEFER leaves
# status `pending` with a defer_reason, so it sails through that check and gets
# the full "Resume execution on THIS goal. Do NOT re-run goal-selector.sh"
# imperative — on a goal whose remaining half was deliberately routed away.
# Observed live 2026-08-04 (alpha, cc-04,  deferred with
# defer_reason precondition_unmet:studio_session_required).


def _write_queue_deferred(path, goal_id, defer_reason, asp_id="asp-001"):
    rec = {"id": asp_id, "title": "t", "status": "active",
           "goals": [{"id": goal_id, "title": "g", "status": "pending",
                      "defer_reason": defer_reason,
                      "defer_reason_set_at": "2026-08-04T22:01:08"}]}
    path.write_text(json.dumps(rec) + "\n", encoding="utf-8")


def test_deferred_anchor_refuses_resume(monkeypatch, tmp_path):
    """The originating incident, reproduced at the read side."""
    mod, _agent, world = _load_module(monkeypatch, tmp_path)
    _write_queue_deferred(world / "aspirations.jsonl", "g-001-01",
                          "precondition_unmet:studio_session_required")

    out = "\n".join(mod._format_iteration_ckpt_block(_ckpt()))

    assert "STALE ANCHOR" in out
    assert "DEFERRED" in out
    assert "precondition_unmet:studio_session_required" in out, (
        "name the defer_reason — a reader deciding whether to override needs "
        "to see WHY it was parked, not just that it was")
    assert RESUME_IMPERATIVE not in out


def test_deferred_anchor_is_not_told_never_to_execute_the_goal(monkeypatch, tmp_path):
    """Deliberately weaker than the terminal wording. A terminal goal is closed
    and must not be touched; a deferred goal is LIVE work that is simply not in
    flight, and may be selected again on its merits once the defer clears.
    Telling the reader never to execute it would be its own wrong instruction."""
    mod, _agent, world = _load_module(monkeypatch, tmp_path)
    _write_queue_deferred(world / "aspirations.jsonl", "g-001-01", "blocked_on_x")

    out = "\n".join(mod._format_iteration_ckpt_block(_ckpt()))

    assert "do NOT write an outcome_note" not in out
    assert "normal selection and fine" in out


def test_a_pending_goal_without_a_defer_reason_still_resumes(monkeypatch, tmp_path):
    """The false-positive guard for axis 2b, and the reason the sibling
    'claimed_by is empty -> released' predicate was written and then REMOVED:
    it fired here. A worker hands its goal to the reducer at in-progress with
    the claim released (worker-loop Phase 4), and stranded-claim-sweep strips
    claim fields by design — so an absent claim field is not evidence of an
    event, while a defer_reason is."""
    mod, _agent, world = _load_module(monkeypatch, tmp_path)
    for status in ("pending", "in-progress", "blocked"):
        _write_queue(world / "aspirations.jsonl", "g-001-01", status)
        out = "\n".join(mod._format_iteration_ckpt_block(_ckpt()))
        assert RESUME_IMPERATIVE in out, status
        assert "STALE ANCHOR" not in out, status


def test_deferred_but_ambiguous_id_does_not_get_a_confident_verdict(monkeypatch, tmp_path):
    """When the id lives in both queues the defer_reason read may belong to the
    OTHER copy. A confident 'this is deferred' about the wrong goal is worse
    than the ambiguity note the reader already gets."""
    mod, agent, world = _load_module(monkeypatch, tmp_path)
    _write_queue_deferred(world / "aspirations.jsonl", "g-001-01", "blocked_on_x")
    _write_queue(agent / "aspirations.jsonl", "g-001-01", "pending")

    out = "\n".join(mod._format_iteration_ckpt_block(_ckpt()))

    assert "STALE ANCHOR" not in out
    assert "BOTH queues" in out


# --- axis 3: surface, never swallow ----------------------------------------

def test_terminal_branch_still_prints_the_full_block(monkeypatch, tmp_path):
    """A read-side check that hid the block would leave the writer unobserved."""
    mod, _agent, world = _load_module(monkeypatch, tmp_path)
    _write_queue(world / "aspirations.jsonl", "g-001-01", "skipped")

    out = "\n".join(mod._format_iteration_ckpt_block(_ckpt()))

    assert "IN-FLIGHT GOAL" in out
    assert "g-001-01" in out
    assert "asp-001" in out
    assert "2026-08-05T06:07:07" in out


# --- axis 4: an unreadable queue admits it did not check --------------------

def test_unreadable_queue_admits_the_check_did_not_run(monkeypatch, tmp_path):
    """guard-1760: never report what you declined to look at as coverage."""
    mod, _agent, world = _load_module(monkeypatch, tmp_path)
    # No aspirations.jsonl written at all -> primary read returns None.
    out = "\n".join(mod._format_iteration_ckpt_block(_ckpt()))

    assert RESUME_IMPERATIVE in out      # fail-open: still resumable
    assert "did NOT run" in out          # but honest that it is UNVERIFIED
    assert "UNVERIFIED" in out


# --- axis 5: cross-queue id ambiguity is surfaced ---------------------------

def test_ambiguous_id_across_queues_is_surfaced(monkeypatch, tmp_path):
    """Measured 2026-08-05:  names DIFFERENT goals in world vs agent."""
    mod, agent, world = _load_module(monkeypatch, tmp_path)
    _write_queue(world / "aspirations.jsonl", "g-001-01", "skipped")
    _write_queue(agent / "aspirations.jsonl", "g-001-01", "pending")

    out = "\n".join(mod._format_iteration_ckpt_block(_ckpt()))

    assert "AMBIGUOUS ID" in out
    assert "world=skipped" in out
    assert "agent=pending" in out


def test_ambiguity_is_reported_on_the_live_branch_too(monkeypatch, tmp_path):
    """Ambiguity makes a RESUME as unsafe as a refusal — both branches warn."""
    mod, agent, world = _load_module(monkeypatch, tmp_path)
    _write_queue(world / "aspirations.jsonl", "g-001-01", "pending")
    _write_queue(agent / "aspirations.jsonl", "g-001-01", "completed")

    out = "\n".join(mod._format_iteration_ckpt_block(_ckpt()))

    assert RESUME_IMPERATIVE in out
    assert "AMBIGUOUS ID" in out


# --- axis 6: the mirrored constant stays equal to its SSOT ------------------

def test_terminal_statuses_match_coordination_merge_ssot(monkeypatch, tmp_path):
    """The tuple is mirrored, not imported (a hook must not die on import).

    That trade is only safe while the two stay equal, so pin them here — this
    test IS the sync mechanism the mirroring comment promises.
    """
    mod, _agent, _world = _load_module(monkeypatch, tmp_path)
    import coordination_merge

    assert set(mod._TERMINAL_STATUSES) == set(
        coordination_merge._TERMINAL_STATUSES)


# --- axis 7: the probe never raises ----------------------------------------

@pytest.mark.parametrize("bad", [
    {"goal_id": "", "source": "world"},
    {"goal_id": "?", "source": "world"},
    {"goal_id": "g-001-01", "source": None},
    {"goal_id": "g-001-01", "source": "agent"},
])
def test_status_probe_never_raises(monkeypatch, tmp_path, bad):
    """A hook that throws takes out the whole context restore."""
    mod, _agent, world = _load_module(monkeypatch, tmp_path)
    (world / "aspirations.jsonl").write_text("not json\n{", encoding="utf-8")

    res = mod._goal_live_status(bad["goal_id"], bad["source"])
    assert isinstance(res, dict)
    # `defer_reason` joined the contract in : a DEFERRED goal keeps
    # status `pending`, so a status-only probe reports it resumable and the
    # caller emits the full resume imperative on a goal that was deliberately
    # parked (observed cc-04, ). The exact-set assertion is kept
    # rather than loosened to a subset — the point of this pin is that every
    # caller can rely on the shape, and a silently-growing dict is how a
    # consumer ends up reading a key that only sometimes exists.
    assert set(res) == {"status", "checked", "ambiguous", "note", "defer_reason"}


# --- axis 8: a terminal goal whose close is still OWED is not a stale anchor () ---
#
# iteration-close --phase verify marks the goal completed BEFORE state-update,
# learning-gate and the productivity-check run, and the checkpoint's `phase` is
# written at selection and never advanced. A compaction in that window printed
# STALE ANCHOR, whose "re-select fresh work" abandons the owed close with no error
# anywhere (guard-7366, guard-4245). The evidence is the diary: the goal's last
# verify phase_end, then which close phases ended after it. Both directions are
# pinned (guard-2319): an owed close must not be called stale, and a FINISHED close
# must still be, or the new branch would swallow the closed-goal protection.

# (diary phase name, carries the goal id), in iteration-close's run order. The
# productivity-check runs without --goal, so its rows carry none.
CLOSE_PHASES = (("phase-5-verify", True), ("phase-8-state-update", True),
                ("phase-12-learning-gate", True), ("phase-12-productivity", False))
OWED = "CLOSE OWED"


def _close_rows(goal_id, through="phase-12-productivity"):
    """The rows iteration-close writes for one goal, in run order, through `through`."""
    rows = []
    for phase, keyed in CLOSE_PHASES:
        for kind in ("phase_start", "phase_end"):
            row = {"entry_type": kind, "phase": phase, "content": f"{kind} {phase}",
                   "timestamp": "2026-10-03T05:00:00"}
            if keyed:
                row["goal_id"] = goal_id
            rows.append(row)
        if phase == through:
            break
    return rows


def _diary(mod, rows):
    mod.DIARY_PATH.write_text("".join(json.dumps(r) + "\n" for r in rows),
                              encoding="utf-8")


def _block(mod, world, status="completed", rows=None, goal="g-001-01"):
    _write_queue(world / "aspirations.jsonl", goal, status)
    if rows is not None:
        _diary(mod, rows)
    return "\n".join(mod._format_iteration_ckpt_block(_ckpt(goal)))


@pytest.mark.parametrize("through,owed", [
    ("phase-5-verify", "state-update, learning-gate, productivity-check"),
    ("phase-8-state-update", "learning-gate, productivity-check"),
    ("phase-12-learning-gate", "productivity-check"),
])
def test_a_terminal_goal_with_its_close_owed_is_not_called_stale(
        monkeypatch, tmp_path, through, owed):
    mod, _agent, world = _load_module(monkeypatch, tmp_path)

    out = _block(mod, world, rows=_close_rows("g-001-01", through))

    assert OWED in out
    assert f"no phase_end after that verify for {owed}." in out
    assert "STALE ANCHOR" not in out
    assert RESUME_IMPERATIVE not in out
    # The closed-goal protection is kept: never execute it again, no hand-written
    # outcome_note (the close phases are scripts and write the fields they own).
    assert "do NOT hand-write an outcome_note" in out
    assert "orchestrator-entry-battery.sh" in out
    # A close phase is its own, often backgrounded, process and the diary cannot say
    # whether one is still running, so the process check comes BEFORE the battery.
    assert "proc-match.sh iteration-close" in out and "proc-match.sh recurring-close" in out
    assert out.index("proc-match.sh iteration-close") < out.index("orchestrator-entry-battery.sh")
    # Surface, never swallow: the full block is still printed.
    assert "IN-FLIGHT GOAL" in out and "g-001-01" in out


@pytest.mark.parametrize("status", ["completed", "skipped", "expired"])
def test_every_terminal_status_gets_the_owed_branch(monkeypatch, tmp_path, status):
    mod, _agent, world = _load_module(monkeypatch, tmp_path)

    out = _block(mod, world, status, rows=_close_rows("g-001-01", "phase-5-verify"))

    assert OWED in out and f"'{status}'" in out
    assert "STALE ANCHOR" not in out


def test_a_terminal_goal_whose_close_finished_is_still_a_stale_anchor(monkeypatch, tmp_path):
    """The refuse-everything twin: the owed branch must not swallow the stale one."""
    mod, _agent, world = _load_module(monkeypatch, tmp_path)

    out = _block(mod, world, rows=_close_rows("g-001-01"))

    assert "STALE ANCHOR" in out
    assert OWED not in out
    assert RESUME_IMPERATIVE not in out


def test_a_productivity_row_from_before_the_verify_does_not_finish_the_close(
        monkeypatch, tmp_path):
    """The productivity-check row carries no goal id, so only ORDER ties it to a close."""
    mod, _agent, world = _load_module(monkeypatch, tmp_path)
    rows = _close_rows("g-002-02") + _close_rows("g-001-01", "phase-5-verify")

    out = _block(mod, world, rows=rows)

    assert OWED in out
    assert ("no phase_end after that verify for "
            "state-update, learning-gate, productivity-check.") in out


def test_the_last_verify_decides(monkeypatch, tmp_path):
    """A re-run verify opens a new close; the earlier finished one must not mask it."""
    mod, _agent, world = _load_module(monkeypatch, tmp_path)
    rows = _close_rows("g-001-01") + _close_rows("g-001-01", "phase-5-verify")

    out = _block(mod, world, rows=rows)

    assert OWED in out
    assert "STALE ANCHOR" not in out


def test_another_goals_close_phases_do_not_satisfy_the_anchored_goal(monkeypatch, tmp_path):
    mod, _agent, world = _load_module(monkeypatch, tmp_path)
    # 's state-update and learning-gate rows only, after 's verify.
    other = _close_rows("g-002-02", "phase-12-learning-gate")[2:]
    rows = _close_rows("g-001-01", "phase-5-verify") + other

    out = _block(mod, world, rows=rows)

    assert ("no phase_end after that verify for "
            "state-update, learning-gate, productivity-check.") in out


def test_no_verify_row_for_the_goal_is_still_a_stale_anchor(monkeypatch, tmp_path):
    mod, _agent, world = _load_module(monkeypatch, tmp_path)

    out = _block(mod, world, rows=_close_rows("g-002-02", "phase-5-verify"))

    assert "STALE ANCHOR" in out
    assert OWED not in out


def test_an_unreadable_diary_degrades_to_the_stale_anchor(monkeypatch, tmp_path):
    """A hook that throws takes out the whole context restore."""
    mod, _agent, world = _load_module(monkeypatch, tmp_path)
    mod.DIARY_PATH.mkdir()  # present, but not readable as a file

    out = _block(mod, world)

    assert "STALE ANCHOR" in out
    assert OWED not in out


def test_junk_diary_lines_do_not_hide_an_owed_close(monkeypatch, tmp_path):
    """Non-JSON and non-object lines are skipped, not fatal and not erasing."""
    mod, _agent, world = _load_module(monkeypatch, tmp_path)
    good = [json.dumps(r) for r in _close_rows("g-001-01", "phase-5-verify")]
    junk_first = ["not json", "[1, 2]", "7", "null"]
    mod.DIARY_PATH.write_text("\n".join(junk_first + good + ["{"]) + "\n",
                              encoding="utf-8")

    out = _block(mod, world)

    assert OWED in out


def test_a_live_anchor_is_not_touched_by_the_diary(monkeypatch, tmp_path):
    """The owed branch is terminal-only: a live goal with a verify row still resumes."""
    mod, _agent, world = _load_module(monkeypatch, tmp_path)

    out = _block(mod, world, "in-progress", rows=_close_rows("g-001-01", "phase-5-verify"))

    assert RESUME_IMPERATIVE in out
    assert OWED not in out and "STALE ANCHOR" not in out


@pytest.mark.parametrize("goal", ["", "?", None, "g-404-04"])
def test_close_tail_probe_finds_nothing_for_an_unknown_goal(monkeypatch, tmp_path, goal):
    mod, _agent, _world = _load_module(monkeypatch, tmp_path)
    _diary(mod, _close_rows("g-001-01", "phase-5-verify"))

    assert mod._close_tail_owed(goal) == []


def test_close_phase_names_match_iteration_close(monkeypatch, tmp_path):
    """The names are mirrored from iteration-close.sh's PHASE_NAME map, not imported
    (a hook must not die on an import), so this test IS the sync mechanism."""
    import re
    mod, _agent, _world = _load_module(monkeypatch, tmp_path)
    text = (CORE_SCRIPTS / "iteration-close.sh").read_text(encoding="utf-8")
    names = dict(re.findall(r'^\s+([a-z-]+)\)\s+PHASE_NAME="([a-z0-9-]+)"', text, re.M))

    assert mod._VERIFY_PHASE == names["verify"]
    assert dict(mod._CLOSE_TAIL) == {
        step: names[step] for step in ("state-update", "learning-gate", "productivity-check")}


def test_the_verify_row_is_found_beyond_the_last_ten_diary_entries(monkeypatch, tmp_path):
    """main() prints the last ten diary rows, but a close's verify row sits well
    behind them once the goal's own execution has written its breadcrumbs."""
    mod, _agent, world = _load_module(monkeypatch, tmp_path)
    notes = [{"entry_type": "note", "goal_id": "g-001-01", "content": f"step {i}",
              "timestamp": "2026-10-03T05:00:00"} for i in range(40)]

    out = _block(mod, world, rows=_close_rows("g-001-01", "phase-5-verify") + notes)

    assert OWED in out


def test_a_phase_that_started_but_not_ended_is_still_owed(monkeypatch, tmp_path):
    """A compaction can land while a close phase is RUNNING: its phase_start is
    on disk and its phase_end is not. Only the end counts as the phase having run."""
    mod, _agent, world = _load_module(monkeypatch, tmp_path)
    rows = _close_rows("g-001-01", "phase-5-verify") + [
        {"entry_type": "phase_start", "phase": "phase-8-state-update",
         "goal_id": "g-001-01", "timestamp": "2026-10-03T05:00:00"}]

    out = _block(mod, world, rows=rows)

    assert ("no phase_end after that verify for "
            "state-update, learning-gate, productivity-check.") in out
    # ...and the banner does not send the reader to run it before asking whether it is.
    assert "proc-match.sh iteration-close" in out


def test_a_damaged_diary_byte_does_not_blank_the_probe(monkeypatch, tmp_path):
    """One undecodable byte costs its own line, not every row. Strict decoding failed the
    whole read, and the probe then fell back to the banner this branch replaces."""
    mod, _agent, world = _load_module(monkeypatch, tmp_path)
    lines = [json.dumps(r).encode("utf-8") for r in _close_rows("g-001-01", "phase-5-verify")]
    damaged = lines[:1] + [b'{"entry_type":"note","content":"cut \xe2\x80'] + lines[1:]
    mod.DIARY_PATH.write_bytes(b"\n".join(damaged) + b"\n")

    out = _block(mod, world)

    assert OWED in out


def test_close_tail_probe_never_raises(monkeypatch, tmp_path):
    """A hook that throws takes out the whole context restore."""
    mod, _agent, _world = _load_module(monkeypatch, tmp_path)

    def boom(limit=10):
        raise RuntimeError("diary read exploded")

    monkeypatch.setattr(mod, "_read_diary_entries", boom)

    assert mod._close_tail_owed("g-001-01") == []
