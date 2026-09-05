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
import atexit
import configparser
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
TESTS_DIR = SCRIPT_DIR / "tests"          # last-resort fallback; see _testpaths()

# Testpaths that are COLLECTED but run as their OWN pytest invocation rather
# than inside the chunk pool (). This is not a way to skip them --
# run-full-suite.sh runs each one and folds its exit code in, exactly as it
# already does for the invisible-suite and domain halves. It is a way to run
# them in a fresh process that has not just executed ~8,000 other tests.
#
# WHY, measured 2026-07-31 (cc-02 / Linux 6.8.0-136-generic, own-cloud, live
# fleet). mind_api/tests is daemon-heavy: its tests spawn a real daemon and
# talk to it over HTTP. It sorts LAST, so in the chunk pool it always runs
# after everything else, and there it fails en masse -- 411 failures at
# rung 16, 271 at rung 20 -- with HTTP 404s from test-spawned daemons and
# assertions that see the LIVE aspirations queue (897 unexpected goals)
# instead of their fixture. Alone it is fine: the whole tree runs 1128/1135,
# and chunk 15's exact 47-file list re-runs SOLO to 5. So the split is not the
# problem and neither are the tests -- something accumulates across the
# sequential run that fresh chunk processes do not reset. WHICH resource is
# UNMEASURED; do not inherit a mechanism here that nobody has confirmed.
#
# Escalating the ladder does NOT fix it, which is why this is a structural
# exception rather than a rung recommendation: rung 20 merely moved the cliff
# from chunk 14 to chunk 17. And the cost of leaving it in the pool is not
# just noise -- run-full-suite's contention classifier cannot see a tail-chunk
# cluster (), so it certifies these runs "VERDICT: GENUINE --
# trustworthy, act on them". A runner that reports ~285 phantom failures under
# a trustworthy verdict on every deep-code closure is worse than one that runs
# the tree separately: agents either chase ghosts or learn to disbelieve the
# verdict, and the code's own guard-580 comment records where that ends.
#
# Revisit when  (classifier) and  (the 7 real reds) land,
# or when the accumulated resource is identified -- at which point this list
# should shrink to empty.
#
# 2026-08-20 (): it shrank to empty. The "accumulated resource" was
# identified and fixed -- 's process-wide backend-cache poisoning
# (conftest restored the env var but not the derived _ACTIVE_BACKEND), whose
# reset fixture now exists in BOTH test-tree conftests (the mind_api mirror
# landed with this change, closing the mixed-chunk vector). mind_api/tests
# was re-measured on this box before folding: standalone 1,386/1,386 green,
# and own-process at END of invocation (the historically fatal position)
# green. Four genuine reds found by that measurement were fixed, not skipped:
# set_at parity port (guard-2323), claim-sid harness pin, citation lane pin
# (guard-4522), conftest MIND_SID coverage. The mechanism below stays for
# future entries; the set being empty is the designed end state.
DEFERRED_TESTPATHS = set()

# ── Scope declaration for --triage () ─────────────────────────────
#
# triage() globs chunk-*.log and NOTHING ELSE, so its verdict is scoped to the
# chunked pytest half alone. That verdict is HONEST about the population it
# read, which is exactly what makes it dangerous: nothing in its output named
# what it declined to look at, so a "0 genuine" read as a clean SUITE. Measured
# 2026-08-02 (, echo, cc-03, 16 chunks): triage printed
# `2 environmental | 0 genuine` while two shell files in the pytest-invisible
# half were red, and red SOLO -- genuine. Closing on that verdict ships past
# real reds. This is guard-1760 (a runner reports what it RAN, never what it
# declined to look for) and the enumerator-all-clear-boundary pattern: the
# qualifier in an all-clear is self-declared and may be narrower than the
# action it authorizes.
#
# THE HALVES ARE FOUR, NOT TWO. The goal that motivated this named "invisible
# or domain"; the deferred testpath is a third, and it is the one that by
# default does NOT RUN AT ALL -- so silence about it is doubly misleading.
# Keeping the list here rather than in the shell makes it the one place that
# answers "what is outside triage's window".
OTHER_HALVES = (
    ("invisible", "pytest-invisible suites (main()-style .py + all .sh)"),
    ("deferred", "deferred testpaths (NOT RUN unless RUN_DEFERRED=1)"),
    ("domain", "domain test suite (world-provided hook)"),
)

# One JSON object per half, appended by run-full-suite.sh AFTER the chunked
# half returns. It lives in the log dir beside chunk-*.log so a later --triage
# -- which runs nothing and re-reads what a prior run wrote -- can report the
# other halves instead of being silent about them.
#
# WHY A FILE AND NOT A PARSE OF THE RUN LOG: measured on this box 2026-08-10,
# a real log dir contains chunk-*.log, this record, and (since ) the
# per-chunk chunk-*.args lists -- but NO RUN LOG. That absence is the point
# here; the other halves stream to the shell's stdout, which an operator may or
# may not have redirected, and never to a path this tool can find. So the
# summary has to be RECORDED at the moment it is produced or it is gone.
HALVES_RECORD = "halves.jsonl"


def read_halves(out):
    """Read the per-half records a prior run wrote. Missing file -> []."""
    p = Path(out) / HALVES_RECORD
    if not p.is_file():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(r, dict) and r.get("half"):
            rows.append(r)
    return rows


def print_scope_declaration(out):
    """Name every half OUTSIDE triage's window. Returns the failed-half list.

    Printed on EVERY triage path -- clean, contended and genuine alike. The
    clean path is the one that matters: a failing run already prompts the
    reader to look further, while a clean one is where an unstated scope turns
    into "the suite is green". So this must not be conditional on there being
    something to report.
    """
    recorded = {r.get("half"): r for r in read_halves(out)}
    failed = []
    print("\nSCOPE -- this triage read the chunked pytest half ONLY.")
    for key, label in OTHER_HALVES:
        r = recorded.get(key)
        if r is None:
            print("  %-10s NOT RECORDED -- this triage says NOTHING about it. "
                  "%s" % (key, label))
            continue
        if not r.get("ran", True):
            print("  %-10s DID NOT RUN -- %s" % (key, r.get("summary") or label))
            continue
        rc = r.get("rc")
        state = "PASS" if rc == 0 else "FAIL(rc=%s)" % rc
        print("  %-10s %-11s %s" % (key, state, r.get("summary") or label))
        if rc != 0:
            failed.append(key)
    if failed:
        # Said as a sentence, not left to be inferred from a table. The whole
        # defect this block exists to close is a reader taking a clean verdict
        # for a clean suite, and a reader who has just been told "0 genuine"
        # is exactly the reader who will not re-derive it from an rc column.
        print("  => %d half/halves above FAILED. The verdict below is about the "
              "chunked half and does NOT cover them." % len(failed))
    else:
        print("  A verdict below is evidence about the chunked half only.")
    return failed


def _testpaths():
    """Resolve the suite's collection roots from pytest.ini `testpaths`.

    Deliberately NOT a hardcoded list here (g-115-3748). This runner shipped
    2026-07-26 collecting exactly `core/scripts/tests`, five weeks AFTER
    pytest.ini already declared three testpaths — so the project's own
    declaration of what the suite IS and the tool that runs it disagreed, and
    the tool was the newer of the two. Re-hardcoding the three paths here
    would fix today's symptom and rebuild the mechanism: a second source of
    truth, free to drift again the next time a test tree is added.

    What that drift cost, measured 2026-07-31: 109 files / 1448 tests never
    ran. Not merely uncovered — 12 of them were RED, some for over a month,
    and every one of the enforcement layers they guard was therefore
    unverified. The blind spot was invisible precisely because the runner
    reports what it RAN and never what it declined to look for (guard-1760).

    Fail-safe: any parse failure, or a testpaths list naming nothing that
    exists, falls back to the historical single dir. A malformed pytest.ini
    must not take the suite offline — that would trade a silent gap for a
    loud one, and this runner is the thing agents use to prove they have not
    broken anything.
    """
    ini = PROJECT_ROOT / "pytest.ini"
    try:
        cp = configparser.ConfigParser()
        cp.read(ini, encoding="utf-8")
        raw = cp.get("pytest", "testpaths", fallback="")
    except Exception:
        raw = ""
    dirs = []
    for frag in raw.split():
        p = PROJECT_ROOT / frag
        if p.is_dir() and p not in dirs and frag not in DEFERRED_TESTPATHS:
            dirs.append(p)
    return dirs or ([TESTS_DIR] if TESTS_DIR.is_dir() else [])

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


# Windows CreateProcess caps a whole command line here; the budget leaves room
# for the interpreter path, the flags and the environment block, so the refusal
# fires as a legible message rather than as WinError 206 (/guard-5635).
_CMDLINE_CEILING = 32767
_ARGV_BUDGET = 28000


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


def _is_measurement(returncode, p, f, e):
    """True when a pytest process's output may be READ AS A RESULT ().

    ONE predicate, TWO call sites. `_solo` carried it inline and the chunk loop
    carried NOTHING -- it discarded `subprocess.run`'s return value entirely, so
    a chunk that exited 2/3/4/5 was counted exactly like one that exited 0. The
    fix the goal asked for is REUSE, not a second copy: a duplicated validity
    rule is the shape that drifts, and this one already reads as authoritative
    on both paths.

    Both halves are required, and `_solo`'s docstring holds the evidence for
    why: pytest exits 0 or 1 to mean "I ran your tests" (2/3/4/5 are
    interrupted, internal error, usage error and collected-nothing), AND the log
    must account for at least one test. An empty population returns the UNSAFE
    verdict, never the safe one (guard-2166).

    WHAT THIS DOES NOT CATCH, stated here because the goal that commissioned it
    (g-115-8887) prescribed it as the whole remedy and it is not. The defect,
    measured on this tree 2026-09-04, is a process that dies mid-run at ~20%
    with `returncode == 0` and a log whose
    counts still parse non-zero (`_parse_counts` falls back to the progress-dot
    tally, so 648 dots read as 648 passed). That input satisfies BOTH halves
    here and is caught one layer over, by `_has_summary_line` -- see
    `_looks_aborted`. guard-1501 states the same thing from the other side:
    "rc=0 is not the tell; the ABSENT SUMMARY LINE is". This predicate covers
    the spawn, usage and collected-nothing lanes; the completion marker covers
    the death-mid-run lane. Neither subsumes the other.

    Both call sites fail in the SAME direction (refuse to certify), so there is
    no deliberate fail-open/fail-closed split to preserve here -- guard-2373
    applies only when the callers' biases differ, and if a future caller wants
    the other bias it must not get it by editing this function.
    """
    return returncode in (0, 1) and (p + f + e) > 0


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
    return tally[3] < 100 and not _has_summary_line(text)


def _died_silently(text):
    """True when an aborted log ends ON a bare progress row with no error text.

    This is the `os._exit()` signature and it is worth separating from ordinary
    contention because the two have OPPOSITE remedies: contention wants a
    higher chunk rung, a hard exit reproduces at every rung (g-115-9018).

    When a chunk is starved of OS resources, SOMETHING reports it -- a
    traceback, an INTERNALERROR, a Fatal Python error, a WinError/STATUS_ code.
    `os._exit()` reports nothing at all: it skips exception handling, atexit
    and every pytest teardown hook, so the last thing on disk is whatever
    progress row was mid-flight, and the OS is handed status 0.

    Callers must gate this behind `_looks_aborted` -- on its own it says
    nothing, because a healthy chunk that has simply not finished writing its
    summary yet looks identical.

    DELIBERATELY NARROW, and the narrowness is the design. Two sibling causes
    truncate a log the same way and have DIFFERENT remedies, so each exclusion
    below defers to a classification that already exists:

      * NUL bytes  -> log corruption (g-115-3387): the sync layer replaced the
        file while the writer held an fd on the old inode. Remedy is `--out`
        outside the synced tree. Claiming a hard exit here would send the
        reader hunting a watchdog that does not exist.
      * last line ENDS ON a complete `[ NN%]` marker -> an ordinary abort,
        which run-full-suite has always called contention and which keeps the
        chunk-ladder remedy on purpose (pinned by
        test_run_full_suite_hang.test_real_contention_still_reported_as_stopped).

    What is left is the measured signature and only that: the log stops
    PART-WAY THROUGH a progress row -- dots with no closing percentage marker,
    because the interpreter vanished between two markers. Under-claiming is the
    safe direction: a missed silent death falls back to the old generic reason,
    which is merely uninformative, whereas a false positive actively points the
    reader at the wrong file.
    """
    if "\x00" in text:
        return False
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False
    # A BARE progress row -- no trailing `[ NN%]`. That absence is the tell.
    if not re.match(r"^[.FEsxX]+\s*$", lines[-1]):
        return False
    return not re.search(
        r"INTERNALERROR|Traceback \(most recent call last\)|Fatal Python error"
        r"|MemoryError|Killed|STATUS_[A-Z_]+|WinError", text)


# pytest's own summary line, at the START of a line: "648 passed, 3 warnings in
# 41.20s" under -q, "===== 648 passed in 41.20s =====" without it. The leading
# `=*` covers the banner form; `\s*` after it must NOT be allowed to skip over
# prose, which is why there is no `.*` anywhere in this pattern.
_SUMMARY_LINE_RE = re.compile(r"^=*\s*\d+\s+(?:passed|failed|error)", re.M)
# A pytest -q progress row: the dots/letters plus the trailing [ NN%] marker.
_PROGRESS_LINE_RE = re.compile(r"^[.FEsxX]+\s*\[\s*\d+%\]", re.M)


