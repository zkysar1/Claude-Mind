#!/usr/bin/env python3
"""Tests for commons-usage.py () — the trust-ledger producer half.

Sibling of test_commons_retrieve.py. Covers the things that would silently break
the feature, which on THIS lane means anything that lets an empty or wrong ledger
look healthy:

  1. validate — the local mirror of the gateway contract. goal_id and agent ARE
     the DynamoDB item key, so a malformed one is not a cosmetic problem: it
     writes (or refuses) the wrong row. Empty pattern_signatures must be refused
     outright — a usage record asserting nothing was consumed is the vacuous
     evidence this lane exists to eliminate.
  2. signatures_from_manifest — must take ONLY patterns marked `drawn`, and must
     refuse a manifest belonging to a DIFFERENT goal. A stale manifest silently
     attributes another goal's draws to this execution.
  3. _str_list / verify_row's attribute names — the readback guard. Measured on
     the first live write, 2026-07-29: reading camelCase `patternSignatures`
     against the stored snake_case `pattern_signatures` returned a confident []
     for a row plainly carrying two signatures. That is the same "confident
     zero" class as this table's documented scan traps, reintroduced INSIDE the
     guard meant to catch them (guard-1720: probing the name you SENT can report
     a false result on a write that landed; guard-1755: a verification surface
     that can report ABSENCE for data that is PRESENT is not a verification
     surface). These tests pin the attribute names so the fix cannot silently
     regress.
"""
import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _paths  # noqa: E402

# The script under test lives in world/scripts, not core/scripts: it speaks ONE
# product's gateway, so it is domain-specific by construction. Core owns only the
# `post-execution` hook slot; the world owns the implementation. See
# core/config/conventions/domain-hooks.md.
_TARGET = Path(_paths.WORLD_DIR) / "scripts" / "commons-usage.py"
if not _TARGET.exists():                                  # pragma: no cover
    import pytest
    pytest.skip(f"domain hook not installed in this world: {_TARGET}",
                allow_module_level=True)

_spec = importlib.util.spec_from_file_location("commons_usage", _TARGET)
cu = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cu)


# ---------------------------------------------------------------- validate ---

def test_valid_call_passes():
    assert cu.validate("g-335-403", "alpha", ["ayoai:rb:rb-683"], []) is None


def test_empty_signatures_refused():
    """A usage record asserting nothing was consumed is vacuous evidence."""
    err = cu.validate("g-335-403", "alpha", [], [])
    assert err is not None and "empty_pattern_signatures" in err


def test_item_key_fields_are_policed():
    """goal_id and agent ARE the item key — a bad one writes the wrong row."""
    assert "invalid_goal_id" in cu.validate("has space", "alpha", ["s"], [])
    assert "invalid_agent" in cu.validate("g-1", "not valid!", ["s"], [])
    assert "invalid_goal_id" in cu.validate("", "alpha", ["s"], [])
    assert "invalid_agent" in cu.validate("g-1", "", ["s"], [])
    # 64 is the documented ceiling: at it, fine; past it, refused.
    assert cu.validate("g" * 64, "a" * 64, ["s"], []) is None
    assert "invalid_goal_id" in cu.validate("g" * 65, "alpha", ["s"], [])


def test_oversized_arrays_are_refused_not_truncated():
    """The contract REFUSES past the cap. Truncating would silently under-report."""
    err = cu.validate("g-1", "alpha", ["s"] * (cu.MAX_ARRAY_ENTRIES + 1), [])
    assert err is not None and "too_long" in err and "refused" in err
    err = cu.validate("g-1", "alpha", ["s"], ["e"] * (cu.MAX_ARRAY_ENTRIES + 1))
    assert err is not None and "attribution_event_ids_too_long" in err
    err = cu.validate("g-1", "alpha", ["x" * (cu.MAX_ENTRY_CHARS + 1)], [])
    assert err is not None and "entry_invalid" in err


def test_empty_event_ids_are_legal():
    """Only contributor credit needs them; the unit still accrues trials."""
    assert cu.validate("g-1", "alpha", ["ayoai:rb:rb-1"], []) is None


# ------------------------------------------------- signatures_from_manifest ---

def _manifest(tmp_path, blob):
    d = tmp_path / "session"
    d.mkdir(parents=True, exist_ok=True)
    (d / "retrieval-session.json").write_text(json.dumps(blob), encoding="utf-8")
    return tmp_path


def test_manifest_takes_only_drawn_patterns(tmp_path):
    """A listed-but-undrawn pattern was never READ, so it cannot have informed."""
    ad = _manifest(tmp_path, {"commons_patterns": {"goal_id": "g-1", "patterns": [
        {"signature": "sig-drawn", "drawn": True,
         "attribution": {"eventId": "evt-1"}},
        {"signature": "sig-listed-only", "drawn": False,
         "attribution": {"eventId": "evt-2"}},
    ]}})
    sigs, evts, note = cu.signatures_from_manifest(ad, "g-1")
    assert note == "ok"
    assert sigs == ["sig-drawn"]
    assert evts == ["evt-1"], "an undrawn pattern must not contribute credit"


