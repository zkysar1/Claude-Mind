"""test_per_goal_experience_check.py —  regression test.

Pins the PER-GOAL Phase 4.25 experience check after its extraction out of
recurring-close.sh into the shared `core/scripts/per-goal-experience-check.py`,
and pins the new wiring of that helper into the NON-recurring close path
(iteration-close.sh do_state_update).

THE DEFECT
----------
Both close paths leave the experience WRITE to the LLM (experience-add.sh) —
the split g-115-4661's extraction created and g-115-5314 names. What differed
was ENFORCEMENT GRANULARITY:

  * recurring-close.sh ran a PER-GOAL check keyed on the specific goal_id and
    set force_experience_archival on a miss, which aspirations-precheck Phase
    0-pre2 then consumes to force a retro-compose.
  * iteration-close.sh (non-recurring) ran only experience-staleness-check.sh,
    which is STORE-level: newest-entry-of-any-kind vs a 12h threshold, with no
    goal_id join at all.

So the per-goal remedy existed and was wired to the path that needed it least.
Measured fleet-wide (echo, cc-03, 2026-08-02, joined against experience.jsonl +
experience-archive.jsonl + experience/*.md across 5 agents): non-recurring
completed goals with ANY experience record ran 16-32%; recurring goals — the one
lane where the check was wired — ran 95%.

WHAT THESE TESTS PIN
--------------------
1. Helper behavior: the 30-min window, the goal_id/source_goal DUAL match, the
   exact 4-key payload Phase 0-pre2 consumes, and always-exit-0 fail-open.
2. guard-2015: recurring-close.sh keeps NO fork of the extracted logic.
3. The non-recurring wiring exists in do_state_update, is gated on
   deep + not-recurring, and carries VISIBLE degradation (not a bare `|| true`).
4. g-115-5314 PER-PATH coverage: the NON-recurring trigger (startswith the
   shared NONRECURRING_PRODUCER from spark-fire-dedup.py) matches on
   goal_id/source_goal ALONE — a record older than the 30-min window still
   suppresses the sentinel, because a non-recurring goal closes exactly once.
   The RECURRING trigger keeps the bounded window — a record from a prior
   close still sets it (the window's load-bearing case). Fail-closed on the
   unbounded path too: a genuinely absent (or cross-goal) record still sets
   the sentinel. The discriminator is the sibling's constant object, not a
   duplicated literal.

Cross-refs:
  - g-115-4661 (this fix), g-115-4660 (zeta's measurement), g-115-547 (origin canary)
  - g-115-5314 (per-path coverage: unbounded non-recurring, bounded recurring)
  - g-115-3351 / spark-fire-dedup.py (the shared NONRECURRING_PRODUCER constant)
  - g-115-2511 / guard-697 / guard-713 (the goal_id vs source_goal seam)
  - guard-2015 (extract-and-delete-the-origin)
  - msg-20260801-171952-zeta-5643 (insight trigger: no bare `|| true` on this file)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
HELPER = CORE_SCRIPTS / "per-goal-experience-check.py"
RECURRING_CLOSE_SH = CORE_SCRIPTS / "recurring-close.sh"
ITERATION_CLOSE_SH = CORE_SCRIPTS / "iteration-close.sh"

TRIGGER = "test-trigger"


# ───────────────────────────── fixtures ─────────────────────────────

def _sandbox_agent(entries):
    """Build a temp AGENT_DIR with an experience.jsonl holding `entries`.

    Returns (tmpdir, agent_dir). Caller removes tmpdir.
    `entries is None` means: do not create experience.jsonl at all.
    """
    tmp = Path(tempfile.mkdtemp(prefix="pgec-"))
    agent_dir = tmp / "agents" / "testagent"
    (agent_dir / "session").mkdir(parents=True)
    if entries is not None:
        (agent_dir / "experience.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8"
        )
    return tmp, agent_dir


def _run(agent_dir: Path, goal_id: str, *extra):
    env = os.environ.copy()
    # : BODY_WM_PATH is the FIRST branch of wm.wm_path(); MIND_AGENT_DIR
    # below is only the SECOND. Every worker Body exports BODY_WM_PATH (the
    # bash-agent-inject hook), so a bare os.environ.copy() sends this helper's WM
    # reads AND WRITES to the LIVE per-Body working-memory.yaml instead of the
    # sandbox -- the tests then fail on state that never round-trips, and the run
    # mutates the Body's merge payload on its way past. Dropping the namespace
    # lets the MIND_AGENT_DIR branch resolve. Same fix as the "BODY_" entry in
    # the sibling files' _FRAMEWORK_ENV_PREFIXES.
    for _k in [_k for _k in env if _k.startswith("BODY_")]:
        del env[_k]
    env["MIND_AGENT_DIR"] = str(agent_dir)
    env["STORAGE_BACKEND"] = "local"          # guard-955
    return subprocess.run(
        [sys.executable, str(HELPER), "--goal-id", goal_id, "--trigger", TRIGGER, *extra],
        capture_output=True, text=True, env=env, timeout=60,
    )


def _sentinel(agent_dir: Path):
    """force_experience_archival lives under data['slots'] (not TOP_LEVEL_KEYS)."""
    wm_path = agent_dir / "session" / "working-memory.yaml"
    if not wm_path.exists():
        return None
    wm = yaml.safe_load(wm_path.read_text(encoding="utf-8")) or {}
    return (wm.get("slots") or {}).get("force_experience_archival")


def _iso(minutes_ago: float) -> str:
    return (datetime.now() - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


# ───────────────────────── helper behavior ──────────────────────────

def test_recent_record_keyed_on_goal_id_suppresses_sentinel():
    tmp, agent_dir = _sandbox_agent([
        {"id": "exp-1", "goal_id": "g-1", "created": _iso(2)},
    ])
    try:
        r = _run(agent_dir, "g-1")
        assert r.returncode == 0, r.stderr
        assert _sentinel(agent_dir) is None, "record exists — sentinel must NOT fire"
        assert "no sentinel needed" in r.stderr
    finally:
        _rm(tmp)


def test_recent_record_keyed_on_legacy_source_goal_also_suppresses():
    """: entries carrying only source_goal must still count.

    Dropping this fallback makes the sentinel FALSE-fire on closes whose record
    exists — the exact defect guard-697 / guard-713 describe from the write side.
    """
    tmp, agent_dir = _sandbox_agent([
        {"id": "exp-1", "source_goal": "g-1", "goal_id": None, "created": _iso(2)},
    ])
    try:
        r = _run(agent_dir, "g-1")
        assert r.returncode == 0, r.stderr
        assert _sentinel(agent_dir) is None, "source_goal match must suppress the sentinel"
    finally:
        _rm(tmp)


def test_record_outside_window_does_not_suppress():
    tmp, agent_dir = _sandbox_agent([
        {"id": "exp-1", "goal_id": "g-1", "created": _iso(45)},   # > 30min
    ])
    try:
        r = _run(agent_dir, "g-1")
        assert r.returncode == 0, r.stderr
        assert _sentinel(agent_dir) is not None, "stale record must not count as coverage"
    finally:
        _rm(tmp)


def test_record_for_a_different_goal_does_not_suppress():
    """The whole point of per-goal granularity: a fresh store is not coverage."""
    tmp, agent_dir = _sandbox_agent([
        {"id": "exp-1", "goal_id": "g-OTHER", "created": _iso(1)},
    ])
    try:
        r = _run(agent_dir, "g-1")
        assert r.returncode == 0, r.stderr
        s = _sentinel(agent_dir)
        assert s is not None, "another goal's record must not cover this goal"
        assert s["goal_id"] == "g-1"
    finally:
        _rm(tmp)


def test_missing_store_fires_sentinel():
    tmp, agent_dir = _sandbox_agent(None)
    try:
        r = _run(agent_dir, "g-1")
        assert r.returncode == 0, r.stderr
        assert _sentinel(agent_dir) is not None, "no store at all == no record"
    finally:
        _rm(tmp)


def test_payload_shape_is_exactly_what_phase_0_pre2_consumes():
    tmp, agent_dir = _sandbox_agent([])
    try:
        r = _run(agent_dir, "g-1", "--original-outcome", "routine")
        assert r.returncode == 0, r.stderr
        s = _sentinel(agent_dir)
        assert s is not None
        assert set(s) == {"triggered_at", "trigger", "goal_id", "original_outcome"}, (
            f"payload shape drifted — Phase 0-pre2 consumes these 4 keys: {s}"
        )
        assert s["goal_id"] == "g-1"
        assert s["trigger"] == TRIGGER
        assert s["original_outcome"] == "routine"
        datetime.fromisoformat(s["triggered_at"])           # parseable
    finally:
        _rm(tmp)


def test_malformed_line_is_skipped_not_fatal():
    tmp, agent_dir = _sandbox_agent(None)
    try:
        (agent_dir / "experience.jsonl").write_text(
            "{not json\n" + json.dumps({"goal_id": "g-1", "created": _iso(1)}) + "\n",
            encoding="utf-8",
        )
        r = _run(agent_dir, "g-1")
        assert r.returncode == 0, r.stderr
        assert _sentinel(agent_dir) is None, "valid line after a malformed one must still match"
    finally:
        _rm(tmp)


def test_dry_run_reports_without_writing():
    tmp, agent_dir = _sandbox_agent([])
    try:
        r = _run(agent_dir, "g-1", "--dry-run")
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["goal_id"] == "g-1"
        assert _sentinel(agent_dir) is None, "--dry-run must not write the sentinel"
    finally:
        _rm(tmp)


def test_empty_goal_id_is_a_noop_not_a_crash():
    """Fail-open: a check failure must never block a close."""
    tmp, agent_dir = _sandbox_agent([])
    try:
        r = _run(agent_dir, "")
        assert r.returncode == 0, r.stderr
        assert _sentinel(agent_dir) is None
    finally:
        _rm(tmp)


# ─────────────── : per-path coverage bounds ───────────────────
# The trigger strings below are the VERBATIM values the two call sites pass
# (iteration-close.sh do_state_update / recurring-close.sh), so these tests
# exercise the discriminator the same way production does.

TRIGGER_NONRECURRING = "nonrecurring-state-update-deep-no-recent-entry"
TRIGGER_RECURRING = "recurring-close-postflip-deep-no-recent-entry"


def test_unbounded_nonrecurring_record_outside_window_suppresses_sentinel():
    """ outcome 3: a non-recurring goal whose record was created
    more than 30 minutes before the check returns 'no sentinel needed' —
    where the pre-fix code set the sentinel. The goal closed exactly once,
    so the joined record is necessarily this close's."""
    tmp, agent_dir = _sandbox_agent([
        {"id": "exp-1", "goal_id": "g-1", "created": _iso(45)},   # > 30min
    ])
    try:
        r = _run(agent_dir, "g-1", "--trigger", TRIGGER_NONRECURRING)
        assert r.returncode == 0, r.stderr
        assert _sentinel(agent_dir) is None, (
            "non-recurring close: a goal_id-joined record older than the "
            f"30-min window must still count — the window false-fires on long "
            f"closes. stderr={r.stderr}"
        )
        assert "no sentinel needed" in r.stderr
    finally:
        _rm(tmp)