def _has_summary_line(text):
    r"""True when the log carries pytest's OWN terminal summary line (g-115-8887).

    THIS REPLACES `re.search(r"\d+\s+(passed|failed)")`, an UNANCHORED search
    over the whole log, and the replacement is the fix for a measured false
    CLEAN. `_looks_aborted` treats a count line as proof the run finished, so
    ANY substring shaped like one anywhere in the log switched the abort
    detector off. The silent-zero branch does not catch the leftover either --
    it requires `_progress_tally` to be None, and a chunk that died at 21% has
    plenty of progress rows. So the chunk contributed NO reason at all and the
    run printed VERDICT: CLEAN.

    NOT HYPOTHETICAL, AND THE CALLER IS THE SOURCE. Measured on this tree
    2026-09-04: 30 files under core/scripts/tests carry pytest-summary-shaped
    strings in their fixtures ("6 passed in 1.0s", "5 passed, 1 failed in 1.0s",
    "32 passed in 4.0s") -- they are the suite's own self-tests, including this
    runner's. pytest prints a failing test's captured stdout and assertion repr
    into the log, so a chunk holding one of those files spills a count-shaped
    line into its own log as a matter of course. Simulated end to end: a chunk
    stopping at 21% with one such line classified `clean`, reasons `[]`, and
    `_parse_counts` returned (20, 0, 0) -- the STRAY number, not the 648 dots.

    THE DISCRIMINATOR IS ORDER, NOT SHAPE (g-115-8887), and it has to be: a
    test that prints "6 passed in 1.0s" at column 0 produces a line
    byte-identical to pytest's own. What cannot be forged is POSITION. pytest emits its summary AFTER all
    progress output, so the real one is the last count line in the file and it
    sits past the last progress row. A line spilled mid-run has progress rows
    after it. When there is no progress output at all there is nothing to order
    against, so the count line is accepted -- that lane is the silent-zero
    branch's, not this one's, and stealing it here would double-report.

    Deliberately NOT a duration or "in Ns" check: the fixture strings carry
    those too, so shape-matching harder loses to the next fixture. Deliberately
    NOT a tail-window check either: the measured log is nine lines, where "the
    tail" is most of the file.
    """
    counts = list(_SUMMARY_LINE_RE.finditer(text))
    if not counts:
        return False
    progress = list(_PROGRESS_LINE_RE.finditer(text))
    if not progress:
        return True
    return counts[-1].start() > progress[-1].start()


_HANG_RE = re.compile(r"Timeout \((\d+:\d{2}:\d{2})\)!")
_FRAME_RE = re.compile(r'File "([^"]+)", line (\d+) in (\S+)')
# Frames belonging to the runner itself, never to the code that hung.
_FRAME_NOISE = ("/_pytest/", "\\_pytest\\", "/pluggy/", "\\pluggy\\",
                "site-packages", "dist-packages", "<frozen", "/pytest/")


def _hang_marker(text):
    """(duration, file, line, test) for a faulthandler timeout abort, else None.

    A DETERMINISTIC HANG IS NOT CONTENTION, AND THE REMEDIES ARE OPPOSITE
    (g-115-6226). The documented response to a bad verdict is to climb the chunk
    ladder and re-run when quiet; against a hang that is pure waste -- it
    reproduces solo, every time. Measured on DESKTOP-O91DLK2: three runs, two of
    them BYTE-IDENTICAL 6990-byte logs, before the hang was recognised as a hang.

    WHAT THE CLASSIFIER SAID BEFORE THIS EXISTED, measured here on a real Linux
    faulthandler log rather than assumed: `_progress_tally` returns None (a hung
    `-q` run never prints a `[NN%]` marker), so `_looks_aborted` is FALSE and the
    silent-zero branch fires instead -- reporting "log empty, truncated, or
    corrupted". That is a third wrong direction: it points at the NUL/own-cloud
    log-corruption remedy for a run whose log is intact and complete.

    Parsed against REAL output, not an assumed shape -- pytest emits
    `Timeout (0:00:05)!` then `Thread 0x... (most recent call first):` then the
    frames. Frame order is deepest-first, so the deepest NON-RUNNER frame is the
    code that actually hung; a test that hangs inside subprocess would otherwise
    be reported as a subprocess.py defect.
    """
    m = _HANG_RE.search(text)
    if not m:
        return None
    frames = _FRAME_RE.findall(text[m.end():])
    if not frames:
        return (m.group(1), None, None, None)
    # PREFER THE TEST FRAME EXPLICITLY rather than inferring it as "the first
    # non-runner frame". A test that hangs inside subprocess has STDLIB frames
    # below it, and stdlib is not site-packages, so a noise-list alone names
    # subprocess.py as the culprit -- which is the single most likely real shape
    # here, since the whole  defect is a subprocess call that never
    # returns. Caught by test_hang_marker_walks_past_runner_frames on the first
    # run of that test, against the exact stack the incident produced.
    for path, line, func in frames:
        norm = path.replace("\\", "/")
        if norm.rsplit("/", 1)[-1].startswith("test_") or "/tests/" in norm:
            return (m.group(1), path, line, func)
    for path, line, func in frames:
        if not any(n in path for n in _FRAME_NOISE):
            return (m.group(1), path, line, func)
    return (m.group(1), frames[0][0], frames[0][1], frames[0][2])


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
        # NUL-BYTE CORRUPTION CHECK (). Deliberately NOT an elif — a
        # chunk can be BOTH corrupted and aborted, and the two carry OPPOSITE
        # remedies, so both reasons must surface.
        #
        # WHY THIS IS ITS OWN CHECK. The measured incident is written up in the
        # silent-zero comment directly below ("the log had 1532 NUL bytes"), but
        # nothing ever TESTED for them: the evidence was recorded in prose and
        # left unmeasured. Both existing branches can miss it. `_looks_aborted`
        # returns False without progress output, and the silent-zero branch is
        # skipped the moment the log still contains a parseable "N passed" —
        # which a partially-overwritten log usually does. Verified on this box:
        # a log of b"...\x00\x00\x00\x00 [100%]\n5 passed\n" satisfies the
        # has-counts regex, so it passes BOTH branches and certifies GENUINE.
        # A false GENUINE is strictly worse than a false INVALID: INVALID at
        # least refuses to be trusted.
        #
        # THE REMEDIES DIVERGE, and that is the actionable half. The documented
        # response to a bad verdict is to climb the chunk ladder (8-12-16-20-24-28)
        # and re-run. Against corruption that is useless AND destructive: the
        # re-run writes into the same default log dir and OVERWRITES the only
        # artifact that could diagnose it. So this reason names the other
        # remedy explicitly — move the logs off the synced tree with --out.
        #
        # NULs survive `read_text(encoding="utf-8", errors="replace")` because
        # 0x00 is VALID UTF-8; `errors="replace"` only rewrites invalid
        # sequences. Measured on this box before relying on it, so this check
        # needs no signature change and no second read of the file.
        nul = chunk.count("\x00")
        if nul:
            reasons.append(
                "chunk %02d log contains %d NUL byte(s) -- the log was REWRITTEN "
                "while the runner was reading it, so its counts describe a file "
                "that no longer exists. This is CORRUPTION, not contention: do "
                "NOT climb the chunk ladder (a re-run overwrites this evidence). "
                "Re-run with --out pointed OUTSIDE the synced tree." % (i, nul))
        # HANG CHECK (). Its own `if`, like the NUL check above and for
        # the same reason: a hang carries the OPPOSITE remedy to contention, so it
        # must surface even alongside other reasons. It is checked BEFORE the two
        # branches below because it EXPLAINS them -- a hung run has no counts and
        # no final progress marker, and attributing that to corruption sends the
        # reader to the wrong lane entirely.
        hang = _hang_marker(chunk)
        if hang:
            dur, path, line, func = hang
            where = ("%s:%s in %s" % (path, line, func)) if path else "UNKNOWN LOCATION"
            reasons.append(
                "chunk %02d HUNG after %s -- faulthandler aborted it at %s. This "
                "is a DETERMINISTIC HANG, not contention: it reproduces SOLO, so "
                "do NOT climb the chunk ladder and do NOT re-run 'when quiet' "
                "(measured g-115-6226: three runs, two byte-identical logs). "
                "Re-run that ONE file with a short -o faulthandler_timeout=90 to "
                "reproduce in 90s instead of the full window."
                % (i, dur, where))
        if _looks_aborted(chunk):
            tally = _progress_tally(chunk)
            pct = tally[3] if tally else 0
            if _died_silently(chunk):
                # IN-PROCESS HARD EXIT, not contention (). The log
                # ends ON a bare progress row: no traceback, no INTERNALERROR,
                # no signal notice -- the interpreter was removed from under
                # pytest by os._exit(), which bypasses every teardown path and
                # hands the OS status 0. So the chunk "succeeds", contributes
                # nothing after the stop point, and its remaining results are
                # erased in silence.
                #
                # THIS IS THE ONE ABORT THE CHUNK LADDER CANNOT FIX, which is
                # why it gets its own reason instead of riding under
                # `contended`: rungs change how many processes the files are
                # split across, and a hard exit reproduces in every one of
                # them. Measured twice -- chunk 04 at 13% () and
                # chunk 09 at 88% (), the second costing ~2h on the
                # ladder before a solo re-run falsified the contention premise.
                # Both were an uncancelled module-level
                # `threading.Timer(N, lambda: os._exit(0))` reaching a
                # long-running process through an `exec_module` of a script
                # that normally lives milliseconds.
                reasons.append(
                    "chunk %02d DIED SILENTLY at %d%% -- the log ends on a bare "
                    "progress row with no traceback and no summary, i.e. an "
                    "IN-PROCESS HARD EXIT (os._exit) with status 0, not "
                    "contention. Do NOT climb the chunk ladder: no rung fixes "
                    "this and every rung reproduces it. Re-run that chunk's "
                    "file list solo to confirm, then look for a module-level "
                    "self-destruct watchdog reaching the pytest process -- "
                    "`grep -rln 'threading.Timer' core/scripts/*.py` and check "
                    "every exec_module of those files cancels it (guard-2138)."
                    % (i, pct))
            else:
                reasons.append(
                    "chunk %02d stopped at %d%% -- it never finished, so the "
                    "totals are missing its tests" % (i, pct))
        elif not hang and _progress_tally(chunk) is None and not re.search(
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
    #
    # THE HANG CHECK IS REPEATED HERE ON PURPOSE (guard-3448: a gate is only as
    # broad as its entry points). classify() has TWO ways in -- the per-chunk
    # loop above and this un-chunked path -- and a detector wired into only one
    # of them presents as mechanical while being honour-system at the second
    # door. An un-chunked run is exactly how someone reproduces a suspected hang.
    if not chunks:
        hang = _hang_marker(text)
        if hang:
            dur, path, line, func = hang
            where = ("%s:%s in %s" % (path, line, func)) if path else "UNKNOWN LOCATION"
            reasons.append(
                "run HUNG after %s -- faulthandler aborted it at %s. This is a "
                "DETERMINISTIC HANG, not contention: it reproduces SOLO, so do "
                "NOT climb the chunk ladder and do NOT re-run 'when quiet'."
                % (dur, where))
        if _looks_aborted(text):
            reasons.append("run produced progress output but never reached 100% (aborted)")

    if reasons:
        return "contended", reasons
    return ("clean" if failed == 0 else "genuine"), reasons


def failing_files(text):
    return sorted({ln.split("::")[0].replace("FAILED ", "").strip()
                   for ln in text.splitlines() if ln.startswith("FAILED")})


def _stem_forms(path):
    """Query strings to look this failing file up by. BOTH forms are required.

    MEASURED 2026-07-31 (echo, cc-03), and this is the whole reason the
    ownership step is worth automating rather than eyeballing:
    `aspirations-query.sh --title-contains` is a substring match on the TITLE
    ONLY, and goal titles routinely drop the `test_` prefix. Querying
    "test_fleet_config_parity" returns 0 hits; querying "fleet_config_parity"
    returns 3 -- including g-115-3803, the OPEN goal that owns it.

    So an ownership check keyed on the file stem alone reports UNOWNED for a
    tracked test, and the caller then files a duplicate goal. That is the exact
    inversion of what this step exists to prevent, and it fails silently.

    `--goal-field description <name>` is NOT a substitute: it is an EXACT
    field match, not a substring search. It returns 0 on g-115-3803, whose
    description provably contains the literal string (verified in the same
    turn; `--goal-field status pending` returns 915, so the flag works -- it
    just does not mean "contains").
    """
    stem = Path(path).stem
    forms = [stem]
    if stem.startswith("test_"):
        forms.append(stem[len("test_"):])
    return forms


OPEN_STATUSES = ("pending", "in-progress")


def _owning_goals(path, root):
    """Open goals in EITHER queue that name this test, in TITLE **or DESCRIPTION**.

    SEARCHING TITLES ALONE IS NOT ENOUGH, and this cost a near-duplicate filing
    on this feature's first live use (2026-07-31, echo, cc-03). Two genuine reds
    -- test_merge_handlers_commutativity_property and
    test_meta_write_class_conflict_retry -- came back "owner: NONE". They were
    owned: g-115-4310 is pending and its DESCRIPTION names both files. Its title
    is "Fix: merge_backpressure breaks two pins -- not byte-commutative
    (guard-907) and it flipped backpressure.yaml to MERGE-PROTECTED", which
    names the DEFECT, not the test files.

    That is how a good goal title is written, so this is the common case rather
    than an edge one: the better the title, the less likely it contains a test
    filename. `_stem_forms` fixed the query STRING; this fixes the searched
    FIELD, and they are independent -- neither alone finds g-115-4310.

    Implementation note: `aspirations-query.sh` has no description-substring
    filter (`--goal-field` is EXACT match), so this pulls the open queues by
    status in one call per status and substring-scans in-process. That still
    goes through the sanctioned script rather than reading the JSONL directly.

    OPEN statuses only. A COMPLETED goal that named this test is not an owner:
    it means the test was fixed and has regressed, which is precisely a thing to
    file rather than suppress.

    Returns ``(owners, rows_scanned)``. **rows_scanned is not a statistic, it is
    the instrument-failure discriminator.** Both `except Exception: continue`
    and `if r.returncode != 0: continue` below fall through to an empty list,
    which the caller used to render as "owner: NONE -- no goal in either queue
    names this test" -- byte-identical to a true negative, so a daemon-unreachable
    query silently authorises a duplicate filing. Measured 2026-08-01 (zeta,
    cc-02): with `subprocess.run` stubbed to rc=1 (the routine
    daemon-unreachable shape -- and no-python-cli-fallback.md means there is NO
    CLI fallback beneath it) this returns []; the same call against the live
    instrument returns 10 goals. An open queue of ~915 goals returning zero rows
    is never a true negative, so the caller reports UNKNOWN rather than UNOWNED.
    rb-245 exactly: verify the instrument answered before believing its zero.

    Owner rows are 4-tuples ``(goal_id, status, title, strength)``. **strength
    exists because a shared name is not ownership (guard-1801).** `_stem_forms`
    widens the QUERY by stripping `test_` and this function widens the FIELD to
    the whole description, and together they turn a filename lookup into a topic
    search: measured the same turn, `test_fleet_config_parity.py` matched TEN
    open goals, of which exactly one (g-115-3803) owns the failing tests -- the
    rest merely discuss the subsystem. Over-match is the silent direction, since
    a spurious owner suppresses ALL filing and exits 0 printing "every genuine
    red already has an owning goal". So the full `test_<stem>` form wins
    outright when it matches anything, and the stripped form is consulted ONLY
    when the full form finds nothing -- those hits are labelled `weak` and the
    caller prints them as needing verification rather than as settled ownership.
    """
    from _runtime_bash import bash_cmd  # guard-580 (never bare "bash") + guard-581 (.as_posix())
    # Boundary-aware, NOT a bare substring. The stripped form of a short name is
    # a common English fragment: `test_thing.py` yields "thing", which
    # substring-matches "nothing", "something", "anything". That direction of
    # error is the silent one -- spurious owners suppress ALL filing, and a
    # suppressed filing leaves no trace to notice. Caught by this feature's own
    # test on the stripped-form case.
    #
    # The left class excludes letters/digits but DELIBERATELY allows `_`: the
    # stripped form is normally preceded by exactly that, in `test_<form>`.
    # Excluding `_` on the left would break the very match this form exists for.
    def _pat(form):
        return re.compile(r"(?<![A-Za-z0-9])" + re.escape(form.lower())
                          + r"(?![A-Za-z0-9_])")

    forms = _stem_forms(path)
    full_pat = _pat(forms[0])                       # test_<stem> -- exact
    weak_pats = [_pat(f) for f in forms[1:]]        # <stem> -- subsystem-wide
    seen, strong, weak = set(), [], []
    rows_scanned = 0
    for status in OPEN_STATUSES:
        try:
            r = subprocess.run(
                bash_cmd(SCRIPT_DIR / "aspirations-query.sh",
                         "--goal-status", status, "--full"),
                capture_output=True, text=True, cwd=str(root), timeout=120)
        except Exception:
            continue
        if r.returncode != 0:
            continue
        try:
            rows = json.loads(r.stdout or "[]")
        except Exception:
            continue
        if isinstance(rows, dict):
            rows = rows.get("goals") or rows.get("results") or []
        rows = rows or []
        rows_scanned += len(rows)
        for g in rows:
            gid = g.get("goal_id") or g.get("id")
            if not gid or gid in seen:
                continue
            hay = ((g.get("title") or "") + " " + (g.get("description") or "")).lower()
            row = (gid, g.get("status") or "?", (g.get("title") or "")[:64])
            if full_pat.search(hay):
                seen.add(gid)
                strong.append(row)
            elif any(p.search(hay) for p in weak_pats):
                seen.add(gid)
                weak.append(row)
    # The full form wins outright when it matches ANYTHING; the stripped form is
    # a fallback, never a supplement (guard-1801 -- a shared name is not ownership).
    hits, strength = (strong, "exact") if strong else (weak, "weak")
    return [(gid, st, ti, strength) for gid, st, ti in hits], rows_scanned


def _recent_commits(path, root, days=7):
    try:
        r = subprocess.run(
            ["git", "log", "--oneline", "-n", "5", "--since=%d.days" % days,
             "--", path],
            capture_output=True, text=True, cwd=str(root), timeout=60)
    except Exception:
        return []
    return [ln for ln in (r.stdout or "").splitlines() if ln.strip()]


def _solo(path, root, env):
    """Re-run ONE file alone. Green solo => environmental; red solo => genuine.

    The discriminator that falsifies contention in a single measurement
    (guard-1448): with no competing processes there is nothing to contend for,
    so a failure that survives is the code's.

    A RUN THAT EXECUTED NO TESTS IS NOT A GREEN. It reaches `_parse_counts` as
    (0, 0, 0) -- byte-identical to a clean pass -- and the caller's `f == 0`
    branch then prints "-> ENVIRONMENTAL (do not file)" and drops a real red on
    the floor. Measured 2026-08-01 (zeta, hostname cc-02, uname -r
    6.8.0-136-generic): `_parse_counts("")`, `_parse_counts("bash: pytest:
    command not found")` and `_parse_counts("no tests ran in 0.01s")` all return
    (0, 0, 0), and a live `_solo` on a file pytest collects nothing from returns
    (0, 0, None) beside a raw pytest rc=5. Every one of those non-measurements
    lands in the single bucket that suppresses filing.

    So a measurement needs BOTH halves: pytest exited with a code meaning "I ran
    your tests" (0 = all passed, 1 = some failed; 2/3/4/5 are interrupted,
    internal error, usage error and collected-nothing), AND the log accounts for
    at least one test. Anything else returns the error shape, which the caller
    already routes to the COULD-NOT-RUN bucket and counts toward rc=1.

    This is guard-2166 in the small -- an empty population must return the
    UNSAFE verdict, never the safe one -- and classify() already refuses the
    identical laundering one step upstream, calling an unparseable log CONTENDED
    rather than clean.
    """
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", path],
                           capture_output=True, text=True, cwd=str(root),
                           env=env, timeout=1800)
    except Exception as exc:
        return None, None, str(exc)
    p, f, e = _parse_counts(r.stdout or "")
    if not _is_measurement(r.returncode, p, f, e):
        return None, None, (
            "pytest rc=%d accounted for %d test(s) -- executed nothing, so this "
            "is not a measurement" % (r.returncode, p + f + e))
    return p, f + e, None


