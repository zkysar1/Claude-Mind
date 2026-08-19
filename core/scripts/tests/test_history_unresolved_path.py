"""Regression tests for  — `history` must not return a false-absent
verdict on the recovery layer.

Before the fix, `history list` collapsed THREE distinct conditions into one
"No history for <p>" line at exit 0:

  1. a path under no governed root (a typo, or genuinely outside world/meta/
     agent/.claude)
  2. a `world/`- or `meta/`-prefixed VIRTUAL path — resolved against
     PROJECT_ROOT, which is not where those roots live (both are external per
     local-paths.conf), so the lookup landed under no governed root
  3. an honest empty store

Only (3) is really "no history". (1) is an error rendered as data, and (2)
actively HID an existing snapshot set — measured on the live box before the
fix: `world/self-evolution.jsonl` reported "No history" while the identical
file by absolute path listed 35 versions.

The mechanism was `_fileops._find_history_snapshots` returning `[]` when
`resolve_base_dir` returns None, i.e. an ERROR condition collapsed into a DATA
condition. `history.py` already carried the correct loud wrapper; `cmd_list`
simply bypassed it.

WHY SUBPROCESS: these run the real CLI as a child process rather than calling
`history.main()` in-process. `_paths` binds WORLD_DIR/META_DIR at import time,
so an in-process test depends on module-reload ordering and can pass against a
stale binding — the subprocess exercises the literal production arg shape
instead (guard-920).

WHY THE `no_history` CASE IS PINNED TOO: the fix must NOT be "error whenever
the file is missing". Snapshots outlive the file they describe, and listing
them after a delete is exactly when they matter. The discriminator is "is the
path under a governed root", never "does the file exist" — so
test_under_root_missing_file_stays_quiet is the guard against over-tightening,
whose failure mode would be a loud error on the one workflow this tool exists
to serve.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent


@pytest.fixture()
def roots():
    """A tmp world + meta pair, wired via MIND_WORLD / MIND_META."""
    with tempfile.TemporaryDirectory(prefix="hist_unres_world_") as w, \
            tempfile.TemporaryDirectory(prefix="hist_unres_meta_") as m:
        yield Path(w), Path(m)


def _run(roots, *args):
    """Invoke the real history.py CLI as a subprocess."""
    world, meta = roots
    env = dict(os.environ)
    env["MIND_WORLD"] = str(world)
    env["MIND_META"] = str(meta)
    # Mandatory on an own-cloud box: without it a tmp-world write derives a
    # PRODUCTION S3 key and can truncate the real store (guard-955, rb-2983).
    env["STORAGE_BACKEND"] = "local"
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "history.py"), *args],
        cwd=str(SCRIPTS), env=env, capture_output=True, text=True,
    )


def _seed_snapshot(base: Path, rel: str, agent: str = "alpha") -> Path:
    """Create one legacy-layout snapshot for `rel` under `base`."""
    target = base / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("current contents\n", encoding="utf-8")
    snap_dir = base / ".history" / rel
    snap_dir.mkdir(parents=True, exist_ok=True)
    snap = snap_dir / f"2026-07-01T10-00-00_{agent}{Path(rel).suffix}"
    snap.write_text("older contents\n", encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# (1) Unresolvable / out-of-root paths are ERRORS, not empty stores
# ---------------------------------------------------------------------------

def test_out_of_root_existing_file_exits_nonzero(roots):
    """A real file outside every governed root must not read as 'no history'."""
    r = _run(roots, "list", "/etc/hostname")
    assert r.returncode != 0, f"expected non-zero, got {r.returncode}: {r.stdout}"
    assert "not under" in r.stderr, r.stderr
    assert "No history" not in r.stdout, r.stdout


def test_out_of_root_missing_file_exits_nonzero(roots):
    """A typo'd absolute path is an error, not a healthy empty store."""
    r = _run(roots, "list", "/nonexistent/path/foo.jsonl")
    assert r.returncode != 0, f"expected non-zero, got {r.returncode}: {r.stdout}"
    assert "not under" in r.stderr, r.stderr
    assert "No history" not in r.stdout, r.stdout


# ---------------------------------------------------------------------------
# (2) Virtual prefixes resolve to the EXTERNAL roots, not PROJECT_ROOT
# ---------------------------------------------------------------------------

def test_world_prefix_resolves_to_world_root(roots):
    """`world/x` must find snapshots under WORLD_DIR.

    This is the false-NEGATIVE half: pre-fix this printed 'No history' while
    the snapshot below existed, because `world/` anchored to PROJECT_ROOT.
    """
    world, _meta = roots
    _seed_snapshot(world, "notes.md")
    r = _run(roots, "list", "world/notes.md")
    assert r.returncode == 0, r.stderr
    assert "1 versions" in r.stdout, r.stdout
    assert "No history" not in r.stdout, r.stdout


