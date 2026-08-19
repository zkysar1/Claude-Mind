"""Regression pins for core/scripts/check-no-hardcoded-secrets.sh.

The script had NO test coverage of any kind before g-306-105 (2026-08-01) --
verified by grep across core/scripts/tests and core/tests: zero files referenced
it. It is Gate 8 of the pre-commit chain, so the control that blocks committed
credentials was itself unguarded against regression.

These tests pin BOTH modes:

  default       staged scan (Gate 8) -- must keep blocking staged secrets
  --scan-head   committed scan (audit) -- added by g-306-105

The exit-code pin (test_scan_head_exits_nonzero_on_hit) exists because that is
the bug this change actually introduced and the positive control caught: the
first draft moved the report into a new branch but left `exit 1` inside the
staged arm only, so audit mode printed a complete findings report and still
exited 0. The recurring audit that consumes the exit code could never have
fired. A report nobody acts on is the same silence as no report, so the exit
code is pinned separately from the report text.

The fake keys below are AKIA-shaped but are not credentials -- they are
literal Z/test filler chosen to match the regex arm without resembling any
issued key. They are assembled from fragments at runtime so this file does not
itself trip the scanner it tests (which would otherwise require adding this
path to allowed_path() and thereby exempt the test file from the very check it
verifies).
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _runtime_bash import BASH  # noqa: E402  guard-580: never a bare "bash" argv[0]

SCANNER = Path(__file__).resolve().parents[1] / "check-no-hardcoded-secrets.sh"

# Assembled at runtime -- see module docstring.
_FAKE_A = "AKIA" + "ZZZZTESTFAKE" + "0000"
_FAKE_B = "AKIA" + "ZZZZTESTFAKE" + "1111"


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=False,
    )


def _run(repo, *args, env_extra=None):
    """Invoke the scanner with repo as cwd, the way the pre-commit hook does."""
    import os
    env = os.environ.copy()
    env.pop("ALLOW_SECRETS_IN_COMMIT", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [BASH, str(SCANNER), *args],
        cwd=str(repo), capture_output=True, text=True, check=False, env=env,
    )


@pytest.fixture
def repo(tmp_path):
    """A git repo whose HEAD carries a committed secret and nothing staged.

    This is the shape the scanner was blind to: the secret is already
    committed, so a diff-scoped scan sees an empty staged set.
    """
    r = tmp_path / "r"
    (r / "sub").mkdir(parents=True)
    _git(r.parent, "init", "-q", str(r))
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    (r / "sub" / "leaked.txt").write_text("key = %s\n" % _FAKE_A)
    (r / "sub" / "clean.txt").write_text("nothing to see\n")
    (r / "sub" / "skipped.txt").write_text(
        "key = %s  # secret-scanner: skip\n" % _FAKE_A
    )
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "pre-gate secret")
    return r


# ── the gap that motivated --scan-head ────────────────────────────────────

def test_staged_mode_is_blind_to_committed_secret(repo):
    """The documented gap, pinned so a future refactor cannot quietly 'fix' it.

    Staged mode SHOULD return 0 here. Nothing is staged, so a diff-scoped gate
    has nothing to look at even though HEAD carries a live-shaped key. This is
    correct behavior for a pre-commit gate and is exactly why the audit mode
    has to exist as a separate path rather than as a widening of this one.
    """
    assert _run(repo).returncode == 0


def test_scan_head_detects_committed_secret(repo):
    r = _run(repo, "--scan-head")
    assert r.returncode == 1
    assert "HEAD AUDIT" in r.stderr
    assert "sub/leaked.txt" in r.stderr


def test_scan_head_exits_nonzero_on_hit(repo):
    """Pinned separately from the report: the report and the exit code were
    decoupled in the first draft, and only the exit code drives the recurring
    audit."""
    assert _run(repo, "--scan-head").returncode == 1


def test_scan_head_clean_repo_exits_zero(tmp_path):
    r = tmp_path / "clean"
    r.mkdir()
    _git(tmp_path, "init", "-q", str(r))
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    (r / "a.txt").write_text("no secrets here\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "clean")
    assert _run(r, "--scan-head").returncode == 0


# ── bypasses apply per-mode, deliberately asymmetrically ──────────────────

def test_scan_head_honors_per_line_skip_marker(repo):
    """skipped.txt carries the same key as leaked.txt plus the marker."""
    out = _run(repo, "--scan-head").stderr
    assert "sub/leaked.txt" in out
    assert "sub/skipped.txt" not in out


def test_allow_secrets_env_does_not_silence_scan_head(repo):
    """ALLOW_SECRETS_IN_COMMIT is a per-COMMIT bypass.

    Honoring it in audit mode would let a stale exported value silence the
    entire audit -- the precise failure the audit exists to prevent.
    """
    r = _run(repo, "--scan-head", env_extra={"ALLOW_SECRETS_IN_COMMIT": "x"})
    assert r.returncode == 1


def test_allow_secrets_env_still_works_in_staged_mode(repo):
    """The commit-time bypass must keep working where it is meant to."""
    (repo / "sub" / "fresh.txt").write_text("k = %s\n" % _FAKE_B)
    _git(repo, "add", "sub/fresh.txt")
    r = _run(repo, env_extra={"ALLOW_SECRETS_IN_COMMIT": "justified"})
    assert r.returncode == 0
    assert "OVERRIDDEN" in r.stderr


# ── Gate 8 must still do its job (guard-914) ──────────────────────────────

def test_staged_secret_still_blocks(repo):
    """The pre-commit gate's actual job, unchanged by the mode split."""
    (repo / "sub" / "fresh.txt").write_text("k = %s\n" % _FAKE_B)
    _git(repo, "add", "sub/fresh.txt")
    r = _run(repo)
    assert r.returncode == 1
    assert "BLOCKED" in r.stderr
    assert "sub/fresh.txt" in r.stderr


