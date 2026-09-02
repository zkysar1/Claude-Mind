"""Acceptance tests for `--append-key` on core/scripts/provision-from-vault.sh
(gap-054, satisfied-by-extension rather than by a forged skill).

THE INCIDENT THIS PINS. The script's default mode is a fresh-container
bootstrap: it TRUNCATES $OUT and rewrites it from the vault. That is correct on
a file the script fully GENERATES and catastrophic on one that has ACCRETED keys
across many sessions (rb-5914 — the discriminator is WHO OWNS THE FILE'S
CONTENTS, not whether the tool rewrites or merges). Measured on cc-03, `--force`
would have replaced 25 accreted keys with the vault's 5, destroying
ANTHROPIC_API_KEY / ARC_API_KEY / AYO_OPERATOR_KEY / STORAGE_BACKEND
(guard-2023). Three agents on three boxes inside nine hours each read the
script, recognised the trap, and hand-built the safe append; a convention doc
did not stop the third (bravo encoded it 07:37, echo still hit it 15:40).

test_force_still_truncates is the load-bearing NEGATIVE CONTROL (guard-1220 — a
predicate must reject as well as accept). --force's truncation is the CORRECT
bootstrap behaviour and must survive this change; a patch that made the script
merge unconditionally would pass every other test here while silently breaking
fresh-container provisioning.

test_bare_run_signposts_append_key pins the highest-value half. The FROM-state
guard used to say only "Re-run with --force to overwrite" — i.e. the tool
actively recommended the destructive branch to the exact caller who wanted to
add one key. That message is what each of the three agents had to know to
disbelieve.

Hermetic — no SSH, no network. Same VAULT_SSH_BIN seam as the sibling
test_provision_from_vault_agent_scope.py: PATH-stubbing `ssh` alone does not
work on Git Bash (g-115-3180).
"""
import os
import subprocess
import sys
import pathlib
from pathlib import Path

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _bash_helpers import BASH  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "core" / "scripts" / "provision-from-vault.sh"

# Obviously-fake placeholders, deliberately not AKIA-/token-shaped so the
# commit-time secret scan does not false-positive. The mapper treats values as
# opaque, so any distinctive string exercises the leak checks identically.
VAULT_BODY = (
    "MIND_MIND_AWS_ACCESS_KEY_ID=VAULT_AKID_VALUE\n"
    "MIND_STORAGE_BACKEND=own-cloud\n"
    "MIND_LODESTAR_CONTRIBUTE_KEY=VAULT_LODESTAR_VALUE\n"
)
VAULT_VALUES = ["VAULT_AKID_VALUE", "VAULT_LODESTAR_VALUE"]

# A LIVE, ACCRETED .env.local: MIND_AWS_ACCESS_KEY_ID (so the FROM-state guard
# sees it as provisioned) plus keys the vault has never heard of. These are the
# ones --force deletes.
ACCRETED = (
    "MIND_AWS_ACCESS_KEY_ID=LIVE_AKID\n"
    "ANTHROPIC_API_KEY=LIVE_ANTHROPIC\n"
    "ARC_API_KEY=LIVE_ARC\n"
    "AYO_OPERATOR_KEY=LIVE_OPERATOR\n"
    "STORAGE_BACKEND=own-cloud\n"
)
ACCRETED_ONLY = ["ANTHROPIC_API_KEY", "ARC_API_KEY", "AYO_OPERATOR_KEY"]


