"""Acceptance tests for core/scripts/provision-github-from-vault.sh ().

Hermetic — no SSH, no network. Two documented test seams stub the two external
dependencies:
  - PROVISION_GH_VAULT_FILE : read the vault from a local file (not over SSH)
  - PROVISION_GH_MOCK_STATE  : file-backed fake GitHub keys store (not curl)
plus SSH_KEY_DIR (a config knob) sandboxes keypair + ssh-config writes to tmp.

Acceptance criteria from the goal:
  - keypair generated 0600
  - registration POST idempotent across 2 consecutive runs (second = skip)
  - ssh config updated exactly once
  - verify output values-blind (the dummy token never lands in stdout/stderr/any file)
  - dormant-but-ready: vault without the token entry -> clear skip, exit 0
"""
import json
import os
import stat
import subprocess
from pathlib import Path

import sys
import pathlib
# guard-580: resolve bash explicitly — a bare 'bash' argv[0] hits System32 WSL on win32.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _bash_helpers import BASH  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "core" / "scripts" / "provision-github-from-vault.sh"

# Unique leak-detection sentinel. Deliberately NOT ghp_/github_pat_/AKIA-shaped
# so the commit-time secret scan (iteration-commit.sh content_secret_regex) does
# not false-positive on this obviously-fake fixture. The provisioner treats the
# token as opaque (never format-validates), so any distinctive string exercises
# the values-blind leak check identically.
DUMMY_TOKEN = "DUMMY_DEPLOY_ADMIN_TOKEN_g2083_fake_sentinel"
VAULT_KEY = "MIND_FLEET_GH_DEPLOYKEY_ADMIN_TOKEN"


def _run(tmp_path, *, vault_body, agent="alpha", extra_args=(), mock_state=None):
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir(exist_ok=True)
    vault_file = tmp_path / "vault.txt"
    vault_file.write_text(vault_body, encoding="utf-8")
    env = dict(os.environ)
    env["PROVISION_GH_VAULT_FILE"] = str(vault_file)
    env["SSH_KEY_DIR"] = str(ssh_dir)
    env["GH_REPO"] = "zkysar1/Ayoai-Mind"
    # Control the vault-key derivation (). VAULT_KEY above is a
    # hardcoded literal, but the script does not take it — it DERIVES
    # ${VAULT_KEY_PREFIX}_FLEET_GH_DEPLOYKEY_ADMIN_TOKEN, where VAULT_KEY_PREFIX
    # is the uppercased first segment of ENVIRONMENT_ID (default "ayoai-mind").
    # Since `env` starts as dict(os.environ), both inputs leaked ambiently, so
    # the fixture and the script agreed only by COINCIDENCE of that default.
    # Under any other env-id the script looks for a key the fixture never wrote,
    # finds none, takes the DORMANT-BUT-READY branch — which the first test
    # asserts is correct — and the seven non-dormant tests fail. Measured on
    # Linux 2026-07-27: bare run 8 passed; ENVIRONMENT_ID=zds-mind 1 passed /
    # 7 failed, exactly the non-dormant set.
    #
    # This is NOT only hygiene: this repo is the DEV SOURCE of the Ayoai-Mind ->
    # Claude-Mind -> ZDS-Mind promotion chain, whose downstream env-ids differ BY
    # CONSTRUCTION — the suite would pass here forever and ship broken to every
    # consumer.
    #
    # PIN the root input (matches the sibling provision-from-vault tests, and
    # keeps the derivation itself under test — if it breaks, these go dormant
    # and fail loudly), then CLEAR every override the script honors further down
    # the chain. The chain has THREE ambient entry points, and pinning only the
    # first defends against none of the others:
    #     ENVIRONMENT_ID  -> VAULT_KEY_PREFIX -> GH_DEPLOYKEY_VAULT_KEY
    # Both later links are `: "${VAR:=default}"` / `if [ -z "${VAR:-}" ]` forms,
    # so an ambient value wins outright and the pin above never gets consulted.
    # guard-1484: clear the value when the run must MEASURE resolution rather
    # than dictate it.
    env["ENVIRONMENT_ID"] = "ayoai-mind"
    env.pop("VAULT_KEY_PREFIX", None)
    env.pop("GH_DEPLOYKEY_VAULT_KEY", None)
    if mock_state is not None:
        env["PROVISION_GH_MOCK_STATE"] = str(mock_state)
    proc = subprocess.run(
        [BASH, str(SCRIPT), "--agent", agent, *extra_args],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
    )
    return proc, ssh_dir


