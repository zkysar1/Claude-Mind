#!/usr/bin/env python3
"""Per-agent fleet CAPACITY snapshot — routed / AVAILABLE / HIGH / in_flight, one call.

Answers "who is actually free to take a HIGH product goal right now?" without the
manual multi-step loop that TIMED OUT at 2m (exit 143) on 2026-08-04, leaving the
largest routed backlog unmeasured so the sprint plan shipped 5-agent in scope and
4-agent in evidence (gap-086, g-353-04).

WHY THIS DOES NOT RUN goal-selector.sh
======================================
The obvious implementation — loop the selector once per agent and count candidates —
is the one thing this script must not do, for three independently disqualifying
reasons found by the pre-apply consultation, not by trial:

  guard-2261  goal-selector.sh IS NOT IDEMPOTENT. Every invocation MUTATES
              agents/<agent>/session/drain-lane-state.json, and every
              drain_lane_interval_iterations-th run (K=5) FORCE-PICKS the most-overdue
              recurring goal. A 5-agent snapshot would therefore mutate five agents'
              drain state and could force-pick recurring goals in lanes it was only
              supposed to OBSERVE. A measurement instrument must not move its subject.
  guard-3562  Its output is STOCHASTIC (exploration_noise ~0.9 against a top-4 spread
              of ~0.8), so any count derived from it is not reproducible, and re-running
              moves the goal the caller is permitted to claim.
  (cost)      Selection measured ~18.5s on this box. Five agents is ~90s+ — which is
              exactly the 2-minute timeout the gap record reports.

So every column here is derived from the STORES plus team-state: deterministic,
side-effect-free, and safe to run against other agents' lanes. The price is that
`top_pick` cannot be computed at all (see NOT MEASURED below) — the honest report,
per guard-3016's reading rule: report an unmeasurable quantity as unmeasurable rather
than quoting a proxy with a caveat.

THE COLUMN THAT MATTERS IS `available`, NOT `routed`
====================================================
guard-2596, measured 2026-08-04: per-agent routed counts read zeta 185 / alpha 137 /
bravo 62 / foxtrot 49 / echo 18-with-zero-HIGH, and the drafted recommendation was
"echo is starved, re-route work to it". But `routed` counts goals DIRECTED to an agent,
while 750 of 1201 open goals sat in the `either` shared pool that every agent can pick
from — echo's own selector returned 618 executable candidates. echo lacked DIRECTED
work, not work. The excluded bucket held 62% of the population and INVERTED the
recommendation. The field name encodes an ASSIGNMENT axis; the decision is about
AVAILABILITY, and there is no predicate of yours to inspect because the exclusion is
baked into the field's own definition.

This script therefore always prints the shared-pool residual beside the per-agent
counts, and emits guard-2596's own prescribed check as a CONSERVATION line: the
per-entity values plus the shared bucket must reconcile against the population total.
A run whose conservation line does not balance has dropped a bucket — do not act on it.

NOT A THROUGHPUT SIGNAL
=======================
These are INVENTORY counts (how much work is available), never rates. guard-3016:
team-state publishes no denominator — `session_start` is null for every agent — so a
cross-agent rate cannot be normalised from the store at all. guard-4198: one goal costs
~2 min in a framework sweep and 15+ min in a lane that drives a live external service,
of which 10 min is an incompressible fixed wait, so a low count is the signature of a
long-running mandate, not of under-production. Never rank agents on these numbers.
"""
import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _agents import get_active_agents  # noqa: E402
from _paths import WORLD_DIR, agent_dir, enumerate_agent_confs  # noqa: E402

# A goal is EXECUTABLE when an agent could pick it up right now. Terminal statuses and
# every "parked" marker are excluded; the exclusions are named so a reader can see what
# each count leaves out rather than having to infer it (guard-2596's own lesson applied
# to this script's own predicate).
OPEN_STATUSES = ("pending",)
BUSY_STATUSES = ("in-progress",)
SHARED_ROUTING = ("either", "", None)


