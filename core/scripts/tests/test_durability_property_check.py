"""Tests for durability-property-check.py (, D5).

Every test drives a FAILURE CONDITION into a fixture and asserts the matching
check fails — the goal's stated verification method. The healthy-path tests are
the positive controls: without them, a check hard-wired to `return 1` would pass
the whole failure suite.

The sharpest cases here are the VACUOUS-PASS ones (rb-245, guard-1802). A
property check that degrades to an empty result set reports the reassuring
answer forever, so "could not measure" must be FAIL, never PASS. Three separate
degradation paths are pinned: the purge wrapper exiting non-zero, the cited-set
lookup failing, and citation_lookup=="failed" (the purge lane silently running
against the pre-inversion allow-list, where the empty intersection is empty for
the wrong reason).

Run with STORAGE_BACKEND=local pinned (guard-955) — these seed tmp worlds and a
stray own-cloud write would collide on a production S3 key.
"""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import types

import pytest

import sys as _sys
_sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _verify_corpus  # noqa: E402

SCRIPTS = pathlib.Path(__file__).resolve().parents[1]


def load_module():
    spec = importlib.util.spec_from_file_location(
        "durability_property_check", SCRIPTS / "durability-property-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def dpc():
    return load_module()


def _completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def _purge_json(files, citation_lookup="ok", drained_gc_files=None):
    payload = {"purged": 0, "would_purge": len(files), "files": files,
               "citation_lookup": citation_lookup, "dry_run": True}
    # Omitted entirely when None so the back-compat test below drives the exact
    # shape an OLDER temp-drain-purge.sh emits (no drained_gc_files key at all).
    if drained_gc_files is not None:
        payload["drained_gc_files"] = drained_gc_files
    return json.dumps(payload)


# ── cited-temp-not-purged ─────────────────────────────────────────────────────

def test_healthy_passes(dpc, monkeypatch, capsys):
    """Positive control: no overlap + citation_lookup ok => PASS."""
    monkeypatch.setattr(dpc, "_run", lambda *a, **k: _completed(_purge_json(["junk.log"])))
    monkeypatch.setattr(dpc, "_cited_basenames", lambda: {"evidence.md"})
    assert dpc.check_cited_temp_not_purged(None) == 0
    assert "PASS: cited-temp-not-purged" in capsys.readouterr().out


def test_cited_file_scheduled_for_purge_fails(dpc, monkeypatch, capsys):
    """The core property: a cited file in the would-purge set must FAIL."""
    monkeypatch.setattr(dpc, "_run",
                        lambda *a, **k: _completed(_purge_json(["evidence.md", "junk.log"])))
    monkeypatch.setattr(dpc, "_cited_basenames", lambda: {"evidence.md"})
    assert dpc.check_cited_temp_not_purged(None) == 1
    out = capsys.readouterr().out
    assert out.startswith("FAIL:")
    assert "evidence.md" in out


def test_degraded_citation_lookup_fails(dpc, monkeypatch, capsys):
    """citation_lookup=='failed' => exemptions never applied.

    The intersection is EMPTY here and the check must still fail: under the
    legacy allow-list a cited .py/.log/.txt carries no `! -name` exemption at
    all, so an empty overlap is evidence of nothing.
    """
    monkeypatch.setattr(dpc, "_run",
                        lambda *a, **k: _completed(_purge_json(["a.py"], "failed")))
    monkeypatch.setattr(dpc, "_cited_basenames", lambda: {"unrelated.md"})
    assert dpc.check_cited_temp_not_purged(None) == 1
    assert "INACTIVE" in capsys.readouterr().out


def test_na_citation_lookup_is_not_a_pass(dpc, monkeypatch, capsys):
    """: citation_lookup=='n/a' => no temp dir, so NOTHING was scanned.

    This is the THIRD way of being unmeasured, and the one that was missing:
    `failed` and `cited is None` were both refused while `n/a` fell through to
    PASS with rc=0, printing that the guard is ACTIVE and the property holds.

    The payload is the real shape `temp-drain-purge.sh` emits when the temp dir
    does not exist (measured: every file list empty, note="temp dir does not
    exist"). The cited set is deliberately NON-EMPTY so that deleting the branch
    under test does NOT crash — it reaches the PASS path with an empty
    intersection and returns 0. That is what makes this pin go RED on mutation
    rather than passing for an incidental reason (guard-1629/guard-1631).
    """
    monkeypatch.setattr(dpc, "_run",
                        lambda *a, **k: _completed(_purge_json([], "n/a", [])))
    monkeypatch.setattr(dpc, "_cited_basenames", lambda: {"evidence.md"})
    assert dpc.check_cited_temp_not_purged(None) == 1
    out = capsys.readouterr().out
    assert out.startswith("FAIL:")
    # Assert on the REASON, not just the code: a mutation that returns 1 from a
    # different branch would satisfy the rc check alone.
    assert "n/a" in out
    assert "NOTHING was scanned" in out
    # The PASS wording must not appear — it is the specific false claim this fixes.
    assert "the property holds" not in out


def test_na_branch_fires_before_the_cited_set_is_computed(dpc, monkeypatch):
    """The n/a refusal must precede _cited_basenames(), as its comment claims.

    Ordering is behaviour here, not tidiness: computing the cited set shells out
    to temp-citation-ratchet.py, and there is nothing to intersect once the purge
    has reported it scanned nothing. Pinning it stops a later edit from sliding
    the branch below that call, where it would still return 1 but pay for a
    subprocess first — and where a ratchet failure would mask the n/a reason
    behind the `cited is None` message.
    """
    def _explode():
        raise AssertionError("_cited_basenames() must not be reached under n/a")

    monkeypatch.setattr(dpc, "_run",
                        lambda *a, **k: _completed(_purge_json([], "n/a", [])))
    monkeypatch.setattr(dpc, "_cited_basenames", _explode)
    assert dpc.check_cited_temp_not_purged(None) == 1


def test_cited_file_in_lane2_drained_gc_fails(dpc, monkeypatch, capsys):
    """: a cited file in LANE 2's would-purge set must FAIL.

    This is the case the check could not see before g-306-102 — Lane 2 returned
    a bare count, so a cited artifact became age-deletable the moment /drain-temp
    archived it into drained/. Lane 1 is CLEAN here on purpose: if the join were
    dropped this test would pass on Lane 1's empty intersection alone, so the
    clean Lane 1 is what makes the assertion load-bearing.
    """
    monkeypatch.setattr(dpc, "_run", lambda *a, **k: _completed(
        _purge_json(["junk.log"], drained_gc_files=["evidence.md"])))
    monkeypatch.setattr(dpc, "_cited_basenames", lambda: {"evidence.md"})
    assert dpc.check_cited_temp_not_purged(None) == 1
    out = capsys.readouterr().out
    assert out.startswith("FAIL:")
    assert "evidence.md" in out
    assert "Lane 2" in out          # names WHICH lane, else the operator cannot act


def test_lane2_clean_still_passes_and_reports_both_lanes(dpc, monkeypatch, capsys):
    """Positive control for the lane-2 join: a non-empty, non-overlapping lane-2
    list must still PASS and must be COUNTED in the PASS line. Without the count
    assertion a check that silently ignored drained_gc_files would look identical
    to one that read it."""
    monkeypatch.setattr(dpc, "_run", lambda *a, **k: _completed(
        _purge_json(["junk.log"], drained_gc_files=["old-scratch.md", "stale.md"])))
    monkeypatch.setattr(dpc, "_cited_basenames", lambda: {"evidence.md"})
    assert dpc.check_cited_temp_not_purged(None) == 0
    out = capsys.readouterr().out
    assert "Lanes 1+2" in out
    assert "2 Lane-2" in out


def test_missing_drained_gc_files_key_degrades_to_lane1(dpc, monkeypatch, capsys):
    """Back-compat: an OLDER temp-drain-purge.sh emits no drained_gc_files key.

    That must degrade to the prior Lane-1-only behaviour, not raise. Pinned
    because the natural `d["drained_gc_files"]` would KeyError against any
    not-yet-upgraded copy of the script — including a downstream deployment
    mid-promotion.
    """
    monkeypatch.setattr(dpc, "_run", lambda *a, **k: _completed(_purge_json(["junk.log"])))
    monkeypatch.setattr(dpc, "_cited_basenames", lambda: {"evidence.md"})
    assert dpc.check_cited_temp_not_purged(None) == 0
    assert "0 Lane-2" in capsys.readouterr().out


def test_purge_wrapper_error_is_not_a_pass(dpc, monkeypatch, capsys):
    monkeypatch.setattr(dpc, "_run", lambda *a, **k: _completed("", 1, "boom"))
    assert dpc.check_cited_temp_not_purged(None) == 1
    assert "NOT a clean pass" in capsys.readouterr().out


def test_unknown_cited_set_is_not_a_pass(dpc, monkeypatch, capsys):
    """_cited_basenames() returning None (unknown) != set() (nothing cited)."""
    monkeypatch.setattr(dpc, "_run", lambda *a, **k: _completed(_purge_json(["a.py"])))
    monkeypatch.setattr(dpc, "_cited_basenames", lambda: None)
    assert dpc.check_cited_temp_not_purged(None) == 1
    assert "unmeasured" in capsys.readouterr().out


def test_unparseable_purge_json_fails(dpc, monkeypatch, capsys):
    monkeypatch.setattr(dpc, "_run", lambda *a, **k: _completed("not json at all"))
    assert dpc.check_cited_temp_not_purged(None) == 1
    assert "unparseable" in capsys.readouterr().out


def test_cited_basenames_returns_none_on_ratchet_failure(dpc, monkeypatch):
    monkeypatch.setattr(dpc, "_run", lambda *a, **k: _completed("", 2))
    assert dpc._cited_basenames() is None


def test_cited_basenames_strips_dirs_and_trailing_slash(dpc, monkeypatch):
    monkeypatch.setattr(dpc, "_run", lambda *a, **k: _completed(
        "agents/a/temp/x.py\nagents/b/temp/sub/\n\n"))
    assert dpc._cited_basenames() == {"x.py", "sub"}


# ── temp-durable-copy ─────────────────────────────────────────────────────────

def test_temp_file_without_durable_copy_fails(dpc, monkeypatch, capsys, tmp_path):
    root = tmp_path / "agents"
    (root / "a" / "temp").mkdir(parents=True)
    (root / "a" / "temp" / "naked.bin").write_text("x", encoding="utf-8")
    monkeypatch.setattr(dpc, "_agents_root", lambda: root)
    monkeypatch.setattr(dpc, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(dpc, "_git_tracked", lambda rels: set())
    monkeypatch.setitem(sys.modules, "storage_backend", None)  # force remote unavailable
    args = types.SimpleNamespace(sample=8)
    assert dpc.check_temp_durable_copy(args) == 1
    out = capsys.readouterr().out
    assert out.startswith("FAIL:")
    assert "ZERO durable copies" in out


def test_git_tracked_temp_file_passes(dpc, monkeypatch, capsys, tmp_path):
    root = tmp_path / "agents"
    (root / "a" / "temp").mkdir(parents=True)
    f = root / "a" / "temp" / "kept.md"
    f.write_text("x", encoding="utf-8")
    monkeypatch.setattr(dpc, "_agents_root", lambda: root)
    monkeypatch.setattr(dpc, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(dpc, "_git_tracked",
                        lambda rels: {str(f.relative_to(tmp_path))})
    monkeypatch.setitem(sys.modules, "storage_backend", None)
    args = types.SimpleNamespace(sample=8)
    assert dpc.check_temp_durable_copy(args) == 0
    assert "PASS: temp-durable-copy" in capsys.readouterr().out


def test_backend_without_remote_surface_is_reported_as_no_remote(
        dpc, monkeypatch, capsys, tmp_path):
    """A backend with no object surface means NO REMOTE, not an empty remote.

    Regression pin for a bug in this script's first version, caught by the
    fresh-eyes dispatch on its own goal. get_backend() SUCCEEDS under a local
    backend, so the old code set remote_ok = set() and then failed every
    per-file probe on a missing .s3 attribute — leaving an empty set, which is
    indistinguishable from "the remote was consulted and holds none of them".
    Measured: a bare STORAGE_BACKEND=local run reported 7 of 8 files "not
    present in the configured remote" on a box that has no configured remote,
    while the own-cloud run passed 8 of 8 on the same files. The FAIL verdict is
    correct there (a gitignored temp file on a local backend really has no
    durable copy) — the ATTRIBUTED CAUSE was not, and it points a reader at a
    sync failure that cannot exist.
    """
    root = tmp_path / "agents"
    (root / "a" / "temp").mkdir(parents=True)
    (root / "a" / "temp" / "scratch.bin").write_text("x", encoding="utf-8")

    class Localish:  # no .s3, no ._s3_key — the shape of a local backend
        pass

    fake = types.ModuleType("storage_backend")
    fake.get_backend = lambda: Localish()
    monkeypatch.setitem(sys.modules, "storage_backend", fake)
    monkeypatch.setattr(dpc, "_agents_root", lambda: root)
    monkeypatch.setattr(dpc, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(dpc, "_git_tracked", lambda rels: set())

    args = types.SimpleNamespace(sample=8)
    assert dpc.check_temp_durable_copy(args) == 1
    out = capsys.readouterr().out
    assert "NO remote was consultable" in out, out
    assert "not present in the configured remote" not in out, (
        "must not blame a remote that does not exist: %s" % out)


def test_total_remote_outage_is_not_reported_as_a_durability_hole(
        dpc, monkeypatch, capsys, tmp_path):
    """An unreachable remote must not read as "the objects are absent".

    Second fresh-eyes finding on this file. The per-file probe swallowed every
    exception, so a backend that HAS a remote surface but cannot reach it
    produced an empty remote_ok — identical to a successful probe that found
    nothing — and the check blamed durability for a connectivity fault.
    Measured: a backend whose head_object always raises reported "1 of 1
    sampled temp file(s) have ZERO durable copies".

    A genuine all-absent result is possible and is indistinguishable from an
    outage at this layer, so the fail-safe direction is to report that we could
    not measure. Partial failures still count as absent — only a TOTAL wipeout
    trips this.
    """
    root = tmp_path / "agents"
    (root / "a" / "temp").mkdir(parents=True)
    (root / "a" / "temp" / "f.bin").write_text("x", encoding="utf-8")

    class Down:
        bucket = "b"

        class _S3:
            def head_object(self, **kw):
                raise RuntimeError("connection reset by peer")

        s3 = _S3()

        def _s3_key(self, p):
            return "k/" + p.name

    fake = types.ModuleType("storage_backend")
    fake.get_backend = lambda: Down()
    monkeypatch.setitem(sys.modules, "storage_backend", fake)
    monkeypatch.setattr(dpc, "_agents_root", lambda: root)
    monkeypatch.setattr(dpc, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(dpc, "_git_tracked", lambda rels: set())

    assert dpc.check_temp_durable_copy(types.SimpleNamespace(sample=8)) == 1
    out = capsys.readouterr().out
    assert "NO remote was consultable" in out, out
    assert "every remote probe failed" in out, out


def test_zero_temp_files_is_vacuous_not_clean(dpc, monkeypatch, capsys, tmp_path):
    root = tmp_path / "agents"
    root.mkdir()
    monkeypatch.setattr(dpc, "_agents_root", lambda: root)
    args = types.SimpleNamespace(sample=8)
    assert dpc.check_temp_durable_copy(args) == 1
    assert "vacuous zero" in capsys.readouterr().out


def test_dotfiles_are_not_sampled(dpc, monkeypatch, tmp_path):
    """temp/'s only tracked file is a 0-byte .gitkeep; dotfiles are not artifacts."""
    root = tmp_path / "agents"
    (root / "a" / "temp").mkdir(parents=True)
    (root / "a" / "temp" / ".gitkeep").write_text("", encoding="utf-8")
    monkeypatch.setattr(dpc, "_agents_root", lambda: root)
    args = types.SimpleNamespace(sample=8)
    # only the dotfile exists => the sampler sees nothing => vacuous, not clean
    assert dpc.check_temp_durable_copy(args) == 1


# ── tree-node-recoverable ─────────────────────────────────────────────────────

def _seed_world(tmp_path, n_nodes, n_with_history, snapshots_layout=True):
    world = tmp_path / "world"
    tree = world / "knowledge" / "tree"
    tree.mkdir(parents=True)
    hist = world / ".history"
    for i in range(n_nodes):
        node = tree / ("node%d.md" % i)
        node.write_text("body", encoding="utf-8")
        if i < n_with_history:
            base = hist / "snapshots" if snapshots_layout else hist
            d = base / node.relative_to(world)
            d.mkdir(parents=True)
            (d / "2026-01-01T00-00-00_zeta.md").write_text("old", encoding="utf-8")
    return world


def test_regression_above_baseline_fails(dpc, monkeypatch, capsys, tmp_path):
    world = _seed_world(tmp_path, n_nodes=10, n_with_history=2)   # 8 missing
    monkeypatch.setattr(dpc, "_world_dir", lambda: world)
    monkeypatch.setattr(dpc, "_read_baseline", lambda key: 5)     # was 5, now 8
    args = types.SimpleNamespace(baseline_key="k")
    assert dpc.check_tree_node_recoverable(args) == 1
    out = capsys.readouterr().out
    assert out.startswith("FAIL:")
    assert "REGRESSED" in out


def test_at_baseline_is_stable(dpc, monkeypatch, capsys, tmp_path):
    world = _seed_world(tmp_path, n_nodes=10, n_with_history=2)   # 8 missing
    monkeypatch.setattr(dpc, "_world_dir", lambda: world)
    monkeypatch.setattr(dpc, "_read_baseline", lambda key: 8)
    args = types.SimpleNamespace(baseline_key="k")
    assert dpc.check_tree_node_recoverable(args) == 0
    assert "STABLE" in capsys.readouterr().out


def test_improvement_ratchets(dpc, monkeypatch, capsys, tmp_path):
    world = _seed_world(tmp_path, n_nodes=10, n_with_history=6)   # 4 missing
    monkeypatch.setattr(dpc, "_world_dir", lambda: world)
    monkeypatch.setattr(dpc, "_read_baseline", lambda key: 8)
    args = types.SimpleNamespace(baseline_key="k")
    assert dpc.check_tree_node_recoverable(args) == 0
    assert "RATCHETED" in capsys.readouterr().out


def test_no_baseline_seeds_without_failing(dpc, monkeypatch, capsys, tmp_path):
    world = _seed_world(tmp_path, n_nodes=4, n_with_history=0)
    monkeypatch.setattr(dpc, "_world_dir", lambda: world)
    monkeypatch.setattr(dpc, "_read_baseline", lambda key: None)
    args = types.SimpleNamespace(baseline_key="k")
    assert dpc.check_tree_node_recoverable(args) == 0
    assert "SEEDED" in capsys.readouterr().out


def test_missing_tree_is_not_clean(dpc, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(dpc, "_world_dir", lambda: tmp_path / "nope")
    args = types.SimpleNamespace(baseline_key="k")
    assert dpc.check_tree_node_recoverable(args) == 1
    assert "unmeasured" in capsys.readouterr().out


def test_empty_tree_is_vacuous_not_clean(dpc, monkeypatch, capsys, tmp_path):
    world = _seed_world(tmp_path, n_nodes=0, n_with_history=0)
    monkeypatch.setattr(dpc, "_world_dir", lambda: world)
    args = types.SimpleNamespace(baseline_key="k")
    assert dpc.check_tree_node_recoverable(args) == 1
    assert "vacuous zero" in capsys.readouterr().out


# ── platform: every bash invocation must go through bash_cmd (guard-580/581) ──

def test_no_bash_invocation_passes_a_str_path(dpc):
    """Source-level pin, because no behavioural test in this file can catch it.

    Every check here monkeypatches `_run`, so the argv this module actually
    builds is never executed under test — and the defect is invisible on Linux
    anyway, where `str(Path)` is already POSIX. It bites only on Windows, where
    bash treats the backslashes as escape introducers and strips them, so the
    script path silently becomes nonexistent, the wrapper "fails", and the check
    reports its could-not-measure FAIL for a reason that has nothing to do with
    the property (guard-581). A green suite on one OS is not evidence.

    Found by the g-306-116 fresh-eyes pass on this file's own commit: 3 of 3
    bash sites used `[BASH, str(...)]`, two of them added by that goal. Note
    check-no-bare-bash.py's fix hint prescribes bash_cmd for production and
    `[BASH, str(SCRIPT)]` for tests — so satisfying guard-580 by copying the
    second line reintroduces guard-581, which is how the pattern spread.
    """
    src = (SCRIPTS / "durability-property-check.py").read_text(encoding="utf-8")
    offenders = [ln.strip() for ln in src.splitlines()
                 if "[BASH," in ln and "str(" in ln and not ln.lstrip().startswith("#")]
    assert not offenders, (
        "guard-581: pass wrapper paths through bash_cmd(...), never [BASH, str(path)] — "
        "str(WindowsPath) reaches bash with backslashes and is silently stripped. "
        "Offending line(s): %r" % offenders)
    assert "bash_cmd(" in src, (
        "no bash_cmd call remains — if every wrapper invocation was removed this pin is "
        "vacuous and should be deleted deliberately, not left passing on an empty set.")


# ── integration path: the checks must actually be WIRED ───────────────────────

def test_all_three_checks_are_wired_into_verify_learning():
    """The checks must be INVOKED, not merely implemented.

    Surfaced by the sq-019 integration-path spark on this file's own goal, and
    it is the goal's own thesis turned on itself: every test above exercises the
    script in isolation, so deleting the three Bash lines from
    verify-learning/SKILL.md leaves all of them green while the checks silently
    stop running. That is exactly the defect this script exists to detect — a
    property guarded by a mechanism nothing verifies is active — reproduced one
    level up, in the wiring rather than in the thing wired.
    """
    skill = SCRIPTS.parents[1] / ".claude" / "skills" / "verify-learning" / "SKILL.md"
    assert skill.is_file(), "verify-learning/SKILL.md not found at %s" % skill
    # Corpus, not the file: the verify-learning check corpus moved to
    # core/config/verify-learning-checks.jsonl on 2026-08-18 ().
    # This canary pins a CALL SITE, and the call site moved with it.
    text = _verify_corpus.corpus_text()

    for sub in ("cited-temp-not-purged", "temp-durable-copy", "tree-node-recoverable",
                "held-key-still-listed", "deadman-armed", "agent-binding-effective"):
        needle = "durability-property-check.py %s" % sub
        assert needle in text, (
            "verify-learning/SKILL.md no longer invokes %r — the property check is "
            "implemented and tested but NO LONGER RUNS, which is the silent-inactive "
            "failure this whole file exists to prevent (g-306-103)." % needle)


def test_tree_node_baseline_entry_is_declared_in_verify_learning():
    """The ratchet is meaningless without its baseline, so the pairing is pinned.

    tree-node-recoverable SEEDS (and passes) when no baseline exists, which is
    the right behavior for a fresh world but means a silently-dropped baseline
    key would downgrade a live regression detector into a permanent no-op that
    still prints PASS.
    """
    skill = SCRIPTS.parents[1] / ".claude" / "skills" / "verify-learning" / "SKILL.md"
    # Corpus, not the file: the verify-learning check corpus moved to
    # core/config/verify-learning-checks.jsonl on 2026-08-18 ().
    # This canary pins a CALL SITE, and the call site moved with it.
    text = _verify_corpus.corpus_text()
    assert "tree_nodes_without_prior_version" in text, (
        "verify-learning/SKILL.md no longer declares the "
        "tree_nodes_without_prior_version baseline entry — without it the ratchet "
        "re-seeds forever and can never report a regression (g-306-103).")


@pytest.mark.parametrize("snapshots_layout", [True, False])
def test_both_history_layouts_are_recognised(dpc, monkeypatch, tmp_path, snapshots_layout):
    """Regression pin for the probe bug this check was born from.

    Bulk stores snapshot to .history/<rel>/, tree bodies to
    .history/snapshots/<rel>/. Probing only one layout reported 0 of 1321 nodes
    covered — a false 100% that a positive control caught on 2026-08-01. Both
    layouts must count, or the check invents a catastrophe.
    """
    world = _seed_world(tmp_path, n_nodes=4, n_with_history=4,
                        snapshots_layout=snapshots_layout)
    monkeypatch.setattr(dpc, "_world_dir", lambda: world)
    monkeypatch.setattr(dpc, "_read_baseline", lambda key: 0)
    args = types.SimpleNamespace(baseline_key="k")
    assert dpc.check_tree_node_recoverable(args) == 0


# ── held-key-still-listed ( A) ───────────────────────────────────────

def _held(*keys):
    return types.SimpleNamespace(held_key=list(keys) or None)


def test_held_key_present_passes(dpc, monkeypatch, capsys):
    """Positive control, and it asserts a SPECIFIC value (guard-2173).

    A rejection-only suite would pass identically over a lister that returns
    nothing at all, because a missing key and a blind lister are the same
    observation from the reject side. Pinning the live key COUNT in the PASS
    line is what a blind check cannot produce.
    """
    monkeypatch.setattr(dpc, "_lane_keys", lambda: ({"lane/a.json", "lane/b.json"}, ""))
    assert dpc.check_held_key_still_listed(_held("lane/a.json")) == 0
    out = capsys.readouterr().out
    assert out.startswith("PASS:")
    assert "2 live key" in out


def test_held_key_no_longer_listed_fails(dpc, monkeypatch, capsys):
    """The peer's measured incident: another world's drain archived a held key."""
    monkeypatch.setattr(dpc, "_lane_keys", lambda: ({"lane/b.json"}, ""))
    assert dpc.check_held_key_still_listed(_held("lane/a.json")) == 1
    assert "NO LONGER LISTED" in capsys.readouterr().out


def test_held_key_matches_on_bare_basename(dpc, monkeypatch, capsys):
    """A caller quoting the key without its lane prefix must not read as missing."""
    monkeypatch.setattr(dpc, "_lane_keys", lambda: ({"agent-inbox/a.json"}, ""))
    assert dpc.check_held_key_still_listed(_held("a.json")) == 0


def test_unlistable_lane_is_not_a_pass(dpc, monkeypatch, capsys):
    """could-not-measure MUST fail — with claims supplied AND without them.

    This is the whole point of the sub-check: while the lane cannot be listed,
    NO hold claim is verifiable, so an empty complaint list is empty for the
    wrong reason.
    """
    monkeypatch.setattr(dpc, "_lane_keys", lambda: (None, "lister exited 2"))
    assert dpc.check_held_key_still_listed(_held("lane/a.json")) == 1
    assert "UNMEASURED" in capsys.readouterr().out
    assert dpc.check_held_key_still_listed(_held()) == 1
    assert "UNMEASURED" in capsys.readouterr().out


def test_no_claims_passes_but_says_it_audited_nothing(dpc, monkeypatch, capsys):
    """The zero-claim PASS must not read as 'past holds were checked and clean'.

    Holds are not durably recorded (guard-2077 second half), so this run can only
    establish that a claim made NOW is verifiable. If that scope statement ever
    disappears the PASS becomes the reassuring-forever answer this file exists to
    prevent.
    """
    monkeypatch.setattr(dpc, "_lane_keys", lambda: ({"lane/a.json"}, ""))
    assert dpc.check_held_key_still_listed(_held()) == 0
    out = capsys.readouterr().out
    assert "NO PAST HOLD WAS AUDITED" in out
    assert "verifiability only" in out


def test_lane_keys_reports_missing_lister_rather_than_empty(dpc, monkeypatch, tmp_path):
    """A deployment with no lister must yield None, never an empty set."""
    monkeypatch.setattr(dpc, "_world_dir", lambda: tmp_path)
    keys, err = dpc._lane_keys()
    assert keys is None and "no inbound-lane lister" in err


def test_lane_keys_parses_the_key_column(dpc, monkeypatch, tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / dpc.LANE_LISTER).write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(dpc, "_world_dir", lambda: tmp_path)
    monkeypatch.setattr(dpc, "_run", lambda *a, **k: _completed(
        "2026-08-01T02:06:38+00:00 535 lane/a.json bucket\n"
        "2026-08-01T03:19:33+00:00 542 lane/b.json bucket\n"
        "short line\n"))
    keys, err = dpc._lane_keys()
    assert keys == {"lane/a.json", "lane/b.json"} and err == ""


def test_lane_keys_reports_lister_failure_rather_than_empty(dpc, monkeypatch, tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / dpc.LANE_LISTER).write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(dpc, "_world_dir", lambda: tmp_path)
    monkeypatch.setattr(dpc, "_run", lambda *a, **k: _completed("", returncode=2, stderr="boom"))
    keys, err = dpc._lane_keys()
    assert keys is None and "exited 2" in err


# ── deadman-armed ( B) ───────────────────────────────────────────────

def _seed_agents(tmp_path, resident=(), non_resident=(), disabled=()):
    root = tmp_path / "agents"
    for name in list(resident) + list(non_resident):
        (root / name / "session").mkdir(parents=True)
        if name in resident:
            (root / name / "local-paths.conf").write_text("x\n", encoding="utf-8")
        if name in disabled:
            (root / name / "session" / "deadman-disabled").write_text("", encoding="utf-8")
    return root


def test_deadman_armed_passes_and_names_the_examined_set(dpc, monkeypatch, tmp_path, capsys):
    """Positive control — and the PASS must NAME who it examined and who it did not.

    The property holds by ABSENCE, so a count alone is indistinguishable from a
    probe that looked nowhere. The named sets are the discriminator.
    """
    root = _seed_agents(tmp_path, resident=["bravo"], non_resident=["alpha", "zeta"])
    monkeypatch.setattr(dpc, "_agents_root", lambda: root)
    monkeypatch.setattr(dpc, "_live_roster", lambda: (["alpha", "bravo", "zeta"], ""))
    assert dpc.check_deadman_armed(None) == 0
    out = capsys.readouterr().out
    assert "1 of 3 live agent(s) examined" in out
    assert "alpha, zeta" in out
    assert "NOT a fleet-wide all-clear" in out


def test_deadman_disabled_file_fails(dpc, monkeypatch, tmp_path, capsys):
    root = _seed_agents(tmp_path, resident=["bravo"], disabled=["bravo"])
    monkeypatch.setattr(dpc, "_agents_root", lambda: root)
    monkeypatch.setattr(dpc, "_live_roster", lambda: (["bravo"], ""))
    assert dpc.check_deadman_armed(None) == 1
    assert "DISARMED" in capsys.readouterr().out


def test_deadman_zero_resident_agents_is_vacuous_not_clean(dpc, monkeypatch, tmp_path, capsys):
    """The exact defect this check was written against.

    The sentinel is machine-local and unsynced, so globbing every agent dir on
    one box answers 'armed' for agents whose file could never be here. With no
    resident agent there is nothing to be zero OF.
    """
    root = _seed_agents(tmp_path, non_resident=["alpha", "zeta"])
    monkeypatch.setattr(dpc, "_agents_root", lambda: root)
    monkeypatch.setattr(dpc, "_live_roster", lambda: (["alpha", "zeta"], ""))
    assert dpc.check_deadman_armed(None) == 1
    assert "vacuous zero" in capsys.readouterr().out


def test_deadman_unreadable_roster_is_not_a_pass(dpc, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(dpc, "_agents_root", lambda: tmp_path / "agents")
    monkeypatch.setattr(dpc, "_live_roster", lambda: (None, "team-state-read.sh exited 1"))
    assert dpc.check_deadman_armed(None) == 1
    assert "UNMEASURED" in capsys.readouterr().out


def test_retired_agents_are_excluded_from_the_roster(dpc, monkeypatch):
    """A tombstoned agent is not a live agent, and its box is gone ()."""
    monkeypatch.setattr(dpc, "_run", lambda *a, **k: _completed(json.dumps(
        {"agent_status": {"bravo": {"last_active": "x"},
                          "charlie": {"retired_at": "2026-07-01T00:00:00"}}})))
    roster, err = dpc._live_roster()
    assert roster == ["bravo"] and err == ""


def test_empty_roster_is_reported_as_unmeasurable(dpc, monkeypatch):
    monkeypatch.setattr(dpc, "_run", lambda *a, **k: _completed(json.dumps({"agent_status": {}})))
    roster, err = dpc._live_roster()
    assert roster is None and "EMPTY agent_status" in err


# ── agent-binding-effective ( C) ─────────────────────────────────────

def _fake_binding_module(agent, source="binding.yaml"):
    mod = types.ModuleType("_session_binding")
    mod.resolve_binding = lambda sid, root: (
        None if agent is None
        else types.SimpleNamespace(agent=agent, source=source, session_id=sid))
    return mod


def _bind_env(monkeypatch, agent="bravo", sid="a" * 36, shim=True):
    monkeypatch.setenv("MIND_AGENT", agent)
    monkeypatch.setenv("MIND_SID", sid)
    entries = [str(SCRIPTS / ".python-shim")] if shim else []
    monkeypatch.setenv("PATH", os.pathsep.join(entries + ["/usr/bin"]))


def test_binding_effective_passes(dpc, monkeypatch, capsys):
    """Positive control asserting the SPECIFIC resolved name, not just exit 0."""
    _bind_env(monkeypatch)
    monkeypatch.setitem(sys.modules, "_session_binding", _fake_binding_module("bravo"))
    assert dpc.check_agent_binding_effective(None) == 0
    out = capsys.readouterr().out
    assert "MIND_AGENT='bravo'" in out and "CONFIRMED" in out


def test_unset_agent_fails(dpc, monkeypatch, capsys):
    """The fail-open default is ABSENCE, which is why this is not vacuous."""
    _bind_env(monkeypatch)
    monkeypatch.delenv("MIND_AGENT")
    assert dpc.check_agent_binding_effective(None) == 1
    assert "unset inside this process" in capsys.readouterr().out


def test_unset_sid_is_uncorroborated_not_confirmed(dpc, monkeypatch, capsys):
    _bind_env(monkeypatch)
    monkeypatch.delenv("MIND_SID")
    assert dpc.check_agent_binding_effective(None) == 1
    assert "UNVERIFIED" in capsys.readouterr().out


def test_disagreeing_binding_fails_distinctly(dpc, monkeypatch, capsys):
    """The dangerous state: bound, succeeding, and writing to the wrong agent."""
    _bind_env(monkeypatch, agent="alpha")
    monkeypatch.setitem(sys.modules, "_session_binding", _fake_binding_module("bravo"))
    assert dpc.check_agent_binding_effective(None) == 1
    out = capsys.readouterr().out
    assert "DISAGREES" in out and "wrong agent" in out


def test_absent_binding_file_fails(dpc, monkeypatch, capsys):
    """An ambient export that corroborates nothing is not a live binding."""
    _bind_env(monkeypatch)
    monkeypatch.setitem(sys.modules, "_session_binding", _fake_binding_module(None))
    assert dpc.check_agent_binding_effective(None) == 1
    assert "no session binding exists" in capsys.readouterr().out


def test_missing_shim_on_path_fails(dpc, monkeypatch, capsys):
    """The other half of the same injection, and it fails open the same way."""
    _bind_env(monkeypatch, shim=False)
    monkeypatch.setitem(sys.modules, "_session_binding", _fake_binding_module("bravo"))
    assert dpc.check_agent_binding_effective(None) == 1
    assert "NOT on PATH" in capsys.readouterr().out


def test_binding_resolution_error_is_not_a_pass(dpc, monkeypatch, capsys):
    _bind_env(monkeypatch)
    mod = types.ModuleType("_session_binding")

    def _boom(sid, root):
        raise RuntimeError("unreadable")

    mod.resolve_binding = _boom
    monkeypatch.setitem(sys.modules, "_session_binding", mod)
    assert dpc.check_agent_binding_effective(None) == 1
    assert "UNCORROBORATED" in capsys.readouterr().out
