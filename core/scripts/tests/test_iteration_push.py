"""test_iteration_push.py — bidirectional GitHub-sync coverage (daemon-safe).

Black-box subprocess tests of iteration-push.sh against HERMETIC tmp git repos:
a bare "origin" plus two clones (A = this machine, B = the other machine).
Never touches the real repo's remotes, never hits the network (file:// clones).

The core scenario under test is the 2026-07-03 multi-machine wedge: machine B
pushes first, machine A's push turns non-fast-forward, and pre-fix the script
retried the same doomed push forever. Post-fix it fetches, merges (never
rebases, never forces), and pushes the merge — both machines' commits reach
origin regardless of which computer they were made on.

Also covers the .gitattributes merge=union lane for APPEND-ONLY agent ledgers
(skill-invocations.jsonl, health/*.jsonl): same-agent-two-machines EOF appends
self-resolve instead of wedging the integrate step.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
for _p in (str(CORE_SCRIPTS), str(SCRIPT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _bash_helpers import BASH  # noqa: E402

PUSH_SH = CORE_SCRIPTS / "iteration-push.sh"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=60,
    )


def _must(repo: Path, *args: str) -> str:
    r = _git(repo, *args)
    assert r.returncode == 0, f"git {' '.join(args)} failed: {r.stderr}"
    return r.stdout.strip()


def _commit_file(repo: Path, rel: str, content: str, msg: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8", newline="\n")
    _must(repo, "add", rel)
    _must(repo, "commit", "-q", "-m", msg, "--", rel)


def _tip(repo: Path, ref: str = "HEAD") -> str:
    return _must(repo, "rev-parse", ref)


def _clone_pair(tmp_path: Path):
    """bare origin + two configured clones with one shared base commit."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                   capture_output=True, text=True, check=True)
    clones = []
    for name in ("cloneA", "cloneB"):
        c = tmp_path / name
        subprocess.run(["git", "clone", "-q", str(origin), str(c)],
                       capture_output=True, text=True, check=True)
        _must(c, "config", "user.name", f"test-{name}")
        _must(c, "config", "user.email", f"{name}@test.local")
        clones.append(c)
    a, b = clones
    _commit_file(a, "base.txt", "base\n", "base commit")
    _must(a, "push", "-q", "-u", "origin", "main")
    _must(b, "pull", "-q", "origin", "main")
    _must(b, "branch", "-q", "--set-upstream-to=origin/main", "main")
    return origin, a, b


def _run_push(repo: Path, *flags: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [BASH, str(PUSH_SH), "--repo", str(repo), *flags],
        capture_output=True, text=True, timeout=120,
    )


def _default_flags(*extra: str) -> list:
    # min-commits 1 + fetch-interval 0 => deterministic push + always-fetch
    return ["--min-commits", "1", "--fetch-interval-min", "0", *extra]


# --------------------------------------------------------------------------- #
# basic push behavior (pre-existing contract)
# --------------------------------------------------------------------------- #
def test_basic_push_when_ahead(tmp_path):
    origin, a, b = _clone_pair(tmp_path)
    _commit_file(a, "f1.txt", "one\n", "A: f1")
    r = _run_push(a, *_default_flags("--strict"))
    assert r.returncode == 0, r.stderr
    assert "push OK" in r.stderr
    assert _must(a, "rev-parse", "origin/main") == _tip(a)


def test_throttled_below_thresholds(tmp_path):
    origin, a, b = _clone_pair(tmp_path)
    _commit_file(a, "f1.txt", "one\n", "A: f1")
    # fresh commit, min-commits 5, age floor 20m => defer
    r = _run_push(a, "--min-commits", "5", "--max-age-min", "20",
                  "--no-fetch", "--strict")
    assert r.returncode == 0
    assert "throttled" in r.stderr
    # origin unchanged (still at base)
    assert _must(a, "rev-parse", "origin/main") != _tip(a)


def test_up_to_date_noop(tmp_path):
    origin, a, b = _clone_pair(tmp_path)
    r = _run_push(a, *_default_flags("--strict"))
    assert r.returncode == 0
    assert "nothing to push" in r.stderr


