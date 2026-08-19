"""Residual-work completion gate logic (Layer B of the  pattern).

Refuses `status=completed` when the goal's outcome_note names work that was
NOT done (deliberate scope narrowing) and no LIVE carrier goal exists for it.
Canonical incident: g-335-1176 (2026-08-12) completed as analyst scope with a
full implementation spec written onto the COMPLETED record — a terminal store
nothing selects — and the implementation was invisible until a fresh-eyes
review filed g-335-1186 a day later.

Layer map (4-layer enforcement pattern, capability-routing-enforcement node):
  A — Step 8.55 in aspirations-state-update/SKILL.md + guard-3601 (honor
      system; vocabulary mirrored here).
  B — THIS gate, at the completion chokepoint (cmd_update_goal / the daemon
      update-goal endpoint), same family as gates.uncommitted_work and
      gates.completion_artifact.
  D — auto-conversion: when this gate blocks, the CALLER files the suggested
      successor atomically under the live aspirations.jsonl lock (mirror of
      the defer-time Layer-D auto-Unblock, probe-before-defer.md), so the
      refusal never strands the agent at the same decision point.

Accept paths (any ONE lifts the block, mirroring Step 8.55):
  1. outcome_note cites a carrier goal id near carrier vocabulary AND that
     goal is LIVE (pending/in-progress) in the world queue or the agent
     queue — validated against queue state, never regex alone.
  2. --override-residual "<justification>" (daemon header
     X-Mind-Override-Residual) — audited to
     `<world_dir>/residual-work-overrides.jsonl`.
  3. outcome_note records an explicit owner decline.

PRECEDENCE IS LOAD-BEARING, AND IT USED TO BE WRONG (g-115-6254). Paths 2 and
3 were swapped, and every path returned EARLY, so an owner-decline INFERRED
FROM PROSE pre-empted an override the caller had EXPLICITLY supplied. Measured
on g-350-233: the note contained "The gate's other exit, an owner decline, is
equally inaccurate — nothing is being declined" — prose ARGUING AGAINST that
exit — which `OWNER_DECLINE_RE` matches. The close was accepted on the exact
exit its own note rejected; `--override-residual` reported success while
`override_applied` stayed None; and the audit row the refusal message promises
was NEVER WRITTEN. That is strictly worse than a block-marker false positive:
a block refuses loudly and leaves a visible carrier, whereas an accept-path
false positive lets the close through AND drops the audit trail, so nothing
anywhere records that a gate was bypassed. An EXPLICIT signal must therefore
always outrank an INFERRED one, and an override that was passed is ALWAYS
recorded and ALWAYS audited whichever path would otherwise have applied.

The marker list is deliberately CONSERVATIVE (goal text, g-115-6099): a
false positive costs one educational refusal with a working escape named in
the message; a false negative costs stranded work nothing re-surfaces.

Public API:
    evaluate(goal_id, outcome_note, override, items, other_items,
             world_dir, agent_name, goal_priority, goal_category) -> dict
    find_existing_successor(items, original_goal_id, other_items) -> dict|None
    build_successor_goal(original_goal_id, gate_result, new_goal_id) -> dict

`items` is the queue being WRITTEN (the --source target); `other_items` is
THE OTHER QUEUE — world when the target is agent, agent when the target is
world. The parameter was called `agent_items` until g-115-6254 and BOTH
callers populated it unconditionally from the AGENT queue, so on a
`--source agent` close the two arguments were the SAME queue and the world
queue was never loaded at all. Every world carrier then read `live: false,
status: null` — indistinguishable from a genuinely dead carrier — and the
gate auto-filed a duplicate successor for work that was already owned
(measured on g-001-80: three world-queue carriers, all `pending`, all
reported dead). The name is the fix as much as the population is: the old
one read like "the other queue" while being nothing of the kind, which is
why two independent callers made the identical mistake.

Output dict shape (evaluate):
    {
      "would_block": bool,
      "matched_markers": [str, ...],          # marker names, first-hit order
      "residual_clause": str | None,          # sentence behind the suggestion
      "carrier_refs_found": [{"goal_id": str, "live": bool, "status": str|None}],
      "owner_decline_found": bool,
      "successor_title": str | None,          # Layer-D suggestion
      "successor_description": str | None,
      "successor_priority": str,
      "successor_category": str,
      "goal_id": str,
      "override_applied": str | None,
      "skipped_reason": str | None,           # set when the scan never ran
    }

Daemon safety:
    - Reads no environment variables; every input is passed in.
    - No subprocess calls; pure regex + in-memory queue scans. The only
      side effect is the fail-open override-ledger append (same contract as
      gates.uncommitted_work / gates.completion_artifact).
"""
from __future__ import annotations

