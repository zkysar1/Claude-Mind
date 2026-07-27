"""test_retrieve_as_of_endpoint_e2e.py --  (bi-temporal reader, rb-335).

g-306-36 added an `as_of` point-in-time read to retrieve across three layers:
the engine (core/scripts/retrieve.py load_* functions), the daemon endpoint
(mind_api/src/endpoints/retrieve.py GET /v1/retrieve?as_of=...), and the
wrapper. test_bitemporal_reader_path.py pins the ENGINE functions in isolation,
but nothing exercises the endpoint-to-engine integration over HTTP -- the exact
wrapper->daemon layer a query-param plumbing bug would break (wrong param name,
not forwarding as_of to the engine, or the 400-validation silently dropping).

This hits GET /v1/retrieve?as_of=<T> end-to-end via the in-process DaemonFixture
and asserts the point-in-time filter returns the record VERSION valid at T --
with the canonical falsification pair (a closed/retired OLD version + an open
NEW version) proving the daemon surfaces the OLD version at T1 and the NEW at T3,
status-agnostic. A malformed as_of asserts the 400 validation fires at the
endpoint.

Pure stdlib + the shared DaemonFixture. Self-contained: never touches the live
world. In-process daemon (NOT a real subprocess) so it is NOT
daemon_integration-marked and is safe to run with a live daemon present
(guard-672 / run-full-suite-after-deep-code live-daemon exception).

Cross-references:
  - g-306-36 -- added the as_of reader (engine + endpoint + wrapper)
  - g-306-35 -- the writer path (valid_from/valid_to fields)
  - test_bitemporal_reader_path.py -- pins the engine functions in isolation
  - test_retrieve_entry_type_endpoint_e2e.py -- the endpoint-e2e pattern reused here
"""
from __future__ import annotations

import json
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _daemon_fixture import DaemonFixture  # noqa: E402

# Non-framework category so the records land in the domain reasoning_bank
# partition (framework-* + applies_to in {framework,any} escalate to universal).
CATEGORY = "test-asof-e2e-cat"
TCUT = "2026-06-15T00:00:00"
T1 = "2026-06-10T00:00:00"   # inside the OLD interval [06-01, 06-15)
T3 = "2026-06-20T00:00:00"   # inside the NEW interval [06-15, +inf)


