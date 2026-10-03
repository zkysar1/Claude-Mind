"""An append-only store's stub is never committed ().

A failed integrate can leave an append-only store's working copy as a
near-empty NEW file: one fresh row where HEAD holds the whole history.
Committing that file records the deletion. append-only-stub-guard.py runs
before both commit paths that stage such stores (iteration-commit.sh and
iteration-push.sh's churn self-heal). It rebuilds the working copy as HEAD's
rows plus its own new ones, and names any file it cannot repair so the caller
leaves it out.

Covered here: the guard's own decisions, then one end-to-end run through each
commit path with the shipped scripts.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
for _p in (str(CORE_SCRIPTS), str(SCRIPT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest  # noqa: E402

import coordination_merge as cm  # noqa: E402
from _bash_helpers import BASH  # noqa: E402
from test_iteration_commit_session_scope import (  # noqa: E402
    _hermetic_env, _shim_iteration_commit, _to_bash_path,
)
from test_iteration_push import (  # noqa: E402
    PUSH_SH, _clone_pair, _commit_file, _default_flags, _must,
)

GUARD = CORE_SCRIPTS / "append-only-stub-guard.py"
STORE = "agents/testagent/changelog.jsonl"


def _rows(n: int, start: int = 0, tag: str = "r") -> list:
    """Dated rows, serialized the way the store's writers append them."""
    base = datetime(2026, 9, 1)
    return [json.dumps({"timestamp": (base + timedelta(minutes=start + i)).strftime("%Y-%m-%dT%H:%M:%S"),
                        "agent": "testagent", "action": "edit", "file_path": f"{tag}{start + i}.md"})
            for i in range(n)]


def _write(repo: Path, rel: str, lines: list) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(ln + "\n" for ln in lines), encoding="utf-8", newline="\n")


def _rows_of(text: str) -> set:
    return {json.dumps(json.loads(ln), sort_keys=True) for ln in text.splitlines() if ln.strip()}


def _canon(lines: list) -> set:
    return _rows_of("\n".join(lines))


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, timeout=60).stdout


def _repo(tmp_path: Path, rel: str = STORE, head: list | None = None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@test.local")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    # The real repo ignores both of the guard's side files.
    (repo / ".gitignore").write_text("*.lock\n*.tmp\n__pycache__/\n", encoding="utf-8", newline="\n")
    _write(repo, rel, head if head is not None else _rows(5))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _guard(repo: Path, *paths: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("MIND_", "WORLD_", "META_", "STORAGE_"))}
    env["STORAGE_BACKEND"] = "local"
    # Pin the agent: with MIND_AGENT unset, _paths.py prints an "unset" WARN to stderr on any
    # box holding more than one agents/*/local-paths.conf, and tests assert an empty stderr.
    env["MIND_AGENT"] = "testagent"
    return subprocess.run([sys.executable, str(GUARD), "--repo", str(repo), *paths],
                          capture_output=True, text=True, timeout=60, env=env)


# --------------------------------------------------------------------------- #
# The guard's decisions
# --------------------------------------------------------------------------- #

# The stores  and its relay name: an agent changelog (whose merge rule
# deletes what one side removed), the world and meta changelogs, a board channel.
@pytest.mark.parametrize("store", [
    "agents/testagent/changelog.jsonl",
    ".mind-data/world/changelog.jsonl",
    ".mind-data/meta/changelog.jsonl",
    ".mind-data/world/board/coordination.jsonl",
])
def test_a_stub_is_rebuilt_from_head_and_keeps_its_own_new_row(tmp_path, store):
    assert cm.merge_handler_for(store).__name__ in (
        "merge_append_only_jsonl", "merge_rotated_board_jsonl"), store
    head = _rows(5)
    repo = _repo(tmp_path, rel=store, head=head)
    new = _rows(1, start=10_000, tag="new")
    _write(repo, store, new)  # the stub: one fresh row, no history

    r = _guard(repo, store)

    assert r.returncode == 0 and r.stdout == "", (r.stdout, r.stderr)
    assert f"REPAIRED {store}: the working copy lost 5 of 5 HEAD row(s)" in r.stderr, r.stderr
    assert "kept 1 new row(s)" in r.stderr, r.stderr
    assert _rows_of((repo / store).read_text(encoding="utf-8")) == _canon(head + new)


