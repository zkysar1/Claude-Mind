# domain-leak-exempt: remote object-store transport codec — the S3 header /
# metadata names it manipulates (ContentEncoding, x-amz-meta-*) are functional
# infrastructure for the own-cloud storage tier, not domain examples.
"""Transport codec for own-cloud governed-store objects (g-358-11).

WHY THIS EXISTS. Every byte of every governed store crosses the wire raw:
`OwnCloudBackend` stores plain JSONL/YAML and `_refresh` GETs the WHOLE
object on any ETag change, so a 19 MB `aspirations.jsonl` that changes every
~2 minutes fans out as ~19 MB to every reading box per change. Measured on
2026-08-17 (cc-10, gzip level 6): aspirations.jsonl 2.7x, reasoning-bank
3.3x, guardrails 3.1x, board 2.7-3.0x, gate-firings 17x, its date segments
26x. Compressing at THIS seam cuts both GET egress and PUT/version bytes for
every store at once with no data-model change (the local read-through cache
keeps DECODED bytes, so nothing above the backend changes).

THE ONE-SEAM CONTRACT (why the codec is its own module rather than a few
lines in the backend): several callers read or write governed objects with a
raw boto3 client instead of the backend (the sync sweep, the persistence
read-back in the aspirations endpoint, worker_stall, tree audits ...). Every
one of them must decode exactly the way the backend decodes, or a reader
left raw parses gzip as text the day the writer flips. So they import
`decode_response` / `decode` / `head_plain_md5` from HERE, and the backend does
too — one implementation, no second copy. (Inventory of the routed raw readers
as of the first landing: owncloud_backend `_refresh` / `read_authoritative_bytes`
/ `_get_remote_raw`; mind_api aspirations_write `_authoritative_goal_lookup`;
worker_stall `read_claims` + the body-heartbeat scan; cold-snapshot-tick
`_read_marker`; tree-body-presence-audit; world/scripts/tree-twin-retire.
world/scripts/s3-fallback.py is deliberately NOT routed — it mirrors
`aws s3 cp` byte-for-byte against the email-inbox bucket, not a governed store.)

DECODE IS MAGIC-BYTE AUTHORITATIVE AND ALWAYS ON. A reader accepts BOTH plain
and gzip objects, forever: peers on older code and boxes that have not pulled
this change may still write plain over a gz key (a plain PutObject REPLACES
the object's metadata, so the ContentEncoding claim disappears with it). The
gzip magic (1f 8b) is the truth; `ContentEncoding` / metadata only corroborate.
A body whose headers CLAIM gzip but that carries no magic is a corrupt or
partial object and raises `CodecError` — loudly — rather than being handed to
a JSONL parser as garbage.

ENCODE IS FLAG-GATED, ENV-SCOPED AND ALLOWLISTED. `should_encode(rel, env_id)`
is True only when the object's deployment `env_id` (the backend's `env_id`,
e.g. "ayoai-mind") is NAMED in `OWNCLOUD_GZIP_STORES` — a comma-separated
list of environment-ids whose readers are attested gzip-ready (`*` = every
env, only ever correct once every peer deployment has attested) — AND the
env-scoped logical path (the backend's `_rel`, e.g. "world/aspirations.jsonl")
matches the hot-store allowlist. Default OFF (unset / empty).

WHY THE FLAG NAMES ENV-IDS RATHER THAN BEING A BOOLEAN. This box's code
writes to OTHER deployments' stores too: `peer-board-post.sh` pins the PEER's
backend (its env_id) and appends to the peer's `world/board/<channel>.jsonl`
— an allowlisted store — with THIS box's writer. A boolean "on" would therefore
encode a peer deployment's board the moment we flipped, and a peer box still on
the pre-codec reader would parse gzip bytes as JSONL. Scoping the flip to named
env-ids makes the cross-deployment case safe by construction: `ayoai-mind`
flips when ITS fleet has attested; `claude-mind` is added only when that
deployment's readers have. A legacy boolean value ("1", "true") names no env
and so encodes NOTHING — the fail-safe direction. This module ships
reader-first (the g-328-39 ordering — reader-capable code on every box AND
downstream before any writer flips), and the flag lives in
`.claude/settings.json` (GATE_FIRINGS_SEGMENTED precedent) so a flip is one
audited commit.

WHAT COMPRESSION CHANGES FOR THE FENCE MODEL. The S3 ETag becomes the md5 of
the COMPRESSED bytes, so it no longer equals the md5 of the plaintext the
local mirror and the sync manifest hold. Every If-Match / IfNoneMatch fence
stays correct — the ETag is opaque there. What needs the plaintext md5 is the
"is local byte-identical to S3?" family (`_overwrite_decision`,
`_sync_one`/`_pull_one` in-sync checks, the pull_sweep LIST pre-filter): the
writer therefore stores the plaintext md5 in object metadata
(`x-amz-meta-plain-md5`, visible on HEAD and GET as `Metadata["plain-md5"]`),
and comparisons prefer it when present (`content_matches`). Encoding is
DETERMINISTIC (gzip mtime=0, no filename) so identical plaintext yields
identical bytes and therefore an identical ETag on every box.
"""
from __future__ import annotations

