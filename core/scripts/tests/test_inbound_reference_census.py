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
    "claude-skills":      ".claude/skills/forge-skill/reference.md",
    # `SKILL.md` USED to be swallowed here by the ALL-CAPS placeholder class,
    # and a test named ..._currently_swallowed_by_the_placeholder_filter pinned
    # that hole deliberately so a fix would flip it.  narrowed the
    # class with `_LITERAL_FILENAMES`, so the hole is closed and that test is
    # gone; this row is what replaced it. Measured 2026-08-01: 61 refs newly
    # visible, 60 of them live, and NOTHING dropped (guard-2201 delta on one
    # corpus snapshot). Do not fold this into the row above — they exercise
    # different halves of `_is_noise` (that one never reaches the placeholder
    # class; this one is only kept BY the exception).
    "claude-skills-md":   ".claude/skills/aspirations/SKILL.md",
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


def test_unlisted_prefixes_are_not_matched():
    """LEVEL 3 (second control): the census is a whitelist, not a path finder.

    `agents/<a>/insights.jsonl` and `core/BOUNDARY.md` are real files that are
    deliberately OUT of the covered families. If they matched, the widening
    would have turned the prefix list into 'anything that looks like a path'
    and every count in the close would mean something different."""
    text = ("agents/echo/insights.jsonl core/BOUNDARY.md "
            "mind_api/src/agent_paths.py world/aspirations.jsonl")
    assert _refs_in(text) == set()


# ------------------------------------------- external-root store consult ()
#
# `world/conventions/` and `meta/` are gitignored EXTERNAL roots. On own-cloud
# the local tree is a read-through cache, so local absence is not evidence and
# these refs are resolved against the STORE. None of the three branches below is
# reachable by any test above: the two existing external-root tests resolve
# LOCALLY and short-circuit at `exists`, and the non-agent dangling test uses a
# `core/` path. The branch shipped uncovered without these.
#
# All three monkeypatch `storage_backend.get_backend` rather than stubbing
# `_store_exists`, so the real tri-state logic — including its except path — is
# what runs. Nothing here touches S3 (guard-955).

class _FakeBackend:
    def __init__(self, present): self._present = present
    def exists(self, path): return self._present


@pytest.fixture
def _patch_backend(monkeypatch):
    import storage_backend

    def _install(behaviour):
        if behaviour == "raise":
            def boom(*a, **k):
                raise RuntimeError("simulated store outage")
            monkeypatch.setattr(storage_backend, "get_backend", boom)
        else:
            monkeypatch.setattr(storage_backend, "get_backend",
                                lambda *a, **k: _FakeBackend(behaviour))
    return _install


@pytest.mark.parametrize("ref", ["meta/never-here-xyzzy.yaml",
                                 "world/conventions/never-here-xyzzy.md"])
def test_external_root_present_in_store_is_live_not_dangling(_patch_backend, ref):
    """THE point of the fix: a file absent locally but alive in the store is a
    CACHE MISS, not breakage. Before g-306-115 this returned `dangling`, which
    is the loudest possible false alarm on a box that has not materialized
    everything — a fresh clone or new fleet member, i.e. exactly the box least
    able to notice it is wrong."""
    _patch_backend(True)
    assert census.classify(ref, resident=set()) == "live"


@pytest.mark.parametrize("ref", ["meta/never-here-xyzzy.yaml",
                                 "world/conventions/never-here-xyzzy.md"])
def test_external_root_absent_from_store_is_dangling(_patch_backend, ref):
    """Negative control. Without it, a classify() that returned `live` for every
    external-root ref would pass the test above — and would hide real breakage,
    which is the failure direction that costs more than a false alarm."""
    _patch_backend(False)
    assert census.classify(ref, resident=set()) == "dangling"


@pytest.mark.parametrize("ref", ["meta/never-here-xyzzy.yaml",
                                 "world/conventions/never-here-xyzzy.md"])
