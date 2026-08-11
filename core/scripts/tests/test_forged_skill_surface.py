"""Tests for core/scripts/forged-skill-surface.py — the Phase 4 forged-skill reader.

The reader used to MATCH the registry against goal text. g-115-4446 measured
five candidate matchers on a 30-goal hand-labelled sample and none cleared the
bar (shipped matcher: recall 0.00; best precision across all candidates: 0.12),
so g-115-4475 retired matching entirely and made the step print the whole
registry unconditionally. This file pins the contract that replaced it:

  * COMPLETENESS — every registered skill appears, unfiltered. That is the
    property the retirement bought (recall 1.00 by construction), so it is the
    one a regression would silently take back.
  * NO MATCHER SURVIVES — the removed symbols must stay removed. Re-introducing
    a filter is the specific drift this goal exists to prevent, and it would
    not fail any other test here: a matcher that filtered the index would still
    return rows, still sort, still render.
  * BLOCK-SCALAR DESCRIPTIONS — 2 of 42 live skills write `description: >-`,
    where a naive `^description:` regex captures the marker instead of the
    folded text. Measured, and it rendered as a literal ">-" in the index.
  * ROBUSTNESS — the advisory must never raise and never exit non-zero,
    including on the legacy `--goal/--source` call shape that in-flight
    sessions still use.

The hermetic tests use a synthetic registry, NOT world/forged-skills.yaml — the
live registry is domain state that changes underneath the framework. The single
live-registry test at the bottom exists because without it every hermetic test
here would still pass while production rendered an empty menu.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "forged-skill-surface.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("forged_skill_surface", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fss = _load_module()


@pytest.fixture
def registry(monkeypatch):
    """Install a synthetic forged-skills registry, deliberately unsorted."""
    entries = [
        {
            "skill": "scan-stale-jobs",
            "triggers": ["stale", "reap"],
            "scripts": ["world/scripts/scan-stale.sh"],
        },
        {
            "skill": "access-aws-services",
            "triggers": ["aws cli", "call aws"],
            "scripts": ["world/scripts/aws-exec.sh"],
        },
        {
            # No triggers at all. Under the old matcher this row was
            # unreachable; under an index it MUST still be listed — that
            # difference is the whole point of the retirement.
            "skill": "no-triggers-at-all",
            "triggers": [],
            "scripts": ["world/scripts/whatever.sh"],
        },
    ]
    monkeypatch.setattr(fss, "_load_forged_skills", lambda _wdir: entries)
    monkeypatch.setattr(fss, "_description", lambda name: "desc for " + name)
    return entries


def _names(index):
    return [e["skill"] for e in index]


# ── Completeness: the property the retirement bought ───────────────────────

def test_index_lists_every_registered_skill(registry):
    assert set(_names(fss.build_index(None))) == {
        "scan-stale-jobs", "access-aws-services", "no-triggers-at-all"}


def test_skill_with_no_triggers_is_still_listed(registry):
    """The old matcher could never surface this row; the index always does."""
    assert "no-triggers-at-all" in _names(fss.build_index(None))


def test_index_is_sorted_by_name(registry):
    names = _names(fss.build_index(None))
    assert names == sorted(names)


def test_render_emits_one_row_per_skill(registry):
    body = fss.render(fss.build_index(None))
    assert len(body.splitlines()) == 3
    assert all(line.startswith("  /") for line in body.splitlines())


def test_render_carries_name_and_description(registry):
    body = fss.render(fss.build_index(None))
    assert "  /access-aws-services — desc for access-aws-services" in body


# ── No matcher survives ────────────────────────────────────────────────────

def test_no_matching_symbols_remain():
    """Mutation guard for the goal's own criterion: 'no per-goal filtering
    logic remains to re-drift'. Re-adding any of these would pass every other
    test in this file."""
    for gone in ("match_skills", "MIN_TRIGGER_WORDS", "_norm",
                 "rule_name_phrases", "_goal_text"):
        assert not hasattr(fss, gone), f"{gone} was retired by g-115-4475"


def test_build_index_takes_no_goal_text():
    """The index is a function of the registry alone — there is no text input
    it could filter on."""
    import inspect
    params = list(inspect.signature(fss.build_index).parameters)
    assert params == ["wdir"]


# ── Description sourcing, including the block-scalar case ──────────────────

def _write_skill(root: Path, name: str, front: str):
    d = root / ".claude" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\n{front}\n---\n\n# body\n",
                                encoding="utf-8")


def test_description_reads_plain_scalar(tmp_path, monkeypatch):
    _write_skill(tmp_path, "plain-skill", 'description: "Does a plain thing"')
    monkeypatch.setattr(fss, "PROJECT_ROOT", tmp_path)
    assert fss._description("plain-skill") == "Does a plain thing"


def test_description_reads_block_scalar(tmp_path, monkeypatch):
    """REFUSE the naive-regex result: `>-` is a YAML marker, not the text.

    Measured on the live registry — 2 of 42 skills use this form, and both
    rendered as a literal '>-' in the index before g-115-4475.
    """
    _write_skill(tmp_path, "folded-skill",
                 "description: >-\n  Joins two decoupled stores that the\n  dashboard intersects")
    monkeypatch.setattr(fss, "PROJECT_ROOT", tmp_path)
    desc = fss._description("folded-skill")
    assert desc.startswith("Joins two decoupled stores")
    assert ">-" not in desc


def test_description_missing_skill_md_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(fss, "PROJECT_ROOT", tmp_path)
    assert fss._description("never-forged") == ""


# ── _one_line clipping ─────────────────────────────────────────────────────

def test_one_line_collapses_whitespace():
    assert fss._one_line("a\n  b\tc", 40) == "a b c"


def test_one_line_clips_on_a_word_boundary():
    out = fss._one_line("alpha beta gamma delta epsilon", 14)
    assert out == "alpha beta"


def test_one_line_does_not_over_clip_a_single_long_token():
    """A word-boundary cut that throws away most of the budget is worse than a
    mid-token cut — the row would carry almost no information."""
    out = fss._one_line("a supercalifragilisticexpialidocious matter", 20)
    assert len(out) >= 14


def test_one_line_handles_none_and_empty():
    assert fss._one_line(None, 10) == ""
    assert fss._one_line("", 10) == ""


# ── Robustness: the advisory must never raise ──────────────────────────────

def test_registry_load_failure_yields_empty_index(monkeypatch):
    def boom(_wdir):
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(fss, "_load_forged_skills", boom)
    assert fss.build_index(None) == []


def test_absent_loader_yields_empty_index(monkeypatch):
    monkeypatch.setattr(fss, "_load_forged_skills", None)
    assert fss.build_index(None) == []


def test_entry_without_a_skill_name_is_skipped(monkeypatch):
    monkeypatch.setattr(fss, "_load_forged_skills",
                        lambda _w: [{"triggers": []}, {"skill": "real-one"}])
    monkeypatch.setattr(fss, "_description", lambda name: "")
    assert _names(fss.build_index(None)) == ["real-one"]


def test_main_exits_zero_on_empty_registry(monkeypatch, capsys):
    monkeypatch.setattr(fss, "_load_forged_skills", lambda _w: [])
    assert fss.main([]) == 0
    assert "empty or unreadable" in capsys.readouterr().err


# ── CLI contract, including the legacy call shape ──────────────────────────

def _require_live_registry():
    """Skip loudly unless a real forged-skill registry is reachable.

    Every test that asserts on NON-EMPTY output shares this precondition, and
    they must handle it the same way. Resolves the world FRESH (see
    test_live_registry_renders_a_non_empty_index for why the module constant
    is unsafe). Deliberately a skip, never a relaxed `>= 0` assertion — a
    vacuous pass would restore the blind spot these tests exist to close
    (rb-245 family).
    """
    import _paths

    world = _paths._resolve_external("MIND_WORLD", "WORLD_PATH")
    if not (world / "forged-skills.yaml").is_file():
        pytest.skip(f"no live registry at {world} — this assertion needs a real world")
    return world


def test_cli_accepts_the_legacy_goal_flags():
    """In-flight sessions still hold the pre- pseudocode. Erroring on
    their arguments would break the one invariant this script has: it must
    never block goal execution."""
    _require_live_registry()
    r = subprocess.run([sys.executable, str(_SCRIPT), "--goal", "g-1-1",
                        "--source", "world"], capture_output=True, text=True)
    assert r.returncode == 0
    assert "FORGED SKILLS ALREADY EXIST" in r.stdout


# ── The CALL SITE, not just the script ─────────────────────────────────────

def test_phase_4_call_site_passes_no_goal_arguments():
    """The invocation in aspirations-execute must carry no per-goal argument.

    The existing /verify-learning wiring check greps only for the FILENAME in
    that SKILL.md, so it passes identically whether the call site says
    `forged-skill-surface.py` or `forged-skill-surface.py --goal <id>`. That
    makes it blind to the one regression this goal exists to prevent: a future
    edit re-introducing per-goal filtering at the call site rather than in the
    script. The script-side guard (test_no_matching_symbols_remain) cannot see
    it either — the script would still be matcher-free while its caller
    pretended otherwise. This is the only assertion covering that seam.
    """
    from _paths import PROJECT_ROOT

    skill = (PROJECT_ROOT / ".claude" / "skills" / "aspirations-execute"
             / "SKILL.md").read_text(encoding="utf-8")
    calls = [ln.strip() for ln in skill.splitlines()
             if "forged-skill-surface.py" in ln and ln.lstrip().startswith("Bash:")]
    assert calls, "aspirations-execute no longer invokes the forged-skill reader"
    for line in calls:
        for arg in ("--goal", "--source", "--text"):
            assert arg not in line, (
                f"call site re-introduced per-goal filtering ({arg}): {line}")


def test_cli_json_shape():
    _require_live_registry()
    r = subprocess.run([sys.executable, str(_SCRIPT), "--json"],
                       capture_output=True, text=True)
    assert r.returncode == 0
    payload = json.loads(r.stdout)
    assert payload["count"] == len(payload["skills"])
    assert payload["count"] > 0
    assert set(payload["skills"][0]) == {"skill", "description", "scripts"}


# ── The live registry — without this, an empty menu passes everything above ─

def _split_empty_descriptions(index, project_root):
    """Partition rows with no description by WHOSE FAULT the emptiness is.

    Two different causes produce an identical empty string, and only one of
    them is a defect in this repo:

      unresolvable — the body IS in this checkout and still yields nothing.
                     A real defect: absent/unparseable front matter, a missing
                     `description` key, or a reader bug like the `description:
                     >-` block-scalar case this file already pins. ASSERT.
      not_landed   — the body is not in this checkout at all. Registry rows are
                     shared state that propagates near-instantly; bodies are
                     git-tracked and arrive only when this box pulls. Between a
                     partner forging and this box pulling, the row names a body
                     that is legitimately absent here. REPORT.

    Returns (unresolvable, not_landed), both sorted name lists.
    """
    unresolvable, not_landed = [], []
    for entry in index:
        if str(entry.get("description") or "").strip():
            continue
        body = Path(project_root) / ".claude" / "skills" / entry["skill"] / "SKILL.md"
        (unresolvable if body.is_file() else not_landed).append(entry["skill"])
    return sorted(unresolvable), sorted(not_landed)


def test_absent_body_is_reported_and_present_body_is_asserted():
    """Pin the discriminator itself — the live test below cannot.

    On a box that is up to date, `not_landed` is empty (measured cc-02: 46 of
    46 registry bodies present), so the live assertion never exercises the
    branch that stops it from false-firing. Without this, the split would be
    wiring nobody tests (guard-1943), and the two buckets could be swapped
    without any test noticing (guard-1220).
    """
    index = [
        {"skill": "has-description", "description": "does a thing"},
        {"skill": "body-here-but-blank", "description": "  "},
        {"skill": "body-not-pulled-yet", "description": ""},
    ]

    # `has-description` is filtered before any filesystem lookup; the other two
    # are separated purely by whether their SKILL.md EXISTS under the tmp root.
    # The file's CONTENT is deliberately irrelevant — the helper calls is_file()
    # and never parses. Description resolution already happened upstream in
    # build_index; this function only attributes an empty one. Do not "improve"
    # this fixture with real front matter: that would imply a parse this code
    # does not do, and the emptiness it is standing in for can equally come from
    # unparseable front matter as from a missing key.
    with tempfile.TemporaryDirectory() as td:
        present = Path(td) / ".claude" / "skills" / "body-here-but-blank"
        present.mkdir(parents=True)
        (present / "SKILL.md").write_text("", encoding="utf-8")

        unresolvable, not_landed = _split_empty_descriptions(index, td)

    assert unresolvable == ["body-here-but-blank"], (
        "a body that IS present and still yields no description must stay a "
        "hard failure — that is the whole detection this split preserves")
    assert not_landed == ["body-not-pulled-yet"], (
        "a body absent from this checkout must be REPORTED, not asserted")
    assert "has-description" not in unresolvable + not_landed


def test_live_registry_renders_a_non_empty_index():
    """Hermetic tests stub the loader, so production could silently render an
    empty menu and every test above would still pass. That is the 'tested
    instrument wired to nothing' shape this reader was built to fix, one level
    up (sig-48).

    Resolves the world path FRESH rather than importing `_paths.WORLD_DIR`.
    That constant is module state many tests in this tree reassign, and a
    stale value survives into whatever runs next in the same process: measured
    2026-08-01, this test passed solo and failed inside chunk 05 of a 16-chunk
    run with `assert 0 > 10`, because the constant pointed at an already-deleted
    tmp dir. `_resolve_external` re-reads env + local-paths.conf at call time
    and is the same resolution production does, so it returns the live world
    (42 rows) even with the constant polluted (0 rows) — both measured.

    WHY AN ABSENT BODY IS REPORTED, NOT ASSERTED (g-306-156). Until this split,
    every empty description failed here — including one caused by a body that
    had simply not been pulled yet. The registry and the body travel by
    DIFFERENT transports (shared state vs git), so between a partner forging a
    skill and this box pulling, a correct repo fails this test on this box
    alone. That red is indistinguishable from a genuine defect at the moment
    you meet it, and a GENUINE verdict is supposed to mean act on it — so it
    minted a spurious lead on every forge/pull crossing.

    The cut is the one the sibling guard already makes: /verify-learning
    Section FSG solves the same two-transport problem and guards ALL THREE of
    its sub-checks with `if not os.path.isfile(p): continue` — "every registry
    row whose skill dir EXISTS LOCALLY". Its own comment calls that check and
    this one a pair covering "both halves of transport"; one half carried the
    locality guard and this half did not. Nothing is given up: a body that IS
    present and still yields no description remains a hard failure, which is
    every genuine case the assertion ever caught.

    Deliberately NOT fixed by ordering the forge writes (push the body, then
    publish the registry row). That guarantees only that the body is on ORIGIN
    when the row appears — it cannot put the body in THIS working tree, so
    every other box must still pull and the window survives. It would also make
    registration depend on push success. The skew is inherent to having two
    transports with different propagation, and read-side tolerance is what
    actually closes it.

    A name that PERSISTS in the reported list across pulls is a REAL strand and
    Section FSG owns it.
    """
    from _paths import PROJECT_ROOT

    world = _require_live_registry()

    index = fss.build_index(world)
    assert len(index) > 10, f"live registry produced only {len(index)} rows"

    unresolvable, not_landed = _split_empty_descriptions(index, PROJECT_ROOT)

    if not_landed:
        warnings.warn(
            "forged-skill transport skew (not a defect in this repo): registry "
            f"rows whose body has not landed in this checkout yet: {not_landed}. "
            "The registry propagates as shared state; the body is git-tracked "
            "and arrives on pull. Re-check after `git pull`. A name that "
            "persists across pulls is a real strand — see /verify-learning "
            "Section FSG.",
            stacklevel=2,
        )

    assert not unresolvable, (
        "skills whose SKILL.md IS present in this checkout but yields no "
        f"description: {unresolvable}")
