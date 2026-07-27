#!/usr/bin/env python3
"""goal-reference-scan.py — inbound-reference precondition for goal-id
relocation (g-115-3096, DEFECT 2).

WHY THIS EXISTS. aspirations-evolve Step 2.75c relocates a recurring goal out
of a sprint/project aspiration by CREATING A COPY under a NEW goal id and
completing the original. A goal id is not a private key: long-lived recurring
goals accumulate inbound references across the reasoning bank, guardrails,
pattern signatures, pipeline, changelog, override ledgers, board posts — and
in non-store surfaces like source comments, tests, and rationale docs. Every
one of those references points at the OLD id, and relocation orphans all of
them to buy a single aspiration archival.

Observed (2026-07-25 evolve pass): a recurring goal with achievedCount=174 was
referenced across six world stores AND three non-store surfaces (a source
comment, a test, a rationale doc). Relocation was correctly skipped by hand.
This script makes that judgement mechanical instead of remembered — the
"write the gate, don't just write the instruction" discipline of guard-399.

WHAT COUNTS AS AN INBOUND REFERENCE. Occurrences of the goal id are split into
two classes, because conflating them makes the check useless:

  BLOCKING   — a live referent that would be ORPHANED by relocation: curated
               stores (reasoning bank, guardrails, pattern signatures,
               pipeline, override ledgers) plus code, tests, config and
               rationale docs under core/ and .claude/. These point AT the
               goal as a current thing.

  HISTORICAL — an append-only log entry DESCRIBING a past event involving the
               goal (changelog, evolution log, journal, board, experience,
               execution diary). These are records of what happened; they do
               not break when the goal moves, and they are not evidence
               against relocation.

Only BLOCKING references set the non-zero exit. Historical counts are still
reported, as context. Without this split the check is vacuous in the
always-fires direction: any goal that has ever run accumulates thousands of
log lines (a real recurring goal measured 3,825 total, of which the vast
majority were evolution-log narration), so a naive total would refuse every
relocation and teach the reader to ignore the gate.

Occurrences inside the aspirations store itself are excluded from both classes
— that is the goal's own record and the queue Step 2.75c is rewriting on
purpose. Archived aspiration files are excluded on the same grounds.

USAGE
    py -3 core/scripts/goal-reference-scan.py <goal-id> [--json] [--limit N]

    exit 0  no inbound references — relocation is safe
    exit 1  references found — SKIP relocation and log why
    exit 2  bad usage

Read-only. Never mutates anything.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from _paths import PROJECT_ROOT, WORLD_DIR, META_DIR
except Exception:  # pragma: no cover - resolver unavailable
    PROJECT_ROOT = Path(__file__).resolve().parents[1]
    WORLD_DIR = META_DIR = None

# Filenames whose hits are NOT inbound references: the aspirations store is
# what Step 2.75c is editing, so its own mentions are expected, not orphans.
_ASPIRATION_STORE_NAMES = {
    "aspirations.jsonl",
    "aspirations-archive.jsonl",
    "aspirations-meta.json",
    "aspirations-compact.json",
}

# Surfaces scanned for inbound references. Repo-side dirs are relative to
# PROJECT_ROOT; the world/meta dirs are external paths resolved by _paths.
_REPO_GLOBS = ("core/**/*", ".claude/**/*")

_TEXT_SUFFIXES = {
    ".py", ".sh", ".md", ".yaml", ".yml", ".json", ".jsonl", ".txt", ".cfg",
    ".ini", ".toml",
}

_SKIP_DIR_PARTS = {".git", "__pycache__", ".history", "node_modules", ".venv"}

# Append-only narration: a mention here DESCRIBES a past event involving the
# goal. Relocation does not break it, so it must not block. Keeping these in
# the blocking set makes the check fire for every goal that has ever run.
_HISTORICAL_NAMES = {
    "changelog.jsonl",
    "evolution-log.jsonl",
    "meta-log.jsonl",
    "experience.jsonl",
    "journal.jsonl",
    "execution-diary.jsonl",
    "skill-invocations.jsonl",
    "gate-firings.jsonl",
    "gate-eval-recommendations.jsonl",
    "precheck-drops.jsonl",
    "aspiration-events.jsonl",
    "improvement-velocity.yaml",   # imp@k per-goal telemetry
    "retrieval-trace.jsonl",       # per-retrieval trace
}
_HISTORICAL_DIR_PARTS = {"board", "journal", "health", "experience", "logs",
                         "sessions", "temp", "drained", "presence"}


def is_historical(p: Path, rel: Path) -> bool:
    """Narration (history / telemetry / archives) vs live referent.

    Archives are matched by SUFFIX rather than by name so a new
    `<store>-archive.jsonl` is classified correctly the day it appears — a
    name list would silently mis-classify it as blocking and re-introduce the
    always-fires failure mode. (changelog-archive.jsonl alone contributed
    3,211 of one real goal's 3,326 pre-split "blocking" hits.)

    `rel` MUST be the path RELATIVE to its scan root, never the absolute path.
    Matching directory names against absolute `p.parts` means every ANCESTOR of
    the repo is tested too, so a checkout under a dir named temp/ (or logs/,
    board/, sessions/, health/, experience/) silently classifies EVERY file as
    narration and the precondition stops blocking anything — the always-PASSES
    inverse of the always-fires bug, and more dangerous because a gate that
    never fires looks exactly like a clean repo. Found by fresh-eyes probe on
    this file (g-115-3096): the same file returned rc=1 under a normal path and
    rc=0 CLEAR under <root>/temp/repo/.
    """
    if p.name.endswith("-archive.jsonl"):
        return True
    if p.name in _HISTORICAL_NAMES:
        return True
    return any(part in _HISTORICAL_DIR_PARTS for part in rel.parts)


def _is_scannable(p: Path, rel: Path) -> bool:
    """`rel` is root-relative for the same reason as is_historical() above: a
    repo checked out under a dir named .venv/ or node_modules/ would otherwise
    skip every file and report a vacuous CLEAR."""
    if not p.is_file():
        return False
    if p.name in _ASPIRATION_STORE_NAMES:
        return False
    if any(part in _SKIP_DIR_PARTS for part in rel.parts):
        return False
    return p.suffix.lower() in _TEXT_SUFFIXES


def _rel(p: Path, root: Path) -> Path:
    try:
        return p.relative_to(root)
    except ValueError:
        return Path(p.name)   # not under root — classify on the name alone


def _iter_targets(extra_roots, project_root=None):
    """Yield (path, root_relative_path) so classification never sees ancestors."""
    seen = set()
    base = Path(project_root) if project_root else PROJECT_ROOT
    for pattern in _REPO_GLOBS:
        for p in base.glob(pattern):
            rel = _rel(p, base)
            if p not in seen and _is_scannable(p, rel):
                seen.add(p)
                yield p, rel
    for root in extra_roots:
        if not root:
            continue
        root = Path(root)
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            rel = _rel(p, root)
            if p not in seen and _is_scannable(p, rel):
                seen.add(p)
                yield p, rel


def scan(goal_id: str, extra_roots=(), project_root=None):
    """Return a list of (path, root_relative_path, line_no, line_text)."""
    # Word-ish boundary so g-115-3 does not match . Goal ids end at
    # a non [A-Za-z0-9-] char, so require the next char not continue the id.
    pat = re.compile(re.escape(goal_id) + r"(?![0-9A-Za-z-])")
    hits = []
    for p, rel in _iter_targets(extra_roots, project_root):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if goal_id not in text:
            continue  # cheap pre-filter before per-line regex
        for i, line in enumerate(text.splitlines(), 1):
            if pat.search(line):
                hits.append((p, rel, i, line.strip()[:160]))
    return hits


def _label(p: Path) -> str:
    for base in (PROJECT_ROOT, WORLD_DIR, META_DIR):
        if not base:
            continue
        try:
            return str(Path(p).relative_to(base))
        except ValueError:
            continue
    return str(p)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("goal_id", help="the goal id about to be relocated")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--limit", type=int, default=25,
                    help="max references to print (default 25)")
    ap.add_argument("--root", default=None,
                    help="scan only this root (test/hermetic use); when set, "
                         "the external world/meta dirs are NOT scanned")
    args = ap.parse_args()

    if not args.goal_id.strip():
        print("usage: goal-reference-scan.py <goal-id>", file=sys.stderr)
        return 2

    extra = () if args.root else (WORLD_DIR, META_DIR)
    hits = scan(args.goal_id.strip(), extra_roots=extra,
                project_root=args.root)

    blocking, historical = [], []
    for p, rel, line_no, text in hits:
        bucket = historical if is_historical(p, rel) else blocking
        bucket.append((p, line_no, text))

    by_file = {}
    for p, line_no, text in blocking:
        by_file.setdefault(_label(p), []).append((line_no, text))

    hist_files = {_label(p) for p, _, _ in historical}

    if args.json:
        print(json.dumps({
            "goal_id": args.goal_id,
            "blocking_count": len(blocking),
            "blocking_file_count": len(by_file),
            "historical_count": len(historical),
            "historical_file_count": len(hist_files),
            "files": {f: [{"line": n, "text": t} for n, t in v]
                      for f, v in sorted(by_file.items())},
            "verdict": "referenced" if blocking else "clear",
        }, indent=2))
        return 1 if blocking else 0

    hist_note = (f" ({len(historical)} historical log mention(s) across "
                 f"{len(hist_files)} file(s) ignored — append-only narration)"
                 if historical else "")

    if not blocking:
        print(f"[goal-reference-scan] {args.goal_id}: CLEAR — no live inbound "
              f"references outside the aspirations store; relocation is safe"
              f"{hist_note}")
        return 0

    print(f"[goal-reference-scan] {args.goal_id}: {len(blocking)} live inbound "
          f"reference(s) across {len(by_file)} file(s){hist_note}",
          file=sys.stderr)
    shown = 0
    for f, entries in sorted(by_file.items()):
        for n, t in entries:
            if shown >= args.limit:
                print(f"  ... {len(blocking) - shown} more suppressed "
                      f"(--limit {args.limit})", file=sys.stderr)
                shown = -1
                break
            print(f"  {f}:{n}: {t}", file=sys.stderr)
            shown += 1
        if shown == -1:
            break
    print(f"""
SKIP RELOCATION. Step 2.75c relocates by creating a COPY under a NEW goal id
and completing the original — every reference above still points at
{args.goal_id} and would be orphaned. Archiving one aspiration is not worth
breaking them.

Log the skip and leave the aspiration open. If relocation is genuinely
required, the references must be migrated first (a separate, deliberate goal),
not silently stranded.
""", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