# --------------------------------------------------------------------------- #
# the multi-machine wedge (fetch + integrate)
# --------------------------------------------------------------------------- #
def test_nonff_heals_by_merge_then_push(tmp_path):
    """THE core fix: B pushed first; A must fetch+merge+push, not wedge."""
    origin, a, b = _clone_pair(tmp_path)
    _commit_file(b, "from_b.txt", "b\n", "B: change")
    _must(b, "push", "-q", "origin", "main")
    _commit_file(a, "from_a.txt", "a\n", "A: change")

    r = _run_push(a, *_default_flags("--strict"))
    assert r.returncode == 0, f"stderr: {r.stderr}"
    assert "integrating" in r.stderr and "push OK" in r.stderr

    # origin now contains BOTH machines' work; A fully converged (0/0)
    _must(a, "fetch", "-q", "origin", "main")
    counts = _must(a, "rev-list", "--left-right", "--count",
                   "origin/main...main")
    assert counts.split() == ["0", "0"], f"not converged: {counts}"
    files = _must(a, "ls-tree", "-r", "--name-only", "origin/main")
    assert "from_a.txt" in files and "from_b.txt" in files


def test_behind_only_fast_forwards_without_push(tmp_path):
    """A has nothing to push but B pushed: A must still catch up (fetch side)."""
    origin, a, b = _clone_pair(tmp_path)
    _commit_file(b, "from_b.txt", "b\n", "B: change")
    _must(b, "push", "-q", "origin", "main")

    r = _run_push(a, *_default_flags("--strict"))
    assert r.returncode == 0, r.stderr
    assert "integrating" in r.stderr and "nothing to push" in r.stderr
    # A's main advanced to B's commit (fast-forward, no merge bubble)
    assert _tip(a) == _must(a, "rev-parse", "origin/main")
    assert (a / "from_b.txt").exists()


def test_fetch_throttle_skips_fetch(tmp_path):
    origin, a, b = _clone_pair(tmp_path)
    # prime FETCH_HEAD (fresh)
    _must(a, "fetch", "origin", "main")
    r = _run_push(a, "--min-commits", "1", "--fetch-interval-min", "60",
                  "--strict")
    assert r.returncode == 0
    assert "fetch throttled" in r.stderr


def test_true_conflict_aborts_cleanly(tmp_path):
    """RMW-file conflict: abort the merge, leave the tree pristine, fail-soft."""
    origin, a, b = _clone_pair(tmp_path)
    _commit_file(b, "base.txt", "B version\n", "B: rewrite base")
    _must(b, "push", "-q", "origin", "main")
    _commit_file(a, "base.txt", "A version\n", "A: rewrite base")
    a_tip_before = _tip(a)

    r_strict = _run_push(a, *_default_flags("--strict"))
    assert r_strict.returncode == 1          # strict surfaces the failure
    assert "MERGE CONFLICT" in r_strict.stderr

    # no mid-merge state left behind; local commit intact; tree clean
    assert not (a / ".git" / "MERGE_HEAD").exists()
    assert _tip(a) == a_tip_before
    assert _must(a, "status", "--porcelain") == ""
    # origin untouched (B's tip)
    assert _must(a, "rev-parse", "origin/main") == _tip(b)

    # non-strict: same behavior but exit 0 (never blocks the loop)
    r_soft = _run_push(a, *_default_flags())
    assert r_soft.returncode == 0


def test_dry_run_mutates_nothing(tmp_path):
    origin, a, b = _clone_pair(tmp_path)
    _commit_file(b, "from_b.txt", "b\n", "B: change")
    _must(b, "push", "-q", "origin", "main")
    _commit_file(a, "from_a.txt", "a\n", "A: change")
    _must(a, "fetch", "-q", "origin", "main")   # make A aware of divergence
    a_tip_before = _tip(a)
    origin_tip_before = _must(a, "rev-parse", "origin/main")

    r = _run_push(a, "--min-commits", "1", "--dry-run", "--strict")
    assert r.returncode == 0
    assert "WOULD merge" in r.stderr
    assert _tip(a) == a_tip_before                       # no merge commit
    _must(a, "fetch", "-q", "origin", "main")
    assert _must(a, "rev-parse", "origin/main") == origin_tip_before  # no push


