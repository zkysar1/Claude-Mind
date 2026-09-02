"""iteration-open contract pins ().

WHAT THESE PIN, AND WHY EACH ONE EXISTS

iteration-open is a loop-ENTRY battery: it composes other batteries and reports.
Its failure mode is not crashing -- it is reporting a clean entry when it did not
actually look. Every pin below targets that class, not happy-path plumbing:

  * the lane count is DERIVED from the tier table, so a lane added there is a
    loud diff here rather than a silent omission (the goal's own check);
  * a zero-row parse RAISES instead of rendering as "0 lanes, all clean";
  * a stage rc != 0 is PRINTED, never swallowed (the goal's other check);
  * `status` and `completeness` stay ORTHOGONAL -- a blind stage can never render
    as an all-clear (guard-4093, the same aggregation the always-run battery pins);
  * a worker Body does NOT write the agent-wide meter stamps, because `end`
    unlinks a syncable file the reducer is using.

The runner is injected everywhere, so no test shells out: these are contract pins
on the aggregation, not an integration test of the composed batteries (each of
those owns its own suite).
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "iteration_open", SCRIPTS / "iteration-open.py"
)
io_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(io_mod)


TABLE = """
Some preamble prose that must not be parsed as a row.

| Phase | Sweep name (for `meter check`) | Tier | Invocation (exact) |
|---|---|---|---|
| 0-pre | tree-debt-gate | always-run | dispatched by battery |
| 0-pre2 | experience-archival-gate | always-run | same battery |
| 0-pre2.5 | evolution-finalize-gate | always-run | same battery |
| 0-pre3 | fresh-eyes-code-gate | always-run | same battery |
| 0.5b.1b | inbox-alert-age-check | always-run | `bash x.sh --apply` |
| 0.5b.1c | user-blocker-escalation-check | always-run | `bash y.sh --apply` |
| 0.5b.2 | dependency-timeout-check | always-run | `bash z.sh --apply` |
| 0.5b.2b | handoff-aging-check | always-run | `bash w.sh --apply` |
| 0.5g.7 | completed-not-closed-drain | always-run | `bash v.sh --json` |
| 0.5.0 | precheck-eval | medium | `bash e.sh run-all` |
| 0.5b.5 | pending-questions-sweep | deferrable | `bash q.sh sweep --apply` |

Trailing prose.
"""


@pytest.fixture()
def table(tmp_path):
    p = tmp_path / "SKILL.md"
    p.write_text(TABLE, encoding="utf-8")
    return str(p)


def make_runner(responses, record=None):
    """(argv, timeout) -> (rc, stdout, elapsed_ms, err). `responses` is keyed on
    the script name (argv[0]); the default is a clean empty battery report."""
    def runner(argv, timeout):
        if record is not None:
            record.append(list(argv))
        r = responses.get(argv[0], (0, json.dumps({"findings": [], "blind": []}), None))
        rc, out, err = r
        return rc, out, 5, err
    return runner


# --- the lane registry is DERIVED, never copied ------------------------------

def test_lane_count_comes_from_the_table_not_a_copy(table):
    rows = io_mod.parse_tier_table(table)
    assert len(rows) == 11, "one row per tier-tagged table line, prose excluded"
    assert {r["tier"] for r in rows} == {"always-run", "medium", "deferrable"}


def test_a_lane_added_to_the_table_appears_without_editing_this_script(table, tmp_path):
    """The loud-diff property. If this ever fails, someone has copied the registry
    into Python and the goal's `count == table row count` check is now vacuous."""
    before = len(io_mod.parse_tier_table(table))
    p = Path(table)
    p.write_text(
        p.read_text(encoding="utf-8").replace(
            "Trailing prose.",
            "| 0.5z | brand-new-sweep | deferrable | `bash new.sh` |\nTrailing prose.",
        ),
        encoding="utf-8",
    )
    assert len(io_mod.parse_tier_table(table)) == before + 1


