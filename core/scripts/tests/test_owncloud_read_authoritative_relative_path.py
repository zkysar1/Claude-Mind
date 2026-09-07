"""read_authoritative_bytes must REFUSE a relative path, not serve local bytes.

REGRESSION PIN for g-115-4256.

`_rel` raises ValueError for a path "not under any configured root". That is
true of BOTH a genuinely out-of-root (git-shipped) path AND a merely RELATIVE
one -- indistinguishable there, but needing opposite handling:

  * out-of-root  -> never on S3 -> local read is correct and DELIBERATE
  * relative     -> caller bug  -> silently returned LOCAL mirror bytes from the
                                   one API whose docstring promises it "NEVER
                                   touches the local mirror"

That is a false all-clear emitted by the API built to prevent false all-clears
(guard-980 class, inside the guard-980 remedy). Measured on cc-02 2026-07-31: a
fleet probe built on `Path('agents')/name/...` reported local==authoritative for
all 5 agents; the same probe with `.resolve()` showed 4 of 5 DIVERGED, confirmed
independently by s3.head_object.

These tests exercise the REAL OwnCloudBackend method. They deliberately do NOT
use a FakeBackend: guard-919 -- a passing fake/monkeypatched test at the
own-cloud/S3 boundary is not evidence about the real backend, which is the
production one. No S3 call is made or needed: both branches under test return or
raise strictly before `self.s3.get_object`.
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / "core" / "scripts"))

from owncloud_backend import OwnCloudBackend  # noqa: E402


def _backend_without_s3(tmp_path):
    """A real OwnCloudBackend with just enough state for the pre-S3 branches.

    __new__ avoids the credentialed __init__; only `_roots` is consulted before
    the branch under test. Using the real class (not a stand-in) is the point.
    """
    be = OwnCloudBackend.__new__(OwnCloudBackend)
    be._roots = [(tmp_path / "world", "world")]
    # _s3_key interpolates _customer_prefix() and env_id BEFORE calling _rel
    # (left-to-right f-string evaluation), so both must exist for the ValueError
    # under test to come from _rel rather than an AttributeError. Neither
    # participates in the branch being pinned.
    be.env_id = "test-env"
    be._customer_prefix = lambda: ""
    return be


def test_relative_path_raises_instead_of_returning_local_bytes(tmp_path):
    """The pin: a relative path must fail loud, never yield mirror bytes."""
    be = _backend_without_s3(tmp_path)
    rel = Path("agents") / "zeta" / "session" / "execution-diary.jsonl"

    with pytest.raises(ValueError) as exc:
        be.read_authoritative_bytes(rel)

    msg = str(exc.value)
    assert "ABSOLUTE" in msg, f"the refusal must say what is wrong, got: {msg}"
    assert "LOCAL" in msg, f"the refusal must say what it refused to do, got: {msg}"


def test_out_of_root_absolute_path_still_reads_locally(tmp_path):
    """The deliberate fallback must SURVIVE -- the goal says do not delete it.

    Without this, a fix that simply removed the except-branch would pass the pin
    above while breaking every git-shipped out-of-root read.
    """
    be = _backend_without_s3(tmp_path)
    outside = tmp_path / "git-shipped" / "note.txt"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"shipped-in-git")

    # Absolute and under no configured root: _rel raises, and the local read is
    # the correct, documented behaviour.
    assert be.read_authoritative_bytes(outside) == b"shipped-in-git"


def test_the_two_cases_are_actually_distinguishable(tmp_path):
    """Positive control for the discriminator the fix relies on.

    If Path.is_absolute() ever stopped separating these, both tests above could
    pass for the wrong reason.
    """
    assert not (Path("agents") / "zeta").is_absolute()
    assert (tmp_path / "git-shipped" / "note.txt").is_absolute()