def test_a_front_slice_rotation_is_staged_as_is(tmp_path):
    """A hygiene rotation also shrinks the file. It drops a contiguous block
    of the OLDEST rows and keeps the rest, which is not a stub."""
    head = _rows(1100)
    repo = _repo(tmp_path, head=head)
    rotated = head[100:] + _rows(1, start=10_000, tag="new")
    _write(repo, STORE, rotated)
    before = (repo / STORE).read_bytes()

    r = _guard(repo, STORE)

    assert r.returncode == 0 and r.stdout == "" and "REPAIRED" not in r.stderr, r.stderr
    assert (repo / STORE).read_bytes() == before


def test_growth_and_unregistered_stores_are_left_alone(tmp_path):
    head = _rows(5)
    repo = _repo(tmp_path, head=head)
    _write(repo, STORE, head + _rows(2, start=10_000, tag="new"))
    other = "agents/testagent/experience.jsonl"  # no append-only merge handler
    assert cm.merge_handler_for(other) is None
    _write(repo, other, _rows(5))
    _git(repo, "add", other)
    _git(repo, "commit", "-q", "-m", "seed other")
    _write(repo, other, _rows(1, start=10_000, tag="new"))
    before = {p: (repo / p).read_bytes() for p in (STORE, other)}

    r = _guard(repo, STORE, other)

    assert r.returncode == 0 and r.stdout == "" and r.stderr == "", (r.stdout, r.stderr)
    assert {p: (repo / p).read_bytes() for p in (STORE, other)} == before


def test_a_stub_it_cannot_repair_is_named_for_the_caller_to_leave_out(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, STORE, _rows(1, start=10_000, tag="new"))
    before = (repo / STORE).read_bytes()
    (repo / (STORE + ".stub-guard.tmp")).mkdir()  # the repair's write target cannot be written

    r = _guard(repo, STORE)

    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == [STORE], (r.stdout, r.stderr)
    assert f"REFUSED {STORE}: lost 5 of 5 HEAD row(s), repair failed" in r.stderr, r.stderr
    assert (repo / STORE).read_bytes() == before


def test_a_held_lock_refuses_rather_than_writing_unlocked(tmp_path):
    spec = importlib.util.spec_from_file_location("append_only_stub_guard", GUARD)
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)

    class _Held:
        def acquire(self, path):
            raise TimeoutError("held by a writer")

        def release(self, path):
            raise AssertionError("released a lock it never took")

    repo = _repo(tmp_path)
    _write(repo, STORE, _rows(1, start=10_000, tag="new"))
    before = (repo / STORE).read_bytes()

    ok = guard.check_path(repo, STORE, cm.merge_handler_for, cm._front_evicted_lines,
                          cm.merge_append_only_jsonl, _Held())

    assert ok is False
    assert (repo / STORE).read_bytes() == before


def test_a_guard_without_its_merge_handlers_says_so_and_stages_as_before(tmp_path):
    """The guard tells an append-only store from any other file by the merge
    driver's own registry. Without it, it logs that and leaves nothing out: a
    broken registry must not stall every JSONL commit."""
    lone = tmp_path / "lone"
    lone.mkdir()
    (lone / GUARD.name).write_bytes(GUARD.read_bytes())  # no coordination_merge beside it
    repo = _repo(tmp_path)
    _write(repo, STORE, _rows(1, start=10_000, tag="new"))
    before = (repo / STORE).read_bytes()

    r = subprocess.run([sys.executable, "-I", str(lone / GUARD.name), "--repo", str(repo), STORE],
                       capture_output=True, text=True, timeout=60)

    assert r.returncode == 0 and r.stdout == "", (r.stdout, r.stderr)
    assert "merge handlers unavailable" in r.stderr and "guard skipped" in r.stderr, r.stderr
    assert (repo / STORE).read_bytes() == before