def test_zero_row_parse_raises_instead_of_reading_as_an_empty_registry(tmp_path):
    """guard-1641/2421: an empty registry that renders as 'all clean' is the
    unreachable-vs-empty collapse this whole battery exists to avoid."""
    p = tmp_path / "SKILL.md"
    p.write_text("# no table here at all\n", encoding="utf-8")
    with pytest.raises(ValueError, match="ZERO rows"):
        io_mod.parse_tier_table(str(p))


def test_dry_run_exits_nonzero_when_the_registry_is_unreadable(tmp_path, capsys):
    """--dry-run is a VERIFICATION mode, so it must be able to FAIL. A check that
    always exits 0 proves nothing (the g-335-1282 `grep -qv` defect)."""
    p = tmp_path / "SKILL.md"
    p.write_text("nothing\n", encoding="utf-8")
    assert io_mod.dry_run(as_json=True, md_path=str(p)) == 1


def test_dry_run_marks_unwired_lanes_rather_than_hiding_them(table, capsys):
    io_mod.dry_run(as_json=True, md_path=table)
    d = json.loads(capsys.readouterr().out)
    assert d["lane_count"] == 11
    # 10, not 9:  wired the medium tier, and this fixture's medium row
    # is precheck-eval. (The same change also added world-script-crlf-check to the
    # always-run stage's `covers` — a real under-report the coverage arithmetic had
    # been carrying since  — but that lane is not in this fixture table,
    # so it is not what moved this number. Attribute counts to the lane that
    # actually moved them.)
    assert d["wired_count"] == 10, "always-run + the medium tier are dispatched"
    unwired = [l["sweep"] for l in d["lanes"] if not l["wired"]]
    # Only the deferrable row remains — that tier is strangler step 3.
    assert set(unwired) == {"pending-questions-sweep"}


# --- a non-zero rc is PRINTED, never swallowed (the goal's explicit check) ----

def test_stage_rc_nonzero_is_printed_in_the_table(table, capsys):
    runner = make_runner({
        "precheck-sentinel-battery.sh": (1, json.dumps({"findings": [], "blind": []}), None),
    })
    io_mod.run(runner=runner, md_path=table)
    out = capsys.readouterr().out
    line = [l for l in out.splitlines() if l.startswith("sentinel-battery")]
    assert line and " 1 " in line[0], f"rc=1 must appear in the stage table: {line}"


def test_timeout_renders_as_rc_124_and_a_blind_stage(table, capsys):
    runner = make_runner({
        "precheck-always-run-battery.sh": (124, "", "always-run: timeout after 180s"),
    })
    io_mod.run(runner=runner, md_path=table)
    out = capsys.readouterr().out
    assert "124" in out, "the shell timeout convention must be visible in the table"
    assert "BLIND" in out


def test_unparseable_stage_output_is_blind_not_clean(table, capsys):
    """A battery that broke or changed shape did not report 'nothing'; it reported
    nothing WE COULD READ. Folding that into a zero is the defect."""
    runner = make_runner({"precheck-sentinel-battery.sh": (0, "not json", None)})
    io_mod.run(as_json=True, runner=runner, md_path=table)
    d = json.loads(capsys.readouterr().out)
    assert d["completeness"] == "partial"
    assert any(b["stage"] == "sentinel-battery" for b in d["blind"])


# --- guard-4093: status and completeness are ORTHOGONAL ----------------------

def test_blind_stage_with_no_findings_never_renders_as_an_all_clear(table, capsys):
    runner = make_runner({"precheck-sentinel-battery.sh": (0, "", "spawn failed")})
    io_mod.run(runner=runner, md_path=table)
    out = capsys.readouterr().out
    assert "NO FINDINGS REACHED" in out
    assert "UNREACHABLE, not clean" in out
    assert "all dispatched lanes clean" not in out


def test_clean_run_says_clean_only_when_completeness_is_complete(table, capsys):
    io_mod.run(runner=make_runner({}), md_path=table)
    out = capsys.readouterr().out
    assert "no findings; all dispatched lanes clean" in out
    assert "UNREACHABLE" not in out


