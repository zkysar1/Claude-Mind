"""test_goal_selector_weights_contract.py —  regression guards.

Three-part fix under test:
  (1) opportunity_boost criterion restored (the rb-498-era promotion clobbered
      the prod-side original; this pins the dev-origin restore).
  (2) score_goal hardened against the orphaned-weight crash class: a meta
      weight naming a criterion the code does not compute must degrade to a
      loud warning + opt-out, never a KeyError that kills selection.
  (3) promotion-preflight weights-contract cross-check: seed template and
      reachable target metas are validated against the selector's
      KNOWN_CRITERIA manifest (AST-parsed, no import).

Import pattern mirrors test_goal_selector_deadline_urgency.py.
"""

from __future__ import annotations

import ast
import importlib
import os
import re
import sys
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
REPO_ROOT = CORE_SCRIPTS.parent.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "bravo")

gs = importlib.import_module("goal-selector")
pp = importlib.import_module("promotion-preflight")

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT

SELECTOR_SRC = (CORE_SCRIPTS / "goal-selector.py").read_text(encoding="utf-8")


# ── (contract) KNOWN_CRITERIA == raw keys score_goal computes ─────────────

def test_known_criteria_matches_score_goal_raw_keys():
    """The code-side manifest must equal the raw keys score_goal assigns
    (minus exploration_noise, whose weight is dynamic). A criterion added to
    score_goal without a manifest entry silently opts every deployment out of
    it (load_weights drops the 'unknown' weight); a manifest entry without a
    criterion re-opens a raw.get(...)=0 dead weight. Keep the two in lockstep."""
    m = re.search(r"def score_goal.*?(?=\ndef )", SELECTOR_SRC, re.S)
    assert m, "score_goal body not found"
    raw_keys = set(re.findall(r'raw\["([a-z_]+)"\]\s*(?:=|\+=)', m.group(0)))
    assert "exploration_noise" in raw_keys
    raw_keys.discard("exploration_noise")
    assert raw_keys == set(gs.KNOWN_CRITERIA), (
        f"manifest drift — in code not manifest: {sorted(raw_keys - set(gs.KNOWN_CRITERIA))}; "
        f"in manifest not code: {sorted(set(gs.KNOWN_CRITERIA) - raw_keys)}"
    )


def test_opportunity_boost_in_manifest_and_seed():
    """The  restore itself: criterion in the manifest AND seeded."""
    assert "opportunity_boost" in gs.KNOWN_CRITERIA
    seed = yaml.safe_load((REPO_ROOT / "core/config/meta.yaml").read_text(encoding="utf-8"))
    w = seed["initial_state"]["goal_selection_strategy"]["weights"]
    assert "opportunity_boost" in w


def test_seed_template_weights_subset_of_known():
    """Every seed-template weight must have a computed criterion — a fresh
    deployment must never seed the orphaned-weight mismatch."""
    seed = yaml.safe_load((REPO_ROOT / "core/config/meta.yaml").read_text(encoding="utf-8"))
    w = seed["initial_state"]["goal_selection_strategy"]["weights"]
    orphans = sorted(set(w) - set(gs.KNOWN_CRITERIA))
    assert orphans == [], f"seed template weights with no criterion: {orphans}"


def test_seed_template_weights_complete():
    """Every computed criterion must be SEEDED (): the sibling of
    the subset check above. Together they pin seed == KNOWN_CRITERIA. Without
    this, a criterion added to code could silently lag out of the seed for
    weeks (the seed had 19/26 before g-115-2543), so a fresh world would seed
    goal-selection with the missing criteria inert-at-0 — degraded scoring
    that the raw.get backstop hides. Pins the lag closed."""
    seed = yaml.safe_load((REPO_ROOT / "core/config/meta.yaml").read_text(encoding="utf-8"))
    w = seed["initial_state"]["goal_selection_strategy"]["weights"]
    unseeded = sorted(set(gs.KNOWN_CRITERIA) - set(w))
    assert unseeded == [], (
        f"criteria computed by score_goal but NOT in the seed template "
        f"(fresh worlds would seed them inert-at-0): {unseeded}")


# ── (crash class) load_weights filter + score_goal backstop ───────────────

