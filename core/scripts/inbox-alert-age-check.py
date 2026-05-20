#!/usr/bin/env python3
"""Inbox-Alert Age Escalation — scan  for aged alert-sweep Unblocks.

Closes finding (2) of g-115-822: when alert-sweep.sh files an Unblock for an
inbound alert email and no agent claims it within a few hours, the alert
silently ages. The bash gate is already in place upstream (alert-sweep.sh
files Unblock goals with `origin_signal=f"alert-email:{s3_key}"`); this
script is the precheck-side aging-escalation sweep — equivalent to Phase
0.5b.1 (blocker_age_hours) but for the goal-queue surface rather than the
working-memory `known_blockers` surface.

Called by aspirations-precheck Phase 0.5b.1b. Reads asp-115 via the daemon
(_rt.aspirations_read) and reads/appends working-memory
`proactive_escalation_log` via the daemon (_rt.wm_read + wm-append.sh).
Dry-run by default; pass --apply to actually fire notifications and write
cooldown entries.

Severity is determined per the goal's age vs.
`config.proactive_escalation.inbox_alert_age_hours.{high,medium}`:
  - age >= high_hours   → severity=high (notify if no prior fire within high_hours)
  - age >= medium_hours → severity=medium (notify if no prior fire within medium_hours)
  - otherwise           → skip (under threshold)

A goal that crosses the high threshold AFTER it already received a medium
notification will re-fire under the high schedule (the cooldown lookup uses
the SAME key but the threshold the lookup compares against is
severity-dependent). This intentionally lets the user receive a fresh
"upgraded to HIGH" notification when an alert ages further. Same pattern as
Phase 0.5b.1 → Phase B7 escalation ladder.

Fail-open at every layer:
  - Missing config block          → fall back to high=4, medium=12 (same as YAML defaults)
  - daemon unreachable            → exit 0, empty `candidates`, stderr note
  - asp-115 not present in world  → exit 0, empty `candidates`
  - wm-read errors                → empty cooldown log (everything fires)
  - email-send failure (per goal) → log to stderr, KEEP cooldown entry to
                                    avoid retry-storm; --apply continues to
                                    remaining candidates

Exit codes: always 0. Use the JSON output's `applied` count to determine
what changed.

Usage:
    python3 inbox-alert-age-check.py [--apply] [--asp-id asp-115]
                                     [--high-hours N] [--medium-hours N]
                                     [--proactive-escalation-log <path>]  # tests only
                                     [--no-email]                          # tests only
"""

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def _now_iso() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat()


def _parse_iso(s):
    """Parse an ISO-8601 timestamp robustly. Return None on parse failure."""
    if not s or not isinstance(s, str):
        return None
    try:
        # Allow trailing Z for UTC (alert-sweep writes local-time, but tolerate either).
        return dt.datetime.fromisoformat(s.rstrip("Z"))
    except Exception:
        return None


def _age_hours(iso_ts: str, now: dt.datetime) -> float:
    """Hours between `now` and the parsed timestamp. None on failure."""
    parsed = _parse_iso(iso_ts)
    if parsed is None:
        return None
    return (now - parsed).total_seconds() / 3600.0