# Records lost at parse time, keyed by store path. NOT bookkeeping: a line that
# fails json.loads never increments `population` AND never lands in a bucket, so
# it leaves BOTH sides of the conservation identity at once -- `balances` stays
# True, `residual` stays 0, and the census is silently short. The conservation
# line is this tool's advertised trust signal and it is structurally blind to
# exactly this class, so the drop is counted separately and reported separately.
# guard-4387: count LINES and count RECORDS and compare -- one-line-per-record is
# a claim about the emitter, not an invariant the emitter enforces. Its asymmetry
# decides the handling here too: a silent drop breaches the very floor this tool
# exists to hold and looks identical to a clean run, so bias to refuse.
_PARSE_DROPS = {}


def _load_jsonl(path):
    """Framework-side store read. Mirrors audit-user-to-agent.py's idiom."""
    if not path.is_file():
        return []
    recs = []
    dropped = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except json.JSONDecodeError:
            dropped += 1
    if dropped:
        _PARSE_DROPS[str(path)] = _PARSE_DROPS.get(str(path), 0) + dropped
    return recs


def _discover_agents():
    """The FLEET roster, not this box's roster.

    `enumerate_agent_confs()` globs `*/local-paths.conf`, which exists only for
    agents BOUND ON THIS MACHINE. It was the first thing tried here and it is
    wrong for a fleet snapshot: measured on this box it returned ('echo',) while
    the live fleet is ('alpha','bravo','echo','foxtrot','zeta'), so 1,134 goals
    routed to the other four fell into `unknown_routing` and four agents were
    simply missing from the table. `get_active_agents()` reads world team-state
    (the SHARED store) and falls back to conf-discovery only when that is
    unreadable — the same precedence every other cross-agent consumer uses.

    Worth recording how this was caught: nothing external found it. The
    guard-2596 conservation line this script emits about ITS OWN counts printed
    a 1,134-goal residual under `unknown intended_agent`, which is precisely the
    "the per-entity partition is narrower than the population" defect that check
    exists to surface — landing on the author of the check. A snapshot without
    the conservation line would have shown one agent and looked fine.
    """
    agents = get_active_agents()
    if agents:
        return sorted(agents)
    return sorted(conf.parent.name for conf in enumerate_agent_confs())


def _iter_goals(asp_path, queue_label):
    """Yield (aspiration, goal, queue_label) for every goal in a live aspiration."""
    for asp in _load_jsonl(asp_path):
        if asp.get("status") in ("archived", "retired", "completed"):
            continue
        for g in (asp.get("goals") or []):
            yield asp, g, queue_label


def _blocked_reason(g):
    """Why this goal is not pickable right now, or None if it is.

    Returned as a REASON string rather than a bool so the summary can report the
    composition of the excluded bucket instead of a bare difference — a count whose
    exclusions are invisible is the exact defect guard-2596 describes.
    """
    st = g.get("status")
    if st in BUSY_STATUSES:
        return "in-progress"
    if st not in OPEN_STATUSES:
        return f"status:{st}"
    if g.get("defer_reason"):
        return "deferred"
    if g.get("blocker_ref"):
        return "blocker_ref"
    return None


def _carve_out(g, exclude_recurring, exclude_hypothesis):
    """Arbitrary carve-out predicates.

    Required by the 2026-08-10 encounter (echo, g-115-4488): a standing directive can
    name its own exclusions -- there, hypothesis-resolution and recurring goals -- and a
    fleet-vantage probe of "who has executable work in THIS aspiration" is wrong unless
    it can honour them. A fixed column set cannot answer a question the directive
    phrases in its own terms.
    """
    if exclude_recurring and g.get("recurring"):
        return "recurring"
    if exclude_hypothesis and g.get("hypothesis_id"):
        return "hypothesis"
    return None


