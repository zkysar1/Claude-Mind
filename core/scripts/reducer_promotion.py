"""Reducer self-promotion: may THIS worker Body become the reducer?

THE THIRD MODULE OF A DELIBERATE THREE-MODULE FAMILY. Each member decides one
role's question and carries its OWN fail-safe direction. They are NOT to be
fused (guard-2783): fusing puts opposite fail-safe defaults behind one branch,
and the next edit silently inverts one of them.

    module                       question                       fail-safe
    ---------------------------- ------------------------------ -----------
    worker_reducer_liveness      "is my reducer still alive?"    wind-down
    reducer_self_fence           "have I been superseded?"       hold (keep
                                                                 running)
    reducer_promotion (this)     "may I BECOME the reducer?"     hold (do NOT
                                                                 promote)

Note the divergence those three encode, because it is the whole safety story:
rc=4 is DECISIVE in worker_reducer_liveness (wind down), INERT in
reducer_self_fence (keep running), and here it is NECESSARY-BUT-NOT-SUFFICIENT.
`test_reducer_promotion.py` pins that three-way split against the real sibling
modules so a future fusion fails loudly.

WHY THIS EXISTS. worker-loop Phase 0.5 declares NEVER-PROMOTE: no rc yields
"become the reducer", so a stale claim waits for a human `/start` however long.
Measured 2026-08-14/15: a 15.7h reducer outage in which the claim sat 72min+
past the ~65min ownership-stale threshold while a healthy same-agent worker had
ALREADY run the full discriminator set correctly. Auto-promotion would have
restored the reducer ~14h earlier.

WHY NEVER-PROMOTE EXISTED, and it is not obsolete — it is now GATED. The
broken-heartbeat-writer trap: a reducer that is ALIVE with a stale heartbeat is
indistinguishable from a dead one (measured precedent: an agent read 59h stale
while demonstrably working), and promoting against a live reducer is a dual
reducer, i.e. split-brain on every shared store. Two things make it relaxable:

  1. reducer_self_fence (g-306-225) gives the lease a real T_stepdown. The
     holder stands down on seeing a different-holder claim, read from the
     authoritative store rather than its own possibly-broken heartbeat leg.
     T_stepdown (1950s) < T_takeover (3900s) BY CONSTRUCTION, so a promotion
     that waits for T_takeover has already given the old holder a full
     stepdown window.
  2. `/start` rc=4 role DERIVATION already makes topology fluid: the framework
     picks the Body role, so a returning box auto-joins as a worker under a
     peer-held claim. Promotion at runtime extends the same philosophy.

THE INVARIANT THIS MODULE LOCKS: promotion requires ALL gates to pass. There is
no "mostly satisfied". Every unknown, unreadable, unmeasured, or unrecognised
input resolves to HOLD, and the reason names WHICH gate refused.

RESIDUAL RISK, stated rather than papered over (guard-1760). A WEDGED (not
dead) old reducer that unwedges mid-window acts as reducer until its next
heartbeat-tick sees the different-holder claim. The dual window is bounded by
one tick cadence, and a wedged reducer was not merging anyway. This is a real
exposure, not a hypothetical one; it is accepted, not eliminated.

SECOND RESIDUAL RISK, inherited from the poll and NOT closable here: the claim
endpoint returns {agent, machine_id, agent_state, heartbeat_at} with NO
runner_token, so a SAME-BOX reducer restart is invisible to `machine_id`
comparison. G4 below is what keeps that gap safe — a same-box restart produces
a LIVE claim, so the liveness verdict is `continue` and promotion is never
reached.

DEFAULT OFF. `enabled` defaults False and `fence_verified_at` defaults absent,
so this module returns HOLD on every input until the kill-test lane has run.
See core/config/conventions/reducer-promotion.md for the kill-test bar and the
RETIREMENT CRITERION recorded at birth (guard-769).
"""

import datetime as dt
import importlib.util
import json
import os
from pathlib import Path

VERDICT_HOLD = "hold"
VERDICT_PROMOTE = "promote"

