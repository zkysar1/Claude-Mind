"""test_seed_engine_deployment_registry.py —  regression.

The seed shipped core/ whole, so the dev deployment's peer registry
(core/config/environments/, one yaml per known deployment) reached every
downstream clone. Three coupled edits fixed it; these tests pin each one AND
the coupling between them:

  (a) SEED-CREATE   walk_include_entry drops every deployment registry entry;
                    only the generic entry ships. The exclusion is derived from
                    the dir itself, so no deployment name is typed anywhere
                    (the fixtures below use synthetic ids for the same reason).
  (b) DEST-PRESERVE _is_preserved_at_dest keeps EVERY deployment entry already at
                    the destination: its own (storage wiring) and its origin's
                    (the upstream lane). Without (b), (a) turns every plant into
                    a deletion of those files. The control test measures exactly
                    that, which is also the proof the (b) tests can go red.
  (c) GATE SCOPE    domain-leak-check.sh counts only hits in files the seed
                    ships, names the rest on every run, and scans UNSCOPED (the
                    over-reporting direction) when the include-set cannot be
                    resolved.

Hermetic: tmp source/dest roots. The gate runs from a COPY of its scripts inside
a tmp project with a synthetic blocklist. A symlink would scan the real tree and
write real gate telemetry. No network, no daemon, no real backend.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
REPO_ROOT = CORE_SCRIPTS.parent.parent
sys.path.insert(0, str(CORE_SCRIPTS))

import _peer_registry as reg  # noqa: E402
from _runtime_bash import BASH  # noqa: E402

ENGINE_PATH = CORE_SCRIPTS / "_seed_engine.py"
_spec = importlib.util.spec_from_file_location("_seed_engine", ENGINE_PATH)
_engine = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_engine)

REG = reg.ENV_REGISTRY_REL
GENERIC = f"{REG}/{reg.GENERIC_ENV_ENTRY}"
# Synthetic ids: a fixture must not type a real deployment name either.
OWN, ORIGIN, DEST_ONLY = "dest-self", "dest-origin", "dest-only"

CORE_ENTRY = {"path": "core/", "type": "directory", "required": True,
              "exclude_children": []}
MANIFEST = {"version": 1, "min_skill_version": 1, "max_skill_version": 1,
            "include": [CORE_ENTRY]}


def _write(root: Path, rel: str, text: str = "x: 1\n") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _registry(root: Path, *ids: str) -> None:
    _write(root, GENERIC, "environment_id: local\nbackend: local\n")
    for i in ids:
        _write(root, f"{REG}/{i}.yaml", f"environment_id: {i}\nbackend: local\n")


# ─────────────────────────────────────────────────────────────────────────────
# The shared predicate
# ─────────────────────────────────────────────────────────────────────────────
def test_predicate_is_the_loader_file_set_minus_the_generic_entry():
    assert reg.is_deployment_registry_entry(f"{REG}/{OWN}.yaml")
    assert reg.is_deployment_registry_entry(f"./{REG}/{OWN}.yaml")
    assert reg.is_deployment_registry_entry(REG.replace("/", "\\") + f"\\{OWN}.yaml")
    assert not reg.is_deployment_registry_entry(GENERIC)
    # load_env_registry globs *.yaml directly in the dir: nothing else is an entry.
    assert not reg.is_deployment_registry_entry(f"{REG}/README.md")
    assert not reg.is_deployment_registry_entry(f"{REG}/nested/{OWN}.yaml")
    assert not reg.is_deployment_registry_entry(f"core/config/{OWN}.yaml")
    assert not reg.is_deployment_registry_entry("")
    assert not reg.is_deployment_registry_entry(None)


# ─────────────────────────────────────────────────────────────────────────────
# (a) SEED-CREATE
# ─────────────────────────────────────────────────────────────────────────────
def test_seed_walk_ships_only_the_generic_entry(tmp_path):
    _registry(tmp_path, OWN, ORIGIN)
    _write(tmp_path, f"{REG}/README.md", "# registry\n")
    _write(tmp_path, "core/config/other.yaml")

    results = set(_engine.walk_include_entry(CORE_ENTRY, tmp_path))

    assert GENERIC in results
    assert f"{REG}/{OWN}.yaml" not in results
    assert f"{REG}/{ORIGIN}.yaml" not in results
    # Only registry ENTRIES are withheld; the rest of the dir and of core/ ship.
    assert f"{REG}/README.md" in results
    assert "core/config/other.yaml" in results


def test_this_repos_seed_carries_no_deployment_entry():
    """Outcome 1 against this checkout's own registry, whatever it holds today."""
    entries = sorted(p.name for p in (REPO_ROOT / REG).glob("*.yaml"))
    if not any(reg.is_deployment_registry_entry(f"{REG}/{n}") for n in entries):
        pytest.skip("this checkout's registry holds no deployment entry to withhold")
    manifest = _engine.load_manifest(REPO_ROOT / "core" / "config" / "seed-manifest.yaml")
    shipped = [r for r in _engine.resolve_include_set(manifest, REPO_ROOT)
               if r.startswith(REG + "/")]
    assert shipped == [GENERIC]


