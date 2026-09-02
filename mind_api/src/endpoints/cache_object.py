"""GET /v1/cache/object — LAN-shared, content-addressed S3 object cache.

WHY THIS EXISTS
---------------
Every box in the fleet is on-prem, so every S3 read is INTERNET egress
(usage type ``USE2-DataTransfer-Out-Bytes``). Measured 2026-09-01 (g-115-7967,
Cost Explorer, settled days): **143.29 GB/day, ~$12.90/day**. The bulk of it is
amplification, not volume: ~16 boxes each independently download the SAME
changed ``world/`` object. ``OwnCloudBackend._refresh`` already avoids the GET
when nothing changed (TTL fast-path, then the ETag fence), so the GETs that DO
fire are exactly the ones every box must make — N downloads of one object.

This endpoint collapses that N to 1. One box holds a copy per (key, ETag) and
serves it over the LAN; the other readers take it from there instead of S3.

WHY IT IS SAFE — CONTENT ADDRESSING, NOT VALIDATION
---------------------------------------------------
The caller has ALREADY done ``head_object`` and holds the S3-authoritative
ETag before it asks us anything (``_refresh`` HEADs, compares its fence, and
only then reaches the GET). So the request names a specific content hash that
the CALLER verified against S3 microseconds earlier. This cache therefore does
NOT validate, re-HEAD, or decide freshness — it is a pure content-addressed
blob store keyed on (key, etag). A hit is byte-equivalent to what
``get_object`` would have returned, by construction.

That is a deliberately stronger contract than "cache validates by HEAD". It
removes the whole class guard-3970 warns about (a fallback chain manufacturing
a plausible-but-wrong value that then reads as data): we can only ever return
bytes stored under the exact ETag asked for, so there is no "close enough"
body to return. The two failure axes are handled in OPPOSITE directions,
on purpose:

  * validation ambiguity  -> FAIL CLOSED (report a miss; never guess a body)
  * transport failure     -> FAIL OPEN   (caller falls through to direct S3)

The caller owns the fail-open half (``OwnCloudBackend._cache_fetch`` swallows
everything and returns None). This module owns the fail-closed half.

WHAT IS SERVED
--------------
The DECODED plaintext — the exact bytes ``_refresh`` writes to the local
mirror. Decoding is a pure function of the raw object (``_owncloud_codec``
reads Body + ContentEncoding + Metadata), so (key, etag) -> plaintext is
well-defined and every reader gets an identical answer.

WRITES ARE NOT AFFECTED. This is a read-side cache only: the fencing /
If-Match PUT path (rb-2639) never consults it.

EXPOSURE
--------
This serves object bytes, so it is only as safe as the daemon's bind. The
daemon binds 127.0.0.1 by default and a non-loopback bind is already
fail-closed behind ``MIND_API_TOKEN`` (FR-5, ``server.py``). Nothing here
loosens that; the LAN deployment is exactly the FR-5 "one canonical daemon"
case that mechanism was built for.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from ..agent_paths import assert_not_cruft
from ..server import Response

# --- tunables ---------------------------------------------------------------
# Cap the on-disk cache. A measured world mirror is 17-25 GB (three boxes,
# 2026-09-01), but this holds only objects in active rotation, so the cap is
# far below that and evicts oldest-first.
_MAX_BYTES = int(os.environ.get("MIND_API_CACHE_MAX_BYTES", str(4 * 1024 * 1024 * 1024)))
# Refuse to cache a single object larger than this (streaming it would pin RAM).
_MAX_OBJECT_BYTES = int(os.environ.get("MIND_API_CACHE_MAX_OBJECT_BYTES", str(256 * 1024 * 1024)))

_LOCK = threading.Lock()

# Counters. guard-3992: a cache whose effectiveness is measured later must
# expose its own coverage, or a zero is indistinguishable from "never ran".
_STATS = {
    "hits": 0, "misses_fetched": 0, "misses_unfetchable": 0,
    "bad_request": 0, "bytes_served": 0, "bytes_fetched": 0,
    "evictions": 0, "started_at": time.time(),
}

_ETAG_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def _cache_root(ctx) -> Path:
    """Machine-local cache home. ``mind_api/state/`` is gitignored and already
    holds per-daemon runtime files, so this adds no new top-level entry under
    any governed root (L1 gate) and never syncs anywhere."""
    root = Path(ctx.paths.project_root) / "mind_api" / "state" / "object-cache"
    #  tripwire: a non-absolute project_root would silently mirror a
    # whole tree under cwd instead of failing. Loud refusal beats a shadow tree.
    assert_not_cruft(root, "mkdir (object-cache root)")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _normalise_etag(etag: str) -> str:
    """S3 ETags arrive quoted (``"abc123"``) and sometimes URL-escaped. Strip to
    a stable filesystem-safe token. Multipart ETags (``<md5>-<n>``) survive
    intact — they are still a valid content identity for OUR purposes because
    we only ever compare for EQUALITY against the caller's HEAD."""
    return _ETAG_SAFE.sub("", (etag or "").strip().strip('"'))


def _entry_path(root: Path, key: str, etag: str) -> Path:
    # Hash the key: S3 keys contain '/' and can exceed filesystem name limits.
    kh = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return root / kh[:2] / kh / _normalise_etag(etag)


