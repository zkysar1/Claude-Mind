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
import ast          # : the self-audit pin at the end of this file
import json
import os
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
    the script name (argv[0]); the default is a clean empty battery report.

    THE SELECTOR GETS ITS OWN DEFAULT, and it is not cosmetic (g-115-9528).
    goal-selector.sh emits a ranked LIST, not a battery {findings, blind}
    dict, so under the single shared default every test that did not
    override it ran with _selection() erroring "expected a list, got dict".
    That was INVISIBLE while a failed selection could not reach
    report["blind"] -- the very defect this goal fixes -- so the fixture
    named "a clean run" was never one. Fixing the fixture is what makes the
    completeness assertions mean what their names say.
    """
    def runner(argv, timeout):
        if record is not None:
            record.append(list(argv))
        if argv[0] == "goal-selector.sh":
            r = responses.get(argv[0], (0, "[]", None))
        else:
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


def test_reducer_end_keeps_the_metering_window_open(table, monkeypatch):
    """: the `end` MUST carry --keep-state.

    A BARE `end` unlinks the agent-wide state file. The 23 deferrable
    `meter check` calls that consult it live in aspirations-precheck-digest.md
    and execute in the LLM's own turns, AFTER run() has returned -- so a bare
    end closes the window before any of them can read the zone, and every one
    fails open to `run`. That is the defect this goal names: the meter's only
    remaining drop path, inert. guard-2832 measured the signature independently
    (92 precheck-end records, zone `fresh` on all 92).

    Pinned as the FLAG rather than the op because the op alone was ALREADY
    correct while the behaviour was broken -- the sibling test above passes in
    both regimes, so it cannot catch this (rb-8963: once the shape is callable,
    pin the behaviour). Deleting the `end` call instead would fix the window and
    regress the stamp precheck-gap-check reads from ~100% back to 82%.
    """
    monkeypatch.setenv("BODY_ROLE", "reducer")
    calls = []
    io_mod.run(runner=make_runner({}, record=calls), md_path=table)
    ends = [a for a in calls
            if a[0] == "aspirations-precheck-budget-meter.sh" and a[1] == "end"]
    assert len(ends) == 1, "exactly one meter end per run, got %d" % len(ends)
    assert "--keep-state" in ends[0], (
        "iteration-open must close with `end --keep-state`; a bare end unlinks "
        "the state file before precheck's deferrable checks run (g-115-9009). "
        "argv was: %r" % (ends[0],)
    )


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


def test_imperative_routes_through_select_before_execute(table, capsys):
    """The imperative is the chain the reducer actually follows, and until
    2026-09-03 it read "claim from SELECTION and enter execution —
    Skill(aspirations-execute)". A reducer read SELECTION as the candidates this
    battery prints and never invoked aspirations-select (measured downstream: 6
    consecutive precheck-led iterations, 0 select invocations, 5 tagged insight
    triggers unread). Directive ack/honor, the insight-trigger scan,
    self-abstention and the deviation check live only in that Skill, so the
    chain must name it, between the precheck tail and execute."""
    io_mod.run(runner=make_runner({}), md_path=table)
    last = [l for l in capsys.readouterr().out.splitlines() if l.strip()][-1]
    sel = last.find("Skill(aspirations-select)")
    exe = last.find("Skill(aspirations-execute)")
    assert sel != -1 and exe != -1, last
    assert sel < exe, last
    assert "claim from SELECTION" not in last


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
    # : the report path MUST be redirected here too. The wrapper's
    # persisted copy runs BEFORE the --dry-run branch returns, so this site was
    # truncating the live /tmp/iteration-open-report-<agent>.log of whatever agent
    # the suite ran under. Re-deriving the invocation list by grep (this goal's
    # outcome 1) is what surfaced this site: the filing description named only
    # three, and this is the fourth.
    dry = subprocess.run(
        [BASH, (SCRIPTS / "iteration-open.sh").as_posix(), "--dry-run",
         "--tier-table", bad.as_posix()],
        capture_output=True, text=True, timeout=120,
        env=dict(os.environ,
                 ITERATION_OPEN_REPORT_PATH=(tmp_path / "report.log").as_posix()),
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
    # : redirect the report path — see _stub_wrapper's docstring below.
    _env = dict(os.environ,
                ITERATION_OPEN_REPORT_PATH=(tmp_path / "report.log").as_posix())
    silent = subprocess.run(
        [BASH, wrapper.as_posix(), "--apply"],
        capture_output=True, text=True, timeout=120, env=_env,
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
        capture_output=True, text=True, timeout=120, env=_env,
    )
    assert noisy.returncode == 0
    assert "SILENT RUN" not in noisy.stdout, "output present must never be called silent"


def _stub_wrapper(tmp_path):
    """Wrapper copied beside a stub .py, plus a private report path.

    The report path MUST be redirected: its default is keyed only by agent
    name, so a test running under a live agent's name would overwrite that
    agent's real iteration-open report.
    """
    from _bash_helpers import BASH  # guard-580: never a bare "bash" argv[0]

    wrapper = tmp_path / "iteration-open.sh"
    wrapper.write_bytes((SCRIPTS / "iteration-open.sh").read_bytes())
    report = tmp_path / "report.log"
    env = dict(os.environ, ITERATION_OPEN_REPORT_PATH=report.as_posix())
    return BASH, wrapper, tmp_path / "iteration-open.py", report, env


def test_truncated_but_nonempty_report_is_not_an_all_clear(tmp_path):
    """The zero-byte guard fires only at exactly 0, so a report that stopped
    part-way sails past it: a non-empty prefix at rc=0 is byte-indistinguishable
    from a short clean run, and run mode forces exit 0. g-115-10295 measured the
    consequence -- two consecutive iterations on cc-04 ran a healthy precheck and
    read a 213-byte stub as clean, twice, because 213 != 0.

    iteration-open.py ends every non-dry-run report with the NEXT ACTION
    imperative (pinned by test_terminal_line_is_the_next_action_imperative just
    above), so its ABSENCE is the discriminator the byte count cannot provide.
    """
    BASH, wrapper, stub, _report, env = _stub_wrapper(tmp_path)

    # A plausible-looking PREFIX: real stage lines, no terminal imperative.
    stub.write_text(
        'print("STAGE  rc  elapsed  note")\n'
        'print("entry-checks  0  132ms  read-only by contract")\n',
        encoding="utf-8",
    )
    cut = subprocess.run([BASH, wrapper.as_posix(), "--apply"],
                         capture_output=True, text=True, timeout=120, env=env)
    assert cut.returncode == 0, "run mode must stay fail-open"
    assert "SILENT RUN" not in cut.stdout, "non-empty output is not silence"
    assert "REPORT INCOMPLETE" in cut.stdout, (
        "a report missing its terminal imperative must be called PARTIAL — "
        "this is the exact 213-byte shape that read as clean twice on cc-04"
    )

    # NEGATIVE CONTROL (guard-3534): a detector that fires on every run is one
    # that can never be wrong, and therefore never useful. The ONLY difference
    # here is the terminal imperative.
    stub.write_text(
        'print("STAGE  rc  elapsed  note")\n'
        'print("entry-checks  0  132ms  read-only by contract")\n'
        'print("[iteration-open] NEXT ACTION REQUIRED: dispose the findings")\n',
        encoding="utf-8",
    )
    whole = subprocess.run([BASH, wrapper.as_posix(), "--apply"],
                           capture_output=True, text=True, timeout=120, env=env)
    assert whole.returncode == 0
    assert "REPORT INCOMPLETE" not in whole.stdout, (
        "a report that reached its terminal imperative is complete"
    )


def test_report_is_persisted_off_the_synced_tree_for_the_caller_to_read(tmp_path):
    """outcome 2 of . The wrapper already captured stdout to a
    non-synced mktemp file and then DELETED it, so the only copy any agent ever
    read was the one flowing through the CALLER's redirect -- and that redirect
    is what the own-cloud sync layer truncates (guard-3789/guard-4045).

    Measured with a one-variable control on two boxes: the same busy 130s
    command cut at t=54s into agents/<agent>/temp/ on cc-03 (40-50s on cc-04)
    while its /tmp twin completed. Keeping the copy makes the report readable
    whatever the caller redirected to, which REMOVES the exposure rather than
    detecting it -- nothing inside this process can observe a truncation that
    happens downstream of it, after it exits.
    """
    BASH, wrapper, stub, report, env = _stub_wrapper(tmp_path)
    stub.write_text(
        'print("STAGE  rc  elapsed  note")\n'
        'print("[iteration-open] NEXT ACTION REQUIRED: dispose the findings")\n',
        encoding="utf-8",
    )
    run = subprocess.run([BASH, wrapper.as_posix(), "--apply"],
                         capture_output=True, text=True, timeout=120, env=env)
    assert run.returncode == 0
    assert report.exists(), "the report must survive the run, not be deleted with the tempfile"
    saved = report.read_text(encoding="utf-8")
    assert "NEXT ACTION REQUIRED" in saved, "the persisted copy must carry the TAIL — the part that goes missing"
    assert "STAGE" in saved
    assert report.as_posix() in run.stdout, "the caller must be told where the surviving copy is"


def test_report_pointer_and_completeness_check_stay_off_the_failure_and_json_paths(tmp_path):
    """Two false positives the first cut of  actually shipped, both
    found by re-reading rather than by a failing test — which is why they are
    pinned here.

    1. `cp` of an EMPTY file succeeds, so a bare "did the copy work" test
       announced a saved report of zero bytes immediately after SILENT RUN had
       correctly said there was nothing to read. A pointer to an empty artifact
       is worse than no pointer: it reads as a recovery path.
    2. --json emits ONE machine-parsed object, and a JSON object legitimately
       contains no terminal imperative — so the completeness check fired on
       EVERY --json run and the pointer line corrupted the object. A detector
       whose false-positive rate on a whole mode is 100% gets ignored, taking
       its true positives with it.
    """
    BASH, wrapper, stub, _report, env = _stub_wrapper(tmp_path)

    # (1) zero-byte run: SILENT RUN is right, the pointer is not.
    stub.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    empty = subprocess.run([BASH, wrapper.as_posix(), "--apply"],
                           capture_output=True, text=True, timeout=120, env=env)
    assert "SILENT RUN" in empty.stdout, "zero bytes must still be called silent"
    assert "full report also saved" not in empty.stdout, (
        "never point the reader at a zero-byte report — cp of an empty file succeeds"
    )
    assert "REPORT INCOMPLETE" not in empty.stdout, (
        "zero bytes is SILENT RUN's case; two warnings for one fault is noise"
    )

    # (2) --json must stay machine-parseable, and must not be called incomplete.
    stub.write_text('import json\nprint(json.dumps({"ok": True}))\n', encoding="utf-8")
    js = subprocess.run([BASH, wrapper.as_posix(), "--json"],
                        capture_output=True, text=True, timeout=120, env=env)
    assert js.returncode == 0
    assert "REPORT INCOMPLETE" not in js.stdout, (
        "a JSON object carries no terminal imperative BY DESIGN — flagging it "
        "makes the check fire on 100% of --json runs"
    )
    assert "full report also saved" not in js.stdout, "the pointer must not be appended to JSON"
    json.loads(js.stdout)  # raises if anything was appended to the object


def test_dry_run_does_not_replace_the_report_a_preceding_apply_wrote(tmp_path):
    """outcome 3 of . The persisted copy is written ABOVE the
    `if [ "$_DRY" = "1" ]` return, so --dry-run reached it too and overwrote the
    report at the SAME agent-keyed path with its lane table -- silently, because
    the dry-run branch returns before both the pointer line and the completeness
    check, so nothing on stdout said the report had been replaced.

    The cost is not the lost bytes, it is WHERE the clobber sits: --dry-run is
    the natural next command after a confusing --apply, so the one action a
    reader takes to recover destroyed the artifact the persisted copy exists to
    give them (g-115-10295 outcome 2).
    """
    BASH, wrapper, stub, report, env = _stub_wrapper(tmp_path)

    def _stub_printing(marker):
        stub.write_text(
            'print("STAGE  rc  elapsed  note")\n'
            f'print("entry-checks  0  132ms  {marker}")\n'
            'print("[iteration-open] NEXT ACTION REQUIRED: dispose the findings")\n',
            encoding="utf-8",
        )

    _stub_printing("APPLY-PAYLOAD")
    applied = subprocess.run([BASH, wrapper.as_posix(), "--apply"],
                             capture_output=True, text=True, timeout=120, env=env)
    assert applied.returncode == 0
    assert report.exists(), "the --apply run must persist its report"
    before = report.read_text(encoding="utf-8")
    assert "APPLY-PAYLOAD" in before

    # The recovery command. Distinguishable payload so a clobber is visible
    # rather than merely suspected.
    _stub_printing("DRY-RUN-PAYLOAD")
    dry = subprocess.run([BASH, wrapper.as_posix(), "--dry-run"],
                         capture_output=True, text=True, timeout=120, env=env)
    assert dry.returncode == 0
    after = report.read_text(encoding="utf-8")
    assert after == before, "--dry-run must not touch the report an --apply wrote"
    assert "DRY-RUN-PAYLOAD" not in after, (
        "the dry-run lane table is NOT the report — writing it here destroys the "
        "artifact the reader ran --dry-run to go and read"
    )

    # NEGATIVE CONTROL (guard-3534). Everything above holds just as happily if
    # persistence were broken outright, or if this test were watching a path
    # nothing writes. Same wrapper, same report path, same payload — only the
    # MODE differs — and now the report MUST change.
    live = subprocess.run([BASH, wrapper.as_posix(), "--apply"],
                          capture_output=True, text=True, timeout=120, env=env)
    assert live.returncode == 0
    replaced = report.read_text(encoding="utf-8")
    assert "DRY-RUN-PAYLOAD" in replaced, (
        "control failed: --apply did not rewrite the report, so the invariance "
        "above proved nothing about the --dry-run gate"
    )
    assert "APPLY-PAYLOAD" not in replaced


def test_report_persist_writes_aside_and_renames_leaving_no_residue(tmp_path):
    """outcome 4 of . _REPORT is keyed by agent name ALONE, and this
    script's own header documents the overlap case: at the 120s Bash bound the
    harness backgrounds the run and the stated remedy is to re-run at a higher
    bound, i.e. two same-agent runs by design. Measured: 8 trials of two
    concurrent `cp -f` of two distinguishable 2,040,000 B sources to one
    destination produced 1 INTERLEAVED file — a report that is neither run's.

    Writing aside and renaming makes the reader see one whole report or the
    previous one, never a splice. What is observable from outside the process is
    the contract pinned here: the temp never survives the run, and a persist that
    FAILS announces nothing and still exits 0 (a broken report copy must never
    fail loop entry).
    """
    BASH, wrapper, stub, report, env = _stub_wrapper(tmp_path)
    stub.write_text(
        'print("STAGE  rc  elapsed  note")\n'
        'print("[iteration-open] NEXT ACTION REQUIRED: dispose the findings")\n',
        encoding="utf-8",
    )
    ok = subprocess.run([BASH, wrapper.as_posix(), "--apply"],
                        capture_output=True, text=True, timeout=120, env=env)
    assert ok.returncode == 0
    assert report.exists()
    residue = sorted(p.name for p in report.parent.iterdir()
                     if p.name.startswith(report.name) and p.name != report.name)
    assert residue == [], f"the write-aside temp must not survive the run: {residue}"

    # A persist that cannot succeed: the destination directory does not exist,
    # so the copy fails before the rename. Nothing may be announced, nothing may
    # be left behind, and loop entry must still be fail-open.
    missing = tmp_path / "no-such-dir"
    failed = subprocess.run(
        [BASH, wrapper.as_posix(), "--apply"],
        capture_output=True, text=True, timeout=120,
        env=dict(env, ITERATION_OPEN_REPORT_PATH=(missing / "report.log").as_posix()),
    )
    assert failed.returncode == 0, "a failed report copy must never fail loop entry"
    assert "full report also saved" not in failed.stdout, (
        "never point the reader at a report the copy did not make"
    )
    assert not missing.exists(), "a failed persist must leave no residue behind"



# --- a stage that dies mid-run cannot exit 0 () --------------------

def test_wrapper_exits_4_naming_a_stage_that_was_dispatched_and_never_returned(tmp_path):
    """rc=0 over a stage that never returned is how a half-run reads as success.

    The check is evaluated from the program's OWN stage returns (a marker it
    writes at dispatch and clears after the `done` crumb), never from captured
    output: measured on cc-04 2026-09-13, six runs of `--apply > LOG` all showed a
    log cut mid-stage while the meter proved every one COMPLETED, so an
    output-side check would have refused six healthy runs (guard-6058).

    The stub drives the REAL `_mark_in_flight`, so a renamed env var or a changed
    marker format breaks this test instead of letting the wrapper and the .py
    silently disagree about what "in flight" means.
    """
    from _bash_helpers import BASH  # guard-580: never a bare "bash" argv[0]

    wrapper = tmp_path / "iteration-open.sh"
    wrapper.write_bytes((SCRIPTS / "iteration-open.sh").read_bytes())
    stub = tmp_path / "iteration-open.py"
    stage = io_mod.STAGES[-1]["key"]
    prelude = (
        "import importlib.util, os, sys\n"
        f"spec = importlib.util.spec_from_file_location('io', {(SCRIPTS / 'iteration-open.py').as_posix()!r})\n"
        "io = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(io)\n"
        "print('STAGE  rc  elapsed  note')\n"
        "sys.stdout.flush()\n"
    )

    def run_stub(body):
        stub.write_text(body, encoding="utf-8")
        return subprocess.run(
            [BASH, wrapper.as_posix(), "--apply"],
            capture_output=True, text=True, timeout=120,
            # : redirect the report path — see _stub_wrapper's docstring.
            env=dict(os.environ,
                     ITERATION_OPEN_REPORT_PATH=(tmp_path / "report.log").as_posix()),
        )

    # os._exit: an abrupt death with no cleanup, as a kill would leave it.
    died = run_stub(prelude + f"io._mark_in_flight({stage!r})\nos._exit(137)\n")
    assert died.returncode == 4, died.stdout + died.stderr
    assert "STAGE UNFINISHED" in died.stdout
    assert f"'{stage}'" in died.stdout, "the refusal must NAME the stage that never returned"
    assert "BLIND" in died.stdout, "must reach the fallback wording the precheck SKILL disposes"

    # NEGATIVE CONTROL 1 -- the same stage dispatched AND returned is the normal
    # fail-open 0. Without it the half above would pass against a wrapper that
    # exited 4 unconditionally (guard-3534).
    ok = run_stub(prelude + f"io._mark_in_flight({stage!r})\nio._mark_in_flight('')\n")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "STAGE UNFINISHED" not in ok.stdout

    # NEGATIVE CONTROL 2 -- a death before ANY stage was dispatched keeps the
    # existing wrapper_failed contract; this change widens only the case it names.
    early = run_stub("import sys\nsys.exit(1)\n")
    assert early.returncode == 0
    assert "wrapper_failed" in early.stdout
    assert "STAGE UNFINISHED" not in early.stdout


def test_the_marker_names_each_stage_only_while_it_is_in_flight(table, monkeypatch, tmp_path):
    """Pins the .py half against the REAL stage registry, selection included."""
    marker = tmp_path / "in-flight"
    marker.write_text("", encoding="utf-8")
    monkeypatch.setenv(io_mod._STAGE_MARKER_ENV, str(marker))
    expected = {s["script"]: s["key"] for s in io_mod.STAGES}
    expected["goal-selector.sh"] = "selection"
    seen = {}
    base = make_runner({})

    def runner(argv, timeout):
        if argv[0] in expected:
            seen[argv[0]] = marker.read_text(encoding="utf-8")
        return base(argv, timeout)

    io_mod.run(as_json=True, runner=runner, md_path=table)
    for script, key in expected.items():
        assert seen.get(script) == key, (
            f"{script} ran while the marker read {seen.get(script)!r}, not {key!r}")
    assert marker.read_text(encoding="utf-8") == "", "a finished run must leave nothing in flight"


class _Killed(BaseException):
    """Not an Exception, so main()'s fail-open handler cannot absorb it -- like a signal."""


