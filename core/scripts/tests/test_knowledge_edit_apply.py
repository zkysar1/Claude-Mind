"""Tests for core/scripts/knowledge-edit-apply.py ().

A synthetic world with one exposed wiki node, one too long to edit, one hypothesis and
one guardrail. Handles are minted with the same ``item_handle`` the export publishes, so
every test addresses items exactly as a member's queued edit would, and an edit's ``base`` is
the digest of the text the export's row showed (what a member's browser sends). The
hypothesis store is the daemon, so ``_rt.rt_call`` is replaced with a fake that echoes the
record.
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import os
import sys
import urllib.parse
from pathlib import Path

import pytest
import yaml

_SCRIPTS = Path(__file__).resolve().parents[1]
_ROOT = _SCRIPTS.parents[1]
for _p in (str(_SCRIPTS), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _rt  # noqa: E402
from knowledge_projection import item_handle, view_digest  # noqa: E402
from mind_api.src.store_registry import STORE_REGISTRY, apply_defaults  # noqa: E402


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


apply_mod = _load("knowledge_edit_apply_under_test", "knowledge-edit-apply.py")
export_mod = _load("knowledge_export_for_edit_tests", "knowledge-export.py")

SECRET = "handle-secret-for-tests"
ENV_ID = "env-under-test"
TODAY = datetime.datetime.now(datetime.timezone.utc).date().isoformat()

FRONT_MATTER = """topic: Acme widgets
created: 2026-01-01
# a comment the edit must keep
tags: [widgets, colour]
last_updated: 2026-01-01
last_update_trigger:
  type: tree_growth
  source: g-001-01
  session: old-session
confidence: 0.8"""
NODE_TEXT = f"---\n{FRONT_MATTER}\n---\n\n# Acme widgets\n\nWidgets are blue.\n"


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = tmp_path / "world"
    tree = w / "knowledge" / "tree"
    (tree / "acme").mkdir(parents=True)
    (tree / "acme" / "acme-widgets.md").write_text(NODE_TEXT, encoding="utf-8")
    long_body = "x" * (export_mod._NODE_BODY_CAP + 1)
    (tree / "acme" / "acme-long.md").write_text(f"---\ntopic: Long\n---\n\n{long_body}\n",
                                                encoding="utf-8")
    # The published view replaces the path with [path], so a member never sees it.
    (tree / "acme" / "acme-paths.md").write_text(
        "---\ntopic: Paths\n---\n\nThe widget spec lives in /srv/acme/spec.md.\n", encoding="utf-8")
    index = {
        "last_updated": "2026-01-01",
        "nodes": {
            "acme-widgets": {"file": "world/knowledge/tree/acme/acme-widgets.md",
                             "summary": "Widgets.", "last_updated": "2026-01-01"},
            "acme-long": {"file": "world/knowledge/tree/acme/acme-long.md",
                          "summary": "Long.", "last_updated": "2026-01-01"},
            "acme-paths": {"file": "world/knowledge/tree/acme/acme-paths.md",
                           "summary": "Paths.", "last_updated": "2026-01-01"},
        },
    }
    (tree / "_tree.yaml").write_text(yaml.safe_dump(index, sort_keys=False), encoding="utf-8")
    (w / "pipeline.jsonl").write_text("".join(json.dumps(h) + "\n" for h in (
        {"id": "hyp-acme-1", "category": "acme", "claim": "old claim", "stage": "active"},
        {"id": "hyp-acme-2", "category": "acme", "claim": "Results are kept in /srv/acme/out.",
         "stage": "active"},
    )), encoding="utf-8")
    (w / "guardrails.jsonl").write_text("".join(json.dumps(g) + "\n" for g in (
        {"id": "guard-1", "category": "acme", "rule": "old rule", "status": "active",
         "source": "g-100-01",
         "trigger_condition": "when painting widgets", "tags": ["paint", "supersedes:guard-0"],
         "when_to_use": {"conditions": ["painting"], "category": "acme"},
         "title": "Old title", "action_hint": "Run the old check.", "severity": "HIGH"},
        # The published view replaces the path with [path], so a member never sees it.
        {"id": "guard-2", "category": "acme", "rule": "Specs live in /srv/acme/spec.md.",
         "status": "active", "source": "g-100-01", "trigger_condition": "when reading specs"},
    )), encoding="utf-8")
    monkeypatch.setenv("WORLD_PATH", str(w))
    monkeypatch.setenv("KNOWLEDGE_HANDLE_SECRET", SECRET)
    monkeypatch.setenv("ENVIRONMENT_ID", ENV_ID)
    monkeypatch.setenv("MIND_SID", "sid-under-test")
    return w


def _handle(kind, item_id):
    return item_handle(kind, item_id, SECRET, ENV_ID)


def _node_path(world):
    return world / "knowledge" / "tree" / "acme" / "acme-widgets.md"


def _bundle(world, env=None):
    """The bundle the export CLI publishes: META_PATH joins the redactor's paths when set,
    as it does for the redactor the applier rebuilds."""
    meta = os.environ.get("META_PATH")
    return export_mod.build_bundle(world, _ROOT, extra_paths=(meta,) if meta else (), env=env)


def _view(row):
    """The text a member corrects: a node row's body, a hypothesis row's statement, a
    guardrail row's rule."""
    for field in ("body", "statement", "rule"):
        if field in row:
            return row[field]
    raise KeyError(row)


