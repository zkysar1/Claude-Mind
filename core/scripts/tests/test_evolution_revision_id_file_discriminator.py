"""evolution-git-sweep revision_id — file discriminator ().

WHY THIS EXISTS, in one sentence: without a file segment in the id, two files
of the same KIND touched by ONE commit get a byte-identical revision_id, and
`coordination_merge.merge_evolution_stream` keys records by revision_id — so a
cross-box merge collapses the family to a single row and destroys the rest,
silently, because every surviving row is well-formed.

That is not hypothetical. Measured 2026-08-29: rule-evolution.jsonl fell
634,133 -> 489,612 bytes between the 2026-08-20 and 2026-08-24 history
snapshots, and the collision the originating goal named by hand,
rule-20260510T015221-framework-2c51, went from FOUR rows
(code-review-protocol / stop-hook-compliance / user-interaction /
path-resolution) down to ONE.

TWO-WAY PROOF (guard-1220). test_two_files_one_commit_get_distinct_ids is RED
against the pre-fix implementation — confirmed by loading the pre-fix copy and
observing the two ids compare equal. A test that only passes on the new code
proves nothing about whether it would have caught the bug.

The DETERMINISM tests are not padding: the sweep's idempotency (module docstring
L33, "deterministic for idempotency") is what makes a re-run skip instead of
re-appending the entire backfill, so any fix to this id MUST keep the same
(commit, file) mapping to the same id forever. A randomized discriminator would
satisfy the uniqueness test above and destroy the store.

Run: STORAGE_BACKEND=local py -3 -m pytest \
       core/scripts/tests/test_evolution_revision_id_file_discriminator.py -q
"""
import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))


def load_mod():
    spec = importlib.util.spec_from_file_location(
        "evo_git_sweep_revid", SCRIPT_DIR / "evolution-git-sweep.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


TS = "2026-05-10T01:52:21-04:00"
SHA = "2c51079a9669e351d4689a4e553513d1252c1a71"
AGENT = "framework"
KIND = "rule_edit"

# The four real paths from the collision family the originating goal named.
FAM = [".claude/rules/code-review-protocol.md",
       ".claude/rules/stop-hook-compliance.md",
       ".claude/rules/user-interaction.md",
       ".claude/rules/path-resolution.md"]


# ---------------------------------------------------------------- the defect

def test_two_files_one_commit_get_distinct_ids():
    """THE regression test. RED against the pre-fix implementation."""
    m = load_mod()
    ids = [m.make_revision_id(KIND, TS, AGENT, SHA, p) for p in FAM]
    assert len(set(ids)) == len(FAM), (
        f"{len(FAM)} files in one commit produced {len(set(ids))} distinct "
        f"id(s) — a union-by-id merge would destroy "
        f"{len(FAM) - len(set(ids))} row(s): {ids}")


def test_only_the_file_segment_differs():
    """Anti-vacuity: prove the ids differ BECAUSE of the file segment, and
    that nothing else about them moved. Uniqueness alone would also be
    satisfied by an id that changed the timestamp or dropped the sha."""
    m = load_mod()
    ids = [m.make_revision_id(KIND, TS, AGENT, SHA, p) for p in FAM]
    heads = {i.rsplit("-", 1)[0] for i in ids}
    tails = {i.rsplit("-", 1)[1] for i in ids}
    assert len(heads) == 1, f"the non-file portion moved too: {heads}"
    assert len(tails) == len(FAM), f"file segments collided: {tails}"


# ------------------------------------------------- idempotency's foundation

def test_same_commit_and_file_is_stable_across_calls():
    """Determinism is what makes a re-run skip rather than re-append."""
    m = load_mod()
    first = m.make_revision_id(KIND, TS, AGENT, SHA, FAM[0])
    for _ in range(5):
        assert m.make_revision_id(KIND, TS, AGENT, SHA, FAM[0]) == first


def test_stable_across_a_fresh_module_load():
    """Guards against a discriminator seeded per-process (e.g. hash() with
    PYTHONHASHSEED, or a module-level random). Same input, new interpreter
    state for the module, same id."""
    a = load_mod().make_revision_id(KIND, TS, AGENT, SHA, FAM[0])
    b = load_mod().make_revision_id(KIND, TS, AGENT, SHA, FAM[0])
    assert a == b


# ------------------------------------------------------ shape / compat pins

def test_sha_traceability_is_retained():
    """The commit sha prefix must survive: it is how a reader gets from an id
    back to the commit that produced it."""
    m = load_mod()
    rid = m.make_revision_id(KIND, TS, AGENT, SHA, FAM[0])
    assert f"-{SHA[:4]}-" in rid, rid


def test_prefix_timestamp_and_agent_are_unchanged():
    m = load_mod()
    rid = m.make_revision_id(KIND, TS, AGENT, SHA, FAM[0])
    assert rid.startswith(f"rule-20260510T015221-{AGENT}-{SHA[:4]}-"), rid


def test_omitting_file_path_yields_the_pre_fix_id():
    """Backward compatibility for any caller with no path in scope."""
    m = load_mod()
    assert (m.make_revision_id(KIND, TS, AGENT, SHA)
            == f"rule-20260510T015221-{AGENT}-{SHA[:4]}")


def test_distinct_commits_still_differ():
    """The fix must not collapse the axes it did not touch."""
    m = load_mod()
    other = "ff022aedf46f32afc981fa1e19a5adb235615ad7"
    assert (m.make_revision_id(KIND, TS, AGENT, SHA, FAM[0])
            != m.make_revision_id(KIND, TS, AGENT, other, FAM[0]))
