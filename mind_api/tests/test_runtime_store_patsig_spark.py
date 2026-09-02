"""POST /v1/store/{append,set-field} for store=pattern-signatures and
store=spark-questions — H2 Wave 3.

The generic store endpoint (endpoints/store.py, parameterized by
store_registry.STORE_REGISTRY['pattern-signatures'] and ['spark-questions'])
must reproduce pattern-signatures.py cmd_add/cmd_update_field/cmd_set_status
and spark-questions.py cmd_add/cmd_update_field/cmd_retire semantics exactly,
over the same daemon write machinery (file_locks + history + changelog +
cache invalidate).

pattern-signatures is a world store (like rb/guard).
spark-questions is a meta store (dual-type: question sq-N + candidate sq-cN).

Mirrors test_runtime_store_rbguard.py structure.
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


def _patsig_path(project_root: Path) -> Path:
    return project_root / "world" / "pattern-signatures.jsonl"


def _spark_path(project_root: Path) -> Path:
    return project_root / "meta" / "spark-questions.jsonl"


# ---------------------------------------------------------------------------
# Minimal valid records
# ---------------------------------------------------------------------------

def _patsig_rec(**kw) -> dict:
    base = {
        "name": "test pattern",
        "description": "a test pattern signature",
        "conditions": ["condition-a", "condition-b"],
        "expected_outcome": "outcome-x",
    }
    base.update(kw)
    return base


def _spark_question_rec(**kw) -> dict:
    base = {
        "type": "question",
        "text": "What happens when X?",
        "category": "surprise",
    }
    base.update(kw)
    return base


def _spark_candidate_rec(**kw) -> dict:
    base = {
        "type": "candidate",
        "text": "Candidate question about Y",
        "category": "learning",
    }
    base.update(kw)
    return base


# ===========================================================================
# Pattern-signatures: store/append (== pattern-signatures.py cmd_add)
# ===========================================================================

def test_patsig_append_creates_record(running_daemon):
    project_root, port = running_daemon
    live = _patsig_path(project_root)
    before = len(_read_jsonl(live))  # conftest seeds sig-001, sig-002

    status, body = _post(port, "/v1/store/append",
                         {"store": "pattern-signatures"},
                         json.dumps(_patsig_rec()).encode("utf-8"))
    assert status == 200
    resp = json.loads(body)
    assert resp["ok"] is True
    assert resp["record"]["name"] == "test pattern"
    # created field script-stamped
    assert "created" in resp["record"]

    after = _read_jsonl(live)
    assert len(after) == before + 1


def test_patsig_append_auto_allocates_id(running_daemon):
    """No id in body -> allocator picks sig-{max+1}.
    Conftest seeds sig-001, sig-002 -> next is sig-3."""
    _, port = running_daemon
    status, body = _post(port, "/v1/store/append",
                         {"store": "pattern-signatures"},
                         json.dumps(_patsig_rec()).encode("utf-8"))
    assert status == 200
    rec = json.loads(body)["record"]
    assert rec["id"] == "sig-3"


def test_patsig_append_caller_supplied_id(running_daemon):
    _, port = running_daemon
    status, body = _post(port, "/v1/store/append",
                         {"store": "pattern-signatures"},
                         json.dumps(_patsig_rec(id="sig-100")).encode("utf-8"))
    assert status == 200
    rec = json.loads(body)["record"]
    assert rec["id"] == "sig-100"


def test_patsig_append_rejects_duplicate_id(running_daemon):
    _, port = running_daemon
    status, body = _post_err(port, "/v1/store/append",
                             {"store": "pattern-signatures"},
                             json.dumps(_patsig_rec(id="sig-001")).encode("utf-8"))
    assert status == 409
    assert "duplicate_id" in body


def test_patsig_append_rejects_missing_required_fields(running_daemon):
    _, port = running_daemon
    bad = {"name": "Incomplete"}  # missing description, conditions, expected_outcome
    status, body = _post_err(port, "/v1/store/append",
                             {"store": "pattern-signatures"},
                             json.dumps(bad).encode("utf-8"))
    assert status == 400
    assert "validation_failed" in body


def test_patsig_append_rejects_bad_status(running_daemon):
    _, port = running_daemon
    bad = _patsig_rec(status="bogus")
    status, body = _post_err(port, "/v1/store/append",
                             {"store": "pattern-signatures"},
                             json.dumps(bad).encode("utf-8"))
    assert status == 400
    assert "validation_failed" in body


def test_patsig_append_defaults_applied(running_daemon):
    """Static defaults (status, outcome_stats, etc.) filled when absent."""
    _, port = running_daemon
    status, body = _post(port, "/v1/store/append",
                         {"store": "pattern-signatures"},
                         json.dumps(_patsig_rec()).encode("utf-8"))
    assert status == 200
    rec = json.loads(body)["record"]
    assert rec["status"] == "active"
    assert rec["validation_status"] == "unvalidated"
    assert rec["outcome_stats"]["total"] == 0
    assert rec["outcome_stats"]["confirmed"] == 0
    assert rec["outcome_stats"]["accuracy"] == 0.0
    assert rec["retrieval_cues"] == []
    assert rec["separation_markers"] == []
    assert rec["confused_with"] == []
    assert rec["last_matched"] is None


def test_patsig_append_recomputes_accuracy_never_trust_input(running_daemon):
    """Faithful to the deleted pattern-signatures.py cmd_add: normalize_record
    applied defaults THEN recompute_accuracy UNCONDITIONALLY ("never trust
    input"). Caller-supplied total/confirmed are preserved, but accuracy is
    recomputed = confirmed/total, overwriting any caller-supplied value."""
    _, port = running_daemon
    rec = _patsig_rec(outcome_stats={"total": 10, "confirmed": 7, "accuracy": 0.5})
    status, body = _post(port, "/v1/store/append",
                         {"store": "pattern-signatures"},
                         json.dumps(rec).encode("utf-8"))
    assert status == 200
    result = json.loads(body)["record"]
    assert result["outcome_stats"]["total"] == 10
    assert result["outcome_stats"]["confirmed"] == 7
    # Accuracy recomputed from confirmed/total (0.5 caller value discarded).
    assert result["outcome_stats"]["accuracy"] == pytest.approx(0.7)


def test_patsig_append_created_stamped(running_daemon):
    """created field is script-stamped (overwritten even if caller supplies)."""
    _, port = running_daemon
    rec = _patsig_rec(created="1999-01-01T00:00:00")
    status, body = _post(port, "/v1/store/append",
                         {"store": "pattern-signatures"},
                         json.dumps(rec).encode("utf-8"))
    assert status == 200
    result = json.loads(body)["record"]
    assert result["created"] != "1999-01-01T00:00:00"
    assert result["created"].startswith("20")  # current century


def test_patsig_append_history_and_changelog(running_daemon):
    project_root, port = running_daemon
    # CAS-delta store manifests () — no legacy .history/<rel>/ copies.
    hist = (project_root / "world" / ".history" / "snapshots"
            / "pattern-signatures.jsonl")
    legacy = project_root / "world" / ".history" / "pattern-signatures.jsonl"
    cl = project_root / "world" / "changelog.jsonl"
    assert not hist.exists()

    _post(port, "/v1/store/append", {"store": "pattern-signatures"},
          json.dumps(_patsig_rec()).encode("utf-8"))

    assert hist.exists()
    assert not legacy.exists()
    entries = _read_jsonl(cl)
    assert any("store-append pattern-signatures" in (e.get("summary", "") or "")
               for e in entries)


def test_patsig_replace_preserves_created_and_recomputes(running_daemon):
    """Faithful to the deleted pattern-signatures.py cmd_update: full replace
    runs normalize_record (defaults + UNCONDITIONAL recompute_accuracy) AND
    preserves the script-owned `created` from the EXISTING record
    (cmd_update did rec["created"]=existing["created"]). A caller cannot
    reset created via replace, and a caller-supplied wrong accuracy is
    discarded in favour of confirmed/total."""
    project_root, port = running_daemon
    status, body = _post(port, "/v1/store/append",
                         {"store": "pattern-signatures"},
                         json.dumps(_patsig_rec(id="sig-200")).encode("utf-8"))
    assert status == 200
    original_created = json.loads(body)["record"]["created"]
    assert original_created.startswith("20")

    replacement = _patsig_rec(
        id="sig-200",
        name="replaced pattern",
        created="1999-01-01T00:00:00",  # caller attempt to reset — must lose
        outcome_stats={"total": 4, "confirmed": 3, "accuracy": 0.99},  # wrong
    )
    status, body = _post(port, "/v1/store/replace",
                         {"store": "pattern-signatures", "id": "sig-200"},
                         json.dumps(replacement).encode("utf-8"))
    assert status == 200
    rec = json.loads(body)["record"]
    assert rec["name"] == "replaced pattern"
    # created preserved from the existing record, NOT the caller's 1999 value.
    assert rec["created"] == original_created
    # accuracy recomputed = 3/4 = 0.75 (caller's 0.99 discarded).
    assert rec["outcome_stats"]["accuracy"] == pytest.approx(0.75)

    on_disk = next(r for r in _read_jsonl(_patsig_path(project_root))
                   if r["id"] == "sig-200")
    assert on_disk["created"] == original_created
    assert on_disk["outcome_stats"]["accuracy"] == pytest.approx(0.75)


# ===========================================================================
# Pattern-signatures: store/set-field (== pattern-signatures.py cmd_update_field + cmd_set_status)
# ===========================================================================

def test_patsig_set_field_updates_value(running_daemon):
    project_root, port = running_daemon
    # Append a valid record first (seeds lack required fields).
    _post(port, "/v1/store/append", {"store": "pattern-signatures"},
          json.dumps(_patsig_rec(id="sig-100")).encode("utf-8"))

    status, body = _post(port, "/v1/store/set-field",
                         {"store": "pattern-signatures",
                          "id": "sig-100", "field": "status",
                          "value": "retired"})
    assert status == 200
    rec = json.loads(body)["record"]
    assert rec["status"] == "retired"

    on_disk = next(r for r in _read_jsonl(_patsig_path(project_root))
                   if r["id"] == "sig-100")
    assert on_disk["status"] == "retired"


def test_patsig_set_field_immutable_created(running_daemon):
    """created is in immutable_fields -> set-field rejects it."""
    _, port = running_daemon
    status, body = _post_err(port, "/v1/store/set-field",
                             {"store": "pattern-signatures",
                              "id": "sig-001", "field": "created",
                              "value": "2099-01-01T00:00:00"})
    assert status == 400
    assert "immutable_field" in body


def test_patsig_set_field_immutable_name(running_daemon):
    """name is HALF the merge identity (_sig_identity = created + name).

    Same fork risk as the rb/guardrail halves — see g-115-8396.
    """
    _, port = running_daemon
    status, body = _post_err(port, "/v1/store/set-field",
                             {"store": "pattern-signatures",
                              "id": "sig-001", "field": "name",
                              "value": "a materially different name"})
    assert status == 400
    assert "immutable_field" in body


def test_patsig_set_field_rejects_dotted(running_daemon):
    _, port = running_daemon
    status, body = _post_err(port, "/v1/store/set-field",
                             {"store": "pattern-signatures",
                              "id": "sig-001",
                              "field": "outcome_stats.total",
                              "value": "5"})
    assert status == 400
    assert "dotted_field_rejected" in body


def test_patsig_set_field_not_found(running_daemon):
    _, port = running_daemon
    status, _ = _post_err(port, "/v1/store/set-field",
                          {"store": "pattern-signatures",
                           "id": "sig-999", "field": "status",
                           "value": "retired"})
    assert status == 404


def test_patsig_set_field_recomputes_accuracy(running_daemon):
    """Writing outcome_stats triggers recompute of accuracy."""
    _, port = running_daemon
    _post(port, "/v1/store/append", {"store": "pattern-signatures"},
          json.dumps(_patsig_rec(id="sig-200")).encode("utf-8"))

    stats = json.dumps({"total": 8, "confirmed": 6, "accuracy": 0.0})
    status, body = _post(port, "/v1/store/set-field",
                         {"store": "pattern-signatures",
                          "id": "sig-200", "field": "outcome_stats",
                          "value": stats})
    assert status == 200
    rec = json.loads(body)["record"]
    # 6/8 = 0.75
    assert rec["outcome_stats"]["accuracy"] == pytest.approx(0.75)


# ===========================================================================
# Spark-questions: store/append — question type (== spark-questions.py cmd_add)
# ===========================================================================

def test_spark_question_append_creates_record(running_daemon):
    project_root, port = running_daemon
    live = _spark_path(project_root)
    before = len(_read_jsonl(live))  # conftest seeds sq-001 + sq-c01

    status, body = _post(port, "/v1/store/append",
                         {"store": "spark-questions"},
                         json.dumps(_spark_question_rec()).encode("utf-8"))
    assert status == 200
    resp = json.loads(body)
    assert resp["ok"] is True
    assert resp["record"]["type"] == "question"
    assert resp["record"]["text"] == "What happens when X?"

    after = _read_jsonl(live)
    assert len(after) == before + 1


def test_spark_question_append_auto_allocates_id(running_daemon):
    """No id -> allocator picks sq-{max+1} (zero-padded width=3).
    Conftest seeds sq-001 -> next is sq-002."""
    _, port = running_daemon
    status, body = _post(port, "/v1/store/append",
                         {"store": "spark-questions"},
                         json.dumps(_spark_question_rec()).encode("utf-8"))
    assert status == 200
    rec = json.loads(body)["record"]
    assert rec["id"] == "sq-002"


def test_spark_question_append_defaults_applied(running_daemon):
    """Question defaults: times_asked=0, sparks_generated=0, yield_rate=0.0, status=active."""
    _, port = running_daemon
    status, body = _post(port, "/v1/store/append",
                         {"store": "spark-questions"},
                         json.dumps(_spark_question_rec()).encode("utf-8"))
    assert status == 200
    rec = json.loads(body)["record"]
    assert rec["times_asked"] == 0
    assert rec["sparks_generated"] == 0
    assert rec["yield_rate"] == 0.0
    assert rec["status"] == "active"


def test_spark_question_append_rejects_missing_required(running_daemon):
    _, port = running_daemon
    bad = {"type": "question", "text": "no category"}
    status, body = _post_err(port, "/v1/store/append",
                             {"store": "spark-questions"},
                             json.dumps(bad).encode("utf-8"))
    assert status == 400
    assert "validation_failed" in body


def test_spark_question_append_rejects_duplicate_id(running_daemon):
    _, port = running_daemon
    status, body = _post_err(port, "/v1/store/append",
                             {"store": "spark-questions"},
                             json.dumps(_spark_question_rec(id="sq-001")).encode("utf-8"))
    assert status == 409
    assert "duplicate_id" in body


# ===========================================================================
# Spark-questions: store/append — candidate type (dual-type dispatch)
# ===========================================================================

def test_spark_candidate_append_creates_record(running_daemon):
    project_root, port = running_daemon
    live = _spark_path(project_root)
    before = len(_read_jsonl(live))

    status, body = _post(port, "/v1/store/append",
                         {"store": "spark-questions"},
                         json.dumps(_spark_candidate_rec()).encode("utf-8"))
    assert status == 200
    resp = json.loads(body)
    assert resp["ok"] is True
    assert resp["record"]["type"] == "candidate"

    after = _read_jsonl(live)
    assert len(after) == before + 1


def test_spark_candidate_append_auto_allocates_id(running_daemon):
    """No id -> candidate allocator picks sq-c{max+1} (zero-padded width=2).
    Conftest seeds sq-c01 -> next is sq-c02."""
    _, port = running_daemon
    status, body = _post(port, "/v1/store/append",
                         {"store": "spark-questions"},
                         json.dumps(_spark_candidate_rec()).encode("utf-8"))
    assert status == 200
    rec = json.loads(body)["record"]
    assert rec["id"] == "sq-c02"


def test_spark_candidate_append_defaults_applied(running_daemon):
    """Candidate defaults: proposed_session=0."""
    _, port = running_daemon
    status, body = _post(port, "/v1/store/append",
                         {"store": "spark-questions"},
                         json.dumps(_spark_candidate_rec()).encode("utf-8"))
    assert status == 200
    rec = json.loads(body)["record"]
    assert rec["proposed_session"] == 0


def test_spark_candidate_append_rejects_bad_id_format(running_daemon):
    """Candidate with sq-N format (question prefix) should fail validation."""
    _, port = running_daemon
    bad = _spark_candidate_rec(id="sq-099")  # wrong prefix for candidate
    status, body = _post_err(port, "/v1/store/append",
                             {"store": "spark-questions"},
                             json.dumps(bad).encode("utf-8"))
    assert status == 400
    assert "validation_failed" in body


# ===========================================================================
# Spark-questions: store/set-field (== spark-questions.py cmd_update_field + cmd_retire)
# ===========================================================================

def test_spark_set_field_updates_value(running_daemon):
    project_root, port = running_daemon
    # Append a valid record first (seeds lack category).
    _post(port, "/v1/store/append", {"store": "spark-questions"},
          json.dumps(_spark_question_rec(id="sq-100")).encode("utf-8"))

    status, body = _post(port, "/v1/store/set-field",
                         {"store": "spark-questions",
                          "id": "sq-100", "field": "status",
                          "value": "retired"})
    assert status == 200
    rec = json.loads(body)["record"]
    assert rec["status"] == "retired"

    on_disk = next(r for r in _read_jsonl(_spark_path(project_root))
                   if r["id"] == "sq-100")
    assert on_disk["status"] == "retired"


def test_spark_set_field_immutable_text(running_daemon):
    """text is the WHOLE merge identity (_spark_identity = text, alone).

    This store was the worst case of the four: it declared no immutable_fields
    at all, so nothing about a spark record was write-protected and a single
    set-field could re-key it outright (g-115-8396).
    """
    _, port = running_daemon
    status, body = _post_err(port, "/v1/store/set-field",
                             {"store": "spark-questions",
                              "id": "sq-001", "field": "text",
                              "value": "a materially different question"})
    assert status == 400
    assert "immutable_field" in body


def test_spark_set_field_rejects_dotted(running_daemon):
    _, port = running_daemon
    status, body = _post_err(port, "/v1/store/set-field",
                             {"store": "spark-questions",
                              "id": "sq-001",
                              "field": "yield_rate.subkey",
                              "value": "0"})
    assert status == 400
    assert "dotted_field_rejected" in body


def test_spark_set_field_not_found(running_daemon):
    _, port = running_daemon
    status, _ = _post_err(port, "/v1/store/set-field",
                          {"store": "spark-questions",
                           "id": "sq-999", "field": "status",
                           "value": "retired"})
    assert status == 404


def test_spark_set_field_recomputes_yield_rate_on_times_asked(running_daemon):
    """Writing times_asked triggers recompute of yield_rate."""
    _, port = running_daemon
    _post(port, "/v1/store/append", {"store": "spark-questions"},
          json.dumps(_spark_question_rec(id="sq-200")).encode("utf-8"))

    # Set sparks_generated first so yield_rate will be non-zero
    _post(port, "/v1/store/set-field",
          {"store": "spark-questions",
           "id": "sq-200", "field": "sparks_generated", "value": "3"})

    status, body = _post(port, "/v1/store/set-field",
                         {"store": "spark-questions",
                          "id": "sq-200", "field": "times_asked",
                          "value": "10"})
    assert status == 200
    rec = json.loads(body)["record"]
    # 3 / max(10, 1) = 0.3
    assert rec["yield_rate"] == pytest.approx(0.3)


def test_spark_set_field_recomputes_yield_rate_on_sparks_generated(running_daemon):
    """Writing sparks_generated triggers recompute of yield_rate."""
    _, port = running_daemon
    _post(port, "/v1/store/append", {"store": "spark-questions"},
          json.dumps(_spark_question_rec(id="sq-201")).encode("utf-8"))

    # Set times_asked first
    _post(port, "/v1/store/set-field",
          {"store": "spark-questions",
           "id": "sq-201", "field": "times_asked", "value": "8"})

    status, body = _post(port, "/v1/store/set-field",
                         {"store": "spark-questions",
                          "id": "sq-201", "field": "sparks_generated",
                          "value": "4"})
    assert status == 200
    rec = json.loads(body)["record"]
    # 4 / max(8, 1) = 0.5
    assert rec["yield_rate"] == pytest.approx(0.5)


def test_spark_retire_via_set_field(running_daemon):
    """Retire a spark question by setting status=retired (mirrors spark-questions-retire.sh)."""
    _, port = running_daemon
    _post(port, "/v1/store/append", {"store": "spark-questions"},
          json.dumps(_spark_question_rec(id="sq-300")).encode("utf-8"))

    status, body = _post(port, "/v1/store/set-field",
                         {"store": "spark-questions",
                          "id": "sq-300", "field": "status",
                          "value": "retired"})
    assert status == 200
    rec = json.loads(body)["record"]
    assert rec["status"] == "retired"
