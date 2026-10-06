"""Tests for T1.8: experience.read --validate daemon parity.

The conftest fixture seeds alpha/experience.jsonl with two records. These
tests verify the daemon endpoint's validate=1 flag mirrors experience.py
cmd_validate (lines 796-837): cross-checking JSONL content_path fields
against .md files on disk.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request


def _get(port: int, path: str, query: dict, *, agent: str) -> tuple[int, str]:
    qs = urllib.parse.urlencode(query)
    url = f"http://127.0.0.1:{port}{path}?{qs}"
    req = urllib.request.Request(url)
    req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


def test_validate_all_valid(running_daemon):
    """validate=1 with matching .md files for every content_path reports valid."""
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"

    # The conftest seeds experience.jsonl with exp-test-1 and exp-test-2,
    # but their content_path fields don't point at real files. Rewrite with
    # records that have content_path pointing at real .md files.
    exp_dir = agent_dir / "experience"
    exp_dir.mkdir(exist_ok=True)
    (exp_dir / "exp-valid-1.md").write_text("trace 1", encoding="utf-8")
    (exp_dir / "exp-valid-2.md").write_text("trace 2", encoding="utf-8")

    # Legacy-era shape: agent name first, no agents/ parent (the pre-Phase-2.5.D
    # rows, still live). The current-era shape has its own test below.
    rel1 = f"alpha/experience/exp-valid-1.md"
    rel2 = f"alpha/experience/exp-valid-2.md"

    (agent_dir / "experience.jsonl").write_text(
        json.dumps({"id": "exp-valid-1", "type": "insight", "category": "test",
                     "summary": "s1", "content_path": rel1,
                     "created": "2026-05-10T08:00:00",
                     "retrieval_stats": {"retrieval_count": 0}}) + "\n"
        + json.dumps({"id": "exp-valid-2", "type": "lesson", "category": "test",
                       "summary": "s2", "content_path": rel2,
                       "created": "2026-05-12T08:00:00",
                       "retrieval_stats": {"retrieval_count": 0}}) + "\n",
        encoding="utf-8",
    )
    (agent_dir / "experience-archive.jsonl").write_text("", encoding="utf-8")

    status, body = _get(
        port, "/v1/experience/read",
        {"validate": "1"},
        agent="alpha",
    )
    assert status == 200
    result = json.loads(body)
    assert result["valid"] is True
    assert result["jsonl_without_md"] == []
    assert result["md_without_jsonl"] == []
    assert result["total_jsonl"] == 2
    assert result["total_md"] == 2


def test_validate_catches_missing_md(running_daemon):
    """validate=1 detects JSONL records whose content_path .md file is missing."""
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"

    # Create one .md but reference two in JSONL — second is missing.
    exp_dir = agent_dir / "experience"
    exp_dir.mkdir(exist_ok=True)
    (exp_dir / "exp-exists.md").write_text("trace", encoding="utf-8")

    rel_exists = "alpha/experience/exp-exists.md"
    rel_missing = "alpha/experience/exp-gone.md"

    (agent_dir / "experience.jsonl").write_text(
        json.dumps({"id": "exp-exists", "type": "insight", "category": "test",
                     "summary": "s1", "content_path": rel_exists,
                     "created": "2026-05-10T08:00:00",
                     "retrieval_stats": {"retrieval_count": 0}}) + "\n"
        + json.dumps({"id": "exp-gone", "type": "lesson", "category": "test",
                       "summary": "s2", "content_path": rel_missing,
                       "created": "2026-05-12T08:00:00",
                       "retrieval_stats": {"retrieval_count": 0}}) + "\n",
        encoding="utf-8",
    )
    (agent_dir / "experience-archive.jsonl").write_text("", encoding="utf-8")

    status, body = _get(
        port, "/v1/experience/read",
        {"validate": "1"},
        agent="alpha",
    )
    assert status == 200
    result = json.loads(body)
    assert result["valid"] is False
    assert len(result["jsonl_without_md"]) == 1
    assert result["jsonl_without_md"][0]["id"] == "exp-gone"


def test_validate_catches_orphan_md(running_daemon):
    """validate=1 detects .md files in experience/ without a matching JSONL record."""
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"

    exp_dir = agent_dir / "experience"
    exp_dir.mkdir(exist_ok=True)
    (exp_dir / "exp-linked.md").write_text("trace", encoding="utf-8")
    (exp_dir / "exp-orphan.md").write_text("orphan", encoding="utf-8")

    rel_linked = "alpha/experience/exp-linked.md"

    (agent_dir / "experience.jsonl").write_text(
        json.dumps({"id": "exp-linked", "type": "insight", "category": "test",
                     "summary": "s1", "content_path": rel_linked,
                     "created": "2026-05-10T08:00:00",
                     "retrieval_stats": {"retrieval_count": 0}}) + "\n",
        encoding="utf-8",
    )
    (agent_dir / "experience-archive.jsonl").write_text("", encoding="utf-8")

    status, body = _get(
        port, "/v1/experience/read",
        {"validate": "1"},
        agent="alpha",
    )
    assert status == 200
    result = json.loads(body)
    assert result["valid"] is False
    assert len(result["md_without_jsonl"]) == 1
    assert result["md_without_jsonl"][0]["file"] == "exp-orphan.md"


def _seed_store(project_root, rows, files):
    """Seed alpha's experience.jsonl with (id, content_path) rows and create the
    files (paths relative to project_root) that exist on disk."""
    for rel in files:
        f = project_root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("trace", encoding="utf-8")
    agent_dir = project_root / "agents" / "alpha"
    (agent_dir / "experience.jsonl").write_text(
        "".join(json.dumps({"id": rid, "type": "insight", "category": "test",
                            "summary": "s", "content_path": cp,
                            "created": "2026-05-10T08:00:00",
                            "retrieval_stats": {"retrieval_count": 0}}) + "\n"
                for rid, cp in rows),
        encoding="utf-8",
    )
    (agent_dir / "experience-archive.jsonl").write_text("", encoding="utf-8")


def _validate_result(port):
    status, body = _get(port, "/v1/experience/read", {"validate": "1"}, agent="alpha")
    assert status == 200
    return json.loads(body)


def test_validate_current_era_content_paths_resolve(running_daemon):
    """Current-era rows (agents/<agent>/experience/x.md, PROJECT_ROOT-relative)
    validate. The endpoint used to resolve them against the agents parent, so
    every such row read missing and its file read orphan (g-115-11647)."""
    project_root, port = running_daemon
    _seed_store(
        project_root,
        [("exp-cur-1", "agents/alpha/experience/exp-cur-1.md"),
         ("exp-cur-2", "agents/alpha/experience/exp-cur-2.md")],
        ["agents/alpha/experience/exp-cur-1.md",
         "agents/alpha/experience/exp-cur-2.md"],
    )
    result = _validate_result(port)
    assert result["valid"] is True
    assert result["jsonl_without_md"] == []
    assert result["md_without_jsonl"] == []
    assert result["total_jsonl"] == 2
    assert result["total_md"] == 2


def test_validate_both_eras_in_one_store(running_daemon):
    """The corpus is two-era (rb-7386): a current-era row and a legacy row (agent
    name first, no agents/ parent) each resolve to their real file."""
    project_root, port = running_daemon
    _seed_store(
        project_root,
        [("exp-new", "agents/alpha/experience/exp-new.md"),
         ("exp-old", "alpha/experience/exp-old.md")],
        ["agents/alpha/experience/exp-new.md",
         "agents/alpha/experience/exp-old.md"],
    )
    result = _validate_result(port)
    assert result["valid"] is True
    assert result["jsonl_without_md"] == []
    assert result["md_without_jsonl"] == []
    assert result["total_jsonl"] == 2
    assert result["total_md"] == 2


def test_validate_current_era_missing_file_is_still_missing(running_daemon):
    """Negative control: the fix widens where a path is looked for, not whether
    the file must exist. A current-era row whose file is absent is reported, at
    the current-era path rather than a doubled agents/agents/ one."""
    project_root, port = running_daemon
    _seed_store(
        project_root,
        [("exp-here", "agents/alpha/experience/exp-here.md"),
         ("exp-gone", "agents/alpha/experience/exp-gone.md")],
        ["agents/alpha/experience/exp-here.md"],
    )
    result = _validate_result(port)
    assert result["valid"] is False
    assert [m["id"] for m in result["jsonl_without_md"]] == ["exp-gone"]
    expected = result["jsonl_without_md"][0]["expected_path"]
    assert expected.endswith("agents/alpha/experience/exp-gone.md")
    assert "/agents/agents/" not in expected


def test_validate_second_path_is_exact_never_a_basename_match(running_daemon):
    """guard-2860: a legacy-shaped row whose file is absent stays missing even
    when the same basename exists under another agent's experience dir."""
    project_root, port = running_daemon
    _seed_store(
        project_root,
        [("exp-dangling", "alpha/experience/exp-dangling.md")],
        ["agents/bravo/experience/exp-dangling.md"],
    )
    result = _validate_result(port)
    assert result["valid"] is False
    assert [m["id"] for m in result["jsonl_without_md"]] == ["exp-dangling"]
    assert result["md_without_jsonl"] == []
    # A legacy-shaped row is reported where that shape resolves: under the agents
    # parent, as it always was (not PROJECT_ROOT/alpha/..., not agents/agents/...).
    expected = result["jsonl_without_md"][0]["expected_path"]
    assert expected.endswith("agents/alpha/experience/exp-dangling.md")
    assert "/agents/agents/" not in expected


def test_validate_does_not_trust_the_old_one_level_off_root(running_daemon):
    """rb-5190: stray content at the old wrong root must not satisfy a row. A file
    under agents/agents/alpha/experience/ is where the endpoint used to look for
    an agents/alpha/... row; with the real file absent the row stays missing."""
    project_root, port = running_daemon
    _seed_store(
        project_root,
        [("exp-stray", "agents/alpha/experience/exp-stray.md")],
        ["agents/agents/alpha/experience/exp-stray.md"],
    )
    result = _validate_result(port)
    assert result["valid"] is False
    assert [m["id"] for m in result["jsonl_without_md"]] == ["exp-stray"]