def test_load_weights_drops_orphan_with_warning(tmp_path, monkeypatch, capsys):
    """A meta weights key the code does not compute is dropped loudly, and
    the known keys survive with their clamped values."""
    meta = tmp_path / "goal-selection-strategy.yaml"
    meta.write_text(
        yaml.dump({"weights": {"priority": 1.0, "ghost_criterion": 3.0}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(gs, "META_GOAL_SELECTION", meta)
    out = gs.load_weights()
    err = capsys.readouterr().err
    assert "ghost_criterion" in err and "WARNING" in err
    assert "ghost_criterion" not in out
    assert out["priority"] == 1.0


def test_score_goal_survives_orphaned_weight(monkeypatch):
    """The pre-fix crash: WEIGHTS carrying a key raw lacks raised KeyError at
    the weighted sum, killing selection fleet-wide. The raw.get backstop must
    keep score_goal returning a result (orphan contributes 0)."""
    gs._ACTIVE_DIRECTIVES = []
    poisoned = dict(gs.WEIGHTS)
    poisoned["ghost_criterion"] = 3.0
    monkeypatch.setattr(gs, "WEIGHTS", poisoned)
    cand = {
        "goal": {"id": "g-t-1", "title": "t", "status": "pending",
                 "participants": ["agent"], "priority": "MEDIUM"},
        "aspiration": {"id": "asp-t"},
        "source": "world",
    }
    result = gs.score_goal(cand, {}, [], [])
    assert result["goal_id"] == "g-t-1"
    assert result["breakdown"]["ghost_criterion"] == 0.0


# ── (restored criterion) opportunity_boost behavior ───────────────────────

def _score_raw(goal_extra):
    gs._ACTIVE_DIRECTIVES = []
    goal = {"id": "g-t-ob", "title": "t", "status": "pending",
            "participants": ["agent"], "priority": "MEDIUM"}
    goal.update(goal_extra)
    cand = {"goal": goal, "aspiration": {"id": "asp-t"}, "source": "world"}
    return gs.score_goal(cand, {}, [], [])["raw"]["opportunity_boost"]


def test_opportunity_boost_full_for_discovery_type():
    assert _score_raw({"discovery_type": "opportunity"}) == 1.0


def test_opportunity_boost_half_for_idea_signal():
    assert _score_raw({"origin_signal": "idea:some-tag"}) == 0.5
    assert _score_raw({"title": "Idea: improve the widget"}) == 0.5


def test_opportunity_boost_zero_default():
    assert _score_raw({}) == 0.0


# ── (preflight) KNOWN_CRITERIA AST parse + contract check ─────────────────

def test_preflight_parses_manifest_from_this_repo():
    parsed = pp.parse_known_criteria(REPO_ROOT)
    assert parsed == set(gs.KNOWN_CRITERIA)


def test_preflight_returns_none_without_manifest(tmp_path):
    sel_dir = tmp_path / "core" / "scripts"
    sel_dir.mkdir(parents=True)
    (sel_dir / "goal-selector.py").write_text("WEIGHTS = {}\n", encoding="utf-8")
    assert pp.parse_known_criteria(tmp_path) is None
    assert pp.parse_known_criteria(tmp_path / "absent") is None


def test_preflight_detects_target_meta_orphan(tmp_path):
    """A reachable target meta with an orphaned weight is reported (the exact
    rb-498 prod shape); the real repo's own seed must report no orphans."""
    tgt = tmp_path / "target-repo"
    conf_dir = tgt / "agents" / "tagent"
    conf_dir.mkdir(parents=True)
    meta_dir = tmp_path / "external-meta"
    meta_dir.mkdir()
    (conf_dir / "local-paths.conf").write_text(
        f"WORLD_PATH={tmp_path / 'w'}\nMETA_PATH={meta_dir}\n", encoding="utf-8")
    (meta_dir / "goal-selection-strategy.yaml").write_text(
        yaml.dump({"weights": {"priority": 1.0, "opportunity_boost": 3.0,
                               "ghost_criterion": 2.0}}),
        encoding="utf-8",
    )
    wc = pp.check_weights_contract(REPO_ROOT, tgt)
    assert wc["checked"] is True
    assert wc["seed_orphans"] == []
    checked = [e for e in wc["target_metas"] if e["status"] == "checked"]
    assert len(checked) == 1
    assert checked[0]["orphans"] == ["ghost_criterion"]


def test_preflight_unreachable_target_meta_is_informational(tmp_path):
    tgt = tmp_path / "target-repo"
    conf_dir = tgt / "agents" / "tagent"
    conf_dir.mkdir(parents=True)
    (conf_dir / "local-paths.conf").write_text(
        "META_PATH=/nonexistent/meta/path\n", encoding="utf-8")
    wc = pp.check_weights_contract(REPO_ROOT, tgt)
    assert wc["checked"] is True
    assert wc["target_metas"][0]["status"] == "unreachable"
    assert wc["target_metas"][0]["orphans"] == []
