#!/usr/bin/env python3
"""Detect drift in knowledge-tree references to external product-repo code.

Knowledge-tree nodes cite code by `File.ext:LINE` and assert behavioural claims
about what is there. When the cited repo changes, the node silently goes stale —
retrieval then surfaces a confidently-wrong fact, which is worse than a missing
one. Nothing detected that before this script (g-115-3114, discovered by
g-326-53, where ONE node carried six drifted line refs plus a behavioural claim
about a code path that no longer existed).

REPORTS ONLY — never edits. The corrections need judgment: a moved line is
mechanical, but a stale behavioural claim is not, and auto-rewriting the line
number would preserve the wrong prose around it.

THE ZERO MUST BE INTERPRETABLE (guard-1641 / guard-467 / guard-1214). A scanner
that resolves nothing reports "0 drift" exactly like a clean corpus does. So the
report always carries a positive control: how many refs RESOLVED to a real file
and how many were verified in range. `interpretable` is false when nothing
resolved, and the human report says so instead of printing a clean-looking zero.

Verdicts
--------
  ok                 resolved, line in range, and any checkable symbol found near it
  line_out_of_range  DRIFT (hard) — the cited line is past the end of the file
  symbol_moved       DRIFT (soft) — a symbol named in the surrounding prose is
                     absent near the cited line but present elsewhere in the file
  unresolved         basename matched no file under any repo root (NOT drift —
                     the repo may simply not be checked out on this box)
  ambiguous          several candidate files and no path hint to choose between

Only `line_out_of_range` and `symbol_moved` count as drift. `symbol_moved`
requires POSITIVE evidence of movement — the symbol must exist elsewhere in the
same file — so a prose word that was never a code symbol cannot manufacture a
finding.

Usage
-----
  py -3 core/scripts/tree-code-ref-drift.py                 # human report
  py -3 core/scripts/tree-code-ref-drift.py --output json
  py -3 core/scripts/tree-code-ref-drift.py --node <path>   # one node
  py -3 core/scripts/tree-code-ref-drift.py --exit-on-drift # rc=1 when drift found

Call site
---------
Recurring goal g-115-5633, interval 168h, wired by g-115-4236. Until then this
script had NO caller at all — only its own test file named it — which made it
indistinguishable from a scanner that always returns clean. Stated here so the
next reader can answer "does this run?" without re-deriving the audit.

Gate on `confirmed_total` only; leads never set a failing status. Read the
verdict from `--output json`, NOT from the exit code: rc=1 means a CONFIRMED
finding *or* an uncaught traceback (g-115-4239 defect 1), and nothing about the
rc distinguishes them.

The recurring lane was chosen over an aspirations-precheck sweep because it is
the only candidate carrying an open-loop starvation detector (precheck Phase
0.5c.1, g-115-3921) — a cadence that can silently stop firing is the same defect
as a scanner with no caller, one level up. Keep the caller a SUBPROCESS: an
in-process cadence calling scan() twice in one interpreter activates the stale
module-global `_line_cache` (g-115-4239 defect 3, which names g-115-4236 by id
as its trigger).
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from _paths import PROJECT_ROOT, WORLD_DIR  # type: ignore
except Exception:  # pragma: no cover - _paths is always importable in-tree
    PROJECT_ROOT = Path(__file__).resolve().parents[2]
    WORLD_DIR = PROJECT_ROOT / "world"

# Code extensions only. Prose refs (`state.md:105`) are a different drift class
# with different repair rules, so they stay out unless asked for explicitly.
CODE_EXTS = (
    "java|py|lua|ts|tsx|js|jsx|mjs|cjs|kt|go|rb|sh|bash|c|cpp|h|hpp|cs|rs|sql|"
    "gradle|yaml|yml|json|xml|toml"
)

REF_RE = re.compile(
    r"(?P<ref>(?:[A-Za-z0-9_./-]*/)?(?P<base>[A-Za-z_][A-Za-z0-9_.-]*\."
    r"(?:" + CODE_EXTS + r")))"
    r":(?P<start>\d+)(?:\s*-\s*(?P<end>\d+))?"
)

SKIP_DIRS = {
    ".git", "node_modules", "build", "target", "dist", "out", "__pycache__",
    ".venv", "venv", ".gradle", ".idea", ".mypy_cache", ".pytest_cache",
    "vendor", "coverage", ".next", ".cache",
}

# Identifier shapes worth checking: CamelCase, snake_case with an underscore,
# or a call/attribute chain. Bare lowercase words are prose far more often than
# they are symbols, so they are not checked (a false "moved" is worse than a
# missed one — it teaches readers to distrust the report).
SYMBOL_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_.]*(?:\(\))?)`")
CAMEL_RE = re.compile(r"\b([a-z]+[A-Z][A-Za-z0-9]*|[A-Z][a-z]+[A-Z][A-Za-z0-9]*)\b")
SNAKE_RE = re.compile(r"(?<![\w.])([a-z][a-z0-9]*(?:_[a-z0-9]+){1,})(?![\w])")

PROSE_WINDOW = 220   # chars of surrounding prose scanned for symbol candidates
LINE_WINDOW = 25     # +/- lines around the cited line the symbol may appear in
MIN_SYMBOL_LEN = 6   # shorter tokens are prose far more often than identifiers
MAX_OCCURRENCES = 20  # a token this common locates nothing; it is not a landmark

# Tokens that LOOK like identifiers but carry no locating power. Every entry
# earned its place by producing a finding that hand-checking falsified.
SYMBOL_STOPLIST = {
    "main", "master", "origin", "python", "python3", "json.dumps", "json.loads",
    "os.environ", "sys.argv", "true", "false", "none", "null", "return",
    "import", "export", "string", "boolean", "integer", "default", "config",
    "status", "result", "output", "input", "error", "value", "values",
    # Log severities and level names: authors backtick them, but they occur
    # wherever logging occurs, so they locate nothing.
    "critical", "warning", "verbose", "severe", "fatal", "debug", "trace",
}


def default_repo_roots():
    """PROJECT_ROOT plus every entry of AGENT_WRITE_PATH that exists.

    AGENT_WRITE_PATH is the agent's sanctioned product-repo list (semicolon-
    separated in local-paths.conf). PROJECT_ROOT is included because a large
    share of tree refs cite framework code, and resolving them costs one more
    root rather than a second code path.
    """
    roots = [Path(PROJECT_ROOT)]
    raw = os.environ.get("AGENT_WRITE_PATH", "")
    if not raw:
        conf = Path(PROJECT_ROOT) / "agents" / os.environ.get("MIND_AGENT", "") / "local-paths.conf"
        if conf.is_file():
            for line in conf.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("AGENT_WRITE_PATH="):
                    raw = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    for part in raw.split(";"):
        part = part.strip().strip('"').strip("'")
        if part and Path(part).is_dir():
            roots.append(Path(part))
    # De-dup while preserving order. Nested roots are not pruned — a duplicate
    # walk costs time, never correctness, since the index is keyed by basename
    # and identical paths collapse when candidates are compared.
    seen, out = set(), []
    for r in roots:
        rp = r.resolve()
        if rp in seen:
            continue
        seen.add(rp)
        out.append(rp)
    return out


def build_index(roots):
    """basename -> [abs paths]. One walk per root; the corpus is ~200k files."""
    index = {}
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                index.setdefault(fn, []).append(os.path.join(dirpath, fn))
    return index


def resolve_ref(ref, base, index):
    """Pick the file a `path/to/File.ext` reference names.

    A ref carrying path segments must match a candidate whose path ENDS with
    those segments — that is what makes two same-named files distinguishable.
    """
    cands = index.get(base)
    if not cands:
        return None, "unresolved"
    if "/" in ref:
        suffix = ref.replace("\\", "/")
        narrowed = [c for c in cands if c.replace("\\", "/").endswith(suffix)]
        if len(narrowed) == 1:
            return narrowed[0], "resolved"
        if len(narrowed) > 1:
            return None, "ambiguous"
        # Path hint matched nothing: the path itself may have moved. Fall back
        # to the basename candidates rather than calling it unresolved.
    if len(cands) == 1:
        return cands[0], "resolved"
    return None, "ambiguous"


EXEMPT_MARKER = "ref-drift-exempt"
EXEMPT_LOOKBACK_LINES = 3


def _is_exempt(text, ref_offset):
    """True when the citation at `ref_offset` is marked deliberately-historical.

    The marker may sit on the citation's own line or any of the
    EXEMPT_LOOKBACK_LINES lines above it. The lookback exists because the
    natural way to write the marker is a wrapped HTML comment, and a one-line
    lookback silently misses every marker whose comment wrapped — which is the
    common case, since the marker text plus a rationale rarely fits on one
    line. Measured: the first marker written in this codebase wrapped to two
    lines and was silently ignored.
    """
    line_end = text.find("\n", ref_offset)
    line_end = len(text) if line_end == -1 else line_end
    start = text.rfind("\n", 0, ref_offset) + 1
    for _ in range(EXEMPT_LOOKBACK_LINES):
        if start == 0:
            break
        start = text.rfind("\n", 0, start - 1) + 1
    return EXEMPT_MARKER in text[start:line_end]


_line_cache = {}


def file_lines(path):
    if path not in _line_cache:
        try:
            _line_cache[path] = Path(path).read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except Exception:
            _line_cache[path] = None
    return _line_cache[path]


def _acceptable(sym, stem):
    """Reject tokens that cannot locate a line even when they are real symbols.

    The file's own name is the big one: `GoalPerception` inside
    GoalPerceptionVerticle.java matches the class declaration and every
    self-reference, so its distance from any cited line is an artifact of where
    the class header sits — not evidence the code moved. Measured: the two
    lowest-distance leads in the first full scan were both this shape.
    """
    if len(sym) < MIN_SYMBOL_LEN or sym.lower() in SYMBOL_STOPLIST:
        return False
    head = sym.split(".", 1)[0]
    return not (head in stem or stem in head)


def prose_symbols(text, ref_start, ref_end, base):
    """Symbol candidates from the prose around a reference.

    BACKTICKED TOKENS WIN OUTRIGHT. Tree convention marks code identifiers with
    backticks, so when the author has done that, guessing at unmarked prose
    words only adds noise. The bare-prose pass is the fallback for nodes that
    did not use backticks, and it is deliberately stricter (>=2 humps or >=2
    underscores) because an unmarked token is a guess about the author's intent.

    Loosening either filter costs more than it buys: a false 'moved' verdict
    teaches the reader to distrust the whole report, and a report nobody trusts
    detects nothing at all.
    """
    lo = max(0, ref_start - PROSE_WINDOW)
    hi = min(len(text), ref_end + PROSE_WINDOW)
    window = text[lo:ref_start] + " " + text[ref_end:hi]
    stem = base.rsplit(".", 1)[0]

    ticked = []
    for m in SYMBOL_RE.finditer(window):
        sym = m.group(1).rstrip("()")
        if _acceptable(sym, stem) and sym not in ticked:
            ticked.append(sym)
    if ticked:
        return ticked[:6]

    loose = []
    for m in CAMEL_RE.finditer(window):
        sym = m.group(1)
        if _acceptable(sym, stem) and sym not in loose:
            loose.append(sym)
    for m in SNAKE_RE.finditer(window):
        sym = m.group(1)
        if sym.count("_") >= 2 and _acceptable(sym, stem) and sym not in loose:
            loose.append(sym)
    return loose[:6]


def check_symbol(lines, start, end, symbols):
    """Return (symbol, actual_lines) when a symbol moved away from the citation.

    Only fires when the symbol IS present in the file but NOT within the window
    around the cited span — presence elsewhere is the positive evidence that
    distinguishes 'the code moved' from 'that word was never a symbol'.

    A token appearing more than MAX_OCCURRENCES times is skipped: it cannot
    locate anything, so its absence from one window is not evidence of movement.
    """
    lo = max(0, start - 1 - LINE_WINDOW)
    hi = min(len(lines), (end or start) + LINE_WINDOW)
    near = "\n".join(lines[lo:hi])
    for sym in symbols:
        if sym in near:
            return None, [], 0       # cited location still carries the symbol
    for sym in symbols:
        hits = [i + 1 for i, ln in enumerate(lines) if sym in ln]
        if hits and len(hits) <= MAX_OCCURRENCES:
            # Distance to the NEAREST occurrence. This is the triage signal: a
            # symbol 3 lines outside the window is prose slop, one 400 lines
            # away is a citation pointing at the wrong part of the file.
            span_lo, span_hi = start, (end or start)
            dist = min(min(abs(h - span_lo), abs(h - span_hi)) for h in hits)
            return sym, hits[:5], dist
    return None, [], 0


def scan(tree_root, index, nodes=None):
    findings, stats = [], {
        "nodes_scanned": 0, "refs_total": 0, "resolved": 0, "ok": 0,
        "unresolved": 0, "ambiguous": 0, "exempt": 0,
        "line_out_of_range": 0, "symbol_moved": 0,
    }
    paths = nodes if nodes else sorted(Path(tree_root).rglob("*.md"))
    for node in paths:
        try:
            text = Path(node).read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        stats["nodes_scanned"] += 1
        for m in REF_RE.finditer(text):
            stats["refs_total"] += 1
            # A node that RETRACTS a stale citation still contains it, so
            # without an opt-out the scanner re-flags every correction it
            # causes — forever, and most loudly on the nodes that did the right
            # thing. `ref-drift-exempt` on the same line (or the one above)
            # marks a deliberately-historical citation. Same marker convention
            # as `domain-leak-exempt`.
            if _is_exempt(text, m.start()):
                stats["exempt"] += 1
                continue
            ref, base = m.group("ref"), m.group("base")
            start = int(m.group("start"))
            end = int(m.group("end")) if m.group("end") else None
            path, status = resolve_ref(ref, base, index)
            if status != "resolved":
                stats[status] += 1
                continue
            lines = file_lines(path)
            if lines is None:
                stats["unresolved"] += 1
                continue
            stats["resolved"] += 1
            node_rel = str(Path(node).relative_to(tree_root)) \
                if str(node).startswith(str(tree_root)) else str(node)
            cited_last = end or start
            if cited_last > len(lines):
                stats["line_out_of_range"] += 1
                findings.append({
                    "verdict": "line_out_of_range", "confidence": "confirmed",
                    "node": node_rel, "ref": m.group(0),
                    "file": path, "cited_line": start, "cited_end": end,
                    "file_line_count": len(lines),
                    "detail": f"cited line {cited_last} exceeds the file's {len(lines)} lines",
                })
                continue
            sym, hits, dist = check_symbol(lines, start, end, prose_symbols(
                text, m.start(), m.end(), base))
            if sym:
                stats["symbol_moved"] += 1
                findings.append({
                    "verdict": "symbol_moved", "confidence": "lead",
                    "node": node_rel, "ref": m.group(0),
                    "file": path, "cited_line": start, "cited_end": end,
                    "symbol": sym, "actual_lines": hits, "distance": dist,
                    "detail": (f"'{sym}' is absent within +/-{LINE_WINDOW} lines of "
                               f"{start} but present at {hits} ({dist} lines away)"),
                })
            else:
                stats["ok"] += 1
    # Confirmed findings first, then leads by how far the symbol has moved —
    # distance is the triage order, since a lead 3 lines out is prose slop and
    # one 400 lines out is a citation pointing at the wrong part of the file.
    findings.sort(key=lambda f: (f["verdict"] != "line_out_of_range",
                                 -f.get("distance", 0)))
    return findings, stats


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tree-root", default=str(Path(WORLD_DIR) / "knowledge" / "tree"))
    ap.add_argument("--repo-root", action="append", default=[],
                    help="Repeatable. Defaults to PROJECT_ROOT + AGENT_WRITE_PATH.")
    ap.add_argument("--node", action="append", default=[],
                    help="Repeatable. Scan only these node paths.")
    ap.add_argument("--output", choices=["human", "json"], default="human")
    ap.add_argument("--limit", type=int, default=40, help="Findings shown in human output.")
    ap.add_argument("--min-distance", type=int, default=0,
                    help="Drop symbol_moved leads whose symbol sits within N lines "
                         "of the citation. Triage knob — 0 reports everything.")
    ap.add_argument("--exit-on-drift", action="store_true",
                    help="rc=1 when a CONFIRMED finding exists. Leads never set rc.")
    args = ap.parse_args()

    roots = [Path(r) for r in args.repo_root] if args.repo_root else default_repo_roots()
    roots = [r for r in roots if r.is_dir()]
    index = build_index(roots)
    nodes = [Path(n) for n in args.node] if args.node else None
    findings, stats = scan(Path(args.tree_root), index, nodes)

    if args.min_distance:
        kept = [f for f in findings
                if f["verdict"] != "symbol_moved" or f["distance"] >= args.min_distance]
        stats["symbol_moved"] = sum(1 for f in kept if f["verdict"] == "symbol_moved")
        findings = kept

    confirmed = stats["line_out_of_range"]
    leads = stats["symbol_moved"]
    # The positive control: a zero is only meaningful once the scanner has been
    # shown to match real references. resolved==0 means the instrument is dark.
    control = {
        "resolved_refs": stats["resolved"],
        "verified_in_range": stats["ok"],
        "interpretable": stats["resolved"] > 0,
        "why": ("scanner resolved and verified real references, so a low drift "
                "count reflects the corpus" if stats["resolved"] > 0 else
                "scanner resolved ZERO references — a drift count of 0 here says "
                "nothing about the corpus, only that the instrument matched nothing"),
    }
    report = {
        "repo_roots": [str(r) for r in roots], "indexed_basenames": len(index),
        "stats": stats, "confirmed_total": confirmed, "lead_total": leads,
        "positive_control": control, "findings": findings,
    }

    if args.output == "json":
        print(json.dumps(report, indent=2))
    else:
        print(f"[tree-code-ref-drift] roots={len(roots)} indexed={len(index)} "
              f"nodes={stats['nodes_scanned']} refs={stats['refs_total']}")
        print(f"  resolved={stats['resolved']} ok={stats['ok']} "
              f"unresolved={stats['unresolved']} ambiguous={stats['ambiguous']} "
              f"exempt={stats['exempt']}")
        print(f"  CONFIRMED drift = {confirmed} (line_out_of_range — mechanical, no judgment needed)")
        print(f"  LEADS           = {leads} (symbol_moved — heuristic; each needs a human read "
              f"before it is called drift)")
        print(f"  positive control: interpretable={control['interpretable']} — {control['why']}")
        for f in findings[:args.limit]:
            print(f"  [{f['confidence']}/{f['verdict']}] {f['node']}")
            print(f"      {f['ref']} -> {f['detail']}")
        if len(findings) > args.limit:
            print(f"  ... {len(findings) - args.limit} more (use --output json)")
    # Only CONFIRMED findings set rc. A heuristic lead must never fail a build or
    # a recurring goal — that is how a noisy signal gets silenced wholesale.
    return 1 if (args.exit_on_drift and confirmed) else 0


if __name__ == "__main__":
    sys.exit(main())
