#!/usr/bin/env python3
"""test_init_meta_seed_parity.py — init-parity guard for fail-loud META consumers
(rb-2672 / g-001-289 audit).

THE LESSON THIS TEST ENFORCES (rb-2672):
  A test with a self-written fixture does NOT prove the prod file exists on a
  fresh clone. test_precheck_cognitive_horizons.py writes its OWN
  cognitive-horizons.yaml into tmp_path, so it stayed GREEN for ~3 weeks
  (2026-06-13..07-03) while a fresh box crashed with FileNotFoundError —
  g-306-02 shipped the fail-loud consumer + convention but never seeded
  meta/cognitive-horizons.yaml into init-meta.sh (init-parity gap).

THE GUARD:
  Some META files are consumed FAIL-LOUD — the consumer raises FileNotFoundError
  or sys.exit()s on a missing file, with NO hardcoded fallback (by design: SSOT,
  no-drift). Every such file MUST be seeded by init-meta.sh so a fresh box does
  not crash on first loop iteration. This test pins that parity:

    for each fail-loud META consumer's file:
      1. a committed core/config/<file> seed template exists, AND
      2. init-meta.sh copies it into $META on init.

  When you add a NEW fail-loud META consumer (raise FileNotFoundError / sys.exit
  on a missing META_DIR file with no fallback), add its file to
  FAIL_LOUD_META_FILES below AND wire the seed into init-meta.sh — this test
  will fail until you do.
"""

import re
import sys
from pathlib import Path

import pytest

# parents[3]: tests -> scripts -> core -> PROJECT_ROOT. The prior
# .parent.parent.parent stopped at <root>/core, doubling CONFIG_DIR to
# core/core/config — the 9 .parents off-by-one class IN a test
# (this file never ran green before 3).
PROJECT_ROOT = Path(__file__).resolve().parents[3]
INIT_META = PROJECT_ROOT / "core" / "scripts" / "init-meta.sh"
CONFIG_DIR = PROJECT_ROOT / "core" / "config"

# META files with a fail-loud consumer (raise FileNotFoundError / sys.exit on a
# missing file, NO hardcoded fallback). Value = the consumer that fails loud.
# Discovered by the  audit: grep core/scripts + mind_api for
# `raise FileNotFoundError` / `not found at` / `sys.exit` referencing META_DIR.
FAIL_LOUD_META_FILES = {
    "cognitive-horizons.yaml": "precheck-eval.py::_load_cognitive_horizons (raise FileNotFoundError)",
    "skill-discovery-strategy.yaml": "skill-discovery.py::load_strategy + mind_api skill_discovery.py (sys.exit 3)",
}


@pytest.fixture(scope="module")
def init_meta_text():
    assert INIT_META.exists(), f"init-meta.sh not found at {INIT_META}"
    return INIT_META.read_text(encoding="utf-8")


@pytest.mark.parametrize("fname,consumer", sorted(FAIL_LOUD_META_FILES.items()))
def test_config_template_exists(fname, consumer):
    """A committed core/config/<file> seed template must exist for each
    fail-loud META consumer, else init-meta.sh has nothing to copy."""
    template = CONFIG_DIR / fname
    assert template.exists(), (
        f"core/config/{fname} seed template is MISSING. Consumer {consumer} "
        f"fails loud on a missing file — a fresh box will crash. Create the "
        f"template (rb-2672 init-parity)."
    )
    assert template.stat().st_size > 0, f"core/config/{fname} is empty"


@pytest.mark.parametrize("fname,consumer", sorted(FAIL_LOUD_META_FILES.items()))
def test_init_meta_seeds_file(fname, consumer, init_meta_text):
    """init-meta.sh MUST copy core/config/<file> into $META so a fresh box has
    it. This is the check that test_precheck_cognitive_horizons.py's self-written
    fixture could NOT provide (rb-2672)."""
    # Match a seed line that copies the config template into $META, e.g.
    #   [[ -f "$META/cognitive-horizons.yaml" ]] || { cp "$CONFIG/cognitive-horizons.yaml" "$META/cognitive-horizons.yaml"; ... }
    escaped = re.escape(fname)
    seeds_from_config = re.search(
        rf'cp\s+"\$CONFIG/{escaped}"\s+"\$META/{escaped}"', init_meta_text
    )
    assert seeds_from_config, (
        f"init-meta.sh does NOT seed {fname} from $CONFIG. Consumer {consumer} "
        f"fails loud on a missing file — a fresh box will crash "
        f"(the g-306-02 / rb-2672 init-parity gap)."
    )


@pytest.mark.parametrize("fname,consumer", sorted(FAIL_LOUD_META_FILES.items()))
def test_seed_is_idempotent(fname, consumer, init_meta_text):
    """The seed MUST be guarded so re-running init on a populated meta/ never
    clobbers an S3-pulled or agent-evolved live SSOT. Two accepted forms: the
    original `[[ -f "$META/<file>" ]] ||` guard, or the g-115-2524
    `if seed_needed "$META/<file>"` guard (seed_needed subsumes the -f check
    and adds the backfill-mode store-of-record probe)."""
    escaped = re.escape(fname)
    guarded = re.search(
        rf'\[\[\s+-f\s+"\$META/{escaped}"\s+\]\]\s*\|\|'
        rf'|if\s+seed_needed\s+"\$META/{escaped}"',
        init_meta_text,
    )
    assert guarded, (
        f"init-meta.sh seed for {fname} is NOT idempotent — guard it with "
        f'`if seed_needed "$META/{fname}"` (or `[[ -f "$META/{fname}" ]] ||`) '
        f"so re-running init does not clobber an evolved live SSOT."
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
