"""test_goal_selector_stale_cache_refresh.py --  regression.

The selector's aspiration READS went straight to disk while every aspiration
WRITE goes through the daemon. Under STORAGE_BACKEND=own-cloud the local tree is
a read-through cache (guard-980, rb-2636), so between a peer's completion and
this box's next sync the selector scored a snapshot in which that goal was still
pending. MEASURED (zeta, cc-02, own-cloud, 2026-09-06): 26 terminal goals inside
a 1604-candidate pool, ranked_goals[0] = g-115-9106 which had been skipped four
hours earlier; after the local store refreshed, 26 -> 0, same code, same box,
five minutes apart. rank #1 is authoritative under scorer sovereignty, so the
agent executes an already-terminal goal -- silently, because the pool looks
healthy.

WHAT IS PINNED HERE, and why each is a real regression rather than a restatement:

  1. force_fresh=True on BOTH aspiration stores. `ensure_local` is
     `_refresh(force_fresh=False)` and is TTL-gated -- it is NOT a
     give-me-current-bytes call (guard-980's REMEDY-HAS-A-HOLE paragraph), so a
     future edit that "simplifies" this to ensure_local would restore the defect
     while every ordering test below still passed.

  2. WIRING, AND AT RUNTIME RATHER THAN BY SOURCE INDEX (guard-1943: a passing
     unit test proves the function, never the wiring). The refresh is only
     worth anything if it happens BEFORE the reads it exists to make current;
     a refresh AFTER the read is a no-op that greps as present. These tests
     record actual call ORDER through cmd_select and cmd_blocked.

  3. The all-blocked retry specifically. That path exists to supply
     verify-before-assuming's SECOND INDEPENDENT SIGNAL before declaring a
     work-gating negative -- but re-reading the same cache is the SAME signal,
     so without the refresh the retry could only ever confirm the first pass.

  4. FAIL-OPEN, deliberately opposite to unit_claim._refresh_board_cache, which
     REFUSES. There a stale cache reports "the unit is free" and produces a
     duplicate build. Here a refusal stops goal selection for every Body on the
     box, while the worst case it averts is one wasted goal execution -- and
     stopping a healthy loop on a plumbing fault is worse than the disease
     (guard-1562). A future edit flipping this to raise would wedge the fleet,
     so the direction is pinned in both failure shapes.
"""

from __future__ import annotations

import argparse
import importlib
import io
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "alpha")

gs = importlib.import_module("goal-selector")

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT


class _SpyBackend:
    """Records every read_text call. ensure_local/refresh are TRAPS.

    They are the TTL-gated siblings guard-980 warns are not
    give-me-current-bytes calls; if the helper ever reaches for one, the test
    fails loudly instead of silently accepting a weaker refresh.
    """

    def __init__(self, raise_on=None):
        self.calls = []
        self.raise_on = raise_on

    def read_text(self, path, encoding="utf-8", *, force_fresh=False):
        self.calls.append((str(path), force_fresh))
        if self.raise_on is not None and self.raise_on in str(path):
            raise OSError("simulated backend failure")
        return ""

    def ensure_local(self, path):  # pragma: no cover - trap
        raise AssertionError(
            "ensure_local is TTL-gated (_refresh(force_fresh=False)) and is NOT "
            "a give-me-current-bytes call -- guard-980. Use "
            "read_text(force_fresh=True).")

    def refresh(self, path):  # pragma: no cover - trap
        raise AssertionError("refresh() bypasses the force_fresh contract pinned here")


def _install(monkeypatch, backend):
    import storage_backend
    monkeypatch.setattr(storage_backend, "get_backend", lambda: backend)
    return backend


# ---------------------------------------------------------------------------
# 1. the refresh itself
# ---------------------------------------------------------------------------

def test_refresh_forces_fresh_on_both_aspiration_stores(monkeypatch):
    """POSITIVE CONTROL for this file (guard-2421): without it, a helper
    hardcoded to return early would satisfy every fail-open assertion below."""
    spy = _install(monkeypatch, _SpyBackend())
    ok = gs.refresh_aspiration_caches(paths=["/w/aspirations.jsonl",
                                             "/a/aspirations.jsonl"])
    assert ok is True
    assert [c[0] for c in spy.calls] == ["/w/aspirations.jsonl",
                                         "/a/aspirations.jsonl"]
    assert all(c[1] is True for c in spy.calls), (
        f"every aspiration read must be force_fresh=True, got {spy.calls}")


