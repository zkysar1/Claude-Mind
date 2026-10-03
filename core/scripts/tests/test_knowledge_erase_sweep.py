"""Tests for the 30-day erase of what a member forgot (, unit u4).

``knowledge_erase.sweep`` is the half of the owner's ruling that comes after the undo: once the
window closes, the retained text is deleted and the world's own copy is finished. These tests
drive the REAL applier through a real forget, so every retained record and every blanked page or
record is what production wrote, then move the clock past the window and sweep.

The stores are not faked. ``_Daemon`` stands in for the daemon transport only: every write goes
through the REAL pipeline ``update_field`` and the REAL store ``set_field`` handlers, in process,
against the world's own files, so a blank the writer would refuse or a value it would coerce
fails here as it would there.

What is pinned is the contract the member relies on: an expired item's text is in NO file under
the spool or the world (a positive control proves the search can find it before the sweep), a
record still inside its window is untouched, a guardrail is erased on a local backend and on no other,
a step the sweep cannot
finish keeps the retained record, a shown item is never blanked, and the receipts hold nothing
a reader could learn the text from. The residues the sweep does NOT reach are pinned too, so
nobody reads "erased" as more than it is.
"""

from __future__ import annotations

import base64
import datetime
import gzip
import importlib.util
import json
import os
import re
import stat
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
import knowledge_erase  # noqa: E402
import knowledge_retention  # noqa: E402
import storage_backend  # noqa: E402
from knowledge_projection import FORGOTTEN_FIELD, is_forgotten, item_handle  # noqa: E402
from mind_api.src.endpoints import store as store_endpoints  # noqa: E402
from mind_api.src.world import pipeline_write  # noqa: E402


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


apply_mod = _load("knowledge_edit_apply_erase_tests", "knowledge-edit-apply.py")
export_mod = _load("knowledge_export_erase_tests", "knowledge-export.py")

SECRET = "handle-secret-for-tests"
ENV_ID = "env-under-test"
NOW = knowledge_retention.now_utc()
LATER = NOW + datetime.timedelta(days=31)
TODAY = NOW.date().isoformat()
TOMBSTONE = f"Forgotten by the member on {TODAY}."

NODE_KEY = "acme-widgets"
NODE_TEXT = "BLUE-WIDGET-NODE-TEXT: widgets are blue."
NODE_PAGE = f"---\ntopic: Acme widgets\n---\n\n{NODE_TEXT}\n"

A, B, C = "2026-01-02_green-widgets", "2026-01-03_gear-demand", "2026-01-04_resolved-widgets"
CLAIM = "CLAIM-A: widgets sell better in green than in blue."
TITLE = "TITLE-A: green widgets outsell blue ones"
RATIONALE = "RATIONALE-A: observed across three markets in 2025."
POSITION = "POSITION-A: yes, green widgets outsell blue in every market tested"
OUTCOME_DETAIL = "OUTCOME-A: settled after the spring launch."
PREMORTEM = "PREMORTEM-A: if blue wins in the north this is wrong"
ZH_NOTE = "ZH-A 绿色的小部件卖得更好"
PROSE = (CLAIM, TITLE, RATIONALE, POSITION, OUTCOME_DETAIL, PREMORTEM, ZH_NOTE)

G, H = "guard-1", "guard-2"
RULE = "RULE-G: paint widgets green because blue paint cracks."


def _hyp(item_id: str, **extra) -> dict:
    rec = {"id": item_id, "slug": item_id.split("_", 1)[1], "title": TITLE, "stage": "active",
           "horizon": "session", "type": "calibration", "confidence": 0.6, "position": POSITION,
           "formed_date": "2026-01-02", "category": "acme", "claim": CLAIM, "rationale": RATIONALE,
           "outcome_detail": OUTCOME_DETAIL, "premortem": {"failure": PREMORTEM, "owner": "alpha"},
           "note_zh": ZH_NOTE, "tags": ["paint", "widgets"], "source_goal": "g-100-01",
           "replay_metadata": {"replay_count": 2, "last_replayed": "2026-02-01"},
           "lesson": "Never", "surprise": None, "reflected": False}
    rec.update(extra)
    return rec


def _guard(item_id: str, **extra) -> dict:
    rec = {"id": item_id, "category": "acme", "rule": RULE, "status": "active",
           "created": "2026-01-01T00:00:00", "source": "g-100-01",
           "trigger_condition": "when painting widgets", "tags": ["paint"],
           "when_to_use": {"conditions": ["painting"], "category": "acme"},
           "title": "Paint green", "action_hint": "Run the paint check.", "severity": "HIGH"}
    rec.update(extra)
    return rec


def _control(item_id: str, mark: str, **extra) -> dict:
    """A hypothesis nothing here forgets, with prose of its own in every field the sweep blanks,
    so a search for A's words never finds a control and a control's words survive every pass."""
    return _hyp(item_id, title=f"TITLE-{mark} control title", claim=f"CLAIM-{mark} control claim, long enough.",
                rationale=f"CONTROL-{mark} rationale stays.", position=f"CONTROL-{mark} position stays",
                outcome_detail=f"CONTROL-{mark} detail stays.",
                premortem={"failure": f"CONTROL-{mark} premortem stays", "owner": "alpha"},
                note_zh=f"CONTROL-{mark} 对照 stays", **extra)


def _records() -> list[dict]:
    return [
        _hyp(A),
        _control(B, "B"),
        _control(C, "C", stage="resolved", outcome="CONFIRMED", resolved_date="2026-02-01"),
    ]


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = tmp_path / "world"
    tree = w / "knowledge" / "tree"
    (tree / "acme").mkdir(parents=True)
    (tree / "acme" / "acme-widgets.md").write_text(NODE_PAGE, encoding="utf-8")
    # A second node in the category: the exposure allowlist is derived from the tree, so forgetting
    # the only node of "acme" would hide every acme hypothesis and guardrail from the member too.
    (tree / "acme" / "acme-gears.md").write_text("---\ntopic: Acme gears\n---\n\nGears are steel.\n",
                                                 encoding="utf-8")
    index = {"last_updated": "2026-01-01", "nodes": {
        NODE_KEY: {"file": "world/knowledge/tree/acme/acme-widgets.md",
                   "summary": "Widgets.", "last_updated": "2026-01-01"},
        "acme-gears": {"file": "world/knowledge/tree/acme/acme-gears.md",
                       "summary": "Gears.", "last_updated": "2026-01-01"}}}
    (tree / "_tree.yaml").write_text(yaml.safe_dump(index, sort_keys=False), encoding="utf-8")
    (w / "pipeline.jsonl").write_text("".join(json.dumps(r) + "\n" for r in _records()), encoding="utf-8")
    (w / "guardrails.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in (_guard(G), _guard(H, rule="Ship gears in wooden crates.",
                                                                  title="Crate gears"))),
        encoding="utf-8")
    monkeypatch.setenv("WORLD_PATH", str(w))
    monkeypatch.setenv(export_mod._GOAL_HANDLE_SECRET_VAR, SECRET)
    monkeypatch.setenv("ENVIRONMENT_ID", ENV_ID)
    monkeypatch.setenv("MIND_SID", "sid-under-test")
    return w


@pytest.fixture
def env(tmp_path):
    """The environment's spool directory, outside the world, as the drain's is."""
    path = tmp_path / "spool" / ENV_ID
    path.mkdir(parents=True)
    return path


@pytest.fixture
def retention(env):
    return env / "retention"


class _Daemon:
    """Stands in for the daemon transport of the pipeline's update-field and the guardrail
    store's set-field and append. Every call that is let through runs the real handler against
    the world's own files. ``fail`` names pipeline fields whose write raises, as a daemon 500
    does, BEFORE anything is written; ``lie`` names fields the daemon acknowledges with a 200 and
    does not write. ``fail_guard`` and ``lie_guard`` do the same for guardrail fields, and ``sets``
    lists every guardrail set-field as ``(id, field)``."""

    def __init__(self, world, fail=(), lie=(), fail_guard=(), lie_guard=()):
        self.world = world
        self.fail = set(fail)
        self.lie = set(lie)
        self.fail_guard = set(fail_guard)
        self.lie_guard = set(lie_guard)
        self.writes = []  # (path, field)
        self.sets = []  # (id, field) of every guardrail set-field

    def __call__(self, method, path, query=None, body=None, headers=None):
        assert method == "POST", method
        params = {k: v[0] for k, v in urllib.parse.parse_qs(query or "", keep_blank_values=True).items()}
        self.writes.append((path, params.get("field")))
        if path == "/v1/pipeline/update-field":
            if params["field"] in self.fail:
                raise _rt.RtError("daemon HTTP 500")
            if params["field"] in self.lie:
                return json.dumps({"ok": True, "record": _stored(self.world, params["id"])})
            ctx = SimpleNamespace(query=params, paths=SimpleNamespace(world=self.world), headers={})
            resp = pipeline_write.update_field(ctx)
        elif path in ("/v1/store/append", "/v1/store/set-field"):
            if path.endswith("set-field"):
                self.sets.append((params.get("id"), params.get("field")))
                if params.get("field") in self.fail_guard:
                    raise _rt.RtError("daemon HTTP 500")
                if params.get("field") in self.lie_guard:
                    return json.dumps({"ok": True, "record": _stored(self.world, params["id"], "guardrails.jsonl")})
            ctx = SimpleNamespace(query=params, paths=SimpleNamespace(world=self.world),
                                  headers={"x-mind-agent": "alpha"},
                                  body=body.encode("utf-8") if body else b"")
            resp = (store_endpoints.append if path.endswith("append") else store_endpoints.set_field)(ctx)
        else:
            raise AssertionError(path)
        if resp.status >= 400:
            raise _rt.RtError(f"daemon HTTP {resp.status}: {resp.body.decode('utf-8')}",
                              status=resp.status, body=resp.body.decode("utf-8"))
        return resp.body.decode("utf-8")


@pytest.fixture
def daemon(world, monkeypatch):
    stand_in = _Daemon(world)
    monkeypatch.setattr(_rt, "rt_call", stand_in)
    return stand_in


# --- helpers -------------------------------------------------------------------


def _read_lines(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln]


def _stored_all(world: Path, name: str = "pipeline.jsonl") -> list[dict]:
    return _read_lines(world / name)


def _stored(world: Path, item_id: str, name: str = "pipeline.jsonl") -> dict:
    return next(r for r in _stored_all(world, name) if r["id"] == item_id)


def _write_all(world: Path, rows: list[dict], name: str = "pipeline.jsonl") -> None:
    (world / name).write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _handle(kind: str, item_id: str) -> str:
    return item_handle(kind, item_id, SECRET, ENV_ID)


def _forget(retention: Path, kind: str, item_id: str, *, report: dict | None = None) -> int:
    return apply_mod.main(["--handle", _handle(kind, item_id), "--op", "forget",
                           f"--retention-dir={retention}", "--apply"], report)


def _undo(retention: Path, kind: str, item_id: str) -> int:
    return apply_mod.main(["--handle", _handle(kind, item_id), "--op", "undo",
                           f"--retention-dir={retention}", "--apply"])


def _retained(retention: Path, kind: str, item_id: str) -> dict | None:
    return knowledge_retention.read_record(knowledge_retention.record_path(retention, kind, item_id))


