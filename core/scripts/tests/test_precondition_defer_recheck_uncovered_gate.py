""": the sweep must not clear a defer whose named gate it never tested.

THE DEFECT. precondition-defer-recheck.py SELECTS goals on the defer TEXT
(`defer_reason` starting `precondition_unmet:`) and then TESTS
`verification.preconditions`. Those are two different things. When a goal's
structured preconditions were satisfied long ago and a LATER defer cites a NEW
gate that was never added to that list, the sweep re-passes the already-satisfied
predicate and clears a defer whose actual gate it never looked at. The defer is
destroyed; the gate is untouched.

MEASURED INSTANCE, which this file reproduces as a fixture. g-373-27 (asp-373, a
BOOSTED closing lane, HIGH): its only structured precondition was
`g115-9872-changes-producer`, satisfied 2026-09-15 and discharged 2026-09-16. On
2026-09-17 it was deferred `precondition_unmet` naming g-370-09 (the weekly
dev-to-main promotion ritual, still pending). By 2026-09-18 `defer_reason` and
`defer_reason_set_at` were both null while g-370-09 was still pending, and the
goal was fully selectable again. Its remaining outcome needs a BILLED live
vessel against a channel that is still wrong on main, so the wrongly-cleared
defer converts a protected gate into a paid run with a guaranteed-misleading
result — which is why the fix is an Unblock and not an Investigate.

RED-THEN-GREEN. `test_mutation_covering_the_gate_restores_the_clear` drives the
IDENTICAL fixture twice, changing only whether a structured precondition
mentions the gate the defer names. Uncovered -> skipped; covered -> cleared.
Without the guard both runs clear, so that pair is the discriminator: a fix that
merely stopped clearing everything would fail the second half.

WHAT IS DELIBERATELY NOT TESTED HERE: prose parsing. The guard matches goal-id
TOKENS only and accepts over-matching (an extra SKIP is cheap, an extra CLEAR is
destructive). `test_incidental_goal_id_mention_is_treated_as_uncovered` pins that
over-matching as INTENDED behaviour, not a bug for a later reader to "fix" by
reaching for defer-recheck._extract_dep_ids — whose dependency-tuned exclusions
are exactly what would let an id through to the clear path (guard-2486: do not
share a predicate across a reversible read and a destructive invalidation).

Hermetic: `_read_goals` is monkeypatched (no daemon, no live store),
`_clear_defer` is monkeypatched in the --apply test, `--metrics-log ""` disables
the metrics JSONL. No world/meta writes.

Run: STORAGE_BACKEND=local py -3 -m pytest \
    core/scripts/tests/test_precondition_defer_recheck_uncovered_gate.py -v
"""
import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def load_module():
    """Import precondition-defer-recheck.py (hyphen in name blocks plain import)."""
    spec = importlib.util.spec_from_file_location(
        "precondition_defer_recheck_uncovered_gate_module",
        SCRIPT_DIR / "precondition-defer-recheck.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = load_module()

# A path that provably exists, so the structured predicate deterministically
# PASSES — this fixture's whole point is "predicates green, gate untested".
EXISTING_PATH = str(SCRIPT_DIR / "precondition-defer-recheck.py")


def _passing_pc(pc_id="pc-g115-9872-changes-producer", extra=None):
    pc = {
        "type": "file_check",
        "id": pc_id,
        "path": EXISTING_PATH,
        "condition": "exists",
    }
    if extra:
        pc.update(extra)
    return pc


def _goal(goal_id="g-373-27-fixture", defer_text=None, pcs=None, hours_old=5.0):
    """The measured  shape: aged precondition_unmet defer, pending, no
    deferred_until, structured preconditions that ALL PASS."""
    set_at = (dt.datetime.now() - dt.timedelta(hours=hours_old)).strftime(
        "%Y-%m-%dT%H:%M:%S")
    return {
        "id": goal_id,
        "status": "pending",
        "defer_reason": defer_text if defer_text is not None else (
            "precondition_unmet: gated on g-370-09, the weekly dev-to-main "
            "promotion ritual, which is still pending"),
        "defer_reason_set_at": set_at,
        "verification": {
            "preconditions": pcs if pcs is not None else [_passing_pc()],
        },
    }


def _run_main(monkeypatch, capsys, goals_world, apply=False, clears=None):
    """Drive main() in-process with _read_goals monkeypatched.
    Returns (exit_code, parsed_json). When apply=True, _clear_defer is also
    monkeypatched and every clear it is asked to perform is appended to
    `clears` — so the test asserts on the DESTRUCTIVE call itself, not only on
    the reported counter."""
    def fake_read_goals(source):
        if source != "world":
            return []
        out = []
        for g in goals_world:
            g = dict(g)
            g["_source"] = "world"
            g["_aspiration_id"] = "asp-test"
            out.append(g)
        return out

    monkeypatch.setattr(M, "_read_goals", fake_read_goals)
    if apply:
        def fake_clear(source, goal_id):
            if clears is not None:
                clears.append((source, goal_id))
            return True, None
        monkeypatch.setattr(M, "_clear_defer", fake_clear)

    argv = ["precondition-defer-recheck.py",
            "--max-age-hours", "2",
            "--metrics-log", ""]
    if apply:
        argv.append("--apply")
    monkeypatch.setattr(sys, "argv", argv)
    rc = M.main()
    return rc, json.loads(capsys.readouterr().out)


def _row(data, goal_id):
    rows = [d for d in data["details"] if d["goal_id"] == goal_id]
    assert len(rows) == 1, f"expected one detail row for {goal_id}: {data['details']}"
    return rows[0]


# --------------------------------------------------------------------------
# 1. The measured shape
# --------------------------------------------------------------------------

def test_measured_g373_27_shape_is_skipped_not_cleared(monkeypatch, capsys):
    """Predicates all pass, defer names , no precondition mentions it.
    Pre-fix this cleared. It must now skip."""
    rc, data = _run_main(monkeypatch, capsys, [_goal()])
    assert rc == 0
    assert data["eligible"] == 1
    assert data["evaluated"] == 1
    assert data["would_clear"] == [], "the uncovered gate must not reach the clear path"
    assert data["cleared"] == 0
    assert data["skipped_uncovered_gate"] == 1
    row = _row(data, "g-373-27-fixture")
    assert row["action"] == "skipped"
    assert row["all_passed"] is True, (
        "the row must record that the predicates DID pass — otherwise the skip "
        "reads as a failing predicate and the real defect is hidden")
    assert row["uncovered_gate_refs"] == ["g-370-09"]
    assert "g-370-09" in row["reason"]


def test_the_skip_is_reported_as_the_pre_fix_wrong_clear_count(monkeypatch, capsys):
    """Outcome 3's standing measurement: a row with all_passed AND
    uncovered_gate_refs IS a defer the old predicate would have cleared, so the
    sweep reports its own former error rate on every run rather than needing a
    one-off census."""
    rc, data = _run_main(monkeypatch, capsys, [_goal()])
    row = _row(data, "g-373-27-fixture")
    assert row["would_have_cleared_pre_fix"] is True
    assert data["skipped_uncovered_gate"] == 1


def test_apply_mode_issues_no_clear_for_an_uncovered_gate(monkeypatch, capsys):
    """The counter is not the contract — the DESTRUCTIVE call is. _clear_defer
    must never be invoked for this goal."""
    clears = []
    rc, data = _run_main(monkeypatch, capsys, [_goal()], apply=True, clears=clears)
    assert rc == 0
    assert clears == [], f"_clear_defer was called for an uncovered gate: {clears}"
    assert data["cleared"] == 0


# --------------------------------------------------------------------------
# 2. The mutation proof (red-then-green)
# --------------------------------------------------------------------------

def test_mutation_covering_the_gate_restores_the_clear(monkeypatch, capsys):
    """THE DISCRIMINATOR. Identical fixture, identical defer text; the only
    change is whether a structured precondition mentions the named gate.
    Uncovered -> skipped. Covered -> cleared. A fix that just stopped clearing
    would pass the first half and fail the second."""
    uncovered = _goal()
    rc_a, data_a = _run_main(monkeypatch, capsys, [uncovered])
    assert data_a["would_clear"] == []
    assert data_a["skipped_uncovered_gate"] == 1

    covered = _goal(pcs=[_passing_pc(
        pc_id="pc-g370-09-promotion",
        extra={"goal_id": "g-370-09"},
    )])
    rc_b, data_b = _run_main(monkeypatch, capsys, [covered])
    assert rc_b == 0
    assert data_b["would_clear"] == ["g-373-27-fixture"], (
        "with the gate represented in the structured preconditions the sweep "
        "must still do its job — the fix narrows the clear, it does not "
        "disable it")
    assert data_b["skipped_uncovered_gate"] == 0


def test_gate_named_in_a_nested_predicate_arg_counts_as_covered(monkeypatch, capsys):
    """Coverage is a substring test over the serialised predicates, so the id
    counts wherever it lives — this pins that the guard does not need to know
    predicate.py's field schema (a second place to keep in sync)."""
    covered = _goal(pcs=[_passing_pc(extra={"args": {"after": {"ref": "g-370-09"}}})])
    rc, data = _run_main(monkeypatch, capsys, [covered])
    assert data["would_clear"] == ["g-373-27-fixture"]
    assert data["skipped_uncovered_gate"] == 0


# --------------------------------------------------------------------------
# 3. Direction and scope of the matcher
# --------------------------------------------------------------------------

def test_self_reference_is_not_an_uncovered_gate(monkeypatch, capsys):
    """A defer naming the goal it sits on is describing itself, not a gate."""
    g = _goal(defer_text=("precondition_unmet: g-373-27-fixture is waiting on "
                          "its own verification window to elapse"))
    rc, data = _run_main(monkeypatch, capsys, [g])
    assert data["would_clear"] == ["g-373-27-fixture"]
    assert data["skipped_uncovered_gate"] == 0


def test_defer_naming_no_goal_id_still_clears(monkeypatch, capsys):
    """No goal-id token in the text = nothing uncovered. The guard must not
    become a blanket refusal (that was rejected remedy (b))."""
    g = _goal(defer_text="precondition_unmet: waiting for the settlement window")
    rc, data = _run_main(monkeypatch, capsys, [g])
    assert data["would_clear"] == ["g-373-27-fixture"]
    assert data["skipped_uncovered_gate"] == 0


def test_incidental_goal_id_mention_is_treated_as_uncovered(monkeypatch, capsys):
    """INTENDED over-matching, pinned so nobody 'fixes' it. A parenthetical
    reference is not a dependency, and defer-recheck._extract_dep_ids correctly
    drops it — but here the polarity is reversed and the write is destructive,
    so an unrecognised id must fail CLOSED. The cost of this choice is an extra
    SKIP; the cost of the other choice is an extra CLEAR."""
    g = _goal(defer_text=("precondition_unmet: waiting on the settlement window "
                          "(g-115-330 is the sibling pattern)"))
    rc, data = _run_main(monkeypatch, capsys, [g])
    assert data["would_clear"] == []
    assert _row(data, "g-373-27-fixture")["uncovered_gate_refs"] == ["g-115-330"]


def test_multiple_uncovered_refs_are_all_reported_and_deduped(monkeypatch, capsys):
    g = _goal(defer_text=("precondition_unmet: gated on g-370-09 and g-369-316; "
                          "see g-370-09 above"))
    rc, data = _run_main(monkeypatch, capsys, [g])
    assert _row(data, "g-373-27-fixture")["uncovered_gate_refs"] == [
        "g-370-09", "g-369-316"]


# --------------------------------------------------------------------------
# 4. No regression on the existing branches
# --------------------------------------------------------------------------

def test_free_form_skip_branch_is_unchanged(monkeypatch, capsys):
    """The vacuous-truth guard still fires first and is still counted
    separately — the new class must not absorb it."""
    g = _goal(pcs=[])
    rc, data = _run_main(monkeypatch, capsys, [g])
    assert data["skipped_free_form"] == 1
    assert data["skipped_uncovered_gate"] == 0
    assert data["evaluated"] == 0
    assert _row(data, "g-373-27-fixture")["reason"].startswith(
        "no structured preconditions")


def test_counter_is_present_even_when_zero(monkeypatch, capsys):
    """A key that only appears when non-zero makes an absent reading
    indistinguishable from a clean one for every downstream consumer."""
    g = _goal(defer_text="precondition_unmet: waiting for the settlement window")
    rc, data = _run_main(monkeypatch, capsys, [g])
    assert "skipped_uncovered_gate" in data
    assert data["skipped_uncovered_gate"] == 0


# --------------------------------------------------------------------------
# 5. The helper, directly
# --------------------------------------------------------------------------

def test_uncovered_gate_refs_helper_contract():
    pcs = [{"type": "goal_completed_after", "id": "pc-x", "goal_id": "g-370-09"}]
    assert M._uncovered_gate_refs(
        "precondition_unmet: gated on g-370-09", pcs, "g-1-1") == []
    assert M._uncovered_gate_refs(
        "precondition_unmet: gated on g-999-99", pcs, "g-1-1") == ["g-999-99"]
    # case-insensitive on both sides
    assert M._uncovered_gate_refs(
        "precondition_unmet: gated on G-370-09", pcs, "g-1-1") == []
    # own id excluded
    assert M._uncovered_gate_refs(
        "precondition_unmet: g-1-1 waits", pcs, "g-1-1") == []
    # empty / missing text is not an error
    assert M._uncovered_gate_refs("", pcs, "g-1-1") == []
    assert M._uncovered_gate_refs(None, pcs, "g-1-1") == []
    # unserialisable predicates: assume NOTHING is covered (fail closed)
    class Unserialisable:
        pass
    assert M._uncovered_gate_refs(
        "gated on g-370-09", [{"o": Unserialisable()}], "g-1-1") == ["g-370-09"]


# --------------------------------------------------------------------------
# 6. Coverage-side boundary ( — fresh-eyes finding on this own fix)
#
# The guard above tokenizes the DEFER side but originally tested coverage with
# a raw `id in json_blob` substring containment. A substring has no
# trailing-digit boundary, so a defer naming a SHORT id read as COVERED
# whenever the predicates happened to mention any LONGER id it prefixes — and
# that fails OPEN into the destructive clear. guard-4516 (goal-id matches need
# a trailing-digit boundary) + guard-2362 (the container TYPE decides the
# semantics; a str `in` is substring, a set `in` is membership, and the wrong
# one fails open). Same tokenizer-asymmetry class as rb-11386, on the other
# side of the same comparison.
#
# guard-1660: a sensitivity test alone is not enough. Each pair below asserts
# BOTH that the collision is now caught AND that genuine coverage still
# clears — otherwise "reject everything" would pass.
# --------------------------------------------------------------------------

def test_short_id_is_not_covered_by_a_longer_unrelated_id():
    """SENSITIVITY: defer names ; predicates mention only ."""
    pcs = [{"type": "goal_status", "id": "pc-a", "goal_id": "g-115-1364"}]
    assert M._uncovered_gate_refs(
        "precondition_unmet: waiting on g-115-1 to complete",
        pcs, "g-999-99") == ["g-115-1"]


def test_the_longer_id_itself_is_still_covered():
    """SPECIFICITY: the very same predicate DOES cover its own full id."""
    pcs = [{"type": "goal_status", "id": "pc-a", "goal_id": "g-115-1364"}]
    assert M._uncovered_gate_refs(
        "precondition_unmet: waiting on g-115-1364 to complete",
        pcs, "g-999-99") == []


def test_short_id_collision_blocks_the_clear_end_to_end(monkeypatch, capsys):
    """The boundary defect must change the SWEEP's verdict, not just the helper.

    Pre-fix this goal cleared. It must now be reported as an uncovered-gate
    skip, carrying the id the defer actually named.
    """
    g = _goal(
        defer_text="precondition_unmet: blocked until g-115-1 lands",
        pcs=[{"type": "file_check", "id": "pc-a",
              "path": EXISTING_PATH,
              "note": "tracked alongside g-115-1364"}],
    )
    rc, data = _run_main(monkeypatch, capsys, [g])
    assert rc == 0
    assert data["would_clear"] == []
    assert data["skipped_uncovered_gate"] == 1
    row = [d for d in data["details"] if d["goal_id"] == g["id"]][0]
    assert row["action"] == "skipped"
    assert row["uncovered_gate_refs"] == ["g-115-1"]
    assert row["would_have_cleared_pre_fix"] is True


def test_multi_letter_suffix_is_not_truncated_in_the_reported_ref():
    """An id like -fixture must be reported WHOLE, not as -f.

    A truncated ref both misnames the gate for the operator reading the skip
    reason and makes the defer-side token disagree with the same id read off a
    predicate.
    """
    pcs = [{"type": "file_check", "id": "pc-a", "path": "/nonexistent"}]
    assert M._uncovered_gate_refs(
        "precondition_unmet: waiting on g-373-27-fixture",
        pcs, "g-999-99") == ["g-373-27-fixture"]


def test_multi_letter_suffix_matches_the_same_id_on_a_predicate():
    """SPECIFICITY twin: both sides tokenize the suffixed id identically."""
    pcs = [{"type": "goal_status", "id": "pc-a", "goal_id": "g-373-27-fixture"}]
    assert M._uncovered_gate_refs(
        "precondition_unmet: waiting on g-373-27-fixture",
        pcs, "g-999-99") == []


def test_gid_tokens_is_the_one_tokenizer_for_both_sides():
    """rb-11386 invariant, asserted directly: same producer, both sides."""
    assert M._gid_tokens("g-115-1 and g-115-1364") == {"g-115-1", "g-115-1364"}
    assert M._gid_tokens("") == set()
    assert M._gid_tokens(None) == set()
    # membership, not containment — the whole point of the set
    assert "g-115-1" not in M._gid_tokens("g-115-1364")