def test_findings_and_blindness_are_reported_together_not_collapsed(table, capsys):
    runner = make_runner({
        "precheck-always-run-battery.sh": (0, json.dumps(
            {"findings": [{"name": "handoff-aging-check", "detail": ["candidate_count=11"]}],
             "blind": []}), None),
        "precheck-sentinel-battery.sh": (0, "", "spawn failed"),
    })
    io_mod.run(as_json=True, runner=runner, md_path=table)
    d = json.loads(capsys.readouterr().out)
    assert d["status"] == "findings" and d["completeness"] == "partial", (
        "status answers 'found anything', completeness answers 'saw everything' — "
        "they are independent and must never be folded into one verdict"
    )


# --- the worker/reducer meter split ------------------------------------------

def test_worker_body_does_not_write_the_agent_wide_meter(table, monkeypatch, capsys):
    """`meter end` UNLINKS a syncable agent-wide file. A worker Body calling it
    would destroy the reducer's in-flight meter session cross-box."""
    monkeypatch.setenv("BODY_ROLE", "worker")
    calls = []
    io_mod.run(runner=make_runner({}, record=calls), md_path=table)
    assert not any(a[0] == "aspirations-precheck-budget-meter.sh" for a in calls), \
        "a worker Body must never invoke the agent-wide budget meter"
    assert "METER: stamps SKIPPED" in capsys.readouterr().out, \
        "the skip must be visible, not silent (guard-1760)"


def test_reducer_writes_both_meter_stamps(table, monkeypatch):
    """The complement (guard-2783: a role-conditional behaviour states both sides).
    precheck-gap-check reads exactly these two stamps."""
    monkeypatch.setenv("BODY_ROLE", "reducer")
    calls = []
    io_mod.run(runner=make_runner({}, record=calls), md_path=table)
    meter = [a for a in calls if a[0] == "aspirations-precheck-budget-meter.sh"]
    assert [a[1] for a in meter] == ["start", "end"]


# --- apply pass-through ------------------------------------------------------

def test_apply_reaches_only_the_stage_that_declares_it(table, monkeypatch):
    """orchestrator-entry-battery is READ-ONLY by contract; handing it --apply
    would silently break that contract."""
    monkeypatch.setenv("BODY_ROLE", "reducer")
    calls = []
    io_mod.run(apply=True, runner=make_runner({}, record=calls), md_path=table)
    by_script = {a[0]: a for a in calls}
    assert "--apply" in by_script["precheck-always-run-battery.sh"]
    assert "--apply" not in by_script["orchestrator-entry-battery.sh"]
    assert "--apply" not in by_script["precheck-sentinel-battery.sh"]


# --- the terminal imperative -------------------------------------------------

def test_terminal_line_is_the_next_action_imperative(table, capsys):
    """Mirrors iteration-close's `═══ ITERATION COMPLETE ═══` imperative — the
    line that has to survive summarization for the entry to be re-derivable."""
    io_mod.run(runner=make_runner({}), md_path=table)
    lines = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert lines[-1].startswith("[iteration-open] NEXT ACTION REQUIRED:")


def test_selector_error_surfaces_rather_than_reading_as_zero_candidates(table, capsys):
    """goal-selector.sh's wrapper already fails loud on the  silent-empty
    signature; this battery must not paper over it with a comfortable 0."""
    runner = make_runner({"goal-selector.sh": (0, "not json", None)})
    io_mod.run(as_json=True, runner=runner, md_path=table)
    d = json.loads(capsys.readouterr().out)
    assert d["candidates"]["count"] is None
    assert "unparseable" in d["candidates"]["error"]
    assert "selector" in d.get("error", "")


# --- fail-open: entry must never be blocked ----------------------------------

def test_every_stage_failing_still_exits_zero(table):
    """An entry gate that can refuse entry is worse than the drift it corrects."""
    runner = make_runner({
        "orchestrator-entry-battery.sh": (2, "", "boom"),
        "precheck-sentinel-battery.sh": (2, "", "boom"),
        "precheck-always-run-battery.sh": (2, "", "boom"),
        "goal-selector.sh": (2, "", "boom"),
    })
    assert io_mod.run(runner=runner, md_path=table) == 0


