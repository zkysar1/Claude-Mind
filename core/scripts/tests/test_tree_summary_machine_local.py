""" — knowledge/tree/_summary.json is a per-box derived cache and
must never sync.

load-tree-summary.sh rebuilds it from THIS box's _tree.yaml whenever it is
missing or older, so every box writes its own copy. While it synced, any box
that rebuilt between sweeps left it both-diverged and the sweep skipped it
forever (93 consecutive sweeps on DESKTOP-O91DLK2, 407 on zc-09).

Every assertion on the target below fails without the _EXCLUDE_NAMES entry.
The controls fail if the set is widened instead (guard-3018: a SHARED store
made machine-local is silent cross-box data loss).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import owncloud_sync as ocs  # noqa: E402

_ROOT = Path("/nonexistent-world")
_SUMMARY = "knowledge/tree/_summary.json"


def _both_legs_machine_local(rel: str, prefix: str = "world") -> bool:
    """The full predicate, BOTH legs (guard-2471): _is_machine_local alone does
    not test _EXCLUDE_DIRS, and one leg answers wrongly rather than erroring.
    Same shape as test_domain_suite_gate._both_legs_machine_local."""
    target = _ROOT / rel
    parts = target.relative_to(_ROOT).parts
    leg_dirs = any(ocs._is_excluded_dir(seg) for seg in parts[:-1])
    leg_name = ocs._is_machine_local(target.name, prefix,
                                     full_path=target, root_path=_ROOT)
    return leg_dirs or leg_name


def test_tree_summary_is_machine_local():
    assert _both_legs_machine_local(_SUMMARY), (
        "_summary.json must be machine-local, or every box that rebuilds it "
        "between sweeps both-diverges and wedges the mirror")


def test_force_pull_cannot_clobber_the_local_summary(tmp_path):
    """A machine-local file is never pushed, so a pre-read force-pull would
    replace this box's rebuild with whatever stale object the store holds
    (guard-881). refresh_would_clobber must gate it. The root is a real
    (resolved) directory: refresh_would_clobber resolves the target, and on
    Windows a bare "/nonexistent-world" resolves to "C:/nonexistent-world",
    which is no longer under the unresolved root."""
    root = tmp_path.resolve()

    class _Be:
        _roots = [(root, "world")]

    assert ocs.refresh_would_clobber(_Be(), root / _SUMMARY) is True
    assert ocs.refresh_would_clobber(_Be(), root / "knowledge/tree/_tree.yaml") is False


@pytest.mark.parametrize("shared", [
    "knowledge/tree/_tree.yaml",          # the file the summary derives FROM
    "knowledge/tree/intelligence/some-node.md",
    "reasoning-bank.jsonl", "guardrails.jsonl", "aspirations.jsonl",
])
def test_shared_tree_state_still_syncs(shared):
    """guard-3018 control: the entry must name one derived cache, not widen
    the class. _tree.yaml in particular MUST keep syncing — it is what each
    box rebuilds its own summary from."""
    assert not _both_legs_machine_local(shared), (
        f"{shared} became machine-local -- silent cross-box divergence")


class _FakeBackend:
    """Only what pull_sweep touches: `_roots` and `list_objects`."""

    def __init__(self, root: Path, rels):
        self._roots = [(str(root), "world")]
        self._rels = list(rels)

    def list_objects(self, root_path):
        return [(r, f"etag-{i}", 100 + i) for i, r in enumerate(self._rels)]


def test_pull_sweep_never_hands_the_summary_to_pull_one(tmp_path, monkeypatch):
    """Drive the REAL periodic pull call site with a fake backend and record
    what reaches _pull_one (the side effect the exclusion prevents). Asserting
    on the predicate alone would stay green if the call site stopped using it."""
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    monkeypatch.setattr(ocs, "_load_manifest", lambda: {})
    seen = []

    def _fake_pull_one(be, full, *, dry_run, stats, baseline_md5=None):
        seen.append(Path(full).relative_to(tmp_path).as_posix())
        return None

    monkeypatch.setattr(ocs, "_pull_one", _fake_pull_one)
    stats = ocs.pull_sweep(
        _FakeBackend(tmp_path, [_SUMMARY, "knowledge/tree/_tree.yaml"]),
        only_root="world", dry_run=True)

    assert stats["skipped"] is None, "must reach the object loop, not early-exit"
    assert _SUMMARY not in seen, "the summary cache reached _pull_one"
    assert "knowledge/tree/_tree.yaml" in seen, "_tree.yaml must still pull"
    assert stats["skipped_machine_local"] == 1
