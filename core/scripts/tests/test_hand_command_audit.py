"""Tests for hand-command-audit.py ().

Layer-C detective for the fourth capability-routing lane: command blocks handed
to the user in chat prose. The predicate's whole value is its SPECIFICITY —
agents post fenced command blocks constantly as evidence and illustration, so a
detective that flags every fence is one nobody reads. These pin the three
exclusions as hard as the one inclusion.

Pattern: importlib + sys.path (the script name has hyphens), per
test_goal_pickup_coordination_check.py.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "hand-command-audit.py"


def _load():
    sys.path.insert(0, str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("hand_command_audit", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load()


# ── the one inclusion ────────────────────────────────────────────────────────

def test_flags_command_handed_to_user():
    text = ("The anchor file is missing. You can run this on that machine:\n\n"
            "```bash\nssh operator.example.com 'cat > /opt/app/anchor.json'\n```\n")
    hits = M.find_handoffs(text)
    assert len(hits) == 1
    assert "ssh operator.example.com" in hits[0]["command"]
    assert hits[0]["cue"].lower().startswith("you can run")


def test_flags_unlabeled_fence_when_body_is_shellish():
    # An unlabeled fence still counts when the body looks like a command —
    # authors routinely omit the language tag.
    text = "Please run the following:\n\n```\nsudo systemctl restart app\n```"
    assert len(M.find_handoffs(text)) == 1


# ── the three exclusions, which are what make it readable ────────────────────

def test_ignores_evidence_block_the_agent_ran_itself():
    """'Here is what I ran' is not routing. This is the common case by far."""
    text = ("I verified the deploy:\n\n```bash\naws lambda get-function "
            "--function-name Foo\n```\nIt returned success.")
    assert M.find_handoffs(text) == []


def test_ignores_command_the_user_explicitly_asked_for():
    """Answering a direct request is not routing work away."""
    text = ("You asked what command would I run to check it. Here is the "
            "command:\n\n```bash\ngit status\n```")
    assert M.find_handoffs(text) == []


def test_ignores_sanctioned_bang_prefix_form():
    """`! <cmd>` runs IN-session and lands output in the conversation — it is
    the sanctioned form for genuinely interactive things, not a handoff."""
    text = "Please run this in the session:\n\n```bash\n! gcloud auth login\n```"
    assert M.find_handoffs(text) == []


def test_ignores_fence_with_no_handoff_cue_nearby():
    text = "The config looks like this:\n\n```bash\nexport FOO=bar\n```"
    assert M.find_handoffs(text) == []


def test_ignores_non_shell_fence():
    text = "You can run it like this:\n\n```json\n{\"a\": 1}\n```"
    assert M.find_handoffs(text) == []


# ── the zero must be interpretable ───────────────────────────────────────────

def test_zero_carries_its_denominator(tmp_path):
    """A 0 with no denominator is indistinguishable from a scan that read
    nothing. The report must say how many blocks it actually looked at."""
    import subprocess
    rows = [{"type": "assistant", "timestamp": "2026-08-08T02:00:00",
             "message": {"content": [{"type": "text",
                                      "text": "I checked it and it was fine."}]}}]
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    r = subprocess.run([sys.executable, str(SCRIPT), "--transcript", str(p), "--json"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["handoffs_found"] == 0
    assert out["assistant_text_blocks_scanned"] == 1
    assert out["interpretable"] is True


def test_empty_scan_is_not_interpretable(tmp_path):
    """No assistant blocks read => interpretable False, so a vacuous zero can
    never be mistaken for a clean audit."""
    import subprocess
    p = tmp_path / "empty.jsonl"
    p.write_text("", encoding="utf-8")
    r = subprocess.run([sys.executable, str(SCRIPT), "--transcript", str(p), "--json"],
                       capture_output=True, text=True, timeout=120)
    out = json.loads(r.stdout)
    assert out["handoffs_found"] == 0
    assert out["interpretable"] is False


def test_tool_calls_are_not_assistant_text(tmp_path):
    """The lane is PROSE. A tool_use block is already covered by the three
    gated surfaces and must not be double-counted here."""
    import subprocess
    rows = [{"type": "assistant", "timestamp": "2026-08-08T02:00:00",
             "message": {"content": [{"type": "tool_use", "name": "Bash",
                                      "input": {"command": "sudo rm -rf /tmp/x"}}]}}]
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    r = subprocess.run([sys.executable, str(SCRIPT), "--transcript", str(p), "--json"],
                       capture_output=True, text=True, timeout=120)
    out = json.loads(r.stdout)
    assert out["assistant_text_blocks_scanned"] == 0
    assert out["handoffs_found"] == 0
