"""Branch proof for the owner's shutdown-report knob ().

Drives `resolve_mode()` directly — it is pure, so every branch is reachable
without a config file, a stop, or an email (guard-1165: no module-level
os.environ mutation, no sys.modules stubs).

TWO INVARIANTS, and the second is the one that actually failed.

  * RESOLUTION — every config shape maps to exactly one of auto/always/never,
    and a shape the config DOCUMENTS as meaningful (an absent key) is clean
    while a shape a person clearly intended but mistyped is DEGRADED. Those
    two must not collapse into each other: "the owner said nothing" and "the
    owner said something we did not understand" call for different stderr.

  * WIRING — Step 9.7 of aspirations-consolidate must actually INVOKE the
    reader. This is the load-bearing test. The knob's original defect was not
    a wrong value, it was that `session_end_notice.mode` existed in config and
    in prose and in NO code path at all (measured 2026-09-10: three
    references, none executable), so setting `never` changed nothing while
    looking like it had. A resolution test alone would have passed happily
    against that defect — the function would have been perfect and unreachable.
    rb-189 names the class; guard-399 prescribes the bash path.
"""

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SCRIPTS.parents[1]
sys.path.insert(0, str(SCRIPTS))

import session_end_notice as sen  # noqa: E402
from session_end_notice import (  # noqa: E402
    CONFIG_BLOCK,
    DEFAULT_MODE,
    VALID_MODES,
    main,
    resolve_mode,
)

CONSUMER_SKILL = (
    PROJECT_ROOT / ".claude" / "skills" / "aspirations-consolidate" / "SKILL.md"
)
READER_SCRIPT = "session-end-notice-mode.sh"


def cfg_with(mode):
    """An aspirations.yaml-shaped mapping carrying one mode value."""
    return {CONFIG_BLOCK: {"mode": mode}, "productivity_gate": {"min_iterations": 10}}


# --------------------------------------------------------------------------
# Invariant 1 — resolution
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", VALID_MODES)
def test_every_documented_mode_round_trips_cleanly(mode):
    assert resolve_mode(cfg_with(mode)) == (mode, None)


def test_never_is_actually_honoured():
    """The knob's whole purpose: the owner can turn the report off.

    Kept as its own named test rather than folded into the parametrize above,
    because this is the assertion the reported defect would have broken.
    """
    mode, degraded = resolve_mode(cfg_with("never"))
    assert mode == "never"
    assert degraded is None


@pytest.mark.parametrize(
    "cfg",
    [
        pytest.param({}, id="block-absent"),
        pytest.param({CONFIG_BLOCK: None}, id="block-empty"),
        pytest.param({CONFIG_BLOCK: {}}, id="mode-key-absent"),
        pytest.param({CONFIG_BLOCK: {"other": 1}}, id="mode-key-absent-siblings"),
    ],
)
def test_documented_absence_is_auto_and_is_NOT_degraded(cfg):
    """`aspirations.yaml` states "absence of this key = auto".

    Absence is a documented meaning, not a fault, so it must not emit the
    stderr warning — otherwise every default deployment nags forever and the
    warning stops being read on the day it matters.
    """
    assert resolve_mode(cfg) == (DEFAULT_MODE, None)


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param("off", id="plausible-typo"),
        pytest.param("nevr", id="misspelled-never"),
        pytest.param("", id="empty-string"),
        pytest.param(3, id="int"),
        pytest.param(True, id="bool"),
        pytest.param(None, id="explicit-null"),
        pytest.param(["never"], id="list"),
    ],
)
def test_unrecognised_value_falls_back_to_auto_and_says_so(bad):
    mode, degraded = resolve_mode(cfg_with(bad))
    assert mode == DEFAULT_MODE
    assert degraded, "an unrecognised value must explain itself on stderr"


@pytest.mark.parametrize(
    "cfg",
    [
        pytest.param({CONFIG_BLOCK: "never"}, id="block-is-scalar"),
        pytest.param({CONFIG_BLOCK: ["never"]}, id="block-is-list"),
        pytest.param(None, id="root-is-none"),
        pytest.param([], id="root-is-list"),
    ],
)
def test_malformed_structure_degrades_rather_than_raising(cfg):
    mode, degraded = resolve_mode(cfg)
    assert mode == DEFAULT_MODE
    assert degraded


@pytest.mark.parametrize("raw", [" never ", "NEVER", "Never", "\tAlways\n"])
def test_case_and_whitespace_are_forgiven(raw):
    """Hand-typed owner-facing knob; the failure direction of being strict is
    "keep emailing him anyway", which is the thing being fixed."""
    mode, degraded = resolve_mode(cfg_with(raw))
    assert mode == raw.strip().lower()
    assert degraded is None


def test_resolution_actually_discriminates():
    """Non-vacuity control (rb-245).

    Every assertion above would still pass if `resolve_mode` were replaced by
    `lambda cfg: (DEFAULT_MODE, None)` — except the ones that demand a
    non-default answer. This states that requirement directly, so the suite
    cannot silently degrade into "always returns auto" and stay green.
    """
    distinct = {resolve_mode(cfg_with(m))[0] for m in VALID_MODES}
    assert distinct == set(VALID_MODES)


