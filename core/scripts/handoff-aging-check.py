#!/usr/bin/env python3
"""Handoff-Aging Escalation — bash-enforce precheck Phase 0.5b.2b ().

Scan the world + agent goal queues for cross-agent handoff goals
(`handoff_to` set, routed to an agent OTHER than self) that have aged past
`handoff_aging.escalate_hours` (default 72) with no recent board escalation,
and post one coordination-board visibility note per aged handoff so the
target agent does not miss it.

WHY THIS EXISTS (the gap g-115-1524 closes): Phase 0.5b.2b was previously
LLM-executed pseudocode with NO bash backstop — unlike its sibling Phase
0.5b.1b (inbox-alert age escalation), which IS bash-enforced via
`inbox-alert-age-check.{py,sh}` (g-115-848). Surfaced by a fresh-eyes-review
on 2026-06-18: the agent's `proactive_escalation_log` was EMPTY despite six
cross-agent handoffs aged 78-782h (one to alpha at 782h / 32 days). An
LLM-only phase silently skips under abbreviation; a bash gate runs every
iteration. This script is that gate (rb-428 sentinel-gate family).

Called by aspirations-precheck Phase 0.5b.2b. Reads the world + agent queues
via the daemon (`_rt.aspirations_read`). Dry-run by default; pass --apply to
actually post the board notes.

Cooldown (g-115-1531 — SHARED + DURABLE): a handoff is re-escalated at most
once per `escalate_hours` window across the WHOLE TEAM. The cooldown record is
the escalation board post itself — before posting, the sweep scans the
coordination board (`board-read.sh`) for an existing `handoff-aged` post for
this goal_id (from ANY agent) within the window. This replaced the original
per-agent WM `proactive_escalation_log` cooldown, which had two production-
confirmed bugs (2026-06-18, ~30 duplicate posts for ~7 handoffs): (1) N-agent
duplicate — each of the 6 agents kept its OWN WM log, so all 6 escalated the
same cross-agent handoff independently; (2) non-durable — a WM reset between
iterations wiped the log and re-fired. A shared, durable board scan fixes both:
one post per window regardless of which agent runs the gate, and board posts
persist across WM resets.

Fail-open at every layer (the action is ADDITIVE board visibility, never a
destructive mutation, so — unlike `defer-recheck.py`'s guard-383 fatal
posture for its destructive defer-clearing aggregate — a half-view from one
unreachable source merely means fewer escalations this run, recoverable on
the next sweep; aborting the precheck would be strictly worse):
  - Missing config block          → escalate_hours = 72 (YAML default)
  - daemon unreachable (either src)→ that source yields [], stderr note, continue
  - board-read scan fails          → empty cooldown set (everything eligible fires)
  - board-post failure (per goal)  → log to stderr; --apply continues to remaining

INBOUND PASS (g-115-5811) — the symmetric half, added 2026-08-11. Everything
above is OUTBOUND: handoffs routed AWAY from self, escalated to the board so
the target agent sees them. There was no pass over work routed TO self, so the
one queue an agent is responsible for DRAINING was the only queue nothing aged.
Measured twice in one day (2026-08-11): a HIGH user directive sat pending
through four cycles, and a HIGH goal sat 111h after its block was cleared —
both found BY HAND during unrelated sweeps, with no automated surface producing
either. The inbound view is emitted under the `inbound` key; every pre-existing
key keeps its name, meaning and value, so existing readers are unaffected.

Three things about it that are load-bearing rather than incidental:
  - It ages on handoff_created_at with a created_at (then started) FALLBACK.
    Measured live: only 2 of 196 inbound goals carry handoff_created_at while
    196 carry created_at, so a handoff_created_at-only pass reports a 2-of-196
    view that is indistinguishable from a clean queue. `age_basis` is reported
    per row because on most rows the age is a created_at proxy, NOT routing age.
  - intended_agent == 'either' is NOT inbound. It means unrouted and is the
    dominant value (898 of 1520 pending), so counting it would swallow most of
    the queue.
  - It is REPORT-ONLY and posts nothing. The outbound pass posts because its
    reader is another agent; this pass's reader is the agent already running it.

Exit codes: always 0. Use the JSON output's `applied` count to determine
what changed.

Usage:
    python3 handoff-aging-check.py [--apply] [--escalate-hours N]
                                   [--agent <name>]                  # default $MIND_AGENT
                                   [--inbound-max-report N]          # non-HIGH cap (default 5)
                                   [--no-inbound]                    # skip the inbound pass
                                   [--board-escalation-log <path>]   # tests only
                                   [--no-board]                      # tests only
"""

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import _rt  # canonical Python -> daemon client (post-cutover)
from _runtime_bash import bash_cmd  # : Windows-safe bash resolution


def _parse_iso(s):
    """Parse an ISO-8601 timestamp robustly. Return None on parse failure."""
    if not s or not isinstance(s, str):
        return None
    try:
        return dt.datetime.fromisoformat(s.rstrip("Z"))
    except Exception:
        return None


def _age_hours(iso_ts: str, now: dt.datetime):
    """Hours between `now` and the parsed timestamp. None on parse failure."""
    parsed = _parse_iso(iso_ts)
    if parsed is None:
        return None
    return (now - parsed).total_seconds() / 3600.0


