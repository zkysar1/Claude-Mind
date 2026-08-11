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
# -c: the fork-time BASELINE staged alongside an orphan WM, so a staged
# orphan can do the same 3-way delta the sessions-pass does. Deliberately NOT
# matched by the `*-wm.yaml` drain glob (it ends `-wm-baseline.yaml`), so it can
# never be mistaken for a staged WM. Mirrored by hand in cleanup-stale-bindings.sh
# (the producer) — the same bash/python boundary _STAGED_HASH_SUFFIX carries.
_STAGED_BASELINE_SUFFIX = "-wm-baseline.yaml"
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


def _get_backend():
    """Storage backend for authoritative staged-file access, or None.

    Module-level seam so the hermetic tests can stub a backend whose store
    diverges from the local dir — the exact state own-cloud read-through
    caching produces (g-306-187). Never raises: no backend means local-only
    operation, which is complete on a local-backend box and the best
    available degradation elsewhere.
    """
    try:
        from storage_backend import get_backend
        return get_backend()
    except Exception:  # noqa: BLE001 — fail-open to local-only
        return None


def _read_staged_bytes(backend, path: Path):
    """(bytes|None, transient) — authoritative-first read of one staged WM.

    Reads the STORE copy first (read_authoritative_bytes never touches the
    possibly-stale local mirror — the staleness half of the g-115-4154
    class); store-absent falls back to the local file so a never-pushed
    local-only staging still drains. `transient=True` means a transport
    error hid the store copy AND no local copy exists — the caller must
    leave the unit staged rather than consume bytes it never saw.
    """
    if backend is not None:
        try:
            return backend.read_authoritative_bytes(path.resolve()), False
        except FileNotFoundError:
            pass  # not in the store: local-only staging (or a consume race)
        except Exception:  # noqa: BLE001 — transport hiccup
            if not path.is_file():
                return None, True
    try:
        return path.read_bytes(), False
    except OSError:
        return None, False


def _read_staged_text(backend, path: Path) -> str:
    """Hash-sidecar read, authoritative-first; '' when absent everywhere."""
    if backend is not None:
        try:
            return backend.read_authoritative_bytes(
                path.resolve()).decode("utf-8", errors="replace").strip()
        except Exception:  # noqa: BLE001 — absent or hiccup: try local
            pass
    return _read_text_strip(path)


def _read_staged_yaml(backend, path: Path):
    """Baseline-sidecar read, authoritative-first; None when absent/invalid.

    None (not {}) so the caller's 3-way merge degrades to the retained 2-way
    fallback exactly as the local-only path always has (see Guard 3 comment).
    """
    if backend is not None:
        try:
            data = yaml.safe_load(backend.read_authoritative_bytes(path.resolve()))
            return data if isinstance(data, dict) and data else None
        except Exception:  # noqa: BLE001
            pass
    try:
        return _read_yaml(path) or None
    except (OSError, yaml.YAMLError):
        return None