def _published_row(world, handle, env=None):
    bundle = _bundle(world, env)
    return next(r for r in bundle.tree + bundle.hypotheses + bundle.guardrails
                if r.get("handle") == handle)


def _base(world, kind, item_id):
    """The ``base`` a member's edit carries: the digest of the text their row showed."""
    return view_digest(_view(_published_row(world, _handle(kind, item_id))))


class _FakeDaemon:
    """Stands in for POST /v1/pipeline/update-field: records calls, echoes the record."""

    def __init__(self, stored_claim=None, error=None):
        self.calls = []
        self.stored_claim = stored_claim
        self.error = error

    def __call__(self, method, path, query=None, body=None, headers=None):
        params = {k: v[0] for k, v in urllib.parse.parse_qs(query, keep_blank_values=True).items()}
        self.calls.append((method, path, params))
        if self.error:
            raise self.error
        claim = params["value"] if self.stored_claim is None else self.stored_claim
        return json.dumps({"record": {"id": params["id"], "claim": claim}})


# --- node -------------------------------------------------------------------


def test_node_edit_replaces_the_body_and_keeps_every_other_front_matter_line(world, capsys):
    # The inbound drain's argv shape: --text= and --base= as single tokens.
    rc = apply_mod.main(["--handle", _handle("node", "acme-widgets"), "--op", "edit",
                         "--text=Widgets are green.\n\nThey were never blue.",
                         f"--base={_base(world, 'node', 'acme-widgets')}", "--apply"])

    assert rc == 0
    assert "node acme-widgets: edit -> body" in capsys.readouterr().out
    text = _node_path(world).read_text(encoding="utf-8")
    fm_text, body = text.split("\n---\n", 1)
    assert body == "\nWidgets are green.\n\nThey were never blue.\n"
    kept = [ln for ln in FRONT_MATTER.split("\n")
            if not ln.startswith(("last_updated", "last_update_trigger", "  "))]
    assert [ln for ln in fm_text.split("\n")[1:] if ln in kept] == kept, "other lines, in order"
    fm = yaml.safe_load(fm_text.split("\n", 1)[1])
    assert str(fm["last_updated"]) == TODAY
    assert fm["last_update_trigger"] == {"type": "direct_correction", "source": "member-edit",
                                         "session": "sid-under-test"}
    index = yaml.safe_load((world / "knowledge" / "tree" / "_tree.yaml").read_text())
    assert index["nodes"]["acme-widgets"]["last_updated"] == TODAY
    assert index["nodes"]["acme-long"]["last_updated"] == "2026-01-01"


def test_the_published_body_is_the_edited_text(world):
    """What the member sees next comes from the export's own reader."""
    apply_mod.main(["--handle", _handle("node", "acme-widgets"), "--op", "edit",
                    "--text", "Widgets are green.",
                    f"--base={_base(world, 'node', 'acme-widgets')}", "--apply"])
    nodes = {n["key"]: n for n in export_mod.read_tree_nodes(world)}
    assert nodes["acme-widgets"]["body"].strip() == "Widgets are green."


