#!/usr/bin/env python3
"""Tree stale-gap-claim check — surface tree nodes that state a CAPABILITY GAP
in the present tense while NAMING a goal that has since gone TERMINAL.

g-115-3300. DETECTIVE ONLY: it never edits node prose (see "Why report-only").

THE PRODUCER THIS TARGETS (measured, not hypothesised). g-115-3274 swept the 63
tree nodes carrying system-absence phrasings and found exactly 2 CONTRADICTED
nodes. BOTH failed the same way, and no other cause produced a contradiction:
the node stated a gap, responsibly NAMED the follow-up goal that would close it,
that goal completed, and nothing ever went back to the node.

  (1) tree-maintenance-patterns (retrieval_count 128) — "tree-body-presence-audit.py
      is NOT wired to a recurring auto-repair cadence", naming "prevention
      g-115-2889"; g-115-2889/g-115-2890 both completed and g-115-2658 is a live
      72h recurring audit.
  (2) history-store-gc-cadence — "**NOTHING invokes it**" under a header reading
      "Implements-into: g-115-2792-b"; that landed as history-vacuum-tick.sh,
      invoked from iteration-close.sh on a 24h gate.

A third was found by hand later (g-363-106, zeta cc-02, 2026-08-27):
usage-first-billing-policy claimed "Money-out is not mirrored yet ... -> g-363-09"
while g-363-09 had shipped 7 days earlier.

The pair (absence phrasing, goal-id) is machine-checkable with ZERO semantic
judgment, which is why this is cheap where re-verifying each claim against code
is not.

============================================================================
THE RESOLUTION TRAP — read this before touching resolve_goal_status()
============================================================================
A cited goal-id must be resolved against THREE stores, and a detector that uses
fewer fails SILENTLY ON ITS ENTIRE TARGET POPULATION — it reports "goal not
found" for precisely the terminal goals it is hunting, which is
indistinguishable from an id that never existed:

  1. LIVE aspirations            — goals still in the live `goals` list.
  2. ARCHIVE store               — a goal inside a COMPLETED, ARCHIVED aspiration
                                   is absent from every live read (guard-1555;
                                   g-115-3916 measured a 37-day defer freeze from
                                   exactly this).
  3. `archived_census.evicted_ids` — aged terminal goals are EVICTED from the live
                                   list by aspirations-evict-completed.py. The
                                   census records them per STATUS, so eviction
                                   itself yields the status (_goal_census).

Measured here 2026-09-06 with a positive control: `aspirations-query.sh
--goal-field id g-363-09 --full` returns `[]` (evicted+completed — a TRUE
POSITIVE this detector must catch), while the same call for a live goal returns
its record. A live-only lookup is therefore not merely incomplete; it is
anti-correlated with the target set.

============================================================================
MENTION vs CLAIM — why only the BODY is scanned
============================================================================
`world/knowledge/tree/system/scanner-design-patterns/prose-filter-pattern.md`:
"a token appearing in text does not mean the text CLAIMS it". A goal-id in a
node's YAML front matter (`cross_refs:`, `last_update_trigger.source`) is a
CITATION, not a gap claim. Measured here: 1310 of 3053 nodes carry a goal-id
ONLY in front matter — scanning front matter would swamp the signal with
provenance. So front matter is split off and never scanned for claims.

The surviving rule from that node is two-branch, and this is its shape here: the
structured field (front matter) is provenance; the CLAIM lives in the body prose,
so the body is the corpus and proximity within a body LINE is the anchor.

============================================================================
Why report-only (no --apply), and why retraction is NOT filtered
============================================================================
The retraction wording needs judgment about WHICH half of a claim died. In case
(1) above the cadence half was false but the auto-REPAIR half was still true, so
a blind "claim is false" rewrite would have been WRONG.

More fundamentally, assertion-vs-retraction is not lexically decidable (the
prose-FIELD boundary in the same node: both readings are grammatical English
carrying the same cue words). A disclaimer-word blocklist is the same losing move
as an English-noun blocklist. So this script does NOT drop lines that look
retracted — it FLAGS them (`retraction_markers`) and reports them anyway, leaving
the judgment to a reader (guard-4664: a filtered-out row is indistinguishable
from one that never existed).
"""
import argparse
import json
import os
import re
import sys

from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()

import _rt  # noqa: E402
from _paths import WORLD_DIR  # noqa: E402
from _goal_census import census_evicted_ids, TERMINAL_STATUSES  # noqa: E402

