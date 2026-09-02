"""Both arms of the unknown-key WARN gate on hypothesis records ().

The pipeline store had NO unknown-key check on any write path, so a caller's
extra key was stored verbatim and stayed invisible until a corpus audit. Census
2026-09-01 over the full live corpus (1,844 deduped records, read through
`pipeline-read.sh --stage <each>`): 363 distinct keys against a 15-key writer
set, i.e. 348 caller-supplied pass-through keys.

WARN, NOT REFUSE, and the tests below pin that distinction because it is the
whole decision. Banded by evidence, 136 of those 348 keys are written by
framework code (101) or prescribed by a SKILL.md/rule/config (35) — 21,691
record-key occurrences across the archive, resolve, reflect and replay paths.
Refusing would fail all four on day one. The remaining 212 keys / 1,323
occurrences are the genuine drift set, over half of them appearing on exactly
one record, and dominated by SPELLING VARIANTS of fields the store already has
(`premortem` 304 vs `pre_mortem` 50 vs `adversarial_premortem` 13;
`reflected_date` 1048 vs `reflected_at` 13 vs `reflected_on` 13).

Both directions are covered per guard-385 / guard-1660:
  - WARN + RETURN the unknown set on a bogus key, WITHOUT raising.
  - SILENT + return None when every key is known.

The does-not-raise assertion is the load-bearing one. A future change that
"tightens" this into a refusal would break live callers, and it would pass any
test that only checked "the bogus key was detected".
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
for _p in (str(CORE_SCRIPTS), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pipeline  # noqa: E402
import _pipeline_fields  # noqa: E402
from mind_api.src.world import pipeline_write  # noqa: E402


def _rec(**extra) -> dict:
    """Minimal record that passes every EXISTING check, plus whatever is added."""
    rec = {
        "id": "2026-09-01_unknown-field-warn-fixture",
        "title": "unknown field warn fixture",
        "stage": "resolved",
        "horizon": "short",
        "type": "calibration",
        "confidence": 0.5,
        "category": "framework-patterns",
        "formed_date": "2026-09-01",
        "position": "YES — a narrative stance long enough to be a genuine claim",
    }
    rec.update(extra)
    return rec


# The CLI and daemon validators are separate implementations that must stay in
# lockstep (guard-2323). Parametrizing over both is what catches a fix applied
# to only one of them — the same idiom test_pipeline_position_type_gate.py uses.
VALIDATORS = [
    pytest.param(pipeline.validate_record, id="cli"),
    pytest.param(pipeline_write._validate_record, id="daemon"),
]


# ---------------------------------------------------------------------------
# Arm 1 — a deliberately bogus key WARNS and is NOT refused
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("validate", VALIDATORS)
def test_bogus_key_warns_but_does_not_refuse(validate, capsys):
    validate(_rec(zzz_definitely_not_a_real_field="x"))
    err = capsys.readouterr().err
    assert "[pipeline-unknown-field]" in err
    assert "zzz_definitely_not_a_real_field" in err


@pytest.mark.parametrize("validate", VALIDATORS)
def test_bogus_key_write_still_succeeds(validate):
    """The refusal-regression guard: tightening this to a raise breaks live callers."""
    validate(_rec(zzz_definitely_not_a_real_field="x"))  # must not raise


def test_helper_returns_the_unknown_set():
    unknown = _pipeline_fields.warn_unknown_fields(
        _rec(zzz_definitely_not_a_real_field="x"), source="test")
    assert unknown == {"zzz_definitely_not_a_real_field"}


# ---------------------------------------------------------------------------
# Arm 2 — a record of only known keys is SILENT
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("validate", VALIDATORS)
def test_known_only_record_is_silent(validate, capsys):
    validate(_rec())
    assert "[pipeline-unknown-field]" not in capsys.readouterr().err


def test_helper_returns_none_when_clean():
    assert _pipeline_fields.warn_unknown_fields(_rec(), source="test") is None


# ---------------------------------------------------------------------------
# The allowlist itself
# ---------------------------------------------------------------------------

def test_writer_required_and_default_fields_are_allowlisted():
    """A field the writer STAMPS must never warn — that would fire on every write."""
    stamped = pipeline.REQUIRED_FIELDS | set(pipeline.DEFAULT_FIELDS)
    assert stamped <= _pipeline_fields.PIPELINE_KNOWN_FIELDS


def test_high_frequency_corpus_fields_are_allowlisted():
    """Regression guard on the measured population.

    These are the band-A keys carried by >25% of the live corpus, written by
    framework code on the archive / resolve / reflect / replay paths. If any
    drops out of the allowlist the warn arm becomes noise on every second write,
    which is how a useful signal gets silenced wholesale.
    """
    for field in ("resolves_by", "claim", "measurement_channel", "outcome_date",
                  "archived_date", "outcome_detail", "reflected_date",
                  "replay_metadata", "resolved_at", "resolved_by",
                  "resolution_method", "source_goal"):
        assert field in _pipeline_fields.PIPELINE_KNOWN_FIELDS, field


def test_both_writers_share_one_allowlist():
    """Parity is structural, not a discipline (guard-2323)."""
    assert pipeline.warn_unknown_fields is pipeline_write.warn_unknown_fields
