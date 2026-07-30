"""Surprise is DERIVED on write, never accepted from the caller ().

`surprise` is a pure function of (outcome, confidence) — both already on the
record — so a caller-supplied value is not an input, it is a second writer of a
derived field. It drifted: measured across the resolved + archived union (769
records, 391 scoreable), 158 stored values (40.4%) disagreed with the canonical
helper and 80 (20.5%) disagreed by enough to change the /review-hypotheses Step
3.5 branch. 47 of those UNDER-stated, so a mandated broad re-retrieve never ran
— silently, because nothing errors and the record still looks complete.

WHY THESE TESTS GO THROUGH THE REAL HTTP ENDPOINTS rather than calling
`_normalize_record` directly. The bug that motivated half of them is an ORDERING
bug: `update_field` normalizes and THEN assigns `rec[field] = value`, so the
derivation ran one line too early to cover that path. A test that calls the
normalizer itself, or that reconstructs the endpoint's ordering inline, cannot
see that class at all — it would test the reconstruction, pass, and leave the
hole open (guard-1832 "subject absent from the target"; guard-920 "replicate the
literal production shape, not the contract-ideal one"). Driving the endpoint is
what makes the ordering part of the subject under test.

BOTH DIRECTIONS (guard-385/1660): a WRONG caller-supplied value must be
corrected, AND a CORRECT one must round-trip unchanged. A test that only asserts
the first passes just as well against a write path that clobbers every surprise
to a constant.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest


def _post(port: int, path: str, query: dict = None, body: bytes = b"",
          *, agent: str = "alpha"):
    qs = urllib.parse.urlencode(query) if query else ""
    url = f"http://127.0.0.1:{port}{path}?{qs}" if qs else f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if agent:
        req.add_header("X-Mind-Agent", agent)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _read_jsonl(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _find(path: Path, rec_id: str):
    return next(r for r in _read_jsonl(path) if r["id"] == rec_id)


# Records seeded per-test. confidence 0.45 is chosen so the canonical value (6)
# differs from every wrong value asserted against it, AND sits below the
# surprise>=7 promotion threshold in both directions -- so a test that passes
# here is not passing by landing on a boundary.
def _rec(**kw):
    base = {
        "id": "2026-07-29_surprise-derived",
        "title": "Test hypothesis for surprise derivation on write",
        "stage": "discovered",
        "horizon": "session",
        "type": "calibration",
        "confidence": 0.45,
        "position": "YES this is a valid multi-word position claim",
        "formed_date": "2026-07-29",
        "category": "test-cat",
        # >=20 chars: required on any non-discovered record (validate_record).
        "claim": "Surprise is derived on write rather than supplied by callers",
    }
    base.update(kw)
    return base


CONFIRMED_AT_045 = 6      # round((1 - 0.45) * 10)
CORRECTED_AT_050 = 5      # round(0.50 * 10)


@pytest.fixture
def clean_pipeline(running_daemon):
    """Empty live + archive so each test owns its records."""
    project_root, port = running_daemon
    live = project_root / "world" / "pipeline.jsonl"
    live.write_text("", encoding="utf-8")
    (project_root / "world" / "pipeline-archive.jsonl").write_text(
        "", encoding="utf-8")
    return project_root, port, live


# ---------------------------------------------------------------------------
# add -- the caller supplies surprise directly
# ---------------------------------------------------------------------------

def test_add_overrides_wrong_caller_supplied_surprise(clean_pipeline):
    project_root, port, live = clean_pipeline
    rec = _rec(stage="resolved", outcome="CONFIRMED", surprise=3,
               outcome_detail="resolved under g-115-3801 derivation test",
               outcome_date="2026-07-29")

    status, body = _post(port, "/v1/pipeline/add",
                         body=json.dumps(rec).encode("utf-8"))
    assert status == 200, body

    assert _find(live, rec["id"])["surprise"] == CONFIRMED_AT_045


def test_add_leaves_correct_caller_supplied_surprise_unchanged(clean_pipeline):
    """The other direction: a right answer must survive, not just be replaced."""
    project_root, port, live = clean_pipeline
    rec = _rec(stage="resolved", outcome="CONFIRMED",
               surprise=CONFIRMED_AT_045,
               outcome_detail="resolved under g-115-3801 derivation test",
               outcome_date="2026-07-29")

    status, body = _post(port, "/v1/pipeline/add",
                         body=json.dumps(rec).encode("utf-8"))
    assert status == 200, body

    assert _find(live, rec["id"])["surprise"] == CONFIRMED_AT_045


def test_add_unresolved_record_keeps_surprise_null(clean_pipeline):
    """`surprise: None` on an unresolved record must keep meaning "not resolved".

    compute_surprise coerces a missing outcome to a score of 0, and 0 is a real
    band meaning "unsurprising" -- so deriving unconditionally would stamp every
    open hypothesis with a measurement nobody took. The gate is the reason
    derive_surprise returns None rather than compute_surprise's 0.
    """
    project_root, port, live = clean_pipeline
    rec = _rec()  # stage=discovered, no outcome

    status, body = _post(port, "/v1/pipeline/add",
                         body=json.dumps(rec).encode("utf-8"))
    assert status == 200, body

    assert _find(live, rec["id"])["surprise"] is None


def test_add_non_scoreable_outcome_leaves_stored_value_untouched(clean_pipeline):
    """UNRESOLVABLE/EXPIRED are excluded from calibration -- not scored as 0."""
    project_root, port, live = clean_pipeline
    rec = _rec(stage="resolved", outcome="UNRESOLVABLE", surprise=4,
               outcome_detail="could not be resolved either way",
               outcome_date="2026-07-29")

    status, body = _post(port, "/v1/pipeline/add",
                         body=json.dumps(rec).encode("utf-8"))
    assert status == 200, body

    assert _find(live, rec["id"])["surprise"] == 4


def test_add_legacy_surprise_level_name_is_also_derived_over(clean_pipeline):
    """The legacy field name must not smuggle a stale value past the derivation.

    `surprise_level` is renamed to `surprise` by the normalizer. The derivation
    is placed AFTER that rename precisely so the legacy name cannot reintroduce
    a caller value behind it.
    """
    project_root, port, live = clean_pipeline
    rec = _rec(stage="resolved", outcome="CORRECTED", confidence=0.5,
               surprise_level=9,
               outcome_detail="corrected under g-115-3801 derivation test",
               outcome_date="2026-07-29")

    status, body = _post(port, "/v1/pipeline/add",
                         body=json.dumps(rec).encode("utf-8"))
    assert status == 200, body

    stored = _find(live, rec["id"])
    assert stored["surprise"] == CORRECTED_AT_050
    assert "surprise_level" not in stored


# ---------------------------------------------------------------------------
# update-field -- the ORDERING path. These are the two holes that survived the
# first fix: the endpoint calls the normalizer, but assigns AFTER it.
# ---------------------------------------------------------------------------

def _seed_resolved(live: Path, **kw):
    rec = _rec(stage="resolved", outcome="CONFIRMED",
               surprise=CONFIRMED_AT_045,
               outcome_detail="resolved under g-115-3801 derivation test",
               outcome_date="2026-07-29")
    rec.update(kw)
    live.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    return rec


def test_update_field_cannot_write_a_wrong_surprise_directly(clean_pipeline):
    """MEASURED HOLE #1: `--field surprise --value 99` used to land the 99.

    _normalize_record derived 6, then `rec[field] = value` overwrote it one line
    later. The endpoint genuinely called the normalizer the whole time, which is
    why "every writer calls the normalizer" read as sufficient coverage.
    """
    project_root, port, live = clean_pipeline
    rec = _seed_resolved(live)

    status, body = _post(port, "/v1/pipeline/update-field",
                         {"id": rec["id"], "field": "surprise", "value": "99"})
    assert status == 200, body

    assert json.loads(body)["record"]["surprise"] == CONFIRMED_AT_045
    assert _find(live, rec["id"])["surprise"] == CONFIRMED_AT_045


def test_update_field_setting_outcome_rederives_surprise(clean_pipeline):
    """MEASURED HOLE #2: setting `outcome` changes the derivation's own input.

    Resolving via `--field outcome --value CONFIRMED` used to leave surprise at
    None, because the derivation had already run against the pre-resolution
    record. This is the resolution path /review-hypotheses actually uses, so the
    hole sat directly under the field it was meant to protect.
    """
    project_root, port, live = clean_pipeline
    rec = _rec(stage="discovered")           # unresolved: surprise is None
    live.write_text(json.dumps(rec) + "\n", encoding="utf-8")

    status, body = _post(port, "/v1/pipeline/update-field",
                         {"id": rec["id"], "field": "outcome",
                          "value": "CONFIRMED"})
    assert status == 200, body

    assert _find(live, rec["id"])["surprise"] == CONFIRMED_AT_045


def test_update_field_setting_confidence_rederives_surprise(clean_pipeline):
    """confidence is the derivation's other input -- same class as outcome."""
    project_root, port, live = clean_pipeline
    rec = _seed_resolved(live)

    status, body = _post(port, "/v1/pipeline/update-field",
                         {"id": rec["id"], "field": "confidence",
                          "value": "0.5"})
    assert status == 200, body

    stored = _find(live, rec["id"])
    assert stored["confidence"] == 0.5
    assert stored["surprise"] == 5        # round((1 - 0.5) * 10)