TREE_DIR = WORLD_DIR / "knowledge" / "tree"

GOAL_RE = re.compile(r"\bg-\d{1,4}-\d{1,5}\b")
FM_RE = re.compile(r"\A---\s*\n(.*?)\n---", re.DOTALL)

# ---------------------------------------------------------------------------
# ABSENCE PHRASINGS — derived INDEPENDENTLY of 's two pattern sets.
#
# The goal forbids seeding from those sets: reusing them would beg the recall
# question this detector exists to settle. These were derived instead from
# (a) the GRAMMATICAL shapes an English absence-of-mechanism claim can take
# (existential "there is no X"; possessive "has no X"; copular "is not <past
# participle>"; adverbial "never <verb>"; lexical "lacks/unwired"), and
# (b) reading an evenly-spaced sample of live corpus lines that carry both a
# goal-id and a generic negation cue.
#
# Overlap with the prior sets is unavoidable and is not contamination — both
# describe the same English. What matters for the recall measurement is that
# this set was not COPIED, and that its extra members (existential, possessive,
# "yet to be", "lacks", "does not exist") are exactly the shapes the goal
# predicted the prior sets would miss.
# ---------------------------------------------------------------------------
_MECH = (r"(?:invoke|schedule|call|read|write|consume|fire|run|trigger|wire|"
         r"reference|enforce|register|mirror|implement|watch|poll|drain)")

ABSENCE_PATTERNS = [
    ("nothing_verbs", re.compile(r"\bnothing\s+" + _MECH + r"s\b", re.I)),
    ("nobody_verbs", re.compile(r"\bnobody\s+" + _MECH + r"s\b", re.I)),
    ("no_agent_noun", re.compile(
        r"\bno\s+(?:known\s+|other\s+|live\s+|real\s+)?"
        r"(?:caller|callers|call[- ]site|call[- ]sites|consumer|consumers|"
        r"reader|readers|writer|writers|invocation|invocations|scheduler|"
        r"cadence|wiring|hook|hooks|trigger|triggers|owner|remote|"
        r"call\s+site|entry\s?point|entry\s?points)\b", re.I)),
    ("zero_callers", re.compile(
        r"\b(?:zero|0)\s+(?:known\s+)?"
        r"(?:caller|callers|consumer|consumers|reader|readers|"
        r"invocation|invocations|call[- ]sites?|refs|references)\b", re.I)),
    ("is_not_participle", re.compile(
        r"\b(?:is|are|was|were|be|been)\s+(?:still\s+|yet\s+|currently\s+)?not\s+"
        r"(?:yet\s+)?" + _MECH + r"(?:d|ed)\b", re.I)),
    ("never_verb", re.compile(
        r"\bnever\s+(?:been\s+)?" + _MECH + r"(?:s|d|ed)?\b", re.I)),
    ("has_no", re.compile(
        r"\b(?:has|have|had)\s+no\s+[a-z][a-z_-]*", re.I)),
    ("does_not_exist", re.compile(
        r"\b(?:does|do|did)\s+not\s+exist\b", re.I)),
    ("there_is_no", re.compile(
        r"\bthere\s+(?:is|are|was|were)\s+(?:currently\s+|still\s+)?no\b", re.I)),
    ("lacks", re.compile(r"\b(?:lacks?|lacking|we\s+lack)\b", re.I)),
    ("yet_to_be", re.compile(
        r"\b(?:yet\s+to\s+be|has\s+yet\s+to|have\s+yet\s+to)\b", re.I)),
    ("un_prefixed", re.compile(
        r"\bun(?:wired|implemented|called|consumed|scheduled|invoked)\b", re.I)),
    ("not_built", re.compile(
        r"\bnot\s+(?:yet\s+)?(?:built|shipped|landed|deployed|written)\b", re.I)),
]

# 's TWO sets, reproduced VERBATIM from the  goal record.
# Present ONLY as the comparison baseline for --recall-report. They are never
# used for detection; keeping them here makes the miss measurement reproducible.
PRIOR_SET_A = re.compile(
    r"nothing schedules|nothing invokes|nothing reads|no caller|no consumer|"
    r"never invoked|never called|not wired|no scheduled", re.I)
PRIOR_SET_B = re.compile(r"never fires|no reader|nobody reads", re.I)

