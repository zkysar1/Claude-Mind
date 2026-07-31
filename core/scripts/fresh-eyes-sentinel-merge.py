#!/usr/bin/env python3
"""Merge a NEW fresh_eyes_dispatch_pending payload into an UNCONSUMED existing one.

`iteration-close.sh` wrote this sentinel with an unconditional `wm-set`, and the
slot holds exactly ONE payload. Two deep closes back-to-back -- the normal cadence
of a productive session, not an edge case -- meant the second close silently
CANCELLED the first close's review obligation: the sentinel reads as satisfied
once the second file set is reviewed, and nothing reports the loss.

Measured (echo, 2026-07-30, board msg-20260730-113233-echo-5163 F-003): a close
set core_count=10 (9 argv-hang scripts + a new test, commit 4ef80c13d); before it
was consumed, the next close overwrote it with core_count=3. The 10 files were
never fresh-eyes reviewed.

WHY MERGE AND NOT REFUSE. Refusing the overwrite loses the NEW set instead of the
old one -- the same defect pointing the other way. Both obligations are real, so
both file sets have to survive.

THE DERIVED-COUNT TRAP (rb-3399). A naive union that appends `files` and leaves
`core_count` alone produces a payload whose count contradicts its own list. That
entry is about git-merge unions on append-only stores, but its prescription is the
one that applies here: union BY IDENTITY and RECOMPUTE the derived counts, never
carry a stale one.

`core_count` is NOT `len(files)`. post-state-update-gate.sh caps `files` at 20
(`[:20]`) while `core_count` carries the TRUE core-file count, so a payload with
core_count=34 legitimately ships 20 files. When either input was capped, the true
size of the union is not derivable from the payloads -- so this reports a LOWER
BOUND and says so via `core_count_is_lower_bound`, rather than inventing a number.
Same reason `dropped_files` is emitted: a cap that is not reported reads as
"covered everything" when it did not.

Contract: NEW payload on stdin, EXISTING payload in $FRESH_EYES_EXISTING (env, not
interpolated -- guard-165), merged payload on stdout. Any unusable input falls back
to emitting the NEW payload unchanged, so the worst case is exactly the old
overwrite behavior and this can never make the sentinel worse than it was.
"""

from __future__ import annotations

import json
import os
import sys

# Mirrors post-state-update-gate.sh emit_json `files[:20]`. Keep in sync: a larger
# cap here would emit a payload the gate itself would never produce, and
# /fresh-eyes-code caps its own target set at 20 regardless.
FILE_CAP = 20


def _payload(raw: object) -> dict | None:
    """A usable sentinel payload, or None."""
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw or raw == "null":
            return None
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None
    return raw if isinstance(raw, dict) else None


def _files(p: dict) -> list[str]:
    v = p.get("files")
    return [f for f in v if isinstance(f, str) and f.strip()] if isinstance(v, list) else []


def _int(p: dict, key: str) -> int:
    try:
        return int(p.get(key) or 0)
    except (ValueError, TypeError):
        return 0


def _was_capped(p: dict) -> bool:
    """True when this payload's own file list was truncated by the gate's cap."""
    return _int(p, "core_count") > len(_files(p))


def merge(existing: dict | None, new: dict) -> dict:
    """New payload folded onto an unconsumed existing one (new alone if none)."""
    if existing is None or not existing.get("fired"):
        return new

    out = dict(new)

    # Union BY IDENTITY, existing first so the OLDER unreviewed obligation keeps
    # priority under the cap -- it is the one that has already survived a close.
    seen: set[str] = set()
    union: list[str] = []
    for f in _files(existing) + _files(new):
        if f not in seen:
            seen.add(f)
            union.append(f)

    out["files"] = union[:FILE_CAP]
    dropped = len(union) - len(out["files"])
    if dropped:
        out["dropped_files"] = dropped  # never a silent cap

    # RECOMPUTE, never carry either input's stale count.
    lower_bound = _was_capped(existing) or _was_capped(new)
    if lower_bound:
        # The union's true size is unknowable from two capped lists; each input's
        # own count is a valid lower bound on it, and so is the union we can see.
        out["core_count"] = max(len(union), _int(existing, "core_count"), _int(new, "core_count"))
        out["core_count_is_lower_bound"] = True
    else:
        out["core_count"] = len(union)

    # Disjoint commit sets, so these genuinely add.
    out["loc_changed"] = _int(existing, "loc_changed") + _int(new, "loc_changed")
    scanned = _int(existing, "commits_scanned") + _int(new, "commits_scanned")
    if scanned:
        out["commits_scanned"] = scanned

    # EARLIEST set_at: the age that matters is the oldest un-dispatched
    # obligation's, which is what the stale-sentinel canary reports on.
    stamps = [s for s in (existing.get("set_at"), new.get("set_at")) if isinstance(s, str) and s]
    if stamps:
        out["set_at"] = min(stamps)

    # Keep the first new_script seen -- the older obligation's, if it had one.
    ns = existing.get("new_script") or new.get("new_script")
    if ns:
        out["new_script"] = ns

    out["merged_payloads"] = _int(existing, "merged_payloads") + 1 if _int(existing, "merged_payloads") else 2
    out["reason"] = (
        f"MERGED {out['merged_payloads']} unconsumed dispatch(es); "
        f"prior: {existing.get('reason', '')} | this: {new.get('reason', '')}"
    )
    return out


def main() -> int:
    new = _payload(sys.stdin.read())
    if new is None:
        # Nothing usable to write. Emit nothing and let the caller fall back.
        return 1
    try:
        merged = merge(_payload(os.environ.get("FRESH_EYES_EXISTING")), new)
    except Exception:  # noqa: BLE001 - fail-open: never lose the new payload
        merged = new
    sys.stdout.write(json.dumps(merged))
    return 0


if __name__ == "__main__":
    sys.exit(main())
