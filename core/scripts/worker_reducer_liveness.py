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

TAKEOVER DETECTION — TWO AXES, and the second one closes what this module used
to call its MEASURED LIMIT. The design asks to wind down when "runner_token
changed (a new reducer stale-broke in)". Measured 2026-08-03, a claim row from
GET /v1/admin/runner-claims carried exactly {agent, machine_id, agent_state,
heartbeat_at} — no token — so only `machine_id` was available and a SAME-BOX
reducer restart (new token, unchanged machine_id) was invisible and reported as
CONTINUE. That gap is now closed (g-306-224), but NOT the way the filing asked.

  machine_id  — a reducer that stale-breaks in from ANOTHER box changes it.
  token fp    — a NON-REVERSIBLE digest of runner_token, added to the endpoint,
                to runner-claim.sh's LIVE line, and consumed here. It changes on
                a re-mint, so it catches the same-box restart machine_id cannot.

THE TWO AXES ARE NOT SYMMETRIC IN WHAT THEY DO AFTER FIRING, and that is load-
bearing rather than an oversight:

  machine axis (cross-box)  LATCHES — the expected machine is re-asserted, so
                            every later poll winds down again. Correct: a reducer
                            on another box may never see this Body's locally-
                            staged WM, so it must stay down until an operator
                            relaunches it.
  token-fp axis (same-box)  ONE-SHOT — the wind-down fires, then the observed fp
                            is ADOPTED, so the next poll rejoins under the new
                            runner. Correct because the merge path is by DISK,
                            not by fork lineage: the same-box runner adopts the
                            staged work exactly as the old one would have.

Adopting was not the original behaviour, and the difference was catastrophic
rather than cosmetic. Measured 2026-08-30 on zc-03 (coach, 8 Bodies): a reducer
relaunch re-minted the token, and because the fp branch re-asserted the STALE
baseline, all 7 workers wound down and could never rejoin — 41 session state
files, not one of which had learned the live fp, each rewritten on every poll
with the value frozen. The fleet ran on its reducer alone for ~5 h. A fail-safe
with no reset path is not a fail-safe; it is an off switch on a timer.

THE RAW TOKEN IS DELIBERATELY NOT EXPOSED, and this module must never ask for
it. `runner_token` is the ConditionExpression bearer credential for the backend's
`heartbeat` and `release_runner`: anything holding it can forge a heartbeat for
another agent (defeating `reclaim_if_stale`, so a crashed runner could never be
reclaimed) or release a LIVE claim, forcing a healthy reducer down mid-flight.
Publishing it to close a liveness gap would defeat the mechanism this poll exists
to protect. A consumer that only needs to notice CHANGE never needs the value —
full argument in `owncloud_backend.runner_token_fingerprint`.

FAIL-SAFE ASYMMETRY FOR THE NEW AXIS, and it points the OTHER way from this
module's usual invariant, on purpose. An ABSENT fingerprint (`unknown` on the
LIVE line: a daemon predating the field, a never-claimed row) is
NON-DISCRIMINATING, exactly like an unparsed machine_id — it is a fact about the
plumbing's version, not a signal about the reducer, so it must not wind anyone
down. Treating absence as change would wind down every worker in a mixed-version
fleet at once, which is the same fleet-wide-kill failure the transient threshold
exists to prevent. Only an observed fp that DIFFERS from a previously observed fp
is decisive. Genuine unreadability is still covered by `error_threshold`.

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

# The token-fingerprint clause runner-claim.sh appends to that SAME LIVE line
# (). Module-level for the same reason LIVE_MARKER is: the emitter is a
# bash script with embedded python that cannot import from here, so a contract
# test asserting the emitter still emits THIS string is the only real join
# (guard-920 — pin the production shape, never a hand-copy that drifts with it).
TOKEN_FP_MARKER = "token-fp "

# What the emitter prints when the daemon supplied no fingerprint. Parsed back to
# None so it is non-discriminating rather than a fingerprint literally spelled
# "unknown" — which would otherwise compare EQUAL across two different reducers
# and read as "no takeover", the one direction this axis must never fail in.
TOKEN_FP_UNKNOWN = "unknown"


