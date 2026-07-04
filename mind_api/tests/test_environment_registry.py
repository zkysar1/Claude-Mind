"""Tests for the local environment registry (, BRD environment-id-as-key).

The daemon derives its storage-backend wiring (STORAGE_BACKEND / STORAGE_S3_BUCKET
/ STORAGE_DDB_* / AWS_DEFAULT_REGION) from core/config/environments/<ENVIRONMENT_ID>.yaml
via mind_api.src.__main__._apply_environment_registry, so the operator sets ONE
value (ENVIRONMENT_ID) instead of four independently-misconfigurable env vars.

These tests exercise _apply_environment_registry directly (in-process, controlled
env). Because that function mutates os.environ via setdefault (NOT via monkeypatch),
the autouse _isolate_env fixture snapshots + restores the registry-relevant keys
around every test so no derived value leaks into the session env (conftest pins
STORAGE_BACKEND=local for the whole session — that pin is restored after each test).
"""

import os
from pathlib import Path

import pytest
import yaml

from mind_api.src.__main__ import (
    _apply_environment_registry,
    _valid_environment_ids,
)

# tests -> mind_api -> PROJECT_ROOT (same shape as __main__._project_root()).
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_DIR = PROJECT_ROOT / "core" / "config" / "environments"

_KEYS = [
    "ENVIRONMENT_ID",
    "STORAGE_BACKEND",
    "STORAGE_S3_BUCKET",
    "STORAGE_DDB_SESSIONS_TABLE",
    "STORAGE_DDB_LOCK_TABLE",
    "AWS_DEFAULT_REGION",
]


@pytest.fixture(autouse=True)
def _isolate_env():
    """Snapshot + restore the registry-relevant env vars around each test.

    _apply_environment_registry writes os.environ directly, so monkeypatch cannot
    undo it — this fixture is the isolation boundary."""
    saved = {k: os.environ.get(k) for k in _KEYS}
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _clear_derived():
    """Drop every derived storage var so setdefault starts from a clean slate."""
    for k in _KEYS[1:]:  # all except ENVIRONMENT_ID
        os.environ.pop(k, None)


def test_registry_derives_storage_from_env_id_when_unset():
    """Outcome 3: ENVIRONMENT_ID=ayoai-mind + no STORAGE_* -> own-cloud wiring
    is derived entirely from the registry."""
    _clear_derived()
    os.environ["ENVIRONMENT_ID"] = "ayoai-mind"
    _apply_environment_registry(PROJECT_ROOT)
    assert os.environ["STORAGE_BACKEND"] == "own-cloud"
    assert os.environ["STORAGE_S3_BUCKET"] == "zds-own-cloud-data"
    assert os.environ["STORAGE_DDB_SESSIONS_TABLE"] == "zds-sessions"
    assert os.environ["STORAGE_DDB_LOCK_TABLE"] == "zds-locks"
    assert os.environ["AWS_DEFAULT_REGION"] == "us-east-2"


def test_unknown_environment_id_refuses_startup():
    """Outcome 4: an ENVIRONMENT_ID with no registry file fails loud and names
    the valid ids (no silent state-mixing)."""
    _clear_derived()
    os.environ["ENVIRONMENT_ID"] = "does-not-exist-zzz"
    with pytest.raises(RuntimeError) as exc:
        _apply_environment_registry(PROJECT_ROOT)
    msg = str(exc.value)
    assert "does-not-exist-zzz" in msg
    for vid in ("ayoai-mind", "claude-mind", "local", "zds-mind"):
        assert vid in msg, f"valid id {vid} not listed in refusal message"


def test_deprecation_warning_and_legacy_precedence(capsys):
    """Outcome 5: a legacy STORAGE_* set alongside the registry emits a
    DEPRECATION warning; setdefault keeps the explicit legacy value winning so
    promotion never changes a downstream env's running behavior."""
    _clear_derived()
    os.environ["ENVIRONMENT_ID"] = "ayoai-mind"
    os.environ["STORAGE_S3_BUCKET"] = "operator-override-bucket"
    _apply_environment_registry(PROJECT_ROOT)
    # setdefault: the explicit legacy value wins over the registry value.
    assert os.environ["STORAGE_S3_BUCKET"] == "operator-override-bucket"
    # keys NOT explicitly set are still derived from the registry.
    assert os.environ["STORAGE_BACKEND"] == "own-cloud"
    err = capsys.readouterr().err
    assert "DEPRECATION" in err
    assert "STORAGE_S3_BUCKET" in err


def test_no_environment_id_is_noop():
    """Backward-compat: ENVIRONMENT_ID unset -> no-op. Legacy N-var mode (and the
    hermetic test suite, which sets no ENVIRONMENT_ID) is fully preserved."""
    _clear_derived()
    os.environ.pop("ENVIRONMENT_ID", None)
    _apply_environment_registry(PROJECT_ROOT)  # must not raise
    assert os.environ.get("STORAGE_BACKEND") is None
    assert os.environ.get("STORAGE_S3_BUCKET") is None


def test_all_registry_files_present_and_parse():
    """Outcome 1: registry files committed for the 4 named environments, each a
    well-formed mapping whose environment_id matches its filename stem."""
    ids = _valid_environment_ids(PROJECT_ROOT)
    for expected in ("ayoai-mind", "claude-mind", "local", "zds-mind"):
        assert expected in ids, f"{expected}.yaml registry file missing"
    for stem in ids:
        data = yaml.safe_load((ENV_DIR / f"{stem}.yaml").read_text(encoding="utf-8"))
        assert isinstance(data, dict), f"{stem}.yaml is not a mapping"
        assert data.get("environment_id") == stem
        assert "backend" in data, f"{stem}.yaml missing backend"


def test_local_env_id_derives_local_backend():
    """ENVIRONMENT_ID=local -> STORAGE_BACKEND=local, and no cloud keys are
    invented (a local-files env needs no bucket / DDB tables / region)."""
    _clear_derived()
    os.environ["ENVIRONMENT_ID"] = "local"
    _apply_environment_registry(PROJECT_ROOT)
    assert os.environ["STORAGE_BACKEND"] == "local"
    assert os.environ.get("STORAGE_S3_BUCKET") is None
    assert os.environ.get("STORAGE_DDB_SESSIONS_TABLE") is None
