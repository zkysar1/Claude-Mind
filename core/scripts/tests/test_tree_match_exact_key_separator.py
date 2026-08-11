"""test_tree_match_exact_key_separator.py — regression tests for the exact_key
channel being separator-sensitive (g-306-182, 2026-08-04).

Pre-fix, `_match_nodes` Strategy 1 gated the exact_key channel
(CHANNEL_SCORES 4.0) on LITERAL case-folded equality:

    if key_lower == cat_lower:

Separators were not normalized, so the natural-language spelling of a node key
could not reach the channel and fell through to word_prefix (1.5) / concept
(2.0). Measured on cc-04 (Linux 6.8.0-136-generic) against the live tree:

    --text "test-coverage-illusions" --top 5  -> that node rank #1, score 6.80
    --text "test coverage illusions" --top 25 -> that node rank #7, score 4.30

Same identifier, ~2.5 points and 6 rank positions apart. Measured at --top 25
on a 25-match corpus, which is the MMR no-op path (_mmr_rerank returns its input
unchanged when len(scored) <= limit), so this
is a pure relevance ranking and MMR does not explain it.

Why it is a gap and not intended weighting: line 275 is unchanged since the
original 2026-03-16 commit and was never revisited, whereas Strategy 3 was
deliberately made separator-agnostic on 2026-05-09 (P0 #2) — the codebase
settled the separator question once, in favour of separator-independence.
Natural-language is the query shape the framework MANDATES
(.claude/rules/code-review-protocol.md step 4 requires two free-text queries).

The negative controls matter as much as the positive cases: this pin must NOT
be satisfiable by making the exact_key channel fire more loosely. A query that
merely shares tokens with a key is NOT an exact key match.

Pure stdlib + PyYAML. Mirrors test_tree_match_word_prefix_tokenizer.py.
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
#  capture-restore pattern: stash env before module-level mutation so
# subsequent tests in the same pytest session don't inherit a popped
# MIND_AGENT.
_ORIG_MIND_WORLD = os.environ.get("MIND_WORLD")
_ORIG_MIND_AGENT = os.environ.get("MIND_AGENT")

_TMPDIR = tempfile.mkdtemp(prefix="tree-match-exactkey-test-")
os.environ["MIND_WORLD"] = _TMPDIR
os.environ.pop("MIND_AGENT", None)

import importlib.util  # noqa: E402

_TM_PATH = CORE_SCRIPTS / "tree_match.py"
_spec = importlib.util.spec_from_file_location("tree_match_exactkey_mod", _TM_PATH)
_tm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tm)

if _ORIG_MIND_WORLD is not None:
    os.environ["MIND_WORLD"] = _ORIG_MIND_WORLD
elif "MIND_WORLD" in os.environ:
    del os.environ["MIND_WORLD"]
if _ORIG_MIND_AGENT is not None:
    os.environ["MIND_AGENT"] = _ORIG_MIND_AGENT


def _make_nodes(keys):
    """Minimal nodes dict matching tree.yaml shape. Summaries are deliberately
    NOT the de-hyphenated key here — that would let the substring channel fire
    on the space-separated form and mask which channel actually matched."""
    return {
        k: {
            "file": f"world/knowledge/tree/{k}.md",
            "depth": 4,
            "summary": f"stub summary for node number {i}",
            "topic": k,
            "confidence": 0.5,
        }
        for i, k in enumerate(keys)
    }


def _channels(category, keys):
    _matched, _matched_keys, channels = _tm._match_nodes(
        category, _make_nodes(keys), {}, {}
    )
    return channels


KEYS = ["test-coverage-illusions", "test-coverage-and-velocity", "unrelated-thing"]


# ---------------------------------------------------------------------------
# Positive: a node's own key, in every separator form, earns exact_key
# ---------------------------------------------------------------------------

def test_hyphenated_form_earns_exact_key():
    """Pre-existing behavior, must not regress."""
    assert _channels("test-coverage-illusions", KEYS).get(
        "test-coverage-illusions") == "exact_key"


def test_space_separated_form_earns_exact_key():
    """THE FIX TARGET. 'test coverage illusions' denotes the same identifier as
    'test-coverage-illusions' under the framework's kebab-case key convention,
    and natural language is the mandated query shape. Pre-fix this returned
    word_prefix/concept, costing the 4.0 channel score."""
    assert _channels("test coverage illusions", KEYS).get(
        "test-coverage-illusions") == "exact_key"


def test_underscore_form_earns_exact_key():
    """Separator-independence is about ANY non-alphanumeric run, matching the
    tokenizer Strategy 3 already uses."""
    assert _channels("test_coverage_illusions", KEYS).get(
        "test-coverage-illusions") == "exact_key"


def test_mixed_case_and_separator_form_earns_exact_key():
    assert _channels("Test Coverage Illusions", KEYS).get(
        "test-coverage-illusions") == "exact_key"


# ---------------------------------------------------------------------------
# Negative controls — the pin must not be satisfiable by loosening the channel
# ---------------------------------------------------------------------------

def test_token_subset_does_not_earn_exact_key():
    """'test coverage' shares tokens with the key but is NOT that key. It must
    not be promoted to exact_key — otherwise this pin could be passed by making
    exact_key fire on any overlap, which would destroy the ranking it exists to
    provide."""
    assert _channels("test coverage", KEYS).get(
        "test-coverage-illusions") != "exact_key"


def test_token_superset_does_not_earn_exact_key():
    """Extra tokens mean a different query, not the key."""
    assert _channels("test coverage illusions and more", KEYS).get(
        "test-coverage-illusions") != "exact_key"


def test_reordered_tokens_do_not_earn_exact_key():
    """A key is an ordered identifier. 'illusions coverage test' is not it —
    separator-independence must not become order-independence."""
    assert _channels("illusions coverage test", KEYS).get(
        "test-coverage-illusions") != "exact_key"


def test_sibling_key_does_not_earn_exact_key():
    """The near-neighbour that outranked the target in the live measurement
    must not itself become an exact match for the target's query."""
    assert _channels("test coverage illusions", KEYS).get(
        "test-coverage-and-velocity") != "exact_key"


def test_unrelated_query_matches_nothing_as_exact_key():
    ch = _channels("completely different topic", KEYS)
    assert "exact_key" not in ch.values()


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