def _record_file(retention: Path, kind: str, item_id: str) -> Path:
    return knowledge_retention.record_path(retention, kind, item_id)


def _sweep(env: Path, retention: Path, *, apply: bool = True, now=None, applier=apply_mod) -> dict:
    return knowledge_erase.sweep(env, retention, applier, apply=apply, now=now or LATER)


def _plain(data: bytes) -> bytes:
    """What a file under the history store says once its gzip layer is off."""
    try:
        return gzip.decompress(data)
    except (OSError, EOFError):
        return data


def _holders(root: Path, needle: bytes, *, skip: str | None = None) -> list[Path]:
    """Every file under ``root`` whose bytes, or gunzipped bytes, hold ``needle``."""
    hits = []
    for path in Path(root).rglob("*"):
        if not path.is_file() or (skip and skip in path.relative_to(root).parts):
            continue
        data = path.read_bytes()
        if needle in data or needle in _plain(data):
            hits.append(path)
    return hits


def _files(*roots: Path) -> dict:
    return {p: p.read_bytes() for r in roots if r.exists() for p in r.rglob("*") if p.is_file()}


def _b64(text: str) -> bytes:
    return base64.b64encode(text.encode("utf-8"))


def _receipts(env: Path) -> list[dict]:
    return _read_lines(env / knowledge_erase.RECEIPTS)


def _statement(record: dict) -> bytes:
    return knowledge_retention.retained_content(record)


# --- the window ------------------------------------------------------------------


def test_a_record_inside_its_window_is_left_alone(world, daemon, env, retention):
    assert _forget(retention, "node", NODE_KEY) == 0
    assert _forget(retention, "hypothesis", A) == 0
    before = _files(env, world)

    out = _sweep(env, retention, now=NOW + datetime.timedelta(days=29))

    assert (out["erased"], out["pending"], out["failed"], out["would_erase"]) == (0, 0, 0, 0)
    assert out["entries"] == []
    assert _files(env, world) == before, "nothing under the spool or the world was touched"
    assert not (env / knowledge_erase.RECEIPTS).exists()


def test_the_window_closes_at_the_instant_the_undo_refuses(world, daemon, env, retention, monkeypatch):
    assert _forget(retention, "node", NODE_KEY) == 0
    record = _retained(retention, "node", NODE_KEY)
    until = datetime.datetime.fromisoformat(record["undo_until"])
    just_after = until + datetime.timedelta(seconds=1)

    on_the_edge = _sweep(env, retention, apply=False, now=until)
    past_it = _sweep(env, retention, apply=False, now=just_after)

    assert on_the_edge["would_erase"] == 0 and knowledge_retention.in_window(record, until)
    assert past_it["would_erase"] == 1 and not knowledge_retention.in_window(record, just_after)
    # The applier's own undo makes the same call at the same instant.
    monkeypatch.setattr(knowledge_retention, "now_utc", lambda: just_after)
    report: dict = {}
    rc = apply_mod.main(["--handle", _handle("node", NODE_KEY), "--op", "undo",
                         f"--retention-dir={retention}", "--apply"], report)
    assert (rc, report["refused"]) == (3, "undo_expired")


# --- a node ----------------------------------------------------------------------


def test_an_expired_node_is_erased_and_its_text_is_in_no_file_under_the_spool_or_the_world(
        world, env, retention):
    assert _forget(retention, "node", NODE_KEY) == 0
    page = base64.b64encode(NODE_PAGE.encode("utf-8"))
    assert _holders(env, page), "positive control: the retained record holds the whole page"
    assert not _holders(world, NODE_TEXT.encode("utf-8")), "and the forget already blanked the page"

    out = _sweep(env, retention)

    assert (out["erased"], out["failed"], out["pending"]) == (1, 0, 0)
    assert not _record_file(retention, "node", NODE_KEY).exists()
    for root in (env, world):
        assert not _holders(root, NODE_TEXT.encode("utf-8"))
        assert not _holders(root, page)


def test_a_node_whose_blank_never_ran_is_blanked_before_its_record_is_erased(
        world, env, retention, monkeypatch):
    """The forget stopped after dropping the index entry and before blanking the page, so the page
    still holds the text and so does the retained record. The sweep owes the blank."""
    real = apply_mod._replace_file
    monkeypatch.setattr(apply_mod, "_replace_file", lambda path, data: False)
    assert _forget(retention, "node", NODE_KEY) == 0  # the forget stands, blanked: False
    monkeypatch.setattr(apply_mod, "_replace_file", real)
    page = world / "knowledge" / "tree" / "acme" / "acme-widgets.md"
    assert page.read_text(encoding="utf-8") == NODE_PAGE, "positive control: the page still holds the text"

    out = _sweep(env, retention)

    assert (out["erased"], out["failed"], out["world_changed"]) == (1, 0, 1)
    assert page.read_bytes().startswith(apply_mod._TOMBSTONE_HEAD.encode("utf-8"))
    assert not _holders(world, NODE_TEXT.encode("utf-8"))
    assert _receipts(env)[-1]["world"] == {"page_blanked": True}


def test_a_page_that_cannot_be_blanked_keeps_the_retained_record(world, env, retention, monkeypatch):
    real = apply_mod._replace_file
    monkeypatch.setattr(apply_mod, "_replace_file", lambda path, data: False)
    assert _forget(retention, "node", NODE_KEY) == 0

    out = _sweep(env, retention)

    assert (out["erased"], out["failed"]) == (0, 1)
    assert out["entries"] == [{"kind": "node", "record": _record_file(retention, "node", NODE_KEY).name,
                               "action": "failed", "detail": "page_not_blanked"}]
    assert _record_file(retention, "node", NODE_KEY).exists(), "nothing is deleted while the world holds text"
    assert not (env / knowledge_erase.RECEIPTS).exists()
    monkeypatch.setattr(apply_mod, "_replace_file", real)
    assert _sweep(env, retention)["erased"] == 1, "the next pass finishes what this one could not"


def test_a_ghost_index_entry_over_a_blanked_page_is_dropped_again(world, env, retention):
    """A merge from a stale copy of the index can bring the entry back over the blanked page."""
    assert _forget(retention, "node", NODE_KEY) == 0
    record = _retained(retention, "node", NODE_KEY)
    tree = world / "knowledge" / "tree" / "_tree.yaml"
    index = yaml.safe_load(tree.read_text(encoding="utf-8"))
    index["nodes"][NODE_KEY] = record["restore"]["index_entry"]
    tree.write_text(yaml.safe_dump(index, sort_keys=False), encoding="utf-8")

    out = _sweep(env, retention)

    assert (out["erased"], out["failed"], out["world_changed"]) == (1, 0, 1)
    assert NODE_KEY not in yaml.safe_load(tree.read_text(encoding="utf-8"))["nodes"]
    assert _receipts(env)[-1]["world"] == {"ghost_dropped": True}


def test_an_entry_that_points_at_another_page_is_not_a_ghost_of_this_one(world, env, retention):
    assert _forget(retention, "node", NODE_KEY) == 0
    record = _retained(retention, "node", NODE_KEY)
    tree = world / "knowledge" / "tree" / "_tree.yaml"
    other = dict(record["restore"]["index_entry"], file="world/knowledge/tree/acme/other-page.md")
    index = yaml.safe_load(tree.read_text(encoding="utf-8"))
    index["nodes"][NODE_KEY] = other
    tree.write_text(yaml.safe_dump(index, sort_keys=False), encoding="utf-8")
    before = tree.read_bytes()

    out = _sweep(env, retention)

    assert (out["erased"], out["failed"], out["world_changed"]) == (1, 0, 0)
    assert tree.read_bytes() == before, "a node re-created under the same key is somebody's live node"


def test_a_node_that_is_still_shown_is_never_blanked(world, env, retention, monkeypatch):
    """The forget stopped before it dropped the index entry: the node is live and shown, its page
    equals the retained copy, and blanking it would destroy a live node. The retained record is
    inert and is erased; the page is not."""
    monkeypatch.setattr(apply_mod, "_drop_index_entry", lambda *a, **k: False)
    assert _forget(retention, "node", NODE_KEY) == 1  # refused: the entry changed while it was retained
    page = world / "knowledge" / "tree" / "acme" / "acme-widgets.md"
    assert page.read_text(encoding="utf-8") == NODE_PAGE

    out = _sweep(env, retention)

    assert (out["erased"], out["failed"], out["world_changed"]) == (1, 0, 0)
    assert page.read_text(encoding="utf-8") == NODE_PAGE, "positive control: the live page is intact"
    assert NODE_KEY in yaml.safe_load(
        (world / "knowledge" / "tree" / "_tree.yaml").read_text(encoding="utf-8"))["nodes"]


def test_an_index_that_cannot_be_read_blanks_nothing_and_keeps_the_record(world, env, retention, monkeypatch):
    real = apply_mod._replace_file
    monkeypatch.setattr(apply_mod, "_replace_file", lambda path, data: False)
    assert _forget(retention, "node", NODE_KEY) == 0
    monkeypatch.setattr(apply_mod, "_replace_file", real)
    (world / "knowledge" / "tree" / "_tree.yaml").write_text("{: not yaml", encoding="utf-8")
    page = world / "knowledge" / "tree" / "acme" / "acme-widgets.md"

    out = _sweep(env, retention)

    assert (out["erased"], out["failed"]) == (0, 1)
    assert out["entries"][0]["detail"] == "index_unreadable"
    assert page.read_text(encoding="utf-8") == NODE_PAGE, "without the index a shown page cannot be told from a forgotten one"
    assert _record_file(retention, "node", NODE_KEY).exists()


def test_an_index_that_moves_between_the_two_reads_decides_nothing_and_keeps_the_record(
        world, env, retention, monkeypatch):
    """The entry is there when the index is mapped and gone when its snapshot is taken. The sweep
    cannot say what it is looking at, so it blanks and drops nothing and decides on the next pass."""
    assert _forget(retention, "node", NODE_KEY) == 0
    record = _retained(retention, "node", NODE_KEY)
    tree = world / "knowledge" / "tree" / "_tree.yaml"
    index = yaml.safe_load(tree.read_text(encoding="utf-8"))
    index["nodes"][NODE_KEY] = record["restore"]["index_entry"]  # a ghost: the entry is back
    tree.write_text(yaml.safe_dump(index, sort_keys=False), encoding="utf-8")
    before = tree.read_bytes()
    real = apply_mod._index_snapshot
    monkeypatch.setattr(apply_mod, "_index_snapshot", lambda *a, **k: None)

    out = _sweep(env, retention)

    assert (out["erased"], out["failed"]) == (0, 1)
    assert out["entries"][0]["detail"] == "index_changed"
    assert tree.read_bytes() == before
    assert _record_file(retention, "node", NODE_KEY).exists()
    monkeypatch.setattr(apply_mod, "_index_snapshot", real)
    again = _sweep(env, retention)
    assert (again["erased"], again["failed"], again["world_changed"]) == (1, 0, 1), "and the next pass decides"


