"""_cadence_anchor.py — shared cadence-anchor policy for the update-goal chokepoint.

`original_interval_hours` is read by TWO consumers in cargo-cult-detector.py with
OPPOSITE requirements, which is why the write rule needs a policy rather than a
one-liner:

  CAP   (_propose_new_interval, ~L706): proposed = min(interval*multiplier,
        original*cap_ratio). Bounds auto-EXTENSION. The anchor must be IMMUTABLE
        here — a mutable one is exactly the g-001-36 unbounded ratchet that
        g-115-2049 was written to stop.

  FLOOR (contract path, ~L1091):        floor = original*contract_floor_ratio.
        Bounds auto-SHORTENING. This anchor goes stale-LOW when a cadence is
        deliberately RAISED, so a goal widened 24h -> 168h kept floor =
        24*0.33 = 7.92h and a deep-outcome streak could walk a weekly cadence
        back toward ~8h (g-115-6104, evidence from g-326-85).

`is_deliberate_raise` is the discriminator that satisfies both. An auto-extension
is bounded BY CONSTRUCTION at original*cap_ratio, so a write STRICTLY ABOVE that
bound provably did not come from one — only a manual or batch cadence edit can
land there. Re-basing on exactly those writes fixes the floor while leaving the
anchor immutable for every automatic path, so the cap still cannot ratchet.

Lives in its own module because BOTH write paths need identical behaviour and
guard-742 makes divergence between them a live hazard: the CLI
(aspirations.py cmd_update_goal) and its DAEMON MIRROR
(mind_api/src/endpoints/aspirations_write.py update_goal), where the daemon is
the LIVE path under daemon-only architecture. Two existing call sites, one
definition — a copy in each would be a second place for the ratio to drift.
"""

from __future__ import annotations

from pathlib import Path

# Mirrors cargo-cult-detector._load_detector_config's own defaults. Both loaders
# must land on the SAME number when the config is unreadable, or a write the
# detector COULD have produced would be classified deliberate and re-base the
# anchor — the  ratchet re-entering through the re-base branch.
DEFAULT_CAP_RATIO = 3.0

_CONFIG_REL = Path("core") / "config" / "aspirations.yaml"


def _config_path() -> Path:
    """core/config/aspirations.yaml, resolved from THIS file's location.

    Deliberately not routed through _paths.CONFIG_DIR: this module is imported
    by the daemon endpoint as well as the CLI, and the daemon must not acquire a
    dependency on the CLI path helper just to read a framework config file that
    is always a fixed distance from this source file.
    """
    return Path(__file__).resolve().parents[2] / _CONFIG_REL


def cargo_cult_cap_ratio() -> float:
    """cargo_cult.cap_ratio from core/config/aspirations.yaml (default 3.0).

    Reads the SAME config block cargo-cult-detector._load_detector_config reads,
    so the deliberate-raise discriminator can never disagree with the cap it is
    reasoning about.

    Fail-SAFE, never raises: this sits on the goal-write hot path, and the
    detector's own loader carries the identical never-refuse-to-run contract.
    A read failure lands both sides on DEFAULT_CAP_RATIO, so they stay in
    agreement even when the config is unreadable.
    """
    try:
        import yaml
        with open(_config_path(), "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        ratio = float((cfg.get("cargo_cult") or {}).get(
            "cap_ratio", DEFAULT_CAP_RATIO))
    except Exception:
        return DEFAULT_CAP_RATIO
    # A non-positive ratio would make EVERY raise "deliberate" (the ratchet) and
    # a NaN would make every comparison False (silently inert). Neither failure
    # is visible at the write, so refuse both here.
    if not (ratio > 0) or ratio != ratio:
        return DEFAULT_CAP_RATIO
    return ratio


def is_deliberate_raise(anchor, new_interval) -> bool:
    """True when new_interval is above what any auto-extension could produce.

    Both arguments are taken straight off the goal record, so non-numeric,
    bool (a bool is an int in Python — `True > 0` is True), and non-positive
    values are all rejected rather than coerced. Returns False on anything it
    cannot prove, which keeps the anchor immutable in every ambiguous case.
    """
    for v in (anchor, new_interval):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return False
    if not (anchor > 0) or not (new_interval > 0):
        return False
    return new_interval > anchor * cargo_cult_cap_ratio()