def collect(agents, aspiration=None, exclude_recurring=False, exclude_hypothesis=False):
    """Single pass over world + every agent queue. No selector, no mutation."""
    rows = {a: {"routed": 0, "routed_executable": 0, "routed_high": 0,
                "private": 0, "private_executable": 0, "private_high": 0,
                # An agent whose private queue is not on THIS box must not read
                # as "0 private goals" — that is an absent bucket wearing a real
                # zero's clothes, the same confusion the conservation check and
                # guard-3948 exist to prevent. The roster now comes from shared
                # team-state, so it can legitimately name an agent whose files
                # this box has never synced.
                "private_queue_readable": False}
            for a in agents}
    shared = {"total": 0, "executable": 0, "high": 0}
    excluded = {}
    carved = {}
    population = 0
    unknown_routing = {}

    sources = [(Path(WORLD_DIR) / "aspirations.jsonl", "world")]
    for a in agents:
        p = Path(agent_dir(a)) / "aspirations.jsonl"
        rows[a]["private_queue_readable"] = p.is_file()
        sources.append((p, f"agent:{a}"))

    for path, label in sources:
        for asp, g, queue in _iter_goals(path, label):
            if aspiration and (asp.get("id") != aspiration):
                continue
            population += 1

            carve = _carve_out(g, exclude_recurring, exclude_hypothesis)
            if carve:
                carved[carve] = carved.get(carve, 0) + 1
                continue

            reason = _blocked_reason(g)
            if reason:
                excluded[reason] = excluded.get(reason, 0) + 1

            is_high = (g.get("priority") == "HIGH")

            if queue.startswith("agent:"):
                # A private queue is owned by its agent by construction — routing
                # fields do not apply. Counted separately so the world-queue
                # conservation check below is not polluted by private work.
                owner = queue.split(":", 1)[1]
                if owner in rows:
                    rows[owner]["private"] += 1
                    if not reason:
                        rows[owner]["private_executable"] += 1
                        if is_high:
                            rows[owner]["private_high"] += 1
                continue

            routed_to = g.get("intended_agent")
            if routed_to in SHARED_ROUTING:
                shared["total"] += 1
                if not reason:
                    shared["executable"] += 1
                    if is_high:
                        shared["high"] += 1
            elif routed_to in rows:
                rows[routed_to]["routed"] += 1
                if not reason:
                    rows[routed_to]["routed_executable"] += 1
                    if is_high:
                        rows[routed_to]["routed_high"] += 1
            else:
                # Routed to a name that is not a live agent (retired agent, typo, a
                # vocabulary value like "any"). Counted, never silently dropped —
                # an unreconciled residual is what makes the conservation line lie.
                unknown_routing[str(routed_to)] = unknown_routing.get(str(routed_to), 0) + 1

    return {
        "agents": rows,
        "shared_pool": shared,
        "population": population,
        "excluded_from_executable": excluded,
        "carved_out": carved,
        "unknown_routing": unknown_routing,
    }


def conservation(data):
    """guard-2596's prescribed check, emitted rather than assumed.

    'Sum the per-entity values and compare against the population total; a large
    residual IS the shared/unassigned bucket, and if the decision is about AVAILABILITY
    rather than ASSIGNMENT that residual is the part that matters.'
    """
    routed = sum(r["routed"] for r in data["agents"].values())
    private = sum(r["private"] for r in data["agents"].values())
    shared = data["shared_pool"]["total"]
    unknown = sum(data["unknown_routing"].values())
    carved = sum(data["carved_out"].values())
    accounted = routed + private + shared + unknown + carved
    # Kept SEPARATE from `balances`. `balances` is the arithmetic identity; a
    # parse drop is invisible to it by construction (both sides lose the record
    # together). Two INDEPENDENT reasons the run is not a result, so they stay two
    # keys and main() ANDs them at the single decision point.
    #
    # There is deliberately NO composite `trustworthy` key here. The first version
    # of this fix added one, computed as `(accounted == population) and not
    # parse_dropped` -- a SECOND expression of the same arithmetic that `balances`
    # already states. test_non_balancing_run_exits_1 caught it immediately: that
    # test forces `balances: False` in the returned dict, the parallel expression
    # never saw the override, and a NON-BALANCING RUN EXITED 0 -- the exact defect
    # the exit code exists to prevent. One fact, one field (communication-clarity
    # rule 5); a derived duplicate is a second source of truth waiting to diverge.
    parse_dropped = sum(_PARSE_DROPS.values())
    return {
        "parse_dropped": parse_dropped,
        "parse_drop_sources": dict(_PARSE_DROPS),
        "routed": routed,
        "private": private,
        "shared_pool": shared,
        "unknown_routing": unknown,
        "carved_out": carved,
        "accounted": accounted,
        "population": data["population"],
        "balances": accounted == data["population"],
        "residual": data["population"] - accounted,
    }


