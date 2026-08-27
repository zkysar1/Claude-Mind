# Regression pins for the promotion preflight MODE gate ().
#
#  added mode DETECTION (report-only, filesystem bits).  makes
# ONE direction of it BLOCK, and reads the mode from git's INDEX rather than the
# filesystem, because the index is the dimension that PROMOTES: a promotion
# commits what `git ls-tree` says, and a bare `chmod +x` never travels
# (guard-844 — the downstream repair needed `git update-index --chmod=+x`).
#
# Originating incident: v2.9.4 shipped `core/githooks/*` as 100644 downstream,
# which disabled every gate in the chain. Nothing went red, because a
# non-executable hook does not error — it simply never runs.
#
# Runnable two ways:
#   py -3 core/scripts/tests/test_promotion_preflight_mode_gate.py   (standalone)
#   py -3 -m pytest core/scripts/tests/test_promotion_preflight_mode_gate.py -q
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "promotion-preflight.py"

HOOK = "core/githooks/pre-commit"
HOOK_BODY = "#!/usr/bin/env bash\necho gate ok\n"


def _run(src: Path, tgt: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--source", str(src), "--target", str(tgt), *extra],
        capture_output=True, text=True,
    )


def _json(src: Path, tgt: Path) -> tuple[int, dict]:
    p = _run(src, tgt, "--json")
    return p.returncode, json.loads(p.stdout)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True)


def _repo(base: Path, rel: str, content: str, executable: bool) -> Path:
    """A git repo carrying ONE framework file at a controlled INDEX mode.

    The mode is set with `git update-index --chmod`, never with a filesystem
    chmod: that is the operation whose result actually propagates through a
    promotion, and using it here means the fixture pins the same dimension the
    gate reads.
    """
    base.mkdir(parents=True, exist_ok=True)
    _git(base, "init", "-q")
    _git(base, "config", "user.email", "test@test.com")
    _git(base, "config", "user.name", "Test")
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    _git(base, "add", rel)
    _git(base, "update-index", "--chmod=+x" if executable else "--chmod=-x", rel)
    env = {**os.environ,
           "GIT_COMMITTER_DATE": "2025-01-01T00:00:00",
           "GIT_AUTHOR_DATE": "2025-01-01T00:00:00"}
    subprocess.run(["git", "-C", str(base), "commit", "-q", "-m", f"add {rel}"],
                   capture_output=True, text=True, check=True, env=env)
    return base


def _index_mode(repo: Path, rel: str) -> str:
    return _git(repo, "ls-tree", "-r", "HEAD", "--", rel).stdout.split()[0]


# ── The pin the goal names verbatim ────────────────────────────────────────
# "a fixture where source and target have identical content and differing
#  modes must return exit 2"

def test_identical_content_stripped_mode_blocks_exit_2(tmp_path):
    """Source non-exec over an executable target STRIPS the bit -> exit 2.

    Content is byte-identical, so every content-oriented check in the gate is
    clean and this exit code can only come from the mode predicate.
    """
    src = _repo(tmp_path / "src", HOOK, HOOK_BODY, executable=False)
    tgt = _repo(tmp_path / "tgt", HOOK, HOOK_BODY, executable=True)

    rc, d = _json(src, tgt)
    assert rc == 2, d.get("verdict")
    assert d["mode_strip_risk"] == [HOOK]
    assert d["verdict"] == "DRIFT"
    # The content lane is genuinely silent — proving the mode predicate alone
    # produced the block, rather than riding along with some other drift.
    assert d["orphan_risk_core"] == [] and d["target_ahead_core"] == []
    assert d["mode_only_differing"] == [HOOK]


def test_the_block_reason_reaches_the_human_report(tmp_path):
    """A verdict a reader cannot act on is not enforcement (guard-1958)."""
    src = _repo(tmp_path / "src", HOOK, HOOK_BODY, executable=False)
    tgt = _repo(tmp_path / "tgt", HOOK, HOOK_BODY, executable=True)

    p = _run(src, tgt)
    assert p.returncode == 2
    assert "MODE STRIP RISK" in p.stdout
    assert HOOK in p.stdout
    # The remedy must name the operation that actually travels, not `chmod`.
    assert "update-index" in p.stdout


# ── The benign mirror: promoting RESTORES the bit ──────────────────────────

def test_target_already_stripped_does_not_block(tmp_path):
    """Source exec over a non-exec target RESTORES the bit — promoting is the repair.

    Blocking this direction would hold the fix hostage to the defect, and it is
    the direction that is common in practice: measured 2026-08-25 against the
    live staging peer, 479 files sit here and ZERO sit in the strip direction.
    """
    src = _repo(tmp_path / "src", HOOK, HOOK_BODY, executable=True)
    tgt = _repo(tmp_path / "tgt", HOOK, HOOK_BODY, executable=False)

    rc, d = _json(src, tgt)
    assert d["mode_strip_risk"] == []
    assert d["mode_target_stripped"] == [HOOK]
    assert rc == 0, d.get("verdict")


