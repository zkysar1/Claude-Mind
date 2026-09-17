"""EXECUTION coverage for the stop hook's vessel vocabulary (2026-09-17).

THE DEFECT. The hook's BLOCK reason -- the loop's life support -- was written in
Claude Code's tool names: "Your FIRST action MUST be: Skill('aspirations') with
args='loop' ... Call the Skill tool IMMEDIATELY." Promoted unchanged to a Mind
whose vessel is zakcode, that line names a tool the model's tool list does not
show (its tools are use_skill and schedule_wakeup). Measured on a downstream
vessel running a small model: it answered the imperative in PROSE ("Verdict: ...
loop resurrected"), the hook BLOCKed with the same foreign names, and the two
spun for hours. The fix routes the spelling through _harness_caps.py -- one
owner, read from the CLAUDECODE / ZAKCODE_* env markers the hook inherits from
the harness that fired it -- and, on a harness that honours it, arms the deadman
net FROM THE HOOK (`wakeup` key, Zak-Code ADR-0102) instead of asking the model.

WHAT THIS PINS
  * Claude Code: the reason is BYTE-IDENTICAL to the pre-change hook and the
    payload carries no `wakeup` key -- live Claude Code behaviour is unchanged.
  * zakcode: the reason names use_skill(name='aspirations', args='loop') and
    "the use_skill tool", carries no Claude Code name, and arms the sentinel
    net at 600s. The decision is still BLOCK; no signal is written.
  * unknown harness: Claude Code's spelling -- a vessel is never guessed.
  * fail-open: an unreachable vocabulary module leaves the Claude Code payload.
  * the worker-net reason (a Body between work units) follows the same table.

HARNESS REUSE (deliberate): the reducer fixture is test_stop_hook_gate_integration's
(same tmp PROJECT_ROOT, same production-shaped env), the worker fixture is
test_stop_hook_in_flight_integration's. Both runners now PIN Claude Code by
default and take `harness=` to name another; these tests are the only callers
that pass it.
"""
from __future__ import annotations

import json

from test_stop_hook_gate_integration import (  # noqa: E402
    _agent_dir,
    _blocked,
    _drive,
    _run_hook_as_runner,
)
from test_stop_hook_in_flight_integration import _drive as _drive_worker  # noqa: E402
from test_stop_hook_in_flight_integration import _hook_log  # noqa: E402

VESSEL = {"ZAKCODE_SESSION": "x"}
CLAUDE_CODE = {"CLAUDECODE": "1"}
UNKNOWN: dict = {}

# The pre-change reducer reason, verbatim -- the Claude Code contract.
CLAUDE_CODE_HEAD = (
    "Turn ended without a Skill(aspirations) re-entry (autocompact OR a text "
    "summary terminated the turn). Your FIRST action MUST be: Skill('aspirations') "
    "with args='loop'. Do NOT manually select goals. Do NOT run Bash commands "
    "first. Call the Skill tool IMMEDIATELY."
)
VESSEL_HEAD = (
    "Turn ended without a use_skill(aspirations) re-entry (autocompact OR a text "
    "summary terminated the turn). Your FIRST action MUST be: "
    "use_skill(name='aspirations', args='loop'). Do NOT manually select goals. "
    "Do NOT run Bash commands first. Call the use_skill tool IMMEDIATELY."
)
SENTINEL_NET = {"prompt": "<<autonomous-loop-dynamic>>", "delay_seconds": 600}


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


# --------------------------------------------------------------------------
# 1. Reducer BLOCK reason per harness
# --------------------------------------------------------------------------

def test_claude_code_reason_is_byte_identical_and_carries_no_wakeup(tmp_path):
    proc, root = _drive(tmp_path)  # the runner pins CLAUDECODE=1 by default
    assert _blocked(proc)
    doc = _payload(proc)
    assert doc["reason"].startswith(CLAUDE_CODE_HEAD), doc["reason"][:400]
    assert "wakeup" not in doc, "Claude Code's payload must stay byte-identical"
    _no_signal_written(root)


