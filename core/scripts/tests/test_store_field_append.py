"""test_store_field_append.py — gap-106 / .

Covers ONLY what store-field-append.py adds over its SSOT. The four pure
helpers (sentinel_for / compose / verify_post, and the shape of the projection
check) are already pinned by test_goal_field_append.py, and this module IMPORTS
them rather than re-typing them — so re-asserting their behaviour here would
test the same function object twice and report it as two covered properties.

What IS new, and therefore tested:

  1. the anti-fork property itself — the helpers must be the SSOT's objects,
     not copies. This is the one test that fails if someone "simplifies" the
     importlib indirection by pasting the functions in.
  2. extract_row       — three payload shapes these two readers actually use
  3. is_read_projected — now canary-parameterised per store
  4. the --anchor drift guard, which has no goal-side equivalent
  5. idempotence-BEFORE-anchor ordering, a deliberate decision that a later
     reordering would silently invert
  6. STORES table integrity, so a third store cannot be added half-wired
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sfa = _load("_store_field_append", "store-field-append.py")
gfa = _load("_goal_field_append_ref", "goal-field-append.py")


# ── 1. the anti-fork property ─────────────────────────────────────────────

def test_helpers_are_the_ssot_objects_not_copies():
    """The contract must not be forked into a second implementation.

    store-field-append.py imports compose/verify_post/sentinel_for from
    goal-field-append.py precisely so a later fix to the verification rule
    cannot land in one file and silently miss the other.

    The assertion is on each function's DEFINING FILE, not on object identity:
    _load_ssot() builds a fresh module object per call, so identity can never
    hold across two independent loads and asserting it would fail on correct
    code (it did, first run). co_filename is the property that actually
    discriminates — a pasted copy would report store-field-append.py and pass
    every behavioural assertion on the day it was pasted.
    """
    for fn in (sfa.compose, sfa.verify_post, sfa.sentinel_for, sfa.cas_conflict,
               sfa.wrapped_marker_refusal):
        assert Path(fn.__code__.co_filename).name == "goal-field-append.py", (
            f"{fn.__name__} is defined in {fn.__code__.co_filename} — the contract has been "
            "forked out of its SSOT")

    # ...and it must still BEHAVE like the SSOT's, so the import is not merely
    # pointing at the right file while something rebinds the name.
    ssot = sfa._load_ssot()
    assert sfa.compose("A", "B", "m") == ssot.compose("A", "B", "m")
    assert sfa.sentinel_for("m") == ssot.sentinel_for("m")
    assert sfa.cas_conflict("A", "A") == ssot.cas_conflict("A", "A")
    assert sfa.cas_conflict("A", "A\n\nB") == ssot.cas_conflict("A", "A\n\nB")
    # The marker convention and its refusal must agree across BOTH sides, or one
    # script accepts the paste the other rejects ().
    assert sfa.wrapped_marker_refusal("[appended:m]") == ssot.wrapped_marker_refusal("[appended:m]")
    assert sfa.wrapped_marker_refusal("plain") is None


def test_missing_ssot_fails_loud_rather_than_degrading():
    """A missing SSOT must raise, never fall back to a local re-implementation."""
    original = sfa._SSOT
    try:
        sfa._SSOT = SCRIPTS / "goal-field-append-does-not-exist.py"
        with pytest.raises((ImportError, FileNotFoundError)):
            sfa._load_ssot()
    finally:
        sfa._SSOT = original


# ── 2. extract_row: the payload shapes these readers really emit ──────────

def test_extract_row_accepts_a_bare_list():
    rows = sfa.extract_row([{"id": "guard-1"}], ("guardrails",))
    assert rows == [{"id": "guard-1"}]


def test_extract_row_accepts_a_keyed_envelope():
    payload = {"guardrails": [{"id": "guard-1"}], "count": 1}
    assert sfa.extract_row(payload, ("guardrails", "results")) == [{"id": "guard-1"}]


def test_extract_row_accepts_a_bare_single_record_object():
    """Both readers may return the record itself rather than an envelope."""
    assert sfa.extract_row({"id": "rb-1", "content": "x"}, ("reasoning_bank",)) == [
        {"id": "rb-1", "content": "x"}
    ]


def test_extract_row_returns_empty_for_an_unrecognised_shape():
    """Empty must be returned rather than guessed at — read_record turns it into
    a loud refusal, and a guess here would append onto a record nobody read."""
    assert sfa.extract_row({"unexpected": {"id": "x"}}, ("guardrails",)) == []
    assert sfa.extract_row("not json-ish", ("guardrails",)) == []


# ── 3. is_read_projected, per-store ───────────────────────────────────────

def test_record_with_a_store_canary_is_not_projected():
    row = {"id": "guard-1", "action_hint": "do the thing"}
    assert sfa.is_read_projected(row, sfa.STORES["guardrails"]["canaries"]) is False


def test_record_without_any_canary_reads_as_projected():
    """Key COUNT is not the discriminator — a wide row with no canary still
    cannot vouch that the long-text fields came back."""
    row = {"id": "guard-1", "category": "x", "severity": "high", "status": "active"}
    assert sfa.is_read_projected(row, sfa.STORES["guardrails"]["canaries"]) is True


def test_canaries_are_store_specific():
    """A reasoning-bank canary must not vouch for a guardrails read.

    Anti-vacuity for the two tests above: they would both pass on a check that
    ignored its canaries argument entirely.
    """
    rb_row = {"id": "rb-1", "content": "text"}
    assert sfa.is_read_projected(rb_row, sfa.STORES["reasoning-bank"]["canaries"]) is False
    assert sfa.is_read_projected(rb_row, sfa.STORES["guardrails"]["canaries"]) is True


def test_non_dict_reads_as_projected():
    assert sfa.is_read_projected(None, ("content",)) is True
    assert sfa.is_read_projected(["a"], ("content",)) is True


# ── 4/5. the --anchor drift guard and its ordering against idempotence ────

def _fake_record(pre: str) -> dict:
    return {"id": "guard-1", "action_hint": pre}


def _run_main(monkeypatch, pre, argv, write_calls=None):
    """Drive main() with the read stubbed and the write captured."""
    monkeypatch.setattr(sfa, "read_record", lambda store, rid: _fake_record(pre))

    def _fake_run(cmd):
        if write_calls is not None:
            write_calls.append(cmd)

        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(sfa, "_run", _fake_run)
    return sfa.main(argv)


def test_a_wrapped_marker_is_refused_at_exit_2_and_never_reaches_a_write(monkeypatch):
    """The SSOT pins the predicate; this pins that the STORE side is WIRED to it.

    A helper that is imported but never called is indistinguishable from an
    absent one at the only layer a caller sees (guard-1943: a green suite
    certifies the FUNCTION, never the WIRING). The write-capture is the load-
    bearing half — a refusal that still wrote would be the original defect.
    """
    writes = []
    with pytest.raises(SystemExit) as exc:
        _run_main(monkeypatch, "some existing note",
                  ["--store", "guardrails", "guard-1", "action_hint",
                   "[appended:g-001-847-double-wrap]", "new text"], writes)
    assert exc.value.code == sfa.RC_USAGE
    assert writes == [], "a refused marker must not reach the write"


def test_a_bare_marker_still_proceeds(monkeypatch):
    """Anti-vacuity for the test above: the new refusal must not refuse everything."""
    writes = []
    # It must get PAST the marker check. Whatever it does afterwards is other
    # tests' business, so assert only that it did not die at RC_USAGE.
    try:
        _run_main(monkeypatch, "some existing note",
                  ["--store", "guardrails", "guard-1", "action_hint",
                   "g-001-847-double-wrap", "new text"], writes)
    except SystemExit as exc:
        assert exc.code != sfa.RC_USAGE, "a BARE marker must not be refused as malformed"


def test_anchor_absent_is_refused_with_its_own_exit_code(monkeypatch):
    writes = []
    with pytest.raises(SystemExit) as exc:
        _run_main(monkeypatch, "some existing note",
                  ["--store", "guardrails", "--anchor", "NOT PRESENT",
                   "guard-1", "action_hint", "m1", "new text"], writes)
    assert exc.value.code == sfa.RC_ANCHOR_ABSENT
    assert writes == [], "a refused anchor must not reach the write"


def test_anchor_present_proceeds(monkeypatch):
    """Anti-vacuity for the test above: the guard must not refuse everything."""
    writes = []
    # read_record is called THREE times: PRE, the pre-write CAS re-read
    # (), then POST for verification. The first two must return the
    # SAME sentinel-free value — differing there is a concurrent modification
    # and the write is correctly refused, and a sentinel in either one would
    # short-circuit the idempotence branch so this test would pass without ever
    # exercising the anchor. Only the POST read carries the sentinel, so
    # verify_post is satisfied.
    state = {"n": 0}

    def _read(store, rid):
        state["n"] += 1
        return _fake_record("some existing note" if state["n"] <= 2
                            else "some existing note\n\nnew text\n[appended:m1]")

    monkeypatch.setattr(sfa, "read_record", _read)

    def _fake_run(cmd):
        writes.append(cmd)

        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(sfa, "_run", _fake_run)
    rc = sfa.main(["--store", "guardrails", "--anchor", "existing",
                   "guard-1", "action_hint", "m1", "new text"])
    assert rc == sfa.RC_OK
    assert len(writes) == 1, "the accepted path must actually write"


def test_idempotence_is_checked_BEFORE_anchor(monkeypatch):
    """A completed prior run is a no-op even when the anchor no longer holds.

    Reporting 'anchor absent' for work that already landed would send a caller
    chasing drift that does not exist. Reordering these two checks inverts that
    and this test is the only thing that would notice.
    """
    writes = []
    rc = _run_main(monkeypatch,
                   "existing note\n\nprior text\n[appended:m1]",
                   ["--store", "guardrails", "--anchor", "TEXT THAT IS GONE",
                    "guard-1", "action_hint", "m1", "new text"], writes)
    assert rc == sfa.RC_OK
    assert writes == [], "an idempotent no-op must not write"


def test_empty_text_is_refused(monkeypatch):
    with pytest.raises(SystemExit) as exc:
        _run_main(monkeypatch, "pre", ["--store", "guardrails",
                                       "guard-1", "action_hint", "m1", "\n\n"])
    assert exc.value.code == sfa.RC_VALUE_SHAPE


def test_non_text_field_is_refused(monkeypatch):
    """utilization is a dict on both stores; a nested write drops sibling keys."""
    monkeypatch.setattr(sfa, "read_record",
                        lambda store, rid: {"id": "guard-1", "action_hint": "x",
                                            "utilization": {"times_helpful": 3}})
    with pytest.raises(SystemExit) as exc:
        sfa.main(["--store", "guardrails", "guard-1", "utilization", "m1", "text"])
    assert exc.value.code == sfa.RC_FIELD_SHAPE


# ── 6. table integrity ────────────────────────────────────────────────────

def test_every_store_is_fully_wired():
    """A half-added store would fail at runtime on a KeyError, after the read."""
    for name, cfg in sfa.STORES.items():
        for key in ("read", "write", "rows_keys", "canaries"):
            assert key in cfg, f"{name} missing '{key}'"
        assert (SCRIPTS / cfg["read"]).exists(), f"{name}: read wrapper missing"
        assert (SCRIPTS / cfg["write"]).exists(), f"{name}: write wrapper missing"
        assert cfg["canaries"], f"{name}: an empty canary set disables the projection guard"


def test_exit_codes_are_distinguishable():
    """Each refusal must be tellable apart from the others AND from a transport
    failure — the discriminator guard-1047 asks for."""
    codes = [sfa.RC_OK, sfa.RC_USAGE, sfa.RC_READ_UNSAFE, sfa.RC_FIELD_SHAPE,
             sfa.RC_VALUE_SHAPE, sfa.RC_WRITE_FAILED, sfa.RC_VERIFY_FAILED,
             sfa.RC_ANCHOR_ABSENT]
    assert len(set(codes)) == len(codes), "exit codes collide"
    assert sfa.RC_ANCHOR_ABSENT not in (sfa.RC_USAGE, sfa.RC_WRITE_FAILED)


# ── 7. the pipeline store () ─────────────────────────────────────
#
# The third store, and the first whose shape was measured against this contract
# rather than assumed to match. These tests pin the three things the
# measurement found, because each one is a property of the CURRENT daemon and
# reader build — exactly the reason the projection guard was kept in the first
# place.

import re as _re


def _pipeline_record(position: str) -> dict:
    """The shape `pipeline-read.sh --id` actually returns: a BARE record object,
    no wrapper key, carrying the large free-text fields."""
    return {
        "id": "2026-09-15_some-hypothesis",
        "title": "A hypothesis",
        "stage": "active",
        "type": "calibration",
        "confidence": 0.55,
        "claim": "the claim",
        "rationale": "the rationale",
        "position": position,
    }


def _stub_run(store_state, writes):
    """Stand in for `_run`, dispatching on WHICH wrapper is being invoked.

    Stubbing `_run` rather than `read_record` is the point: it leaves the real
    `read_record` — and therefore this store's `rows_keys` and `canaries` — in
    the code path under test. A test that stubs `read_record` would pass
    identically with the pipeline entry deleted from STORES.
    """
    def _run(cmd):
        joined = " ".join(str(c) for c in cmd)

        class R:
            returncode = 0
            stderr = ""
            stdout = ""

        if "pipeline-read.sh" in joined:
            import json as _json
            R.stdout = _json.dumps(_pipeline_record(store_state["position"]))
        elif "pipeline-update-field.sh" in joined:
            writes.append(cmd)
            # Pin the argv SHAPE before indexing it (fresh-eyes F2, guard-920).
            # `bash_cmd` returns [BASH, script, *args] and the call is
            # _bash(write, record_id, field, new), so the composed value is cmd[-1]
            # TODAY. A future trailing flag would make the stub capture the flag,
            # and the byte-identity assertion below would then compare flag to flag
            # and PASS — a silently weakened test. This assert turns that into a red.
            assert len(cmd) == 5 and cmd[3] == "position", f"write argv shape changed: {cmd}"
            store_state["position"] = cmd[-1]   # the composed value
        return R
    return _run


def test_pipeline_bare_object_read_is_accepted(monkeypatch):
    """rows_keys is empty for pipeline; extract_row's bare-object branch carries it."""
    writes = []
    state = {"position": "an existing position paragraph"}
    monkeypatch.setattr(sfa, "_run", _stub_run(state, writes))
    row = sfa.read_record("pipeline", "2026-09-15_some-hypothesis")
    assert row["position"] == "an existing position paragraph"


