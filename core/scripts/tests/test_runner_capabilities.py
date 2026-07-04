"""Tests for _runner_capabilities (0 per-runner capability filter).

Covers the three pure functions + the config-override precedence that make the
goal-selector capability gate safe: derive_runner_capabilities (probe + provides
+ lacks), goal_required_capabilities (explicit-only, conservative), and
goal_is_locally_executable (subset test, empty-requirement = always executable).
"""
import _runner_capabilities as rc


# ── derive_runner_capabilities: probe injection + config override precedence ──

def test_derive_uses_injected_probe():
    caps = rc.derive_runner_capabilities(config={}, probe_fn=lambda: {"aws", "gpu"})
    assert caps == {"aws", "gpu"}


def test_derive_probe_false_skips_probes():
    # probe: False -> config-only; injected probe must NOT run.
    caps = rc.derive_runner_capabilities(
        config={"probe": False, "provides": ["git-push"]},
        probe_fn=lambda: {"aws"})
    assert caps == {"git-push"}
    assert "aws" not in caps


def test_derive_provides_adds():
    caps = rc.derive_runner_capabilities(
        config={"provides": ["studio-session"]}, probe_fn=lambda: {"git-push"})
    assert caps == {"git-push", "studio-session"}


def test_derive_lacks_removes_even_probed():
    # lacks is the authoritative last word -- removes a PROBED capability
    # (a read-only clone declaring `lacks: [git-push]` must win over the
    # default-present git-push probe).
    caps = rc.derive_runner_capabilities(
        config={"lacks": ["git-push"]}, probe_fn=lambda: {"git-push", "aws"})
    assert caps == {"aws"}


def test_derive_lacks_beats_provides():
    # lacks applied after provides -> removal wins on conflict.
    caps = rc.derive_runner_capabilities(
        config={"provides": ["gpu"], "lacks": ["gpu"]}, probe_fn=lambda: set())
    assert "gpu" not in caps


def test_derive_none_config_probes():
    caps = rc.derive_runner_capabilities(config=None, probe_fn=lambda: {"aws"})
    assert caps == {"aws"}


def test_derive_probe_error_fails_safe():
    def boom():
        raise RuntimeError("probe blew up")
    # A probe exception must NOT crash selection -> empty caps + config still applied.
    caps = rc.derive_runner_capabilities(config={"provides": ["aws"]}, probe_fn=boom)
    assert caps == {"aws"}


def test_derive_bad_config_type_is_ignored():
    caps = rc.derive_runner_capabilities(config=["not", "a", "dict"], probe_fn=lambda: {"aws"})
    assert caps == {"aws"}


# ── goal_required_capabilities: explicit-only, conservative ──

def test_required_none_is_empty():
    assert rc.goal_required_capabilities({"id": "g-1"}) == set()


def test_required_string_coerced_to_set():
    assert rc.goal_required_capabilities({"requires_capability": "ml-deps"}) == {"ml-deps"}


def test_required_list():
    assert rc.goal_required_capabilities(
        {"requires_capability": ["ml-deps", "gpu"]}) == {"ml-deps", "gpu"}


def test_required_strips_blanks():
    assert rc.goal_required_capabilities(
        {"requires_capability": ["ml-deps", "", "  "]}) == {"ml-deps"}


def test_required_non_list_type_is_empty():
    # A malformed value (dict) must NOT gate the goal -> empty -> always executable.
    assert rc.goal_required_capabilities({"requires_capability": {"x": 1}}) == set()


def test_required_non_dict_goal_is_empty():
    assert rc.goal_required_capabilities("not-a-dict") == set()


# ── goal_is_locally_executable: the safe subset gate ──

def test_executable_when_no_requirement():
    # The conservative core: an UNTAGGED goal is ALWAYS executable, never hidden.
    assert rc.goal_is_locally_executable({"id": "g"}, set()) is True