def test_dry_run_writes_nothing(world, capsys):
    rc = apply_mod.main(["--handle", _handle("node", "acme-widgets"), "--op", "edit",
                         "--text", "Widgets are green.",
                         f"--base={_base(world, 'node', 'acme-widgets')}"])
    assert rc == 0
    assert "dry run" in capsys.readouterr().out
    assert _node_path(world).read_text(encoding="utf-8") == NODE_TEXT


def test_a_text_that_starts_with_a_dash_is_text_not_a_flag(world):
    rc = apply_mod.main(["--handle", _handle("node", "acme-widgets"), "--op", "edit",
                         "--text=- widgets\n- gears",
                         f"--base={_base(world, 'node', 'acme-widgets')}", "--apply"])
    assert rc == 0
    assert _node_path(world).read_text(encoding="utf-8").endswith("\n\n- widgets\n- gears\n")


def test_a_crlf_node_keeps_its_line_ending(world):
    _node_path(world).write_bytes(NODE_TEXT.replace("\n", "\r\n").encode("utf-8"))
    rc = apply_mod.main(["--handle", _handle("node", "acme-widgets"), "--op", "edit",
                         "--text", "Widgets are green.",
                         f"--base={_base(world, 'node', 'acme-widgets')}", "--apply"])
    assert rc == 0
    raw = _node_path(world).read_bytes()
    assert raw.count(b"\n") == raw.count(b"\r\n")


def test_a_node_longer_than_the_published_view_is_refused(world, capsys):
    path = world / "knowledge" / "tree" / "acme" / "acme-long.md"
    before = path.read_bytes()
    rc = apply_mod.main(["--handle", _handle("node", "acme-long"), "--op", "edit",
                         "--text", "short", "--apply"])
    out = capsys.readouterr()
    assert rc == 3
    assert "view_truncated" in out.err
    assert out.out == ""
    assert path.read_bytes() == before


# --- refusals -------------------------------------------------------------------


@pytest.mark.parametrize("handle_of, op, reason", [
    (lambda: "0" * 16, "edit", "not_addressable"),
    (lambda: _handle("guardrail", "guard-1"), "edit", "view_base_missing"),
    (lambda: _handle("node", "acme-widgets"), "forget", "no_retention_store"),
    (lambda: _handle("hypothesis", "hyp-acme-1"), "forget", "no_retention_store"),
    (lambda: _handle("guardrail", "guard-1"), "forget", "no_retention_store"),
    (lambda: _handle("node", "acme-widgets"), "rename", "unknown_op"),
    (lambda: _handle("node", "acme-paths"), "edit", "view_redacted"),
])
def test_refusals_exit_3_and_write_nothing(world, capsys, handle_of, op, reason):
    stores = {p: p.read_bytes() for p in world.rglob("*") if p.is_file()}
    report = {}
    rc = apply_mod.main(["--handle", handle_of(), "--op", op, "--text", "anything", "--apply"],
                        report)
    out = capsys.readouterr()
    assert rc == 3
    assert f"refused ({reason})" in out.err
    assert report == {"refused": reason}, "the drain reads the code from here, not stderr"
    assert out.out == "", "a refusal prints nothing on stdout"
    assert {p: p.read_bytes() for p in world.rglob("*") if p.is_file()} == stores


