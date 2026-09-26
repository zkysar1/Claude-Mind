"""Tests for core/scripts/run-scoped-suite.py — the impact-scoped verification tier.

The property under test that matters most is the TRI-STATE contract: an empty
selection must NEVER return PASS. Measured 2026-09-10 over 14 real source-touching
commits, 3 selected zero tests, so a runner that printed green on an empty
selection would have manufactured confidence on 21% of real changes (g-115-9602).

The module name is hyphenated, so it loads by file location rather than import.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = PROJECT_ROOT / "core" / "scripts" / "run-scoped-suite.py"
WRAPPER_PATH = PROJECT_ROOT / "core" / "scripts" / "run-scoped-suite.sh"


def _load():
    spec = importlib.util.spec_from_file_location("run_scoped_suite", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


def test_module_and_wrapper_exist():
    assert MODULE_PATH.is_file(), MODULE_PATH
    assert WRAPPER_PATH.is_file(), WRAPPER_PATH


def test_variants_normalises_underscore_dash_and_py_sh_siblings(mod):
    names, stem = mod._variants("core/scripts/dependency-timeout-check.py")
    assert stem == "dependency_timeout_check"
    # both the .sh sibling and the underscore form must be candidates, because
    # tests invoke wrappers by path string rather than importing them
    assert "dependency-timeout-check.sh" in names
    assert "dependency-timeout-check.py" in names
    assert "dependency_timeout_check.py" in names


def test_select_maps_a_source_file_to_a_test_that_names_it(mod):
    idx = {
        Path("t/test_alpha.py"): "subprocess.run(['bash', 'core/scripts/foo-bar.sh'])",
        Path("t/test_beta.py"): "nothing relevant here",
    }
    per_file, unmapped = mod.select(["core/scripts/foo-bar.py"], idx)
    assert per_file["core/scripts/foo-bar.py"] == {Path("t/test_alpha.py")}
    assert unmapped == []


def test_select_maps_by_module_import(mod):
    idx = {Path("t/test_gamma.py"): "from foo_bar import thing\n"}
    per_file, unmapped = mod.select(["core/scripts/foo_bar.py"], idx)
    assert per_file["core/scripts/foo_bar.py"] == {Path("t/test_gamma.py")}
    assert unmapped == []


def test_select_maps_by_test_filename_convention(mod):
    idx = {Path("t/test_foo_bar.py"): "unrelated body"}
    per_file, unmapped = mod.select(["core/scripts/foo-bar.py"], idx)
    assert per_file["core/scripts/foo-bar.py"] == {Path("t/test_foo_bar.py")}


def test_select_reports_unmapped_when_nothing_references_the_file(mod):
    """The load-bearing case: an unreferenced file must surface, not vanish."""
    idx = {Path("t/test_alpha.py"): "totally unrelated"}
    per_file, unmapped = mod.select(["core/scripts/never-referenced.py"], idx)
    assert per_file["core/scripts/never-referenced.py"] == set()
    assert unmapped == ["core/scripts/never-referenced.py"]


def test_discover_changed_filters_to_source_files(mod, monkeypatch):
    monkeypatch.setattr(mod, "_git", lambda args: (
        " M core/scripts/foo.py\n"
        "?? core/scripts/tests/test_foo.py\n"      # a test, excluded
        " M core/config/aspirations.yaml\n"        # not a source suffix
        " M docs/readme.md\n"                      # not a source prefix
        " M mind_api/src/bar.py\n"
    ))
    got = mod.discover_changed(None)
    assert got == ["core/scripts/foo.py", "mind_api/src/bar.py"]


def test_discover_changed_handles_renames(mod, monkeypatch):
    monkeypatch.setattr(mod, "_git", lambda args: "R  core/scripts/old.py -> core/scripts/new.py\n")
    assert mod.discover_changed(None) == ["core/scripts/new.py"]


def test_testpaths_are_read_from_pytest_ini(mod):
    roots = mod._testpaths()
    names = {r.name for r in roots}
    # pytest.ini is the single source of truth; a new tree joins by being declared
    # there. Hardcoding here would re-create the drift that hid 1,448 tests.
    assert "tests" in names or "gates" in names
    assert all(r.is_dir() for r in roots)


def test_log_dir_is_outside_the_repo(mod):
    """guard-3375: a runner that writes near a Body's working memory destroys it
    (measured 166,024 B -> 8,705 B). This tier must write nowhere near the tree."""
    d = mod._log_dir("echo")
    assert PROJECT_ROOT not in d.parents and d != PROJECT_ROOT
    assert "agents" not in d.parts


# ── The tri-state contract, exercised through the real CLI ────────────────────

def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(MODULE_PATH), *args],
        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=300,
    )


def test_empty_selection_is_inconclusive_not_pass():
    """An empty selection returns 2 (INCONCLUSIVE) and NEVER 0.

    The probe name is generated, not literal, because the map is TEXTUAL: a test
    file that merely MENTIONS a path is selected for it, and a literal name here
    would appear in this very file and self-match (it did — the first version of
    this test selected 1 of 1443 and returned 0). That over-selection is the safe
    direction and is by design; it is only a problem for a test of the selector.
    """
    import uuid
    probe = f"core/scripts/zz-{uuid.uuid4().hex}.py"
    r = _run_cli("--changed", probe, "--list-only")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "INCONCLUSIVE" in r.stdout


def test_no_changed_source_file_is_inconclusive():
    r = _run_cli("--changed", "core/config/aspirations.yaml", "--list-only")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "INCONCLUSIVE" in r.stdout


def test_changed_and_since_are_mutually_exclusive():
    r = _run_cli("--changed", "core/scripts/foo.py", "--since", "HEAD~1")
    assert r.returncode == 3


def test_json_output_carries_the_population_control():
    """Every count is reported beside the corpus size it came from (guard-2298)."""
    import json
    r = _run_cli("--changed", "core/scripts/dependency-timeout-check.py",
                 "--list-only", "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    payload = json.loads(r.stdout)
    assert payload["corpus_count"] > 0
    assert payload["selected_count"] > 0
    assert payload["selected_share_pct"] == pytest.approx(
        100.0 * payload["selected_count"] / payload["corpus_count"], abs=0.01)


# ── The PASS path, exercised for real ─────────────────────────────────────────
#
# Every other CLI test above stops at --list-only or at an argument error, so
# until 2026-09-10 nothing in this file ever ran pytest through the tier: the
# one verdict the tier exists to produce — 0/PASS — was covered only by
# hand-runs (sq-019, ). An untested PASS is the worst gap this file
# could carry, because PASS is what licenses skipping the full suite.
#
# It needs a RECURSION GUARD rather than a careful choice of target. The map is
# TEXTUAL, so any real source path named in this file makes the tier select THIS
# file — measured: `--changed core/scripts/dependency-timeout-check.py` selects
# 6 files and test_run_scoped_suite.py is one of them — and a nested run would
# re-enter this same test forever. run_pytest hands the child `dict(os.environ)`
# (run-scoped-suite.py:219), so a sentinel set here reaches the nested pytest.
_SELFTEST_ENV = "MIND_SCOPED_SUITE_SELFTEST"


@pytest.mark.skipif(
    os.environ.get(_SELFTEST_ENV) == "1",
    reason="nested run inside the tier's own PASS-path test",
)
def test_pass_path_runs_pytest_for_real_and_returns_zero():
    env = dict(os.environ)
    env[_SELFTEST_ENV] = "1"
    r = subprocess.run(
        [sys.executable, str(MODULE_PATH),
         "--changed", "core/scripts/dependency-timeout-check.py"],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=600,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "VERDICT: PASS" in r.stdout
    # a PASS must never be reachable on an empty selection — the tri-state's
    # whole point; assert the selection was non-empty in the same breath
    assert "0 test file(s) selected" not in r.stdout


# ──  F3: the filter's removals are REPORTED, not silent ──────────────
# The filter itself is unchanged and must stay unchanged — a test file is a
# SELECTOR in this tier, never a subject, so excluding it is correct. What was
# wrong is that a caller saw only `changed_count: 0` and an INCONCLUSIVE reading
# "no changed source file found", which asserts the caller changed nothing when
# the truth is the runner discarded everything it was handed. Same silent-signal
# class the parent goal audits (guard-1760: a runner reports what it RAN, never
# what it declined to look at).

def test_a_supplied_test_file_is_reported_as_dropped_not_as_nothing_changed():
    import json
    r = _run_cli("--changed", "core/scripts/tests/test_release.py",
                 "--list-only", "--json")
    assert r.returncode == 2, r.stdout + r.stderr
    payload = json.loads(r.stdout)
    assert payload["changed_count"] == 0
    assert payload["dropped_count"] == 1
    assert payload["dropped_inputs"] == ["core/scripts/tests/test_release.py"]
    # The reason must say what happened, not merely that nothing was found.
    assert "DROPPED" in payload["reason"]
    assert "core/scripts/tests/test_release.py" in payload["reason"]
    assert "/tests/" in payload["reason"], (
        "the reason must NAME the filter that removed the path, or the caller "
        "cannot tell a dropped input from an absent one")


def test_a_real_source_file_is_not_dropped_positive_control():
    """The drop pin must be able to fail in BOTH directions.

    A generated name is used for the same reason the empty-selection test uses
    one: the selection map is TEXTUAL, so a literal source path written here
    would self-match this very file. The generated path is a legitimate source
    path (prefix + suffix, no '/tests/' segment), so it must survive the filter
    and select nothing — dropped_count 0 beside changed_count 1.
    """
    import json
    import uuid
    probe = f"core/scripts/zz-{uuid.uuid4().hex}.py"
    r = _run_cli("--changed", probe, "--list-only", "--json")
    assert r.returncode == 2, r.stdout + r.stderr
    payload = json.loads(r.stdout)
    assert payload["dropped_count"] == 0, (
        "a valid source path must never be reported as dropped")
    assert payload["changed_count"] == 1
    assert payload["dropped_inputs"] == []


def test_kept_and_dropped_reconcile_against_the_supplied_list():
    """changed_count + dropped_count == what the caller passed.

    Without this the two counts can drift apart silently and a caller auditing
    its own inputs would have no way to notice a third, unreported outcome.
    """
    import json
    import uuid
    kept = f"core/scripts/zz-{uuid.uuid4().hex}.py"
    supplied = [kept, "core/scripts/tests/test_release.py", "CLAUDE.md"]
    r = _run_cli("--changed", *supplied, "--list-only", "--json")
    payload = json.loads(r.stdout)
    assert payload["changed_count"] + payload["dropped_count"] == len(supplied)
    assert payload["dropped_count"] == 2
    assert "CLAUDE.md" in payload["dropped_inputs"]


def test_human_readable_output_separates_dropped_from_unmapped():
    """DROPPED and UNMAPPED are different findings and must not share a line.

    UNMAPPED means 'this file survived the filter and no test references it' —
    a coverage finding. DROPPED means 'this file never reached selection at
    all'. Collapsing them would re-hide exactly what this change exposes.
    """
    import uuid
    kept = f"core/scripts/zz-{uuid.uuid4().hex}.py"
    r = _run_cli("--changed", kept, "core/scripts/tests/test_release.py",
                 "--list-only")
    out = r.stdout
    assert "DROPPED before selection" in out
    assert "core/scripts/tests/test_release.py" in out.split("DROPPED before selection")[1]


def test_unmapped_can_never_contain_a_dropped_path(mod):
    """The comment above SOURCE_PREFIXES claimed the opposite until 2026-09-13.

    select() is only ever handed the SURVIVORS, so a path outside the source
    prefixes cannot appear in `unmapped`. Pinned at the unit level because the
    claim was a comment, and a comment that contradicts its code is the exact
    silent signal this goal audits.
    """
    idx = {Path("core/scripts/tests/test_zzz.py"): "nothing references anything"}
    per_file, unmapped = mod.select(["CLAUDE.md"], idx)
    # select() does not filter — main() does — so this proves the ORDER matters:
    # handed a non-source path directly, select() reports it unmapped, which is
    # precisely why main() must record its own drops separately.
    assert unmapped == ["CLAUDE.md"]
    assert per_file["CLAUDE.md"] == set()


# ── : a REPEATED --changed must accumulate, not keep the last ──────
# `--changed` was declared nargs='+' with argparse's default store action, so
# `--changed A --changed B --changed C` REPLACED the list on every repeat and
# only C reached selection (measured 2026-09-24: "every one of the 1 supplied
# path(s)" for three). Had C been a mappable source file the tier would have
# printed VERDICT PASS over one of three changed files — a false fast-tier
# closure (guard-7376). action='extend' keeps the one-flag `--changed a b c` form.

def test_repeated_changed_flags_accumulate_every_path():
    """Both forms must hand select() the SAME paths, and every one must reach it.

    Asserting on the parser alone would not do: a flag the parser accepts is not
    a flag the script wires (guard-3893). The probe paths are generated names no
    test references, so the proof that each one REACHED selection is that
    select() reports it in unmapped_files. The test-file input keeps the
    kept/dropped reconciliation honest across repeats.
    """
    import json
    import uuid
    a = f"core/scripts/zz-{uuid.uuid4().hex}.py"
    b = f"core/scripts/zz-{uuid.uuid4().hex}.py"
    t = "core/scripts/tests/test_release.py"

    repeated = _run_cli("--changed", a, "--changed", b, "--changed", t,
                        "--list-only", "--json")
    single = _run_cli("--changed", a, b, t, "--list-only", "--json")
    for r in (repeated, single):
        # unmapped probes => INCONCLUSIVE, never PASS
        assert r.returncode == 2, r.stdout + r.stderr
    got, want = json.loads(repeated.stdout), json.loads(single.stdout)

    assert got["changed_files"] == [a, b], got["changed_files"]
    assert got["dropped_inputs"] == [t]
    assert got["changed_count"] + got["dropped_count"] == 3
    assert got["unmapped_files"] == [a, b]
    # the repeated form must be indistinguishable from the one-flag form
    for key in ("changed_files", "dropped_inputs", "unmapped_files"):
        assert got[key] == want[key], key


def test_zero_test_files_flags_main_style_only(mod, tmp_path):
    """: pytest collects nothing from a main()-style file."""
    main_style = tmp_path / "test_main_style.py"
    main_style.write_text("def main():\n    assert True\n\nif __name__ == '__main__':\n    main()\n")
    plain = tmp_path / "test_plain.py"
    plain.write_text("def test_ok():\n    assert True\n")
    klass = tmp_path / "test_klass.py"
    klass.write_text("class TestThing:\n    def test_ok(self):\n        assert True\n")
    # pytest collects a unittest.TestCase subclass whatever its name.
    unit = tmp_path / "test_unit.py"
    unit.write_text("import unittest\n\nclass Advisory(unittest.TestCase):\n"
                    "    def test_ok(self):\n        self.assertTrue(True)\n")
    assert mod._zero_test_files([main_style, plain, klass, unit]) == [str(main_style)]


def test_green_run_with_a_zero_test_file_is_not_a_bare_pass(mod, monkeypatch, capsys):
    """: a green run whose selection includes main()-style files
    (6 of 26 for capability-gate.py) must not report a bare PASS."""
    import json
    monkeypatch.setattr(mod, "run_pytest", lambda files, log_path, timeout: (0, ""))
    monkeypatch.setattr(sys, "argv", ["run-scoped-suite.py", "--changed",
                                      "core/scripts/capability-gate.py", "--json"])
    rc = mod.main()
    out = json.loads(capsys.readouterr().out)
    assert rc == 2 and out["verdict"] == "PASS_WITH_GAPS", out["verdict"]
    assert "core/scripts/tests/test_layer_d_telemetry.py" in out["zero_test_files"]
    assert "test_layer_d_telemetry.py" in out["reason"]