def test_list_includes_lines_is_the_json_list(tmp_path):
    _registry(tmp_path, OWN)
    mpath = _write(tmp_path, "m.yaml", json.dumps(MANIFEST))

    def run(*extra):
        r = subprocess.run([sys.executable, str(ENGINE_PATH), "list-includes",
                            "--manifest", str(mpath), "--source", str(tmp_path), *extra],
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr
        return r.stdout

    assert run("--lines").splitlines() == json.loads(run())["files"]


# ─────────────────────────────────────────────────────────────────────────────
# (b) DEST-PRESERVE, and the coupling with (a)
# ─────────────────────────────────────────────────────────────────────────────
def test_every_dest_entry_is_preserved_but_the_generic_one_is_not():
    for i in (OWN, ORIGIN, DEST_ONLY):
        assert _engine._is_preserved_at_dest(f"{REG}/{i}.yaml")
    # The generic entry still ships and must keep refreshing. Preserving it would
    # freeze it at every living-prod destination (copy-staged skips preserved files).
    assert not _engine._is_preserved_at_dest(GENERIC)


def _dest_with_registry(tmp_path):
    src, dest = tmp_path / "src", tmp_path / "dest"
    _registry(src, "src-peer-a", "src-peer-b")
    _write(src, "core/config/other.yaml")
    _registry(dest, OWN, ORIGIN, DEST_ONLY)
    _write(dest, "core/config/other.yaml")
    # Positive control: a genuine orphan the sweep MUST still find.
    _write(dest, "core/config/planted-orphan.yaml")
    return src, dest


def test_orphan_sweep_dry_run_keeps_dest_entries_and_still_finds_real_orphans(tmp_path):
    src, dest = _dest_with_registry(tmp_path)
    removed = set(_engine.do_remove_orphans(dest, MANIFEST, src, dry_run=True)["removed"])
    assert "core/config/planted-orphan.yaml" in removed, "the sweep must be live"
    for i in (OWN, ORIGIN, DEST_ONLY):
        assert f"{REG}/{i}.yaml" not in removed


def test_orphan_sweep_for_real_leaves_dest_entries_on_disk(tmp_path):
    src, dest = _dest_with_registry(tmp_path)
    out = _engine.do_remove_orphans(dest, MANIFEST, src, dry_run=False)
    assert out["removed"] == ["core/config/planted-orphan.yaml"]
    for i in (OWN, ORIGIN, DEST_ONLY):
        assert (dest / REG / f"{i}.yaml").is_file()
    assert (dest / GENERIC).is_file()


def test_without_dest_preserve_the_seed_exclusion_deletes_every_dest_entry(tmp_path,
                                                                          monkeypatch):
    """The coupling, measured: with (a) and without (b) every dest entry is an
    orphan. This is the production hazard (b) exists for."""
    src, dest = _dest_with_registry(tmp_path)
    real = _engine._is_preserved_at_dest
    monkeypatch.setattr(
        _engine, "_is_preserved_at_dest",
        lambda rel, extra_tops=frozenset(): (not reg.is_deployment_registry_entry(rel)
                                             and real(rel, extra_tops)))
    removed = set(_engine.do_remove_orphans(dest, MANIFEST, src, dry_run=True)["removed"])
    for i in (OWN, ORIGIN, DEST_ONLY):
        assert f"{REG}/{i}.yaml" in removed


def test_plan_lists_no_dest_entry_as_an_orphan(tmp_path):
    src, dest = _dest_with_registry(tmp_path)
    plan = _engine.do_plan(src, dest, MANIFEST, living_prod=True)
    real_orphans = plan["sections"]["orphan_deletions"]["real_orphans"]
    assert "core/config/planted-orphan.yaml" in real_orphans
    assert not [r for r in real_orphans if r.startswith(REG + "/")]


# ─────────────────────────────────────────────────────────────────────────────
# (c) GATE SCOPE — end to end, through the real script
# ─────────────────────────────────────────────────────────────────────────────
GATE_FILES = ("domain-leak-check.sh", "_domain_leak_marker.py", "_seed_engine.py",
              "_seed_transforms.py", "_exec_bits.py", "_peer_registry.py")
TERM = "Widgetron"
CLEAN = "CLEAN: No domain terms found in framework files."


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    scripts = root / "core" / "scripts"
    scripts.mkdir(parents=True)
    for name in GATE_FILES:
        shutil.copy2(CORE_SCRIPTS / name, scripts / name)
    _write(root, "core/config/domain-term-blocklist.txt", f"# synthetic\n{TERM}\n")
    _write(root, "core/config/seed-manifest.yaml", json.dumps(MANIFEST))
    _registry(root, OWN)
    _write(root, f"{REG}/{OWN}.yaml", f"environment_id: {OWN}\nnote: {TERM}\n")
    return root


def _gate(root: Path, env=None):
    r = subprocess.run([BASH, str(root / "core" / "scripts" / "domain-leak-check.sh")],
                       capture_output=True, text=True, timeout=300, env=env)
    return r.returncode, r.stdout, r.stderr


def _crlf_py_env(tmp_path: Path) -> dict:
    """PATH with a `py` that writes CRLF like Windows Python's text-mode stdout
    (guard-5779). Linux cannot produce that otherwise, so this is the only way
    the CRLF case can go red here."""
    bindir = tmp_path / "crlf-bin"
    bindir.mkdir()
    shim = bindir / "py"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        '[ "${1:-}" = "-3" ] && shift\n'
        f'"{sys.executable}" "$@" | "{sys.executable}" -c '
        "\"import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()"
        ".replace(b'\\n', b'\\r\\n'))\"\n"
        'exit "${PIPESTATUS[0]}"\n', encoding="utf-8", newline="")
    shim.chmod(0o755)
    return dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")


