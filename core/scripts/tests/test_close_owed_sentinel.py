""" -- the owed-close sentinel (`pending_close_owed`).

`iteration-close.sh --phase verify` marks a deep, non-recurring goal completed
BEFORE its spark, state-update and learning-gate run, and until this sentinel
nothing durable named those owed steps: a compaction in that window re-selected a
new goal and the closed one ended with no encoding (g-374-415).

What is pinned here, and where each pin comes from:

  1. HELPER    close-owed-sentinel.py -- the payload, the write/clear verdicts, the
               CLI, and fail-open in the direction that loses nothing.
  2. GLUE      the two bash functions in iteration-close.sh, EXTRACTED from the
               real script (not re-typed here) and run against stub wm wrappers,
               so the wm I/O, the refusal and the non-fatal WARN paths execute.
  3. SITING    source pins on iteration-close.sh: the write sits right after the
               status write in do_verify and ahead of the bookkeeping that can abort
               the phase, the clear sits after state-update's checkpoint stamp, and
               nothing on the spark side touches the slot.
  4. BATTERY   a simulated compaction (a WM file holding the payload) lists the owed
               close at phase -0.5c.3, ahead of the spark sentinel, and the dispatch
               text carries the protocol (it IS the protocol: no SKILL.md body).
  5. SURVIVAL  the slot outlives the two WM cleanup paths that would otherwise eat
               it -- consolidation reset (RESET_SURVIVING_SLOTS) and idle eviction
               (protected_slots) -- each with a negative control so a cleanup that
               silently did nothing cannot pass.
  6. MERGE     generalize-down (body-merge.py) must not hand a cleared owed close
               back from a Body that forked while the slot was set; the slot is
               reset- and prune-protected, so a resurrected copy would never leave.

NOT COVERED, so a green run is not read as more than it is:
  * No end-to-end `iteration-close.sh --phase verify|state-update` run. Both phases
    call daemon-routed aspirations and wm wrappers (no CLI fallback), so a hermetic
    subprocess is infeasible; section 2 runs the real glue against stubs and
    section 3 pins the call sites, the same split test_pending_phase_6_spark_
    integration.py makes.
  * The daemon's own reset()/prune() loops are not driven in-process. Membership on
    the daemon copy is pinned by AST, test_wm_reset_cadence.py pins the two copies
    equal, and both prune paths read the one memory-pipeline.yaml list pinned here.
  * Whether the model follows the dispatch text. The text is pinned, not obeyed.

Why a sentinel, why it clears at state-update and not at the spark, the refusal
rule, and the limits: core/config/rationale/owed-close-sentinel.md
"""
from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

