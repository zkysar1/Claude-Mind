#!/usr/bin/env python3
"""Decide whether two versions of a file are SEMANTICALLY identical.

Written for iteration-push.sh's self-heal (g-115-5717). The self-heal must
choose between restoring/unstaging a file and DEFERRING the merge, and the
predicate it had was "git says this path differs" -- which cannot tell

  (a) a partner's real uncommitted work           <- must DEFER, never discard
  (b) byte-level churn over identical content     <- safe to restore

from each other. Case (b) is the common case on an own-cloud fleet: a
re-serializing JSON writer reorders keys, and a Windows box rewrites line
endings. It NEVER self-clears, because the sync layer re-churns the file, so
every later iteration defers again -- unbounded stranding (measured on cc-03:
3 consecutive defers, 49 commits behind, unable to push).

THE FAIL-SAFE DIRECTION IS NOT SYMMETRIC AND MUST NOT BE INVERTED. Reporting
"different" when content is identical costs one deferred merge, retried next
iteration. Reporting "identical" when content genuinely differs DESTROYS a
partner's uncommitted work, which nothing can recover. So every ambiguity --
an unparseable file, a parse error, an unknown extension, a decode failure --
resolves to UNPARSEABLE, and the caller treats that exactly as it treats a
real difference: defer. `identical` is returned only on positive proof.

Exit codes (CLI):
  0  identical    -- provably the same content; caller may restore
  1  different    -- a real difference; caller MUST defer
  2  unparseable  -- could not prove either way; caller MUST defer
"""

import argparse
import json
import sys

IDENTICAL = 0
DIFFERENT = 1
UNPARSEABLE = 2

_VERDICT_NAME = {IDENTICAL: "identical", DIFFERENT: "different",
                 UNPARSEABLE: "unparseable"}


def _load_jsonl(text):
    """Parse JSONL into a list of records. Raises ValueError on any bad line.

    Blank lines are skipped -- a trailing newline is formatting, not content,
    and treating it as a difference would defeat the whole purpose here.
    """
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


def _record_key(rec, index):
    """Identity for one JSONL record: its id-ish field, else its position.

    Falling back to POSITION rather than to a content hash is deliberate. A
    content hash would make every record its own key, so two files with the
    same records in a different ORDER would compare equal -- and for an
    append-only store, order is content.
    """
    if isinstance(rec, dict):
        for field in ("id", "goal_id", "hypothesis_id", "key", "record_id"):
            if field in rec:
                return ("k", rec[field])
    return ("i", index)


def compare_jsonl(left, right):
    """Compare two JSONL texts record-by-record, ignoring key ORDER.

    json.loads already normalises key order (a dict compares by content, not
    by source order), so this is exactly the "189 insertions / 189 deletions
    that are pure key-order churn" case the incident measured.
    """
    try:
        lrecs = _load_jsonl(left)
        rrecs = _load_jsonl(right)
    except ValueError:
        return UNPARSEABLE

    if len(lrecs) != len(rrecs):
        return DIFFERENT

    lkeys = [_record_key(r, i) for i, r in enumerate(lrecs)]
    rkeys = [_record_key(r, i) for i, r in enumerate(rrecs)]
    if lkeys != rkeys:
        return DIFFERENT

    for lrec, rrec in zip(lrecs, rrecs):
        if lrec != rrec:
            return DIFFERENT
    return IDENTICAL


def compare_json(left, right):
    try:
        return IDENTICAL if json.loads(left) == json.loads(right) else DIFFERENT
    except ValueError:
        return UNPARSEABLE


def compare_yaml(left, right):
    try:
        import yaml
    except ImportError:
        # No parser -> cannot prove identity -> defer. Never guess.
        return UNPARSEABLE
    try:
        return IDENTICAL if yaml.safe_load(left) == yaml.safe_load(right) else DIFFERENT
    except Exception:
        # yaml raises a family of errors (scanner/parser/composer/constructor);
        # every one of them means "not proven identical".
        return UNPARSEABLE


def compare(left, right, name):
    """Return IDENTICAL / DIFFERENT / UNPARSEABLE for two versions of `name`.

    A byte-identical pair short-circuits to IDENTICAL for ANY extension --
    including extensions this module cannot parse. That is not an optimisation:
    it is the one case where identity is provable without a parser.
    """
    if left == right:
        return IDENTICAL

    lower = name.lower()
    if lower.endswith(".jsonl"):
        return compare_jsonl(left, right)
    if lower.endswith(".json"):
        return compare_json(left, right)
    if lower.endswith((".yaml", ".yml")):
        return compare_yaml(left, right)
    # Unknown extension: bytes already differ and we have no parser for it.
    return UNPARSEABLE


def _read(path):
    """Read a file as text, tolerating CRLF and a BOM.

    newline="" is NOT used: universal-newline translation is what collapses the
    measured CRLF-vs-LF case ("a Windows box wrote it") to equality. utf-8-sig
    drops a BOM, which is likewise an encoding artifact and not content.
    """
    with open(path, "r", encoding="utf-8-sig", errors="strict", newline=None) as fh:
        return fh.read()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("left")
    ap.add_argument("right")
    ap.add_argument("--name", required=True,
                    help="original path, used for extension dispatch")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    try:
        left = _read(args.left)
        right = _read(args.right)
    except (OSError, UnicodeDecodeError):
        # Binary, missing, or undecodable -> cannot prove identity -> defer.
        if not args.quiet:
            print("unparseable")
        return UNPARSEABLE

    verdict = compare(left, right, args.name)
    if not args.quiet:
        print(_VERDICT_NAME[verdict])
    return verdict


if __name__ == "__main__":
    sys.exit(main())
