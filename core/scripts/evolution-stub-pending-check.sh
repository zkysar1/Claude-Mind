#!/usr/bin/env bash
# Evolution-stub PENDING check — the missing PROMPT half of the stub lifecycle.
#
# THE GAP THIS CLOSES (, 2026-07-14):
#   evolution-record.py writes an `awaiting_completion` stub on every self.md /
#   program.md edit. The LLM is then supposed to call evolution-complete.sh,
#   which records the WHY and — for a MATERIAL agent_self change — fires the
#   guard-380 user notification. Nothing ever PROMPTS that call. 24h later
#   evolution-stub-expiry.py silently transitions the stub to `expired`
#   (honestly: it refuses to fabricate a rationale, which is correct).
#
#   Net effect measured on 2026-07-14: of 65 MATERIAL Self edits fleet-wide,
#   only 11 (17%) ever reached the user. 22 EXPIRED unnotified — the agent's
#   identity changed and the user was never told. On 2026-04-22 the user
#   explicitly traded "ask first" for "notify after, revert if wrong"; the
#   notify-after half was silently not executing, so the autonomy was unearned.
#
#   The expiry sweep is the honest FALLBACK. This script is the missing PROMPT:
#   it fires a WM sentinel while the stub is still finalizable, and
#   aspirations-precheck Phase 0-pre2.5 forces the LLM to complete it BEFORE
#   goal selection. Same rb-428 sentinel-gate shape as tree-debt (Phase 0-pre),
#   experience-archival (0-pre2), fresh-eyes-code (0-pre3), metric-encoding
#   (0-pre4) — every other obligation in this family already has a forcing
#   consumer; self-evolution finalization was the one that did not.
#
# SCOPE — deliberately narrow (implementation-discipline.md):
#   Only the `self` and `program` streams. These carry the guard-380
#   user-notification promise, are low-volume (80 + 6 rows ever), and had ZERO
#   pending stubs at authoring time, so the gate is silent unless a real
#   obligation exists.
#   NOT script/skill/rule. Measured 2026-07-14: script-evolution has 152
#   pending and 1992 expired vs 23 final (a 99% expiry rate) — a firehose, and
#   a separate finding filed on its own. Widening this gate to those streams
#   without first fixing that would fire every iteration forever and train the
#   agent to ignore the sentinel.
#
# Idempotent (re-fires the same sentinel until finalized), fail-open (any error
# -> no sentinel, never blocks the loop), ALWAYS exits 0.
#
# Usage: bash core/scripts/evolution-stub-pending-check.sh [--threshold-minutes N]
#        (default 20 — long enough that an in-flight edit is not nagged, far
#         inside evolution-stub-expiry's 24h deadline so there is room to act)
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh" 2>/dev/null || exit 0

THRESHOLD_MINUTES=20
while [[ $# -gt 0 ]]; do
    case "$1" in
        --threshold-minutes) THRESHOLD_MINUTES="${2:-20}"; shift 2 ;;
        *) shift ;;
    esac
done

export EVO_STUB_THRESHOLD_MINUTES="$THRESHOLD_MINUTES"
# PROJECT_ROOT comes from _paths.sh (sourced above, line 47), which exports it
# unconditionally as the REPO ROOT. Do NOT re-add a `${PROJECT_ROOT:-...}`
# fallback here: the obvious one ($SCRIPT_DIR/..) resolves to core/, NOT the repo
# root, so the sentinel path would become core/core/scripts/wm.py. That fallback
# was dead (unreachable — _paths.sh always wins) AND wrong, which is the worst
# combination: harmless today, a trap for the next reader. Single source of truth
# — fail visibly rather than fall back to an inconsistent one
# (communication-clarity.md rule 5). If _paths.sh cannot be sourced we exit 0 at
# line 47 and never reach here. (fresh-eyes-code, )

# NOTE: stderr is deliberately NOT swallowed (no `2>/dev/null`). The sibling
# producer experience-staleness-check.sh:63 omits it too, and guard-424's class
# ("gate/precheck scripts must fail loud with stderr, not silently") applies:
# a gate built to catch SILENT non-execution must not itself be able to silently
# non-execute. `|| exit 0` keeps it fail-OPEN (never blocks the loop) while a
# crash still lands in iteration-close's stderr log. (fresh-eyes-code, )
python3 - <<'PY' || exit 0
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

