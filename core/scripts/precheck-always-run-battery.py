"""precheck-always-run-battery — one call running every STANDALONE always-run
precheck lane under the budget meter and printing FINDINGS ONLY (g-115-6466).

WHY THIS EXISTS. aspirations-precheck/SKILL.md is 172,391 B / 2,695 lines / 50
`## Phase` headers (re-measured on cc-07 2026-08-17; byte-identical to the cc-04
measurement in the goal's progress_note). The per-iteration loop spec is larger
than the context window, so the reducer runs it from compaction memory and skips
phases -- and an always-run lane that lives only as an LLM-orchestrated phase is
skippable by construction. This is the g-115-2303 sentinel-battery /
g-115-2984 cadence-battery pattern applied to the always-run tier: post-compaction,
"run the always-run battery" is the ONE line that has to survive summarization,
and the output re-derives the full lane set from LANES so a phase can never
silently fall out of the protocol.

FINDINGS ONLY IS THE POINT, AND IT IS ALSO THE HAZARD. The six lanes emit
tens of kB of JSON between them on a quiet iteration (22,747 B measured when
the set was five -- the denominator is kept rather than silently rescaled). Printing that
every entry is exactly the context cost this goal exists to remove. But a
findings-only battery that stays quiet when a lane FAILED is guard-4093's defect:
"a zero with ANY blind lane is UNREACHABLE, not EMPTY". So this battery keeps two
ORTHOGONAL fields and never collapses them:

    status        did I find anything?      findings | clean
    completeness  did I see everything?     complete | partial

A clean run prints "all N lanes clean" ONLY when completeness == complete. With
any blind lane it prints NO-FINDINGS-REACHED and names the blind lanes, because
"found nothing" and "could not look" must never render identically. The natural
aggregator ("if all lanes blind -> blind") is the wrong one: one reachable lane
returning nothing would outvote every blind lane.

THE LANE SET IS SIX, NOT TEN, AND THE DIFFERENCE IS NOT AN OMISSION. The tier
table carries 10 always-run rows. Four of them (0-pre tree-debt-gate, 0-pre2
experience-archival-gate, 0-pre2.5 evolution-finalize-gate, 0-pre3
fresh-eyes-code-gate) have NO standalone script -- they are already dispatched by
`precheck-sentinel-battery.sh` and their bodies live in SKILL.md phase sections.
Wrapping them here would run them twice. This battery owns the six lanes that
have their own scripts; `_uncovered_lanes()` names the other four in the report so
the split is visible rather than inferred.

METER NAME != SCRIPT NAME, AND THE MISMATCH IS LOAD-BEARING. The 0.5g.7 lane is
registered with the budget meter as `completed-not-closed-drain` while its script
is `completed-not-closed-slate.sh`. A registry that passed the script stem to the
meter would miss `sweep_tier()`'s case arm, hit the WARN-default `medium` tier,
and make an always-run lane DROPPABLE in a tight zone -- which is exactly the
g-115-3124 drift the meter's own comment records for dependency-timeout-check.
`meter_name` is therefore a separate field from `script`, never derived from it.

DRY-RUN IS THE DEFAULT AND THE MODE IS ALWAYS PRINTED. Four of the six lanes
send notifications and post to the board under `--apply`; the tier table calls
them with it. Defaulting to apply would make any manual or test invocation fire
real escalations, and defaulting to dry-run SILENTLY would turn the loop's
escalation lanes into no-ops the day the SKILL.md is rewired to call this. So the
default is dry-run, `--apply` passes through, and every report carries `mode` --
a reader can never mistake one for the other.

Output (guard-424 fail-loud-with-stderr; guard-614 structured on EVERY exit path):
  default -- one human line per lane WITH findings, then a summary:
      > FINDING: <lane> (phase <phase>) <detail>
      [always-run-battery] 2 finding / 6 lanes (mode=dry_run, completeness=complete)
  --json  -- {checked_at, mode, status, completeness, lanes_registered,
             findings:[...], blind:[...], uncovered:[...], error?}

Fail-open: any error prints the structured report (also to stderr) and exits 0.
The battery must never block the loop.

Invocation: `bash core/scripts/precheck-always-run-battery.sh [--apply] [--json]`.
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

# handoff-aging-check scanned 2,616 records in the measurement run; 120 s leaves
# headroom without letting one wedged lane hold the loop entry open.
_LANE_TIMEOUT_S = 120
_METER = "aspirations-precheck-budget-meter.sh"


# --- the registry -----------------------------------------------------------
# INLINE ON PURPOSE. `_cadence_registry` is a module because six scripts import
# it; this set has exactly one consumer, and a single-use registry module would
# be the abstraction implementation-discipline.md rule 3 forbids. The sibling
# precheck-sentinel-battery.py holds its registry inline for the same reason.
#
# `finds` is declarative rather than a per-lane function so that adding a lane is
# a data edit. Semantics:
#   counts  -- int key, >0 is a finding
#   lists   -- list key, non-empty is a finding
#   false   -- bool key, FALSE is a finding (an all_clear-style flag)
# Every lane additionally treats a non-empty `failed` list as BOTH a finding and
# an error, so a lane that half-worked is never reported as clean.
#
# COUNTS IN THE PROSE ABOVE ARE PROSE. `len(LANES)` is the only authority for
# how many lanes this battery owns, and the report prints it at runtime. Seven
# hardcoded "five lanes" claims went stale the moment a sixth was registered
# ( fresh-eyes) -- the three ENFORCED registration sites (this tuple,
# the meter's always-run arm, the SKILL.md tier table) are pinned by
# test_budget_meter_sweep_tier_parity, but no test can pin a sentence. If you
# add a lane, re-grep this file for a spelled-out count before you commit.
LANES = (
    {
        "name": "inbox-alert-age-check",
        "phase": "0.5b.1b",
        "meter_name": "inbox-alert-age-check",
        "script": "inbox-alert-age-check.sh",
        "apply_flag": True,
        "finds": {"counts": ("candidate_count",), "lists": (), "false": ()},
    },
    {
        "name": "user-blocker-escalation-check",
        "phase": "0.5b.1c",
        "meter_name": "user-blocker-escalation-check",
        "script": "user-blocker-escalation-check.sh",
        "apply_flag": True,
        # This lane reports health as a POSITIVE flag rather than a count, so the
        # finding is all_clear == False. Reading `eligible` instead would fire on
        # every iteration: eligible counts rows in scope, not rows needing action.
        "finds": {"counts": (), "lists": (), "false": ("all_clear",)},
    },
    {
        "name": "dependency-timeout-check",
        "phase": "0.5b.2",
        "meter_name": "dependency-timeout-check",
        "script": "dependency-timeout-check.sh",
        "apply_flag": True,
        "finds": {
            "counts": (),
            "lists": ("candidates", "escalated", "needs_user_notification"),
            "false": (),
        },
    },
    {
        "name": "handoff-aging-check",
        "phase": "0.5b.2b",
        "meter_name": "handoff-aging-check",
        "script": "handoff-aging-check.sh",
        "apply_flag": True,
        "finds": {"counts": ("candidate_count",), "lists": (), "false": ()},
    },
    {
        "name": "completed-not-closed-drain",
        "phase": "0.5g.7",
        # NOT derived from `script` -- see the module docstring. The meter's
        # sweep_tier() case arm knows this name; the script stem would WARN-default
        # to `medium` and make an always-run lane droppable.
        "meter_name": "completed-not-closed-drain",
        "script": "completed-not-closed-slate.sh",
        # Report-only by contract: it has no --apply and must never be handed one.
        "apply_flag": False,
        "extra_args": ("--json",),
        "finds": {"counts": (), "lists": ("slate",), "false": ()},
    },
    {
        "name": "world-script-crlf-check",
        "phase": "0.5g.8",
        # Registered in sweep_tier()'s always-run arm under this exact name. See
        # the meter-name trap above: the script stem happens to match here, but
        # the field stays explicit so a future rename cannot silently demote an
        # always-run lane to droppable.
        "meter_name": "world-script-crlf-check",
        "script": "world-script-crlf-check.sh",
        # Report-only by contract: it has no --apply and must never be handed one.
        # Repairing a *.sh under world/ writes into a live own-cloud-synced store
        # and can race the sync; the 2026-08-22 repair needed a content-
        # preservation assertion to be safe ().
        "apply_flag": False,
        "finds": {"counts": (), "lists": ("offenders",), "false": ()},
    },
)

# The always-run rows this battery deliberately does NOT run, because they have no
# standalone script and are already dispatched by precheck-sentinel-battery.sh.
# Named in the report so "6 lanes" is never read as "the whole always-run tier".
_SENTINEL_DISPATCHED = (
    ("0-pre", "tree-debt-gate"),
    ("0-pre2", "experience-archival-gate"),
    ("0-pre2.5", "evolution-finalize-gate"),
    ("0-pre3", "fresh-eyes-code-gate"),
)


def _now_iso() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


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


def _meter_check(lane_name, runner):
    """Log this lane's meter decision. Always-run lanes never drop, so the return
    is telemetry, not a gate -- but skipping the call would lose the per-sweep
    elapsed-ms record the drop-log is built from."""
    rc, out, err = runner([_METER, "check", lane_name], 30)
    if err is not None:
        return None
    return (out or "").strip() or None


def _findings_for(lane, payload):
    """Return a list of human detail strings for whatever this lane reported.

    Empty list == this lane is genuinely clean. A lane whose payload could not be
    parsed never reaches here; that is a BLIND lane, handled by the caller.
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
    # Universal: a lane that recorded its own failures is never clean.
    failed = payload.get("failed")
    if isinstance(failed, list) and failed:
        out.append(f"failed={len(failed)}")
    return out


