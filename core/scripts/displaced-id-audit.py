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


def _roots():
    from _paths import WORLD_DIR, META_DIR, PROJECT_ROOT  # noqa: PLC0415
    return (pathlib.Path(WORLD_DIR), pathlib.Path(META_DIR),
            pathlib.Path(PROJECT_ROOT))


def _skip(p: pathlib.Path) -> bool:
    return (not p.is_file() or ".history" in p.parts
            or "__pycache__" in p.parts or "temp" in p.parts)


def _snippet(rec: dict) -> str:
    for k in TEXT_KEYS:
        v = rec.get(k)
        if isinstance(v, str) and v.strip():
            return " ".join(v.split())[:120]
    return ""


def _toks(s: str) -> set:
    return set(re.findall(r"[a-z0-9]{4,}", (s or "").lower()))


def _sim(a: str, b: str) -> float:
    A, B = _toks(a), _toks(b)
    return len(A & B) / len(A | B) if (A | B) else 0.0


def collect(world, meta):
    """-> (pairs, occupancy, stats). Streams; never loads a store whole."""
    pairs, occ = [], {}
    n_rec = n_byte = 0
    for root in (world, meta):
        for p in sorted(root.rglob("*.jsonl")):
            if _skip(p):
                continue
            try:
                n_byte += p.stat().st_size
            except OSError:
                continue
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
                    n_rec += 1
                    rid = rec.get("id")
                    if isinstance(rid, str):
                        occ.setdefault(rid, _snippet(rec))
                    df = rec.get("displaced_from")
                    if isinstance(df, str) and df and isinstance(rid, str):
                        pairs.append({"old": df, "new": rid,
                                      "store": p.name, "moved": _snippet(rec)})
    return pairs, occ, {"records": n_rec, "bytes": n_byte}


def surfaces(world, repo):
    specs = [("tree", world / "knowledge", ("*.md", "*.yaml")),
             ("world-conventions", world / "conventions", ("*.md",)),
             ("core-config", repo / "core/config", ("*.md", "*.yaml")),
             ("rules", repo / ".claude/rules", ("*.md",)),
             ("skills", repo / ".claude/skills", ("*.md",)),
             ("scripts", repo / "core/scripts", ("*.py", "*.sh"))]
    seen = set()
    for label, root, pats in specs:
        for pat in pats:
            for p in root.rglob(pat):
                if not _skip(p) and p not in seen:
                    seen.add(p)
                    yield label, p
    claude_md = repo / "CLAUDE.md"
    if claude_md.is_file():
        yield "CLAUDE.md", claude_md
    for name in ("guardrails.jsonl", "reasoning-bank.jsonl",
                 "aspirations.jsonl", "pipeline.jsonl",
                 "board/findings.jsonl", "board/coordination.jsonl",
                 "board/general.jsonl"):
        p = world / name
        if p.is_file():
            yield "records", p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()

    world, meta, repo = _roots()
    pairs, occ, stats = collect(world, meta)

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
    for label, p in surfaces(world, repo):
        try:
            t = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        nfiles += 1
        nbytes += len(t)
        for tok, n in Counter(big.findall(t)).items():
            if tok == CONTROL_ID:
                ctrl += n
                continue
            for r in by_old[tok]:
                r["citations"] += n
                r["where"].append(f"{label}:{p.name}({n})")
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
