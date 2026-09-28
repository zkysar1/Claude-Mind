"""permissions-add.py writes no dead-verb path rules and drops the legacy ones.

Claude Code matches file permission checks on Edit(path) rules ONLY, and at every
startup prints "Write(<glob>) is not matched by file permission checks — only
Edit(path) rules are" for each Write(path)/MultiEdit(path) rule, in allow AND
deny (15 such lines measured 2026-09-28, Claude Code 2.1.280). This script
GENERATED those rules into .claude/settings.local.json, so cleaning the file
without fixing the writer would re-grow them on the next run of it (the
first-run /start ceremony's B10 step, or a manual run). Zak Code maps
Edit/Write/MultiEdit denies onto one protected-path regex (rb-9355), so the Edit
twin alone keeps both harnesses' coverage identical (guard-6856).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "core" / "scripts" / "permissions-add.py"
WORLD = "C:/example/world"
META = "C:/example/meta"

BASELINE_EDIT_DENIES = [
    "Edit(*/.claude/projects/*/memory/*)",
    "Edit(*core/scripts/settings-structural-validator.py)",
    "Edit(*core/scripts/settings-structural-validator.sh)",
    "Edit(*settings.local.json)",
    "Edit(**/.claude/settings.local.json)",
]


def run(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), WORLD, META, str(root)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=60,
    )


def settings_file(root: Path) -> Path:
    return root / ".claude" / "settings.local.json"


def dead(rules) -> list:
    return [
        r for r in rules
        if isinstance(r, str)
        and r.startswith(("Write(", "MultiEdit("))
        and r not in ("Write(*)", "MultiEdit(*)")
    ]


def test_fresh_create_writes_only_edit_path_rules(tmp_path):
    r = run(tmp_path)
    assert r.returncode == 0, r.stderr
    perms = json.loads(settings_file(tmp_path).read_text(encoding="utf-8"))["permissions"]
    assert dead(perms["allow"]) == [] and dead(perms["deny"]) == []
    for rule in BASELINE_EDIT_DENIES:
        assert rule in perms["deny"], rule
    assert "Edit(//C:/example/world/**)" in perms["allow"]
    assert "Edit(//C:/example/meta/**)" in perms["allow"]
    # Read(*) is seeded on fresh create, so per-path Read allows are redundant.
    assert not [a for a in perms["allow"] if a.startswith("Read(//C:/example")]


def test_legacy_file_drops_twins_and_keeps_everything_else(tmp_path):
    legacy = {
        "env": {"EXAMPLE": "1"},
        "statusLine": {"type": "command", "command": "echo status"},
        "outputStyle": "default",
        "autoMemoryEnabled": False,
        "permissions": {
            "allow": [
                "Bash(*)", "Read(*)", "Write(*)",
                "Edit(//C:/example/world/**)", "Write(//C:/example/world/**)",
                "Edit(//C:/old/place/**)", "Write(//C:/old/place/**)",
                "Write(//C:/only-write/**)",
            ],
            "deny": [
                "Write(*/.claude/projects/*/memory/*)", "Edit(*/.claude/projects/*/memory/*)",
                "Edit(*settings.local.json)", "Write(*settings.local.json)",
                # Write-only: the baseline merge adds the Edit twin, then this drops.
                "Write(**/.claude/settings.local.json)",
                "MultiEdit(*core/scripts/settings-structural-validator.py)",
                "Write(*/secret/*)",
            ],
            "ask": ["Edit(*/docs/*)", "Write(*/docs/*)"],
        },
    }
    path = settings_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(legacy, indent=2), encoding="utf-8")

    r = run(tmp_path)
    assert r.returncode == 0, r.stderr
    after = json.loads(path.read_text(encoding="utf-8"))
    perms = after["permissions"]

    # Only untwinned dead-verb rules survive, and each is reported, not converted.
    assert dead(perms["allow"]) == ["Write(//C:/only-write/**)"]
    assert dead(perms["deny"]) == ["Write(*/secret/*)"]
    assert dead(perms["ask"]) == []
    assert "! Write(//C:/only-write/**)" in r.stdout
    assert "! Write(*/secret/*)" in r.stdout
    assert "- Write(//C:/example/world/**)" in r.stdout
    # The whole-tool form is not a path rule and is left alone.
    assert "Write(*)" in perms["allow"]
    # Every live twin and every baseline concept keeps its Edit rule.
    assert "Edit(//C:/old/place/**)" in perms["allow"]
    assert "Edit(*/docs/*)" in perms["ask"]
    for rule in BASELINE_EDIT_DENIES:
        assert rule in perms["deny"], rule
    # Nothing outside permissions changed.
    for key in ("env", "statusLine", "outputStyle", "autoMemoryEnabled"):
        assert after[key] == legacy[key], key


def test_second_run_is_a_no_op(tmp_path):
    assert run(tmp_path).returncode == 0
    first = settings_file(tmp_path).read_bytes()
    r = run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert "(no changes needed" in r.stdout
    assert settings_file(tmp_path).read_bytes() == first


def test_tracked_settings_json_carries_no_dead_verb_path_rules():
    perms = json.loads((REPO / ".claude" / "settings.json").read_text(encoding="utf-8"))["permissions"]
    for key in ("allow", "deny", "ask"):
        assert dead(perms.get(key) or []) == [], key
