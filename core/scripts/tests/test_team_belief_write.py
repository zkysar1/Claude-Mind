"""Unit tests for the Theory-of-Mind partner-belief supersede/cap logic ().

Covers `_team_belief.supersede_beliefs` (the pure hygiene rule behind
`team-belief-write.sh`) and the `main()` stdin/argv plumbing. Uses generic
placeholder partner names (partner-a/b/...) so the file is domain-free.
"""
import io
import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import _team_belief as TB  # noqa: E402


# --- supersede_beliefs: the core hygiene rule ---------------------------------

def test_supersede_replaces_prior_belief_about_same_partner():
    current = [{"about": "partner-a", "belief": "old", "confidence": 0.5,
                "last_observed": "2026-06-01T00:00:00"}]
    out = TB.supersede_beliefs(current, "partner-a", "new", 0.6, "2026-06-18T00:00:00")
    # Exactly one entry about partner-a, and it is the new one (one-per-partner).
    about_a = [b for b in out if b["about"] == "partner-a"]
    assert len(about_a) == 1
    assert about_a[0]["belief"] == "new"
    assert about_a[0]["confidence"] == 0.6
    assert about_a[0]["last_observed"] == "2026-06-18T00:00:00"


def test_supersede_preserves_beliefs_about_other_partners():
    current = [
        {"about": "partner-a", "belief": "keep-a", "confidence": 0.4, "last_observed": "t0"},
        {"about": "partner-b", "belief": "old-b", "confidence": 0.5, "last_observed": "t0"},
    ]
    out = TB.supersede_beliefs(current, "partner-b", "new-b", 0.7, "t1")
    by_about = {b["about"]: b for b in out}
    assert by_about["partner-a"]["belief"] == "keep-a"   # untouched
    assert by_about["partner-b"]["belief"] == "new-b"     # superseded
    assert len(out) == 2                                   # no growth


def test_new_belief_has_full_shape():
    out = TB.supersede_beliefs([], "partner-a", "observed x", 0.5, "2026-06-18T01:02:03")
    assert out == [{
        "about": "partner-a", "belief": "observed x",
        "confidence": 0.5, "last_observed": "2026-06-18T01:02:03",
        "domain": None,
        # : bi-temporal interval — valid_from = now_iso, valid_to = None.
        "valid_from": "2026-06-18T01:02:03", "valid_to": None,
    }]


def test_domain_field_recorded_when_provided():
    # : an optional structured domain enables contradiction detection.
    out = TB.supersede_beliefs([], "partner-a", "on framework", 0.6,
                               "2026-06-18T01:02:03", domain="framework-architecture")
    assert out[0]["domain"] == "framework-architecture"


def test_domain_defaults_to_none_additive():
    # Beliefs written without --domain stay free-form (domain=None) — the field
    # is purely additive and never breaks the  consumers.
    out = TB.supersede_beliefs([], "partner-a", "free-form claim", 0.5, "t")
    assert out[0]["domain"] is None


# --- cap ---------------------------------------------------------------------

def test_caps_at_max_beliefs_keeping_most_recent():
    # Seed MAX_BELIEFS distinct partners, then add one more distinct partner.
    current = [
        {"about": f"partner-{i}", "belief": f"b{i}", "confidence": 0.5, "last_observed": "t"}
        for i in range(TB.MAX_BELIEFS)
    ]
    out = TB.supersede_beliefs(current, "partner-overflow", "newest", 0.5, "t")
    assert len(out) == TB.MAX_BELIEFS           # never exceeds the cap
    assert out[-1]["about"] == "partner-overflow"  # newest retained
    assert all(b["about"] != "partner-0" for b in out)  # oldest dropped


def test_cap_respects_custom_max_total():
    current = [{"about": f"p{i}", "belief": "b", "confidence": 0.5, "last_observed": "t"}
               for i in range(3)]
    out = TB.supersede_beliefs(current, "p-new", "b", 0.5, "t", max_total=2)
    assert len(out) == 2
    assert out[-1]["about"] == "p-new"


# --- confidence clamping / fallback ------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (1.5, 1.0),       # above range clamps to 1.0
    (-0.3, 0.0),      # below range clamps to 0.0
    (0.42, 0.42),     # in range preserved
    ("0.8", 0.8),     # numeric string parsed
    ("not-a-number", 0.5),  # non-numeric falls back to calibrated default
    (None, 0.5),      # None falls back
])
def test_confidence_clamp_and_fallback(raw, expected):
    out = TB.supersede_beliefs([], "partner-a", "b", raw, "t")
    assert out[0]["confidence"] == pytest.approx(expected)


