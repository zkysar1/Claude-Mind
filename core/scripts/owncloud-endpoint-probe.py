#!/usr/bin/env python3
# domain-leak-exempt: the S3 verbs below are the SUBJECT of the probe (it
# proves an object-store endpoint is reachable and writable through the
# production client), not an incidental cloud reference.
"""Own-cloud endpoint round-trip probe (g-372-07 outcome 2; g-372-08 outcome 1).

Proves, from THIS box and through PRODUCTION'S client, that the object store
the environment currently selects is reachable and round-trips a write:

    put_object -> head_object (ETag) -> get_object (bytes) -> delete

No head_bucket: HeadBucket needs s3:ListBucket on the bucket ROOT, which the
scoped fleet identity is deliberately denied on the incumbent store (403
measured 2026-09-11 from cc-13; guard-2203 -- a 403 there is zero signal
about the bucket). It would fail the very positive control this probe
exists to provide, while put/head/get/delete under the env prefix succeed.

THE CLIENT IS PRODUCTION'S CLIENT. Like object-store-conformance.py this does
not build its own boto3 client: it constructs an ``OwnCloudBackend`` via
``from_env`` and uses ``backend.s3`` / ``backend.bucket``, so the endpoint
override (``STORAGE_S3_ENDPOINT_URL``), the scoped credentials and the
transport Config are exactly what the daemon on this box will use. Selecting
the store is therefore the g-372-01 switch:

    STORAGE_S3_ENDPOINT_URL=http://<host>:9000 python3 core/scripts/owncloud-endpoint-probe.py
    python3 core/scripts/owncloud-endpoint-probe.py            # the incumbent store

On cutover night this is the per-box "S3-authoritative head on a fresh write"
that g-372-08 outcome 1 asks for: run it on every flipped box and keep the
JSON line.

SAFETY. The probe key lives at ``<customer_prefix><env_id>/_probe/<host>-<ts>``
-- inside the env prefix (the scoped credential is confined to it) and BESIDE
the governed roots, never under one, so the sync layer can never list it as
data. It is deleted in a ``finally``; a leftover is reported, not hidden.

EXIT: 0 ok; 1 a step failed (the JSON names which); 2 backend not constructible.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
    endpoint = os.environ.get("STORAGE_S3_ENDPOINT_URL", "").strip() or "(incumbent)"
    out = {"host": socket.gethostname(), "endpoint": endpoint, "ok": False}
    try:
        from owncloud_backend import OwnCloudBackend  # noqa: E402
        be = OwnCloudBackend.from_env()
    except Exception as e:  # pragma: no cover - environment dependent
        out["error"] = f"backend not constructible: {type(e).__name__}: {e}"
        print(json.dumps(out))
        return 2
    out["bucket"] = be.bucket
    out["resolved_endpoint"] = be.s3_endpoint_url or "aws-regional"
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    key = f"{be._customer_prefix()}{be.env_id}/_probe/{socket.gethostname()}-{stamp}-{os.getpid()}"
    body = f"owncloud-endpoint-probe {out['host']} {stamp}\n".encode()
    out["key"] = key
    step = "put_object"
    t0 = time.monotonic()
    try:
        put = be.s3.put_object(Bucket=be.bucket, Key=key, Body=body)
        out["put_etag"] = put.get("ETag", "").strip('"')
        step = "head_object"
        head = be.s3.head_object(Bucket=be.bucket, Key=key)
        out["head_etag"] = head.get("ETag", "").strip('"')
        out["head_bytes"] = int(head.get("ContentLength", -1))
        step = "get_object"
        got = be.s3.get_object(Bucket=be.bucket, Key=key)["Body"].read()
        out["get_bytes"] = len(got)
        out["bytes_match"] = got == body
        out["etag_is_md5"] = out["head_etag"] == hashlib.md5(body).hexdigest()
        out["ms"] = round((time.monotonic() - t0) * 1000, 1)
        out["ok"] = bool(out["bytes_match"] and out["head_etag"] == out["put_etag"]
                         and out["head_bytes"] == len(body))
        if not out["ok"]:
            out["error"] = "round-trip mismatch (see fields)"
    except Exception as e:
        out["error"] = f"{step}: {type(e).__name__}: {e}"
    finally:
        try:
            be.s3.delete_object(Bucket=be.bucket, Key=key)
            out["cleaned"] = True
        except Exception as e:
            out["cleaned"] = False
            out["leftover"] = f"{type(e).__name__}: {e}"
    print(json.dumps(out))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
