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
  STILL unregistered    -> _validated_text_merge (see below), else exit 1

UNREGISTERED BASENAMES ARE THE COMMON CASE, NOT THE EDGE (g-115-4253). The
.gitattributes globs route .mind-data/{world,meta}/**/*.{jsonl,yaml,json} — a
population that GROWS on its own — into a dispatcher keyed by hand-enumerated
basename. Measured 2026-07-31: 167 of 253 routed files (66%) have no handler.
Each was a hard stop on first both-sides-touch, and a hard stop is not local to
the file: it leaves the path unmerged, so iteration-push.sh's integrate aborts
and the box stops integrating EVERYTHING. cc-06 stranded 54 commits for 6.2h
behind exactly one such file (backpressure.yaml) with no error anywhere.

The .gitattributes rationale claimed "AN UNREGISTERED BASENAME IS A STRICT
IMPROVEMENT, NOT A RISK ... no worse than today until then." Measured, that is
half right. Decision table for an unhandled basename (probe, 2026-07-31):

    case                        merge_rc  unmerged  markers  parses
    ROUTED   + disjoint edits      1         1         0      yes   <- REGRESSION
    UNROUTED + disjoint edits      0         0         0      yes   (both kept)
    ROUTED   + overlapping         1         1         0      yes   <- protection
    UNROUTED + overlapping         1         1         1      NO    (corrupt)

Routing is a strict improvement over the OVERLAPPING column (no marker
corruption) and a strict REGRESSION over the DISJOINT one (a clean merge became
an aborted integrate). The original measurement covered "handled / unhandled /
unrouted" but never the disjoint cell, so the regression was invisible.

WHY A VALIDATED TEXT MERGE AND NOT A DEFAULT COMMUTATIVE HANDLER. A blanket
union over unregistered stores is refuted by guard-1816 and by the registry's
own DELIBERATELY-NOT-REGISTERED list: dead-ends.jsonl and knowledge-graph.jsonl
are full-file REWRITES, so a union resurrects rows their writers deleted. Git's
3-way text merge has the opposite property — it APPLIES both sides' deletions —
which is precisely what those stores need. So the fallback is git's own merge,
accepted only when it is clean AND the result still parses, never a union.

This takes the best cell of each column: disjoint merges clean (integrate
proceeds), overlapping/add-add conflicts exit 1 with %A untouched (markers=0,
exactly today's behavior). It cannot be worse than the status quo in any
measured case.

KNOWN TRADE (guard-1871): for a DERIVED CACHE with mtime-staleness regeneration,
accepting a merge advances the cache mtime past its source and suppresses one
regeneration cycle. That window is bounded — the next source write re-fires it —
whereas the alternative it replaces strands the whole box. Recorded rather than
elided.
"""
import json
import os
import subprocess
import sys
import tempfile

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


def _wellformed(pathname: str, data: bytes) -> bool:
    """True when ``data`` is still well-formed for ``pathname``'s format.

    A textually-clean 3-way merge can be SEMANTICALLY wrong, so a clean rc from
    git is not sufficient evidence to accept the result. Two shapes are checked:

      * it must parse at all (a merge that produced invalid JSON/YAML is a
        corruption we must not write);
      * for YAML, no DUPLICATE mapping key anywhere. Both sides inserting the
        same key at different offsets merges cleanly at line level, and
        yaml.safe_load silently keeps the LAST — a one-side write dropped with
        no sound. compose_all walks the node tree without constructing objects,
        so this stays cheap and alias-safe.

    An extension with no validator returns False: refusing is the conservative
    direction and costs nothing, since the routing globs only cover these three.
    """
    ext = os.path.splitext(pathname)[1].lower()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if ext == ".jsonl":
        for line in text.splitlines():
            if line.strip():
                try:
                    json.loads(line)
                except ValueError:
                    return False
        return True
    if ext == ".json":
        try:
            json.loads(text or "{}")
        except ValueError:
            return False
        return True
    if ext in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError:
            return False
        try:
            docs = list(yaml.compose_all(text))
        except Exception:  # noqa: BLE001 - any parse failure disqualifies
            return False

        def _no_dupe_keys(node) -> bool:
            if isinstance(node, yaml.MappingNode):
                seen = set()
                for k, v in node.value:
                    if isinstance(k, yaml.ScalarNode):
                        if k.value in seen:
                            return False
                        seen.add(k.value)
                    if not _no_dupe_keys(v):
                        return False
            elif isinstance(node, yaml.SequenceNode):
                for child in node.value:
                    if not _no_dupe_keys(child):
                        return False
            return True

        return all(_no_dupe_keys(d) for d in docs if d is not None)
    return False


def _validated_text_merge(pathname: str, base: bytes, ours: bytes,
                          theirs: bytes) -> "bytes | None":
    """Git's own 3-way text merge, or None when it must not be accepted.

    None on ANY doubt — conflicts, a git failure, or a result that no longer
    parses — so the caller falls through to the historical exit-1 and %A is
    left untouched. %O is finally load-bearing here; the record handlers above
    are 2-way commutative and never needed it."""
    try:
        with tempfile.TemporaryDirectory() as d:
            paths = {}
            for name, blob in (("ours", ours), ("base", base), ("theirs", theirs)):
                paths[name] = os.path.join(d, name)
                with open(paths[name], "wb") as f:
                    f.write(blob)
            # rc is the CONFLICT COUNT (255 on error); only 0 is acceptable.
            proc = subprocess.run(
                ["git", "merge-file", "-p", paths["ours"], paths["base"],
                 paths["theirs"]],
                capture_output=True,
            )
    except Exception:  # noqa: BLE001 - git missing/unusable -> fall through
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout if _wellformed(pathname, proc.stdout) else None


def merge_bytes(pathname: str, ours: bytes, theirs: bytes,
                base: bytes = b"") -> bytes:
    """Return the merged bytes for ``pathname`` given the ours/theirs contents.

    Raises when no handler matches AND the text-merge fallback declines — the
    caller maps any exception to exit 1 (git keeps the conflict; %A is never
    overwritten)."""
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
    merged = _validated_text_merge(pathname, base, ours, theirs)
    if merged is not None:
        return merged
    raise ValueError(f"no ayoai-ledger handler for basename {bn!r}")


def main(argv) -> int:
    if len(argv) < 4:
        sys.stderr.write("git-merge-ayoai-ledger: expected args %O %A %B [%P]\n")
        return 1
    base_path, ours_path, theirs_path = argv[1], argv[2], argv[3]
    pathname = argv[4] if len(argv) > 4 else ours_path
    try:
        with open(ours_path, "rb") as f:
            ours = f.read()
        with open(theirs_path, "rb") as f:
            theirs = f.read()
        # %O is absent/empty on an add/add. Read it best-effort — the fallback
        # treats an empty base as "both sides added everything", which conflicts
        # and declines, i.e. exactly the pre-fix behavior for that shape.
        try:
            with open(base_path, "rb") as f:
                base = f.read()
        except OSError:
            base = b""
        merged = merge_bytes(pathname, ours, theirs, base)
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