def _delete_staged(backend, *paths: Path) -> None:
    """Consume staged files exactly once: local unlink + authoritative delete.

    A local-only unlink is the third layer of the g-306-187 defect: on an
    own-cloud reducer the authoritative copy lives in the store, so deleting
    only the mirror leaves an object that re-materializes and RE-MERGES on
    the next consolidation (a 3-way delta applied twice double-counts
    counters). Best-effort per file but LOUD on failure — a surviving
    authoritative object is a future double-merge, not cosmetic residue.
    LocalBackend has no delete_object: there the local unlink IS the
    authoritative delete (getattr guard, correct by construction). The
    content being deleted was merged into the reducer WM (or proven
    baseline-identical / already-merged), so nothing unrecoverable is
    destroyed; the bucket is additionally versioned (delete marker only).
    """
    deleter = getattr(backend, "delete_object", None)
    for p in paths:
        _unlink_quiet(p)
        if deleter is None:
            continue
        try:
            deleter(p.resolve())
        except Exception as e:  # noqa: BLE001 — never abort the drain
            print(
                f"[body-merge] WARN: authoritative delete failed for {p.name}: "
                f"{e} — this unit may re-merge at the next consolidation",
                file=sys.stderr)


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
      3. otherwise merge. 3-WAY when the producer staged the fork-time baseline
         CONTENT as <unitKey>-wm-baseline.yaml (g-306-119-c), matching what the
         sessions-pass does from sessions/<unitKey>/forked-wm-baseline.yaml;
         2-way union+SUM otherwise. The 2-way path is a RETAINED FALLBACK, not
         dead code: stagings written before the baseline producer shipped, and
         any crash-preserve path that could not capture a baseline, still land
         here and must still merge correctly.

    WHY THE 3-WAY MATTERS (the defect, not the plumbing): under 2-way union+SUM
    a counter the worker NEVER TOUCHED is summed into the reducer's value a
    second time, because both sides carry the shared baseline. `r + (b - B)`
    counts B once. So the observable symptom is a counter DRIFTING UPWARD on
    every orphan drain — including for slots the orphan never wrote.

    CROSS-BOX (g-306-187): a REMOTE worker's staged files arrive via
    body-manifest.py push-staged -> the authoritative store, and NOTHING on
    the reducer box reads those exact names — so under own-cloud read-through
    caching the local staging dir stays empty and a local-only glob drains
    nothing, silently (the g-115-4154 enumerate-by-what-this-box-holds class;
    live specimen: 3 staged files for one unitKey survived a full reducer
    stop/start cycle un-consumed, 2026-08-04). All three surfaces now route
    through the backend: ENUMERATE (list_dir union'd with the local glob),
    READ (read_authoritative_bytes first, local fallback), DELETE
    (delete_object beside each unlink). Merged units delete only AFTER the
    merged reducer WM is durably written: array slots (spark_capture — the
    learning payload) re-merge idempotently via content-hash dedup, so a
    crash between write and delete costs at worst a bounded counter re-add,
    while delete-first would let a crash destroy the divergence outright.

    Each staged WM (+ its hash and baseline sidecars) is deleted after
    processing so it is consumed exactly once (malformed/empty ones dropped,
    never retried). Exception: a unit whose authoritative read failed on a
    TRANSPORT error with no local copy is left staged (`staged_deferred`) —
    consuming bytes never seen would destroy the worker's divergence on an
    S3 hiccup.
    Mutates `summary` (staged_merged / staged_dedup / staged_deferred / noop
    / skipped / scanned).
    In single-runner no Body forks -> the staging dir is absent locally and
    empty in the store -> no-op (dormant).
    """
    staged_dir = state_dir / _STAGED_DIRNAME
    backend = _get_backend()
    staged_names: set = set()
    if staged_dir.is_dir():
        staged_names.update(p.name for p in staged_dir.glob("*-wm.yaml"))
    if backend is not None:
        # UNION with the authoritative listing, never replace: a local-only
        # staging (cleanup-stale-bindings on THIS box, never pushed) must
        # still drain when the store errors or lags. `.resolve()` is
        # load-bearing — a relative path makes _s3_key raise inside the
        # backend and the listing silently degrades (the _fleet_diary.py
        # lesson). The baseline sidecar ends "-wm-baseline.yaml", which does
        # NOT match endswith("-wm.yaml") — same disjointness the local glob
        # relies on (see _STAGED_BASELINE_SUFFIX comment above).
        try:
            staged_names.update(
                n for n in backend.list_dir(staged_dir.resolve())
                if n.endswith("-wm.yaml"))
        except Exception:  # noqa: BLE001 — store listing is additive, never fatal
            pass
    if not staged_names:
        return
    reducer_wm = _read_yaml(reducer_wm_path)
    staged_changed = False
    merged_files: list = []  # authoritative deletes deferred past the WM write
    for name in sorted(staged_names):
        staged_path = staged_dir / name
        unit_key = name[: -len("-wm.yaml")]
        summary["scanned"] += 1
        hash_path = staged_dir / f"{unit_key}{_STAGED_HASH_SUFFIX}"
        baseline_path = staged_dir / f"{unit_key}{_STAGED_BASELINE_SUFFIX}"
        # Guard 1: already merged from sessions/ this run -> consume, don't merge.
        if unit_key in already:
            summary["staged_dedup"].append(unit_key)
            _delete_staged(backend, staged_path, hash_path, baseline_path)
            continue
        body_bytes, transient = _read_staged_bytes(backend, staged_path)
        if transient:
            summary["staged_deferred"].append(unit_key)
            continue
        body_wm = None
        if body_bytes is not None:
            try:
                body_wm = yaml.safe_load(body_bytes) or {}
            except yaml.YAMLError:
                body_wm = None
        if isinstance(body_wm, dict) and body_wm:
            # Guard 2: staged WM unchanged from the fork baseline -> no-op.
            baseline_hash = _read_staged_text(backend, hash_path)
            if baseline_hash and hashlib.sha256(body_bytes).hexdigest() == baseline_hash:
                summary["noop"].append(unit_key)
                _delete_staged(backend, staged_path, hash_path, baseline_path)
            else:
                # Guard 3: 3-way when the baseline CONTENT was staged, else the
                # retained 2-way fallback. None (not {}) collapses an empty or
                # unreadable baseline to the 2-way path rather than merging
                # against {} — against {} every counter's base_val is absent,
                # which silently degrades to 2-way anyway but would read as a
                # 3-way merge. This input comes from the CRASH-preserve path,
                # so a truncated or half-copied baseline is a real shape here
                # in a way it is not in the sessions-pass. Falling back to
                # 2-way merges the orphan with a stale-but-correct policy;
                # raising would abort the whole drain and strand every later
                # staged unit in the loop.
                baseline_wm = _read_staged_yaml(backend, baseline_path)
                reducer_wm = merge_wm(reducer_wm, body_wm, baseline_wm)
                staged_changed = True
                summary["staged_merged"].append(unit_key)
                merged_files.append((staged_path, hash_path, baseline_path))
        else:
            summary["skipped"].append(unit_key)
            _delete_staged(backend, staged_path, hash_path, baseline_path)
    if staged_changed:
        _write_yaml_atomic(reducer_wm_path, reducer_wm)
    # Consume merged units only now — after their content is durably in the
    # reducer WM (see the CROSS-BOX docstring paragraph for the crash trade).
    for files in merged_files:
        _delete_staged(backend, *files)


def _completed_goal_ids(wm: dict) -> list:
    """goal_id values from a WM's `goals_completed_this_session` slot.

    Tolerates BOTH live row shapes, which is not defensive padding — the two
    coexist in the fleet today. The top-level WM slot holds dict rows
    (`{goal_id, aspiration_id, recurring, work_class, _item_ts}`, measured 372
    rows on cc-02 2026-08-07), while the identically-named key INSIDE
    `loop_state` is an int counter and never reaches here. A bare-string row is
    accepted too because `counted_goals_this_session` — the sibling slot an
    author reaching for "the list of completed goals" is equally likely to hand
    this function — is a list of bare ids. Any other shape yields nothing rather
    than a guess.
    """
    rows = wm.get("goals_completed_this_session")
    if not isinstance(rows, list):
        return []
    out = []
    for r in rows:
        gid = r.get("goal_id") if isinstance(r, dict) else (r if isinstance(r, str) else None)
        if gid:
            out.append(gid)
    return out


def _stamp_merged_goal_ids(reducer_wm_path: Path, pre_ids: set, summary: dict) -> None:
    """Name the completed goals that arrived from a Body during this merge.

    THIS IS THE ONLY PLACE THE FLEET RECORDS WORKER-COMPLETION, and it exists
    because measurement showed nothing else does (g-306-198, 2026-08-07):

      * The WM rows carry no session id — union of keys over all 372 live rows
        is {goal_id, aspiration_id, recurring, work_class, _item_ts}.
      * `claimed_by` and `claimed_by_sid` are ERASED at close: 0 of 4015
        completed goals carry either, against 24 of 24 in-progress. That
        erasure is deliberate (g-115-3176 — "the stamp must not survive the
        claim it describes") and must NOT be reverted to make this easier; a
        stamp outliving its claim is how the NEXT claimer inherits a stale SID.
      * `body-manifest.yaml` records {unitKey, mindKey, env_id, role,
        reducer_sid, body_state, started_at, forked_wm_hash} — no goal list.

    So a reducer asking "which of my completed goals did a Body close?" had no
    field to read, and any downstream audit of the worker-learning lanes was
    undecidable by construction (the same shape as g-306-197's A1). The answer
    is only derivable HERE, where both sides of the merge are in hand: an id
    present in the reducer's slot AFTER the merge and absent BEFORE it came
    from a Body. Deriving it anywhere else would mean re-deriving provenance
    from an effect (e.g. "this goal has no journal entry"), which is a
    broadened criterion, not a measurement (guard-2950).

    Fail-safe direction is NO ATTRIBUTION: an unreadable reducer WM yields an
    empty list, so a plumbing fault produces zero retrospective work rather
    than a false claim that some goal was worker-completed.
    """
    seen = set(pre_ids)
    out = []
    for gid in _completed_goal_ids(_read_yaml(reducer_wm_path)):
        if gid not in seen:
            seen.add(gid)
            out.append(gid)
    summary["merged_goal_ids"] = out


def generalize_down(agent: str, project_root: Path | None = None,
                    max_passes: int = 2) -> dict:
    """Merge every closed-pending-merge Body's WM into the reducer's WM.

    Returns a summary dict. No-op (empty merged/noop/skipped) when no
    closed-pending-merge Body exists — the dormant single-runner case.

    `merged_goal_ids` names the completed goals that arrived from a Body this
    run — see `_stamp_merged_goal_ids` for why this is the only place that can
    answer that question. It is the input to the consolidate retrospective
    sub-step (worker_retrospective.py), which runs the reducer-only lanes a
    worker Body structurally skipped.
    """
    pr = project_root or _project_root()
    adir = bm._agent_dir(pr, agent)  # validates agent name; raises on bad input
    state_dir = adir / bm._STATE_DIRNAME
    sessions_root = adir / bm._SESSIONS_DIRNAME
    reducer_wm_path = state_dir / bm._WM_FILENAME
    # Snapshot BEFORE any merge — both the sessions-pass and _consume_staged
    # write this file, so taking the "before" once here (rather than inside
    # either path) keeps the set-difference correct for both without touching
    # either one. .
    pre_goal_ids = set(_completed_goal_ids(_read_yaml(reducer_wm_path)))

    summary = {
        "agent": agent,
        "scanned": 0,
        "merged": [],
        "noop": [],
        "skipped": [],
        "staged_merged": [],
        "staged_dedup": [],  # : staged orphans skipped (already merged this run)
        "staged_deferred": [],  # : authoritative read failed transiently — retry next run
        "merged_goal_ids": [],  # : completed goals that arrived from a Body
        "passes": 0,
    }
    if not sessions_root.is_dir():
        # No Body ever forked a sessions/<unitKey>/ dir -> skip the sessions-pass,
        # but still drain staged orphans: their staging dir lives under session/
        # (not sessions/), so cleanup-stale-bindings can leave staged WMs even
        # when no sessions/ dir exists. . No sessions-pass ran -> the
        # already-merged set is empty.
        _consume_staged(state_dir, reducer_wm_path, summary, set())
        _stamp_merged_goal_ids(reducer_wm_path, pre_goal_ids, summary)
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
    _stamp_merged_goal_ids(reducer_wm_path, pre_goal_ids, summary)
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
