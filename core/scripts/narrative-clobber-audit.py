#!/usr/bin/env python3
"""Detect narrative-field CLOBBERS in the goal store (guard-5228).

`aspirations-update-goal.sh <id> progress_note|outcome_note` REPLACES the field.
It does not append and it warns about nothing, so a write meant as an addendum
silently destroys the prior note. Four such losses were measured in a single
session on 2026-08-26 (zeta, cc-02); three of the four destroyed PEER findings,
including a safety correction against a command that would have emptied a live
production place.

THE PREDICATE IS CONTAINMENT, NOT LENGTH. A length delta detects only the
shrinking case: one measured clobber read +11 chars because the replacing note
happened to be the same size as the one it destroyed. This audit asks whether
the pre-write content still appears in the post-write content, which is the
question that actually matters. (guard-5679 calls this "a size heuristic
(post < pre)" — that was never true of this file and is corrected there.)

CONTAINMENT IS NECESSARY, NOT SUFFICIENT. Two SANCTIONED writes also fail it, so
a bare containment verdict over-reports, and that over-reporting is not merely
noisy: `if clob:` is the ONLY branch that prints the recovery footer, so every
false positive walks an agent up to a recipe whose destructive sibling has
already caught three of them (guard-5651, guard-4165). Measured 2026-09-01 on
two boxes independently — cc-07 6 of 6, cc-02 4 of 5 — the majority of flagged
rows were sanctioned. Hence two reclassifications, both narrow, both still
printed:

  superseded — a recurring goal's per-occurrence supersede, where the DESTROYED
    note itself carries a properly-shaped script provenance stamp (`:auto`, or
    `:deferred` — see _provenance_stamp; the second token was found only by
    widening --examine past its default). The stamp on the PRE value is
    load-bearing: the mechanism's own rule is that an unstamped note was
    hand-written and must never be superseded, and its recurring branch has no
    never-clobber test to enforce that (guard-5049, g-115-7733). Keying on
    `recurring` alone would have hidden that live defect, which is the one TRUE
    positive in this class — an exemption must never be broader than the thing
    it exempts (guard-3086).
  restored — the write re-inserts an OLDER snapshot's value, i.e. it is a repair
    putting destroyed history back. Containment cannot see this because recovered
    text lands in the MIDDLE and the immediately-prior string stops being
    contiguous, so the audit flags the very remedy it exists to prompt.

Neither reclassification drops a row; both are reported beside CLOBBERED. An
exemption you cannot audit is how an exempter's silent false miss survives
review (guard-4015).

MECHANISM. `.history` snapshots are PRE-write: the snapshot whose manifest
summary names an operation holds the state BEFORE that operation. So for a
snapshot at index i (newest-first), the post-state is snapshot i-1. New-store
snapshots carry their summary in the MANIFEST, not in a sibling `.meta` file —
reading `.meta` returns empty for every one of them and yields a confident
zero hits (measured; the first version of this audit did exactly that).

SCOPE — THIS AUDIT IS BOX-LOCAL AND CANNOT SEE PEER AGENTS. `.history/` is in
`owncloud_sync._EXCLUDE_NAMES` and is pruned from the sync walk, so it is never
pushed: it records only writes made ON THIS MACHINE. A fleet whose other agents
run on other boxes therefore reports as a single-agent population here. Measured
2026-08-26 (zeta, cc-02): 4000 manifests spanning 2026-08-12..08-26 yielded 691
narrative writes and `by_agent: {zeta: 691}` — that is the instrument's blind
spot, NOT evidence that peers do not clobber. A clean run means "no clobber by
the resident agent on this box"; it can never mean "the fleet is clean"
(guard-1715: a scan with an empty population reports clean identically to one
that examined everything). To cover the fleet, run this on each box.

Read-only. Never writes, never mutates the store.
"""
import argparse, json, re, sys, pathlib

_HERE = pathlib.Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import history as H
import _history_store
from _history_store import _read_manifest

