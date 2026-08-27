"""POST /v1/tree/write — daemon writer for the private knowledge tree.

This is the headline gap the Mycelium refactor closes: before this endpoint,
the daemon could READ the tree (GET /v1/tree/find-node, /v1/tree/read) but
every WRITE still went through the CLI (core/scripts/tree.py via
tree-update.sh). This endpoint daemonises the mechanical write ops.

BYTE-COMPATIBILITY with the CLI write path is a hard requirement: the
on-disk _tree.yaml produced here must be byte-identical to what the CLI
produces, so a tree alternately written by the CLI and the daemon does not
churn the whole 270 KB file in git / .history on every write. Compatibility
holds because:

  1. Field computation mirrors core/scripts/tree.py exactly. The stable,
     pure helpers (apply_defaults, parse_value, _recompute_utility_ratio,
     _UTILITY_RATIO_FIELDS) are copied VERBATIM below with line refs, rather
     than imported — importing tree.py runs its module-top
     `TREE_PATH = str(WORLD_DIR / ...)`, which raises when WORLD_DIR is None
     (the daemon resolves paths per-request via agent_paths, not _paths
     globals; WORLD_DIR is unset in non-bound / test contexts). The
     byte-compat test (test_runtime_tree_write.py) runs the REAL CLI against
     a temp world and diffs the bytes, catching any drift in these copies.
     normalize_virtual_path / compute_child_path are reimplemented against
     ctx.paths.world (tenant-correct — the CLI versions read the module-global
     WORLD_DIR, wrong for a daemon serving a non-bound agent).

  2. The YAML dump uses the EXACT params _fileops.locked_modify_yaml uses —
     Dumper=yaml.CSafeDumper, default_flow_style=False, allow_unicode=True,
     sort_keys=False (default width). NOT write_tree's params
     (default_flow_style=None, width=200): cmd_add_child / cmd_set /
     cmd_increment / cmd_remove_child — what agents actually run — all write
     through locked_modify_yaml, so THAT is the byte-compat target.

  3. The lock is path.with_suffix('.lock') (== <tree-dir>/_tree.lock), the
     SAME lock file _fileops.acquire_lock uses, so a daemon write and a
     concurrent CLI / fallback-path write serialise correctly.

  4. history.snapshot + changelog.append mirror _fileops.save_history +
     append_changelog (byte-identical snapshot filename + format).

SCOPE:
  op = add-child | set | increment | remove-child | propagate
       | reconcile-capabilities | reparent | batch | record-maintenance
  Optional `body` on add-child writes the node .md file (daemon-only
  convenience; the CLI add-child does NOT write .md — the /tree skill writes
  it separately via the Write tool. Omit `body` for byte-compat with the CLI.)

  add-child curation gates (2026-05-29): add-child now runs the dedup gate
  (tree-dedup-check.py) and the capability-aware child-limit gate
  (tree.py _enforce_child_limit) BEFORE writing — full CLI parity, so cutting
  the tree-add-child wrapper to daemon-only no longer silently drops those two
  safety enforcements. Both are pure gating (no _tree.yaml byte impact); a
  reject returns 409 instead of the CLI's sys.exit. Optional body fields:
  `no_dedup` (bypasses BOTH gates, matching the CLI's --no-dedup binding) and
  `accept_overflow` (a justification string that writes a tree-debt entry and
  allows an over-cap add, matching --accept-overflow).

  Batch-3 propagation core (2026-05-29): set field=confidence now propagates
  confidence up the parent chain + self-graduates the source node, and the
  standalone `propagate` / `reconcile-capabilities` ops are exposed. The engine
  (_propagate_in_memory + _graduate_node_level) is copied VERBATIM from
  core/scripts/tree.py; competence thresholds load from core/config/tree.yaml
  via _load_competence_config (mirrors the CLI's empty-dict-on-missing-key
  semantics exactly — see cross-check FINDING 1). All three write via the same
  _write_tree_locked (locked_modify_yaml params), so byte-compat holds by
  construction; verified by test_runtime_tree_write.py real-CLI diffs.

  Batch-3 reparent (2026-05-29): the `reparent` op mirrors cmd_reparent
  (tree.py:2392-2560) — move a node between parents, recompute the whole
  subtree's file paths + depths, dual-chain confidence propagation (new-parent
  chain via the node first, THEN the old-parent chain — order is byte-compat
  significant because shared ancestors are re-read on the second pass). All 9
  CLI sys.exit() validations become 4xx responses. Depth check reads D_max via
  _config_d_max(ctx) → _merged_config(ctx), which overlays meta/config-overrides
  `tree.*` entries on core/config/tree.yaml exactly as the CLI does (drift would
  silently change the depth gate vs the CLI). Physical .md file moves are NOT
  performed — reported in `file_moves` for the caller to execute, same as the
  CLI. The L1-pick-log telemetry (cmd_reparent S9) is DEFERRED: it appends to a
  separate file and does not affect _tree.yaml bytes.

  record-maintenance (2026-05-29): the `record-maintenance` op mirrors
  cmd_record_maintenance (tree.py:1355-1556) — stamps the top-level
  `maintenance` cadence block (last_maintain_at + optional last_backlog_mode_at
  / last_stop_mode_at) and auto-sets last_backlog_clear_at when post-run debt
  (distill + decompose candidate counts) has dropped to/below
  tree_debt_check.debt_threshold. The candidate-scan helpers
  (_get_distill/decompose/redistribute_candidates, _qualifies_for_decomposition,
  _get_all_leaves) are ctx-aware ports — config + node .md reads route through
  ctx.paths, never the CLI module-global WORLD_DIR/CONFIG_DIR. With
  with_run_record + a run_record_input object (the daemon analogue of the CLI's
  stdin JSON blob), a full run record is appended to
  world/tree-maintenance-log.jsonl via _fileops.locked_append_jsonl (the same
  byte-compatible path the CLI uses).

L1-pick-log telemetry IMPLEMENTED as of g-115-1943 (2026-07-12): add-child,
batch add-child (per op), and reparent all append via the _l1_pick.py SSOT
(shared with CLI tree.py) using ctx.paths.meta — fail-open, separate file, no
_tree.yaml byte impact. The deferral (2026-05-28..2026-07-12) silenced the S9
pick-rate signal for ~6 weeks while tree writes flowed through this module.

DELIBERATELY NOT YET IMPLEMENTED (tracked in mycelium-api-impl.md risk register):
  - the post-remove dangling-ref sweep (CLI-side cleanup).
  - _validate_no_surrogates pre-write check.
  - Multi-tenant commons topology (this writes ctx.paths.world only).
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .. import file_locks, history, changelog
from ..agent_paths import assert_not_cruft

from _fileops import _atomic_write_with_fallback  # noqa: E402
from _l1_pick import log_l1_pick  # noqa: E402  # S9 SSOT, 
from _growth_log import (  # noqa: E402  # tree_growth_log SSOT, 
    record_batch as _growth_record_batch,
    record_reparent as _growth_record_reparent,
)
# : DELEGATE the distill detector to the CLI rather than mirroring it.
# The module docstring above explains why the OTHER helpers are copied verbatim:
# importing tree.py runs its module-top `TREE_PATH = str(WORLD_DIR / ...)`, which
# raises when WORLD_DIR is None. That reason does NOT apply here, measured rather
# than assumed: tree.py is ALREADY imported in every live daemon process —
# endpoints/__init__.py:40 loads .world.tree_read, whose module top does
# `from tree import ...` (tree_read.py:44), and core/scripts/tree.py is a pinned
# entry on the daemon import surface (mind-api-code-changed.sh:165). So this
# import adds no new failure mode; it binds a module the process already holds.
from tree import (  # noqa: E402
    get_distill_candidates as _cli_get_distill_candidates,
)


VALID_OPS = {"add-child", "set", "increment", "remove-child",
             "propagate", "reconcile-capabilities", "reparent", "batch",
             "record-maintenance"}

# Fields copied verbatim from the add-child payload onto the new node, in the
# SAME order as core/scripts/tree.py cmd_add_child._do_add (insertion order
# matters for sort_keys=False byte-compat).
# : re-synced origin_goal_id + poignancy to match the CLI copy list.
# The CLI added both to cmd_add_child._do_add (origin_goal_id instrumentation;
# poignancy ) but this mirror lagged, so a daemon add-child payload
# carrying either field silently dropped it vs the CLI (byte-compat drift, same
# class as ). Enforced by
# core/scripts/tests/test_daemon_cli_mirror_parity.py.
# : node_type is intentionally NOT in this copy list — it is
# derive-always from child-presence at the create path (_apply_add_child,
# mirroring child_count). Do NOT re-add it; the copy-tuple parity test
# (test_daemon_cli_mirror_parity.py) pins this against the CLI cmd_add_child
# tuple, which also dropped it.
_CHILD_COPY_FIELDS = (
    "summary", "domain_confidence", "capability_level", "confidence",
    "article_count", "growth_state", "origin_goal_id",
    "poignancy",
)

# Mirror of core/scripts/tree.py:104-109. DO NOT edit independently — the
# byte-compat test diffs daemon output against the CLI, which uses this exact
# set to decide when to recompute utility_ratio on increment.
_UTILITY_RATIO_FIELDS = (
    "times_helpful",
    "times_inferred_helpful",
    "retrieval_count",
    "times_noise",
)


# ---------------------------------------------------------------------------
# Pure helpers — copied VERBATIM from core/scripts/tree.py (line refs noted).
# Drift is caught by test_runtime_tree_write.py's real-CLI byte-compat diff.
# ---------------------------------------------------------------------------

def _recompute_utility_ratio(node: Dict[str, Any]) -> None:
    """Mirror of tree.py:112-126."""
    rc = node.get("retrieval_count", 0)
    th = node.get("times_helpful", 0)
    tih = node.get("times_inferred_helpful", 0)
    node["utility_ratio"] = round((th + 0.5 * tih) / max(rc, 1), 4)
    ta = node.get("times_active", 0)
    tc = node.get("times_cited", 0)
    node["utility_ratio_v2"] = round(
        (th + 0.5 * tih + 0.25 * ta + 1.0 * tc) / max(rc + 1, 1), 4
    )


def _apply_defaults(node: Dict[str, Any]) -> Dict[str, Any]:
    """Mirror of tree.py:347-394. Returns a new dict (does not mutate)."""
    out = dict(node)
    if "article_count" not in out:
        out["article_count"] = 0
    if "growth_state" not in out:
        out["growth_state"] = "stable"
    children = out.get("children", [])
    if "node_type" not in out:
        out["node_type"] = "interior" if children else "leaf"
    if "capability_level" not in out:
        out["capability_level"] = "EXPLORE"
    if "retrieval_count" not in out:
        out["retrieval_count"] = 0
    if "times_helpful" not in out:
        out["times_helpful"] = 0
    if "times_noise" not in out:
        out["times_noise"] = 0
    if "utility_ratio" not in out:
        out["utility_ratio"] = 0.0
    # : mirror tree.py apply_defaults' trailing fields (poignancy
    # , last_relevant_at ) — inserted before last_updated in
    # CLI order. This mirror had lagged tree.py:347-394, so daemon-created
    # child nodes dropped both keys vs the CLI (byte-compat drift at the first
    # post-utility_ratio key).
    if "poignancy" not in out:
        out["poignancy"] = None
    if "last_relevant_at" not in out:
        out["last_relevant_at"] = None
    # valid_from / valid_to (, BRD Gap 5): mirror of tree.py
    # apply_defaults' bi-temporal read-safe null defaults. Keep in lockstep with
    # the CLI (byte-compat parity test asserts identical ordered out[] keys).
    if "valid_from" not in out:
        out["valid_from"] = None
    if "valid_to" not in out:
        out["valid_to"] = None
    return out


def _parse_value(value_str: str) -> Any:
    """Mirror of tree.py:1178-1213 (case-insensitive bools/null)."""
    lowered = value_str.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered == "null" or lowered == "none":
        return None
    if value_str == "[]":
        return []
    if value_str.startswith("{") or value_str.startswith("["):
        try:
            return json.loads(value_str)
        except json.JSONDecodeError:
            pass
    try:
        return int(value_str)
    except ValueError:
        pass
    try:
        return float(value_str)
    except ValueError:
        pass
    return value_str


# ---------------------------------------------------------------------------
# Path helpers (reimplemented tenant-correct against ctx.paths.world; mirror
# core/scripts/tree.py normalize_virtual_path / compute_child_path for the
# virtual-input case — guarded by test_runtime_tree_write.py byte-compat test)
# ---------------------------------------------------------------------------

def _normalize_virtual_path(raw_path: str, world_path: Path) -> str:
    """Mirror tree.normalize_virtual_path, but strip THIS request's world_path
    (not the CLI module-global WORLD_DIR). The META_DIR branch is omitted —
    tree node files never live under meta."""
    if not raw_path:
        return raw_path
    path = raw_path.replace("\\", "/")
    while "//" in path:
        path = path.replace("//", "/")
    path = path.rstrip("/")
    world_str = str(world_path).replace("\\", "/").rstrip("/")
    if path.startswith(world_str + "/"):
        path = "world/" + path[len(world_str) + 1:]
    elif path.lower().startswith(world_str.lower() + "/"):
        path = "world/" + path[len(world_str) + 1:]
    if path.startswith("knowledge/"):
        path = "world/" + path
    if path.endswith(".md") and not (
        path.startswith("world/") or path.startswith("meta/")
    ):
        path = "world/knowledge/tree/" + path
    return path


def _compute_child_path(parent_file: str, slug: str, world_path: Path) -> str:
    """Mirror tree.compute_child_path: strip .md from parent, append slug.md."""
    parent_dir = parent_file[:-3] if parent_file.endswith(".md") else parent_file
    return _normalize_virtual_path(parent_dir + "/" + slug + ".md", world_path)


def _resolve_node_md(virtual_file: str, world_path: Path) -> Path:
    """Resolve a virtual `world/...` node-file path to a concrete path under
    THIS request's world. Used when add-child carries a `body` and by the
    g-115-4140 body-presence advisory."""
    vf = virtual_file.replace("\\", "/")
    if vf.startswith("world/"):
        return world_path / vf[len("world/"):]
    return world_path / vf


def _durability_witness(path: Path, key: str, expectations: Dict[str, Any],
                        write_stamp: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """POST-write durability probe for a merge-registered tree write ().

    Distinct from the g-115-7816 STRUCTURAL post-condition, which guard-3150
    pins BEFORE the write. That check asks "does the in-memory tree reflect what
    the op claimed?" -- answerable pre-write, so writing first would be pure
    downside. THIS check asks "did the write PERSIST?", which has no pre-write
    form at all, so guard-3150's remedy is unavailable by construction rather
    than declined. The two must stay separate: merging them would drag the
    structural assertion after the write and re-open what guard-3150 closed.

    WITNESS FIELDS MUST BE MERGE *INPUTS*, NEVER *OUTPUTS* (guard-5212). For
    merge_tree, `_rebuild_tree_structure` unconditionally recomputes children /
    child_count / node_type / depth from each node's `parent` on EVERY merge, so
    those four pass any read-back regardless of what happened to the write and
    have zero witnessing power. Callers must pass only INPUT fields: `parent`,
    or a per-node non-structural field.

    Peer-tolerant by construction: a MEMBERSHIP/FIELD assertion (not byte
    identity) survives a peer editing a different node, or a different field of
    the same node -- both merge cleanly and leave the asserted field untouched.
    A byte/etag comparison would alarm forever on any multi-writer store.

    Returns None when the write is witnessed, else a verdict dict. FAIL-OPEN:
    any probe error returns None -- the write already succeeded, and a probe
    fault must never be reported as data loss (guard-1562 class).
    """
    try:
        from storage_backend import get_backend  # noqa: E402
        be = get_backend()
        raw = be.read_authoritative_bytes(Path(path).resolve())
        if not raw:
            return None                      # unreadable -> fail open, not "lost"
        stored = yaml.safe_load(raw.decode("utf-8", "replace")) or {}
    except Exception:
        return None                          # fail-open (see docstring)
    node = ((stored.get("nodes") or {}).get(key)) or {}
    if not node:
        return {"verdict": "write_not_durable", "key": key, "reason": "node absent",
                "checked_fields": sorted(expectations)}
    mismatched = {f: {"expected": v, "authoritative": node.get(f)}
                  for f, v in expectations.items() if node.get(f) != v}
    if not mismatched:
        return None
    # A peer legitimately winning the same node+field with a strictly-newer
    # stamp is NOT a lost write. Keep the two verdicts distinct -- collapsing
    # them reintroduces the indistinguishability  refused to inherit.
    peer_stamp = node.get("last_updated")
    if write_stamp and peer_stamp and str(peer_stamp) > str(write_stamp):
        return {"verdict": "superseded_by_peer", "key": key,
                "mismatched": mismatched, "peer_last_updated": peer_stamp,
                "write_stamp": write_stamp}
    return {"verdict": "write_not_durable", "key": key, "mismatched": mismatched}


def _body_presence_warning(node_key: str, file_field, world_path: Path,
                           context: str):
    """ daemon mirror of tree.py warn_if_body_absent: return an
    advisory string (or None) when a registration/enrichment touches a node
    whose `file:` has no body on the LOCAL mirror. Advisory only — the handler
    attaches it to the response as `body_presence_warning` (additive key;
    consumers preserve unknown keys) and stderr-logs it; the write is NEVER
    refused (guard-1562: register-then-author is a legitimate flow for 8+
    callers). Local-mirror-only by design — under own-cloud read-through the
    store of record may hold a body this box never pulled, so the wording
    points at tree-body-presence-audit.py for the authoritative verdict.
    No _tree.yaml byte impact (byte-compat with the CLI is untouched).
    Fail-open."""
    try:
        if not file_field:
            return None
        if _resolve_node_md(str(file_field), world_path).exists():
            return None
        return ("body-presence g-115-4140 [{}]: node '{}' has file '{}' but "
                "no body on the LOCAL mirror — author the body now, or verify "
                "against the store of record via tree-body-presence-audit.py"
                .format(context, node_key, file_field))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Confidence propagation engine — copied VERBATIM from core/scripts/tree.py
# (line refs noted). Drift is caught by test_runtime_tree_write.py's real-CLI
# byte-compat diff. These power set(field=confidence), propagate, and
# reconcile-capabilities.
# ---------------------------------------------------------------------------

def _load_competence_config(ctx) -> Dict[str, Any]:
    """Mirror of tree.py:2163-2170, reading THIS request's project root.

    BYTE-COMPAT TRAP (cross-check FINDING 1): when core/config/tree.yaml EXISTS
    but `domain_health` or `competence_mapping` is absent, `.get(..., {})`
    returns an EMPTY dict — NOT the hardcoded defaults. The hardcoded default
    fires ONLY when the file is missing. Replicate exactly; a "helpful"
    fallback on the missing-key path would produce different capability_level
    strings on disk than the CLI. (In practice core/config/tree.yaml carries
    the mapping, so the empty-dict path never fires today.)
    """
    config_path = ctx.paths.project_root / "core" / "config" / "tree.yaml"
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        return config.get("domain_health", {}).get("competence_mapping", {})
    return {"EXPLORE": 0.25, "CALIBRATE": 0.50, "EXPLOIT": 0.75, "MASTER": 1.00}


# Only `tree.*`-prefixed override keys apply to tree.yaml — every other config
# is filtered by its own reader. Mirror of tree.py:42.
_OVERRIDE_FILE_PREFIX = "tree."


def _merged_config(ctx) -> Dict[str, Any]:
    """Mirror of tree.py:45-84, reading THIS request's project root + meta.

    Read core/config/tree.yaml and overlay any `tree.*`-prefixed entries from
    meta/config-overrides.yaml. Same dict-entry-with-`value` schema and the
    same typo-guard (`parts[-1] in target`) as the CLI — drift would change
    D_max and silently desync the reparent depth gate from the CLI. The
    `or {}` guards on an empty overrides file can never change byte-output for
    valid input; they only stop a daemon thread crashing on a degenerate file.
    """
    config_path = ctx.paths.project_root / "core" / "config" / "tree.yaml"
    with config_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    overrides_path = ctx.paths.meta / "config-overrides.yaml"
    if not overrides_path.exists():
        return cfg
    with overrides_path.open("r", encoding="utf-8") as f:
        override_doc = yaml.safe_load(f)
    overrides = (override_doc or {}).get("overrides", {})
    for dotted_key, entry in overrides.items():
        if not dotted_key.startswith(_OVERRIDE_FILE_PREFIX):
            continue
        in_file_path = dotted_key[len(_OVERRIDE_FILE_PREFIX):]
        val = entry["value"] if isinstance(entry, dict) else entry
        parts = in_file_path.split(".")
        target = cfg
        for p in parts[:-1]:
            if not isinstance(target, dict) or p not in target:
                target = None
                break
            target = target[p]
        if target is not None and isinstance(target, dict) and parts[-1] in target:
            target[parts[-1]] = val
    return cfg


def _config_d_max(ctx) -> int:
    """Mirror of tree.py:96-98. D_max from tree.yaml + meta overlay."""
    return _merged_config(ctx)["config"]["D_max"]


def _config_k_max(ctx) -> int:
    """Mirror of tree.py:101-107. K_max — max children per node (the Zhong K=4
    fan-out cap). Raises on missing key (rb-215/rb-275 anti-drift)."""
    return _merged_config(ctx)["config"]["K_max"]


def _config_leaf_cap(ctx) -> int:
    """Mirror of tree.py:120-133. Per-retrieval-subtree leaf cap, derived from
    K_max and D_retrieval (K_max^(D_retrieval-1) = 4^3 = 64 at defaults) unless
    an explicit config.leaf_cap overrides. DERIVED (not a 4th knob) so it can
    never drift from K_max / D_retrieval — single source of truth (rb-215)."""
    cfg = _merged_config(ctx)["config"]
    explicit = cfg.get("leaf_cap")
    if explicit is not None:
        return explicit
    return cfg["K_max"] ** (cfg["D_retrieval"] - 1)


def _graduate_node_level(node: Dict[str, Any], competence: Dict[str, Any]):
    """Verbatim mirror of tree.py:2173-2196. Recompute a node's
    capability_level from its own confidence vs the competence thresholds.
    Mutates `node` in place when the level changes; returns (old, new) or
    (None, None)."""
    conf = node.get("confidence")
    if conf is None or not isinstance(conf, (int, float)):
        return None, None
    levels_sorted = sorted(competence.items(), key=lambda x: x[1])
    if not levels_sorted:
        return None, None
    old_level = node.get("capability_level", "") or ""
    new_level = "EXPLORE"
    for level_name, threshold in levels_sorted:
        if conf >= threshold:
            new_level = level_name
    if old_level != new_level:
        node["capability_level"] = new_level
        return old_level, new_level
    return None, None


def _propagate_in_memory(nodes: Dict[str, Any], key: str,
                         competence: Dict[str, Any]):
    """Verbatim mirror of tree.py:2199-2299. Propagate confidence up the
    parent chain (and self-graduate the source). Mutates `nodes` in place;
    returns (ancestors_updated, capability_changes)."""
    levels_sorted = sorted(competence.items(), key=lambda x: x[1])

    if key not in nodes:
        return [], []

    ancestors_updated: List[Dict[str, Any]] = []
    capability_changes: List[Dict[str, Any]] = []

    # Self-graduation of the source (the ancestor loop skips index 0).
    src_old, src_new = _graduate_node_level(nodes[key], competence)
    if src_old is not None:
        capability_changes.append({
            "key": key,
            "old_level": src_old,
            "new_level": src_new,
        })

    result_chain: List[str] = []
    visited = set()
    current = key
    while current is not None:
        if current in visited or current not in nodes:
            break
        visited.add(current)
        result_chain.append(current)
        current = nodes[current].get("parent")

    if len(result_chain) < 2:
        return ancestors_updated, capability_changes

    for anc_key in result_chain[1:]:  # skip self (index 0) — handled above
        anc_node = nodes.get(anc_key)
        if not anc_node:
            break

        children_keys = anc_node.get("children", [])
        if not children_keys:
            continue

        child_confidences = []
        for ck in children_keys:
            if ck in nodes:
                c = nodes[ck].get("confidence")
                if c is not None and isinstance(c, (int, float)):
                    child_confidences.append(c)

        if not child_confidences:
            continue

        new_confidence = round(sum(child_confidences) / len(child_confidences), 4)
        old_confidence = anc_node.get("confidence")
        if old_confidence is not None:
            old_confidence = round(float(old_confidence), 4)

        old_level = anc_node.get("capability_level", "EXPLORE")
        new_level = "EXPLORE"
        for level_name, threshold in levels_sorted:
            if new_confidence >= threshold:
                new_level = level_name

        capability_changed = old_level != new_level

        anc_node["confidence"] = new_confidence
        anc_node["domain_confidence"] = new_confidence
        if capability_changed:
            anc_node["capability_level"] = new_level
        nodes[anc_key] = anc_node

        ancestors_updated.append({
            "key": anc_key,
            "old_confidence": old_confidence,
            "new_confidence": new_confidence,
            "capability_changed": capability_changed,
        })

        if capability_changed:
            capability_changes.append({
                "key": anc_key,
                "old_level": old_level,
                "new_level": new_level,
            })

        if old_confidence is not None and old_confidence == new_confidence:
            break

    return ancestors_updated, capability_changes


# ---------------------------------------------------------------------------
# add-child curation gates — dedup (tree-dedup-check.py) + child-limit
# (tree.py:147-272), reimplemented tenant-correct against ctx.paths. Both are
# PURE GATING — they decide WHETHER add-child proceeds, never the bytes written
# when it does — so they never affect _tree.yaml byte-compat. The CLI's
# sys.exit() rejections become 409 responses (a daemon thread must not exit).
# The dedup module is read-only and self-contained when handed a `tree` dict,
# so rather than importlib-load core/scripts/tree-dedup-check.py (whose
# module-top `TREE_PATH = WORLD_DIR / ...` raises when WORLD_DIR is None and
# reads the wrong tenant otherwise), the small pure logic is copied verbatim.
# ---------------------------------------------------------------------------

# Verbatim from tree-dedup-check.py:47-58.
_DEDUP_DEFAULT_CONFIG = {
    "sibling_overlap_threshold": 0.6,
    "min_tokens_for_overlap": 3,
    "enforce_from_depth": 2,
}
_DEDUP_STOPWORDS = frozenset([
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "has", "have", "in", "is", "it", "its", "of", "on", "or", "that",
    "the", "this", "to", "was", "were", "will", "with", "but", "not",
    "been", "being", "via", "over", "into", "than", "then",
])

# Verbatim from tree.py:158 — the hardcoded child-limit fallback when
# core/config/tree.yaml has no `child_limits:` section.
_CHILD_LIMITS_DEFAULT = {"mode": "block", "EXPLORE": 2, "CALIBRATE": 4,
                         "EXPLOIT": 8, "MASTER": 8}


def _tokenize(text: str) -> set:
    """Verbatim mirror of tree-dedup-check.py:82-87."""
    if not text:
        return set()
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return {t for t in tokens if len(t) >= 3 and t not in _DEDUP_STOPWORDS}


def _jaccard(a: set, b: set) -> float:
    """Verbatim mirror of tree-dedup-check.py:90-95."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _load_dedup_config(ctx) -> Dict[str, Any]:
    """Mirror of tree-dedup-check.py:61-69, reading THIS request's project
    root. NOT cached (per-tenant ctx; the file is tiny)."""
    config_path = ctx.paths.project_root / "core" / "config" / "tree.yaml"
    try:
        with config_path.open("r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        merged = dict(_DEDUP_DEFAULT_CONFIG)
        merged.update(cfg.get("dedup", {}) or {})
        return merged
    except (OSError, yaml.YAMLError):
        return dict(_DEDUP_DEFAULT_CONFIG)


def _check_dedup(parent_key: str, proposed_key: str, proposed_summary: str,
                 tree: Dict[str, Any], ctx) -> Dict[str, Any]:
    """Verbatim mirror of tree-dedup-check.py:98-198 check_dedup, operating on
    the in-memory `tree` (never re-reads _tree.yaml) and the tenant config.
    Returns a dict with at minimum `action` ('accept' | 'reject' | 'error')."""
    nodes = tree.get("nodes", {})
    parent = nodes.get(parent_key)
    if parent is None:
        return {"action": "error", "reason": "parent_not_found", "exit_code": 4}

    cfg = _load_dedup_config(ctx)
    parent_depth = parent.get("depth", 0)
    proposed_depth = parent_depth + 1

    if proposed_depth < cfg["enforce_from_depth"]:
        return {"action": "accept", "reason": "below_enforce_depth",
                "exit_code": 0, "proposed_depth": proposed_depth}

    proposed_key_lower = proposed_key.lower()
    children = parent.get("children", []) or []

    for sibling_key in children:
        if sibling_key.lower() == proposed_key_lower:
            return {"action": "reject", "reason": "exact_key_match",
                    "exit_code": 2, "sibling_key": sibling_key,
                    "suggestion": "update_existing"}

    proposed_tokens = _tokenize(proposed_summary)
    if len(proposed_tokens) < cfg["min_tokens_for_overlap"]:
        return {"action": "accept", "reason": "summary_too_short",
                "exit_code": 0}

    best_match = None
    best_overlap = 0.0
    threshold = cfg["sibling_overlap_threshold"]
    for sibling_key in children:
        sibling = nodes.get(sibling_key)
        if not sibling:
            continue
        sibling_tokens = _tokenize(sibling.get("summary") or "")
        if len(sibling_tokens) < cfg["min_tokens_for_overlap"]:
            continue
        overlap = _jaccard(proposed_tokens, sibling_tokens)
        if overlap > best_overlap:
            best_overlap = overlap
            best_match = sibling_key

    if best_match is not None and best_overlap >= threshold:
        return {"action": "reject", "reason": "summary_overlap", "exit_code": 3,
                "sibling_key": best_match, "overlap": round(best_overlap, 4),
                "threshold": threshold, "suggestion": "update_existing"}

    return {"action": "accept", "reason": "no_conflict", "exit_code": 0,
            "best_sibling_overlap": round(best_overlap, 4) if best_match else 0.0,
            "best_sibling_key": best_match}


def _load_child_limits(ctx) -> Dict[str, Any]:
    """Mirror of tree.py:147-166 (sans the process-wide cache — wrong for a
    multi-tenant daemon; the file is tiny so a per-call read is fine)."""
    cfg = dict(_CHILD_LIMITS_DEFAULT)
    config_path = ctx.paths.project_root / "core" / "config" / "tree.yaml"
    try:
        with config_path.open("r", encoding="utf-8") as f:
            file_cfg = yaml.safe_load(f) or {}
        cfg.update(file_cfg.get("child_limits", {}) or {})
    except (OSError, yaml.YAMLError):
        pass
    return cfg


def _write_tree_debt_entry(ctx, entry: Dict[str, Any]) -> None:
    """Append a tree-debt record to world/tree-debt.jsonl. Fail-soft, mirroring
    tree.py:169-185 — a broken debt log must never block an accept-overflow
    acknowledgement. Locked (the daemon is multi-threaded); tree-debt.jsonl is
    append-only telemetry, NOT byte-compat-tested, so the lock is a pure
    correctness win over the CLI's unlocked append."""
    try:
        debt_path = ctx.paths.world / "tree-debt.jsonl"
        with file_locks.locked(debt_path):
            with debt_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception:  # noqa: BLE001 — fail-soft by contract
        pass


def _check_child_limit(parent_key: str, parent_node: Dict[str, Any], ctx,
                       no_limit: bool,
                       accept_overflow: Optional[str]) -> Optional[Dict[str, Any]]:
    """Mirror of tree.py:188-247 _enforce_child_limit, RETURNING the reject dict
    instead of sys.exit(6). Returns None when the add may proceed (under limit,
    mode off, or accept_overflow acknowledged); returns the reject payload when
    mode=block and the cap is hit without an override."""
    if no_limit:
        return None
    cfg = _load_child_limits(ctx)
    mode = str(cfg.get("mode", "block")).lower()
    if mode == "off":
        return None
    level = (parent_node.get("capability_level") or "CALIBRATE").upper()
    limit = cfg.get(level)
    if not isinstance(limit, int) or limit <= 0:
        return None
    current = parent_node.get("child_count")
    if not isinstance(current, int):
        current = len(parent_node.get("children", []) or [])
    if current < limit:
        return None
    msg = {
        "context": "add-child", "parent": parent_key,
        "capability_level": level, "limit": limit, "current": current,
        "recommendation": "MERGE or DISTILL an existing child first, "
                          "or update-in-place",
    }
    if accept_overflow:
        _write_tree_debt_entry(ctx, {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "parent": parent_key, "capability_level": level, "limit": limit,
            "current": current, "context": "add-child",
            "justification": str(accept_overflow),
            "source_agent": _agent_name(ctx),
        })
        return None
    if mode == "block":
        return {"child_limit_reject": msg}
    # warn mode (legacy): allow.
    return None


# ---------------------------------------------------------------------------
# Request plumbing
# ---------------------------------------------------------------------------

def _tree_path(ctx) -> Path:
    return ctx.paths.world / "knowledge" / "tree" / "_tree.yaml"


def _agent_name(ctx) -> str:
    return (ctx.headers.get("x-mind-agent") or "").strip() or "system"


def _parse_body_json(body: bytes) -> Any:
    if not body:
        raise ValueError("empty body")
    return json.loads(body.decode("utf-8"))


def _read_tree_locked(path: Path) -> Dict[str, Any]:
    """Read _tree.yaml fresh (NOT via yaml_cache — the cache returns a shared
    copy under a no-mutation contract). CSafeLoader matches the CLI read path
    and is required for speed on the 270 KB tree; CSafeDumper output is
    byte-identical regardless of which loader produced the in-memory dict."""
    with path.open("r", encoding="utf-8") as f:
        return yaml.load(f, Loader=yaml.CSafeLoader)


def _write_tree_locked(path: Path, data: Dict[str, Any], base_dir: Path,
                       agent: str, summary: str) -> None:
    """Snapshot → atomic write → changelog, byte-compatible with
    _fileops.locked_modify_yaml. Caller MUST hold file_locks.locked(path)."""
    history.snapshot(path, base_dir, agent, summary=summary)

    def _write(handle):
        yaml.dump(data, handle, Dumper=yaml.CSafeDumper,
                  default_flow_style=False, allow_unicode=True,
                  sort_keys=False)

    assert_not_cruft(path.parent, "mkdir (tree_write._write_tree_locked)")
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_with_fallback(path, _write,
                                fallback_counter_key="daemon_tree_write")
    changelog.append(base_dir, agent, path, "edit",
                     summary=summary, lines_changed=len(data.get("nodes", {})))


# ---------------------------------------------------------------------------
# Mutation helpers (mirror core/scripts/tree.py cmd_* _do_* closures exactly)
# ---------------------------------------------------------------------------

def _read_in_flight_goal_id(world_path: Path, agent: str):
    """Daemon mirror of core/scripts/tree.py _read_in_flight_goal_id ().
    Reads the EXECUTING goal id from world/team-state.yaml in_flight for the
    per-request agent, producing the SAME origin signal the CLI records. Fail-open:
    any error (no agent, missing file, parse error, no in_flight) returns None.

    Unlike the CLI helper (which uses the _fileops._agent_name()/WORLD_DIR module
    globals), the daemon is a shared multi-agent process — agent identity is
    per-request, so it is passed explicitly alongside world_path. The OUTPUT is
    byte-identical to the CLI for the same (agent, team-state) pair."""
    try:
        if not agent or world_path is None:
            return None
        #  sharding: row-first read (world/team-state/agents/<agent>.yaml)
        # with core-file residual fallback for un-migrated deployments.
        from _team_state import read_agent_row
        status = read_agent_row(world_path, agent,
                                core_path=world_path / "team-state.yaml") or {}
        return (status.get("in_flight") or {}).get("goal_id") or None
    except Exception:
        return None


def _apply_add_child(tree: Dict[str, Any], parent_key: str,
                     child_data: Dict[str, Any], world_path: Path,
                     agent: str) -> Dict[str, Any]:
    nodes = tree["nodes"]
    parent = nodes[parent_key]
    parent_depth = parent.get("depth", 0)

    child_key = child_data["key"]
    child_node: Dict[str, Any] = {}
    if "file" not in child_data:
        child_node["file"] = _compute_child_path(parent.get("file", ""),
                                                  child_key, world_path)
    else:
        child_node["file"] = _normalize_virtual_path(child_data["file"], world_path)
    child_node["depth"] = parent_depth + 1
    child_node["parent"] = parent_key
    child_node["children"] = child_data.get("children", [])
    child_node["child_count"] = len(child_node["children"])
    # : node_type is derive-always from child-presence (mirror
    # child_count) — set at the create path, not copied (removed from
    # _CHILD_COPY_FIELDS) and not via _apply_defaults' fill-if-absent (which
    # also normalizes reads, masking on-disk drift). Sibling .
    child_node["node_type"] = "interior" if child_node["children"] else "leaf"
    for field in _CHILD_COPY_FIELDS:
        if field in child_data:
            child_node[field] = child_data[field]
    if "capability_level" not in child_node:
        parent_cl = parent.get("capability_level")
        if parent_cl:
            child_node["capability_level"] = parent_cl
    child_node = _apply_defaults(child_node)
    child_node["last_updated"] = date.today().isoformat()
    # origin_goal_id (): record the EXECUTING goal that created this
    # node, for the Gate D SPILL-1 spillover analysis. Caller-wins (an explicit
    # value copied above is preserved); otherwise auto-inject from team-state
    # in_flight. Mirrors tree.py cmd_add_child:1849-1852 ( / ).
    # Absent when no goal is executing (a manual add) — pre- readers
    # ignore the unknown field, so existing consumers parse unchanged.
    if "origin_goal_id" not in child_node:
        _origin = _read_in_flight_goal_id(world_path, agent)
        if _origin:
            child_node["origin_goal_id"] = _origin

    nodes[child_key] = child_node
    if child_key not in parent.get("children", []):
        if "children" not in parent:
            parent["children"] = []
        parent["children"].append(child_key)
        parent["child_count"] = len(parent["children"])
        # : a freshly-created parent that gains its first child via
        # add-child must flip leaf->interior. add-child updates child_count but
        # historically left node_type stale, mislabeling interior nodes as
        # leaves (misleads retrieval/decompose and trips tree-validate). Mirrors
        # tree.py cmd_add_child:1866-1867 and the cmd_reparent new-parent idiom.
        if parent.get("node_type") == "leaf":
            parent["node_type"] = "interior"
    nodes[parent_key] = parent

    tree["nodes"] = nodes
    tree["last_updated"] = date.today().isoformat()
    return child_node


# --- PROGRESSION calibration stamp (; mirror of tree.py) ------------
# MUST match tree.py._PROGRESSION_STAMP_FIELDS / _stamp_progression byte-for-byte
# (the byte-compat parity tests enforce CLI<->daemon equality). See tree.py for
# the full rationale: PROGRESSION-field writers stamp progression_updated_at so
# the own-cloud _tree.yaml merge keys the PROGRESSION LWW on a signal that
# advances on a calibration edit (last_updated deliberately does not, ).
_PROGRESSION_STAMP_FIELDS = ("confidence", "capability_level", "domain_confidence")


def _stamp_progression(node: Dict[str, Any]) -> None:
    """Bump the PROGRESSION calibration stamp (). Date-granular to
    match tree.py._stamp_progression exactly (byte-compat parity)."""
    node["progression_updated_at"] = date.today().isoformat()


# --- CALIBRATION stamp (; mirror of tree.py) -----------------------
# MUST match tree.py._CALIBRATION_STAMP_FIELDS / _stamp_calibration byte-for-byte
# (the byte-compat parity tests enforce CLI<->daemon equality). Deliberately a
# SEPARATE key from progression_updated_at — see tree.py for why one selector
# serving two field groups with different write triggers is unsound (guard-3358).
_CALIBRATION_STAMP_FIELDS = ("accuracy",)


def _stamp_calibration(node: Dict[str, Any]) -> None:
    """Bump the CALIBRATION stamp (). Date-granular to match
    tree.py._stamp_calibration exactly (byte-compat parity)."""
    node["calibration_updated_at"] = date.today().isoformat()


# --- BASE-class per-field amendment stamp (; mirror of tree.py) -----
# MUST match tree.py._NON_BASE_STAMP_FIELDS / _stamp_amendment byte-for-byte.
# See tree.py for the full rationale: BASE is the DEFAULT class, so it is written
# as the COMPLEMENT of the named classes (a field added later gets a stamp
# automatically), and the stamp is SECOND-granular unlike the two date-granular
# stamps above -- date granularity here would reproduce the very same-day tie the
# stamp exists to break.
_NON_BASE_STAMP_FIELDS = (
    "retrieval_count", "times_helpful", "times_noise",
    "times_inferred_helpful", "sample_size",
    "last_retrieved", "last_updated", "last_relevant_at",
    "progression_updated_at", "calibration_updated_at",
    "confidence", "capability_level", "domain_confidence",
    "accuracy",
    "children", "parent", "depth", "child_count", "node_type",
)


def _stamp_amendment(node: Dict[str, Any], field: str) -> None:
    """Record WHEN this BASE-class field was written, PER FIELD ().
    Mirrors tree.py._stamp_amendment exactly. This is the DAEMON copy and the
    LIVE path -- wrappers are daemon-only, so a stamp added to tree.py alone
    changes nothing at runtime while reading as correct in the diff (g-115-2422:
    the CLI dropped its stamp and the daemon kept it for 19 days)."""
    if field in _NON_BASE_STAMP_FIELDS:
        return
    stamps = node.get("amended_fields")
    if not isinstance(stamps, dict):
        stamps = {}
    stamps[field] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    node["amended_fields"] = {k: stamps[k] for k in sorted(stamps)}


def _apply_set(tree: Dict[str, Any], key: str, field: str, value: Any,
               world_path: Path) -> Dict[str, Any]:
    """Non-confidence field set. Confidence is rejected upstream (handler)."""
    nodes = tree["nodes"]
    node = nodes[key]
    v = _parse_value(value) if isinstance(value, str) else value
    if field == "file" and isinstance(v, str):
        v = _normalize_virtual_path(v, world_path)
    node[field] = v
    # : stamp the PROGRESSION calibration signal (mirror tree.py
    # cmd_set) so an own-cloud reconcile preserves a data-derived downgrade
    # instead of reverting it via _merge_field_progression's never-regress tie.
    if field in _PROGRESSION_STAMP_FIELDS:
        _stamp_progression(node)
    # : same for the CALIBRATION stamp (separate key, see above).
    if field in _CALIBRATION_STAMP_FIELDS:
        _stamp_calibration(node)
    # : BASE-class fields get a PER-FIELD amendment stamp, which is
    # what makes a BASE edit survive an own-cloud reconcile at all (mirror
    # tree.py cmd_set). No-ops for non-BASE fields.
    _stamp_amendment(node, field)
    #  (Option B): do NOT auto-bump per-node last_updated on a
    # metadata set. node .md front matter is the single source of truth
    # (); the _tree.yaml index last_updated is synced to it ONLY by
    # tree-front-matter-sync.py and at node creation. The old auto-bump here
    # marched the index AHEAD of the .md fm (the index-ahead drift class,
    #  audit). The CLI (tree.py cmd_set) dropped its stamp
    # 2026-06-28; this daemon path kept it for 19 days — the live write path,
    # so the Option B fix never actually shipped until the byte-compat parity
    # tests flagged the divergence (). Explicit
    # `set <k> last_updated <d>` still lands via node[field]=v above; the
    # index-level tree["last_updated"] below is intentionally retained.
    nodes[key] = node
    tree["nodes"] = nodes
    tree["last_updated"] = date.today().isoformat()
    return node


def _apply_increment(tree: Dict[str, Any], key: str, field: str) -> Dict[str, Any]:
    nodes = tree["nodes"]
    node = nodes[key]
    current = node.get(field, 0)
    if not isinstance(current, (int, float)):
        current = 0
    node[field] = current + 1
    if field in _UTILITY_RATIO_FIELDS:
        _recompute_utility_ratio(node)
    nodes[key] = node
    tree["last_updated"] = date.today().isoformat()
    return node


def _apply_remove_child(tree: Dict[str, Any], parent_key: str,
                        child_key: str) -> List[str]:
    """Returns the immediate-children list of child_key if removal would
    orphan a subtree (caller refuses); empty list means removal proceeded."""
    nodes = tree["nodes"]
    parent = nodes[parent_key]
    descendants = list(nodes.get(child_key, {}).get("children") or [])
    if descendants:
        return descendants
    children = parent.get("children", [])
    if child_key in children:
        children.remove(child_key)
    parent["children"] = children
    parent["child_count"] = len(children)
    if not children:
        # : last-child removal flips parent interior->leaf, mirroring
        # cmd_remove_child (tree.py ) + the daemon reparent old-parent
        # path (L1431-1432). Pre-fix the daemon updated child_count but left
        # node_type stale at "interior" on a now-childless parent -- a byte-compat
        # parity gap vs the CLI and a present-but-wrong node_type (
        # validate ERROR class). Shared by the standalone remove-child op AND the
        # batch remove-child sub-op (both call this helper).
        parent["node_type"] = "leaf"
    nodes[parent_key] = parent
    if child_key in nodes:
        del nodes[child_key]
    tree["nodes"] = nodes
    tree["last_updated"] = date.today().isoformat()
    return []


# ---------------------------------------------------------------------------
# Maintenance candidate-scan helpers — ctx-aware ports of core/scripts/tree.py
# (line refs noted). Copied rather than imported for the SAME reason as the
# pure helpers above: importing tree.py runs its module-top
# `TREE_PATH = str(WORLD_DIR / ...)`, which raises when WORLD_DIR is None (the
# daemon / pytest non-bound context). These power op="record-maintenance".
# Path resolution + config reads are threaded through ctx.paths, NOT the CLI's
# module-global WORLD_DIR / CONFIG_DIR / META_DIR. The distill + decompose
# counts gate maintenance.last_backlog_clear_at on disk (byte-compat) and feed
# the run-record JSONL; byte-compat is verified by test_runtime_tree_write.py.
# ---------------------------------------------------------------------------

def _config_threshold(ctx) -> int:
    """Mirror of tree.py:87-93 / _paths.py:87-93. decompose_threshold from
    core/config/tree.yaml + meta/config-overrides.yaml overlay (no fallback —
    a drifted default would silently change decomposition behavior)."""
    return _merged_config(ctx)["config"]["decompose_threshold"]


def _resolve_candidate_path(ctx, virtual_path: str) -> str:
    """ctx-aware mirror of _paths.resolve_file_path (core/scripts/_paths.py:328)
    INCLUDING the Windows `\\\\?\\` long-path wrap (_longpath_safe, _paths.py:315)
    that gates os.path.exists / line-count on deep tree-node paths. Returns the
    str form the CLI's `str(resolve_file_path(...))` produces. The `world/`
    branch is the only one tree nodes hit; the bare branch resolves to
    PROJECT_ROOT (NOT world) to mirror resolve_file_path exactly — unlike
    _resolve_node_md, which is add-child-specific and world-anchored."""
    if virtual_path.startswith("world/"):
        p = ctx.paths.world / virtual_path[len("world/"):]
    elif virtual_path.startswith("meta/"):
        p = ctx.paths.meta / virtual_path[len("meta/"):]
    else:
        p = ctx.paths.project_root / virtual_path
    s = str(p)
    if os.name == "nt" and len(s) >= 260 and not s.startswith("\\\\?\\"):
        return "\\\\?\\" + s
    return s


def _get_all_leaves(tree: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Mirror of tree.py:474-484. Leaf nodes (empty children), defaults applied.
    Only file/depth/key/growth_state are read downstream, so _apply_defaults
    field-ORDER is irrelevant here (this output is never dumped to _tree.yaml)."""
    nodes = tree.get("nodes", {})
    leaves = []
    for key, node in nodes.items():
        if not node.get("children", []):
            out = _apply_defaults(node)
            out["key"] = key
            leaves.append(out)
    return leaves


_CANONICAL_END_SECTIONS = {"verified values", "decision rules"}


def _qualifies_for_decomposition(abs_path: str):
    """Verbatim mirror of tree.py:692-747 (semantic-structure decompose gate).
    Returns (qualifies, skip_reason | None). skip_reason ∈
    {insufficient_sections, short_sections_avg}."""
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return True, None
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            body = text[end + 4:]
    lines = body.splitlines()
    section_starts = [
        (i, ln.lstrip("# ").strip())
        for i, ln in enumerate(lines)
        if ln.startswith("## ") and not ln.startswith("### ")
    ]
    non_end_sections = [
        (i, title) for (i, title) in section_starts
        if title.lower() not in _CANONICAL_END_SECTIONS
    ]
    if len(non_end_sections) < 4:
        return False, "insufficient_sections"
    all_starts = [i for (i, _) in section_starts]
    spans = []
    for i, _ in non_end_sections:
        next_starts = [s for s in all_starts if s > i]
        end_idx = next_starts[0] if next_starts else len(lines)
        spans.append(end_idx - i)
    avg_span = sum(spans) / len(spans) if spans else 0
    if avg_span <= 10:
        return False, "short_sections_avg"
    return True, None


def _subtree_leaf_counts(nodes):
    """Verbatim mirror of tree.py:917-954. Map every node key -> number of leaf
    descendants in its subtree. A leaf counts as 1 (itself); an interior node's
    count is the sum of its children's counts. Memoized post-order; cycle- and
    dangling-child-safe (a missing child contributes 0, a back-edge resolves to
    0 without raising). Drives the K=D=4 retrieval-locality decompose check
    (g-306-13)."""
    counts = {}
    visiting = set()

    def _count(key):
        if key in counts:
            return counts[key]
        node = nodes.get(key)
        if node is None:
            return 0
        children = node.get("children", [])
        if not children:
            counts[key] = 1
            return 1
        if key in visiting:
            return 0  # cycle back-edge — contribute 0, let the outer frame finish
        visiting.add(key)
        total = 0
        for child in children:
            total += _count(child)
        visiting.discard(key)
        counts[key] = total
        return total

    for k in nodes:
        _count(k)
    return counts


def _node_maintain_exempt(node):
    """Verbatim mirror of tree.py:974-990. Return the set of maintenance actions
    a node is durably exempted from via its optional per-node `maintain_exempt`
    field (g-115-1648). Accepts a list/tuple/set (canonical), a bare string
    (single action), or absent/None (no exemption). Permissive — an unknown or
    malformed value yields no exemption (the detector simply never matches)."""
    raw = node.get("maintain_exempt")
    if not raw:
        return set()
    if isinstance(raw, str):
        return {raw}
    if isinstance(raw, (list, tuple, set)):
        return set(raw)
    return set()


def _get_distill_candidates(ctx, tree, include_skipped=False):
    """DELEGATES to core/scripts/tree.py::get_distill_candidates ().

    This was a hand-maintained mirror of the CLI detector, and it drifted FOUR
    fixes behind while both halves stayed live: crit3 (the g-115-1570 oversized
    read-cap sweep) was absent entirely; interior nodes were skipped by an early
    `continue` (g-115-2913/rb-4648) so an oversized hub escaped every detector;
    the g-115-2317 sparse-feedback + stale-signal bars were missing, leaving the
    permissive `has_feedback >= 1` gate that flagged a standing false-positive
    pool; and `maintain_exempt: distill` (g-115-1700/guard-896) went unhonoured.

    The consequence was not cosmetic. Measured at the fix (foxtrot, 2026-07-30,
    cc-04/Linux) on ONE live tree of 1297 nodes, in one process: the pre-edit code
    at HEAD returned **809** candidates, the CLI returned **566**, and after the
    delegation both return 566 with identical key lists. g-115-4062 filed the same
    divergence as 807 vs 558 a few hours earlier — a different tree state, the same
    ~243-candidate gap. The daemon's number is the one that lands in
    `post_run_debt` and gates backlog-mode escalation fleet-wide, while
    `tree-read.sh --distill-candidates` falls through to the CLI (tree_read.py
    lists it under "NOT served on the daemon path"), so the READ path and the
    WRITE path had been disagreeing by ~40% on the same tree.

    Only TWO things ever made this function need to be separate, and both are now
    injected rather than forked: the config path and the node-.md path both have
    to resolve through the per-request `ctx` (a daemon process may serve a project
    root that is not the CLI module's). `_resolve_candidate_path` is still the
    ctx-aware resolver — it is passed in, not replaced. Everything else was
    duplication (rb-4880/rb-4884: byte-near-identical siblings are maintenance
    debt, not a design). Do NOT re-fork this to add a caller-specific rule; add a
    seam to the CLI function instead, so one implementation keeps serving both.
    """
    return _cli_get_distill_candidates(
        tree,
        include_skipped,
        config_dir=ctx.paths.project_root / "core" / "config",
        resolve_path=lambda virtual_path: _resolve_candidate_path(ctx, virtual_path),
    )


def _get_decompose_candidates(ctx, tree, include_skipped=False):
    """ctx-aware mirror of tree.py:993-1050. STRUCTURAL trigger (): a
    non-root node is a decompose candidate when its retrieval-subtree leaf count
    exceeds the leaf cap (K_max^(D_retrieval-1)) — NOT when its .md body exceeds
    a line count. The line-count trigger is retired per g-306-13 outcome 3
    (board decision msg-20260619-075228-bravo-086). Root (depth 0) is excluded.
    Honors maintain_exempt (g-115-1648). Skip-reason enum: is_root,
    decompose_exempt, within_leaf_cap.

    Ported to the daemon 2026-07-17 (g-115-2481): the g-306-13 migration landed
    CLI-only and this LIVE production write-path mirror kept the retired
    line-count logic (reading node .md files, reporting file_not_found/no_file),
    diverging the record-maintenance run-record's candidates_pre_filter block —
    the byte-compat parity failure test_byte_compat_record_maintenance_with_run_record
    caught. Same bug class as g-115-1683/g-115-2422. Structural = no .md read, so
    file_not_found/read_error are structurally impossible here (cf. tree.py:656)."""
    leaf_cap = _config_leaf_cap(ctx)
    nodes = tree.get("nodes", {})
    leaf_counts = _subtree_leaf_counts(nodes)
    candidates = []
    skipped = []
    for key, node in nodes.items():
        depth = node.get("depth", 0)
        if depth < 1:
            if include_skipped:
                skipped.append({"node_key": key, "skip_reason": "is_root"})
            continue
        if "decompose" in _node_maintain_exempt(node):
            if include_skipped:
                skipped.append({"node_key": key, "skip_reason": "decompose_exempt"})
            continue
        subtree_leaves = leaf_counts.get(key, 0)
        if subtree_leaves > leaf_cap:
            candidates.append({
                "key": key,
                "file": node.get("file", ""),
                "depth": depth,
                "child_count": len(node.get("children", [])),
                "subtree_leaves": subtree_leaves,
                "leaf_cap": leaf_cap,
                "reason": "leaf_overflow",
                "recommended_action": "decompose",
                "growth_state": node.get("growth_state", "stable"),
            })
        elif include_skipped:
            skipped.append({"node_key": key, "skip_reason": "within_leaf_cap"})
    candidates.sort(key=lambda c: c["subtree_leaves"], reverse=True)
    if include_skipped:
        return {"candidates": candidates, "skipped": skipped}
    return candidates


def _get_redistribute_candidates(ctx, tree, include_skipped=False):
    """ctx-aware mirror of tree.py:1053-1106. STRUCTURAL trigger (): an
    interior node is a regroup candidate when it has more than K_max children
    (the Zhong K=4 fan-out cap) — NOT when its .md body exceeds a line count
    (retired per g-306-13 outcome 3). Honors maintain_exempt (g-115-1648).
    Skip-reason enum: no_children, redistribute_exempt, within_k_max.

    Ported to the daemon 2026-07-17 (g-115-2481) — see _get_decompose_candidates
    for the CLI-only-migration incident. Structural = no .md read."""
    k_max = _config_k_max(ctx)
    nodes = tree.get("nodes", {})
    candidates = []
    skipped = []
    for key, node in nodes.items():
        children = node.get("children", [])
        if not children:
            if include_skipped:
                skipped.append({"node_key": key, "skip_reason": "no_children"})
            continue  # leaves handled by _get_decompose_candidates
        if "redistribute" in _node_maintain_exempt(node):
            if include_skipped:
                skipped.append({"node_key": key, "skip_reason": "redistribute_exempt"})
            continue
        if len(children) > k_max:
            candidates.append({
                "key": key,
                "file": node.get("file", ""),
                "depth": node.get("depth", 0),
                "child_count": len(children),
                "children": children,
                "k_max": k_max,
                "reason": "k_overflow",
                "recommended_action": "regroup",
                "growth_state": node.get("growth_state", "stable"),
            })
        elif include_skipped:
            skipped.append({"node_key": key, "skip_reason": "within_k_max"})
    candidates.sort(key=lambda c: c["child_count"], reverse=True)
    if include_skipped:
        return {"candidates": candidates, "skipped": skipped}
    return candidates


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def write(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/tree/write

    Body JSON (op-tagged):
      add-child:    {"op":"add-child","parent":"<key>","child":{"key":...,...},
                     "body":"<optional .md markdown>",
                     "no_dedup":<bool, bypasses dedup+child-limit>,
                     "accept_overflow":"<justification, allows over-cap add>"}
      set:          {"op":"set","key":"<key>","field":"<f>","value":<v>}
      increment:    {"op":"increment","key":"<key>","field":"<f>"}
      remove-child: {"op":"remove-child","parent":"<key>","child_key":"<key>"}
      propagate:    {"op":"propagate","key":"<key>"}
      reconcile-capabilities: {"op":"reconcile-capabilities"}
      reparent:     {"op":"reparent","key":"<key>","new_parent":"<key>"}
      batch:        {"op":"batch","operations":[<set|increment|add-child|
                     remove-child|propagate op objects>]}  (atomic; propagate
                     ops apply last)
      record-maintenance: {"op":"record-maintenance",
                     "backlog_mode":<bool>,"stop_mode":<bool>,
                     "with_run_record":<bool>,
                     "run_record_input":{mode,started_at,decompose,distill,...}}
                     (run_record_input required only when with_run_record; it is
                     the daemon analogue of the CLI's stdin JSON blob)
    """
    from ..server import Response

    try:
        req = _parse_body_json(ctx.body)
    except (ValueError, json.JSONDecodeError) as e:
        return Response.error(400, "invalid_body", f"body must be JSON: {e}")
    if not isinstance(req, dict):
        return Response.error(400, "invalid_body", "body must be a JSON object")

    op = (req.get("op") or "").strip()
    if op not in VALID_OPS:
        return Response.error(400, "invalid_op",
                              f"op must be one of {sorted(VALID_OPS)}; got {op!r}")

    path = _tree_path(ctx)
    base_dir = ctx.paths.world
    agent = _agent_name(ctx)
    world_path = ctx.paths.world

    if not path.exists():
        return Response.error(404, "tree_not_found",
                              f"_tree.yaml not found at {path}")

    try:
        with file_locks.locked(path):
            tree = _read_tree_locked(path)
            if not isinstance(tree, dict) or "nodes" not in tree:
                return Response.error(500, "invalid_tree",
                                      "_tree.yaml missing 'nodes' key")
            nodes = tree["nodes"]

            # ---- add-child --------------------------------------------------
            if op == "add-child":
                parent_key = (req.get("parent") or "").strip()
                child = req.get("child")
                if not parent_key:
                    return Response.error(400, "missing_param",
                                          "'parent' required for add-child")
                if not isinstance(child, dict) or not child.get("key"):
                    return Response.error(400, "missing_param",
                                          "'child' object with a 'key' required")
                # rb-8572: body-bearing keys inside `child` are silently
                # dropped by _apply_add_child's allowlist — the caller who
                # sends them believes add-child writes the .md body, and the
                # result is an index-only orphan node (two live instances,
                # 2026-08-20 fleet sweep). Refuse with the correct interface:
                # the request-level `body` field (verbatim .md text), or the
                # body-first register-second flow.
                _body_keys = [k for k in ("content", "markdown")
                              if k in child] + (["body"] if "body" in child
                                                else [])
                if _body_keys and not req.get("body"):
                    return Response.error(
                        400, "body_in_child",
                        "child JSON carries body-bearing key(s) "
                        + ", ".join(repr(k) for k in _body_keys)
                        + " — add-child registers the index only and would "
                        "silently drop them (rb-8572). Pass the .md text as "
                        "the request-level 'body' field, or write the body "
                        "first and register after.")
                child_key = child["key"]
                if parent_key not in nodes:
                    return Response.error(404, "parent_not_found",
                                          f"parent node not found: {parent_key}")
                if child_key in nodes:
                    return Response.error(409, "duplicate_key",
                                          f"node key already exists: {child_key}")

                # Curation gates (mirror cmd_add_child:1667-1681): dedup first,
                # then child-limit. The CLI binds BOTH bypasses to --no-dedup
                # (tree.py:1679 passes no_limit=no_dedup), so a single
                # `no_dedup` field skips both here too. Dedup is fail-open
                # (a crash must never block the write — _enforce_dedup contract).
                if not req.get("no_dedup"):
                    try:
                        ded = _check_dedup(parent_key, child_key,
                                           child.get("summary", ""), tree, ctx)
                    except Exception:  # noqa: BLE001 — fail-open like the CLI
                        ded = {"action": "error"}
                    if ded.get("action") == "reject":
                        return Response.error(409, "dedup_reject",
                                              f"sibling already covers this: "
                                              f"{ded}")
                cl = _check_child_limit(parent_key, nodes[parent_key], ctx,
                                        no_limit=bool(req.get("no_dedup")),
                                        accept_overflow=req.get("accept_overflow"))
                if cl is not None:
                    return Response.error(409, "child_limit_reject", f"{cl}")

                child_node = _apply_add_child(tree, parent_key, child, world_path, agent)

                # Optional .md body (daemon-only; CLI add-child writes no .md).
                md_written = False
                body_text = req.get("body")
                if isinstance(body_text, str) and body_text:
                    md_path = _resolve_node_md(child_node["file"], world_path)
                    assert_not_cruft(md_path.parent, "mkdir (tree_write node .md)")
                    md_path.parent.mkdir(parents=True, exist_ok=True)
                    md_path.write_text(body_text, encoding="utf-8")
                    changelog.append(base_dir, agent, md_path, "create",
                                     summary=f"tree-add-child .md {child_key}",
                                     lines_changed=body_text.count("\n") + 1)
                    md_written = True

                _write_tree_locked(path, tree, base_dir, agent,
                                   summary=f"tree-add-child {child_key} -> {parent_key}")
                # S9 pick-log (): restores the telemetry the tree
                # daemonization deferred. Fail-open; separate file, no
                # _tree.yaml byte impact. `nodes` is post-add (child present),
                # so the L1 walk resolves the fresh node. Path() is guarded:
                # a None meta (parity-test ctx, misconfigured env) must reach
                # log_l1_pick's own swallow-to-WARN interior instead of
                # raising TypeError at the call site — telemetry must never
                # crash the write path ().
                _meta = ctx.paths.meta
                log_l1_pick(nodes, Path(_meta) if _meta else None,
                            child_key, "add-child",
                            source=req.get("encoding_source"),
                            reason=req.get("encoding_reason"), agent=agent)
                out = _apply_defaults(child_node)
                out["key"] = child_key
                resp = {"ok": True, "op": op, "key": child_key,
                        "node": out, "md_written": md_written}
                # : body-presence advisory (skip when this request
                # just wrote the body itself via the optional `body` field).
                if not md_written:
                    _bw = _body_presence_warning(child_key,
                                                 child_node.get("file"),
                                                 world_path, "add-child")
                    if _bw:
                        print("WARN tree_write: " + _bw, file=sys.stderr)
                        resp["body_presence_warning"] = _bw
                return Response.json(resp)

            # ---- set --------------------------------------------------------
            if op == "set":
                key = (req.get("key") or "").strip()
                field = (req.get("field") or "").strip()
                if not key or not field:
                    return Response.error(400, "missing_param",
                                          "'key' and 'field' required for set")
                if "value" not in req:
                    return Response.error(400, "missing_param",
                                          "'value' required for set")
                if key not in nodes:
                    return Response.error(404, "node_not_found",
                                          f"node not found: {key}")
                # : WIRE-INTEGRITY CHECK, and it runs BEFORE the write
                # on purpose. guard-3150 forbids write-then-verify on a
                # structured file — once short bytes are on disk they are, on an
                # S3-authoritative store, already the shared truth for every
                # other box, so a post-write check cannot prevent anything.
                #
                # WHY IT COMPARES ACROSS THE WIRE rather than in memory: the
                # obvious daemon-side check (len(req["value"]) vs
                # len(node[field]) after _apply_set) is VACUOUS — _apply_set
                # does node[field] = v where v IS the parsed request value, so
                # it compares a string to itself and passes 100% of the time
                # while looking exactly like protection (the
                # checker-input-assumption-defects class). The client's
                # declared length is derived independently, on the other side
                # of the wire, so this comparison is real.
                #
                # It targets the measured 2026-08-19 loss: a 17,708-byte value
                # stored as 8,186 bytes cut mid-word, rc=0, with a
                # complete-looking echo. That echo is rendered from the
                # in-memory dict (see `out = _apply_defaults(node)` below) and
                # never from a re-read, so nothing in this path could see it.
                # The goal's own analysis places that loss at or before
                # CLIENT-side serialization (a truncated HTTP body could not
                # have returned 200 — it would be invalid JSON), which is
                # exactly the span this check covers and an in-daemon check
                # cannot.
                #
                # FAIL-OPEN WHEN ABSENT, FAIL-CLOSED ON MISMATCH. A client that
                # does not declare value_bytes is not refused: every existing
                # caller predates this field, and refusing them would take the
                # tree write path down fleet-wide. That asymmetry is deliberate
                # but it IS a coverage gap — an undeclared write is unchecked,
                # so the protection is only as wide as the callers that opt in.
                declared = req.get("value_bytes")
                _sent = req["value"]
                if declared is not None and isinstance(_sent, str):
                    try:
                        declared_int = int(declared)
                    except (TypeError, ValueError):
                        declared_int = None
                    received = len(_sent.encode("utf-8"))
                    if declared_int is not None and declared_int != received:
                        return Response.error(
                            500, "value_truncated_in_transit",
                            f"REFUSING write to {key}.{field}: client declared "
                            f"{declared_int} bytes but the daemon received "
                            f"{received}. The value lost "
                            f"{declared_int - received} bytes between the "
                            f"caller and this endpoint; nothing was written. "
                            f"See g-115-6823 / guard-4449.")
                node = _apply_set(tree, key, field, req["value"], world_path)
                # field=confidence triggers parent-chain propagation +
                # self-graduation (mirrors cmd_set:1599-1604). The node is
                # re-fetched after propagation because self-graduation may
                # have changed the source node's capability_level.
                ancestors_updated: List[Dict[str, Any]] = []
                capability_changes: List[Dict[str, Any]] = []
                if field == "confidence":
                    competence = _load_competence_config(ctx)
                    ancestors_updated, capability_changes = _propagate_in_memory(
                        tree["nodes"], key, competence)
                    node = tree["nodes"][key]
                _write_tree_locked(path, tree, base_dir, agent,
                                   summary=f"tree-set {key}.{field}")
                out = _apply_defaults(node)
                out["key"] = key
                if field == "confidence":
                    out["ancestors_updated"] = ancestors_updated
                    out["capability_changes"] = capability_changes
                resp = {"ok": True, "op": op, "key": key, "node": out}
                #  / guard-1661: the success response must carry
                # evidence the caller can check WITHOUT a second read. The
                # caller compares this against the length it sent; that is the
                # half of the end-to-end check the daemon cannot perform alone.
                _stored = node.get(field)
                if isinstance(_stored, str):
                    resp["value_bytes"] = len(_stored.encode("utf-8"))
                # : DURABILITY post-condition. `field` is the op's own
                # INPUT to merge_tree's per-node field merge, so it witnesses
                # this write (guard-5212 -- never children/child_count/
                # node_type/depth, which the merge recomputes unconditionally).
                # Report-only: the write already landed, so refusing here would
                # be theatre. Fail-open inside the helper.
                _dur = _durability_witness(path, key, {field: _stored},
                                           write_stamp=node.get("last_updated"))
                if _dur:
                    print("WARN tree_write durability: " + json.dumps(_dur),
                          file=sys.stderr)
                    resp["durability_warning"] = _dur
                # : enriching a bodiless node is the desync signature.
                _bw = _body_presence_warning(key, node.get("file"),
                                             world_path, "set")
                if _bw:
                    print("WARN tree_write: " + _bw, file=sys.stderr)
                    resp["body_presence_warning"] = _bw
                return Response.json(resp)

            # ---- increment --------------------------------------------------
            if op == "increment":
                key = (req.get("key") or "").strip()
                field = (req.get("field") or "").strip()
                if not key or not field:
                    return Response.error(400, "missing_param",
                                          "'key' and 'field' required for increment")
                if key not in nodes:
                    return Response.error(404, "node_not_found",
                                          f"node not found: {key}")
                node = _apply_increment(tree, key, field)
                _write_tree_locked(path, tree, base_dir, agent,
                                   summary=f"tree-increment {key}.{field}")
                out = _apply_defaults(node)
                out["key"] = key
                return Response.json({"ok": True, "op": op, "key": key, "node": out})

            # ---- remove-child -----------------------------------------------
            if op == "remove-child":
                parent_key = (req.get("parent") or "").strip()
                child_key = (req.get("child_key") or "").strip()
                if not parent_key or not child_key:
                    return Response.error(400, "missing_param",
                                          "'parent' and 'child_key' required")
                if parent_key not in nodes:
                    return Response.error(404, "parent_not_found",
                                          f"parent node not found: {parent_key}")
                if child_key not in nodes[parent_key].get("children", []):
                    return Response.error(404, "child_not_found",
                                          f"child '{child_key}' not in parent "
                                          f"'{parent_key}' children list")
                descendants = _apply_remove_child(tree, parent_key, child_key)
                if descendants:
                    return Response.error(
                        409, "would_orphan_subtree",
                        f"'{child_key}' has {len(descendants)} descendant(s) "
                        f"({', '.join(descendants)}); remove them first")
                # tree_growth_log PRUNE row () — same SSOT call the
                # CLI's cmd_remove_child makes, so standalone and batch removal
                # agree across BOTH write paths.
                _growth_record_batch(
                    tree,
                    [{"op": "remove-child", "key": parent_key,
                      "child_key": child_key}],
                    date.today().isoformat())
                #  / guard-4592 / guard-1661 / guard-3150.
                # STRUCTURAL POST-CONDITION, asserted BEFORE the write.
                #
                # The response below used to be exactly
                # {"removed": child_key, "parent": parent_key} -- both values
                # echoed VERBATIM FROM THE REQUEST. It was therefore true by
                # construction and structurally incapable of reporting a failed
                # removal: on 2026-08-20 (alpha worker Body, cc-08, own-cloud)
                # that precise payload came back rc=0 while the node AND the
                # parent's children entry were still present in the local mirror
                # AND the authoritative store, both reading 1,514,666 B.
                #
                # The two sides here have DISTINCT ORIGINS, which is what
                # guard-4592 requires: child_key comes from the REQUEST, while
                # the membership tests read the MUTATED TREE STRUCTURE. This is
                # not the one-side-of-an-assignment comparison that passes 100%
                # of the time.
                #
                # THE NON-TAUTOLOGICAL CATCH: list.remove() removes only the
                # FIRST occurrence. A duplicated child entry -- the shape an
                # own-cloud union merge produces when a peer write resurrects a
                # node (rb-2859 class) -- leaves the child STILL PRESENT while
                # _apply_remove_child returns success. That is candidate (b) of
                # this goal's two un-separated mechanisms, and this assertion
                # catches it without asserting which mechanism is at fault.
                #
                # BEFORE the write, never write-then-verify (guard-3150): on an
                # S3-authoritative store a post-write verify has already made the
                # bad state the shared truth for every other box.
                _nodes_after = tree.get("nodes", {})
                _kids_after = list(
                    (_nodes_after.get(parent_key) or {}).get("children") or [])
                if child_key in _kids_after or child_key in _nodes_after:
                    return Response.error(
                        500, "remove_child_post_condition_failed",
                        f"REFUSING to persist tree-remove-child: after applying "
                        f"the removal in memory, '{child_key}' is STILL present "
                        f"(in parent children: {child_key in _kids_after}; as a "
                        f"node: {child_key in _nodes_after}). Nothing was "
                        f"written. A duplicated child entry is the most likely "
                        f"cause -- list.remove() drops only the first "
                        f"occurrence. Inspect "
                        f"nodes['{parent_key}']['children'] for duplicates. "
                        f"See g-115-7816 / guard-4592.")
                _write_tree_locked(path, tree, base_dir, agent,
                                   summary=f"tree-remove-child {child_key} from {parent_key}")
                # guard-1661: return evidence the caller can check WITHOUT a
                # second read. Send the STORED LIST, not a daemon-computed
                # verdict -- the caller derives membership itself from its own
                # argv, so neither side inherits the other's conclusion.
                return Response.json({"ok": True, "op": op,
                                      "removed": child_key, "parent": parent_key,
                                      "parent_children": _kids_after,
                                      "parent_child_count": len(_kids_after)})

            # ---- propagate --------------------------------------------------
            # Mirrors cmd_propagate (tree.py:2302-2337). No key-existence 404:
            # _propagate_in_memory returns ([],[]) for a missing key and the
            # tree is still written (last_updated bumped) — identical to the
            # CLI, which the cutover must match exactly.
            if op == "propagate":
                key = (req.get("key") or "").strip()
                if not key:
                    return Response.error(400, "missing_param",
                                          "'key' required for propagate")
                competence = _load_competence_config(ctx)
                p_ancestors, p_caps = _propagate_in_memory(
                    tree["nodes"], key, competence)
                tree["last_updated"] = date.today().isoformat()
                _write_tree_locked(path, tree, base_dir, agent,
                                   summary=f"tree-propagate {key}")
                return Response.json({"ok": True, "op": op, "source_node": key,
                                      "ancestors_updated": p_ancestors,
                                      "capability_changes": p_caps})

            # ---- reconcile-capabilities -------------------------------------
            # Mirrors cmd_reconcile_capabilities (tree.py:2340-2389): recompute
            # every node's capability_level from its confidence vs the
            # competence thresholds. Always writes (last_updated bumped).
            if op == "reconcile-capabilities":
                competence = _load_competence_config(ctx)
                changes: List[Dict[str, Any]] = []
                for nkey, nnode in tree["nodes"].items():
                    old_level, new_level = _graduate_node_level(nnode, competence)
                    if old_level is not None:
                        changes.append({
                            "key": nkey,
                            "old_level": old_level,
                            "new_level": new_level,
                            "confidence": nnode.get("confidence"),
                        })
                tree["last_updated"] = date.today().isoformat()
                _write_tree_locked(path, tree, base_dir, agent,
                                   summary="tree-reconcile-capabilities")
                return Response.json({"ok": True, "op": op,
                                      "reconciled": len(changes),
                                      "total_nodes": len(tree["nodes"]),
                                      "changes": changes})

            # ---- reparent ---------------------------------------------------
            # Mirrors cmd_reparent (tree.py:2392-2560). Every CLI sys.exit(1)
            # validation becomes a 4xx response. Dual-chain propagation runs in
            # the EXACT CLI order — new-parent chain (via node_key, whose parent
            # was just repointed) first, THEN the old-parent chain — because the
            # two chains can share ancestors and the second pass reads
            # confidences the first pass wrote; order is part of byte-compat.
            # Physical .md moves are NOT performed: reported in `file_moves` for
            # the caller. The L1-pick-log telemetry (cmd_reparent S9) appends
            # after the write () — separate file, no _tree.yaml byte
            # impact.
            if op == "reparent":
                node_key = (req.get("key") or req.get("node") or "").strip()
                new_parent_key = (req.get("new_parent") or "").strip()
                if not node_key or not new_parent_key:
                    return Response.error(
                        400, "missing_param",
                        "'key' and 'new_parent' required for reparent")
                if node_key not in nodes:
                    return Response.error(404, "node_not_found",
                                          f"node not found: {node_key}")
                if new_parent_key not in nodes:
                    return Response.error(404, "new_parent_not_found",
                                          f"new parent not found: {new_parent_key}")
                if node_key == "root":
                    return Response.error(400, "invalid_reparent",
                                          "cannot reparent root node")
                if node_key == new_parent_key:
                    return Response.error(400, "invalid_reparent",
                                          "cannot reparent a node to itself")

                node = nodes[node_key]
                old_parent_key = node.get("parent")
                if old_parent_key is None:
                    return Response.error(
                        400, "invalid_reparent",
                        f"node '{node_key}' has no parent (is it root?)")
                if old_parent_key not in nodes:
                    return Response.error(
                        500, "invalid_tree",
                        f"old parent '{old_parent_key}' not found in tree")
                if new_parent_key == old_parent_key:
                    return Response.error(
                        409, "already_child",
                        f"node '{node_key}' is already a child of "
                        f"'{new_parent_key}'")

                # Circular check: new parent must not be a descendant of node.
                descendants = set()
                stack = [node_key]
                while stack:
                    cur = stack.pop()
                    for ch in nodes.get(cur, {}).get("children", []):
                        if ch not in descendants:
                            descendants.add(ch)
                            stack.append(ch)
                if new_parent_key in descendants:
                    return Response.error(
                        409, "circular_reparent",
                        f"'{new_parent_key}' is a descendant of '{node_key}'")

                # Depth check (D_max via tree.yaml + meta overlay).
                d_max = _config_d_max(ctx)
                new_parent_depth = nodes[new_parent_key].get("depth", 0)

                def _max_subtree_depth(k):
                    children = nodes.get(k, {}).get("children", [])
                    if not children:
                        return 0
                    return 1 + max(_max_subtree_depth(c) for c in children)

                subtree_height = _max_subtree_depth(node_key)
                new_max_depth = new_parent_depth + 1 + subtree_height
                if new_max_depth > d_max:
                    return Response.error(
                        409, "depth_exceeded",
                        f"reparent would exceed D_max={d_max}: deepest "
                        f"descendant would be at depth {new_max_depth}")

                # --- Execute (mirrors tree.py:2481-2531 exactly) ---
                old_parent = nodes[old_parent_key]
                old_children = old_parent.get("children", [])
                if node_key in old_children:
                    old_children.remove(node_key)
                old_parent["children"] = old_children
                old_parent["child_count"] = len(old_children)
                if not old_children:
                    old_parent["node_type"] = "leaf"

                new_parent = nodes[new_parent_key]
                if "children" not in new_parent:
                    new_parent["children"] = []
                new_parent["children"].append(node_key)
                new_parent["child_count"] = len(new_parent["children"])
                if new_parent.get("node_type") == "leaf":
                    new_parent["node_type"] = "interior"

                node["parent"] = new_parent_key

                file_moves: List[Dict[str, Any]] = []

                def _recompute_subtree(k, parent_file, parent_depth):
                    n = nodes[k]
                    old_file = n.get("file", "")
                    new_depth = parent_depth + 1
                    new_file = _compute_child_path(parent_file, k, world_path)
                    if old_file != new_file:
                        file_moves.append(
                            {"key": k, "old": old_file, "new": new_file})
                    n["file"] = new_file
                    n["depth"] = new_depth
                    for ck in n.get("children", []):
                        _recompute_subtree(ck, new_file, new_depth)

                _recompute_subtree(node_key, new_parent.get("file", ""),
                                   new_parent_depth)

                competence = _load_competence_config(ctx)
                new_ancestors, new_cap = _propagate_in_memory(
                    nodes, node_key, competence)
                old_ancestors, old_cap = _propagate_in_memory(
                    nodes, old_parent_key, competence)

                tree["nodes"] = nodes
                # tree_growth_log REPARENT row () — mirrors
                # cmd_reparent (tree.py) via the shared _growth_log SSOT.
                _growth_record_reparent(tree, node_key, new_parent_key,
                                        date.today().isoformat())
                tree["last_updated"] = date.today().isoformat()
                _write_tree_locked(
                    path, tree, base_dir, agent,
                    summary=f"tree-reparent {node_key} -> {new_parent_key}")
                # S9 pick-log (): the formerly-DEFERRED reparent
                # telemetry — cross-L1 reparents are the highest-signal
                # entries. `nodes` is post-reparent, so the walk resolves the
                # NEW L1 (mirrors the CLI's fresh-read semantics). Fail-open;
                # None-meta guarded at the call site ().
                _meta = ctx.paths.meta
                log_l1_pick(nodes, Path(_meta) if _meta else None,
                            node_key, "reparent",
                            source=req.get("encoding_source"),
                            reason=req.get("encoding_reason"), agent=agent)
                return Response.json({
                    "ok": True, "op": op,
                    "reparented": node_key,
                    "old_parent": old_parent_key,
                    "new_parent": new_parent_key,
                    "new_depth": nodes[node_key].get("depth"),
                    "file_moves": file_moves,
                    "old_chain_propagation": {
                        "ancestors_updated": old_ancestors,
                        "capability_changes": old_cap,
                    },
                    "new_chain_propagation": {
                        "ancestors_updated": new_ancestors,
                        "capability_changes": new_cap,
                    },
                })

            # ---- batch ------------------------------------------------------
            # Mirrors cmd_batch (tree.py:1902-2160). Applies a sequence of ops
            # atomically under the single lock already held: validate ALL ops
            # first (any failure returns 4xx and writes NOTHING), then phase-1
            # mutations IN ORDER (reusing the same _apply_* helpers + gates as
            # the single-op branches — byte-identical per-op logic, verified:
            # batch increment, like _apply_increment, does NOT stamp node
            # last_updated), then phase-2 propagate ops LAST so they see the
            # phase-1 mutations. As in the CLI, a forward-referenced new key is
            # only usable if its add-child op precedes the op that uses it
            # (pending_child_keys suppresses only the validation error, not the
            # execution-order requirement) — a mis-ordered ref surfaces as a
            # clean 400 here rather than the CLI's bare KeyError traceback.
            if op == "batch":
                operations = req.get("operations")
                if not isinstance(operations, list) or not operations:
                    return Response.error(400, "missing_param",
                                          "'operations' must be a non-empty list")
                _VALID_BATCH = ("set", "increment", "add-child",
                                "remove-child", "propagate")
                pending_child_keys = set()
                for o in operations:
                    if o.get("op") == "add-child":
                        c = o.get("child") or {}
                        if c.get("key"):
                            pending_child_keys.add(c["key"])

                # ---- Validation (all ops, before any mutation) ----
                mutation_ops: List[Dict[str, Any]] = []
                propagate_ops: List[Dict[str, Any]] = []
                for i, o in enumerate(operations):
                    op_type = o.get("op")
                    key = o.get("key")
                    if not op_type or not key:
                        return Response.error(
                            400, "invalid_operation",
                            f"operation {i} missing 'op' or 'key'")
                    if op_type not in _VALID_BATCH:
                        return Response.error(
                            400, "invalid_operation",
                            f"operation {i} invalid op {op_type!r}; must be one "
                            f"of {list(_VALID_BATCH)}")
                    if (op_type != "add-child" and key not in nodes
                            and key not in pending_child_keys):
                        return Response.error(
                            404, "node_not_found",
                            f"operation {i} references non-existent node {key!r}")
                    if op_type in ("set", "increment") and not o.get("field"):
                        return Response.error(
                            400, "missing_param",
                            f"operation {i} ({op_type}) missing 'field'")
                    if op_type == "add-child":
                        c = o.get("child") or {}
                        if not c.get("key"):
                            return Response.error(
                                400, "missing_param",
                                f"operation {i} (add-child) missing child.key")
                        if key not in nodes:
                            return Response.error(
                                404, "parent_not_found",
                                f"operation {i} (add-child) parent {key!r} "
                                f"not found")
                    if op_type == "remove-child" and not o.get("child_key"):
                        return Response.error(
                            400, "missing_param",
                            f"operation {i} (remove-child) missing 'child_key'")
                    (propagate_ops if op_type == "propagate"
                     else mutation_ops).append(o)

                updated_keys = set()
                batch_added_child_keys: List[str] = []  # S9 pick-log, 
                batch_set_keys: List[str] = []  #  body-presence advisory
                propagate_results: List[Dict[str, Any]] = []
                try:
                    # ---- Phase 1: mutations in order ----
                    for o in mutation_ops:
                        op_type = o["op"]
                        key = o["key"]
                        if op_type == "set":
                            _apply_set(tree, key, o["field"], o.get("value"),
                                       world_path)
                            updated_keys.add(key)
                            batch_set_keys.append(key)  # 
                        elif op_type == "increment":
                            _apply_increment(tree, key, o["field"])
                            updated_keys.add(key)
                        elif op_type == "add-child":
                            child = o["child"]
                            child_key = child["key"]
                            if child_key in nodes:
                                return Response.error(
                                    409, "duplicate_key",
                                    f"node key already exists: {child_key}")
                            if not o.get("no_dedup"):
                                try:
                                    ded = _check_dedup(
                                        key, child_key,
                                        child.get("summary", ""), tree, ctx)
                                except Exception:  # noqa: BLE001 — fail-open
                                    ded = {"action": "error"}
                                if ded.get("action") == "reject":
                                    return Response.error(
                                        409, "dedup_reject",
                                        f"sibling already covers this: {ded}")
                            cl = _check_child_limit(
                                key, nodes[key], ctx,
                                no_limit=bool(o.get("no_dedup")),
                                accept_overflow=o.get("accept_overflow"))
                            if cl is not None:
                                return Response.error(
                                    409, "child_limit_reject", f"{cl}")
                            _apply_add_child(tree, key, child, world_path, agent)
                            updated_keys.add(child_key)
                            updated_keys.add(key)
                            batch_added_child_keys.append(child_key)
                        elif op_type == "remove-child":
                            child_key = o["child_key"]
                            descendants = _apply_remove_child(
                                tree, key, child_key)
                            if descendants:
                                return Response.error(
                                    409, "would_orphan_subtree",
                                    f"'{child_key}' has {len(descendants)} "
                                    f"descendant(s) "
                                    f"({', '.join(descendants)}); "
                                    f"remove them first")
                            updated_keys.add(key)

                    # ---- Phase 2: propagate ops LAST ----
                    if propagate_ops:
                        competence = _load_competence_config(ctx)
                        for o in propagate_ops:
                            p_key = o["key"]
                            anc, caps = _propagate_in_memory(
                                tree["nodes"], p_key, competence)
                            propagate_results.append({
                                "source_node": p_key,
                                "ancestors_updated": anc,
                                "capability_changes": caps,
                            })
                            for a in anc:
                                updated_keys.add(a["key"])
                except KeyError as e:
                    return Response.error(
                        400, "batch_execution_error",
                        f"operation referenced a missing node mid-batch "
                        f"(ensure add-child precedes ops that use the new "
                        f"key): {e}")

                # tree_growth_log: DECOMPOSE + PRUNE rows for this batch
                # (). Mirrors cmd_batch (tree.py) by calling the SAME
                # _growth_log SSOT — the whole point, since this log's sibling
                # (l1-pick-log) went silent for ~6 weeks precisely because the
                # daemon copy of a write path did not carry the CLI's append
                # (). Must precede serialization: it mutates `tree`.
                _growth_record_batch(tree, mutation_ops,
                                     date.today().isoformat())
                tree["last_updated"] = date.today().isoformat()
                _write_tree_locked(path, tree, base_dir, agent,
                                   summary=f"tree-batch ({len(operations)} ops)")
                # S9 pick-log per add-child in this batch (; mirrors
                # cmd_batch's per-op logging). Fail-open; None-meta guarded at
                # the call site ().
                _meta = ctx.paths.meta
                for _ck in batch_added_child_keys:
                    log_l1_pick(nodes, Path(_meta) if _meta else None, _ck,
                                "batch-add-child",
                                source=req.get("encoding_source"),
                                reason=req.get("encoding_reason"), agent=agent)
                updated_nodes: List[Dict[str, Any]] = []
                for k in updated_keys:
                    if k in tree["nodes"]:
                        nd = _apply_defaults(tree["nodes"][k])
                        nd["key"] = k
                        updated_nodes.append(nd)
                resp = {"ok": True, "op": op,
                        "updated_nodes": updated_nodes,
                        "propagate": propagate_results}
                # : body-presence advisory for nodes this batch
                # REGISTERED (add-child) or ENRICHED (set) — mirrors cmd_batch.
                # Excludes parents touched only by child-list bookkeeping and
                # propagate-walked ancestors; a key set-then-removed in the
                # same batch is absent from tree["nodes"] and skipped.
                _bwarns: List[str] = []
                for _wk in set(batch_added_child_keys) | set(batch_set_keys):
                    if _wk in tree["nodes"]:
                        _bw = _body_presence_warning(
                            _wk, tree["nodes"][_wk].get("file"),
                            world_path, "batch")
                        if _bw:
                            _bwarns.append(_bw)
                if _bwarns:
                    for _bw in _bwarns:
                        print("WARN tree_write: " + _bw, file=sys.stderr)
                    resp["body_presence_warnings"] = _bwarns
                return Response.json(resp)

            # ---- record-maintenance -----------------------------------------
            # Mirrors cmd_record_maintenance (tree.py:1355-1556). Records a
            # /tree maintain completion in the top-level `maintenance` block and
            # (with with_run_record) appends a run record to
            # world/tree-maintenance-log.jsonl. Byte-compat significant:
            #   • maintenance-block key INSERTION ORDER (last_maintain_at, then
            #     optionally last_backlog_mode_at, last_stop_mode_at, and
            #     last_backlog_clear_at) — sort_keys=False dumps in this order.
            #   • the post-run debt math (distill+decompose counts vs
            #     tree_debt_check.debt_threshold) decides whether
            #     last_backlog_clear_at lands on disk.
            #   • `maintenance` is assigned LAST (after entity_index) when the
            #     key is new, matching the CLI's `tree["maintenance"] = ...`.
            # Candidate counts are computed ctx-aware (config + node .md reads
            # through ctx.paths). The `tree`/`nodes` read under the held lock
            # above are reused — same single-snapshot guarantee as the CLI's
            # locked_modify_yaml(_do_record).
            if op == "record-maintenance":
                now = datetime.now().isoformat(timespec="seconds")
                # No fallback — tree.yaml is framework-owned and MUST carry
                # tree_debt_check.debt_threshold (single-source-of-truth; mirror
                # cmd_record_maintenance:1395-1397). A missing config/key is a
                # misconfiguration surfaced as 500, not silently defaulted.
                cfg_path = ctx.paths.project_root / "core" / "config" / "tree.yaml"
                try:
                    with cfg_path.open("r", encoding="utf-8") as f:
                        rm_cfg = yaml.safe_load(f)
                    debt_threshold = rm_cfg["tree_debt_check"]["debt_threshold"]
                except (OSError, KeyError, TypeError) as e:
                    return Response.error(
                        500, "config_error",
                        f"core/config/tree.yaml missing "
                        f"tree_debt_check.debt_threshold: {e}")

                with_run_record = bool(req.get("with_run_record"))
                backlog_mode = bool(req.get("backlog_mode"))
                stop_mode = bool(req.get("stop_mode"))

                maintenance = tree.get("maintenance") or {}
                maintenance["last_maintain_at"] = now
                if backlog_mode:
                    maintenance["last_backlog_mode_at"] = now
                if stop_mode:
                    maintenance["last_stop_mode_at"] = now

                decompose_detail = distill_detail = redistribute_detail = None
                if with_run_record:
                    distill_detail = _get_distill_candidates(
                        ctx, tree, include_skipped=True)
                    decompose_detail = _get_decompose_candidates(
                        ctx, tree, include_skipped=True)
                    redistribute_detail = _get_redistribute_candidates(
                        ctx, tree, include_skipped=True)
                    distill_count = len(distill_detail["candidates"])
                    decompose_count = len(decompose_detail["candidates"])
                else:
                    distill_count = len(_get_distill_candidates(ctx, tree))
                    decompose_count = len(
                        _get_decompose_candidates(ctx, tree))

                post_debt = distill_count + decompose_count
                if post_debt <= debt_threshold:
                    maintenance["last_backlog_clear_at"] = now

                tree["maintenance"] = maintenance
                tree["last_updated"] = date.today().isoformat()
                _write_tree_locked(path, tree, base_dir, agent,
                                   summary="tree-record-maintenance")

                result = {
                    "maintenance": maintenance,
                    "post_run_debt": {
                        "distill": distill_count,
                        "decompose": decompose_count,
                        "total": post_debt,
                        "threshold": debt_threshold,
                        "cleared": post_debt <= debt_threshold,
                    },
                }

                if with_run_record:
                    run_input = req.get("run_record_input")
                    if not isinstance(run_input, dict):
                        return Response.error(
                            400, "missing_param",
                            "with_run_record requires a 'run_record_input' object "
                            "(mode/started_at/decompose/distill/...) — the daemon "
                            "analogue of the CLI's stdin JSON blob")

                    def _agg_skip_reasons(skipped_items):
                        agg: Dict[str, int] = {}
                        for item in skipped_items:
                            reason = item.get("skip_reason", "unknown")
                            agg[reason] = agg.get(reason, 0) + 1
                        return agg

                    pre_filter = {
                        "decompose": {
                            "candidates_in": len(decompose_detail["candidates"]),
                            "skipped_by_reason": _agg_skip_reasons(decompose_detail["skipped"]),
                        },
                        "distill": {
                            "candidates_in": len(distill_detail["candidates"]),
                            "skipped_by_reason": _agg_skip_reasons(distill_detail["skipped"]),
                        },
                        "redistribute": {
                            "candidates_in": len(redistribute_detail["candidates"]),
                            "skipped_by_reason": _agg_skip_reasons(redistribute_detail["skipped"]),
                        },
                    }

                    started_at = run_input.get("started_at") or now
                    # Run-record agent mirrors the CLI's
                    # `os.environ.get("MIND_AGENT","") or "unknown"` (NOT
                    # _agent_name's "system" default) so run_id matches the CLI
                    # byte-for-byte when an agent header is present, and uses the
                    # same "unknown" sentinel when it is absent.
                    rr_agent = (ctx.headers.get("x-mind-agent") or "").strip() or "unknown"
                    run_id = ("maint-" + started_at.replace(":", "").replace("-", "")
                              + "-" + rr_agent)

                    record = {
                        "run_id": run_id,
                        "agent": rr_agent,
                        "mode": run_input.get("mode", "standard"),
                        "started_at": started_at,
                        "ended_at": now,
                        "candidates_pre_filter": pre_filter,
                        "llm_reported": {
                            k: v for k, v in run_input.items()
                            if k not in ("mode", "started_at")
                        },
                        "post_run_debt": result["post_run_debt"],
                    }
                    # Function-local import mirrors the CLI idiom
                    # (cmd_record_maintenance:1472). ensure_ascii=True + the
                    # snapshot/changelog ceremony are identical, so the appended
                    # JSONL record is byte-compatible with the CLI path.
                    from _fileops import locked_append_jsonl
                    log_path = ctx.paths.world / "tree-maintenance-log.jsonl"
                    locked_append_jsonl(log_path, record)
                    result["run_record"] = {
                        "run_id": run_id,
                        "log_path": str(log_path),
                        "appended": True,
                    }

                return Response.json({"ok": True, "op": op, "result": result})

    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    # Unreachable (all ops return inside the lock), but keep the type checker
    # and any future op-without-return honest.
    return Response.error(500, "no_op_result", "operation produced no result")


def register(routes) -> None:
    routes[("POST", "/v1/tree/write")] = write
