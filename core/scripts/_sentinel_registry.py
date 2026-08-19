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
    # The "run experience-read.sh FIRST" clause is guard-2939 delivered at the
    # moment of use, and it is here because delivery — not encoding — is what was
    # missing. That guardrail describes this exact failure precisely (recurring
    # goal, cycle N>1, bare `exp-<goal-id>` id and path both already taken by
    # cycle 1) and had utilization times_active=32 / times_helpful=0 when bravo
    # reproduced it on 2026-08-14 (cc-05, ): Write clobbered a real
    # 5,318-byte experience file and returned "has been updated successfully",
    # which reads as success. Only the store's `duplicate_id` refusal surfaced it,
    # and that is luck — an id that happened to be unique against a colliding
    # content_path destroys the file and reports total success.
    #
    # 32 activations with 0 credits is the signature of a guardrail that MATCHES
    # but never REACHES anyone (guard-1984: a guardrail cannot outvote the
    # instrument it guards; guard-2462: fix the instrument, not the prose). This
    # dispatch line is the instrument — it is what an agent reads immediately
    # before composing the record.
    #
    # It names the COMMAND and the guard ID deliberately, and does NOT paraphrase
    # guard-2939's content: per guard-2494 a verbatim quote of an amendable
    # upstream field is a snapshot that decays. The wrapper API and the id are
    # stable; the guardrail's wording is not.
    {
        "slot": "force_experience_archival",
        "phase": "0-pre2",
        "skill_section": (
            "aspirations-precheck Phase 0-pre2 (Experience Archival Gate; compose + experience-add.sh"
            " — FIRST run `bash core/scripts/experience-read.sh --goal <goal-id>`: if it returns any"
            " record, this goal already has one, so the bare `exp-<goal-id>` id AND"
            " `agents/<agent>/experience/exp-<goal-id>.md` path are BOTH taken and Write will"
            " silently overwrite the existing file while reporting success. Use a unique slug."
            " See guard-2939)"
        ),
        "fired_key": False,
        "dispatch_slot": None,
        "canary_tracked": False,
    },
    # . Consumer phase 0-pre2.5 shipped with  but was never
    # registered, so the battery enumerated six slots and its "all N null — no
    # gates to dispatch" line instructed a SKIP past 0-pre..0-pre6 that included
    # this gate. Measured twice: zeta/cc-02 2 stubs unseen ~9h; bravo/cc-05
    # 15 MATERIAL self.md stubs unseen ~19h, found only by a standalone read.
    #
    # fired_key False — MEASURED, not copied. The producer
    # (evolution-stub-pending-check.sh) exits at `if not pending` BEFORE
    # building the payload, so a zero-count payload is unreachable and the dict
    # is always non-empty. is_set()'s no-"fired"-key branch (bool(value)) is
    # therefore exactly right; a count-based actionable test would be dead code.
    #
    # canary_tracked True WITH a dispatch_slot — also measured. The producer is
    # idempotent and RE-ARMS every iteration while any stub is pending, which is
    # the fresh_eyes_dispatch_pending shape  fixed, NOT the
    # force_tree_maintain shape guard-868 says is safe to presence-count (that
    # one's writers are rate-limited so it never re-arms). Presence-counting
    # here would false-fire on the legitimate never-fabricate path: a stub whose
    # rationale genuinely cannot be reconstructed is SUPPOSED to sit set until
    # evolution-stub-expiry's 24h deadline. Dispatch-advancement gives the
    # staleness detection this gate needs without that false fire.
    #
    # Position is load-bearing: the battery prints in REGISTRY order (it does
    # not sort), so registry order must equal protocol order — 0-pre2.5 belongs
    # between 0-pre2 and 0-pre3, not appended at the end.
    {
        "slot": "force_evolution_finalize",
        "phase": "0-pre2.5",
        "skill_section": "aspirations-precheck Phase 0-pre2.5 (Evolution-Stub Finalize Gate; evolution-complete.sh per stub — NEVER fabricate a rationale)",
        "fired_key": False,
        "dispatch_slot": "force_evolution_finalize_last_dispatch",
        "canary_tracked": True,
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
