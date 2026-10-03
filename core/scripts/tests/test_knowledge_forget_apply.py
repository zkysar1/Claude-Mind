"""Tests for the soft forget and the undo of a wiki node ().

The forget leg of ``knowledge-edit-apply.py``. A synthetic world with a parent that lists
two ordered children and a leaf that has no parent, and a retention directory that sits OUTSIDE
the world, as the caller's does. Handles are minted with the same ``item_handle`` the export
publishes. What these tests pin is the contract the member relies on: a forgotten node is
gone from everything the resident and the member read, its text lives in exactly one place
outside the world, an undo puts it back byte for byte in the slot it left, and every
partial failure leaves a state that is safe to be in.
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest
import yaml

_SCRIPTS = Path(__file__).resolve().parents[1]
_ROOT = _SCRIPTS.parents[1]
for _p in (str(_SCRIPTS), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import knowledge_retention  # noqa: E402
from knowledge_projection import item_handle  # noqa: E402


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


apply_mod = _load("knowledge_edit_apply_forget_tests", "knowledge-edit-apply.py")
export_mod = _load("knowledge_export_forget_tests", "knowledge-export.py")

SECRET = "handle-secret-for-tests"
ENV_ID = "env-under-test"
TODAY = datetime.datetime.now(datetime.timezone.utc).date().isoformat()

WIDGETS = "---\ntopic: Acme widgets\nlast_updated: 2026-01-01\n---\n\n# Acme widgets\n\nWidgets are blue.\n"
GEARS = "---\ntopic: Acme gears\n---\n\nGears are round.\n"
LONER = "---\ntopic: Loner\n---\n\nThe loner is a secret nobody else knows.\n"
SECRET_PHRASE = "Widgets are blue."


def _entry(file_rel: str, summary: str, **extra) -> dict:
    return {"file": f"world/knowledge/tree/{file_rel}", "summary": summary,
            "last_updated": "2026-01-01", **extra}


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = tmp_path / "world"
    tree = w / "knowledge" / "tree"
    (tree / "acme").mkdir(parents=True)
    for rel, text in {"acme.md": "---\ntopic: Acme\n---\n\nAcme overview.\n",
                      "acme/acme-widgets.md": WIDGETS, "acme/acme-gears.md": GEARS,
                      "loner.md": LONER}.items():
        (tree / rel).write_text(text, encoding="utf-8")
    index = {"last_updated": "2026-01-01", "nodes": {
        "acme": _entry("acme.md", "Acme.", node_type="interior", child_count=2,
                       children=["acme-widgets", "acme-gears"]),
        "acme-widgets": _entry("acme/acme-widgets.md", "Widgets.", parent="acme",
                               node_type="leaf", confidence=0.8),
        "acme-gears": _entry("acme/acme-gears.md", "Gears.", parent="acme", node_type="leaf"),
        "loner": _entry("loner.md", "Alone.", node_type="leaf"),
    }}
    (tree / "_tree.yaml").write_text(yaml.safe_dump(index, sort_keys=False), encoding="utf-8")
    (w / "pipeline.jsonl").write_text("", encoding="utf-8")
    (w / "guardrails.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setenv("WORLD_PATH", str(w))
    monkeypatch.setenv(export_mod._GOAL_HANDLE_SECRET_VAR, SECRET)
    monkeypatch.setenv("ENVIRONMENT_ID", ENV_ID)
    monkeypatch.setenv("MIND_SID", "sid-under-test")
    return w


@pytest.fixture
def retention(tmp_path):
    """Outside the world, as the caller's directory is. Created by the first forget."""
    return tmp_path / "spool" / ENV_ID / "retention"


def _handle(key: str) -> str:
    return item_handle("node", key, SECRET, ENV_ID)


def _index(world: Path) -> dict:
    return yaml.safe_load((world / "knowledge" / "tree" / "_tree.yaml").read_text(encoding="utf-8"))


