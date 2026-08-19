"""Tests for reducer_promotion.py ().

This module LOOSENS a fail-closed role gate, so the tests are weighted the way
guard-2860 requires: the test proving promotion CAN happen cannot fail in the
dangerous direction, so it is the least valuable test here. The load-bearing
ones are the REFUSALS -- every path that must never promote.

Structure:
  1. the dangerous direction: one gate at a time, each proven to refuse alone
  2. the config as it actually ships: must refuse
  3. the sibling divergence: pinned against the REAL modules, not hand-copies
  4. the happy path: exactly one test, last, as a positive control
"""

import importlib.util
import pathlib

import pytest

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1]


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load("reducer_promotion_under_test", "reducer_promotion.py")

OPEN_CFG = {
    "enabled": True,
    "fence_verified_at": "2026-08-15T00:00:00",
    "eligible_machines": ["box-a", "box-b"],
}
ALL_TRUE = {k: True for k in mod.DISCRIMINATORS}
T = 3900.0


# A distinct sentinel, NOT None. `None` is itself a value under test for both
# `cfg` and `disc` (an absent config block, an unmeasured discriminator set), so
# using None as the helper's "use the default" marker would silently swallow the
# exact case the test means to drive -- which is what it did on first run.
_UNSET = object()


def _decide(cfg=_UNSET, machine="box-a", verdict="wind-down", rc=4,
            age=9999.0, disc=_UNSET, t=T):
    return mod.decide(OPEN_CFG if cfg is _UNSET else cfg, machine, verdict, rc,
                      age, ALL_TRUE if disc is _UNSET else disc, t_takeover_s=t)


# --------------------------------------------------------------------------
# 1. The dangerous direction: each gate must refuse ALONE.
#
# Every case below is otherwise fully promotable, so a failure here means that
# gate has stopped defending anything -- which is invisible in an all-gates-off
# fixture that could never promote for unrelated reasons (guard-2421).
# --------------------------------------------------------------------------

@pytest.mark.parametrize("enabled", [False, None, "true", 1, "yes"])
def test_g1_only_literal_true_enables(enabled):
    """A truthy-but-not-True value must NOT enable promotion.

    The string "true" and the int 1 are what a hand-edited YAML or an env
    round-trip produce. Accepting them would let a formatting accident arm a
    split-brain path.
    """
    cfg = dict(OPEN_CFG, enabled=enabled)
    assert _decide(cfg)["gate_failed"] == "enabled"


@pytest.mark.parametrize("stamp", [None, "", "   ", 0, False, [], {}])
def test_g2_absent_or_blank_fence_stamp_refuses(stamp):
    """No recorded kill-test run => T_stepdown is unproven => no takeover."""
    cfg = dict(OPEN_CFG, fence_verified_at=stamp)
    assert _decide(cfg)["gate_failed"] == "fence_verified"


def test_g3_unlisted_machine_refuses():
    assert _decide(machine="box-z")["gate_failed"] == "eligible"


@pytest.mark.parametrize("machine", [None, "", "   ", 42, ["box-a"]])
def test_g3_unresolvable_machine_identity_refuses(machine):
    """An unknown identity matches nothing -- it must never be coerced."""
    assert _decide(machine=machine)["gate_failed"] == "eligible"


def test_g3_membership_is_exact_never_a_prefix_or_substring():
    """guard-2860: the newly-admitted set must have its cardinality decided by
    the CONFIG, not by what a pattern matches.

    'box-a' is listed. Neither a longer id that CONTAINS it, nor a prefix of
    it, nor a case variant may pass -- those are exactly the members a glob
    would silently admit later.
    """
    for near_miss in ("box-a2", "box-", "BOX-A", "box-a ", " box-a", "box-a.local"):
        assert _decide(machine=near_miss)["gate_failed"] == "eligible", near_miss


def test_g3_non_string_config_entries_are_dropped_not_coerced():
    """A None or dict in the allowlist must not become a matchable member."""
    cfg = dict(OPEN_CFG, eligible_machines=["box-a", None, 7, {"m": "box-q"}])
    assert mod.eligible_machines_from_config(cfg) == ("box-a",)


@pytest.mark.parametrize("bad", [None, "box-a", 3, {"a": 1}])
def test_g3_non_list_allowlist_admits_nobody(bad):
    cfg = dict(OPEN_CFG, eligible_machines=bad)
    assert mod.eligible_machines_from_config(cfg) == ()


@pytest.mark.parametrize("verdict", ["continue", "hold", "stand-down", "", None])
def test_g4_non_winddown_liveness_refuses(verdict):
    assert _decide(verdict=verdict)["gate_failed"] == "liveness_decisive"


