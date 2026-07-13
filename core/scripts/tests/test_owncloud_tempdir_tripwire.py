"""5 regression: OwnCloudBackend._put tempdir tripwire.

Unit-tests _assert_not_tempdir_put -- the UNIVERSAL net that refuses an
own-cloud S3 PUT whose path resolves under a tempfile/pytest temp dir. This is
the guard that would have PREVENTED the 2026-07-09 incident (rb-2983/guard-955):
a subprocess seeding a tmp world but inheriting STORAGE_BACKEND=own-cloud
collided on the PRODUCTION S3 key (=_customer_prefix+env_id+_rel(path), which
IGNORES the tmp local path) and truncated world/aspirations.jsonl 22 asp -> 1
fixture record. The conftest STORAGE_BACKEND=local pin only covers pytest-
COLLECTED tests; this backend-level tripwire additionally covers main()-style
files run directly (`python3 test_x.py`) and the bash aggregator that ran the
truncating test.

The guard uses no instance state, so we exercise it on a bare instance
(__new__, bypassing the boto3-constructing __init__) -- a fast, hermetic unit
test with no moto / no boto3 client.

File basename starts with `test_` so domain-leak-check.sh skips the incident
identifiers here (they are test rationale, not a domain leak).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from owncloud_backend import OwnCloudBackend  # noqa: E402


def _guard():
    # _assert_not_tempdir_put reads only its `path` arg + os.environ + tempfile
    # (no self.* attributes), so __new__ (bypassing __init__, which constructs
    #  clients) yields a usable instance for the guard in isolation.
    return OwnCloudBackend.__new__(OwnCloudBackend)


def test_tripwire_fires_on_tmp_path(tmp_path, monkeypatch):
    """A PUT under a pytest tmp dir (the incident path) is REFUSED loud."""
    monkeypatch.delenv("MIND_ALLOW_TMP_OWNCLOUD_PUT", raising=False)
    target = tmp_path / "world" / "aspirations.jsonl"  # under gettempdir()
    with pytest.raises(RuntimeError, match="test-isolation tripwire"):
        _guard()._assert_not_tempdir_put(target)


def test_tripwire_bypassed_by_override(tmp_path, monkeypatch):
    """The one legitimate own-cloud test opts out via the env escape hatch."""
    monkeypatch.setenv("MIND_ALLOW_TMP_OWNCLOUD_PUT", "1")
    _guard()._assert_not_tempdir_put(tmp_path / "world" / "aspirations.jsonl")


def test_tripwire_allows_non_tmp_production_path(monkeypatch):
    """A production-shaped world path (NOT under a temp dir) never trips."""
    monkeypatch.delenv("MIND_ALLOW_TMP_OWNCLOUD_PUT", raising=False)
    _guard()._assert_not_tempdir_put(
        Path("/opt/ayoai-mind/.mind-data/world/aspirations.jsonl"))


def test_tripwire_fires_on_relocated_pytest_dir(monkeypatch):
    """Backstop: a pytest tmp dir relocated OUTSIDE gettempdir (TMPDIR /
    --basetemp) is still caught via the 'pytest-' path-segment marker."""
    monkeypatch.delenv("MIND_ALLOW_TMP_OWNCLOUD_PUT", raising=False)
    with pytest.raises(RuntimeError, match="test-isolation tripwire"):
        _guard()._assert_not_tempdir_put(
            Path("/data/ci/pytest-of-runner/pytest-3/test_x0/world/aspirations.jsonl"))
