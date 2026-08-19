""" — local-mirror materialization must be ATOMIC.

The own-cloud backend used bare ``Path.write_bytes`` to materialize local
mirrors (read-through download, machine-local _put, post-PUT cache sync, and
the merge path). ``write_bytes`` opens with O_TRUNC, so a concurrent reader in
the truncate-to-written window sees an EMPTY or PARTIAL file. Two measured
consequences: the worker fork-WM wipe (a ``wm set`` reading the transiently
empty file tripped the g-115-748 empty-file self-heal, which rebuilt the LIVE
working memory from template — every capture lane destroyed, cc-08 2/2), and
the g-115-3253 mid-run suite-log truncation/NUL class.

Three layers of pin, because each catches a different regression:

1. The helper itself is atomic (same-dir tmp + os.replace) and cleans up.
2. A reader hammering a file rewritten via the OLD idiom observes the empty
   window; via ``_atomic_write_local`` it never does. The old-idiom half is
   the POSITIVE CONTROL — if this platform cannot observe the window at all,
   the atomic assertion is vacuous, so the test skips loudly instead of
   passing silently (guard-1866 resolving-power discipline).
3. Every production materialization site routes through the helper — pinned
   by source scan for the bare idiom, so a fifth site cannot regress quietly.
   (The scan anchors on ``local.write_bytes(`` — the Path call shape — and
   deliberately not on the backend's ``write_bytes`` interface METHOD, which
   funnels through _put.)
"""
import os
import re
import sys
import threading
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

boto3 = pytest.importorskip("boto3")  # backend import needs  present

import owncloud_backend  # noqa: E402
from owncloud_backend import _atomic_write_local  # noqa: E402


# --- 1. helper correctness ------------------------------------------------

def test_helper_writes_bytes_and_leaves_no_tmp(tmp_path):
    target = tmp_path / "sub" / "wm.yaml"
    _atomic_write_local(target, b"hello: world\n")
    assert target.read_bytes() == b"hello: world\n"
    _atomic_write_local(target, b"hello: replaced\n")
    assert target.read_bytes() == b"hello: replaced\n"
    leftovers = [p for p in target.parent.iterdir() if p.name != target.name]
    assert leftovers == [], f"tmp residue left behind: {leftovers}"


def test_helper_failure_cleans_tmp_and_preserves_old_content(tmp_path, monkeypatch):
    target = tmp_path / "wm.yaml"
    target.write_bytes(b"original\n")

    def boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(owncloud_backend.os, "replace", boom)
    with pytest.raises(OSError, match="simulated replace failure"):
        _atomic_write_local(target, b"new\n")
    monkeypatch.undo()
    assert target.read_bytes() == b"original\n", "failed write must not touch the target"
    leftovers = [p for p in target.parent.iterdir() if p.name != target.name]
    assert leftovers == [], f"tmp residue left behind on failure: {leftovers}"


# --- 2. the race window, with its positive control ------------------------

def _hammer(write_one, target: Path, duration_s: float):
    """Rewrite `target` in a loop while a reader thread samples it.

    Returns (samples, empty_or_partial) observed by the reader.
    """
    payload = b"x" * 65536  # large enough that truncate->written is a real window
    target.write_bytes(payload)
    stop = threading.Event()
    observed = {"samples": 0, "bad": 0}

    def reader():
        while not stop.is_set():
            try:
                size = len(target.read_bytes())
            except FileNotFoundError:
                size = -1
            observed["samples"] += 1
            if size != len(payload):
                observed["bad"] += 1

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    deadline = time.monotonic() + duration_s
    while time.monotonic() < deadline:
        write_one(target, payload)
    stop.set()
    t.join(timeout=5)
    return observed["samples"], observed["bad"]


def test_atomic_write_never_exposes_the_truncate_window(tmp_path):
    # POSITIVE CONTROL first: the bare idiom must be observably non-atomic on
    # this platform, or the assertion below has no resolving power.
    control_samples, control_bad = _hammer(
        lambda p, b: p.write_bytes(b), tmp_path / "control.bin", duration_s=1.5)
    if control_bad == 0:
        pytest.skip(
            f"platform did not expose the write_bytes truncate window in "
            f"{control_samples} samples — atomicity assertion would be vacuous here")

    samples, bad = _hammer(
        _atomic_write_local, tmp_path / "atomic.bin", duration_s=1.5)
    assert bad == 0, (
        f"reader observed {bad}/{samples} empty-or-partial reads through "
        f"_atomic_write_local — the atomic idiom regressed (control observed "
        f"{control_bad}/{control_samples} through bare write_bytes)")


# --- 3. no production site uses the bare idiom ----------------------------

def test_no_bare_local_write_bytes_in_backend_source():
    src = (SCRIPTS / "owncloud_backend.py").read_text(encoding="utf-8")
    # Strip comments and docstrings crudely: scan code lines only.
    bare_sites = []
    for i, line in enumerate(src.splitlines(), 1):
        code = line.split("#", 1)[0]
        if re.search(r"\blocal\.write_bytes\(", code):
            bare_sites.append(f"L{i}: {line.strip()}")
    assert bare_sites == [], (
        "bare local.write_bytes materialization reintroduced — route through "
        "_atomic_write_local (g-115-6054):\n" + "\n".join(bare_sites))
