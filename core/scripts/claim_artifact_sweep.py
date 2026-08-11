#!/usr/bin/env python3
"""claim_artifact_sweep.py — find EVERY artifact asserting a falsified claim.

Mechanises guard-1710: when a measurement falsifies a claim, sweep for every
artifact that still asserts it and fix them in the SAME change. Recording the
correction in ONE artifact while another keeps emitting the old claim leaves a
half-corrected state that is worse than no correction, because the stale copy
is what the next reader finds.

The procedure is deterministic given the claim's distinctive tokens, which is
why this is a `utility` mechanisation and not a judgment call:

  1. grep the tokens across the FULL surface set (below)
  2. classify each hit  ASSERTS / ALREADY_CORRECTED / UNRELATED
  3. recommend a correction SHAPE per hit (edit-in-place vs qualify-in-place)

Step 2 is the load-bearing one. A raw grep emits false edit targets — the same
words used in an unrelated sentence, or a line that ALREADY carries the
retraction. Editing either is a fresh defect.

SURFACE SET (widened by gap-040's third encounter, g-335-392):
  knowledge tree · world conventions · core conventions · framework config ·
  framework rules · skills · THE REASONING BANK · guardrails · goal
  descriptions · framework scripts (docstrings + stdout/stderr strings) ·
  optionally a product repo's source docblocks and docs/

The reasoning bank is the easiest to miss and the worst to leave stale: an rb
`when_to_use` fires at the exact decision the stale claim would misinform. In
g-335-392 rb-5545 surfaced ONLY because an unrelated protocol step happened to
mandate a duplicate check — luck, not coverage. Mechanising that is the point.

POSITIVE CONTROL: every surface reports how many records/files it actually
scanned. A surface that scanned 0 is reported as UNREADABLE, never as "clean"
(rb-245 / guard-1102 — a zero measured through a failed path is vacuous, and
this tool exists to prevent exactly that class of error).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from _paths import PROJECT_ROOT, WORLD_DIR  # type: ignore
except Exception:  # pragma: no cover - fallback for hermetic tests
    PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
    WORLD_DIR = Path(os.environ.get("MIND_WORLD", PROJECT_ROOT / ".mind-data" / "world"))

# A hit is ALREADY_CORRECTED when one of these sits near the matched tokens.
# Deliberately generous: a false ALREADY_CORRECTED costs a second look, while a
# false ASSERTS costs an edit that re-breaks a corrected artifact.
RETRACTION_MARKERS = re.compile(
    r"\b(falsifi\w*|refut\w*|retract\w*|correct(?:ed|ion)|supersed\w*|"
    r"no longer (?:true|valid|correct|holds)|turned out|was wrong|"
    r"NOT the reason|not the case|disprov\w*|obsolete|stale|deprecated|"
    r"do not (?:re-?implement|reuse|trust)|never (?:was|held))\b",
    re.IGNORECASE,
)

# Stores whose records must be MARKED, never removed (guard-1072: union-by-id
# merge handlers have no tombstone, so a pop silently resurrects on next merge).
MARK_NEVER_REMOVE = {"reasoning_bank", "guardrails", "goals", "goals_agent_queue"}


# Vendored/build trees carry no authored claims and will exhaust the per-surface
# cap before a single first-party file is reached (measured: a product repo hit
# the 4000 cap almost entirely on node_modules).
EXCLUDE_DIRS = {"node_modules", ".git", "dist", "build", ".next", ".venv",
                "venv", "vendor", "__pycache__", ".mypy_cache", ".pytest_cache",
                "coverage", ".turbo", "out", "target"}


def _iter_text_files(root: Path, patterns, limit=None):
    """Files under root matching any glob, vendor dirs excluded. Deterministic.

    Returns (files, truncated). `truncated` is load-bearing: a capped scan that
    reports only its count reads exactly like a complete one, which is the
    failure this whole tool exists to prevent (guard-1760).
    """
    seen = []
    if not root.exists():
        return [], False
    for pat in patterns:
        try:
            seen.extend(sorted(root.glob(pat)))
        except Exception:
            continue
    uniq, out, truncated = set(), [], False
    for p in seen:
        if p in uniq or not p.is_file():
            continue
        if EXCLUDE_DIRS & set(p.parts):
            continue
        uniq.add(p)
        out.append(p)
        if limit and len(out) >= limit:
            truncated = True
            break
    return out, truncated


def _read(p: Path):
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def _jsonl_records(path: Path):
    """Read a JSONL store. Returns (records, error). Never raises."""
    if not path.exists():
        return [], f"missing: {path}"
    out = []
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception as exc:
        return [], f"unreadable: {exc}"
    return out, None


def _windows(text, tokens, radius):
    """Yield (line_no, excerpt, matched_tokens) for regions where tokens cluster."""
    lines = text.splitlines()
    low = [ln.lower() for ln in lines]
    hits = []
    for i, ln in enumerate(low):
        matched = [t for t in tokens if t.lower() in ln]
        if not matched:
            continue
        lo, hi = max(0, i - radius), min(len(lines), i + radius + 1)
        window = "\n".join(lines[lo:hi])
        wl = window.lower()
        near = [t for t in tokens if t.lower() in wl]
        hits.append((i + 1, window, sorted(set(near)), lines[i].strip()))
    return hits


def _merge_adjacent(hits, gap=6):
    """Collapse hits whose line numbers are within `gap` — one artifact region."""
    if not hits:
        return []
    hits = sorted(hits, key=lambda h: h[0])
    out = [hits[0]]
    for h in hits[1:]:
        if h[0] - out[-1][0] <= gap:
            prev = out[-1]
            out[-1] = (prev[0], prev[1], sorted(set(prev[2]) | set(h[2])), prev[3])
        else:
            out.append(h)
    return out


def classify(excerpt, matched_tokens, all_tokens, min_tokens):
    """ASSERTS | ALREADY_CORRECTED | UNRELATED, plus the reason.

    The three-way split is the whole value over a raw grep:
      UNRELATED         -> same words, not this claim. Do NOT edit.
      ALREADY_CORRECTED -> the retraction already landed here. Do NOT re-edit.
      ASSERTS           -> still states the falsified claim. THIS is the work.
    """
    n = len(matched_tokens)
    required = min(min_tokens, len(all_tokens))
    if n < required:
        return "UNRELATED", (
            f"only {n}/{len(all_tokens)} distinct token(s) co-occur "
            f"(needs {required}) — same words, different subject"
        )
    m = RETRACTION_MARKERS.search(excerpt)
    if m:
        return "ALREADY_CORRECTED", (
            f"retraction marker {m.group(0)!r} within the window — "
            "already carries the correction; re-editing would be a fresh defect"
        )
    return "ASSERTS", f"{n}/{len(all_tokens)} tokens co-occur with no retraction marker nearby"


def correction_shape(surface):
    """guard-1072: union-by-id stores must be MARKED, never removed."""
    # Per-agent queues are named goals_agent_<name> — prefix-match, or every
    # peer queue silently loses the never-remove warning it most needs.
    if surface in MARK_NEVER_REMOVE or surface.startswith("goals_agent"):
        return (
            "qualify-in-place (amend the record) or mark retired — "
            "NEVER pop/remove: union-by-id merge has no tombstone (guard-1072)"
        )
    return "edit-in-place"


def sweep(tokens, min_tokens=2, radius=2, product_repo=None, per_surface_limit=4000):
    surfaces = {}

    def add_text_surface(name, root, patterns, label_root=None):
        files, truncated = _iter_text_files(root, patterns, limit=per_surface_limit)
        rec = {"scanned": len(files), "unit": "files", "hits": [], "error": None,
               "truncated": truncated}
        if not root.exists():
            # Only a MISSING root is vacuous. A root that exists and holds
            # nothing is an informative zero — see `unreadable` below.
            rec["error"] = f"root missing: {root}"
        for p in files:
            txt = _read(p)
            if txt is None:
                continue
            for ln, window, matched, line in _merge_adjacent(_windows(txt, tokens, radius)):
                verdict, why = classify(window, matched, tokens, min_tokens)
                rec["hits"].append({
                    "surface": name,
                    "path": str(p.relative_to(label_root or PROJECT_ROOT))
                    if str(p).startswith(str(label_root or PROJECT_ROOT)) else str(p),
                    "line": ln,
                    "verdict": verdict,
                    "why": why,
                    "matched_tokens": matched,
                    "excerpt": line[:240],
                    "correction_shape": correction_shape(name),
                })
        surfaces[name] = rec

    def add_jsonl_surface(name, path, fields, nested_key=None):
        """Scan a JSONL store. `nested_key` flattens sub-records (goals inside
        aspirations) — without it the goal-description surface scans only the 28
        aspiration wrappers and silently misses all ~4700 goals inside them."""
        records, err = _jsonl_records(path)
        if nested_key:
            flat = []
            for r in records:
                for sub in (r.get(nested_key) or []):
                    if isinstance(sub, dict):
                        flat.append(sub)
            records = flat
        rec = {"scanned": len(records), "unit": "records", "hits": [], "error": err}
        for r in records:
            blob = "\n".join(str(r.get(f, "")) for f in fields if r.get(f))
            if not blob:
                continue
            low = blob.lower()
            matched = sorted({t for t in tokens if t.lower() in low})
            if not matched:
                continue
            verdict, why = classify(blob, matched, tokens, min_tokens)
            rec["hits"].append({
                "surface": name,
                "path": f"{path.name}#{r.get('id') or r.get('goal_id') or '?'}",
                "record_id": r.get("id") or r.get("goal_id"),
                "line": None,
                "verdict": verdict,
                "why": why,
                "matched_tokens": matched,
                "excerpt": (r.get("title") or r.get("rule") or blob)[:240],
                "correction_shape": correction_shape(name),
            })
        surfaces[name] = rec

    add_text_surface("knowledge_tree", WORLD_DIR / "knowledge" / "tree", ["**/*.md"], WORLD_DIR)
    add_text_surface("world_conventions", WORLD_DIR / "conventions", ["*.md"], WORLD_DIR)
    add_text_surface("core_conventions", PROJECT_ROOT / "core" / "config" / "conventions", ["*.md"])
    add_text_surface("framework_config", PROJECT_ROOT / "core" / "config", ["*.md", "*.yaml"])
    add_text_surface("framework_rules", PROJECT_ROOT / ".claude" / "rules", ["*.md"])
    add_text_surface("skills", PROJECT_ROOT / ".claude" / "skills", ["*/SKILL.md"])
    add_text_surface("framework_scripts", PROJECT_ROOT / "core" / "scripts", ["*.py", "*.sh"])

    add_jsonl_surface("reasoning_bank", WORLD_DIR / "reasoning-bank.jsonl",
                      ["title", "content", "when_to_use", "failure_lesson"])
    add_jsonl_surface("guardrails", WORLD_DIR / "guardrails.jsonl",
                      ["rule", "trigger_condition", "action_hint"])
    # Goal descriptions live NESTED inside aspiration records — flatten, or this
    # surface scans 28 wrappers instead of ~4700 goals (measured 2026-08-01).
    add_jsonl_surface("goals", WORLD_DIR / "aspirations.jsonl",
                      # Field-name set MEASURED, not assumed (rb-245): across 4693 live
                      # goals, outcome_note=687, verify_summary=18,
                      # verification_summary=9. Picking only the last would miss the
                      # closure-rationale surface almost entirely.
                      ["title", "description", "motivation",
                       "outcome_note", "verify_summary", "verification_summary"],
                      nested_key="goals")
    # EVERY agent's private queue, not just the bound one. A claim asserted in a
    # peer's goal description is exactly as stale-making as one in mine, and
    # scoping to MIND_AGENT hid 4 of 5 live queues (measured 2026-08-01).
    # Routed through agents_root() so it tracks an AGENTS_PARENT_DIR rename —
    # a depth-1 glob here would silently match nothing (CLAUDE.md audit table).
    try:
        from _paths import agents_root  # type: ignore
        _agent_queues = sorted(agents_root().glob("*/aspirations.jsonl"))
    except Exception:
        _agent_queues = []
    for _q in _agent_queues:
        add_jsonl_surface(f"goals_agent_{_q.parent.name}", _q,
                          # Field-name set MEASURED, not assumed (rb-245): across 4693 live
                      # goals, outcome_note=687, verify_summary=18,
                      # verification_summary=9. Picking only the last would miss the
                      # closure-rationale surface almost entirely.
                      ["title", "description", "motivation",
                       "outcome_note", "verify_summary", "verification_summary"],
                          nested_key="goals")

    if product_repo:
        pr = Path(product_repo)
        add_text_surface("product_repo", pr, ["**/*.md", "**/*.java", "**/*.py",
                                              "**/*.ts", "**/*.js", "**/*.lua"], pr)

    all_hits = [h for s in surfaces.values() for h in s["hits"]]
    asserts = [h for h in all_hits if h["verdict"] == "ASSERTS"]
    # A zero is vacuous ONLY when the path failed. A surface whose root exists
    # and legitimately holds nothing scanned zero and told you something true —
    # conflating the two makes every sparse deployment look broken and trains
    # readers to ignore the warning that matters (rb-245 protects the FIRST case).
    unreadable = [n for n, s in surfaces.items() if s["scanned"] == 0 and s["error"]]
    empty = [n for n, s in surfaces.items() if s["scanned"] == 0 and not s["error"]]

    return {
        "tokens": tokens,
        "min_tokens_to_assert": min_tokens,
        "surfaces_scanned": len(surfaces),
        "coverage": {n: {"scanned": s["scanned"], "unit": s["unit"], "error": s["error"],
                         "truncated": s.get("truncated", False)}
                     for n, s in surfaces.items()},
        "truncated_surfaces": [n for n, s in surfaces.items() if s.get("truncated")],
        "unreadable_surfaces": unreadable,
        "empty_surfaces": empty,
        "counts": {
            "total_hits": len(all_hits),
            "ASSERTS": len(asserts),
            "ALREADY_CORRECTED": sum(1 for h in all_hits if h["verdict"] == "ALREADY_CORRECTED"),
            "UNRELATED": sum(1 for h in all_hits if h["verdict"] == "UNRELATED"),
        },
        "verdict": (
            "UNREADABLE_SURFACES" if unreadable else
            "CORRECTIONS_REQUIRED" if asserts else
            "CLEAN"
        ),
        "asserts": sorted(asserts, key=lambda h: (h["surface"], h["path"], h["line"] or 0)),
        "all_hits": all_hits,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tokens", required=True,
                    help="Comma-separated distinctive tokens of the falsified claim.")
    ap.add_argument("--claim", default="", help="One-line statement of the falsified claim (echoed).")
    ap.add_argument("--min-tokens", type=int, default=2,
                    help="Distinct tokens that must co-occur for ASSERTS (default 2).")
    ap.add_argument("--radius", type=int, default=2, help="Context lines each side (default 2).")
    ap.add_argument("--product-repo", default=None, help="Optional product repo root to also scan.")
    ap.add_argument("--output", choices=["json", "text"], default="text")
    ap.add_argument("--show", type=int, default=25, help="Max ASSERTS rows in text output.")
    ap.add_argument("--limit", type=int, default=4000,
                    help="Max files scanned per text surface (default 4000). A surface "
                         "that hits this is reported TRUNCATED — coverage is incomplete.")
    args = ap.parse_args()

    tokens = [t.strip() for t in args.tokens.split(",") if t.strip()]
    if not tokens:
        print(json.dumps({"error": "no tokens supplied"}))
        return 2

    res = sweep(tokens, min_tokens=args.min_tokens, radius=args.radius,
                product_repo=args.product_repo, per_surface_limit=args.limit)
    res["claim"] = args.claim

    if args.output == "json":
        print(json.dumps(res, indent=1))
    else:
        print(f"claim-artifact-sweep  tokens={tokens}  min_tokens={args.min_tokens}")
        if args.claim:
            print(f"  falsified claim: {args.claim}")
        print(f"  VERDICT: {res['verdict']}")
        c = res["counts"]
        print(f"  hits: {c['total_hits']}  ASSERTS={c['ASSERTS']} "
              f"ALREADY_CORRECTED={c['ALREADY_CORRECTED']} UNRELATED={c['UNRELATED']}")
        print("  coverage (positive control — a 0 here is UNREADABLE, not clean):")
        for n, cov in res["coverage"].items():
            if cov["scanned"] == 0:
                flag = "  <-- UNREADABLE (vacuous zero)" if cov["error"] \
                    else "  <-- empty (root present, nothing to scan)"
            elif cov.get("truncated"):
                flag = "  <-- TRUNCATED at cap: coverage INCOMPLETE, raise --limit"
            else:
                flag = ""
            err = f" err={cov['error']}" if cov["error"] else ""
            print(f"    {n:<20} {cov['scanned']:>6} {cov['unit']}{err}{flag}")
        if res["asserts"]:
            print(f"  --- ASSERTS (correct ALL of these in ONE change, guard-1710) ---")
            for h in res["asserts"][:args.show]:
                loc = f":{h['line']}" if h["line"] else ""
                print(f"    [{h['surface']}] {h['path']}{loc}")
                print(f"       {h['excerpt']}")
                print(f"       why: {h['why']}")
                print(f"       fix: {h['correction_shape']}")
            if len(res["asserts"]) > args.show:
                print(f"    ... and {len(res['asserts']) - args.show} more (use --output json)")
    return 0 if res["verdict"] != "UNREADABLE_SURFACES" else 3


if __name__ == "__main__":
    sys.exit(main())