def test_hook_invokes_scanner_with_no_arguments():
    """core/githooks/pre-commit calls the scanner bare.

    Pinned because the mode parser makes the empty-argument case load-bearing:
    if it ever stopped defaulting to staged, Gate 8 would silently change
    meaning for every commit in the repo.
    """
    hook = Path(__file__).resolve().parents[2] / "githooks" / "pre-commit"
    if not hook.exists():
        pytest.skip("pre-commit hook not present in this checkout")
    line = [
        ln for ln in hook.read_text().splitlines()
        if "check-no-hardcoded-secrets.sh" in ln and ln.strip().startswith("_gate")
    ]
    assert line, "pre-commit no longer wires the secret scanner"
    assert "--scan-head" not in line[0], "hook must invoke the STAGED gate, not the audit"


# ── binary scope: audit mode must match staged mode's scope () ──
#
# `git grep` emits a DIFFERENT line shape for a binary match --
# `Binary file HEAD:blob.bin matches` rather than `HEAD:path:lineno:content` --
# so the `sed 's/^HEAD://'` + `${gl%%:*}` extraction resolved the path field to
# the literal string "Binary file HEAD". Two consequences: the report named a
# file nobody could open, and the hit was suppressable by NEITHER bypass
# (allowed_path() is keyed on real repo paths, and a binary carries no line to
# hold a `# secret-scanner: skip` marker). One false positive would have wedged
# the  recurring HEAD audit red with no remedy available to an agent.
#
# Staged mode was never affected: it pre-filters binaries out via numstat
# (`[[ "$adds" == "-" ]] && continue`), which is why the fix is to make audit
# mode match the scope staged mode ALREADY declares, not to widen either.


def _binary_repo(tmp_path, name, *, also_text=False):
    """A repo whose HEAD carries the key inside a BINARY blob.

    with also_text=False the binary is the ONLY possible hit, so any report at
    all came from the binary.
    """
    r = tmp_path / name
    r.mkdir()
    _git(tmp_path, "init", "-q", str(r))
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    (r / "blob.bin").write_bytes(b"\x00\x01\x02" + _FAKE_A.encode() + b"\x00\xff")
    if also_text:
        (r / "plain.txt").write_text("key = %s\n" % _FAKE_B)
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "binary")
    return r


def test_scan_head_binary_only_repo_exits_zero(tmp_path):
    """Scope pin: a binary-only hit is OUT of scope, exactly as when staged."""
    r = _run(_binary_repo(tmp_path, "b1"), "--scan-head")
    assert r.returncode == 0, (
        "audit mode reported a binary that staged mode would have skipped; "
        "stderr=%s" % r.stderr
    )


def test_scan_head_never_reports_the_binary_pseudo_path(tmp_path):
    """Outcome 1: the report must never name the un-openable "Binary file HEAD".

    Asserted against a repo that ALSO has a real text hit, so the scanner is
    demonstrably producing a report -- an assertion that some string is absent
    proves nothing if nothing was printed at all.
    """
    out = _run(_binary_repo(tmp_path, "b2", also_text=True), "--scan-head").stderr
    assert "Binary file HEAD" not in out, out
    assert "blob.bin" not in out, out


def test_scan_head_still_detects_text_secret_alongside_a_binary(tmp_path):
    """POSITIVE CONTROL for the two pins above.

    Without this, `-I` could be silently disabling the whole HEAD scan and both
    preceding tests would still pass -- rc=0 and "no pseudo-path in the report"
    are exactly what a dead scanner produces. This is the g-115-4400 twin of the
    probe-was-live control the module docstring describes for the exit code.
    """
    r = _run(_binary_repo(tmp_path, "b3", also_text=True), "--scan-head")
    assert r.returncode == 1
    assert "plain.txt" in r.stderr, r.stderr


def test_staged_mode_binary_scope_is_unchanged(tmp_path):
    """The fix touches the HEAD arm only; staged mode must be byte-for-byte
    unaffected. Staging the binary alone still exits 0 via the numstat
    pre-filter, as it always did."""
    r = _binary_repo(tmp_path, "b4")
    (r / "late.bin").write_bytes(b"\x00" + _FAKE_B.encode() + b"\xff")
    _git(r, "add", "late.bin")
    assert _run(r).returncode == 0


# ── argument handling ─────────────────────────────────────────────────────

def test_unknown_argument_is_rejected(repo):
    r = _run(repo, "--bogus")
    assert r.returncode == 2
    assert "unknown argument" in r.stderr


def test_help_exits_zero(repo):
    r = _run(repo, "--help")
    assert r.returncode == 0
    assert "scan-head" in r.stdout