def test_a_page_that_is_not_the_retained_text_is_pending_and_never_blanked_or_erased_over(
        world, env, retention, monkeypatch):
    """The blank never ran, and since then the page at that path was rewritten: whatever it holds
    now may be the member's words or somebody else's, and only a person can say which. The sweep
    blanks only what it can show is the member's, and keeps the retained copy a person would
    compare it with."""
    real = apply_mod._replace_file
    monkeypatch.setattr(apply_mod, "_replace_file", lambda path, data: False)
    assert _forget(retention, "node", NODE_KEY) == 0
    monkeypatch.setattr(apply_mod, "_replace_file", real)
    page = world / "knowledge" / "tree" / "acme" / "acme-widgets.md"
    rewritten = "---\ntopic: Something else\n---\n\nA new page at the old path.\n"
    page.write_text(rewritten, encoding="utf-8")

    out = _sweep(env, retention)

    assert (out["erased"], out["failed"], out["pending"], out["world_changed"]) == (0, 0, 1, 0)
    assert page.read_text(encoding="utf-8") == rewritten
    assert _record_file(retention, "node", NODE_KEY).exists()


# --- a hypothesis -----------------------------------------------------------------


def test_a_forgotten_hypothesis_is_reduced_to_its_identity_and_lifecycle(world, daemon, env, retention):
    assert _forget(retention, "hypothesis", A) == 0
    before_b = _stored(world, B)
    statement = _statement(_retained(retention, "hypothesis", A))
    assert _holders(env, base64.b64encode(statement)), "positive control: the retained record holds the statement"
    # What the forget left in the world: the statement blank, the supporting text not.
    kept = _stored(world, A)
    assert (kept["claim"], kept["title"]) == (TOMBSTONE, TOMBSTONE)
    assert RATIONALE in kept["rationale"] and POSITION in kept["position"]

    out = _sweep(env, retention)

    assert (out["erased"], out["failed"], out["pending"], out["world_changed"]) == (1, 0, 0, 1)
    now = _stored(world, A)
    for name in ("claim", "title", "position", "rationale", "outcome_detail", "note_zh"):
        assert now[name] == TOMBSTONE, name
    assert now["premortem"] == {}, "a nested value that held prose is emptied to its own type"
    # Identity, lifecycle, markers and everything that is not prose are untouched.
    for name in ("id", "slug", "stage", "horizon", "type", "category", "confidence", "formed_date",
                 "tags", "source_goal", "replay_metadata", "surprise", "reflected", "lesson"):
        assert now[name] == _hyp(A)[name], name
    assert is_forgotten(now) and now[FORGOTTEN_FIELD] == kept[FORGOTTEN_FIELD]
    assert _stored(world, B) == before_b, "the control hypothesis is byte for byte what it was"
    assert not _record_file(retention, "hypothesis", A).exists()
    for text in PROSE:
        assert not _holders(env, text.encode("utf-8"))
        assert not _holders(world, text.encode("utf-8"), skip=".history"), text
    assert not _holders(env, base64.b64encode(statement))


def test_the_pipelines_own_history_still_holds_the_text_after_the_erase(world, daemon, env, retention):
    """The residue is named, not hidden: every pipeline write snapshots the file first, so the
    pre-forget text survives under the world's history directory and this sweep does not reach it.
    The privacy text has to say so (g-335-1726 u6)."""
    assert _forget(retention, "hypothesis", A) == 0

    assert _sweep(env, retention)["erased"] == 1

    holders = _holders(world, RATIONALE.encode("utf-8"))
    assert holders, "the history snapshot of the pre-forget pipeline is real"
    assert {p.relative_to(world).parts[0] for p in holders} == {".history"}


def test_a_forgotten_one_word_claim_is_still_blanked(world, daemon, env, retention):
    """A claim of identifier characters only reads as an id by shape, so the statement fields are
    blanked by name as well."""
    rows = _stored_all(world)
    rows[0]["claim"] = "green-widgets-win"
    _write_all(world, rows)
    assert _forget(retention, "hypothesis", A) == 0
    rows = _stored_all(world)
    rows[0]["claim"] = "green-widgets-win"  # the member's forget blanked it; put it back, as a stale copy would
    _write_all(world, rows)

    assert _sweep(env, retention)["erased"] == 1

    assert _stored(world, A)["claim"] == TOMBSTONE


def test_a_marker_whose_text_was_never_blanked_is_finished_with_no_retained_record(world, daemon, env, retention):
    """The residue the pipeline merge leaves: two copies of one forget caught at different writes,
    the marker stamped and the text not blanked, and no retained record on this box at all."""
    old = (NOW - datetime.timedelta(days=40)).replace(microsecond=0).isoformat()
    rows = _stored_all(world)
    rows[0][FORGOTTEN_FIELD] = old
    _write_all(world, rows)
    before_b = _stored(world, B)
    retention.mkdir(parents=True)  # a box that has handled a forget has the directory, empty or not

    out = _sweep(env, retention)  # and holds no retained record for this one

    assert (out["erased"], out["failed"], out["world_changed"]) == (0, 0, 1)
    assert [e["action"] for e in out["entries"]] == ["blanked"]
    now = _stored(world, A)
    assert (now["claim"], now["title"], now["rationale"]) == (TOMBSTONE, TOMBSTONE, TOMBSTONE)
    assert _stored(world, B) == before_b
    assert not (env / knowledge_erase.RECEIPTS).exists(), "nothing was deleted, so there is no receipt"
    assert _sweep(env, retention)["entries"] == [], "a second pass has nothing left to do"


def test_a_marker_still_inside_its_window_is_left_exactly_as_it_is(world, daemon, env, retention):
    """An undo restores the statement and nothing else, so the supporting text of a record that can
    still be undone must not be blanked: the member would get back a record that lost it."""
    stamp = (NOW - datetime.timedelta(days=5)).replace(microsecond=0).isoformat()
    rows = _stored_all(world)
    rows[0][FORGOTTEN_FIELD] = stamp
    _write_all(world, rows)
    retention.mkdir(parents=True)
    before = _stored(world, A)

    out = _sweep(env, retention, now=NOW)

    assert out["entries"] == [] and _stored(world, A) == before


def test_a_marker_stamp_that_does_not_read_counts_as_a_closed_window_when_nothing_can_undo_it(
        world, daemon, env, retention):
    """With no retained record there is nothing an undo could restore from, and a stamp that does not
    read names no window, so the marker is treated as over and the record is finished."""
    rows = _stored_all(world)
    rows[0][FORGOTTEN_FIELD] = "not-a-date"
    _write_all(world, rows)
    retention.mkdir(parents=True)

    out = _sweep(env, retention)

    assert (out["erased"], out["failed"], out["world_changed"]) == (0, 0, 1)
    assert [e["action"] for e in out["entries"]] == ["blanked"]
    assert _stored(world, A)["rationale"] == TOMBSTONE


def test_a_writer_that_reports_a_write_it_did_not_make_keeps_the_retained_record(
        world, daemon, env, retention, monkeypatch):
    """The sweep does not take the applier's word for a write: it reads the world again, and a copy
    that still holds text keeps the retained record and fails the pass."""
    assert _forget(retention, "hypothesis", A) == 0
    real = apply_mod._write_hypothesis_field
    monkeypatch.setattr(apply_mod, "_write_hypothesis_field", lambda *a, **k: True)

    out = _sweep(env, retention)

    assert (out["erased"], out["failed"]) == (0, 1)
    assert out["entries"][0]["detail"] == "text_remains"
    assert _record_file(retention, "hypothesis", A).exists()
    assert _stored(world, A)["rationale"] == RATIONALE, "positive control: nothing was written"
    monkeypatch.setattr(apply_mod, "_write_hypothesis_field", real)
    assert _sweep(env, retention)["erased"] == 1, "the next pass, with a writer that writes, finishes it"
    assert _stored(world, A)["rationale"] == TOMBSTONE


def test_a_hypothesis_that_is_still_shown_is_never_blanked(world, daemon, env, retention):
    """The forget retained the statement and stopped before it stamped the marker. The record is
    shown and live; its retained copy is inert and is erased, and the record is left alone."""
    daemon.fail = {FORGOTTEN_FIELD}
    assert _forget(retention, "hypothesis", A) == 1
    daemon.fail = set()
    before = _stored(world, A)
    assert not is_forgotten(before)

    out = _sweep(env, retention)

    assert (out["erased"], out["failed"], out["world_changed"]) == (1, 0, 0)
    assert _stored(world, A) == before


@pytest.mark.parametrize("how", ["fail", "lie"])
def test_a_blank_the_writer_does_not_land_keeps_the_retained_record(world, daemon, env, retention, how):
    """A 200 is not evidence the value landed, and a refused write lands nothing: either way the
    world still holds text, so the retained record stays and the next pass tries again."""
    assert _forget(retention, "hypothesis", A) == 0
    setattr(daemon, how, {"rationale"})

    out = _sweep(env, retention)

    assert (out["erased"], out["failed"]) == (0, 1)
    assert out["entries"][0]["detail"] == "blank_failed"
    assert _record_file(retention, "hypothesis", A).exists()
    assert _stored(world, A)["position"] == TOMBSTONE, "every field was tried, not just up to the failure"
    setattr(daemon, how, set())
    assert _sweep(env, retention)["erased"] == 1
    assert _stored(world, A)["rationale"] == TOMBSTONE


def test_a_record_that_exists_only_in_the_archive_file_is_blanked_through_the_writer(
        world, daemon, env, retention):
    assert _forget(retention, "hypothesis", A) == 0
    row = _stored(world, A)
    _write_all(world, [r for r in _stored_all(world) if r["id"] != A])
    _write_all(world, [row], "pipeline-archive.jsonl")

    out = _sweep(env, retention)

    assert (out["erased"], out["failed"], out["pending"]) == (1, 0, 0)
    assert _stored(world, A, "pipeline-archive.jsonl")["rationale"] == TOMBSTONE
    assert not _holders(world, RATIONALE.encode("utf-8"), skip=".history")


def test_an_archive_only_copy_that_is_still_shown_is_never_blanked(world, daemon, env, retention):
    """The forget that stopped before it stamped the marker, on a record the pipeline now holds only in
    the archive file. Whichever file the writer would land on, a copy with no marker is shown."""
    daemon.fail = {FORGOTTEN_FIELD}
    assert _forget(retention, "hypothesis", A) == 1
    daemon.fail = set()
    row = _stored(world, A)
    assert not is_forgotten(row)
    _write_all(world, [r for r in _stored_all(world) if r["id"] != A])
    _write_all(world, [row], "pipeline-archive.jsonl")

    out = _sweep(env, retention)

    assert (out["erased"], out["failed"], out["world_changed"]) == (1, 0, 0)
    assert _stored(world, A, "pipeline-archive.jsonl") == row


def test_a_copy_in_the_archive_file_beside_a_live_copy_is_reported_as_residue(world, daemon, env, retention):
    """The pipeline writer reaches the live copy when both exist, and the archive is append-only,
    so the archive copy keeps its text. The sweep says so instead of calling the record erased."""
    older = _hyp(A, forgotten_at=None)
    _write_all(world, [older], "pipeline-archive.jsonl")
    assert _forget(retention, "hypothesis", A) == 0

    out = _sweep(env, retention)

    assert (out["erased"], out["failed"], out["pending"]) == (1, 0, 1)
    pending = [e for e in out["entries"] if e["action"] == "pending"]
    assert len(pending) == 1 and "cannot reach" in pending[0]["detail"]
    assert _stored(world, A)["rationale"] == TOMBSTONE
    assert _stored(world, A, "pipeline-archive.jsonl")["rationale"] == RATIONALE, "the residue is real"