# The gates, in evaluation order. First failure short-circuits, so the cheapest
# and most-often-refusing gates come first and the reason is always specific.
GATES = (
    "enabled",             # G1 master kill switch
    "fence_verified",      # G2 kill-test lane has passed
    "eligible",            # G3 this box may hold the claim at all
    "liveness_decisive",   # G4 the reducer is provably not-live, not merely unverifiable
    "claim_stale",         # G5 past T_takeover, so T_stepdown has elapsed
    "discriminators",      # G6 the observation is not a local read-path artifact
)

# The discriminator set, by name. Every one must be exactly True. `None`
# (unmeasured) is NOT a pass: an unverifiable recovery signal is treated as
# ABSENT, never as satisfied (the archive-before-delete step-2 discipline
# applied to a takeover decision).
#
# THEY ARE NOT THREE INDEPENDENT BOOLEANS, and reading this tuple as a flat set
# is the easy mistake (). D3 DEPENDS ON D2: D3's sibling half is read
# from the same authoritative store D2 is a statement about, so when the store
# did not answer, "no other fresh carrier" is a fact about the read path and not
# about the fleet. `measure_only_fresh_carrier_is_mine` below enforces that —
# D3 is None whenever D2 is False. The dependency is specified in
# core/config/conventions/reducer-promotion.md § "D3 DEPENDS ON D2".
DISCRIMINATORS = (
    "peers_alive_from_this_box",  # D1 — rules out a LOCAL read-path wedge: if
                                  # peers read alive from here, "reducer looks
                                  # dead" is about the reducer, not this box.
    "claim_read_authoritative",   # D2 — the claim came from the authoritative
                                  # store, never the local read-through mirror.
    "only_fresh_carrier_is_mine",  # D3 — no other Body has a fresh heartbeat
                                   # carrier, so promoting cannot race a live
                                   # sibling. Measured as a UNION (store for
                                   # siblings, local for self); depends on D2.
)

# rc from the liveness poll that constitutes DECISIVE evidence of a not-live
# reducer. Deliberately excludes the transient set {1,2,3} and the marker-less
# rc=0: those wind a worker down on ACCUMULATED UNVERIFIABILITY, which is
# exactly the broken-heartbeat-writer trap. Winding down on "I cannot tell"
# is safe; promoting on it is the split-brain this module must not cause.
DECISIVE_RCS = (4,)


