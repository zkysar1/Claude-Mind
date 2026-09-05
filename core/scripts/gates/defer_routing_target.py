"""Defer routing-target validation — is the OWNER a defer NAMES actually the owner?
(g-115-8837)

Sibling of `gates.defer_target_existence`, which asks the same question about a
different target kind. That module validates a cited DEPENDENCY GOAL ID; this one
validates the three targets a defer uses to say WHO the work belongs to:

    agent name   -> checked against the live lane-pin registry
    grant id     -> checked against the Standing User Grants registry
    owner phrase -> advisory only; prose cannot be adjudicated

`capability-gate.py` already runs at defer-write time and keyword-matches the
VERB ("is this action agent-provisionable"). It never looks at the named TARGET.
So the best-written, most-specific defers — the ones naming a concrete agent or
citing a grant id — pass unexamined, because specificity reads as diligence.
Three measured instances (g-369-112, g-369-08, g-369-03) each froze HIGH work for
days behind a named owner who was not the owner.

THE PERVERSE GRADIENT this closes: a vague defer invites scrutiny; a specific one
reads as diligence and gets less. That inverts once a registry check exists,
because a specific claim is the only kind a machine can falsify.

FIVE DESIGN CONSTRAINTS, each MEASURED on the live corpus rather than reasoned
-----------------------------------------------------------------------------

1. **Trigger on the FIELD, never on `is_narrative_defer`.** Measured 2026-09-04
   across world + every agent queue: of 131 non-terminal goals carrying a
   defer_reason, **131 were structured and 0 were narrative**. The capability
   gate sits behind `_is_narrative_defer` (aspirations.py), so a routing check
   wired at the same place would fire on ZERO of the population while looking
   correct in review — the guard-1802 class (a predicate narrower than the
   population it audits reports clean forever). Independently reproduces
   `defer_target_existence` constraint 2, which measured 79/0 for goal ids.

2. **Role-aware agent extraction, never `text CONTAINS <name>`.** Measured on the
   same corpus: a WIDE predicate (roster name appears anywhere) flags 91 defers;
   the role-aware one (a routing phrase governing the name) flags 8. The other 83
   merely MENTION a sibling as context. A check firing on 83 correct defers gets
   trained away as noise, leaving it technically present and practically dead
   (guard-4883). `"<agent>'s own ..."` is excluded deliberately — it is a
   statement about that agent's own surface, not a routing-away claim.

3. **Grant validation is EXISTENCE ONLY, and that ceiling is measured, not shy.**
   The obvious richer rule — "a grant cited beside blocking language contradicts
   the grant, refuse" — was tested against all 9 live grant-citing defers and
   would have refused **9 of 9, every one of them correct**. Most cite a grant
   precisely to say it does NOT apply (g-335-1438: "grant-007 does not cover
   it"). So semantic support is NOT machine-checkable here and this module does
   not pretend otherwise: it refuses only a grant id absent from the registry —
   revoked (rows are deleted to revoke, so grant-002..005 no longer exist) or
   fabricated. That check fires on 0 of the 9 live citations, which is the
   correct behaviour, not a dead check.

4. **Refuse only the CONFIDENT subset; ambiguity is an advisory.** This reuses
   `gates.lane_pin.evaluate` rather than re-deriving pin semantics, and inherits
   its "out-of-lane evidence AND no in-lane evidence" rule for refusals. Where
   lane_pin returns `ambiguous` this module ADVISES instead of allowing
   silently: the error asymmetry is REVERSED between the two call sites. At
   CLAIM time a false refusal wedges a live agent, so allowing on ambiguity is
   right. At DEFER time a false refusal costs one `--force-defer` justification
   while a false allow freezes the goal for the 120h fail-open TTL — so the
   ambiguous case is worth a word, and guard-2900 says so outright: the gate
   "enforces only the CONFIDENT SUBSET ... so THIS guardrail still carries
   everything the gate declines to classify."

5. **No roster, no agent check — never a wide fallback.** `roster` defaults to
   the fleet SSOT (`_agents.get_active_agents`, the same source `lane_pin._roster`
   and `gates.capability_route` read), so no caller has to supply one and no
   caller can supply a WRONG one. Resolution is fail-open: an unreadable SSOT
   yields the empty set, which skips the lane and reports it unchecked — it never
   degrades to a looser regex, which would silently re-open constraint 2.
   Pass `roster=[]` to force-skip the lane explicitly.

   Do NOT glob `team-state/agents/*.yaml` for this. Measured 2026-09-04: that
   glob returns 15 names on this box, 10 of them test residue
   (`no-such-agent-xyz`, `test-race-0`, `__probe_nonexistent__`, ...), against
   the SSOT's 5. A roster is an allowlist the refusal path keys on, so padding
   it with junk names widens what the gate will refuse on.

KNOWN CEILING, stated rather than hidden. `lane_pin.evaluate` judges a GOAL, but
a defer routinely routes away a DIFFERENT unit of work than the goal it sits on
— g-369-112 is a handle-reservation goal whose defer routed an env-server
PROMOTION. This module therefore evaluates the pin against a pseudo-goal built
from the DEFER TEXT, which is the described work. That is closer to the right
question and still imperfect: on the historical g-369-112 text the verdict is
`ambiguous` (pin-001's in-lane column contains "promotion per grant-008", so
"promotion" matches in-lane even though this was a service promotion, not the
kind of promotion the pin means), so that instance is an ADVISORY, not a refusal. Widening
lane_pin to resolve it was rejected: its posture protects the hotter claim-time
path.

Daemon safety: no env reads, no module-level path constants, no globals mutated
after first use. Every path is passed in by the caller (endpoints resolve through
`ctx.paths` — `.claude/rules/path-resolution.md`). Every failure path returns a
no-op ALLOW (guard-142), including a failure while COMPOSING a refusal message:
the refusal DECISION is made before any text is built, so a formatting bug
cannot silently downgrade a refusal to a pass (guard-3803).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

__all__ = [
    "evaluate",
    "extract_agent_targets",
    "extract_grant_citations",
    "extract_owner_phrases",
    "REFUSAL_PREFIX",
    "ADVISORY_PREFIX",
]

REFUSAL_PREFIX = "[defer-routing-target] REFUSED"
ADVISORY_PREFIX = "[defer-routing-target] ADVISORY"

# A routing phrase must GOVERN the name — constraint 2. Kept as one alternation
# so the matched phrase can be quoted back to the author verbatim.
_ROUTING_VERBS = (
    r"routed to|route to|routing to|assigned to|assign to|belongs to|owned by|"
    r"owner is|handed off to|handed to|hand off to|hand to|deferred to|defer to|"
    r"awaiting|waiting on|waiting for|blocked on|needs"
)
# "<agent>'s lane|queue|box|remit" is a routing claim; "<agent>'s own ..." is not.
_POSSESSIVE_NOUNS = r"lane|queue|box|remit|leg|call"

_GRANT_RE = re.compile(r"\bgrant-\d{3}\b", re.IGNORECASE)

# Prose ownership claims naming no addressable target. Advisory only — refusing
# on prose is the guard-1470 false-positive shape.
_OWNER_PHRASE_RE = re.compile(
    r"(an?\s+upstream\s+owner|the\s+upstream\s+owner|whoever\s+owns|"
    r"another\s+team|a\s+different\s+team|not\s+self-service|"
    r"assigns?\s+the\s+\w+\s+to\b|the\s+owner\s+of\b)",
    re.IGNORECASE,
)

# A probe citation discharges the owner-phrase advisory: the author looked.
_PROBE_CITATION_RE = re.compile(
    r"(probed|re-probed|measured|verified|positive control|AccessDenied|"
    r"describe-|get-caller-identity|returns? empty|exit code|rc=)",
    re.IGNORECASE,
)


def _empty(reason: str = "") -> Dict[str, Any]:
    return {
        "refuse": False,
        "reason": None,
        "advisories": [],
        "agent_targets": [],
        "grant_citations": [],
        "owner_phrases": [],
        "checked": False,
        "skip_reason": reason or None,
    }


def _clause_around(text: str, start: int, end: int) -> str:
    """The sentence containing [start:end] — the SCOPE of the routing claim.

    Load-bearing, and measured. Evaluating the WHOLE defer text as the routed
    work refused g-350-334, a correct defer: its text names env-server, Java,
    Lua and IntentEngineVerticle as goal CONTEXT while routing only "a DEV
    session" — which pin-001 puts squarely IN foxtrot's lane, as that defer
    itself says while quoting the pin. A defer is mostly context about the goal;
    only a clause describes the work being routed away. Same defect class as the
    wide-vs-role-aware one (constraint 2), one level down.
    """
    body = text or ""
    left = max(body.rfind(". ", 0, start), body.rfind("\n", 0, start),
               body.rfind("; ", 0, start))
    left = 0 if left < 0 else left + 1
    right_candidates = [i for i in (body.find(". ", end), body.find("\n", end),
                                    body.find("; ", end)) if i != -1]
    right = min(right_candidates) if right_candidates else len(body)
    return body[left:right].strip()


def extract_agent_targets(text: str, roster) -> List[Dict[str, str]]:
    """Role-aware routing claims naming a roster agent (constraint 2).

    Returns [{"agent", "phrase", "clause"}], deduped on agent, preserving
    first-match order. A bare mention yields nothing. `clause` is the sentence
    the claim was made in — the scope the pin is evaluated against.
    """
    out: List[Dict[str, str]] = []
    names = [str(a).strip() for a in (roster or []) if str(a or "").strip()]
    if not names:
        return out
    alt = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    routing = re.compile(
        r"(?:%s)\s+(?:the\s+)?(%s)\b" % (_ROUTING_VERBS, alt), re.IGNORECASE)
    # The "own" marker is captured INSIDE the pattern, not tested after the
    # match. Measured 2026-09-04 (mutation test): as a tail-slice check it was
    # unreachable — the noun alternation cannot match across an intervening
    # "own", so "foxtrot's own lane" already failed to match and the guard never
    # ran. Worse, the ONE input shape that did reach it ("foxtrot's lane owner")
    # is a genuine routing claim, which the check then suppressed. Dead in the
    # case it was written for and wrong in the case it actually fired.
    possessive = re.compile(
        r"\b(%s)'s\s+(own\s+)?(?:%s)\b" % (alt, _POSSESSIVE_NOUNS),
        re.IGNORECASE)
    seen = set()
    for rx in (routing, possessive):
        for m in rx.finditer(text or ""):
            agent = (m.group(1) or "").lower()
            # "<agent>'s OWN <noun>" describes that agent's existing surface,
            # not a routing-away. group(2) is the marker; see the pattern above.
            if rx is possessive and m.lastindex and m.lastindex >= 2 and m.group(2):
                continue
            if agent and agent not in seen:
                seen.add(agent)
                out.append({
                    "agent": agent,
                    "phrase": m.group(0).strip(),
                    "clause": _clause_around(text or "", m.start(), m.end()),
                })
    return out


def extract_grant_citations(text: str) -> List[str]:
    """Every grant id cited in the text, lowercased and deduped, in order."""
    out: List[str] = []
    for g in _GRANT_RE.findall(text or ""):
        g = g.lower()
        if g not in out:
            out.append(g)
    return out


def extract_owner_phrases(text: str) -> List[str]:
    """Prose ownership claims naming no addressable target."""
    return [m.group(0).strip() for m in _OWNER_PHRASE_RE.finditer(text or "")]


def _pin_verdict(agent, clause, goal_id, world_dir, registry_text):
    """lane_pin verdict for `agent` against the CLAUSE that routes the work.

    `clause` — not the whole defer — is the described work (see KNOWN CEILING
    and `_clause_around`), and it is the ONLY thing passed. The goal RECORD is
    not a parameter at all: after the category fix below its sole contribution
    was `id`, which duplicates `goal_id`, so accepting it could only let a caller
    pass a MISMATCHED record. The goal's own `category` is deliberately NOT
    forwarded: `lane_pin._goal_headline` matches
    bare TOKENS against title+category, so forwarding it makes the category
    itself out-of-lane evidence on every clause. Measured 2026-09-04 — a
    `category: framework` goal was refused for routing ANY clause to an agent
    whose pin excludes "framework scripts", including a clause reading
    "run a no-player session on dev" that the same pin lists as in-lane. That is
    the whole-goal-context leak `_clause_around` exists to remove, arriving
    through a second door. The live 131-defer sweep did NOT catch it: every
    role-aware target there sat in `npc-domain`, a category no pin column names,
    so the branch was never exercised (guard-5121 — a stored predicate validated
    on a population that does not exercise its own branch).
    Returns None when lane_pin is unavailable — silence, never a fallback.
    """
    try:
        from gates.lane_pin import evaluate as pin_evaluate  # type: ignore
    except Exception:
        return None
    pseudo = {
        "id": str(goal_id or ""),
        "title": str(clause or ""),
        "category": "",  # deliberate — see docstring (category-leak, 2026-09-04)
        "description": str(clause or ""),
    }
    try:
        return pin_evaluate(agent, pseudo, registry_text=registry_text,
                            world_dir=world_dir)
    except Exception:
        return None


def _known_grant_ids(world_dir):
    """Grant ids present in `## Standing User Grants`, or None if unreadable.

    Imports the registry parser that already exists rather than re-typing the
    table regex (guard-4883). None means "could not read" and disables the grant
    lane — it never means "no grants exist", which would refuse every citation.
    """
    try:
        import importlib.util
        from pathlib import Path
        path = Path(__file__).resolve().parent.parent / "audit-user-to-agent.py"
        spec = importlib.util.spec_from_file_location(
            "_a2a_standing_grants", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        parsed = mod._parse_standing_grants(Path(world_dir))
    except Exception:
        return None
    if not isinstance(parsed, dict) or parsed.get("error"):
        return None
    ids = set()
    heads = {}
    for _scope, gids in (parsed.get("by_scope") or {}).items():
        for gid in gids or []:
            ids.add(str(gid).lower())
    for gid, head in (parsed.get("unkeyed") or []):
        ids.add(str(gid).lower())
        heads[str(gid).lower()] = str(head or "")
    if not ids:
        return None
    return ids, heads


def _default_roster():
    """Fleet agent names from the SSOT. Never raises — unreadable means empty,
    which skips the agent lane rather than widening it (constraint 5)."""
    try:
        from _agents import get_active_agents
        return [str(a).lower() for a in (get_active_agents() or ()) if str(a or "").strip()]
    except Exception:
        return []


def evaluate(goal_id: str, text: Any, *,
             world_dir=None, registry_text=None, roster=None) -> Dict[str, Any]:
    """Validate the routing targets a defer_reason names.

    ALWAYS returns the same shape; every failure path is a no-op ALLOW.

        {"refuse": bool, "reason": str|None, "advisories": [str],
         "agent_targets": [...], "grant_citations": [...],
         "owner_phrases": [...], "checked": bool, "skip_reason": str|None}

    Takes NO goal record — see `_pin_verdict`. The unit of work being routed
    away is the defer's own clause, never the goal it sits on (KNOWN CEILING),
    so there is nothing about the goal this gate is entitled to read.

    `refuse` is decided BEFORE any message is composed, so a formatting failure
    cannot downgrade a refusal to a pass (guard-3803).
    """
    body = str(text or "")
    if not body.strip():
        return _empty("empty defer_reason")

    if roster is None:
        roster = _default_roster()

    result = _empty()
    result["checked"] = True
    result["skip_reason"] = None
    refusals: List[str] = []
    advisories: List[str] = []

    # ---- lane 1: agent targets -------------------------------------------
    if roster:
        for target in extract_agent_targets(body, roster):
            agent = target["agent"]
            clause = target.get("clause") or body
            verdict = _pin_verdict(agent, clause, goal_id, world_dir, registry_text)
            if not verdict:
                continue
            row = {
                "agent": agent,
                "phrase": target["phrase"],
                "clause": clause[:240],
                "verdict": verdict.get("verdict"),
                "pin_id": verdict.get("pin_id"),
                "evidence": verdict.get("evidence") or [],
            }
            result["agent_targets"].append(row)
            if verdict.get("would_block"):
                refusals.append(
                    "%s: this defer routes work to '%s' (\"%s\"), but lane pin %s "
                    "excludes that agent from this work class. Out-of-lane "
                    "evidence: %s. A pin retires ONLY by user directive "
                    "(delete its registry row) — re-probing cannot clear it, so "
                    "this goal would freeze behind an agent that may not take "
                    "it." % (goal_id, agent, target["phrase"],
                             verdict.get("pin_id") or "(unnamed)",
                             ", ".join(row["evidence"]) or "(none recorded)")
                )
            elif verdict.get("verdict") == "ambiguous":
                advisories.append(
                    "%s %s: routing to '%s' (\"%s\") matches BOTH in-lane and "
                    "out-of-lane evidence on pin %s (%s), so the pin gate cannot "
                    "adjudicate it and did not refuse. guard-2900: the pin's "
                    "guardrail still carries what the gate declines to classify "
                    "— confirm this agent may take this specific work before "
                    "relying on the routing."
                    % (ADVISORY_PREFIX, goal_id, agent, target["phrase"],
                       verdict.get("pin_id") or "(unnamed)",
                       ", ".join(row["evidence"]) or "no tokens recorded")
                )
    else:
        result["skip_reason"] = "roster unavailable or empty — agent lane skipped"

    # ---- lane 2: grant citations (EXISTENCE ONLY — constraint 3) ----------
    cited = extract_grant_citations(body)
    if cited:
        known = _known_grant_ids(world_dir) if world_dir is not None else None
        if known is None:
            for g in cited:
                result["grant_citations"].append(
                    {"grant": g, "known": None, "head": None})
        else:
            ids, heads = known
            for g in cited:
                is_known = g in ids
                result["grant_citations"].append({
                    "grant": g,
                    "known": is_known,
                    "head": (heads.get(g) or "")[:200] or None,
                })
                if not is_known:
                    refusals.append(
                        "%s: this defer cites %s, which is NOT in the Standing "
                        "User Grants registry. Grants are revoked by DELETING "
                        "their row, so a missing id means revoked or never "
                        "existed — either way it cannot support the claim. "
                        "Live grant ids: %s."
                        % (goal_id, g, ", ".join(sorted(ids)))
                    )

    # ---- lane 3: owner phrases (ADVISORY ONLY — never a refusal) ----------
    phrases = extract_owner_phrases(body)
    if phrases:
        result["owner_phrases"] = phrases
        if not _PROBE_CITATION_RE.search(body):
            advisories.append(
                "%s %s: this defer names an owner in prose (%s) but cites no "
                "probe. An ownership claim that was never tested is the shape "
                "that froze g-369-08 and g-369-03 — both named 'an upstream "
                "owner' for resources the fleet identity could already reach. "
                "Probe the boundary before accepting the routing (rb-10090). "
                "NOT refused — the defer still applies."
                % (ADVISORY_PREFIX, goal_id,
                   "; ".join('"%s"' % p for p in phrases[:3]))
            )

    # Decision first, prose second (guard-3803).
    result["refuse"] = bool(refusals)
    result["advisories"] = advisories
    if refusals:
        try:
            result["reason"] = (
                "%s %s\n%s\nOverride with --force-defer \"<why this routing is "
                "correct despite the registry>\" — the same flag the capability "
                "gate already uses; no second override exists."
                % (REFUSAL_PREFIX, goal_id, "\n".join(refusals))
            )
        except Exception:
            result["reason"] = "%s %s: routing target contradicted by the registry." % (
                REFUSAL_PREFIX, goal_id)
    return result
