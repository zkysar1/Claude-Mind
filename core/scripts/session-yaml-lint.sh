#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# session-yaml-lint.sh — advisory YAML lint for session-state files.
#
# Origin:  (alpha session 62 iter 18). Follow-up to 
# (rb-752, guard-487) — the fail-open silent YAML bug in
# fresh-eyes-cadence-check.py was caused by a malformed pending-questions.yaml
# that downstream readers silently swallowed. This gate catches the
# malformation AT WRITE TIME (per iteration-close), not days later when a
# specific consumer hits it.
#
# Targets `<agent>/session/*.yaml` for the active agent. Emits one WARN line
# per parse error to stderr and appends a structured record to
# <agent>/session/yaml-lint-errors.jsonl for cross-iteration tracking.
#
# Exit code: ALWAYS 0 (advisory). The intent is "tell me when YAML is bad,"
# not "block iteration close." A blocking gate would prevent the iteration
# from finishing while the offending YAML is still mid-edit by another tool;
# the consumer-side fail-CLOSED guardrail (guard-487) is the load-bearing
# defense at read time.
#
# Output (JSON to stdout — last line):
#   {"checked": N, "errors": [{file, error, line}], "ok": bool}
#
# Usage:
#   bash core/scripts/session-yaml-lint.sh
#   bash core/scripts/session-yaml-lint.sh --strict   # exit 1 on parse error
#                                                     # (for ad-hoc use, not iteration-close)
#
# Wired from: iteration-close.sh do_state_update (advisory invocation).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh" 2>/dev/null || {
    echo "[session-yaml-lint] WARN: _paths.sh not sourceable; lint skipped (fail-open)" >&2
    echo '{"checked": 0, "errors": [], "ok": true, "skip_reason": "no_paths"}'
    exit 0
}

STRICT=0
if [[ "${1:-}" == "--strict" ]]; then
    STRICT=1
fi

AGENT_NAME="${MIND_AGENT:-}"
if [[ -z "$AGENT_NAME" ]]; then
    echo "[session-yaml-lint] WARN: MIND_AGENT not set; lint skipped (fail-open)" >&2
    echo '{"checked": 0, "errors": [], "ok": true, "skip_reason": "no_agent"}'
    exit 0
fi

SESSION_DIR="$(agent_dir "$AGENT_NAME")/session"
if [[ ! -d "$SESSION_DIR" ]]; then
    echo "[session-yaml-lint] WARN: $SESSION_DIR not a directory; lint skipped (fail-open)" >&2
    echo '{"checked": 0, "errors": [], "ok": true, "skip_reason": "no_session_dir"}'
    exit 0
fi

# Walk the YAML inventory in Python so yaml.safe_load matches the consumers'
# parser exactly. Matches the rb-752 incident shape: yaml.safe_load raised
# YAMLError on malformed input — the lint must use the SAME parser to detect
# the SAME failures consumers would see.
#
# Python heredoc inherits AGENT_NAME, SESSION_DIR, STRICT via env. Stdout is
# the JSON summary; stderr collects WARN lines per error.
SESSION_DIR="$SESSION_DIR" AGENT_NAME="$AGENT_NAME" STRICT="$STRICT" python3 - <<'PYEOF'
import datetime
import json
import os
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("[session-yaml-lint] WARN: yaml module not available; lint skipped", file=sys.stderr)
    print(json.dumps({"checked": 0, "errors": [], "ok": True, "skip_reason": "no_yaml_module"}))
    sys.exit(0)

session_dir = Path(os.environ["SESSION_DIR"])
agent       = os.environ["AGENT_NAME"]
strict      = os.environ.get("STRICT", "0") == "1"

errors = []
checked = 0

for path in sorted(session_dir.glob("*.yaml")):
    checked += 1
    try:
        with open(path, "r", encoding="utf-8") as f:
            yaml.safe_load(f)
    except yaml.YAMLError as e:
        # Mark line if available — yaml.MarkedYAMLError carries problem_mark.
        line = None
        if hasattr(e, "problem_mark") and e.problem_mark is not None:
            line = e.problem_mark.line + 1  # 1-indexed for human readability
        rel_path = str(path.relative_to(session_dir.parent.parent)).replace("\\", "/")
        err_msg = str(e).split("\n")[0][:200]
        errors.append({"file": rel_path, "error": err_msg, "line": line})
        line_str = f":{line}" if line else ""
        print(f"[session-yaml-lint] WARN: {rel_path}{line_str}: {err_msg}", file=sys.stderr)
    except OSError as e:
        # File disappeared between glob and open — race with a writer. Skip
        # silently; the lint is advisory and not load-bearing.
        rel_path = str(path.relative_to(session_dir.parent.parent)).replace("\\", "/")
        errors.append({"file": rel_path, "error": f"read failed: {e}", "line": None})
        print(f"[session-yaml-lint] WARN: {rel_path}: read failed: {e}", file=sys.stderr)

# Append errors to <agent>/session/yaml-lint-errors.jsonl for cross-iteration
# tracking. Best-effort: failure to log is not fatal.
if errors:
    log_path = session_dir / "yaml-lint-errors.jsonl"
    record = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "agent": agent,
        "errors": errors,
    }
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"[session-yaml-lint] WARN: log append failed: {e}", file=sys.stderr)

result = {"checked": checked, "errors": errors, "ok": len(errors) == 0}
print(json.dumps(result, ensure_ascii=False))

if strict and errors:
    sys.exit(1)
PYEOF

# Python script's exit (0 normally, 1 if --strict and errors). Bash exits with
# Python's RC by default since this is the last command. The wrapper itself
# never blocks (set -uo pipefail does not propagate Python's exit; we'd need
# `set -e` for that, which is intentionally absent).
