"""Regression test for the default `--out` path in
core/scripts/provision-from-vault.sh (g-335-253).

THE BUG: line 90 read `REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"`. SCRIPT_DIR is
core/scripts, so that expression yields **core/**, not the project root — and OUT
defaulted to `$REPO_ROOT/.env.local` = `<repo>/core/.env.local`, a path nothing
reads. A fresh container provisioning with default flags would have written its
entire credential set to a dead location and still exited 0 with a clean verify
block: silent failure on the exact bootstrap path this script exists to serve.

The name was the trap, not the arithmetic. `_paths.sh:18-22` binds the IDENTICAL
expression to `CORE_ROOT` and then defines `REPO_ROOT="$PROJECT_ROOT"` as a legacy
alias — so "REPO_ROOT" already means *repo root* everywhere else in this codebase.
Same expression, correct name there, wrong name here.

WHY THIS TEST EXISTS AND WHY IT LOOKS LIKE THIS: the sibling suite
(test_provision_from_vault_agent_scope.py) always passes an explicit `--out` to a
tmp path, precisely so the real `.env.local` is never a target. That discipline is
correct, and it is also why the DEFAULT path went untested — the one code path
nobody could safely exercise in-tree is the one that broke. This test resolves the
tension by running the script from a **fake repo tree** in tmp: the copy's own
SCRIPT_DIR anchors the derivation inside tmp, so the default `--out` is exercised
for real while the live credential file is untouchable by construction.

The assertion is deliberately two-sided. Asserting only "lands at fake root" would
also pass if the script wrote to BOTH locations; asserting core/.env.local is
absent is what pins the actual defect.
"""
import os
import shutil
import subprocess

import pytest

from pathlib import Path

import sys
import pathlib
# guard-580: resolve bash explicitly — a bare 'bash' argv[0] hits System32 WSL on win32.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _bash_helpers import BASH  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "core" / "scripts" / "provision-from-vault.sh"

VAULT_BODY = "MIND_MIND_AWS_ACCESS_KEY_ID=PLACEHOLDER_AKID\nMIND_STORAGE_BACKEND=own-cloud\n"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")


def _fake_repo(tmp_path):
    """Build <tmp>/core/scripts/provision-from-vault.sh from the real script.

    The copy's BASH_SOURCE anchors SCRIPT_DIR inside tmp, so the root derivation
    under test runs against a tree whose layout mirrors the real repo (root/core/
    scripts) without the real repo being reachable as a write target.
    """
    scripts = tmp_path / "core" / "scripts"
    scripts.mkdir(parents=True)
    script = scripts / "provision-from-vault.sh"
    script.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    script.chmod(0o755)
    return script


def _run_with_default_out(tmp_path):
    """Run the copied script with NO --out flag, so the default is exercised."""
    script = _fake_repo(tmp_path)

    vault = tmp_path / "vault"
    vault.write_text(VAULT_BODY, encoding="utf-8")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "ssh"
    stub.write_text(f'#!/usr/bin/env bash\ncat "{vault}"\n', encoding="utf-8")
    stub.chmod(0o755)
    bootstrap = tmp_path / "bootstrap.pem"
    bootstrap.touch()  # FROM-state guard only checks presence

    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    # PATH-prepend alone DOES NOT stub ssh on Git Bash () — MSYS bash
    # prepends its own /mingw64/bin:/usr/bin ahead of the inherited Windows PATH,
    # so this bindir lands mid-list and real OpenSSH wins. VAULT_SSH_BIN is the
    # explicit seam; the PATH prepend is kept for POSIX.
    env["VAULT_SSH_BIN"] = str(stub)
    env["BOOTSTRAP_KEY_PATH"] = str(bootstrap)
    env["VAULT_SSH_HOST"] = "stub.invalid"
    env["VAULT_REMOTE_PATH"] = "/stub/vault"
    # PIN the root input (keeps the derivation itself under test), then CLEAR
    # every override the script honors further DOWN the chain:
    #     ENVIRONMENT_ID -> VAULT_KEY_PREFIX -> prefix filter (:189)
    # Both later links are `: "${VAR:=default}"` / `if [ -z "${VAR:-}" ]` forms,
    # so an ambient value wins outright and the pin above is never consulted —
    # pinning only the first link defends against none of the others.
    # guard-1484: clear the value when the run must MEASURE resolution rather
    # than dictate it. Measured before this fix (): baseline 2 passed,
    # VAULT_KEY_PREFIX=BOGUS 2 FAILED.
    env["ENVIRONMENT_ID"] = "ayoai-mind"
    env.pop("VAULT_KEY_PREFIX", None)
    env.pop("OUT", None)  # the default is the whole point
    # VAULT_SSH_USER (:106, used at :145/:148) is the one remaining override this
    # test does not pin. It is inert HERE only because the VAULT_SSH_BIN stub above
    # ignores its argv — not because the script ignores it. Cleared so hermeticity
    # rests on the script's contract rather than on the stub's current shape.
    env.pop("VAULT_SSH_USER", None)

    proc = subprocess.run(
        [BASH, str(script)],  # NO --out
        capture_output=True, text=True, env=env, cwd=str(tmp_path),
    )
    return proc


def test_default_out_lands_at_project_root_not_core(tmp_path):
    """The default --out resolves to <root>/.env.local, never <root>/core/.env.local."""
    proc = _run_with_default_out(tmp_path)

    at_root = tmp_path / ".env.local"
    at_core = tmp_path / "core" / ".env.local"

    assert at_root.exists(), (
        "default --out did not write to the project root "
        f"(rc={proc.returncode}); stderr tail: {proc.stderr[-600:]}"
    )
    # The load-bearing half: pins the  defect specifically.
    assert not at_core.exists(), (
        "default --out wrote to core/.env.local — REPO_ROOT is resolving to "
        "CORE_ROOT again (g-335-253 regression)"
    )


def test_default_out_file_is_mode_600(tmp_path):
    """Credential file stays 0600 on the default path (secrets.md / guard-724).

    The mode guarantee is documented for the script as a whole; the default path
    is a distinct code path from the explicit --out the sibling suite covers, so
    it gets its own assertion rather than inheriting the claim.
    """
    _run_with_default_out(tmp_path)
    at_root = tmp_path / ".env.local"
    assert at_root.exists(), "default --out produced no file"
    # Enforced on POSIX (where the fleet runs); not assertable on Windows, where
    # NTFS has no POSIX mode bits and os.stat synthesizes 0o666 for any writable
    # file whatever chmod did — a guaranteed red that measures the platform, not
    # the script ().
    if os.name != "nt":
        assert (at_root.stat().st_mode & 0o777) == 0o600, (
            f"expected mode 600, got {oct(at_root.stat().st_mode & 0o777)}"
        )
