#!/usr/bin/env python3
"""skill-freshness-report.py --  (Master plan Layer 5d).

Cross-reference each SKILL.md's file-modification time against its last
recorded invocation in the cross-agent skill-invocations.jsonl ledger, and
split skills into two actionable cohorts:

  1. stale_modified  (FRESHNESS ALERT)  -- SKILL.md modified AT/AFTER its last
     recorded invocation (mtime >= last_invocation). The current definition has
     NOT been exercised since it changed: the edit is unverified in practice.
     Sorted by gap_days (how long the changed definition has gone un-exercised).

  2. fresh_stable    (PROMOTION CANDIDATE) -- invoked AFTER its last
     modification (last_invocation > mtime) AND the definition has been stable
     for >= stable_days. Battle-tested: in active use, definition unchanged.
     Sorted by days_since_mtime (longest-stable first), then invocation_count.

A third SUPPLEMENTARY bucket, never_invoked_in_window, lists skills with zero
invocations inside the ledger window. It is NOT a freshness alert: the ledger
is a rolling window (currently ~8 days), so "never invoked" means "not in the
last N days", NOT "never ever" -- and user/control skills (start, stop,
open-questions, init, ...) are absent from the model-sourced ledger BY DESIGN.
Each entry carries a `user_invocable` flag so a reader can discount those at a
glance. Keeping this bucket separate is the false-positive control: the two
primary cohorts above only contain skills that DO have invocation data.

Signal calibration (guard-594 -- verify the signal against real data BEFORE
building on it; the g-304-10 lesson):
  Concern: on a OneDrive+git working tree, `git checkout` can reset every file
  st_mtime to checkout-time, which would make every SKILL.md look "fresh" and
  collapse the cohort split. Probed 2026-06-19 against the live repo: SKILL.md
  st_mtime span was 36.23 days (matching real edit history 2026-05-13..06-18)
  and diverged from `git log -1 --format=%cI` by only 0.02d median / 0.06d max.
  So st_mtime is a RELIABLE freshness signal here -- no checkout-reset occurred,
  and st_mtime is far cheaper than 65 per-file git subprocess calls. The
  failure mode is guarded at runtime: if the st_mtime span ever collapses
  below mtime_span_floor_days (default 1.0) across >10 skills, the report emits
  a checkout-reset WARNING and marks the signal unreliable, because that tight
  cluster is the signature of a checkout having rewritten every mtime.

Join-key calibration (rb-245 -- verify the field/key before a per-skill
negative claim): probed 2026-06-19 -- ledger `skill` values match SKILL.md
directory names EXACTLY (0 need slash-stripping; every ledger value maps to a
real dir). So the join is a direct dir-name == ledger-skill equality.

The ledger read is read-only; the script never writes (a reporting tool, not a
discovery-persist tool -- contrast skill-coinvocation-discovery.py --apply).

Cross-references: g-304-14; skill-coinvocation-discovery.py (ledger-read +
agents_root().glob cross-agent pattern reused); skill-discovery.py (days_between
/ last-invocation precedent -- but that tracks FORGED skills via forged_date,
NOT file mtime, so this is NOT a duplicate -- rb-1992 verified); guard-594
(signal calibrated to real data); rb-245 (join-key verified before negative
per-skill claims).

Usage:
  py -3 core/scripts/skill-freshness-report.py [--output json|human]
        [--stable-days N] [--min-window-days N] [--top N]
  Read-only; no --apply. Default output json.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime

try:
    from _stdio import reconfigure_stdio
    reconfigure_stdio()
except Exception:  # pragma: no cover - defensive stdio fallback
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from _paths import PROJECT_ROOT, agents_root

SKILLS_DIR = PROJECT_ROOT / ".claude" / "skills"
TS_FMT = "%Y-%m-%dT%H:%M:%S"

DEFAULT_STABLE_DAYS = 7.0
DEFAULT_MIN_WINDOW_DAYS = 7.0
DEFAULT_TOP = 0  # 0 = no cap (only ~65 skills)
MTIME_SPAN_FLOOR_DAYS = 1.0  # below this, across >10 skills, suspect checkout-reset

# Front-matter user-invocable detector (handles both spellings -- the field is
# spelled inconsistently across the repo: underscore on forged skills, hyphen on
# base skills, per .claude/rules/return-protocol.md).
_USER_INVOCABLE_RE = re.compile(r"(?m)^\s*user[-_]invocable:\s*(true|false)\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_ts(ts):
    try:
        return datetime.strptime(ts, TS_FMT)
    except (ValueError, TypeError):
        return None


def _days(later, earlier):
    """Signed days between two datetimes (later - earlier)."""
    return (later - earlier).total_seconds() / 86400.0


def read_skill_mtimes(skills_dir=None):
    """Map each skill dir-name -> {mtime: datetime, path: str,
    user_invocable: bool|None}. user_invocable parsed from front matter (None if
    no field present). `skills_dir` override exists only for tests."""
    base = skills_dir if skills_dir is not None else SKILLS_DIR
    out = {}
    if not base.exists():
        return out
    for md in sorted(base.glob("*/SKILL.md")):
        name = md.parent.name
        try:
            mtime = datetime.fromtimestamp(md.stat().st_mtime)
        except OSError:
            continue
        user_invocable = None
        try:
            with open(md, "r", encoding="utf-8") as fh:
                head = fh.read(800)
            m = _USER_INVOCABLE_RE.search(head)
            if m:
                user_invocable = (m.group(1).lower() == "true")
        except OSError:
            pass
        out[name] = {"mtime": mtime, "path": str(md), "user_invocable": user_invocable}
    return out


def read_ledger_invocations(root=None):
    """Read every agent's skill-invocations.jsonl via the routed cross-agent
    glob (agents_root().glob -- the audited pattern that auto-tracks an
    AGENTS_PARENT_DIR rename; NEVER a depth-1 PROJECT_ROOT glob). Returns
    (per_skill, window) where per_skill maps skill-name -> {last: datetime,
    count: int} and window is (min_ts, max_ts) datetimes (or (None, None)).
    `root` override exists only for tests."""
    base = root if root is not None else agents_root()
    per_skill = {}
    all_ts = []
    for f in sorted(base.glob("*/skill-invocations.jsonl")):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(r, dict):
                        continue
                    skill = r.get("skill")
                    ts = _parse_ts(r.get("ts", ""))
                    if not skill or ts is None:
                        continue
                    all_ts.append(ts)
                    cur = per_skill.get(skill)
                    if cur is None:
                        per_skill[skill] = {"last": ts, "count": 1}
                    else:
                        cur["count"] += 1
                        if ts > cur["last"]:
                            cur["last"] = ts
        except OSError:
            continue
    window = (min(all_ts), max(all_ts)) if all_ts else (None, None)
    return per_skill, window


def build_report(skills_dir=None, root=None, stable_days=DEFAULT_STABLE_DAYS,
                 min_window_days=DEFAULT_MIN_WINDOW_DAYS, top=DEFAULT_TOP, now=None):
    """Assemble the freshness report. `now` override exists only for tests."""
    now = now or datetime.now()
    mtimes = read_skill_mtimes(skills_dir)
    per_skill, (win_min, win_max) = read_ledger_invocations(root)

    window_days = _days(win_max, win_min) if (win_min and win_max) else 0.0
    data_window_sufficient = window_days >= min_window_days

    # Checkout-reset self-diagnostic (guard-594 failure-mode guard).
    mtime_values = [v["mtime"] for v in mtimes.values()]
    mtime_span_days = _days(max(mtime_values), min(mtime_values)) if len(mtime_values) >= 2 else 0.0
    checkout_reset_suspected = (len(mtime_values) > 10 and mtime_span_days < MTIME_SPAN_FLOOR_DAYS)

    stale_modified = []
    fresh_stable = []
    never_invoked = []

    for name, meta in mtimes.items():
        mtime = meta["mtime"]
        days_since_mtime = _days(now, mtime)
        inv = per_skill.get(name)
        if inv is None:
            never_invoked.append({
                "skill": name,
                "mtime": mtime.strftime(TS_FMT),
                "days_since_mtime": round(days_since_mtime, 2),
                "invocation_count": 0,
                "user_invocable": meta["user_invocable"],
                "note": "no invocations in {:.1f}d window (NOT 'never ever'; "
                        "user/control skills are absent by design)".format(window_days),
            })
            continue
        last = inv["last"]
        gap_days = _days(mtime, last)  # + => modified after last use (stale)
        entry = {
            "skill": name,
            "mtime": mtime.strftime(TS_FMT),
            "last_invocation": last.strftime(TS_FMT),
            "days_since_mtime": round(days_since_mtime, 2),
            "days_since_invocation": round(_days(now, last), 2),
            "invocation_count": inv["count"],
            "gap_days": round(gap_days, 2),
        }
        if gap_days >= 0:
            entry["note"] = ("definition modified {:.2f}d after last invocation -- "
                             "change unexercised since".format(gap_days))
            stale_modified.append(entry)
        elif days_since_mtime >= stable_days:
            entry["note"] = ("invoked {:.2f}d after last modification; definition "
                             "stable {:.1f}d -- battle-tested".format(-gap_days, days_since_mtime))
            fresh_stable.append(entry)
        # else: invoked-after-mod but modified < stable_days ago -- healthy, not reported.

    stale_modified.sort(key=lambda e: e["gap_days"], reverse=True)
    fresh_stable.sort(key=lambda e: (e["days_since_mtime"], e["invocation_count"]), reverse=True)
    never_invoked.sort(key=lambda e: e["days_since_mtime"], reverse=True)

    if top:
        stale_modified = stale_modified[:top]
        fresh_stable = fresh_stable[:top]
        never_invoked = never_invoked[:top]

    return {
        "generated_at": now.strftime(TS_FMT),
        "skills_scanned": len(mtimes),
        "ledger_window_start": win_min.strftime(TS_FMT) if win_min else None,
        "ledger_window_end": win_max.strftime(TS_FMT) if win_max else None,
        "ledger_window_days": round(window_days, 2),
        "data_window_sufficient": data_window_sufficient,
        "min_window_days": min_window_days,
        "stable_days": stable_days,
        "mtime_signal": "git" if checkout_reset_suspected else "st_mtime",
        "mtime_span_days": round(mtime_span_days, 2),
        "checkout_reset_suspected": checkout_reset_suspected,
        "stale_modified_count": len(stale_modified),
        "fresh_stable_count": len(fresh_stable),
        "never_invoked_count": len(never_invoked),
        "stale_modified": stale_modified,
        "fresh_stable": fresh_stable,
        "never_invoked_in_window": never_invoked,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_human(rep):
    print("skill-freshness-report: {} skills | ledger window {}..{} ({:.1f}d, "
          "sufficient={})".format(
              rep["skills_scanned"], rep["ledger_window_start"], rep["ledger_window_end"],
              rep["ledger_window_days"], rep["data_window_sufficient"]))
    if rep["checkout_reset_suspected"]:
        print("  WARNING: SKILL.md mtime span {:.2f}d < {:.1f}d across >10 skills -- "
              "possible git-checkout mtime reset; freshness signal UNRELIABLE "
              "(re-run after confirming working-tree mtimes; consider git-commit-time)".format(
                  rep["mtime_span_days"], MTIME_SPAN_FLOOR_DAYS))
    if not rep["data_window_sufficient"]:
        print("  NOTICE: ledger window {:.1f}d < {:.1f}d -- 'never_invoked' is low-confidence".format(
            rep["ledger_window_days"], rep["min_window_days"]))
    print("\n  STALE-MODIFIED (freshness alert -- {} skills, definition changed after last use):".format(
        rep["stale_modified_count"]))
    for e in rep["stale_modified"]:
        print("    {:<30} mtime={} last_inv={} gap=+{:.2f}d inv={}".format(
            e["skill"], e["mtime"][:10], e["last_invocation"][:10], e["gap_days"], e["invocation_count"]))
    print("\n  FRESH-STABLE (promotion candidate -- {} skills, stable >= {:.0f}d + still invoked):".format(
        rep["fresh_stable_count"], rep["stable_days"]))
    for e in rep["fresh_stable"]:
        print("    {:<30} mtime={} ({:.1f}d stable) inv={}".format(
            e["skill"], e["mtime"][:10], e["days_since_mtime"], e["invocation_count"]))
    print("\n  NEVER-INVOKED-IN-WINDOW (supplementary, NOT an alert -- {} skills):".format(
        rep["never_invoked_count"]))
    for e in rep["never_invoked_in_window"]:
        ui = "user-invocable" if e["user_invocable"] else ("agent" if e["user_invocable"] is False else "?")
        print("    {:<30} mtime={} ({:.1f}d ago) [{}]".format(
            e["skill"], e["mtime"][:10], e["days_since_mtime"], ui))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Report SKILL.md freshness vs last invocation (mtime cross-reference)")
    ap.add_argument("--output", choices=["json", "human"], default="json")
    ap.add_argument("--stable-days", type=float, default=DEFAULT_STABLE_DAYS,
                    help="Min days a definition must be unchanged (while still invoked) "
                         "to count as a fresh_stable promotion candidate")
    ap.add_argument("--min-window-days", type=float, default=DEFAULT_MIN_WINDOW_DAYS,
                    help="Min ledger window to treat 'never invoked' as confident")
    ap.add_argument("--top", type=int, default=DEFAULT_TOP,
                    help="Cap entries per cohort (0 = no cap)")
    args = ap.parse_args(argv)

    rep = build_report(stable_days=args.stable_days,
                        min_window_days=args.min_window_days, top=args.top)

    if args.output == "human":
        _print_human(rep)
    else:
        print(json.dumps(rep, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