def test_the_box_accepts_exactly_the_rows_the_export_marks_unredacted(world, monkeypatch, capsys):
    """The member UI gates on the export's ``unredacted`` flag and the box on its own checks.
    An edit sent with the base of the row it came from must get the same verdict from both,
    on every row the export published."""
    monkeypatch.setattr(_rt, "rt_call", _FakeDaemon())
    bundle = _bundle(world)
    rows = [r for r in bundle.tree + bundle.hypotheses + bundle.guardrails if "handle" in r]
    assert len(rows) == 7, "every node, hypothesis and guardrail in the fixture is addressable"
    assert {bool(r.get("unredacted")) for r in rows} == {True, False}, "both verdicts occur"
    assert {bool(r.get("unredacted")) for r in bundle.guardrails} == {True, False}
    for row in rows:
        rc = apply_mod.main(["--handle", row["handle"], "--op", "edit", "--text", "Corrected.",
                             f"--base={view_digest(_view(row))}"])
        err = capsys.readouterr().err
        label = row.get("key") or row.get("statement") or row.get("rule")
        if row.get("unredacted"):
            assert rc == 0, (label, err)
        elif row.get("key") == "acme-long":
            # A body the export cut at its cap is not marked ( finding 7), and the
            # box refuses it for that reason.
            assert "refused (view_truncated)" in err, label
        else:
            assert "refused (view_redacted)" in err, label


def test_the_export_and_the_box_agree_at_the_body_cap(world, capsys):
    """A page of exactly the cap is whole: marked, and accepted. One character more is cut:
    unmarked, and refused. A cut body reads like a whole one, so the reader says which it
    is (g-335-1726 finding 7)."""
    tree = world / "knowledge" / "tree"
    index = yaml.safe_load((tree / "_tree.yaml").read_text(encoding="utf-8"))
    edge = "x" * export_mod._NODE_BODY_CAP
    (tree / "acme" / "acme-edge.md").write_text(f"---\ntopic: Edge\n---\n\n{edge}",
                                                encoding="utf-8")
    index["nodes"]["acme-edge"] = {"file": "world/knowledge/tree/acme/acme-edge.md",
                                   "summary": "Edge.", "last_updated": "2026-01-01"}
    (tree / "_tree.yaml").write_text(yaml.safe_dump(index, sort_keys=False), encoding="utf-8")
    rows = {r["key"]: r for r in _bundle(world).tree}
    assert len(rows["acme-edge"]["body"]) == len(rows["acme-long"]["body"]) == len(edge)
    assert rows["acme-edge"].get("unredacted") is True
    assert "handle" in rows["acme-long"] and "unredacted" not in rows["acme-long"]
    for key, refusal in (("acme-edge", None), ("acme-long", "view_truncated")):
        rc = apply_mod.main(["--handle", rows[key]["handle"], "--op", "edit",
                             "--text", "Corrected.", f"--base={view_digest(_view(rows[key]))}"])
        err = capsys.readouterr().err
        if refusal is None:
            assert rc == 0, (key, err)
        else:
            assert rc == 3 and f"refused ({refusal})" in err, (key, err)


def test_a_hypothesis_whose_published_statement_hides_text_is_refused(world, monkeypatch, capsys):
    daemon = _FakeDaemon()
    monkeypatch.setattr(_rt, "rt_call", daemon)
    rc = apply_mod.main(["--handle", _handle("hypothesis", "hyp-acme-2"), "--op", "edit",
                         "--text", "Results are kept in [path].", "--apply"])
    assert rc == 3
    assert "refused (view_redacted)" in capsys.readouterr().err
    assert daemon.calls == [], "a refused edit never reaches the store"


def test_an_unprovisioned_environment_resolves_nothing(world, monkeypatch, capsys):
    monkeypatch.setenv("ENVIRONMENT_ID", "")
    rc = apply_mod.main(["--handle", _handle("node", "acme-widgets"), "--op", "edit",
                         "--text", "Widgets are green.", "--apply"])
    assert rc == 3
    assert "not_addressable" in capsys.readouterr().err


# --- base: an edit lands only on the view the member corrected ----------------------


_MALFORMED_BASES = {
    "absent": lambda b: None,
    "empty": lambda b: "",
    "short": lambda b: b[:63],
    "long": lambda b: b + "0",
    "not hex": lambda b: b[:63] + "g",
    "prefixed": lambda b: "sha256:" + b,
}


