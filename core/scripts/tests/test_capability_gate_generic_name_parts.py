"""test_capability_gate_generic_name_parts.py — regression test for .

A SOLE shared token qualifies as distinctive when it is a hyphen-component of
the entry's skill NAME (`_identifier_parts`, g-248-105). That premise — "the
name is what the author chose as its identity, so a prose keyword equal to a
name part is a deliberate reference" — holds for proper-noun/domain identifiers
(efs, roblox, vinheim, aws, npc) and FAILS for common English nouns that merely
happen to sit in a skill name.

Measured live 2026-07-29 via the canonical gate invocation, on defers naming no
capability at all:
  "the next session has not happened yet and the task which must run before it
   is not scheduled"        -> 'task'    -> add-npc-task                 would_block=True
  "a new runtime endpoint must appear before this can be verified"
                            -> 'runtime' -> scan-runtime-margin +
                                            ship-vinheim-runtime-endpoint  would_block=True
Both are Layer-D FP chains: the gate REFUSES the defer and auto-files a spurious
Unblock (the same chain that produced g-115-2329).

PROOF THE TRIGGER VOCABULARY IS NOT THE MECHANISM (this is the part the original
g-115-3934 report got wrong, and why this file exists separately from
test_capability_gate_table_token_noise.py): an entry built with triggers=[] AND
scripts=[] still qualifies 'task'. Multi-word trigger tokenization IS a real
mechanism, but it is the >=2-hit path, which bypasses `_single_token_qualifies`
by design ("Multi-token overlaps always survive"). That path is pinned by the
table-token-noise sibling; THIS file pins the sole-token name-parts path.

Fix (g-115-3934): `_GENERIC_NAME_PARTS` demotes common-noun name parts in
`_single_token_qualifies`. Same class the author already closed one step away —
`_identifier_parts` excludes companion SCRIPT names precisely because
"roblox-bridge.py would make 'bridge' an identifier part of access-roblox-studio,
reintroducing the exact observed FP". Only the script source had been demoted.

NOT frequency-based (measured and rejected): 'efs' is a name part of BOTH
access-efs-data and archive-efs-graveyard, so "shared by 2+ skills => not
distinctive" would kill the g-248-105 recall case this predicate protects. The
discriminator is common-noun-vs-proper-noun, not rarity — asserted below.

NOT _STOPWORDS (deliberate): the demoted tokens must stay in EXTRACTION so they
keep counting inside 2+-token overlaps — zero recall loss for genuine multi-token
references. Asserted below, and it is the load-bearing design property: if a
future change stopwords these instead, `test_demoted_token_still_extracted` goes
red.

guard-958 compliance: every addition to `_GENERIC_NAME_PARTS` DROPS matches, which
LOOSENS the gate (the g-115-792 anti-pattern). The adversarial single-surviving-
keyword recall controls are therefore mandatory and live here, adjacent to the
change — a multi-keyword happy path would MASK single-keyword recall loss.

Subprocess + fixture shape mirrors test_capability_gate_table_token_noise.py.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
GATE_PY = CORE_SCRIPTS / "capability-gate.py"

# The two measured FPs. Neither names a capability: no imperative verb aimed at a
# provisionable action, no compound identifier, no script name.
_TASK_FP = (
    "precondition_unmet: the next session has not happened yet and the task "
    "which must run before it is not scheduled"
)
_RUNTIME_FP = (
    "precondition_unmet: a new runtime endpoint must appear before this can be "
    "verified"
)
# Measured 2026-08-30. `land` reached the name-parts branch through the forged
# skill `land-stranded-pr`; here it is ordinary English ("before we land the
# unit-file change") about a systemd unit, not a pull request. Surfaced as the
# only red in the pytest-invisible half of a full-suite run
# (test_capability_gate_narrative.py case ).
_LAND_FP = (
    "user approves the systemd Restart=always vs Restart=no policy decision "
    "before we land the unit-file change"
)

# guard-958 adversarial recall controls — each is a SOLE-surviving-keyword case.
_SOLE_IDENTIFIER = "human_blocked: cannot access EFS"          # 'efs' ()
_SOLE_COMPOUND = "human_blocked: user must start the play-mode bridge session"
_SOLE_IMPERATIVE = "human_blocked: commit the hotfix"
# A demoted token inside a GENUINE reference: 'task' co-occurs with its skill's
# true discriminator ('npc'), so the >=2-hit path must still fire.
_DEMOTED_WITH_DISCRIMINATOR = (
    "human_blocked: user must add an npc task to the environment"
)
# The recall control the EXTENSION RULE demands for `land`: a GENUINE reference
# to land-stranded-pr. Measured hit counts are what make the demotion safe —
# the FP carries one shared token, this carries four (land, main, onto,
# stranded) and so never reaches _single_token_qualifies at all.
_LAND_WITH_DISCRIMINATOR = (
    "human_blocked: the PR went conflicting because main moved; someone must "
    "land the stranded PR onto main"
)


def _load_module():
    sys.path.insert(0, str(CORE_SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "cap_gng", CORE_SCRIPTS / "gates" / "capability.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_gate(failure_reason: str,
              intended_participants: str = "user") -> tuple[int, dict]:
    """Invoke capability-gate.py via subprocess. Returns (exit_code, parsed)."""
    cmd = [
        sys.executable, str(GATE_PY),
        "--failure-reason", failure_reason,
        "--intended-participants", intended_participants,
        "--output", "json",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = {"_raw_stdout": proc.stdout, "_raw_stderr": proc.stderr}
    return proc.returncode, payload


# --- the two measured false positives ---------------------------------------

def test_task_name_part_does_not_falsely_block():
    _, d = _run_gate(_TASK_FP)
    assert not d.get("would_block"), (
        f"generic name part 'task' wrongly blocked a capability-free defer; "
        f"matches={d.get('matches')} keywords={d.get('keywords_extracted')}"
    )


def test_runtime_name_part_does_not_falsely_block():
    _, d = _run_gate(_RUNTIME_FP)
    assert not d.get("would_block"), (
        f"generic name part 'runtime' wrongly blocked a capability-free defer; "
        f"matches={d.get('matches')} keywords={d.get('keywords_extracted')}"
    )


def test_land_name_part_does_not_falsely_block():
    """`land` is a common English verb that happens to open a forged skill's
    name. Left qualified, the gate refuses the defer AND auto-files an Unblock
    (Layer D) pointing at a PR-landing skill for a systemd unit-file change."""
    _, d = _run_gate(_LAND_FP)
    assert not d.get("would_block"), (
        f"generic name part 'land' wrongly blocked a capability-free defer; "
        f"matches={d.get('matches')} keywords={d.get('keywords_extracted')}"
    )


# --- guard-958 recall controls (sole-surviving-keyword, not happy paths) -----

def test_sole_identifier_name_part_still_matches():
    """The  recall case: 'efs' is the ONLY keyword and must still fire.
    This is what a frequency-based predicate would have broken."""
    _, d = _run_gate(_SOLE_IDENTIFIER)
    kws = {k for m in (d.get("matches") or [])
           for k in (m.get("all_matched_keywords") or [])}
    assert d.get("would_block"), (
        f"sole-identifier recall lost; keywords={d.get('keywords_extracted')}")
    assert "efs" in kws, f"'efs' no longer carries the match: {sorted(kws)}"


def test_sole_compound_still_matches():
    _, d = _run_gate(_SOLE_COMPOUND)
    assert d.get("would_block"), (
        f"sole-compound recall lost; keywords={d.get('keywords_extracted')}")


def test_sole_imperative_verb_still_matches():
    """_IMPERATIVE_VERBS qualifies via an EARLIER branch and must be untouched —
    dropping these re-opened the g-115-1883 recall regressions."""
    _, d = _run_gate(_SOLE_IMPERATIVE)
    assert d.get("would_block"), (
        f"sole-imperative recall lost; keywords={d.get('keywords_extracted')}")


def test_demoted_token_still_matches_with_its_discriminator():
    """The design property that makes demotion recall-safe: a demoted token still
    counts inside a >=2-token overlap, so a GENUINE reference still fires."""
    _, d = _run_gate(_DEMOTED_WITH_DISCRIMINATOR)
    kws = {k for m in (d.get("matches") or [])
           for k in (m.get("all_matched_keywords") or [])}
    assert d.get("would_block"), (
        f"genuine multi-token reference lost; "
        f"keywords={d.get('keywords_extracted')}")
    assert "task" in kws, (
        f"'task' was suppressed from MATCHING entirely (looks like it was "
        f"stopworded rather than demoted): {sorted(kws)}")


def test_land_still_matches_a_genuine_stranded_pr_reference():
    """guard-958 recall control for the `land` demotion, and the positive
    control the mutation proof must show NOT flipping: demoting a token may only
    remove the SOLE-token collision, never a real reference. A genuine
    stranded-PR defer shares four tokens with the skill, so it fires on the
    >=2-hit path that never consults _single_token_qualifies."""
    _, d = _run_gate(_LAND_WITH_DISCRIMINATOR)
    kws = {k for m in (d.get("matches") or [])
           for k in (m.get("all_matched_keywords") or [])}
    assert d.get("would_block"), (
        f"genuine land-stranded-pr reference lost; "
        f"keywords={d.get('keywords_extracted')} matches={d.get('matches')}")
    assert "land" in kws, (
        f"'land' was suppressed from MATCHING entirely (stopworded rather than "
        f"demoted): {sorted(kws)}")


def test_demoted_token_still_extracted():
    """Demotion must happen at QUALIFICATION, never at extraction. If a future
    change moves these tokens into _STOPWORDS, multi-token recall silently dies
    and this assertion is the tripwire."""
    _, d = _run_gate(_TASK_FP)
    kws = set(d.get("keywords_extracted") or [])
    assert "task" in kws, (
        f"'task' vanished from extraction — it must stay extractable so it can "
        f"contribute to multi-token overlaps: {sorted(kws)}"
    )


# --- structural pins --------------------------------------------------------

def test_generic_name_part_does_not_qualify_but_identifier_does():
    """Direct predicate test, and the proof that trigger vocabulary is not the
    mechanism: entries carry NO triggers and NO scripts."""
    m = _load_module()
    task_entry = {"source": "forged-skills.yaml", "skill": "add-npc-task",
                  "triggers": [], "scripts": []}
    efs_entry = {"source": "forged-skills.yaml", "skill": "access-efs-data",
                 "triggers": [], "scripts": []}
    # both ARE name parts — the difference is only common-noun vs identifier
    assert "task" in m._identifier_parts(task_entry)
    assert "efs" in m._identifier_parts(efs_entry)
    assert not m._single_token_qualifies("task", task_entry), (
        "'task' is a common noun and must not qualify as a sole distinctive token")
    assert m._single_token_qualifies("efs", efs_entry), (
        "'efs' is a domain identifier and must still qualify")


def test_demoted_set_excludes_imperative_verbs_and_identifiers():
    """Keeps the two lists from colliding: an _IMPERATIVE_VERB in
    _GENERIC_NAME_PARTS would be dead weight (earlier branch wins), and a real
    domain identifier in there would be a recall regression."""
    m = _load_module()
    overlap = m._GENERIC_NAME_PARTS & m._IMPERATIVE_VERBS
    assert not overlap, (
        f"_GENERIC_NAME_PARTS overlaps _IMPERATIVE_VERBS (unreachable "
        f"entries — the verb branch returns True first): {sorted(overlap)}")
    for ident in ("efs", "roblox", "aws", "npc", "vinheim"):
        assert ident not in m._GENERIC_NAME_PARTS, (
            f"domain identifier {ident!r} must never be demoted")


def test_demotion_is_load_bearing():
    """Mutation sensitivity, in-process: emptying _GENERIC_NAME_PARTS must make
    BOTH measured FPs match again, while the recall cases stay invariant."""
    m = _load_module()
    # Resolve the world dir exactly as the production entry point does. Do NOT
    # fall back to reading WORLD_PATH from the environment: an interactive shell
    # exports it and the pytest runner does not, so an env-based lookup makes
    # this test PASS by hand and SKIP in the suite — a silent vacuity (guard-1906,
    # and the guard-920 production-shape rule). `_resolve_world_dir` lives in
    # capability-gate.py (the CLI), not gates/capability.py (the library).
    spec = importlib.util.spec_from_file_location("cap_gate_cli", GATE_PY)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    world = cli._resolve_world_dir()
    assert world is not None and world.is_dir(), (
        f"world dir did not resolve via the production path: {world!r} — this "
        f"test must never silently skip, that is the vacuity it exists to avoid"
    )
    entries = (m._load_forged_skills(world)
               + m._load_skill_md_triggers(pathlib.Path(".claude/skills"))
               + m._load_capability_routing(world))
    extract = getattr(m, "_extract_keywords", None) or getattr(m, "_keywords")

    def matched(text):
        return bool(m._find_matches(extract(text), entries))

    assert not matched(_TASK_FP)
    assert not matched(_RUNTIME_FP)
    assert matched(_SOLE_IDENTIFIER)

    saved = m._GENERIC_NAME_PARTS
    try:
        m._GENERIC_NAME_PARTS = frozenset()
        assert matched(_TASK_FP), "reverting the fix did not reproduce the 'task' FP"
        assert matched(_RUNTIME_FP), "reverting the fix did not reproduce the 'runtime' FP"
        assert matched(_SOLE_IDENTIFIER), "recall case must be invariant to the fix"
    finally:
        m._GENERIC_NAME_PARTS = saved


# ---------------------------------------------------------------------------
#  (2026-08-31): the eight-token survey slice.
#
# The originating filing named three FPs — 'page', 'found' via build-landing-page
# / manage-website / document-found-opportunity. Re-measured on 2026-08-31 those
# tokens no longer fire, and NOT because the matcher was fixed: all three skills
# had been retired from forged-skills.yaml, so the collisions left with them. The
# defect class was fully intact. Anyone re-running the filing's stated repro today
# reads "no match" and wrongly concludes it is closed — which is why these cases
# are pinned against tokens that are still live.
#
# SURVEY (canonical evaluate() over the production corpus resolved by
# capability-gate.py's own _resolve_world_dir — 241 entries: 80 forged + 133
# SKILL.md + 28 routing): 138 of 196 name-only-qualifying tokens block a NEUTRAL
# prose carrier. So this branch's premise fails for ~70% of its own population.
# The eight below are the unambiguous common-noun slice.
#
# guard-2201 delta discipline: OLD and NEW predicates were run against ONE corpus
# snapshot in ONE process — neutral-carrier FPs 138 -> 130, REMOVED set == exactly
# these eight, ADDED set EMPTY.
_SURVEY_20260831 = {
    # token: (measured FP prose, target skill, genuine reference)
    "felt": (
        "precondition_unmet: the defect felt intermittent and has not shown up again",
        "felt-sense-checkin",
        "human_blocked: user must run the felt sense checkin"),
    "sense": (
        "precondition_unmet: the numbers make no sense until the next invoice arrives",
        "felt-sense-checkin",
        "human_blocked: user must run the felt sense checkin"),
    "eyes": (
        "precondition_unmet: more eyes on the rollout are wanted before the window opens",
        "fresh-eyes-tree",
        "human_blocked: user must review the tree with fresh eyes"),
    "body": (
        "precondition_unmet: the body of the incident writeup is not finished yet",
        "decode-email-body",
        "human_blocked: user must decode the email body"),
    "field": (
        "precondition_unmet: the field is empty on every row until the backfill completes",
        "recover-clobbered-store-field",
        "human_blocked: user must recover the clobbered store field"),
    "usage": (
        "precondition_unmet: usage has not crossed the threshold that would make this measurable",
        "sweep-customer-server-usage",
        "human_blocked: user must sweep the customer server usage"),
    "proof": (
        "precondition_unmet: no proof of the race exists until it happens again under load",
        "mutation-proof-regression-test",
        "human_blocked: user must write the mutation proof regression test"),
    "timeline": (
        "precondition_unmet: the timeline slipped and the dependency has not shipped",
        "reconstruct-env-server-restart-timeline",
        "human_blocked: user must reconstruct the env server restart timeline"),
}


def _production_world():
    """Resolve the world dir exactly as the production entry point does.

    Never fall back to os.environ["WORLD_PATH"]: an interactive shell exports it
    and the pytest runner does not, so an env lookup PASSES by hand and goes
    vacuous in the suite (guard-1906 / guard-920). Measured while writing these
    tests: passing world_dir=None silently drops forged-skills.yaml AND the
    capability-routing rows, cutting the corpus from 241 entries to 133 — a
    measurement of a subset wearing the costume of the whole.
    """
    spec = importlib.util.spec_from_file_location("cap_gate_cli_survey", GATE_PY)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    world = cli._resolve_world_dir()
    assert world is not None and world.is_dir(), (
        f"world dir did not resolve via the production path: {world!r}")
    return world


@pytest.mark.parametrize("token", sorted(_SURVEY_20260831))
def test_survey_token_does_not_falsely_block(token):
    """Each demoted token, in ordinary English prose naming no capability."""
    fp_text = _SURVEY_20260831[token][0]
    _, d = _run_gate(fp_text)
    assert not d.get("would_block"), (
        f"generic name part {token!r} wrongly blocked a capability-free defer; "
        f"matches={d.get('matches')} keywords={d.get('keywords_extracted')}"
    )


@pytest.mark.parametrize("token", sorted(_SURVEY_20260831))
def test_survey_token_recall_control(token):
    """guard-958: a GENUINE reference must still match, with the demoted token
    LOAD-BEARING on the right skill.

    Asserting only `would_block` would be the multi-keyword happy path this
    file's docstring warns MASKS recall loss — measured while writing this:
    "decode the email body" blocks via `email` on access-email, which says
    nothing about whether demoting `body` cost anything. So the assertion is
    that the target skill matched AND the demoted token is among ITS matched
    keywords, alongside >=1 other token (the >=2-hit path this predicate never
    reaches).
    """
    _, skill, genuine = _SURVEY_20260831[token]
    m = _load_module()
    world = _production_world()
    res = m.evaluate(genuine, intended_participants="user", world_dir=world,
                     skills_dir=pathlib.Path(".claude/skills"))
    hit = [x for x in (res.get("matches") or [])
           if x.get("skill") == skill and token in (x.get("all_matched_keywords") or [])]
    assert hit, (
        f"recall lost: genuine reference {genuine!r} no longer matches {skill!r} "
        f"with {token!r} load-bearing; matches="
        f"{[(x.get('skill'), x.get('all_matched_keywords')) for x in (res.get('matches') or [])[:5]]}"
    )
    assert len(hit[0]["all_matched_keywords"]) >= 2, (
        f"{token!r} is a SOLE-token match on {skill!r} — the demotion would kill "
        f"it; a genuine reference must carry >=2 tokens: {hit[0]['all_matched_keywords']}"
    )


def test_survey_demotion_is_load_bearing():
    """Mutation sensitivity for the  slice (guard-4166): emptying
    _GENERIC_NAME_PARTS must reproduce EVERY one of the eight FPs, while every
    recall control stays put. Naming the recall controls here is the half that
    makes the proof mean something — a mutation that flipped them too would show
    the same RED and prove nothing."""
    m = _load_module()
    world = _production_world()
    skills = pathlib.Path(".claude/skills")

    def blocks(text):
        return bool(m.evaluate(text, intended_participants="user",
                               world_dir=world, skills_dir=skills).get("would_block"))

    for tok, (fp, _skill, genuine) in sorted(_SURVEY_20260831.items()):
        assert not blocks(fp), f"{tok}: FP blocks with the fix in place"
        assert blocks(genuine), f"{tok}: recall control does not block with the fix in place"

    saved = m._GENERIC_NAME_PARTS
    try:
        m._GENERIC_NAME_PARTS = frozenset()
        for tok, (fp, _skill, genuine) in sorted(_SURVEY_20260831.items()):
            assert blocks(fp), (
                f"reverting the fix did not reproduce the {tok!r} FP — the test "
                f"is not pinning what it claims to pin")
            assert blocks(genuine), (
                f"{tok!r} recall control FLIPPED under the mutation; it is "
                f"tracking the fix rather than controlling for it")
    finally:
        m._GENERIC_NAME_PARTS = saved
