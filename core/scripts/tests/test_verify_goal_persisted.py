"""Unit tests for aspirations_write._verify_goal_persisted (8).

The daemon add_goal path on own-cloud can return a success-shaped 200 while
NOTHING persisted to the authoritative store (the silent no-conflict write-loss
specimen observed on cc-03: HTTP 200, id g-115-2083 in the body, zero conflict
logged, no trace on S3). `_verify_goal_persisted` is the never-success-without-
persistence invariant: a raw-S3 read-back that makes that silent loss LOUD by
returning False (→ the endpoint returns a 500 instead of a false 200).

The helper is deliberately CONSERVATIVE: it returns False ONLY when a clean raw
read of the authoritative store shows the aspiration PRESENT but the goal ABSENT
(the specimen's exact signature). Every other path — local backend, unreadable
S3, aspiration absent from the raw read, malformed content, backend-init failure
— fails OPEN (returns True). So the invariant can only ADD an error return on a
genuine loss; it can never turn a real fleet-wide success into a false failure.

These tests exercise each branch with a fake backend that mimics
OwnCloudBackend's `.s3.get_object(Bucket, Key) -> {"Body": <readable>}`,
`.bucket`, and `._s3_key(path)` surface (mirrors the g-115-2188
test_team_state_authoritative.py fake-backend shape). No daemon, no real S3.
Run under STORAGE_BACKEND=local (guard-955) — no live backend is touched.
"""
from __future__ import annotations

