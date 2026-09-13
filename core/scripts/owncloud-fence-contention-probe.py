#!/usr/bin/env python3
# domain-leak-exempt: the conditional-PUT verbs and ETag semantics are the
# SUBJECT of this probe (it proves a store rejects a stale fence under
# two-writer contention), not an incidental cloud reference.
"""Two-writer If-Match contention probe (g-372-08 outcome 2; runbook §14.8 step 6).

Two INDEPENDENT clients — two ``OwnCloudBackend.from_env()`` instances, hence
two credential sessions and two HTTP clients — write ONE key on the store the
environment selects (``STORAGE_S3_ENDPOINT_URL`` set = the basement store,
unset = the incumbent):

    A  put K                     -> ETag E1
    B  put K                     -> ETag E2   (a second writer moved the object)
    A  put K  If-Match=E1        -> the store MUST answer 412 PreconditionFailed
    A  put K  If-Match=E2        -> accepted (control: a fresh fence still works)

This is the fence the whole fleet coordinates on (rb-2639: a per-object stale
If-Match is a 412, never a silent overwrite). The conformance harness's C2 proves
the same property from one client; this one holds it with two writers, which is
what §13.7 step 6 asks to see before the roll proceeds.

Key: ``<customer_prefix><env_id>/_probe/contention-<host>-<ts>-<pid>`` — inside the
env prefix, BESIDE the governed roots, deleted in a ``finally``.

Prints one JSON line. EXIT 0 when the stale fence was rejected AND the fresh
fence accepted; 1 otherwise (the JSON names the step); 2 backend not constructible.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _code(exc) -> str:
    try:
        return str(exc.response["Error"]["Code"])
    except Exception:
        return type(exc).__name__


def main() -> int:
    out = {"host": socket.gethostname(),
           "endpoint": os.environ.get("STORAGE_S3_ENDPOINT_URL", "").strip() or "(incumbent)",
           "ok": False}
    try:
        from owncloud_backend import OwnCloudBackend  # noqa: E402
        a = OwnCloudBackend.from_env()
        b = OwnCloudBackend.from_env()
    except Exception as e:  # pragma: no cover - environment dependent
        out["error"] = f"backend not constructible: {type(e).__name__}: {e}"
        print(json.dumps(out))
        return 2
    out["bucket"] = a.bucket
    out["resolved_endpoint"] = a.s3_endpoint_url or "aws-regional"
    out["two_clients"] = a.s3 is not b.s3
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    key = f"{a._customer_prefix()}{a.env_id}/_probe/contention-{socket.gethostname()}-{stamp}-{os.getpid()}"
    out["key"] = key
    step = "A-put"
    try:
        e1 = a.s3.put_object(Bucket=a.bucket, Key=key, Body=b"writer-A v1\n")["ETag"]
        step = "B-put"
        e2 = b.s3.put_object(Bucket=b.bucket, Key=key, Body=b"writer-B v2\n")["ETag"]
        out["etag_a"], out["etag_b"] = e1.strip('"'), e2.strip('"')
        out["object_moved"] = e1 != e2
        step = "A-put-stale-fence"
        try:
            a.s3.put_object(Bucket=a.bucket, Key=key, Body=b"writer-A v3 (stale)\n", IfMatch=e1)
            out["stale_fence_rejected"] = False
            out["error"] = "stale If-Match was ACCEPTED — the store does not fence"
        except Exception as e:
            code = _code(e)
            out["stale_fence_code"] = code
            out["stale_fence_rejected"] = code in ("PreconditionFailed", "412")
        step = "A-put-fresh-fence"
        try:
            e3 = a.s3.put_object(Bucket=a.bucket, Key=key, Body=b"writer-A v3 (fresh)\n", IfMatch=e2)["ETag"]
            out["fresh_fence_accepted"] = True
            out["etag_after"] = e3.strip('"')
        except Exception as e:
            out["fresh_fence_accepted"] = False
            out["fresh_fence_code"] = _code(e)
        out["ok"] = bool(out.get("object_moved") and out.get("stale_fence_rejected")
                         and out.get("fresh_fence_accepted"))
        if not out["ok"] and "error" not in out:
            out["error"] = "see fields"
    except Exception as e:
        out["error"] = f"{step}: {_code(e)}: {e}"
    finally:
        try:
            a.s3.delete_object(Bucket=a.bucket, Key=key)
            out["cleaned"] = True
        except Exception as e:
            out["cleaned"] = False
            out["leftover"] = f"{type(e).__name__}: {e}"
    print(json.dumps(out))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