def _seed_world(tmp: Path) -> Path:
    """Seed a temp world with a close-old/insert-new RB pair, both matching
    CATEGORY: OLD (retired, valid [06-01,06-15)) and NEW (active, valid
    [06-15,+inf))."""
    world = tmp / "world"
    (world / "knowledge" / "tree").mkdir(parents=True, exist_ok=True)

    def _rec(rid, status, valid_from, valid_to, created):
        return {
            "id": rid,
            "title": f"as_of e2e seed {rid}",
            "type": "success",
            "category": CATEGORY,
            "content": f"{rid}: as_of endpoint e2e seed",
            "applies_to": "domain",
            "status": status,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "created": created,
            "when_to_use": {"category": CATEGORY},
            "utilization": {
                "retrieval_count": 0, "times_helpful": 0,
                "times_noise": 0, "last_retrieved": None,
            },
        }

    recs = [
        _rec("rb-asof-old", "retired", "2026-06-01T00:00:00", TCUT, "2026-06-01T00:00:00"),
        _rec("rb-asof-new", "active", TCUT, None, TCUT),
    ]
    (world / "reasoning-bank.jsonl").write_text(
        "\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    (world / "knowledge" / "tree" / "_tree.yaml").write_text("nodes: {}\n", encoding="utf-8")
    return world


def _http_get(port, path, agent="alpha"):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", headers={"X-Mind-Agent": agent})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status, resp.read().decode("utf-8")


def _retrieve_rb_ids(port, *, as_of=None):
    """GET /v1/retrieve (read-only: no goal) and return the union of
    reasoning_bank + meta_lessons ids."""
    params = {"category": CATEGORY, "depth": "shallow"}
    if as_of is not None:
        params["as_of"] = as_of
    status, body = _http_get(port, "/v1/retrieve?" + urllib.parse.urlencode(params))
    assert status == 200, f"HTTP {status}: {body[:200]}"
    data = json.loads(body)
    rb = data.get("reasoning_bank", []) or []
    ml = data.get("meta_lessons", []) or []
    return {r.get("id") for r in rb} | {r.get("id") for r in ml}


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="retrieve-asof-e2e-") as tmpd:
        world = _seed_world(Path(tmpd))
        with DaemonFixture(world) as df:
            current = _retrieve_rb_ids(df.port)              # no as_of: current active
            at_t1 = _retrieve_rb_ids(df.port, as_of=T1)      # point-in-time T1
            at_t3 = _retrieve_rb_ids(df.port, as_of=T3)      # point-in-time T3
            # malformed as_of => 400 at the endpoint
            malformed_status = None
            try:
                _http_get(df.port, "/v1/retrieve?" + urllib.parse.urlencode(
                    {"category": CATEGORY, "as_of": "not-a-date"}))
            except urllib.error.HTTPError as e:
                malformed_status = e.code

    # 1: default (no as_of) returns ONLY the current active version. The OLD
    #    record is retired, so the current view excludes it -- proving as_of is
    #    not silently always-on.
    if current != {"rb-asof-new"}:
        print(f"FAIL: default view should be {{rb-asof-new}}, got {sorted(current)}",
              file=sys.stderr)
        return 1

    # 2: as_of=T1 returns the OLD closed version (retired, yet valid at T1) and
    #    NOT the new version (not yet valid). This is the discriminating axis:
    #    an endpoint that ignored as_of, or failed to forward it, would return
    #    the current view {rb-asof-new} here instead.
    if at_t1 != {"rb-asof-old"}:
        print(f"FAIL: as_of=T1 should be {{rb-asof-old}}, got {sorted(at_t1)} "
              f"-- endpoint did not forward as_of to the engine (or status filter "
              f"not dropped on the as_of path)", file=sys.stderr)
        return 1

    # 3: as_of=T3 returns the NEW open version and not the OLD (closed at 06-15,
    #    half-open excludes it at 06-20).
    if at_t3 != {"rb-asof-new"}:
        print(f"FAIL: as_of=T3 should be {{rb-asof-new}}, got {sorted(at_t3)}",
              file=sys.stderr)
        return 1

    # 4: malformed as_of is rejected at the endpoint with HTTP 400.
    if malformed_status != 400:
        print(f"FAIL: malformed as_of should 400, got {malformed_status}",
              file=sys.stderr)
        return 1

    print("PASS: /v1/retrieve?as_of -- default={rb-asof-new}, T1={rb-asof-old} "
          "(retired version surfaced), T3={rb-asof-new}, malformed->400 "
          "-- endpoint-to-engine bi-temporal path intact")
    return 0


def _configured_embedding_model_is_loadable() -> bool:
    """True when this box can actually load the CONFIGURED embedding model.

    Guards against a misleading failure mode, not against a real regression
    (g-115-3180). core/config/tree.yaml pins embedding_model_name to
    all-MiniLM-L6-v2 and its own comment says fleet-wide provisioning is NOT
    given and must be probed per box (correction recorded under g-115-3109).
    On a box carrying only bge-small, the retrieve endpoint's encoder load fails
    under local_files_only=True, the request then blows past the 15s urlopen
    bound, and the test dies with a bare socket TimeoutError whose stack points
    at recv_into — naming neither the model nor the cause.

    Skipping is strictly more informative than that red: this asserts the
    bi-temporal as_of path, which cannot be exercised at all without an encoder.
    On any box where the configured model IS present the probe returns True and
    the test runs exactly as before, so a genuine regression is still caught.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from _embedding_model import load_encoder  # noqa: PLC0415
        import yaml  # noqa: PLC0415
        cfg = yaml.safe_load(
            (Path(__file__).resolve().parents[2] / "core" / "config" / "tree.yaml")
            .read_text(encoding="utf-8"))
        name = None
        for section in (cfg or {}).values():
            if isinstance(section, dict) and section.get("embedding_model_name"):
                name = section["embedding_model_name"]
                break
        if not name:
            return True          # cannot determine -> do not mask anything
        load_encoder(name)
        return True
    except Exception:
        return False


def test_retrieve_as_of_endpoint_point_in_time():
    if not _configured_embedding_model_is_loadable():
        pytest.skip(
            "configured embedding model (core/config/tree.yaml "
            "embedding_model_name) is not loadable on this box — the retrieve "
            "endpoint cannot serve, producing an opaque 15s socket timeout. "
            "Environment provisioning gap, tracked under g-115-3109; not a "
            "regression in the as_of path."
        )
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