@pytest.mark.parametrize("death", [_Killed, RuntimeError])
def test_a_death_mid_stage_leaves_that_stage_in_the_marker(table, monkeypatch, tmp_path, death):
    """RuntimeError is the case main() used to turn into rc=0: it catches the
    exception, prints a report and returns 0, so only the marker still says which
    stage never came back."""
    marker = tmp_path / "in-flight"
    monkeypatch.setenv(io_mod._STAGE_MARKER_ENV, str(marker))
    dying = io_mod.STAGES[-1]
    base = make_runner({})

    def runner(argv, timeout):
        if argv[0] == dying["script"]:
            raise death("stage died mid-run")
        return base(argv, timeout)

    with pytest.raises(death):
        io_mod.run(runner=runner, md_path=table)
    assert marker.read_text(encoding="utf-8") == dying["key"]


def test_an_unwritable_marker_says_the_check_is_blind(table, monkeypatch, tmp_path, capsys):
    """A marker that cannot be written must not read as "nothing was in flight"."""
    monkeypatch.setenv(io_mod._STAGE_MARKER_ENV, str(tmp_path / "no-such-dir" / "in-flight"))
    assert io_mod.run(runner=make_runner({}), md_path=table) == 0, "still fail-open"
    assert "unfinished-stage check is BLIND" in capsys.readouterr().err

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


