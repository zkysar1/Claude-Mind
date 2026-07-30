"""test_retrieve_daemon_readonly_false.py — regression pin for the daemon
/v1/retrieve counter-bump (read_only=false) lane returning a POPULATED body.

Regression for g-115-765 / g-115-785 (Decision #58, mind_api/src/endpoints/
retrieve.py). The 2026-05-14 daemon-only cutover (commit 25d6520) deleted
retrieve.py's argparse + main() + __main__. The read_only=false
(counter-bump / goal-execution) lane of the retrieve wrapper fell through to
that deleted CLI and silently returned EMPTY (rc=0, 0 bytes) for every
autonomous-mode retrieve from 2026-05-14 until Decision #58 made the
/v1/retrieve endpoint serve BOTH the read-only AND counter-bump paths
in-daemon (no more read_only_required 400).

Coverage gap this closes (verified 2026-05-27):
  - test_retrieve_supplementary_filter.py pins load_reasoning_bank(
    read_only=False) at the MODULE-FUNCTION level (in-process call).
  - test_retrieve_write_locking.py pins _locked_bump_jsonl concurrency.
  NEITHER exercises the daemon /v1/retrieve ENDPOINT's read_only=false path —
  the exact wrapper->daemon layer that broke. This test hits the endpoint over
  HTTP via the in-process DaemonFixture, with read_only ABSENT + a goal param
  (so the g-304-01 no-goal auto-read-only gate at retrieve.py endpoint L267
  does NOT flip the request to read_only=true), and asserts the counter-bump
  lane returns a populated body.

Pure stdlib + the shared DaemonFixture. Self-contained: never touches the live
world directory (DaemonFixture spins an in-process daemon against a temp world).
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

# Distinctive synthetic category so bidirectional-substring matching is
# unambiguous and cannot collide with any real seeded data (the world is a
# fresh temp dir). MUST NOT start with "framework-": is_universal_rb auto-
# escalates framework-* categories (and applies_to in {framework,any}) to the
# universal `meta_lessons` partition. A domain-partition (reasoning_bank)
# record needs a non-framework category + applies_to NOT in {framework,any}.
CATEGORY = "test-retrieve-rofalse-cat"
SEED_IDS = {"rb-rofalse-000", "rb-rofalse-001", "rb-rofalse-002"}


def _seed_world(tmp: Path) -> Path:
    """Seed a temp world with 3 active reasoning-bank records matching CATEGORY
    so the read_only=false lane has data to return. Mirrors the record shape
    the matcher recognises (category + when_to_use.category + active status)."""
    world = tmp / "world"
    (world / "knowledge" / "tree").mkdir(parents=True, exist_ok=True)

    recs = []
    for i in range(3):
        recs.append({
            "id": f"rb-rofalse-{i:03d}",
            "title": f"daemon read_only=false regression seed {i}",
            "type": "success",
            "category": CATEGORY,
            "content": f"seed {i}: counter-bump lane must return this record",
            "applies_to": "domain",
            "status": "active",
            "when_to_use": {"category": CATEGORY},
            "utilization": {
                "retrieval_count": 0,
                "times_helpful": 0,
                "times_noise": 0,
                "last_retrieved": None,
            },
        })
    (world / "reasoning-bank.jsonl").write_text(
        "\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    # Minimal well-formed tree (empty nodes) so the tree read does not error;
    # the regression is about the supplementary (reasoning_bank) lane.
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


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="retrieve-ro-false-") as tmpd:
        world = _seed_world(Path(tmpd))
        with DaemonFixture(world) as df:
            # read_only ABSENT (no &read_only=1) + goal provided. The goal
            # keeps effective_goal truthy so the endpoint's  no-goal
            # gate does NOT auto-flip the request to read_only=true — the
            # genuine counter-bump path runs (the exact path that broke).
            q = urllib.parse.urlencode({
                "category": CATEGORY,
                "depth": "shallow",
                "goal": "g-test-rofalse",
            })
            status, body = _http_get(df.port, f"/v1/retrieve?{q}")

    # 1: HTTP 200 — not the deleted-CLI silent failure, not the old
    #    read_only_required 400, not a 5xx.
    if status != 200:
        print(f"FAIL: expected HTTP 200, got {status}: {body[:200]}",
              file=sys.stderr)
        return 1

    # 2: non-empty body (the exact regression signature was rc=0 + 0 bytes).
    if not body.strip():
        print("FAIL: empty body — the silent-empty regression reproduced",
              file=sys.stderr)
        return 1

    # 3: valid JSON.
    try:
        data = json.loads(body)
    except Exception as e:
        print(f"FAIL: body not valid JSON: {e}", file=sys.stderr)
        return 1

    # 4: the counter-bump lane actually ran — read_only resolved to FALSE,
    #    NOT auto-flipped to true. This is the discriminating axis: if the
    #    endpoint reverted to requiring read_only=1, or the no-goal gate
    #    over-fired, this assertion catches it.
    ro = data.get("meta", {}).get("read_only")
    if ro is not False:
        print(f"FAIL: meta.read_only expected False (counter-bump lane), "
              f"got {ro!r}", file=sys.stderr)
        return 1

    # 5: populated — the seeded records came back. The regression returned an
    #    EMPTY body on this lane. Check BOTH partitions (domain reasoning_bank
    #    + universal meta_lessons) so the pin is robust to the universal/domain
    #    split; the seed (non-framework category, applies_to=domain) lands in
    #    reasoning_bank.
    rb = data.get("reasoning_bank", []) or []
    ml = data.get("meta_lessons", []) or []
    returned = {r.get("id") for r in rb} | {r.get("id") for r in ml}
    if not (returned & SEED_IDS):
        print(f"FAIL: seeded records absent on read_only=false lane; "
              f"reasoning_bank ids={sorted(r.get('id') for r in rb)}, "
              f"meta_lessons ids={sorted(r.get('id') for r in ml)}",
              file=sys.stderr)
        return 1

    print(f"PASS: /v1/retrieve read_only=false lane returned {len(rb)} domain "
          f"+ {len(ml)} universal record(s) incl. {sorted(returned & SEED_IDS)} "
          f"(HTTP 200, meta.read_only=False) — counter-bump lane serves a "
          f"populated body")
    return 0


def test_retrieve_daemon_readonly_false_returns_populated():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
