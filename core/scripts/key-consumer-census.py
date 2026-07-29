#!/usr/bin/env python3
# domain-leak-exempt: --help examples name real framework field keys
# (blocker_ref, last_active) for traceability to the incidents that
# motivated this tool. The logic is key-agnostic.
"""key-consumer-census.py — tabulate every WRITER vs every READER of a field key.

Mechanizes "Rule 1 — census before edit" from the knowledge-tree node
`system/system-constraints-loop/producer-consumer-key-drift.md`.

THE PROBLEM IT SOLVES. When a producer and a consumer disagree on a field
name, the observation ("writer emits `x`, reader reads `y`") is SYMMETRIC — it
does not say which side is deviant. A plan that samples one writer and one
reader picks a side by intuition, and the usual intuition ("the canonical
creation path must be right") is wrong often enough to matter. The tree node's
decision rule is to tabulate EVERY participant and change the MINORITY, which
is nearly always a single file — smaller diff, and it repairs consumers the
plan never enumerated.

Two incidents drove this (gap-029, times_encountered 2/2):

  1. g-115-3348 (zeta) — `known_blockers`. The goal reported writer-vs-reader
     disagreement and prescribed patching the READERS. All three of its signals
     verified verbatim, yet a full census showed the documented schema + the
     second writer + all six readers agreed, and ONE writer (create-blocker.py,
     the canonical CREATE_BLOCKER path, emitting id/created_at/failure_reason
     instead of blocker_id/detected_at/reason) was the sole deviant. Applying
     the prescription would have BROKEN infra-health.py's already-conformant
     blockers. Fixing the one deviant repaired six consumers, two unnamed.

  2. g-115-3642 window (foxtrot, 2026-07-28) — `blocker_ref`. A write of
     `blocker_ref.reason` was silently normalized to `.why` by the validator,
     so a read-back probing the sent key reported a FALSE FAILURE on a write
     that had landed (guard-1720). One census answers "who writes `reason`, who
     reads `why`" in a single pass.

WHY A SCRIPT AND NOT A FORGED SKILL. gap-029 is type=utility. Utility skills
are exercised via direct Bash calls to their companion scripts — a usage mode
invisible to EVERY invocation source, including the skill-invocations ledger
(the Skill tool never fires for a bash call). Measured 2026-07-28: 10 of 12
skill-discovery flags were utility-type and had to be downgraded to advisory,
including `access-roblox-studio` reading 0 invocations while being a core
instrument (meta/skill-discovery-strategy.yaml type_triage.utility,
g-115-2289). Forging a skill here would ship a registry entry that reads
0-invocation forever and re-flags on every discovery cycle, for what is
fundamentally one grep-and-classify pass. So: script.

CLASSIFICATION IS HEURISTIC, AND DELIBERATELY SO. The gap specifies that
"judgement about which side is deviant stays with the agent; only the
enumeration is mechanized." This tool sorts hits into WRITE / READ / MENTION
and never says who is wrong. MENTION is a first-class bucket, not a failure —
docstrings, comments and schema docs land there, and the schema doc is usually
the DECISIVE vote per the tree node.

Shared file-iteration is IMPORTED from goal-reference-scan.py rather than
copied. That module's `is_historical` / `_is_scannable` take a ROOT-RELATIVE
path for a hard-won reason (g-115-3096): matching directory names against an
absolute path tests every ANCESTOR of the repo, so a checkout under a dir named
temp/ or logs/ classifies EVERY file as narration and the scan reports a
vacuous clean result — an always-passes bug that looks exactly like a healthy
repo. Copying the loop would risk re-deriving that subtly wrong.

Usage:
    py -3 core/scripts/key-consumer-census.py blocker_id detected_at reason
    py -3 core/scripts/key-consumer-census.py last_active --json
    py -3 core/scripts/key-consumer-census.py reason why --include-narration
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _stdio import reconfigure_stdio  # type: ignore  # noqa: E402

reconfigure_stdio()

from _paths import PROJECT_ROOT, WORLD_DIR, META_DIR  # type: ignore  # noqa: E402


def _load_ref_scan():
    """Import goal-reference-scan.py (hyphenated, so not a normal import).

    Fail LOUD: this module owns the root-relative classification contract, and
    silently falling back to a local copy would re-introduce exactly the
    g-115-3096 bug this import exists to avoid (communication-clarity rule 5 —
    fail visibly rather than degrade to an inconsistent second source).
    """
    path = SCRIPT_DIR / "goal-reference-scan.py"
    spec = importlib.util.spec_from_file_location("_goal_reference_scan", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_refscan = _load_ref_scan()

# Census scans WIDER than goal-reference-scan: the tree node's Rule 1 grep names
# core/scripts, .claude/skills, mind_api and core/config, and world/scripts holds
# domain wrappers that read the same keys.
_EXTRA_REPO_GLOBS = ("mind_api/**/*",)


def _iter_census_targets(project_root: Path, extra_roots):
    """goal-reference-scan's targets plus mind_api, de-duplicated."""
    seen = set()
    for p, rel in _refscan._iter_targets(extra_roots, project_root=project_root):
        if p not in seen:
            seen.add(p)
            yield p, rel
    for pattern in _EXTRA_REPO_GLOBS:
        for p in project_root.glob(pattern):
            rel = _refscan._rel(p, project_root)
            if p not in seen and _refscan._is_scannable(p, rel):
                seen.add(p)
                yield p, rel