# --- : a BLIND selection is not an empty one -----------------------
#
# The selector runs OUTSIDE the STAGES loop, so nothing routed a failed
# selection into report["blind"], and `_selection()`'s deliberate `count: None`
# (guard-1091 -- a failed measurement is not a measurement of zero) was rendered
# by "%s candidate(s)" as the literal word None. MEASURED 2026-09-09 (foxtrot,
# LAPTOP-3IOFCNEO) BEFORE the fix, on a real 180s rc=124:
#     SELECTION: None candidate(s); top: (none)
#     completeness = complete   status = clean   blind = []
# The stage row's rc=1 and the stderr line were the only signals, and no caller
# parses either. Two pins below and they are a PAIR: the second is the positive
# control guard-4166 requires, because the fix's effect is that something STOPS
# being printed -- a mutation that reverts the fix must flip the first WITHOUT
# flipping the second, or the pin is just asserting that selection prints.

def test_a_timed_out_selector_reads_as_blind_not_as_zero_candidates(table, capsys):
    runner = make_runner({"goal-selector.sh": (124, "", "goal-selector.sh: timeout after 110s")})
    io_mod.run(runner=runner, md_path=table)
    out = capsys.readouterr().out
    assert "SELECTION: BLIND" in out, out
    assert "timeout after 110s" in out, "the blind line must name WHY it is blind"
    assert "None candidate(s)" not in out, (
        "the exact measured regression: a stage that never returned rendered as "
        "a candidate COUNT"
    )
    assert "UNMEASURED, not " in out, "the reader must be told not to act on it"


