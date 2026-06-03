"""Tests for the daemon's startup env wiring (s7 own-cloud cutover).

Two startup helpers in mind_api.src.__main__ populate os.environ before any
endpoint calls get_backend():

  * _load_env_local — reads .env.local for the MIND_* storage-backend selector
    + scoped creds. The SECURITY property — it must NEVER load the root
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


def test_loads_mind_keys_and_region(tmp_path):
    body = (
        "MIND_STORAGE_BACKEND=own-cloud\n"
        "MIND_TEST_BUCKET=lodestar-data\n"
        "AWS_DEFAULT_REGION=us-east-2\n"
    )
    env = _run_loader(tmp_path, body,
                      clear_keys=("MIND_STORAGE_BACKEND", "MIND_TEST_BUCKET",
                                  "AWS_DEFAULT_REGION"))
    assert env["MIND_STORAGE_BACKEND"] == "own-cloud"
    assert env["MIND_TEST_BUCKET"] == "lodestar-data"
    assert env["AWS_DEFAULT_REGION"] == "us-east-2"


def test_excludes_root_aws_keys(tmp_path):
    # SECURITY: the root AWS_* keys (reserved for lambda, NOT the daemon) must
    # never enter the daemon env, or the default  chain could resolve them.
    body = (
        "AWS_ACCESS_KEY_ID=AKIA_ROOT_SHOULD_NOT_LOAD\n"
        "AWS_SECRET_ACCESS_KEY=secret_root_should_not_load\n"
        "MIND_TEST_SCOPED=ok\n"
    )
    env = _run_loader(tmp_path, body,
                      clear_keys=("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                                  "MIND_TEST_SCOPED"))
    assert env["MIND_TEST_SCOPED"] == "ok"
    assert "AWS_ACCESS_KEY_ID" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env


def test_setdefault_does_not_clobber_existing(tmp_path):
    # An explicit launch-env value must win over .env.local.
    body = "MIND_TEST_KEEP=from_env_local\n"
    env = _run_loader(tmp_path, body, preset={"MIND_TEST_KEEP": "from_launch_env"})
    assert env["MIND_TEST_KEEP"] == "from_launch_env"


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
        "MIND_TEST_AFTER=value1\n"
        "# MIND_TEST_COMMENTED=should_not_load\n"
    )
    env = _run_loader(tmp_path, body,
                      clear_keys=("MIND_TEST_AFTER", "MIND_TEST_COMMENTED"))
    assert env["MIND_TEST_AFTER"] == "value1"
    assert "MIND_TEST_COMMENTED" not in env


def test_value_takes_first_token(tmp_path):
    # Mirrors provision_aws / backfill parsing: value is the first whitespace
    # token (trailing inline content ignored).
    body = "MIND_TEST_TOKEN=firsttoken trailing junk\n"
    env = _run_loader(tmp_path, body, clear_keys=("MIND_TEST_TOKEN",))
    assert env["MIND_TEST_TOKEN"] == "firsttoken"


# --- _ensure_owncloud_roots (daemon resolves governed roots for own-cloud) ---

_ROOT_KEYS = ("MIND_STORAGE_BACKEND", "WORLD_PATH", "META_PATH",
              "MIND_WORLD", "MIND_META", "MIND_AGENTS_ROOT")


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
            os.environ["MIND_STORAGE_BACKEND"] = backend
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
    assert Path(env["MIND_AGENTS_ROOT"]) == tmp_path / "agents"


def test_local_mode_does_not_touch_roots(tmp_path):
    # 100%-local must stay zero-added-I/O: no resolution, no env mutation.
    env = _run_ensure(tmp_path, backend="local",
                      confs={"alpha": (tmp_path / "w", tmp_path / "m")})
    assert "WORLD_PATH" not in env
    assert "META_PATH" not in env
    assert "MIND_AGENTS_ROOT" not in env


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
