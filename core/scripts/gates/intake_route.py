"""Intake routing gate (, B2) — goal-intake-management.md §3 + §8.

The candidate tier's front door. The head of a NEW goal's origin_signal
decides whether the goal is filed as an ordinary work item or parked as a
`candidate` for grooming (spec §5). It is a MUTATOR in the daemon add-goal
battery, slotted right AFTER the origin-signal gate (whose Layer-D
auto-derive may have just filled origin_signal), at both write sites:

  - mind_api/src/endpoints/aspirations_write.py :: _run_add_goal_pipeline
  - mind_api/src/endpoints/aspirations_write.py :: add (per goal of a batch)

The CLI add path runs no origin-signal gate (wrappers are daemon-only since
the 2026-05-14 cutover), so there is no CLI twin to keep in step.

THE TABLE (§3, BINDING):

  flag off                            -> the filer's status, else pending
  world-source user context           -> pending    (the owner never queues)
  head in exempt_origins              -> pending    (I3: detection lanes)
  head maintain + status completed    -> completed  (inline-fix bookkeeping)
  head in candidate_origins AND a resolve goal (hypothesis_id, or skill
  /review-hypotheses as its first token) -> pending  (g-353-185: time-gated)
  head in candidate_origins           -> candidate
  any other head                      -> pending    (I1: unknown fails open)

ONLY `candidate` CHANGES A GOAL. apply() writes the verdict back only when it
is "candidate"; every other verdict leaves the filer's status exactly as it
arrived (I2: creation is never refused, only routed). With the flag off no
verdict is ever "candidate", so nothing is written at all, which is what makes
flag-off byte-identical to the battery without this step.

CONFIG (§8): the `candidate_tier` block of core/config/aspirations.yaml is the
only source of the flag and of the two origin lists. A missing file, block or
`enabled` key reads as OFF, silently (rb-1918: a gate that ships before its flag
is turned on must read the missing key as False). A config that cannot be USED
also reads as OFF but says so on stderr, so it is not byte-identical to a flag
that is off (I1 fails open; guard-424, rb-6115): a file that cannot be read or
parsed, a document that is not a mapping, or an `enabled: true` whose origin
lists are not lists. Only a literal `enabled: true` turns the gate on.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

from gates.origin_signal import world_user_context

CANDIDATE = "candidate"

OFF: Dict[str, Any] = {
    "enabled": False,
    "candidate_origins": frozenset(),
    "exempt_origins": frozenset(),
}


def _degraded(cfg_path: Path, why: str) -> Dict[str, Any]:
    """OFF, with one stderr line saying why. The gate fails open (I1), but a config it
    could not use must not be byte-identical to a flag that is simply off (guard-424,
    rb-6115): once B6 flips the flag, a broken file would otherwise read as a kill
    switch nobody pulled."""
    print(f"[intake-route] WARN: {cfg_path.name} {why}: the candidate tier reads OFF "
          f"(I1, fail open)", file=sys.stderr)
    return dict(OFF)


def load_config(project_root) -> Dict[str, Any]:
    """The `candidate_tier` block of core/config/aspirations.yaml, or OFF."""
    import yaml
    cfg_path = Path(project_root) / "core" / "config" / "aspirations.yaml"
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f)
    except FileNotFoundError:
        return dict(OFF)   # a config that predates the block: the documented dark default
    except Exception as exc:  # noqa: BLE001 - OSError, YAMLError, UnicodeDecodeError: all OFF
        return _degraded(cfg_path, f"cannot be read ({type(exc).__name__})")
    # This runs on EVERY add-goal, to read the flag, so a document that is not a
    # mapping (a list, a scalar) must read as OFF too and never raise (I1).
    if doc is not None and not isinstance(doc, dict):
        return _degraded(cfg_path, f"is a {type(doc).__name__}, not a mapping")
    block = (doc or {}).get("candidate_tier")
    if not isinstance(block, dict) or block.get("enabled") is not True:
        return dict(OFF)
    cand = block.get("candidate_origins")
    exempt = block.get("exempt_origins")
    if not isinstance(cand, list) or not isinstance(exempt, list):
        return _degraded(cfg_path, "enables the tier but its origin lists are not lists")
    return {
        "enabled": True,
        "candidate_origins": frozenset(str(x) for x in cand),
        "exempt_origins": frozenset(str(x) for x in exempt),
    }


def route_intake(goal: Dict[str, Any], *, config: Dict[str, Any],
                 user_context: bool) -> str:
    """The status `goal` should be filed with: the §3 pseudocode, row for row."""
    status = goal.get("status")
    if not config.get("enabled"):
        return status or "pending"
    if user_context:
        return "pending"
    head = str(goal.get("origin_signal") or "").split(":")[0]
    if head in config["exempt_origins"]:
        return "pending"
    if head in config["candidate_origins"]:
        if head == "maintain" and status == "completed":
            return "completed"
        # A resolve goal is time-gated by its own window: a candidate sits past
        # resolves_by with no reviewer (). The sq-009 handler files `skill`
        # WITH args ("/review-hypotheses --hypothesis <id>"; 21 of 21 candidates on
        # 2026-10-06), so match its first token, never the whole string.
        skill = str(goal.get("skill") or "").split()
        if goal.get("hypothesis_id") or skill[:1] == ["/review-hypotheses"]:
            return "pending"
        return CANDIDATE
    return "pending"


def apply(goal: Dict[str, Any], *, config: Dict[str, Any], source,
          agent_name) -> str:
    """Route one goal in place and return the verdict. Only "candidate" is written."""
    verdict = route_intake(goal, config=config,
                           user_context=world_user_context(source, agent_name))
    if verdict == CANDIDATE:
        goal["status"] = CANDIDATE
    return verdict
