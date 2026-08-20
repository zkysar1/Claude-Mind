"""POST /v1/store/append near-duplicate REFUSAL tier ().

The wrapper-side advisory (store_dupe_warn.py) warns and never blocks —
guard-4090 measured warned twins landing anyway. This suite pins the
enforcement tier at the daemon chokepoint: past refuse_threshold (0.75,
calibrated against 6,550 fleet firings: ambient nearest-neighbour max 0.500,
verbatim twins at 1.0) the append returns 409 near_duplicate carrying
caller-verifiable evidence (guard-1661), increments the existing entry's
times_inferred_helpful (the strengthen event — the whole point of the gate),
and stores nothing. Body field allow_near_dup:true bypasses and is never
persisted.

Mirrors test_runtime_store_rbguard.py structure.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def _post(port: int, path: str, query: dict = None, body: bytes = b"",
          *, agent: str = "alpha"):
    qs = urllib.parse.urlencode(query) if query else ""
    url = (f"http://127.0.0.1:{port}{path}?{qs}" if qs
           else f"http://127.0.0.1:{port}{path}")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if agent:
        req.add_header("X-Mind-Agent", agent)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _read_jsonl(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _guard_path(project_root: Path) -> Path:
    return project_root / "world" / "guardrails.jsonl"


def _rb_path(project_root: Path) -> Path:
    return project_root / "world" / "reasoning-bank.jsonl"


# A rule long and distinctive enough that its only high-similarity neighbour
# is its own twin — conftest's seeded guardrails share no vocabulary with it.
_DISTINCTIVE_RULE = (
    "before repointing the frobnicator manifold always quiesce the flux "
    "capacitor spool and verify the chronosynclastic infundibulum drained"
)


def _guard_rec(**kw) -> dict:
    base = {
        "rule": _DISTINCTIVE_RULE,
        "category": "test-guard",
        "trigger_condition": "before any manifold repoint",
        "source": "g-115-6948-test",
        "tags": ["dupe-refuse-test"],
    }
    base.update(kw)
    return base


def _append(port, store, rec):
    return _post(port, "/v1/store/append", {"store": store},
                 json.dumps(rec).encode("utf-8"))


def test_verbatim_guardrail_twin_refused_409_with_evidence(running_daemon):
    project_root, port = running_daemon
    status, body = _append(port, "guardrails", _guard_rec())
    assert status == 200, f"seed add failed: {body}"
    seed_id = json.loads(body)["record"]["id"]
    before = len(_read_jsonl(_guard_path(project_root)))

    status, body = _append(port, "guardrails", _guard_rec())
    assert status == 409, f"twin landed instead of refusing: {body}"
    resp = json.loads(body)
    # guard-1661: a governed-store refusal must carry caller-verifiable
    # evidence, not a bare status.
    assert resp["error"] == "near_duplicate"
    assert resp["nearest_id"] == seed_id
    assert resp["similarity"] >= 0.75
    assert resp["refuse_threshold"] == 0.75
    assert "frobnicator" in resp["existing"]
    assert "--allow-near-dup" in resp["detail"]
    assert "times_inferred_helpful" in resp["detail"]
    # Nothing stored.
    assert len(_read_jsonl(_guard_path(project_root))) == before


def test_refusal_increments_the_existing_entry(running_daemon):
    """The strengthen event: a refused twin is usefulness evidence for the
    entry it restates. Same counter as the board findings-citation lane.

    TWO valid durable surfaces (g-358 counter-spool cutover): on a box with
    UTILIZATION_COUNTERS_SPOOLED set — which the spawned test daemon inherits
    from the host env — the increment appends to the world-local counter
    spool instead of read-modify-writing the record. Either surface proves
    the event was recorded; asserting the record alone fails exactly on
    cutover boxes."""
    project_root, port = running_daemon
    status, body = _append(port, "guardrails", _guard_rec())
    assert status == 200
    seed_id = json.loads(body)["record"]["id"]

    status, _ = _append(port, "guardrails", _guard_rec())
    assert status == 409

    on_disk = next(r for r in _read_jsonl(_guard_path(project_root))
                   if r["id"] == seed_id)
    on_record = on_disk["utilization"]["times_inferred_helpful"] == 1
    spool = project_root / "world" / "guardrails-utilization.spool.jsonl"
    in_spool = (spool.exists()
                and seed_id in spool.read_text(encoding="utf-8")
                and "times_inferred_helpful" in
                spool.read_text(encoding="utf-8"))
    assert on_record or in_spool, (
        f"strengthen increment recorded on neither surface: "
        f"record counter={on_disk['utilization']['times_inferred_helpful']}, "
        f"spool exists={spool.exists()}")


def test_allow_near_dup_bypasses_and_is_never_stored(running_daemon):
    project_root, port = running_daemon
    status, _ = _append(port, "guardrails", _guard_rec())
    assert status == 200

    rec = _guard_rec()
    rec["allow_near_dup"] = True
    status, body = _append(port, "guardrails", rec)
    assert status == 200, f"override did not bypass: {body}"
    new_id = json.loads(body)["record"]["id"]

    on_disk = next(r for r in _read_jsonl(_guard_path(project_root))
                   if r["id"] == new_id)
    assert "allow_near_dup" not in on_disk, \
        "transport flag leaked into the stored record"


def test_distinct_record_lands_normally(running_daemon):
    _, port = running_daemon
    status, _ = _append(port, "guardrails", _guard_rec())
    assert status == 200
    status, body = _append(port, "guardrails", _guard_rec(
        rule="never trust a single signal when concluding a store is empty"))
    assert status == 200, f"distinct record wrongly refused: {body}"


def test_rb_title_twin_refused(running_daemon):
    """The reasoning-bank lane keys on title (store_dupe_warn STORES config)."""
    _, port = running_daemon
    rb = {
        "title": "Quiesce the frobnicator flux spool before manifold repoint",
        "type": "failure",
        "category": "test-cat",
        "content": "distinctive body one",
        "applies_to": "framework",
    }
    status, body = _append(port, "reasoning-bank", rb)
    assert status == 200, f"rb seed failed: {body}"

    rb2 = dict(rb, content="a different body — title is the signal field")
    status, body = _append(port, "reasoning-bank", rb2)
    assert status == 409, f"rb title twin landed: {body}"
    assert json.loads(body)["error"] == "near_duplicate"


def test_sub_threshold_similarity_is_not_blocked(running_daemon):
    """The advisory band (warn threshold .. refuse threshold) must keep
    landing — only the twin cluster is refused. Shares a few tokens with the
    seed (jaccard well under 0.75) but is a different rule."""
    _, port = running_daemon
    status, _ = _append(port, "guardrails", _guard_rec())
    assert status == 200
    status, body = _append(port, "guardrails", _guard_rec(
        rule="always verify the flux capacitor spool pressure gauge twice "
             "after any maintenance window closes on the assembly line"))
    assert status == 200, f"sub-threshold record wrongly refused: {body}"
