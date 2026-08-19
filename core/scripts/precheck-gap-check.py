#!/usr/bin/env python3
"""precheck-gap-check.py — is the reducer running Phase 0-1 (aspirations-precheck) at all?

WHY THIS EXISTS (measured 2026-08-17 00:5x, alpha reducer, hostname cc-04,
uname -r 6.8.0-137-generic, session ed59f154): between 16:36 and 00:28 the
reducer closed 19 iterations and STARTED the precheck in only a handful of them
(meter `start` mtime 23:05:47, no `end` since 22:40; slate last printed 23:05:54;
zero `phase-0-precheck` diary markers after 20:51). After each autocompact (four
between 23:00 and 00:40) the resumed loop re-entered `Skill(aspirations)` and
then ran "the always-run calls" it REMEMBERED from the compaction summary — a
few scripts (stranded-claim-sweep, quiescence-cache) — straight into
`goal-selector.sh select`, never invoking `Skill(aspirations-precheck)`. The
whole precheck battery (blocker re-probe, guardrail pre-check, cadences,
completed-not-closed drain 0.5g.7, zombie scan, …) was dark for hours while
every iteration looked healthy. This is why a precheck-lane change "takes ages"
to show up on the reducer: the CODE lands within one iteration (iteration-push
merges origin every close), but the LANE only fires when the precheck runs.

WHAT IT MEASURES. "Last precheck activity" = the newest of
  * mtime of  agents/<agent>/session/precheck-budget-state.json  (written by
    `aspirations-precheck-budget-meter.sh start`, precheck Step 0a — a one-shot
    file the meter unlinks at `end`, so its presence+mtime is the START stamp)
  * the newest `ts` in agents/<agent>/session/precheck-drops.jsonl (written by
    `meter check` decisions and the `meter end` summary)
and "iterations closed since" = the count of execution-diary
`phase-12-productivity` `phase_end` entries stamped AFTER it. Both stores are
written by SCRIPTS the precheck runs early, not by an LLM-elective diary marker
(the digest's `phase-0-precheck` markers are LLM-emitted and were measured
absent while the precheck did run — same caveat the digest gives for the
strategic-scan markers).

VERDICT is one line on stdout, always exit 0 (fail-open: this is a detector
that must never block a close). gap >= 1 additionally prints a loud banner
naming the exact next action, because the reader is the loop LLM at the moment
it decides what to do next.

Call sites (both print this as their LAST lines, where the LLM reads):
  * iteration-close.sh do_productivity_check — just above the ITERATION COMPLETE
    imperative, so the banner is in the same tool output the return protocol
    reads every iteration
  * compact-restore-slots.sh — the post-autocompact resume path, which is where
    the abbreviated "always-run calls from memory" shape is born.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def _parse_ts(s):
    """Accept BOTH stamp shapes this detector reads.

    The execution diary writes ISO strings; the budget meter's drop log writes
    `ts` as EPOCH MILLISECONDS (`cur_ms=$(now_ms)` in
    aspirations-precheck-budget-meter.sh — e.g. 1786931051030). The first
    version of this file parsed ISO only, so every drop-log row was silently
    unreadable; and because the meter UNLINKS the state file at `end`, a
    COMPLETED precheck left no readable stamp at all and the detector printed
    "NEVER … 19 iterations closed since" on cc-04 at 01:57 (2026-08-17) when
    the true gap was ~1 (precheck-end at 01:44:11). A production-shape fixture
    (guard-920) pins the ms form now.
    """
    if s is None or s == "":
        return None
    try:
        if isinstance(s, bool):
            return None
        if isinstance(s, (int, float)) or (isinstance(s, str) and s.strip().lstrip("-").isdigit()):
            v = float(s)
            if v > 1e11:          # milliseconds
                v = v / 1000.0
            return datetime.fromtimestamp(v)
        return datetime.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S")
    except Exception:
        return None


def last_precheck_activity(session_dir: Path):
    """Newest of meter-start mtime and newest drop-log ts.

    Returns (best, src, last_end) where last_end is {"ts": datetime,
    "sweeps_ran": int|None} for the newest `precheck-end` event (None if the
    log has no end event). The state file is one-shot — present only while a
    precheck is in flight or was abandoned before `end` — so after a completed
    precheck the drop log is the ONLY stamp; that is why both are read.
    (None, "none", None) if neither exists."""
    best = None
    src = "none"
    last_end = None
    st = session_dir / "precheck-budget-state.json"
    if st.is_file():
        try:
            best = datetime.fromtimestamp(st.stat().st_mtime)
            src = "meter-start"
        except Exception:
            pass
    dl = session_dir / "precheck-drops.jsonl"
    if dl.is_file():
        try:
            newest = None
            with dl.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        ts = _parse_ts(rec.get("ts"))
                    except Exception:
                        continue
                    if not ts:
                        continue
                    if newest is None or ts > newest:
                        newest = ts
                    if rec.get("event") == "precheck-end" and (last_end is None or ts > last_end["ts"]):
                        last_end = {"ts": ts, "sweeps_ran": rec.get("sweeps_ran")}
            if newest and (best is None or newest > best):
                best, src = newest, "meter-log"
        except Exception:
            pass
    return best, src, last_end


def iterations_closed_since(session_dir: Path, since):
    """Count of phase-12-productivity phase_end diary entries after `since`
    (all of them when since is None). Returns (count, newest_close_ts)."""
    diary = session_dir / "execution-diary.jsonl"
    n = 0
    newest = None
    if not diary.is_file():
        return 0, None
    try:
        with diary.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or "phase-12-productivity" not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("phase") != "phase-12-productivity" or d.get("entry_type") != "phase_end":
                    continue
                ts = _parse_ts(d.get("timestamp"))
                if ts is None:
                    continue
                if newest is None or ts > newest:
                    newest = ts
                if since is None or ts > since:
                    n += 1
    except Exception:
        return 0, None
    return n, newest


def compute(session_dir: Path, now=None):
    now = now or datetime.now()
    last, src, last_end = last_precheck_activity(session_dir)
    gap, newest_close = iterations_closed_since(session_dir, last)
    return {
        "last_precheck": last.strftime("%Y-%m-%dT%H:%M:%S") if last else None,
        "last_precheck_source": src,
        "last_precheck_age_min": round((now - last).total_seconds() / 60.0, 1) if last else None,
        "last_precheck_end": last_end["ts"].strftime("%Y-%m-%dT%H:%M:%S") if last_end else None,
        "last_precheck_end_sweeps_ran": last_end["sweeps_ran"] if last_end else None,
        "iterations_closed_since": gap,
        "newest_close": newest_close.strftime("%Y-%m-%dT%H:%M:%S") if newest_close else None,
    }


def render(r, threshold: int) -> str:
    lp = r["last_precheck"] or "NEVER (no meter start/log in this session dir)"
    age = f" ({r['last_precheck_age_min']:g}m ago)" if r["last_precheck_age_min"] is not None else ""
    end = ""
    if r.get("last_precheck_end"):
        end = f"; last precheck END: {r['last_precheck_end']} (sweeps_ran={r.get('last_precheck_end_sweeps_ran')})"
    lines = [f"[precheck-gap] last precheck activity: {lp}{age} via {r['last_precheck_source']}{end}; "
             f"iterations closed since: {r['iterations_closed_since']}"]
    if r["iterations_closed_since"] >= threshold:
        n = r["iterations_closed_since"]
        lines.append(
            f"[precheck-gap] ⚠ PRECHECK GAP: {n} iteration(s) closed since the precheck last RAN. "
            "Phase 0-1 of EVERY iteration is `Skill(aspirations-precheck)` — a Skill call that re-reads "
            "the SKILL.md from disk, NOT 'the always-run calls' remembered from a compaction summary "
            "(that shape ran a handful of scripts and skipped ~40 lanes: blocker re-probe, guardrail "
            "pre-check, cadences, completed-not-closed drain 0.5g.7, zombie scan). Invoke it BEFORE "
            "goal-selector.sh select in the next iteration; the meter `start` it runs is the stamp this "
            "check reads, so a real precheck clears this line by itself.")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--session-dir", help="agent session dir (default: bound agent's session/)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--threshold", type=int, default=1,
                    help="iterations-closed-since at which the loud banner prints (default 1)")
    a = ap.parse_args()
    if a.session_dir:
        sd = Path(a.session_dir)
    else:
        try:
            from _paths import AGENT_DIR  # type: ignore
        except Exception:
            AGENT_DIR = None
        if not AGENT_DIR:
            print("[precheck-gap] no bound agent — skipped")
            return 0
        sd = Path(AGENT_DIR) / "session"
    r = compute(sd)
    if a.json:
        print(json.dumps(r))
    else:
        print(render(r, a.threshold))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # detector must never break a close
        print(f"[precheck-gap] skipped ({type(e).__name__}: {e})")
        sys.exit(0)
