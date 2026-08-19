#!/usr/bin/env python3
"""Layer-C detective: find user-email senders that never reach the routing gate.

The 2026-08-10 directive ("I actually do not want any emails anymore, if you can
handle them") is enforced by notification_routing_gate.decide. A gate is only as
broad as its entry points (guard-3448): g-115-5825 shipped the gate on ONE
surface (/notify-user Step 1.5b) while seven other call sites invoked the
transport directly and never saw it. g-115-6422 swept those. This script is what
stops the next one from re-opening the hole silently.

WHY A SCRIPT AND NOT A GREP. The goal's own acceptance text proposed
`grep -rln 'email-send.sh|notify-from-file.sh' ... returns no unrouted sender`.
Run literally that returns 25 files on this repo, of which 8 are senders: the
rest are the two transports themselves, the payload builder, four test files,
and a dozen prose mentions in comments and docstrings. A check whose clean state
is "25 hits, and you must eyeball which ones count" is a check nobody runs. This
separates INVOCATION from MENTION and reports only what is actionable.

VERDICT SEMANTICS. exit 0 = every detected sender is routed or explicitly
allowlisted. exit 1 = at least one unrouted sender. exit 2 = the scan could not
run (an unreadable root); that is NOT a pass — a zero-hit report from a scan
that read nothing is the rb-245 false clean, so it is reported separately.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

TRANSPORTS = ("email-send.sh", "notify-from-file.sh")

# A line that INVOKES a transport, as opposed to naming it. Three real shapes:
#   shell pipe        : ... | bash "$SCRIPT_DIR/email-send.sh"
#   python subprocess : bash_cmd(email_script) / subprocess.run([... sender ...])
#   path assignment   : sender = world / "scripts" / "email-send.sh"
_INVOKE_PATTERNS = (
    re.compile(r"\|\s*bash\s+[^|\n]*(?:email-send|notify-from-file)\.sh"),
    re.compile(r'(?:sender|email_script|transport)\s*=\s*.*(?:email-send|notify-from-file)'),
    re.compile(r'"scripts"\s*/\s*"(?:email-send|notify-from-file)\.sh"'),
)

# Files that contain a transport string but are not senders.
_NOT_A_SENDER = (
    "email-send.sh",            # the transport itself
    "notify-from-file.sh",      # the transport itself
    "notify-build-payload.py",  # builds a payload, writes stdout, sends nothing
)

# Senders that legitimately do NOT call the gate. Each needs a REASON, and the
# reason is the point: an allowlist of bare paths becomes a place to hide a
# regression. Both entries below are load-bearing and were measured.
ALLOWLIST = {
    "core/scripts/user-blocker-escalation-check.py": (
        "carries explicit recorded categories (user-digest / info) instead. Its "
        "all-clear is `info`, which the gate would SUPPRESS, and that send is "
        "user-requested (D3: 'it would give me comfort'). Routing it would break "
        "a user-directed requirement."),
    "core/scripts/inbox-alert-age-check.py": (
        "routed, but via its own _routing_decision helper rather than the shared "
        "decide_and_log — it was the reference implementation and predates it."),
}

_ROUTED_MARKERS = (
    "notification_routing_gate",
    "notification-routing-gate.sh",
    "_notify_gate.sh",
    "notify_gate_allows",
)


def _roots():
    """Return (roots, errors).

    AN UNRESOLVABLE WORLD ROOT IS AN ERROR, NOT AN OMISSION. This returned a
    bare list until 2026-08-17 and swallowed the _paths ImportError with a
    `pass`, which meant the world root vanished from the scan while `errors`
    stayed empty -- so main() returned 0 and the report read CLEAN. Measured by
    blocking the import: files_scanned fell 1401 -> 1195 and three world senders
    (billing-accuracy-guard.sh, daily-cost-report.sh, processor-run.sh) were
    never examined, with nothing in the output saying so.

    That is precisely the rb-245 false clean this module's docstring claims to
    defend against, and the asymmetry is what hid it: the `not root.is_dir()`
    branch in scan() records an error and exits 2 (loud), while the resolution
    failure exited 0 (silent). Two routes to "world/ was not scanned", and only
    one of them told you.
    """
    roots = [SCRIPT_DIR.parent.parent / "core" / "scripts",
             SCRIPT_DIR.parent.parent / ".claude" / "skills"]
    errors = []
    try:
        from _paths import WORLD_DIR
        roots.append(Path(WORLD_DIR) / "scripts")
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        errors.append(
            "world root unresolvable (%s: %s) -- world/scripts senders were "
            "NOT scanned; this report is incomplete, not clean"
            % (type(exc).__name__, exc))
    return roots, errors


def scan():
    project = SCRIPT_DIR.parent.parent
    roots, root_errors = _roots()
    findings, errors, scanned = [], list(root_errors), 0

    for root in roots:
        if not root.is_dir():
            errors.append("root not readable: %s" % root)
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in (".py", ".sh", ".md"):
                continue
            # Tests reference transports to assert on them; excluding them is
            # correct, and naming the exclusion here keeps it from being a
            # silent one.
            if "/tests/" in path.as_posix() or path.name.startswith("test_"):
                continue
            if path.name in _NOT_A_SENDER:
                continue
            try:
                src = path.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:  # noqa: BLE001
                errors.append("unreadable: %s (%s)" % (path, exc))
                continue
            scanned += 1
            if not any(t in src for t in TRANSPORTS):
                continue

            # Strip comment-only lines before testing for invocation — the
            # densest source of false positives is prose ABOUT the transport.
            code = "\n".join(
                ln for ln in src.splitlines()
                if not ln.lstrip().startswith(("#", "//"))
            )
            if not any(p.search(code) for p in _INVOKE_PATTERNS):
                continue

            try:
                rel = path.relative_to(project).as_posix()
            except ValueError:
                rel = "world/scripts/%s" % path.name

            if any(m in src for m in _ROUTED_MARKERS):
                continue
            if rel in ALLOWLIST:
                continue
            findings.append({"file": rel,
                             "why": "invokes a user-email transport with no "
                                    "routing-gate call and no allowlist entry"})

    return {"findings": findings, "errors": errors, "files_scanned": scanned,
            "allowlisted": sorted(ALLOWLIST)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--output", choices=("text", "json"), default="text")
    args = ap.parse_args(argv)

    r = scan()
    if args.output == "json":
        print(json.dumps(r, indent=2))
    else:
        # Always print the population, never just the hit count. A "0 unrouted"
        # beside "0 files scanned" is a broken scan, not a clean tree (rb-245).
        print("notification-routing-coverage: scanned %d file(s), %d unrouted, "
              "%d allowlisted" % (r["files_scanned"], len(r["findings"]),
                                  len(r["allowlisted"])))
        for e in r["errors"]:
            print("  ERROR %s" % e)
        for f in r["findings"]:
            print("  UNROUTED %s — %s" % (f["file"], f["why"]))

    if r["errors"]:
        return 2
    return 1 if r["findings"] else 0


if __name__ == "__main__":
    sys.exit(main())
