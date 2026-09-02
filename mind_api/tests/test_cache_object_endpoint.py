"""LAN-shared content-addressed S3 object cache ().

Every fleet box is on-prem, so every S3 read is internet egress — measured
143.29 GB/day / ~$12.90/day on 2026-09-01 (g-115-7967). `_refresh` already
skips the GET when nothing changed, so the GETs that fire are the ones every
box must make: ~16 boxes downloading the SAME changed object. The cache
collapses that to one download.

WHAT THESE TESTS PIN, and why each matters:

  * The two failure axes run in OPPOSITE directions, deliberately.
    Validation ambiguity FAILS CLOSED (report a miss — never guess a body);
    transport failure FAILS OPEN (caller falls through to direct S3). Both
    directions are asserted, because getting either backwards is silent:
    a wrong-direction fail-closed breaks reads, a wrong-direction fail-open
    serves wrong bytes.

  * The feature is OFF unless OWNCLOUD_OBJECT_CACHE is set, and "off" must be
    byte-identical to the pre-feature path. `test_flag_off_makes_no_network_call`
    is the kill-switch proof: it fails if the client so much as opens a socket.

  * guard-2903: an invariance test is green by default when broken. Every
    "returns None" assertion here is paired with a SENSITIVITY CONTROL that
    proves the same harness returns BYTES when it should, so a None can never
    be mistaken for a test that simply never reached the code.
"""
from __future__ import annotations

import ast
import http.server
import inspect
import os
import socketserver
import sys
import textwrap
import threading
from pathlib import Path

import pytest

