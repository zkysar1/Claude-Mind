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
                 nothing — the run proved nothing about those files

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


def run_pytest(test_files: list[Path], log_path: Path, timeout: int) -> tuple[int, str]:
    env = dict(os.environ)
    env["STORAGE_BACKEND"] = "local"   # guard-955 — MANDATORY on an own-cloud box
    env["PYTHONUNBUFFERED"] = "1"
    cmd = [sys.executable, "-u", "-m", "pytest", "-q",
           "-m", "not daemon_integration",
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
    ap.add_argument("--changed", nargs="+", metavar="FILE",
                    help="Explicit changed-file list (repo-relative). "
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
    if rc == 0:
        # Non-empty selection ran green. This is the ONLY path that returns PASS,
        # and `unmapped` still qualifies it.
        if unmapped:
            result["verdict"] = "PASS_WITH_GAPS"
            result["reason"] = ("selected tests passed, but these changed files are "
                                f"referenced by NO test: {', '.join(unmapped)}. They "
                                "were not verified by this run.")
            _emit(result, args.json, tail)
            return 2      # a gap is not a pass — INCONCLUSIVE for the gate's purposes
        result["verdict"] = "PASS"
        result["reason"] = f"{len(selected)} test file(s) selected and green"
        _emit(result, args.json, tail)
        return 0
    result["verdict"] = "FAIL"
    result["reason"] = f"pytest exited {rc}"
    _emit(result, args.json, tail)
    return 1


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
    if result["elapsed_s"] is not None:
        print(f"  elapsed: {result['elapsed_s']}s   log: {result['log']}")
    if tail:
        print("  --- pytest tail ---")
        for line in tail.splitlines():
            print(f"  {line}")


if __name__ == "__main__":
    sys.exit(main())
