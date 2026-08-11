""" — owner-qualified origin_signal keys (guard-2107).

Agent-queue goal ids are not globally unique (every agent has its own asp-001
with its own `g-001-NN` series), so a dedup key embedding a bare goal id is
ambiguous to any reader whose scope is wider than one agent's queue. Measured
2026-08-01: every reader in this family spans world + the bound agent, so the
defect condition is "a key derived from a per-agent id that lands in WORLD".

WHAT THESE TESTS BIND TO, AND WHY IT MATTERS
--------------------------------------------
`test_canary_re_*` imports the LIVE `CANARY_RE` out of streak-break-reflector
rather than re-declaring the pattern. That is the whole point: the writer
emits an owner-qualified key and `_auto_resolve_recovered_canaries` parses it
back, so a regex that rejects the new form would silently kill auto-resolve
while every write-side assertion stayed green (guard-1943 — a green suite
certifies the FUNCTION, never the WIRING). A test over a copied regex cannot
see that, so it must bind to the shipped object.

`test_round_trip_*` closes the pair explicitly: what the writer emits, the
reader must parse, for both queue sources.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

from _owner_qualified_signal import (  # noqa: E402
    legacy_signal, qualified_signal, signal_candidates)


def _load_reflector():
    """Load streak-break-reflector.py (hyphenated -> not importable normally)."""
    path = CORE_SCRIPTS / "streak-break-reflector.py"
    spec = importlib.util.spec_from_file_location("streak_break_reflector", path)
    assert spec and spec.loader, "spec_from_file_location returned None"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def reflector():
    return _load_reflector()


@pytest.fixture()
def owner_zeta(monkeypatch):
    """guard-1165: env mutation via monkeypatch, never at module level."""
    monkeypatch.setenv("MIND_AGENT", "zeta")


# ─────────────────────────── helper semantics ───────────────────────────

def test_world_source_is_byte_identical(owner_zeta):
    """World ids ARE globally unique — rewriting them would orphan every key
    already filed under the old form and re-file each exactly once."""
    assert qualified_signal("idea:", "g-115-99", "world") == "idea:g-115-99"


def test_agent_source_is_qualified(owner_zeta):
    assert qualified_signal("idea:", "g-001-08", "agent") == "idea:zeta-g-001-08"


def test_explicit_owner_beats_env(owner_zeta):
    """A sweep acting on a goal it does not own must stamp the OWNER, not the
    running agent — a confidently-wrong qualification is worse than legacy."""
    assert qualified_signal("idea:", "g-001-08", "agent",
                            owner="foxtrot") == "idea:foxtrot-g-001-08"


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_owner_falls_back_to_legacy(monkeypatch, blank):
    """Never mint `<prefix>-<id>`: it READS as qualified while colliding
    fleet-wide in a NEW way, which is strictly worse than the original bug."""
    monkeypatch.setenv("MIND_AGENT", blank)
    assert qualified_signal("idea:", "g-001-08", "agent") == "idea:g-001-08"
    assert qualified_signal("idea:", "g-001-08", "agent",
                            owner=blank) == "idea:g-001-08"


def test_unset_env_falls_back_to_legacy(monkeypatch):
    monkeypatch.delenv("MIND_AGENT", raising=False)
    assert qualified_signal("idea:", "g-001-08", "agent") == "idea:g-001-08"


def test_legacy_signal_is_always_bare(owner_zeta):
    assert legacy_signal("idea:", "g-001-08") == "idea:g-001-08"


def test_candidates_agent_returns_both_forms(owner_zeta):
    """The read side must match in-flight keys filed BEFORE this fix, or each
    one gets re-filed exactly once."""
    assert signal_candidates("idea:", "g-001-08", "agent") == [
        "idea:zeta-g-001-08", "idea:g-001-08"]


def test_candidates_world_returns_one_form(owner_zeta):
    """No duplicate entry when qualified == legacy, so callers can compare
    lengths without special-casing."""
    assert signal_candidates("idea:", "g-115-99", "world") == ["idea:g-115-99"]


# ──────────────────── canary_re: bound to the LIVE object ────────────────────

def test_canary_re_accepts_legacy_key(reflector):
    m = reflector.CANARY_RE.match("investigate:streak-break:g-001-08")
    assert m is not None
    assert m.group("gid") == "g-001-08"
    assert m.group("owner") is None, "a legacy key must not be mis-split"


def test_canary_re_accepts_qualified_key(reflector):
    m = reflector.CANARY_RE.match("investigate:streak-break:zeta-g-001-08")
    assert m is not None
    assert m.group("gid") == "g-001-08"
    assert m.group("owner") == "zeta"


@pytest.mark.parametrize("bad", [
    "idea:g-001-08",
    "investigate:completed-not-committed-g-001-08",
    "investigate:streak-break:not-a-goal",
    "investigate:streak-break:g-001-08-extra",
])
def test_canary_re_still_rejects_other_families(reflector, bad):
    """Widening for the owner must not start consuming other families' goals —
    the auto-resolve sweep would close goals it does not own."""
    assert reflector.CANARY_RE.match(bad) is None


def test_canary_re_would_reject_qualified_key_before_the_fix():
    """Discrimination proof (rb-5828): the PRE-fix regex, re-introduced here
    verbatim, rejects the key the writer now emits. Without this the suite
    could pass over a regex that never matches a live canary."""
    pre_fix = re.compile(r"^investigate:streak-break:(g-\d+-\d+)$")
    assert pre_fix.match("investigate:streak-break:g-001-08") is not None
    assert pre_fix.match("investigate:streak-break:zeta-g-001-08") is None


# ─────────────── round trip: writer emits what reader parses ───────────────

@pytest.mark.parametrize("source,expected_owner", [
    ("world", None),
    ("agent", "zeta"),
])
def test_round_trip_writer_to_reader(reflector, owner_zeta, source,
                                     expected_owner):
    written = qualified_signal(reflector.CANARY_PREFIX, "g-001-08", source)
    m = reflector.CANARY_RE.match(written)
    assert m is not None, f"reader rejected what writer emitted: {written}"
    assert m.group("gid") == "g-001-08"
    assert m.group("owner") == expected_owner


def test_writer_and_reader_share_one_prefix(reflector):
    """SSOT: the pair cannot drift if both sides name the same constant."""
    assert reflector.CANARY_PREFIX == "investigate:streak-break:"
    assert reflector.CANARY_RE.match(
        reflector.CANARY_PREFIX + "g-001-08") is not None