def render(data, team_state, cons, args):
    out = []
    out.append("FLEET CAPACITY SNAPSHOT — INVENTORY, NOT THROUGHPUT.")
    out.append("  These are counts of AVAILABLE WORK, never rates. team-state publishes no")
    out.append("  denominator (session_start is null fleet-wide, guard-3016) and one goal's")
    out.append("  cost differs ~7x by lane (guard-4198). Do NOT rank agents on these numbers.")
    if args.aspiration:
        out.append(f"  scope: aspiration={args.aspiration}")
    if args.exclude_recurring or args.exclude_hypothesis:
        cv = [k for k, v in (("recurring", args.exclude_recurring),
                             ("hypothesis", args.exclude_hypothesis)) if v]
        out.append(f"  carve-outs applied: {', '.join(cv)}")
    out.append("")

    shared = data["shared_pool"]
    hdr = (f"{'agent':<10} {'routed':>7} {'r-exec':>7} {'r-HIGH':>7} "
           f"{'private':>8} {'p-exec':>7} {'AVAILABLE':>10} {'HIGH-avail':>11} {'in_flight':>10}")
    out.append(hdr)
    out.append("-" * len(hdr))
    for a in sorted(data["agents"]):
        r = data["agents"][a]
        # AVAILABLE is the load-bearing column: what this agent could pick up NOW =
        # its own executable routed work + its private queue + the SHARED pool every
        # agent can draw from. Omitting the shared term is the guard-2596 inversion.
        available = r["routed_executable"] + r["private_executable"] + shared["executable"]
        high_avail = r["routed_high"] + r["private_high"] + shared["high"]
        if team_state is None:
            infl_s = "n/r"   # team-state read FAILED — not "this agent is idle"
        else:
            infl = (team_state.get(a) or {}).get("in_flight")
            infl_s = (infl or {}).get("goal_id", "-") if isinstance(infl, dict) else "-"
        # "n/r" (not readable), never a bare 0, when this box has no copy of
        # that agent's private queue. The distinction is the whole point.
        priv = str(r["private"]) if r["private_queue_readable"] else "n/r"
        priv_x = str(r["private_executable"]) if r["private_queue_readable"] else "n/r"
        out.append(f"{a:<10} {r['routed']:>7} {r['routed_executable']:>7} {r['routed_high']:>7} "
                   f"{priv:>8} {priv_x:>7} "
                   f"{available:>10} {high_avail:>11} {infl_s:>10}")
    if team_state is None:
        out.append("  n/r in in_flight = team-state read FAILED. This column is unknown for "
                   "every agent; it does NOT mean nobody is mid-execution. Counts unaffected.")
    unreadable = [a for a in sorted(data["agents"])
                  if not data["agents"][a]["private_queue_readable"]]
    if unreadable:
        out.append(f"  n/r = private queue not present on this box: {', '.join(unreadable)}. "
                   f"Their AVAILABLE counts the world queue only and is a LOWER BOUND.")
    out.append("")
    out.append(f"SHARED POOL (every agent can draw from this): {shared['total']} total, "
               f"{shared['executable']} executable, {shared['high']} HIGH")
    out.append("  ^ this is the bucket a routed-count comparison EXCLUDES (guard-2596). It is")
    out.append("    added into every agent's AVAILABLE above, which is why AVAILABLE columns")
    out.append("    are not disjoint and MUST NOT be summed.")
    out.append("")

    out.append(f"CONSERVATION (guard-2596's own check): routed {cons['routed']} + private "
               f"{cons['private']} + shared {cons['shared_pool']} + unknown-routing "
               f"{cons['unknown_routing']} + carved {cons['carved_out']} = {cons['accounted']} "
               f"vs population {cons['population']} -> "
               f"{'BALANCES' if cons['balances'] else 'RESIDUAL ' + str(cons['residual'])}")
    if not cons["balances"]:
        out.append("  ** DOES NOT BALANCE — a bucket was dropped. Do not act on this run. **")
    if cons["parse_dropped"]:
        out.append(f"  ** {cons['parse_dropped']} record(s) DROPPED AT PARSE: "
                   f"{cons['parse_drop_sources']}. A parse drop leaves BOTH sides of the "
                   f"identity at once, so the line above still reads BALANCES while the "
                   f"census is short. Do not act on this run. **")
    if data["unknown_routing"]:
        out.append(f"  unknown intended_agent values: {data['unknown_routing']}")
    if data["excluded_from_executable"]:
        out.append(f"  not-executable composition: {data['excluded_from_executable']}")
    out.append("")
    out.append("top_pick per agent: NOT MEASURED. Its only source is goal-selector.sh, which is")
    out.append("  non-idempotent (mutates drain-lane-state.json; every 5th run force-picks an")
    out.append("  overdue recurring goal — guard-2261) and stochastic (guard-3562). Running it")
    out.append("  per agent would move the lanes this snapshot exists to observe. Reported as")
    out.append("  unmeasurable rather than proxied (guard-3016 reading rule).")
    return "\n".join(out)