def test_meta_prefix_resolves_to_meta_root(roots):
    """Same for `meta/` — it was broken identically and by the same cause."""
    _world, meta = roots
    _seed_snapshot(meta, "strategy.yaml")
    r = _run(roots, "list", "meta/strategy.yaml")
    assert r.returncode == 0, r.stderr
    assert "1 versions" in r.stdout, r.stdout


def test_world_prefix_matches_absolute_equivalent(roots):
    """The virtual and absolute forms of one file must agree.

    Their DISAGREEMENT is precisely what the live measurement found (35
    versions vs 'No history'), so pin the equivalence rather than only the
    absolute form's correctness.
    """
    world, _meta = roots
    target = _seed_snapshot(world, "sub/dir/thing.jsonl")
    virtual = _run(roots, "list", "world/sub/dir/thing.jsonl")
    absolute = _run(roots, "list", str(target))
    assert virtual.returncode == absolute.returncode == 0, virtual.stderr
    # Bodies differ only in the echoed path; the version COUNT must match.
    assert "1 versions" in virtual.stdout and "1 versions" in absolute.stdout, (
        f"virtual={virtual.stdout!r} absolute={absolute.stdout!r}"
    )


# ---------------------------------------------------------------------------
# (3) The one honest empty case still exits 0 — guard against over-tightening
# ---------------------------------------------------------------------------

def test_deleted_file_with_snapshots_still_lists(roots):
    """THE property the retired test below was protecting, pinned directly.

    g-115-4181 wrote `test_under_root_missing_file_stays_quiet`, which asserted
    exit 0 for `never-existed.jsonl` and warned in its own docstring: "snapshots
    outlive their file, so 'missing on disk' must never by itself become an
    error. If this test ever fails, the fix has over-tightened into the
    delete-then-restore workflow."

    g-115-5021 asks for exactly that change, so the two goals appeared to
    conflict. They do not, and the reason is that THE RETIRED TEST DID NOT
    EXERCISE ITS OWN RATIONALE: `never-existed.jsonl` has no snapshots, so it is
    not the delete-then-restore case at all. A file that was ever snapshotted
    HAS snapshots, and the snapshot lookup runs BEFORE the missing-file check,
    so that workflow never reaches the new branch.

    This test pins the real property — save, delete, still list — instead of a
    path that merely resembles it. Measured on the live store 2026-08-11 before
    the fix and again after: `1 versions`, exit 0, both times.
    """
    world, meta = roots
    f = world / "deleted-but-snapshotted.md"
    f.write_text("v1\n", encoding="utf-8")
    # history.py has no `save` subcommand — snapshots are written by
    # _fileops.save_history, which is what history-save.sh shells out to. Going
    # through the real writer (rather than hand-building a .history tree) is the
    # point: a fabricated snapshot layout would pin this test to a directory
    # shape instead of to the behaviour.
    env = dict(os.environ)
    env.update(MIND_WORLD=str(world), MIND_META=str(meta),
               STORAGE_BACKEND="local", CORE_SCRIPTS=str(SCRIPTS))
    saved = subprocess.run(
        [sys.executable, "-c",
         "import os, sys; sys.path.insert(0, os.environ['CORE_SCRIPTS']);"
         "from _fileops import save_history, resolve_base_dir;"
         "b = resolve_base_dir(sys.argv[1]);"
         "sys.exit(1) if b is None else save_history(sys.argv[1], b, 'alpha', 'pin')",
         str(f)],
        cwd=str(SCRIPTS), env=env, capture_output=True, text=True,
    )
    assert saved.returncode == 0, f"save failed: {saved.stderr}"
    f.unlink()
    r = _run(roots, "list", str(f))
    assert r.returncode == 0, f"{r.returncode}: {r.stderr}"
    assert "History for" in r.stdout, r.stdout


def test_under_root_missing_file_with_no_snapshots_is_an_error(roots):
    """: in-scope + missing + NO snapshots is an ERROR, not an empty
    store.

    Before this, a typo'd path under a governed root returned the same
    reassuring "No history" at exit 0 as a real file with an empty store, so a
    mistyped path read as "nothing to lose". Same shape as the defect
    g-115-4181 fixed one layer out (an error condition rendered as data) — it
    simply stopped one branch short.

    The sibling above is what keeps this from over-tightening; both must pass.
    """
    world, _meta = roots
    r = _run(roots, "list", str(world / "never-existed.jsonl"))
    assert r.returncode == 1, f"{r.returncode}: {r.stdout}{r.stderr}"
    assert "does not exist" in r.stderr, r.stderr


def test_under_root_existing_file_no_snapshots_stays_quiet(roots):
    """Same, for a file that exists but was never snapshotted."""
    world, _meta = roots
    f = world / "fresh.md"
    f.write_text("brand new\n", encoding="utf-8")
    r = _run(roots, "list", str(f))
    assert r.returncode == 0, f"{r.returncode}: {r.stderr}"
    assert "No history" in r.stdout, r.stdout