def _load_canonical_takeover_seconds():
    """T_takeover, from the SAME reader the rest of the system already uses.

    guard-2783: key a role decision on the predicate the system already uses to
    derive that role -- never a new one, because two predicates for one role is
    a second bug. The canonical reader is agent-watchdog.py's
    `_claim_stale_window_seconds` (env OWNERSHIP_STALE_SECONDS, default 3900,
    rejecting 0/negative/float). That file is hyphenated and so not importable
    by name; import it by path rather than re-typing its 3900.

    Returns None when the canonical reader cannot be reached, which callers must
    treat as "cannot evaluate G5" -> HOLD. It deliberately does NOT fall back to
    a local 3900: a hardcoded copy here is the exact drift this function exists
    to prevent, and a missing predicate must fail visibly (rb-313).
    """
    path = Path(__file__).resolve().parent / "agent-watchdog.py"
    try:
        spec = importlib.util.spec_from_file_location("_agent_watchdog_for_promo", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return float(mod._claim_stale_window_seconds())
    except Exception:
        return None


def eligible_machines_from_config(config):
    """The explicit allowlist, normalised to a tuple of exact strings.

    guard-2860: a loosened ownership/role gate must compute its exempt set from
    IDENTITY, never relax the predicate to a pattern. Membership here is exact
    string equality against a hand-listed set, so the cardinality of what this
    newly admits is a property of the CONFIG (reviewable at review time), not of
    whatever happens to be on disk or of what a glob matches later.

    Any non-string entry is DROPPED rather than coerced -- a `None` or a dict
    that stringified would be a member nobody listed.
    """
    raw = (config or {}).get("eligible_machines") or []
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(m for m in raw if isinstance(m, str) and m.strip())


def _hold(gate, reason, gates_passed):
    return {
        "verdict": VERDICT_HOLD,
        "gate_failed": gate,
        "reason": reason,
        "gates_passed": tuple(gates_passed),
    }


def decide(config, self_machine, liveness_verdict, liveness_rc,
           claim_age_s, discriminators, t_takeover_s=None):
    """Pure decision function -- no I/O, so tests can drive every branch.

    Args:
      config:            the `reducer_promotion` config block (may be None).
      self_machine:      this box's resolved machine id (may be None).
      liveness_verdict:  worker_reducer_liveness.decide()'s `verdict` string.
      liveness_rc:       the rc that produced that verdict.
      claim_age_s:       age of the observed claim in seconds (may be None).
      discriminators:    dict keyed by DISCRIMINATORS; values True/False/None.
      t_takeover_s:      T_takeover override for tests; None reads the canonical.

    Returns {verdict, gate_failed, reason, gates_passed}. `gate_failed` is None
    only on PROMOTE.
    """
    cfg = config or {}
    passed = []

    # G1 -- master kill switch. Absent means off; only an explicit True enables.
    if cfg.get("enabled") is not True:
        return _hold("enabled",
                     "reducer_promotion.enabled is not True -- promotion is "
                     "default-off until the kill-test lane passes", passed)
    passed.append("enabled")

    # G2 -- the fence must be PROVEN live on this fleet, not merely present.
    # A non-empty stamp is written only after the kill-test lane passes; see the
    # convention for the three tests it must record.
    stamp = cfg.get("fence_verified_at")
    if not (isinstance(stamp, str) and stamp.strip()):
        return _hold("fence_verified",
                     "reducer_promotion.fence_verified_at is absent -- the "
                     "self-fence kill-tests have not been recorded as passing, "
                     "so T_stepdown is unproven and takeover is not yet legal",
                     passed)
    passed.append("fence_verified")

    # G3 -- eligibility by exact identity. An unresolvable self_machine cannot
    # match any listed member and must not be coerced into one.
    allow = eligible_machines_from_config(cfg)
    if not isinstance(self_machine, str) or not self_machine.strip():
        return _hold("eligible",
                     "this box's machine id did not resolve -- eligibility is "
                     "exact-identity and an unknown identity matches nothing",
                     passed)
    if self_machine not in allow:
        return _hold("eligible",
                     f"machine {self_machine!r} is not in "
                     f"reducer_promotion.eligible_machines ({len(allow)} listed) "
                     f"-- on-demand and laptop boxes are deliberately excluded",
                     passed)
    passed.append("eligible")

    # G4 -- the reducer must be provably NOT-LIVE. Two independent conditions,
    # and the second is the load-bearing one: the liveness module winds a worker
    # down on accumulated unverifiability too, and promoting on "I could not
    # tell" is precisely the broken-heartbeat-writer trap.
    if liveness_verdict != "wind-down":
        return _hold("liveness_decisive",
                     f"liveness verdict is {liveness_verdict!r}, not 'wind-down' "
                     f"-- the reducer is not established as gone", passed)
    if liveness_rc not in DECISIVE_RCS:
        return _hold("liveness_decisive",
                     f"liveness wound down on rc={liveness_rc}, which is "
                     f"accumulated UNVERIFIABILITY rather than decisive evidence "
                     f"of a dead reducer (decisive: rc in {DECISIVE_RCS}). "
                     f"Winding down on 'I cannot tell' is safe; promoting on it "
                     f"is split-brain", passed)
    passed.append("liveness_decisive")

    # G5 -- the lease must be past T_takeover, which is what guarantees the old
    # holder has had its full T_stepdown window to yield.
    t_takeover = t_takeover_s if t_takeover_s is not None else _load_canonical_takeover_seconds()
    if t_takeover is None:
        return _hold("claim_stale",
                     "T_takeover could not be read from the canonical reader -- "
                     "refusing to promote against an unknown lease window rather "
                     "than falling back to a hardcoded copy", passed)
    if not isinstance(claim_age_s, (int, float)) or isinstance(claim_age_s, bool):
        return _hold("claim_stale",
                     "claim age is unreadable -- an unreadable age is not a long "
                     "one", passed)
    if claim_age_s < t_takeover:
        return _hold("claim_stale",
                     f"claim age {claim_age_s:.0f}s < T_takeover {t_takeover:.0f}s "
                     f"-- the holder's stepdown window has not fully elapsed",
                     passed)
    passed.append("claim_stale")

    # G6 -- the discriminator set. Every one must be exactly True; None means
    # unmeasured, which is treated as absent, never as satisfied.
    given = discriminators if isinstance(discriminators, dict) else {}
    for name in DISCRIMINATORS:
        val = given.get(name)
        if val is True:
            continue
        state = "unmeasured" if val is None else f"{val!r}"
        return _hold("discriminators",
                     f"discriminator {name!r} is {state} -- every discriminator "
                     f"must be exactly True; an unmeasured signal is absent, not "
                     f"satisfied", passed)
    passed.append("discriminators")

    return {
        "verdict": VERDICT_PROMOTE,
        "gate_failed": None,
        "reason": (f"all {len(GATES)} gates passed: fence verified {stamp}, "
                   f"machine {self_machine} eligible, reducer decisively not-live "
                   f"(rc={liveness_rc}), claim {claim_age_s:.0f}s stale past "
                   f"T_takeover {t_takeover:.0f}s, all "
                   f"{len(DISCRIMINATORS)} discriminators true"),
        "gates_passed": tuple(passed),
    }


# --------------------------------------------------------------------------
# The DRY-RUN observation mode (). Stays adjacent to `decide()` and is
# pure for the same reason `decide()` is.
# --------------------------------------------------------------------------

# Written into a SIMULATED config only, never to disk. A real stamp is an ISO
# timestamp; this is deliberately not one, so if it ever appears in
# aspirations.yaml or in a log claiming the fence is verified, it is a bug that
# announces itself rather than a plausible-looking date nobody questions.
DRY_RUN_FENCE_SENTINEL = "DRY-RUN-SIMULATED-NEVER-A-REAL-STAMP"


def dry_run(config, self_machine, liveness_verdict, liveness_rc,
            claim_age_s, discriminators, t_takeover_s=None):
    """Evaluate G3-G6 against LIVE inputs while G1/G2 stay genuinely OFF.

    THE CIRCULARITY THIS EXISTS FOR. The kill-test lane's bar (convention § "The
    kill-test lane") is "observe an eligible worker's gates all pass" -- but
    `fence_verified_at` IS G2 and `enabled` is G1, both default-off, and G1 sits
    in FRONT of G2. Fed PERFECT observations (reducer provably gone, claim age
    999999s, every discriminator True, machine listed), `decide()` returns
    `hold / gate_failed=enabled`. The bar cannot be observed without first arming
    the flag the bar is the precondition for, so no fieldwork can ever meet it.

    The general form is worth more than this instance: when a gate's verification
    bar is stated as "observe the gated behaviour happening", check whether the
    gate itself blocks that observation -- a fail-closed gate usually does. The
    cheap test is to run the decision function with every input at its most
    favourable value; if it still refuses, the bar is unreachable by
    construction.

    THIS IS AN OBSERVATION MODE, NOT A BYPASS, and three properties enforce it:

      1. `verdict` is ALWAYS `hold`. The hypothetical answer lives in a
         SEPARATE key, `would_promote`. A caller that branches on `verdict` --
         the field every other function in this family returns -- can never be
         made to promote by this function, even by misreading it. That is the
         whole reason the result is not simply `decide()`'s dict with a flag
         added: a mistakable dry-run is worse than no dry-run (guard-1562, and
         loosening a fail-closed gate is the dangerous direction).
      2. It NEVER writes config. The armed config is a shallow COPY; the
         caller's dict is not mutated, and nothing is persisted.
      3. It is not reachable from any promotion path -- `decide()` does not call
         it, and it calls `decide()` rather than reimplementing the gates, so
         the two can never disagree about what G3-G6 mean.

    `simulated_gates` names exactly which gates were faked, so a reader can
    never mistake a simulated pass for a real one. When the live config is
    ALREADY armed, nothing needs simulating: `simulated_gates` is empty,
    `armed_for_real` is True, and the run is not a simulation at all -- it is
    what `decide()` would return right now. That case is reported rather than
    hidden, because a dry-run whose output is identical to the real decision is
    the one moment the distinction matters most.
    """
    cfg = dict(config or {})          # COPY -- property 2
    simulated = []

    if cfg.get("enabled") is not True:
        cfg["enabled"] = True
        simulated.append("enabled")

    stamp = cfg.get("fence_verified_at")
    if not (isinstance(stamp, str) and stamp.strip()):
        cfg["fence_verified_at"] = DRY_RUN_FENCE_SENTINEL
        simulated.append("fence_verified")

    inner = decide(cfg, self_machine, liveness_verdict, liveness_rc,
                   claim_age_s, discriminators, t_takeover_s)

    would = inner["verdict"] == VERDICT_PROMOTE
    sim = tuple(simulated)
    real_passed = tuple(g for g in inner["gates_passed"] if g not in sim)

    if would:
        reason = (f"WOULD PROMOTE under a hypothetical armed config. Really "
                  f"evaluated on live inputs: {', '.join(real_passed) or 'none'}. "
                  f"Simulated (NOT satisfied in reality): "
                  f"{', '.join(sim) or 'none -- config is already armed'}. "
                  f"No promotion occurred and none can occur from this call.")
    else:
        reason = (f"would NOT promote: {inner['reason']} "
                  f"(really evaluated: {', '.join(real_passed) or 'none'}; "
                  f"simulated: {', '.join(sim) or 'none -- config is already armed'})")

    return {
        "dry_run": True,
        "verdict": VERDICT_HOLD,      # ALWAYS -- property 1
        "would_promote": would,
        "gate_failed": inner["gate_failed"],
        "simulated_gates": sim,
        "real_gates_evaluated": real_passed,
        "armed_for_real": not sim,
        "reason": reason,
    }


# --------------------------------------------------------------------------
# Measuring G6. `decide()` above TAKES the discriminators and never measures
# them; for a while nothing else did either, so the gate the whole safety
# argument rests on was unmeasurable (). This section is that
# measurement. It stays BELOW `decide()` and does I/O, so `decide()` remains
# pure and every branch above is still drivable from a dict.
# --------------------------------------------------------------------------

def _load_worker_stall():
    """The carrier enumerator, from the module that already owns it.

    guard-2783 / the no-transcription contract: `worker_stall.enumerate_carriers`
    already ranks authoritative-over-mirror, already reports its own
    completeness, and already carries the g-306-247 read-error accounting. A
    second enumerator here would be a second predicate for one question, and the
    two would disagree the first time either is fixed.

    Returns None when the module cannot be loaded, which callers must treat as
    "cannot measure D3" -> None -> HOLD. It deliberately does NOT fall back to a
    local glob: a private copy of the read path is the exact drift this reuse
    exists to prevent (rb-313).
    """
    path = Path(__file__).resolve().parent / "worker_stall.py"
    try:
        spec = importlib.util.spec_from_file_location("_worker_stall_for_promo", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def _self_carrier_local(agents_root, agent, sid):
    """THIS Body's own carrier, read from the LOCAL path. Never the store.

    This is the `self` half of the union and the reason the union exists. The
    carrier is written locally by heartbeat-tick.sh and PUSHED to the store
    afterwards, so between the write and the push an authoritative read cannot
    see it. A store-only D3 would then find zero fresh carriers -- including
    this Body's own, which is fresh by construction because this very process
    just ticked it -- and report `only_fresh_carrier_is_mine = False`
    BY ABSENCE. Reading self locally is not a shortcut; it is the only read
    that is guaranteed to be able to answer.
    """
    p = Path(agents_root) / str(agent) / "session" / f"body-heartbeat-{sid}.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def measure_only_fresh_carrier_is_mine(
    agents_root, self_agent, self_sid,
    claim_read_authoritative=None, fresh_minutes=None, now=None,
):
    """D3, measured as a UNION: the STORE for siblings, the LOCAL file for self.

    Returns `(value, evidence)`. `value` is True / False / None, where None means
    UNMEASURED and `decide()` treats it as absent rather than satisfied.

    WHY A UNION AND NOT EITHER READ ALONE. The two obvious implementations are
    unsound in OPPOSITE directions, which is what makes picking one feel safe:

      LOCAL-only  cannot see a sibling's carrier on another box. Under own-cloud
                  the local tree is a read-through cache (guard-980), so a
                  carrier this box never pulled simply is not here -- and D3
                  would read True while a live sibling holds one. That is the
                  dual-reducer race G6 exists to prevent, i.e. the DANGEROUS
                  direction.
      STORE-only  can miss this Body's OWN carrier in the window between the
                  local write and the push, so D3 reads False BY ABSENCE. Safe
                  (it only ever refuses a legal promotion) but it makes the gate
                  unreliable rather than conservative, and an unreliable gate
                  gets overridden.

    D3 THEREFORE DEPENDS ON D2, and this is the part that is easy to get wrong
    when reading the discriminator list as three independent booleans. The
    sibling half is only trustworthy when the authoritative store answered. When
    it did not, a local-mirror enumeration reports "no other fresh carrier" for
    a reason that has nothing to do with the fleet, so D3 must be None -- never
    True. Both signals are required: this function's own enumeration must have
    read `authoritative` AND `complete`, and the caller's D2 must not be False.

    LIVENESS-FILTER THE POPULATION, AND REPORT IT. Measured 2026-08-16 (zeta,
    cc-02): 24 carriers across 5 agents, 7 fresh across 7 distinct hosts, 17
    stale -- every one of the 17 belonging to a DEAD session, some 10+ days old.
    So ~71% of the carrier population is dead-session residue, and a reader that
    does not filter by freshness concludes "nothing is fresh" from a healthy
    fleet. `evidence` therefore always carries the scanned population beside the
    verdict: a bare boolean here is unfalsifiable (rb-245, guard-1922).

    The freshness window is `worker_stall.DEFAULT_STALE_MINUTES`, deliberately
    NOT a new constant (guard-2783 -- one predicate per question). Note the
    fail-safe direction runs with reuse rather than against it: a WIDER window
    over-counts fresh siblings and refuses a legal promotion, while a narrower
    one under-counts them and promotes into a live sibling. If this ever needs
    its own window, it may only ever be WIDER than the stall probe's.

    RESIDUAL RISK THE UNION DOES NOT CLOSE, and it runs in the DANGEROUS
    direction, so it is stated rather than omitted (guard-1760). guard-3917
    failure mode (2): a same-box FORKED Body writes into an isolated worktree
    the sync layer never sees, so its carrier never reaches the store at all.
    A sibling measuring D3 would then find no fresh carrier for it and could
    read True while that Body is live. The union does not fix this -- the local
    half covers only SELF, and the store half cannot see what was never pushed.
    What bounds it is G3: `eligible_machines` is a hand-listed set of long-lived
    fleet boxes, and a forked-worktree Body on a listed box shares that box's
    agent dir, so its carrier IS on the local path of any promoter running
    there. The exposure is a forked Body on a box that is not the promoter's.
    Accepted, not eliminated; closing it needs the fork to publish a carrier the
    store can see.
    """
    ev = {
        "carriers_scanned": 0,
        "fresh": [],
        "stale": 0,
        "unreadable": 0,
        "read_via": "none",
        "enumeration_complete": False,
        "self_seen_in_store": False,
        "self_carrier_fresh": None,
        "fresh_minutes": None,
        "reason": None,
    }

    ws = _load_worker_stall()
    if ws is None:
        ev["reason"] = ("worker_stall could not be loaded, so the sibling half of "
                        "the union is unavailable -- refusing to measure D3 from a "
                        "private copy of the read path")
        return None, ev

    window = float(fresh_minutes if fresh_minutes is not None else ws.DEFAULT_STALE_MINUTES)
    ev["fresh_minutes"] = window
    now = now or dt.datetime.now()

    rows, meta = ws.enumerate_carriers(Path(agents_root))
    ev["read_via"] = meta.get("read_via")
    ev["enumeration_complete"] = bool(meta.get("complete"))
    ev["agents_enumerated"] = meta.get("agents_enumerated")
    ev["carrier_read_errors"] = meta.get("carrier_read_errors")
    ev["carriers_scanned"] = len(rows)

    # SELF, from the local file — the half the store cannot be relied on for.
    self_doc = _self_carrier_local(agents_root, self_agent, self_sid)
    self_ts = ws._parse_iso(str((self_doc or {}).get("ts") or "")) if self_doc else None
    self_fresh = (
        self_ts is not None
        and (now - self_ts).total_seconds() <= window * 60.0
    )
    ev["self_carrier_fresh"] = bool(self_fresh)

    # SIBLINGS, from the store. Rows whose sid is ours are the store's copy of
    # our own carrier — counted as `self_seen_in_store` for evidence, never as a
    # sibling, or this Body would race itself.
    for row in rows:
        if str(row.get("sid")) == str(self_sid):
            ev["self_seen_in_store"] = True
            continue
        ts = ws._parse_iso(str((row.get("doc") or {}).get("ts") or ""))
        if ts is None:
            ev["unreadable"] += 1
            continue
        if (now - ts).total_seconds() <= window * 60.0:
            ev["fresh"].append({
                "agent": row.get("agent"),
                "sid": row.get("sid"),
                "host": (row.get("doc") or {}).get("host"),
                "age_s": round((now - ts).total_seconds()),
            })
        else:
            ev["stale"] += 1

    # THE D2 DEPENDENCY, enforced after the scan so the evidence is populated
    # either way — a reader must be able to see WHAT was scanned even when the
    # verdict is None.
    if claim_read_authoritative is False:
        ev["reason"] = ("D2 claim_read_authoritative is False, and D3's sibling "
                        "half needs the same authoritative store -- D3 is "
                        "unmeasured, not satisfied")
        return None, ev
    if meta.get("read_via") != "authoritative" or not meta.get("complete"):
        ev["reason"] = (
            f"carrier enumeration read_via={meta.get('read_via')!r} "
            f"complete={bool(meta.get('complete'))} "
            f"({meta.get('reason')}) -- a mirror read cannot see a sibling "
            f"carrier this box never pulled, so 'no other fresh carrier' would "
            f"be an artifact of the read path")
        return None, ev

    # An UNREADABLE sibling carrier is a carrier whose freshness is unknown, and
    # unknown is not absent. Refusing here is the same discipline as `None`
    # meaning unmeasured in `decide()` — treating it as stale would let a
    # corrupt-but-live sibling be promoted over.
    if ev["unreadable"]:
        ev["reason"] = (f"{ev['unreadable']} sibling carrier(s) had no parseable "
                        f"ts -- unknown freshness is not absence")
        return None, ev

    if not self_fresh:
        ev["reason"] = ("this Body's own carrier is absent or stale locally, so "
                        "'the only fresh carrier is mine' has no subject")
        return False, ev

    if ev["fresh"]:
        ev["reason"] = (f"{len(ev['fresh'])} other Body/Bodies hold a fresh carrier "
                        f"(<= {window:.0f} min) -- promoting would race a live "
                        f"sibling")
        return False, ev

    ev["reason"] = (f"self carrier fresh; 0 of {ev['carriers_scanned']} scanned "
                    f"carriers are a fresh sibling ({ev['stale']} stale) at a "
                    f"{window:.0f} min window, read authoritatively across "
                    f"{ev.get('agents_enumerated')} agent(s)")
    return True, ev


def measure_discriminators(
    agents_root, self_agent, self_sid,
    peers_alive_from_this_box=None, claim_read_authoritative=None,
    fresh_minutes=None, now=None,
):
    """Assemble the G6 dict `decide()` expects, plus the evidence behind it.

    D1 and D2 are INPUTS, not measured here, and that is deliberate rather than
    unfinished. D1 is `liveness_check.py`'s question ("do peers read alive from
    this box?") and D2 is a property of the CLAIM read performed by
    `worker_reducer_liveness`. Measuring either here would be a second predicate
    for a question another module already owns (guard-2783). Passing None for
    either leaves it unmeasured, which `decide()` refuses at G6 — the correct
    outcome for a caller that has not done the other two measurements.

    Only D3 is measured, because only D3 had no owner at all.
    """
    d3, ev = measure_only_fresh_carrier_is_mine(
        agents_root, self_agent, self_sid,
        claim_read_authoritative=claim_read_authoritative,
        fresh_minutes=fresh_minutes, now=now,
    )
    return (
        {
            "peers_alive_from_this_box": peers_alive_from_this_box,
            "claim_read_authoritative": claim_read_authoritative,
            "only_fresh_carrier_is_mine": d3,
        },
        {"only_fresh_carrier_is_mine": ev},
    )


def load_config(config_path):
    """The `reducer_promotion` block from aspirations.yaml, or None.

    No hardcoded default on the read path: a missing or unreadable block returns
    None so `decide` refuses at G1 rather than silently promoting on a magic
    default (the rb-313 rule the surrounding config block already follows).
    """
    try:
        import yaml
        with open(config_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:
        return None
    block = data.get("reducer_promotion")
    return block if isinstance(block, dict) else None


def self_machine_id():
    """This box's machine id, by the same env the fleet already keys on."""
    val = (os.environ.get("MACHINE_ID") or "").strip()
    return val or None
