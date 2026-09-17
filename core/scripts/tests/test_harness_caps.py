"""_harness_caps -- harness detection and the no-notify yield helpers ().

The all-blocked B7.2 yield, idle-tick.sh and both cycle-cache directive
printers now branch on ONE question -- can the hosting harness notify the loop
when a background job exits? -- answered here from env markers alone. These
tests pin the detection precedence (mirrors _runtime.sh::rt_judge_provenance),
the fail-safe default for an unknown harness, the override, the wake sizing
against ScheduleWakeup's [60, 3600] clamp, and the shared directive text.
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from _bash_helpers import BASH  # guard-580: never a bare "bash" argv[0]

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent.parent

_HARNESS_MARKERS = ("CLAUDECODE", "ZAKCODE_MODEL", "ZAKCODE_SESSION", "MIND_HARNESS_BG_NOTIFY")


def _mod():
    spec = importlib.util.spec_from_file_location("_harness_caps_t", SCRIPTS / "_harness_caps.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in _HARNESS_MARKERS}
    env.update(extra)
    return env


def test_detection_precedence_matches_runtime_sh():
    hc = _mod()
    assert hc.detect_harness({}) == "unknown"
    assert hc.detect_harness({"CLAUDECODE": "1"}) == "claude-code"
    assert hc.detect_harness({"ZAKCODE_MODEL": "x"}) == "zakcode"
    assert hc.detect_harness({"ZAKCODE_SESSION": "s"}) == "zakcode"
    # _runtime.sh checks CLAUDECODE first; both set -> claude-code.
    assert hc.detect_harness({"CLAUDECODE": "1", "ZAKCODE_MODEL": "x"}) == "claude-code"


def test_capability_table_and_fail_safe_unknown():
    hc = _mod()
    assert hc.background_job_notify({"CLAUDECODE": "1"}) is True
    assert hc.background_job_notify({"ZAKCODE_MODEL": "x"}) is False
    # Unknown harness -> False: a spare wake-up is harmless, a missing one is a dead loop.
    assert hc.background_job_notify({}) is False
    assert hc.capabilities({})["harness"] == "unknown"


def test_override_env_wins_and_garbage_is_ignored():
    hc = _mod()
    assert hc.background_job_notify({"ZAKCODE_MODEL": "x", "MIND_HARNESS_BG_NOTIFY": "1"}) is True
    assert hc.background_job_notify({"CLAUDECODE": "1", "MIND_HARNESS_BG_NOTIFY": "false"}) is False
    assert hc.background_job_notify({"CLAUDECODE": "1", "MIND_HARNESS_BG_NOTIFY": "maybe"}) is True


def test_wake_delay_is_sleep_plus_margin_clamped():
    hc = _mod()
    assert hc.wake_delay_seconds(1800) == 1860
    assert hc.wake_delay_seconds(30) == 90
    assert hc.wake_delay_seconds(7200) == 3600
    assert hc.wake_delay_seconds(3600) == 3600
    assert hc.wake_delay_seconds("garbage") == 60


def test_no_notify_hint_is_empty_on_notifying_harness_and_sized_otherwise():
    hc = _mod()
    assert hc.no_notify_hint(1800, {"CLAUDECODE": "1"}) == ""
    hint = hc.no_notify_hint(1800, {"ZAKCODE_MODEL": "x"})
    assert "delaySeconds=1860" in hint
    assert "<<autonomous-loop-dynamic>>" in hint
    # The re-entry the hint forbids is named in the VESSEL's vocabulary (2026-09-17).
    assert "END THE TURN" in hint and "no use_skill(aspirations)" in hint
    assert "Skill(aspirations)" not in hint.replace("use_skill(aspirations)", "")
    # Positive control: a no-notify CLAUDE CODE (override) keeps Claude Code's spelling.
    cc_silent = hc.no_notify_hint(1800, {"CLAUDECODE": "1", "MIND_HARNESS_BG_NOTIFY": "0"})
    assert "no Skill(aspirations)" in cc_silent and "ScheduleWakeup(" in cc_silent
    assert hint.endswith("\n")
    assert "delaySeconds=3600" in hc.no_notify_hint(7200, {})


def test_cli_get_json_and_hint():
    py = SCRIPTS / "_harness_caps.py"
    r = subprocess.run([sys.executable, str(py), "--get", "background_job_notify"],
                       env=_clean_env(ZAKCODE_MODEL="x"), capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "false", r
    r = subprocess.run([sys.executable, str(py), "--get", "background_job_notify"],
                       env=_clean_env(CLAUDECODE="1"), capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "true", r
    r = subprocess.run([sys.executable, str(py), "--json"],
                       env=_clean_env(), capture_output=True, text=True)
    assert '"harness": "unknown"' in r.stdout and '"background_job_notify": false' in r.stdout
    r = subprocess.run([sys.executable, str(py), "--hint", "1800"],
                       env=_clean_env(CLAUDECODE="1"), capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout == ""
    r = subprocess.run([sys.executable, str(py), "--hint", "1800"],
                       env=_clean_env(), capture_output=True, text=True)
    assert "delaySeconds=1860" in r.stdout
    r = subprocess.run([sys.executable, str(py), "--get", "nope"],
                       env=_clean_env(), capture_output=True, text=True)
    assert r.returncode == 2 and "unknown capability" in r.stderr


def test_wrapper_script_is_the_python_invocation_safe_path():
    text = (SCRIPTS / "harness-capabilities.sh").read_text(encoding="utf-8")
    assert "_paths.sh" in text and "_harness_caps.py" in text
    r = subprocess.run([BASH, (SCRIPTS / "harness-capabilities.sh").as_posix(), "--get", "background_job_notify"],
                       env=_clean_env(ZAKCODE_SESSION="s", MIND_AGENT=os.environ.get("MIND_AGENT", "alpha")),
                       capture_output=True, text=True, cwd=str(REPO))
    assert r.returncode == 0 and r.stdout.strip() == "false", r


# ── max_foreground_sleep_seconds / foreground chunking ( half c) ──────


def test_foreground_ceiling_is_in_the_table_per_harness():
    hc = _mod()
    assert hc.capabilities(_clean_env(CLAUDECODE="1"))["max_foreground_sleep_seconds"] is None
    assert hc.capabilities(_clean_env(ZAKCODE_MODEL="x"))["max_foreground_sleep_seconds"] == 600
    # Unknown is conservative in the SAFE direction, same as its notify row:
    # chunking a harness that did not need it costs iteration boundaries;
    # not chunking one that did means the tool kills the sleep mid-call.
    assert hc.capabilities(_clean_env())["max_foreground_sleep_seconds"] == 600


def test_chunk_plan_is_none_when_uncapped_or_within_ceiling_and_ceil_divides_otherwise():
    hc = _mod()
    zak, cc = _clean_env(ZAKCODE_MODEL="x"), _clean_env(CLAUDECODE="1")
    assert hc.foreground_chunk_plan(7200, cc) is None      # uncapped harness
    assert hc.foreground_chunk_plan(600, zak) is None      # exactly at ceiling
    assert hc.foreground_chunk_plan(1200, zak) == (600, 2)
    assert hc.foreground_chunk_plan(700, zak) == (600, 2)  # ceil-div: last chunk short
    # Never a 1-chunk plan, so a caller may treat None and "one chunk" alike.
    for s in (0, 1, 599, 600):
        assert hc.foreground_chunk_plan(s, zak) is None
    assert hc.foreground_chunk_plan("garbage", zak) is None


def test_chunk_hint_keys_on_the_CEILING_not_on_background_notify():
    """The load-bearing case, and the whole reason foreground_chunk_hint is a
    separate predicate. zakcode is BOTH no-notify AND capped, so keying the
    chunk directive on notify would pass every test written against the
    harnesses measured today and silently leave a future notifying-but-capped
    harness unchunked -- invisible, because a truncated sleep reads as an early
    wake. The override gives us exactly that harness: notify True, ceiling 600.
    """
    hc = _mod()
    notifying_but_capped = _clean_env(ZAKCODE_MODEL="x", MIND_HARNESS_BG_NOTIFY="1")
    caps = hc.capabilities(notifying_but_capped)
    assert caps["background_job_notify"] is True
    assert caps["max_foreground_sleep_seconds"] == 600
    assert "CAPS A FOREGROUND SLEEP" in hc.foreground_chunk_hint(1200, notifying_but_capped)
    # and no_notify_hint still carries it, so the four existing printers reach it
    # with no caller change -- while the notify half is correctly absent.
    text = hc.no_notify_hint(1200, notifying_but_capped)
    assert "CAPS A FOREGROUND SLEEP" in text
    assert "CANNOT NOTIFY" not in text


def test_no_notify_hint_carries_both_halves_on_a_capped_no_notify_harness():
    hc = _mod()
    zak = _clean_env(ZAKCODE_MODEL="x")
    text = hc.no_notify_hint(1200, zak)
    assert "CAPS A FOREGROUND SLEEP AT 600s" in text
    assert "CANNOT NOTIFY ON BACKGROUND-JOB EXIT" in text
    # A short sleep on the same harness gets the notify half only.
    short = hc.no_notify_hint(120, zak)
    assert "CAPS A FOREGROUND SLEEP" not in short
    assert "CANNOT NOTIFY ON BACKGROUND-JOB EXIT" in short
    # An uncapped notifying harness stays exactly as before: empty.
    assert hc.no_notify_hint(7200, _clean_env(CLAUDECODE="1")) == ""


def test_cli_get_renders_none_lowercase_for_the_uncapped_harness():
    hc = _mod()
    assert hc._fmt(None) == "none"
    assert hc._fmt(True) == "true" and hc._fmt(False) == "false"
    assert hc._fmt(600) == "600"


# --- sleep_directive: the yield block's ONE owner ( leg c) -----------
#
# The four printers (idle-tick.sh, both cycle caches, dry-spin-guard) each
# hardcoded a Claude-Code yield block and then appended no_notify_hint(), which
# on a capped harness told the model the OPPOSITE on all three axes. Every
# capped harness in KNOWN is also no-notify, so that was the whole capped
# population, not an edge case. These pin the resolution.

_UNCAPPED_EXPECTED = (
    "Emit exactly ONE tool call:\n"
    '  Bash("MIND_AGENT=alpha QUIESCENCE_SLEEP=1 bash core/scripts/interruptible-sleep.sh '
    '1800", run_in_background=true)\n'
    "When the harness notifies you of its exit, call Skill('aspirations') with args='loop'.\n"
)


def test_uncapped_notifying_branch_is_byte_identical_to_the_old_hardcoded_block():
    """REGRESSION PIN: claude-code must see exactly what the four sites printed
    before the extraction -- the three literal lines, then no_notify_hint (which
    is "" there). If this breaks, the refactor changed live Claude Code behaviour."""
    hc = _mod()
    cc = _clean_env(CLAUDECODE="1")
    got = hc.sleep_directive(1800, "alpha", "QUIESCENCE_SLEEP=1", cc)
    assert got == _UNCAPPED_EXPECTED + hc.no_notify_hint(1800, cc)
    assert got == _UNCAPPED_EXPECTED  # the hint is empty on a notifying harness


def test_capped_branch_never_emits_the_guard_3892_pair():
    """A capped directive must not carry run_in_background=true, because the
    no-notify half it used to sit beside says to use a trailing & -- and obeying
    both double-detaches and forfeits the notification (guard-3892)."""
    hc = _mod()
    zak = _clean_env(ZAKCODE_MODEL="x")
    text = hc.sleep_directive(1800, "alpha", "QUIESCENCE_SLEEP=1", zak)
    assert "run_in_background=true" not in text
    assert "Skill('aspirations')" not in text          # the wake is the re-entry here
    assert "FOREGROUND" in text


def test_capped_branch_asks_for_one_chunk_not_the_total():
    hc = _mod()
    zak = _clean_env(ZAKCODE_MODEL="x")
    text = hc.sleep_directive(1800, "alpha", "DRY_SLEEP=1", zak)
    assert "interruptible-sleep.sh 600\"" in text       # the chunk
    assert "interruptible-sleep.sh 1800" not in text    # never the whole thing
    assert "3 chunks" in text


def test_capped_branch_keeps_the_env_prefix_on_the_chunk_guard_1230():
    """interruptible-sleep.sh registers as a Tier-A background job ONLY with
    QUIESCENCE_SLEEP=1 or DRY_SLEEP=1 (guard-1230); without it stop-hook Gate 2.6
    BLOCKs the very turn-end the sleep exists to allow."""
    hc = _mod()
    zak = _clean_env(ZAKCODE_MODEL="x")
    for prefix in ("QUIESCENCE_SLEEP=1", "DRY_SLEEP=1"):
        text = hc.sleep_directive(1800, "alpha", prefix, zak)
        # every interruptible-sleep invocation in the text carries the prefix
        assert text.count("interruptible-sleep.sh") == text.count(
            f"{prefix} bash core/scripts/interruptible-sleep.sh")


def test_capped_wake_arm_is_sized_to_one_chunk_not_the_remaining_total():
    """Arming for the TOTAL would add to the chunks already slept and overshoot:
    600 slept + 1860 armed = 2460 for a 1800s sleep. The printers recompute the
    remainder on re-entry, so one chunk plus the margin is the correct arm."""
    hc = _mod()
    zak = _clean_env(ZAKCODE_MODEL="x")
    for total in (601, 1800, 7200):
        text = hc.sleep_directive(total, "alpha", "DRY_SLEEP=1", zak)
        assert f"delaySeconds={hc.wake_delay_seconds(600)}" in text
        assert f"delaySeconds={hc.wake_delay_seconds(total)}" not in text or total == 600


def test_positive_control_capped_and_uncapped_differ():
    """Guards every absence assertion above: if the harness branch ever stopped
    discriminating, those asserts would pass vacuously on one shared text."""
    hc = _mod()
    a = hc.sleep_directive(1800, "alpha", "DRY_SLEEP=1", _clean_env(CLAUDECODE="1"))
    b = hc.sleep_directive(1800, "alpha", "DRY_SLEEP=1", _clean_env(ZAKCODE_MODEL="x"))
    assert a != b
    assert "run_in_background=true" in a and "run_in_background=true" not in b


def test_no_notify_hint_output_is_unchanged_by_the_wake_phrase_extraction():
    """_wake_arm_phrase was extracted OUT of no_notify_hint so sleep_directive
    could reuse it. no_notify_hint's text must not have moved a byte -- B7.2 and
    any downstream deployment still reading --hint depend on it."""
    hc = _mod()
    zak = _clean_env(ZAKCODE_MODEL="x")
    text = hc.no_notify_hint(1200, zak)
    # Since 2026-09-17 the arm is spelled in the vessel's vocabulary (schedule_wakeup
    # on zakcode, ScheduleWakeup on Claude Code); the SHAPE around it is unchanged.
    assert "then arm schedule_wakeup(prompt=\"<<autonomous-loop-dynamic>>\"" in text
    cc_silent = hc.no_notify_hint(1200, _clean_env(CLAUDECODE="1", MIND_HARNESS_BG_NOTIFY="0"))
    assert "then arm ScheduleWakeup(prompt=\"<<autonomous-loop-dynamic>>\"" in cc_silent
    assert text.endswith("(g-357-89, rb-9668).\n")
    assert "launch the sleep ONCE with a trailing &" in text


def test_cli_sleep_directive_flag_matches_the_function_and_guards_arity():
    hc = _mod()
    script = SCRIPTS / "harness-capabilities.sh"
    env = _clean_env(CLAUDECODE="1")
    out = subprocess.run([BASH, str(script), "--sleep-directive", "1800", "alpha",
                          "QUIESCENCE_SLEEP=1"], capture_output=True, text=True, env=env)
    assert out.returncode == 0
    assert out.stdout == hc.sleep_directive("1800", "alpha", "QUIESCENCE_SLEEP=1", env)
    bad = subprocess.run([BASH, str(script), "--sleep-directive", "1800"],
                         capture_output=True, text=True, env=env)
    assert bad.returncode == 2 and "usage:" in bad.stderr


def test_every_directive_printer_routes_through_the_one_owner():
    """SHAPE PIN. The defect was four independent hardcoded copies; if a site
    re-grows its own, this fails. Keyed on the call, not on output, because a
    site's own output needs cache/WM state these tests do not build."""
    py_sites = ("quiescence-cycle-cache.py", "dry-idle-cycle-cache.py", "dry-spin-guard.py")
    for name in py_sites:
        src = (SCRIPTS / name).read_text(encoding="utf-8")
        assert "_harness_caps.sleep_directive(" in src, name
        assert "run_in_background=true)" not in src, f"{name} re-grew a hardcoded yield block"
    sh = (SCRIPTS / "idle-tick.sh").read_text(encoding="utf-8")
    assert "--sleep-directive" in sh
    assert "run_in_background=true)" not in sh