TESTS_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = TESTS_DIR.parent                      # core/scripts/
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
for _p in (CORE_SCRIPTS, TESTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import wm  # noqa: E402
from _bash_helpers import BASH  # noqa: E402  (rb-1472: bin-first absolute bash)

HELPER = CORE_SCRIPTS / "close-owed-sentinel.py"
ITERATION_CLOSE_SH = CORE_SCRIPTS / "iteration-close.sh"
BATTERY = CORE_SCRIPTS / "orchestrator-entry-battery.py"
SLOT = "pending_close_owed"
OWED = ["spark", "state-update", "learning-gate"]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cos = _load("close_owed_sentinel_under_test", HELPER)
oeb = _load("orchestrator_entry_battery_owed_close", BATTERY)


# ===========================================================================
# 1. HELPER -- close-owed-sentinel.py
# ===========================================================================


def test_helper_names_the_slot_this_file_pins():
    assert cos.SLOT == SLOT
    assert cos.OWED_STEPS == OWED


def test_payload_shape():
    p = cos.build_payload("g-1-1", "world", "deep", "2026-10-15T00:30:00",
                          "did a thing", now="2026-10-15T00:31:00")
    assert p == {
        "goal_id": "g-1-1",
        "source": "world",
        "outcome": "deep",
        "producer": "nonrecurring-verify",
        "started_at": "2026-10-15T00:30:00",
        "set_at": "2026-10-15T00:31:00",
        "owed": OWED,
        "summary": "did a thing",
    }


def test_payload_summary_is_capped_and_none_safe():
    capped = cos.build_payload("g", "world", "deep", "t", "x" * 1000)["summary"]
    assert len(capped) == cos.SUMMARY_CAP == 240
    assert cos.build_payload("g", "world", "deep", "t", None)["summary"] == ""


def test_payload_owed_list_is_a_copy_of_the_module_constant():
    p = cos.build_payload("g", "world", "deep", "t")
    p["owed"].append("tampered")
    assert cos.OWED_STEPS == OWED, "build_payload handed out the module's own list"


def test_payload_set_at_defaults_to_now():
    p = cos.build_payload("g", "world", "deep", "t")
    assert abs((datetime.now() - datetime.fromisoformat(p["set_at"])).total_seconds()) < 60


@pytest.mark.parametrize("raw,expected", [
    (None, None),
    ("", None),
    ("  \n", None),
    ("null", None),
    ("{not json", None),
    ("[1, 2]", None),
    ('"text"', None),
    ("7", None),
    ('{"goal_id": "g-1-1"}', {"goal_id": "g-1-1"}),
    ('  {"goal_id": "g-1-1"}\n', {"goal_id": "g-1-1"}),
])
def test_slot_payload_reads_only_an_object(raw, expected):
    assert cos.slot_payload(raw) == expected


@pytest.mark.parametrize("raw", [
    None, "", "null", "garbage", "[1, 2]", "42",
    '{"summary": "no goal id"}', '{"goal_id": null}', '{"goal_id": ""}',
])
def test_write_verdict_writes_when_nothing_attributable_is_held(raw):
    assert cos.write_verdict(raw, "g-1-1") == "write"


def test_write_verdict_rewrites_its_own_goal():
    assert cos.write_verdict(json.dumps({"goal_id": "g-1-1"}), "g-1-1") == "write"


def test_write_verdict_refuses_another_goals_obligation():
    """guard-2104: the payload is derived from ONE close, so overwriting another
    goal's unconsumed payload cancels its obligation with nothing reporting it."""
    assert cos.write_verdict(json.dumps({"goal_id": "g-2-2"}), "g-1-1") == "refuse:g-2-2"


@pytest.mark.parametrize("raw", [None, "", "null", "garbage", "[1]", "7"])
def test_clear_verdict_is_absent_when_nothing_is_readable(raw):
    assert cos.clear_verdict(raw, "g-1-1") == "absent"


def test_clear_verdict_clears_only_its_own_goal():
    assert cos.clear_verdict(json.dumps({"goal_id": "g-1-1"}), "g-1-1") == "clear"
    assert cos.clear_verdict(json.dumps({"goal_id": "g-2-2"}), "g-1-1") == "keep:g-2-2"


def test_clear_verdict_keeps_an_unattributed_payload():
    assert cos.clear_verdict(json.dumps({"summary": "x"}), "g-1-1") == "keep:unattributed"


def _cli(monkeypatch, capsys, argv, stdin=""):
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    rc = cos.main(argv)
    return rc, capsys.readouterr().out


def test_cli_payload_emits_one_json_object(monkeypatch, capsys):
    rc, out = _cli(monkeypatch, capsys, [
        "payload", "--goal", "g-1-1", "--source", "world", "--outcome", "deep",
        "--started-at", "2026-10-15T00:30:00", "--summary", "s"])
    assert rc == 0
    p = json.loads(out)
    assert (p["goal_id"], p["started_at"], p["summary"]) == ("g-1-1", "2026-10-15T00:30:00", "s")


def test_cli_check_commands_print_the_verdict_and_exit_zero(monkeypatch, capsys):
    other = json.dumps({"goal_id": "g-2-2"})
    for cmd, stdin, want in [
        ("check-write", "null", "write"),
        ("check-write", other, "refuse:g-2-2"),
        ("check-clear", "null", "absent"),
        ("check-clear", other, "keep:g-2-2"),
        ("check-clear", json.dumps({"goal_id": "g-1-1"}), "clear"),
    ]:
        rc, out = _cli(monkeypatch, capsys, [cmd, "--goal", "g-1-1"], stdin=stdin)
        assert (rc, out.strip()) == (0, want), f"{cmd} on {stdin!r}"


def test_check_write_fails_open_to_write(monkeypatch, capsys):
    """A helper error must not strand the close without its sentinel."""
    def boom(raw, goal):
        raise RuntimeError("boom")
    monkeypatch.setattr(cos, "write_verdict", boom)
    rc, out = _cli(monkeypatch, capsys, ["check-write", "--goal", "g-1-1"], stdin="null")
    assert (rc, out) == (0, "write")


def test_check_clear_fails_open_to_leaving_the_slot(monkeypatch, capsys):
    """The direction that loses nothing: a stuck sentinel nags and has a manual
    clear, a wrongly cleared one drops the obligation silently."""
    def boom(raw, goal):
        raise RuntimeError("boom")
    monkeypatch.setattr(cos, "clear_verdict", boom)
    rc, out = _cli(monkeypatch, capsys, ["check-clear", "--goal", "g-1-1"], stdin="null")
    assert (rc, out) == (0, "absent")


def test_payload_failure_exits_nonzero_with_no_stdout(monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(cos, "build_payload", boom)
    rc, out = _cli(monkeypatch, capsys, [
        "payload", "--goal", "g", "--source", "world", "--outcome", "deep", "--started-at", "t"])
    assert rc == 1 and out == ""


def _py(*argv, stdin=""):
    r = subprocess.run([sys.executable, str(HELPER), *argv], input=stdin,
                       capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout, r.stderr


def test_subprocess_round_trip_payload_into_both_checks():
    """The real CLI the bash glue calls: a payload built for one goal is refused
    for a different goal and cleared by its own."""
    rc, payload, _ = _py("payload", "--goal", "g-1-1", "--source", "world",
                         "--outcome", "deep", "--started-at", "2026-10-15T00:30:00")
    assert rc == 0
    assert _py("check-write", "--goal", "g-2-2", stdin=payload)[1] == "refuse:g-1-1"
    assert _py("check-write", "--goal", "g-1-1", stdin=payload)[1] == "write"
    assert _py("check-clear", "--goal", "g-1-1", stdin=payload)[1] == "clear"
    assert _py("check-clear", "--goal", "g-2-2", stdin=payload)[1] == "keep:g-1-1"


# ===========================================================================
# 2. GLUE -- the real bash functions, run against stub wm wrappers
# ===========================================================================


def _function_source(name: str) -> str:
    """The REAL function definition, sliced out of iteration-close.sh: from its
    column-0 `name() {` line through the next column-0 `}`. Extraction (not a
    hand-copied reproduction) is what keeps this test from drifting off the script."""
    lines = ITERATION_CLOSE_SH.read_text(encoding="utf-8").splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith(f"{name}() {{")), None)
    assert start is not None, f"{name}() not found in iteration-close.sh -- moved or renamed; update the harness"
    end = next((j for j in range(start + 1, len(lines)) if lines[j] == "}"), None)
    assert end is not None, f"closing brace of {name}() not found"
    return "\n".join(lines[start:end + 1])


def _chmod_x(p: Path) -> None:
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run_glue(call: str, *, slot_value, goal: str = "g-NEW-01", source: str = "world",
              outcome: str = "deep", summary: str = "a deep framework close",
              now_iso: str = "2026-10-15T00:30:00", set_rc: int = 0, read_rc: int = 0,
              with_helper: bool = True) -> dict:
    """Run `call` (one of the two functions) the way iteration-close.sh does, with
    stub wm wrappers and no daemon.

    slot_value is what `wm-read.sh pending_close_owed --json` prints (None = nothing).
    set_rc is the exit code of the stub verified-wm-set.sh; read_rc that of wm-read.sh.
    Returns rc/out/err, `set` ({"argv","stdin"} when verified-wm-set.sh ran, else None)
    and `read_argv`. `set -euo pipefail` matches the script, so an unguarded failure
    in either function would abort here exactly as it would in do_verify.
    """
    td = Path(tempfile.mkdtemp(prefix="owedclose-"))
    try:
        sd = td / "scripts"
        (td / "bin").mkdir()
        sd.mkdir()
        if with_helper:
            shutil.copy(HELPER, sd / "close-owed-sentinel.py")

        read_argv, set_argv, set_stdin = td / "read_argv", td / "set_argv", td / "set_stdin"
        reader = ["#!/usr/bin/env bash", f'printf "%s" "$*" > {json.dumps(str(read_argv))}']
        if slot_value is not None:
            (td / "slot_value").write_text(slot_value, encoding="utf-8")
            reader.append(f"cat {json.dumps(str(td / 'slot_value'))}")
        reader.append(f"exit {read_rc}")
        (sd / "wm-read.sh").write_text("\n".join(reader) + "\n", encoding="utf-8")

        (sd / "verified-wm-set.sh").write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s" "$*" > {json.dumps(str(set_argv))}\n'
            f"cat > {json.dumps(str(set_stdin))}\n"
            f"exit {set_rc}\n", encoding="utf-8")

        py3 = td / "bin" / "python3"
        py3.write_text(f'#!/usr/bin/env bash\nexec {json.dumps(sys.executable)} "$@"\n',
                       encoding="utf-8")
        for stub in (sd / "wm-read.sh", sd / "verified-wm-set.sh", py3):
            _chmod_x(stub)

        prog = (
            "set -euo pipefail\n"
            "_winpath() { printf '%s' \"$1\"; }\n"
            f"{_function_source('_write_close_owed_sentinel')}\n"
            f"{_function_source('_clear_close_owed_sentinel')}\n"
            f"{call}\n"
        )
        env = dict(os.environ)
        env.update({
            "PATH": str(td / "bin") + os.pathsep + env.get("PATH", ""),
            "SCRIPT_DIR": str(sd),
            "GOAL_ID": goal, "SOURCE": source, "OUTCOME": outcome,
            "SUMMARY": summary, "NOW_ISO": now_iso,
        })
        r = subprocess.run([BASH, "-c", prog], env=env, capture_output=True, text=True, timeout=60)
        set_call = None
        if set_argv.exists():
            set_call = {"argv": set_argv.read_text(encoding="utf-8"),
                        "stdin": set_stdin.read_text(encoding="utf-8") if set_stdin.exists() else ""}
        return {"rc": r.returncode, "out": r.stdout, "err": r.stderr, "set": set_call,
                "read_argv": read_argv.read_text(encoding="utf-8") if read_argv.exists() else None}
    finally:
        shutil.rmtree(td, ignore_errors=True)


