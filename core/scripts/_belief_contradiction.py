#!/usr/bin/env python3
"""Theory-of-Mind contradiction-triggered forced reflection ().

Outcome 3 of the partner-belief loop (g-306-18 proved storage; g-306-28 wired
the write+consume loop in `_team_belief.py` / `team-belief-write.sh` /
fresh-eyes-review Phase 2.6b/2.6c). This module closes the loop: when a
partner's freshly OBSERVED focus contradicts a domain-belief THIS agent holds
about that partner (confidence >= threshold) for N CONSECUTIVE observations, a
forced reflection REVISES the held belief (lowers its confidence or supersedes
it) and records the surprise.

Three pure functions plus a composer, mirroring `_team_belief.py`'s
pure-module-+-thin-daemon-orchestrator pattern (rb-1989) so the whole detection
logic is unit-testable without a daemon:

  classify(beliefs, about, observed_domain, threshold)
      -> "match" | "contradiction" | "skip"
  next_streak(prev, status, observed_domain, n_required)
      -> (new_streak, should_revise)   # the N-consecutive gate
  revise(beliefs, about, observed_domain, now_iso, mode, ...)
      -> revised beliefs list          # lower-confidence or supersede
  process_all(team_state, self_agent, prev_streaks, now_iso, ...)
      -> {revised_beliefs, new_streaks, events}   # all partners in one pass

The `no false-trigger on first observation` invariant (g-306-29 outcome 3) is
enforced by `next_streak`: a single divergent observation increments the streak
to 1 but `should_revise` stays False until the count reaches `n_required`
(default 2). A `match` or `skip` clears the streak, so a one-off divergence that
reverts never triggers.

Race safety is inherited from `_team_belief.py`: the revised beliefs are the
calling agent's OWN sublist (agent_status.<self>.beliefs), of which it is the
sole writer, so the daemon read -> compute -> set the wrapper performs is
race-free at the field level under the shared team-state lock.
"""
import argparse
import json
import math
import sys
from datetime import datetime

# Defaults — overridable via process_all kwargs / CLI flags.
DEFAULT_THRESHOLD = 0.5      # only beliefs held at/above this confidence are checkable
DEFAULT_N_REQUIRED = 2       # consecutive contradicting observations before a trigger
DEFAULT_LOWER_FACTOR = 0.5   # mode=lower multiplies confidence by this
DEFAULT_SUPERSEDE_CONFIDENCE = 0.5  # mode=supersede resets to this calibrated value


def _conf(value):
    """Parse a confidence to a finite float in [0, 1]; junk -> 0.0 (un-checkable)."""
    try:
        c = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(c):
        return 0.0
    return max(0.0, min(1.0, c))


def _find_domain_belief(beliefs, about, threshold):
    """Return the first dict belief about `about` carrying a non-empty `domain`
    held at confidence >= threshold, else None. (supersede keeps one-per-about,
    but stay defensive about malformed lists.)"""
    if not isinstance(beliefs, list):
        return None
    for b in beliefs:
        if not isinstance(b, dict):
            continue
        if b.get("about") != about:
            continue
        domain = b.get("domain")
        if not domain:  # None or "" -> free-form belief, not contradiction-checkable
            continue
        if _conf(b.get("confidence")) >= threshold:
            return b
    return None


def _focus_lane(value):
    """Reduce a current_focus / belief-domain value to its LANE key for
    comparison (g-115-1576).

    Since g-115-1575 the producer stamps current_focus as the verbose
    'asp-NNN: title' string (team-state.py in_flight + its daemon twin,
    byte-symmetric per guard-742), and fresh-eyes-review Phase 2.6c stamps
    belief.domain FROM that current_focus VERBATIM — so both operands carry the
    same verbose vocabulary. The semantic unit a domain-belief actually asserts
    is "WHICH LANE is the partner in", i.e. the 'asp-NNN' prefix, NOT the
    per-goal title. Comparing the verbose strings raw therefore fires a SPURIOUS
    contradiction every time the partner advances to a different GOAL within the
    SAME lane. Reducing both sides to the lane makes the comparison fire only on
    a genuine cross-lane move.

        'asp-115: Idea: reconcile ...'  -> 'asp-115'  (first ':' only; titles may carry ':')
        'asp-115'                       -> 'asp-115'   (bare lane -> itself)
        'framework-architecture'        -> 'framework-architecture'  (colon-free token -> itself)
        None / ''                       -> ''          (no observation)
    """
    if not value:
        return ""
    return str(value).split(":", 1)[0].strip()