def test_update_field_unrelated_field_leaves_surprise_alone(clean_pipeline):
    """Editing an unrelated field must not disturb an already-correct value."""
    project_root, port, live = clean_pipeline
    rec = _seed_resolved(live)

    status, body = _post(port, "/v1/pipeline/update-field",
                         {"id": rec["id"], "field": "reflected",
                          "value": "true"})
    assert status == 200, body

    stored = _find(live, rec["id"])
    assert stored["reflected"] is True
    assert stored["surprise"] == CONFIRMED_AT_045


def test_update_field_on_unresolved_record_keeps_surprise_null(clean_pipeline):
    """An edit to an open hypothesis must not manufacture a score for it."""
    project_root, port, live = clean_pipeline
    rec = _rec(stage="discovered")
    live.write_text(json.dumps(rec) + "\n", encoding="utf-8")

    status, body = _post(port, "/v1/pipeline/update-field",
                         {"id": rec["id"], "field": "confidence",
                          "value": "0.8"})
    assert status == 200, body

    assert _find(live, rec["id"])["surprise"] is None


# ---------------------------------------------------------------------------
# update -- whole-record replace
# ---------------------------------------------------------------------------

def test_update_overrides_wrong_caller_supplied_surprise(clean_pipeline):
    project_root, port, live = clean_pipeline
    rec = _seed_resolved(live)

    sent = dict(rec)
    sent["surprise"] = 1
    status, body = _post(port, "/v1/pipeline/update", {"id": rec["id"]},
                         body=json.dumps(sent).encode("utf-8"))
    assert status == 200, body

    assert _find(live, rec["id"])["surprise"] == CONFIRMED_AT_045