def _print_ownership(path, root, indent="      "):
    """Step 5. Print who owns this failing file, or say plainly that nobody does.

    Deliberately prints on BOTH branches. A silent "no owner" is
    indistinguishable from "the check did not run", which is how a GENUINE
    failure sat unowned for a day while run-full-suite-after-deep-code.md told
    every reader it was tracked.

    Returns the owner rows, or **None when the ownership query itself did not
    answer** -- which is a third outcome, not a flavour of "no owner". The
    caller must not fold None into the unowned bucket: unowned means file it,
    unknown means the instrument is broken and nothing has been established.
    """
    owners, rows_scanned = _owning_goals(path, root)
    commits = _recent_commits(path, root)
    if rows_scanned == 0:
        # Not "nobody owns this" -- "nobody was asked". Both failure paths in
        # _owning_goals return an empty list, so without this branch a
        # daemon-unreachable query reads as a clean true negative.
        print("%sowner: UNKNOWN -- ownership query returned no goals at all "
              "(instrument failure, not evidence)" % indent)
        owners = None
    elif owners:
        for gid, status, title, strength in owners:
            note = "" if strength == "exact" else \
                "  <- WEAK match on the subsystem name, not the test file; verify"
            print("%sowner: %s [%s] %s%s" % (indent, gid, status, title, note))
    else:
        # SAY WHAT WAS SEARCHED, NOT WHAT WAS CONCLUDED (, guard-4432).
        # This used to read "no goal in either queue names this test (N open
        # goal(s) scanned)". Two problems, and N is what made them invisible:
        # it counts goals SCANNED, not fields or statuses SEARCHED, so a
        # four-digit N reads as exhaustive coverage and the reader stops.
        # (a) The scan is title+description, which is broad but not everything
        #     -- a goal citing the test only in an outcome_note is not found.
        # (b) OPEN statuses ONLY, deliberately (a completed goal naming this
        #     test means a REGRESSION, which is a thing to file, not to
        #     suppress). But an unqualified "no goal in either queue" reads as
        #     ALL goals, so the one exclusion most likely to explain a
        #     surprising NONE is the one the message hides.
        # guard-4432 is the general form: a literal-token scan that finds zero
        # may report "not found"; it may not assert the positive conclusion,
        # and above all may not attach an action instruction to it.
        print("%sowner: NONE -- no %s goal matched '%s' in title or description "
              "(%d goal(s) scanned; outcome_note and completed goals NOT searched)"
              % (indent, "/".join(OPEN_STATUSES), Path(path).stem, rows_scanned))
    if commits:
        print("%srecent commits (7d): %s" % (indent, commits[0]))
    else:
        print("%srecent commits (7d): none" % indent)
    return owners


def triage(out, root, env):
    """Consume ALREADY-WRITTEN chunk logs and triage them. Does NOT re-run the suite.

    The run half of this tool has always printed a verdict; nothing covered what
    to do when that verdict is not CLEAN, so the four-step triage was re-derived
    by hand every time (gap-053: twice in two days, a full iteration each). Every
    input is already on disk in the chunk logs, so this reads them rather than
    paying for another ~30min run.

    Order matters and is not arbitrary:
      1. positional bucket + completeness -- reuses classify(), so the verdict
         here can never disagree with the verdict the run printed
      2. solo re-run per candidate    -- falsifies contention in ONE measurement
      3. ownership, per genuine red   -- the step that keeps getting skipped
      4. report only what survives all three
    """
    logs = sorted(out.glob("chunk-*.log"))
    if not logs:
        print("run-full-suite --triage: no chunk-*.log in %s" % out, file=sys.stderr)
        print("  --triage reads the logs a run wrote; run the suite first, or "
              "point --out at the directory that has them.", file=sys.stderr)
        return 3

    combined = [p.read_text(encoding="utf-8", errors="replace") for p in logs]
    blob = "\n".join(combined)
    tot_f = sum(_parse_counts(t)[1] for t in combined)
    verdict, reasons = classify(blob, tot_f, chunks=combined)
    candidates = failing_files(blob)

    print("=" * 66)
    print("TRIAGE: %d chunk log(s) in %s" % (len(logs), out))
    print("verdict on record: %s | %d failing file(s)"
          % (verdict.upper(), len(candidates)))
    for r in reasons:
        print("  - %s" % r)

    # BEFORE the verdict is acted on, not after: the reader must know what this
    # window excludes while they are still deciding what the numbers mean.
    failed_halves = print_scope_declaration(out)

    if not candidates:
        # A contended verdict with NOTHING in the FAILED list is the common and
        # most deceptive shape: every per-chunk line reads "0 failed" and the
        # TOTAL looks like a pass. The defect is an incomplete chunk, not a
        # failing test, so there is nothing to solo-run -- say so explicitly
        # rather than printing an empty table that reads as all-clear.
        print("\nNo FAILED lines to triage.")
        if verdict == "contended":
            print("The problem is COMPLETENESS, not a failing test: a chunk did "
                  "not finish, so the totals are missing its tests.")
            print("Re-run with more --chunks; do not read the totals above.")
            print("=" * 66)
            return 2
        if failed_halves:
            print("Nothing to triage in the CHUNKED half -- but %s FAILED "
                  "(see SCOPE above)." % ", ".join(failed_halves))
        print("=" * 66)
        return 1 if failed_halves else 0

    print("\nStep 2-3: solo re-run + ownership, per candidate")
    genuine_unowned, genuine_owned, environmental, errored = [], [], [], []
    ownership_unknown = []
    for path in candidates:
        n = blob.count("FAILED " + path)
        print("\n  %s (%d failure line(s) in the run)" % (path, n))
        p, f, err = _solo(path, root, env)
        if err is not None:
            print("      solo: COULD NOT RUN (%s) -- not classified" % err[:120])
            errored.append(path)
            continue
        if f == 0:
            print("      solo: %d passed, 0 failed -> ENVIRONMENTAL (do not file)" % p)
            environmental.append(path)
            continue
        print("      solo: %d passed, %d failed -> GENUINE" % (p, f))
        owners = _print_ownership(path, root)
        if owners is None:
            # Instrument failure. NOT unowned -- nothing was established, so this
            # candidate is unclassified and must keep the exit code non-zero.
            ownership_unknown.append(path)
        else:
            (genuine_owned if owners else genuine_unowned).append(path)

    print("\n" + "=" * 66)
    print("TRIAGE RESULT: %d environmental | %d genuine-owned | %d genuine-UNOWNED"
          % (len(environmental), len(genuine_owned), len(genuine_unowned)))
    if errored:
        print("  %d candidate(s) could not be re-run -- unclassified: %s"
              % (len(errored), ", ".join(errored)))
    if ownership_unknown:
        print("  %d genuine red(s) with an UNANSWERED ownership query -- "
              "unclassified, do NOT read as unowned: %s"
              % (len(ownership_unknown), ", ".join(ownership_unknown)))
    if genuine_unowned:
        print("\nFILE THESE -- genuine, reproduce solo, and no goal names them:")
        for path in genuine_unowned:
            print("  %s" % path)
    elif not errored and not ownership_unknown:
        # These two are NOT the same finding and must not share a sentence.
        # "every genuine red is owned" says reds exist and are tracked; "none
        # reproduced" says the run's failures were not real. Collapsing them
        # would report a fully-environmental run as though it had confirmed
        # regressions under management.
        if genuine_owned:
            print("\nNothing to file: every genuine red already has an owning goal.")
        else:
            print("\nNothing to file: no candidate reproduced solo -- all %d were "
                  "environmental, so the run's failures were not regressions."
                  % len(environmental))
    # A "nothing to file" sentence is precisely where a scoped verdict gets read
    # as a whole-suite all-clear, so the exclusion is restated at the point of
    # the conclusion rather than only in the header block.
    if failed_halves:
        print("\nBut do NOT read the above as a clean suite: %s FAILED "
              "(see SCOPE above). This triage did not examine %s."
              % (", ".join(failed_halves), " or ".join(failed_halves)))
    print("=" * 66)
    return 1 if (genuine_unowned or errored or ownership_unknown
                 or failed_halves) else 0


