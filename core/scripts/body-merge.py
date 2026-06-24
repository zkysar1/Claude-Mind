#!/usr/bin/env python3
"""Generalize-down body-WM merge (Phase 1C, Mind/Body — ).

The JOIN half of the Body lifecycle `fork -> diverge -> generalize-down`. When a
non-reducer worker Body's session closes, the stop-hook producer marks its
`body-manifest.yaml` `body_state: closed-pending-merge`. The next time the
reducer (the worker holding `running-session-id`) runs `aspirations-consolidate`,
its NEW Step -1 calls `generalize_down(agent)` here, which:

  1. enumerates `agents/<mindKey>/sessions/*/body-manifest.yaml` whose
     `body_state == closed-pending-merge`,
  2. for each, delta-merges the Body's forked WM into the reducer's
     (agent-wide) WM under per-slot policies,
  3. copies the merged WM back to the reducer,
  4. re-scans once for late arrivals,
  5. marks each merged manifest `body_state: merged`.

This is the engine's session-termination memory-persistence merge (the
shutdown-time persistence step of a multi-instance runtime). Design SSOT: tree
node `mind-engine-identity-bridge`. Schema/lifecycle: `session-state.md`
"Phase 1B/1C".

PER-SLOT MERGE POLICIES (driven by the WM schema in `wm.py`; recursive for
`loop_state` and nested `signals`):
  - arrays (ARRAY_SLOTS, `encoding_queue`, `goals_completed_this_session`):
        append + content-hash dedup (reducer items first, then new body items)
  - `active_context` / `archived_context` (MAP_SLOTS), `session_id`,
    `session_start`:                          reducer-wins (canonical session ctx)
  - numeric counters (int/float, incl. nested loop_state counters):  SUM
  - ISO-timestamp strings (cadence trackers `last_*`):               latest-wins
  - dicts (`loop_state`, `signals`):                                 recurse
  - other scalars / type mismatch:                                   reducer-wins

THE forked_wm_hash BASELINE acts as the no-op short-circuit: if a Body's current
WM still hashes to the `forked_wm_hash` recorded at fork time, the Body never
diverged -> no merge needed, just mark `merged`. (The hash detects WHETHER a
delta exists; the per-slot policies above define HOW reducer+body combine.)

3-WAY DELTA (Phase 2B, g-306-70): when the Body's fork-time WM is preserved
alongside the manifest as `forked-wm-baseline.yaml` (written byte-faithfully at
fork by body-manifest.write_manifest), `generalize_down` loads it and passes it
to `merge_wm(reducer, body, baseline)`. NUMERIC counters then merge as
`reducer + (body - baseline)` — the body's NET divergence from the common
ancestor — instead of the 2-way `reducer + body`, which double-counts the
baseline each side inherited at fork. When no baseline file exists (the
dormant single-runner case, or a staged orphan that carries only the hash),
`baseline` is None and the merge degrades to the original 2-way union+SUM, so
behavior is unchanged where 3-way content is unavailable.

BACKWARD-COMPATIBLE / DORMANT: in single-runner there are no non-reducer worker
Bodies, so no `closed-pending-merge` manifest ever exists; `generalize_down`
returns an empty summary (the 1-body / 0-body no-op) and never touches the
reducer WM. It activates only once a 2nd worker Body forks (Phase 2, g-306-65).

CLI:
  py -3 core/scripts/body-merge.py generalize-down --agent <mindKey> [--output json]

Prints a JSON summary {agent, scanned, merged[], noop[], skipped[], passes}.
Exit 0 on success (incl. nothing-to-merge); non-zero + stderr on a hard error.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import yaml  # noqa: E402


def _load_body_manifest():
    """Load the hyphen-named body-manifest.py (not importable by name).

    body-manifest.py is the SOLE manifest writer/reader — we reuse its path
    helpers (_agent_dir, _STATE_DIRNAME, _SESSIONS_DIRNAME, _WM_FILENAME) and
    its set_state/read_manifest. Cached in sys.modules so a repeat load no-ops.
    """
    cached = sys.modules.get("body_manifest")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        "body_manifest", SCRIPT_DIR / "body-manifest.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["body_manifest"] = mod
    spec.loader.exec_module(mod)
    return mod


bm = _load_body_manifest()  # noqa: E402

# Slot/field names that take reducer-wins (never merged from the Body). The two
# MAP_SLOTS hold the reducer's authoritative session context; session identity
# fields describe the reducer's session, not the Body's.
REDUCER_WINS_KEYS = frozenset(
    {"active_context", "archived_context", "session_id", "session_start"}
)

_ISO_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
_PENDING_STATE = "closed-pending-merge"
_MERGED_STATE = "merged"
_STAGED_DIRNAME = "pending-body-merges"  # : orphan-WM staging dir (cleanup-stale-bindings.sh)
_STAGED_HASH_SUFFIX = "-wm.hash"  # : forked_wm_hash sidecar staged with an orphan WM
# The fork-time WM snapshot (the 3-way-delta common ancestor) filename is owned
# by body-manifest.py (the fork writer); read here as bm._BASELINE_FILENAME.


def _project_root() -> Path:
    return SCRIPT_DIR.parent.parent


def _is_iso_ts(s: str) -> bool:
    return isinstance(s, str) and bool(_ISO_TS_RE.match(s))


def _content_hash(item) -> str:
    """Stable content hash for array dedup (order-insensitive on dict keys)."""
    try:
        blob = json.dumps(item, sort_keys=True, default=str, ensure_ascii=True)
    except (TypeError, ValueError):
        blob = repr(item)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _dedup_append(reducer_list: list, body_list: list) -> list:
    """Union: keep all reducer items (in order), append body items not already
    present by content hash. Reducer order is preserved; new body items follow."""
    out = list(reducer_list)
    seen = {_content_hash(x) for x in reducer_list}
    for x in body_list:
        h = _content_hash(x)
        if h not in seen:
            out.append(x)
            seen.add(h)
    return out


def _merge_value(key: str, r_val, b_val, base_val=None):
    """Merge one reducer value with the corresponding body value per policy.

    `base_val` is the fork-time BASELINE value (the common ancestor) when a
    `forked-wm-baseline` is available — it enables a true 3-way delta on numeric
    counters. `None` (no baseline content) falls back to the 2-way policy, so
    callers without a baseline behave exactly as before (backward-compatible).
    """
    # Absence rules first: a side that has nothing contributes nothing.
    if r_val is None:
        return b_val
    if b_val is None:
        return r_val
    # Name-based override: reducer-wins keys are never merged from the Body.
    if key in REDUCER_WINS_KEYS:
        return r_val
    # Arrays: append + content-hash dedup. The union policy is baseline-immune —
    # dedup already drops body copies of baseline-shared items, so 2-way == 3-way.
    if isinstance(r_val, list) and isinstance(b_val, list):
        return _dedup_append(r_val, b_val)
    # Bools are NOT counters — never SUM them (True+True == 2). Reducer-wins.
    if isinstance(r_val, bool) or isinstance(b_val, bool):
        return r_val
    # Numeric counters: 3-way DELTA when the baseline value is known
    # (reducer + the body's divergence from the common ancestor), else 2-way SUM.
    # WHY 3-way fixes a real bug: both sides forked from baseline B, so 2-way
    # `r + b` counts B twice (each side carries it); `r + (b - B)` counts B once
    # (the reducer already holds it) and adds only the body's net work. A Body
    # that never advanced the counter (b == B) contributes 0 -> merged == r.
    if isinstance(r_val, (int, float)) and isinstance(b_val, (int, float)):
        if isinstance(base_val, (int, float)) and not isinstance(base_val, bool):
            return r_val + (b_val - base_val)
        return r_val + b_val
    # Dicts (loop_state, signals): recurse per key (thread the baseline down so
    # nested counters like loop_state.signals.* get the same 3-way treatment).
    if isinstance(r_val, dict) and isinstance(b_val, dict):
        return _merge_dict(r_val, b_val, base_val if isinstance(base_val, dict) else None)
    # ISO timestamp strings: latest-wins (ISO-8601 sorts lexically).
    if isinstance(r_val, str) and isinstance(b_val, str) and _is_iso_ts(r_val) and _is_iso_ts(b_val):
        return max(r_val, b_val)
    # Other scalars / type mismatch: reducer-wins (canonical).
    return r_val


def _merge_dict(reducer: dict, body: dict, baseline: dict | None = None) -> dict:
    """Recursively merge two dicts (union of keys, per-key policy).

    `baseline` (the fork-time common ancestor dict, or None) is threaded into
    `_merge_value` so nested numeric counters get the 3-way delta treatment.
    """
    merged = dict(reducer)
    base = baseline or {}
    for k, b_val in body.items():
        if k in reducer:
            merged[k] = _merge_value(k, reducer[k], b_val, base.get(k))
        else:
            merged[k] = b_val
    return merged


def merge_wm(reducer: dict, body: dict, baseline: dict | None = None) -> dict:
    """Merge a Body's WM dict into the reducer's WM dict under per-slot policies.

    Preserves the reducer's structure (`slots`, `slot_meta`, top-level keys).
    `slots` merge per-policy; `slot_meta` is reducer-wins (Body-only metas added).
    `baseline` is the Body's fork-time WM (the common ancestor). When provided,
    numeric counters use a 3-way delta (no baseline double-count); when None the
    merge is the original 2-way union+SUM (backward-compatible / dormant case).
    """
    merged = dict(reducer)
    base = baseline or {}
    base_slots = base.get("slots") or {}

    # Top-level keys (everything except the two structural maps).
    for k, b_val in body.items():
        if k in ("slots", "slot_meta"):
            continue
        if k in reducer:
            merged[k] = _merge_value(k, reducer[k], b_val, base.get(k))
        else:
            merged[k] = b_val

    # slots: per-policy merge (baseline value per-slot threads the 3-way delta).
    r_slots = reducer.get("slots") or {}
    b_slots = body.get("slots") or {}
    m_slots = dict(r_slots)
    for sk, b_val in b_slots.items():
        if sk in r_slots:
            m_slots[sk] = _merge_value(sk, r_slots[sk], b_val, base_slots.get(sk))
        else:
            m_slots[sk] = b_val
    merged["slots"] = m_slots

    # slot_meta: reducer-wins; add Body-only slot metadata so a new Body slot
    # carries its meta forward (else wm.py would synthesize a default on access).
    r_meta = reducer.get("slot_meta") or {}
    b_meta = body.get("slot_meta") or {}
    m_meta = dict(r_meta)
    for sk, mv in b_meta.items():
        if sk not in r_meta:
            m_meta[sk] = mv
    if r_meta or b_meta:
        merged["slot_meta"] = m_meta

    return merged


def _read_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _write_yaml_atomic(path: Path, data: dict) -> None:
    """Atomic YAML write matching wm.py write_yaml (same dump flags + tmp rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    os.replace(tmp, path)


def _enumerate_pending(sessions_root: Path, already: set) -> list:
    """Return [(unitKey, manifest_dict), ...] for closed-pending-merge Bodies
    not already processed this run. Sorted by unitKey for determinism."""
    out = []
    if not sessions_root.is_dir():
        return out
    for manifest_path in sorted(sessions_root.glob("*/body-manifest.yaml")):
        unit_key = manifest_path.parent.name
        if unit_key in already:
            continue
        try:
            manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if manifest.get("body_state") == _PENDING_STATE:
            out.append((unit_key, manifest))
    return out


def _unlink_quiet(p: Path) -> None:
    """Best-effort unlink; a missing/locked file is not an error here."""
    try:
        p.unlink()
    except OSError:
        pass


def _read_text_strip(p: Path) -> str:
    """Read + strip a small sidecar file; '' when absent/unreadable."""
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _consume_staged(state_dir: Path, reducer_wm_path: Path, summary: dict,
                    already: set) -> None:
    """Drain orphan Body WMs staged by cleanup-stale-bindings.sh ().

    When cleanup-stale-bindings.sh reaps a stale Body dir before the Body's
    closed-pending-merge WM was consolidated, it copies the WM to
    session/pending-body-merges/<unitKey>-wm.yaml and (g-306-70) the Body's
    `forked_wm_hash` to <unitKey>-wm.hash. The staging dir lives under session/
    (independent of sessions/), so this runs whether or not a sessions/ dir
    exists.

    Three guards before merging a staged orphan (g-306-70 hardening over the
    earlier "merge unconditionally" behavior):
      1. DEDUP — if this unitKey was ALREADY merged in the sessions-pass this
         run (`already`), the cleanup raced generalize-down (the staged copy AND
         the sessions/<unitKey>/ dir were both visible): consume the staged copy
         WITHOUT merging, closing the concurrent-double-merge window the 1D
         scaffolding left open (a Body merging from sessions/ AND re-merging from
         staging).
      2. NO-OP SHORT-CIRCUIT — if the staged hash sidecar is present and the
         staged WM still hashes to it, the Body never diverged from its fork
         baseline: consume without merging (mirrors the sessions-pass hash
         short-circuit, which staged orphans previously lacked, so a
         never-diverged orphan used to be merged as if divergent).
      3. otherwise merge (2-way: only the hash, not the baseline CONTENT, is
         staged, so a staged orphan cannot do the 3-way delta the sessions-pass
         can — it still merges correctly under the union+SUM policy).

    Each staged WM (+ its hash sidecar) is deleted after processing so it is
    consumed exactly once (malformed/empty ones dropped, never retried).
    Mutates `summary` (staged_merged / staged_dedup / noop / skipped / scanned).
    In single-runner no Body forks -> the staging dir is absent -> no-op (dormant).
    """
    staged_dir = state_dir / _STAGED_DIRNAME
    if not staged_dir.is_dir():
        return
    reducer_wm = _read_yaml(reducer_wm_path)
    staged_changed = False
    for staged_path in sorted(staged_dir.glob("*-wm.yaml")):
        unit_key = staged_path.name[: -len("-wm.yaml")]
        summary["scanned"] += 1
        hash_path = staged_dir / f"{unit_key}{_STAGED_HASH_SUFFIX}"
        # Guard 1: already merged from sessions/ this run -> consume, don't merge.
        if unit_key in already:
            summary["staged_dedup"].append(unit_key)
            _unlink_quiet(staged_path)
            _unlink_quiet(hash_path)
            continue
        try:
            body_bytes = staged_path.read_bytes()
            body_wm = yaml.safe_load(body_bytes) or {}
        except (OSError, yaml.YAMLError):
            body_bytes = b""
            body_wm = None
        if isinstance(body_wm, dict) and body_wm:
            # Guard 2: staged WM unchanged from the fork baseline -> no-op.
            baseline_hash = _read_text_strip(hash_path)
            if baseline_hash and hashlib.sha256(body_bytes).hexdigest() == baseline_hash:
                summary["noop"].append(unit_key)
            else:
                reducer_wm = merge_wm(reducer_wm, body_wm)
                staged_changed = True
                summary["staged_merged"].append(unit_key)
        else:
            summary["skipped"].append(unit_key)
        _unlink_quiet(staged_path)  # consume exactly once (incl. malformed/empty)
        _unlink_quiet(hash_path)
    if staged_changed:
        _write_yaml_atomic(reducer_wm_path, reducer_wm)


def generalize_down(agent: str, project_root: Path | None = None,
                    max_passes: int = 2) -> dict:
    """Merge every closed-pending-merge Body's WM into the reducer's WM.

    Returns a summary dict. No-op (empty merged/noop/skipped) when no
    closed-pending-merge Body exists — the dormant single-runner case.
    """
    pr = project_root or _project_root()
    adir = bm._agent_dir(pr, agent)  # validates agent name; raises on bad input
    state_dir = adir / bm._STATE_DIRNAME
    sessions_root = adir / bm._SESSIONS_DIRNAME
    reducer_wm_path = state_dir / bm._WM_FILENAME

    summary = {
        "agent": agent,
        "scanned": 0,
        "merged": [],
        "noop": [],
        "skipped": [],
        "staged_merged": [],
        "staged_dedup": [],  # : staged orphans skipped (already merged this run)
        "passes": 0,
    }
    if not sessions_root.is_dir():
        # No Body ever forked a sessions/<unitKey>/ dir -> skip the sessions-pass,
        # but still drain staged orphans: their staging dir lives under session/
        # (not sessions/), so cleanup-stale-bindings can leave staged WMs even
        # when no sessions/ dir exists. . No sessions-pass ran -> the
        # already-merged set is empty.
        _consume_staged(state_dir, reducer_wm_path, summary, set())
        return summary  # nothing else to merge

    already: set = set()
    for _pass in range(max_passes):
        pending = _enumerate_pending(sessions_root, already)
        if not pending:
            break
        summary["passes"] = _pass + 1
        reducer_wm = _read_yaml(reducer_wm_path)
        changed = False
        for unit_key, manifest in pending:
            summary["scanned"] += 1
            already.add(unit_key)
            body_wm_path = sessions_root / unit_key / bm._WM_FILENAME
            if not body_wm_path.is_file():
                # Manifest says pending but the Body never forked a WM file
                # (or it was reaped). Nothing to merge — close it out.
                bm.set_state(unit_key, agent, _MERGED_STATE, pr)
                summary["skipped"].append(unit_key)
                continue
            body_bytes = body_wm_path.read_bytes()
            baseline_hash = manifest.get("forked_wm_hash")
            if baseline_hash and hashlib.sha256(body_bytes).hexdigest() == baseline_hash:
                # Body never diverged from its fork baseline -> no-op merge.
                bm.set_state(unit_key, agent, _MERGED_STATE, pr)
                summary["noop"].append(unit_key)
                continue
            body_wm = yaml.safe_load(body_bytes) or {}
            if not isinstance(body_wm, dict):
                bm.set_state(unit_key, agent, _MERGED_STATE, pr)
                summary["skipped"].append(unit_key)
                continue
            # 3-way delta (): load the fork-time baseline if preserved
            # so numeric counters merge by their net divergence from the common
            # ancestor, not a 2-way SUM that double-counts the shared baseline.
            # Absent baseline -> None -> 2-way fallback (backward-compatible).
            baseline = _read_yaml(sessions_root / unit_key / bm._BASELINE_FILENAME) or None
            reducer_wm = merge_wm(reducer_wm, body_wm, baseline)
            changed = True
            bm.set_state(unit_key, agent, _MERGED_STATE, pr)
            summary["merged"].append(unit_key)
        if changed:
            _write_yaml_atomic(reducer_wm_path, reducer_wm)  # copy-back

    # /: drain staged orphans, skipping any unit_key already
    # merged in the sessions-pass above (the concurrent-double-merge guard).
    _consume_staged(state_dir, reducer_wm_path, summary, already)
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    gd = sub.add_parser("generalize-down")
    gd.add_argument("--agent", required=True)
    gd.add_argument("--output", default="json", choices=["json", "text"])
    args = parser.parse_args(argv)

    try:
        if args.cmd == "generalize-down":
            summary = generalize_down(args.agent)
            if args.output == "json":
                print(json.dumps(summary))
            else:
                print(
                    f"generalize-down {args.agent}: "
                    f"{len(summary['merged'])} merged, {len(summary['noop'])} noop, "
                    f"{len(summary['skipped'])} skipped (scanned {summary['scanned']})"
                )
    except (ValueError, FileNotFoundError) as e:
        print(f"body-merge: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"body-merge: io failed: {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
