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
    assert set(res) == {"status", "checked", "ambiguous", "note"}