def test_dormant_when_vault_lacks_token(tmp_path):
    """Vault without the token entry -> clear skip, exit 0 (bring-up never breaks)."""
    proc, ssh_dir = _run(tmp_path, vault_body="MIND_STORAGE_BACKEND=own-cloud\n")
    assert proc.returncode == 0, proc.stderr
    assert "DORMANT-BUT-READY" in proc.stderr
    # No keypair generated on the dormant path.
    assert not (ssh_dir / "alpha_deploy").exists()


def test_keypair_generated_mode_600(tmp_path):
    mock = tmp_path / "gh_keys.json"
    proc, ssh_dir = _run(
        tmp_path,
        vault_body=f"{VAULT_KEY}={DUMMY_TOKEN}\n",
        mock_state=mock,
    )
    assert proc.returncode == 0, proc.stderr
    priv = ssh_dir / "alpha_deploy"
    pub = ssh_dir / "alpha_deploy.pub"
    assert priv.exists() and pub.exists()
    # Enforced on POSIX, where the fleet runs and where the private-key mode is a
    # real security property. NOT assertable on Windows: NTFS has no POSIX mode
    # bits, so os.stat synthesizes st_mode and reports 0o666 for any writable file
    # whatever chmod did. On win32 this can never pass and can never catch a
    # regression — it measures the platform, not the script ().
    if os.name != "nt":
        mode = stat.S_IMODE(priv.stat().st_mode)
        assert mode == 0o600, f"private key mode {oct(mode)} != 0600"


def test_registration_idempotent_second_run_skips(tmp_path):
    mock = tmp_path / "gh_keys.json"
    vault = f"{VAULT_KEY}={DUMMY_TOKEN}\n"

    proc1, ssh_dir = _run(tmp_path, vault_body=vault, mock_state=mock)
    assert proc1.returncode == 0, proc1.stderr
    assert "registered WRITE deploy key" in proc1.stderr
    keys_after_1 = json.loads(mock.read_text(encoding="utf-8"))
    assert len(keys_after_1) == 1
    assert keys_after_1[0]["read_only"] is False

    # Second run: same keypair (already present), key already registered -> skip,
    # NOT a duplicate POST.
    proc2, _ = _run(tmp_path, vault_body=vault, mock_state=mock)
    assert proc2.returncode == 0, proc2.stderr
    assert "already registered as WRITE" in proc2.stderr
    keys_after_2 = json.loads(mock.read_text(encoding="utf-8"))
    assert len(keys_after_2) == 1, "second run must not create a duplicate deploy key"


def test_ssh_config_updated_exactly_once(tmp_path):
    mock = tmp_path / "gh_keys.json"
    vault = f"{VAULT_KEY}={DUMMY_TOKEN}\n"

    proc1, ssh_dir = _run(tmp_path, vault_body=vault, mock_state=mock)
    assert proc1.returncode == 0, proc1.stderr
    cfg = ssh_dir / "config"
    assert cfg.exists()
    body1 = cfg.read_text(encoding="utf-8")
    assert body1.count("Host github.com") == 1
    assert f"IdentityFile {ssh_dir}/alpha_deploy" in body1
    # POSIX-only: see the note on the private-key mode check above ().
    if os.name != "nt":
        assert stat.S_IMODE(cfg.stat().st_mode) == 0o600

    # Re-run: ssh-config must NOT gain a second block.
    proc2, _ = _run(tmp_path, vault_body=vault, mock_state=mock)
    assert proc2.returncode == 0, proc2.stderr
    body2 = cfg.read_text(encoding="utf-8")
    assert body2.count("Host github.com") == 1, "ssh-config block appended more than once"


