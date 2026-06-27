"""test_seed_transform_exempt_pierce.py — per-rule apply_even_if_exempt gating.

Background (2026-06-26, brand-convergence promotion):
  The `domain-leak-exempt:` marker made transform_file() skip ALL global_regex
  rules for a file. 11 framework scripts carry the marker to protect domain
  COMMENT strings (product/repo names) via G3/G4 — but the blanket skip also
  blocked G2 (MIND_->MIND_), a FUNCTIONAL env-var rename. The destination kept
  legacy MIND_AGENT reads, forcing a dual-export bridge in production.

  Fix: a global_regex rule may set `apply_even_if_exempt: true` to pierce the
  marker (G2 does). Other rules stay skipped for exempt files. Seed machinery
  whose MIND_ is DATA (this manifest's rule patterns, the self-ref-scan
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
# one seed-machinery path where MIND_ is protected data.
G2 = {
    "id": "G2",
    "type": "global_regex",
    "pattern": "MIND_",
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
FUNCTIONAL = 'agent = os.environ.get("MIND_AGENT", "")\n'
DOMAIN = "# AyoAI ships the widget\n"


def _run(rel_path, content):
    new, applied, _ = transform_file(rel_path, content, [G2, G4], _DUMMY_ROOT)
    return new, applied


# --- exempt file: G2 pierces, G4 stays skipped --------------------------------

def test_exempt_file_g2_converts_functional_envvar():
    new, applied = _run("core/scripts/capability-gate.py", EXEMPT + FUNCTIONAL)
    assert 'os.environ.get("MIND_AGENT"' in new, "G2 must rename MIND_AGENT in an exempt file"
    assert "MIND_AGENT" not in new
    assert "G2" in applied


def test_exempt_file_g4_still_skipped():
    new, applied = _run("core/scripts/capability-gate.py", EXEMPT + DOMAIN)
    assert "AyoAI ships the widget" in new, "G4 (no flag) must NOT touch domain string in exempt file"
    assert "G4" not in applied


# --- exempt seed-machinery file in excludes_paths: G2 must NOT fire ------------

def test_exempt_seed_machinery_protected_by_excludes_paths():
    content = EXEMPT + "    pattern: 'MIND_'\n"  # the manifest's own G2 definition
    new, applied = _run("core/config/seed-manifest.yaml", content)
    assert "pattern: 'MIND_'" in new, "excludes_paths must protect the manifest's own MIND_ rule pattern"
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


# --- real-manifest config pin: G2 flag + the full data-MIND_ exclusion set ----

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
    # Every file where MIND_ is protected DATA, not a functional identifier.
    required = {
        "core/config/seed-manifest.yaml",
        "core/scripts/seed-path-self-reference-scan.sh",
        ".claude/skills/seed/SKILL.md",
        "core/config/domain-term-blocklist.txt",
        ".claude/rules/domain-free-examples.md",
    }
    missing = required - excl
    assert not missing, f"G2 excludes_paths missing data-MIND_ files: {missing}"