def _write_patterns(key: str):
    k = re.escape(key)
    return [
        re.compile(rf'["\']{k}["\']\s*:'),          # dict/JSON literal  "k": v
        re.compile(rf'^\s*{k}\s*:(?!:)'),           # YAML key at line start
        re.compile(rf'\[\s*["\']{k}["\']\s*\]\s*='),  # d["k"] = v
        re.compile(rf'\.setdefault\(\s*["\']{k}["\']'),
        re.compile(rf'\b{k}\s*=(?!=)'),             # kwarg / assignment
        re.compile(rf'--{k.replace("_", "[-_]")}\b'),  # argparse flag defn
    ]


def _read_patterns(key: str):
    k = re.escape(key)
    return [
        re.compile(rf'\.get\(\s*["\']{k}["\']'),
        re.compile(rf'\[\s*["\']{k}["\']\s*\](?!\s*=)'),   # d["k"] not assigned
        re.compile(rf'["\']{k}["\']\s+in\s+'),             # "k" in rec
        re.compile(rf'--field\s+\S*{k}'),                  # CLI field selector
        re.compile(rf'\.{k}\b'),                           # attr / dotpath read
    ]


def _alias_patterns(key: str):
    """`key` appearing as the VALUE of a string->string mapping.

    An alias/normalization map (`{"reason": "why"}`) is the single most
    important line type in a census: it is where two spellings get reconciled,
    and it explains an otherwise baffling asymmetry. Without this role the
    left-hand key reads as a plain WRITE and the right-hand key — the CANONICAL
    one — is invisible.

    Found by running this tool on its own motivating incident (g-115-3642): the
    `blocker_ref` census showed `reason` with 8 writers and `why` with none,
    while the STORED record demonstrably used `why`. The transform was
    `core/scripts/gates/blocker_ref.py:130  "reason": "why",` — in scope, read,
    and mis-classified. A census that cannot see alias maps answers the easy
    half of the question and silently drops the half that matters.
    """
    k = re.escape(key)
    return [
        re.compile(rf':\s*["\']{k}["\']\s*,?\s*(?:#.*)?$'),   # "old": "KEY",
        re.compile(rf'=>\s*["\']{k}["\']'),                    # "old" => "KEY"
    ]


def classify(line: str, key: str) -> str:
    """ALIAS / WRITE / READ / MENTION for one line.

    ALIAS is checked FIRST and beats WRITE: on `"reason": "why"` the line
    matches the dict-literal WRITE pattern for `reason`, so without this
    ordering the canonical target is reported as a mention at best.

    Otherwise WRITE wins ties. A line can genuinely do both
    (`out["k"] = src.get("k")`); calling that WRITE is the safer default,
    because the census question is "who PRODUCES this spelling" and a missed
    writer is what mis-attributes the deviant side.
    """
    for pat in _alias_patterns(key):
        if pat.search(line):
            return "ALIAS"
    for pat in _write_patterns(key):
        if pat.search(line):
            return "WRITE"
    for pat in _read_patterns(key):
        if pat.search(line):
            return "READ"
    return "MENTION"


