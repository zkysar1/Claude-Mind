""" — guardrail `severity` is case-normalized AT THE WRITE.

WHY THIS EXISTS. `severity` was in GUARD_KNOWN_FIELDS as a NAME allowlist with
no value check, so any string passed straight through. The corpus was migrated
to uppercase ONCE, by hand, and then re-accumulated lowercase immediately:
measured 2026-08-09 on cc-07, 198 of 797 severity-bearing active records were
non-canonical, spread evenly across every day since the migration (~28/day).

The two things that were NOT measured when this goal was filed, and are now:

  1. WHICH writer emits lowercase — no single one. `guardrails-add.sh` never
     mentions the field (`grep -n severity` on it returns nothing); severity is
     caller-composed JSON that the wrapper forwards blind. The 198 trace back
     to many goals (g-326-85:10, g-001-02:5, g-001-05:3, ...). So "fix the
     caller" was never available and a re-sweep would re-run forever.
  2. WHETHER an enum validator already existed and was merely unwired — it did
     not. `severity` appeared in store_registry.py at exactly one place, the
     name allowlist. This is a new check, not a rewiring.

WHAT IS PINNED HERE is the ORDER (normalize, then validate the normalized
value) and the COVERAGE (every write path reaches it). Order is the whole
design: 100% of the observed defect was case-only, so normalize-then-validate
repairs 198/198 with zero refusals, while a genuinely wrong value still fails
loudly. Reversing it would refuse 198 legitimate writes.

WHAT IS NOT PINNED: that a periodic re-sweep never runs again. Nothing prevents
one; this only removes the need for it.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "mind_api" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import store_registry as sr  # noqa: E402


def _rec(**over):
    """A minimal record that passes every OTHER check in validate_guard_record,
    so a failure here can only be about severity."""
    rec = {
        "id": "guard-001",
        "rule": "r",
        "category": "c",
        "trigger_condition": "t",
        "source": "s",
        "status": "active",
    }
    rec.update(over)
    return rec


def _validate(rec):
    sr.validate_guard_record(None, rec)
    return rec


class TestNormalization:

    @pytest.mark.parametrize("given", ["high", "HIGH", "High", "hIgH", " high ", "high\n"])
    def test_every_casing_and_surrounding_space_lands_as_one_canonical_value(self, given):
        """The 198 measured records were case-only; whitespace is folded too
        because a trailing newline from a shell heredoc is the same defect
        wearing different clothes."""
        assert _validate(_rec(severity=given))["severity"] == "HIGH"

    @pytest.mark.parametrize("canonical", ["CRITICAL", "HIGH", "MEDIUM", "LOW"])
    def test_an_already_canonical_value_is_unchanged(self, canonical):
        assert _validate(_rec(severity=canonical))["severity"] == canonical

    def test_all_four_canonical_values_are_reachable_from_lowercase(self):
        """Guards the enum itself: a typo'd constant would refuse a value the
        corpus legitimately holds. 15 CRITICAL / 240 HIGH / 331 MEDIUM / 13 LOW
        are live in the store right now, so all four must survive."""
        for low in ("critical", "high", "medium", "low"):
            assert _validate(_rec(severity=low))["severity"] == low.upper()


class TestRefusal:

    @pytest.mark.parametrize("bad", ["urgent", "P1", "sev1", "", "  ", "highest", "MED"])
    def test_a_value_that_is_not_merely_miscased_is_REFUSED(self, bad):
        """The half that normalization alone would lose. Without this, the next
        silent variant simply replaces the one just fixed."""
        with pytest.raises(ValueError, match="[Ii]nvalid severity"):
            _validate(_rec(severity=bad))

    @pytest.mark.parametrize("bad", [3, ["HIGH"], {"level": "HIGH"}, True])
    def test_a_non_string_severity_is_refused_not_coerced(self, bad):
        """`.upper()` on a non-string raises AttributeError, which surfaces as a
        500 rather than a validation error. The explicit type check is what
        makes the refusal legible to the caller."""
        with pytest.raises(ValueError, match="[Ii]nvalid severity"):
            _validate(_rec(severity=bad))

    def test_the_refusal_message_names_the_accepted_values_and_the_case_rule(self):
        """A refusal a caller cannot act on gets worked around. The message must
        say both WHAT is accepted and that case is already handled — otherwise
        the obvious reaction to 'invalid severity: high' is to conclude the enum
        is lowercase-hostile and drop the field."""
        with pytest.raises(ValueError) as ei:
            _validate(_rec(severity="urgent"))
        msg = str(ei.value)
        for v in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
            assert v in msg, f"refusal message omits the accepted value {v}: {msg!r}"
        assert "high" in msg, (
            "refusal message does not tell the caller that case is normalized, so "
            f"a lowercase-but-valid value looks equally rejected: {msg!r}")


class TestAbsenceStaysLegal:

    def test_a_record_with_no_severity_key_passes_untouched(self):
        """2046 of 2843 active records carry no severity at all. Requiring it
        here would refuse the majority of writes for a field nothing has ever
        required — a far larger regression than the one being fixed."""
        rec = _validate(_rec())
        assert "severity" not in rec

    def test_an_explicit_null_severity_is_preserved_as_null(self):
        """Distinct from absence: a caller that explicitly clears the field must
        not have it silently refused, and must not have `None` stringified."""
        assert _validate(_rec(severity=None))["severity"] is None


class TestItIsWiredWhereWritesActuallyGo:
    """Coverage, not behaviour. A validator nobody calls is the defect this goal
    was filed about, one layer up (guard-1943: pinning the writer says nothing
    about the wiring)."""

    def test_the_guardrails_store_routes_its_writes_through_this_validator(self):
        spec = sr.STORE_REGISTRY["guardrails"]
        assert spec.validate is sr.validate_guard_record, (
            "the guardrails StoreSpec no longer validates through "
            "validate_guard_record — severity normalization is now unreachable "
            "regardless of how green the tests above are")

    def test_validate_guard_record_actually_invokes_the_normalizer(self):
        """Mutation-facing: deleting the `_normalize_severity(rec)` call from
        validate_guard_record leaves every test above passing ONLY if they call
        the normalizer directly. They do not — they go through the validator —
        so this is belt-and-braces on the seam itself."""
        src = (SRC / "store_registry.py").read_text(encoding="utf-8")
        body = src[src.index("def validate_guard_record"):]
        body = body[:body.index("\ndef ", 1)]
        assert "_normalize_severity(rec)" in body, (
            "validate_guard_record no longer calls _normalize_severity")

    @pytest.mark.parametrize("endpoint", ["append", "replace", "set_field"])
    def test_every_write_endpoint_runs_spec_validate(self, endpoint):
        """The single-call-site claim in the code comment, checked rather than
        asserted. If a new write path lands without spec.validate, severity
        drifts again through that door alone and nothing else here would notice.
        """
        src = (SRC / "endpoints" / "store.py").read_text(encoding="utf-8")
        i = src.index(f"def {endpoint}(ctx)")
        nxt = src.find("\ndef ", i + 1)
        body = src[i:nxt if nxt != -1 else len(src)]
        assert "spec.validate" in body, (
            f"store.py::{endpoint} does not run spec.validate, so guardrail "
            "writes through it bypass severity normalization entirely")
