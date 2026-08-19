"""Unit tests for core/scripts/_owncloud_codec.py — the own-cloud transport
codec (g-358-11) — plus the codec-aware helpers in owncloud_sync.

Pure-Python: no moto, no boto3, no daemon. The moto-mocked backend half
(decode on the read path, plain-md5 stat/compare, forced-failure control)
lives in test_owncloud_codec_backend.py so THIS half still runs on a box
without moto installed.

Coverage:
  1. encode/decode round-trip; encoding is DETERMINISTIC (identical plaintext
     -> identical bytes -> identical ETag on every box)
  2. decode is MAGIC-BYTE AUTHORITATIVE: gzip bytes with no headers decode;
     plain bytes with no claim pass through; plain bytes under a gzip CLAIM
     (ContentEncoding or metadata codec) raise CodecError — never garbage out
  3. put_kwargs shape: gzip Body + ContentEncoding + plain-md5/codec metadata
  4. content_matches: plaintext-md5 metadata wins; classic single-part ETag rule
     for plain objects; multipart never matches; quoting-insensitive
  5. should_encode: default OFF even for an allowlisted rel; flag+allowlist
     required together; allowlist patterns; backslash normalization
  6. owncloud_sync._content_matches / _manifest_etag / _etag_equal
"""
from __future__ import annotations

import gzip
import hashlib
import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_DIR = PROJECT_ROOT / "core" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import _owncloud_codec as codec  # noqa: E402

PLAIN = b'{"id": "asp-001", "title": "x"}\n' * 200
PLAIN_MD5 = hashlib.md5(PLAIN).hexdigest()


# --- 1. round-trip + determinism -------------------------------------------
def test_encode_decode_round_trip():
    gz = codec.encode(PLAIN)
    assert codec.is_gzip(gz)
    assert gz != PLAIN
    assert len(gz) < len(PLAIN)  # this payload is highly compressible
    assert codec.decode(gz) == PLAIN


def test_encode_is_deterministic():
    """Same plaintext -> same bytes twice (mtime=0, no filename), so the S3
    ETag of an encoded object is reproducible across boxes."""
    assert codec.encode(PLAIN) == codec.encode(PLAIN)
    assert hashlib.md5(codec.encode(PLAIN)).hexdigest() == \
        hashlib.md5(codec.encode(PLAIN)).hexdigest()
    # Explicit level is honored and bounded.
    assert codec.decode(codec.encode(PLAIN, level=1)) == PLAIN
    assert codec.decode(codec.encode(PLAIN, level=99)) == PLAIN  # clamped to 9


def test_stdlib_gzip_default_is_not_deterministic_but_ours_is():
    """Positive control for the determinism claim: stdlib gzip.compress with
    the default mtime embeds the wall clock, so two calls CAN differ; ours
    pins mtime=0. (Compares only when the stdlib call actually varied.)"""
    a = gzip.compress(PLAIN, mtime=1)
    b = gzip.compress(PLAIN, mtime=2)
    assert a != b  # header differs by mtime -> proves mtime lands in the bytes
    assert codec.encode(PLAIN) == codec.encode(PLAIN)


# --- 2. decode is magic-byte authoritative ---------------------------------
def test_decode_plain_no_claim_passes_through():
    assert codec.decode(PLAIN) is PLAIN or codec.decode(PLAIN) == PLAIN
    assert codec.decode(b"") == b""


def test_decode_gzip_without_headers_still_decodes():
    """A foreign copy (server-side copy without metadata, or a peer that
    re-PUT the bytes raw) strips ContentEncoding; the magic is the truth."""
    gz = codec.encode(PLAIN)
    assert codec.decode(gz, content_encoding=None, metadata=None) == PLAIN
    assert codec.decode(gz, content_encoding="", metadata={}) == PLAIN


def test_decode_gzip_with_headers_decodes():
    gz = codec.encode(PLAIN)
    assert codec.decode(gz, content_encoding="gzip",
                        metadata={"plain-md5": PLAIN_MD5, "codec": "gzip"}) == PLAIN
    # Header casing tolerated.
    assert codec.decode(gz, content_encoding="GZIP") == PLAIN


def test_decode_claims_gzip_without_magic_raises_loudly():
    """Plain bytes under a gzip CLAIM = corrupt/partial object. Must raise,
    never hand the bytes to a JSONL parser as if they were the plaintext."""
    with pytest.raises(codec.CodecError):
        codec.decode(PLAIN, content_encoding="gzip", key="k")
    with pytest.raises(codec.CodecError):
        codec.decode(PLAIN, metadata={"codec": "gzip"}, key="k")
    # The message names the key so the operator can find the object.
    with pytest.raises(codec.CodecError, match="world/aspirations.jsonl"):
        codec.decode(PLAIN, content_encoding="gzip",
                     key="ayoai-mind/world/aspirations.jsonl")


