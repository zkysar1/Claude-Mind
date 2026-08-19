# domain-leak-exempt: every AYOAI_ / AyoAI literal below is a transform FIXTURE,
# never a functional identifier. This marker is load-bearing as of g-115-4443:
# the file is now in G2's excludes_paths, so it keeps its AYOAI_ literals at a
# destination, where post_copy_action A4 (seed-path-self-reference-scan.sh) greps
# for exactly those and drops only files carrying this marker. Before the
# exclusion the file had no AYOAI_ downstream and never entered that scan, so
# nothing depended on the marker; it was satisfied only INCIDENTALLY, by a
# docstring mention and by the EXEMPT fixture string. Stated explicitly so a
# reword of either cannot start tripping A4.
"""test_seed_transform_exempt_pierce.py — per-rule apply_even_if_exempt gating.

Background (2026-06-26, brand-convergence promotion):
  The `domain-leak-exempt:` marker made transform_file() skip ALL global_regex
  rules for a file. 11 framework scripts carry the marker to protect domain
  COMMENT strings (product/repo names) via G3/G4 — but the blanket skip also
  blocked G2 (AYOAI_->MIND_), a FUNCTIONAL env-var rename. The destination kept
  legacy AYOAI_AGENT reads, forcing a dual-export bridge in production.

  Fix: a global_regex rule may set `apply_even_if_exempt: true` to pierce the
  marker (G2 does). Other rules stay skipped for exempt files. Seed machinery
  whose AYOAI_ is DATA (this manifest's rule patterns, the self-ref-scan
  detection literal, seed sentinels) is held out via the rule's `excludes_paths`,
  honored by apply_global_regex -> _applies_to.

These tests pin all four quadrants (exempt x flag, exempt x excludes, non-exempt)
so the bridge-forcing regression cannot return.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

TRANSFORMS_PATH = CORE_SCRIPTS / "_seed_transforms.py"
_spec = importlib.util.spec_from_file_location("_seed_transforms", TRANSFORMS_PATH)
_t = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_t)
transform_file = _t.transform_file

_DUMMY_ROOT = CORE_SCRIPTS.parent  # only used by file_replace, which we don't exercise

# G2-shaped rule: functional env-var rename, pierces exemption, but holds out
# one seed-machinery path where AYOAI_ is protected data.
G2 = {
    "id": "G2",
    "type": "global_regex",
    "pattern": "AYOAI_",
    "replacement": "MIND_",
    "applies_to": ["**/*"],
    "apply_even_if_exempt": True,
    "excludes_paths": ["core/config/seed-manifest.yaml"],
}
# G4-shaped rule: cosmetic domain-string rewrite, must STAY skipped for exempt files.
G4 = {
    "id": "G4",
    "type": "global_regex",
    "pattern": "AyoAI",
    "replacement": "the framework",
    "applies_to": ["**/*"],
}

EXEMPT = "# domain-leak-exempt: protects AyoAI product names in comments\n"
FUNCTIONAL = 'agent = os.environ.get("AYOAI_AGENT", "")\n'
DOMAIN = "# AyoAI ships the widget\n"


def _run(rel_path, content):
    new, applied, _ = transform_file(rel_path, content, [G2, G4], _DUMMY_ROOT)
    return new, applied


# --- exempt file: G2 pierces, G4 stays skipped --------------------------------

def test_exempt_file_g2_converts_functional_envvar():
    new, applied = _run("core/scripts/capability-gate.py", EXEMPT + FUNCTIONAL)
    assert 'os.environ.get("MIND_AGENT"' in new, "G2 must rename AYOAI_AGENT in an exempt file"
    assert "AYOAI_AGENT" not in new
    assert "G2" in applied


def test_exempt_file_g4_still_skipped():
    new, applied = _run("core/scripts/capability-gate.py", EXEMPT + DOMAIN)
    assert "AyoAI ships the widget" in new, "G4 (no flag) must NOT touch domain string in exempt file"
    assert "G4" not in applied


# --- exempt seed-machinery file in excludes_paths: G2 must NOT fire ------------

def test_exempt_seed_machinery_protected_by_excludes_paths():
    content = EXEMPT + "    pattern: 'AYOAI_'\n"  # the manifest's own G2 definition
    new, applied = _run("core/config/seed-manifest.yaml", content)
    assert "pattern: 'AYOAI_'" in new, "excludes_paths must protect the manifest's own AYOAI_ rule pattern"
    assert "MIND_" not in new
    assert "G2" not in applied


# --- non-exempt file: both rules apply normally (regression guard) -------------

def test_non_exempt_file_g2_and_g4_apply():
    new, applied = _run("core/scripts/some_plain.py", FUNCTIONAL + DOMAIN)
    assert 'os.environ.get("MIND_AGENT"' in new
    assert "the framework ships the widget" in new
    assert "G2" in applied and "G4" in applied


# --- non-excluded seed-adjacent file still converts (excludes is path-specific)-

def test_excludes_paths_is_path_specific():
    # A different file (not in excludes_paths) carrying a functional read still converts.
    new, applied = _run("core/scripts/seed-path-OTHER.py", EXEMPT + FUNCTIONAL)
    assert 'os.environ.get("MIND_AGENT"' in new
    assert "G2" in applied


# --- real-manifest config pin: G2 flag + the full data-AYOAI_ exclusion set ----

def test_real_manifest_g2_config():
    """Pin the actual seed-manifest G2 rule so a removed flag/exclude can't
    silently re-introduce the dual-bridge regression OR re-muddle a data file
    (blocklist term, pedagogical example, transform rule pattern)."""
    import yaml

    manifest_path = CORE_SCRIPTS.parent / "config" / "seed-manifest.yaml"
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    g2 = next(t for t in data["transformations"] if t.get("id") == "G2")
    assert g2.get("apply_even_if_exempt") is True, "G2 must pierce domain-leak-exempt"
    excl = set(g2.get("excludes_paths", []))
    # Every file where AYOAI_ is protected DATA, not a functional identifier.
    required = {
        "core/config/seed-manifest.yaml",
        "core/scripts/seed-path-self-reference-scan.sh",
        ".claude/skills/seed/SKILL.md",
        "core/config/domain-term-blocklist.txt",
        ".claude/rules/domain-free-examples.md",
        # THIS FILE. Every AYOAI_ above is a FIXTURE describing the transform,
        # never a functional read, so it is data-AYOAI_ in exactly the sense the
        # other five are — and it was the omission (g-115-4443). See the
        # self-immunity pin below for what its absence cost.
        "core/scripts/tests/test_seed_transform_exempt_pierce.py",
    }
    missing = required - excl
    assert not missing, f"G2 excludes_paths missing data-AYOAI_ files: {missing}"


# --- self-immunity: the real manifest must not rewrite THIS file --------------

def test_this_file_is_immune_to_the_real_manifest():
    """Applying the REAL manifest to this very file must change nothing.

    THE DEFECT THIS PINS (g-115-4443, measured at hop 1 into a Claude-Mind clone
    2026-08-01, and reproduced locally): G2 rewrote this file's own AYOAI_
    fixtures, turning `assert "AYOAI_AGENT" not in new` (line 73) into
    `assert "MIND_AGENT" not in new` while line 72 asserts the same literal IS
    present. Two tests then failed at EVERY destination and passed 6/6 in dev,
    because dev is the one place the transform never runs.

    The two it broke are the ones asserting that seed machinery IS protected
    from the seed — so the mechanism that would detect this class was the
    mechanism the defect disabled. Self-concealing downstream, invisible
    upstream.

    WHY THIS PIN AND NOT A NARROWER ONE. Asserting `"...pierce.py" in
    excludes_paths` (test_real_manifest_g2_config, above) only pins G2. This
    asserts the PROPERTY — no rule, present or future, may rewrite this file —
    so a new transformation added years from now cannot silently reintroduce
    the class. It runs in dev and catches the defect in dev, which is the half
    that was missing: the previous arrangement could only fail downstream.

    Note this test is itself immune, because the file it protects is the file
    it lives in.
    """
    import yaml

    root = CORE_SCRIPTS.parent.parent
    manifest_path = CORE_SCRIPTS.parent / "config" / "seed-manifest.yaml"
    rules = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))["transformations"]

    rel = "core/scripts/tests/test_seed_transform_exempt_pierce.py"
    src = (root / rel).read_text(encoding="utf-8")
    new, applied, _ = transform_file(rel, src, rules, root)

    assert applied == [], (
        "the seed rewrites this file's own protected fixtures; rules that fired: "
        f"{applied}. Add this path to that rule's excludes_paths -- do NOT "
        "exclude the whole test tree: 171 test files read AYOAI_ as a real env "
        "var and legitimately need the rename (measured 2026-08-10)."
    )
    assert new == src

    # The exclusion makes this file keep its AYOAI_ literals at a destination,
    # where post_copy_action A4 greps for them and drops only files carrying a
    # domain-leak-exempt marker. That dependency did not exist before the
    # exclusion, so pin it rather than leave it satisfied by a docstring mention.
    assert src.lstrip().startswith("# domain-leak-exempt:"), (
        "this file must carry an explicit domain-leak-exempt marker: its "
        "excludes_paths entry keeps AYOAI_ literals at the destination, and "
        "seed-path-self-reference-scan.sh (post_copy_action A4) filters on "
        "exactly that marker"
    )