def test_zakcode_reason_names_the_vessels_tools_and_arms_the_net(tmp_path):
    """THE FIX. Same fixture, the vessel's env markers: the model reads its own
    tool's name, and the net is armed by the hook rather than asked of it."""
    _proc, root = _drive(tmp_path)
    proc = _run_hook_as_runner(root, harness=VESSEL)
    assert _blocked(proc), "the decision must still be BLOCK on a vessel"
    doc = _payload(proc)
    assert doc["reason"].startswith(VESSEL_HEAD), doc["reason"][:400]
    assert "Skill(" not in doc["reason"].replace("use_skill(", ""), doc["reason"][:400]
    assert "ScheduleWakeup" not in doc["reason"]
    assert doc.get("wakeup") == SENTINEL_NET, doc
    # The rest of the reason (agent prefix, banner) is the same additive tail.
    assert "Prefix all Bash with MIND_AGENT=" in doc["reason"]
    _no_signal_written(root)


def test_the_two_harnesses_differ_only_where_the_vocabulary_says(tmp_path):
    """Positive control for every absence assertion above: the reasons differ,
    and ONLY by the tool names -- the checkpoint / agent / banner tail is shared."""
    _proc, root = _drive(tmp_path)
    cc = _payload(_run_hook_as_runner(root, harness=CLAUDE_CODE))["reason"]
    zak = _payload(_run_hook_as_runner(root, harness=VESSEL))["reason"]
    assert cc != zak
    assert cc[len(CLAUDE_CODE_HEAD):] == zak[len(VESSEL_HEAD):]


def test_an_unknown_harness_keeps_the_claude_code_spelling(tmp_path):
    """Never guess a vessel: no marker at all -> Claude Code's names, no net."""
    _proc, root = _drive(tmp_path)
    doc = _payload(_run_hook_as_runner(root, harness=UNKNOWN))
    assert doc["reason"].startswith(CLAUDE_CODE_HEAD)
    assert "wakeup" not in doc


def test_an_unreachable_vocabulary_module_falls_open_to_the_claude_code_payload(tmp_path):
    """The reducer imports _harness_caps in-process; if that import fails the
    except-branch must reproduce the Claude Code payload exactly, even on a
    vessel -- the pre-change behaviour, never a garbled reason or a crash."""
    def _unreachable(text: str) -> str:
        marker = 'HOOK_SCRIPTS_DIR="$CORE_ROOT/scripts"'
        assert marker in text
        return text.replace(marker, 'HOOK_SCRIPTS_DIR="/nonexistent-vocab-dir"')

    proc, root = _drive(tmp_path, mutate=_unreachable)
    assert _blocked(proc)
    doc = _payload(_run_hook_as_runner(root, harness=VESSEL))
    assert doc["reason"].startswith(CLAUDE_CODE_HEAD)
    assert "wakeup" not in doc


# --------------------------------------------------------------------------
# 2. Worker-net BLOCK reason per harness
# --------------------------------------------------------------------------

WORKER_CLAUDE_CODE = (
    "Worker Body turn ended without a Skill(worker-loop) re-entry (a text summary "
    "or autocompact terminated the turn). Your FIRST action MUST be: "
    "Skill('worker-loop') — NOT Skill('aspirations'), which is the REDUCER-only "
    "re-entry (guard-517/guard-463)."
)
WORKER_VESSEL = (
    "Worker Body turn ended without a use_skill(worker-loop) re-entry (a text "
    "summary or autocompact terminated the turn). Your FIRST action MUST be: "
    "use_skill(name='worker-loop') — NOT use_skill(name='aspirations'), which is "
    "the REDUCER-only re-entry (guard-517/guard-463)."
)


def test_worker_net_reason_is_byte_identical_on_claude_code(tmp_path):
    proc, _shard, root = _drive_worker(tmp_path, closing=False, scrub_env=True)
    assert _blocked(proc), _hook_log(root)
    assert _payload(proc)["reason"].startswith(WORKER_CLAUDE_CODE), proc.stdout


def test_worker_net_reason_names_the_vessels_tools_on_zakcode(tmp_path):
    proc, _shard, root = _drive_worker(tmp_path, closing=False, scrub_env=True,
                                       harness=VESSEL)
    assert _blocked(proc), _hook_log(root)
    reason = _payload(proc)["reason"]
    assert reason.startswith(WORKER_VESSEL), reason
    assert "Skill(" not in reason.replace("use_skill(", "")
    assert "gate=worker-net" in _hook_log(root)