def _load_config(args) -> dict:
    """Resolve high_hours / medium_hours from CLI > YAML > YAML-default.
    Fail-open: missing YAML or missing keys fall back to (4, 12).
    """
    high = args.high_hours
    medium = args.medium_hours
    if high is not None and medium is not None:
        return {"high": float(high), "medium": float(medium)}
    try:
        import yaml  # type: ignore
        with open(CORE_ROOT / "config" / "aspirations.yaml", "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        pe = (cfg.get("proactive_escalation") or {})
        block = (pe.get("inbox_alert_age_hours") or {})
        if high is None:
            high = block.get("high", 4)
        if medium is None:
            medium = block.get("medium", 12)
    except Exception as exc:
        # Fail-open: stderr note + YAML defaults.
        sys.stderr.write(
            "inbox-alert-age-check: config load failed (%s) — using defaults\n" % exc)
        if high is None:
            high = 4
        if medium is None:
            medium = 12
    return {"high": float(high), "medium": float(medium)}


def _read_aspiration(asp_id: str) -> dict:
    """Read a single aspiration from the world queue. Empty dict on failure."""
    try:
        import _rt
        raw = _rt.aspirations_read(source="world", asp_id=asp_id)
        return json.loads(raw) if raw else {}
    except Exception as exc:
        sys.stderr.write(
            "inbox-alert-age-check: aspirations_read(%s) failed (%s) — fail-open\n"
            % (asp_id, exc))
        return {}


def _read_proactive_log(log_path: Path = None) -> list:
    """Read wm.proactive_escalation_log. Returns [] on any failure.

    When `log_path` is provided (tests only), read it as JSON-list directly,
    bypassing the daemon. Production callers must pass log_path=None so the
    real WM is consulted via _rt.wm_read.
    """
    if log_path is not None:
        try:
            with open(log_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                return data
            return []
        except Exception:
            return []
    try:
        import _rt
        raw = _rt.wm_read(slot="proactive_escalation_log", as_json=True)
        if not raw:
            return []
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
        if parsed is None:
            return []
        # wm_read prints "null" + empty list both — normalize.
        return []
    except Exception as exc:
        sys.stderr.write(
            "inbox-alert-age-check: wm_read(proactive_escalation_log) failed (%s) — treating as empty\n"
            % exc)
        return []


def _classify_severity(age_hours: float, thresholds: dict) -> str:
    """Return "high", "medium", or "" if under threshold."""
    if age_hours is None:
        return ""
    if age_hours >= thresholds["high"]:
        return "high"
    if age_hours >= thresholds["medium"]:
        return "medium"
    return ""


def _find_last_escalation(log: list, blocker_id: str) -> dict:
    """Return the most-recent log entry for blocker_id, or None.
    Most-recent is determined by sent_at ISO compare (lexicographic == chronological)."""
    matches = [e for e in log if isinstance(e, dict) and e.get("blocker_id") == blocker_id]
    if not matches:
        return None
    matches.sort(key=lambda e: e.get("sent_at", ""), reverse=True)
    return matches[0]


def _on_cooldown(last_entry: dict, threshold_hours: float, now: dt.datetime) -> bool:
    """True if the last escalation is within `threshold_hours` of now."""
    if not last_entry:
        return False
    sent_at = last_entry.get("sent_at")
    age = _age_hours(sent_at, now)
    if age is None:
        # Corrupted entry — treat as expired (safer to re-notify than to suppress forever).
        return False
    return age < threshold_hours


def _classifier_subject(goal: dict) -> str:
    """Extract the classifier subject from the alert. Falls back to ''."""
    # The alert-sweep filer doesn't bake the classifier subject into a top-level
    # goal field — it lives in the description. The current alert-sweep.sh format
    # puts the subject after "Subject: " in the description. Best-effort regex
    # so a description-format drift degrades to empty rather than crashing.
    import re
    desc = goal.get("description", "") or ""
    m = re.search(r"^Subject:\s*(.+)$", desc, re.MULTILINE)
    if m:
        return m.group(1).strip()
    return ""


def _send_email(goal: dict, severity: str, age_hours: float, no_email: bool) -> tuple:
    """Fire the notification via world/scripts/email-send.sh. Returns (ok, detail).

    When `no_email` is True (tests), skip the subprocess and return (True, "no_email").
    """
    title = goal.get("title", "")
    goal_id = goal.get("id", "")
    classifier = _classifier_subject(goal)
    age_h = age_hours if age_hours is not None else 0.0
    sev_label = severity.upper()
    subject = "Unclaimed alert >%dh: %s" % (int(age_h), title)
    body_lines = [
        "Alert-sweep filed an Unblock goal %.0f hours ago and no agent has claimed it yet."
        % age_h,
        "",
        "Goal: %s" % title,
        "Goal id: %s" % goal_id,
        "Severity: %s" % sev_label,
    ]
    if classifier:
        body_lines.append("Classifier subject: %s" % classifier)
    body_lines.append("")
    body_lines.append(
        "Action: claim the goal manually (one agent should run it), or "
        "investigate why no agent is picking up alert-sweep Unblocks. The "
        "goal will continue to age and re-notify per cooldown until claimed.")
    payload = {
        "InfoType": "Inbox Alert Age Escalation",
        "Title": subject,
        "InfoMessage": "\n".join(body_lines),
    }
    if no_email:
        return True, "no_email"
    try:
        world_dir = os.environ.get("WORLD_DIR")
        if not world_dir:
            # Resolve from local-paths.conf
            try:
                import _paths  # type: ignore
                world_dir = str(_paths.WORLD_DIR)
            except Exception:
                sys.stderr.write(
                    "inbox-alert-age-check: cannot resolve WORLD_DIR for email-send.sh — skipping notify\n")
                return False, "no_world_dir"
        email_script = Path(world_dir) / "scripts" / "email-send.sh"
        if not email_script.is_file():
            sys.stderr.write(
                "inbox-alert-age-check: email-send.sh not found at %s — skipping notify\n"
                % email_script)
            return False, "no_email_script"
        proc = subprocess.run(
            ["bash", str(email_script)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if proc.returncode == 0:
            return True, "sent"
        sys.stderr.write(
            "inbox-alert-age-check: email-send.sh exit=%d stderr=%s\n"
            % (proc.returncode, (proc.stderr or "").strip()[:300]))
        return False, "email_send_nonzero:%d" % proc.returncode
    except Exception as exc:
        sys.stderr.write(
            "inbox-alert-age-check: email-send.sh exception (%s) — skipping notify\n" % exc)
        return False, "email_send_exception:%s" % exc.__class__.__name__


def _append_log(blocker_id: str, severity: str, sent_at: str, log_path: Path = None) -> bool:
    """Append a cooldown entry. Test-mode writes directly to log_path; production writes via wm-append.sh.

    Returns True on success.
    """
    entry = {
        "blocker_id": blocker_id,
        "severity": severity,
        "sent_at": sent_at,
    }
    if log_path is not None:
        # Test mode: read-modify-write the JSON file directly.
        existing = []
        try:
            with open(log_path, "r", encoding="utf-8") as fh:
                existing = json.load(fh)
            if not isinstance(existing, list):
                existing = []
        except Exception:
            existing = []
        existing.append(entry)
        try:
            with open(log_path, "w", encoding="utf-8") as fh:
                json.dump(existing, fh)
            return True
        except Exception as exc:
            sys.stderr.write(
                "inbox-alert-age-check: test-mode log write failed (%s)\n" % exc)
            return False
    try:
        # wm-append.sh appends one entry per stdin call. The slot is auto-init to
        # an empty list by wm.py if missing.
        # Windows path-separator fix ( audit): .as_posix() avoids
        # the bash backslash-escape stripping that silently no-ops these
        # invocations on Windows. Same pattern as dependent-unblock.py.
        proc = subprocess.run(
            ["bash", (SCRIPT_DIR / "wm-append.sh").as_posix(),
             "proactive_escalation_log"],
            input=json.dumps(entry),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if proc.returncode == 0:
            return True
        sys.stderr.write(
            "inbox-alert-age-check: wm-append.sh exit=%d stderr=%s\n"
            % (proc.returncode, (proc.stderr or "").strip()[:300]))
        return False
    except Exception as exc:
        sys.stderr.write(
            "inbox-alert-age-check: wm-append.sh exception (%s)\n" % exc)
        return False


def run(args) -> dict:
    """Main sweep. Returns the JSON-shape result dict (also printed to stdout)."""
    thresholds = _load_config(args)
    now = dt.datetime.now()
    asp = _read_aspiration(args.asp_id)
    goals = (asp.get("goals") or []) if isinstance(asp, dict) else []
    candidates = []
    cooldown_log_path = Path(args.proactive_escalation_log) if args.proactive_escalation_log else None
    log_entries = _read_proactive_log(cooldown_log_path)

    for g in goals:
        if not isinstance(g, dict):
            continue
        if g.get("status") not in ("pending", "in-progress"):
            continue
        title = g.get("title", "") or ""
        if not title.startswith("Unblock"):
            continue
        sig = g.get("origin_signal", "") or ""
        if not sig.startswith("alert-email:"):
            continue
        age = _age_hours(g.get("created_at", ""), now)
        sev = _classify_severity(age, thresholds)
        if not sev:
            continue
        blocker_id = "inbox_alert_%s" % g.get("id", "")
        # Cooldown threshold is the severity's age threshold — re-notify after
        # that many hours have elapsed since the last fire of the SAME severity.
        # Using thresholds[sev] keeps the cadence matched to the severity's
        # urgency: HIGH alerts re-fire every high_hours, MEDIUM every medium_hours.
        last = _find_last_escalation(log_entries, blocker_id)
        on_cooldown = False
        if last:
            # Severity-aware: a fresh HIGH fire should ignore old MEDIUM cooldown,
            # but a recent HIGH should suppress a fresh MEDIUM. Implementation:
            # compare against the threshold for the CURRENT severity.
            on_cooldown = _on_cooldown(last, thresholds[sev], now)
        candidates.append({
            "goal_id": g.get("id"),
            "title": title,
            "age_hours": round(age, 2),
            "severity": sev,
            "blocker_id": blocker_id,
            "origin_signal": sig,
            "on_cooldown": on_cooldown,
            "last_escalation": last,
        })

    fired = []
    skipped_cooldown = []
    failed = []
    if args.apply:
        for c in candidates:
            if c["on_cooldown"]:
                skipped_cooldown.append(c["goal_id"])
                continue
            # Look up the full goal for the email payload.
            full = next((g for g in goals if g.get("id") == c["goal_id"]), None)
            if full is None:
                continue
            sent_iso = _now_iso()
            ok, detail = _send_email(full, c["severity"], c["age_hours"], args.no_email)
            if ok:
                # Append log regardless of email send outcome — see fail-open
                # note above (keep cooldown to prevent retry storm).
                _append_log(c["blocker_id"], c["severity"], sent_iso, cooldown_log_path)
                fired.append({
                    "goal_id": c["goal_id"],
                    "severity": c["severity"],
                    "age_hours": c["age_hours"],
                    "detail": detail,
                })
            else:
                # email_send failed — STILL append log per fail-open contract:
                # without it the next sweep tick would retry within the same
                # minute, spamming the email infra.
                _append_log(c["blocker_id"], c["severity"], sent_iso, cooldown_log_path)
                failed.append({
                    "goal_id": c["goal_id"],
                    "severity": c["severity"],
                    "detail": detail,
                })

    return {
        "mode": "apply" if args.apply else "dry_run",
        "asp_id": args.asp_id,
        "thresholds_hours": thresholds,
        "scanned": len(goals),
        "candidates": candidates,
        "candidate_count": len(candidates),
        "applied": len(fired),
        "fired": fired,
        "skipped_cooldown": skipped_cooldown,
        "failed": failed,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--apply", action="store_true",
                   help="Actually send notifications and append cooldown entries (default: dry-run).")
    p.add_argument("--asp-id", default="asp-115",
                   help="Aspiration to scan (default: asp-115 — the alert-sweep target queue).")
    p.add_argument("--high-hours", type=float, default=None,
                   help="Override high-severity threshold (default: config or 4).")
    p.add_argument("--medium-hours", type=float, default=None,
                   help="Override medium-severity threshold (default: config or 12).")
    p.add_argument("--proactive-escalation-log", default=None,
                   help="Test-only: path to a JSON file standing in for the WM proactive_escalation_log slot.")
    p.add_argument("--no-email", action="store_true",
                   help="Test-only: skip the email-send.sh subprocess and pretend it succeeded.")
    args = p.parse_args()
    result = run(args)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