def _write_index(world: Path, data: dict) -> None:
    (world / "knowledge" / "tree" / "_tree.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _page(world: Path, rel: str) -> Path:
    return world / "knowledge" / "tree" / rel


def _files(*roots: Path) -> dict:
    return {p: p.read_bytes() for r in roots if r.exists() for p in r.rglob("*") if p.is_file()}


def _forget(retention: Path, key: str, *, apply: bool = True, report: dict | None = None) -> int:
    argv = ["--handle", _handle(key), "--op", "forget", f"--retention-dir={retention}"]
    return apply_mod.main(argv + (["--apply"] if apply else []), report)


def _undo(retention: Path, key: str, *, report: dict | None = None) -> int:
    return apply_mod.main(["--handle", _handle(key), "--op", "undo",
                           f"--retention-dir={retention}", "--apply"], report)


def _keys(world: Path) -> set:
    return {n["key"] for n in export_mod.read_tree_nodes(world)}


# --- forget -------------------------------------------------------------------


def test_forget_hides_the_node_keeps_one_retained_copy_and_blanks_the_file(world, retention, capsys):
    original = _page(world, "acme/acme-widgets.md").read_bytes()
    report = {}

    rc = _forget(retention, "acme-widgets", report=report)

    assert rc == 0
    assert "node acme-widgets: forget" in capsys.readouterr().out
    assert "acme-widgets" not in _keys(world), "the export, and so the member, no longer lists it"
    assert "acme-widgets" not in _index(world)["nodes"]
    blank = _page(world, "acme/acme-widgets.md").read_text(encoding="utf-8")
    assert blank == f"---\ntopic: forgotten\n---\n\nForgotten by the member on {TODAY}.\n"
    records = list(knowledge_retention.list_records(retention))
    assert len(records) == 1
    _path, record = records[0]
    assert knowledge_retention.retained_content(record) == original
    assert (report["forgotten"], report["applied"], report["undo_until"]) == (
        True, True, record["undo_until"])
    assert "Widgets are blue" not in json.dumps(report), "the report never carries the text"


def test_the_forgotten_text_is_held_in_exactly_one_place_and_not_under_the_world(world, retention):
    assert any(SECRET_PHRASE.encode() in b for b in _files(world).values()), "positive control"

    _forget(retention, "acme-widgets")

    assert not any(SECRET_PHRASE.encode() in b for b in _files(world).values()), (
        "nothing under the world still holds the text, so a resident that greps its own "
        "world cannot find it")
    record_file = next(retention.glob("*.json"))
    assert SECRET_PHRASE.encode() not in record_file.read_bytes(), "the record holds it encoded"
    assert knowledge_retention.retained_content(json.loads(record_file.read_text())) is not None


def test_a_forgotten_node_can_no_longer_be_addressed(world, retention, capsys):
    _forget(retention, "acme-widgets")
    capsys.readouterr()

    rc = apply_mod.main(["--handle", _handle("acme-widgets"), "--op", "edit",
                         "--text", "Widgets are green.", "--apply"])

    assert rc == 3
    assert "refused (not_addressable)" in capsys.readouterr().err


def test_forget_takes_the_entry_out_of_its_parent_and_an_emptied_parent_becomes_a_leaf(world, retention):
    _forget(retention, "acme-widgets")
    parent = _index(world)["nodes"]["acme"]
    assert (parent["children"], parent["child_count"], parent["node_type"]) == (
        ["acme-gears"], 1, "interior")

    _forget(retention, "acme-gears")
    parent = _index(world)["nodes"]["acme"]
    assert (parent["children"], parent["child_count"], parent["node_type"]) == ([], 0, "leaf")
    assert _index(world)["last_updated"] == TODAY


def test_a_node_without_a_parent_is_forgotten_without_touching_any_other_entry(world, retention):
    before = _index(world)["nodes"]

    assert _forget(retention, "loner") == 0

    after = _index(world)["nodes"]
    assert set(after) == set(before) - {"loner"}
    assert all(after[k] == before[k] for k in after), "no other entry was rewritten"


def test_a_node_with_children_is_refused_and_nothing_is_written(world, retention, capsys):
    stores = _files(world)
    report = {}

    rc = _forget(retention, "acme", report=report)

    assert rc == 3
    assert report == {"refused": "forget_not_a_leaf"}
    assert "refused (forget_not_a_leaf)" in capsys.readouterr().err
    assert _files(world) == stores
    assert not retention.exists()


def test_a_forget_with_no_retention_directory_is_refused_and_nothing_is_written(world, capsys):
    stores = _files(world)
    report = {}

    rc = apply_mod.main(["--handle", _handle("acme-widgets"), "--op", "forget", "--apply"], report)

    assert (rc, report) == (3, {"refused": "no_retention_store"})
    assert _files(world) == stores


def test_a_dry_run_forget_writes_nothing(world, retention, capsys):
    stores = _files(world)

    rc = _forget(retention, "acme-widgets", apply=False)

    assert rc == 0
    assert "dry run" in capsys.readouterr().out
    assert _files(world) == stores
    assert not retention.exists()


def test_a_kind_with_an_edit_and_no_forgetter_is_refused_and_nothing_is_written(world, retention, monkeypatch):
    """The net under a kind that gains an edit before it gains a forgetter: it is refused, never
    forgotten some other way. The hypothesis and guardrail forgets have their own files
    (test_knowledge_forget_hypothesis_apply.py, test_knowledge_forget_guardrail_apply.py)."""
    (world / "guardrails.jsonl").write_text(
        json.dumps({"id": "guard-1", "category": "acme", "rule": "r", "status": "active"}) + "\n",
        encoding="utf-8")
    stores = _files(world)
    monkeypatch.delitem(apply_mod._FORGETTERS, "guardrail")
    report = {}
    rc = apply_mod.main(["--handle", item_handle("guardrail", "guard-1", SECRET, ENV_ID), "--op", "forget",
                         f"--retention-dir={retention}", "--apply"], report)
    assert (rc, report) == (3, {"refused": "forget_pending_ruling"})
    assert not retention.exists()
    assert _files(world) == stores


def test_the_text_is_retained_and_verified_before_anything_is_changed(world, retention, monkeypatch, capsys):
    """guard-6223: a recovery layer that was not verified is not a recovery layer."""
    stores = _files(world)

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(knowledge_retention, "write_record", boom)

    assert _forget(retention, "acme-widgets") == 1
    assert "its text was not retained" in capsys.readouterr().err
    assert _files(world) == stores, "the index and the page are untouched"


def test_a_record_that_does_not_read_back_stops_the_forget_before_it_changes_anything(world, retention, monkeypatch):
    stores = _files(world)
    monkeypatch.setattr(knowledge_retention, "read_record", lambda _path: None)

    assert _forget(retention, "acme-widgets") == 1
    assert _files(world) == stores


def test_an_index_entry_that_changed_after_retention_is_not_forgotten(world, retention, monkeypatch, capsys):
    """The retained copy must be a copy of what the member forgot: if the resident changed
    the entry meanwhile, nothing is dropped and the page is not blanked."""
    real = knowledge_retention.write_record

    def write_then_change(retention_dir, record):
        path = real(retention_dir, record)
        index = _index(world)
        index["nodes"]["acme-widgets"]["summary"] = "Changed by the resident."
        _write_index(world, index)
        return path

    monkeypatch.setattr(knowledge_retention, "write_record", write_then_change)

    assert _forget(retention, "acme-widgets") == 1
    assert "its index entry changed" in capsys.readouterr().err
    assert "acme-widgets" in _index(world)["nodes"]
    assert _page(world, "acme/acme-widgets.md").read_bytes() == WIDGETS.encode("utf-8")


def test_a_page_that_cannot_be_blanked_is_still_forgotten_and_the_report_says_so(world, retention, monkeypatch, capsys):
    """The node is hidden and its text retained, which is what the member asked for. Telling
    them it failed would be wrong, and a retry would find no node to forget."""
    monkeypatch.setattr(apply_mod, "_replace_file", lambda _path, _data: False)
    report = {}

    assert _forget(retention, "acme-widgets", report=report) == 0

    assert report["applied"] is True and report["blanked"] is False
    assert "is forgotten and retained, but its file was not blanked" in capsys.readouterr().err
    assert "acme-widgets" not in _keys(world), "hidden: the privacy-relevant effect came first"
    assert len(list(knowledge_retention.list_records(retention))) == 1, "and recoverable"
    assert _page(world, "acme/acme-widgets.md").read_bytes() == WIDGETS.encode("utf-8"), "the page is left for the erase"


def test_a_ghost_entry_over_a_blanked_page_is_hidden_again_and_the_retained_text_is_kept(world, retention):
    """A merge from a stale copy of the index can bring a forgotten node's entry back. Its page
    is the marker, so there is nothing to retain, and a record written from it would replace
    the text the member can still undo."""
    ghost = dict(_index(world)["nodes"]["acme-widgets"])
    _forget(retention, "acme-widgets")
    retained_path = knowledge_retention.record_path(retention, "node", "acme-widgets")
    retained = knowledge_retention.read_record(retained_path)
    index = _index(world)  # the stale merge: the entry is back, and so is its slot in the parent
    index["nodes"]["acme-widgets"] = ghost
    index["nodes"]["acme"]["children"].insert(0, "acme-widgets")
    index["nodes"]["acme"]["child_count"] = 2
    _write_index(world, index)
    assert "acme-widgets" in _keys(world)
    report = {}

    assert _forget(retention, "acme-widgets", report=report) == 0

    assert "acme-widgets" not in _keys(world)
    assert report["undo_until"] == retained["undo_until"]
    assert knowledge_retention.read_record(retained_path) == retained, "the retained text was not replaced"
    assert _undo(retention, "acme-widgets") == 0
    assert _page(world, "acme/acme-widgets.md").read_bytes() == WIDGETS.encode("utf-8")


def test_a_list_shaped_index_is_refused_rather_than_edited(world, retention):
    index = _index(world)
    _write_index(world, {"nodes": [{"key": k, **v} for k, v in index["nodes"].items()]})
    stores = _files(world)
    report = {}

    rc = _forget(retention, "loner", report=report)

    assert (rc, report) == (3, {"refused": "index_unsupported"})
    assert _files(world) == stores and not retention.exists()


def test_an_unquoted_date_in_the_index_survives_a_forget_and_an_undo(world, retention):
    path = world / "knowledge" / "tree" / "_tree.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace(
        "last_updated: '2026-01-01'", "last_updated: 2026-01-01"), encoding="utf-8")
    assert isinstance(yaml.safe_load(path.read_text(encoding="utf-8"))["nodes"]["loner"]["last_updated"],
                      datetime.date), "positive control: the fixture really holds a date"

    assert _forget(retention, "loner") == 0
    assert _undo(retention, "loner") == 0

    assert _page(world, "loner.md").read_bytes() == LONER.encode("utf-8")
    assert "loner" in _index(world)["nodes"]


