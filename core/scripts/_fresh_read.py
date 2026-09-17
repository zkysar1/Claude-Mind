"""Refresh-then-read for governed world/meta stores that go stale locally ().

WHY THIS EXISTS. Under the own-cloud backend the local tree is a read-through
cache, and the eager pull deliberately skips the large `*-archive.jsonl`
stores and never re-pulls one once it has been materialized
(owncloud_sync `_EAGER_PULL_EXCLUDE_GLOBS`). So a reader that opens the local
archive (`path.exists()` + `open()`) sees that box's first pull forever, and
nothing errors (guard-6878). Measured 2026-09-17 on cc-02: the local
guardrails-archive trailed the store by 28 records and the reasoning-bank
archive by 7, both for 11 days. A reader stays current only if it refreshes
on EVERY read, which is what this module does.

WHAT A READ COSTS (measured 2026-09-17, cc-02, own-cloud; see the g-358-125
progress notes). On a gzip-stored archive (199,297 B stored, 773,392 B plain):
  - local copy current -> 1 HEAD, 0 GET
  - local copy stale   -> 1 HEAD + 1 GET of the COMPRESSED size (or a LAN
                          object-cache hit and no remote GET). The ranged tail
                          pull never applies to an encoded object.
The local backend's refresh is a no-op, so tests and local deployments pay
nothing.

SAFETY. `refresh()` force-pulls the remote copy over the local file. For a
per-machine store (never pushed) that overwrites the only good copy, so the
refresh is skipped whenever `owncloud_sync.refresh_would_clobber` says so
(guard-881 / g-333-09). A refresh FAILURE is best-effort: it is reported on
stderr under the caller's label and the read proceeds on the local copy, never
silently.

ADOPTION IS NOT UNIVERSAL (guard-4622). The goal that introduced this module
lists the readers it covers and records a measured reason for each one it
does not. Do not read "who imports this module" as the census of archive
readers: grep for `-archive.jsonl` instead.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List


def refresh_for_read(path: Path, *, label: str) -> None:
    """Pull the store copy of `path` over the local mirror before a read.

    Skipped for per-machine stores (a refresh would clobber them). Any error is
    reported on stderr as ``[<label>] (refresh skipped for <name>: <error>)``
    and swallowed, so the caller's read still runs on the local copy.
    `get_backend` is resolved at CALL time so a test that patches
    `storage_backend.get_backend` reaches this function."""
    try:
        from storage_backend import get_backend
        import owncloud_sync
        be = get_backend()
        if not owncloud_sync.refresh_would_clobber(be, path):
            be.refresh(path)
    except Exception as e:  # noqa: BLE001 - refresh is best-effort
        print(f"[{label}] (refresh skipped for {Path(path).name}: {e})",
              file=sys.stderr)


def read_jsonl_fresh(path: Path, *, label: str) -> List[dict]:
    """`refresh_for_read`, then parse with the SAME reader `locked_modify_jsonl`
    uses inside its lock (`read_jsonl_with_recovery`), so a snapshot and an
    in-lock read skip malformed lines identically. If that reader cannot be
    imported or raises, a plain skip-malformed parse is used. Returns [] if the
    file is absent after the refresh."""
    path = Path(path)
    refresh_for_read(path, label=label)
    if not path.exists():
        return []
    try:
        from _fileops import read_jsonl_with_recovery
        return read_jsonl_with_recovery(path)
    except Exception:  # noqa: BLE001 - fall back to a plain skip-malformed parse
        out = []
        for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
        return out


def read_text_authoritative(path: Path, *, label: str) -> str:
    """Text of the STORE bytes for a store whose local mirror a refresh cannot
    keep current: a PEER agent's per-agent store (g-358-140).

    WHY NOT `refresh_for_read`. agents/ is outside the eager pull, so the
    per-read refresh is the only refresh path, and on these objects it does
    nothing. Measured 2026-09-17 on cc-02 (own-cloud): the local mirror was
    short of the store for 3 of 5 skill-invocations.jsonl files, 3 of 7
    experience.jsonl files and 1 of 5 journal.jsonl files. `refresh_for_read`
    left a +612 B skill-invocations mirror byte-identical (same sha, same
    mtime) because the backend's overwrite decision returned `no_clobber`.
    `read_authoritative_bytes` returned the store bytes.

    NEVER SHORTER THAN THE MIRROR. When the store is a byte prefix of the local
    copy, this box has appended bytes the store does not have yet (its own
    recent write; guard-5369), so the local copy is returned. The consumers
    make destructive decisions from absence, so a short read is the harmful
    direction. Otherwise the store bytes win. On cc-02 every peer ledger store
    extended its local copy; experience and journal diverged.

    A backend error falls back to the local copy and says so on stderr under
    `label`. Raises FileNotFoundError when neither copy exists."""
    path = Path(path)
    local = path.read_bytes() if path.exists() else None
    try:
        from storage_backend import get_backend
        store = get_backend().read_authoritative_bytes(path)
    except FileNotFoundError:
        store = None
    except Exception as e:  # noqa: BLE001 - degrade to the mirror, loudly
        print(f"[{label}] (store read failed for {path.name}: {e}; "
              f"using the local copy)", file=sys.stderr)
        store = None
    if store is None:
        if local is None:
            raise FileNotFoundError(str(path))
        chosen = local
    elif local is not None and len(local) > len(store) and local.startswith(store):
        chosen = local
    else:
        chosen = store
    return chosen.decode("utf-8", errors="replace")


def read_text_for_membership(path: Path, *, label: str) -> str:
    """Both copies when they DIVERGE, for a reader that asks `x in text`.

    SEPARATE FUNCTION, NOT A FLAG ON THE DEFAULT, and that is the whole design
    (g-358-144, from zeta's fresh-eyes F1 on g-358-140). `read_text_authoritative`
    picks ONE copy, which is right for an APPEND ledger and for every COUNTING
    reader: `skill-retire-candidates.load_invocation_counts` tallies rows, so
    handing it both copies would double-count a shared prefix and inflate a
    peer's invocation count — the opposite of the under-count that goal fixed.
    A MEMBERSHIP reader has no such hazard: duplicate text cannot change the
    answer to an `in` test, while a MISSING line can.

    WHY DIVERGENCE HAPPENS AT ALL. `experience.jsonl` is REWRITTEN by the store
    (rotation), not only appended, so the mirror and the store can each hold
    lines the other lacks — neither is a byte prefix of the other, and
    `read_text_authoritative`'s store-wins branch then drops every mirror-only
    line. Measured 2026-09-17 on cc-02 (`uname -r` 6.8.0-139-generic, own-cloud)
    by line-set diff: alpha local_only=30 / store_only=31, bravo 16/17,
    echo 19/20, foxtrot 10/11.

    WHY IT MATTERS HERE. `housekeeping-tick.build_cited_blob` feeds Lane B,
    which KEEPS a session dir when its name appears in the blob and `rmtree`s it
    otherwise. A SID cited only in a mirror-only line reads as uncited, and the
    consequence of the miss is deletion. The risk is LATENT, not live: the same
    day's `probe-lane-b-cited-delta` measured 0 dirs lost and 0 gained. Fixing a
    latent deletion path is still worth it — absence is the harmful direction
    for every consumer of these blobs (guard-5369, and the NEVER SHORTER THAN
    THE MIRROR clause above is the same principle one branch over).

    Returns one copy when they agree or when one extends the other (no reason to
    duplicate), both concatenated when they genuinely diverge, and degrades to
    whatever single copy is readable — loudly, under `label` — exactly as its
    sibling does. Raises FileNotFoundError when neither copy exists."""
    path = Path(path)
    local = path.read_bytes() if path.exists() else None
    try:
        from storage_backend import get_backend
        store = get_backend().read_authoritative_bytes(path)
    except FileNotFoundError:
        store = None
    except Exception as e:  # noqa: BLE001 - degrade to the mirror, loudly
        print(f"[{label}] (store read failed for {path.name}: {e}; "
              f"using the local copy)", file=sys.stderr)
        store = None
    if store is None:
        if local is None:
            raise FileNotFoundError(str(path))
        return local.decode("utf-8", errors="replace")
    if local is None or local == store:
        return store.decode("utf-8", errors="replace")
    # One extends the other: the longer copy already contains every line of the
    # shorter, so a union would only duplicate bytes for no membership gain.
    if local.startswith(store):
        return local.decode("utf-8", errors="replace")
    if store.startswith(local):
        return store.decode("utf-8", errors="replace")
    # Genuine divergence: neither is a prefix, so each may hold lines the other
    # lacks. Concatenate with a newline so the join cannot splice two partial
    # lines into one token that matches neither.
    return (local.decode("utf-8", errors="replace")
            + "\n"
            + store.decode("utf-8", errors="replace"))
