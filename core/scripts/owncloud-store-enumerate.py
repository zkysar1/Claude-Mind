#!/usr/bin/env python3
# domain-leak-exempt: the S3 list/ETag semantics below are the SUBJECT, not an
# incidental cloud reference. This tool exists to produce and compare integrity
# baselines for the own-cloud object tier, so naming the protocol it reads is
# the whole point.
"""Own-cloud store enumerator and manifest differ (g-372-06).

Produces the integrity baseline a store migration is verified against, and
compares a destination against it. Two modes:

    enumerate   list every object under a prefix -> JSONL manifest + summary
    diff        compare two manifests on count AND bytes AND per-object checksum

WHY THIS EXISTS. ``archive-before-delete.md`` step 1 requires the enumeration to
exist BEFORE the destructive or migrating step, and step 4 requires count, total
bytes and per-object checksums to ALL match -- "a sampled spot-check is not
verification". g-372-06 restates that as its three outcomes. Nothing in this
repo produced such a manifest; ``owncloud_sync``/``owncloud_backend`` paginate
for their own purposes but emit no durable baseline.

THE CLIENT IS PRODUCTION'S CLIENT. Like ``object-store-conformance.py``, this
does not build its own boto3 client -- it constructs an ``OwnCloudBackend`` and
uses ``backend.s3`` / ``backend.bucket``. So the endpoint override, the scoped
credentials and the retry/timeout Config are production's, and pointing the tool
at a candidate store is exactly the g-372-01 switch:

    STORAGE_S3_ENDPOINT_URL=http://<host>:9000 python3 core/scripts/owncloud-store-enumerate.py enumerate ...

With the variable unset the target is the incumbent store. That also satisfies
g-372-06's third outcome ("the copy was made through the storage backend client,
not a hand-built URI") for the VERIFICATION half.

DELIBERATELY NOT THE CLI. ``rb-6860`` measured, on two boxes and two binaries,
that a cloud-CLI measurement redirected to a file can return ZERO BYTES at rc=0,
INTERMITTENTLY -- indistinguishable from a genuinely empty listing. An
enumeration whose failure mode is "silently reports fewer objects than exist" is
the worst possible integrity baseline, because it makes the destination look
complete. An in-process paginator has no redirect and no subprocess, so that
entire failure class is absent by construction rather than by care.

A ZERO IS A REFUSAL, NOT A RESULT. ``enumerate`` exits non-zero if it finds no
objects (override with ``--allow-empty``). Per guard-2298 an empty result from an
instrument that can silently fail is zero signals, not one signal of emptiness.

=== THE ETAG COMPARABILITY TRAP (read before trusting any diff verdict) ===

A cross-store checksum diff is NOT simply "compare the ETags", and a tool that
pretended otherwise would emit confident false PASSES.

  * A SINGLE-PART object's ETag IS the content md5. Comparable across stores;
    an equal ETag is real checksum evidence. (This is the property
    ``_maybe_tail_append`` depends on -- ``owncloud_backend`` L861 accepts a tail
    append only when ``md5(local_prefix + tail)`` equals the ETag -- and the
    property ``object-store-conformance.py`` case C4a measures.)
  * A MULTIPART object's ETag is ``md5(concat of part md5s)-<N>``. It is a
    function of THE PART SIZE THE UPLOADER CHOSE, not of the content alone. Copy
    the identical bytes to another store with a different multipart threshold and
    the ETag legitimately differs. ``_etag_is_multipart`` (``owncloud_sync``
    L900) exists precisely to mark these UNCOMPARABLE.

So an ETag mismatch on a multipart object is NOT evidence of corruption, and an
ETag match on one is not evidence of integrity. This tool never conflates the
two: ``diff`` reports ``checksum_verified`` and ``checksum_unverifiable``
separately and REFUSES to call a run verified while any object sits in the second
bucket. Clearing that bucket needs a content re-read (``--deep``), which
downloads both sides and compares real md5s.

=== THE COPY MODE ===

``copy`` moves objects from one store to another, and is the Phase 1 first copy.
Three properties it is built for, none of them optional:

  * TWO CLIENTS, BOTH PRODUCTION'S. The endpoint is read inside
    ``OwnCloudBackend.__init__`` from ``STORAGE_S3_ENDPOINT_URL``, so this builds
    each side by setting that variable around the constructor and restoring it.
    Ugly, and deliberately so: the alternative is a hand-rolled client, which is
    what g-372-06's third outcome forbids. Cross-endpoint server-side ``CopyObject``
    does not exist, so every object is streamed down and back up.
  * RESUMABLE, AND SAFE TO RE-RUN. An object already at the destination with a
    matching size AND a matching comparable ETag is skipped. A 79,087-object copy
    WILL be interrupted; a copier that cannot resume turns that into a restart.
  * IT NEVER DELETES, ANYWHERE. Not at the source, not at the destination. The
    source bucket surviving untouched IS the cutover's rollback (working doc
    §13.7 step 8), and nothing here may weaken that.

Copy is not verification. Run ``diff`` afterwards against a fresh enumeration of
both sides; the copier's own success count is a claim about what it did, not
evidence about what is there.

Usage:
    enumerate  [--prefix P] [--out FILE] [--bucket B] [--region R] [--allow-empty]
    copy       --dest-endpoint URL [--source-endpoint URL] [--prefix P]
               [--dest-bucket B] [--limit N] [--dry-run] [--progress-every N]
    diff       --source A.jsonl --dest B.jsonl
               [--source-strip P] [--dest-strip P] [--deep] [--workers N]
               [--source-endpoint URL] [--dest-endpoint URL]
               [--baseline-source M] [--baseline-dest M] [--baseline-report J] [--json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

_MULTIPART = "-"


def _etag(raw: str) -> str:
    """Normalize an ETag: strip the quotes the API returns it in."""
    return (raw or "").strip('"')


def _is_multipart(etag: str) -> bool:
    """Mirror of owncloud_sync._etag_is_multipart -- keyed on the dash that
    marks the ``<hex>-<partcount>`` form. Kept as its own function so the
    comparability rule has one name in this file."""
    return _MULTIPART in _etag(etag)


class _endpoint:
    """Set (or clear) STORAGE_S3_ENDPOINT_URL for the duration of a backend
    construction, then restore it exactly.

    The endpoint is read inside ``OwnCloudBackend.__init__`` rather than passed
    as an argument, so this is the only way to build two differently-targeted
    clients in one process WITHOUT hand-rolling one. Restoring the previous value
    (including its absence) matters: anything constructed later in this process
    must see the environment it expected."""

    def __init__(self, url: str | None):
        self.url = url
        self._prev = None
        self._had = False

    def __enter__(self):
        self._had = "STORAGE_S3_ENDPOINT_URL" in os.environ
        self._prev = os.environ.get("STORAGE_S3_ENDPOINT_URL")
        if self.url:
            os.environ["STORAGE_S3_ENDPOINT_URL"] = self.url
        else:
            os.environ.pop("STORAGE_S3_ENDPOINT_URL", None)
        return self

    def __exit__(self, *exc):
        if self._had:
            os.environ["STORAGE_S3_ENDPOINT_URL"] = self._prev
        else:
            os.environ.pop("STORAGE_S3_ENDPOINT_URL", None)
        return False


def _build_backend(bucket: str, region: str):
    """Construct the PRODUCTION backend so the S3 client, its endpoint override
    and its credentials are the same objects production uses. The lock tables
    are never touched by an enumeration -- per g-372-01 the endpoint override is
    S3-only -- so the table names need only be syntactically present."""
    import owncloud_backend as ob
    return ob.OwnCloudBackend(
        env_id=os.environ.get("ENVIRONMENT_ID", "ayoai-mind"),
        bucket=bucket,
        lock_table=os.environ.get("STORAGE_DDB_LOCK_TABLE", "unused-by-enumerate"),
        sessions_table=os.environ.get("STORAGE_DDB_SESSIONS_TABLE",
                                      "unused-by-enumerate"),
        cache_root=os.environ.get("TMPDIR", "/tmp"),
        region=region,
        machine_id=os.environ.get("MACHINE_ID", "enumerate"),
        # The SCOPED pair, exactly as from_env selects it. Without these two
        # the client factory falls to the SDK's default credential chain, which
        # on a fleet box resolves the ROOT key from the exported env — a
        # different principal from the one the daemon runs as, and one the
        # basement store does not know at all (InvalidAccessKeyId measured
        # 2026-09-11 while the daemon-path probe succeeded). Refuse rather than
        # fall back (guard-1208: say which credential tier answered).
        aws_access_key_id=_scoped_pair()[0],
        aws_secret_access_key=_scoped_pair()[1],
    )


def _scoped_pair() -> "tuple[str, str]":
    akid = os.environ.get("MIND_AWS_ACCESS_KEY_ID", "").strip()
    asec = os.environ.get("MIND_AWS_SECRET_ACCESS_KEY", "").strip()
    if not (akid and asec):
        raise SystemExit("enumerate: MIND_AWS_ACCESS_KEY_ID / MIND_AWS_SECRET_ACCESS_KEY are "
                         "not set -- refusing to fall back to the default credential "
                         "chain (that is a different principal from production's)")
    return akid, asec


# --------------------------------------------------------------------------
# enumerate
# --------------------------------------------------------------------------

def cmd_enumerate(args) -> int:
    bucket = args.bucket
    if not bucket:
        print("FATAL: no bucket. Pass --bucket or set STORAGE_S3_BUCKET.",
              file=sys.stderr)
        return 2

    backend = _build_backend(bucket, args.region)

    prefix = args.prefix
    if prefix is None:
        # Default to this environment's whole subtree -- the same derivation the
        # backend uses for every key, so the default cannot drift from the keys
        # production actually writes.
        prefix = f"{backend._customer_prefix()}{backend.env_id}/"

    endpoint = os.environ.get("STORAGE_S3_ENDPOINT_URL") or "(incumbent)"
    started = time.time()

    rows = []
    total_bytes = 0
    roots: dict[str, dict] = {}
    multipart_n = 0

    paginator = backend.s3.get_paginator("list_objects_v2")
    pages = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        pages += 1
        for obj in page.get("Contents", []) or []:
            key = obj["Key"]
            size = int(obj["Size"])
            etag = _etag(obj.get("ETag", ""))
            rel = key[len(prefix):] if key.startswith(prefix) else key
            root = rel.split("/", 1)[0] if "/" in rel else "(top-level)"

            rows.append({
                "key": key,
                "rel": rel,
                "size": size,
                "etag": etag,
                "multipart": _is_multipart(etag),
                "last_modified": obj["LastModified"].isoformat(),
            })
            total_bytes += size
            if _is_multipart(etag):
                multipart_n += 1
            r = roots.setdefault(root, {"objects": 0, "bytes": 0})
            r["objects"] += 1
            r["bytes"] += size

    elapsed = time.time() - started

    if not rows and not args.allow_empty:
        print(f"FATAL: enumeration returned 0 objects under {prefix!r} "
              f"(bucket={bucket}, endpoint={endpoint}, pages={pages}).\n"
              "A zero from an instrument that can fail silently is ZERO SIGNALS, "
              "not one signal of emptiness (guard-2298, rb-6860). Re-run; pass "
              "--allow-empty only if you have independently established the "
              "prefix is genuinely empty.", file=sys.stderr)
        return 3

    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "bucket": bucket,
        "endpoint": endpoint,
        "prefix": prefix,
        "objects": len(rows),
        "bytes": total_bytes,
        "gib": round(total_bytes / (1024 ** 3), 3),
        "multipart_objects": multipart_n,
        "checksum_comparable": len(rows) - multipart_n,
        "pages": pages,
        "elapsed_s": round(elapsed, 1),
        "roots": {k: dict(v, gib=round(v["bytes"] / (1024 ** 3), 3))
                  for k, v in sorted(roots.items())},
    }

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps({"_summary": summary}) + "\n")
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        # Read back rather than trusting the write echo: a successful write
        # echo says the command ran, not that the bytes are there.
        wrote = sum(1 for _ in out.open(encoding="utf-8")) - 1
        summary["manifest"] = str(out)
        summary["manifest_rows_readback"] = wrote
        if wrote != len(rows):
            print(f"FATAL: manifest read-back mismatch: wrote {len(rows)} rows, "
                  f"read {wrote}.", file=sys.stderr)
            return 4

    print(json.dumps(summary, indent=2))
    return 0


# --------------------------------------------------------------------------
# copy
# --------------------------------------------------------------------------

def _dest_state(s3, bucket: str, key: str):
    """(size, etag, last_modified) at the destination, or None when absent."""
    try:
        h = s3.head_object(Bucket=bucket, Key=key)
        return int(h["ContentLength"]), _etag(h.get("ETag", "")), h.get("LastModified")
    except Exception as exc:                                      # noqa: BLE001
        code = str((getattr(exc, "response", {}).get("Error") or {}).get("Code") or "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def cmd_copy(args) -> int:
    src_bucket = args.bucket
    if not src_bucket:
        print("FATAL: no source bucket. Pass --bucket or set STORAGE_S3_BUCKET.",
              file=sys.stderr)
        return 2
    dst_bucket = args.dest_bucket or src_bucket

    with _endpoint(args.source_endpoint):
        src = _build_backend(src_bucket, args.region)
    with _endpoint(args.dest_endpoint):
        dst = _build_backend(dst_bucket, args.region)

    prefix = args.prefix
    if prefix is None:
        prefix = f"{src._customer_prefix()}{src.env_id}/"

    started = time.time()
    copied = skipped = failed = 0
    copied_bytes = 0
    errors = []
    scanned = 0
    done = 0

    def _one(obj) -> tuple[str, int, dict | None]:
        """Process one object. Returns (outcome, bytes, error-or-None).

        WHY THIS IS THREADED. Measured 2026-09-09 on cc-07 against the incumbent
        store: the serial path runs at 19.1 objects/s -- one network round trip
        per object, latency-bound, not bandwidth-bound. The working set is 79,087
        objects, so a serial HEAD-only pass is ~69 min and a full GET+PUT copy is
        ~3.4 h. The migration plan budgeted "~10 minutes of transfer" from
        255.7 Mbps against 18.2 GB, which is a BYTES calculation for a workload
        whose cost is OBJECT COUNT. That is a 20x error, and it lands inside a
        cutover window with the fleet stopped. Concurrency is what reconciles
        them: at 32 workers the same copy is ~7 min.

        botocore clients are thread-safe, so both sides share one client."""
        key, size = obj["Key"], int(obj["Size"])
        etag = _etag(obj.get("ETag", ""))
        src_lm = obj.get("LastModified")

        # Resume: skip when the destination already holds this object. Size must
        # match always; the ETag is additionally required only when it is
        # COMPARABLE -- a multipart ETag legitimately differs across stores, so
        # demanding equality there would re-copy those objects on every run,
        # forever. For a NON-comparable object a same-size REWRITE at the source
        # would be invisible to the size check, so the timestamps decide too:
        # the destination copy is written after the source version it copied, so
        # a source LastModified newer than the destination's means the source
        # moved on -- re-copy. (Hardening added 2026-09-11 while chasing a deep
        # mismatch that turned out to be the source GROWING between the manifest
        # snapshot and the deep read -- see ``changed_during_read`` below -- not
        # a same-size rewrite; the same-size case stays real and unmeasured.)
        try:
            state = _dest_state(dst.s3, dst_bucket, key)
        except Exception as exc:                                  # noqa: BLE001
            return "failed", 0, {"key": key, "phase": "head",
                                 "error": f"{type(exc).__name__}: {exc}"}
        if state is not None:
            d_size, d_etag, d_lm = state
            comparable = not _is_multipart(etag) and not _is_multipart(d_etag)
            if comparable:
                same = d_etag == etag
            elif args.multipart_compare == "size":
                # SIZE ONLY for the non-comparable bucket. The timestamp arm
                # below assumes the destination was written FROM this source, so
                # `src_lm > d_lm` means "the source moved on". That assumption
                # INVERTS on a reverse-direction copy: after the 2026-09-14
                # cutover every object in the basement store carries the
                # LastModified of the migration that wrote it, which is newer
                # than the AWS original it came from — so every multipart object
                # re-copies on every run, forever. Measured 2026-09-16 (bravo,
                # cc-13, g-372-24): 615 objects / 19.8 GiB "to copy" against a
                # true delta of 423 objects / 0.85 GiB. Size equality is NOT
                # content verification (guard-6371) and this mode does not claim
                # it is: it is the right predicate for an append-only archive,
                # where a key is written once and only ever grows, and it is
                # wrong for any source that rewrites objects in place.
                same = True
            else:
                same = not (src_lm and d_lm and src_lm > d_lm)
            if d_size == size and same:
                return "skipped", size, None

        if args.dry_run:
            return "copied", size, None

        try:
            # CARRY ContentEncoding AND Metadata (g-372-21, 2026-09-14 window).
            # `upload_fileobj` sends the BODY and nothing else, so before this a
            # gzip-encoded object landed at the destination with no
            # `ContentEncoding: gzip` -- every later reader got compressed bytes
            # it did not know to decompress -- and the plain-md5 sidecar metadata
            # that the integrity checks compare against was simply absent
            # (rb-10944: a post-codec size mismatch on a copy diff IS this, not
            # corruption). The source GET response already carries both, so no
            # extra HEAD round trip is needed -- and on a 79k-object copy whose
            # cost is round trips (see the threading note above), that matters.
            # Silent in BOTH directions: the copy returns success and the object
            # looks present, which is why the pin asserts the DESTINATION's
            # stored kwargs rather than this call's return value.
            resp = src.s3.get_object(Bucket=src_bucket, Key=key)
            body = resp["Body"]
            extra = {}
            if resp.get("ContentEncoding"):
                extra["ContentEncoding"] = resp["ContentEncoding"]
            if resp.get("Metadata"):
                extra["Metadata"] = dict(resp["Metadata"])
            if extra:
                dst.s3.upload_fileobj(body, dst_bucket, key, ExtraArgs=extra)
            else:
                dst.s3.upload_fileobj(body, dst_bucket, key)
            return "copied", size, None
        except Exception as exc:                                  # noqa: BLE001
            return "failed", 0, {"key": key, "phase": "transfer",
                                 "error": f"{type(exc).__name__}: {exc}"}

    from concurrent.futures import ThreadPoolExecutor

    paginator = src.s3.get_paginator("list_objects_v2")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        stop = False
        for page in paginator.paginate(Bucket=src_bucket, Prefix=prefix):
            batch = []
            for obj in page.get("Contents", []) or []:
                if args.limit and scanned >= args.limit:
                    stop = True
                    break
                scanned += 1
                batch.append(obj)
            for outcome, nbytes, err in pool.map(_one, batch):
                done += 1
                if outcome == "copied":
                    copied += 1
                    copied_bytes += nbytes
                elif outcome == "skipped":
                    skipped += 1
                else:
                    failed += 1
                    if err:
                        errors.append(err)
                if args.progress_every and done % args.progress_every == 0:
                    el = time.time() - started
                    print(f"[{int(el)}s] scanned={scanned} copied={copied} "
                          f"skipped={skipped} failed={failed} "
                          f"copied_gib={copied_bytes / 1024 ** 3:.2f} "
                          f"rate={done / el:.1f}/s", file=sys.stderr)
            if stop:
                break

    elapsed = time.time() - started
    report = {
        "mode": "dry-run" if args.dry_run else "copy",
        "source": {"bucket": src_bucket,
                   "endpoint": args.source_endpoint or "(incumbent)"},
        "dest": {"bucket": dst_bucket, "endpoint": args.dest_endpoint},
        "prefix": prefix,
        "multipart_compare": args.multipart_compare,
        "scanned": scanned,
        "copied": copied,
        "skipped_already_present": skipped,
        "failed": failed,
        "copied_bytes": copied_bytes,
        "copied_gib": round(copied_bytes / (1024 ** 3), 3),
        "elapsed_s": round(elapsed, 1),
        "objects_per_s": round((copied + skipped) / elapsed, 1) if elapsed else None,
        "errors": errors[:25],
        "note": ("COPY IS NOT VERIFICATION. Re-enumerate both sides and run "
                 "`diff` -- these counts say what this run did, not what is "
                 "there. Nothing was deleted at either end."),
    }
    print(json.dumps(report, indent=2))
    return 0 if failed == 0 else 1


# --------------------------------------------------------------------------
# diff
# --------------------------------------------------------------------------

def _load(path: str, strip: str | None) -> tuple[dict, dict]:
    summary = {}
    by_rel: dict[str, dict] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if "_summary" in rec:
                summary = rec["_summary"]
                continue
            rel = rec["rel"]
            if strip and rel.startswith(strip):
                rel = rel[len(strip):]
            by_rel[rel] = rec
    return summary, by_rel


def _deep_md5(backend, bucket: str, key: str) -> tuple[str, str]:
    """(content md5, ETag the GET actually served). The ETag is compared with the
    manifest row by the caller: on a LIVE store an object can change between the
    enumeration and this read, and then the md5 describes a version the manifest
    never listed -- that is churn, not a mismatch (measured 2026-09-11: a
    transcript grew 65.25 -> 68.48 MB between the snapshot and the read)."""
    h = hashlib.md5()
    resp = backend.s3.get_object(Bucket=bucket, Key=key)
    body = resp["Body"]
    for chunk in iter(lambda: body.read(1024 * 1024), b""):
        h.update(chunk)
    return h.hexdigest(), _etag(resp.get("ETag", ""))


def _deep_endpoint(side: str, summary: dict, override: str | None) -> str | None:
    """The endpoint one manifest side is read through for --deep: the endpoint
    the manifest itself records (``(incumbent)`` = the regional default =
    unset). An explicit flag may CONFIRM it; a flag that contradicts a recorded
    endpoint is refused, because the manifest is the record of where those
    ETags came from (guard-1857)."""
    recorded = summary.get("endpoint") or ""
    rec = None if recorded in ("", "(incumbent)") else recorded
    if override is None:
        return rec
    ov = None if override in ("", "(incumbent)") else override
    if recorded and ov != rec:
        raise ValueError(f"--{side}-endpoint {override!r} contradicts the {side} "
                         f"manifest, which was enumerated through {recorded!r}")
    return ov


def cmd_diff(args) -> int:
    src_sum, src = _load(args.source, args.source_strip)
    dst_sum, dst = _load(args.dest, args.dest_strip)

    missing = sorted(set(src) - set(dst))
    extra = sorted(set(dst) - set(src))
    common = sorted(set(src) & set(dst))

    size_mismatch = []
    checksum_ok = []
    checksum_bad = []
    unverifiable = []

    for rel in common:
        a, b = src[rel], dst[rel]
        if a["size"] != b["size"]:
            size_mismatch.append({"rel": rel, "source": a["size"], "dest": b["size"]})
            continue
        if a["multipart"] or b["multipart"]:
            # An ETag that encodes the uploader's part size is not a content
            # checksum. Neither equality nor inequality is evidence here.
            unverifiable.append(rel)
            continue
        if a["etag"] == b["etag"]:
            checksum_ok.append(rel)
        else:
            checksum_bad.append({"rel": rel, "source": a["etag"], "dest": b["etag"]})

    deep_ok, deep_bad, deep_err, deep_baseline, deep_changed = [], [], [], [], []
    src_ep = dst_ep = None
    if args.deep and unverifiable:
        bucket_s = src_sum.get("bucket")
        bucket_d = dst_sum.get("bucket")
        if not (bucket_s and bucket_d):
            print("FATAL: --deep needs both manifests to carry a _summary line "
                  "with a bucket.", file=sys.stderr)
            return 2
        # Each side is read through the endpoint ITS OWN manifest was
        # enumerated from. Reading both sides through one endpoint compares
        # every object with itself whenever the two stores reuse a bucket
        # name -- and this deployment's basement store deliberately does
        # (guard-4592; caught 2026-09-11 before the first verification run).
        try:
            src_ep = _deep_endpoint("source", src_sum, args.source_endpoint)
            dst_ep = _deep_endpoint("dest", dst_sum, args.dest_endpoint)
        except ValueError as exc:
            print(f"FATAL: {exc}", file=sys.stderr)
            return 2
        if src_ep == dst_ep and bucket_s == bucket_d:
            print("FATAL: --deep would read BOTH sides through the same endpoint "
                  f"({src_ep or '(incumbent)'}) and bucket ({bucket_s}) -- that "
                  "compares each object with itself and proves nothing. Enumerate "
                  "each side through its own endpoint (or pass --source-endpoint / "
                  "--dest-endpoint).", file=sys.stderr)
            return 2
        with _endpoint(src_ep):
            src_be = _build_backend(bucket_s, args.region)
        with _endpoint(dst_ep):
            dst_be = _build_backend(bucket_d, args.region)

        # A baseline is the manifest PAIR from an earlier VERIFIED run. Within
        # ONE store an unchanged (size, ETag) pair means unchanged bytes, so an
        # object unchanged on BOTH sides since that run inherits its verdict
        # instead of being re-read: the cutover-night delta verify then costs
        # what changed, not the whole multipart population.
        base_src: dict[str, dict] = {}
        base_dst: dict[str, dict] = {}
        base_ok: set[str] = set()
        if args.baseline_source or args.baseline_dest or args.baseline_report:
            if not (args.baseline_source and args.baseline_dest and args.baseline_report):
                print("FATAL: --baseline-source, --baseline-dest and --baseline-report go "
                      "together.", file=sys.stderr)
                return 2
            _, base_src = _load(args.baseline_source, args.source_strip)
            _, base_dst = _load(args.baseline_dest, args.dest_strip)
            with open(args.baseline_report, encoding="utf-8") as fh:
                base_rep = json.load(fh)
            # Only what the baseline RUN actually proved carries over: the rels it
            # lists as content-verified. Its overall verdict does not matter (a
            # live fleet's churn can fail the verdict while every stable object
            # verified), and a rel it found MISMATCHED or never read is re-read
            # here however unchanged it is -- otherwise a bad copy inherits a
            # good verdict forever.
            base_ok = set((base_rep.get("deep") or {}).get("content_md5_match_rels") or [])
        todo = []
        for rel in unverifiable:
            bs, bd = base_src.get(rel), base_dst.get(rel)
            if (rel in base_ok and bs and bd
                    and (bs["size"], bs["etag"]) == (src[rel]["size"], src[rel]["etag"])
                    and (bd["size"], bd["etag"]) == (dst[rel]["size"], dst[rel]["etag"])):
                deep_baseline.append(rel)
            else:
                todo.append(rel)

        def _both(rel: str):
            ma, ea = _deep_md5(src_be, bucket_s, src[rel]["key"])
            mb, eb = _deep_md5(dst_be, bucket_d, dst[rel]["key"])
            changed = (ea and ea != src[rel]["etag"]) or (eb and eb != dst[rel]["etag"])
            return ma, mb, bool(changed)

        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = {pool.submit(_both, rel): rel for rel in todo}
            for fut in as_completed(futures):
                rel = futures[fut]
                try:
                    ma, mb, changed = fut.result()
                except Exception as exc:                          # noqa: BLE001
                    deep_err.append({"rel": rel, "error": f"{type(exc).__name__}: {exc}"})
                    continue
                if ma == mb:
                    deep_ok.append(rel)
                elif changed:
                    # The GET served a different version from the one the manifest
                    # listed: the object moved under us. Not verified, not corrupt.
                    deep_changed.append(rel)
                else:
                    deep_bad.append(rel)
        deep_err.sort(key=lambda e: e["rel"])

    verified = (not missing and not extra and not size_mismatch
                and not checksum_bad and not deep_bad and not deep_err and not deep_changed
                and (not unverifiable or (args.deep and not deep_err)))

    report = {
        "verdict": "VERIFIED" if verified else "NOT VERIFIED",
        "source": {"manifest": args.source, "objects": len(src),
                   "bytes": src_sum.get("bytes"), "prefix": src_sum.get("prefix")},
        "dest": {"manifest": args.dest, "objects": len(dst),
                 "bytes": dst_sum.get("bytes"), "prefix": dst_sum.get("prefix")},
        "counts": {
            "common": len(common),
            "missing_at_dest": len(missing),
            "extra_at_dest": len(extra),
            "size_mismatch": len(size_mismatch),
            "checksum_verified": len(checksum_ok),
            "checksum_mismatch": len(checksum_bad),
            "checksum_unverifiable_multipart": len(unverifiable),
        },
        "samples": {
            "missing_at_dest": missing[:20],
            "extra_at_dest": extra[:20],
            "size_mismatch": size_mismatch[:20],
            "checksum_mismatch": checksum_bad[:20],
            "checksum_unverifiable_multipart": unverifiable[:20],
        },
    }
    if args.deep:
        report["deep"] = {"content_md5_match": len(deep_ok),
                          "content_md5_mismatch": len(deep_bad),
                          "content_md5_mismatch_rels": sorted(deep_bad),
                          "changed_during_read": len(deep_changed),
                          "changed_during_read_rels": sorted(deep_changed),
                          "content_md5_match_rels": sorted(deep_ok) + sorted(deep_baseline),
                          "inherited_from_baseline": len(deep_baseline),
                          "objects_read": len(deep_ok) + len(deep_bad) + len(deep_err),
                          "read_through": {"source": src_ep or "(incumbent)",
                                           "dest": dst_ep or "(incumbent)"},
                          "workers": args.workers,
                          "errors": deep_err[:20]}

    if unverifiable and not args.deep:
        report["note"] = (
            f"{len(unverifiable)} object(s) carry a multipart ETag, which encodes "
            "the uploader's part size rather than the content. Their ETags are "
            "NOT comparable across stores in either direction. This run is NOT a "
            "full checksum verification until --deep clears them.")

    if args.as_json:
        print(json.dumps(report, indent=2))
    else:
        print(json.dumps(report, indent=2))
    return 0 if verified else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__ and __doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("enumerate", help="list a prefix into a JSONL manifest")
    e.add_argument("--bucket", default=os.environ.get("STORAGE_S3_BUCKET"))
    e.add_argument("--region", default=os.environ.get("AWS_DEFAULT_REGION", "us-east-2"))
    e.add_argument("--prefix", default=None,
                   help="key prefix (default: this environment's whole subtree)")
    e.add_argument("--out", default=None, help="write the JSONL manifest here")
    e.add_argument("--allow-empty", action="store_true",
                   help="permit a zero-object result (see the module docstring)")
    e.set_defaults(func=cmd_enumerate)

    c = sub.add_parser("copy", help="copy objects from one store to another")
    c.add_argument("--bucket", default=os.environ.get("STORAGE_S3_BUCKET"),
                   help="source bucket (default: STORAGE_S3_BUCKET)")
    c.add_argument("--dest-bucket", default=None,
                   help="destination bucket (default: same name as the source)")
    c.add_argument("--source-endpoint", default=None,
                   help="source endpoint URL (default: unset = the incumbent store)")
    c.add_argument("--dest-endpoint", required=True,
                   help="destination endpoint URL, e.g. http://<host>:9000")
    c.add_argument("--region", default=os.environ.get("AWS_DEFAULT_REGION", "us-east-2"))
    c.add_argument("--prefix", default=None,
                   help="key prefix to copy (default: this environment's subtree)")
    c.add_argument("--limit", type=int, default=0,
                   help="stop after N objects (0 = no limit); use for a canary")
    c.add_argument("--dry-run", action="store_true",
                   help="report what would be copied; transfer nothing")
    c.add_argument("--workers", type=int, default=32,
                   help="concurrent transfers (default 32). The copy is "
                        "latency-bound at ~19 objects/s serial, so this is what "
                        "keeps the first copy inside the cutover window")
    c.add_argument("--progress-every", type=int, default=1000,
                   help="emit a progress line to stderr every N objects (0 = off)")
    c.add_argument("--multipart-compare", choices=("timestamp", "size"),
                   default="timestamp",
                   help="how to decide whether a MULTIPART object (whose ETag is "
                        "not comparable across stores, guard-6371) already "
                        "matches at the destination. timestamp (default): a "
                        "source newer than the destination means re-copy — "
                        "correct when the destination was written from this "
                        "source. size: same size means present — correct for a "
                        "reverse-direction or append-only copy, where the "
                        "timestamp rule re-copies every large object forever")
    c.set_defaults(func=cmd_copy)

    d = sub.add_parser("diff", help="compare a destination manifest to a source")
    d.add_argument("--source", required=True)
    d.add_argument("--dest", required=True)
    d.add_argument("--source-strip", default=None,
                   help="prefix to strip from source rel paths before matching")
    d.add_argument("--dest-strip", default=None,
                   help="prefix to strip from dest rel paths before matching")
    d.add_argument("--deep", action="store_true",
                   help="download and md5 the objects whose ETags are not "
                        "comparable (multipart); required for a full verdict")
    d.add_argument("--source-endpoint", default=None,
                   help="endpoint the SOURCE manifest's objects are read through "
                        "for --deep; defaults to the endpoint that manifest records "
                        "and may not contradict it")
    d.add_argument("--dest-endpoint", default=None,
                   help="same, for the DEST manifest")
    d.add_argument("--baseline-source", default=None,
                   help="source manifest from an earlier deep run; with "
                        "--baseline-dest and --baseline-report, objects that run "
                        "content-verified and that are unchanged on BOTH sides "
                        "inherit its verdict instead of a re-read")
    d.add_argument("--baseline-dest", default=None,
                   help="dest manifest from that same earlier run")
    d.add_argument("--baseline-report", default=None,
                   help="that run's diff --json report; only rels it lists under "
                        "deep.content_md5_match_rels can inherit")
    d.add_argument("--workers", type=int, default=8,
                   help="parallel object reads for --deep (default 8)")
    d.add_argument("--region", default=os.environ.get("AWS_DEFAULT_REGION", "us-east-2"))
    d.add_argument("--json", dest="as_json", action="store_true")
    d.set_defaults(func=cmd_diff)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