def test_no_fetch_skips_integrate(tmp_path):
    """--no-fetch preserves the old push-only behavior under an explicit flag."""
    origin, a, b = _clone_pair(tmp_path)
    _commit_file(b, "from_b.txt", "b\n", "B: change")
    _must(b, "push", "-q", "origin", "main")
    _commit_file(a, "from_a.txt", "a\n", "A: change")
    a_tip_before = _tip(a)

    r = _run_push(a, "--min-commits", "1", "--no-fetch", "--strict")
    assert r.returncode == 1                 # non-ff push fails (old wedge)
    assert "push FAILED" in r.stderr
    assert _tip(a) == a_tip_before           # and nothing was merged


# --------------------------------------------------------------------------- #
# merge=union lane for append-only agent ledgers
# --------------------------------------------------------------------------- #
def test_union_merge_selfresolves_appendonly(tmp_path):
    """Same agent appends the same ledger on two machines: union keeps both."""
    origin, a, b = _clone_pair(tmp_path)
    # hermetic copy of the real repo's union rules
    _commit_file(a, ".gitattributes",
                 "agents/*/skill-invocations.jsonl merge=union\n"
                 "agents/*/health/*.jsonl merge=union\n",
                 "attrs")
    _commit_file(a, "agents/x/skill-invocations.jsonl",
                 '{"n":1}\n', "ledger base")
    _must(a, "push", "-q", "origin", "main")
    _must(b, "pull", "-q", "origin", "main")

    # both machines append at EOF from the same base
    ledger = "agents/x/skill-invocations.jsonl"
    (b / ledger).write_text('{"n":1}\n{"n":"from-b"}\n',
                            encoding="utf-8", newline="\n")
    _must(b, "add", ledger)
    _must(b, "commit", "-q", "-m", "B: append", "--", ledger)
    _must(b, "push", "-q", "origin", "main")

    (a / ledger).write_text('{"n":1}\n{"n":"from-a"}\n',
                            encoding="utf-8", newline="\n")
    _must(a, "add", ledger)
    _must(a, "commit", "-q", "-m", "A: append", "--", ledger)

    r = _run_push(a, *_default_flags("--strict"))
    assert r.returncode == 0, f"union merge should self-resolve: {r.stderr}"
    assert "push OK" in r.stderr
    merged = (a / ledger).read_text(encoding="utf-8")
    assert '{"n":1}' in merged
    assert '{"n":"from-a"}' in merged and '{"n":"from-b"}' in merged


def test_real_repo_union_scope_is_evidence_gated():
    """Read-only probe of the REAL .gitattributes: union ONLY where append-only
    was proven (zero historical deleted lines); RMW stores stay unspecified."""
    paths = {
        "agents/alpha/skill-invocations.jsonl": "union",
        "agents/alpha/health/2026-01-01.jsonl": "union",
        "agents/alpha/journal.jsonl": "unspecified",       # index rewrites
        "agents/alpha/changelog.jsonl": "unspecified",     # pruning
        "agents/alpha/experience.jsonl": "unspecified",    # archival
        "agents/alpha/aspirations.jsonl": "unspecified",   # RMW status updates
    }
    r = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "check-attr", "merge", "--",
         *paths.keys()],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0, r.stderr
    for line in r.stdout.strip().splitlines():
        path, _, value = (s.strip() for s in line.split(":", 2))
        assert value == paths[path.replace("\\", "/")], line


# --------------------------------------------------------------------------- #
# cross-agent-churn self-heal for the dirty-tree merge deadlock (3)
#
# The deadlock (per 3): origin advances a file that THIS machine has
# UNSTAGED cross-agent churn on (agents/<other>/* — owncloud re-materialised
# sibling state the namespace filter refuses to commit). `git merge` refuses
# BEFORE starting (no MERGE_HEAD), the tree is deferred, and owncloud re-creates
# the same churn every cycle so it never self-heals. The fix clears ONLY the
# blocking cross-agent churn and retries the merge once — but NEVER touches
# staged entries (guard-741: a concurrent agent's in-flight staged work) nor any
# file outside agents/<other>/* (self/core/world). All-or-nothing: any
# non-clearable file in the blocking set defers the WHOLE tree untouched.
# --------------------------------------------------------------------------- #
def _run_push_env(repo: Path, agent: str, *flags: str) -> subprocess.CompletedProcess:
    """_run_push with MIND_AGENT set — the script reads it to identify 'self'."""
    return subprocess.run(
        [BASH, str(PUSH_SH), "--repo", str(repo), *flags],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "MIND_AGENT": agent},
    )


