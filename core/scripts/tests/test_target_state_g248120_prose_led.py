"""test_target_state_g248120_prose_led.py — PAST-TENSE completed-work verbs +
prose-led catalog additions (g-248-120, target_state prose-led residual).

Background:
  After g-248-119 shipped the class-2/3a/3b DEMOTE detectors, a ledger analysis
  of world/goal-duplication-overrides.jsonl found the target_state SOLO residual
  (31 cases where NO detector fires -> still HARD-blocks as a false positive)
  concentrated in a PROSE-LED tail: the title leads with a plain English word
  that is neither a catalogued intent verb nor a code identifier. Two sub-patterns:

  (1) PAST-TENSE modify verbs the present-tense sets miss — canonically
      "Maintain: Replaced productivity-stop-gate encoding_ratio ..." ('replace'
      is catalogued in MODIFY_INTENT_VERBS, 'replaced' is not).
  (2) UNCATALOGUED action verbs — "Switch utilization-gate.sh to call ...",
      "Idea: Document mode-name divergence between session.py ... and ...".

Fix (surgical, DEMOTE-only):
  - A verb-aware past-tense stemmer `_past_tense_base` maps a past-tense form to
    its base ONLY WHEN that base is a known intent verb (_ALL_INTENT_VERBS),
    consumed by is_modify_intent + is_run_intent (both DEMOTE) via
    `_matches_intent_verb`. It can NEVER fabricate a verb from a non-verb.
  - 'switch' + 'document' cataloged into MODIFY_INTENT_VERBS (both NAME an
    existing subject present pre- and post-work -> DEMOTE). Routed to MODIFY
    (DEMOTE), not is_read (SKIP), so a "Document <doc-that-exists>" dup keeps a
    visible advisory instead of being fully exempted.

CRITICAL adversarial-negative invariant (regression guard): a non-verb whose
-ed/-d strip coincidentally resembles a stem must NOT become a verb. The stemmer
gates every candidate base on membership in _ALL_INTENT_VERBS, so 'embedded',
'need', 'speed', 'proceed', 'field', 'embed-ded' all stay unchanged. Measured
effect: SOLO residual 31 -> 28, ANY residual 152 -> 143, no previously-covered
case lost (coverage strictly increases).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

TS_PATH = CORE_SCRIPTS / "_target_state.py"
spec = importlib.util.spec_from_file_location("_target_state", TS_PATH)
ts_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ts_mod)


# ── _past_tense_base positive controls (base IS a known intent verb) ──────────

def test_past_tense_strip_d_base_ends_in_e():
    # base ends in 'e' -> strip only 'd'
    assert ts_mod._past_tense_base("replaced") == "replace"
    assert ts_mod._past_tense_base("moved") == "move"
    assert ts_mod._past_tense_base("tuned") == "tune"
    assert ts_mod._past_tense_base("restored") == "restore"
    assert ts_mod._past_tense_base("normalized") == "normalize"


def test_past_tense_strip_ed_base_ends_in_consonant():
    assert ts_mod._past_tense_base("fixed") == "fix"
    assert ts_mod._past_tense_base("hardened") == "harden"
    assert ts_mod._past_tense_base("bumped") == "bump"
    assert ts_mod._past_tense_base("instrumented") == "instrument"
    assert ts_mod._past_tense_base("backfilled") == "backfill"


def test_past_tense_doubled_final_consonant():
    assert ts_mod._past_tense_base("flipped") == "flip"
    # 'dropped' -> 'drop' but 'drop' is in REMOVAL, not MODIFY; still a valid stem
    assert ts_mod._past_tense_base("dropped") == "drop"


def test_past_tense_new_catalog_verbs():
    assert ts_mod._past_tense_base("switched") == "switch"
    assert ts_mod._past_tense_base("documented") == "document"


# ── _past_tense_base adversarial negatives (non-verb -> UNCHANGED) ────────────

def test_past_tense_non_verbs_unchanged():
    for w in ["embedded", "need", "speed", "proceed", "exceed", "succeed",
              "field", "shield", "wield", "embed-ded", "shred", "misled",
              "yield", "unyield"]:
        assert ts_mod._past_tense_base(w) == w, w


def test_past_tense_already_base_verb_unchanged():
    # a present-tense verb already in a set is returned as-is (no over-stemming)
    assert ts_mod._past_tense_base("replace") == "replace"
    assert ts_mod._past_tense_base("add") == "add"
    assert ts_mod._past_tense_base("build") == "build"


def test_past_tense_empty_and_non_d():
    assert ts_mod._past_tense_base("") == ""
    assert ts_mod._past_tense_base("refactor") == "refactor"  # ends in 'r'


def test_create_verb_past_tense_not_in_modify():
    # 'added'->'add', 'created'->'create': valid stems, but NOT in MODIFY, so a
    # completed CREATE record does not false-demote via is_modify_intent.
    assert ts_mod._past_tense_base("added") not in ts_mod.MODIFY_INTENT_VERBS
    assert ts_mod._past_tense_base("created") not in ts_mod.MODIFY_INTENT_VERBS


# ── The 3 named residual cases NOW covered (is_modify_intent demotes) ─────────

def test_replaced_past_tense_modify():
    assert ts_mod.is_modify_intent(
        "Maintain: Replaced productivity-stop-gate encoding_ratio (gap-counter -> session-total)"
    ) is True


def test_switch_cataloged_modify():
    assert ts_mod.is_modify_intent(
        "Switch utilization-gate.sh to call utilization-feedback.py --infer (RB times_helpful)"
    ) is True


def test_document_cataloged_modify():
    assert ts_mod.is_modify_intent(
        "Idea: Document mode-name divergence between session.py VALID_MODES and skill-structure-gate.py"
    ) is True


def test_document_routed_to_modify_not_read():
    # DEMOTE-not-SKIP invariant: 'document' must NOT be read-intent (which would
    # fully exempt a "Document <doc-that-exists>" dup).
    assert ts_mod.is_read_intent("Idea: Document mode-name divergence X and Y") is False


# ── Detector-level adversarial negatives (no false demote / no false skip) ────

def test_embed_ded_config_not_modify():
    # 'add' pre-colon (not modify), 'embed-ded' post-colon (not a verb).
    assert ts_mod.is_modify_intent("Add: embed-ded config") is False


def test_create_dup_stays_blockable():
    # genuine create with no integration prep -> neither modify nor add-to-surface
    assert ts_mod.is_modify_intent("Add: new feature flag") is False
    assert ts_mod.is_add_to_surface_intent("Add: new feature flag") is False


def test_non_verb_ed_leading_word_not_modify():
    assert ts_mod.is_modify_intent("Idea: speed up retrieval") is False
    assert ts_mod.is_modify_intent("Idea: embedded config loader") is False


# ── Regressions: present-tense unchanged; run past-tense now covered ──────────

def test_present_tense_modify_unchanged():
    assert ts_mod.is_modify_intent("Fix: the retry logic") is True
    assert ts_mod.is_modify_intent("Apply: refactor goal-selector.py") is True


def test_run_present_and_past_tense():
    assert ts_mod.is_run_intent("Apply: run provision_aws.py") is True
    assert ts_mod.is_run_intent("Maintain: Executed provision_aws.py against staging") is True


def test_read_removal_not_stemmed():
    # is_read / is_removal deliberately NOT wired to the stemmer (SKIP = higher
    # stakes, zero past-tense residual). A leading past-tense read/removal verb
    # stays a noop — if the stemmer WERE wired, 'investigated'->'investigate' and
    # 'removed'->'remove' would both flip these True.
    assert ts_mod.is_read_intent("Investigated: the retry path") is False
    assert ts_mod.is_removal_intent("Maintain: Removed the dead flag") is False
