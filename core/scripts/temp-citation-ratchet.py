#!/usr/bin/env python3
"""temp-citation-ratchet.py — advisory drift ratchet for `agents/*/temp/`
citations in the DURABLE knowledge stores (g-115-3946).

WHAT THIS COVERS, AND WHY IT EXISTS
-----------------------------------
The experience lane already has an anti-orphan guard: the /verify-learning check
`experience-content-path-no-temp` greps `agents/*/experience.jsonl` and
`agents/*/experience-archive.jsonl` for a `content_path` under `temp/`. The
other three durable stores had NO equivalent — never considered, not declined
(g-115-3772 established that distinction; the record of the goal that shipped
the experience check contains zero occurrences of tree/declined/scope).

`agents/<agent>/temp/` is PURGEABLE BY DESIGN (temp-drain-purge.sh: Lane 1
`-mmin +120` on root temp/, Lane 2 `-mtime +30` on drained/). So a citation
into it from a durable store is a latent orphan the moment it is written — the
cited evidence evaporates on the next drain and the citing text is left
pointing at nothing.

THE METRIC IS CITATIONS, NOT DANGLING CITATIONS — this is the load-bearing
design choice and it is not what the filing goal assumed.
Dangling-ness is BOX-DEPENDENT. Measured 2026-07-31 on the Studio host
(hostname LAPTOP-3IOFCNEO, Linux 6.6.87.2-microsoft-standard-WSL2): of 45
temp paths cited by tree nodes and owned by OTHER agents, **0 exist on this
box**, while 3 of this agent's own 6 do. `agents/zeta/temp/x` lives on zeta's
box; every other box reads it as dangling. A dangling-count baseline would
therefore report a different number on every machine and flap on each agent's
run — the two sides of the delta would not share a predicate (guard-1951), and
a ratchet that cannot compare its own measurements is decoration.

Counting CITATIONS is deterministic on every box, needs no filesystem probe,
and is the correct unit for a WRITE-SIDE guard: the question is "did a new
citation into a purgeable directory land", not "has the purge run yet".

The unit is the (record, path) PAIR, not the distinct path. A brand-new record
citing an already-cited path is still a new latent orphan, and a distinct-path
count would silently absorb it.

SCOPE: WRITE-SIDE GUARD ONLY. The filing goal (g-115-3946) is explicit that the
cleanup of the pre-existing citations is separate, contested work — four
independent guardrails (guard-952, guard-731, guard-712, guard-667) all say a
missing-file signal alone does not license deleting the citing text. So this
ships as a RATCHET seeded at the current count: existing citations are
grandfathered, and only GROWTH is reported.

Exit codes:
  0  always (advisory), unless VERIFY_LEARNING_DRIFT_HARD_GATE=1 and regressed
  2  script error (unreadable store, unwriteable baseline file)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
from _paths import META_DIR, WORLD_DIR  # type: ignore
from _fileops import locked_modify_yaml  # type: ignore

BASELINES_PATH = META_DIR / "audit-baselines.yaml"
KEY = "temp_citations_durable_stores"

# Trailing punctuation is excluded from the match so a citation ending a
# sentence ("see agents/x/temp/y.md.") does not fold the period into the path.
TEMP_CITE_RE = re.compile(r"agents/[A-Za-z0-9_-]+/temp/[^\s\"',;)\]`>]*[^\s\"',;)\]`>.]")

# The three uncovered durable stores, plus pattern-signatures. The filing goal
# measured pattern-signatures at 0 and asked that it be stated rather than
# re-measured by a future audit — so it is scanned and reported, not assumed.
JSONL_STORES = ("reasoning-bank.jsonl", "guardrails.jsonl", "pattern-signatures.jsonl")


def _scan_jsonl(path: Path):
    """Citations in a JSONL store, as (record_id, temp_path) pairs.

    Matches against the RAW line rather than parsed fields: a citation can sit
    in `content`, `rule`, `failure_lesson`, `when_to_use`, or any future field,
    and enumerating field names would make the check narrower than the store it
    audits (the guard-1802 class this whole goal is an instance of).
    """
    pairs = set()
    if not path.is_file():
        return pairs, 0
    records = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        records += 1
        hits = TEMP_CITE_RE.findall(line)
        if not hits:
            continue
        rec_id = ""
        try:
            rec_id = str(json.loads(line).get("id") or "")
        except Exception:
            # A malformed row still cites; fall back to a positional key so the
            # pair stays distinct rather than collapsing every bad row into one.
            rec_id = f"<unparsed-line-{records}>"
        for h in hits:
            pairs.add((rec_id, h))
    return pairs, records


def _scan_tree(tree_dir: Path):
    """Citations in knowledge-tree node bodies, as (node_relpath, temp_path)."""
    pairs = set()
    if not tree_dir.is_dir():
        return pairs, 0
    files = 0
    for p in sorted(tree_dir.rglob("*.md")):
        files += 1
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for h in TEMP_CITE_RE.findall(text):
            pairs.add((p.relative_to(tree_dir).as_posix(), h))
    return pairs, files


def _compute():
    breakdown, total, scanned = {}, 0, 0
    missing = []
    tree_dir = WORLD_DIR / "knowledge" / "tree"
    tree_pairs, tree_files = _scan_tree(tree_dir)
    breakdown["tree"] = len(tree_pairs)
    total += len(tree_pairs)
    scanned += tree_files
    if not tree_dir.is_dir():
        missing.append("knowledge/tree")
    for fn in JSONL_STORES:
        p = WORLD_DIR / fn
        pairs, records = _scan_jsonl(p)
        breakdown[fn.replace(".jsonl", "")] = len(pairs)
        total += len(pairs)
        scanned += records
        if not p.is_file():
            missing.append(fn)
    return {"total": total, "breakdown": breakdown,
            "units_scanned": scanned, "stores_missing": missing}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true", help="Emit JSON")
    ap.add_argument("--dry-run", action="store_true",
                    help="Compute and report without touching the baseline file")
    args = ap.parse_args()

    try:
        current = _compute()
    except Exception as e:
        print(f"ERROR: temp-citation audit failed: {e}", file=sys.stderr)
        return 2

    if current["units_scanned"] == 0:
        # No tree node and no store row was readable. Reporting 0 citations here
        # would be a vacuous PASS (rb-245: verify the population exists before
        # believing a zero) — and on a satellite box with an unmounted world
        # that is exactly what would happen.
        msg = ("no tree node or durable-store row was readable — nothing measured "
               "(check WORLD_DIR resolution before reading this as clean)")
        print(json.dumps({"verdict": "skipped", "message": msg}, indent=2)
              if args.json else f"[temp-citation-ratchet] SKIPPED: {msg}")
        return 0

    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    captured: dict = {}

    def _modify(baselines):
        # Locked RMW: sibling ratchets share this file and this lock
        # (core/config/conventions/audit-baselines.md). Reading the prior value
        # outside the lock lets two writers each ratchet against a stale
        # baseline, and the second silently reverts the first.
        if not isinstance(baselines, dict):
            baselines = {}
        entry = baselines.get(KEY) or {}
        prior = entry.get("baseline")
        cur = current["total"]

        if prior is None:
            verdict, new_baseline = "seeded", cur
            message = (f"Seeded baseline at {cur} temp/ citation(s) across the durable "
                       f"stores. Future runs compare against it; only GROWTH is reported.")
        elif cur > prior:
            verdict, new_baseline = "regressed", prior  # never raise the baseline
            message = (f"WARN: temp/ citations grew from baseline {prior} to {cur} "
                       f"(+{cur - prior}). A durable store now cites a PURGEABLE path — "
                       "temp-drain-purge.sh will orphan it (Lane 1 -mmin +120 on root "
                       "temp/, Lane 2 -mtime +30 on drained/). Fold the cited evidence "
                       "INLINE into the record, or move it to a durable path, before the "
                       "next drain. Do NOT delete the citing text (guard-952/731/712/667).")
        elif cur < prior and current["stores_missing"]:
            # A store that vanished looks EXACTLY like a store that got cleaned:
            # both drop the count. Ratcheting here would bake a transient read
            # failure into the baseline permanently (one unreadable
            # reasoning-bank.jsonl = -17), and the store's RETURN would then read
            # as a phantom +17 regression sending someone after a bug that never
            # existed. This is the partial-population twin of the units_scanned==0
            # vacuous-zero guard in main(): that one catches losing EVERY store,
            # this one catches losing SOME — which is the likelier failure and was
            # the hole left when only the total case was guarded (guard-1802 class:
            # the predicate was narrower than the population it claimed to cover).
            # A REGRESSION is still reported normally even with a store missing —
            # more citations from fewer stores is genuinely worse, not ambiguous.
            verdict, new_baseline = "skipped", prior
            message = (f"count fell from {prior} to {cur}, but "
                       f"{', '.join(current['stores_missing'])} was unreadable — "
                       "that is indistinguishable from a real cleanup, so the "
                       "baseline is HELD, not lowered. Check WORLD_DIR resolution; "
                       "re-run once the store is readable to ratchet for real.")
        elif cur < prior:
            verdict, new_baseline = "ratcheted", cur
            message = (f"OK: temp/ citations shrank from baseline {prior} to {cur} "
                       f"(-{prior - cur}). Baseline lowered.")
        else:
            verdict, new_baseline = "stable", prior
            message = f"OK: temp/ citations stable at baseline {cur}."

        history = entry.get("history") or []
        history.append({
            "recorded_at": now_iso,
            "drift_total": cur,
            "verdict": verdict,
            "breakdown": dict(current["breakdown"]),
        })
        baselines[KEY] = {
            "baseline": new_baseline,
            "last_recorded": now_iso,
            "last_verdict": verdict,
            # Named so a future reader does not assume this counts DANGLING
            # citations — it deliberately does not (see module docstring).
            "unit": "record_path_pairs_cited_not_dangling",
            "history": history[-50:],
        }
        captured.update(verdict=verdict, new_baseline=new_baseline, message=message)
        return baselines

    if args.dry_run:
        entry = {}
        try:
            import yaml  # type: ignore
            if BASELINES_PATH.is_file():
                entry = (yaml.safe_load(BASELINES_PATH.read_text(encoding="utf-8")) or {}).get(KEY) or {}
        except Exception:
            entry = {}
        prior = entry.get("baseline")
        captured.update(
            verdict="dry-run",
            new_baseline=prior,
            message=f"current={current['total']} prior_baseline={prior} (no write)",
        )
    else:
        try:
            locked_modify_yaml(BASELINES_PATH, _modify, initial={})
        except Exception as e:
            # OVERWRITE, never setdefault. _modify runs INSIDE locked_modify_yaml
            # and populates `captured` before the write; if the write then fails
            # (disk full, conflict-retry exhausted, validation), setdefault is a
            # no-op and this would report the COMPUTED verdict — "seeded",
            # "ratcheted" — as though it had persisted. stderr is the only
            # contradicting signal and no JSON consumer reads it, so
            # /verify-learning would record a successful seed against a file that
            # was never written. A tool must not claim a write it did not make.
            print(f"WARN: could not persist baseline to {BASELINES_PATH}: {e}", file=sys.stderr)
            computed = captured.get("verdict")
            captured["verdict"] = "error"
            captured["new_baseline"] = None
            captured["message"] = (
                f"baseline operation FAILED and nothing was persisted: {e}"
                + (f" (the computed verdict was '{computed}' — it did NOT take effect)"
                   if computed else ""))

    result = {
        "verdict": captured["verdict"],
        "baseline": captured["new_baseline"],
        "current": current,
        "unit": "record_path_pairs_cited_not_dangling",
        "message": captured["message"],
    }

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"[temp-citation-ratchet] {captured['verdict'].upper()}: {captured['message']}")
        print(f"  per-store: {current['breakdown']}")

    if os.environ.get("VERIFY_LEARNING_DRIFT_HARD_GATE") == "1":
        return 1 if captured["verdict"] == "regressed" else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
