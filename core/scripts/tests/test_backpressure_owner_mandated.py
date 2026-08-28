"""Regression pins for owner_mandated_fields ().

DEFECT (measured 2026-08-28, live meta-log). weights.directive_boost was raised
1.5 -> 3.0 TWICE on explicit owner instruction and silently auto-reverted both
times by imp@k backpressure:

    mc-946  1.5 -> 3.0  owner directive 2026-08-25 (asp-368 revenue)
    mc-951  3.0 -> 1.5  BACKPRESSURE ROLLBACK: 5 consecutive below baseline
    mc-964  1.5 -> 3.0  THE VINHEIM RALLY (owner)
    mc-965  3.0 -> 1.5  BACKPRESSURE ROLLBACK

Two agents, two owner statements, both undone by an automatic mechanism with
no notification.

WHY NOT audit_only_fields. That allowlist's stated admission criterion is that
rolling the field back "loses evidence WITHOUT changing behavior". An owner-
mandated tunable is the inverse: changing selection behavior is precisely why
it was set. Admitting it there would falsify that block's own criterion for
every future reader, so the protection is a SECOND allowlist keyed on
PROVENANCE, sharing the matcher but not the meaning.

WHAT THESE TESTS PIN, and why each would otherwise regress silently:

1. SPLIT-BRAIN. mind_api/src/meta/meta_backpressure.py carries its OWN copy of
   the gate. A single-file fix refuses the revert on the CLI path and silently
   applies it via the daemon -- green tests, unprotected production. Pinned by
   asserting the branch exists in BOTH sources.
2. guard-130. The three mirrored helpers must stay byte-identical across the
   layer boundary (the files may not import each other -- core/BOUNDARY.md).
   This change satisfies that by NOT touching them; the pin keeps it that way.
3. DISTINCTNESS. The refusal must not be collapsed into audit_only_skips, or a
   reader cannot tell WHICH refusal fired.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest
import yaml

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO = SCRIPTS_DIR.parent.parent
SCRIPT = SCRIPTS_DIR / "meta-backpressure.py"
MIRROR = REPO / "mind_api" / "src" / "meta" / "meta_backpressure.py"
META_YAML = REPO / "core" / "config" / "meta.yaml"


def _import():
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location("meta_backpressure_omf", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["meta_backpressure_omf"] = mod
    spec.loader.exec_module(mod)
    return mod


MOD = _import()
CLI_SRC = SCRIPT.read_text(encoding="utf-8")
API_SRC = MIRROR.read_text(encoding="utf-8")
BOTH = pytest.mark.parametrize("src,name", [(CLI_SRC, "core/scripts"), (API_SRC, "mind_api/src")])


def _owner_allowlist():
    """The live allowlist, read from config -- not restated here."""
    doc = yaml.safe_load(META_YAML.read_text(encoding="utf-8"))

    def find(node, key):
        if isinstance(node, dict):
            if key in node:
                return node[key]
            for v in node.values():
                got = find(v, key)
                if got is not None:
                    return got
        return None

    return find(doc, "owner_mandated_fields")


# ── config ───────────────────────────────────────────────────────────────────

def test_config_declares_the_owner_mandated_allowlist():
    got = _owner_allowlist()
    assert isinstance(got, dict) and got, "owner_mandated_fields missing from meta.yaml"
    assert "weights.directive_boost" in got.get("goal-selection-strategy.yaml", []), (
        "the field that was reverted twice (mc-951, mc-965) is not protected")


def test_the_two_allowlists_stay_disjoint_in_meaning():
    """directive_boost must NOT be smuggled into audit_only_fields.

    That block admits only fields whose rollback loses evidence without
    changing behavior. directive_boost changes behavior by design.
    """
    doc = yaml.safe_load(META_YAML.read_text(encoding="utf-8"))

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "audit_only_fields" and isinstance(v, dict):
                    for fields in v.values():
                        for f in (fields or []):
                            assert "directive_boost" not in str(f), (
                                "directive_boost is in audit_only_fields, which falsifies "
                                "that block's own admission criterion (g-115-8149)")
                walk(v)

    walk(doc)


# ── predicate ────────────────────────────────────────────────────────────────

def test_the_protected_field_matches_the_shared_matcher():
    allow = _owner_allowlist()["goal-selection-strategy.yaml"]
    assert MOD._is_audit_only_field("weights.directive_boost", allow) is True


def test_sibling_weights_stay_rollback_eligible():
    """Protection is per-field, not a blanket exemption for weights.*."""
    allow = _owner_allowlist()["goal-selection-strategy.yaml"]
    for other in ("weights.opportunity_boost", "weights.handoff_bonus", "weights"):
        assert MOD._is_audit_only_field(other, allow) is False, (
            f"{other} was exempted; only the owner-mandated field may be")


# ── the split-brain pin (1) ──────────────────────────────────────────────────

@BOTH
def test_both_paths_load_the_allowlist(src, name):
    assert 'config.get("owner_mandated_fields"' in src, (
        f"{name} never reads owner_mandated_fields -- that path silently reverts "
        f"owner-mandated values while the other refuses (split-brain)")


@BOTH
def test_both_paths_refuse_and_record_distinctly(src, name):
    assert '"owner_mandated_skipped"' in src, f"{name} lacks the refusal status"
    assert '"owner_mandated_skips"' in src, (
        f"{name} does not record the refusal under its own key -- collapsing it into "
        f"audit_only_skips hides which refusal fired")


@BOTH
def test_the_refusal_precedes_the_rollback_branch(src, name):
    """Order matters: the check must gate the rollback, not follow it."""
    refusal = src.index('"owner_mandated_skipped"')
    rollback = src.index('"rolled_back"', refusal - 4000 if refusal > 4000 else 0)
    assert refusal < src.index('"rolled_back"', refusal), (
        f"{name}: the owner-mandated refusal does not precede a rollback assignment")


# ── guard-130 (2) ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("fn", [
    "_is_audit_only_field", "_audit_allowlist_basename", "_audit_allowlist_for"])
def test_mirrored_helpers_remain_identical_across_the_boundary(fn):
    """guard-130: the two files may not import each other, so drift is silent.

    This change reuses the helpers rather than editing them -- they already take
    the allowlist as a parameter. That is what keeps the mirror satisfied, and
    what makes the new allowlist inherit prefix+boundary matching (g-115-4552)
    and basename normalization (g-115-6413) instead of re-deriving them.
    """
    def body(src):
        m = re.search(rf"\ndef {fn}\(.*?(?=\ndef |\Z)", src, re.S)
        assert m, f"{fn} not found"
        # Strip type annotations and comments/docstrings-insensitive whitespace.
        text = m.group(0)
        text = re.sub(r"#.*", "", text)
        text = re.sub(r'""".*?"""', "", text, flags=re.S)
        text = re.sub(r"->\s*[\w\[\]]+\s*:", ":", text)
        text = re.sub(r"\s+", " ", text)
        # The mirror is type-annotated and the CLI copy is not -- a SANCTIONED
        # difference. Collapse the whitespace the annotation strip leaves behind
        # (") :" vs "):"), or this pin reports drift that does not exist.
        text = re.sub(r"\s+([:,)])", r"\1", text)
        return text.strip()

    assert body(CLI_SRC) == body(API_SRC), (
        f"{fn} has DRIFTED between core/scripts and mind_api/src (guard-130)")
