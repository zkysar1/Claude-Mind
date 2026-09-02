"""test_identity_key_immutability.py — every merge-identity field is write-protected ().

coordination_merge.py identifies a record across boxes by a CONTENT key, not by its
volatile id. If any field in that key can be written through the generic
POST /v1/store/set-field endpoint, an in-place edit changes the record's identity,
and the next cross-box merge reads the edited copy as a NEW entity and keeps BOTH.

THAT IS NOT HYPOTHETICAL. rb-5511 measured **11 forked pairs in the live guardrail
store** from in-place `rule` edits, with `displaced_from` stamps matching the fork
pairs exactly. Before g-115-8396 the registry declared immutable_fields on three
stores and all three declared only {"created"} — the SCRIPT-STAMPED half. Every
human-authored half (rule / title / name) was writable, and spark-questions, whose
identity is the SOLE field `text`, declared no immutable_fields at all.

WHY THIS TEST AND NOT JUST THE REGISTRY CHANGE. The hole is opened by editing
EITHER file: adding a field to an identity function, or dropping one from
immutable_fields. Nothing links the two, they live in different trees
(core/scripts/ vs mind_api/src/), and the failure is INVISIBLE on one box — a fork
needs two boxes merging, so a hermetic suite cannot reproduce it and nothing fails
when the protection lapses. Same "hand-kept mirror pinned by a test" shape as
test_live_phase_entry_type_sync.py.

DELIBERATELY NOT COVERED: _goal_identity. It takes `alloc_nonce` in PRECEDENCE over
(created_at, title), and a nonce cannot be meaningfully amended, so the identity
survives a title edit by construction. That is the pattern the other four should
converge on; it needs no write-protection and asserting one here would be wrong.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
MERGE_PY = CORE_SCRIPTS / "coordination_merge.py"

for p in (str(CORE_SCRIPTS), str(PROJECT_ROOT / "mind_api" / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from store_registry import STORE_REGISTRY  # noqa: E402

# identity function -> the store whose records it identifies
IDENTITY_TO_STORE = {
    "_rb_identity": "reasoning-bank",
    "_guard_identity": "guardrails",
    "_sig_identity": "pattern-signatures",
    "_spark_identity": "spark-questions",
}


def _identity_fields(fn_name: str) -> set[str]:
    """Field names the named identity function reads out of the record.

    Parsed from source rather than imported: coordination_merge.py is a large
    module whose import pulls in the merge machinery, and only the field NAMES
    are needed here.
    """
    src = MERGE_PY.read_text(encoding="utf-8")
    m = re.search(r"^def %s\(.*?(?=^def |\Z)" % re.escape(fn_name), src, re.M | re.S)
    assert m, "identity function %s not found in %s" % (fn_name, MERGE_PY.name)
    return set(re.findall(r'rec\.get\(\s*"([a-z_]+)"', m.group(0)))


def test_parser_actually_matched_fields():
    """POSITIVE CONTROL (guard-2298). Every assertion below is of the form
    "each parsed field is protected", which passes VACUOUSLY if the parse
    returns nothing. If the identity functions are ever rewritten to a shape
    this regex does not match, this test fails instead of the file going green
    while measuring nothing."""
    per_fn = {fn: _identity_fields(fn) for fn in IDENTITY_TO_STORE}
    for fn, fields in per_fn.items():
        assert fields, (
            "parsed ZERO fields out of %s — the identity function's shape changed "
            "and this file is now measuring nothing. Update the parser." % fn)
    total = sum(len(f) for f in per_fn.values())
    assert total >= 7, (
        "expected at least 7 identity fields across the four functions "
        "(2 rb + 2 guard + 2 sig + 1 spark), parsed %d: %r" % (total, per_fn))


@pytest.mark.parametrize("fn_name,store", sorted(IDENTITY_TO_STORE.items()))
def test_every_identity_field_is_immutable(fn_name, store):
    """THE pin: no field in a merge-identity key may be writable via set-field."""
    spec = STORE_REGISTRY[store]
    fields = _identity_fields(fn_name)
    unprotected = sorted(fields - set(spec.immutable_fields))
    assert not unprotected, (
        "%s keys record identity on %s, but %r is NOT in the '%s' store's "
        "immutable_fields (%s). POST /v1/store/set-field would accept a write to "
        "it, changing the record's identity in place — the next cross-box merge "
        "then reads the edited copy as a NEW record and keeps BOTH (rb-5511: 11 "
        "forked pairs measured live). Add it to immutable_fields, or give the "
        "identity an alloc_nonce-style stable key the way _goal_identity does."
        % (fn_name, sorted(fields), unprotected, store, sorted(spec.immutable_fields)))


def test_spark_questions_has_any_protection_at_all():
    """spark-questions was the worst case and is called out separately.

    Its identity is the SOLE field `text`, so an unprotected `text` left the store
    with no protected half whatsoever — not merely a weak key, but no key. A
    generic parametrized pass would report this as one failure among four; it is
    worth its own name so a regression reads as what it is.
    """
    spec = STORE_REGISTRY["spark-questions"]
    assert spec.immutable_fields, (
        "spark-questions declares NO immutable_fields. Its identity is the single "
        "field `text`, so nothing about a spark record is write-protected.")
    assert "text" in spec.immutable_fields


def test_goal_identity_is_deliberately_excluded():
    """Documents the exemption so a future reader does not 'fix' it.

    _goal_identity prefers alloc_nonce over (created_at, title), so a title edit
    cannot change the identity. Asserting title-immutability on goals would break
    aspirations-update-goal.sh, which legitimately edits titles.
    """
    src = MERGE_PY.read_text(encoding="utf-8")
    m = re.search(r"^def _goal_identity\(.*?(?=^def |\Z)", src, re.M | re.S)
    assert m, "_goal_identity not found"
    assert "alloc_nonce" in m.group(0), (
        "_goal_identity no longer mentions alloc_nonce. If it now keys on "
        "(created_at, title) alone, `title` became an identity field on a store "
        "whose titles are edited routinely — re-derive this exemption.")
    assert "_goal_identity" not in IDENTITY_TO_STORE