# --- undo ---------------------------------------------------------------------


def test_undo_puts_the_page_back_byte_for_byte_and_the_entry_back_in_its_slot(world, retention, capsys):
    before = _index(world)["nodes"]
    _forget(retention, "acme-widgets")
    capsys.readouterr()
    report = {}

    rc = _undo(retention, "acme-widgets", report=report)

    assert rc == 0
    assert "node acme-widgets: undo" in capsys.readouterr().out
    assert report["restored"] is True
    assert _page(world, "acme/acme-widgets.md").read_bytes() == WIDGETS.encode("utf-8")
    nodes = _index(world)["nodes"]
    assert nodes["acme"]["children"] == ["acme-widgets", "acme-gears"], "the slot it left"
    assert (nodes["acme"]["child_count"], nodes["acme"]["node_type"]) == (2, "interior")
    restored = dict(nodes["acme-widgets"])
    assert restored.pop("last_updated") == TODAY
    assert restored == {k: v for k, v in before["acme-widgets"].items() if k != "last_updated"}
    assert "acme-widgets" in _keys(world)


def test_a_restored_node_is_addressable_again(world, retention):
    _forget(retention, "acme-widgets")
    _undo(retention, "acme-widgets")

    nodes = {n["key"]: n for n in export_mod.read_tree_nodes(world)}
    assert nodes["acme-widgets"]["body"].strip().endswith("Widgets are blue.")
    assert export_mod.resolve_item(world, _handle("acme-widgets"))[:2] == ("node", "acme-widgets")