import fnmatch
import gzip
import hashlib
import os
from typing import Optional

GZIP_MAGIC = b"\x1f\x8b"
CONTENT_ENCODING_GZIP = "gzip"
# S3 user-metadata keys. boto3 sends `Metadata={"plain-md5": ...}` and S3
# stores/returns it as the `x-amz-meta-plain-md5` header; boto3 hands it back
# LOWERCASED under response["Metadata"], so read with these exact keys.
META_PLAIN_MD5 = "plain-md5"
META_CODEC = "codec"

# Writer flag (unit 3 of g-358-11): a comma/space-separated list of
# environment-ids whose readers are attested gzip-ready ("ayoai-mind",
# "ayoai-mind,claude-mind", or "*"). Read per call, never cached at import:
# the daemon and every CLI must see a flip without a restart, exactly like
# GATE_FIRINGS_SEGMENTED. (A daemon inherits its env at spawn, so a flip in
# .claude/settings.json reaches an already-running daemon only after it is
# restarted — the cutover check must verify the daemon's start time, not
# just the file.)
FLAG_ENV = "OWNCLOUD_GZIP_STORES"
LEVEL_ENV = "OWNCLOUD_GZIP_LEVEL"
DEFAULT_LEVEL = 6
FLAG_ALL_ENVS = "*"
# Legacy boolean spellings. They name no environment, so they enable NOTHING
# (see the module docstring: a bare "on" would encode a PEER deployment's board
# through peer-board-post). Kept only so `flag_enabled` can say why a value
# that looks like a flip did not flip anything.
_LEGACY_TRUTHY = {"1", "true", "yes", "on"}