def test_pipeline_projected_read_is_refused(monkeypatch):
    """Anti-vacuity for the test above: the canary set must still refuse a read
    that dropped the free-text fields, or it is not guarding anything."""
    def _run(cmd):
        class R:
            returncode = 0
            stderr = ""
            stdout = '{"id": "2026-09-15_some-hypothesis", "title": "t", "stage": "active"}'
        return R
    monkeypatch.setattr(sfa, "_run", _run)
    with pytest.raises(SystemExit) as exc:
        sfa.read_record("pipeline", "2026-09-15_some-hypothesis")
    assert exc.value.code == sfa.RC_READ_UNSAFE


def test_pipeline_second_append_with_same_marker_does_not_grow_the_field(monkeypatch):
    """The property the whole marker exists for, measured on the field LENGTH.

    Asserting only `rc == RC_OK` twice would pass against a helper that appended
    twice, and asserting only the write count would pass against one that wrote
    a truncated value. Growth-once is the claim; length is how it is checked.
    """
    writes = []
    state = {"position": "an existing position paragraph"}
    monkeypatch.setattr(sfa, "_run", _stub_run(state, writes))
    argv = ["--store", "pipeline", "2026-09-15_some-hypothesis", "position",
            "m-828", "a measured note"]

    assert sfa.main(list(argv)) == sfa.RC_OK
    after_first = state["position"]
    assert len(writes) == 1
    assert "a measured note" in after_first

    assert sfa.main(list(argv)) == sfa.RC_OK
    assert state["position"] == after_first, "the retry rewrote the field"
    assert len(writes) == 1, "the retry reached the write"
    assert after_first.count("a measured note") == 1


