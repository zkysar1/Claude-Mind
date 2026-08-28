#!/usr/bin/env python3
"""Telemetry for the bare-bash authoring hook ().

The hook wrote NOTHING on deny, so three things were true at once: its firing
rate was unmeasurable (and it could never be assessed for retirement under
guard-769), rb-5255's central claim -- that ad-hoc throwaway code is where the
bare-bash pattern actually returns -- was untestable, and because the hook is
fail-open BY DESIGN a regression that swallowed a real deny left no trace at
all. These tests pin the instrumentation that closes that.

WHAT IS DELIBERATELY *NOT* LOGGED, because a reader will otherwise call it a
gap: the hot path. `main()` approves and returns long before any logging call
whenever the Bash command carries no inline Python -- which is essentially
every Bash command on the box. A record there would be pure volume, and the
goal's own criterion 2 requires an approved command to produce no line. Hook
LIVENESS is covered instead by the registration assertion at the bottom
(guard-1943: a gate nothing calls is indistinguishable from one that always
passes), which costs nothing at runtime.

TWO TRAPS THIS FILE HAS TO STEP AROUND, both of which would make it pass
vacuously:
  1. `_gate_log.log()` RETURNS EARLY under PYTEST_CURRENT_TEST unless
     GATE_LOG_ALLOW_PYTEST is set (_gate_log.py, the pytest-suppression
     branch). Without that env var every assertion below would read zero
     records and the suite would go green over a completely dead writer.
  2. The destination depends on the storage lane: `_spool_active()` True routes
     to the machine-local spool, False routes to the segmented store. The tests
     therefore glob the redirected META_DIR rather than naming one file, so
     they pin BEHAVIOUR (a record landed) and not the lane that happens to be
     configured on the box running them (guard-4348: verify at the file the
     writer actually touches).
"""
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]      # core/scripts
REPO = Path(__file__).resolve().parents[3]         # repo root
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# bare-bash-authoring-gate.py has hyphens in the filename -> load via importlib
# (the repo's standard shape for a hyphenated script; a plain import raises
# ModuleNotFoundError). The module's __main__ guard keeps main() from running
# at import time.
_spec = importlib.util.spec_from_file_location(
    "bare_bash_authoring_gate", str(SCRIPTS / "bare-bash-authoring-gate.py")
)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

DENY_CMD = """python3 -c 'import subprocess; subprocess.run(["bash", "x.sh"])'"""
CLEAN_CMD = """python3 -c 'print(1 + 1)'"""


def _records(meta_dir):
    """Every firing record written under a redirected META_DIR, either lane."""
    out = []
    for f in Path(meta_dir).glob("gate-firings*.jsonl"):
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _run_hook(monkeypatch, tmp_path, command):
    """Drive the hook's main() over one Bash payload, returning its records."""
    import _gate_log

    monkeypatch.setenv("GATE_LOG_ALLOW_PYTEST", "1")
    monkeypatch.setenv("MIND_AGENT", "testagent")
    monkeypatch.setattr(_gate_log, "META_DIR", tmp_path)

    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    with pytest.raises(SystemExit):
        gate.main()
    return _records(tmp_path)


# ── criterion 1: a deny writes exactly one record, with the required fields ──

def test_denied_payload_writes_exactly_one_record(monkeypatch, tmp_path):
    recs = _run_hook(monkeypatch, tmp_path, DENY_CMD)
    assert len(recs) == 1, f"expected exactly 1 record, got {len(recs)}: {recs}"
    r = recs[0]
    assert r["gate_id"] == "bare-bash-authoring-gate"
    assert r["decision"] == "block"
    assert r["ts"], "record carries no timestamp"
    assert r["agent"] == "testagent", "record does not carry the acting agent"
    assert r.get("trigger_matched"), "record does not say WHICH form matched"


def test_denied_record_names_the_syntactic_form(monkeypatch, tmp_path):
    """The form is the field that makes rb-5255's claim testable at all."""
    recs = _run_hook(monkeypatch, tmp_path, DENY_CMD)
    assert "[" in recs[0]["trigger_matched"], recs[0]["trigger_matched"]


# ── criterion 2: deny-only, not fire-only ────────────────────────────────────

def test_approved_command_writes_nothing(monkeypatch, tmp_path):
    assert _run_hook(monkeypatch, tmp_path, CLEAN_CMD) == []


def test_command_with_no_inline_python_writes_nothing(monkeypatch, tmp_path):
    """The hot path. This is the case that must stay free."""
    assert _run_hook(monkeypatch, tmp_path, "ls -la /tmp") == []


# ── criterion 3: the logging path is fail-open ───────────────────────────────

def test_logging_failure_does_not_change_the_verdict(monkeypatch, tmp_path):
    """A broken writer must still deny, and must still exit the same way."""
    import _gate_log

    def _boom(*a, **k):
        raise OSError("simulated telemetry failure")

    monkeypatch.setenv("GATE_LOG_ALLOW_PYTEST", "1")
    monkeypatch.setattr(_gate_log, "log", _boom)
    payload = {"tool_name": "Bash", "tool_input": {"command": DENY_CMD}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    with pytest.raises(SystemExit):
        gate.main()
    assert "BLOCKED by bare-bash-authoring-gate" in buf.getvalue()


# ── criterion 4: discriminating power (guard-1475) ───────────────────────────

def test_removing_the_logging_call_makes_criterion_1_fail(monkeypatch, tmp_path):
    """Anti-vacuity: neuter _log and criterion 1 must FAIL.

    Without this the three tests above would pass just as happily against a
    hook that never logged anything, since 'no records' is also what a silent
    writer produces.
    """
    monkeypatch.setattr(gate, "_log", lambda *a, **k: None)
    recs = _run_hook(monkeypatch, tmp_path, DENY_CMD)
    assert recs == [], "expected the neutered hook to write nothing"


# ── liveness: the hook is actually wired (guard-1943) ────────────────────────

def test_hook_is_registered_in_settings_json():
    """A gate nothing invokes is indistinguishable from one that always passes.

    Deny-only telemetry cannot detect de-registration: zero records is also
    what a correctly-behaving hook produces on a quiet day. This assertion is
    the liveness half, and it is why per-command logging is not needed for it.
    """
    settings = json.loads((REPO / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert "bare-bash-authoring-gate" in json.dumps(settings)


def test_gate_id_matches_gates_yaml():
    """guard-502: the gate_id MUST match an id in core/config/gates.yaml, or the
    retirement evaluator never sees this gate's firings."""
    import yaml
    d = yaml.safe_load((REPO / "core" / "config" / "gates.yaml").read_text(encoding="utf-8"))
    assert gate.GATE_ID in {g["id"] for g in d["gates"]}