def test_a_field_name_the_writer_refuses_is_residue_not_a_retry_forever(world, daemon, env, retention):
    rows = _stored_all(world)
    rows[0]["a.b"] = "DOTTED-A text that the writer will not take"
    _write_all(world, rows)
    assert _forget(retention, "hypothesis", A) == 0

    out = _sweep(env, retention)

    assert (out["erased"], out["failed"], out["pending"]) == (1, 0, 1)
    assert _stored(world, A)["rationale"] == TOMBSTONE
    assert _stored(world, A)["a.b"].startswith("DOTTED-A"), "named residue: the writer takes no dotted name"


def test_the_applier_writes_an_emptied_container_and_reads_it_back_as_the_same_type(world, daemon):
    """The sweep empties a nested value to its own type, through the applier's one writer."""
    assert apply_mod._write_hypothesis_field(A, "premortem", {}) is True
    assert apply_mod._write_hypothesis_field(A, "tags", []) is True
    assert apply_mod._write_hypothesis_field(A, "lesson", None) is True

    now = _stored(world, A)
    assert now["premortem"] == {} and now["tags"] == [] and now["lesson"] is None
    assert apply_mod._write_hypothesis_field(A, "rationale", TOMBSTONE) is True
    assert _stored(world, A)["rationale"] == TOMBSTONE


# --- blank_plan, the shape rule -------------------------------------------------------


def _plan(record: dict) -> dict:
    return knowledge_erase.blank_plan(record, tombstone="TOMB", head="Forgotten by the member on ")


def test_prose_is_decided_by_the_shape_of_the_value():
    plan = _plan({
        "id": "2026-01-02_x", "claim": "yes", "title": "T t", "stage": "active",
        "position": "YES green", "rationale": "because 绿色 sells", "unheard_of_field": "a new sentence here",
        "ascii_id": "g-100-01", "when": "2026-10-03T03:58:32+00:00", "enum": "CONFIRMED", "one": "Never",
        "empty": "", "none": None, "n": 3, "flag": True, "ratio": 0.5,
        "premortem": {"a": "if it fails because x"}, "tags": ["a", "b"], "mixed": ["ok", "not ok"],
        "replay_metadata": {"n": 3, "last": "2026-01-01"}, "done": "Forgotten by the member on 2026-10-03.",
    })

    assert plan == {"claim": "TOMB", "title": "TOMB", "position": "TOMB", "rationale": "TOMB",
                    "unheard_of_field": "TOMB", "premortem": {}, "mixed": []}


def test_a_name_the_record_depends_on_is_never_blanked_even_when_it_reads_as_prose():
    plan = _plan({"id": "a b", "slug": "a b", "stage": "a b", "horizon": "a b", "type": "a b",
                  "category": "Home care 家", "outcome": "a b", "forgotten_at": "a b", "restored_at": "a b"})

    assert plan == {}


def test_a_blank_record_has_nothing_left_to_blank():
    once = _plan(_hyp(A))
    blanked = {**_hyp(A), **{k: ("Forgotten by the member on 2026-10-03." if v == "TOMB" else v)
                             for k, v in once.items()}}

    assert once, "positive control: the full record has prose to blank"
    assert _plan(blanked) == {}


# --- a guardrail -------------------------------------------------------------------

GUARDS = "guardrails.jsonl"
CORRECTED_RULE = "RULE-S: paint widgets green because blue paint cracks, the member said."


def _other_backend():
    return object()


def _no_backend():
    raise RuntimeError("the backend cannot be built")


def _restored_as(world: Path, item_id: str) -> dict:
    """The guardrail a member's undo added for ``item_id``: the one tagged as restoring it."""
    return next(g for g in _stored_all(world, GUARDS) if f"restores:{item_id}" in g.get("tags", []))


def test_a_forgotten_guardrail_is_erased_once_its_window_is_over(world, daemon, env, retention):
    assert _forget(retention, "guardrail", G) == 0
    retired = _stored(world, G, GUARDS)
    assert retired["status"] == "retired" and RULE in retired["rule"]
    assert _holders(world, RULE.encode("utf-8"), skip=".history"), "positive control: the rule is findable"
    assert _holders(env, _b64(RULE)), "positive control: so is the retained copy"
    other = _stored(world, H, GUARDS)

    out = _sweep(env, retention)

    assert (out["erased"], out["failed"], out["pending"], out["world_changed"]) == (1, 0, 0, 1)
    kept = _stored(world, G, GUARDS)
    assert (kept["rule"], kept["title"], kept["action_hint"], kept["trigger_condition"]) == (TOMBSTONE,) * 4
    for name in ("id", "category", "status", "created", "retirement_reason", "retirement_date",
                 "severity", "source", "tags", "when_to_use"):
        assert kept[name] == retired[name], f"{name} is identity or lifecycle and stays"
    assert _stored(world, H, GUARDS) == other, "a guardrail nobody forgot is untouched"
    assert _holders(world, RULE.encode("utf-8"), skip=".history") == []
    assert _holders(env, _b64(RULE)) == []
    assert not _record_file(retention, "guardrail", G).exists()
    receipts = _receipts(env)
    assert [(r["kind"], r["event"]) for r in receipts] == [("guardrail", "erasing"), ("guardrail", "erased")]
    assert receipts[-1]["world"] == {"fields_blanked": 4, "records": 1}
    dump = json.dumps(receipts)
    for leaked in (RULE, G, "Paint green"):
        assert leaked not in dump, "a receipt carries counts, never text or an item id"


def test_a_second_pass_over_an_erased_guardrail_changes_nothing(world, daemon, env, retention):
    assert _forget(retention, "guardrail", G) == 0
    assert _sweep(env, retention)["erased"] == 1
    before = _files(env, world)

    again = _sweep(env, retention)

    assert (again["erased"], again["failed"], again["pending"], again["entries"]) == (0, 0, 0, [])
    assert _files(env, world) == before


def test_the_guardrails_own_history_still_holds_the_text_after_the_erase(world, daemon, env, retention):
    """The residue is named, not hidden: every store write snapshots the file first, so the
    pre-forget rule survives under the world's history directory. The privacy text has to say so."""
    assert _forget(retention, "guardrail", G) == 0

    assert _sweep(env, retention)["erased"] == 1

    holders = _holders(world, RULE.encode("utf-8"))
    assert holders, "the history snapshot of the pre-forget store is real"
    assert {p.relative_to(world).parts[0] for p in holders} == {".history"}


def test_a_dry_run_blanks_nothing_and_says_it_did_not_ask_the_store_about_its_backend(
        world, daemon, env, retention):
    assert _forget(retention, "guardrail", G) == 0
    before = _files(env, world)

    out = _sweep(env, retention, apply=False)

    assert (out["would_erase"], out["erased"], out["world_changed"], out["failed"]) == (1, 0, 0, 0)
    (entry,) = out["entries"]
    assert (entry["kind"], entry["action"]) == ("guardrail", "would-erase")
    assert entry["world"] == {"fields_blanked": 4, "records": 1, "backend_unchecked": True}
    assert _files(env, world) == before


@pytest.mark.parametrize("backend", [_other_backend, _no_backend], ids=["a-merging-backend", "an-unbuildable-backend"])
def test_a_backend_that_is_not_one_box_keeps_the_rule_and_counts_pending_on_every_pass(
        world, daemon, env, retention, monkeypatch, backend):
    assert _forget(retention, "guardrail", G) == 0
    monkeypatch.setattr(store_endpoints, "get_backend", backend)
    before = _files(world)
    daemon.sets.clear()

    for _ in range(2):
        out = _sweep(env, retention)
        assert (out["pending"], out["erased"], out["failed"], out["world_changed"]) == (1, 0, 0, 0)
        assert [(e["kind"], e["action"]) for e in out["entries"]] == [("guardrail", "pending")]

    assert _files(world) == before, "nothing in the world was written"
    assert _record_file(retention, "guardrail", G).exists()
    assert not (env / knowledge_erase.RECEIPTS).exists()
    assert daemon.sets == [(G, "rule")] * 2, "the refused rule is each pass's first and only write"


def test_a_guardrail_that_is_shown_again_is_not_blanked_and_its_retained_copy_is_erased(
        world, daemon, env, retention):
    assert _forget(retention, "guardrail", G) == 0
    rows = _stored_all(world, GUARDS)
    for row in rows:
        if row["id"] == G:
            row["status"] = "active"
            del row["retirement_reason"]
    _write_all(world, rows, GUARDS)
    shown = _stored(world, G, GUARDS)

    out = _sweep(env, retention)

    assert (out["erased"], out["world_changed"], out["failed"], out["pending"]) == (1, 0, 0, 0)
    assert _stored(world, G, GUARDS) == shown and RULE in shown["rule"]
    assert not _record_file(retention, "guardrail", G).exists()


def test_a_guardrail_that_is_no_longer_in_the_store_has_nothing_to_blank(world, daemon, env, retention):
    assert _forget(retention, "guardrail", G) == 0
    _write_all(world, [r for r in _stored_all(world, GUARDS) if r["id"] != G], GUARDS)

    out = _sweep(env, retention)

    assert (out["erased"], out["world_changed"], out["failed"], out["pending"]) == (1, 0, 0, 0)


def test_a_store_that_is_not_mounted_keeps_the_retained_record_and_is_a_failure(world, daemon, env, retention):
    assert _forget(retention, "guardrail", G) == 0
    (world / GUARDS).unlink()

    out = _sweep(env, retention)

    assert (out["erased"], out["failed"], out["pending"]) == (0, 1, 0)
    assert [(e["kind"], e["action"], e["detail"]) for e in out["entries"]] == [
        ("guardrail", "failed", "guardrails_missing")]
    assert _record_file(retention, "guardrail", G).exists()


def test_a_restored_guardrail_that_is_forgotten_again_takes_every_earlier_copy_with_it(
        world, daemon, env, retention):
    assert _forget(retention, "guardrail", G) == 0
    assert _undo(retention, "guardrail", G) == 0
    again = _restored_as(world, G)
    assert again["rule"] == RULE and again["status"] == "active"
    assert _forget(retention, "guardrail", again["id"]) == 0
    assert RULE in _stored(world, G, GUARDS)["rule"], "positive control: the first copy still holds it"
    daemon.sets.clear()

    out = _sweep(env, retention)

    assert (out["erased"], out["failed"], out["pending"]) == (1, 0, 0), "the undone record is not a live one"
    for item in (G, again["id"]):
        assert _stored(world, item, GUARDS)["rule"] == TOMBSTONE, item
    assert _holders(world, RULE.encode("utf-8"), skip=".history") == []
    assert [i for i, f in daemon.sets if f == "rule"] == [G, again["id"]], \
        "the earlier copy is blanked before the record whose tag names it"


