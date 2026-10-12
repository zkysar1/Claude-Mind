#!/usr/bin/env python3
"""Cross-link census + review frame for the reasoning bank and guardrails — REPORT-ONLY. ()

THE DEFECT CLASS. The one real finding of the dropped adjudication lane (g-306-395,
rb-11106) was an entry that is RIGHT but whose nearest neighbours on the MECHANISM
axis are uncited. Back-classified over the pilot's 52 challenges
(telemetry/adjudication-lane-backclass.jsonl) it is present in 37 (26 as the only
finding), so it is common.

WHAT A SWEEP CAN AND CANNOT DO. The goal premise was that the property is
computable from the corpus alone. It is not, and this is measured twice:
  * lexical (IDF-weighted cosine, cross-goal): of 87 neighbour ids the reviewers
    named, 1.1% surfaced at tau 0.30 (flagging 43.0% of the corpus) and 12.6% at
    tau 0.15 (flagging 92.3%). On the nine pairs checked by hand the raw token
    Jaccard with the entry is 0.06-0.14 and the shared tokens are generic words
    ("check", "code", "correct"), so the miss is the instrument's ceiling, not a bug.
  * semantic (the retrieval embedding index, all-MiniLM-L6-v2): the named neighbour
    ranked within the top 10 for 5.7% of entries, median rank 1,967 of ~19.6k.
A mechanism-axis neighbour is, by construction, a DIFFERENT PHRASING of the same
mechanism (rb-10588), so no similarity over the entry's own text reaches it; the
reviewers found theirs by writing a mechanism-phrased query. That step is judgement.
A neighbour finder here would flag a share of the corpus unrelated to the defect, and
a detector that fails its positive control must not ship as a detector (guard-1665).

WHAT THIS DOES INSTEAD, exactly:
  census   citation-graph facts, computable and exact: entries no other entry mentions
           (anywhere / from a different goal), and entries that mention nothing. A
           same-goal mention is not a cross-link: an entry citing only its authorship
           cluster looks well-linked and is unreachable (rb-10588).
  --sample N --seed S
           a seeded random draw, stratified by store, of entries NOT already reviewed
           by the pilot, each with its census flags. It is the frame for the
           mechanism-phrased review that estimates the true rate, and the seed makes
           the draw reproducible.

OWN OUTPUT IS NOT INPUT (guard-2019). A store record that encodes a slate this sweep
produced is tagged `crosslink-sweep-output`; it is excluded from the population AND
from the link graph (its mentions are not links), so enumerating ids in it cannot
make them look cited. `--include-tagged` turns the exclusion off and is the positive
control that shows the exclusion matters. The sweep itself writes nothing.

Entry point is crosslink-sweep.sh (it sources _paths.sh for WORLD_PATH, which a bare
`py -3` cannot resolve — guard-3864). Reads the two LIVE store files only; the
-archive sinks are write-only cold by design (rb-2410).
"""
import argparse
import json
import os
import random
import re
import sys
from datetime import datetime
from pathlib import Path

_SELF = Path(__file__).resolve().parent
if str(_SELF) not in sys.path:
    sys.path.insert(0, str(_SELF))

from store_dupe_warn import _INACTIVE_STATUSES as INACTIVE_STATUSES  # noqa: E402

OUTPUT_TAG = "crosslink-sweep-output"
STORES = (("reasoning_bank", "reasoning-bank.jsonl"), ("guardrails", "guardrails.jsonl"))
LEDGER_REL = Path("telemetry") / "adjudication-lane-ledger.jsonl"
ID_RE = re.compile(r"\b(?:rb|guard)-\d+\b")


class StoreUnreadable(RuntimeError):
    """A store file is missing. NOT the same as an empty store (guard-1715)."""


def _world_path() -> Path:
    for var in ("WORLD_PATH", "MIND_WORLD", "WORLD_DIR"):
        v = os.environ.get(var)
        if v:
            return Path(v)
    return _SELF.parent.parent / "world"


def is_sweep_output(rec: dict) -> bool:
    return OUTPUT_TAG in (rec.get("tags") or [])