def test_values_blind_token_never_leaks(tmp_path):
    mock = tmp_path / "gh_keys.json"
    proc, ssh_dir = _run(
        tmp_path,
        vault_body=f"{VAULT_KEY}={DUMMY_TOKEN}\n",
        mock_state=mock,
    )
    assert proc.returncode == 0, proc.stderr
    # Token must appear in NO output stream.
    assert DUMMY_TOKEN not in proc.stdout
    assert DUMMY_TOKEN not in proc.stderr
    # Token must appear in NO file the script wrote: ssh dir + mock keys store.
    for f in ssh_dir.rglob("*"):
        if f.is_file():
            assert DUMMY_TOKEN not in f.read_text(encoding="utf-8", errors="ignore"), f"token leaked into {f}"
    assert DUMMY_TOKEN not in mock.read_text(encoding="utf-8")


def test_dry_run_does_not_post_or_write_config(tmp_path):
    mock = tmp_path / "gh_keys.json"
    proc, ssh_dir = _run(
        tmp_path,
        vault_body=f"{VAULT_KEY}={DUMMY_TOKEN}\n",
        mock_state=mock,
        extra_args=("--dry-run",),
    )
    assert proc.returncode == 0, proc.stderr
    assert "would POST WRITE deploy key" in proc.stderr
    # Keypair IS generated (dry-run only skips the POST + config write).
    assert (ssh_dir / "alpha_deploy").exists()
    # No registration and no ssh-config write.
    assert not mock.exists() or json.loads(mock.read_text(encoding="utf-8")) == []
    assert not (ssh_dir / "config").exists()


def test_github_error_response_reported_not_crashed(tmp_path):
    """A GitHub API error (a JSON OBJECT, not a list) must surface a clean
    diagnostic + exit 1 — never an AttributeError traceback. Regression for the
    fresh-eyes-code finding on _key_status iterating a dict's keys."""
    mock = tmp_path / "gh_keys.json"
    # Seed the mock GET response with an error OBJECT (what 401/403/404 returns).
    mock.write_text(json.dumps({"message": "Bad credentials", "status": "401"}), encoding="utf-8")
    proc, ssh_dir = _run(
        tmp_path,
        vault_body=f"{VAULT_KEY}={DUMMY_TOKEN}\n",
        mock_state=mock,
    )
    assert proc.returncode != 0
    assert "could not read existing deploy keys" in proc.stderr
    assert "Bad credentials" in proc.stderr  # the real GitHub error is surfaced
    assert "Traceback" not in proc.stderr    # no python crash
    assert "AttributeError" not in proc.stderr


def test_readonly_conflict_reports_and_fails(tmp_path):
    """If our key material is already registered READ-ONLY, warn + non-zero (no auto-delete)."""
    mock = tmp_path / "gh_keys.json"
    vault = f"{VAULT_KEY}={DUMMY_TOKEN}\n"
    # First run registers a WRITE key so the pubkey material exists on disk.
    proc1, ssh_dir = _run(tmp_path, vault_body=vault, mock_state=mock)
    assert proc1.returncode == 0, proc1.stderr
    # Flip the mock entry to read_only=true, simulating a pre-existing RO deploy key.
    keys = json.loads(mock.read_text(encoding="utf-8"))
    keys[0]["read_only"] = True
    mock.write_text(json.dumps(keys), encoding="utf-8")
    # Re-run: same keypair present, now classified read-only -> conflict.
    proc2, _ = _run(tmp_path, vault_body=vault, mock_state=mock)
    assert proc2.returncode != 0
    assert "READ-ONLY" in proc2.stderr
