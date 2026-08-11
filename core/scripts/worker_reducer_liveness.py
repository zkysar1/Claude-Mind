#!/usr/bin/env python3
"""Worker-Body reducer-liveness poll ( mechanism 2).

A forked WORKER Body must wind down when its reducer stops being live —
otherwise it keeps claiming and executing goals whose results nobody will ever
merge (the reducer is the only Body that runs generalize-down). This module is
the decision half: it polls the cross-machine runner claim and returns
CONTINUE or WIND-DOWN.

FAIL-SAFE DIRECTION (the design invariant): a worker NEVER promotes itself to
reducer. Every ambiguous or unreadable signal therefore resolves toward
WIND-DOWN, not toward "keep going". Winding down loses nothing — the Body's
divergent WM is staged for the reducer to merge — while continuing without a
reducer accumulates work that is silently discarded.

Signal source: `runner-claim.sh status --agent <agent>`, whose exit code IS its
answer (that script refuses rather than affirms when it cannot establish
liveness). Measured contract:

    rc 0  LIVE         claim row is RUNNING with a fresh heartbeat
    rc 4  NOT LIVE     ABSENT | NOT-RUNNING | STALE | REFUSE (unverifiable)
    rc 2  FAILED       daemon returned an error
    rc 1  daemon error (bash layer maps the daemon's rc=2 to 1)
    rc 3  no daemon

rc=4 deliberately conflates "definitely not live" with "cannot establish".
That conflation is CORRECT here and wrong almost everywhere else: for this one
consumer both readings imply the same fail-safe action, so collapsing them
loses no decision. Do not copy the treatment to a consumer that would act
differently on the two.

TRANSIENT vs TERMINAL: rc in {1,2,3} is a plumbing fault, not evidence about
the reducer, so a single occurrence must not wind a worker down — a daemon
blip would kill every worker in the fleet at once. These accumulate against
`error_threshold` (default 3 consecutive) and only then wind down, per the
design's "N consecutive rc=1 -> wind down too". Any rc=0 resets the counter.

TAKEOVER DETECTION — and its MEASURED LIMIT. The design asks to wind down when
"runner_token changed (a new reducer stale-broke in)". Measured 2026-08-03
against GET /v1/admin/runner-claims: a claim row carries exactly
{agent, machine_id, agent_state, heartbeat_at}. There is NO runner_token in
the payload, so token-change detection is NOT implementable at this endpoint
and is NOT implemented here. `machine_id` is the available proxy: a reducer
that stale-breaks in from ANOTHER box changes it. The residual gap is a
SAME-BOX reducer restart (new token, same machine_id), which this poll cannot
see and will report as CONTINUE. That gap is stated rather than papered over
(guard-1760: never report what you declined to look at as coverage); closing
it requires the endpoint to expose the token.

The expected machine is LEARNED on the first LIVE poll rather than read from
the body manifest. The manifest's `machine_id` field is written by the fork
path and its subject (this Body vs the reducer) is not something this module
should assume — asserting a field's meaning without reading the code that
WRITES it is the rb-3419 class. Self-bootstrapping needs no such assumption.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

VERDICT_CONTINUE = "continue"
VERDICT_WIND_DOWN = "wind-down"

DEFAULT_ERROR_THRESHOLD = 3

# rc values that mean "the poll itself failed", as opposed to "the reducer is
# not live". Only these accumulate toward the threshold.
TRANSIENT_RCS = (1, 2, 3)

# The one token runner-claim.sh prints on its LIVE branch and nowhere else (its
# STALE branch says "is RUNNING but", which deliberately does not match).
#
# F2 (): takeover detection keys on a PROSE summary line, and nothing
# linked the parser to the emitter — so a reformat upstream would leave every
# parsing test GREEN (they pin hand-copies of the line) while _parse_machine
# silently returned None and the takeover branch went dead. Module-level so the
# contract test can assert the EMITTER still emits THIS string rather than
# re-typing a copy that drifts with it (guard-920 class).
#
# This is as close to a shared constant as reach allows: the emitter is a bash
# script with embedded python and cannot import from here, and editing it is
# outside this goal's scope fence. The contract test is the join.
LIVE_MARKER = "is RUNNING on "


def decide(rc, observed_machine, expected_machine, consecutive_errors,
           error_threshold=DEFAULT_ERROR_THRESHOLD):
    """Pure decision function — no I/O, so tests can drive every branch.

    Returns {verdict, reason, consecutive_errors, expected_machine}.
    `consecutive_errors` in the result is the NEW value to persist.
    """
    if rc == 0 and not observed_machine:
        # F5 (): rc==0 ALONE is not proof of life. `runner-claim.sh
        # status` exits 0 for exactly ONE designed reason — the LIVE branch —
        # and that branch ALWAYS prints "is RUNNING on '<machine>'" (verified in
        # the emitter: every other status branch sys.exit(4), and the
        # no-python-launcher path exits 4 too, with the comment "its exit code IS
        # the answer ... Refuse (4) rather than affirm (0)").
        #
        # So a zero WITHOUT that marker cannot come from the contract; it comes
        # from the wrapper dying in a way that still yields 0 (measured upstream,
        # msg-20260802-175041-alpha-5896). Previously this landed on the LIVE
        # branch below and returned continue with the reason "reducer LIVE on
        # unknown-machine" — a crash reading as life, which inverts the
        # never-promote invariant this module advertises.
        #
        # It is TRANSIENT, not decisive: a single crashed poll must not kill the
        # fleet's workers, and the threshold already encodes "unverifiable for
        # too long". Because no designed emitter path can produce this shape, the
        # false-positive risk against the contract is zero.
        n = consecutive_errors + 1
        if n >= error_threshold:
            return {
                "verdict": VERDICT_WIND_DOWN,
                "reason": (f"{n} consecutive polls returned rc=0 with no LIVE "
                           f"marker at threshold {error_threshold} — a zero exit "
                           f"carrying no 'is RUNNING on' line is an unverified "
                           f"claim, not a live reducer"),
                "consecutive_errors": n,
                "expected_machine": expected_machine,
            }
        return {
            "verdict": VERDICT_CONTINUE,
            "reason": (f"rc=0 but no LIVE marker parsed — asserting nothing, "
                       f"{n}/{error_threshold} consecutive — not yet decisive"),
            "consecutive_errors": n,
            "expected_machine": expected_machine,
        }

    if rc == 0:
        # Live claim. Counter resets — a run of transient faults that ends in a
        # successful poll was a blip, not a dying reducer.
        if expected_machine and observed_machine and observed_machine != expected_machine:
            return {
                "verdict": VERDICT_WIND_DOWN,
                "reason": (f"reducer takeover: claim is now LIVE on "
                           f"{observed_machine!r}, but this Body was forked under "
                           f"a reducer on {expected_machine!r}"),
                "consecutive_errors": 0,
                "expected_machine": expected_machine,
            }
        return {
            "verdict": VERDICT_CONTINUE,
            "reason": f"reducer LIVE on {observed_machine or 'unknown-machine'}",
            "consecutive_errors": 0,
            # First LIVE poll learns the machine; later ones keep it.
            "expected_machine": expected_machine or observed_machine,
        }

    if rc in TRANSIENT_RCS:
        n = consecutive_errors + 1
        if n >= error_threshold:
            return {
                "verdict": VERDICT_WIND_DOWN,
                "reason": (f"{n} consecutive transient poll failures "
                           f"(rc={rc}) at threshold {error_threshold} — the "
                           f"reducer's liveness has been unverifiable for too "
                           f"long to keep claiming work"),
                "consecutive_errors": n,
                "expected_machine": expected_machine,
            }
        return {
            "verdict": VERDICT_CONTINUE,
            "reason": (f"transient poll failure (rc={rc}), "
                       f"{n}/{error_threshold} consecutive — not yet decisive"),
            "consecutive_errors": n,
            "expected_machine": expected_machine,
        }

    # rc == 4, or anything unrecognised. Both resolve toward the fail-safe:
    # an unknown rc from a script whose exit code IS its answer is precisely
    # the case where a worker must not assume it may keep running.
    return {
        "verdict": VERDICT_WIND_DOWN,
        "reason": (f"reducer not live (rc={rc}: ABSENT | NOT-RUNNING | STALE | "
                   f"REFUSE)" if rc == 4 else
                   f"unrecognised poll rc={rc} — treating as not-live per the "
                   f"never-promote invariant"),
        "consecutive_errors": 0,
        "expected_machine": expected_machine,
    }


def _state_path(agent_dir, sid):
    return Path(agent_dir) / "sessions" / sid / "reducer-liveness-state.json"


def _read_state(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _parse_machine(stdout):
    """Pull the machine id out of runner-claim.sh's LIVE summary line.

    Format: ... — 'zeta' is RUNNING on 'cc-02', heartbeat 272s old ...
    Returns None when absent; decide() treats an unknown machine as
    non-discriminating rather than as a takeover.
    """
    marker = LIVE_MARKER
    i = stdout.find(marker)
    if i < 0:
        return None
    rest = stdout[i + len(marker):]
    if not rest.startswith("'"):
        return None
    j = rest.find("'", 1)
    return rest[1:j] if j > 0 else None


def poll(agent, agent_dir, sid, scripts_dir, error_threshold=DEFAULT_ERROR_THRESHOLD):
    """Run the real poll and persist the counter. Returns the decide() dict."""
    # F3 (): this import used to rely on a sys.path.insert that only
    # main() performed, so ANY direct library call (a test, another module)
    # raised ImportError before reaching the first line of real work. Inserting
    # here makes poll() self-sufficient; main() still inserts for its own use and
    # a duplicate entry is harmless.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _runtime_bash import bash_cmd  # guard-580/581: never a bare "bash" argv[0]

    proc = subprocess.run(
        bash_cmd(str(Path(scripts_dir) / "runner-claim.sh"), "status", "--agent", agent),
        capture_output=True, text=True,
    )
    combined = (proc.stdout or "") + (proc.stderr or "")

    state = _read_state(_state_path(agent_dir, sid))
    result = decide(
        proc.returncode,
        _parse_machine(combined),
        state.get("expected_machine"),
        int(state.get("consecutive_errors") or 0),
        error_threshold,
    )
    result["rc"] = proc.returncode
    result["poll_output"] = combined.strip()[:400]

    sp = _state_path(agent_dir, sid)
    try:
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps({
            "consecutive_errors": result["consecutive_errors"],
            "expected_machine": result["expected_machine"],
        }), encoding="utf-8")
    except Exception as exc:
        # F1 (). The old comment here read "never let a state-write
        # failure decide the loop" and was exactly backwards. Each poll is a
        # SEPARATE PROCESS, so the transient threshold can only accumulate
        # THROUGH THIS FILE. If the write fails, `consecutive_errors` is frozen
        # at whatever is on disk and decide() can never reach the threshold — so
        # a swallowed write error does not merely lose telemetry, it DISARMS the
        # only mechanism that ever winds a worker down on transient faults.
        # Measured in the filing (): with the state path unwritable, 6
        # consecutive transient polls all returned continue with the counter
        # frozen at 1; the control with working persistence wound down at poll 3.
        #
        # The fix is scoped to the case where the counter was actually LOAD-
        # BEARING — a pending, unpersistable escalation. On a verdict that
        # already decided (wind-down) or that resets the counter (a genuine LIVE
        # poll, consecutive_errors == 0), a failed write costs nothing and must
        # not stop a healthy loop (guard-1562: stopping a healthy loop on a
        # plumbing fault is worse than the disease).
        result["state_write_error"] = str(exc)
        if (result["verdict"] == VERDICT_CONTINUE
                and result["consecutive_errors"] > 0):
            result["verdict"] = VERDICT_WIND_DOWN
            result["reason"] = (
                f"{result['reason']} — AND the failure counter could not be "
                f"persisted ({exc}), so it cannot accumulate across polls and "
                f"the transient threshold can never fire. Winding down rather "
                f"than continuing with a disarmed fail-safe."
            )

    return result


def main(argv):
    if len(argv) > 1 and argv[1] == "decide-only":
        # Test/inspection seam: decide() over argv, no daemon, no state file.
        # rc observed expected consecutive [threshold]
        rc = int(argv[2])
        observed = argv[3] or None
        expected = argv[4] or None
        consecutive = int(argv[5])
        threshold = int(argv[6]) if len(argv) > 6 else DEFAULT_ERROR_THRESHOLD
        out = decide(rc, observed, expected, consecutive, threshold)
        print(json.dumps(out))
        return 0 if out["verdict"] == VERDICT_CONTINUE else 1

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    agent = os.environ.get("MIND_AGENT", "").strip()
    sid = os.environ.get("MIND_SID", "").strip()
    if not agent or not sid:
        # No bound identity: this is not a worker Body context. Report continue
        # (rc 0) — the poll is a worker-loop step and a reducer must never be
        # wound down by it.
        print(json.dumps({
            "verdict": VERDICT_CONTINUE,
            "reason": "no MIND_AGENT/MIND_SID — not a worker Body context",
            "consecutive_errors": 0,
        }))
        return 0

    import _paths
    agent_dir = _paths.agent_dir(agent)

    # A Body with no forked per-session WM file is the REDUCER (the same
    # predicate bash-agent-inject uses; derived locally per guard-2445). The
    # reducer must never wind itself down on its own liveness.
    if not (Path(agent_dir) / "sessions" / sid / "working-memory.yaml").exists():
        print(json.dumps({
            "verdict": VERDICT_CONTINUE,
            "reason": "this Body is the reducer (no forked per-session WM) — poll does not apply",
            "consecutive_errors": 0,
        }))
        return 0

    result = poll(agent, agent_dir, sid, Path(__file__).resolve().parent)
    print(json.dumps(result))
    return 0 if result["verdict"] == VERDICT_CONTINUE else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