def test_unreadable_registry_still_runs_the_stages(table, tmp_path, capsys):
    """Losing the lane INVENTORY must not lose the lane EXECUTION -- the stages
    are what actually protect the loop; coverage reporting is commentary."""
    calls = []
    bad = tmp_path / "gone.md"
    io_mod.run(as_json=True, runner=make_runner({}, record=calls), md_path=str(bad))
    d = json.loads(capsys.readouterr().out)
    assert d["coverage"] is None
    assert any(b["name"] == "lane-registry" for b in d["blind"])
    assert {a[0] for a in calls} >= {
        "orchestrator-entry-battery.sh",
        "precheck-sentinel-battery.sh",
        "precheck-always-run-battery.sh",
    }


# --- the wrapper's mode-dependent exit code ----------------------------------

def test_wrapper_preserves_dry_run_rc_but_fails_open_in_run_mode(tmp_path):
    """The wrapper forces exit 0 for loop entry and PRESERVES it for --dry-run.
    Collapsing the two would make the verification check unable to fail."""
    from _bash_helpers import BASH  # guard-580: never a bare "bash" argv[0]

    bad = tmp_path / "nope.md"
    # .as_posix() throughout, never str(Path) — bash silently strips the
    # backslashes of a str(WindowsPath) (guard-581).
    dry = subprocess.run(
        [BASH, (SCRIPTS / "iteration-open.sh").as_posix(), "--dry-run",
         "--tier-table", bad.as_posix()],
        capture_output=True, text=True, timeout=120,
    )
    assert dry.returncode == 1, "an unreadable registry must FAIL the dry-run check"
    assert "unreadable" in (dry.stderr or "")


def test_wrapper_calls_a_silent_run_blind_instead_of_passing_it_off_as_clean(tmp_path):
    """rc=0 with ZERO stdout is the one failure this wrapper cannot otherwise see.

    _emit() prints the STAGE table unconditionally, so zero stdout PROVES the
    report was never emitted -- yet run mode forces exit 0, which makes silence
    indistinguishable from "ran clean" to a caller whose SKILL.md says to dispose
    what it prints. Measured on foxtrot (LAPTOP-3IOFCNEO, WSL2 6.18.33.2)
    2026-08-21: `--apply` returned rc=0 / 0 bytes / ~370s while the standalone
    fallback returned two real findings minutes later, and the run was read as an
    all-clear. Not reproducible on cc-07 (Linux 6.8.0-137-generic); root cause is
    NOT established, so what is pinned here is that the failure is LOUD, not that
    it is cured (guard-4093 / guard-1715).

    The stub-sibling shape is required, not incidental: the wrapper resolves
    iteration-open.py from its OWN dirname, so copying it beside a stub is the
    only way to force a silent run without touching the real script.
    """
    from _bash_helpers import BASH  # guard-580: never a bare "bash" argv[0]

    wrapper = tmp_path / "iteration-open.sh"
    wrapper.write_bytes((SCRIPTS / "iteration-open.sh").read_bytes())
    stub = tmp_path / "iteration-open.py"

    stub.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    silent = subprocess.run(
        [BASH, wrapper.as_posix(), "--apply"],
        capture_output=True, text=True, timeout=120,
    )
    assert silent.returncode == 0, "run mode must stay fail-open"
    assert "SILENT RUN" in silent.stdout
    assert "BLIND" in silent.stdout, "the warning must route the reader to the fallbacks"

    # NEGATIVE CONTROL. Without this half the assertion above would pass just as
    # happily against a wrapper that printed the warning unconditionally, which is
    # a detector that can never be wrong and therefore never useful (guard-3534).
    stub.write_text('print("STAGE  rc  elapsed  note")\n', encoding="utf-8")
    noisy = subprocess.run(
        [BASH, wrapper.as_posix(), "--apply"],
        capture_output=True, text=True, timeout=120,
    )
    assert noisy.returncode == 0
    assert "SILENT RUN" not in noisy.stdout, "output present must never be called silent"


# ── stage-registry parity () ───────────────────────────────────────

