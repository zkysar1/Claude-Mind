"""dry_run() — the observation mode that breaks the kill-test lane's circular gate ().

NEGATIVES FIRST. The dangerous direction here is a dry-run that can be mistaken
for an arm, so the first block pins what it must NEVER do, and the "it works"
cases come after. Every test in the first block was verified RED by mutation
before being committed -- a green test that cannot fail is worth less than no
test (guard-1943).

THE CIRCULARITY UNDER TEST. `test_the_circularity_this_exists_for` is the whole
motivation, executable: feed decide() PERFECT inputs and it still refuses,
because G1 gates G2 and both are default-off. That test failing green (i.e.
decide() promoting on a default config) would mean the fail-closed default had
been lost, which is a far worse regression than anything dry_run() does.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import reducer_promotion as rp  # noqa: E402
import worker_reducer_liveness as wrl  # noqa: E402


# The most favourable inputs a real box could ever present: reducer provably
# gone, claim ancient, every discriminator satisfied, this machine listed.
#
# `liveness_rc=4` is not arbitrary and is the input most likely to be got wrong
# by a later editor: rc=4 is the ONLY member of DECISIVE_RCS. Any other rc means
# the liveness module wound down on accumulated UNVERIFIABILITY, which G4
# deliberately refuses -- winding down on "I cannot tell" is safe, promoting on
# it is split-brain.
#
# `t_takeover_s` is passed explicitly so G5 never reaches the canonical reader.
# A test that silently depends on a config file on the box is not hermetic, and
# G5 refuses outright when that read fails -- which would make every case here
# pass for the wrong reason.
PERFECT = dict(
    self_machine="cc-08",
    liveness_verdict=wrl.VERDICT_WIND_DOWN,
    liveness_rc=4,
    claim_age_s=999_999,
    t_takeover_s=3900,
    discriminators={name: True for name in rp.DISCRIMINATORS},
)


def test_decides_hardcoded_verdict_string_matches_the_sibling_constant():
    """decide() compares against the LITERAL 'wind-down', not an import.

    That is a deliberate seam (the two modules stay independently importable),
    but it means a rename in worker_reducer_liveness would silently stop G4 from
    ever passing -- the gate would refuse forever and read as merely cautious.
    Pin the two together so the rename fails loudly here instead.
    """
    assert wrl.VERDICT_WIND_DOWN == "wind-down"
    assert rp.DECISIVE_RCS == (4,)

ARMED = {
    "enabled": True,
    "fence_verified_at": "2026-08-16T00:00:00",
    "eligible_machines": ["cc-08"],
}
UNARMED = {"eligible_machines": ["cc-08"]}


def _dry(config, **over):
    kw = dict(PERFECT)
    kw.update(over)
    return rp.dry_run(config, **kw)


# ---------------------------------------------------------------------------
# NEGATIVES -- what dry_run must never do
# ---------------------------------------------------------------------------

def test_verdict_is_never_promote_even_on_perfect_inputs():
    """The single most important pin in this file.

    A caller branching on `verdict` -- the field every other function in this
    family returns -- must never be led to promote by this one.
    """
    out = _dry(UNARMED)
    assert out["would_promote"] is True, "precondition: these inputs DO satisfy G3-G6"
    assert out["verdict"] == rp.VERDICT_HOLD
    assert out["verdict"] != rp.VERDICT_PROMOTE


def test_no_returned_value_anywhere_equals_promote():
    """Structural, not field-by-field: PROMOTE must not appear in the result at all.

    Pinning only `verdict` would let a future edit add a `recommended_verdict`
    or stuff PROMOTE into `reason`, and a caller that greps the dict would find
    it. This asserts on every value in the mapping.
    """
    out = _dry(UNARMED)
    for key, value in out.items():
        assert value != rp.VERDICT_PROMOTE, f"{key} carries the PROMOTE verdict"
        if isinstance(value, str):
            # Case-SENSITIVE on purpose. VERDICT_PROMOTE is the machine token
            # ("promote"); the reason prose deliberately says "WOULD PROMOTE ...
            # No promotion occurred", which must stay readable. If this ever
            # fires on the reason string, the fix is to re-case the prose, NOT
            # to relax the check -- the point is that no machine-readable value
            # can be mistaken for the verdict constant.
            assert rp.VERDICT_PROMOTE not in value, f"{key} embeds PROMOTE in text"


def test_the_callers_config_is_never_mutated():
    cfg = dict(UNARMED)
    before = dict(cfg)
    _dry(cfg)
    assert cfg == before, "dry_run mutated the caller's config dict"


def test_the_sentinel_never_escapes_into_the_callers_config():
    cfg = dict(UNARMED)
    _dry(cfg)
    assert "fence_verified_at" not in cfg
    assert rp.DRY_RUN_FENCE_SENTINEL not in str(cfg)


def test_the_sentinel_is_not_a_plausible_timestamp():
    """If it ever leaks into a log or a config it must announce itself.

    A sentinel shaped like an ISO date would read as a real fence verification
    to every human and every parser. This one cannot.
    """
    s = rp.DRY_RUN_FENCE_SENTINEL
    assert not s[:4].isdigit(), "sentinel looks like it starts with a year"
    assert "DRY-RUN" in s
    assert "NEVER" in s


def test_decide_does_not_call_dry_run(monkeypatch):
    """Property 3: unreachable from any promotion path, enforced not asserted."""
    def explode(*a, **k):
        raise AssertionError("decide() reached dry_run -- promotion path contaminated")

    monkeypatch.setattr(rp, "dry_run", explode)
    rp.decide(ARMED, **PERFECT)  # must not raise


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    """No filesystem write of any kind. Fails loudly if one is attempted."""
    real_open = open

    def guarded(file, mode="r", *a, **k):
        if any(w in mode for w in ("w", "a", "x", "+")):
            raise AssertionError(f"dry_run opened {file!r} for write (mode={mode})")
        return real_open(file, mode, *a, **k)

    monkeypatch.setattr("builtins.open", guarded)
    _dry(UNARMED)


# ---------------------------------------------------------------------------
# The circularity itself
# ---------------------------------------------------------------------------

def test_the_circularity_this_exists_for():
    """decide() refuses PERFECT observations on a default config.

    This is the kill-test lane's unsatisfiable bar, executable. G1 (`enabled`)
    sits in front of G2 (`fence_verified_at`) and both default off, so the bar
    "observe an eligible worker's six gates all pass" cannot be met by any
    fieldwork at all.
    """
    real = rp.decide(UNARMED, **PERFECT)
    assert real["verdict"] == rp.VERDICT_HOLD
    assert real["gate_failed"] == "enabled"


def test_dry_run_breaks_the_circularity_on_the_same_inputs():
    """Same inputs decide() refused: G3-G6 are now really evaluated."""
    out = _dry(UNARMED)
    assert out["would_promote"] is True
    assert out["gate_failed"] is None
    assert set(out["simulated_gates"]) == {"enabled", "fence_verified"}
    for g in ("enabled", "fence_verified"):
        assert g not in out["real_gates_evaluated"]
    assert out["real_gates_evaluated"], "no gate was really evaluated -- proves nothing"


# ---------------------------------------------------------------------------
# Simulation bookkeeping -- a simulated pass must never read as a real one
# ---------------------------------------------------------------------------

def test_an_already_armed_config_simulates_nothing():
    out = _dry(ARMED)
    assert out["simulated_gates"] == ()
    assert out["armed_for_real"] is True
    assert "already armed" in out["reason"]


def test_a_partially_armed_config_simulates_only_the_missing_half():
    out = _dry({"enabled": True, "eligible_machines": ["cc-08"]})
    assert out["simulated_gates"] == ("fence_verified",)
    assert out["armed_for_real"] is False

    out = _dry({"fence_verified_at": "2026-08-16T00:00:00",
                "eligible_machines": ["cc-08"]})
    assert out["simulated_gates"] == ("enabled",)


def test_a_blank_fence_stamp_counts_as_unverified():
    """Whitespace is not a verification. Mirrors decide()'s own G2 test."""
    for blank in ("", "   ", None):
        out = _dry({"enabled": True, "fence_verified_at": blank,
                    "eligible_machines": ["cc-08"]})
        assert "fence_verified" in out["simulated_gates"], f"blank {blank!r} accepted"


