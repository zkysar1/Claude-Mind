""": archives are excluded from the EAGER pull, but are NOT machine-local.

The whole risk of this change is class confusion. `_EXCLUDE_NAMES`/`_EXCLUDE_GLOBS`
mean MACHINE-LOCAL (never mirrored to S3 in either direction) and are consulted by
`OwnCloudBackend._put`; `_EAGER_PULL_EXCLUDE_GLOBS` means "do not drag this along on
a cold start or a sweep tick" and is consulted only by the two eager pull paths.

If a future edit collapses the two, archive appends made on one box stop being pushed
and every other box's copy diverges permanently -- silently, because nothing errors.
`test_archive_is_NOT_machine_local` is the tripwire for exactly that.

The second risk is scope. Ten *-archive.jsonl files live under agents/<name>/ and are
per-agent continuity, not egress whales; `test_agents_root_is_out_of_scope` pins them
out of the policy so a future caller passing prefix="agents" cannot un-sync them.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import owncloud_sync as ocs  # noqa: E402


# --- the eager-pull predicate itself ---------------------------------------

def test_changelog_archive_is_eager_pull_excluded():
    assert ocs._is_eager_pull_excluded("changelog-archive.jsonl", "world") is True


def test_any_archive_sibling_is_eager_pull_excluded():
    # The goal asks for "changelog-archive.jsonl (and any *-archive.jsonl sibling)".
    # These 8 were measured live in S3 under world/ on 2026-08-07.
    for name in ("aspirations-archive.jsonl", "coordination-archive.jsonl",
                 "findings-archive.jsonl", "pipeline-archive.jsonl",
                 "reasoning-bank-archive.jsonl", "guardrails-archive.jsonl",
                 "pattern-signatures-archive.jsonl",
                 "coordination-archive-archive.jsonl"):
        assert ocs._is_eager_pull_excluded(name, "world") is True, name


def test_meta_root_is_in_scope():
    assert ocs._is_eager_pull_excluded("changelog-archive.jsonl", "meta") is True


def test_hot_files_are_not_eager_pull_excluded():
    # Negative control. The hot append-only files must keep syncing eagerly -- a
    # glob that caught these would be a severe regression, and "*-archive.jsonl"
    # is deliberately narrow enough not to.
    for name in ("changelog.jsonl", "aspirations.jsonl", "reasoning-bank.jsonl",
                 "guardrails.jsonl", "pipeline.jsonl", "retrieval-trace.jsonl",
                 "archive.jsonl",                  # no "-archive" suffix segment
                 "changelog-archive.jsonl.bak"):   # suffix must be terminal
        assert ocs._is_eager_pull_excluded(name, "world") is False, name


# --- scope: agents/ is deliberately NOT covered ----------------------------

def test_agents_root_is_out_of_scope():
    """agents/<name>/experience-archive.jsonl is per-agent CONTINUITY that a cold
    box needs and experience-read.sh actually opens (largest measured 656 KB, vs
    changelog-archive.jsonl at 231 MB). pull_bootstrap's prefix guard admits
    "agents", so without this scoping one future caller would silently un-sync
    every agent's experience archive."""
    for name in ("experience-archive.jsonl", "aspirations-archive.jsonl"):
        assert ocs._is_eager_pull_excluded(name, "agents") is False, name


def test_unknown_prefix_is_out_of_scope():
    assert ocs._is_eager_pull_excluded("changelog-archive.jsonl", "core") is False


# --- the load-bearing class separation -------------------------------------

def test_archive_is_NOT_machine_local():
    """THE tripwire. Machine-local would make OwnCloudBackend._put write local-only,
    so archive appends would never reach S3 and boxes would diverge permanently."""
    for prefix in ("world", "meta"):
        assert ocs._is_machine_local("changelog-archive.jsonl", prefix) is False, prefix


def test_machine_local_set_unchanged_by_this_feature():
    """The eager-pull globs must not have leaked into the machine-local policy."""
    assert "changelog-archive.jsonl" not in ocs._EXCLUDE_NAMES
    for g in ocs._EXCLUDE_GLOBS:
        assert "archive" not in g, g


def test_refresh_would_clobber_unaffected_for_archive(tmp_path):
    """refresh_would_clobber gates the pre-read refresh. If the archive were
    machine-local this would flip to True and block the on-demand refresh that
    keeps archives READABLE -- verification item 3 of the goal."""
    root = tmp_path / "world"
    root.mkdir()
    target = root / "changelog-archive.jsonl"
    target.write_text("{}\n", encoding="utf-8")

    class _Be:
        _roots = [(root, "world")]

    assert ocs.refresh_would_clobber(_Be(), target) is False


# --- the two eager call sites actually consult the predicate ----------------

def test_both_eager_pull_sites_call_the_predicate():
    """Code-review assertion (guard-919: a green fake-backend test is not proof at
    this boundary, so pin the real call sites textually as a second signal)."""
    src = Path(ocs.__file__).read_text(encoding="utf-8")
    # 1 def + 2 call sites (_materialize_tree cold-start, pull_sweep periodic).
    assert src.count("_is_eager_pull_excluded(") >= 3
    assert "skipped_eager_excluded" in src


