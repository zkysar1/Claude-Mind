"""Pins the token-estimate calibration in build-agent-context.py ().

THE DEFECT. `estimate_tokens` divided chars by 4. That estimate GATES a real
budget: `build_context()` runs two trimming loops
(`while matched_nodes and estimate_tokens(result) > max_tokens` and the same
over `cat_rb`), so the value decides how much context a spawned agent receives
via `aspirations-execute` Phase 4 spawn-time injection.

Dividing by 4 UNDERESTIMATED this script's own output by 1.67-1.71x, and it did
so in the UNSAFE direction: an undercount stops trimming early and emits a block
LARGER than the caller budgeted, which the consumer then truncates at the far
end silently. The fix sets CHARS_PER_TOKEN = 2.3, deliberately below the
measured floor, so the estimate errs HIGH and trims one node too many instead.

MEASURED 2026-09-03 (alpha, DESKTOP-O91DLK2), real builds of this script:
  - `--category infrastructure --role executor`: 203,547 chars -> 85,232 tokens.
    GROUND TRUTH, not a proxy: the Read tool's own truncation notice reported
    the token count for that exact file.
  - framework-maintenance (149,607 chars) and knowledge-management (30,486
    chars) counted with tiktoken and corrected by the factor the ground-truth
    file calibrates (x1.6903) -> 62,621 and 13,035 tokens.
Independent corroboration: rb-9606 measures chars/4 at ~40% low on id-dense
Mind text — the same 1.69x, arrived at from different content.

WHY THE SAMPLES ARE STORED AS (chars, tokens) PAIRS rather than fixtures: the
generated files were scratch and drain by design, and a fixture regenerated from
today's guardrail corpus would drift as that corpus grows. The pairs ARE the
measurement, and they are what a future re-calibration must beat.

guard-955: pinned local so no case can reach a production store.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

os.environ["STORAGE_BACKEND"] = "local"

_SCRIPTS = Path(__file__).resolve().parents[1]
_TARGET = _SCRIPTS / "build-agent-context.py"


def _load():
    """Import the hyphenated script as a module (no package path exists)."""
    sys.path.insert(0, str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location("_bac_under_test", _TARGET)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# (label, chars, measured Claude tokens) — see module docstring for provenance.
MEASURED = [
    ("infrastructure", 203_547, 85_232),
    ("framework-maintenance", 149_607, 62_621),
    ("knowledge-management", 30_486, 13_035),
]

# The lowest chars/token any measured sample exhibited. The constant must sit at
# or below this or the estimate can run low on the densest observed content.
MEASURED_FLOOR = min(chars / tokens for _, chars, tokens in MEASURED)  # 2.3379


def test_estimate_never_underestimates_measured_samples():
    """THE LOAD-BEARING PROPERTY. Under-estimating overflows the budget silently.

    Stated as a direction, not a number, so a future re-calibration to any
    safe constant still passes and only an unsafe one fails.
    """
    bac = _load()
    for label, chars, truth in MEASURED:
        est = bac.estimate_tokens("x" * chars)
        assert est >= truth, (
            f"{label}: estimate {est} < measured {truth} tokens — the estimate "
            f"runs LOW, so trimming stops early and the emitted block exceeds "
            f"max_tokens. CHARS_PER_TOKEN={bac.CHARS_PER_TOKEN} is above the "
            f"measured floor {MEASURED_FLOOR:.4f}."
        )


def test_old_divisor_would_have_failed_this_suite():
    """POSITIVE CONTROL. Proves the samples above can actually discriminate.

    Without this, a bug that made estimate_tokens enormous would satisfy the
    direction test while telling us nothing about whether the corpus is
    sensitive. The pre-fix divisor of 4 must fail on every sample.
    """
    for label, chars, truth in MEASURED:
        old = chars // 4
        assert old < truth, (
            f"{label}: the pre-fix chars//4 estimate ({old}) is not below the "
            f"measured {truth}; this sample cannot discriminate the defect."
        )
        assert truth / old > 1.5, (
            f"{label}: pre-fix underestimate factor {truth / old:.2f}x is "
            f"smaller than the 1.67-1.71x that was measured — re-verify the "
            f"stored pairs before trusting this suite."
        )


def test_constant_is_at_or_below_measured_floor():
    bac = _load()
    assert bac.CHARS_PER_TOKEN <= MEASURED_FLOOR, (
        f"CHARS_PER_TOKEN={bac.CHARS_PER_TOKEN} exceeds the measured floor "
        f"{MEASURED_FLOOR:.4f} chars/token; the estimate would run low on the "
        f"densest content actually observed."
    )


def test_overestimate_stays_bounded():
    """The safe direction is not a licence to trim everything.

    An absurdly small constant would pass the direction test while gutting the
    knowledge and reasoning sections on every build.
    """
    bac = _load()
    for label, chars, truth in MEASURED:
        ratio = bac.estimate_tokens("x" * chars) / truth
        assert ratio <= 1.25, (
            f"{label}: estimate is {ratio:.2f}x the measured token count — "
            f"over-conservative enough to trim context that fits."
        )


def test_edge_cases_preserved():
    """Falsy input returned 0 before the change and must still."""
    bac = _load()
    assert bac.estimate_tokens("") == 0
    assert bac.estimate_tokens(None) == 0
    assert isinstance(bac.estimate_tokens("abc"), int)


def test_estimate_still_gates_the_trimming_loops():
    """WIRING. A calibrated constant that nothing consults is inert.

    Pins that both budget-enforcement loops still call estimate_tokens, so a
    refactor cannot leave the constant correct and the gate disconnected
    (guard-1943: pinning a writer says nothing about the wiring).
    """
    src = _TARGET.read_text(encoding="utf-8")
    assert "while matched_nodes and estimate_tokens(result) > max_tokens:" in src, (
        "the knowledge-trimming loop no longer gates on estimate_tokens"
    )
    assert "while cat_rb and estimate_tokens(result) > max_tokens:" in src, (
        "the reasoning-trimming loop no longer gates on estimate_tokens"
    )
    assert "CHARS_PER_TOKEN" in src


def test_not_unified_with_tree_constant():
    """The two 2.3 constants are DIFFERENT invariants that agree by coincidence.

    tree.py's is measured on tree-node markdown against a ~25k-token Read cap;
    this one on guardrail one-liners against an output budget. A shared helper
    would silently re-tune this site whenever that one is recalibrated
    (guard-3810), so the local definition must stay local and say so.
    """
    src = _TARGET.read_text(encoding="utf-8")
    assert "DO NOT UNIFY" in src, (
        "the warning against unifying this constant with tree.py's has been "
        "removed; without it a future consolidation pass will merge them."
    )
    assert "from tree import" not in src and "import tree" not in src, (
        "build-agent-context.py must not import tree.py's constant"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
