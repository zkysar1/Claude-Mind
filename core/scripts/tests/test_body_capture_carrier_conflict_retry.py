""" — a LOST RACE on the body-capture carrier's key is retried in
place, on a FRESH fence, instead of abandoned until the next append.

WHY THE CARRIER KEY HAS A RACE AT ALL. `body_capture_carrier.push()` PUTs the
whole carrier from the daemon's request thread. The daemon's own sync sweep
(`owncloud_sync.sweep`, run in-process from `mind_api/src/__main__.py`) mirrors
`world/`, and `body-carriers` is not in `owncloud_sync._EXCLUDE_DIRS` — so the
key has TWO writers by construction, not the one its docstring assumed. The
daemon log on cc-09 (`uname -r` 6.8.0-139-generic, own-cloud, 2026-09-14) holds
five carrier push failures and both race shapes: 3x `ConflictError` (the 412 the
backend maps from a stale If-Match) and 2x raw `ClientError`
`ConditionalRequestConflict` (S3's 409 for two conditional writes in flight at
once, which `OwnCloudBackend._put` does NOT map to `ConflictError`).

WHY A PLAIN RETRY WOULD NOT DO. The losing PUT was fenced on the ETag this
process last saw. Re-PUTting on that same fence 412s deterministically (the
rb-2639 stale-IfMatch class, guard-908). So every retry re-HEADs for the current
version and PUTs fenced on THAT — `stat()` + `mirror_put(expected_version=)`,
the sync sweep's own local->store mirror primitive — and re-reads the local
carrier, so an append that landed during the backoff rides along.

WHAT IS NOT RETRIED. Anything that is not a lost race: a transport error, a
permission gap, a `NoClaimError` (structural — no retry can ever succeed). Those
still fail on the first attempt and are reported exactly as before.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import body_capture_carrier as bcc  # noqa: E402
import storage_backend  # noqa: E402


class _LostRace(Exception):
    """Stands in for OwnCloudBackend.ConflictError — the backend's own 412 type,
    exposed to callers as `backend.conflict_error`."""


class RacingBackend:
    """A store whose writes can lose a race a configurable number of times.

    `write_bytes` is the fenced PUT on whatever fence the process already holds
    (attempt 0). `stat` + `mirror_put` is the re-fenced PUT a retry must use; the
    version `stat` returns is deliberately distinct from anything `write_bytes`
    could have been fenced on, so a test can tell the two apart.
    """

    conflict_error = _LostRace

    def __init__(self, *, write_raises=(), mirror_raises=(),
                 fresh_version="etag-after-the-other-writer"):
        self.write_raises = list(write_raises)
        self.mirror_raises = list(mirror_raises)
        self.fresh_version = fresh_version
        self.store: dict[str, bytes] = {}
        self.write_calls = 0
        self.stat_calls = 0
        self.mirror_versions: list = []

    def write_bytes(self, path, data):
        self.write_calls += 1
        if self.write_raises:
            raise self.write_raises.pop(0)
        self.store[Path(path).name] = data

    def stat(self, path):
        self.stat_calls += 1
        return SimpleNamespace(version=self.fresh_version)

    def mirror_put(self, path, data, *, expected_version=None):
        self.mirror_versions.append(expected_version)
        if self.mirror_raises:
            raise self.mirror_raises.pop(0)
        self.store[Path(path).name] = data


def _carrier(tmp_path: Path) -> Path:
    p = tmp_path / "world" / "body-carriers" / "agent-x" / "sid-r-fastlane.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"unit_key": "sid-r", "slot": "spark_capture",
                             "entry": {"fact": "one", "load_bearing": True}})
                 + "\n", encoding="utf-8")
    return p


@pytest.fixture
def backoffs(monkeypatch):
    """Record every backoff instead of sleeping (guard-4582: a retry must not put
    real wall-clock time into the suite that reaches it)."""
    seen: list[int] = []

    def _record(attempt):
        seen.append(attempt)
        return 0.0

    monkeypatch.setattr(bcc, "_conflict_backoff", _record, raising=False)
    monkeypatch.setattr(bcc, "_PUSH_FAILURE_REPORTED", False, raising=False)
    return seen


def _use(monkeypatch, be):
    monkeypatch.setattr(storage_backend, "get_backend", lambda: be)


def test_a_lost_race_is_repushed_on_a_fresh_fence(tmp_path, monkeypatch, backoffs):
    p = _carrier(tmp_path)
    be = RacingBackend(write_raises=[_LostRace("If-Match failed")])
    _use(monkeypatch, be)

    assert bcc.push(p) is True, (
        "one lost race abandoned the push; the message itself says re-run it")
    assert be.stat_calls == 1, "the retry must re-HEAD, not reuse the stale fence"
    assert be.mirror_versions == ["etag-after-the-other-writer"], (
        "the re-PUT must be fenced on the version the retry just read")
    assert be.store["sid-r-fastlane.jsonl"] == p.read_bytes()
    assert backoffs == [0], "exactly one jittered backoff before the one retry"


def test_s3_conditional_request_conflict_is_a_lost_race_too(tmp_path, monkeypatch, backoffs):
    """S3 answers two conditional PUTs in flight at once with a 409 the backend
    leaves unmapped, so it arrives as a raw ClientError. Measured twice on this
    key; missing it would leave half the observed failures unretried."""
    botocore_exceptions = pytest.importorskip("botocore.exceptions")
    p = _carrier(tmp_path)
    race = botocore_exceptions.ClientError(
        {"Error": {"Code": "ConditionalRequestConflict",
                   "Message": "The conditional request cannot succeed due to a "
                              "conflicting operation against this resource."}},
        "PutObject")
    be = RacingBackend(write_raises=[race])
    _use(monkeypatch, be)

    assert bcc.push(p) is True
    assert be.mirror_versions == ["etag-after-the-other-writer"]


def test_a_failure_that_is_not_a_race_is_not_retried(tmp_path, monkeypatch, backoffs):
    """A retry is only honest for a race. A transport or permission error, and
    the structural NoClaimError, must fail on the first attempt as before."""
    p = _carrier(tmp_path)
    be = RacingBackend(write_raises=[RuntimeError("simulated transport error")])
    _use(monkeypatch, be)

    assert bcc.push(p) is False
    assert be.write_calls == 1 and be.stat_calls == 0 and be.mirror_versions == []
    assert backoffs == [], "a non-race failure must not sleep"


def test_the_retry_is_bounded_and_a_surviving_conflict_is_named_a_wedge(
        tmp_path, monkeypatch, backoffs, capsys):
    """A conflict that survives every re-fenced attempt is not a race any more
    (guard-908's persistence discriminator, built in). The push gives up, still
    never raises, and the report says how many attempts it survived."""
    p = _carrier(tmp_path)
    attempts = bcc._CONFLICT_ATTEMPTS
    be = RacingBackend(
        write_raises=[_LostRace("If-Match failed")],
        mirror_raises=[_LostRace("If-Match failed")] * attempts)
    _use(monkeypatch, be)

    assert bcc.push(p) is False
    assert be.write_calls + len(be.mirror_versions) == attempts, "bounded"
    assert backoffs == list(range(attempts - 1))
    err = capsys.readouterr().err
    assert "[body-capture-carrier] push FAILED (" in err, (
        "keep the prefix the wm-append.sh notice tells readers to grep for")
    assert f"after {attempts} attempt" in err, (
        "a report that cannot tell one lost race from a wedge hides the wedge")


def test_each_retry_re_reads_the_local_carrier(tmp_path, monkeypatch):
    """Whole-file semantics survive the retry: an append that lands while the
    push backs off must be in the bytes the retry sends."""
    p = _carrier(tmp_path)
    late = json.dumps({"unit_key": "sid-r", "slot": "exp_capture",
                       "entry": {"fact": "appended during the backoff",
                                 "load_bearing": True}}) + "\n"

    def _append_while_backing_off(attempt):
        with p.open("a", encoding="utf-8") as fh:
            fh.write(late)
        return 0.0

    monkeypatch.setattr(bcc, "_conflict_backoff", _append_while_backing_off,
                        raising=False)
    be = RacingBackend(write_raises=[_LostRace("If-Match failed")])
    _use(monkeypatch, be)

    assert bcc.push(p) is True
    assert be.store["sid-r-fastlane.jsonl"].decode("utf-8").endswith(late)


# --------------------------------------------------------------------------
# The same race against the REAL OwnCloudBackend fence (moto), so the fix is
# pinned on `_put`'s If-Match code path rather than on a fake's idea of it.
# --------------------------------------------------------------------------

BUCKET = "test-bucket"
LOCKS = "test-locks"
SESSIONS = "test-sessions"
REGION = "us-east-2"
ENV_ID = "test-env"


@pytest.fixture
def cloud(monkeypatch, tmp_path):
    pytest.importorskip("moto")
    boto3 = pytest.importorskip("boto3")
    from moto import mock_aws
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
              "AWS_SECURITY_TOKEN", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(k, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        ddb = boto3.client("dynamodb", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET,
                         CreateBucketConfiguration={"LocationConstraint": REGION})
        for table, key in ((LOCKS, "lock_key"), (SESSIONS, "session_key")):
            ddb.create_table(
                TableName=table,
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST")
        yield {"s3": s3, "ddb": ddb, "root": tmp_path}


def test_real_backend_stale_fence_is_repushed_on_a_fresh_head(cloud, monkeypatch, backoffs):
    from owncloud_backend import OwnCloudBackend
    be = OwnCloudBackend(env_id=ENV_ID, bucket=BUCKET, lock_table=LOCKS,
                         sessions_table=SESSIONS, cache_root=cloud["root"],
                         machine_id="m1", region=REGION,
                         s3=cloud["s3"], ddb=cloud["ddb"])
    _use(monkeypatch, be)
    p = _carrier(cloud["root"])
    first = p.read_bytes()

    assert bcc.push(p) is True                  # creates the key; fence = its ETag

    # The OTHER writer: the sync sweep mirrors a newer snapshot of the same file,
    # moving the remote ETag underneath this process's fence.
    second = json.dumps({"unit_key": "sid-r", "slot": "spark_capture",
                         "entry": {"fact": "two", "load_bearing": True}}) + "\n"
    cloud["s3"].put_object(Bucket=BUCKET, Key=be._s3_key(p),
                           Body=first + second.encode("utf-8"))
    third = json.dumps({"unit_key": "sid-r", "slot": "spark_capture",
                        "entry": {"fact": "three", "load_bearing": True}}) + "\n"
    with p.open("a", encoding="utf-8") as fh:
        fh.write(second + third)

    assert bcc.push(p) is True, (
        "the stale fence 412s the plain PUT; the retry must land on a fresh HEAD")
    assert backoffs == [0], "the plain PUT really did lose (one retry ran)"
    assert be.read_authoritative_bytes(p) == p.read_bytes(), (
        "the store must hold the WHOLE local carrier, including the late append")