def test_a_corrected_guardrail_that_is_then_forgotten_takes_the_wording_it_replaced_with_it(
        world, daemon, env, retention):
    landed = apply_mod._write_guardrail(export_mod, world, G, _stored(world, G, GUARDS), CORRECTED_RULE)
    successor = landed["superseded_by"]
    assert _stored(world, G, GUARDS)["rule"] == RULE, "positive control: the old wording is still in the store"
    assert _forget(retention, "guardrail", successor) == 0

    out = _sweep(env, retention)

    assert (out["erased"], out["failed"], out["pending"]) == (1, 0, 0)
    for item in (G, successor):
        assert _stored(world, item, GUARDS)["rule"] == TOMBSTONE, item
    assert _stored(world, G, GUARDS)["retirement_reason"] == apply_mod._SUPERSEDED_REASON.format(new_id=successor), \
        "the framework's sentence holds no text, and is the link a later pass would walk"
    for text in (RULE, CORRECTED_RULE):
        assert _holders(world, text.encode("utf-8"), skip=".history") == [], text


def test_a_tag_that_names_a_record_which_does_not_agree_blanks_nothing_there(world, daemon, env, retention):
    forgotten_elsewhere = f"Forgotten by the member on {TODAY}."
    others = [
        _guard("guard-4", rule="RULE-K: retired by something else", status="retired",
               retirement_reason="duplicate of guard-9", title="K title"),
        _guard("guard-5", rule="RULE-L: corrected into another record", status="retired", title="L title",
               retirement_reason=apply_mod._SUPERSEDED_REASON.format(new_id="guard-9")),
        _guard("guard-6", rule="RULE-M: forgotten, and never restored into this one", status="retired",
               retirement_reason=forgotten_elsewhere, title="M title"),
    ]
    _write_all(world, _stored_all(world, GUARDS) + others, GUARDS)
    assert _forget(retention, "guardrail", G) == 0
    rows = _stored_all(world, GUARDS)
    for row in rows:
        if row["id"] == H:
            row["retirement_reason"] = forgotten_elsewhere  # active, with a marker left over: only its status refuses it
        if row["id"] == G:
            row["tags"] = ["paint", "restores:guard-2", "supersedes:guard-2", "restores:guard-4",
                           "supersedes:guard-4", "supersedes:guard-5", "supersedes:guard-6", "restores:guard-9"]
    _write_all(world, rows, GUARDS)
    untouched = {r["id"]: r for r in rows if r["id"] != G}

    out = _sweep(env, retention)

    assert (out["erased"], out["failed"]) == (1, 0)
    assert _stored(world, G, GUARDS)["rule"] == TOMBSTONE
    for item_id, before in untouched.items():
        assert _stored(world, item_id, GUARDS) == before, f"{item_id} does not agree it was replaced or restored"


def test_a_lineage_longer_than_the_walk_allows_is_a_failure_and_blanks_nothing(world, daemon, env, retention):
    chain = [_guard(f"guard-{n}", rule=f"RULE-{n}: one link of a long chain",
                    **({"status": "retired", "retirement_reason": f"Forgotten by the member on {TODAY}."}
                       if n < 141 else {}),
                    tags=[f"restores:guard-{n - 1}"] if n > 101 else ["paint"])
             for n in range(101, 142)]
    _write_all(world, _stored_all(world, GUARDS) + chain, GUARDS)
    assert _forget(retention, "guardrail", "guard-141") == 0
    before = _stored_all(world, GUARDS)

    out = _sweep(env, retention)

    assert (out["erased"], out["failed"], out["pending"]) == (0, 1, 0)
    assert [(e["kind"], e["action"], e["detail"]) for e in out["entries"]] == [
        ("guardrail", "failed", "lineage_too_deep")]
    assert _stored_all(world, GUARDS) == before
    assert _record_file(retention, "guardrail", "guard-141").exists()


def test_a_field_that_fails_keeps_the_retained_record_and_the_next_pass_finishes(world, daemon, env, retention):
    assert _forget(retention, "guardrail", G) == 0
    daemon.fail_guard = {"title"}

    first = _sweep(env, retention)

    assert (first["erased"], first["failed"], first["world_changed"]) == (0, 1, 0)
    assert [(e["kind"], e["action"], e["detail"]) for e in first["entries"]] == [
        ("guardrail", "failed", "blank_failed")]
    half = _stored(world, G, GUARDS)
    assert half["rule"] == TOMBSTONE and half["title"] == "Paint green", \
        "the rule goes first, and the field that failed still holds its text"
    assert _record_file(retention, "guardrail", G).exists()

    daemon.fail_guard = set()
    second = _sweep(env, retention)

    assert (second["erased"], second["failed"], second["world_changed"]) == (1, 0, 1)
    assert _stored(world, G, GUARDS)["title"] == TOMBSTONE


def test_a_failed_earlier_copy_leaves_the_record_that_names_it_untouched(world, daemon, env, retention):
    assert _forget(retention, "guardrail", G) == 0
    assert _undo(retention, "guardrail", G) == 0
    again = _restored_as(world, G)
    assert _forget(retention, "guardrail", again["id"]) == 0
    successor_before = _stored(world, again["id"], GUARDS)
    daemon.sets.clear()
    daemon.fail_guard = {"rule"}

    out = _sweep(env, retention)

    assert (out["erased"], out["failed"]) == (0, 1)
    assert {i for i, _ in daemon.sets} == {G}, "the walk stopped at the first record that failed"
    assert _stored(world, again["id"], GUARDS) == successor_before
    assert _record_file(retention, "guardrail", again["id"]).exists()


def test_a_write_the_store_acknowledges_and_did_not_make_is_a_failure(world, daemon, env, retention):
    assert _forget(retention, "guardrail", G) == 0
    daemon.lie_guard = {"rule"}

    out = _sweep(env, retention)

    assert (out["erased"], out["failed"], out["pending"]) == (0, 1, 0)
    assert RULE in _stored(world, G, GUARDS)["rule"]
    assert _record_file(retention, "guardrail", G).exists()


def test_a_writer_that_reports_a_guardrail_write_it_did_not_make_keeps_the_retained_record(
        world, daemon, env, retention, monkeypatch):
    """The sweep does not take the applier's word for a write: it reads the store again, and a record
    that still holds text keeps the retained record and fails the pass."""
    assert _forget(retention, "guardrail", G) == 0
    real = apply_mod._write_guardrail_field
    monkeypatch.setattr(apply_mod, "_write_guardrail_field", lambda *a, **k: "ok")

    out = _sweep(env, retention)

    assert (out["erased"], out["failed"]) == (0, 1)
    assert out["entries"][0]["detail"] == "text_remains"
    assert _record_file(retention, "guardrail", G).exists()
    assert RULE in _stored(world, G, GUARDS)["rule"], "positive control: nothing was written"
    monkeypatch.setattr(apply_mod, "_write_guardrail_field", real)
    assert _sweep(env, retention)["erased"] == 1, "the next pass, with a writer that writes, finishes it"
    assert _stored(world, G, GUARDS)["rule"] == TOMBSTONE


def test_a_forgotten_one_word_rule_is_still_blanked(world, daemon, env, retention):
    """A rule of identifier characters only reads as an id by shape, so the statement field is
    blanked by name as well."""
    rows = _stored_all(world, GUARDS)
    for row in rows:
        if row["id"] == G:
            row["rule"] = "paint-widgets-green"
    _write_all(world, rows, GUARDS)
    assert _forget(retention, "guardrail", G) == 0

    assert _sweep(env, retention)["erased"] == 1

    assert _stored(world, G, GUARDS)["rule"] == TOMBSTONE


def test_a_name_the_writer_would_land_on_a_neighbour_is_never_written(world, daemon, env, retention):
    assert _forget(retention, "guardrail", G) == 0
    rows = _stored_all(world, GUARDS)
    for row in rows:
        if row["id"] == G:
            row["action_hint "] = "PADDED prose that the member forgot"
    _write_all(world, rows, GUARDS)
    before = _files(world)
    daemon.sets.clear()

    out = _sweep(env, retention)

    assert (out["erased"], out["failed"]) == (0, 1)
    assert [e["detail"] for e in out["entries"]] == ["unwritable_name"]
    assert daemon.sets == [] and _files(world) == before, "nothing was written, so no neighbour was overwritten"


def test_a_records_tags_are_written_last_and_its_rule_first(world, daemon, env, retention):
    assert _forget(retention, "guardrail", G) == 0
    rows = _stored_all(world, GUARDS)
    for row in rows:
        if row["id"] == G:
            row["tags"] = ["paint", "a tag that is a whole sentence"]
            row["when_to_use"] = {"conditions": ["when the member paints widgets"], "category": "acme"}
    _write_all(world, rows, GUARDS)
    daemon.sets.clear()

    assert _sweep(env, retention)["erased"] == 1

    fields = [f for i, f in daemon.sets if i == G]
    assert fields[0] == "rule" and fields[-1] == "tags"
    assert {"title", "action_hint", "trigger_condition", "when_to_use"} <= set(fields)
    kept = _stored(world, G, GUARDS)
    assert kept["tags"] == [], "a container that holds text is emptied to its own type"
    assert "paints widgets" not in json.dumps(kept), \
        "and the store gives an emptied mapping its default shape on the next write, which holds no text"


def test_the_guardrail_writer_tells_a_refused_erase_from_a_failed_write(
        world, daemon, retention, monkeypatch):
    assert _forget(retention, "guardrail", G) == 0
    write = apply_mod._write_guardrail_field

    assert write(G, "rule", TOMBSTONE) == "ok"
    assert write(H, "rule", TOMBSTONE) == "failed", "an active record is not erased"
    assert write(G, "created", "2099-01-01T00:00:00") == "failed", "only the statement has an erase mode"
    daemon.lie_guard = {"title"}
    assert write(G, "title", TOMBSTONE) == "failed", "a 200 that did not land is not a write"
    daemon.lie_guard = set()
    monkeypatch.setattr(store_endpoints, "get_backend", _other_backend)
    assert write(G, "rule", TOMBSTONE) == "not_local"


# --- residue in the retention directory -----------------------------------------------


def _aged(path: Path, seconds_before_now: float, text: str, now=LATER) -> Path:
    """Write ``text`` to ``path`` and make it ``seconds_before_now`` old as of ``now``."""
    path.write_text(text, encoding="utf-8")
    stamp = now.timestamp() - seconds_before_now
    os.utime(path, (stamp, stamp))
    return path


def _named(kind: str, digit: str, suffix: str = ".json") -> str:
    """A name the retention writer could have given: ``<kind>-<16 hex digits><suffix>``."""
    return f"{kind}-{digit * 16}{suffix}"