def test_executable_when_requirement_satisfied():
    assert rc.goal_is_locally_executable(
        {"requires_capability": ["ml-deps"]}, {"ml-deps", "aws"}) is True


def test_not_executable_when_requirement_missing():
    assert rc.goal_is_locally_executable(
        {"requires_capability": ["ml-deps"]}, {"aws"}) is False


def test_not_executable_when_partial_requirement():
    # ALL required caps must be present (subset, not intersection).
    assert rc.goal_is_locally_executable(
        {"requires_capability": ["ml-deps", "gpu"]}, {"ml-deps"}) is False


def test_executable_accepts_list_runner_caps():
    # runner_caps passed as a list (not a set) still works.
    assert rc.goal_is_locally_executable(
        {"requires_capability": ["aws"]}, ["aws", "gpu"]) is True


# ── smoke: real probe path must not crash ──

def test_real_probe_runs_and_returns_set():
    caps = rc._probe_default_capabilities()
    assert isinstance(caps, set)
    # git-push is unconditionally added by the probe (default-present).
    assert "git-push" in caps


def test_known_capabilities_contract_stable():
    # The token set is a cross-file contract; guard against accidental drift.
    assert {"ml-deps", "aws", "product-runtime", "gpu", "git-push",
            "studio-session"} <= set(rc.KNOWN_CAPABILITIES)


# ── apply_capability_filter: the goal-selector integration point ──

def _cand(gid, req=None):
    g = {"id": gid}
    if req is not None:
        g["requires_capability"] = req
    return {"goal": g, "aspiration": {"id": "asp-x"}, "source": "world"}


def test_filter_drops_unexecutable_when_executable_remains():
    cands = [_cand("g-fw"), _cand("g-ml", ["ml-deps"])]  # runner lacks ml-deps
    out, dropped = rc.apply_capability_filter(cands, {"git-push"})
    assert dropped == 1
    assert [c["goal"]["id"] for c in out] == ["g-fw"]


def test_filter_no_regression_when_all_unexecutable():
    # A FULLY capability-constrained set must be kept intact (dropped == 0),
    # so the caller's existing all-unexecutable path is unchanged.
    cands = [_cand("g-ml", ["ml-deps"]), _cand("g-gpu", ["gpu"])]
    out, dropped = rc.apply_capability_filter(cands, {"git-push"})
    assert dropped == 0
    assert len(out) == 2  # all kept -- the no-regression guard


def test_filter_keeps_all_when_all_executable():
    cands = [_cand("g-1"), _cand("g-2", ["git-push"])]
    out, dropped = rc.apply_capability_filter(cands, {"git-push"})
    assert dropped == 0
    assert len(out) == 2


def test_filter_empty_candidates():
    out, dropped = rc.apply_capability_filter([], {"git-push"})
    assert out == []
    assert dropped == 0


def test_filter_untagged_always_kept():
    # An untagged goal is never dropped even when the runner has zero caps.
    cands = [_cand("g-untagged"), _cand("g-aws", ["aws"])]
    out, dropped = rc.apply_capability_filter(cands, set())
    assert dropped == 1
    assert [c["goal"]["id"] for c in out] == ["g-untagged"]


def test_filter_multi_requirement_partial_satisfaction():
    # requires BOTH ml-deps AND gpu; runner has only ml-deps -> dropped.
    cands = [_cand("g-fw"), _cand("g-both", ["ml-deps", "gpu"])]
    out, dropped = rc.apply_capability_filter(cands, {"ml-deps", "git-push"})
    assert dropped == 1
    assert [c["goal"]["id"] for c in out] == ["g-fw"]


def test_filter_malformed_candidate_treated_as_untagged():
    # A non-dict candidate has no goal -> empty requirement -> kept (never crash).
    cands = ["not-a-dict", _cand("g-fw")]
    out, dropped = rc.apply_capability_filter(cands, set())
    assert dropped == 0
    assert len(out) == 2