@pytest.mark.parametrize("kind, item_id", [("node", "acme-widgets"), ("hypothesis", "hyp-acme-1")])
@pytest.mark.parametrize("shape", sorted(_MALFORMED_BASES))
def test_an_edit_without_a_well_formed_base_is_refused(world, monkeypatch, capsys, kind, item_id,
                                                        shape):
    daemon = _FakeDaemon()
    monkeypatch.setattr(_rt, "rt_call", daemon)
    base = _MALFORMED_BASES[shape](_base(world, kind, item_id))
    stores = {p: p.read_bytes() for p in world.rglob("*") if p.is_file()}
    argv = ["--handle", _handle(kind, item_id), "--op", "edit", "--text", "Corrected.", "--apply"]
    rc = apply_mod.main(argv + ([] if base is None else [f"--base={base}"]))
    assert rc == 3
    assert "refused (view_base_missing)" in capsys.readouterr().err
    assert daemon.calls == [], "a refused edit never reaches the store"
    assert {p: p.read_bytes() for p in world.rglob("*") if p.is_file()} == stores


def test_the_base_is_read_case_and_whitespace_insensitively(world):
    rc = apply_mod.main(["--handle", _handle("node", "acme-widgets"), "--op", "edit",
                         "--text", "Widgets are green.",
                         f"--base= {_base(world, 'node', 'acme-widgets').upper()}\n"])
    assert rc == 0


def test_a_node_the_resident_changed_after_the_member_saw_it_is_refused(world, capsys):
    """The member corrected the page they were shown, and the resident has rewritten it
    since. Landing the correction would delete the resident's change unseen."""
    base = _base(world, "node", "acme-widgets")
    changed = NODE_TEXT.replace("Widgets are blue.", "Widgets are blue. Some are red.")
    _node_path(world).write_text(changed, encoding="utf-8")
    assert _base(world, "node", "acme-widgets") != base, "not a positive control"
    rc = apply_mod.main(["--handle", _handle("node", "acme-widgets"), "--op", "edit",
                         "--text", "Widgets are green.", f"--base={base}", "--apply"])
    assert rc == 3
    assert "refused (view_stale)" in capsys.readouterr().err
    assert _node_path(world).read_text(encoding="utf-8") == changed


def test_a_hypothesis_the_resident_changed_after_the_member_saw_it_is_refused(world, monkeypatch,
                                                                              capsys):
    daemon = _FakeDaemon()
    monkeypatch.setattr(_rt, "rt_call", daemon)
    base = _base(world, "hypothesis", "hyp-acme-1")
    store = world / "pipeline.jsonl"
    store.write_text(store.read_text(encoding="utf-8").replace('"old claim"', '"newer claim"'),
                     encoding="utf-8")
    assert _base(world, "hypothesis", "hyp-acme-1") != base, "not a positive control"
    rc = apply_mod.main(["--handle", _handle("hypothesis", "hyp-acme-1"), "--op", "edit",
                         "--text", "Corrected claim.", f"--base={base}", "--apply"])
    assert rc == 3
    assert "refused (view_stale)" in capsys.readouterr().err
    assert daemon.calls == [], "a stale edit never reaches the store"


def test_a_view_another_box_redacted_differently_is_stale(world, capsys):
    """The box that applies an edit may not redact as the box that published the view did.
    Here the publishing box held a credential whose value is in the node, so the member saw
    it masked. This box does not hold it, so its own view is whole and passes
    view_redacted; the base still differs, and nothing lands."""
    handle = _handle("node", "acme-widgets")
    elsewhere = _published_row(world, handle,
                               env={**os.environ, "ACME_API_KEY": "Widgets are blue"})
    assert _view(elsewhere) != _view(_published_row(world, handle)), "not a positive control"
    rc = apply_mod.main(["--handle", handle, "--op", "edit", "--text", "Widgets are green.",
                         f"--base={view_digest(_view(elsewhere))}", "--apply"])
    assert rc == 3
    assert "refused (view_stale)" in capsys.readouterr().err
    assert _node_path(world).read_text(encoding="utf-8") == NODE_TEXT


def test_the_base_covers_the_published_view_not_the_stored_text(world, capsys):
    """A browser can hash only what the row carries. The stored body ends in a newline that
    the view trims, so a digest of the stored text is a different base."""
    stored = export_mod._strip_front_matter(NODE_TEXT)
    assert stored != stored.strip(), "not a positive control"
    rc = apply_mod.main(["--handle", _handle("node", "acme-widgets"), "--op", "edit",
                         "--text", "Widgets are green.", f"--base={view_digest(stored)}"])
    assert rc == 3
    assert "refused (view_stale)" in capsys.readouterr().err