def test_write_residue_and_an_unreadable_record_past_their_age_are_purged(env, retention, world):
    retention.mkdir(parents=True)
    held = "RESIDUE-TEXT member words that a half-written record holds"
    held_json = json.dumps({"content_b64": _b64(held).decode()})
    old_tmp = _aged(retention / _named("node", "a", ".json.tmp"), 7200, held_json)
    old_bad = _aged(retention / _named("hypothesis", "b"), 31 * 86400,
                    '{"schema": 1, "kind": "hypothesis", "content_b64": "' + _b64(held).decode())
    young_tmp = _aged(retention / _named("node", "c", ".json.tmp"), 60, "young write in progress")
    young_bad = _aged(retention / _named("node", "d"), 86400, "{not json")
    # A kind the unreadable-record guard does not name (a node or a guardrail would be kept by that
    # guard anyway), so only the schema check can be what keeps this one.
    newer = _aged(retention / _named("hypothesis", "e"), 40 * 86400,
                  json.dumps({"schema": 2, "kind": "hypothesis", "item_id": "x",
                              "content_b64": _b64(held).decode()}))
    # Old, unreadable and holding text, so age and shape alone would purge them. Neither carries a
    # name the retention writer gives, and that is what says they are not the sweep's to delete.
    by_hand = [_aged(retention / "operator-notes.json", 40 * 86400, held_json),
               _aged(retention / "node-abc.json.tmp", 7200, held_json)]

    assert {p.name for p in _holders(env, _b64(held))} == {
        old_tmp.name, old_bad.name, newer.name, *(p.name for p in by_hand)}

    out = _sweep(env, retention)

    assert out["erased"] == 2 and out["failed"] == 0 and out["pending"] == 1
    assert not old_tmp.exists() and not old_bad.exists()
    assert young_tmp.exists() and young_bad.exists(), "young files may be a writer's, or a record about to age out"
    assert newer.exists(), "a record of a newer schema is unknown, not unreadable: it is kept"
    assert all(p.exists() for p in by_hand), "a file the retention writer would not have named is left alone"
    erased = [e for e in out["entries"] if e["action"] == "erased"]
    assert len(erased) == 2 and all(re.fullmatch(r"[0-9a-f]{16}", e["record"]) for e in erased), \
        "an erase is named by its receipt's id, and the file name is a digest of an item id"
    assert {e["record"]: e["action"] for e in out["entries"] if e["action"] != "erased"} == {
        newer.name: "pending"}, "a record that is kept is named by its file, and is not counted as erased"
    receipts = _receipts(env)
    assert [r["kind"] for r in receipts] == ["residue"] * 4
    assert {p.name for p in _holders(env, _b64(held))} == {newer.name, *(p.name for p in by_hand)}
    assert _b64(held) not in (env / knowledge_erase.RECEIPTS).read_bytes()


def test_the_residue_name_pattern_matches_every_name_the_retention_writer_gives():
    for kind in ("node", "hypothesis", "guardrail"):
        name = knowledge_retention.record_name(kind, "an item id")
        assert knowledge_erase._RECORD_NAME.fullmatch(name), name
        assert knowledge_erase._RECORD_NAME.fullmatch(name + ".tmp"), "the temp file its write goes through"
    for foreign in ("operator-notes.json", "node-abc.json", "node-" + "A" * 16 + ".json",
                    "node-" + "a" * 17 + ".json", "node-" + "a" * 16 + ".json.bak"):
        assert knowledge_erase._RECORD_NAME.fullmatch(foreign) is None, foreign


def test_an_undone_record_holds_no_text_and_is_left_alone(world, daemon, env, retention):
    assert _forget(retention, "node", NODE_KEY) == 0
    assert _undo(retention, "node", NODE_KEY) == 0
    marker = _record_file(retention, "node", NODE_KEY).read_bytes()
    assert not knowledge_retention.is_live(_retained(retention, "node", NODE_KEY))

    out = _sweep(env, retention)

    assert out["entries"] == [] and out["erased"] == 0
    assert _record_file(retention, "node", NODE_KEY).read_bytes() == marker


# --- the receipts and the delete --------------------------------------------------------


def test_the_receipts_hold_no_text_no_item_id_no_file_name_and_no_digest_of_the_text(
        world, daemon, env, retention):
    assert _forget(retention, "node", NODE_KEY) == 0
    assert _forget(retention, "hypothesis", A) == 0
    records = {"node": _retained(retention, "node", NODE_KEY), "hypothesis": _retained(retention, "hypothesis", A)}
    names = {k: _record_file(retention, k, i).name for k, i in (("node", NODE_KEY), ("hypothesis", A))}

    assert _sweep(env, retention)["erased"] == 2

    path = env / knowledge_erase.RECEIPTS
    raw = path.read_bytes()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    lines = _receipts(env)
    assert [(r["kind"], r["event"]) for r in lines] == [
        ("node", "erasing"), ("node", "erased"), ("hypothesis", "erasing"), ("hypothesis", "erased")]
    allowed = {"schema", "event", "at", "kind", "id", "forgotten_at", "undo_until", "bytes", "world", "restore"}
    ids: dict = {}
    for line in lines:
        assert set(line) <= allowed, set(line) - allowed
        assert re.fullmatch(r"[0-9a-f]{16}", line["id"]), "a random id, not derived from the record"
        ids.setdefault(line["kind"], set()).add(line["id"])
        assert line["bytes"] == len(knowledge_retention.retained_content(records[line["kind"]]))
        assert ("restore" in line) == (line["event"] == "erased")
    assert [len(v) for v in ids.values()] == [1, 1], "the write-ahead line and its closing line share one id"
    assert ids["node"] != ids["hypothesis"]
    for name in names.values():
        digest = name.split("-", 1)[1].split(".")[0]
        assert name.encode("ascii") not in raw and digest.encode("ascii") not in raw, \
            "the file name is a digest of the item id, and a guess confirms it"
    for forbidden in (NODE_TEXT, CLAIM, TITLE, RATIONALE, POSITION, NODE_KEY, A):
        assert forbidden.encode("utf-8") not in raw, forbidden
    for kind, record in records.items():
        assert record["sha256"].encode("ascii") not in raw, "a digest of short text confirms a guess"
        assert record["content_b64"].encode("ascii") not in raw


def test_the_delete_goes_through_the_storage_backends_own_delete(world, env, retention, monkeypatch):
    assert _forget(retention, "node", NODE_KEY) == 0
    seen = []
    real = storage_backend.LocalBackend.delete

    def spy(self, path):
        seen.append(Path(path))
        return real(self, path)

    monkeypatch.setattr(storage_backend.LocalBackend, "delete", spy)

    assert _sweep(env, retention)["erased"] == 1

    assert seen == [_record_file(retention, "node", NODE_KEY)]


def test_a_delete_that_leaves_the_file_behind_is_a_failure_not_an_erase(world, env, retention, monkeypatch):
    assert _forget(retention, "node", NODE_KEY) == 0

    def stuck(self, path):
        raise OSError(f"delete incomplete for {path}: still present locally")

    monkeypatch.setattr(storage_backend.LocalBackend, "delete", stuck)

    out = _sweep(env, retention)

    assert (out["erased"], out["failed"]) == (0, 1)
    assert out["entries"][0]["action"] == "failed" and "OSError" in out["entries"][0]["detail"]
    assert [r["event"] for r in _receipts(env)] == ["erasing"], "the intent is on record, the erase is not"


def test_no_delete_happens_when_the_write_ahead_receipt_cannot_be_written(world, env, retention, monkeypatch):
    assert _forget(retention, "node", NODE_KEY) == 0

    def refuse(env_dir, line):
        raise OSError("disk full")

    monkeypatch.setattr(knowledge_erase, "_receipt", refuse)

    out = _sweep(env, retention)

    assert (out["erased"], out["failed"]) == (0, 1)
    assert _record_file(retention, "node", NODE_KEY).exists(), "no receipt, no delete"


def test_an_erase_whose_closing_receipt_fails_is_counted_as_both(world, env, retention, monkeypatch):
    assert _forget(retention, "node", NODE_KEY) == 0
    real = knowledge_erase._receipt
    calls = []

    def second_fails(env_dir, line):
        calls.append(line["event"])
        if line["event"] == "erased":
            raise OSError("disk full")
        real(env_dir, line)

    monkeypatch.setattr(knowledge_erase, "_receipt", second_fails)

    out = _sweep(env, retention)

    assert (out["erased"], out["failed"]) == (1, 1)
    assert not _record_file(retention, "node", NODE_KEY).exists()
    assert calls == ["erasing", "erased"]
    assert [r["event"] for r in _receipts(env)] == ["erasing"], "visible as an erasing with no erased"
    assert "closing receipt" in out["entries"][0]["detail"]


def test_a_record_that_changed_while_the_sweep_ran_is_not_deleted(world, env, retention, monkeypatch):
    """The decision was made on a snapshot (guard-3881): a forget written again after the sweep
    looked at the record has a new window, and must not be deleted by the old verdict."""
    assert _forget(retention, "node", NODE_KEY) == 0
    path = _record_file(retention, "node", NODE_KEY)
    real = knowledge_erase._settle_node

    def settle_then_reforget(*args, **kwargs):
        verdict = real(*args, **kwargs)
        record = knowledge_retention.read_record(path)
        record["forgotten_at"] = (LATER + datetime.timedelta(days=1)).replace(microsecond=0).isoformat()
        record["undo_until"] = (LATER + datetime.timedelta(days=31)).replace(microsecond=0).isoformat()
        path.write_bytes(json.dumps(record, sort_keys=True).encode("utf-8"))
        return verdict

    monkeypatch.setattr(knowledge_erase, "_settle_node", settle_then_reforget)

    out = _sweep(env, retention)

    assert out["erased"] == 0 and [e["action"] for e in out["entries"]] == ["skipped"]
    assert path.exists()
    assert not (env / knowledge_erase.RECEIPTS).exists(), "no intent was written for a record that moved"


# --- a dry run, a second pass, the applier -------------------------------------------------


def test_a_dry_run_changes_nothing_and_says_what_it_would_do(world, daemon, env, retention):
    assert _forget(retention, "node", NODE_KEY) == 0
    assert _forget(retention, "hypothesis", A) == 0
    before = _files(env, world)

    out = _sweep(env, retention, apply=False)

    assert (out["would_erase"], out["erased"], out["failed"], out["dry_run"]) == (2, 0, 0, True)
    assert {e["action"] for e in out["entries"]} == {"would-erase"}
    assert _files(env, world) == before
    assert not (env / knowledge_erase.RECEIPTS).exists()


def test_a_dry_run_that_would_blank_the_world_counts_no_world_change(world, daemon, env, retention, monkeypatch):
    """A node whose page blank never ran and a marker whose text was never blanked both owe the world a
    write. A dry run names each in its entries, makes neither, and counts neither as a change."""
    real = apply_mod._replace_file
    monkeypatch.setattr(apply_mod, "_replace_file", lambda path, data: False)
    assert _forget(retention, "node", NODE_KEY) == 0
    monkeypatch.setattr(apply_mod, "_replace_file", real)
    rows = _stored_all(world)
    rows[0][FORGOTTEN_FIELD] = (NOW - datetime.timedelta(days=40)).replace(microsecond=0).isoformat()
    _write_all(world, rows)
    before = _files(env, world)

    out = _sweep(env, retention, apply=False)

    assert (out["would_erase"], out["erased"], out["failed"], out["world_changed"]) == (1, 0, 0, 0)
    assert sorted(e["action"] for e in out["entries"]) == ["would-blank", "would-erase"]
    assert {e["record"] for e in out["entries"] if e["action"] == "would-blank"} == {"-"}, \
        "a record with no file is not named by a digest of its id"
    assert _files(env, world) == before
    assert not (env / knowledge_erase.RECEIPTS).exists()


def test_a_second_pass_finds_nothing_left_to_do(world, daemon, env, retention):
    assert _forget(retention, "node", NODE_KEY) == 0
    assert _forget(retention, "hypothesis", A) == 0
    assert _sweep(env, retention)["erased"] == 2
    settled = _files(env, world)

    again = _sweep(env, retention)

    assert (again["erased"], again["failed"], again["pending"], again["world_changed"]) == (0, 0, 0, 0)
    assert again["entries"] == []
    assert _files(env, world) == settled


