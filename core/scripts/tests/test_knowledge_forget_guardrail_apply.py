"""Tests for the soft forget and the undo of a guardrail (, unit u3a).

The guardrail leg of ``knowledge-edit-apply.py``. A guardrail's ``rule`` is immutable by design
(guard-6210: the store's merge keys a record on ``created`` and the whole rule, so a rule edited
in place forks the record at the next cross-box merge), which shapes both operations:

* FORGET retains the rule outside the world, then RETIRES the guardrail with a reason that starts
  "Forgotten by the member". The export and retrieval publish active guardrails only, so a retired
  one is hidden from both, and the store's merge keeps a retirement (retired dominates), so a stale
  copy cannot bring it back. The rule is NOT blanked: the retired record keeps it for the erase.
* UNDO cannot reactivate that record, for the same reason a stale copy cannot: retired is terminal
  in the merge. The rule comes back as a NEW guardrail, the way a member's correction lands.

The store is not faked. ``_GuardStore`` stands in for the daemon transport only: every call goes
through the REAL ``store.append`` and ``store.set_field`` handlers, in process, against the world's
own guardrails file, so the spec's defaults, validator, immutable fields and amendment stamps are
the ones production runs. A fake that only recorded calls let a dropped required field pass every
test, which is why the older edit tests carry a hand-written contract; this file does not need one.
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import re
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
import coordination_merge  # noqa: E402
import knowledge_retention  # noqa: E402
from knowledge_projection import is_active_guardrail, item_handle  # noqa: E402
from mind_api.src.endpoints import store as store_endpoints  # noqa: E402


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


apply_mod = _load("knowledge_edit_apply_forget_guard_tests", "knowledge-edit-apply.py")
export_mod = _load("knowledge_export_forget_guard_tests", "knowledge-export.py")

SECRET = "handle-secret-for-tests"
ENV_ID = "env-under-test"
TODAY = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
REASON = f"Forgotten by the member on {TODAY}."

RULE = "Paint widgets green: blue paint cracks on the second coat."
OTHER_RULE = "Ship gears in wooden crates, never in cardboard."

G = "guard-1"      # the guardrail these tests forget
H = "guard-2"      # the control: exposed, and nothing here touches it
RETIRED = "guard-3"  # already retired by the framework, so never exposed

STORE = "/v1/store/append"
SET_FIELD = "/v1/store/set-field"


def _guard(item_id: str, **extra) -> dict:
    rec = {"id": item_id, "category": "acme", "rule": RULE, "status": "active",
           "created": "2026-01-01T00:00:00", "source": "g-100-01",
           "trigger_condition": "when painting widgets",
           "tags": ["paint", "supersedes:guard-0"],
           "when_to_use": {"conditions": ["painting"], "category": "acme"},
           "title": "Paint green", "action_hint": "Run the paint check.", "severity": "HIGH"}
    rec.update(extra)
    return rec


def _records() -> list[dict]:
    return [
        _guard(G),
        _guard(H, rule=OTHER_RULE, title="Crate gears", trigger_condition="when shipping gears",
               tags=["ship"], action_hint="Check the crate."),
        _guard(RETIRED, rule="An old rule the framework retired.", status="retired",
               retirement_reason="superseded by guard-9", retirement_date="2026-01-05"),
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
    (w / "pipeline.jsonl").write_text("", encoding="utf-8")
    (w / "guardrails.jsonl").write_text("".join(json.dumps(r) + "\n" for r in _records()),
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


class _GuardStore:
    """Stands in for the daemon transport of the guardrails store's append and set-field.

    Every call that is let through runs the real handler against the world's guardrails file.
    ``fail`` names ``(path, field)`` pairs whose write raises, as a daemon 500 does, BEFORE
    anything is written (an append has field ``None``). ``lie`` names pairs the daemon
    acknowledges with a 200 and does not write. ``on_call`` sees each call's 1-based index and
    params before it is served, so a test can look at the disk at the moment a write is about
    to happen.
    """

    def __init__(self, world, fail=(), lie=(), on_call=None):
        self.world = world
        self.fail = set(fail)
        self.lie = set(lie)
        self.on_call = on_call
        self.calls = []   # (path, field) per call
        self.bodies = []  # the JSON body of each call, or None

    def __call__(self, method, path, query=None, body=None, headers=None):
        assert method == "POST" and path in (STORE, SET_FIELD), (method, path)
        assert query is None or not re.search(r"\s", query), "a request line cannot carry whitespace"
        params = {k: v[0] for k, v in urllib.parse.parse_qs(query, keep_blank_values=True).items()}
        key = (path, params.get("field"))
        self.calls.append(key)
        self.bodies.append(json.loads(body) if body else None)
        if self.on_call is not None:
            self.on_call(len(self.calls), params)
        if key in self.fail:
            raise _rt.RtError("daemon HTTP 500")
        if key in self.lie:
            lied = _stored(self.world, params["id"]) if path == SET_FIELD else \
                {"id": "guard-99", "rule": "Something that was never written.", "status": "active"}
            return json.dumps({"ok": True, "record": lied})
        ctx = SimpleNamespace(query=params, paths=SimpleNamespace(world=self.world),
                              headers={"x-mind-agent": "alpha"},
                              body=body.encode("utf-8") if body else b"")
        resp = (store_endpoints.append if path == STORE else store_endpoints.set_field)(ctx)
        if resp.status >= 400:
            raise _rt.RtError(f"daemon HTTP {resp.status}: {resp.body.decode('utf-8')}")
        return resp.body.decode("utf-8")

    def fields(self) -> list:
        return [field for _path, field in self.calls]


@pytest.fixture
def guards(world, monkeypatch):
    daemon = _GuardStore(world)
    monkeypatch.setattr(_rt, "rt_call", daemon)
    return daemon


def _store_path(world: Path) -> Path:
    return world / "guardrails.jsonl"


def _stored_all(world: Path) -> list[dict]:
    return [json.loads(ln) for ln in _store_path(world).read_text(encoding="utf-8").splitlines() if ln]


def _stored(world: Path, item_id: str) -> dict:
    return next(r for r in _stored_all(world) if r["id"] == item_id)


def _rewrite(world: Path, item_id: str, **changes) -> None:
    """Change a stored record directly, as a write from another box would. A value of the
    ``_GONE`` sentinel drops the key."""
    rows = _stored_all(world)
    for row in rows:
        if row["id"] == item_id:
            for key, value in changes.items():
                if value is _GONE:
                    row.pop(key, None)
                else:
                    row[key] = value
    _store_path(world).write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


_GONE = object()


def _handle(item_id: str) -> str:
    return item_handle("guardrail", item_id, SECRET, ENV_ID)


def _forget(retention: Path, item_id: str = G, *, apply: bool = True, report: dict | None = None) -> int:
    argv = ["--handle", _handle(item_id), "--op", "forget", f"--retention-dir={retention}"]
    return apply_mod.main(argv + (["--apply"] if apply else []), report)


def _undo(retention: Path, item_id: str = G, *, report: dict | None = None) -> int:
    return apply_mod.main(["--handle", _handle(item_id), "--op", "undo",
                           f"--retention-dir={retention}", "--apply"], report)


def _retained(retention: Path, item_id: str = G) -> dict | None:
    return knowledge_retention.read_record(knowledge_retention.record_path(retention, "guardrail", item_id))


def _published_handles(world: Path) -> set:
    bundle = export_mod.build_bundle(world, _ROOT, extra_paths=(), env=None)
    return {row.get("handle") for row in bundle.guardrails}


def _bytes(*roots: Path) -> dict:
    return {p: p.read_bytes() for r in roots if r.exists() for p in r.rglob("*") if p.is_file()}


def _restorers(world: Path, item_id: str = G) -> list[dict]:
    """The active guardrails that carry the tag an undo of ``item_id`` puts on its successor."""
    tag = f"restores:{item_id}"
    return [r for r in _stored_all(world) if is_active_guardrail(r) and tag in (r.get("tags") or [])]


def _forged(retention: Path, *, rule: bytes = RULE.encode("utf-8"), restore=None, item_id: str = G):
    """Replace the retained record with one whose digest is VALID and whose body is not what a
    forget writes. The digest covers the rule bytes only, so it cannot vouch for ``restore``."""
    restore = {"carried": {"category": "acme", "trigger_condition": "when painting widgets"},
               "tags": ["paint"]} if restore is None else restore
    record = knowledge_retention.build_record("guardrail", item_id, rule,
                                              now=knowledge_retention.now_utc(), restore=restore)
    knowledge_retention.write_record(retention, record)


# --- forget -------------------------------------------------------------------


def test_forget_hides_the_guardrail_keeps_one_retained_copy_and_leaves_the_rule_in_the_retired_record(
        world, retention, guards, capsys):
    before = _stored_all(world)
    assert {_handle(G), _handle(H)} <= _published_handles(world), "both start exposed"
    assert _handle(RETIRED) not in _published_handles(world), "a retired guardrail was never exposed"
    report = {}

    rc = _forget(retention, report=report)

    assert rc == 0, capsys.readouterr().err
    kept = _retained(retention)
    assert report == {"kind": "guardrail", "id": G, "op": "forget", "applied": True,
                      "forgotten": True, "undo_until": kept["undo_until"], "blanked": False}
    # Hidden from what the member and the resident read; the control is not.
    published = _published_handles(world)
    assert _handle(G) not in published
    assert _handle(H) in published, "positive control: an unforgotten guardrail is still shown"
    assert export_mod.resolve_item(world, _handle(G)) is None
    assert export_mod.resolve_item(world, _handle(H))[1] == H
    # The record stays, retired, with the reason a forget writes. The rule is NOT blanked: it is
    # immutable, and the report says so with blanked False.
    now = _stored(world, G)
    assert [r["id"] for r in _stored_all(world)] == [r["id"] for r in before], "no record was added"
    assert (now["status"], now["retirement_reason"], now["retirement_date"]) == ("retired", REASON, TODAY)
    assert now["rule"] == RULE
    assert (now["title"], now["action_hint"]) == ("Paint green", "Run the paint check.")
    assert [r for r in _stored_all(world) if r["id"] != G] == [r for r in before if r["id"] != G], \
        "no other record was touched"
    # One retained copy, outside the world: the rule, and what an undo needs to add it back.
    assert [p.name for p in retention.iterdir()] == [knowledge_retention.record_name("guardrail", G)]
    assert knowledge_retention.retained_content(kept) == RULE.encode("utf-8")
    assert kept["restore"] == {
        "carried": {"category": "acme", "trigger_condition": "when painting widgets",
                    "when_to_use": {"conditions": ["painting"], "category": "acme"}},
        "tags": ["paint", "supersedes:guard-0"]}
    assert knowledge_retention.in_window(kept, knowledge_retention.now_utc())


def test_the_retirement_is_written_reason_then_date_then_status_last(world, retention, guards):
    """A stop part way leaves the guardrail published, never retired without its reason."""
    assert _forget(retention) == 0

    assert guards.calls == [(SET_FIELD, "retirement_reason"), (SET_FIELD, "retirement_date"),
                            (SET_FIELD, "status")]


def test_a_retired_guardrail_cannot_be_forgotten_or_edited_again(world, retention, guards):
    assert _forget(retention) == 0
    stores = _bytes(world, retention)

    for op, extra in (("forget", []), ("edit", ["--text", "A replacement rule long enough."])):
        report = {}
        rc = apply_mod.main(["--handle", _handle(G), "--op", op, f"--retention-dir={retention}",
                             "--apply", *extra], report)
        assert (rc, report) == (3, {"refused": "not_addressable"}), op
    assert _bytes(world, retention) == stores


def test_a_dry_run_forget_writes_nothing(world, retention, guards, capsys):
    stores = _bytes(world)

    rc = _forget(retention, apply=False)

    assert rc == 0
    assert "dry run" in capsys.readouterr().out
    assert _bytes(world) == stores
    assert not retention.exists()
    assert guards.calls == []


def test_forget_without_a_retention_directory_is_refused_before_any_write(world, guards):
    stores = _bytes(world)
    report = {}

    rc = apply_mod.main(["--handle", _handle(G), "--op", "forget", "--apply"], report)

    assert (rc, report) == (3, {"refused": "no_retention_store"})
    assert _bytes(world) == stores
    assert guards.calls == []


def test_the_rule_is_retained_and_read_back_before_the_first_write(world, retention, monkeypatch):
    """guard-6223: a recovery layer that was not verified is not a recovery layer. The disk is
    looked at when the FIRST write is about to happen."""
    seen = {}

    def at_first_write(index, params):
        if index == 1:
            seen["field"] = params["field"]
            seen["retained"] = _retained(retention)
            seen["stored"] = _stored(world, G)

    monkeypatch.setattr(_rt, "rt_call", _GuardStore(world, on_call=at_first_write))

    assert _forget(retention) == 0

    assert seen["field"] == "retirement_reason"
    assert knowledge_retention.retained_content(seen["retained"]) == RULE.encode("utf-8")
    assert seen["stored"]["status"] == "active" and "retirement_reason" not in seen["stored"], \
        "nothing was retired yet"


@pytest.mark.parametrize("failure", [OSError("disk full"), ValueError("did not read back as written")])
def test_a_retained_copy_that_is_not_there_stops_the_forget_before_it_changes_anything(
        world, retention, guards, monkeypatch, capsys, failure):
    stores = _bytes(world)

    def boom(*_a, **_k):
        raise failure

    monkeypatch.setattr(knowledge_retention, "write_record", boom)

    assert _forget(retention) == 1
    assert "its rule was not retained" in capsys.readouterr().err
    assert _bytes(world) == stores
    assert guards.calls == []


def test_a_status_that_does_not_land_leaves_the_guardrail_shown_and_the_retained_copy_inert(
        world, retention, monkeypatch, capsys):
    daemon = _GuardStore(world, fail={(SET_FIELD, "status")})
    monkeypatch.setattr(_rt, "rt_call", daemon)

    assert _forget(retention) == 1

    assert "write failed for guardrail" in capsys.readouterr().err
    assert _handle(G) in _published_handles(world), "still shown: the forget did not happen"
    assert _stored(world, G)["status"] == "active"
    assert daemon.fields() == ["retirement_reason", "retirement_date", "status"]
    assert knowledge_retention.retained_content(_retained(retention)) == RULE.encode("utf-8")
    # Inert: an undo of it is refused, and a retry of the forget completes.
    report = {}
    assert _undo(retention, report=report) == 3
    assert report == {"refused": "undo_conflict"}
    daemon.fail.clear()
    assert _forget(retention) == 0
    assert _handle(G) not in _published_handles(world)
    assert _stored(world, G)["status"] == "retired"


def test_a_daemon_that_acknowledges_a_status_it_did_not_write_is_not_believed(
        world, retention, monkeypatch, capsys):
    """A 2xx is not evidence the value landed. The record the endpoint returns is read back."""
    daemon = _GuardStore(world, lie={(SET_FIELD, "status")})
    monkeypatch.setattr(_rt, "rt_call", daemon)

    assert _forget(retention) == 1

    assert "did not read back as retired" in capsys.readouterr().err
    assert _stored(world, G)["status"] == "active"
    assert _handle(G) in _published_handles(world)


@pytest.mark.parametrize("change", [{"status": "retired"}, {"rule": "A rule somebody swapped in underneath."}])
def test_a_guardrail_that_changed_while_its_rule_was_retained_is_not_forgotten(
        world, retention, guards, monkeypatch, capsys, change):
    """guard-3881: the decision was made on a snapshot. A retained copy of a rule that is no
    longer the active one is not a copy of what the member forgot."""
    real = knowledge_retention.write_record

    def write_then_change(retention_dir, record):
        path = real(retention_dir, record)
        _rewrite(world, G, **change)
        return path

    monkeypatch.setattr(knowledge_retention, "write_record", write_then_change)

    assert _forget(retention) == 1

    assert "it changed while its rule was retained" in capsys.readouterr().err
    assert guards.calls == []


def test_a_guardrail_the_store_could_not_add_back_is_refused_before_anything_is_written(
        world, retention, guards):
    """A forget that cannot be undone is not the soft forget that was ruled. An undo adds the rule
    back as a new record, and the store requires a trigger condition for an add."""
    _rewrite(world, G, trigger_condition=_GONE)
    stores = _bytes(world)
    report = {}

    rc = _forget(retention, report=report)

    assert (rc, report) == (3, {"refused": "not_restorable"})
    assert _bytes(world) == stores
    assert not retention.exists()
    assert guards.calls == []


@pytest.mark.parametrize("field, value", [
    ("trigger_condition", ["when painting"]),
    ("trigger_condition", 7),
    ("phases", "plan"),
    ("context_triggers", {"finishing": True}),
    ("trigger_pattern", 5),
    ("when_to_use", ["painting"]),
])
def test_a_guardrail_holding_a_carried_field_in_a_type_the_store_never_has_is_refused_before_a_write(
        world, retention, guards, field, value):
    """A forget that an undo would refuse to take back is not the soft forget that was ruled."""
    _rewrite(world, G, **{field: value})
    stores = _bytes(world)
    report = {}

    rc = _forget(retention, report=report)

    assert (rc, report) == (3, {"refused": "not_restorable"})
    assert _bytes(world) == stores
    assert not retention.exists()
    assert guards.calls == []


def test_a_guardrail_with_no_category_is_never_exposed_so_it_is_never_addressed(
        world, retention, guards):
    """The other field an add requires is the one the export keys exposure on, so a record that
    lacks it cannot be handed to the applier in the first place."""
    _rewrite(world, G, category=_GONE)
    assert _handle(G) not in _published_handles(world)
    stores = _bytes(world)
    report = {}

    rc = _forget(retention, report=report)

    assert (rc, report) == (3, {"refused": "not_addressable"})
    assert _bytes(world) == stores
    assert guards.calls == []


def test_the_forget_refusal_reads_the_fields_by_presence_and_the_rule_by_content():
    ok = _guard(G)
    assert apply_mod._guardrail_forget_refusal(ok) is None
    assert apply_mod._guardrail_forget_refusal({**ok, "trigger_condition": ""}) is None, \
        "present but empty is still the old rule's: the store tests presence"
    assert apply_mod._guardrail_forget_refusal({k: v for k, v in ok.items() if k != "trigger_condition"}) == "not_restorable"
    assert apply_mod._guardrail_forget_refusal({**ok, "rule": ""}) == "not_addressable"
    assert apply_mod._guardrail_forget_refusal({**ok, "rule": ["not", "text"]}) == "not_addressable"
    assert apply_mod._guardrail_forget_refusal({**ok, "category": ["acme"]}) == "not_restorable"
    assert apply_mod._guardrail_forget_refusal({**ok, "trigger_pattern": None}) is None, \
        "null is what the live store holds here on 7,063 of 7,097"
    assert apply_mod._guardrail_forget_refusal({**ok, "trigger_pattern": "paint"}) is None


def test_the_carried_type_table_covers_exactly_the_fields_a_forget_carries():
    assert set(apply_mod._CARRIED_TYPES) == set(apply_mod._SUCCESSOR_CARRIES)
    assert apply_mod._carried_typed({"made_up": "x"}) is False, "a field with no row is refused"
    assert apply_mod._carried_typed({}) is True


# --- undo ---------------------------------------------------------------------


def test_undo_adds_the_retained_rule_back_as_a_new_guardrail_and_leaves_the_retired_one_alone(
        world, retention, guards, capsys):
    assert _forget(retention) == 0
    retired = _stored(world, G)
    known_ids = {r["id"] for r in _stored_all(world)}
    guards.calls.clear()
    guards.bodies.clear()
    report = {}

    rc = _undo(retention, report=report)

    assert rc == 0, capsys.readouterr().err
    new = next(r for r in _stored_all(world) if r["id"] not in known_ids)
    assert re.fullmatch(r"guard-\d+", new["id"])
    assert report == {"kind": "guardrail", "id": G, "op": "undo", "applied": True, "restored": True,
                      "restored_as": new["id"], "handle": _handle(new["id"])}
    # The rule is back, active, with WHEN it applies. What restated the rule and the resident's own
    # rating are not carried, exactly as for a correction, and the old supersedes tag is dropped.
    assert (new["rule"], new["status"], new["source"]) == (RULE, "active", "member-edit")
    assert new["category"] == "acme" and new["trigger_condition"] == "when painting widgets"
    assert new["when_to_use"] == {"conditions": ["painting"], "category": "acme"}
    assert new["tags"] == ["paint", "member-edit", f"restores:{G}"]
    assert not {"title", "action_hint", "severity"} & set(new)
    # One add, past the near-duplicate refusal (the retired record holds this very rule).
    assert guards.calls == [(STORE, None)] and guards.bodies[0]["allow_near_dup"] is True
    # Shown again, under a NEW handle; the old handle stays dead and its record is untouched.
    published = _published_handles(world)
    assert _handle(new["id"]) in published and _handle(G) not in published
    assert export_mod.resolve_item(world, _handle(new["id"]))[1] == new["id"]
    assert _stored(world, G) == retired
    # The retained copy is spent: the rule is held in exactly one place outside the store again.
    kept = _retained(retention)
    assert kept is not None and not knowledge_retention.is_live(kept)
    assert RULE.encode("utf-8") not in (retention / knowledge_retention.record_name("guardrail", G)).read_bytes()
    assert len(_restorers(world)) == 1


@pytest.mark.parametrize("pattern", [None, "paint(ing)?"])
def test_a_live_shaped_guardrail_comes_back_with_every_field_that_says_when_it_applies(
        world, retention, guards, pattern):
    """The live store holds odd shapes: a text when_to_use, a trigger_pattern that is null on 7,063
    of 7,097 and a string on 34, list-valued context_triggers and phases. The real add handler
    accepts each one the undo carries."""
    shapes = {"when_to_use": "Use when painting.", "trigger_pattern": pattern,
              "context_triggers": ["finishing"], "phases": ["plan", "execute"]}
    _rewrite(world, G, **shapes)
    assert _forget(retention) == 0

    report = {}
    assert _undo(retention, report=report) == 0

    new = _stored(world, report["restored_as"])
    for name, value in shapes.items():
        assert new[name] == value, name


def test_an_undo_survives_a_retained_copy_that_could_not_be_cleared_and_a_retry_adds_no_second_copy(
        world, retention, guards, monkeypatch, capsys):
    """The restore is the write that matters. If marking the record spent fails, the same undo can
    run again, and it reuses the guardrail it already added instead of adding another."""
    assert _forget(retention) == 0
    real = knowledge_retention.mark_undone
    monkeypatch.setattr(knowledge_retention, "mark_undone",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    first = {}

    assert _undo(retention, report=first) == 0

    assert "restored, but its retained copy was not cleared" in capsys.readouterr().err
    assert knowledge_retention.is_live(_retained(retention)), "still live: the undo can run again"
    assert len(_restorers(world)) == 1
    monkeypatch.setattr(knowledge_retention, "mark_undone", real)
    guards.calls.clear()
    second = {}

    assert _undo(retention, report=second) == 0

    assert guards.calls == [], "nothing was added again"
    assert len(_restorers(world)) == 1
    assert second["restored_as"] == first["restored_as"]
    assert not knowledge_retention.is_live(_retained(retention))


def test_a_second_undo_after_a_successful_one_is_refused_and_adds_nothing(world, retention, guards):
    assert _forget(retention) == 0
    assert _undo(retention) == 0
    stores = _bytes(world, retention)
    guards.calls.clear()
    report = {}

    rc = _undo(retention, report=report)

    assert (rc, report) == (3, {"refused": "not_addressable"})
    assert _bytes(world, retention) == stores
    assert guards.calls == []
    assert len(_restorers(world)) == 1


def test_a_guardrail_tagged_as_a_restore_of_this_one_but_holding_another_rule_is_not_taken_for_it(
        world, retention, guards):
    """The reuse that makes an undo safe to run again is for THIS rule. A member can rewrite a
    restored guardrail, and the rewrite inherits the tag, so a tag alone must not stand in for
    the rule the member is asking to get back."""
    assert _forget(retention) == 0
    rewritten = _guard("guard-50", rule="A rule the member rewrote after an earlier restore.",
                       tags=[f"restores:{G}", "member-edit"])
    with _store_path(world).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rewritten) + "\n")
    report = {}

    assert _undo(retention, report=report) == 0

    added = _stored(world, report["restored_as"])
    assert added["id"] != "guard-50" and added["rule"] == RULE
    assert _stored(world, "guard-50") == rewritten, "the member's rewrite was not touched"


def test_an_add_that_fails_leaves_the_store_alone_the_retained_copy_live_and_a_retry_works(
        world, retention, monkeypatch, capsys):
    daemon = _GuardStore(world)
    monkeypatch.setattr(_rt, "rt_call", daemon)
    assert _forget(retention) == 0
    daemon.fail = {(STORE, None)}
    stores = _bytes(world)

    assert _undo(retention) == 1

    assert "write failed for guardrail" in capsys.readouterr().err
    assert _bytes(world) == stores
    assert knowledge_retention.is_live(_retained(retention))
    daemon.fail.clear()
    assert _undo(retention) == 0
    assert len(_restorers(world)) == 1


def test_a_daemon_that_acknowledges_an_add_it_did_not_write_is_not_believed(
        world, retention, monkeypatch, capsys):
    daemon = _GuardStore(world)
    monkeypatch.setattr(_rt, "rt_call", daemon)
    assert _forget(retention) == 0
    daemon.lie = {(STORE, None)}

    assert _undo(retention) == 1

    assert "did not read back as written" in capsys.readouterr().err
    assert _restorers(world) == []
    assert knowledge_retention.is_live(_retained(retention))


def test_an_undo_attempted_between_the_retained_copy_and_the_retirement_is_refused_as_inert(
        world, retention, monkeypatch):
    """The forget-versus-undo race that cannot be closed for a hypothesis is benign here: until
    the status lands the guardrail is still active, so the undo has nothing to put back, and the
    forget that was in flight completes."""
    attempts = []

    def undo_before_the_first_write(index, params):
        if index == 1 and not attempts:
            report = {}
            attempts.append((_undo(retention, report=report), report))

    daemon = _GuardStore(world, on_call=undo_before_the_first_write)
    monkeypatch.setattr(_rt, "rt_call", daemon)

    assert _forget(retention) == 0

    assert attempts == [(3, {"refused": "undo_conflict"})]
    assert _stored(world, G)["status"] == "retired"
    assert _handle(G) not in _published_handles(world)
    assert knowledge_retention.is_live(_retained(retention))


def test_an_undo_after_the_window_is_refused_and_writes_nothing(world, retention, guards):
    assert _forget(retention) == 0
    path = retention / knowledge_retention.record_name("guardrail", G)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["undo_until"] = "2020-01-01T00:00:00+00:00"
    path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    stores = _bytes(world, retention)
    guards.calls.clear()
    report = {}

    rc = _undo(retention, report=report)

    assert (rc, report) == (3, {"refused": "undo_expired"})
    assert _bytes(world, retention) == stores
    assert guards.calls == []


@pytest.mark.parametrize("change", [
    # Retired for another cause since the forget: restoring would put back a rule something
    # retired on purpose.
    {"retirement_reason": "superseded by guard-9: a member corrected this rule"},
    # Not retired: a forget that stopped before the status landed, or a status something cleared.
    {"status": "active"},
    # Not the rule that was retained.
    {"rule": "A rule that is not the one the member forgot."},
])
def test_an_undo_of_a_guardrail_that_is_not_the_one_a_forget_retired_is_refused(
        world, retention, guards, change):
    assert _forget(retention) == 0
    _rewrite(world, G, **change)
    stores = _bytes(world, retention)
    guards.calls.clear()
    report = {}

    rc = _undo(retention, report=report)

    assert (rc, report) == (3, {"refused": "undo_conflict"})
    assert _bytes(world, retention) == stores
    assert guards.calls == []


def test_an_undo_for_a_guardrail_nobody_forgot_resolves_nothing(world, retention, guards):
    report = {}

    rc = _undo(retention, H, report=report)

    assert (rc, report) == (3, {"refused": "not_addressable"})
    assert guards.calls == []


def test_an_undo_whose_live_record_is_gone_resolves_nothing(world, retention, guards):
    assert _forget(retention) == 0
    rows = [r for r in _stored_all(world) if r["id"] != G]
    _store_path(world).write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    guards.calls.clear()
    report = {}

    rc = _undo(retention, report=report)

    assert (rc, report) == (3, {"refused": "not_addressable"})
    assert guards.calls == []


def test_an_undo_in_an_unprovisioned_environment_resolves_nothing(world, retention, guards, monkeypatch):
    assert _forget(retention) == 0
    monkeypatch.delenv(export_mod._GOAL_HANDLE_SECRET_VAR)
    report = {}

    rc = apply_mod.main(["--handle", _handle(G), "--op", "undo", f"--retention-dir={retention}",
                         "--apply"], report)

    assert (rc, report) == (3, {"refused": "not_addressable"})


@pytest.mark.parametrize("kwargs, reason", [
    # A record that names a field the member was never shown would turn the undo into an
    # arbitrary write against the store. Its digest is valid: the digest covers the rule only.
    ({"restore": {"carried": {"category": "acme", "trigger_condition": "t", "status": "retired"},
                  "tags": []}}, "not_addressable"),
    ({"restore": {"carried": {"category": "acme", "trigger_condition": "t", "severity": "LOW"},
                  "tags": []}}, "not_addressable"),
    ({"restore": {"carried": ["category"], "tags": []}}, "not_addressable"),
    # The store checks which keys an add carries and never what they hold, so a field in a type
    # the live store never has would be written as it stands.
    ({"restore": {"carried": {"category": ["acme"], "trigger_condition": "t"}, "tags": []}}, "not_addressable"),
    ({"restore": {"carried": {"category": "acme", "trigger_condition": 7}, "tags": []}}, "not_addressable"),
    ({"restore": {"carried": {"category": "acme", "trigger_condition": "t", "phases": "plan"}, "tags": []}}, "not_addressable"),
    ({"restore": {"carried": {"category": "acme", "trigger_condition": "t", "trigger_pattern": 5}, "tags": []}}, "not_addressable"),
    ({"restore": {"carried": {"category": "acme", "trigger_condition": "t", "when_to_use": 5}, "tags": []}}, "not_addressable"),
    ({"restore": {"carried": {"category": "acme", "trigger_condition": "t"}, "tags": "paint"}}, "not_addressable"),
    ({"restore": {"carried": {"category": "acme", "trigger_condition": "t"}, "tags": ["ok", 7]}}, "not_addressable"),
    ({"restore": {"tags": []}}, "not_addressable"),
    ({"restore": {}}, "not_addressable"),
    ({"rule": b""}, "not_addressable"),
    ({"rule": b"\xff\xfe not utf-8"}, "not_addressable"),
    # A rule the store could not add back: this is a refusal the member can be told about.
    ({"restore": {"carried": {"category": "acme"}, "tags": []}}, "not_restorable"),
    ({"restore": {"carried": {"trigger_condition": "t"}, "tags": []}}, "not_restorable"),
])
def test_a_retained_record_that_does_not_hold_a_restorable_guardrail_is_refused_and_writes_nothing(
        world, retention, guards, kwargs, reason):
    assert _forget(retention) == 0
    _forged(retention, **kwargs)
    # The forged rule must still equal the stored one for the conflict check to be passed, or the
    # refusal would be undo_conflict for the wrong reason: restore the stored rule's bytes.
    stores = _bytes(world)
    guards.calls.clear()
    report = {}

    rc = _undo(retention, report=report)

    assert (rc, report) == (3, {"refused": reason}), report
    assert _bytes(world) == stores
    assert guards.calls == []


def test_a_forget_after_an_undo_retains_the_restored_guardrail_afresh(world, retention, guards):
    assert _forget(retention) == 0
    first = {}
    assert _undo(retention, report=first) == 0
    new_id = first["restored_as"]
    assert not knowledge_retention.is_live(_retained(retention))

    assert _forget(retention, new_id) == 0

    again = _retained(retention, new_id)
    assert knowledge_retention.is_live(again)
    assert knowledge_retention.retained_content(again) == RULE.encode("utf-8")
    assert again["restore"]["tags"] == ["paint", "member-edit", f"restores:{G}"]
    assert _stored(world, new_id)["status"] == "retired"
    assert _handle(new_id) not in _published_handles(world)
    # The first retained copy is spent and the second forget does not reopen it.
    assert not knowledge_retention.is_live(_retained(retention, G))


def test_an_undo_of_the_second_forget_does_not_inherit_the_tag_of_the_first(world, retention, guards):
    """A restored guardrail carries ``restores:<old id>``. Forgotten and restored again, its
    successor must name THIS guardrail, never the one before it, or the reuse check could take
    one guardrail's restore for another's."""
    assert _forget(retention) == 0
    first = {}
    assert _undo(retention, report=first) == 0
    assert _forget(retention, first["restored_as"]) == 0
    second = {}

    assert _undo(retention, first["restored_as"], report=second) == 0

    new = _stored(world, second["restored_as"])
    assert new["tags"] == ["paint", "member-edit", f"restores:{first['restored_as']}"]
    assert second["restored_as"] not in (first["restored_as"], G)


# --- the store's merge --------------------------------------------------------


def test_a_stale_copy_of_a_forgotten_guardrail_cannot_bring_it_back_in_either_merge_order(
        world, retention, guards):
    """What makes a forget stick across boxes is the store's own merge, not anything here: a
    retirement dominates a copy that is still active. This pins the property the guardrail forget
    relies on, in both argument orders, with a positive control that the merge CAN return active."""
    stale = _stored(world, G)
    assert _forget(retention) == 0
    gone = _stored(world, G)
    assert stale["status"] == "active" and gone["status"] == "retired"

    for left, right in ((gone, stale), (stale, gone)):
        merged = coordination_merge._merge_guard_record(left, right)
        assert merged["status"] == "retired"
        assert merged["retirement_reason"] == REASON
    control = coordination_merge._merge_guard_record(stale, dict(stale))
    assert control["status"] == "active", "positive control: two active copies merge to active"


def test_the_merge_would_retire_a_reactivated_copy_which_is_why_an_undo_adds_a_new_record(
        world, retention, guards):
    """The design constraint behind the undo. A retirement is terminal in the merge, whatever
    stamp a reactivation carries, so an undo that reactivated the retired record would be undone
    by the next merge from any box that still holds the retired copy. If this ever fails, the merge
    has learned to un-retire, and the undo can be rethought."""
    assert _forget(retention) == 0
    gone = _stored(world, G)
    revived = dict(gone, status="active",
                   amended_fields={**(gone.get("amended_fields") or {}), "status": "2999-01-01T00:00:00"})

    for left, right in ((gone, revived), (revived, gone)):
        assert coordination_merge._merge_guard_record(left, right)["status"] == "retired"


def test_a_forget_retires_the_record_in_place_so_the_merge_still_sees_one_record(
        world, retention, guards):
    """A rule edited in place forks the record at the next merge (guard-6210). A retirement writes
    other fields only, so the identity the merge keys on, (created, rule), does not move."""
    before = _stored(world, G)
    assert _forget(retention) == 0

    after = _stored(world, G)

    assert (after["created"], after["rule"]) == (before["created"], before["rule"])
    assert coordination_merge._guard_identity(after) == coordination_merge._guard_identity(before)


def test_the_guardrail_an_undo_adds_is_a_different_record_to_the_merge(world, retention, guards):
    """The merge keys a guardrail on (created, rule). The restored one carries the same rule and a
    fresh ``created``, so it is its own record and cannot be folded into the retired one."""
    assert _forget(retention) == 0
    report = {}
    assert _undo(retention, report=report) == 0

    old, new = _stored(world, G), _stored(world, report["restored_as"])

    assert new["rule"] == old["rule"], "the same rule, so only ``created`` can tell them apart"
    assert new["created"] != old["created"]
    assert coordination_merge._guard_identity(new) != coordination_merge._guard_identity(old)