def test_a_blind_selector_makes_the_whole_entry_partial(table, capsys):
    """guard-4093 in the file's own words: ANY blind -> partial. The selector is
    the most decisive stage of the run, so a run that could not read it is the
    LAST one that may report completeness=complete."""
    runner = make_runner({"goal-selector.sh": (124, "", "goal-selector.sh: timeout after 110s")})
    io_mod.run(as_json=True, runner=runner, md_path=table)
    d = json.loads(capsys.readouterr().out)
    assert d["completeness"] == "partial", d["completeness"]
    assert any(b["stage"] == "selection" for b in d["blind"]), d["blind"]
    assert d["candidates"]["count"] is None, "a failed read is not a measured zero"


def test_a_measured_zero_still_prints_its_count(table, capsys):
    """POSITIVE CONTROL for the two pins above (guard-4166).

    The selector RAN and legitimately found nothing. That is a measurement, and
    it must keep rendering as `0 candidate(s)` on a COMPLETE run -- folding it
    into the blind branch would trade one unreadable verdict for another.
    """
    runner = make_runner({"goal-selector.sh": (0, "[]", None)})
    io_mod.run(runner=runner, md_path=table)
    out = capsys.readouterr().out
    assert "SELECTION: 0 candidate(s)" in out, out
    assert "SELECTION: BLIND" not in out, "a measured zero is not blindness"

    io_mod.run(as_json=True, runner=make_runner({"goal-selector.sh": (0, "[]", None)}),
               md_path=table)
    d = json.loads(capsys.readouterr().out)
    assert d["completeness"] == "complete"
    assert not any(b["stage"] == "selection" for b in d["blind"])


