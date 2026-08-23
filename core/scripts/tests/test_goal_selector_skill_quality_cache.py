"""test_goal_selector_skill_quality_cache.py — selection-stack review fix 1.

skill_affinity (score_goal criterion 12) used to call
read_yaml_file(SKILL_QUALITY_PATH) once per CANDIDATE. Measured 2026-08-21
(alpha, cc-09, 1,335 candidates): 27.9ms of the 29.9ms per-goal scoring cost
— ~37s of a ~40s selector invocation re-parsing one unchanged ~18KB file.
_load_skill_quality_cached() memoizes one parse per invocation, keyed by
str(SKILL_QUALITY_PATH) so a repointed path (the test / perf-probe seam)
misses the cache instead of returning stale content.

Pattern: import module, patch read_yaml_file with a counting wrapper,
assert parse count across repeated score_goal calls. Direct function calls;
no subprocess.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# goal-selector.py requires MIND_AGENT to load (paths derive AGENT_DIR).
# Capture-restore around the module-level mutation (Layer 1 test-pollution
# defense — rb-1096, guard-588).
_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "bravo")

gs = importlib.import_module("goal-selector")

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT


def _mk_candidate(gid="g-900-01", skill="/tree"):
    return {
        "goal": {"id": gid, "title": "cache probe", "status": "pending",
                 "priority": "MEDIUM", "skill": skill},
        "aspiration": {"id": "asp-900", "goals": [], "priority": "MEDIUM"},
        "source": "world",
    }


def _score(cand):
    # epsilon=0 zeroes the noise weight — determinism is not under test, but
    # it keeps breakdown assertions exact.
    return gs.score_goal(cand, {}, [], [], epsilon=0.0, noise_scale=0.0)


def _write_sq(path, overall):
    path.write_text(
        "skills:\n  tree:\n    aggregate:\n      overall: {v}\n".format(v=overall),
        encoding="utf-8")


def test_one_parse_per_invocation(tmp_path, monkeypatch):
    sq = tmp_path / "skill-quality.yaml"
    _write_sq(sq, 0.9)
    monkeypatch.setattr(gs, "SKILL_QUALITY_PATH", sq)
    gs._SKILL_QUALITY_CACHE.clear()

    calls = []
    real = gs.read_yaml_file

    def counting(path):
        if str(path) == str(sq):
            calls.append(str(path))
        return real(path)

    monkeypatch.setattr(gs, "read_yaml_file", counting)
    for i in range(5):
        _score(_mk_candidate(gid="g-900-0{n}".format(n=i + 1)))
    assert len(calls) == 1, "expected exactly 1 parse across 5 score_goal calls, saw {n}".format(n=len(calls))


def test_cached_content_still_scores(tmp_path, monkeypatch):
    sq = tmp_path / "skill-quality.yaml"
    _write_sq(sq, 0.9)
    monkeypatch.setattr(gs, "SKILL_QUALITY_PATH", sq)
    gs._SKILL_QUALITY_CACHE.clear()
    r = _score(_mk_candidate())
    # raw skill_affinity maps overall [0,1] -> [-1,+1]: (0.9 - 0.5) * 2 = 0.8
    assert abs(r["raw"]["skill_affinity"] - 0.8) < 1e-9


def test_repointed_path_misses_cache(tmp_path, monkeypatch):
    a = tmp_path / "a.yaml"
    b = tmp_path / "b.yaml"
    _write_sq(a, 0.9)
    _write_sq(b, 0.1)
    gs._SKILL_QUALITY_CACHE.clear()
    monkeypatch.setattr(gs, "SKILL_QUALITY_PATH", a)
    ra = _score(_mk_candidate())
    monkeypatch.setattr(gs, "SKILL_QUALITY_PATH", b)
    rb = _score(_mk_candidate())
    assert abs(ra["raw"]["skill_affinity"] - 0.8) < 1e-9
    assert abs(rb["raw"]["skill_affinity"] - (-0.8)) < 1e-9


def test_missing_file_cached_as_neutral(tmp_path, monkeypatch):
    monkeypatch.setattr(gs, "SKILL_QUALITY_PATH", tmp_path / "absent.yaml")
    gs._SKILL_QUALITY_CACHE.clear()
    r = _score(_mk_candidate())
    # read_yaml_file returns {} for a missing file -> overall defaults 0.5
    # -> raw 0.0 (neutral). The {} is cached like any parse (read-once).
    assert abs(r["raw"]["skill_affinity"]) < 1e-9
