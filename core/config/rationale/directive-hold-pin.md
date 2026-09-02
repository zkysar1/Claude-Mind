# Directive-Hold Pin — why a user interrupt needs a signal of its own

Goal: `g-306-386`. Mechanism: `core/scripts/interrupt_task.py` (+ `interrupt-task.sh`).
Loop wiring: `aspirations/SKILL.md` Phase -1.35, immediately after the stop check.

## The field trace (coach deployment, 2026-08-30, measured twice)

A human was waiting live, both times.

A mid-loop user directive arrived at interrupt priority — an OAuth consent task. It was
**handled correctly in the turn it arrived**. That is the part worth sitting with: nothing
about the directive-handling logic failed. The turn ended, the loop re-entered, and the
reducer resumed strategic-scan/precheck as if nothing were outstanding. The task was gone.

It happened a second time the same day. On one of the two, a **single-use consent code sat
unconsumed in the pane** until an operator noticed and backstopped it by hand.

## The asymmetry

`stop-requested` survives turn boundaries. A directive does not. The difference is not
priority or intent — it is that one of them is **written to disk** and every loop re-entry
checks for it, and the other lives only in the turn that received it.

So the loop has a durable mechanism for *"stop what you are doing"* and none for *"do this
first."* Those are the same mechanism with opposite polarity, and only one was built.

## Design

A WM slot, `interrupt_task_open`, holding `{task, opened_at, source, opened_by, sid}`.

**Why a WM slot and not a session signal.** Session signals are payload-free marker files
behind a `VALID_SIGNALS` allowlist — `session.py cmd_signal_set` touches an empty file. A
pin needs its one-line text to survive *with* it, or the next turn knows only that
something is open and not what. A signal could carry the flag and a slot the payload, but
two artifacts that must be kept in sync is one more thing to go stale; the slot alone is
sufficient and atomic.

**Why it is in `RESET_SURVIVING_SLOTS`.** A pinned task is a standing human obligation, not
session bookkeeping. A consolidation-time `wm-reset` dropping it would reproduce the exact
defect the pin exists to fix, just on a longer timescale.

**Why it sits after Phase -1.4, not before.** A real `/stop` must still win. A pin that
could wedge a stop would be a worse failure than the one it prevents.

**Why it fails open.** Every error path returns "no pin," and that is the polarity that
*loses* a task — the very defect this module addresses. It is still correct: a plumbing
fault here must not wedge a healthy loop (`guard-1562`; the `reducer_self_fence`
HOLD-on-ambiguity precedent). The mitigation is that `check` prints **loudly** on a
malformed slot, so a broken pin is visible in the turn it breaks instead of being
byte-identical to "nothing pinned."

**Why `open` refuses a second pin.** Two concurrent human obligations silently overwriting
each other is worse than one that has to be acknowledged. `--force` exists and is explicit.

## A bug the self-test caught that inspection did not

First lifecycle run reported `MALFORMED PIN` on a healthy slot and `WRITE DID NOT VERIFY`
on a write that **had actually landed** — confirmed by an independent `wm-read.sh` read.

Cause: `wm.resolve_slot(data, path)` returns a **locator** `(parent_dict, key,
is_top_level)`, not a value. Reading it as a value yields a 3-tuple, which `decide()`
correctly rejected as malformed. The duplicate-pin guard then failed open and the second
`open` silently overwrote the first.

This is the `guard-1755` class: **the read-back instrument was the broken part**, so the
tool reported failure on success and its own guard stopped working. Worth noting the shape
— an API that returns a locator where a value is expected produces a well-formed wrong
answer, not an exception.

## Promotion

Developed at the dev origin per `promotion-cycle.md`; flows downstream via the promotion train.
Nothing about the mechanism is coach-specific; the field trace is.