# Flagged, never filtered — see the module docstring.
RETRACTION_RE = re.compile(
    r"~~|\bRESOLVED\b|\bFIXED\b|\bSHIPPED\b|\bCLOSED\b|\bLANDED\b|"
    r"\bno longer\b|\bCORRECT(?:ED|ION)\b|\bsince\s+(?:closed|fixed|landed)\b", re.I)

# ---------------------------------------------------------------------------
# CITATION DIRECTION — classifies, NEVER filters, and the distinction matters.
#
# WHY IT IS NEEDED (measured 2026-09-06, this corpus):
#   ALL goal-citing tree body lines   17195, of which 76.5% cite a TERMINAL goal
#   ABSENCE-phrasing subset             427, of which 86.4% cite a TERMINAL goal
#   => lift from the absence filter: +9.9 percentage points.
# So "cites a terminal goal" is very close to this corpus's BASE RATE: nearly
# every cited goal is terminal, because completed goals dominate (10,047 of the
# 15,649 indexed ids are evicted-terminal). The terminal half of the predicate
# therefore carries far less information than the goal's premise assumed, and a
# raw `contradicted` count MUST NOT be read as "369 stale nodes".
#
# What separates the 3 KNOWN true positives from the rest is not status, it is
# that the goal-id is a FORWARD pointer — the remedy that would close the stated
# gap ("prevention ", "Implements-into: -b", "not mirrored
# yet ... -> ") — rather than a BACKWARD provenance citation
# ("— source: "), which is the overwhelmingly common shape.
#
# This is deliberately NOT a filter, for the reason the prose-filter-pattern node
# gives: 281 DISTINCT preceding-context strings across 369 hits means no lexical
# rule generalises, so a connector blocklist would silently drop true positives
# (guard-2097's handoff hazard, arriving through a filter change). It is emitted
# as a BUCKET so a reader can triage the ~15 forward-pointing lines first while
# every other hit stays visible and countable (guard-4664).
# ---------------------------------------------------------------------------
FORWARD_RE = re.compile(
    r"(->|-->|→|=>|\bprevention\b|\bimplements[- ]into\b|\btracked by\b|"
    r"\bqueued\b|\bfollow[- ]?up\b|\bcovers\b|\bwill close\b|\bfiled as\b|"
    r"\bpending\b|\bblocked on\b)", re.I)
BACKWARD_RE = re.compile(
    r"(\bsource:|\bper\b|\bmeasured\b|\bclosed by\b|\bfixed by\b|"
    r"\bresolved by\b|\bvia\b|\blanded\b|\bshipped\b|\bfound by\b|"
    r"\banswers\b|\bfrom\b)", re.I)


def citation_direction(text, terminal_ids):
    """forward | backward | both | neither, over a 60-char preceding window.

    Window-based rather than whole-line so a provenance citation later in the
    same sentence cannot mask a forward pointer earlier in it.
    """
    fwd = bwd = False
    for m in GOAL_RE.finditer(text):
        if m.group(0) not in terminal_ids:
            continue
        window = text[max(0, m.start() - 60):m.start()]
        fwd = fwd or bool(FORWARD_RE.search(window))
        bwd = bwd or bool(BACKWARD_RE.search(window))
    if fwd and bwd:
        return "both"
    if fwd:
        return "forward"
    if bwd:
        return "backward"
    return "neither"


def split_front_matter(text):
    """Return (front_matter_str, body_str). Body is the whole file when absent."""
    norm = text.replace("\r\n", "\n")
    m = FM_RE.match(norm)
    if not m:
        return "", norm
    return m.group(1), norm[m.end():]


def _iter_aspirations(errors):
    """Yield every aspiration record from live AND archive, both sources.

    Tolerant by design: an unreadable source reduces coverage and is REPORTED in
    `errors`, never raised — but see the caller, which refuses to emit a clean
    verdict when any source failed (a partial index would silently downgrade
    every terminal goal to `unresolved`).
    """
    for source in ("world", "agent"):
        for archive in (False, True):
            tag = "%s%s" % (source, "-archive" if archive else "")
            try:
                # The two reads take DIFFERENT flags and must be issued
                # separately. Measured 2026-09-06: a bare
                # aspirations_read(source=...) with neither flag is HTTP 400,
                # and passing active+archive together returns the ACTIVE result
                # (archive silently ignored) — so a single combined call would
                # look like it covered both stores while reading only one.
                if archive:
                    raw = _rt.aspirations_read(source=source, archive=True)
                else:
                    raw = _rt.aspirations_read(source=source, active=True)
                data = json.loads(raw)
            except Exception as e:  # noqa: BLE001 — coverage loss, not a crash
                errors[tag] = "%s: %s" % (type(e).__name__, e)
                continue
            # Live reads return {"aspirations": [...]}; archive returns a bare list.
            asps = data.get("aspirations", []) if isinstance(data, dict) else data
            if not isinstance(asps, list):
                errors[tag] = "unexpected shape %s" % type(asps).__name__
                continue
            for asp in asps:
                if isinstance(asp, dict):
                    yield tag, asp