def load_corpus(world: Path, include_tagged: bool = False):
    entries, stats = [], {}
    for store, fname in STORES:
        path = world / fname
        if not path.exists():
            raise StoreUnreadable(
                f"{fname} not found at {path}. WORLD_PATH resolved to {world}, almost "
                f"certainly the fallback; invoke core/scripts/crosslink-sweep.sh."
            )
        total = active = excluded = 0
        with open(str(path), "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict) or not rec.get("id"):
                    continue
                total += 1
                if str(rec.get("status") or "active").lower() in INACTIVE_STATUSES:
                    continue
                if is_sweep_output(rec) and not include_tagged:
                    excluded += 1
                    continue
                active += 1
                body = {k: v for k, v in rec.items() if k != "id"}
                entries.append({
                    "id": rec["id"], "store": store,
                    "goal": rec.get("source_goal") or rec.get("source") or rec.get("origin_goal_id") or "",
                    "mentions": frozenset(ID_RE.findall(json.dumps(body, ensure_ascii=False))) - {rec["id"]},
                })
        stats[store] = {"records": total, "active": active, "sweep_output_excluded": excluded,
                        "file_mtime": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")}
    return entries, stats


def citation_flags(entries):
    """id -> {inbound_any, inbound_cross_goal, outbound}: counts over the included population."""
    by_id = {e["id"]: e for e in entries}
    flags = {e["id"]: {"inbound_any": 0, "inbound_cross_goal": 0, "outbound": 0} for e in entries}
    for e in entries:
        for m in e["mentions"]:
            if m not in by_id:
                continue
            flags[e["id"]]["outbound"] += 1
            flags[m]["inbound_any"] += 1
            if not (e["goal"] and e["goal"] == by_id[m]["goal"]):
                flags[m]["inbound_cross_goal"] += 1
    return flags


def census(entries, flags) -> dict:
    n = len(entries)

    def block(subset):
        k = len(subset)
        row = {
            "active": k,
            "no_mention_anywhere": sum(1 for e in subset if not flags[e["id"]]["inbound_any"]),
            "no_cross_goal_mention": sum(1 for e in subset if not flags[e["id"]]["inbound_cross_goal"]),
            "mentions_nothing": sum(1 for e in subset if not flags[e["id"]]["outbound"]),
        }
        for key in ("no_mention_anywhere", "no_cross_goal_mention", "mentions_nothing"):
            row[key + "_share"] = round(row[key] / k, 4) if k else None
        return row

    out = {"all": block(entries)}
    for store, _ in STORES:
        out[store] = block([e for e in entries if e["store"] == store])
    assert out["all"]["active"] == n
    return out


def reviewed_ids(ledger: Path) -> set:
    if not ledger.exists():
        return set()
    out = set()
    with open(str(ledger), "r", encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("kind") == "review" and r.get("entry_id"):
                out.add(r["entry_id"])
    return out


def draw_sample(entries, flags, n: int, seed: int, exclude: set):
    """Seeded, stratified by store: n // 2 per store, the odd one to reasoning_bank."""
    rng = random.Random(seed)
    out = []
    for k, (store, _) in enumerate(STORES):
        want = n // 2 + (n % 2 if k == 0 else 0)
        pool = sorted((e for e in entries if e["store"] == store and e["id"] not in exclude), key=lambda e: e["id"])
        for e in rng.sample(pool, min(want, len(pool))):
            out.append({"id": e["id"], "store": store, "goal": e["goal"], **flags[e["id"]]})
    return out


def run(world: Path, include_tagged: bool = False, sample: int = 0, seed: int = 0, ledger=None) -> dict:
    entries, stats = load_corpus(world, include_tagged)
    flags = citation_flags(entries)
    exclude = reviewed_ids(ledger) if ledger else set()
    return {
        "run_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "params": {"include_tagged": include_tagged, "sample": sample, "seed": seed,
                   "pilot_reviewed_excluded_from_sample": len(exclude)},
        "corpus": {**stats, "active_total": len(entries)},
        "census": census(entries, flags),
        "sample": draw_sample(entries, flags, sample, seed, exclude) if sample else None,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Report-only cross-link census over reasoning bank + guardrails.")
    ap.add_argument("--world", help="world directory (default: WORLD_PATH from the environment)")
    ap.add_argument("--sample", type=int, default=0, metavar="N", help="also draw N entries for mechanism-phrased review")
    ap.add_argument("--seed", type=int, default=0, help="seed for --sample (default 0)")
    ap.add_argument("--include-tagged", action="store_true",
                    help="positive control: count sweep-output records as input")
    args = ap.parse_args(argv)
    world = Path(args.world) if args.world else _world_path()
    try:
        out = run(world, args.include_tagged, args.sample, args.seed, world / LEDGER_REL)
    except StoreUnreadable as e:
        print(f"crosslink-sweep: {e}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
