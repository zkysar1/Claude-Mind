#!/usr/bin/env python3
"""test_init_backfill.py — 4 additive seed-backfill regression pins.

THE CLASS UNDER GUARD (seed-drift, guard-146 incident): a world/meta
initialized BEFORE a seed line was added to init-world.sh / init-meta.sh
never receives the later-added seed — the `.initialized` early-exit closed
the gate forever (canonical: the cognitive-horizons ~3wk fleet-wide
FileNotFoundError). The fix: an already-initialized target runs the SAME
seed body in BACKFILL mode, with every seeding op per-file guarded — missing
seeds land, existing files are NEVER overwritten (additive-only).

Coverage split:
  - init-meta.sh: BEHAVIORAL tests (subprocess runs against tmp meta/) —
    its seed body is fully self-contained (no daemon calls).
  - init-world.sh: STRUCTURAL pin (text-level guard assertions) — its body
    calls the daemon-routed team-state-init.sh, so a behavioral run would be
    daemon-touching (rt_try_autospawn can spawn a REAL daemon). The shared
    seed_needed()/BACKFILL semantics are behaviorally proven via init-meta.sh;
    this pin holds init-world.sh to the same guard structure.

STORAGE_BACKEND=local is pinned in every subprocess env (guard-955 / rb-2983:
on an own-cloud box an env-copying subprocess writing a tmp world collides on
production S3 keys).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
INIT_META = PROJECT_ROOT / "core" / "scripts" / "init-meta.sh"
INIT_WORLD = PROJECT_ROOT / "core" / "scripts" / "init-world.sh"


def _run_init_meta(meta_dir: Path, world_dir: Path, extra_env: dict | None = None):
    env = os.environ.copy()
    env["STORAGE_BACKEND"] = "local"  # guard-955: never let tmp writes reach S3 keys
    env["MIND_META"] = str(meta_dir)
    env["MIND_WORLD"] = str(world_dir)
    env.pop("INIT_BACKFILL", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(INIT_META)],
        capture_output=True, text=True, env=env, cwd=str(PROJECT_ROOT),
        timeout=120,
    )


@pytest.fixture()
def meta_env(tmp_path):
    meta = tmp_path / "ext-meta"
    world = tmp_path / "ext-world"
    meta.mkdir()
    world.mkdir()
    return meta, world


# ── init-meta.sh behavioral pins ──────────────────────────────────────────

def test_fresh_init_seeds_meta(meta_env):
    meta, world = meta_env
    r = _run_init_meta(meta, world)
    assert r.returncode == 0, f"fresh init failed:\n{r.stdout}\n{r.stderr}"
    assert (meta / ".initialized").exists()
    for f in ("goal-selection-strategy.yaml", "cognitive-horizons.yaml",
              "skill-gaps.yaml", "spark-questions.jsonl", "skill-quality.yaml"):
        assert (meta / f).exists(), f"fresh init did not seed {f}"


def test_backfill_restores_missing_seed(meta_env):
    """The core 4 property: a seed absent from an already-initialized
    meta/ (added to the script after this meta/ was initialized, or lost) is
    re-seeded on the next init run instead of being skipped forever."""
    meta, world = meta_env
    assert _run_init_meta(meta, world).returncode == 0
    (meta / "cognitive-horizons.yaml").unlink()
    (meta / "skill-gaps.yaml").unlink()
    r = _run_init_meta(meta, world)
    assert r.returncode == 0, f"backfill run failed:\n{r.stdout}\n{r.stderr}"
    assert "seed-backfill" in r.stdout, "backfill mode banner missing"
    assert (meta / "cognitive-horizons.yaml").exists(), "backfill did not restore missing seed"
    assert (meta / "skill-gaps.yaml").exists(), "backfill did not restore missing seed"


def test_backfill_never_overwrites_existing(meta_env):
    """Additive-only: an evolved live file is NEVER replaced by the pristine
    template on a backfill run (the rb-498 clobber class this design forbids)."""
    meta, world = meta_env
    assert _run_init_meta(meta, world).returncode == 0
    evolved_strategy = "weights:\n  priority: 9.9  # agent-evolved sentinel\n"
    (meta / "goal-selection-strategy.yaml").write_text(evolved_strategy, encoding="utf-8")
    sq = meta / "skill-quality.yaml"
    evolved_sq = sq.read_text(encoding="utf-8") + "# evolved marker\n"
    sq.write_text(evolved_sq, encoding="utf-8")
    r = _run_init_meta(meta, world)
    assert r.returncode == 0
    assert (meta / "goal-selection-strategy.yaml").read_text(encoding="utf-8") == evolved_strategy, \
        "backfill OVERWROTE an evolved strategy file (additive-only violated)"
    assert sq.read_text(encoding="utf-8") == evolved_sq, \
        "backfill OVERWROTE an evolved skill-quality.yaml (additive-only violated)"


def test_backfill_optout_restores_old_skip(meta_env):
    meta, world = meta_env
    assert _run_init_meta(meta, world).returncode == 0
    (meta / "cognitive-horizons.yaml").unlink()
    r = _run_init_meta(meta, world, extra_env={"INIT_BACKFILL": "0"})
    assert r.returncode == 0
    assert "skipping" in r.stdout
    assert not (meta / "cognitive-horizons.yaml").exists(), \
        "INIT_BACKFILL=0 must restore the old early-exit (no seeding)"


# ── init-world.sh structural pins ─────────────────────────────────────────

# Every clobber-capable seed target in init-world.sh MUST be wrapped in an
# `if seed_needed "<target>"` guard. Adding a new seed without the guard
# reintroduces the reseed-clobber hazard (fresh-eyes H3 2026-05-18) AND
# breaks backfill additivity.
WORLD_GUARDED_TARGETS = [
    '$WORLD/config/capability-routing.yaml',
    '$WORLD/config/scaffolded-exploration.yaml',
    '$WORLD/config/applies-to-rules.yaml',
    '$WORLD/config/work-class-mapping.yaml',
    '$WORLD/config/stale-scanner.yaml',
    '$WORLD/config/infra-health-categories.yaml',
    '$WORLD/config/compatibility.yaml',
    '$WORLD/verification-checklist.md',
    '$WORLD/aspirations.jsonl',
    '$WORLD/aspirations-meta.json',
    '$WORLD/knowledge/tree/_tree.yaml',
    '$WORLD/evolution-triggers.yaml',
    '$WORLD/memory-pipeline.yaml',
    '$WORLD/pattern-signatures.jsonl',
    '$WORLD/pipeline-meta.json',
    '$WORLD/sources.yaml',
    '$WORLD/knowledge/beliefs.yaml',
    '$WORLD/knowledge/transitions.yaml',
    '$WORLD/knowledge/patterns/_index.yaml',
    '$WORLD/knowledge/strategies/_index.yaml',
    '$WORLD/knowledge/tree/execution.md',
    '$WORLD/knowledge/tree/intelligence.md',
    '$WORLD/knowledge/tree/performance.md',
    '$WORLD/knowledge/tree/system.md',
    '$WORLD/forged-skills.yaml',
    '$WORLD/skill-relations.yaml',
    '$WORLD/scripts/output-style-mode-guard.sh',
    '$WORLD/scripts/trailing-text-detector.py',
    # 4: bare-touch targets routed through seed_needed too — under
    # own-cloud BACKFILL a bare touch on a cache-absent-but-store-present file
    # creates a 0-byte local that the single-file push path would push over
    # the store content (guard-980 class; zeta fresh-eyes finding
    # zeta-fec-touch-cache-absent-202607172355).
    '$WORLD/aspirations-archive.jsonl',
    '$WORLD/pipeline.jsonl',
    '$WORLD/pipeline-archive.jsonl',
    '$WORLD/reasoning-bank.jsonl',
    '$WORLD/guardrails.jsonl',
    '$WORLD/program.md',
    '$WORLD/board/$channel.jsonl',
    '$WORLD/changelog.jsonl',
]


@pytest.fixture(scope="module")
def init_world_text():
    return INIT_WORLD.read_text(encoding="utf-8")


def test_init_world_has_backfill_gate(init_world_text):
    assert "INIT_BACKFILL" in init_world_text
    assert "additive seed-backfill" in init_world_text
    assert re.search(r"^seed_needed\(\)", init_world_text, re.M), \
        "init-world.sh must define seed_needed()"
    # The marker must no longer trigger an unconditional early exit: the only
    # `exit 0` tied to .initialized must sit behind the INIT_BACKFILL=0 opt-out.
    gate = re.search(
        r'if \[ -f "\$WORLD/\.initialized" \]; then\n(.*?)\nfi', init_world_text, re.S
    )
    assert gate and "INIT_BACKFILL" in gate.group(1), \
        "the .initialized gate must branch on INIT_BACKFILL, not exit unconditionally"


@pytest.mark.parametrize("target", WORLD_GUARDED_TARGETS)
def test_init_world_seed_targets_guarded(init_world_text, target):
    assert f'if seed_needed "{target}"' in init_world_text, (
        f"init-world.sh seed for {target} is not wrapped in "
        f'`if seed_needed "{target}"` — reseed/backfill would clobber it'
    )


def test_init_meta_defines_seed_needed():
    text = INIT_META.read_text(encoding="utf-8")
    assert re.search(r"^seed_needed\(\)", text, re.M)
    assert "INIT_BACKFILL" in text
    assert "--missing-only" in text, \
        "backfill mode must call meta-init.py --missing-only (bare call clobbers all strategies)"


# 4: init-meta.sh bare-touch targets routed through seed_needed
# (same clobber class as the WORLD_GUARDED_TARGETS touch entries above).
META_GUARDED_TOUCH_TARGETS = [
    '$META/meta-log.jsonl',
    '$META/dead-ends.jsonl',
    '$META/evolution-log.jsonl',
]


@pytest.mark.parametrize("target", META_GUARDED_TOUCH_TARGETS)
def test_init_meta_touch_targets_guarded(target):
    text = INIT_META.read_text(encoding="utf-8")
    assert f'if seed_needed "{target}"' in text, (
        f"init-meta.sh touch for {target} is not wrapped in "
        f'`if seed_needed "{target}"` — a 0-byte touch over a cache-absent-'
        f"but-store-present file re-opens the guard-980 clobber lane"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
