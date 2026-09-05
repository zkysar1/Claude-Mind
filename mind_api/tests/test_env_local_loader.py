"""Tests for the daemon's startup env wiring (s7 own-cloud cutover).

Two startup helpers in mind_api.src.__main__ populate os.environ before any
endpoint calls get_backend():

  * _load_env_local — reads .env.local for the N3 exact-allowlist storage keys
    + scoped MIND_AWS_* creds. The SECURITY property — it must NEVER load the root
    AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY into the daemon env — is the most
    important guard here (the OwnCloudBackend.from_env fail-closed design depends
    on those root keys not being present for the default boto3 chain to resolve).

  * _ensure_owncloud_roots — when own-cloud is selected, resolves the governed
    roots (WORLD_PATH/META_PATH) from local-paths.conf so from_env can build its
    root map even on spawn paths (watchdog mind-api-start.sh --restart) that do
    not export them. Gated on own-cloud so 100%-local stays zero-added-I/O.
"""
import os
from pathlib import Path

import pytest

from mind_api.src.__main__ import (
    _load_env_local,
    _ensure_owncloud_roots,
    _resolve_bind_port,
)


def _run_loader(tmp_path: Path, body: str, clear_keys=(), preset=None):
    """Write .env.local, snapshot os.environ, run the loader, return the
    post-load environ dict, then fully restore os.environ (no test pollution)."""
    (tmp_path / ".env.local").write_text(body, encoding="utf-8")
    saved = dict(os.environ)
    try:
        for k in clear_keys:
            os.environ.pop(k, None)
        for k, v in (preset or {}).items():
            os.environ[k] = v
        _load_env_local(tmp_path)
        return dict(os.environ)
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_loads_allowlisted_storage_keys_and_region(tmp_path):
    # N3: the loader accepts ONLY the exact _N3_ALLOWED_EXACT storage keys (plus
    # the MIND_AWS_* family + AWS_DEFAULT_REGION). These three are all allowlisted.
    body = (
        "STORAGE_BACKEND=own-cloud\n"
        "STORAGE_S3_BUCKET=zds-data\n"
        "AWS_DEFAULT_REGION=us-east-2\n"
    )
    env = _run_loader(tmp_path, body,
                      clear_keys=("STORAGE_BACKEND", "STORAGE_S3_BUCKET",
                                  "AWS_DEFAULT_REGION"))
    assert env["STORAGE_BACKEND"] == "own-cloud"
    assert env["STORAGE_S3_BUCKET"] == "zds-data"
    assert env["AWS_DEFAULT_REGION"] == "us-east-2"


def test_loads_mind_aws_credential_family(tmp_path):
    # N3: MIND_ now means EXCLUSIVELY the scoped-credential family — the MIND_AWS_*
    # prefix is the ONLY surviving MIND_ surface and must still load.
    body = (
        "MIND_AWS_ACCESS_KEY_ID=AKIA_SCOPED\n"
        "MIND_AWS_SECRET_ACCESS_KEY=scoped_secret\n"
        "MIND_AWS_ALLOW_DEFAULT_CHAIN=1\n"
    )
    keys = ("MIND_AWS_ACCESS_KEY_ID", "MIND_AWS_SECRET_ACCESS_KEY",
            "MIND_AWS_ALLOW_DEFAULT_CHAIN")
    env = _run_loader(tmp_path, body, clear_keys=keys)
    for k in keys:
        assert k in env, k
    assert env["MIND_AWS_ACCESS_KEY_ID"] == "AKIA_SCOPED"


