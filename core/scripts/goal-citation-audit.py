#!/usr/bin/env python3
"""goal-citation-audit.py — do the files a goal CITES actually contain the
tokens it cites them for, and have they changed since the goal was written?
(gap-119, forged from g-115-5923.)

WHY THIS EXISTS. A goal description is read as evidence. It names paths and
asserts what is in them, and whoever executes the goal inherits those claims as
scope. When a cited path is wrong, the claim is not merely unhelpful — it can
make a verification check UNSATISFIABLE BY CONSTRUCTION. Motivating case
(g-115-3652): the description named a target file for a block that `git -S`
proves was never in that file in any commit, so one of its two verification
checks returned 0 whether the edit was perfect or catastrophic. A check that
cannot fail is not a check.

TWO QUESTIONS ON ONE PARSE. Both halves need the same expensive step — pulling
file references out of prose — and are cheap once it exists:

  SPATIAL  (gap-119 as registered): does the cited path EXIST, and does it
           CONTAIN the token it is cited for? Catches a citation that was
           NEVER true.
  TEMPORAL (second encounter, echo 2026-08-11 / g-115-3979): has the cited path
           CHANGED since the goal was written? Catches a citation that WAS true
           and STOPPED being true. Measured case: g-115-4249 exempted a sentinel
           as self-healing at 2026-07-31T10:08:41 with a correct stated reason;
           commit 622bff0d5 added a second writer to that slot at
           2026-08-02T16:11:27. The exemption was false 54h later and sat in a
           pending goal reading as current. The SPATIAL probe misses this
           entirely — every path it cites is real and contains exactly what it
           is cited for. What changed was elsewhere.

THE UPPER-BOUND TRAP, STATED HERE BECAUSE IT IS THE MAIN WAY THIS TOOL LIES.
A changed file is NOT a falsified claim. Most edits are irrelevant to the
sentence citing them, so the raw TEMPORAL rate is an UPPER BOUND and will look
alarming. This script therefore refuses to emit a headline rate without also
printing a hand-read sample, and labels every temporal hit `needs-hand-read`
rather than `stale`. A vacuous alarm is the mirror image of a vacuous zero
(rb-245): both are authoritative-looking numbers that nobody can act on.

THE JUDGMENT HALF IS NOT HERE, DELIBERATELY. What to DO about a wrong or stale
citation stays with the executor and is already encoded — rb-7333 (spatial) and
rb-7538 (an audit's per-item verdict expires when a new writer is added to the
item it classified). This script REPORTS; it never edits a goal.

PRIOR ART REUSED, NOT DUPLICATED. `tree-code-ref-drift.py` asks the SPATIAL
question one surface over (tree nodes citing product-repo code) and carries
hard-won false-positive suppression: a symbol stoplist, a minimum token length,
and an occurrence ceiling. Those are imported from it rather than retyped, so a
fix there reaches here. This is the FIFTH surface in that family; the others are
verify-learning SKILL.md -> store records
(`check-verify-learning-citation-drift.py`), durable stores -> temp/ paths
(`temp-citation-ratchet.py`), and reasoning-bank capability claims (g-115-4687,
open). Note the direction: `goal-reference-scan.py` is the INVERSE of this
script — it finds references INBOUND TO a goal id; this one audits references
OUTBOUND FROM a goal description.

MEASURED COVERAGE LIMIT — READ BEFORE TRUSTING A CLEAN TEMPORAL RESULT. The
temporal half needs a git commit date, so it covers ONLY git-tracked paths
(`core/`, `.claude/`, `mind_api/`, `CLAUDE.md`). `world/` and `meta/` are
EXTERNAL and gitignored in this deployment, so a citation into them gets
temporal verdict `no-history` — NOT `unchanged`. Those two roots hold the
conventions and strategy files that goals cite most, so a "0 stale" headline
computed over all citations would be badly wrong. The reported temporal rate is
therefore over the COVERED subset only, and the uncovered count is printed
beside it. A zero whose denominator you did not read is not a clean result
(guard-1665).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import PROJECT_ROOT, WORLD_DIR, META_DIR  # noqa: E402
from _runtime_bash import bash_cmd  # noqa: E402  (guard-580/581)


# --- prior-art reuse (single source of truth for the FP heuristics) ---------
def _load_prior_art():
    """Import the hyphenated sibling so its FP heuristics are shared, not
    retyped. Fail-open: if it moves or breaks, this script still runs with
    empty/default heuristics rather than dying — a citation audit that refuses
    to start because a sibling moved is worse than one with a smaller stoplist.
    """
    p = Path(__file__).resolve().parent / "tree-code-ref-drift.py"
    try:
        spec = importlib.util.spec_from_file_location("_tcrd", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as exc:  # noqa: BLE001
        print(f"[goal-citation-audit] NOTE: prior-art import failed ({exc}); "
              f"using local defaults for FP heuristics", file=sys.stderr)
        return None


_PA = _load_prior_art()
SYMBOL_STOPLIST = set(getattr(_PA, "SYMBOL_STOPLIST", set())) | {
    # Additions specific to GOAL prose (the prior art's surface is tree nodes).
    "goal", "goals", "agent", "agents", "world", "meta", "core", "scripts",
    "pending", "completed", "blocked", "recurring", "worker", "reducer",
}
MIN_SYMBOL_LEN = getattr(_PA, "MIN_SYMBOL_LEN", 6)
MAX_OCCURRENCES = getattr(_PA, "MAX_OCCURRENCES", 20)

EXEMPT_MARKER = "citation-audit-exempt"

# A path citation in goal prose. Two shapes, because goals cite both:
#   bare path      core/scripts/foo.py        world/conventions/bar.md
#   path with line AyoOperator/Foo.java:1354
# Anchored on a directory separator so bare words are not mistaken for paths.
#
# The bare-root alternation is NOT cosmetic and was added after this script
# FAILED ITS OWN POSITIVE CONTROL. The `(dir/)+` form alone cannot match
# `CLAUDE.md`, which has no directory separator — so the very citation that
# motivated gap-119 ( attributing a blockquote to CLAUDE.md when it
# lives in `.claude/rules/run-full-suite-after-deep-code.md`) was invisible to
# the tool built to catch it. A filter that cannot match its own founding case
# reports a clean sweep forever (guard-1665). Run the control before believing
# any zero this script prints.
PATH_RE = re.compile(
    r"(?P<path>(?:(?:[A-Za-z0-9_.\-]+/)+[A-Za-z0-9_.\-]+\."
    r"(?:py|sh|md|ya?ml|jsonl?|java|lua|ts|js|toml|cfg|ini|txt))"
    r"|(?:CLAUDE\.md))"
    r"(?::(?P<line>\d+))?"
)
# Root-level files goals legitimately cite without a directory prefix.
BARE_ROOT_FILES = ("CLAUDE.md",)

# Roots that ARE this repo. A path under none of these is in some OTHER repo
# (the fleet works across ~40 sibling product repos) and CANNOT be checked from
# here — see FOREIGN handling in resolve_cited_path.
MIND_PREFIXES = ("core/", ".claude/", "mind_api/", "agents/", "world/", "meta/",
                 "CLAUDE.md")

# A shell variable standing where a path segment would be: "$WORLD_PATH/x.py",
# "${META_DIR}/y.yaml". The regex below matches from the NAME, so the sigil sits
# just before the match. Caught on the very first self-test of this script,
# which reported `$WORLD_PATH/scripts/family-sweep.py` as a missing path — it is
# not a path at all, it is an expression that RESOLVES to one at runtime.
SHELL_SIGILS = ("$", "{")
# Same shape without a sigil: goals also write bare WORLD_PATH/... in prose.
# All-caps-with-underscores is the shell-variable naming convention and is never
# a real directory name in this tree.
SHELLVAR_SEG_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")

SYMBOL_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_.\-]*(?:\(\))?)`")

# Goals assert file CONTENT far more often with quote marks than with backticks
# ("CLAUDE.md's section carries a blockquote 'RE-BASELINED 2026-07-26 ...'"),
# and a quoted phrase is the strongest possible citation: it claims those exact
# characters are in that file. Backticks alone missed the founding case.
# Bounded 12..160 chars — shorter is prose, longer is usually a paraphrase that
# was never meant as a verbatim quote.
# The `(?<![A-Za-z0-9])` lookbehind on the single-quote arm is load-bearing:
# without it the apostrophe in a possessive ("CLAUDE.md's section") opens a
# match that runs to the NEXT quote, yielding a garbage token like
# "s run-full-suite-after-deep-code section carries a blockquote". Measured on
# the founding case — the VERDICT was right and the EVIDENCE was unreadable,
# which is its own failure: a finding nobody can check is a finding nobody acts
# on. An apostrophe preceded by an alphanumeric is possessive/contraction, never
# an opening quote.
QUOTE_RE = re.compile(
    r"(?<![A-Za-z0-9])'([^'\n]{12,160})'"
    r"|\"([^\"\n]{12,160})\""
)

# How much prose around a path mention is scanned for the tokens it is cited
# for. Mirrors the prior art's PROSE_WINDOW rationale: a symbol far from the
# path reference is usually about something else.
PROSE_WINDOW = getattr(_PA, "PROSE_WINDOW", 220)

# Roots whose citations can be temporally checked (git-tracked). See the
# MEASURED COVERAGE LIMIT note in the module docstring.
GIT_TRACKED_PREFIXES = ("core/", ".claude/", "mind_api/", "CLAUDE.md")


EPHEMERAL_RE = re.compile(r"^agents/[^/]+/(temp|reports|sessions)/")


def is_ephemeral_root(cited: str) -> bool:
    """Paths whose ABSENCE is expected rather than defective.

    `agents/*/temp/` is the sanctioned scratch store and is drained on a
    cadence; `agents/*/reports/` was abolished outright by the file-model
    normalization; `agents/*/sessions/<sid>/` is per-session scratch that
    cleanup-stale-bindings removes. A goal citing any of them is not
    mis-citing — its referent was legitimately deleted after the goal was
    written.
    """
    return bool(EPHEMERAL_RE.match(cited.strip()))


def resolve_cited_path(cited: str) -> tuple[Path | None, str]:
    """Map a citation string to a real filesystem path.

    Returns (path_or_None, root_class) where root_class is one of
    'git', 'external-world', 'external-meta', 'foreign'.

    The external roots matter: they resolve fine for the SPATIAL check but
    carry no git history, so the TEMPORAL check must report `no-history`
    rather than silently counting them as unchanged.

    FOREIGN is the load-bearing class and the reason this function is not a
    one-liner. The fleet works across ~40 sibling product repos, and goals
    routinely cite paths inside them (`AyoOperator/Tasks/Foo.java`). Those
    files are genuinely NOT in this tree, so joining them to PROJECT_ROOT and
    reporting MISSING-PATH is a false alarm — and it would fire on essentially
    every product goal, which is how an audit teaches its readers to ignore it.
    'Cannot be checked from here' and 'the citation is wrong' are different
    findings and must not share a verdict.
    """
    c = cited.strip()
    if c.startswith("world/"):
        return (Path(WORLD_DIR) / c[len("world/"):], "external-world")
    if c.startswith("meta/"):
        return (Path(META_DIR) / c[len("meta/"):], "external-meta")
    if c.startswith(GIT_TRACKED_PREFIXES):
        return (Path(PROJECT_ROOT) / c, "git")
    if c.startswith(MIND_PREFIXES):
        return (Path(PROJECT_ROOT) / c, "git")
    return (None, "foreign")


def git_last_commit_iso(rel: str) -> str | None:
    """Last commit date for a repo-relative path, or None when untracked."""
    try:
        out = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "log", "-1", "--format=%cI", "--", rel],
            capture_output=True, text=True, timeout=30,
        )
        v = (out.stdout or "").strip()
        return v or None
    except Exception:  # noqa: BLE001
        return None


def parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    t = str(s).strip().replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(t)
        return d.replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        return None


def cited_symbols(text: str, start: int, end: int) -> list[str]:
    """Tokens near a path mention — what the path is cited FOR.

    TWO extractors, because they catch different citation styles and the
    backtick-only version missed this tool's own founding case:
      backticked identifiers  -> `some_function`, `SOME_CONST`
      quoted phrases          -> 'RE-BASELINED 2026-07-26 (g-115-3085 ...)'
    A quoted phrase is the stronger signal: it asserts those exact characters
    are in that file, so its absence is a hard finding rather than a hint.
    """
    lo = max(0, start - PROSE_WINDOW)
    hi = min(len(text), end + PROSE_WINDOW)
    window = text[lo:hi]
    out = []

    for m in SYMBOL_RE.finditer(window):
        sym = m.group(1).strip().rstrip("()")
        if len(sym) < MIN_SYMBOL_LEN:
            continue
        if sym.lower() in SYMBOL_STOPLIST:
            continue
        if PATH_RE.fullmatch(sym) or "/" in sym:
            continue  # that is a path, not a symbol
        if sym not in out:
            out.append(sym)

    for m in QUOTE_RE.finditer(window):
        phrase = (m.group(1) or m.group(2) or "").strip()
        if not phrase:
            continue
        # A quoted path is a citation, not content — already handled above.
        if PATH_RE.fullmatch(phrase):
            continue
        if phrase not in out:
            out.append(phrase)

    return out


def audit_goal(goal: dict) -> dict:
    gid = goal.get("id")
    text = (goal.get("description") or "")
    created = parse_iso(goal.get("created_at") or "")
    rec = {
        "goal_id": gid,
        "created_at": goal.get("created_at"),
        "status": goal.get("status"),
        "citations": [],
        "exempt": EXEMPT_MARKER in text,
    }
    if rec["exempt"]:
        return rec

    seen = set()
    skipped_shellvar = 0
    for m in PATH_RE.finditer(text):
        cited = m.group("path")

        # Shell expressions are not paths. Two forms, both measured live on the
        # first self-test of this script: a sigil immediately before the match
        # ("$WORLD_PATH/x"), and a bare all-caps first segment in prose
        # (WORLD_PATH/x). Skipping is correct rather than conservative — the
        # runtime value is unknown here, so there is nothing to verify.
        prev = text[m.start() - 1] if m.start() > 0 else ""
        first_seg = cited.split("/", 1)[0]
        if prev in SHELL_SIGILS or SHELLVAR_SEG_RE.match(first_seg):
            skipped_shellvar += 1
            continue

        if cited in seen:
            continue
        seen.add(cited)
        path, root_class = resolve_cited_path(cited)
        exists = bool(path and path.exists())

        if root_class == "foreign":
            spatial = "foreign-repo(not-checkable-here)"
        elif exists:
            spatial = "ok"
        elif is_ephemeral_root(cited):
            # EXPECTED DECAY, NOT A DEFECT. `agents/*/temp/` is drained by
            # design (temp-store.md) and `agents/*/reports/` was abolished by
            # the file-model normalization, so a citation into either is a
            # pointer whose referent was legitimately removed. Measured on the
            # first full sweep: these dominated the raw MISSING-PATH count and
            # would have buried the citations that were never true. The
            # sanctioned remedy for this class is FOLDING (dissolve the
            # pointer, inline the detail) per artifact-reference-integrity.md
            # — not a correction of the path. `temp-citation-ratchet.py` counts
            # the same class in the DURABLE stores; this is that surface's
            # goal-description sibling, so keep the two verdicts distinct.
            spatial = "MISSING-PATH(ephemeral-root,expected)"
        else:
            spatial = "MISSING-PATH"

        entry = {
            "cited": cited,
            "line": m.group("line"),
            "root_class": root_class,
            "exists": exists,
            "spatial": spatial,
            "symbols_checked": [],
            "symbols_absent": [],
            "temporal": "not-evaluated",
            "last_commit": None,
        }

        # --- SPATIAL: does the file contain the tokens it is cited for? ---
        if exists:
            try:
                body = path.read_text(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                body = ""
            syms = cited_symbols(text, m.start(), m.end())
            for s in syms:
                # A token appearing everywhere locates nothing (prior art's
                # MAX_OCCURRENCES rationale) — skip rather than false-clear.
                if body.count(s) > MAX_OCCURRENCES:
                    continue
                entry["symbols_checked"].append(s)
                if s not in body:
                    entry["symbols_absent"].append(s)
            if entry["symbols_absent"]:
                entry["spatial"] = "TOKEN-ABSENT"

        # --- TEMPORAL: changed since the goal was written? ---
        if root_class == "git" and exists:
            iso = git_last_commit_iso(cited)
            entry["last_commit"] = iso
            lc = parse_iso(iso or "")
            if lc is None:
                entry["temporal"] = "no-history"
            elif created is None:
                entry["temporal"] = "no-goal-date"
            elif lc > created:
                entry["temporal"] = "CHANGED-SINCE(needs-hand-read)"
            else:
                entry["temporal"] = "unchanged"
        else:
            # External/gitignored roots carry no commit history. This is
            # `no-history`, NOT `unchanged` — see the coverage limit above.
            entry["temporal"] = "no-history"

        rec["citations"].append(entry)
    rec["skipped_shellvar"] = skipped_shellvar
    return rec


def load_goals(source: str, goal_ids: list[str] | None) -> list[dict]:
    """Read goals via the aspirations reader (never a raw JSONL read)."""
    # bash_cmd, never a bare "bash" argv[0]: that resolves via CreateProcess,
    # which searches System32 BEFORE PATH on win32 and reaches the WSL launcher,
    # where it can hang forever (guard-580). It also passes the script path as
    # .as_posix(), because bash silently strips the backslashes of a
    # str(WindowsPath) and then fails on a nonexistent path (guard-581).
    cmd = bash_cmd(Path(PROJECT_ROOT) / "core/scripts/aspirations-read.sh",
                   "--source", source, "--active")
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                         cwd=str(PROJECT_ROOT))
    raw = out.stdout or ""
    i = raw.find("[")
    j = raw.find("{")
    k = min(x for x in (i, j) if x >= 0) if (i >= 0 or j >= 0) else -1
    if k < 0:
        return []
    data, _ = json.JSONDecoder().raw_decode(raw[k:])
    asps = data if isinstance(data, list) else data.get("aspirations", [])
    goals = []
    for a in asps:
        for g in a.get("goals", []):
            if goal_ids and g.get("id") not in goal_ids:
                continue
            goals.append(g)
    return goals


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Audit the file citations in goal descriptions "
                    "(spatial: does the path contain what it is cited for; "
                    "temporal: has it changed since the goal was written).")
    ap.add_argument("goal_ids", nargs="*", help="goal ids; omit for --all")
    ap.add_argument("--all", action="store_true", help="audit every active goal")
    ap.add_argument("--source", default="world", choices=("world", "agent"))
    ap.add_argument("--json", action="store_true", help="emit JSON")
    ap.add_argument("--sample", type=int, default=3,
                    help="hand-read sample size printed with any rate (min 1)")
    ap.add_argument("--exit-on-hits", action="store_true",
                    help="exit 1 when any SPATIAL hit is found (temporal hits "
                         "never set a non-zero exit — they are an upper bound)")
    args = ap.parse_args()

    if not args.all and not args.goal_ids:
        ap.error("give goal ids or --all")

    goals = load_goals(args.source, args.goal_ids or None)
    if args.goal_ids:
        missing = set(args.goal_ids) - {g.get("id") for g in goals}
        for gid in sorted(missing):
            print(f"[goal-citation-audit] NOT FOUND in active {args.source} "
                  f"queue: {gid}", file=sys.stderr)

    results = [audit_goal(g) for g in goals]

    goals_with_citations = [r for r in results if r["citations"]]
    all_cites = [c for r in goals_with_citations for c in r["citations"]]
    missing_path = [c for c in all_cites if c["spatial"] == "MISSING-PATH"]
    token_absent = [c for c in all_cites if c["spatial"] == "TOKEN-ABSENT"]
    foreign = [c for c in all_cites
               if c["spatial"] == "foreign-repo(not-checkable-here)"]
    ephemeral = [c for c in all_cites
                 if c["spatial"] == "MISSING-PATH(ephemeral-root,expected)"]
    shellvar_skipped = sum(r.get("skipped_shellvar", 0) for r in results)
    covered = [c for c in all_cites if c["temporal"] in
               ("unchanged", "CHANGED-SINCE(needs-hand-read)")]
    changed = [c for c in all_cites
               if c["temporal"] == "CHANGED-SINCE(needs-hand-read)"]
    uncovered = [c for c in all_cites if c["temporal"] == "no-history"]

    if args.json:
        print(json.dumps({
            "goals_scanned": len(results),
            "goals_with_citations": len(goals_with_citations),
            "citations_total": len(all_cites),
            "spatial_missing_path": len(missing_path),
            "spatial_token_absent": len(token_absent),
            "spatial_foreign_not_checkable": len(foreign),
            "skipped_shell_expressions": shellvar_skipped,
            "temporal_covered": len(covered),
            "temporal_changed_since": len(changed),
            "temporal_uncovered_no_history": len(uncovered),
            "results": results,
        }, indent=1))
        return 1 if (args.exit_on_hits and (missing_path or token_absent)) else 0

    print(f"goals scanned            {len(results)}")
    print(f"  with >=1 citation      {len(goals_with_citations)}")
    print(f"citations found          {len(all_cites)}")
    print()
    print("SPATIAL (a hit here is a citation that was NEVER true)")
    print(f"  cited path MISSING     {len(missing_path)}   <-- actionable")
    print(f"  token ABSENT from file {len(token_absent)}   <-- actionable")
    print(f"  missing but EPHEMERAL root (drained by design, expected): "
          f"{len(ephemeral)}")
    print(f"  foreign repo, NOT checkable from here: {len(foreign)}")
    print(f"  shell expressions skipped (not paths):  {shellvar_skipped}")
    print("  ^ neither of the last two is a defect. A path in a sibling product")
    print("    repo cannot be verified from this tree, and \"$WORLD_PATH/x\" is")
    print("    an expression, not a citation. Counting either as MISSING would")
    print("    fire on nearly every product goal and train readers to ignore it.")
    print()
    print("TEMPORAL (an UPPER BOUND — a changed file is not a falsified claim)")
    if covered:
        pct = 100.0 * len(changed) / len(covered)
        print(f"  changed since written  {len(changed)} of {len(covered)} "
              f"covered ({pct:.1f}%)")
    else:
        print("  changed since written  0 of 0 covered  <-- NOTHING WAS COVERED")
    print(f"  NOT covered (no git history, external/gitignored roots): "
          f"{len(uncovered)}")
    print("  ^ these are `no-history`, NOT `unchanged`. world/ and meta/ are")
    print("    external here, and goals cite them heavily — do not read the")
    print("    rate above as fleet-wide.")

    # The mandated hand-read sample: never print a rate without specimens.
    # Dedupe by identity: one citation can be BOTH token-absent and changed,
    # and printing it once per category reads as two separate findings.
    sample_pool = []
    for c in (missing_path + token_absent + changed):
        if not any(c is s for s in sample_pool):
            sample_pool.append(c)
    if sample_pool:
        n = max(1, args.sample)
        print(f"\nHAND-READ SAMPLE (verify before quoting any rate above):")
        for c in sample_pool[:n]:
            owner = next((r["goal_id"] for r in results if c in r["citations"]),
                         "?")
            print(f"  {owner}  {c['cited']}")
            print(f"     spatial={c['spatial']}  temporal={c['temporal']}")
            if c["symbols_absent"]:
                print(f"     tokens absent: {', '.join(c['symbols_absent'][:5])}")

    return 1 if (args.exit_on_hits and (missing_path or token_absent)) else 0


if __name__ == "__main__":
    sys.exit(main())