def test_external_root_unreachable_store_is_unmeasurable_not_dangling(
        _patch_backend, ref):
    """`OwnCloudBackend.exists` RE-RAISES anything that is not a not-found code,
    so a permissions or network failure arrives as an exception rather than a
    False. Collapsing it to `dangling` would report an S3 outage as a pile of
    broken references. It is also how the g-306-115 predecessor probe went
    wrong — it read a raise as absence, so it returned no signal while looking
    like a negative result."""
    _patch_backend("raise")
    assert census.classify(ref, resident=set()) == "unmeasurable"


def test_store_consult_is_scoped_to_external_roots(_patch_backend):
    """Scope guard. `core/` and `.claude/` are git-tracked and present on every
    box, so absence IS absence and they must NOT reach the store — a store
    outage cannot turn them unmeasurable. `world/knowledge/tree/` is likewise
    excluded (the tree has its own tooling)."""
    _patch_backend("raise")
    for ref in ("core/scripts/definitely-not-real-xyzzy.py",
                ".claude/rules/definitely-not-real-xyzzy.md",
                "world/knowledge/tree/definitely-not-real-xyzzy.md"):
        assert census.classify(ref, resident=set()) == "dangling", ref


@pytest.mark.parametrize("ref,expected", [
    ("meta/x.yaml", True),
    ("world/conventions/x.md", True),
    ("core/scripts/x.py", False),
    (".claude/rules/x.md", False),
    ("world/knowledge/tree/x.md", False),
    ("agents/echo/temp/x.json", False),
])
def test_is_external_root_gating(ref, expected):
    assert census._is_external_root(ref) is expected


# ── Reporting layer ──────────────────────────────────────────────────────────
# Every test above targets classify(). The reporter had NONE, and that is where
# the  fresh-eyes pass found a defect: `unmeasurable` gained a second
# producer and the human-readable NOTE still attributed the whole bucket to
# "non-resident agents' boxes" — a confident mis-diagnosis printed exactly when
# the store is down. A classifier-only suite cannot see it, because classify()
# was right the whole time.

def test_unmeasurable_note_distinguishes_its_two_producers(monkeypatch, capsys):
    """`unmeasurable` means 'this box cannot vouch', which has two causes that
    call for OPPOSITE responses: an off-box agent artifact (nothing is wrong)
    and a store that could not be consulted (infrastructure is wrong). One
    hardcoded explanation is necessarily false for one of them at all times."""
    records = [
        {"ref": "agents/alpha/temp/x.md", "inbound_count": 1, "surfaces": ["s"],
         "owner": "alpha", "status": "unmeasurable"},
        {"ref": "world/conventions/a.md", "inbound_count": 1, "surfaces": ["s"],
         "owner": None, "status": "unmeasurable"},
        {"ref": "meta/b.yaml", "inbound_count": 1, "surfaces": ["s"],
         "owner": None, "status": "unmeasurable"},
    ]
    monkeypatch.setattr(census, "census", lambda *a, **k: (records, ["bravo"]))
    monkeypatch.setattr(sys, "argv", ["inbound-reference-census.py"])
    census.main()
    out = capsys.readouterr().out

    assert "1 ref(s) belong to non-resident agents' boxes" in out
    assert "2 external-root ref(s) could not be checked" in out
    # The regression: the off-box wording must not absorb the store-failure count.
    assert "3 ref(s) belong to non-resident agents' boxes" not in out


def test_unmeasurable_note_omits_the_store_line_when_no_store_failure(
        monkeypatch, capsys):
    """Negative control for the test above — without it, a reporter that printed
    the infrastructure NOTE unconditionally would pass, and every ordinary
    off-box census would cry outage."""
    records = [
        {"ref": "agents/alpha/temp/x.md", "inbound_count": 1, "surfaces": ["s"],
         "owner": "alpha", "status": "unmeasurable"},
    ]
    monkeypatch.setattr(census, "census", lambda *a, **k: (records, ["bravo"]))
    monkeypatch.setattr(sys, "argv", ["inbound-reference-census.py"])
    census.main()
    out = capsys.readouterr().out

    assert "belong to non-resident agents' boxes" in out
    assert "could not be checked against the store" not in out