def _seed_and_sync(a: Path, b: Path, files: dict) -> None:
    """Commit tracked files from A, push to origin, pull into B (shared base)."""
    for rel, content in files.items():
        _commit_file(a, rel, content, f"seed {rel}")
    _must(a, "push", "-q", "origin", "main")
    _must(b, "pull", "-q", "origin", "main")


def test_selfheal_dirty_tracked_crossagent_clears_and_merges(tmp_path):
    """t1: unstaged tracked agents/<other>/* churn overlapping the merge is
    cleared; the merge retries and succeeds; A's own commit survives; the
    cross-agent file converges to origin's version."""
    origin, a, b = _clone_pair(tmp_path)
    _seed_and_sync(a, b, {"agents/bravo/state.jsonl": "v1\n"})
    # B advances the cross-agent file at origin
    _commit_file(b, "agents/bravo/state.jsonl", "v2-from-b\n", "B: bravo v2")
    _must(b, "push", "-q", "origin", "main")
    # A has its own framework commit to push
    _commit_file(a, "core/scripts/foo.sh", "echo hi\n", "A: framework work")
    # ...plus UNSTAGED cross-agent churn overlapping the incoming merge
    (a / "agents/bravo/state.jsonl").write_text("dirty-churn\n",
                                                encoding="utf-8", newline="\n")

    r = _run_push_env(a, "alpha", *_default_flags("--strict"))
    assert r.returncode == 0, f"stderr: {r.stderr}"
    assert "self-heal: clearing" in r.stderr
    assert "after cross-agent-churn self-heal" in r.stderr
    assert "push OK" in r.stderr

    # A fully converged; origin holds BOTH A's commit and B's bravo change
    _must(a, "fetch", "-q", "origin", "main")
    counts = _must(a, "rev-list", "--left-right", "--count", "origin/main...main")
    assert counts.split() == ["0", "0"], f"not converged: {counts}"
    tree = _must(a, "ls-tree", "-r", "--name-only", "origin/main")
    assert "core/scripts/foo.sh" in tree and "agents/bravo/state.jsonl" in tree
    # churn discarded → the file is origin's committed version, tree clean
    assert (a / "agents/bravo/state.jsonl").read_text() == "v2-from-b\n"
    assert _must(a, "status", "--porcelain") == ""


def test_selfheal_untracked_crossagent_clears_and_merges(tmp_path):
    """t2: an UNTRACKED agents/<other>/* file that collides with an incoming
    origin ADD is removed; the merge retries and brings origin's version."""
    origin, a, b = _clone_pair(tmp_path)
    # B adds a NEW cross-agent file at origin
    _commit_file(b, "agents/bravo/new.jsonl", "from-b\n", "B: new bravo file")
    _must(b, "push", "-q", "origin", "main")
    _commit_file(a, "core/scripts/bar.sh", "echo bar\n", "A: framework work")
    # A has an UNTRACKED file at the same path (collides with the incoming add)
    (a / "agents" / "bravo").mkdir(parents=True, exist_ok=True)
    (a / "agents/bravo/new.jsonl").write_text("untracked-local\n",
                                              encoding="utf-8", newline="\n")

    r = _run_push_env(a, "alpha", *_default_flags("--strict"))
    assert r.returncode == 0, f"stderr: {r.stderr}"
    assert "self-heal: clearing" in r.stderr
    assert "push OK" in r.stderr
    # untracked collision removed → merge brought origin's committed version
    assert (a / "agents/bravo/new.jsonl").read_text() == "from-b\n"
    assert _must(a, "status", "--porcelain") == ""