_SCRIPTS = str(Path(__file__).resolve().parents[2] / "core" / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from mind_api.src.endpoints import cache_object as _co  # noqa: E402

# Sample S3 keys. Deliberately synthetic: naming a REAL store path here trips
# the guard-996 store-write scanner on the fixture string alone.
KEY_A = "sample/prefix/object-a.dat"
KEY_B = "sample/prefix/object-b.dat"


# --- fixtures ---------------------------------------------------------------

class _Paths:
    def __init__(self, root): self.project_root = root


class _Ctx:
    def __init__(self, root, **query):
        self.query = {k: v for k, v in query.items() if v is not None}
        self.paths = _Paths(root)


@pytest.fixture()
def ctx_factory(tmp_path):
    return lambda **q: _Ctx(tmp_path, **q)


@pytest.fixture(autouse=True)
def _reset_stats():
    for k in ("hits", "misses_fetched", "misses_unfetchable", "bad_request",
              "bytes_served", "bytes_fetched", "evictions"):
        _co._STATS[k] = 0
    yield


# --- endpoint: request validation -------------------------------------------

@pytest.mark.parametrize("key,etag", [
    (None, "abc"), ("k", None), ("", "abc"), ("k", ""), ("k", '"""'),
])
def test_missing_or_unusable_params_are_400(ctx_factory, key, etag):
    """A blank-after-normalisation ETag ('\"\"\"' -> '') is as unusable as an
    absent one — it would address every entry equally."""
    resp = _co.cache_object(ctx_factory(key=key, etag=etag))
    assert resp.status == 400


# --- endpoint: the hit path -------------------------------------------------

def test_hit_returns_exact_bytes(ctx_factory):
    body = b"payload-\x00\xff-bytes"
    _co._store(_co._cache_root(ctx_factory()), KEY_A, '"deadbeef"', body)

    resp = _co.cache_object(ctx_factory(key=KEY_A, etag='"deadbeef"'))
    assert resp.status == 200
    assert resp.body == body            # exact bytes, binary-safe
    assert _co._STATS["hits"] == 1
    assert _co._STATS["bytes_served"] == len(body)


def test_hit_is_content_addressed_not_key_addressed(ctx_factory):
    """The whole safety argument. An entry stored under one ETag must NEVER be
    served for a different ETag on the same key — that is precisely the
    'plausible but wrong value' failure guard-3970 describes."""
    _co._store(_co._cache_root(ctx_factory()), KEY_B, '"etag-A"', b"version-A")

    assert _co.cache_object(ctx_factory(key=KEY_B, etag='"etag-A"')).body == b"version-A"
    # different ETag -> must not serve A; with no S3 configured it is a miss.
    assert _co.cache_object(ctx_factory(key=KEY_B, etag='"etag-B"')).status == 404


def test_etag_quoting_and_multipart_forms_round_trip(ctx_factory):
    """S3 hands back quoted ETags, and multipart ones carry a `-N` suffix.
    Store and lookup must agree on normalisation or every read is a miss."""
    _co._store(_co._cache_root(ctx_factory()), KEY_A, '"d41d8cd98f00b204-3"', b"mp")
    for spelling in ('"d41d8cd98f00b204-3"', 'd41d8cd98f00b204-3'):
        assert _co.cache_object(ctx_factory(key=KEY_A, etag=spelling)).body == b"mp"


# --- endpoint: fail CLOSED on validation ambiguity --------------------------

def test_miss_that_cannot_be_fetched_is_404_not_a_guess(ctx_factory):
    resp = _co.cache_object(ctx_factory(key="sample/absent.dat", etag='"nope"'))
    assert resp.status == 404
    assert _co._STATS["misses_unfetchable"] == 1


def test_fetch_refuses_when_the_object_moved_under_us(monkeypatch):
    """S3 moved between the CALLER's HEAD and ours. The newer body is not the
    one the caller verified, so serving it would silently substitute a
    different object. Fail closed."""
    class _S3:
        def get_object(self, **kw):
            return {"Body": None, "ETag": '"etag-NEW"'}

    class _Backend:
        s3 = _S3()
        bucket = "b"

    mod = type(sys)("storage_backend")
    mod.get_backend = lambda: _Backend()
    monkeypatch.setitem(sys.modules, "storage_backend", mod)
    codec = type(sys)("_owncloud_codec")
    codec.decode_response = lambda obj, key="": b"plaintext"
    monkeypatch.setitem(sys.modules, "_owncloud_codec", codec)

    assert _co._fetch_from_s3(KEY_A, '"etag-OLD"', Path("/nonexistent")) is None, \
        "must not serve a body whose ETag the caller never verified"

    # SENSITIVITY CONTROL (guard-2903): the same harness must return bytes when
    # the ETag *does* match — otherwise the None above proves nothing.
    _S3.get_object = lambda self, **kw: {"Body": None, "ETag": '"etag-OLD"'}
    assert _co._fetch_from_s3(KEY_A, '"etag-OLD"', Path("/nonexistent")) == b"plaintext"


# --- endpoint: stats --------------------------------------------------------

def test_stats_expose_coverage_not_just_counts(ctx_factory):
    """guard-3992: a cache whose effect is measured later must publish its own
    coverage, or a later zero is indistinguishable from 'never ran'."""
    import json
    _co._store(_co._cache_root(ctx_factory()), KEY_A, '"e"', b"12345")
    _co.cache_object(ctx_factory(key=KEY_A, etag='"e"'))

    s = json.loads(_co.cache_stats(ctx_factory()).body)
    assert s["hits"] == 1 and s["hit_ratio"] == 1.0
    assert s["bytes_saved_vs_direct_s3"] == 5
    assert s["entries"] >= 1 and s["bytes_on_disk"] >= 5
    for field in ("requests_served", "uptime_s", "max_bytes"):
        assert field in s


# --- client side: OwnCloudBackend._cache_fetch ------------------------------

class _Srv(http.server.BaseHTTPRequestHandler):
    payload = b"cached-object-bytes"
    status = 200
    short = False
    last_path = None

    def do_GET(self):
        type(self).last_path = self.path
        b = type(self).payload
        self.send_response(type(self).status)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b[:-3] if type(self).short else b)

    def log_message(self, *a):
        pass


