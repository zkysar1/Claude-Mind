""": the pre-apply consult gate must fire on OWN-AUTHORED framework goals.

THE BUG THIS PINS
-----------------
`pre-apply-consult-gate.py` (g-115-826) enforces code-review-protocol.md step 4: run a
retrieve.sh consult BEFORE editing a framework file, so you do not re-derive — or
actively contradict — a lesson you already wrote down.

It shipped with this early return:

    handoff_from = goal.get("handoff_from")
    if not handoff_from or handoff_from == agent:
        return 0            # own-authored -> gate does not fire

That scoped it to its originating incident (g-115-796: an agent applying ANOTHER
agent's spec) and left the COMMON case completely uncovered: an agent skipping the
consult on its OWN framework goals.

MEASURED (zeta, 2026-07-14): four consecutive deep framework goals closed with
`retrieval-summary: performed=false` — g-115-2194, g-115-2195, g-115-2179, g-115-2202.
All four had handoff_from=None, so the gate was SILENT on every one. The 4/4 miss rate
was never evidence that "an advisory doesn't work" — THE ADVISORY NEVER RAN.

Cost of the gap, measured: guard-1077 was written at 17:25 that day, and the exact
incident it describes was then re-derived from scratch by an hour of git archaeology at
20:42; a duplicate guardrail (guard-1089) was created and had to be retired, because
the Phase-6.5 anti-duplication check ALSO depends on retrieving first. A guardrail only
works if it is RETRIEVED.

MUTATION-VERIFIED: restore the `if not handoff_from: return 0` early-return and
`test_own_authored_framework_goal_fires` FAILS. A regression test that still passes
when you delete what it guards is not a test (the g-115-2195 lesson, learned the hard
way when a test passed 9/9 with the entire pathspec entry it guarded deleted).

THE OVER-FIX IS ALSO PINNED
---------------------------
Widening a trigger on a shared path is exactly where you create a tax (guard-1080:
before ADDING a refusal to a shared path, enumerate who legitimately performs the
behavior). Two tests exist solely to stop this gate from becoming noise:
  * a non-framework goal must stay SILENT — otherwise every close pays for it
  * a goal whose consult ALREADY ran must stay SILENT — a banner that fires even when
    satisfied is one the agent learns to ignore, which is the same habituation that let
    8 red tests be waved through for days (guard-1090). A gate must be silent when
    satisfied or it stops being a signal.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
GATE = SCRIPTS / "pre-apply-consult-gate.py"


def _load():
    spec = importlib.util.spec_from_file_location("_paconsult_gate", GATE)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SCRIPTS))
    spec.loader.exec_module(mod)
    return mod


QUEUE = [
    # own-authored, framework path in the title -> MUST fire (the regression)
    {"id": "g-1", "title": "Fix retry logic in core/scripts/foo.sh",
     "description": "surgical", "category": "infra"},
    # own-authored, NO framework reference -> must stay SILENT (over-fix guard)
    {"id": "g-2", "title": "Research market pricing for widgets",
     "description": "interview customers", "category": "market-research"},
    # own-authored, bare filenames + framework CATEGORY -> MUST fire via category.
    #  was written exactly this way and slipped through the first widening.
    {"id": "g-3", "title": "Maintain: fix bare_filename.py counter",
     "description": "names files bare, no path prefix",
     "category": "framework-maintenance"},
    # INHERITED framework spec -> must still fire (do not regress the original case)
    {"id": "g-4", "title": "Apply: patch core/config/foo.yaml",
     "description": "inherited spec", "category": "infra",
     "handoff_from": "alpha"},
    # deep PRODUCT work -> must stay SILENT
    {"id": "g-5", "title": "Ship the game server login flow",
     "description": "no framework files at all", "category": "product"},
    # : own-authored, NON-framework category, names a real framework file
    # BARE with no path prefix anywhere -> MUST fire via on-disk resolution. Neither
    # the needle scan (no path) nor the category trigger (category is "infra") sees
    # this shape; measured, it was 19.9% of goals that actually edited framework code.
    {"id": "g-6", "title": "Idea: teach pre-apply-consult-gate.py to resolve bare names",
     "description": "no path prefix appears anywhere in this goal", "category": "infra"},
    # names a plausible-looking script that does NOT exist -> must stay SILENT. This is
    # the discriminator between the APPLIED fix (resolve against disk, FP 42.3%) and the
    # REJECTED any-bare-mention form (FP 54.9%, +313 banners for 21 more misses closed).
    {"id": "g-7", "title": "Idea: rewrite definitely-not-a-real-script-zzz.sh",
     "description": "product tooling, not framework", "category": "product"},
]


@pytest.fixture()
def queue_file(tmp_path):
    p = tmp_path / "q.jsonl"
    # ONE json object per LINE — the gate's _find_goal parses line-by-line. A
    # pretty-printed fixture silently matches NOTHING and makes every assertion below
    # pass for the wrong reason (this exact fixture bug bit during development).
    p.write_text(json.dumps({"id": "asp-999", "goals": QUEUE}) + "\n", encoding="utf-8")
    return p


def _env(agent="zeta", **extra) -> dict:
    """Ambient env with MIND_AGENT pinned — NOT a hand-built POSIX PATH ().

    These sites used {"MIND_AGENT": agent, "PATH": "/usr/bin:/bin"}. sys.executable
    is absolute so Python itself starts, but the gate shells out, and on Windows a
    POSIX-only PATH resolves none of it — so the gate silently did not fire and the
    load-bearing assertions read as "the gate is broken". Proven by controlled
    comparison, same goal and queue file, ONLY env differing:
        POSIX-only PATH {"PATH": "/usr/bin:/bin"} -> rc=0, fired=False
        ambient env + MIND_AGENT                 -> rc=0, fired=True
    The gate was correct throughout; the harness was not.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("MIND_")}
    env["MIND_AGENT"] = agent
    env.update(extra)
    return env


