"""Behaviour + CLI-parity tests for the aspiration-supply gate (g-357-82 / g-357-83).
# domain-leak-exempt: the replay fixture is a real deployment's portfolio (fantasy-football coaching world, 2026-09-02) — the gate must be pinned against the records that motivated it, not a paraphrase.

The portfolio below is what one deployment's idle path produced in six days:
nine self-generated aspirations restating one idea, three of them stamped
`user_directive` by the replacement lane. Every one must be refused by the
gate when evaluated against the portfolio it was filed into; a well-formed
candidate carrying supply_evidence must pass; a user-directed aspiration must
not be gated at all.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "core" / "scripts"
CLI = SCRIPTS_DIR / "aspiration-supply-gate.py"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from gates.aspiration_supply import (  # noqa: E402
    DEFAULT_CONFIG, LEDGER_NAME, containment, evaluate, is_gated, tokens,
    verify_referent,
)

# --- Replay fixture -------------------------------------------------------------

PORTFOLIO = [
    {"id": "asp-005", "title": "In-Season Fantasy Football Operations",
     "motivation": "Build operational decision-making systems that put the data infrastructure (asp-004) to work during actual fantasy football seasons — waiver wire prioritization engines, matchup analysis scoring, and lineup optimization tools that transform raw data into weekly GM decisions.",
     "origin_signal": "idle_fallback", "status": "retired"},
    {"id": "asp-006", "title": "Yahoo Fantasy Sports API: OAuth, client, resource scripts, adapters and skills (built offline, tested live at the end)",
     "motivation": "The waiver engine, lineup optimizer and matchup calculator (asp-004/asp-005) run on sample data. To actually manage a Yahoo league they need a real feed: league settings, rosters, free agents, transactions, scoreboard, and the ability to submit roster moves and waiver claims.",
     "origin_signal": "user_directive", "status": "active",
     "goals": [{"id": "g-006-15"}, {"id": "g-006-22"}]},
    {"id": "asp-007", "title": "Coaching intelligence: long-horizon decision skills that learn from their own calls (waivers, trades, hypotheses, odds, planning, calibration)",
     "motivation": "asp-006 delivers the Yahoo feed and the five report skills. What a manager actually needs next are DECISIONS with a memory: which waiver claim to make and why, whether a trade offer is good, who to stream, what the playoff picture is, and every recommendation filed as a hypothesis and scored the following week.",
     "origin_signal": "user_directive", "status": "completed"},
    {"id": "asp-011", "title": "Build Research Foundation — Player Rankings, Strategy Playbooks, and Knowledge Encoding",
     "motivation": "The agent is research-driven but currently blocked on API access. Meanwhile, the knowledge tree is at 0.15 maturity and most content is stub files. Generate the substantive research and analysis work that produces real learning artifacts — player rankings with evidence, draft/trade/injury strategy frameworks, and position-tier analysis.",
     "origin_signal": None, "status": "completed"},
    {"id": "asp-012", "title": "Coach the operator's 2026 Yahoo Fantasy league — roster, research, weekly start/sit",
     "motivation": "Zak delivered a complete 2026 roster (12-team full PPR) and wants coaching starting Week 1. Yahoo API approval pending, so starting with roster encoding and live research.",
     "origin_signal": "user_directive", "status": "active",
     "goals": [{"id": "g-012-07"}, {"id": "g-012-11"}, {"id": "g-012-12"}]},
    {"id": "asp-013", "title": "Pre-Draft Strategy & Position Value Analysis",
     "motivation": "Build the analytical foundation for draft decisions — position value curves, ADP tiers, team depth chart analysis — without requiring Yahoo API access. This advances asp-012 (coaching) with independent work while asp-006 (Yahoo API) remains blocked.",
     "origin_signal": "all_blocked_gap", "status": "retired"},
    {"id": "asp-014", "title": "Waiver Wire Strategy & Weekly Research Framework",
     "motivation": "Build the research framework for waiver wire prioritization and weekly player news tracking using publicly available web sources (ESPN, NFL.com, etc.) instead of live Yahoo API data. This enables in-season coaching without API access and serves as a training dataset for the waiver engine once API is authorized.",
     "origin_signal": "all_blocked_gap", "status": "active",
     "goals": [{"id": "g-014-01"}, {"id": "g-014-03"}]},
    {"id": "asp-018", "title": "Waiver Wire Strategy Research & Player Valuation Framework",
     "motivation": "Extend the completed waiver wire framework (asp-014) with deep research on player valuation, scarcity-adjusted replacement-level analysis, and lineup optimization strategies that provide immediate value independent of Yahoo API access.",
     "origin_signal": "aspirations-all-blocked", "status": "completed"},
    {"id": "asp-019", "title": "Operational Waiver Wire Monitoring System",
     "motivation": "Replace completed Waiver Wire Strategy Research & Player Valuation Framework (asp-018) with an operational waiver wire monitoring system.",
     "origin_signal": "user_directive", "status": "active", "goals": [{"id": "g-019-01"}]},
    {"id": "asp-020", "title": "In-Season Operations Playbook",
     "motivation": "Replace completed In-Season Operations framework with an ongoing in-season operations playbook covering waiver wire, matchup analysis, and lineup optimization.",
     "origin_signal": "user_directive", "status": "retired"},
    {"id": "asp-021", "title": "Week 1 Opponent Scouting Reports & Roster Analysis",
     "motivation": "Replace completed Week 1 Coaching Deep-Dive with ongoing weekly opponent scouting reports and roster analysis for each week of the season.",
     "origin_signal": "user_directive", "status": "completed"},
    {"id": "asp-024", "title": "Pre-coaching analytical frameworks: build player evaluation rubrics and matchup templates from public data sources",
     "motivation": "Yahoo API is human-blocked. Build reusable coaching artifacts using public data (ESPN, NFL.com, PFR) that become immediately actionable when API access is restored. Focus on player evaluation frameworks, matchup analysis templates, and waiver prioritization matrices that don't require live API access.",
     "origin_signal": "all_blocked_gap", "status": "active",
     "goals": [{"id": "g-024-01", "title": "Design implementation plan for pre-coaching analytical frameworks and file sub-goals"},
               {"id": "g-024-02", "title": "Populate Player Evaluation Rubric with Week 1 roster data and calibrate position weightings"}]},
]

SELF_GENERATED = ["asp-005", "asp-013", "asp-014", "asp-018", "asp-019", "asp-020", "asp-021", "asp-024"]


def _portfolio_without(asp_id: str):
    return [dict(e) for e in PORTFOLIO if e["id"] != asp_id]


def _record(asp_id: str):
    return dict(next(e for e in PORTFOLIO if e["id"] == asp_id))


def _control():
    return {
        "title": "Trade evaluator: score an incoming trade offer against rest-of-season projections and the user's roster needs",
        "motivation": "The user receives trade offers and has no way to get a scored recommendation; the tree holds valuation nodes but no offer-scoring capability.",
        "origin_signal": "all_blocked_gap",
        "supply_evidence": {
            "gap": "No skill or script scores a two-sided trade offer; g-012-11 covers lineup only and asp-007 closed without a trade evaluator.",
            "needle": "The user can paste a trade offer and get accept/decline with the projected point delta for the rest of the season.",
            "checked": ["asp-007", "asp-012", "g-012-11"],
        },
        "goals": [{"title": "Score a trade offer: rest-of-season projection delta per side"}],
    }


# --- World fixture for referent verification ------------------------------------

@pytest.fixture
def world(tmp_path: Path):
    wd = tmp_path / "world"
    (wd / "board").mkdir(parents=True)
    (wd / "knowledge" / "tree").mkdir(parents=True)
    (wd / "conventions").mkdir()
    live = [e for e in PORTFOLIO if e.get("status") == "active"]
    arch = [e for e in PORTFOLIO if e.get("status") != "active"]
    (wd / "aspirations.jsonl").write_text("".join(json.dumps(e) + "\n" for e in live), encoding="utf-8")
    (wd / "aspirations-archive.jsonl").write_text("".join(json.dumps(e) + "\n" for e in arch), encoding="utf-8")
    (wd / "board" / "coordination.jsonl").write_text(
        json.dumps({"id": "msg-20260902-195616-alpha-340", "text": "x"}) + "\n", encoding="utf-8")
    (wd / "reasoning-bank.jsonl").write_text(json.dumps({"id": "rb-7", "summary": "x"}) + "\n", encoding="utf-8")
    (wd / "guardrails.jsonl").write_text(json.dumps({"id": "guard-3", "rule": "x"}) + "\n", encoding="utf-8")
    (wd / "pipeline.jsonl").write_text(json.dumps({"id": "2026-08-31_tiered-target-actionability"}) + "\n", encoding="utf-8")
    (wd / "knowledge" / "tree" / "_tree.yaml").write_text(
        "nodes:\n  matchup-analysis-scoring-engine:\n    title: x\n  waiver-wire-framework:\n    title: y\n", encoding="utf-8")
    (wd / "conventions" / "capability-routing.md").write_text("# x\n", encoding="utf-8")
    ad = tmp_path / "agents" / "coach"
    (ad / "session").mkdir(parents=True)
    (ad / "session" / "pending-questions.yaml").write_text("- id: pq-scoring-layout\n  status: pending\n", encoding="utf-8")
    meta = tmp_path / "meta"
    meta.mkdir()
    return {"world": wd, "agent": ad, "meta": meta, "root": tmp_path}


def _eval(cand, existing, w=None, **kw):
    kwargs = dict(existing=existing, agent_name="coach")
    if w is not None:
        from gates.aspiration_supply import load_tree_keys
        kwargs.update(world_dir=w["world"], meta_dir=w["meta"], project_root=w["root"],
                      agent_dir=w["agent"], tree_keys=load_tree_keys(w["world"]))
    kwargs.update(kw)
    return evaluate(cand, **kwargs)


# --- Tests ----------------------------------------------------------------------

def test_user_directive_is_not_gated():
    r = _eval(_record("asp-012"), _portfolio_without("asp-012"))
    assert r["gated"] is False and r["would_block"] is False
    assert r["failures"] == []


@pytest.mark.parametrize("asp_id", SELF_GENERATED)
def test_replay_refuses_every_self_generated_record(asp_id):
    """Each manufactured aspiration, evaluated against the portfolio it was
    filed into, is refused — and refused for a NAMED reason, not a generic one."""
    r = _eval(_record(asp_id), _portfolio_without(asp_id))
    assert r["gated"] is True, asp_id
    assert r["would_block"] is True, asp_id
    checks = {f["check"] for f in r["failures"]}
    assert "supply_evidence_missing" in checks
    assert r["remedy"] and "supply_evidence" in r["remedy"]


def test_asp024_is_refused_as_blocker_restated_as_gap():
    r = _eval(_record("asp-024"), _portfolio_without("asp-024"))
    checks = {f["check"] for f in r["failures"]}
    assert "blocker_as_gap" in checks
    detail = next(f["detail"] for f in r["failures"] if f["check"] == "blocker_as_gap")
    assert "human-blocked" in detail
    # its nearest neighbour is the same idea filed two days earlier
    assert r["overlaps"][0]["id"] == "asp-014"


@pytest.mark.parametrize("asp_id,twin", [("asp-019", "asp-018"), ("asp-020", "asp-005")])
def test_replacement_lane_under_false_user_directive_is_caught(asp_id, twin):
    r = _eval(_record(asp_id), _portfolio_without(asp_id))
    checks = {f["check"] for f in r["failures"]}
    assert "origin_misattributed" in checks
    assert "overlaps_archived" in checks
    top = r["overlaps"][0]
    assert top["id"] == twin and top["containment"] >= DEFAULT_CONFIG["overlap_threshold"]


def test_overlap_with_active_aspiration_routes_to_goals_under_it():
    r = _eval(_record("asp-018"), _portfolio_without("asp-018"))
    f = next(f for f in r["failures"] if f["check"] == "overlaps_active")
    assert "asp-014" in f["detail"] and "file goals under asp-014" in f["detail"]


def test_positive_control_passes_with_real_referents():
    r = _eval(_control(), PORTFOLIO)
    assert r["gated"] is True
    assert r["would_block"] is False, r["failures"]
    assert r["failures"] == []
    assert {x["ref"] for x in r["checks"]["referents"] if x["verified"]} == {"asp-007", "asp-012", "g-012-11"}


def test_building_on_completed_work_is_allowed_only_when_cited():
    cand = _record("asp-019")
    cand["origin_signal"] = "successor:asp-018"
    cand["supply_evidence"] = {
        "gap": "asp-018 delivered the valuation rubric but no weekly run of it; nothing produces a ranked waiver list each Tuesday.",
        "needle": "Every Tuesday the user gets a ranked waiver list with one recommended claim.",
        "checked": ["asp-018", "asp-014"],
    }
    r = _eval(cand, _portfolio_without("asp-019"))
    checks = {f["check"] for f in r["failures"]}
    assert "overlaps_archived" not in checks
    # still overlaps ACTIVE asp-014 at >= threshold -> file goals there instead
    assert "overlaps_active" in checks


def test_unblock_aspiration_may_name_the_blocker_but_still_owes_referents():
    cand = {
        "title": "Unblock: dependency class — 3 goals wait on a fixture nobody owns",
        "motivation": "g-006-15 and g-006-22 are blocked on the same missing fixture; no outstanding Unblock covers it.",
        "origin_signal": "blocker_pattern:dependency",
        "supply_evidence": {
            "gap": "No goal or aspiration owns producing the shared fixture the dependency class waits on.",
            "needle": "The three waiting goals become executable in the next iteration.",
            "checked": ["g-006-15", "g-006-22"],
        },
    }
    r = _eval(cand, PORTFOLIO)
    assert r["gated"] and not r["would_block"], r["failures"]
    cand["supply_evidence"]["checked"] = ["dependency"]
    r2 = _eval(cand, PORTFOLIO)
    assert {f["check"] for f in r2["failures"]} == {"referents_unverified"}


def test_tiny_candidate_overlap_is_advisory_only():
    cand = {"title": "smoke: gated empty evidence", "origin_signal": "all_blocked_gap",
            "motivation": "Framework work meanwhile.",
            "supply_evidence": {"gap": "x" * 50, "needle": "y" * 40, "checked": ["asp-012", "asp-006"]}}
    existing = [{"id": "asp-900", "title": "smoke framework evidence", "motivation": "", "status": "retired"}]
    r = _eval(cand, existing + PORTFOLIO)
    assert r["overlaps"][0]["containment"] >= DEFAULT_CONFIG["overlap_threshold"]
    assert "overlaps_archived" not in {f["check"] for f in r["failures"]}


def test_daily_cap_counts_only_this_agents_self_generated_today():
    today = datetime.now().strftime("%Y-%m-%dT10:00:00")
    filled = PORTFOLIO + [
        {"id": "asp-901", "title": "a1", "origin_signal": "all_blocked_gap", "status": "active",
         "created_at": today, "created_by_agent": "coach"},
        {"id": "asp-902", "title": "a2", "origin_signal": "idle_fallback", "status": "active",
         "created_at": today, "created_by_agent": "coach"},
        {"id": "asp-903", "title": "a3", "origin_signal": "all_blocked_gap", "status": "active",
         "created_at": today, "created_by_agent": "other-agent"},
        {"id": "asp-904", "title": "a4", "origin_signal": "user_directive", "status": "active",
         "created_at": today, "created_by_agent": "coach"},
    ]
    r = _eval(_control(), filled)
    assert r["checks"]["self_generated_today"] == 2
    assert "daily_cap" in {f["check"] for f in r["failures"]}
    r2 = _eval(_control(), filled, agent_name="other-agent")
    assert r2["checks"]["self_generated_today"] == 1
    assert "daily_cap" not in {f["check"] for f in r2["failures"]}
    r3 = _eval(_control(), filled, config={"max_self_generated_per_agent_per_day": 0})
    assert "daily_cap" not in {f["check"] for f in r3["failures"]}


def test_override_bypasses_and_writes_ledger(world):
    r = _eval(_record("asp-024"), _portfolio_without("asp-024"), world,
              override_supply="operator replay: keep the record for the teaching test")
    assert r["would_block"] is False
    assert r["override_applied"].startswith("operator replay")
    assert r["failures"]  # the refusal is still reported
    ledger = (world["world"] / LEDGER_NAME).read_text(encoding="utf-8").strip().splitlines()
    assert len(ledger) == 1
    rec = json.loads(ledger[0])
    assert rec["gate"] == "aspiration-supply-gate" and "blocker_as_gap" in rec["failures"]


def test_referent_kinds_are_verified_against_the_stores(world):
    from gates.aspiration_supply import load_tree_keys
    ids = {"asp-012", "g-012-11"}
    kw = dict(existing_ids=ids, tree_keys=load_tree_keys(world["world"]),
              world_dir=world["world"], meta_dir=world["meta"],
              project_root=world["root"], agent_dir=world["agent"])
    ok = ["asp-012", "g-012-11", "msg-20260902-195616-alpha-340", "rb-7", "guard-3",
          "2026-08-31_tiered-target-actionability", "pq-scoring-layout",
          "world/conventions/capability-routing.md", "matchup-analysis-scoring-engine"]
    for ref in ok:
        v = verify_referent(ref, **kw)
        assert v["verified"], (ref, v)
    bad = ["asp-999", "g-999-01", "msg-20260101-000000-nobody-1", "rb-999", "guard-999",
           "2020-01-01_nope", "pq-missing", "world/conventions/missing.md", "no-such-node",
           "https://example.invalid/page", "research", 42]
    for ref in bad:
        v = verify_referent(ref, **kw)
        assert not v["verified"], (ref, v)
    assert verify_referent("https://example.invalid/page", **kw)["kind"] == "url"
    # a bare category label reads as a would-be tree key and fails existence —
    # never as a verified referent
    assert verify_referent("research", **kw)["kind"] in ("tree_node", "unrecognized")
    assert verify_referent("Research coverage", **kw)["kind"] == "unrecognized"


def test_gating_matches_every_spelling_of_the_idle_lane():
    spellings = ["all_blocked_gap", "all-blocked", "all-blocked: create-aspiration from-self (x)",
                 "all-blocked-constraint-generation", "aspirations-all-blocked",
                 "idea:b2-constraint-aware-aspiration-generation", "idle_fallback",
                 "blocker_pattern:dependency", "successor:asp-018"]
    for s in spellings:
        assert is_gated({"origin_signal": s, "motivation": "x"}, DEFAULT_CONFIG)["gated"], s
    for s in ["user_directive", "board_post:msg-1", "pending_question:pq-1", "", None]:
        assert not is_gated({"origin_signal": s, "motivation": "Do the thing the user asked."}, DEFAULT_CONFIG)["gated"], s
    g = is_gated({"origin_signal": "user_directive", "motivation": "Replace completed X with Y."}, DEFAULT_CONFIG)
    assert g["gated"] and g["successor_shaped"] and g["origin_misattributed"]


def test_tokenizer_and_containment_are_symmetric_on_stems():
    a = tokens("Player evaluation frameworks and matchup templates")
    b = tokens("player evaluation framework; matchup template")
    assert a == b
    assert containment(a, b) == 1.0
    assert containment(set(), b) == 0.0


def test_cli_parity_with_module(world):
    env = os.environ.copy()
    env.update({"MIND_WORLD": str(world["world"]), "MIND_META": str(world["meta"]),
                "STORAGE_BACKEND": "local"})
    env.pop("MIND_AGENT", None)
    cand = _record("asp-024")
    proc = subprocess.run([sys.executable, str(CLI), "--output", "json"], input=json.dumps(cand),
                          env=env, capture_output=True, text=True, check=False)
    assert proc.returncode == 1, proc.stderr
    out = json.loads(proc.stdout)
    # the CLI reads the seeded world, where asp-024 itself is live — so the
    # module comparison uses the full portfolio too
    mod = evaluate(cand, existing=PORTFOLIO, world_dir=world["world"])
    assert out["would_block"] is True
    assert [f["check"] for f in out["failures"]] == [f["check"] for f in mod["failures"]]
    ctrl = _control()
    proc2 = subprocess.run([sys.executable, str(CLI), "--output", "human"], input=json.dumps(ctrl),
                           env=env, capture_output=True, text=True, check=False)
    assert proc2.returncode == 0, proc2.stdout + proc2.stderr
    assert "PASS" in proc2.stdout
    proc3 = subprocess.run([sys.executable, str(CLI)], input="not json", env=env,
                           capture_output=True, text=True, check=False)
    assert proc3.returncode == 2