def test_a_missing_retention_directory_is_an_empty_pass(env, retention, world):
    assert not retention.exists()

    out = _sweep(env, retention)

    assert out == {"erased": 0, "world_changed": 0, "pending": 0, "failed": 0, "would_erase": 0,
                   "dry_run": False, "entries": []}


def test_the_applier_offers_every_name_the_sweep_calls():
    assert [n for n in knowledge_erase.APPLIER_NAMES if not hasattr(apply_mod, n)] == []


def test_an_applier_that_lacks_a_name_is_refused_by_that_name_before_any_write(world, daemon, env, retention):
    assert _forget(retention, "node", NODE_KEY) == 0
    stub = SimpleNamespace(**{n: getattr(apply_mod, n) for n in knowledge_erase.APPLIER_NAMES
                              if n != "_replace_file"})
    before = _files(env, world)

    with pytest.raises(knowledge_erase.EraseError, match="_replace_file"):
        _sweep(env, retention, applier=stub)

    assert _files(env, world) == before


def test_the_entries_name_an_erase_by_its_random_id_and_never_by_the_item_or_a_digest_of_it(
        world, daemon, env, retention):
    assert _forget(retention, "node", NODE_KEY) == 0
    assert _forget(retention, "hypothesis", A) == 0
    rows = _stored_all(world)
    rows[1][FORGOTTEN_FIELD] = (NOW - datetime.timedelta(days=40)).replace(microsecond=0).isoformat()
    _write_all(world, rows)  # B is marked with no retained record: an entry of the other shape

    out = _sweep(env, retention)

    dump = json.dumps(out)
    for forbidden in (NODE_KEY, A, B, NODE_TEXT, CLAIM, RATIONALE, "CONTROL-B"):
        assert forbidden not in dump, forbidden
    for kind, item in (("node", NODE_KEY), ("hypothesis", A), ("hypothesis", B)):
        assert knowledge_retention.record_name(kind, item) not in dump, "a digest of the id confirms a guess"
    named = sorted((e["kind"], e["action"], "id" if re.fullmatch(r"[0-9a-f]{16}", e["record"]) else e["record"])
                   for e in out["entries"])
    assert named == [("hypothesis", "blanked", "-"), ("hypothesis", "erased", "id"), ("node", "erased", "id")]


# --- what the review found ( u4): each case below lost text, or lost a count -------------


def _tree_page(world: Path) -> Path:
    return world / "knowledge" / "tree" / "acme" / "acme-widgets.md"


def _index_file(world: Path) -> Path:
    return world / "knowledge" / "tree" / "_tree.yaml"


def _forget_node_without_blanking(retention: Path, monkeypatch) -> None:
    """A forget that retained the page and the index entry went, but stopped before the blank."""
    real = apply_mod._replace_file
    monkeypatch.setattr(apply_mod, "_replace_file", lambda path, data: False)
    assert _forget(retention, "node", NODE_KEY) == 0
    monkeypatch.setattr(apply_mod, "_replace_file", real)


def _register(world: Path, key: str, page: str) -> None:
    """Add an index entry ``key`` resolving to ``page`` (a path under the world's tree)."""
    index = yaml.safe_load(_index_file(world).read_text(encoding="utf-8"))
    index["nodes"][key] = {"file": page, "summary": "Re-registered.", "last_updated": "2026-03-01"}
    _index_file(world).write_text(yaml.safe_dump(index, sort_keys=False), encoding="utf-8")


def test_a_page_that_only_differs_in_its_line_endings_is_pending_and_never_erased_over(
        world, env, retention, monkeypatch):
    _forget_node_without_blanking(retention, monkeypatch)
    page = _tree_page(world)
    page.write_bytes(NODE_PAGE.replace("\n", "\r\n").encode("utf-8"))

    first = _sweep(env, retention)
    second = _sweep(env, retention)

    for out in (first, second):
        assert (out["erased"], out["failed"], out["pending"], out["world_changed"]) == (0, 0, 1, 0)
        assert [e["action"] for e in out["entries"]] == ["pending"]
    assert NODE_TEXT.encode("utf-8") in page.read_bytes(), "positive control: the page still holds the text"
    assert _record_file(retention, "node", NODE_KEY).exists(), "the retained copy is what a person compares with"


def test_a_page_nothing_shows_is_blanked_even_when_its_key_was_re_created_at_another_path(
        world, env, retention, monkeypatch):
    _forget_node_without_blanking(retention, monkeypatch)
    tree = world / "knowledge" / "tree"
    (tree / "sales").mkdir()
    fresh = tree / "sales" / "acme-widgets.md"
    fresh.write_text("---\ntopic: Acme widgets\n---\n\nA new page.\n", encoding="utf-8")
    _register(world, NODE_KEY, "world/knowledge/tree/sales/acme-widgets.md")

    out = _sweep(env, retention)

    assert (out["erased"], out["failed"], out["world_changed"]) == (1, 0, 1)
    assert NODE_TEXT.encode("utf-8") not in _tree_page(world).read_bytes(), "the orphan page held the text"
    assert fresh.read_text(encoding="utf-8").endswith("A new page.\n"), "the live page is never touched"
    assert NODE_KEY in yaml.safe_load(_index_file(world).read_text(encoding="utf-8"))["nodes"]


def test_a_page_another_key_shows_is_left_alone_and_the_record_is_erased_as_inert(
        world, env, retention, monkeypatch):
    _forget_node_without_blanking(retention, monkeypatch)
    _register(world, "acme-widgets-v2", "world/knowledge/tree/acme/acme-widgets.md")

    out = _sweep(env, retention)

    assert (out["erased"], out["failed"], out["world_changed"]) == (1, 0, 0)
    assert _tree_page(world).read_text(encoding="utf-8") == NODE_PAGE, "a live node is never blanked"
    assert "acme-widgets-v2" in yaml.safe_load(_index_file(world).read_text(encoding="utf-8"))["nodes"]


def test_a_missing_index_keeps_the_record_and_the_next_pass_finishes(world, env, retention, monkeypatch):
    _forget_node_without_blanking(retention, monkeypatch)
    index = _index_file(world)
    moved = index.with_name("moved-away")
    index.rename(moved)

    first = _sweep(env, retention)

    assert (first["erased"], first["failed"], first["world_changed"]) == (0, 1, 0)
    assert [e["detail"] for e in first["entries"]] == ["tree_unavailable"]
    assert NODE_TEXT.encode("utf-8") in _tree_page(world).read_bytes()
    assert _record_file(retention, "node", NODE_KEY).exists()
    moved.rename(index)
    again = _sweep(env, retention)
    assert (again["erased"], again["failed"], again["world_changed"]) == (1, 0, 1)
    assert NODE_TEXT.encode("utf-8") not in _tree_page(world).read_bytes()


def test_a_missing_pipeline_keeps_every_retained_hypothesis_and_the_next_pass_finishes(
        world, daemon, env, retention):
    assert _forget(retention, "hypothesis", A) == 0
    live = world / "pipeline.jsonl"
    moved = world / "pipeline.moved"
    live.rename(moved)

    first = _sweep(env, retention)

    assert (first["erased"], first["failed"]) == (0, 1)
    assert [e["detail"] for e in first["entries"]] == ["pipeline_missing"]
    assert _record_file(retention, "hypothesis", A).exists(), "no pipeline is not a pipeline with no text in it"
    moved.rename(live)
    again = _sweep(env, retention)
    assert (again["erased"], again["failed"], again["world_changed"]) == (1, 0, 1)
    assert _stored(world, A)["rationale"].startswith("Forgotten by the member on ")


@pytest.mark.parametrize("kind,key", [("node", NODE_KEY), ("guardrail", G)])
def test_an_unreadable_record_that_names_a_node_or_a_guardrail_is_kept_and_counted_pending(
        world, daemon, env, retention, kind, key):
    """Its world side (a page to blank, a rule to blank) cannot be checked without it."""
    assert _forget(retention, kind, key) == 0
    path = _record_file(retention, kind, key)
    record = json.loads(path.read_bytes())
    record["sha256"] = "0" * 64
    path.write_bytes(json.dumps(record, sort_keys=True).encode("utf-8"))
    assert knowledge_retention.read_record(path) is None, "positive control: it no longer reads"

    out = _sweep(env, retention)

    assert (out["erased"], out["failed"], out["pending"]) == (0, 0, 1)
    assert [(e["kind"], e["action"]) for e in out["entries"]] == [("residue", "pending")]
    assert path.exists()


def test_a_padded_field_name_is_never_written_and_is_counted_as_residue(world, daemon, env, retention):
    """The pipeline writer strips a field name, so a write to ``"category "`` would land on
    ``category``, which is the member's own scoping."""
    rows = _stored_all(world)
    rows[0]["category "] = "PADDED prose that the member forgot"
    _write_all(world, rows)
    assert _forget(retention, "hypothesis", A) == 0

    out = _sweep(env, retention)

    assert (out["erased"], out["failed"], out["pending"]) == (1, 0, 1)
    kept = _stored(world, A)
    assert kept["category"] == "acme", "the neighbouring name is untouched"
    assert kept["category "].startswith("PADDED"), "and the residue is real, so it is counted, not hidden"
    assert [e["action"] for e in out["entries"] if e["action"] != "erased"] == ["pending"]


def test_one_records_failure_neither_hides_the_others_nor_their_counts(
        world, daemon, env, retention, monkeypatch):
    assert _forget(retention, "node", NODE_KEY) == 0
    assert _forget(retention, "guardrail", G) == 0
    assert _forget(retention, "hypothesis", A) == 0
    real = knowledge_erase._settle_other

    def node_raises(applier, export, tree_dir, env_dir, kind, *rest, **kwargs):
        if kind == "node":
            raise ValueError(f"{NODE_TEXT} in {NODE_KEY}: a message that quotes the text")
        return real(applier, export, tree_dir, env_dir, kind, *rest, **kwargs)

    monkeypatch.setattr(knowledge_erase, "_settle_other", node_raises)

    out = _sweep(env, retention)

    assert (out["erased"], out["failed"], out["pending"]) == (2, 1, 0)
    assert sorted((e["kind"], e["action"]) for e in out["entries"]) == [
        ("guardrail", "erased"), ("hypothesis", "erased"), ("node", "failed")]
    (failed,) = [e for e in out["entries"] if e["action"] == "failed"]
    assert failed["detail"].startswith("ValueError at "), "its type and where it was raised"
    dump = json.dumps(out)
    for leaked in (NODE_TEXT, NODE_KEY, "quotes the text"):
        assert leaked not in dump, "an exception's message can carry the text, so it is never reported"
    assert _record_file(retention, "node", NODE_KEY).exists()


def test_a_pipeline_that_cannot_be_scanned_fails_the_hypotheses_and_not_the_nodes(
        world, daemon, env, retention, monkeypatch):
    assert _forget(retention, "node", NODE_KEY) == 0
    assert _forget(retention, "hypothesis", A) == 0

    def cannot(export, world_dir):
        raise ValueError("the pipeline is not readable")

    monkeypatch.setattr(knowledge_erase, "_scan", cannot)

    out = _sweep(env, retention)

    assert (out["erased"], out["failed"]) == (1, 1)
    assert sorted((e["kind"], e["action"], e["record"] == "-") for e in out["entries"] if e["action"] == "failed") == [
        ("hypothesis", "failed", True)]
    assert _record_file(retention, "hypothesis", A).exists()
    assert not _record_file(retention, "node", NODE_KEY).exists()