def _fires(queue_file, goal_id, agent="zeta", env_extra=None) -> bool:
    env = _env(agent, **(env_extra or {}))
    r = subprocess.run(
        [sys.executable, str(GATE), goal_id, "--queue-file", str(queue_file)],
        capture_output=True, text=True, timeout=30, env=env,
        encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, "the gate is advisory — it must ALWAYS exit 0"
    return "PRE-APPLY CONSULT GATE" in (r.stdout or "")


def test_fixture_is_not_vacuous(queue_file):
    """POSITIVE CONTROL ON THE PROBE. If the fixture does not parse, every goal reads
    as 'not found' and the gate is silent for ALL of them — so the 'must stay silent'
    assertions below would pass for the wrong reason while testing nothing."""
    line = queue_file.read_text(encoding="utf-8").strip()
    assert len(line.splitlines()) == 1, "queue must be real JSONL — one object per line"
    assert len(json.loads(line)["goals"]) == len(QUEUE)


def test_own_authored_framework_goal_fires(queue_file):
    """THE LOAD-BEARING ASSERTION. Mutation check: restore the `if not handoff_from:
    return 0` early-return and this fails."""
    assert _fires(queue_file, "g-1"), (
        "an OWN-AUTHORED goal touching core/scripts/ must fire the consult gate. "
        "This is the 4/4-miss case (g-115-2194/2195/2179/2202) the gate was blind to."
    )


def test_framework_category_catches_bare_filenames(queue_file):
    """The needle scan reads goal PROSE and misses files named without a path prefix.
    `category` is a structured field, so it holds regardless of phrasing."""
    assert _fires(queue_file, "g-3"), (
        "a framework-maintenance goal must fire even when its description names files "
        "bare (the g-115-2202 shape)"
    )


def test_inherited_spec_still_fires(queue_file):
    """Do not regress the ORIGINAL case ( / rb-987). handoff_from became an
    escalator, not a gate — it must not have become a no-op."""
    assert _fires(queue_file, "g-4")


def test_bare_framework_filename_fires(queue_file):
    """. A goal naming a real framework file BARE — no path prefix, no
    framework category — must fire.

    MEASURED, not assumed: scored over 1624 completed goals against git ground truth
    (did the goal's commits actually touch a framework file?), the needle+category
    predicate MISSED 103 of 518 framework-editing goals — 19.9%. Every miss had this
    exact shape: "Idea: deploy-verify.sh should classify...", "Investigate:
    fixture-leak-scan.py should exclude...". The category trigger was ADDED to cover
    bare names (g-115-2201) and measurably did not, because these goals do not sit in
    a framework-* category.

    The fixture names THIS GATE'S OWN FILE deliberately: it is the one framework
    filename guaranteed to exist on disk whenever this test runs, so the positive case
    cannot rot into a silent skip.

    MUTATION CHECK: delete the `_detect_bare_framework_files` loop from main() and this
    fails while every other test in the file still passes.
    """
    assert _fires(queue_file, "g-6"), (
        "a goal naming a real framework file bare must fire the consult gate — this is "
        "the 19.9%-miss shape (g-115-4358)"
    )


def test_unresolvable_bare_filename_stays_silent(queue_file):
    """THE DISCRIMINATOR between the applied fix and the rejected one.

    Firing on ANY bare .sh/.py/.yaml mention was measured and REJECTED: it closes 21
    more misses but adds 313 banners (FP 39.5% -> 54.9%), which is the banner-fatigue
    that makes a gate ignorable (guard-1090). Resolving the name against disk is what
    keeps the widening honest — a name only counts when a real framework file answers
    to it.
    """
    assert not _fires(queue_file, "g-7"), (
        "a bare filename that resolves to NO framework file must not fire — that is "
        "candidate C, measured at 54.9% false positives and rejected"
    )


def test_bare_name_resolution_is_on_disk_not_prose_shape():
    """Unit-level statement of the same discriminator, independent of the queue fixture."""
    mod = _load()
    assert mod._detect_bare_framework_files(
        "fix pre-apply-consult-gate.py", "") == ["pre-apply-consult-gate.py"]
    assert mod._detect_bare_framework_files(
        "fix definitely-not-a-real-script-zzz.sh", "") == []


def test_denylisted_generic_name_is_not_evidence():
    """A deny-listed name that WOULD otherwise resolve must not count as evidence.

    A deny entry is only testable when it is BOTH regex-reachable AND present on
    disk. The first version of this test checked NEITHER: it keyed on `__init__.py`,
    which BARE_FILENAME_RE can never emit (the pattern opens `[a-z0-9]`, so a
    leading underscore fails to match), and it skip-guarded on that file merely
    EXISTING — which it does, in two search dirs. So the test RAN and PASSED while
    proving nothing about the deny mechanism, because the name never reached it.
    Found by fresh-eyes on this goal's own diff; same shape as the g-115-4471
    receipt blind spot — covering the noun in the test's name while missing the
    path it claims to exercise.

    Measured 2026-08-01: NO entry satisfies both preconditions (`__init__.py` and
    `readme.md` are regex-unreachable; `conftest.py` and `utils.py` are absent from
    every search dir), so BARE_FILENAME_DENY is currently INERT and this skips
    loudly. It becomes a real assertion the moment any deny entry is both reachable
    and present — which is the point of deriving the fixture from the constants
    instead of hardcoding a name.
    """
    mod = _load()
    testable = [n for n in mod.BARE_FILENAME_DENY
                if mod.BARE_FILENAME_RE.fullmatch(n)
                and any((d / n).is_file() for d in mod._bare_search_dirs())]
    if not testable:
        pytest.skip("no deny entry is both regex-reachable and present on disk — "
                    "the deny list is inert here, so asserting on it would be a "
                    "tautology (see docstring)")
    for name in testable:
        assert mod._detect_bare_framework_files(f"touch {name}", "") == [], (
            f"{name} is deny-listed and must not count as a framework reference")


@pytest.mark.parametrize("goal_id,why", [
    ("g-2", "market-research goal with no framework reference"),
    ("g-5", "deep PRODUCT work with no framework files"),
    ("g-7", "product goal naming a script that does not exist on disk"),
])
def test_non_framework_goal_stays_silent(queue_file, goal_id, why):
    """THE OVER-FIX GUARD. Widening the trigger must not tax every close (guard-1080)."""
    assert not _fires(queue_file, goal_id), (
        f"gate must stay SILENT for {why} — a gate that fires on everything is one the "
        f"agent learns to ignore (guard-1090)"
    )


def test_suppressed_when_consult_already_done(tmp_path):
    """A gate must be SILENT WHEN SATISFIED or it stops being a signal. Reads the same
    retrieval-session.json the learning gate audits, so the gate that ASKS for the
    consult and the audit that MEASURES it share one source of truth.

    THE FIXTURES BELOW ARE THE SHAPES THE PRODUCTION WRITERS ACTUALLY EMIT. Verified by
    running `retrieve.sh --goal` live and reading the file back. This matters: the
    obvious check — `bool(d["retrieval_performed"])` — REJECTS every real consultation,
    because the real retrieve.sh path leaves that field ABSENT and only the stub writes
    it as False. My first version of this test asserted `retrieval_performed: True`, a
    shape retrieve.sh NEVER produces, so it passed while the code was wrong. That is
    rb-3449 (a test double diverging from the real writer makes the suite structurally
    blind) reappearing in the very test written to pin this gate. Pin the REAL shape.
    """
    mod = _load()
    agent_dir = tmp_path / "agents" / "zeta"
    (agent_dir / "session").mkdir(parents=True)
    rs = agent_dir / "session" / "retrieval-session.json"
    mod._agent_dir = lambda _a: agent_dir  # noqa: SLF001

    # REAL shape from `retrieve.sh --goal g-X`: goal_id + counts recorded,
    # `retrieval_performed` ABSENT. This is a genuine consultation and MUST suppress.
    rs.write_text(json.dumps({
        "goal_id": "g-X",
        "counts": {"reasoning_bank": 10, "guardrails": 14, "tree_nodes": 15},
    }))
    assert mod._consult_already_done("zeta", "g-X") is True, (
        "a REAL consult (goal_id recorded, retrieval_performed absent) must suppress. "
        "Requiring bool(retrieval_performed) rejects every real consultation."
    )
    assert mod._consult_already_done("zeta", "g-Y") is False, "different goal -> fire"

    # REAL shape of iteration-close.sh's no-retrieval STUB: retrieval_performed is
    # explicitly False, counts all zero. It must NOT count as a consult — otherwise a
    # MISS would suppress the very banner meant to prevent the next miss.
    rs.write_text(json.dumps({
        "goal_id": "g-X",
        "retrieval_performed": False,
        "counts": {"reasoning_bank": 0, "guardrails": 0, "tree_nodes": 0},
    }))
    assert mod._consult_already_done("zeta", "g-X") is False, "stub is not a consult"


def test_banner_recommends_goal_scoped_retrieval(queue_file):
    """The gate must not recommend a command that cannot satisfy the audit measuring it.

    Without `--goal`, retrieve.sh performs the retrieval but never records it against
    the goal, so iteration-close.sh:1398 (which credits on goal_id match) writes a
    `performed=false` stub — the consult you ACTUALLY DID is logged as a MISS, this
    gate's suppression never fires, and any measurement of the miss-rate counts every
    consult as a failure. The recommendation and the audit must agree.
    """
    env = _env("zeta")
    r = subprocess.run(
        [sys.executable, str(GATE), "g-1", "--queue-file", str(queue_file)],
        capture_output=True, text=True, timeout=30, env=env,
        encoding="utf-8", errors="replace",
    )
    assert "--goal g-1" in r.stdout, (
        "the recommended retrieve.sh invocation MUST carry --goal <goal-id>, or the "
        "consultation it asks for cannot be credited"
    )
