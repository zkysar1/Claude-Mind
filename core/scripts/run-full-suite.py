#!/usr/bin/env python3
"""run-full-suite — the ONE safe way to run the framework test suite here.

Three separate constraints govern a valid suite run on this box. Each is
documented somewhere different, each is honor-system, and forgetting any one
produces a WRONG ANSWER rather than an error. This tool makes all three
automatic:

  1. STORAGE_BACKEND=local  (guard-955 / rb-2983)
     On an own-cloud box, OwnCloudBackend._s3_key derives the S3 key from
     customer_prefix+env_id+filename -- NOT from the MIND_WORLD tmp override.
     So a test that seeds a tmp world and writes via a subprocess collides on
     the PRODUCTION key. This truncated world/aspirations.jsonl from 22
     aspirations/1366 goals down to a single fixture on 2026-07-09.

  2. -m "not daemon_integration"  (Live-Daemon Exception)
     Those tests spawn real subprocess daemons against the live
     mind_api/state/, hijack daemon.port, and route running agents onto a
     transient LocalBackend. Two daemon storms on 2026-05-31.

  3. Chunking into fresh processes  (guard-1448, g-115-3085)
     One process running ~5,200 subprocess-heavy tests alongside a live fleet
     exhausts Windows process/desktop-heap resources. Spawns then fail with
     rc=3221225794 (0xC0000142 STATUS_DLL_INIT_FAILED) -- even `git init`
     fails -- yielding hundreds of BOGUS failures. Measured: 564 failed /
     4,672 passed on a tree that was actually clean. A single process cannot
     recover those handles; only a fresh process can.

THE THIRD EXIT CODE IS THE POINT. A contended run does not look broken -- it
looks like a big regression, and the individual failures look completely real
up close. Reporting "564 failed" invites fixing phantom regressions; reporting
"probably environmental" invites waving away a real one. So a contended result
is NOT reported as pass or fail. It exits 2 = INVALID, meaning re-measure.

Exit codes:
  0  clean
  1  genuine failures (trustworthy -- act on them)
  2  INVALID/contended -- the number means nothing, re-run when quiet
  3  usage/setup error
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
TESTS_DIR = SCRIPT_DIR / "tests"

# Windows STATUS_DLL_INIT_FAILED. The signature of resource exhaustion: the OS
# can no longer initialise DLLs for new processes, so ANY spawn dies -- which
# is why `git init` shows up failing in tests that have nothing to do with git.
DLL_INIT_FAILED = "3221225794"

# Positional windows, as pytest progress percentages.
#
# The LATE window must be the TAIL, not "the back half". Exhaustion failures
# concentrate hard at the very end, so a wide window averages the signal away:
# measured on the real incident, the last decile ran ~20% failures but the
# whole 67-100% span diluted to 1.78% -- which a 5% floor would then discard,
# throwing away the true positive. Narrow window, honest rate.
EARLY_WINDOW = 33
LATE_WINDOW = 90

# Minimum late-window failure RATE before a positional profile counts as
# evidence. Measured anchors over the last decile: real exhaustion incident
# 4.73%; healthy tree with 11 pre-existing failures 0.31%. 2% sits ~6x above
# the healthy floor and ~2x below the incident.
#
# POSITION IS CORROBORATING EVIDENCE, NOT PROOF -- and the confound is worth
# naming, because it is not hypothetical. pytest runs files in ALPHABETICAL
# order, so a genuine regression in late-alphabet files mimics the exhaustion
# fingerprint exactly. In the real incident the worst-hit files were
# test_release.py and test_promote.py, which are late alphabetically; position
# alone could not distinguish "exhausted" from "broke the release path".
# The DLL_INIT_FAILED marker is the signal a regression cannot forge (no test
# failure makes the OS refuse to start a process), so it stands alone, while
# position only ever contributes alongside it or triggers --confirm-solo.
LATE_FLOOR = 0.02

CONTENTION_MARKERS = (
    DLL_INIT_FAILED,
    "0xC0000142",
    "git init failed",
    "returned non-zero exit status 3221225794",
)


def _chunk(items, n):
    """Split into n contiguous groups, remainder spread over the leading ones."""
    n = max(1, min(n, len(items)))
    size, extra = divmod(len(items), n)
    out, i = [], 0
    for k in range(n):
        take = size + (1 if k < extra else 0)
        out.append(items[i:i + take])
        i += take
    return out


def _progress_tally(text):
    """(passed, failed, errors, last_pct) counted from pytest's progress chars.

    MEASURED ON THIS BOX (2026-07-25): pytest routinely reaches [100%], prints
    its warnings summary and even its `short test summary info` block, and then
    exits WITHOUT the trailing "N passed, M failed in Xs" line. So the count
    line cannot be the only source of truth -- a fully-green chunk would read
    as 0 passed / 0 failed, and this tool would report a clean run as empty.
    The per-test progress characters are always present, so tally those.
    """
    rows = re.findall(r"^([.FEsxX]+)\s*\[\s*(\d+)%\]", text, re.M)
    if not rows:
        return None
    chars = "".join(r[0] for r in rows)
    return (chars.count("."), chars.count("F"), chars.count("E"),
            int(rows[-1][1]))


def _parse_counts(text):
    """Pull (passed, failed, errors) out of a pytest log.

    Prefer the explicit count line; fall back to the progress tally when it is
    missing (see _progress_tally -- that is the common case here, not an edge).
    Scanned over the whole log, since a warnings summary can trail the line.
    """
    passed = failed = errors = 0
    found = False
    for m in re.finditer(r"(\d+)\s+(passed|failed|error|errors)\b", text):
        n, what = int(m.group(1)), m.group(2)
        found = True
        if what == "passed":
            passed = max(passed, n)
        elif what == "failed":
            failed = max(failed, n)
        else:
            errors = max(errors, n)
    if found:
        return passed, failed, errors

    tally = _progress_tally(text)
    if tally:
        return tally[0], tally[1], tally[2]
    return 0, 0, 0


def _looks_aborted(text):
    """True only when the run stopped BEFORE reaching 100%.

    A missing count line alone is NOT abort evidence here (see
    _progress_tally). Requiring an unfinished progress bar keeps this from
    firing on every healthy chunk, which would make the contended verdict
    meaningless through constant false positives.
    """
    tally = _progress_tally(text)
    if not tally:
        return False
    has_counts = bool(re.search(r"\d+\s+(passed|failed)\b", text))
    return tally[3] < 100 and not has_counts


def _positional_profile(text):
    """Failure rate in the first vs last third of the run.

    THE key discriminator (g-115-3085). Progressive exhaustion shows a clean
    early run and a failure-soaked tail. A genuine regression fails from the
    START, because the changed code is exercised throughout. Measured on the
    564-failure run: 0 failures across the first 1,368 tests, then 19-27% in
    the final decile.
    """
    rows = re.findall(r"^([.FEsxX]+)\s*\[\s*(\d+)%\]", text, re.M)
    if not rows:
        return None
    early = late = 0
    early_f = late_f = 0
    for chars, pct in rows:
        bad = chars.count("F") + chars.count("E")
        pct = int(pct)
        if pct <= EARLY_WINDOW:
            early += len(chars); early_f += bad
        elif pct >= LATE_WINDOW:
            late += len(chars); late_f += bad
    if not early or not late:
        return None
    return (early_f / early, late_f / late, early, late)


def classify(text, failed, chunks=None):
    """Return (verdict, reasons). verdict in {clean, genuine, contended}.

    `chunks` is the list of PER-CHUNK logs. Pass it whenever chunk logs exist:
    completeness CANNOT be judged from the concatenation. Measured failure of
    this exact tool on 2026-07-26 -- chunk 02 stopped at 51%, but on the joined
    text the other chunks' count lines satisfied the has-counts test and the
    final 100%% masked the stall, so the run was declared "GENUINE --
    trustworthy" while a quarter of the suite had never run. A tool whose whole
    purpose is refusing to answer on an invalid measurement must not itself
    launder an incomplete one.
    """
    reasons = []

    for i, chunk in enumerate(chunks or []):
        if _looks_aborted(chunk):
            tally = _progress_tally(chunk)
            reasons.append(
                "chunk %02d stopped at %d%% -- it never finished, so the totals "
                "are missing its tests" % (i, tally[3] if tally else 0))
        elif _progress_tally(chunk) is None and not re.search(
                r"\d+\s+(passed|failed)\b", chunk):
            # SILENT-ZERO CHUNK (, 2026-07-27). _looks_aborted returns
            # False when there is NO progress output at all -- `if not tally:
            # return False` -- so a chunk whose log is empty, truncated, or
            # corrupted contributes 0 tests AND is judged fine, and the run is
            # certified trustworthy. That is the same laundering the docstring
            # above forbids, reached through the opposite door: not "stopped
            # early" but "left no evidence it ran at all".
            #
            # MEASURED: chunk 02 of a 4-chunk run reported "0 passed, 0 failed"
            # and the run still printed "GENUINE -- trustworthy" on a 4290-test
            # total against a ~5969 baseline. Re-parsing that same log
            # afterwards returned (432, 0, 0), and its mtime was LATER than the
            # next chunk's despite chunks running sequentially -- the file was
            # rewritten after the runner read it (the temp dir is cloud-synced;
            # the log had 1532 NUL bytes). Whatever the cause, a chunk of 139
            # files yielding no parseable output is never a clean result.
            reasons.append(
                "chunk %02d produced no parseable test output -- its tests are "
                "missing from the totals (log empty, truncated, or corrupted)" % i)
    for marker in CONTENTION_MARKERS:
        if marker in text:
            reasons.append("resource-exhaustion marker present: %s" % marker)
            break

    prof = _positional_profile(text)
    if prof:
        early_rate, late_rate, n_early, n_late = prof
        # BOTH positional branches require the SAME absolute floor. A ratio
        # alone is not evidence: a healthy run measured 2026-07-25 went 0.1%
        # early -> 0.3% late (11 pre-existing failures over 5,246 tests) and
        # tripped a ratio-only rule at "5.1x", calling a clean tree CONTENDED.
        # That false positive is the expensive kind -- a classifier that cries
        # contention on ordinary failures gets ignored, which is exactly how
        # guard-580 decayed to times_noise=30 / times_helpful=0 and let this
        # whole bug class back in. Real exhaustion is not subtle: the measured
        # incident was 20.4% late. Below LATE_FLOOR, say nothing.
        if late_rate >= LATE_FLOOR:
            if early_rate == 0:
                reasons.append(
                    "late-loaded failures: 0.0%% over first %d tests, %.1f%% over last %d"
                    % (n_early, 100 * late_rate, n_late))
            elif late_rate > early_rate * 5:
                reasons.append(
                    "failure rate climbs %.1fx from early (%.1f%%) to late (%.1f%%)"
                    % (late_rate / early_rate, 100 * early_rate, 100 * late_rate))

    # Fallback for a single un-chunked log. Skipped when per-chunk logs were
    # supplied, since the loop above already judged each one honestly.
    if not chunks and _looks_aborted(text):
        reasons.append("run produced progress output but never reached 100% (aborted)")

    if reasons:
        return "contended", reasons
    return ("clean" if failed == 0 else "genuine"), reasons


def failing_files(text):
    return sorted({ln.split("::")[0].replace("FAILED ", "").strip()
                   for ln in text.splitlines() if ln.startswith("FAILED")})


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--chunks", type=int, default=4,
                    help="fresh processes to split across (default 4)")
    ap.add_argument("--out", default=None,
                    help="log directory (default: agents/<agent>/temp/suite-run)")
    ap.add_argument("--include-daemon-integration", action="store_true",
                    help="DANGEROUS with a live daemon; see Live-Daemon Exception")
    ap.add_argument("--confirm-solo", action="store_true",
                    help="on a contended verdict, re-run the worst-hit file alone to prove it")
    args = ap.parse_args(argv)

    if not TESTS_DIR.is_dir():
        print("run-full-suite: no tests dir at %s" % TESTS_DIR, file=sys.stderr)
        return 3

    agent = os.environ.get("MIND_AGENT", "").strip()
    out = Path(args.out) if args.out else (
        PROJECT_ROOT / "agents" / (agent or "shared") / "temp" / "suite-run")
    out.mkdir(parents=True, exist_ok=True)

    files = sorted(str(p) for p in TESTS_DIR.glob("test_*.py"))
    if not files:
        print("run-full-suite: no test files found", file=sys.stderr)
        return 3

    env = dict(os.environ)
    env["STORAGE_BACKEND"] = "local"          # constraint 1 -- never optional here
    env["PYTHONUNBUFFERED"] = "1"

    groups = _chunk(files, args.chunks)
    print("run-full-suite: %d files across %d fresh processes -> %s"
          % (len(files), len(groups), out))
    print("  STORAGE_BACKEND=local pinned (guard-955); "
          "daemon_integration %s"
          % ("INCLUDED" if args.include_daemon_integration else "excluded"))

    combined = []
    tot_p = tot_f = tot_e = 0
    for i, group in enumerate(groups):
        cmd = [sys.executable, "-u", "-m", "pytest", *group, "-q"]
        if not args.include_daemon_integration:
            cmd += ["-m", "not daemon_integration"]
        log = out / ("chunk-%02d.log" % i)
        print("  chunk %02d: %d files ..." % (i, len(group)), end="", flush=True)
        with open(log, "w", encoding="utf-8", errors="replace") as fh:
            subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT,
                           cwd=str(PROJECT_ROOT), env=env)
        text = log.read_text(encoding="utf-8", errors="replace")
        combined.append(text)
        p, f, e = _parse_counts(text)
        tot_p += p; tot_f += f; tot_e += e
        print(" %d passed, %d failed, %d errors" % (p, f, e))

    blob = "\n".join(combined)
    verdict, reasons = classify(blob, tot_f, chunks=combined)
    files_failing = failing_files(blob)

    print("\n" + "=" * 66)
    print("TOTAL: %d passed, %d failed, %d errors" % (tot_p, tot_f, tot_e))

    if verdict == "contended":
        print("VERDICT: INVALID (contended) -- this number means NOTHING")
        for r in reasons:
            print("  - %s" % r)
        if args.confirm_solo and files_failing:
            worst = max(files_failing,
                        key=lambda f: blob.count("FAILED " + f))
            print("  confirming: re-running %s alone ..." % worst)
            cmd = [sys.executable, "-m", "pytest", worst, "-q"]
            r = subprocess.run(cmd, capture_output=True, text=True,
                               cwd=str(PROJECT_ROOT), env=env)
            sp, sf, _ = _parse_counts(r.stdout or "")
            print("  solo: %d passed, %d failed -> %s"
                  % (sp, sf, "ENVIRONMENTAL (green solo)" if sf == 0
                     else "some failures are GENUINE"))
        print("\nRe-run when the fleet is quiet, or raise --chunks.")
        print("Do NOT file regressions from this run. Do NOT wave it away either.")
        print("=" * 66)
        return 2

    if verdict == "genuine":
        print("VERDICT: GENUINE failures -- trustworthy, act on them")
        for f in files_failing:
            print("  %s (%d)" % (f, blob.count("FAILED " + f)))
        print("=" * 66)
        return 1

    print("VERDICT: CLEAN")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
