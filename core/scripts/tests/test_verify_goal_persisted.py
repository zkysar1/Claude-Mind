"""Unit tests for aspirations_write._verify_goal_persisted ().

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


# ═══ : _verify_claim_persisted (claim-path extension) ═════════════
# The add_goal guard checks goal EXISTENCE; a lost CLAIM leaves the goal
# present with claimed_by unset/stale (two cc-05 specimens 2026-07-16: claim
# returned success JSON with claimed_by set, raw read-back showed
# claimed_by=None). Same conservative fail-open contract; False only on a
# clean raw read where the claim is definitively absent.

CLAIM_AGENT = "alpha"
# Production ALWAYS sends a sid: aspirations-claim.sh appends &sid=$MIND_SID,
# and bash-agent-inject.py injects MIND_SID into every Bash call. A sid-less
# claim() call was therefore already diverging from the production arg shape
# (guard-920) before -b made that divergence a hard 400
# missing_claim_sid. Sending one here restores the production shape rather than
# reaching for the MIND_CLAIM_ALLOW_NO_SID escape hatch, which exists for a
# genuinely un-hooked caller and would mask this shape drift instead of fixing
# it. Same fix applied to the three sibling claim suites in -b; this
# file was missed because it calls aw.claim(ctx) DIRECTLY with a fake ctx and so
# never appeared in that goal's grep of HTTP claim callers (rb-6511 — the
# enumeration UNIT must match the changed thing's unit).
CLAIM_SID = "77777777-aaaa-bbbb-cccc-777777777777"


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


# ═══ : HEAD-ETag fast path — skip the whole-store GET when S3's head
# IS this process's own last write ══════════════════════════════════════════
#
# The fake below adds the two surfaces the fast path feature-detects on top of
# the own-cloud trio: `_etags` (the fence `_put` records) and `stat()` (HEAD).
# `get_object` is COUNTED, and the raw S3 body is deliberately set to DISAGREE
# with the local mirror in the "no GET" tests — so a fast path that silently
# fell through to the GET would be caught by the body, not just the counter.

from types import SimpleNamespace as _NS  # noqa: E402


class _FakeFastPathBackend(_FakeOwnCloudBackend):
    def __init__(self, *, mirror: Path, fence, head, raw: bytes | None = None,
                 head_raises: Exception | None = None):
        super().__init__(raw=raw)
        self._mirror = mirror
        self._etags = {} if fence is None else {self._s3_key(str(mirror)): fence}
        self._head = head
        self._head_raises = head_raises
        self.get_calls = 0
        _orig = self.s3.get_object

        def _counting(Bucket, Key):  # noqa: N803
            self.get_calls += 1
            return _orig(Bucket=Bucket, Key=Key)
        self.s3.get_object = _counting

    def stat(self, path):
        if self._head_raises is not None:
            raise self._head_raises
        return None if self._head is None else _NS(version=self._head, size=1)

    def _local(self, path):
        return self._mirror


def _mirror_with(tmp_path, *goal_ids: str) -> Path:
    p = tmp_path / "aspirations.jsonl"
    p.write_bytes(_jsonl({"id": ASP_ID, "goals": [{"id": g} for g in goal_ids]}))
    return p


def test_fast_path_head_equal_answers_from_mirror_with_zero_gets(tmp_path, monkeypatch):
    """Fence == HEAD ETag and the goal is in the local mirror -> found, no GET.
    The raw S3 body LACKS the goal on purpose: had the GET run, the verdict
    would have been False."""
    mirror = _mirror_with(tmp_path, "g-115-0001", GOAL_ID)
    be = _FakeFastPathBackend(mirror=mirror, fence='"e1"', head='"e1"',
                              raw=_jsonl({"id": ASP_ID, "goals": [{"id": "g-115-0001"}]}))
    _patch_backend(monkeypatch, be)
    assert aw._verify_goal_persisted(mirror, ASP_ID, GOAL_ID) is True
    assert be.get_calls == 0


def test_fast_path_quoting_differences_still_match(tmp_path, monkeypatch):
    """S3 ETags arrive quoted; a fence recorded unquoted must still compare."""
    mirror = _mirror_with(tmp_path, GOAL_ID)
    be = _FakeFastPathBackend(mirror=mirror, fence="e1", head='"e1"',
                              raw=_jsonl({"id": ASP_ID, "goals": []}))
    _patch_backend(monkeypatch, be)
    assert aw._verify_goal_persisted(mirror, ASP_ID, GOAL_ID) is True
    assert be.get_calls == 0


def test_fast_path_head_mismatch_falls_back_to_get(tmp_path, monkeypatch):
    """A peer wrote since (HEAD != fence): only the authoritative bytes decide.
    Mirror HAS the goal, raw S3 does NOT -> the GET verdict (False) wins."""
    mirror = _mirror_with(tmp_path, GOAL_ID)
    be = _FakeFastPathBackend(mirror=mirror, fence='"e1"', head='"e2"',
                              raw=_jsonl({"id": ASP_ID, "goals": [{"id": "g-115-0001"}]}))
    _patch_backend(monkeypatch, be)
    assert aw._verify_goal_persisted(mirror, ASP_ID, GOAL_ID) is False
    assert be.get_calls == 1


def test_fast_path_head_raises_falls_back_to_get(tmp_path, monkeypatch):
    mirror = _mirror_with(tmp_path, GOAL_ID)
    be = _FakeFastPathBackend(mirror=mirror, fence='"e1"', head='"e1"',
                              head_raises=RuntimeError("HEAD transient"),
                              raw=_jsonl({"id": ASP_ID, "goals": [{"id": GOAL_ID}]}))
    _patch_backend(monkeypatch, be)
    assert aw._verify_goal_persisted(mirror, ASP_ID, GOAL_ID) is True
    assert be.get_calls == 1


def test_fast_path_head_absent_falls_back_to_get(tmp_path, monkeypatch):
    mirror = _mirror_with(tmp_path, GOAL_ID)
    be = _FakeFastPathBackend(mirror=mirror, fence='"e1"', head=None,
                              raw=_jsonl({"id": ASP_ID, "goals": [{"id": GOAL_ID}]}))
    _patch_backend(monkeypatch, be)
    assert aw._verify_goal_persisted(mirror, ASP_ID, GOAL_ID) is True
    assert be.get_calls == 1


def test_fast_path_no_fence_falls_back_to_get(tmp_path, monkeypatch):
    """Fresh daemon (empty _etags): the existing GET path runs unchanged."""
    mirror = _mirror_with(tmp_path, GOAL_ID)
    be = _FakeFastPathBackend(mirror=mirror, fence=None, head='"e1"',
                              raw=_jsonl({"id": ASP_ID, "goals": [{"id": GOAL_ID}]}))
    _patch_backend(monkeypatch, be)
    assert aw._verify_goal_persisted(mirror, ASP_ID, GOAL_ID) is True
    assert be.get_calls == 1


def test_fast_path_goal_absent_from_mirror_falls_back_to_get(tmp_path, monkeypatch):
    """ETag equal but the mirror lacks the goal: never a fast negative — the
    GET decides (here: present in S3 -> True)."""
    mirror = _mirror_with(tmp_path, "g-115-0001")
    be = _FakeFastPathBackend(mirror=mirror, fence='"e1"', head='"e1"',
                              raw=_jsonl({"id": ASP_ID, "goals": [{"id": GOAL_ID}]}))
    _patch_backend(monkeypatch, be)
    assert aw._verify_goal_persisted(mirror, ASP_ID, GOAL_ID) is True
    assert be.get_calls == 1


def test_fast_path_unreadable_mirror_falls_back_to_get(tmp_path, monkeypatch):
    mirror = tmp_path / "missing.jsonl"  # never written
    be = _FakeFastPathBackend(mirror=mirror, fence='"e1"', head='"e1"',
                              raw=_jsonl({"id": ASP_ID, "goals": [{"id": GOAL_ID}]}))
    _patch_backend(monkeypatch, be)
    assert aw._verify_goal_persisted(mirror, ASP_ID, GOAL_ID) is True
    assert be.get_calls == 1


def test_fast_path_serves_the_claim_verifier_fields(tmp_path, monkeypatch):
    """The claim verifier compares fields on the returned record; the mirror's
    record carries them, so the claim read-back also needs zero GETs."""
    mirror = tmp_path / "aspirations.jsonl"
    mirror.write_bytes(_jsonl({"id": ASP_ID, "goals": [
        {"id": GOAL_ID, "claimed_by": CLAIM_AGENT, "status": "in-progress"}]}))
    be = _FakeFastPathBackend(mirror=mirror, fence='"e1"', head='"e1"',
                              raw=_jsonl({"id": ASP_ID, "goals": [{"id": GOAL_ID}]}))
    _patch_backend(monkeypatch, be)
    assert aw._verify_claim_persisted(mirror, ASP_ID, GOAL_ID, CLAIM_AGENT) is True
    assert be.get_calls == 0


# ═══ : claim() endpoint refuses the false 200 on resolve-away ═════
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
    # Mirror the real PathSet slots (agent_name, world, meta, agent,
    # project_root). This fake carried only three of the five, so it passed
    # only while claim() happened not to read the other two — the sibling
    # builder below already carries all five. A fake missing a production slot
    # is a latent red for whichever endpoint change reads it next (guard-920).
    meta = tmp_path / "meta"
    meta.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        query={"id": goal_id, "agent": agent, "sid": CLAIM_SID},
        headers={"x-mind-agent": agent},
        paths=SimpleNamespace(world=world, agent=agent_dir, meta=meta,
                              project_root=tmp_path, agent_name=agent),
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


# ═══ : the lane-pin gate must never wedge a claim ═══
#
# The gate's own body is fail-open, but its ARGUMENTS are built before it is
# entered, so a raise while building them escapes that protection entirely and
# 500s the claim. That is not hypothetical: adding the gate turned the three
# tests above red, because this file's fake ctx.paths carried no `meta`.
# Both halves are pinned — a missing path slot, and the gate raising outright.

def test_claim_survives_a_ctx_paths_missing_a_slot(tmp_path, monkeypatch, capsys):
    """A ctx.paths without `meta` must not refuse or crash the claim — AND the
    gate must still RUN, with only its telemetry degraded.

    The status assertion alone would not distinguish the two ways to survive a
    missing slot, and they are not equally good: reading the slot defensively
    keeps the gate CLASSIFYING with telemetry off, while letting it raise and
    catching that below disables the gate ENTIRELY for the call. Both return
    200, so the absence of the WARN is what pins the better one.
    """
    gid = "g-115-7704"
    ctx = _fake_ctx(tmp_path, gid, CLAIM_AGENT)
    del ctx.paths.meta          # the exact shape that produced the regression
    _quiet_side_writers(monkeypatch)
    _patch_backend(monkeypatch, _FakeLocalBackend())
    resp = aw.claim(ctx)
    assert resp.status == 200, resp.body
    assert json.loads(resp.body)["ok"] is True
    assert "lane-pin gate raised" not in capsys.readouterr().err


def test_claim_survives_a_raising_lane_pin_gate(tmp_path, monkeypatch, capsys):
    """A gate that raises for ANY reason allows the claim, and says so.

    Loud rather than silent (guard-1977): a gate that quietly declines to run
    reports success by default, so a permanently-broken gate would be
    indistinguishable from a permanently-passing one.
    """
    gid = "g-115-7705"
    ctx = _fake_ctx(tmp_path, gid, CLAIM_AGENT)
    _quiet_side_writers(monkeypatch)
    _patch_backend(monkeypatch, _FakeLocalBackend())

    def _boom(*a, **k):
        raise RuntimeError("registry parser exploded")
    monkeypatch.setattr(aw, "_lane_pin_eval", _boom)

    resp = aw.claim(ctx)
    assert resp.status == 200, resp.body
    assert json.loads(resp.body)["ok"] is True
    err = capsys.readouterr().err
    assert "lane-pin gate raised" in err
    assert "registry parser exploded" in err


# ═══ : _verify_transition_persisted (critical goal transitions) ═══
# A swallowed transition PUT (release / defer / complete) leaves the goal
# PRESENT in the authoritative store with the OLD field values — the
#  specimen (release+defer verified against the local mirror at
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
        {"defer_reason": "waiting on X"})[0] is True


def test_transition_verifier_stale_value_false(monkeypatch):
    """THE  shape: store still carries the PRE-transition state."""
    raw = _jsonl({"id": ASP_ID, "goals": [
        {"id": GOAL_ID, "status": "in-progress", "claimed_by": "echo"}]})
    _patch_backend(monkeypatch, _FakeOwnCloudBackend(raw=raw))
    # The release wrote claimed_by=absent; the store still shows echo → loss.
    assert aw._verify_transition_persisted(
        LIVE_PATH, ASP_ID, GOAL_ID,
        {"claimed_by": None, "claimed_at": None})[0] is False


def test_transition_verifier_none_expected_matches_absent(monkeypatch):
    """None expects absent-or-null: a cleared field verifies on both shapes."""
    raw = _jsonl({"id": ASP_ID, "goals": [
        {"id": GOAL_ID, "status": "pending", "defer_reason": None}]})
    _patch_backend(monkeypatch, _FakeOwnCloudBackend(raw=raw))
    assert aw._verify_transition_persisted(
        LIVE_PATH, ASP_ID, GOAL_ID,
        {"defer_reason": None, "claimed_by": None})[0] is True


def test_transition_verifier_goal_absent_false(monkeypatch):
    """Store lacks the goal entirely → it certainly lacks the transition."""
    raw = _jsonl({"id": ASP_ID, "goals": [{"id": "g-115-0001"}]})
    _patch_backend(monkeypatch, _FakeOwnCloudBackend(raw=raw))
    assert aw._verify_transition_persisted(
        LIVE_PATH, ASP_ID, GOAL_ID, {"status": "completed"})[0] is False


def test_transition_verifier_local_backend_fails_open_true(monkeypatch):
    _patch_backend(monkeypatch, _FakeLocalBackend())
    assert aw._verify_transition_persisted(
        LIVE_PATH, ASP_ID, GOAL_ID, {"status": "completed"})[0] is True


def test_transition_verifier_s3_raises_fails_open_true(monkeypatch):
    _patch_backend(monkeypatch,
                   _FakeOwnCloudBackend(raise_exc=RuntimeError("transient")))
    assert aw._verify_transition_persisted(
        LIVE_PATH, ASP_ID, GOAL_ID, {"status": "completed"})[0] is True


# ═══ : endpoint guards refuse the false 200 on swallowed PUT ═══


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
    """ specimen: release write succeeds locally but the raw store
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
    """Swallowed TERMINAL status PUT (): the terminal branch extends
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


# ── : the RECURRING complete-by claim-clear read-back ───────────
#
# A recurring completion cycles status back to `pending`, so `status` is
# unchanged by the write and cannot witness the in-lock claim pop; and
# `lastAchievedAt` is monotonic in coordination_merge._merge_goal
# ("strictly-newer wins"), so it survives a per-field reconcile INDEPENDENTLY
# of the claim fields and cannot witness it either. Before the fix, a lost
# claim-clear on a recurring goal returned a clean 200 with the goal left
# pending AND still claimed — the residual-claim shape guard-4775 measures.


class _ReplayS3:
    """Return exactly what the endpoint just wrote locally, with `mutate`
    applied to each goal — the per-field reconcile shape (rb-3636 mechanism
    B), where most of the PUT lands and the claim pair alone survives.

    Replaying the endpoint's OWN write is what makes this test pin the right
    field: status and lastAchievedAt match by construction, so the only
    expectation that can fail is the claim pair (guard-920 — a regression
    test must fail for the reason it names, not an incidental mismatch)."""

    def __init__(self, world, mutate):
        self._world = world
        self._mutate = mutate

    def get_object(self, Bucket, Key):  # noqa: N803 ( kwarg names)
        raw = (self._world / "aspirations.jsonl").read_bytes()
        recs = [json.loads(ln) for ln in raw.decode("utf-8").splitlines()
                if ln.strip()]
        for r in recs:
            for g in (r.get("goals") or []):
                self._mutate(g)
        return {"Body": _FakeBody(_jsonl(*recs))}


def _recurring_ctx(tmp_path, gid):
    return _fake_ctx_claimed(
        tmp_path, gid, CLAIM_AGENT,
        goal_extra={"recurring": True, "interval_hours": 24},
        query={"goal_id": gid, "source": "world",
               "agent_name": CLAIM_AGENT})


def _replay_backend(monkeypatch, tmp_path, mutate):
    backend = _FakeOwnCloudBackend()
    backend.s3 = _ReplayS3(tmp_path / "world", mutate)
    _patch_backend(monkeypatch, backend)


def test_complete_by_recurring_refuses_when_claim_clear_swallowed(
        tmp_path, monkeypatch, capsys):
    """The whole point of : everything else about the completion
    persisted, ONLY the claim pair survived in the store → must be 500, not a
    false 200. status is `pending` on both sides and lastAchievedAt matches,
    so the claim pair is the sole discriminator."""
    gid = "g-115-7820"
    ctx = _recurring_ctx(tmp_path, gid)
    _quiet_side_writers(monkeypatch)
    _replay_backend(monkeypatch, tmp_path, lambda g: g.update(
        {"claimed_by": CLAIM_AGENT, "claimed_at": "2026-07-16T11:45:56"}))
    resp = aw.complete_by(ctx)
    assert resp.status == 500, resp.body
    assert b"complete_not_persisted" in resp.body
    assert "WRITE-LOSS DETECTED" in capsys.readouterr().err


def test_complete_by_recurring_success_when_claim_clear_persisted(
        tmp_path, monkeypatch):
    """Success twin — identical setup, claim pair absent in the store (the
    clear landed) → 200. Without this the test above would also pass if the
    endpoint had simply started refusing every recurring completion."""
    gid = "g-115-7821"
    ctx = _recurring_ctx(tmp_path, gid)
    _quiet_side_writers(monkeypatch)
    _replay_backend(monkeypatch, tmp_path, lambda g: None)
    resp = aw.complete_by(ctx)
    assert resp.status == 200, resp.body
    assert json.loads(resp.body)["ok"] is True


def test_complete_by_nonrecurring_unaffected_by_the_recurring_branch(
        tmp_path, monkeypatch):
    """Scope pin: the new expectation is gated on `recurring`. A NON-recurring
    completion whose store copy still carries claimed_by still returns 200 —
    its status IS terminal, and _merge_goal clears the whole claim triple on a
    merged terminal status, so `status` witnesses the clear transitively.
    Guards the documented 'shown not to need it' half of the enumeration."""
    gid = "g-115-7822"
    ctx = _fake_ctx_claimed(
        tmp_path, gid, CLAIM_AGENT,
        query={"goal_id": gid, "source": "world",
               "agent_name": CLAIM_AGENT})
    _quiet_side_writers(monkeypatch)
    _replay_backend(monkeypatch, tmp_path, lambda g: g.update(
        {"claimed_by": CLAIM_AGENT}))
    resp = aw.complete_by(ctx)
    assert resp.status == 200, resp.body
    assert json.loads(resp.body)["ok"] is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# ═══ : the verifier must NAME the field that disagreed ═══════════
#
# The predicate returned a bare `all(...)`, so all three call sites could
# report only THAT a critical transition had not persisted. Six failed closes
# of one recurring goal were measured from the outside across two days
# (achievedCount 66 -> 72, so every "failed" write had in fact LANDED) and the
# failing key could not be named from any of them.
#
# guard-3292: the diagnostic is part of the RETURN, not an optional
# out-parameter — an optional one has no failure mode at the call site, so two
# of the three sites would have stayed silently undiagnosable.


def test_transition_verifier_names_the_mismatching_field(monkeypatch):
    """The observed value is the half the old bare-bool implementation threw
    away, and it is the half that identifies the defect."""
    raw = _jsonl({"id": ASP_ID, "goals": [
        {"id": GOAL_ID, "status": "in-progress", "claimed_by": "echo"}]})
    _patch_backend(monkeypatch, _FakeOwnCloudBackend(raw=raw))
    ok, mm = aw._verify_transition_persisted(
        LIVE_PATH, ASP_ID, GOAL_ID, {"claimed_by": None, "claimed_at": None})
    assert ok is False
    assert [m["field"] for m in mm] == ["claimed_by"]
    assert mm[0]["expected"] is None
    assert mm[0]["observed"] == "echo"


def test_transition_verifier_reports_every_mismatch_not_just_the_first():
    """A close writes several fields at once; short-circuiting on the first
    disagreement would hide the rest and invite a second measurement pass."""
    raw = _jsonl({"id": ASP_ID, "goals": [
        {"id": GOAL_ID, "status": "in-progress",
         "lastAchievedAt": "2026-08-31T18:05:14", "claimed_by": "alpha"}]})
    import pytest as _pytest
    mp = _pytest.MonkeyPatch()
    try:
        _patch_backend(mp, _FakeOwnCloudBackend(raw=raw))
        ok, mm = aw._verify_transition_persisted(
            LIVE_PATH, ASP_ID, GOAL_ID,
            {"status": "pending", "lastAchievedAt": "2026-09-01T00:35:25",
             "claimed_by": None, "claimed_at": None})
    finally:
        mp.undo()
    assert ok is False
    assert {m["field"] for m in mm} == {"status", "lastAchievedAt",
                                        "claimed_by"}


def test_transition_verifier_match_reports_no_mismatches(monkeypatch):
    """POSITIVE CONTROL. Without it the two assertions above would pass even
    if the verifier had started reporting a mismatch unconditionally."""
    raw = _jsonl({"id": ASP_ID, "goals": [
        {"id": GOAL_ID, "status": "pending", "defer_reason": "waiting on X"}]})
    _patch_backend(monkeypatch, _FakeOwnCloudBackend(raw=raw))
    ok, mm = aw._verify_transition_persisted(
        LIVE_PATH, ASP_ID, GOAL_ID, {"defer_reason": "waiting on X"})
    assert ok is True
    assert mm == []


def test_transition_verifier_fail_open_paths_report_no_mismatches(monkeypatch):
    """The conservative fail-open contract is unchanged: no-verify returns
    True, and must not manufacture a mismatch for a caller to render."""
    _patch_backend(monkeypatch, _FakeLocalBackend())
    ok, mm = aw._verify_transition_persisted(
        LIVE_PATH, ASP_ID, GOAL_ID, {"status": "completed"})
    assert (ok, mm) == (True, [])


def test_transition_verifier_goal_absent_carries_a_sentinel(monkeypatch):
    """goal-absent is a real failure, so it must not render as the empty
    'no field mismatch reported' string at the call site."""
    raw = _jsonl({"id": ASP_ID, "goals": [{"id": "g-115-0001"}]})
    _patch_backend(monkeypatch, _FakeOwnCloudBackend(raw=raw))
    ok, mm = aw._verify_transition_persisted(
        LIVE_PATH, ASP_ID, GOAL_ID, {"status": "completed"})
    assert ok is False
    assert mm and mm[0]["field"] == "<goal>"
    assert "absent" in aw._format_transition_mismatches(mm)


def test_format_transition_mismatches_renders_both_shapes():
    assert aw._format_transition_mismatches([]) == "no field mismatch reported"
    rendered = aw._format_transition_mismatches(
        [{"field": "status", "expected": "pending", "observed": "completed"},
         {"field": "claimed_at", "expected": None, "observed": "2026-09-01"}])
    assert "status: expected='pending' observed='completed'" in rendered
    assert "claimed_at: expected=None observed='2026-09-01'" in rendered


def test_format_transition_mismatches_caps_long_values():
    """`_CRITICAL_TRANSITION_FIELDS` includes `defer_reason`, which is
    narrative by design (probe-before-defer tells authors to cite probe
    output). Measured on the live queue 2026-09-01: 157 goals carrying one,
    median 1,044 chars, max 5,204, 133 of 157 over 500. Uncapped, one
    mismatch renders both ends into an HTTP body and a stderr line."""
    long_a = "x" * 4000
    rendered = aw._format_transition_mismatches(
        [{"field": "defer_reason", "expected": long_a, "observed": "short"}])
    assert len(rendered) < 400, f"rendering not bounded: {len(rendered)} chars"
    assert "truncated, 4002 chars" in rendered   # repr adds the two quotes
    assert "observed='short'" in rendered        # the short side stays whole


def test_cap_keeps_length_visible_when_both_sides_truncate():
    """The length is the load-bearing part. Two long values that differ only
    PAST the cap would render identically without it, turning a real
    disagreement into apparent noise."""
    a, b = "y" * 3000, "y" * 3500
    rendered = aw._format_transition_mismatches(
        [{"field": "defer_reason", "expected": a, "observed": b}])
    assert "3002 chars" in rendered and "3502 chars" in rendered


def test_cap_is_off_below_the_threshold():
    """POSITIVE CONTROL: values under the cap must pass through untouched,
    or every short diagnostic would carry truncation noise."""
    short = "z" * (aw._MISMATCH_VALUE_CAP - 10)
    rendered = aw._format_transition_mismatches(
        [{"field": "status", "expected": short, "observed": None}])
    assert "truncated" not in rendered
    assert short in rendered