def test_the_reason_always_names_what_was_simulated():
    """A reader must never be able to read a pass without reading the caveat."""
    for cfg in (UNARMED, ARMED, {"enabled": True, "eligible_machines": ["cc-08"]}):
        assert "imulated" in _dry(cfg)["reason"]


# ---------------------------------------------------------------------------
# G3-G6 are REALLY evaluated -- each must still be able to refuse
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label,over,expect_gate", [
    ("machine not listed",      dict(self_machine="cc-99"),                    "eligible"),
    ("machine unresolvable",    dict(self_machine=""),                         "eligible"),
    ("reducer still live",      dict(liveness_verdict=wrl.VERDICT_CONTINUE),   "liveness_decisive"),
    ("wound down on unverif.",  dict(liveness_rc=1),                           "liveness_decisive"),
    ("claim not yet stale",     dict(claim_age_s=1),                           "claim_stale"),
    ("claim age unreadable",    dict(claim_age_s=None),                        "claim_stale"),
])
def test_a_real_gate_failure_still_refuses_under_dry_run(label, over, expect_gate):
    """Simulating G1/G2 must not soften G3-G6 -- that would be a bypass.

    Each row is one live gate proving it can still refuse while G1/G2 are faked.
    Without these, "dry_run returns would_promote=True on good inputs" would be
    satisfied equally well by a function that always says yes.
    """
    out = _dry(UNARMED, **over)
    assert out["would_promote"] is False, label
    assert out["gate_failed"] == expect_gate, label
    assert out["verdict"] == rp.VERDICT_HOLD, label