def test_selfheal_staged_crossagent_defers_guard741(tmp_path):
    """t3 (guard-741): a STAGED cross-agent change is a concurrent agent's
    in-flight work — NEVER discarded. The tree defers, staged work preserved."""
    origin, a, b = _clone_pair(tmp_path)
    _seed_and_sync(a, b, {"agents/bravo/state.jsonl": "v1\n"})
    _commit_file(b, "agents/bravo/state.jsonl", "v2-from-b\n", "B: bravo v2")
    _must(b, "push", "-q", "origin", "main")
    _commit_file(a, "core/scripts/baz.sh", "echo baz\n", "A: framework work")
    # A STAGES a change to the cross-agent file (concurrent-agent staged work)
    (a / "agents/bravo/state.jsonl").write_text("staged-by-partner\n",
                                                encoding="utf-8", newline="\n")
    _must(a, "add", "agents/bravo/state.jsonl")

    r = _run_push_env(a, "alpha", *_default_flags("--strict"))
    assert r.returncode == 1, f"should defer, not heal: {r.stderr}"
    assert "staged index entries present" in r.stderr
    assert "guard-741" in r.stderr
    assert "merge DEFERRED" in r.stderr
    # staged work NEVER discarded — content preserved AND still staged
    assert (a / "agents/bravo/state.jsonl").read_text() == "staged-by-partner\n"
    assert "agents/bravo/state.jsonl" in _must(a, "diff", "--cached", "--name-only")


def test_selfheal_self_dir_dirty_defers(tmp_path):
    """t4: dirty churn under the agent's OWN dir (agents/<self>/*) is never
    cleared — that would discard the agent's own uncommitted work. Defer."""
    origin, a, b = _clone_pair(tmp_path)
    _seed_and_sync(a, b, {"agents/alpha/state.jsonl": "v1\n"})
    # origin advances a file under alpha's own dir (hermetic trigger — the
    # commit author is irrelevant; the point is A has agents/<self>/* dirty)
    _commit_file(b, "agents/alpha/state.jsonl", "v2-from-b\n", "advance alpha state")
    _must(b, "push", "-q", "origin", "main")
    _commit_file(a, "core/scripts/qux.sh", "echo\n", "A: framework work")
    (a / "agents/alpha/state.jsonl").write_text("dirty-self\n",
                                                encoding="utf-8", newline="\n")

    r = _run_push_env(a, "alpha", *_default_flags("--strict"))
    assert r.returncode == 1, f"should defer, not clear own work: {r.stderr}"
    assert "under SELF agent dir" in r.stderr
    assert "merge DEFERRED" in r.stderr
    # own dirty work preserved (not cleared)
    assert (a / "agents/alpha/state.jsonl").read_text() == "dirty-self\n"


def test_selfheal_mixed_blocking_set_defers_all_or_nothing(tmp_path):
    """t5: a mixed blocking set (one clearable agents/<other>/* + one
    non-clearable core/) defers the WHOLE tree untouched — the scope guard
    returns BEFORE clearing anything, so neither file is cleared."""
    origin, a, b = _clone_pair(tmp_path)
    _seed_and_sync(a, b, {"agents/bravo/state.jsonl": "v1\n",
                          "core/scripts/shared.sh": "c1\n"})
    _commit_file(b, "agents/bravo/state.jsonl", "v2-from-b\n", "B: bravo v2")
    _commit_file(b, "core/scripts/shared.sh", "c2-from-b\n", "B: shared v2")
    _must(b, "push", "-q", "origin", "main")
    _commit_file(a, "agents/alpha/note.md", "note\n", "A: own work")
    # BOTH dirty: cross-agent (clearable) + core (never clearable)
    (a / "agents/bravo/state.jsonl").write_text("dirty-bravo\n",
                                                encoding="utf-8", newline="\n")
    (a / "core/scripts/shared.sh").write_text("dirty-core\n",
                                              encoding="utf-8", newline="\n")

    r = _run_push_env(a, "alpha", *_default_flags("--strict"))
    assert r.returncode == 1, f"mixed set must defer: {r.stderr}"
    assert "outside agents/<other>/*" in r.stderr
    assert "merge DEFERRED" in r.stderr
    # ALL-OR-NOTHING: neither file cleared (defer touches nothing)
    assert (a / "agents/bravo/state.jsonl").read_text() == "dirty-bravo\n"
    assert (a / "core/scripts/shared.sh").read_text() == "dirty-core\n"
