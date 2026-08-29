#!/usr/bin/env python3
"""Pins for the reflection-queue fixture tell ().

`pipeline-read.sh --unreflected` is a WORK QUEUE whose prescribed action is a
full ABC chain, and it could not tell a TEST FIXTURE from a finding: both are
resolved records carrying an outcome and a surprise score. Following a fixture
literally MANUFACTURES learning -- fabricated ABC chains, belief updates and
pattern signatures from a claim that was never a prediction -- and the artifacts
are indistinguishable from real ones afterward. Caught by eye in 2026-07; nothing
in the pipeline would have.

THE POSITIVE CONTROL IS THE REAL g-115-3801 POPULATION, transcribed from the
live store (alpha, cc-07, 2026-08-28): five records sharing the identical title
"Test hypothesis for surprise derivation on write", all category test-cat, with
outcomes CONFIRMED / CORRECTED / CONFIRMED / None / CONFIRMED. One claim cannot
resolve three ways -- that is a test matrix, and no real prediction produces it.
FOUR OF THE FIVE ARE REFLECTABLE (outcome in CONFIRMED/CORRECTED), so the
existing `is_reflectable` split does NOT already exclude fixtures; that is why
this tell has to exist rather than riding on the reflectable filter.

THE NEGATIVE CONTROL IS THE HARD HALF (guard-1665). Genuine hypotheses ABOUT
testing -- categories test-coverage / test-quality / framework-test -- must not
be flagged. `2026-05-05_test-fixture-shape-mismatch-class` is the sharpest case:
a real hypothesis whose title begins "Test fixtures structurally mismatched..."
It is what killed an earlier candidate predicate.

A PREDICATE THAT LOOKED PERFECT AND WAS NOT, pinned so it is not re-derived: a
"skeletal" conjunct (no rationale AND no evidence AND no outcome_detail) scored
5/5 with zero false positives when tuned on the UNREFLECTED QUEUE and 1/5 on the
full corpus -- 4 of the 5 g-115-3801 fixtures carry outcome_detail naming the
derivation test, and only census-d (the one still in the queue) is skeletal. The
queue is a BIASED SAMPLE of the corpus because the other four are reflected:true.
test_skeletal_conjunct_would_have_missed_four_of_five pins that measurement.
"""
import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS = _TESTS_DIR.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _reflectable  # noqa: E402


# --- the real  population, transcribed from the live store ---------
FIXTURES = [
    {"id": "2026-07-29_census-a", "category": "test-cat", "outcome": "CONFIRMED",
     "title": "Test hypothesis for surprise derivation on write",
     "outcome_detail": "g-115-3801 derivation test"},
    {"id": "2026-07-29_census-b", "category": "test-cat", "outcome": "CORRECTED",
     "title": "Test hypothesis for surprise derivation on write",
     "outcome_detail": "g-115-3801 derivation test"},
    {"id": "2026-07-29_census-c", "category": "test-cat", "outcome": "CONFIRMED",
     "title": "Test hypothesis for surprise derivation on write",
     "outcome_detail": "g-115-3801 derivation test"},
    {"id": "2026-07-29_census-d", "category": "test-cat", "outcome": None,
     "title": "Test hypothesis for surprise derivation on write"},
    {"id": "2026-07-29_surprise-derived", "category": "test-cat", "outcome": "CONFIRMED",
     "title": "Test hypothesis for surprise derivation on write",
     "outcome_detail": "g-115-3801 derivation test"},
]

# --- genuine hypotheses that must NOT be flagged (guard-1665) ----------------
GENUINE = [
    {"id": "2026-05-05_test-fixture-shape-mismatch-class", "category": "test-quality",
     "outcome": "CONFIRMED", "rationale": "measured on 5 newest Test*.java",
     "title": "Test fixtures structurally mismatched from production shape: at least 3 of 5"},
    {"id": "2026-05-12_counter-parity-tests-detect-future-regression",
     "category": "test-coverage", "outcome": "CONFIRMED", "rationale": "r",
     "outcome_detail": "d",
     "title": "Counter-parity structural source-pin tests will catch a real future regression"},
    {"id": "2026-07-31_verification-checklist-stale-check-density",
     "category": "test-coverage", "outcome": "CORRECTED", "rationale": "r",
     "outcome_detail": "d",
     "title": "A staleness scanner over verification-checklist.md will find 3+ stale checks"},
    {"id": "2026-04-03_bidirectional-flywheel", "category": "player-signal-framework",
     "outcome": "CONFIRMED", "rationale": "r",
     "title": "Engagement and NPC diversity reinforce each other"},
]