def build_goal_status_index():
    """goal_id -> {"status", "asp_id", "origin"} across all three stores."""
    index, errors = {}, {}
    counts = {"live": 0, "evicted": 0}
    for tag, asp in _iter_aspirations(errors):
        asp_id = asp.get("id")
        for g in asp.get("goals") or []:
            if not isinstance(g, dict):
                continue
            gid = g.get("id")
            if not gid:
                continue
            # A live record always wins: it is the current truth, and an id can
            # legitimately appear in both a live list and a stale census.
            index[gid] = {"status": g.get("status"), "asp_id": asp_id,
                          "origin": tag}
            counts["live"] += 1
        for status, ids in census_evicted_ids(asp).items():
            for gid in ids:
                if gid not in index:
                    index[gid] = {"status": status, "asp_id": asp_id,
                                  "origin": tag + "-evicted"}
                    counts["evicted"] += 1
    return index, errors, counts


def scan_node(path, body):
    """Yield one hit per BODY line carrying an absence phrasing AND a goal-id."""
    for lineno, line in enumerate(body.split("\n"), start=1):
        ids = GOAL_RE.findall(line)
        if not ids:
            continue
        matched = [name for name, rx in ABSENCE_PATTERNS if rx.search(line)]
        if not matched:
            continue
        yield {
            "node": os.path.relpath(str(path), str(TREE_DIR)).replace("\\", "/"),
            "line": lineno,
            "patterns": matched,
            "cited_goal_ids": sorted(set(ids)),
            "text": line.strip()[:400],
            # The UNTRUNCATED line, popped before output. citation_direction
            # must run over the same text `cited_goal_ids` came from: these
            # corpus bullets routinely exceed 400 chars, so computing direction
            # over the truncated `text` silently returns "neither" whenever the
            # pointer sits past the cut. Measured: it did exactly that for the
            # `prevention ` pointer in tree-maintenance-patterns,
            # dropping a KNOWN true positive out of the review set.
            "_full_line": line,
            "retraction_markers": bool(RETRACTION_RE.search(line)),
            "prior_set_a": bool(PRIOR_SET_A.search(line)),
            "prior_set_b": bool(PRIOR_SET_B.search(line)),
        }


def collect_hits():
    hits, read_errors, files = [], {}, 0
    for root, _dirs, names in os.walk(str(TREE_DIR)):
        for name in names:
            if not name.endswith(".md"):
                continue
            path = os.path.join(root, name)
            files += 1
            try:
                text = open(path, encoding="utf-8", errors="replace").read()
            except OSError as e:
                read_errors[path] = str(e)
                continue
            _fm, body = split_front_matter(text)
            hits.extend(scan_node(path, body))
    return hits, files, read_errors


def classify(hits, index):
    """Split hits by the status of the goals they cite."""
    contradicted, open_gap, unresolved = [], [], []
    for h in hits:
        statuses = {}
        for gid in h["cited_goal_ids"]:
            rec = index.get(gid)
            statuses[gid] = rec["status"] if rec else None
        full_line = h.get("_full_line", h["text"])
        h = dict(h, cited_goal_status=statuses)
        h.pop("_full_line", None)  # never emitted; see scan_node
        terminal = [g for g, s in statuses.items() if s in TERMINAL_STATUSES]
        unknown = [g for g, s in statuses.items() if s is None]
        if terminal:
            h["terminal_goal_ids"] = sorted(terminal)
            h["citation_direction"] = citation_direction(full_line, set(terminal))
            contradicted.append(h)
        elif len(unknown) == len(statuses):
            unresolved.append(h)
        else:
            open_gap.append(h)
    return contradicted, open_gap, unresolved


