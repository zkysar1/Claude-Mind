"""Daemon wiring for the aspiration-supply gate ( / ).

Verifies the add endpoint CALLS gates.aspiration_supply.evaluate with the
right inputs and HONORS its verdict: a self-generated aspiration without
supply_evidence is refused with a structured 400; X-Mind-Override-Supply
(and X-Mind-Override-All) let it through and audit; a user-directed
aspiration is never gated; the stored record carries created_at and
created_by_agent so the daily cap can count it next time.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def _post(port, path, query, body, *, agent="alpha", headers=None):
    qs = urllib.parse.urlencode(query)
    url = f"http://127.0.0.1:{port}{path}?{qs}"
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST")
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


def _manufactured():
    return {
        "title": "Pre-coaching analytical frameworks from public data while the feed is blocked",
        "motivation": "The upstream feed is human-blocked. Build reusable frameworks that become actionable when access is restored.",
        "priority": "MEDIUM", "status": "active", "scope": "project",
        "origin_signal": "all_blocked_gap",
        "goals": [],
    }


def _add(port, asp, **kw):
    return _post(port, "/v1/aspirations/add", {"source": "world"}, asp, **kw)


def test_self_generated_without_evidence_is_refused(running_daemon):
    root, port = running_daemon
    before = len(_read_jsonl(root / "world" / "aspirations.jsonl"))
    status, body = _add(port, _manufactured())
    assert status == 400, body
    assert body["error"] == "aspiration_supply_blocked"
    assert body["gate"] == "aspiration-supply-gate"
    checks = {f["check"] for f in body["gate_output"]["failures"]}
    assert {"supply_evidence_missing", "blocker_as_gap"} <= checks
    assert body["gate_output"]["remedy"]
    assert len(_read_jsonl(root / "world" / "aspirations.jsonl")) == before


def test_user_directive_is_not_gated(running_daemon):
    root, port = running_daemon
    asp = _manufactured()
    asp["origin_signal"] = "user_directive"
    asp["motivation"] = "The operator asked for this in chat today."
    status, body = _add(port, asp)
    assert status == 200, body
    rec = next(a for a in _read_jsonl(root / "world" / "aspirations.jsonl") if a["id"] == body["aspiration_id"])
    assert rec["created_at"] and rec["created_by_agent"] == "alpha"


def test_override_supply_header_bypasses_and_audits(running_daemon):
    root, port = running_daemon
    status, body = _add(port, _manufactured(),
                        headers={"X-Mind-Override-Supply": "wiring test: keep the refusal on record"})
    assert status == 200, body
    assert any("aspiration-supply-gate: refusal overridden" in w for w in body.get("warnings", [])), body
    ledger = _read_jsonl(root / "world" / "aspiration-supply-overrides.jsonl")
    assert len(ledger) == 1 and ledger[0]["justification"].startswith("wiring test")
    assert "blocker_as_gap" in ledger[0]["failures"]


def test_bulk_override_fans_into_the_supply_slot(running_daemon):
    root, port = running_daemon
    status, body = _add(port, _manufactured(), headers={"X-Mind-Override-All": "bulk: wiring test"})
    assert status == 200, body
    bulk = _read_jsonl(root / "world" / "override-bypass-ledger.jsonl")
    assert bulk and "override_supply" in bulk[-1]["slots_filled"]
    assert "aspiration-supply-gate" in bulk[-1].get("gate_ids", [])


def test_well_formed_self_generated_aspiration_lands(running_daemon):
    root, port = running_daemon
    live = _read_jsonl(root / "world" / "aspirations.jsonl")
    existing_id = live[0]["id"]
    asp = {
        "title": "Nightly digest of unanswered questions with one recommended action each",
        "motivation": "Open questions age silently; the store shows several older than a day with no follow-up path.",
        "priority": "MEDIUM", "status": "active", "scope": "sprint",
        "origin_signal": "all_blocked_gap",
        "supply_evidence": {
            "gap": "No cadence reads the pending-question store and produces a digest; the existing aspiration only files questions.",
            "needle": "Each morning the user sees the open questions with one suggested default action per question.",
            "checked": [existing_id, "world/aspirations.jsonl"],
        },
        "goals": [],
    }
    status, body = _add(port, asp)
    assert status == 200, body
    rec = next(a for a in _read_jsonl(root / "world" / "aspirations.jsonl") if a["id"] == body["aspiration_id"])
    assert rec["supply_evidence"]["checked"][0] == existing_id
    assert rec["created_by_agent"] == "alpha"