def test_an_unmeasured_discriminator_is_not_a_pass():
    """None means unmeasured. It must refuse exactly as False does (fail-closed).

    Iterates DISCRIMINATORS rather than a hand-copied list, so a discriminator
    added to the module joins this test automatically instead of silently
    escaping it.
    """
    assert rp.DISCRIMINATORS, "precondition: there is at least one discriminator"
    for key in rp.DISCRIMINATORS:
        for bad in (None, False, "true", 1):
            d = dict(PERFECT["discriminators"])
            d[key] = bad
            out = _dry(UNARMED, discriminators=d)
            assert out["would_promote"] is False, f"{key}={bad!r} accepted"
            assert out["gate_failed"] == "discriminators", f"{key}={bad!r}"


def test_a_missing_discriminator_dict_refuses():
    for bad in (None, {}, "yes"):
        assert _dry(UNARMED, discriminators=bad)["would_promote"] is False


def test_an_unreadable_t_takeover_refuses_under_dry_run(monkeypatch):
    """G5 must refuse rather than fall back to a hardcoded lease window.

    Note `t_takeover_s=None` is decide()'s NOT-SUPPLIED sentinel, not "unknown"
    -- it routes to the canonical reader, which succeeds on a healthy box. So
    this branch is only reachable by making the reader itself fail, and a test
    that passed None would silently exercise the happy path instead. (Found by
    exactly that mistake: the None row read as a passing gate.)
    """
    monkeypatch.setattr(rp, "_load_canonical_takeover_seconds", lambda: None)
    out = rp.dry_run(UNARMED, **{k: v for k, v in PERFECT.items() if k != "t_takeover_s"})
    assert out["would_promote"] is False
    assert out["gate_failed"] == "claim_stale"
    assert "T_takeover" in out["reason"]


# ---------------------------------------------------------------------------
# Shape / robustness
# ---------------------------------------------------------------------------

def test_the_result_is_self_labelling():
    out = _dry(UNARMED)
    assert out["dry_run"] is True
    assert set(out) == {"dry_run", "verdict", "would_promote", "gate_failed",
                        "simulated_gates", "real_gates_evaluated",
                        "armed_for_real", "reason"}


def test_a_none_config_does_not_crash():
    out = rp.dry_run(None, **PERFECT)
    assert out["verdict"] == rp.VERDICT_HOLD
    assert out["would_promote"] is False   # no eligible_machines -> G4 refuses


def test_dry_run_and_decide_agree_when_the_config_is_really_armed():
    """The one case where the two must not diverge on the gate outcome.

    Nothing is simulated, so `would_promote` has to equal decide()'s verdict --
    otherwise dry_run has its own opinion of the gates, which is the drift the
    call-through design exists to prevent.
    """
    real = rp.decide(ARMED, **PERFECT)
    out = rp.dry_run(ARMED, **PERFECT)
    assert out["would_promote"] == (real["verdict"] == rp.VERDICT_PROMOTE)
    assert out["gate_failed"] == real["gate_failed"]
