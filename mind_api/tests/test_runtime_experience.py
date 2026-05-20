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

    # Use paths relative to project_root (matching CLI convention).
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