def test_manifest_for_a_different_goal_is_refused(tmp_path):
    """A stale manifest would attribute ANOTHER goal's draws to this execution."""
    ad = _manifest(tmp_path, {"commons_patterns": {"goal_id": "g-OTHER",
                                                   "patterns": [
        {"signature": "sig-a", "drawn": True, "attribution": {"eventId": "e"}}]}})
    sigs, evts, note = cu.signatures_from_manifest(ad, "g-MINE")
    assert sigs == [] and evts == []
    assert "g-OTHER" in note, "the refusal must name the goal it actually found"


def test_null_attribution_yields_no_event_id(tmp_path):
    """attribution is None when recordRetrieval threw — never the string 'null'."""
    ad = _manifest(tmp_path, {"commons_patterns": {"goal_id": "g-1", "patterns": [
        {"signature": "sig-a", "drawn": True, "attribution": None}]}})
    sigs, evts, note = cu.signatures_from_manifest(ad, "g-1")
    assert note == "ok" and sigs == ["sig-a"]
    assert evts == [], "a missing attribution must contribute nothing, not 'null'"


def test_missing_and_malformed_manifests_report_distinctly(tmp_path):
    """Fail-open, but each cause must be nameable — rb-683: never silent.

    AMENDED g-335-1205. The reader now has TWO sources: the manifest, then the
    durable `commons-draw-log.jsonl` (the manifest is a single-goal snapshot the
    daemon rewrites on every retrieval, so it is reliably gone by the time Step
    1.6 reads it). A genuine miss therefore names BOTH attempts —
    `<manifest_cause>+draw_ledger:<ledger_cause>` — so `startswith` here, not
    equality.

    That is a STRENGTHENING of this test's own contract, not a loosening: the
    manifest cause is still named, and the fallback's outcome is now named too.
    The extra assertion below pins that second half, so a future change that
    silently drops the fallback attempt from the note still fails here.

    NEVER RE-PIN THE FULL NOTE (guard-3300). The tail is derived from the
    RUNTIME ENVIRONMENT — whether `world/commons-draw-log.jsonl` resolves on
    this box — so equality there goes red on exactly the machines where the
    ledger is real. Measured 2026-08-13 (alpha, cc-04): the same call returned
    `no_manifest+draw_ledger:ok` run solo and `no_manifest+draw_ledger:no_draw_ledger`
    in-suite. Assert the shape, never the value. (Independently converged on by
    two agents from opposite directions — g-335-1205 forward from the code, and
    g-115-6134 backward from the red — which is why both halves are kept.)
    """
    (tmp_path / "session").mkdir(parents=True, exist_ok=True)
    note = cu.signatures_from_manifest(tmp_path, "g-1")[2]
    assert note.startswith("no_manifest"), note
    assert "draw_ledger:" in note, (
        f"the fallback attempt must be named too, never silently dropped: {note}")

    ad = _manifest(tmp_path, {"something_else": {}})
    assert cu.signatures_from_manifest(ad, "g-1")[2].startswith(
        "no_commons_patterns_key")

    (tmp_path / "session" / "retrieval-session.json").write_text(
        "{not json", encoding="utf-8")
    assert cu.signatures_from_manifest(tmp_path, "g-1")[2].startswith(
        "manifest_unreadable:")


# ------------------------------------------------------- the readback guard ---

def test_str_list_reads_the_stored_snake_case_attribute():
    """The two names must genuinely disagree on the SAME item.

    This pins the helper only. The pin on verify_row's own attribute CHOICE —
    the thing that actually broke — is test_verify_row_reads_the_real_attribute
    below, which drives verify_row itself. Keeping them separate matters: a
    helper-level assertion cannot fail when the CALLER passes the wrong name,
    so on its own it would have gone green throughout the live defect.
    """
    item = {"pattern_signatures": {"L": [{"S": "ayoai:rb:rb-683"},
                                         {"S": "ayoai:rb:rb-539"}]}}
    assert cu._str_list(item, "pattern_signatures") == ["ayoai:rb:rb-683",
                                                        "ayoai:rb:rb-539"]
    assert cu._str_list(item, "patternSignatures") == [], (
        "the camelCase name must read EMPTY — that is exactly why the original "
        "guard reported a confident zero against a populated row")


_STORED_ROW = {"Item": {
    "PK": {"S": "USAGE#alpha"}, "SK": {"S": "g-335-403"},
    "agent": {"S": "alpha"}, "goal_id": {"S": "g-335-403"},
    "record_type": {"S": "usage"}, "world": {"S": "world-uuid"},
    "pattern_signatures": {"L": [{"S": "ayoai:rb:rb-683"},
                                 {"S": "ayoai:rb:rb-539"}]},
    "attribution_event_ids": {"L": [{"S": "evt-1"}, {"S": "evt-2"}]},
}}


