#!/usr/bin/env bash
# Board triage: scan the unhandled posts addressed to this agent and dispose of
# each with exactly one disposition (FYI, DO_NOW, GOAL, NOTED_NOT_FILING).
# Pure CLI wrapper around board_triage.py: no rt_call and no daemon claim. The
# board reads, replies and goal filings it performs go through board-read.sh,
# board-post.sh and aspirations-add-goal.sh, the daemon-routed parts
# (.claude/rules/no-python-cli-fallback.md).
#
# Usage: bash core/scripts/board-triage.sh scan [--begin-iteration] [--since <hours>] [--json]
#        bash core/scripts/board-triage.sh dispose --msg-id <id> --disposition <d> [flags]
# The flags are defined ONCE, in board_triage.py (argparse refuses an unknown
# flag with exit 2): `board-triage.sh dispose --help` lists the per-disposition set.
# The agent is --agent <a> or MIND_AGENT (injected by the bash hook).
# -b.
set -euo pipefail

_SELF="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "$_SELF/_paths.sh"

exec python3 "$_SELF/board_triage.py" "$@"
