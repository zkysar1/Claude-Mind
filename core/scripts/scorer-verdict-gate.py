#!/usr/bin/env python3
"""scorer-verdict-gate.py — Scorer Sovereignty Layer B claim chokepoint ().

The claim wrapper (aspirations-claim.sh) calls this gate BEFORE the daemon
claim POST. It reads the per-agent scorer-verdict sidecar written by
goal-selector.py (write_scorer_verdict) and refuses an UNSANCTIONED divergence
from the scorer's fresh top pick: claiming a goal that is NOT top_goal_id
requires a --deviation <code> drawn from a CLOSED enum, each mapping to a
sanctioned divergence path already present in aspirations-select.

Design precedents:
  - capability-gate.py  — closed-enum match + audit-on-match, educational deny.
  - schedule-wakeup-gate.py — fail-closed PreToolUse chokepoint with a deny
    message that teaches the correct pattern.
  - This gate is the STRUCTURAL half of Scorer Sovereignty; zeta shipped the
    ADVISORY half (g-115-2805, emit_directive_honor_banner). The advisory
    banner nudges; this gate refuses.

FAIL-OPEN is the load-bearing safety property: a MISSING, MALFORMED, or STALE
verdict all ALLOW without validation. A broken or slow selector must never
wedge claiming — the worst case of a verdict-write failure is that the gate
simply does not run this iteration. Only a FRESH verdict carrying a known
top_goal_id can produce a deny.

Verdict schema (written by goal-selector.write_scorer_verdict):
  {"top_goal_id": str, "top_score": float, "ts": "YYYY-MM-DDTHH:MM:SS",
   "top_5": [{"goal_id": str, "score": float}, ...]}
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Closed set of sanctioned deviation codes. A claim of a goal OTHER than the
# scorer's fresh top pick MUST name exactly one. Each corresponds to a
# divergence path aspirations-select already sanctions:
VALID_DEVIATION_CODES = (
    "first-action",       # first-iteration handoff override (select Phase 2 First-Action)
    "partner-claim",      # scorer top is partner.in_flight — dropped by the partner filter
    "guardrail-forbids",  # a cross-cutting guardrail forbids the top goal now (Phase 2.27)
    "self-abstention",    # capability mismatch — abstained past the top goal (Phase 2.55)
    "blocker-gate",       # top goal blocked by infra / known-blocker probe (Phase 2.5b)
    "meta-tiebreaker",    # meta-strategy re-sort/adjustment chose a different top (Phase 2.05/2.07)
    "precondition-fail",  # top goal's string/structured precondition not met (Precondition Gate)
    "cross-agent",        # deliberately claiming a cross-lane / foreign-world goal
    "no-goals-rebound",   # verdict names goals since gone; rebound to a live candidate
    "force-override",     # explicit force escape hatch — audited, last resort
    "always-run-lane",    # always-run-lane.sh pinned an opted-in, due, candidate goal ()
)

FRESHNESS_MINUTES = 10
TS_FMT = "%Y-%m-%dT%H:%M:%S"

#  — gate-telemetry identity. MUST match an `id` in
# core/config/gates.yaml or gate-retirement-eval / gate-stats cannot see this
# gate's firings (see _gate_log.log's docstring, and the `instrumented` field).
GATE_ID = "scorer-sovereignty-claim-gate"

# Every branch of _classify maps to exactly one _gate_log decision, chosen by
# the branch's OBSERVABLE CONTROL-FLOW EFFECT AT THE CALLER (guard-1743), not
# by local intent. The caller is aspirations-claim.sh: `exit 2` aborts the
# claim; every other rc proceeds to the daemon claim POST.
#   block     -> caller aborts the claim (rc 2)
#   pass      -> gate validated the claim and approved it (claim proceeds)
#   override  -> gate validated and allowed a NAMED bypass (--deviation <code>)
#   fail_open -> gate could not validate at all; claim proceeds unchecked
#
# WHY EVERY BRANCH LOGS (guard-502; guard-2293 names the defect): before this,
# the ONLY branch leaving a trace was the sanctioned deviation, written to the
# per-agent execution diary. An escape-path-only record is not gate telemetry —
# blocks that STOOD and every ordinary PASS were invisible, so this gate's
# false-positive ratio count(override)/(block+override) was unmeasurable.
# Measured 2026-09-06: the diary carrier does not aggregate across Bodies
# either — alpha's store copy held 0 of its 70 scorer_override rows while being
# FRESHER than the local copy (a cross-Body last-writer-wins clobber), so 82%
# of the fleet's abstention signal was invisible to the only fleet-wide reader.
DECISION_BY_PATH = {
    "no_verdict":             "fail_open",
    "malformed_verdict":      "fail_open",
    "stale_verdict":          "fail_open",
    "path_resolution_failed": "fail_open",
    "top_pick_match":         "pass",
    "unsanctioned_deviation": "block",
    "sanctioned_deviation":   "override",
}


def _parse_ts(raw):
    """Parse the naive verdict timestamp. Returns a datetime, or None if absent
    or unparseable (which the caller treats as stale -> fail-open)."""
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw).strip(), TS_FMT)
    except (ValueError, TypeError):
        return None


def _deny_message(claimed, top, code):
    enum = " ".join(VALID_DEVIATION_CODES)
    if code:
        head = (f"scorer-sovereignty: claiming {claimed} diverges from the scorer's "
                f"top pick {top}, and --deviation '{code}' is not a recognized code.")
    else:
        head = (f"scorer-sovereignty: claiming {claimed} diverges from the scorer's "
                f"top pick {top} without a --deviation code.")
    return (
        f"{head}\n"
        f"A claim that is NOT the scorer's top pick requires --deviation <code> "
        f"from the closed set:\n  {enum}\n"
        f"Pick the code matching WHY you diverge (see the sanctioned-deviation "
        f"phases in aspirations-select), or claim the top pick {top} instead.\n"
        f"If {claimed} genuinely SHOULD rank first, the fix is to tune the scoring "
        f"weights in meta/goal-selection-strategy.yaml — change the scorer, do not "
        f"route around it."
    )


def _classify(verdict, claimed_goal_id, deviation_code, now,
              freshness_minutes=FRESHNESS_MINUTES):
    """Pure branch classifier (no I/O) — the SINGLE source of branch truth.

    Returns (decision_path, exit_code, message, override_event):
      decision_path  : stable label, a key of DECISION_BY_PATH — the gate's
                       telemetry discriminator (guard-502: every branch emits a
                       UNIQUE label, so no two branches are conflated in the
                       firing log)
      exit_code      : 0 = allow, 2 = deny
      message        : educational deny text on exit 2, else ""
      override_event : dict to record to the diary on a SANCTIONED deviation,
                       else None

    FAIL-OPEN: a non-dict/missing verdict, a verdict with no top_goal_id, or a
    stale/unparseable-timestamp verdict all allow. Only a FRESH verdict with a
    known top pick can deny.

    `evaluate` below is a 3-tuple facade over this function; the branch logic
    lives here ONCE so the telemetry label and the decision cannot drift apart.
    """
    if not isinstance(verdict, dict):
        return "no_verdict", 0, "", None
    top = str(verdict.get("top_goal_id") or "").strip()
    if not top:
        return "malformed_verdict", 0, "", None  # no top pick -> fail-open

    ts = _parse_ts(verdict.get("ts"))
    if ts is None or (now - ts) > timedelta(minutes=freshness_minutes):
        return "stale_verdict", 0, "", None  # stale/unparseable -> fail-open

    claimed = str(claimed_goal_id or "").strip()
    if claimed == top:
        # Happy path: claiming the scorer's top pick, no flag needed. This is
        # the branch guard-2293 names — it used to leave NO trace at all, which
        # is precisely what made this gate's FP ratio unmeasurable.
        return "top_pick_match", 0, "", None

    code = str(deviation_code or "").strip()
    if not code or code not in VALID_DEVIATION_CODES:
        return "unsanctioned_deviation", 2, _deny_message(claimed, top, code), None

    # Sanctioned divergence — allow, and hand back the event to record for the
    # Layer C audit (filterable via `execution-diary read --entry-type scorer_override`).
    return ("sanctioned_deviation", 0, "",
            {"claimed": claimed, "scorer_top": top, "code": code})


def evaluate(verdict, claimed_goal_id, deviation_code, now,
             freshness_minutes=FRESHNESS_MINUTES):
    """Pure decision core (no I/O) — 3-tuple facade over `_classify`.

    Returns (exit_code, message, override_event). Kept at three elements
    deliberately: this is the long-standing public shape with existing call
    sites and tests, and widening it would be churn with no behavioural gain.
    Callers that need the telemetry label call `_classify` directly.
    """
    _path, exit_code, message, override_event = _classify(
        verdict, claimed_goal_id, deviation_code, now, freshness_minutes)
    return exit_code, message, override_event


def _load_verdict(path):
    """Read + parse the verdict sidecar. Any error -> None (fail-open)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _log_override(event, agent):
    """Best-effort scorer_override diary entry on a sanctioned deviation.
    FAIL-OPEN — a diary-log failure MUST NOT block the (already sanctioned)
    claim. execution-diary.sh is direct-python (local file append), so this is
    cheap and never spawns the daemon."""
    try:
        diary_sh = Path(__file__).resolve().parent / "execution-diary.sh"
        content = (f"scorer-override: claimed {event['claimed']} over scorer top "
                   f"{event['scorer_top']} (deviation={event['code']})")
        payload = json.dumps({
            "entry_type": "scorer_override",
            "content": content,
            "goal_id": event["claimed"],
        })
        env = dict(os.environ)
        if agent:
            env["MIND_AGENT"] = agent
        from _runtime_bash import bash_cmd  # guard-580 + guard-581
        subprocess.run(
            bash_cmd(diary_sh, "append"),
            input=payload, text=True, capture_output=True, timeout=15, env=env,
        )
    except Exception:
        pass  # audit log is best-effort; the deviation is already sanctioned


