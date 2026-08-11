"""Tests for exp_capture_drain — the reducer-side exp_capture drain ().

The two functions under test are the ones carrying real logic: `conforms`
(which entries may be encoded at all) and `anchor_objects` (the cross-store
shape mapping that the daemon validator refuses without).

The anchor tests are the load-bearing ones. worker-loop Phase 3.6 documents
`verbatim_anchors` as BARE STRINGS; the experience record documents the same
field name as `{key, content}` objects and `_validate_record` rejects strings
outright. Passing the worker's list through unchanged failed all 8 encodes on
2026-08-10. A future "simplification" back to pass-through re-breaks every
encode at once and silently — the entries stay queued and the drain reports
failures to stderr inside a fail-open call site — so these pins exist to make
that change fail loudly here instead.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

import exp_capture_drain as drain  # noqa: E402


# ── conforms: the documented worker-loop Phase 3.6 shape ────────────────────

def _worker_entry(**over):
    e = {
        "goal_id": "g-115-4247",
        "category": "framework-architecture",
        "execution_summary": "Removed the swallow-decorator coverage hole from three test files.",
        "outcome_class": "deep",
        "key_decisions": ["Chose re-raise over skip because a swallowed failure reads as a pass."],
        "surprise_level": 7,
        "verbatim_anchors": ["pytest.raises", "core/scripts/tests/test_x.py:44"],
    }
    e.update(over)
    return e


def test_documented_shape_conforms():
    assert drain.conforms(_worker_entry())


@pytest.mark.parametrize("entry", [
    # The three non-conforming shapes measured live in the slot on 2026-08-10.
    {"goal_id": "g-115-5700", "summary": "x", "outcome": "y",
     "lesson": "z", "what_failed": "a", "what_worked": "b"},
    {"goal_id": "g-115-5689", "summary": "x", "outcome": "y", "note": "z"},
    # Degenerate forms.
    {"execution_summary": "no goal id at all, which cannot be attributed"},
    {"goal_id": "g-1-1"},
    "a bare string, not a dict",
    None,
], ids=["retro-shape", "note-shape", "no-goal-id", "no-summary", "bare-string", "none"])
def test_nonconforming_shapes_rejected(entry):
    assert not drain.conforms(entry)


def test_summary_below_daemon_floor_is_rejected_here():
    """Reject locally rather than letting the daemon warn.

    MIN_SUMMARY_CHARS is a WARNING in the endpoint, not an error — so a too-thin
    entry would encode successfully and be dropped from the slot, converting a
    visible queue item into a near-empty permanent record.
    """
    assert not drain.conforms(_worker_entry(execution_summary="too short"))
    assert drain.conforms(_worker_entry(execution_summary="x" * drain.MIN_SUMMARY_CHARS))


def test_conforms_does_not_require_the_optional_fields():
    """Only goal_id + execution_summary are required; the rest degrade to empty.

    A worker unit with no decisions and no anchors ("nothing surprising
    happened") is a legitimate narrative — Phase 3.6 is explicit that capturing
    only interesting units biases the archive toward drama.
    """
    minimal = {"goal_id": "g-1-1", "execution_summary": "A routine sweep that found nothing."}
    assert drain.conforms(minimal)


# ── anchor_objects: the cross-store shape mapping ───────────────────────────

def test_bare_strings_become_key_content_objects():
    out = drain.anchor_objects(["exp-g-115-1655-evict-2", "rc=124"])
    assert all(isinstance(a, dict) and "key" in a and "content" in a for a in out), out
    assert [a["content"] for a in out] == ["exp-g-115-1655-evict-2", "rc=124"]


def test_content_is_preserved_verbatim():
    """The whole value of an anchor is that it is EXACT. Only `key` is derived."""
    anchors = [
        "MemoryPersistenceVerticle.java:2267 writeConsolidatedMemory removed",
        "/home/ec2-user/AyoAi-Efs/mnt/AyoAi/Accounts",
        "interval_hours=461.329",
        "msg-20260810-111841-alpha-5155",
    ]
    out = drain.anchor_objects(anchors)
    assert [a["content"] for a in out] == anchors


def test_keys_are_unique_even_for_identical_content():
    out = drain.anchor_objects(["same", "same", "same"])
    assert len({a["key"] for a in out}) == 3


def test_key_survives_content_with_no_alphanumerics():
    out = drain.anchor_objects(["///", "==="])
    assert all(a["key"] for a in out), out
    assert len({a["key"] for a in out}) == 2


def test_already_shaped_anchors_pass_through_untouched():
    shaped = [{"key": "k", "content": "c", "extra": 1}]
    assert drain.anchor_objects(shaped) == shaped


@pytest.mark.parametrize("raw", [None, "not a list", 42, {}])
def test_non_list_anchors_degrade_to_empty(raw):
    assert drain.anchor_objects(raw) == []


def test_empty_and_whitespace_anchors_dropped():
    assert drain.anchor_objects(["", "   ", "real"]) == [
        {"key": "real", "content": "real"}]


def test_mapped_anchors_satisfy_the_daemon_validator_predicate():
    """Pin against the endpoint's ACTUAL check, not a paraphrase of it.

    Mirrors experience_write._validate_record: every anchor must be a dict
    carrying both 'key' and 'content'. This is the assertion that would have
    caught the 2026-08-10 failure before it reached the daemon.
    """
    out = drain.anchor_objects(_worker_entry()["verbatim_anchors"])
    assert out, "worker anchors must not map to an empty list"
    for anchor in out:
        assert isinstance(anchor, dict) and "key" in anchor and "content" in anchor


# ── render_trace: the .md body ──────────────────────────────────────────────

def test_trace_clears_the_daemon_minimum_size():
    """MIN_TRACE_BYTES is 200 in the endpoint; a thinner body triggers a warning."""
    minimal = {"goal_id": "g-1-1", "execution_summary": "A routine sweep that found nothing new."}
    assert len(render_bytes(minimal)) >= 200


def render_bytes(entry):
    return drain.render_trace(entry).encode("utf-8")


def test_trace_carries_every_worker_field():
    e = _worker_entry()
    body = drain.render_trace(e)
    assert e["goal_id"] in body
    assert e["execution_summary"] in body
    assert e["key_decisions"][0] in body
    for a in e["verbatim_anchors"]:
        assert a in body
    assert "surprise_level: 7" in body
    assert "outcome_class: deep" in body


def test_trace_front_matter_opens_on_line_one():
    """`---` must be the first line or every front-matter parser sees nothing."""
    assert drain.render_trace(_worker_entry()).splitlines()[0] == "---"


# ── CLI contract ───────────────────────────────────────────────────────────

def _run_cli(*args, slot=None, tmp_path=None):
    argv = [sys.executable, str(SCRIPT_DIR / "exp_capture_drain.py"), "--json"]
    if slot is not None:
        p = tmp_path / "slot.json"
        p.write_text(json.dumps(slot), encoding="utf-8")
        argv += ["--slot-json", str(p)]
    argv += list(args)
    return subprocess.run(argv, capture_output=True, text=True, cwd=str(PROJECT_ROOT))


def _report(proc):
    # The JSON report is the last object printed; human lines precede it.
    start = proc.stdout.index("{")
    return json.loads(proc.stdout[start:])


def test_dry_run_reports_encodable_count_without_writing(tmp_path):
    slot = [_worker_entry(), {"goal_id": "g-2-2", "note": "wrong shape"}]
    proc = _run_cli(slot=slot, tmp_path=tmp_path)
    assert proc.returncode == 0
    r = _report(proc)
    assert r["verdict"] == "dry-run"
    assert r["scanned"] == 2
    assert r["conforming"] == 1
    assert r["nonconforming"] == 1
    # The dry-run count must not read 0 over encodable entries: a "would
    # encode=0" line over a populated slot reads as a clean queue, which is the
    # same false all-clear this drain exists to end.
    assert r["drained"] == 1


def test_empty_slot_is_distinct_from_unreadable_slot(tmp_path):
    """guard-2352: a fail-open guard that renders 'nothing to do' and 'could not
    look' identically reports healthy while inert."""
    empty = _run_cli(slot=[], tmp_path=tmp_path)
    assert _report(empty)["verdict"] == "empty"

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "exp_capture_drain.py"), "--json",
         "--slot-json", str(bad)],
        capture_output=True, text=True, cwd=str(PROJECT_ROOT))
    assert proc.returncode == 0, "must stay fail-open"
    assert _report(proc)["verdict"] == "read-failed"


