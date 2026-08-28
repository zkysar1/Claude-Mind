#!/usr/bin/env python3
"""tree-body-presence-audit — authoritative-store body-presence auditor for the knowledge tree.

For every ``_tree.yaml`` node carrying a ``file:`` field, compare the ``.md``
body's presence on the LOCAL mirror (``os.path.exists``) against the
AUTHORITATIVE remote store (``backend.stat`` -> ``None`` means the remote
returns not-found). Four buckets:

  synced      local T + remote T   OK — body present both places
  local_only  local T + remote F   NEVER-PUSHED AT-RISK — body only on THIS
                                    box's mirror, never reached the authoritative
                                    store; permanent-loss risk if this box dies.
                                    The holding box CAN re-push (body in hand).
  cache_miss  local F + remote T   harmless read-through cache miss (remote-synced
                                    store; body simply not pulled to this box yet)
  desync      local F + remote F   INDEX-BODY DESYNC — the index entry synced but
                                    the body reached NEITHER this box's mirror NOR
                                    the authoritative store; a registered node with
                                    no retrievable body.

Why this exists: ``tree-read.sh --validate`` checks LOCAL ``os.path.exists`` only,
so it is BLIND to index->remote-absent (both the desync AND the never-pushed-
at-risk classes). This audit is the authoritative-store-aware complement — the
``backend.stat is None`` signal is exactly what a local existence check
structurally cannot produce.

BOTH DIRECTIONS (g-115-3237). The four buckets above walk the INDEX and ask
"where is this node's body?", which cannot see a body that exists with no index
entry naming it. ``_scan_orphans`` adds the mirror direction — walk each LANE and
diff against the index — reported under ``orphans``:

  local_unregistered  .md under the tree that no node's ``file:`` names
  s3_unregistered     .md under the tree S3 prefix that no node names

``.archive/`` hits are counted separately (``*_archived``) instead of reported,
because archive snapshots are deliberately unregistered; counting them as
findings would make this audit fire forever on 3 known-good files. Enumerating
only the direction you happen to think of is the recurring defect this closes —
see rb-5204.

Both orphan diffs run against local ∪ AUTHORITATIVE ``_tree.yaml`` — the mirror's
registry lags the store, and diffing against it alone reports sync-window
phantoms (see ``_remote_registered``). ``orphans.registry_source`` names the
baseline the run actually used.

Backend scope: performs remote HEADs ONLY on a remote-synced backend. On the
local backend the four-bucket body-presence scan is a no-op (the local mirror IS
authoritative — there is no separate store to compare against), so the tool is
safe to invoke anywhere. The LOCAL orphan direction still runs there: it is an
index-vs-mirror diff needing no remote, and ``s3_unregistered`` reports ``null``
to mark "not probeable" rather than a misleading empty list.

Single-box caveat (rb-4089): local_only=0 on THIS box does NOT clear the fleet.
The never-pushed-at-risk class is visible ONLY from the box holding the body, so
full at-risk coverage requires EACH box to run this audit. The desync class
(absent everywhere) is detectable from any box.

Read-only: performs remote HEADs and local existence checks; never mutates the
mirror or the store.

Exit codes: 0 clean run; 1 error; 3 findings present — only when
``--exit-on-findings``. Findings = desync + at-risk + NON-ARCHIVE orphans
(``.archive/`` hits are counted, never a finding). A local-backend run can
still exit 3: its local-lane orphan scan produces real findings.
"""
import argparse
import json
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _paths import WORLD_DIR, assert_world_dir  # noqa: E402
from storage_backend import get_backend  # noqa: E402
from _owncloud_codec import decode_response as _codec_decode_response  # noqa: E402  # 

# core/scripts/<this>.py -> parents[2] == PROJECT_ROOT (matches _paths' own
# derivation). Used only for the rare non-"world/"-prefixed file field.
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _resolve(file_field, world_dir, project_root):
    p = str(file_field).replace("\\", "/")
    if p.startswith("world/"):
        return Path(world_dir) / p[len("world/"):]
    return Path(project_root) / p


def _is_local_backend(b):
    """True when the backend is the plain local mirror (no separate remote store)."""
    btype = type(b).__name__
    bname = getattr(b, "name", "") or ""
    return btype == "LocalBackend" or (isinstance(bname, str) and bname.lower().startswith("local"))