def test_excludes_root_aws_keys(tmp_path):
    # SECURITY: the root AWS_* keys (reserved for lambda, NOT the daemon) must
    # never enter the daemon env, or the default  chain could resolve them.
    # They are neither in _N3_ALLOWED_EXACT nor under the MIND_AWS_ prefix.
    body = (
        "AWS_ACCESS_KEY_ID=AKIA_ROOT_SHOULD_NOT_LOAD\n"
        "AWS_SECRET_ACCESS_KEY=secret_root_should_not_load\n"
        "STORAGE_BACKEND=own-cloud\n"
    )
    env = _run_loader(tmp_path, body,
                      clear_keys=("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                                  "STORAGE_BACKEND"))
    assert env["STORAGE_BACKEND"] == "own-cloud"      # allowlisted control loads
    assert "AWS_ACCESS_KEY_ID" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env


def test_rejects_non_allowlisted_keys(tmp_path):
    # N3 TIGHTENING: the exact allowlist is NOT a prefix match. A bare old-broad
    # MIND_ key, a stray STORAGE_-prefixed key NOT in the exact set, and an
    # arbitrary host var must all be rejected — only the exact keys leak in.
    body = (
        "MIND_LEGACY_BROAD=should_not_load\n"     # old broad MIND_ prefix is dead
        "STORAGE_NOT_REAL=should_not_load\n"      # right prefix, not an exact key
        "ENVIRONMENT_NOT_REAL=should_not_load\n"  # right prefix, not an exact key
        "RANDOM_HOST_VAR=should_not_load\n"
        "STORAGE_BACKEND=own-cloud\n"             # the one real allowlisted key
    )
    clear = ("MIND_LEGACY_BROAD", "STORAGE_NOT_REAL", "ENVIRONMENT_NOT_REAL",
             "RANDOM_HOST_VAR", "STORAGE_BACKEND")
    env = _run_loader(tmp_path, body, clear_keys=clear)
    assert env["STORAGE_BACKEND"] == "own-cloud"
    for k in ("MIND_LEGACY_BROAD", "STORAGE_NOT_REAL", "ENVIRONMENT_NOT_REAL",
              "RANDOM_HOST_VAR"):
        assert k not in env, f"{k} leaked past the exact allowlist"


def test_setdefault_does_not_clobber_existing(tmp_path):
    # An explicit launch-env value must win over .env.local.
    body = "STORAGE_BACKEND=from_env_local\n"
    env = _run_loader(tmp_path, body, preset={"STORAGE_BACKEND": "from_launch_env"})
    assert env["STORAGE_BACKEND"] == "from_launch_env"


def test_missing_env_local_is_silent(tmp_path):
    # No .env.local present → no error, no keys added.
    saved = dict(os.environ)
    try:
        _load_env_local(tmp_path)  # tmp_path has no .env.local
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_comments_and_blanks_skipped(tmp_path):
    body = (
        "# a comment line\n"
        "\n"
        "   \n"
        "STORAGE_BACKEND=value1\n"
        "# OWNCLOUD_SYNC_DISABLE=should_not_load\n"
    )
    env = _run_loader(tmp_path, body,
                      clear_keys=("STORAGE_BACKEND", "OWNCLOUD_SYNC_DISABLE"))
    assert env["STORAGE_BACKEND"] == "value1"
    assert "OWNCLOUD_SYNC_DISABLE" not in env


def test_value_takes_first_token(tmp_path):
    # Mirrors provision_aws / backfill parsing: value is the first whitespace
    # token (trailing inline content ignored).
    body = "STORAGE_S3_BUCKET=firsttoken trailing junk\n"
    env = _run_loader(tmp_path, body, clear_keys=("STORAGE_S3_BUCKET",))
    assert env["STORAGE_S3_BUCKET"] == "firsttoken"


# --- _ensure_owncloud_roots (daemon resolves governed roots for own-cloud) ---

_ROOT_KEYS = ("STORAGE_BACKEND", "WORLD_PATH", "META_PATH",
              "MIND_WORLD", "MIND_META", "AGENTS_ROOT")


def _run_ensure(tmp_path: Path, backend=None, confs=None, preset=None, raw_confs=None):
    """Lay down agents/<name>/local-paths.conf under a fake project_root,
    snapshot os.environ, set the backend selector + presets, run
    _ensure_owncloud_roots, return the post-run environ, then fully restore
    os.environ. `confs` maps agent_name -> (world_path, meta_path); `raw_confs`
    maps agent_name -> verbatim conf text (for malformed / WORLD_PATH-less confs,
    e.g. the g-115-1449 `_`-prefixed shadow case)."""
    for name, (world, meta) in (confs or {}).items():
        d = tmp_path / "agents" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "local-paths.conf").write_text(
            f"WORLD_PATH={world}\nMETA_PATH={meta}\n", encoding="utf-8")
    for name, text in (raw_confs or {}).items():
        d = tmp_path / "agents" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "local-paths.conf").write_text(text, encoding="utf-8")
    saved = dict(os.environ)
    try:
        for k in _ROOT_KEYS:
            os.environ.pop(k, None)
        if backend is not None:
            os.environ["STORAGE_BACKEND"] = backend
        for k, v in (preset or {}).items():
            os.environ[k] = v
        _ensure_owncloud_roots(tmp_path)
        return dict(os.environ)
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_owncloud_sets_roots_from_first_agent_conf(tmp_path):
    w = tmp_path / "ext" / "world"
    m = tmp_path / "ext" / "meta"
    env = _run_ensure(tmp_path, backend="own-cloud", confs={"alpha": (w, m)})
    assert Path(env["WORLD_PATH"]) == w
    assert Path(env["META_PATH"]) == m
    assert Path(env["AGENTS_ROOT"]) == tmp_path / "agents"