def _battery_lane_names(filename):
    spec = importlib.util.spec_from_file_location(
        filename.replace("-", "_").replace(".py", ""), SCRIPTS / filename
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return {lane["name"] for lane in mod.LANES}


def test_iteration_open_stage_registry_parity():
    """A stage's `covers` must equal what its battery actually runs.

    The STAGES header promises that keeping `covers` declarative means "a newly-
    wired battery is a data edit here and a registry edit there, with the coverage
    arithmetic catching any disagreement between the two". Nothing compared them,
    so the arithmetic caught nothing: `world-script-crlf-check` was registered in
    precheck-always-run-battery.LANES by g-115-7288 and never added to `covers`,
    so it RAN every iteration while `--dry-run` printed it unwired and
    not_yet_wired_count was inflated by one (found 2026-08-26, g-115-7847).

    The direction that matters most is RUNS-BUT-UNCLAIMED: an under-report makes
    the entry battery look less complete than it is, which invites someone to
    "wire" a lane that is already wired and run it twice. CLAIMED-BUT-NOT-RUN is
    worse in consequence -- a lane reported as covered that never executes is the
    silent-absence class this whole goal exists to close -- so both are asserted.
    """
    registries = {
        "always-run-battery": "precheck-always-run-battery.py",
        "medium-battery": "precheck-medium-battery.py",
    }
    checked = 0
    for stage in io_mod.STAGES:
        filename = registries.get(stage["key"])
        if filename is None:
            continue          # sentinel/entry stages hold no LANES tuple
        checked += 1
        actual = _battery_lane_names(filename)
        claimed = set(stage["covers"])
        assert actual - claimed == set(), (
            f'{stage["key"]}: runs but does not claim {sorted(actual - claimed)} — '
            f"coverage under-reports and `--dry-run` will print these unwired"
        )
        assert claimed - actual == set(), (
            f'{stage["key"]}: claims but does not run {sorted(claimed - actual)} — '
            f"coverage over-reports; these lanes are silently absent"
        )
    assert checked == len(registries), (
        f"expected to check {len(registries)} registries, checked {checked} — a "
        f"stage key was renamed and this test silently stopped covering it"
    )


def test_the_medium_tier_is_dispatched_by_a_stage():
    """The regression pin for the 208h/94.3h dark window.

    Between 2026-08-17 (iteration-open landing) and 2026-08-26 no medium lane ran
    from loop entry on either measured box. If a future refactor drops this stage,
    the tail goes dark again in exactly the same silent way -- no error, no signal,
    `sweeps_dropped: 0` still reading healthy. Assert the dispatch exists.
    """
    wired = {c for s in io_mod.STAGES for c in s["covers"]}
    for lane in ("defer-recheck", "precondition-defer-recheck", "blocker-recheck",
                 "precheck-eval", "recurring-starvation-check"):
        assert lane in wired, f"{lane} is no longer dispatched from loop entry"


# --- both legitimate selector shapes () -----------------------------
#
# goal-selector cmd_select emits a bare LIST normally and a DICT carrying
# all_blocked in the every-goal-blocked branch. Only the list was ever exercised
# here, so the dict rendered as `expected a list, got dict` -- an ERROR in exactly
# the state whose signal the iteration most needs. These pin BOTH shapes, which is
# the whole point: the defect was that one of two real branches was untested.

ALL_BLOCKED_PAYLOAD = {
    "candidates": [],
    "all_blocked": True,
    "blocked_count": 7,
    "by_reason": {
        "defer_reason": {"count": 4, "goal_ids": []},
        "blocked_by": {"count": 3, "goal_ids": []},
    },
    "blocked_goals": [{"goal_id": "g-1-1", "title": "t", "reason": "defer_reason",
                       "detail": ""}],
}


def test_all_blocked_dict_is_not_an_error(table, capsys):
    """The ALL-BLOCKED branch is a legitimate producer shape, not drift."""
    runner = make_runner(
        {"goal-selector.sh": (0, json.dumps(ALL_BLOCKED_PAYLOAD), None)})
    io_mod.run(as_json=True, runner=runner, md_path=table)
    c = json.loads(capsys.readouterr().out)["candidates"]
    assert "error" not in c, c
    assert c["all_blocked"] is True
    assert c["blocked_count"] == 7


def test_all_blocked_zero_is_a_measured_zero_not_a_failed_measurement(table, capsys):
    """guard-1091: count 0 here means the selector RAN and found nothing eligible.
    It must stay distinguishable from the count None every error path returns --
    otherwise a wedged queue and a broken selector read identically."""
    runner = make_runner(
        {"goal-selector.sh": (0, json.dumps(ALL_BLOCKED_PAYLOAD), None)})
    io_mod.run(as_json=True, runner=runner, md_path=table)
    c = json.loads(capsys.readouterr().out)["candidates"]
    assert c["count"] == 0
    assert c["count"] is not None


def test_all_blocked_routing_signal_reaches_the_operator(table, capsys):
    """blocked_count/by_reason are what the all-blocked handler needs. Printing
    '0 candidate(s); top: (none)' would read as a quiet nothing-to-do."""
    runner = make_runner(
        {"goal-selector.sh": (0, json.dumps(ALL_BLOCKED_PAYLOAD), None)})
    io_mod.run(runner=runner, md_path=table)
    out = capsys.readouterr().out
    assert "ALL BLOCKED" in out
    assert "7 blocked goal(s)" in out
    assert "defer_reason=4" in out
    assert "all-blocked handler" in out


def test_normal_list_shape_still_works(table, capsys):
    """The positive control for the three pins above (guard-2421): if this
    regressed, they would pass while the common path was broken."""
    runner = make_runner({"goal-selector.sh": (0, json.dumps(
        [{"goal_id": "g-9-9", "score": 12.5, "title": "a real candidate"}]), None)})
    io_mod.run(as_json=True, runner=runner, md_path=table)
    c = json.loads(capsys.readouterr().out)["candidates"]
    assert c["count"] == 1
    assert "g-9-9" in c["top"]
    assert not c.get("all_blocked")


def test_a_dict_without_all_blocked_still_errors(table, capsys):
    """Widening the guard must not blanket-accept dicts -- a dict that is neither
    producer branch is still a genuinely unexpected shape."""
    runner = make_runner(
        {"goal-selector.sh": (0, json.dumps({"unexpected": "shape"}), None)})
    io_mod.run(as_json=True, runner=runner, md_path=table)
    c = json.loads(capsys.readouterr().out)["candidates"]
    assert c["count"] is None
    assert "expected a list, got dict" in c["error"]


def test_by_reason_shows_the_largest_reasons_and_discloses_truncation(table, capsys):
    """Fresh-eyes finding on 's own diff: the first cut sliced
    by_reason at 5 in DICT ORDER and said nothing about the rest. A silently
    truncated list reads as the complete picture -- wrong exactly when the
    operator is working out how the queue got wedged (no-silent-caps)."""
    payload = dict(ALL_BLOCKED_PAYLOAD)
    payload["by_reason"] = {
        "small_a": {"count": 1}, "small_b": {"count": 2}, "small_c": {"count": 3},
        "mid": {"count": 40}, "big": {"count": 500}, "small_d": {"count": 4},
        "small_e": {"count": 5},
    }
    payload["blocked_count"] = 555
    runner = make_runner({"goal-selector.sh": (0, json.dumps(payload), None)})
    io_mod.run(runner=runner, md_path=table)
    out = capsys.readouterr().out
    assert "big=500" in out and "mid=40" in out          # largest are shown
    assert "small_a=1" not in out                         # smallest elided
    assert "+2 more reason(s) not shown" in out           # elision disclosed


# --- the premise-supersession advisory on the top candidate () -----

def _emit_with_premise(monkeypatch, capsys, payload, rc=0, boom=None, stderr=""):
    """Drive _emit's SELECTION branch with a stubbed premise-check subprocess."""
    class _R:
        def __init__(self): self.stdout = payload; self.stderr = stderr; self.returncode = rc

    def fake_run(*a, **k):
        if boom:
            raise boom
        return _R()

    monkeypatch.setattr(io_mod.subprocess, "run", fake_run)
    io_mod._emit({"candidates": {"count": 3, "top": "g-115-7935 (12.60) Some title"}},
                 as_json=False)
    return capsys.readouterr().out


def test_premise_advisory_speaks_even_when_it_has_nothing_to_flag(monkeypatch, capsys):
    """The QUIET branch must print.

    This is the pin that matters. A clean premise-check emits no warning, which is
    byte-identical to the block never executing -- and the handler is fail-open, so
    a dead call site raises nothing either. Measured on the first live --apply entry
    after the block was wired: the top candidate was 0d old with no outcome_note,
    both loud branches were correctly silent, and working was indistinguishable from
    dead. An advisory whose whole purpose is to fight "green is the only observable
    state" must not itself have one observable state.
    """
    out = _emit_with_premise(monkeypatch, capsys, json.dumps({
        "goal_id": "g-115-7935", "verdict": "NO-CITED-MEASUREMENT",
        "age_days": 0, "cited_measurement_count": 0,
        "own_record_fields_present": [], "cited_measurements": [],
        "commits_touching_named_paths_since_filing": [],
    }))
    assert "[premise-supersession]" in out, (
        "the clean case printed nothing — silence here cannot be distinguished "
        "from the call site never running"
    )


def test_premise_advisory_flags_an_aged_cited_measurement(monkeypatch, capsys):
    out = _emit_with_premise(monkeypatch, capsys, json.dumps({
        "goal_id": "g-115-3206", "verdict": "RE-MEASURE-BEFORE-EXECUTING",
        "age_days": 31, "cited_measurement_count": 23,
        "own_record_fields_present": ["outcome_note"],
        "cited_measurements": ["113%", "97.5%"],
        "commits_touching_named_paths_since_filing": ["abc123\t2026-08-01\tfix"],
    }))
    assert "RE-MEASURE BEFORE EXECUTING" in out and "31d ago" in out
    assert "cites: 113%" in out, "the citations themselves must reach the reader"
    # Two-way discrimination: the aged verdict must not render like the quiet one.
    assert "nothing to re-measure" not in out


def test_premise_advisory_failure_is_loud_not_swallowed(monkeypatch, capsys):
    """Fail-open must not mean fail-silent.

    A bare `except: pass` here already hid a real defect during authoring: the block
    referenced a module name that was never imported, raised NameError at runtime,
    and the check simply never fired.
    """
    out = _emit_with_premise(monkeypatch, capsys, "", boom=RuntimeError("boom"))
    assert "did not run" in out and "UNVERIFIED" in out, (
        "a check that declines to run must say so — reporting success by default "
        "is the failure mode this whole advisory exists to catch"
    )


def test_a_failing_child_is_voiced_not_swallowed(monkeypatch, capsys):
    """rc!=0 with EMPTY stdout must print — the F-001 regression.

    The premise-check script is LOUD BY CONTRACT: on any load failure it writes
    its diagnostic to stderr and exits 2 with stdout empty. The first version of
    this call site gated only on `stdout.strip()`, so that path printed NOTHING,
    and the surrounding `except` never fired because subprocess.run itself had
    succeeded. Byte-identical to a dead call site -- the exact failure the whole
    advisory exists to prevent, reproduced in its own caller.

    Not hypothetical: the child invoked a bare `.sh` as argv[0], which Windows
    CreateProcess cannot exec, so rc=2 was the PERMANENT state on every Windows
    box in the fleet.
    """
    out = _emit_with_premise(
        monkeypatch, capsys, "", rc=2,
        stderr="[premise-supersession] CANNOT CHECK: no goal record returned\n"
               "[premise-supersession] This is NOT a clean result.\n")
    assert "[premise-supersession]" in out, "a failing child printed nothing at all"
    assert "UNVERIFIED" in out and "rc=2" in out, (
        "the failure must name itself and its exit code, not degrade to silence"
    )
    # Must not be mistaken for the quiet (healthy) branch.
    assert "nothing to re-measure" not in out


def test_the_stale_verdict_exit_code_is_not_treated_as_failure(monkeypatch, capsys):
    """rc=1 is the script's STALE verdict, not an error.

    premise_supersession_check.main() returns 1 when the verdict is
    RE-MEASURE-BEFORE-EXECUTING. Treating every non-zero rc as failure would
    swallow the single most important verdict the check produces.
    """
    out = _emit_with_premise(monkeypatch, capsys, json.dumps({
        "goal_id": "g-115-3206", "verdict": "RE-MEASURE-BEFORE-EXECUTING",
        "age_days": 31, "cited_measurement_count": 2,
        "own_record_fields_present": [], "cited_measurements": ["113%"],
        "commits_touching_named_paths_since_filing": [],
    }), rc=1)
    assert "RE-MEASURE BEFORE EXECUTING" in out
    assert "check FAILED" not in out, "rc=1 is a verdict, not a failure"


# --- : a killed run must not look like a clean one -----------------

def test_selection_is_in_the_stage_table_with_its_own_timing(table, capsys):
    """The stage table must account for the selector, which is the biggest stage.

    It runs outside the STAGES loop and _selection() discarded the runner's
    elapsed_ms, so the table a reader consults to size a timeout omitted the single
    most expensive thing the run does. Measured on cc-08 2026-08-31 BEFORE the fix:
    68,205 ms wall against a 42,190 ms table sum — 26,015 ms (38%) invisible, with
    goal-selector.sh alone timing 23,442 ms as the positive control. Sizing a bound
    off that table under-shoots by more than a third, which is how a 110s bound got
    chosen for a run that cannot fit in it on a slower box.
    """
    runner = make_runner({"goal-selector.sh": (0, json.dumps([]), None)})
    io_mod.run(as_json=True, runner=runner, md_path=table)
    report = json.loads(capsys.readouterr().out)
    keys = [s["key"] for s in report["stages"]]
    assert "selection" in keys, f"selector missing from the stage table: {keys}"
    row = next(s for s in report["stages"] if s["key"] == "selection")
    assert isinstance(row["elapsed_ms"], int)
    assert report["candidates"]["elapsed_ms"] == row["elapsed_ms"]


def test_every_stage_announces_itself_on_stderr_BEFORE_it_runs(table, capsys):
    """The breadcrumb must precede the stage, or a kill leaves no name behind.

    THE ORDERING IS THE WHOLE POINT. A breadcrumb emitted after a stage completes
    tells you only about stages that already finished — the one that was in flight
    when the process died, which is the one you need, is exactly the one it omits.
    """
    seen = []
    base = make_runner({"goal-selector.sh": (0, json.dumps([]), None)})

    def runner(argv, timeout):
        seen.append(("RAN", argv[0]))
        return base(argv, timeout)

    io_mod.run(as_json=True, runner=runner, md_path=table)
    err = capsys.readouterr().err
    assert err, "a run that emits nothing on stderr is indistinguishable from a kill"
    for stage in io_mod.STAGES:
        arrow = f"-> {stage['key']}"
        done = f"{stage['key']} done"
        assert arrow in err, f"no pre-run breadcrumb for {stage['key']}"
        assert err.index(arrow) < err.index(done), (
            f"{stage['key']} announced itself only AFTER running — a kill during "
            "that stage would leave no trace of which stage it was"
        )


def test_breadcrumbs_go_to_stderr_so_the_wrappers_stdout_capture_cannot_eat_them(table, capsys):
    """iteration-open.sh redirects ONLY stdout (`> "$_OUT"`), so stderr reaches the
    caller live. Moving these to stdout would put them inside the captured file
    that a killed run never writes — reintroducing the defect invisibly, since a
    completed run would still look perfectly correct."""
    runner = make_runner({"goal-selector.sh": (0, json.dumps([]), None)})
    io_mod.run(as_json=True, runner=runner, md_path=table)
    cap = capsys.readouterr()
    assert "[iteration-open] ->" in cap.err
    assert "[iteration-open] ->" not in cap.out, "breadcrumbs must never touch stdout"
    json.loads(cap.out)  # stdout stays a single parseable JSON object
