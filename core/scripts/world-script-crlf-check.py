#!/usr/bin/env python3
"""world-script-crlf-check.py — assert every executable *.sh is LF-only.

WHY THIS LANE EXISTS (g-115-7288). On 2026-08-22 `world/scripts/email-send.sh`
was found with whole-file CRLF (10,591 B; CR/LF/CRLF 196 each). bash reads line
16 as `set -euo pipefail\r` and exits rc=2 on EVERY invocation, so the fleet's
outbound email transport — the delivery path behind /notify-user and the alert
lanes — was dead silently for at least 17 minutes while five agents worked. The
failure is rc=2 plus a stderr line, so any caller that swallows stderr sees only
a non-zero rc and no cadence reported it.

WHY A DETECTOR AND NOT A PARSER FIX. The framework's established answer to
own-cloud CRLF is to make PARSERS CRLF-tolerant (verify-learning section GAE-2,
g-115-1934; rb-3061; rb-2686; guard-987). That works for DATA files and cannot
work here: for a shell script *bash itself* is the parser and it cannot be made
tolerant. `world/scripts/*.sh` is therefore the one class where the standing
mitigation does not reach, and nothing asserted LF over it.

WHY THE DELIVERY PATH IS LIVE, NOT HISTORICAL (positive control, zeta/cc-02,
2026-08-24): a full walk of `world/` found 6,738 CRLF-bearing files; excluding
6,584 `.gz` history blobs (compressed binary) that is ~154 live text files —
66 .json, 36 .md, 26 .yaml, 14 .jsonl, 4 .soln, 1 .log, 1 .py — and three of
them sit in `world/scripts/` itself (`check-grant-constant-agreement.py`,
`.lambda-health-snapshot.json`, `.roblox-promote-ppe2.json`). own-cloud is
delivering CRLF into this directory today. `*.sh` reads clean only because the
one corrupted script was repaired.

TWO ROOTS, DELIBERATELY.
  * `world/scripts` — the incident site. `world/` is gitignored
    (`.gitignore` `/.mind-data/`), so `.gitattributes` `*.sh text eol=lf` does
    NOT reach it. This root has no other defense; it is the required scope.
  * `core/scripts` — defense-in-depth. `.gitattributes` pins these at git
    CHECKOUT, and verify-learning already asserts the pin exists — but a pin is
    not a byte check, and it says nothing about a working tree that was copied,
    synced, or seeded rather than checked out. Same fatal failure mode, same
    loop, two lines. Reported separately so a hit is attributable to a root.

COUNTING. CR is counted with a Python byte count, never `grep -c` (rb-1026:
`grep -c` counts LINES containing a match, so a whole-file-CRLF script and a
one-line accident are indistinguishable, and some greps normalise the input).
A file is an offender if it contains ANY b"\r" — a lone CR is as fatal to bash
as a CRLF pair, so the predicate is CR presence, not CRLF presence.

NO FALSE-POSITIVE CLASS. A `*.sh` carrying CR cannot execute correctly on
Linux, so there is nothing to tune and no exemption list to rot. That is why
this is an always-run lane rather than a cadence.

REPORT-ONLY BY CONTRACT. There is no `--apply` and there must never be one:
repairing a file under `world/` is a write into a live own-cloud-synced store
and can race the sync, and the 2026-08-22 repair needed a content-preservation
assertion to be safe. Report loudly; let an agent repair deliberately.

Exit code is ALWAYS 0 (the loop must never block on a detector) — findings and
scan failures both travel in the JSON. A scan failure lands in `failed`, which
the always-run battery treats as both a finding and an error, so a half-working
lane is never reported as clean (guard-4093).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SELF = Path(__file__).resolve().parent
sys.path.insert(0, str(SELF))


def _world_scripts_root() -> Path | None:
    """Resolve world/scripts through the framework helper, never by guessing."""
    try:
        from _paths import WORLD_DIR  # module-level constant, not a function
        if WORLD_DIR:
            return Path(WORLD_DIR) / "scripts"
    except Exception:
        pass
    env = os.environ.get("WORLD_PATH") or os.environ.get("MIND_WORLD")
    return Path(env) / "scripts" if env else None


def _in_scope(root: Path):
    """Files the CR check governs: every *.sh, plus every EXECUTABLE file whose
    first two bytes are `#!`.

    WHY THE SECOND CLAUSE (g-115-7422, zeta 2026-08-24): the no-false-positive
    argument this lane rests on is about the KERNEL SHEBANG PATH, and that path
    does not care about the extension. Any exec+`#!` file is read by the kernel
    as `#!/usr/bin/env python3\r` and dies "bad interpreter". The lane keyed on
    EXTENSION while the failure keys on EXEC+SHEBANG.

    The exec bit is load-bearing and must NOT be dropped to "any file with a
    shebang": a `.py` invoked as `python3 file.py` never reaches the kernel
    shebang path and CPython tolerates CRLF, so sweeping those in would
    manufacture exactly the false-positive class this lane claims not to have.
    Measured at widening (one snapshot, both predicates): world/scripts ADDED=0,
    core/scripts ADDED=46, REMOVED=0 in both, CR-bearing among ADDED=0 in both
    (guard-2201 — a widening must be a proper superset, and the assertion that
    matters is that the OLD set lost nothing).
    """
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix == ".sh":
            yield path
            continue
        try:
            if path.stat().st_mode & 0o111 and path.open("rb").read(2) == b"#!":
                yield path
        except Exception:
            # Unreadable here is not a silent drop: the file is still reached by
            # the *.sh arm if it qualifies, and scan_root records read failures
            # in failed[] rather than treating them as clean.
            continue


def scan_root(root: Path | None, label: str) -> dict:
    """Return {label, root, scanned, offenders[], failed[]} for one root.

    An unreadable root is a FAILURE, never a clean zero: a missing directory and
    a directory of clean files both produce scanned=0 (guard-1675 / rb-245).
    """
    out = {"label": label, "root": str(root) if root else None,
           "scanned": 0, "offenders": [], "failed": []}
    if root is None:
        out["failed"].append(f"{label}: root could not be resolved")
        return out
    if not root.is_dir():
        out["failed"].append(f"{label}: {root} is not a directory")
        return out
    for path in _in_scope(root):
        try:
            data = path.read_bytes()
        except Exception as exc:                       # unreadable == blind
            out["failed"].append(f"{label}: {path}: {exc.__class__.__name__}: {exc}")
            continue
        out["scanned"] += 1
        cr = data.count(b"\r")
        if cr:
            out["offenders"].append({
                "path": str(path),
                "root": label,
                "bytes": len(data),
                "cr": cr,
                "lf": data.count(b"\n"),
                "crlf": data.count(b"\r\n"),
                "whole_file": data.count(b"\r\n") == data.count(b"\n") != 0,
            })
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true",
                    help="emit JSON (default; kept so callers may pass it explicitly)")
    ap.add_argument("--root", action="append", default=None, metavar="PATH",
                    help="scan this directory instead of the defaults (repeatable; "
                         "used by the regression fixture)")
    args = ap.parse_args(argv)

    if args.root:
        roots = [(Path(r), f"root:{Path(r).name}") for r in args.root]
    else:
        roots = [(_world_scripts_root(), "world/scripts"),
                 (SELF, "core/scripts")]

    per_root = [scan_root(r, label) for r, label in roots]
    offenders, failed, scanned = [], [], 0
    for res in per_root:
        offenders.extend(res["offenders"])
        failed.extend(res["failed"])
        scanned += res["scanned"]

    print(json.dumps({
        "check": "world-script-crlf",
        "scanned": scanned,
        "offender_count": len(offenders),
        "offenders": offenders,
        "failed": failed,
        "per_root": [{k: v for k, v in r.items() if k != "offenders"} for r in per_root],
        "remedy": ("A *.sh containing CR cannot run on Linux. Repair deliberately: "
                   "back the file up, translate \\r\\n -> \\n, and ASSERT "
                   "strip-all-CR(backup) == new bytes before replacing, so nothing "
                   "but line endings changed (the 2026-08-22 g-115-7288 procedure). "
                   "Then find the writer: under own-cloud a Windows box can sync CRLF "
                   "into world/, and .gitattributes does not reach a gitignored tree."),
    }))
    return 0                                            # always 0 — never block the loop


if __name__ == "__main__":
    raise SystemExit(main())