def cmd_audit(args):
    index, index_errors, counts = build_goal_status_index()
    hits, files, read_errors = collect_hits()
    contradicted, open_gap, unresolved = classify(hits, index)

    # RECALL vs 's two sets: hits THIS set surfaces that NEITHER prior
    # set matched. This settles the carried-forward micro-hypothesis (source
    # , confidence 0.40) that those sets had adequate recall.
    missed = [h for h in hits if not h["prior_set_a"] and not h["prior_set_b"]]
    missed_contra = [h for h in contradicted
                     if not h["prior_set_a"] and not h["prior_set_b"]]

    by_direction = {}
    for h in contradicted:
        d = h.get("citation_direction", "neither")
        by_direction[d] = by_direction.get(d, 0) + 1
    # The triage set: the goal-id points FORWARD at the remedy for the stated
    # gap. Every hit stays in `contradicted`; this only orders the reading.
    review = [h for h in contradicted
              if h.get("citation_direction") in ("forward", "both")]

    summary = {
        "mode": "audit",
        "tree_dir": str(TREE_DIR),
        "files_scanned": files,
        "goal_index_size": len(index),
        "goal_index_counts": counts,
        "goal_index_errors": index_errors,
        "read_errors": len(read_errors),
        "hits_total": len(hits),
        "contradicted": len(contradicted),
        "contradicted_nodes": len({h["node"] for h in contradicted}),
        "contradicted_by_citation_direction": by_direction,
        "review_set": len(review),
        "review_set_nodes": len({h["node"] for h in review}),
        "PRECISION_CAVEAT": (
            "`contradicted` is NOT a count of stale nodes. 76.5% of ALL "
            "goal-citing tree body lines cite a terminal goal (measured "
            "2026-09-06, n=17195), so the terminal half of this predicate sits "
            "near the corpus base rate and the absence-phrasing filter adds "
            "only ~+10pp. Triage `review_set` (citation_direction forward|both "
            "— the goal-id is the REMEDY for the stated gap, the shape all 3 "
            "known true positives take) before the rest."),
        "open_gap": len(open_gap),
        "unresolved_citation": len(unresolved),
        "recall_vs_g_115_3274": {
            "hits_neither_prior_set_matched": len(missed),
            "contradicted_neither_prior_set_matched": len(missed_contra),
            "note": ("NONZERO means g-115-3274's 58-CLEAN verdict is scoped to "
                     "its two pattern sets, not to the tree, and that audit "
                     "should be re-run."),
        },
    }
    # An incomplete index downgrades real terminal goals to `unresolved`, so a
    # clean verdict from a partial read would be a false negative (guard-2421).
    if index_errors:
        summary["VERDICT"] = ("PARTIAL — one or more aspiration stores were "
                              "unreadable; `unresolved_citation` is inflated and "
                              "`contradicted` is a LOWER BOUND.")
    else:
        summary["VERDICT"] = "COMPLETE"

    if args.review:
        # The triage view: only the forward-pointer hits, in full. Counts above
        # are unchanged, so nothing is hidden by choosing this view.
        summary["review_set_full"] = review
    elif args.full:
        summary["contradicted_full"] = contradicted
        summary["missed_by_prior_sets_full"] = missed
        summary["review_set_full"] = review
    else:
        summary["sample_review_set"] = review[:10]
        summary["sample_contradicted"] = contradicted[:5]
        summary["sample_missed_by_prior_sets"] = missed[:5]

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if args.exit_on_hits and review:
        return 2
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Surface tree nodes claiming a present-tense capability gap "
                    "while citing a goal that has since gone terminal "
                    "(read-only; g-115-3300).")
    ap.add_argument("--full", action="store_true",
                    help="emit COMPLETE contradicted[] and "
                         "missed_by_prior_sets[] lists, not capped samples.")
    ap.add_argument("--review", action="store_true",
                    help="emit ONLY the forward-pointer triage set in full "
                         "(citation_direction forward|both) — the shape all 3 "
                         "known true positives take. Counts are unchanged.")
    ap.add_argument("--exit-on-hits", action="store_true",
                    help="exit 2 when the REVIEW SET is non-empty, for a "
                         "recurring strategic-scan goal to file ONE deduped "
                         "Investigate. Deliberately keyed to review_set, not "
                         "to `contradicted` — the latter sits near the corpus "
                         "base rate (see PRECISION_CAVEAT) and would fire "
                         "every run forever.")
    args = ap.parse_args()
    sys.exit(cmd_audit(args))


if __name__ == "__main__":
    main()