def _emit(report, as_json):
    if report.get("error"):
        # guard-424: precheck/gate scripts fail LOUD on stderr, never silent.
        print(f"[always-run-battery] {report['error']}", file=sys.stderr)
    if as_json:
        print(json.dumps(report, ensure_ascii=False))
        return

    for e in report.get("findings", []):
        print(
            f"▸ FINDING: {e['name']} (phase {e['phase']}) {', '.join(e['detail'])}"
        )
    for b in report.get("blind", []):
        print(f"▸ BLIND: {b['name']} (phase {b['phase']}) — {b['reason']}")

    n_find = len(report.get("findings", []))
    n_reg = report.get("lanes_registered", 0)
    mode = report.get("mode")
    comp = report.get("completeness")

    if n_find == 0 and comp == "complete":
        print(f"[always-run-battery] all {n_reg} lanes clean (mode={mode})")
    elif n_find == 0:
        # guard-4093: this is the sentence that must NOT read like "all clear".
        print(
            f"[always-run-battery] NO FINDINGS REACHED — {len(report.get('blind', []))} "
            f"of {n_reg} lanes blind, so this is UNREACHABLE, not clean "
            f"(mode={mode})"
        )
    else:
        print(
            f"[always-run-battery] {n_find} finding / {n_reg} lanes "
            f"(mode={mode}, completeness={comp})"
        )


