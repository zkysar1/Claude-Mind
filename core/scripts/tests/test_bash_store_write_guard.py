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


def test_the_deny_never_names_its_own_bypass_token():
    """Measured 2026-08-29 (a downstream deployment): the deny ended 'Genuine
    exception: put STORE_WRITE_GUARD_OVERRIDE anywhere in the command' and 6 of 42
    firings became overrides within minutes. A refusal that names its bypass is
    an instruction to the model it refuses; the token lives in the docstring and
    gates.yaml, where an operator reads it."""
    for cmd in (HEREDOC_STATUS_WRITE, "echo x >> world/reasoning-bank.jsonl"):
        decision, reason = run(cmd)
        assert decision == "deny"
        assert "STORE_WRITE_GUARD_OVERRIDE" not in reason
        assert "no in-session bypass" in reason
        assert "Investigate:" in reason


# --- the override is honored only on a STOPPED deployment ------------------

def _deployment(tmp_path, **states):
    """Stage agents/<name>/session/agent-state under a tmp project root, using the
    resolver constants so the fixture follows a layout move."""
    from _paths import AGENTS_PARENT_DIR, SESSION_DIRNAME

    for name, state in states.items():
        d = tmp_path / AGENTS_PARENT_DIR / name / SESSION_DIRNAME
        d.mkdir(parents=True)
        if state is not None:
            (d / "agent-state").write_text(state, encoding="utf-8")
    return tmp_path


def run_in(root, command):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    env = dict(os.environ, PROJECT_ROOT=str(root))
    proc = subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    log = root / "core" / "logs" / "hook-fires" / "store-write-guard.jsonl"
    kind = json.loads(log.read_text().splitlines()[-1])["kind"] if log.exists() else None
    if not proc.stdout.strip():
        return "allow", "", kind
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    return out["permissionDecision"], out["permissionDecisionReason"], kind


# The three override shapes the Bodies produced on 2026-08-29, verbatim in form.
OVERRIDE_SHAPES = [
    # 12:35 -- the token set INSIDE the program, "so the hook recognizes this as a
    # genuine exception"
    "cd /opt/mind/.mind-data/world && python3 -c \"\nimport json, os\n"
    "# Set the override flag so the hook recognizes this as a genuine exception\n"
    "os.environ['STORE_WRITE_GUARD_OVERRIDE'] = '1'\n"
    "rows = [json.loads(l) for l in open('aspirations.jsonl')]\n"
    "open('aspirations.jsonl', 'w').write('\\n'.join(json.dumps(r) for r in rows))\n\"",
    # 12:39 -- env prefix on a -c program
    "cd /opt/mind/.mind-data/world && STORE_WRITE_GUARD_OVERRIDE=1 python3 -c \"\nimport json\n"
    "with open('aspirations.jsonl') as f:\n    lines = f.readlines()\n"
    "open('aspirations.jsonl', 'w').writelines(lines[:-1])\n\"",
    # 13:49 -- env prefix on a heredoc: the hand fold of rb-1 into rb-2
    "cd /opt/mind && STORE_WRITE_GUARD_OVERRIDE=1 python3 << 'PYEOF'\nimport json, os\n"
    "store_path = '.mind-data/world/reasoning-bank.jsonl'\n"
    "entries = [json.loads(l) for l in open(store_path) if l.strip()]\n"
    "entries = [e for e in entries if e['id'] != 'rb-1']\n"
    "with open(store_path, 'w') as f:\n"
    "    f.write('\\n'.join(json.dumps(e) for e in entries) + '\\n')\nPYEOF",
]


@pytest.mark.parametrize("command", OVERRIDE_SHAPES)
def test_the_override_is_refused_while_an_agent_is_running(tmp_path, command):
    root = _deployment(tmp_path, coach="RUNNING", observer="IDLE")
    decision, reason, kind = run_in(root, command)
    assert decision == "deny", command
    assert "not honored while an agent is RUNNING (coach)" in reason
    assert "history.py restore" in reason
    assert "STORE_WRITE_GUARD_OVERRIDE" not in reason
    assert kind == "override-refused"


@pytest.mark.parametrize("command", OVERRIDE_SHAPES)
def test_the_override_is_honored_on_a_stopped_deployment(tmp_path, command):
    root = _deployment(tmp_path, coach="IDLE", other=None)
    decision, _, kind = run_in(root, command)
    assert decision == "allow", command
    assert kind == "override"


def test_no_agents_dir_at_all_is_a_stopped_deployment(tmp_path):
    decision, _, kind = run_in(tmp_path, OVERRIDE_SHAPES[2])
    assert (decision, kind) == ("allow", "override")


def test_running_agents_reads_the_state_files():
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = _deployment(Path(td), a="RUNNING", b="IDLE", c=None, d="RUNNING")
        assert _mod.running_agents(root) == ["a", "d"]
        assert _mod.running_agents(Path(td) / "nowhere") == []