def test_matching_modes_are_clean_both_ways(tmp_path):
    """Positive control on the predicate itself: same mode -> no mode drift."""
    for executable in (True, False):
        base = tmp_path / f"pair-{executable}"
        src = _repo(base / "src", HOOK, HOOK_BODY, executable=executable)
        tgt = _repo(base / "tgt", HOOK, HOOK_BODY, executable=executable)
        rc, d = _json(src, tgt)
        assert d["mode_differing"] == [], executable
        assert d["mode_strip_risk"] == [], executable
        assert rc == 0, (executable, d.get("verdict"))


# ── The INDEX-vs-FILESYSTEM half (guard-844) ───────────────────────────────

def test_index_mode_wins_over_a_filesystem_chmod(tmp_path):
    """`chmod +x` without `git update-index` must NOT clear the strip risk.

    This is the whole difference between g-360-09 (filesystem bits) and this
    gate. A filesystem chmod looks like a fix, satisfies `ls -l`, and does not
    survive the commit a promotion makes — so a gate reading the filesystem
    would go green on a repo that still promotes 100644.
    """
    src = _repo(tmp_path / "src", HOOK, HOOK_BODY, executable=False)
    tgt = _repo(tmp_path / "tgt", HOOK, HOOK_BODY, executable=True)

    # The tempting non-fix: make the source file executable on disk only.
    (src / HOOK).chmod(0o755)
    assert os.access(src / HOOK, os.X_OK), "fixture precondition: fs bit is set"
    assert _index_mode(src, HOOK) == "100644", "fixture precondition: index unchanged"

    rc, d = _json(src, tgt)
    assert d["mode_strip_risk"] == [HOOK], "filesystem chmod must not launder the strip risk"
    assert rc == 2
    assert "index" in d["mode_source"]


def test_update_index_chmod_is_what_actually_clears_it(tmp_path):
    """The sibling of the test above: the real fix DOES clear the block.

    Without this, the pin above is satisfiable by a gate that never goes green,
    which would be indistinguishable from a correct one.
    """
    src = _repo(tmp_path / "src", HOOK, HOOK_BODY, executable=False)
    tgt = _repo(tmp_path / "tgt", HOOK, HOOK_BODY, executable=True)
    assert _json(src, tgt)[0] == 2, "precondition: blocked before the fix"

    _git(src, "update-index", "--chmod=+x", HOOK)
    env = {**os.environ,
           "GIT_COMMITTER_DATE": "2025-01-02T00:00:00",
           "GIT_AUTHOR_DATE": "2025-01-02T00:00:00"}
    subprocess.run(["git", "-C", str(src), "commit", "-q", "-m", "chmod +x"],
                   capture_output=True, text=True, check=True, env=env)
    assert _index_mode(src, HOOK) == "100755"

    rc, d = _json(src, tgt)
    assert d["mode_strip_risk"] == []
    assert rc == 0, d.get("verdict")


# ── The positive control that keeps a zero honest ──────────────────────────

def test_no_git_no_exec_bits_reports_unavailable_not_clean(tmp_path):
    """A uniform zero means 'cannot see', and must never read as 'no drift'.

    With no git repo the index map is empty and resolve_exec falls back to the
    filesystem; with no execute bit anywhere, mode_bits_visible is False and the
    report says UNAVAILABLE. The gate must not manufacture a block from that.
    """
    src, tgt = tmp_path / "src", tmp_path / "tgt"
    for base in (src, tgt):
        p = base / HOOK
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(HOOK_BODY)

    rc, d = _json(src, tgt)
    assert d["mode_bits_visible"] is False
    assert d["mode_differing"] == [] and d["mode_strip_risk"] == []
    assert rc == 0
    assert "UNAVAILABLE" in _run(src, tgt).stdout


# ── The surface the incident actually escaped through ──────────────────────

def test_core_githooks_is_inside_the_scanned_framework_surface(tmp_path):
    """Before , `core/githooks` was not a FRAMEWORK_PATH at all.

    So NO mode check could have caught the incident regardless of how the
    predicate was written — the files were never walked. Pinning the surface is
    what keeps the predicate reachable.
    """
    sys.path.insert(0, str(SCRIPT.parent))
    import importlib.util
    spec = importlib.util.spec_from_file_location("_preflight", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "core/githooks" in mod.FRAMEWORK_PATHS

    # And end-to-end: a hook-only difference is visible to the gate at all.
    src = _repo(tmp_path / "src", HOOK, HOOK_BODY, executable=True)
    tgt = _repo(tmp_path / "tgt", HOOK, HOOK_BODY + "# target ahead\n", executable=True)
    _, d = _json(src, tgt)
    assert HOOK in (d["target_ahead_core"] + d["ambiguous_core"]), d


if __name__ == "__main__":
    import tempfile

    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        with tempfile.TemporaryDirectory() as td:
            try:
                fn(Path(td))
                print(f"PASS {name}")
            except Exception as e:  # noqa: BLE001 — standalone runner reports, never raises
                failures += 1
                print(f"FAIL {name}: {e}")
    print(f"{'FAIL' if failures else 'PASS'}: {failures} failure(s)")
    sys.exit(1 if failures else 0)