def test_decode_response_reads_body_and_headers():
    gz = codec.encode(PLAIN)
    resp = {"Body": io.BytesIO(gz), "ContentEncoding": "gzip",
            "Metadata": {"plain-md5": PLAIN_MD5, "codec": "gzip"},
            "ETag": '"abc"'}
    assert codec.decode_response(resp, key="k") == PLAIN
    # Plain response, no headers.
    assert codec.decode_response({"Body": io.BytesIO(PLAIN)}) == PLAIN
    # Claim without magic surfaces through decode_response too.
    with pytest.raises(codec.CodecError):
        codec.decode_response({"Body": io.BytesIO(PLAIN),
                               "ContentEncoding": "gzip"}, key="k")


# --- 3. put_kwargs shape ---------------------------------------------------
def test_put_kwargs_shape():
    kw = codec.put_kwargs(PLAIN)
    assert set(kw) == {"Body", "ContentEncoding", "Metadata"}
    assert codec.is_gzip(kw["Body"]) and codec.decode(kw["Body"]) == PLAIN
    assert kw["ContentEncoding"] == "gzip"
    assert kw["Metadata"] == {"plain-md5": PLAIN_MD5, "codec": "gzip"}
    # Never carries Bucket/Key/IfMatch — the caller owns the fence kwargs.
    assert "IfMatch" not in kw and "Key" not in kw


def test_head_plain_md5():
    assert codec.head_plain_md5(None) is None
    assert codec.head_plain_md5({}) is None
    assert codec.head_plain_md5({"Metadata": {}}) is None
    assert codec.head_plain_md5({"Metadata": {"plain-md5": " ABC "}}) == "abc"
    assert codec.head_plain_md5({"Metadata": {"plain-md5": ""}}) is None


# --- 4. content_matches ----------------------------------------------------
def test_content_matches_prefers_plain_md5_metadata():
    gz_etag = '"' + hashlib.md5(codec.encode(PLAIN)).hexdigest() + '"'
    assert gz_etag.strip('"') != PLAIN_MD5  # the whole reason this exists
    # Encoded object: ETag != plaintext md5, metadata says it IS the plaintext.
    assert codec.content_matches(gz_etag, PLAIN_MD5, PLAIN_MD5) is True
    assert codec.content_matches(gz_etag, PLAIN_MD5, "0" * 32) is False
    # Metadata wins even when the ETag would have matched by accident.
    assert codec.content_matches('"' + PLAIN_MD5 + '"', "f" * 32, PLAIN_MD5) is False


def test_content_matches_plain_object_etag_rule():
    assert codec.content_matches('"' + PLAIN_MD5 + '"', None, PLAIN_MD5) is True
    assert codec.content_matches(PLAIN_MD5, None, PLAIN_MD5) is True  # unquoted
    assert codec.content_matches('"' + PLAIN_MD5 + '"', None, "0" * 32) is False
    assert codec.content_matches(None, None, PLAIN_MD5) is False
    assert codec.content_matches("", None, PLAIN_MD5) is False


def test_content_matches_multipart_never_matches():
    assert codec.content_matches('"deadbeef-3"', None, "deadbeef-3") is False
    assert codec.content_matches('"deadbeef-3"', None, "deadbeef") is False
    # ...unless the plaintext md5 metadata is present, which is authoritative.
    assert codec.content_matches('"deadbeef-3"', PLAIN_MD5, PLAIN_MD5) is True


# --- 5. writer gate --------------------------------------------------------
def test_should_encode_default_off_even_for_allowlisted_rel():
    assert codec.flag_enabled({}) is False
    assert codec.flag_envs({}) == frozenset()
    assert codec.should_encode("world/aspirations.jsonl", "ayoai-mind", env={}) is False


@pytest.mark.parametrize("v", ["1", "true", "TRUE", "yes", "on", " on ", "1,true"])
def test_legacy_boolean_flag_names_no_env_and_enables_nothing(v):
    """A bare boolean would encode a PEER deployment's board through
    peer-board-post; it names no env, so it is the empty set — fail-safe."""
    e = {"OWNCLOUD_GZIP_STORES": v}
    assert codec.flag_envs(e) == frozenset()
    assert codec.flag_enabled(e) is False
    assert codec.env_enabled("ayoai-mind", e) is False
    assert codec.should_encode("world/aspirations.jsonl", "ayoai-mind", env=e) is False


