#!/usr/bin/env python3
# domain-leak-exempt: framework observability consumer; no domain strings.
"""sweep-mutation-surface.py (g-115-2676)

Surface silent auto-close sweep mutations to a visible channel.

The three Phase-0.5b precheck sweeps — parent-supersession-sweep (0.5b.6),
unblock-parent-status-sweep (0.5b.7), routing-audit-target-status-sweep
(0.5b.8) — mutate a goal's status to a TERMINAL value (skipped/completed) and
write a metric record, but NOTHING surfaces those mutations. A swept goal
leaves BOTH the selector candidate list AND its blocked list (rb-4149), so the
filer never notices. Canonical incident (2026-07-19): 7 goals silently skipped
for days, one a heartbeat-writer fix whose absence had already produced a live
near-miss — nobody noticed because nothing announced it.

This is the missing CONSUMER. It reads the EXISTING per-sweep metrics logs (no
edits to the sweeps — decoupled, single-source-of-truth), finds apply-mutation
records newer than a per-agent watermark, and:
  - prints a one-line aggregate to stdout (iteration-header observability
    surface, mirrors the Phase 0-pre.0c stash-carryover probe), and
  - with --announce, posts ONE findings-board message naming the swept goal_ids
    so cross-agent filers are notified (findings is cross-agent; fresh-eyes
    Phase 2.3b surfaces untagged findings to every agent).

Fail-open by design: any error prints nothing to stderr-as-fatal and does NOT
advance the watermark (so the next iteration retries). The surface is
observability, never a gate — it must never block the precheck loop.

Discriminator (generic across all 3 sweeps): an apply-mutation record has a
`goal_id` field AND `type != "run_summary"` (the always-written summary record).
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
# Two INDEPENDENT try blocks on purpose. Combining them (g-335-253 first draft,
# caught by fresh-eyes) couples unrelated failures: if _paths lacks
# agent_state_dir, a single combined import loses _WORLD_DIR too — degrading the
# script to "no metrics-dir -> WARN -> surface nothing", which the pre-change code
# survived. Each name gets its own fallback so one missing symbol cannot take the
# other down.
try:
    from _paths import WORLD_DIR as _WORLD_DIR  # noqa: E402
except Exception:  # noqa: BLE001 — test env without _paths falls back to --metrics-dir
    _WORLD_DIR = None
try:
    from _paths import agent_state_dir as _agent_state_dir  # noqa: E402
except Exception:  # noqa: BLE001 — falls back to the manual SCRIPT_DIR derivation below
    _agent_state_dir = None

SWEEP_LOGS = {
    "parent-supersession-sweep-metrics.jsonl": "parent-supersession",
    "unblock-parent-status-sweep-metrics.jsonl": "unblock-parent-status",
    "routing-audit-target-status-sweep-metrics.jsonl": "routing-audit-target",
}
DEFAULT_WINDOW_HOURS = 24  # first-run lookback when no watermark exists
MAX_DISPLAY = 6            # cap the header line so a big batch stays one line


def _parse_ts(s):
    """Parse an ISO timestamp (sweeps write timespec='seconds'). None on failure."""
    if not s or not isinstance(s, str):
        return None
    try:
        return dt.datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _read_watermark(path, now, window_hours):
    """Return the last-surfaced datetime. Missing/unparseable → now - window."""
    fallback = now - dt.timedelta(hours=window_hours)
    try:
        raw = Path(path).read_text(encoding="utf-8").strip()
    except (OSError, IOError):
        return fallback
    ts = _parse_ts(raw)
    return ts if ts is not None else fallback


def _write_watermark(path, now):
    """Advance the watermark. Fail-open (a write miss just re-surfaces next run)."""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(now.isoformat(timespec="seconds"), encoding="utf-8")
        return True
    except (OSError, IOError):
        return False


def _collect_new_mutations(metrics_dir, watermark):
    """Read the 3 logs; return apply-mutation records with timestamp > watermark.

    Generic discriminator: has 'goal_id' AND type != 'run_summary'. Robust to a
    partially-written last line (own-cloud append) — bad lines are skipped.
    """
    out = []
    for fname, short in SWEEP_LOGS.items():
        fpath = Path(metrics_dir) / fname
        try:
            lines = fpath.read_text(encoding="utf-8").splitlines()
        except (OSError, IOError):
            continue  # log absent — that sweep simply never applied anything
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # Cheap pre-filter (this phase is always-run and these logs grow
            # unboundedly — a run_summary is appended every sweep run): an
            # apply-mutation record contains '"goal_id"'; the dominant
            # run_summary records do not. Skip the majority WITHOUT a JSON parse.
            if '"goal_id"' not in line:
                continue
            try:
                rec = json.loads(line)
            except (ValueError, TypeError):
                continue  # tolerate a torn append line
            if not isinstance(rec, dict):
                continue
            if rec.get("type") == "run_summary":
                continue
            gid = rec.get("goal_id")
            if not gid:
                continue
            ts = _parse_ts(rec.get("timestamp"))
            if ts is None or ts <= watermark:
                continue
            out.append({
                "sweep": short,
                "goal_id": gid,
                "type": rec.get("type") or "?",
                "source": rec.get("source") or rec.get("aspiration_id") or "?",
                "agent": rec.get("agent"),
                "timestamp": rec.get("timestamp"),
                "ts": ts,
            })
    out.sort(key=lambda r: r["ts"])
    return out


def _announce_board(mutations, agent):
    """Post ONE aggregate findings-board message. Fail-open; returns msg-id or None.

    Untagged-to-a-specific-agent by design: fresh-eyes Phase 2.3b surfaces a
    finding with no agent tag to EVERY agent, so a cross-agent filer is notified
    (the safe over-notify direction). Names goal_ids so the filer recognizes
    their goal. One post per batch (no per-goal spam)."""
    if not mutations:
        return None
    items = ", ".join(
        "%s→%s(%s)" % (m["goal_id"], "terminal", m["sweep"]) for m in mutations[:12]
    )
    more = "" if len(mutations) <= 12 else " (+%d more)" % (len(mutations) - 12)
    msg = (
        "Auto-close sweep visibility (g-115-2676): %d goal(s) moved to a TERMINAL "
        "status by precheck Phase-0.5b sweeps since last surface — %s%s. A swept "
        "goal leaves BOTH the selector candidate list AND its blocked list "
        "(rb-4149), so if you filed one of these and did not intend it closed, "
        "re-open it (aspirations-update-goal.sh <id> status pending). Applied by "
        "agent=%s." % (len(mutations), items, more, agent or "?")
    )
    # g-115-2681: was `Path(__file__).resolve().parent.parent / "core/scripts/..."`,
    # which resolves to CORE_ROOT (not PROJECT_ROOT) and so built
    # `<root>/core/core/scripts/board-post.sh` — rc=127 on every announce, i.e.
    # the CROSS-AGENT half of this visibility mechanism never fired since it
    # landed. (Local stdout still worked, which is why it went unnoticed.) The
    # `.parent`-as-PROJECT_ROOT class CLAUDE.md's third audit grep names.
    # board-post.sh is a SIBLING in core/scripts, so anchor on SCRIPT_DIR and
    # count no parents at all.
    try:
        from _runtime_bash import BASH  # rb-1472: not bare "bash"
        proc = subprocess.run(
            [BASH, str(SCRIPT_DIR / "board-post.sh"),
             "--channel", "findings", "--type", "finding",
             "--tags", "sweep-auto-close,visibility,g-115-2676"],
            input=msg, capture_output=True, text=True, timeout=30,
        )
        if proc.returncode == 0:
            return (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else "posted"
        print("[sweep-mutation-surface] WARN: board-post rc=%d: %s"
              % (proc.returncode, (proc.stderr or "").strip()[:200]), file=sys.stderr)
    except (subprocess.SubprocessError, OSError) as e:
        print("[sweep-mutation-surface] WARN: board-post failed: %s"
              % str(e)[:200], file=sys.stderr)
    return None


def main():
    ap = argparse.ArgumentParser(description="Surface silent auto-close sweep mutations.")
    ap.add_argument("--metrics-dir", default="",
                    help="Directory holding the *-sweep-metrics.jsonl logs "
                         "(default: resolved WORLD_DIR; override for tests).")
    ap.add_argument("--agent", default=os.environ.get("MIND_AGENT", "") or "",
                    help="Current agent (for the watermark file + announcement).")
    ap.add_argument("--watermark-file", default="",
                    help="Override watermark path (default: agent session dir).")
    ap.add_argument("--announce", action="store_true",
                    help="Also post a findings-board message for the batch.")
    ap.add_argument("--window-hours", type=int, default=DEFAULT_WINDOW_HOURS,
                    help="First-run lookback when no watermark exists (default 24).")
    ap.add_argument("--now", default="", help="Override 'now' (ISO) for tests.")
    ap.add_argument("--no-advance", action="store_true",
                    help="Do not advance the watermark (report-only / tests).")
    ap.add_argument("--output", choices=["human", "json"], default="human")
    args = ap.parse_args()

    # Fail-open wrapper: ANY unexpected error → exit 0, surface nothing.
    try:
        metrics_dir = args.metrics_dir or (str(_WORLD_DIR) if _WORLD_DIR else "")
        if not metrics_dir:
            print("[sweep-mutation-surface] WARN: no --metrics-dir and WORLD_DIR "
                  "unresolved — nothing to surface", file=sys.stderr)
            return 0

        now = _parse_ts(args.now) or dt.datetime.now()

        wm_path = args.watermark_file
        if not wm_path:
            agent = args.agent or "unknown"
            # g-335-253: was `repo = Path(__file__).resolve().parent.parent` then
            # `repo / "agents" / ...`. That `.parent.parent` is CORE_ROOT, not
            # PROJECT_ROOT, so the watermark was written to
            # <root>/core/agents/<agent>/session/ — a path nothing reads. The dedup
            # watermark therefore never persisted: _read_watermark fell back to the
            # window default on EVERY run, so the same mutations re-surfaced forever
            # (silent, because local stdout still looked right — same tell as the
            # board-post bug below). This is the SECOND instance of the
            # `.parent`-as-PROJECT_ROOT class in this very file; the g-115-2681 fix
            # corrected the board-post site and missed this one.
            # Route through the _paths helper instead of re-deriving a root at all:
            # it is the single sync point for AGENTS_PARENT_DIR, and CLAUDE.md
            # "Agent-dir Resolution" forbids joining PROJECT_ROOT/<agent> by hand.
            if _agent_state_dir is not None:
                wm_path = str(_agent_state_dir(agent) / "sweep-surface-watermark")
            else:  # test env without _paths — SCRIPT_DIR is core/scripts, so ../.. is root
                wm_path = str(SCRIPT_DIR.parent.parent / "agents" / agent
                              / "session" / "sweep-surface-watermark")

        watermark = _read_watermark(wm_path, now, args.window_hours)
        mutations = _collect_new_mutations(metrics_dir, watermark)

        # Board announce fires ONCE per mutation: only the SWEEPING agent
        # (metric.agent == self) announces its own mutations. Otherwise, with
        # per-agent watermarks every agent would announce the same mutation →
        # N board posts. The stdout header below still shows ALL new mutations
        # to every agent (that IS the cross-agent visibility); the board post is
        # the single cross-agent NOTIFICATION, made once by the applier.
        own = [m for m in mutations if m.get("agent") and m["agent"] == args.agent]
        board_msg_id = None
        if args.announce and own:
            board_msg_id = _announce_board(own, args.agent)

        if not args.no_advance:
            _write_watermark(wm_path, now)

        if args.output == "json":
            print(json.dumps({
                "new_mutations": len(mutations),
                "own_announce_candidates": len(own),
                "watermark_before": watermark.isoformat(timespec="seconds"),
                "watermark_after": now.isoformat(timespec="seconds"),
                "board_msg_id": board_msg_id,
                "mutations": [{k: v for k, v in m.items() if k != "ts"} for m in mutations],
            }))
            return 0

        # human: quiet on the common empty case (mirrors the stash probe).
        if not mutations:
            return 0
        shown = mutations[:MAX_DISPLAY]
        items = ", ".join("%s(%s)" % (m["goal_id"], m["sweep"]) for m in shown)
        more = "" if len(mutations) <= MAX_DISPLAY else " (+%d more)" % (len(mutations) - MAX_DISPLAY)
        print("▸ ⚠ SWEEP AUTO-CLOSE: %d goal(s) moved to TERMINAL status by "
              "Phase-0.5b sweeps since last surface — %s%s. Swept goals leave "
              "BOTH candidate+blocked lists (rb-4149) — verify intended; re-open "
              "with aspirations-update-goal.sh <id> status pending if not."
              % (len(mutations), items, more))
        if board_msg_id:
            print("    (findings-board notified: %s)" % board_msg_id)
        return 0
    except Exception as e:  # noqa: BLE001 — fail-open is the whole contract
        print("[sweep-mutation-surface] WARN: fail-open on error: %s"
              % str(e)[:200], file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
