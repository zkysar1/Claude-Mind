"""Tests for collective_belief.py (Phase 5 — collective-level interpretability)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collective_belief as cbf  # noqa: E402


def _v(agent, drive="", tags=(), asps=()):
    return cbf.AgentView.from_dict({"agent": agent, "primary_drive": drive,
                                    "focus_tags": list(tags), "aspirations": list(asps)})


def test_from_dict_normalizes_tags_and_requires_agent():
    v = cbf.AgentView.from_dict({"agent": "alpha", "focus_tags": ["NPC ", "npc", " Backend"]})
    assert v.focus_tags == frozenset({"npc", "backend"})  # lowercased, stripped, deduped
    with pytest.raises(ValueError, match="missing 'agent'"):
        cbf.AgentView.from_dict({"focus_tags": ["x"]})


def test_roster_and_focus_distribution():
    views = [_v("alpha", "backend dev", ["backend", "infra"]),
             _v("delta", "client dev", ["client", "backend"])]
    cv = cbf.collective_view(views)
    assert cv["n_agents"] == 2
    assert cv["roster"] == {"alpha": "backend dev", "delta": "client dev"}
    assert cv["focus_distribution"]["backend"] == ["alpha", "delta"]
    assert cv["focus_distribution"]["infra"] == ["alpha"]


def test_shared_focus_and_overlaps():
    views = [_v("alpha", tags=["backend", "infra"]),
             _v("delta", tags=["client", "backend"]),
             _v("zeta", tags=["analysis"])]
    cv = cbf.collective_view(views)
    assert cv["shared_focus"] == ["backend"]  # only backend held by >=2
    assert cv["overlaps"] == [{"agents": ["alpha", "delta"], "shared": ["backend"]}]


def test_siloed_agent_detected():
    views = [_v("alpha", tags=["backend"]),
             _v("delta", tags=["backend"]),
             _v("echo", tags=["arc-agi"])]  # shares with nobody
    cv = cbf.collective_view(views)
    assert cv["siloed"] == ["echo"]


def test_agent_with_no_tags_is_not_siloed():
    # No focus tags = no signal, not a silo (avoid false-flagging a tag-less agent).
    views = [_v("alpha", tags=["backend"]), _v("bravo", tags=[])]
    assert cbf.collective_view(views)["siloed"] == []


def test_single_agent_has_no_overlaps_or_silos():
    cv = cbf.collective_view([_v("alpha", tags=["backend"])])
    assert cv["overlaps"] == [] and cv["siloed"] == []


def test_duplicate_agents_rejected():
    with pytest.raises(ValueError, match="duplicate agents"):
        cbf.collective_view([_v("alpha"), _v("alpha")])


def test_load_views_jsonl_and_cli(tmp_path, capsys):
    p = tmp_path / "views.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in [
        {"agent": "alpha", "focus_tags": ["backend"]},
        {"agent": "delta", "focus_tags": ["backend", "client"]},
    ]), encoding="utf-8")
    rc = cbf.main(["--views", str(p)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["shared_focus"] == ["backend"]
    assert out["overlaps"][0]["agents"] == ["alpha", "delta"]
