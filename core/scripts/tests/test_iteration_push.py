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

import json
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


def test_forced_zero_thresholds_push_what_throttling_would_defer(tmp_path):
    """Graceful-stop D6.65 flush: forced-zero thresholds must push a batch the
    defaults decline.

    Deliberately the SAME scenario as test_throttled_below_thresholds directly
    above — one fresh commit, 1 ahead — asserting the OPPOSITE outcome. That
    pair is the contract aspirations-graceful-stop SKILL.md D6.65 rests on:
    mid-loop batching is correct because another iteration will always follow,
    but at shutdown there is no next iteration, so a deferred batch is stranded
    (observed: 7 commits unpushed ~12h after a stop). g-115-4134.
    """
    origin, a, b = _clone_pair(tmp_path)
    _commit_file(a, "f1.txt", "one\n", "A: f1")
    r = _run_push(a, "--min-commits", "0", "--max-age-min", "0",
                  "--fetch-interval-min", "0", "--strict")
    assert r.returncode == 0, r.stderr
    assert "throttled" not in r.stderr, \
        "forced-zero thresholds must not defer — the stop path has no next iteration"
    assert "push OK" in r.stderr
    assert _must(a, "rev-parse", "origin/main") == _tip(a)


def test_graceful_stop_flush_precedes_the_s3_flush():
    """The D6.65 git flush must run BEFORE the D6.7 S3 flush, and stay fail-soft.

    Ordering is load-bearing, not cosmetic: iteration-push does fetch+MERGE,
    which mutates git-tracked files under agents/<agent>/ (journal.jsonl,
    experience.jsonl and changelog.jsonl are all tracked — only session/ and
    sessions/ are gitignored). Those paths sit inside the owned-set D6.7 pushes
    to S3, so a merge landing AFTER that flush leaves local newer than S3 —
    exactly the machine-move stranding D6.7 exists to prevent.

    Paired with the behavioral test above per guard-1451 (a structural test is
    never sufficient alone): that one proves the flush flushes, this one proves
    it is wired in at a position where the flush is safe. g-115-4134.
    """
    skill = (PROJECT_ROOT / ".claude" / "skills"
             / "aspirations-graceful-stop" / "SKILL.md")
    src = skill.read_text(encoding="utf-8")

    push_idx = src.find("iteration-push.sh --min-commits 0")
    assert push_idx != -1, "D6.65 forced-zero git flush invocation is missing"
    s3_idx = src.find("owncloud-flush.sh")
    assert s3_idx != -1, "D6.7 S3 flush invocation is missing"
    assert push_idx < s3_idx, (
        "D6.65 git flush must precede the D6.7 S3 flush — a merge after the "
        "flush strands local-newer-than-S3 state on a machine move"
    )

    invocation = src[push_idx:src.find("\n", push_idx)]
    # guard-775: without --strict, iteration-push's soft_exit() returns 0 on
    # every path, so the stop can never be aborted by a push failure. Adding
    # --strict here would let a transient network error break the stop sequence.
    assert "--strict" not in invocation, \
        "stop-path flush must stay fail-soft — --strict would let a push failure abort the stop"
    assert "--fetch-interval-min 0" in invocation, \
        "stop-path flush must defeat the fetch throttle — no next iteration will correct a stale origin ref"


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


def test_conflict_names_the_paths_and_their_merge_drivers(tmp_path):
    """A conflict abort must ATTRIBUTE itself: name each unmerged path and the
    merge driver it resolved to (g-115-6593).

    Before this, every abort site logged "investigate which store conflicted"
    and then ran `git merge --abort` on the next line, destroying the only
    state that could answer it -- 388 integrates / 6 conflict events / 17d on
    cc-02 produced ZERO attributable conflicts, so any gate phrased as
    "agent-ledger conflict count over the window" was unsatisfiable by
    construction.

    THE ORDERING IS THE INVARIANT, and the last assertion is what pins it:
    `git diff --diff-filter=U` returns EMPTY once the merge is aborted, so
    moving the capture below the abort still logs a line -- the NONE REPORTED
    one -- and every other assertion here would still pass. That branch
    existing is what makes this test discriminating rather than decorative.
    """
    origin, a, b = _clone_pair(tmp_path)
    _commit_file(b, "base.txt", "B version\n", "B: rewrite base")
    _must(b, "push", "-q", "origin", "main")
    _commit_file(a, "base.txt", "A version\n", "A: rewrite base")

    r = _run_push(a, *_default_flags("--strict"))
    assert r.returncode == 1
    assert "MERGE CONFLICT" in r.stderr

    line = [ln for ln in r.stderr.splitlines() if "conflicted paths" in ln]
    assert line, f"no conflict-attribution line in stderr:\n{r.stderr}"
    attribution = line[0]
    assert "base.txt" in attribution, attribution
    assert "merge=" in attribution, attribution
    # Capture must precede the abort -- see the docstring.
    assert "NONE REPORTED" not in attribution, attribution


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
# push-race recovery ( — the rb-3970 phantom-window shape)
# --------------------------------------------------------------------------- #
def test_pushrace_recovery_lands_same_invocation(tmp_path):
    """THE 2026-07-18 phantom shape: A's pre-push fetch is THROTTLED, so the
    integrate step compares against a stale tracking ref (BEHIND=0, merge
    skipped) while B has already pushed — A's push rejects non-fast-forward.
    Pre-fix the script deferred and the next iteration re-failed identically
    under the same throttle. Post-fix the in-invocation recovery (unthrottled
    fetch + merge + one retry push) lands the commit in the SAME run."""
    origin, a, b = _clone_pair(tmp_path)
    # Prime A's FETCH_HEAD so a 60-min throttle suppresses the pre-push fetch.
    _must(a, "fetch", "origin", "main")
    # B pushes AFTER A's fetch — A's tracking ref is now stale.
    _commit_file(b, "from_b.txt", "b\n", "B: change")
    _must(b, "push", "-q", "origin", "main")
    _commit_file(a, "from_a.txt", "a\n", "A: change")

    r = _run_push(a, "--min-commits", "1", "--fetch-interval-min", "60",
                  "--strict")
    assert r.returncode == 0, f"recovery should land the push: {r.stderr}"
    assert "fetch throttled" in r.stderr          # the stale-ref precondition
    assert "push rejected (race shape)" in r.stderr
    assert "push-race recovery OK" in r.stderr
    # origin holds BOTH machines' commits; A fully converged
    _must(a, "fetch", "-q", "origin", "main")
    counts = _must(a, "rev-list", "--left-right", "--count",
                   "origin/main...main")
    assert counts.split() == ["0", "0"], f"not converged: {counts}"
    files = _must(a, "ls-tree", "-r", "--name-only", "origin/main")
    assert "from_a.txt" in files and "from_b.txt" in files


def test_pushrace_recovery_conflict_defers_cleanly(tmp_path):
    """Race recovery hits a TRUE content conflict on the recovery merge: it
    aborts cleanly (no MERGE_HEAD debris), defers fail-soft, and never
    forces — same safety contract as the primary integrate step."""
    origin, a, b = _clone_pair(tmp_path)
    _must(a, "fetch", "origin", "main")           # throttle precondition
    _commit_file(b, "base.txt", "B version\n", "B: rewrite base")
    _must(b, "push", "-q", "origin", "main")
    _commit_file(a, "base.txt", "A version\n", "A: rewrite base")
    a_tip_before = _tip(a)

    r = _run_push(a, "--min-commits", "1", "--fetch-interval-min", "60",
                  "--strict")
    assert r.returncode == 1, f"true conflict must defer: {r.stderr}"
    assert "push rejected (race shape)" in r.stderr
    assert "recovery merge CONFLICT" in r.stderr
    # clean abort: no mid-merge state, local commit intact, tree pristine
    assert not (a / ".git" / "MERGE_HEAD").exists()
    assert _tip(a) == a_tip_before
    assert _must(a, "status", "--porcelain") == ""
    # origin untouched (B's tip) — never forced
    _must(a, "fetch", "-q", "origin", "main")
    assert _must(a, "rev-parse", "origin/main") == _tip(b)


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


def _union_attrs_pre_merge() -> bool:
    """True when origin/main's .gitattributes carries the ledger-merge entries
    but the local checkout does not yet (behind box, merge deferred) — check-attr
    then reports 'unspecified' for box-state reasons, not regression (g-115-1940;
    extended for the g-115-2767 ayoai-ledger migration). Checks BOTH the union
    needle (line-append-safe ledgers) and the ayoai-ledger needle (RMW ledgers)
    so a box behind on EITHER migration skips rather than fails. A needle absent
    locally but present in origin = behind box; absent everywhere returns False
    so a REAL regression (entry removed everywhere) still fails the test."""
    needles = ("skill-invocations.jsonl merge=union",
               "journal.jsonl merge=ayoai-ledger")
    try:
        ga = PROJECT_ROOT / ".gitattributes"
        local = ga.read_text(encoding="utf-8") if ga.is_file() else ""
        missing = [n for n in needles if n not in local]
        if not missing:
            return False  # local carries both migrations — run the test
        r = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "show", "origin/main:.gitattributes"],
            capture_output=True, text=True, timeout=10,
        )
        origin = r.stdout if r.returncode == 0 else ""
        # Behind box: a needle absent locally but present in origin.
        return any(n in origin for n in missing)
    except Exception:
        return False


