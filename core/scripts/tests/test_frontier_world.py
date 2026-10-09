"""_frontier_world.py: skip a frontier-origin-only test in a world that is not the frontier ()."""
import pytest

import _frontier_world as fw


def _world(tmp_path, text):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "compatibility.yaml").write_text(text, encoding="utf-8")
    return tmp_path


def test_read_role_returns_the_declared_role(tmp_path):
    assert fw.read_role(_world(tmp_path, "self_role: downstream\n")) == "downstream"


@pytest.mark.parametrize("text", ["", "self_role: 3\n", "self_role: [a]\n", "other: x\n", "{{{ not yaml"])
def test_read_role_is_none_for_anything_unreadable(tmp_path, text):
    assert fw.read_role(_world(tmp_path, text)) is None


def test_read_role_is_none_when_the_file_is_absent(tmp_path):
    assert fw.read_role(tmp_path) is None


@pytest.mark.parametrize("role", [None, "frontier"])
def test_the_frontier_and_an_unreadable_world_run_the_test(role):
    assert fw.skip_reason(role) is None


@pytest.mark.parametrize("role", ["downstream", "seed"])
def test_any_other_role_skips_and_names_itself(role):
    assert role in fw.skip_reason(role)


def test_the_mark_follows_the_world_role(monkeypatch):
    monkeypatch.setattr(fw, "world_self_role", lambda: "downstream")
    skip = fw.requires_frontier_world("reads the frontier catalog").mark
    assert skip.name == "skipif" and skip.args == (True,)
    assert "reads the frontier catalog" in skip.kwargs["reason"] and "downstream" in skip.kwargs["reason"]
    monkeypatch.setattr(fw, "world_self_role", lambda: "frontier")
    assert fw.requires_frontier_world("x").mark.args == (False,)
    monkeypatch.setattr(fw, "world_self_role", lambda: None)
    assert fw.requires_frontier_world("x").mark.args == (False,)
