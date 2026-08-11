"""Pin -e: locked appends + union merge handlers for the two per-agent
append-only telemetry ledgers — skill-invocations.jsonl and health/<date>.jsonl.

WHAT THE REAL DEFECT IS, and why the concurrency test is shaped the way it is.
The obvious test — N processes appending at once, assert no interleaving — would
be VACUOUS here: on Linux an O_APPEND write below PIPE_BUF is already atomic, so
a bare open(...,"a") passes it. That test would go green against the pre-fix code
and pin nothing.

The corruption actually measured on this repo (2026-08-02, git archaeology over
3900 commits) is whole-file DIVERGENCE, not line tearing: a bare append never
reaches the storage backend, so the record lives only in the local mirror until a
peer's full-file PUT replaces it. agents/alpha/skill-invocations.jsonl went
1303 -> 708 lines inside a commit about sweeping findings-channel triggers, and a
sampled lost record is still absent from origin/main. So the divergence-union
tests below are the ones that carry the goal's value, and each fails pre-fix for
a stated reason.

guard-955: STORAGE_BACKEND is pinned local for every subprocess.
guard-1165: nothing here mutates os.environ or installs sys.modules stubs at
module scope — the pin is per-subprocess env, and the one in-process fixture
restores what it changed.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import coordination_merge as CM  # noqa: E402
import _fileops as F  # noqa: E402

REPO = SCRIPTS.parents[1]


def _rec(i, skill="tree"):
    return {"ts": f"2026-08-02T10:00:{i:02d}", "skill": skill,
            "agent": "zeta", "sid": "s1", "invocation_source": "model"}


def _lines(b):
    return [json.loads(x) for x in b.decode("utf-8").splitlines() if x.strip()]


def _dump(recs):
    return "".join(json.dumps(r, ensure_ascii=True) + "\n" for r in recs).encode()


# ── merge registration ──────────────────────────────────────────────────────
# Each of these returned None before the fix, which is exactly what made the
# backend freeze/clobber these two stores on a both-diverged 412.

@pytest.mark.parametrize("path", [
    "agents/zeta/skill-invocations.jsonl",
    "agents/alpha/skill-invocations.jsonl",
])
def test_skill_invocations_is_merge_registered(path):
    h = CM.merge_handler_for(path)
    assert h is not None, "unregistered -> backend freezes on both-diverged"
    assert h.__name__ == "merge_append_only_jsonl"


@pytest.mark.parametrize("path", [
    "agents/zeta/health/2026-08-02.jsonl",
    "agents/alpha/health/2025-01-01.jsonl",       # any past date
    "agents/foxtrot/health/2099-12-31.jsonl",     # any future date
])
def test_health_daily_file_is_merge_registered(path):
    """The health ledger is daily-ROTATED, so its basename is a date and it can
    never live in the basename-keyed _HANDLERS table. It is matched by parent
    dir instead, which is why arbitrary dates must resolve."""
    h = CM.merge_handler_for(path)
    assert h is not None, "date basenames cannot be enumerated in _HANDLERS"
    assert h.__name__ == "merge_append_only_jsonl"


@pytest.mark.parametrize("path,why", [
    ("agents/zeta/health/notes.md", "non-jsonl under health/ is not a ledger"),
    ("some/other/health.jsonl", "a FILE named health is not the health DIR"),
    ("agents/zeta/experience.jsonl", "unrelated agent store stays unregistered"),
])
def test_negative_controls_stay_unregistered(path, why):
    """A path-pattern branch is easy to write too broadly; these pin the edges."""
    assert CM.merge_handler_for(path) is None, why


def test_existing_registrations_not_disturbed():
    """Regression: the two pre-existing path-pattern branches still win."""
    assert CM.merge_handler_for(
        "world/team-state/agents/zeta.yaml").__name__ == "merge_team_state_shard"
    assert CM.merge_handler_for("core/config/skill-gaps.yaml") is None


# ── the corruption this actually cures ──────────────────────────────────────

def test_union_recovers_both_sides_of_a_divergence():
    """THE measured failure, in miniature.

    Two boxes share a baseline record, then each appends its own. Last-writer-
    wins keeps ONE side and silently drops the other — that is the 595-record
    loss. The union keeps the baseline once and both new records.
    """
    base = _rec(1)
    local = _dump([base, _rec(2, "reflect")])
    remote = _dump([base, _rec(3, "aspirations")])

    merged = _lines(CM.merge_append_only_jsonl(local, remote))

    assert len(merged) == 3, "baseline must collapse to one, both appends kept"
    skills = sorted(r["skill"] for r in merged)
    assert skills == ["aspirations", "reflect", "tree"]
    # Neither side may be the loser — the whole point.
    assert _rec(2, "reflect") in merged
    assert _rec(3, "aspirations") in merged


def test_merge_is_commutative_byte_for_byte():
    """guard-907: a non-commutative handler makes two machines compute DIFFERENT
    merged bytes, so the fenced PUT loop ping-pongs forever instead of
    converging — reintroducing the deadlock the merge exists to cure."""
    a = _dump([_rec(1), _rec(2, "reflect")])
    b = _dump([_rec(1), _rec(3, "aspirations")])
    assert CM.merge_append_only_jsonl(a, b) == CM.merge_append_only_jsonl(b, a)


def test_merge_is_idempotent():
    """Re-merging a merged result must not grow it, or a retry loop inflates."""
    a = _dump([_rec(1), _rec(2, "reflect")])
    b = _dump([_rec(1), _rec(3, "aspirations")])
    once = CM.merge_append_only_jsonl(a, b)
    assert CM.merge_append_only_jsonl(once, once) == once
    assert CM.merge_append_only_jsonl(once, a) == once


def test_health_records_sort_chronologically():
    """_log_ts must find the health record's stamp, or every record ties and the
    merged ledger loses the append order recent_records() walks."""
    r1 = {"ts": "2026-08-02T01:00:00", "agent": "zeta", "composite": 0.9}
    r2 = {"ts": "2026-08-02T02:00:00", "agent": "zeta", "composite": 0.8}
    merged = _lines(CM.merge_append_only_jsonl(_dump([r2]), _dump([r1])))
    assert [r["ts"] for r in merged] == [r1["ts"], r2["ts"]]


# ── snapshot blacklist ──────────────────────────────────────────────────────

def test_both_stores_are_snapshot_blacklisted():
    """Without this, routing the writers through locked_append_jsonl snapshots a
    multi-thousand-line file on EVERY skill fire — O(N^2), the exact reason
    meta/gate-firings.jsonl is already blacklisted. A fix that ships without
    this entry is a regression wearing a fix's clothes.

    Uses a REAL agent dir rather than tmp_path: _classify_base resolves against
    agents_root(), so a tmp dir classifies as "unknown" and every assertion
    below would pass vacuously against an empty pattern tuple. Read-only — this
    only classifies a path, it never writes.
    """
    import _paths
    agent = _paths.agent_dir("zeta")
    assert F._classify_base(agent) == "agent", "fixture must reach the agent arm"
    assert F._is_snapshot_blacklisted(agent, "skill-invocations.jsonl")
    assert F._is_snapshot_blacklisted(agent, "health/2026-08-02.jsonl")
    assert F._is_snapshot_blacklisted(agent, "health/2099-01-01.jsonl")
    # Negative controls — the blacklist must not swallow real agent state.
    assert not F._is_snapshot_blacklisted(agent, "self.md")
    assert not F._is_snapshot_blacklisted(agent, "experience.jsonl")


# ── writers ────────────────────────────────────────────────────────────────

WRITERS = [
    ("user-prompt-skill-record.sh", "skill-invocations.jsonl"),
    ("context-reads-skill-gate.sh", "skill-invocations.jsonl"),
    ("health-ledger-append.py", None),
]

#  SPLIT THE CONTRACT. This list was uniformly pinned to
# locked_append_jsonl. The two .sh entries are PreToolUse/UserPromptExpansion
# hooks carrying the IRREDUCIBLY LOCAL banner, and routing their per-fire append
# through the backend is a force-fresh GET + full-file PUT: measured on cc-04
# against a size-matched 486KB ledger, 700ms median vs 0.09ms bare (~7,435x), on
# a path that fires on every user prompt.
#
# That hop was never the cure, and this module's own docstring is the evidence.
# It states the measured corruption was whole-file DIVERGENCE (1303 -> 708
# lines), that a concurrency test here would be VACUOUS because a small
# O_APPEND write is already atomic on Linux, and that "the divergence-union
# tests below are the ones that carry the goal's value". Those tests pin the
# MERGE REGISTRATION, which -e also shipped and which  left
# fully intact: owncloud_sync._try_merge_put takes the LOCAL bytes and pushes
# the union, so a bare local append still reaches S3 with "no side's records
# ever dropped". Registration buys durability; the backend hop bought only
# immediacy, and telemetry does not need immediacy at 700ms per prompt.
#
# health-ledger-append.py is NOT a hook and carries no such banner, so it keeps
# the original contract. Only the banner-carrying hooks flip.
BACKEND_ROUTED_WRITERS = [w for w in WRITERS if w[0].endswith(".py")]
LOCAL_HOOK_WRITERS = [w[0] for w in WRITERS if w[0].endswith(".sh")]


@pytest.mark.parametrize("script,_store", BACKEND_ROUTED_WRITERS)
def test_writer_no_longer_bare_appends(script, _store):
    """Regression pin: this writer wrote via open(..., "a"), which takes no lock
    and never reaches the storage backend. Re-introducing that form is the
    defect, so it is matched textually.

    Scoped to the non-hook writers by g-115-4675 — the two hooks are pinned to
    the opposite contract in test_skill_invocation_hooks.py, for the reason
    given above this function."""
    src = (SCRIPTS / script).read_text(encoding="utf-8")
    # Strip comment lines FIRST (guard-958: token-anchor a predicate so quoted
    # prose cannot trip it). Every one of these writers now carries a comment
    # NAMING the old bare-append form to explain why it was replaced, and the
    # first version of this test matched those comments and failed on the very
    # files it had just certified. A scanner that cannot tell code from prose
    # about code reports the fix as the defect.
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    bare = re.findall(r"open\([^)]*,\s*['\"]a['\"]", code)
    assert not bare, f"{script} still bare-appends: {bare}"
    assert "locked_append_jsonl" in code, f"{script} must use the locked helper"


@pytest.mark.parametrize("script", LOCAL_HOOK_WRITERS)
def test_hook_writers_stay_fail_open(script):
    """guard-141: these are hooks. The telemetry append must sit INSIDE a try, so
    a failure degrades to a silent no-op rather than an error, and the script
    must still exit 0 unconditionally.

    g-115-4675 RE-ANCHORED this. It used to locate the try block by slicing 400
    chars back from `import _fileops` — an anchor that vanished with the import,
    so the test did not fail with its own message, it raised ValueError from the
    slice. The PROPERTY is unchanged and still worth pinning; only the landmark
    moved.

    Deliberately a LINE-ADJACENCY check, not a character window. The first
    re-anchoring used `"try:" in src[idx-400:idx]` and did NOT discriminate:
    both hooks now carry a second try (the session-binding resolution) inside
    that window, so deleting the append's own guard still passed. A window wide
    enough to find the guard is wide enough to find an unrelated one."""
    src = (SCRIPTS / script).read_text(encoding="utf-8")
    lines = src.splitlines()
    hits = [n for n, ln in enumerate(lines) if "open(os.path.join(agent_dir" in ln]
    assert len(hits) == 1, f"expected exactly one telemetry append in {script}"
    prev = next(lines[n] for n in range(hits[0] - 1, -1, -1) if lines[n].strip())
    assert prev.strip() == "try:", (
        f"the telemetry append in {script} must sit directly under `try:` "
        f"(found {prev.strip()!r})"
    )
    assert "2>/dev/null || true" in src, "fail-open mask removed"
    assert src.rstrip().endswith("exit 0"), "hook must exit 0 unconditionally"


def test_concurrent_appends_from_separate_processes_all_land(tmp_path):
    """Real concurrency through the production helper: N processes x M records.

    Deliberately NOT presented as the pre-fix-failing test — see the module
    docstring. On Linux a small O_APPEND write is already atomic, so bare append
    would also pass this. It pins that the LOCKED path does not REGRESS what the
    bare path got right (a lock held across read-modify-write is exactly where a
    naive implementation starts losing records).
    """
    target = tmp_path / "agents" / "zeta" / "skill-invocations.jsonl"
    target.parent.mkdir(parents=True)
    n_proc, n_rec = 4, 25

    prog = (
        "import sys, os\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "import _fileops\n"
        "w = sys.argv[1]; p = sys.argv[2]\n"
        f"for i in range({n_rec}):\n"
        "    _fileops.locked_append_jsonl(p, {'w': w, 'i': i})\n"
    )
    env = dict(os.environ)
    env["STORAGE_BACKEND"] = "local"          # guard-955
    env.pop("MIND_AGENT", None)
    env["MIND_WORLD"] = str(tmp_path / "world")
    env["MIND_META"] = str(tmp_path / "meta")

    procs = [subprocess.Popen([sys.executable, "-c", prog, str(w), str(target)],
                              env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
             for w in range(n_proc)]
    for p in procs:
        out, err = p.communicate(timeout=300)
        assert p.returncode == 0, err.decode()[:800]

    raw = target.read_text(encoding="utf-8").splitlines()
    recs = [json.loads(x) for x in raw if x.strip()]
    assert len(recs) == n_proc * n_rec, (
        f"lost records: {len(recs)} of {n_proc * n_rec}")
    # No torn lines: every line parsed above, and every (w,i) pair is present.
    # w arrives via argv, so it is a STRING — compare in the same domain rather
    # than loosening the assertion to a count (a count alone would pass even if
    # one worker wrote every record twice and another wrote none).
    assert {(r["w"], r["i"]) for r in recs} == {
        (str(w), i) for w in range(n_proc) for i in range(n_rec)}