def _remote_registered(backend, tree_root, world_dir, project_root):
    """Registered paths per the AUTHORITATIVE registry (the S3 ``_tree.yaml``).

    The local mirror's ``_tree.yaml`` lags the store under read-through caching,
    and during that window every body that arrived alongside the newer registry
    looks unregistered. Diffing bodies against a stale LOCAL registry therefore
    manufactures phantom orphans — observed live 2026-07-26: a node registered
    remotely at 06:36:21 reported as an S3 orphan until the mirror caught up at
    06:38:40, a ~2min false-positive window on a single node.

    Returns a ``set`` of resolved paths on success, or a ``str`` carrying the
    failure cause. Callers treat the failure as "no extra knowledge" (keep the
    local registry) rather than "nothing is registered" — the latter would flag
    every body in the tree. Discriminate on ``isinstance(x, set)``, never on
    truthiness: an empty registry is a legitimate empty set.

    Do NOT "simplify" this to ``backend.ensure_local(...)`` + a local read.
    ``ensure_local`` is ``_refresh(force_fresh=False)`` — TTL-gated
    (``OWNCLOUD_CACHE_TTL``, default 30s) — so a stale-but-present mirror is
    NOT re-pulled and the staleness bug returns, just with a shorter window.
    ``backend.refresh()`` does bypass the TTL, but it WRITES the pulled object
    into the local mirror, which would break this tool's read-only contract.
    A direct get_object is the only form that is both always-fresh and
    non-mutating, which is why it is hand-rolled here.
    """
    try:
        key = backend._s3_key(tree_root / "_tree.yaml")
        obj = backend.s3.get_object(Bucket=backend.bucket, Key=key)
        # : decode through the one transport seam (plain passes through).
        remote = yaml.safe_load(_codec_decode_response(obj, key=key).decode("utf-8")) or {}
    except Exception as ex:  # noqa: BLE001 - best-effort; fall back to the local registry
        # Carry the CAUSE. "unreadable" alone cannot distinguish AccessDenied (a
        # real permissions gap needing action — the  scoped-identity
        # class) from a transient timeout (retry) or a corrupt registry.
        return f"{ex.__class__.__name__}: {ex}"
    return {_resolve(n["file"], world_dir, project_root).resolve()
            for n in (remote.get("nodes") or {}).values() if (n or {}).get("file")}


def _is_archive_key(rel):
    """True when a tree-relative POSIX key lies under an `.archive/` directory.

    THE ONE archive policy for both orphan lanes (g-115-4975, outcome 2). It
    previously existed twice, hand-written and in different forms: the local
    lane tested `".archive" not in p.parts` (exact path COMPONENT) and the S3
    lane tested `"/.archive/" not in k` (SUBSTRING). Component-exact is the
    correct one and is what survives here -- a substring test also has to get
    the boundary slashes right at both ends, and gets them wrong for a
    top-level or trailing component.

    Unification was MEASURED before it was applied, per guard-4807 (a shared
    helper can be strictly worse on the second path when two paths assign the
    same structure different meanings). Scenario table over plain / nested /
    `.archive` / mid-path dot-dir / a directory literally named `x.archive`:
    the two filters agreed 5 of 5, so the divergence was in FORM only and is
    not load-bearing. Consumer grep at the same time: 15 references to
    local_unregistered / s3_unregistered / orphan_findings, all of them in
    core/scripts/tests/test_tree_body_presence_audit.py -- no production
    consumer parses these fields.

    The KEY SHAPE is unified alongside it: the local lane now emits
    `.relative_to(tree_root).as_posix()` rather than `str(...)`, so both lanes
    produce forward slashes on every platform. That is not cosmetic -- the
    union fold in main() is only meaningful if both lanes emit the same shape,
    and `str(WindowsPath)` yields backslashes, which silently degraded that
    union to the old double-counting sum on Windows.
    """
    return ".archive" in rel.split("/")


def _scan_orphans_safe(world_dir, project_root, registered_abs, backend):
    """Orphans are ADDITIVE — their failure must never discard the primary result.

    The four-bucket body-presence classification is this tool's purpose and costs
    ~1250 remote HEADs; the orphan direction is a secondary enumeration bolted on
    beside it. Without this guard a transient ``list_objects_v2`` throttle
    propagates out of ``scan()`` and throws away every completed HEAD — verified
    by probe (g-115-3237 fresh-eyes): a backend whose ``stat()`` succeeded but
    whose listing raised ``SlowDown`` lost the entire run.

    Degrades to a recorded ``error`` instead. ``main()`` treats that error as a
    finding, never as a clean zero — a zero computed from a failed input reads
    exactly like a real one (guard-980), and suppression fails CLOSED (guard-487).
    """
    try:
        return _scan_orphans(world_dir, project_root, registered_abs, backend)
    except Exception as ex:  # noqa: BLE001 - additive direction must not fail the run
        return {"error": f"{ex.__class__.__name__}: {ex}"}


