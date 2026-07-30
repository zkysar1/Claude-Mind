#!/usr/bin/env python3
"""PreToolUse[Bash] ADVISORY hook -- trailing `echo` masks a backgrounded exit.

Layer B of the guard-1150 defense (goal g-115-3511). Layer A is the guardrail
text, and it demonstrably did not hold: guard-1150 describes this failure
exactly, was retrieved during the very session it failed to prevent, and sat at
`times_helpful: 0, times_active: 0`. Honour-system text loses inside a 40-entry
retrieval dump (guard-1421). This layer delivers the same lesson at the moment
of use, which is the one thing a stored entry cannot do.

MECHANISM. A shell's exit status is the status of its LAST executed statement.
So a trailing `echo` -- typically added to make the real status VISIBLE in a log
-- becomes the status itself:

    false > /dev/null 2>&1; echo "EXIT=$?"          -> shell exits 0
    false > /dev/null 2>&1; rc=$?; echo "$rc"; exit $rc  -> shell exits 1

WHY BACKGROUNDED ONLY. This gate deliberately fires only when
`run_in_background` is true. For a backgrounded command the harness surfaces
the exit status as the task-completion notification, and that notification is
frequently the ONLY signal anyone sees. A masked status there is always a real
loss of signal, never a harmless log line -- which is what makes the detection
high-precision rather than a blanket complaint about a ubiquitous shell idiom.
Foreground commands show their own output and are left alone.

RECURRENCES (why a gate rather than more prose):
  2026-07-16  a gradle BUILD FAILED reported as exit 0
  2026-07-27  three background jobs reported exit 0; real exits were 1, 4, 1 --
              after which the agent told the user the HARNESS was untrustworthy
              (retracted; see guard-1644). Same session, a `push exit=0` was
              actually `tail`'s exit and the push had been rejected.

WHICH SEPARATORS MASK. Not all do, and the gate only fires on the ones that:
    cmd ; echo x        masks -- echo always runs, echo's 0 wins
    cmd || echo x       masks -- echo runs precisely when cmd FAILED
    cmd && echo x       does NOT mask -- on failure && short-circuits and cmd's
                        nonzero status is preserved, so this is safe
    cmd | tail          a DIFFERENT class (PIPESTATUS, guard-626/776); out of
                        scope here, though the advisory names the idiom for it

SAFETY: advisory, fail-open, ALWAYS exit 0, NEVER writes to stdout. Claude Code
interprets hook stdout as a deny payload, so the banner goes to stderr only
(same posture as pre-edit-context-gate.sh). Anything unparseable -- heredocs,
unbalanced quotes, an undeterminable final statement -- approves silently.

Lineage: g-115-3511 (this hook); guard-1150 (the rule); guard-1644 (do not
blame shared infrastructure before reproducing it); guard-1421 (a truncated
entry is unread -- why Layer A alone was insufficient).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hook_helpers import (  # noqa: E402
    approve_no_mutation,
    stdin_json_or_approve,
)

# Statement heads whose exit status is ~always 0 and which are therefore used
# as log lines rather than as the meaningful result of the command.
REPORTING_HEADS = ("echo", "printf")

# Separators that let the trailing statement's status become the shell's.
# `&&` is deliberately absent: it short-circuits on failure, preserving the
# real nonzero status.
MASKING_SEPARATORS = (";", "\n", "||")

_OVERRIDE = "TRAILING_ECHO_GATE_OVERRIDE"

ADVISORY = (
    "[trailing-echo-exit-gate] ADVISORY (guard-1150): this BACKGROUNDED command's "
    "last statement is `{head}`, so the shell -- and therefore the task-completion "
    "notification you receive -- reports that statement's exit status (almost "
    "always 0), NOT the command's. A real failure will be reported to you as "
    "success.\n"
    "    last statement: {stmt}\n"
    "    reached via   : {sep}\n"
    "Correct idiom -- keeps the log line AND a truthful status:\n"
    "    cmd > log 2>&1; rc=$?; echo \"EXIT=$rc\"; exit $rc\n"
    "    # pipes:  rc=${{PIPESTATUS[0]}}\n"
    "This is advisory; the command still runs. Suppress with " + _OVERRIDE + "."
)


def emit_advisory(message: str) -> None:
    """Deliver a NON-BLOCKING advisory that actually reaches the model.

    WHY NOT PLAIN STDERR (measured 2026-07-28, g-115-3511). The obvious
    advisory shape -- write to stderr, exit 0 -- was built first and verified
    live: the hook-fire sentinel confirmed the hook RAN on a real backgrounded
    command, and the banner never reached the model. That matches Claude Code's
    hook contract: on exit 0, stderr goes to the user's terminal, not to Claude
    (only a BLOCKING hook feeds stderr back to the model). An advisory on
    stderr therefore fires, costs latency, and communicates nothing to the one
    reader who needs it -- failing silently in the one way nobody notices.

    So the advisory rides the structured channel instead. The decision stays
    `permissionDecision: "allow"` -- explicitly NOT a deny, the command still
    runs, nothing is blocked -- and the message is attached to every non-
    blocking field the payload accepts. `allow` + `permissionDecisionReason`
    ALONE was probed and did NOT deliver; the delivered shape is the one below.
    See the field-by-field probe table at the payload.

    stderr is ALSO written, deliberately: it is what a human watching the
    terminal sees, and it keeps the gate useful if the structured reason is
    ever dropped. Belt and braces, because the failure mode being defended
    against is precisely a channel that silently carries nothing.
    """
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": message,
            "additionalContext": message,
        },
        "systemMessage": message,
        # WHY ALL FOUR CHANNELS (measured 2026-07-28, ). Five live
        # probes against a real backgrounded command, hook-fire sentinel
        # confirming the hook RAN on every one:
        #   allow + reason only ................... not delivered
        #   + additionalContext + systemMessage ... DELIVERED (system-reminder)
        #   + additionalContext only .............. not delivered
        #   + additionalContext only, new text .... not delivered
        #   + systemMessage only .................. not delivered
        # Delivery is NOT a function of which field is set: the predicate emits
        # for every probe and the hook fires every time. The one delivery was
        # the run carrying BOTH fields; the parsimonious reading is that hook-
        # injected context is deduped per session, which makes every later
        # probe uninformative FROM INSIDE THE SAME SESSION -- a negative there
        # is consistent with both "wrong field" and "already delivered once".
        # That ambiguity is unresolvable here, so this ships the exact shape
        # observed to work rather than a narrower guess. Cost of the extra keys
        # is a few bytes; cost of guessing wrong is a silent gate. Narrowing is
        # tracked in  (needs a FRESH session per probe).
    }
    sys.stderr.write(message + "\n")
    print(json.dumps(payload))


def _has_heredoc(command: str) -> bool:
    """True when a heredoc is present. Its body can contain anything, so the
    top-level statement scan below cannot be trusted -- fail open instead."""
    return "<<" in command


def split_top_level(command: str):
    """Split into (separator, statement) pairs at top-level shell separators.

    Quote-aware: `;` and `||` inside quotes are literal text, not separators.
    Returns None when the command cannot be scanned confidently (unbalanced
    quotes), so the caller can fail open rather than guess.

    Deliberately hand-rolled rather than shlex: shlex.split discards the
    separators, and the separator is exactly what decides whether the trailing
    statement masks (`;`/`||`) or not (`&&`).
    """
    pairs = []           # (separator-that-preceded, statement-text)
    buf = []
    sep = ""             # separator that introduced the statement being built
    i = 0
    quote = None         # None | "'" | '"'
    n = len(command)
    while i < n:
        ch = command[i]
        if quote:
            if quote == '"' and ch == "\\" and i + 1 < n:
                buf.append(ch); buf.append(command[i + 1]); i += 2; continue
            if ch == quote:
                quote = None
            buf.append(ch); i += 1; continue
        if ch in ("'", '"'):
            quote = ch; buf.append(ch); i += 1; continue
        if ch == "\\" and i + 1 < n and command[i + 1] == "\n":
            i += 2; continue                      # line continuation
        two = command[i:i + 2]
        if two in ("||", "&&"):
            pairs.append((sep, "".join(buf))); buf = []; sep = two; i += 2; continue
        if ch in (";", "\n"):
            pairs.append((sep, "".join(buf))); buf = []; sep = ch; i += 1; continue
        buf.append(ch); i += 1
    if quote:
        return None                                # unbalanced -- out of reach
    pairs.append((sep, "".join(buf)))
    return pairs


def _statement_head(stmt: str) -> str | None:
    """First word of a statement, ignoring leading redirections/whitespace."""
    s = stmt.strip()
    if not s or s.startswith("#"):
        return None
    m = re.match(r"[A-Za-z_][A-Za-z0-9_.-]*", s)
    return m.group(0) if m else None


def analyze(command: str):
    """Return (head, statement, separator) when the final statement masks the
    exit status, else None. None also means 'undeterminable' -- fail open."""
    if not command or _has_heredoc(command):
        return None
    if _OVERRIDE in command:
        return None
    pairs = split_top_level(command)
    if pairs is None:
        return None

    # Drop trailing blank / comment-only statements (a trailing `;` or newline
    # produces an empty final segment).
    meaningful = [(sep, s) for sep, s in pairs if _statement_head(s) is not None]
    if len(meaningful) < 2:
        # Nothing precedes the echo, so nothing is being masked.
        return None

    sep, stmt = meaningful[-1]
    if sep not in MASKING_SEPARATORS:
        return None                                # `&&` preserves failure
    head = _statement_head(stmt)
    if head not in REPORTING_HEADS:
        return None
    return head, stmt.strip(), ("newline" if sep == "\n" else sep)


def main() -> None:
    payload = stdin_json_or_approve()
    if not isinstance(payload, dict):
        approve_no_mutation()
    if payload.get("tool_name") != "Bash":
        approve_no_mutation()

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        approve_no_mutation()

    # The scope restriction that makes this precise. bash-agent-inject.py
    # reads the same key, confirming hooks do receive it.
    if tool_input.get("run_in_background") is not True:
        approve_no_mutation()

    command = tool_input.get("command")
    if not isinstance(command, str) or not command:
        approve_no_mutation()

    found = analyze(command)
    if found:
        head, stmt, sep = found
        if len(stmt) > 160:
            stmt = stmt[:157] + "..."
        emit_advisory(ADVISORY.format(head=head, stmt=stmt, sep=sep))
    approve_no_mutation()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # Bottom catch-all: a broken hook must never block legitimate work.
        pass