def run(as_json=False, apply=False, lane_runner=None) -> int:
    """Run every registered always-run lane; report findings and blindness.

    lane_runner: injectable (argv, timeout) -> (rc, stdout, err) for tests.
    """
    runner = lane_runner or _run_bash
    report = {
        "checked_at": _now_iso(),
        "mode": "apply" if apply else "dry_run",
        "lanes_registered": len(LANES),
        "findings": [],
        "blind": [],
        "uncovered": [
            {"phase": p, "name": n, "dispatched_by": "precheck-sentinel-battery.sh"}
            for p, n in _SENTINEL_DISPATCHED
        ],
    }
    errors = []

    for lane in LANES:
        _meter_check(lane["meter_name"], runner)

        argv = [lane["script"]]
        argv.extend(lane.get("extra_args", ()))
        if apply and lane["apply_flag"]:
            argv.append("--apply")

        rc, out, err = runner(argv, _LANE_TIMEOUT_S)
        if err is not None:
            report["blind"].append(
                {"name": lane["name"], "phase": lane["phase"], "reason": err}
            )
            errors.append(err)
            continue
        try:
            payload = json.loads(out)
            if not isinstance(payload, dict):
                raise ValueError(f"expected a JSON object, got {type(payload).__name__}")
        except Exception as exc:
            # Unparseable output is BLIND, never clean. All six lanes emit JSON
            # on stdout by default (measured); a non-JSON body means the lane
            # broke or changed shape, and either way we did not see its findings.
            reason = f"unparseable output (rc={rc}): {exc}"
            report["blind"].append(
                {"name": lane["name"], "phase": lane["phase"], "reason": reason}
            )
            errors.append(f"{lane['name']}: {reason}")
            continue

        detail = _findings_for(lane, payload)
        if detail:
            report["findings"].append(
                {"name": lane["name"], "phase": lane["phase"], "detail": detail}
            )

    # guard-4093: two ORTHOGONAL fields. ANY blind lane -> partial; never "if all
    # blind", which lets one reachable empty lane outvote every blind one.
    report["completeness"] = "partial" if report["blind"] else "complete"
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
        help="pass --apply to the four notification lanes (default: dry-run). "
             "The loop entry path uses this; manual invocation usually should not.",
    )
    args = p.parse_args()
    try:
        return run(as_json=args.json, apply=args.apply)
    except Exception as exc:  # fail-open: the battery must never block the loop
        rep = {
            "checked_at": _now_iso(), "mode": "apply" if args.apply else "dry_run",
            "lanes_registered": len(LANES), "findings": [], "blind": [],
            "uncovered": [], "completeness": "partial", "status": "clean",
            "error": f"battery_failed: {exc}",
        }
        _emit(rep, args.json)
        return 0


if __name__ == "__main__":
    sys.exit(main())
