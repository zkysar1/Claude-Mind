# domain-leak-exempt: IAUS scorer implementation (BRD Gap 8, g-306-32) -- ports
# the proven IAUS shape into the MIND goal-selector. IAUS is the framework
# feature name and NPC is a concrete source-artifact reference, not an example.
# Companion to the exempt design core/config/iaus-selector-design.md.
"""IAUS-shaped scorer for the MIND goal-selector (g-306-32, BRD Gap 8).

Second, flag-gated scoring path for `goal-selector.py::score_goal()`. Ports the
proven Infinite Axis Utility System (IAUS) shape into the MIND's own goal
selector. The existing additive scorer stays the default; this path runs only
when `iaus_selector.use_iaus` is true in `core/config/aspirations.yaml`.

Authoritative design: `core/config/iaus-selector-design.md` (deliverable of
g-306-31). The A/B + flagged cutover is the sibling g-306-33.

Why IAUS (the additive defect): additive scoring lets a high value in one
criterion mask a disqualifying low value in another. A goal barely
agent-executable (raw agent_executable near 0) but with high completion_pressure
+ recurring_urgency still ranks high under the sum. IAUS fixes this with
multiply + veto-by-zero: a zero in the VETO tier zeros the whole score,
regardless of any other axis.

Combination (design section 2c):
    veto    = product over VETO axes          (any 0 => veto = 0)
    primary = product over PRIMARY axes
    base    = veto * primary**(1/n)           (geometric-mean compensation)
    makeup  = base + (1 - base) * (1 - 1/m) * base   (Dave-Mark count-based lift)
    score   = makeup * bonus_mult             (tier-3 bounded multiplier)
    final   = score + exploration_noise * noise_weight   (added by the CALLER)

Two design-fidelity decisions made for this initial implementation, both
flagged for g-306-33 A/B tuning:

  1. PRIMARY axes carry a small floor `b` (primary_floor, default 0.1) so an
     axis that is legitimately 0 for a goal-class (recurring_urgency=0 for
     non-recurring goals; deadline_urgency=0 for no-deadline goals;
     critical_blocker_surface=0 for almost all goals) does NOT act as an
     unintended veto. Only the VETO tier (b=0) zeros the score. The design's
     section-6 proposal of pure linear b=0 curves is unviable for these axes:
     with b=0, every non-recurring goal (recurring_urgency=0) would multiply to
     a zero primary product and be vetoed, which is wrong. The design's own
     response-curve `b` (floor) parameter is exactly the fix.

  2. Tier-3 (MAKEUP/BONUS) is aggregated as a single bounded multiplier
     (`bonus_mult` in (0.5, 1.5) via tanh) rather than per-axis response curves.
     This is a faithful reading of the design's `score = makeup *
     bonus_sum_normalized` ("tier-3 makeup applied as a bounded multiplier")
     and honors the section-2b requirement that tier-3 "must NOT dominate or
     veto." The existing per-axis WEIGHTS are reused to weight the tier-3 sum.

This module has NO import-time side effects and does not read config or files
on its own (it is pure given its inputs), so it is cheap to import and trivial
to unit-test.
"""
import math

# --- Tier membership (design section 2d) -----------------------------------
# Each of score_goal()'s ~26 raw criteria maps to exactly one tier. The
# orthogonal exploration_noise term is NOT a tier member — the caller adds it
# additively on top of the IAUS utility, preserving epsilon-greedy exploration.
VETO_AXES = (
    "agent_executable",            # 0 => unselectable (the primary veto)
)
PRIMARY_AXES = (
    "priority",
    "completion_pressure",
    "recurring_urgency",
    "deadline_urgency",
    "critical_blocker_surface",
)
MAKEUP_AXES = (
    "variety_bonus", "novelty_bonus", "streak_momentum", "reward_history",
    "depth_bonus", "tail_bonus", "role_affinity", "class_balance_bonus",
    "evidence_backing", "context_coherence", "skill_affinity",
    "directive_boost", "handoff_bonus", "per_goal_saturation",
    "user_signal_boost", "cross_aspiration_support", "co_invest_alignment",
    "deferred_readiness", "recurring_saturation",
)

# --- Per-axis domain maxima (scale raw input to a known domain before curve) -
# Design 2a: "Raw inputs must first be scaled to a known domain before the
# curve." Only VETO + PRIMARY axes need scaling (MAKEUP is handled by the
# weighted-sum multiplier). priority is special-cased (design's explicit
# HIGH/MED/LOW -> 1.0/0.6/0.3 mapping). recurring_urgency's max tracks
# RECURRING_CONFIG['urgency_max'] (default 4.0) — passed in via config so the
# module stays decoupled from goal-selector's config loaders.
_DOMAIN_MAX = {
    "agent_executable": 2.0,        # raw is {0, 2}
    "completion_pressure": 2.5,     # ratio**2 * 2.5, max at ratio=1
    "deadline_urgency": 3.0,        # max raw is 3 (<=1d)
    "critical_blocker_surface": 1.0,  # already normalized in its compute
    # recurring_urgency: from config (urgency_max); fallback 4.0
}