def _evict_if_oversize(root: Path) -> None:
    """Oldest-first eviction to bring the tree back under _MAX_BYTES.

    Fail-quiet toward NOT evicting: an OSError here must never fail a request
    that is otherwise serviceable."""
    try:
        entries = []
        total = 0
        for p in root.rglob("*"):
            if p.is_file():
                try:
                    st = p.stat()
                except OSError:
                    continue
                entries.append((st.st_mtime, st.st_size, p))
                total += st.st_size
        if total <= _MAX_BYTES:
            return
        entries.sort(key=lambda t: t[0])
        for _mtime, size, p in entries:
            if total <= _MAX_BYTES:
                break
            try:
                p.unlink()
                total -= size
                _STATS["evictions"] += 1
            except OSError:
                continue
    except OSError:
        return


def _fetch_from_s3(key: str, etag: str, project_root: Path) -> Optional[bytes]:
    """Fetch + decode exactly this key, and return the plaintext ONLY if the
    object's live ETag still equals the one the caller asked for.

    The ETag equality check is the fail-closed half: if S3 moved between the
    caller's HEAD and ours, the caller wants the version IT verified, and we
    do not have it. Returning the newer body would hand back bytes whose ETag
    the caller never validated — silently substituting a different object for
    the one requested. Report a miss instead and let the caller fetch."""
    scripts_dir = str(Path(project_root) / "core" / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    try:
        from storage_backend import get_backend
        from _owncloud_codec import decode_response
    except Exception:
        return None
    try:
        backend = get_backend()
    except Exception:
        return None
    s3 = getattr(backend, "s3", None)
    bucket = getattr(backend, "bucket", None)
    if s3 is None or not bucket:
        return None  # LocalBackend or an un-configured remote — nothing to serve
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
    except Exception:
        return None
    live_etag = _normalise_etag(obj.get("ETag") or "")
    if live_etag != _normalise_etag(etag):
        return None  # moved under us — fail closed, never substitute
    try:
        body = decode_response(obj, key=key)
    except Exception:
        return None
    if body is None or len(body) > _MAX_OBJECT_BYTES:
        return None
    return body


def _store(root: Path, key: str, etag: str, body: bytes) -> None:
    """Write the entry atomically. Fail-quiet: a cache that cannot persist
    still served a correct body this request."""
    dest = _entry_path(root, key, etag)
    tmp = dest.with_name(dest.name + f".tmp{os.getpid()}")
    try:
        assert_not_cruft(dest.parent, "mkdir (object-cache entry)")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(body)
        os.replace(tmp, dest)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return
    _evict_if_oversize(root)


def cache_object(ctx) -> Response:
    key = (ctx.query.get("key") or "").strip()
    etag = (ctx.query.get("etag") or "").strip()
    if not key or not etag or not _normalise_etag(etag):
        with _LOCK:
            _STATS["bad_request"] += 1
        return Response.error(400, "bad_request",
                              "both 'key' and 'etag' are required")

    root = _cache_root(ctx)
    entry = _entry_path(root, key, etag)

    # --- hit -----------------------------------------------------------
    try:
        if entry.is_file():
            body = entry.read_bytes()
            try:
                os.utime(entry, None)  # LRU touch; best-effort
            except OSError:
                pass
            with _LOCK:
                _STATS["hits"] += 1
                _STATS["bytes_served"] += len(body)
            return Response(200, body, "application/octet-stream")
    except OSError:
        pass  # unreadable entry -> treat as a miss, never as an error

    # --- miss: fetch once, serve, keep for the next reader ---------------
    body = _fetch_from_s3(key, etag, Path(ctx.paths.project_root))
    if body is None:
        with _LOCK:
            _STATS["misses_unfetchable"] += 1
        return Response.error(404, "not_cached",
                              "no entry for this (key, etag) and it could not "
                              "be fetched — caller should read S3 directly")
    _store(root, key, etag, body)
    with _LOCK:
        _STATS["misses_fetched"] += 1
        _STATS["bytes_fetched"] += len(body)
        _STATS["bytes_served"] += len(body)
    return Response(200, body, "application/octet-stream")


def cache_stats(ctx) -> Response:
    with _LOCK:
        s = dict(_STATS)
    served = s["hits"] + s["misses_fetched"]
    s["requests_served"] = served
    # The number the whole feature exists to move: bytes callers got from the
    # LAN instead of S3. Reported explicitly so a later effectiveness
    # measurement reads a real figure rather than inferring one (guard-3992).
    s["bytes_saved_vs_direct_s3"] = s["bytes_served"] - s["bytes_fetched"]
    s["hit_ratio"] = round(s["hits"] / served, 4) if served else None
    s["uptime_s"] = round(time.time() - s["started_at"], 1)
    s["max_bytes"] = _MAX_BYTES
    try:
        root = _cache_root(ctx)
        n = 0
        total = 0
        for p in root.rglob("*"):
            if p.is_file():
                n += 1
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
        s["entries"] = n
        s["bytes_on_disk"] = total
    except OSError:
        s["entries"] = None
        s["bytes_on_disk"] = None
    return Response.json(s)


def register(routes) -> None:
    routes[("GET", "/v1/cache/object")] = cache_object
    routes[("GET", "/v1/cache/stats")] = cache_stats
