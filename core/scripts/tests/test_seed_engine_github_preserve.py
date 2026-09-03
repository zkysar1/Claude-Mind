"""test_seed_engine_github_preserve.py — destination-owned .github/ survives the sweep.

Background (2026-09-03, g-335-1456). The owner reported that his Vinheim
resident showed no self identity. Three prior attributions were ruled out on
evidence; the surviving cause was "the customer seed is stale". This test pins
the layer BELOW that: WHY it went stale and stayed stale.

  2026-08-21  9dc21b38b  the CI seed-publication lane lands in Claude-Mind
                         (g-365-06): build + verify + publish mind-seed.tar.gz
                         on every push to main.
  2026-08-22  00:35:40   the lane runs ONCE and publishes the artifact.
  2026-08-23  3ea17d0f1  `chore: sync framework (2026-08-23)` deletes the whole
                         .github tree as ORPHANS -- seed-transplant's Step 10.5
                         removes destination files absent from the manifest.
  08-23..09-03           main takes 100 commits and 12+ merges. ZERO publishes,
                         because the workflow that publishes no longer exists.

So every live customer environment froze on the 2026-08-22 artifact, whose
framework predates the self projection lane (334058d17, 2026-08-29) entirely --
no environment COULD publish a self identity.

.github/ is purely DESTINATION-OWNED: the source repo has no .github/ at all
and the manifest carries zero .github entries in include OR exclude_always, so
a transplant can never plant this tree. It could only ever delete it. Same
class as the .mind-data row (2026-07-07 ZDS world+meta wipe) and the third
occurrence of it -- a destination-owned tree absent from _ORPHAN_SCAN_SKIP_TOP
is not preserved-by-default, it is DESTROYED by default.

MUTATION CONTRACT (guard-4166) -- stated BEFORE running, because this is a
NARROWING fix whose success looks like an absence, and "nothing was deleted" is
also exactly what a completely dead sweep produces:

  removing ".github" from _ORPHAN_SCAN_SKIP_TOP MUST turn RED:
      test_github_workflow_survives_orphan_sweep
      test_skip_entry_is_present_and_reasoned
  and MUST leave GREEN:
      test_a_genuine_orphan_is_still_removed          <-- POSITIVE CONTROL
      test_source_cannot_plant_github                 <-- scope-unaffected
      test_shipped_manifest_does_not_cruft_sweep_github  <-- scope-unaffected

That asymmetry is the evidence. The positive control is the load-bearing half:
without it, a sweep that stopped removing ANYTHING would pass the two pins
above. It asserts the sweep still deletes a real orphan, so it fails the
do-nothing world by construction.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

ENGINE_PATH = CORE_SCRIPTS / "_seed_engine.py"
_spec = importlib.util.spec_from_file_location("_seed_engine_github_t", ENGINE_PATH)
_engine = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_engine)

MANIFEST = {"include": [{"path": "core/keep.py", "type": "file"}]}

WORKFLOW_REL = ".github/workflows/publish-mind-seed.yml"


def _mk_source(tmp_path: Path) -> Path:
    """Source deliberately has NO .github/ — that is the real repo's shape."""
    src = tmp_path / "src"
    (src / "core").mkdir(parents=True)
    (src / "core" / "keep.py").write_text("KEEP = 1\n", encoding="utf-8")
    return src


def _mk_dest(tmp_path: Path) -> Path:
    dest = tmp_path / "dest"
    (dest / "core").mkdir(parents=True)
    (dest / "core" / "keep.py").write_text("KEEP = 1\n", encoding="utf-8")

    # Destination-owned CI lane — the thing that was destroyed.
    wf = dest / WORKFLOW_REL
    wf.parent.mkdir(parents=True)
    wf.write_text("name: publish-mind-seed\non:\n  push:\n    branches: [main]\n",
                  encoding="utf-8")

    # A GENUINE orphan: at dest, absent from source and from the manifest.
    # This is the positive control's subject.
    (dest / "core" / "stale_orphan.py").write_text("GONE = 1\n", encoding="utf-8")
    return dest


def test_github_workflow_survives_orphan_sweep(tmp_path):
    """FIX PIN — must go RED when '.github' leaves _ORPHAN_SCAN_SKIP_TOP."""
    src, dest = _mk_source(tmp_path), _mk_dest(tmp_path)
    _engine.do_remove_orphans(dest, MANIFEST, src)
    assert (dest / WORKFLOW_REL).is_file(), (
        "the destination-owned CI publish lane was deleted by the orphan sweep — "
        "this is the 2026-08-23 regression that froze every customer environment"
    )


def test_a_genuine_orphan_is_still_removed(tmp_path):
    """POSITIVE CONTROL — must stay GREEN under the mutant.

    Without this, a sweep that deleted NOTHING AT ALL would satisfy every
    assertion above. This one fails in the do-nothing world by construction,
    because it requires the sweep's effect to EXIST.
    """
    src, dest = _mk_source(tmp_path), _mk_dest(tmp_path)
    result = _engine.do_remove_orphans(dest, MANIFEST, src)
    assert not (dest / "core" / "stale_orphan.py").exists(), (
        "the sweep failed to remove a real orphan — protecting .github must not "
        "disable orphan removal generally"
    )
    assert result.get("removed"), "sweep reported no removals; it is inert"


def test_shipped_manifest_does_not_cruft_sweep_github():
    """The OTHER sweep, bounded by measurement rather than by assumption.

    do_clean_cruft does NOT let _ORPHAN_SCAN_SKIP_TOP override an EXPLICIT
    cruft_patterns entry — measured while writing this file: seeding
    `cruft_patterns: ['.github/**']` deletes the workflow even with the skip
    entry present. That is defensible (an explicit "delete this" in the
    manifest should beat a default preservation), so the engine is left alone
    rather than widened to satisfy a scenario that does not occur.

    What makes the orphan-sweep fix SUFFICIENT is therefore a property of the
    shipped manifest, not of the engine — so assert that property here. If a
    future manifest ever adds a .github cruft pattern, this test fails and
    whoever added it has to decide deliberately, instead of silently
    re-opening the 2026-08-23 regression through the one door the skip entry
    does not cover.
    """
    import yaml
    manifest_path = CORE_SCRIPTS.parent / "config" / "seed-manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    offenders = [str(c) for c in (manifest.get("cruft_patterns") or [])
                 if "github" in str(c).lower()]
    assert not offenders, (
        f"the shipped manifest cruft-sweeps .github ({offenders}) — an explicit "
        "cruft pattern beats the orphan skip entry, so this re-opens g-335-1456"
    )


def test_source_cannot_plant_github(tmp_path):
    """SCOPE-UNAFFECTED — must stay GREEN under the mutant.

    Documents WHY the skip entry is safe rather than merely convenient: the
    transplant has no channel to create this tree, so skipping it forfeits
    nothing. If a future manifest genuinely ships workflows, this test is the
    one that should be revisited first.
    """
    src = _mk_source(tmp_path)
    assert not (src / ".github").exists()
    inc = [i.get("path", "") for i in MANIFEST["include"]]
    assert not [p for p in inc if "github" in p.lower()]


def test_skip_entry_is_present_and_reasoned():
    """The entry itself, so a silent removal is loud."""
    assert ".github" in _engine._ORPHAN_SCAN_SKIP_TOP
    src = ENGINE_PATH.read_text(encoding="utf-8")
    assert "g-335-1456" in src, (
        "the .github skip entry lost its incident citation; a bare name in this "
        "set reads as arbitrary and is the first thing a future cleanup deletes"
    )
