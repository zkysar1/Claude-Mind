#!/usr/bin/env python3
"""citation-credit-sweep — harvest commit-message citations of rb-/guard- ids
into usefulness-signal increments (g-115-6948).

WHY THIS IS THE CONSULTATION-CREDIT CHOKEPOINT (measured, 2026-08-20)
---------------------------------------------------------------------
The usefulness signal was structurally starved: ~103 rb/guard births per day
against 1-7 explicit helpful events per day, so utilization_score could never
separate load-bearing entries from noise. The candidate chokepoints were
compared on measured volume:

  * encode-session strengthen lane — fires, but at LLM discretion (a few
    events per session) and only in chat mode.
  * pre-apply consult gate — sees that a consult query RAN, but which entries
    HELPED is unknowable at that moment; no volume in gate-firings.
  * commit messages — **588 rb-/guard- citations in the last 7 days (84/day,
    362 unique ids)**, written as a side effect of the evidence discipline the
    rules already mandate. Durable, greppable, zero honor-system dependency.

Citing an entry in a commit message means it was load-bearing in working
context when the change shipped. This sweep converts each citation into ONE
`utilization.times_inferred_helpful` increment — the same counter and the same
reasoning as the board findings-citation lane (endpoints/board_write.py:
findings posts with guard-/rb- tags), extended to the highest-volume citation
surface. NEVER times_cited: zero weight in the active utilization_score v1
formula (guard-343 measurement-gap incident).

TARGET-vs-CITATION noise (guard-3154): a commit ABOUT an entry (retiring it,
editing it) would be a false credit — but the stores live in external world/,
which this git repo cannot contain, so in-repo commits cannot be store edits.
The residual noise (a message narrating a retirement performed via script) is
accepted at inferred-counter grade.

IDEMPOTENCY + FLEET SAFETY
--------------------------
Every commit is on every box after sync, and every agent's reducer runs
iteration-close — so dedup state must be FLEET-SHARED: the ledger
(meta/citation-credit-ledger.jsonl) records each credited sha, and swept shas
are skipped forever. Only shas that produced >= 1 credit attempt are ledgered
(no-citation commits are cheap to re-filter). The ledger row is appended
BEFORE the increments fire (claim-first), so the cross-box race window is one
sync period; a double credit inside it costs one extra inferred-grade count
and is accepted. A daemon outage aborts the sweep BEFORE ledgering the
unprocessed remainder, so nothing is lost — only deferred.

SELF-GATING: an internal min-interval (3600s, marker-file mtime) makes the
per-iteration call from iteration-close a stat-check no-op; ~30 commits/day
does not need more than an hourly harvest. `--force` bypasses (tests, manual
runs). Fail-open everywhere: this is a harvester, and a missed sweep is
recovered by the next one.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Set, Tuple

_SELF = Path(__file__).resolve().parent
if str(_SELF) not in sys.path:
    sys.path.insert(0, str(_SELF))

LEDGER_NAME = "citation-credit-ledger.jsonl"
MARKER_NAME = "citation-credit-ledger.last-sweep"
MIN_INTERVAL_S = 3600

# 2-to-5 digit ids, word-bounded: matches the fleet's id formats (guard-97 ..
# guard-4272, rb-209 .. rb-8546) without matching goal ids or dates.
CITE_RE = re.compile(r"\b(rb|guard)-(\d{2,5})\b")
STORE_OF = {"rb": "reasoning-bank", "guard": "guardrails"}
STORE_FILE = {"reasoning-bank": "reasoning-bank.jsonl",
              "guardrails": "guardrails.jsonl"}
_INACTIVE = {"retired", "superseded", "archived"}
FIELD = "utilization.times_inferred_helpful"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def commits_in_window(repo: Path, days: int) -> List[Tuple[str, str]]:
    """[(sha, full message)] oldest-first, bounded to the window.

    Windowed on COMMITTER TIMESTAMP, not `git log --since` (g-115-6959 /
    guard-4539). `--since` is a TRAVERSAL CUTOFF, not a filter: git walks from
    the tip and STOPS at the first commit older than the bound, so ONE
    old-dated commit at the tip hides every recent commit behind it (measured
    on a fixture 2026-08-20: 7 commits 67 SECONDS old returned EMPTY). Commit
    dates go non-monotonic in ordinary operation (rebase, cherry-pick,
    --amend --date, a merged long-lived branch, peer clock skew).

    Here a hidden commit is credit that is never awarded and never will be:
    the sweep is cadence-driven and ledgers what it has SEEN, so a commit the
    window skipped is not retried later -- by the next run it has aged out of
    the window entirely. Silent, permanent under-credit.
    """
    cutoff = int(datetime.now().timestamp()) - int(days) * 86400
    out = subprocess.run(
        ["git", "log", "--reverse", "--format=%ct%x1f%H%x1f%B%x1e"],
        capture_output=True, text=True, cwd=str(repo), timeout=120)
    if out.returncode != 0:
        raise RuntimeError(f"git log failed: {out.stderr.strip()[:200]}")
    commits = []
    for chunk in out.stdout.split("\x1e"):
        chunk = chunk.strip("\n\r ")
        if not chunk or "\x1f" not in chunk:
            continue
        parts = chunk.split("\x1f", 2)
        if len(parts) != 3:
            continue
        ct, sha, msg = parts
        try:
            if int(ct.strip()) < cutoff:
                continue
        except ValueError:
            # Unparseable stamp: KEEP. Credit is awarded at most once per sha
            # (the ledger dedupes), so an extra candidate is harmless while a
            # dropped one is credit lost for good.
            pass
        commits.append((sha.strip(), msg))
    return commits


def active_ids(world_dir: Path) -> Dict[str, str]:
    """{id: store} for every ACTIVE rb/guard entry. Direct read is the
    established core-script pattern for read-only census (store_dupe_warn
    load_corpus); malformed lines are skipped."""
    known: Dict[str, str] = {}
    for store, fname in STORE_FILE.items():
        p = world_dir / fname
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(d, dict):
                continue
            if str(d.get("status", "")).lower() in _INACTIVE:
                continue
            rid = str(d.get("id") or "")
            if rid:
                known[rid] = store
    return known


def ledgered_shas(ledger: Path) -> Set[str]:
    shas: Set[str] = set()
    try:
        raw = ledger.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return shas
    for line in raw.splitlines():
        try:
            d = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(d, dict) and d.get("sha"):
            shas.add(str(d["sha"]))
    return shas


def _marker_fresh(meta_dir: Path) -> bool:
    m = meta_dir / MARKER_NAME
    try:
        import time
        return (time.time() - m.stat().st_mtime) < MIN_INTERVAL_S
    except OSError:
        return False


def _touch_marker(meta_dir: Path) -> None:
    try:
        (meta_dir / MARKER_NAME).write_text(_now() + "\n", encoding="utf-8")
    except OSError:
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Convert commit-message rb-/guard- citations into "
                    "times_inferred_helpful increments (g-115-6948).")
    ap.add_argument("--window-days", type=int, default=7)
    ap.add_argument("--max-increments", type=int, default=200)
    ap.add_argument("--repo", default=None)
    ap.add_argument("--world-dir", default=None)
    ap.add_argument("--meta-dir", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="bypass the min-interval self-gate")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    from _paths import PROJECT_ROOT, WORLD_DIR, META_DIR
    repo = Path(args.repo) if args.repo else Path(PROJECT_ROOT)
    world = Path(args.world_dir) if args.world_dir else Path(WORLD_DIR)
    meta = Path(args.meta_dir) if args.meta_dir else Path(META_DIR)
    ledger = meta / LEDGER_NAME

    def say(msg):
        if not args.quiet:
            print(f"[citation-credit-sweep] {msg}")

    if not args.force and not args.dry_run and _marker_fresh(meta):
        return 0  # self-gated no-op (the iteration-close common case)

    try:
        commits = commits_in_window(repo, args.window_days)
    except Exception as e:
        say(f"WARN git scan failed, sweep skipped: {e}")
        return 0
    seen = ledgered_shas(ledger)
    known = active_ids(world)

    # Daemon preflight (kills the retry-burn): claim-first ledgering means a
    # mid-sweep daemon failure costs the current row's credits, and WITHOUT
    # this probe every retry against a down daemon would burn one more
    # commit's credits (ledger the row, fail its first increment, abort).
    # One health hit up front turns a full outage into a clean zero-cost
    # deferral; only a daemon dying MID-sweep still pays the one-row loss.
    if not args.dry_run:
        try:
            import _rt
            _rt.rt_call("GET", "/v1/admin/health")
        except Exception as e:
            say(f"WARN daemon preflight failed, sweep skipped "
                f"(nothing ledgered): {e}")
            return 0

    credited_total = 0
    skipped_total = 0
    rows_written = 0
    daemon_down = False

    for sha, msg in commits:
        if sha in seen:
            continue
        cites = sorted({f"{kind}-{num}" for kind, num in CITE_RE.findall(msg)})
        if not cites:
            continue
        to_credit = [(c, known[c]) for c in cites if c in known]
        unknown = [c for c in cites if c not in known]
        if not to_credit and not unknown:
            continue
        if credited_total + len(to_credit) > args.max_increments:
            say(f"cap {args.max_increments} reached — remainder deferred to "
                f"next sweep")
            break
        if args.dry_run:
            say(f"DRY {sha[:12]}: would credit {[c for c, _ in to_credit]}, "
                f"skip {unknown}")
            continue
        # Claim-first: ledger the sha, then fire the increments. A crash
        # between the two loses at most this row's credits; the alternative
        # (increment-first) double-credits on every crash-rerun.
        row = {"sha": sha, "ts": _now(),
               "by": os.environ.get("MIND_AGENT", "unknown"),
               "credited": [c for c, _ in to_credit],
               "skipped_unknown_or_inactive": unknown}
        try:
            from _fileops import locked_append_jsonl
            locked_append_jsonl(str(ledger), row)
        except Exception as e:
            say(f"WARN ledger append failed, aborting sweep: {e}")
            return 0
        rows_written += 1
        skipped_total += len(unknown)
        for rid, store in to_credit:
            try:
                import _rt
                resp = _rt.store_increment(store, rid, FIELD)
                if isinstance(resp, dict) and (resp.get("ok")
                                               or resp.get("spooled")):
                    credited_total += 1
                else:
                    skipped_total += 1
            except Exception as e:
                # Daemon unreachable: stop crediting, leave the REMAINING
                # shas unledgered for the next sweep. This row's uncredited
                # tail is the accepted claim-first loss.
                say(f"WARN increment failed ({rid}): {e} — sweep aborted, "
                    f"remainder deferred")
                daemon_down = True
                break
        if daemon_down:
            break

    if not args.dry_run:
        _touch_marker(meta)
    say(f"swept window={args.window_days}d: {rows_written} commit(s) "
        f"ledgered, {credited_total} increment(s) credited, "
        f"{skipped_total} citation(s) skipped (unknown/inactive/failed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