import datetime as dt
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from _fileops import locked_append_jsonl  # type: ignore

# Statuses under which a cited carrier (or a dedup-found successor) counts as
# LIVE. Same set the defer-time Layer-D dedup uses for Unblocks.
ACTIVE_STATUSES = ("pending", "in-progress")

# Conservative residual markers — Step 8.55 vocabulary, tightened to phrases
# that rarely appear in a fully-executed goal's outcome. Bare "follow-up" /
# "remainder" / "successor" mostly occur while NAMING the filed carrier, in
# which case accept path 1 lifts the block; the negative lookbehinds strip the
# common benign negations ("no follow-up needed").
RESIDUAL_MARKERS: List[Tuple[str, re.Pattern]] = [
    ("no_code_written",
     re.compile(r"\bno\s+(?:product\s+|new\s+)?code\s+(?:was\s+)?written\b",
                re.IGNORECASE)),
    ("spec_only",
     re.compile(r"\b(?:spec(?:ification)?|criteria|spec\s*/\s*criteria)\s+only\b",
                re.IGNORECASE)),
    ("drafted_not_sent",
     re.compile(r"\bdrafted,?\s+(?:but\s+)?not\s+sent\b", re.IGNORECASE)),
    ("out_of_scope_this_pass",
     re.compile(r"\bout\s+of\s+scope\s+(?:for\s+)?this\s+pass\b",
                re.IGNORECASE)),
    ("deferred_to",
     re.compile(r"\bdeferred\s+to\b", re.IGNORECASE)),
    ("follow_up",
     re.compile(r"(?<!\bno\s)(?<!without\s)\bfollow-up\b", re.IGNORECASE)),
    ("remainder",
     re.compile(r"\bremainder\b", re.IGNORECASE)),
    ("successor",
     re.compile(r"\bsuccessor\b", re.IGNORECASE)),
]

GOAL_ID_RE = re.compile(r"\bg-\d{1,4}-\d{1,4}\b")

# Vocabulary that marks a nearby goal id as a CARRIER citation rather than an
# incidental cross-reference. Window below is ±120 chars — wider than the
# dedup proximity (80) because citations often carry a parenthetical between
# the verb and the id.
CARRIER_VOCAB_RE = re.compile(
    r"\b(?:carried|carrier|carries|filed|files|successor|follow-up|"
    r"remainder|deferred|tracked|routed|owns)\b",
    re.IGNORECASE,
)
CARRIER_WINDOW = 120

OWNER_DECLINE_RE = re.compile(
    r"(?:\bowner\b.{0,40}?\bdeclin\w+|\bdeclined\s+by\s+(?:the\s+)?"
    r"(?:owner|user)\b|\buser\s+declined\b)",
    re.IGNORECASE | re.DOTALL,
)

LEDGER_BASENAME = "residual-work-overrides.jsonl"


def _split_sentences(text: str) -> List[str]:
    """Cheap sentence split — good enough to lift a residual clause for the
    successor title. Newlines and .!? all terminate."""
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [p.strip() for p in parts if p.strip()]


def _residual_clause(text: str, first_match_start: int) -> str:
    """The sentence containing the first matched marker."""
    pos = 0
    for sent in _split_sentences(text):
        idx = text.find(sent, pos)
        if idx < 0:
            idx = pos
        if idx <= first_match_start < idx + len(sent):
            return sent
        pos = idx + len(sent)
    # Fallback: a window around the match.
    lo = max(0, first_match_start - 60)
    return text[lo:first_match_start + 90].strip()


def _extract_carrier_candidates(text: str) -> List[str]:
    """Goal ids within CARRIER_WINDOW chars of carrier vocabulary."""
    vocab_spans = [m.start() for m in CARRIER_VOCAB_RE.finditer(text)]
    out: List[str] = []
    for m in GOAL_ID_RE.finditer(text):
        if any(abs(m.start() - v) <= CARRIER_WINDOW for v in vocab_spans):
            if m.group(0) not in out:
                out.append(m.group(0))
    return out