def classify(beliefs, about, observed_domain, threshold=DEFAULT_THRESHOLD):
    """Classify a fresh observation of `about`'s focus against the held belief.

    Returns one of:
      "skip"          — no observation, or no domain-belief about `about` at/above
                        `threshold` (nothing to contradict — conservative, the
                        source of the no-false-trigger guarantee for un-held beliefs)
      "match"         — held domain and observed share a LANE (belief confirmed)
      "contradiction" — held domain and observed are in different LANES (candidate
                        for revision)

    Comparison is LANE-granular via `_focus_lane` (g-115-1576): a verbose
    'asp-NNN: title' current_focus reduces to its 'asp-NNN' lane so a goal-title
    change within one lane is NOT a contradiction. Colon-free short tokens reduce
    to themselves, so legacy/short-token beliefs compare exactly as before.
    """
    if not observed_domain:  # None/empty observation: cannot compare
        return "skip"
    b = _find_domain_belief(beliefs, about, threshold)
    if b is None:
        return "skip"
    return ("match" if _focus_lane(b.get("domain")) == _focus_lane(observed_domain)
            else "contradiction")


def next_streak(prev, status, observed_domain, n_required=DEFAULT_N_REQUIRED):
    """Advance the per-partner consecutive-contradiction streak.

    `prev` is the persisted streak dict {"observed_domain": str, "count": int}
    or None. Returns `(new_streak, should_revise)`:

      status in ("match", "skip")  -> (None, False)   # clears the streak
      status == "contradiction":
        - same observed_domain as prev  -> count = prev.count + 1
        - different / no prev           -> count = 1   (NEW divergence)
        should_revise = count >= n_required
        when should_revise -> (None, True)   # streak resets; the belief is revised
        else               -> ({observed_domain, count}, False)  # first obs: count 1 < N -> no trigger

    The reset-on-match/skip + reset-on-domain-change behavior is what makes a
    one-off or oscillating divergence NOT trigger — only a SUSTAINED contradiction
    on the SAME observed domain accumulates to the threshold.

    `n_required` is floored to 2: outcome 3 ("no false-trigger on a FIRST
    observation") is a structural invariant, not merely the default. An
    adversarial env override (BELIEF_CONTRADICTION_N=1/0/negative) can never
    force a revision on the first divergent observation.
    """
    n_required = max(2, n_required)  # outcome-3 floor ( review, 2026-06-18)
    if status != "contradiction":
        return None, False
    if isinstance(prev, dict) and prev.get("observed_domain") == observed_domain:
        count = int(prev.get("count", 0)) + 1
    else:
        count = 1
    if count >= n_required:
        return None, True
    return {"observed_domain": observed_domain, "count": count}, False


def revise(beliefs, about, observed_domain, now_iso,
           mode="lower", factor=DEFAULT_LOWER_FACTOR,
           superseded_confidence=DEFAULT_SUPERSEDE_CONFIDENCE,
           threshold=DEFAULT_THRESHOLD):
    """Return a new beliefs list with the contradicted belief about `about`
    revised, recording the surprise. Other entries are untouched.

    mode == "lower":     multiply the held belief's confidence by `factor`
                         (keeps the held domain, just less certain).
    mode == "supersede": replace the held domain with the OBSERVED domain at a
                         calibrated `superseded_confidence`.

    Both paths stamp `revised_at`, `prior_domain`, and `prior_confidence` onto
    the entry — that annotation IS the recorded surprise (g-306-29 outcome:
    "records the surprise"). If no contradicted domain-belief is found, the list
    is returned unchanged (defensive — process_all only calls this after a
    `contradiction` verdict, but revise must be safe in isolation).
    """
    if not isinstance(beliefs, list):
        return []
    target = _find_domain_belief(beliefs, about, threshold)
    if target is None or _focus_lane(target.get("domain")) == _focus_lane(observed_domain):
        return list(beliefs)  # nothing to revise (no held domain-belief, or same lane)

    out = []
    for b in beliefs:
        if b is not target:
            out.append(b)
            continue
        prior_domain = b.get("domain")
        prior_conf = _conf(b.get("confidence"))
        revised = dict(b)
        revised["prior_domain"] = prior_domain
        revised["prior_confidence"] = prior_conf
        revised["revised_at"] = now_iso
        revised["last_observed"] = now_iso
        if mode == "supersede":
            revised["domain"] = observed_domain
            revised["confidence"] = max(0.0, min(1.0, float(superseded_confidence)))
            revised["belief"] = (
                "superseded after sustained contradiction: observed "
                "'{obs}' (prior held domain '{prior}')".format(
                    obs=observed_domain, prior=prior_domain)
            )
        else:  # "lower"
            revised["confidence"] = max(0.0, min(1.0, prior_conf * float(factor)))
        out.append(revised)
    return out