def test_default_paths_are_the_two_aspiration_queues(monkeypatch):
    """The default MUST cover both queues. Covering only the world store would
    leave the agent queue on the stale snapshot -- rb-9476's shape, a fix that
    is present, correct-looking and inert for half its population."""
    spy = _install(monkeypatch, _SpyBackend())
    gs.refresh_aspiration_caches()
    seen = [c[0] for c in spy.calls]
    assert str(gs.WORLD_ASP_PATH) in seen, seen
    if gs.AGENT_ASP_PATH:
        assert str(gs.AGENT_ASP_PATH) in seen, seen


def test_a_none_path_is_skipped_not_stringified(monkeypatch):
    """AGENT_ASP_PATH is None when no agent dir is bound. str(None) would send
    the literal 'None' to the backend as a path."""
    spy = _install(monkeypatch, _SpyBackend())
    gs.refresh_aspiration_caches(paths=[None, "/w/aspirations.jsonl"])
    assert [c[0] for c in spy.calls] == ["/w/aspirations.jsonl"]


# ---------------------------------------------------------------------------
# 2. fail-open direction
# ---------------------------------------------------------------------------

def test_a_failing_read_is_fail_open_and_warns(monkeypatch, capsys):
    spy = _install(monkeypatch, _SpyBackend(raise_on="/w/"))
    ok = gs.refresh_aspiration_caches(paths=["/w/aspirations.jsonl",
                                             "/a/aspirations.jsonl"])
    assert ok is False, "a failed refresh must report False"
    err = capsys.readouterr().err
    assert "refresh FAILED" in err
    assert "g-115-9264" in err, "the warning must name the defect it re-enables"
    # and it must NOT abandon the remaining store
    assert [c[0] for c in spy.calls] == ["/w/aspirations.jsonl",
                                         "/a/aspirations.jsonl"]


def test_an_unavailable_backend_is_fail_open_and_warns(monkeypatch, capsys):
    import storage_backend

    def _boom():
        raise RuntimeError("no backend")

    monkeypatch.setattr(storage_backend, "get_backend", _boom)
    ok = gs.refresh_aspiration_caches(paths=["/w/aspirations.jsonl"])
    assert ok is False
    assert "UNAVAILABLE" in capsys.readouterr().err


def test_refresh_never_raises_so_selection_cannot_be_wedged(monkeypatch):
    """The load-bearing half of fail-open: unit_claim REFUSES on a failed
    refresh; here a raise would stop goal selection for every Body on the box."""
    _install(monkeypatch, _SpyBackend(raise_on="aspirations"))
    gs.refresh_aspiration_caches(paths=["/w/aspirations.jsonl"])  # must not raise


# ---------------------------------------------------------------------------
# 3. wiring — ORDER, at runtime (guard-1943)
# ---------------------------------------------------------------------------

def _order_probe(monkeypatch):
    """Record refresh + world-read events in call order."""
    order = []
    monkeypatch.setattr(gs, "refresh_aspiration_caches",
                        lambda *a, **k: order.append("refresh"))

    def _rj(path):
        if path == gs.WORLD_ASP_PATH:
            order.append("read-world")
        return []

    monkeypatch.setattr(gs, "read_jsonl", _rj)
    return order


def test_cmd_select_refreshes_before_the_first_aspiration_read(monkeypatch):
    order = _order_probe(monkeypatch)
    buf = io.StringIO()
    with redirect_stdout(buf):
        gs.cmd_select(argparse.Namespace())
    assert "refresh" in order, "cmd_select never refreshed the cache"
    assert "read-world" in order, "probe did not observe the world read"
    assert order.index("refresh") < order.index("read-world"), (
        f"the refresh must PRECEDE the read it exists to make current; got {order}")


def test_cmd_blocked_refreshes_before_reading(monkeypatch):
    order = _order_probe(monkeypatch)
    buf = io.StringIO()
    with redirect_stdout(buf):
        gs.cmd_blocked(argparse.Namespace())
    assert order.index("refresh") < order.index("read-world"), order


