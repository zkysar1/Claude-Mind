"""Write-site bound for append-ordered goal narrative fields ().

THE DIRECTION THAT MATTERS. This code DELETES text from a live record, so the
cheap half is proving it cuts when it should. The load-bearing half is proving
it does NOT cut when it should not, that nothing is lost when it does, and that
it fails CLOSED (leaves the field whole) whenever any precondition is unmet.
Both directions are asserted below.

Measured premise (alpha/cc-04, 2026-09-21): g-115-817.progress_note held
206,540 B across 45 blocks written by five agents on five boxes -- a read
surface 5.19x the 62,500 B cap. Detection existed downstream; nothing governed
the write.
"""
import importlib.util
import os
import pathlib
import sys

import pytest

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

# guard-955: pin the backend BEFORE importing anything that resolves one, or an
# own-cloud box derives the S3 key from the customer prefix -- NOT from tmp --
# and a test write collides on the production key.
os.environ["STORAGE_BACKEND"] = "local"

_SPEC = importlib.util.spec_from_file_location(
    "goal_field_append", _SCRIPTS / "goal-field-append.py")
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)


def _blocks(n, size=4000, start=0):
    """n composed blocks in the shape compose() actually writes."""
    return "\n\n".join(f"BLOCK{i:04d}-HEAD {'x' * size} body{i}\n[appended:m{i}]"
                       for i in range(start, start + n))


