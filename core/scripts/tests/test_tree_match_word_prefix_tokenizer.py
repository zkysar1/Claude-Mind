"""test_tree_match_word_prefix_tokenizer.py — regression tests for tree_match.py
word_prefix tokenizer (P0 #2, knowledge-system audit, 2026-05-09).

Pre-fix, `_match_nodes` Strategy 3 (word_prefix) used `cat_lower.split("-")`
which split only on hyphens. Space-separated multi-word queries and
natural-language questions degenerated into one giant un-prefix-matchable
token, returning 0 tree nodes. The fix unifies tokenization with Strategy 4
(`re.findall(r'[a-z0-9]+')`), splitting on any non-alphanumeric separator.
The min-4-char filter is preserved to dampen stop-word noise.

These tests verify the tokenizer behavior in isolation: build a synthetic
nodes dict, run `_match_nodes`, assert the word_prefix channel hits the
expected keys.

Pure stdlib + PyYAML.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# tree_match imports `from _paths import PROJECT_ROOT`. _paths reads agent
# binding via env; suppress agent lookup so the import is harmless.
#  capture-restore pattern: stash env before module-level mutation
# so subsequent tests in the same pytest session don't inherit a popped
# MIND_AGENT. See test_applies_to_required.py for full rationale.
_ORIG_MIND_WORLD = os.environ.get("MIND_WORLD")
_ORIG_MIND_AGENT = os.environ.get("MIND_AGENT")

_TMPDIR = tempfile.mkdtemp(prefix="tree-match-tokenizer-test-")
os.environ["MIND_WORLD"] = _TMPDIR
os.environ.pop("MIND_AGENT", None)

import importlib.util  # noqa: E402

_TM_PATH = CORE_SCRIPTS / "tree_match.py"
_spec = importlib.util.spec_from_file_location("tree_match_mod", _TM_PATH)
_tm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tm)

# Restore env so downstream tests inherit clean conftest defaults.
if _ORIG_MIND_WORLD is not None:
    os.environ["MIND_WORLD"] = _ORIG_MIND_WORLD
elif "MIND_WORLD" in os.environ:
    del os.environ["MIND_WORLD"]
if _ORIG_MIND_AGENT is not None:
    os.environ["MIND_AGENT"] = _ORIG_MIND_AGENT


def _make_nodes(keys):
    """Build a minimal nodes dict matching tree.yaml shape — enough for
    _match_nodes. Each key gets a stub summary identical to the key (keeps
    substring strategy honest)."""
    return {
        k: {
            "file": f"world/knowledge/tree/{k}.md",
            "depth": 4,
            "summary": k.replace("-", " "),
            "topic": k,
            "confidence": 0.5,
        }
        for k in keys
    }


def _match(category, keys, *, entity_index=None, concept_index=None):
    """Run _match_nodes against synthetic keys, return (matched_keys, channels)."""
    nodes = _make_nodes(keys)
    matched, matched_keys, channels = _tm._match_nodes(
        category, nodes, entity_index or {}, concept_index or {}
    )
    return matched_keys, channels


# ---------------------------------------------------------------------------
# Word-prefix: hyphen-separated query (existing behavior, must not regress)
# ---------------------------------------------------------------------------

def test_hyphen_query_still_matches_hyphen_keys():
    """Pre-existing behavior: 'framework-architecture' tokenizes to
    {framework, architecture} and prefix-matches 'framework-patterns'."""
    keys = ["framework-architecture", "framework-patterns", "unrelated-thing"]
    matched, channels = _match("framework-patterns", keys)
    # framework-patterns is exact_key; framework-architecture should hit via word_prefix
    assert "framework-patterns" in matched
    assert "framework-architecture" in matched
    assert channels["framework-architecture"] in {"word_prefix", "substring"}


# ---------------------------------------------------------------------------
# Word-prefix: space-separated query (NEW — fix target)
# ---------------------------------------------------------------------------

def test_space_separated_query_tokenizes():
    """'memory consolidation' was a single token pre-fix, matching nothing
    via word_prefix. Post-fix, tokenizes to {memory, consolidation} and
    matches 'memory-lifecycle' and 'consolidation-window'."""
    keys = ["memory-lifecycle", "consolidation-window", "unrelated-thing"]
    matched, channels = _match("memory consolidation", keys)
    assert "memory-lifecycle" in matched
    assert "consolidation-window" in matched
    # Both should be word_prefix matches (no substring match — their summaries
    # are "memory lifecycle" and "consolidation window", which substring would
    # match too — but channel ordering puts substring first if both hit).
    # We only assert the keys are matched; channel could be either.


def test_natural_language_query_tokenizes():
    """'how does NPC selection work' should match nodes containing
    'selection' or 'work*' words. Pre-fix returned nothing (single token,
    too long to prefix anything)."""
    keys = ["task-selection", "selection-strategies", "workflow-engine",
            "unrelated-thing"]
    matched, channels = _match("how does NPC selection work", keys)
    # 'selection' matches both task-selection and selection-strategies
    assert "task-selection" in matched
    assert "selection-strategies" in matched
    # 'work' (4 chars) matches workflow-engine via prefix
    assert "workflow-engine" in matched
    assert "unrelated-thing" not in matched


def test_min_4_char_filter_drops_short_tokens():
    """Tokens with len < 4 are dropped — protects against stop-word noise.
    'how does NPC selection work': how(3) and NPC(3) drop; does(4),
    selection(9), work(4) survive. Therefore a key like 'npc-only' is NOT
    matched via word_prefix (npc was filtered out). Substring strategy
    still catches 'npc' independently."""
    keys = ["npc-only-thing"]
    matched, channels = _match("how does NPC selection work", keys, )
    # npc-only-thing has no token >=4 chars overlapping with our surviving
    # tokens (does, selection, work) → no word_prefix match.
    # However: substring strategy matches 'npc' (case-insensitive) appearing
    # in 'npc-only-thing'? Let's check — `cat_lower` is the full lowercased
    # query 'how does npc selection work'. substring tests if cat_lower is in
    # key/summary OR vice versa. 'how does npc...' is NOT in 'npc-only-thing'.
    # 'npc-only-thing' is NOT in 'how does npc...'. So substring doesn't fire.
    # Bidirectional substring is on the WHOLE strings, not tokens.
    assert "npc-only-thing" not in matched


def test_underscore_separator_now_tokenizes():
    """Tokenizer is non-alphanumeric-aware, not just hyphen-aware. This means
    a query like 'memory_consolidation' (underscore) tokenizes to
    {memory, consolidation} — pre-fix it was one token."""
    keys = ["memory-lifecycle"]
    matched, _ = _match("memory_consolidation", keys)
    assert "memory-lifecycle" in matched


def test_mixed_separators():
    """Real-world queries mix hyphens, spaces, capitals, and punctuation.
    Tokenizer should normalize all to lowercase alphanumeric runs."""
    keys = ["alpha-beta", "gamma-delta"]
    matched, _ = _match("ALPHA-BETA gamma/delta", keys)
    assert "alpha-beta" in matched
    assert "gamma-delta" in matched


def test_empty_query_after_filter_returns_no_word_prefix_match():
    """If all tokens are <4 chars, no word_prefix match is attempted."""
    keys = ["alpha-thing"]
    matched, channels = _match("a b c", keys)
    # No tokens survive 4-char filter, so no word_prefix match.
    # Substring also fails ("a b c" not in "alpha-thing", vice versa false too).
    assert "alpha-thing" not in matched


# ---------------------------------------------------------------------------
# Acronym handling
# ---------------------------------------------------------------------------

def test_4char_acronym_works_via_word_prefix():
    """'' (4 chars exactly) survives the filter and prefix-matches
    keys containing 'iaus*'."""
    keys = ["iaus-personality-and-drift", "unrelated-thing"]
    matched, _ = _match("IAUS", keys)
    assert "iaus-personality-and-drift" in matched


def test_3char_acronym_falls_through_to_substring_if_in_summary():
    """'NPC' (3 chars) drops from word_prefix filter. But substring strategy
    catches it independently if it appears in the key/summary/topic."""
    keys_with_summary_match = {
        "intelligent-agent": {
            "file": "world/knowledge/tree/intelligent-agent.md",
            "depth": 4,
            "summary": "deals with NPC behavior",  # 'npc' lowercase appears
            "topic": "intelligent-agent",
            "confidence": 0.5,
        },
        "unrelated-thing": {
            "file": "world/knowledge/tree/unrelated-thing.md",
            "depth": 4,
            "summary": "no match here",
            "topic": "unrelated-thing",
            "confidence": 0.5,
        },
    }
    matched, matched_keys, channels = _tm._match_nodes(
        "npc", keys_with_summary_match, {}, {}
    )
    # substring (3.0 score) catches 'npc' in summary
    assert "intelligent-agent" in matched_keys
    assert channels["intelligent-agent"] == "substring"
    assert "unrelated-thing" not in matched_keys


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

def _run_all():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failures = []
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failures.append((t.__name__, str(e) or "<no message>"))
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:
            failures.append((t.__name__, f"{type(e).__name__}: {e}"))
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(_run_all())