def _pytest_expands_argfile(out, env):
    """Does THIS pytest turn `@<file>` into the arguments inside that file?

    PROBED, NEVER ASSUMED. The @argfile chunking landed 2026-09-03 (5b7089537)
    on the stated premise that "pytest reads arguments from a file given an `@`
    prefix (argparse fromfile_prefix_chars)". That premise is FALSE on pytest
    7.4.4, which does not set `fromfile_prefix_chars` at all -- measured on
    cc-07 2026-09-04: zero occurrences of the token in
    `_pytest/config/argparsing.py`, and `pytest @<file>` naming ONE real test
    file collected 0 tests where the same path on argv collected 25.

    THE FAILURE IS QUIET IN THE WAY THAT MATTERS. pytest treats the unexpanded
    `@<path>` as a test path, prints `ERROR: file or directory not found` at
    rc=4 and collects NOTHING -- so every chunk log carries a warnings block
    and NO summary line. _parse_counts reads (0,0,0) from each and classify()
    calls the run INVALID (contended): a verdict that sends the reader up the
    chunk ladder, which can NEVER fix it (run-full-suite-after-deep-code.md
    item 3 -- INVALID has two causes and the ladder only fixes one). Measured
    2026-09-04: all 4 chunks, 1,365 files, 0 tests run, on a box where the
    same tree had run 15,513 tests four hours earlier.

    The original change was RIGHT ABOUT ITS OWN PROBLEM -- guard-5635's
    Windows CreateProcess ceiling is real and argv-length-bound. So support is
    USED where it exists and fallen back from where it does not, rather than
    reverted: a POSIX box has a ~2MB ARG_MAX and never needed the argfile.

    AND THE POSITIVE CONTROL WAS NOT SKIPPED -- that is the instructive part.
    `test_pytest_still_honours_an_argfile` was written as an external-contract
    pin for exactly this, and it WORKED: measured at HEAD on cc-07 2026-09-04
    it was the ONE failure in its file, naming the cause in a single line. The
    gap was never a missing test. It is that the pin's answer DEPENDS ON THE
    BOX -- green where the change was authored (Windows), red on Linux/pytest
    7.4.4 -- and nothing ran it on a box of the second kind before the change
    propagated there. A gate only reachable on hardware nobody runs it on is
    not a gate. That test is now a probe-AGREEMENT pin, which holds on every
    box, and this function is what makes the disagreement survivable.

    Returns True only on positive evidence. Any error or unexpected shape reads
    False: argv is the mechanism that demonstrably works here, so an unreadable
    probe must degrade toward it, not away from it.

    ANSWERED IN-PROCESS, DELIBERATELY -- DO NOT "IMPROVE" THIS BACK INTO A
    SPAWN. The first shipped form ran the real binary (`pytest @<tmpfile>`
    carrying `--version`, cwd=PROJECT_ROOT, timeout=60). That is the more
    direct measurement and it was the wrong call, on two counts:

      1. IT TIMED OUT, AND THE BARE `except` TURNED THAT INTO A CONFIDENT WRONG
         ANSWER. Measured on cc-04 (Linux 6.8.0-138-generic, pytest 9.1.1)
         2026-09-04: `subprocess.TimeoutExpired` after the full 60s -- pytest
         starting up inside this project root is not a cheap process, whatever
         the argfile asks it to do -- caught by `except Exception` and returned
         as False. This box DOES expand argfiles, so the probe was selecting
         the branch that collects NOTHING. A 60s wrong answer is strictly worse
         than no probe at all.
      2. main()'s spawns are OBSERVED STATE. Five tests in
         test_run_full_suite_fleet_layout.py read `pytest_cmds[0]`, and the
         probe's own pytest silently became that.

    Its own agreement pin caught (1) -- not a suite run, not a reader. At HEAD,
    test_the_argfile_probe_agrees_with_this_pytest was the single red and named
    the disagreement in one line. That is the pin working exactly as designed.

    Matching `fromfile_prefix_chars=` (WITH the `=`) looks for the KWARG in
    argparsing's source, not the bare word in prose. 0.042s, and BOTH controls
    were run before this shipped: the token is present here (True) and a
    deliberately-bogus near-token is not (False), so this reads a real
    distinction rather than always answering yes (guard-4414).

    THE SPLIT IS BY PYTEST VERSION, MEASURED ON BOTH SIDES (folded in from the
    g-115-8876 twin of this function at the 2026-09-04 merge, which reached the
    same answer independently): pytest 7.4.4 on cc-02 REFUSES the argfile
    (fromfile_prefix_chars unset); pytest 9.0.2 on DESKTOP-O91DLK2 accepts it
    and collects identically to direct paths. Two boxes disagreeing is the
    whole reason this is probed rather than version-compared -- a version
    boundary would encode a cutoff nobody on this fleet has measured, and the
    attribute IS the thing that decides.

    `out` and `env` are unused now. The signature is kept because the call site
    and the harness stub in test_run_full_suite_chunk_spawn.py both pass them.
    """
    try:
        import inspect
        from _pytest.config import argparsing
        return "fromfile_prefix_chars=" in inspect.getsource(argparsing)
    except Exception:
        return False


def _git_head(root):
    """Current HEAD sha, or None when it cannot be read.

    Returns None rather than raising: a suite run must never fail because the
    tree is not a git checkout, git is missing, or the call hangs. Callers MUST
    render None as "NOT RUN" and never as "unchanged" -- a check that reports
    what it RAN but stays silent about what it declined to look for is how a
    detector becomes decorative (guard-1760, the same defect that hid the
    three-testpaths gap for five weeks).
    """
    # The RESULT is guarded as well as the call, deliberately: the suite's own
    # tests stub subprocess.run and some stubs RETURN None instead of raising,
    # so a try/except around only the call still dies on `r.returncode`. Caught
    # by test_run_full_suite_triage.py the first time this shipped.
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"],
                           capture_output=True, text=True,
                           cwd=str(root), timeout=10)
        if r is None or r.returncode != 0:
            return None
        return (r.stdout or "").strip() or None
    except Exception:
        return None


# How many commits the offender range READS, and how many the verdict PRINTS.
# Both caps are load-bearing and were set from a measurement, not a guess: on
# this fleet `HEAD~5..HEAD` resolves to THIRTY commits, because a merge brings
# a whole branch into the range and the worker loop merges on every turn-end.
# Uncapped, a voided run prints a screen of merge commits and the machine
# record becomes a multi-kilobyte log line.
_OFFENDER_READ_LIMIT = 50
_OFFENDER_SHOW_LIMIT = 15


def _git_offenders(root, start_sha, end_sha, limit=_OFFENDER_READ_LIMIT):
    """Commits reachable from end_sha but not start_sha, newest first.

    -> [{"sha", "author", "subject"}, ...], or None when the range cannot be
    read. None and [] are DIFFERENT answers and callers MUST render them
    differently: None means "could not look", [] means "looked and the range
    is empty" -- which is itself a finding, because HEAD moved yet nothing is
    in the range, i.e. a reset or rebase moved it BACKWARDS. Collapsing the
    two is the same guard-1760 defect _git_head's docstring already warns
    about: a detector that reports what it RAN but stays silent about what it
    declined to look for.

    Merges are deliberately NOT excluded. The worker loop's Phase -0.3 pull
    integrates origin commits with `git merge --no-edit`, so the merge commit
    is frequently the offender itself.

    Fail-open like _git_head: a suite run must never fail because git could
    not answer a question about who disturbed it.
    """
    try:
        r = subprocess.run(
            ["git", "log", "--pretty=format:%h\x1f%an\x1f%s",
             "-n", str(limit), "%s..%s" % (start_sha, end_sha)],
            capture_output=True, text=True, cwd=str(root), timeout=15)
        if r is None or r.returncode != 0:
            return None
        rows = []
        for line in (r.stdout or "").splitlines():
            parts = line.split("\x1f", 2)
            if len(parts) == 3:
                rows.append({"sha": parts[0], "author": parts[1],
                             "subject": parts[2]})
        return rows
    except Exception:
        return None


# One greppable line per voided run ( item 3). The human-readable
# verdict beside it is the by-product; THIS is the line that lets a later
# cadence count voided runs per week and per offending Body.
VOID_RECORD_PREFIX = "SUITE-VOID-RECORD:"


def _render_offenders(offenders):
    """-> the verdict's offender lines for `offenders` (the _git_offenders result).

    EXTRACTED SO IT CAN BE TESTED (guard-5867): this text only ever prints on
    the tree-moved branch, and driving main() to that branch means running a
    real suite whose HEAD moves underneath it. Inline, the branch was verifiable
    only by re-typing its logic in a scratch script and eyeballing the output --
    which tests the copy, not the code. A pure list-returning helper lets the
    None / empty / capped / normal cases each be asserted directly.
    """
    if offenders is None:
        return ["  Offending commits: COULD NOT READ (git log failed) -- "
                "this is 'did not look', NOT 'there were none'."]
    if not offenders:
        return ["  Offending commits: range is EMPTY. HEAD moved but nothing "
                "is reachable from finish that was not reachable from launch "
                "-- a reset or rebase moved HEAD BACKWARDS. The run is still "
                "void; the cause is not a new commit."]
    authors = sorted({c["author"] for c in offenders})
    capped = len(offenders) >= _OFFENDER_READ_LIMIT
    lines = ["  Offending commits: %s%d, by %s"
             % ("AT LEAST " if capped else "", len(offenders),
                ", ".join(authors))]
    for c in offenders[:_OFFENDER_SHOW_LIMIT]:
        lines.append("    %s  %-18s %s"
                     % (c["sha"], c["author"][:18], c["subject"][:72]))
    if len(offenders) > _OFFENDER_SHOW_LIMIT:
        lines.append("    ... and %d more not shown"
                     % (len(offenders) - _OFFENDER_SHOW_LIMIT))
    if capped:
        lines.append("  The range read stopped at %d commits, so the count "
                     "above is a FLOOR, not a total -- a merge brings a whole "
                     "branch into the range." % _OFFENDER_READ_LIMIT)
    lines.append("  That list is the COMMITTED half only. A suite is ALSO "
                 "voided by an UNCOMMITTED mid-run edit to the code under "
                 "test, which moves no sha and which this runner is "
                 "structurally blind to (guard-5987) -- so do not read the "
                 "list above as a complete account of what disturbed the run.")
    return lines


def _emit_void_record(cause, **fields):
    """Print the machine-readable void record. Never raises.

    Emitted on EVERY path that returns 2, not only tree-moved. A cadence that
    counted only tree-moved voids would under-report exactly the way the
    unattributed verdict this supplements already does, and a contract key
    present on some exit paths but not others is guard-3948's defect --
    an exact-equality test over the causes is what catches the one you miss.
    """
    rec = {"cause": cause,
           "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "agent": os.environ.get("MIND_AGENT", "") or None,
           "sid": os.environ.get("MIND_SID", "") or None}
    rec.update(fields)
    try:
        print("%s %s" % (VOID_RECORD_PREFIX, json.dumps(rec, sort_keys=True)))
    except Exception:
        pass


