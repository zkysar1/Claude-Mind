"""Regression pins for the  enabled_by shape-crash class.

Defect (g-115-11563, measured 2026-09-28 by omni on cc-06, filed at user's
directive): cmd_temporal_credit called .get() on EVERY enabled_by entry. The
write path shape-validates none of it: the goal record states both writers
pass the field through as given [UNVERIFIED -- writer pass-through is the
record's measurement (g-115-11563 description), not this session's], and the
fleet corpus carries non-dict entries — ZDS measured 41 records of bare id
strings such as ["rb-1556", "guard-1268"] (which writer produced them is NOT
measured). A non-dict entry raised AttributeError, and that raise aborted
cmd_run_all BEFORE relative-advantage ran — the g-115-5086 class (one bad
value cost three audits) one step later.

Fixes pinned here:
  - cmd_temporal_credit skips a non-dict entry, a dict without experience_id,
    and a dict whose temporal_distance is not numeric (bool excluded, the
    _numeric_range precedent), instead of raising; it flags the count as
    `malformed_enabled_by_entries:<n>` — informational, NOT in
    HARD_FAIL_FLAGS, so run-all still reaches relative-advantage and the
    audit exit code stays 0.
  - THE REGRESSION PIN: a mixed enabled_by list (dicts and bare strings)
    through cmd_run_all completes, relative-advantage runs, and credit
    propagates to the dict entries only.
  - A clean (dict-only) list carries NO new flag — the guard is surgical.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
SCRIPT = SCRIPTS_DIR / "state-update-audit.py"


def _import():
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location("state_update_audit_tc", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["state_update_audit_tc"] = mod
    spec.loader.exec_module(mod)
    return mod


MOD = _import()

# One well-formed enabler (credit at lv * 0.9^1) and the malformed shapes the
# fleet corpus actually carries: bare id strings (the ZDS shape), a dict with
# no id to resolve, a non-dict scalar, and a dict whose temporal_distance is a
# string (gamma ** <str> is the TypeError twin of the original AttributeError).
_GOOD = {"experience_id": "exp-ENABLER", "relationship": "provided_foundation",
         "temporal_distance": 1}
_MALFORMED = [
    "rb-1556",                                   # bare store id (ZDS shape)
    42,                                          # non-dict scalar
    {"relationship": "provided_foundation"},     # dict without experience_id
    {"experience_id": "exp-FAR", "temporal_distance": "far"},  # non-numeric distance
]
MIXED = [_GOOD] + _MALFORMED

# Quality inputs that make cmd_velocity MEASURED: lv = .3 + .06 + .2 + 0 = 0.56,
# so the dist-1 credit 0.56 * 0.9 = 0.504 survives the 0.01 floor.
_QUALITY = dict(tree_updated=True, artifacts_count=1, encoding_score=1.0,
                findings_count=0)


def _args(**kw):
    base = dict(goal="g-test", outcome_class="deep", category="cat",
                experience_id="exp-exec-g-1", tree_updated=False,
                artifacts_count=None, encoding_score=None, findings_count=None,
                exploration=False, learning_value=0.56)
    base.update(kw)
    return argparse.Namespace(**base)


def _fake_run(mapping, calls):
    """Dispatch _run by argv prefix (most-specific first); unmapped -> rc=1."""
    def _r(argv, **kw):
        calls.append(argv)
        for prefix, resp in mapping.items():
            if list(argv[:len(prefix)]) == list(prefix):
                return resp
        return ("", "not mapped", 1)
    return _r


def _temporal_mapping(record):
    """Subprocess answers for the temporal-credit stage (exact-id path)."""
    return {
        ("experience-read.sh", "--id", "exp-exec-g-1"): (json.dumps(record), "", 0),
        ("experience-read.sh", "--id", "exp-ENABLER"): (
            json.dumps({"id": "exp-ENABLER", "temporal_credit": 0.0}), "", 0),
        ("experience-update-field.sh",): ("", "", 0),
    }


# ── standalone: mixed list skips with a flag instead of raising ────────────

def test_temporal_credit_mixed_entries_skip_malformed_with_flag(monkeypatch):
    calls = []
    monkeypatch.setattr(MOD, "_run", _fake_run(
        _temporal_mapping({"id": "exp-exec-g-1", "enabled_by": MIXED}), calls))
    r = MOD.cmd_temporal_credit(_args(learning_value=0.56))
    # Credit lands on the dict entry only; the four malformed shapes are
    # skipped, not raised and not coerced.
    assert [p["experience_id"] for p in r["propagated"]] == ["exp-ENABLER"]
    assert r["propagated"][0]["credit_added"] == 0.504
    assert r["flags"] == ["malformed_enabled_by_entries:4"]
    # Exactly one enabler update — no None-id write, no string-distance math.
    updates = [a for a in calls if a[0] == "experience-update-field.sh"]
    assert len(updates) == 1
    assert updates[0][1] == "exp-ENABLER"


def test_temporal_credit_clean_dict_list_carries_no_flag(monkeypatch):
    """Surgical: the new flag must not appear on a well-formed list."""
    calls = []
    monkeypatch.setattr(MOD, "_run", _fake_run(
        _temporal_mapping({"id": "exp-exec-g-1", "enabled_by": [_GOOD]}), calls))
    r = MOD.cmd_temporal_credit(_args(learning_value=0.56))
    assert [p["experience_id"] for p in r["propagated"]] == ["exp-ENABLER"]
    assert r["flags"] == []


def test_malformed_flag_is_informational_not_a_hard_failure():
    """The flag must not drive a non-zero audit exit (HARD_FAIL_FLAGS denylist)
    — the cascade COMPLETED; the entries were skipped, exactly the g-115-5086
    remediation shape (flag the count instead of raising)."""
    assert not MOD._has_hard_failure(
        ["temporal-credit:malformed_enabled_by_entries:4"])
    assert not MOD._has_hard_failure(
        ["malformed_enabled_by_entries:4"])
    assert MOD._has_hard_failure(["check_failed"])  # control: denylist intact


# ── THE REGRESSION PIN: mixed list through run-all ─────────────────────────

def test_run_all_mixed_enabled_by_completes_and_credits_dicts_only(
        tmp_path, monkeypatch):
    """The record's required outcome verbatim: a mixed enabled_by list
    (dicts and bare strings) through run-all; assert run-all completes,
    relative-advantage runs, and credit propagates to the dict entries only.

    Pre-fix, this raised AttributeError out of cmd_temporal_credit inside
    cmd_run_all — before relative-advantage — and main() rendered it as
    check_failed over an EMPTY audit.
    """
    # relative-advantage reads improvement-velocity.yaml straight from META_DIR
    # (not via _run), so give it a real file with >=3 same-category entries.
    vel = tmp_path / "improvement-velocity.yaml"
    vel.write_text(
        "entries:\n"
        "  - category: cat\n    learning_value: 0.5\n"
        "  - category: cat\n    learning_value: 0.6\n"
        "  - category: cat\n    learning_value: 0.4\n",
        encoding="utf-8")
    monkeypatch.setattr(MOD, "META_DIR", str(tmp_path))

    calls = []
    monkeypatch.setattr(MOD, "_run", _fake_run({
        # velocity stage
        ("meta-backpressure.sh", "status"): ("{}", "", 0),
        ("aspirations-query.sh",): ("[]", "", 0),   # no close key (fail-open)
        ("meta-impk.sh", "snapshot"): ("{}", "", 0),
        # backpressure stage
        ("meta-backpressure.sh", "check"): (
            json.dumps({"rollback_actions": [], "dead_end_candidates": [],
                        "graduated": []}), "", 0),
        # temporal-credit stage (exact-record path, mixed enabled_by)
        **_temporal_mapping({"id": "exp-exec-g-1", "enabled_by": MIXED}),
        # relative-advantage write-back
        ("experience-update-field.sh",): ("", "", 0),
    }, calls))

    r = MOD.cmd_run_all(_args(**_QUALITY))

    # run-all COMPLETED — all four stages present in the result.
    assert r["subcommand"] == "run-all"
    assert set(r["results"].keys()) == {
        "velocity", "backpressure", "temporal_credit", "relative_advantage"}
    assert not any(f.endswith("check_failed") for f in r["flags"])

    # relative-advantage RAN (sequenced after temporal-credit, where the old
    # raise aborted the chain): mean 0.5, lv 0.56 -> adv +0.06.
    ra = r["results"]["relative_advantage"]
    assert ra["subcommand"] == "relative-advantage"
    assert ra["relative_advantage"] == 0.06
    assert any(a[:3] == ["experience-update-field.sh", "exp-exec-g-1",
                         "relative_advantage"] for a in calls)

    # credit propagated to the DICT entry only, and the skip is flagged.
    tc = r["results"]["temporal_credit"]
    assert [p["experience_id"] for p in tc["propagated"]] == ["exp-ENABLER"]
    assert tc["flags"] == ["malformed_enabled_by_entries:4"]
    assert "temporal-credit:malformed_enabled_by_entries:4" in r["flags"]
    # No hard failure at the run-all level: the audit would exit 0.
    assert not MOD._has_hard_failure(r["flags"])
