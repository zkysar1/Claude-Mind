#!/usr/bin/env python3
# domain-leak-exempt: the board-id fixtures below are FORMAT examples describing
# the msg-<date>-<time>-<agent>-<n> shape, never functional identifiers.
"""board-citation-check.py — do the board message ids cited in LIVE surfaces resolve?

WHY THIS EXISTS. A board id written into a live surface that resolves to no post is
the artifact-reference-integrity.md class moved one step EARLIER: a reference to an
artifact that NEVER EXISTED. No purge-side or move-side check can catch it -- there is
no referent to have moved. Only resolution against the board can. Measured 2026-08-10:
30 such citations across 17 files, out of 234 citations in live surfaces.

THE GOAL THAT ASKED FOR THIS (g-115-4405) WAS ITSELF A FALSE POSITIVE, AND THAT IS THE
REASON THIS TOOL GLOBS EVERY CHANNEL. It reported that /fresh-eyes-review invented the
receipt id msg-20260801-042738-alpha-611 and wrote it into a live tree node, "verified
3 ways" as existing on no channel: board-post --reply-to refused it, an id-scan
returned nothing, and a literal string scan matched only a later correction.

The message exists. It is on reasoning.jsonl, authored by alpha at 2026-08-01T04:27:38
-- the exact timestamp the id encodes. Every one of the three probes had searched
`findings` and `coordination`, against a board that carries EIGHT channels. Worse, the
detail offered as the clinching evidence -- "the suffix was off-pattern for its channel,
-611 against findings -55xx" -- is the reasoning-channel counter, i.e. confirmation the
id was genuine and correctly formed. The single strongest stated argument for
fabrication was, read correctly, proof of authenticity.

The lesson is built into this tool's shape: a channel-incomplete resolver does not
report "unknown", it reports a confident, specific, wrong "this never existed" -- and
that output is indistinguishable from a true finding. So load_board_ids() globs
board/*.jsonl with no channel allowlist, and an empty load REFUSES to interpret rather
than rendering every correct citation as dangling.

WHY REPORT-ONLY, AND WHY IT DOES NOT AUTO-FIX. A dangling citation has at least three
causes and they want opposite responses, so collapsing them into one verdict would be
worse than the defect:

  dangling      the cited post does not exist -- the g-115-4405 case. Fix the citation.
  example       a FORMAT illustration inside a schema block, e.g. the `"id": "msg-..."`
                line in core/config/conventions/board.md. Correct as written; the id is
                a shape, not a claim. Reported separately, never counted as dangling.
  peer          a citation to a PEER DEPLOYMENT's post. This world's board is not the
                store of record for those (core/config/conventions/cross-deployment-channel.md
                -- the channel has been live since 2026-06-02), so non-resolution here
                is EXPECTED and is not evidence of fabrication.

The tool classifies, prints the citing line, and stops. A human or an agent decides.
Auto-rewriting a citation whose referent cannot be located would invent a second
fabrication on top of the first.

USE vs MENTION is the trap that makes a naive version of this tool useless. The stores
that RECORD incidents -- world/aspirations.jsonl, the board itself, the reasoning bank,
changelog -- legitimately quote nonexistent ids while reporting them. g-115-4405's own
description quotes the bogus id verbatim. So those stores are OUT of scope by
construction: this scans only surfaces where a citation asserts a currently-true fact.

Exemption: a file carrying `board-citation-exempt:` anywhere is skipped entirely, for
files whose whole purpose is to document the id format.

Usage:
  py -3 core/scripts/board-citation-check.py [--json] [--exit-on-hits] [--root <dir>]
Exit: 0 always, unless --exit-on-hits and >=1 DANGLING citation was found (then 1).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import PROJECT_ROOT, WORLD_DIR  # noqa: E402

# msg-YYYYMMDD-HHMMSS-<agent>-<counter>, where <agent> may itself be hyphenated
# (meta-tiebreaker is a real fleet name).
#
# The lazy quantifier is NOT load-bearing and this comment used to claim it was
# ("non-greedy so a hyphenated agent cannot swallow the counter"). Measured by
# mutation: swapping in the greedy form `[A-Za-z0-9_-]+-\d+` broke NO test and
# changes no real match, because backtracking lands both forms on the same split
# for every id this board actually contains. They diverge only on a
# double-counter shape (`...-alpha-611-999`) that board-post.sh never mints.
# Kept as-is because it is correct and costs nothing; recorded as non-load-bearing
# so nobody defends it as a safety property it does not provide.
BOARD_ID = re.compile(r"msg-\d{8}-\d{6}-[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*?-\d+")

EXEMPT_MARKER = "board-citation-exempt:"

# A schema/format illustration rather than a citation: the id is the VALUE of an
# "id" key in a documented record shape. Deliberately anchored to that exact
# construction rather than a loose "looks like documentation" heuristic -- a
# relaxed predicate here silently stops reporting real citations (guard-2860).
EXAMPLE_LINE = re.compile(r'["\']id["\']\s*:\s*["\']msg-')

SKIP_DIR_PARTS = {".history", "archive", "drained", "__pycache__", ".git", ".graveyard"}
SCAN_SUFFIXES = {".md", ".yaml", ".yml"}


def live_surfaces(root: Path, world: Path) -> list[Path]:
    """Surfaces where a citation asserts a currently-true fact.

    Deliberately EXCLUDES the incident-recording stores (aspirations, board,
    reasoning bank, changelog): those quote nonexistent ids as part of reporting
    them, so scanning them would flag the reports rather than the defect.
    """
    return [
        world / "knowledge" / "tree",
        world / "conventions",
        root / ".claude" / "skills",
        root / ".claude" / "rules",
        root / "core" / "config",
    ]


def load_board_ids(world: Path) -> set[str]:
    """Every id on this world's board.

    board JSONL is ONE OBJECT PER LINE, never a JSON array (guard on board.py
    output shape) -- a whole-file json.load here returns nothing and every
    citation would read as dangling, which is the exact false-mass-negative this
    tool must not produce.
    """
    ids: set[str] = set()
    board = world / "board"
    if not board.is_dir():
        return ids
    for f in sorted(board.glob("*.jsonl")):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                mid = json.loads(line).get("id")
            except (ValueError, AttributeError):
                continue
            if mid:
                ids.add(mid)
    return ids


def peer_env_ids(root: Path) -> set[str]:
    """Environment ids of known PEER deployments, from the registry.

    Used only to explain a non-resolution, never to suppress one silently.
    """
    envs: set[str] = set()
    d = root / "core" / "config" / "environments"
    if d.is_dir():
        for f in d.glob("*.yaml"):
            envs.add(f.stem)
    return envs


def scan(root: Path, world: Path) -> dict:
    real = load_board_ids(world)
    findings: list[dict] = []
    scanned = cited_total = 0

    for surface in live_surfaces(root, world):
        if not surface.exists():
            continue
        for f in sorted(surface.rglob("*")):
            if not f.is_file() or f.suffix not in SCAN_SUFFIXES:
                continue
            if SKIP_DIR_PARTS & set(f.parts):
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if EXEMPT_MARKER in text:
                continue
            scanned += 1
            for lineno, line in enumerate(text.splitlines(), 1):
                for mid in BOARD_ID.findall(line):
                    cited_total += 1
                    if mid in real:
                        continue
                    kind = "example" if EXAMPLE_LINE.search(line) else "dangling"
                    findings.append({
                        "id": mid,
                        "kind": kind,
                        "file": str(f.relative_to(root)) if root in f.parents or f.is_relative_to(root)
                                else str(f),
                        "line": lineno,
                        "text": line.strip()[:200],
                    })

    dangling = [f for f in findings if f["kind"] == "dangling"]
    return {
        "board_ids_known": len(real),
        "files_scanned": scanned,
        "citations_seen": cited_total,
        "unresolved_total": len(findings),
        "dangling": dangling,
        "examples": [f for f in findings if f["kind"] == "example"],
        "peer_envs_known": sorted(peer_env_ids(root)),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Resolve board-id citations in live surfaces.")
    ap.add_argument("--json", action="store_true", help="emit the full result as JSON")
    ap.add_argument("--exit-on-hits", action="store_true",
                    help="exit 1 when >=1 DANGLING citation is found (gate use)")
    ap.add_argument("--root", default=None, help="project root override (tests)")
    ap.add_argument("--world", default=None, help="world dir override (tests)")
    args = ap.parse_args()

    root = Path(args.root).resolve() if args.root else PROJECT_ROOT
    world = Path(args.world).resolve() if args.world else WORLD_DIR

    result = scan(root, world)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        # A zero is only meaningful next to the population it was drawn from, so
        # print both -- "0 dangling" beside "0 citations seen" is a broken scan,
        # not a clean surface (guard-2421 / guard-2273).
        print(f"[board-citation-check] board ids known: {result['board_ids_known']} | "
              f"files scanned: {result['files_scanned']} | "
              f"citations seen: {result['citations_seen']}")
        if not result["board_ids_known"]:
            print("[board-citation-check] WARN: zero board ids loaded -- every citation "
                  "would read as dangling. Refusing to interpret this run.", file=sys.stderr)
            return 0
        for f in result["dangling"]:
            print(f"  DANGLING {f['id']}  {f['file']}:{f['line']}")
            print(f"           {f['text']}")
        for f in result["examples"]:
            print(f"  example  {f['id']}  {f['file']}:{f['line']} (format illustration, not a claim)")
        print(f"[board-citation-check] DANGLING: {len(result['dangling'])} | "
              f"format examples: {len(result['examples'])}")
        if result["dangling"]:
            print("[board-citation-check] NOTE: a citation to a PEER deployment's post "
                  "(see conventions/cross-deployment-channel.md) will not resolve against "
                  "this world's board and is EXPECTED -- classify before editing.")

    if args.exit_on_hits and result["dangling"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