def _log_gate_firing(decision_path, agent, claimed, top, code):
    """Best-effort gate-telemetry firing (). NEVER raises.

    Routes through the shared `_gate_log` component rather than a gate-local
    file, so every firing reaches the gate-firings store and is flushed
    fleet-wide by gate-firings-flush.sh — the aggregating carrier the per-agent
    execution diary is not.

    NOTE for anyone verifying this on an own-cloud box: _gate_log routes the hot
    path to a MACHINE-LOCAL SPOOL, so reading the shared store directly yields a
    false "gate telemetry is dead" (guard-4040). Verify via the spool, or after
    a gate-firings-flush.sh run.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import _gate_log
        _gate_log.log(
            GATE_ID,
            DECISION_BY_PATH.get(decision_path, "fail_open"),
            caller="aspirations-claim.sh",
            trigger_matched=decision_path,
            # `extra` and NOT `payload`: _gate_log stores `extra` VERBATIM but
            # reduces `payload` to a `payload_hash`, so anything a consumer must
            # READ has to travel here. `scorer_top` is the whole point — it is
            # the goal the selector ranked first and this Body did not take, and
            # a hash of it answers no question anyone will ask.
            extra={
                "decision_path": decision_path,
                "claimed": claimed,
                "scorer_top": top,
                "deviation": code or None,
            },
            override_reason=(
                code or None) if decision_path == "sanctioned_deviation" else None,
            agent_name=agent or None,
        )
    except Exception:
        pass  # telemetry is best-effort; it must never affect the claim


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Scorer Sovereignty Layer B claim gate (g-115-2812)")
    ap.add_argument("--agent", required=True)
    ap.add_argument("--goal-id", required=True)
    ap.add_argument("--deviation", default="")
    ap.add_argument("--verdict-file", default="",
                    help="explicit verdict path (tests); default resolves via _paths")
    args = ap.parse_args(argv)

    # Resolve the verdict sidecar. --agent is authoritative for path resolution:
    # seed MIND_AGENT so the lazy _paths import never hits the unset-agent exit.
    if args.verdict_file:
        verdict_path = Path(args.verdict_file)
    else:
        os.environ.setdefault("MIND_AGENT", args.agent)
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from _paths import agent_state_dir  # lazy: only the CLI path needs it (SSOT for AGENTS_PARENT_DIR)
            verdict_path = agent_state_dir(args.agent) / "scorer-verdict.json"
        except Exception:
            # cannot resolve the path -> fail-open (allow). Still a BRANCH, so
            # it emits its own firing (guard-502) — an unresolvable verdict path
            # is exactly the silent degradation telemetry should surface.
            _log_gate_firing("path_resolution_failed", args.agent,
                             args.goal_id, None, args.deviation)
            return 0

    verdict = _load_verdict(verdict_path)
    decision_path, exit_code, message, override_event = _classify(
        verdict, args.goal_id, args.deviation, datetime.now())

    _log_gate_firing(
        decision_path, args.agent, args.goal_id,
        verdict.get("top_goal_id") if isinstance(verdict, dict) else None,
        args.deviation)

    if exit_code == 2:
        print(message, file=sys.stderr)
        return 2
    if override_event is not None:
        _log_override(override_event, args.agent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