class _Proc:
    def __init__(self, out, rc=0, err=""):
        self.stdout, self.returncode, self.stderr = out, rc, err


def _canned(monkeypatch, payload, rc=0):
    monkeypatch.setattr(cu.subprocess, "run",
                        lambda *a, **k: _Proc(json.dumps(payload), rc))


def test_verify_row_reads_the_real_attribute(monkeypatch):
    """THE regression pin. Reverting verify_row to `patternSignatures` turns
    this red — that is the mutation this test exists to catch."""
    _canned(monkeypatch, _STORED_ROW)
    r = cu.verify_row("alpha", "g-335-403")
    assert r["verified"] is True
    assert r["pattern_signatures"] == ["ayoai:rb:rb-683", "ayoai:rb:rb-539"], (
        "verify_row must read the STORED snake_case attribute; a camelCase read "
        "reports a confident empty list for a row carrying two signatures")
    assert r["attribution_event_ids"] == ["evt-1", "evt-2"]
    assert r["world"] == "world-uuid"


def test_verify_row_fails_when_stored_row_lacks_a_posted_signature(monkeypatch):
    """Presence alone is not verification — content must be compared."""
    _canned(monkeypatch, _STORED_ROW)
    r = cu.verify_row("alpha", "g-335-403",
                      expect_sigs=["ayoai:rb:rb-683", "ayoai:rb:rb-NEVER"])
    assert r["verified"] is False
    assert "rb-NEVER" in r["reason"]


def test_verify_row_passes_when_every_posted_signature_is_stored(monkeypatch):
    """SPECIFICITY control — the comparison must not reject a correct row.

    Stays GREEN under the camelCase mutation only if the read is right; pairing
    it with the mismatch case above proves the check discriminates rather than
    always failing (guard-1660).
    """
    _canned(monkeypatch, _STORED_ROW)
    r = cu.verify_row("alpha", "g-335-403",
                      expect_sigs=["ayoai:rb:rb-683", "ayoai:rb:rb-539"])
    assert r["verified"] is True and "reason" not in r


def test_verify_row_distinguishes_absent_row_from_failed_access(monkeypatch):
    """A real negative and an access failure demand opposite responses."""
    _canned(monkeypatch, {})                      # call succeeded, no Item
    assert cu.verify_row("alpha", "g-none")["reason"] == "row_absent_after_write"

    _canned(monkeypatch, {}, rc=255)              # call itself failed
    assert cu.verify_row("alpha", "g-1")["reason"].startswith("aws_rc_")

    monkeypatch.setattr(cu.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    assert cu.verify_row("alpha", "g-1")["reason"] == "aws_cli_absent"


def test_verify_row_never_raises_on_unparseable_output(monkeypatch):
    monkeypatch.setattr(cu.subprocess, "run", lambda *a, **k: _Proc("not json"))
    r = cu.verify_row("alpha", "g-1")
    assert r["verified"] is False and r["reason"] == "aws_unparseable_output"


def test_str_list_is_total_on_absent_or_malformed_attributes():
    assert cu._str_list({}, "pattern_signatures") == []
    assert cu._str_list({"pattern_signatures": {}}, "pattern_signatures") == []
    assert cu._str_list({"pattern_signatures": {"L": []}}, "pattern_signatures") == []
    # A non-string member is skipped rather than crashing the readback.
    assert cu._str_list({"pattern_signatures": {"L": [{"N": "1"}]}},
                        "pattern_signatures") == []


def test_ledger_target_constants_are_pinned():
    """A wrong table/region reads an empty ledger and reports it as healthy —
    the same wrong-store failure class where 'found nothing' and 'nothing to
    find' are byte-identical (guard-1857)."""
    assert cu.LEDGER_TABLE == "lodestar-commons"
    assert cu.LEDGER_REGION == "us-east-2"


def test_ledger_table_honours_the_owning_systems_env_override(monkeypatch):
    """The owning code is `process.env.LODESTAR_COMMONS_TABLE ?? 'lodestar-commons'`
    (Lodestar-Web-App lib/commons/dynamo-store.ts:98). If the gateway is pointed at
    another table and this verifier is not, the gateway writes correctly while the
    readback queries the old table and reports the row absent — a confident zero.
    Pins that both sides resolve the SAME way, via the same var name.
    """
    monkeypatch.setenv("LODESTAR_COMMONS_TABLE", "lodestar-commons-staging")
    _spec2 = importlib.util.spec_from_file_location("commons_usage_reload", _TARGET)
    reloaded = importlib.util.module_from_spec(_spec2)
    _spec2.loader.exec_module(reloaded)
    assert reloaded.LEDGER_TABLE == "lodestar-commons-staging"

    monkeypatch.delenv("LODESTAR_COMMONS_TABLE", raising=False)
    _spec3 = importlib.util.spec_from_file_location("commons_usage_reload2", _TARGET)
    default = importlib.util.module_from_spec(_spec3)
    _spec3.loader.exec_module(default)
    assert default.LEDGER_TABLE == "lodestar-commons", "fallback must match the owning default"
