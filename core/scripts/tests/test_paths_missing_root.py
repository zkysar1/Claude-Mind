""" outcome 1, CLI half: _paths.assert_world_dir / assert_meta_dir
refuse a RESOLVED world or meta root that does not exist, and so does every
resolve_file_path('world/...') / ('meta/...') that goes through them.

Measured on a Windows box: a root inherited as /c/<path> under
MSYS_NO_PATHCONV=1 resolved to a phantom, empty C:/c/<path>; reads answered
'nothing found' and a write landed in a tree nobody reads. The daemon half is
mind_api/tests/test_agent_paths_missing_root.py. Under own-cloud the local tree
is a read-through cache, so its absence proves nothing (guard-980): local only.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

import _paths

CORE_SCRIPTS = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _local(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setattr(_paths, "_ROOTS_SEEN", set(), raising=False)


def _roots(monkeypatch, world, meta):
    monkeypatch.setattr(_paths, "WORLD_DIR", world)
    monkeypatch.setattr(_paths, "META_DIR", meta)


def test_existing_roots_pass(tmp_path, monkeypatch):
    """Positive control: an existing root is served exactly as before."""
    (tmp_path / "w").mkdir()
    (tmp_path / "m").mkdir()
    _roots(monkeypatch, tmp_path / "w", tmp_path / "m")
    assert _paths.assert_world_dir("t") is None
    assert _paths.assert_meta_dir("t") is None
    assert _paths.resolve_file_path("world/a/b.jsonl") == tmp_path / "w" / "a" / "b.jsonl"
    assert _paths.resolve_file_path("meta/c.yaml") == tmp_path / "m" / "c.yaml"


@pytest.mark.parametrize("missing", ["world", "meta"])
def test_a_missing_root_is_refused_by_name(tmp_path, monkeypatch, capsys, missing):
    world, meta = tmp_path / "w", tmp_path / "m"
    (meta if missing == "world" else world).mkdir()
    _roots(monkeypatch, world, meta)
    guard = _paths.assert_world_dir if missing == "world" else _paths.assert_meta_dir
    with pytest.raises(RuntimeError, match=f"{missing} root .* does not exist") as exc:
        guard("mod-x")
    assert "g-115-11554" in str(exc.value) and "mod-x" in str(exc.value)
    assert str(world if missing == "world" else meta) in capsys.readouterr().err


@pytest.mark.parametrize("prefix", ["world", "meta"])
def test_resolve_file_path_refuses_through_a_missing_root(tmp_path, monkeypatch, prefix):
    _roots(monkeypatch, tmp_path / "w", tmp_path / "m")
    with pytest.raises(RuntimeError, match="does not exist"):
        _paths.resolve_file_path(f"{prefix}/x.jsonl")
    # A path outside world/ and meta/ never consults either root.
    assert _paths.resolve_file_path("core/x.py") == _paths.PROJECT_ROOT / "core" / "x.py"


def test_an_unresolved_root_keeps_its_own_refusal(monkeypatch):
    """Two refusals share the guard; pin which one answers (rb-6536)."""
    _roots(monkeypatch, None, None)
    with pytest.raises(RuntimeError, match="WORLD_DIR unresolved"):
        _paths.assert_world_dir("t")
    with pytest.raises(RuntimeError, match="META_DIR unresolved"):
        _paths.assert_meta_dir("t")


def test_a_refusal_is_not_remembered(tmp_path, monkeypatch):
    world = tmp_path / "w"
    _roots(monkeypatch, world, tmp_path)
    with pytest.raises(RuntimeError):
        _paths.assert_world_dir("t")
    world.mkdir()
    assert _paths.assert_world_dir("t") is None


def test_a_served_root_costs_one_stat_per_process(tmp_path, monkeypatch):
    """tree.py and tree_match.py resolve once per node: only the first call
    checks the disk, and only a success is remembered."""
    (tmp_path / "w").mkdir()
    _roots(monkeypatch, tmp_path / "w", tmp_path)
    calls = []
    real = Path.is_dir

    def counting(self):
        calls.append(self)
        return real(self)

    monkeypatch.setattr(Path, "is_dir", counting)
    for i in range(50):
        _paths.resolve_file_path(f"world/n{i}.md")
    assert calls == [tmp_path / "w"]


def test_own_cloud_does_not_refuse_a_missing_local_root(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    _roots(monkeypatch, tmp_path / "w", tmp_path / "m")
    assert _paths.assert_world_dir("t") is None
    assert _paths.assert_meta_dir("t") is None


def _cli_write(root_env, project_tmp):
    """A CLI writer as the incident ran it: MIND_WORLD inherited, resolve a
    store path, create its parent, append a row."""
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); import _paths\n"
        "p = _paths.resolve_file_path('world/board/general.jsonl')\n"
        "p.parent.mkdir(parents=True, exist_ok=True)\n"
        "with open(p, 'a', encoding='utf-8') as f: f.write('row\\n')\n"
    )
    env = dict(os.environ, MIND_WORLD=str(root_env), STORAGE_BACKEND="local")
    return subprocess.run([sys.executable, "-c", code, str(CORE_SCRIPTS)],
                          env=env, cwd=str(project_tmp), capture_output=True,
                          text=True, timeout=60)


def test_a_cli_writer_on_a_phantom_world_writes_nothing(tmp_path):
    """Outcome 3 for the CLI, end to end in a fresh process: the write is
    refused and the phantom tree is never created; the same writer on an
    existing root lands its row (positive control)."""
    phantom = tmp_path / "c" / "phantom" / "world"
    res = _cli_write(phantom, tmp_path)
    assert res.returncode != 0 and "does not exist" in res.stderr, res.stderr[-600:]
    assert not (tmp_path / "c").exists(), "the refused write created the phantom root"

    real = tmp_path / "real-world"
    real.mkdir()
    res = _cli_write(real, tmp_path)
    assert res.returncode == 0, res.stderr[-600:]
    assert (real / "board" / "general.jsonl").read_text(encoding="utf-8") == "row\n"
