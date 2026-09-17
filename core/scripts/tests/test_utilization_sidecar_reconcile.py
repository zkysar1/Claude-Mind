"""test_utilization_sidecar_reconcile.py — .

Pins `plan()`, the pure decision function behind the one-time sidecar
reconcile. Every assertion here is about WHICH fields move and in WHICH
direction, because that is the whole risk surface: the values feed
`utilization_of`, which feeds utility scoring and the retirement slate.

THE CONTROL THAT MATTERS is `test_times_active_is_never_touched`. The
originating goal's own progress_note proposed a max() over the utilization
dict; measured on guard-2115, embedded `times_active` advanced 15 -> 22 over
twelve days while the sidecar stood at 11, i.e. the live surface for that
counter is the OPPOSITE of what it is for `retrieval_count` on the same record.
A widening of INT_FIELDS that reintroduced times_active would look harmless and
would write a stale value over a live one, so the narrowness is pinned by a
test rather than left to review.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "utilization-sidecar-reconcile.py"
sys.path.insert(0, str(SCRIPT.parent))

_spec = importlib.util.spec_from_file_location("utilization_sidecar_reconcile", SCRIPT)
recon = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(recon)

plan = recon.plan


def test_stale_low_retrieval_count_is_raised_to_embedded():
    side = {"rb-1": {"retrieval_count": 12}}
    emb = {"rb-1": {"retrieval_count": 48}}
    assert plan(side, emb) == {"rb-1": {"retrieval_count": 48}}


def test_sidecar_ahead_is_left_alone():
    """The common case after : the sidecar has raced past embedded."""
    side = {"rb-1": {"retrieval_count": 49}}
    emb = {"rb-1": {"retrieval_count": 28}}
    assert plan(side, emb) == {}


def test_equal_is_a_no_op_so_the_pass_is_idempotent():
    side = {"rb-1": {"retrieval_count": 5, "last_retrieved": "2026-09-15"}}
    emb = {"rb-1": {"retrieval_count": 5, "last_retrieved": "2026-09-15"}}
    assert plan(side, emb) == {}


def test_missing_sidecar_counter_counts_as_stale():
    """A flip-window row seeded without the counter reads as zero downstream."""
    side = {"g-1": {}}
    emb = {"g-1": {"retrieval_count": 3}}
    assert plan(side, emb) == {"g-1": {"retrieval_count": 3}}


def test_older_last_retrieved_is_advanced():
    side = {"g-1": {"last_retrieved": "2026-08-03"}}
    emb = {"g-1": {"last_retrieved": "2026-09-15"}}
    assert plan(side, emb) == {"g-1": {"last_retrieved": "2026-09-15"}}


def test_newer_sidecar_last_retrieved_is_kept():
    """max() runs both ways — a live sidecar date must not be rolled back."""
    side = {"g-1": {"last_retrieved": "2026-09-14"}}
    emb = {"g-1": {"last_retrieved": "2026-08-20"}}
    assert plan(side, emb) == {}


def test_times_active_is_never_touched():
    """THE control. Widening INT_FIELDS to this counter is the known hazard."""
    side = {"g-2115": {"times_active": 11, "retrieval_count": 49}}
    emb = {"g-2115": {"times_active": 22, "retrieval_count": 28}}
    assert plan(side, emb) == {}
    assert "times_active" not in recon.INT_FIELDS
    assert "times_helpful" not in recon.INT_FIELDS
    assert recon.INT_FIELDS == ("retrieval_count",)
    assert recon.DATE_FIELDS == ("last_retrieved",)


def test_id_absent_from_embedded_is_skipped():
    side = {"rb-new": {"retrieval_count": 0}}
    assert plan(side, {}) == {}


def test_id_absent_from_sidecar_is_skipped_for_first_touch_seeding():
    """Missing rows self-heal via utilization-flush._seed_from_content."""
    assert plan({}, {"rb-x": {"retrieval_count": 9}}) == {}


def test_bool_is_not_treated_as_an_int_counter():
    side = {"g-1": {"retrieval_count": False}}
    emb = {"g-1": {"retrieval_count": True}}
    assert plan(side, emb) == {}


def test_empty_embedded_date_does_not_clobber_a_real_one():
    side = {"g-1": {"last_retrieved": "2026-09-01"}}
    emb = {"g-1": {"last_retrieved": ""}}
    assert plan(side, emb) == {}


def test_both_fields_move_together_on_one_record():
    side = {"g-1": {"retrieval_count": 1, "last_retrieved": "2026-08-01"}}
    emb = {"g-1": {"retrieval_count": 4, "last_retrieved": "2026-09-10"}}
    assert plan(side, emb) == {
        "g-1": {"retrieval_count": 4, "last_retrieved": "2026-09-10"}
    }


# ── merge_counters: the WRITE-layer decision () ─────────────────────
#
# `plan()` above decides from a snapshot taken BEFORE the flush lock. Everything
# under this line is about what happens when the row MOVED in that gap — which
# `plan()` cannot see and therefore cannot pin. Until  the write layer
# did an unconditional `dict.update()` of the proposal, so the module's own
# "max() can lose nothing" safety argument held at the plan layer and nowhere
# else.

merge_counters = recon.merge_counters


def test_merge_does_not_roll_back_a_live_counter():
    """THE recorded race, verbatim ().

    Replayed from the modifier body against a row a concurrent flush had
    raised: live {51, 2026-09-16}, proposal {48, 2026-09-15}. Before the fix
    this wrote 48 and 2026-09-15 — a lost update AND a date rolled BACKWARD,
    on the one field the pass exists to make honest.
    """
    live = {"retrieval_count": 51, "last_retrieved": "2026-09-16"}
    proposed = {"retrieval_count": 48, "last_retrieved": "2026-09-15"}
    assert merge_counters(live, proposed) == {
        "retrieval_count": 51, "last_retrieved": "2026-09-16"
    }


def test_merge_applies_the_backfill_when_the_live_row_is_still_stale():
    """The pass must still do its job when nothing raced it."""
    live = {"retrieval_count": 12, "last_retrieved": "2026-08-01"}
    proposed = {"retrieval_count": 48, "last_retrieved": "2026-09-15"}
    assert merge_counters(live, proposed) == {
        "retrieval_count": 48, "last_retrieved": "2026-09-15"
    }


def test_merge_is_per_field_not_whole_record():
    """One field raced, the other did not — each is decided on its own."""
    live = {"retrieval_count": 51, "last_retrieved": "2026-08-01"}
    proposed = {"retrieval_count": 48, "last_retrieved": "2026-09-15"}
    assert merge_counters(live, proposed) == {
        "retrieval_count": 51, "last_retrieved": "2026-09-15"
    }


def test_merge_seeds_a_field_the_live_row_lacks():
    assert merge_counters({}, {"retrieval_count": 7}) == {"retrieval_count": 7}
    assert merge_counters(None, {"last_retrieved": "2026-09-15"}) == {
        "last_retrieved": "2026-09-15"
    }


def test_merge_preserves_counters_the_proposal_does_not_name():
    """times_active must survive untouched — the narrowness control, at write time."""
    live = {"retrieval_count": 51, "times_active": 22, "times_helpful": 3}
    out = merge_counters(live, {"retrieval_count": 48})
    assert out == {"retrieval_count": 51, "times_active": 22, "times_helpful": 3}


def test_merge_does_not_mutate_the_live_dict():
    live = {"retrieval_count": 12}
    merge_counters(live, {"retrieval_count": 48})
    assert live == {"retrieval_count": 12}


def test_merge_bool_live_value_is_not_treated_as_an_int_counter():
    """Mirrors plan()'s guard: a bool row is corrupt, so the proposal wins."""
    assert merge_counters({"retrieval_count": True}, {"retrieval_count": 4}) == {
        "retrieval_count": 4
    }


