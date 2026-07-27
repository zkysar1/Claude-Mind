"""test_owncloud_sync_local_wins.py — both-diverged LOCAL-WINS auto-resolve for
single-writer per-agent session files (g-115-2820).

Root cause the fix closes: owncloud_sync._sync_one's both-diverged branch
auto-reconciles by UNION only for merge-REGISTERED stores. A single-writer
per-agent session file (working-memory.yaml, ...) is deliberately NOT
merge-registered (unioning two sessions' private scratch — loop_state counters
etc. — is semantically wrong; guard-907), so it fell to the clobber-safe
skip-forever path and the both-diverged streak climbed unbounded (zeta's
working-memory.yaml: 451 consecutive skips before a MANUAL reconcile,
g-115-2816). Nothing AUTO-RESOLVED it.

The fix: for a single-writer session file under own_cloud_authority, resolve
both-diverged as LOCAL-WINS (push local -> S3, adopt local md5 as the new
baseline). Reaching the both-diverged branch for such a file PROVES this box
authored the local change (the sweep's H4a owned-prune never walks a PEER
agent's session dir under own-cloud), so local IS authoritative.

Tests:
  A  single-writer session file + own-cloud auth -> local-wins (push+adopt)
  B  dry-run -> counts local_wins_would_resolve, touches nothing
  C  peer-authored world/ store + own-cloud auth -> NO local-wins, clobber-safe
     skip preserved (the load-bearing multi-writer safety, guard-907)
  D  session file WITHOUT own-cloud auth -> NO local-wins (skip preserved)
  E  machine_local / unregistered / scratch / sessions-plural / world paths ->
     classifier rejects (direct _is_single_writer_session_file unit coverage)

Transport is faked (FakeBackend from test_owncloud_sync); FakeBackend exposes no
merge_put, so _try_merge_put returns _MERGE_NA exactly as a real unregistered
store would, and the local-wins branch is reached the same way it is in prod.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))
sys.path.insert(0, str(SCRIPT_DIR))

import owncloud_sync as _mod  # noqa: E402
from test_owncloud_sync import FakeBackend, _md5, _new_stats  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Autouse fixtures do not cross module boundaries — pin our own clean env
    against a runner shell exporting backend / multi-machine env."""
    monkeypatch.delenv("MACHINE_MULTI", raising=False)
    monkeypatch.delenv("OWNERSHIP_STALE_SECONDS", raising=False)
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)


def _session_wm(root: Path, agent: str = "bravo") -> Path:
    """A continuity-tier single-writer session file under agents/<agent>/session/."""
    f = root / agent / "session" / "working-memory.yaml"
    f.parent.mkdir(parents=True, exist_ok=True)
    return f


# ── A: the fix — single-writer session file both-diverged self-heals ────────
def test_single_writer_session_local_wins(tmp_path):
    """Both moved since baseline + single-writer session file + own-cloud auth ->
    LOCAL-WINS: push local -> S3, adopt local as the new baseline. The wedge
    self-heals in ONE sweep instead of skipping forever (g-115-2820)."""
    be = FakeBackend([(tmp_path, "agents")])
    f = _session_wm(tmp_path)
    local = b"active_context:\n  session_id: session-500\n"   # local authored
    f.write_bytes(local)
    be.s3[str(f)] = b"active_context:\n  session_id: session-499\n"  # S3 moved
    baseline = _md5(b"active_context:\n  session_id: session-498\n")  # neither side
    stats = _new_stats()

    out = _mod._sync_one(be, f, dry_run=False, stats=stats,
                         baseline_md5=baseline, multi_machine=True,
                         own_cloud_authority=True)

    # Local wins: S3 now carries local bytes, local is UNCHANGED, and the
    # returned md5 (new baseline) is local's -> next sweep reads in-sync.
    assert stats.get("local_wins_resolved") == 1
    assert be.s3[str(f)] == local
    assert f.read_bytes() == local
    assert out == _md5(local)
    # Terminating: NOT counted/queued as a persistent conflict.
    assert stats.get("diverged_skipped") in (None, 0)
    assert "conflict_paths" not in stats
    assert be.puts == [str(f)]              # a real fenced PUT happened


# ── B: dry-run observes, mutates nothing ────────────────────────────────────
def test_single_writer_dry_run_would_resolve(tmp_path):
    be = FakeBackend([(tmp_path, "agents")])
    f = _session_wm(tmp_path)
    local = b"active_context:\n  session_id: session-500\n"
    f.write_bytes(local)
    s3 = b"active_context:\n  session_id: session-499\n"
    be.s3[str(f)] = s3
    stats = _new_stats()

    out = _mod._sync_one(be, f, dry_run=True, stats=stats,
                         baseline_md5=_md5(b"other"), multi_machine=True,
                         own_cloud_authority=True)

    assert out is None
    assert stats.get("local_wins_would_resolve") == 1
    assert stats.get("local_wins_resolved") in (None, 0)
    assert be.s3[str(f)] == s3               # S3 untouched
    assert f.read_bytes() == local           # local untouched
    assert be.puts == []


