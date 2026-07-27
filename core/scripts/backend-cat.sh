#!/usr/bin/env bash
# backend-cat.sh — S3-authoritative governed-store probe (companion of the
# forged skill probe-governed-store, gap-12).
#
# Reads/lists/probes governed files THROUGH the configured storage backend
# (own-cloud = S3-authoritative via read_authoritative_bytes, a PURE
# to-memory read that never mutates the local mirror; local = plain
# filesystem), replacing the recurring 10-line `python3 -c` incantation
# (source _paths.sh + cd core/scripts + get_backend() ceremony) that gap-12
# recorded across  (write-propagation HEAD probe) and 
# (sharded-store read forensics).
#
# Usage:
#   bash core/scripts/backend-cat.sh cat  <path> [--local]   # store content (--local = mirror as-is)
#   bash core/scripts/backend-cat.sh list <dir>              # one entry name per line
#   bash core/scripts/backend-cat.sh head <path>             # backend, exists, version/ETag, size, local-mirror drift
#
# Paths: absolute, `world/...` / `meta/...` virtual prefixes (resolved via
# _paths.sh — never relative to cwd; see .claude/rules/path-resolution.md),
# or PROJECT_ROOT-relative for anything else.
#
# Exit codes: 0 ok · 1 not found / probe failed · 2 usage error.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"

usage() {
    echo "usage: backend-cat.sh {cat|list|head} <path> [--local]" >&2
    exit 2
}

SUB="${1:-}"
case "$SUB" in cat|list|head) ;; *) usage ;; esac
TARGET="${2:-}"
[ -n "$TARGET" ] || usage
FRESH=1
for a in "${@:3}"; do
    case "$a" in
        --local) FRESH=0 ;;
        --fresh) FRESH=1 ;;
        *) usage ;;
    esac
done

# Values cross into Python via env, never string interpolation (guard-165).
BC_SUB="$SUB" BC_PATH="$TARGET" BC_FRESH="$FRESH" \
BC_ROOT="$PROJECT_ROOT" BC_WORLD="${WORLD_PATH:-}" BC_META="${META_PATH:-}" \
PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}" \
python3 - <<'PYEOF'
import datetime
import hashlib
import os
import sys
from pathlib import Path

from storage_backend import get_backend

sub = os.environ["BC_SUB"]
raw = os.environ["BC_PATH"]
fresh = os.environ.get("BC_FRESH", "1") == "1"
root = os.environ["BC_ROOT"]
world = os.environ.get("BC_WORLD") or ""
meta = os.environ.get("BC_META") or ""

# Virtual-prefix resolution: world/ and meta/ are EXTERNAL roots, not
# PROJECT_ROOT-relative (path-resolution.md). Bare relative paths anchor to
# PROJECT_ROOT so cwd never matters.
p = raw.replace("\\", "/")
if p == "world" or p.startswith("world/"):
    if not world:
        sys.exit("backend-cat: path uses world/ prefix but WORLD_PATH is unset")
    resolved = Path(world) / p[6:] if len(p) > 6 else Path(world)
elif p == "meta" or p.startswith("meta/"):
    if not meta:
        sys.exit("backend-cat: path uses meta/ prefix but META_PATH is unset")
    resolved = Path(meta) / p[5:] if len(p) > 5 else Path(meta)
elif os.path.isabs(raw):
    resolved = Path(raw)
else:
    resolved = Path(root) / p

b = get_backend()

if sub == "cat":
    # Diagnostic reads must be PURE (). read_text(force_fresh=True)
    # routes through _refresh, which (a) downloads S3 INTO the local mirror
    # (rb-3128 read-side clobber) and (b) in the both-diverged no_clobber
    # state returns the LOCAL bytes while claiming freshness.
    # read_authoritative_bytes streams the store object to memory, mutating
    # nothing. --local is likewise a plain mirror read (no cache refresh).
    try:
        if fresh:
            sys.stdout.buffer.write(b.read_authoritative_bytes(resolved))
        else:
            sys.stdout.buffer.write(resolved.read_bytes())
    except FileNotFoundError:
        where = f"{b.name} store" if fresh else "local mirror"
        sys.exit(f"backend-cat: not found in {where}: {resolved}")
elif sub == "list":
    names = b.list_dir(resolved)
    if not names:
        print(f"backend-cat: empty or absent in {b.name} store: {resolved}",
              file=sys.stderr)
        sys.exit(1)
    for n in names:
        print(n)
elif sub == "head":
    st = b.stat(resolved)
    print(f"backend:  {b.name}")
    print(f"path:     {resolved}")
    if st is None:
        print("exists:   false")
        sys.exit(1)
    print("exists:   true")
    # version = S3 ETag (content md5 for single-part PUTs) on own-cloud,
    # str(st_mtime_ns) on local.
    print(f"version:  {st.version}")
    print(f"size:     {st.size}")
    if st.mtime_ns:
        mt = datetime.datetime.fromtimestamp(st.mtime_ns / 1e9)
        print(f"mtime:    {mt.isoformat(timespec='seconds')}")
    # Local-mirror drift indicator — the  use case: did the write
    # actually propagate, and does the local mirror match the store?
    if b.name == "local":
        print("local:    (local backend — the file IS the store)")
    else:
        try:
            lst = resolved.stat()
            local_md5 = hashlib.md5(resolved.read_bytes()).hexdigest()
            etag = st.version.strip('"')
            if "-" in etag:
                verdict = "multipart ETag — md5 compare n/a"
            elif local_md5 == etag:
                verdict = "match"
            else:
                verdict = "DRIFT — local mirror differs from store"
            print(f"local:    size={lst.st_size} md5={local_md5} [{verdict}]")
        except FileNotFoundError:
            print("local:    (no local mirror file)")
        except Exception as e:  # probe is best-effort; never mask the HEAD
            print(f"local:    (probe failed: {e.__class__.__name__}: {e})")
PYEOF