HELD_OTHER = json.dumps({"goal_id": "g-OTHER-01", "source": "world", "outcome": "deep"})
HELD_SAME = json.dumps({"goal_id": "g-NEW-01", "source": "world", "outcome": "deep"})
WRITE = "_write_close_owed_sentinel"
CLEAR = "_clear_close_owed_sentinel"


def test_glue_functions_extract_from_the_real_script():
    assert "close-owed-sentinel.py" in _function_source(WRITE)
    assert "verified-wm-set.sh" in _function_source(WRITE)
    assert "check-clear" in _function_source(CLEAR)
    assert "verified-wm-set.sh" in _function_source(CLEAR)


def test_write_on_an_empty_slot_sets_the_full_payload():
    r = _run_glue(WRITE, slot_value="null", summary="closed the loop")
    assert r["rc"] == 0, r["err"]
    assert r["set"] is not None, f"no sentinel was written; stderr={r['err']!r}"
    assert r["set"]["argv"].strip() == SLOT
    p = json.loads(r["set"]["stdin"])
    assert (p["goal_id"], p["source"], p["outcome"]) == ("g-NEW-01", "world", "deep")
    assert p["producer"] == "nonrecurring-verify"
    assert p["started_at"] == "2026-10-15T00:30:00", "started_at must be verify's NOW_ISO, not the write time"
    assert p["owed"] == OWED
    assert p["summary"] == "closed the loop"
    datetime.fromisoformat(p["set_at"])
    assert r["read_argv"].strip() == f"{SLOT} --json"
    assert r["out"] == "", "the write must leave do_verify's stdout alone (callers parse it)"


