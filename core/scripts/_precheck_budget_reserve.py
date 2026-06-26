"""Retrospection-budget reservation for the precheck budget meter ().

bravo session-66 #4: in the TIGHT context zone, ALL deferrable precheck sweeps
drop (zone_drop_rules.tight=[deferrable]) -- INCLUDING the retrospection-class
sweeps (pending-questions, parent-supersession, fresh-eyes-review, felt-sense)
that are exactly what catches boxed-in patterns under context pressure. That is
backwards: the sweeps that would notice the agent is boxed-in are the first
throttled by context economy.

This helper RESERVES budget for retrospection: a retrospection-class sweep that
WOULD drop under the tight zone is forced to RUN when no retrospection sweep has
run in >= threshold ITERATIONS. The staleness is a discrete per-iteration
counter, NOT wall-clock -- guard-784 (the g-115-1489 lesson): wall-clock-since-
start captures inter-tool-call LLM latency (minutes), not script cost
(sub-second), and fires constantly; gate on zone + a discrete counter instead.

Single source of truth: aspirations-precheck-budget-meter.sh imports
reserve_decision() from here; tests import it directly. Do NOT inline + duplicate
the logic into the .sh heredoc AND a test fixture -- that is precisely the
duplicate-allowlist rot class g-303-21 / the zeta audit just fixed.
"""

# The retrospection-class deferrable sweeps named in  (bravo session-66
# #4): pending-questions, parent-supersession, fresh-eyes-review, felt-sense.
# pending-questions-sweep + parent-supersession-sweep are per-iteration sweeps
# that EXECUTE when the meter allows them; fresh-eyes-cadence + felt-sense-cadence
# are the precheck gates for the cadence-gated rituals (they still fire on their
# own goal-count cadence downstream, but the meter must not be what throttles
# them under context pressure).
RETROSPECTION_SWEEPS = frozenset({
    "pending-questions-sweep",
    "parent-supersession-sweep",
    "fresh-eyes-cadence",
    "felt-sense-cadence",
})

DEFAULT_RESERVE_THRESHOLD = 8  # iterations without a retrospection run -> reserve


def is_retrospection(sweep):
    """True if `sweep` is one of the retrospection-class sweeps."""
    return sweep in RETROSPECTION_SWEEPS


def reserve_decision(decision, reason, sweep, cur_iter, last_retro_iter,
                     threshold=DEFAULT_RESERVE_THRESHOLD):
    """Apply the  retrospection-reservation override to a base decision.

    Args:
      decision, reason: the meter's BASE decision ('run'/'drop') + its reason
        string, computed from the always-run / zone-drop logic.
      sweep: the sweep name being checked.
      cur_iter: the current discrete iteration counter (from the persistent
        tracker, incremented once per `meter start`).
      last_retro_iter: the iteration at which a retrospection sweep last RAN.
      threshold: staleness (in iterations) at which the reservation fires.

    Returns (decision, reason, new_last_retro_iter):
      - A retrospection sweep that would DROP and whose staleness
        (cur_iter - last_retro_iter) >= threshold is overridden to 'run'
        (reason 'retrospection-reserved:staleN'). This is the reservation --
        the stale retrospection wins over the tight-zone drop.
      - new_last_retro_iter advances to cur_iter when the (possibly-overridden)
        decision is 'run' for a retrospection sweep, else stays unchanged. The
        CALLER persists it (this function is pure: no I/O, no clock).

    Non-retrospection sweeps and non-stale retrospection sweeps pass through
    untouched -- the override is conservative and only ever turns a drop into a
    run for a genuinely-stale retrospection class.
    """
    if sweep in RETROSPECTION_SWEEPS:
        stale = cur_iter - last_retro_iter
        if decision == "drop" and stale >= threshold:
            decision = "run"
            reason = "retrospection-reserved:stale{}".format(stale)
        if decision == "run":
            return decision, reason, cur_iter
    return decision, reason, last_retro_iter
