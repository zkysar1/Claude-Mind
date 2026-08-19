#!/usr/bin/env python3
"""Compact the guardrail index to a grouped ID MANIFEST (stdin -> stdout).

WHY THIS EXISTS (g-115-6703). `guardrails-read.sh --summary` emits one line per
guardrail as `guard-NNN: [category] <rule truncated to 80 chars>`. Prime and
worker-loop load it whole on every entry / re-entry. Measured 2026-08-19 (zeta,
cc-02): 467,777 B over 4,099 records in 324 categories, of which the id+category
PREFIX is 136,901 B (29.3%) and the truncated rule TEXT is 326,589 B (69.8%).

The safety floor those callers actually claim is stated verbatim in
prime/SKILL.md: "every guardrail is present BY ID". That is ID coverage, not
rule-text coverage -- and the two are separable. This transformer keeps 100% of
the ids and drops the rule text, which the SAME callers already re-fetch on
demand via the documented expand path (`guardrails-read.sh --id guard-NNN`, or
`--category <cat>` for a whole lane; prime/SKILL.md, guard-1421).

Measured output: 52,258 B for the same 4,099 records = an 88.8% reduction.

WHAT THIS IS NOT. It is not a ranked slice and it is not a filter: every input
record appears in the output exactly once, and `--assert-total` makes that
checkable rather than assumed. guard-841 forbids substituting a ranked subset
for the 100%-coverage index; nothing here drops a record, so that prohibition is
respected rather than worked around. Per-line compression is separately
EXHAUSTED (the rule text is already truncated at exactly 80: p50 = p90 = max =
80), so the only remaining lever was count-driven -- see the goal record.

FAIL LOUD, NEVER SILENTLY THIN. An unparseable input line is an error, not a
skipped record: silently dropping one would breach the very id-coverage floor
this script exists to preserve, and would look identical to a clean run.
"""
import argparse
import collections
import re
import sys

# `guard-001: [category] rule text`  — the colon is optional so the parser does
# not hinge on a punctuation detail of the emitter it does not own.
LINE_RE = re.compile(r"^(\S+?):?\s+\[([^\]]+)\]\s*(.*)$")


def build(lines):
    """Return (manifest_text, record_count, category_count, bad_lines, continuations).

    `--summary` is documented as one line per guardrail, and IS NOT: a rule whose
    text contains an embedded newline wraps onto extra lines. Measured 2026-08-19:
    3 of 4,102 lines (guard-900, guard-4031, guard-4149). A line-based consumer
    that skips non-matching lines mis-counts silently; one that errors on them
    refuses a healthy corpus. Neither is right -- a continuation carries no id, so
    it is attached to the record above it and COUNTED, never dropped and never fatal.

    A non-matching line with NO record above it is still fatal: there is nothing to
    attach it to, so it is genuine garbage rather than a wrap.
    """
    cats = collections.OrderedDict()
    bad = []
    count = 0
    continuations = 0
    seen_any = False
    for raw in lines:
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        m = LINE_RE.match(line)
        if not m:
            if seen_any:
                # Wrapped rule text. The id lives on the first line, which is
                # already recorded, so id coverage is unaffected.
                continuations += 1
            else:
                bad.append(line[:120])
            continue
        gid, cat = m.group(1).rstrip(":"), m.group(2)
        cats.setdefault(cat, []).append(gid)
        count += 1
        seen_any = True
    parts = []
    for cat in sorted(cats):
        ids = cats[cat]
        parts.append("[{}] ({}) {}".format(cat, len(ids), " ".join(ids)))
    text = "\n".join(parts) + ("\n" if parts else "")
    return text, count, len(cats), bad, continuations


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--assert-total",
        type=int,
        default=None,
        help="expected record count; exit 2 if the manifest does not cover exactly this many",
    )
    ap.add_argument(
        "--stats",
        action="store_true",
        help="emit a trailing coverage line to STDERR (never stdout, so callers can consume stdout verbatim)",
    )
    args = ap.parse_args()

    text, count, ncats, bad, continuations = build(sys.stdin)

    if bad:
        sys.stderr.write(
            "[guardrail-manifest] REFUSING: {} leading unparseable line(s) with no record above "
            "them to attach to; a dropped record would breach the 100%% id-coverage floor this "
            "script exists to preserve. First: {!r}\n".format(len(bad), bad[0])
        )
        return 2

    if count == 0:
        sys.stderr.write(
            "[guardrail-manifest] REFUSING: zero records parsed from stdin. An empty manifest "
            "is indistinguishable from a healthy one downstream, so this is an error, not a clean run.\n"
        )
        return 2

    if args.assert_total is not None and count != args.assert_total:
        sys.stderr.write(
            "[guardrail-manifest] COVERAGE MISMATCH: manifest covers {} record(s), expected {}.\n".format(
                count, args.assert_total
            )
        )
        return 2

    sys.stdout.write(text)
    if args.stats:
        sys.stderr.write(
            "[guardrail-manifest] {} records / {} categories / {} bytes / {} wrapped-text "
            "continuation line(s) folded (100% id coverage; expand rule text with "
            "guardrails-read.sh --id <id> or --category <cat>)\n".format(
                count, ncats, len(text.encode("utf-8")), continuations
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
