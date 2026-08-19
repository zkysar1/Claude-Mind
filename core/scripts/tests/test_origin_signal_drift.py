"""test_origin_signal_drift.py — regression for .

Guards the prescribed-vs-allowed origin_signal drift class: a skill's
pseudocode prescribes a literal `origin_signal` that `ALLOWED_PREFIXES` in
gates/origin_signal.py does not admit.

THREE shapes, and only ONE is loud — which is why the class kept accruing:
  * GOAL level, auto-derivable title — SILENT, and the COMMON case. Layer-D
                      auto-derive rewrites the invalid signal from the title
                      slug and returns a NON-BLOCKING pass. Cognitive-primitive
                      goals are titled exactly Investigate:/Idea:/Unblock:/
                      Maintain:/Apply:, so most goal-level violations land here.
  * GOAL level, non-derivable title — LOUD. The gate refuses the add. This is
                      the only self-announcing shape, and the only reason
                      `skill-discovery-audit:` was ever noticed.
  * ASPIRATION level — SILENT. NOT gate-checked at add time, so the bad value
                      is simply written. `blocker_pattern:` survived here.

Both mismatches were live simultaneously and neither was caught by
test_goal_source_infer_parity.py, which pins code→gate while this pins
docs→gate. The two are complementary halves, not duplicates.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from _goal_source import infer
from gates.origin_signal import ALLOWED_PREFIXES, is_valid

import pathlib
import sys as _sys
_sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _verify_corpus  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "check-origin-signal-drift.py"

# The two prefixes this goal reconciled, with a representative concrete suffix.
G3096_ADDED = {
    "skill-discovery-audit:": "skill-discovery-audit:some-skill:silently_undertriggering",
    "blocker_pattern:": "blocker_pattern:remote-store-unreachable",
}


def _run(root: Path | None = None):
    cmd = [sys.executable, str(SCRIPT)]
    if root is not None:
        cmd += ["--root", str(root)]
    return subprocess.run(cmd, capture_output=True, text=True)


# --- the reconciliation itself ---------------------------------------------

@pytest.mark.parametrize("prefix", sorted(G3096_ADDED))
def test_added_prefix_in_allowed_prefixes(prefix):
    assert prefix in ALLOWED_PREFIXES, (
        f"{prefix} missing from ALLOWED_PREFIXES (g-115-3096 regressed)")


@pytest.mark.parametrize("sig", sorted(G3096_ADDED.values()))
def test_added_prefix_accepted_by_gate(sig):
    assert is_valid(sig), f"gate rejects {sig!r}"


@pytest.mark.parametrize("sig", sorted(G3096_ADDED.values()))
def test_added_prefix_infers_cycle_detector(sig):
    """Both are automated detection-driven filers.

    Registering in the gate WITHOUT the infer() branch would leave goal_source
    null — the exact half-fix that made the g-115-1100 prefixes look registered
    while their attribution stayed broken.
    """
    assert infer(sig) == "cycle-detector", (
        f"infer({sig!r}) = {infer(sig)!r}, expected cycle-detector")


# --- the standing checker --------------------------------------------------

def test_live_repo_is_clean():
    """The live corpus has no prescribed-but-unregistered prefix.

    This is the assertion that actually fails when a future skill edit
    reintroduces the class.
    """
    r = _run()
    assert r.returncode == 0, (
        f"origin_signal drift detected in the live repo:\n{r.stderr}")


def test_checker_detects_drift(tmp_path):
    """Negative control — a clean live repo must not be a vacuous pass."""
    skill = tmp_path / ".claude" / "skills" / "fake" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text('"origin_signal": "totally-made-up:thing"\n', encoding="utf-8")

    r = _run(tmp_path)
    assert r.returncode == 1
    assert "ORIGIN_SIGNAL_DRIFT" in r.stderr
    assert "totally-made-up:thing" in r.stderr


def test_checker_scans_config_surface_too(tmp_path):
    """core/config/**/*.md is in scope, not just .claude/skills.

    One of the two real mismatches was found outside the skills tree, so a
    skills-only scan would have been a half-audit.
    """
    conv = tmp_path / "core" / "config" / "conventions" / "fake.md"
    conv.parent.mkdir(parents=True)
    conv.write_text('origin_signal: "bogus-prefix:x"\n', encoding="utf-8")

    r = _run(tmp_path)
    assert r.returncode == 1
    assert "bogus-prefix:x" in r.stderr


def test_sanctioned_prefix_passes(tmp_path):
    skill = tmp_path / ".claude" / "skills" / "fake" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text('"origin_signal": "investigate:real-thing"\n', encoding="utf-8")

    r = _run(tmp_path)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("literal", [
    '"origin_signal": "<origin_signal>"',
    '"origin_signal": "{new_goal.type}:{goal.id}"',
])
def test_templated_values_do_not_fail(tmp_path, literal):
    """Placeholders carry no literal prefix — reported, never fatal."""
    skill = tmp_path / ".claude" / "skills" / "fake" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(literal + "\n", encoding="utf-8")

    r = _run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert "templated" in r.stdout


def test_checker_is_wired_into_verify_learning():
    """The checker must be INVOKED, not merely exist.

    sq-019 (integration-path coverage) surfaced this: every other test here
    proves the script behaves correctly in isolation. None of them would fail
    if Section OSD were deleted from verify-learning/SKILL.md — the script
    would keep passing its unit tests while the standing guard silently stopped
    running. That is the same trigger-exists-but-nothing-calls-it shape the
    goal's own DEFECT 2 was about.
    """
    skill = (Path(__file__).resolve().parents[3]
             / ".claude" / "skills" / "verify-learning" / "SKILL.md")
    # Corpus, not the file: the verify-learning check corpus moved to
    # core/config/verify-learning-checks.jsonl on 2026-08-18 ().
    # This canary pins a CALL SITE, and the call site moved with it.
    text = _verify_corpus.corpus_text()
    assert "check-origin-signal-drift.py" in text, (
        "verify-learning/SKILL.md no longer invokes check-origin-signal-drift.py "
        "— the docs->gate drift guard is unwired and silently stopped running "
        "(g-115-3096 Section OSD)")


def test_json_mode_reports_verdict(tmp_path):
    skill = tmp_path / ".claude" / "skills" / "fake" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text('"origin_signal": "nope:x"\n', encoding="utf-8")

    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path), "--json"],
        capture_output=True, text=True)
    assert r.returncode == 1
    import json
    payload = json.loads(r.stdout)
    assert payload["verdict"] == "drift"
    assert payload["mismatches"][0]["value"] == "nope:x"