# --- a selector that RAN AND FAILED must say why (2026-09-20) -----------------
#
# _run_bash captured stderr and threw it away, and _selection never looked at rc.
# So a selector that exited non-zero -- stdout empty by the wrapper's contract --
# fell through to json.loads and reached the model as "unparseable selector
# output: Expecting value: line 1 column 1 (char 0)": true of the bytes, silent on
# the cause. Measured 2026-09-19 on a served loop in a never-initialized world:
# the selector failed 9 of 9 and its reason never crossed this boundary. Same
# defect, same fix shape as precheck-medium-battery._run_bash (guard-2586).

_META_NOT_READY = (
    "[goal-selector] FATAL (rc=10): meta tier NOT READY -- NEVER INITIALIZED.\n"
    "  REMEDY: run boot Phase -2 -- `bash core/scripts/init-mind.sh testagent`."
)


def test_a_failed_selector_reports_its_rc_and_its_own_reason(table, capsys):
    def runner(argv, timeout):
        if argv[0] == "goal-selector.sh":
            return 10, "", 5, None, _META_NOT_READY      # the 5-slot production shape
        return 0, json.dumps({"findings": [], "blind": []}), 5, None, ""

    io_mod.run(as_json=True, runner=runner, md_path=table)
    d = json.loads(capsys.readouterr().out)
    err = d["candidates"]["error"]
    assert d["candidates"]["count"] is None, "a failed read is not a measured zero"
    assert "rc=10" in err
    assert "NEVER INITIALIZED" in err and "init-mind.sh" in err, (
        "the selector's state AND its remedy must both cross the boundary")
    assert "unparseable" not in err, (
        "naming the JSON parser instead of the cause is the defect being fixed")