def test_pipeline_anchor_staleness_is_refused(monkeypatch):
    """--anchor must guard the pipeline store too, not only the first two."""
    writes = []
    state = {"position": "an existing position paragraph"}
    monkeypatch.setattr(sfa, "_run", _stub_run(state, writes))
    with pytest.raises(SystemExit) as exc:
        sfa.main(["--store", "pipeline", "--anchor", "TEXT THAT IS GONE",
                  "2026-09-15_some-hypothesis", "position", "m-828", "a note"])
    assert exc.value.code == sfa.RC_ANCHOR_ABSENT
    assert writes == [], "a refused anchor must not reach the write"


def test_shell_wrapper_store_list_matches_the_map():
    """The .sh validates --store itself, so its list is a SECOND source of truth.

    A store added to STORES but not to the wrapper is accepted by the .py and
    refused by the .sh at exit 2 — which reads as "unsupported store", not as a
    wiring bug. Nothing tested this until pipeline became the third store.
    """
    sh = (SCRIPTS / "store-field-append.sh").read_text(encoding="utf-8")
    m = _re.search(r'^_STORES="([^"]+)"', sh, _re.M)
    assert m, "store-field-append.sh no longer declares a single _STORES literal"
    assert set(m.group(1).split("|")) == set(sfa.STORES), (
        "the shell wrapper's --store list has drifted from STORES")


