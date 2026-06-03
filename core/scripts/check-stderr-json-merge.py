#!/usr/bin/env python3
"""
check-stderr-json-merge.py -- detect the guard-659 regression class.

guard-659: a shell command captured with `2>&1` whose output is then fed to
python `json.loads` / `json.load` silently zeroes out on ANY stderr line --
the parse fails, the `except` branch returns empty, and the caller reads a
false-empty result (canonical: infra-streak-notify.sh:58, alert_count -> 0,
a guard-465 silent-monitoring instance; detected g-249-16, fixed g-249-18).

Three valid mitigations (all recognized here):
  (a) remove `2>&1` so stderr never merges into the captured stdout
      (infra-streak-notify.sh, g-249-18) -- the var is then not 2>&1-captured,
      so it never enters the flag set.
  (b) keep `2>&1` (unavoidable stream merge, e.g. rt_call) but parse with
      `json.JSONDecoder().raw_decode()` + residual-split instead of
      `json.loads()` (aspirations-update-goal.sh:143-160, g-115-769) -- the
      `raw_decode` token in the consumer window marks the site EXEMPT.
  (c) keep `2>&1` but extract the JSON line first: iterate stdin line by line,
      skip lines that are not valid JSON (`for line in ...` + a
      `startswith("{")` prefix filter), and `json.loads(line)` only the
      surviving line (iteration-close.sh:769-784, guard-559) -- the stderr
      INFO lines are filtered out BEFORE the parse, so the merge is harmless.
      The `for line in` + `startswith` pair in the consumer window marks the
      site EXEMPT.

The bug shape that REMAINS flagged: `json.loads(<whole-2>&1-stream>)` (e.g.
`json.loads(sys.stdin.read())`) with neither raw_decode nor a per-line
prefix filter -- any stderr line then fails the whole-blob parse.

Detection (per shell file):
  1. Find variables captured via `VAR=$(...)` / `VAR="$(...)` where `2>&1`
     appears INSIDE the command substitution (depth-matched, handles
     multi-line captures).
  2. For each such var, find references `"$VAR"` that are piped (`|`) soon
     after -- i.e. the captured value flows into a downstream parser.
  3. In the ~1500-char consumer window, if `json.loads`/`json.load(` appears
     and the window is NOT hardened by mitigation (b) or (c) -> FLAG.
     A hardened window -> EXEMPT.

Scope: top-level core/scripts/*.sh only (NOT core/scripts/tests/*.sh -- test
files capture 2>&1 intentionally to assert on stderr). Pass explicit file
paths as args to scan a custom set (used by the regression test fixture).

Exit 0 = clean (no flagged sites). Exit 1 = flagged sites found.
Output: JSON {scanned, flagged:[{file,var,pos}], exempt:[{file,var,reason}]}.
"""
import sys
import re
import glob
import json

CAP_RE = re.compile(r'(\w+)=\"?\$\(')


def captures_with_2to1(text):
    """Var names captured via $(...) with `2>&1` inside the substitution body."""
    found = set()
    for m in CAP_RE.finditer(text):
        var = m.group(1)
        i = m.end()  # position just after the opening `$(`
        depth = 1
        body_start = i
        n = len(text)
        while i < n and depth > 0:
            c = text[i]
            if c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
            i += 1
        body = text[body_start:i]
        if '2>&1' in body:
            found.add(var)
    return found


def hardening_reason(window):
    """Reason string if the json.loads consumer window is hardened, else None.

    (b) raw_decode + residual-split (g-115-769).
    (c) per-line extraction: line iteration + a `startswith` prefix filter that
        drops non-JSON stderr lines before the parse (guard-559).
    """
    if 'raw_decode' in window:
        return "raw_decode hardening (g-115-769)"
    if ('for line in' in window) and ('startswith' in window):
        return "per-line JSON extraction (guard-559)"
    return None


def scan(paths):
    flagged, exempt, scanned = [], [], 0
    for path in paths:
        try:
            text = open(path, encoding='utf-8').read()
        except (OSError, UnicodeDecodeError):
            continue
        scanned += 1
        vars_2to1 = captures_with_2to1(text)
        if not vars_2to1:
            continue
        for var in vars_2to1:
            for vm in re.finditer(r'"\$' + re.escape(var) + r'"', text):
                # require the captured value to be piped soon after the reference
                tail = text[vm.end():vm.end() + 30]
                if '|' not in tail:
                    continue
                window = text[vm.start():vm.start() + 1500]
                uses_loads = ('json.loads' in window) or ('json.load(' in window)
                if not uses_loads:
                    continue
                reason = hardening_reason(window)
                if reason:
                    exempt.append({"file": path, "var": var, "reason": reason})
                else:
                    flagged.append({"file": path, "var": var, "pos": vm.start()})
                break  # one verdict per var is enough
    return flagged, exempt, scanned


def main(argv):
    paths = argv[1:] or sorted(glob.glob("core/scripts/*.sh"))
    flagged, exempt, scanned = scan(paths)
    print(json.dumps({"scanned": scanned, "flagged": flagged, "exempt": exempt},
                     indent=2))
    return 1 if flagged else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