@pytest.mark.parametrize("v", ["", "0", "false", "off", "no"])
def test_flag_falsy_variants(v):
    """Off spellings are not env-ids either — but note '0'/'false'/'off'/'no'
    are NOT stripped as legacy tokens: they name no real deployment, so they
    only ever match a deployment literally called 'false'. Not a concern in
    practice; pinned so the behavior is at least deliberate."""
    e = {"OWNCLOUD_GZIP_STORES": v}
    assert codec.env_enabled("ayoai-mind", e) is False
    assert codec.should_encode("world/aspirations.jsonl", "ayoai-mind", env=e) is False


def test_flag_names_env_ids_and_scopes_the_encode():
    e = {"OWNCLOUD_GZIP_STORES": "ayoai-mind"}
    assert codec.flag_envs(e) == frozenset({"ayoai-mind"})
    assert codec.flag_enabled(e) is True
    assert codec.env_enabled("ayoai-mind", e) is True
    assert codec.env_enabled("Ayoai-Mind", e) is True          # case-insensitive
    assert codec.env_enabled("claude-mind", e) is False        # peer NOT named
    assert codec.env_enabled(None, e) is False
    assert codec.env_enabled("", e) is False
    # The cross-deployment hazard, spelled out: same rel, peer env -> plain.
    assert codec.should_encode("world/aspirations.jsonl", "ayoai-mind", env=e) is True
    assert codec.should_encode("world/aspirations.jsonl", "claude-mind", env=e) is False
    # Multiple envs, comma or space separated, legacy tokens ignored.
    e2 = {"OWNCLOUD_GZIP_STORES": "ayoai-mind, claude-mind 1"}
    assert codec.flag_envs(e2) == frozenset({"ayoai-mind", "claude-mind"})
    assert codec.env_enabled("claude-mind", e2) is True
    assert codec.env_enabled("zds-mind", e2) is False
    # Star = every env (only correct once every peer has attested).
    e3 = {"OWNCLOUD_GZIP_STORES": "ayoai-mind,*"}
    assert codec.flag_envs(e3) == frozenset({"*"})
    assert codec.env_enabled("zds-mind", e3) is True


def test_should_encode_requires_env_and_allowlist_together():
    on = {"OWNCLOUD_GZIP_STORES": "ayoai-mind"}
    assert codec.should_encode("world/aspirations.jsonl", "ayoai-mind", env=on) is True
    assert codec.should_encode("world/knowledge/tree/_tree.yaml", "ayoai-mind", env=on) is False
    assert codec.should_encode("agents/alpha/experience.jsonl", "ayoai-mind", env=on) is False
    assert codec.should_encode("world/aspirations.jsonl", "ayoai-mind", env={}) is False
    assert codec.should_encode("world/aspirations.jsonl", "claude-mind", env=on) is False


@pytest.mark.parametrize("rel,expected", [
    ("world/aspirations.jsonl", True),
    ("world/reasoning-bank.jsonl", True),
    ("world/reasoning-bank-2026-08-17.jsonl", True),
    ("world/reasoning-bank-counters.jsonl", True),
    ("world/guardrails.jsonl", True),
    ("world/guardrails-2026-08.jsonl", True),
    ("world/pipeline.jsonl", True),
    ("meta/gate-firings.jsonl", True),
    ("meta/gate-firings-2026-08-17.jsonl", True),
    # NOT on the list — written exactly as before.
    ("world/aspirations-archive.jsonl", False),
    ("world/pipeline-archive.jsonl", False),
    # DEFERRED (inbound cross-deployment writers, see BOARD_PATTERN_DEFERRED):
    ("world/board/coordination.jsonl", False),
    ("world/board/general.jsonl", False),
    ("world/board/coordination-reads.jsonl", False),
    ("world/board/sub/x.jsonl", False),               # segment glob never crosses '/'
    ("world/team-state.yaml", False),
    ("world/knowledge/tree/_tree.yaml", False),
    ("meta/audit-baselines.yaml", False),
    ("agents/alpha/aspirations.jsonl", False),
    ("", False),
])
def test_rel_allowlist_patterns(rel, expected):
    assert codec.rel_allowlisted(rel) is expected


def test_rel_allowlist_normalizes_backslashes():
    assert codec.rel_allowlisted("world\\aspirations.jsonl") is True
    assert codec.rel_allowlisted("meta\\gate-firings-2026-08-17.jsonl") is True


