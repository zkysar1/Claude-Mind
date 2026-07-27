"""test_fileops_cross_agent_resolve.py — cross-agent resolve_base_dir (Defect A / ).

Ports + locks zeta's b34a169b (stranded unpushed on cc-02): _fileops.resolve_base_dir
and its mirror _classify_base must resolve ANY agent dir under agents_root(), not
only the MIND_AGENT-bound AGENT_DIR.

Before the fix, a NON-bound agent path (e.g. agents/bravo/... while alpha is bound,
or with MIND_AGENT unset) returned None from resolve_base_dir. owncloud_sync.
_snapshot_before_pull's fail-open guard is `base = resolve_base_dir(full); if base
is None: return` — so the pre-pull .history snapshot was silently skipped for every
non-bound agent, and the own-cloud sweep (which syncs ALL agent dirs) clobbered
those files with no backup. This test is the regression lock the goal requires:
"THE FIX HAS NO TEST. Without one the hole reopens silently."

Pins (per g-115-2169 acceptance):
  1. resolve_base_dir('agents/<other>/experience.jsonl') -> agents/<other>
     with MIND_AGENT UNSET.
  2. Same, with MIND_AGENT bound to a DIFFERENT agent (the real sweep
     condition) — binding is now irrelevant to resolution; the bound agent
     still resolves identically (subsumes the old AGENT_DIR-only branch).
  3. _classify_base(<that base>) -> 'agent' (the mirrored half — an unmirrored
     classifier would route the snapshot blacklist to the wrong store).
  4. Negative guards preserved: agents_root() itself -> None; a path outside all
     roots -> None; world/ meta/ .claude/ resolution unchanged.
  5. Integration-shaped: owncloud_sync._snapshot_before_pull actually FIRES
     save_history for a non-bound agent path (the behavior that matters — proving
     the None->non-None transition closes the fail-open skip, not just the
     resolver in isolation).

Hermetic: monkeypatches _fileops.WORLD_DIR / META_DIR / PROJECT_ROOT and the
_fileops.agents_root FUNCTION to tmp roots, so resolution never touches the real
repo agents/ dir and does not depend on MIND_WORLD (agents_root derives from
PROJECT_ROOT, not MIND_WORLD, so an env-reload sandbox would not control it).

Run (own-cloud box: STORAGE_BACKEND=local per guard-955):
  STORAGE_BACKEND=local python -m pytest core/scripts/tests/test_fileops_cross_agent_resolve.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

import _fileops  # noqa: E402


def _setup_roots(tmp_path, monkeypatch):
    """Point _fileops at hermetic tmp world / meta / agents / .claude roots.

    WORLD_DIR / META_DIR / PROJECT_ROOT are module-level Path constants imported
    into _fileops at import time; agents_root is imported as a FUNCTION. Patch all
    of them so resolve_base_dir / _classify_base run fully against tmp and never the
    real repo.
    """
    world = tmp_path / "world"; world.mkdir()
    meta = tmp_path / "meta"; meta.mkdir()
    agents = tmp_path / "agents"; agents.mkdir()
    (agents / "alpha").mkdir()
    (agents / "bravo").mkdir()
    proj = tmp_path / "proj"
    claude = proj / ".claude"; claude.mkdir(parents=True)
    monkeypatch.setattr(_fileops, "WORLD_DIR", world)
    monkeypatch.setattr(_fileops, "META_DIR", meta)
    monkeypatch.setattr(_fileops, "PROJECT_ROOT", proj)
    monkeypatch.setattr(_fileops, "agents_root", lambda: agents)
    return world, meta, agents, claude


# ── 1. non-bound agent resolves, MIND_AGENT unset ───────────────────────────

def test_resolve_non_bound_agent_env_unset(tmp_path, monkeypatch):
    _world, _meta, agents, _claude = _setup_roots(tmp_path, monkeypatch)
    monkeypatch.delenv("MIND_AGENT", raising=False)
    p = agents / "bravo" / "experience.jsonl"
    assert _fileops.resolve_base_dir(p) == (agents / "bravo").resolve()


# ── 2. non-bound agent resolves while bound elsewhere; bound agent unchanged ──

def test_resolve_non_bound_agent_while_bound_elsewhere(tmp_path, monkeypatch):
    _world, _meta, agents, _claude = _setup_roots(tmp_path, monkeypatch)
    monkeypatch.setenv("MIND_AGENT", "alpha")  # bound to alpha — the sweep condition
    # The NON-bound agent (bravo) resolves even though alpha is bound (pre-fix: None).
    assert _fileops.resolve_base_dir(agents / "bravo" / "changelog.jsonl") \
        == (agents / "bravo").resolve()
    # The BOUND agent still resolves identically — the agents_root() branch subsumes
    # the old AGENT_DIR-only branch (AGENT_DIR == agents_root()/<bound-name>).
    assert _fileops.resolve_base_dir(agents / "alpha" / "self.md") \
        == (agents / "alpha").resolve()
    # A deeper nested path under an agent still resolves to the agent dir, not the
    # nested subdir (base is agents_root/<name>, first segment only).
    assert _fileops.resolve_base_dir(agents / "bravo" / "session" / "handoff.yaml") \
        == (agents / "bravo").resolve()


# ── 3. _classify_base mirrors the resolver (snapshot-blacklist routing) ───────

def test_classify_base_agent_for_any_agent_dir(tmp_path, monkeypatch):
    world, meta, agents, _claude = _setup_roots(tmp_path, monkeypatch)
    assert _fileops._classify_base(agents / "bravo") == "agent"
    assert _fileops._classify_base(agents / "alpha") == "agent"
    # world / meta classification unchanged by the agent branch.
    assert _fileops._classify_base(world) == "world"
    assert _fileops._classify_base(meta) == "meta"


# ── 4. negative guards preserved ─────────────────────────────────────────────

def test_negative_guards_preserved(tmp_path, monkeypatch):
    world, meta, agents, claude = _setup_roots(tmp_path, monkeypatch)
    # agents_root() ITSELF is not an agent dir -> None / not "agent".
    assert _fileops.resolve_base_dir(agents) is None
    assert _fileops._classify_base(agents) != "agent"
    # A path outside every root -> None.
    assert _fileops.resolve_base_dir(tmp_path / "nowhere" / "x.txt") is None
    # world / meta / .claude resolution unchanged (the agent branch must not steal them).
    assert _fileops.resolve_base_dir(world / "aspirations.jsonl") == world.resolve()
    assert _fileops.resolve_base_dir(meta / "meta-log.jsonl") == meta.resolve()
    assert _fileops.resolve_base_dir(claude / "skills" / "x" / "SKILL.md") == claude.resolve()


# ── 5. integration-shaped: _snapshot_before_pull FIRES for a non-bound agent ──

def test_snapshot_before_pull_fires_for_non_bound_agent(tmp_path, monkeypatch):
    """The behavior that actually matters: pre-fix, resolve_base_dir(bravo_path)
    returned None so _snapshot_before_pull returned early and NO snapshot was taken
    before the clobbering pull. Post-fix, base is agents/bravo, so save_history
    fires. save_history is stubbed to a recorder so the test needs no history store."""
    import owncloud_sync
    _world, _meta, agents, _claude = _setup_roots(tmp_path, monkeypatch)
    monkeypatch.delenv("MIND_AGENT", raising=False)

    calls = []
    # owncloud_sync._snapshot_before_pull does `from _fileops import ... save_history`
    # lazily at call time, so patching the module attribute is picked up.
    monkeypatch.setattr(_fileops, "save_history",
                        lambda *a, **k: calls.append((Path(a[0]), Path(a[1]))))

    bravo_file = agents / "bravo" / "experience.jsonl"
    bravo_file.write_text('{"id":"exp-x"}\n', encoding="utf-8")

    owncloud_sync._snapshot_before_pull(bravo_file)

    assert len(calls) == 1, "snapshot must fire for a non-bound agent path (pre-fix: skipped)"
    assert calls[0][0] == bravo_file
    assert calls[0][1] == (agents / "bravo").resolve()


def test_snapshot_before_pull_still_skips_out_of_root(tmp_path, monkeypatch):
    """Symmetric negative: a path outside every root still resolves None, so the
    snapshot is (correctly) skipped — the fix widened agent coverage without
    swallowing genuinely-unmanaged paths."""
    import owncloud_sync
    _setup_roots(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(_fileops, "save_history", lambda *a, **k: calls.append(a))
    outside = tmp_path / "nowhere" / "y.txt"
    outside.parent.mkdir(parents=True)
    outside.write_text("x", encoding="utf-8")
    owncloud_sync._snapshot_before_pull(outside)
    assert calls == []
