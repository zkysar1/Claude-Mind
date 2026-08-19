"""GET /v1/pipeline/read — exactly-one selector enforcement ().

WHY A REFUSAL AND NOT A COMPOSITION
    The module docstring has always declared these selectors "mutually exclusive
    -- exactly one". Nothing enforced it: every branch RETURNS, so a caller
    passing two got whichever sat earlier in the function and was never told the
    other was discarded. Making the pair an ERROR is the implementation honoring
    the contract it already declared; composing them would have CHANGED that
    contract and the response shape of every pair.

WHY THESE CASES ARE BUILT ON THE FAILING SIDE (rb-6208, guard-1220)
    A guard that refused EVERYTHING would satisfy every refusal case here, so
    the refusal cases alone are vacuous. `test_each_selector_alone_still_works`
    and `test_sanctioned_narrative_composition_still_works` are the mutation
    proof: they fail against an over-broad guard, and the refusal cases fail
    against no guard at all. Neither half is dropable.

PRODUCTION ARG SHAPE, NOT THE CONTRACT-IDEAL ONE (guard-920)
    `stage=resolved&unreflected=1` and `unreflected=1&counts=1` are the two
    combinations that were MEASURED in the field -- the first is the 22.5x
    over-count (90 records returned where 4 were correct), the second is the one
    live call site in the whole corpus (aspirations-consolidate Step 0.1). They
    are pinned as the literal query strings pipeline-read.sh emits, not as a
    tidied-up equivalent.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest


def _get(port: int, query: dict, *, agent: str = "alpha"):
    """(status, body) — never raises on 4xx, because 4xx IS the thing under test."""
    qs = urllib.parse.urlencode(query)
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/pipeline/read?{qs}", method="GET")
    req.add_header("X-Mind-Agent", agent)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _rec(rid, stage, reflected=False):
    return {"id": rid, "stage": stage, "hypothesis": f"h for {rid}",
            "reflected": reflected}


@pytest.fixture
def seeded(running_daemon):
    project_root, port = running_daemon
    live = project_root / "world" / "pipeline.jsonl"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text("".join(json.dumps(r) + "\n" for r in [
        _rec("2026-08-01_alpha", "resolved", reflected=False),
        _rec("2026-08-02_bravo", "resolved", reflected=True),
        _rec("2026-08-03_charlie", "active"),
    ]), encoding="utf-8")
    (project_root / "world" / "pipeline-archive.jsonl").write_text("", encoding="utf-8")
    return project_root, port


# --- the two combinations measured in the field ----------------------------

def test_measured_stage_plus_unreflected_is_refused(seeded):
    """The 22.5x over-count. Pre-fix this returned the FULL stage=resolved set
    with `unreflected` silently discarded -- 90 records where 4 were correct,
    always in the direction that makes the reflection backlog look enormous."""
    _, port = seeded
    status, body = _get(port, {"stage": "resolved", "unreflected": "1"})
    assert status == 400, f"expected refusal, got {status}: {body[:200]}"
    assert "ambiguous_selectors" in body
    assert "stage" in body and "unreflected" in body, (
        "the refusal must NAME both selectors -- a caller who cannot see which "
        "flag was discarded is no better off than before")


def test_measured_unreflected_plus_counts_is_refused(seeded):
    """The one live call site in the corpus (aspirations-consolidate Step 0.1).
    Pre-fix this returned the stage-counts object, and the consumer then read an
    `active_unreflected` key that no response shape carries -- a permanent,
    silent zero folded into the consolidation triage tier."""
    _, port = seeded
    status, body = _get(port, {"unreflected": "1", "counts": "1"})
    assert status == 400, f"expected refusal, got {status}: {body[:200]}"
    assert "ambiguous_selectors" in body


def test_refusal_names_the_branch_that_would_have_won(seeded):
    """Precedence is the whole mechanism, so the error states it. `counts` is
    tried before `unreflected`, which is exactly why the live consumer got a
    counts object back when it asked for unreflected records."""
    _, port = seeded
    _, body = _get(port, {"unreflected": "1", "counts": "1"})
    assert "counts" in body
    assert re.search(r"discard", body), "must say the others were discarded"


# --- anti-vacuity: the guard must not refuse everything --------------------

@pytest.mark.parametrize("query,expect", [
    ({"unreflected": "1"}, list),
    ({"counts": "1"}, dict),
    ({"stage": "resolved"}, list),
    ({"replay_candidates": "1"}, list),
])
def test_each_selector_alone_still_works(seeded, query, expect):
    """MUTATION PROOF. An over-broad guard (refusing on key PRESENCE, or on any
    two query params including non-selectors) passes every refusal case above
    and fails here. Without this, the refusal cases prove nothing."""
    _, port = seeded
    status, body = _get(port, query)
    assert status == 200, f"{query} must still work, got {status}: {body[:200]}"
    assert isinstance(json.loads(body), expect)


def test_falsy_flag_value_is_not_a_selector(seeded):
    """flag() treats "0"/"false"/"" as ABSENT, and the guard must use that same
    predicate rather than mere key presence -- otherwise a wrapper that emits
    every flag with a default of 0 would be refused on every call."""
    _, port = seeded
    status, body = _get(port, {"counts": "0", "unreflected": "1"})
    assert status == 200, f"counts=0 is not a selector; got {status}: {body[:200]}"
    assert isinstance(json.loads(body), list)


def test_sanctioned_narrative_composition_still_works(seeded):
    """narrative= is the ONE documented composition. Refusing it would break the
    replay path (`pipeline-read.sh --narrative --id <id>`)."""
    _, port = seeded
    for q in ({"narrative": "1", "stage": "resolved"},
              {"narrative": "1", "id": "2026-08-01_alpha"}):
        status, body = _get(port, q)
        assert status == 200, f"{q} is sanctioned; got {status}: {body[:200]}"


def test_narrative_with_both_id_and_stage_is_refused(seeded):
    """Three is never sanctioned. narrative+id+stage drops the stage filter on
    the SAME precedence principle this guard exists to stop -- the narrative
    branch takes the id path and never applies n_stage."""
    _, port = seeded
    status, _ = _get(port, {"narrative": "1", "id": "2026-08-01_alpha",
                            "stage": "resolved"})
    assert status == 400


def test_zero_selectors_still_gets_the_missing_flag_error(seeded):
    """The guard fires on >1 only. A caller passing NONE must keep the existing
    missing-flag error, which lists the valid selectors -- swallowing that into
    an ambiguity error would make the empty call harder to diagnose, not easier."""
    _, port = seeded
    status, body = _get(port, {"unused": "x"})
    assert status != 200
    assert "ambiguous_selectors" not in body


# --- the constant must describe the code (guard-1943) ----------------------

def test_selector_precedence_matches_the_real_branch_order():
    """SELECTOR_PRECEDENCE exists so the refusal can name the winning branch. If
    it drifts from the actual `if` order, every refusal message names the WRONG
    winner -- authoritative-looking and wrong. Pinning the constant against
    itself would prove nothing, so this parses the source's branch order."""
    src = (Path(__file__).resolve().parents[1] / "src" / "world" / "pipeline.py"
           ).read_text(encoding="utf-8")
    body = src[src.index("def read(ctx)"):]
    body = body[body.index("# Checked BEFORE stage/id"):]  # skip the guard itself

    order, seen = [], set()
    for name in re.findall(
            r'flag\(q,\s*"(\w+)"\)|\b(?:stage|rec_id)\s*=\s*q\.get\("(\w+)"\)', body):
        n = name[0] or name[1]
        if n not in seen:
            seen.add(n)
            order.append(n)

    # Read the constant OUT OF THE SOURCE rather than importing it: these tests
    # drive the daemon over HTTP and never put mind_api/src on sys.path, so an
    # import here would couple a source-order assertion to a path fixture it does
    # not otherwise need.
    decl = re.search(r"SELECTOR_PRECEDENCE\s*=\s*\((.*?)\)", src, re.S).group(1)
    SELECTOR_PRECEDENCE = re.findall(r'"(\w+)"', decl)
    assert order == list(SELECTOR_PRECEDENCE), (
        f"branch order {order} has drifted from SELECTOR_PRECEDENCE "
        f"{list(SELECTOR_PRECEDENCE)} — the refusal message would name the "
        f"wrong winning branch")
