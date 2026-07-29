"""_strip_long_form must not mutate the SHARED jsonl-cache records (g-115-3387).

WHY THIS EXISTS. `mind_api/src/jsonl_cache.py:53` warns in capitals that the
list and dicts it returns are the SHARED cache copy and must be deep-copied
before modification. `_strip_long_form` used to violate that directly --
`r["content"] = None` on those very dicts -- so ONE metadata-only retrieval
permanently nulled `content` on the daemon's cached reasoning-bank records for
every later caller, INCLUDING callers that explicitly passed `--full-content`,
until the cache reloaded on mtime/TTL.

Metadata-only is the DEFAULT, so nearly every retrieval poisoned the cache for
the entries it touched. MEASURED 2026-07-27: rb-3698 carries 1243 chars in the
store; after one default retrieval touched it, a DIFFERENT query with
`--full-content` returned content length 0. The consumers that need the lesson
text -- the code-review-protocol pre-apply consultation, encode-session dedup,
/respond -- silently received title-only entries, indistinguishable from an
entry that genuinely had no body.

The defect is invisible to any test that only inspects the RETURNED result:
stripping is the correct behaviour there. The assertion that matters is on the
INPUT records surviving the call, which is what these tests pin.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parent.parent


def _load():
    sys.path.insert(0, str(CORE_SCRIPTS))
    spec = importlib.util.spec_from_file_location("_r_strip", CORE_SCRIPTS / "retrieve.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_r = _load()


def _cached_rb():
    """A record shaped like the shared jsonl-cache dict the daemon hands out."""
    return {"id": "rb-test", "title": "t", "when_to_use": {"conditions": []},
            "content": "LESSON BODY", "description": "LONG DESCRIPTION"}


def test_strip_returns_stripped_rows():
    """The caller-visible contract is unchanged: metadata-only really strips."""
    rec = _cached_rb()
    out = _r._strip_long_form({"reasoning_bank": [rec], "meta_lessons": [],
                               "pattern_signatures": []})
    row = out["reasoning_bank"][0]
    assert row.get("content") is None, "metadata-only must drop content"
    assert row.get("description") is None, "metadata-only must drop description"
    # Discriminative fields the LLM triages on must survive.
    assert row.get("title") == "t"
    assert row.get("when_to_use") is not None


def test_strip_does_not_mutate_the_shared_record():
    """THE regression: the input record must be untouched after stripping."""
    rec = _cached_rb()
    _r._strip_long_form({"reasoning_bank": [rec], "meta_lessons": [],
                         "pattern_signatures": []})
    assert rec["content"] == "LESSON BODY", (
        "_strip_long_form mutated the SHARED jsonl-cache record — one "
        "metadata-only retrieval now nulls content for every later caller, "
        "including --full-content ones (g-115-3387)")
    assert rec["description"] == "LONG DESCRIPTION", (
        "_strip_long_form nulled description on the SHARED cache record")


def test_strip_does_not_mutate_shared_meta_lessons():
    """meta_lessons travels the same code path and the same shared cache."""
    rec = {"id": "ml-1", "content": "META BODY", "description": "MD"}
    _r._strip_long_form({"reasoning_bank": [], "meta_lessons": [rec],
                         "pattern_signatures": []})
    assert rec["content"] == "META BODY"
    assert rec["description"] == "MD"


def test_strip_does_not_mutate_shared_pattern_signature():
    """Signature descriptions are TRUNCATED rather than nulled — same hazard."""
    rec = {"id": "sig-1", "description": "x" * 400}
    out = _r._strip_long_form({"reasoning_bank": [], "meta_lessons": [],
                               "pattern_signatures": [rec]})
    assert len(out["pattern_signatures"][0]["description"]) == 241, "241 = 240 + ellipsis"
    assert len(rec["description"]) == 400, (
        "_strip_long_form truncated the SHARED cache signature record")


def test_repeated_strip_is_stable():
    """Two retrievals over the same cache rows must both see full input.

    Directly models the live failure: call 1 is a default (metadata-only)
    retrieval, call 2 asks for the body. Before the fix, call 2 saw None.
    """
    rec = _cached_rb()
    shared_view = [rec]
    _r._strip_long_form({"reasoning_bank": shared_view, "meta_lessons": [],
                         "pattern_signatures": []})
    # Second caller reads the same shared rows and wants the full body.
    assert shared_view[0]["content"] == "LESSON BODY", (
        "second retrieval saw a nulled body — the cache was poisoned by the first")


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
