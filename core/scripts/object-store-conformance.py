#!/usr/bin/env python3
# domain-leak-exempt: the S3 API verbs, ETag semantics and boto3 client calls
# below are the SUBJECT UNDER TEST, not an incidental cloud reference. This
# harness exists to decide whether a candidate object store can host the
# own-cloud tier, so naming the protocol it must speak is the whole point.
"""Object-store conformance harness (g-372-04).

Decides whether an S3-compatible object store is safe to put the own-cloud tier
on, by measuring the four store behaviours this codebase's correctness actually
rests on. Vendor claims are not evidence: Garage's If-Match support is recorded
as [UNVERIFIED -- model prior] in the migration plan, and this harness is what
settles it.

WHY THESE FOUR CASES. Each maps to a line in ``owncloud_backend.py`` that fails
SILENTLY -- not loudly -- if the store's semantics differ:

  C1 conditional-PUT ACCEPT   Every own-cloud write is a compare-and-swap:
                              ``_put`` sends ``If-Match=<etag observed at read>``
                              (module docstring "Fix #3"). A store that rejects
                              a VALID fence makes every write fail.
  C2 conditional-PUT REJECT   The same fence must 412 when the object moved
                              underneath. A store that silently ACCEPTS a stale
                              fence turns the CAS into a last-write-wins clobber
                              -- concurrent agents overwrite each other with no
                              error anywhere. This is the single most dangerous
                              failure mode on the list.
  C3 create-only              ``If-None-Match: *`` must succeed on a missing key
                              and 412 on an existing one. Used wherever a record
                              must be created exactly once.
  C4 ETag semantics           (a) For a single-part PUT the ETag must BE the
                              content md5 -- ``_maybe_tail_append`` accepts a
                              tail append "ONLY when md5(local_prefix + tail)
                              equals the object's ETag" (backend L861). A store
                              whose ETag is an opaque token silently breaks that
                              comparison.
                              (b) For an upload above the managed-transfer
                              multipart threshold the ETag must carry the
                              ``<hex>-N`` form, because ``_etag_is_multipart``
                              (owncloud_sync L900) keys on the ``-`` to mark an
                              object UNCOMPARABLE. A store that returns a plain
                              md5-shaped ETag for a multipart object makes that
                              classifier read a non-md5 as an md5.

C4a is not in the goal's four-case list; it is the precondition that makes C4b
meaningful, it is cheap, and the code path it protects is load-bearing. Reported
separately so a reader can score the goal's four without it.

THE CLIENT IS PRODUCTION'S CLIENT. The harness does not build its own boto3
client. It constructs an ``OwnCloudBackend`` and uses ``backend.s3``, so the
endpoint override, the scoped credentials, the retry/timeout Config and the
botocore-version preflight are the same objects production uses. Selecting the
target store is therefore exactly the g-372-01 configuration switch:

    STORAGE_S3_ENDPOINT_URL=http://<host>:3900 python3 core/scripts/object-store-conformance.py

With the variable unset the target is AWS itself -- the positive control. A
candidate failing a case that AWS passes is a fact about the candidate; a case
that fails on AWS too is a fact about this harness.

SAFETY. Every object is written under ``<customer_prefix><env_id>/_conformance/
<run-id>/`` -- INSIDE the environment prefix, beside the governed roots rather
than under one. Both halves of that placement are load-bearing and were measured
on 2026-09-09, not assumed:

  * Inside the env prefix because the scoped credential is confined to it. A
    first run used a top-level ``_conformance/`` key and every case returned
    ``AccessDenied/403`` against AWS -- least-privilege working correctly, and a
    verdict about the harness rather than about the store. That is precisely the
    confusion the positive control exists to expose, so it is recorded here.
  * Beside the governed roots, never under one, because production keys are
    ``<customer_prefix><env_id>/<rel>`` (backend ``_s3_key``) and the sync layer
    lists per governed root (``world/``, ``agents/``, ``meta/``). A
    ``_conformance/`` sibling is inside no root's listing, so it can never be
    read as data, pulled, or reconciled.

All keys created are deleted in a ``finally``; a non-empty leftover list is
reported as a failure of the harness, not of the store.

Exit status: 0 iff every REQUIRED case passed. 2 on a harness/setup error (no
verdict was reached) -- never conflate that with a store failure.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Required cases are the goal's four (g-372-04 outcome 2). C4a is advisory-scored
# but still reported; see the module docstring.
REQUIRED = ("C1", "C2", "C3", "C4b")


def _err_code(exc) -> str:
    """Best-effort S3 error code across botocore shapes."""
    resp = getattr(exc, "response", None) or {}
    err = resp.get("Error") or {}
    code = str(err.get("Code") or "")
    status = str((resp.get("ResponseMetadata") or {}).get("HTTPStatusCode") or "")
    return f"{code or type(exc).__name__}/{status or '-'}"


def _is_precondition_failed(exc) -> bool:
    """412 in any of the spellings stores use. The backend's own predicate is
    ``_PRECONDITION = {"PreconditionFailed", "412"}`` (backend L198); this widens
    it only by also accepting the HTTP status, never by accepting a different
    class of error."""
    resp = getattr(exc, "response", None) or {}
    code = str((resp.get("Error") or {}).get("Code") or "")
    status = (resp.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    return code in ("PreconditionFailed", "412") or status == 412


def _etag(s: str) -> str:
    return (s or "").strip('"')


class Harness:
    def __init__(self, backend, bucket: str, prefix: str, mp_threshold: int):
        self.s3 = backend.s3
        self.bucket = bucket
        self.prefix = prefix.rstrip("/") + "/"
        self.mp_threshold = mp_threshold
        self.created: list[str] = []
        self.results: list[dict] = []

    # -- plumbing ---------------------------------------------------------
    def _key(self, name: str) -> str:
        k = self.prefix + name
        self.created.append(k)
        return k

    def _record(self, case: str, name: str, status: str, detail: str) -> None:
        self.results.append({"case": case, "name": name, "status": status,
                             "detail": detail})

    def cleanup(self) -> list[str]:
        """Delete every key this run created. Returns keys that survived."""
        left = []
        for k in self.created:
            try:
                self.s3.delete_object(Bucket=self.bucket, Key=k)
            except Exception:
                try:
                    self.s3.head_object(Bucket=self.bucket, Key=k)
                    left.append(k)          # still there -> genuinely leaked
                except Exception:
                    pass                    # already gone
        return left

    # -- cases ------------------------------------------------------------
    def c1_conditional_put_accept(self) -> str | None:
        """PUT, read the ETag, PUT again fenced on it. Must succeed.
        Returns the now-STALE first ETag for C2, or None if C1 failed."""
        key = self._key("c1-fence-accept")
        try:
            r1 = self.s3.put_object(Bucket=self.bucket, Key=key, Body=b"v1")
            e1 = _etag(r1.get("ETag", ""))
            if not e1:
                self._record("C1", "conditional-PUT accept", "FAIL",
                             "PUT returned no ETag, so no fence can be formed")
                return None
            r2 = self.s3.put_object(Bucket=self.bucket, Key=key, Body=b"v2",
                                    IfMatch=e1)
            e2 = _etag(r2.get("ETag", ""))
            self._record("C1", "conditional-PUT accept", "PASS",
                         f"valid fence accepted; etag {e1[:12]}.. -> {e2[:12]}..")
            return e1
        except Exception as exc:                                # noqa: BLE001
            self._record("C1", "conditional-PUT accept", "FAIL",
                         f"store rejected a VALID If-Match fence: {_err_code(exc)}")
            return None

    def c2_conditional_put_reject(self, stale_etag: str | None) -> None:
        """The stale fence from C1 must now 412. Accepting it is the
        silent-clobber failure -- the worst outcome on this list."""
        if stale_etag is None:
            self._record("C2", "conditional-PUT reject on stale fence", "SKIP",
                         "C1 produced no usable ETag")
            return
        key = self.prefix + "c1-fence-accept"       # already tracked by C1
        try:
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=b"v3",
                               IfMatch=stale_etag)
        except Exception as exc:                                # noqa: BLE001
            if _is_precondition_failed(exc):
                self._record("C2", "conditional-PUT reject on stale fence", "PASS",
                             "stale fence correctly rejected (412)")
            else:
                self._record("C2", "conditional-PUT reject on stale fence", "FAIL",
                             f"rejected, but not as 412: {_err_code(exc)} -- the "
                             "backend maps only PreconditionFailed/412 to "
                             "ConflictError, so this would surface as a hard error")
            return
        self._record("C2", "conditional-PUT reject on stale fence", "FAIL",
                     "STALE FENCE ACCEPTED -- compare-and-swap is not enforced; "
                     "concurrent writers would clobber each other silently")

    def c3_create_only(self) -> None:
        """If-None-Match:* -- succeed when absent, 412 when present."""
        key = self._key("c3-create-only")
        try:
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=b"first",
                               IfNoneMatch="*")
        except Exception as exc:                                # noqa: BLE001
            self._record("C3", "create-only semantics", "FAIL",
                         f"create-only PUT refused on a MISSING key: {_err_code(exc)}")
            return
        try:
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=b"second",
                               IfNoneMatch="*")
        except Exception as exc:                                # noqa: BLE001
            if _is_precondition_failed(exc):
                self._record("C3", "create-only semantics", "PASS",
                             "create succeeded on absent key, rejected (412) on present")
            else:
                self._record("C3", "create-only semantics", "FAIL",
                             f"second create rejected, but not as 412: {_err_code(exc)}")
            return
        self._record("C3", "create-only semantics", "FAIL",
                     "SECOND create-only PUT ACCEPTED -- create-once is not enforced")

    def c4a_singlepart_etag_is_md5(self) -> None:
        """Advisory but load-bearing: the tail-append optimisation compares
        md5(prefix+tail) to the ETag (backend L861)."""
        key = self._key("c4a-singlepart-etag")
        body = b"conformance-single-part-body-" + uuid.uuid4().bytes
        want = hashlib.md5(body).hexdigest()
        try:
            r = self.s3.put_object(Bucket=self.bucket, Key=key, Body=body)
            got = _etag(r.get("ETag", ""))
        except Exception as exc:                                # noqa: BLE001
            self._record("C4a", "single-part ETag is content md5", "FAIL",
                         f"PUT failed: {_err_code(exc)}")
            return
        if got == want:
            self._record("C4a", "single-part ETag is content md5", "PASS",
                         f"etag == md5(body) ({want[:12]}..)")
        else:
            self._record("C4a", "single-part ETag is content md5", "FAIL",
                         f"etag {got[:20]!r} != md5 {want[:12]}.. -- the tail-append "
                         "content check would silently mis-compare")

    def c4b_multipart_etag_form(self) -> None:
        """Above the managed-transfer threshold the ETag must carry '-N'.
        ``_etag_is_multipart`` keys on that dash to mark the object
        UNCOMPARABLE (owncloud_sync L900)."""
        try:
            from boto3.s3.transfer import TransferConfig
        except Exception as exc:                                # noqa: BLE001
            self._record("C4b", "multipart ETag form", "SKIP",
                         f"boto3 transfer unavailable: {exc}")
            return
        key = self._key("c4b-multipart-etag")
        size = self.mp_threshold + (1 << 20)        # threshold + 1 MiB
        cfg = TransferConfig(multipart_threshold=self.mp_threshold,
                             multipart_chunksize=self.mp_threshold)
        body = (b"x" * 1024) * (size // 1024)
        try:
            self.s3.upload_fileobj(io.BytesIO(body), self.bucket, key, Config=cfg)
            got = _etag(self.s3.head_object(Bucket=self.bucket, Key=key).get("ETag", ""))
        except Exception as exc:                                # noqa: BLE001
            self._record("C4b", "multipart ETag form", "FAIL",
                         f"multipart upload/head failed at {size} bytes: {_err_code(exc)}")
            return
        if "-" in got:
            self._record("C4b", "multipart ETag form", "PASS",
                         f"multipart ETag carries the -N form ({got}) at {size} bytes")
        else:
            self._record("C4b", "multipart ETag form", "FAIL",
                         f"multipart upload returned a NON-multipart ETag ({got!r}) -- "
                         "_etag_is_multipart would read a non-md5 value as a content md5")

    def run(self) -> None:
        stale = self.c1_conditional_put_accept()
        self.c2_conditional_put_reject(stale)
        self.c3_create_only()
        self.c4a_singlepart_etag_is_md5()
        self.c4b_multipart_etag_form()


def _build_backend(bucket: str, region: str):
    """Construct the PRODUCTION backend so the S3 client, its endpoint override
    and its credentials are the same objects production uses. DynamoDB is never
    called by this harness -- per g-372-01 the override is S3-only and the lock
    tables stay regional -- so the table names need only be syntactically present."""
    import owncloud_backend as ob
    return ob.OwnCloudBackend(
        env_id=os.environ.get("ENVIRONMENT_ID", "ayoai-mind"),
        bucket=bucket,
        lock_table=os.environ.get("STORAGE_DDB_LOCK_TABLE", "unused-by-conformance"),
        sessions_table=os.environ.get("STORAGE_DDB_SESSIONS_TABLE",
                                      "unused-by-conformance"),
        cache_root=os.environ.get("TMPDIR", "/tmp"),
        region=region,
        machine_id=os.environ.get("MACHINE_ID", "conformance"),
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
        raise SystemExit("conformance: MIND_AWS_ACCESS_KEY_ID / MIND_AWS_SECRET_ACCESS_KEY are "
                         "not set -- refusing to fall back to the default credential "
                         "chain (that is a different principal from production's)")
    return akid, asec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    ap.add_argument("--bucket", default=os.environ.get("STORAGE_S3_BUCKET"),
                    help="target bucket (default: STORAGE_S3_BUCKET)")
    ap.add_argument("--region", default=os.environ.get("AWS_DEFAULT_REGION", "us-east-2"))
    ap.add_argument("--label", default=None,
                    help="name for this target in the report (default: derived "
                         "from STORAGE_S3_ENDPOINT_URL, or 'aws' when unset)")
    ap.add_argument("--multipart-threshold", type=int, default=8 * 1024 * 1024,
                    help="bytes; boto3 managed-transfer default is 8 MiB")
    ap.add_argument("--json", dest="as_json", action="store_true",
                    help="emit the result table as JSON on stdout")
    ap.add_argument("--keep", action="store_true",
                    help="do not delete the conformance objects (debugging only)")
    args = ap.parse_args()

    endpoint = os.environ.get("STORAGE_S3_ENDPOINT_URL", "").strip()
    label = args.label or (endpoint or "aws")

    if not args.bucket:
        print("conformance: no bucket -- pass --bucket or set STORAGE_S3_BUCKET",
              file=sys.stderr)
        return 2

    run_id = f"{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"

    try:
        backend = _build_backend(args.bucket, args.region)
        # Derive the scratch prefix from the backend's OWN key builder rather
        # than hardcoding it: the customer prefix is deployment-specific, and a
        # key outside it is refused by the scoped credential (measured
        # 2026-09-09 -- see SAFETY in the module docstring).
        try:
            env_root = f"{backend._customer_prefix()}{backend.env_id}/"
        except Exception:                                       # noqa: BLE001
            env_root = f"{os.environ.get('ENVIRONMENT_ID', 'ayoai-mind')}/"
        prefix = f"{env_root}_conformance/{run_id}"
    except Exception as exc:                                    # noqa: BLE001
        # A botocore too old for PutObject IfMatch fails HERE, by design: the
        # backend preflights it (L597). That is a harness/environment verdict,
        # not a statement about the store.
        print(f"conformance: SETUP FAILED building the backend client: {exc}",
              file=sys.stderr)
        return 2

    h = Harness(backend, args.bucket, prefix, args.multipart_threshold)
    leaked: list[str] = []
    try:
        h.run()
    finally:
        if not args.keep:
            leaked = h.cleanup()

    required = [r for r in h.results if r["case"] in REQUIRED]
    passed = all(r["status"] == "PASS" for r in required)
    verdict = "PASS" if (passed and not leaked) else "FAIL"

    report = {
        "target": label,
        "endpoint": endpoint or None,
        "bucket": args.bucket,
        "region": args.region,
        "run_id": run_id,
        "prefix": prefix,
        "multipart_threshold": args.multipart_threshold,
        "results": h.results,
        "required_cases": list(REQUIRED),
        "leaked_keys": leaked,
        "verdict": verdict,
    }

    if args.as_json:
        print(json.dumps(report, indent=2))
    else:
        print(f"\nobject-store conformance -- target: {label}")
        print(f"  bucket={args.bucket} region={args.region} prefix={prefix}")
        print(f"  {'case':<5} {'status':<6} {'what':<40} detail")
        print(f"  {'-'*5} {'-'*6} {'-'*40} {'-'*60}")
        for r in h.results:
            req = "" if r["case"] in REQUIRED else "  (advisory)"
            print(f"  {r['case']:<5} {r['status']:<6} {r['name']:<40} "
                  f"{r['detail']}{req}")
        if leaked:
            print(f"\n  HARNESS LEAK: {len(leaked)} object(s) not deleted: {leaked}")
        print(f"\n  VERDICT ({label}): {verdict}"
              f"   [required cases: {', '.join(REQUIRED)}]\n")

    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