def test_mode_is_always_one_of_the_documented_three():
    """Callers consume stdout without error handling, so there is no fourth
    value they could ever be handed."""
    for cfg in ({}, None, cfg_with("never"), cfg_with("garbage"), {CONFIG_BLOCK: 7}):
        assert resolve_mode(cfg)[0] in VALID_MODES


# --------------------------------------------------------------------------
# Invariant 2 — wiring (the load-bearing half)
# --------------------------------------------------------------------------


def test_reader_script_exists_and_is_executable_by_bash():
    script = SCRIPTS / READER_SCRIPT
    assert script.is_file(), f"{READER_SCRIPT} is the knob's only code path"
    body = script.read_text(encoding="utf-8")
    assert "session_end_notice.py" in body, "wrapper must reach the resolver"


def test_step_9_7_actually_invokes_the_reader():
    """THE REGRESSION GUARD.

    The defect being fixed was a knob that existed everywhere except in a
    code path. If a future edit turns this call back into a bare prose
    mention, the knob silently stops working and nothing else in this suite
    would notice — `resolve_mode` would keep passing every test above while
    no caller ever reached it.
    """
    assert CONSUMER_SKILL.is_file(), f"consumer skill missing: {CONSUMER_SKILL}"
    body = CONSUMER_SKILL.read_text(encoding="utf-8")
    assert READER_SCRIPT in body, (
        "aspirations-consolidate Step 9.7 must INVOKE "
        f"{READER_SCRIPT}; naming the config key in prose is exactly the "
        "declared-but-unenforced shape this fix removes (rb-189, guard-399)"
    )


def test_config_still_declares_the_knob_the_reader_reads():
    """Pins the two ends together (guard-359: SKILL.md/script name drift).

    If the config block is renamed without renaming CONFIG_BLOCK, the reader
    silently returns `auto` forever — a green suite over a dead knob.
    """
    config = PROJECT_ROOT / "core" / "config" / "aspirations.yaml"
    assert config.is_file()
    assert f"{CONFIG_BLOCK}:" in config.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Invariant 3 — the CLI contract
#
# Added a turn after the rest ( follow-up). The original suite drove
# only the pure resolver, while the commit message asserted a CLI property —
# "stdout always carries a usable mode; exit 3 distinguishes 'the owner chose
# auto' from 'we could not tell what the owner chose'". That claim was true by
# construction and UNTESTED, which makes it exactly the unverified positive
# claim `verify-before-assuming.md` warns about. A hand-run of the clean path
# is not coverage of the degraded one.
#
# `load_config` is monkeypatched rather than driven through a temp config file:
# the contract under test is main()'s stdout/exit mapping, and reaching it
# through a real YAML file would also be testing the loader, so a failure could
# not be localised.
# --------------------------------------------------------------------------


def test_cli_prints_the_mode_and_exits_0_on_a_clean_read(monkeypatch, capsys):
    monkeypatch.setattr(sen, "load_config", lambda: (cfg_with("never"), None))
    rc = main()
    captured = capsys.readouterr()
    assert captured.out.strip() == "never"
    assert rc == 0
    assert captured.err == "", "a clean read must not nag on stderr"


def test_cli_exits_3_yet_still_prints_a_usable_mode_on_a_bad_value(monkeypatch, capsys):
    monkeypatch.setattr(sen, "load_config", lambda: (cfg_with("nevr"), None))
    rc = main()
    captured = capsys.readouterr()
    assert captured.out.strip() == DEFAULT_MODE, "stdout must stay consumable"
    assert rc == 3
    assert "nevr" in captured.err, "stderr must name what it could not parse"


def test_cli_exits_3_when_the_config_cannot_be_read_at_all(monkeypatch, capsys):
    monkeypatch.setattr(sen, "load_config", lambda: (None, "OSError: nope"))
    rc = main()
    captured = capsys.readouterr()
    assert captured.out.strip() == DEFAULT_MODE
    assert rc == 3
    assert "OSError" in captured.err


def test_cli_absent_key_is_clean_not_degraded(monkeypatch, capsys):
    """The default deployment must not emit a warning on every stop.

    A knob nobody has set is the COMMON case; if it warned, the stderr line
    would be background noise by the time a real misconfiguration produced one.
    """
    monkeypatch.setattr(sen, "load_config", lambda: ({}, None))
    rc = main()
    captured = capsys.readouterr()
    assert captured.out.strip() == DEFAULT_MODE
    assert rc == 0
    assert captured.err == ""


def test_cli_exit_codes_actually_discriminate(monkeypatch, capsys):
    """Non-vacuity control for the exit-code claim itself (rb-245).

    Every assertion above still passes if main() always returned 3, or always
    returned 0, as long as each test were written to match. This one fails
    unless the two paths genuinely differ, which is the whole content of
    "exit 3 distinguishes" — the sentence this section exists to verify.
    """
    monkeypatch.setattr(sen, "load_config", lambda: (cfg_with("always"), None))
    clean = main()
    capsys.readouterr()
    monkeypatch.setattr(sen, "load_config", lambda: (cfg_with("bogus"), None))
    degraded = main()
    capsys.readouterr()
    assert (clean, degraded) == (0, 3)
