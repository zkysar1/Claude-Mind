"""test_target_state_external_path.py — world/ + meta/ virtual prefixes resolve.

Background (g-115-3601, 2026-07-28, bravo/cc-05):
  `_resolve_target_paths` Mode B did a bare `Path(project_root) / rel_path`
  join. But world/ and meta/ are EXTERNAL, user-configured roots
  (agents/<agent>/local-paths.conf) — only core/, .claude/ and agents/ live
  under PROJECT_ROOT. So `meta/skill-gaps.yaml` resolved to a PROJECT_ROOT path
  that does not exist, the follow-on `relative_to(project_root)` boundary check
  would have refused the real external path anyway, and the Mode C basename
  fallback is unreachable for a slashed path. Result: [] -> exists:false.

  Measured on the originating symptom goal g-115-3292 (target
  meta/skill-gaps.yaml, extraction confidence "high"):
      BEFORE: exists=false readable=false hits=[] misses=[all 9]
              verdict="unknown"  ("target files unreadable or out of project")
      AFTER : exists=true  readable=true  hits=7 misses=2
              verdict="already_present" (hit_ratio 0.778)

WHY THIS TEST EXISTS AND NOT JUST THE FIX:
  The probe is fail-open by design, so this defect never raised. It surfaced as
  verdict=unknown, which is INDISTINGUISHABLE from "probe ran, found nothing
  conclusive". The whole advisory was dead for every goal whose target lives
  under meta/ or world/ (skill-gaps, forged-skills, aspirations, conventions,
  knowledge-tree nodes, reasoning bank, guardrails) and nothing could notice.
  Without a test pinning exists=true, it re-rots exactly as silently — which is
  the failure mode, not a side-effect of it.

SHARED-LAYER SCOPE: the fix lives in _target_state.py, which
  gates/goal_duplication.py imports (probe_target_state) for the FILING-time
  duplicate check — "same extractor, different chokepoint". Both chokepoints
  are repaired by the one change; test_shared_layer_reaches_goal_duplication
  pins that import relationship so a future split cannot silently re-narrow it.

Cross-refs: guard-132 (core framework Python handling a world//meta/ path MUST
  resolve via _paths.resolve_file_path — never PROJECT_ROOT / path),
  guard-1102, guard-521, .claude/rules/path-resolution.md.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

import _paths  # noqa: E402 — patched per-test; must be the same module object
               # that _resolve_virtual_path's late `import _paths` resolves to.

TS_PATH = CORE_SCRIPTS / "_target_state.py"
spec = importlib.util.spec_from_file_location("_target_state", TS_PATH)
ts_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ts_mod)

# Neutralize gate telemetry (same rationale as test_target_state_check_positional:
# without this every run appends synthetic firings to PRODUCTION
# meta/gate-firings.jsonl).
ts_mod._gate_log = lambda *args, **kwargs: None


@pytest.fixture
def ext_roots(tmp_path, monkeypatch):
    """Point WORLD_DIR/META_DIR at hermetic tmp roots.

    Hermetic on purpose: binding to the live external roots would make the
    assertions depend on real world/meta content, and under STORAGE_BACKEND=
    own-cloud a tmp world that a subprocess writes can collide on the
    PRODUCTION S3 key (guard-955). Nothing here spawns a subprocess, and
    nothing writes outside tmp_path.
    """
    world = tmp_path / "ext-world"
    meta = tmp_path / "ext-meta"
    (world / "conventions").mkdir(parents=True)
    meta.mkdir(parents=True)
    (world / "conventions" / "capability-routing.md").write_text(
        "grant-007 product-repo PR merge\n", encoding="utf-8")
    (meta / "skill-gaps.yaml").write_text(
        "gaps:\n  - id: gap-031\n    type: utility\n", encoding="utf-8")
    monkeypatch.setattr(_paths, "WORLD_DIR", world, raising=False)
    monkeypatch.setattr(_paths, "META_DIR", meta, raising=False)
    return world, meta


# ─── THE regression assertion: external target resolves + exists ────────────

def test_meta_prefix_resolves_to_external_root(ext_roots):
    """meta/<f> resolves under META_DIR, not PROJECT_ROOT. THE pin."""
    world, meta = ext_roots
    got = ts_mod._resolve_virtual_path("meta/skill-gaps.yaml")
    assert got == (meta / "skill-gaps.yaml").resolve()


def test_world_prefix_resolves_to_external_root(ext_roots):
    world, meta = ext_roots
    got = ts_mod._resolve_virtual_path("world/conventions/capability-routing.md")
    assert got == (world / "conventions" / "capability-routing.md").resolve()


def test_resolve_target_paths_finds_existing_external_file(ext_roots):
    """_resolve_target_paths returns the external file rather than []."""
    world, meta = ext_roots
    got = ts_mod._resolve_target_paths(str(_paths.PROJECT_ROOT), "meta/skill-gaps.yaml")
    assert len(got) == 1
    assert got[0] == (meta / "skill-gaps.yaml").resolve()


def test_probe_reports_exists_true_for_external_target(ext_roots):
    """End-to-end through probe_target_state: per_file[].exists is True.

    This is the criterion the goal names verbatim, asserted at the level the
    caller actually consumes — a unit-level path assertion alone would not
    catch a regression in how probe_target_state consumes the resolution.
    """
    world, meta = ext_roots
    pr = ts_mod.probe_target_state(
        str(_paths.PROJECT_ROOT),
        ["meta/skill-gaps.yaml"],
        ["gap-031"],
    )
    assert len(pr["per_file"]) == 1
    entry = pr["per_file"][0]
    assert entry["exists"] is True, "external-path target must not report exists=false"
    assert entry["readable"] is True
    assert "gap-031" in entry["hits"]
    assert pr["verdict"] != "unknown", "verdict=unknown was the silent-death signature"


def test_line_hint_path_also_resolves_externally(ext_roots):
    """_read_target_file shares the fix, so line hints agree with per_file.

    Fixing only _resolve_target_paths would let per_file say exists=true while
    line_hint_verifications said 'file unreadable' for the IDENTICAL path —
    internally contradictory output from one probe call.
    """
    world, meta = ext_roots
    content, existed = ts_mod._read_target_file(
        str(_paths.PROJECT_ROOT), "meta/skill-gaps.yaml")
    assert existed is True
    assert content is not None and "gap-031" in content


# ─── Controls: the fix must not widen what is reachable ─────────────────────

def test_traversal_out_of_external_root_is_refused(ext_roots):
    """world/../../<anything> must NOT escape WORLD_DIR.

    The virtual prefix authorizes its own root and nothing else. This control
    is why _resolve_virtual_path re-asserts containment instead of simply
    waiving the caller's project_root boundary check.
    """
    assert ts_mod._resolve_virtual_path("world/../../etc/passwd") is None
    assert ts_mod._resolve_target_paths(
        str(_paths.PROJECT_ROOT), "world/../../etc/passwd") == []


def test_traversal_onto_an_EXISTING_out_of_root_file_is_refused(ext_roots, tmp_path):
    """The escape above lands on a path that does not exist, so two of its
    three surfaces prove nothing. This one lands on a file that DOES exist.

    WHY BOTH CASES ARE NEEDED (g-115-3752, measured 2026-07-28 by neutered-build
    differential). `world/../../etc/passwd` is filtered by `.is_file()` before
    containment is ever consulted, so `_resolve_target_paths(...) == []` and the
    `_read_target_file` chokepoint stay GREEN even with the containment line in
    `_resolve_virtual_path` deleted. Only its `is None` assertion discriminates.
    That makes the sibling above a real guard on ONE surface and a decoration on
    the others — the exact shape guard-4166 warns about, where an absence-shaped
    expectation is also what a dead component produces.

    Here the escape resolves onto a file that exists, so all three surfaces
    separate the builds: with containment removed, `_resolve_virtual_path`
    returns the path, `_resolve_target_paths` returns it too, and
    `_read_target_file` READS IT — the last being the chokepoint where an
    out-of-root read actually happens, which carried no traversal assertion at
    all before this test.

    Kept hermetic (the target file is created inside tmp_path, one level above
    the fixture's WORLD_DIR) rather than escaping onto a real repo file. The
    fixture's own docstring makes hermeticity a deliberate property, and a test
    that reads PROJECT_ROOT content to prove containment would trade this
    file's isolation for the very reachability it is asserting against.
    """
    world, _meta = ext_roots
    outside = tmp_path / "outside-of-world-root.md"
    outside.write_text("SENTINEL_OUT_OF_ROOT_CONTENT\n", encoding="utf-8")
    escape = "world/../outside-of-world-root.md"

    # Positive control for the SETUP: the escape must genuinely land on the
    # file, or this test degrades into the non-existent case it exists to
    # complement and would go green for the wrong reason.
    assert (world / ".." / "outside-of-world-root.md").resolve() == outside.resolve()
    assert outside.is_file()

    assert ts_mod._resolve_virtual_path(escape) is None, (
        "containment: a virtual prefix authorizes its OWN root only"
    )
    assert ts_mod._resolve_target_paths(str(_paths.PROJECT_ROOT), escape) == [], (
        "an existing out-of-root file must not be returned as a resolved target"
    )
    content, existed = ts_mod._read_target_file(str(_paths.PROJECT_ROOT), escape)
    assert existed is False, (
        "_read_target_file is the chokepoint where the out-of-root READ happens; "
        "it must refuse the escape rather than report the file as present"
    )
    assert content is None, "no out-of-root content may be returned"
    assert "SENTINEL_OUT_OF_ROOT_CONTENT" not in (content or ""), (
        "out-of-root file content leaked through _read_target_file"
    )


def test_non_virtual_path_is_untouched(ext_roots):
    """An ordinary in-repo path still resolves via the PROJECT_ROOT join."""
    assert ts_mod._resolve_virtual_path("core/scripts/_target_state.py") is None
    got = ts_mod._resolve_target_paths(
        str(_paths.PROJECT_ROOT), "core/scripts/_target_state.py")
    assert len(got) == 1 and got[0].name == "_target_state.py"


def test_resolvable_but_absent_returns_empty(ext_roots):
    """Prefix resolves, file absent -> [] (not a PROJECT_ROOT fallback)."""
    assert ts_mod._resolve_virtual_path("meta/no-such-file.yaml") is not None
    assert ts_mod._resolve_target_paths(
        str(_paths.PROJECT_ROOT), "meta/no-such-file.yaml") == []


def test_unconfigured_external_root_fails_open(monkeypatch):
    """WORLD_DIR unset -> None, never a raise.

    _paths.resolve_file_path raises RuntimeError when the matching *_DIR is
    None. Every caller of this helper is a fail-open advisory, so the raise
    must be absorbed — a throw here would wedge the Phase 4-pre probe and the
    filing-time duplication gate together.
    """
    monkeypatch.setattr(_paths, "WORLD_DIR", None, raising=False)
    assert ts_mod._resolve_virtual_path("world/anything.md") is None


def test_shared_layer_reaches_goal_duplication():
    """goal_duplication imports the fixed extractor — one fix, both chokepoints.

    Pins the import relationship the goal asked to confirm, so a future refactor
    that gives the filing-time gate its own copy of the resolver cannot silently
    re-narrow the fix to the probe alone.
    """
    src = (CORE_SCRIPTS / "gates" / "goal_duplication.py").read_text(encoding="utf-8")
    assert "probe_target_state" in src
    assert "_target_state" in src
