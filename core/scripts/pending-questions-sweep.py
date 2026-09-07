#!/usr/bin/env python3
# domain-leak-exempt: INFRA_PATTERN regex literally enumerates infra terms
# (PID, port, VRAM, GPU, SSH host-key, .exe, Lambda) to detect infrastructure-
# specific prose in pending questions. The blocklist tokens are the data, not
# accidental leakage.
"""Pending-questions sweep — fast self-resolution scan.

Replaces consolidation-housekeeping.md Step 2.8's per-question LLM probe loop
with a single Python pass over pure string/date heuristics.

Subcommands (individually testable):
  sweep             — full scan; emits JSON with per-entry verdicts and flags
  stats             — status / type histogram only

Output contract:
  JSON to stdout with at least `{"subcommand","summary","flags":[],...}`.
  Exit 0 = clean (no flags raised). Exit 1 = flags raised (LLM should review
  the flagged entries). Exit 2 = input error (missing file, bad YAML).
  READ-ONLY BY DEFAULT. With NO apply flag the script never writes
  pending-questions.yaml; the LLM consumes the verdicts and applies them in
  consolidation-housekeeping Step 2.8. TWO flags opt into writing, and nothing
  else in this script mutates the file:
    --apply          → discharges verdict=auto_resolve
    --apply-cleanup  → discharges verdict=needs_transition
  Both go through `_apply_auto_resolve`, which sets status / resolved_at /
  resolution ONLY (never `answer`) and is atomic via tempfile + os.replace.
  This paragraph read "No side effects — script never writes
  pending-questions.yaml" until 2026-08-10, which had been false since --apply
  shipped — and `core/config/conventions/coordination.md` cites THIS docstring
  as the source of truth for a concurrency-safety argument about observer vs
  runner writes, so the stale claim was load-bearing somewhere else.
  Fail-open at every layer: a malformed entry yields verdict=no_action with a
  reason, never a crash.

Verdicts assigned per entry (priority order — see HEURISTIC_CHAIN below):
  already_terminal  — INERT. Terminal status + non-empty resolution. NOTHING TO DO.
                      Reported, never flagged, never applied. This class persists by
                      DESIGN and re-classifies on every sweep forever.
  needs_transition  — ACTIONABLE. status=answered with a non-empty answer; the only
                      thing missing is the status flip to `resolved`. Discharged by
                      `--apply-cleanup`; reaches zero when the work is done.
  auto_resolve      — high-confidence: no-op default_action + age > 14d
  likely_resolved   — agent_self_answered + 7d grace
  likely_stale      — infra question + 14d, OR superseded ritual
  flag_for_review   — pending > 30d (catch-all to bound growth)
  no_action         — entry is fresh / pending / no signal yet

Carrier axis (g-115-3714) — a SECOND, ORTHOGONAL field on each OPEN entry,
`carrier` + `carrier_reason`, tallied separately in `carrier_counts`. The
verdicts above all answer "should this question be CLOSED?"; this answers "will
any goal ever ACT on it?", the forward direction neither pre-existing check
covers (_h_source_goal_completed maps a question BACK to its origin goal; the
verify-learning check is goal-to-question). It is deliberately NOT a member of
HEURISTIC_CHAIN, which is first-match-wins and would let the two axes suppress
each other. Closed questions carry no `carrier` field at all — absence means not
applicable, which is why carrier_counts are taken over `open_total` rather than
`counts["total"]`.
  carried             — a live carrier goal that also lists `user` in participants
  carrier_no_user_leg — live carrier, no user leg: present in the queue and still
                        absent from the user digest, which keys on participants
  carrier_terminal    — every referencing goal is terminal (guard-2526: this must
                        not be folded into `uncarried`, or "never filed" and
                        "already finished" become indistinguishable)
  uncarried           — no goal in either queue references the question id
  unknown             — the goal index did not load; NOT evidence of absence
READ-ONLY: this axis never writes. Nothing auto-files a carrier, because ~11 of
the 24 questions in the originating audit correctly need none, so auto-filing
would manufacture queue noise at roughly a 2:1 wrong-to-right ratio.

Usage:
  pending-questions-sweep.sh sweep [--pq-path PATH]
  pending-questions-sweep.sh stats [--pq-path PATH]
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from _paths import AGENT_DIR, WORLD_DIR  # type: ignore
from _fileops import log_script_decision  # type: ignore

try:
    import yaml  # type: ignore
except ImportError:
    print(json.dumps({"error": "PyYAML not installed", "exit": 2}))
    sys.exit(2)


# ---------------------------------------------------------------------------
# Heuristic constants — change here, not inline
# ---------------------------------------------------------------------------

# g-115-3746: sourced from the shared vocabulary module, NOT re-inlined here.
#
# This is the SWEEP's set: "needs no further transition". It deliberately EXCLUDES
# `answered`/`agent_answered`, which are the transition backlog `_h_answered_not_
# cleaned` classifies as needs_transition and `--apply-cleanup` discharges. Adding
# them here would make that heuristic unreachable (heuristic 1 fires first) and
# make the writer skip those entries, deleting the executor g-115-3753/g-115-5025
# built. Measured: doing so turns three tests in test_pending_questions_sweep.py
# red. If you came here from a goal saying "answered should be terminal", read the
# module docstring first -- that reading is five weeks stale.
#
# Widened by this goal from {"resolved", "superseded"} to also cover `retired`,
# `closed` and `done` -- states that are settled but were in NEITHER script's set,
# so a retired question stayed eligible for staleness flagging forever (g-115-4276).
from _pending_question_status import SWEEP_SETTLED as TERMINAL_STATUSES  # noqa: F401
# `is_closed` is CLOSED_STATUSES, which is TERMINAL_STATUSES *plus* the
# TRANSITION_PENDING pair {answered, agent_answered}. The carrier check below
# needs "is anyone still owed an answer?", which is the CLOSER's notion, NOT the
# sweep's — an `answered` question is finished for the asker and only owes a
# canonicalisation pass, so counting it as an orphan would manufacture work. The
# SSOT names this exact confusion as the bug that made a blocked signal citing an
# answered question undischargeable; do not substitute TERMINAL_STATUSES here.
from _pending_question_status import is_closed  # noqa: F401

# Goal statuses meaning "this goal will never act again." Deliberately NOT the
# pending-question vocabulary (guard-1127: a constant serving two subsystems is
# decoupled at the consumer, never widened into one shared value) — a goal is
# never "answered" and a question is never "completed".
GOAL_TERMINAL_STATUSES = frozenset({
    "completed", "skipped", "expired", "archived", "superseded", "decomposed",
})

# Matches a pending-question id wherever it appears in a serialized goal record.
# Ids in live data range from `pq-034` through `pq-027-01` to
# `pq-g-326-591-upstream-filing`, so the tail is permissive.
PQ_ID_RE = re.compile(r"\bpq-[A-Za-z0-9][A-Za-z0-9_-]*")

# Fields where a question id expresses a STRUCTURAL link rather than prose that
# merely mentions it. A match here is strong carrier evidence; a match only in
# description/progress_note/outcome_note is weak, because audit and measurement
# narratives routinely enumerate question ids they will never act on. The sweep
# reports both and does not silently drop either — narrowing the match to these
# fields alone would have scored the live "Apply: close-pending-questions" chore
# as a non-carrier, turning a real carrier into a false orphan, which is the
# more expensive direction of the two.
LINK_FIELDS = frozenset({
    "origin_signal", "blocker_ref", "blocked_by", "defer_reason",
    "source_goal", "title", "pending_question", "pending_questions",
})

INFRA_PATTERN = re.compile(
    r"PID \d+|port \d+|VRAM|GPU|SSH.*host.?key|\.exe|Lambda",
    re.IGNORECASE,
)
DEFAULT_ACTION_NOOP = re.compile(
    r"no change|keep current|left .* unchanged|not killing|do nothing",
    re.IGNORECASE,
)
STALENESS_DAYS = 30
INFRA_STALENESS_DAYS = 14
AGENT_ANSWER_GRACE_DAYS = 7
NOOP_AUTO_RESOLVE_DAYS = 14

RITUAL_TYPES = {"fresh-eyes-review", "fresh-eyes-program"}

# Decision-log markers (g-115-1369): self.md Decision-Authority pending-questions
# are filed with the decision ALREADY executed (default_action prefixed
# "Already executed:") and framed for retroactive user review. They MUST outlive
# their source goal so the user can override. _is_decision_log + the
# _h_source_goal_completed exemption keep them out of source-goal-completion
# auto-resolve. Observed types in real data: "infrastructure-decision",
# "architecture-decision" (both end "-decision").
DECISION_LOG_MARKER = "already executed:"
DECISION_LOG_TYPES = {"decision-log", "decision_log", "decision"}


# ---------------------------------------------------------------------------
# YAML loading — handles both `questions: [...]` wrapper AND bare list shape
# (alpha's pending-questions.yaml mixes both; precedent: fresh-eyes-cadence-
# check.py::_scan_pending_questions_file)
# ---------------------------------------------------------------------------

def _load_questions(path):
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        # Fail-open: surface to stderr, return empty list
        print(f"[pending-questions-sweep] YAML parse error: {e}", file=sys.stderr)
        return []
    if data is None:
        return []
    # Container-shape tolerance:
    #   shape A: top-level dict with "questions" key
    #   shape B: top-level list of {questions: [...]} dicts (alpha's mixed shape)
    #   shape C: top-level list of bare entry dicts
    entries = []
    if isinstance(data, dict):
        entries = data.get("questions", []) or []
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                if "questions" in item and isinstance(item["questions"], list):
                    entries.extend(item["questions"])
                elif "id" in item:
                    entries.append(item)
    return [e for e in entries if isinstance(e, dict)]


def _load_completed_goal_ids():
    """Load completed/superseded goal IDs from world+agent aspiration queues.

    Reads each line as an aspiration record and harvests goals whose
    `status == "completed"`, OR `status == "skipped"` with a "superseded"
    indicator in the outcome note. Fail-open at every layer: missing files
    or parse errors yield an empty set rather than aborting the sweep.

    Used by `_h_source_goal_completed` to detect pending-questions whose
    source goal has already wrapped up — closes the sentinel-lifecycle gap
    that left pq-g-115-305-roblox-publish lingering 12d after both source
    goals completed (g-115-485 finding).
    """
    return _load_goal_index()["completed_goal_ids"]


def _load_goal_index():
    """One pass over both aspiration queues; two indexes out.

    Split out of `_load_completed_goal_ids` (which now delegates here) because
    the carrier check needs a SECOND projection of the same records, and the
    world queue is multi-megabyte — walking it twice to build two dicts is pure
    waste. Returns:

      completed_goal_ids — unchanged semantics, for `_h_source_goal_completed`.
      carriers           — {pq_id: [{id, status, has_user_leg, via,
                           link_evidence}, ...]}, every goal in EITHER queue
                           that mentions that question id in any field; `via`
                           names the field(s), `link_evidence` is whether any
                           of them is in LINK_FIELDS.
      goals_scanned      — the unfiltered population, so a caller can tell
                           "nothing references this question" from "the index
                           never loaded" (guard-2298: a zero is reported beside
                           the population it was drawn from, never alone).

    TERMINAL GOALS ARE INDEXED, NOT FILTERED OUT, and that is load-bearing:
    guard-2526 measured that a query filtered to non-terminal status cannot
    answer "has work been queued for X?" — it excludes the already-done
    population by construction, so an empty result is ambiguous between "never
    filed" and "finished". Keeping terminal carriers lets `_carrier_verdict`
    report that third state instead of collapsing it into `uncarried`.

    Fail-open at every layer, as before: a missing file or a bad line yields a
    smaller index rather than aborting the sweep.
    """
    ids = set()
    carriers = {}
    goals_scanned = 0
    candidates = []
    if WORLD_DIR:
        candidates.append(Path(WORLD_DIR) / "aspirations.jsonl")
    if AGENT_DIR:
        candidates.append(Path(AGENT_DIR) / "aspirations.jsonl")
    for path in candidates:
        if not path.exists():
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    for goal in rec.get("goals", []) or []:
                        if not isinstance(goal, dict):
                            continue
                        gid = goal.get("id")
                        status = goal.get("status")
                        if not gid:
                            continue
                        goals_scanned += 1
                        if status == "completed":
                            ids.add(gid)
                        elif status == "skipped":
                            note = " ".join(
                                str(goal.get(f) or "") for f in
                                ("outcome_note", "completion_note", "skip_reason")
                            ).lower()
                            if "supersed" in note:
                                ids.add(gid)
                        # Carrier projection. Scan FIELD BY FIELD rather than one
                        # serialized blob, and keep only the pq ids plus the
                        # field names that mentioned them — never the text
                        # itself, which would hold the whole queue in memory.
                        #
                        # The field name is the JUDGMENT SIGNAL, and recording it
                        # is why this is not a blob scan. A literal id match
                        # cannot tell "this goal will act on the question" from
                        # "this goal's prose happens to cite the id", and both
                        # occur in live data: g-115-9049 ("Apply: close-pending-
                        # questions") enumerates 8 ids in its description and IS
                        # a carrier, while g-115-3714 cites 2 of the same ids as
                        # measurement evidence and is NOT. No predicate separates
                        # them, so the sweep reports WHERE it matched and a
                        # reader decides — which is the goal's own stated design
                        # ("surface for judgment; do not automate the fix").
                        # LINK_FIELDS below is the strong half of that evidence.
                        by_field = {}
                        for key, value in goal.items():
                            if isinstance(value, str):
                                text = value
                            elif isinstance(value, (list, dict)):
                                try:
                                    text = json.dumps(value, default=str)
                                except (TypeError, ValueError):
                                    continue
                            else:
                                continue
                            if "pq-" not in text:
                                continue
                            for pq_id in PQ_ID_RE.findall(text):
                                by_field.setdefault(pq_id, set()).add(key)
                        if not by_field:
                            continue
                        participants = goal.get("participants") or []
                        has_user_leg = (
                            isinstance(participants, list)
                            and "user" in participants
                        )
                        for pq_id, fields in by_field.items():
                            carriers.setdefault(pq_id, []).append({
                                "id": gid,
                                "status": status,
                                "has_user_leg": has_user_leg,
                                "via": sorted(fields),
                                "link_evidence": bool(fields & LINK_FIELDS),
                            })
        except OSError as e:
            print(
                f"[pending-questions-sweep] could not read {path}: {e}",
                file=sys.stderr,
            )
            continue
    return {
        "completed_goal_ids": ids,
        "carriers": carriers,
        "goals_scanned": goals_scanned,
    }


def _parse_date(s):
    if not s:
        return None
    if isinstance(s, datetime):
        return s
    s = str(s).strip()
    # Strip surrounding quotes that some entries have ('"2026-04-21"')
    s = s.strip('"').strip("'")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _age_days(entry, now):
    for field in ("created", "created_at", "date", "asked_at", "logged_at"):
        d = _parse_date(entry.get(field))
        if d is not None:
            return (now - d).total_seconds() / 86400.0
    return None  # unknown age


def _is_decision_log(entry):
    """True when the entry is a self.md Decision-Authority decision-log (g-115-1369).

    Decision-logs are FILED at goal completion with the decision already
    executed (default_action prefixed "Already executed:") so the user can
    review and override retroactively (.claude/rules/self.md "Decision
    Authority"). They are MEANT to outlive their source goal — auto-resolving
    them the same iteration the source goal completes silently defeats the
    oversight mechanism.

    Either signal suffices:
      - default_action begins with the "Already executed:" marker (primary;
        matches the pq-ollama-numparallel-2026-06-08 incident exactly)
      - an explicit decision-log type ("*-decision", or one of DECISION_LOG_TYPES)
    """
    da = str(entry.get("default_action") or "").strip().lower()
    if da.startswith(DECISION_LOG_MARKER):
        return True
    qtype = str(entry.get("type") or "").strip().lower()
    if qtype.endswith("-decision") or qtype in DECISION_LOG_TYPES:
        return True
    return False


# ---------------------------------------------------------------------------
# Heuristics — return (verdict, reason, confidence) or None to fall through
# ---------------------------------------------------------------------------

def _h_already_terminal(entry, now, ctx):
    """Heuristic 1: status already terminal AND has resolution → INERT.

    Emits `already_terminal`, NOT the old shared `cleanup_only` (g-115-3753 /
    g-115-5025). This class is NOTHING TO DO and persists by design: an entry
    that is terminal-with-resolution re-classifies here on every sweep forever,
    so its count is a steady state and can never reach zero. Sharing one verdict
    with the actionable heuristic below made the combined count unreadable —
    neither "there is work" nor "there is none" could be told from it, which is
    the silent-failure-discriminator class (a metric whose clean state and dirty
    state look identical). Measured on cc-07 2026-08-10: 38 `cleanup_only` was
    24 actionable + 14 inert, and the flag fired on all 38.
    """
    status = entry.get("status")
    if status in TERMINAL_STATUSES and entry.get("resolution"):
        return ("already_terminal", f"already {status} with non-empty resolution", 1.0)
    return None


def _h_answered_not_cleaned(entry, now, ctx):
    """Heuristic 2: status=answered AND has answer → ACTIONABLE.

    Emits `needs_transition`. This IS a backlog: the user's answer is already
    recorded and only the status flip to `resolved` is outstanding, so the count
    falls to zero once the work is done. Discharged by `--apply-cleanup`, which
    is deliberately NOT folded into `--apply` — see the flag's help text.
    """
    if entry.get("status") == "answered" and entry.get("answer"):
        return ("needs_transition", "answered with non-empty answer; transition to resolved", 0.95)
    return None


def _h_agent_self_answered(entry, now, ctx):
    """Heuristic 3: agent_answered + grace period → likely_resolved."""
    if entry.get("status") != "agent_answered":
        return None
    if not entry.get("agent_self_answer") and not entry.get("answer"):
        return None
    age = _age_days(entry, now)
    if age is None or age < AGENT_ANSWER_GRACE_DAYS:
        return None
    return (
        "likely_resolved",
        f"agent_answered {age:.0f}d ago; no user objection within {AGENT_ANSWER_GRACE_DAYS}d grace",
        0.8,
    )


def _h_source_goal_completed(entry, now, ctx):
    """Heuristic 3.5: source_goal completed/superseded → auto_resolve.

    The pending question's premise has dissolved when its source goal
    has wrapped up. Closes the sentinel-lifecycle gap that left
    pq-g-115-305-roblox-publish lingering 12d after both source goals
    completed (g-115-485 / g-001-226 finding). Inserted after
    `_h_agent_self_answered` per the g-115-486 spec so that an agent's
    own self-answer still wins when both signals are present.
    """
    sg = entry.get("source_goal")
    if not sg:
        return None
    completed_ids = ctx.get("completed_goal_ids") or set()
    if sg not in completed_ids:
        return None
    if entry.get("status") in TERMINAL_STATUSES:
        return None
    # g-115-1369: EXEMPT decision-log pending-questions. A decision-log is the
    # self.md Decision-Authority retroactive-review mechanism — filed AT goal
    # completion, MEANT to outlive the source goal so the user can review the
    # executed decision and override. Auto-resolving it the same iteration the
    # source goal completes means it never reaches the user pending-review
    # queue, silently defeating the oversight. Contrast a BLOCKING pending-
    # question ("should I do X for goal G?"), which correctly becomes moot when
    # G completes — this heuristic conflated the two shapes. Decision-logs fall
    # through to _h_noop_auto_resolve (14d) / _h_pending_old (30d
    # flag_for_review) / explicit user resolution instead.
    if _is_decision_log(entry):
        return None
    return (
        "auto_resolve",
        f"source_goal {sg} completed/superseded; pending-question premise dissolved",
        0.95,
    )


def _h_stale_infra(entry, now, ctx):
    """Heuristic 4: infra-state question + age > 14d → likely_stale."""
    text = " ".join(filter(None, [
        entry.get("question", ""),
        entry.get("default_action", ""),
        entry.get("context", ""),
    ]))
    if not INFRA_PATTERN.search(text):
        return None
    age = _age_days(entry, now)
    if age is None or age < INFRA_STALENESS_DAYS:
        return None
    if entry.get("status") in TERMINAL_STATUSES:
        return None
    return (
        "likely_stale",
        f"infra-state question {age:.0f}d old (PID/port/process state likely changed)",
        0.7,
    )


def _h_ritual_superseded(entry, now, ctx):
    """Heuristic 6: ritual entry whose newer sibling already resolved → likely_stale."""
    qtype = entry.get("type")
    if qtype not in RITUAL_TYPES:
        return None
    if entry.get("status") in TERMINAL_STATUSES:
        return None
    # Look across all entries for a newer same-type resolved sibling
    own_age = _age_days(entry, now)
    if own_age is None:
        return None
    for other in ctx.get("all_entries", []):
        if other is entry:
            continue
        if other.get("type") != qtype:
            continue
        if other.get("status") not in TERMINAL_STATUSES:
            continue
        other_age = _age_days(other, now)
        if other_age is None:
            continue
        if other_age < own_age:
            return (
                "likely_stale",
                f"superseded by newer resolved {qtype} sibling ({other.get('id')})",
                0.85,
            )
    return None


def _h_pending_old(entry, now, ctx):
    """Heuristic 7: pending > 30 days → flag_for_review (catch-all)."""
    if entry.get("status") != "pending":
        return None
    age = _age_days(entry, now)
    if age is None or age < STALENESS_DAYS:
        return None
    return (
        "flag_for_review",
        f"pending for {age:.0f}d (>{STALENESS_DAYS}d threshold); user review desired",
        0.4,
    )


def _h_noop_auto_resolve(entry, now, ctx):
    """Heuristic 8: pending + default_action is no-op + age > 14d → auto_resolve.

    The agent already declared it would do nothing. Time has validated that
    posture (no user override arrived). Safe to mark resolved without LLM
    judgment. This is the ONE heuristic that auto-resolves.
    """
    if entry.get("status") != "pending":
        return None
    da = entry.get("default_action") or ""
    if not DEFAULT_ACTION_NOOP.search(da):
        return None
    age = _age_days(entry, now)
    if age is None or age < NOOP_AUTO_RESOLVE_DAYS:
        return None
    return (
        "auto_resolve",
        f"no-op default_action active for {age:.0f}d without user override",
        0.9,
    )


HEURISTIC_CHAIN = [
    _h_already_terminal,
    _h_answered_not_cleaned,
    _h_agent_self_answered,
    _h_source_goal_completed,  # g-115-486: catches sentinel-lifecycle gap
    _h_stale_infra,
    _h_ritual_superseded,
    _h_noop_auto_resolve,  # before _h_pending_old so noop entries auto-resolve
    _h_pending_old,
]


def _describe_carriers(rows, limit=3):
    """Render carrier rows as `g-115-9049 (via description)`, strongest first.

    The `via` clause is the whole reason the index stores field names: it is what
    lets a reader tell a structural link from a passing prose mention without
    opening the goal. Rows carrying LINK_FIELDS evidence sort first so the
    strongest evidence survives the truncation.
    """
    ordered = sorted(
        rows,
        key=lambda r: (not r.get("link_evidence"), str(r.get("id"))),
    )
    parts = []
    for r in ordered[:limit]:
        via = ", ".join(r.get("via") or []) or "unknown field"
        parts.append(f"{r.get('id')} (via {via})")
    if len(ordered) > limit:
        parts.append(f"+{len(ordered) - limit} more")
    return "; ".join(parts)


def _carrier_verdict(entry, ctx):
    """Forward-direction check: does an OPEN question have a goal that will act?

    Deliberately NOT a member of HEURISTIC_CHAIN (design record: g-115-3714).
    That chain is priority-ordered and first-match-wins (`_evaluate` below), and
    its verdicts all answer "should this question be CLOSED?". Carrier-presence
    answers "will anyone ACT on it?" — an orthogonal axis, so putting it in the
    chain would make the two suppress each other (a stale question would hide
    its own orphanhood, or vice versa). It rides alongside the verdict as its
    own field, which is also what g-115-3714 asked for: "emit uncarried as a
    distinct flag".

    Returns (carrier_verdict, reason) or None when not applicable.

      uncarried           — no goal in either queue mentions this question id.
                            The 2026-07-28 orphan shape: real work, no goal,
                            structurally invisible to the goal selector.
      carrier_terminal    — every referencing goal is terminal. The work ran;
                            the question was never closed. Reported separately
                            because guard-2526 measured that folding this into
                            "uncarried" makes "never filed" and "already
                            finished" indistinguishable.
      carrier_no_user_leg — a live carrier exists but none carries a user leg.
                            Measured 2026-08-24 (foxtrot): user-blocker-
                            escalation-check keys on `user` in participants, not
                            on the carrier link, so such a question is absent
                            from the user digest — present in the queue and
                            still unreachable. Two of four rows had this shape,
                            and a carrier-presence-only check scores both clean.
      carried             — a live carrier WITH a user leg.

    The conjunction is the point (reclaim-routed-work.md rule 7 / guard-1802): a
    predicate narrower than the population's creator reports clean forever.
    """
    if is_closed(entry.get("status")):
        return None
    pq_id = entry.get("id")
    if not pq_id:
        return None
    # Fail-safe: with no goal index there is no evidence of absence, and
    # emitting `uncarried` for every open question would be a fleet-wide false
    # positive built out of a failed read.
    if not ctx.get("goals_scanned"):
        return ("unknown", "goal index empty or unavailable — carrier state not determined")
    refs = (ctx.get("carriers") or {}).get(pq_id) or []
    if not refs:
        return ("uncarried", "no goal in either queue references this question id")
    live = [r for r in refs if r.get("status") not in GOAL_TERMINAL_STATUSES]
    if not live:
        return (
            "carrier_terminal",
            f"all {len(refs)} referencing goal(s) are terminal "
            f"[{_describe_carriers(refs)}]; work finished but the question is "
            f"still open",
        )
    with_user_leg = [r for r in live if r.get("has_user_leg")]
    if not with_user_leg:
        return (
            "carrier_no_user_leg",
            f"live carrier(s) {_describe_carriers(live)} exist but none lists "
            f"`user` in participants, so the question is absent from the user digest",
        )
    return (
        "carried",
        f"live carrier(s) {_describe_carriers(with_user_leg)} with a user leg",
    )


def _evaluate(entry, now, ctx):
    """Run heuristics in priority order. First match wins."""
    for h in HEURISTIC_CHAIN:
        try:
            result = h(entry, now, ctx)
        except Exception as e:  # noqa: BLE001 — fail-open per heuristic
            print(
                f"[pending-questions-sweep] heuristic {h.__name__} failed on "
                f"{entry.get('id')}: {e}",
                file=sys.stderr,
            )
            continue
        if result is not None:
            verdict, reason, confidence = result
            return {
                "id": entry.get("id"),
                "status": entry.get("status"),
                "verdict": verdict,
                "reason": reason,
                "confidence": confidence,
            }
    return {
        "id": entry.get("id"),
        "status": entry.get("status"),
        "verdict": "no_action",
        "reason": "no heuristic matched",
        "confidence": 0.0,
    }


# ---------------------------------------------------------------------------
# Sub-commands
# ---------------------------------------------------------------------------

def _walk_entries(data):
    """Yield (parent_list, index, entry) tuples for every entry across the
    three container shapes _load_questions tolerates. Lets `_apply_auto_resolve`
    mutate entries in place without re-implementing shape detection."""
    if isinstance(data, dict):
        questions = data.get("questions")
        if isinstance(questions, list):
            for i, e in enumerate(questions):
                if isinstance(e, dict):
                    yield questions, i, e
    elif isinstance(data, list):
        for top_idx, item in enumerate(data):
            if isinstance(item, dict):
                if "questions" in item and isinstance(item["questions"], list):
                    for i, e in enumerate(item["questions"]):
                        if isinstance(e, dict):
                            yield item["questions"], i, e
                elif "id" in item:
                    yield data, top_idx, item


def _apply_auto_resolve(path, ids_to_resolve, results_by_id):
    """Mutate pending-questions.yaml in place: mark `verdict=auto_resolve`
    entries as `status=resolved` with timestamp + resolution note. Atomic
    via tempfile + os.replace so a crash mid-write cannot leave a partial
    file. Returns the count of entries actually mutated.

    Pattern mirrors `defer-recheck.sh --apply`, `blocker-recheck.sh --apply`,
    `monitor-stale-check.sh --apply` — single-writer, idempotent, fail-quiet."""
    if not path.exists() or not ids_to_resolve:
        return 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        print(
            f"[pending-questions-sweep] --apply read failed: {e}",
            file=sys.stderr,
        )
        return 0
    if raw is None:
        return 0
    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    mutated = 0
    for _parent, _idx, entry in _walk_entries(raw):
        eid = entry.get("id")
        if eid not in ids_to_resolve:
            continue
        if entry.get("status") in TERMINAL_STATUSES:
            continue
        verdict_info = results_by_id.get(eid, {})
        reason = verdict_info.get("reason", "auto_resolve heuristic match")
        entry["status"] = "resolved"
        entry["resolved_at"] = now_iso
        entry["resolution"] = f"auto-resolved by sweep: {reason}"
        mutated += 1
    if mutated == 0:
        return 0
    import os
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                raw, f,
                sort_keys=False, allow_unicode=True, default_flow_style=False,
            )
        os.replace(str(tmp), str(path))
    except OSError as e:
        print(
            f"[pending-questions-sweep] --apply write failed: {e}",
            file=sys.stderr,
        )
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return 0
    return mutated


def _resolve_pq_path(args):
    if args.pq_path:
        return Path(args.pq_path)
    if AGENT_DIR is None:
        print(json.dumps({"error": "MIND_AGENT not resolved", "exit": 2}))
        sys.exit(2)
    return Path(AGENT_DIR) / "session" / "pending-questions.yaml"


def cmd_sweep(args):
    path = _resolve_pq_path(args)
    entries = _load_questions(path)
    now = datetime.now()
    goal_index = _load_goal_index()
    ctx = {
        "all_entries": entries,
        "completed_goal_ids": goal_index["completed_goal_ids"],
        "carriers": goal_index["carriers"],
        "goals_scanned": goal_index["goals_scanned"],
    }

    results = [_evaluate(e, now, ctx) for e in entries]

    # Carrier axis, attached alongside each verdict rather than folded into it.
    # Closed questions get no carrier field at all — absence means "not
    # applicable", which is why the counts below are taken over the open set.
    for entry, result in zip(entries, results):
        try:
            carrier = _carrier_verdict(entry, ctx)
        except Exception as e:  # noqa: BLE001 — fail-open, matching _evaluate
            print(
                f"[pending-questions-sweep] carrier check failed on "
                f"{entry.get('id')}: {e}",
                file=sys.stderr,
            )
            continue
        if carrier is not None:
            result["carrier"] = carrier[0]
            result["carrier_reason"] = carrier[1]

    counts = {
        "total": len(results),
        "auto_resolve": 0,
        "already_terminal": 0,
        "needs_transition": 0,
        "likely_resolved": 0,
        "likely_stale": 0,
        "flag_for_review": 0,
        "no_action": 0,
    }
    for r in results:
        v = r["verdict"]
        if v in counts:
            counts[v] += 1

    # BACK-COMPAT, deliberately derived rather than emitted by a heuristic:
    # `cleanup_only` no longer exists as a verdict, but it stays in `counts` as
    # the sum of the two halves it used to conflate, so an older consumer reading
    # counts["cleanup_only"] keeps seeing the same number instead of a KeyError.
    # It is NOT what any flag or applier keys on any more — that is the whole
    # point of the split (g-115-3753 / g-115-5025).
    counts["cleanup_only"] = counts["already_terminal"] + counts["needs_transition"]

    # Carrier counts are a SEPARATE block, not merged into `counts`: they are
    # taken over the OPEN subset while every count above is over all entries,
    # and summing across two different denominators is how a tally starts lying.
    # `open_total` and `goals_scanned` ride along as the unfiltered populations
    # so a reader can never take `uncarried: 0` as clean without seeing whether
    # anything was examined at all (guard-2298 / guard-2448).
    carrier_counts = {
        "open_total": 0,
        "carried": 0,
        "carrier_no_user_leg": 0,
        "carrier_terminal": 0,
        "uncarried": 0,
        "unknown": 0,
        "goals_scanned": ctx["goals_scanned"],
    }
    for r in results:
        c = r.get("carrier")
        if c is None:
            continue
        carrier_counts["open_total"] += 1
        if c in carrier_counts:
            carrier_counts[c] += 1

    flags = []
    if counts["auto_resolve"]:
        flags.append("auto_resolvable")
    # Fires on the ACTIONABLE half ONLY. Keyed on the combined count it fired
    # forever: `already_terminal` never reaches zero by design, so the flag was
    # on permanently and carried no information. Measured on cc-07 2026-08-10 —
    # 14 of the 38 were inert, so the flag would still have been on after every
    # piece of real work was finished.
    if counts["needs_transition"]:
        flags.append("cleanup_available")
    if counts["likely_resolved"] or counts["likely_stale"]:
        flags.append("candidates_for_resolution")
    if counts["flag_for_review"]:
        flags.append("stale_entries_need_review")
    # Surfaced for JUDGMENT, never auto-filed. About 11 of the 24 questions in
    # the originating audit correctly need no carrier (pure decisions awaiting a
    # human), so auto-filing carriers would manufacture queue noise at roughly a
    # 2:1 wrong-to-right ratio. The sweep reports; a reader decides.
    if carrier_counts["uncarried"]:
        flags.append("uncarried_questions")
    if carrier_counts["carrier_no_user_leg"]:
        flags.append("carrier_missing_user_leg")
    if carrier_counts["carrier_terminal"]:
        flags.append("carrier_terminal_only")
    if carrier_counts["unknown"]:
        flags.append("carrier_index_unavailable")

    summary = (
        f"sweep: {counts['auto_resolve']} auto, "
        f"{counts['needs_transition']} needs-transition, "
        f"{counts['already_terminal']} already-terminal (inert), "
        f"{counts['likely_resolved'] + counts['likely_stale']} likely, "
        f"{counts['flag_for_review']} review, {counts['no_action']} no-action "
        f"out of {counts['total']} total"
    )
    summary = (
        f"{summary}; carrier: {carrier_counts['uncarried']} uncarried, "
        f"{carrier_counts['carrier_no_user_leg']} no-user-leg, "
        f"{carrier_counts['carrier_terminal']} terminal-only, "
        f"{carrier_counts['carried']} carried "
        f"of {carrier_counts['open_total']} open "
        f"({carrier_counts['goals_scanned']} goals scanned)"
    )

    applied = 0
    apply_ids = set()
    if getattr(args, "apply", False):
        apply_ids |= {
            r.get("id") for r in results
            if r.get("verdict") == "auto_resolve" and r.get("id")
        }
    if getattr(args, "apply_cleanup", False):
        apply_ids |= {
            r.get("id") for r in results
            if r.get("verdict") == "needs_transition" and r.get("id")
        }
    if apply_ids:
        results_by_id = {r.get("id"): r for r in results if r.get("id")}
        applied = _apply_auto_resolve(path, apply_ids, results_by_id)
        if applied:
            summary = f"{summary}; applied={applied}"

    return {
        "subcommand": "sweep",
        "summary": summary,
        "flags": flags,
        "counts": counts,
        "carrier_counts": carrier_counts,
        "entries": results,
        "applied": applied,
        "thresholds": {
            "staleness_days": STALENESS_DAYS,
            "infra_staleness_days": INFRA_STALENESS_DAYS,
            "agent_answer_grace_days": AGENT_ANSWER_GRACE_DAYS,
            "noop_auto_resolve_days": NOOP_AUTO_RESOLVE_DAYS,
        },
    }


def cmd_stats(args):
    path = _resolve_pq_path(args)
    entries = _load_questions(path)
    by_status = {}
    by_type = {}
    for e in entries:
        s = e.get("status", "unknown")
        by_status[s] = by_status.get(s, 0) + 1
        t = e.get("type", "general")
        by_type[t] = by_type.get(t, 0) + 1
    return {
        "subcommand": "stats",
        "summary": f"{len(entries)} entries; statuses={by_status}",
        "flags": [],
        "counts": {"total": len(entries)},
        "by_status": by_status,
        "by_type": by_type,
    }


DISPATCH = {
    "sweep": cmd_sweep,
    "stats": cmd_stats,
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Pending-questions self-resolution sweep",
        prog="pending-questions-sweep",
    )
    parser.add_argument(
        "subcommand",
        choices=list(DISPATCH.keys()),
        help="sweep | stats",
    )
    parser.add_argument(
        "--pq-path",
        default=None,
        help="Override default <agent>/session/pending-questions.yaml path",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "sweep only: mutate pending-questions.yaml in place — mark "
            "verdict=auto_resolve entries as status=resolved with timestamp."
        ),
    )
    parser.add_argument(
        "--apply-cleanup",
        action="store_true",
        help=(
            "sweep only: ALSO discharge verdict=needs_transition entries "
            "(status=answered with a non-empty answer) by flipping them to "
            "status=resolved. Deliberately SEPARATE from --apply rather than "
            "folded into it: --apply runs unattended from precheck, and these "
            "entries carry a real user answer, so bulk-closing them without an "
            "explicit opt-in is a large unattended state change. The entry's "
            "`answer` field is never touched — the writer sets status, "
            "resolved_at and resolution only. verdict=already_terminal is "
            "NEVER applied by either flag: it is inert by construction."
        ),
    )
    args = parser.parse_args(argv)

    try:
        result = DISPATCH[args.subcommand](args)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 — fail-open to keep consolidation moving
        print(
            f"[pending-questions-sweep] FATAL fail-open: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        result = {
            "subcommand": args.subcommand,
            "summary": f"failed-open: {type(e).__name__}",
            "flags": [],
            "counts": {"total": 0},
            "error": str(e),
        }

    try:
        log_script_decision("pending-questions-sweep", {
            "subcommand": result.get("subcommand"),
            "flags": result.get("flags", []),
            "total": result.get("counts", {}).get("total", 0),
        })
    except Exception:  # noqa: BLE001 — logging failure must never block the sweep
        pass

    print(json.dumps(result, ensure_ascii=False, default=str))
    sys.exit(1 if result.get("flags") else 0)


if __name__ == "__main__":
    main()
