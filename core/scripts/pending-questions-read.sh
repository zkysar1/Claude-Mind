#!/usr/bin/env bash
# domain-leak-exempt: no domain terms; shape-tolerant reader wrapper.
#
# pending-questions-read.sh — the ONE shape-tolerant read entry point for
# pending-questions.yaml. User-facing consumers (agent-completion-report,
# open-questions, respond) MUST route through this instead of hand-rolling a
# naive top-level `status == "pending"` scan, which silently SKIPS entries
# nested inside a `{questions: [...]}` wrapper element (shapes A and B below).
#
# The on-disk file tolerates three container shapes (rb-1786 — a paired
# reader/writer over one store must tolerate the same shapes as its sibling
# pending-questions-add.sh / pending-questions-sweep.py):
#   shape A:   {"questions": [ {...}, ... ]}            dict wrapper
#   shape B:   [ {"questions": [ {...}, ... ]}, ... ]   list with wrapper element
#   shape C:   [ {"id": ...}, {"id": ...}, ... ]        bare list of entry dicts
#   mixed B+C: a list carrying BOTH a wrapper element and bare entry dicts
# The g-115-3038 initializer fix makes fresh files shape C, but existing files
# and partner-authored files still carry A/B/mixed — so read-time tolerance
# stays mandatory (rb-5021: a tolerant reader sibling masking the producer bug
# is why each naive consumer must instead share ONE tolerant path).
#
# The flatten logic below is kept BYTE-FAITHFUL to
# pending-questions-sweep.py::_load_questions (the canonical flattener). Change
# both together (add.sh's resolve() is the third lock-step copy).
#
# Usage:
#   pending-questions-read.sh [--status STATUS] [--type TYPE] [--pq-path PATH]
#                             [--prefix ID-PREFIX]
#   pending-questions-read.sh --all-agents [--status STATUS] [--type TYPE]
#                             [--prefix ID-PREFIX]
# Output: a JSON array (to stdout) of matching entry dicts. Exit 0 on success,
#   INCLUDING a missing file (normal empty state → [] + exit 0, matching the
#   canonical _load_questions). Fail-open on YAML parse error: prints [] and
#   exits 0 (never blocks a consumer). Exit 2 only on a genuine env error
#   (PyYAML missing).
#
# --all-agents (FLEET MODE, g-115-3074): read EVERY agent's file instead of one,
# tagging each returned entry with an `agent` key so a consumer can group by
# owner. Mutually exclusive with --pq-path. Motivation: /open-questions Phase 2
# read only the BOUND agent's path, so a user running it from an alpha session
# structurally could not see the other agents' questions — 21 of 31 fleet-wide
# questions were invisible (user-surfaced 2026-07-25). Fleet mode is the read
# half of that fix; owncloud-pull.sh --all-agents is the freshness half.
#
# The per-file flatten is the SAME `_flatten()` used by the single-file path —
# NOT a second implementation. That matters: the flatten body is kept
# byte-faithful across three lock-step copies (this file,
# pending-questions-sweep.py::_load_questions, pending-questions-add.sh's
# resolve()); a fleet-specific hand-rolled walk would have made a fourth, in the
# one place least likely to be kept in sync.
#
# CROSS-AGENT GLOB CONSUMER: the fleet glob is routed through `agents_root()`
# from _paths.sh, so it auto-tracks an AGENTS_PARENT_DIR rename. Per the
# CLAUDE.md "cross-agent glob consumers" table, a depth-1 PROJECT_ROOT glob here
# would match NOTHING post-relocation and is invisible to all three audit greps
# — the table is this call site's only audit surface.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/_paths.sh" ]]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/_paths.sh"
fi
PY="${PY_CMD:-python3}"

STATUS=""
TYPE=""
PQ_PATH=""
ALL_AGENTS=""
PREFIX=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --status) STATUS="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --type)   TYPE="$2";   shift $(( $# >= 2 ? 2 : 1 )) ;;
        --pq-path) PQ_PATH="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --all-agents) ALL_AGENTS=1; shift ;;
        --prefix) PREFIX="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        *) echo "pending-questions-read: unknown arg '$1'" >&2; exit 2 ;;
    esac