def test_undo_restores_into_the_parent_after_the_parent_was_emptied(world, retention):
    _forget(retention, "acme-widgets")
    _forget(retention, "acme-gears")
    assert _index(world)["nodes"]["acme"]["node_type"] == "leaf"

    assert _undo(retention, "acme-gears") == 0

    parent = _index(world)["nodes"]["acme"]
    assert (parent["children"], parent["child_count"], parent["node_type"]) == (
        ["acme-gears"], 1, "interior")


def test_an_undone_record_no_longer_holds_the_text_and_cannot_be_undone_twice(world, retention, capsys):
    _forget(retention, "acme-widgets")
    _undo(retention, "acme-widgets")
    capsys.readouterr()

    (_path, record), = list(knowledge_retention.list_records(retention))
    assert not knowledge_retention.is_live(record)
    assert "content_b64" not in record and "undone_at" in record
    assert SECRET_PHRASE.encode() not in next(retention.glob("*.json")).read_bytes()

    assert _undo(retention, "acme-widgets") == 3
    assert "refused (not_addressable)" in capsys.readouterr().err


def test_undo_after_the_window_is_refused_and_changes_nothing(world, retention, capsys):
    _forget(retention, "acme-widgets")
    path = knowledge_retention.record_path(retention, "node", "acme-widgets")
    record = json.loads(path.read_text(encoding="utf-8"))
    record["undo_until"] = "2020-01-01T00:00:00+00:00"
    path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    stores = _files(world, retention)
    report = {}

    rc = _undo(retention, "acme-widgets", report=report)

    assert (rc, report) == (3, {"refused": "undo_expired"})
    assert _files(world, retention) == stores