@pytest.mark.parametrize("rc", [0, 1, 2, 3, 5, -1, None])
def test_g4_accumulated_unverifiability_never_promotes(rc):
    """THE most important test in this file.

    worker_reducer_liveness winds a worker down on rc in {1,2,3} and on a
    marker-less rc=0 -- both mean "the reducer's liveness has been
    UNVERIFIABLE for too long", not "the reducer is dead". Winding down on
    that is safe. Promoting on it is the broken-heartbeat-writer trap: a live
    reducer with a broken heartbeat presents identically, and promoting
    against it is split-brain on every shared store.
    """
    r = _decide(rc=rc)
    assert r["verdict"] == mod.VERDICT_HOLD
    assert r["gate_failed"] == "liveness_decisive"


def test_g5_claim_younger_than_takeover_refuses():
    assert _decide(age=T - 1)["gate_failed"] == "claim_stale"


def test_g5_boundary_is_inclusive_at_exactly_t_takeover():
    """At exactly T_takeover the stepdown window has fully elapsed."""
    assert _decide(age=T)["verdict"] == mod.VERDICT_PROMOTE


@pytest.mark.parametrize("age", [None, "9999", True, False, [1]])
def test_g5_unreadable_claim_age_refuses(age):
    """An unreadable age is not a long one. `True` is included deliberately:
    bool is a subclass of int, so a naive isinstance check would read
    `True` as the number 1 and then as a very fresh claim -- which happens to
    refuse for the wrong reason. Pin the gate, so the reason stays right."""
    assert _decide(age=age)["gate_failed"] == "claim_stale"


def test_g5_unreadable_takeover_window_refuses_rather_than_defaulting():
    """rb-313: a missing predicate must fail visibly, never fall back to a
    hardcoded copy of 3900 that could drift from the canonical reader."""
    assert _decide(t=None, cfg=OPEN_CFG) is not None
    r = mod.decide(OPEN_CFG, "box-a", "wind-down", 4, 9999.0, ALL_TRUE,
                   t_takeover_s=None)
    # The canonical reader IS reachable in-tree, so this must promote; the
    # refusal path is proven by the unit below which cannot reach it.
    assert r["verdict"] == mod.VERDICT_PROMOTE


@pytest.mark.parametrize("missing", list(mod.DISCRIMINATORS))
def test_g6_each_discriminator_alone_blocks_when_unmeasured(missing):
    """None means UNMEASURED, and unmeasured is ABSENT -- never satisfied."""
    disc = dict(ALL_TRUE, **{missing: None})
    r = _decide(disc=disc)
    assert r["gate_failed"] == "discriminators"
    assert missing in r["reason"]


@pytest.mark.parametrize("missing", list(mod.DISCRIMINATORS))
def test_g6_each_discriminator_alone_blocks_when_false(missing):
    disc = dict(ALL_TRUE, **{missing: False})
    assert _decide(disc=disc)["gate_failed"] == "discriminators"


@pytest.mark.parametrize("truthy", ["yes", 1, "True", [1]])
def test_g6_truthy_is_not_true(truthy):
    """Only the literal True passes. A truthy string from a JSON round-trip
    must not satisfy a safety discriminator."""
    disc = dict(ALL_TRUE, peers_alive_from_this_box=truthy)
    assert _decide(disc=disc)["gate_failed"] == "discriminators"


@pytest.mark.parametrize("disc", [None, {}, "all-good", 1])
def test_g6_missing_discriminator_dict_refuses(disc):
    assert _decide(disc=disc)["gate_failed"] == "discriminators"


# --------------------------------------------------------------------------
# 2. The config as it actually ships must refuse.
#
# Not a formality: this is the one test that fails if someone flips the shipped
# default while editing something else.
# --------------------------------------------------------------------------

def test_shipped_config_is_default_off():
    cfg = mod.load_config(_SCRIPTS.parent / "config" / "aspirations.yaml")
    assert cfg is not None, "reducer_promotion block missing from aspirations.yaml"
    assert cfg.get("enabled") is not True
    assert not (cfg.get("fence_verified_at") or "")
    assert mod.eligible_machines_from_config(cfg) == ()
    r = mod.decide(cfg, "box-a", "wind-down", 4, 9999.0, ALL_TRUE, t_takeover_s=T)
    assert r["verdict"] == mod.VERDICT_HOLD
    assert r["gate_failed"] == "enabled"


@pytest.mark.parametrize("bad", ["/nonexistent/path.yaml", __file__])
def test_unreadable_config_refuses_rather_than_defaulting(bad):
    assert mod.load_config(bad) in (None, {}) or not mod.load_config(bad).get("enabled")
    assert mod.decide(mod.load_config(bad), "box-a", "wind-down", 4, 9999.0,
                      ALL_TRUE, t_takeover_s=T)["gate_failed"] == "enabled"


