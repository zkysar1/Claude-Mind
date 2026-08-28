#!/usr/bin/env python3
# domain-leak-exempt: NARRATIVE_PATTERNS literally lists domain phrases ("game
# session", "post-processor game session", etc.) so the auditor can detect
# narrative defers that the agent could resolve itself. The terms are data
# powering capability-routing classification, not engine logic.
"""Audit all deferred goals for stale narrative defers.

Scans world/aspirations.jsonl + every <agent>/aspirations.jsonl, classifies
each goal with a non-null defer_reason into one of three categories:

  a) genuine — legitimate block (time-gated, sequential dep, agent-locked
     physical action, partner-claimed work)
  b) ambiguous — defer language is plausible but the underlying state is
     reachable via agent action (e.g., "needs game session data" when a
     game session is something the agent can launch)
  c) narrative-only — the defer reads like a narrative excuse rather than
     a structural block (matches NARRATIVE_PATTERNS, "both abstained",
     "needs user attention" without capability evidence, etc.)

Output: JSON to stdout (default) or markdown report to a path via --report.

Sibling to capability-gate.py — that gate fires at write time on new
defers/blockers. This audit catches PRE-existing defers that predate or
slipped past the gate.

Origin: g-255-06 / g-255-02 lineage.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

# Single source of truth for WORLD_DIR resolution. _paths.py reads the same
# local-paths.conf via _resolve_agent_name + _read_local_paths and applies the
# MIND_WORLD env override identically. Importing it here eliminates the prior
# divergence where this file's own _resolve_world_dir() hardcoded
# bravo-before-alpha agent fallback while _paths.py uses sorted(glob).
sys.path.insert(0, str(SCRIPT_DIR))
from _paths import WORLD_DIR, agents_root as _agents_root  # noqa: E402


# ---- Classifier ------------------------------------------------------------

# Narrative patterns from capability-gate.py NARRATIVE_PATTERNS plus extras
# specific to defer-reason audits (gate's list is for failure_reason on
# blockers; this list adds defer-flavored phrasings).
DEFER_NARRATIVE_PATTERNS = [
    # capability-gate's NARRATIVE_PATTERNS (synced 2026-04-25)
    "user approves", "user approved", "user authorizes", "user authorized",
    "waiting for user decision", "user-leg scope: approval",
    "user-leg: approval", "user must", "user needs to", "user should",
    "pending user sign-off", "pending user review",
    "blocked on user-initiated", "blocked on user action",
    # defer-specific extensions
    "both agents abstained",
    "needs user attention",
    "requires user setup",
    "cannot be triggered via api",  # often false: capability-routing may have [agent, user] split
]

# Cat-A signals — structurally legitimate defers
# g-303-21 / zeta allowlist audit 8b: GENUINE_PREFIXES/GENUINE_PATTERNS are
# AUDIT-side heuristics ("is this existing defer genuine?"), deliberately broader
# than and distinct in PURPOSE from gates/defer_classifier.STRUCTURED_DEFER_PREFIXES
# (which ROUTES new defers). They overlap (precondition_unmet:, blocked_on_dependency)
# but differ by design: the audit adds "time-gated:" and handles circuit-breaker via
# the GENUINE_PATTERNS free-text list below, not a prefix. GENUINE_PATTERNS has no
# source anchor (content heuristics). A forced parity test would false-fail.
# => document, no test.
#
# `human_blocked:` (g-115-3805) — the comment above enumerates the divergence's
# ADDITIONS and never mentioned this OMISSION, so of defer_classifier's four
# canonical prefixes exactly one reached no path here. The divergence otherwise
# STANDS: do not import the SSOT tuple or force parity (defer_classifier.py:13-14
# names three call sites that must stay aligned, and this audit is deliberately
# not one of them).
#
# This one belongs because of WHERE the omission bit. The 14-day age downgrade
# lives INSIDE `if pfx:` — so for an unrecognised prefix `pfx` is None, that
# branch never runs, and the row can never become a stale-structured re-check
# candidate AT ANY AGE. That pointed at exactly the wrong prefix:
# `.claude/rules/probe-before-defer.md` rule 1e makes `human_blocked:` the ONE
# member that never auto-clears (goal-selector exempts it from the 120h
# fall-through) and says verbatim that, because it never expires on its own, it
# is the defer MOST exposed to the RULE axis. The prefix the rule names as most
# needing periodic re-derivation was the one this lane was structurally blind to
# — guard-1802's shape: a reclaim predicate diverged from the set that creates
# its population, so the sweep reports clean by construction.
#
# MEASURED 2026-08-10 (bravo, hostname cc-05, uname -r 6.8.0-136-generic) on the
# live queue: 11 of 36 rows (30.6%) carry this prefix; before the fix they split
# cat-b 10 / cat-c 1 with ZERO reaching stale-structured, and TWO were already
# past the 14d threshold (g-240-101 at 18.5d, g-115-2050 at 14.0d) — confirming
# a prediction filed on 2026-08-04 that the first miss would land ~08-05/06.
# Adding the prefix needs no new machinery: the population routes into the
# EXISTING age path (fresh -> cat-A, past --stale-days -> cat-B stale-structured).
#
# Second, smaller half, fixed as a side effect and VERIFIED not assumed: falling
# through to the pattern matchers, a `human_blocked:` defer met
# DEFER_NARRATIVE_PATTERNS ("user approved", "user must", ...) — the exact
# vocabulary a legitimate human-gate defer uses to DESCRIBE its gate, so the
# better it documented what the human must do, the likelier this lane called it
# an excuse. The prefix branch returns before c_hits is computed, so recognising
# the prefix stops it reaching the narrative matcher at all.
#
# `time-gated:` is retained but is DEAD as of the same measurement: 0 matches and
# 0 near-misses across the live corpus (this closes one of g-115-3805's own
# "EXPLICITLY UNMEASURED" items). Left in place deliberately — it is harmless,
# and removing a defensive allowlist member would silently reclassify any future
# writer that adopts it.
GENUINE_PREFIXES = (
    "blocked_on_dependency:",
    "precondition_unmet:",
    "time-gated:",
    "human_blocked:",
)

# Age past which a structured-prefix defer stops being self-certifying and
# becomes a re-check candidate. See classify() for the full rationale and
# `.claude/rules/reclaim-routed-work.md` rule 2 ("well-formed is not valid").
# Tunable via --stale-days. 14d matches the pending-questions-sweep
# auto_resolve horizon so the reclaim lanes age items on one clock.
STALE_STRUCTURED_DAYS = 14.0

GENUINE_PATTERNS = [
    "weeks of post-",
    "weeks of live",
    "week measurement window",
    "month monitoring window",
    "12 months",
    "monitoring window",
    "post-ship is too early",
    "post-deploy",
    "circuit breaker",  # circuit-breaker style timed defers
    "multi-hour test-implementation",  # partner test work, persists across sessions
    "must ship+settle",
    "natural /stop",  # event-gated by orchestrator
    "observation window",
    "telemetry accumulated",
    "let predicate.py bake",
    "settle before",
    "t+",  # t+14d, T+14d (classify() lowercases the haystack into `lo` before
           # calling _has_any, so every needle in this list MUST be lowercase or
           # it can never match. Named by expression, not line number: the prior
           # form cited "classify():134" and drifted 125 lines out of date in a
           # single edit — a stale pointer reads as authoritative.
    "machine restart to clear",  # purely physical, but mark it for review (could be Unblock)
]

# Cat-B signals — ambiguous: defer mentions data that the agent COULD
# obtain by triggering a game session OR processor run, but only when
# such triggering is feasible right now (no live session, etc).
AMBIGUOUS_PATTERNS = [
    "post-processor game session",
    "post-fix game session",
    "no post-processor",
    "no post-fix",
    "needs processor run",
    "no processor run",
    "no live player sessions",
    "5 commits awaiting ci",
    "consolidatedmemory strategy",
    "structurechangeprocessor.lua algorithm",
]


def _has_any(text_lo: str, needles: list) -> list:
    return [n for n in needles if n in text_lo]


def _has_prefix(text: str, prefixes: tuple) -> str | None:
    s = text.lstrip()
    for p in prefixes:
        if s.lower().startswith(p):
            return p
    return None


def _defer_age_days(defer_set_at) -> float | None:
    """Age of the defer in days, or None when unparseable/absent.

    Fail-open by design (guard-142): an unreadable timestamp yields None, and
    every caller treats None as "not stale" — an audit heuristic must never
    manufacture staleness out of a parse failure.
    """
    if not defer_set_at:
        return None
    try:
        parsed = datetime.fromisoformat(str(defer_set_at)[:19])
    except Exception:
        return None
    return (datetime.now() - parsed).total_seconds() / 86400.0


# A defer that NAMES its own resolution date is not stale until that date
# arrives. The staleness test is age-since-defer_set_at ONLY, so a defer
# carrying an explicit machine-readable future window was flagged identically to
# one carrying no date at all -- and then demanded a full two-axis re-derivation
# on EVERY precheck iteration until its window closed. Measured 2026-08-10
# (bravo, cc-05): of 7 live defers naming a keyed date, THREE will cross 14d
# while their own window is still open -- g-115-4946 by 1 day, g-335-646 by 23,
# g-335-648 by 38. That is the treadmill that trains a reader to skim lane B,
# which is how a REAL stale defer gets missed.
#
# THREE constraints, all deliberate:
#  1. Do NOT raise STALE_STRUCTURED_DAYS instead. That delays genuine stale hits
#     by the same amount and cannot distinguish the two cases at all.
#  2. Suppress on the NAMED KEY, never on a bare date match. A "by DATE
#     deadline" due-date is NOT a defer-until date (sibling lesson: the
#     _extract_defer_date semantic inversion), so a loose date scan would read
#     an urgency deadline as a licence to stay stopped -- inverting the meaning.
#  3. Only a date that PARSES and is in the FUTURE suppresses. Unparseable or
#     past dates keep current behavior, matching this file's fail-open posture
#     (guard-142): an audit heuristic must never manufacture staleness from a
#     parse failure, and must never launder a CLOSED window into genuineness.
#
# `\D{0,32}` cannot cross another date (dates contain digits), so widening the
# gap can never capture a different date than the one this key introduces. Text
# between key and date that contains a digit yields NO match -- under-matching,
# which is the safe direction here: a missed suppression costs one re-derivation,
# a wrong suppression hides a genuinely stale defer.
# WIDENED 2026-08-28 (g-115-5639) with FOUR measured keys. Each was chosen by
# scanning the LIVE corpus, never by guessing a phrasing: of 158 live defers, 108
# carry a date with no existing key — and nearly all of those dates are
# MEASUREMENT TIMESTAMPS ("Probed 2026-08-16", "Measured on cc-07 2026-08-26"),
# which is constraint 2 demonstrated at scale. A bare-date scan would suppress
# ~108 defers, most of them wrongly. Live hit counts at the time of widening:
#   resolves_no_earlier_than  2  (a real structured key nobody had registered)
#   gated until               1
#   ACTIVE until              2  (deploy-hold windows)
#   window open until         2  (hypothesis windows)
# NOT added, because they have ZERO live hits and every unused alternative is
# surface a future false positive can enter through: "no earlier than",
# "not before".
# `until` is NOT admitted bare. It is anchored to the two attested contexts
# ("ACTIVE until", "window open until"): a bare \buntil would match a future
# deadline phrasing and invert the meaning exactly as constraint 2 forbids.
# Before/after delta on the live corpus (2236 goals): 8 -> 15 matches, ADDED 7,
# REMOVED 0 — no row that was a finding before stops being one (guard-2201).
_DATE_GATE_RE = re.compile(
    r"(resolves_by|deferred_until|window closes|resolves_no_earlier_than"
    r"|gated until|ACTIVE until|window open until)"
    r"\D{0,32}(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)


def _keyed_future_date(defer_reason: str, now=None):
    """Return (key, datetime) when the defer NAMES a FUTURE resolution date.

    None when no keyed date is present, when it does not parse, or when the
    named window has already closed (all three keep existing behavior).
    """
    if not defer_reason:
        return None
    m = _DATE_GATE_RE.search(defer_reason)
    if not m:
        return None
    try:
        when = datetime.fromisoformat(m.group(2))
    except Exception:
        return None
    if when <= (now or datetime.now()):
        return None
    return (m.group(1).lower(), when)


def classify(defer_reason: str, participants: list | None,
             defer_set_at=None, stale_days: float = STALE_STRUCTURED_DAYS) -> dict:
    """Return {"category": "a"|"b"|"c"|"unknown", "evidence": [...]}."""
    if not defer_reason:
        return {"category": "unknown", "evidence": ["empty defer_reason"]}

    text = defer_reason.strip()
    lo = text.lower()

    # Cat-A first: structured prefix defers are the most explicit.
    #
    # WELL-FORMED IS NOT VALID (.claude/rules/reclaim-routed-work.md rule 2).
    # This branch used to be an UNCONDITIONAL early return, which made a
    # formatting convention into a laundering mechanism: a defer that merely
    # STARTED with a structured prefix was stamped "genuine" and no other
    # signal — narrative patterns, ambiguity patterns, age — was ever
    # consulted. Measured 2026-07-28 on the live queue: 29 of 40 defers
    # (72.5%) took this return, including two frozen 6 and 8 days past a
    # standing grant that had explicitly retired their reason. The prefix
    # attests that the AUTHOR declared a precondition; it says nothing about
    # whether that precondition is still a legitimate reason to stay stopped.
    #
    # The fast path is preserved for FRESH structured defers (the common,
    # correct case). Past `stale_days` the prefix downgrades to a cat-B
    # re-check candidate instead of a verdict — age is the TRIGGER to look
    # again, never itself the finding (rule 3), so this surfaces for review
    # rather than concluding anything.
    pfx = _has_prefix(text, GENUINE_PREFIXES)
    if pfx:
        age = _defer_age_days(defer_set_at)
        if age is None:
            return {"category": "a",
                    "evidence": [f"structured-prefix:{pfx}", "age:unknown"]}
        if age <= stale_days:
            return {"category": "a",
                    "evidence": [f"structured-prefix:{pfx}", f"age:{age:.1f}d"]}
        # Past the age threshold, but the defer may NAME its own future
        # resolution date -- in which case age is the wrong question and
        # re-derivation is not yet owed. See _keyed_future_date for the three
        # constraints on this suppression. Stays visible as cat-A with explicit
        # date-gated evidence rather than becoming a new category: the
        # by_category schema ("a","b","c","unknown") is consumed downstream, and
        # the lane-B phase selects on `stale-structured` evidence, so dropping
        # that marker is exactly and only what "suppress" has to mean here.
        gate = _keyed_future_date(text)
        if gate is not None:
            key, when = gate
            left = (when - datetime.now()).total_seconds() / 86400.0
            return {
                "category": "a",
                "evidence": [
                    f"structured-prefix:{pfx}",
                    f"age:{age:.1f}d",
                    # ASCII-only: evidence is DATA that reaches shell args
                    # downstream (guard-607, guard-606).
                    #
                    # MUST NOT contain the literal token the consumer selects
                    # on. Lane B (aspirations-precheck Phase 0.5b.13) picks rows
                    # by substring-testing evidence for `stale-structured`, so
                    # an explanatory phrase naming the marker it is suppressing
                    # re-selects the very row it just exempted -- the
                    # suppression defeated by its own prose. Caught by
                    # test_suppressed_rows_never_carry_the_selector_token, which
                    # exists because the first draft of this string said
                    # "stale-structured suppressed" and shipped a no-op.
                    f"date-gated:{key}:{when.date()} - defer names its own "
                    f"resolution date, {left:.1f}d still remaining; age "
                    f"downgrade not applied (age is not the trigger when the "
                    f"window is declared and still open)",
                ],
            }
        return {
            "category": "b",
            "evidence": [
                f"structured-prefix:{pfx}",
                f"age:{age:.1f}d",
                # ASCII-only: this string is DATA, not a comment -- it lands in
                # the JSON payload and can reach shell args downstream, where a
                # multi-byte sequence fails argv parsing (guard-607, guard-606).
                f"stale-structured: prefix is well-formed but {age:.1f}d old "
                f"(> {stale_days}d) - re-derive BOTH axes: is the premise still "
                f"true, AND is the reason still a valid reason?",
            ],
        }

    # Cat-A pattern matches
    a_hits = _has_any(lo, GENUINE_PATTERNS)
    # Cat-C narrative pattern matches
    c_hits = _has_any(lo, DEFER_NARRATIVE_PATTERNS)
    # Cat-B ambiguous matches
    b_hits = _has_any(lo, AMBIGUOUS_PATTERNS)

    # Decision priorities: C beats A when narrative phrasing dominates,
    # because category-c is the actionable finding and we want it surfaced.
    # An A-pattern match alongside a C-pattern match is suspicious — a
    # legitimate dep should not also be using narrative excuse language.
    if c_hits and not a_hits:
        return {"category": "c", "evidence": [f"narrative:{p}" for p in c_hits]}
    if c_hits and a_hits:
        # Both A and C signal: surface as C with conflict note (reviewer should look)
        return {
            "category": "c",
            "evidence": [f"narrative:{p}" for p in c_hits]
                        + [f"genuine:{p}" for p in a_hits]
                        + ["conflict: defer carries both narrative and structural signals"],
        }
    if a_hits:
        return {"category": "a", "evidence": [f"genuine:{p}" for p in a_hits]}
    if b_hits:
        return {"category": "b", "evidence": [f"ambiguous:{p}" for p in b_hits]}

    # Unmatched — review manually
    return {"category": "b", "evidence": ["unmatched: review-by-hand"]}


# ---- Loader ---------------------------------------------------------------

def _enumerate_agents() -> list:
    """Find all <agent>/aspirations.jsonl pairs in PROJECT_ROOT."""
    out = []
    for child in sorted(_agents_root().iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith(".") or child.name in ("core", "meta", "world"):
            continue
        path = child / "aspirations.jsonl"
        if path.is_file():
            out.append((child.name, path))
    return out


# A defer on a goal that has already reached a terminal status is not routed-away
# work — there is nothing left to reclaim, so re-deriving its routing can never
# close anything. Reporting them anyway is not merely noisy: it is the failure mode
# lane B exists to prevent. Measured 2026-07-29 on the live queue, this lane
# reported 3 stale-structured defers of which 2 were `retired` — 67% permanent,
# un-actionable residue that reappears identically every sweep. A reader who checks
# the lane, finds nothing they can act on, and stops checking has been trained to
# ignore it by the sweep's own output, which is exactly how the one REAL item
# (foxtrot's g-005-17) went repeatedly surfaced and never routed.
# `retired` is included deliberately: it is not in the documented goal-status enum
# (it is an aspiration status) but live goal records carry it, and both phantoms
# here had it.
#
# `decomposed` + `superseded` added g-115-3805. This set had diverged from the
# framework's canonical terminal set -- {completed, skipped, expired, decomposed,
# superseded}, written identically in FIVE places (aspirations.py:43,
# insight-trigger-gate.py:87, insight-trigger-sweep.py:143, precheck-eval.py:63,
# unblock-parent-status-sweep.py:125). It is now that consensus set PLUS
# `retired`, which those five omit and which this lane provably needs.
#
# HONEST SIZING, because the number will mislead whoever measures next
# (guard-2529 -- a filter must report what it excluded): adding these two
# excludes ZERO rows today. There are no `decomposed` or `superseded` goals
# carrying a defer_reason anywhere in the live corpus. That is not a reason to
# skip them, and the guard-2616 probe is what shows why: of the FOUR incumbents,
# `completed`, `skipped` and `expired` are ALSO absent from the corpus (only
# `retired` has live members, n=2). Speculative-but-correct is the normal and
# intended state of this set -- it is a defensive predicate over statuses that
# are rare on deferred goals by construction, not a description of today's queue.
#
# guard-2616 (extending a declared set is not additive until you measure the
# MATCHER can read the new members) is satisfied by construction here, and was
# probed rather than assumed: the matcher is a plain
# `(status or "").strip().lower() in TERMINAL_STATUSES` exact compare, not a path
# template or key layout, so a lowercase member cannot be silently unreadable.
# The same probe re-run against the incumbents found no shape defeat either.
TERMINAL_STATUSES = frozenset({
    "completed", "skipped", "expired", "decomposed", "superseded", "retired",
})


def load_deferred() -> list:
    """Return list of dicts {src, agent, asp_id, goal_id, defer_reason,
    defer_set_at, participants, title, status}.

    Terminal-status goals are excluded — see TERMINAL_STATUSES."""
    out = []
    sources = []
    world = WORLD_DIR if WORLD_DIR.is_dir() else None
    if world is not None:
        sources.append(("world", None, world / "aspirations.jsonl"))
    for agent_name, path in _enumerate_agents():
        sources.append(("agent", agent_name, path))

    for src, agent_name, path in sources:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                a = json.loads(line)
            except json.JSONDecodeError:
                continue
            for g in a.get("goals", []):
                dr = g.get("defer_reason")
                if not dr:
                    continue
                if (g.get("status") or "").strip().lower() in TERMINAL_STATUSES:
                    continue
                out.append({
                    "src": src,
                    "agent": agent_name,
                    "asp_id": a.get("id"),
                    "goal_id": g.get("id"),
                    "title": (g.get("title") or "")[:120],
                    "defer_reason": dr,
                    "defer_set_at": g.get("defer_reason_set_at"),
                    "participants": g.get("participants"),
                    "status": g.get("status"),
                })
    return out


# ---- Output ---------------------------------------------------------------

def render_markdown(records: list) -> str:
    by_cat = {"a": [], "b": [], "c": [], "unknown": []}
    for r in records:
        by_cat.setdefault(r["category"], []).append(r)
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"# Deferred Defers Audit — {today}", ""]
    lines.append(f"**Total deferred:** {len(records)}")
    lines.append(f"**Cat A (genuine):** {len(by_cat['a'])} | "
                 f"**Cat B (ambiguous):** {len(by_cat['b'])} | "
                 f"**Cat C (narrative-only):** {len(by_cat['c'])} | "
                 f"**Unknown:** {len(by_cat['unknown'])}")
    lines.append("")
    for cat, label in [("c", "Category C — Narrative-Only (action recommended)"),
                       ("b", "Category B — Ambiguous (review)"),
                       ("a", "Category A — Genuine (legitimate block)"),
                       ("unknown", "Category Unknown — manual review")]:
        rs = by_cat.get(cat, [])
        if not rs:
            continue
        lines.append(f"## {label}")
        lines.append("")
        lines.append("| Goal | Asp | Src | Participants | Defer Reason | Evidence |")
        lines.append("|------|-----|-----|--------------|--------------|----------|")
        for r in rs:
            parts = ",".join(r.get("participants") or [])
            reason = (r["defer_reason"] or "").replace("|", "\\|").replace("\n", " ")[:120]
            evid = ", ".join(r.get("evidence") or []).replace("|", "\\|")[:80]
            src_label = f"{r['src']}/{r['agent']}" if r['agent'] else r['src']
            lines.append(f"| {r['goal_id']} | {r['asp_id']} | {src_label} | {parts} | {reason} | {evid} |")
        lines.append("")
    return "\n".join(lines)


# ---- Main -----------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--output", choices=("json", "human"), default="json",
                   help="json (default) or human (summary table)")
    p.add_argument("--report", help="Write markdown report to this path")
    p.add_argument("--stale-days", type=float, default=STALE_STRUCTURED_DAYS,
                   help=f"Age past which a structured-prefix defer stops being "
                        f"self-certifying and becomes a cat-B re-check candidate "
                        f"(default {STALE_STRUCTURED_DAYS}). See "
                        f".claude/rules/reclaim-routed-work.md rule 2.")
    args = p.parse_args(argv)

    deferred = load_deferred()
    enriched = []
    for r in deferred:
        c = classify(r["defer_reason"], r.get("participants"),
                     defer_set_at=r.get("defer_set_at"),
                     stale_days=args.stale_days)
        enriched.append({**r, "category": c["category"], "evidence": c["evidence"]})

    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(render_markdown(enriched), encoding="utf-8")

    if args.output == "json":
        print(json.dumps({
            "scanned_at": datetime.now().isoformat(timespec="seconds"),
            "total": len(enriched),
            "by_category": {
                cat: sum(1 for r in enriched if r["category"] == cat)
                for cat in ("a", "b", "c", "unknown")
            },
            "records": enriched,
        }, indent=2))
    else:
        print(f"Total deferred: {len(enriched)}")
        for cat in ("a", "b", "c", "unknown"):
            n = sum(1 for r in enriched if r["category"] == cat)
            print(f"  Category {cat}: {n}")
        print()
        for r in enriched:
            print(f"  [{r['category']}] {r['goal_id']:14} | {r['asp_id']} | {r['evidence']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