def test_exits_zero_on_a_malformed_slot_payload(tmp_path):
    """Fail-open by contract: the call site is inside a goal close."""
    proc = _run_cli(slot={"not": "a list"}, tmp_path=tmp_path)
    assert proc.returncode == 0
    assert _report(proc)["verdict"] == "read-failed"


# ── wiring ─────────────────────────────────────────────────────────────────

def test_drain_is_wired_into_iteration_close():
    """An unwired script is indistinguishable from one that never runs.

    guard-399's amendment (2): the operative test is WHO executes the call — a
    script the flow already runs, or a model reading a file. This pin fails if
    the call is ever removed from iteration-close.sh, which would silently
    return the slot to having a writer and no consumer.
    """
    text = (SCRIPT_DIR / "iteration-close.sh").read_text(encoding="utf-8")
    assert "exp_capture_drain.py" in text, "drain call missing from iteration-close.sh"

    # ...and inside do_state_update, not merely present in the file. Function
    # bodies are contiguous, so the call must fall between this function's
    # header and the next do_* header.
    lines = text.splitlines()
    starts = {i for i, ln in enumerate(lines) if ln.startswith("do_") and ln.rstrip().endswith("() {")}
    su = next(i for i, ln in enumerate(lines) if ln.startswith("do_state_update()"))
    nxt = min((i for i in starts if i > su), default=len(lines))
    call = next(i for i, ln in enumerate(lines) if "exp_capture_drain.py" in ln)
    assert su < call < nxt, f"drain call at line {call+1} is outside do_state_update ({su+1}..{nxt})"


def test_call_site_passes_apply():
    """A wired dry-run would report healthy forever while draining nothing."""
    text = (SCRIPT_DIR / "iteration-close.sh").read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if "exp_capture_drain.py" in ln)
    assert "--apply" in line, f"call site is not in apply mode: {line.strip()}"


def test_no_op_close_does_not_rewrite_the_slot(tmp_path):
    """A close that encodes nothing must not write the slot at all.

    This drain runs on EVERY goal close. Once the conforming backlog is drained
    the steady state is a slot holding only un-encodable entries, and rewriting
    it every close would advance slot_meta.update_count each time — the field
    wm_write.py uses as the CAS token for the loop_state stale-lock-steal check.
    A no-op write is therefore churn on a concurrency primitive, not a wasted
    millisecond.
    """
    only_nonconforming = [{"goal_id": "g-2-2", "summary": "s", "outcome": "success", "note": "n"}]
    proc = _run_cli("--apply", slot=only_nonconforming, tmp_path=tmp_path)
    assert proc.returncode == 0
    r = _report(proc)
    assert r["drained"] == 0
    assert r["residue_write"] == "skipped-unchanged"
    assert r["residue_count"] == 1
