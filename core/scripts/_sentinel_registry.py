"""_sentinel_registry — single source of truth for precheck force-gate sentinels.

Shared by:
  - precheck-sentinel-battery.py (g-115-2303): enumerates ALL battery slots in
    one call at the top of aspirations-precheck Phase 0-pre, so a compaction
    summary need only preserve "run the battery" instead of six phase
    enumerations (the omission class: post-autocompact reconstructions carried
    3 of 6 phases; a set sentinel sat unread for 3 iterations, g-115-2302).
  - stale-sentinel-canary.py (g-115-717): derives its TRACKED_SENTINELS and
    CONSUMPTION_AWARE map from here — the slow-path backstop stays unchanged
    in behavior.

Adding a new precheck sentinel gate? Add ONE entry here (and its consumer
phase in aspirations-precheck/SKILL.md). The battery and the canary pick it
up from this list — do not re-enumerate slots in either script.

Entry fields:
  slot            WM slot name (wm-read.sh <slot>)
  phase           aspirations-precheck phase that consumes it (None = no
                  precheck consumer; legacy canary tripwire only)
  skill_section   human pointer printed by the battery (dispatch target)
  fired_key       True when the payload is a dict that must have fired==true
                  to count as actionable (post-*-gate.sh JSON payloads)
  dispatch_slot   consumption-aware canary stamp slot (None = presence-count)
  canary_tracked  True when stale-sentinel-canary counts this slot
"""

from __future__ import annotations

SENTINELS: list[dict] = [
    {
        "slot": "force_tree_maintain",
        "phase": "0-pre",
        "skill_section": "aspirations-precheck Phase 0-pre (Tree-Debt Critical Gate; source-dispatch: encoding-drift=ack+clear, else /tree maintain --backlog)",
        "fired_key": False,
        "dispatch_slot": "force_tree_maintain_last_dispatch",
        "canary_tracked": True,
    },
    {
        "slot": "force_experience_archival",
        "phase": "0-pre2",
        "skill_section": "aspirations-precheck Phase 0-pre2 (Experience Archival Gate; compose + experience-add.sh)",
        "fired_key": False,
        "dispatch_slot": None,
        "canary_tracked": False,
    },
    {
        "slot": "fresh_eyes_dispatch_pending",
        "phase": "0-pre3",
        "skill_section": "aspirations-precheck Phase 0-pre3 (Fresh-Eyes-Code Dispatch Gate; invoke /fresh-eyes-code)",
        "fired_key": True,
        "dispatch_slot": "fresh_eyes_last_dispatch",
        "canary_tracked": True,
    },
    {
        "slot": "force_metric_encoding_pending",
        "phase": "0-pre4",
        "skill_section": "aspirations-precheck Phase 0-pre4 (Metric-Encoding Dispatch Gate; encode Verified Values to tree)",
        "fired_key": True,
        "dispatch_slot": "force_metric_encoding_last_dispatch",
        "canary_tracked": True,
    },
    {
        "slot": "pipeline_reconcile_pending",
        "phase": "0-pre5",
        "skill_section": "aspirations-precheck Phase 0-pre5 (Pipeline-Reconcile Gate; invoke signal.skill reconcile)",
        "fired_key": True,
        "dispatch_slot": None,
        "canary_tracked": False,
    },
    {
        "slot": "force_pre_apply_consult",
        "phase": "0-pre6",
        "skill_section": "aspirations-precheck Phase 0-pre6 (Pre-Apply-Consult Drift Gate; retrieve.sh consult or log n/a)",
        "fired_key": False,
        "dispatch_slot": None,
        "canary_tracked": False,
    },
    # Legacy tripwire — writer RETIRED (); no precheck consumer.
    # Kept canary-tracked so any future re-introduced set without a hot-path
    # consumer is still caught (see stale-sentinel-canary.py docstring).
    {
        "slot": "force_tree_encoding",
        "phase": None,
        "skill_section": "RETIRED writer (g-115-1521) — canary tripwire only; if set, clear after confirming no consumer exists",
        "fired_key": False,
        "dispatch_slot": None,
        "canary_tracked": True,
    },
]


def battery_slots() -> list[dict]:
    """Entries the precheck battery enumerates: every slot with a precheck phase."""
    return [s for s in SENTINELS if s["phase"] is not None]


def canary_tracked_slots() -> list[str]:
    """Slot names stale-sentinel-canary counts (order preserved from SENTINELS)."""
    return [s["slot"] for s in SENTINELS if s["canary_tracked"]]


def consumption_aware_map() -> dict[str, str]:
    """{slot: dispatch_slot} for canary-tracked slots with a dispatch stamp."""
    return {
        s["slot"]: s["dispatch_slot"]
        for s in SENTINELS
        if s["canary_tracked"] and s["dispatch_slot"]
    }


def is_set(value) -> bool:
    """A sentinel is 'set' when it carries a meaningful non-empty value.

    JSON null / boolean false / empty / 'null'-strings are NOT set.
    Dict-shaped sentinels (post-*-gate.sh JSON payloads) are set when their
    top-level 'fired' key is truthy, or — if 'fired' is absent — when the
    dict has any keys at all (defensive default). (Moved verbatim from
    stale-sentinel-canary._is_set so battery and canary can never diverge.)
    """
    if value is None or value is False:
        return False
    if isinstance(value, str):
        s = value.strip().lower()
        return s not in ("", "null", "false")
    if isinstance(value, dict):
        if "fired" in value:
            return bool(value["fired"])
        return bool(value)
    if isinstance(value, (list, tuple)):
        return bool(value)
    return True
