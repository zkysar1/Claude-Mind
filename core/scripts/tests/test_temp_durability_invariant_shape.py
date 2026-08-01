"""Pins predicate_shape() in checks/temp_durability_invariant.py ().

WHY THIS EXISTS. That checker derives "which files the purge lane covers" by
parsing temp-drain-purge.sh's PURGE_FIND_PRED assignment. g-306-111 inverted
that lane from an allow-list to purge-by-default-with-exemptions while KEEPING
the same `-name '*.md'` / `-name '*.json'` tokens — they simply stopped meaning
"purged" and started meaning "exempt". A parser reading tokens without the
polarity of the expression around them therefore flips its verdict silently:
every .jsonl/.yaml/.tsv would be reported as lifecycle-less at the exact moment
it gained a lifecycle, and .md/.json would be credited to a lane they are exempt
from. Nothing raises; the regex still matches and the output is still confident.

predicate_shape() is the defense: it returns an explicit discriminator so a
future polarity change forces a NEW branch instead of silently re-interpreting
the old one. An untested defense is the presence-only-verification anti-pattern,
so the discriminator is pinned here against BOTH real shapes — a test that only
saw today's shape would pass forever without ever exercising the distinction it
exists to make.

Guardrail: guard-2190. Strategy: rb-6174.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
CHECK_PATH = CORE_SCRIPTS / "checks" / "temp_durability_invariant.py"

# The two forms verbatim: the CURRENT inverted predicate and the PRE-inversion
# allow-list, both copied from temp-drain-purge.sh (_purge_find_predicate and
# _purge_find_predicate_legacy respectively).
INVERTED = (
    "  PURGE_FIND_PRED=( -maxdepth 1 -type f ! -name '.*' "
    "\\( ! \\( -name '*.md' -o -name '*.json' \\) -o -empty \\) )\n"
)
ALLOWLIST = (
    "  PURGE_FIND_PRED=( -maxdepth 1 -type f ! -name '.*' "
    "\\( \\( -name '*.log' -o -name '*.txt' -o -name '*.py' -o -name '*.sh' "
    "-o -name '*.err' -o -name '*.raw' -o -name '*.out' -o -name '*.bak' \\) "
    "-o -empty \\) -mmin \"+$age_min\" )\n"
)


def _load(monkeypatch, purge_text=None, purge_exists=True, tmp_path=None):
    """Import the checker fresh with PURGE pointed at a synthetic script."""
    spec = importlib.util.spec_from_file_location("tdi_shape", CHECK_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tdi_shape"] = mod
    spec.loader.exec_module(mod)
    target = tmp_path / "temp-drain-purge.sh"
    if purge_exists:
        target.write_text(purge_text or "", encoding="utf-8")
    monkeypatch.setattr(mod, "PURGE", target)
    return mod


def test_detects_the_inverted_predicate(monkeypatch, tmp_path):
    mod = _load(monkeypatch, INVERTED, tmp_path=tmp_path)
    shape, exts = mod.predicate_shape()
    assert shape == "inverted"
    # exts MUST be None, not {'.md', '.json'}: under the inversion those two
    # name what is EXEMPT. Returning them as a coverage set is precisely the
    # bug — it would credit .md/.json to the purge lane.
    assert exts is None


def test_detects_the_pre_inversion_allowlist(monkeypatch, tmp_path):
    """The legacy shape must still parse to its 8 covered extensions — the
    checker has to keep working against a lane that reverts or a deployment
    still on the old predicate."""
    mod = _load(monkeypatch, ALLOWLIST, tmp_path=tmp_path)
    shape, exts = mod.predicate_shape()
    assert shape == "allowlist"
    assert exts == {".log", ".txt", ".py", ".sh", ".err", ".raw", ".out", ".bak"}


def test_the_two_shapes_do_not_collide(monkeypatch, tmp_path):
    """The discriminating assertion. Both forms contain `-name '*.<ext>'`
    tokens and both are a PURGE_FIND_PRED assignment, so a token-only parser
    returns the SAME kind of answer for both. These must differ."""
    inv = _load(monkeypatch, INVERTED, tmp_path=tmp_path).predicate_shape()
    allow = _load(monkeypatch, ALLOWLIST, tmp_path=tmp_path).predicate_shape()
    assert inv[0] != allow[0]


def test_unparseable_and_missing_both_yield_no_shape(monkeypatch, tmp_path):
    """No shape => main() SKIPs. It must never fall through to a coverage
    verdict derived from a file it could not read."""
    assert _load(monkeypatch, "nothing to see here\n",
                 tmp_path=tmp_path).predicate_shape() == (None, None)
    assert _load(monkeypatch, purge_exists=False,
                 tmp_path=tmp_path).predicate_shape() == (None, None)


def test_live_script_still_matches_a_known_shape():
    """Contract pin against the REAL temp-drain-purge.sh. If someone edits the
    predicate into a third form, this fails loudly here rather than silently
    degrading the durability check to a permanent SKIP."""
    spec = importlib.util.spec_from_file_location("tdi_live", CHECK_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tdi_live"] = mod
    spec.loader.exec_module(mod)
    if not mod.PURGE.is_file():
        pytest.skip("temp-drain-purge.sh not present on this box")
    shape, _ = mod.predicate_shape()
    assert shape in ("inverted", "allowlist"), (
        "PURGE_FIND_PRED parsed to no known shape — temp_durability_invariant.py "
        "will SKIP forever and the temp/ durability check goes silently dark"
    )
