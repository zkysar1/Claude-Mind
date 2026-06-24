"""test_strategy_apply_malformed_heuristics.py --  regression.

Bug (2026-06-10): strategy-apply.py threw
`AttributeError: 'str' object has no attribute 'get'` on --increment because
`aspiration-generation-strategy.yaml` had `generation_heuristics: '[]'` -- an
empty list serialized as the STRING "[]". YAML loads that as the str "[]", and
`for h in heuristics` iterates it character-by-character ('[' , ']'); each bare
str hit `keyword_match`'s `heuristic.get(...)` and crashed. The --increment is
fail-open at the call site, so the strategy->execution feedback loop
(times_applied) silently broke whenever this fired.

Fix: `_normalize_heuristics()` coerces a non-list field to [] and drops any
non-dict entries, so `load()` never hands a bare string to `keyword_match`.
This test pins that behavior.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# Bootstrap a sandbox env BEFORE importing the module (mirrors
# test_applies_to_required.py): the module-level `from _paths import META_DIR`
# resolves at import; point it at a tmpdir so import never depends on a bound
# agent, and restore env immediately after so this module does not leak
# MIND_* mutations into other tests during pytest's collection phase.
_ORIG_MIND_WORLD = os.environ.get("MIND_WORLD")
_ORIG_MIND_META = os.environ.get("MIND_META")
_ORIG_MIND_AGENT = os.environ.get("MIND_AGENT")

_TMPDIR = tempfile.mkdtemp(prefix="strategy-apply-malformed-test-")
os.environ["MIND_WORLD"] = _TMPDIR
os.environ["MIND_META"] = _TMPDIR
os.environ.pop("MIND_AGENT", None)

SA_PATH = CORE_SCRIPTS / "strategy-apply.py"
spec = importlib.util.spec_from_file_location("strategy_apply", SA_PATH)
sa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sa)

# The functions exercised here (_normalize_heuristics, keyword_match) are pure
# and do not re-read META_DIR at call time, so restoring env now is safe.
for _k, _v in (("MIND_WORLD", _ORIG_MIND_WORLD),
               ("MIND_META", _ORIG_MIND_META),
               ("MIND_AGENT", _ORIG_MIND_AGENT)):
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v


def test_string_empty_list_coerces_to_empty():
    """The exact  shape: YAML field is the str "[]"."""
    assert sa._normalize_heuristics("[]") == []


def test_iterating_string_chars_are_dropped():
    """Belt-and-suspenders: if a string somehow reaches the filter as a
    list of its chars, those bare strs are dropped (not crashed on)."""
    assert sa._normalize_heuristics(["[", "]"]) == []


def test_non_dict_entries_filtered_dicts_kept():
    good = {"id": "h1", "description": "vinheim dmarc dns"}
    raw = ["bare-string", 42, None, good, {"id": "h2"}]
    out = sa._normalize_heuristics(raw)
    assert out == [good, {"id": "h2"}]


def test_none_and_missing_coerce_to_empty():
    assert sa._normalize_heuristics(None) == []
    assert sa._normalize_heuristics({}) == []  # a dict is not a list -> []


def test_valid_list_passes_through():
    raw = [{"id": "h1", "description": "alpha"}, {"id": "h2", "description": "beta"}]
    assert sa._normalize_heuristics(raw) == raw


def test_keyword_match_never_crashes_on_normalized_output():
    """End-to-end: the original crash was keyword_match(h, tokens) with h a str.
    After normalization no bare str survives, so keyword_match is safe and
    still matches real heuristics by description tokens."""
    raw = ["[", "]", {"id": "h1", "description": "vinheim dmarc deliverability"}]
    norm = sa._normalize_heuristics(raw)
    tokens = ["dmarc"]
    # No AttributeError, and the real dict matches on its description token.
    assert any(sa.keyword_match(h, tokens) for h in norm) is True
    # A token absent from every description does not match.
    assert any(sa.keyword_match(h, ["nonexistenttoken"]) for h in norm) is False


if __name__ == "__main__":
    test_string_empty_list_coerces_to_empty()
    test_iterating_string_chars_are_dropped()
    test_non_dict_entries_filtered_dicts_kept()
    test_none_and_missing_coerce_to_empty()
    test_valid_list_passes_through()
    test_keyword_match_never_crashes_on_normalized_output()
    print("all strategy-apply malformed-heuristics regression tests passed")
