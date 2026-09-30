#!/usr/bin/env python3
"""tree-retired-fm-keys.py -- census and clock-neutral removal of RETIRED node
front-matter keys (g-115-11490). The key set and the strip itself live in
_tree_fm_retired.py, shared with T21, the merge handler and tree validate.

Modes (one; default --dry-run):
  --check    census: exit 1 when any node file carries a retired key, else 0.
  --dry-run  what --apply would change: per file, the exact line(s) it would
             remove. Writes nothing.
  --apply    remove them. Per carrier: read the STORE's current bytes, strip,
             and write back fenced on the version read (If-Match), so a
             concurrent writer is merged or refused, never overwritten.
             Idempotent: a file whose store copy is already clean is left alone.
  --store    scan each file's STORE copy instead of the local mirror (slower;
             catches a carrier the local mirror has not pulled yet).

Clock-neutral by construction: it never goes through the Edit/Write hooks, so
T21 does not re-stamp last_updated and _tree.yaml is not bumped. Removing a key
nobody reads re-verifies nothing, so a node's freshness must not move
(guard-6455).

Byte-minimal: only the key's own line(s) are removed. Each write is checked
twice, by the helper's re-parse and here by a line diff that must be a pure
deletion; a file failing either is reported and left alone.

Refuses to touch a file whose local mirror differs from its store copy
(status local-diverged): the backend write also rewrites the local file, and
local bytes the store lacks may be unpushed work (guard-4949). Re-run once the
sync has settled.

Scope: every *.md under <world>/knowledge/tree except `_`-prefixed files and
.archive/ snapshots. Output: one JSON document on stdout.
Exit: 0 clean or done, 1 carriers remain, 2 setup error.
"""
import argparse
import difflib
import json
import sys
from concurrent.futures import ThreadPoolExecutor

from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()

from _paths import WORLD_DIR  # noqa: E402
from _tree_fm_retired import (  # noqa: E402
    RetiredKeyStripError, find_retired_fm_keys, strip_retired_fm_keys)

TREE_DIR = WORLD_DIR / "knowledge" / "tree"
_DONE = ("stripped", "merged", "already-clean")


def _node_files():
    for p in sorted(TREE_DIR.rglob("*.md")):
        if p.name.startswith("_") or ".archive" in p.relative_to(TREE_DIR).parts:
            continue
        yield p


def _virtual(p):
    return "world/" + p.relative_to(WORLD_DIR).as_posix()


def _deleted_lines(before, after):
    """Lines removed from `before`, or None when `after` is not a pure deletion
    of whole lines (an independent check of the helper's byte-minimality)."""
    a, b = before.split("\n"), after.split("\n")
    out = []
    for tag, i1, i2, _j1, _j2 in difflib.SequenceMatcher(
            a=a, b=b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if tag != "delete":
            return None
        out.extend(ln.rstrip("\r") for ln in a[i1:i2])
    return out


def _plan(text):
    """(new_text, removed_keys, deleted_lines) or raise RetiredKeyStripError."""
    new, removed = strip_retired_fm_keys(text)
    if not removed:
        return text, [], []
    deleted = _deleted_lines(text, new)
    if deleted is None:
        raise RetiredKeyStripError("the strip is not a pure line deletion")
    return new, removed, deleted


def _scan(files, backend):
    """[(path, text-or-None, error-or-None)] for every file, local or store."""
    def read(p):
        try:
            raw = backend.read_authoritative_bytes(p) if backend else p.read_bytes()
            return p, raw.decode("utf-8"), None
        except (OSError, UnicodeDecodeError, ValueError) as e:
            return p, None, f"{type(e).__name__}: {e}"
    if backend is None:
        return [read(p) for p in files]
    with ThreadPoolExecutor(max_workers=16) as pool:
        return list(pool.map(read, files))


def _apply_one(backend, p):
    st = backend.stat(p)
    if st is None:
        return {"status": "absent-in-store"}
    raw = backend.read_authoritative_bytes(p)
    try:
        text = raw.decode("utf-8")
        new, removed, deleted = _plan(text)
    except UnicodeDecodeError:
        return {"status": "undecodable"}
    except RetiredKeyStripError as e:
        return {"status": "unverifiable", "reason": str(e)}
    if not removed:
        return {"status": "already-clean"}
    try:
        if p.read_bytes() != raw:
            return {"status": "local-diverged", "keys": removed}
    except OSError as e:
        return {"status": "error", "reason": f"local read: {e}"}
    new_b = new.encode("utf-8")
    try:
        if hasattr(backend, "mirror_put"):
            backend.mirror_put(p, new_b, expected_version=st.version)
        else:
            backend.write_bytes(p, new_b)
    except Exception as e:  # noqa: BLE001 -- ConflictError / transport: report, skip
        kind = "conflict" if type(e).__name__ == "ConflictError" else "error"
        return {"status": kind, "reason": f"{type(e).__name__}: {e}"[:300]}
    after = backend.read_authoritative_bytes(p)
    if find_retired_fm_keys(after.decode("utf-8", "replace")):
        return {"status": "still-carrier", "keys": removed}
    return {"status": "stripped" if after == new_b else "merged",
            "keys": removed, "removed_lines": deleted,
            "bytes_before": len(raw), "bytes_after": len(after)}


def main():
    ap = argparse.ArgumentParser(
        description="Census and clock-neutral removal of retired node "
                    "front-matter keys (g-115-11490).")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="census; exit 1 when any file carries a retired key")
    mode.add_argument("--dry-run", action="store_true",
                      help="(default) list the line(s) --apply would remove")
    mode.add_argument("--apply", action="store_true",
                      help="remove them, fenced on the store version read")
    ap.add_argument("--store", action="store_true",
                    help="scan each file's store copy, not the local mirror")
    args = ap.parse_args()
    mode_name = "check" if args.check else "apply" if args.apply else "dry-run"

    if not TREE_DIR.is_dir():
        print(json.dumps({"error": f"tree dir not found: {TREE_DIR}"}))
        return 2
    backend = None
    if args.store or args.apply:
        from storage_backend import get_backend
        backend = get_backend()

    files = list(_node_files())
    scan = _scan(files, backend if args.store else None)
    carriers, unreadable = [], []
    for p, text, err in scan:
        if err:
            unreadable.append({"path": _virtual(p), "reason": err})
        elif find_retired_fm_keys(text):
            carriers.append((p, text))

    report = {"mode": mode_name, "tree_dir": str(TREE_DIR),
              "scanned_from": "store" if args.store else "local",
              "scanned": len(files), "carriers": len(carriers),
              "unreadable": unreadable, "files": []}
    remaining = 0
    for p, text in carriers:
        rec = {"path": _virtual(p), "keys": find_retired_fm_keys(text)}
        if args.dry_run or mode_name == "dry-run":
            try:
                _new, _removed, rec["removed_lines"] = _plan(text)
                rec["status"] = "would-strip"
            except RetiredKeyStripError as e:
                rec.update(status="unverifiable", reason=str(e))
        elif args.apply:
            rec.update(_apply_one(backend, p))
            if rec["status"] not in _DONE:
                remaining += 1
        report["files"].append(rec)
    if args.apply:
        report["by_status"] = {}
        for rec in report["files"]:
            s = rec["status"]
            report["by_status"][s] = report["by_status"].get(s, 0) + 1
        report["remaining"] = remaining
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if unreadable and args.check:
        return 2
    if args.check:
        return 1 if carriers else 0
    if args.apply:
        return 1 if remaining else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
