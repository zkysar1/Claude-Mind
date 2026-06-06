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

from mind_api.src.__main__ import _load_env_local, _ensure_owncloud_roots


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


def _run_ensure(tmp_path: Path, backend=None, confs=None, preset=None):
    """Lay down agents/<name>/local-paths.conf under a fake project_root,
    snapshot os.environ, set the backend selector + presets, run
    _ensure_owncloud_roots, return the post-run environ, then fully restore
    os.environ. `confs` maps agent_name -> (world_path, meta_path)."""
    for name, (world, meta) in (confs or {}).items():
        d = tmp_path / "agents" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "local-paths.conf").write_text(
            f"WORLD_PATH={world}\nMETA_PATH={meta}\n", encoding="utf-8")
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


def test_fail_open_when_no_agent_conf(tmp_path):
    # own-cloud selected but no agents/*/local-paths.conf → resolver raises,
    # _ensure_owncloud_roots swallows it (fail-open): roots stay unset and the
    # daemon does not crash at startup.
    env = _run_ensure(tmp_path, backend="own-cloud", confs={})
    assert "WORLD_PATH" not in env
    assert "META_PATH" not in env