def test_gate_does_not_count_a_hit_in_an_unshipped_entry_and_names_it(project):
    rc, out, err = _gate(project)
    assert rc == 0, out + err
    assert out.strip().splitlines()[-1] == CLEAN
    assert f"not shipped: {REG}/{OWN}.yaml (1 hit(s))" in out


def test_gate_still_counts_a_hit_in_a_shipped_file(project):
    """Positive control: scoping must not blind the gate to what ships."""
    _write(project, "core/config/shipped-note.md", f"mentions {TERM}\n")
    rc, out, err = _gate(project)
    assert rc == 1, out + err
    assert f"LEAK: '{TERM}'" in out


def test_gate_counts_a_hit_in_the_generic_entry_because_it_ships(project):
    _write(project, GENERIC, f"environment_id: local\nnote: {TERM}\n")
    rc, out, err = _gate(project)
    assert rc == 1, out + err


def test_gate_scans_unscoped_and_says_so_when_the_scope_is_unresolvable(project):
    (project / "core" / "config" / "seed-manifest.yaml").unlink()
    rc, out, err = _gate(project)
    assert "WARN: could not resolve the seed include-set" in err
    assert rc == 1, "an unresolvable scope must over-report, never hide the entry's hit"


def test_gate_scans_unscoped_when_the_include_set_is_empty(project):
    """An empty include set still prints one newline byte, which passed a bare
    non-empty test and scoped EVERY hit out (fresh-eyes msg-20260923-043229-bravo-3735)."""
    _write(project, "core/config/seed-manifest.yaml", json.dumps({**MANIFEST, "include": []}))
    rc, out, err = _gate(project)
    assert "WARN: could not resolve the seed include-set" in err
    assert rc == 1, out + err


def test_gate_scope_survives_crlf_from_py(project, tmp_path):
    """A CR on every ship-list line made every whole-line match miss, so every
    hit was scoped out (fresh-eyes msg-20260923-043222-bravo-3734). The shipped
    leak must still count AND the scope must still apply."""
    _write(project, "core/config/shipped-note.md", f"mentions {TERM}\n")
    rc, out, err = _gate(project, _crlf_py_env(tmp_path))
    assert rc == 1, out + err
    assert f"LEAK: '{TERM}'" in out
    assert f"not shipped: {REG}/{OWN}.yaml (1 hit(s))" in out


def test_gate_reads_a_crlf_blocklist(project):
    """A CRLF checkout (core.autocrlf=true) leaves a CR on every blocklist line;
    `read` kept it, "Widgetron\r" matched nothing, and the scan printed CLEAN on
    every Windows box (guard-987). Bytes, so the case is pinned on every OS."""
    (project / "core" / "config" / "domain-term-blocklist.txt").write_bytes(
        f"# synthetic\r\n{TERM}\r\n".encode("utf-8"))
    _write(project, "core/config/shipped-note.md", f"mentions {TERM}\n")
    rc, out, err = _gate(project)
    assert rc == 1, out + err
    assert f"LEAK: '{TERM}'" in out, out