def test_a_historical_4_slot_runner_still_works_when_the_selector_fails(table, capsys):
    """Every injected runner in this file returns 4 slots. The 5th must be
    OPTIONAL or the fix breaks the doubles it shares a contract with."""
    runner = make_runner({"goal-selector.sh": (3, "", None)})   # ran, failed, no stderr slot
    io_mod.run(as_json=True, runner=runner, md_path=table)
    d = json.loads(capsys.readouterr().out)
    assert "rc=3" in d["candidates"]["error"]
    assert d["candidates"]["count"] is None


def test_a_selector_that_succeeds_is_untouched_by_the_rc_branch(table, capsys):
    """POSITIVE CONTROL (guard-4166): the new branch keys on rc != 0 only. A
    ranking on rc=0 must still be read as a ranking."""
    rows = [{"goal_id": "g-001-01", "score": 4.2, "title": "t"}]

    def runner(argv, timeout):
        if argv[0] == "goal-selector.sh":
            return 0, json.dumps(rows), 5, None, "a banner on stderr is not a failure"
        return 0, json.dumps({"findings": [], "blind": []}), 5, None, ""

    io_mod.run(as_json=True, runner=runner, md_path=table)
    d = json.loads(capsys.readouterr().out)
    assert d["candidates"]["count"] == 1
    assert "error" not in d["candidates"] or not d["candidates"]["error"]