# ── Tool vocabulary (2026-09-17): the re-entry as the VESSEL spells it ────────
#
# Every terminal imperative (stop-hook BLOCK reason, ITERATION COMPLETE, the
# recurring close, the state-mismatch landing, the PostToolUse reminder) named
# Claude Code's tools -- Skill(aspirations) with args='loop', ScheduleWakeup(...).
# Promoted to a zakcode vessel, a small model answered those names in prose for
# hours. One owner for the spelling; these pin it per harness, the shell reach,
# the fail-open defaults, and that no printing site re-grew a vocabulary.

_CC, _ZAK, _UNK = {"CLAUDECODE": "1"}, {"ZAKCODE_SESSION": "x"}, {}
_HC_KEYS = ("HC_HARNESS", "HC_SKILL_TOOL", "HC_WAKEUP_TOOL", "HC_LOOP_CALL", "HC_LOOP_REF",
            "HC_LOOP_REF_Q", "HC_SPARK_REF", "HC_WORKER_REF", "HC_WORKER_CALL_Q", "HC_DEADMAN_ARM")


def test_vocabulary_table_per_harness_and_unknown_never_guesses_a_vessel():
    hc = _mod()
    assert (hc.skill_tool(_CC), hc.wakeup_tool(_CC)) == ("Skill", "ScheduleWakeup")
    assert (hc.skill_tool(_ZAK), hc.wakeup_tool(_ZAK)) == ("use_skill", "schedule_wakeup")
    assert (hc.skill_tool(_UNK), hc.wakeup_tool(_UNK)) == ("Skill", "ScheduleWakeup")
    assert hc.skill_tool({"ZAKCODE_MODEL": "m"}) == "use_skill"
    assert hc.skill_tool({"CLAUDECODE": "1", "ZAKCODE_SESSION": "x"}) == "Skill"  # precedence