# ---------------------------------------------------------------------------
# The census assertion the goal asks for, scoped to records written after the
# fix: for every scoreable record this test wrote, stored == canonical, so the
# band-changing count over that set is 0 by construction.
# ---------------------------------------------------------------------------

def test_no_write_path_can_store_a_non_canonical_surprise(clean_pipeline):
    """Sweep every write endpoint, then re-run the census over what landed.

    This is the goal's "band-changing count is 0 for records written after the
    fix", made executable: rather than trusting each endpoint's own assertion
    above, re-derive from the canonical helper over the resulting store.
    """
    import sys

    project_root, port, live = clean_pipeline
    root = Path(__file__).resolve().parents[2]
    scripts = str(root / "core" / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from _surprise import derive_surprise

    # add (wrong), add (correct), add (legacy name), add (unresolved)
    written = [
        _rec(id="2026-07-29_census-a", stage="resolved", outcome="CONFIRMED",
             surprise=0, outcome_detail="census record a, g-115-3801",
             outcome_date="2026-07-29"),
        _rec(id="2026-07-29_census-b", stage="resolved", outcome="CORRECTED",
             confidence=0.8, surprise=10, outcome_detail="census record b, g-115-3801",
             outcome_date="2026-07-29"),
        _rec(id="2026-07-29_census-c", stage="resolved", outcome="CONFIRMED",
             confidence=0.2, surprise_level=1, outcome_detail="census record c, g-115-3801",
             outcome_date="2026-07-29"),
        _rec(id="2026-07-29_census-d"),
    ]
    for r in written:
        status, body = _post(port, "/v1/pipeline/add",
                             body=json.dumps(r).encode("utf-8"))
        assert status == 200, body

    # then mutate two of them through update-field
    for rec_id, field, value in [
        ("2026-07-29_census-a", "surprise", "99"),
        ("2026-07-29_census-b", "confidence", "0.35"),
    ]:
        status, body = _post(port, "/v1/pipeline/update-field",
                             {"id": rec_id, "field": field, "value": value})
        assert status == 200, body

    mismatched = []
    for stored in _read_jsonl(live):
        canonical = derive_surprise(stored)
        if canonical is None:
            continue                       # not scoreable -- nothing to check
        if stored.get("surprise") != canonical:
            mismatched.append((stored["id"], stored.get("surprise"), canonical))

    assert mismatched == [], f"non-canonical surprise stored: {mismatched}"