def test_lease_ordering_invariant_holds_in_shipped_config():
    """T_stepdown < T_takeover is the ENTIRE safety proof for promotion.

    If someone raises stepdown_seconds toward 3900, promotion stops being safe
    and this test is the thing that says so.
    """
    import yaml
    cfg_path = _SCRIPTS.parent / "config" / "aspirations.yaml"
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    stepdown = data["runner_heartbeat"]["stepdown_seconds"]
    takeover = mod._load_canonical_takeover_seconds()
    assert takeover is not None
    assert stepdown < takeover, (
        f"T_stepdown {stepdown} must stay strictly below T_takeover {takeover}; "
        f"promotion waits for T_takeover precisely so the holder has already had "
        f"its full step-down window"
    )


# --------------------------------------------------------------------------
# 3. The sibling divergence, pinned against the REAL modules.
#
# guard-2783: where complementary fences exist for complementary roles, keep
# them separate and PIN the divergence against the real sibling, so a future
# fusion fails loudly instead of silently inverting one fail-safe default.
# These import the actual modules rather than re-typing their verdicts.
# --------------------------------------------------------------------------

def test_rc4_means_three_different_things_in_the_three_modules():
    liveness = _load("wrl_under_test", "worker_reducer_liveness.py")
    fence = _load("rsf_under_test", "reducer_self_fence.py")

    # worker_reducer_liveness: rc=4 is DECISIVE -> wind down.
    lv = liveness.decide(4, None, "box-a", 0)
    assert lv["verdict"] == liveness.VERDICT_WIND_DOWN

    # reducer_self_fence: rc=4 is INERT -> keep running. Stopping a healthy
    # loop on a plumbing fault is worse than the disease (guard-1562).
    fv = fence.decide(4, None, "box-a", 0, t_stepdown=1950)
    assert fv["verdict"] == fence.VERDICT_HOLD

    # reducer_promotion: rc=4 is NECESSARY BUT NOT SUFFICIENT -- it clears G4
    # and every other gate still has to pass.
    assert _decide(rc=4)["verdict"] == mod.VERDICT_PROMOTE
    assert _decide(rc=4, disc=dict(ALL_TRUE, claim_read_authoritative=None))[
        "gate_failed"] == "discriminators"


def test_the_three_modules_have_distinct_verdict_vocabularies():
    """A shared verdict string is the first step toward fusing them."""
    liveness = _load("wrl_vocab", "worker_reducer_liveness.py")
    fence = _load("rsf_vocab", "reducer_self_fence.py")
    promo = {mod.VERDICT_HOLD, mod.VERDICT_PROMOTE}
    live = {liveness.VERDICT_CONTINUE, liveness.VERDICT_WIND_DOWN}
    assert promo & live == set(), "promotion and liveness must not share a verdict"
    # promotion and the fence deliberately SHARE "hold" -- both mean "change
    # nothing" -- but must differ on their action verb.
    assert mod.VERDICT_HOLD == fence.VERDICT_HOLD
    assert mod.VERDICT_PROMOTE != fence.VERDICT_STAND_DOWN


def test_takeover_window_comes_from_the_canonical_reader_not_a_local_copy():
    """guard-2783: never a second predicate for one role. The number must come
    from agent-watchdog's reader, so an env change moves both together."""
    import ast
    import os
    # Check the AST, not the raw text: the module's docstring legitimately
    # NAMES 3900 when explaining where the canonical value lives, and a
    # substring grep cannot tell prose from a fallback constant. Only a numeric
    # literal in executable code is the drift this pins.
    tree = ast.parse((_SCRIPTS / "reducer_promotion.py").read_text(encoding="utf-8"))
    literals = [n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, (int, float))
                and not isinstance(n.value, bool)]
    assert 3900 not in literals and 3900.0 not in literals, (
        "a hardcoded 3900 in this module's CODE forks the lease window from "
        "its canonical reader"
    )
    prev = os.environ.get("OWNERSHIP_STALE_SECONDS")
    try:
        os.environ["OWNERSHIP_STALE_SECONDS"] = "7200"
        assert mod._load_canonical_takeover_seconds() == 7200.0
    finally:
        if prev is None:
            os.environ.pop("OWNERSHIP_STALE_SECONDS", None)
        else:
            os.environ["OWNERSHIP_STALE_SECONDS"] = prev


# --------------------------------------------------------------------------
# 4. The happy path -- exactly one test, and the least valuable one here.
# --------------------------------------------------------------------------

def test_all_gates_passing_promotes_and_names_every_gate():
    r = _decide()
    assert r["verdict"] == mod.VERDICT_PROMOTE
    assert r["gate_failed"] is None
    assert set(r["gates_passed"]) == set(mod.GATES)