done

PROJECT_ROOT="${PROJECT_ROOT:-$(pwd)}"

# Fleet mode resolves an agents ROOT to glob, not a single file. Refuse the
# ambiguous combination rather than silently picking one (single-source-of-truth:
# a caller passing both has a bug, and quietly honouring one hides it).
AGENTS_ROOT=""
if [[ -n "$ALL_AGENTS" ]]; then
    if [[ -n "$PQ_PATH" ]]; then
        echo "pending-questions-read: --all-agents and --pq-path are mutually exclusive" >&2
        echo "[]"
        exit 2
    fi
    # Routed through the _paths.sh helper (NOT a literal 'agents/' glob) so this
    # call site auto-tracks an AGENTS_PARENT_DIR rename — see the header note.
    if ! declare -F agents_root >/dev/null 2>&1; then
        echo "pending-questions-read: agents_root() unavailable (_paths.sh not sourced)" >&2
        echo "[]"
        exit 2
    fi
    AGENTS_ROOT="$(agents_root)"
fi

if [[ -z "$ALL_AGENTS" && -z "$PQ_PATH" ]]; then
    # _paths.sh exposes the bound agent as AGENT_NAME (NOT AGENT). Mirror
    # pending-questions-add.sh: resolve from MIND_AGENT and fail cleanly if
    # unbound — under `set -u` a bare `$AGENT` reference would abort with an
    # "unbound variable" instead of this diagnostic (g-115-3039 fresh-eyes
    # follow-up: the default path was untested because every test passed
    # --pq-path).
    AGENT="${MIND_AGENT:-}"
    if [[ -z "$AGENT" ]]; then
        echo "pending-questions-read: MIND_AGENT not set — pass --pq-path or bind an agent" >&2
        echo "[]"
        exit 2
    fi
    PQ_PATH="$(agent_dir "$AGENT")/session/pending-questions.yaml"
fi

export PQ_PATH STATUS TYPE ALL_AGENTS AGENTS_ROOT PREFIX

"$PY" - <<'PYEOF'
import glob
import json
import os
import sys

try:
    import yaml
except ImportError:
    print(json.dumps({"error": "PyYAML not installed"}))
    sys.exit(2)

path = os.environ.get("PQ_PATH", "")
want_status = os.environ.get("STATUS", "") or None
want_type = os.environ.get("TYPE", "") or None
all_agents = bool(os.environ.get("ALL_AGENTS", ""))
agents_root = os.environ.get("AGENTS_ROOT", "")
want_prefix = os.environ.get("PREFIX", "") or None


def _read_one(p):
    """Load + flatten ONE pending-questions.yaml. Returns a list of entry dicts.

    A missing pending-questions.yaml is the NORMAL empty state for an agent that
    never logged a question — NOT an input error. Match the canonical
    pending-questions-sweep.py::_load_questions semantics (`if not
    path.exists(): return []`) so a consumer that checks the exit code does not
    see a false error for a routine empty file (g-115-3039 fresh-eyes-code
    self-review). Fail-open on a malformed file: warn to stderr, return [].
    """
    if not os.path.exists(p):
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (yaml.YAMLError, OSError) as e:
        # Fail-open: never block a consumer on an unusable file. In fleet mode
        # this also means ONE bad peer file cannot blank the whole view.
        #
        # OSError is load-bearing, not defensive padding (g-115-3107 F2): the
        # exists() check above only proves a path EXISTS, not that it opens as a
        # file. A directory at that path raises IsADirectoryError [Errno 21]; a
        # root-owned peer file raises PermissionError; and exists()->open() is a
        # TOCTOU window that another box's sync can lose. All three are OSError
        # subclasses, and before this catch any of them killed the process —
        # blanking the ENTIRE fleet view and exiting nonzero, the exact opposite
        # of what the docstring promises. Fleet mode amplifies it: single-agent
        # opened one self-owned file, fleet mode opens N peer files other boxes
        # are actively writing.
        print(f"[pending-questions-read] unreadable ({p}): {type(e).__name__}: {e}",
              file=sys.stderr)
        return []

    # --- flatten: BYTE-FAITHFUL to pending-questions-sweep.py::_load_questions ---
    entries = []
    if data is None:
        entries = []
    elif isinstance(data, dict):
        entries = data.get("questions", []) or []
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                if "questions" in item and isinstance(item["questions"], list):
                    entries.extend(item["questions"])
                elif "id" in item:
                    entries.append(item)
    return [e for e in entries if isinstance(e, dict)]