@pytest.mark.skipif(
    _union_attrs_pre_merge(),
    reason="origin/main has the union .gitattributes entries but this checkout "
           "pre-dates the merge — heals when iteration-push integrates origin "
           "(g-115-1940)",
)
def test_real_repo_union_scope_is_evidence_gated():
    """Read-only probe of the REAL .gitattributes: line-append-safe ledgers get
    merge=union; RMW ledgers (rewritten/pruned/archived) route to the record-aware
    merge=ayoai-ledger driver (g-115-2767), NOT unspecified — leaving them
    unspecified stranded cross-box MIND commits, and union would resurrect
    pruned/edited lines."""
    paths = {
        "agents/alpha/skill-invocations.jsonl": "union",
        "agents/alpha/health/2026-01-01.jsonl": "union",
        # RMW ledgers → record-aware ayoai-ledger driver (), NOT
        # unspecified: union resurrects pruned/edited lines, and unspecified
        # stranded cross-box MIND commits (the exact reason  added it).
        "agents/alpha/journal.jsonl": "ayoai-ledger",       # index rewrites
        "agents/alpha/changelog.jsonl": "ayoai-ledger",     # pruning
        "agents/alpha/experience.jsonl": "ayoai-ledger",    # archival
        "agents/alpha/aspirations.jsonl": "ayoai-ledger",   # RMW status updates
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
# churn self-heal for the dirty-tree merge deadlock ( + )
#
# The deadlock (per ): origin advances a file that THIS machine has
# UNSTAGED churn on. `git merge` refuses BEFORE starting (no MERGE_HEAD), the
# tree is deferred, and the churn re-creates every cycle so it never
# self-heals. Two healable namespaces (all-or-nothing scan first):
#   - agents/<other>/* (owncloud re-materialised sibling state): CLEARED —
#     origin is authoritative, owncloud re-syncs next cycle ().
#   - agents/<self>/* (own ledgers; changelog re-appends on EVERY write, so
#     pre-2249 the defer wedged a behind box forever — cc-05
#     15-ahead/53-behind): COMMITTED pathspec-limited, then merged + pushed
#     ().
# NEVER touches staged entries (guard-741: a concurrent agent's in-flight
# staged work) nor any file outside agents/* (core/world). Any such file in
# the blocking set defers the WHOLE tree untouched.
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
    assert "after churn self-heal" in r.stderr
    assert "push OK" in r.stderr
    #  shipped these DISCARDING lines with no test;  folds
    # the assertion in here rather than filing a third goal. One line PER cleared
    # tracked path. The count line above stays byte-identical (guard-695).
    assert ("self-heal: DISCARDING uncommitted tracked cross-agent work: "
            "agents/bravo/state.jsonl") in r.stderr, r.stderr
    assert "clearing 1 tracked + 0 untracked cross-agent file(s)" in r.stderr

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
    #  (sq-018/sq-019 addendum): one line PER cleared untracked path.
    assert ("self-heal: DISCARDING untracked cross-agent file: "
            "agents/bravo/new.jsonl") in r.stderr, r.stderr
    assert "clearing 0 tracked + 1 untracked cross-agent file(s)" in r.stderr
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


def test_selfheal_self_dir_dirty_commits_and_merges(tmp_path):
    """t4 (): the TRUE cc-05 wedge shape — a union-attributed self
    ledger (health/*.jsonl) advanced at origin while THIS box holds an
    UNCOMMITTED append to the same ledger. git refuses the merge
    (checkout-over-dirty) even though content-level merge is clean
    (merge=union). The self churn is COMMITTED (pathspec-limited), the merge
    retries, union keeps BOTH appends, and the push converges. Pre-2249 this
    shape DEFERRED forever (own ledgers re-dirty every write — the cc-05
    15-ahead/53-behind wedge)."""
    origin, a, b = _clone_pair(tmp_path)
    # hermetic copy of the real repo's union rules (same as the union test)
    _commit_file(a, ".gitattributes",
                 "agents/*/skill-invocations.jsonl merge=union\n"
                 "agents/*/health/*.jsonl merge=union\n",
                 "attrs")
    _must(a, "push", "-q", "origin", "main")
    _must(b, "pull", "-q", "origin", "main")
    _seed_and_sync(a, b, {"agents/alpha/health/day.jsonl": "base\n"})
    # origin appends to alpha's ledger (another box merged alpha's history)
    _commit_file(b, "agents/alpha/health/day.jsonl", "base\nfrom-b\n",
                 "advance alpha ledger")
    _must(b, "push", "-q", "origin", "main")
    _commit_file(a, "core/scripts/qux.sh", "echo\n", "A: framework work")
    # THIS box's uncommitted append to its own ledger (re-appears every write)
    (a / "agents/alpha/health/day.jsonl").write_text("base\nlocal-append\n",
                                                     encoding="utf-8", newline="\n")

    r = _run_push_env(a, "alpha", *_default_flags("--strict"))
    assert r.returncode == 0, f"should commit self churn and heal: {r.stderr}"
    assert "committing 1 SELF-namespace file(s) pre-merge" in r.stderr
    assert "push OK" in r.stderr
    # union kept BOTH sides' appends; tree fully converged and clean
    merged = (a / "agents/alpha/health/day.jsonl").read_text()
    assert "local-append" in merged and "from-b" in merged
    _must(a, "fetch", "-q", "origin", "main")
    counts = _must(a, "rev-list", "--left-right", "--count", "origin/main...main")
    assert counts.split() == ["0", "0"], f"not converged: {counts}"
    assert _must(a, "status", "--porcelain") == ""
    # the self-churn commit is attributed + traceable
    subjects = _must(a, "log", "--format=%s", "-8")
    assert "chore(alpha): pre-merge self-namespace churn" in subjects


def test_selfheal_self_dir_both_diverged_surfaces_conflict(tmp_path):
    """t4b ( rare shape): self file diverged on BOTH sides (origin
    advanced it AND local dirty). The self churn is committed (preserved in
    history), the merge then hits a TRUE content conflict which is aborted
    cleanly and surfaced LOUDLY — strictly better than the pre-2249 silent
    defer-forever: the local bytes are recoverable from the commit."""
    origin, a, b = _clone_pair(tmp_path)
    _seed_and_sync(a, b, {"agents/alpha/state.jsonl": "v1\n"})
    _commit_file(b, "agents/alpha/state.jsonl", "v2-from-b\n", "advance alpha state")
    _must(b, "push", "-q", "origin", "main")
    _commit_file(a, "core/scripts/qux.sh", "echo\n", "A: framework work")
    (a / "agents/alpha/state.jsonl").write_text("dirty-self\n",
                                                encoding="utf-8", newline="\n")

    r = _run_push_env(a, "alpha", *_default_flags("--strict"))
    assert r.returncode == 1, f"true divergence must surface, not push: {r.stderr}"
    assert "committing 1 SELF-namespace file(s) pre-merge" in r.stderr
    # the retry hit a real conflict; no mid-merge state left behind
    assert not (a / ".git" / "MERGE_HEAD").exists()
    # own bytes preserved in the pre-merge commit (recoverable)
    assert _must(a, "show", "HEAD:agents/alpha/state.jsonl") == "dirty-self"


def test_selfheal_mixed_self_and_crossagent_heals_both(tmp_path):
    """t6 (): blocking set spans BOTH namespaces — self churn is
    committed, cross-agent churn is cleared, merge retries and the push
    converges."""
    origin, a, b = _clone_pair(tmp_path)
    _seed_and_sync(a, b, {"agents/alpha/state.jsonl": "v1\n",
                          "agents/bravo/state.jsonl": "v1\n"})
    _commit_file(b, "agents/bravo/state.jsonl", "v2-from-b\n", "B: bravo v2")
    _must(b, "push", "-q", "origin", "main")
    _commit_file(a, "core/scripts/quux.sh", "echo\n", "A: framework work")
    (a / "agents/alpha/state.jsonl").write_text("dirty-self\n",
                                                encoding="utf-8", newline="\n")
    (a / "agents/bravo/state.jsonl").write_text("dirty-bravo\n",
                                                encoding="utf-8", newline="\n")

    r = _run_push_env(a, "alpha", *_default_flags("--strict"))
    assert r.returncode == 0, f"mixed self+cross should heal both: {r.stderr}"
    assert "committing 1 SELF-namespace file(s) pre-merge" in r.stderr
    assert "clearing 1 tracked + 0 untracked cross-agent file(s)" in r.stderr
    assert "push OK" in r.stderr
    # self churn committed; cross churn converged to origin's version
    assert _must(a, "show", "HEAD:agents/alpha/state.jsonl") == "dirty-self"
    assert (a / "agents/bravo/state.jsonl").read_text() == "v2-from-b\n"
    assert _must(a, "status", "--porcelain") == ""


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
    assert "outside agents/*" in r.stderr
    assert "merge DEFERRED" in r.stderr
    # ALL-OR-NOTHING: neither file cleared (defer touches nothing)
    assert (a / "agents/bravo/state.jsonl").read_text() == "dirty-bravo\n"
    assert (a / "core/scripts/shared.sh").read_text() == "dirty-core\n"


# --------------------------------------------------------------------------- #
# --no-push: the session-start continuity pull ()
# --------------------------------------------------------------------------- #
# owncloud-pull.sh no-ops on local backend by design, so on a local-backend
# deployment NOTHING fetched outside the autonomous loop and every assistant /
# reader session started on whatever the checkout was when the loop last ran
# (observed: 47 commits behind while a session actively read and wrote those
# files). The fix routes owncloud-pull.sh's local-backend branch here rather
# than re-deriving the hardened fetch+integrate. --no-push is what makes that
# safe: becoming current must not publish local state as a side effect of
# STARTING a session.
#
# Each test below is paired with a control that differs ONLY by the flag. A
# "did not push" assertion is otherwise satisfied by any scenario where nothing
# WOULD have pushed anyway, which is a test that passes forever while guarding
# nothing (guard-1832).

def test_no_push_integrates_origin_commits(tmp_path):
    """The POINT of the flag: a behind session becomes current."""
    origin, a, b = _clone_pair(tmp_path)
    _commit_file(b, "from_b.txt", "b\n", "B: change")
    _must(b, "push", "-q", "origin", "main")
    before = _tip(a)

    r = _run_push(a, *_default_flags("--no-push", "--strict"))
    assert r.returncode == 0, r.stderr
    assert "integrated 1 origin commit" in r.stderr, r.stderr
    assert _tip(a) != before, "local HEAD did not advance — no integrate happened"
    assert (a / "from_b.txt").exists(), "origin's file is not in the working tree"


def test_no_push_does_not_push_when_ahead(tmp_path):
    """THE REGRESSION TEST. Local is ahead, so the unflagged run pushes; the
    flagged run must integrate and stop. Asserts against origin's real ref, not
    just log text."""
    origin, a, b = _clone_pair(tmp_path)
    _commit_file(a, "from_a.txt", "a\n", "A: local work")
    origin_before = _must(a, "rev-parse", "origin/main")

    r = _run_push(a, *_default_flags("--no-push", "--strict"))
    assert r.returncode == 0, r.stderr
    assert "skipping push decision" in r.stderr, r.stderr
    _must(a, "fetch", "-q", "origin")
    assert _must(a, "rev-parse", "origin/main") == origin_before, \
        "--no-push PUBLISHED local commits — a session must not push by starting"


def test_control_same_scenario_does_push_without_the_flag(tmp_path):
    """Positive control for the test above. Identical setup minus --no-push:
    it MUST push. Without this, the 'did not push' assertion could be passing
    because the scenario never pushes at all."""
    origin, a, b = _clone_pair(tmp_path)
    _commit_file(a, "from_a.txt", "a\n", "A: local work")
    origin_before = _must(a, "rev-parse", "origin/main")

    r = _run_push(a, *_default_flags("--strict"))
    assert r.returncode == 0, r.stderr
    assert "push OK" in r.stderr, r.stderr
    _must(a, "fetch", "-q", "origin")
    assert _must(a, "rev-parse", "origin/main") != origin_before, \
        "control did not push — the paired no-push assertion proves nothing"


def test_no_push_stops_before_push_even_when_integrate_is_a_noop(tmp_path):
    """The seam is placed after integrate, so the flag must still hold when
    there is nothing to integrate — i.e. it is not accidentally coupled to the
    merge path having run."""
    origin, a, b = _clone_pair(tmp_path)
    _commit_file(a, "from_a.txt", "a\n", "A: local work")

    r = _run_push(a, *_default_flags("--no-push", "--strict"))
    assert "skipping push decision" in r.stderr, r.stderr
    # never reached the push-decision logging below the seam
    assert "pushing " not in r.stderr, r.stderr
    assert "nothing to push" not in r.stderr, r.stderr


# --------------------------------------------------------------------------- #
# blocking-set narrowing ()
# --------------------------------------------------------------------------- #
# The self-heal classifier used to scan the ENTIRE dirty tree, and one path
# outside agents/* vetoed the whole heal. Because nothing clears that path, the
# veto repeated every iteration: measured on ZDS cc-06 as ~2.5h / 20+ cycles of
# merge refusal with 14 dirty files, only some of which the merge touched.
#
# The narrowing consults the set git NAMES in its refusal message, and ONLY at
# the veto and clear sites — never to shrink what gets committed (t8 is the
# regression pin for that, and t_mixed above is the shape that caught it during
# development). Each test below is paired against the property it would silently
# lose, not merely against a log line.
#
# The ESCALATION half of  was implemented concurrently by two agents;
# the surviving mechanism is _ip_defer_streak_tick, covered by
# test_defer_streak_escalates_then_resets / _survives_dry_run below. The
# duplicate _ip_refusal_bump and its two tests were retired with it.


def test_selfheal_nonblocking_outofscope_file_does_not_veto(tmp_path):
    """t6 (, the fix): a dirty core/ file that the incoming merge does
    NOT touch must not veto a heal that would otherwise succeed — and must not
    be touched either. Pre-fix this deferred forever."""
    origin, a, b = _clone_pair(tmp_path)
    _seed_and_sync(a, b, {"agents/bravo/state.jsonl": "v1\n",
                          "core/scripts/shared.sh": "c1\n"})
    # origin advances ONLY the cross-agent file -> only IT blocks the merge
    _commit_file(b, "agents/bravo/state.jsonl", "v2-from-b\n", "B: bravo v2")
    _must(b, "push", "-q", "origin", "main")
    _commit_file(a, "agents/alpha/note.md", "note\n", "A: own work")
    (a / "agents/bravo/state.jsonl").write_text("dirty-bravo\n",
                                                encoding="utf-8", newline="\n")
    # ...and an UNRELATED dirty core/ file the merge never touches
    (a / "core/scripts/shared.sh").write_text("dirty-core\n",
                                              encoding="utf-8", newline="\n")

    r = _run_push_env(a, "alpha", *_default_flags("--strict"))
    assert r.returncode == 0, f"non-blocking core/ churn must not veto: {r.stderr}"
    assert "as blocking this merge" in r.stderr, r.stderr
    assert "after churn self-heal" in r.stderr, r.stderr
    # the blocking cross-agent file converged to origin...
    assert (a / "agents/bravo/state.jsonl").read_text() == "v2-from-b\n"
    # ...and the unrelated file was left EXACTLY as found (not cleared, not
    # committed). Narrowing must remove the veto without widening the blast
    # radius of the heal.
    assert (a / "core/scripts/shared.sh").read_text() == "dirty-core\n"


def test_selfheal_nonblocking_crossagent_churn_is_not_cleared(tmp_path):
    """t7: clearing discards a sibling's local churn, so it is now restricted to
    what git actually named. A cross-agent file the merge does not touch must
    survive the heal untouched."""
    origin, a, b = _clone_pair(tmp_path)
    _seed_and_sync(a, b, {"agents/bravo/blocking.jsonl": "v1\n",
                          "agents/bravo/idle.jsonl": "v1\n"})
    _commit_file(b, "agents/bravo/blocking.jsonl", "v2-from-b\n", "B: blocking v2")
    _must(b, "push", "-q", "origin", "main")
    _commit_file(a, "agents/alpha/note.md", "note\n", "A: own work")
    (a / "agents/bravo/blocking.jsonl").write_text("dirty-blocking\n",
                                                   encoding="utf-8", newline="\n")
    (a / "agents/bravo/idle.jsonl").write_text("dirty-idle\n",
                                               encoding="utf-8", newline="\n")

    r = _run_push_env(a, "alpha", *_default_flags("--strict"))
    assert r.returncode == 0, f"should heal: {r.stderr}"
    assert "self-heal: clearing 1 tracked" in r.stderr, r.stderr
    assert (a / "agents/bravo/blocking.jsonl").read_text() == "v2-from-b\n"
    # NOT cleared — git never named it, so the heal has no business touching it
    assert (a / "agents/bravo/idle.jsonl").read_text() == "dirty-idle\n"


def test_selfheal_commits_self_churn_git_did_not_name(tmp_path):
    """t8: the narrowing must NOT reach the commit sites. /
    exist because own ledger churn re-dirties every tick and must be PRESERVED,
    not merely unblocked — so self-namespace churn is committed whether or not
    git named it as blocking. (This exact shape caught a wrong first cut of the
    g-115-4484 fix, which narrowed the classified set wholesale.)"""
    origin, a, b = _clone_pair(tmp_path)
    _seed_and_sync(a, b, {"agents/alpha/state.jsonl": "v1\n",
                          "agents/bravo/state.jsonl": "v1\n"})
    # origin advances ONLY bravo's file -> git names ONLY that as blocking
    _commit_file(b, "agents/bravo/state.jsonl", "v2-from-b\n", "B: bravo v2")
    _must(b, "push", "-q", "origin", "main")
    _commit_file(a, "core/scripts/quux.sh", "echo\n", "A: framework work")
    (a / "agents/alpha/state.jsonl").write_text("dirty-self\n",
                                                encoding="utf-8", newline="\n")
    (a / "agents/bravo/state.jsonl").write_text("dirty-bravo\n",
                                                encoding="utf-8", newline="\n")

    r = _run_push_env(a, "alpha", *_default_flags("--strict"))
    assert r.returncode == 0, f"should heal both halves: {r.stderr}"
    assert "committing 1 SELF-namespace file(s) pre-merge" in r.stderr, r.stderr
    # the self churn is IN HISTORY, not merely un-blocking
    log = _must(a, "log", "--format=%s")
    assert "pre-merge self-namespace churn" in log, log
    assert (a / "agents/alpha/state.jsonl").read_text() == "dirty-self\n"


# --------------------------------------------------------------------------- #
# integrate-defer streak escalation + persisted log ()
# --------------------------------------------------------------------------- #
def test_defer_streak_escalates_then_resets(tmp_path):
    """Three consecutive dirty-defer integrates escalate LOUDLY (banner +
    health JSONL); every defer line is persisted to .git/iteration-push.log;
    a successful integrate clears the streak state. State lives inside .git/
    so it can never appear in porcelain (an untracked non-agents/* file would
    itself trigger the self-heal defer — the bug this feature observes)."""
    origin, a, b = _clone_pair(tmp_path)
    # B pushes a rewrite of a file A ALSO holds dirty (uncommitted): A's merge
    # is refused before starting (would overwrite), and the blocker is outside
    # agents/* and .mind-data/*, so the self-heal defers the whole tree.
    _commit_file(b, "base.txt", "B v2\n", "B: rewrite base")
    _must(b, "push", "-q", "origin", "main")
    (a / "base.txt").write_text("A dirty uncommitted\n", encoding="utf-8")

    streak = a / ".git" / "iteration-push-defer-streak"
    iplog = a / ".git" / "iteration-push.log"
    env = {**os.environ, "MIND_AGENT": "testagent"}
    (a / "agents" / "testagent").mkdir(parents=True)

    outs = []
    for _ in range(3):
        r = subprocess.run(
            [BASH, str(PUSH_SH), "--repo", str(a), *_default_flags()],
            capture_output=True, text=True, timeout=120, env=env,
        )
        assert r.returncode == 0, r.stderr          # fail-soft, never blocks
        outs.append(r.stderr)
    assert "merge DEFERRED" in outs[0], outs[0]
    assert "INTEGRATE-DEFER STREAK" not in outs[0]
    assert "INTEGRATE-DEFER STREAK" not in outs[1]
    assert "INTEGRATE-DEFER STREAK" in outs[2], outs[2]
    assert streak.is_file() and streak.read_text().split()[0] == "3"
    # escalation record landed in the resolved agent's health JSONL
    health = list((a / "agents" / "testagent" / "health").glob("*.jsonl"))
    assert health, "no health JSONL written at escalation"
    assert "integrate_defer_streak" in health[0].read_text(encoding="utf-8")
    # persisted log carries the defer lines (log() tee half of )
    assert iplog.is_file() and "merge DEFERRED" in iplog.read_text(encoding="utf-8")
    # streak state is invisible to porcelain (the .git/ placement invariant)
    assert "iteration-push-defer-streak" not in _must(a, "status", "--porcelain")

    # clear the dirt -> integrate succeeds -> streak state is gone
    _must(a, "checkout", "--", "base.txt")
    r = subprocess.run(
        [BASH, str(PUSH_SH), "--repo", str(a), *_default_flags()],
        capture_output=True, text=True, timeout=120, env=env,
    )
    assert r.returncode == 0, r.stderr
    assert "integrated" in r.stderr, r.stderr
    assert not streak.exists(), "streak file must reset on successful integrate"


def test_selfheal_retry_conflict_reports_conflict_shape_not_dirty_defer(tmp_path):
    """When the churn self-heal SUCCEEDS and the retry then hits a true content
    conflict, the streak shape must be conflict-abort — not dirty-defer.

    The two merge failures are distinct shapes (guard-1985) with non-
    interchangeable remedies, and this path used to collapse them: the retry
    aborted a real conflict, then the caller's else-branch logged 'merge
    DEFERRED' and ticked 'dirty-defer'. The resulting alarm pointed the reader
    at staged entries / index.lock / dirty shared files, while its companion
    line ruled OUT the shape that had actually occurred ('NOT ... cross-agent
    churn (auto-cleared)') — the churn HAD been cleared, successfully, which is
    precisely why the conflict became reachable. A conflict is also the one
    shape retrying can never clear, so the wrong label costs unbounded cycles.
    """
    origin, a, b = _clone_pair(tmp_path)
    # B rewrites a tracked file A has ALSO committed differently (-> conflict on
    # retry) and adds an agents/<other>/ file A holds dirty+untracked (-> the
    # first merge is refused before starting, so the self-heal engages).
    _commit_file(b, "base.txt", "B v2\n", "B: rewrite base")
    _commit_file(b, "agents/otheragent/note.txt", "B note\n", "B: add note")
    _must(b, "push", "-q", "origin", "main")
    _commit_file(a, "base.txt", "A v2\n", "A: rewrite base differently")
    (a / "agents" / "otheragent").mkdir(parents=True)
    (a / "agents" / "otheragent" / "note.txt").write_text("A churn\n", encoding="utf-8")

    env = {**os.environ, "MIND_AGENT": "testagent",
           "ITERATION_PUSH_DEFER_STREAK_ALARM": "1"}
    (a / "agents" / "testagent").mkdir(parents=True)

    r = subprocess.run(
        [BASH, str(PUSH_SH), "--repo", str(a), *_default_flags()],
        capture_output=True, text=True, timeout=120, env=env,
    )
    assert r.returncode == 0, r.stderr              # fail-soft, never blocks
    err = r.stderr
    # the self-heal did engage and did clear the cross-agent churn
    assert "clearing 0 tracked + 1 untracked cross-agent file(s)" in err, err
    # ...and the residual failure is reported as a CONFLICT, not a dirty defer
    assert "conflict-abort" in err, err
    assert "dirty-defer" not in err, err
    assert "TRUE cross-machine content conflict" in err, err
    assert "merge DEFERRED" not in err, err
    # the alarm's remedy is the conflict remedy, not the dirty-tree one
    assert "resolve it by hand" in err, err
    assert "index.lock contention" not in err, err
    # the tree is never left mid-merge for the loop to trip over
    assert not (a / ".git" / "MERGE_HEAD").exists()
    # the streak still counts this failure
    streak = a / ".git" / "iteration-push-defer-streak"
    assert streak.is_file() and streak.read_text().split()[0] == "1"
    health = list((a / "agents" / "testagent" / "health").glob("*.jsonl"))
    assert health, "no health JSONL written at escalation"
    assert '"shape":"conflict-abort"' in health[0].read_text(encoding="utf-8")


def test_defer_streak_survives_dry_run(tmp_path):
    """--dry-run proves nothing about the merge, so it must not reset a real
    streak (the reset is guarded by DRY_RUN)."""
    origin, a, b = _clone_pair(tmp_path)
    _commit_file(b, "base.txt", "B v2\n", "B: rewrite base")
    _must(b, "push", "-q", "origin", "main")
    (a / "base.txt").write_text("A dirty uncommitted\n", encoding="utf-8")

    streak = a / ".git" / "iteration-push-defer-streak"
    r = _run_push(a, *_default_flags())
    assert r.returncode == 0
    assert streak.is_file() and streak.read_text().split()[0] == "1"

    r = _run_push(a, *_default_flags("--dry-run"))
    assert r.returncode == 0
    assert streak.is_file() and streak.read_text().split()[0] == "1", \
        "dry-run must neither tick nor reset the streak"


def test_repeating_conflict_prints_escalation_directive_once(tmp_path):
    """: a conflict-abort is the one shape retrying can never clear,
    so at the 2nd consecutive conflict the tick prints a caller-facing
    ESCALATION REQUIRED directive — exactly once per streak (marker keyed on
    the streak's `since` stamp), with the shape persisted as the streak file's
    3rd field so the repeat is attributable."""
    origin, a, b = _clone_pair(tmp_path)
    # TRUE content conflict: both sides COMMIT divergent rewrites of base.txt
    # (clean tree on A, so the merge starts and conflicts — not a dirty defer).
    _commit_file(b, "base.txt", "B v2\n", "B: rewrite base")
    _must(b, "push", "-q", "origin", "main")
    _commit_file(a, "base.txt", "A v2\n", "A: rewrite base differently")

    streak = a / ".git" / "iteration-push-defer-streak"
    marker = a / ".git" / "iteration-push-defer-streak-escalated"

    r1 = _run_push(a, *_default_flags())
    assert r1.returncode == 0, r1.stderr            # fail-soft, never blocks
    assert "TRUE cross-machine content conflict" in r1.stderr, r1.stderr
    assert "ESCALATION REQUIRED" not in r1.stderr, "must not escalate at n=1"
    fields = streak.read_text().split()
    assert fields[0] == "1" and fields[2] == "conflict-abort", fields

    r2 = _run_push(a, *_default_flags())
    assert r2.returncode == 0, r2.stderr
    assert "REPEATING MERGE CONFLICT — ESCALATION REQUIRED" in r2.stderr, r2.stderr
    assert marker.is_file(), "one-shot marker must persist the streak's since stamp"
    assert marker.read_text().strip() == streak.read_text().split()[1]

    r3 = _run_push(a, *_default_flags())
    assert r3.returncode == 0, r3.stderr
    assert "ESCALATION REQUIRED" not in r3.stderr, \
        "directive is once-per-streak; n=3 must not re-print"
    assert streak.read_text().split()[0] == "3"


def test_repeating_dirty_defer_prints_its_own_escalation_directive_once(tmp_path):
    """: a repeating DEFER strands the box exactly as a repeating
    CONFLICT does, so it gets an escalation on the same contract.

    THE ASYMMETRY THIS PINS. Before this change the defer lane emitted nothing
    a caller could act on: the ⚠ streak WARNING starts at 3 while the conflict
    lane escalates at 2, it re-prints every cycle instead of once per streak,
    and worker-loop Phase -0.3 greps only the '— ESCALATION REQUIRED (g-'
    headline. Measured cc-08 2026-08-20: two consecutive defers on a dirty
    repo-root blocker-gate-overrides.jsonl, 39 commits behind and climbing,
    ZERO escalation, while origin/main ALREADY CARRIED the fix.

    The n=1 assertion is the discriminator, not decoration: without it this
    test would pass against a directive that fired on EVERY defer, which is the
    noise the once-per-streak marker exists to prevent.
    """
    origin, a, b = _clone_pair(tmp_path)
    # DIRTY-TREE defer (not a conflict): B commits, A leaves base.txt dirty and
    # uncommitted, so git refuses to START the merge. Same shape as
    # test_defer_streak_survives_dry_run above.
    _commit_file(b, "base.txt", "B v2\n", "B: rewrite base")
    _must(b, "push", "-q", "origin", "main")
    (a / "base.txt").write_text("A dirty uncommitted\n", encoding="utf-8")

    streak = a / ".git" / "iteration-push-defer-streak"
    marker = a / ".git" / "iteration-push-defer-streak-escalated-defer"
    conflict_marker = a / ".git" / "iteration-push-defer-streak-escalated"

    r1 = _run_push(a, *_default_flags())
    assert r1.returncode == 0, r1.stderr            # fail-soft, never blocks
    assert "ESCALATION REQUIRED" not in r1.stderr, "must not escalate at n=1"
    fields = streak.read_text().split()
    assert fields[0] == "1" and fields[2] == "dirty-defer", fields

    r2 = _run_push(a, *_default_flags())
    assert r2.returncode == 0, r2.stderr
    assert "REPEATING INTEGRATE DEFER — ESCALATION REQUIRED" in r2.stderr, r2.stderr
    # A SEPARATE headline from the conflict lane (guard-2586: a fallback path
    # and a failure path must never emit the same message — here the remedies
    # are opposite, clear-the-file vs hand-resolve-the-merge).
    assert "REPEATING MERGE CONFLICT" not in r2.stderr, \
        "the two lanes must stay distinguishable; their remedies are opposite"
    # ...but both carry the tail worker-loop Phase -0.3 branches on, so one
    # predicate covers both shapes.
    assert "— ESCALATION REQUIRED (g-" in r2.stderr, r2.stderr
    # The whole point of carrying $2 through: the blocking path is NAMED, so the
    # escalation is actionable without re-deriving it on the wedged box.
    assert "base.txt" in r2.stderr, r2.stderr
    assert "Blocking path(s):" in r2.stderr, r2.stderr

    assert marker.is_file(), "one-shot marker must persist the streak's since stamp"
    assert marker.read_text().strip() == streak.read_text().split()[1]
    assert not conflict_marker.is_file(), \
        "the defer lane must not consume the conflict lane's marker"

    r3 = _run_push(a, *_default_flags())
    assert r3.returncode == 0, r3.stderr
    assert "ESCALATION REQUIRED" not in r3.stderr, \
        "directive is once-per-streak; n=3 must not re-print"
    assert streak.read_text().split()[0] == "3"
    # n=3 is where the pre-existing ⚠ WARNING starts. It still fires, and that
    # is deliberate — the directive replaced nothing, it filled a hole below it.
    assert "INTEGRATE-DEFER STREAK" in r3.stderr, r3.stderr


def test_streak_reset_clears_BOTH_lane_markers(tmp_path):
    """A surviving marker silently suppresses the NEXT streak of its shape.

    Pins the reset, not the tick: `_ip_defer_streak_reset` removes the streak
    file plus both `-escalated` markers. If only the conflict marker were
    cleared, a box that wedged, recovered and re-wedged on a dirty file would
    escalate exactly once in its lifetime and then go quiet forever — the
    failure mode is invisible because the second wedge looks identical to the
    first from inside the box.
    """
    origin, a, b = _clone_pair(tmp_path)
    _commit_file(b, "base.txt", "B v2\n", "B: rewrite base")
    _must(b, "push", "-q", "origin", "main")
    (a / "base.txt").write_text("A dirty uncommitted\n", encoding="utf-8")

    streak = a / ".git" / "iteration-push-defer-streak"
    marker = a / ".git" / "iteration-push-defer-streak-escalated-defer"

    _run_push(a, *_default_flags())
    r2 = _run_push(a, *_default_flags())
    assert "REPEATING INTEGRATE DEFER" in r2.stderr, r2.stderr
    assert marker.is_file() and streak.is_file()

    # Clear the blocker; the next run integrates and must reset BOTH.
    _must(a, "checkout", "--", "base.txt")
    r3 = _run_push(a, *_default_flags())
    assert r3.returncode == 0, r3.stderr
    assert not streak.is_file(), "a clean integrate must reset the streak"
    assert not marker.is_file(), \
        "a surviving defer marker suppresses the NEXT dirty-file wedge forever"


# --------------------------------------------------------------------------- #
# tracked .mind-data/ under a NON-local backend ()
# --------------------------------------------------------------------------- #
def test_tracked_mind_data_commits_under_own_cloud(tmp_path):
    """A dirty, merge-blocking .mind-data/ path must be COMMITTED whenever git
    TRACKS .mind-data/ — including under own-cloud, where the old arm gated on
    `_backend = local` and deferred instead.

    This is the g-115-5703 shape, measured on ZDS-Mind (cc-06, own-cloud) where
    .mind-data/ has been git-tracked since 2026-07-28: every close phase
    re-dirties the world/meta ledgers that iteration-commit just staged, so the
    merge deferred on EVERY iteration -- a permanent stall wearing a transient's
    message, silent because rc=2 does not fail the loop.

    Pre-fix this asserts False on the very first assertion: the run logs
    'blocking file outside agents/* (.mind-data/world/changelog.jsonl) — defer'.
    """
    origin, a, b = _clone_pair(tmp_path)
    ledger = ".mind-data/world/changelog.jsonl"

    # Make .mind-data/ TRACKED on both sides — that is what the fix keys on.
    _commit_file(a, ledger, '{"seq":1}\n', "A: seed tracked mind-data ledger")
    _must(a, "push", "-q", "origin", "main")
    _must(b, "pull", "-q", "origin", "main")

    # B advances the ledger; A holds the SAME path dirty => git refuses the
    # merge with "local changes would be overwritten", which is the real error
    # the deferral message never surfaced.
    _commit_file(b, ledger, '{"seq":1}\n{"seq":2}\n', "B: append")
    _must(b, "push", "-q", "origin", "main")
    (a / ledger).write_text('{"seq":1}\n{"seq":3}\n', encoding="utf-8")

    env = {**os.environ, "MIND_AGENT": "testagent", "STORAGE_BACKEND": "own-cloud"}
    (a / "agents" / "testagent").mkdir(parents=True)
    r = subprocess.run(
        [BASH, str(PUSH_SH), "--repo", str(a), *_default_flags()],
        capture_output=True, text=True, timeout=120, env=env,
    )
    out = r.stdout + r.stderr

    assert "blocking file outside agents/*" not in out, (
        "tracked .mind-data/ must not veto the self-heal under own-cloud "
        f"(g-115-5703). Output:\n{out}"
    )
    assert "storage-root" in out, (
        f"expected the storage-root commit path to fire. Output:\n{out}"
    )
    # The heal must COMMIT the ledger, never discard it: A's own row survives.
    assert '{"seq":3}' in (a / ledger).read_text(encoding="utf-8"), \
        "self-heal must commit .mind-data churn, never checkout -- it away"
    assert r.returncode == 0


# --------------------------------------------------------------------------- #
# : serialization churn must not strand the box
#
# Measured 2026-08-10 (echo, cc-03): 3 consecutive integrate-defers, 49 commits
# behind, unable to push -- on two files whose PARSED content was identical to
# HEAD (672 vs 672 record ids, zero records differing on any field; the .yaml
# differed only by CRLF). The predicate was "git says this path differs", which
# cannot tell a partner's real work from a re-serializing writer's key order.
#
# The gate that actually deferred is the STAGED-INDEX one, not a dirty-path arm:
# a cross-agent dirty file is routed to cross_dirty and restored, never deferred.
# Measured on cc-07 (.git/iteration-push.log, 1719 lines): 1 of 1 deferred merges
# came from the staged gate, 0 from any dirty-path arm.
# --------------------------------------------------------------------------- #

def _env_agent(name: str = "testagent") -> dict:
    return {**os.environ, "MIND_AGENT": name, "STORAGE_BACKEND": "local"}


def _run_push_as(repo: Path, agent: str, *flags: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [BASH, str(PUSH_SH), "--repo", str(repo), *flags],
        capture_output=True, text=True, timeout=120, env=_env_agent(agent),
    )


PARTNER_JSONL = "agents/partner/experience.jsonl"
CANON = '{"id":"e1","kind":"note","n":1}\n{"id":"e2","kind":"note","n":2}\n'
REORDERED = '{"n":1,"id":"e1","kind":"note"}\n{"kind":"note","n":2,"id":"e2"}\n'
REAL_EDIT = '{"id":"e1","kind":"note","n":1}\n{"id":"e2","kind":"note","n":999}\n'


def _setup_merge_required(tmp_path):
    """A and B both touch the partner file; A ends up behind AND ahead."""
    origin, a, b = _clone_pair(tmp_path)
    _commit_file(a, PARTNER_JSONL, CANON, "seed partner store")
    _must(a, "push", "-q", "origin", "main")
    _must(b, "pull", "-q", "origin", "main")
    # B advances the SAME file, so the merge must touch it.
    _commit_file(b, PARTNER_JSONL,
                 CANON.replace('"n":2', '"n":22'), "B: real partner work")
    _must(b, "push", "-q", "origin", "main")
    _commit_file(a, "from_a.txt", "a\n", "A: local work")   # A ahead -> non-ff
    return origin, a, b


def test_staged_serialization_churn_unstages_instead_of_deferring(tmp_path):
    """THE measured case: staged key-order churn must not strand the merge."""
    origin, a, b = _setup_merge_required(tmp_path)
    (a / PARTNER_JSONL).write_text(REORDERED, encoding="utf-8", newline="\n")
    _must(a, "add", PARTNER_JSONL)                    # STAGED, churn-only
    assert _must(a, "diff", "--cached", "--name-only") == PARTNER_JSONL

    r = _run_push_as(a, "testagent", *_default_flags())

    assert "SEMANTICALLY identical" in r.stderr, r.stderr
    assert "guard-741, defer" not in r.stderr, r.stderr
    # The merge actually completed and B's real work survived.
    _must(a, "fetch", "-q", "origin", "main")
    assert '"n":22' in (a / PARTNER_JSONL).read_text(encoding="utf-8")


def test_staged_real_partner_work_still_defers(tmp_path):
    """FAIL-SAFE CONTROL: a genuine staged difference must still defer.

    Same wedged tree as the test above -- the ONLY difference is that the staged
    content genuinely differs from HEAD. If this ever passes, the discriminator
    has been inverted and the self-heal is discarding a partner's work.
    """
    origin, a, b = _setup_merge_required(tmp_path)
    (a / PARTNER_JSONL).write_text(REAL_EDIT, encoding="utf-8", newline="\n")
    _must(a, "add", PARTNER_JSONL)                    # STAGED, REAL change

    r = _run_push_as(a, "testagent", *_default_flags())

    assert "guard-741, defer" in r.stderr, r.stderr
    assert "SEMANTICALLY identical" not in r.stderr, r.stderr
    # The staged work is still staged -- nothing was discarded.
    assert _must(a, "diff", "--cached", "--name-only") == PARTNER_JSONL


def test_staged_unparseable_churn_still_defers(tmp_path):
    """A file the comparator cannot parse is NOT proven identical -> defer."""
    origin, a, b = _setup_merge_required(tmp_path)
    (a / PARTNER_JSONL).write_text("this is not json\n", encoding="utf-8",
                                   newline="\n")
    _must(a, "add", PARTNER_JSONL)

    r = _run_push_as(a, "testagent", *_default_flags())
    assert "guard-741, defer" in r.stderr, r.stderr


def test_dirty_shared_yaml_crlf_churn_does_not_block_merge(tmp_path):
    """The .yaml/CRLF half: a shared file differing only by line endings.

    Exercises the dirty-path arm (`*)`) rather than the staged gate, which is
    the case the goal's verification names explicitly.
    """
    origin, a, b = _clone_pair(tmp_path)
    _commit_file(a, "shared/config.yaml", "alpha: 1\nbeta: 2\n", "seed shared")
    _must(a, "push", "-q", "origin", "main")
    _must(b, "pull", "-q", "origin", "main")
    _commit_file(b, "shared/config.yaml", "alpha: 1\nbeta: 3\n", "B: real edit")
    _must(b, "push", "-q", "origin", "main")
    _commit_file(a, "from_a.txt", "a\n", "A: local work")
    # A's copy differs from its OWN HEAD by line endings only (a Windows writer).
    (a / "shared/config.yaml").write_text("alpha: 1\r\nbeta: 2\r\n",
                                          encoding="utf-8", newline="")

    r = _run_push_as(a, "testagent", *_default_flags())

    assert "only by serialization" in r.stderr, r.stderr
    assert "blocking file outside agents/*" not in r.stderr, r.stderr


def test_semantic_identity_helper_verdicts():
    """Unit-pin the comparator itself, including the fail-safe direction."""
    sys.path.insert(0, str(SCRIPT_DIR.parent))
    from semantic_identity import compare, IDENTICAL, DIFFERENT, UNPARSEABLE

    assert compare(CANON, REORDERED, "e.jsonl") == IDENTICAL
    assert compare("a: 1\n", "a: 1\r\n", "c.yaml") == IDENTICAL
    assert compare("x", "x", "anything.bin") == IDENTICAL
    assert compare(CANON, REAL_EDIT, "e.jsonl") == DIFFERENT
    # ORDER is content for an append-only store -- must NOT read as identical.
    assert compare('{"id":"a"}\n{"id":"b"}\n', '{"id":"b"}\n{"id":"a"}\n',
                   "e.jsonl") == DIFFERENT
    assert compare(CANON, "not json", "e.jsonl") == UNPARSEABLE
    assert compare("a: [1,2\n", "a: [1,2]\n", "c.yaml") == UNPARSEABLE
    assert compare("p", "q", "blob.bin") == UNPARSEABLE


# ---------------------------------------------------------------------------
# : DURABLE cross-agent state must not be cleared unconditionally.
#
# The agents/* self-heal branch restored/deleted anything git named as blocking
# with NO content check, while both sibling branches refused to (.mind-data/*
# COMMITS, the shared *) arm clears only on a provable `identical`). These pin
# the third branch's version of that refusal, scoped to identity + the learning
# archive. Non-durable cross-agent churn (state.jsonl, new.jsonl) still clears —
# t1/t2 above are the unchanged control.
# ---------------------------------------------------------------------------

def test_selfheal_durable_crossagent_content_divergence_defers(tmp_path):
    """ outcome 1: agents/<other>/aspirations.jsonl with GENUINE
    content divergence defers instead of being checkout-restored to HEAD.
    `checkout --` is unrecoverable, so a wrong clear destroys partner work."""
    origin, a, b = _clone_pair(tmp_path)
    _seed_and_sync(a, b, {"agents/bravo/aspirations.jsonl": CANON})
    _commit_file(b, "agents/bravo/aspirations.jsonl",
                 '{"id":"e3","kind":"note","n":3}\n', "B: bravo asp v2")
    _must(b, "push", "-q", "origin", "main")
    _commit_file(a, "core/scripts/dur1.sh", "echo dur1\n", "A: framework work")
    # A holds UNCOMMITTED, genuinely-different content on a DURABLE path
    (a / "agents/bravo/aspirations.jsonl").write_text(
        REAL_EDIT, encoding="utf-8", newline="\n")

    r = _run_push_env(a, "alpha", *_default_flags("--strict"))
    assert r.returncode == 1, f"should defer, not clear: {r.stderr}"
    assert "merge DEFERRED" in r.stderr
    assert "g-115-6145" in r.stderr, r.stderr
    # THE POINT: the uncommitted partner work is still on disk, byte-identical.
    assert (a / "agents/bravo/aspirations.jsonl").read_text() == REAL_EDIT


def test_selfheal_durable_crossagent_serialization_only_still_clears(tmp_path):
    """ outcome 2: a DURABLE path differing from HEAD only by key
    ORDER carries nothing to lose, so it still clears (g-115-5717 unregressed).
    Without this the fix would trade a data-loss bug for a permanent stall."""
    origin, a, b = _clone_pair(tmp_path)
    _seed_and_sync(a, b, {"agents/bravo/aspirations.jsonl": CANON})
    _commit_file(b, "agents/bravo/aspirations.jsonl",
                 '{"id":"e9","kind":"note","n":9}\n', "B: bravo asp v2")
    _must(b, "push", "-q", "origin", "main")
    _commit_file(a, "core/scripts/dur2.sh", "echo dur2\n", "A: framework work")
    # Semantically IDENTICAL to A's HEAD — same records, reordered keys.
    (a / "agents/bravo/aspirations.jsonl").write_text(
        REORDERED, encoding="utf-8", newline="\n")

    r = _run_push_env(a, "alpha", *_default_flags("--strict"))
    assert r.returncode == 0, f"serialization-only must still clear: {r.stderr}"
    assert "self-heal: clearing" in r.stderr
    assert "push OK" in r.stderr
    assert (a / "agents/bravo/aspirations.jsonl").read_text() == \
        '{"id":"e9","kind":"note","n":9}\n'


# ---------------------------------------------------------------------------
# : the defer predicate must compare local against the INCOMING
# version, not only against HEAD.
#
# HEAD is the version this box is moving AWAY from. own-cloud sync routinely
# writes the partner's incoming bytes into the working copy between fetch and
# merge, which makes local `different` from HEAD and IDENTICAL to origin — a
# file with nothing to lose. The HEAD-only test deferred on it, and could not
# self-clear (each sync re-dirties it), so the box rode to the stranded alarm.
# Measured three times by three operators before the predicate was changed.
# ---------------------------------------------------------------------------

def test_selfheal_durable_untracked_crossagent_defers(tmp_path):
    """: an UNTRACKED durable cross-agent file has NO HEAD side, so
    `git clean` destroys it with no recovery path at all — strictly worse than
    the tracked case. No semantic verdict is possible; defer unconditionally."""
    origin, a, b = _clone_pair(tmp_path)
    _commit_file(b, "agents/bravo/self.md", "# bravo identity\n",
                 "B: new bravo self.md")
    _must(b, "push", "-q", "origin", "main")
    _commit_file(a, "core/scripts/dur3.sh", "echo dur3\n", "A: framework work")
    (a / "agents" / "bravo").mkdir(parents=True, exist_ok=True)
    (a / "agents/bravo/self.md").write_text(
        "# LOCAL UNCOMMITTED IDENTITY\n", encoding="utf-8", newline="\n")

    r = _run_push_env(a, "alpha", *_default_flags("--strict"))
    assert r.returncode == 1, f"should defer, not delete: {r.stderr}"
    assert "merge DEFERRED" in r.stderr
    assert "g-115-6145" in r.stderr, r.stderr
    # THE POINT: the unrecoverable file still exists with its local content.
    assert (a / "agents/bravo/self.md").read_text() == \
        "# LOCAL UNCOMMITTED IDENTITY\n"


# ---------------------------------------------------------------------------
# : the defer above is CORRECT and is also half of a DEADLOCK.
#
# iteration-push refuses to clear the file (never clobber partner divergence),
# and iteration-commit.sh's namespace filter refuses to commit it (never commit
# another agent's namespace). Both are right; nothing arbitrates between them,
# so the file stays dirty forever and every later merge defers on it. Measured
# cc-07 2026-08-17: six consecutive integrate failures, behind=12, ahead 27->28,
# sole blocker agents/zeta/aspirations.jsonl. T_recovery = INFINITY.
#
# The arbiter the deadlock lacks already exists — git's own commutative merge
# driver. When one is CONFIGURED for the path, the third option is COMMIT, and
# the driver unions at the merge.
#
# The two legs are independent in production and so are they here: .gitattributes
# is version-controlled, the driver lives in unversioned .git/config. Testing the
# attribute alone would pass on a box where the union guarantee does not exist.
# ---------------------------------------------------------------------------

def _install_ledger_attrs(repo: Path, pattern: str = "agents/*/aspirations.jsonl") -> None:
    """Version-controlled half: the merge ATTRIBUTE, present in every clone."""
    _commit_file(repo, ".gitattributes", f"{pattern} merge=ayoai-ledger\n",
                 "attrs: route the ledger")


def _install_ledger_driver(repo: Path) -> None:
    """Unversioned half: the DRIVER, registered per-clone by install-git-hooks.sh."""
    _must(repo, "config", "merge.ayoai-ledger.driver",
          f"{BASH} {CORE_SCRIPTS / 'git-merge-ayoai-ledger.sh'} %O %A %B %P")
    _must(repo, "config", "merge.ayoai-ledger.name", "test ledger merge")


def _durable_wedge_fixture(tmp_path):
    """The measured deadlock: A holds uncommitted divergence on a partner's
    durable ledger that also blocks an incoming merge."""
    origin, a, b = _clone_pair(tmp_path)
    _seed_and_sync(a, b, {"agents/bravo/aspirations.jsonl": CANON})
    _commit_file(b, "agents/bravo/aspirations.jsonl",
                 '{"id":"e3","kind":"note","n":3}\n', "B: bravo asp v2")
    _must(b, "push", "-q", "origin", "main")
    _commit_file(a, "core/scripts/wedge.sh", "echo wedge\n", "A: framework work")
    (a / "agents/bravo/aspirations.jsonl").write_text(
        REAL_EDIT, encoding="utf-8", newline="\n")
    return origin, a, b


def test_selfheal_durable_crossagent_commits_when_git_can_merge_it(tmp_path):
    """ outcome 1: with BOTH legs present the file is COMMITTED, not
    deferred — so the dirty path that could be neither cleared nor committed is
    gone and the box can integrate again."""
    origin, a, b = _durable_wedge_fixture(tmp_path)
    _install_ledger_attrs(a)
    _install_ledger_driver(a)

    r = _run_push_env(a, "alpha", *_default_flags("--strict"))
    assert "g-115-6572" in r.stderr, r.stderr
    # The defer arm must NOT have fired — that is the deadlock half being fixed.
    assert "g-115-6145" not in r.stderr, r.stderr
    # THE POINT: the wedge is gone. The path is no longer dirty, so it can never
    # again block the merge on the next iteration, and the one on the one after.
    assert _must(a, "status", "--porcelain", "--",
                 "agents/bravo/aspirations.jsonl").strip() == ""
    # ...and the partner's divergence was PRESERVED into git rather than
    # discarded: `checkout --` would have destroyed exactly this content.
    assert "999" in _must(a, "show", "HEAD:agents/bravo/aspirations.jsonl")


def test_selfheal_durable_crossagent_defers_when_driver_is_not_configured(tmp_path):
    """ outcome 2 — THE SECOND LEG, and the one that rots silently.

    .gitattributes ships in every clone, so the attribute is present even on a
    box where install-git-hooks.sh never ran. There git falls back to its default
    text merge and the union guarantee does not exist, so committing a partner's
    ledger would be an ordinary conflicting write. Attribute-only must DEFER."""
    origin, a, b = _durable_wedge_fixture(tmp_path)
    _install_ledger_attrs(a)          # attribute present...
    # ...driver deliberately NOT registered.

    r = _run_push_env(a, "alpha", *_default_flags("--strict"))
    assert r.returncode == 1, f"attribute without driver must defer: {r.stderr}"
    assert "g-115-6145" in r.stderr, r.stderr
    assert "g-115-6572" not in r.stderr, r.stderr
    assert (a / "agents/bravo/aspirations.jsonl").read_text() == REAL_EDIT


def test_selfheal_durable_crossagent_defers_for_unmergeable_paths(tmp_path):
    """ outcome 3: scope is not over-wide. self.md carries NO merge
    attribute — an identity file is not commutative and there is nothing to
    union — so it still defers even with the driver fully configured."""
    origin, a, b = _clone_pair(tmp_path)
    _seed_and_sync(a, b, {"agents/bravo/self.md": "# bravo v1\n"})
    _commit_file(b, "agents/bravo/self.md", "# bravo v2\n", "B: bravo self v2")
    _must(b, "push", "-q", "origin", "main")
    _commit_file(a, "core/scripts/scope.sh", "echo scope\n", "A: framework work")
    _install_ledger_attrs(a)          # routes aspirations.jsonl, NOT self.md
    _install_ledger_driver(a)
    (a / "agents/bravo/self.md").write_text(
        "# LOCAL UNCOMMITTED IDENTITY\n", encoding="utf-8", newline="\n")

    r = _run_push_env(a, "alpha", *_default_flags("--strict"))
    assert r.returncode == 1, f"unmergeable path must still defer: {r.stderr}"
    assert "g-115-6145" in r.stderr, r.stderr
    assert (a / "agents/bravo/self.md").read_text() == \
        "# LOCAL UNCOMMITTED IDENTITY\n"


# : the clear decision must compare local vs the INCOMING origin
# version, not local vs HEAD.
#
# Under own-cloud the sync applies locally the exact bytes the pending merge is
# about to deliver. That content is identical to origin and DIFFERENT from the
# stale HEAD, so the HEAD-only predicate deferred forever and the wedge could
# never self-clear — measured three times by two operators, each freed by
# hand-proving the blob equal to origin. The pair below pins both directions:
# identical-to-origin now clears, and different-from-BOTH still defers
# ( unregressed).
# ---------------------------------------------------------------------------

def test_selfheal_durable_crossagent_identical_to_origin_clears(tmp_path):
    """ outcome 1: a durable cross-agent file whose content is
    byte-identical to the INCOMING origin version no longer defers.

    RED against pre-fix HEAD (the HEAD-only predicate sees `different` and
    defers), because A's local copy matches what B pushed, not A's HEAD."""
    origin, a, b = _clone_pair(tmp_path)
    _seed_and_sync(a, b, {"agents/bravo/aspirations.jsonl": CANON})
    incoming = '{"id":"e7","kind":"note","n":7}\n'
    _commit_file(b, "agents/bravo/aspirations.jsonl", incoming, "B: bravo asp v2")
    _must(b, "push", "-q", "origin", "main")
    _commit_file(a, "core/scripts/dur6538a.sh", "echo dur\n", "A: framework work")
    # The own-cloud sync has already landed B's exact bytes in A's worktree:
    # DIFFERENT from A's HEAD (still CANON), IDENTICAL to origin/main.
    (a / "agents/bravo/aspirations.jsonl").write_text(
        incoming, encoding="utf-8", newline="\n")

    r = _run_push_env(a, "alpha", *_default_flags("--strict"))
    assert r.returncode == 0, f"identical-to-origin must clear: {r.stderr}"
    assert "push OK" in r.stderr, r.stderr
    # Nothing was lost: the merge delivered the very bytes the clear discarded.
    assert (a / "agents/bravo/aspirations.jsonl").read_text() == incoming


def test_selfheal_durable_crossagent_differs_from_both_still_defers(tmp_path):
    """ outcome 2: content that diverges from BOTH HEAD and origin
    still defers and is never cleared — g-115-6145 unregressed.

    This is the arm that must NOT widen: here the local bytes exist nowhere
    else, so a clear would destroy them with no recovery path.

    ITS FORCED-FAILURE CONTROL IS A MUTATION, MEASURED RATHER THAN ASSUMED
    (guard-3534), and it needs one because this arm is green both before and
    after the fix by construction — so a pre-fix run cannot prove it tests
    anything. Inverting the origin comparison (`!= identical` ->
    `= identical`) makes the predicate clear on genuine divergence and takes
    this test RED. Dropping the HEAD leg instead does NOT fail it: that
    mutation was run and this test stayed green, because REAL_EDIT differs
    from origin too. Carried here from a duplicate implementation of this
    same goal (cc-07, 2026-08-18) whose test twin was dropped in the merge —
    the measurement is the only part of it that was not redundant."""
    origin, a, b = _clone_pair(tmp_path)
    _seed_and_sync(a, b, {"agents/bravo/aspirations.jsonl": CANON})
    _commit_file(b, "agents/bravo/aspirations.jsonl",
                 '{"id":"e8","kind":"note","n":8}\n', "B: bravo asp v2")
    _must(b, "push", "-q", "origin", "main")
    _commit_file(a, "core/scripts/dur6538b.sh", "echo dur\n", "A: framework work")
    # Matches neither HEAD (CANON) nor origin (e8) — genuinely unique local work.
    (a / "agents/bravo/aspirations.jsonl").write_text(
        REAL_EDIT, encoding="utf-8", newline="\n")

    r = _run_push_env(a, "alpha", *_default_flags("--strict"))
    assert r.returncode == 1, f"divergent-from-both must defer: {r.stderr}"
    assert "merge DEFERRED" in r.stderr
    assert "g-115-6538" in r.stderr, r.stderr
    assert (a / "agents/bravo/aspirations.jsonl").read_text() == REAL_EDIT


# ---------------------------------------------------------------------------
# : a defer must not abandon self-namespace churn already collected.
#
# self_paths is populated inside the classification loop but STAGED after it, so
# every one of the six defer arms used to `return 1` past the staging. The
# routine trigger is the  arm: a partner's durable store goes dirty
# from own-cloud sync, the classifier correctly refuses to clear it, and this
# Body's own ledger churn is stranded uncommitted as collateral — where it
# cannot travel on refs/workers/<agent>/<sid> and the reducer never sees it.
# Measured cc-07 2026-08-16: 3 consecutive dirty-defer cycles at behind=17
# ahead=6 with agents/alpha/{aspirations,changelog,experience}.jsonl dirty
# throughout.
#
# Both tests below assert the SAME two properties together, because either one
# alone is satisfiable by a wrong fix: committing self churn (the repair) AND
# leaving the partner file untouched (, which must not be weakened to
# get it). The existing durable-defer tests above are the no-self-churn control.
# ---------------------------------------------------------------------------

def test_selfheal_defer_commits_self_churn_before_giving_up(tmp_path):
    """ outcomes 1+2. A durable partner file defers the merge; the
    self-namespace churn dirty alongside it is committed anyway."""
    origin, a, b = _clone_pair(tmp_path)
    _seed_and_sync(a, b, {"agents/bravo/aspirations.jsonl": CANON,
                          "agents/alpha/ledger.jsonl": "v1\n"})
    _commit_file(b, "agents/bravo/aspirations.jsonl",
                 '{"id":"e7","kind":"note","n":7}\n', "B: bravo asp v2")
    _must(b, "push", "-q", "origin", "main")
    _commit_file(a, "core/scripts/g6373a.sh", "echo a\n", "A: framework work")
    # SELF churn (must survive) + DURABLE partner divergence (must defer)
    (a / "agents/alpha/ledger.jsonl").write_text(
        "dirty-self\n", encoding="utf-8", newline="\n")
    (a / "agents/bravo/aspirations.jsonl").write_text(
        REAL_EDIT, encoding="utf-8", newline="\n")

    r = _run_push_env(a, "alpha", *_default_flags("--strict"))

    # THE DEFER IS UNCHANGED — still defers, still clears nothing.
    assert r.returncode == 1, f"must still defer: {r.stderr}"
    assert "merge DEFERRED" in r.stderr
    assert "g-115-6145" in r.stderr, r.stderr
    assert (a / "agents/bravo/aspirations.jsonl").read_text() == REAL_EDIT, \
        "the defer was weakened — partner work was cleared"
    # THE FIX — self churn reached a commit despite the defer.
    assert _must(a, "show", "HEAD:agents/alpha/ledger.jsonl") == "dirty-self", \
        "self-namespace churn was abandoned by the defer (the g-115-6373 bug)"
    assert "g-115-6373" in r.stderr, r.stderr


def test_selfheal_defer_commits_self_churn_when_self_sorts_after_partner(tmp_path):
    """The ordering half, and the reason the fix RECORDS the defer rather than
    breaking out of the loop.

    `git diff --name-only` is sorted, so whether a self path is classified
    before the blocking partner path is decided by how the agent's own name
    sorts against the partner's. Here it sorts AFTER (zeta > bravo), so the
    defer is reached while self_paths is still EMPTY. A fix that committed
    what it had at the moment of the defer would pass the test above and fail
    this one — leaving the bug live for every agent whose name sorts late."""
    origin, a, b = _clone_pair(tmp_path)
    _seed_and_sync(a, b, {"agents/bravo/aspirations.jsonl": CANON,
                          "agents/zeta/ledger.jsonl": "v1\n"})
    _commit_file(b, "agents/bravo/aspirations.jsonl",
                 '{"id":"e8","kind":"note","n":8}\n', "B: bravo asp v2")
    _must(b, "push", "-q", "origin", "main")
    _commit_file(a, "core/scripts/g6373b.sh", "echo b\n", "A: framework work")
    (a / "agents/bravo/aspirations.jsonl").write_text(
        REAL_EDIT, encoding="utf-8", newline="\n")
    (a / "agents/zeta/ledger.jsonl").write_text(
        "dirty-self-late\n", encoding="utf-8", newline="\n")
    # Pin the premise rather than assuming it: if git ever stopped emitting the
    # dirty set sorted, this test would silently stop testing ordering at all.
    dirty = _must(a, "diff", "--name-only").splitlines()
    assert dirty.index("agents/bravo/aspirations.jsonl") \
         < dirty.index("agents/zeta/ledger.jsonl"), \
        f"premise broken — partner must be classified first: {dirty}"

    r = _run_push_env(a, "zeta", *_default_flags("--strict"))

    assert r.returncode == 1, f"must still defer: {r.stderr}"
    assert (a / "agents/bravo/aspirations.jsonl").read_text() == REAL_EDIT
    assert _must(a, "show", "HEAD:agents/zeta/ledger.jsonl") == "dirty-self-late", \
        "self churn classified AFTER the defer point was abandoned"


# --------------------------------------------------------------------------- #
#  (wedge shape B): the streak alarm's remedy must match the defer's
# SHAPE. A durable-cross-agent defer and an ordinary dirty-tree defer reach the
# alarm identically, but their remedies are OPPOSITE — the dirty-tree hint says
# "clear it", which for a durable path DESTROYS the partner divergence that
#  deferred to protect. Same class as the conflict-abort/dirty-defer
# split (guard-1985) one layer down.
# --------------------------------------------------------------------------- #
def _run_until_streak_alarm(a: Path, agent: str, n: int = 3) -> list:
    """Run the fail-soft push n times so the 3rd trips INTEGRATE-DEFER STREAK."""
    outs = []
    for _ in range(n):
        r = _run_push_env(a, agent, *_default_flags())
        assert r.returncode == 0, r.stderr          # fail-soft, never blocks
        outs.append(r.stderr)
    return outs


def test_durable_crossagent_defer_streak_names_its_shape_not_clear_it(tmp_path):
    """The alarm on a DURABLE cross-agent defer must state the sanctioned
    procedure and must NOT prescribe clearing.

    guard-2536: the negative assertion ("does not say clear it") is paired with
    a positive reached-the-path marker — the streak file's 3rd field, which is
    the shape string the tick was actually called with. Without that pairing the
    negative would pass just as happily against a run where no alarm fired."""
    origin, a, b = _clone_pair(tmp_path)
    _seed_and_sync(a, b, {"agents/bravo/aspirations.jsonl": CANON})
    _commit_file(b, "agents/bravo/aspirations.jsonl",
                 '{"id":"e3","kind":"note","n":3}\n', "B: bravo asp v2")
    _must(b, "push", "-q", "origin", "main")
    _commit_file(a, "core/scripts/dur6632.sh", "echo dur\n", "A: framework work")
    # Genuinely divergent from BOTH HEAD and origin, and re-dirties every pass.
    (a / "agents/bravo/aspirations.jsonl").write_text(
        REAL_EDIT, encoding="utf-8", newline="\n")

    outs = _run_until_streak_alarm(a, "alpha")

    # REACHED-THE-PATH: the tick recorded THIS shape, not dirty-defer.
    streak = a / ".git" / "iteration-push-defer-streak"
    assert streak.is_file(), "no streak file — the alarm path never ran"
    fields = streak.read_text().split()
    assert fields[0] == "3", fields
    assert fields[2] == "durable-crossagent-defer", \
        f"wrong shape recorded — the split did not fire: {fields}"

    alarm = outs[2]
    assert "INTEGRATE-DEFER STREAK" in alarm, alarm
    assert "SANCTIONED PROCEDURE" in alarm, alarm
    assert "Do NOT clear them" in alarm, alarm
    # The offending path is named, so the operator need not re-derive the set.
    assert "agents/bravo/aspirations.jsonl" in alarm, alarm
    # NEGATIVE: the dirty-tree remedy must not appear on this shape.
    assert "and clear it" not in alarm, \
        f"durable defer still prescribes the FORBIDDEN action: {alarm}"
    # The partner's uncommitted work survived all three passes.
    assert (a / "agents/bravo/aspirations.jsonl").read_text() == REAL_EDIT


def test_ordinary_dirty_defer_still_gets_the_clear_it_hint(tmp_path):
    """POSITIVE CONTROL for the test above. An ordinary dirty-tree defer is
    unchanged: it still reports shape dirty-defer and still says "clear it".
    Without this, the sibling test would pass against a build that simply
    deleted the dirty-tree hint outright."""
    origin, a, b = _clone_pair(tmp_path)
    # Blocker outside agents/* and .mind-data/* — the plain dirty-tree shape.
    _commit_file(b, "base.txt", "B v2\n", "B: rewrite base")
    _must(b, "push", "-q", "origin", "main")
    (a / "base.txt").write_text("A dirty uncommitted\n", encoding="utf-8")

    outs = _run_until_streak_alarm(a, "alpha")

    streak = a / ".git" / "iteration-push-defer-streak"
    assert streak.is_file(), "no streak file — the alarm path never ran"
    fields = streak.read_text().split()
    assert fields[2] == "dirty-defer", f"control shape changed: {fields}"

    alarm = outs[2]
    assert "INTEGRATE-DEFER STREAK" in alarm, alarm
    assert "and clear it" in alarm, alarm
    assert "SANCTIONED PROCEDURE" not in alarm, \
        f"durable hint leaked onto an ordinary dirty defer: {alarm}"


# --------------------------------------------------------------------------- #
# tree-lock: publish-only degrade ()
# --------------------------------------------------------------------------- #
# A held tree lock used to end the WHOLE invocation, --push-worker-ref included, which
# made guard-5291's same-iteration SKILL.md publication unsatisfiable for as long as a
# co-resident suite held the lock. The fix hoists ONE safe action above the gate.
#
# WHAT THESE TESTS PIN, and why it is two tests and not one: the flag is NOT safe by
# nature. In its production arg shape --push-worker-ref runs fetch+integrate and MOVES
# HEAD, so exempting it wholesale would let a worker merge into a tree whose co-resident
# Body is mid-suite. The degrade is what makes it safe, so BOTH halves are asserted --
# the push happens AND the tree does not move -- and the merge modes are asserted still
# refused. A test that only checked "the push happened" would pass on the unsafe fix.
LOCK_SH = CORE_SCRIPTS / "tree-lock.sh"


def _lock_as_peer(project_root: Path, sid: str = "peer-body-sid"):
    env = dict(os.environ, MIND_SID=sid)
    env.pop("BODY_WM_PATH", None)
    return subprocess.run(
        [BASH, str(LOCK_SH), "acquire", "--project-root", str(project_root),
         "--reason", "peer suite running", "--ttl", "600"],
        capture_output=True, text=True, timeout=60, env=env,
    )


def _unlock_as_peer(project_root: Path, sid: str = "peer-body-sid"):
    env = dict(os.environ, MIND_SID=sid)
    env.pop("BODY_WM_PATH", None)
    return subprocess.run(
        [BASH, str(LOCK_SH), "release", "--project-root", str(project_root)],
        capture_output=True, text=True, timeout=60, env=env,
    )


def _run_push_as(repo: Path, agent: str, sid: str, *flags: str):
    env = dict(os.environ, MIND_AGENT=agent, MIND_SID=sid)
    env.pop("BODY_WM_PATH", None)   # guard-3375: never let a test resolve a live Body WM
    return subprocess.run(
        [BASH, str(PUSH_SH), "--repo", str(repo), *flags],
        capture_output=True, text=True, timeout=120, env=env,
    )


def test_held_lock_still_publishes_worker_carrier_ref(tmp_path):
    """A peer lock must NOT starve the carrier push (outcome 0)."""
    origin, a, b = _clone_pair(tmp_path)
    # give origin a commit `a` lacks, so an integrate WOULD move HEAD if it ran
    _commit_file(b, "upstream.txt", "up\n", "upstream commit")
    _must(b, "push", "-q", "origin", "main")

    before = _tip(a)
    assert _lock_as_peer(a).returncode == 0
    try:
        r = _run_push_as(a, "alpha", "unit-sid", "--push-worker-ref",
                         "--fetch-interval-min", "0")
        out = r.stdout + r.stderr
        assert "DEGRADING --push-worker-ref to PUBLISH-ONLY" in out, out
        assert "--push-worker-ref: pushed HEAD" in out, out
        assert "skip, retry next iteration" not in out, out
        # the ref must exist on the REMOTE (guard-1250: ls-remote, never rev-parse)
        ls = _must(a, "ls-remote", "origin", "refs/workers/alpha/unit-sid")
        assert ls.split()[0] == before, f"carrier ref != pre-run HEAD: {ls}"
        # and the tree must NOT have moved -- this is the half that makes it safe
        assert _tip(a) == before, "publish-only degrade moved HEAD (tree-moved hazard)"
        assert "integrating" not in out, out
    finally:
        _unlock_as_peer(a)


def test_held_lock_still_refuses_every_merge_mode(tmp_path):
    """The gate keeps its breadth for anything that touches the tree (outcome 1)."""
    origin, a, b = _clone_pair(tmp_path)
    _commit_file(b, "upstream.txt", "up\n", "upstream commit")
    _must(b, "push", "-q", "origin", "main")

    before = _tip(a)
    assert _lock_as_peer(a).returncode == 0
    try:
        for flags in ([], ["--no-push"], ["--strict"]):
            r = _run_push_as(a, "alpha", "unit-sid",
                             *flags, "--fetch-interval-min", "0")
            out = r.stdout + r.stderr
            assert "skip, retry next iteration" in out, (flags, out)
            assert "DEGRADING" not in out, (flags, out)
            assert _tip(a) == before, f"{flags} merged under a peer lock"
    finally:
        _unlock_as_peer(a)