def test_skill_call_keeps_both_claude_code_spellings_and_uses_the_vessels_schema():
    hc = _mod()
    assert hc.skill_call("aspirations", "loop", _CC) == "Skill(aspirations) with args='loop'"
    assert hc.skill_call("aspirations", "loop", _CC, quoted=True) == "Skill('aspirations') with args='loop'"
    assert hc.skill_call("worker-loop", env=_CC, quoted=True) == "Skill('worker-loop')"
    assert hc.skill_call("aspirations-spark", env=_CC) == "Skill(aspirations-spark)" == hc.skill_ref("aspirations-spark", _CC)
    assert hc.skill_call("aspirations", "loop", _ZAK) == "use_skill(name='aspirations', args='loop')"
    assert hc.skill_call("aspirations", "loop", _ZAK, quoted=True) == "use_skill(name='aspirations', args='loop')"
    assert hc.skill_call("worker-loop", env=_ZAK, quoted=True) == "use_skill(name='worker-loop')"
    assert hc.skill_ref("aspirations", _ZAK) == "use_skill(aspirations)"


def test_wakeup_call_differs_only_in_the_tool_name():
    hc = _mod()
    assert hc.wakeup_call(env=_CC) == "ScheduleWakeup(prompt='<<autonomous-loop-dynamic>>', delaySeconds=600)"
    assert hc.wakeup_call(env=_ZAK) == "schedule_wakeup(prompt='<<autonomous-loop-dynamic>>', delaySeconds=600)"
    assert hc.wakeup_call("check the deploy", 270, _ZAK, quote='"') == 'schedule_wakeup(prompt="check the deploy", delaySeconds=270)'
    assert hc.wakeup_call(env=_UNK) == hc.wakeup_call(env=_CC)


