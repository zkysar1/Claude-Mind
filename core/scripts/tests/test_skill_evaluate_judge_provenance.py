"""test_skill_evaluate_judge_provenance.py --  (asp-306).

Pins judge provenance on skill-quality evaluations. The gap this closes:
meta/skill-quality.yaml recorded five dimension grades with NOTHING naming
which model produced them, while the judge population is heterogeneous across
the fleet -- so aggregate drift across a model upgrade was indistinguishable
from real skill-quality change.

Four properties are pinned here, and the third is the one that matters most:

  1. Both fields are written on every new evaluation.
  2. Both fall back to "unknown" rather than guessing.
  3. CLAUDE_CODE_SUBAGENT_MODEL must NEVER become judge_model. It names the
     SUBAGENT model while scoring runs on the MAIN loop; measured 2026-09-01
     on cc-04 the two genuinely differed (subagent env read claude-opus-4-6
     while the session ran claude-opus-5). A confidently wrong judge is worse
     than an absent one -- it corrupts exactly the cross-model comparison the
     field exists to enable (guard-1925).
  4. TWIN PARITY of entry key order. core/scripts/skill-evaluate.py and
     mind_api/src/meta/skill_evaluate.py are byte-compat twins and both dump
     with sort_keys=False, so insertion order IS the on-disk byte order. The
     boundary (core/BOUNDARY.md) forbids sharing a module between the layers,
     so the only thing keeping them aligned is a test.

Legacy records carry neither key; they must read as unknown and must NOT be
dropped from the judge summary, since hiding un-provenanced records would hide
the very mixture the summary exists to expose.

Run: py -3 -m pytest core/scripts/tests/test_skill_evaluate_judge_provenance.py -v
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
REPO = CORE_SCRIPTS.parent.parent
CLI = CORE_SCRIPTS / "skill-evaluate.py"
DAEMON = REPO / "mind_api" / "src" / "meta" / "skill_evaluate.py"

JUDGE_ENV = ("MIND_JUDGE_MODEL", "CLAUDECODE", "ZAKCODE_MODEL",
             "ZAKCODE_SESSION", "CLAUDE_CODE_SUBAGENT_MODEL")


def _load():
    spec = importlib.util.spec_from_file_location("skill_evaluate_cli", CLI)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod(monkeypatch):
    for key in JUDGE_ENV:
        monkeypatch.delenv(key, raising=False)
    return _load()


# --- 1 + 2: resolution, both paths -----------------------------------------
#
# These exercise the CLI LAYER (_judge_from_env composed with the normalizer),
# because that is where reading the environment is correct: this module runs as
# a fresh subprocess of the session that produced the grades, so its
# environment IS the judge's. The writer must never do this -- pinned below in
# "the wiring".

def _resolve(mod):
    """The CLI layer's own resolution, exactly as cmd_score performs it."""
    return mod._judge_provenance(*mod._judge_from_env())


def test_unresolvable_falls_back_to_unknown(mod):
    assert _resolve(mod) == ("unknown", "unknown")