# --- hypothesis -------------------------------------------------------------------


def test_hypothesis_edit_writes_claim_through_the_daemon(world, monkeypatch, capsys):
    daemon = _FakeDaemon()
    monkeypatch.setattr(_rt, "rt_call", daemon)
    rc = apply_mod.main(["--handle", _handle("hypothesis", "hyp-acme-1"), "--op", "edit",
                         "--text", "Widgets sell better in green.",
                         f"--base={_base(world, 'hypothesis', 'hyp-acme-1')}", "--apply"])
    assert rc == 0
    assert daemon.calls == [("POST", "/v1/pipeline/update-field",
                             {"id": "hyp-acme-1", "field": "claim",
                              "value": "Widgets sell better in green."})]
    assert "hypothesis hyp-acme-1: edit -> claim" in capsys.readouterr().out


# ids= keeps the node id short: pytest copies it into PYTEST_CURRENT_TEST, and Windows
# caps an environment value at 32,767 characters (a default id embeds the whole value).
@pytest.mark.parametrize("text, reason", [
    ("42", "store_would_coerce"),
    ("null", "store_would_coerce"),
    ('{"claim": "x"}', "store_would_coerce"),
    ("汉" * 7000, "too_long_for_store"),
], ids=["integer", "null", "json-object", "7000-cjk-chars"])
def test_hypothesis_texts_the_store_would_alter_are_refused(world, monkeypatch, capsys,
                                                            text, reason):
    daemon = _FakeDaemon()
    monkeypatch.setattr(_rt, "rt_call", daemon)
    rc = apply_mod.main(["--handle", _handle("hypothesis", "hyp-acme-1"), "--op", "edit",
                         "--text", text, f"--base={_base(world, 'hypothesis', 'hyp-acme-1')}",
                         "--apply"])
    assert rc == 3
    assert f"refused ({reason})" in capsys.readouterr().err
    assert daemon.calls == [], "a refused text never reaches the store"


@pytest.mark.parametrize("daemon", [
    _FakeDaemon(stored_claim="something else"),
    _FakeDaemon(error=_rt.RtError("daemon HTTP 414 for POST /v1/pipeline/update-field")),
])
def test_a_hypothesis_write_that_does_not_read_back_fails(world, monkeypatch, daemon):
    monkeypatch.setattr(_rt, "rt_call", daemon)
    rc = apply_mod.main(["--handle", _handle("hypothesis", "hyp-acme-1"), "--op", "edit",
                         "--text", "Widgets sell better in green.",
                         f"--base={_base(world, 'hypothesis', 'hyp-acme-1')}", "--apply"])
    assert rc == 1


def test_coerce_predicate_matches_the_pipeline_endpoint_parser():
    """The refusal mirrors the daemon's parser. If the parser changes, this fails."""
    from mind_api.src.world.pipeline_write import _parse_value

    for text in ["42", "-7", "1_000", "3.5", "1e5", "nan", "inf", "true", "false", "null",
                 "[]", "[1, 2]", '{"a": 1}', "{not json", "[draft] widgets", "true story",
                 "42 widgets", "Widgets are blue.", "  "]:
        assert apply_mod.store_would_coerce(text) == (not isinstance(_parse_value(text), str)), text


# --- guardrail: a correction supersedes the rule ------------------------------------


_GUARD_SPEC = STORE_REGISTRY["guardrails"]