import sys
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parent.parent  # core/scripts
PROJECT_ROOT = CORE_SCRIPTS.parent.parent  # repo root
for _p in (str(CORE_SCRIPTS), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import json  # noqa: E402

import pytest  # noqa: E402

from mind_api.src.endpoints import aspirations_write as aw  # noqa: E402

LIVE_PATH = Path("/opt/ayoai-mind/.mind-data/world/aspirations.jsonl")
ASP_ID = "asp-115"
GOAL_ID = "g-115-2208-test"


# ── Fake backend surface (own-cloud shape) ────────────────────────────────
class _FakeBody:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data


class _FakeS3:
    def __init__(self, raw: bytes | None = None, raise_exc: Exception | None = None):
        self._raw = raw
        self._raise = raise_exc

    def get_object(self, Bucket, Key):  # noqa: N803 ( kwarg names)
        if self._raise is not None:
            raise self._raise
        return {"Body": _FakeBody(self._raw or b"")}


class _FakeOwnCloudBackend:
    """Exposes the exact trio the helper feature-detects: s3, bucket, _s3_key."""

    def __init__(self, raw: bytes | None = None, raise_exc: Exception | None = None):
        self.bucket = "test-bucket"
        self.s3 = _FakeS3(raw=raw, raise_exc=raise_exc)

    def _s3_key(self, path: str) -> str:
        return "prefix/" + str(path).rsplit("/", 1)[-1]

    def refresh(self, path) -> None:
        """No-op read-freshness hook (endpoint _read_jsonl calls this)."""


class _FakeLocalBackend:
    """No s3/bucket/_s3_key attrs — mimics LocalBackend (local write IS truth)."""

    def refresh(self, path) -> None:
        """No-op read-freshness hook (endpoint _read_jsonl calls this)."""


def _jsonl(*records: dict) -> bytes:
    return ("\n".join(json.dumps(r) for r in records) + "\n").encode("utf-8")


def _patch_backend(monkeypatch, backend):
    monkeypatch.setattr(aw, "get_backend", lambda: backend)


# ── The single False case: the specimen (asp present, goal absent) ─────────
def test_owncloud_asp_present_goal_absent_returns_false(monkeypatch):
    """THE SPECIMEN: raw S3 has the aspiration but NOT the goal → loss detected.

    This is the only signature that returns False. It is exactly the cc-03
    silent write-loss shape: the add-goal 'succeeded' but the goal is absent
    from the authoritative store.
    """
    raw = _jsonl(
        {"id": ASP_ID, "goals": [{"id": "g-115-0001"}, {"id": "g-115-0002"}]},
        {"id": "asp-001", "goals": [{"id": "g-001-0001"}]},
    )
    _patch_backend(monkeypatch, _FakeOwnCloudBackend(raw=raw))
    assert aw._verify_goal_persisted(LIVE_PATH, ASP_ID, GOAL_ID) is False


# ── Fail-open cases: every one must return True ────────────────────────────
def test_owncloud_goal_present_returns_true(monkeypatch):
    """Happy path: the goal IS in the raw S3 read → persisted → True."""
    raw = _jsonl(
        {"id": ASP_ID, "goals": [{"id": "g-115-0001"}, {"id": GOAL_ID}]},
    )
    _patch_backend(monkeypatch, _FakeOwnCloudBackend(raw=raw))
    assert aw._verify_goal_persisted(LIVE_PATH, ASP_ID, GOAL_ID) is True


def test_local_backend_skips_returns_true(monkeypatch):
    """LocalBackend (no s3/bucket/_s3_key): local write is authoritative → True."""
    _patch_backend(monkeypatch, _FakeLocalBackend())
    assert aw._verify_goal_persisted(LIVE_PATH, ASP_ID, GOAL_ID) is True


def test_s3_get_object_raises_fails_open_true(monkeypatch):
    """Raw S3 read unavailable (get_object raises) → fail-open True.

    A transient S3 read failure must NEVER be reported as a write-loss — the
    write may well have persisted; we simply cannot confirm it right now.
    """
    _patch_backend(
        monkeypatch,
        _FakeOwnCloudBackend(raise_exc=RuntimeError("S3 GetObject transient error")),
    )
    assert aw._verify_goal_persisted(LIVE_PATH, ASP_ID, GOAL_ID) is True


def test_asp_absent_from_raw_fails_open_true(monkeypatch):
    """Aspiration itself not in the raw read → conservative fail-open True.

    Could be an eventual-consistency lag on the whole record; only asp-PRESENT
    + goal-ABSENT is a confirmed loss.
    """
    raw = _jsonl(
        {"id": "asp-999", "goals": [{"id": "g-999-0001"}]},
        {"id": "asp-001", "goals": [{"id": "g-001-0001"}]},
    )
    _patch_backend(monkeypatch, _FakeOwnCloudBackend(raw=raw))
    assert aw._verify_goal_persisted(LIVE_PATH, ASP_ID, GOAL_ID) is True


def test_malformed_raw_fails_open_true(monkeypatch):
    """Un-parseable raw content → fail-open True (never a false loss report)."""
    _patch_backend(monkeypatch, _FakeOwnCloudBackend(raw=b"not json at all\n{broken"))
    assert aw._verify_goal_persisted(LIVE_PATH, ASP_ID, GOAL_ID) is True


def test_get_backend_raises_fails_open_true(monkeypatch):
    """Backend init/resolution failure → fail-open True (helper's outer guard)."""

    def _boom():
        raise RuntimeError("backend unavailable")

    monkeypatch.setattr(aw, "get_backend", _boom)
    assert aw._verify_goal_persisted(LIVE_PATH, ASP_ID, GOAL_ID) is True


def test_empty_goals_list_returns_false(monkeypatch):
    """Aspiration present with an empty goals list → goal absent → loss (False).

    Guards the `(rec.get("goals") or [])` path: an asp that exists but carries
    no goals at all is still the specimen signature for THIS goal.
    """
    raw = _jsonl({"id": ASP_ID, "goals": []})
    _patch_backend(monkeypatch, _FakeOwnCloudBackend(raw=raw))
    assert aw._verify_goal_persisted(LIVE_PATH, ASP_ID, GOAL_ID) is False


# ═══ 6: _verify_claim_persisted (claim-path extension) ═════════════
# The add_goal guard checks goal EXISTENCE; a lost CLAIM leaves the goal
# present with claimed_by unset/stale (two cc-05 specimens 2026-07-16: claim
# returned success JSON with claimed_by set, raw read-back showed
# claimed_by=None). Same conservative fail-open contract; False only on a
# clean raw read where the claim is definitively absent.

CLAIM_AGENT = "alpha"


def test_claim_verifier_claimed_by_matches_true(monkeypatch):
    raw = _jsonl({"id": ASP_ID, "goals": [
        {"id": GOAL_ID, "claimed_by": CLAIM_AGENT}]})
    _patch_backend(monkeypatch, _FakeOwnCloudBackend(raw=raw))
    assert aw._verify_claim_persisted(LIVE_PATH, ASP_ID, GOAL_ID,
                                      CLAIM_AGENT) is True


def test_claim_verifier_specimen_claimed_by_none_false(monkeypatch):
    """THE cc-05 specimen: goal present, claimed_by never landed → loss."""
    raw = _jsonl({"id": ASP_ID, "goals": [{"id": GOAL_ID, "status": "pending"}]})
    _patch_backend(monkeypatch, _FakeOwnCloudBackend(raw=raw))
    assert aw._verify_claim_persisted(LIVE_PATH, ASP_ID, GOAL_ID,
                                      CLAIM_AGENT) is False


def test_claim_verifier_claimed_by_other_agent_false(monkeypatch):
    """Clean read shows another agent's claim → ours definitively lost."""
    raw = _jsonl({"id": ASP_ID, "goals": [
        {"id": GOAL_ID, "claimed_by": "bravo"}]})
    _patch_backend(monkeypatch, _FakeOwnCloudBackend(raw=raw))
    assert aw._verify_claim_persisted(LIVE_PATH, ASP_ID, GOAL_ID,
                                      CLAIM_AGENT) is False


def test_claim_verifier_goal_absent_false(monkeypatch):
    """A clean read that lacks the goal also lacks the claim → loss."""
    raw = _jsonl({"id": ASP_ID, "goals": [{"id": "g-115-0001"}]})
    _patch_backend(monkeypatch, _FakeOwnCloudBackend(raw=raw))
    assert aw._verify_claim_persisted(LIVE_PATH, ASP_ID, GOAL_ID,
                                      CLAIM_AGENT) is False


def test_claim_verifier_asp_absent_fails_open_true(monkeypatch):
    raw = _jsonl({"id": "asp-999", "goals": []})
    _patch_backend(monkeypatch, _FakeOwnCloudBackend(raw=raw))
    assert aw._verify_claim_persisted(LIVE_PATH, ASP_ID, GOAL_ID,
                                      CLAIM_AGENT) is True


def test_claim_verifier_local_backend_fails_open_true(monkeypatch):
    _patch_backend(monkeypatch, _FakeLocalBackend())
    assert aw._verify_claim_persisted(LIVE_PATH, ASP_ID, GOAL_ID,
                                      CLAIM_AGENT) is True


def test_claim_verifier_s3_raises_fails_open_true(monkeypatch):
    _patch_backend(monkeypatch,
                   _FakeOwnCloudBackend(raise_exc=RuntimeError("transient")))
    assert aw._verify_claim_persisted(LIVE_PATH, ASP_ID, GOAL_ID,
                                      CLAIM_AGENT) is True


# ═══ 6: claim() endpoint refuses the false 200 on resolve-away ═════
# Direct-call endpoint tests: real claim() write path against a tmp world,
# with get_backend monkeypatched to a fake own-cloud whose raw content IS the
# authoritative store state after the (simulated) resolve-away.

from types import SimpleNamespace  # noqa: E402


def _fake_ctx(tmp_path, goal_id: str, agent: str):
    world = tmp_path / "world"
    world.mkdir(parents=True, exist_ok=True)
    agent_dir = tmp_path / "agents" / agent
    agent_dir.mkdir(parents=True, exist_ok=True)
    asp = {"id": ASP_ID, "title": "t", "status": "active", "goals": [{
        "id": goal_id, "title": "Claimable", "status": "pending",
        "priority": "MEDIUM", "participants": ["agent"],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
    }]}
    (world / "aspirations.jsonl").write_text(
        json.dumps(asp) + "\n", encoding="utf-8")
    return SimpleNamespace(
        query={"id": goal_id, "agent": agent},
        headers={"x-mind-agent": agent},
        paths=SimpleNamespace(world=world, agent=agent_dir,
                              project_root=tmp_path),
    )


def _quiet_side_writers(monkeypatch):
    """history/changelog are provenance side-writers, not under test."""
    monkeypatch.setattr(aw.history, "snapshot",
                        lambda *a, **k: None, raising=False)
    monkeypatch.setattr(aw.changelog, "append",
                        lambda *a, **k: None, raising=False)


def test_claim_endpoint_refuses_on_resolve_away_loss(tmp_path, monkeypatch, capsys):
    """Simulated resolve-away: the local write succeeds but the authoritative
    store never received the claim (raw goal has NO claimed_by). claim() must
    NOT return success — 500 claim_not_persisted so the caller retries."""
    gid = "g-115-7701"
    ctx = _fake_ctx(tmp_path, gid, CLAIM_AGENT)
    _quiet_side_writers(monkeypatch)
    raw = _jsonl({"id": ASP_ID, "goals": [{"id": gid, "status": "pending"}]})
    _patch_backend(monkeypatch, _FakeOwnCloudBackend(raw=raw))
    resp = aw.claim(ctx)
    assert resp.status == 500, resp.body
    assert b"claim_not_persisted" in resp.body
    assert "WRITE-LOSS DETECTED" in capsys.readouterr().err


def test_claim_endpoint_success_when_claim_persisted(tmp_path, monkeypatch):
    """Authoritative store shows claimed_by == agent after write → success."""
    gid = "g-115-7702"
    ctx = _fake_ctx(tmp_path, gid, CLAIM_AGENT)
    _quiet_side_writers(monkeypatch)
    raw = _jsonl({"id": ASP_ID, "goals": [
        {"id": gid, "status": "pending", "claimed_by": CLAIM_AGENT}]})
    _patch_backend(monkeypatch, _FakeOwnCloudBackend(raw=raw))
    resp = aw.claim(ctx)
    assert resp.status == 200, resp.body
    body = json.loads(resp.body)
    assert body["ok"] is True
    assert body["goal"]["claimed_by"] == CLAIM_AGENT


def test_claim_endpoint_local_backend_unaffected(tmp_path, monkeypatch):
    """LocalBackend (test/dev default): verifier fails open — claim behaves
    exactly as before the guard (no false refusals)."""
    gid = "g-115-7703"
    ctx = _fake_ctx(tmp_path, gid, CLAIM_AGENT)
    _quiet_side_writers(monkeypatch)
    _patch_backend(monkeypatch, _FakeLocalBackend())
    resp = aw.claim(ctx)
    assert resp.status == 200, resp.body
    assert json.loads(resp.body)["ok"] is True


# ═══ 9: _verify_transition_persisted (critical goal transitions) ═══
# A swallowed transition PUT (release / defer / complete) leaves the goal
# PRESENT in the authoritative store with the OLD field values — the
# 1 specimen (release+defer verified against the local mirror at
# 11:53, silently never persisted, time-traveled back at the 18:21 restart
# re-sync). The transition verifier compares each just-written critical
# field's final in-memory value against the raw-S3 copy. Same conservative
# fail-open contract as the add_goal/claim siblings.


def test_transition_verifier_fields_match_true(monkeypatch):
    raw = _jsonl({"id": ASP_ID, "goals": [
        {"id": GOAL_ID, "status": "pending", "defer_reason": "waiting on X"}]})
    _patch_backend(monkeypatch, _FakeOwnCloudBackend(raw=raw))
    assert aw._verify_transition_persisted(
        LIVE_PATH, ASP_ID, GOAL_ID,
        {"defer_reason": "waiting on X"}) is True


def test_transition_verifier_stale_value_false(monkeypatch):
    """THE 1 shape: store still carries the PRE-transition state."""
    raw = _jsonl({"id": ASP_ID, "goals": [
        {"id": GOAL_ID, "status": "in-progress", "claimed_by": "echo"}]})
    _patch_backend(monkeypatch, _FakeOwnCloudBackend(raw=raw))
    # The release wrote claimed_by=absent; the store still shows echo → loss.
    assert aw._verify_transition_persisted(
        LIVE_PATH, ASP_ID, GOAL_ID,
        {"claimed_by": None, "claimed_at": None}) is False


def test_transition_verifier_none_expected_matches_absent(monkeypatch):
    """None expects absent-or-null: a cleared field verifies on both shapes."""
    raw = _jsonl({"id": ASP_ID, "goals": [
        {"id": GOAL_ID, "status": "pending", "defer_reason": None}]})
    _patch_backend(monkeypatch, _FakeOwnCloudBackend(raw=raw))
    assert aw._verify_transition_persisted(
        LIVE_PATH, ASP_ID, GOAL_ID,
        {"defer_reason": None, "claimed_by": None}) is True


def test_transition_verifier_goal_absent_false(monkeypatch):
    """Store lacks the goal entirely → it certainly lacks the transition."""
    raw = _jsonl({"id": ASP_ID, "goals": [{"id": "g-115-0001"}]})
    _patch_backend(monkeypatch, _FakeOwnCloudBackend(raw=raw))
    assert aw._verify_transition_persisted(
        LIVE_PATH, ASP_ID, GOAL_ID, {"status": "completed"}) is False


def test_transition_verifier_local_backend_fails_open_true(monkeypatch):
    _patch_backend(monkeypatch, _FakeLocalBackend())
    assert aw._verify_transition_persisted(
        LIVE_PATH, ASP_ID, GOAL_ID, {"status": "completed"}) is True


def test_transition_verifier_s3_raises_fails_open_true(monkeypatch):
    _patch_backend(monkeypatch,
                   _FakeOwnCloudBackend(raise_exc=RuntimeError("transient")))
    assert aw._verify_transition_persisted(
        LIVE_PATH, ASP_ID, GOAL_ID, {"status": "completed"}) is True


# ═══ 9: endpoint guards refuse the false 200 on swallowed PUT ═══


def _fake_ctx_claimed(tmp_path, goal_id: str, agent: str, *, goal_extra=None,
                      query=None, body: bytes = b""):
    """Like _fake_ctx but the seeded goal carries a live claim + extras, and
    query/body are caller-shaped (release/update/complete-by differ)."""
    world = tmp_path / "world"
    world.mkdir(parents=True, exist_ok=True)
    agent_dir = tmp_path / "agents" / agent
    agent_dir.mkdir(parents=True, exist_ok=True)
    meta = tmp_path / "meta"
    meta.mkdir(parents=True, exist_ok=True)
    g = {"id": goal_id, "title": "Transition target", "status": "in-progress",
         "priority": "MEDIUM", "participants": ["agent"],
         "claimed_by": agent, "claimed_at": "2026-07-16T11:45:56",
         "verification": {"outcomes": ["x"], "checks": [], "preconditions": []}}
    g.update(goal_extra or {})
    asp = {"id": ASP_ID, "title": "t", "status": "active", "goals": [g]}
    (world / "aspirations.jsonl").write_text(
        json.dumps(asp) + "\n", encoding="utf-8")
    return SimpleNamespace(
        query=query or {},
        body=body,
        headers={"x-mind-agent": agent},
        paths=SimpleNamespace(world=world, agent=agent_dir, meta=meta,
                              project_root=tmp_path, agent_name=agent),
    )


def test_release_endpoint_refuses_on_swallowed_release(tmp_path, monkeypatch,
                                                       capsys):
    """1 specimen: release write succeeds locally but the raw store
    still shows the claim → 500 release_not_persisted, never a false 200."""
    gid = "g-115-7801"
    ctx = _fake_ctx_claimed(tmp_path, gid, CLAIM_AGENT,
                            query={"id": gid, "source": "world"})
    _quiet_side_writers(monkeypatch)
    raw = _jsonl({"id": ASP_ID, "goals": [
        {"id": gid, "status": "in-progress", "claimed_by": CLAIM_AGENT,
         "claimed_at": "2026-07-16T11:45:56"}]})
    _patch_backend(monkeypatch, _FakeOwnCloudBackend(raw=raw))
    resp = aw.release(ctx)
    assert resp.status == 500, resp.body
    assert b"release_not_persisted" in resp.body
    assert "WRITE-LOSS DETECTED" in capsys.readouterr().err


def test_release_endpoint_success_when_release_persisted(tmp_path, monkeypatch):
    gid = "g-115-7802"
    ctx = _fake_ctx_claimed(tmp_path, gid, CLAIM_AGENT,
                            query={"id": gid, "source": "world"})
    _quiet_side_writers(monkeypatch)
    raw = _jsonl({"id": ASP_ID, "goals": [{"id": gid, "status": "in-progress"}]})
    _patch_backend(monkeypatch, _FakeOwnCloudBackend(raw=raw))
    resp = aw.release(ctx)
    assert resp.status == 200, resp.body
    body = json.loads(resp.body)
    assert body["ok"] is True and body["had_claim"] is True


def test_release_endpoint_local_backend_unaffected(tmp_path, monkeypatch):
    gid = "g-115-7803"
    ctx = _fake_ctx_claimed(tmp_path, gid, CLAIM_AGENT,
                            query={"id": gid, "source": "world"})
    _quiet_side_writers(monkeypatch)
    _patch_backend(monkeypatch, _FakeLocalBackend())
    resp = aw.release(ctx)
    assert resp.status == 200, resp.body


def _quiet_update_gates(monkeypatch):
    """Pre-write orchestration gates are not under test here."""
    monkeypatch.setattr(aw, "_run_update_goal_gates",
                        lambda *a, **k: (None, None, None))


def test_update_goal_refuses_on_swallowed_defer(tmp_path, monkeypatch, capsys):
    """Swallowed defer_reason PUT: store shows the goal WITHOUT the defer →
    500 update_not_persisted (the exact 11:53 g-115-2351 write)."""
    gid = "g-115-7804"
    ctx = _fake_ctx_claimed(
        tmp_path, gid, CLAIM_AGENT,
        query={"id": gid, "field": "defer_reason", "source": "world"},
        body=json.dumps("blocked on external runner push").encode("utf-8"))
    _quiet_side_writers(monkeypatch)
    _quiet_update_gates(monkeypatch)
    raw = _jsonl({"id": ASP_ID, "goals": [
        {"id": gid, "status": "in-progress", "claimed_by": CLAIM_AGENT}]})
    _patch_backend(monkeypatch, _FakeOwnCloudBackend(raw=raw))
    resp = aw.update_goal(ctx)
    assert resp.status == 500, resp.body
    assert b"update_not_persisted" in resp.body
    assert "WRITE-LOSS DETECTED" in capsys.readouterr().err


def test_update_goal_success_when_defer_persisted(tmp_path, monkeypatch):
    gid = "g-115-7805"
    defer_text = "blocked on external runner push"
    ctx = _fake_ctx_claimed(
        tmp_path, gid, CLAIM_AGENT,
        query={"id": gid, "field": "defer_reason", "source": "world"},
        body=json.dumps(defer_text).encode("utf-8"))
    _quiet_side_writers(monkeypatch)
    _quiet_update_gates(monkeypatch)
    raw = _jsonl({"id": ASP_ID, "goals": [
        {"id": gid, "status": "in-progress", "claimed_by": CLAIM_AGENT,
         "defer_reason": defer_text}]})
    _patch_backend(monkeypatch, _FakeOwnCloudBackend(raw=raw))
    resp = aw.update_goal(ctx)
    assert resp.status == 200, resp.body
    assert json.loads(resp.body)["ok"] is True


def test_update_goal_noncritical_field_skips_verifier(tmp_path, monkeypatch):
    """A non-critical field write must NOT pay the raw-S3 GET (cost contract):
    even a store that would fail the compare returns 200."""
    gid = "g-115-7806"
    ctx = _fake_ctx_claimed(
        tmp_path, gid, CLAIM_AGENT,
        query={"id": gid, "field": "notes", "source": "world"},
        body=json.dumps("cosmetic note").encode("utf-8"))
    _quiet_side_writers(monkeypatch)
    _quiet_update_gates(monkeypatch)
    raw = _jsonl({"id": "asp-999", "goals": []})  # would fail any lookup
    called = {"n": 0}

    def _counting_lookup(*a, **k):
        called["n"] += 1
        return "no-verify", None

    monkeypatch.setattr(aw, "_authoritative_goal_lookup", _counting_lookup)
    _patch_backend(monkeypatch, _FakeOwnCloudBackend(raw=raw))
    resp = aw.update_goal(ctx)
    assert resp.status == 200, resp.body
    assert called["n"] == 0, "non-critical field must not invoke the verifier"


def test_update_goal_refuses_on_swallowed_terminal_status(tmp_path, monkeypatch,
                                                          capsys):
    """Swallowed TERMINAL status PUT (2): the terminal branch extends
    the expectation with claimed_by=None (step 9 popped the claim in-lock), so
    a store still showing the pre-transition state — in-progress AND the live
    claim — must fail the verify → 500, never a false 200. Pins the branch a
    regression could silently delete (fresh-eyes finding
    echo-fec-terminal-status-guard-untested)."""
    gid = "g-115-7809"
    ctx = _fake_ctx_claimed(
        tmp_path, gid, CLAIM_AGENT,
        query={"id": gid, "field": "status", "source": "world"},
        body=json.dumps("completed").encode("utf-8"))
    _quiet_side_writers(monkeypatch)
    _quiet_update_gates(monkeypatch)
    raw = _jsonl({"id": ASP_ID, "goals": [
        {"id": gid, "status": "in-progress", "claimed_by": CLAIM_AGENT}]})
    _patch_backend(monkeypatch, _FakeOwnCloudBackend(raw=raw))
    resp = aw.update_goal(ctx)
    assert resp.status == 500, resp.body
    assert b"update_not_persisted" in resp.body
    assert "WRITE-LOSS DETECTED" in capsys.readouterr().err


def test_update_goal_refuses_on_resurrected_claim_after_terminal_status(
        tmp_path, monkeypatch, capsys):
    """THE branch-discriminating pin: status=completed DID persist but the
    claim resurrected (claimed_by still present in the store — the other half
    of the g-115-2351 time-travel). Only the terminal-branch extension
    (expected claimed_by=None) catches this; without it the expectation
    {status: completed} matches and a false 200 escapes. A regression deleting
    the branch fails THIS test (the full-revert test above still passes on the
    status mismatch alone)."""
    gid = "g-115-7811"
    ctx = _fake_ctx_claimed(
        tmp_path, gid, CLAIM_AGENT,
        query={"id": gid, "field": "status", "source": "world"},
        body=json.dumps("completed").encode("utf-8"))
    _quiet_side_writers(monkeypatch)
    _quiet_update_gates(monkeypatch)
    raw = _jsonl({"id": ASP_ID, "goals": [
        {"id": gid, "status": "completed", "claimed_by": CLAIM_AGENT}]})
    _patch_backend(monkeypatch, _FakeOwnCloudBackend(raw=raw))
    resp = aw.update_goal(ctx)
    assert resp.status == 500, resp.body
    assert b"update_not_persisted" in resp.body
    assert "WRITE-LOSS DETECTED" in capsys.readouterr().err


def test_update_goal_success_when_terminal_status_persisted(tmp_path,
                                                            monkeypatch):
    """Success twin: store shows status=completed with claimed_by ABSENT —
    the claimed_by=None expectation matches absent-or-null, so the terminal
    transition verifies → 200."""
    gid = "g-115-7810"
    ctx = _fake_ctx_claimed(
        tmp_path, gid, CLAIM_AGENT,
        query={"id": gid, "field": "status", "source": "world"},
        body=json.dumps("completed").encode("utf-8"))
    _quiet_side_writers(monkeypatch)
    _quiet_update_gates(monkeypatch)
    raw = _jsonl({"id": ASP_ID, "goals": [{"id": gid, "status": "completed"}]})
    _patch_backend(monkeypatch, _FakeOwnCloudBackend(raw=raw))
    resp = aw.update_goal(ctx)
    assert resp.status == 200, resp.body
    assert json.loads(resp.body)["ok"] is True


def test_complete_by_refuses_on_swallowed_completion(tmp_path, monkeypatch,
                                                     capsys):
    """Swallowed completion PUT: store still shows the goal in-progress →
    500 complete_not_persisted (the reversion class applied to completions)."""
    gid = "g-115-7807"
    ctx = _fake_ctx_claimed(
        tmp_path, gid, CLAIM_AGENT,
        query={"goal_id": gid, "source": "world",
               "agent_name": CLAIM_AGENT})
    _quiet_side_writers(monkeypatch)
    raw = _jsonl({"id": ASP_ID, "goals": [
        {"id": gid, "status": "in-progress", "claimed_by": CLAIM_AGENT}]})
    _patch_backend(monkeypatch, _FakeOwnCloudBackend(raw=raw))
    resp = aw.complete_by(ctx)
    assert resp.status == 500, resp.body
    assert b"complete_not_persisted" in resp.body
    assert "WRITE-LOSS DETECTED" in capsys.readouterr().err


def test_complete_by_success_when_completion_persisted(tmp_path, monkeypatch):
    gid = "g-115-7808"
    ctx = _fake_ctx_claimed(
        tmp_path, gid, CLAIM_AGENT,
        query={"goal_id": gid, "source": "world",
               "agent_name": CLAIM_AGENT})
    _quiet_side_writers(monkeypatch)
    raw = _jsonl({"id": ASP_ID, "goals": [
        {"id": gid, "status": "completed", "claimed_by": CLAIM_AGENT}]})
    # NOTE: complete_by's terminal-status cleanup pops the claim in-memory,
    # but the verifier checks only status/lastAchievedAt (the fields the
    # endpoint owns); a store copy still carrying claimed_by does not fail it.
    _patch_backend(monkeypatch, _FakeOwnCloudBackend(raw=raw))
    resp = aw.complete_by(ctx)
    assert resp.status == 200, resp.body
    assert json.loads(resp.body)["ok"] is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
