"""Decide what /start does with a runner-claim acquire exit code.

WHY THIS EXISTS (2026-09-10, from a live paying-member incident).

A member's Vinheim resident agent ran for a billed hour and did nothing. Its own
end-of-run mail said: *"Attempted runner-claim acquire and it failed because the
daemon was unreachable (ACQUIRE_RC=1). Agent remains IDLE."* It then spent the
hour re-reading its context and mailing the owner the same three options twice,
tripping a burn-rate alarm at 21x baseline and billing $1.96 for a stopped agent.

THE AGENT WAS NOT WRONG TO BE CONFUSED — the instructions had a hole.

  * `runner-claim.sh` header, rc contract: "1 — daemon returned an error
    (caller decides; the three mutating call sites fail open)."
  * `core/config/start-phase-c.md` (the UNINITIALIZED flow): "HALT ON
    ACQUIRE_RC=4 ... Any OTHER non-zero rc is FAIL-OPEN: log and PROCEED."
  * `.claude/skills/start/SKILL.md` (the IDLE->autonomous flow — the path a
    resident actually takes): documents `ACQUIRE_RC=4 + reducer_only` and
    `ACQUIRE_RC=4 otherwise`, and **says nothing at all about any other
    non-zero rc**.

So the rule covering the COMMON transient failure lived only in a different file
describing a different flow. An LLM executing the primary path saw a failed
acquire, found no sanctioned branch, and stopped — reading the surrounding bold
HALT/refusal language as the closest match. The rare dangerous branch (rc=4) was
emphasised AND pinned by a test; the common benign branch was neither.

That is rb-189's class (declared-but-unenforced capability: skill docs are not
workflow enforcement) with guard-399's remedy (write the bash path before an
"LLM must do X at step N" instruction is worth anything). This module is that
path: the caller runs it instead of interpreting an integer against prose.

POLICY IS INHERITED, NOT INVENTED. rc=4 is the only refusal, exactly as
start-phase-c.md already states; every other non-zero rc fails open. Notably
rc=2 (bad usage) also fails open, because that is what the existing contract
says — it is surfaced loudly in the reason string rather than silently promoted
to a halt, since changing that policy is a separate decision from closing this
hole.

`decide()` is pure: no filesystem, environment, clock or subprocess, so every
branch is reachable from a test without a daemon or a second machine
(guard-1165).
"""

import sys

# Verdicts. The caller switches on these; they are the whole API surface.
PROCEED = "proceed"
JOIN_AS_WORKER = "join-as-worker"
REFUSE = "refuse"

#: The ONLY exit code that refuses the autonomous start. Everything else
#: fails open. See the module docstring — this mirrors start-phase-c.md
#: rather than establishing new policy.
REFUSE_RC = 4


def decide(rc, reducer_only=False):
    """Map a `runner-claim.sh acquire` exit code to a /start verdict.

    Returns ``(verdict, reason)``. ``verdict`` is always one of
    :data:`PROCEED`, :data:`JOIN_AS_WORKER` or :data:`REFUSE`, so the caller
    never has to handle an unmapped integer — which is precisely the gap that
    stranded a paying member's agent.

    ``reducer_only`` is the `/start --reducer-only` flag: it converts the
    peer-holds case from an automatic worker-join into a refusal.
    """
    try:
        rc = int(rc)
    except (TypeError, ValueError):
        # An unreadable rc is not evidence that a peer holds the claim, and
        # refusing on it would strand the agent for the same reason this
        # module exists. Fail open and say why.
        return PROCEED, f"unreadable acquire rc {rc!r} — failing open"

    if rc == 0:
        return PROCEED, "claim acquired (or backend no-op)"

    if rc == REFUSE_RC:
        if reducer_only:
            return REFUSE, "a peer holds a live claim and --reducer-only was passed"
        return JOIN_AS_WORKER, "a peer holds a live claim — joining as a second body"

    # Every other non-zero rc. rc=1 is the measured case: the daemon returned
    # an error / was unreachable. Failing open here is what keeps a transient
    # daemon fault from silently converting into an hour of paid idleness.
    return PROCEED, f"acquire rc={rc} is not a refusal — failing open and proceeding"


def main(argv=None):
    """Print the verdict on stdout and the reason on stderr; exit 0 always.

    Exit status deliberately carries NO signal: the verdict is the product,
    and an exit code here would be a second thing to interpret — which is the
    failure mode this module removes. A caller that ignores stderr still gets
    a correct verdict on stdout.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    reducer_only = "--reducer-only" in argv
    positional = [a for a in argv if not a.startswith("--")]
    rc = positional[0] if positional else None

    verdict, reason = decide(rc, reducer_only=reducer_only)
    print(verdict)
    print(f"[runner-claim-acquire] {verdict}: {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