def test_claude_code_harness_detected(mod, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    assert _resolve(mod) == ("unknown", "claude-code")


def test_explicit_judge_model_is_used(mod, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("MIND_JUDGE_MODEL", "claude-opus-5")
    assert _resolve(mod) == ("claude-opus-5", "claude-code")


def test_zakcode_harness_detected(mod, monkeypatch):
    monkeypatch.setenv("ZAKCODE_MODEL", "qwen3")
    assert _resolve(mod) == ("unknown", "zakcode")


def test_blank_env_is_not_a_value(mod, monkeypatch):
    monkeypatch.setenv("MIND_JUDGE_MODEL", "   ")
    monkeypatch.setenv("CLAUDECODE", "")
    assert _resolve(mod) == ("unknown", "unknown")


def test_unrecognised_harness_normalizes_to_unknown(mod):
    """The value is caller-supplied over HTTP, so it is untrusted input and the
    vocabulary is closed -- aggregate consumers group by it."""
    assert mod._judge_provenance("m", "not-a-harness") == ("m", "unknown")
    assert mod._judge_provenance("m", "claude-code") == ("m", "claude-code")


# --- 3: the trap -----------------------------------------------------------

def test_subagent_model_never_becomes_judge_model(mod, monkeypatch):
    """The whole point of the field is cross-model comparison; a wrong value
    is worse than an absent one."""
    monkeypatch.setenv("CLAUDE_CODE_SUBAGENT_MODEL", "claude-opus-4-6")
    monkeypatch.setenv("CLAUDECODE", "1")
    judge_model, harness = _resolve(mod)
    assert judge_model == "unknown"
    assert harness == "claude-code"


def test_source_does_not_read_subagent_env():
    """Belt and braces: neither twin may READ that variable.

    The predicate is deliberately "does not read it", not "does not mention
    it" -- both twins name it in a docstring precisely to record why it is
    excluded, and a bare substring check would forbid documenting the
    decision. Assert against the read forms instead.
    """
    reads = ('os.environ.get("CLAUDE_CODE_SUBAGENT_MODEL"',
             "os.environ.get('CLAUDE_CODE_SUBAGENT_MODEL'",
             'os.environ["CLAUDE_CODE_SUBAGENT_MODEL"',
             "os.environ['CLAUDE_CODE_SUBAGENT_MODEL'",
             'os.getenv("CLAUDE_CODE_SUBAGENT_MODEL"',
             "os.getenv('CLAUDE_CODE_SUBAGENT_MODEL'")
    for path in (CLI, DAEMON):
        text = path.read_text(encoding="utf-8")
        for form in reads:
            assert form not in text, (path, form)


# --- writer: fields land on a real scored record ----------------------------

def _score(mod, goal, **grades):
    args = argparse.Namespace(
        skill="t-skill", goal=goal,
        safety=grades.get("safety", "good"),
        completeness=grades.get("completeness", "good"),
        executability=grades.get("executability", "average"),
        maintainability=grades.get("maintainability", "average"),
        cost_awareness=grades.get("cost_awareness", "poor"))
    mod.cmd_score(args)


def test_score_writes_both_fields(mod, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(mod, "QUALITY_PATH", tmp_path / "skill-quality.yaml")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("MIND_JUDGE_MODEL", "claude-opus-5")
    _score(mod, "g-1")
    capsys.readouterr()
    entry = mod.read_yaml(mod.QUALITY_PATH)["skills"]["t-skill"]["evaluations"][0]
    assert entry["judge_model"] == "claude-opus-5"
    assert entry["harness"] == "claude-code"


def test_score_writes_unknown_when_unresolvable(mod, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(mod, "QUALITY_PATH", tmp_path / "skill-quality.yaml")
    _score(mod, "g-1")
    capsys.readouterr()
    entry = mod.read_yaml(mod.QUALITY_PATH)["skills"]["t-skill"]["evaluations"][0]
    assert entry["judge_model"] == "unknown"
    assert entry["harness"] == "unknown"


def test_new_keys_are_appended_last(mod, monkeypatch, tmp_path, capsys):
    """sort_keys=False -> insertion order is the byte order. New fields go at
    the END so existing records' prefix is unchanged."""
    monkeypatch.setattr(mod, "QUALITY_PATH", tmp_path / "skill-quality.yaml")
    _score(mod, "g-1")
    capsys.readouterr()
    entry = mod.read_yaml(mod.QUALITY_PATH)["skills"]["t-skill"]["evaluations"][0]
    assert list(entry)[-2:] == ["judge_model", "harness"]
    assert list(entry)[:8] == ["goal_id", "date", "safety", "completeness",
                               "executability", "maintainability",
                               "cost_awareness", "overall"]


# --- judge summary: mixture visibility + legacy records ---------------------

def test_judge_summary_counts_legacy_as_unknown(mod):
    evals = [
        {"judge_model": "claude-opus-5", "harness": "claude-code"},
        {"judge_model": "claude-opus-5", "harness": "claude-code"},
        {"goal_id": "legacy-no-judge-keys"},
    ]
    assert mod._judge_summary(evals) == [
        {"judge_model": "claude-opus-5", "harness": "claude-code", "n": 2},
        {"judge_model": "unknown", "harness": "unknown", "n": 1},
    ]


def test_judge_summary_empty_and_malformed_are_safe(mod):
    assert mod._judge_summary([]) == []
    assert mod._judge_summary(None) == []
    assert mod._judge_summary(["not-a-dict", 7]) == []


def test_report_surfaces_judge_mixture(mod, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(mod, "QUALITY_PATH", tmp_path / "skill-quality.yaml")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("MIND_JUDGE_MODEL", "model-a")
    _score(mod, "g-1")
    monkeypatch.setenv("MIND_JUDGE_MODEL", "model-b")
    _score(mod, "g-2")
    capsys.readouterr()
    import json
    mod.cmd_report(argparse.Namespace())
    report = json.loads(capsys.readouterr().out)
    judges = report["skills"]["t-skill"]["judges"]
    assert len(judges) == 2, judges
    assert {j["judge_model"] for j in judges} == {"model-a", "model-b"}


# --- 4: twin parity ---------------------------------------------------------

_ENTRY_RE = re.compile(
    r'"cost_awareness": scores\["cost_awareness"\],\s*\n'
    r'\s*"overall": overall,\s*\n'
    r'\s*"judge_model": judge_model,\s*\n'
    r'\s*"harness": harness,')


def test_twins_share_entry_key_order():
    """core/BOUNDARY.md forbids the daemon sharing a module with core/scripts,
    so the twins are duplicated by design and only a test keeps them aligned.
    Both dump with sort_keys=False, so a divergence here is a byte divergence."""
    for path in (CLI, DAEMON):
        assert _ENTRY_RE.search(path.read_text(encoding="utf-8")), path


def test_twins_both_define_the_helpers():
    for path in (CLI, DAEMON):
        text = path.read_text(encoding="utf-8")
        assert "def _judge_provenance(" in text, path
        assert "def _judge_summary(" in text, path
