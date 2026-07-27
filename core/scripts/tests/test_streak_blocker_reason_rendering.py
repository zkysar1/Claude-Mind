"""test_streak_blocker_reason_rendering.py —  regression.

THE BUG: when an infra component crosses the failing-streak threshold,
infra-health.py::_sync_known_blockers auto-generates a category-gating
known_blocker record. It wrote the failure narrative under the key
`description`, but EVERY goal-selector block-detail renderer
(collect_blocked + trace_root_bottleneck) reads `b.get("reason", "unknown")`.
Field-name mismatch → every streak-gated goal's block reason rendered as the
literal word "unknown". Verified 2026-07-21 with the roblox-studio streak
blocker at 20 consecutive failures.

THE FIX (single-source-of-truth, producer-side): the streak-record
construction site emits the narrative under `reason` — the field the
renderers consume — instead of patching the 4 consumer sites.

This test pins BOTH halves of the contract end-to-end and hermetically:
  1. PRODUCER: drive _sync_known_blockers with a synthetic alert (fake in-memory
     `wm` module + stubbed category/session helpers), capture the written record,
     assert the narrative lands under `reason`.
  2. JOINED: feed that captured record into goal-selector.collect_blocked via a
     category-matched pending goal; assert block_detail NAMES the probe failure
     and is NOT the bare word "unknown".
  3. NEGATIVE CONTROL: the OLD `description`-only shape renders "unknown" —
     proving the field name is load-bearing and this test would have caught the
     original bug.

No daemon, no real WM, no world files — the fake wm module and helper stubs keep
it hermetic (no daemon_integration marker).
"""
from __future__ import annotations

import importlib.util
import os
import sys
from contextlib import contextmanager
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

# Both modules derive AGENT_DIR from MIND_AGENT at import — set + restore so
# collection-time env mutation cannot leak to sibling tests (rb-1096, guard-588).
_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "alpha")


def _load(alias, filename):
    path = CORE_SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(alias, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ih = _load("infra_health_streak", "infra-health.py")
gs = _load("goal_selector_streak", "goal-selector.py")

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT

collect_blocked = gs.collect_blocked

_STREAK_COMPONENT = "roblox-studio"
_STREAK_CATEGORY = "roblox-client"
_LAST_FAILURE = "connection refused"


class _FakeWM:
    """Minimal in-memory stand-in for the `wm` module _sync_known_blockers uses."""

    def __init__(self):
        self.data = {"known_blockers": []}

    @contextmanager
    def wm_lock(self):
        yield

    def read_wm(self):
        return self.data

    def resolve_slot(self, data, slot):
        # (parent, key, is_top) — top-level slot lives directly on `data`.
        return data, slot, True

    def update_modified(self, data, slot):
        pass

    def write_wm(self, data):
        self.data = data


def _produce_streak_record(consecutive_failures=20):
    """Run the REAL _sync_known_blockers producer against a fake wm + stubbed
    helpers; return the single streak known_blocker record it wrote."""
    fake = _FakeWM()
    saved_wm = sys.modules.get("wm")
    sys.modules["wm"] = fake  # `import wm` inside _sync_known_blockers picks this up
    saved_cats = ih._load_component_categories
    saved_sess = ih._get_session_number
    ih._load_component_categories = lambda: {_STREAK_COMPONENT: [_STREAK_CATEGORY]}
    ih._get_session_number = lambda: 999
    try:
        ih._sync_known_blockers([{
            "component": _STREAK_COMPONENT,
            "consecutive_failures": consecutive_failures,
            "last_failure_reason": _LAST_FAILURE,
        }])
    finally:
        ih._load_component_categories = saved_cats
        ih._get_session_number = saved_sess
        if saved_wm is None:
            sys.modules.pop("wm", None)
        else:
            sys.modules["wm"] = saved_wm

    streaks = [b for b in fake.data["known_blockers"]
               if isinstance(b, dict) and str(b.get("blocker_id", "")).startswith("streak-")]
    assert len(streaks) == 1, f"expected exactly one streak record; got {fake.data['known_blockers']!r}"
    return streaks[0]


def _asp_with_category_goal():
    """One active aspiration, one pending goal with skill=null in the gated
    category (so collect_blocked's category-fallback infra path fires)."""
    return [{
        "id": "asp-streak", "status": "active", "priority": "MEDIUM",
        "goals": [{
            "id": "g-streak-1",
            "title": "gated on roblox-studio",
            "status": "pending",
            "priority": "MEDIUM",
            "category": _STREAK_CATEGORY,
            "participants": ["agent"],
            "recurring": False,
        }],
    }]


def _block_detail(known_blockers):
    blocked = collect_blocked(_asp_with_category_goal(), known_blockers=known_blockers)
    entries = {b["goal_id"]: b for b in blocked}
    assert "g-streak-1" in entries, f"streak-gated goal absent from blocked[]; got {entries!r}"
    return entries["g-streak-1"]["block_detail"]


# ── 1. PRODUCER: streak record narrative lands under `reason`, not `description` ──

def test_producer_writes_reason_field():
    rec = _produce_streak_record()
    assert "reason" in rec, f"streak record must carry a `reason` field; got {rec!r}"
    assert "description" not in rec, (
        "streak record must NOT carry the old `description` key — the renderers "
        f"read `reason`; got {rec!r}")
    assert "consecutive probe failures" in rec["reason"], rec
    assert _LAST_FAILURE in rec["reason"], rec
    assert rec["affected_categories"] == [_STREAK_CATEGORY], rec


# ── 2. JOINED: end-to-end block_detail names the probe failure, not "unknown" ──

def test_streak_gated_block_detail_names_failure_not_unknown():
    rec = _produce_streak_record()
    detail = _block_detail([rec])
    assert "Streak alert: 20 consecutive probe failures" in detail, detail
    assert _LAST_FAILURE in detail, detail
    # The bug's fingerprint: the rendered reason must not be the bare default.
    assert not detail.endswith("blocked: unknown"), detail


# ── 3. NEGATIVE CONTROL: the OLD `description`-only shape renders "unknown" ──

def test_old_description_shape_renders_unknown_regression_guard():
    """Documents that the field name is load-bearing: a record carrying the
    pre-fix `description` key (and no `reason`) renders the bare 'unknown'
    default. If this ever stops holding, the renderer contract changed and the
    producer fix above must be re-validated."""
    legacy = {
        "blocker_id": f"streak-{_STREAK_COMPONENT}",
        "description": "Streak alert: 20 consecutive probe failures. Last failure: connection refused",
        "affected_categories": [_STREAK_CATEGORY],
        "affected_skills": [],
        "resolution": None,
        "source": "infra-health.streak-alert",
    }
    detail = _block_detail([legacy])
    assert detail.endswith("blocked: unknown"), (
        f"legacy `description`-only record should render the bare 'unknown' "
        f"default (proving the field name matters); got {detail!r}")


if __name__ == "__main__":
    test_producer_writes_reason_field()
    test_streak_gated_block_detail_names_failure_not_unknown()
    test_old_description_shape_renders_unknown_regression_guard()
    print("ok")
