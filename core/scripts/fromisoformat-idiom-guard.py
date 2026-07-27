#!/usr/bin/env python3
# domain-leak-exempt: enumerates a framework datetime idiom + excluded script basenames (no domain strings)
"""g-115-3030 ratchet guard — the dangerous strip-to-empty datetime idiom.

Flags the DIRECT form ``fromisoformat( ... .replace("Z", "") ... )``: the
``.replace("Z", "")`` strips a trailing ``Z`` but NOT a numeric ``+00:00`` /
``-05:00`` offset, so an offset-bearing input yields an *aware* datetime and the
next ``datetime.now() - dt`` (naive) raises ``TypeError``. The robust fix is
``core/scripts/_dt.parse_naive_iso`` (strips tzinfo AFTER parsing).

Only the DIRECT wrapping form is dangerous: the guard scans FORWARD from each
``fromisoformat(`` and flags a strip-to-empty ``replace`` INSIDE that call. A
value that is pre-normalized on an earlier line and then fed to a bare
``fromisoformat(v)`` is NOT flagged (those are safe — typically ``[:19]``-sliced).

SAFE forms never flagged: ``replace("Z", "+00:00")`` (deliberate offset convert),
or a ``[:19]`` / ``[:15]`` / ``[:10]`` / ``.date()`` / ``tzinfo=None`` safener in
the same call.

Exit 0 = clean (floor 0), 1 = a NEW dangerous idiom appeared. Excludes:
  - ``_dt.py``  (the canonical fix; its docstring quotes the idiom pedagogically)
  - ``fromisoformat-idiom-guard.py`` (this file — quotes the idiom in docs)
  - ``scorer-override-audit.py`` (zeta g-115-3001 — migrate after zeta lands)
  - tests (``test_*.py`` / any path under ``tests/``)

Companion: guard-1398 (write-time guardrail), core/config/conventions — g-115-3030.
"""
import glob
import json
import os
import re
import sys

EXCLUDE_BASENAMES = {
    "_dt.py",
    "fromisoformat-idiom-guard.py",
    "scorer-override-audit.py",  # zeta g-115-3001, tracked separately
}
SAFENERS = ("[:19]", "[:15]", "[:10]", ".date(", "tzinfo=None")
STRIP_EMPTY = re.compile(r"""replace\(\s*["']Z["']\s*,\s*["']{2}\s*\)""")
WINDOW = 150


def target_files():
    for base in ("core/scripts", "mind_api/src"):
        for f in glob.glob(base + "/**/*.py", recursive=True):
            b = os.path.basename(f)
            if b in EXCLUDE_BASENAMES:
                continue
            if b.startswith("test_") or "/tests/" in f.replace("\\", "/"):
                continue
            yield f


def violations():
    out = []
    for f in target_files():
        try:
            txt = open(f, encoding="utf-8").read()
        except Exception:
            continue
        flat = re.sub(r"\s+", " ", txt)
        for m in re.finditer(r"fromisoformat\(", flat):
            window = flat[m.start(): m.start() + WINDOW]
            if STRIP_EMPTY.search(window) and not any(s in window for s in SAFENERS):
                out.append((f, window[:110]))
    return out


def main():
    v = violations()
    if "--json" in sys.argv:
        print(json.dumps({"count": len(v),
                          "violations": [{"file": f, "context": c} for f, c in v]}))
    else:
        for f, c in v:
            print(f"DANGEROUS: {f}: {c}")
        verdict = "FAIL" if v else "PASS"
        print(f"{verdict}: {len(v)} dangerous strip-to-empty fromisoformat idiom(s) "
              f"(floor 0; excludes _dt.py / this guard / scorer-override-audit.py [zeta g-115-3001] / tests). "
              f"Route new datetime parses through core/scripts/_dt.parse_naive_iso (g-115-3030 / guard-1398).")
    return 1 if v else 0


if __name__ == "__main__":
    sys.exit(main())
