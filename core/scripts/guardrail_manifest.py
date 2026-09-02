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
import json
import re
import sys

# : prime's guardrail-index store-load budget, enforced at EMIT time.
# 40k tokens is the budget prime/SKILL.md Phase 2 item 3 states; conversion at
# the MEASURED 2.5 B/token ID-dense floor (self.md rule, guard-1478 class) so
# the token estimate is an UPPER bound and the warning fires EARLY, never late.
# WARN, never refuse: an absent index is strictly worse than an oversized one
# (the  lesson — a refusal here would hand prime NO index at all).
# The paired verify-learning check (Section 4T) is the fail-level layer. The
# first breach of this budget reached 3.2x over, silently, because the budget
# lived in prose with no gate — that is the shape this constant retires.
BUDGET_TOKENS = 40_000
ID_DENSE_BYTES_PER_TOKEN = 2.5

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


def render_critical(path):
    """(section_text, n) for the CRITICAL always-load core, or ("", 0).

    g-115-6965: the id manifest realizes the coverage floor but loads NO rule
    text — including for the CRITICAL tier, whose admission rule (see
    prime-store-load-budget.md) is precisely that the trigger zone is NOT
    self-announcing, so the expand-on-demand path cannot cover it. This
    section prepends those few rules IN FULL (guard-1421 honest mode:
    "fewer entries read whole").

    DEGRADES, NEVER REFUSES — the asymmetry with build() is deliberate: the
    refusal there guards the id-coverage floor; this section is additive, and
    its absence is exactly yesterday's behavior. Any failure warns on stderr
    and returns empty. The CRITICAL ids also remain in the id manifest below,
    so --assert-total semantics are untouched.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            recs = json.load(fh)
        assert isinstance(recs, list)
        recs = [r for r in recs if isinstance(r, dict)]
    except Exception as e:
        sys.stderr.write(
            "[guardrail-manifest] WARN: CRITICAL section unavailable ({}: {}) "
            "— emitting the id manifest without it (yesterday's shape, not a "
            "coverage loss).\n".format(type(e).__name__, e))
        return "", 0
    if not recs:
        return "", 0
    parts = [
        "=== CRITICAL guardrails ({}) — the always-load core: harm outlives "
        "the loop AND the trigger zone is not self-announcing. Read in full; "
        "these are the rules expansion-on-demand cannot save you from: "
        "===".format(len(recs))
    ]
    for rec in sorted(recs, key=lambda r: str(r.get("id") or "")):
        parts.append("{} [{}]:".format(rec.get("id", "?"),
                                       rec.get("category", "?")))
        parts.append(str(rec.get("rule") or "(no rule)"))
        parts.append("")
    return "\n".join(parts) + "\n", len(recs)


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
    ap.add_argument(
        "--critical-json-file",
        default=None,
        help="JSON array of full CRITICAL records; rendered whole ABOVE the id "
             "manifest (additive — failures degrade to no section, never refuse)",
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

    crit_text, crit_n = ("", 0)
    if args.critical_json_file:
        crit_text, crit_n = render_critical(args.critical_json_file)
        text = crit_text + text

    # Budget accounting covers the WHOLE emitted payload, critical section
    # included — prime pays for every byte on stdout, not just the rollup.
    nbytes = len(text.encode("utf-8"))
    est_tokens = int(nbytes / ID_DENSE_BYTES_PER_TOKEN)
    if est_tokens > BUDGET_TOKENS:
        # Bytes AND est-tokens AND the record-count denominator together — a
        # token estimate without its denominator is the guard-2529/guard-2129
        # failure (nothing to sanity-check the conversion against).
        sys.stderr.write(
            "[guardrail-manifest] BUDGET BREACH: {} bytes ~= {} est tokens at the "
            "{} B/token ID-dense floor, over prime's {}-token budget ({} records / "
            "{} categories). Every session start of every agent pays this load. "
            "Retire or consolidate guardrails, or re-derive the budget deliberately "
            "— the first breach reached 3.2x over in silence (g-115-6893).\n".format(
                nbytes, est_tokens, ID_DENSE_BYTES_PER_TOKEN, BUDGET_TOKENS,
                count, ncats
            )
        )
    sys.stdout.write(text)
    if args.stats:
        sys.stderr.write(
            "[guardrail-manifest] {} records / {} categories / {} bytes (~{} est "
            "tokens of a {}-token budget) / {} wrapped-text continuation line(s) "
            "folded / {} CRITICAL rule(s) in full (100% id coverage; expand rule "
            "text with guardrails-read.sh --id <id> or --category <cat>)\n".format(
                count, ncats, nbytes, est_tokens, BUDGET_TOKENS, continuations,
                crit_n
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
