"""Cross-world GRANT entity + G1-G5 enforcement (, ).

A GRANT is a directed edge authorizing one world to INFLUENCE another:

    {from_env} --influence--> {to_env}

"Influence" means a write that lands in the target world's stores or work
queue (a board cross-post, an injected goal, a pattern emission). It does NOT
mean READING what a peer already published to a shared channel -- `peer_retrieve`
documents at length why a read needs no grant, and that posture is unchanged
here. This module governs the WRITE direction only.

The five cross-world guardrails (`core/config/conventions/world-contract.md`):

  G1 default-private   A world starts with ZERO grants. Absence of a matching
                       grant is a DENY, not an "unknown". This is the whole
                       reason the entity exists, and it is why `evaluate()`
                       never returns ALLOW on an empty store.
  G2 goal sandboxing   Out of scope here -- G2 is enforced by the target world's
                       execution context, not by the grant edge. Named so a
                       reader does not conclude it was forgotten.
  G3 human approval    The FIRST grant from a given `from_env` requires explicit
                       human approval. An agent may not consent on its owner's
                       behalf, so `approved_by` must not be an agent identity.
  G4 no-transitive     A->B and B->C never imply A->C. Enforced structurally:
                       `evaluate()` matches only a DIRECT edge, so transitivity
                       is unrepresentable rather than merely forbidden. Cycle
                       detection + a depth cap guard the chain a payload carries.
  G5 provenance        Every influencing payload carries origin_env, the
                       influence_chain that produced it, and its source refs.

WHY DENY AND UNAVAILABLE ARE DIFFERENT VERDICTS (guard-142)
-----------------------------------------------------------
guard-142 requires that a gate which can refuse work fail OPEN on its OWN
dependency errors -- an unreadable store, a missing parser, zero sources
scanned. G1 requires that a missing grant fail CLOSED. Those are not in
conflict, but they are trivially confusable, and collapsing them breaks the
gate in whichever direction the author happened to pick:

  DENY        the gate READ the store successfully and policy says no.
              Fail-closed. This is G1 working.
  UNAVAILABLE the gate could not read the store / could not decide.
              Fail-OPEN: the caller proceeds and logs. A gate that blocks
              real work because of its own bug is worse than the problem
              it was meant to catch.

An empty-but-readable store is DENY (G1). An unreadable store is UNAVAILABLE.
The discriminator is whether the read SUCCEEDED, never whether it returned rows
-- that is the same "an empty result is not a signal" trap as guard-2421, moved
from probes to policy.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ── Verdicts ────────────────────────────────────────────────────────────────
ALLOW = "allow"
DENY = "deny"
UNAVAILABLE = "unavailable"

# G4 depth cap: how long an influence_chain may get before a payload is
# refused. 1 = direct influence only. The cap exists so a chain cannot be
# grown one legal hop at a time into de-facto transitivity.
MAX_INFLUENCE_DEPTH = 1

# Identities that may NOT satisfy G3. An agent approving its own world's
# inbound grant is the exact thing G3 forbids, so the check is on the SHAPE
# of the approver, not on a list of names we would have to keep current.
_AGENT_APPROVER_PREFIXES = ("agent:", "bot:", "system:")

REQUIRED_FIELDS = ("grant_id", "from_env", "to_env", "status", "origin_env")


def _is_human_approver(approved_by: Any) -> bool:
    """G3: an approver must be a human identity, not an agent one.

    Shape-based, deliberately. A name allowlist would need updating every time
    an agent is added and would fail OPEN on the one it had not heard of.
    """
    if not isinstance(approved_by, str) or not approved_by.strip():
        return False
    low = approved_by.strip().lower()
    return not low.startswith(_AGENT_APPROVER_PREFIXES)


def _active(grant: Dict[str, Any]) -> bool:
    return isinstance(grant, dict) and grant.get("status") == "active"


def detect_cycle(edges: Sequence[Tuple[str, str]],
                 new_edge: Tuple[str, str]) -> Optional[List[str]]:
    """G4: would `new_edge` close a cycle over `edges`? Returns the path or None.

    A cycle means world A can influence itself through a chain, which makes
    every rate limit and depth cap unbounded in practice. Detected at GRANT
    CREATION time (cheap, one-shot) rather than at every use.
    """
    src, dst = new_edge
    if src == dst:
        return [src, dst]
    adj: Dict[str, List[str]] = {}
    for a, b in edges:
        adj.setdefault(a, []).append(b)
    # Is `src` already reachable FROM `dst`? If so, adding src->dst closes a loop.
    stack: List[Tuple[str, List[str]]] = [(dst, [src, dst])]
    seen = {dst}
    while stack:
        node, path = stack.pop()
        if node == src:
            return path
        for nxt in adj.get(node, []):
            if nxt == src:
                return path + [nxt]
            if nxt not in seen:
                seen.add(nxt)
                stack.append((nxt, path + [nxt]))
    return None


def validate_new_grant(new: Dict[str, Any],
                       existing: Iterable[Dict[str, Any]]) -> List[str]:
    """Return a list of G-violations for a proposed grant. Empty list == valid."""
    violations: List[str] = []
    if not isinstance(new, dict):
        return ["G0: grant record is not an object"]

    for f in REQUIRED_FIELDS:
        if not new.get(f):
            violations.append("G0: missing required field %r" % f)

    src, dst = new.get("from_env"), new.get("to_env")
    existing = [g for g in (existing or []) if isinstance(g, dict)]

    # G3 -- first grant from this source needs a human approval.
    prior_from_src = [g for g in existing
                      if g.get("from_env") == src and _active(g)]
    if not prior_from_src:
        if not _is_human_approver(new.get("approved_by")):
            violations.append(
                "G3: first grant from %r requires explicit human approval; "
                "approved_by=%r is absent or is an agent identity"
                % (src, new.get("approved_by")))
        if not new.get("approved_at"):
            violations.append("G3: first grant from %r requires approved_at" % src)

    # G4 -- cycle detection at creation.
    if src and dst:
        edges = [(g["from_env"], g["to_env"]) for g in existing
                 if _active(g) and g.get("from_env") and g.get("to_env")]
        cycle = detect_cycle(edges, (src, dst))
        if cycle:
            violations.append("G4: grant would close an influence cycle: %s"
                              % " -> ".join(cycle))

    # G5 -- provenance must name the world the grant originates from.
    if new.get("origin_env") and src and new["origin_env"] != src:
        violations.append(
            "G5: origin_env %r does not match from_env %r"
            % (new["origin_env"], src))

    return violations


def check_influence(from_env: str, to_env: str,
                    grants: Sequence[Dict[str, Any]],
                    *, depth: int = 1, node_key=None, index=None) -> Dict[str, Any]:
    """Decide whether `from_env` may influence `to_env`, given loaded grants.

    Pure: takes grants as DATA. Every failure mode that belongs to the STORE
    (unreadable, unparseable) is the caller's to turn into UNAVAILABLE -- see
    `evaluate()`. Reaching this function at all means the read succeeded, so
    every verdict here is a genuine policy verdict.
    """
    if not from_env or not to_env:
        return {"verdict": DENY, "guardrail": "G0",
                "reason": "from_env and to_env are both required"}

    if from_env == to_env:
        return {"verdict": ALLOW, "guardrail": None,
                "reason": "a world influencing itself is not cross-world"}

    # G4 depth cap -- checked before the edge lookup so a legal edge cannot
    # launder an over-deep chain.
    if depth > MAX_INFLUENCE_DEPTH:
        return {"verdict": DENY, "guardrail": "G4",
                "reason": "influence depth %d exceeds cap %d (no transitive "
                          "influence: A->B->C requires an explicit A->C grant)"
                          % (depth, MAX_INFLUENCE_DEPTH)}

    # G4 no-transitive: only a DIRECT edge counts. There is deliberately no
    # graph traversal here -- transitivity is unrepresentable, not merely denied.
    # An index, when supplied, changes only HOW the direct edge is found; the
    # candidate set it yields is identical to the scan's (pinned by a test).
    if index is not None:
        candidates = index.get((from_env, to_env), [])
    else:
        candidates = [g for g in (grants or []) if isinstance(g, dict)
                      and g.get("from_env") == from_env
                      and g.get("to_env") == to_env]
    for g in candidates:
        if True:
            if not _active(g):
                return {"verdict": DENY, "guardrail": "G1",
                        "grant_id": g.get("grant_id"),
                        "reason": "grant %r exists but status=%r (not active)"
                                  % (g.get("grant_id"), g.get("status"))}
            if not _is_human_approver(g.get("approved_by")):
                return {"verdict": DENY, "guardrail": "G3",
                        "grant_id": g.get("grant_id"),
                        "reason": "grant %r lacks a human approver "
                                  "(approved_by=%r)"
                                  % (g.get("grant_id"), g.get("approved_by"))}
            # Scope is checked LAST, after G1/G3/status: an edge must be
            # authorized before asking how much of the tree it reaches.
            if node_key is not None:
                sc = check_scope(g, node_key)
                if sc["verdict"] != ALLOW:
                    sc["grant_id"] = g.get("grant_id")
                    return sc
            return {"verdict": ALLOW, "guardrail": None,
                    "grant_id": g.get("grant_id"),
                    "scope": normalize_scope(g.get("scope")),
                    "reason": "active human-approved grant %r"
                              % g.get("grant_id")}

    # G1 -- default-private. The store READ fine and holds no such edge.
    return {"verdict": DENY, "guardrail": "G1",
            "reason": "no active grant %s -> %s; worlds are default-private "
                      "(zero-grant), so absence is a denial, not an unknown"
                      % (from_env, to_env)}


# ── Subtree scoping () ──────────────────────────────────────────────
# A grant may carry a `scope`: the knowledge-tree node key it authorizes, which
# covers that node AND its descendants. ROOT_SCOPE grants the whole tree, and an
# ABSENT scope means the same — a grant written before scoping existed must not
# silently narrow to nothing, so absence widens rather than restricts. That is the
# one place in this module where absence is permissive, and it is deliberate: the
# authorization decision (may A influence B at all?) is already made by
# check_influence under G1 default-deny. Scope only narrows an ALREADY-GRANTED
# edge, so a missing scope cannot escalate anyone's access.
ROOT_SCOPE = "/"
_SEP = "/"


def normalize_scope(scope):
    """Canonical form: ROOT_SCOPE, or a key with no leading/trailing separator."""
    if scope is None:
        return ROOT_SCOPE
    scope = str(scope).strip()
    if not scope or scope == ROOT_SCOPE:
        return ROOT_SCOPE
    return scope.strip(_SEP)


def covers(scope, node_key) -> bool:
    """Does `scope` cover `node_key` (the node itself or any descendant)?

    MATCHES ON THE SEPARATOR BOUNDARY, never on a bare string prefix. A bare
    `node_key.startswith(scope)` would make the scope `intelligence/agent` also
    cover `intelligence/agent-secrets`, which is a DIFFERENT sibling subtree --
    silent over-granting, and invisible because the common cases all look right.
    This codebase already carries the same lesson for tree key matching
    (test_tree_match_exact_key_separator); it is re-stated here because a scope
    is an AUTHORIZATION boundary, where over-matching grants access rather than
    merely returning an extra search hit.
    """
    scope = normalize_scope(scope)
    if scope == ROOT_SCOPE:
        return True
    if node_key is None:
        return False
    node_key = str(node_key).strip().strip(_SEP)
    if not node_key:
        return False
    return node_key == scope or node_key.startswith(scope + _SEP)


def check_scope(grant, node_key) -> Dict[str, Any]:
    """Scope half of the decision, split out so it is testable on its own."""
    scope = normalize_scope((grant or {}).get("scope"))
    if covers(scope, node_key):
        return {"verdict": ALLOW, "guardrail": None, "scope": scope,
                "reason": "scope %r covers %r" % (scope, node_key)}
    return {"verdict": DENY, "guardrail": "G1", "scope": scope,
            "reason": "grant scope %r does not cover node %r; a subtree grant "
                      "covers the node and its descendants only"
                      % (scope, node_key)}


# ── Grant-store-as-data addressing () ───────────────────────────────
# The registry this replaces was a hand-curated handful of entries, where a
# linear scan per decision is free. Per-CHARACTER grants are a different scale:
# the edge lookup runs on every influence decision, so it is indexed by the
# (from_env, to_env) pair rather than re-scanned. The index is BUILT FROM the
# same grant list, never stored alongside it — a cached index that can disagree
# with its source is a second source of truth, and this module's whole job is
# deciding authorization.


def build_index(grants: Sequence[Dict[str, Any]]) -> Dict[tuple, List[Dict[str, Any]]]:
    """Index grants by (from_env, to_env). Values are LISTS: one edge may carry
    several grants at different scopes, and collapsing them to one would make
    whichever happened to be last silently win."""
    idx: Dict[tuple, List[Dict[str, Any]]] = {}
    for g in grants or []:
        if not isinstance(g, dict):
            continue
        idx.setdefault((g.get("from_env"), g.get("to_env")), []).append(g)
    return idx


def query_grants(grants: Sequence[Dict[str, Any]], *, from_env=None, to_env=None,
                 covering=None, status="active") -> List[Dict[str, Any]]:
    """Filter grants as data. `status=None` means any status (audit reads).

    `covering` selects grants whose scope covers that node key — the query the
    read path needs to answer "which grants let this reader see this node?"
    """
    out = []
    for g in grants or []:
        if not isinstance(g, dict):
            continue
        if from_env is not None and g.get("from_env") != from_env:
            continue
        if to_env is not None and g.get("to_env") != to_env:
            continue
        if status is not None and g.get("status") != status:
            continue
        if covering is not None and not covers(g.get("scope"), covering):
            continue
        out.append(g)
    return out


def readable_scopes(grants: Sequence[Dict[str, Any]], from_env: str,
                    to_env: str) -> List[str]:
    """Normalized scopes `to_env` may read from `from_env`. ROOT_SCOPE present
    means the whole tree; the read path can then skip per-node checks."""
    return sorted({normalize_scope(g.get("scope"))
                   for g in query_grants(grants, from_env=from_env, to_env=to_env)})


def stamp_provenance(payload: Dict[str, Any], origin_env: str,
                     *, chain: Optional[Sequence[str]] = None,
                     source_trace_ids: Optional[Sequence[str]] = None,
                     contributor_ids: Optional[Sequence[str]] = None
                     ) -> Dict[str, Any]:
    """G5: attach cross-world provenance to an outbound payload.

    Returns a NEW dict. Never mutates the caller's payload -- a provenance
    stamp that silently edits its input is indistinguishable from the payload
    having been authored that way.
    """
    out = dict(payload or {})
    out["origin_env"] = origin_env
    out["influence_chain"] = list(chain or [origin_env])
    out["source_trace_ids"] = list(source_trace_ids or [])
    out["contributor_ids"] = list(contributor_ids or [])
    return out


def load_grants(path) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Read a JSONL grant store. Returns (grants, error).

    `error` is non-None ONLY for a genuine dependency failure (unreadable
    file, malformed line). A MISSING store file is NOT an error: a world that
    has never been granted anything correctly has no store, and that state
    must produce G1 DENY, not a fail-open UNAVAILABLE. Conflating the two is
    how default-private silently becomes default-public.
    """
    try:
        from pathlib import Path
        p = Path(path)
        if not p.exists():
            return [], None          # G1: absent store == zero grants
        rows: List[Dict[str, Any]] = []
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                rows.append(json.loads(line))
            except Exception as e:
                return [], "grant store line %d is not valid JSON: %s" % (i, e)
        return rows, None
    except Exception as e:                                # pragma: no cover
        return [], "grant store unreadable: %s" % e


def evaluate(from_env: str, to_env: str, store_path,
             *, depth: int = 1, node_key=None) -> Dict[str, Any]:
    """Full gate entry point: load + decide, with guard-142 fail-open.

    This is the function a caller should use. It is the ONLY place that can
    return UNAVAILABLE, because it is the only place that touches the store.
    """
    grants, err = load_grants(store_path)
    if err:
        return {"verdict": UNAVAILABLE, "guardrail": None, "reason": err,
                "fail_open": True}
    result = check_influence(from_env, to_env, grants, depth=depth, node_key=node_key)
    result["fail_open"] = False
    return result