def test_unbounded_nonrecurring_record_keyed_on_legacy_source_goal_suppresses():
    """The unbounded path keeps the goal_id/source_goal DUAL match ():
    dropping the fallback would re-introduce false fires on this path."""
    tmp, agent_dir = _sandbox_agent([
        {"id": "exp-1", "source_goal": "g-1", "goal_id": None, "created": _iso(120)},
    ])
    try:
        r = _run(agent_dir, "g-1", "--trigger", TRIGGER_NONRECURRING)
        assert r.returncode == 0, r.stderr
        assert _sentinel(agent_dir) is None, "source_goal match must count on the unbounded path"
    finally:
        _rm(tmp)


def test_recurring_record_outside_window_still_sets_sentinel():
    """ outcome 4: the RECURRING call site keeps the window — its
    load-bearing case. The same goal_id closes many times, so a record from a
    PRIOR close must not suppress the sentinel for THIS close."""
    tmp, agent_dir = _sandbox_agent([
        {"id": "exp-1", "goal_id": "g-1", "created": _iso(45)},   # prior close
    ])
    try:
        r = _run(agent_dir, "g-1", "--trigger", TRIGGER_RECURRING)
        assert r.returncode == 0, r.stderr
        assert _sentinel(agent_dir) is not None, (
            "recurring close: a record older than the window must NOT count as "
            f"coverage — it may belong to a prior close. stderr={r.stderr}"
        )
    finally:
        _rm(tmp)