# --------------------------------------------------------------------------- #
# End to end: the goal commit
# --------------------------------------------------------------------------- #

GOAL_STORE = "agents/alpha/changelog.jsonl"
TOOL = "core/scripts/tool.py"
# The measured incident was the WORLD changelog under the tracked storage root;
# the agent changelog is the case zeta's relay added. Every end-to-end test below
# runs against both, because the self-heal reaches them through different arms
# (`.mind-data/*` versus the self namespace).
WORLD_STORE = ".mind-data/world/changelog.jsonl"
KINDS = pytest.mark.parametrize("kind", ["agent-changelog", "world-changelog"])


def _goal_store(kind: str) -> str:
    return GOAL_STORE if kind == "agent-changelog" else WORLD_STORE


def _heal_store(kind: str) -> str:
    return STORE if kind == "agent-changelog" else WORLD_STORE


def _goal_commit_repo(tmp_path: Path, head: list, store: str) -> tuple:
    """A repo whose `store` holds `head`, and the shimmed iteration-commit.sh
    (with the guard beside it) that will commit in it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@test.local")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "agents" / "alpha" / "session").mkdir(parents=True)
    (repo / "agents" / "alpha" / "self.md").write_text("# alpha\n", encoding="utf-8")
    (repo / "core" / "scripts").mkdir(parents=True)
    (repo / "core" / "scripts" / ".gitkeep").write_text("", encoding="utf-8")
    (repo / ".gitignore").write_text("**/session/\n*.lock\n*.tmp\n__pycache__/\n",
                                     encoding="utf-8", newline="\n")
    _write(repo, store, head)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    shim = _shim_iteration_commit(tmp_path)
    (shim.parent / GUARD.name).write_bytes(GUARD.read_bytes())
    (repo / TOOL).write_text("print('goal work')\n", encoding="utf-8")
    return repo, shim


def _run_goal_commit(repo: Path, shim: Path) -> subprocess.CompletedProcess:
    env = _hermetic_env(MIND_AGENT="alpha", PYTHONPATH=str(CORE_SCRIPTS))
    args = ["--goal-id", "g-test-sg-01", "--title", "Apply: stub guard", "--outcome", "deep",
            "--repo", str(repo)]
    return subprocess.run([BASH] + [_to_bash_path(a) for a in [shim, *args]],
                          env=env, capture_output=True, text=True, timeout=120)


@KINDS
def test_a_goal_commit_never_records_the_stub(tmp_path, kind):
    store = _goal_store(kind)
    head = _rows(5)
    repo, shim = _goal_commit_repo(tmp_path, head, store)
    new = _rows(1, start=10_000, tag="new")
    _write(repo, store, new)  # the stub

    r = _run_goal_commit(repo, shim)

    assert r.returncode == 0, r.stdout + r.stderr
    committed = set(_git(repo, "show", "--name-only", "--format=", "HEAD").split())
    assert {store, TOOL} <= committed, committed
    assert f"REPAIRED {store}" in r.stderr, r.stderr
    assert _rows_of(_git(repo, "show", f"HEAD:{store}")) == _canon(head + new)


@KINDS
def test_a_stub_the_guard_cannot_repair_stays_out_of_the_goal_commit(tmp_path, kind):
    store = _goal_store(kind)
    head = _rows(5)
    repo, shim = _goal_commit_repo(tmp_path, head, store)
    _write(repo, store, _rows(1, start=10_000, tag="new"))  # the stub
    (repo / (store + ".stub-guard.tmp")).mkdir()  # the repair's write target cannot be written
    before = (repo / store).read_bytes()

    r = _run_goal_commit(repo, shim)

    assert r.returncode == 0, r.stdout + r.stderr
    committed = set(_git(repo, "show", "--name-only", "--format=", "HEAD").split())
    assert TOOL in committed and store not in committed, committed
    assert f"left out of this commit by the append-only stub guard: {store}" in r.stderr, r.stderr
    assert (repo / store).read_bytes() == before  # still the stub, uncommitted, untouched
    assert _rows_of(_git(repo, "show", f"HEAD:{store}")) == _canon(head)


# --------------------------------------------------------------------------- #
# End to end: the churn self-heal in iteration-push
# --------------------------------------------------------------------------- #

NOTES = "agents/testagent/notes.md"


def _heal_setup(tmp_path: Path, store: str) -> tuple:
    """Clone A holds the seeded ledger `store` and notes; origin then gains B's
    append to the ledger, so A is behind and any dirty ledger on A blocks the
    integrate."""
    origin, a, b = _clone_pair(tmp_path)
    # Union on the ledgers so the merge after the heal lands without a driver.
    _commit_file(a, ".gitattributes",
                 f"agents/*/changelog.jsonl merge=union\n{WORLD_STORE} merge=union\n", "attrs")
    head = _rows(5)
    _commit_file(a, store, "".join(ln + "\n" for ln in head), "seed ledger")
    _commit_file(a, NOTES, "n1\n", "seed notes")
    _must(a, "push", "-q", "origin", "main")
    _must(b, "pull", "-q", "origin", "main")
    from_b = _rows(1, start=20_000, tag="from-b")
    _commit_file(b, store, "".join(ln + "\n" for ln in head + from_b), "B: append")
    _must(b, "push", "-q", "origin", "main")
    return a, head, from_b


def _run_heal(tmp_path: Path, a: Path, *flags: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MIND_", "BODY_", "STORAGE_"))}
    env.update(MIND_AGENT="testagent", STORAGE_BACKEND="local",
               ITERATION_PUSH_LOG_FILE=str(tmp_path / "push.log"))
    return subprocess.run([BASH, str(PUSH_SH), "--repo", str(a), *_default_flags(*flags)],
                          capture_output=True, text=True, timeout=180, env=env)


def _heal_commits(a: Path) -> list:
    return [ln.split("\t", 1)[0] for ln in _must(a, "log", "--format=%H\t%s", "-8").splitlines()
            if "chore(testagent): pre-merge" in ln]


@KINDS
def test_the_churn_self_heal_never_records_the_stub(tmp_path, kind):
    store = _heal_store(kind)
    a, head, from_b = _heal_setup(tmp_path, store)
    new = _rows(1, start=10_000, tag="new")
    _write(a, store, new)  # this box's stub: blocks the merge, so the self-heal commits it

    r = _run_heal(tmp_path, a, "--strict")

    assert r.returncode == 0, r.stderr
    assert f"REPAIRED {store}" in r.stderr, r.stderr
    heal = _heal_commits(a)
    assert len(heal) == 1, _must(a, "log", "--format=%s", "-8")
    assert _rows_of(_must(a, "show", f"{heal[0]}:{store}")) == _canon(head + new)
    assert _rows_of(_must(a, "show", f"HEAD:{store}")) == _canon(head + new + from_b)
    _must(a, "fetch", "-q", "origin", "main")
    assert _must(a, "rev-list", "--left-right", "--count", "origin/main...main").split() == ["0", "0"]


@KINDS
def test_a_stub_the_guard_cannot_repair_stays_out_of_the_self_heal_commit(tmp_path, kind):
    store = _heal_store(kind)
    a, head, from_b = _heal_setup(tmp_path, store)
    new = _rows(1, start=10_000, tag="new")
    _write(a, store, new)  # the stub
    (a / NOTES).write_text("n2\n", encoding="utf-8", newline="\n")  # other self churn, committable
    (a / (store + ".stub-guard.tmp")).mkdir()  # the repair's write target cannot be written
    before = (a / store).read_bytes()

    r = _run_heal(tmp_path, a)  # not --strict: the integrate may defer on the dirty stub

    assert f"REFUSED {store}" in r.stderr, r.stderr
    heal = _heal_commits(a)
    assert len(heal) == 1, _must(a, "log", "--format=%s", "-8")
    assert set(_must(a, "show", "--name-only", "--format=", heal[0]).split()) == {NOTES}
    assert (a / store).read_bytes() == before  # still the stub, uncommitted, untouched
    assert new[0] not in _must(a, "show", f"HEAD:{store}")
