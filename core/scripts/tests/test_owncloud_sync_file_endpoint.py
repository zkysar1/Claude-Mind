"""test_owncloud_sync_file_endpoint.py — route contract for 7 (0).

POST /v1/admin/owncloud-sync-file is the per-file governed push route the
PostToolUse shim (owncloud-push-on-write.sh) and reconcile-owncloud-conflicts
Step 5 call daemon-first. The S3 push leg needs live creds and stays
live-verified (daemon_integration scope); the two lanes BELOW the backend
gate are hermetic and pinned here:

  1. Missing/blank `path` query param -> 400 {"ok": false, "error": ...}.
  2. Non-own-cloud backend -> 200 {"ok": true, "pushed": 0, "reason": ...}
     WITHOUT touching owncloud_sync or the filesystem (the skip fires before
     path resolution, so an arbitrary path is safe).

Pattern: DaemonFixture + direct HTTP POST (bash-free, in-process daemon).
Mirrors test_add_goal_filed_by_agent_stamp.py. DaemonFixture hard-pins
STORAGE_BACKEND=local (guard-955), which is exactly lane 2's trigger — the
pin IS the fixture for the skip lane. Extends the shim-side coverage in
test_owncloud_push_on_write.py from the hook gate to the route contract.
"""

from __future__ import annotations

import json
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

from _daemon_fixture import DaemonFixture  # noqa: E402


def _post_sync_file(port: int, query: str) -> tuple[int, dict]:
    """POST the sync-file route with a raw query string; return (status, json)."""
    url = f"http://127.0.0.1:{port}/v1/admin/owncloud-sync-file"
    if query:
        url += f"?{query}"
    req = urllib.request.Request(url, data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_missing_path_returns_400():
    """No `path` param -> 400 with ok:false and an error naming the param."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd) / "world"
        world.mkdir()
        with DaemonFixture(world) as df:
            status, data = _post_sync_file(df.port, "")
            assert status == 400, f"expected 400, got {status}: {data!r}"
            assert data.get("ok") is False
            assert "path" in (data.get("error") or ""), (
                f"error must name the missing param; got {data.get('error')!r}")


def test_blank_path_returns_400():
    """Whitespace-only `path` is stripped to empty -> same 400 lane."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd) / "world"
        world.mkdir()
        with DaemonFixture(world) as df:
            q = "path=" + urllib.parse.quote("   ", safe="")
            status, data = _post_sync_file(df.port, q)
            assert status == 400, f"expected 400, got {status}: {data!r}"
            assert data.get("ok") is False


def test_non_own_cloud_backend_skips_ok():
    """Under local backend the route no-ops with ok:true + reason, pushed=0.

    The fixture's STORAGE_BACKEND=local pin is the trigger. The path is
    deliberately arbitrary/nonexistent — the skip must fire BEFORE any
    filesystem or owncloud_sync involvement.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd) / "world"
        world.mkdir()
        with DaemonFixture(world) as df:
            q = "path=" + urllib.parse.quote(
                "world/nonexistent/probe-file.yaml", safe="")
            status, data = _post_sync_file(df.port, q)
            assert status == 200, f"expected 200, got {status}: {data!r}"
            assert data.get("ok") is True
            assert data.get("pushed") == 0
            assert data.get("backend") == "local"
            assert "own-cloud" in (data.get("reason") or ""), (
                f"skip must carry a reason; got {data.get('reason')!r}")
