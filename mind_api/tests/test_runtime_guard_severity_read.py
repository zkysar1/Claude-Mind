"""GET /v1/guard/read?severity=<TIER> — full active records for one tier ().

Consumer: guardrail-manifest.sh, which prepends the CRITICAL always-load core
(full rule text) above the id manifest that prime/worker-loop load. The tier's
admission rule (prime-store-load-budget.md) is that the trigger zone is NOT
self-announcing, so the expand-on-demand path (--id / --category) structurally
cannot cover it — this param is the one read shape that serves it.

Contracts pinned here:
  1. Only ACTIVE records of the requested tier return, and they return WHOLE
     (untruncated rule text — the whole point vs --summary's 80-char slice).
  2. Matching folds case on BOTH sides: the query (severity=critical) and a
     non-canonical straggler record written by an unmigrated box (write-side
     normalization is pinned separately in test_guard_severity_normalization.py;
     the read must still not silently drop a straggler that predates it).
  3. `severity` satisfies the read gate: the missing-flag error names it.

Mirrors test_runtime_store_dupe_refuse.py harness (running_daemon fixture).
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


def _get(port: int, path: str, query: dict = None):
    qs = urllib.parse.urlencode(query) if query else ""
    url = (f"http://127.0.0.1:{port}{path}?{qs}" if qs
           else f"http://127.0.0.1:{port}{path}")
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


_LONG_RULE = (
    "always quiesce the widget-spool reconciler before repointing its manifold "
    "and verify the drain completed by reading the ledger back, because a "
    "half-drained spool silently reorders every downstream frobnication "
    "batch and the corruption only surfaces two sessions later"
)

# One DISTINCT rule per seed slug. The daemon's near-duplicate refusal tier
# () 409s any pair sharing a rule template past 0.75 similarity —
# the first draft of this file seeded two tiers from one template and was
# refused at 0.94, which is that gate doing its job, not a bug here.
_RULES = {
    "crit": _LONG_RULE,
    "med": ("prefer batching acme-api mutations into one idempotent envelope "
            "per cycle rather than issuing them singly"),
    "fold": ("never trust a chronosynclastic checksum computed before the "
             "infundibulum settles; recompute after settlement or the "
             "comparison is against a moving target"),
}


def _seed(port: int, severity: str, *, slug: str = "crit"):
    body = json.dumps({
        "rule": _RULES[slug],
        "category": f"sev-read-{slug}",
        "trigger_condition": "t",
        "source": "test",
        "severity": severity,
    }).encode("utf-8")
    status, text = _post(port, "/v1/store/append",
                         {"store": "guardrails"}, body)
    assert status == 200, text
    return json.loads(text)["record"]["id"]


def test_severity_read_returns_only_active_records_of_that_tier_in_full(running_daemon):
    project_root, port = running_daemon
    crit_id = _seed(port, "CRITICAL", slug="crit")
    med_id = _seed(port, "MEDIUM", slug="med")

    status, text = _get(port, "/v1/guard/read", {"severity": "CRITICAL"})
    assert status == 200, text
    recs = json.loads(text)
    ids = {r["id"] for r in recs}
    assert crit_id in ids
    assert med_id not in ids
    # Whole records, untruncated — the delta vs --summary's 80-char slice.
    rec = next(r for r in recs if r["id"] == crit_id)
    assert rec["rule"].startswith(_LONG_RULE[:120])
    assert len(rec["rule"]) > 200

    # A retired CRITICAL record is excluded: append directly (the jsonl cache
    # is mtime_ns-keyed, so a direct write is visible to the next read).
    gpath = project_root / "world" / "guardrails.jsonl"
    with gpath.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "id": "guard-90001", "rule": "retired critical rule",
            "category": "sev-read-ret", "trigger_condition": "t",
            "source": "test", "status": "retired", "severity": "CRITICAL",
        }) + "\n")
    status, text = _get(port, "/v1/guard/read", {"severity": "CRITICAL"})
    assert status == 200
    assert "guard-90001" not in {r["id"] for r in json.loads(text)}


def test_severity_match_folds_case_on_query_and_on_straggler_records(running_daemon):
    project_root, port = running_daemon
    crit_id = _seed(port, "CRITICAL", slug="fold")

    # Query-side fold: lowercase query finds the canonical-UPPER record.
    status, text = _get(port, "/v1/guard/read", {"severity": "critical"})
    assert status == 200
    assert crit_id in {r["id"] for r in json.loads(text)}

    # Record-side fold: a lowercase straggler (predating write normalization,
    # so seeded by direct file append — the endpoint would canonicalize it).
    gpath = project_root / "world" / "guardrails.jsonl"
    with gpath.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "id": "guard-90002", "rule": "straggler critical rule",
            "category": "sev-read-strag", "trigger_condition": "t",
            "source": "test", "status": "active", "severity": "critical",
        }) + "\n")
    status, text = _get(port, "/v1/guard/read", {"severity": "CRITICAL"})
    assert status == 200
    assert "guard-90002" in {r["id"] for r in json.loads(text)}


def test_missing_flag_error_names_severity(running_daemon):
    _, port = running_daemon
    status, text = _get(port, "/v1/guard/read")
    assert status != 200
    assert "severity" in text