def test_hook_wakeup_is_armed_only_for_a_harness_that_honours_it():
    hc = _mod()
    assert hc.hook_wakeup(_CC) == {} and hc.hook_wakeup(_UNK) == {}
    assert hc.hook_wakeup(_ZAK) == {"wakeup": {"prompt": "<<autonomous-loop-dynamic>>", "delay_seconds": 600}}


def _source_vocab(env, path=None):
    """Source _harness_vocab.sh (or a copy) under `env` and read the HC_* set back."""
    vocab = (path or (SCRIPTS / "_harness_vocab.sh")).as_posix()
    script = f'source "{vocab}"; for k in {" ".join(_HC_KEYS)}; do printf "%s\\n" "${{!k}}"; done'
    r = subprocess.run([BASH, "-c", script], capture_output=True, text=True, env=env, cwd=str(REPO))
    assert r.returncode == 0, r.stderr
    return dict(zip(_HC_KEYS, r.stdout.split("\n")))


@pytest.mark.parametrize("markers", [_CC, _ZAK, _UNK])
def test_vocab_sh_round_trips_the_python_table_through_bash_eval(markers):
    hc = _mod()
    env = _clean_env(**markers)
    assert _source_vocab(env) == hc.loop_vocab(env)


def test_vocab_sh_fail_open_defaults_are_the_claude_code_spelling(tmp_path):
    """A copy with no resolver beside it: the defaults must equal the module's
    claude-code output byte for byte -- the two copies cannot drift silently."""
    hc = _mod()
    copy = tmp_path / "_harness_vocab.sh"
    copy.write_bytes((SCRIPTS / "_harness_vocab.sh").read_bytes())
    got = _source_vocab(_clean_env(**_ZAK), path=copy)  # a vessel env, and STILL Claude Code names
    expected = dict(hc.loop_vocab(_clean_env(**_CC)), HC_HARNESS="unknown")
    assert got == expected