def test_allblocked_retry_refreshes_so_the_second_signal_is_independent(monkeypatch):
    """The retry supplies verify-before-assuming's SECOND INDEPENDENT SIGNAL.
    Re-reading the same cache is the SAME signal, so the retry could only ever
    confirm the negative it was added to falsify. Two refreshes must occur: one
    before the first read, one before the re-read."""
    order = []
    monkeypatch.setattr(gs, "refresh_aspiration_caches",
                        lambda *a, **k: order.append("refresh"))

    # The world must be NON-EMPTY but yield ZERO candidates, or cmd_select takes
    # its "no goals at all" early-return and never reaches the retry at all.
    # A goal deferred to 2099 is present, counted, and hard-blocked.
    blocked_world = [{
        "id": "asp-test", "status": "active",
        "goals": [{
            "id": "g-test-09", "title": "blocked goal",
            "status": "pending", "participants": ["agent"],
            "deferred_until": "2099-01-01T00:00:00",
        }],
    }]

    def _rj(path):
        if path == gs.WORLD_ASP_PATH:
            order.append("read-world")
            return [dict(a) for a in blocked_world]
        return []

    monkeypatch.setattr(gs, "read_jsonl", _rj)
    monkeypatch.setattr(gs, "read_wm", lambda: {"slots": {}})
    monkeypatch.setattr(gs, "load_recent_class_completions", lambda window_size=20: [])
    monkeypatch.setattr(gs, "load_exploration_params", lambda: (0.0, 0.0))
    monkeypatch.setattr(gs, "AGENT_DIR", None)
    buf = io.StringIO()
    with redirect_stdout(buf):
        gs.cmd_select(argparse.Namespace())
    assert order.count("refresh") >= 2, (
        "the all-blocked retry must refresh before its re-read, or its second "
        f"signal is byte-identical to the first; got {order}")


def test_every_refresh_call_site_is_wired(monkeypatch):
    """Source-level breadth check, complementing the runtime order tests above.

    Three call sites are expected: cmd_select's first read, the all-blocked
    retry re-read, and cmd_blocked. A dropped site is invisible to the ordering
    tests for the OTHER two, which would still pass.
    """
    src = (CORE_SCRIPTS / "goal-selector.py").read_text(encoding="utf-8")
    assert src.count("refresh_aspiration_caches()") == 3, (
        "expected exactly 3 wired call sites (cmd_select, all-blocked retry, "
        "cmd_blocked)")
    assert src.count("def refresh_aspiration_caches") == 1, (
        "the helper must have exactly one definition -- a second copy is the "
        "no-transcription violation guard-2676 forbids")


# ---------------------------------------------------------------------------
# 4. the SILENT decline — 
# ---------------------------------------------------------------------------
#
# The refresh above has one failure shape that raises (section 2) and one that
# does NOT: OwnCloudBackend._refresh returns the LOCAL path unchanged in the
# both-diverged (no_clobber) state, so read_text succeeds, the caller reads
# bytes, and the bytes are the exact stale snapshot the refresh exists to
# replace. Every test in sections 1-3 passes against that state, because from
# the caller's side it is indistinguishable from a successful refresh.
#
# guard-5501 governs what these tests have to be: a diagnostic's silence is not
# evidence until you PROVE it can fire. So the decline is INDUCED here (the
# backend is put into the no_clobber state) and the warning is watched, and the
# non-declining case is asserted SILENT so the emit is discriminating rather
# than unconditional.


class _DivergedBackend(_SpyBackend):
    """A backend in the both-diverged state for the paths named in `diverged`.

    Mirrors the real contract: OwnCloudBackend._refresh adds the S3 key to
    self._diverged_keys in the no_clobber branch and discards it on every other
    outcome, so immediately after a force_fresh read the set records what THAT
    read decided. read_text still returns normally -- that is the whole defect.
    """

    def __init__(self, diverged=(), key_raises=False):
        super().__init__()
        self._diverged_keys = {self._s3_key(d) for d in diverged}
        self._key_raises = key_raises

    def _s3_key(self, path):
        if getattr(self, "_key_raises", False):
            raise ValueError("path is not under any configured root")
        return "prefix/" + str(path).lstrip("/")