def test_recurring_record_inside_window_still_suppresses():
    """The recurring window is unchanged in its load-bearing direction: a
    fresh record from THIS close still suppresses the sentinel."""
    tmp, agent_dir = _sandbox_agent([
        {"id": "exp-1", "goal_id": "g-1", "created": _iso(2)},
    ])
    try:
        r = _run(agent_dir, "g-1", "--trigger", TRIGGER_RECURRING)
        assert r.returncode == 0, r.stderr
        assert _sentinel(agent_dir) is None, "a fresh record on the recurring path must still count"
    finally:
        _rm(tmp)


def test_unknown_trigger_keeps_the_bounded_window():
    """The default trigger (anything that is not a known close-path trigger)
    behaves exactly as pre-g-115-5314: stale record, no coverage. This is the
    direction the change must not leak into: only the non-recurring producer
    gets the unbounded path."""
    tmp, agent_dir = _sandbox_agent([
        {"id": "exp-1", "goal_id": "g-1", "created": _iso(45)},
    ])
    try:
        r = _run(agent_dir, "g-1")   # TRIGGER = "test-trigger"
        assert r.returncode == 0, r.stderr
        assert _sentinel(agent_dir) is not None, (
            "an unknown trigger must NOT inherit the unbounded path — "
            "the window stays for every trigger that is not the non-recurring producer"
        )
    finally:
        _rm(tmp)


