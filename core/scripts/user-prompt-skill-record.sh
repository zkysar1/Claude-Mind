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
    PROJECT_ROOT="${PROJECT_ROOT:-}" \
    SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)" \
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
sys.path.insert(0, os.environ.get('SCRIPTS_DIR', ''))
# AGENT_DIR arrives EMPTY on every real fire. _paths.sh populates it from
# MIND_AGENT, and a UserPromptExpansion hook is never given that var — only
# PreToolUse[Bash] injects it. So the guard below exited 0 unconditionally and
# this hook recorded NOTHING, ever: measured 2026-08-02 across all five agents,
# 16,600 ledger rows, 100% invocation_source=model, zero user (alpha 3158,
# bravo 3512, echo 2795, foxtrot 3831, zeta 3304). Every "model" row was real;
# the user-invocation path this hook exists to capture was simply absent.
#
# Resolve from the payload's session_id instead — the same canonical resolver
# the sibling PreToolUse hook uses (context-reads-skill-gate.sh L59-66).
# Guarded + fail-open: an unresolvable SID leaves agent_dir empty and the hook
# stays the silent no-op it was, never an error on the user's prompt path.
if not agent_dir and sid:
    try:
        from pathlib import Path
        from _session_binding import resolve_binding, _agent_dir
        _root = Path(os.environ.get('PROJECT_ROOT', ''))
        _b = resolve_binding(sid, _root)
        if _b is not None:
            agent_name = agent_name or _b.agent
            agent_dir = str(_agent_dir(_root, _b.agent))
    except Exception:
        pass
if not (agent_dir and skill):
    sys.exit(0)
row = {
    'ts': datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
    'skill': skill,
    'agent': agent_name,
    'sid': sid,
    'invocation_source': 'user',
}
#  (G3 worker rail): skip this append when the bound Body is a WORKER.
# Sibling of the identical rail in context-reads-skill-gate.sh — same store,
# same cross-box loss class, same derivation. See that file for the full
# rationale; the short version is the part that matters here:
#
# DO NOT rewrite this as `os.environ.get('BODY_ROLE') != 'worker'`. BODY_ROLE is
# exported by bash-agent-inject.py, which rewrites the command string of BASH
# TOOL calls ONLY. This is a UserPromptSubmit hook, invoked directly by Claude
# Code, so the var is never present and the rail would be 100% INERT while
# hand-testing GREEN (guard-1680; read-before-edit.md Rule 4). Note this file
# already depends on that same fact from the other side: `agent_dir` comes from
# the environment the hook wrapper sets, not from bash-agent-inject.
#
# Derive the role LOCALLY instead, using the SAME predicate bash-agent-inject
# uses: a per-session body-WM file exists ONLY for a non-reducer Body. `sessions`
# is inlined to keep this hook off the _paths import path and mirrors
# SESSIONS_DIRNAME (CLAUDE.md Agent-dir Resolution, inlined-copies table).
_SDN='sessions'
if sid and os.path.exists(
        os.path.join(agent_dir, _SDN, sid, 'working-memory.yaml')):
    sys.exit(0)
# Bare O_APPEND write, deliberately NOT _fileops.locked_append_jsonl.
#
# -e replaced this append with locked_append_jsonl to fix real data
# loss (595 rows lost from one agent's ledger). That commit shipped TWO
# independent changes, and only ONE of them was the cure:
#   - the merge REGISTRATION in coordination_merge — this is what carries
#     durability. owncloud_sync._try_merge_put pushes the UNION for any
#     merge-registered store on the sync sweep, so no side's rows are dropped.
#     It has done so since a2f9f8a17 (2026-07-16), independent of how the row
#     was written, and it is what actually stopped the overwrite.
#   - routing the hook write through the backend — this buys only IMMEDIACY,
#     and under own-cloud it is a force-fresh GET + full-file PUT per fire.
# Measured on cc-04 (Linux 6.8.0-136-generic) against a size-matched 486KB
# ledger: locked_append_jsonl median 700ms vs bare append median 0.09ms —
# ~7,435x, paid on a hook that fires on every user prompt. The IRREDUCIBLY
# LOCAL banner at line 2 is exactly the contract that forbids that round trip.
# The registration and the _fileops snapshot-blacklist entry both STAY; only
# the per-fire backend hop is reverted.
#
# Fail-open is the contract here (guard-141): the enclosing `2>/dev/null ||
# true` guarantees exit 0, so a failed append never blocks the user's prompt.
try:
    with open(os.path.join(agent_dir, 'skill-invocations.jsonl'),
              'a', encoding='utf-8') as fh:
        fh.write(json.dumps(row) + '\n')
except Exception:
    pass
PY

exit 0