def test_a_silent_decline_is_reported(monkeypatch, capsys):
    """POSITIVE CONTROL (guard-5501 (c)): induce the fault, watch it fire."""
    _install(monkeypatch, _DivergedBackend(diverged=["/w/aspirations.jsonl"]))
    ok = gs.refresh_aspiration_caches(paths=["/w/aspirations.jsonl",
                                             "/a/aspirations.jsonl"])
    err = capsys.readouterr().err
    assert ok is False, (
        "a refresh that handed back the local file must not report success -- "
        "the return value is the one signal that survives a piped caller "
        "(guard-5596)")
    assert "DECLINED" in err
    assert "/w/aspirations.jsonl" in err, "the warning must name WHICH store"
    assert "g-115-9276" in err, "the warning must name the defect it re-enables"


def test_only_the_declining_path_warns_and_the_other_is_still_read(monkeypatch, capsys):
    """The emit must DISCRIMINATE. An unconditional warning is as useless as
    silence, and a decline on one store must not abandon the other."""
    spy = _install(monkeypatch, _DivergedBackend(diverged=["/w/aspirations.jsonl"]))
    gs.refresh_aspiration_caches(paths=["/w/aspirations.jsonl",
                                        "/a/aspirations.jsonl"])
    err = capsys.readouterr().err
    assert err.count("DECLINED") == 1, err
    assert "/a/aspirations.jsonl" not in err
    assert [c[0] for c in spy.calls] == ["/w/aspirations.jsonl",
                                         "/a/aspirations.jsonl"]


def test_no_divergence_is_silent_and_still_reports_success(monkeypatch, capsys):
    """NEGATIVE CONTROL: the healthy path must stay quiet and stay True."""
    _install(monkeypatch, _DivergedBackend(diverged=[]))
    ok = gs.refresh_aspiration_caches(paths=["/w/aspirations.jsonl"])
    assert ok is True
    assert "DECLINED" not in capsys.readouterr().err


def test_an_unmappable_path_cannot_have_declined(monkeypatch, capsys):
    """_s3_key raises ValueError for a path under no configured root. Such a
    path is never on the remote store, so it can never be in the diverged set --
    the consult must swallow that and stay fail-open, never wedge selection."""
    _install(monkeypatch, _DivergedBackend(diverged=["/w/aspirations.jsonl"],
                                           key_raises=True))
    ok = gs.refresh_aspiration_caches(paths=["/w/aspirations.jsonl"])
    assert ok is True
    assert "DECLINED" not in capsys.readouterr().err


def test_a_backend_without_the_attribute_is_unaffected(monkeypatch, capsys):
    """LocalBackend has neither _diverged_keys nor _s3_key -- the local file IS
    the store, so nothing can diverge. The consult must be a no-op there rather
    than an AttributeError that wedges every local-backend run."""
    _install(monkeypatch, _SpyBackend())
    ok = gs.refresh_aspiration_caches(paths=["/w/aspirations.jsonl"])
    assert ok is True
    assert "DECLINED" not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 5. the decline is a DECISION, not just a message ( item 3)
# ---------------------------------------------------------------------------
#
# Item 1 made the refresh helper's decline LOUD and gave it a return value.
# Nothing consumed that return value, so the cross-agent lane still scored a
# peer queue it had failed to refresh -- and the rows a stale peer mirror is
# missing are always the NEWEST, i.e. exactly the closes (guard-6156). These
# tests pin the consumption, and the two controls are the point: a withhold
# that cannot be shown to FIRE, and one that cannot be shown NOT to, are both
# worthless (guard-5501).


def _peer_fixture(tmp_path, peers=("zeta", "echo")):
    """agents/<me> plus sibling peer dirs, each with an empty queue file.

    The queue files are created with touch(): the code under test only probes
    them with .exists(), and read_jsonl is stubbed out below, so an empty file
    is the whole fixture.
    """
    _ASP_NAME = gs.AGENT_ASP_PATH.name
    agents = tmp_path / "agents"
    me = agents / "alpha"
    me.mkdir(parents=True)
    (me / _ASP_NAME).touch()
    qs = {}
    for p in peers:
        d = agents / p
        d.mkdir(parents=True)
        q = d / _ASP_NAME
        q.touch()
        qs[p] = q
    return agents, me, qs


