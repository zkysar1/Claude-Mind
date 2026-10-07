"""Tests for the soft forget and the undo of a hypothesis (, unit u2).

The hypothesis leg of ``knowledge-edit-apply.py``. A pipeline record is never deleted and its
lifecycle only moves forward, so a forget leaves the record where it is, stamps ``forgotten_at``
(the one field the exposure predicate reads), and blanks the statement the member was shown.
The retained statement lives in a caller-supplied directory outside the world.

The store is not faked. ``_Pipeline`` stands in for the daemon transport only: every write
goes through the REAL ``pipeline_write.update_field`` handler, in process, against the world's
own pipeline file, so the value parser, the record validator and the write machinery are the
ones production runs. A fake that only recorded calls could not have told a marker the store
would reject, or a statement it would coerce, from one it keeps.

What these tests pin is the contract the member relies on: a forgotten hypothesis is gone from
the export and from every handle resolver, its statement is held in exactly one place outside
the world (the retention record), an undo puts it back and clears the marker LAST, and every
partial failure leaves a state that is safe to be in.
"""

from __future__ import annotations

import datetime
import gzip
import importlib.util
import json
import sys
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

_SCRIPTS = Path(__file__).resolve().parents[1]
_ROOT = _SCRIPTS.parents[1]
for _p in (str(_SCRIPTS), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _rt  # noqa: E402
import knowledge_retention  # noqa: E402
from knowledge_projection import FORGOTTEN_FIELD, RESTORED_FIELD, is_forgotten, item_handle  # noqa: E402
from mind_api.src.world import pipeline_write  # noqa: E402


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


apply_mod = _load("knowledge_edit_apply_forget_hyp_tests", "knowledge-edit-apply.py")
export_mod = _load("knowledge_export_forget_hyp_tests", "knowledge-export.py")
drain_mod = _load("inbound_drain_forget_hyp_tests", "inbound_drain.py")

SECRET = "handle-secret-for-tests"
ENV_ID = "env-under-test"
TODAY = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
TOMBSTONE = f"Forgotten by the member on {TODAY}."

CLAIM = "Widgets sell better in green than in blue."
TITLE = "Green widgets outsell blue ones"
RATIONALE = "Observed across three markets in 2025."
POSITION = "YES green widgets outsell blue in every market tested"

A = "2026-01-02_green-widgets"
B = "2026-01-03_gear-demand"
C = "2026-01-04_resolved-widgets"


def _hyp(item_id: str, **extra) -> dict:
    rec = {"id": item_id, "title": TITLE, "stage": "active", "horizon": "session",
           "type": "calibration", "confidence": 0.6, "position": POSITION,
           "formed_date": "2026-01-02", "category": "acme", "claim": CLAIM,
           "rationale": RATIONALE}
    rec.update(extra)
    return rec


def _records() -> list[dict]:
    return [
        _hyp(A),
        # The control: a second exposed hypothesis nothing in these tests touches.
        _hyp(B, title="Gear demand is flat", claim="Demand for gears did not move in 2025."),
        _hyp(C, stage="resolved", outcome="CONFIRMED", resolved_date="2026-02-01",
             title="Widgets resolved title", claim="Resolved widgets claim, long enough to keep."),
    ]


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = tmp_path / "world"
    tree = w / "knowledge" / "tree"
    (tree / "acme").mkdir(parents=True)
    (tree / "acme" / "acme-widgets.md").write_text(
        "---\ntopic: Acme widgets\n---\n\nWidgets are blue.\n", encoding="utf-8")
    index = {"last_updated": "2026-01-01", "nodes": {
        "acme-widgets": {"file": "world/knowledge/tree/acme/acme-widgets.md",
                         "summary": "Widgets.", "last_updated": "2026-01-01"}}}
    (tree / "_tree.yaml").write_text(yaml.safe_dump(index, sort_keys=False), encoding="utf-8")
    (w / "pipeline.jsonl").write_text("".join(json.dumps(r) + "\n" for r in _records()),
                                      encoding="utf-8")
    (w / "guardrails.jsonl").write_text(
        json.dumps({"id": "guard-1", "category": "acme", "rule": "r", "status": "active"}) + "\n",
        encoding="utf-8")
    monkeypatch.setenv("WORLD_PATH", str(w))
    monkeypatch.setenv(export_mod._GOAL_HANDLE_SECRET_VAR, SECRET)
    monkeypatch.setenv("ENVIRONMENT_ID", ENV_ID)
    monkeypatch.setenv("MIND_SID", "sid-under-test")
    return w


@pytest.fixture
def retention(tmp_path):
    """Outside the world, as the caller's directory is. Created by the first forget."""
    return tmp_path / "spool" / ENV_ID / "retention"


class _Pipeline:
    """Stands in for the daemon transport of POST /v1/pipeline/update-field.

    Every call that is let through runs the real handler against the world's pipeline file.
    ``fail`` names fields whose write raises, as a daemon 500 does, BEFORE anything is
    written. ``lie`` names fields the daemon acknowledges with a 200 and does not write.
    ``on_call`` sees each call's 1-based index and params before it is served, so a test can
    look at the disk at the moment a given write is about to happen.
    """

    def __init__(self, world, fail=(), lie=(), on_call=None):
        self.world = world
        self.fail = set(fail)
        self.lie = set(lie)
        self.on_call = on_call
        self.calls = []

    def __call__(self, method, path, query=None, body=None, headers=None):
        assert (method, path) == ("POST", "/v1/pipeline/update-field")
        params = {k: v[0] for k, v in urllib.parse.parse_qs(query, keep_blank_values=True).items()}
        self.calls.append((params["id"], params["field"], params["value"]))
        if self.on_call is not None:
            self.on_call(len(self.calls), params)
        if params["field"] in self.fail:
            raise _rt.RtError("daemon HTTP 500")
        if params["field"] in self.lie:
            return json.dumps({"ok": True, "record": _stored(self.world, params["id"])})
        ctx = SimpleNamespace(query=params, paths=SimpleNamespace(world=self.world), headers={})
        resp = pipeline_write.update_field(ctx)
        if resp.status >= 400:
            raise _rt.RtError(f"daemon HTTP {resp.status}: {resp.body.decode('utf-8')}")
        return resp.body.decode("utf-8")

    def fields(self) -> list[str]:
        return [field for _id, field, _value in self.calls]


@pytest.fixture
def pipeline(world, monkeypatch):
    daemon = _Pipeline(world)
    monkeypatch.setattr(_rt, "rt_call", daemon)
    return daemon


def _store_path(world: Path) -> Path:
    return world / "pipeline.jsonl"


def _stored_all(world: Path) -> list[dict]:
    return [json.loads(ln) for ln in _store_path(world).read_text(encoding="utf-8").splitlines() if ln]


def _stored(world: Path, item_id: str) -> dict:
    return next(r for r in _stored_all(world) if r["id"] == item_id)


def _rewrite(world: Path, item_id: str, **changes) -> None:
    """Change a stored record directly, as a write from another box would."""
    rows = _stored_all(world)
    for row in rows:
        if row["id"] == item_id:
            row.update(changes)
    _store_path(world).write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _handle(item_id: str, kind: str = "hypothesis") -> str:
    return item_handle(kind, item_id, SECRET, ENV_ID)


def _forget(retention: Path, item_id: str, *, apply: bool = True, report: dict | None = None) -> int:
    argv = ["--handle", _handle(item_id), "--op", "forget", f"--retention-dir={retention}"]
    return apply_mod.main(argv + (["--apply"] if apply else []), report)


def _undo(retention: Path, item_id: str, *, report: dict | None = None) -> int:
    return apply_mod.main(["--handle", _handle(item_id), "--op", "undo",
                           f"--retention-dir={retention}", "--apply"], report)


def _retained(retention: Path, item_id: str = A) -> dict | None:
    return knowledge_retention.read_record(knowledge_retention.record_path(retention, "hypothesis", item_id))


def _retained_statement(record: dict) -> dict:
    return json.loads(knowledge_retention.retained_content(record))


def _published_handles(world: Path) -> set:
    bundle = export_mod.build_bundle(world, _ROOT, extra_paths=(), env=None)
    return {row.get("handle") for row in bundle.hypotheses}


def _bytes(*roots: Path) -> dict:
    return {p: p.read_bytes() for r in roots if r.exists() for p in r.rglob("*") if p.is_file()}


def _plain(data: bytes) -> bytes:
    """What a file under the history store says once its gzip layer is off."""
    try:
        return gzip.decompress(data)
    except (OSError, EOFError):
        return data


# --- forget -------------------------------------------------------------------


def test_forget_hides_the_hypothesis_keeps_one_retained_copy_and_blanks_the_statement(
        world, retention, pipeline, capsys):
    before = _stored_all(world)
    assert {_handle(A), _handle(B)} <= _published_handles(world), "both start exposed"
    report = {}

    rc = _forget(retention, A, report=report)

    assert rc == 0, capsys.readouterr().err
    kept = _retained(retention)
    assert report == {"kind": "hypothesis", "id": A, "op": "forget", "applied": True,
                      "forgotten": True, "undo_until": kept["undo_until"], "blanked": True}
    # Hidden from what the member and the resident read; the control is not.
    published = _published_handles(world)
    assert _handle(A) not in published
    assert _handle(B) in published, "positive control: an unforgotten hypothesis is still shown"
    assert export_mod.resolve_item(world, _handle(A)) is None
    assert export_mod.resolve_item(world, _handle(B))[1] == B
    # The record stays: same id, same stage, marker equal to the retained stamp, statement blanked.
    now = _stored(world, A)
    assert [r["id"] for r in _stored_all(world)] == [r["id"] for r in before]
    assert (now["stage"], now[FORGOTTEN_FIELD]) == ("active", kept["forgotten_at"])
    assert RESTORED_FIELD not in now, "a forget stamps no undo event"
    assert (now["claim"], now["title"]) == (TOMBSTONE, TOMBSTONE)
    # The supporting free text is NOT blanked: it is residue the erase sweep owns, not part of
    # what the member saw as the statement.
    assert (now["rationale"], now["position"]) == (RATIONALE, POSITION)
    assert _stored_all(world)[1:] == before[1:], "no other record was touched"
    # One retained copy, outside the world, holding exactly the statement.
    assert [p.name for p in retention.iterdir()] == [knowledge_retention.record_name("hypothesis", A)]
    assert _retained_statement(kept) == {"claim": CLAIM, "title": TITLE}
    assert knowledge_retention.in_window(kept, knowledge_retention.now_utc())


def test_the_statement_is_held_outside_the_world_apart_from_the_pipeline_history(
        world, retention, pipeline):
    """The residue is named, not hidden: the pipeline writer snapshots the store before each
    write, so the pre-forget statement survives under the world's history directory. The erase
    sweep and the privacy text have to account for it (g-335-1726 u4, u6)."""
    assert _forget(retention, A) == 0

    files = _bytes(world)
    holders = {p for p, data in files.items() if CLAIM.encode("utf-8") in _plain(data)}
    listing = sorted(str(p.relative_to(world)) for p in files)
    assert holders, f"the history snapshot of the pre-forget store is real: {listing}"
    assert {p.relative_to(world).parts[0] for p in holders} == {".history"}, listing
    assert CLAIM not in _store_path(world).read_text(encoding="utf-8"), "the live store holds none"
    assert [p.name for p in retention.iterdir()] == [knowledge_retention.record_name("hypothesis", A)]


def test_forgetting_a_resolved_hypothesis_leaves_its_stage_and_outcome_alone(world, retention, pipeline):
    """The lifecycle only moves forward and an archive copy is append-only: neither can carry a
    forget. The stage is not the marker."""
    report = {}

    assert _forget(retention, C, report=report) == 0

    row = _stored(world, C)
    assert (row["stage"], row["outcome"]) == ("resolved", "CONFIRMED")
    assert is_forgotten(row)
    assert row["claim"] == row["title"] == TOMBSTONE
    assert _handle(C) not in _published_handles(world)
    assert report["blanked"] is True


def test_a_forgotten_hypothesis_cannot_be_forgotten_or_edited_again(world, retention, pipeline):
    assert _forget(retention, A) == 0
    stores = _bytes(world, retention)

    for op, extra in (("forget", []), ("edit", ["--text", "A replacement statement long enough."])):
        report = {}
        rc = apply_mod.main(["--handle", _handle(A), "--op", op, f"--retention-dir={retention}",
                             "--apply", *extra], report)
        assert (rc, report) == (3, {"refused": "not_addressable"}), op
    assert _bytes(world, retention) == stores


def test_a_dry_run_forget_writes_nothing(world, retention, pipeline, capsys):
    stores = _bytes(world)

    rc = _forget(retention, A, apply=False)

    assert rc == 0
    assert "dry run" in capsys.readouterr().out
    assert _bytes(world) == stores
    assert not retention.exists()
    assert pipeline.calls == []


def test_forget_without_a_retention_directory_is_refused_before_any_write(world, pipeline):
    stores = _bytes(world)
    report = {}

    rc = apply_mod.main(["--handle", _handle(A), "--op", "forget", "--apply"], report)

    assert (rc, report) == (3, {"refused": "no_retention_store"})
    assert _bytes(world) == stores
    assert pipeline.calls == []


def test_every_kind_that_can_be_forgotten_can_be_undone():
    assert set(apply_mod._FORGETTERS) == set(apply_mod._UNDOERS) == {"node", "hypothesis", "guardrail"}


# ids= keeps the node id short: pytest copies it into PYTEST_CURRENT_TEST, and Windows
# caps an environment value at 32,767 characters (a default id embeds the whole value).
@pytest.mark.parametrize("field, text, reason", [
    ("claim", "42", "store_would_coerce"),
    ("claim", "true", "store_would_coerce"),
    ("claim", '{"a": 1}', "store_would_coerce"),
    ("title", "1e5", "store_would_coerce"),
    ("claim", "x" * 70_000, "too_long_for_store"),
], ids=["claim-integer", "claim-bool", "claim-object", "title-float", "claim-70000-chars"])
def test_a_statement_the_store_would_alter_is_refused_before_anything_is_written(
        world, retention, pipeline, field, text, reason):
    """A forget that cannot be undone is not the soft forget that was ruled. The undo writes the
    retained text back through the same parser, so the forget refuses what it could not restore."""
    _rewrite(world, A, **{field: text})
    stores = _bytes(world)
    report = {}

    rc = _forget(retention, A, report=report)

    assert (rc, report) == (3, {"refused": reason})
    assert _bytes(world) == stores
    assert not retention.exists()
    assert pipeline.calls == []


# --- forget: the order is the safety ------------------------------------------


def test_the_statement_is_retained_and_read_back_before_the_marker_is_written(world, retention, monkeypatch):
    """guard-6223: a recovery layer that was not verified is not a recovery layer. The disk is
    looked at when the FIRST write is about to happen."""
    seen = {}

    def at_first_write(index, params):
        if index == 1:
            seen["field"] = params["field"]
            seen["retained"] = _retained(retention)
            seen["stored"] = _stored(world, A)

    monkeypatch.setattr(_rt, "rt_call", _Pipeline(world, on_call=at_first_write))

    assert _forget(retention, A) == 0

    assert seen["field"] == FORGOTTEN_FIELD
    assert _retained_statement(seen["retained"]) == {"claim": CLAIM, "title": TITLE}
    assert (seen["stored"]["claim"], seen["stored"]["title"]) == (CLAIM, TITLE), "nothing blanked yet"
    assert not is_forgotten(seen["stored"]), "nothing hidden yet"


def test_the_marker_is_written_before_the_statement_is_blanked(world, retention, pipeline):
    assert _forget(retention, A) == 0

    assert pipeline.fields() == [FORGOTTEN_FIELD, "claim", "title"]


@pytest.mark.parametrize("failure", [OSError("disk full"), ValueError("did not read back as written")])
def test_a_retained_copy_that_is_not_there_stops_the_forget_before_it_changes_anything(
        world, retention, pipeline, monkeypatch, capsys, failure):
    stores = _bytes(world)

    def boom(*_a, **_k):
        raise failure

    monkeypatch.setattr(knowledge_retention, "write_record", boom)

    assert _forget(retention, A) == 1
    assert "its text was not retained" in capsys.readouterr().err
    assert _bytes(world) == stores
    assert pipeline.calls == []


def test_a_marker_that_does_not_land_leaves_the_hypothesis_shown_and_the_retained_copy_inert(
        world, retention, monkeypatch, capsys):
    daemon = _Pipeline(world, fail={FORGOTTEN_FIELD})
    monkeypatch.setattr(_rt, "rt_call", daemon)
    stores = _bytes(world)

    assert _forget(retention, A) == 1

    assert "write failed for hypothesis" in capsys.readouterr().err
    assert _handle(A) in _published_handles(world), "still shown: the forget did not happen"
    assert _bytes(world) == stores, "the store is untouched"
    assert daemon.fields() == [FORGOTTEN_FIELD], "nothing was blanked behind a marker that failed"
    kept = _retained(retention)
    assert _retained_statement(kept) == {"claim": CLAIM, "title": TITLE}
    # Inert: an undo of it is refused, and a retry of the forget completes.
    report = {}
    assert _undo(retention, A, report=report) == 3
    assert report == {"refused": "undo_conflict"}
    daemon.fail.clear()
    assert _forget(retention, A) == 0
    assert _handle(A) not in _published_handles(world)


def test_a_daemon_that_acknowledges_a_marker_it_did_not_write_is_not_believed(
        world, retention, monkeypatch, capsys):
    """A 2xx is not evidence the value landed. The record the endpoint returns is read back."""
    daemon = _Pipeline(world, lie={FORGOTTEN_FIELD})
    monkeypatch.setattr(_rt, "rt_call", daemon)
    stores = _bytes(world)

    assert _forget(retention, A) == 1

    assert "did not read back as written" in capsys.readouterr().err
    assert _bytes(world) == stores
    assert daemon.fields() == [FORGOTTEN_FIELD]


def test_a_blank_that_fails_leaves_the_forget_standing_and_the_other_field_still_blanked(
        world, retention, monkeypatch, capsys):
    """Blanking is cleanup: the record is already hidden and the statement already retained.
    Every field is tried, so one failure does not leave the other holding its text."""
    daemon = _Pipeline(world, fail={"claim"})
    monkeypatch.setattr(_rt, "rt_call", daemon)
    report = {}

    rc = _forget(retention, A, report=report)

    assert rc == 0
    assert (report["forgotten"], report["blanked"]) == (True, False)
    assert "not all blanked" in capsys.readouterr().err
    assert _handle(A) not in _published_handles(world)
    row = _stored(world, A)
    assert (row["claim"], row["title"]) == (CLAIM, TOMBSTONE), "title was still tried and blanked"
    assert _retained_statement(_retained(retention)) == {"claim": CLAIM, "title": TITLE}
    assert daemon.fields() == [FORGOTTEN_FIELD, "claim", "title"]


def test_a_statement_that_changed_while_it_was_retained_is_not_forgotten(
        world, retention, pipeline, monkeypatch, capsys):
    """guard-3881: the decision was made on a snapshot. A retained copy of text that has since
    changed is not a copy of what the member forgot, and the newer text is not overwritten."""
    real = knowledge_retention.write_record
    newer = "Somebody rewrote this claim after the member saw it."

    def write_then_change(retention_dir, record):
        path = real(retention_dir, record)
        _rewrite(world, A, claim=newer)
        return path

    monkeypatch.setattr(knowledge_retention, "write_record", write_then_change)

    assert _forget(retention, A) == 1

    assert "its text changed while it was retained" in capsys.readouterr().err
    row = _stored(world, A)
    assert (row["claim"], is_forgotten(row)) == (newer, False)
    assert pipeline.calls == []


# --- the stamps order a forget against an undo ---------------------------------

T0 = datetime.datetime(2026, 10, 3, 12, 0, 0, tzinfo=datetime.timezone.utc)
ONE_SECOND = datetime.timedelta(seconds=1)
AHEAD = "2999-01-01T00:00:00+00:00"


def _clock(monkeypatch, moment=T0) -> dict:
    """Pin the retention clock. A test moves it by assigning to ``now`` in the returned dict."""
    clock = {"now": moment}
    monkeypatch.setattr(knowledge_retention, "now_utc", lambda: clock["now"])
    return clock


def _at(stamp: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(stamp)


def test_an_undo_in_the_same_second_as_the_forget_is_stamped_after_it(world, retention, pipeline, monkeypatch):
    """Two events in one second would tie in the store merge, and a tie hides: the undo would
    lose to a stale copy of the forget it reversed."""
    _clock(monkeypatch)
    assert _forget(retention, A) == 0
    assert _at(_stored(world, A)[FORGOTTEN_FIELD]) == T0

    assert _undo(retention, A) == 0

    assert _at(_stored(world, A)[RESTORED_FIELD]) == T0 + ONE_SECOND


def test_a_forget_after_an_undo_is_stamped_after_the_undo_when_the_clock_has_not_moved(
        world, retention, pipeline, monkeypatch):
    _clock(monkeypatch)
    assert _forget(retention, A) == 0
    assert _undo(retention, A) == 0

    assert _forget(retention, A) == 0

    row = _stored(world, A)
    assert (_at(row[RESTORED_FIELD]), _at(row[FORGOTTEN_FIELD])) == (T0 + ONE_SECOND, T0 + 2 * ONE_SECOND)
    assert _retained(retention)["forgotten_at"] == row[FORGOTTEN_FIELD], "the marker is the retained stamp"


def test_a_clock_that_runs_behind_the_record_cannot_stamp_an_event_before_it(
        world, retention, pipeline, monkeypatch):
    """A box whose clock is behind the one that forgot still undoes AFTER the forget."""
    clock = _clock(monkeypatch)
    assert _forget(retention, A) == 0
    clock["now"] = T0 - datetime.timedelta(hours=1)

    assert _undo(retention, A) == 0

    assert _at(_stored(world, A)[RESTORED_FIELD]) == T0 + ONE_SECOND


def test_a_clock_well_ahead_of_every_stamp_is_used_as_it_is(world, retention, pipeline, monkeypatch):
    clock = _clock(monkeypatch)
    assert _forget(retention, A) == 0
    clock["now"] = T0 + datetime.timedelta(hours=1)

    assert _undo(retention, A) == 0

    assert _at(_stored(world, A)[RESTORED_FIELD]) == clock["now"]


def test_a_forget_is_stamped_after_any_stamp_the_record_already_carries(world, retention, pipeline, monkeypatch):
    _clock(monkeypatch)
    _rewrite(world, A, **{RESTORED_FIELD: AHEAD})

    assert _forget(retention, A) == 0

    assert _at(_stored(world, A)[FORGOTTEN_FIELD]) == _at(AHEAD) + ONE_SECOND
    assert _retained(retention)["forgotten_at"] == _stored(world, A)[FORGOTTEN_FIELD]


# --- forget: ghosts -----------------------------------------------------------


def test_a_record_holding_blanked_text_with_no_marker_is_hidden_again_and_the_retained_copy_kept(
        world, retention, pipeline):
    """A merge from a stale copy can leave a record with the blanked text and without the
    marker. It holds nothing to retain, and a retained record written from it would replace the
    only copy of the real text."""
    assert _forget(retention, A) == 0
    kept = _retained(retention)
    kept_bytes = (retention / knowledge_retention.record_name("hypothesis", A)).read_bytes()
    _rewrite(world, A, **{FORGOTTEN_FIELD: None})
    assert _handle(A) in _published_handles(world), "the ghost is shown again"
    report = {}

    rc = _forget(retention, A, report=report)

    assert rc == 0
    assert (retention / knowledge_retention.record_name("hypothesis", A)).read_bytes() == kept_bytes
    assert report["undo_until"] == kept["undo_until"]
    assert _handle(A) not in _published_handles(world)
    assert _retained_statement(_retained(retention)) == {"claim": CLAIM, "title": TITLE}


def test_a_ghost_hidden_again_is_stamped_after_the_stamps_it_carries(world, retention, pipeline, monkeypatch):
    _clock(monkeypatch)
    assert _forget(retention, A) == 0
    _rewrite(world, A, **{FORGOTTEN_FIELD: None, RESTORED_FIELD: AHEAD})

    assert _forget(retention, A) == 0

    assert _at(_stored(world, A)[FORGOTTEN_FIELD]) == _at(AHEAD) + ONE_SECOND


def test_a_statement_that_merely_starts_like_the_tombstone_is_retained_like_any_other(
        world, retention, pipeline):
    """Without a live retained record the same words are the member's own statement. Treating
    them as a ghost would blank them with no copy kept."""
    own = "Forgotten by the member on a Tuesday: a note about widgets."
    _rewrite(world, A, claim=own)

    assert _forget(retention, A) == 0

    assert _retained_statement(_retained(retention)) == {"claim": own, "title": TITLE}
    assert _stored(world, A)["claim"] == TOMBSTONE


# --- undo ---------------------------------------------------------------------


def test_undo_restores_the_statement_clears_the_marker_and_shows_the_hypothesis_again(
        world, retention, pipeline, capsys):
    before = _stored(world, A)
    assert _forget(retention, A) == 0
    report = {}

    rc = _undo(retention, A, report=report)

    assert rc == 0, capsys.readouterr().err
    assert report == {"kind": "hypothesis", "id": A, "op": "undo", "applied": True, "restored": True}
    row = _stored(world, A)
    assert (row["claim"], row["title"]) == (CLAIM, TITLE)
    assert not is_forgotten(row)
    assert row["stage"] == before["stage"] and row["rationale"] == RATIONALE
    assert _handle(A) in _published_handles(world), "shown again, under the handle it had"
    assert export_mod.resolve_item(world, _handle(A))[1] == A
    # The retained copy is gone: the statement is held in exactly one place again.
    kept = _retained(retention)
    assert kept is not None and not knowledge_retention.is_live(kept)
    assert CLAIM.encode("utf-8") not in (retention / knowledge_retention.record_name("hypothesis", A)).read_bytes()
    # The undo is an event of its own, stamped AFTER the forget's: the store merge ranks the two by
    # these stamps (test_pipeline_forget_merge.py).
    assert row[RESTORED_FIELD] > kept["forgotten_at"]


def test_the_marker_is_cleared_last_so_the_hypothesis_is_never_shown_half_restored(
        world, retention, monkeypatch):
    seen = []

    def watch(index, params):
        row = _stored(world, A)
        seen.append((params["field"], is_forgotten(row)))

    daemon = _Pipeline(world)
    monkeypatch.setattr(_rt, "rt_call", daemon)
    assert _forget(retention, A) == 0
    daemon.calls.clear()
    daemon.on_call = watch

    assert _undo(retention, A) == 0

    assert daemon.fields() == [RESTORED_FIELD, "claim", "title", FORGOTTEN_FIELD]
    # Every write that puts text back happens while the record is still hidden.
    assert seen == [(RESTORED_FIELD, True), ("claim", True), ("title", True), (FORGOTTEN_FIELD, True)]


def test_a_marker_that_will_not_clear_leaves_the_text_restored_and_the_record_hidden_and_a_retry_works(
        world, retention, monkeypatch, capsys):
    daemon = _Pipeline(world)
    monkeypatch.setattr(_rt, "rt_call", daemon)
    assert _forget(retention, A) == 0
    daemon.fail = {FORGOTTEN_FIELD}

    assert _undo(retention, A) == 1

    assert "write failed for hypothesis" in capsys.readouterr().err
    row = _stored(world, A)
    assert (row["claim"], row["title"], is_forgotten(row)) == (CLAIM, TITLE, True)
    assert _handle(A) not in _published_handles(world), "still hidden, with its text back"
    assert knowledge_retention.is_live(_retained(retention)), "the retained copy is kept for the retry"
    daemon.fail.clear()
    assert _undo(retention, A) == 0
    assert _handle(A) in _published_handles(world)


def test_a_text_write_that_fails_part_way_leaves_the_record_hidden_and_a_retry_completes(
        world, retention, monkeypatch):
    daemon = _Pipeline(world)
    monkeypatch.setattr(_rt, "rt_call", daemon)
    assert _forget(retention, A) == 0
    daemon.fail = {"title"}

    assert _undo(retention, A) == 1

    row = _stored(world, A)
    assert (row["claim"], row["title"], is_forgotten(row)) == (CLAIM, TOMBSTONE, True)
    daemon.fail.clear()
    assert _undo(retention, A) == 0
    row = _stored(world, A)
    assert (row["claim"], row["title"], is_forgotten(row)) == (CLAIM, TITLE, False)


def test_an_undo_that_cannot_read_the_record_it_stamps_is_not_applied(
        world, retention, pipeline, monkeypatch, capsys):
    """The stamp has to come after the stamps the record carries, so an unreadable record is not
    stamped blind. The refusal check reads the record first; the undo reads it again to stamp."""
    assert _forget(retention, A) == 0
    real = apply_mod._stored_hypothesis
    reads = []

    def unreadable_the_second_time(export, world_path, item_id):
        reads.append(item_id)
        return real(export, world_path, item_id) if len(reads) == 1 else None

    monkeypatch.setattr(apply_mod, "_stored_hypothesis", unreadable_the_second_time)
    pipeline.calls.clear()

    assert _undo(retention, A) == 1

    assert reads == [A, A]
    assert "its record could not be read" in capsys.readouterr().err
    assert pipeline.calls == []
    assert knowledge_retention.is_live(_retained(retention)), "the retained copy is kept for the retry"


def test_a_retry_of_an_undo_never_stamps_before_its_first_attempt(world, retention, monkeypatch):
    """A clock stepped back between the attempts must not take the stamp back with it: a copy
    caught after the first attempt would then outrank the finished undo."""
    clock = _clock(monkeypatch)
    daemon = _Pipeline(world)
    monkeypatch.setattr(_rt, "rt_call", daemon)
    assert _forget(retention, A) == 0
    daemon.fail = {"title"}
    assert _undo(retention, A) == 1
    first = _stored(world, A)[RESTORED_FIELD]
    daemon.fail.clear()
    clock["now"] = T0 - datetime.timedelta(hours=2)

    assert _undo(retention, A) == 0

    assert _at(_stored(world, A)[RESTORED_FIELD]) > _at(first)


@pytest.mark.parametrize("how", ["fail", "lie"])
def test_a_stamp_that_does_not_land_stops_the_undo_before_any_text_goes_back(
        world, retention, monkeypatch, capsys, how):
    daemon = _Pipeline(world)
    monkeypatch.setattr(_rt, "rt_call", daemon)
    assert _forget(retention, A) == 0
    daemon.calls.clear()
    (daemon.fail if how == "fail" else daemon.lie).add(RESTORED_FIELD)

    assert _undo(retention, A) == 1

    assert capsys.readouterr().err.count("hypothesis") >= 1
    assert daemon.fields() == [RESTORED_FIELD], "nothing is written once the stamp is refused"
    row = _stored(world, A)
    assert (row["claim"], row["title"], is_forgotten(row)) == (TOMBSTONE, TOMBSTONE, True)
    assert knowledge_retention.is_live(_retained(retention)), "the retained copy is kept for the retry"
    daemon.fail.clear()
    daemon.lie.clear()
    assert _undo(retention, A) == 0
    assert (_stored(world, A)["claim"], is_forgotten(_stored(world, A))) == (CLAIM, False)


def test_an_undo_after_the_window_is_refused_and_writes_nothing(world, retention, pipeline):
    assert _forget(retention, A) == 0
    path = retention / knowledge_retention.record_name("hypothesis", A)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["undo_until"] = "2020-01-01T00:00:00+00:00"
    path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    stores = _bytes(world, retention)
    pipeline.calls.clear()
    report = {}

    rc = _undo(retention, A, report=report)

    assert (rc, report) == (3, {"refused": "undo_expired"})
    assert _bytes(world, retention) == stores
    assert pipeline.calls == []


def test_an_undo_of_a_statement_written_since_the_forget_is_refused(world, retention, pipeline):
    """A restore would overwrite text the member never saw."""
    assert _forget(retention, A) == 0
    newer = "A resident wrote this after the forget, and it is not the tombstone."
    _rewrite(world, A, claim=newer)
    stores = _bytes(world, retention)
    pipeline.calls.clear()
    report = {}

    rc = _undo(retention, A, report=report)

    assert (rc, report) == (3, {"refused": "undo_conflict"})
    assert _bytes(world, retention) == stores
    assert pipeline.calls == []


def test_an_undo_of_a_hypothesis_that_is_not_marked_forgotten_is_refused(world, retention, pipeline):
    assert _forget(retention, A) == 0
    _rewrite(world, A, **{FORGOTTEN_FIELD: None})
    pipeline.calls.clear()
    report = {}

    rc = _undo(retention, A, report=report)

    assert (rc, report) == (3, {"refused": "undo_conflict"})
    assert pipeline.calls == []


def test_an_undo_for_a_hypothesis_nobody_forgot_resolves_nothing(world, retention, pipeline):
    report = {}

    rc = _undo(retention, B, report=report)

    assert (rc, report) == (3, {"refused": "not_addressable"})
    assert pipeline.calls == []


def test_an_undo_whose_live_record_is_gone_resolves_nothing(world, retention, pipeline):
    assert _forget(retention, A) == 0
    rows = [r for r in _stored_all(world) if r["id"] != A]
    _store_path(world).write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    pipeline.calls.clear()
    report = {}

    rc = _undo(retention, A, report=report)

    assert (rc, report) == (3, {"refused": "not_addressable"})
    assert pipeline.calls == []


def test_an_undo_in_an_unprovisioned_environment_resolves_nothing(world, retention, pipeline, monkeypatch):
    assert _forget(retention, A) == 0
    monkeypatch.delenv(export_mod._GOAL_HANDLE_SECRET_VAR)
    report = {}

    rc = apply_mod.main(["--handle", _handle(A), "--op", "undo", f"--retention-dir={retention}",
                         "--apply"], report)

    assert (rc, report) == (3, {"refused": "not_addressable"})


@pytest.mark.parametrize("content, reason", [
    # A record that names a field the member was never shown would turn the undo into an
    # arbitrary write against the store. Its digest is valid: the digest covers the bytes only.
    ({"claim": "Restored text.", "stage": "archived"}, "not_addressable"),
    ({"claim": "Restored text.", FORGOTTEN_FIELD: "x"}, "not_addressable"),
    ({"claim": "Restored text.", RESTORED_FIELD: "x"}, "not_addressable"),
    ({"claim": ["not", "a", "string"]}, "not_addressable"),
    ({"claim": ""}, "not_addressable"),
    ([1, 2, 3], "not_addressable"),
    # A statement the store would coerce on the way back in is refused, not restored as a number.
    ({"claim": "42"}, "store_would_coerce"),
])
def test_a_retained_record_that_does_not_hold_a_statement_is_refused_and_writes_nothing(
        world, retention, pipeline, content, reason):
    assert _forget(retention, A) == 0
    forged = knowledge_retention.build_record(
        "hypothesis", A, json.dumps(content).encode("utf-8"), now=knowledge_retention.now_utc(), restore={})
    knowledge_retention.write_record(retention, forged)
    stores = _bytes(world)
    pipeline.calls.clear()
    report = {}

    rc = _undo(retention, A, report=report)

    assert (rc, report) == (3, {"refused": reason})
    assert _bytes(world) == stores
    assert pipeline.calls == []


def test_a_forget_after_an_undo_retains_afresh_and_restarts_the_window(world, retention, pipeline):
    assert _forget(retention, A) == 0
    first = _retained(retention)
    assert _undo(retention, A) == 0
    assert not knowledge_retention.is_live(_retained(retention))

    assert _forget(retention, A) == 0

    again = _retained(retention)
    assert knowledge_retention.is_live(again)
    assert _retained_statement(again) == {"claim": CLAIM, "title": TITLE}
    assert again["undo_until"] >= first["undo_until"]
    assert _handle(A) not in _published_handles(world)


# --- the marker and the store -------------------------------------------------


def test_the_marker_field_is_one_the_pipeline_store_expects(world, retention, pipeline, capsys):
    """An unknown key is not refused, it is warned about on every write. The marker and the undo
    stamp are known fields, so a forget and an undo are silent."""
    assert _forget(retention, A) == 0
    assert _undo(retention, A) == 0

    assert "[pipeline-unknown-field]" not in capsys.readouterr().err


# --- one item's operations are serialized by the drain ------------------------


def _queue(env: Path, name: str, op: str, item_id: str) -> None:
    """A member's knowledge record, as the writer queues it."""
    record = {"kind": "knowledge", "environmentKey": ENV_ID, "accountId": "acct-1",
              "queued_at": "2026-10-03T08:00:00+00:00", "source": "PutAyoEnvironmentDirectives",
              "handle": _handle(item_id), "op": op}
    (env / "inbound").mkdir(parents=True, exist_ok=True)
    (env / "inbound" / name).write_text(json.dumps(record), encoding="utf-8")


def test_an_undo_queued_behind_a_forget_waits_for_it_instead_of_running_inside_it(
        world, retention, monkeypatch, tmp_path):
    """The interleaving nothing in the applier prevents: a member's undo that starts while their
    forget sits between its marker and its blank restores the text, and the forget's late blank
    then writes the tombstone over it with no marker, so the hypothesis is shown blank for good.

    The drain closes it with one lock per environment. A second drain started at that moment
    (here from inside the forget's first blank, which is exactly when a concurrent run would
    arrive) finds the environment held and claims nothing; the undo is applied by the first
    drain after the forget has finished. Direct callers of the applier are not serialized, which
    is why the lock is the drain's, and the drain is the applier's only caller in production.

    The destination fence is the REAL one: the spool root is the directory that holds the world.
    """
    monkeypatch.setattr(drain_mod, "_own_world_root", lambda: world)
    env = tmp_path / "spool" / ENV_ID
    _queue(env, "20261003T080000000000-a.json", "forget", A)
    _queue(env, "20261003T080000000001-b.json", "undo", A)

    def drain():
        return drain_mod.drain_environment(env, apply=True, source="world", asp_id="asp-1",
                                           max_records=0, tmp_age_min=60, spool_root=tmp_path)

    nested = []
    daemon = _Pipeline(world)
    monkeypatch.setattr(_rt, "rt_call", daemon)

    def a_second_drain_arrives(index, params):
        # Call 1 stamped the marker; call 2 is the first blank, about to be served.
        if index == 2 and not nested:
            nested.append(drain())

    daemon.on_call = a_second_drain_arrives
    first = drain()

    assert [(n["busy"], n["claimed"]) for n in nested] == [(1, 0)], "the second drain stood aside"
    assert (first["busy"], first["processed"]) == (0, 2), first["records"]
    row = _stored(world, A)
    assert row["claim"] == CLAIM, "the undo ran after the forget, so the text is back"
    assert not is_forgotten(row)
