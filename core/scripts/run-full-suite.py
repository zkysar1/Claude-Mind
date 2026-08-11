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
import configparser
import json
import os
import re
import subprocess
import sys
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
DEFERRED_TESTPATHS = {"mind_api/tests"}


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
        r = subprocess.run([sys.executable, "-m", "pytest", path, "-q"],
                           capture_output=True, text=True, cwd=str(root),
                           env=env, timeout=1800)
    except Exception as exc:
        return None, None, str(exc)
    p, f, e = _parse_counts(r.stdout or "")
    if r.returncode not in (0, 1) or (p + f + e) == 0:
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
        print("%sowner: NONE -- no goal in either queue names this test "
              "(%d open goal(s) scanned)" % (indent, rows_scanned))
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
        print("=" * 66)
        return 0

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
    print("=" * 66)
    return 1 if (genuine_unowned or errored or ownership_unknown) else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--chunks", type=int, default=4,
                    help="fresh processes to split across (default 4)")
    ap.add_argument("--triage", action="store_true",
                    help="triage the chunk logs already in --out; does NOT re-run the suite")
    ap.add_argument("--out", default=None,
                    help="log directory (default: agents/<agent>/temp/suite-run)")
    ap.add_argument("--include-daemon-integration", action="store_true",
                    help="DANGEROUS with a live daemon; see Live-Daemon Exception")
    ap.add_argument("--confirm-solo", action="store_true",
                    help="on a contended verdict, re-run the worst-hit file alone to prove it")
    args = ap.parse_args(argv)

    testpaths = _testpaths()
    if not testpaths:
        print("run-full-suite: no tests dir resolved (pytest.ini testpaths "
              "named nothing that exists, and %s is absent)" % TESTS_DIR,
              file=sys.stderr)
        return 3

    agent = os.environ.get("MIND_AGENT", "").strip()
    out = Path(args.out) if args.out else (
        PROJECT_ROOT / "agents" / (agent or "shared") / "temp" / "suite-run")
    out.mkdir(parents=True, exist_ok=True)

    if args.triage:
        # Constraint 1 governs the solo re-runs too -- they are real pytest
        # invocations against the real tree, so an unpinned backend can collide
        # on the production S3 key exactly as a full run would (guard-955).
        env = dict(os.environ)
        env["STORAGE_BACKEND"] = "local"
        env["PYTHONUNBUFFERED"] = "1"
        return triage(out, PROJECT_ROOT, env)

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
    for old in out.glob("chunk-*.log"):
        try:
            old.unlink()
        except OSError:
            pass

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
