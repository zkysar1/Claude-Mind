"""An unreadable blocked view must not render as a clean sweep ().

THE DEFECT. `_read_blocked()` returned a bare `{}` on ANY failure -- non-zero
rc, timeout, unparseable stdout. The caller could not tell that `{}` from a
genuinely empty blocked view, so `run()` emitted `scanned: 0, eligible: 0` with
all three finding-lists empty whether the sweep saw nothing or saw NOTHING AT
ALL. The always-run battery reads exactly those lists, so an unmeasured run was
byte-indistinguishable from a clean one on the ONE tier whose signal is not
optional -- the fleet's only automated path from a stale dependency to a human.

MEASURED (foxtrot, LAPTOP-3IOFCNEO, WSL2 6.18.33.2, 2026-09-09, recorded on
g-115-9447): the lane's inner `goal-selector.sh blocked` call timed out at 180s
and the lane reported `scanned:0`; a run ~40 minutes earlier on the same box
reported `scanned:12`. Same box, same corpus, ~40 minutes apart -- the coverage
is nondeterministic and the zero is not a result. guard-2298's class: an
except-branch converting a failure into a confident zero.

THE FAIL-OPEN BEHAVIOUR IS NOT THE DEFECT and is deliberately preserved -- an
unreadable view must never stop the sweep. Only the RENDERING changes. That is
the goal's instruction, not this author's preference. g-115-9447's own SCOPE
field reads "Do NOT fix by raising the bound before (a) is measured", and
foxtrot's progress_note on that goal (read from the live record 2026-09-09)
says verbatim: "DO NOT FIX BY RAISING THE BOUNDS -- this goal already said so
for (a) and the reason is now concrete: the bound is not the defect, the
fail-open rendering is", and prescribes "(1) make the timeout branch report
UNMEASURED/INCONCLUSIVE rather than scanned:0 (the roblox-classname-check rc=2
posture, rb-245)". This file is that repair.

WHY THE REASON RIDES AN INTERNAL KEY rather than a changed return type. The
first version of this fix returned a `(view, reason)` tuple. That signature is
cleaner and it broke NINE existing tests across `test_dependency_timeout_stale`
and `test_dependency_timeout_reprobe`, every one of which patches
`_read_blocked` with a plain `lambda: {"blocked_goals": [...]}`. The internal
`_unmeasured_reason` key carries the same information while keeping the dict
contract, so a patched plain dict is correctly read as MEASURED and those tests
are untouched. Same convention as the `_source` / `_archived` keys the module
already stamps on goal dicts.

THE DISCRIMINATOR (guard-3134). A test that merely asserts `scanned == 0` on the
failure path passes against the UNFIXED code, because the unfixed code also
reports 0 -- it would prove nothing. Every case below asserts on
`blocked_view_measured` / `unmeasured_reason` / the rc, none of which exist
before this fix, so each one fails against the pre-fix shape. Case 3 pins the
two TOGETHER (a zero beside the un-measurement marker), which is the property
that actually distinguishes the fixed behaviour from the broken one.

This is a TWO-COMPONENT regression (producer `dependency-timeout-check.py` +
consumer `precheck-always-run-battery.py`), so guard-1220 requires a mutation
proof; case 5 is the consumer half that a producer-only test would miss.

Run: py -3 -m pytest core/scripts/tests/test_dependency_timeout_unmeasured.py -v
"""
import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))


def _load(mod_name, filename):
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPT_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load("dependency_timeout_unmeasured_module", "dependency-timeout-check.py")
B = _load("always_run_battery_unmeasured_module", "precheck-always-run-battery.py")


class _Proc:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def _args(**kw):
    ns = argparse.Namespace(
        apply=False, threshold_hours=None, agent="alpha",
        board_escalation_log=None, no_board=True)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def _neutralize_io(monkeypatch):
    """Stub every I/O path in run() EXCEPT the blocked view under test."""
    monkeypatch.setattr(M, "_read_goal_index", lambda: {})
    monkeypatch.setattr(M, "_read_recent_escalations", lambda *a, **k: set())
    monkeypatch.setattr(M, "_load_threshold_hours", lambda a: 36.0)
    monkeypatch.setattr(M, "_resolve_self_agent", lambda a: "alpha")
    monkeypatch.setattr(M, "_prose_only_dependency_census", lambda idx: [])


# --- 1. the producer's failure paths carry a REASON, not a bare {} -----------

def test_timeout_yields_a_reason(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="goal-selector.sh", timeout=180)
    monkeypatch.setattr(M.subprocess, "run", boom)
    view = M._read_blocked()
    assert not view.get("blocked_goals"), "fail-open must still yield no entries"
    reason = M._blocked_unmeasured(view)
    assert reason is not None, "a timeout must be reported as UNMEASURED"
    assert "goal-selector" in reason