class _FakeGuardStore:
    """Stands in for the guardrails store's append and set-field endpoints. It writes the
    world's guardrails.jsonl as the daemon does, so the export's next read sees what a
    supersede left behind. ``fail`` names one (path, field) call that raises instead.

    It applies the store's own contract, the spec's defaults, validator and immutable
    fields, so a body the daemon would refuse is refused here too. A fake that only
    recorded calls let a dropped required field pass every test."""

    def __init__(self, world, fail=None):
        self.path = world / "guardrails.jsonl"
        self.fail = fail
        self.calls = []

    def records(self):
        return [json.loads(ln) for ln in self.path.read_text(encoding="utf-8").splitlines() if ln]

    def __call__(self, method, path, query=None, body=None, headers=None):
        params = {k: v[0] for k, v in urllib.parse.parse_qs(query, keep_blank_values=True).items()}
        self.calls.append((path, params.get("field"), json.loads(body) if body else None))
        if self.fail == (path, params.get("field")):
            raise _rt.RtError("daemon HTTP 500")
        records = self.records()
        if path == "/v1/store/append":
            rec = {k: v for k, v in json.loads(body).items() if k != "allow_near_dup"}
            rec["id"] = f"guard-{1 + max(int(r['id'].split('-')[1]) for r in records)}"
            records.append(apply_defaults(rec, _GUARD_SPEC.default_fields))
        else:
            if params["field"] in _GUARD_SPEC.immutable_fields:
                raise _rt.RtError("daemon HTTP 400: immutable_field")
            rec = next(r for r in records if r["id"] == params["id"])
            apply_defaults(rec, _GUARD_SPEC.default_fields)[params["field"]] = params["value"]
        try:
            _GUARD_SPEC.validate(None, rec)
        except (ValueError, TypeError) as exc:
            raise _rt.RtError(f"daemon HTTP 400: {exc}") from exc
        self.path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        return json.dumps({"ok": True, "record": rec})


def _guard_argv(world, text="Paint widgets green."):
    return ["--handle", _handle("guardrail", "guard-1"), "--op", "edit", f"--text={text}",
            f"--base={_base(world, 'guardrail', 'guard-1')}", "--apply"]


def test_a_guardrail_edit_supersedes_the_rule_it_corrects(world, monkeypatch, capsys):
    store = _FakeGuardStore(world)
    monkeypatch.setattr(_rt, "rt_call", store)
    rc = apply_mod.main(_guard_argv(world) + ["--json"])
    out = capsys.readouterr()
    assert rc == 0, out.err
    report = json.loads(out.out)
    assert report["superseded_by"] == "guard-3"
    assert report["handle"] == _handle("guardrail", "guard-3")
    by_id = {r["id"]: r for r in store.records()}
    new, old = by_id["guard-3"], by_id["guard-1"]
    assert new["rule"] == "Paint widgets green." and new["status"] == "active"
    assert new["source"] == "member-edit"
    # WHEN the rule applies is carried. The text that restated the old rule is not, and
    # neither is the old record's own supersedes tag.
    assert new["category"] == "acme" and new["trigger_condition"] == "when painting widgets"
    assert new["when_to_use"] == {"conditions": ["painting"], "category": "acme"}
    assert new["tags"] == ["paint", "member-edit", "supersedes:guard-1"]
    assert not {"title", "action_hint", "severity"} & set(new)
    # The old record keeps what the member replaced and names what replaced it.
    assert old["status"] == "retired" and old["retirement_date"] == TODAY
    assert old["retirement_reason"].startswith("superseded by guard-3")
    assert old["rule"] == "old rule" and old["action_hint"] == "Run the old check."
    # One add, sent past the near-duplicate refusal, then status written last.
    assert [(p, f) for p, f, _ in store.calls] == [
        ("/v1/store/append", None), ("/v1/store/set-field", "retirement_reason"),
        ("/v1/store/set-field", "retirement_date"), ("/v1/store/set-field", "status")]
    assert store.calls[0][2]["allow_near_dup"] is True


def test_an_in_process_caller_reads_the_new_handle_from_its_report(world, monkeypatch, capsys):
    """The drain records this handle for the member, so their list can follow the rule."""
    monkeypatch.setattr(_rt, "rt_call", _FakeGuardStore(world))
    report = {}
    assert apply_mod.main(_guard_argv(world), report) == 0, capsys.readouterr().err
    assert report["handle"] == _handle("guardrail", "guard-3")
    assert report["superseded_by"] == "guard-3" and report["applied"] is True


