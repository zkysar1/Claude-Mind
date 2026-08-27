"""G1-G5 cross-world GRANT enforcement ().

Every guardrail gets a POSITIVE control (the thing it permits) beside its
negative one. A refusal test alone passes against a module that refuses
everything, which is the failure shape guard-3221 warns about: an assertion
that cannot distinguish "correctly blocked" from "inert and blocking all".
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import _grants as G  # noqa: E402


def _grant(src, dst, **kw):
    rec = {
        "grant_id": "grant-%s-%s" % (src, dst),
        "from_env": src,
        "to_env": dst,
        "status": "active",
        "origin_env": src,
        "approved_by": "owner@example",
        "approved_at": "2026-08-27T00:00:00",
    }
    rec.update(kw)
    return rec


# ── G1: default-private ─────────────────────────────────────────────────────

def test_g1_empty_store_denies():
    r = G.check_influence("world-a", "world-b", [])
    assert r["verdict"] == G.DENY
    assert r["guardrail"] == "G1"


def test_g1_absent_store_file_is_deny_not_unavailable(tmp_path):
    """A world that was never granted anything has no store file.

    That MUST be G1 DENY. If it came back UNAVAILABLE the caller fails open
    (guard-142) and default-private silently becomes default-public -- the
    single most dangerous confusion in this module.
    """
    r = G.evaluate("world-a", "world-b", tmp_path / "nope.jsonl")
    assert r["verdict"] == G.DENY
    assert r["fail_open"] is False


def test_g1_positive_control_granted_edge_allows(tmp_path):
    """Positive control: the deny tests above are not just a module that says no."""
    p = tmp_path / "grants.jsonl"
    p.write_text(json.dumps(_grant("world-a", "world-b")) + "\n", encoding="utf-8")
    r = G.evaluate("world-a", "world-b", p)
    assert r["verdict"] == G.ALLOW, r


# ── guard-142: gate malfunction fails OPEN, and is NOT a deny ───────────────

def test_malformed_store_is_unavailable_and_fails_open(tmp_path):
    p = tmp_path / "grants.jsonl"
    p.write_text("{not json at all\n", encoding="utf-8")
    r = G.evaluate("world-a", "world-b", p)
    assert r["verdict"] == G.UNAVAILABLE
    assert r["fail_open"] is True


def test_unavailable_is_distinct_from_deny(tmp_path):
    """The two failure directions must not collapse into one verdict."""
    good = tmp_path / "empty.jsonl"
    good.write_text("", encoding="utf-8")
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{oops\n", encoding="utf-8")
    assert G.evaluate("a", "b", good)["verdict"] == G.DENY
    assert G.evaluate("a", "b", bad)["verdict"] == G.UNAVAILABLE


# ── G3: human approval gate ────────────────────────────────────────────────

@pytest.mark.parametrize("approver", [None, "", "agent:zeta", "bot:runner", "system:loop"])
def test_g3_agent_approver_is_refused(approver):
    g = _grant("world-a", "world-b", approved_by=approver)
    r = G.check_influence("world-a", "world-b", [g])
    assert r["verdict"] == G.DENY
    assert r["guardrail"] == "G3", r


def test_g3_human_approver_is_accepted():
    """Positive control for the parametrized refusals above."""
    r = G.check_influence("world-a", "world-b", [_grant("world-a", "world-b")])
    assert r["verdict"] == G.ALLOW


def test_g3_first_grant_from_a_source_requires_human_approval():
    v = G.validate_new_grant(_grant("world-a", "world-b", approved_by="agent:zeta"), [])
    assert any("G3" in x for x in v), v


def test_g3_first_grant_requires_approved_at():
    bad = _grant("world-a", "world-b")
    bad.pop("approved_at")
    assert any("approved_at" in x for x in G.validate_new_grant(bad, []))


# ── G4: no transitive influence ────────────────────────────────────────────

def test_g4_transitivity_is_not_representable():
    """THE core G4 assertion: A->B and B->C must never imply A->C."""
    grants = [_grant("world-a", "world-b"), _grant("world-b", "world-c")]
    r = G.check_influence("world-a", "world-c", grants)
    assert r["verdict"] == G.DENY
    assert r["guardrail"] == "G1"
    # Positive control: both legs the chain IS made of still work, so the
    # denial above is about transitivity and not about a broken lookup.
    assert G.check_influence("world-a", "world-b", grants)["verdict"] == G.ALLOW
    assert G.check_influence("world-b", "world-c", grants)["verdict"] == G.ALLOW


def test_g4_explicit_direct_grant_restores_the_hop():
    grants = [_grant("world-a", "world-b"), _grant("world-b", "world-c"),
              _grant("world-a", "world-c")]
    assert G.check_influence("world-a", "world-c", grants)["verdict"] == G.ALLOW


def test_g4_cycle_detected_at_creation():
    existing = [_grant("world-a", "world-b"), _grant("world-b", "world-c")]
    v = G.validate_new_grant(_grant("world-c", "world-a"), existing)
    assert any("G4" in x and "cycle" in x for x in v), v


def test_g4_self_edge_is_a_cycle():
    assert G.detect_cycle([], ("world-a", "world-a")) == ["world-a", "world-a"]


def test_g4_acyclic_addition_is_permitted():
    """Positive control: cycle detection does not simply refuse every edge."""
    existing = [_grant("world-a", "world-b")]
    assert G.validate_new_grant(_grant("world-b", "world-c"), existing) == []


def test_g4_depth_cap_blocks_a_laundered_chain():
    grants = [_grant("world-a", "world-b")]
    r = G.check_influence("world-a", "world-b", grants, depth=2)
    assert r["verdict"] == G.DENY
    assert r["guardrail"] == "G4"
    # Positive control: the same edge at legal depth is allowed.
    assert G.check_influence("world-a", "world-b", grants, depth=1)["verdict"] == G.ALLOW


# ── G5: provenance ─────────────────────────────────────────────────────────

def test_g5_stamp_sets_all_four_provenance_fields():
    out = G.stamp_provenance({"body": "x"}, "world-a",
                             source_trace_ids=["t1"], contributor_ids=["c1"])
    assert out["origin_env"] == "world-a"
    assert out["influence_chain"] == ["world-a"]
    assert out["source_trace_ids"] == ["t1"]
    assert out["contributor_ids"] == ["c1"]
    assert out["body"] == "x"


def test_g5_stamp_does_not_mutate_the_caller_payload():
    payload = {"body": "x"}
    G.stamp_provenance(payload, "world-a")
    assert payload == {"body": "x"}, "stamp mutated its input"


def test_g5_origin_env_must_match_from_env():
    bad = _grant("world-a", "world-b", origin_env="world-z")
    assert any("G5" in x for x in G.validate_new_grant(bad, []))


def test_g5_matching_origin_env_is_accepted():
    assert G.validate_new_grant(_grant("world-a", "world-b"), []) == []


# ── status handling ────────────────────────────────────────────────────────

@pytest.mark.parametrize("status", ["revoked", "pending", "expired", ""])
def test_non_active_grant_denies(status):
    g = _grant("world-a", "world-b", status=status)
    assert G.check_influence("world-a", "world-b", [g])["verdict"] == G.DENY


def test_self_influence_is_not_cross_world():
    assert G.check_influence("world-a", "world-a", [])["verdict"] == G.ALLOW


# ── Subtree scoping () ─────────────────────────────────────────────

def test_root_scope_covers_everything():
    assert G.covers(G.ROOT_SCOPE, "intelligence/agent/memory") is True
    assert G.covers("/", "anything") is True


def test_absent_scope_widens_to_root_rather_than_narrowing_to_nothing():
    """A pre-scoping grant must not silently lose all its access."""
    assert G.covers(None, "intelligence/agent") is True
    assert G.check_scope({}, "intelligence/agent")["verdict"] == G.ALLOW


def test_scope_covers_the_node_itself_and_its_descendants():
    assert G.covers("intelligence/agent", "intelligence/agent") is True
    assert G.covers("intelligence/agent", "intelligence/agent/memory") is True
    assert G.covers("intelligence/agent", "intelligence/agent/memory/rag") is True


def test_scope_does_not_cover_a_sibling_sharing_a_string_prefix():
    """THE over-granting case. `intelligence/agent` must NOT reach
    `intelligence/agent-secrets` — a different subtree that merely starts with
    the same characters. A bare startswith() would grant it silently."""
    assert G.covers("intelligence/agent", "intelligence/agent-secrets") is False
    assert G.covers("intelligence/agent", "intelligence/agentic") is False


def test_scope_does_not_cover_an_unrelated_subtree_or_a_parent():
    assert G.covers("intelligence/agent", "performance/latency") is False
    assert G.covers("intelligence/agent", "intelligence") is False


def test_scope_normalization_is_separator_insensitive_at_the_edges():
    assert G.normalize_scope("/intelligence/agent/") == "intelligence/agent"
    assert G.normalize_scope("") == G.ROOT_SCOPE
    assert G.normalize_scope(None) == G.ROOT_SCOPE


def test_check_influence_honours_scope_when_a_node_is_named():
    g = _grant("world-a", "world-b", scope="intelligence/agent")
    inside = G.check_influence("world-a", "world-b", [g], node_key="intelligence/agent/memory")
    outside = G.check_influence("world-a", "world-b", [g], node_key="performance/latency")
    assert inside["verdict"] == G.ALLOW, inside
    assert outside["verdict"] == G.DENY, outside
    # Positive control: with no node named, the edge itself still authorizes.
    assert G.check_influence("world-a", "world-b", [g])["verdict"] == G.ALLOW


def test_scope_is_checked_after_authorization_not_instead_of_it():
    """An UNAUTHORIZED edge must deny on G1/G3, never pass because the node
    happened to fall inside some scope."""
    g = _grant("world-a", "world-b", scope=G.ROOT_SCOPE, approved_by="agent:zeta")
    r = G.check_influence("world-a", "world-b", [g], node_key="anything")
    assert r["verdict"] == G.DENY
    assert r["guardrail"] == "G3", r


# ── Grant-store-as-data addressing () ──────────────────────────────

def test_index_groups_by_edge_and_keeps_multiple_grants_per_edge():
    """One edge may carry several grants at different scopes. Collapsing them to
    a single value would make whichever landed last silently win."""
    a = _grant("world-a", "world-b", grant_id="g1", scope="intelligence")
    b = _grant("world-a", "world-b", grant_id="g2", scope="performance")
    idx = G.build_index([a, b, _grant("world-c", "world-b")])
    assert len(idx[("world-a", "world-b")]) == 2
    assert len(idx[("world-c", "world-b")]) == 1


def test_index_path_and_scan_path_agree():
    """The index changes only HOW the direct edge is found. If the two paths can
    disagree, the optimisation silently becomes an authorization change."""
    grants = [_grant("world-a", "world-b", scope="intelligence"),
              _grant("world-b", "world-c"),
              _grant("world-a", "world-d", approved_by="agent:zeta")]
    idx = G.build_index(grants)
    cases = [("world-a", "world-b", "intelligence/agent"),
             ("world-a", "world-b", "performance/x"),
             ("world-a", "world-c", None),
             ("world-b", "world-c", None),
             ("world-a", "world-d", None),
             ("world-z", "world-b", None)]
    for src, dst, node in cases:
        scan = G.check_influence(src, dst, grants, node_key=node)
        indexed = G.check_influence(src, dst, grants, node_key=node, index=idx)
        assert scan["verdict"] == indexed["verdict"], (src, dst, node, scan, indexed)
        assert scan.get("guardrail") == indexed.get("guardrail"), (src, dst, node)


def test_query_filters_by_edge_and_status():
    grants = [_grant("world-a", "world-b"),
              _grant("world-a", "world-c", status="revoked"),
              _grant("world-b", "world-c")]
    assert len(G.query_grants(grants, from_env="world-a")) == 1          # revoked excluded
    assert len(G.query_grants(grants, from_env="world-a", status=None)) == 2
    assert len(G.query_grants(grants, to_env="world-c", status=None)) == 2
    assert G.query_grants(grants, from_env="nobody") == []


def test_query_covering_selects_grants_whose_scope_reaches_a_node():
    grants = [_grant("world-a", "world-b", grant_id="g1", scope="intelligence/agent"),
              _grant("world-a", "world-b", grant_id="g2", scope="performance")]
    hit = G.query_grants(grants, covering="intelligence/agent/memory")
    assert [g["grant_id"] for g in hit] == ["g1"]
    # Positive control: a root-scoped grant reaches anything.
    root = _grant("world-a", "world-b", grant_id="g3", scope=G.ROOT_SCOPE)
    assert len(G.query_grants(grants + [root], covering="anywhere/at/all")) == 1


def test_readable_scopes_are_normalized_and_deduped():
    grants = [_grant("world-a", "world-b", grant_id="g1", scope="/intelligence/agent/"),
              _grant("world-a", "world-b", grant_id="g2", scope="intelligence/agent"),
              _grant("world-a", "world-b", grant_id="g3", scope="performance")]
    assert G.readable_scopes(grants, "world-a", "world-b") == [
        "intelligence/agent", "performance"]
