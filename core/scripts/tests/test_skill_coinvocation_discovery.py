"""test_skill_coinvocation_discovery.py -- .

Pins the co-invocation ledger-mining logic of skill-coinvocation-discovery.py:
  - episode gap-splitting (sessions split where consecutive gap > gap_minutes)
  - per-session isolation (no cross-session pairs)
  - episode-level (NOT per-record) pair counting
  - existing-compose dedup (mirrors skill-relations.py cmd_discover)
  - Jaccard confidence math
  - end-to-end discover() against a synthetic cross-agent ledger

The hyphen-named module is loaded via importlib (the pattern proven in
test_rb_entry_type_taxonomy_sync.py / test_applies_to_required.py). Importing it
resolves _paths (WORLD/path bootstrap), so MIND_WORLD/MIND_AGENT are stashed
to a tmp dir FIRST and restored immediately after the load (guard-588: a
module-level os.environ mutation must not leak into other tests in the same
pytest session). Freezing WORLD_DIR to an empty tmp also makes the e2e test's
load_existing_compose() read an empty world (deterministic); synthetic
"coinvoc-test-*" skill names dodge the real base-config compose dedup.

Cross-references:
  - g-304-10 -- Master plan finding #13 (the build goal)
  - skill-relations.py cmd_discover -- the dedup/pair pattern reused
  - guard-588 -- module-level env stash discipline
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

# guard-588: stash env BEFORE the import bootstraps _paths, restore right after.
_ORIG_WORLD = os.environ.get("MIND_WORLD")
_ORIG_AGENT = os.environ.get("MIND_AGENT")
_TMPDIR = tempfile.mkdtemp(prefix="coinvoc-disc-test-")
os.environ["MIND_WORLD"] = _TMPDIR
os.environ.pop("MIND_AGENT", None)

_PATH = CORE_SCRIPTS / "skill-coinvocation-discovery.py"
_spec = importlib.util.spec_from_file_location("skill_coinvocation_discovery", _PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

if _ORIG_WORLD is not None:
    os.environ["MIND_WORLD"] = _ORIG_WORLD
elif "MIND_WORLD" in os.environ:
    del os.environ["MIND_WORLD"]
if _ORIG_AGENT is not None:
    os.environ["MIND_AGENT"] = _ORIG_AGENT


def _rec(ts, skill, sid="s1", agent="alpha"):
    return {"ts": ts, "skill": skill, "agent": agent, "sid": sid,
            "invocation_source": "model"}


# --- build_episodes ---------------------------------------------------------

def test_build_episodes_splits_on_gap():
    recs = [
        _rec("2026-06-19T01:00:00", "a"),
        _rec("2026-06-19T01:05:00", "b"),   # 5min gap -> same episode
        _rec("2026-06-19T02:00:00", "c"),   # 55min gap -> NEW episode
        _rec("2026-06-19T02:03:00", "d"),
    ]
    eps = _mod.build_episodes(recs, gap_minutes=15)
    assert len(eps) == 2
    assert {"a", "b"} in eps and {"c", "d"} in eps


def test_build_episodes_drops_singleton():
    # Two isolated invocations: each is a 1-skill episode -> dropped (no pairs).
    recs = [
        _rec("2026-06-19T01:00:00", "a"),
        _rec("2026-06-19T02:00:00", "b"),
    ]
    assert _mod.build_episodes(recs, gap_minutes=15) == []


def test_build_episodes_separates_sessions():
    # Same wall-clock, different sid -> distinct sessions, NO cross-session pair.
    recs = [
        _rec("2026-06-19T01:00:00", "a", sid="s1"),
        _rec("2026-06-19T01:01:00", "b", sid="s1"),
        _rec("2026-06-19T01:00:00", "c", sid="s2"),
        _rec("2026-06-19T01:01:00", "d", sid="s2"),
    ]
    eps = _mod.build_episodes(recs, gap_minutes=15)
    assert len(eps) == 2
    assert {"a", "b"} in eps and {"c", "d"} in eps
    # No {a,c} cross-session episode.
    assert not any("a" in e and "c" in e for e in eps)


# --- count_cooccurrence -----------------------------------------------------

def test_count_cooccurrence_one_vote_per_episode():
    eps = [{"a", "b"}, {"a", "b"}, {"a", "c"}]
    pc, se = _mod.count_cooccurrence(eps)
    assert pc[("a", "b")] == 2     # one vote per episode (not per record)
    assert pc[("a", "c")] == 1
    assert se["a"] == 3 and se["b"] == 2 and se["c"] == 1


# --- propose ----------------------------------------------------------------

def test_propose_dedups_existing_compose():
    pc = Counter({("a", "b"): 5})
    se = Counter({"a": 5, "b": 5})
    assert _mod.propose(pc, se, {("a", "b")}, min_co=3, top=10) == []


def test_propose_min_co_threshold():
    pc = Counter({("a", "b"): 2})
    se = Counter({"a": 2, "b": 2})
    assert _mod.propose(pc, se, set(), min_co=3, top=10) == []


def test_propose_jaccard_math():
    pc = Counter({("a", "b"): 4})
    se = Counter({"a": 6, "b": 5})
    out = _mod.propose(pc, se, set(), min_co=3, top=10)
    assert len(out) == 1
    # Jaccard = co / (eps_a + eps_b - co) = 4 / (6 + 5 - 4) = 4/7
    assert out[0]["confidence"] == round(4 / 7, 3)
    assert out[0]["co_occurrence_episodes"] == 4
    assert out[0]["type"] == "compose_with"


def test_propose_ranks_specific_above_ubiquitous():
    # ubiquitous pair: high raw count but low Jaccard; specific pair: lower count
    # but higher Jaccard -> specific must rank first.
    pc = Counter({("loop1", "loop2"): 10, ("x", "y"): 4})
    se = Counter({"loop1": 40, "loop2": 40, "x": 4, "y": 5})  # x,y nearly always together
    out = _mod.propose(pc, se, set(), min_co=3, top=10)
    assert (out[0]["source"], out[0]["target"]) == ("x", "y")


# --- discover() end-to-end --------------------------------------------------

def test_discover_end_to_end(tmp_path):
    root = tmp_path
    for ag in ("alpha", "bravo"):
        d = root / ag
        d.mkdir()
        recs = []
        for i in range(4):
            sid = "s{}".format(i)
            recs.append(_rec("2026-06-19T0{}:00:00".format(i), "coinvoc-test-x", sid=sid, agent=ag))
            recs.append(_rec("2026-06-19T0{}:02:00".format(i), "coinvoc-test-y", sid=sid, agent=ag))
        (d / "skill-invocations.jsonl").write_text(
            "\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")

    payload = _mod.discover(gap_minutes=15, min_co=3, top=10, root=root)
    assert payload["ledger_records"] == 16          # 2 agents * 4 sessions * 2 skills
    assert payload["total_episodes"] == 8           # 1 episode per (agent, sid)
    pairs = {(c["source"], c["target"]) for c in payload["candidates"]}
    assert ("coinvoc-test-x", "coinvoc-test-y") in pairs
    cand = next(c for c in payload["candidates"]
                if (c["source"], c["target"]) == ("coinvoc-test-x", "coinvoc-test-y"))
    assert cand["co_occurrence_episodes"] == 8       # co-occurs in all 8 episodes
    assert cand["confidence"] == 1.0                 # Jaccard 8/(8+8-8) = 1.0


# --- main(--apply) read-modify-write preservation () ----------------
# The pure-function tests above never exercise main(--apply), where the single
# world/skill-relations.yaml write lives. That write is a read-modify-write that
# MUST preserve co_invocation_log, forged_relations, and last_updated while
# (re)writing only co_invocation_candidates (script docstring lines 32-34).
# Verified manually at ship (); these pin it so a future refactor that
# drops a sibling key or clobbers last_updated fails loudly. WORLD_RELATIONS_PATH
# and agents_root are module globals resolved at call time, so monkeypatching
# _mod makes main() hermetic; the script's own _read_yaml/_write_yaml_atomic are
# reused for seed + readback (no extra yaml import, dodges the env-stash block).


def test_apply_rmw_preserves_sibling_keys(tmp_path, monkeypatch):
    world_file = tmp_path / "skill-relations.yaml"
    seed = {
        "forged_relations": [
            {"source": "skill-alpha", "target": "skill-beta", "type": "compose_with"},
        ],
        "co_invocation_log": [
            {"skills": ["skill-x", "skill-y"], "goal_id": "g-test-01",
             "ts": "2026-06-18T10:00:00"},
        ],
        "last_updated": "2026-06-18T12:00:00",
        # a stale candidates block the --apply run MUST replace wholesale
        "co_invocation_candidates": {"candidate_count": 999, "candidates": []},
    }
    _mod._write_yaml_atomic(world_file, seed)
    monkeypatch.setattr(_mod, "WORLD_RELATIONS_PATH", world_file)

    # Hermetic ledger: a temp agents root with a deterministic co-occurring pair
    # (4 episodes >= default min_co; synthetic names dodge real base-config dedup).
    agents_dir = tmp_path / "agents"
    alpha = agents_dir / "alpha"
    alpha.mkdir(parents=True)
    recs = []
    for i in range(4):
        sid = "s{}".format(i)
        recs.append(_rec("2026-06-19T0{}:00:00".format(i), "coinvoc-rmw-x", sid=sid))
        recs.append(_rec("2026-06-19T0{}:02:00".format(i), "coinvoc-rmw-y", sid=sid))
    (alpha / "skill-invocations.jsonl").write_text(
        "\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    monkeypatch.setattr(_mod, "agents_root", lambda: agents_dir)

    rc = _mod.main(["--apply", "--min-co-occurrences", "3", "--gap-minutes", "15",
                    "--top", "10", "--output", "json"])
    assert rc == 0

    after = _mod._read_yaml(world_file)
    # The three sibling keys survived the RMW unchanged.
    assert after["forged_relations"] == seed["forged_relations"]
    assert after["co_invocation_log"] == seed["co_invocation_log"]
    assert after["last_updated"] == seed["last_updated"]
    # The candidates block was (re)written, replacing the stale 999-count seed.
    assert "co_invocation_candidates" in after
    assert after["co_invocation_candidates"]["candidate_count"] != 999
    pairs = {(c["source"], c["target"])
             for c in after["co_invocation_candidates"]["candidates"]}
    assert ("coinvoc-rmw-x", "coinvoc-rmw-y") in pairs


def test_apply_creates_file_when_world_absent(tmp_path, monkeypatch):
    # The other RMW branch: when world/skill-relations.yaml does not yet exist,
    # _read_yaml returns {} -> data={} -> a fresh file with just the candidates
    # key lands (main lines 282-286). Must not crash on the missing-file path.
    world_file = tmp_path / "skill-relations.yaml"   # intentionally NOT created
    monkeypatch.setattr(_mod, "WORLD_RELATIONS_PATH", world_file)
    agents_dir = tmp_path / "agents"
    alpha = agents_dir / "alpha"
    alpha.mkdir(parents=True)
    recs = []
    for i in range(4):
        sid = "s{}".format(i)
        recs.append(_rec("2026-06-19T0{}:00:00".format(i), "coinvoc-new-x", sid=sid))
        recs.append(_rec("2026-06-19T0{}:02:00".format(i), "coinvoc-new-y", sid=sid))
    (alpha / "skill-invocations.jsonl").write_text(
        "\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    monkeypatch.setattr(_mod, "agents_root", lambda: agents_dir)

    rc = _mod.main(["--apply", "--min-co-occurrences", "3", "--output", "json"])
    assert rc == 0
    assert world_file.exists()
    after = _mod._read_yaml(world_file)
    assert "co_invocation_candidates" in after


if __name__ == "__main__":
    import subprocess
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-v"]))
