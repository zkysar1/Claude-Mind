#!/usr/bin/env python3
"""cells-archive.py -- Go-Explore cell archive substrate for Mind goal-selection.

BRD Gap 17, child 1/4 of g-306-16. Evidence: Go-Explore (1901.10995) -- maintain
an archive of "cells" (discretized interesting states), remember the BEST trajectory
found to reach each cell, and deterministically return to a promising cell to explore
from it. Here the archive is a goal-selection INPUT: aspirations-select can deterministically
return to a high-value cell instead of always greedily re-scoring from scratch.

MIND-INTERNAL -- READ THIS BEFORE EXTENDING.
  This is a substrate over the Mind framework's OWN goal-selection state. A "cell" is a
  discretized interesting state the agent has visited (keyed by a caller-supplied
  state_signature); a "trajectory" is the path that reached it; a "score" is the caller's
  measure of how promising the cell is (higher = better). There is NO product-domain analogue --
  the product behavior pipeline does not consume this. Cell similarity (child 2/4, g-306-48)
  reuses the Mind knowledge-graph + PPR substrate (g-306-42..45), NOT product-domain embeddings.

STORE MODEL.
  One YAML file per category at  agents/<agent>/cells/<safe-category>.yaml.
  Each file is a dict  {cell_id: cell_record}. A cell_record is:
    cell_id          str   -- caller-chosen stable id for the interesting state
    category         str   -- the category bucket (caps are PER-category)
    state_signature  any   -- discretized state descriptor (the "cell" key in Go-Explore)
    trajectory       list  -- best-known path to reach this cell (caller-defined steps)
    score            float -- promise of this cell; higher is better
    visits           int   -- times upsert touched this cell (return-frequency signal)
    created_at       str   -- ISO local timestamp (first insert)
    updated_at       str   -- ISO local timestamp (last touch)

WRITE SEMANTICS (goal contract).
  * Replace-on-strictly-better: an upsert to an EXISTING cell replaces its trajectory/
    state_signature/score ONLY when the new score is strictly greater than the stored
    score (Go-Explore "found a better way to reach this cell"). A worse-or-equal score
    keeps the stored trajectory; visits still increments (return-frequency is real even
    when the path did not improve).
  * K^D per-category cap: each category holds at most CELL_CAP = K**D = 4**4 = 256 cells
    (honors Gap 2 / g-306-13 "K=D=4 cognitively-grounded tree decomposition contract",
    memory-pipeline.yaml tree_structure "K=4, D=4"). When the cap is full, a NEW cell is
    admitted only if its score strictly exceeds the weakest incumbent's, evicting that
    incumbent; otherwise the new cell is rejected (the archive keeps the strongest 256).

CONCURRENCY.
  All mutation routes through _fileops.locked_modify_yaml -- an atomic read-modify-write
  holding the file lock across the whole cycle, so concurrent upserts from different
  sessions cannot clobber each other. agents/<agent>/cells/ is LOCAL-GIT (not own-cloud
  synced), so the own-cloud append-only constraint (guard-832) does not apply; a locked
  full-file RMW per category is the correct primitive.

TESTABILITY.
  Every public function accepts cells_dir=None. Pass an explicit directory (e.g. a tmp
  path) to isolate from the real agents/<agent>/cells/ tree; production resolves the path
  from agent_dir(<agent>)/cells. upsert_cell accepts now= for deterministic timestamps.

Usage:
  cells-archive.py upsert --cell-id c1 --category planning --state-signature "s" --score 3.0 \
      --trajectory '["g-1","g-2"]'
  cells-archive.py get   --cell-id c1 --category planning
  cells-archive.py list  --category planning
  cells-archive.py stats --category planning
  cells-archive.py categories
"""

import argparse
import json
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _fileops import locked_modify_yaml  # noqa: E402
from _paths import agent_dir, _resolve_agent_name  # noqa: E402

# K^D per-category cell cap. K=D=4 is the Gap 2 /  cognitively-grounded tree
# decomposition contract (memory-pipeline.yaml tree_structure "K=4, D=4"); 4**4 = 256.
CELL_CAP_K = 4
CELL_CAP_D = 4
CELL_CAP = CELL_CAP_K ** CELL_CAP_D  # 256

_NEG_INF = float("-inf")
_SAFE_CATEGORY_RE = re.compile(r"[^a-z0-9_-]+")


