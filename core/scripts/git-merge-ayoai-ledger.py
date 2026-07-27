#!/usr/bin/env python3
# domain-leak-exempt: the dispatch table names literal AyoAI agent-ledger
# basenames (experience.jsonl, changelog.jsonl, experience-meta.json, ...) that
# this git merge driver routes by — they are real repo file names the driver
# operates on, not illustrative examples.
"""git merge driver for AyoAI append/RMW agent ledgers (merge=ayoai-ledger).

Resolves cross-box git conflicts on the record-structured agent ledgers by
RECORD-LEVEL commutative union instead of aborting iteration-push.sh's
integrate step. Reuses the already-tested commutative primitives in
coordination_merge.py (the SAME functions the own-cloud S3 path uses), so
there is NO new merge logic here — only the git-driver protocol glue.
(g-115-2767, from zeta's g-115-2727 investigation: cross-box conflicts on
experience.jsonl / experience-meta.json / changelog.jsonl aborted iteration-push
and stranded MIND commits for 2 consecutive iters g-335-157..g-335-159.)

Git invokes this as:  driver %O %A %B %P
  argv[1] = %O  ancestor/base file (UNUSED — the primitives are 2-way commutative)
  argv[2] = %A  "ours" file   — ALSO the OUTPUT path (driver writes merged here)
  argv[3] = %B  "theirs" file
  argv[4] = %P  pathname in the repo (basename drives dispatch)
Exit 0 = merged cleanly (result written to %A).
Exit 1 = could not merge -> git keeps the conflict (safe fallback; NEVER
         corrupts %A, so iteration-push surfaces it for manual union as before).

Dispatch by basename:
  experience.jsonl / experience-archive.jsonl / journal.jsonl
                        -> id-keyed jsonl union (_union_dict_list key=('id',);
                           journal has no 'id' -> degrades to canon-union =
                           append + exact-dup dedup, no data loss)
  experience-meta.json  -> _merge_counters (MAX-on-numeric; derived/regenerable
                           so a lossy-safe rollup self-heals on next experience-add)
  everything else       -> coordination_merge.merge_handler_for() registry
                           (changelog.jsonl, aspirations.jsonl, and any other
                           registered store share the S3-path handler)
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import coordination_merge as cm  # noqa: E402  (path insert must precede import)

# Basenames NOT registered in coordination_merge._HANDLERS that this driver
# handles via the lower-level primitives. Registered basenames (changelog.jsonl,
# aspirations.jsonl, ...) fall through to merge_handler_for below.
_JSONL_ID_UNION = {"experience.jsonl", "experience-archive.jsonl", "journal.jsonl"}
_COUNTER_JSON = {"experience-meta.json"}


def _parse_jsonl(b: bytes) -> list:
    """Parse jsonl bytes into a list of records (blank lines skipped)."""
    out = []
    for line in b.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


def _dump_jsonl(records: list) -> bytes:
    """Serialize records back to jsonl bytes (one compact object per line).

    The union keeps whole records unchanged (it does not re-key them), so each
    record's own key order is preserved by json.dumps(sort_keys=False)."""
    lines = [json.dumps(r, ensure_ascii=False) for r in records]
    return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")


def merge_bytes(pathname: str, ours: bytes, theirs: bytes) -> bytes:
    """Return the merged bytes for ``pathname`` given the ours/theirs contents.

    Raises on an unhandled basename or a parse error — the caller maps any
    exception to exit 1 (git keeps the conflict; %A is never overwritten)."""
    bn = os.path.basename(pathname)
    if bn in _JSONL_ID_UNION:
        merged = cm._union_dict_list(
            _parse_jsonl(ours), _parse_jsonl(theirs), key_fields=("id",)
        )
        return _dump_jsonl(merged)
    if bn in _COUNTER_JSON:
        a = json.loads(ours.decode("utf-8") or "{}")
        b = json.loads(theirs.decode("utf-8") or "{}")
        return (
            json.dumps(cm._merge_counters(a, b), ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
    handler = cm.merge_handler_for(pathname)
    if handler is not None:
        return handler(ours, theirs)
    raise ValueError(f"no ayoai-ledger handler for basename {bn!r}")


def main(argv) -> int:
    if len(argv) < 4:
        sys.stderr.write("git-merge-ayoai-ledger: expected args %O %A %B [%P]\n")
        return 1
    ours_path, theirs_path = argv[2], argv[3]
    pathname = argv[4] if len(argv) > 4 else ours_path
    try:
        with open(ours_path, "rb") as f:
            ours = f.read()
        with open(theirs_path, "rb") as f:
            theirs = f.read()
        merged = merge_bytes(pathname, ours, theirs)
        # Write ONLY after a successful merge — on any failure %A is untouched,
        # so git keeps the conflict rather than us corrupting the ledger.
        with open(ours_path, "wb") as f:
            f.write(merged)
        return 0
    except Exception as e:  # noqa: BLE001 — never corrupt; signal conflict instead
        sys.stderr.write(f"git-merge-ayoai-ledger: {pathname}: {e}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