def test_vocab_sh_is_idempotent_and_the_second_source_is_free():
    script = ('source core/scripts/_harness_vocab.sh; a="$HC_LOOP_CALL"; HC_LOOP_REF=sentinel; '
              'source core/scripts/_harness_vocab.sh; printf "%s|%s" "$a" "$HC_LOOP_REF"')
    r = subprocess.run([BASH, "-c", script], capture_output=True, text=True,
                       env=_clean_env(**_ZAK), cwd=str(REPO))
    assert r.stdout == "use_skill(name='aspirations', args='loop')|sentinel", r


def test_cli_skill_call_and_wakeup_call_follow_the_harness_and_guard_arity():
    script = SCRIPTS / "harness-capabilities.sh"
    zak, cc = _clean_env(**_ZAK), _clean_env(**_CC)
    r = subprocess.run([BASH, str(script), "--skill-call", "aspirations", "loop"], capture_output=True, text=True, env=zak)
    assert r.stdout.strip() == "use_skill(name='aspirations', args='loop')", r
    r = subprocess.run([BASH, str(script), "--skill-call", "aspirations", "loop"], capture_output=True, text=True, env=cc)
    assert r.stdout.strip() == "Skill(aspirations) with args='loop'", r
    r = subprocess.run([BASH, str(script), "--wakeup-call"], capture_output=True, text=True, env=zak)
    assert r.stdout.strip() == "schedule_wakeup(prompt='<<autonomous-loop-dynamic>>', delaySeconds=600)", r
    r = subprocess.run([BASH, str(script), "--wakeup-call", "poll the run", "270"], capture_output=True, text=True, env=cc)
    assert r.stdout.strip() == "ScheduleWakeup(prompt='poll the run', delaySeconds=270)", r
    for bad in (["--skill-call"], ["--wakeup-call", "p", "not-a-number"], ["--wakeup-call", "a", "b", "c"]):
        r = subprocess.run([BASH, str(script), *bad], capture_output=True, text=True, env=cc)
        assert r.returncode == 2 and "usage:" in r.stderr, bad