# --- path + value helpers ----------------------------------------------------

def _resolve_agent(agent=None):
    """Agent name: explicit arg wins, else MIND_AGENT. Loud when neither is set."""
    if agent:
        return agent
    name = _resolve_agent_name()
    if not name:
        raise RuntimeError(
            "cells-archive: no agent resolved -- set MIND_AGENT or pass agent="
        )
    return name


def _cells_root(agent=None, cells_dir=None):
    """Directory holding this agent's per-category cell files.

    cells_dir overrides everything (test injection). Otherwise
    agent_dir(<agent>)/cells.
    """
    if cells_dir is not None:
        return Path(cells_dir)
    return agent_dir(_resolve_agent(agent)) / "cells"


def _safe_category(category):
    """Filesystem-safe category stem: lowercased, '/'->'-', non [a-z0-9_-] -> '_'."""
    s = str(category).strip().lower().replace("/", "-")
    s = _SAFE_CATEGORY_RE.sub("_", s)
    return s or "uncategorized"


def _category_path(category, agent=None, cells_dir=None):
    """Path to the YAML file backing one category bucket."""
    return _cells_root(agent, cells_dir) / f"{_safe_category(category)}.yaml"


def _as_float(v):
    """Coerce to float; unparseable / missing -> -inf (sorts as weakest)."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return _NEG_INF


def _now_iso():
    """Local-system ISO timestamp (never UTC) -- matches the framework convention."""
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _new_record(cell_id, category, state_signature, trajectory, score, now):
    return {
        "cell_id": cell_id,
        "category": category,
        "state_signature": state_signature,
        "trajectory": list(trajectory) if trajectory else [],
        "score": float(score),
        "visits": 1,
        "created_at": now,
        "updated_at": now,
    }


def _min_score_cell(data):
    """(cell_id, score) of the weakest incumbent. Deterministic id tie-break."""
    min_id = None
    min_score = None
    for cid in sorted(data.keys()):
        s = _as_float(data[cid].get("score"))
        if min_score is None or s < min_score:
            min_score = s
            min_id = cid
    return min_id, min_score


# --- read API ----------------------------------------------------------------

def load_category(category, agent=None, cells_dir=None):
    """All cells in a category as {cell_id: record}. {} when the file is absent.

    Plain read: _atomic_write_with_fallback writes via temp+rename, so a reader
    sees either the prior or the new file whole -- never a torn write.
    """
    path = _category_path(category, agent, cells_dir)
    if not path.exists():
        return {}
    import yaml
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.load(f, Loader=loader)
    return data or {}


def get_cell(cell_id, category, agent=None, cells_dir=None):
    """One cell record, or None if absent."""
    return load_category(category, agent, cells_dir).get(cell_id)


def category_stats(category, agent=None, cells_dir=None):
    """count / min_score / max_score / total_visits for a category bucket."""
    data = load_category(category, agent, cells_dir)
    if not data:
        return {"category": category, "count": 0, "cap": CELL_CAP,
                "min_score": None, "max_score": None, "total_visits": 0}
    scores = [_as_float(r.get("score")) for r in data.values()]
    visits = sum(int(r.get("visits", 0) or 0) for r in data.values())
    return {
        "category": category,
        "count": len(data),
        "cap": CELL_CAP,
        "min_score": min(scores),
        "max_score": max(scores),
        "total_visits": visits,
    }


def list_categories(agent=None, cells_dir=None):
    """Sorted list of category stems with at least one cell file."""
    root = _cells_root(agent, cells_dir)
    if not root.exists():
        return []
    return sorted(p.stem for p in root.glob("*.yaml"))


# --- write API ---------------------------------------------------------------

def upsert_cell(cell_id, category, *, state_signature, trajectory, score,
                agent=None, cells_dir=None, now=None):
    """Insert or update a cell, honoring replace-on-strictly-better + K^D cap.

    Returns a verdict dict:
      {verdict, evicted, cell_id, category, score, size, cap}
    where verdict is one of:
      "added"             -- new cell, under cap
      "replaced"          -- existing cell, new score strictly better (trajectory replaced)
      "kept"              -- existing cell, new score not better (visits still bumped)
      "evicted-and-added" -- new cell admitted at full cap, weakest incumbent evicted
      "rejected-full"     -- new cell rejected at full cap (not better than the weakest)
    `evicted` is the evicted cell_id for "evicted-and-added", else None.
    """
    now = now or _now_iso()
    score = _as_float(score)
    path = _category_path(category, agent, cells_dir)
    # The modifier runs INSIDE locked_modify_yaml's lock; it captures its verdict
    # in this closure dict (locked_modify_yaml returns the written data, not the verdict).
    outcome = {"verdict": None, "evicted": None}

    def _modifier(data):
        if not isinstance(data, dict):
            data = {}
        existing = data.get(cell_id)

        if existing is not None:
            # EXISTING cell -- always record the return (visits + updated_at).
            existing["visits"] = int(existing.get("visits", 0) or 0) + 1
            existing["updated_at"] = now
            old_score = _as_float(existing.get("score"))
            if score > old_score:
                # Strictly better trajectory found -- replace path/state/score.
                existing["state_signature"] = state_signature
                existing["trajectory"] = list(trajectory) if trajectory else []
                existing["score"] = score
                outcome["verdict"] = "replaced"
            else:
                outcome["verdict"] = "kept"
            data[cell_id] = existing
            return data

        # NEW cell.
        rec = _new_record(cell_id, category, state_signature, trajectory, score, now)
        if len(data) >= CELL_CAP:
            min_id, min_score = _min_score_cell(data)
            if score > min_score:
                # Admit by evicting the weakest incumbent -- keep the strongest CELL_CAP.
                del data[min_id]
                data[cell_id] = rec
                outcome["verdict"] = "evicted-and-added"
                outcome["evicted"] = min_id
            else:
                # Cap full and not better than the weakest -- reject (no insert).
                outcome["verdict"] = "rejected-full"
        else:
            data[cell_id] = rec
            outcome["verdict"] = "added"
        return data

    written = locked_modify_yaml(path, _modifier, initial={})
    return {
        "verdict": outcome["verdict"],
        "evicted": outcome["evicted"],
        "cell_id": cell_id,
        "category": category,
        "score": score,
        "size": len(written) if isinstance(written, dict) else 0,
        "cap": CELL_CAP,
    }


# --- CLI ---------------------------------------------------------------------

def _emit(obj):
    print(json.dumps(obj, ensure_ascii=True, indent=2, default=str))


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="cells-archive.py",
        description="Go-Explore cell archive substrate (Mind-internal, BRD Gap 17).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("upsert", help="insert/update a cell")
    up.add_argument("--cell-id", required=True)
    up.add_argument("--category", required=True)
    up.add_argument("--state-signature", default="")
    up.add_argument("--score", type=float, required=True)
    up.add_argument("--trajectory", default="[]",
                    help="JSON list of trajectory steps (passed via argv, not interpolated)")
    up.add_argument("--agent", default=None)

    g = sub.add_parser("get", help="read one cell")
    g.add_argument("--cell-id", required=True)
    g.add_argument("--category", required=True)
    g.add_argument("--agent", default=None)

    ls = sub.add_parser("list", help="all cells in a category")
    ls.add_argument("--category", required=True)
    ls.add_argument("--agent", default=None)

    st = sub.add_parser("stats", help="stats for a category")
    st.add_argument("--category", required=True)
    st.add_argument("--agent", default=None)

    cats = sub.add_parser("categories", help="list category buckets")
    cats.add_argument("--agent", default=None)

    args = p.parse_args(argv)

    if args.cmd == "upsert":
        try:
            trajectory = json.loads(args.trajectory)
        except (ValueError, TypeError) as e:
            print(f"cells-archive: --trajectory is not valid JSON: {e}", file=sys.stderr)
            return 2
        if not isinstance(trajectory, list):
            print("cells-archive: --trajectory must be a JSON list", file=sys.stderr)
            return 2
        result = upsert_cell(
            args.cell_id, args.category,
            state_signature=args.state_signature,
            trajectory=trajectory, score=args.score, agent=args.agent,
        )
        _emit(result)
        return 0

    if args.cmd == "get":
        _emit(get_cell(args.cell_id, args.category, agent=args.agent))
        return 0

    if args.cmd == "list":
        _emit(load_category(args.category, agent=args.agent))
        return 0

    if args.cmd == "stats":
        _emit(category_stats(args.category, agent=args.agent))
        return 0

    if args.cmd == "categories":
        _emit(list_categories(agent=args.agent))
        return 0

    p.error(f"unknown command {args.cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