def test_write_refuses_to_overwrite_another_goals_unconsumed_obligation():
    r = _run_glue(WRITE, slot_value=HELD_OTHER)
    assert r["rc"] == 0, "a refusal is non-fatal: the status write already landed"
    assert r["set"] is None, "the sentinel for another goal was overwritten"
    assert "REFUSING to overwrite pending_close_owed" in r["err"]
    assert "g-OTHER-01" in r["err"] and "guard-2104" in r["err"]
    assert r["out"] == ""


def test_write_over_its_own_goal_replaces_the_payload():
    r = _run_glue(WRITE, slot_value=HELD_SAME)
    assert r["rc"] == 0 and r["set"] is not None
    assert json.loads(r["set"]["stdin"])["goal_id"] == "g-NEW-01"


def test_write_proceeds_when_the_slot_read_fails():
    r = _run_glue(WRITE, slot_value=None, read_rc=1)
    assert r["rc"] == 0, r["err"]
    assert r["set"] is not None, "an unreadable slot must not cost the close its sentinel"


def test_write_warns_and_survives_when_the_set_does_not_verify():
    r = _run_glue(WRITE, slot_value="null", set_rc=1)
    assert r["rc"] == 0
    assert "sentinel write did not verify" in r["err"]


def test_write_warns_and_survives_when_the_helper_is_missing():
    r = _run_glue(WRITE, slot_value="null", with_helper=False)
    assert r["rc"] == 0
    assert r["set"] is None
    assert "could not build pending_close_owed payload" in r["err"]


def test_write_caps_the_summary_in_the_stored_payload():
    r = _run_glue(WRITE, slot_value="null", summary="y" * 1000)
    assert len(json.loads(r["set"]["stdin"])["summary"]) == 240


def test_write_carries_a_hostile_summary_literally():
    """The summary reaches the payload as an argv string, never through a shell
    re-parse: quotes, backslashes, command substitution and newlines survive."""
    nasty = 'He said "hi" \\ and `id` $(echo pwned) ${HOME}\nnext line'
    r = _run_glue(WRITE, slot_value="null", summary=nasty)
    assert r["rc"] == 0, r["err"]
    assert json.loads(r["set"]["stdin"])["summary"] == nasty


def test_clear_removes_its_own_goals_payload_with_null():
    r = _run_glue(CLEAR, slot_value=HELD_SAME)
    assert r["rc"] == 0, r["err"]
    assert r["set"] is not None, "the owed close was not cleared"
    assert r["set"]["argv"].strip() == SLOT
    assert r["set"]["stdin"].strip() == "null"
    assert r["out"] == ""


def test_clear_keeps_another_goals_obligation():
    r = _run_glue(CLEAR, slot_value=HELD_OTHER)
    assert r["rc"] == 0
    assert r["set"] is None, "state-update for one goal cleared a different goal's owed close"
    assert "pending_close_owed left set" in r["err"] and "g-OTHER-01" in r["err"]