def test_every_known_fixture_is_flagged():
    """Positive control: all 5  records, by the goal's own check 1."""
    out = _reflectable.annotate_fixture_suspects([dict(r) for r in FIXTURES])
    for r in out:
        assert r["fixture_suspect"], f"{r['id']} not flagged"


def test_no_genuine_hypothesis_is_flagged():
    """Negative control (guard-1665) -- including a real hypothesis ABOUT fixtures."""
    out = _reflectable.annotate_fixture_suspects([dict(r) for r in GENUINE])
    for r in out:
        assert r["fixture_suspect"] == [], f"{r['id']} wrongly flagged: {r['fixture_suspect']}"


def test_mixed_corpus_separates_cleanly():
    """The discriminating case: both populations in ONE array, as the endpoint sees them."""
    out = _reflectable.annotate_fixture_suspects(
        [dict(r) for r in FIXTURES] + [dict(r) for r in GENUINE])
    flagged = {r["id"] for r in out if r["fixture_suspect"]}
    assert flagged == {r["id"] for r in FIXTURES}


def test_duplicate_title_signal_fires_on_the_test_matrix():
    """One claim resolving three ways is a test matrix; no real prediction does that."""
    out = _reflectable.annotate_fixture_suspects([dict(r) for r in FIXTURES])
    for r in out:
        assert "duplicate-title" in r["fixture_suspect"]


def test_key_is_always_present_even_when_clean():
    """Absence must mean 'old build', never 'clean' -- a conditional key cannot
    distinguish the two, and a consumer written against the wrong reading loses
    the guard silently."""
    out = _reflectable.annotate_fixture_suspects([dict(r) for r in GENUINE])
    for r in out:
        assert "fixture_suspect" in r


def test_it_flags_and_never_filters():
    """guard-1072: mark residue in place; never remove from a union-by-id store."""
    src = [dict(r) for r in FIXTURES] + [dict(r) for r in GENUINE]
    out = _reflectable.annotate_fixture_suspects(src)
    assert len(out) == len(FIXTURES) + len(GENUINE)


def test_a_lone_unique_title_is_not_flagged_by_the_duplicate_signal():
    """The duplicate signal must key on a real collision, not on any title."""
    r = dict(FIXTURES[0]); r["title"] = "A one-off title nothing else shares"
    r["category"] = "npc-intelligence"
    out = _reflectable.annotate_fixture_suspects([r])
    assert out[0]["fixture_suspect"] == []


def test_skeletal_conjunct_would_have_missed_four_of_five():
    """Pins the measurement that killed the first candidate predicate.

    Tuned on the unreflected QUEUE a skeletal conjunct scored 5/5; on the full
    corpus it scores 1/5, because 4 of the 5 fixtures carry outcome_detail and
    only census-d is skeletal. Tune a detector on the population its positive
    control lives in.
    """
    def skeletal(r):
        return not r.get("rationale") and not r.get("evidence") and not r.get("outcome_detail")
    assert sum(1 for r in FIXTURES if skeletal(r)) == 1
    assert sum(1 for r in FIXTURES if not skeletal(r)) == 4


def test_non_dict_rows_do_not_raise():
    out = _reflectable.annotate_fixture_suspects([None, 3, "x", dict(FIXTURES[0])])
    assert out[-1]["fixture_suspect"]


def test_endpoint_module_installs_core_scripts_on_syspath_in_a_fresh_process():
    """The tell is unreachable from the daemon if this bridge is not explicit.

    The endpoint's `import _reflectable` is a LAZY import inside the
    --unreflected branch, and `mind_api/src/world/pipeline.py` reaches
    core/scripts only because it imports `file_locks`, which does the sys.path
    insert at module load. That import was MISSING when the tell first landed:
    a fresh process importing only the endpoint module raised
    ModuleNotFoundError, and the code worked in the live daemon solely because
    some OTHER module had already imported agent_paths. The original comment
    asserted agent_paths as the source; pipeline.py never imports it.

    This must be a SUBPROCESS. The pytest conftest puts core/scripts on
    sys.path for every test, so an in-process assertion passes against the
    broken code and pins nothing -- the exact way this defect stayed invisible.
    Same class rb-3868 names on the sibling world/ modules ("explicit, NOT
    transitive through .team_state").
    """
    import os
    import subprocess
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)  # must not inherit a path that hides the bug
    env["STORAGE_BACKEND"] = "local"  # guard-955

    probe = (
        "import sys; sys.path.insert(0, '.');"
        "import importlib;"
        "importlib.import_module('mind_api.src.world.pipeline');"
        "import _reflectable;"
        "assert hasattr(_reflectable, 'annotate_fixture_suspects');"
        "print('BRIDGE-OK')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(repo_root), env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, (
        "importing the endpoint module alone did not make _reflectable "
        f"importable.\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "BRIDGE-OK" in proc.stdout