# ── C: THE load-bearing safety — a peer-authored world/ store must NOT ──────
#      local-wins even under own-cloud authority (multi-writer clobber-safe).
def test_world_store_diverged_not_local_wins(tmp_path):
    """A both-diverged PEER-authored store (world/, unregistered) under own-cloud
    authority keeps the conservative clobber-safe skip — local-wins must NEVER
    leak past the single-writer session-file class onto a shared tree, or it
    would clobber a peer (guard-907 / the whole reason merge-registration exists
    for shared stores)."""
    be = FakeBackend([(tmp_path, "world")])
    f = tmp_path / "some-unregistered.jsonl"       # NOT a session file
    f.write_bytes(b"local\n")
    be.s3[str(f)] = b"peer\n"
    stats = _new_stats()

    out = _mod._sync_one(be, f, dry_run=False, stats=stats,
                         baseline_md5=_md5(b"base\n"), multi_machine=True,
                         own_cloud_authority=True)

    assert out is None
    assert stats.get("local_wins_resolved") in (None, 0)
    assert stats.get("diverged_skipped") == 1        # clobber-safe skip preserved
    assert stats.get("conflict_paths") == [str(f)]
    assert be.s3[str(f)] == b"peer\n"                # S3 untouched (no clobber)
    assert f.read_bytes() == b"local\n"              # local untouched
    assert be.puts == []


# ── D: own_cloud_authority gate — a pure multi-machine (non-own-cloud) run ──
#      cannot prove single authority, so it keeps the skip.
def test_session_file_without_authority_skips(tmp_path):
    be = FakeBackend([(tmp_path, "agents")])
    f = _session_wm(tmp_path)
    f.write_bytes(b"local\n")
    be.s3[str(f)] = b"s3\n"
    stats = _new_stats()

    out = _mod._sync_one(be, f, dry_run=False, stats=stats,
                         baseline_md5=_md5(b"base\n"), multi_machine=True,
                         own_cloud_authority=False)   # no own-cloud authority

    assert out is None
    assert stats.get("local_wins_resolved") in (None, 0)
    assert stats.get("diverged_skipped") == 1
    assert be.s3[str(f)] == b"s3\n"
    assert f.read_bytes() == b"local\n"


# ── E: classifier unit coverage — the single-writer session-file predicate ──
def test_classifier_matrix(tmp_path):
    """_is_single_writer_session_file: only agents/<name>/session/<continuity|
    ephemeral file> is eligible; peer/shared/machine-local/unregistered/scratch
    paths are rejected."""
    be = FakeBackend([(tmp_path / "agents", "agents"),
                      (tmp_path / "world", "world"),
                      (tmp_path / "meta", "meta")])
    agents = tmp_path / "agents"

    # eligible: continuity-tier session files
    assert _mod._is_single_writer_session_file(
        agents / "bravo" / "session" / "working-memory.yaml", be) is True
    assert _mod._is_single_writer_session_file(
        agents / "alpha" / "session" / "handoff.yaml", be) is True
    assert _mod._is_single_writer_session_file(
        agents / "zeta" / "session" / "execution-diary.jsonl", be) is True

    # rejected: scratch (machine-local ad-hoc workspace)
    assert _mod._is_single_writer_session_file(
        agents / "bravo" / "session" / "scratch" / "x.json", be) is False
    # rejected: an extensionless / unregistered signal file fails safe
    assert _mod._is_single_writer_session_file(
        agents / "bravo" / "session" / "running-session-id", be) is False
    # rejected: agent-dir file NOT under session/ (e.g. self.md)
    assert _mod._is_single_writer_session_file(
        agents / "bravo" / "self.md", be) is False
    # rejected: sessions/ (plural per-SID scratch) is not session/ (singular)
    assert _mod._is_single_writer_session_file(
        agents / "bravo" / "sessions" / "sid-1" / "binding.yaml", be) is False
    # rejected: peer-authored shared trees
    assert _mod._is_single_writer_session_file(
        tmp_path / "world" / "aspirations.jsonl", be) is False
    assert _mod._is_single_writer_session_file(
        tmp_path / "meta" / "evolution-log.jsonl", be) is False


def test_classifier_no_roots_false():
    """A backend without _roots (e.g. LocalBackend) -> not eligible (fail safe)."""
    class _NoRoots:
        pass
    assert _mod._is_single_writer_session_file(
        Path("/whatever/agents/bravo/session/working-memory.yaml"),
        _NoRoots()) is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
