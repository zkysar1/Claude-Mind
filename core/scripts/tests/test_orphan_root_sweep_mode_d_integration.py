"""test_orphan_root_sweep_mode_d_integration.py — end-to-end Scan 4 ().

Companion to test_orphan_root_mode_d.py (which pins the Python predicate).
g-115-756 added _orphan_root_helpers.is_mode_d_cruft (predicate) AND the
bash sweep Scan 4 that invokes it via subprocess. The predicate is covered
by 12 unit tests; the bash glue / subprocess plumbing / stdout shape are
NOT — that integration is what this file pins.

Closes integration gap surfaced by sq-019 during g-115-756 close, filed
as g-115-876 Idea (asp-115, framework-hygiene).

## Why subprocess-with-fixture (not env-override)

orphan-root-sweep.sh sources _paths.sh which computes PROJECT_ROOT from
BASH_SOURCE[0] of the SOURCED file — env override does not work because
_paths.sh anchors paths to its own directory. The test must materialize
a real core/scripts/ tree, copy the script + its dependencies into it,
and invoke from that location. (Pattern parallels
test_path_resolution_virtual_prefix_cruft.py's subprocess invocation but
goes further: we copy scripts, not just call them in-place.)

## Cases

1. Mode D U+F03A cruft directory at synthetic PROJECT_ROOT → MODE-D ORPHAN
   line emitted with name-bytes hex showing the U+F03A bytes (ef 80 ba).
2. Mode D drive-letter directory (`C`) at synthetic PROJECT_ROOT → MODE-D
   ORPHAN line emitted.
3. Drive-letter-colon directory (`C:`) — accepts both literal `:` form and
   NTFS-remapped form (since the OS may rewrite). Assert at least one of
   the two shapes appears (single test fixture covers both possibilities).
4. Multiple cruft entries → MODE-D ORPHAN line for EACH (not just the first).
5. Clean PROJECT_ROOT (no Mode D entries) → "0 findings" summary, NO
   MODE-D ORPHAN lines.
6. Legitimate sibling dirs (alpha, world, core) at synthetic PROJECT_ROOT →
   not flagged (false-positive guard, complements predicate's negative cases).

Cross-refs: g-115-756 (Scan 4 addition), rb-939 (three-probe daemon
staleness), guard-554 (daemon restart before delete), rb-1029 (Read-tool
PUA empty-quotes verify via byte probe).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# Resolve bash absolutely so subprocess.run on Windows doesn't pick a different
# bash than the one Python's PATH advertises. Bare "bash" in subprocess.run on
# Windows can dispatch to WSL's `/bin/bash` (via Win32 CreateProcess's App Paths
# registry lookup) even when shutil.which finds Git Bash first — and the two
# bashes have very different default PATHs, with WSL omitting `/c/Windows`
# where `py.exe` lives. The sweep's Scan 4 needs `py -3` on PATH; with the
# wrong bash it errors `py: command not found` and emits zero findings.
# shutil.which returns Git Bash here because pytest is launched from a context
# where Git Bash is in PATH — matches production sweep behavior.
from _bash_helpers import BASH as BASH_PATH  # rb-1472: bin-first, clean-PATH-safe

TESTS_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = TESTS_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent

SWEEP_SH = CORE_SCRIPTS / "orphan-root-sweep.sh"
HELPERS_PY = CORE_SCRIPTS / "_orphan_root_helpers.py"
PATHS_SH = CORE_SCRIPTS / "_paths.sh"

def _copy_lf(src: Path, dst: Path) -> None:
    """Copy text file from src to dst with LF line endings.

    Git on Windows checks out shell scripts with CRLF by default
    (core.autocrlf=true). bash on Git Bash / MINGW interprets the
    embedded \\r as part of paths and commands (e.g.
    `cd: $'/path/core/scripts\\r/..'`, `syntax error near unexpected
    token 'elif'`), breaking sourcing. `shutil.copy2` preserves bytes
    verbatim, so the CRLF survives the copy and breaks the synthetic
    tree even though the real tree on the same machine works (because
    bash invocation paths differ — this discrepancy is exactly what
    the integration test exists to surface).

    Normalize to LF before writing so the synthetic scripts parse
    cleanly under Git Bash. Python files are unaffected (Python 3
    universal newlines handles CRLF transparently) but we use the same
    helper for them to keep the fixture uniform.
    """
    text = src.read_text(encoding="utf-8")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # newline="" disables write-time translation so the LF we wrote stays LF.
    dst.write_text(text, encoding="utf-8", newline="")


# U+F03A — NTFS private-use remapping of the colon character (Windows can't
# create files containing literal `:` in NTFS, so the kernel substitutes the
# PUA code point).  UTF-8 encoding is the 3-byte sequence ef 80 ba.
U_F03A = ""


@pytest.fixture
def synthetic_root(tmp_path: Path) -> Path:
    """Materialize a complete synthetic PROJECT_ROOT.

    Layout produced under tmp_path:
        tmp_path/
          core/scripts/
            _paths.sh                  (copied verbatim)
            orphan-root-sweep.sh       (copied verbatim)
            _orphan_root_helpers.py    (copied verbatim)
          delta/
            local-paths.conf           (synthetic — points WORLD/META OUTSIDE
                                        the synthetic PROJECT_ROOT so Scan 1
                                        does not also flag the Mode D cruft
                                        as Scan 1 orphans, isolating the
                                        Scan 4 signal under test)

    Returns tmp_path (the synthetic PROJECT_ROOT).
    """
    # 1. core/scripts skeleton
    synth_scripts = tmp_path / "core" / "scripts"
    synth_scripts.mkdir(parents=True)
    # Normalize CRLF→LF for bash-sourced files; see _copy_lf docstring for why
    # shutil.copy2 alone is insufficient on Windows checkouts.
    _copy_lf(PATHS_SH, synth_scripts / "_paths.sh")
    _copy_lf(SWEEP_SH, synth_scripts / "orphan-root-sweep.sh")
    _copy_lf(HELPERS_PY, synth_scripts / "_orphan_root_helpers.py")
    # _paths.sh probes for .python-shim/; not strictly needed for orphan-root-
    # sweep but copy if it exists so the synthetic root behaves like the real
    # one when invoked under MINGW/Git Bash.
    real_shim = CORE_SCRIPTS / ".python-shim"
    if real_shim.is_dir():
        synth_shim = synth_scripts / ".python-shim"
        synth_shim.mkdir()
        for child in real_shim.iterdir():
            if child.is_file():
                shutil.copy2(child, synth_shim / child.name)
                # Preserve executable bit for the shim scripts.
                try:
                    os.chmod(synth_shim / child.name, 0o755)
                except OSError:
                    pass

    # 2. Per-agent local-paths.conf — point WORLD/META OUTSIDE synthetic root.
    # If WORLD_PATH fell back to PROJECT_ROOT/world, dirname(WORLD_PATH) ==
    # PROJECT_ROOT and Scan 1 (world-parent) would also iterate top-level
    # entries → false signal mixing Scan 1 ORPHAN with Scan 4 MODE-D ORPHAN.
    # An external path keeps Scan 1 reading a different directory (which
    # may not exist — the sweep emits "world-parent does not exist —
    # skipping" and moves on, exactly what we want).
    agent_dir = tmp_path / "delta"
    agent_dir.mkdir()
    external_root = tmp_path / "_external"
    external_root.mkdir()
    # POSIX paths (forward slashes) — both Windows native and Git Bash accept
    # them, but backslashes (Path's str() on Windows) get interpreted by bash
    # as escape sequences inside double-quoted strings on some versions.
    world_path_str = (external_root / "world").as_posix()
    meta_path_str = (external_root / "meta").as_posix()
    conf_text = (
        f'WORLD_PATH="{world_path_str}"\n'
        f'META_PATH="{meta_path_str}"\n'
    )
    # newline="" disables CRLF translation on Windows — without it, write_text
    # writes "\r\n" and bash sources WORLD_PATH with a trailing \r in its value
    # (visible later as e.g. `cd: $'/path\r/..': No such file or directory`).
    (agent_dir / "local-paths.conf").write_text(
        conf_text, encoding="utf-8", newline=""
    )

    return tmp_path


def _invoke_sweep(synth_root: Path) -> subprocess.CompletedProcess:
    """Run synthetic core/scripts/orphan-root-sweep.sh and return result.

    Sets MIND_AGENT=delta so _paths.sh picks the synthetic delta/
    local-paths.conf, not the real one in the test-host repo.

    Windows-path handling: invoke bash with a CWD-relative script path
    (`core/scripts/orphan-root-sweep.sh`) rather than a drive-letter
    absolute path. Git Bash / MINGW does not accept `C:/...` style paths
    as `argv[1]` for the script-to-execute (only as cwd arg); passing
    the absolute drive-letter form silently errors "No such file or
    directory" even when the file exists. The relative form works
    universally because bash resolves it against the cwd we pass. cwd
    itself accepts the Windows-form string because subprocess hands it
    to CreateProcess (Win32 API), not the shell.
    """
    env = os.environ.copy()
    # 1: strip the ENTIRE ambient MIND_* namespace before invoking the
    # sweep so it resolves agent/world/meta purely from MIND_AGENT (set below) +
    # the synthetic local-paths.conf — immune to cross-test env pollution in the
    # full suite. The subprocess inherits os.environ; conftest's autouse
    # _restore_env_per_test restores only AGENT/WORLD/STORAGE_BACKEND, and the
    # prior code popped only WORLD/META — so a leaked ambient MIND_* (e.g.
    # MIND_SHELL, empirically proven to break the sweep when it points at a
    # non-GNU shell) survived into this subprocess and flipped the Mode-D
    # assertion. Sweeping the whole namespace closes the class regardless of
    # which var leaked (guard-652: isolate more than WORLD in test fixtures).
    for _ayoai_key in [k for k in env if k.startswith("MIND_")]:
        del env[_ayoai_key]
    env["MIND_AGENT"] = "delta"
    # PATH augmentation for `py` (Windows Python launcher) findability.
    # Pytest's bash subprocess (Git Bash via BASH_PATH) inherits Python's
    # PATH, but that PATH may not include `/c/Windows` where `py.exe` lives
    # (depends on how Python itself was launched). The sweep's Scan 4 helper
    # calls `py -3 -` to run the Mode D predicate; without `py` on PATH it
    # errors `py: command not found` (visible only if the script's
    # `2>/dev/null` is stripped) and the scan emits zero findings even when
    # cruft exists. Prepend `/c/Windows` (where py.exe is) AND `/mnt/c/Windows`
    # (WSL form, harmless on Git Bash) so the test works under both shells.
    #
    # IMPORTANT: do NOT prepend `/c/Windows/System32` — it contains Windows'
    # native `FIND.EXE`, which shadows GNU find. The sweep's Scan 4 needs
    # GNU find's `-mindepth/-maxdepth/-printf '%f\0'` flags; Windows FIND
    # responds with `FIND: Parameter format not correct` and the cruft
    # detection silently no-ops.
    path_prefix = ":".join(["/mnt/c/Windows", "/c/Windows"])
    env["PATH"] = path_prefix + ":" + env.get("PATH", "")
    return subprocess.run(
        [BASH_PATH, "core/scripts/orphan-root-sweep.sh"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(synth_root),
        timeout=60,
    )


# ─── Positive cases — Mode D cruft is detected ──────────────────────────


def test_u_f03a_cruft_detected_in_scan_4(synthetic_root: Path) -> None:
    """U+F03A-shaped name at PROJECT_ROOT triggers MODE-D ORPHAN line."""
    cruft = synthetic_root / f"C{U_F03A}"
    cruft.mkdir()
    result = _invoke_sweep(synthetic_root)
    assert result.returncode == 0, f"sweep exit {result.returncode}; stderr: {result.stderr}"
    assert "MODE-D ORPHAN" in result.stdout, (
        f"expected MODE-D ORPHAN line; stdout: {result.stdout[-800:]}"
    )
    # Verify the U+F03A byte sequence (ef 80 ba) appears in the hex dump,
    # not just any "name-bytes" suffix — that's the actual diagnostic
    # signal a human triages on.
    assert "ef80ba" in result.stdout, (
        f"expected hex 'ef80ba' (U+F03A bytes) in stdout; stdout: {result.stdout[-800:]}"
    )


def test_drive_letter_dir_detected(synthetic_root: Path) -> None:
    """Single uppercase letter dir at PROJECT_ROOT triggers MODE-D ORPHAN."""
    cruft = synthetic_root / "C"
    cruft.mkdir()
    result = _invoke_sweep(synthetic_root)
    assert result.returncode == 0, f"sweep exit {result.returncode}; stderr: {result.stderr}"
    # The MODE-D ORPHAN line for "C" should appear in Scan 4 output.
    assert "MODE-D ORPHAN" in result.stdout, (
        f"expected MODE-D ORPHAN line; stdout: {result.stdout[-800:]}"
    )
    # The directory name itself appears in the line ("MODE-D ORPHAN: C (dir, ...").
    assert ("MODE-D ORPHAN: C " in result.stdout) or ("MODE-D ORPHAN: C\t" in result.stdout), (
        f"expected the 'C' entry name in MODE-D ORPHAN line; stdout: {result.stdout[-800:]}"
    )


def test_drive_letter_colon_dir_detected(synthetic_root: Path) -> None:
    """Either literal `C:` OR U+F03A-substituted `C` triggers MODE-D ORPHAN.

    On NTFS the OS may rewrite literal `:` to U+F03A automatically; on
    POSIX it can be created as-is. The predicate accepts both shapes
    (test_letter_plus_literal_colon_detected pins this) — assert at
    least one shape gets emitted.
    """
    # Try literal first; fall back to U+F03A form if OS rejects it.
    literal = synthetic_root / "C:"
    try:
        literal.mkdir()
    except (OSError, ValueError):
        literal = synthetic_root / f"C{U_F03A}"
        literal.mkdir()
    result = _invoke_sweep(synthetic_root)
    assert result.returncode == 0, f"sweep exit {result.returncode}; stderr: {result.stderr}"
    assert "MODE-D ORPHAN" in result.stdout, (
        f"expected MODE-D ORPHAN for `C:` or `C\\uf03a`; stdout: {result.stdout[-800:]}"
    )


def test_multiple_cruft_entries_all_emitted(synthetic_root: Path) -> None:
    """Two distinct Mode D entries → two MODE-D ORPHAN lines (not collapsed)."""
    (synthetic_root / "C").mkdir()
    (synthetic_root / f"D{U_F03A}").mkdir()
    result = _invoke_sweep(synthetic_root)
    assert result.returncode == 0, f"sweep exit {result.returncode}; stderr: {result.stderr}"
    line_count = result.stdout.count("MODE-D ORPHAN")
    assert line_count >= 2, (
        f"expected ≥2 MODE-D ORPHAN lines (one per cruft entry); "
        f"got {line_count}; stdout: {result.stdout[-1000:]}"
    )


# ─── Negative cases — no false positives ────────────────────────────────


def test_clean_root_produces_zero_findings(synthetic_root: Path) -> None:
    """No Mode D entries → '0 findings' summary, no MODE-D ORPHAN lines.

    Note: even on a clean synthetic root, the sweep's Scan 1 may emit
    ORPHAN lines if the synthetic delta/ dir surfaces in dirname(WORLD_DIR)
    iteration. We isolate Scan 4 by asserting:
      - NO "MODE-D ORPHAN" lines appear (Scan 4 is silent)
      - Scan 4 header IS present (the scan did run, didn't error)
    """
    result = _invoke_sweep(synthetic_root)
    assert result.returncode == 0, f"sweep exit {result.returncode}; stderr: {result.stderr}"
    assert "MODE-D ORPHAN" not in result.stdout, (
        f"clean synthetic root should NOT trigger Scan 4 cruft lines; "
        f"stdout: {result.stdout[-800:]}"
    )
    # Scan 4 header confirms the scan executed.
    assert "Mode D scan" in result.stdout, (
        f"expected Scan 4 header in stdout; stdout: {result.stdout[-800:]}"
    )


def test_legitimate_siblings_not_flagged_by_scan_4(synthetic_root: Path) -> None:
    """alpha/, core/, world-shaped dirs at synthetic PROJECT_ROOT do NOT trip Scan 4.

    Mirrors test_orphan_root_mode_d.py's negative cases at the integration
    layer — the predicate's filtering must hold up through the bash pipe.
    Scan 1 / Scan 3 may still flag these (different scans, different rules)
    but Scan 4 specifically must not.
    """
    for name in ("alpha", "bravo", "knowledge", "scripts"):
        (synthetic_root / name).mkdir()
    result = _invoke_sweep(synthetic_root)
    assert result.returncode == 0, f"sweep exit {result.returncode}; stderr: {result.stderr}"
    assert "MODE-D ORPHAN" not in result.stdout, (
        f"legitimate sibling dir names should NOT trigger MODE-D ORPHAN; "
        f"stdout: {result.stdout[-1200:]}"
    )