def test_undo_is_refused_when_the_key_is_back_in_the_index(world, retention):
    _forget(retention, "acme-widgets")
    index = _index(world)
    index["nodes"]["acme-widgets"] = _entry("acme/acme-widgets.md", "Relearned.")
    _write_index(world, index)
    report = {}

    rc = _undo(retention, "acme-widgets", report=report)

    assert (rc, report) == (3, {"refused": "undo_conflict"})
    assert _index(world)["nodes"]["acme-widgets"]["summary"] == "Relearned."


def test_undo_is_refused_when_a_new_page_was_written_at_the_path(world, retention):
    _forget(retention, "acme-widgets")
    _page(world, "acme/acme-widgets.md").write_text("A page written since.\n", encoding="utf-8")
    report = {}

    rc = _undo(retention, "acme-widgets", report=report)

    assert (rc, report) == (3, {"refused": "undo_conflict"})
    assert _page(world, "acme/acme-widgets.md").read_text(encoding="utf-8") == "A page written since.\n"


def test_undo_is_allowed_when_the_blanking_never_ran(world, retention, monkeypatch):
    """A stop between the drop and the blanking leaves the page itself in place: that is
    still the member's page, and an undo must not call it a conflict."""
    with monkeypatch.context() as patched:  # not monkeypatch.undo(): that would drop the env too
        patched.setattr(apply_mod, "_replace_file", lambda _path, _data: False)
        _forget(retention, "acme-widgets")
    assert _page(world, "acme/acme-widgets.md").read_bytes() == WIDGETS.encode("utf-8")

    assert _undo(retention, "acme-widgets") == 0
    assert "acme-widgets" in _index(world)["nodes"]


def test_undo_is_refused_when_the_parent_is_gone(world, retention):
    _forget(retention, "acme-widgets")
    index = _index(world)
    del index["nodes"]["acme"]
    _write_index(world, index)
    stores = _files(world, retention)
    report = {}

    rc = _undo(retention, "acme-widgets", report=report)

    assert (rc, report) == (3, {"refused": "undo_parent_missing"})
    assert _files(world, retention) == stores


def test_a_failed_index_restore_puts_the_marker_back_so_no_orphan_holds_the_text(world, retention, monkeypatch, capsys):
    _forget(retention, "acme-widgets")
    monkeypatch.setattr(apply_mod, "_restore_index_entry", lambda *_a, **_k: "key_taken")

    assert _undo(retention, "acme-widgets") == 1

    assert "not restored: key_taken" in capsys.readouterr().err
    assert SECRET_PHRASE.encode() not in _page(world, "acme/acme-widgets.md").read_bytes()
    assert "acme-widgets" not in _index(world)["nodes"]
    assert knowledge_retention.is_live(next(r for _p, r in knowledge_retention.list_records(retention))), (
        "and the retained copy is still there to try again")


def test_undo_with_no_retention_directory_or_nothing_retained_there_is_not_addressable(world, retention):
    _forget(retention, "acme-widgets")
    for extra in ([], [f"--retention-dir={retention.parent}"]):  # none; a directory holding no record
        report = {}
        rc = apply_mod.main(["--handle", _handle("acme-widgets"), "--op", "undo", "--apply", *extra], report)
        assert (rc, report) == (3, {"refused": "not_addressable"}), extra


