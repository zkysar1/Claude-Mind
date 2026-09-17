#!/usr/bin/env bash
# pending-questions-close.sh — safely close a pending question in ANY agent's
# session/pending-questions.yaml via the authoritative storage backend, under a
# lock (the cross-agent CLOSE half that  found missing). Thin wrapper
# over pending_questions_close.py; the Python does the locked read-modify-write.
#
# Usage:
#   pending-questions-close.sh --agent <name> --id <qid> \
#       --answered-by <who> [--rationale <text>] [--dry-run]
#   pending-questions-close.sh --pq-path <abs-path> --id <qid> \
#       --answered-by <who> [--rationale <text>] [--dry-run]   # test hook
#
# --amend rewrites the answer of an ALREADY-TERMINAL question (requires
# --rationale); the prior answer is appended to `superseded_answers` and is
# never destroyed. Without it a terminal question returns action=already_terminal
# and writes NOTHING — so an rc=0 is not evidence an amendment was recorded.
# Use only when the governing decision POSTDATES the stored answer (guard-5868);
# a resolved question is an answer store later readers trust (guard-6282).
#
# Exit: 0 closed/amended/already-terminal/dry-run · 2 input error (incl. --amend
#       without --rationale) · 3 not found · 4 lock failure ·
#       5 wrote-but-verify-failed.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/_paths.sh"
# OwnCloudBackend.from_env() (used by get_backend when STORAGE_BACKEND=own-cloud)
# needs WORLD/META roots in the env; _paths.sh exports WORLD_DIR/META_DIR.
export WORLD_PATH="${WORLD_DIR:-}" MIND_WORLD="${WORLD_DIR:-}"
export META_PATH="${META_DIR:-}" MIND_META="${META_DIR:-}"
python3 "$SCRIPT_DIR/pending_questions_close.py" "$@"