entries = []
if all_agents:
    # ROSTER-SOURCE DIVERGENCE — deliberate, ACCEPTED, not an oversight
    # (g-115-3107 F3). This read leg enumerates the fleet from an ON-DISK GLOB,
    # while its sibling write leg (owncloud-pull.sh --all-agents) enumerates from
    # team-state agent_status. The two legs of one feature therefore disagree:
    # on cc-04 the roster held 5 agents while the glob matched 6 dirs.
    #
    # Keeping them different is the correct call, because the legs have OPPOSITE
    # failure requirements:
    #   * The PULL leg must use the live roster — pulling for a retired agent is
    #     wasted S3 round-trips, and a stale name there costs real work.
    #   * This READ leg must keep working when team-state is unreadable. Its job
    #     is surfacing questions the USER is waiting on; those silently vanishing
    #     because a sync hiccuped or the daemon was mid-restart is strictly worse
    #     than a stale one lingering. Routing this leg through team-state would
    #     make every user-facing question conditional on a second subsystem being
    #     healthy — and would re-introduce, from the other side, the same fleet
    #     blindness g-115-3074 fixed.
    #
    # ACCEPTED RESIDUAL RISK: an agent dir present on disk but absent from the
    # roster is READ but never REFRESHED by the pull, so a retired agent's
    # leftover pending-questions.yaml could surface indefinitely as stale
    # user-facing work. Mitigated — not eliminated — by the per-entry `agent` tag
    # set below: every entry names its owning agent, so a question from a retired
    # or debug agent is identifiable at the point of display rather than
    # anonymous. The reverse direction (in-roster but no file yet) is already
    # safe: the pull runs first and creates the file.
    # Revisit if a retired agent's questions are ever actually observed lingering;
    # the fix then is agent-dir retirement hygiene (delete the dir), NOT coupling
    # this leg to team-state.
    #
    # Sorted for deterministic output (stable across boxes + reruns).
    for pq in sorted(glob.glob(os.path.join(agents_root, "*", "session", "pending-questions.yaml"))):
        # agents_root/<agent>/session/pending-questions.yaml -> <agent>
        agent = os.path.basename(os.path.dirname(os.path.dirname(pq)))
        for e in _read_one(pq):
            # Tag with the owning agent so a consumer can group by owner. Do not
            # clobber an explicit `agent` key if a producer ever writes one.
            e.setdefault("agent", agent)
            entries.append(e)
else:
    entries = _read_one(path)

# --- optional filters ---
if want_status is not None:
    entries = [e for e in entries if e.get("status") == want_status]
if want_type is not None:
    entries = [e for e in entries if e.get("type") == want_type]
if want_prefix is not None:
    # id-prefix filter (g-115-3100). Exists so anti-stacking guards can ask
    # "is a <family>- question already open?" — the canonical consumer is
    # aspirations-evolve Step 0.5b's l1-taxonomy- check, which previously
    # passed this very flag to a parser that did not accept it: exit 2 +
    # EMPTY stdout, which the call site read as "nothing open", so the guard
    # never suppressed (guard-487 — a suppression gate whose probe cannot
    # match fails OPEN). str() guards a non-string id (a YAML-coerced date or
    # int) from raising on .startswith.
    entries = [e for e in entries if str(e.get("id", "")).startswith(want_prefix)]

print(json.dumps(entries, ensure_ascii=False))
PYEOF