def test_fail_closed_on_unbounded_path_when_record_genuinely_absent():
    """ outcome 5: the unbounded path only removes false POSITIVES.
    A genuinely absent record on the non-recurring path must STILL set the
    sentinel — a missing experience record is a real lost artifact."""
    tmp, agent_dir = _sandbox_agent(None)   # no store at all
    try:
        r = _run(agent_dir, "g-1", "--trigger", TRIGGER_NONRECURRING)
        assert r.returncode == 0, r.stderr
        assert _sentinel(agent_dir) is not None, "no store at all must still fire, unbounded or not"
    finally:
        _rm(tmp)


def test_fail_closed_on_unbounded_path_for_cross_goal_record():
    """ outcome 5 (negative test): the unbounded path is keyed on
    goal_id — a DIFFERENT goal's fresh record is not coverage for this goal,
    whatever its age."""
    tmp, agent_dir = _sandbox_agent([
        {"id": "exp-1", "goal_id": "g-OTHER", "created": _iso(1)},
    ])
    try:
        r = _run(agent_dir, "g-1", "--trigger", TRIGGER_NONRECURRING)
        assert r.returncode == 0, r.stderr
        s = _sentinel(agent_dir)
        assert s is not None, "another goal's record must not cover this goal on the unbounded path"
        assert s["goal_id"] == "g-1"
        assert s["trigger"] == TRIGGER_NONRECURRING
    finally:
        _rm(tmp)


def test_nonrecurring_discriminator_is_the_sibling_constant_not_a_copy():
    """ outcome 2: the producer discriminator must be the
    NONRECURRING_PRODUCER constant imported from spark-fire-dedup.py, not a
    second literal in this file. If someone 'inlines' it, this fails."""
    import importlib.util as _ilu

    sfd_spec = _ilu.spec_from_file_location("sfd_test", str(CORE_SCRIPTS / "spark-fire-dedup.py"))
    sfd = _ilu.module_from_spec(sfd_spec)
    sfd_spec.loader.exec_module(sfd)

    pgec_spec = _ilu.spec_from_file_location("pgec_test", str(HELPER))
    pgec = _ilu.module_from_spec(pgec_spec)
    pgec_spec.loader.exec_module(pgec)

    assert pgec._NONRECURRING_PRODUCER == sfd.NONRECURRING_PRODUCER, (
        "the helper's discriminator drifted from the sibling's constant — "
        "import it, do not copy it (g-115-5314)"
    )
    # And the REAL call-site triggers must still discriminate as intended:
    assert TRIGGER_NONRECURRING.startswith(pgec._NONRECURRING_PRODUCER), (
        "iteration-close.sh's trigger no longer names the non-recurring producer"
    )
    assert not TRIGGER_RECURRING.startswith(pgec._NONRECURRING_PRODUCER), (
        "the recurring trigger must NOT start with the non-recurring producer"
    )
    # No EXECUTABLE second copy of the producer value in the helper's source.
    # A comment/docstring MAY quote the sibling's definition (this file's
    # module-level note does, for provenance), but no live code line may define
    # it — the value must come from the import, not a re-typed literal.
    helper_src = HELPER.read_text(encoding="utf-8")
    literal = '"nonrecurring-state-update"'
    code_lines = [
        ln for ln in helper_src.splitlines()
        if literal in ln and not ln.lstrip().startswith("#")
    ]
    assert not code_lines, (
        "the producer literal is defined in per-goal-experience-check.py code — "
        f"it must come from spark-fire-dedup.py (g-115-5314): {code_lines}"
    )