def test_merge_wrong_typed_live_date_takes_the_proposal():
    assert merge_counters({"last_retrieved": None}, {"last_retrieved": "2026-09-15"}) == {
        "last_retrieved": "2026-09-15"
    }


# ── reconcile_kind end-to-end: the race, through the real lock ───────────────


def test_reconcile_kind_end_to_end_does_not_roll_back_a_raced_sidecar(tmp_path, monkeypatch):
    """The whole path — real plan(), real flush lock, real locked_modify_jsonl.

    The race is made deterministic rather than timed: `load_counters` returns
    the PRE-LOCK snapshot (45 / 2026-09-14) while the sidecar ON DISK already
    carries the values a concurrent flush wrote (51 / 2026-09-16). That is
    exactly the gap between L139 and L182 — the plan is computed from the
    snapshot, the write happens against the live file.
    """
    sidecar = tmp_path / "guardrails-utilization.jsonl"
    sidecar.write_text(
        json.dumps({"id": "guard-1",
                    "utilization": {"retrieval_count": 51,
                                    "last_retrieved": "2026-09-16",
                                    "times_active": 22}}) + "\n"
        + json.dumps({"id": "guard-2",
                      "utilization": {"retrieval_count": 3,
                                      "last_retrieved": "2026-08-01"}}) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(recon.us, "load_counters", lambda kind, wd=None: {
        "guard-1": {"retrieval_count": 45, "last_retrieved": "2026-09-14"},
        "guard-2": {"retrieval_count": 3, "last_retrieved": "2026-08-01"},
    })
    monkeypatch.setattr(recon, "load_embedded", lambda kind, wd=None: {
        "guard-1": {"retrieval_count": 48, "last_retrieved": "2026-09-15"},
        "guard-2": {"retrieval_count": 9, "last_retrieved": "2026-09-10"},
    })

    summary, edits = recon.reconcile_kind("guardrails", tmp_path, True)
    assert summary["applied"] is True
    assert set(edits) == {"guard-1", "guard-2"}

    rows = {json.loads(line)["id"]: json.loads(line)["utilization"]
            for line in sidecar.read_text(encoding="utf-8").splitlines() if line.strip()}

    # The raced row keeps BOTH live values — no lost update, no backward date.
    assert rows["guard-1"]["retrieval_count"] == 51
    assert rows["guard-1"]["last_retrieved"] == "2026-09-16"
    # ...and the counter this pass must never touch is still there, untouched.
    assert rows["guard-1"]["times_active"] == 22
    # The un-raced row still gets its backfill: the fix is not a no-op.
    assert rows["guard-2"]["retrieval_count"] == 9
    assert rows["guard-2"]["last_retrieved"] == "2026-09-10"


# ---------------------------------------------------------------------------
#  — the two edges the  fix left open.
#
# Both were found by /fresh-eyes-code on the fix ITSELF, and they are the same
# defect that fix was written to remove, one level down: `plan()` (pure decider)
# was guarded and tested, `merge_counters` (the new writer) was guarded and
# tested LESS. A fix's own tests walk the path the bug took; they do not probe
# the surface the fix just created.
# ---------------------------------------------------------------------------


def test_merge_treats_a_malformed_live_counters_value_as_absent():
    """A truthy non-dict `live` must not raise — plan() skips such a row.

    `dict("x")` raises ValueError and `dict([1, 2])` raises TypeError, so the
    pre-fix `dict(live or {})` turned one malformed row into a hard abort of the
    whole kind (the raise escapes _modifier, escapes locked_modify_jsonl, and
    the lock is already held). Falsy non-dicts never reproduced it, which is why
    the original 8 merge_* tests missed it: `[]`, `0` and `None` all short to {}.
    """
    for bad in ("x", "ab", [1, 2], 7, 0.5):
        out = recon.merge_counters(bad, {"retrieval_count": 5})
        assert out == {"retrieval_count": 5}, f"live={bad!r} did not behave as absent"
    # The falsy non-dicts must keep behaving exactly as before (no regression).
    for falsy in (None, {}, [], 0, ""):
        assert recon.merge_counters(falsy, {"retrieval_count": 5}) == {"retrieval_count": 5}


def test_reconcile_kind_skips_a_malformed_row_instead_of_aborting_the_kind(tmp_path, monkeypatch):
    """One bad row must not cost the other rows their backfill.

    This is the assertion that makes the guard worth having: it is not that
    merge_counters returns something reasonable for garbage, it is that the
    GOOD row in the same file still reconciles.
    """
    sidecar = tmp_path / "guardrails-utilization.jsonl"
    sidecar.write_text(
        json.dumps({"id": "guard-bad", "utilization": "not-a-dict"}) + "\n"
        + json.dumps({"id": "guard-good",
                      "utilization": {"retrieval_count": 3,
                                      "last_retrieved": "2026-08-01"}}) + "\n",
        encoding="utf-8",
    )
    # Both ids are in the pre-lock snapshot as well-formed dicts — which is how
    # a malformed LIVE row reaches _modifier at all: the snapshot is read
    # unlocked, so the row can be rewritten between the read and the lock.
    monkeypatch.setattr(recon.us, "load_counters", lambda kind, wd=None: {
        "guard-bad": {"retrieval_count": 1, "last_retrieved": "2026-08-01"},
        "guard-good": {"retrieval_count": 3, "last_retrieved": "2026-08-01"},
    })
    monkeypatch.setattr(recon, "load_embedded", lambda kind, wd=None: {
        "guard-bad": {"retrieval_count": 4, "last_retrieved": "2026-09-10"},
        "guard-good": {"retrieval_count": 9, "last_retrieved": "2026-09-10"},
    })

    summary, _ = recon.reconcile_kind("guardrails", tmp_path, True)
    assert summary["applied"] is True

    rows = {json.loads(line)["id"]: json.loads(line)["utilization"]
            for line in sidecar.read_text(encoding="utf-8").splitlines() if line.strip()}
    # The good row got its backfill — the kind did not abort.
    assert rows["guard-good"]["retrieval_count"] == 9
    assert rows["guard-good"]["last_retrieved"] == "2026-09-10"
    # The malformed row is rebuilt from the proposal rather than crashing.
    assert rows["guard-bad"]["retrieval_count"] == 4


def test_summary_reports_the_realized_gap_not_only_the_pre_lock_proposal(tmp_path, monkeypatch):
    """`int_total_gap` sizes the PROPOSAL; `int_applied_gap` sizes the OUTCOME.

    Same deterministic race as the end-to-end test above. The write is correct
    either way — merge_counters refuses the rollback — so this pins the half
    that was still wrong: reporting a closed gap of 5 beside `applied: true`
    when nothing moved.
    """
    sidecar = tmp_path / "guardrails-utilization.jsonl"
    sidecar.write_text(
        json.dumps({"id": "guard-1",
                    "utilization": {"retrieval_count": 60,
                                    "last_retrieved": "2026-09-16"}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(recon.us, "load_counters", lambda kind, wd=None: {
        "guard-1": {"retrieval_count": 45, "last_retrieved": "2026-09-14"},
    })
    monkeypatch.setattr(recon, "load_embedded", lambda kind, wd=None: {
        "guard-1": {"retrieval_count": 50, "last_retrieved": "2026-09-15"},
    })

    summary, _ = recon.reconcile_kind("guardrails", tmp_path, True)
    assert summary["int_total_gap"] == 5           # the proposal, from the snapshot
    assert summary["records_to_edit"] == 1         # ditto
    assert summary["int_applied_gap"] == 0         # what actually moved: nothing
    assert summary["records_actually_changed"] == 0
    # ...and the store is genuinely untouched, so the two figures disagree for
    # the right reason.
    row = json.loads(sidecar.read_text(encoding="utf-8").strip())["utilization"]
    assert row["retrieval_count"] == 60


def test_realized_gap_equals_the_proposal_when_nothing_raced(tmp_path, monkeypatch):
    """The positive control for the test above.

    Without this, `int_applied_gap == 0` passes trivially for a field that is
    never populated. Here the live row IS stale, so the realized gap must equal
    the proposed one — the two figures agree when there is no race.
    """
    sidecar = tmp_path / "guardrails-utilization.jsonl"
    sidecar.write_text(
        json.dumps({"id": "guard-1",
                    "utilization": {"retrieval_count": 45,
                                    "last_retrieved": "2026-09-14"}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(recon.us, "load_counters", lambda kind, wd=None: {
        "guard-1": {"retrieval_count": 45, "last_retrieved": "2026-09-14"},
    })
    monkeypatch.setattr(recon, "load_embedded", lambda kind, wd=None: {
        "guard-1": {"retrieval_count": 50, "last_retrieved": "2026-09-15"},
    })

    summary, _ = recon.reconcile_kind("guardrails", tmp_path, True)
    assert summary["int_total_gap"] == 5
    assert summary["int_applied_gap"] == 5
    assert summary["records_actually_changed"] == 1
    row = json.loads(sidecar.read_text(encoding="utf-8").strip())["utilization"]
    assert row["retrieval_count"] == 50