def test_board_pattern_is_deferred_not_forgotten():
    """The board is excluded from the FIRST flip because boxes of OTHER
    deployments write into it with their own (possibly pre-codec) reader.
    Pin: the pattern exists as a named constant, matches board channels, and
    is NOT in the live allowlist — so re-admitting it is a deliberate act."""
    assert codec.BOARD_PATTERN_DEFERRED == "world/board/*.jsonl"
    assert codec.BOARD_PATTERN_DEFERRED not in codec.DEFAULT_ALLOWLIST
    assert codec.rel_allowlisted("world/board/coordination.jsonl",
                                 allowlist=(codec.BOARD_PATTERN_DEFERRED,)) is True
    assert codec.rel_allowlisted("world/board/sub/x.jsonl",
                                 allowlist=(codec.BOARD_PATTERN_DEFERRED,)) is False
    assert codec.should_encode("world/board/coordination.jsonl", "ayoai-mind",
                               env={"OWNCLOUD_GZIP_STORES": "*"}) is False


def test_gzip_level_parse_and_bounds():
    assert codec.gzip_level({}) == codec.DEFAULT_LEVEL
    assert codec.gzip_level({"OWNCLOUD_GZIP_LEVEL": "9"}) == 9
    assert codec.gzip_level({"OWNCLOUD_GZIP_LEVEL": "0"}) == 1
    assert codec.gzip_level({"OWNCLOUD_GZIP_LEVEL": "42"}) == 9
    assert codec.gzip_level({"OWNCLOUD_GZIP_LEVEL": "junk"}) == codec.DEFAULT_LEVEL


# --- 6. owncloud_sync helpers ----------------------------------------------
def test_sync_content_matches_uses_plain_md5_when_present():
    from storage_backend import FileStat
    import owncloud_sync as sync
    gz = codec.encode(PLAIN)
    st_enc = FileStat(version='"' + hashlib.md5(gz).hexdigest() + '"',
                      size=len(gz), mtime_ns=0, plain_md5=PLAIN_MD5)
    assert sync._content_matches(st_enc, PLAIN_MD5) is True
    assert sync._content_matches(st_enc, "0" * 32) is False
    st_plain = FileStat(version='"' + PLAIN_MD5 + '"', size=len(PLAIN), mtime_ns=0)
    assert st_plain.plain_md5 is None
    assert sync._content_matches(st_plain, PLAIN_MD5) is True
    assert sync._content_matches(st_plain, "0" * 32) is False


def test_sync_content_matches_tolerates_stat_without_plain_md5_attr():
    """Test fakes (test_owncloud_sync.FakeBackend et al.) build their own
    stat objects; a stat with no plain_md5 attribute keeps the ETag rule."""
    import owncloud_sync as sync
    fake = SimpleNamespace(version='"' + PLAIN_MD5 + '"', size=1)
    assert sync._content_matches(fake, PLAIN_MD5) is True
    assert sync._content_matches(SimpleNamespace(version='"x-2"'), "x") is False


def test_sync_manifest_etag_shapes():
    import owncloud_sync as sync
    assert sync._manifest_etag(None) is None
    assert sync._manifest_etag(12345) is None                       # legacy int
    assert sync._manifest_etag({"mtime": 1, "md5": "a"}) is None   # legacy dict
    assert sync._manifest_etag({"mtime": 1, "md5": "a", "etag": '"e1"'}) == "e1"
    assert sync._manifest_etag({"mtime": 1, "md5": "a", "etag": ""}) is None
    # _manifest_entry is unchanged by the new field.
    assert sync._manifest_entry({"mtime": 1, "md5": "a", "etag": "e1"}) == (1, "a")


def test_sync_etag_equal_quote_insensitive():
    import owncloud_sync as sync
    assert sync._etag_equal('"abc"', "abc") is True
    assert sync._etag_equal("abc", '"abc"') is True
    assert sync._etag_equal('"abc"', '"abd"') is False
    assert sync._etag_equal(None, "abc") is False
    assert sync._etag_equal("abc", "") is False


