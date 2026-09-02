#!/usr/bin/env python3
"""Layer-C detective: enumerate hand-rolled embedded-block extraction sites.

WHY (g-115-6228, closing guard-2222 at the tool layer). The sibling
PreToolUse advisory (embedded-block-extraction-gate.py) catches the shape at the
moment it is TYPED. It can do nothing about the sites already committed. This
audit is the standing half: it reports where the hand-rolled shape already
lives, so the backlog is a measured list rather than an assumption.

SHARED PREDICATE. The shape itself is defined once in
_embedded_block_predicate.py and imported here verbatim. The gate and this audit
therefore cannot drift into disagreeing about what the shape IS -- the same
arrangement _swakeup_predicate.py gives the ScheduleWakeup gate and its
detective.

WINDOW, NOT RUN-FORWARD (guard-2655). In a script the `grep -n` and the `sed`
slice sit on DIFFERENT lines, so a per-line scan cannot see the shape. This uses
a fixed 6-line sliding window and never runs forward to the next incidental
match. That bound is the whole anti-swallow property: a scanner that chases a
closer emits a line-numbered accusation against a CORRECT file, which is worse
than a miss because the blamed author has nothing to fix and the cost repeats on
every box, every run.

PROSE FILTERING (guard-319). Comment lines are skipped and inline-backtick spans
are stripped via _prose_filter -- the canonical shared implementation, not a
re-roll -- BEFORE the predicate sees the text. Without it, every guardrail that
QUOTES the hand-rolled shape (guard-2222 does, at length) and every SKILL.md
pseudocode comment reads as a live site.

POSITIVE CONTROL IS MANDATORY, NOT DECORATION. A zero from a scanner that is not
actually scanning is indistinguishable from a clean corpus, and this codebase has
been bitten by exactly that (guard-2298). So a synthetic known-hand-rolled string
is run through the same predicate on every invocation and reported beside the
result. If the control does not fire, the run reports BROKEN and exits non-zero
rather than printing a reassuring 0.

READ-ONLY. Never rewrites a site. Repair is a judgement call per site -- some
hand-rolls predate the helper, some are in vendored or frozen files -- so the
output is a work list for a reader, not a patch.

  py -3 core/scripts/embedded-block-hand-roll-audit.py [--json] [--strict]
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _embedded_block_predicate import detect  # noqa: E402

try:
    from _prose_filter import is_prose_line, strip_prose_refs
except Exception:  # pragma: no cover - fail loud rather than scan unfiltered
    def is_prose_line(line, path):
        return Path(path).suffix in (".md", ".sh") and line.lstrip().startswith("#")

    def strip_prose_refs(line, path):
        return line

WINDOW = 6

# In a .py file a shell-looking command inside a string literal is almost always
# DATA -- a fixture row, an example in a docstring, an expected-output constant.
# Measured on first run (): the sole core/ hit was a negative-control
# TABLE in test_git_hook_bypass_gate.py holding `sed -n "1,5p" f.txt` as a quoted
# string for a DIFFERENT gate's tests. guard-319's comment-skip and backtick-strip
# cannot see that, because it is neither a comment nor markdown prose.
#
# The semantic tell is whether the window actually HANDS the string to a shell.
# So for .py only, require an exec indicator in the same window. This is a
# narrowing on meaning, not a filename exemption (guard-2860: never relax an
# ownership predicate into a pattern) -- a .py that genuinely hand-rolls via
# subprocess still fires.
_PY_EXEC_INDICATORS = ("subprocess", "os.system", "os.popen", "check_output",
                       "check_call", "Popen", "run(")

# Files that CONTAIN the shape as data (predicate, consumers, their tests) or
# that ARE the sanctioned helper. Excluding by basename, not by path fragment,
# so a same-named file elsewhere is not silently exempted.
SELF_EXCLUDE = {
    "_embedded_block_predicate.py",
    "embedded-block-extraction-gate.py",
    "embedded-block-extraction-gate.sh",
    "embedded-block-hand-roll-audit.py",
    "embedded-block-hand-roll-audit.sh",
    "test_embedded_block_predicate.py",
    "extract-embedded-block.py",
    "extract-embedded-block.sh",
    "test_extract_embedded_block.py",
}

CONTROL = (
    "start=$(grep -n 'MARKER' host.sh | cut -d: -f1)\n"
    "end=$(awk '/^EOF/{print NR; exit}' host.sh)\n"
    "sed -n \"${start},${end}p\" host.sh\n"
)


def _roots(project_root):
    out = [
        os.path.join(project_root, "core", "scripts"),
        os.path.join(project_root, ".claude", "skills"),
    ]
    # Both known incidents (, ) were in world/scripts, so a
    # corpus that omits it would miss the very sites this audit exists to find.
    world = os.environ.get("WORLD_PATH")
    if world:
        ws = os.path.join(world, "scripts")
        if os.path.isdir(ws):
            out.append(ws)
    return out


def _files(roots):
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in ("__pycache__", ".git", ".python-shim")]
            for fn in sorted(filenames):
                if fn in SELF_EXCLUDE:
                    continue
                if fn.endswith((".sh", ".py")) or fn == "SKILL.md":
                    yield os.path.join(dirpath, fn)


def scan(project_root):
    roots = _roots(project_root)
    findings = []
    scanned = 0
    lines_total = 0
    for path in _files(roots):
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        scanned += 1
        raw = text.splitlines()
        lines_total += len(raw)
        # guard-319: comment-skip THEN backtick strip, before the predicate.
        # _prose_filter's API takes a Path (it reads .suffix) -- convert once
        # per file rather than per line.
        ppath = Path(path)
        eff = []
        for line in raw:
            if is_prose_line(line, ppath):
                eff.append("")
            else:
                eff.append(strip_prose_refs(line, ppath))
        seen_windows = set()
        for i in range(len(eff)):
            window = "\n".join(eff[i:i + WINDOW])
            if not window.strip():
                continue
            f = detect(window)
            if not f:
                continue
            if path.endswith(".py") and not any(
                    ind in window for ind in _PY_EXEC_INDICATORS):
                continue
            # One finding per contiguous region, not one per overlapping window.
            key = (i // WINDOW)
            if key in seen_windows:
                continue
            seen_windows.add(key)
            findings.append({
                "file": os.path.relpath(path, project_root),
                "line": i + 1,
                "form": f["form"],
                "line_range_slice": f["line_range_slice"],
                "line_number_source": f.get("line_number_source"),
            })
    return {
        "findings": findings,
        "count": len(findings),
        "files_scanned": scanned,
        "lines_scanned": lines_total,
        "roots": [os.path.relpath(r, project_root) if r.startswith(project_root)
                  else r for r in roots],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 when any site is found")
    args = ap.parse_args()

    project_root = os.environ.get("PROJECT_ROOT") or os.getcwd()

    control_ok = detect(CONTROL) is not None
    result = scan(project_root)
    result["positive_control"] = "PASS" if control_ok else "BROKEN"

    if args.json:
        print(json.dumps(result, indent=1))
    else:
        print("=== embedded-block hand-roll audit ===")
        print("  roots            %s" % ", ".join(result["roots"]))
        print("  files scanned    %d" % result["files_scanned"])
        print("  lines scanned    %d" % result["lines_scanned"])
        print("  [positive control] synthetic hand-roll detected: %s"
              % result["positive_control"])
        print("  SITES            %d" % result["count"])
        for f in result["findings"]:
            print("    %s:%d  [%s]  %s"
                  % (f["file"], f["line"], f["form"], f["line_range_slice"]))
        if result["count"] == 0 and control_ok:
            print("  (zero is a MEASURED zero -- the control fired on the same "
                  "predicate in the same run)")

    if not control_ok:
        print("BROKEN: positive control did not fire -- this run's count means "
              "NOTHING. Do not record it as clean.", file=sys.stderr)
        return 2
    if args.strict and result["count"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
