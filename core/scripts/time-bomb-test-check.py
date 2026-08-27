#!/usr/bin/env python3
"""BR16 (): flag tests pinning an absolute timestamp against now().

DIRECTION is the discriminator. A literal used as a LOWER bound (`> "2026-.."`)
grows more true as time passes and can never flip; an upper bound or equality
against a now()-derived value is the bomb. Comments are stripped — the fixed
form of a past bomb often quotes the literal it replaced.
"""
import re, pathlib, sys

ISO = re.compile(r'["\']20[0-9]{2}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}')
NOW = re.compile(r'\b(datetime\.now|datetime\.utcnow|utcnow\(\)|time\.time\(\)|date\.today)\b')
SAFE = re.compile(r'>=?\s*["\']20')  # lower bound — safe by direction


def main() -> int:
    hits, scanned = [], 0
    for root in (pathlib.Path("core/scripts/tests"), pathlib.Path("mind_api/tests")):
        if not root.exists():
            continue
        for p in sorted(root.rglob("*.py")):
            scanned += 1
            lines = [l.split("#", 1)[0] for l in
                     p.read_text(encoding="utf-8", errors="replace").splitlines()]
            nows = [i for i, l in enumerate(lines) if NOW.search(l)]
            for i, l in enumerate(lines):
                # Polarity is per-COMPARISON, not per-line: excising the safe
                # lower bounds first stops one of them suppressing a real bomb
                # sharing the line (`start >= "..." and end == "<now-derived>"`).
                if ISO.search(SAFE.sub("", l)) and any(abs(i - j) <= 2 for j in nows):
                    hits.append(f"{p}:{i+1}")
                    break
    if hits:
        print(f"WARN: {len(hits)} possible time-bomb test(s) of {scanned} scanned: "
              + ", ".join(hits[:6]))
    else:
        print(f"PASS: 0 time-bomb tests of {scanned} scanned")
    return 0


if __name__ == "__main__":
    sys.exit(main())
