"""Pins for ``owncloud-store-enumerate.py diff --deep`` (, 2026-09-11).

The deep compare exists because a multipart ETag is not a content checksum
(guard-6371), so the only cross-store evidence for such an object is a content
md5 read from EACH store. The defect these tests pin: the first version built
ONE backend and read both sides through it, so with the same bucket name on
both stores -- which this deployment's basement store deliberately reuses --
every deep compare read an object against itself and reported VERIFIED
(guard-4592). Caught 2026-09-11 before the first verification run.

The fakes stand in for ``_build_backend``; which store a fake represents is
decided by the STORAGE_S3_ENDPOINT_URL in force at construction time, exactly
the mechanism the tool uses to target a side.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPT_DIR / "owncloud-store-enumerate.py"
DEST_URL = "http://dest.test:9000"


@pytest.fixture(scope="module")
def mod():
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    spec = importlib.util.spec_from_file_location("_ose_under_test", str(SCRIPT))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class _FakeS3:
    def __init__(self, objects: dict[tuple[str, str], bytes], etags: dict[str, str] | None = None):
        self.objects = objects
        self.etags = etags or {}          # key -> ETag the GET serves (default: the manifest's)
        self.reads: list[tuple[str, str]] = []

    def get_object(self, Bucket, Key):  # noqa: N803 -  signature
        self.reads.append((Bucket, Key))
        out = {"Body": io.BytesIO(self.objects[(Bucket, Key)])}
        if Key in self.etags:
            out["ETag"] = '"' + self.etags[Key] + '"'
        return out


class _FakeBackend:
    def __init__(self, s3):
        self.s3 = s3


def _md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def _manifest(path: Path, endpoint: str, rows: list[dict], bucket="b") -> str:
    lines = [json.dumps({"_summary": {"bucket": bucket, "endpoint": endpoint,
                                      "prefix": "p/", "bytes": sum(r["size"] for r in rows)}})]
    lines += [json.dumps(r) for r in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _rows(big_etag: str, small: bytes) -> list[dict]:
    return [
        {"key": "p/big", "rel": "big", "size": 10, "etag": big_etag, "multipart": True},
        {"key": "p/small", "rel": "small", "size": len(small), "etag": _md5(small),
         "multipart": False},
    ]


def _wire(monkeypatch, mod, src_s3, dst_s3):
    """Route _build_backend to the fake for whichever endpoint is in force."""
    built = []

    def fake_build(bucket, region):
        ep = os.environ.get("STORAGE_S3_ENDPOINT_URL")
        built.append(ep)
        return _FakeBackend(dst_s3 if ep == DEST_URL else src_s3)

    monkeypatch.setattr(mod, "_build_backend", fake_build)
    return built


def _run(monkeypatch, capsys, mod, argv: list[str]) -> tuple[int, dict, str]:
    monkeypatch.setattr(sys, "argv", ["owncloud-store-enumerate.py", *argv])
    monkeypatch.delenv("STORAGE_S3_ENDPOINT_URL", raising=False)
    rc = mod.main()
    out, err = capsys.readouterr()
    return rc, (json.loads(out) if out.strip() else {}), err


def test_deep_reads_each_side_through_its_own_store_and_detects_a_mismatch(
        tmp_path, monkeypatch, capsys, mod):
    small = b"hello"
    src_s3 = _FakeS3({("b", "p/big"): b"A" * 10, ("b", "p/small"): small})
    dst_s3 = _FakeS3({("b", "p/big"): b"B" * 10, ("b", "p/small"): small})
    built = _wire(monkeypatch, mod, src_s3, dst_s3)
    s = _manifest(tmp_path / "src.jsonl", "(incumbent)", _rows("aaaa-2", small))
    d = _manifest(tmp_path / "dst.jsonl", DEST_URL, _rows("bbbb-3", small))

    rc, rep, _ = _run(monkeypatch, capsys, mod,
                      ["diff", "--source", s, "--dest", d, "--deep", "--json"])

    assert rc == 1 and rep["verdict"] == "NOT VERIFIED"
    assert rep["deep"]["content_md5_mismatch"] == 1
    assert rep["deep"]["content_md5_mismatch_rels"] == ["big"]
    assert rep["deep"]["content_md5_match"] == 0
    assert rep["deep"]["read_through"] == {"source": "(incumbent)", "dest": DEST_URL}
    # One backend per side, each constructed under ITS endpoint ...
    assert sorted(built, key=str) == [None, DEST_URL]
    # ... and the big object was read once from EACH store, never twice from one.
    assert src_s3.reads == [("b", "p/big")]
    assert dst_s3.reads == [("b", "p/big")]
    # STORAGE_S3_ENDPOINT_URL restored (absent) after the per-side constructions.
    assert "STORAGE_S3_ENDPOINT_URL" not in os.environ


def test_deep_verified_when_both_stores_hold_the_same_bytes(
        tmp_path, monkeypatch, capsys, mod):
    small = b"hello"
    src_s3 = _FakeS3({("b", "p/big"): b"A" * 10, ("b", "p/small"): small})
    dst_s3 = _FakeS3({("b", "p/big"): b"A" * 10, ("b", "p/small"): small})
    _wire(monkeypatch, mod, src_s3, dst_s3)
    s = _manifest(tmp_path / "src.jsonl", "(incumbent)", _rows("aaaa-2", small))
    d = _manifest(tmp_path / "dst.jsonl", DEST_URL, _rows("bbbb-3", small))

    rc, rep, _ = _run(monkeypatch, capsys, mod,
                      ["diff", "--source", s, "--dest", d, "--deep", "--json"])

    assert rc == 0 and rep["verdict"] == "VERIFIED"
    assert rep["counts"]["checksum_verified"] == 1          # small, by ETag
    assert rep["counts"]["checksum_unverifiable_multipart"] == 1
    assert rep["deep"] == {**rep["deep"], "content_md5_match": 1,
                           "content_md5_mismatch": 0, "objects_read": 1,
                           "inherited_from_baseline": 0, "errors": []}


def test_deep_refuses_to_read_both_sides_through_one_endpoint(
        tmp_path, monkeypatch, capsys, mod):
    small = b"hello"
    src_s3 = _FakeS3({("b", "p/big"): b"A" * 10, ("b", "p/small"): small})
    _wire(monkeypatch, mod, src_s3, src_s3)
    # Both manifests claim the incumbent endpoint and the same bucket: a deep
    # read would compare each object with itself (guard-4592).
    s = _manifest(tmp_path / "src.jsonl", "(incumbent)", _rows("aaaa-2", small))
    d = _manifest(tmp_path / "dst.jsonl", "(incumbent)", _rows("bbbb-3", small))

    rc, rep, err = _run(monkeypatch, capsys, mod,
                        ["diff", "--source", s, "--dest", d, "--deep", "--json"])

    assert rc == 2 and rep == {}
    assert "same endpoint" in err and "compares each object with itself" in err
    assert src_s3.reads == []


def test_deep_endpoint_flag_may_confirm_but_not_contradict_the_manifest(
        tmp_path, monkeypatch, capsys, mod):
    small = b"hello"
    src_s3 = _FakeS3({("b", "p/big"): b"A" * 10, ("b", "p/small"): small})
    dst_s3 = _FakeS3({("b", "p/big"): b"A" * 10, ("b", "p/small"): small})
    _wire(monkeypatch, mod, src_s3, dst_s3)
    s = _manifest(tmp_path / "src.jsonl", "(incumbent)", _rows("aaaa-2", small))
    d = _manifest(tmp_path / "dst.jsonl", DEST_URL, _rows("bbbb-3", small))

    rc, rep, _ = _run(monkeypatch, capsys, mod,
                      ["diff", "--source", s, "--dest", d, "--deep", "--json",
                       "--dest-endpoint", DEST_URL])
    assert rc == 0 and rep["verdict"] == "VERIFIED"

    rc, rep, err = _run(monkeypatch, capsys, mod,
                        ["diff", "--source", s, "--dest", d, "--deep", "--json",
                         "--dest-endpoint", "http://other.test:9000"])
    assert rc == 2 and rep == {} and "contradicts the dest manifest" in err


def test_deep_baseline_pair_skips_objects_unchanged_on_both_sides(
        tmp_path, monkeypatch, capsys, mod):
    small = b"hello"
    src_s3 = _FakeS3({("b", "p/big"): b"A" * 10, ("b", "p/small"): small})
    dst_s3 = _FakeS3({("b", "p/big"): b"A" * 10, ("b", "p/small"): small})
    _wire(monkeypatch, mod, src_s3, dst_s3)
    s = _manifest(tmp_path / "src.jsonl", "(incumbent)", _rows("aaaa-2", small))
    d = _manifest(tmp_path / "dst.jsonl", DEST_URL, _rows("bbbb-3", small))
    # The baseline is an earlier deep run: its manifest pair plus its report, which
    # names the rels it content-verified.
    bs = _manifest(tmp_path / "base-src.jsonl", "(incumbent)", _rows("aaaa-2", small))
    bd = _manifest(tmp_path / "base-dst.jsonl", DEST_URL, _rows("bbbb-3", small))
    rc, base_rep, _ = _run(monkeypatch, capsys, mod,
                           ["diff", "--source", bs, "--dest", bd, "--deep", "--json"])
    assert rc == 0 and base_rep["deep"]["content_md5_match_rels"] == ["big"]
    br = tmp_path / "base-report.json"
    br.write_text(json.dumps(base_rep), encoding="utf-8")
    src_s3.reads.clear(); dst_s3.reads.clear()

    rc, rep, _ = _run(monkeypatch, capsys, mod,
                      ["diff", "--source", s, "--dest", d, "--deep", "--json",
                       "--baseline-source", bs, "--baseline-dest", bd,
                       "--baseline-report", str(br)])
    assert rc == 0 and rep["verdict"] == "VERIFIED"
    assert rep["deep"]["inherited_from_baseline"] == 1
    assert rep["deep"]["objects_read"] == 0
    assert rep["deep"]["content_md5_match_rels"] == ["big"]
    assert src_s3.reads == [] and dst_s3.reads == []

    # A changed dest ETag (re-uploaded object) is NOT inherited: it is re-read.
    d2 = _manifest(tmp_path / "dst2.jsonl", DEST_URL, _rows("cccc-3", small))
    rc, rep, _ = _run(monkeypatch, capsys, mod,
                      ["diff", "--source", s, "--dest", d2, "--deep", "--json",
                       "--baseline-source", bs, "--baseline-dest", bd,
                       "--baseline-report", str(br)])
    assert rc == 0 and rep["deep"]["inherited_from_baseline"] == 0
    assert rep["deep"]["objects_read"] == 1

    # A rel the baseline run did NOT content-verify (it MISMATCHED there) is
    # re-read however unchanged both sides are -- a bad copy never inherits.
    bad_rep = dict(base_rep); bad_rep["deep"] = {**base_rep["deep"], "content_md5_match_rels": [],
                                                 "content_md5_mismatch_rels": ["big"]}
    br2 = tmp_path / "base-report-bad.json"; br2.write_text(json.dumps(bad_rep), encoding="utf-8")
    src_s3.reads.clear(); dst_s3.reads.clear()
    rc, rep, _ = _run(monkeypatch, capsys, mod,
                      ["diff", "--source", s, "--dest", d, "--deep", "--json",
                       "--baseline-source", bs, "--baseline-dest", bd,
                       "--baseline-report", str(br2)])
    assert rc == 0 and rep["deep"]["inherited_from_baseline"] == 0
    assert rep["deep"]["objects_read"] == 1 and src_s3.reads == [("b", "p/big")]

    # A partial baseline is refused.
    rc, rep, err = _run(monkeypatch, capsys, mod,
                        ["diff", "--source", s, "--dest", d, "--deep", "--json",
                         "--baseline-source", bs, "--baseline-dest", bd])
    assert rc == 2 and "go together" in err


def test_deep_source_that_changed_since_enumeration_is_churn_not_mismatch(
        tmp_path, monkeypatch, capsys, mod):
    """The GET serves a different ETag from the manifest row: the object moved between
    the snapshot and the read. Not verified (no VERIFIED verdict), but named as
    changed_during_read rather than a content mismatch."""
    small = b"hello"
    src_s3 = _FakeS3({("b", "p/big"): b"A" * 10, ("b", "p/small"): small}, etags={"p/big": "ffff-4"})
    dst_s3 = _FakeS3({("b", "p/big"): b"B" * 10, ("b", "p/small"): small}, etags={"p/big": "bbbb-3"})
    _wire(monkeypatch, mod, src_s3, dst_s3)
    s = _manifest(tmp_path / "src.jsonl", "(incumbent)", _rows("aaaa-2", small))
    d = _manifest(tmp_path / "dst.jsonl", DEST_URL, _rows("bbbb-3", small))

    rc, rep, _ = _run(monkeypatch, capsys, mod,
                      ["diff", "--source", s, "--dest", d, "--deep", "--json"])
    assert rc == 1 and rep["verdict"] == "NOT VERIFIED"
    assert rep["deep"]["content_md5_mismatch"] == 0
    assert rep["deep"]["changed_during_read"] == 1
    assert rep["deep"]["changed_during_read_rels"] == ["big"]


def test_deep_read_error_is_reported_and_blocks_the_verdict(
        tmp_path, monkeypatch, capsys, mod):
    small = b"hello"
    src_s3 = _FakeS3({("b", "p/big"): b"A" * 10, ("b", "p/small"): small})
    dst_s3 = _FakeS3({("b", "p/small"): small})          # big is missing at dest
    _wire(monkeypatch, mod, src_s3, dst_s3)
    s = _manifest(tmp_path / "src.jsonl", "(incumbent)", _rows("aaaa-2", small))
    d = _manifest(tmp_path / "dst.jsonl", DEST_URL, _rows("bbbb-3", small))

    rc, rep, _ = _run(monkeypatch, capsys, mod,
                      ["diff", "--source", s, "--dest", d, "--deep", "--json"])
    assert rc == 1 and rep["verdict"] == "NOT VERIFIED"
    assert rep["deep"]["errors"][0]["rel"] == "big"
    assert "KeyError" in rep["deep"]["errors"][0]["error"]


# ---------------------------------------------------------------------------
# copy: the resume/skip rule
# ---------------------------------------------------------------------------
import datetime as _dt

_T0 = _dt.datetime(2026, 9, 11, 2, 0, tzinfo=_dt.timezone.utc)


class _NotFound(Exception):
    response = {"Error": {"Code": "404"}}


class _FakeSrcS3:
    def __init__(self, listing: list[dict], bodies: dict[str, bytes]):
        self.listing, self.bodies = listing, bodies

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        listing = self.listing

        class _P:
            def paginate(self, Bucket, Prefix):  # noqa: N803
                yield {"Contents": [o for o in listing if o["Key"].startswith(Prefix)]}
        return _P()

    def get_object(self, Bucket, Key):  # noqa: N803
        return {"Body": io.BytesIO(self.bodies[Key])}


class _FakeDstS3:
    def __init__(self, heads: dict[str, dict]):
        self.heads, self.uploads = heads, []

    def head_object(self, Bucket, Key):  # noqa: N803
        if Key not in self.heads:
            raise _NotFound()
        return self.heads[Key]

    def upload_fileobj(self, body, bucket, key):
        self.uploads.append(key)
        body.read()


def test_copy_skip_rule_recopies_a_same_size_multipart_rewrite(tmp_path, monkeypatch, capsys, mod):
    minute = _dt.timedelta(minutes=1)
    listing = [
        # single-part, unchanged: ETag decides, timestamps irrelevant
        {"Key": "p/small", "Size": 5, "ETag": '"' + _md5(b"hello") + '"', "LastModified": _T0 + 9 * minute},
        # multipart, same size, source rewritten AFTER the destination copy -> re-copy
        {"Key": "p/rewritten", "Size": 10, "ETag": '"aaaa-2"', "LastModified": _T0 + 5 * minute},
        # multipart, same size, destination copy newer than the source version -> skip
        {"Key": "p/stable", "Size": 10, "ETag": '"cccc-2"', "LastModified": _T0},
        # absent at the destination -> copy
        {"Key": "p/new", "Size": 3, "ETag": '"' + _md5(b"abc") + '"', "LastModified": _T0},
    ]
    src = _FakeSrcS3(listing, {"p/small": b"hello", "p/rewritten": b"B" * 10, "p/stable": b"A" * 10, "p/new": b"abc"})
    dst = _FakeDstS3({
        "p/small": {"ContentLength": 5, "ETag": '"' + _md5(b"hello") + '"', "LastModified": _T0 + 2 * minute},
        "p/rewritten": {"ContentLength": 10, "ETag": '"bbbb-3"', "LastModified": _T0 + 2 * minute},
        "p/stable": {"ContentLength": 10, "ETag": '"dddd-3"', "LastModified": _T0 + 2 * minute},
    })

    def fake_build(bucket, region):
        return _FakeBackend(dst if os.environ.get("STORAGE_S3_ENDPOINT_URL") == DEST_URL else src)
    monkeypatch.setattr(mod, "_build_backend", fake_build)
    monkeypatch.setattr(sys, "argv", ["owncloud-store-enumerate.py", "copy", "--bucket", "b",
                                      "--dest-endpoint", DEST_URL, "--prefix", "p/", "--workers", "2",
                                      "--progress-every", "0"])
    monkeypatch.delenv("STORAGE_S3_ENDPOINT_URL", raising=False)
    rc = mod.main()
    rep = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert sorted(dst.uploads) == ["p/new", "p/rewritten"]
    assert (rep["scanned"], rep["copied"], rep["skipped_already_present"], rep["failed"]) == (4, 2, 2, 0)