@pytest.mark.parametrize("value", ["null", "", None])
def test_clear_on_an_empty_slot_is_a_silent_noop(value):
    r = _run_glue(CLEAR, slot_value=value)
    assert r["rc"] == 0
    assert r["set"] is None
    assert r["err"] == ""


def test_clear_warns_and_survives_when_the_set_does_not_verify():
    r = _run_glue(CLEAR, slot_value=HELD_SAME, set_rc=1)
    assert r["rc"] == 0
    assert "clear did not verify" in r["err"]


def test_clear_leaves_the_slot_when_the_helper_is_missing():
    r = _run_glue(CLEAR, slot_value=HELD_SAME, with_helper=False)
    assert r["rc"] == 0
    assert r["set"] is None, "an unreadable verdict must leave the obligation standing"


# ===========================================================================
# 3. SITING -- source pins on iteration-close.sh
# ===========================================================================


def _lines() -> list:
    return ITERATION_CLOSE_SH.read_text(encoding="utf-8").splitlines()


def _find(lines, pred, start=0, what=""):
    for i in range(start, len(lines)):
        if pred(lines[i]):
            return i
    raise AssertionError(f"anchor not found: {what}")


def _call_line(lines, fn, start=0):
    return _find(lines, lambda ln: ln.strip() == fn, start, f"a call to {fn}")


def test_write_is_sited_in_do_verify_between_the_status_write_and_the_later_bookkeeping():
    lines = _lines()
    dv = _find(lines, lambda ln: ln.startswith("do_verify() {"), what="do_verify")
    dsu = _find(lines, lambda ln: ln.startswith("do_state_update() {"), what="do_state_update")
    status = _find(lines, lambda ln: ln.strip() == '"${update_cmd[@]}"', dv, 'the "${update_cmd[@]}" status write')
    call = _call_line(lines, WRITE, dv)
    stamp = _find(lines, lambda ln: '"$GOAL_ID" outcome_class "$OUTCOME"' in ln, status, "the outcome_class stamp")
    assert dv < status < call < stamp < dsu, (
        "the sentinel must follow the status write directly and precede the "
        "bookkeeping that can abort the phase (g-115-7663) -- an abort past it "
        "must not cost the sentinel")


def test_write_gate_is_reducer_deep_nonrecurring_nondrain():
    lines = _lines()
    gate = lines[_call_line(lines, WRITE) - 1]
    for needle in ('"$GOAL_STATUS" == "completed"', '"$OUTCOME" == "deep"',
                   '-z "$DRAIN"', '"${BODY_ROLE:-}" != "worker"'):
        assert needle in gate, f"write gate lost {needle!r}: {gate.strip()}"