def _read_team_state():
    """agent_status map, or None when the read FAILED.

    None and {} are different facts and the caller renders them differently: {}
    means "read fine, nobody holds an in_flight", None means "could not tell".
    Returning {} for both would print an empty in_flight column for every agent
    on a failed read -- i.e. report "nobody in the fleet is mid-execution" from
    no evidence, which is the guard-3016 reading rule (report an unmeasurable
    quantity as unmeasurable) violated in the one column that reads as live
    state. Every other column in this file already honours that distinction:
    n/r for an unsynced private queue, NOT MEASURED for top_pick.
    """
    try:
        import subprocess
        from _runtime_bash import BASH as bash
        r = subprocess.run([bash, str(_HERE / "team-state-read.sh"), "--json"],
                           capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            return None
        s = r.stdout or ""
        i = s.find("{")
        if i == -1:
            return None
        return (json.loads(s[i:]) or {}).get("agent_status") or {}
    except Exception:
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Per-agent fleet capacity snapshot (routed / AVAILABLE / HIGH / in_flight).")
    ap.add_argument("--json", action="store_true", help="emit the JSON payload instead of the table")
    ap.add_argument("--aspiration", help="scope every count to one aspiration id")
    ap.add_argument("--exclude-recurring", action="store_true",
                    help="carve-out: drop recurring goals from every count")
    ap.add_argument("--exclude-hypothesis", action="store_true",
                    help="carve-out: drop hypothesis-resolution goals from every count")
    ap.add_argument("--agents", help="comma-separated agent subset (default: all discovered)")
    args = ap.parse_args(argv)

    agents = ([a.strip() for a in args.agents.split(",") if a.strip()]
              if args.agents else _discover_agents())
    # Module-level accumulator: reset per run so a second in-process call cannot
    # inherit the first's drops. One-shot from the CLI, but the tests call main()
    # twice in one process and a stale count would make the second run's verdict
    # depend on the first's corpus.
    _PARSE_DROPS.clear()
    data = collect(agents, args.aspiration, args.exclude_recurring, args.exclude_hypothesis)
    cons = conservation(data)
    team_state = _read_team_state()

    if args.json:
        data["conservation"] = cons
        data["top_pick"] = None
        data["top_pick_not_measured_reason"] = (
            "goal-selector.sh is non-idempotent (guard-2261) and stochastic (guard-3562); "
            "running it per agent would mutate the lanes being observed")
        data["not_a_throughput_signal"] = (
            "inventory counts only; no denominator exists (guard-3016) and goal unit costs "
            "differ ~7x by lane (guard-4198)")
        print(json.dumps(data, indent=2))
    else:
        print(render(data, team_state, cons, args))
    # Exit 1 ONLY when the run is not a result: buckets that do not reconcile, OR
    # records lost at parse. Both authoritative fields are read HERE, at the one
    # decision point, rather than pre-combined in conservation() — see the note
    # there for the regression that produced this shape.
    return 0 if (cons["balances"] and not cons["parse_dropped"]) else 1


if __name__ == "__main__":
    sys.exit(main())