def test_the_existing_directive_owners_speak_the_vessels_vocabulary():
    """_wake_arm_phrase / sleep_directive route through the same table, and the
    Claude Code output is byte-identical to before (the _UNCAPPED_EXPECTED pin
    above already holds the uncapped line; this holds the arm)."""
    hc = _mod()
    assert hc._wake_arm_phrase(1200, _clean_env(**_CC)).startswith(
        'arm ScheduleWakeup(prompt="<<autonomous-loop-dynamic>>", delaySeconds=1260) as the TERMINAL')
    zak = hc._wake_arm_phrase(1200, _clean_env(**_ZAK))
    assert zak.startswith('arm schedule_wakeup(prompt="<<autonomous-loop-dynamic>>", delaySeconds=1260) as the TERMINAL')
    assert "no use_skill(aspirations)" in zak and "Skill(aspirations)" not in zak.replace("use_skill(aspirations)", "")


def test_every_terminal_imperative_site_routes_through_the_vocabulary():
    """SHAPE PIN (same pattern as the sleep-directive owner pin above): if a
    site re-grows a Claude Code literal, this fails."""
    hook = (SCRIPTS / "stop-hook.sh").read_text(encoding="utf-8")
    assert '_hc.skill_call("aspirations", "loop", quoted=True)' in hook
    assert 'print(json.dumps({"decision": "block", "reason": reason, **_wakeup}))' in hook
    assert '"$HC_WORKER_REF" "$HC_WORKER_CALL_Q" "$HC_LOOP_REF_Q"' in hook
    assert "Your FIRST action MUST be: Skill('aspirations')" not in hook
    close = (SCRIPTS / "iteration-close.sh").read_text(encoding="utf-8")
    assert "Call $HC_LOOP_CALL as your VERY NEXT tool call" in close
    assert "(1) $HC_DEADMAN_ARM" in close and "THEN (2) $HC_LOOP_CALL" in close
    # two echo sites (worker / reducer NEXT lines); the comment above them also names it
    assert close.count("invoke $HC_SPARK_REF") == 2
    assert "ScheduleWakeup(prompt='<<autonomous-loop-dynamic>>', delaySeconds=600)" not in close
    rec = (SCRIPTS / "recurring-close.sh").read_text(encoding="utf-8")
    assert "(1) $HC_DEADMAN_ARM" in rec and "$HC_SKILL_TOOL ALONE keeps" in rec
    assert rec.count("$HC_SPARK_REF FIRST") == 2 and "$HC_LOOP_CALL" in rec
    assert "Skill(aspirations) with args='loop'" not in rec
    landing = (SCRIPTS / "state-mismatch-landing.sh").read_text(encoding="utf-8")
    assert "do NOT call $HC_LOOP_REF" in landing
    reminder = (SCRIPTS / "iteration-close-reminder.py").read_text(encoding="utf-8")
    assert '_hc.skill_call("aspirations", "loop")' in reminder
    assert "Skill(aspirations) with args='loop'" not in reminder
    assert 'ScheduleWakeup(prompt=' not in reminder
    for name in ("stop-hook.sh", "iteration-close.sh", "recurring-close.sh", "state-mismatch-landing.sh"):
        assert "_harness_vocab.sh" in (SCRIPTS / name).read_text(encoding="utf-8"), name


