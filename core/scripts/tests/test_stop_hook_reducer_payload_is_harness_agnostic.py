"""The reducer BLOCK payload is ONE document on every harness (2026-09-18).

The framework targets exactly one harness contract -- Claude Code's -- and a
vessel implements it (loop-terminal-protocol.md section 4.1). Two pins:

  * the reason opens with the Claude Code imperative, byte for byte -- the
    line a Mind's model reads on every harness;
  * the payload carries the deadman `wakeup` key UNCONDITIONALLY (Zak-Code
    ADR-0102: a harness with a wake-up slot arms the net itself; Claude Code
    ignores keys it does not define), so the hook never reads a harness marker.

The positive control is the third test: the SAME payload under a Claude Code
marker, a vessel marker, and no marker at all -- if a branch on harness
identity ever returns, that equality breaks first.

HARNESS REUSE (deliberate): test_stop_hook_gate_integration's reducer fixture
(same tmp PROJECT_ROOT, same production-shaped env).
"""
from __future__ import annotations

import json

from test_stop_hook_gate_integration import (  # noqa: E402
    _agent_dir,
    _blocked,
    _drive,
    _run_hook_as_runner,
)

CONTRACT_HEAD = (
    "Turn ended without a Skill(aspirations) re-entry (autocompact OR a text "
    "summary terminated the turn). Your FIRST action MUST be: Skill('aspirations') "
    "with args='loop'. Do NOT manually select goals. Do NOT run Bash commands "
    "first. Call the Skill tool IMMEDIATELY."
)
SENTINEL_NET = {"prompt": "<<autonomous-loop-dynamic>>", "delay_seconds": 600}

CLAUDE_CODE = {"CLAUDECODE": "1", "ZAKCODE_SESSION": ""}
VESSEL = {"CLAUDECODE": "", "ZAKCODE_SESSION": "x"}
NO_MARKER = {"CLAUDECODE": "", "ZAKCODE_SESSION": ""}


def _payload(proc) -> dict:
    """The hook's decision document (the JSON line carrying `decision`)."""
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if "decision" in doc:
            return doc
    raise AssertionError(f"no decision payload in stdout: {proc.stdout!r}")


def _no_signal_written(root) -> None:
    sess = _agent_dir(root) / "session"
    for name in ("stop-requested", "stop-loop", "stop-target-mode"):
        assert not (sess / name).exists(), f"the hook wrote {name}"


def test_reducer_reason_opens_with_the_claude_code_imperative(tmp_path):
    proc, root = _drive(tmp_path)
    assert _blocked(proc)
    doc = _payload(proc)
    assert doc["decision"] == "block"
    assert doc["reason"].startswith(CONTRACT_HEAD), doc["reason"][:400]
    assert "Prefix all Bash with MIND_AGENT=" in doc["reason"]
    _no_signal_written(root)


def test_reducer_payload_arms_the_deadman_net_unconditionally(tmp_path):
    proc, _root = _drive(tmp_path)
    doc = _payload(proc)
    assert doc.get("wakeup") == SENTINEL_NET, doc
    # Exactly the three keys: the contract's two plus the additive extension.
    assert set(doc) == {"decision", "reason", "wakeup"}, sorted(doc)


def test_the_payload_is_identical_under_every_harness_marker(tmp_path):
    """Positive control for harness-agnosticism: no marker changes a byte."""
    _proc, root = _drive(tmp_path)
    docs = {
        label: _payload(_run_hook_as_runner(root, extra_env=env))
        for label, env in (("claude-code", CLAUDE_CODE), ("vessel", VESSEL), ("none", NO_MARKER))
    }
    assert docs["claude-code"] == docs["vessel"] == docs["none"], docs
    assert docs["none"]["wakeup"] == SENTINEL_NET
    assert docs["none"]["reason"].startswith(CONTRACT_HEAD)
