#!/usr/bin/env python3
"""Tests for inbound-reference-census.py ( / D1).

The load-bearing case is the THREE-STATE classification. A two-state
(live/dangling) census reports a different number on every box, because
`agents/<other>/temp/x` is absent here whether or not it was ever purged.
Both the filing goal's cited figure and an early Ayoai-Mind measurement
carried that flaw, so the `unmeasurable` state is pinned hardest here — a
regression that folds it into `dangling` is exactly the defect this tool
exists to avoid, and it would look like a passing census.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "inbound_reference_census", SCRIPTS / "inbound-reference-census.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


census = _load()


# ---------------------------------------------------------------- three-state

def test_nonresident_agent_missing_path_is_unmeasurable_not_dangling():
    """THE regression guard. Folding this into `dangling` re-introduces the
    box-dependence flaw the whole three-state model exists to prevent."""
    ref = "agents/zzznotreal/temp/never-existed-anywhere.json"
    assert census.classify(ref, resident={"echo"}) == "unmeasurable"


def test_resident_agent_missing_path_is_dangling():
    """Symmetry: for an agent this box OWNS, absence IS evidence."""
    ref = "agents/echo/temp/definitely-not-present-xyzzy.json"
    assert census.classify(ref, resident={"echo"}) == "dangling"


def test_nonagent_missing_path_is_dangling():
    """Git-tracked artifacts are present on every box, so absence is real."""
    ref = "core/scripts/definitely-not-a-real-script-xyzzy.py"
    assert census.classify(ref, resident=set()) == "dangling"


def test_existing_path_is_live_negative_control():
    """Negative control: a file that certainly exists must not read dangling.
    Without this, a classify() that returned 'dangling' unconditionally would
    pass every other test in this file."""
    assert census.classify("core/scripts/tree.py", resident=set()) == "live"


def test_external_world_path_resolves_and_is_live():
    """`world/` is an EXTERNAL configured path — resolving it against
    PROJECT_ROOT yields a path that never exists.

    This is the regression guard for the g-306-99 fresh-eyes finding: the
    original classify() did `PROJECT_ROOT / ref`, so the most-referenced
    convention in the repo reported `dangling` and ~70 of 132 dangling refs
    were resolver artifacts. The negative control above did NOT catch it,
    because every path it probed was PROJECT_ROOT-relative — so this test
    deliberately probes the OTHER resolution root. A census that mis-resolves
    is not slightly wrong, it is loudly wrong in the exact direction people
    act on."""
    assert census.classify(
        "world/conventions/capability-routing.md", resident=set()) == "live"


def test_assume_local_authoritative_collapses_unmeasurable():
    """The documented single-box escape hatch still works, and only when asked."""
    ref = "agents/zzznotreal/temp/never-existed-anywhere.json"
    assert census.classify(ref, resident={"echo"},
                           assume_local_authoritative=True) == "dangling"


# ------------------------------------------------------------- JSONL decoding

def test_store_scan_decodes_json_and_does_not_glue_escaped_newlines(tmp_path):
    """A `\\n` inside a JSON string must not fuse a path with the next line.

    Regexing the RAW line yielded `core/config/aspirations.yaml\\nhealth_...`
    on this tool's first run — a non-existent path that then classified as
    dangling. Decoding first is the fix; this pins it."""
    store = tmp_path / "s.jsonl"
    store.write_text(json.dumps(
        {"content": "see core/config/aspirations.yaml\nhealth_regression.mode is set"}
    ) + "\n", encoding="utf-8")
    texts = list(census._iter_store_text(store))
    joined = "\n".join(texts)
    found = set(census._ARTIFACT_RE.findall(joined))
    assert "core/config/aspirations.yaml" in found
    assert not any("health_regression" in f for f in found)


def test_store_scan_survives_malformed_line(tmp_path):
    """Append-only stores must not be blinded by one bad row (fail-open)."""
    store = tmp_path / "s.jsonl"
    store.write_text(
        "{not json at all\n"
        + json.dumps({"c": "core/scripts/tree.py"}) + "\n",
        encoding="utf-8")
    assert any("core/scripts/tree.py" in t for t in census._iter_store_text(store))


def test_walk_strings_reaches_nested_values():
    rec = {"a": "x", "b": {"c": ["y", {"d": "z"}]}, "n": 3}
    assert set(census._walk_strings(rec)) == {"x", "y", "z"}


# ------------------------------------------------------------- noise filtering

@pytest.mark.parametrize("ref", [
    "core/scripts/_dt.parse_naive_iso",      # module.function, not a file
    "core/scripts/...sh",                    # ellipsis
    "core/scripts/PYFILE.py",                # ALL-CAPS placeholder
    "core/config/X.yaml",                    # single-letter placeholder
    "core/scripts/foo.sh",                   # generic doc example
    "world/conventions/",                    # bare directory
    "core/config/.+",                        # regex fragment
    "core/scripts/somedir",                  # no extension
])
def test_noise_is_rejected(ref):
    assert census._is_noise(ref) is True


@pytest.mark.parametrize("ref", [
    "core/scripts/tree.py",
    "world/conventions/capability-routing.md",
    "agents/echo/temp/real-report.json",
    ".claude/rules/self.md",
])
def test_real_refs_are_not_noise(ref):
    assert census._is_noise(ref) is False


def test_owning_agent():
    assert census._owning_agent("agents/echo/temp/x.json") == "echo"
    assert census._owning_agent("core/scripts/tree.py") is None


# ------------------------------------------------------------ prefix coverage
#
#  widened _ARTIFACT_RE from four prefix families to six (adding the
# external meta root and agents/*/journal). These tests pin ALL SIX, not just
# the two that were added: a widening that silently DROPPED one of the original
# four would still pass a test suite that only checked the new ones, and the
# headline census total would move for a reason nobody could see.

_FAMILY_REFS = {
    "agents-temp":        "agents/echo/temp/report.json",
    "agents-reports":     "agents/echo/reports/summary.md",
    "agents-experience":  "agents/echo/experience/exp-thing.md",
    "agents-session":     "agents/echo/session/handoff.yaml",
    "agents-journal":     "agents/echo/journal/2026/07/2026-07-31.md",   # new
    "world-conventions":  "world/conventions/capability-routing.md",
    "meta-root":          "meta/goal-selection-strategy.yaml",           # new
    "meta-subdir":        "meta/transfer/bundle.md",                     # new
    "core-config":        "core/config/aspirations.yaml",
    "core-scripts":       "core/scripts/tree.py",
    # NOT `.claude/skills/<x>/SKILL.md` — see
    # test_skill_md_refs_are_currently_swallowed_by_the_placeholder_filter.
    "claude-skills":      ".claude/skills/forge-skill/reference.md",
    "claude-rules":       ".claude/rules/self.md",
}


def _refs_in(text):
    """Regex + noise filter together — the same pair `collect` applies."""
    out = set()
    for m in census._ARTIFACT_RE.findall(text):
        ref = m.rstrip(".,);:")
        if not census._is_noise(ref):
            out.add(ref)
    return out


@pytest.mark.parametrize("family,ref", sorted(_FAMILY_REFS.items()))
def test_every_prefix_family_is_matched(family, ref):
    """LEVEL 1 — agreement: each family is matched where we claim it is."""
    assert ref in _refs_in("see `%s` for detail" % ref), family


def test_collect_emits_all_six_families_end_to_end(tmp_path, monkeypatch):
    """LEVEL 2 — emission: the refs survive the REAL collect() pipeline.

    Level 1 proves the regex matches a string. It cannot prove the ref reaches
    the census output, because `collect` also applies `_is_noise` and the
    surface plumbing — a noise rule that swallowed a whole family would leave
    every Level-1 assert green while the census reported nothing. So this
    drives collect() itself, with `_sources` pointed at a tmp file instead of
    the live world (hermetic, and it keeps the assertion about OUR text)."""
    doc = tmp_path / "surface.md"
    doc.write_text("\n".join("ref: `%s`" % r for r in _FAMILY_REFS.values()),
                   encoding="utf-8")
    monkeypatch.setattr(census, "_sources", lambda names: [([doc], "tree")])

    hits = census.collect([])
    missing = [r for r in _FAMILY_REFS.values() if r not in hits]
    assert not missing, "collect() dropped: %s" % missing
    assert all(hits[r] == {"tree"} for r in _FAMILY_REFS.values())


def test_meta_boundary_guard_rejects_mind_api_paths():
    """LEVEL 3 — discrimination: a healthy corpus must NOT emit these.

    Without the `(?<![\\w/-])` guard on the meta alternative, `meta/` matches
    the tail of any path containing a `meta/` directory. Measured 2026-07-31:
    three such refs were fabricated from `mind_api/src/meta/*` paths and each
    would classify DANGLING against the meta root — a referent nobody cited,
    which is the resolver-artifact class g-306-99 removed. A negative control
    is what separates 'the pattern matches what we want' from 'the pattern
    matches everything'; the first two tests above pass either way."""
    for path in ("mind_api/src/meta/meta_yaml.py",
                 "mind_api/src/meta/meta_impk.py",
                 "cognitive-horizons/meta/memory-pipeline/gates.yaml"):
        found = _refs_in(path)
        assert not any(f.startswith("meta/") for f in found), \
            "%s leaked a meta-root ref: %s" % (path, found)