def decide(rc, observed_machine, expected_machine, consecutive_errors,
           error_threshold=DEFAULT_ERROR_THRESHOLD,
           observed_token_fp=None, expected_token_fp=None):
    """Pure decision function — no I/O, so tests can drive every branch.

    Returns {verdict, reason, consecutive_errors, expected_machine,
    expected_token_fp}. Both `expected_*` values in the result are the NEW
    values to persist, as is `consecutive_errors`.

    The token-fp pair is KEYWORD-with-default so every pre-existing positional
    call (and every test pinning the 4/5-arg shape) stays valid and keeps its
    old behaviour exactly: absent fingerprints are non-discriminating, so a
    caller that never learned to pass them gets today's machine-only decision
    rather than a new wind-down. That is the required direction for a
    mixed-version fleet — see the module docstring's FAIL-SAFE ASYMMETRY note.
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
                "expected_token_fp": expected_token_fp,
            }
        return {
            "verdict": VERDICT_CONTINUE,
            "reason": (f"rc=0 but no LIVE marker parsed — asserting nothing, "
                       f"{n}/{error_threshold} consecutive — not yet decisive"),
            "consecutive_errors": n,
            "expected_machine": expected_machine,
            "expected_token_fp": expected_token_fp,
        }

    if rc == 0:
        # Live claim. Counter resets — a run of transient faults that ends in a
        # successful poll was a blip, not a dying reducer.
        #
        # Machine is checked BEFORE token fp only because a cross-box takeover
        # changes BOTH, and naming the two boxes is the more useful diagnostic.
        # Neither ordering changes any verdict.
        if expected_machine and observed_machine and observed_machine != expected_machine:
            return {
                "verdict": VERDICT_WIND_DOWN,
                "reason": (f"reducer takeover: claim is now LIVE on "
                           f"{observed_machine!r}, but this Body was forked under "
                           f"a reducer on {expected_machine!r}"),
                "consecutive_errors": 0,
                "expected_machine": expected_machine,
                "expected_token_fp": expected_token_fp,
            }
        if (expected_token_fp and observed_token_fp
                and observed_token_fp != expected_token_fp):
            # SAME-BOX reducer restart — the axis machine_id structurally cannot
            # see. The claim row is LIVE on the machine this Body expects, but
            # the runner_token behind it was re-minted, which happens only when
            # the old claim was released or stale-broken and a NEW runner
            # acquired it. That new runner did not fork this Body, so nobody
            # will merge its work.
            #
            # Both operands are digests, never the raw token, so printing them
            # is safe AND is the whole diagnostic value of the axis — a reader
            # can see that the identity moved without ever holding the
            # credential (owncloud_backend.runner_token_fingerprint).
            return {
                "verdict": VERDICT_WIND_DOWN,
                "reason": (f"reducer restart: claim is still LIVE on "
                           f"{observed_machine or 'unknown-machine'!r}, but the "
                           f"runner token was re-minted (fp "
                           f"{expected_token_fp} -> {observed_token_fp}) — "
                           f"winding down THIS unit so its work is staged for "
                           f"the runner now holding the claim; the new fp is "
                           f"ADOPTED below, so the next poll rejoins under it"),
                "consecutive_errors": 0,
                "expected_machine": expected_machine,
                # ADOPT, do not re-assert the stale baseline. This one word is
                # the difference between a fail-safe that fires and one that
                # LATCHES, and it cost the coach fleet its entire worker
                # population (measured 2026-08-30, zc-03 — see below).
                #
                # Persisting `expected_token_fp` here made the verdict permanent
                # for the life of the session dir: every subsequent poll re-read
                # the stale baseline, re-compared it against the same live fp,
                # and wound down again. The state file was rewritten on EVERY
                # poll (fresh mtimes prove the polls ran) while the value never
                # advanced — a latch by VALUE rather than by write, which is why
                # it survived a reviewer looking for an unwritten store
                # (guard-4870 names the write-side twin).
                #
                # WHY ADOPTING IS NOT A WEAKENING. The wind-down's purpose is
                # discharged by its FIRST firing: the Body closes its unit and
                # stages its WM. A Body that has already wound down has nothing
                # left to orphan, so every later firing is pure cost. Adoption
                # keeps the fire and drops the repeat.
                #
                # AND THE PREMISE ONLY HOLDS CROSS-BOX. The old reason claimed
                # "a new runner ... did not fork this Body, so nobody will merge
                # its work" — a LINEAGE argument the merge path does not use.
                # The reducer adopts staged work by reading the session dirs on
                # DISK (stranded-claim-sweep, generalize-down), judging bodies by
                # carrier/transcript/in-flight, never by who forked them.
                # Measured the same day: a freshly-relaunched reducer released
                # , a claim held by a Body it had never forked. On one
                # box the new runner therefore merges exactly what the old one
                # would have.
                #
                # Hence the deliberate asymmetry with the machine axis above,
                # which stays LATCHED: a reducer on another box may never see
                # this Body's locally-staged WM, so there the premise is true and
                # a Body must stay down until an operator relaunches it.
                "expected_token_fp": observed_token_fp,
            }
        return {
            "verdict": VERDICT_CONTINUE,
            "reason": f"reducer LIVE on {observed_machine or 'unknown-machine'}",
            "consecutive_errors": 0,
            # First LIVE poll learns the machine; later ones keep it.
            "expected_machine": expected_machine or observed_machine,
            # Same self-bootstrapping for the fp — and note the `or` is what
            # makes an ABSENT observation non-destructive: a daemon that predates
            # the field (observed None) leaves a previously-learned fp in place
            # rather than erasing it, so a fleet that upgrades mid-session does
            # not lose the axis it had already armed.
            "expected_token_fp": expected_token_fp or observed_token_fp,
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
                "expected_token_fp": expected_token_fp,
            }
        return {
            "verdict": VERDICT_CONTINUE,
            "reason": (f"transient poll failure (rc={rc}), "
                       f"{n}/{error_threshold} consecutive — not yet decisive"),
            "consecutive_errors": n,
            "expected_machine": expected_machine,
            "expected_token_fp": expected_token_fp,
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
        "expected_token_fp": expected_token_fp,
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

    LINE-SCOPED, for the same reason :func:`_parse_token_fp` is, and it was NOT
    until 2026-08-17 (g-306-224 fresh-eyes). Without the ``split`` below, ``rest``
    runs to the end of the WHOLE capture, so a LIVE line truncated between the
    opening and closing quote — an ssh/pipe cut, a bounded read, an OOM kill
    mid-write — lets ``find("'", 1)`` match a quote on a LATER line. Measured on
    ``"...— 'zeta' is RUNNING on 'cc-0\\n[warn] peer 'cc-99' busy"``: returned
    ``'cc-0\\n[warn] peer '``, which differs from expected_machine and produced
    verdict ``wind-down`` on a HEALTHY reducer. That is this module's
    fail-UNSAFE direction, and truncated output is likeliest exactly when a
    worker is being polled during trouble. ``_parse_token_fp`` returned None on
    the identical input — the newer sibling was hardened and this one was not,
    which is the parity gap rb-1915 / guard-1924 name. Both are now pinned to
    the SAME truncation fixture so the next reader cannot harden one alone.
    """
    marker = LIVE_MARKER
    i = stdout.find(marker)
    if i < 0:
        return None
    rest = stdout[i + len(marker):].split("\n", 1)[0]
    if not rest.startswith("'"):
        return None
    j = rest.find("'", 1)
    return rest[1:j] if j > 0 else None


