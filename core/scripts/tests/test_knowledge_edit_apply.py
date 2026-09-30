"""Tests for core/scripts/knowledge-edit-apply.py ().

A synthetic world with one exposed wiki node, one too long to edit, one hypothesis and
one guardrail. Handles are minted with the same ``item_handle`` the export publishes, so
every test addresses items exactly as a member's queued edit would. The hypothesis store
is the daemon, so ``_rt.rt_call`` is replaced with a fake that echoes the record.
"""

from __future__ import annotations

import datetime
import importlib.util
import json
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
from knowledge_projection import item_handle  # noqa: E402


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
    index = {
        "last_updated": "2026-01-01",
        "nodes": {
            "acme-widgets": {"file": "world/knowledge/tree/acme/acme-widgets.md",
                             "summary": "Widgets.", "last_updated": "2026-01-01"},
            "acme-long": {"file": "world/knowledge/tree/acme/acme-long.md",
                          "summary": "Long.", "last_updated": "2026-01-01"},
        },
    }
    (tree / "_tree.yaml").write_text(yaml.safe_dump(index, sort_keys=False), encoding="utf-8")
    (w / "pipeline.jsonl").write_text(json.dumps(
        {"id": "hyp-acme-1", "category": "acme", "claim": "old claim", "stage": "active"}) + "\n",
        encoding="utf-8")
    (w / "guardrails.jsonl").write_text(json.dumps(
        {"id": "guard-1", "category": "acme", "rule": "old rule", "status": "active"}) + "\n",
        encoding="utf-8")
    monkeypatch.setenv("WORLD_PATH", str(w))
    monkeypatch.setenv("KNOWLEDGE_HANDLE_SECRET", SECRET)
    monkeypatch.setenv("ENVIRONMENT_ID", ENV_ID)
    monkeypatch.setenv("MIND_SID", "sid-under-test")
    return w


def _handle(kind, item_id):
    return item_handle(kind, item_id, SECRET, ENV_ID)


def _node_path(world):
    return world / "knowledge" / "tree" / "acme" / "acme-widgets.md"


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
    rc = apply_mod.main(["--handle", _handle("node", "acme-widgets"), "--op", "edit",
                         "--text", "Widgets are green.\n\nThey were never blue.", "--apply"])

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
                    "--text", "Widgets are green.", "--apply"])
    nodes = {n["key"]: n for n in export_mod.read_tree_nodes(world)}
    assert nodes["acme-widgets"]["body"].strip() == "Widgets are green."


def test_dry_run_writes_nothing(world, capsys):
    rc = apply_mod.main(["--handle", _handle("node", "acme-widgets"), "--op", "edit",
                         "--text", "Widgets are green."])
    assert rc == 0
    assert "dry run" in capsys.readouterr().out
    assert _node_path(world).read_text(encoding="utf-8") == NODE_TEXT


def test_a_text_that_starts_with_a_dash_is_text_not_a_flag(world):
    rc = apply_mod.main(["--handle", _handle("node", "acme-widgets"), "--op", "edit",
                         "--text=- widgets\n- gears", "--apply"])
    assert rc == 0
    assert _node_path(world).read_text(encoding="utf-8").endswith("\n\n- widgets\n- gears\n")


def test_a_crlf_node_keeps_its_line_ending(world):
    _node_path(world).write_bytes(NODE_TEXT.replace("\n", "\r\n").encode("utf-8"))
    rc = apply_mod.main(["--handle", _handle("node", "acme-widgets"), "--op", "edit",
                         "--text", "Widgets are green.", "--apply"])
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
    (lambda: _handle("guardrail", "guard-1"), "edit", "edit_unsupported"),
    (lambda: _handle("node", "acme-widgets"), "forget", "forget_pending_ruling"),
    (lambda: _handle("hypothesis", "hyp-acme-1"), "forget", "forget_pending_ruling"),
    (lambda: _handle("node", "acme-widgets"), "rename", "unknown_op"),
])
def test_refusals_exit_3_and_write_nothing(world, capsys, handle_of, op, reason):
    stores = {p: p.read_bytes() for p in world.rglob("*") if p.is_file()}
    rc = apply_mod.main(["--handle", handle_of(), "--op", op, "--text", "anything", "--apply"])
    out = capsys.readouterr()
    assert rc == 3
    assert f"refused ({reason})" in out.err
    assert out.out == "", "a refusal prints nothing on stdout"
    assert {p: p.read_bytes() for p in world.rglob("*") if p.is_file()} == stores


def test_an_unprovisioned_environment_resolves_nothing(world, monkeypatch, capsys):
    monkeypatch.setenv("ENVIRONMENT_ID", "")
    rc = apply_mod.main(["--handle", _handle("node", "acme-widgets"), "--op", "edit",
                         "--text", "Widgets are green.", "--apply"])
    assert rc == 3
    assert "not_addressable" in capsys.readouterr().err


# --- hypothesis -------------------------------------------------------------------


def test_hypothesis_edit_writes_claim_through_the_daemon(world, monkeypatch, capsys):
    daemon = _FakeDaemon()
    monkeypatch.setattr(_rt, "rt_call", daemon)
    rc = apply_mod.main(["--handle", _handle("hypothesis", "hyp-acme-1"), "--op", "edit",
                         "--text", "Widgets sell better in green.", "--apply"])
    assert rc == 0
    assert daemon.calls == [("POST", "/v1/pipeline/update-field",
                             {"id": "hyp-acme-1", "field": "claim",
                              "value": "Widgets sell better in green."})]
    assert "hypothesis hyp-acme-1: edit -> claim" in capsys.readouterr().out


@pytest.mark.parametrize("text, reason", [
    ("42", "store_would_coerce"),
    ("null", "store_would_coerce"),
    ('{"claim": "x"}', "store_would_coerce"),
    ("汉" * 7000, "too_long_for_store"),
])
def test_hypothesis_texts_the_store_would_alter_are_refused(world, monkeypatch, capsys,
                                                            text, reason):
    daemon = _FakeDaemon()
    monkeypatch.setattr(_rt, "rt_call", daemon)
    rc = apply_mod.main(["--handle", _handle("hypothesis", "hyp-acme-1"), "--op", "edit",
                         "--text", text, "--apply"])
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
                         "--text", "Widgets sell better in green.", "--apply"])
    assert rc == 1


def test_coerce_predicate_matches_the_pipeline_endpoint_parser():
    """The refusal mirrors the daemon's parser. If the parser changes, this fails."""
    from mind_api.src.world.pipeline_write import _parse_value

    for text in ["42", "-7", "1_000", "3.5", "1e5", "nan", "inf", "true", "false", "null",
                 "[]", "[1, 2]", '{"a": 1}', "{not json", "[draft] widgets", "true story",
                 "42 widgets", "Widgets are blue.", "  "]:
        assert apply_mod.store_would_coerce(text) == (not isinstance(_parse_value(text), str)), text


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