def test_a_tampered_record_is_never_used(world, retention):
    _forget(retention, "acme-widgets")
    path = knowledge_retention.record_path(retention, "node", "acme-widgets")
    record = json.loads(path.read_text(encoding="utf-8"))
    record["content_b64"] = "QW5vdGhlciBwYWdlLg=="
    path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    report = {}

    rc = _undo(retention, "acme-widgets", report=report)

    assert (rc, report) == (3, {"refused": "not_addressable"}), "a digest mismatch reads as nothing retained"
    assert "acme-widgets" not in _index(world)["nodes"]


def test_a_record_that_names_a_file_outside_the_tree_is_refused_and_nothing_there_is_read_or_written(world, retention):
    """The record's digest covers its text and not the name of the file it restores to, so that
    name is held to the tree the same way a live node's is."""
    _forget(retention, "acme-widgets")
    outside = world / "outside.md"
    outside.write_text("Not part of the tree.\n", encoding="utf-8")
    path = knowledge_retention.record_path(retention, "node", "acme-widgets")
    record = json.loads(path.read_text(encoding="utf-8"))
    record["restore"]["index_entry"]["file"] = "world/knowledge/tree/../../outside.md"
    path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    stores = _files(world, retention)
    report = {}

    assert _undo(retention, "acme-widgets", report=report) == 3
    assert report == {"refused": "not_addressable"}
    assert _files(world, retention) == stores, "nothing outside the tree was written, nothing changed"

    outside.unlink()  # the same answer whether or not that file exists: a record cannot probe for one
    again = {}
    assert _undo(retention, "acme-widgets", report=again) == 3
    assert again == {"refused": "not_addressable"}


def test_one_unusable_record_in_the_directory_does_not_stop_another_undo(world, retention):
    _forget(retention, "acme-widgets")
    good = knowledge_retention.record_path(retention, "node", "acme-widgets")
    record = json.loads(good.read_text(encoding="utf-8"))
    del record["item_id"]  # it still passes its digest, which covers the text only
    (retention / "node-0000000000000000.json").write_text(json.dumps(record, sort_keys=True), encoding="utf-8")

    assert _undo(retention, "acme-widgets") == 0
    assert "acme-widgets" in _index(world)["nodes"]


def test_a_retained_record_is_not_readable_by_other_users(world, retention):
    if os.name == "nt":  # pragma: no cover - POSIX modes only
        pytest.skip("POSIX file modes")
    _forget(retention, "acme-widgets")
    mode = stat.S_IMODE(next(retention.glob("*.json")).stat().st_mode)
    assert mode & 0o077 == 0, oct(mode)


# --- order ----------------------------------------------------------------------


def test_forget_retains_before_it_drops_and_drops_before_it_blanks(world, retention, monkeypatch):
    """The order is the safety: a stop at any step leaves a state that is safe to be in."""
    seen = {}
    real = apply_mod._drop_index_entry

    def spy(tree_dir, key, entry, parent, today):
        seen["retained"] = [r["item_id"] for _p, r in knowledge_retention.list_records(retention)]
        seen["page"] = _page(world, "acme/acme-widgets.md").read_bytes()
        return real(tree_dir, key, entry, parent, today)

    monkeypatch.setattr(apply_mod, "_drop_index_entry", spy)

    assert _forget(retention, "acme-widgets") == 0
    assert seen["retained"] == ["acme-widgets"], "the verified copy exists before the index is touched"
    assert seen["page"] == WIDGETS.encode("utf-8"), "the page is blanked only after the entry is dropped"


def test_undo_restores_the_file_before_it_restores_the_entry(world, retention, monkeypatch):
    """guard-4836: an index entry with no file behind it is a phantom node."""
    _forget(retention, "acme-widgets")
    seen = {}
    real = apply_mod._restore_index_entry

    def spy(tree_dir, key, restore, today):
        seen["page"] = _page(world, "acme/acme-widgets.md").read_bytes()
        return real(tree_dir, key, restore, today)

    monkeypatch.setattr(apply_mod, "_restore_index_entry", spy)

    assert _undo(retention, "acme-widgets") == 0
    assert seen["page"] == WIDGETS.encode("utf-8")