FIELD_RE = re.compile(r"update-goal\s+(g-\d+-\d+)\s+(progress_note|outcome_note)")

# ANCHORED, LITERAL MARKER MATCH — mirrors closure-evidence-write.sh::_ce_marker_ach
# (its `index($0, tok) == 1 && index($0, " by closure-evidence-write.sh ...") > 0`).
# Keep the two in lockstep; tests/test_narrative_clobber_audit.py pins them together.
#
# WHY ANCHORED AND NOT A SUBSTRING (guard-4015). This is an EXEMPTER, not a
# detector, so a false match is a SILENT false MISS that disables the protection
# — the inverse of the noisy self-reference family. A bare `in` test would exempt
# any note that merely MENTIONS the token, and diagnostic notes about this very
# mechanism do exactly that (measured: the closure narrative for 
# quotes the supersede marker verbatim while explaining it). closure-evidence-
# write.sh already shipped this same fix for the same reason (); this
# is the second consumer of that shape, not a new idea.
CE_AUTO_MARK = "[closure-evidence:auto]"
CE_DEFER_MARK = "[closure-evidence:deferred]"
CE_AUTO_SHAPE = " by closure-evidence-write.sh (achievedCount="
CE_SUPERSEDE_MARK = "[closure-evidence] SUPERSEDES a prior-occurrence note"


def _provenance_stamp(text):
    """Which script-written closure-evidence stamp `text` carries, or None.

    Returns "auto", "deferred", or None. The token must START a line AND that
    line must carry the full written shape. A note that pastes a token at the
    start of a line without the shape, or quotes it mid-prose, does NOT match —
    both are real observed cases.

    TWO tokens, not one, and the second was found only by widening the examine
    window past its default (measured: g-115-105, invisible at --examine 20 and
    the sole CLOBBERED row at 68). `deferred` is written when an occurrence
    DECLINES to supersede a note it cannot prove is machine-written, and its own
    text states the contract: "The NEXT occurrence MAY supersede it." So the
    write that consumes that grace is sanctioned by the mechanism itself.
    """
    for line in (text or "").splitlines():
        if CE_AUTO_SHAPE not in line:
            continue
        if line.startswith(CE_AUTO_MARK):
            return "auto"
        if line.startswith(CE_DEFER_MARK):
            return "deferred"
    return None


def _has_auto_marker(text):
    """True when text carries EITHER script-written provenance stamp."""
    return _provenance_stamp(text) is not None


def _summary(snap):
    try:
        return (_read_manifest(snap).get("summary") or "").strip()
    except Exception:
        return ""


def _goal(content, gid):
    for line in content.splitlines():
        if gid not in line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        for g in rec.get("goals", []):
            if g.get("id") == gid:
                return g
    return None


def _note(content, gid, field):
    g = _goal(content, gid)
    return None if g is None else (g.get(field) or "")


def _classify(pre, post, goal, restore_lookup=None):
    """Verdict for one narrative write, as (verdict, why).

    `goal` is the POST-write goal record (may be None). `restore_lookup` is a
    zero-arg callable returning the older snapshot name whose value `post`
    re-inserts, or None — a callable rather than a value so the expensive
    lookback runs ONLY for rows that would otherwise be reported as loss.
    """
    if not pre:
        return "new", ""
    if pre.strip() in post:
        return "preserved", ""
    # Containment failed. Necessary, not sufficient: two SANCTIONED writes also
    # fail it. Reclassify those two narrowly; everything else stays CLOBBERED.
    stamp = _provenance_stamp(pre)
    if (goal or {}).get("recurring") and stamp:
        # closure-evidence-write.sh deliberately replaces occurrence N-1's note
        # with occurrence N's, says so in the note it writes, and leaves the
        # prior text in .history. The auto marker must be on the DESTROYED note:
        # the mechanism's own rule is that an unstamped note was hand-written and
        # must never be superseded, and its recurring branch has no never-clobber
        # test to enforce that (guard-5049, ). Keying on `recurring`
        # alone would hide that live defect — the one TRUE positive in this class.
        if stamp == "deferred":
            # The prior occurrence DECLINED to supersede because it could not
            # prove the note was machine-written, and said so: "The NEXT
            # occurrence MAY supersede it." This write consumes that grace. It
            # is sanctioned, but it is also the ONE place the framework
            # knowingly destroys possibly-hand-written text, so say so in the
            # row rather than letting it read like any other supersede.
            return ("superseded",
                    "deferred grace consumed — prior note may be hand-written (g-115-7733)")
        return "superseded", "recurring auto-note -> auto-note"
    if restore_lookup:
        src = restore_lookup()
        if src:
            # A repair re-inserts recovered history into the MIDDLE of the field,
            # so the immediately-prior text stops being contiguous and containment
            # reads the remedy as the disease.
            return "restored", "re-inserts %s" % src
    return "CLOBBERED", ""