def test_eager_pull_stats_initialize_the_counter(monkeypatch):
    """pull_sweep uses `+=`, so a missing key would raise KeyError on the first
    excluded object -- on a real box, mid-sweep. Assert BEHAVIOURALLY (call the
    function and read its returned stats) rather than by grepping the source: a
    textual probe passes on a key that sits in the wrong function, and it also
    silently depends on how far the docstring happens to push the dict.
    Both functions return their stats dict on the local-backend early exit."""
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    for fn in (ocs.pull_sweep, ocs.pull_bootstrap):
        stats = fn(be=None)
        assert stats["skipped"] == "local backend (no-op)"
        assert stats["skipped_eager_excluded"] == 0, fn.__name__


# --- the exclusion actually FIRES inside the real pull paths ----------------
#
# Everything above tests the PREDICATE, or asserts on source text. Nothing above
# ever executes the `if _is_eager_pull_excluded(...): continue` branch: the only
# test that calls a real entry point pins STORAGE_BACKEND=local, where both
# pull_sweep and pull_bootstrap return at the early exit before the object loop.
# So a refactor that reorders the guard after the pull, or strands it in dead
# code, keeps every test above green while the archives are pulled again.
#
# These two close that gap by driving the real functions with a fake backend.
# guard-919 bounds what they are worth: a green fake-backend test is NOT proof
# at the S3 boundary. Their value is regression detection on the CALL SITES;
# the live-S3 probe (, cc-03: skipped_eager_excluded=9, all 9
# *-archive.jsonl objects excluded, none missed) is the boundary evidence, and
# these complement it rather than replacing it.
#
# NOTE on what NOT to assert: `pulled_files` is appended only when
# stats["pulled"] increments, which never happens under dry_run=True. Asserting
# "no archive in pulled_files" from a dry run therefore passes even if the
# exclusion is deleted outright — it is vacuous. Assert instead on what actually
# reached _pull_one, which is the real side effect being prevented.


class _FakeBackend:
    """Only what the two eager pull paths touch: `_roots` (via ocs._roots),
    `list_objects` (pull_sweep) and `list_dir` (_materialize_tree)."""

    def __init__(self, root: Path, names):
        self._roots = [(str(root), "world")]
        self._root = Path(root)
        self._names = list(names)

    def list_objects(self, root_path):           # pull_sweep
        return [(n, f"etag-{i}", 100 + i) for i, n in enumerate(self._names)]

    def list_dir(self, cur):                     # _materialize_tree
        if Path(cur) == self._root:
            return [self._root / n for n in self._names]
        return []                                # every child is a leaf


def _record_pull_one(monkeypatch):
    """Capture which basenames reach _pull_one — the side effect the exclusion
    exists to prevent. Returns the list, populated as the path runs."""
    seen = []

    def _fake(be, full, *, dry_run, stats, baseline_md5=None):
        seen.append(Path(full).name)
        stats["would_pull"] = stats.get("would_pull", 0) + 1
        return None

    monkeypatch.setattr(ocs, "_pull_one", _fake)
    return seen


_NAMES = ("changelog-archive.jsonl", "aspirations.jsonl")


def test_pull_sweep_really_skips_the_archive(tmp_path, monkeypatch):
    """pull_sweep call site (the periodic sweep tick)."""
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    monkeypatch.setattr(ocs, "_load_manifest", lambda: {})
    seen = _record_pull_one(monkeypatch)

    stats = ocs.pull_sweep(_FakeBackend(tmp_path, _NAMES),
                           only_root="world", dry_run=True)

    # Guard the guard: if this ever early-exits again, the rest is vacuous.
    assert stats["skipped"] is None, "must reach the object loop, not early-exit"
    assert stats["skipped_eager_excluded"] == 1
    assert "changelog-archive.jsonl" not in seen, "archive reached _pull_one"
    assert "aspirations.jsonl" in seen, "hot file must still pull"


def test_materialize_tree_really_skips_the_archive(tmp_path, monkeypatch):
    """_materialize_tree call site (cold-start bootstrap)."""
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    seen = _record_pull_one(monkeypatch)
    # Same counter shape pull_sweep builds — _materialize_tree uses bare `+=`
    # on several of these, so a missing key raises KeyError mid-walk.
    stats = {"scanned": 0, "pulled": 0, "would_pull": 0, "in_sync": 0,
             "s3_absent": 0, "local_ahead_skipped": 0, "multipart_deferred": 0,
             "errors": 0, "skipped_machine_local": 0,
             "skipped_eager_excluded": 0, "pulled_files": []}

    ocs._materialize_tree(_FakeBackend(tmp_path, _NAMES), tmp_path, tmp_path,
                          "world", stats=stats, manifest={}, new_manifest={},
                          dry_run=True)

    assert stats["skipped_eager_excluded"] == 1
    assert "changelog-archive.jsonl" not in seen, "archive reached _pull_one"
    assert "aspirations.jsonl" in seen, "hot file must still pull"