def _load_escalate_hours(args) -> float:
    """Resolve escalate_hours from CLI > YAML handoff_aging.escalate_hours > 72.
    Fail-open: missing YAML or missing key falls back to 72.
    """
    if args.escalate_hours is not None:
        return float(args.escalate_hours)
    try:
        import yaml  # type: ignore
        with open(CORE_ROOT / "config" / "aspirations.yaml", "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        block = (cfg.get("handoff_aging") or {})
        return float(block.get("escalate_hours", 72))
    except Exception as exc:
        sys.stderr.write(
            "handoff-aging-check: config load failed (%s) — using default 72\n" % exc)
        return 72.0


def _load_inbound_max_report(args) -> int:
    """Cap on reported NON-HIGH inbound rows. CLI > YAML > 5.

    Same fail-open shape as _load_escalate_hours. HIGH rows are reported in
    full regardless of this cap (see _inbound_pass) — the cap exists to stop a
    ~200-row backlog being emitted whole, not to hide priority signal.
    """
    cli = getattr(args, "inbound_max_report", None)
    if cli is not None:
        return int(cli)
    try:
        import yaml  # type: ignore
        with open(CORE_ROOT / "config" / "aspirations.yaml", "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        block = (cfg.get("handoff_aging") or {})
        return int(block.get("inbound_max_report", 5))
    except Exception as exc:
        sys.stderr.write(
            "handoff-aging-check: inbound config load failed (%s) — using default 5\n" % exc)
        return 5


def _resolve_self_agent(args) -> str:
    """Resolve the bound agent name. CLI --agent > $MIND_AGENT > ''.

    When unresolved (''), no real handoff_to equals '' so every routed-
    elsewhere handoff is treated as eligible — fail toward escalating
    (additive, safe) rather than silently suppressing.
    """
    if args.agent:
        return args.agent
    return os.environ.get("MIND_AGENT", "") or ""


def _read_goals(source: str) -> list:
    """Return list of pending/in-progress goals from world or agent queue.

    FAIL-OPEN (contrast defer-recheck.py guard-383): defer-recheck makes a
    source RtError FATAL because it merges sources to drive a DESTRUCTIVE
    defer-clear — a silent [] there would clear defers on a half-view. This
    sweep's only action is ADDITIVE board escalation, so a missing source
    just escalates fewer handoffs this run (recoverable next sweep). Aborting
    the precheck would be worse. Each goal is tagged with `_source` and
    `_aspiration_id` for downstream context.
    """
    try:
        out = _rt.aspirations_read(source=source, active=True)
    except _rt.RtError as e:
        sys.stderr.write(
            "handoff-aging-check: %s read failed (%s) — fail-open, skipping source\n"
            % (source, e.body or e))
        return []
    except Exception as e:
        sys.stderr.write(
            "handoff-aging-check: %s read raised (%s) — fail-open, skipping source\n"
            % (source, e))
        return []
    data = _rt.tolerant_decode_aggregate(f"[handoff-aging-check] {source}", out)
    if data is None:
        return []
    goals = []
    for asp in (data.get("aspirations") if isinstance(data, dict) else data) or []:
        for g in asp.get("goals", []) or []:
            if isinstance(g, dict):
                g["_source"] = source
                g["_aspiration_id"] = asp.get("id")
                goals.append(g)
    return goals


def _escalate_window_str(escalate_hours: float) -> str:
    """board-read --since needs an int+unit duration; round up + 1h margin so
    the read window safely covers the full escalate_hours cooldown window."""
    import math
    return "%dh" % (int(math.ceil(escalate_hours)) + 1)


def _read_recent_escalations(escalate_hours: float, now: dt.datetime,
                             board_log_path: Path = None) -> set:
    """Return the SET of goal_ids that already have a `handoff-aged`
    coordination-board post within `escalate_hours` — from ANY agent.

    THE SHARED, DURABLE COOLDOWN (g-115-1531). The board post this script makes
    (`_post_board`, tagged `handoff-aged,<goal_id>,<handoff_to>`) IS the cooldown
    record: it is shared (every agent reads the same coordination board) and
    durable (board posts persist in world/board/, unlike the per-agent WM
    `proactive_escalation_log` slot each agent kept SEPARATELY and that WM resets
    wiped). Scanning the board before posting therefore fixes BOTH original bugs
    at once — the N-agent duplicate (6 agents each escalating the same
    cross-agent handoff, observed 2026-06-18: ~30 posts for ~7 handoffs) and the
    non-durable cooldown (a WM reset between iterations re-fired the same agent).

    Single source of truth (communication-clarity rule 5): the escalation post
    and the cooldown record are ONE artifact — no separate ledger to keep in sync.

    `board_log_path` (tests only): read a JSON list of post dicts directly,
    bypassing the daemon/subprocess board scan.

    FAIL-OPEN: any read failure yields an empty set → no cooldown → eligible
    handoffs re-escalate (additive, board posts are cheap, recoverable next
    sweep). Same direction as the prior empty-log fail-open.
    """
    posts = []
    if board_log_path is not None:
        try:
            with open(board_log_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            posts = data if isinstance(data, list) else []
        except Exception:
            posts = []
    else:
        try:
            proc = subprocess.run(
                bash_cmd(SCRIPT_DIR / "board-read.sh",
                         "--channel", "coordination",
                         "--type", "status",
                         "--since", _escalate_window_str(escalate_hours),
                         "--json"),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            if proc.returncode == 0:
                for line in (proc.stdout or "").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        posts.append(json.loads(line))
                    except Exception:
                        continue
            else:
                sys.stderr.write(
                    "handoff-aging-check: board-read.sh exit=%d stderr=%s — "
                    "fail-open (no cooldown this sweep)\n"
                    % (proc.returncode, (proc.stderr or "").strip()[:200]))
        except Exception as exc:
            sys.stderr.write(
                "handoff-aging-check: board-read.sh exception (%s) — fail-open\n" % exc)

    recent = set()
    for p in posts:
        if not isinstance(p, dict):
            continue
        tags = p.get("tags") or []
        if "handoff-aged" not in tags:
            continue
        age = _age_hours(p.get("timestamp") or p.get("ts"), now)
        if age is None or age >= escalate_hours:
            continue  # outside the cooldown window (or unparseable) — does not suppress
        for t in tags:
            # The escalation tags the goal_id (`g-*`); agent-name tags never do.
            if isinstance(t, str) and t.startswith("g-"):
                recent.add(t)
    return recent


def _post_board(goal: dict, handoff_to: str, age_hours: float, no_board: bool) -> tuple:
    """Post one coordination-board visibility note. Returns (ok, detail).

    When `no_board` is True (tests), skip the subprocess and return (True, "no_board").
    """
    title = goal.get("title", "") or ""
    goal_id = goal.get("id", "") or ""
    age_h = age_hours if age_hours is not None else 0.0
    msg = "Handoff aged %.0fh: %s [%s] waiting on %s" % (age_h, title, goal_id, handoff_to)
    if no_board:
        return True, "no_board"
    try:
        tags = "handoff-aged,%s,%s" % (goal_id, handoff_to)
        proc = subprocess.run(
            bash_cmd(SCRIPT_DIR / "board-post.sh",
                     "--channel", "coordination",
                     "--type", "status",
                     "--tags", tags),
            input=msg,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if proc.returncode == 0:
            return True, "posted"
        sys.stderr.write(
            "handoff-aging-check: board-post.sh exit=%d stderr=%s\n"
            % (proc.returncode, (proc.stderr or "").strip()[:300]))
        return False, "board_post_nonzero:%d" % proc.returncode
    except Exception as exc:
        sys.stderr.write(
            "handoff-aging-check: board-post.sh exception (%s) — skipping post\n" % exc)
        return False, "board_post_exception:%s" % exc.__class__.__name__


def _has_self_clearing_defer(g: dict) -> bool:
    """True when the goal carries an ACTIVE defer that re-probes on its own.

    Only `precondition_unmet:` qualifies. It is the one structured prefix whose
    whole contract is that a sweep re-evaluates it and clears it without a human
    (aspirations-precheck Phase 0.5b), so a row carrying it is attended work on a
    cadence, not an un-attended aged handoff.

    `human_blocked:` is deliberately NOT self-clearing — it never auto-clears by
    design — so it keeps ageing here, which is the behaviour that surfaces it to
    a person. Narrative (unprefixed) defers also keep ageing: an unstructured
    defer has no re-probe contract at all.

    This deliberately matches ONE prefix, not the whole structured set, so it is
    written literally rather than imported: `gates/defer_classifier.py` owns
    STRUCTURED_DEFER_PREFIXES and importing it here would exclude
    `human_blocked:` too, which is the opposite of what this predicate wants. The
    cost of the literal is that a RENAME of that prefix leaves this silently
    matching nothing — so `test_self_clearing_defer_prefix_still_exists_in_ssot`
    asserts the string is still a member of that tuple.

    ONE CARVE-OUT, AND IT IS LOAD-BEARING: a SHELVED recurring goal is never
    treated as self-clearing, however its defer reads. Such a goal shelves
    BECAUSE its precondition keeps failing, so the very contract that makes
    `precondition_unmet:` benign elsewhere is the thing that is not happening
    here — the re-probe runs and fails, forever, silently. guard-2197's own
    specimen (g-115-15) sat 17+ days unattended in exactly this state.

    Without this carve-out the two halves of g-115-7833 cancel: the recurring
    branch in `_inbound_age` deliberately keeps shelved rows ageing on
    `created_at` so they stay visible, and a blanket defer exclusion would then
    drop them anyway. Measured live 2026-09-06 (DESKTOP-O91DLK2) before the
    carve-out existed: g-115-105 (recurring, shelved, achievedCount 386,
    3258.87h) LEFT the reported set, which is precisely the silencing goal
    outcome 2 forbids. Of 107 pending recurring goals, 5 are shelved and 1 is in
    both states — small, and exactly the row that must not vanish.
    """
    raw = g.get("defer_reason")
    if not raw:
        return False
    if g.get("recurring") and g.get("last_shelved_at") \
            and g.get("lastAchievedAt") == g.get("last_shelved_at"):
        return False
    return str(raw).lower().startswith("precondition_unmet:")


def _inbound_age(g: dict, now: dt.datetime) -> tuple:
    """Age of an INBOUND goal, with an explicit fallback chain.

    Returns (age_hours, basis) where basis names WHICH field produced the age,
    or (None, None) when no field parses.

    THE FALLBACK IS NOT DEFENSIVE POLISH — WITHOUT IT THIS PASS SEES ALMOST
    NOTHING. Measured on the live queue 2026-08-11 (cc-08): of the 196 pending
    goals routed to alpha, only 2 carry `handoff_created_at` while 196 carry
    `created_at`. A pass aged solely on handoff_created_at would therefore
    report a 2-goal view of a 196-goal backlog — a number that looks like a
    nearly-clean queue and is indistinguishable from one (guard-1802: measure
    what the predicate EXCLUDES, not what it returns).

    `basis` is returned rather than discarded because it changes what the age
    MEANS: on 194 of those 196 the age is a created_at proxy (how long the goal
    has existed) and NOT how long it has been routed to this agent. Reporting
    the number without the basis would silently overstate routing age.

    RECURRING GOALS AGE FROM `lastAchievedAt`, NOT `created_at` (g-115-7833).
    A recurring goal returns to status:pending on close and NEVER leaves, so its
    `created_at` never advances and its reported age grows without bound forever
    — it is permanently, maximally "aged". Measured 2026-09-06 (DESKTOP-O91DLK2):
    373 of 378 aged inbound rows reached the list on a `created_at` basis, so the
    lane was ~1% signal by its own basis field, and the top of the HIGH list —
    where a reader looks first — was structurally guaranteed noise.

    BUT A FRESH `lastAchievedAt` IS NOT EVIDENCE OF ACHIEVEMENT (guard-2197).
    `recurring-precondition-sweep.py` advances `lastAchievedAt` on every
    iteration where the goal is past its time gate and a structured precondition
    FAILS — deliberately, to stop overdue_ratio inflating — and it never writes
    `achievedCount`. So a SHELVED goal (genuinely stuck, exactly what this
    detector exists to surface) would read as freshly achieved and drop out of
    the list entirely. `achievedCount > 0` does NOT protect against this: a goal
    can carry 43 real past closes and still be shelved right now.

    The discriminator guard-2197 names is the single-read test
    `lastAchievedAt == last_shelved_at => shelved, not achieved`. Measured on the
    live queue the same day: of 107 pending recurring goals, `lastAchievedAt` is
    present on 107 and `last_shelved_at` on 5 — and all 5 are currently shelved,
    including g-115-15 (the goal guard-2197 was itself measured on) and g-115-105
    (3258.76h, on this box's HIGH list). Those five keep aging from `created_at`
    so the fix cannot silence a genuinely stale sensor (goal outcome 2,
    guard-1562 / guard-2499).
    """
    if g.get("recurring") and (g.get("achievedCount") or 0) > 0:
        last_achieved = g.get("lastAchievedAt")
        # Shelved (stamp advanced by the precondition sweep, not by a close) —
        # fall through to created_at so the row keeps aging. guard-2197.
        if last_achieved and last_achieved != g.get("last_shelved_at"):
            age = _age_hours(last_achieved, now)
            if age is not None:
                return age, "lastAchievedAt"

    for field, basis in (("handoff_created_at", "handoff_created_at"),
                         ("created_at", "created_at"),
                         ("started", "started")):
        age = _age_hours(g.get(field), now)
        if age is not None:
            return age, basis
    return None, None


def _inbound_pass(goals: list, self_agent: str, escalate_hours: float,
                  max_report: int, now: dt.datetime) -> dict:
    """Second pass: goals routed TO self that nothing else ages ().

    The outbound pass above enumerates handoffs routed AWAY from self. There was
    no symmetric pass, so the one queue an agent is responsible for DRAINING was
    the one queue nothing aged — measured twice in one day on 2026-08-11
    (a HIGH user directive sat pending through four cycles; a HIGH goal sat 111h
    after its block was cleared), both found BY HAND during unrelated sweeps.

    PREDICATE, measured against the live population rather than assumed:
      status == pending AND (intended_agent == self OR handoff_to == self)
    `intended_agent` is a plain string or None on every live row (never a list),
    and its dominant value is 'either' (898 of 1520 pending) meaning UNROUTED —
    'either' is deliberately NOT inbound, or the pass would swallow 59% of the
    queue and mean nothing. `handoff_to` is set on only 24 pending rows but is
    included because it is the EXPLICIT routing and the outbound pass skips it
    for self by construction (line ~300); on this box it contributes 1 goal the
    intended_agent predicate misses.

    REPORT-ONLY, and that is deliberate. The outbound pass posts to the board
    because its reader is ANOTHER agent who would otherwise never see the
    handoff. This pass's reader is the agent already running it — the precheck
    consumes this stdout every iteration — so a board post would add fleet noise
    with no new reader. Escalation here means NAMING the backlog in the output.

    BOUNDED, because the raw count is noise: 196 candidates on this box. The
    signal is everything HIGH plus the oldest few of the rest. The suppressed
    count is reported so a bounded view is never mistaken for the whole queue.
    """
    scanned_pending = 0
    excluded_self_clearing = 0
    matched = []
    for g in goals:
        if not isinstance(g, dict):
            continue
        if g.get("status") != "pending":
            continue
        scanned_pending += 1
        if not self_agent:
            continue  # unresolved self: no row can be inbound — say nothing
        if g.get("intended_agent") != self_agent and g.get("handoff_to") != self_agent:
            continue
        # A row with an ACTIVE self-clearing defer is NOT un-attended work
        # (). `precondition_unmet:` re-probes on its own cadence
        # (aspirations-precheck Phase 0.5b), so board-escalating it as an aged
        # handoff is noise on a shared surface — strictly worse than a silent
        # stall, because it trains other agents to discount the tag.
        # `human_blocked:` is deliberately NOT excluded: it never auto-clears, so
        # ageing it toward a human is exactly what should happen. Measured
        # 2026-09-06 (DESKTOP-O91DLK2): of 378 inbound rows, 24 carry a defer —
        # 22 precondition_unmet:, 2 human_blocked:.
        if _has_self_clearing_defer(g):
            excluded_self_clearing += 1
            continue
        age, basis = _inbound_age(g, now)
        if age is None:
            matched.append({
                "goal_id": g.get("id", ""),
                "title": g.get("title", ""),
                "priority": g.get("priority"),
                "age_hours": None,
                "age_basis": None,
                "routed_by": "handoff_to" if g.get("handoff_to") == self_agent else "intended_agent",
            })
            continue
        matched.append({
            "goal_id": g.get("id", ""),
            "title": g.get("title", ""),
            "priority": g.get("priority"),
            "age_hours": round(age, 2),
            "age_basis": basis,
            "routed_by": "handoff_to" if g.get("handoff_to") == self_agent else "intended_agent",
        })

    undateable = [m for m in matched if m["age_hours"] is None]
    aged = [m for m in matched
            if m["age_hours"] is not None and m["age_hours"] >= escalate_hours]
    aged.sort(key=lambda m: m["age_hours"], reverse=True)

    # Everything HIGH, plus the oldest non-HIGH up to the cap. HIGH is never
    # truncated: a HIGH goal silently dropped by an output bound reproduces the
    # exact failure this pass exists to fix.
    high = [m for m in aged if m.get("priority") == "HIGH"]
    rest = [m for m in aged if m.get("priority") != "HIGH"]
    reported = high + rest[:max(0, int(max_report))]

    return {
        "self_agent": self_agent,
        "escalate_hours": escalate_hours,
        "scanned_pending": scanned_pending,
        "matched_count": len(matched),
        "aged_count": len(aged),
        "high_count": len(high),
        "undateable_count": len(undateable),
        # Reported, never silent: a row this pass DECLINED to age is a row a
        # reader would otherwise assume was scanned and found clean (guard-1802).
        "excluded_self_clearing_defer": excluded_self_clearing,
        "reported": reported,
        "suppressed_count": max(0, len(aged) - len(reported)),
        "max_report": int(max_report),
        # `lastAchievedAt` MUST appear here (). This tally hardcodes its
        # bases, so a basis missing from this tuple is counted by nothing and the
        # breakdown silently under-reports the population it claims to describe —
        # the same vacuous-reporting shape the basis field exists to prevent.
        "age_basis_breakdown": {
            b: sum(1 for m in matched if m["age_basis"] == b)
            for b in ("handoff_created_at", "lastAchievedAt", "created_at",
                      "started")
        },
    }


# ---- lane-legality limb () --------------------------------------
# WHY: this sweep's predicate was TIME ONLY. A handoff addressed to an agent a
# standing lane pin FORBIDS from claiming it ages FOREVER -- the sweep re-reports
# it on cooldown indefinitely and no escalation it emits can ever resolve it.
# MEASURED 2026-09-15 (alpha, cc-04): 12 aged handoffs, ALL handoff_to=foxtrot,
# ages 97.67 / 113.89 (x8) / 520.02 / 525.31 / 526.36 hours -- three waiting ~22
# DAYS -- and roughly half were CODE work that capability-routing.md pin-001
# (user directive 2026-08-06) puts out of foxtrot's lane. Foxtrot was alive the
# whole time; gates/lane_pin.py REFUSES those claims at the daemon endpoint, so
# the assignee could not have taken them even by trying.
#
# This is the RULE axis of .claude/rules/reclaim-routed-work.md: the premise
# ("foxtrot owns that surface") was retired by a directive while the routing
# FIELD kept pointing there, and every sweep since tested AGE and never whether
# the reason was still a valid reason. Per that rule's #7 the reclaim predicate
# was narrower than the population it had to drain, so the sweep reported
# "on cooldown" forever and read as working.
#
# The verdict is NOT re-derived here (guard-4883 / guard-2676): it comes from
# gates/lane_pin.py::evaluate -- the same function the claim endpoint runs --
# called with the REAL goal, so this sweep agrees with the gate that would
# actually refuse the claim rather than approximating it.

_LANE_UNKNOWN = "unknown"


def _world_dir():
    """Resolved WORLD path, or None. Never raises."""
    try:
        if str(SCRIPT_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPT_DIR))
        from _paths import WORLD_DIR
        return WORLD_DIR
    except Exception:
        return None


def _registry_text(world_dir):
    """The lane-pin registry markdown, or None if unreadable.

    READ ONCE PER RUN and threaded into every evaluate() call. The cheap reason
    is N file reads. The LOAD-BEARING reason is g-115-5226: handed neither
    registry_text nor a world_dir, evaluate() returns verdict="no-pin" --
    BYTE-IDENTICAL to "this agent has no pin" -- so a wiring mistake reads as a
    clean, confident, structurally-inert PASS with nothing to notice. Resolving
    the registry here, once, is what lets this sweep tell "no pin for this
    agent" from "I could not read the registry at all" and report UNKNOWN for
    the latter instead of silently declaring every aged handoff legitimate.
    """
    if world_dir is None:
        return None
    try:
        from gates import lane_pin as _lane_pin
        text = Path(world_dir).joinpath(
            _lane_pin.REGISTRY_RELPATH).read_text(encoding="utf-8")
    except Exception:
        return None
    # AN EMPTY READ IS A DEGRADED READ, NOT AN EMPTY REGISTRY (
    # fresh-eyes, 2026-09-18). The try/except above catches an unreadable
    # registry, but a file that reads as "" sails through it -- and "" handed
    # to evaluate() yields verdict="no-pin", byte-identical to "this agent has
    # no pin", with registry_readable reported TRUE. That is precisely the
    #  failure this function exists to prevent, one step in: a
    # confident, structurally-inert PASS declaring every aged handoff
    # legitimate. Not hypothetical on this fleet -- rb-2970 measures reads
    # transiently returning EMPTY on the S3-backed own-cloud mount while a
    # file settles. Degrade to the unknown path instead (rb-3099, rb-5242).
    if not text.strip():
        return None
    return text


def _lane_verdict(goal, handoff_to, registry_text, world_dir) -> dict:
    """Can `handoff_to` LEGALLY claim this goal?

    Returns {"verdict", "confident", "pin_id", "evidence", "reason"}. `verdict`
    is one of lane_pin's own words -- in-lane / out-of-lane / ambiguous / no-pin
    -- plus "unknown" when the registry was unreadable or the gate could not run.

    `confident` is deliberately NARROW: True only when lane_pin would BLOCK
    (would_block=True AND verdict=="out-of-lane"). That is the exact condition
    the daemon claim endpoint refuses on, so a confident mis-route is a handoff
    whose claim provably cannot succeed. EVERY other state -- ambiguous (both
    lane columns matched), in-lane, no-pin, unknown, or any exception -- is not
    confident and is left alone. The asymmetry is the whole posture: a false
    re-route steals a partner's legitimate work, while a false leave-alone is
    merely the status quo this sweep already produces.
    """
    base = {"verdict": _LANE_UNKNOWN, "confident": False, "pin_id": None,
            "evidence": [], "reason": ""}
    if registry_text is None:
        base["reason"] = "lane-pin registry unreadable — no verdict attempted"
        return base
    try:
        from gates.lane_pin import evaluate as _pin_evaluate
    except Exception as exc:
        base["reason"] = "lane_pin import failed: %s" % exc.__class__.__name__
        return base
    try:
        res = _pin_evaluate(handoff_to, goal, registry_text=registry_text,
                            world_dir=world_dir)
    except Exception as exc:
        base["reason"] = "lane_pin raised: %s" % exc.__class__.__name__
        return base
    if not isinstance(res, dict):
        base["reason"] = "lane_pin returned %s, expected dict" % type(res).__name__
        return base
    verdict = res.get("verdict") or _LANE_UNKNOWN
    evidence = list(res.get("evidence") or [])[:4]
    # SPLIT "in-lane" FROM "unmatched", and do not let the gate's own word stand
    # (measured on the live population, 2026-09-18). lane_pin is calibrated for
    # the CLAIM decision, where allow-on-doubt is correct: a pin exists but
    # NEITHER column matched the goal, so it returns verdict="in-lane" with
    # reason "in-lane-or-unmatched" and an EMPTY evidence list. Reusing that
    # word verbatim for a ROUTING decision silently converts "the pin does not
    # settle this goal" into "this handoff is legitimate" -- the same
    # empty-evidence blindness  added evidence-naming to expose.
    # Measured: 5 of 12 aged handoffs land here, including three "Wire <X>
    # action ..." goals that ARE the client Lua pin-001 forbids -- the pin names
    # ARTIFACTS ("client lua", "analyzers") while the goals name OUTCOMES, so a
    # token join between them cannot fire in either direction. Keyed on the
    # empty evidence rather than the reason STRING, which is the SSOT's own
    # signal and does not break if the wording changes.
    if verdict == "in-lane" and not evidence:
        verdict = "unmatched"
    return {"verdict": verdict,
            "confident": bool(res.get("would_block")) and verdict == "out-of-lane",
            "pin_id": res.get("pin_id"),
            "evidence": evidence,
            "reason": res.get("reason") or ""}


def _reroute(goal: dict, lane: dict, no_write: bool) -> tuple:
    """Clear `handoff_to` so a confidently mis-routed goal returns to the pool.

    Returns (ok, detail). `handoff_to` is optional and additive in the goal
    schema -- "goals without handoff_to are unchanged" -- so clearing it drops
    both the other-agent selector penalty and this sweep's aging escalation,
    which IS "route it back to the fleet". `handoff_from` and
    `handoff_created_at` are deliberately LEFT INTACT: they record who routed it
    and when, and that history is what makes the re-route auditable rather than
    a field silently going missing.

    The clearing form is the literal string "null" (measured: an empty value is
    refused with "goal_id, field, and value are all required"; "null" writes
    JSON null).
    """
    goal_id = goal.get("id", "") or ""
    source = goal.get("_source", "world") or "world"
    if no_write:
        return True, "no_write"
    try:
        proc = subprocess.run(
            bash_cmd(SCRIPT_DIR / "aspirations-update-goal.sh",
                     "--source", source, goal_id, "handoff_to", "null"),
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60)
        if proc.returncode != 0:
            sys.stderr.write(
                "handoff-aging-check: reroute of %s exit=%d stderr=%s\n"
                % (goal_id, proc.returncode, (proc.stderr or "").strip()[:300]))
            return False, "reroute_nonzero:%d" % proc.returncode
    except Exception as exc:
        sys.stderr.write("handoff-aging-check: reroute of %s raised (%s)\n"
                         % (goal_id, exc.__class__.__name__))
        return False, "reroute_exception:%s" % exc.__class__.__name__
    # Audit on the goal itself. Best-effort: the clear above already landed, and
    # a reader who finds handoff_to simply GONE with no reason on the record is
    # the failure this append exists to prevent.
    try:
        note = ("handoff_to cleared by handoff-aging-check lane-legality limb "
                "(g-115-10020): target %r is forbidden from claiming this goal by "
                "lane pin %s, so the handoff could never be honoured and was "
                "ageing indefinitely. lane_pin evidence: %s. handoff_from and "
                "handoff_created_at left intact as the routing history."
                % (goal.get("handoff_to"), lane.get("pin_id"),
                   ", ".join(str(e) for e in (lane.get("evidence") or [])) or "none"))
        subprocess.run(
            bash_cmd(SCRIPT_DIR / "goal-field-append.sh",
                     "--source", source, goal_id, "progress_note",
                     "lane-misroute-reroute-%s" % goal_id, note),
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60)
    except Exception:
        pass
    # Tell the fleet. The assignee is losing a row from its queue and must not
    # discover that by noticing an absence; the board is the shared record that
    # survives whichever box either of us is on (guard-997).
    try:
        msg = ("Handoff MIS-ROUTED and cleared: %s [%s] was routed to %s and aged "
               "%.0fh, but lane pin %s FORBIDS that agent from claiming it, so the "
               "handoff could never be honoured. handoff_to cleared -> the goal is "
               "fleet-claimable again; handoff_from/handoff_created_at left intact. "
               "lane_pin evidence: %s. Verdict came from gates/lane_pin.py::evaluate, "
               "the same function the claim endpoint runs. Re-route wrongly? Set "
               "handoff_to back and say so on the goal."
               % (goal.get("title", "") or "", goal_id, goal.get("handoff_to"),
                  float(goal.get("_age_hours") or 0.0), lane.get("pin_id"),
                  ", ".join(str(e) for e in (lane.get("evidence") or [])) or "none"))
        subprocess.run(
            bash_cmd(SCRIPT_DIR / "board-post.sh",
                     "--channel", "coordination", "--type", "status",
                     "--tags", "handoff-misrouted,%s,%s" % (goal_id, goal.get("handoff_to") or "")),
            input=msg, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30)
    except Exception:
        pass
    return True, "rerouted"


def _lane_split(candidates, registry_text) -> dict:
    """Per-verdict tally of the aged population, plus the ids in each bucket.

    `registry_readable` is reported explicitly rather than left to be inferred
    from an all-"unknown" tally: those two states look identical in the counts
    and mean opposite things (a degraded run vs a fleet with no pins).
    """
    buckets = {}
    for c in candidates:
        buckets.setdefault(c["lane"]["verdict"], []).append(c["goal_id"])
    return {
        "registry_readable": registry_text is not None,
        "mis_routed": [c["goal_id"] for c in candidates if c["mis_routed"]],
        "mis_routed_count": sum(1 for c in candidates if c["mis_routed"]),
        "by_verdict": {k: sorted(v) for k, v in sorted(buckets.items())},
    }


def run(args) -> dict:
    """Main sweep. Returns the JSON-shape result dict (also printed to stdout)."""
    escalate_hours = _load_escalate_hours(args)
    self_agent = _resolve_self_agent(args)
    now = dt.datetime.now()
    goals = _read_goals("world") + _read_goals("agent")
    board_log_path = Path(args.board_escalation_log) if args.board_escalation_log else None
    recent_escalations = _read_recent_escalations(escalate_hours, now, board_log_path)

    # Lane-legality inputs, resolved ONCE (see _registry_text for why this is
    # not left to evaluate()'s own lookup — ).
    world_dir = _world_dir()
    registry_text = _registry_text(world_dir)
    if registry_text is None:
        sys.stderr.write(
            "handoff-aging-check: lane-pin registry unreadable (world_dir=%r) — "
            "every candidate reports lane verdict 'unknown' and NOTHING is "
            "re-routed this run. This is a degraded run, not a clean one.\n"
            % (str(world_dir) if world_dir else None,))

    candidates = []
    for g in goals:
        if not isinstance(g, dict):
            continue
        if g.get("status") not in ("pending", "in-progress"):
            continue
        ht = g.get("handoff_to")
        if not ht or ht == self_agent:
            continue  # only goals routed to ANOTHER agent
        created = g.get("handoff_created_at")
        if not created:
            continue
        age = _age_hours(created, now)
        if age is None or age < escalate_hours:
            continue
        goal_id = g.get("id", "")
        lane = _lane_verdict(g, ht, registry_text, world_dir)
        candidates.append({
            "goal_id": goal_id,
            "title": g.get("title", ""),
            "handoff_to": ht,
            "age_hours": round(age, 2),
            "blocker_id": "handoff_%s" % goal_id,
            "on_cooldown": goal_id in recent_escalations,
            "lane": lane,
            "mis_routed": lane["confident"],
        })

    fired = []
    skipped_cooldown = []
    failed = []
    rerouted = []
    if args.apply:
        for c in candidates:
            full = next((g for g in goals if g.get("id") == c["goal_id"]), None)
            # MIS-ROUTED outranks the cooldown. The cooldown exists to stop the
            # same AGING notice being re-posted; a re-route is a different act
            # that resolves the row permanently, and suppressing it on cooldown
            # is exactly how this population sat 98-526h (guard-6571: a verdict
            # this sweep computes and never applies is a promise of effect).
            if c["mis_routed"] and not getattr(args, "no_reroute", False):
                if full is None:
                    continue
                full["_age_hours"] = c["age_hours"]
                ok, detail = _reroute(full, c["lane"], args.no_board)
                (rerouted if ok else failed).append({
                    "goal_id": c["goal_id"],
                    "handoff_to": c["handoff_to"],
                    "age_hours": c["age_hours"],
                    "pin_id": c["lane"].get("pin_id"),
                    "evidence": c["lane"].get("evidence"),
                    "detail": detail,
                })
                continue
            if c["on_cooldown"]:
                skipped_cooldown.append(c["goal_id"])
                continue
            if full is None:
                continue
            ok, detail = _post_board(full, c["handoff_to"], c["age_hours"], args.no_board)
            # No separate cooldown write: the board post above IS the shared,
            # durable cooldown record the next sweep (any agent) reads.
            if ok:
                fired.append({
                    "goal_id": c["goal_id"],
                    "handoff_to": c["handoff_to"],
                    "age_hours": c["age_hours"],
                    "detail": detail,
                })
            else:
                failed.append({
                    "goal_id": c["goal_id"],
                    "handoff_to": c["handoff_to"],
                    "detail": detail,
                })

    result = {
        "mode": "apply" if args.apply else "dry_run",
        "self_agent": self_agent,
        "escalate_hours": escalate_hours,
        "scanned": len(goals),
        "candidates": candidates,
        "candidate_count": len(candidates),
        "applied": len(fired),
        "fired": fired,
        "skipped_cooldown": skipped_cooldown,
        "failed": failed,
        "rerouted": rerouted,
        "lane_split": _lane_split(candidates, registry_text),
    }
    # Inbound pass (). ADDITIVE: every key above is unchanged in name,
    # meaning and value, so existing readers of this JSON are unaffected. The
    # inbound view lives entirely under its own `inbound` key.
    # getattr rather than attribute access: run() is importable and callers
    # build their own Namespace (the test helper does). A hard access makes
    # every pre-existing caller AttributeError the moment a new optional flag
    # lands — which is exactly what happened when this pass was first wired.
    if not getattr(args, "no_inbound", False):
        result["inbound"] = _inbound_pass(
            goals, self_agent, escalate_hours,
            _load_inbound_max_report(args), now)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--apply", action="store_true",
                   help="Actually post board notes and append cooldown entries (default: dry-run).")
    p.add_argument("--escalate-hours", type=float, default=None,
                   help="Override the aging threshold (default: config handoff_aging.escalate_hours or 72).")
    p.add_argument("--agent", default=None,
                   help="Self agent name; handoffs routed to this agent are skipped (default: $MIND_AGENT).")
    p.add_argument("--board-escalation-log", default=None,
                   help="Test-only: path to a JSON file of coordination-board posts standing in for the live board scan.")
    p.add_argument("--no-board", action="store_true",
                   help="Test-only: skip the board-post.sh subprocess and pretend it succeeded.")
    p.add_argument("--inbound-max-report", type=int, default=None,
                   help="Cap on reported NON-HIGH inbound rows (default: config "
                        "handoff_aging.inbound_max_report or 5). HIGH rows are never capped.")
    p.add_argument("--no-reroute", action="store_true",
                   help="Compute and report lane verdicts but never clear handoff_to "
                        "(escape hatch; --apply otherwise re-routes confident mis-routes).")
    p.add_argument("--no-inbound", action="store_true",
                   help="Skip the inbound pass entirely (escape hatch; the outbound "
                        "result keys are unaffected either way).")
    args = p.parse_args()
    result = run(args)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