def process_all(team_state, self_agent, prev_streaks, now_iso,
                threshold=DEFAULT_THRESHOLD, n_required=DEFAULT_N_REQUIRED,
                mode="lower", factor=DEFAULT_LOWER_FACTOR):
    """Run the contradiction check across every partner in one pass.

    Reads each partner's observed focus from
    `team_state["agent_status"][partner]["current_focus"]` and compares it
    against the self agent's held domain-beliefs
    (`team_state["agent_status"][self_agent]["beliefs"]`). Returns:

      {
        "revised_beliefs": <self.beliefs list IF any revision fired, else None>,
        "new_streaks":     {partner: {observed_domain, count}}  # only ongoing streaks
        "events":          [ {partner, status, observed_domain, held_domain,
                              should_revise, ...} ]  # one per non-skip partner
      }

    `revised_beliefs` is None when nothing changed so the wrapper can skip the
    daemon write entirely. Revisions chain across partners on the same beliefs
    list (each targets a distinct `about` entry).
    """
    agent_status = (team_state or {}).get("agent_status", {}) or {}
    self_beliefs = agent_status.get(self_agent, {}).get("beliefs") or []
    prev_streaks = prev_streaks or {}

    beliefs = list(self_beliefs)
    new_streaks = {}
    events = []
    any_revision = False

    for partner in sorted(agent_status):
        if partner == self_agent:
            continue
        # Reduce the verbose 'asp-NNN: title' current_focus to its lane key
        # (6) so the streak below counts CONSECUTIVE cross-lane
        # observations rather than resetting on every goal-title change within a
        # lane. classify/revise re-apply _focus_lane idempotently; keeping the
        # lane here is what makes next_streak's same-domain accumulation correct.
        observed = _focus_lane(agent_status.get(partner, {}).get("current_focus"))
        status = classify(beliefs, partner, observed, threshold)
        if status == "skip":
            # No checkable belief / no observation. Clear any stale streak by
            # simply not carrying it forward.
            continue
        held = _find_domain_belief(beliefs, partner, threshold)
        held_domain = held.get("domain") if held else None
        streak, should_revise = next_streak(
            prev_streaks.get(partner), status, observed, n_required)
        event = {
            "partner": partner,
            "status": status,
            "observed_domain": observed,
            "held_domain": held_domain,
            "should_revise": should_revise,
        }
        if should_revise:
            beliefs = revise(beliefs, partner, observed, now_iso,
                             mode=mode, factor=factor, threshold=threshold)
            any_revision = True
            event["revised_mode"] = mode
        elif streak is not None:
            new_streaks[partner] = streak
        events.append(event)

    return {
        "revised_beliefs": beliefs if any_revision else None,
        "new_streaks": new_streaks,
        "events": events,
    }


def main(argv=None):
    """CLI composer: team-state JSON on stdin, self + prev-streaks + params via
    argv; prints the process_all result JSON to stdout. Values arrive as argv /
    stdin — never interpolated into a `python -c` source string (guard-165 safe;
    this is a real module file)."""
    ap = argparse.ArgumentParser(
        description="Theory-of-Mind contradiction-triggered belief revision (g-306-29)")
    ap.add_argument("--self", dest="self_agent", required=True,
                    help="The agent whose beliefs are being checked (the belief owner)")
    ap.add_argument("--prev-streaks", default="{}",
                    help="JSON map {partner: {observed_domain, count}} of ongoing streaks")
    ap.add_argument("--now", default=None, help="ISO timestamp (default: now, local)")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    ap.add_argument("--n-required", type=int, default=DEFAULT_N_REQUIRED)
    ap.add_argument("--mode", choices=("lower", "supersede"), default="lower")
    ap.add_argument("--factor", type=float, default=DEFAULT_LOWER_FACTOR)
    args = ap.parse_args(argv)

    raw = sys.stdin.read().strip()
    try:
        team_state = json.loads(raw) if raw and raw != "null" else {}
    except (json.JSONDecodeError, ValueError):
        team_state = {}
    if not isinstance(team_state, dict):
        team_state = {}

    try:
        prev_streaks = json.loads(args.prev_streaks) if args.prev_streaks else {}
    except (json.JSONDecodeError, ValueError):
        prev_streaks = {}
    if not isinstance(prev_streaks, dict):
        prev_streaks = {}

    now_iso = args.now or datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    result = process_all(team_state, args.self_agent, prev_streaks, now_iso,
                         threshold=args.threshold, n_required=args.n_required,
                         mode=args.mode, factor=args.factor)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