def test_skill_md_refs_are_currently_swallowed_by_the_placeholder_filter():
    """KNOWN HOLE, pinned deliberately so a fix flips this test ().

    `_PLACEHOLDER_SEG` treats an ALL-CAPS stem as a documentation metavariable
    (`X.yaml`, `PYFILE.py`). `SKILL.md` is ALL-CAPS and is also the single most
    common real filename under `.claude/skills/`, so every concrete
    `.claude/skills/<name>/SKILL.md` citation is dropped as noise. Measured
    2026-07-31 on the live corpus: 65 `*/SKILL.md` refs filtered, against 2
    `.claude/skills/` refs surviving — the family is ~97% invisible and its
    census line reads as near-clean because of it.

    This is NOT the prefix regex (which matches the path fine — the assert
    below shows the raw pattern finding it) and so was out of scope for the
    g-306-107 widening, which deliberately changed `_ARTIFACT_RE` only. It is
    pinned here rather than left implicit because the alternative is a silent
    coverage hole in a family this file otherwise claims to cover: when the
    filter is narrowed, this test fails loudly and gets deleted on purpose."""
    ref = ".claude/skills/aspirations/SKILL.md"
    assert ref in census._ARTIFACT_RE.findall(ref), "prefix regex should match"
    assert census._is_noise(ref) is True, \
        "SKILL.md no longer filtered — the hole is fixed; delete this test " \
        "and add SKILL.md to _FAMILY_REFS"


def test_unlisted_prefixes_are_not_matched():
    """LEVEL 3 (second control): the census is a whitelist, not a path finder.

    `agents/<a>/insights.jsonl` and `core/BOUNDARY.md` are real files that are
    deliberately OUT of the covered families. If they matched, the widening
    would have turned the prefix list into 'anything that looks like a path'
    and every count in the close would mean something different."""
    text = ("agents/echo/insights.jsonl core/BOUNDARY.md "
            "mind_api/src/agent_paths.py world/aspirations.jsonl")
    assert _refs_in(text) == set()
