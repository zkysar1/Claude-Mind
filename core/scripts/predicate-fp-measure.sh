#!/usr/bin/env bash
# predicate-fp-measure.sh — wrapper for predicate_fp_measure.py (guard-350:
# SKILL.md pseudocode must invoke a .sh, never a bare python script).
#
# Measures a CANDIDATE GATE PREDICATE before it ships. Two phases, because the
# decisive step is a judgment call that no script can make:
#
#   sample : corpus -> predicate -> denominator + match count + FIRE RATE + a
#            sample to classify. Emits verdict "unclassified", never a pass.
#   score  : reads the classification on stdin -> FP RATIO + verdict.
#
# Examples:
#   bash core/scripts/predicate-fp-measure.sh sample \
#     --predicate 'retriev(e|al)' --corpus goals --sample-size 20
#
#   bash core/scripts/predicate-fp-measure.sh sample \
#     --predicate '2>/dev/null' --corpus files --path 'core/scripts/*.sh'
#
#   echo '{"corpus_size":6155,"match_count":21,"classified":[
#          {"unit_id":"g-1","verdict":"narration"},
#          {"unit_id":"g-2","verdict":"genuine"}]}' \
#     | bash core/scripts/predicate-fp-measure.sh score
#
# Exit codes pass through: 0 measured, 1 refusal (empty corpus / empty
# classification), 2 input error.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/_paths.sh"

cd "${PROJECT_ROOT}"

#  fix: under Git Bash on Windows, $(cd ... && pwd) returns POSIX
# form /c/... Windows python3 misinterprets that as drive C: with a literal
# subdir c/, yielding FileNotFoundError on C:\c\...\predicate_fp_measure.py.
# Convert to Windows-native form before exec. Linux/macOS lack cygpath and
# fall through with SCRIPT_DIR unchanged (POSIX paths work natively).
if command -v cygpath >/dev/null 2>&1; then
    SCRIPT_DIR_NATIVE="$(cygpath -w "$SCRIPT_DIR")"
else
    SCRIPT_DIR_NATIVE="$SCRIPT_DIR"
fi

exec python3 "$SCRIPT_DIR_NATIVE/predicate_fp_measure.py" "$@"
