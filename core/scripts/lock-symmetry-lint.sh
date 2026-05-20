#!/usr/bin/env bash
# lock-symmetry-lint.sh
#
# Scans core/scripts/*.sh and world/scripts/*.sh for acquire/release pairs
# on the mkdir/rm-rf lock pattern and surfaces sites whose release predicate
# is not a superset of the acquire predicate — the class of bug that
# stranded .autocompact-serialize-lock (rb-356 fresh-eyes review).
#
# Structural surfacing, not semantic proof: emits both predicate chains
# per pair so the reader can judge symmetry. True superset-of-predicates
# decision is undecidable on arbitrary bash.
#
# Elevates guard-328 from LLM-review-time to tool-enforced.
#
# Exit 0: no acquire-without-release pairs found.
# Exit 1: one or more acquire sites have no matching release (lint-style).
# --force: always exit 0 (for WIP work).
# --check-pair <acquire-script> <release-script>: verify one specific pair.
#
# Wired into PostToolUse[Write|Edit|MultiEdit] in .claude/settings.json.

set -u
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/lock-symmetry-lint.py" "$@"