def test_stop_hook_fallback_tuple_equals_the_modules_claude_code_output():
    """The reducer payload's except-branch literals are the fail-open copy of the
    module's claude-code spelling; pin them together so neither drifts alone."""
    hc = _mod()
    hook = (SCRIPTS / "stop-hook.sh").read_text(encoding="utf-8")
    m = re.search(r'_loop_ref, _loop_call, _skill_tool, _wakeup = \(\n\s*"([^"]+)", "([^"]+)", "([^"]+)", \{\}\)', hook)
    assert m, "fallback tuple not found in stop-hook.sh"
    cc = _clean_env(**_CC)
    assert m.groups() == (hc.skill_ref("aspirations", cc),
                          hc.skill_call("aspirations", "loop", cc, quoted=True),
                          hc.skill_tool(cc))
    assert hc.hook_wakeup(cc) == {}


_ITER_CLOSE_IMPERATIVE_START = 'source "$SCRIPT_DIR/_harness_vocab.sh"\n    if [ -f "$AGENT_DIR/session/deadman-disabled" ]; then'
_ITER_CLOSE_PRE_CHANGE_PAIR = (
    "[iteration-close] NEXT ACTION REQUIRED (deadman-switch ON): your terminal response MUST be "
    "EXACTLY these TWO batched tool calls, in this order — (1) ScheduleWakeup(prompt="
    "'<<autonomous-loop-dynamic>>', delaySeconds=600) — the self-resurrection net; this call is "
    "MANDATORY, do NOT omit it; THEN (2) Skill(aspirations) with args='loop' — the primary "
    "re-entry and the LAST call, which continues the loop NOW. Emitting Skill(aspirations) ALONE "
    "keeps THIS iteration alive but leaves the NEXT one unprotected against a silent text-death "
    "— so arm the net EVERY iteration. Both calls, every time."
)
_ITER_CLOSE_PRE_CHANGE_BARE = (
    "[iteration-close] NEXT ACTION REQUIRED: Call Skill(aspirations) with args='loop' as your "
    "VERY NEXT tool call."
)


