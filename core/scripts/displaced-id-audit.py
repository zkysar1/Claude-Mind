#!/usr/bin/env python3
"""Audit stale references to ids reassigned by the collision-reid merge path.

THE DEFECT THIS MEASURES (g-115-6704). When two boxes independently mint the
same `guard-N`/`rb-N` for DIFFERENT records, `coordination_merge.py::
_merge_id_keyed_jsonl` keeps the earlier-`created` record at that id and moves
the loser to the next free id, stamping it `displaced_from: <the id it lost>`.
The merge is correct -- it preserves both RECORDS. Nothing preserves REFERENCES
to them.

WHY IT NEEDS A DETECTOR AT ALL. A stale reference here does not dangle. It
resolves cleanly to a real, well-formed, entirely unrelated record, so every
ordinary defense passes: the write succeeded, the read-back succeeded, the
reference is well-formed the whole time. The only way to see it is to ask
whether the cited id still MEANS what the citing text says it means.

READ-ONLY. Never rewrites a citation. Repair is a judgement call per site -- a
citation written AFTER a displacement correctly names the new occupant, and
nothing here can date a citation -- so the output is a work list for a reader,
not a patch. Exit 0 unless --strict.

  py -3 core/scripts/displaced-id-audit.py [--json] [--strict] [--all]

--all    also list the low-harm classes (reworded twins, dangling)
--strict exit 1 when any UNRELATED-class stale citation exists
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

# : board channels are enumerated through the reader seam, never by
# hardcoding one filename per channel -- a citation audit that misses a segment
# under-reports, and an under-report here reads exactly like a clean audit.
from _board_paths import channel_paths

TEXT_KEYS = ("rule", "title", "content", "text", "summary", "description",
             "failure_lesson", "name")
# An id claimed by >=SENTINEL_MIN_CLAIMS distinct records is a template default
# leaking into the field, not a real collision (measured: rb-001 claimed by 6
# records; guard-001 whose stored content is a literal /tmp path).
SENTINEL_MIN_CLAIMS = 3
# Token-overlap below this => the old id now means something unrelated. A
# heuristic, not ground truth; hand-verify a borderline row before acting.
UNRELATED_SIM = 0.25
# A never-displaced id that MUST be found, or the citation regex is broken and
# every zero below is meaningless (guard-2298: never trust an unverified zero).
CONTROL_ID = "guard-321"
# The archives of the only stores the collision-reid merge re-keys
# (coordination_merge merge_reasoning_bank / merge_guardrails /
# merge_pattern_signatures). Only these can hold a displaced record or the
# record now at its old id, so only these are refreshed before the scan. The
# other world/meta archives (changelog, board, pipeline, aspirations: 237 MB
# measured on cc-05) carry neither ().
REID_ARCHIVES = ("reasoning-bank-archive.jsonl", "guardrails-archive.jsonl",
                 "pattern-signatures-archive.jsonl")


def _roots():
    from _paths import WORLD_DIR, META_DIR, PROJECT_ROOT, agents_root  # noqa: PLC0415
    return (pathlib.Path(WORLD_DIR), pathlib.Path(META_DIR),
            pathlib.Path(PROJECT_ROOT), agents_root())


def _skip(p: pathlib.Path) -> bool:
    return (not p.is_file() or ".history" in p.parts
            or "__pycache__" in p.parts or "temp" in p.parts)


def _snippet(rec: dict) -> str:
    for k in TEXT_KEYS:
        v = rec.get(k)
        if isinstance(v, str) and v.strip():
            return " ".join(v.split())[:120]
    return ""


def _created(rec: dict):
    """The record's creation stamp, whichever field its store uses.

    id-keyed stores split on the field name: guardrails/reasoning-bank/pattern
    signatures carry `created`; nested goal records in aspirations.jsonl carry
    `created_at` (g-115-8934). The clone determination compares STAMPS across
    stores, so it must read both.
    """
    for k in ("created", "created_at"):
        v = rec.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def _toks(s: str) -> set:
    return set(re.findall(r"[a-z0-9]{4,}", (s or "").lower()))


def _sim(a: str, b: str) -> float:
    A, B = _toks(a), _toks(b)
    return len(A & B) / len(A | B) if (A | B) else 0.0


def _nested_goal_records(rec: dict):
    """Yield the goal dicts nested INSIDE one top-level record.

    g-115-11630 (item 1): in aspirations.jsonl the top-level records are
    ASPIRATIONS and the goals sit nested in `goals[]`, so a goal's
    `displaced_from` (and its id/occupancy snippet) lived below the scan.
    The collision-reid merge re-keys the nested goal, not the aspiration,
    which is why the nesting must be walked, not assumed flat.
    """
    goals = rec.get("goals")
    if isinstance(goals, list):
        for g in goals:
            if isinstance(g, dict):
                yield g


def _iter_records(p: pathlib.Path):
    """Yield every record in a JSONL store: each top-level row PLUS the goal
    dicts nested inside `goals[]` (g-115-11630). Streams line by line."""
    with open(p, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue        # torn line: skipped, never rewritten
            if not isinstance(rec, dict):
                continue
            yield rec
            yield from _nested_goal_records(rec)


def _determine(r, occ_rec):
    """(clone_check, reason) for one event -- the goal's both-conditions rule.

    g-115-8934. An event is a CLONE iff (A) the record now at the OLD id has
    displaced_from absent (it never moved) AND (B) the moved record's created
    stamp equals that record's created (same instant, written twice). Both are
    required -- the goal's counterexample guard-343 -> guard-388 fails A/B
    (differing stamps) and IS a true displacement.

    MEASURED PRECISION CAVEAT (bravo cc-05, 2026-09-21, this goal's
    progress_note): on 31 live UNRELATED events, condition A was a CONSTANT
    (the displacement pointer is written on the SUCCESSOR, never on the id
    left behind), so the rule collapses to created-equality -- and all six
    created-equal events there were GENUINE re-ids (0/6 clones), because a
    re-id preserves the moved record's created stamp. clone_check is therefore
    a CANDIDATE marker for a reader to verify, never a work-queue exemption:
    the split reports it, nothing here suppresses a row on its strength.
    """
    old = occ_rec.get(r["old"])
    new_created = r.get("new_created")
    if old is None:
        return ("undetermined",
                "the old id has no text-bearing record in the scan, so "
                "condition A (old.displaced_from absent) is unreadable")
    old_df = old.get("df")
    old_created = old.get("created")
    if old_df is None and new_created is not None and old_created is not None \
            and new_created == old_created:
        return ("clone_candidate",
                "both conditions: old.displaced_from absent AND created stamps "
                "equal ({}). CANDIDATE ONLY -- measured 0/6 precision on "
                "UNRELATED events (g-115-8934): a re-id preserves created, and "
                "the pointer lives on the successor, so condition A is a "
                "constant. Verify before treating the old id's citations as "
                "correct.".format(new_created))
    if old_df is not None:
        return ("true_displacement",
                "condition A fails: the record at the old id carries "
                "displaced_from={!r} (it was itself a successor)".format(old_df))
    if new_created is None or old_created is None:
        return ("true_displacement",
                "created stamps unreadable (new={!r} old={!r}) -- the "
                "both-conditions rule cannot be met".format(
                    new_created, old_created))
    return ("true_displacement",
            "created stamps differ (new={} old={}) -- the goal's "
            "counterexample shape".format(new_created, old_created))


def collect(world, meta, agents=None):
    """-> (pairs, occupancy, occupant_meta, stats). Streams; never loads a
    store whole.

    `occupant_meta` (g-115-8934) maps an id to the FIRST text-bearing record's
    {"df": displaced_from-or-None, "created": stamp-or-None} -- the same
    slot semantics as `occupancy` (first textful record wins), so the clone
    determination reads the old id's own record, not a sidecar's empty row.

    Scans `world` and `meta` JSONL, PLUS (g-115-11630 item 1) each agent's
    aspirations.jsonl under `agents` — the world queue and every agent queue.
    Nested `goals[]` inside an aspiration record are walked (see
    _nested_goal_records). Only the collision-reid stores carry a displaced
    record or the occupant, so the agent queue (aspirations.jsonl) is the only
    agent file pulled in — not the whole agents/ tree, which is large and
    carries no reid-merged record.
    """
    from _fresh_read import refresh_for_read  # noqa: PLC0415
    pairs, occ, occ_meta = [], {}, {}
    n_rec = n_byte = 0
    roots = [world, meta]
    if agents is not None:
        roots.append(agents)
    for root in roots:
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*.jsonl")):
            if _skip(p):
                continue
            # : under the agents root, only the per-agent queue
            # (aspirations.jsonl) can hold a reid-merged record.
            if root is agents and p.name != "aspirations.jsonl":
                continue
            if p.name in REID_ARCHIVES:
                # The eager pull never re-pulls an archive ().
                refresh_for_read(p, label="displaced-id-audit")
            try:
                n_byte += p.stat().st_size
            except OSError:
                continue
            for rec in _iter_records(p):
                n_rec += 1
                rid = rec.get("id")
                if isinstance(rid, str):
                    snip = _snippet(rec)
                    # Only a record carrying TEXT can describe what an id
                    # now MEANS. A text-less row must not win the slot: a
                    # `*-utilization` sidecar sorts BEFORE its content
                    # store ("-" < "."), so first-wins locked in "" and
                    # every displaced id owning a utilization row scored
                    # _sim()==0.0 -> UNRELATED. Measured 2026-09-19: 70 of
                    # 76 ids and 4633 of 4669 reported stale citations
                    # were phantom (). An id with no textful
                    # record anywhere now reads DANGLING, which is what it
                    # is. The positive control below does NOT cover this:
                    # it validates the citation regex, not occupancy.
                    if snip:
                        occ.setdefault(rid, snip)
                        dfv = rec.get("displaced_from")
                        occ_meta.setdefault(rid, {
                            "df": dfv if isinstance(dfv, str) and dfv else None,
                            "created": _created(rec)})
                df = rec.get("displaced_from")
                if isinstance(df, str) and df and isinstance(rid, str):
                    pairs.append({"old": df, "new": rid,
                                  "store": p.name, "moved": _snippet(rec),
                                  # the moved record's own stamp (condition B
                                  # compares it against the old id's record)
                                  "new_created": _created(rec)})
    for r in pairs:
        r["clone_check"], r["clone_check_reason"] = _determine(r, occ_meta)
    return pairs, occ, occ_meta, {"records": n_rec, "bytes": n_byte}


def surfaces(world, repo, agents=None):
    """Yield (label, path, relpath) for every file a citation could sit in.

    `relpath` is the file's path RELATIVE TO ITS SURFACE ROOT, not the basename
    (g-115-11662): a basename here collapsed 80 same-named SKILL.md files into
    ONE where entry, so the where-list under-reported the citations total. With
    relpath, two same-named files in different directories are two distinct
    entries and the sum of their counts equals the citation total.

    g-115-11630 (item 2) adds two surfaces where stale citations of a displaced
    id actually sit: `world/scripts` (code, like core/scripts) and each agent's
    `experience` .md (ZDS counted 10 and 20 there).
    """
    specs = [("tree", world / "knowledge", ("*.md", "*.yaml")),
             ("world-conventions", world / "conventions", ("*.md",)),
             ("world-scripts", world / "scripts", ("*.py", "*.sh")),
             ("core-config", repo / "core/config", ("*.md", "*.yaml")),
             ("rules", repo / ".claude/rules", ("*.md",)),
             ("skills", repo / ".claude/skills", ("*.md",)),
             ("scripts", repo / "core/scripts", ("*.py", "*.sh"))]
    if agents is not None and agents.is_dir():
        for adir in sorted(agents.iterdir()):
            exp = adir / "experience"
            if exp.is_dir():
                specs.append((f"experience:{adir.name}", exp, ("*.md",)))
    seen = set()
    for label, root, pats in specs:
        if not root.is_dir():
            continue
        for pat in pats:
            for p in root.rglob(pat):
                if _skip(p) or p in seen:
                    continue
                seen.add(p)
                try:
                    rel = p.relative_to(root).as_posix()
                except ValueError:
                    rel = p.name
                yield label, p, rel
    claude_md = repo / "CLAUDE.md"
    if claude_md.is_file():
        yield "CLAUDE.md", claude_md, "CLAUDE.md"
    for name in ("guardrails.jsonl", "reasoning-bank.jsonl",
                 "aspirations.jsonl", "pipeline.jsonl"):
        p = world / name
        if p.is_file():
            yield "records", p, name
    # Board channels: one NAME can be several FILES. `channel_paths` already
    # filters to files that exist, so no `is_file()` re-check is needed.
    # `include_archive=False` preserves today's scanned set exactly -- the
    # archives were never scanned here, and widening the corpus of a citation
    # audit is a different decision from making it segment-correct.
    for channel in ("findings", "coordination", "general"):
        for bp in channel_paths(world / "board", channel,
                                include_archive=False):
            yield "records", bp, bp.name


def cite_file(text, big, by_old, label, rel):
    """Count the control + cite the sites in one file's text ().

    Returns the control-id hits found (main() sums them). A site is cited by
    a per-file-DISTINCT `rel` (its path relative to the surface root), never
    by basename: a basename collapsed 80 same-named SKILL.md files into ONE
    where entry and under-reported the citations total. With a distinct rel,
    two same-named files in different directories are two where entries, and
    the sum of the per-entry counts equals `citations` for the id.
    """
    ctrl = 0
    for tok, n in Counter(big.findall(text)).items():
        if tok == CONTROL_ID:
            ctrl += n
            continue
        for r in by_old[tok]:
            r["citations"] += n
            r["where"].append(f"{label}:{rel}({n})")
    return ctrl


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()

    world, meta, repo, agents = _roots()
    pairs, occ, _occ_meta, stats = collect(world, meta, agents)

    claims = Counter(p["old"] for p in pairs)
    sentinels = {o for o, n in claims.items() if n >= SENTINEL_MIN_CLAIMS}
    real = [p for p in pairs if p["old"] not in sentinels]

    for r in real:
        now = occ.get(r["old"])
        r["now_at_old_id"] = now or ""
        if now is None:
            r["cls"] = "DANGLING"
        elif _sim(r["moved"], now) >= UNRELATED_SIM:
            r["cls"] = "NEAR-TWIN"
        else:
            r["cls"] = "UNRELATED"
        r["citations"], r["where"] = 0, []

    by_old = {}
    for r in real:
        by_old.setdefault(r["old"], []).append(r)
    if not by_old:
        print("displaced-id-audit: no displacement events found.")
        return 0

    big = re.compile(r"\b(" + "|".join(
        re.escape(o) for o in sorted(by_old, key=len, reverse=True))
        + r"|" + re.escape(CONTROL_ID) + r")\b")

    ctrl = nfiles = nbytes = 0
    for label, p, rel in surfaces(world, repo, agents):
        try:
            t = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        nfiles += 1
        nbytes += len(t)
        ctrl += cite_file(t, big, by_old, label, rel)
    for r in real:
        r["where"] = sorted(set(r["where"]))

    bad = sorted((r for r in real if r["cls"] == "UNRELATED" and r["citations"]),
                 key=lambda r: -r["citations"])

    if a.json:
        print(json.dumps({"control_hits": ctrl, "scan": stats,
                          "surface_files": nfiles, "surface_bytes": nbytes,
                          "sentinels": sorted(sentinels),
                          "events": real}, indent=1))
        return 1 if (a.strict and bad) else (2 if not ctrl else 0)

    print("=== displaced-id audit ===")
    print("  scanned {} records / {} bytes".format(stats["records"],
                                                   stats["bytes"]))
    print("  swept   {} files / {} bytes".format(nfiles, nbytes))
    print("  [positive control] {} = {} citations {}".format(
        CONTROL_ID, ctrl,
        "OK" if ctrl else "*** REGEX BROKEN - RESULTS MEANINGLESS ***"))
    if sentinels:
        print("  sentinel ids excluded: " + ", ".join(sorted(sentinels)))
    for cls, n in Counter(r["cls"] for r in real).most_common():
        print("  {:10s} {}".format(cls, n))
    # : split the two work-list classes by the per-event
    # clone/true-displacement determination. ADDITIVE: the raw per-class
    # totals above are unchanged and nothing is suppressed -- a
    # clone_candidate row still reports its citations (0/6 measured
    # precision on UNRELATED; re-ids preserve created, so the marker is a
    # candidate for a reader, not an exemption).
    for cls in ("UNRELATED", "NEAR-TWIN"):
        rows = [r for r in real if r["cls"] == cls]
        if not rows:
            continue
        det = Counter(r.get("clone_check") or "undetermined" for r in rows)
        parts = "  ".join("{} {}".format(n, k)
                          for k, n in det.most_common())
        print("  {:10s} by determination: {}".format(cls, parts))
        cand = det.get("clone_candidate", 0)
        if cand:
            print("     ({} are clone CANDIDATES -- created stamps equal and the "
                  "old id never moved; measured 0/6 on live UNRELATED, so "
                  "verify before treating their citations as correct)"
                  .format(cand))
    print("\n  STALE CITATIONS OF UNRELATED-CLASS IDS: {} across {} ids".format(
        sum(r["citations"] for r in bad), len(bad)))
    for r in bad:
        print("\n  {} -> {}   {} citations".format(
            r["old"], r["new"], r["citations"]))
        print("     cited text expects : {!r}".format(r["moved"][:74]))
        print("     but now resolves to: {!r}".format(r["now_at_old_id"][:74]))
        print("     in: " + ", ".join(r["where"][:6]))
    if a.all:
        for cls in ("NEAR-TWIN", "DANGLING"):
            rows = [r for r in real if r["cls"] == cls and r["citations"]]
            print("\n  --- {} ({} citations) ---".format(
                cls, sum(r["citations"] for r in rows)))
            for r in rows:
                print("    {} -> {}  {}".format(r["old"], r["new"],
                                                r["citations"]))
    if not ctrl:
        return 2
    return 1 if (a.strict and bad) else 0


if __name__ == "__main__":
    sys.exit(main())