agent = os.environ.get("MIND_AGENT", "").strip()
if not agent:
    sys.exit(0)  # unbound session — nothing to attribute

world = os.environ.get("WORLD_PATH") or os.environ.get("WORLD_DIR") or ""
if not world:
    sys.exit(0)
world = Path(world)

try:
    threshold = float(os.environ.get("EVO_STUB_THRESHOLD_MINUTES", "20"))
except ValueError:
    threshold = 20.0

now = datetime.now()

# Only the governance-critical streams. See SCOPE in the header — widening this
# to script/skill/rule would fire on a 152-deep pending backlog every iteration.
STREAMS = {
    "self": "self-evolution.jsonl",
    "program": "program-evolution.jsonl",
}

pending = []
for stream_name, filename in STREAMS.items():
    path = world / filename
    if not path.exists():
        continue
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        continue
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (ValueError, TypeError):
            continue
        if rec.get("status") != "awaiting_completion":
            continue
        if rec.get("agent") != agent:
            continue  # never nag an agent about a partner's stub
        ts = rec.get("ts") or rec.get("timestamp") or rec.get("created")
        try:
            age_min = (now - datetime.fromisoformat(str(ts))).total_seconds() / 60.0
        except (ValueError, TypeError):
            # Unparseable timestamp: treat as OLD, not as absent. An
            # unfinalizable stub we cannot age is exactly the one that rots.
            age_min = threshold + 1.0
        if age_min < threshold:
            continue  # in-flight edit — do not nag mid-work
        pending.append({
            "revision_id": rec.get("revision_id"),
            "stream": stream_name,
            "file_kind": rec.get("file_kind"),
            "file_path": rec.get("file_path"),
            "change_class": rec.get("change_class"),
            "section_changed": rec.get("section_changed"),
            "age_minutes": round(age_min, 1),
            "ts": ts,
        })

if not pending:
    sys.exit(0)  # clean — the common case, silent

pending.sort(key=lambda p: -p["age_minutes"])
material = [p for p in pending if p.get("change_class") == "material"]

payload = json.dumps({
    "triggered_at": now.isoformat(timespec="seconds"),
    "count": len(pending),
    "material_count": len(material),
    "threshold_minutes": threshold,
    "stubs": pending[:10],
})

# Sentinel write. FAIL-OPEN (a missed sentinel is re-fired next iteration) but
# never FAIL-SILENT: the rc is checked and the summary line below reports what
# ACTUALLY happened. Claiming "set" without verifying the write is a positive
# state claim made without evidence (verify-before-assuming.md § Positive
# File-State Claims) — and here it would be self-defeating, since a persistently
# failing write would print "set" forever while the consumer never sees a
# sentinel: the exact silent-drift this gate exists to prevent. (fresh-eyes-code)
#
# wm.py REQUIRES MIND_AGENT in the env (it raises at import without it). We
# inherit it implicitly — safe, because this script already exits above when
# MIND_AGENT is unset — but a broken inherit now surfaces as a WARN, not silence.
ok = False
pr = (os.environ.get("PROJECT_ROOT") or "").rstrip("/")
if not pr:
    print("[evolution-stub-pending] WARN: PROJECT_ROOT unset — cannot write sentinel",
          file=sys.stderr)
else:
    try:
        r = subprocess.run(
            [sys.executable, pr + "/core/scripts/wm.py", "set", "force_evolution_finalize"],
            input=payload, text=True, capture_output=True, timeout=5, cwd=pr,
        )
        ok = r.returncode == 0
        if not ok:
            print("[evolution-stub-pending] WARN: wm.py set failed rc={rc}: {err}".format(
                rc=r.returncode, err=(r.stderr or "").strip()[:200]), file=sys.stderr)
    except Exception as exc:
        print("[evolution-stub-pending] WARN: sentinel write raised: {e!r}".format(e=exc),
              file=sys.stderr)

print(
    "[evolution-stub-pending] {n} awaiting_completion stub(s) for {a} "
    "({m} MATERIAL) past {t:.0f}min — force_evolution_finalize {s}".format(
        n=len(pending), a=agent, m=len(material), t=threshold,
        s="set" if ok else "NOT SET (write failed — see WARN above)",
    )
)
PY

exit 0