def test_compose_sentinel_defeats_the_pipeline_endpoints_value_coercion():
    """The pipeline write endpoint COERCES; the other two stores' does not.

    /v1/pipeline/update-field runs `_parse_value` on the string: "true"/"false"/
    "null" become bools/None, a leading { or [ is tried as JSON, and a numeric
    string becomes int/float. A composed append cannot reach any of those arms
    because compose() always ends the value with "\\n[appended:<marker>]" — so
    the sentinel that exists for IDEMPOTENCE is also what keeps a pipeline text
    field typed as text. Nothing else says so, and dropping the sentinel would
    silently retype the field rather than fail.

    The control half is load-bearing: without it a `_parse_value` that had been
    neutered into `return value_str` would pass every assertion above it.
    """
    import sys as _sys
    _sys.path.insert(0, str(SCRIPTS.parent.parent))
    from mind_api.src.world import pipeline_write as _pw

    for pre, text in [("", "a measured note"), ("", "true"), ("", "42"),
                      ("", '{"a": 1}'), ("", "[1, 2]"), ("old position", "note")]:
        composed = sfa.compose(pre, text, "m-828")
        assert _pw._parse_value(composed) == composed, (
            f"a composed append was coerced away from str: {text!r}")

    assert _pw._parse_value("true") is True
    assert isinstance(_pw._parse_value("42"), int)
    assert isinstance(_pw._parse_value('{"a": 1}'), dict)
