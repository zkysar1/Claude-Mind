"""Absent-verification-outcomes advisory ().

Emits a WARN when a goal is filed with no `verification.outcomes`. Such a goal
can only ever be ASSERTED complete: Phase-5 verify, the learning gate and
pre-completion review are all anchored on the structured field, so with none
present they degrade to narrative and the executor invents the acceptance
criteria at close time -- precisely when they are least impartial, having
already done the work.

WARN, NEVER BLOCK. Fast-capture paths (from-followup, watchdog probes) file
deliberately thin goals so the work is not lost; refusing them would lose more
than the rigor gains. Same advisory posture as the description-length and
user-leg-scope siblings: visibility, not enforcement. rc stays 0 and the goal
is filed.

MEASURED (omni, ZDS, 2026-07-31) on three such goals in one session --
g-001-289 (auto-filed by a watchdog probe), g-001-259 and g-022-60 (both
agent-authored). The distribution is the point: only ONE came from a probe, so
this is not a gap in one filer. The FILING PATH permits it, so every author can
produce an unverifiable goal and two of three here did.

RELATIONSHIP TO gates.prose_verification -- they are complementary, not
overlapping, and the split is deliberate:

    prose_verification : description ADVERTISES criteria ("Verification
                         outcomes:", "Acceptance criteria:", ...) but the
                         structured field is empty  ->  BLOCK
    this gate          : structured field is empty AND the description
                         advertises nothing            ->  WARN

A goal carrying a prose marker is therefore left entirely to prose_verification
(which either blocks it or finds real checks); warning here as well would
double-report one defect and put an advisory in front of a hard block. The
marker set and the code-region stripping are IMPORTED from that module rather
than restated, so a marker added there is honoured here automatically -- the
duplication that produced the g-115-1440 CLI/daemon split is exactly what this
import avoids (guard-547).

WHY THE MESSAGE DOES NOT SAY "this goal has no acceptance criteria": it may
well have them. guard-2328 measured a goal (g-115-3132) whose verification was
empty while its description carried a 5-item VERIFY list. What is true, and
what the message says, is narrower and load-bearing: the verify TOOLING reads
only the structured field (guard-3649), so prose criteria -- however good --
cannot be machine-checked at close.

Public API:
    evaluate(goal) -> dict

Return shape:
    {
      "warned": bool,
      "message": str | None,   # populated only when warned
      "reason": str,           # why it did/didn't warn, for callers + tests
    }

Caller emits `message` wherever it emits its other advisories. The daemon
appends it to the response's `warnings` array, which the wrapper re-emits to
STDERR while keeping stdout pure JSON -- that channel separation is what makes
this safe to add (guard-659: a stderr line merged into stdout corrupts every
caller that pipes the wrapper into json.loads).

Daemon safety: pure function, no I/O, no env reads, no telemetry.
"""
from __future__ import annotations

from typing import Any, Dict

from gates.prose_verification import ALL_PROSE_MARKERS, _strip_code_regions


def _has_outcomes(goal: Dict[str, Any]) -> bool:
    """True when verification.outcomes carries at least one non-empty entry.

    A present-but-empty list, a list of blank strings, and an absent
    verification block are all the SAME defect from the verifier's point of
    view -- there is nothing to check a close against -- so they are treated
    identically rather than distinguished.
    """
    verification = goal.get("verification")
    if not isinstance(verification, dict):
        return False
    outcomes = verification.get("outcomes")
    if not isinstance(outcomes, (list, tuple)):
        return False
    return any(str(o).strip() for o in outcomes)


def _advertises_criteria(goal: Dict[str, Any]) -> bool:
    """True when the description carries a prose criteria header.

    Delegates to prose_verification's marker set and code-stripping so a
    description that merely QUOTES a marker (documenting a gate, pasting a
    template) does not count -- the same guard-1668 protection that module
    already implements, reused rather than re-derived.
    """
    desc = goal.get("description")
    if not isinstance(desc, str) or not desc:
        return False
    prose = _strip_code_regions(desc)
    return any(marker in prose for marker in ALL_PROSE_MARKERS)


def evaluate(goal: Dict[str, Any]) -> Dict[str, Any]:
    """Detect a goal filed with no machine-checkable verification outcomes."""
    gid = goal.get("id") or "<unassigned>"

    if _has_outcomes(goal):
        return {"warned": False, "message": None, "reason": "outcomes-present"}

    if _advertises_criteria(goal):
        # Owned by gates.prose_verification — see the module docstring.
        return {
            "warned": False,
            "message": None,
            "reason": "prose-advertised-owned-by-prose-verification",
        }

    return {
        "warned": True,
        "reason": "outcomes-absent",
        "message": (
            f"[verification-outcomes-absent] ADVISORY: goal {gid} was filed with no "
            f"verification.outcomes, so it cannot be machine-checked at close — Phase-5 "
            f"verify, the learning gate and pre-completion review all read ONLY the "
            f"structured field (guard-3649), which means criteria written as prose in the "
            f"description do NOT count however good they are (guard-2328). The goal IS "
            f"filed and rc is 0; this is advisory. Add outcomes with: "
            f"aspirations-update-goal.sh --source <world|agent> {gid} verification "
            f"'{{\"outcomes\": [\"...\"], \"checks\": [], \"preconditions\": []}}'"
        ),
    }