@pytest.mark.parametrize("status,outcome,drain,role,armed", [
    ("completed", "deep", "", "", True),            # the reducer's one-shot deep close
    ("completed", "deep", "", "reducer", True),
    ("completed", "routine", "", "", False),        # routine closes owe no spark or state-update encoding
    ("completed", "deep", "true", "", False),       # a drained goal is another session's unit
    ("completed", "deep", "", "worker", False),     # a worker Body runs no state-update
    ("blocked", "deep", "", "", False),             # only a completed goal owes a close
    ("skipped", "deep", "", "", False),
])
def test_write_gate_decisions(status, outcome, drain, role, armed):
    """The REAL gate, extracted and run: which closes arm the sentinel. A routine
    verify must not (outcome 1 of g-115-11905); the recurring case is the branch
    pin below, since a recurring goal never reaches this block."""
    lines = _lines()
    call = _call_line(lines, WRITE)
    block = "\n".join(lines[call - 1:call + 2])      # `if [[ ... ]]; then` / the call / `fi`
    assert block.lstrip().startswith("if [[") and block.rstrip().endswith("fi"), block
    prog = f"set -euo pipefail\n{WRITE}() {{ echo ARMED; }}\n{block}\n"
    env = dict(os.environ)
    env.update({"GOAL_STATUS": status, "OUTCOME": outcome, "DRAIN": drain})
    env.pop("BODY_ROLE", None)
    if role:
        env["BODY_ROLE"] = role
    r = subprocess.run([BASH, "-c", prog], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert (r.stdout.strip() == "ARMED") is armed, \
        f"status={status} outcome={outcome} drain={drain!r} role={role!r}: got {r.stdout.strip()!r}"


def test_write_sits_inside_the_non_recurring_branch_of_the_status_write():
    """A recurring goal's close is recurring-close.sh's, which writes the spark
    sentinel itself; this slot must come only from the one-shot completion path,
    which is the `else` of do_verify's IS_RECURRING branch."""
    lines = _lines()
    dv = _find(lines, lambda ln: ln.startswith("do_verify() {"), what="do_verify")
    rec_if = _find(lines, lambda ln: ln.strip() == 'if [[ "$IS_RECURRING" == "true" ]]; then', dv,
                   "the IS_RECURRING branch")
    indent = len(lines[rec_if]) - len(lines[rec_if].lstrip())

    def at_branch_level(ln, word):
        return ln.strip() == word and len(ln) - len(ln.lstrip()) == indent

    els = _find(lines, lambda ln: at_branch_level(ln, "else"), rec_if, "the else of the IS_RECURRING branch")
    fi = _find(lines, lambda ln: at_branch_level(ln, "fi"), els, "the fi closing the IS_RECURRING branch")
    status = _find(lines, lambda ln: ln.strip() == '"${update_cmd[@]}"', els, "the status write")
    call = _call_line(lines, WRITE, dv)
    assert rec_if < els < status < call < fi, \
        "the write must sit in the one-shot (non-recurring) branch, after its status write"


def test_clear_is_sited_in_do_state_update_after_the_checkpoint_phase_stamp():
    lines = _lines()
    dsu = _find(lines, lambda ln: ln.startswith("do_state_update() {"), what="do_state_update")
    assigned = _find(lines, lambda ln: ln.strip() == '_su_is_recurring="$(_probe_is_recurring)"', dsu,
                     "the recurrence probe")
    refresh = _find(lines, lambda ln: ln.strip() == '_checkpoint_refresh state_update "$GOAL_ID"', dsu,
                    "the state_update checkpoint stamp")
    call = _call_line(lines, CLEAR, dsu)
    assert dsu < assigned < call and refresh < call, (
        "the clear must follow the checkpoint's own phase_completed stamp so the two "
        "durable records cannot disagree, and must read a flag that is already set")
    gate = lines[call - 1]
    assert '"$OUTCOME" == "deep"' in gate and '"$_su_is_recurring" != "true"' in gate, gate.strip()


def test_each_function_has_exactly_one_call_site():
    src = ITERATION_CLOSE_SH.read_text(encoding="utf-8")
    for fn in (WRITE, CLEAR):
        assert len(re.findall(rf"^\s*{fn}\s*$", src, re.M)) == 1, fn


def test_slot_is_only_ever_written_through_the_verified_wrapper():
    """Loss-bearing (guard-2104): write, read back, retry once -- never a bare set."""
    src = ITERATION_CLOSE_SH.read_text(encoding="utf-8")
    verified = 'verified-wm-set.sh" pending_close_owed'
    assert src.count(verified) == 2, "expected exactly the write and the clear"
    assert 'wm-set.sh" pending_close_owed' not in src.replace(verified, ""), \
        "a bare wm-set.sh targets the owed-close slot"


@pytest.mark.parametrize("rel", [
    ".claude/skills/aspirations/SKILL.md",
    ".claude/skills/aspirations-spark/SKILL.md",
    "core/scripts/spark-fire-dedup.py",
    "core/scripts/recurring-close.sh",
])
def test_spark_side_code_never_touches_the_slot(rel):
    """A spark firing alone must never clear the owed close: the spark is one of
    its three steps, and clearing on it would drop state-update and the learning
    gate. If a future change makes this file legitimately mention the slot, this
    fails and forces that decision to be made consciously."""
    p = PROJECT_ROOT / rel
    assert p.is_file(), f"{rel} moved -- update this pin"
    assert SLOT not in p.read_text(encoding="utf-8")


# ===========================================================================
# 4. BATTERY -- a simulated compaction lists the owed close
# ===========================================================================


@pytest.fixture()
def battery_env(tmp_path, monkeypatch):
    """An INITIALIZED world and a fake agent state dir, wired through the battery's
    two seams, so only the WM slot under test can make an entry actionable."""
    markers = {}
    for tier in ("world", "agent", "meta"):
        d = tmp_path / "markers" / tier
        d.mkdir(parents=True)
        (d / ".initialized").touch()
        markers[tier] = d / ".initialized"
    monkeypatch.setattr(oeb, "_init_marker_paths", lambda agent: dict(markers))
    state = tmp_path / "agents" / "testagent" / "session"
    state.mkdir(parents=True)
    import _paths
    monkeypatch.setattr(_paths, "agent_state_dir", lambda name: state)
    return state


def _wm_file(tmp_path, slots: dict) -> str:
    p = tmp_path / "wm.yaml"
    p.write_text(yaml.safe_dump({"slots": slots}), encoding="utf-8")
    return str(p)


def _battery(capsys, wm_path, as_json=True):
    rc = oeb.run("testagent", wm_path, as_json)
    out = capsys.readouterr().out
    assert rc == 0
    return json.loads(out.splitlines()[0]) if as_json else out


def _owed_payload(goal="g-OWED-01", summary="a deep close"):
    return cos.build_payload(goal, "world", "deep", "2026-10-15T00:30:00", summary,
                             now="2026-10-15T00:31:00")


def _dispatch() -> str:
    return next(s for s in oeb.ENTRY_CHECKS if s["name"] == SLOT)["skill_section"]


def test_entry_is_registered_once_ahead_of_the_spark_entry():
    names = [s["name"] for s in oeb.ENTRY_CHECKS]
    assert names.count(SLOT) == 1
    assert names.index(SLOT) < names.index("pending_phase_6_spark"), \
        "the owed close is the wider obligation and the spark is one of its steps"
    spec = oeb.ENTRY_CHECKS[names.index(SLOT)]
    assert (spec["kind"], spec["slot"], spec["phase"]) == ("wm_slot", SLOT, "-0.5c.3")


def test_a_compacted_session_lists_the_owed_close(battery_env, tmp_path, capsys):
    """The writer's payload (built by the real helper) is what the battery reads
    back after a compaction -- the contract between the two ends."""
    rep = _battery(capsys, _wm_file(tmp_path, {SLOT: _owed_payload()}))
    assert [e["name"] for e in rep["actionable"]] == [SLOT]
    entry = rep["actionable"][0]
    assert entry["phase"] == "-0.5c.3"
    assert entry["payload"]["goal_id"] == "g-OWED-01"
    assert entry["payload"]["owed"] == OWED
    assert [f["name"] for f in rep["findings"]] == [SLOT]


@pytest.mark.parametrize("value", [None, "null"])
def test_a_cleared_slot_is_clean(battery_env, tmp_path, capsys, value):
    rep = _battery(capsys, _wm_file(tmp_path, {SLOT: value}))
    assert rep["actionable"] == []


def test_a_spark_that_already_fired_does_not_clear_the_owed_close(battery_env, tmp_path, capsys):
    """Spark done, its own sentinel consumed -- the owed close still stands, because
    state-update and the learning gate have not run. Whatever shape the dedup record
    has, the battery must not consult it to drop this entry."""
    slots = {SLOT: _owed_payload(), "pending_phase_6_spark": None,
             "spark_fired_session": ["g-OWED-01"]}
    rep = _battery(capsys, _wm_file(tmp_path, slots))
    assert [e["name"] for e in rep["actionable"]] == [SLOT]


def test_both_sentinels_set_lists_the_owed_close_first(battery_env, tmp_path, capsys):
    slots = {"pending_phase_6_spark": {"goal_id": "g-OWED-01", "outcome": "deep"},
             SLOT: _owed_payload()}
    rep = _battery(capsys, _wm_file(tmp_path, slots))
    assert [e["name"] for e in rep["actionable"]] == [SLOT, "pending_phase_6_spark"]


def test_dispatch_text_carries_the_protocol():
    """No SKILL.md phase body exists for this entry (the hot-path ratchet keeps the
    always-loaded loop skill from growing), so this text IS the protocol."""
    text = _dispatch()
    for needle in (
        "BEFORE selecting a new goal",
        "never execute that goal again",
        "proc-match.sh iteration-close",
        "proc-match.sh recurring-close",
        "rc 0 = a close is still running",
        "spark_fired_session",
        "iteration-close.sh --phase state-update --goal <goal_id> --source <source> --outcome deep",
        "it clears this slot itself, and only when it lands",
        "not idempotent",
        "execution-diary.sh read --goal <goal_id>",
        "phase-8-state-update",
        "echo null | bash core/scripts/verified-wm-set.sh pending_close_owed",
    ):
        assert needle in text, f"dispatch text lost {needle!r}"
    assert text.replace("verified-wm-set.sh", "").count("wm-set.sh") == 0, \
        "the hand-clear must go through verified-wm-set.sh, never a bare wm-set.sh"


def test_human_mode_line_keeps_the_machine_keys_under_the_400_char_cap(battery_env, tmp_path, capsys):
    """Phase -0.5a0's contract is that the payload on the line IS the read. A full
    240-char summary must not push goal_id/started_at/set_at past the cap."""
    payload = _owed_payload(goal="g-115-11905", summary="y" * 400)
    out = _battery(capsys, _wm_file(tmp_path, {SLOT: payload}), as_json=False)
    line = next(ln for ln in out.splitlines() if ln.startswith("▸ ENTRY: pending_close_owed"))
    assert '"goal_id": "g-115-11905"' in line
    assert '"started_at": "2026-10-15T00:30:00"' in line
    assert '"set_at": "2026-10-15T00:31:00"' in line
    assert "…" in line, "the cap should still fire -- it may eat only the prose tail"


# ===========================================================================
# 5. SURVIVAL -- the slot outlives WM reset and WM prune
# ===========================================================================

CONTROL = "owed_close_control_probe"   # dict-valued like the sentinel, registered nowhere


def _daemon_constant(name: str) -> set:
    """AST-read a constant from the daemon endpoint without importing it."""
    src = (PROJECT_ROOT / "mind_api" / "src" / "endpoints" / "wm_write.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == name):
            return set(ast.literal_eval(node.value))
    raise AssertionError(f"{name} not found in the daemon mirror")


def _aged_iso(minutes: int) -> str:
    return (datetime.now() - timedelta(minutes=minutes)).isoformat()


def _seed_wm(minutes_old: int) -> None:
    """Fresh WM holding the sentinel AND a same-shaped unregistered control."""
    wm.cmd_init(SimpleNamespace())
    data = wm.read_wm()
    aged = _aged_iso(minutes_old)
    for name in (SLOT, CONTROL):
        data["slots"][name] = _owed_payload()
        data.setdefault("slot_meta", {})[name] = {
            "updated_at": aged, "accessed_at": aged, "update_count": 1}
    wm.write_wm(data)


def test_slot_is_reset_surviving_on_both_the_cli_and_the_live_daemon_copy():
    """wm-reset.sh is daemon-only, so the daemon copy is the LIVE path: a CLI-only
    edit would leave consolidation wiping the sentinel in production (guard-2323)."""
    assert SLOT in wm.RESET_SURVIVING_SLOTS
    assert SLOT in _daemon_constant("RESET_SURVIVING_SLOTS")


def test_slot_is_in_the_authoritative_protected_list():
    """Both prune paths (wm.py _do_prune and the daemon's prune()) read this one
    list; the code defaults are only fallbacks."""
    cfg = yaml.safe_load((PROJECT_ROOT / "core" / "config" / "memory-pipeline.yaml").read_text(encoding="utf-8"))
    assert SLOT in cfg["working_memory_pruning"]["protected_slots"]


def test_cli_reset_preserves_the_owed_close(tmp_path, monkeypatch):
    monkeypatch.setenv("BODY_WM_PATH", str(tmp_path / "working-memory.yaml"))
    _seed_wm(minutes_old=5)
    wm.cmd_reset(SimpleNamespace())
    after = wm.read_wm()["slots"]
    assert after.get(CONTROL) is None, \
        "negative control survived the reset: it nulled nothing, so this run proves nothing"
    assert after.get(SLOT) == _owed_payload(), "consolidation's wm-reset destroyed an owed close"


def test_cli_prune_does_not_evict_an_idle_owed_close(tmp_path, monkeypatch):
    """Idle past evict_threshold_minutes (120): an owed close has no producer that
    re-arms it, so eviction would drop it while the close is still owed."""
    monkeypatch.setenv("BODY_WM_PATH", str(tmp_path / "working-memory.yaml"))
    _seed_wm(minutes_old=200)
    wm._do_prune(SimpleNamespace(dry_run=False))
    after = wm.read_wm()["slots"]
    assert after.get(CONTROL) is None, \
        "negative control survived prune: nothing was evicted, so this run proves nothing"
    assert after.get(SLOT) == _owed_payload(), "wm-prune evicted an owed close"


# ===========================================================================
# 6. MERGE -- generalize-down must not resurrect a cleared owed close
# ===========================================================================

_BMG: list = []


def _bmg():
    """body-merge.py, imported once and lazily (its import also loads body-manifest)."""
    if not _BMG:
        _BMG.append(_load("body_merge_owed_close_under_test", CORE_SCRIPTS / "body-merge.py"))
    return _BMG[0]


def _wm_with(slots: dict) -> dict:
    return {"slots": slots, "slot_meta": {}}


def test_merge_policy_names_the_slot():
    assert SLOT in _bmg().REDUCER_AUTHORITATIVE_SLOTS


def test_merge_does_not_resurrect_a_cleared_owed_close():
    """A Body that forked while the slot was set still holds the payload. Once the
    reducer clears it at state-update, merge_wm's absence rule (reducer None -> take
    the Body's value) would hand the stale copy back, and the slot is reset- and
    prune-protected, so it would never go away. The control is the same payload in a
    same-shaped unregistered slot: it IS re-delivered, which shows the absence rule
    is the mechanism under test and not an artifact of the fixture."""
    stale = _owed_payload()
    merged = _bmg().merge_wm(
        _wm_with({SLOT: None, CONTROL: None}),
        _wm_with({SLOT: stale, CONTROL: stale}),
        _wm_with({SLOT: stale, CONTROL: stale}),
    )["slots"]
    assert merged[CONTROL] == stale, \
        "control: the absence rule no longer re-delivers, so this test proves nothing"
    assert merged[SLOT] is None, "a cleared owed close was resurrected from a Body's stale fork"


def test_merge_does_not_add_an_owed_close_the_reducer_never_held():
    """The absent-key branch of the slot loop assigns the Body's value wholesale."""
    stale = _owed_payload()
    merged = _bmg().merge_wm(
        _wm_with({}),
        _wm_with({SLOT: stale, CONTROL: stale}),
        _wm_with({SLOT: stale, CONTROL: stale}),
    )["slots"]
    assert merged.get(CONTROL) == stale, \
        "control: the absent-key branch no longer adds a Body-only slot, so this test proves nothing"
    assert merged.get(SLOT) is None and SLOT not in merged, \
        "a Body-only owed close was added to the reducer's WM"