def _populated_agents(agents_root_dir=None):
    """Agent dirs on THIS box carrying an identity file. -> (names|None, reason)

    Filed by omni from ZDS-Mind (g-306-325): a run-full-suite there reported 132
    GENUINE unowned failures, 45 files, every one a fixture that assumes a
    POPULATED MULTI-AGENT layout -- a second agent dir with real content, a live
    peer to probe, or forked worker Bodies. On a single-agent deployment those
    assumptions are false, the code under test behaves CORRECTLY, and the test
    fails anyway. Since run-full-suite-after-deep-code makes a green suite the
    closure criterion for every deep core/scripts change, that criterion is
    UNSATISFIABLE there, and a permanently-red suite is indistinguishable from
    one nobody reads.

    self.md is the evidence, not local-paths.conf and not the team-state roster:

      - `local-paths.conf` is WRONG here and the mistake is pre-recorded. Per
        agent-dir-resolution.md, on any given box only the RESIDENT agent has a
        conf (cc-04 has one for alpha alone), so conf-enumeration "would silently
        degrade fleet mode to single-agent" -- i.e. it would produce exactly the
        false single-agent verdict this function must never produce.
      - The team-state roster is wrong for a DIFFERENT reason, and this is the
        subtle one: it is the LIVE FLEET roster, and the fixtures do not read it.
        They read local agent dirs. On ZDS the roster names several agents while
        `agents/alpha`, `agents/delta` and `agents/zeta` hold only `session/` --
        so a roster-based count would say "fleet", the clause would not fire, and
        the suite would stay red. LOCAL-box evidence is not a degraded proxy for
        the roster here; it is the correct question. Do not "fix" this to use
        team-state.
      - self.md is what `fleet_config_parity._has_agent_identity` uses for the
        same discriminator ("is this roster row an agent at all"), backed by
        rb-4246 and guard-1574 (never resolve a fleet member by NAME alone).
        The predicate is duplicated rather than imported to keep the test runner
        free of a dependency on a checker module that pulls in yaml +
        _team_state; the rule it encodes is stable and cited above.

    Returns (None, reason) when the check could not run -- callers MUST NOT read
    that as a single-agent verdict. Mirrors `_has_agent_identity`'s root probe:
    without it a missing or misresolved agents root makes every lookup a
    confident False, i.e. N negatives instead of N unknowns, which is the vacuous
    pass (`mandatory-step-vacuity`: a step that silently has nothing to do is
    indistinguishable from one that ran clean).
    """
    if agents_root_dir is None:
        try:
            sys.path.insert(0, str(SCRIPT_DIR))
            from _paths import agents_root          # the ONLY sanctioned resolver
            agents_root_dir = agents_root()
        except Exception as e:                       # noqa: BLE001
            return None, "agents root unresolved (%s)" % e
    try:
        root = Path(agents_root_dir)
        if not root.is_dir():
            return None, "agents root is not a directory: %s" % root
        names = sorted(d.name for d in root.iterdir()
                       if d.is_dir() and (d / "self.md").is_file())
        return names, None
    except OSError as e:
        return None, "agents root unreadable (%s)" % e


# ── The log dir is shared, so treat it like one ──────────────────────────────
# Two distinct defects, one dir (). The dir is keyed only on agent
# name, so every run by one agent on one box lands in the SAME place.
#
# (a) EVIDENCE DESTRUCTION. The stale-chunk clear below used to be a bare
#     unlink() of chunk-*.log -- a hard delete of records the system cannot
#     regenerate (a 10-hour run's logs), which is precisely the anti-pattern
#     archive-before-delete.md names. MEASURED 2026-09-04: a run finished at
#     03:09 and a second run started 05:24 deleted its chunk logs while they
#     were still being read for triage. The invariant "one run per log dir"
#     is right and stays; what was wrong was achieving it by destruction.
#     Move-aside is the form that rule prefers (step 5), and one slot bounds
#     the growth at 2 runs -- the previous run survives, the one before it
#     does not, deliberately: an unbounded archive in a temp dir is a disk
#     leak, and a pruner would put a delete path back.
#
# (b) CONCURRENT STOMP, which is worse and was entirely unguarded. The
#     working-tree lock in run-full-suite.sh does NOT cover this: it is keyed
#     on sid, returns 0 for your own sid, and a BACKGROUNDED run inherits no
#     MIND_SID at all, so it takes no lock while still printing
#     authoritative-looking chunk counts. Two live runs interleaving into one
#     dir produce a verdict assembled from two measurements.
# Refusal texts live as module constants, not inline literals: they are long,
# they are the only thing a refused caller sees, and a test asserts on their
# content rather than on a substring buried in main().
WORKTREE_DAEMON_REFUSAL_TEXT = """run-full-suite: REFUSING -- %s.
  The worktree spawns its own daemon, and mind-api-start.sh's orphan sweep
  matches mind_api.src processes by COMMAND LINE with no runtime-dir scoping,
  so it KILLS the fleet's live daemon -- once per chunk gap. Every daemon-backed
  test then fails on a stale port, and the run reports a large,
  authoritative-looking failure count that is PURE ENVIRONMENT (guard-5866).
  Copying daemon.port does not fix this and neither does a symlink: the kill is
  the defect, the stale port is only its most visible symptom.
  Use the daemon-safe MAIN-REPO route instead -- run from the main checkout with
  STORAGE_BACKEND=local, chunked, and simply do not commit while it runs.
  Deliberate exception: --override-worktree-daemon "<justification>"."""

CONCURRENT_RUN_REFUSAL_TEXT = """run-full-suite: REFUSING -- another run holds this log dir (%s).
  Two runs sharing one log dir produce a verdict assembled from two different
  measurements, and the later run clears the earlier one's chunk logs.
  The working-tree lock in run-full-suite.sh does NOT cover this: it is keyed on
  sid, returns 0 for your own sid, and a BACKGROUNDED run inherits no MIND_SID
  at all, so it takes no lock while still printing authoritative chunk counts.
  Wait for the other run, give this one its own --out, or pass
  --override-concurrent-run "<justification>".
  If you are certain nothing is running, delete %s."""


RUN_LOCK_NAME = ".run-lock.json"
# Longer than the longest measured run on the slowest box (10h01m, alpha
# DESKTOP-O91DLK2, 24 chunks, 2026-09-03). A TTL shorter than a real run would
# let a second run steal the lock mid-flight -- the exact collision it exists
# to prevent.
RUN_LOCK_TTL_SECONDS = 12 * 3600


def _pid_alive_platform(pid):
    """True / False / None -- and on Windows it actually answers.

    tree_lock._pid_alive returns None for a DEAD pid on Windows, because
    os.kill(pid, 0) raises a bare OSError there. The run lock's only other
    escape is the 12h TTL, so a hard kill of a suite run wedges the runner for
    half a day. Not hypothetical: the FIRST production crash of this lock did
    exactly that (2026-09-04, pid 23180 killed mid-chunk-01, atexit never fired,
    lock held with liveness unknowable). OpenProcess + GetExitCodeProcess is
    the question Windows can answer.

    Every uncertain path returns None so the caller's fail-direction is
    unchanged when this genuinely cannot tell: ERROR_ACCESS_DENIED (a live
    process owned by another user) and a handle that will not read both stay
    UNKNOWN rather than being guessed. Only ERROR_INVALID_PARAMETER -- which is
    Windows for "there is no such pid" -- returns False.
    """
    if os.name != "nt":
        from tree_lock import _pid_alive
        return _pid_alive(pid)
    try:
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        ERROR_INVALID_PARAMETER = 87
        STILL_ACTIVE = 259
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False if ctypes.get_last_error() == ERROR_INVALID_PARAMETER else None
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return None
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    except Exception:  # noqa: BLE001 -- cannot tell is not the same as dead
        return None


def evaluate_run_lock(record, my_pid, now, ttl=RUN_LOCK_TTL_SECONDS,
                      pid_alive=None):
    """Pure decision: {'blocked': bool, 'reason': str}.

    Split from the IO so the whole truth table is unit-testable with no
    filesystem and no live process, mirroring tree_lock.evaluate.

    UNKNOWN LIVENESS FALLS BACK TO THE TTL, and on Windows that is the ONLY
    path that ever frees a lock: os.kill(dead_pid, 0) raises a bare OSError
    there, so _pid_alive returns None -- not False -- for a dead holder
    (verified on this box: _pid_alive(999999) -> None). A design that broke
    the lock only on an explicit False would wedge every future run on
    Windows, which is the platform this runner is most used on.
    """
    if pid_alive is None:
        pid_alive = _pid_alive_platform
    if not isinstance(record, dict):
        return {"blocked": False, "reason": "no lock"}
    pid = record.get("pid")
    started = record.get("started_at")
    if pid == my_pid:
        return {"blocked": False, "reason": "our own lock"}
    age = None
    if isinstance(started, (int, float)):
        age = now - started
    alive = pid_alive(pid) if isinstance(pid, int) else None
    if alive is False:
        return {"blocked": False, "reason": "holder pid %s is gone" % pid}
    if age is not None and age >= ttl:
        return {"blocked": False,
                "reason": "lock is %.1fh old (TTL %.1fh) -- stale"
                          % (age / 3600.0, ttl / 3600.0)}
    # Say WHICH of the two blocking cases this is. `alive is True` is a
    # confirmed live holder and the operator should wait; `alive is None` is
    # UNCONFIRMED -- on Windows that is every dead holder, because os.kill
    # raises a bare OSError there. Both block, but they call for opposite
    # operator actions, and a message that reads equally confident in both
    # cases sends someone away to wait out a holder that died hours ago.
    # A log-mtime progress check was considered as a second signal and
    # rejected: chunk logs are block-buffered, so a slow buffered chunk can
    # look stalled, and unblocking on that would readmit the concurrent run
    # this lock exists to prevent.
    return {"blocked": True,
            "reason": "pid %s holds this log dir%s%s"
                      % (pid,
                         "" if age is None else " (started %.1fh ago)"
                         % (age / 3600.0),
                         "" if alive else
                         " -- LIVENESS UNCONFIRMED on this platform, so the "
                         "holder may already be dead")}


