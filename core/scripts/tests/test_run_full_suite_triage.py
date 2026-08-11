"""Regression pins for run-full-suite.py --triage ( / gap-053).

The run half of run-full-suite has always printed a verdict; nothing covered
what to do when that verdict is not CLEAN, so the triage chain was re-derived
by hand every time it was needed (gap-053: twice in two days, a full iteration
each). `--triage` mechanises it: position bucket -> solo discriminate ->
ownership -> file only what survives.

THE LOAD-BEARING PIN IS `_stem_forms`. Measured 2026-07-31 (echo, cc-03)
against a known-answer case:

    aspirations-query.sh --title-contains "test_fleet_config_parity"  -> 0 hits
    aspirations-query.sh --title-contains "fleet_config_parity"       -> 3 hits
                                            (incl. g-115-3803, status=pending)

`--title-contains` is a substring match on the TITLE ONLY, and goal titles
routinely drop the `test_` prefix. So an ownership check keyed on the failing
file's stem alone reports UNOWNED for a test that two open goals track, and the
caller files a duplicate -- the exact inversion of what the step exists to
prevent, arrived at silently. `--goal-field description <name>` is not a
substitute: it is an EXACT field match (it returns 0 on g-115-3803, whose
description provably contains the string, while `--goal-field status pending`
returns 915 -- so the flag works, it just does not mean "contains").

The solo/ownership tests stub `_solo` and `_owning_goals`. That is deliberate:
the real versions spawn pytest and query the live aspirations queues, so a
behavioural test of them would be slow, non-hermetic, and would change its
answer as the queue changes. What must not regress is the ROUTING between their
results and the four outcome buckets, which is pure logic and is what these pin.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
TARGET = SCRIPT_DIR.parent / "run-full-suite.py"
WRAPPER = SCRIPT_DIR.parent / "run-full-suite.sh"


def _load():
    spec = importlib.util.spec_from_file_location("run_full_suite_triage", TARGET)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_full_suite_triage"] = mod
    spec.loader.exec_module(mod)
    return mod


RFS = _load()


# ── _stem_forms: the measured defect ────────────────────────────────────────

def test_stem_forms_yields_both_prefixed_and_stripped():
    """A test_-prefixed file must be looked up BOTH ways.

    Stripping is what finds the owner: the live case that motivated this
    (test_fleet_config_parity -> g-115-3803) is reachable only via the
    stripped form.
    """
    forms = RFS._stem_forms("core/scripts/tests/test_fleet_config_parity.py")
    assert "test_fleet_config_parity" in forms
    assert "fleet_config_parity" in forms


def test_stem_forms_strips_only_the_leading_prefix():
    """Only the LEADING `test_` comes off, and only once."""
    forms = RFS._stem_forms("x/test_test_thing.py")
    assert forms == ["test_test_thing", "test_thing"]


def test_stem_forms_leaves_unprefixed_names_alone():
    """A file with no `test_` prefix yields exactly one form -- no empty string.

    An empty query form would substring-match EVERY goal title, turning the
    ownership check into "everything is owned" and suppressing all filing.
    """
    forms = RFS._stem_forms("core/scripts/tests/check_thing.py")
    assert forms == ["check_thing"]
    assert "" not in forms


# ── triage: log-level outcomes ──────────────────────────────────────────────

def test_triage_with_no_logs_is_setup_error(tmp_path):
    """rc=3, not 0. No logs means the measurement is absent, not clean."""
    assert RFS.triage(tmp_path, tmp_path, {}) == 3


def test_triage_contended_with_zero_failures_returns_invalid(tmp_path):
    """The most deceptive shape: every chunk line reads 0 failed, TOTAL looks
    like a pass, and only completeness is wrong. Must return 2 (re-measure),
    never 0, and must not present an empty candidate table as all-clear."""
    (tmp_path / "chunk-00.log").write_text("...... [ 45%]\n", encoding="utf-8")
    (tmp_path / "chunk-01.log").write_text(
        "...... [100%]\n6 passed in 1.0s\n", encoding="utf-8")
    assert RFS.triage(tmp_path, tmp_path, {}) == 2


def test_triage_clean_logs_with_no_failures_returns_zero(tmp_path):
    (tmp_path / "chunk-00.log").write_text(
        "...... [100%]\n6 passed in 1.0s\n", encoding="utf-8")
    assert RFS.triage(tmp_path, tmp_path, {}) == 0


# ── triage: the four candidate buckets ──────────────────────────────────────

def _log_with_failure(tmp_path, target="core/scripts/tests/test_thing.py"):
    (tmp_path / "chunk-00.log").write_text(
        ".....F [100%]\nFAILED " + target + "::test_x\n5 passed, 1 failed in 1.0s\n",
        encoding="utf-8")
    return target


def test_green_solo_is_environmental_and_files_nothing(tmp_path, monkeypatch, capsys):
    """Green solo falsifies contention in one measurement -> do not file."""
    _log_with_failure(tmp_path)
    monkeypatch.setattr(RFS, "_solo", lambda p, r, e: (32, 0, None))
    monkeypatch.setattr(RFS, "_owning_goals", lambda p, r: ([], 915))
    rc = RFS.triage(tmp_path, tmp_path, {})
    out = capsys.readouterr().out
    assert rc == 0
    assert "ENVIRONMENTAL" in out
    assert "FILE THESE" not in out


def test_all_environmental_does_not_claim_reds_are_owned(tmp_path, monkeypatch, capsys):
    """A fully-environmental run must NOT report 'every genuine red already has
    an owning goal' -- there were no genuine reds. Those are different findings
    and collapsing them reports a non-regression as a managed regression."""
    _log_with_failure(tmp_path)
    monkeypatch.setattr(RFS, "_solo", lambda p, r, e: (32, 0, None))
    monkeypatch.setattr(RFS, "_owning_goals", lambda p, r: ([], 915))
    RFS.triage(tmp_path, tmp_path, {})
    out = capsys.readouterr().out
    assert "no candidate reproduced solo" in out
    assert "already has an owning goal" not in out


def test_red_solo_unowned_is_filed(tmp_path, monkeypatch, capsys):
    """Reproduces solo AND no goal names it -> the one actionable bucket."""
    target = _log_with_failure(tmp_path)
    monkeypatch.setattr(RFS, "_solo", lambda p, r, e: (10, 2, None))
    monkeypatch.setattr(RFS, "_owning_goals", lambda p, r: ([], 915))
    rc = RFS.triage(tmp_path, tmp_path, {})
    out = capsys.readouterr().out
    assert rc == 1
    assert "FILE THESE" in out
    assert target in out
    assert "owner: NONE" in out


def test_red_solo_owned_is_not_filed(tmp_path, monkeypatch, capsys):
    """Genuine but already tracked -> report the owner, file nothing.

    This is the bucket the whole `_stem_forms` fix serves: without the stripped
    form this case would be mis-sorted into FILE THESE.
    """
    _log_with_failure(tmp_path)
    monkeypatch.setattr(RFS, "_solo", lambda p, r, e: (10, 2, None))
    monkeypatch.setattr(
        RFS, "_owning_goals",
        lambda p, r: ([("g-115-3803", "pending", "Investigate: fleet_config_parity",
                        "exact")], 915))
    rc = RFS.triage(tmp_path, tmp_path, {})
    out = capsys.readouterr().out
    assert rc == 0
    assert "FILE THESE" not in out
    assert "g-115-3803" in out


def test_solo_that_cannot_run_is_unclassified_not_clean(tmp_path, monkeypatch, capsys):
    """A candidate whose solo re-run errored must not be silently dropped into
    'environmental'. Unmeasured is not clean -- it exits non-zero and says so."""
    _log_with_failure(tmp_path)
    monkeypatch.setattr(RFS, "_solo", lambda p, r, e: (None, None, "boom"))
    rc = RFS.triage(tmp_path, tmp_path, {})
    out = capsys.readouterr().out
    assert rc == 1
    assert "could not be re-run" in out
    assert "ENVIRONMENTAL" not in out


# ── wrapper: --triage must not fire the other two halves ────────────────────

def test_wrapper_short_circuits_triage_before_other_suites():
    """--triage re-reads logs; it runs no tests. The wrapper must exit before
    the invisible-suite and domain halves, which are full test runs whose exit
    codes would otherwise be folded into a chunk-log verdict.

    STATIC pin: a behavioural test would have to execute the real suites, which
    is precisely what this asserts does not happen.
    """
    text = WRAPPER.read_text(encoding="utf-8")
    i_triage = text.index('"--triage" ]')
    # Anchor on the CALL SITES, not the first mention. Both scripts are named in
    # explanatory comments ABOVE the python3 invocation, so a bare
    # .index("run-invisible-suites.sh") matches a comment at char 2972 and the
    # assertion inverts -- which is how this pin failed on its first run.
    i_invisible = text.index('bash "$SCRIPT_DIR/tests/run-invisible-suites.sh"')
    i_domain = text.index('bash "$WORLD_PATH/scripts/run-domain-tests.sh"')
    assert i_triage < i_invisible, "--triage guard must precede the invisible half"
    assert i_triage < i_domain, "--triage guard must precede the domain half"


# ── ownership must search DESCRIPTIONS, not titles alone ────────────────────

def _stub_query(monkeypatch, rows):
    """Stand in for `aspirations-query.sh --goal-status <s> --full`."""
    class _R:
        returncode = 0
        stdout = json.dumps(rows)
    monkeypatch.setattr(RFS.subprocess, "run", lambda *a, **k: _R())


def test_ownership_finds_a_goal_that_names_the_test_only_in_its_description():
    """The near-duplicate this feature caused on its first live use.

    g-115-4310 is pending and its DESCRIPTION names both failing test files; its
    TITLE names the DEFECT ("merge_backpressure breaks two pins ..."). A
    title-only search returned "owner: NONE" for two genuinely-owned reds, and
    the tool built to prevent duplicate filings was one step from causing one.

    A good goal title describes the defect, so this is the COMMON case: the
    better the title, the less likely it contains a test filename.
    """
    mp = pytest.MonkeyPatch()
    try:
        _stub_query(mp, [{
            "goal_id": "g-115-4310",
            "status": "pending",
            "title": "Fix: merge_backpressure breaks two pins -- not byte-commutative",
            "description": ("both pins red: "
                            "core/scripts/tests/test_merge_handlers_commutativity_property.py "
                            "and test_meta_write_class_conflict_retry.py"),
        }])
        owners, scanned = RFS._owning_goals(
            "core/scripts/tests/test_merge_handlers_commutativity_property.py", ".")
    finally:
        mp.undo()
    assert [o[0] for o in owners] == ["g-115-4310"]


def test_ownership_ignores_goals_that_do_not_name_the_test():
    """Substring scan must not match everything -- an over-broad owner check
    suppresses ALL filing, which fails in the silent direction."""
    mp = pytest.MonkeyPatch()
    try:
        _stub_query(mp, [{
            "goal_id": "g-999-99", "status": "pending",
            "title": "Unrelated work", "description": "nothing to do with tests",
        }])
        owners, scanned = RFS._owning_goals("core/scripts/tests/test_thing.py", ".")
    finally:
        mp.undo()
    assert owners == []


def test_ownership_queries_only_open_statuses():
    """A COMPLETED goal naming the test is not an owner -- it means the test was
    fixed and has REGRESSED, which is a thing to file, not to suppress."""
    assert RFS.OPEN_STATUSES == ("pending", "in-progress")
    assert "completed" not in RFS.OPEN_STATUSES


# ── a run must leave exactly ONE run's logs behind ──────────────────────────

def test_run_clears_stale_chunk_logs_from_a_prior_run(tmp_path, monkeypatch):
    """A run at a lower --chunks count must not leave a higher-numbered chunk
    behind for --triage to read.

    MEASURED on this feature's first live use (2026-07-31, echo, cc-03): a
    16-chunk run left chunk-16..19 from a 20-chunk run 7.5h earlier, and
    --triage then read 20 logs for a 16-chunk run. Those four were clean so the
    verdict held by luck; a stale FAILED line injects a phantom candidate, and a
    stale INCOMPLETE chunk makes classify() call a healthy run contended.
    """
    fake_tests = tmp_path / "tests"
    fake_tests.mkdir()
    (fake_tests / "test_x.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    stale = out / "chunk-19.log"
    stale.write_text("FAILED some/old_test.py::test_gone\n", encoding="utf-8")

    monkeypatch.setattr(RFS, "TESTS_DIR", fake_tests)
    monkeypatch.setattr(RFS.subprocess, "run", lambda *a, **k: None)

    RFS.main(["--out", str(out), "--chunks", "1"])

    assert not stale.exists(), "stale chunk-19.log from a prior run was not cleared"
    assert (out / "chunk-00.log").exists(), "this run's chunk-00.log should exist"


# ── F1: a solo run that executed NOTHING is not a green ─────────────────────
#
# Found by /fresh-eyes-code on the code  shipped (echo, cc-03), each
# defect below re-measured independently 2026-08-01 (zeta, hostname cc-02,
# uname -r 6.8.0-136-generic) before any fix was written.

class _FakeRun:
    def __init__(self, returncode, stdout=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def _stub_pytest(monkeypatch, returncode, stdout=""):
    monkeypatch.setattr(RFS.subprocess, "run",
                        lambda *a, **k: _FakeRun(returncode, stdout))


@pytest.mark.parametrize("rc,stdout,label", [
    (5, "no tests ran in 0.01s\n", "collected nothing (pytest rc=5)"),
    (4, "ERROR: file or directory not found\n", "usage error (rc=4)"),
    (2, "", "interrupted (rc=2)"),
    (127, "bash: pytest: command not found\n", "interpreter never started"),
    (0, "", "rc=0 but the log accounts for no tests at all"),
])
def test_solo_that_executed_no_tests_is_not_a_green(monkeypatch, rc, stdout, label):
    """`_parse_counts` maps EVERY one of these to (0, 0, 0).

    That is byte-identical to a clean pass, so before this pin `_solo` returned
    f == 0 and the caller printed "-> ENVIRONMENTAL (do not file)" -- silently
    discarding a real red. Measured: `_parse_counts("")`,
    `_parse_counts("bash: pytest: command not found")` and
    `_parse_counts("no tests ran in 0.01s")` all return (0, 0, 0).

    guard-2166 in the small: the empty population must return the UNSAFE
    verdict. Remove the completeness branch in `_solo` and every case here
    reverts to (0, 0, None), which reads as green.
    """
    _stub_pytest(monkeypatch, rc, stdout)
    p, f, err = RFS._solo("core/scripts/tests/test_thing.py", ".", {})
    assert err is not None, "%s was reported as a measurement" % label
    assert (p, f) == (None, None)


@pytest.mark.parametrize("rc,stdout", [
    (0, "32 passed in 4.0s\n"),
    (1, "10 passed, 2 failed in 4.0s\n"),
])
def test_solo_still_measures_when_tests_actually_ran(monkeypatch, rc, stdout):
    """The other half of the fix: a real run must NOT be refused.

    Without this, tightening `_solo` could pass the pin above by rejecting
    everything -- which would route every candidate to unclassified and make the
    whole triage useless.
    """
    _stub_pytest(monkeypatch, rc, stdout)
    p, f, err = RFS._solo("core/scripts/tests/test_thing.py", ".", {})
    assert err is None
    assert p is not None and f is not None


def test_zero_test_solo_routes_to_unclassified_not_environmental(
        tmp_path, monkeypatch, capsys):
    """End-to-end: the non-measurement must not exit 0 claiming nothing to file."""
    _log_with_failure(tmp_path)
    monkeypatch.setattr(RFS.subprocess, "run",
                        lambda *a, **k: _FakeRun(5, "no tests ran in 0.01s\n"))
    rc = RFS.triage(tmp_path, tmp_path, {})
    out = capsys.readouterr().out
    assert rc == 1
    assert "ENVIRONMENTAL" not in out
    assert "COULD NOT RUN" in out


# ── F2: an unanswered ownership query is UNKNOWN, never UNOWNED ─────────────

def test_ownership_query_failure_is_not_a_true_negative(monkeypatch, capsys):
    """Both failure paths in `_owning_goals` fall through to an empty list.

    Measured: with `subprocess.run` stubbed to rc=1 -- the routine
    daemon-unreachable shape, and no-python-cli-fallback.md means there is NO
    CLI fallback beneath it -- `_owning_goals` returns []. Rendered as
    "owner: NONE", that is byte-identical to a true negative and authorises a
    duplicate filing. rb-245: verify the instrument answered before believing
    its zero.
    """
    monkeypatch.setattr(RFS.subprocess, "run", lambda *a, **k: _FakeRun(1, ""))
    owners, scanned = RFS._owning_goals("core/scripts/tests/test_thing.py", ".")
    assert (owners, scanned) == ([], 0)

    result = RFS._print_ownership("core/scripts/tests/test_thing.py", ".")
    out = capsys.readouterr().out
    assert result is None, "instrument failure must not present as 'no owner'"
    assert "UNKNOWN" in out and "instrument failure" in out
    assert "owner: NONE" not in out


def test_unanswered_ownership_keeps_the_exit_code_nonzero(
        tmp_path, monkeypatch, capsys):
    """A genuine red whose ownership is UNKNOWN is unclassified, not clean.

    It must not land in genuine_unowned either -- that would file a goal on the
    strength of a query that never ran.
    """
    _log_with_failure(tmp_path)
    monkeypatch.setattr(RFS, "_solo", lambda p, r, e: (10, 2, None))
    monkeypatch.setattr(RFS, "_owning_goals", lambda p, r: ([], 0))
    rc = RFS.triage(tmp_path, tmp_path, {})
    out = capsys.readouterr().out
    assert rc == 1
    assert "UNANSWERED ownership query" in out
    assert "FILE THESE" not in out


def test_true_negative_still_reports_unowned_and_files(monkeypatch, capsys):
    """The instrument ANSWERED and found nothing -- that is real evidence.

    Pins that the F2 fix keys on 'did the query return rows', not on 'is the
    owner list empty'. Without this, a fix could call every empty result UNKNOWN
    and nothing would ever be filed again.
    """
    monkeypatch.setattr(
        RFS.subprocess, "run",
        lambda *a, **k: _FakeRun(0, json.dumps(
            [{"goal_id": "g-999-99", "status": "pending",
              "title": "Unrelated", "description": "nothing to do with it"}])))
    result = RFS._print_ownership("core/scripts/tests/test_thing.py", ".")
    out = capsys.readouterr().out
    assert result == []
    assert "owner: NONE" in out and "open goal(s) scanned" in out


# ── F3: a shared subsystem name is not ownership (guard-1801) ───────────────

_PARITY = "core/scripts/tests/test_fleet_config_parity.py"


def test_exact_test_file_match_wins_over_subsystem_name_matches(monkeypatch):
    """Measured live 2026-08-01: `_owning_goals` on this file matched TEN open
    goals; exactly one (g-115-3803) owns the failing tests. The rest merely
    discuss `fleet_config_parity` as a subsystem.

    `_stem_forms` widens the QUERY by stripping `test_` and `_owning_goals`
    widens the FIELD to the whole description; together they turn a filename
    lookup into a topic search. Over-match is the SILENT direction -- a spurious
    owner suppresses all filing and exits 0 printing "every genuine red already
    has an owning goal" (guard-1801: a shared file path is not ownership).
    """
    _stub_query(monkeypatch, [
        {"goal_id": "g-115-3803", "status": "pending",
         "title": "Investigate: fleet_config_parity CLI-lane collector exits 1",
         "description": "the failing pins live in test_fleet_config_parity.py"},
        {"goal_id": "g-115-3221", "status": "pending",
         "title": "Investigate: which config values resolve through >1 lane",
         "description": "context: fleet_config_parity covers the env key set"},
        {"goal_id": "g-115-3344", "status": "pending",
         "title": "Idea: fleet_config_parity checks KEY SET but not VALUE SHAPE",
         "description": "no test file named here"},
    ])
    owners, scanned = RFS._owning_goals(_PARITY, ".")
    assert [o[0] for o in owners] == ["g-115-3803"]
    assert {o[3] for o in owners} == {"exact"}
    assert scanned > 0


def test_stripped_form_is_a_fallback_and_is_labelled_weak(monkeypatch, capsys):
    """When NOTHING names the test file, the subsystem hits are still shown --
    losing g-115-4310 (owner only via its description) is the opposite failure
    and is pinned above. But they are labelled, because they are not ownership.
    """
    _stub_query(monkeypatch, [
        {"goal_id": "g-115-3221", "status": "pending",
         "title": "Investigate: config lanes",
         "description": "context: fleet_config_parity covers the env key set"},
    ])
    owners, _ = RFS._owning_goals(_PARITY, ".")
    assert [o[0] for o in owners] == ["g-115-3221"]
    assert {o[3] for o in owners} == {"weak"}

    RFS._print_ownership(_PARITY, ".")
    assert "WEAK match" in capsys.readouterr().out


# ── F4: never hand bash a str(WindowsPath) — repo-wide (guard-581) ──────────

PRODUCTION_SCRIPTS = sorted(p for p in SCRIPT_DIR.parent.glob("*.py"))


def test_no_production_script_passes_a_str_path_to_bash():
    """Repo-wide source pin. File-scoped copies of this cannot hold the class.

    `str(WindowsPath)` reaches bash with backslashes, which it treats as escape
    introducers and strips -- so the script path silently becomes nonexistent,
    the wrapper "fails", and the caller reports a confident wrong answer. It is
    invisible on Linux, where `str()` and `.as_posix()` are identical by
    definition, so a green suite on one OS is not evidence (guard-581).

    Measured 2026-08-01: 8 live sites across 7 files -- and the enforcer's own
    `check-no-bare-bash.py` fix hint prescribed exactly this shape as its
    `tests:` remedy, so an author fixing guard-580 was shown the guard-581
    defect by the gate, at the one moment they were looking for something to
    copy. That hint now prescribes `.as_posix()` on both lines.

    Comment lines are excluded so a file may still NAME the forbidden shape in
    prose -- this file and that hint both do.

    THE PREDICATE IS THE LITERAL `[BASH, str(`, not "`[BASH,` and `str(` on one
    line". The looser form is what the file-scoped ancestor of this pin used, and
    it was correct THERE only because that file's population was one file. Widened
    repo-wide it produced two false positives on the first run, both measured:
    `_runtime_bash.py:94` is `bash_cmd` ITSELF (`[BASH, Path(script).as_posix(),
    *(str(a) for a in args)]` -- the `str()` stringifies ARGUMENTS, which is the
    correct implementation), and `product-repo-freshness.py:213` is
    `[BASH, *argv] if str(argv[0]).endswith(".sh")` -- a type-safe suffix check
    whose single caller passes a forward-slash string literal, verified by reading
    it. A predicate that is right on a narrow population can be wrong once the
    population widens (guard-1802's shape, inverted).

    SO BE CLEAR WHAT THIS DOES NOT COVER: only the LITERAL idiom. A variable
    path -- `[BASH, some_path]`, `[BASH, *argv]` -- is invisible to it, and that
    shape is a real guard-581 exposure whenever the variable holds a Path. Do
    not read a green here as full guard-581 coverage. Probed 2026-08-01 (zeta):
    all 3 remaining bare-BASH production sites are safe by construction -- two
    are `[BASH, "-s"]` with the script on stdin (no path argument at all) and
    one is `[BASH, gh_bin()]`, where gh_bin() returns os.environ["GH_BIN"] or
    the literal "gh", never a Path. Zero live instances today; the gap is about
    tomorrow. Tightening the predicate to catch them would reintroduce the false
    positives above, so the honest boundary is a narrow pin plus this note.
    """
    offenders = []
    for path in PRODUCTION_SCRIPTS:
        for i, ln in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if "[BASH, str(" in ln and not ln.lstrip().startswith("#"):
                offenders.append("%s:%d %s" % (path.name, i, ln.strip()[:80]))
    assert not offenders, (
        "guard-581: pass wrapper paths through bash_cmd(...) or Path(...).as_posix(), "
        "never [BASH, str(path)]. Offending line(s): %r" % offenders)


def test_the_str_path_pin_is_not_vacuous():
    """guard-2166: an all()-shaped pin passes on an empty population.

    If the glob stops finding scripts, or every bash callsite is deleted, the
    pin above goes green having verified nothing. Assert the population exists
    and that the safe shape is actually in use.
    """
    assert len(PRODUCTION_SCRIPTS) > 100, (
        "production script glob returned %d files -- the pin above is scanning "
        "almost nothing" % len(PRODUCTION_SCRIPTS))
    users = [p.name for p in PRODUCTION_SCRIPTS
             if "bash_cmd(" in p.read_text(encoding="utf-8", errors="replace")]
    assert len(users) >= 5, (
        "only %d script(s) call bash_cmd() -- if wrapper invocation moved to a "
        "new idiom this pin is measuring a dead pattern: %r" % (len(users), users))


def test_the_enforcer_hint_does_not_prescribe_the_defect():
    """The root, not a symptom. `check-no-bare-bash.py` is read at the exact
    moment an author wants a shape to copy; a remedy there that satisfies
    guard-580 while violating guard-581 propagates the second defect under the
    authority of the first gate. That is the mechanism behind the 8 sites.
    """
    src = (SCRIPT_DIR.parent / "check-no-bare-bash.py").read_text(encoding="utf-8")
    hint = src[src.index("def _fix_hint"):]
    hint = hint[:hint.index("def main")]
    # Only the lines that SHOW a call shape. The hint also WARNS about str(Path)
    # in prose, and matching that too would forbid it from naming what to avoid.
    prescriptions = [ln for ln in hint.splitlines()
                     if not ln.lstrip().startswith("#")
                     and "subprocess.run(" in ln and "str(" in ln]
    assert not prescriptions, (
        "the fix hint prescribes a str(path) bash invocation: %r" % prescriptions)
    assert "as_posix" in hint, "the hint must show the safe script-path shape"