def test_local_mode_does_not_touch_roots(tmp_path):
    # 100%-local must stay zero-added-I/O: no resolution, no env mutation.
    env = _run_ensure(tmp_path, backend="local",
                      confs={"alpha": (tmp_path / "w", tmp_path / "m")})
    assert "WORLD_PATH" not in env
    assert "META_PATH" not in env
    assert "AGENTS_ROOT" not in env


def test_unset_backend_does_not_touch_roots(tmp_path):
    env = _run_ensure(tmp_path, backend=None,
                      confs={"alpha": (tmp_path / "w", tmp_path / "m")})
    assert "WORLD_PATH" not in env


def test_setdefault_keeps_explicit_world_fills_missing_meta(tmp_path):
    # Partial env: WORLD_PATH explicitly set, META_PATH missing. Resolution runs
    # (have_meta falsy); setdefault keeps the explicit WORLD_PATH and fills META.
    m = tmp_path / "ext" / "meta"
    env = _run_ensure(tmp_path, backend="own-cloud",
                      confs={"alpha": (tmp_path / "ext" / "world", m)},
                      preset={"WORLD_PATH": "/explicit/world"})
    assert env["WORLD_PATH"] == "/explicit/world"   # not clobbered
    assert Path(env["META_PATH"]) == m              # filled from conf


def test_ayoai_world_meta_preset_short_circuits(tmp_path):
    # MIND_WORLD + MIND_META already set → from_env reads those first, so we
    # must NOT additionally set WORLD_PATH/META_PATH.
    env = _run_ensure(tmp_path, backend="own-cloud",
                      confs={"alpha": (tmp_path / "w", tmp_path / "m")},
                      preset={"MIND_WORLD": "/ay/world", "MIND_META": "/ay/meta"})
    assert "WORLD_PATH" not in env
    assert "META_PATH" not in env


def test_fail_loud_when_no_agent_conf(tmp_path):
    # own-cloud selected but no agents/*/local-paths.conf → no agent resolves
    # the governed roots. FAIL LOUD ( / rb-1796): the prior silent
    # fail-open served own-cloud with unset roots, 500ing every storage op — a
    # startup refusal is far easier to diagnose than a healthy-but-broken daemon.
    with pytest.raises(RuntimeError, match="no .*agent resolved WORLD/META"):
        _run_ensure(tmp_path, backend="own-cloud", confs={})


def test_owncloud_skips_underscore_test_agent_shadow(tmp_path):
    #  canonical case: a `_`-prefixed test/throwaway agent whose conf
    # lacks WORLD_PATH sorts BEFORE real agents. The OLD resolve(None) picked it
    # first, raised, and silently fail-opened (roots unset). The fix skips
    # `_`-prefixed dirs and resolves the real agent (alpha) instead.
    w = tmp_path / "ext" / "world"
    m = tmp_path / "ext" / "meta"
    env = _run_ensure(
        tmp_path, backend="own-cloud",
        confs={"alpha": (w, m)},
        raw_confs={"_gate_test_throwaway_agent_": "META_PATH=/tmp/junk-meta\n"},
    )
    assert Path(env["WORLD_PATH"]) == w   # resolved from alpha, not the shadow
    assert Path(env["META_PATH"]) == m


