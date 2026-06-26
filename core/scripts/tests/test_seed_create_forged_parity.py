"""seed-create forged-registry cross-reference for exclude_children
(g-303-21, zeta allowlist audit site 7b).

seed-manifest.yaml:41 exclude_children hand-mirrors the world/forged-skills.yaml
registry with NO sync enforcement; the audit flagged 7b rot-risk HIGH -- a 16th
forged skill registered without a matching exclude_children entry LEAKS into the
domain-agnostic seed. The path-(i) fix adds a dynamic-source audit to
_seed_create_scan.py: at seed-create time it queries the live forged registry
(via the seed engine's resolver) and diffs it against exclude_children.

These tests cover the pure diff logic (forged_not_excluded). The registry READ
is external (world/ is off-repo) and is exercised by the reused
_seed_engine._dest_forged_skill_names; the leak-detection contract is hermetic
and tested here.
"""
import importlib.util
from pathlib import Path

_MOD = Path(__file__).resolve().parent.parent / "_seed_create_scan.py"
_spec = importlib.util.spec_from_file_location("_seed_create_scan", _MOD)
sc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sc)


def test_clean_when_every_forged_skill_is_excluded():
    forged = {"notify-user", "run-processor", "access-email"}
    exclude = {"notify-user", "run-processor", "access-email", "worktrees", ".history"}
    assert sc.forged_not_excluded(forged, exclude) == []


def test_leak_detected_for_unexcluded_forged_skill():
    """The rot the fix catches: a 16th forged skill registered but not excluded."""
    forged = {"notify-user", "run-processor", "new-forged-skill"}
    exclude = {"notify-user", "run-processor", "worktrees", ".history"}
    assert sc.forged_not_excluded(forged, exclude) == ["new-forged-skill"]


def test_multiple_leaks_returned_sorted():
    forged = {"zeta-skill", "alpha-skill", "mid-skill"}
    exclude = set()
    assert sc.forged_not_excluded(forged, exclude) == [
        "alpha-skill", "mid-skill", "zeta-skill"
    ]


def test_none_registry_failsafes_to_empty():
    """Registry unlocatable/unparseable -> [] : the audit cannot run, so it must
    not false-alarm (the mtime new-skills scan still surfaces drift)."""
    assert sc.forged_not_excluded(None, {"notify-user"}) == []


def test_empty_forged_is_empty():
    assert sc.forged_not_excluded(set(), {"notify-user", "worktrees"}) == []


def test_exclude_superset_never_flags_extra_ephemeral_entries():
    """Asymmetric by design: exclude_children carries worktrees/.history that are
    NOT forged skills; forged_not_excluded flags forged-not-in-exclude ONLY,
    never exclude-not-in-forged."""
    forged = {"notify-user"}
    exclude = {"notify-user", "worktrees", ".history", "some-other-dir"}
    assert sc.forged_not_excluded(forged, exclude) == []
