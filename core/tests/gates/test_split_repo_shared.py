"""Split-Repo Registry shared-parse coverage ().

The lift exists because ONE defect lived in TWO consumers and only one was
fixed: work merged to `origin/dev` on a registry-listed repo read as STRANDED
when containment was asked against the default branch. `completed-not-committed
-sweep.py` was fixed in g-115-9040; `gates/uncommitted_work.py` was left
registry-blind. Both now share `core/scripts/_split_repo.py`.

What these tests pin, in priority order:
  1. FAIL-CLOSED. Every unreadable input yields the EMPTY set, so every caller
     falls through to its default-branch behaviour. Widening on an unreadable
     registry would turn a close gate into a rubber stamp, silently, in the
     dangerous direction — so this is the invariant with the most cases.
  2. NO DRIFT BETWEEN CONSUMERS. The sweep's published `split_repo_names` and
     the shared parse must agree. That agreement IS the lift; if it can drift,
     nothing was fixed.
  3. THE GATE ACTUALLY READS IT (guard-6374: a helper is not wired until the
     consumer predicate reads it), exercised through the literal production
     argument shape — a `Path`, positionally, with no injected registry
     (guard-920).
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = REPO_ROOT / "core" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _split_repo import is_split_repo, split_repo_names  # noqa: E402


def _load_by_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_registry(root: Path, body: str) -> Path:
    conv = root / "conventions"
    conv.mkdir(parents=True, exist_ok=True)
    (conv / "sdlc-environments.md").write_text(body, encoding="utf-8")
    return root


# ─── 1. fail-closed ──────────────────────────────────────────────────────────

def test_no_world_path_returns_empty(monkeypatch):
    monkeypatch.delenv("WORLD_PATH", raising=False)
    assert split_repo_names() == frozenset()


def test_nonexistent_world_returns_empty():
    assert split_repo_names("/nonexistent/definitely/not/here") == frozenset()


def test_missing_section_returns_empty(tmp_path):
    _write_registry(tmp_path, "# Doc\n\n## Some Other Heading\n\n| A | YES |\n")
    assert split_repo_names(str(tmp_path)) == frozenset()


def test_unparseable_table_returns_empty(tmp_path):
    _write_registry(tmp_path, "## Split-Repo Registry\n\nprose, no table rows\n")
    assert split_repo_names(str(tmp_path)) == frozenset()


# ─── 2. the parse itself ─────────────────────────────────────────────────────

def test_parses_yes_rows_only_and_stops_at_next_heading(tmp_path):
    _write_registry(tmp_path, "\n".join([
        "## Split-Repo Registry",
        "| Repo | Split? |",
        "| --- | --- |",
        "| `Alpha-App` | YES |",
        "| `Beta-App` | no |",
        "| (placeholder) | YES |",
        "",
        "## Next Section",
        "| `Gamma-App` | YES |",
    ]))
    assert split_repo_names(str(tmp_path)) == frozenset({"Alpha-App"})


# ─── 3. membership ───────────────────────────────────────────────────────────

def test_is_split_repo_injected_membership():
    names = frozenset({"Vinheim-Web-App"})
    assert is_split_repo("/x/y/Vinheim-Web-App", names) is True
    assert is_split_repo("/x/y/other-repo", names) is False


def test_explicit_empty_set_is_honoured_and_reads_no_disk(monkeypatch):
    """An explicitly-empty registry means 'nothing is split'. Only None triggers
    a read — otherwise an injected empty set would silently re-read the world
    and the injection point would be untestable."""
    monkeypatch.setenv("WORLD_PATH", "/nonexistent/should/not/be/read")
    assert is_split_repo("/x/y/Vinheim-Web-App", frozenset()) is False


# ─── 4. no drift between the two consumers (the reason the lift exists) ──────

def test_sweep_wrapper_agrees_with_shared_parse(tmp_path, monkeypatch):
    _write_registry(tmp_path, "\n".join([
        "## Split-Repo Registry",
        "| Repo | Split? |",
        "| --- | --- |",
        "| `Alpha-App` | YES |",
    ]))
    monkeypatch.setenv("WORLD_PATH", str(tmp_path))
    sweep = _load_by_path(
        "cnc_sweep", SCRIPTS / "completed-not-committed-sweep.py")
    assert sweep.split_repo_names() == split_repo_names(str(tmp_path))
    assert sweep.split_repo_names() == frozenset({"Alpha-App"})


# ─── 5. the gate reads it, in the production arg shape ───────────────────────

def _git(repo: Path, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=30)


@pytest.fixture
def repo_with_dev(tmp_path):
    """A real git repo carrying refs/remotes/origin/{main,dev}."""
    repo = tmp_path / "Alpha-App"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("x", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c1")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    for ref in ("refs/remotes/origin/main", "refs/remotes/origin/dev"):
        _git(repo, "update-ref", ref, head)
    return repo


def test_gate_landing_ref_prefers_dev_for_registry_listed(
        repo_with_dev, tmp_path, monkeypatch):
    _write_registry(tmp_path, "\n".join([
        "## Split-Repo Registry",
        "| Repo | Split? |",
        "| --- | --- |",
        "| `Alpha-App` | YES |",
    ]))
    monkeypatch.setenv("WORLD_PATH", str(tmp_path))
    uw = _load_by_path("uw_gate", SCRIPTS / "gates" / "uncommitted_work.py")
    # PRODUCTION ARG SHAPE (guard-920): a Path, positional, no injected registry.
    assert uw._repo_landing_ref(repo_with_dev) == "refs/remotes/origin/dev"


def test_gate_landing_ref_falls_back_when_not_listed(
        repo_with_dev, tmp_path, monkeypatch):
    _write_registry(tmp_path, "\n".join([
        "## Split-Repo Registry",
        "| Repo | Split? |",
        "| --- | --- |",
        "| `Some-Other-Repo` | YES |",
    ]))
    monkeypatch.setenv("WORLD_PATH", str(tmp_path))
    uw = _load_by_path("uw_gate2", SCRIPTS / "gates" / "uncommitted_work.py")
    got = uw._repo_landing_ref(repo_with_dev)
    assert got == uw._repo_default_ref(repo_with_dev)
    assert got != "refs/remotes/origin/dev"


def test_gate_landing_ref_falls_back_when_registry_unreadable(
        repo_with_dev, monkeypatch):
    """Fail-closed end to end: an unreadable registry must leave the gate
    behaving exactly as it did before this change."""
    monkeypatch.setenv("WORLD_PATH", "/nonexistent/definitely/not/here")
    uw = _load_by_path("uw_gate3", SCRIPTS / "gates" / "uncommitted_work.py")
    assert uw._repo_landing_ref(repo_with_dev) == uw._repo_default_ref(
        repo_with_dev)