def test_call_site_triggers_match_the_discriminator():
    """The two call sites pass exactly the triggers the discriminator keys on.
    Pins the seam end-to-end: if either call site changes its --trigger value
    without updating this test, one of the two paths silently loses its bound
    choice."""
    iter_src = ITERATION_CLOSE_SH.read_text(encoding="utf-8")
    recur_src = RECURRING_CLOSE_SH.read_text(encoding="utf-8")
    assert TRIGGER_NONRECURRING in iter_src, (
        "iteration-close.sh no longer passes the non-recurring trigger this test family assumes"
    )
    assert TRIGGER_RECURRING in recur_src, (
        "recurring-close.sh no longer passes the recurring trigger this test family assumes"
    )


# ──────────────────── extraction + wiring invariants ────────────────────

def test_recurring_close_keeps_no_fork_of_the_extracted_logic():
    """guard-2015: the origin must not keep a copy, or it rots silently.

    Asserted on the DISTINCTIVE lines of the extracted block, not on the word
    `force_experience_archival` — recurring-close.sh legitimately still mentions
    the sentinel in its comments.
    """
    src = RECURRING_CLOSE_SH.read_text(encoding="utf-8")
    for forked in (
        'e.get("goal_id"), e.get("source_goal")',
        '"trigger": "recurring-close-postflip-deep-no-recent-entry"',
        '"set", "force_experience_archival"',
    ):
        assert forked not in src, (
            f"recurring-close.sh still carries the extracted logic: {forked!r} "
            "(guard-2015 — delete the origin copy in the SAME change)"
        )
    assert "per-goal-experience-check.py" in src, (
        "recurring-close.sh must invoke the shared helper"
    )
    # The trigger label the helper now receives must still be the original one,
    # so consumers keyed on it are unaffected by the extraction.
    assert "recurring-close-postflip-deep-no-recent-entry" in src


def test_nonrecurring_close_path_invokes_the_helper_gated_and_loudly():
    src = ITERATION_CLOSE_SH.read_text(encoding="utf-8")
    marker = "Phase 4.25 PER-GOAL experience check for NON-recurring deep closes"
    assert marker in src, "the non-recurring wiring is missing from iteration-close.sh"
    idx = src.index(marker)
    end = src.index("End Phase 4.25 per-goal experience check", idx)
    block = src[idx:end]

    # Assert on EXECUTABLE lines only. The block's own comments quote the very
    # strings under test (`|| true`), so a whole-block substring check reports a
    # prose mention as live code — the guard-2401 false-positive class, caught by
    # this test failing on its own first run.
    code = "\n".join(
        ln for ln in block.splitlines() if ln.strip() and not ln.lstrip().startswith("#")
    )

    assert "per-goal-experience-check.py" in code
    assert "_winpath" in code, "python3 <file-arg> in this file must route through _winpath"
    assert '"$OUTCOME" == "deep"' in code, "must fire only on deep closes"
    assert '"$_su_is_recurring" != "true"' in code, (
        "must NOT fire for recurring goals — recurring-close.sh already runs the check"
    )
    # The insight trigger this goal was filed under: degradation must be visible.
    assert "|| echo" in code and "WARN" in code, (
        "a bare `|| true` here makes the check undetectable in exactly the "
        "scenario it exists for (msg-20260801-171952-zeta-5643)"
    )
    assert "|| true" not in code, f"bare `|| true` in executable lines:\n{code}"


def test_store_level_check_is_untouched():
    """The store-level canary stays as the long-horizon backstop."""
    src = ITERATION_CLOSE_SH.read_text(encoding="utf-8")
    assert 'bash "$SCRIPT_DIR/experience-staleness-check.sh"' in src


def _rm(path: Path) -> None:
    import shutil
    shutil.rmtree(path, ignore_errors=True)
