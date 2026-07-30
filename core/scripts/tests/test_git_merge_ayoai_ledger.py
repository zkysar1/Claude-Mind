"""Tests for the merge=ayoai-ledger git merge driver ().

Two layers:
  1. merge_bytes() dispatch unit tests — each basename routes to the right
     commutative primitive, and unregistered basenames raise (=> exit 1).
  2. A REAL end-to-end git-merge integration test — a temp repo with the
     driver configured, a genuine cross-branch conflict on experience.jsonl,
     and `git merge` proving git invokes the driver and self-heals the
     conflict instead of aborting.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import importlib.util

_DRIVER_PY = os.path.join(_SCRIPTS, "git-merge-ayoai-ledger.py")
_spec = importlib.util.spec_from_file_location("git_merge_ayoai_ledger", _DRIVER_PY)
drv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drv)


def _jsonl(*records):
    return ("\n".join(json.dumps(r) for r in records) + "\n").encode("utf-8")


def _lines(b):
    return [json.loads(x) for x in b.decode().splitlines() if x.strip()]


# ── Layer 1: merge_bytes dispatch ────────────────────────────────────────────

def test_experience_id_union_dedups_by_id():
    ours = _jsonl({"id": "exp-A", "s": "a"}, {"id": "exp-B", "s": "b1"})
    theirs = _jsonl({"id": "exp-B", "s": "b2"}, {"id": "exp-C", "s": "c"})
    out = _lines(drv.merge_bytes("agents/x/experience.jsonl", ours, theirs))
    ids = sorted(r["id"] for r in out)
    assert ids == ["exp-A", "exp-B", "exp-C"]  # union, exp-B deduped to one


def test_experience_id_union_is_commutative():
    ours = _jsonl({"id": "exp-A", "s": "a"}, {"id": "exp-B", "s": "b1"})
    theirs = _jsonl({"id": "exp-B", "s": "b2"}, {"id": "exp-C", "s": "c"})
    ab = drv.merge_bytes("agents/x/experience.jsonl", ours, theirs)
    ba = drv.merge_bytes("agents/x/experience.jsonl", theirs, ours)
    assert ab == ba  # commutative — both boxes converge to the same bytes


def test_experience_archive_also_id_union():
    ours = _jsonl({"id": "exp-A"}, {"id": "exp-B"})
    theirs = _jsonl({"id": "exp-C"})
    out = _lines(drv.merge_bytes("agents/x/experience-archive.jsonl", ours, theirs))
    assert sorted(r["id"] for r in out) == ["exp-A", "exp-B", "exp-C"]


def test_journal_no_id_degrades_to_canon_union_no_dataloss():
    # journal.jsonl has no 'id' -> canon-union: keep ALL distinct lines,
    # dedup EXACT duplicates. Distinct entries sharing goal_id are NOT collapsed.
    ours = _jsonl({"goal_id": "g-1", "e": "x"}, {"goal_id": "g-1", "e": "y"})
    theirs = _jsonl({"goal_id": "g-1", "e": "y"}, {"goal_id": "g-2", "e": "z"})
    out = _lines(drv.merge_bytes("agents/x/journal.jsonl", ours, theirs))
    # 3 distinct entries; the shared {g-1,e:y} line deduped once.
    assert len(out) == 3


def test_experience_meta_counter_max():
    ours = b'{"total_live": 100, "total_archived": 10}'
    theirs = b'{"total_live": 105, "total_archived": 8}'
    out = json.loads(drv.merge_bytes("agents/x/experience-meta.json", ours, theirs))
    assert out["total_live"] == 105  # MAX — a counter only grows
    assert out["total_archived"] == 10


def test_changelog_routes_to_registry():
    # changelog.jsonl IS registered in coordination_merge._HANDLERS
    # (merge_append_only_jsonl) — merge_bytes must fall through to it.
    ours = _jsonl({"at": "t1", "op": "a"})
    theirs = _jsonl({"at": "t2", "op": "b"})
    out = _lines(drv.merge_bytes("agents/x/changelog.jsonl", ours, theirs))
    assert len(out) == 2  # append-only union keeps both


def test_aspirations_routes_to_registry():
    import coordination_merge as cm
    assert cm.merge_handler_for("agents/x/aspirations.jsonl") is cm.merge_aspirations


def test_unregistered_basename_raises():
    with pytest.raises(Exception):
        drv.merge_bytes("agents/x/random-unknown.jsonl", b"{}", b"{}")


def test_empty_side_ours_empty():
    ours = b""
    theirs = _jsonl({"id": "exp-C"})
    out = _lines(drv.merge_bytes("agents/x/experience.jsonl", ours, theirs))
    assert [r["id"] for r in out] == ["exp-C"]


# ── main() with real files ───────────────────────────────────────────────────

def test_main_writes_merged_to_ours(tmp_path):
    base = tmp_path / "base"; base.write_bytes(b"")
    ours = tmp_path / "ours"; ours.write_bytes(_jsonl({"id": "exp-A"}))
    theirs = tmp_path / "theirs"; theirs.write_bytes(_jsonl({"id": "exp-B"}))
    rc = drv.main(["prog", str(base), str(ours), str(theirs),
                   "agents/x/experience.jsonl"])
    assert rc == 0
    assert sorted(r["id"] for r in _lines(ours.read_bytes())) == ["exp-A", "exp-B"]


def test_main_unregistered_returns_1_and_leaves_ours_untouched(tmp_path):
    base = tmp_path / "base"; base.write_bytes(b"")
    ours = tmp_path / "ours"; ours.write_bytes(b"OURS-ORIGINAL")
    theirs = tmp_path / "theirs"; theirs.write_bytes(b"THEIRS")
    rc = drv.main(["prog", str(base), str(ours), str(theirs),
                   "agents/x/not-a-ledger.jsonl"])
    assert rc == 1
    assert ours.read_bytes() == b"OURS-ORIGINAL"  # never corrupt on failure


# ── Layer 2: real git-merge integration ──────────────────────────────────────

def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)


@pytest.mark.skipif(subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
                    reason="git not available")
def test_live_git_merge_experience_selfheals(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    # Wire the driver as install-git-hooks.sh does, but POSIX-form ().
    # os.path.join yields backslashes on Windows, and git hands the driver command
    # to a SHELL — bash then treats each \X as an escape and CONSUMES it, so
    # C:\<WORKSPACE>\GitHub\... arrived as C:<WORKSPACE>GitHub... and the merge
    # aborted with "No such file or directory". That reads as "driver not
    # invoked", i.e. a product bug, and is not one: guard-581 / rb-577, the same
    # class this repo already fixed in _runtime_bash.bash_cmd via .as_posix().
    #
    # Note production is NOT affected — install-git-hooks.sh:54 configures a
    # RELATIVE posix path ('bash core/scripts/git-merge-ayoai-ledger.sh ...'),
    # which has no backslashes to eat. So the comment above used to claim parity
    # with install-git-hooks.sh while actually diverging from it in the one
    # detail that matters (guard-920: replicate the literal production shape).
    wrapper = Path(_SCRIPTS, "git-merge-ayoai-ledger.sh").as_posix()
    _git(repo, "config", "merge.ayoai-ledger.driver",
         f'bash {wrapper} %O %A %B %P')
    (repo / ".gitattributes").write_text(
        "agents/*/experience.jsonl merge=ayoai-ledger\n")
    led = repo / "agents" / "x"; led.mkdir(parents=True)
    exp = led / "experience.jsonl"
    exp.write_bytes(_jsonl({"id": "exp-base"}))
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "base")
    # Branch ours: append exp-O
    _git(repo, "checkout", "-qb", "ours")
    exp.write_bytes(_jsonl({"id": "exp-base"}, {"id": "exp-O"}))
    _git(repo, "commit", "-qam", "ours")
    # Branch theirs: append exp-T (from base)
    _git(repo, "checkout", "-q", "master") if _git(repo, "rev-parse", "--verify", "master").returncode == 0 else _git(repo, "checkout", "-q", "main")
    _git(repo, "checkout", "-qb", "theirs")
    exp.write_bytes(_jsonl({"id": "exp-base"}, {"id": "exp-T"}))
    _git(repo, "commit", "-qam", "theirs")
    # Merge ours into theirs — WITHOUT the driver this line-level conflict aborts.
    _git(repo, "checkout", "-q", "ours")
    res = _git(repo, "merge", "theirs", "-m", "merge")
    assert res.returncode == 0, f"merge aborted (driver not invoked?): {res.stderr}"
    ids = sorted(r["id"] for r in _lines(exp.read_bytes()))
    assert ids == ["exp-O", "exp-T", "exp-base"]  # union of both sides, deduped base


def _init_ledger_repo(tmp_path, attributes: str):
    """Temp repo with the ayoai-ledger driver wired exactly as the test above
    does (see its comment for why the wrapper path must be POSIX-form)."""
    repo = tmp_path / "repo"; repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    wrapper = Path(_SCRIPTS, "git-merge-ayoai-ledger.sh").as_posix()
    _git(repo, "config", "merge.ayoai-ledger.driver", f'bash {wrapper} %O %A %B %P')
    (repo / ".gitattributes").write_text(attributes)
    return repo


def _diverge(repo, path, base: bytes, ours: bytes, theirs: bytes):
    """base -> two branches writing conflicting content -> merge theirs into
    ours. Returns the CompletedProcess of the merge."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base)
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "base")
    trunk = "master" if _git(repo, "rev-parse", "--verify",
                             "master").returncode == 0 else "main"
    _git(repo, "checkout", "-qb", "ours")
    path.write_bytes(ours); _git(repo, "commit", "-qam", "ours")
    _git(repo, "checkout", "-q", trunk)
    _git(repo, "checkout", "-qb", "theirs")
    path.write_bytes(theirs); _git(repo, "commit", "-qam", "theirs")
    _git(repo, "checkout", "-q", "ours")
    return _git(repo, "merge", "theirs", "-m", "merge")


