"""test_retrieve_entry_type_endpoint_e2e.py --  (follow-up to ).

g-306-11 added a reasoning-bank `entry_type` filter to retrieve across three
layers: the engine (core/scripts/retrieve.py load_reasoning_bank), the daemon
endpoint (mind_api/src/endpoints/retrieve.py GET /v1/retrieve?entry_type=...),
and the wrapper. test_retrieve_supplementary_filter.py
::test_load_rb_entry_type_filters_to_procedure pins the ENGINE function in
isolation, but NOTHING exercised the endpoint-to-engine integration path over
HTTP -- the exact wrapper->daemon layer a query-param plumbing bug would break
(endpoint reading the wrong param name, not forwarding entry_type to the
engine, or a default that silently drops the filter).

This hits GET /v1/retrieve?entry_type=procedure end-to-end via the in-process
DaemonFixture and asserts the filter actually restricts the result set to
procedure-tagged records -- with a NO-FILTER CONTROL proving the records ARE
all retrievable absent the filter, so the filtered-out absence is the filter's
doing and not some unrelated exclusion.

Pure stdlib + the shared DaemonFixture. Self-contained: never touches the live
world (DaemonFixture spins an in-process daemon against a temp world). Uses the
in-process daemon pattern (NOT a real subprocess daemon) so it is NOT
daemon_integration-marked and is safe to run with a live daemon present
(guard-672 / run-full-suite-after-deep-code live-daemon exception).

Cross-references:
  - g-306-11 -- added the entry_type filter (engine + endpoint + wrapper)
  - test_retrieve_supplementary_filter.py -- pins the engine filter in isolation
  - test_retrieve_daemon_readonly_false.py -- the endpoint-e2e pattern reused here
"""
from __future__ import annotations

import json
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _daemon_fixture import DaemonFixture  # noqa: E402

# Distinctive synthetic category. MUST NOT start with "framework-": framework-*
# categories (applies_to in {framework,any}) auto-escalate to the universal
# meta_lessons partition. A domain-partition (reasoning_bank) record needs a
# non-framework category + applies_to NOT in {framework,any} -- mirrors the
# constraint documented in test_retrieve_daemon_readonly_false.py.
CATEGORY = "test-entrytype-e2e-cat"
PROC_IDS = {"rb-et-proc-0", "rb-et-proc-1"}
ORD_IDS = {"rb-et-ord-0", "rb-et-ord-1"}


def _seed_world(tmp: Path) -> Path:
    """Seed a temp world with 2 procedure-tagged + 2 ordinary (untagged) RB
    records, all matching CATEGORY. The record shape mirrors what the matcher
    recognises (top-level category + when_to_use.category + active status)."""
    world = tmp / "world"
    (world / "knowledge" / "tree").mkdir(parents=True, exist_ok=True)

    def _rec(rid: str, entry_type: str | None = None) -> dict:
        r = {
            "id": rid,
            "title": f"entry_type e2e seed {rid}",
            "type": "success",
            "category": CATEGORY,
            "content": f"{rid}: entry_type endpoint-filter e2e seed",
            "applies_to": "domain",
            "status": "active",
            "when_to_use": {"category": CATEGORY},
            "utilization": {
                "retrieval_count": 0,
                "times_helpful": 0,
                "times_noise": 0,
                "last_retrieved": None,
            },
        }
        if entry_type is not None:
            r["entry_type"] = entry_type
        return r

    recs = [
        _rec("rb-et-proc-0", entry_type="procedure"),
        _rec("rb-et-ord-0"),
        _rec("rb-et-proc-1", entry_type="procedure"),
        _rec("rb-et-ord-1"),
    ]
    (world / "reasoning-bank.jsonl").write_text(
        "\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    (world / "knowledge" / "tree" / "_tree.yaml").write_text(
        "nodes: {}\n", encoding="utf-8")
    return world


def _http_get(port: int, path: str, agent: str = "alpha"):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers={"X-Mind-Agent": agent},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status, resp.read().decode("utf-8")


def _retrieve_rb_ids(port: int, *, entry_type: str | None = None) -> set:
    """GET /v1/retrieve and return the union of reasoning_bank + meta_lessons
    ids. goal= keeps the counter-bump (read_only=false) lane truthy; depth
    shallow's supplementary cap (20) is far above the 4 seeds."""
    params = {"category": CATEGORY, "depth": "shallow", "goal": "g-test-entrytype"}
    if entry_type is not None:
        params["entry_type"] = entry_type
    status, body = _http_get(port, "/v1/retrieve?" + urllib.parse.urlencode(params))
    assert status == 200, f"HTTP {status}: {body[:200]}"
    data = json.loads(body)
    rb = data.get("reasoning_bank", []) or []
    ml = data.get("meta_lessons", []) or []
    return {r.get("id") for r in rb} | {r.get("id") for r in ml}


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="retrieve-entrytype-e2e-") as tmpd:
        world = _seed_world(Path(tmpd))
        with DaemonFixture(world) as df:
            all_ids = _retrieve_rb_ids(df.port)                         # control: no filter
            proc_ids = _retrieve_rb_ids(df.port, entry_type="procedure")  # filtered

    # 1: control proves all four seeds are retrievable absent the filter, so any
    #    absence under the filter below is the filter's doing -- not an
    #    unrelated exclusion (sparse-match, cap, or category miss).
    if not (PROC_IDS | ORD_IDS) <= all_ids:
        print(f"FAIL: control retrieve missing seeds; got {sorted(all_ids)}",
              file=sys.stderr)
        return 1

    # 2: entry_type=procedure returns the procedure-tagged records.
    if not PROC_IDS <= proc_ids:
        print(f"FAIL: entry_type=procedure dropped procedure seeds; "
              f"got {sorted(proc_ids)}", file=sys.stderr)
        return 1

    # 3: the discriminating axis -- entry_type=procedure EXCLUDES the ordinary
    #    (untagged) records. An endpoint that ignored entry_type, or failed to
    #    forward it to the engine, would leak the ordinary records here.
    leaked = ORD_IDS & proc_ids
    if leaked:
        print(f"FAIL: entry_type=procedure leaked ordinary records {sorted(leaked)} "
              f"-- endpoint did not forward the filter to the engine",
              file=sys.stderr)
        return 1

    print(f"PASS: /v1/retrieve?entry_type=procedure returned {sorted(proc_ids & PROC_IDS)} "
          f"and excluded ordinary {sorted(ORD_IDS)} (control returned all "
          f"{len(all_ids)}) -- endpoint-to-engine filter path intact")
    return 0


def test_retrieve_entry_type_endpoint_filters_to_procedure():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
