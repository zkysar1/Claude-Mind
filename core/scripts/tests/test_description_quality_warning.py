"""test_description_quality_warning.py — regression test for .

Verifies that goal-duplication-gate.py emits a `description_quality_warning`
tag in its JSON output when the goal's description has fewer non-stopword
tokens than the title. The tag is INFORMATIONAL ONLY — it MUST NOT change
the gate's `would_block` verdict.

Cases covered:
  1. Long title + SHORT description (1 non-stopword token) →
     description_quality_warning=True; would_block unchanged
  2. Long title + LONG description (more tokens than title) →
     description_quality_warning absent; would_block unchanged
  3. Recurring goal (recurring=true) + same shape as case 1 →
     description_quality_warning absent (recurring exempt)
  4. Empty description + non-empty title → description_quality_warning=True
     (desc_tokens=0 < title_tokens>0)

Inputs are crafted to avoid overlapping with any real recent_completions or
git activity — no backup/restore needed. The fixed-token-count assertions
hold against whatever live state exists, because the warning check only
compares title vs description tokens (independent of overlap detection).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
GATE_PY = CORE_SCRIPTS / "goal-duplication-gate.py"


def _run_gate(goal: dict) -> dict:
    """Invoke the gate as a subprocess. Returns parsed JSON output."""
    env = os.environ.copy()
    env.setdefault("MIND_AGENT", "alpha")
    proc = subprocess.run(
        [sys.executable, str(GATE_PY)],
        input=json.dumps(goal),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    if proc.returncode not in (0, 1):
        raise RuntimeError(
            f"goal-duplication-gate exit {proc.returncode}: "
            f"{(proc.stderr or proc.stdout).strip()[:300]}"
        )
    return json.loads(proc.stdout)


def main() -> int:
    failures: list[str] = []

    # ── Case 1: long title + short description → warning fires ───────────
    # Title has 4+ non-stopword tokens (>5 chars each, not stopwords);
    # description has 0-1 non-stopword tokens. Picked vocabulary that won't
    # collide with normal recent_completions (made-up identifiers).
    case1 = {
        "title": "investigate xylophone concierge bromeliad zucchini",
        "description": "spec",
        "participants": ["agent"],
        "source": "world",
        "origin_signal": "test:g-115-336-c1",
    }
    r1 = _run_gate(case1)
    if r1.get("description_quality_warning") is not True:
        failures.append(
            f"CASE 1: description_quality_warning expected True, "
            f"got {r1.get('description_quality_warning')!r}. "
            f"reason={r1.get('description_quality_reason')!r}"
        )

    # ── Case 2: long title + long description → warning absent ───────────
    # Description has MORE non-stopword tokens than the title.
    case2 = {
        "title": "investigate xylophone",
        "description": (
            "Detailed investigation of xylophone concierge bromeliad "
            "zucchini accordion harlequin matriarch tessellation "
            "labyrinth mosaic architecture. Includes verification "
            "ouroboros snapshot."
        ),
        "participants": ["agent"],
        "source": "world",
        "origin_signal": "test:g-115-336-c2",
    }
    r2 = _run_gate(case2)
    if r2.get("description_quality_warning"):
        failures.append(
            f"CASE 2: description_quality_warning expected absent/False, "
            f"got {r2.get('description_quality_warning')!r}. "
            f"reason={r2.get('description_quality_reason')!r}"
        )

    # ── Case 3: recurring goal + short description → warning absent ──────
    # Same shape as case 1, but recurring=true exempts it (title-as-spec
    # pattern for infrastructure checks).
    case3 = {
        "title": "investigate xylophone concierge bromeliad zucchini",
        "description": "spec",
        "participants": ["agent"],
        "source": "world",
        "recurring": True,
        "origin_signal": "test:g-115-336-c3",
    }
    r3 = _run_gate(case3)
    if r3.get("description_quality_warning"):
        failures.append(
            f"CASE 3 (recurring): description_quality_warning expected absent, "
            f"got {r3.get('description_quality_warning')!r}"
        )

    # ── Case 4: empty description + non-empty title → warning fires ──────
    # Boundary: 0 desc tokens < title tokens. Same exempt-from-recurring path.
    case4 = {
        "title": "investigate xylophone concierge bromeliad",
        "description": "",
        "participants": ["agent"],
        "source": "world",
        "origin_signal": "test:g-115-336-c4",
    }
    r4 = _run_gate(case4)
    if r4.get("description_quality_warning") is not True:
        failures.append(
            f"CASE 4 (empty desc): description_quality_warning expected True, "
            f"got {r4.get('description_quality_warning')!r}"
        )

    # ── Cross-case verdict invariance (cases 1, 3, 4 only) ────────────────
    # Cases 1, 3, 4 use SHORT descriptions ("spec" or "") that cannot
    # produce keyword overlap with real recent_completions. would_block must
    # remain False on these — if the warning emission accidentally mutated
    # verdict logic, would_block would flip.
    #
    # Case 2 is excluded from this check: its long description uses common
    # vocabulary that may overlap with ambient recent_completions in any
    # given run, which is a property of LIVE world state, not of the
    # warning-emission code path under test.
    for label, r in (("CASE 1", r1), ("CASE 3", r3), ("CASE 4", r4)):
        if r.get("would_block"):
            failures.append(
                f"{label}: would_block unexpectedly True — warning emission "
                f"may have mutated verdict logic. reason={r.get('reason')!r}"
            )

    if failures:
        print("FAIL: " + str(len(failures)) + " case(s)", file=sys.stderr)
        for f in failures:
            print("  - " + f, file=sys.stderr)
        return 1
    print("PASS: 4/4 description_quality_warning cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
