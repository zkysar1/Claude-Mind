"""Catastrophic-shrink refusal for long-lived goal prose fields ().

WHY THIS EXISTS
---------------
On 2026-08-21 a world goal's 36,904-char `description` was overwritten with
3,467 chars belonging to an UNRELATED goal. Every layer reported success: the
wrapper exited rc=0 and echoed the full (now-wrong) record, the daemon wrote
cleanly, the changelog recorded an ordinary edit. Nothing warned, because
nothing in the write path had any opinion about a field getting 10x smaller.

The write itself was recoverable — `.history` held a designed copy-on-write
pre-image (snapshot `2026-08-21T04-22-40` -> blob `3f597ef0...`, from which the
36,904 chars were recovered intact). So this gate is NOT the last line of
defense; it is the line that makes the loss VISIBLE at write time, when the
author still knows what they meant, instead of hours later when someone
notices the prose is about the wrong goal.

The mechanism it defends against is documented and recurrent — guard-2525 /
guard-2444 / guard-1691: `aspirations-update-goal.sh` has no `--append`, an
unrecognized flag is silently DROPPED, and the remaining value REPLACES the
field wholesale at rc=0. Read-modify-write against a truncated read
(guard-1251) produces the same shape.

THRESHOLD, AND HOW IT WAS CHOSEN (guard-1562 — never widen blindly)
-------------------------------------------------------------------
Measured against the live corpus before choosing, asking "who NEWLY gets
refused", not "does this catch the incident":

  description   63 update pairs, 45 with old >= 2000 chars
                -> exactly ONE refused at every threshold 0.50 / 0.40 / 0.25:
                   the g-115-105 incident itself (36904 -> 3467, ratio 0.09).
                   EVERY other description update GREW.
  outcome_note  40 pairs, 30 fresh writes, 8 with old >= 2000 chars
                -> ZERO refused below 0.50. Closest legitimate shrink is
                   g-115-2571 (7784 -> 4250, ratio 0.55).

0.25 sits 2.2x below the closest legitimate shrink ever observed and still
catches the incident 2.8x over. The `old >= 2000` floor keeps every short
field — titles-as-spec, one-line notes, early drafts — entirely out of scope,
which is where wholesale rewrites are normal and cheap.

RETIREMENT CRITERION, recorded at birth (guard-769)
---------------------------------------------------
Retire this gate when EITHER holds:
  (a) The wholesale-replace mechanism is gone — i.e. `aspirations-update-goal.sh`
      grows a real append mode AND unrecognized flags become a hard error, so a
      truncating write can no longer be produced by a dropped flag; or
  (b) Telemetry shows override/(block+override) > 0.5 over 20+ firings, meaning
      the threshold is refusing legitimate work more often than catching loss.
Until one of those is true, a `noop`-heavy firing log is the gate WORKING —
this predicate is designed to be silent, because the event it catches is rare
and expensive rather than common and cheap.

DESIGN NOTES
------------
No try/except anywhere in this module, deliberately. guard-3803: a gate's
fail-open handler also covers its own deny-message construction, so a bug while
COMPOSING a refusal silently converts the refusal into an approval. The
predicate is pure length arithmetic behind explicit isinstance checks, so there
is no dependency to fail and no fail-open surface to get wrong. Callers must
NOT wrap it in a bare `except: pass`.

Public API:
    evaluate(field, old_value, new_value) -> dict

Return shape (every branch sets `decision_path` — guard-502):
    {
      "blocked": bool,
      "message": str | None,      # pre-formatted refusal text; None when not blocked
      "field": str,
      "old_len": int,
      "new_len": int,
      "ratio": float | None,      # new_len / old_len, None when not computable
      "decision_path": str,       # unique per branch, for gate telemetry
    }
"""
from __future__ import annotations

# Fields whose prose accumulates across sessions and whose loss is expensive.
# Deliberately NOT every string field: a `title` or `defer_reason` is meant to
# be rewritten wholesale, and gating those would refuse ordinary work.
GUARDED_FIELDS = ("description", "outcome_note")

# Below this, wholesale rewrites are normal. Measured: no legitimate shrink
# below the ratio threshold exists above this floor in the live corpus.
MIN_OLD_CHARS = 2000

# Refuse when the new value is smaller than this fraction of the old one.
MAX_SHRINK_RATIO = 0.25


def evaluate(field, old_value, new_value) -> dict:
    """Decide whether this field write destroys long-lived prose.

    Args:
        field:     the field name being written.
        old_value: the value currently on the record (pre-mutation).
        new_value: the incoming value.

    Pure — reads no files, no env, no clock. Never raises on ordinary input.
    """
    def _result(blocked, decision_path, message=None,
                old_len=0, new_len=0, ratio=None):
        return {
            "blocked": blocked,
            "message": message,
            "field": field,
            "old_len": old_len,
            "new_len": new_len,
            "ratio": ratio,
            "decision_path": decision_path,
        }

    if field not in GUARDED_FIELDS:
        return _result(False, "field-not-guarded")

    # Non-str on either side: a clear (None), a JSON structure, a numeric.
    # Out of scope — this gate has an opinion about PROSE only.
    if not isinstance(old_value, str) or not isinstance(new_value, str):
        return _result(False, "non-string-operand")

    old_len = len(old_value)
    new_len = len(new_value)

    if old_len < MIN_OLD_CHARS:
        return _result(False, "old-below-floor", old_len=old_len, new_len=new_len)

    ratio = new_len / old_len

    if ratio >= MAX_SHRINK_RATIO:
        return _result(False, "ratio-within-tolerance",
                       old_len=old_len, new_len=new_len, ratio=ratio)

    message = (
        f"field_shrink_blocked: writing `{field}` would shrink it from "
        f"{old_len} chars to {new_len} ({ratio:.0%} of the original, floor is "
        f"{MAX_SHRINK_RATIO:.0%}). A drop this large is almost always a "
        f"read-modify-write against a truncated read, or a wholesale REPLACE "
        f"where an append was intended — `aspirations-update-goal.sh` has no "
        f"--append, and an unrecognized flag is dropped silently while the "
        f"value replaces the field at rc=0 (guard-2525 / guard-1251). "
        f"Re-read the field, compose the full intended value, and retry. "
        f"If the shrink is genuinely intended (a deliberate condense), pass "
        f"--override-shrink \"<justification>\"."
    )
    return _result(True, "shrink-refused", message=message,
                   old_len=old_len, new_len=new_len, ratio=ratio)