def test_a_marker_stamped_so_far_ahead_that_its_window_cannot_be_added_up_stops_nothing(
        world, daemon, env, retention):
    assert _forget(retention, "node", NODE_KEY) == 0
    assert _forget(retention, "hypothesis", A) == 0
    poison = _control("2026-01-09_poison", "P", forgotten_at="9999-12-31T00:00:00")
    _write_all(world, _stored_all(world) + [poison])
    before = _stored(world, "2026-01-09_poison")

    first = _sweep(env, retention)
    second = _sweep(env, retention)

    assert (first["erased"], first["failed"], first["pending"]) == (2, 0, 0)
    assert (second["erased"], second["failed"], second["pending"]) == (0, 0, 0)
    assert _stored(world, "2026-01-09_poison") == before, "a window that never closes is never settled"
    assert knowledge_erase._window_closed("9999-12-31T00:00:00", LATER) is False
    assert knowledge_erase._window_closed("not a stamp", LATER) is True


def test_an_export_the_sweep_was_not_written_against_is_refused_by_name_before_any_delete(
        world, daemon, env, retention, monkeypatch):
    assert _forget(retention, "node", NODE_KEY) == 0
    assert _forget(retention, "hypothesis", A) == 0
    real = apply_mod._load_export_mod()
    drifted = SimpleNamespace(**{n: getattr(real, n) for n in dir(real)
                                 if not n.startswith("__") and n != "_read_jsonl"})
    monkeypatch.setattr(apply_mod, "_load_export_mod", lambda: drifted)
    before = _files(env, world)

    with pytest.raises(knowledge_erase.EraseError, match="_read_jsonl"):
        _sweep(env, retention)

    assert _files(env, world) == before, "found before the first delete, so nothing is half done"


def test_a_world_that_cannot_be_found_ends_the_pass_before_any_file_is_touched(
        world, env, retention, monkeypatch):
    retention.mkdir(parents=True)
    stale = _aged(retention / _named("node", "a", ".json.tmp"), 7200, "text that a later pass would purge")
    monkeypatch.delenv("WORLD_PATH", raising=False)
    monkeypatch.delenv("MIND_WORLD", raising=False)

    with pytest.raises(knowledge_erase.EraseError, match="WORLD_PATH is not set") as raised:
        _sweep(env, retention)

    assert raised.value.__cause__ is None, "the export's own exit, and its message, are not passed on"
    assert stale.exists(), "the world is resolved before the residue is, so an unset world purges nothing"
    assert not (env / knowledge_erase.RECEIPTS).exists()


def test_two_sweeps_that_meet_on_one_file_write_one_erased_and_one_gone(world, env, retention, monkeypatch):
    assert _forget(retention, "node", NODE_KEY) == 0
    real = knowledge_erase._receipt
    state = {"nested": False, "inner": None}

    def receipt(env_dir, line):
        real(env_dir, line)
        if line["event"] == "erasing" and not state["nested"]:
            state["nested"] = True  # a second sweep runs between this one's re-check and its delete
            state["inner"] = _sweep(env, retention)

    monkeypatch.setattr(knowledge_erase, "_receipt", receipt)

    outer = _sweep(env, retention)

    assert state["inner"]["erased"] == 1
    assert (outer["erased"], outer["failed"]) == (0, 0), "the file was already gone: one erase, counted once"
    assert [e["action"] for e in outer["entries"]] == ["skipped"]
    lines = _receipts(env)
    assert [r["event"] for r in lines] == ["erasing", "erasing", "erased", "gone"]
    assert lines[0]["id"] == lines[3]["id"] and lines[1]["id"] == lines[2]["id"]
    assert lines[0]["id"] != lines[1]["id"], "each erase is its own receipt, so the two cannot be told as one"


def test_a_pass_shows_its_caller_it_is_alive_before_every_record_and_every_write(
        world, daemon, env, retention, monkeypatch):
    assert _forget(retention, "node", NODE_KEY) == 0
    assert _forget(retention, "hypothesis", A) == 0
    ticks = []
    at_write = []
    real = apply_mod._write_hypothesis_field

    def counted(*args, **kwargs):
        at_write.append(len(ticks))
        return real(*args, **kwargs)

    monkeypatch.setattr(apply_mod, "_write_hypothesis_field", counted)

    out = knowledge_erase.sweep(env, retention, apply_mod, apply=True, now=LATER, tick=lambda: ticks.append(1))

    assert out["erased"] == 2
    assert len(at_write) >= 4, "positive control: the hypothesis took several daemon writes"
    assert all(later > earlier for earlier, later in zip(at_write, at_write[1:])), \
        "a tick falls between every two writes, since each can take as long as the lock's staleness"
    assert at_write[0] >= 1, "and before the first"
    # One tick per file in the directory (2), one before the node is settled, one before the marked
    # hypothesis, and one before each of its writes: a record that does no daemon write still shows
    # the caller it is alive, since the node's page and index writes can wait on a lock.
    assert len(ticks) == 2 + 1 + 1 + len(at_write)


def test_a_receipt_cannot_confirm_a_guessed_item_id(world, daemon, env, retention):
    assert _forget(retention, "hypothesis", A) == 0
    out = _sweep(env, retention)
    assert out["erased"] == 1
    raw = (env / knowledge_erase.RECEIPTS).read_bytes()

    for guess in (A, "2026-01-02_blue-widgets", "2026-01-02_red-widgets"):
        name = knowledge_retention.record_name("hypothesis", guess)
        assert name.encode("ascii") not in raw, "a file name is a digest of the id, and a guess confirms it"
        assert name not in json.dumps(out)
        assert guess.encode("utf-8") not in raw


def test_a_sentence_that_only_starts_like_the_marker_is_prose():
    head = "Forgotten by the member on "

    plan = _plan({"rationale": head + "2026-01-01. Real reason: blue paint cracks.",
                  "position": head + "Tuesday", "lesson": head + "2026-01-01.",
                  "claim": head + "Tuesday: green widgets outsell blue"})

    assert plan == {"rationale": "TOMB", "position": "TOMB", "claim": "TOMB"}, \
        "only the whole marker, the head and a date and a full stop, reads as already blank"


def test_a_mapping_keyed_by_a_sentence_is_emptied_whatever_its_values():
    plan = _plan({"evidence": {"widgets sell better in green than in blue": 0.7},
                  "scores": {"north": 3}, "nested": {"a": {"widgets sell better": 1}},
                  "plain": [["a", "b"], {"x": 1}]})

    assert plan == {"evidence": {}, "nested": {}}


def test_text_of_only_identifier_characters_stays_whatever_its_length_and_that_is_the_rules_limit():
    plan = _plan({"ref": "g" * 300, "lesson": "green-widgets-sell-better-than-blue-in-every-market",
                  "source": "https://acme.example/blog/green-widgets-outsell-blue",
                  "when": "2026-03-01 12:00:00", "stamp": "2026-03-01T12:00:00+00:00", "day": "2026-03-01"})

    assert plan == {}, "a hyphenated sentence and a bare URL pass as ids, and a date with a time is lifecycle"


def test_the_statement_fields_are_blanked_whatever_they_hold_unless_already_blank():
    head = "Forgotten by the member on "
    fields = knowledge_erase.item_text_fields("hypothesis")

    assert fields, "positive control: the projection names statement fields"
    assert _plan({name: "yes" for name in fields}) == {name: "TOMB" for name in fields}
    assert _plan({name: head + "2026-01-01." for name in fields}) == {}
    assert _plan({name: None for name in fields}) == {}
    assert _plan({name: "" for name in fields}) == {}


def test_the_markers_the_sweep_recognises_are_built_from_the_appliers_own_templates(world, env, retention):
    today = apply_mod._today()
    text = apply_mod._FORGOTTEN_TEXT.format(today=today)
    head = apply_mod._FORGOTTEN_HEAD
    assert text.startswith(head)
    assert knowledge_erase._is_marker(text, head), "what the applier writes into a record reads as blank"
    assert not knowledge_erase._is_marker(text + " More words.", head)

    page = apply_mod._TOMBSTONE.format(today=today).encode("utf-8")
    pattern = knowledge_erase._page_marker(apply_mod)
    assert pattern.fullmatch(page) is not None
    assert pattern.fullmatch(page + b"and then text") is None
    assert pattern.fullmatch(page.replace(today.encode("ascii"), b"not-a-date")) is None
    assert _forget(retention, "node", NODE_KEY) == 0
    assert pattern.fullmatch(_tree_page(world).read_bytes()) is not None, \
        "positive control: the page a real forget leaves is what the pattern calls blank"


def test_a_marked_record_stored_with_a_padded_id_cannot_be_written_and_is_a_failure_never_an_erase(
        world, daemon, env, retention):
    """The writer strips the id it is asked for and matches the stored one exactly, so a record stored
    with a padded id is out of its reach. The sweep says so on every pass and blanks nothing."""
    retention.mkdir()  # a pass over an environment with no retention directory is an empty pass
    rows = _stored_all(world)
    rows[0]["id"] = f" {A} "
    rows[0][FORGOTTEN_FIELD] = (NOW - datetime.timedelta(days=40)).replace(microsecond=0).isoformat()
    _write_all(world, rows)
    before = _files(env, world)

    first = _sweep(env, retention)
    second = _sweep(env, retention)

    for out in (first, second):
        assert (out["erased"], out["failed"], out["world_changed"]) == (0, 1, 0)
        assert [(e["kind"], e["action"], e["detail"]) for e in out["entries"]] == [
            ("hypothesis", "failed", "blank_failed")]
    assert _files(env, world) == before, "nothing was written, so nothing can have been blanked half way"
    assert {path for path, _ in daemon.writes} == {"/v1/pipeline/update-field"}, \
        "the writer was asked and refused (a failure, not a skip), and nothing else was called"


def test_a_forgotten_record_whose_world_copy_was_padded_keeps_its_retained_record(
        world, daemon, env, retention):
    """The retained record is the only other copy of the text. A world copy the writer cannot reach is
    a copy the sweep cannot show is blank, so the retained record is kept, never erased over it."""
    assert _forget(retention, "hypothesis", A) == 0
    rows = _stored_all(world)
    next(r for r in rows if r["id"] == A)["id"] = f" {A} "
    _write_all(world, rows)
    daemon.writes.clear()
    before = _files(env, world)

    first = _sweep(env, retention)
    second = _sweep(env, retention)

    for out in (first, second):
        assert (out["erased"], out["failed"], out["world_changed"]) == (0, 1, 0)
        assert [(e["kind"], e["action"], e["detail"]) for e in out["entries"]] == [
            ("hypothesis", "failed", "blank_failed")]
    assert _files(env, world) == before
    assert {path for path, _ in daemon.writes} == {"/v1/pipeline/update-field"}, "the writer was asked and refused"
    assert _retained(retention, "hypothesis", A) is not None
    assert not (env / knowledge_erase.RECEIPTS).exists(), "a failure is not an erase, so no receipt names it"