def census(keys, project_root=None, include_narration=False, scope=None):
    """Census `keys`, optionally restricted to files that also mention `scope`.

    WHY --scope IS EFFECTIVELY REQUIRED FOR GENERIC KEYS. A census is always
    about a STRUCTURE ("who writes known_blockers' id key"), never about a bare
    word. Measured on the first ground-truth run of this tool (g-115-3642,
    reproducing the g-115-3348 census): `blocker_id detected_at reason` with no
    scope returned 3,781 hits across 732 files, because `reason` is both common
    English and a field on many unrelated structures. The signal the tree node's
    table exists to surface — one deviant writer among ~8 participants — was
    still in there, and completely undetectable. Scoping to files that also
    mention `known_blockers` is what a human doing this by hand does implicitly.

    Left OPTIONAL rather than required because a distinctive key (`blocker_ref`,
    `last_fresh_eyes_run`) needs no scope, and forcing one would add ceremony to
    the easy case. The no-scope path prints a hit-count warning instead.
    """
    base = Path(project_root) if project_root else PROJECT_ROOT
    # HERMETICITY: only reach the live external stores when scanning the REAL
    # project root. An explicit project_root means a caller (a test) has built a
    # controlled tree and expects the scan bounded to it; adding WORLD_DIR /
    # META_DIR anyway made a two-file fixture return 207 files of production
    # world+meta content. Caught by the first run of this file's own tests —
    # the same "tests must not reach live stores" class as guard-955.
    if project_root is not None:
        extra_roots = []
    else:
        extra_roots = [r for r in (WORLD_DIR, META_DIR) if r]
    # Word-ish boundary: `reason` must not match `failure_reason` or `reasons`.
    pats = {k: re.compile(r"(?<![0-9A-Za-z_])" + re.escape(k) + r"(?![0-9A-Za-z_])")
            for k in keys}

    rows = []
    for p, rel in _iter_census_targets(base, extra_roots):
        narration = _refscan.is_historical(p, rel)
        if narration and not include_narration:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if scope and scope not in text:
            continue
        if not any(k in text for k in keys):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for k, pat in pats.items():
                if pat.search(line):
                    rows.append({
                        "file": _refscan._label(p),
                        "line": i,
                        "key": k,
                        "role": classify(line, k),
                        "narration": narration,
                        "text": line.strip()[:160],
                    })
    return rows


def _tabulate(rows, keys):
    """participant -> {key: Counter(role -> site count)} — the census table.

    Counter, not set (g-115-3611). A set answers "does this file write the key
    at all", which is the right unit for the SPELLING question this tool was
    built for: which spelling is the minority, so which one to change. It is
    the wrong unit for the SITE-ENUMERATION question — "have I found every
    place I must edit" — because a file with four write sites and a file with
    one render identically, so a second variant inside an already-classified
    file is invisible in the table.

    That is not a hypothetical. It is the measured mechanism behind hypothesis
    2026-07-28_side-observation-goals-under-enumerate-sites (CONFIRMED): a goal
    named 9 sites, a census found 15, and the miss was NOT a file escaping the
    scan — the file was scanned, classified by the first variant found, and
    never re-examined for others. Counter keeps the per-file multiplicity the
    set discarded, so `WRITE x4` cannot be mistaken for `WRITE`.

    Counter is a dict subclass, so every `"WRITE" in table[f][k]` membership
    test still reads role NAMES and is unaffected. Equality against a set is
    the one thing that changes shape — see the mention-only count in main().
    """
    table = defaultdict(lambda: defaultdict(Counter))
    for r in rows:
        table[r["file"]][r["key"]][r["role"]] += 1
    return table


