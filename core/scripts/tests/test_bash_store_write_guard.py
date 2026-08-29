"""bash-store-write-guard.py -- a governed store is written ONLY through its script.

Canonical incident (a downstream worker Body, 2026-08-29): a python heredoc set a goal's
status to "done" straight in agents/alpha/aspirations.jsonl. Every case below is
the literal shape of an ad-hoc command the hook sees.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
GUARD = ROOT / "core" / "scripts" / "bash-store-write-guard.py"

sys.path.insert(0, str(ROOT / "core" / "scripts"))
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("bash_store_write_guard", GUARD)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
direct_store_writes = _mod.direct_store_writes


def run(command, tool_name="Bash"):
    payload = {"tool_name": tool_name, "tool_input": {"command": command}}
    env = dict(os.environ, PROJECT_ROOT=str(ROOT))
    proc = subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    if not proc.stdout.strip():
        return "allow", ""
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    return out["permissionDecision"], out["permissionDecisionReason"]


HEREDOC_STATUS_WRITE = """cd /opt/mind && python3 << 'PYEOF'
import json
p = "agents/alpha/aspirations.jsonl"
rows = [json.loads(l) for l in open(p)]
for a in rows:
    for g in a["goals"]:
        if g["id"] == "g-005-06":
            g["status"] = "done"
open(p, "w").write("\\n".join(json.dumps(r) for r in rows) + "\\n")
print("Updated g-005-06 status to done")
PYEOF"""


@pytest.mark.parametrize(
    "command",
    [
        HEREDOC_STATUS_WRITE,
        "echo '{\"id\":\"g-1\"}' >> agents/alpha/aspirations.jsonl",
        "jq '.' world/guardrails.jsonl > world/guardrails.jsonl",
        "sed -i 's/\"done\"/\"completed\"/' agents/alpha/aspirations.jsonl",
        "cp /root/backup.jsonl /opt/mind/.mind-data/world/reasoning-bank.jsonl",
        "mv fixed.jsonl \"$WORLD_PATH/pipeline.jsonl\"",
        "cat extra.jsonl | tee -a world/journal.jsonl",
        "rm agents/alpha/session/working-memory.yaml",
        'python3 -c "from pathlib import Path; Path(\'world/team-state.yaml\').write_text(\'{}\')"',
        "python3 - <<'EOF'\nimport yaml\nd=yaml.safe_load(open('world/knowledge/tree/_tree.yaml'))\nyaml.safe_dump(d, open('world/knowledge/tree/_tree.yaml','w'))\nEOF",
    ],
)
def test_direct_writes_are_refused(command):
    decision, reason = run(command)
    assert decision == "deny", command
    assert "direct store write refused" in reason


def test_the_deny_names_the_close_writer_for_a_goal_status():
    _, reason = run(HEREDOC_STATUS_WRITE)
    assert "iteration-close.sh --phase verify" in reason
    assert "aspirations-update-goal.sh" in reason
    assert "STORE_WRITE_GUARD_OVERRIDE" in reason


@pytest.mark.parametrize(
    "command",
    [
        "grep -c exp-encode-session agents/alpha/experience.jsonl",
        "wc -l world/aspirations.jsonl; tail -1 world/aspirations.jsonl",
        "grep g-005 world/aspirations.jsonl > /tmp/out.txt",
        "python3 - <<'EOF'\nimport json\nfor l in open('agents/alpha/aspirations.jsonl'):\n    print(json.loads(l)['id'])\nEOF",
        "bash core/scripts/aspirations-update-goal.sh --source agent g-005-06 status in-progress",
        "bash core/scripts/iteration-close.sh --phase verify --goal g-005-06 --status completed --source agent --outcome deep --summary x",
        "cp world/aspirations.jsonl /root/backup-aspirations.jsonl",
        "echo '{}' > /tmp/fixture/world/aspirations.jsonl",
        "echo '{}' > agents/alpha/temp/aspirations.jsonl",
        "git diff -- world/aspirations.jsonl 2>/dev/null | head",
        "ls -la .mind-data/world/ && echo done 2>&1",
        "python3 -c \"import sys; sys.stdout.write('guardrails.jsonl has rows')\"",
    ],
)
def test_reads_scripts_and_temp_paths_pass(command):
    decision, _ = run(command)
    assert decision == "allow", command


def test_override_token_passes_and_is_logged(tmp_path):
    cmd = "STORE_WRITE_GUARD_OVERRIDE=restore-from-history cp snap.jsonl world/guardrails.jsonl"
    payload = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    env = dict(os.environ, PROJECT_ROOT=str(tmp_path))
    proc = subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0 and not proc.stdout.strip()
    log = tmp_path / "core" / "logs" / "hook-fires" / "store-write-guard.jsonl"
    rec = json.loads(log.read_text().splitlines()[-1])
    assert rec["kind"] == "override"


def test_non_bash_and_malformed_payloads_approve():
    assert run("echo x >> world/aspirations.jsonl", tool_name="Read")[0] == "allow"
    proc = subprocess.run(
        [sys.executable, str(GUARD)], input="not json", capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0 and not proc.stdout.strip()


def test_predicate_reports_the_first_governed_path():
    hits = direct_store_writes("echo x >> /tmp/a/aspirations.jsonl; echo y >> world/aspirations.jsonl")
    assert hits == [("shell redirect into", "world/aspirations.jsonl")]


def test_hook_is_registered_on_the_bash_matcher():
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    bash_hooks = [
        h["command"]
        for entry in settings["hooks"]["PreToolUse"]
        if entry.get("matcher") == "Bash"
        for h in entry["hooks"]
    ]
    assert any(c.endswith("core/scripts/bash-store-write-guard.sh") for c in bash_hooks)
