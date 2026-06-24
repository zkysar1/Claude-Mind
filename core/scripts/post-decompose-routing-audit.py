"""post-decompose-routing-audit.py — Self.md domain-match audit after intended_agent stamp.

g-115-1060 sub-fix 3 of 3 (LIFECYCLE gap). Complements:
  - sub-fix 1 (g-115-1083): extended world/config/capability-routing.yaml with echo routes
  - sub-fix 2 (g-115-1084): let Tier 3 high-delta heuristics override Tier 1 when
    Tier 1 confidence < 0.85 (SEMANTIC gap closure inside gates.capability_route)

After the capability-route gate stamps intended_agent on a new goal, this
audit reads each active agent's Self.md "What I Do" / "Boundaries" sections,
tokenizes them, and computes which agent's self-description best matches the
goal's title+description. If the best-match agent != stamped agent AND the
score gap is significant (>= min_gap, default 5 tokens), the audit decision
is "file" — the caller (aspirations_write.py post-Phase-D) is expected to
file an Investigate goal in asp-115 so bravo / the better-matched agent can
review and decide whether to re-route.

Design constraints
  - Pure module. Never raises on bad inputs — every failure path returns a
    decision of "no_file" with a descriptive reason. Fail-open is the contract.
  - No env reads. Filesystem reads limited to per-agent Self.md files.
  - Single source of truth for active agents: _agents.get_active_agents().
  - Caller-level idempotency: callers check origin_signal="routing-mismatch:<id>"
    for an existing Investigate before filing. This module emits the spec; the
    caller does the file/dedup.

Public API
    audit(goal, *, project_root, agents_root=None, min_gap=3,
          min_gap_either=5, exclude_agents=None) -> dict

Returns a dict shaped per the schema in the module docstring's RETURNS block;
see _make_decision below.

Two routing-mismatch shapes filed (g-115-1122):
  - "routing-mismatch:<id>" — stamped_agent is a SPECIFIC agent but another
    agent's Self.md domains match better by at least min_gap tokens.
  - "routing-either-resolve:<id>" — stamped_agent is the "either" sentinel
    AND one agent's Self.md domains stand out from second-best by at least
    min_gap_either tokens, suggesting a re-stamp from either → best_agent.

Both shapes use the recursion guard at the origin_signal check; an Investigate
filed BY this audit (either shape) is never re-audited.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Iterable, Optional


# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------

# Tokens shorter than this are dropped before overlap scoring. 4 chars filters
# "a/i/the/do/and/or" while preserving "test", "code", "self", "goal", etc.
_MIN_TOKEN_LEN = 4

# Common English words that survive the length filter but carry no domain
# signal. Kept inline to avoid a stopwords dependency.
_STOPWORDS = frozenset({
    "this", "that", "with", "from", "have", "into", "when",
    "what", "which", "their", "these", "those", "there", "then",
    "than", "also", "must", "only", "some", "such", "very", "your",
    "they", "them", "will", "would", "could", "should", "been",
    "more", "most", "less", "make", "made", "many", "much", "each",
    "after", "before", "while", "where", "while", "about", "above",
    "below", "between", "across", "against", "during", "through",
    "under", "until", "without", "within", "shall", "still",
})


def _tokenize(text: str) -> set:
    """Lower-cased word-token set, > _MIN_TOKEN_LEN chars, stopwords removed.

    Returns set (not list) — every test downstream wants unique-overlap counts,
    not raw frequency. fail-open: returns empty set for any non-str input.
    """
    if not isinstance(text, str) or not text:
        return set()
    raw = re.findall(r"[a-zA-Z0-9_]+", text.lower())
    return {t for t in raw
            if len(t) >= _MIN_TOKEN_LEN
            and t not in _STOPWORDS}


# ---------------------------------------------------------------------------
# Self.md section extraction
# ---------------------------------------------------------------------------

# Headings that count as "domain-describing" sections. The match is
# case-insensitive on the heading text. "What I Don't Do" / "What I Do Not Do"
# are explicitly excluded — those describe NEGATIVE boundaries and including
# their tokens would invert the signal.
_DOMAIN_HEADING_PATTERN = re.compile(
    r"^##\s+(What I Do(?!n['’]?t|n['’]?t Do| Not)|"
    r"Boundaries(?:\s+With\s+My\s+Teammates)?(?:\s*\(.*\))?|"
    r"Lane Split.*|"
    r"My Teammates|"
    r"Agent-Provisionable Actions.*|"
    r"Operating Principles|"
    r"Primary Workspace)"
    r"\s*$",
    flags=re.MULTILINE | re.IGNORECASE,
)

# Section-end heading: next `## ` heading or EOF.
_NEXT_HEADING_PATTERN = re.compile(r"^##\s+", flags=re.MULTILINE)


def _extract_domain_sections(self_md_text: str) -> str:
    """Extract concatenated text of domain-describing sections in a Self.md.

    Returns the BODY text of every section whose heading matches the domain
    heading set. Excludes "What I Don't Do" by construction. If no domain
    sections match, returns the FULL document (fail-open — better to over-
    match than to silently zero out an agent's signal).

    Pure function. Returns empty string on non-str / empty input.
    """
    if not isinstance(self_md_text, str) or not self_md_text:
        return ""

    matches = []
    for m in _DOMAIN_HEADING_PATTERN.finditer(self_md_text):
        section_start = m.end()
        # Find next `## ` heading or EOF
        next_m = _NEXT_HEADING_PATTERN.search(self_md_text, section_start)
        section_end = next_m.start() if next_m else len(self_md_text)
        matches.append(self_md_text[section_start:section_end])

    if not matches:
        # Fail-open: no recognized sections → use whole doc rather than
        # silently producing empty signal.
        return self_md_text
    return "\n".join(matches)


def _read_self_md(agents_root: Path, agent_name: str) -> str:
    """Read agents/<agent_name>/self.md. Return empty string on any error.

    Errors are swallowed by design — caller treats empty score as "agent has
    no Self.md signal" and falls through to no_file. This is the test-case-5
    fail-open path.
    """
    try:
        path = agents_root / agent_name / "self.md"
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError, UnicodeDecodeError):
        return ""


# ---------------------------------------------------------------------------
# Active-agent enumeration
# ---------------------------------------------------------------------------

def _active_agents(project_root: Path) -> tuple:
    """Resolve active agents via _agents.get_active_agents(). Fail-open on
    import or read errors — returns empty tuple, which leads to no_file.

    Prefers the SSOT helper to keep the audit's agent list consistent with
    capability_route.evaluate(). The helper reads
    world/team-state.yaml agent_status keys.
    """
    try:
        scripts_dir = project_root / "core" / "scripts"
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        from _agents import get_active_agents  # type: ignore
        agents = get_active_agents()
        return tuple(agents) if agents else ()
    except Exception:  # pylint: disable=broad-except
        return ()


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _score_agents(goal_tokens: set, agents: Iterable[str],
                  agents_root: Path) -> dict:
    """Return {agent_name: overlap_score} where overlap_score is the Jaccard
    similarity |goal_tokens ∩ domain_tokens| / |goal_tokens ∪ domain_tokens|
    for that agent's Self.md domain tokens (a float in [0.0, 1.0]).

    Jaccard normalizes for Self.md vocabulary size (g-115-1200). The prior raw
    set-intersection cardinality mechanically favored broad-vocab agents: the
    largest Self.md (bravo, 658 domain tokens vs the 180-539 of specialists)
    intersects more of ANY goal, so it won best_agent in ~61% of all goals
    (695/1141 measured) regardless of true ownership, producing a false-positive
    routing-mismatch cluster that grew with every decompose. The union
    denominator dilutes a broad agent's raw advantage — dropping bravo's
    best_agent share to ~3% (36/1141) while specialists win their true domains.

    Agents whose Self.md is unreadable score 0.0. Empty goal_tokens yields a
    dict of {agent: 0.0} — caller treats that as a no-signal goal (test case 3).
    """
    scores = {}
    for agent in agents:
        if not isinstance(agent, str) or not agent:
            continue
        self_md_text = _read_self_md(agents_root, agent)
        sections_text = _extract_domain_sections(self_md_text)
        agent_tokens = _tokenize(sections_text)
        union = goal_tokens | agent_tokens
        scores[agent] = (len(goal_tokens & agent_tokens) / len(union)
                         if union else 0.0)
    return scores


# ---------------------------------------------------------------------------
# Decision assembly
# ---------------------------------------------------------------------------

def _make_decision(*, decision: str, reason: str,
                   stamped_agent: Optional[str],
                   best_agent: Optional[str], best_score: float,
                   stamped_score: float, gap: float, scores: dict,
                   goal_id: Optional[str],
                   investigate_spec: Optional[dict]) -> dict:
    """Assemble the final decision dict. Always returns a complete schema so
    callers can rely on key presence."""
    return {
        "decision": decision,
        "reason": reason,
        "stamped_agent": stamped_agent,
        "best_agent": best_agent,
        "best_score": best_score,
        "stamped_score": stamped_score,
        "gap": gap,
        "scores": scores,
        "goal_id": goal_id,
        "investigate_spec": investigate_spec,
    }


def _build_investigate_spec(goal_id: str, stamped_agent: str,
                            best_agent: str, gap: float,
                            best_score: float, stamped_score: float) -> dict:
    """Build the Investigate goal payload the caller writes into .

    Schema matches the goal-add path (mind_api aspirations_write.py
    _run_add_goal_pipeline expects title, description, priority,
    participants, origin_signal, category).
    """
    title = (
        f"Investigate: routing-mismatch {goal_id} "
        f"intended_agent={stamped_agent} but {best_agent}'s Self.md domains match"
    )
    description = (
        f"Routing-audit triggered: goal {goal_id} was stamped "
        f"intended_agent={stamped_agent} (Jaccard {stamped_score:.4f}) but the "
        f"highest Self.md domain-token Jaccard overlap was {best_agent} "
        f"({best_score:.4f}, gap +{gap:.4f}). Review whether the routing should be "
        f"corrected manually (aspirations-update-goal.sh {goal_id} "
        f"intended_agent {best_agent}) or whether capability_route's tables "
        f"need extending (sub-fix 1 / sub-fix 2 pattern). "
        f"Source: post-decompose-routing-audit.py (g-115-1085 / rb-1060)."
    )
    return {
        "title": title,
        "description": description,
        "priority": "MEDIUM",
        "participants": ["agent"],
        "origin_signal": f"routing-mismatch:{goal_id}",
        "category": "framework-decomposition",
        "discovered_by": "post-decompose-routing-audit",
        "aspiration_id": "asp-115",
    }


def _build_either_resolve_spec(goal_id: str, best_agent: str, gap: float,
                               best_score: float, second_best_score: float) -> dict:
    """Build the Investigate spec for the either-case stand-out path.

    Distinct from _build_investigate_spec: stamped_agent is the "either"
    sentinel, so there is no specific agent to compare against. The "gap"
    here is best_score - second_best_score — a measure of how much the
    best-matching agent stands out from the next-best.

    Same caller pattern in aspirations_write.py (_file_routing_audit_investigate
    handles any decision="file" with any spec); recursion guard recognizes the
    "routing-either-resolve:" prefix in audit()'s origin_signal check.
    """
    title = (
        f"Investigate: routing-either-resolve {goal_id} "
        f"intended_agent=either but {best_agent}'s Self.md domains strongly match"
    )
    description = (
        f"Routing-audit triggered: goal {goal_id} was stamped "
        f"intended_agent=either (classifier uncertain) but the highest "
        f"Self.md domain-token Jaccard overlap was {best_agent} "
        f"({best_score:.4f}, stand-out gap +{gap:.4f} over second-best "
        f"{second_best_score:.4f}). Suggest re-stamp either → {best_agent}: "
        f"aspirations-update-goal.sh {goal_id} intended_agent {best_agent}. "
        f"Surfaced under the either-case threshold (min_gap_either, default 0.015) "
        f"because either-case is signal-ambiguous by design — strong "
        f"Self.md stand-out is the deciding signal. "
        f"Source: post-decompose-routing-audit.py (g-115-1122)."
    )
    return {
        "title": title,
        "description": description,
        "priority": "MEDIUM",
        "participants": ["agent"],
        "origin_signal": f"routing-either-resolve:{goal_id}",
        "category": "framework-decomposition",
        "discovered_by": "post-decompose-routing-audit",
        "aspiration_id": "asp-115",
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def audit(goal: dict, *, project_root: Path,
          agents_root: Optional[Path] = None,
          min_gap: float = 0.015,
          min_gap_either: float = 0.015,
          min_best_score: float = 0.10,
          exclude_agents: Optional[set] = None) -> dict:
    """Audit a freshly-stamped goal and decide whether to file an Investigate
    goal for routing-mismatch detection.

    Args:
      goal: The freshly-stamped goal dict (must carry id, title, description,
            intended_agent — others optional). abstained_by, handoff_to are
            consulted as "already-routed" signals.
      project_root: Project root (defaults derived from agents_root if both
                    provided; project_root wins). Used to locate _agents.py.
      agents_root: Directory holding per-agent subdirectories. Defaults to
                   project_root / "agents".
      min_gap: Minimum (best_score - stamped_score) Jaccard gap required to
               file for the SPECIFIC-AGENT case. Default 0.015 — the p90 of the
               empirical best-minus-second Jaccard gap distribution measured
               over 1141 live world goals (median 0.0032, p75 0.0078, p90
               0.0152, max 0.0571; g-115-1200, guard-594 empirical calibration).
               Fires only on the top-decile clearest stand-outs — preserving
               the original meaningful-signal-floor intent (a fuzzy vocabulary
               overlap must not second-guess capability_route's curated table
               stamp on a weak gap) on the normalized 0-1 scale. The prior raw
               default of 5 (token-count gap) is meaningless post-normalization
               — a fixed 5 on a 0-1 gap would silence the audit entirely.
      min_gap_either: Minimum (best_score - second_best_score) Jaccard gap
               required to file for the EITHER-CASE — gap measures how much the
               best-matching agent stands out from next-best. Default 0.015
               (same p90 floor as min_gap; either-case is signal-ambiguous by
               design — we want a clear stand-out before suggesting re-stamp).
               g-115-1122 / g-115-1200.
      min_best_score: Absolute floor on the best agent's Jaccard score below
               which NO routing-mismatch is filed, regardless of gap. Default
               0.10 (rb-1488 / g-115-1351). The gap thresholds above catch
               RELATIVE stand-outs, but bag-of-words contamination produces a
               top-decile GAP between two NEAR-ZERO absolute scores — observed
               FP band best ~0.073-0.081 vs stamped ~0.056-0.061 across
               g-1348/1352/1354 (rb-1478, 82% all-time skip rate). When the
               best agent's own absolute Jaccard is below this floor, no agent
               has a real ownership signal, so the "mismatch" is contamination
               noise, not a routing error. 0.10 sits just above the observed FP
               band (guard-594 empirical calibration) — suppressing the
               near-zero cluster without silencing genuine domain stand-outs
               (true-domain wins score well above the floor). Applies to BOTH
               the specific-agent and either-case paths.
      exclude_agents: Optional set of agent names to skip in scoring (e.g.,
                      {"delta"} when auditing a delta-bound goal). Defaults
                      to the empty set.

    Returns:
      decision dict with keys: decision, reason, stamped_agent, best_agent,
      best_score, stamped_score, gap, scores, goal_id, investigate_spec.

    Never raises. All errors collapse to decision="no_file".
    """
    # Defensive resolution: agents_root defaults under project_root.
    if not isinstance(project_root, Path):
        project_root = Path(project_root) if project_root else Path(".")
    if agents_root is None:
        agents_root = project_root / "agents"
    elif not isinstance(agents_root, Path):
        agents_root = Path(agents_root)

    exclude = set(exclude_agents) if exclude_agents else set()

    # Defensive: goal must be a dict with the keys we read. Anything else →
    # no_file, since the caller stamped nothing meaningful.
    if not isinstance(goal, dict):
        return _make_decision(
            decision="no_file", reason="goal is not a dict",
            stamped_agent=None, best_agent=None, best_score=0,
            stamped_score=0, gap=0, scores={}, goal_id=None,
            investigate_spec=None,
        )

    goal_id = goal.get("id") if isinstance(goal.get("id"), str) else None
    stamped_agent = goal.get("intended_agent")
    if not isinstance(stamped_agent, str) or not stamped_agent:
        return _make_decision(
            decision="no_file", reason="no intended_agent stamped",
            stamped_agent=None, best_agent=None, best_score=0,
            stamped_score=0, gap=0, scores={}, goal_id=goal_id,
            investigate_spec=None,
        )
    # 2: the "either" sentinel used to short-circuit here with
    # "classifier was uncertain — auditing for a mismatch produces noise."
    # That removed Self.md affinity from exactly the case where it's the
    # deciding signal. The fix runs the audit under a higher gap threshold
    # (min_gap_either) measured as best - second_best instead of
    # best - stamped (since "either" has no real stamped_score to compare
    # against). A clear stand-out match files an Investigate suggesting
    # re-stamp either → best_agent; ambiguous either-cases still no_file.
    is_either_case = (stamped_agent == "either")

    # Already-routed signal: if the goal carries abstained_by or handoff_to,
    # routing was considered already and re-filing produces noise.
    if isinstance(goal.get("abstained_by"), str) and goal.get("abstained_by"):
        return _make_decision(
            decision="no_file", reason="goal carries abstained_by",
            stamped_agent=stamped_agent, best_agent=None, best_score=0,
            stamped_score=0, gap=0, scores={}, goal_id=goal_id,
            investigate_spec=None,
        )
    if isinstance(goal.get("handoff_to"), str) and goal.get("handoff_to"):
        return _make_decision(
            decision="no_file", reason="goal carries handoff_to",
            stamped_agent=stamped_agent, best_agent=None, best_score=0,
            stamped_score=0, gap=0, scores={}, goal_id=goal_id,
            investigate_spec=None,
        )

    # Recursion guard: an Investigate goal filed BY this audit carries
    # origin_signal "routing-mismatch:<X>" (specific-agent path) or
    # "routing-either-resolve:<X>" (either-case path, 2).
    # Re-auditing the audit's own output produces an infinite filing chain.
    # Bail early on either prefix.
    origin = goal.get("origin_signal")
    if isinstance(origin, str) and (
            origin.startswith("routing-mismatch:") or
            origin.startswith("routing-either-resolve:")):
        return _make_decision(
            decision="no_file",
            reason="goal is itself an audit-filed Investigate (recursion guard)",
            stamped_agent=stamped_agent, best_agent=None, best_score=0,
            stamped_score=0, gap=0, scores={}, goal_id=goal_id,
            investigate_spec=None,
        )

    # 1 / rb-1488: ALSO skip insight-trigger-derived goals. The
    # insight-trigger sweep () files Apply/Investigate goals from
    # findings-channel posts and stamps intended_agent from the finding
    # author's requires_action_by tag — that routing is AUTHOR-CURATED, not
    # classifier-inferred. The routing audit exists to catch CLASSIFIER
    # mis-stamps, so it only produces FPs on author-curated routing. Worse,
    # when such a goal's TEXT is about routing itself (agent names / Jaccard /
    # "routing"), it Jaccard-matches the agent it MENTIONS rather than the one
    # that should OWN it, spawning a routing-mismatch FP that the sweep
    # re-files next cadence — a self-perpetuation chain (g-1346 -> g-1329 ->
    # g-1351/1353 -> g-1348/1352/1354). Bail before scoring.
    if isinstance(origin, str) and origin.startswith("insight_trigger:"):
        return _make_decision(
            decision="no_file",
            reason="goal is insight-trigger-derived; routing is author-curated (recursion guard)",
            stamped_agent=stamped_agent, best_agent=None, best_score=0,
            stamped_score=0, gap=0, scores={}, goal_id=goal_id,
            investigate_spec=None,
        )

    title = goal.get("title") or ""
    description = goal.get("description") or ""
    goal_tokens = _tokenize(title + "\n" + description)
    if not goal_tokens:
        return _make_decision(
            decision="no_file", reason="goal title+description has no signal tokens",
            stamped_agent=stamped_agent, best_agent=None, best_score=0,
            stamped_score=0, gap=0, scores={}, goal_id=goal_id,
            investigate_spec=None,
        )

    agents = _active_agents(project_root)
    if not agents:
        return _make_decision(
            decision="no_file", reason="no active agents resolvable",
            stamped_agent=stamped_agent, best_agent=None, best_score=0,
            stamped_score=0, gap=0, scores={}, goal_id=goal_id,
            investigate_spec=None,
        )
    agents = tuple(a for a in agents if a not in exclude)
    # The "stamped in agents" check applies only to the specific-agent path —
    # the "either" sentinel is intentionally not a real agent name.
    if not is_either_case and stamped_agent not in agents:
        # Stamped agent is outside the active set (stale snapshot?). Skip the
        # audit — comparing against a non-active agent's Self.md isn't meaningful.
        return _make_decision(
            decision="no_file",
            reason=f"stamped_agent {stamped_agent} not in active agent set",
            stamped_agent=stamped_agent, best_agent=None, best_score=0,
            stamped_score=0, gap=0, scores={}, goal_id=goal_id,
            investigate_spec=None,
        )

    scores = _score_agents(goal_tokens, agents, agents_root)
    # For the either-case, stamped_score is 0 by convention — "either" has
    # no Self.md to score against. Gap is measured against second-best below.
    stamped_score = 0 if is_either_case else scores.get(stamped_agent, 0)

    # Pick the agent with the highest score. Ties: lexicographically
    # smallest name (deterministic). The stamped agent counts in the tie
    # set — if the stamped agent ties for first place, no mismatch.
    if not scores:
        return _make_decision(
            decision="no_file", reason="no scores produced",
            stamped_agent=stamped_agent, best_agent=None, best_score=0,
            stamped_score=stamped_score, gap=0, scores=scores,
            goal_id=goal_id, investigate_spec=None,
        )
    max_score = max(scores.values())
    best_candidates = sorted(a for a, s in scores.items() if s == max_score)
    best_agent = best_candidates[0]
    best_score = max_score

    if is_either_case:
        # Gap for the either-case is best - second_best (stand-out measure).
        sorted_vals = sorted(scores.values(), reverse=True)
        second_best_score = sorted_vals[1] if len(sorted_vals) >= 2 else 0
        gap = best_score - second_best_score
        threshold = min_gap_either
        gap_label = "min_gap_either"
    else:
        # Existing specific-agent path: gap is best - stamped.
        second_best_score = 0  # not meaningful for specific-agent path
        gap = best_score - stamped_score
        threshold = min_gap
        gap_label = "min_gap"

        # "stamped is best match" check only applies to the specific-agent
        # path — for either, best_agent is never == "either" since "either"
        # is a sentinel, not a real agent name.
        if best_agent == stamped_agent:
            return _make_decision(
                decision="no_file", reason="stamped agent is best match",
                stamped_agent=stamped_agent, best_agent=best_agent,
                best_score=best_score, stamped_score=stamped_score,
                gap=gap, scores=scores, goal_id=goal_id,
                investigate_spec=None,
            )

    # Absolute min-best-score floor (rb-1488 / 1). The gap check below
    # catches RELATIVE stand-outs, but bag-of-words contamination yields a
    # top-decile GAP between two NEAR-ZERO absolute scores (FP band best
    # ~0.073-0.081, rb-1478). When the best agent's own absolute Jaccard is
    # below the floor, no agent has a real ownership signal — the "mismatch" is
    # contamination noise. Suppress on BOTH paths (specific-agent + either).
    # Placed before the gap check so the returned dict carries the real gap.
    if best_score < min_best_score:
        return _make_decision(
            decision="no_file",
            reason=(f"best_score {best_score} below min_best_score floor "
                    f"{min_best_score} (no real ownership signal)"),
            stamped_agent=stamped_agent, best_agent=best_agent,
            best_score=best_score, stamped_score=stamped_score,
            gap=gap, scores=scores, goal_id=goal_id,
            investigate_spec=None,
        )

    if gap < threshold:
        return _make_decision(
            decision="no_file",
            reason=f"gap {gap} below {gap_label} {threshold}",
            stamped_agent=stamped_agent, best_agent=best_agent,
            best_score=best_score, stamped_score=stamped_score,
            gap=gap, scores=scores, goal_id=goal_id,
            investigate_spec=None,
        )

    # Significant mismatch — assemble the Investigate spec.
    if not goal_id:
        # We have enough signal to detect a mismatch but no goal_id to point
        # the Investigate at. Surface in the decision dict but don't build a
        # spec the caller would file under an unknown source.
        return _make_decision(
            decision="no_file", reason="mismatch detected but goal has no id",
            stamped_agent=stamped_agent, best_agent=best_agent,
            best_score=best_score, stamped_score=stamped_score,
            gap=gap, scores=scores, goal_id=None,
            investigate_spec=None,
        )

    if is_either_case:
        spec = _build_either_resolve_spec(
            goal_id=goal_id, best_agent=best_agent, gap=gap,
            best_score=best_score, second_best_score=second_best_score,
        )
        reason = (f"either-case stand-out: {best_agent} score {best_score} "
                  f"beats second-best {second_best_score} by {gap}")
    else:
        spec = _build_investigate_spec(
            goal_id=goal_id, stamped_agent=stamped_agent,
            best_agent=best_agent, gap=gap,
            best_score=best_score, stamped_score=stamped_score,
        )
        reason = (f"{best_agent} score {best_score} beats "
                  f"{stamped_agent} {stamped_score} by {gap}")

    return _make_decision(
        decision="file",
        reason=reason,
        stamped_agent=stamped_agent, best_agent=best_agent,
        best_score=best_score, stamped_score=stamped_score,
        gap=gap, scores=scores, goal_id=goal_id,
        investigate_spec=spec,
    )


# ---------------------------------------------------------------------------
# CLI entry point — ad-hoc audit of a goal from the live aspirations.jsonl
# ---------------------------------------------------------------------------

def _cli_main(argv=None) -> int:
    import argparse
    import json
    parser = argparse.ArgumentParser(
        description="Audit a freshly-stamped goal for Self.md routing mismatch.")
    parser.add_argument("--goal-json", required=True,
                        help="Inline goal JSON or @path/to/goal.json")
    parser.add_argument("--project-root", default=".",
                        help="Project root (default: cwd)")
    parser.add_argument("--min-gap", type=float, default=0.015,
                        help="Minimum Jaccard-overlap gap to file for the "
                             "specific-agent case (default: 0.015; g-115-1200)")
    parser.add_argument("--min-gap-either", type=float, default=0.015,
                        help="Minimum best-vs-second-best Jaccard gap to file "
                             "for the either-case (default: 0.015; g-115-1200)")
    parser.add_argument("--min-best-score", type=float, default=0.10,
                        help="Absolute floor on the best agent's Jaccard score "
                             "below which no mismatch is filed regardless of "
                             "gap (default: 0.10; rb-1488 / g-115-1351)")
    parser.add_argument("--exclude", default="",
                        help="Comma-separated agent names to exclude")
    args = parser.parse_args(argv)

    goal_arg = args.goal_json
    if goal_arg.startswith("@"):
        try:
            goal_arg = Path(goal_arg[1:]).read_text(encoding="utf-8")
        except OSError as exc:
            print(json.dumps({"error": str(exc)}), file=sys.stderr)
            return 2
    try:
        goal = json.loads(goal_arg)
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"goal JSON parse: {exc}"}), file=sys.stderr)
        return 2

    exclude = {a.strip() for a in args.exclude.split(",") if a.strip()}
    result = audit(
        goal,
        project_root=Path(args.project_root).resolve(),
        min_gap=args.min_gap,
        min_gap_either=args.min_gap_either,
        min_best_score=args.min_best_score,
        exclude_agents=exclude or None,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("decision") == "no_file" else 0  # exit 0 either way


if __name__ == "__main__":
    raise SystemExit(_cli_main())
