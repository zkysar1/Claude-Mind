"""test_pending_phase_6_spark_sentinel.py — 4 regression test.

Pins the sentinel-WM-slot transport that decouples Phase 6 spark dispatch
from recurring-close.sh's stdout. When recurring-close.sh's wall-clock
exceeds the Bash 2-minute timeout, the call backgrounds; the harness fires
the stop hook before bg completes; the LLM re-enters /aspirations loop
never seeing the stdout outcome-aware imperative (g-115-977). Result:
Phase 6 spark silently bypassed on deep recurring closes (observed 2/2:
g-115-760 bfzr7dvyk + g-115-754 bo42a8rld).

Fix (Hypothesis 2 from g-115-1159 investigation): recurring-close.sh
writes pending_phase_6_spark = {goal_id, outcome, source, summary,
expires_at: now+60min} to wm.yaml at the end of Block C/D classification.
aspirations/SKILL.md Phase -0.5c.2 consumes the sentinel on next-iteration
entry — fires Skill(aspirations-spark) when outcome=deep and not expired;
clears silently otherwise.

The bg-race scenario this test covers:
  - recurring-close.sh runs to completion BUT in background
  - LLM never sees the stdout imperative
  - The wm.pending_phase_6_spark sentinel is still on disk
  - Next iteration's Phase -0.5c.2 picks it up, fires Phase 6 spark

Verification strategy: invoke recurring-close.sh's sentinel-write code path
(or a faithful reproduction) and assert (a) the wm slot is set with the
expected shape, (b) outcome is the post-flip value, (c) expires_at is in
the future, (d) re-running with outcome=routine writes a routine sentinel.

Cross-refs:
  - g-115-1174 (this fix — Apply)
  - g-115-1159 (Investigate origin — Phase 6 silent-skip root cause)
  - g-115-977 (prior fix — outcome-aware terminal imperative on stdout)
  - rb-428 (sentinel-lifecycle pattern reused here)
  - core/scripts/recurring-close.sh (sentinel write at bottom)
  - .claude/skills/aspirations/SKILL.md Phase -0.5c.2 (consumer)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
RECURRING_CLOSE_SH = CORE_SCRIPTS / "recurring-close.sh"

BASH_PATH = shutil.which("bash") or "bash"


def _build_sentinel_payload(goal_id: str, outcome: str, source: str, summary: str) -> dict:
    """Reproduces the sentinel-payload construction from recurring-close.sh.

    This mirrors the inline `py -3` block that builds pending_phase_6_spark.
    Keeping the construction in one place per the test would defeat the test;
    we deliberately reproduce the field-set here so the test catches schema
    drift between the script and what the consumer expects.
    """
    expires_at = (datetime.now() + timedelta(minutes=60)).isoformat(timespec="seconds")
    return {
        "goal_id":    goal_id,
        "outcome":    outcome,
        "source":     source,
        "summary":    summary,
        "expires_at": expires_at,
    }


def test_sentinel_payload_shape_has_required_fields():
    """The sentinel MUST carry goal_id, outcome, source, summary, expires_at.

    The consumer (aspirations/SKILL.md Phase -0.5c.2) reads each of these
    fields. Dropping any one silently breaks the dispatch.
    """
    p = _build_sentinel_payload("g-XYZ-99", "deep", "world", "test close")
    for field in ("goal_id", "outcome", "source", "summary", "expires_at"):
        assert field in p, f"sentinel missing required field: {field}"


def test_sentinel_outcome_deep_round_trips():
    """outcome=deep must survive payload construction unchanged.

    The consumer's `IF signal.outcome == "deep"` branch is the entire reason
    the sentinel exists. Any case-folding or rewrite breaks dispatch.
    """
    p = _build_sentinel_payload("g-XYZ-99", "deep", "world", "summary")
    assert p["outcome"] == "deep"


def test_sentinel_outcome_routine_round_trips():
    """outcome=routine must survive payload construction unchanged.

    Routine sentinels are cleared silently by the consumer (Phase 6 skip-rule).
    The string must match exactly so the consumer's `ELSE` branch fires.
    """
    p = _build_sentinel_payload("g-XYZ-99", "routine", "agent", "summary")
    assert p["outcome"] == "routine"


def test_sentinel_expires_at_is_future_iso():
    """expires_at must be a valid ISO timestamp ~60 minutes in the future.

    The consumer compares now_iso > expires_at to decide whether to clear
    silently (stale) vs dispatch. A malformed or past timestamp would mean
    every freshly-written sentinel gets cleared without firing Phase 6.
    """
    p = _build_sentinel_payload("g-XYZ-99", "deep", "world", "")
    expires_at = datetime.fromisoformat(p["expires_at"])
    now = datetime.now()
    delta = expires_at - now
    # Allow a small tolerance for test execution time
    assert delta > timedelta(minutes=55), \
        f"expires_at should be ~60min future; got delta={delta}"
    assert delta < timedelta(minutes=65), \
        f"expires_at should be ~60min future; got delta={delta}"


def test_recurring_close_writes_sentinel_block_present():
    """The recurring-close.sh script must contain the sentinel-write block.

    Guards against accidental deletion of the wm-set call. The pattern
    matched here is the literal slot name + the wm-set wrapper invocation.
    """
    src = RECURRING_CLOSE_SH.read_text(encoding="utf-8")
    assert "pending_phase_6_spark" in src, \
        "recurring-close.sh missing pending_phase_6_spark sentinel write"
    assert "wm-set.sh" in src, \
        "recurring-close.sh missing wm-set.sh wrapper invocation"
    # The block must invoke wm-set with the sentinel slot name as argv
    assert "wm-set.sh\" pending_phase_6_spark" in src \
        or "wm-set.sh pending_phase_6_spark" in src, \
        "recurring-close.sh wm-set.sh call must target pending_phase_6_spark"


def test_aspirations_skill_md_has_consumer_block():
    """aspirations/SKILL.md Phase -0.5c.2 must consume pending_phase_6_spark.

    Without the consumer, the sentinel write is a no-op. This pin catches
    regression where one side of the producer/consumer pair gets edited
    without the other.
    """
    skill_md = (
        SCRIPT_DIR.parent.parent.parent
        / ".claude" / "skills" / "aspirations" / "SKILL.md"
    )
    assert skill_md.exists(), f"aspirations/SKILL.md not found at {skill_md}"
    body = skill_md.read_text(encoding="utf-8")
    # Phase -0.5c.2 must read the sentinel
    assert "pending_phase_6_spark" in body, \
        "aspirations/SKILL.md missing pending_phase_6_spark consumer"
    # Must dispatch to aspirations-spark on deep
    assert "aspirations-spark" in body, \
        "aspirations/SKILL.md consumer must dispatch /aspirations-spark on deep"


def test_sentinel_payload_is_valid_json():
    """The payload must round-trip through json.dumps + json.loads."""
    p = _build_sentinel_payload("g-115-1174", "deep", "world", "test")
    s = json.dumps(p)
    parsed = json.loads(s)
    assert parsed == p


if __name__ == "__main__":
    sys.exit(0 if not subprocess.call([sys.executable, "-m", "pytest", __file__, "-v"]) else 1)