def _lookup_goal_status(gid: str,
                        items: Optional[List[Dict[str, Any]]],
                        other_items: Optional[List[Dict[str, Any]]]
                        ) -> Optional[str]:
    """Status of goal `gid` across the target queue and THE OTHER queue, or
    None when not found. First hit wins (ids are globally unique by
    convention). Both arguments must be genuinely different queues — see the
    module docstring; passing the same queue twice silently halves the search
    space and reports live carriers as dead."""
    for source in (items, other_items):
        if not source:
            continue
        for asp in source:
            for g in asp.get("goals", []) or []:
                if g.get("id") == gid:
                    return g.get("status")
    return None


def _append_override_ledger(world_dir: Optional[Path], record: Dict[str, Any]
                            ) -> None:
    """Fail-open audit append — same contract as the sibling gates."""
    if world_dir is None:
        return
    try:
        locked_append_jsonl(str(Path(world_dir) / LEDGER_BASENAME), record)
    except Exception as exc:  # noqa: BLE001 — audit must never block the gate
        print(f"[residual-work-gate] override ledger append failed: {exc}",
              file=sys.stderr)


def evaluate(goal_id: str,
             outcome_note: str,
             override: Optional[str],
             items: Optional[List[Dict[str, Any]]],
             other_items: Optional[List[Dict[str, Any]]],
             world_dir: Optional[Path],
             agent_name: str = "",
             goal_priority: Optional[str] = None,
             goal_category: Optional[str] = None) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "would_block": False,
        "matched_markers": [],
        "residual_clause": None,
        "carrier_refs_found": [],
        "owner_decline_found": False,
        "successor_title": None,
        "successor_description": None,
        "successor_priority": (goal_priority if goal_priority in
                               ("HIGH", "MEDIUM", "LOW") else "MEDIUM"),
        "successor_category": goal_category or "framework-maintenance",
        "goal_id": goal_id,
        "override_applied": None,
        "skipped_reason": None,
    }

    text = (outcome_note or "").strip()
    if not text:
        # Layer A owns narrative discipline; an empty note has nothing to
        # scan and blocking on absence would refuse every legacy close.
        result["skipped_reason"] = "empty_outcome_note"
        return result

    first_start: Optional[int] = None
    for name, rx in RESIDUAL_MARKERS:
        m = rx.search(text)
        if m:
            result["matched_markers"].append(name)
            if first_start is None or m.start() < first_start:
                first_start = m.start()
    if not result["matched_markers"]:
        return result

    # Accept signals are computed WITHOUT early return (), then
    # ranked by how much we trust them. Early-returning is what let a signal
    # INFERRED from prose pre-empt one the caller stated EXPLICITLY — see the
    # PRECEDENCE note in the module docstring for the measured incident.
    live_found = False
    for gid in _extract_carrier_candidates(text):
        if gid == goal_id:
            continue  # self-citation is not a carrier
        status = _lookup_goal_status(gid, items, other_items)
        live = status in ACTIVE_STATUSES
        result["carrier_refs_found"].append(
            {"goal_id": gid, "live": live, "status": status})
        if live:
            live_found = True
    result["owner_decline_found"] = bool(OWNER_DECLINE_RE.search(text))

    # Accept path 2 — the audited override. Ranked ABOVE the owner-decline
    # inference and evaluated even when a live carrier was found, because an
    # override that was PASSED must always be recorded and always audited:
    # the silent-bypass failure was `override_applied` reading None with an
    # empty ledger while the caller reported success.
    if override:
        clause = _residual_clause(text, first_start or 0)
        result["residual_clause"] = clause
        result["override_applied"] = override
        _append_override_ledger(world_dir, {
            "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
            "goal_id": goal_id,
            "agent": agent_name or None,
            "matched_markers": result["matched_markers"],
            "residual_clause": clause[:300],
            "justification": override,
        })
        return result

    # Accept path 1: live carrier citation (factual, verified against queue
    # state — needs no audit row because nothing was bypassed).
    if live_found:
        return result

    # Accept path 3: owner decline, INFERRED from prose. Last because it is
    # the only accept path that cannot be verified against anything.
    if result["owner_decline_found"]:
        return result

    clause = _residual_clause(text, first_start or 0)
    result["residual_clause"] = clause

    # Block + Layer-D suggestion. Title from the residual clause (the
    # defer-gate precedent: the title tells the executor what to DO).
    short = clause if len(clause) <= 90 else clause[:87].rstrip() + "..."
    result["would_block"] = True
    result["successor_title"] = f"Residual: {short} (from {goal_id})"
    result["successor_description"] = (
        f"Residual-work carrier auto-filed by the residual-work gate "
        f"(g-115-6099 Layer B/D): completing {goal_id} named undone work "
        f"with no live carrier. Residual clause: \"{clause[:400]}\". Read "
        f"{goal_id}'s outcome_note for full context, and copy any "
        f"verification criteria or spec it carries INTO this goal's "
        f"verification field before executing — criteria on a completed "
        f"record are invisible to every selector (g-335-1186 reference "
        f"shape)."
    )
    return result


