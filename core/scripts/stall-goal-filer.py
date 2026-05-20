#!/usr/bin/env python3
"""Convert loop-stall-warnings.jsonl entries into Unblock: goals on .

Closes the observability-to-backlog gap: stop-hook-analyze.sh writes
<agent>/session/loop-stall-warnings.jsonl when it detects BLOCK streaks,
but nothing reads that file. This filer reads unprocessed entries, files
a HIGH-priority Unblock goal per entry on asp-240 (Cognitive-core hook
reliability follow-ups), and marks the entry goal_filed so we don't
refile on the next sweep.

Dedup rules:
- Skip entry if asp-240 already has a goal tagged with matching
  stall:<sid>:<first_block_ts>.
- Skip entry if another auto-filed stall goal was created for this agent
  within the last 24h (rate limit to avoid flood during a single session).

Usage:
    python3 core/scripts/stall-goal-filer.py [--agent NAME] [--dry-run]

--agent defaults to $MIND_AGENT (current session binding).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))  # so `import _paths` finds the sibling module
import _paths  # noqa: E402  — single source of truth for WORLD_DIR resolution
import _rt  # canonical Python -> daemon client (post-cutover; see _rt.py)

TARGET_ASP_ID = "asp-240"
RATE_LIMIT_HOURS = 24
DEFAULT_PRIORITY = "HIGH"
DEFAULT_CATEGORY = "framework-patterns"
TAG_PREFIX = "stall:"
AGENT_TAG_PREFIX = "stall-agent:"  # per-agent rate-limit key; every filed goal carries one


def resolve_world_aspirations_path() -> Path:
    """Delegate to _paths.WORLD_DIR — do not re-parse local-paths.conf here."""
    return _paths.WORLD_DIR / "aspirations.jsonl"


def read_asp(world_asp_path: Path, asp_id: str) -> dict | None:
    with world_asp_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            a = json.loads(line)
            if a.get("id") == asp_id:
                return a
    return None


def stall_tag(warning: dict) -> str:
    return f"{TAG_PREFIX}{warning['sid']}:{warning['first_block_ts']}"


def already_filed(asp: dict, tag: str) -> str | None:
    """Return the goal_id if  already has a goal carrying this tag."""
    for g in asp.get("goals", []):
        if tag in (g.get("tags") or []):
            return g.get("id")
    return None


def recent_auto_filed(asp: dict, agent: str, now: datetime) -> bool:
    """Return True if an auto-filed stall goal for THIS agent exists in last 24h.

    Per-agent scoping is load-bearing: without the stall-agent:<name> tag check,
    alpha's filer could be rate-limited by bravo's recent filing. Every filed
    goal carries a stall-agent:<name> tag (see build_goal). Do not relax this
    check to a generic loop-stall scan.
    """
    cutoff = now - timedelta(hours=RATE_LIMIT_HOURS)
    agent_tag = f"{AGENT_TAG_PREFIX}{agent}"
    for g in asp.get("goals", []):
        tags = g.get("tags") or []
        if agent_tag not in tags:
            continue
        created = g.get("created_at")
        if not created:
            continue
        try:
            ts = datetime.fromisoformat(created.split(".")[0])
        except ValueError:
            continue
        if ts >= cutoff:
            return True
    return False


def infer_last_goal(agent: str, first_block_ts: str) -> str | None:
    """Return a short hint naming the last goal executed before the stall.

    Single source: <agent>/session/execution-diary.jsonl — per-second timestamps
    and goal_ids. Do NOT add a journal fallback: the journal has date-only
    precision and summarizes sessions, so 'most recent preceding entry' is
    ambiguous on busy days. For stalls older than diary retention, honestly
    return None (caller renders '(none resolved)' in the goal description).

    Returns 'g-XXX-YY: <first 120 chars of content>' when the diary has an
    entry preceding the stall. Returns None when the diary is missing or
    has no preceding entry.
    """
    dpath = _paths.agent_dir(agent) / "session" / "execution-diary.jsonl"
    if not dpath.exists():
        return None
    try:
        stall_ts = datetime.fromisoformat(first_block_ts)
    except ValueError:
        return None
    best: tuple[datetime, dict] | None = None
    with dpath.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            ts_s = rec.get("timestamp")
            if not ts_s:
                continue
            try:
                rec_ts = datetime.fromisoformat(ts_s)
            except ValueError:
                continue
            if rec_ts >= stall_ts:
                continue
            if best is None or rec_ts > best[0]:
                best = (rec_ts, rec)
    if best is None:
        return None
    rec = best[1]
    gid = rec.get("goal_id") or "(no goal_id)"
    content = (rec.get("content") or "").strip()
    return f"{gid}: {content[:120]}" if content else gid


def build_goal(agent: str, warning: dict, last_goal_hint: str | None, now: datetime) -> dict:
    short_ts = warning["first_block_ts"][:16].replace("T", " ")
    blocks = warning["consecutive_blocks"]
    hint = last_goal_hint or "last sub-skill"
    title = (
        f"Unblock: loop-stall {blocks} BLOCKs at {short_ts} — "
        f"investigate {hint[:60]} return-protocol"
    )
    description = (
        f"Auto-filed by stall-goal-filer from {agent}/session/loop-stall-warnings.jsonl.\n\n"
        f"Warning detected: {warning['detected_at']}\n"
        f"Consecutive BLOCKs: {blocks} (threshold {warning.get('threshold')}, "
        f"window {warning.get('window_sec')}s)\n"
        f"First BLOCK: {warning['first_block_ts']}\n"
        f"Last BLOCK: {warning['last_block_ts']}\n"
        f"Session id: {warning['sid']}\n"
        f"Last-goal context (from execution-diary): {last_goal_hint or '(none resolved)'}\n\n"
        f"Recommended action:\n"
        f"  bash core/scripts/skill-branch-terminator-audit.sh\n\n"
        f"Root cause is almost always a procedural branch ending with text output "
        f"instead of a tool call (Bash:/Skill(/invoke /). Every BLOCK in the "
        f"streak is one turn that ended with text, killing the autonomous loop.\n"
    )
    return {
        "title": title,
        "description": description,
        "status": "pending",
        "priority": DEFAULT_PRIORITY,
        "category": DEFAULT_CATEGORY,
        "participants": ["agent"],
        # origin-signal-gate requires concrete signal citation. Stall warnings
        # are the triggering signal here — cite the session id so later audits
        # can trace back to loop-stall-warnings.jsonl.
        "origin_signal": f"unblock:stall-warning-{warning['sid']}",
        "tags": [
            "loop-stall", "return-protocol", "auto-filed",
            stall_tag(warning),
            f"{AGENT_TAG_PREFIX}{agent}",  # load-bearing: recent_auto_filed filters on this
        ],
        "created_at": now.replace(microsecond=0).isoformat(),
        "verification": {
            "outcomes": [
                "Root cause skill identified and fix committed (text-ending → tool call)",
                "skill-branch-terminator-audit.sh reports FAIL=0 after fix",
                "loop-stall-warnings.jsonl entry marked goal_filed=true",
            ],
            "checks": [],
        },
    }


def file_goal(asp_id: str, goal: dict, override_just: str | None = None) -> str | None:
    """File a goal via the daemon; returns the new goal id.

    Returns None only if the daemon returned an error (real framework break,
    not a dedup case).

    override_just (g-115-487): when supplied, passes Override-Duplication to
    the daemon so the goal-duplication-gate is bypassed for genuine stall
    warnings. Without this, the gate's keyword/path overlap heuristic
    silently consumes stall Unblocks whenever recent completions mention the
    same framework context — see bravo finding msg-20260509-044142-bravo-875
    (alpha stall 93e98a77 was rejected by the gate against 6 recent non-self
    completions despite being a genuine novel-action stall warning).
    """
    overrides = {"Duplication": override_just} if override_just else None
    try:
        record = _rt.aspirations_add_goal(
            asp_id, goal, source="world", overrides=overrides)
    except _rt.RtError as e:
        sys.stderr.write(
            f"add-goal failed: {(e.body or str(e)).strip()[:400]}\n")
        return None
    # Daemon response: top-level "goal_id"; legacy CLI returned nested "id".
    # Defensive fallback same shape as cargo-cult-detector.py and
    # blocker-recheck.py:166 (rb-1041 / ). Without this, the daemon's
    # successful add returns goal_id=g-... at top-level but record.get("id")
    # is None — main() at L315 then logs "failed to file goal" for every
    # successful filing AND skips marking the warning as filed (L318-319).
    return record.get("goal_id") or record.get("id")


def rewrite_warnings(warn_path: Path, warnings: list[dict]) -> None:
    tmp = warn_path.with_suffix(warn_path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for w in warnings:
            f.write(json.dumps(w) + "\n")
    os.replace(tmp, warn_path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", help="Agent name (default: $MIND_AGENT)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()

    agent = args.agent or os.environ.get("MIND_AGENT")
    if not agent:
        sys.stderr.write("stall-goal-filer: no agent resolved (pass --agent or set MIND_AGENT)\n")
        return 2

    os.environ["MIND_AGENT"] = agent
    warn_path = _paths.agent_dir(agent) / "session" / "loop-stall-warnings.jsonl"
    if not warn_path.exists():
        print(f"stall-goal-filer: no warnings file at {warn_path}")
        return 0

    world_asp_path = resolve_world_aspirations_path()
    asp = read_asp(world_asp_path, TARGET_ASP_ID)
    if asp is None:
        sys.stderr.write(f"stall-goal-filer: {TARGET_ASP_ID} not found in {world_asp_path}\n")
        return 3

    warnings = []
    with warn_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                warnings.append(json.loads(line))
            except ValueError as e:
                sys.stderr.write(f"stall-goal-filer: bad line skipped ({e})\n")
                continue

    now = datetime.now()
    rate_limited = recent_auto_filed(asp, agent, now)
    processed_any = False
    filed_count = 0
    skipped_count = 0

    for w in warnings:
        if w.get("goal_filed"):
            continue
        tag = stall_tag(w)
        existing_gid = already_filed(asp, tag)
        if existing_gid:
            print(f"stall-goal-filer: {tag} already filed as {existing_gid} — marking")
            w["goal_filed"] = True
            w["goal_id"] = existing_gid
            processed_any = True
            skipped_count += 1
            continue
        if rate_limited:
            print(f"stall-goal-filer: rate-limited (recent auto-filed goal within {RATE_LIMIT_HOURS}h) — skipping {tag}")
            skipped_count += 1
            continue

        hint = infer_last_goal(agent, w["first_block_ts"])
        goal = build_goal(agent, w, hint, now)
        if args.dry_run:
            print(f"[dry-run] would file goal on {TARGET_ASP_ID}: {goal['title']}")
            filed_count += 1
            rate_limited = True  # dry-run models the real rate-limit cascade
            continue
        # Bypass goal-duplication-gate for stall warnings ():
        # the gate's keyword/path heuristic over-fires on stall warnings whenever
        # recent completions reference the same framework context (return-protocol,
        # loop-stall, etc.). The override is justified by the genuine stall_tag,
        # which is unique per (sid, first_block_ts) and not synthesizable from
        # prose. Audit trail lands in world/goal-duplication-overrides.jsonl.
        override_just = (
            f"stall-warning {tag} for agent {agent}: auto-filed by stall-goal-filer "
            f"because the duplication gate over-fires on stall context keyword overlap "
            f"with recent completions. The stall_tag is unique per (sid, first_block_ts) "
            f"and not duplicable. See msg-20260509-044142-bravo-875 / g-115-487."
        )
        new_gid = file_goal(TARGET_ASP_ID, goal, override_just)
        if not new_gid:
            sys.stderr.write(f"stall-goal-filer: failed to file goal for {tag}\n")
            continue
        w["goal_filed"] = True
        w["goal_id"] = new_gid
        print(f"stall-goal-filer: filed {new_gid} for {tag}")
        processed_any = True
        filed_count += 1
        rate_limited = True  # further entries in this run are rate-limited

    if processed_any and not args.dry_run:
        rewrite_warnings(warn_path, warnings)

    summary = f"stall-goal-filer: filed={filed_count} skipped={skipped_count} total={len(warnings)}"
    if args.dry_run:
        summary += " (dry-run: no writes)"
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