# Hot-store allowlist, matched against the backend's env-scoped logical path
# (`OwnCloudBackend._rel`, e.g. "world/aspirations.jsonl"). Deliberately a
# short explicit list of the measured egress/churn leaders, not "*.jsonl":
# graveyard/, .history and small YAML stay plain until measured, and a key
# NOT on this list is written exactly as before. Segment and sidecar shapes of
# the segmented stores are included so a future writer flip on those stores
# does not need a second edit here.
#
# `world/board/*.jsonl` (8.6% of GET egress) is DELIBERATELY ABSENT for now.
# The board is the one store that boxes of OTHER deployments write INTO
# (peer-board-post: 139 inbound posts since 2026-06-02), and they do it with
# THEIR checkout's backend — a whole-object RMW that reads the current board
# through their `_refresh`. A peer box that predates the codec would pull the
# gzip bytes raw, parse nothing, and its merge/append would then re-PUT a
# board holding only its own record (merge_append_only_jsonl drops unparseable
# lines LOUDLY but still drops them). Env-scoping the flag protects the
# outbound direction (we never encode a peer's store); this exclusion protects
# the inbound one until Claude-Mind and ZDS-Mind carry the reader. Add the
# pattern back in the SAME change that records those two attestations.
DEFAULT_ALLOWLIST = (
    "world/aspirations.jsonl",
    "world/reasoning-bank.jsonl",
    "world/reasoning-bank-*.jsonl",      # date segments + counters sidecar (g-358-05)
    "world/guardrails.jsonl",
    "world/guardrails-*.jsonl",
    "world/pipeline.jsonl",
    "meta/gate-firings.jsonl",
    "meta/gate-firings-*.jsonl",         # date segments (GATE_FIRINGS_SEGMENTED)
)
# The board pattern, kept as a named constant so the follow-up that re-admits
# it (after downstream reader attestation) is a one-token change here and a
# one-line change in the allowlist tuple — and so a test can pin that it is
# currently NOT admitted.
BOARD_PATTERN_DEFERRED = "world/board/*.jsonl"


class CodecError(ValueError):
    """The object claims an encoding its bytes do not carry (corrupt / partial
    object). Raised loudly so a garbage body never reaches a store parser."""


def flag_envs(env: Optional[dict] = None) -> frozenset:
    """The environment-ids the writer flag names (lower-cased), or the
    singleton {"*"} for every env. A legacy boolean spelling ("1", "true",
    "yes", "on") names no env and yields the EMPTY set — the fail-safe
    reading of a flip that forgot to say which deployment it is for."""
    e = os.environ if env is None else env
    raw = (e.get(FLAG_ENV, "") or "").replace(",", " ").split()
    ids = {t.strip().lower() for t in raw if t.strip()}
    ids -= _LEGACY_TRUTHY
    if FLAG_ALL_ENVS in ids:
        return frozenset({FLAG_ALL_ENVS})
    return frozenset(ids)


def flag_enabled(env: Optional[dict] = None) -> bool:
    """Is the writer flag set to something that can encode ANY env?"""
    return bool(flag_envs(env))


def env_enabled(env_id: Optional[str], env: Optional[dict] = None) -> bool:
    """Is THIS deployment's env_id named by the writer flag (or is it `*`)?"""
    ids = flag_envs(env)
    if not ids:
        return False
    if FLAG_ALL_ENVS in ids:
        return True
    return bool(env_id) and env_id.strip().lower() in ids


def gzip_level(env: Optional[dict] = None) -> int:
    e = os.environ if env is None else env
    raw = (e.get(LEVEL_ENV, "") or "").strip()
    try:
        lvl = int(raw) if raw else DEFAULT_LEVEL
    except ValueError:
        lvl = DEFAULT_LEVEL
    return min(9, max(1, lvl))


def _path_glob_match(rel: str, pat: str) -> bool:
    """Segment-wise glob: `*` never crosses a `/` (plain fnmatch's `*` DOES,
    so `world/board/*.jsonl` would also match `world/board/sub/x.jsonl`).
    An allowlist entry names exactly one directory depth, on purpose — the
    list is meant to be read as an explicit set of files, not a subtree."""
    rs, ps = rel.split("/"), pat.split("/")
    if len(rs) != len(ps):
        return False
    return all(fnmatch.fnmatchcase(r, p) for r, p in zip(rs, ps))


def rel_allowlisted(rel: str, allowlist=DEFAULT_ALLOWLIST) -> bool:
    """Does the env-scoped logical path match the hot-store allowlist?
    Backslashes are normalized so a Windows-built rel matches the same rule."""
    r = (rel or "").replace("\\", "/")
    return bool(r) and any(_path_glob_match(r, pat) for pat in allowlist)