@pytest.mark.skipif(subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
                    reason="git not available")
def test_live_git_merge_strategy_generations_selfheals(tmp_path):
    """ criterion 3, as a REAL merge rather than a unit test of the
    handler (guard-1290: a .gitattributes entry plus a passing unit test proves
    nothing about what git actually does).

    Reproduces the wedge that was measured on two live omni bodies: both boxes
    advance the SAME open generation, so the tail row conflicts at line level.
    """
    yaml = pytest.importorskip("yaml")

    def sg(goals, total):
        return yaml.dump({
            "version": 1, "current_generation": 42,
            "generations": [{"generation": 42, "started": "2026-07-28T05:59:34",
                             "ended": None, "goals_completed": goals,
                             "metrics": {"avg_learning_value": round(total / goals, 4),
                                         "total_learning_value": total}}],
            "peak_generation": 40, "peak_score": 0.9,
        }, default_flow_style=False, sort_keys=False).encode("utf-8")

    repo = _init_ledger_repo(tmp_path, ".mind-data/meta/**/*.yaml merge=ayoai-ledger\n")
    gen = repo / ".mind-data" / "meta" / "strategy-generations.yaml"
    res = _diverge(repo, gen, sg(100, 50.0), sg(158, 79.0), sg(212, 106.0))

    # Name BOTH causes. "wedge not fixed" alone would misattribute a broken
    # harness (driver never registered, temp repo not initialised) as a product
    # bug, which is the exact misreading this suite exists to prevent.
    assert res.returncode == 0, (
        f"merge aborted — either the handler is missing or the driver was never "
        f"registered in this temp repo: {res.stderr}")
    # The goal's named diagnostic: a wedged path stays in unmerged index state
    # while its working-tree copy has ZERO conflict markers, so a marker grep
    # reports the file as fine. Assert on the index, never on the content.
    unmerged = _git(repo, "diff", "--name-only", "--diff-filter=U").stdout.strip()
    assert unmerged == "", f"path left in unmerged index state: {unmerged}"

    m = yaml.safe_load(gen.read_text())
    g = m["generations"][0]
    assert g["goals_completed"] == 212                              # MAX, not 370
    assert g["metrics"]["avg_learning_value"] == round(106.0 / 212, 4)  # recomputed
    assert m["current_generation"] == 42


@pytest.mark.skipif(subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
                    reason="git not available")
def test_live_git_merge_unregistered_basename_still_fails_safe(tmp_path):
    """ criterion 4. Registering a new basename must not weaken the
    no-corruption guarantee for the ones still unregistered: the driver exits 1,
    git keeps the conflict, and %A is never silently overwritten with one side.
    """
    repo = _init_ledger_repo(tmp_path, ".mind-data/meta/**/*.yaml merge=ayoai-ledger\n")
    unknown = repo / ".mind-data" / "meta" / "no-handler-for-this.yaml"
    res = _diverge(repo, unknown, b"k: base\n", b"k: ours\n", b"k: theirs\n")

    assert res.returncode != 0, "unregistered basename merged — fail-safe weakened"
    unmerged = _git(repo, "diff", "--name-only", "--diff-filter=U").stdout.strip()
    assert unmerged.endswith("no-handler-for-this.yaml")