def find_existing_successor(items: Optional[List[Dict[str, Any]]],
                            original_goal_id: str,
                            other_items: Optional[List[Dict[str, Any]]] = None
                            ) -> Optional[Dict[str, Any]]:
    """Cross-queue dedup for Layer-D filing — 3 OR-ed strategies mirroring
    the defer-gate's `_find_existing_unblock_for` (rb-574):
      (a) origin_signal == 'residual:{original_goal_id}'
      (b) title starts 'Residual:' AND contains the original goal id
      (c) description holds 'residual' within 80 chars of the original id
    Only ACTIVE_STATUSES count — a resolved/skipped successor never blocks
    re-filing."""
    expected_origin = f"residual:{original_goal_id}"
    gid_lo = original_goal_id.lower()

    def _scan(source, label):
        if not source:
            return None
        for asp in source:
            for g in asp.get("goals", []) or []:
                if g.get("status") not in ACTIVE_STATUSES:
                    continue
                if g.get("origin_signal") == expected_origin:
                    return {**g, "_aspiration_id": asp.get("id", ""),
                            "_source": label,
                            "_match_strategy": "origin_signal"}
                title = (g.get("title", "") or "")
                if (title.startswith("Residual:")
                        and gid_lo in title.lower()):
                    return {**g, "_aspiration_id": asp.get("id", ""),
                            "_source": label,
                            "_match_strategy": "title_prefix"}
                desc = (g.get("description", "") or "").lower()
                r_idx = desc.find("residual")
                g_idx = desc.find(gid_lo)
                if r_idx >= 0 and g_idx >= 0 and abs(r_idx - g_idx) <= 80:
                    return {**g, "_aspiration_id": asp.get("id", ""),
                            "_source": label,
                            "_match_strategy": "description_proximity"}
        return None

    # Labels are "target"/"other", NOT "world"/"agent": which concrete queue
    # each argument holds depends on --source, and calling the second one
    # "agent" unconditionally is precisely the misreading that produced
    # . The caller knows its own --source and can name it.
    return _scan(items, "target") or _scan(other_items, "other")


def build_successor_goal(original_goal_id: str,
                         gate_result: Dict[str, Any],
                         new_goal_id: str) -> Dict[str, Any]:
    """The successor record for Layer-D filing. The CALLER allocates
    `new_goal_id` under its chosen aspiration, appends, and persists under
    the lock it already holds — this function never writes (rb-403
    single-writer)."""
    return {
        "id": new_goal_id,
        "title": (gate_result.get("successor_title")
                  or f"Residual: carrier for {original_goal_id}"),
        "description": (gate_result.get("successor_description")
                        or f"Residual work from {original_goal_id} "
                           f"(residual-work gate)."),
        "type": "idea",
        "category": gate_result.get("successor_category",
                                    "framework-maintenance"),
        "priority": gate_result.get("successor_priority", "MEDIUM"),
        "participants": ["agent"],
        "status": "pending",
        "blocked_by": [],
        "origin_signal": f"residual:{original_goal_id}",
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        # alloc_nonce (): this lane appends directly and never
        # passes through add_goal()'s setdefault — mint our own.
        "alloc_nonce": uuid.uuid4().hex,
        "tags": ["residual", "residual-gate-routed"],
        "verification": {"outcomes": [], "checks": [], "preconditions": []},
    }