_PRIORITY_SCALE = {3: 1.0, 2: 0.6, 1: 0.3}  # design 2a explicit mapping


def response_curve(x, c=0.0, m=1.0, k=1.0, b=0.0):
    """IAUS response curve: clamp(m*(x - c)**k + b, 0.0, 1.0).

    Reused from the NPC IAUS response-curve math (world/conventions/
    iaus-tuning-schema.md), NOT modified. With the initial linear params
    (m=1, k=1, c=0) this is clamp(x + b, 0, 1): identity shifted by the floor b.
    """
    base = x - c
    # A negative base with a non-integer exponent yields a COMPLEX result in
    # Python (no exception raised), which would break the comparisons below.
    # Degrade to the floor. With the initial linear curves (k=1) this branch is
    # never taken; it guards future non-linear curve tuning (g-306-33).
    if base < 0.0 and k != int(k):
        val = b
    else:
        try:
            val = m * (base ** k) + b
        except (ValueError, OverflowError):
            val = b
    if val < 0.0:
        return 0.0
    if val > 1.0:
        return 1.0
    return val


def scale_axis(axis, value, urgency_max=4.0):
    """Scale a raw criterion value into [0,1] for its tier curve.

    priority uses the design's explicit HIGH/MED/LOW mapping; recurring_urgency
    uses the configured urgency_max; everything else divides by its _DOMAIN_MAX.
    Values are clamped into [0,1] (negative raw — only possible in MAKEUP, which
    does not call this — clamps to 0).
    """
    if axis == "priority":
        return _PRIORITY_SCALE.get(int(round(value)), max(0.0, min(1.0, value / 3.0)))
    if axis == "recurring_urgency":
        dmax = urgency_max if urgency_max and urgency_max > 0 else 4.0
    else:
        dmax = _DOMAIN_MAX.get(axis, 1.0)
    if dmax <= 0:
        dmax = 1.0
    scaled = value / dmax
    if scaled < 0.0:
        return 0.0
    if scaled > 1.0:
        return 1.0
    return scaled


def iaus_score(raw, weights, config):
    """Compute the IAUS utility for one goal's raw-criteria dict.

    Returns a dict: {"score", "veto", "primary", "base", "makeup", "bonus_mult",
    "pruned"}. The caller adds exploration_noise additively to "score" to get
    the final total; "pruned" is True when the watermark dropped this candidate
    (score forced to 0.0).

    Pure function — no I/O, no globals. `weights` is the existing WEIGHTS dict
    (reused for the tier-3 multiplier); `config` carries primary_floor,
    watermark, bonus_scale, and urgency_max.
    """
    floor = float(config.get("primary_floor", 0.1))
    watermark = float(config.get("watermark", 0.0))
    bonus_scale = float(config.get("bonus_scale", 4.0)) or 4.0
    urgency_max = float(config.get("urgency_max", 4.0))

    # --- VETO tier (b=0, can zero the score) ---
    veto = 1.0
    for ax in VETO_AXES:
        veto *= response_curve(scale_axis(ax, raw.get(ax, 0.0), urgency_max), b=0.0)

    # --- PRIMARY tier (b=floor, multiplied as geometric mean) ---
    primary = 1.0
    n = 0
    for ax in PRIMARY_AXES:
        primary *= response_curve(scale_axis(ax, raw.get(ax, 0.0), urgency_max), b=floor)
        n += 1
    geo = primary ** (1.0 / n) if n > 0 else 1.0
    base = veto * geo

    # --- Watermark prune (default 0.0 => never prunes) ---
    if base < watermark:
        return {"score": 0.0, "veto": veto, "primary": primary,
                "base": base, "makeup": 0.0, "bonus_mult": 0.0, "pruned": True}

    # --- Dave-Mark makeup compensation (count-based lift of base) ---
    m = len(MAKEUP_AXES)
    if m > 0:
        makeup = base + (1.0 - base) * (1.0 - 1.0 / m) * base
    else:
        makeup = base

    # --- Tier-3 bounded multiplier (refines ordering, cannot dominate/veto) ---
    bonus_raw = sum(raw.get(a, 0.0) * weights.get(a, 0.0) for a in MAKEUP_AXES)
    bonus_mult = 1.0 + math.tanh(bonus_raw / bonus_scale) * 0.5  # in (0.5, 1.5)

    score = makeup * bonus_mult
    return {"score": score, "veto": veto, "primary": primary, "base": base,
            "makeup": makeup, "bonus_mult": bonus_mult, "pruned": False}