@pytest.mark.parametrize(
    "command",
    [
        "grep -c exp-encode-session agents/alpha/experience.jsonl",
        "wc -l world/aspirations.jsonl; tail -1 world/aspirations.jsonl",
        "grep g-005 world/aspirations.jsonl > /tmp/out.txt",
        "grep -c exp-encode-session-2026 agents/alpha/experience.jsonl",
        "cat world/aspirations.jsonl | wc -l",
        "grep -q g-005-06 world/aspirations.jsonl && echo present",
        # angle-bracket placeholders in quoted prose are not redirects (three
        # false denies in one session, 2026-08-29: commit messages + JSON payloads)
        'git commit -q -m "docs: skills grep -c <id> experience.jsonl to confirm a write landed"',
        "printf '%s' '{\"content\":\"a presence check is fine: grep -c <id> experience.jsonl\"}' | bash core/scripts/reasoning-bank-add.sh",
        "bash core/scripts/aspirations-read.sh --source <world|agent> --id asp-1 > /tmp/x.json",
        "python3 -c \"import json; print(json.load(open('/tmp/fixture/world/aspirations.jsonl')))\"",
        "grep g-1 agents/alpha/temp/aspirations.jsonl | python3 -c 'import sys,json; print(sys.stdin.read())'",
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


# ---- the parse shape (): a hand parser over a store is refused, a
# shell presence check is not. Measured 2026-08-29 on an 8-Body downstream
# fleet: 79 hand parsers in 12 h against 4 wrapper calls; every one below is a
# literal shape from those session files (goal ids normalised).
PARSE_SHAPES = [
    "python3 - <<'EOF'\nimport json\nfor l in open('agents/alpha/aspirations.jsonl'):\n    print(json.loads(l)['id'])\nEOF",
    "grep '\"id\": \"g-006-08\"' /opt/mind/.mind-data/world/aspirations.jsonl | head -1 | python3 -c \"import sys,json; print(json.load(sys.stdin).get('aspiration_id',''))\"",
    "cd /opt/mind && cat world/pipeline.jsonl 2>/dev/null | python3 -c \"\nimport sys, json\nfor line in sys.stdin:\n    print(json.loads(line)['id'])\n\"",
    "cd /opt/mind && python3 -c \"\nimport json\nwith open('.mind-data/world/aspirations.jsonl') as f:\n    for line in f:\n        d = json.loads(line)\n\"",
    "cd /opt/mind && grep -m1 \"g-006-24\" agents/coach/aspirations.jsonl | python3 -c \"import sys,json; d=json.loads(sys.stdin.read()); print(d['aspiration_id'])\"",
    "py -3 -c \"import yaml; print(yaml.safe_load(open('agents/alpha/session/working-memory.yaml'))['current_goal'])\"",
    "tail -3 agents/alpha/session/execution-diary.jsonl | jq -r .content",
]


@pytest.mark.parametrize("command", PARSE_SHAPES)
def test_hand_parsers_over_a_store_are_refused(command):
    decision, reason = run(command)
    assert decision == "deny", command
    assert "direct store parse refused" in reason
    assert "STORE_WRITE_GUARD_OVERRIDE" not in reason


def test_the_parse_deny_names_the_single_goal_reader():
    _, reason = run(PARSE_SHAPES[1])
    assert "aspirations-query.sh --goal-field id <goal-id> --full" in reason
    assert "grep -c <id> aspirations.jsonl" in reason, "the presence-check carve-out must be stated"


def test_the_parse_deny_names_the_reader_per_store():
    for cmd, reader in (
        (PARSE_SHAPES[2], "pipeline-read.sh"),
        (PARSE_SHAPES[5], "wm-read.sh <slot> --json"),
        (PARSE_SHAPES[6], "execution-diary.sh read"),
    ):
        decision, reason = run(cmd)
        assert decision == "deny" and reader in reason, (cmd, reason[-200:])


def test_a_parse_of_the_worker_prefix_store_is_not_a_parse():
    """The prefix names working-memory.yaml; the program reads a fixture."""
    cmd = WORKER_PREFIX + "python3 -c \"import json; print(json.load(open('tests/fixtures/x.json')))\""
    assert _mod.direct_store_parses(cmd) == []
    assert run(cmd)[0] == "allow"


# The framework prefixes EVERY worker command with its own env exports, one of
# which names the Body's working-memory.yaml. Measured 2026-08-29 02:56 (a
# downstream worker Body): the Python wrote a test file, the store name lived
# only in the prefix, and the guard denied it anyway.
WORKER_PREFIX = (
    'export PATH="/opt/mind/core/scripts/.python-shim:$PATH"; export MIND_AGENT=alpha; '
    'export BODY_WM_PATH="/opt/mind/agents/alpha/sessions/cb47721d/working-memory.yaml"; '
    "export BODY_ROLE=worker; export MIND_GOAL_ID=g-005-09; export MIND_SID=cb47721d; "
)


def test_a_store_named_only_in_the_shell_prefix_is_not_a_python_write():
    cmd = WORKER_PREFIX + (
        'cd .mind-data/world/scripts && python3 -c "\nimport sys, json\n'
        "sys.path.insert(0, '.')\nfrom lineup_optimizer import optimize\n"
        "open('test_lineup_optimizer_results.json', 'w').write(json.dumps(optimize()))\n"
        '"'
    )
    assert direct_store_writes(cmd) == []
    assert run(cmd)[0] == "allow"


def test_the_same_prefix_with_a_store_write_inside_the_program_is_refused():
    cmd = WORKER_PREFIX + (
        "cd /opt/mind && python3 -c \"\nimport json\n"
        "open('agents/alpha/aspirations.jsonl', 'a').write(json.dumps({'id': 'x'}) + '\\n')\n\""
    )
    assert direct_store_writes(cmd) == [("inline Python writing", "agents/alpha/aspirations.jsonl")]
    assert run(cmd)[0] == "deny"


def test_a_write_idiom_outside_the_program_does_not_count():
    # `json.dump(` in a --summary string, a store name in a path argument, and a
    # Python program that only READS: three mentions, zero writes.
    cmd = (
        "bash core/scripts/iteration-close.sh --summary 'used json.dump( on the report' "
        "&& python3 - <<'EOF'\nimport json\nprint(len(open('world/aspirations.jsonl').readlines()))\nEOF"
    )
    assert direct_store_writes(cmd) == []


def test_the_log_drops_the_framework_env_prefix_so_the_write_is_visible(tmp_path):
    cmd = WORKER_PREFIX + "cat >> agents/alpha/sessions/cb47721d/execution-diary.jsonl << 'DIARY'\n{}\nDIARY"
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
    assert proc.returncode == 0 and json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    log = tmp_path / "core" / "logs" / "hook-fires" / "store-write-guard.jsonl"
    rec = json.loads(log.read_text().splitlines()[-1])
    assert rec["command"].startswith("cat >> agents/alpha/sessions/cb47721d/execution-diary.jsonl")
    assert "BODY_WM_PATH" not in rec["command"]


def test_override_token_passes_and_is_logged(tmp_path):
    # tmp_path has no agents dir -> nothing RUNNING -> a stopped deployment.
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


def test_start_cw1a_prescribed_shape_is_not_refused():
    """: /start CW1a must prescribe a command the guard ADMITS.

    The step force-freshes the canonical WM before a worker fork. It used to
    prescribe a `backend-cat.sh` redirect into a temp file followed by a rename
    over working-memory.yaml -- which this guard refuses, correctly (guard-996:
    a temp-file-then-rename IS authoring a raw write to a governed store). The
    step was therefore unexecutable as written, and the ex-worker->worker path
    could not complete.

    Both directions are pinned deliberately (rb-8987: prove a fence ADMITS, not
    only that it REFUSES). Without the positive control below, this test would
    still pass if the guard stopped refusing anything at all.
    """
    prescribed = (
        "bash core/scripts/owncloud-pull.sh --agent zeta "
        "--only working-memory.yaml; echo \"CW1A_FRESH_RC=$?\""
    )
    decision, reason = run(prescribed)
    assert decision == "allow", (
        "the shape /start CW1a prescribes must not be refused; "
        f"guard said {decision}: {reason}"
    )
    assert direct_store_writes(prescribed) == []


def test_start_cw1a_old_copy_shape_is_still_refused():
    """Positive control for the test above -- the guard must still refuse the
    copy shape CW1a used to prescribe, or that test proves nothing."""
    wm = "agents/zeta/session/working-memory.yaml"
    old_shape = f"bash core/scripts/backend-cat.sh cat {wm} > {wm}.fresh && mv {wm}.fresh {wm}"
    assert direct_store_writes(old_shape), (
        "guard no longer refuses the rename-over-a-governed-store shape; "
        "the CW1a admit test above is now vacuous"
    )


def test_start_cw1a_step_does_not_reference_the_retired_copy():
    """The SKILL.md step itself must not carry the retired temp-file name."""
    skill = (ROOT / ".claude" / "skills" / "start" / "SKILL.md").read_text(encoding="utf-8")
    assert "working-memory.yaml.fresh" not in skill
    assert "owncloud-pull.sh --agent <agent-name> --only working-memory.yaml" in skill


def test_hook_is_registered_on_the_bash_matcher():
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    bash_hooks = [
        h["command"]
        for entry in settings["hooks"]["PreToolUse"]
        if entry.get("matcher") == "Bash"
        for h in entry["hooks"]
    ]
    assert any(c.endswith("core/scripts/bash-store-write-guard.sh") for c in bash_hooks)