def test_owncloud_iterates_past_unresolvable_conf(tmp_path):
    # Defense in depth: a non-`_` agent that sorts first but whose conf lacks
    # WORLD_PATH must not break resolution — iterate past the raise to the next
    # conf-bearing agent that resolves.
    w = tmp_path / "ext" / "world"
    m = tmp_path / "ext" / "meta"
    env = _run_ensure(
        tmp_path, backend="own-cloud",
        confs={"zeta": (w, m)},                            # sorts AFTER 'aaa'
        raw_confs={"aaa": "META_PATH=/tmp/junk-meta\n"},   # no WORLD_PATH → raises
    )
    assert Path(env["WORLD_PATH"]) == w   # resolved from zeta after aaa raised
    assert Path(env["META_PATH"]) == m


# ---------------------------------------------------------------------------
#  — the inherited-override WARNING.
#
# `_load_env_local` uses setdefault ("explicit launch env wins"), so a var the
# daemon inherited from whoever spawned it BLOCKS the .env.local value for the
# daemon's whole life. On 2026-09-02 that was silent: seven --restart recycles
# from inside pytest handed the shared daemon STORAGE_BACKEND=local on an
# own-cloud box, and every daemon-mediated world write from that box stayed on
# the local mirror 00:34Z-06:45Z with nothing said anywhere.
#
# The setdefault CONTRACT IS DELIBERATELY UNCHANGED — a deliberate operator
# override must keep working. What changed is that the override now announces
# itself. Hence the assertion in the first test that the inherited value still
# wins: these tests pin a REPORT, never a refusal (a false positive here would
# fail every wrapper on the box under the daemon-only architecture; the same
# predicate family wired as a verdict produced 31h of false HIGH goals against
# a correctly-configured node, ).
# ---------------------------------------------------------------------------

def test_inherited_override_warns_with_both_values(tmp_path, capsys):
    """The incident, reduced: inherited local vs .env.local own-cloud."""
    env = _run_loader(tmp_path, "STORAGE_BACKEND=own-cloud\n",
                      preset={"STORAGE_BACKEND": "local"})
    err = capsys.readouterr().err
    assert "inherited env OVERRIDES .env.local for STORAGE_BACKEND" in err
    assert "'local'" in err and "'own-cloud'" in err, (
        "the warning must name BOTH values — which one is in force is the "
        "whole question a reader has at that moment"
    )
    assert "g-115-8604" in err
    # The setdefault contract is unchanged: the inherited value still wins.
    assert env["STORAGE_BACKEND"] == "local"


def test_agreeing_inherited_value_is_silent(tmp_path, capsys):
    """NEGATIVE CONTROL (guard-1220). A predicate must reject as well as
    accept. An inherited value EQUAL to .env.local's is not an override and
    must stay silent, or the warning becomes noise on every healthy daemon
    start and is tuned out exactly when it matters."""
    _run_loader(tmp_path, "STORAGE_BACKEND=own-cloud\n",
                preset={"STORAGE_BACKEND": "own-cloud"})
    assert "OVERRIDES" not in capsys.readouterr().err


def test_uninherited_key_is_silent(tmp_path, capsys):
    """The ordinary path — .env.local supplies a key nothing inherited."""
    _run_loader(tmp_path, "STORAGE_BACKEND=own-cloud\n",
                clear_keys=("STORAGE_BACKEND",))
    assert "OVERRIDES" not in capsys.readouterr().err


def test_empty_file_value_is_silent(tmp_path, capsys):
    """`MACHINE_ID=  # note` (an unedited .env.example copy) declares no value,
    so an inherited value is not overriding anything. Silent by construction —
    the existing required-var checks own that case."""
    _run_loader(tmp_path, "MACHINE_ID=  # UNIQUE per-machine id\n",
                preset={"MACHINE_ID": "cc-10"})
    assert "OVERRIDES" not in capsys.readouterr().err