def _run(tmp_path, args, *, live=ACCRETED, name="env", agent="alpha"):
    vault = tmp_path / f"vault.{name}"
    vault.write_text(VAULT_BODY, encoding="utf-8")
    bindir = tmp_path / f"bin.{name}"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "ssh"
    stub.write_text(f'#!/usr/bin/env bash\ncat "{vault}"\n', encoding="utf-8")
    stub.chmod(0o755)
    bootstrap = tmp_path / "bootstrap.pem"
    bootstrap.touch()  # the guard checks presence only

    out = tmp_path / name
    if live is not None:
        out.write_text(live, encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["VAULT_SSH_BIN"] = str(stub)
    env["BOOTSTRAP_KEY_PATH"] = str(bootstrap)
    env["VAULT_SSH_HOST"] = "stub.invalid"
    env["VAULT_REMOTE_PATH"] = "/stub/vault"
    env["ENVIRONMENT_ID"] = "ayoai-mind"
    # Clear every override further down the resolution chain, or an ambient
    # value wins outright and the pin above is never consulted (guard-1484).
    env.pop("VAULT_KEY_PREFIX", None)
    env.pop("VAULT_SSH_USER", None)
    roster = tmp_path / f"agents.{name}"
    roster.mkdir(exist_ok=True)
    (roster / agent).mkdir(exist_ok=True)
    env["MIND_AGENTS_ROOT"] = str(roster)
    env["MIND_AGENT"] = agent

    proc = subprocess.run(
        [BASH, str(SCRIPT), "--out", str(out), *args],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
    )
    body = out.read_text(encoding="utf-8") if out.exists() else ""
    return proc, out, body


def _keys(body):
    return [ln.split("=", 1)[0] for ln in body.splitlines()
            if "=" in ln and not ln.startswith("#")]


def test_append_preserves_accreted_keys(tmp_path):
    """The incident, reduced: adding one vault key must not cost the others."""
    proc, _out, body = _run(tmp_path, ["--append-key", "LODESTAR_CONTRIBUTE_KEY"])
    assert proc.returncode == 0, proc.stderr
    for k in ACCRETED_ONLY:
        assert k in _keys(body), f"{k} was destroyed by an append"
    assert "LODESTAR_CONTRIBUTE_KEY=VAULT_LODESTAR_VALUE" in body
    assert "LIVE_ANTHROPIC" in body and "LIVE_ARC" in body


def test_force_still_truncates(tmp_path):
    """NEGATIVE CONTROL (guard-1220). --force's truncate-rewrite is the CORRECT
    fresh-container behaviour. A patch that merged unconditionally would pass
    every other test here and silently break bootstrap provisioning."""
    proc, _out, body = _run(tmp_path, ["--force"], name="forced")
    assert proc.returncode == 0, proc.stderr
    for k in ACCRETED_ONLY:
        assert k not in _keys(body), (
            "--force no longer truncates — the bootstrap path was broken by the "
            "append-key change"
        )


def test_bare_run_signposts_append_key(tmp_path):
    """The FROM-state guard must point at --append-key, not only --force. The
    old message recommended the destructive branch to the caller who wanted to
    add one key — that is what each of the three agents had to disbelieve."""
    proc, _out, body = _run(tmp_path, [], name="bare")
    assert proc.returncode == 0
    assert "--append-key" in proc.stderr, (
        "the already-provisioned message does not name the safe path"
    )
    assert body == ACCRETED, "a bare run must not modify a provisioned file"


def test_differing_value_refuses_without_allow_replace(tmp_path):
    """A live key with a different value is a ROTATION and must not happen by
    accident."""
    live = ACCRETED + "LODESTAR_CONTRIBUTE_KEY=OLD_LIVE_VALUE\n"
    proc, _out, body = _run(tmp_path, ["--append-key", "LODESTAR_CONTRIBUTE_KEY"],
                            live=live, name="refuse")
    assert proc.returncode == 3, proc.stderr
    assert "--allow-replace" in proc.stderr
    assert body == live, "a refused rotation must leave the file byte-identical"


def test_allow_replace_rotates_without_duplicating_or_losing(tmp_path):
    live = ACCRETED + "LODESTAR_CONTRIBUTE_KEY=OLD_LIVE_VALUE\n"
    proc, _out, body = _run(
        tmp_path, ["--append-key", "LODESTAR_CONTRIBUTE_KEY", "--allow-replace"],
        live=live, name="rotate")
    assert proc.returncode == 0, proc.stderr
    assert "OLD_LIVE_VALUE" not in body
    assert body.count("LODESTAR_CONTRIBUTE_KEY=") == 1, "rotation duplicated the key"
    for k in ACCRETED_ONLY:
        assert k in _keys(body)


def test_idempotent_when_already_current(tmp_path):
    live = ACCRETED + "LODESTAR_CONTRIBUTE_KEY=VAULT_LODESTAR_VALUE\n"
    proc, _out, body = _run(tmp_path, ["--append-key", "LODESTAR_CONTRIBUTE_KEY"],
                            live=live, name="idem")
    assert proc.returncode == 0
    assert body == live, "a no-op must not rewrite the file"
    assert body.count("LODESTAR_CONTRIBUTE_KEY=") == 1


def test_append_is_values_blind(tmp_path):
    """Invariant 5 / guard-1270: printing a secret to confirm it is the leak."""
    proc, _out, _body = _run(tmp_path, ["--append-key", "LODESTAR_CONTRIBUTE_KEY"],
                             name="blind")
    combined = proc.stdout + proc.stderr
    for v in VAULT_VALUES + ["LIVE_ANTHROPIC", "LIVE_ARC", "LIVE_OPERATOR"]:
        assert v not in combined, f"value {v!r} was printed"


def test_missing_key_names_only(tmp_path):
    proc, _out, body = _run(tmp_path, ["--append-key", "NOT_IN_VAULT"], name="missing")
    assert proc.returncode == 1
    assert "NOT_IN_VAULT" in proc.stderr
    for v in VAULT_VALUES:
        assert v not in proc.stdout + proc.stderr, "listing leaked a value"
    assert body == ACCRETED, "a failed lookup must not modify the file"


def test_append_and_force_are_mutually_exclusive(tmp_path):
    proc, _out, body = _run(
        tmp_path, ["--append-key", "LODESTAR_CONTRIBUTE_KEY", "--force"], name="excl")
    assert proc.returncode == 2
    assert body == ACCRETED


def test_missing_trailing_newline_does_not_splice(tmp_path):
    """A live file whose last line has no newline would otherwise get the new
    key spliced onto it, corrupting BOTH keys."""
    live = ACCRETED.rstrip("\n")  # no trailing newline
    proc, _out, body = _run(tmp_path, ["--append-key", "LODESTAR_CONTRIBUTE_KEY"],
                            live=live, name="nonl")
    assert proc.returncode == 0, proc.stderr
    assert "own-cloudLODESTAR_CONTRIBUTE_KEY" not in body
    assert "STORAGE_BACKEND=own-cloud" in body
    # an ACCRETED-ONLY key: STORAGE_BACKEND is also in the vault, so it would
    # survive a truncation too and cannot witness preservation on its own.
    assert "ANTHROPIC_API_KEY=LIVE_ANTHROPIC" in body
    assert "LODESTAR_CONTRIBUTE_KEY=VAULT_LODESTAR_VALUE" in body


def test_companion_non_secret_var_is_appended(tmp_path):
    """gap-054 step 3: the companion var the vault does NOT carry."""
    proc, _out, body = _run(
        tmp_path,
        ["--append-key", "LODESTAR_CONTRIBUTE_KEY", "--also", "LODESTAR_API_URL=https://x.example"],
        name="also")
    assert proc.returncode == 0, proc.stderr
    assert "LODESTAR_API_URL=https://x.example" in body
    for k in ACCRETED_ONLY:
        assert k in _keys(body)


def test_dry_run_writes_nothing(tmp_path):
    proc, _out, body = _run(tmp_path, ["--append-key", "LODESTAR_CONTRIBUTE_KEY", "--dry-run"],
                            name="dry")
    assert proc.returncode == 0, proc.stderr
    assert body == ACCRETED
