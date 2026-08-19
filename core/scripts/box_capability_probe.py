#!/usr/bin/env python3
"""
box_capability_probe.py — does THIS box actually hold the resources a goal needs?
(g-115-6754, rb-8408)

NOT a permissions tool. `agent-capability-sheet.py` answers "am I ALLOWED to",
by rendering a view of documentation. This answers "can I ACTUALLY, right now,
on this filesystem" — a live probe of resource PRESENCE. The distinction is the
reason both exist: in all three failures that motivated this, the agent was
fully permitted and simply lacked the resource, so a permission sheet would have
reported green on every one. Documentation cannot answer a filesystem question.

The measured cases (alpha, cc-04, 2026-08-19 — three consecutive iterations, each
a top-ranked goal claimed and then unfinishable):

  g-363-23  needed a reachable peer world — $PEER_WORLD_* unset, peer_world_path
            set in zero of four registry files, no ZDS checkout on this disk
  g-363-13  needed MIND_STRIPE_SECRET_KEY — env-read.sh rc=2 here; the goal was
            measured on cc-13, which has it
  g-363-10  piece D needed an operator deploy this box does not perform

None was a permission problem, a defer excuse, or a slow partner, and each
produced a legitimate-looking blocked outcome with a well-argued, TRUE reason.
That is what makes the class invisible: nothing is wrong, the work just cannot
finish here, and the reason stays true forever so the reclaim sweep correctly
re-defers it every cycle.

DESIGN NOTES, the non-obvious ones:

  * Consulted at CLAIM time, not scoring time. Scoring is the hot path and runs
    over ~1200 candidates; probing the filesystem there would be absurd. At claim
    time it runs once, for one goal.

  * A missing capability means DECLINE, never defer. Deferring marks the goal
    un-workable for everyone; declining leaves it claimable by a box that has the
    resource. This distinction is the entire point — the three cases above became
    global defers when they should have been local declines.

  * It reports, it does not gate. Splitting a goal along its box-dependence seam
    beats refusing it whole: g-363-13's grep-and-positive-control was valid from
    any box and reduced the remaining work to two API calls. A caller that
    auto-refused on the first missing capability would have thrown that away.

  * Absence is reported as absence, never as failure. A probe that cannot tell
    "not here" from "the probe broke" is worse than none, because the second one
    silently reads as the first.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
ENV_REGISTRY = PROJECT_ROOT / "core" / "config" / "environments"

# Present = probe succeeded. Absent = probe ran and the resource is not here.
# Unknown = the probe itself could not run, which is NOT absence (see module doc).
PRESENT, ABSENT, UNKNOWN = "present", "absent", "unknown"


def probe_secret(name):
    """A vaulted secret, by NAME. The value is never read, printed, or returned.

    Uses env-read.sh — the canonical accessor — rather than reading .env.local
    directly, so this measures what a consumer would actually get (a direct file
    read would report present for a secret the accessor cannot resolve).
    """
    script = PROJECT_ROOT / "core" / "scripts" / "env-read.sh"
    if not script.exists():
        return UNKNOWN, "env-read.sh not found — cannot probe secrets on this box"
    try:
        # bash_cmd, not a bare "bash" argv: bare bash[0] resolves to System32 WSL
        # on win32 and can hang forever (guard-580), and it passes the script path
        # as a posix string because bash silently strips a str(WindowsPath)'s
        # backslashes (guard-581). A hang here would be especially bad — it would
        # surface as UNKNOWN on the timeout branch, i.e. as "we could not tell",
        # on a box where the answer was simply available.
        from _runtime_bash import bash_cmd
        r = subprocess.run(
            bash_cmd(str(script), name),
            capture_output=True, text=True, timeout=30,
        )
    except ImportError:
        return UNKNOWN, "_runtime_bash unavailable — cannot probe secrets safely"
    except subprocess.TimeoutExpired:
        return UNKNOWN, "env-read.sh timed out"
    except OSError as e:
        return UNKNOWN, f"could not run env-read.sh: {e}"
    if r.returncode == 0 and r.stdout.strip():
        return PRESENT, "resolves via env-read.sh"
    return ABSENT, f"env-read.sh rc={r.returncode}"


def probe_peer_world(env_id):
    """A peer deployment's world, by environment id.

    Mirrors the resolution order in cross-world-post.sh:45-60 deliberately —
    $PEER_WORLD_<ENV_ID> first, then peer_world_path: in the registry entry. If
    those two ever diverge from this, the probe reports a reachability the
    posting scripts do not have, which is the failure mode worth guarding.
    """
    var = "PEER_WORLD_" + env_id.upper().replace("-", "_")
    candidates = []
    if os.environ.get(var, "").strip():
        candidates.append((os.environ[var].strip(), f"${var}"))

    entry = ENV_REGISTRY / f"{env_id}.yaml"
    if entry.exists():
        m = re.search(r"^peer_world_path:\s*(.+)$", entry.read_text(), re.MULTILINE)
        if m:
            val = m.group(1).strip().strip("'\"")
            if val:
                candidates.append((val, f"peer_world_path in {entry.name}"))
    elif not ENV_REGISTRY.exists():
        return UNKNOWN, "environment registry directory not found", None

    if not candidates:
        return ABSENT, f"neither ${var} nor peer_world_path is set", None

    for path, source in candidates:
        if Path(path).is_dir():
            return PRESENT, f"resolved via {source}", path
    # Configured but not on disk. Distinct from unconfigured, and more alarming:
    # something claims this path exists and it does not.
    paths = ", ".join(p for p, _ in candidates)
    return ABSENT, f"configured ({paths}) but no such directory on this box", None


def probe_path(path):
    """A named filesystem surface — a repo checkout, a deploy tree, a mount."""
    p = Path(path)
    if p.is_dir():
        return PRESENT, "directory exists"
    if p.exists():
        return PRESENT, "exists (not a directory)"
    return ABSENT, "no such path on this box"


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__)
        print("usage: box_capability_probe.py --secret NAME | --peer ENV_ID | "
              "--path PATH [...]\n"
              "       box_capability_probe.py --self-check\n\n"
              "Exit 0 when every probed capability is PRESENT, 1 when any is\n"
              "ABSENT, 2 when any is UNKNOWN (probe could not run — not absence).")
        return 0

    # --self-check is the positive control the goal's verification asks for: it
    # proves the probe can report PRESENT, so a run of all-absent is a finding
    # about the box rather than about a broken probe.
    if argv[1] == "--self-check":
        st, why = probe_path(str(PROJECT_ROOT))
        ok = st == PRESENT
        print(json.dumps({
            "self_check": "pass" if ok else "FAIL",
            "probed": str(PROJECT_ROOT),
            "status": st,
            "detail": why,
            "note": "positive control — the probe can report PRESENT; an "
                    "all-absent run elsewhere is about the box, not the probe",
        }, indent=2))
        return 0 if ok else 2

    results, i = [], 1
    while i < len(argv):
        arg = argv[i]
        if i + 1 >= len(argv):
            print(f"error: {arg} needs a value", file=sys.stderr)
            return 2
        target = argv[i + 1]
        if arg == "--secret":
            st, why = probe_secret(target)
            results.append({"kind": "secret", "name": target,
                            "status": st, "detail": why})
        elif arg == "--peer":
            st, why, resolved = probe_peer_world(target)
            row = {"kind": "peer_world", "name": target,
                   "status": st, "detail": why}
            if resolved:
                row["path"] = resolved
            results.append(row)
        elif arg == "--path":
            st, why = probe_path(target)
            results.append({"kind": "path", "name": target,
                            "status": st, "detail": why})
        else:
            print(f"error: unknown flag {arg}", file=sys.stderr)
            return 2
        i += 2

    if not results:
        print("error: nothing probed", file=sys.stderr)
        return 2

    absent = [r for r in results if r["status"] == ABSENT]
    unknown = [r for r in results if r["status"] == UNKNOWN]
    verdict = ("can_execute" if not absent and not unknown
               else "cannot_execute_here" if absent and not unknown
               else "indeterminate")
    print(json.dumps({
        "box": os.uname().nodename if hasattr(os, "uname") else "unknown",
        "agent": os.environ.get("MIND_AGENT", ""),
        "verdict": verdict,
        "guidance": {
            "can_execute": "claim normally",
            "cannot_execute_here": "DECLINE and leave the goal claimable by a "
                                   "box that has the resource — do NOT defer, "
                                   "which marks it un-workable for everyone. Do "
                                   "the box-independent half if there is one.",
            "indeterminate": "a probe could not run; absence is NOT established "
                             "— investigate the probe before concluding",
        }[verdict],
        "results": results,
    }, indent=2))
    return 2 if unknown else (1 if absent else 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
