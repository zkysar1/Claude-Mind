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
    assert d["wired_count"] == 9, "the 9 always-run lanes are dispatched today"
    unwired = [l["sweep"] for l in d["lanes"] if not l["wired"]]
    assert set(unwired) == {"precheck-eval", "pending-questions-sweep"}


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
