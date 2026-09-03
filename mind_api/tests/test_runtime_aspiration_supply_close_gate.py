"""Daemon wiring for the aspiration-supply CLOSURE gate ().

The add endpoint's supply gate decides whether a self-generated aspiration
may be FILED (test_runtime_aspiration_supply_gate.py). This file pins the
twin at CLOSE: /v1/aspirations/complete refuses a record carrying
supply_evidence.needle unless the closer states how the delivered work meets
the needle and names an artifact that exists; force does not bypass; the
audited override and --override-all do; user-directed records without a
needle are untouched; complete-intent's rationale must address the needle.

Fixture: the coach asp-025 shape (2026-09-03) — one completed goal, needle
present, no intent block.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def _post(port, path, query, body=None, *, agent="alpha", headers=None):
    qs = urllib.parse.urlencode(query)
    url = f"http://127.0.0.1:{port}{path}?{qs}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if agent:
        req.add_header("X-Mind-Agent", agent)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _read_jsonl(path: Path):
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _seed(world: Path, *asps):
    (world / "aspirations.jsonl").write_text(
        "".join(json.dumps(a, ensure_ascii=True) + "\n" for a in asps), encoding="utf-8")


def _ensure_intent_config(project_root: Path):
    cfg_dir = project_root / "core" / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = cfg_dir / "aspirations.yaml"
    if not cfg_path.exists():
        cfg_path.write_text("intent_satisfaction:\n  min_evidence_by_scope:\n    sprint: 2\n"
                            "    project: 3\n    initiative: 5\n", encoding="utf-8")


NEEDLE = ("weekly matchup-specific start/sit recommendations backed by documented "
          "defensive weakness analysis")
GOOD_STATEMENT = ("Every matchup now has start/sit recommendations for the week, each backed by the "
                  "documented defensive weakness analysis in the report the operator reads.")


def _asp_025(asp_id="asp-025", *, goals=None):
    n = asp_id[4:]
    return {
        "id": asp_id, "title": "Scouting database sources while the feed is blocked",
        "origin_signal": "all_blocked_gap", "status": "active", "priority": "MEDIUM",
        "archived": False, "scope": "sprint",
        "motivation": "Build the scouting database of sources for the coaching workflow.",
        "supply_evidence": {
            "gap": "No compiled list of scouting sources with documented defensive tendencies exists.",
            "needle": NEEDLE, "needle_by": "2026-09-14", "checked": ["asp-024"],
        },
        "goals": goals if goals is not None else [
            {"id": f"g-{n}-01", "title": "Compile scouting database sources", "status": "completed",
             "recurring": False}],
        "progress": {"completed_goals": 1, "total_goals": 1, "recurring_goals": 0},
    }


def _artifact(world: Path) -> str:
    (world / "reports").mkdir(exist_ok=True)
    (world / "reports" / "week1-start-sit.md").write_text("# Week 1 start/sit\n", encoding="utf-8")
    return "reports/week1-start-sit.md"


def _complete(port, asp_id, **kw):
    return _post(port, "/v1/aspirations/complete", {"asp_id": asp_id, "source": "world", **kw.pop("query", {})}, **kw)


def test_plain_close_of_a_needle_record_is_refused_naming_the_needle(running_daemon):
    root, port = running_daemon
    world = root / "world"
    _seed(world, _asp_025())
    status, body = _complete(port, "asp-025")
    assert status == 400, body
    assert body["error"] == "aspiration_needle_unmet"
    assert body["gate"] == "aspiration-supply-close-gate"
    out = body["gate_output"]
    assert [f["check"] for f in out["failures"]] == ["needle_unaddressed"]
    assert NEEDLE in out["failures"][0]["detail"]
    assert "--needle-satisfied" in out["remedy"] and "parent_aspiration:asp-025" in out["remedy"]
    assert len(_read_jsonl(world / "aspirations.jsonl")) == 1
    assert _read_jsonl(world / "aspirations-archive.jsonl") == []
    # force skips the recurring/unfinished guards, never this one
    status, body = _complete(port, "asp-025", query={"force": "true"})
    assert status == 400 and body["error"] == "aspiration_needle_unmet"


def test_needle_satisfied_with_statement_and_artifact_lands(running_daemon):
    root, port = running_daemon
    world = root / "world"
    _seed(world, _asp_025())
    art = _artifact(world)
    status, body = _complete(port, "asp-025", query={"needle_satisfied": "true"},
                             body={"statement": GOOD_STATEMENT, "artifacts": [art]})
    assert status == 200, body
    rec = body["aspiration"]
    assert rec["status"] == "completed" and rec["needle_satisfaction"]["claimed_at"]
    assert rec["needle_satisfaction"]["artifacts"] == [art]
    archive = _read_jsonl(world / "aspirations-archive.jsonl")
    assert len(archive) == 1 and archive[0]["needle_satisfaction"]["statement"] == GOOD_STATEMENT
    assert _read_jsonl(world / "aspirations.jsonl") == []


def test_needle_block_is_checked_on_content(running_daemon):
    root, port = running_daemon
    world = root / "world"
    _seed(world, _asp_025())
    status, body = _complete(port, "asp-025", query={"needle_satisfied": "true"},
                             body={"statement": "done", "artifacts": ["g-025-01"]})
    assert status == 400, body
    checks = {f["check"] for f in body["gate_output"]["failures"]}
    assert checks == {"needle_statement_short", "needle_statement_disjoint", "needle_artifact_unverified"}
    status, body = _complete(port, "asp-025", query={"needle_satisfied": "true"}, body=["not", "an", "object"])
    assert status == 400 and body["error"] == "invalid_body"


def test_override_header_bypasses_and_ledgers(running_daemon):
    root, port = running_daemon
    world = root / "world"
    _seed(world, _asp_025())
    status, body = _complete(port, "asp-025",
                             headers={"X-Mind-Override-Supply-Close": "wiring test: keep the refusal on record"})
    assert status == 200, body
    assert any("aspiration-supply-close-gate: refusal overridden" in w for w in body.get("warnings") or []), body
    rows = _read_jsonl(world / "aspiration-supply-overrides.jsonl")
    assert len(rows) == 1 and rows[0]["gate"] == "aspiration-supply-close-gate"
    assert rows[0]["kind"] == "close" and rows[0]["asp_id"] == "asp-025"
    assert rows[0]["failures"] == ["needle_unaddressed"]


def test_bulk_override_fans_into_the_close_slot(running_daemon):
    root, port = running_daemon
    world = root / "world"
    _seed(world, _asp_025())
    status, body = _complete(port, "asp-025", headers={"X-Mind-Override-All": "bulk: wiring test"})
    assert status == 200, body
    bulk = _read_jsonl(world / "override-bypass-ledger.jsonl")
    assert bulk and "override_supply_close" in bulk[-1]["slots_filled"]
    assert "aspiration-supply-close-gate" in bulk[-1].get("gate_ids", [])
    assert bulk[-1]["context"]["caller"] == "aspirations_write.py:complete"


def test_user_directed_record_without_a_needle_is_unaffected(running_daemon):
    root, port = running_daemon
    world = root / "world"
    asp = _asp_025("asp-031")
    asp["origin_signal"] = "user_directive"
    del asp["supply_evidence"]
    _seed(world, asp)
    status, body = _complete(port, "asp-031")
    assert status == 200, body
    assert "needle_satisfaction" not in body["aspiration"]


def test_complete_intent_rationale_must_address_the_needle(running_daemon):
    root, port = running_daemon
    _ensure_intent_config(root)
    world = root / "world"
    goals = [
        {"id": "g-025-01", "title": "Compile scouting database sources", "status": "completed",
         "recurring": False, "verification": {"outcomes": ["sources listed"]}},
        {"id": "g-025-02", "title": "Document defensive tendencies per source", "status": "completed",
         "recurring": False, "verification": {"outcomes": ["tendencies documented"]}},
        {"id": "g-025-03", "title": "Leftover", "status": "pending", "recurring": False},
    ]
    _seed(world, _asp_025(goals=goals))
    block = {"evidence_goal_ids": ["g-025-01", "g-025-02"], "superseded_goal_ids": ["g-025-03"],
             "rationale": ("The scouting database of sources for the coaching workflow is compiled "
                           "and validated across every provider we track.")}
    status, body = _post(port, "/v1/aspirations/complete-intent", {"asp_id": "asp-025", "source": "world"},
                         body=block)
    assert status == 400, body
    assert body["error"] == "aspiration_needle_unmet"
    assert [f["check"] for f in body["gate_output"]["failures"]] == ["needle_statement_disjoint"]
    assert len(_read_jsonl(world / "aspirations.jsonl")) == 1

    # the intent validation checks the rationale against the MOTIVATION; the
    # closure gate checks it against the NEEDLE — an honest rationale meets both
    block["rationale"] = ("The scouting database of sources for the coaching workflow now yields weekly "
                          "matchup-specific start/sit recommendations backed by the documented defensive "
                          "weakness analysis per source.")
    status, body = _post(port, "/v1/aspirations/complete-intent", {"asp_id": "asp-025", "source": "world"},
                         body=block)
    assert status == 200, body
    assert body["aspiration"]["intent_satisfaction"]["claimed_at"]
    assert "needle_satisfaction" not in body["aspiration"]
