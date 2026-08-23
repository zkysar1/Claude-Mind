"""moto-mocked tests for the  ETag-verified range-tail delta pull.

THE SEAM: OwnCloudBackend._range_tail_pull, called from _refresh just before
the full GET, on the "download" branch of the no-clobber decision. When an
APPEND-MOSTLY plaintext object has grown and the local mirror is exactly the
last-pulled prefix, fetch only `Range: bytes=<local_size>-` and accept it ONLY
when md5(local_prefix + tail) == the object's ETag. For a single-part plaintext
object the ETag IS the content md5, so that equality is an exact proof of
byte-identity with what a full GET would have returned.

WHAT THESE PINS ARE FOR. The fix's visible effect is an ABSENCE (a full GET
that no longer happens), and per guard-4166 an absence-shaped assertion is also
what a completely DEAD component produces. So every test here asserts on the
OBSERVED S3 CALL SHAPE via a get_object spy — range-vs-full is a positive
signal, not an absence — and the file carries a forced-failure CONTROL
(test 7) proving the fallback repairs a bad tail rather than trusting it.

Coverage (the goal's enumerated cases):
  1. append           -> tail path taken; exactly one RANGE get; bytes fetched
                         == the delta; mirror byte-identical to remote
  2. in-place edit    -> md5 proof fails -> full GET; mirror correct
  3. gz object        -> skipped by construction (tail of a gzip stream is not
                         a suffix of the plaintext)
  4. multipart etag   -> skipped (a '<hex>-N' ETag is not a content md5)
  5. modified mirror  -> skipped; no_clobber preserved, local NOT overwritten
  6. remote shrank    -> skipped (no tail to fetch)
  7. CONTROL          -> range GET stubbed to return WRONG bytes; the md5 proof
                         must reject them and the full GET must repair
  8. non-allowlisted  -> skipped (proves the allowlist actually filters)

Harness mirrors test_owncloud_codec_backend.py (moto mock_aws + RUNTIME_DIR
isolation). File basename starts with ``test_`` so domain-leak-check.sh skips it.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

moto = pytest.importorskip("moto")
import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_DIR = PROJECT_ROOT / "core" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import _owncloud_codec as codec  # noqa: E402

ENV_ID = "test-env"
BUCKET = "test-bucket"
LOCKS = "test-locks"
SESSIONS = "test-sessions"
REGION = "us-west-2"

# An append-mostly board channel: the store this whole lever exists for.
BOARD_REL = ("world", "board", "general.jsonl")
BASE = b'{"id": "msg-0001", "text": "seed"}\n' * 40
TAIL = b'{"id": "msg-0002", "text": "appended"}\n' * 5


@pytest.fixture(autouse=True)
def _default_machine_id(monkeypatch):
    monkeypatch.setenv("MACHINE_ID", "test-machine-ci")


@pytest.fixture(autouse=True)
def _isolate_sync_manifest(monkeypatch, tmp_path):
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "_owncloud_rt"))


@pytest.fixture
def aws_env(monkeypatch):
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
              "AWS_SECURITY_TOKEN", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(k, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


@pytest.fixture
def cloud(aws_env, tmp_path):
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        ddb = boto3.client("dynamodb", region_name=REGION)
        s3.create_bucket(
            Bucket=BUCKET,
            CreateBucketConfiguration={"LocationConstraint": REGION})
        for name, keyname in ((LOCKS, "lock_key"), (SESSIONS, "session_key")):
            ddb.create_table(
                TableName=name,
                KeySchema=[{"AttributeName": keyname, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": keyname,
                                       "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST")
        yield {"s3": s3, "ddb": ddb, "root": tmp_path}


class _GetSpy:
    """Records the shape of every get_object call: range-vs-full is the
    POSITIVE signal these tests assert on (guard-4166 — never assert only on
    the absence of a full GET, which a dead component also produces)."""

    def __init__(self, real):
        self._real = real
        self.calls = []          # list of Range strings; None == full GET

    def __call__(self, **kw):
        self.calls.append(kw.get("Range"))
        return self._real(**kw)

    @property
    def ranges(self):
        return [r for r in self.calls if r is not None]

    @property
    def fulls(self):
        return [r for r in self.calls if r is None]


def _backend(cloud, machine_id="m1", **kw):
    from owncloud_backend import OwnCloudBackend
    return OwnCloudBackend(
        env_id=ENV_ID, bucket=BUCKET, lock_table=LOCKS,
        sessions_table=SESSIONS, cache_root=cloud["root"],
        machine_id=machine_id, region=REGION,
        s3=cloud["s3"], ddb=cloud["ddb"], **kw)


def _prime(cloud, rel_parts=BOARD_REL, body=BASE):
    """Seed S3 and do ONE full read so the mirror exists AND the persistent
    manifest baseline is stamped — the state a real box is in between pulls.
    Returns (backend, local_path, key)."""
    b = _backend(cloud)
    p = cloud["root"].joinpath(*rel_parts)
    key = b._s3_key(p)
    cloud["s3"].put_object(Bucket=BUCKET, Key=key, Body=body)
    assert b.read_bytes(p, force_fresh=True) == body
    return b, p, key


def _append_remote(cloud, key, base, tail):
    cloud["s3"].put_object(Bucket=BUCKET, Key=key, Body=base + tail)


# --- 1. the append case: the whole point -------------------------------------
def test_append_takes_range_tail_and_mirror_matches_remote(cloud, monkeypatch):
    b, p, key = _prime(cloud)
    _append_remote(cloud, key, BASE, TAIL)

    spy = _GetSpy(b.s3.get_object)
    monkeypatch.setattr(b.s3, "get_object", spy)
    got = b.read_bytes(p, force_fresh=True)

    assert got == BASE + TAIL, "mirror must equal what a full GET would return"
    assert p.read_bytes() == BASE + TAIL
    assert spy.ranges == [f"bytes={len(BASE)}-"], (
        "exactly one RANGE get, starting at the old mirror size")
    assert spy.fulls == [], "no full GET may follow a successful tail pull"
    # The fence and the persistent baseline must both advance, or the next
    # refresh re-downloads and the saving evaporates.
    assert b._etags[key].strip('"') == hashlib.md5(BASE + TAIL).hexdigest()
    man = json.loads((cloud["root"] / "_owncloud_rt"
                      / "owncloud-sync-manifest.json").read_text())
    assert man["world/board/general.jsonl"]["md5"] == \
        hashlib.md5(BASE + TAIL).hexdigest()


def test_range_tail_fetches_only_the_delta_bytes(cloud, monkeypatch):
    """The cost claim itself: bytes off the wire == the appended delta, not
    the object. Asserted on the RANGE header rather than inferred."""
    b, p, key = _prime(cloud)
    big_tail = TAIL * 20
    _append_remote(cloud, key, BASE, big_tail)

    spy = _GetSpy(b.s3.get_object)
    monkeypatch.setattr(b.s3, "get_object", spy)
    b.read_bytes(p, force_fresh=True)

    start = int(spy.ranges[0].split("=")[1].rstrip("-"))
    assert start == len(BASE)
    assert (len(BASE) + len(big_tail)) - start == len(big_tail)


# --- 2. in-place edit -> proof fails -> full GET ------------------------------
def test_in_place_edit_falls_back_to_full_get(cloud, monkeypatch):
    b, p, key = _prime(cloud)
    # Same-prefix-length is not enough: change a byte INSIDE the prefix and
    # also grow, so the size guard passes but the md5 proof cannot.
    edited = BASE.replace(b"seed", b"SEED", 1) + TAIL
    cloud["s3"].put_object(Bucket=BUCKET, Key=key, Body=edited)

    spy = _GetSpy(b.s3.get_object)
    monkeypatch.setattr(b.s3, "get_object", spy)
    got = b.read_bytes(p, force_fresh=True)

    assert got == edited, "fallback must produce the correct bytes"
    assert p.read_bytes() == edited
    assert len(spy.ranges) == 1, "the tail was attempted (cheap probe)"
    assert len(spy.fulls) == 1, "and the full GET repaired it"


# --- 3. gz object: skipped by construction -----------------------------------
def test_encoded_object_never_takes_the_tail_path(cloud, monkeypatch):
    b = _backend(cloud)
    p = cloud["root"].joinpath(*BOARD_REL)
    key = b._s3_key(p)
    cloud["s3"].put_object(Bucket=BUCKET, Key=key, **codec.put_kwargs(BASE))
    assert b.read_bytes(p, force_fresh=True) == BASE
    cloud["s3"].put_object(Bucket=BUCKET, Key=key,
                           **codec.put_kwargs(BASE + TAIL))

    spy = _GetSpy(b.s3.get_object)
    monkeypatch.setattr(b.s3, "get_object", spy)
    assert b.read_bytes(p, force_fresh=True) == BASE + TAIL
    assert spy.ranges == [], (
        "a gzip tail is not a suffix of the plaintext — must never be tried")
    assert len(spy.fulls) == 1


# --- 4. multipart ETag: skipped ----------------------------------------------
def test_multipart_etag_skips_tail(cloud):
    b, p, key = _prime(cloud)
    head = b.s3.head_object(Bucket=BUCKET, Key=key)
    head = dict(head)
    head["ContentLength"] = len(BASE) + len(TAIL)
    assert b._range_tail_pull(p, key, head, p, '"deadbeef-2"') is None


# --- 5. locally-modified mirror: no_clobber wins ------------------------------
def test_locally_modified_mirror_is_not_clobbered_or_tailed(cloud, monkeypatch):
    b, p, key = _prime(cloud)
    p.write_bytes(BASE + b'{"id": "local-unpushed"}\n')   # local diverges
    _append_remote(cloud, key, BASE, TAIL)                # and S3 moved

    spy = _GetSpy(b.s3.get_object)
    monkeypatch.setattr(b.s3, "get_object", spy)
    got = b.read_bytes(p, force_fresh=True)

    assert got == BASE + b'{"id": "local-unpushed"}\n', (
        "the unpushed local write must survive (no-clobber semantics)")
    assert spy.calls == [], "no GET at all on the both-diverged path"
    assert key in b._diverged_keys


# --- 6. remote shrank: skipped ------------------------------------------------
def test_remote_shrank_skips_tail(cloud, monkeypatch):
    b, p, key = _prime(cloud)
    shrunk = BASE[: len(BASE) // 2]
    cloud["s3"].put_object(Bucket=BUCKET, Key=key, Body=shrunk)

    spy = _GetSpy(b.s3.get_object)
    monkeypatch.setattr(b.s3, "get_object", spy)
    assert b.read_bytes(p, force_fresh=True) == shrunk
    assert spy.ranges == [], "nothing to append when the object got smaller"
    assert len(spy.fulls) == 1


# --- 7. FORCED-FAILURE CONTROL ------------------------------------------------
def test_wrong_tail_bytes_are_rejected_and_full_get_repairs(cloud, monkeypatch):
    """guard-4166: the load-bearing control. If the md5 proof were skipped (or
    the equality inverted), this test writes CORRUPTION into the mirror. It
    passes only because a bad tail is rejected and the full GET repairs it."""
    b, p, key = _prime(cloud)
    _append_remote(cloud, key, BASE, TAIL)

    real = b.s3.get_object
    calls = []

    def _poisoned(**kw):
        calls.append(kw.get("Range"))
        if kw.get("Range"):
            class _B:
                @staticmethod
                def read():
                    return b"XXXX-not-the-real-tail-XXXX"
            return {"Body": _B()}
        return real(**kw)

    monkeypatch.setattr(b.s3, "get_object", _poisoned)
    got = b.read_bytes(p, force_fresh=True)

    assert got == BASE + TAIL, "the poisoned tail must NOT reach the mirror"
    assert p.read_bytes() == BASE + TAIL
    assert calls[0] is not None, "the range attempt happened"
    assert None in calls, "and the full GET repaired it"


# --- 8. allowlist actually filters --------------------------------------------
def test_non_allowlisted_store_skips_tail(cloud, monkeypatch):
    """aspirations.jsonl is an in-place-edited store (: it SHRINKS), so
    it is deliberately outside _RANGE_TAIL_STORES. Without this pin the
    allowlist could be emptied and every other test here would still pass."""
    b, p, key = _prime(cloud, rel_parts=("world", "aspirations.jsonl"))
    _append_remote(cloud, key, BASE, TAIL)

    spy = _GetSpy(b.s3.get_object)
    monkeypatch.setattr(b.s3, "get_object", spy)
    assert b.read_bytes(p, force_fresh=True) == BASE + TAIL
    assert spy.ranges == [], "not an allowlisted append-mostly store"
    assert len(spy.fulls) == 1


def test_allowlist_covers_the_two_stores_the_goal_names(cloud):
    import owncloud_backend as B
    assert "world/board/" in B._RANGE_TAIL_STORES
    assert "world/changelog.jsonl" in B._RANGE_TAIL_STORES


# --- 9. the  widening: new members, and what it must NOT swallow ----
def test_allowlist_covers_every_class_a_store_the_goal_names(cloud):
    """ item (1) is "implement the range read for class A", and that
    goal names FIVE class-A (append-only, byte-range sound) stores. Only TWO
    were reachable -- the board pair, via the "world/board/" prefix -- so the
    feature shipped covering 2/5 of its own stated scope while every test here
    passed. This pins all five plus the two changelogs g-358-17 shipped with.

    The board pair is asserted by PREFIX rather than by channel name because
    that is what the matcher actually uses; naming the channels would pin a
    stronger claim than the code makes.
    """
    import owncloud_backend as B
    assert "world/board/" in B._RANGE_TAIL_STORES
    for rel in ("world/productivity-snapshots.jsonl",
                "world/goal-duplication-overrides.jsonl",
                "meta/trigger-firings.jsonl"):
        assert rel in B._RANGE_TAIL_STORES, f"class-A store {rel} unreachable"
    assert "world/changelog.jsonl" in B._RANGE_TAIL_STORES
    assert "meta/changelog.jsonl" in B._RANGE_TAIL_STORES


def test_widening_does_not_swallow_gate_firings_or_its_segments(cloud):
    """guard-2201 pins what a widening must not LOSE (the REMOVED set); this
    pins what it must not GAIN. Three near-misses sit beside the entries added
    by g-115-5268 and all three must stay OUT:

      meta/gate-firings.jsonl             g-358-03 MEASURED it shrinking, so the
          md5 proof fails on every pull and the range GET is pure cost.
          `_gate_log.py` calls it "(legacy, append-only)" -- that describes its
          WRITE IDIOM (locked_append_jsonl), not its size trajectory, and
          mistaking one for the other is precisely the trap this test keeps shut.
      meta/gate-firings-YYYY-MM-DD.jsonl  date segments
      meta/gate-firings.spool.jsonl       machine-local, never synced

    A future widening reaching for "meta/gate-firings" as a PREFIX would pull in
    all three silently. Asserted THROUGH the production predicate
    (`rel == s or rel.startswith(s)`) rather than by eyeballing the tuple, so a
    change to the matching rule is caught too.
    """
    import owncloud_backend as B

    def matches(rel):
        return any(rel == s or rel.startswith(s) for s in B._RANGE_TAIL_STORES)

    for rel in ("meta/gate-firings.jsonl",
                "meta/gate-firings-2026-08-21.jsonl",
                "meta/gate-firings.spool.jsonl"):
        assert not matches(rel), f"{rel} must stay outside the allowlist"

    # POSITIVE CONTROL (guard-2421): without this, a `matches` that returned
    # False for everything would make all three assertions above pass while
    # asserting nothing at all.
    assert matches("meta/trigger-firings.jsonl")
    assert matches("world/board/general.jsonl")
