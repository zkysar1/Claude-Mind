#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# UserPromptExpansion hook — records user-typed slash command invocations
# (e.g., user types "/research-topic foo") to the per-agent skill-invocations
# JSONL ledger. Sibling to context-reads-skill-gate.sh which captures the
# model-invoked path via PreToolUse[Skill].
#
# Per Anthropic hooks docs: typing /foo directly BYPASSES PreToolUse[Skill]
# and fires UserPromptExpansion instead — this hook is the only way to capture
# the user-invocation path. Without it, every "unknown" row from the
# PreToolUse hook is actually model-invoked.
#
# Knowledge tree: world/knowledge/tree/system/system-constraints-loop/skill-telemetry-signal-master-plan.md
#
# Fail-open: any failure here must NOT block the user prompt from being
# processed. The hook returns 0 unconditionally.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"

# Capture stdin once. The heredoc form below would otherwise redirect stdin
# to the heredoc CONTENT (the python source), losing the JSON payload from
# the upstream pipe. Without this capture, python's json.load(sys.stdin)
# fails with "Expecting value: line 1 column 1 (char 0)" and the
# 2>/dev/null || true fail-open mask makes the failure silent.
# See guard-536 for the pattern.
stdin_payload="$(cat)"

# Single-pass parse + record. Filters by expansion_type=="slash_command"
# (ignores mcp_prompt expansions and other future expansion types).
AGENT_DIR="${AGENT_DIR:-}" AGENT_NAME="${AGENT_NAME:-}" JSON_PAYLOAD="$stdin_payload" \
    python3 - <<'PY' 2>/dev/null || true
import sys, json, datetime, os
payload = os.environ.get('JSON_PAYLOAD', '')
try:
    d = json.loads(payload) if payload else {}
except Exception:
    sys.exit(0)
if d.get('expansion_type') != 'slash_command':
    sys.exit(0)
skill = d.get('command_name', '')
sid = d.get('session_id', '')
agent_dir = os.environ.get('AGENT_DIR', '')
agent_name = os.environ.get('AGENT_NAME', '')
if not (agent_dir and skill):
    sys.exit(0)
row = {
    'ts': datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
    'skill': skill,
    'agent': agent_name,
    'sid': sid,
    'invocation_source': 'user',
}
try:
    with open(os.path.join(agent_dir, 'skill-invocations.jsonl'), 'a', encoding='utf-8') as f:
        f.write(json.dumps(row) + chr(10))
except Exception:
    pass
PY

exit 0