def test_pull_one_encoded_object_in_sync_by_plain_md5(tmp_path, monkeypatch):
    """_pull_one against a fake backend whose stat reports an ENCODED object
    (ETag = md5 of gzip bytes, plain_md5 = md5 of plaintext): a local mirror
    holding the plaintext must classify as in_sync — no pull, no clobber.
    Pre-codec, the ETag != local md5 read as 'S3 moved' and pulled every tick."""
    from storage_backend import FileStat
    import owncloud_sync as sync
    gz = codec.encode(PLAIN)

    class EncodedFake:
        def __init__(self):
            self.refreshed = []
        def stat(self, path):
            return FileStat(version='"' + hashlib.md5(gz).hexdigest() + '"',
                            size=len(gz), mtime_ns=0, plain_md5=PLAIN_MD5)
        def refresh(self, path):
            self.refreshed.append(str(path))

    full = tmp_path / "world" / "aspirations.jsonl"
    full.parent.mkdir(parents=True)
    full.write_bytes(PLAIN)
    be = EncodedFake()
    stats = {"scanned": 0, "in_sync": 0, "s3_absent": 0, "pulled": 0,
             "would_pull": 0}
    got = sync._pull_one(be, full, dry_run=False, stats=stats,
                         baseline_md5=PLAIN_MD5)
    assert got == PLAIN_MD5
    assert stats["in_sync"] == 1
    assert be.refreshed == []  # never pulled over the plaintext mirror
    assert full.read_bytes() == PLAIN


def test_pull_sweep_prefilter_answers_encoded_object_from_manifest_etag(tmp_path, monkeypatch):
    """LIST-time pre-filter for an ENCODED object (): the LIST ETag
    (md5 of the gzip bytes) can never equal the manifest's plaintext md5, so
    the answer comes from the last-seen ETag the baseline recorded.

    Harness: the sibling sync test module's FakeBackend + tree, subclassed so
    S3 presents encoded objects (ETag = md5(gzip(plaintext)), plain-md5 in
    stat) while storing/refreshing plaintext — the real backend's contract.

      tick 1: manifest entries come from the PUSH sweep (legacy {mtime, md5},
              no etag) -> pre-filter cannot answer -> _pull_one HEADs -> in_sync
              via plain_md5 -> the manifest gains the ETag.
      tick 2: pre-filter answers from the recorded ETag -> in_sync with NO
              stat/HEAD (stat is stubbed to raise: forced-failure control).
    """
    import test_owncloud_sync as tos          # sibling harness (same tests dir)
    from storage_backend import FileStat
    import owncloud_sync as sync

    class EncodedFake(tos.FakeBackend):
        """S3 holds encoded objects: the dict keeps PLAINTEXT (what a decoding
        reader sees), the ETag/size views are of the gzip bytes."""
        stat_calls = 0
        def _etag(self, b):
            return '"' + hashlib.md5(codec.encode(b)).hexdigest() + '"'
        def stat(self, path):
            EncodedFake.stat_calls += 1
            b = self.s3.get(str(path))
            if b is None:
                return None
            gz = codec.encode(b)
            return FileStat(version=self._etag(b), size=len(gz), mtime_ns=0,
                            plain_md5=hashlib.md5(b).hexdigest())
        def list_objects(self, path):
            prefix = str(path).replace("\\", "/").rstrip("/") + "/"
            out = []
            for k, b in self.s3.items():
                kk = str(k).replace("\\", "/")
                if kk.startswith(prefix):
                    out.append((kk[len(prefix):], self._etag(b), len(codec.encode(b))))
            return sorted(out)

    roots = tos._build_tree(tmp_path)
    be = EncodedFake(roots)
    tos._prime_baselines(be, monkeypatch, tmp_path)   # push sweep -> {mtime, md5}
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    board = tmp_path / "world" / "board" / "general.jsonl"
    assert board.read_bytes() == b'{"m":1}\n'
    m0 = sync._load_manifest()
    assert "etag" not in m0["world/board/general.jsonl"]   # legacy shape after push

    EncodedFake.stat_calls = 0
    s1 = sync.pull_sweep(be)
    assert s1["pulled"] == 0 and s1["errors"] == 0
    assert EncodedFake.stat_calls >= 1                    # tick 1 needed HEADs
    m1 = sync._load_manifest()
    ent = m1["world/board/general.jsonl"]
    assert ent["md5"] == hashlib.md5(b'{"m":1}\n').hexdigest()
    assert ent["etag"] == be._etag(b'{"m":1}\n').strip('"')  # gzip-bytes ETag recorded
    assert board.read_bytes() == b'{"m":1}\n'             # never clobbered with gz

    def _no_head(path):
        raise AssertionError("tick 2 must answer from the manifest ETag, not HEAD: %s" % path)
    be.stat = _no_head                                    # forced-failure control
    s2 = sync.pull_sweep(be)
    assert s2["errors"] == 0, s2
    assert s2["pulled"] == 0 and s2["in_sync"] >= 1
    assert sync._load_manifest()["world/board/general.jsonl"] == ent  # unchanged