def _scan_orphans(world_dir, project_root, registered_abs, backend):
    """The MIRROR direction: bodies present on a lane but absent from the index.

    The four-bucket scan above walks the index and asks "where is this node's
    body?" — it is structurally blind to a body that exists with no index entry
    pointing at it. This pass walks each LANE and diffs against the index.

      local_unregistered  .md under the tree that no node's ``file:`` names
      s3_unregistered     .md under the tree S3 prefix that no node names

    ``.archive/`` hits are split into ``*_archived`` counts rather than reported
    as findings: archive snapshots are deliberately unregistered, so counting
    them as orphans makes this audit fire forever on 3 known-good files and
    trains the reader to ignore it.

    Registry baseline is local ∪ AUTHORITATIVE (see ``_remote_registered``), not
    the local mirror alone: under read-through caching the mirror's ``_tree.yaml``
    lags the store, and a body that arrived with the newer registry would be
    reported as an orphan for the length of that window. ``registry_source``
    records which baseline was actually used, so a reader can tell a real finding
    from a degraded run.

    S3 side runs only on a remote-synced backend. ``_s3_key`` requires an
    ABSOLUTE path under a configured root — a relative ``world/...`` Path raises
    ValueError, so every path handed to it here is already resolved.
    """
    tree_root = (Path(world_dir) / "knowledge" / "tree").resolve()
    out = {}

    # Union the AUTHORITATIVE registry in BEFORE either diff. A body is an
    # orphan only when NEITHER registry names it; comparing against one lagging
    # registry reports sync-window phantoms on both lanes.
    if hasattr(backend, "s3"):
        remote_reg = _remote_registered(backend, tree_root, world_dir, project_root)
        if isinstance(remote_reg, set):
            registered_abs = registered_abs | remote_reg
            out["registry_source"] = "local+remote"
        else:
            # A str carries the failure CAUSE; anything else is an unexpected
            # shape. Either way the baseline degrades to the LAGGING local
            # registry, so the reason must reach the reader (see the caller's
            # docstring) — never a bare "unreadable".
            out["registry_source"] = f"local (remote registry unreadable: {remote_reg})"
    else:
        out["registry_source"] = "local"

    local_md = {p.resolve() for p in tree_root.rglob("*.md")}
    local_orphans = sorted(local_md - registered_abs)
    _local_keys = [p.relative_to(tree_root).as_posix() for p in local_orphans]
    out["local_unregistered"] = [k for k in _local_keys if not _is_archive_key(k)]
    out["local_unregistered_archived"] = sum(1 for k in _local_keys if _is_archive_key(k))

    if not hasattr(backend, "s3"):
        out["s3_unregistered"] = None  # not probeable without a remote store
        out["s3_unregistered_archived"] = None
        return out

    prefix = backend._s3_key(tree_root) + "/"
    keys, token = [], None
    while True:
        kw = {"Bucket": backend.bucket, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        resp = backend.s3.list_objects_v2(**kw)
        keys.extend(o["Key"] for o in resp.get("Contents", []))
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")

    reg_keys = set()
    for p in registered_abs:
        try:
            reg_keys.add(backend._s3_key(p))
        except ValueError:
            pass  # registered outside a configured root — not S3-addressable
    s3_orphans = sorted(k for k in keys if k.endswith(".md") and k not in reg_keys)
    _s3_keys = [k[len(prefix):] for k in s3_orphans]
    out["s3_unregistered"] = [k for k in _s3_keys if not _is_archive_key(k)]
    out["s3_unregistered_archived"] = sum(1 for k in _s3_keys if _is_archive_key(k))
    return out


def scan(world_dir, project_root, backend=None, quiet=False):
    """Audit every file-bearing tree node. Returns a summary dict.

    On a local backend: returns {local_noop: True, ...} without any remote HEAD.
    On a remote-synced backend: returns the 4-way classification with full
    records for the two actionable buckets (local_only, desync).
    """
    b = backend if backend is not None else get_backend()
    bname = getattr(b, "name", "") or type(b).__name__

    tree_path = Path(world_dir) / "knowledge" / "tree" / "_tree.yaml"
    with open(tree_path) as f:
        tree = yaml.safe_load(f) or {}
    nodes = tree.get("nodes", {}) or {}
    registered_abs = {_resolve(n["file"], world_dir, project_root).resolve()
                      for n in nodes.values() if (n or {}).get("file")}

    if _is_local_backend(b):
        # The four-bucket body-presence scan needs a remote to compare against,
        # so it stays a no-op here. The LOCAL orphan direction does not — it is
        # an index-vs-mirror diff that is fully meaningful without any store.
        return {
            "backend": bname,
            "local_noop": True,
            "note": ("local backend — the local mirror IS authoritative; there is "
                     "no separate remote store to audit against. Orphan scan runs "
                     "local-lane only."),
            "orphans": _scan_orphans_safe(world_dir, project_root, registered_abs, b),
        }

    buckets = {"synced": [], "local_only": [], "cache_miss": [], "desync": [], "probe_error": []}
    no_file = 0
    keys = sorted(nodes.keys())
    for i, key in enumerate(keys):
        node = nodes[key] or {}
        ff = node.get("file")
        if not ff:
            no_file += 1
            continue
        abs_local = _resolve(ff, world_dir, project_root)
        local_present = abs_local.exists()
        try:
            st = b.stat(abs_local)
            remote_present = st is not None
            err = None
        except Exception as ex:  # noqa: BLE001 - probe is best-effort
            remote_present = None
            err = f"{ex.__class__.__name__}: {ex}"
        rec = {"key": key, "file": ff,
               "last_updated": node.get("last_updated"),
               "parent": node.get("parent")}
        if remote_present is None:
            buckets["probe_error"].append({**rec, "err": err})
        elif local_present and remote_present:
            buckets["synced"].append(rec)
        elif local_present and not remote_present:
            buckets["local_only"].append(rec)
        elif (not local_present) and remote_present:
            buckets["cache_miss"].append(rec)
        else:
            buckets["desync"].append(rec)
        if not quiet and (i + 1) % 200 == 0:
            sys.stderr.write(f"...{i + 1}/{len(keys)} scanned\n")
            sys.stderr.flush()

    return {
        "backend": bname,
        "local_noop": False,
        "total_with_file": sum(len(v) for v in buckets.values()),
        "no_file_nodes": no_file,
        "counts": {k: len(v) for k, v in buckets.items()},
        "local_only": buckets["local_only"],   # NEVER-PUSHED AT-RISK (full records)
        "desync": buckets["desync"],            # INDEX-BODY DESYNC (full records)
        "probe_error": buckets["probe_error"],
        "orphans": _scan_orphans_safe(world_dir, project_root, registered_abs, b),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Authoritative-store body-presence audit for the knowledge tree")
    ap.add_argument("--exit-on-findings", action="store_true",
                    help="exit 3 when desync + at-risk + non-archive orphans > 0 "
                         "(for cadence alerting)")
    ap.add_argument("--quiet", action="store_true",
                    help="suppress per-N progress to stderr")
    args = ap.parse_args(argv)
    try:
        assert_world_dir("tree-body-presence-audit")
        result = scan(str(WORLD_DIR), str(PROJECT_ROOT), quiet=args.quiet)
    except Exception as ex:  # noqa: BLE001
        print(json.dumps({"error": f"{ex.__class__.__name__}: {ex}"}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    orph = result.get("orphans") or {}
    # An ERRORED orphan block must never read as a clean zero: a zero computed
    # from a failed probe is indistinguishable from a real one (guard-980), so
    # suppression fails CLOSED (guard-487) and the error itself is a finding.
    orphan_error = bool(orph.get("error"))
    # UNION, never SUM (guard-2625, ). The two lanes enumerate the
    # SAME orphan population from two sides -- measured on cc-07 during the
    #  fire, local_unregistered == s3_unregistered == 1331 -- so
    # adding their lengths reported 2662 findings for 1331 orphans and fired
    # every consumer gate below at double strength. |A u B| is not a function
    # of |A| and |B|: sum() is right only if the sets are disjoint, max() only
    # if one contains the other, and neither relation is knowable from the
    # counts. Both LISTS are in hand here, so the fold is computed where the
    # sets still exist rather than from two lengths, and the per-shape terms
    # stay published alongside it so disjointness remains checkable.
    #
    # SHAPE PRECONDITION, and it is why the sibling half of this goal matters:
    # a set union is only meaningful if both lanes emit the same key shape.
    # They do on POSIX (scenario table, 5 of 5 over plain / nested / .archive /
    # mid-path dot-dir / a dir named `x.archive`). They do NOT on Windows,
    # where the local lane's `str(p.relative_to(tree_root))` yields BACKSLASHES
    # while the S3 lane emits a POSIX key suffix -- there the union degrades to
    # the old sum. Unifying the two hand-written normalisers in _scan_orphans
    # is the remaining fix; until then this is correct on POSIX and no worse
    # than before anywhere else.
    _local = orph.get("local_unregistered") or []
    _s3 = orph.get("s3_unregistered") or []
    orphan_findings = len(set(_local) | set(_s3))
    if result.get("local_noop"):
        # Only the local-lane orphan direction ran; it is still actionable.
        return 3 if (args.exit_on_findings and (orphan_findings > 0 or orphan_error)) else 0
    findings = (len(result.get("desync", [])) + len(result.get("local_only", []))
                + orphan_findings)
    if args.exit_on_findings and (findings > 0 or orphan_error):
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