def test_the_real_runner_carries_the_real_selectors_remedy(tmp_path, monkeypatch):
    """THE SEAM ITSELF, no doubles: the real _run_bash, the real wrapper, the real
    selector, pointed at a meta dir that was never initialized. This is the path
    that was measured broken, so it is the one worth pinning end to end."""
    meta = tmp_path / "meta"
    meta.mkdir()
    monkeypatch.setenv("MIND_META", str(meta))
    monkeypatch.setenv("MIND_AGENT", "testagent")
    monkeypatch.setenv("STORAGE_BACKEND", "local")   # guard-955

    res = io_mod._run_bash(["goal-selector.sh"], 120)
    assert len(res) == 5, "the runner contract is 5 slots; the 5th is stage stderr"
    assert res[0] == 10 and res[3] is None, res[:4]
    assert "REMEDY" in res[4]

    sel = io_mod._selection(io_mod._run_bash)
    assert "rc=10" in sel["error"] and "init-mind.sh testagent" in sel["error"]


# --- the gap cannot silently reopen () ----------------------------

# Names whose value is a dict BUILT with the override, forwarded as `env=<name>`.
# Keep this list SHORT: every entry is a promise the pin stops verifying and
# starts trusting, so add one only when the builder itself is pinned elsewhere.
# `env` is _stub_wrapper's return (its body sets ITERATION_OPEN_REPORT_PATH, and
# test_report_is_persisted_off_the_synced_tree_for_the_caller_to_read fails if it
# ever stops); `_env` is the local built inline two tests above.
_ENV_HELPERS = {"env", "_env"}