def _parse_token_fp(stdout):
    """Pull the runner-token FINGERPRINT off that SAME LIVE summary line.

    Format: ... heartbeat 272s old (threshold 3900s), token-fp 1f4c0a9b2e6d8035

    Scoped to the LIVE line deliberately — the marker is searched only AFTER
    LIVE_MARKER and only to the end of THAT line — so a `token-fp` string
    appearing anywhere else in captured stderr (a traceback quoting this
    module, a peer's diagnostic) cannot be mistaken for this claim's
    fingerprint. A mis-scoped read here would be worse than no read: it would
    manufacture a spurious "the fp changed" and wind down a healthy worker.

    Returns None when absent, unparsable, or literally TOKEN_FP_UNKNOWN.
    decide() treats a None fp as NON-discriminating, never as a change.
    """
    i = stdout.find(LIVE_MARKER)
    if i < 0:
        return None
    line = stdout[i:].split("\n", 1)[0]
    j = line.find(TOKEN_FP_MARKER)
    if j < 0:
        return None
    rest = line[j + len(TOKEN_FP_MARKER):].strip()
    if not rest:
        return None
    fp = rest.split()[0].strip(",.;:'\"")
    if not fp or fp == TOKEN_FP_UNKNOWN:
        return None
    return fp


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
        # A state file written before  carries no expected_token_fp, so
        # .get() yields None and the fp axis is simply non-discriminating until
        # the first LIVE poll learns one. That is the whole upgrade path — no
        # migration, and no wind-down caused by the upgrade itself.
        observed_token_fp=_parse_token_fp(combined),
        expected_token_fp=state.get("expected_token_fp"),
    )
    result["rc"] = proc.returncode
    result["poll_output"] = combined.strip()[:400]

    sp = _state_path(agent_dir, sid)
    try:
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps({
            "consecutive_errors": result["consecutive_errors"],
            "expected_machine": result["expected_machine"],
            "expected_token_fp": result["expected_token_fp"],
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
        # rc observed expected consecutive [threshold] [observed_fp] [expected_fp]
        # The two fp args are trailing and optional so every existing 5/6-arg
        # invocation keeps its exact meaning; an empty string means "absent",
        # matching what _parse_token_fp returns for a fp-less LIVE line.
        rc = int(argv[2])
        observed = argv[3] or None
        expected = argv[4] or None
        consecutive = int(argv[5])
        threshold = int(argv[6]) if len(argv) > 6 else DEFAULT_ERROR_THRESHOLD
        observed_fp = (argv[7] or None) if len(argv) > 7 else None
        expected_fp = (argv[8] or None) if len(argv) > 8 else None
        out = decide(rc, observed, expected, consecutive, threshold,
                     observed_token_fp=observed_fp,
                     expected_token_fp=expected_fp)
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