def should_encode(rel: str, env_id: Optional[str], env: Optional[dict] = None,
                  allowlist=DEFAULT_ALLOWLIST) -> bool:
    """Writer gate: the object's deployment `env_id` is named by the flag AND
    `rel` is on the hot-store allowlist. Never true by default (flag unset),
    and never true for an env the flag does not name — which is what keeps a
    cross-deployment write (peer-board-post) plain until THAT deployment's
    readers have attested."""
    return env_enabled(env_id, env) and rel_allowlisted(rel, allowlist)


def is_gzip(body: bytes) -> bool:
    return bool(body) and body[:2] == GZIP_MAGIC


def plain_md5(body: bytes) -> str:
    return hashlib.md5(body).hexdigest()


def encode(body: bytes, level: Optional[int] = None) -> bytes:
    """Deterministic gzip: mtime=0 and no embedded filename, so identical
    plaintext -> identical bytes -> identical ETag on every box."""
    lvl = gzip_level() if level is None else min(9, max(1, int(level)))
    return gzip.compress(body, compresslevel=lvl, mtime=0)


def decode(body: bytes, *, content_encoding: Optional[str] = None,
           metadata: Optional[dict] = None, key: str = "") -> bytes:
    """Return the plaintext for a stored body.

    - gzip magic present -> gunzip (regardless of headers; headers may be
      absent on an object a foreign writer copied without metadata).
    - no magic and no gzip claim -> the body is plain; return as-is.
    - no magic but headers CLAIM gzip -> CodecError (corrupt/partial object).
    """
    if is_gzip(body):
        return gzip.decompress(body)
    claims = (content_encoding or "").strip().lower() == CONTENT_ENCODING_GZIP
    if not claims and metadata:
        claims = (metadata.get(META_CODEC) or "").strip().lower() == CONTENT_ENCODING_GZIP
    if claims:
        raise CodecError(
            f"object {key or '<unknown key>'} claims Content-Encoding gzip but "
            f"carries no gzip magic ({len(body)} bytes) — corrupt or partial "
            "object; refusing to hand it to a store parser")
    return body


def decode_response(resp: dict, key: str = "") -> bytes:
    """Decode a boto3 get_object response in one call (reads the Body)."""
    body = resp["Body"].read()
    return decode(body, content_encoding=resp.get("ContentEncoding"),
                  metadata=resp.get("Metadata"), key=key)


def put_kwargs(plain: bytes, level: Optional[int] = None) -> dict:
    """The extra put_object kwargs for an ENCODED write of `plain`:
    Body (gzip bytes), ContentEncoding, and the plaintext-md5 metadata that
    keeps the mirror/manifest comparisons exact. Callers merge this over
    their own Bucket/Key/IfMatch kwargs."""
    return {
        "Body": encode(plain, level),
        "ContentEncoding": CONTENT_ENCODING_GZIP,
        "Metadata": {META_PLAIN_MD5: plain_md5(plain),
                     META_CODEC: CONTENT_ENCODING_GZIP},
    }


def head_plain_md5(head: Optional[dict]) -> Optional[str]:
    """Plaintext md5 recorded by an encoded write, from a head_object /
    get_object response; None for a plain object (or no metadata)."""
    if not head:
        return None
    md = head.get("Metadata") or {}
    v = md.get(META_PLAIN_MD5)
    return v.strip().lower() if isinstance(v, str) and v.strip() else None


def content_matches(etag: Optional[str], plain_md5_meta: Optional[str],
                    local_md5: str) -> bool:
    """Is the S3 object byte-identical (as plaintext) to local content with
    md5 `local_md5`? Prefers the writer-recorded plaintext md5 (present only
    on encoded objects); falls back to the classic single-part ETag == md5
    rule for plain objects. A multipart ETag ('<hex>-N') never matches."""
    if plain_md5_meta:
        return plain_md5_meta == local_md5
    e = (etag or "").strip('"')
    if not e or "-" in e:
        return False
    return e == local_md5
