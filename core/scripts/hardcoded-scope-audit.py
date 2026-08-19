#!/usr/bin/env python3
"""hardcoded-scope-audit.py — detective layer for hardcoded-scope vacuity.

Finds absolute-path literals in the documentation/script corpus that DO NOT
RESOLVE on the running box, and ranks how likely each one is to be a real
vacuity site.

THE CLASS (g-115-4041, g-115-4170, g-115-4184): a mandatory step whose scope is
a hand-written absolute root. Where that root does not exist, the step iterates
an EMPTY SET, reports success, and does so for months. Two of the three known
instances failed silently; the third failed loudly and was caught in minutes.
Same root cause, opposite detectability — which is why a detector is needed for
the silent half.

A NON-RESOLVING LITERAL IS A CANDIDATE, NOT A VERDICT (guard-1561). This script
FILES NOTHING and asserts nothing. It ranks and explains, and a human or the
executing agent judges.

THE DISCRIMINATOR — ask what ABSENCE PRODUCES, not whether the path resolves:

  * SCOPE ROOT that a step iterates  -> absence yields a silent empty set and a
    success report. THIS is the vacuity class.
  * PRESENCE MARKER in a gate/predicate -> absence yields an explicit False, and
    the gate does its job. Working as designed, NOT a defect.

The second category is not hypothetical and is why a naive "non-resolving = bad"
scan is worse than useless here: this corpus documents a box-affinity pattern
whose entire mechanism is a marker path that MUST NOT resolve on the box reading
it. A scanner without this distinction would rank the healthiest construct in the
corpus as its top defect, which is how detectors get switched off.

ANTI-VACUITY (the detector must not inherit the defect it detects): it reports
the number of files it scanned, and a scan that reaches ZERO files exits 2 with
verdict CANNOT_CHECK rather than reporting a clean corpus.

Usage:
  hardcoded-scope-audit.py                       # default corpus
  hardcoded-scope-audit.py --root <dir> [...]    # explicit roots (testing)
  hardcoded-scope-audit.py --tier active-scope   # filter the report
  hardcoded-scope-audit.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# Absolute-root prefixes worth testing. Deliberately an allowlist: a bare
# `/v1/status` or `/tasks/execute` is an API route, not a filesystem root, and
# matching those would bury the real hits under endpoint strings.
_POSIX_ROOTS = "opt|home|usr|var|mnt|media|Users|srv|etc|srv|data|workspace"
LITERAL_RX = re.compile(
    r'(?<![A-Za-z0-9_.])('
    r'[A-Za-z]:[\\/][^\s`"\'<>|)*\],;]+'          # drive-letter form
    r'|/(?:' + _POSIX_ROOTS + r')/[^\s`"\'<>|)*\],;]+'  # posix-rooted form
    r')'
)

# --- context signals. Each is a WEAK signal; the tier is their combination. ---
# Absence produces a silent empty set -> the vacuity class.
SCOPE_HINTS = (
    "for each", "for every", "iterate", "enumerate", "all repos", "each repo",
    "every repo", "scope root", "scope is", "sweep", "walk ", "under this root",
    "in this directory", "loop over", "list all", "find all",
)
# Shell/Python iteration shapes. Applied only to script files, where the literal
# is typically a CONSTANT on one line and the dereference is on the next — so a
# prose phrase like "for each" never appears. Without these the script variant
# (the instance-3 shape) scores zero iteration signal and lands unclassified.
SCRIPT_SCOPE_RX = re.compile(
    r'(for\s+\w+\s+in\s|\bglob\b|\brglob\b|\blistdir\b|\bfind\s+"?\$|\bls\s+"?\$|/\*|\biterdir\b|\bwalk\b)'
)
# A bare `scope`/`scan` reads as a hint in meta-discussion ABOUT scanning, which
# this corpus is full of — including the section headings of documents that
# discuss this very defect. Kept out of SCOPE_HINTS deliberately.
# Absence produces an explicit False -> a gate doing its job, NOT a defect.
MARKER_HINTS = (
    "file_check", "precondition", "marker", "condition", "exists", "affinity",
    "passed=", "passed =", "gate", "filter", "min_count", "must not resolve",
    "cannot exist", "absent elsewhere", "present iff", "box-gated",
    # CANDIDATE LISTS are the same shape as a gate and were the largest
    # false-positive family on the first full run: a probe enumerates several
    # well-known install paths and takes whichever EXISTS, so every entry that
    # does not resolve is expected and already handled. Absence is the designed
    # outcome, exactly as with a marker.
    "candidate", "fallback", "is_file", ".exists(", "shutil.which", "first that",
    "try each", "whichever", "if os.path.exists", "-x ",
)
DEFAULT_TIERS_DOC = "active-scope | unclassified | absence-is-signal | prose | test-fixture"
# Narrative / quoted / dated -> almost always prose.
PROSE_HINTS = (
    "for example", "e.g.", "example", "such as", "measured", "observed",
    "incident", "historical", "formerly", "used to", "was ", "until 20",
    "retired", "deprecated", "note that", "illustrat", "entirely ordinary",
)
DATE_RX = re.compile(r"\b20\d\d-\d\d-\d\d\b")

DEFAULT_TIERS = ("active-scope", "unclassified", "absence-is-signal", "prose", "test-fixture")

# A path inside a COMMENT is prose that happens to live in a script — the
# narrative half of a source file. Second-largest false-positive family on the
# first full run (incident traces and worked examples in header comments).
COMMENT_RX = re.compile(r'^\s*(#|//|--)|(?<!:)#\s')


def _is_test_fixture(path):
    """Tests deliberately name paths that must NOT exist — that is the point of
    an out-of-root or negative fixture. Reporting them as vacuity inverts their
    meaning, and they outnumbered every other family on the first full run."""
    parts = {p.lower() for p in Path(path).parts}
    return "tests" in parts or "test" in parts or Path(path).name.startswith("test_")


def _default_roots():
    """Returns (roots, skipped). SKIPPED IS THE LOAD-BEARING HALF.

    world/ is an EXTERNAL path, so this resolves it from $WORLD_PATH — and a
    caller that has not sourced _paths.sh has no WORLD_PATH, which silently
    drops world/conventions from the corpus. Measured on this script's OWN
    verify-learning call site: 2063 files -> 1985, and the active-scope count
    fell from 9 to 4, while the scan still reported success. That is precisely
    the defect this script exists to detect, occurring inside its own wiring —
    so a skipped root is REPORTED, never inferred from a smaller number nobody
    is comparing against."""
    roots, skipped = [], []
    world = os.environ.get("WORLD_PATH")
    if not world:
        skipped.append({"root": "world/conventions",
                        "reason": "$WORLD_PATH is unset — source core/scripts/_paths.sh first. "
                                  "world/ is external, so without it this scan silently omits "
                                  "the domain conventions half of the corpus."})
    else:
        p = Path(world) / "conventions"
        (roots if p.is_dir() else skipped).append(
            p if p.is_dir() else {"root": str(p), "reason": "directory not found"})
    here = Path(__file__).resolve().parents[1]  # core/
    for sub in ("config", "scripts"):
        d = here / sub
        if d.is_dir():
            roots.append(d)
        else:
            skipped.append({"root": str(d), "reason": "directory not found"})
    return roots, skipped


def _iter_files(roots):
    exts = {".md", ".sh", ".py", ".yaml", ".yml"}
    for root in roots:
        root = Path(root)
        if root.is_file():
            yield root
            continue
        for p in sorted(root.rglob("*")):
            if p.is_file() and p.suffix in exts and "__pycache__" not in p.parts:
                yield p


def _fence_map(lines):
    """Which lines sit inside a fenced code block. A fenced literal is usually
    quoted output or a worked example, not a live scope."""
    inside, out, fence = False, [], None
    for ln in lines:
        s = ln.lstrip()
        if s.startswith("```") or s.startswith("~~~"):
            tok = s[:3]
            if not inside:
                inside, fence = True, tok
            elif tok == fence:
                inside, fence = False, None
            out.append(True)
            continue
        out.append(inside)
    return out


def _score(line, in_fence, is_script, window, is_fixture):
    if is_fixture:
        return "test-fixture", "under a test path — a non-resolving literal here is usually a deliberate negative fixture"
    if is_script and COMMENT_RX.search(line):
        return "prose", "inside a comment — narrative or a worked example, not a live scope"
    low = (line + " " + window).lower()
    marker = sum(h in low for h in MARKER_HINTS)
    scope = sum(h in low for h in SCOPE_HINTS)
    if is_script:
        scope += len(SCRIPT_SCOPE_RX.findall(low))
    prose = sum(h in low for h in PROSE_HINTS) + (1 if DATE_RX.search(low) else 0)

    # Marker beats everything: absence there is the DESIGNED outcome, so the
    # site is not a vacuity candidate at all no matter how it is phrased.
    if marker >= 2 or (marker and ("file_check" in low or "must not resolve" in low)):
        return "absence-is-signal", f"gate/marker context ({marker} marker hint(s))"
    if scope and scope >= prose:
        where = "script constant/scope" if is_script else "documented scope root"
        return "active-scope", f"{where}; {scope} iteration hint(s), {prose} prose hint(s)"
    if in_fence and not scope:
        return "prose", "inside a fenced block (quoted output or worked example)"
    # RELATIVE, not absolute. `prose and not scope` reads as prose only when the
    # iteration score is exactly zero — so a single incidental scope-ish word
    # anywhere in the 7-line window suppressed the prose verdict entirely and
    # dumped the site into `unclassified`. Measured on the control fixture, whose
    # own heading contained the word "scope".
    if prose > scope:
        return "prose", f"{prose} narrative/historical hint(s) vs {scope} iteration hint(s)"
    if marker:
        return "absence-is-signal", f"gate/marker context ({marker} marker hint(s))"
    return "unclassified", "no decisive context signal — needs a human read"


def scan(roots):
    files, findings = 0, []
    for path in _iter_files(roots):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        files += 1
        lines = text.splitlines()
        fences = _fence_map(lines)
        is_script = path.suffix in (".sh", ".py")
        is_fixture = _is_test_fixture(path)
        for i, line in enumerate(lines):
            for m in LITERAL_RX.finditer(line):
                lit = m.group(1).rstrip(".,;:)")
                try:
                    resolves = Path(lit).exists()
                except OSError:
                    resolves = False
                if resolves:
                    continue
                window = " ".join(lines[max(0, i - 3):i + 4])
                tier, why = _score(line, fences[i], is_script, window, is_fixture)
                findings.append({
                    "file": str(path), "line": i + 1, "literal": lit,
                    "tier": tier, "why": why, "context": line.strip()[:160],
                })
    return files, findings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", action="append", default=[], help="corpus root (repeatable)")
    ap.add_argument("--tier", action="append", default=[], choices=list(DEFAULT_TIERS))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.root:
        roots, skipped = [Path(r) for r in a.root], []
    else:
        roots, skipped = _default_roots()
    files, findings = scan(roots)

    counts = {t: sum(1 for f in findings if f["tier"] == t) for t in DEFAULT_TIERS}
    if files == 0:
        out = {"verdict": "CANNOT_CHECK", "files_scanned": 0,
               "reason": "the scan reached ZERO files — this is NOT a clean corpus. "
                         "Check the roots; a detector that reports clean on an empty "
                         "scan has inherited the vacuity it exists to find.",
               "roots": [str(r) for r in roots], "roots_skipped": skipped}
        print(json.dumps(out, indent=2))
        return 2

    wanted = a.tier or list(DEFAULT_TIERS)
    shown = [f for f in findings if f["tier"] in wanted]
    out = {
        "verdict": "SCANNED_PARTIAL" if skipped else "SCANNED",
        "files_scanned": files,
        "roots": [str(r) for r in roots],
        "roots_skipped": skipped,
        "non_resolving_literals": len(findings),
        "tier_counts": counts,
        "note": ("Tiers are CANDIDATES, never verdicts (guard-1561). "
                 "'active-scope' is the vacuity class: absence yields a silent empty "
                 "set. 'absence-is-signal' is a gate whose marker is SUPPOSED not to "
                 "resolve here — not a defect. Nothing is filed automatically."),
        "findings": sorted(shown, key=lambda f: (DEFAULT_TIERS.index(f["tier"]), f["file"], f["line"])),
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