def _run_iteration_close_imperative(markers, deadman_disabled, tmp_path):
    """EXECUTE iteration-close.sh's ITERATION COMPLETE conditional (extracted
    verbatim, never re-typed) under a pinned harness env."""
    src = (SCRIPTS / "iteration-close.sh").read_text(encoding="utf-8")
    i = src.find(_ITER_CLOSE_IMPERATIVE_START)
    assert i >= 0, "the productivity-check imperative block moved"
    j = src.find("\n    fi\n", i)
    block = src[i:j + len("\n    fi\n")]
    agent_dir = tmp_path / f"agent-{'off' if deadman_disabled else 'on'}"
    (agent_dir / "session").mkdir(parents=True, exist_ok=True)
    if deadman_disabled:
        (agent_dir / "session" / "deadman-disabled").write_text("")
    script = f'SCRIPT_DIR={SCRIPTS.as_posix()!r}\nAGENT_DIR={agent_dir.as_posix()!r}\n' + block
    r = subprocess.run([BASH, "-c", script], capture_output=True, text=True,
                       env=_clean_env(**markers), cwd=str(REPO))
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_iteration_close_imperative_is_byte_identical_on_claude_code(tmp_path):
    """REGRESSION PIN for the live Claude Code fleet: the two ITERATION COMPLETE
    lines (deadman on / off) are exactly what iteration-close.sh printed before
    the vocabulary extraction."""
    assert _run_iteration_close_imperative(_CC, False, tmp_path) == _ITER_CLOSE_PRE_CHANGE_PAIR + "\n"
    assert _run_iteration_close_imperative(_CC, True, tmp_path) == _ITER_CLOSE_PRE_CHANGE_BARE + "\n"
    # unknown harness: the same bytes -- a vessel is never guessed.
    assert _run_iteration_close_imperative(_UNK, False, tmp_path) == _ITER_CLOSE_PRE_CHANGE_PAIR + "\n"


def test_iteration_close_imperative_names_the_vessels_tools_on_zakcode(tmp_path):
    """THE FIX, executed: on a zakcode vessel the same block names use_skill and
    schedule_wakeup -- the tools in that model's own list -- and nothing else."""
    pair = _run_iteration_close_imperative(_ZAK, False, tmp_path)
    assert "(1) schedule_wakeup(prompt='<<autonomous-loop-dynamic>>', delaySeconds=600) — the self-resurrection net" in pair
    assert "THEN (2) use_skill(name='aspirations', args='loop') — the primary re-entry" in pair
    assert "Emitting use_skill(aspirations) ALONE keeps THIS iteration alive" in pair
    assert "Skill(" not in pair.replace("use_skill(", "") and "ScheduleWakeup" not in pair
    bare = _run_iteration_close_imperative(_ZAK, True, tmp_path)
    assert bare == "[iteration-close] NEXT ACTION REQUIRED: Call use_skill(name='aspirations', args='loop') as your VERY NEXT tool call.\n"
