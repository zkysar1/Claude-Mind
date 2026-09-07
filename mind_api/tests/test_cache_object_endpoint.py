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
    # Reset EVERY counter, not a hardcoded list. This tuple named seven keys and
    # silently stopped covering _STATS when `superseded_purged` /
    # `superseded_bytes_reclaimed` were added (, 2026-09-07): the new
    # counters leaked across tests and read as phantom counts — 4 where the test
    # asserted 1 — which looks like a production bug and is not one. Deriving the
    # key set from _STATS makes a future counter covered by existing, the same
    # generality argument as guard-5971 one layer down.
    # `started_at` is a timestamp, not a counter, and must survive the reset.
    for k in list(_co._STATS):
        if k != "started_at":
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
    names = ("OWNCLOUD_OBJECT_CACHE", "OWNCLOUD_OBJECT_CACHE_TIMEOUT",
             "OWNCLOUD_OBJECT_CACHE_TOKEN", "MIND_API_TOKEN")
    old = {k: os.environ.get(k) for k in names}
    try:
        # BOTH credential keys are popped, not just the one a given test sets:
        # an ambient value for either would silently supply an Authorization
        # header and make the token tests assert against the box's env.
        os.environ.pop("MIND_API_TOKEN", None)
        os.environ.pop("OWNCLOUD_OBJECT_CACHE_TOKEN", None)
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


def _seen_auth(url, **env):
    """Run one _fetch and return the Authorization header the server saw."""
    seen = {}
    orig = _Srv.do_GET

    def spy(self):
        seen["auth"] = self.headers.get("Authorization")
        orig(self)

    _Srv.do_GET = spy
    try:
        assert _fetch(url, **env) == b"cached-object-bytes"
        return seen["auth"]
    finally:
        _Srv.do_GET = orig


def test_cache_token_alone_authenticates_without_mind_api_token(cache_server):
    """ item 5 (option B — decouple). THE POINT OF THE SECOND KEY:
    a client box can carry the cache credential while MIND_API_TOKEN stays
    UNSET, so its own daemon is not flipped to FR-4 auth-required (server.py
    reads MIND_API_TOKEN to decide that, and under daemon-only architecture
    that flip's blast radius is a total agent wedge rather than degradation).

    The fallback leg — MIND_API_TOKEN alone still authenticates, so boxes that
    never set the new key are byte-identical — is pinned by
    test_bearer_token_is_attached_when_configured above.
    """
    _srv, url = cache_server
    assert _seen_auth(url, OWNCLOUD_OBJECT_CACHE_TOKEN="cache-only") \
        == "Bearer cache-only"


def test_cache_token_wins_when_both_are_set(cache_server):
    """Precedence, stated rather than left to the `or`: the DEDICATED key is
    the cache client's credential, so a box that sets both (the cache host
    itself, which needs MIND_API_TOKEN server-side anyway) presents the cache
    token. Without this pin the two keys could silently swap precedence and
    every box would still authenticate, so nothing else would fail."""
    _srv, url = cache_server
    assert _seen_auth(url, OWNCLOUD_OBJECT_CACHE_TOKEN="cache-only",
                      MIND_API_TOKEN="s3cr3t") == "Bearer cache-only"


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


# ── superseded-version reclaim (, 2026-09-07) ────────────────────────
# Entries are addressed by (key, etag), so every write to a high-churn key mints
# a new entry and strands the old one. Nothing reclaimed those but LRU, so they
# competed with live entries for the cap. Measured on cc-03 before the fix: 89
# distinct keys occupying 592 entries, 95.4% of 4.38 GB held by superseded
# versions — the hottest world store alone kept 130 copies for 3.68 GB (84% of
# the cache), which is why hit_ratio sat at 0.60 with 1,290 evictions in 2.42h.
# (Store paths are named generically on purpose: a real one trips the guard-996
# scanner on the comment text alone, as this module's header already warns.)

def _entry_dir(ctx, key, etag):
    return _co._entry_path(_co._cache_root(ctx), key, etag).parent


def _live_entries(ctx, key, etag):
    d = _entry_dir(ctx, key, etag)
    return sorted(p.name for p in d.iterdir() if p.is_file()) if d.is_dir() else []


def test_storing_a_new_etag_purges_the_superseded_one(ctx_factory):
    ctx = ctx_factory()
    root = _co._cache_root(ctx)
    _co._store(root, KEY_A, '"v1"', b"body-one")
    assert len(_live_entries(ctx, KEY_A, '"v1"')) == 1, "precondition: v1 stored"

    _co._store(root, KEY_A, '"v2"', b"body-two-longer")

    names = _live_entries(ctx, KEY_A, '"v2"')
    assert len(names) == 1, f"superseded version was not reclaimed: {names}"
    assert _co._STATS["superseded_purged"] == 1
    assert _co._STATS["superseded_bytes_reclaimed"] == len(b"body-one")
    # The survivor must be v2, not merely "some one file" — a purge that dropped
    # the NEW entry instead would also leave exactly one.
    assert (_co._entry_path(root, KEY_A, '"v2"')).read_bytes() == b"body-two-longer"


def test_purge_is_scoped_to_the_key_and_never_the_whole_cache(ctx_factory):
    """SENSITIVITY CONTROL (guard-2903) for the test above.

    A purge implemented as "clear the cache on every write" would make that test
    pass too. This proves the blast radius is one key's directory: an unrelated
    key's entry must survive a purge triggered by KEY_A.
    """
    ctx = ctx_factory()
    root = _co._cache_root(ctx)
    _co._store(root, KEY_B, '"other"', b"untouched")
    _co._store(root, KEY_A, '"v1"', b"body-one")
    _co._store(root, KEY_A, '"v2"', b"body-two")

    assert _live_entries(ctx, KEY_B, '"other"') != [], "unrelated key was purged"
    assert (_co._entry_path(root, KEY_B, '"other"')).read_bytes() == b"untouched"
    assert _co._STATS["superseded_purged"] == 1  # only KEY_A's v1


def test_purge_leaves_a_concurrent_writers_tmp_file_alone(ctx_factory):
    """A `.tmp<pid>` sibling is another process mid-`os.replace`.

    Deleting it turns a correct write into a silent loss, so the purge must skip
    it. Asserted directly because the failure is invisible at runtime: the other
    writer's os.replace simply raises and its fail-quiet handler swallows it.
    """
    ctx = ctx_factory()
    root = _co._cache_root(ctx)
    _co._store(root, KEY_A, '"v1"', b"body-one")
    tmp = _entry_dir(ctx, KEY_A, '"v1"') / "somefile.tmp999999"
    tmp.write_bytes(b"in-flight")

    _co._store(root, KEY_A, '"v2"', b"body-two")

    assert tmp.exists(), "purge deleted a concurrent writer's staging file"
    assert _co._STATS["superseded_purged"] == 1  # v1 only, not the tmp


def test_stats_expose_the_reclaim_separately_from_evictions(ctx_factory):
    """`evictions` means the cap was too small; `superseded_purged` means the
    entry was garbage. Folding them makes cap-thrash undiagnosable (guard-3992).
    """
    import json as _json
    ctx = ctx_factory()
    root = _co._cache_root(ctx)
    _co._store(root, KEY_A, '"v1"', b"aaaa")
    _co._store(root, KEY_A, '"v2"', b"bbbb")

    payload = _json.loads(_co.cache_stats(ctx).body)
    assert payload["superseded_purged"] == 1
    assert payload["superseded_bytes_reclaimed"] == 4
    assert payload["evictions"] == 0, "a superseded purge must not read as an eviction"