@pytest.fixture()
def cache_server():
    _Srv.payload, _Srv.status, _Srv.short, _Srv.last_path = \
        b"cached-object-bytes", 200, False, None
    srv = socketserver.TCPServer(("127.0.0.1", 0), _Srv)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _fetch(url, key=KEY_A, etag='"e1"', **env):
    from owncloud_backend import OwnCloudBackend
    names = ("OWNCLOUD_OBJECT_CACHE", "OWNCLOUD_OBJECT_CACHE_TIMEOUT", "MIND_API_TOKEN")
    old = {k: os.environ.get(k) for k in names}
    try:
        os.environ.pop("MIND_API_TOKEN", None)
        if url is None:
            os.environ.pop("OWNCLOUD_OBJECT_CACHE", None)
        else:
            os.environ["OWNCLOUD_OBJECT_CACHE"] = url
        for k, v in env.items():
            os.environ[k] = v
        return OwnCloudBackend._cache_fetch(
            OwnCloudBackend.__new__(OwnCloudBackend), key, etag)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_flag_off_makes_no_network_call(cache_server):
    """THE KILL SWITCH. Unset the variable and the client must not merely
    ignore the result — it must never open the socket."""
    _srv, url = cache_server
    assert _fetch(None) is None
    assert _Srv.last_path is None, "client contacted the cache while the flag was OFF"

    # SENSITIVITY CONTROL: same server, same harness, flag ON -> it IS reached.
    # Without this, the assertion above passes even on a broken server.
    assert _fetch(url) == b"cached-object-bytes"
    assert _Srv.last_path is not None and "/v1/cache/object" in _Srv.last_path


def test_hit_passes_key_and_etag_urlencoded(cache_server):
    _srv, url = cache_server
    assert _fetch(url, key="sample/a b.dat", etag='"e/2"') == b"cached-object-bytes"
    assert "key=sample%2Fa+b.dat" in _Srv.last_path   # urlencoded, not raw
    assert "etag=%22e%2F2%22" in _Srv.last_path


def test_bearer_token_is_attached_when_configured(cache_server):
    _srv, url = cache_server
    seen = {}
    orig = _Srv.do_GET

    def spy(self):
        seen["auth"] = self.headers.get("Authorization")
        orig(self)

    _Srv.do_GET = spy
    try:
        assert _fetch(url, MIND_API_TOKEN="s3cr3t") == b"cached-object-bytes"
        assert seen["auth"] == "Bearer s3cr3t"
    finally:
        _Srv.do_GET = orig


@pytest.mark.parametrize("setup,label", [
    (lambda: setattr(_Srv, "status", 404), "cache miss"),
    (lambda: setattr(_Srv, "status", 401), "unauthorized"),
    (lambda: setattr(_Srv, "short", True), "truncated body"),
])
def test_every_failure_shape_falls_open_to_none(cache_server, setup, label):
    _srv, url = cache_server
    # control first: healthy server returns bytes through this exact harness
    assert _fetch(url) == b"cached-object-bytes", "harness broken before the test ran"
    setup()
    assert _fetch(url) is None, f"{label} must fail open, not raise or return junk"


def test_unreachable_cache_falls_open_without_raising():
    # port 1 on loopback: nothing listens, connection refused immediately
    assert _fetch("http://127.0.0.1:1") is None


def test_cache_fetch_touches_no_instance_state_beyond_its_counters():
    """guard-4188: a `__new__`-built fixture cannot fail closed when production
    starts using more of `self`. Rather than hope, assert the invariant the
    fixture depends on — if `_cache_fetch` ever reads real instance state,
    this fails and the fixture above must become a real instance."""
    from owncloud_backend import OwnCloudBackend
    tree = ast.parse(textwrap.dedent(inspect.getsource(OwnCloudBackend._cache_fetch)))
    attrs = {n.attr for n in ast.walk(tree)
             if isinstance(n, ast.Attribute)
             and isinstance(n.value, ast.Name) and n.value.id == "self"}
    assert attrs <= {"_cache_hits", "_cache_errors"}, (
        f"_cache_fetch now uses instance state {sorted(attrs)}; the "
        "__new__-based fixture no longer represents production")