def test_credential_mismatch_warns_without_leaking_values(tmp_path, capsys):
    """SECURITY. The loader also accepts the MIND_AWS_* credential family, so
    the warning path can see secrets. It names the key and withholds both
    values — the same key-NAME-only posture fleet-config-parity uses."""
    _run_loader(tmp_path, "MIND_AWS_SECRET_ACCESS_KEY=file-secret-value\n",
                preset={"MIND_AWS_SECRET_ACCESS_KEY": "inherited-secret-value"})
    err = capsys.readouterr().err
    assert "MIND_AWS_SECRET_ACCESS_KEY" in err and "credential key" in err
    assert "file-secret-value" not in err
    assert "inherited-secret-value" not in err


# ---------------------------------------------------------------------------
# MIND_API_PORT — the listen-port pin ()
#
# The daemon's port was OS-assigned and turned over on every recycle (measured
# on one box: 41247 -> 42055 -> 42387 inside ~25 min, the third from a SHA-move
# auto-restart). A client pinned to the old port fails OPEN, so the breakage is
# silent. MIND_API_PORT pins it, delivered through .env.local for the same
# reason MIND_API_TOKEN / MIND_API_BIND are: it is re-read at EVERY start,
# including an auto-respawn, whereas the two spawn wrappers are declared twins
# a third spawn path would silently bypass.
#
# The end-to-end test below is the one that matters, and it is deliberately
# MUTATION-SHAPED: it exercises .env.local -> _load_env_local -> the allowlist
# -> _resolve_bind_port, so deleting "MIND_API_PORT" from _N3_ALLOWED_EXACT
# makes it fail (the loader drops the key, the resolver sees unset, 4599 -> 0).
# A resolver-only test would stay green through exactly that regression --
# which is the guard-3485 defect class this key's own comment cites.


def test_mind_api_port_is_loadable_from_env_local(tmp_path):
    """READ <=> LOADABLE for MIND_API_PORT, through the real chain.

    Mutation proof: drop the key from _N3_ALLOWED_EXACT and this fails.
    """
    env = _run_loader(tmp_path, "MIND_API_PORT=4599\n",
                      clear_keys=("MIND_API_PORT",))
    assert env.get("MIND_API_PORT") == "4599", (
        "MIND_API_PORT did not survive _load_env_local — it is almost certainly "
        "missing from _N3_ALLOWED_EXACT, which makes the documented .env.local "
        "channel silently do nothing."
    )
    # ...and the loaded value is what the daemon would actually bind.
    assert _resolve_bind_port(None, env) == 4599


def test_resolve_bind_port_unset_is_os_assigned(tmp_path):
    """Outcome 3: an unset key is byte-identical to the pre-existing behaviour."""
    assert _resolve_bind_port(None, {}) == 0
    # An empty value is 'unset', matching how the loader treats a blank
    # .env.example-style line rather than binding something arbitrary.
    assert _resolve_bind_port(None, {"MIND_API_PORT": ""}) == 0
    assert _resolve_bind_port(None, {"MIND_API_PORT": "   "}) == 0


def test_resolve_bind_port_explicit_cli_wins_over_env():
    # default=None is what makes "not passed" distinguishable from "passed 0";
    # both used to collapse to 0, so an explicit --port 0 could not be honoured.
    assert _resolve_bind_port(8123, {"MIND_API_PORT": "4599"}) == 8123
    assert _resolve_bind_port(0, {"MIND_API_PORT": "4599"}) == 0


def test_resolve_bind_port_refuses_bad_values_instead_of_falling_back():
    """A bad pin must NOT degrade to 0 — that is the failure it exists to remove."""
    for bad in ("not-a-port", "80.5", "-1", "65536", "99999"):
        with pytest.raises(ValueError):
            _resolve_bind_port(None, {"MIND_API_PORT": bad})


def test_resolve_bind_port_accepts_the_range_boundaries():
    assert _resolve_bind_port(None, {"MIND_API_PORT": "0"}) == 0
    assert _resolve_bind_port(None, {"MIND_API_PORT": "65535"}) == 65535
