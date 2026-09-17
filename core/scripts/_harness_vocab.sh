#!/usr/bin/env bash
# _harness_vocab.sh -- the loop's re-entry tools as the HOSTING harness spells them.
# SOURCE this (never execute it). It sets, for the current harness:
#
#   HC_HARNESS        claude-code | zakcode | unknown
#   HC_SKILL_TOOL     Skill | use_skill
#   HC_WAKEUP_TOOL    ScheduleWakeup | schedule_wakeup
#   HC_LOOP_CALL      Skill(aspirations) with args='loop'  | use_skill(name='aspirations', args='loop')
#   HC_LOOP_REF       Skill(aspirations)                   | use_skill(aspirations)          (a mention)
#   HC_LOOP_REF_Q     Skill('aspirations')                 | use_skill(name='aspirations')
#   HC_SPARK_REF      Skill(aspirations-spark)             | use_skill(aspirations-spark)
#   HC_WORKER_REF     Skill(worker-loop)                   | use_skill(worker-loop)
#   HC_WORKER_CALL_Q  Skill('worker-loop')                 | use_skill(name='worker-loop')
#   HC_DEADMAN_ARM    ScheduleWakeup(prompt='<<autonomous-loop-dynamic>>', delaySeconds=600)
#                                                          | schedule_wakeup(prompt='<<autonomous-loop-dynamic>>', delaySeconds=600)
#
# WHY (2026-09-17). Every terminal imperative the framework prints -- the stop
# hook's BLOCK reason, ITERATION COMPLETE, the recurring close, the
# state-mismatch landing -- was written in Claude Code's vocabulary. Promoted
# unchanged to a Mind whose vessel is zakcode, those lines named tools the
# model's tool list does not show, and a small model answered them in prose
# for hours while the stop hook BLOCKed in the same foreign names. The spelling
# has ONE owner, _harness_caps.py (env-only, pure); this file is its shell
# reach, so no printing site carries a vocabulary of its own. The _Q variants
# exist only because two pre-existing Claude Code lines quote the skill name
# and their bytes are pinned; zakcode's re-entry parser reads either spelling.
#
# FAIL-OPEN: when the resolver cannot run, Claude Code's spelling stands. The
# defaults below are pinned byte-identical to the resolver's claude-code output
# by test_harness_caps.py, so the two copies cannot drift apart silently.
# Idempotent: a second source is free.
[ -n "${HC_LOOP_CALL:-}" ] && return 0
_hc_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# ${PY:-python3}: stop-hook.sh resolves a launcher into PY (`py -3` on Windows,
# hence the unquoted expansion); every other sourcer has sourced _paths.sh,
# where python3 is the shimmed, invocation-safe path (python-invocation.md).
_hc_out="$(${PY:-python3} "$_hc_dir/_harness_caps.py" --loop-vocab-sh 2>/dev/null)" || _hc_out=""
# A bare python3 on a Windows box can be the Store stub (python-invocation.md);
# `py -3` is the launcher that works there. Tried only when the first form
# produced nothing, so a POSIX box pays no extra spawn.
if [ -z "$_hc_out" ] && command -v py >/dev/null 2>&1; then
    _hc_out="$(py -3 "$_hc_dir/_harness_caps.py" --loop-vocab-sh 2>/dev/null)" || _hc_out=""
fi
if [ -n "$_hc_out" ]; then
    eval "$_hc_out"
fi
unset _hc_dir _hc_out
: "${HC_HARNESS:=unknown}"
: "${HC_SKILL_TOOL:=Skill}"
: "${HC_WAKEUP_TOOL:=ScheduleWakeup}"
: "${HC_LOOP_CALL:=Skill(aspirations) with args='loop'}"
: "${HC_LOOP_REF:=Skill(aspirations)}"
: "${HC_LOOP_REF_Q:=Skill('aspirations')}"
: "${HC_SPARK_REF:=Skill(aspirations-spark)}"
: "${HC_WORKER_REF:=Skill(worker-loop)}"
: "${HC_WORKER_CALL_Q:=Skill('worker-loop')}"
: "${HC_DEADMAN_ARM:=ScheduleWakeup(prompt='<<autonomous-loop-dynamic>>', delaySeconds=600)}"