def read_run_lock(out):
    """The lock record, or None. Absent/unreadable/malformed all collapse to
    None on purpose -- each means "no evidence anyone holds this dir", and a
    gate that blocks on its own read error violates guard-142."""
    try:
        data = json.loads((Path(out) / RUN_LOCK_NAME).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def take_run_lock(out, pid=None, now=None):
    """Best-effort. A dir we cannot write is a dir whose logs will fail loudly
    a moment later; never abort the run over the lock file itself."""
    rec = {"pid": pid if pid is not None else os.getpid(),
           "started_at": now if now is not None else time.time(),
           "argv": sys.argv[1:]}
    try:
        (Path(out) / RUN_LOCK_NAME).write_text(json.dumps(rec), encoding="utf-8")
    except OSError:
        pass
    return rec


def release_run_lock(out, pid=None):
    """Remove only OUR lock. Releasing a peer's would hand the dir to a third
    run while the peer is still writing into it."""
    me = pid if pid is not None else os.getpid()
    rec = read_run_lock(out)
    if isinstance(rec, dict) and rec.get("pid") != me:
        return False
    try:
        (Path(out) / RUN_LOCK_NAME).unlink()
        return True
    except OSError:
        return False


def rotate_prior_logs(out):
    """Move the previous run's chunk logs + halves record into out/prev/.

    Returns the number of files rotated. Clears the top level exactly as the
    unlink did -- --triage still globs one run's logs -- but the prior run
    stays readable for one more cycle.
    """
    out = Path(out)
    prior = sorted(out.glob("chunk-*.log")) + sorted(out.glob("chunk-*.args"))
    rec = out / HALVES_RECORD
    if rec.is_file():
        prior.append(rec)
    if not prior:
        return 0
    prev = out / "prev"
    try:
        if prev.is_dir():
            for stale in prev.iterdir():
                try:
                    stale.unlink()
                except OSError:
                    pass
        prev.mkdir(parents=True, exist_ok=True)
    except OSError:
        return 0
    moved = 0
    for p in prior:
        try:
            p.replace(prev / p.name)
            moved += 1
        except OSError:
            # Could not move it aside -> LEAVE IT. Falling back to unlink here
            # would restore the exact destruction this function exists to stop.
            pass
    return moved

# ── Constraint 4: never run pinned-in-a-worktree beside a live daemon ────────
# guard-5866. The worktree spawns its OWN daemon, and mind-api-start.sh
# _sweep_orphan_daemons matches mind_api.src processes by COMMAND LINE with
# ZERO runtime-dir scoping -- so its spawn-time sweep KILLS the fleet's live
# daemon, once per chunk gap. The copied daemon.port then goes stale and every
# daemon-backed test fails `REFUSED: recycle/spawn requested from inside
# pytest`: a large, authoritative-looking count that is PURE ENVIRONMENT.
# Measured as a one-variable pre-registered control (bravo cc-05 2026-09-03:
# 3 daemon kills + 12 stale-port errors in the worktree vs 0 and 0 for the SAME
# suite/commit/box in the main repo); reproduced alpha cc-04 2026-09-04.
#
# WHY A CHECK AND NOT PROSE (). The guardrail existed 12h before the
# run that tripped it; what lagged was _full_suite_imperative.py, the always-on
# PreToolUse block whose entire job is to carry this warning -- by 29 HOURS.
# Hand-maintained prose in a second file is a delivery channel that fails
# silently, so the condition is decided here, in the tool, where it cannot lag.
#
# FAIL-OPEN BY CONTRACT (guard-142): every probe below collapses an error into
# "not established", so a gate bug can never block a legitimate run. Only two
# POSITIVE readings refuse.
def _git_dir_pair(root):
    """(git_dir, common_dir) resolved absolute, or None when git cannot say.

    Not --absolute-git-dir / --path-format=absolute: both are newer flags, and
    an old git on one box would make this probe error -- i.e. fail open -- on
    exactly the boxes most likely to need it. Plain --git-dir/--git-common-dir
    are ancient and portable; resolving them against root does the rest.
    """
    out = {}
    for flag in ("--git-dir", "--git-common-dir"):
        # The WHOLE per-flag block is inside the try, not just the spawn.
        # Wrapping only subprocess.run() left the dereference below outside the
        # fail-open contract, and a subprocess.run that returns an unexpected
        # object -- None, a stub, a Mock -- raised AttributeError straight out
        # of a gate that guard-142 requires to fail OPEN on its own dependency
        # errors. Caught by test_run_full_suite_chunk_spawn, which stubs
        # subprocess.run to None: 19 tests went red on a gate that was supposed
        # to be invisible to them ().
        try:
            p = subprocess.run(["git", "-C", str(root), "rev-parse", flag],
                               capture_output=True, text=True, timeout=15)
            if p.returncode != 0 or not p.stdout.strip():
                return None
            out[flag] = (Path(root) / p.stdout.strip()).resolve()
        except Exception:  # noqa: BLE001 -- no usable git answer, no verdict
            return None
    return out["--git-dir"], out["--git-common-dir"]


def _live_daemon_port(main_root):
    """The port a daemon is ACTUALLY listening on for main_root, else None.

    The port FILE is not the signal -- a stale file outlives its daemon, and
    refusing on a stale file would block runs on a box with no daemon at all.
    Connecting is the signal.
    """
    try:
        raw = (Path(main_root) / "mind_api" / "state" / "daemon.port").read_text(
            encoding="utf-8").strip()
        port = int(raw)
    except Exception:  # noqa: BLE001 -- absent/unreadable/garbage = no daemon proven
        return None
    if not (0 < port < 65536):
        return None
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return port
    except Exception:  # noqa: BLE001 -- nothing listening = no live daemon
        return None


def worktree_daemon_refusal(root):
    """Refusal detail when BOTH conditions hold, else None (proceed).

    Split from main() so the two-condition truth table is unit-testable by
    monkeypatching the two probes, with no git tree and no socket.
    """
    pair = _git_dir_pair(root)
    if pair is None:
        return None                       # git unreadable -> not established
    git_dir, common_dir = pair
    if git_dir == common_dir:
        return None                       # main checkout -> the safe case
    main_root = common_dir.parent         # <main>/.git -> <main>
    port = _live_daemon_port(main_root)
    if port is None:
        return None                       # no live daemon -> worktree is fine
    return ("this is a LINKED WORKTREE (%s) and a LIVE mind_api daemon is "
            "listening on port %d for the main checkout at %s"
            % (git_dir, port, main_root))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--chunks", type=int, default=4,
                    help="fresh processes to split across (default 4)")
    ap.add_argument("--triage", action="store_true",
                    help="triage the chunk logs already in --out; does NOT re-run the suite")
    ap.add_argument("--out", default=None,
                    help="log directory (default: <tmpdir>/ayoai-suite-run-<agent>, "
                         "deliberately OFF the synced tree -- see g-115-6409)")
    ap.add_argument("--include-daemon-integration", action="store_true",
                    help="DANGEROUS with a live daemon; see Live-Daemon Exception")
    ap.add_argument("--fleet-layout", choices=("auto", "include", "exclude"),
                    default="auto",
                    help="fleet_layout-marked tests (fixtures needing a populated "
                         "multi-agent layout). auto=exclude only when this box has "
                         "<2 agent dirs with a self.md; include/exclude force it")
    ap.add_argument("--confirm-solo", action="store_true",
                    help="on a contended verdict, re-run the worst-hit file alone to prove it")
    ap.add_argument("--print-out-dir", action="store_true",
                    help="resolve and print the log dir, then exit (for run-full-suite.sh)")
    ap.add_argument("--override-worktree-daemon", metavar="JUSTIFICATION",
                    help="proceed despite worktree+live-daemon (guard-5866). "
                         "Justification is REQUIRED and is echoed into the run "
                         "header, which is what run-full-suite.sh captures -- so "
                         "the audit trail rides with the verdict a reader sees")
    ap.add_argument("--override-concurrent-run", metavar="JUSTIFICATION",
                    help="proceed despite another run holding this log dir. "
                         "Justification is REQUIRED and echoed as above")
    args = ap.parse_args(argv)

    # Resolved BEFORE the testpaths check so --print-out-dir answers even on a
    # box whose pytest.ini is unusable: the shell needs somewhere to record the
    # other halves' results precisely when the framework half is the thing that
    # failed. The mkdir on that path is a temp log dir and costs nothing.
    # DEFAULT IS OFF THE SYNCED TREE, and that is the whole point ().
    # This used to default to agents/<agent>/temp/suite-run. Under own-cloud that
    # directory is a FLEET-SYNCED surface (guard-3422), and the sync layer REPLACES
    # a file at a NEW INODE while a writer still holds an fd on the old one -- the
    # writer then keeps writing into an orphaned inode nobody will ever read.
    # Chunk logs are exactly that shape: line ~1027 opens the log and holds the fd
    # across a multi-minute subprocess.run(stdout=fh).
    #
    # Measured 2026-08-17 (alpha, hostname cc-04, uname -r 6.8.0-137-generic,
    # own-cloud). Paired control, same producer and flags, ~1 min apart:
    #   redirect into agents/<agent>/temp/ -> 0 bytes, rc=0
    #   redirect into tempfile.gettempdir() -> 129,157 bytes, rc=0
    # An inode watch caught the swap directly: ino=2010435 size=0 -> ino=2009953
    # size=551, then frozen while the producer ran 71 more seconds. Clean prefix,
    # ZERO NUL bytes, rc=0 -- so the corruption is indistinguishable from a short
    # run, which is why it read as "contended" for three false INVALID verdicts.
    # Duration is the discriminator, not size: a 13.2 MB fast write survives and a
    # 60-second trickle does not.
    #
    # gettempdir() (not a hardcoded /tmp) honours TMPDIR/TEMP/TMP, so this stays
    # portable to the MSYS2 and WSL2 boxes; it returns an absolute path, keeping
    # the guard-552 resolver contract. --out still overrides for anyone who wants
    # the logs kept somewhere durable.
    agent = os.environ.get("MIND_AGENT", "").strip()
    out = Path(args.out) if args.out else (
        Path(tempfile.gettempdir()) / ("ayoai-suite-run-" + (agent or "shared")))
    out.mkdir(parents=True, exist_ok=True)

    # One resolver, two callers (). run-full-suite.sh appends the
    # per-half records that --triage later reads, and it must write them into
    # the SAME dir this resolution picked -- re-deriving the default in bash
    # would be a second source of truth free to drift from this one.
    if args.print_out_dir:
        print(str(out))
        return 0

    testpaths = _testpaths()
    if not testpaths:
        print("run-full-suite: no tests dir resolved (pytest.ini testpaths "
              "named nothing that exists, and %s is absent)" % TESTS_DIR,
              file=sys.stderr)
        return 3

    if args.triage:
        # Constraint 1 governs the solo re-runs too -- they are real pytest
        # invocations against the real tree, so an unpinned backend can collide
        # on the production S3 key exactly as a full run would (guard-955).
        env = dict(os.environ)
        env["STORAGE_BACKEND"] = "local"
        env["PYTHONUNBUFFERED"] = "1"
        return triage(out, PROJECT_ROOT, env)

    # Constraint 4 (guard-5866) and the concurrency check run HERE: after
    # --triage returns (triage re-reads logs, it never runs tests, so a
    # worktree is harmless there) and BEFORE anything touches the prior run's
    # logs. A refusal that fired after the rotation would still have moved a
    # peer's evidence.
    if args.override_worktree_daemon:
        print("  OVERRIDE (guard-5866 worktree+daemon): %s"
              % args.override_worktree_daemon)
    else:
        detail = worktree_daemon_refusal(PROJECT_ROOT)
        if detail:
            print(WORKTREE_DAEMON_REFUSAL_TEXT % detail, file=sys.stderr)
            return 3

    # ONE run per log dir, always. --triage globs chunk-*.log, so a leftover
    # chunk from an earlier run at a DIFFERENT --chunks count silently joins the
    # evidence for this one. MEASURED 2026-07-31 (echo, cc-03) on this feature's
    # own first live use: a 16-chunk run left chunk-16..19 behind from a 20-chunk
    # run 7.5h earlier, and --triage read 20 logs for a 16-chunk run. Those four
    # happened to carry no FAILED lines, so the verdict was right by luck -- a
    # stale FAILED would have injected a phantom candidate, and a stale
    # INCOMPLETE chunk would have made classify() call a healthy run contended.
    # This tool exists to refuse an invalid measurement; it must not assemble one
    # out of two runs.
    #
    # A CONCURRENT run is the case the paragraph above does not cover: those
    # logs are not stale, they are LIVE, and clearing them corrupts a
    # measurement that is still being taken. Check before touching anything.
    if args.override_concurrent_run:
        print("  OVERRIDE (concurrent run): %s" % args.override_concurrent_run)
    else:
        verdict = evaluate_run_lock(read_run_lock(out), os.getpid(), time.time())
        if verdict["blocked"]:
            print(CONCURRENT_RUN_REFUSAL_TEXT
                  % (verdict["reason"], out / RUN_LOCK_NAME), file=sys.stderr)
            return 3
    take_run_lock(out)
    atexit.register(release_run_lock, out, os.getpid())

    # The prior run's logs move ASIDE rather than being deleted (see
    # rotate_prior_logs). The halves record travels with them for the same
    # reason it was cleared with them: a stale PASS reads as coverage.
    rotated = rotate_prior_logs(out)
    if rotated:
        print("  rotated %d file(s) from the previous run -> %s"
              % (rotated, out / "prev"))

    # Non-recursive glob per root, matching pytest's own default discovery for
    # these trees (all three are flat -- verified 2026-07-31: 0 nested test
    # files across 761). A root that grows subdirectories will need rglob.
    # Merge note (echo, 2026-07-31): this multi-root form REPLACED a single-root
    # `TESTS_DIR.glob(...)` that this box's lineage still carried. Keeping the
    # single-root line would have silently defeated _testpaths() entirely --
    # TESTS_DIR is now only the last-resort fallback (see its definition), so
    # the two are not interchangeable and this is not a cosmetic conflict.
    files = sorted(str(p) for d in testpaths for p in d.glob("test_*.py"))
    if not files:
        print("run-full-suite: no test files found under %s"
              % ", ".join(str(d.relative_to(PROJECT_ROOT)) for d in testpaths),
              file=sys.stderr)
        return 3

    env = dict(os.environ)
    env["STORAGE_BACKEND"] = "local"          # constraint 1 -- never optional here
    env["PYTHONUNBUFFERED"] = "1"

    groups = _chunk(files, args.chunks)
    # Name the roots, not just the file count. A reader who cannot see WHICH
    # trees ran cannot tell coverage from a silently-narrowed scope -- which
    # is the exact failure this line now reports against ().
    print("run-full-suite: %d files from %s across %d fresh processes -> %s"
          % (len(files),
             ", ".join(str(d.relative_to(PROJECT_ROOT)) for d in testpaths),
             len(groups), out))
    print("  STORAGE_BACKEND=local pinned (guard-955); "
          "daemon_integration %s"
          % ("INCLUDED" if args.include_daemon_integration else "excluded"))

    # fleet_layout decision (). ALWAYS narrated, never silent: this
    # clause NARROWS what runs, and a runner that reports what it ran but not
    # what it declined to look for is how the mind_api gap hid for five weeks
    # (guard-1760). The evidence is printed with the verdict so a reader can
    # check the premise instead of trusting the label.
    agent_names, roster_err = _populated_agents()
    if args.fleet_layout == "auto":
        if agent_names is None:
            # UNKNOWN is not single-agent. Fail toward running MORE tests: a
            # false "fleet" costs some red tests on a single-agent box, a false
            # "single-agent" silently deletes coverage everywhere else.
            exclude_fleet_layout = False
            fleet_why = "auto -> INCLUDED (roster undeterminable: %s)" % roster_err
        elif len(agent_names) < 2:
            exclude_fleet_layout = True
            fleet_why = ("auto -> excluded (effectively single-agent: %d agent "
                         "dir(s) with self.md%s)"
                         % (len(agent_names),
                            (" -- " + ", ".join(agent_names)) if agent_names else ""))
        else:
            exclude_fleet_layout = False
            fleet_why = ("auto -> INCLUDED (%d agent dirs with self.md: %s)"
                         % (len(agent_names), ", ".join(agent_names)))
    else:
        exclude_fleet_layout = (args.fleet_layout == "exclude")
        fleet_why = "forced --fleet-layout %s" % args.fleet_layout
    print("  fleet_layout %s: %s"
          % ("excluded" if exclude_fleet_layout else "INCLUDED", fleet_why))

    # : record HEAD BEFORE the first chunk. The chunk file lists are
    # computed AT LAUNCH, so a merge landing mid-run hands later chunks paths
    # that may no longer exist -- chunk 00 runs against one tree and the rest
    # against another, and the TOTAL is a mixed-tree number that means nothing.
    # Measured 2026-08-18 on cc-07: the worker loop's Phase -0.3 pull integrated
    # 4 origin commits mid-run, two of them DELETING files the later chunks had
    # already been handed. DETECT, do not prevent: the runner cannot own the
    # loop's pull policy, but it can refuse to certify a run whose tree moved.
    head_at_launch = _git_head(PROJECT_ROOT)

    # Whether the @argfile indirection below is available is a property of the
    # INSTALLED pytest, not of this code, so it is measured once per run rather
    # than assumed. False here is the normal, healthy Linux path.
    argfile_ok = _pytest_expands_argfile(out, env)
    if not argfile_ok:
        print("  @argfile unsupported by this pytest (fromfile_prefix_chars "
              "unset) -- chunk paths go on argv as REPO-RELATIVE paths "
              "instead (fine on POSIX; see _pytest_expands_argfile, g-115-8876)")

    combined = []
    tot_p = tot_f = tot_e = 0
    rc_invalid = []
    rc_unreadable = []
    for i, group in enumerate(groups):
        # NO -q HERE, DELIBERATELY (). pytest.ini:12 already sets
        # `addopts = -q`; passing it again makes -qq, which SUPPRESSES the final
        # summary line. _parse_counts then reads (0,0,0) from every chunk log and
        # the whole verdict path silently computes from nothing, while the run
        # still exits 0 and still prints dots to [100%] so it looks healthy.
        # Measured both directions on cc-02 2026-08-29: with -q, zero lines match
        # "[0-9]+ passed"; without it, "20 passed, 2 warnings in 0.50s".
        # ARGV LENGTH IS A CHUNK'S REAL LIMIT, NOT ITS FILE COUNT ().
        # Windows CreateProcess caps a command line near 32,767 chars, so a
        # chunk's cost is (mean path length x files per chunk) -- guard-5635's
        # measured product. MEASURED on DESKTOP-O91DLK2 2026-09-03: 1,358 files
        # at an 83.7-char mean put the default --chunks 4 at a 29,824-char argv
        # IN THE MAIN REPO -- 9% under the ceiling, and rising with every test
        # file added. From a worktree (longer prefix, +26 chars per path) the
        # same chunk measured 38,664 and died `FileNotFoundError: [WinError 206]`
        # BEFORE collection. So this is not a worktree-only problem deferred by
        # raising --chunks; the default is on track to cross on its own.
        # WHERE pytest SUPPORTS IT, an `@` prefix reads arguments from a file
        # (argparse fromfile_prefix_chars), making argv a constant ~6 items no
        # matter how many files the chunk holds or how deep the tree sits -- the
        # ceiling stops being reachable instead of being sized around. SUPPORT
        # IS NOT UNIVERSAL AND IS NOT ASSUMED: pytest 7.4.4 does not enable it,
        # and using it there collects ZERO tests while looking like contention.
        # `argfile_ok` above is the measured answer; see _pytest_expands_argfile.
        # CHUNK COUNT IS DELIBERATELY LEFT ALONE. Sizing chunks to a byte budget
        # would fix the same product, but the chunk ladder is a retry protocol
        # and the per-chunk diagnostics keyed on it (guard-1448 chunk-confinement,
        # the chunk-09 signature) are read by index -- silently splitting a
        # requested 4 into 6 would change the thing those readings depend on.
        # ONLY PATHS GO IN THE FILE; FLAGS STAY ON ARGV. A value containing
        # spaces (`-m "not daemon_integration and not fleet_layout"`) would ride
        # on argparse's one-arg-per-line convert_arg_line_to_args, and the -m
        # clause is ~50 chars -- every byte of the bloat is the paths.
        # A malformed argfile is LOUD, not silent: verified 2026-09-03 that both
        # a nonexistent path and an MSYS-form path yield pytest's
        # `ERROR: file or directory not found` at rc=4 rather than a quiet
        # 0-collected, so a broken chunk cannot read as an empty-but-fine one.
        # CORRECTED 2026-09-04 (). That verification tested only BAD
        # inputs, and the conclusion it licensed -- "a broken chunk cannot read
        # as an empty-but-fine one" -- was FALSE for the very case it enabled.
        # `@argfile` is argparse's fromfile_prefix_chars, which pytest 7.4.4
        # does not set, so on that pytest a PERFECTLY VALID argfile is itself
        # the rc=4 usage error. rc was then discarded (see the capture below),
        # _parse_counts read (0,0,0), and classify() called it `contended` --
        # so the loud failure this comment promised arrived as a quiet INVALID
        # verdict blaming contention, on EVERY run, on every box with an older
        # pytest. A positive control on a bad input certifies the INPUT, never
        # the QUESTION (guard-4512).
        argfile = out / ("chunk-%02d.args" % i)
        argfile.write_text("\n".join(group) + "\n", encoding="utf-8")
        # The .args file is written EITHER WAY: it is a run artifact this
        # module's docstring already promises, --triage reads the chunk file
        # lists back out of it, and it is the per-chunk diagnostic that lets a
        # reader re-run exactly this chunk's file list solo (the guard-1448
        # discriminator) -- all of which matter on the fallback path too.
        if argfile_ok:
            cmd = [sys.executable, "-u", "-m", "pytest", "@" + str(argfile)]
        else:
            # REPO-RELATIVE paths, not absolute. This is what makes the
            # fallback safe rather than a revert to the pre- state:
            # argv still scales with the file count, but it no longer scales
            # with WHERE THE REPO LIVES, and the prefix was the whole of
            # guard-5635 (a worktree added +26 chars per path and pushed
            # 29,824 -> 38,664). Measured on this repo 2026-09-04: mean path
            # 84.7 chars absolute vs 52.7 relative; at the default --chunks 4
            # that is 28,975 -> 18,031, i.e. 45% under the ceiling and
            # INVARIANT to the worktree prefix.
            cmd = [sys.executable, "-u", "-m", "pytest"]
            cmd += [os.path.relpath(p, str(PROJECT_ROOT)) for p in group]
        # ONE -m carrying every clause. A SECOND -m does not AND with the first;
        # pytest keeps only the last and SILENTLY DISCARDS the earlier one.
        # Measured 2026-08-19 on test_daemon_orphan_prevention.py: `-m "not
        # daemon_integration"` alone collected 0 files, and adding a second
        # harmless `-m` collected 1 -- i.e. the daemon exclusion vanished. Two
        # -m flags here would quietly repeal the Live-Daemon Exception (constraint
        # 2 in this module's docstring) and let the suite hijack the live daemon
        # out from under the running fleet. Append terms to this list, never a
        # second -m.
        marker_terms = []
        if not args.include_daemon_integration:
            marker_terms.append("not daemon_integration")
        if exclude_fleet_layout:
            marker_terms.append("not fleet_layout")
        if marker_terms:
            cmd += ["-m", " and ".join(marker_terms)]
        # ARGV BUDGET -- CHECKED BEFORE THE SPAWN, so the fallback path fails
        # with a sentence naming its own remedy instead of a WinError 206 that
        # a reader has to decode. Deliberately NOT a re-chunk: the ladder is a
        # retry protocol and its per-chunk diagnostics are read by index (see
        # the CHUNK COUNT note above), so this refuses and tells the caller to
        # raise --chunks -- which is exactly what guard-5635 already prescribes.
        # Unreachable on the argfile path, where argv is a constant ~6 items.
        argv_len = sum(len(a) + 1 for a in cmd)
        if argv_len > _ARGV_BUDGET:
            print(" ARGV TOO LONG")
            print("\n" + "=" * 66)
            print("VERDICT: INVALID (chunk %02d argv %d chars > budget %d) -- "
                  "this run means NOTHING" % (i, argv_len, _ARGV_BUDGET))
            print("  This pytest does not read @argfiles, so the chunk's file "
                  "list must ride on argv, and this chunk's list does not fit "
                  "under the %d-char CreateProcess ceiling." % _CMDLINE_CEILING)
            print("  REMEDY: raise --chunks (fewer files per chunk). "
                  "guard-5635 measured 16 working where the default 4 died.")
            print("  Refusing BEFORE the spawn: a chunk that cannot run must "
                  "not be parsed as a chunk that found nothing.")
            _emit_void_record("argv-too-long", chunk=i, argv_len=argv_len,
                              budget=_ARGV_BUDGET)
            return 2
        log = out / ("chunk-%02d.log" % i)
        print("  chunk %02d: %d files ..." % (i, len(group)), end="", flush=True)
        try:
            with open(log, "w", encoding="utf-8", errors="replace") as fh:
                # BIND THE RESULT (, and  independently).
                # This call discarded it, so a chunk that exited 4 (usage
                # error) or 5 (collected nothing) was counted exactly like one
                # that exited 0 -- the chunk loop was the one pytest call site
                # in this module with no rc check at all, while `_solo` twelve
                # hundred lines up had the correct one inline.
                proc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT,
                                      cwd=str(PROJECT_ROOT), env=env)
        except OSError as exc:
            # A CHUNK THAT NEVER SPAWNED IS NOT A CHUNK THAT PASSED ().
            # Until this branch existed the OSError escaped main(), and
            # `sys.exit(main())` rendered it as rc=1 -- so run-full-suite.sh,
            # which is careful and DOES separate did-not-run from ran-and-failed,
            # faithfully reported "rc=1 genuine failures" over a half in which
            # zero tests executed. The wrapper was not wrong; it was handed a
            # dishonest code. Exit 2 instead, reusing the "INVALID, re-measure"
            # contract callers already honour (same reuse as the tree-move check
            # below, rather than inventing a fourth exit code).
            # FATAL, NOT SKIP-AND-CONTINUE: the remaining chunks cannot rescue a
            # verdict that is already uncertifiable, and another ~30 minutes of
            # green-looking chunk lines is precisely what makes a missing half
            # invisible (guard-1760 -- a runner reports what it RAN, never what
            # it declined to look for).
            # KEPT EVEN THOUGH THE @argfile ABOVE SHOULD MAKE WinError 206
            # UNREACHABLE. The next argv-shaped surprise will carry a different
            # errno; the value of this branch is refusing to certify, which does
            # not depend on knowing the cause.
            not_run = sum(len(g) for g in groups[i:])
            print(" SPAWN FAILED")
            print("\n" + "=" * 66)
            print("VERDICT: INVALID (chunk %02d could not spawn) -- this run "
                  "means NOTHING" % i)
            print("  %s" % exc)
            print("  %d of %d test files never ran (chunks %02d-%02d of %d)."
                  % (not_run, len(files), i, len(groups) - 1, len(groups)))
            print("  Counts before the failure -- NOT a result: %d passed, "
                  "%d failed, %d errors." % (tot_p, tot_f, tot_e))
            if getattr(exc, "winerror", None) == 206:
                print("  WinError 206 is the command line exceeding the "
                      "32,767-char CreateProcess ceiling.")
                if argfile_ok:
                    print("  The chunk file list went via @argfile to keep argv "
                          "constant, so an argv this long means something OTHER "
                          "than the file count grew -- inspect cmd, not --chunks.")
                else:
                    # NOT "raise --chunks" (origin's wording, correct before
                    # the pre-spawn budget check existed): _ARGV_BUDGET now
                    # refuses over-long argv BEFORE the spawn, so reaching a
                    # WinError 206 here means the budget itself is too close
                    # to the 32,767-char ceiling.
                    print("  This pytest does NOT support @argfile, so the file "
                          "list rides on argv. The pre-spawn budget check "
                          "should have refused first -- if it did not, the "
                          "budget (%d) is too close to the ceiling."
                          % _ARGV_BUDGET)
            print("  A spawn failure is a setup fault, not a test regression: "
                  "do not triage it as a red, and do not file goals from the "
                  "partial counts above.")
            _emit_void_record("chunk-spawn-failed", chunk=i,
                              error=str(exc), files_never_run=not_run)
            print("=" * 66)
            return 2
        # A CHUNK THAT SPAWNED BUT RAN NOTHING IS NOT A CHUNK THAT PASSED
        # (). The OSError branch above covers the chunk that never
        # STARTED; this covers the one that started, refused, and exited -- and
        # until now that rc was DISCARDED (`subprocess.run(...)` with no
        # binding), so pytest's rc=4 usage error reached _parse_counts as an
        # ordinary log, parsed to (0, 0, 0), and classify() reported the whole
        # run as `contended`. The operator was then sent up the chunk ladder,
        # which can never fix a usage error. Same lesson as the branch above,
        # one step later in the lifecycle, and the same remedy: refuse to
        # certify. This is the module's own idiom -- _rerun_solo already says
        # "executed nothing, so this is not a measurement" for exactly this
        # shape; the chunk loop simply never applied it to itself.
        # 0 = passed, 1 = tests failed, 5 = nothing collected (legitimate when a
        # marker deselects a whole chunk). 2/3/4 and anything else mean pytest
        # did not run this chunk's tests.
        # THE RESULT IS GUARDED AS WELL AS THE CALL, exactly as _git_head above
        # documents and for the same reason: the suite's own tests stub
        # subprocess.run and some stubs RETURN None instead of raising, so
        # reading `proc.returncode` unguarded dies on the stub. An unreadable rc
        # fails toward CONTINUING -- aborting a 30-minute run over a missing
        # attribute that only a stub can produce would be the cure being worse
        # than the disease.
        rc_chunk = getattr(proc, "returncode", None)
        if rc_chunk is not None and rc_chunk not in (0, 1, 5):
            not_run = sum(len(g) for g in groups[i:])
            print(" RC=%d" % rc_chunk)
            print("\n" + "=" * 66)
            print("VERDICT: INVALID (chunk %02d exited rc=%d without running "
                  "its tests) -- this run means NOTHING" % (i, rc_chunk))
            # SAME SENTENCE the `_is_measurement` collector below emits, on
            # purpose (2026-09-04 merge of  with ). The two
            # checks are complements, not rivals: this one is a pure rc-SET
            # test for the SYSTEMIC faults (2/3/4) where every later chunk will
            # fail identically, so it aborts and saves the hours; the collector
            # is a per-chunk validity test over rc AND counts, so it continues.
            # A reader must not have to learn two vocabularies for one
            # condition, and grepping either phrase must find both lanes.
            print("  chunk %02d is NOT A MEASUREMENT -- pytest rc=%d ran none "
                  "of its tests. rc 2/3/4 mean interrupted, internal error and "
                  "usage error. This is a SETUP fault, not contention: do NOT "
                  "climb the chunk ladder -- re-read chunk-%02d.log for "
                  "pytest's own error." % (i, rc_chunk, i))
            print("  %d of %d test files never ran (chunks %02d-%02d of %d)."
                  % (not_run, len(files), i, len(groups) - 1, len(groups)))
            print("  Counts before the failure -- NOT a result: %d passed, "
                  "%d failed, %d errors." % (tot_p, tot_f, tot_e))
            if rc_chunk == 4:
                print("  rc=4 is pytest's USAGE error, not a test failure. If "
                      "the log says `file or directory not found: @...` then "
                      "this pytest does not read @argfiles and the probe that "
                      "should have caught it (_pytest_expands_argfile) "
                      "disagreed with reality -- fix the probe, not --chunks.")
            print("  Chunk log: %s" % log)
            print("  This is a setup fault, not a test regression: do not "
                  "triage it as a red, and do NOT climb the chunk ladder -- a "
                  "retry cannot clear it.")
            # Emit the same verdict line the general rc check below would have
            # produced for this chunk (). The two features met here in
            # a merge and disagreed about who owns the rc!=0 exit: that check
            # COLLECTS and continues so --triage still gets every chunk log,
            # while this rc=4 path STOPS at the first refusing chunk because a
            # usage error cannot be cleared by running more chunks. Both are
            # right; only the reporting was lost. Stopping is kept (it is this
            # path's whole point) and the verdict is restored, so a reader gets
            # the same words for the same condition on either path.
            #
            # ⚠ THIS BLOCK NOW PRINTS THE "NOT A MEASUREMENT" VERDICT TWICE --
            # once ~28 lines above and once here, with DIFFERENT text ("ran none
            # of its tests" vs "accounted for 0 test(s)", and rc 2/3/4 vs
            # 2/3/4/5). Not introduced by the merge that added _emit_void_record
            # below: measured on origin/main itself, which carries three
            # occurrences of the phrase where this branch carried two. Two agents
            # each restored the verdict into this block at different offsets, so
            # git auto-merged both without a conflict -- guard-1849's DOUBLED
            # shape, and the hazard worker-loop's own Phase -0.2 comment
            # narrates. Left in place DELIBERATELY rather than deleted inside a
            # merge resolution, where a silent deletion of a peer's landed line
            # gets no review; relayed as sq-013 for an owner. Note the second
            # copy hardcodes 0 for its count, so it always reads "0 test(s)".
            print("  chunk %02d is NOT A MEASUREMENT -- pytest rc=%d accounted "
                  "for %d test(s). rc 2/3/4/5 mean interrupted, internal error, "
                  "usage error and collected-nothing; none of them ran your "
                  "tests." % (i, rc_chunk, 0))
            _emit_void_record("chunk-rc-without-running", chunk=i,
                              rc=rc_chunk, files_never_run=not_run)
            print("=" * 66)
            return 2
        text = log.read_text(encoding="utf-8", errors="replace")
        combined.append(text)
        p, f, e = _parse_counts(text)
        tot_p += p; tot_f += f; tot_e += e
        rc = getattr(proc, "returncode", None)
        if rc is None:
            # rc UNREADABLE IS A THIRD OUTCOME, NOT "FINE". `_git_head` twenty
            # lines up documents the same hazard and guards the same way: this
            # module's own tests stub subprocess.run and several stubs RETURN
            # None rather than a CompletedProcess, so reading `.returncode`
            # unguarded dies inside six harnesses that are about argv length and
            # chunk counts, not about this check. Production always yields a
            # CompletedProcess, so this branch is reachable only under a stub --
            # and it still NARRATES, because a check that quietly declines to
            # run reports success by default (guard-1760). The completion-marker
            # half is unaffected and still judges this chunk.
            rc_unreadable.append(i)
        elif not _is_measurement(rc, p, f, e):
            # NOT a second predicate -- the same `_is_measurement` `_solo` uses
            # ( outcome 2). Collected rather than returned so the
            # remaining chunks still run and their logs still land on disk:
            # --triage reads chunk-*.log, and aborting here would destroy the
            # evidence for the very chunk being reported.
            rc_invalid.append(
                "chunk %02d is NOT A MEASUREMENT -- pytest rc=%d accounted for "
                "%d test(s). rc 2/3/4/5 mean interrupted, internal error, usage "
                "error and collected-nothing; none of them ran your tests. Its "
                "%d passed below is NOT a result and the TOTAL includes it. "
                "This is a SETUP fault, not contention: do NOT climb the chunk "
                "ladder -- re-read chunk-%02d.log for pytest's own error."
                % (i, rc, p + f + e, p, i))
        print(" %d passed, %d failed, %d errors" % (p, f, e))

    blob = "\n".join(combined)
    if rc_unreadable:
        print("  exit-code check: NOT RUN for chunk(s) %s (the process object "
              "carried no returncode) -- those chunks are NOT certified against "
              "a setup fault; the completion-marker check still applies."
              % ", ".join("%02d" % i for i in rc_unreadable))
    verdict, reasons = classify(blob, tot_f, chunks=combined)
    if rc_invalid:
        # FIRST in the list because an rc fault EXPLAINS whatever classify()
        # then says about the same chunk's truncated log, exactly as the hang
        # check is ordered ahead of the two branches it explains. Forcing
        # "contended" reuses the existing INVALID/exit-2 contract callers
        # already honour rather than inventing a fourth verdict; the remedy
        # divergence rides in the reason STRING, which is how the NUL check
        # already carries "do NOT climb the ladder".
        reasons = rc_invalid + list(reasons)
        verdict = "contended"
    files_failing = failing_files(blob)

    print("\n" + "=" * 66)
    print("TOTAL: %d passed, %d failed, %d errors" % (tot_p, tot_f, tot_e))

    # NO-COUNTS RECOVERY POINTER (). All three zero means no chunk log
    # yielded a pytest summary line, so every verdict below is computed from
    # nothing -- and the caller cannot tell that silence from a normal red. The
    # analysis is NOT lost: --triage re-reads the chunk logs already on disk and
    # re-runs each failing file solo, recovering the full attribution in ~1min
    # with no re-run. Measured 2026-08-29 on cc-04 (alpha): the runner exited 1
    # after only its 4 header lines, twice, and --triage then returned
    # "1 environmental | 16 genuine-owned | 0 genuine-UNOWNED" from those same
    # logs. The recovery path existed the whole time and was undocumented AT THE
    # CALL SITE, which is the part that cost the time -- the filer did not know
    # the flag existed. Printing it here is deliberately cheaper than
    # root-causing why the chunks emit no summary, which is still open and is a
    # SEPARATE question from the parent's own lost output (the chunks already
    # run with -u; only the parent does not).
    if (tot_p, tot_f, tot_e) == (0, 0, 0) and list(Path(out).glob("chunk-*.log")):
        print("  NO COUNTS PARSED -- no chunk log carried a pytest summary line.")
        print("  This is NOT a clean run and NOT a normal red; the verdict below "
              "is computed from nothing.")
        print("  RECOVER WITHOUT RE-RUNNING: run-full-suite.sh --triage")
        print("  (reads the chunk-*.log files already in %s and solo-classifies "
              "each failure)" % out)

    # Tree-move check runs BEFORE every other verdict and outranks all of them
    # (). A mixed-tree run is uninterpretable in BOTH directions: a
    # CLEAN is the dangerous case (looks certified, certifies nothing) and a
    # GENUINE invites filing regressions against files the merge changed or
    # deleted. Exit 2 reuses the existing "invalid, re-measure" contract that
    # callers already honour rather than adding a fourth exit code.
    head_at_finish = _git_head(PROJECT_ROOT)
    if head_at_launch is None or head_at_finish is None:
        print("  tree-move check: NOT RUN (HEAD unreadable) -- this run is NOT "
              "certified against a mid-run merge")
    elif head_at_launch != head_at_finish:
        print("VERDICT: INVALID (tree-moved) -- this number means NOTHING")
        print("  HEAD at launch: %s" % head_at_launch)
        print("  HEAD at finish: %s" % head_at_finish)
        # NAME THE OFFENDERS (). Two opaque shas told the reader a
        # tree moved but never who moved it, and the verdict prints hours
        # later to the Body that LAUNCHED the run -- never the one that caused
        # the void. So nobody who causes this ever learns they did.
        offenders = _git_offenders(PROJECT_ROOT, head_at_launch, head_at_finish)
        for line in _render_offenders(offenders):
            print(line)
        print("  Chunk file lists are computed AT LAUNCH, so later chunks ran "
              "against a different tree than chunk 00 -- possibly against "
              "paths the merge deleted.")
        print("  classify() would have said: %s" % verdict)
        for r in reasons:
            print("    - %s" % r)
        print("\nRe-run on a settled tree. Do NOT climb the chunk ladder -- no "
              "rung fixes a tree that moved, and do NOT file regressions from "
              "this run.")
        print("On a worker Body the merge is Phase -0.3 "
              "(iteration-push.sh --no-push), which fires on EVERY turn-end "
              "re-entry, so any suite longer than one turn is exposed at every "
              "turn boundary.")
        # The COUNT and the AUTHOR SET are the fields a cadence needs ("voided
        # runs per week and per offending Body"); the commit list is a
        # convenience and is capped so one void cannot emit a multi-kilobyte
        # log line. offender_count_is_floor says the count hit the read cap.
        _emit_void_record("tree-moved",
                          head_at_launch=head_at_launch,
                          head_at_finish=head_at_finish,
                          offenders_readable=offenders is not None,
                          offender_count=None if offenders is None
                          else len(offenders),
                          offender_count_is_floor=bool(
                              offenders and
                              len(offenders) >= _OFFENDER_READ_LIMIT),
                          offender_authors=None if offenders is None
                          else sorted({c["author"] for c in offenders}),
                          offenders=None if offenders is None
                          else offenders[:_OFFENDER_SHOW_LIMIT],
                          would_have_said=verdict)
        print("=" * 66)
        return 2

    if verdict == "contended":
        # A HANG AND CONTENTION BOTH INVALIDATE THE RUN, SO THE EXIT CODE IS THE
        # SAME 2 -- but the LABEL and the remedy must differ, because the
        # documented contention remedy (climb the ladder, re-run when quiet) is
        # exactly what a deterministic hang defeats. Relabel rather than adding a
        # fourth verdict: the enum drives the exit code, and callers already
        # treat 2 as "invalid, re-measure", which is still correct here.
        hung = [r for r in reasons if "HUNG after" in r]
        if hung:
            print("VERDICT: INVALID (HUNG) -- this number means NOTHING")
        else:
            print("VERDICT: INVALID (contended) -- this number means NOTHING")
        for r in reasons:
            print("  - %s" % r)
        if args.confirm_solo and files_failing:
            worst = max(files_failing,
                        key=lambda f: blob.count("FAILED " + f))
            print("  confirming: re-running %s alone ..." % worst)
            cmd = [sys.executable, "-m", "pytest", worst]
            r = subprocess.run(cmd, capture_output=True, text=True,
                               cwd=str(PROJECT_ROOT), env=env)
            sp, sf, _ = _parse_counts(r.stdout or "")
            print("  solo: %d passed, %d failed -> %s"
                  % (sp, sf, "ENVIRONMENTAL (green solo)" if sf == 0
                     else "some failures are GENUINE"))
        if hung:
            print("\nFix or skip the hanging test -- re-running and raising "
                  "--chunks CANNOT help a deterministic hang.")
        else:
            print("\nRe-run when the fleet is quiet, or raise --chunks.")
        print("Do NOT file regressions from this run. Do NOT wave it away either.")
        _emit_void_record("hung" if hung else "contended",
                          reasons=list(reasons),
                          passed=tot_p, failed=tot_f, errors=tot_e)
        print("=" * 66)
        return 2

    if verdict == "genuine":
        print("VERDICT: GENUINE failures -- trustworthy, act on them")
        for f in files_failing:
            print("  %s (%d)" % (f, blob.count("FAILED " + f)))
            # Step 5 inline, because it is cheap (two queries + a git log) and
            # because it is the step that keeps getting skipped when it is
            # merely documented. "Pre-existing" is not "tracked": a wrong
            # tracking ID sat in run-full-suite-after-deep-code.md for a day
            # while a GENUINE failure was unowned and every reader was told it
            # was handled. Solo discrimination is NOT run here -- that costs a
            # pytest invocation per file; use --triage for the full chain.
            _print_ownership(f, PROJECT_ROOT)
        print("\nRun `run-full-suite.sh --triage` to solo-discriminate these "
              "before filing (green solo => environmental, not a regression).")
        print("=" * 66)
        return 1

    print("VERDICT: CLEAN")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