def test_the_next_view_shows_the_correction_under_a_new_handle_and_not_the_old_rule(
        world, monkeypatch, capsys):
    monkeypatch.setattr(_rt, "rt_call", _FakeGuardStore(world))
    assert apply_mod.main(_guard_argv(world)) == 0
    rows = {r["handle"]: r for r in _bundle(world).guardrails}
    old, new = _handle("guardrail", "guard-1"), _handle("guardrail", "guard-3")
    assert old not in rows
    assert rows[new]["rule"] == "Paint widgets green." and rows[new].get("unredacted") is True
    # A second correction sent from the old view now addresses nothing.
    capsys.readouterr()
    rc = apply_mod.main(["--handle", old, "--op", "edit", "--text", "Paint them red.",
                         f"--base={view_digest('old rule')}", "--apply"])
    assert rc == 3 and "refused (not_addressable)" in capsys.readouterr().err


def test_a_rerun_after_a_failed_retire_reuses_the_successor(world, monkeypatch, capsys):
    """The drain leaves a failed record for an operator to requeue. Part way, both rules are
    published, never neither, and the requeued run retires the old rule without a second
    successor."""
    monkeypatch.setattr(_rt, "rt_call", _FakeGuardStore(world, fail=("/v1/store/set-field", "status")))
    argv = _guard_argv(world)
    assert apply_mod.main(argv) == 1
    assert "write failed for guardrail guard-1" in capsys.readouterr().err
    shown = [r["rule"] for r in _bundle(world).guardrails]
    assert "old rule" in shown and "Paint widgets green." in shown

    healthy = _FakeGuardStore(world)
    monkeypatch.setattr(_rt, "rt_call", healthy)
    assert apply_mod.main(argv) == 0
    assert "/v1/store/append" not in [p for p, _, _ in healthy.calls], "the successor is reused"
    records = healthy.records()
    assert [r["rule"] for r in records].count("Paint widgets green.") == 1
    assert next(r for r in records if r["id"] == "guard-1")["status"] == "retired"


def test_a_present_but_empty_required_field_still_reaches_the_successor(world, monkeypatch, capsys):
    """The store checks a required field by presence, so an empty trigger_condition is a valid
    record and the old rule's own. Dropping it as falsy would fail the add. A null optional
    field rides along without tripping the validator."""
    path = world / "guardrails.jsonl"
    rows = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln]
    rows[0].update(trigger_condition="", trigger_pattern=None)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    store = _FakeGuardStore(world)
    monkeypatch.setattr(_rt, "rt_call", store)
    assert apply_mod.main(_guard_argv(world)) == 0, capsys.readouterr().err
    new = next(r for r in store.records() if r["rule"] == "Paint widgets green.")
    assert new["trigger_condition"] == "" and new["status"] == "active"
    assert next(r for r in store.records() if r["id"] == "guard-1")["status"] == "retired"


def test_a_guardrail_whose_published_rule_hides_text_is_refused(world, monkeypatch, capsys):
    store = _FakeGuardStore(world)
    monkeypatch.setattr(_rt, "rt_call", store)
    rc = apply_mod.main(["--handle", _handle("guardrail", "guard-2"), "--op", "edit",
                         "--text", "Specs live in [path].",
                         f"--base={_base(world, 'guardrail', 'guard-2')}", "--apply"])
    assert rc == 3
    assert "refused (view_redacted)" in capsys.readouterr().err
    assert store.calls == [], "a refused edit never reaches the store"


# --- front matter -------------------------------------------------------------------


def test_restamp_adds_the_stamp_to_a_node_with_no_front_matter():
    out = apply_mod.restamp_front_matter("", TODAY, None)
    assert yaml.safe_load(out) == {
        "last_updated": datetime.date.fromisoformat(TODAY),
        "last_update_trigger": {"type": "direct_correction", "source": "member-edit",
                                "session": None},
    }


def test_restamp_replaces_a_legacy_scalar_trigger():
    out = apply_mod.restamp_front_matter("topic: T\nlast_update_trigger: legacy note", TODAY, "s")
    assert yaml.safe_load(out)["last_update_trigger"]["source"] == "member-edit"
    assert "legacy note" not in out


def test_restamp_refuses_front_matter_that_is_not_a_mapping():
    with pytest.raises(ValueError):
        apply_mod.restamp_front_matter("- a\n- b", TODAY, "s")