def _fmt_roles(roles) -> str:
    """`WRITE x4/READ` — role names with their per-file site counts.

    A count of 1 is left bare so the common single-site case reads exactly as
    it did before; only genuine multiplicity adds ink. Tolerates a plain set
    (renders bare names) so a caller holding an older table shape still works.
    """
    if not roles:
        return "-"
    if not hasattr(roles, "get"):
        return "/".join(sorted(roles))
    return "/".join(f"{r} x{roles[r]}" if roles[r] > 1 else r
                    for r in sorted(roles))


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Tabulate every writer vs every reader of one or more field keys.",
        epilog="Enumeration only — deciding WHICH side is deviant stays with the agent.",
    )
    ap.add_argument("keys", nargs="+", help="Field key(s) / alias spellings to census.")
    ap.add_argument("--json", action="store_true", help="Emit raw rows as JSON.")
    ap.add_argument("--include-narration", action="store_true",
                    help="Include append-only logs/telemetry (default: excluded — "
                         "any key that has ever been logged has thousands of them).")
    ap.add_argument("--scope", metavar="TERM",
                    help="Restrict to files that ALSO mention TERM — the structure "
                         "the keys belong to (e.g. --scope known_blockers). Strongly "
                         "recommended for generic keys: an unscoped census of "
                         "'reason' returns thousands of hits and buries the table.")
    ap.add_argument("--project-root", help="Override PROJECT_ROOT (tests).")
    args = ap.parse_args()

    rows = census(args.keys, project_root=args.project_root,
                  include_narration=args.include_narration, scope=args.scope)

    if args.json:
        print(json.dumps({"keys": args.keys, "count": len(rows), "rows": rows}, indent=1))
        return 0

    if not rows:
        print(f"key-consumer-census: no live hits for {args.keys}.")
        print("  NOT proof of absence — narration is excluded by default "
              "(--include-narration), and a key referenced only via a variable "
              "is invisible to a literal scan. Verify before concluding "
              "(verify-before-assuming.md).")
        return 0

    table = _tabulate(rows, args.keys)
    writers = sorted(f for f, ks in table.items()
                     if any("WRITE" in r for r in ks.values()))
    readers = sorted(f for f, ks in table.items()
                     if any("READ" in r for r in ks.values()))

    print(f"=== key-consumer census: {', '.join(args.keys)} ===")
    print(f"{len(rows)} live hits across {len(table)} files "
          f"({len(writers)} with writes, {len(readers)} with reads)\n")

    width = max((len(f) for f in table), default=10)
    width = min(width, 62)
    # Key columns size to their widest CELL, not a fixed 14 (g-115-3611). Adding
    # ` xN` pushed the widest cell from ~18 to ~28 chars, and the fixed pad was
    # already narrower than that. With one key the overflow is invisible (the
    # cell ends the line), but this tool exists to compare TWO spellings, and
    # there an overflowing first column shifts the second — misaligning exactly
    # the side-by-side read the census is for.
    kw = 14
    for f in table:
        for k in args.keys:
            kw = max(kw, len(_fmt_roles(table[f].get(k))))
    kw = min(kw, 34)
    print(f"{'participant':<{width}}  " + "  ".join(f"{k:<{kw}}" for k in args.keys))
    print("-" * (width + 2 + (kw + 2) * len(args.keys)))
    for f in sorted(table):
        cells = []
        for k in args.keys:
            roles = table[f].get(k)
            # `WRITE x4` not `WRITE` — the multiplicity is the whole point when
            # this table is read to enumerate edit sites (g-115-3611). A bare
            # role name renders a 4-site file identically to a 1-site file.
            cells.append(f"{_fmt_roles(roles):<{kw}}")
        print(f"{f[:width]:<{width}}  " + "  ".join(cells))

    # Spelling counts per role — the minority is the change candidate.
    # Reported in BOTH units deliberately: files answers "which spelling is
    # deviant" (change the minority file), sites answers "how much editing is
    # there". They diverge by whatever the per-file multiplicity is, and the
    # gap is exactly what the old set-valued table hid.
    print("\nspelling counts (files / sites):")
    for k in args.keys:
        w = sum(1 for f in table if "WRITE" in table[f].get(k, ()))
        r = sum(1 for f in table if "READ" in table[f].get(k, ()))
        a = sum(1 for f in table if "ALIAS" in table[f].get(k, ()))
        m = sum(1 for f in table if set(table[f].get(k, ())) == {"MENTION"})
        ws = sum(table[f].get(k, {}).get("WRITE", 0) for f in table)
        rs = sum(table[f].get(k, {}).get("READ", 0) for f in table)
        print(f"  {k:<20} writers={w:<3}({ws:<4} sites)  readers={r:<3}({rs:<4} sites)  "
              f"alias-target={a:<3} mention-only={m}")

    alias_files = sorted(f for f in table
                         if any("ALIAS" in roles for roles in table[f].values()))
    if alias_files:
        print("\nALIAS MAP(S) FOUND — read these FIRST; they reconcile two spellings "
              "and explain any writer/reader asymmetry above:")
        for f in alias_files:
            tgt = ", ".join(sorted(k for k in args.keys
                                   if "ALIAS" in table[f].get(k, set())))
            print(f"  {f}  (canonical target: {tgt})")

    print("\nDecision rule (producer-consumer-key-drift.md): the documented "
          "schema is a vote and usually the decisive one. Change the MINORITY "
          "— it is nearly always a single file, and fixing it repairs consumers "
          "the plan never enumerated. Roles here are HEURISTIC; read the cited "
          "lines before editing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