def test_nonzero_rc_yields_a_reason(monkeypatch):
    monkeypatch.setattr(M.subprocess, "run", lambda *a, **k: _Proc(returncode=124))
    view = M._read_blocked()
    reason = M._blocked_unmeasured(view)
    assert reason is not None and "124" in reason


def test_unparseable_stdout_yields_a_reason(monkeypatch):
    monkeypatch.setattr(M.subprocess, "run",
                        lambda *a, **k: _Proc(returncode=0, stdout="not json"))
    assert M._blocked_unmeasured(M._read_blocked()) is not None


def test_a_non_dict_view_is_unmeasured():
    # Never assume a shape you did not check: a caller handing back None or a
    # list must not be read as a clean empty sweep.
    assert M._blocked_unmeasured(None) is not None
    assert M._blocked_unmeasured([]) is not None


# --- 2. the success path reports MEASURED ------------------------------------

def test_success_reports_measured(monkeypatch):
    payload = '{"blocked_goals": [{"goal_id": "g-1-1", "block_reason": "dependency"}]}'
    monkeypatch.setattr(M.subprocess, "run",
                        lambda *a, **k: _Proc(returncode=0, stdout=payload))
    view = M._read_blocked()
    assert M._blocked_unmeasured(view) is None, "a clean read is not unmeasured"
    assert view["blocked_goals"][0]["goal_id"] == "g-1-1"


def test_a_plain_patched_dict_reads_as_measured():
    # The nine existing tests in the sibling files patch _read_blocked with
    # exactly this shape. If this ever fails, the contract broke under them.
    assert M._blocked_unmeasured({"blocked_goals": []}) is None


# --- 3. the wiring: a zero must arrive BESIDE the un-measurement marker ------

def test_run_marks_the_zero_as_unmeasured(monkeypatch):
    _neutralize_io(monkeypatch)
    monkeypatch.setattr(M, "_read_blocked",
                        lambda: {M.UNMEASURED_KEY: "goal-selector blocked rc=124"})
    out = M.run(_args())
    # The zero is still emitted -- fail-open is preserved, deliberately.
    assert out["scanned"] == 0 and out["eligible"] == 0
    # ...but it can no longer be read as a clean sweep.
    assert out["blocked_view_measured"] is False
    assert out["unmeasured_reason"] == "goal-selector blocked rc=124"


def test_run_marks_a_genuine_empty_as_measured(monkeypatch):
    _neutralize_io(monkeypatch)
    monkeypatch.setattr(M, "_read_blocked", lambda: {"blocked_goals": []})
    out = M.run(_args())
    assert out["scanned"] == 0 and out["eligible"] == 0
    assert out["blocked_view_measured"] is True
    assert out["unmeasured_reason"] is None
    # The two runs above emit the SAME counts. That is the whole point: the
    # counts cannot discriminate, so the flag must.


# --- 4. the rc posture (rb-245 roblox-classname-check: 2 == INCONCLUSIVE) ----

def test_main_exits_2_when_unmeasured(monkeypatch, capsys):
    _neutralize_io(monkeypatch)
    monkeypatch.setattr(M, "_read_blocked",
                        lambda: {M.UNMEASURED_KEY: "goal-selector blocked rc=124"})
    monkeypatch.setattr(sys, "argv", ["dependency-timeout-check.py"])
    rc = M.main()
    capsys.readouterr()
    assert rc == 2, "an unmeasured sweep is INCONCLUSIVE, neither clean nor failed"


def test_main_exits_0_when_measured(monkeypatch, capsys):
    _neutralize_io(monkeypatch)
    monkeypatch.setattr(M, "_read_blocked", lambda: {"blocked_goals": []})
    monkeypatch.setattr(sys, "argv", ["dependency-timeout-check.py"])
    rc = M.main()
    capsys.readouterr()
    assert rc == 0


# --- 5. the CONSUMER half: the battery must turn the flag into a finding -----

def _lane():
    return next(l for l in B.LANES if l["name"] == "dependency-timeout-check")


def test_battery_reports_a_finding_when_unmeasured():
    payload = {"scanned": 0, "eligible": 0, "candidates": [], "escalated": [],
               "needs_user_notification": [], "blocked_view_measured": False}
    detail = B._findings_for(_lane(), payload)
    assert detail, "an unmeasured lane must not read as clean to the battery"
    assert any("blocked_view_measured" in d for d in detail)


def test_battery_stays_clean_on_a_measured_empty():
    payload = {"scanned": 0, "eligible": 0, "candidates": [], "escalated": [],
               "needs_user_notification": [], "blocked_view_measured": True}
    assert B._findings_for(_lane(), payload) == [], \
        "a genuinely empty measured sweep must still report clean"


def test_battery_lane_actually_reads_the_flag():
    # Pins the WIRING, not just the helper: if someone removes the key from the
    # lane's `finds.false` tuple, the cases above still pass via a hand-built
    # lane dict -- this is the one that fails.
    assert "blocked_view_measured" in _lane()["finds"]["false"]