def _stub_candidates(monkeypatch):
    """Bypass the eligibility filter: this section tests the WITHHOLD decision."""
    monkeypatch.setattr(gs, "read_jsonl", lambda p: [{"id": "asp-x"}])
    monkeypatch.setattr(
        gs, "collect_candidates",
        lambda *a, **k: [{"goal": {"id": "g-x", "intended_agent": "alpha"},
                          "source": k.get("source")}])


def test_a_declined_peer_queue_contributes_no_candidates(tmp_path, monkeypatch, capsys):
    """POSITIVE CONTROL -- the decline is INDUCED and the withhold must fire."""
    agents, me, qs = _peer_fixture(tmp_path, peers=("zeta",))
    _stub_candidates(monkeypatch)
    monkeypatch.setattr(gs, "refresh_aspiration_caches", lambda paths: False)
    out = gs.collect_cross_agent_candidates(agents, me, "alpha")
    assert out == [], "a peer queue that could not be refreshed must not be scored"
    err = capsys.readouterr().err
    assert "WITHHELD" in err and "zeta" in err
    assert "g-115-9276" in err


def test_a_refreshed_peer_queue_still_contributes(tmp_path, monkeypatch, capsys):
    """NEGATIVE CONTROL -- proves the withhold is not unconditional."""
    agents, me, qs = _peer_fixture(tmp_path, peers=("zeta",))
    _stub_candidates(monkeypatch)
    monkeypatch.setattr(gs, "refresh_aspiration_caches", lambda paths: True)
    out = gs.collect_cross_agent_candidates(agents, me, "alpha")
    assert len(out) == 1, "a cleanly refreshed peer queue must still be scored"
    assert "WITHHELD" not in capsys.readouterr().err


def test_one_declining_peer_does_not_withhold_the_other(tmp_path, monkeypatch):
    """DISCRIMINATION -- the batching regression.

    refresh_aspiration_caches returns ONE bool for a whole path list, so a
    batched call would let a single unrefreshable peer suppress EVERY peer's
    candidates. That is a strictly worse failure than the one being fixed.
    """
    agents, me, qs = _peer_fixture(tmp_path, peers=("zeta", "echo"))
    _stub_candidates(monkeypatch)
    monkeypatch.setattr(
        gs, "refresh_aspiration_caches",
        lambda paths: "zeta" not in str(paths[0]))
    out = gs.collect_cross_agent_candidates(agents, me, "alpha")
    assert len(out) == 1, "only the declining peer may be withheld"
    assert out[0]["source"] == "cross-agent:echo"


def test_refresh_is_called_once_per_queue_not_batched(tmp_path, monkeypatch):
    """The per-queue shape is what makes the decision scoped to its own queue."""
    agents, me, qs = _peer_fixture(tmp_path, peers=("zeta", "echo"))
    _stub_candidates(monkeypatch)
    seen = []

    def _spy(paths):
        seen.append(list(paths))
        return True

    monkeypatch.setattr(gs, "refresh_aspiration_caches", _spy)
    gs.collect_cross_agent_candidates(agents, me, "alpha")
    assert len(seen) == 2, "one refresh call per peer queue"
    assert all(len(c) == 1 for c in seen), "each call carries exactly one path"


def test_a_raising_refresh_withholds_that_peer_and_does_not_wedge(
        tmp_path, monkeypatch, capsys):
    """A raise is treated as a decline -- and must not abort the whole sweep."""
    agents, me, qs = _peer_fixture(tmp_path, peers=("zeta", "echo"))
    _stub_candidates(monkeypatch)

    def _boom(paths):
        if "zeta" in str(paths[0]):
            raise RuntimeError("simulated refresh explosion")
        return True

    monkeypatch.setattr(gs, "refresh_aspiration_caches", _boom)
    out = gs.collect_cross_agent_candidates(agents, me, "alpha")
    assert len(out) == 1 and out[0]["source"] == "cross-agent:echo"
    err = capsys.readouterr().err
    assert "RAISED" in err and "WITHHELD" in err