def test_every_wrapper_invocation_in_this_file_redirects_the_report_path():
    """THE PIN. Fixing the four unguarded call sites fixed the instances; this
    fixes the CLASS, for this file.

    `_stub_wrapper` already existed when the defect shipped, and its docstring
    already stated the hazard verbatim — it had simply been applied to the five
    tests written beside it and to none of the four older ones. A helper that
    documents a hazard reads as coverage, so the next author to add a
    `subprocess.run` here will copy whichever neighbour they happen to see and
    the file silently regresses (rb-11398). A helper cannot enforce its own use;
    an assertion over the file can.

    Measured pre-fix: a run of this file took the live
    /tmp/iteration-open-report-<agent>.log from 3,271 B to 0 B, at pytest exit 0
    with every test PASSING, because the tests assert on stdout and never on the
    report file.
    """
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))

    def invokes_the_wrapper(call):
        """First positional arg is an argv list whose argv[0] is BASH."""
        if not call.args or not isinstance(call.args[0], (ast.List, ast.Tuple)):
            return False
        head = call.args[0].elts[0] if call.args[0].elts else None
        return isinstance(head, ast.Name) and head.id == "BASH"

    checked, offenders = [], []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
        if name not in ("run", "Popen", "check_output", "check_call"):
            continue
        if not invokes_the_wrapper(node):
            continue          # in-process io_mod.run() never reaches the shell copy
        checked.append(node.lineno)
        # The KEYWORD is not the contract — the OVERRIDE is. `env=os.environ`
        # satisfies "passes env=" and still destroys the live report, so assert
        # the variable name appears in the argument that is actually passed
        # (directly, or via a helper-built dict the call forwards by name).
        env_src = " ".join(ast.unparse(kw.value) for kw in node.keywords
                           if kw.arg == "env")
        if "ITERATION_OPEN_REPORT_PATH" not in env_src and env_src.strip() not in _ENV_HELPERS:
            offenders.append(node.lineno)

    # POSITIVE CONTROL (guard-3130/guard-3534): a predicate that matches NOTHING
    # passes this test forever while covering nothing. State the population.
    assert len(checked) >= 10, (
        f"the predicate found only {len(checked)} wrapper invocation(s) — it has "
        "drifted away from the call shape it is supposed to audit, so its silence "
        "proves nothing"
    )
    assert offenders == [], (
        f"{len(offenders)} of {len(checked)} wrapper invocations pass no env= at "
        f"lines {offenders}. Every one MUST set ITERATION_OPEN_REPORT_PATH: the "
        "wrapper's default is keyed by agent name alone, so an unredirected call "
        "destroys the live report of whatever agent runs this suite. Route through "
        "_stub_wrapper, or pass env=dict(os.environ, "
        "ITERATION_OPEN_REPORT_PATH=(tmp_path / 'report.log').as_posix())."
    )


def test_selection_line_labels_a_hoist_and_leaves_a_true_argmax_unmarked():
    """index 0 may be a deliberate HOIST, and the SELECTION line must say so.

    goal-selector.py promotes ONE goal to index 0 from three lanes, each stamping its
    own marker key (guard-5135; rationale/selector-index-0-is-a-hoist.md). This line
    rendered only goal_id/score, so a hoist and a true argmax came out BYTE-IDENTICAL
    -- and the only apparent way to tell them apart was to run the selector repeatedly,
    which is invalid: goal-selector.sh is NOT a pure read (guard-2331), so each sample
    mutates the very drain-lane counter (invocations_since_pick vs k) that rate-limits
    the hoist. Measured 2026-09-21 (zeta, cc-02): six iterations of sampling produced
    two confident false signals -- a strict alternation and a caller correlation --
    before the marker, present on the FIRST draw all along, settled it (g-115-10466).

    BOTH directions are asserted on purpose. A renderer that appended the label
    unconditionally would satisfy the hoist case alone, so the unmarked case is what
    gives this test discriminating power (rb-5828).
    """
    def runner_for(cands):
        return lambda argv, timeout: (0, json.dumps(cands), 5, None, "")

    hoisted = io_mod._selection(runner_for([
        {"goal_id": "g-001-10", "score": 9.56, "title": "Generate hypotheses",
         "drain_lane_pick": True},
        {"goal_id": "g-326-188", "score": 19.38, "title": "Load test"},
    ]))["top"]
    assert "[HOIST: drain_lane_pick]" in hoisted, hoisted
    assert hoisted.startswith("g-001-10 (9.56)"), hoisted

    argmax = io_mod._selection(runner_for([
        {"goal_id": "g-326-188", "score": 20.03, "title": "Load test"},
        {"goal_id": "g-115-817", "score": 18.88, "title": "Sweep inbox"},
    ]))["top"]
    assert "HOIST" not in argmax, argmax
    assert argmax == "g-326-188 (20.03) Load test", argmax

    for marker in ("strategic_focus_pick", "reducer_only_pick"):
        line = io_mod._selection(runner_for([
            {"goal_id": "g-373-45", "score": 15.31, "title": "x", marker: True},
            {"goal_id": "g-326-899", "score": 17.15, "title": "y"},
        ]))["top"]
        assert "[HOIST: %s]" % marker in line, (marker, line)
