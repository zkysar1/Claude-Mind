"""test_scripts_referenced_gate_githooks.py — scripts-referenced-gate.py ().

The orphan detector collects reference text from .claude/, core/config/, and
core/scripts/ filtered by REF_EXTS. core/githooks/ hook files (pre-commit,
post-commit) are EXTENSIONLESS, so a script referenced ONLY by a git hook used
to false-flag as orphan (g-248-90 found 5 such false positives). The fix scans
core/githooks/* with NO extension filter. These tests pin that behavior on a
synthetic temp repo so the false-positive class cannot regress.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))


def _load_gate():
    # scripts-referenced-gate.py is hyphenated — load via importlib for its symbols.
    spec = importlib.util.spec_from_file_location(
        "scripts_referenced_gate", CORE_SCRIPTS / "scripts-referenced-gate.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GATE = _load_gate()


def _setup_repo(tmp_path: Path):
    scripts = tmp_path / "core" / "scripts"
    githooks = tmp_path / "core" / "githooks"
    config = tmp_path / "core" / "config"
    claude = tmp_path / ".claude"
    for d in (scripts, githooks, config, claude):
        d.mkdir(parents=True)
    # A script referenced ONLY by an extensionless git hook.
    (scripts / "hook-only.sh").write_text(
        "#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
    # A script referenced nowhere — a genuine orphan.
    (scripts / "true-orphan.sh").write_text(
        "#!/usr/bin/env bash\necho bye\n", encoding="utf-8")
    # The extensionless pre-commit hook that invokes hook-only.sh.
    (githooks / "pre-commit").write_text(
        "#!/usr/bin/env bash\nbash core/scripts/hook-only.sh || exit 1\n",
        encoding="utf-8")
    return scripts, githooks, config, claude


def _patch(monkeypatch, tmp_path, scripts, githooks, config, claude):
    monkeypatch.setattr(GATE, "SCRIPTS_DIR", scripts)
    monkeypatch.setattr(GATE, "GITHOOKS_DIR", githooks)
    monkeypatch.setattr(GATE, "LIVE_REF_ROOTS", [claude, config, scripts])
    monkeypatch.setattr(GATE, "LIVE_REF_FILES", [])
    monkeypatch.setattr(GATE, "SETTINGS_JSON", tmp_path / ".claude" / "settings.json")


def test_githooks_reference_prevents_orphan(tmp_path, monkeypatch):
    scripts, githooks, config, claude = _setup_repo(tmp_path)
    _patch(monkeypatch, tmp_path, scripts, githooks, config, claude)

    corpus = GATE._collect_reference_text()
    # The extensionless hook MUST be scanned as a reference surface.
    hook_in_corpus = any(str(p).endswith("pre-commit") for p, _ in corpus)
    assert hook_in_corpus, "core/githooks/pre-commit must be scanned (g-248-93)"

    report = GATE._build_report(
        GATE._collect_scripts(scripts), corpus, set(), set())
    orphans = {e["basename"] for e in report["script_orphans"]}
    assert "hook-only.sh" not in orphans, \
        "hook-referenced script must NOT be flagged orphan (g-248-93)"
    assert "true-orphan.sh" in orphans, \
        "genuinely-unreferenced script must still be flagged"


def test_githooks_missing_dir_fail_open(tmp_path, monkeypatch):
    scripts, githooks, config, claude = _setup_repo(tmp_path)
    # Point GITHOOKS_DIR at a nonexistent path — collection must not raise,
    # and without the hook scanned hook-only.sh reverts to orphan. This proves
    # the githooks scan is exactly what un-flags it (not some other path).
    monkeypatch.setattr(GATE, "SCRIPTS_DIR", scripts)
    monkeypatch.setattr(GATE, "GITHOOKS_DIR", tmp_path / "core" / "absent-githooks")
    monkeypatch.setattr(GATE, "LIVE_REF_ROOTS", [claude, config, scripts])
    monkeypatch.setattr(GATE, "LIVE_REF_FILES", [])
    monkeypatch.setattr(GATE, "SETTINGS_JSON", tmp_path / ".claude" / "settings.json")

    corpus = GATE._collect_reference_text()  # must not raise on missing dir
    report = GATE._build_report(
        GATE._collect_scripts(scripts), corpus, set(), set())
    orphans = {e["basename"] for e in report["script_orphans"]}
    assert "hook-only.sh" in orphans, \
        "without githooks scanned, the hook-only script reverts to orphan"