# How many OLDER writes to the same goal+field a restore check will materialize.
# Bounded because each load reconstructs the whole store from the blob chain.
# The bound is REPORTED when it bites (never a silent cap): a truncated lookback
# can only UNDER-detect restores, which fails toward CLOBBERED — the loud side.
RESTORE_LOOKBACK = 8


def _restored_from(post, gid, field, i, hits, load, stats=None):
    """Name the older snapshot whose value `post` re-inserts, or None.

    `hits` is newest-first, so an index j > i is OLDER. A repair write puts
    destroyed history back into the field; the recovered text is the value from
    a PRIOR write, so it appears verbatim inside `post` even though the
    immediately-prior text does not.
    """
    checked = 0
    for j, p2, gid2, f2 in hits:
        if j <= i or gid2 != gid or f2 != field:
            continue
        if checked >= RESTORE_LOOKBACK:
            if stats is not None:
                stats["restore_lookback_truncated"] += 1
            return None
        checked += 1
        old = _note(load(p2), gid, field)
        if old and old.strip() and old.strip() in post:
            return p2.name[:19]
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scan", type=int, default=400,
                    help="manifests to scan for narrative writes (cheap; default 400)")
    ap.add_argument("--examine", type=int, default=20,
                    help="most-recent narrative writes to materialize (expensive; default 20)")
    ap.add_argument("--file", default="world/aspirations.jsonl")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    target = H.resolve_target(a.file)
    base = H.resolve_base_dir(target)
    snaps = H._find_history_snapshots(target)[:a.scan]

    non_empty = 0
    hits = []
    for i, p in enumerate(snaps):
        s = _summary(p)
        if s:
            non_empty += 1
        if i > 0:
            m = FIELD_RE.search(s)
            if m:
                hits.append((i, p, m.group(1), m.group(2)))

    # POSITIVE CONTROL. Zero non-empty summaries across a non-empty snapshot list
    # means the manifest read is broken, not that nothing was written — the exact
    # failure that made this audit's first version report 0 hits (guard-2298).
    if snaps and non_empty == 0:
        print("[narrative-clobber-audit] MANIFEST READ IS BROKEN: %d snapshots, 0 with a "
              "summary. This is NOT a clean result — hits below are meaningless."
              % len(snaps), file=sys.stderr)
        return 2

    cache = {}

    def load(p):
        if p.name not in cache:
            c = _history_store.restore(target, p.name, base)
            cache[p.name] = c.decode("utf-8") if isinstance(c, bytes) else c
        return cache[p.name]

    rows = []
    stats = {"restore_lookback_truncated": 0}
    for i, p, gid, field in hits[:a.examine]:
        _ts, agent = H.parse_snapshot_name(p.name)
        pre = _note(load(p), gid, field)
        post = _note(load(snaps[i - 1]), gid, field)
        if pre is None or post is None:
            continue
        verdict, note = _classify(
            pre, post, _goal(load(snaps[i - 1]), gid),
            lambda: _restored_from(post, gid, field, i, hits, load, stats))
        rows.append({"snapshot": p.name[:19], "agent": agent, "goal": gid,
                     "field": field, "pre_chars": len(pre), "post_chars": len(post),
                     "verdict": verdict, "note": note})

    clob = [r for r in rows if r["verdict"] == "CLOBBERED"]
    sup = [r for r in rows if r["verdict"] == "superseded"]
    res = [r for r in rows if r["verdict"] == "restored"]
    by_agent = {}
    for r in clob:
        by_agent[r["agent"]] = by_agent.get(r["agent"], 0) + 1

    if a.json:
        print(json.dumps({"scanned": len(snaps), "narrative_writes_found": len(hits),
                          "examined": len(rows), "clobbered": len(clob),
                          "superseded": len(sup), "restored": len(res),
                          "restore_lookback_truncated": stats["restore_lookback_truncated"],
                          "by_agent": by_agent, "rows": rows}, indent=1))
    else:
        print("scanned=%d manifests | narrative writes found=%d | examined=%d"
              % (len(snaps), len(hits), len(rows)))
        print("%-20s %-9s %-13s %-13s %6s %6s  %-10s %s"
              % ("snapshot", "agent", "goal", "field", "pre", "post", "verdict", "why"))
        for r in rows:
            mark = "  <==" if r["verdict"] == "CLOBBERED" else ""
            print("%-20s %-9s %-13s %-13s %6d %6d  %-10s %s%s"
                  % (r["snapshot"], r["agent"], r["goal"], r["field"],
                     r["pre_chars"], r["post_chars"], r["verdict"],
                     r.get("note", ""), mark))
        print("\nCLOBBERED=%d  by agent: %s" % (len(clob), by_agent or "none"))
        # The reclassified rows are PRINTED, not dropped. They failed containment
        # and were then shown to be sanctioned; hiding them would make the
        # exemption unauditable, which is how an exempter's silent false miss
        # (guard-4015) survives review.
        print("reclassified (NOT data loss, and NOT hidden — rows above): "
              "superseded=%d restored=%d" % (len(sup), len(res)))
        if stats["restore_lookback_truncated"]:
            print("NOTE: restore lookback hit its %d-write bound on %d row(s) — those "
                  "may be restores reported as CLOBBERED. Truncation can only "
                  "UNDER-detect restores, so it fails toward the loud side."
                  % (RESTORE_LOOKBACK, stats["restore_lookback_truncated"]))
        print("SCOPE: box-local. .history is never synced, so only writes made ON THIS "
              "MACHINE are visible — a single-agent result is expected and is NOT a "
              "fleet all-clear. Run on each box to cover the fleet.")
        if clob:
            print("Recover per guard-5228: the snapshot named in each row IS the pre-write "
                  "state; _history_store.restore() RETURNS content instead of writing it.")
            # The sentence above is TRUE of the module function and FALSE of the
            # identically-named CLI subcommand, and this footer is read at the exact
            # moment an agent is already recovering from data loss. Three agents have
            # reached for the destructive sibling from here (guard-5651 names this
            # footer as "the one to fix"; guard-4165 records the first firing). Naming
            # the safe call without naming its dangerous twins is what made it a trap.
            print("*** NOT THE CLI. `history.py restore` and `history-restore.sh` are "
                  "IDENTICALLY NAMED and DESTRUCTIVE — they OVERWRITE the live file and "
                  "print one line, so a bare call looks like it returned nothing while it "
                  "has already reverted the store. Three agents have been caught here "
                  "(guard-5651, guard-4165).")
            print("READ-ONLY inspection: py -3 core/scripts/history.py diff <file> <version>. "
                  "APPEND-SAFE narrative writes: core/scripts/goal-field-append.sh — it avoids "
                  "the read-and-concatenate clobber class entirely (g-115-8404).")
    return 1 if clob else 0


if __name__ == "__main__":
    sys.exit(main())