class _Res:
    returncode = 0
    stdout = ""
    stderr = ""


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Rotation with its three I/O edges redirected at a tmp dir."""
    sink = tmp_path / "sink.md"
    monkeypatch.setattr(mod, "_sink_path", lambda g, f: sink)
    state = {"value": None, "written": None}

    def fake_read_goal(goal_id, source):
        return {"progress_note": state["value"]}

    def fake_run(argv, **kw):
        state["written"] = kw.get("input")
        state["value"] = kw.get("input")     # the store now holds the reduction
        return _Res()

    monkeypatch.setattr(mod, "read_goal", fake_read_goal)
    monkeypatch.setattr(mod, "_run", fake_run)
    return state, sink


# --- shape: blocks are split on the sentinel, and legacy text is never dropped

def test_split_never_drops_pre_sentinel_legacy_text():
    """Content written before the marker convention has no sentinel. A block ENDS
    at its sentinel, so such text is absorbed into the FIRST block rather than
    split off -- either way it must survive, because dropping it silently is the
    loss this whole mechanism exists to prevent."""
    v = "legacy prose with no marker\n\n" + _blocks(1)
    out = mod.split_blocks(v)
    assert len(out) == 1
    assert "legacy prose" in out[0]
    assert "".join(out).count("legacy prose") == 1


def test_split_keeps_trailing_unsentinelled_text_as_its_own_block():
    """Text AFTER the last sentinel has no marker to end it -- the tail branch
    must emit it rather than let it fall off the end of the loop."""
    out = mod.split_blocks(_blocks(2) + "\n\ndangling tail text")
    assert len(out) == 3
    assert "dangling tail text" in out[-1]


def test_split_of_empty_is_empty():
    assert mod.split_blocks("") == []


# --- the direction that matters: does NOT cut when it should not --------------

def test_under_threshold_is_untouched(wired):
    state, sink = wired
    pre = _blocks(2, size=100)
    state["value"] = pre
    assert mod.rotate_oversize("g-1-1", "progress_note", "world", pre) == pre
    assert state["written"] is None, "no write may be issued below the threshold"
    assert not sink.exists(), "no archive may be created below the threshold"


def test_single_block_is_never_split_however_large(wired):
    """One oversize block has no older sibling to cut. Cutting into it would
    truncate prose mid-sentence; leaving it whole is correct."""
    state, sink = wired
    pre = "y" * 80000 + "\n[appended:only]"
    state["value"] = pre
    assert mod.rotate_oversize("g-1-1", "progress_note", "world", pre) == pre
    assert not sink.exists()


def test_disabled_by_env_is_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "ROTATE_DISABLED", True)
    pre = _blocks(20)
    assert mod.rotate_oversize("g-1-1", "progress_note", "world", pre) == pre


# --- it cuts when it should, and NOTHING IS LOST -----------------------------

def test_rotation_archives_oldest_keeps_newest_and_loses_nothing(wired):
    state, sink = wired
    pre = _blocks(20)                      # ~80 KB, well past 32768
    state["value"] = pre
    original = mod.split_blocks(pre)
    out = mod.rotate_oversize("g-1-1", "progress_note", "world", pre)

    assert out != pre, "an oversize field must be reduced"
    assert len(out.encode()) < len(pre.encode())
    assert mod.ROTATE_NOTICE_HEAD in out, "the cut must carry its provenance (guard-6105)"
    assert str(sink) in out, "the notice must name where the removed text went"

    # NOTHING LOST: every original block is either still live or in the archive.
    archived = sink.read_text(encoding="utf-8")
    for b in original:
        head = b[:200]
        assert head in out or head in archived, "a block vanished from BOTH surfaces"

    # The NEWEST block is the one a reader needs most -- it must stay live.
    assert original[-1][:200] in out
    # ...and the OLDEST must have moved out of the live field.
    assert original[0][:200] not in out
    assert original[0][:200] in archived


def test_repeated_appends_do_not_grow_the_surface_without_bound(wired):
    """THE PIN for check 1. Simulate many cycles of a recurring goal: the live
    field must plateau instead of tracking total history."""
    state, sink = wired
    field = ""
    sizes = []
    for cycle in range(40):
        field = (field + "\n\n" if field else "") + _blocks(1, size=3000, start=cycle)
        state["value"] = field
        field = mod.rotate_oversize("g-1-1", "progress_note", "world", field)
        sizes.append(len(field.encode()))

    assert max(sizes) < 3 * mod.ROTATE_AT_BYTES, (
        f"live field is not bounded: peaked at {max(sizes)} B")
    # Total history still greatly exceeds the live field -- i.e. the bound is
    # doing real work rather than the test simply never crossing it.
    assert sink.stat().st_size > mod.ROTATE_AT_BYTES
    assert sizes[-1] < sizes[len(sizes) // 2] * 3


# --- fails CLOSED on every unmet precondition --------------------------------

def test_unverifiable_archive_cuts_nothing(tmp_path, monkeypatch):
    """archive-before-delete step 4: if the archive cannot be verified, the
    store must be left INTACT -- never cut-then-discover."""
    bad = tmp_path / "unwritable-sink.md"

    class _Sink(type(bad)):
        pass

    monkeypatch.setattr(mod, "_sink_path", lambda g, f: bad)

    def boom(*a, **k):
        raise OSError("simulated archive failure")

    monkeypatch.setattr(pathlib.Path, "open", boom)
    pre = _blocks(20)
    assert mod.rotate_oversize("g-1-1", "progress_note", "world", pre) == pre


def test_concurrent_peer_append_aborts_the_rotation(tmp_path, monkeypatch):
    """CAS. Rotating from a stale read would drop the peer's block -- the one
    outcome worse than an oversize field (guard-5234: this field has concurrent
    cross-box writers)."""
    sink = tmp_path / "sink.md"
    monkeypatch.setattr(mod, "_sink_path", lambda g, f: sink)
    pre = _blocks(20)
    # The pre-write re-read returns something OTHER than `pre`: a peer landed.
    monkeypatch.setattr(mod, "read_goal",
                        lambda g, s: {"progress_note": pre + "\n\npeer block\n[appended:peer]"})
    called = {"n": 0}

    def fake_run(argv, **kw):
        called["n"] += 1
        return _Res()

    monkeypatch.setattr(mod, "_run", fake_run)
    assert mod.rotate_oversize("g-1-1", "progress_note", "world", pre) == pre
    assert called["n"] == 0, "no write may be issued when CAS detects a peer"


def test_failed_store_write_leaves_the_caller_on_the_original(wired, monkeypatch):
    state, sink = wired
    pre = _blocks(20)
    state["value"] = pre

    class _Bad:
        returncode = 6
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(mod, "_run", lambda argv, **kw: _Bad())
    assert mod.rotate_oversize("g-1-1", "progress_note", "world", pre) == pre


def test_reduction_never_starts_with_a_json_opener(wired):
    """aspirations-update-goal.sh JSON-decodes a value beginning with '{' or '['
    and would store an object instead of text -- which is why the notice head is
    prose. Pin it so a future edit cannot reintroduce a '[ROTATED ...]' form."""
    assert mod.ROTATE_NOTICE_HEAD[:1] not in ("{", "[")
    state, sink = wired
    pre = _blocks(20)
    state["value"] = pre
    out = mod.rotate_oversize("g-1-1", "progress_note", "world", pre)
    assert out[:1] not in ("{", "[")


def test_retry_after_a_refused_cut_does_not_double_the_archive(wired, monkeypatch):
    """MEASURED REGRESSION (2026-09-21). rotate_oversize can legitimately bail
    AFTER writing the archive and BEFORE the cut -- the field-shrink guard
    refused the reduction (6% of original against a 25% floor) and fail-open
    correctly returned the field whole. The blocks were then archived AND still
    live, so the next attempt archived them again: 2 rotation headers and 84
    chunks, every one redundant. Not a data risk, but an append-only sink that
    doubles on each retry stops being readable, so the archive step is now
    idempotent on an already-archived cut."""
    state, sink = wired
    pre = _blocks(20)
    state["value"] = pre

    class _Bad:
        returncode = 1
        stdout = ""
        stderr = '{"error": "field_shrink_blocked"}'

    # First attempt: archive lands, the store write is refused, field untouched.
    monkeypatch.setattr(mod, "_run", lambda argv, **kw: _Bad())
    assert mod.rotate_oversize("g-1-1", "progress_note", "world", pre) == pre
    first = sink.stat().st_size
    assert first > 0 and sink.read_text(encoding="utf-8").count("<!-- rotated") == 1

    # Second attempt on the SAME field: must not re-archive the same blocks.
    assert mod.rotate_oversize("g-1-1", "progress_note", "world", pre) == pre
    assert sink.stat().st_size == first, "retry re-archived an already-archived cut"
    assert sink.read_text(encoding="utf-8").count("<!-- rotated") == 1


def test_shrink_override_is_passed_or_the_bound_can_never_apply(wired):
    """The field-shrink guard blocks any narrative write below 25% of the
    original; a rotation is ~6%. Without the override the bound is refused at
    rc=1 on exactly the records that most need it -- measured live on
    g-115-817 before the flag was added. Pin that the flag is sent."""
    state, sink = wired
    pre = _blocks(20)
    state["value"] = pre
    seen = {}

    def capture(argv, **kw):
        seen["argv"] = [str(a) for a in argv]
        state["value"] = kw.get("input")
        return _Res()

    mod._run = capture
    mod.rotate_oversize("g-1-1", "progress_note", "world", pre)
    assert "--override-shrink" in seen["argv"]
    assert "--override-narrative-replace" in seen["argv"]
