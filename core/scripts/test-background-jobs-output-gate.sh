#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Test the output-sanity gate (check_output_artifacts) in background-jobs.py.
#
# Covers:
#   1. missing     — declared path does not exist            → FAIL (reason: missing)
#   2. too-small   — file exists but size < min_bytes        → FAIL (reason: too_small)
#   3. json-invalid — format=json, file is not valid JSON    → FAIL (reason: json_parse)
#   4. jsonl-invalid — format=jsonl, first line not JSON     → FAIL (reason: jsonl_parse)
#   5. jsonl-empty-first — format=jsonl, first line blank    → FAIL (reason: jsonl_empty_first_line)
#   6. happy-json — format=json, valid JSON                  → PASS (no failures)
#   7. happy-jsonl — format=jsonl, valid first line          → PASS (no failures)
#   8. size-only — no format, size OK                        → PASS (no failures)
#   9. empty-list — no artifacts declared                    → PASS (no failures, backward compat)
#
# Implementation: writes a temporary python driver (never use `python3 <<EOF` —
# guard-140 + rb-247: heredoc+stdin patterns silently break on subprocess boundaries).

set -euo pipefail

# Source _paths.sh so python3 resolves via the project's shim (avoids the
# Microsoft Store python3 stub on Windows when python3 is not installed).
# MIND_AGENT defaults to 'alpha' only if the caller hasn't already set it —
# respects the hook-injected binding during LLM-triggered runs, and gives
# manual runs a deterministic default. This test does not touch agent state;
# MIND_AGENT exists only to let _paths.sh resolve and set up the shim.
: "${MIND_AGENT:=alpha}"
export MIND_AGENT
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"

PROJECT_ROOT_REAL="$(cd "$(dirname "$0")/../.." && pwd)"
SANDBOX="$(mktemp -d)"
trap "rm -rf '$SANDBOX'" EXIT

# Python on Windows needs a native path (C:/...) — cygpath -m translates from
# MSYS /tmp/xxx to mixed format. On Linux/macOS cygpath is absent and the path
# is already native, so fall through.
if command -v cygpath &>/dev/null; then
    SANDBOX_PY="$(cygpath -m "$SANDBOX")"
    PROJECT_ROOT_PY="$(cygpath -m "$PROJECT_ROOT_REAL")"
else
    SANDBOX_PY="$SANDBOX"
    PROJECT_ROOT_PY="$PROJECT_ROOT_REAL"
fi

# Write the python driver once — it reads artifacts JSON from argv[1] and prints failures JSON.
cat > "$SANDBOX/driver.py" <<'PYDRIVER'
import json
import sys
import types
import importlib.util
from pathlib import Path

project_root = sys.argv[1]
sandbox = sys.argv[2]
artifacts_json = sys.argv[3]

# Stub _paths so importing background-jobs.py does not crash when no agent
# is bound. We only exercise the pure check_output_artifacts function.
fake_paths = types.ModuleType("_paths")
fake_paths.AGENT_DIR = Path(sandbox)
fake_paths.PROJECT_ROOT = Path(sandbox)
fake_paths.assert_agent_dir = lambda module_name="<unknown>": None
sys.modules["_paths"] = fake_paths

spec = importlib.util.spec_from_file_location(
    "background_jobs_under_test",
    str(Path(project_root) / "core" / "scripts" / "background-jobs.py"),
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

artifacts = json.loads(artifacts_json)
failures = module.check_output_artifacts(artifacts)
print(json.dumps(failures))
PYDRIVER

PASS=0
FAIL=0

run_case() {
    local label="$1"; shift
    local expected_failure_substr="$1"; shift  # empty string = expect PASS (no failures)
    local artifacts_json="$1"; shift

    local result
    result=$(python3 "$SANDBOX/driver.py" "$PROJECT_ROOT_PY" "$SANDBOX_PY" "$artifacts_json")

    if [ -z "$expected_failure_substr" ]; then
        if [ "$result" = "[]" ]; then
            echo "  PASS: $label"
            PASS=$((PASS + 1))
        else
            echo "  FAIL: $label — expected [] got: $result"
            FAIL=$((FAIL + 1))
        fi
    else
        if echo "$result" | grep -q "$expected_failure_substr"; then
            echo "  PASS: $label ($expected_failure_substr matched)"
            PASS=$((PASS + 1))
        else
            echo "  FAIL: $label — expected substring [$expected_failure_substr] in: $result"
            FAIL=$((FAIL + 1))
        fi
    fi
}

# Prepare test artifacts
printf "not valid json at all" > "$SANDBOX/bad.json"             # 21 bytes, invalid JSON
printf '{"valid":true}' > "$SANDBOX/good.json"                   # valid JSON
printf '{"line":1}\n' > "$SANDBOX/good.jsonl"                    # valid JSONL first line
printf "\n" > "$SANDBOX/blank-first.jsonl"                       # blank first line
printf "garbage" > "$SANDBOX/small.bin"                          # 7 bytes
printf "xxxxxxxxxxxxxxxxxxxxxxxxx" > "$SANDBOX/sized.bin"        # 25 bytes

# Use the cygpath-translated SANDBOX_PY for all JSON paths so Python
# resolves them against the real filesystem instead of /tmp/.
SB="$SANDBOX_PY"

echo ""
echo "Running output-sanity gate tests..."
echo ""

run_case "1. missing file" "missing" \
  "[{\"path\":\"$SB/does-not-exist.jsonl\",\"min_bytes\":1}]"

run_case "2. too small" "too_small" \
  "[{\"path\":\"$SB/small.bin\",\"min_bytes\":100}]"

run_case "3. json invalid" "json_parse" \
  "[{\"path\":\"$SB/bad.json\",\"min_bytes\":1,\"format\":\"json\"}]"

run_case "4. jsonl invalid" "jsonl_parse" \
  "[{\"path\":\"$SB/bad.json\",\"min_bytes\":1,\"format\":\"jsonl\"}]"

run_case "5. jsonl blank first line" "jsonl_empty_first_line" \
  "[{\"path\":\"$SB/blank-first.jsonl\",\"min_bytes\":0,\"format\":\"jsonl\"}]"

run_case "6. happy json" "" \
  "[{\"path\":\"$SB/good.json\",\"min_bytes\":1,\"format\":\"json\"}]"

run_case "7. happy jsonl" "" \
  "[{\"path\":\"$SB/good.jsonl\",\"min_bytes\":1,\"format\":\"jsonl\"}]"

run_case "8. size only (no format)" "" \
  "[{\"path\":\"$SB/sized.bin\",\"min_bytes\":10}]"

run_case "9. empty artifacts list (backward compat)" "" \
  "[]"

echo ""
echo "Total: $PASS passed, $FAIL failed"
exit $FAIL
