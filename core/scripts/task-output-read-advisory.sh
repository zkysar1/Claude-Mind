#!/usr/bin/env bash
# task-output-read-advisory.sh — PreToolUse[Read] ADVISORY (G21 Layer B,
# built 2026-08-21; retrieval-triggers.md G21).
#
# WHY: a <task-notification> reports the exit code of the process the harness
# launched, which is routinely NOT the command's verdict — a trailing pipe
# substitutes the pipe's status (guard-1150), a self-classifying runner writes
# its verdict to a LOG not to $?, and a fail-open wrapper exits 0 by contract
# (guard-1431 / guard-1341 / guard-1096). Four guardrails encode this and it
# still landed a 4th time in one session, so it is a RETRIEVAL gap: nothing
# surfaced those four at the moment of acceptance. The notification itself is
# not a tool call, so no hook can fire there; the one chokepoint that IS a
# tool call is the Read of the task's output file. This advisory fires exactly
# there and nowhere else.
#
# NEVER BLOCKS: permissionDecision is always "allow"; every path exits 0.
# The prefilter is a raw-stdin substring test — a false fire on an unrelated
# path containing "/tasks/…output" costs one banner, never a block.
set -uo pipefail
IN="$(cat 2>/dev/null || true)"
case "$IN" in
  *'/tasks/'*'.output'*) ;;
  *) exit 0 ;;
esac
MSG='[task-verdict advisory — G21] You are reading a background task output file. The task notification exit code is the HARNESS process exit, routinely NOT the command verdict: a trailing pipe substitutes the pipe status (guard-1150); self-classifying runners write their verdict to the LOG — read VERDICT:/RUNNER_EXIT= lines, not rc (guard-1431); fail-open wrappers exit 0 by contract (guard-1341, guard-1096). Read to the END of this file (or the log it names) for the real verdict before claiming success or failure.'
echo "$MSG" >&2
# Structured channel — the only one that reaches the model. Shape mirrors
# pre-edit-context-gate.sh's measured payload verbatim.
printf '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow", "permissionDecisionReason": %s, "additionalContext": %s}, "systemMessage": %s}\n' \
  "$(printf '%s' "$MSG" | sed 's/"/\\"/g; s/^/"/; s/$/"/')" \
  "$(printf '%s' "$MSG" | sed 's/"/\\"/g; s/^/"/; s/$/"/')" \
  "$(printf '%s' "$MSG" | sed 's/"/\\"/g; s/^/"/; s/$/"/')"
exit 0
