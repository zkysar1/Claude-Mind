#!/usr/bin/env python3
"""domain-suite-gate.py — refuse a status=completed close while the world's
domain test suite is red or uncollectable, IF a domain script changed since
the goal was claimed (g-353-75).

WHY. The domain half of the test population lives at $WORLD_PATH/scripts —
external, gitignored, invisible to full-suite-recommender.sh (it detects
changes through git, so it reports "no code changes" for every domain-script
edit ever made — guard-1947). run-full-suite-after-deep-code.md names the
command for that half, but naming a command is honor-system, and a small-model
Body never runs it. Measured 2026-08-29 on a live deployment: two test modules
imported symbols that later units had removed from the modules they test; the
domain suite ended in `Interrupted: 2 errors during collection`, and every
later goal in that lane "verified" against a suite that could not collect.
guard-399: an instruction the LLM must follow at a step needs the gate that
makes skipping it impossible. This is that gate.

WHAT IT DOES, in order (each step is cheap until the last):
  1. No $WORLD_PATH/scripts, no runner hook AND no test_*.py under it → noop.
     A world without domain tests is a supported configuration, not a breakage
     (same stance as run-full-suite.sh's domain block).
  2. No code file under scripts/ (.py .sh .bash .yaml .yml .toml .cfg .ini)
     modified at or after the goal's claim → noop. The claim time is
     `claimed_at` on the goal record (read through aspirations-read.sh, never
     the store directly); `--since <iso>` overrides it; with neither readable
     the window falls back to the last 6 hours and says so. mtime is the
     honest trigger: Edit-tool writes to world/scripts do not pass through
     _fileops, so the changelog cannot attribute them, and on a shared world
     several Bodies edit the same tree — a red suite blocks all of their
     verification regardless of who broke it, so the gate names the touched
     files and leaves attribution to the reader.
  3. Run the domain suite: the world-provided hook
     $WORLD_PATH/scripts/run-domain-tests.sh when present (Pattern B,
     domain-hooks.md — core names the slot, the world fills it), else
     `python3 -m pytest -q` with cwd=scripts so `from <pkg> import ...`
     resolves the way the rule's own command runs it. STORAGE_BACKEND=local is
     pinned (guard-955). Bounded at 900 s. One stderr line goes out BEFORE it
     starts, naming the gate, the changed files and how long to expect: the
     bound, and the last run's length on this box, kept in the baseline file
     (g-375-02 — Bodies that were not told killed it and wrote status by hand).
  3b. Credential tripwire (g-353-79): the credential-NAMED files directly under
     the world, its parent and the project root (.env* / *token* / *secret* /
     *credential* / *.pem / *.key / *.p12 / id_*) are snapshotted by size+mtime
     around the run; if any was rewritten the close is BLOCKED whatever the
     suite's rc, and no override lifts it (a live deployment lost its token to
     a mocked-refresh test on 2026-08-29).
  4. rc 0 → pass. rc 5 (pytest: nothing collected) → pass, noted. A timeout
     or a pytest internal/usage error (3/4) is a gate fault → fail-open with a
     warning. rc 2 (collection error) → BLOCK, always. rc 1 (red) → the
     RATCHET: the failing set from the previous run on this box
     (world/domain-suite-baseline.json) is the baseline; the first run seeds
     it and passes; later runs block only on a red NOT in it, and a run whose
     reds are a subset passes and shrinks the baseline. A block exits 1 with
     the touched files and the last lines on stderr. `--override "<why>"`
     turns a block into a pass and appends one row to
     world/domain-suite-overrides.jsonl — the ledger is the audit, and a
     collection error is never a legitimate override (the message says so).

Every branch is reported to _gate_log so the pass/block/override split is
measurable (gate-stats.sh --gate domain-suite-gate); the gate itself fails
OPEN on its own errors (decision `error`, exit 0) — a broken gate must never
wedge a close.

Usage:
    python3 core/scripts/domain-suite-gate.py --goal <id> --source <world|agent>
        [--since <iso>] [--override "<justification>"] [--timeout <s>]

stdout: exactly one JSON line —
    {"gate": "domain-suite-gate", "decision": noop|pass|block|override|error,
     "reason": "...", "runner": "...", "touched": [...], "rc": N, "tail": [...]}
exit:   0 on noop/pass/override/error; 1 on block.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _paths import PROJECT_ROOT, WORLD_DIR  # noqa: E402
from _gate_log import log as _gate_log  # noqa: E402
from _runtime_bash import bash_cmd  # noqa: E402  guard-580/581: never a bare "bash", never str(Path)

GATE_ID = "domain-suite-gate"  # MUST match the id in core/config/gates.yaml
RUNNER_HOOK = "run-domain-tests.sh"
CODE_SUFFIXES = {".py", ".sh", ".bash", ".yaml", ".yml", ".toml", ".cfg", ".ini"}
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".locks", ".git", "node_modules", ".venv", "venv"}
FALLBACK_WINDOW = timedelta(hours=6)
# One minute of slack under the claim stamp: a Body that edits in the same
# minute it claims must not slip under the window on clock granularity.
SLACK_SECONDS = 60
DEFAULT_TIMEOUT = 900
TAIL_LINES = 25
# Where a NON-CLEAN run's full output is preserved (). Gitignored,
# and already the home of this gate's own stderr, so a reader chasing a refusal
# looks in one place. See `_retain_log` for why `tail` alone was not enough.
# DOMAIN_SUITE_LOG_DIR redirects it — the gate's own tests must not write into
# the live tree, which is the "point the tests at a tmp path" pattern this
# script's refusal text already prescribes to others (guard-5541).
RETAINED_LOG_DIR = Path(os.environ.get("DOMAIN_SUITE_LOG_DIR")
                        or PROJECT_ROOT / "core" / "logs" / "domain-suite-gate")

# ─── credential tripwire () ────────────────────────────────────────
# The suite this gate demands as the price of a close is also a process running
# with the Body's full environment. Measured 2026-08-29 23:46Z on a live
# deployment: the green re-run a close was waiting on persisted a MOCKED
# token-refresh response over the real token file (a save helper defaulting to a
# hardcoded absolute path), and the close then passed on that green. The gate
# cannot know a domain's credential paths, but it can know their SHAPE: a file
# directly under a governed root whose NAME says what it holds. Those are
# snapshotted (size, mtime) before the run and compared after; a rewrite is a
# BLOCK no override lifts — restoring the file and isolating the persistence
# path (guard-5541) is the only way through. Name only, not mode: measured on the
# deployment that motivated this, every bland-named 0600 file under the roots
# was a peer-written store or doc (forged-skills.yaml, program.md,
# requirements.txt) — under eight concurrent Bodies a mode heuristic blocks
# closes for writes no test made, and caught nothing a name does not. Stores
# and docs (.jsonl/.md) are skipped for the same reason even when named.
PRIVATE_NAME_RE = re.compile(r"(?i)^\.env|token|secret|credential|\.pem$|\.key$|\.p12$|^id_(rsa|ed25519|ecdsa)")
PRIVATE_SKIP_SUFFIXES = {".lock", ".pid", ".port", ".sock", ".log", ".tmp", ".bak", ".jsonl", ".md"}
# Ceiling on the content read behind the sha256 (). A credential file is
# small — the live .env.local that motivated this is ~2 KB — so anything past this
# is not one, and the gate declines to read it rather than pulling an arbitrarily
# large file into memory. Over the ceiling the digest is None, which classifies as
# `unverifiable` and warns, never as a silent "unchanged".
PRIVATE_HASH_MAX_BYTES = 1 << 20  # 1 MiB


def private_roots(world_dir: Path | None) -> list[Path]:
    """The world, its parent (where a deployment keeps .env.local beside .mind-data),
    and the project root — deduplicated, in that order."""
    roots: list[Path] = []
    candidates = ([Path(world_dir), Path(world_dir).parent] if world_dir else []) + [Path(PROJECT_ROOT)]
    for r in candidates:
        if r not in roots:
            roots.append(r)
    return roots


def _content_digest(p: Path, size: int) -> str | None:
    """sha256 of a credential-shaped file, or None when it cannot be established.

    The DIGEST, never the value: it is compared against another digest and never
    logged, emitted or carried into a message (guard-724 / guard-1563 — read a
    credential store by shape, never by content). A digest is not reversible and
    is not a value, which is what makes this the one content read the gate may do.

    None means "could not establish", and every caller must treat that as UNKNOWN
    rather than as 'unchanged' — an unreadable file is exactly the case where a
    confident answer would be wrong.
    """
    if size > PRIVATE_HASH_MAX_BYTES:
        return None          # not a credential file at that size; do not read it
    try:
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            h.update(fh.read(PRIVATE_HASH_MAX_BYTES + 1))
        return h.hexdigest()
    except OSError:
        return None


def private_files(roots: list[Path]) -> dict[str, tuple[int, int, str | None]]:
    """{path: (size, mtime_ns, sha256|None)} of the credential-shaped files DIRECTLY
    under each root.

    The digest was added by g-115-9346. Shape alone (size, mtime_ns) CANNOT tell a
    destructive clobber from a bare `touch` or a byte-identical round-trip rewrite,
    so a gate keying on it hard-refuses every close over an event that may have
    changed nothing — measured twice, contents byte-identical both times, with a
    refusal that states `--override-domain-suite does not apply`. The digest is what
    lets the two be separated; see classify_private_changes.
    """
    out: dict[str, tuple[int, int, str | None]] = {}
    for root in roots:
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for p in entries:
            try:
                st = p.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode) or p.suffix.lower() in PRIVATE_SKIP_SUFFIXES:
                continue
            if PRIVATE_NAME_RE.search(p.name):
                out[str(p)] = (st.st_size, st.st_mtime_ns, _content_digest(p, st.st_size))
    return out


def classify_private_changes(before: dict, after: dict) -> dict[str, list[str]]:
    """Split the credential-shaped files that moved across the suite window into
    what the instrument can actually support (g-115-9346).

    Returns {"content_changed": [...], "touched": [...], "unverifiable": [...]},
    each a sorted path list:

      content_changed — the file VANISHED, or its sha256 differs. The only class
                        that justifies a hard refusal: contents really are not what
                        they were.
      touched         — the (size, mtime_ns) signature moved but the sha256 is
                        IDENTICAL. Nothing was lost. A `touch`, a restore, or a
                        provisioner re-writing the same values all land here.
      unverifiable    — the signature moved and the digest could not be established
                        on one side or the other. NOT silently folded into either
                        class: the caller says so out loud.

    WHY THIS SPLIT EXISTS, stated so it is not "simplified" back: the predicate
    brackets a TIME WINDOW on a live multi-agent box, so it measures co-occurrence
    and never causation — any concurrent writer satisfies it exactly as a test
    would. Two independent investigations (cc-04, 86 tests; cc-08, 82 tests plus a
    full canonical runner pass under a 1-second watcher) found NO domain test that
    writes the live credential file, while the refusal it produced was hard and
    unoverridable. A guard that cannot be satisfied by any action available to the
    blocked agent is a wedge, not a guard — so only `content_changed` may block.
    """
    out = {"content_changed": [], "touched": [], "unverifiable": []}
    for path, sig in before.items():
        post = after.get(path)
        if post == sig:
            continue
        if post is None:
            out["content_changed"].append(path)     # vanished
            continue
        before_digest, after_digest = sig[2], post[2]
        if before_digest is None or after_digest is None:
            out["unverifiable"].append(path)
        elif before_digest != after_digest:
            out["content_changed"].append(path)
        else:
            out["touched"].append(path)
    return {k: sorted(v) for k, v in out.items()}


def rewritten_private_files(before: dict, after: dict) -> list[str]:
    """Paths whose signature changed, or that vanished, across the suite run.

    Retained as the raw any-movement predicate; the CLOSE decision now routes
    through classify_private_changes, which says WHICH KIND of movement it was.
    """
    return sorted(p for p, sig in before.items() if after.get(p) != sig)


# ─── discovery ────────────────────────────────────────────────────────────

def _scripts_dir(world_dir: Path | None) -> Path | None:
    if world_dir is None:
        return None
    d = Path(world_dir) / "scripts"
    return d if d.is_dir() else None


def _walk(scripts_dir: Path):
    """Yield files under scripts_dir, pruning the cache/venv dirs."""
    for root, dirs, files in os.walk(scripts_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            yield Path(root) / name


def has_domain_tests(scripts_dir: Path) -> bool:
    if (scripts_dir / RUNNER_HOOK).is_file():
        return True
    return any(p.name.startswith("test_") and p.suffix == ".py" for p in _walk(scripts_dir))


def touched_since(scripts_dir: Path, since: datetime) -> list[tuple[str, str]]:
    """[(relative path, mtime iso)] for code files modified at/after `since`."""
    cutoff = since.timestamp() - SLACK_SECONDS
    out = []
    for p in _walk(scripts_dir):
        if p.suffix not in CODE_SUFFIXES:
            continue
        try:
            mt = p.stat().st_mtime
        except OSError:
            continue
        if mt >= cutoff:
            out.append((p.relative_to(scripts_dir).as_posix(),
                        datetime.fromtimestamp(mt).strftime("%Y-%m-%dT%H:%M:%S")))
    out.sort(key=lambda t: t[1])
    return out


# ─── claim time ───────────────────────────────────────────────────────────

def _parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text).strip()[:19])
    except ValueError:
        return None


def claimed_at(goal_id: str, source: str) -> datetime | None:
    """The goal's claimed_at through aspirations-read.sh (daemon-routed).

    Returns None when the record or the stamp is unreadable — the caller
    falls back to a bounded window (FALLBACK_WINDOW, 6h) and says so; the gate
    never reads the store file directly. Both behaviours are this gate's
    founding contract, stated in the commit that built it (g-353-75,
    109b9f2725: "noop without ... a code file newer than the goal's claimed_at
    (aspirations-read.sh; fallback 6h)").
    """
    parts = goal_id.split("-")
    if len(parts) < 3 or parts[0] != "g":
        return None
    asp_id = "asp-" + parts[1]
    try:
        proc = subprocess.run(
            bash_cmd(SCRIPT_DIR / "aspirations-read.sh", "--source", source, "--id", asp_id),
            capture_output=True, text=True, timeout=60, check=False,
        )
        doc = json.loads(proc.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    for g in (doc.get("goals") or []) if isinstance(doc, dict) else []:
        if g.get("id") == goal_id:
            return _parse_iso(g.get("claimed_at"))
    return None


# ─── the run ──────────────────────────────────────────────────────────────

def runner_command(scripts_dir: Path) -> tuple[list[str], str]:
    hook = scripts_dir / RUNNER_HOOK
    if hook.is_file():
        return bash_cmd(hook), "scripts/" + RUNNER_HOOK
    return [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--color=no"], "python -m pytest (cwd=scripts)"


def _retain_log(log_path: str, goal_id: str) -> str | None:
    """Copy a NON-CLEAN run's log somewhere a human can read it later; None on failure.

    g-115-9560: this function is the whole of that goal's third outcome. The run
    log was written to a mktemp file, read once, and unlinked in the `finally`
    below — so on a red run the ONLY surviving evidence was `tail` (25 lines).
    The domain runner reports one line per unit, and the suite is ~97 units, so a
    failure at [17/97] and every assertion line under it fell outside that window
    and was destroyed. Measured 2026-09-09 on cc-04: exactly one real red,
    `[17/93] FAIL test_deploy_hold_check_freshness.sh (rc=1)`, whose failure text
    no longer existed by the time anyone read the refusal. A red whose only
    evidence is auto-deleted is close to unfixable by anyone who was not watching
    it live — and this gate BLOCKS every close on the box while it stands, so the
    cost of not being able to diagnose it is paid by every agent here.

    Clean runs still delete: the point is to keep what a reader needs, not to
    accumulate a log per close. `core/logs/` is the established home for this
    class of file — this gate's own stderr already lands there, redirected by
    `iteration-close.sh:1224` into `core/logs/iteration-close-stderr.log` — and
    the whole dir is gitignored (`.gitignore:199`, measured g-115-9560).

    NOTHING PRUNES THESE, DELIBERATELY (g-115-9560). Measured while adding this:
    no reaper covers `core/logs/` — `housekeeping-tick.py` Lane A drains
    `agents/<agent>/temp/`, not this tree. That is the intended state, because a
    retained log is written ONLY when the suite did not come back clean, and per
    the gate's own contract (g-353-75) that is the case that BLOCKS every close
    on the box until someone fixes it. So the population is bounded by how often
    a close is blocked, not by close volume. A reaper here would be a second
    thing that can delete the one copy of a failing run — the defect this
    function exists to remove. If the dir ever does grow enough to matter, that
    growth is itself the finding.
    """
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", goal_id or "unknown")
    dest = RETAINED_LOG_DIR / f"{safe}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    try:
        RETAINED_LOG_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(log_path, dest)
        return str(dest)
    except OSError as e:  # fail-open: a gate must never wedge a close over a log
        print(f"domain-suite-gate: could not retain run log: {e}", file=sys.stderr)
        return None


def run_suite(scripts_dir: Path, timeout: int,
              goal_id: str) -> tuple[int | None, list[str], set[str], str | None]:
    """(rc, last lines, failing ids, retained log path). rc None = exceeded `timeout`.

    The fourth element is the path a NON-CLEAN run's full output was preserved at,
    or None (clean run, or the copy failed). See `_retain_log`.
    """
    cmd, _ = runner_command(scripts_dir)
    env = dict(os.environ)
    env["STORAGE_BACKEND"] = "local"  # guard-955: any test runner, always
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("PYTEST_ADDOPTS", None)
    # : isolate the suite's DAEMON RUNTIME. RUNTIME_DIR is the B16
    # override (`RT_DIR="${RUNTIME_DIR:-$PROJECT_ROOT/mind_api/state}"` in
    # mind-api-start.sh, and lifecycle.runtime_dir). Without it, a suite child
    # that reaches ANY daemon-backed wrapper hits rt_ensure_running -> rt_spawn,
    # claims the SHARED mind_api/state/daemon.port, and force-kills the live
    # daemon. MEASURED 2026-09-05/06 on DESKTOP-O91DLK2: NINETEEN daemon starts
    # in 57 minutes, each on a new port and a new parent_pid, wedging every
    # in-flight wrapper with "daemon is unreachable" — one close printed its
    # probe URL literally as `127.0.0.1:?` because daemon.port was EMPTY
    # mid-rewrite when the wrapper read it.
    #
    # The existing  chokepoint does NOT cover this path. It refuses a
    # shared-runtime claim only when PYTEST_CURRENT_TEST is set (_runtime.sh:411,
    # mind-api-start.sh:481), and the domain runner is a BASH aggregator —
    # runner_command() returns bash_cmd(hook) for the world's run-domain-tests.sh
    # — so that variable is unset in the child and the refusal never fires.
    # Isolation also contains guard-1144: the STORAGE_BACKEND=local pin above is
    # INHERITED by any daemon the suite respawns, so a shared-port respawn hands
    # the live fleet a LocalBackend split-brain on top of the port churn.
    #
    # This bounds the BLAST RADIUS of an orphan, not its existence: a suite that
    # outlives its parent still runs (see the guard-4375 note below), but it can
    # no longer touch the shared daemon. Reaping the child is the goal's separate
    # outcome and is not attempted here.
    # BOTH names are required, and setting only one silently covers half the
    # surface. There are TWO spawn paths with TWO different variable names:
    #   _runtime.sh:33      RT_DIR="${RT_DIR:-$PROJECT_ROOT/mind_api/state}"
    #   mind-api-start.sh:52 RT_DIR="${RUNTIME_DIR:-$PROJECT_ROOT/mind_api/state}"
    # A wrapper reaches EITHER (rt_ensure_running -> rt_spawn, or the launcher
    # directly), and _runtime.sh never reads RUNTIME_DIR at all — grep it: the
    # only occurrences are comments. Its own note at :397 states the split
    # ("via RT_DIR here ... or RUNTIME_DIR in mind-api-start.sh") and records
    # that fixtures "set RT_DIR and never RUNTIME_DIR". So RUNTIME_DIR alone
    # isolates the launcher path and leaves rt_spawn claiming the shared dir —
    # which is the MORE common path, since it is what an ordinary daemon-backed
    # wrapper call takes. Setting both is what actually closes it.
    rt_dir = tempfile.mkdtemp(prefix="domain-suite-rt-")
    env["RUNTIME_DIR"] = rt_dir
    env["RT_DIR"] = rt_dir
    # guard-4375: `timeout` is DECORATIVE against a Git-Bash child whenever
    # stdout or stderr is a PIPE. The kill fires on schedule; what blocks is the
    # post-kill communicate() reap inside subprocess.run — it takes no timeout,
    # and a surviving descendant of the runner still holds the inherited pipe
    # write handle, so the reader threads never see EOF. MEASURED on this box
    # 2026-09-05: `--goal ` ran 51 min against timeout=900 while the
    # runner kept spawning children, wedging three Bodies' closes. capture_output
    # is not an option here because the verdict READS the output (guard-4375:
    # "parameterize the helper" rather than DEVNULL when the caller reads it), so
    # redirect to a file — the idiom run-full-suite.py and framework_pull.py
    # already use — and read it back. Positive control, same box: pipes hung
    # >37s at timeout=3 with no exception; the file redirect raised at 3.1s.
    fd, log_path = tempfile.mkstemp(prefix="domain-suite-gate-", suffix=".log")
    os.close(fd)
    rc: int | None = None
    timed_out = False
    retained: str | None = None
    try:
        with open(log_path, "w", encoding="utf-8", errors="replace") as fh:
            try:
                proc = subprocess.run(cmd, cwd=str(scripts_dir), env=env,
                                      stdout=fh, stderr=subprocess.STDOUT,
                                      timeout=timeout, check=False)
                rc = proc.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
        text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    finally:
        # PRESERVE BEFORE DELETING, and only when the run was not clean
        # (). The ordering is the whole point: the old code read the
        # text into memory and unlinked, so the file every later reader wanted
        # was gone before the verdict was even computed.
        if timed_out or rc not in (0, 5):
            retained = _retain_log(log_path, goal_id)
        # A timed-out runner's descendants may still hold the file open; on
        # Windows that makes the unlink fail. Leaking one temp file beats
        # raising over cleanup.
        try:
            os.unlink(log_path)
        except OSError:
            pass
        # Same reasoning for the isolated runtime dir, and more so: an orphaned
        # suite's daemon holds daemon.pid/port open under it, so on Windows the
        # tree is often UNREMOVABLE here. ignore_errors keeps cleanup from
        # raising over a leaked tmp dir — the isolation has already done its job
        # by the time we get here, and a stale tmp dir harms nobody.
        shutil.rmtree(rt_dir, ignore_errors=True)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if timed_out:
        return None, lines[-TAIL_LINES:], set(), retained
    return rc, lines[-TAIL_LINES:], failing_ids(lines), retained


_PYTEST_FAILED = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)")
# The runner's per-unit verdict: `[N/M] FAIL test_x.sh (rc=1)`, `[6/77] FAIL pytest batch (rc=1)`.
_RUNNER_UNIT_FAIL = re.compile(r"^\s*\[\d+/\d+\]\s+FAIL\s+(.+?)(?:\s+\(rc=\d+\))?\s*$")
# A bare `FAIL <unit>` only when <unit> looks like a file or a node id. A shell test's
# INNER assertion lines (`  FAIL the tag alone decides whether it retries`) must not
# become ids: measured on the first seed, they produced "the" — and a baseline entry
# "the" would have laundered every future inner failure that starts with that word.
_RUNNER_BARE_FAIL = re.compile(r"^\s*FAIL\s+(\S+(?:\.sh|\.py|\.bash)|\S+::\S+|\S+/\S+)\s*$")


def failing_ids(lines: list[str]) -> set[str]:
    """The failing units named in a run's output, as a set of identifiers.

    Two shapes are recognised, both stable: pytest's short summary
    (`FAILED path::test - msg`, `ERROR path::test`) and the domain runner's
    per-unit lines (`[N/M] FAIL file.sh (rc=1)`, or a bare `FAIL file.sh`).
    Everything else — including a shell test's inner `FAIL <sentence>` lines —
    is ignored, so an unrecognised runner yields an EMPTY set, and an empty set
    on a red run is treated as "cannot prove pre-existing", which blocks.
    """
    ids: set[str] = set()
    for ln in lines:
        m = _PYTEST_FAILED.match(ln)
        if m:
            ids.add(m.group(1))
            continue
        m = _RUNNER_UNIT_FAIL.match(ln) or _RUNNER_BARE_FAIL.match(ln)
        if m:
            ids.add(m.group(1).strip())
    return ids


# ─── the baseline (ratchet) ───────────────────────────────────────────────
#
# A world's suite may already be red before this gate exists (measured on the
# dev world: 2 real reds nobody had seen, in a 651 s run). Demanding GREEN would
# refuse every close on such a world until someone fixed reds that no close
# caused, so the verdict is a RATCHET, the audit-baselines shape: the failing
# set from the previous run is the baseline; a close blocks only on a red that
# is NOT in it (or on a collection error, which is never baseline-able); a run
# whose reds are a subset of the baseline passes AND shrinks the baseline to
# what still fails. The first run seeds it. The file is per box — the baseline
# describes what THIS box last saw, which is the honest scope.
#
# THAT PER-BOX SCOPE IS ENFORCED BY THE BASENAME BEING MACHINE-LOCAL, not by
# the write being "plain" (). Until 2026-08-31 this comment read "a
# plain write to the world mirror; on a synced world it stays local" — and the
# parenthetical was FALSE on any own-cloud deployment. The write IS plain, but
# the sync WALK picks the file up afterwards, so two boxes rewrote one shared
# S3 object. merge_handler_for("domain-suite-baseline.json") returns None, i.e.
# governed-store-write-classes class (b): nothing reconciles below the write, so
# the resulting fence is a PERMANENT wedge. Measured both-diverged for 123
# consecutive mirror sweeps on cc-07 and 417 on cc-02. Because the consumer is a
# GATE whose verdict is a ratchet, a foreign box's baseline mis-gates closes in
# BOTH directions: admitting a real regression the other box already carried, or
# blocking on a red this box never saw.
# The basename is now in owncloud_sync._EXCLUDE_NAMES, which makes the claim
# above true — _put short-circuits on _machine_local and writes local-only. If
# that entry is ever removed, this comment is false again and the wedge returns.

def _baseline_path(world_dir: Path) -> Path:
    return Path(world_dir) / "domain-suite-baseline.json"


def load_baseline(world_dir: Path) -> dict | None:
    try:
        doc = json.loads(_baseline_path(world_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and isinstance(doc.get("failing"), list) else None


def save_baseline(world_dir: Path, failing: set[str], rc: int, runner: str,
                  seconds: int | None = None) -> None:
    payload = {"recorded_at": datetime.now().isoformat(timespec="seconds"),
               "agent": os.environ.get("MIND_AGENT", "unknown"),
               "runner": runner, "rc": rc, "failing": sorted(failing)}
    if seconds is not None:
        payload["seconds"] = seconds  # the run's length, for the next run's announcement
    try:
        _baseline_path(world_dir).write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
    except OSError as e:
        print(f"domain-suite-gate: baseline write failed: {e}", file=sys.stderr)


# ─── ledger + telemetry ───────────────────────────────────────────────────

def _log_override(world_dir: Path, payload: dict) -> None:
    ledger = Path(world_dir) / "domain-suite-overrides.jsonl"
    try:
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with open(ledger, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"domain-suite-gate: override ledger write failed: {e}", file=sys.stderr)


def _emit(decision: str, goal_id: str, override: str | None, **fields) -> dict:
    doc = {"gate": GATE_ID, "decision": decision, "goal_id": goal_id}
    doc.update(fields)
    try:
        _gate_log(GATE_ID, decision, caller="iteration-close.sh do_verify",
                  trigger_matched=bool(fields.get("touched")),
                  payload={"goal_id": goal_id, "runner": fields.get("runner"),
                           "rc": fields.get("rc"), "touched": len(fields.get("touched") or [])},
                  override_reason=override if decision == "override" else None)
    except Exception:  # noqa: BLE001 — telemetry must never break the gate
        pass
    print(json.dumps(doc, ensure_ascii=False))
    return doc


# ─── main ─────────────────────────────────────────────────────────────────

def evaluate(goal_id: str, source: str, since: datetime | None, override: str | None,
             timeout: int, world_dir: Path | None) -> int:
    scripts_dir = _scripts_dir(world_dir)
    if scripts_dir is None or not has_domain_tests(scripts_dir):
        _emit("noop", goal_id, override, reason="no domain test suite under world scripts")
        return 0

    since_note = ""
    if since is None:
        since = claimed_at(goal_id, source)
        if since is None:
            since = datetime.now() - FALLBACK_WINDOW
            since_note = " (claimed_at unreadable; used the last 6 hours)"
    touched = touched_since(scripts_dir, since)
    if not touched:
        _emit("noop", goal_id, override, reason="no domain script modified since "
              + since.strftime("%Y-%m-%dT%H:%M:%S") + since_note)
        return 0

    _, runner_label = runner_command(scripts_dir)
    roots = private_roots(world_dir)
    before = private_files(roots)
    # SAY SO BEFORE THE SUITE STARTS (). Measured 2026-09-23 on two worker
    # Bodies: this run took 12-15 min, their shell tool allowed 10 at most, and with
    # nothing saying the close was running a suite they killed it 8 times, then wrote
    # the goal status by hand. stderr reaches the caller's output (do_verify does not
    # redirect it), so the line is there for any reader of the call's output so far.
    last = (load_baseline(world_dir) or {}).get("seconds")
    names = ", ".join(t[0] for t in touched[:3]) + (" ..." if len(touched) > 3 else "")
    expect = f"up to {timeout // 60} min"
    if isinstance(last, int) and not isinstance(last, bool):
        took = "under a minute" if last < 60 else f"{round(last / 60)} min"
        expect += f"; the last run on this box took {took}"
    print(f"[domain-suite-gate] running the world's domain suite before this close, because "
          f"{len(touched)} domain script(s) changed since the claim ({names}). Expect {expect}. "
          "It is not hung: let it finish.", file=sys.stderr, flush=True)
    started = time.monotonic()
    rc, tail, failing, log = run_suite(scripts_dir, timeout, goal_id)
    seconds = round(time.monotonic() - started)
    changes = classify_private_changes(before, private_files(roots))
    clobbered = changes["content_changed"]
    if clobbered:
        # CONTENTS really differ (or the file vanished). This is the case the hard
        # refusal was always meant for, and now the only one that reaches it.
        why = (f"{len(clobbered)} credential-shaped file(s) outside the suite's own tree CHANGED CONTENT "
               "during the domain-suite window: "
               + ", ".join(clobbered[:4]) + (" ..." if len(clobbered) > 4 else "")
               + " — verified by sha256, not by mtime")
        _emit("block", goal_id, override, reason=why, runner=runner_label, rc=rc, touched=touched,
              clobbered=clobbered, tail=tail, log=log)
        print("", file=sys.stderr)
        print(f"[domain-suite-gate] ✖ REFUSED status=completed for {goal_id}: {why}.", file=sys.stderr)
        print("  Restore each file from its backup or upstream source of truth FIRST (the run may have", file=sys.stderr)
        print("  replaced a live token with a fixture), then make the persistence path overridable and", file=sys.stderr)
        print("  point the tests at a tmp path (guard-5541). --override-domain-suite does not apply here:", file=sys.stderr)
        print("  a rewritten credential is never pre-existing.", file=sys.stderr)
        print("  ATTRIBUTION CAVEAT: this brackets a TIME WINDOW, so it establishes that the contents", file=sys.stderr)
        print("  changed while the suite ran — NOT that the suite changed them. On a live multi-agent", file=sys.stderr)
        print("  box a concurrent writer satisfies it identically. Confirm before repointing a test.", file=sys.stderr)
        return 1
    # Signature moved but the CONTENTS did not (or could not be compared). Warn with
    # what was actually observed and let the close proceed: a hard refusal here is
    # the  wedge — unoverridable, unreproducible by the bisect it implies,
    # and raised over a file that is provably intact.
    if changes["touched"] or changes["unverifiable"]:
        if changes["touched"]:
            print(f"[domain-suite-gate] ⚠ {len(changes['touched'])} credential-shaped file(s) were TOUCHED "
                  "during the suite window — sha256 UNCHANGED, so nothing was lost: "
                  + ", ".join(changes["touched"][:4])
                  + (" ..." if len(changes["touched"]) > 4 else ""), file=sys.stderr)
        if changes["unverifiable"]:
            print(f"[domain-suite-gate] ⚠ {len(changes['unverifiable'])} credential-shaped file(s) moved and "
                  "their contents could NOT be compared (unreadable, or past the hash ceiling); this is "
                  "UNKNOWN, not a clean bill: "
                  + ", ".join(changes["unverifiable"][:4])
                  + (" ..." if len(changes["unverifiable"]) > 4 else ""), file=sys.stderr)
        print("  Not blocking the close. Who wrote it is NOT measured — the predicate brackets a time "
              "window, not a cause.", file=sys.stderr)
    if rc in (0, 5):
        note = "" if rc == 0 else " (pytest collected no tests)"
        save_baseline(world_dir, set(), rc, runner_label, seconds)
        _emit("pass", goal_id, override, reason="domain suite green" + note, runner=runner_label,
              rc=rc, touched=touched)
        return 0

    # Gate faults fail OPEN: a suite that cannot finish in `timeout`, a pytest
    # internal error (3) or usage error (4) say nothing about THIS close.
    if rc is None or rc in (3, 4):
        why = f"domain suite exceeded {timeout}s" if rc is None else f"pytest rc={rc} (internal/usage error)"
        _emit("error", goal_id, override, reason=why + " — not a verdict on this close; fail-open",
              runner=runner_label, rc=rc, touched=touched, tail=tail, log=log)
        print(f"[domain-suite-gate] WARN {why}; the domain suite was NOT verified for {goal_id}", file=sys.stderr)
        # A fail-open fault is the case where the tail says LEAST — a timeout's
        # last 25 lines are wherever the run happened to be when the clock ran
        # out, which is not where it went wrong. So name the retained log here
        # too, not only on the block path ().
        if log:
            print(f"  Full run log (retained): {log}", file=sys.stderr)
        return 0

    baseline = load_baseline(world_dir)
    # The units this block is actually about, recorded verbatim in the override
    # ledger below. Without them the ledger holds only `why` (truncated at 6
    # entries with " ...") and a free-text claim, so no later reader can check
    # whether an override was honest — measured 2026-08-30 on a live 8-worker
    # fleet: 7 of 12 refusals overridden, every one asserting the reds were
    # "pre-existing", with the asserted count wandering 54 -> 66 -> 44 -> 47 ->
    # 43 across seven hours (the true figure was 25). An unverifiable audit
    # field is not an audit field.
    blocking_units: list[str] = []
    if rc == 2 or not failing:
        why = ("domain suite could not COLLECT (rc=2: an import or syntax error in a test module)"
               if rc == 2 else f"domain suite RED (rc={rc}) and no failing unit could be identified from its output")
    elif baseline is None:
        # First run on this box: what is red now predates this gate — EXCEPT a
        # red sitting in a file THIS unit touched, which the seed must not
        # launder. Measured on the first live seed of a deployment (2026-08-29):
        # 63 reds recorded, 2 of them in a test file the closing unit had
        # written 20 minutes earlier. Those block; the rest are recorded and
        # the ratchet starts here, not at green.
        touched_files = {t[0] for t in touched}
        touched_names = {Path(t[0]).name for t in touched}
        own = sorted(f for f in failing
                     if f.split("::")[0] in touched_files or Path(f.split("::")[0]).name in touched_names)
        if not own:
            save_baseline(world_dir, failing, rc, runner_label, seconds)
            _emit("pass", goal_id, override, reason=f"seeded baseline: {len(failing)} pre-existing red(s) recorded, "
                  "later closes block only on NEW reds", runner=runner_label, rc=rc, touched=touched,
                  failing=sorted(failing))
            return 0
        blocking_units = own
        why = (f"no baseline yet, and {len(own)} red(s) sit in files THIS unit touched, so they cannot be "
               "called pre-existing: " + ", ".join(own[:6]) + (" ..." if len(own) > 6 else "")
               + f" ({len(failing) - len(own)} other red(s) will be recorded once these are fixed)")
    else:
        new_reds = sorted(failing - set(baseline["failing"]))
        if not new_reds:
            save_baseline(world_dir, failing, rc, runner_label, seconds)  # ratchet: only what still fails
            _emit("pass", goal_id, override, reason=f"{len(failing)} pre-existing red(s), none new since "
                  f"{baseline.get('recorded_at', '?')} (baseline ratcheted)", runner=runner_label, rc=rc,
                  touched=touched, failing=sorted(failing))
            return 0
        blocking_units = new_reds
        why = (f"domain suite has {len(new_reds)} NEW red(s) not in the baseline of "
               f"{baseline.get('recorded_at', '?')}: " + ", ".join(new_reds[:6])
               + (" ..." if len(new_reds) > 6 else ""))
    if override:
        _log_override(world_dir, {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "goal_id": goal_id, "agent": os.environ.get("MIND_AGENT", "unknown"),
            "reason": override, "runner": runner_label, "rc": rc,
            "touched": [t[0] for t in touched], "why": why,
            # The full blocking set, untruncated — what the claim in `reason`
            # can be checked against later. Empty means no unit was
            # identifiable (a collection error), which is never overridable.
            "blocking_units": blocking_units,
            "baseline_recorded_at": (baseline or {}).get("recorded_at"),
        })
        _emit("override", goal_id, override, reason=why + " — overridden: " + override,
              runner=runner_label, rc=rc, touched=touched, tail=tail, log=log)
        return 0

    _emit("block", goal_id, override, reason=why, runner=runner_label, rc=rc, touched=touched, tail=tail, log=log)
    print("", file=sys.stderr)
    print(f"[domain-suite-gate] ✖ REFUSED status=completed for {goal_id}: {why}, and "
          f"{len(touched)} domain script(s) changed since this goal's claim "
          f"({since.strftime('%Y-%m-%dT%H:%M:%S')}{since_note}):", file=sys.stderr)
    for rel, mt in touched[-12:]:
        print(f"    {rel}  ({mt})", file=sys.stderr)
    if len(touched) > 12:
        print(f"    ... and {len(touched) - 12} more", file=sys.stderr)
    print(f"  Runner: {runner_label}. Last lines of the run:", file=sys.stderr)
    for ln in tail[-12:]:
        print("    " + ln[:200], file=sys.stderr)
    # NAME THE FULL LOG (). The tail above is 25 lines of a ~97-unit
    # run, so a failure partway through is not in it. Until this line existed the
    # only copy was deleted at the end of run_suite and the reader had no way to
    # know more had ever existed.
    if log:
        print(f"  FULL RUN LOG (retained): {log}", file=sys.stderr)
    else:
        print("  Full run log could NOT be retained — the tail above is all there is.", file=sys.stderr)
    print("  Fix the suite, then close again. A collection error is almost always an import that", file=sys.stderr)
    print("  no longer resolves: restore the symbol in the module, or update the test if the rename", file=sys.stderr)
    print("  was deliberate and every importer moved. Re-run it yourself first:", file=sys.stderr)
    print('    cd "$WORLD_PATH/scripts" && STORAGE_BACKEND=local python3 -m pytest -q', file=sys.stderr)
    # "PRE-EXISTING" IS NOT AN AVAILABLE RATIONALE HERE, and this text used to
    # invite it. Every blocking branch above has ALREADY excluded the reds it
    # knew about: the baseline holds the previous run's failing set, so a red
    # that reaches this point is by construction either NEW to it or sitting in
    # a file this very unit touched. Telling the operator to override when "the
    # red is PRE-EXISTING" therefore named the one condition that cannot be true
    # at this line — and the fleet answered exactly as instructed. Measured
    # 2026-08-30 across 8 workers: 7 of 12 refusals overridden, every single
    # justification asserting "pre-existing", one of them reading "all 47 NEW
    # reds are pre-existing failures". The operators were honest; the prompt was
    # wrong. Name the real alternative causes instead, so the claim is one a
    # later reader can check against `blocking_units` in the ledger.
    print("  These reds are NOT pre-existing — the baseline already excluded every red it knew", file=sys.stderr)
    print("  about, so do not override by calling them that. Override only when another cause is", file=sys.stderr)
    print("  identifiable, and NAME it:", file=sys.stderr)
    print("    - a concurrent unit or an out-of-scope commit broke it (name the agent/goal/sha)", file=sys.stderr)
    print("    - the unit is flaky or environment-dependent, not caused by any code change", file=sys.stderr)
    print('    --override-domain-suite "<goal-id>: <named cause>, not this close"', file=sys.stderr)
    print("  (appended to world/domain-suite-overrides.jsonl). Never override a collection error.", file=sys.stderr)
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--goal", required=True)
    ap.add_argument("--source", default="world", choices=["world", "agent"])
    ap.add_argument("--since", default=None, help="ISO timestamp; overrides the goal's claimed_at")
    ap.add_argument("--override", default=None, help="justification; turns a block into a logged pass")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = ap.parse_args(argv)
    since = _parse_iso(args.since) if args.since else None
    if args.since and since is None:
        print(f"domain-suite-gate: --since {args.since!r} is not an ISO timestamp", file=sys.stderr)
        return 2
    try:
        return evaluate(args.goal, args.source, since, args.override, args.timeout, WORLD_DIR)
    except Exception as e:  # noqa: BLE001 — fail OPEN: a broken gate must not wedge a close
        _emit("error", args.goal, args.override, reason=f"gate error, fail-open: {type(e).__name__}: {e}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
