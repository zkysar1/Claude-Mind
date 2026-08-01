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
    monkeypatch.setattr(RFS, "_owning_goals", lambda p, r: [])
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
    monkeypatch.setattr(RFS, "_owning_goals", lambda p, r: [])
    RFS.triage(tmp_path, tmp_path, {})
    out = capsys.readouterr().out
    assert "no candidate reproduced solo" in out
    assert "already has an owning goal" not in out


def test_red_solo_unowned_is_filed(tmp_path, monkeypatch, capsys):
    """Reproduces solo AND no goal names it -> the one actionable bucket."""
    target = _log_with_failure(tmp_path)
    monkeypatch.setattr(RFS, "_solo", lambda p, r, e: (10, 2, None))
    monkeypatch.setattr(RFS, "_owning_goals", lambda p, r: [])
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
        lambda p, r: [("g-115-3803", "pending", "Investigate: fleet_config_parity")])
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
        owners = RFS._owning_goals(
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
        owners = RFS._owning_goals("core/scripts/tests/test_thing.py", ".")
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
