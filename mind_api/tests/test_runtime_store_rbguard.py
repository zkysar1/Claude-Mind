"""POST /v1/store/{append,set-field,increment} for store=reasoning-bank and
store=guardrails — H2 Wave 2.

The generic store endpoint (endpoints/store.py, parameterized by
store_registry.STORE_REGISTRY['reasoning-bank'] and ['guardrails']) must
reproduce reasoning-bank.py rb_add / rb_update_field / rb_increment and
guard_add / guard_update_field / guard_increment semantics exactly, over
the same daemon write machinery (file_locks + history + changelog + cache
invalidate). Both stores are world-rooted.

Mirrors test_runtime_store_journal.py structure.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest


def _post(port: int, path: str, query: dict = None, body: bytes = b"",
          *, agent: str = "alpha"):
    qs = urllib.parse.urlencode(query) if query else ""
    url = (f"http://127.0.0.1:{port}{path}?{qs}" if qs
           else f"http://127.0.0.1:{port}{path}")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if agent:
        req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


def _post_err(port: int, path: str, query: dict = None, body: bytes = b"",
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


def _rb_path(project_root: Path) -> Path:
    return project_root / "world" / "reasoning-bank.jsonl"


def _guard_path(project_root: Path) -> Path:
    return project_root / "world" / "guardrails.jsonl"


# Valid rb record (conftest seeds are legacy type=insight — new records must
# use RB_VALID_TYPES: success, failure, user_provided).
def _rb_rec(**kw) -> dict:
    base = {
        "title": "Test RB entry",
        "type": "success",
        "category": "test-cat",
        "content": "A test reasoning-bank entry.",
        "applies_to": "framework",
        "tags": ["wave2"],
    }
    base.update(kw)
    return base


def _guard_rec(**kw) -> dict:
    base = {
        "rule": "always test before deploy",
        "category": "test-guard",
        "trigger_condition": "before any deploy",
        "source": "wave-2-test",
        "when_to_use": "always",
        "tags": ["wave2"],
    }
    base.update(kw)
    return base


# ===========================================================================
# Reasoning-bank: store/append (== reasoning-bank.py rb_add)
# ===========================================================================

def test_rb_append_creates_record(running_daemon):
    project_root, port = running_daemon
    live = _rb_path(project_root)
    before = len(_read_jsonl(live))  # conftest seeds rb-001..003

    status, body = _post(port, "/v1/store/append",
                         {"store": "reasoning-bank"},
                         json.dumps(_rb_rec()).encode("utf-8"))
    assert status == 200
    resp = json.loads(body)
    assert resp["ok"] is True
    assert resp["record"]["title"] == "Test RB entry"
    # created field script-stamped
    assert "created" in resp["record"]

    after = _read_jsonl(live)
    assert len(after) == before + 1


def test_rb_append_auto_allocates_id(running_daemon):
    """No id in body -> allocator picks rb-{max+1}.
    Conftest seeds rb-001..003 -> next is rb-4 (next_id_for_prefix uses
    unpadded numeric IDs)."""
    _, port = running_daemon
    status, body = _post(port, "/v1/store/append",
                         {"store": "reasoning-bank"},
                         json.dumps(_rb_rec()).encode("utf-8"))
    assert status == 200
    rec = json.loads(body)["record"]
    assert rec["id"] == "rb-4"


def test_rb_append_rejects_duplicate_id(running_daemon):
    _, port = running_daemon
    status, body = _post_err(port, "/v1/store/append",
                             {"store": "reasoning-bank"},
                             json.dumps(_rb_rec(id="rb-001")).encode("utf-8"))
    assert status == 409
    assert "duplicate_id" in body


def test_rb_append_rejects_missing_required_fields(running_daemon):
    _, port = running_daemon
    bad = {"title": "Incomplete"}  # missing type, category, content, applies_to
    status, body = _post_err(port, "/v1/store/append",
                             {"store": "reasoning-bank"},
                             json.dumps(bad).encode("utf-8"))
    assert status == 400
    assert "validation_failed" in body


def test_rb_append_rejects_bad_type(running_daemon):
    _, port = running_daemon
    bad = _rb_rec(type="bogus")
    status, body = _post_err(port, "/v1/store/append",
                             {"store": "reasoning-bank"},
                             json.dumps(bad).encode("utf-8"))
    assert status == 400
    assert "validation_failed" in body


def test_rb_append_defaults_applied(running_daemon):
    """Static defaults (status, utilization, etc.) filled when absent."""
    _, port = running_daemon
    status, body = _post(port, "/v1/store/append",
                         {"store": "reasoning-bank"},
                         json.dumps(_rb_rec()).encode("utf-8"))
    assert status == 200
    rec = json.loads(body)["record"]
    assert rec["status"] == "active"
    assert "utilization" in rec
    assert rec["utilization"]["times_helpful"] == 0


def test_rb_append_history_and_changelog(running_daemon):
    # : history.snapshot delegates to _fileops.save_history, whose
    # Stage-2 authoritative store is the CAS-delta layout — assert a manifest
    # lands under .history/snapshots/<rel>/ instead of the legacy per-file
    # uncompressed tree (which is no longer written by default).
    project_root, port = running_daemon
    manifest_dir = (project_root / "world" / ".history" / "snapshots"
                    / "reasoning-bank.jsonl")
    legacy_dir = project_root / "world" / ".history" / "reasoning-bank.jsonl"
    cl = project_root / "world" / "changelog.jsonl"
    assert not manifest_dir.exists()

    _post(port, "/v1/store/append", {"store": "reasoning-bank"},
          json.dumps(_rb_rec()).encode("utf-8"))

    assert manifest_dir.exists()
    manifests = [p for p in manifest_dir.iterdir() if p.suffix == ".yaml"]
    assert len(manifests) == 1
    assert manifests[0].name.endswith("_alpha.yaml")
    # The legacy uncompressed tree must NOT be re-created (the 13.9G/4days
    # growth shape the  unification killed).
    assert not legacy_dir.exists()
    entries = _read_jsonl(cl)
    assert any("store-append reasoning-bank" in (e.get("summary", "") or "")
               for e in entries)


# ===========================================================================
# Reasoning-bank: store/set-field (== reasoning-bank.py rb_update_field)
# ===========================================================================

def test_rb_set_field_updates_value(running_daemon):
    # Conftest seeds have legacy type=insight — append a valid record first.
    project_root, port = running_daemon
    _post(port, "/v1/store/append", {"store": "reasoning-bank"},
          json.dumps(_rb_rec(id="rb-100")).encode("utf-8"))

    status, body = _post(port, "/v1/store/set-field",
                         {"store": "reasoning-bank",
                          "id": "rb-100", "field": "status",
                          "value": "retired"})
    assert status == 200
    rec = json.loads(body)["record"]
    assert rec["status"] == "retired"

    on_disk = next(r for r in _read_jsonl(_rb_path(project_root))
                   if r["id"] == "rb-100")
    assert on_disk["status"] == "retired"


def test_rb_set_field_immutable_created(running_daemon):
    # Immutable check fires before validation — safe on seed records.
    _, port = running_daemon
    status, body = _post_err(port, "/v1/store/set-field",
                             {"store": "reasoning-bank",
                              "id": "rb-001", "field": "created",
                              "value": "2099-01-01T00:00:00"})
    assert status == 400
    assert "immutable_field" in body


def test_rb_set_field_immutable_title(running_daemon):
    """title is HALF the merge identity (_rb_identity = created + title).

    Editing it in place changes what the record IS, so the next cross-box merge
    reads the edited copy as a new entity and keeps BOTH (g-115-8396). `created`
    was protected here since day one; the human-authored half was not.
    """
    _, port = running_daemon
    status, body = _post_err(port, "/v1/store/set-field",
                             {"store": "reasoning-bank",
                              "id": "rb-001", "field": "title",
                              "value": "a materially different title"})
    assert status == 400
    assert "immutable_field" in body


def test_rb_set_field_rejects_dotted(running_daemon):
    # Dotted check fires before validation — safe on seed records.
    _, port = running_daemon
    status, body = _post_err(port, "/v1/store/set-field",
                             {"store": "reasoning-bank",
                              "id": "rb-001",
                              "field": "utilization.times_helpful",
                              "value": "5"})
    assert status == 400
    assert "dotted_field_rejected" in body


def test_rb_set_field_not_found(running_daemon):
    _, port = running_daemon
    status, _ = _post_err(port, "/v1/store/set-field",
                          {"store": "reasoning-bank",
                           "id": "rb-999", "field": "status",
                           "value": "retired"})
    assert status == 404


def test_rb_set_field_recomputes_on_utilization(running_daemon):
    """Writing the full utilization dict triggers recompute of
    utilization_score (same as reasoning-bank.py rb_update_field)."""
    # Append a valid record first (seed type=insight fails validation).
    _, port = running_daemon
    _post(port, "/v1/store/append", {"store": "reasoning-bank"},
          json.dumps(_rb_rec(id="rb-200")).encode("utf-8"))

    util = json.dumps({
        "retrieval_count": 10,
        "last_retrieved": None,
        "times_helpful": 5,
        "times_inferred_helpful": 2,
        "times_cited": 0,
        "times_noise": 0,
        "times_active": 0,
        "times_skipped": 0,
        "times_inferred_unknown": 0,
        "utilization_score": 0.0,  # should be recomputed
    })
    status, body = _post(port, "/v1/store/set-field",
                         {"store": "reasoning-bank",
                          "id": "rb-200", "field": "utilization",
                          "value": util})
    assert status == 200
    rec = json.loads(body)["record"]
    #  smoothed formula: (5 + 0.5*2) / (max(10, 5+2) + 1) = 6/11 = 0.5455
    assert rec["utilization"]["utilization_score"] == pytest.approx(0.5455)


# ===========================================================================
# Reasoning-bank: store/increment (== reasoning-bank.py rb_increment)
# ===========================================================================

def test_rb_increment_counter(running_daemon, monkeypatch):
    # LANE PIN (2026-08-20): this test asserts the LEGACY embedded-counter RMW.
    # Post  the session env of any flipped box carries
    # UTILIZATION_COUNTERS_SPOOLED=1, which leaks into the in-process test
    # daemon and routes the increment to the spool — freezing the embedded
    # counter and failing this test only on flipped boxes (env-dependence,
    # not portability). Pin the legacy lane explicitly; the spool lane has
    # its own tests (test_utilization_spool.py + *_spooled_surface twins).
    monkeypatch.delenv("UTILIZATION_COUNTERS_SPOOLED", raising=False)
    project_root, port = running_daemon
    # rb-002 has times_helpful=3, retrieval_count=5 → score 3/5=0.6
    status, body = _post(port, "/v1/store/increment",
                         {"store": "reasoning-bank",
                          "id": "rb-002",
                          "field": "utilization.times_helpful"})
    assert status == 200
    rec = json.loads(body)["record"]
    assert rec["utilization"]["times_helpful"] == 4
    #  smoothed formula: (4 + 0.5*0) / (max(5, 4) + 1) = 4/6 = 0.6667
    assert rec["utilization"]["utilization_score"] == pytest.approx(0.6667)

    on_disk = next(r for r in _read_jsonl(_rb_path(project_root))
                   if r["id"] == "rb-002")
    assert on_disk["utilization"]["times_helpful"] == 4


def test_rb_increment_invalid_prefix(running_daemon):
    _, port = running_daemon
    status, body = _post_err(port, "/v1/store/increment",
                             {"store": "reasoning-bank",
                              "id": "rb-002",
                              "field": "status"})
    assert status == 400
    assert "invalid_field" in body


def test_rb_increment_invalid_counter(running_daemon):
    _, port = running_daemon
    status, body = _post_err(port, "/v1/store/increment",
                             {"store": "reasoning-bank",
                              "id": "rb-002",
                              "field": "utilization.bogus_counter"})
    assert status == 400
    assert "invalid_counter" in body


def test_rb_increment_not_found(running_daemon, monkeypatch):
    # LANE PIN (2026-08-20): the 404 is LEGACY-lane behavior. The spool lane
    # returns 200 on an unknown id BY DOCUMENTED DESIGN (store.py: the spool
    # path never reads the store, so it cannot 404; an orphan sidecar entry is
    # inert). Same env-leak class as the counter tests above.
    monkeypatch.delenv("UTILIZATION_COUNTERS_SPOOLED", raising=False)
    _, port = running_daemon
    status, _ = _post_err(port, "/v1/store/increment",
                          {"store": "reasoning-bank",
                           "id": "rb-999",
                           "field": "utilization.times_helpful"})
    assert status == 404


# ===========================================================================
# Guardrails: store/append (== reasoning-bank.py guard_add)
# ===========================================================================

def test_guard_append_creates_record(running_daemon):
    project_root, port = running_daemon
    live = _guard_path(project_root)
    before = len(_read_jsonl(live))  # conftest seeds guard-001,002,099

    status, body = _post(port, "/v1/store/append",
                         {"store": "guardrails"},
                         json.dumps(_guard_rec()).encode("utf-8"))
    assert status == 200
    resp = json.loads(body)
    assert resp["ok"] is True
    assert resp["record"]["rule"] == "always test before deploy"
    assert "created" in resp["record"]

    after = _read_jsonl(live)
    assert len(after) == before + 1


def test_guard_append_auto_allocates_id(running_daemon):
    """No id -> guard-{max+1}. Conftest seeds 001,002,099 -> guard-100."""
    _, port = running_daemon
    status, body = _post(port, "/v1/store/append",
                         {"store": "guardrails"},
                         json.dumps(_guard_rec()).encode("utf-8"))
    assert status == 200
    rec = json.loads(body)["record"]
    assert rec["id"] == "guard-100"


def test_guard_append_rejects_duplicate_id(running_daemon):
    _, port = running_daemon
    status, body = _post_err(port, "/v1/store/append",
                             {"store": "guardrails"},
                             json.dumps(_guard_rec(id="guard-001")).encode("utf-8"))
    assert status == 409
    assert "duplicate_id" in body


def test_guard_append_rejects_missing_required(running_daemon):
    _, port = running_daemon
    bad = {"rule": "no category"}  # missing category, trigger_condition, source
    status, body = _post_err(port, "/v1/store/append",
                             {"store": "guardrails"},
                             json.dumps(bad).encode("utf-8"))
    assert status == 400
    assert "validation_failed" in body


def test_guard_append_rejects_unknown_fields(running_daemon):
    _, port = running_daemon
    bad = _guard_rec(completely_unknown_field="surprise")
    status, body = _post_err(port, "/v1/store/append",
                             {"store": "guardrails"},
                             json.dumps(bad).encode("utf-8"))
    assert status == 400
    assert "validation_failed" in body


def test_guard_append_defaults_applied(running_daemon):
    _, port = running_daemon
    status, body = _post(port, "/v1/store/append",
                         {"store": "guardrails"},
                         json.dumps(_guard_rec()).encode("utf-8"))
    assert status == 200
    rec = json.loads(body)["record"]
    assert rec["status"] == "active"
    assert "utilization" in rec


def test_guard_append_history_and_changelog(running_daemon):
    # : CAS-delta manifest shape, not the legacy per-file tree —
    # see test_rb_append_history_and_changelog for the full rationale.
    project_root, port = running_daemon
    manifest_dir = (project_root / "world" / ".history" / "snapshots"
                    / "guardrails.jsonl")
    legacy_dir = project_root / "world" / ".history" / "guardrails.jsonl"
    cl = project_root / "world" / "changelog.jsonl"
    assert not manifest_dir.exists()

    _post(port, "/v1/store/append", {"store": "guardrails"},
          json.dumps(_guard_rec()).encode("utf-8"))

    assert manifest_dir.exists()
    manifests = [p for p in manifest_dir.iterdir() if p.suffix == ".yaml"]
    assert len(manifests) == 1
    assert manifests[0].name.endswith("_alpha.yaml")
    assert not legacy_dir.exists()
    entries = _read_jsonl(cl)
    assert any("store-append guardrails" in (e.get("summary", "") or "")
               for e in entries)


# ===========================================================================
# Guardrails: store/set-field (== reasoning-bank.py guard_update_field)
# ===========================================================================

def test_guard_set_field_updates_value(running_daemon):
    # Conftest seeds lack trigger_condition/source — append a valid record first.
    project_root, port = running_daemon
    _post(port, "/v1/store/append", {"store": "guardrails"},
          json.dumps(_guard_rec(id="guard-100")).encode("utf-8"))

    status, body = _post(port, "/v1/store/set-field",
                         {"store": "guardrails",
                          "id": "guard-100", "field": "status",
                          "value": "retired"})
    assert status == 200
    rec = json.loads(body)["record"]
    assert rec["status"] == "retired"

    on_disk = next(r for r in _read_jsonl(_guard_path(project_root))
                   if r["id"] == "guard-100")
    assert on_disk["status"] == "retired"


def test_guard_set_field_immutable_created(running_daemon):
    _, port = running_daemon
    status, body = _post_err(port, "/v1/store/set-field",
                             {"store": "guardrails",
                              "id": "guard-001", "field": "created",
                              "value": "2099-01-01T00:00:00"})
    assert status == 400
    assert "immutable_field" in body


def test_guard_set_field_immutable_rule(running_daemon):
    """rule is HALF the merge identity (_guard_identity = created + rule).

    NOT hypothetical: rb-5511 measured 11 forked guardrail pairs in the live
    store from in-place `rule` edits. coordination_merge's own amendment tier
    deliberately exposes trigger_condition / action_hint / source instead, so
    protecting `rule` ALIGNS the write path with the merge design rather than
    removing a capability (g-115-8396).
    """
    _, port = running_daemon
    status, body = _post_err(port, "/v1/store/set-field",
                             {"store": "guardrails",
                              "id": "guard-001", "field": "rule",
                              "value": "a materially different rule"})
    assert status == 400
    assert "immutable_field" in body


def test_guard_set_field_rejects_dotted(running_daemon):
    _, port = running_daemon
    status, body = _post_err(port, "/v1/store/set-field",
                             {"store": "guardrails",
                              "id": "guard-001",
                              "field": "utilization.times_helpful",
                              "value": "5"})
    assert status == 400
    assert "dotted_field_rejected" in body


def test_guard_set_field_not_found(running_daemon):
    _, port = running_daemon
    status, _ = _post_err(port, "/v1/store/set-field",
                          {"store": "guardrails",
                           "id": "guard-999", "field": "status",
                           "value": "retired"})
    assert status == 404


# ===========================================================================
# Guardrails: store/increment (== reasoning-bank.py guard_increment)
# ===========================================================================

def test_guard_increment_counter(running_daemon, monkeypatch):
    # Append a valid record first (seeds lack trigger_condition/source).
    # LANE PIN (2026-08-20): this test asserts the LEGACY embedded-counter RMW.
    # Post  the session env of any flipped box carries
    # UTILIZATION_COUNTERS_SPOOLED=1, which leaks into the in-process test
    # daemon and routes the increment to the spool — freezing the embedded
    # counter and failing this test only on flipped boxes (env-dependence,
    # not portability). Pin the legacy lane explicitly; the spool lane has
    # its own tests (test_utilization_spool.py + *_spooled_surface twins).
    monkeypatch.delenv("UTILIZATION_COUNTERS_SPOOLED", raising=False)
    project_root, port = running_daemon
    _post(port, "/v1/store/append", {"store": "guardrails"},
          json.dumps(_guard_rec(id="guard-200")).encode("utf-8"))

    status, body = _post(port, "/v1/store/increment",
                         {"store": "guardrails",
                          "id": "guard-200",
                          "field": "utilization.times_helpful"})
    assert status == 200
    rec = json.loads(body)["record"]
    assert rec["utilization"]["times_helpful"] == 1
    #  smoothed formula: (1 + 0.5*0) / (max(0, 1) + 1) = 1/2 = 0.5
    assert rec["utilization"]["utilization_score"] == pytest.approx(0.5)

    on_disk = next(r for r in _read_jsonl(_guard_path(project_root))
                   if r["id"] == "guard-200")
    assert on_disk["utilization"]["times_helpful"] == 1


def test_guard_increment_invalid_prefix(running_daemon):
    _, port = running_daemon
    status, body = _post_err(port, "/v1/store/increment",
                             {"store": "guardrails",
                              "id": "guard-001",
                              "field": "status"})
    assert status == 400
    assert "invalid_field" in body


def test_guard_increment_not_found(running_daemon, monkeypatch):
    # LANE PIN (2026-08-20): the 404 is LEGACY-lane behavior. The spool lane
    # returns 200 on an unknown id BY DOCUMENTED DESIGN (store.py: the spool
    # path never reads the store, so it cannot 404; an orphan sidecar entry is
    # inert). Same env-leak class as the counter tests above.
    monkeypatch.delenv("UTILIZATION_COUNTERS_SPOOLED", raising=False)
    _, port = running_daemon
    status, _ = _post_err(port, "/v1/store/increment",
                          {"store": "guardrails",
                           "id": "guard-999",
                           "field": "utilization.times_helpful"})
    assert status == 404
