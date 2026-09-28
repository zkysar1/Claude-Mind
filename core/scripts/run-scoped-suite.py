#!/usr/bin/env python3
"""run-scoped-suite — impact-scoped verification tier (fast lane).

WHY THIS EXISTS (g-115-9602, standing user directive 2026-09-10, user
verbatim): "We need it much smaller, and self serve for each agent. WE cannot
have each of our agents pausing for 4 hours after each deep goal."

The framework had exactly ONE verification tier: the full suite, ~1,442 files /
~20.4k tests, measured 187 min, mandatory for deep-code closes. Every deep close
either paid that or skipped verification, and the user observed 3-4 agents
running it CONCURRENTLY, each idle-blocked. This runs only the tests that
reference what actually changed.

IT DOES NOT REPLACE THE FULL SUITE. It makes the full run rare instead of
mandatory-and-skipped. `run-full-suite-after-deep-code.md` says when each applies.

── THE VERDICT IS TRI-STATE, AND THAT IS THE LOAD-BEARING PART ────────────────
An empty selection is NOT a pass. Measured 2026-09-10 over 14 real
source-touching commits: 3 of them selected ZERO tests (owncloud-store-enumerate.py
twice, object-store-conformance.py) because no test anywhere references those
files. A scoped runner that runs 0 tests and prints green is worse than no
runner — it manufactures confidence. So:

    PASS         a NON-EMPTY selection ran and every test passed
    FAIL         any test failed or errored
    INCONCLUSIVE the selection was empty, or some changed file mapped to
                 nothing, or a selected test file is main()-style (pytest
                 runs 0 tests from it) — the run proved nothing about those files

INCONCLUSIVE names the unmapped files, because "this file has no test that
references it" is the most useful thing this tool can tell you. It is a coverage
finding, not a tool failure. (guard-963 / guard-3351: never emit a clean verdict
over a collection in which nothing was verified.)

── SELECTION IS A PATH-REFERENCE MAP, NOT AN IMPORT GRAPH ─────────────────────
This repo's tests mostly do NOT import the code under test — they invoke wrappers
as SUBPROCESSES by path string (test_release.py and test_promote.py import only
stdlib). An import graph would therefore select almost nothing. A changed source
file selects a test file when any of these holds:

    (a) the test text contains the changed file's basename, or its sibling
        wrapper name (foo.py <-> foo.sh, with _ / - normalised both ways)
    (b) the test imports the module stem (`import foo` / `from foo import ...`)
    (c) the test is named test_<stem>.py or test_<stem>_*.py

Measured selectivity over those same 14 commits, against a 1,442-file corpus:
median 0.3%, min 0.0%, max 10.0%. The 10.0% case was a 3-file change touching
aspirations.py — a genuine hub, so a wide fan-out there is the map working.

THE MAP IS TEXTUAL, SO IT OVER-SELECTS, AND THAT IS THE SAFE DIRECTION. A test
that merely MENTIONS a path — in a fixture argument, a docstring, a comment — is
selected for it even if it never exercises it. Over-selection costs extra
seconds; under-selection would ship an unverified change. Do not "fix" this by
narrowing to imports: this repo's tests reach their subject by subprocess path
string, so an import-only map selects almost nothing (see above). Measured
in-house: the first version of test_run_scoped_suite.py used a literal
placeholder filename to assert the empty-selection path, and the placeholder
appeared in that file's own source, so the probe self-matched (1 of 1443) and
the empty-selection assertion failed. The test now generates its probe name.

ZERO DEPENDENCIES BY REQUIREMENT. pytest_testmon, coverage, pytest_cov,
pytest_picked and pytest_xdist are all ABSENT on this box (measured, pytest
7.4.4). A coverage- or testmon-based tier would need a dependency installed on
every box, which is a fleet change, not self-serve.

── SAFETY ─────────────────────────────────────────────────────────────────────
* WORKER-BODY SAFE (guard-3375). BODY_WM_PATH is injected into every Bash call
  and the full suite's writes land on the live Body working memory (measured
  destruction 166,024 B -> 8,705 B). This runner writes NOTHING to working
  memory and puts its log outside the synced tree.
* STORAGE_BACKEND=local is forced (guard-955). On an own-cloud box a tmp-world
  write otherwise collides on the PRODUCTION S3 key.
* `-m "not daemon_integration"` (Live-Daemon Exception) so it is safe beside a
  live daemon.
* It takes NO tree lock and needs NO quiet window: it is minutes, not hours, so
  the tree-moved class the full suite fights does not apply. Do not add a lock.

Exit: 0 PASS | 1 FAIL | 2 INCONCLUSIVE | 3 setup error
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Mirrors pytest.ini `testpaths`. Read from the config rather than hardcoded so a
# new test tree joins by being declared there — the same drift that let 1,448
# tests go uncollected for five weeks (run-full-suite-after-deep-code.md
# § "THREE testpaths").
PYTEST_INI = PROJECT_ROOT / "pytest.ini"

# Source roots whose files this tier can map. A change OUTSIDE these is DROPPED
# before selection — it is NOT reported as unmapped. `unmapped` can only ever hold
# files that SURVIVED the filter in main(), because select() never sees the rest.
# This comment said the opposite until 2026-09-13 (). The drops are now
# recorded in `dropped_inputs` so the loss is visible instead of inferred.
SOURCE_PREFIXES = ("core/scripts/", "mind_api/src/", "core/tests/", "world/scripts/")

SOURCE_SUFFIXES = (".py", ".sh")


def _testpaths() -> list[Path]:
    """Read testpaths out of pytest.ini. Fail loud — a wrong root selects nothing."""
    if not PYTEST_INI.exists():
        raise SystemExit(f"[setup] pytest.ini not found at {PYTEST_INI}")
    roots: list[Path] = []
    in_block = False
    for raw in PYTEST_INI.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if line.startswith("testpaths"):
            in_block = True
            after = line.split("=", 1)[1].strip() if "=" in line else ""
            if after:
                roots.append(PROJECT_ROOT / after)
            continue
        if in_block:
            if not line.startswith((" ", "\t")) or not line.strip():
                break
            if line.strip().startswith("#"):
                continue
            roots.append(PROJECT_ROOT / line.strip())
    live = [r for r in roots if r.is_dir()]
    if not live:
        raise SystemExit("[setup] pytest.ini declared no usable testpaths")
    return live


def _index_tests(roots: list[Path]) -> dict[Path, str]:
    idx: dict[Path, str] = {}
    for root in roots:
        for t in sorted(root.rglob("test_*.py")):
            try:
                idx[t] = t.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
    return idx


def _variants(changed: str) -> tuple[set[str], str]:
    """Literal strings that identify this source file, plus its module stem."""
    p = Path(changed)
    stem = p.stem
    alt = stem.replace("-", "_")
    dash = stem.replace("_", "-")
    names = {p.name, f"{stem}.py", f"{stem}.sh", f"{dash}.py", f"{dash}.sh",
             f"{alt}.py", f"{alt}.sh"}
    return names, alt


def select(changed_files: list[str], idx: dict[Path, str]) -> tuple[dict[str, set[Path]], list[str]]:
    """Map each changed file to the test files that reference it.

    Returns (per_file_selection, unmapped). `unmapped` is what makes the verdict
    INCONCLUSIVE — never drop it.
    """
    per_file: dict[str, set[Path]] = {}
    unmapped: list[str] = []
    for changed in changed_files:
        names, alt = _variants(changed)
        import_re = re.compile(rf"^\s*(?:from|import)\s+{re.escape(alt)}\b", re.M)
        hits: set[Path] = set()
        for t, txt in idx.items():
            if any(n in txt for n in names):
                hits.add(t)
                continue
            if import_re.search(txt):
                hits.add(t)
                continue
            if t.name == f"test_{alt}.py" or t.name.startswith(f"test_{alt}_"):
                hits.add(t)
        per_file[changed] = hits
        if not hits:
            unmapped.append(changed)
    return per_file, unmapped


def _git(args: list[str]) -> str:
    out = subprocess.run(["git", *args], cwd=PROJECT_ROOT,
                         capture_output=True, text=True)
    return out.stdout


def discover_changed(since: str | None) -> list[str]:
    """Changed source files: `--since <ref>` diff, else the uncommitted working set."""
    if since:
        raw = _git(["diff", "--name-only", f"{since}...HEAD"])
        files = [f.strip() for f in raw.splitlines() if f.strip()]
    else:
        raw = _git(["status", "--porcelain"])
        files = []
        for line in raw.splitlines():
            if len(line) > 3:
                # porcelain: XY <path>, and renames carry "old -> new"
                path = line[3:].strip()
                if " -> " in path:
                    path = path.split(" -> ", 1)[1]
                files.append(path.strip().strip('"'))
    return [f for f in files
            if f.startswith(SOURCE_PREFIXES)
            and f.endswith(SOURCE_SUFFIXES)
            and "/tests/" not in f]


def _log_dir(agent: str) -> Path:
    """Outside the synced tree, per-agent. NEVER under agents/<agent>/ — a Body's
    working memory lives there and guard-3375 exists because suite writes destroyed
    one (166,024 B -> 8,705 B)."""
    d = Path(tempfile.gettempdir()) / f"ayoai-scoped-run-{agent or 'shared'}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_invisible_delegation(zero_files: list[str], timeout: int) -> tuple[int, str]:
    """Execute zero-test files through run-invisible-suites.sh --files.

    pytest collects 0 tests from a main()-style file, so no pytest invocation
    can ever RUN it — naming it in a gap is recording that this tier never
    executes it (g-115-10962 outcome 2). The invisible runner is the only one
    that can, and its --files mode delegates exactly the given files through
    the same QUARANTINE + per-file-timeout + agent-binding contract as the
    full-enumeration half. bash_cmd per guard-580: never a bare "bash"
    argv[0] (the Windows WSL-stub class); deferred import because the test
    loader (spec_from_file_location) has no core/scripts on sys.path — same
    shape as _shared_tick.py. Returns (rc, output): 0 = every file passed,
    1 = at least one failed, 2 = setup refusal (unbound, missing path,
    unsupported type).
    """
    from _runtime_bash import bash_cmd  # guard-580
    script = PROJECT_ROOT / "core" / "scripts" / "tests" / "run-invisible-suites.sh"
    cmd = bash_cmd(str(script), "--files", *zero_files)
    proc = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True,
                          timeout=timeout)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def run_pytest(test_files: list[Path], log_path: Path, timeout: int) -> tuple[int, str]:
    env = dict(os.environ)
    env["STORAGE_BACKEND"] = "local"   # guard-955 — MANDATORY on an own-cloud box
    env["PYTHONUNBUFFERED"] = "1"
    cmd = [sys.executable, "-u", "-m", "pytest", "-q",
           "-m", "not daemon_integration",
           "-rs",   #  outcome 3: report skip reasons so a module-level
           # `importorskip` (dep absent on this box) is NAMEABLE in the verdict
           # instead of reading as green. Measured shape 2026-09-27:
           #   SKIPPED [1] <file>:<line>: could not import '<dep>': No module named '<dep>'
           *[str(p.relative_to(PROJECT_ROOT)) for p in sorted(test_files)]]
    with log_path.open("w", encoding="utf-8") as fh:
        try:
            rc = subprocess.run(cmd, cwd=PROJECT_ROOT, env=env,
                                stdout=fh, stderr=subprocess.STDOUT,
                                timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            fh.write(f"\n[scoped] TIMEOUT after {timeout}s\n")
            return 124, log_path.read_text(encoding="utf-8", errors="ignore")
    return rc, log_path.read_text(encoding="utf-8", errors="ignore")


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="run-scoped-suite",
        description="Impact-scoped verification tier. Tri-state: "
                    "0 PASS | 1 FAIL | 2 INCONCLUSIVE | 3 setup.")
    # action='extend': a REPEATED --changed accumulates. nargs='+' alone kept only
    # the last repeat, so a mappable last path read PASS over 1 of N ().
    ap.add_argument("--changed", nargs="+", action="extend", metavar="FILE",
                    help="Explicit changed-file list (repo-relative); may be "
                         "repeated, values accumulate. "
                         "Default: the uncommitted working set.")
    ap.add_argument("--since", metavar="REF",
                    help="Derive the changed set from `git diff REF...HEAD`.")
    ap.add_argument("--list-only", action="store_true",
                    help="Print the selection and exit without running pytest.")
    ap.add_argument("--json", action="store_true", help="Machine-readable output.")
    ap.add_argument("--timeout", type=int, default=1800,
                    help="Hard bound in seconds (default 1800). A scoped run that "
                         "needs 30 min is not a fast tier — investigate, do not raise.")
    args = ap.parse_args()

    if args.changed and args.since:
        print("[setup] --changed and --since are mutually exclusive", file=sys.stderr)
        return 3

    supplied = args.changed if args.changed else discover_changed(args.since)
    changed = [c for c in supplied
               if c.startswith(SOURCE_PREFIXES) and c.endswith(SOURCE_SUFFIXES)
               and "/tests/" not in c]
    # THE FILTER IS CORRECT AND IS NOT CHANGED HERE — what changes is that its
    # removals are now REPORTED. A caller passing a test file, or a path outside
    # SOURCE_PREFIXES, previously saw only `changed_count: 0` and an INCONCLUSIVE
    # reading "no changed source file found", which says "you changed nothing"
    # when the truth is "I discarded everything you gave me". Same shape as the
    # silent-signal class this goal audits: the runner reports what it RAN and
    # never what it declined to look at (guard-1760). Order-preserving, and a
    # duplicate in `supplied` is deliberately kept in both lists rather than
    # de-duplicated, so the two counts always reconcile against the input.
    _kept = set(changed)
    dropped = [c for c in supplied if c not in _kept]

    roots = _testpaths()
    idx = _index_tests(roots)
    if not idx:
        print("[setup] indexed 0 test files — check pytest.ini testpaths", file=sys.stderr)
        return 3

    per_file, unmapped = select(changed, idx)
    selected: set[Path] = set()
    for hits in per_file.values():
        selected |= hits

    result = {
        "verdict": None,
        "changed_files": changed,
        "changed_count": len(changed),
        "dropped_inputs": dropped,
        "dropped_count": len(dropped),
        "unmapped_files": unmapped,
        "selected_count": len(selected),
        "corpus_count": len(idx),          # population control beside every count
        "selected_share_pct": round(100.0 * len(selected) / len(idx), 2) if idx else 0.0,
        "selected": sorted(str(p.relative_to(PROJECT_ROOT)) for p in selected),
        # pytest collects 0 tests from these (main()-style), so a green run says
        # nothing about them (, guard-1653).
        "zero_test_files": _zero_test_files(selected),
        "testpaths": [str(r.relative_to(PROJECT_ROOT)) for r in roots],
        "log": None,
        "elapsed_s": None,
        "reason": None,
    }

    # ── The tri-state gate, BEFORE any run ────────────────────────────────────
    if not changed:
        result["verdict"] = "INCONCLUSIVE"
        if dropped:
            result["reason"] = (
                f"every one of the {len(dropped)} supplied path(s) was DROPPED before "
                "selection — nothing was verified. This is not 'you changed nothing'. "
                f"Dropped: {', '.join(dropped)}. A path is dropped unless it starts with "
                f"one of {SOURCE_PREFIXES}, ends with one of {SOURCE_SUFFIXES}, and "
                "contains no '/tests/' segment. Test files are excluded BY DESIGN (this "
                "tier maps source->test, so a test file is a selector, never a subject); "
                "pass the SOURCE file it covers instead.")
        else:
            result["reason"] = ("no changed source file found — nothing was verified. "
                                "Pass --changed explicitly, or --since <ref>.")
        _emit(result, args.json)
        return 2
    if not selected:
        result["verdict"] = "INCONCLUSIVE"
        result["reason"] = ("selection is EMPTY: no test in the corpus references any "
                            f"changed file ({', '.join(changed)}). This is a COVERAGE "
                            "finding, not a pass — the full suite would not cover them "
                            "either. Write a test, or close with the gap stated.")
        _emit(result, args.json)
        return 2

    if args.list_only:
        result["verdict"] = "LIST_ONLY"
        result["reason"] = "selection printed; pytest not run"
        _emit(result, args.json)
        return 0

    agent = os.environ.get("MIND_AGENT", "")
    log_path = _log_dir(agent) / f"scoped-{time.strftime('%Y%m%dT%H%M%S')}.log"
    t0 = time.time()
    rc, log_text = run_pytest(sorted(selected), log_path, args.timeout)
    result["elapsed_s"] = round(time.time() - t0, 1)
    result["log"] = str(log_path)

    tail = "\n".join(log_text.strip().splitlines()[-8:])
    if rc == 124:
        result["verdict"] = "INCONCLUSIVE"
        result["reason"] = f"timed out after {args.timeout}s — nothing was proven"
        _emit(result, args.json, tail)
        return 2
    if rc == 5:
        # pytest exit 5 = "no tests ran" — every selected file was skipped at
        # module level. Measured 2026-09-27 on the deciding box (zc-10): a
        # selection consisting only of module-`importorskip`-gated files (12 of
        # them; the gated dependency absent on this box) exits 5 with a
        # green-looking "1 skipped" tail. The pre-fix code fell through to FAIL below — the wrong
        # direction: nothing FAILED, nothing RAN. INCONCLUSIVE, with the
        # module-skip evidence named ( outcome 3).
        mod_skip = _module_skip_files(selected, log_text)
        result["skipped_module_files"] = mod_skip
        result["verdict"] = "INCONCLUSIVE"
        if mod_skip:
            result["reason"] = (
                f"pytest ran 0 tests: {len(mod_skip)} selected file(s) were skipped "
                f"at MODULE level by `importorskip` (dependency absent on this box) — "
                f"{', '.join(mod_skip)}. Nothing ran, so nothing was verified; the "
                "skips are environmental, not red (g-115-10962 outcome 3).")
        else:
            result["reason"] = ("pytest ran 0 tests and no module-level importorskip "
                                "skip is visible in the run's own -rs summary — "
                                "nothing was verified (check the log for collection "
                                "errors).")
        _emit(result, args.json, tail)
        return 2
    if rc == 0:
        # Non-empty selection ran green. This is the ONLY path that returns PASS,
        # and `unmapped` still qualifies it; zero-test files are EXECUTED, not
        # just named ( outcome 2) — see the delegation below.
        zero = result["zero_test_files"]
        if unmapped:
            gaps = [f"these changed files are referenced by NO test: "
                    f"{', '.join(unmapped)}"]
            result["verdict"] = "PASS_WITH_GAPS"
            result["reason"] = ("selected tests passed, but " + "; and ".join(gaps)
                                + ". They were not verified by this run.")
            _emit(result, args.json, tail)
            return 2      # a gap is not a pass — INCONCLUSIVE for the gate's purposes
        if not zero:
            mod_skip = _module_skip_files(selected, log_text)
            if mod_skip:
                #  outcome 3: a green run is NOT a bare pass over
                # files whose tests were skipped at module level (importorskip,
                # dep absent on this box). The PASS is qualified and the files
                # are named, from the run's own -rs evidence only.
                result["skipped_module_files"] = mod_skip
                result["verdict"] = "PASS_WITH_SKIPS"
                result["reason"] = (
                    f"{len(selected)} test file(s) selected and green, but "
                    f"{len(mod_skip)} file(s) were SKIPPED at module level by "
                    f"importorskip (dependency absent on this box) and ran 0 "
                    f"tests: {', '.join(mod_skip)}. This PASS is QUALIFIED over "
                    "those files (g-115-10962 outcome 3).")
                _emit(result, args.json, tail)
                return 0
            result["verdict"] = "PASS"
            result["reason"] = f"{len(selected)} test file(s) selected and green"
            _emit(result, args.json, tail)
            return 0
        # pytest collected 0 tests from these — the ONLY runner that can execute
        # them is the invisible-suite runner, so the green pytest half is
        # extended with a delegation rather than left as a gap. The delegation
        # runs OUTSIDE the pytest log (own per-file tail, printed below).
        result["zero_test_delegated"] = True
        try:
            zrc, zout = run_invisible_delegation(zero, args.timeout)
        except subprocess.TimeoutExpired:
            result["verdict"] = "INCONCLUSIVE"
            result["reason"] = (f"selected tests passed, but the {len(zero)} zero-test "
                                f"file(s) ({', '.join(zero)}) timed out during invisible "
                                f"delegation after {args.timeout}s — they were not verified")
            _emit(result, args.json, tail)
            return 2
        result["zero_test_result"] = zout
        mod_skip = _module_skip_files(selected, log_text)
        if mod_skip:
            result["skipped_module_files"] = mod_skip
        if zrc == 0:
            if mod_skip:
                #  outcome 3: the delegated half is green, but some
                # pytest-collected files' tests never ran (module-level
                # importorskip, dep absent on this box) — qualified PASS.
                result["verdict"] = "PASS_WITH_SKIPS"
                result["reason"] = (f"{len(selected)} test file(s) selected: "
                                    f"{len(selected) - len(zero) - len(mod_skip)} pytest-collected and green, "
                                    f"{len(zero)} zero-test file(s) delegated to run-invisible-suites.sh and green, "
                                    f"but {len(mod_skip)} file(s) were SKIPPED at module level by importorskip "
                                    f"and ran 0 tests: {', '.join(mod_skip)}. This PASS is QUALIFIED over "
                                    "those files (g-115-10962 outcome 3).")
            else:
                result["verdict"] = "PASS"
                result["reason"] = (f"{len(selected)} test file(s) selected: {len(selected) - len(zero)} "
                                    f"pytest-collected and green, {len(zero)} zero-test file(s) "
                                    f"delegated to run-invisible-suites.sh and green")
            _emit(result, args.json, tail + "\n" + zout)
            return 0
        if zrc == 2:
            result["verdict"] = "INCONCLUSIVE"
            result["reason"] = (f"selected tests passed, but invisible delegation REFUSED "
                                f"({len(zero)} zero-test file(s), runner exit 2) — "
                                f"{zout.splitlines()[-1] if zout else 'no output'}. "
                                "Set MIND_AGENT or check the file paths; 'did not run' "
                                "must not read as pass.")
            _emit(result, args.json, tail + "\n" + zout)
            return 2
        result["verdict"] = "FAIL"
        result["reason"] = (f"pytest half passed, but invisible delegation FAILED "
                            f"({len(zero)} zero-test file(s), runner exit 1) — a "
                            "main()-style file in the selection is red")
        _emit(result, args.json, tail + "\n" + zout)
        return 1
    result["verdict"] = "FAIL"
    result["reason"] = f"pytest exited {rc}"
    _emit(result, args.json, tail)
    return 1


_TOP_LEVEL_TEST = re.compile(
    r"^(?:async\s+)?def test_|^class Test|^class \w+\([^)]*TestCase", re.MULTILINE)


def _zero_test_files(paths) -> list:
    """Selected files pytest collects nothing from: no top-level `def test_`,
    no `class Test*`, and no unittest.TestCase subclass (collected whatever its
    name). Checked 2026-09-25 against `pytest --collect-only` over all 1573
    test files: 0 false positives. It does not flag module-level
    `importorskip` files; pytest reports those as skipped rather than hiding
    them (g-115-10962 outcome 3: `_module_skip_files` names them instead)."""
    zero = []
    for p in paths:
        try:
            text = Path(p).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not _TOP_LEVEL_TEST.search(text):
            try:
                zero.append(str(Path(p).relative_to(PROJECT_ROOT)))
            except ValueError:
                zero.append(str(p))
    return sorted(zero)


# The -rs skip-summary line for a module-level importorskip (measured
# 2026-09-27, deciding box zc-10; <dep> = the gated dependency):
#   SKIPPED [1] core/scripts/tests/test_<module>.py:29: could not import '<dep>': No module named '<dep>'
# A TEST-LEVEL skip (`pytest.skip` / `skipif`) does NOT name an import — the
# 'could not import' clause is the module-skip discriminator, so a conditional
# skip in one test never qualifies a PASS that otherwise ran green.
_MODULE_SKIP_RE = re.compile(
    r"^SKIPPED \[\d+\] (\S+?)(?::\d+)?: could not import '([^']+)'")


def _module_skip_files(selected, log_text: str) -> list:
    """Selected files whose tests were skipped at MODULE level by
    `importorskip` (dependency absent on this box), from the run's OWN -rs
    evidence — not from re-reading source text, which would miss the box
    dimension (the same file runs normally where the dep exists).
    Returns sorted repo-relative names; an empty result means 'the green run
    ran everything it collected'."""
    hits: set[str] = set()
    selected_names = {p.name for p in selected}
    for line in log_text.splitlines():
        m = _MODULE_SKIP_RE.match(line.strip())
        if not m:
            continue
        raw = m.group(1)
        # -rs paths are rootdir-relative, and this runner's rootdir is always
        # PROJECT_ROOT (pytest.ini lives at the root): in-repo files appear
        # repo-relative, out-of-repo files (tmp_path probes) as ../ hops.
        # Resolve against PROJECT_ROOT, never CWD — the CWD is not a fact the
        # runner controls.
        cand = Path(raw)
        resolved = cand if cand.is_absolute() else PROJECT_ROOT / cand
        try:
            hits.add(str(resolved.resolve().relative_to(PROJECT_ROOT)))
        except (ValueError, OSError):
            if cand.name in selected_names:
                hits.add(cand.name)
    return sorted(hits)


def _emit(result: dict, as_json: bool, tail: str = "") -> None:
    if as_json:
        print(json.dumps(result, indent=2))
        return
    print(f"VERDICT: {result['verdict']} — {result['reason']}")
    print(f"  changed: {result['changed_count']} file(s)  "
          f"selected: {result['selected_count']} of {result['corpus_count']} "
          f"({result['selected_share_pct']}% of corpus)")
    if result.get("dropped_inputs"):
        print(f"  DROPPED before selection (not verified, not unmapped): "
              f"{', '.join(result['dropped_inputs'])}")
    if result["unmapped_files"]:
        print(f"  UNMAPPED (no test references these): {', '.join(result['unmapped_files'])}")
    if result.get("skipped_module_files"):
        print(f"  SKIPPED AT MODULE LEVEL (importorskip, dep absent on this box — "
              f"the PASS is qualified over these): "
              f"{', '.join(result['skipped_module_files'])}")
    if result["elapsed_s"] is not None:
        print(f"  elapsed: {result['elapsed_s']}s   log: {result['log']}")
    if tail:
        print("  --- pytest tail ---")
        for line in tail.splitlines():
            print(f"  {line}")


if __name__ == "__main__":
    sys.exit(main())
