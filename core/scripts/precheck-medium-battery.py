"""precheck-medium-battery — one call running every MEDIUM-tier precheck lane
under the budget meter, printing FINDINGS ONLY (g-115-7847).

WHY THIS EXISTS, AND WHY IT IS A SCRIPT RATHER THAN A BETTER SENTENCE.
`iteration-open.sh` (2026-08-17, g-115-6468) took over loop entry and dispatches
the always-run tier. Its terminal line -- the LAST thing the reducer reads before
acting -- says "dispose the findings above, then claim from SELECTION and enter
execution". It never mentions the medium/deferrable tail, and the tail's own
phase bodies live ~2,700 lines below in a SKILL.md larger than the context
window. So from the day that script landed, the tail stopped running:

    cc-04 (alpha): last precheck with sweeps_ran > 6 was 2026-08-17T11:34 -- the
                   SAME DAY iteration-open.py landed. 208h dark.
    cc-02 (zeta):  last was 2026-08-22T05:32. 94.3h dark. Over the final 24h,
                   sweeps_ran == always_run_count on EVERY one of 35 rows.

Two agents, two boxes, one cause: fleet-wide, on evidence.

THE FIX THAT WAS PROPOSED AND WHY IT WOULD NOT HAVE WORKED. The originating goal
read "the fix is one line in iteration-open.sh's imperative, not a rewiring."
guard-399 amendment (2) falsifies that, measured: "MECHANIZING PROSE INTO A
`Bash:` LINE IS NOT A FIX ... prose and a `Bash:` line are the SAME ENFORCEMENT
CLASS: both require the model to elect to run them. Changing the FORM of an
instruction does not change WHO executes it." aspirations-precheck/SKILL.md
Step 0-open ALREADY says "resume at the first medium/deferrable sweep", and that
sentence is what was being skipped. A second sentence in a different file would
have been the same sentence. The operative test is not "is the invocation a
command?" but "WHO executes it -- a script, or a model reading a file?", and the
remedy guard-399 names is to move the call into a script the flow already runs.
That is this battery: iteration-open.sh runs it unconditionally, so no election
happens anywhere.

WHAT IT COSTS. All seven lanes, measured end-to-end on cc-02 2026-08-26:
~16.1 s (monitor-stale 0.1, precheck-eval 9.0, blocker-recheck 0.6,
defer-recheck 2.2, precondition-defer-recheck 2.3, starvation 1.9). Comfortably
inside iteration-open's 180 s per-stage budget.

THE DROP DECISION IS HONORED HERE, UNLIKE THE ALWAYS-RUN BATTERY. Always-run
lanes never drop, so its `_meter_check` return is pure telemetry. Medium lanes
CAN drop (`zone_drop_rules`), so this battery must obey the answer it gets. It
does not hardcode "medium never drops today" -- that is true of the current
config and is exactly the assumption that rots. A dropped lane is reported in
`dropped[]`, never silently omitted (guard-1760).

WHY MEDIUM ONLY, AND WHY THAT IS A BOUNDARY RATHER THAN AN OMISSION. The
deferrable tier contains 0.5b.6 parent-supersession-sweep, 0.5b.7
unblock-parent-status-sweep and 0.5b.8 routing-audit-target-status-sweep, which
guard-4033 measured destroying a 5,748 B outcome_note. TWO SEPARATE DEFENSES ARE
AT ISSUE AND THEY HAVE DIFFERENT STATUSES. The first draft of this paragraph
conflated them and reached the right exclusion via two false reasons -- caught by
fresh-eyes review 2026-08-26 under guard-1685 (a TRUE measurement of the WRONG
object refutes a TRUE claim about the right one, and reads exactly like staleness):

  * LOST-UPDATE (do not write to a goal another agent just closed) -- SHIPPED.
    All three route through `_shared_stale_candidate_reason`, re-asserting the
    candidate predicate against the STORE OF RECORD, and
    tests/test_unblock_parent_lost_update_guard.py pins it. So "they bare-replace
    WITHOUT CHECKING claimed_by / in-progress" is FALSE as measured, and
    g-115-6332 is NOT an open blocker -- it is the goal that shipped this guard.
  * NOTE-PRESERVATION (when the write DOES proceed, keep the existing note) --
    ABSENT in all three. Measured `_compose_note` occurrences 0 / 0 / 0 against
    monitor-stale-check's 2; parent-supersession-sweep.py:405 still builds a
    ~50-char f-string and hands it straight to `update-goal ... outcome_note`,
    which REPLACES (guard-1691 / guard-3626). THIS is guard-4033's actual claim
    and it STANDS.

The exclusion therefore holds on the second bullet alone: wiring three
bare-replacing sweeps to fire unconditionally from every agent's loop entry
would multiply that blast radius. Medium excludes all three.

MEDIUM IS NOT OUTCOME_NOTE-FREE -- do not read it that way when adding a lane.
`monitor-stale-check`, registered below, writes outcome_note. It is safe because
g-115-6415 gave it BOTH defenses (store-of-record re-read AND `_compose_note`
preservation), not because the medium tier excludes writers. The admission
predicate for a new lane is "does it bare-replace a field another agent owns?",
never "is it in the medium tier?".

`_NOT_COVERED` names the deferrable tier in every report so "7 lanes" is never
read as "the whole tail".

Output (guard-424 fail-loud-on-stderr; guard-614 structured on EVERY exit path):
  default -- one human line per lane WITH findings, then a summary:
      > FINDING: <lane> (phase <phase>) <detail>
      [medium-battery] 2 finding / 7 lanes (mode=apply, completeness=complete)
  --json  -- {checked_at, mode, status, completeness, lanes_registered,
              findings:[...], blind:[...], dropped:[...], uncovered:[...],
              executed:[...], error?}

Fail-open: any error prints the structured report (also to stderr) and exits 0.
The battery must never block the loop -- an entry gate that can refuse entry is
worse than the drift it corrects.

Invocation: `bash core/scripts/precheck-medium-battery.sh [--apply] [--json]`.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

# precheck-eval run-all is the slowest registered lane at 9.0 s measured; 120 s
# leaves headroom for a cold daemon without letting one wedged lane hold loop
# entry open. Mirrors the always-run battery deliberately.
_LANE_TIMEOUT_S = 120
_METER = "aspirations-precheck-budget-meter.sh"


# --- the registry -----------------------------------------------------------
# INLINE ON PURPOSE, same reasoning as the sibling always-run battery: exactly
# one consumer, so a registry module would be the single-use abstraction
# implementation-discipline.md rule 3 forbids.
#
# `finds` semantics are inherited from precheck-always-run-battery.py verbatim so
# a reader of one can read the other:
#   counts -- int key, >0 is a finding
#   lists  -- list key, non-empty is a finding
#   false  -- bool key, FALSE is a finding
# Every lane additionally treats a non-empty `failed` list as BOTH finding and
# error, so a lane that half-worked is never reported clean.
#
# `meter_name` is a SEPARATE FIELD, never derived from `script`. The meter's
# sweep_tier() case arm keys on these exact strings; a script stem that missed an
# arm would WARN-default to `medium` -- which happens to be right for this
# battery and catastrophically wrong for the always-run one (). Keeping
# the field explicit means the two batteries stay readable against each other.
#
# `variants` exists for ONE lane: aspirations-recover-recurring takes --source
# and the tier table calls it twice (world, then agent). Modelling that as two
# registry rows would double-count the lane in `lanes_registered` and meter it
# twice under one name; modelling it as an arg list keeps one lane, one meter
# decision, and both invocations.
LANES = (
    {
        "name": "aspirations-recover-recurring",
        "phase": "0",
        "meter_name": "aspirations-recover-recurring",
        "script": "aspirations-recover-recurring.sh",
        "variants": (("--source", "world"), ("--source", "agent")),
        "apply_flag": False,   # no --apply; it recovers unconditionally
        "finds": {"counts": ("recovered",), "lists": (), "false": ()},
    },
    {
        "name": "monitor-stale-check",
        "phase": "0",
        "meter_name": "monitor-stale-check",
        "script": "monitor-stale-check.sh",
        "apply_flag": True,
        # `skipped` is NOT a finding: "no_current_run_id" is this lane's normal
        # quiet state on a box with no active monitor run, and firing on it would
        # make the battery noisy every iteration. completed_count/candidates are
        # the keys that mean work happened or is waiting.
        "finds": {
            "counts": ("completed_count",),
            "lists": ("candidates",),
            "false": (),
        },
    },
    {
        "name": "precheck-eval",
        "phase": "0.5.0",
        "meter_name": "precheck-eval",
        "script": "precheck-eval.sh",
        # Subcommand REQUIRED -- a bare call exits 2 (tier table, ).
        "extra_args": ("run-all",),
        "apply_flag": False,
        "finds": {"counts": (), "lists": ("flags",), "false": ()},
    },
    {
        "name": "blocker-recheck",
        "phase": "0.5b.0.5",
        "meter_name": "blocker-recheck",
        "script": "blocker-recheck.sh",
        # Resolved from config, not pinned -- see _blocker_age_hours().
        "dynamic_args": lambda: ("--max-age-hours", _blocker_age_hours()),
        "apply_flag": True,
        # NOT total_blockers: that counts rows in scope, not rows needing action,
        # so it fires every iteration (the guard-4093 lesson the always-run
        # battery learned on user-blocker-escalation-check's `eligible`).
        "finds": {"counts": ("matches_found", "cleared"), "lists": (), "false": ()},
    },
    {
        "name": "defer-recheck",
        "phase": "0.5b.4",
        "meter_name": "defer-recheck",
        "script": "defer-recheck.sh",
        "extra_args": ("--max-age-hours", "2"),
        "apply_flag": True,
        # NOT `details`: measured 20,170 B and always non-empty -- it is the
        # per-goal working set, not a finding. `would_clear` is the actionable
        # half; `eligible` is scope.
        "finds": {"counts": ("cleared",), "lists": ("would_clear",), "false": ()},
    },
    {
        "name": "precondition-defer-recheck",
        "phase": "0.5b.3",
        "meter_name": "precondition-defer-recheck",
        "script": "precondition-defer-recheck.sh",
        "extra_args": ("--max-age-hours", "2"),
        "apply_flag": True,
        # Same shape as defer-recheck, same reason for excluding `details`
        # (29,983 B measured). NOTE: this lane skips ~97% of its own eligible
        # population as free-form prose defers (measured 102 of 105 on cc-02).
        # That is L2 evaluability, owned by  -- running the lane does
        # not fix it, and a green report here must not be read as "defers clear".
        "finds": {"counts": ("cleared",), "lists": ("would_clear",), "false": ()},
    },
    {
        "name": "recurring-starvation-check",
        "phase": "0.5c.1",
        "meter_name": "recurring-starvation-check",
        "script": "recurring-starvation-check.sh",
        # --output json is REQUIRED: this lane's default is human text, which the
        # json.loads below would classify BLIND. Deliberately NOT tier `deferrable`
        # despite sitting among them -- it exists because a 5-day recurring blind
        # spot went unnoticed ().
        "extra_args": ("--output", "json", "--max-file", "1"),
        "apply_flag": True,
        "finds": {
            "counts": ("file_failures",),
            "lists": ("starved",),
            "false": (),
        },
    },
)

# The tail rows this battery deliberately does NOT run. Named in every report so
# "7 lanes" is never read as "the medium/deferrable tail is covered".
# NO SPELLED-OUT COUNT HERE. The first draft of this tuple said "20 lanes" when
# the tier table held 26 — stale before it was ever committed, which is precisely
# the failure precheck-always-run-battery.py's docstring records ("seven hardcoded
# 'five lanes' claims went stale the moment a sixth was registered ... no test can
# pin a sentence"). `iteration-open.sh --dry-run` prints the live number.
_NOT_COVERED = (
    ("deferrable tier", "not yet wired — see `iteration-open.sh --dry-run` for the live count"),
    (
        "0.5b.6 / 0.5b.7 / 0.5b.8",
        "outcome_note clobber risk under unconditional dispatch (guard-4033); "
        "they bare-REPLACE outcome_note with no `_compose_note` "
        "preservation (measured 0/0/0, 2026-08-26). The lost-update half "
        "shipped under g-115-6332; the preservation half has no owner yet",
    ),
)


def _now_iso() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _blocker_age_hours() -> str:
    """`proactive_escalation.blocker_age_hours` from aspirations.yaml.

    Read rather than hardcoded because the tier table's invocation for this lane
    is literally `--max-age-hours <config.proactive_escalation.blocker_age_hours>`
    -- a battery that pinned its own number would silently diverge from the
    documented behaviour the moment anyone tuned the config, and in the wrong
    direction: the config is 2, so a hardcoded 24 would have re-checked only
    blockers twelve times older than intended while every doc still said 2.
    Caught pre-merge 2026-08-26 by diffing the row against the config
    (communication-clarity rule 5, single source of truth).

    Fail-safe to the config's own default on any read error -- this runs at loop
    entry and must never raise.
    """
    try:
        import yaml

        cfg = yaml.safe_load(
            (PROJECT_ROOT / "core" / "config" / "aspirations.yaml").read_text(
                encoding="utf-8"
            )
        ) or {}
        v = (cfg.get("proactive_escalation") or {}).get("blocker_age_hours")
        if isinstance(v, (int, float)) and v > 0:
            return str(int(v))
    except Exception:
        pass
    return "2"


def _run_bash(argv, timeout):
    """Run a core/scripts bash script. Return (rc, stdout, err_or_None).

    err set == the lane could not be RUN at all (timeout / spawn failure). That
    is a BLIND lane, not a clean one -- the caller must not fold it into a zero.
    """
    from _runtime_bash import bash_cmd  # guard-580 + guard-581

    full = bash_cmd(SCRIPT_DIR / argv[0], *argv[1:])
    try:
        r = subprocess.run(
            full, cwd=str(PROJECT_ROOT),
            capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode, r.stdout, None
    except Exception as exc:
        return None, "", f"{argv[0]}: {exc}"


def _is_worker_body(env=None):
    """True when this process is a worker Body rather than the reducer.

    Same predicate and same signal as iteration-open._is_worker_body and
    agent-watchdog.is_worker_body -- BODY_ROLE, injected by the PreToolUse bash
    hook. A one-line env read of an established signal, not a third definition of
    the role.
    """
    import os

    e = env if env is not None else os.environ
    return (e.get("BODY_ROLE") or "").strip().lower() == "worker"


def _meter(action, runner, sweep=None):
    """Call the budget meter. REFUSED ON A WORKER BODY -- inherited verbatim from
    iteration-open._meter, whose docstring carries the measurement: the state file
    `agents/<agent>/session/precheck-budget-state.json` is agent-wide and MEASURED
    SYNCABLE (owncloud_sync._is_machine_local returns False for it, cc-07
    2026-08-17). Its ruling is that "the LANES are safe to run from either role,
    the agent-wide METER WRITE is not", and that applies with EXTRA force to the
    `executed` records this battery writes: a worker's lane runs appended into the
    reducer's in-flight session would inflate the reducer's `tail_executed`, and
    two boxes doing read-modify-write on one syncable file can lose records
    outright. So a worker still runs every lane and still gets every finding; only
    the agent-wide write is withheld.

    Returning None here is load-bearing on the CHECK path: the caller treats None
    as RUN, so withholding the meter never withholds the work.
    """
    if _is_worker_body():
        return None
    argv = [_METER, action] + ([sweep] if sweep else [])
    rc, out, err = runner(argv, 30)
    if err is not None:
        return None
    return (out or "").strip() or None


def _findings_for(lane, payload):
    """Human detail strings for whatever this lane reported. Empty == clean.

    Byte-identical in semantics to precheck-always-run-battery._findings_for;
    kept as a sibling copy rather than a shared import because the two batteries
    are independently fail-open and a shared helper would couple their blast
    radii for eleven lines of arithmetic.
    """
    out = []
    f = lane["finds"]
    for k in f.get("counts", ()):
        v = payload.get(k)
        if isinstance(v, int) and v > 0:
            out.append(f"{k}={v}")
    for k in f.get("lists", ()):
        v = payload.get(k)
        if isinstance(v, list) and v:
            out.append(f"{k}={len(v)}")
    for k in f.get("false", ()):
        if payload.get(k) is False:
            out.append(f"{k}=False")
    failed = payload.get("failed")
    if isinstance(failed, list) and failed:
        out.append(f"failed={len(failed)}")
    return out


def _emit(report, as_json):
    if report.get("error"):
        # guard-424: precheck/gate scripts fail LOUD on stderr, never silent.
        print(f"[medium-battery] {report['error']}", file=sys.stderr)
    if as_json:
        print(json.dumps(report, ensure_ascii=False))
        return

    for e in report.get("findings", []):
        print(f"▸ FINDING: {e['name']} (phase {e['phase']}) {', '.join(e['detail'])}")
    for b in report.get("blind", []):
        print(f"▸ BLIND: {b['name']} (phase {b['phase']}) — {b['reason']}")
    for d in report.get("dropped", []):
        print(f"▸ DROPPED: {d['name']} (phase {d['phase']}) — {d['reason']}")

    n_find = len(report.get("findings", []))
    n_reg = report.get("lanes_registered", 0)
    mode = report.get("mode")
    comp = report.get("completeness")

    if n_find == 0 and comp == "complete":
        print(f"[medium-battery] all {n_reg} lanes clean (mode={mode})")
    elif n_find == 0:
        # guard-4093: this sentence must NOT read like "all clear".
        print(
            f"[medium-battery] NO FINDINGS REACHED — "
            f"{len(report.get('blind', []))} blind + "
            f"{len(report.get('dropped', []))} dropped of {n_reg} lanes, so this "
            f"is UNREACHABLE, not clean (mode={mode})"
        )
    else:
        print(
            f"[medium-battery] {n_find} finding / {n_reg} lanes "
            f"(mode={mode}, completeness={comp})"
        )


def run(as_json=False, apply=False, lane_runner=None) -> int:
    """Run every registered medium lane the meter allows; report what happened.

    lane_runner: injectable (argv, timeout) -> (rc, stdout, err) for tests.
    """
    runner = lane_runner or _run_bash
    report = {
        "checked_at": _now_iso(),
        "mode": "apply" if apply else "dry_run",
        "lanes_registered": len(LANES),
        "findings": [],
        "blind": [],
        "dropped": [],
        "executed": [],
        "uncovered": [
            {"name": n, "reason": r} for n, r in _NOT_COVERED
        ],
    }
    errors = []

    for lane in LANES:
        # HONORED, not telemetry -- see the module docstring. A meter that cannot
        # be reached returns None, which must mean RUN: the meter is a velocity
        # optimization and a meter bug must never silently disable a lane.
        decision = _meter("check", runner, lane["meter_name"])
        if decision == "drop":
            report["dropped"].append({
                "name": lane["name"], "phase": lane["phase"],
                "reason": "budget meter returned drop",
            })
            continue

        variants = lane.get("variants") or ((),)
        for variant in variants:
            argv = [lane["script"]]
            argv.extend(lane.get("extra_args", ()))
            dyn = lane.get("dynamic_args")
            if dyn is not None:
                argv.extend(dyn())
            argv.extend(variant)
            if apply and lane["apply_flag"]:
                argv.append("--apply")

            label = lane["name"] + (f" {' '.join(variant)}" if variant else "")
            rc, out, err = runner(argv, _LANE_TIMEOUT_S)
            if err is not None:
                report["blind"].append(
                    {"name": label, "phase": lane["phase"], "reason": err}
                )
                errors.append(err)
                continue
            try:
                payload = json.loads(out)
                if not isinstance(payload, dict):
                    raise ValueError(
                        f"expected a JSON object, got {type(payload).__name__}"
                    )
            except Exception as exc:
                # Unparseable output is BLIND, never clean.
                reason = f"unparseable output (rc={rc}): {exc}"
                report["blind"].append(
                    {"name": label, "phase": lane["phase"], "reason": reason}
                )
                errors.append(f"{label}: {reason}")
                continue

            # The lane ran to completion and we parsed it. THIS is the moment the
            # execution record is earned -- emitted by this script, never by an
            # LLM reading a protocol file (guard-399 witness corollary). Recorded
            # only on the success path: a blind or dropped lane did NOT execute,
            # and a witness that fires for them would be the decoration
            # guard-5163 exists to refuse.
            # FIRES PER VARIANT, so `executed[]` and the meter's `tail_executed`
            # count INVOCATIONS, not lanes: aspirations-recover-recurring runs
            # twice (--source world, then agent) under ONE meter_name, so a clean
            # 7-lane run reports tail_executed=8. Verified live (ran:7 /
            # executed:8). Do NOT close the 7-vs-8 gap by hoisting this out of
            # the variant loop -- one record per invocation is what makes a
            # half-failed multi-variant lane visible instead of averaged away.
            _meter("executed", runner, lane["meter_name"])
            report["executed"].append(label)

            detail = _findings_for(lane, payload)
            if detail:
                report["findings"].append(
                    {"name": label, "phase": lane["phase"], "detail": detail}
                )

    # guard-4093: two ORTHOGONAL fields, never collapsed. A DROPPED lane is also
    # a lane we did not see, so it degrades completeness exactly as blindness
    # does -- the drop was deliberate, but the ignorance it produces is not.
    report["completeness"] = (
        "partial" if (report["blind"] or report["dropped"]) else "complete"
    )
    report["status"] = "findings" if report["findings"] else "clean"
    if errors:
        report["error"] = "lane_errors: " + "; ".join(errors)

    _emit(report, as_json)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--json", action="store_true", help="emit one JSON object")
    p.add_argument(
        "--apply", action="store_true",
        help="pass --apply to the lanes that take one (default: dry-run). "
             "The loop entry path uses this; manual invocation usually should not.",
    )
    args = p.parse_args()
    try:
        return run(as_json=args.json, apply=args.apply)
    except Exception as exc:  # fail-open: the battery must never block the loop
        rep = {
            "checked_at": _now_iso(), "mode": "apply" if args.apply else "dry_run",
            "lanes_registered": len(LANES), "findings": [], "blind": [],
            "dropped": [], "executed": [], "uncovered": [],
            "completeness": "partial", "status": "clean",
            "error": f"battery_failed: {exc}",
        }
        _emit(rep, args.json)
        return 0


if __name__ == "__main__":
    sys.exit(main())
