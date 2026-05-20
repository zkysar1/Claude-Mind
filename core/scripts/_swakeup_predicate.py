"""Single source of truth: "would this ScheduleWakeup prompt be rejected?"

Imported by:
  - schedule-wakeup-gate.py  (Layer A — enforce at PreToolUse)
  - aspirations-rejection-audit.py  (Layer C — observe in transcripts)

CRITICAL: do not duplicate this predicate inline anywhere else. If a third
caller needs the same check, import it from here. The two layers MUST agree
on what "bad" means or the detective layer's signal diverges from the gate's
enforcement.
"""


def is_bad_slash_prefix(prompt) -> bool:
    """True when `prompt` starts with `/` and the first whitespace-delimited
    token is not literally `/loop`.

    Per the ScheduleWakeup tool documentation:
      - `<<autonomous-loop-dynamic>>` sentinel (and similar) doesn't start
        with `/` -> returns False (passes through trivially).
      - `/loop ...` is the ONLY legitimate slash-prefixed wakeup prompt
        (user-initiated /loop continuation) -> returns False.
      - Anything else starting with `/` (e.g., `/aspirations`, `/boot`,
        `/respond`) would get re-parsed as user input when the wakeup fires
        and rejected at Claude Code's user-invocable=false gate -> True.

    Non-string inputs return False (fail-open at the type boundary).
    Leading whitespace is NOT stripped — `"  /aspirations"` returns False
    because `.startswith("/")` returns False. That matches Claude Code's
    slash-resolver which also anchors on the first character.
    """
    if not isinstance(prompt, str) or not prompt.startswith("/"):
        return False
    return prompt.split(None, 1)[0] != "/loop"
