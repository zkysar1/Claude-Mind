"""Tests for _runner_capabilities ( per-runner capability filter).

Covers the three pure functions + the config-override precedence that make the
goal-selector capability gate safe: derive_runner_capabilities (probe + provides
+ lacks), goal_required_capabilities (explicit-only, conservative), and
goal_is_locally_executable (subset test, empty-requirement = always executable).
"""
import pytest
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


# ── per-box declaration surface: local-paths.conf () ───────────────
# `core/config/aspirations.yaml` is git-shared and `meta/config-overrides.yaml`
# is S3-shared, so neither can say "THIS box has a live Studio session".
# `local-paths.conf` is machine-local (gitignored + owncloud_sync._EXCLUDE_NAMES);
# these cover its translation into a config block and the two-layer merge.

def test_box_config_absent_keys_is_empty():
    # A conf with only path keys declares nothing -> {} so the fleet default and
    # probe behaviour are byte-identical on every box that never opts in.
    assert rc.box_config_from_conf({"WORLD_PATH": "/w", "META_PATH": "/m"}) == {}


def test_box_config_parses_comma_separated_tokens():
    cfg = rc.box_config_from_conf({
        rc.BOX_CONF_PROVIDES: "studio-session, product-runtime",
        rc.BOX_CONF_LACKS: "gpu",
    })
    assert cfg["provides"] == ["studio-session", "product-runtime"]
    assert cfg["lacks"] == ["gpu"]
    assert "probe" not in cfg


def test_box_config_probe_falsey_strings():
    for raw in ("false", "False", "0", "no", "OFF"):
        assert rc.box_config_from_conf({rc.BOX_CONF_PROBE: raw})["probe"] is False
    for raw in ("true", "1", "yes"):
        assert rc.box_config_from_conf({rc.BOX_CONF_PROBE: raw})["probe"] is True


def test_box_config_blank_and_malformed_are_safe():
    assert rc.box_config_from_conf({rc.BOX_CONF_PROVIDES: "  ,, "}) == {}
    assert rc.box_config_from_conf(None) == {}
    assert rc.box_config_from_conf("not-a-dict") == {}


def test_merge_unions_provides():
    merged = rc.merge_capability_config(
        {"provides": ["aws"]}, {"provides": ["studio-session"]})
    assert merged["provides"] == ["aws", "studio-session"]


def test_merge_dedups_across_layers():
    merged = rc.merge_capability_config(
        {"provides": ["aws"]}, {"provides": ["aws", "gpu"]})
    assert merged["provides"] == ["aws", "gpu"]


def test_merge_lacks_from_either_layer_wins_over_provides():
    # THE load-bearing case: a box must never be forced to claim a capability it
    # says it does not have. lacks unions, and derive applies it after provides.
    merged = rc.merge_capability_config(
        {"provides": ["gpu"]}, {"lacks": ["gpu"]})
    caps = rc.derive_runner_capabilities(merged, probe_fn=lambda: set())
    assert "gpu" not in caps
    # ...and symmetrically when the FLEET declares the lack.
    merged = rc.merge_capability_config(
        {"lacks": ["gpu"]}, {"provides": ["gpu"]})
    caps = rc.derive_runner_capabilities(merged, probe_fn=lambda: set())
    assert "gpu" not in caps


def test_merge_box_wins_on_probe():
    assert rc.merge_capability_config({"probe": True}, {"probe": False})["probe"] is False
    # fleet value survives when the box declares none
    assert rc.merge_capability_config({"probe": False}, {})["probe"] is False
    assert "probe" not in rc.merge_capability_config({}, {})


def test_merge_empty_layers_is_noop():
    assert rc.merge_capability_config({}, {}) == {}
    assert rc.merge_capability_config(None, None) == {}


def test_studio_session_only_reachable_via_box_declaration():
    # End-to-end shape of the real cc-05 case: studio-session is never probed,
    # so ONLY a per-box conf declaration can make a runner provide it.
    assert "studio-session" not in rc._probe_default_capabilities()
    conf = {"WORLD_PATH": "/w", rc.BOX_CONF_PROVIDES: "studio-session"}
    merged = rc.merge_capability_config({}, rc.box_config_from_conf(conf))
    caps = rc.derive_runner_capabilities(merged, probe_fn=lambda: {"git-push"})
    assert "studio-session" in caps
    # A box that does NOT declare it still never provides it.
    plain = rc.merge_capability_config({}, rc.box_config_from_conf({"WORLD_PATH": "/w"}))
    assert "studio-session" not in rc.derive_runner_capabilities(
        plain, probe_fn=lambda: {"git-push"})


# ---------------------------------------------------------------------------
# product-runtime vs a MULTI-ROOT AGENT_WRITE_PATH ()
#
# This module was already covered by the 38 tests above, including a probe call
# and a product-runtime assertion — but nothing exercised the ';'-separated
# form, so the probe called Path() on the WHOLE string, is_dir() was False, and
# the capability was never added on exactly the boxes that DO hold the product
# repo. Silent and INVERTED: goals tagged requires_capability:[product-runtime]
# read as not-my-lane there. File-level coverage was not path-level coverage.
# ---------------------------------------------------------------------------

def _probe_awp(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("AGENT_WRITE_PATH", raising=False)
    else:
        monkeypatch.setenv("AGENT_WRITE_PATH", value)
    return "product-runtime" in rc._probe_default_capabilities()


def test_multiroot_pre_fix_predicate_reproduces_the_bug(tmp_path):
    """POSITIVE CONTROL — pins the DIFFERENCE, so a revert reddens here first.

    Every other case below passes against a probe that never regressed; only
    this one distinguishes a working fix from a deleted one (guard-5501).
    """
    from pathlib import Path as _P
    a = tmp_path / "a"; b = tmp_path / "b"; a.mkdir(); b.mkdir()
    multi = f"{a};{b}"
    assert _P(multi).is_dir() is False, "old whole-string predicate must be False"
    assert any(p.strip() and _P(p.strip()).is_dir() for p in multi.split(";")) is True


def test_multiroot_grants_product_runtime(monkeypatch, tmp_path):
    a = tmp_path / "a"; b = tmp_path / "b"; a.mkdir(); b.mkdir()
    assert _probe_awp(monkeypatch, f"{a};{b}") is True


def test_multiroot_single_root_still_works(monkeypatch, tmp_path):
    a = tmp_path / "a"; a.mkdir()
    assert _probe_awp(monkeypatch, str(a)) is True


def test_multiroot_any_existing_root_suffices(monkeypatch, tmp_path):
    """The capability asserts the runtime is REACHABLE, not that every root exists."""
    b = tmp_path / "b"; b.mkdir()
    assert _probe_awp(monkeypatch, f"/nonexistent/xyz;{b}") is True


@pytest.mark.parametrize("tmpl", [" {a} ; {b} ", "{a};", ";{a}"])
def test_multiroot_whitespace_and_empty_parts_tolerated(monkeypatch, tmp_path, tmpl):
    """Mirrors _path_roots.compute_allowed_roots: split ';', strip, skip empties."""
    a = tmp_path / "a"; b = tmp_path / "b"; a.mkdir(); b.mkdir()
    assert _probe_awp(monkeypatch, tmpl.format(a=a, b=b)) is True


@pytest.mark.parametrize("value", ["", "/nonexistent/a;/nonexistent/b", ";", None])
def test_multiroot_absent_when_no_root_resolves(monkeypatch, value):
    assert _probe_awp(monkeypatch, value) is False
