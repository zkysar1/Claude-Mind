"""aspirations-complete.sh carries the closure gate's flags ().

  - a supply-gated aspiration refuses a plain close (rc 1, aspiration_needle_unmet)
  - --needle-satisfied reads the needle block from stdin and lands the close
  - --override-supply-close sends the audited bypass header
  - --help lists the new flags; an unknown flag is still refused (rc 2)
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from _bash_helpers import BASH  # guard-580: never a bare "bash" argv[0]

REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "core" / "scripts" / "aspirations-complete.sh"

NEEDLE = ("weekly matchup-specific start/sit recommendations backed by documented "
          "defensive weakness analysis")


def _seed(world: Path, asp):
    (world / "aspirations.jsonl").write_text(json.dumps(asp, ensure_ascii=True) + "\n", encoding="utf-8")


def _read_jsonl(path: Path):
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _run(args, *, project_root: Path, stdin: str = ""):
    env = os.environ.copy()
    env["MIND_AGENT"] = "alpha"
    env["MSYS_NO_PATHCONV"] = "1"
    env["RT_DIR"] = str(project_root / "mind_api" / "state")
    proc = subprocess.run([BASH, WRAPPER.as_posix(), *args], env=env, input=stdin,
                          capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout, proc.stderr


def _asp_025():
    return {
        "id": "asp-025", "title": "Scouting database sources while the feed is blocked",
        "origin_signal": "all_blocked_gap", "status": "active", "priority": "MEDIUM",
        "archived": False, "scope": "sprint",
        "motivation": "Build the scouting database of sources for the coaching workflow.",
        "supply_evidence": {"gap": "No compiled list of scouting sources with documented defensive tendencies exists.",
                            "needle": NEEDLE, "needle_by": "2026-09-14", "checked": ["asp-024"]},
        "goals": [{"id": "g-025-01", "title": "Compile scouting database sources",
                   "status": "completed", "recurring": False}],
        "progress": {"completed_goals": 1, "total_goals": 1, "recurring_goals": 0},
    }


def test_plain_close_of_a_needle_record_is_refused(running_daemon):
    root, _port = running_daemon
    _seed(root / "world", _asp_025())
    rc, out, err = _run(["asp-025"], project_root=root)
    assert rc == 1, (rc, out, err)
    assert "aspiration_needle_unmet" in err and "needle_unaddressed" in err
    assert len(_read_jsonl(root / "world" / "aspirations.jsonl")) == 1  # still live


def test_needle_satisfied_reads_the_block_from_stdin(running_daemon):
    root, _port = running_daemon
    world = root / "world"
    _seed(world, _asp_025())
    (world / "reports").mkdir(exist_ok=True)
    (world / "reports" / "week1-start-sit.md").write_text("# Week 1\n", encoding="utf-8")
    block = {"statement": ("Every matchup now has start/sit recommendations for the week, each backed by "
                           "the documented defensive weakness analysis in the report the operator reads."),
             "artifacts": ["reports/week1-start-sit.md"]}
    rc, out, err = _run(["asp-025", "--needle-satisfied"], project_root=root, stdin=json.dumps(block))
    assert rc == 0, (rc, out, err)
    rec = json.loads(out)
    assert rec["status"] == "completed" and rec["needle_satisfaction"]["claimed_at"]
    assert _read_jsonl(world / "aspirations.jsonl") == []


def test_override_supply_close_sends_the_audited_header(running_daemon):
    root, _port = running_daemon
    world = root / "world"
    _seed(world, _asp_025())
    rc, out, err = _run(["asp-025", "--override-supply-close", "wrapper test: keep the refusal on record"],
                        project_root=root)
    assert rc == 0, (rc, out, err)
    assert "aspiration-supply-close-gate: refusal overridden" in err
    rows = _read_jsonl(world / "aspiration-supply-overrides.jsonl")
    assert rows and rows[-1]["kind"] == "close" and rows[-1]["asp_id"] == "asp-025"


def test_help_lists_the_flags_and_unknown_flags_still_refuse(running_daemon):
    root, _port = running_daemon
    rc, out, err = _run(["--help"], project_root=root)
    assert rc == 0 and "--needle-satisfied" in (out + err) and "--override-supply-close" in (out + err)
    rc, out, err = _run(["asp-025", "--needle-satisfied-please"], project_root=root)
    assert rc == 2, (rc, out, err)
