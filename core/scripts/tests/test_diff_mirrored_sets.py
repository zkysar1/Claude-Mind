"""Dogfood + regression suite for core/scripts/diff-mirrored-sets.py (gap-036 forge).

Required by /forge-skill Step 3.6: the companion script is a COMPUTATION whose
output other code trusts, so it must be proven to DISCRIMINATE before the skill is
registered -- a script that returns the same verdict on a PASS and a FAIL fixture
is vacuous regardless of how many fixtures pass.

FIXTURE-SEAM SCOPE DECLARATION (guard-1462 -- name what the seam excludes):
these fixtures inject at the PAYLOAD boundary, i.e. at already-extracted member
lists. Therefore EXTRACTION ITSELF -- how a caller greps a file, walks a
filesystem, or queries an API to produce `members` -- is structurally
unfalsifiable by any test in this file. That exclusion is by DESIGN, not an
oversight: the script deliberately does not own extraction (extraction is
domain-specific and unmockable in general), which is precisely why it forces the
caller to declare a positive CONTROL over that excluded layer. The controls are
the script's only handle on the layer these fixtures cannot reach.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "diff-mirrored-sets.py"


def _load():
    spec = importlib.util.spec_from_file_location("diff_mirrored_sets", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dms = _load()


def _min1(members, label):
    return {"label": label, "members": members, "control": {"kind": "min_count", "value": 1}}


# ---------------------------------------------------------------- core verdicts

def test_pass_fixture_identical_sets_verdict_ok():
    payload = {"a": _min1(["x", "y"], "gate"), "b": _min1(["y", "x"], "doc")}
    result, code = dms.diff(payload)
    assert result["verdict"] == "ok"
    assert code == 0
    assert result["counts"]["both"] == 2


def test_fail_fixture_divergent_reports_both_directions_separately():
    """The whole point of the skill: the UNEXAMINED direction is reported too."""
    payload = {
        "a": _min1(["shared", "only_in_a"], "gate"),
        "b": _min1(["shared", "only_in_b1", "only_in_b2"], "doc"),
    }
    result, code = dms.diff(payload)
    assert code == 1
    assert result["verdict"] == "divergent"
    assert result["a_only"] == ["only_in_a"]
    assert result["b_only"] == ["only_in_b1", "only_in_b2"]


def test_control_failure_is_exit_2_not_exit_1():
    """An untrustworthy extraction must NOT be reportable as a clean diff.

    This is the load-bearing distinction: 'the sets differ' and 'I cannot tell
    whether the sets differ' are different states.
    """
    payload = {"a": _min1([], "gate"), "b": _min1(["y"], "doc")}
    result, code = dms.diff(payload)
    assert code == 2
    assert result["verdict"] == "control_failed"
    assert result["controls"]["a"]["passed"] is False
    assert "min_count FAILED" in result["controls"]["a"]["detail"]


def test_empty_extraction_without_declaration_cannot_pass():
    """Zero members is only acceptable when declared IN ADVANCE (rb-245)."""
    payload = {"a": _min1([], "gate"), "b": _min1([], "doc")}
    _, code = dms.diff(payload)
    assert code == 2


def test_expect_empty_is_the_only_way_zero_passes():
    payload = {
        "a": {"label": "gate", "members": [], "control": {"kind": "expect_empty"}},
        "b": {"label": "doc", "members": [], "control": {"kind": "expect_empty"}},
    }
    result, code = dms.diff(payload)
    assert code == 0
    assert result["verdict"] == "ok"


def test_expect_empty_fails_when_source_disagrees():
    payload = {
        "a": {"label": "gate", "members": ["surprise"], "control": {"kind": "expect_empty"}},
        "b": _min1(["surprise"], "doc"),
    }
    _, code = dms.diff(payload)
    assert code == 2


def test_missing_control_fails_closed():
    """A side with NO control must fail -- an optional control is not run when it matters."""
    payload = {"a": {"label": "gate", "members": ["x"]}, "b": _min1(["x"], "doc")}
    result, code = dms.diff(payload)
    assert code == 2
    assert "no control declared" in result["controls"]["a"]["detail"]


# ------------------------------------------------------------------- sentinel

def test_sentinel_control_detects_truncated_probe():
    """The terminating-token check that caught the phantom block comment ()."""
    ok = {
        "label": "gate", "members": ["f1"],
        "control": {"kind": "sentinel", "token": "END_OF_BLOCK"},
        "raw_text": "f1 ... END_OF_BLOCK",
    }
    truncated = dict(ok, raw_text="f1 ... (probe cut off here)")
    _, code_ok = dms.diff({"a": ok, "b": _min1(["f1"], "doc")})
    _, code_trunc = dms.diff({"a": truncated, "b": _min1(["f1"], "doc")})
    assert code_ok == 0
    assert code_trunc == 2


# -------------------------------------------------------------- label / value

def test_label_value_mismatch_flagged_even_when_sets_agree():
    """A row present on BOTH sides can still be wrong -- a distinct axis."""
    payload = {
        "a": {"label": "gate",
              "members": [{"label": "timeout=30", "value": 60}],
              "control": {"kind": "min_count", "value": 1}},
        "b": _min1(["timeout=30"], "doc"),
    }
    result, code = dms.diff(payload)
    assert code == 1
    assert result["a_only"] == [] and result["b_only"] == []   # sets agree
    m = result["label_value_mismatches"]
    assert len(m) == 1
    assert m[0]["claimed_in_label"] == "30" and m[0]["rendered_value"] == "60"


def test_label_value_agreement_produces_no_finding():
    payload = {
        "a": {"label": "gate",
              "members": [{"label": "timeout=30", "value": 30}],
              "control": {"kind": "min_count", "value": 1}},
        "b": _min1(["timeout=30"], "doc"),
    }
    result, code = dms.diff(payload)
    assert code == 0
    assert result["label_value_mismatches"] == []


def test_plain_string_members_exempt_from_label_value_check():
    """A plain string has no rendered value, so there is nothing to disagree with."""
    payload = {"a": _min1(["timeout=30"], "gate"), "b": _min1(["timeout=30"], "doc")}
    result, code = dms.diff(payload)
    assert code == 0
    assert result["label_value_mismatches"] == []


# ------------------------------------------------------------------ partition

def test_partition_sum_equals_union_on_every_verdict():
    payload = {"a": _min1(["p", "q"], "gate"), "b": _min1(["q", "r"], "doc")}
    result, _ = dms.diff(payload)
    c = result["counts"]
    assert c["a_only"] + c["b_only"] + c["both"] == c["union"]
    assert result["partition"]["ok"] is True


def test_duplicate_members_are_dropped_loudly_not_silently():
    payload = {"a": _min1(["dup", "dup", "x"], "gate"), "b": _min1(["dup", "x"], "doc")}
    result, code = dms.diff(payload)
    assert code == 0
    assert result["duplicates_dropped"]["a"] == ["dup"]
    assert result["counts"]["a_total"] == 2


# ------------------------------------------------------- malformed input fails closed

def test_malformed_payload_is_control_failed_not_ok():
    try:
        dms.diff({"a": _min1(["x"], "gate")})          # missing 'b'
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_member_object_without_label_rejected():
    payload = {"a": {"label": "gate", "members": [{"value": 1}],
                     "control": {"kind": "min_count", "value": 1}},
               "b": _min1(["x"], "doc")}
    try:
        dms.diff(payload)
        raised = False
    except ValueError:
        raised = True
    assert raised


# ------------------------------------------------------------------ CLI contract

def test_cli_exit_codes_and_json_shape():
    payload = {"a": _min1(["only_a", "shared"], "gate"), "b": _min1(["shared"], "doc")}
    p = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(payload),
                       capture_output=True, text=True)
    assert p.returncode == 1
    out = json.loads(p.stdout)
    assert out["verdict"] == "divergent"
    assert out["a_only"] == ["only_a"]


def test_cli_unreadable_payload_exits_2():
    p = subprocess.run([sys.executable, str(SCRIPT)], input="{not json",
                       capture_output=True, text=True)
    assert p.returncode == 2
    assert json.loads(p.stdout)["verdict"] == "control_failed"


# ------------------------------------------------- ANTI-VACUITY (guard-1220 / guard-1793)

def test_vacuity_proof_one_field_apart_yields_different_verdicts():
    """Decisive pair: two payloads differing in EXACTLY ONE member, opposite verdicts.

    guard-1793: this is a PER-FIXTURE decisive assertion, not a summary aggregate.
    A count-of-distinct-verdicts line would stay green through a defect that
    corrupted WHICH verdict each fixture got, so it is deliberately not used as
    the anti-vacuity guard here.
    """
    base_b = _min1(["shared"], "doc")
    agree = {"a": _min1(["shared"], "gate"), "b": base_b}
    differ = {"a": _min1(["shared", "extra"], "gate"), "b": base_b}   # ONE member added

    r_agree, code_agree = dms.diff(agree)
    r_differ, code_differ = dms.diff(differ)

    assert (r_agree["verdict"], code_agree) == ("ok", 0)
    assert (r_differ["verdict"], code_differ) == ("divergent", 1)
    assert code_agree != code_differ


def test_vacuity_proof_control_axis_one_field_apart():
    """Same discrimination proof for the control axis, independent of the diff axis."""
    trusted = {"a": _min1(["x"], "gate"), "b": _min1(["x"], "doc")}
    untrusted = {"a": {"label": "gate", "members": [],
                       "control": {"kind": "min_count", "value": 1}},
                 "b": _min1(["x"], "doc")}
    assert dms.diff(trusted)[1] == 0
    assert dms.diff(untrusted)[1] == 2