# --- defensive handling of malformed `current` -------------------------------

def test_none_current_treated_as_empty():
    out = TB.supersede_beliefs(None, "partner-a", "b", 0.5, "t")
    assert len(out) == 1 and out[0]["about"] == "partner-a"


def test_non_dict_junk_entries_preserved_unless_matching_about():
    current = ["junk", 42, {"about": "partner-a", "belief": "old", "confidence": 0.5,
                            "last_observed": "t"}]
    out = TB.supersede_beliefs(current, "partner-a", "new", 0.5, "t")
    assert "junk" in out and 42 in out          # non-dict junk preserved
    about_a = [b for b in out if isinstance(b, dict) and b.get("about") == "partner-a"]
    assert len(about_a) == 1 and about_a[0]["belief"] == "new"


def test_supersede_drops_all_duplicate_about_entries():
    # A malformed `current` with two entries about the same partner: supersede
    # must drop BOTH, leaving exactly one (the new) entry.
    current = [
        {"about": "partner-a", "belief": "v1", "confidence": 0.5, "last_observed": "t0"},
        {"about": "partner-a", "belief": "v2", "confidence": 0.6, "last_observed": "t1"},
    ]
    out = TB.supersede_beliefs(current, "partner-a", "v3", 0.7, "t2")
    about_a = [b for b in out if isinstance(b, dict) and b.get("about") == "partner-a"]
    assert len(about_a) == 1 and about_a[0]["belief"] == "v3"


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", float("nan"), float("inf")])
def test_non_finite_confidence_falls_back_to_default(raw):
    # float("nan") parses without raising, so the except-clause never fires;
    # the isfinite guard is what enforces the documented 0.5 fallback.
    out = TB.supersede_beliefs([], "partner-a", "b", raw, "t")
    assert out[0]["confidence"] == 0.5


@pytest.mark.parametrize("bad", [{"k": "v"}, "abc", 42])
def test_non_list_current_coerced_to_empty(bad):
    # Truthy non-list inputs must coerce to empty (docstring contract) without
    # crashing or leaking junk (dict keys / string chars / int TypeError).
    out = TB.supersede_beliefs(bad, "partner-a", "b", 0.5, "t")
    assert out == [{"about": "partner-a", "belief": "b", "confidence": 0.5,
                    "last_observed": "t", "domain": None,
                    "valid_from": "t", "valid_to": None}]  #  bi-temporal


# --- main(): stdin/argv plumbing ---------------------------------------------

def _run_main(stdin_text, argv, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
    rc = TB.main(argv)
    out = capsys.readouterr().out.strip()
    return rc, json.loads(out)


def test_main_null_stdin_starts_empty(monkeypatch, capsys):
    # The real-world "missing field" shape: team-state-read returns literal "null".
    rc, parsed = _run_main(
        "null", ["--about", "partner-a", "--belief", "x", "--now", "2026-06-18T00:00:00"],
        monkeypatch, capsys)
    assert rc == 0
    assert parsed == [{"about": "partner-a", "belief": "x", "confidence": 0.5,
                       "last_observed": "2026-06-18T00:00:00", "domain": None,
                       "valid_from": "2026-06-18T00:00:00", "valid_to": None}]  # 


def test_main_empty_stdin_starts_empty(monkeypatch, capsys):
    rc, parsed = _run_main(
        "", ["--about", "partner-a", "--belief", "x", "--now", "t"], monkeypatch, capsys)
    assert rc == 0 and len(parsed) == 1


def test_main_supersedes_existing_list_from_stdin(monkeypatch, capsys):
    existing = json.dumps([{"about": "partner-a", "belief": "old", "confidence": 0.5,
                            "last_observed": "t0"}])
    rc, parsed = _run_main(
        existing, ["--about", "partner-a", "--belief", "new", "--confidence", "0.7",
                   "--now", "t1"], monkeypatch, capsys)
    assert rc == 0
    assert len(parsed) == 1
    assert parsed[0]["belief"] == "new" and parsed[0]["confidence"] == 0.7


def test_main_malformed_json_stdin_starts_empty(monkeypatch, capsys):
    rc, parsed = _run_main(
        "{not json", ["--about", "partner-a", "--belief", "x", "--now", "t"],
        monkeypatch, capsys)
    assert rc == 0 and len(parsed) == 1   # malformed input does not crash the write
