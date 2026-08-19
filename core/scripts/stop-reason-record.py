#!/usr/bin/env python3
"""stop-reason-record.py — record WHY a deliberate loop-stop fired, and tell the user.

THE PROBLEM THIS SOLVES (g-115-6322, user directive 2026-08-15: "the loop goes
into quiet mode and stays quiet unnoticed"). Several framework paths stop the
loop ON PURPOSE and tell no one:

    productivity-stop-gate.sh   composite score below floor
    recovery-gate.sh            6-condition zombie RUNNING -> IDLE
    recovery-gate.sh            recovery-failed-permanent circuit breaker (>=3)
    reducer-self-fence.sh       cross-machine lease stepdown

`/start` is USER-ONLY, so after any of these the box sits IDLE until a human
notices on their own. That is exactly what the user experiences as quiet mode.

WHY A NOTIFICATION IS CORRECT HERE, against the standing "I do not want any
emails anymore, if you can handle it" directive: these are precisely the cases
the agent CANNOT handle. Restart requires a user-only command, so the routing is
capability-gated, not a convenience. A user-INITIATED stop passes
`--user-initiated` and never emails (the user already knows) while still writing
the reason file, so the sweeper can classify the box as EXPECTED-IDLE.

WHY PYTHON AND NOT BASH. `productivity-stop-gate.sh`'s stop path is a Python
heredoc that deliberately avoids `subprocess.run(["bash", ...])` — on Windows a
bare `bash` argv[0] resolves to System32's WSL bash, whose filesystem view is
/mnt/c/... and cannot see these paths (the comment at that call site, and
guard-580). A Python helper is callable via `sys.executable` from there and via
`python3` from the two bash callers, so one implementation serves all four paths
with no bash-dialect layer.

Contract:
  - ALWAYS exits 0. A stop must never be blocked by its own announcement.
  - Writes agents/<agent>/session/last-stop-reason ATOMICALLY (.tmp + os.replace,
    guard-320) — the stop-hook and the sweeper may read it concurrently.
  - Notification is fail-open but LOUD: the invoke is logged BEFORE it fires, the
    verdict AFTER, and the failure branch surfaces the consumer's ACTUAL stderr
    (guard-1673, guard-3737 — an opaque `|| true` renders "fired and worked",
    "refused with a precise reason" and "never reached" byte-identically).

Consumer: `g-115-6320` (the out-of-loop fleet-liveness sweeper) reads this file to
separate EXPECTED-IDLE (a reason file exists — the stop was deliberate and has
already been announced once) from UNEXPECTED-IDLE (no reason file — the process
died, nobody was told, alert).
"""
from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _paths  # noqa: E402

REASON_FILENAME = "last-stop-reason"
TS_FMT = "%Y-%m-%dT%H:%M:%S"
DEFAULT_THROTTLE_MINUTES = 60

# The four deliberate-stop paths, plus `user-stop` for the /stop carve-out (it
# records so the sweeper reads EXPECTED-IDLE, but never emails — the user is the
# one who stopped it). A closed set so a typo in a caller surfaces as a refused
# value rather than a silently-unclassifiable reason file the sweeper cannot
# bucket. tests/test_stop_reason_record.py asserts that each of the FOUR
# deliberate-stop paths has a real call site; `user-stop` is deliberately NOT in
# that assertion because nothing calls it yet — /stop is a user-only skill, not
# one of the four automatic paths this goal wired. It is declared here so the
# carve-out is expressible the moment /stop adopts it. Stating that bound
# explicitly rather than letting the comment read as "all of these are wired"
# (guard-1936: a marker is scoped to the sites it names, never to the codebase).
VALID_PATHS = (
    "productivity-stop-gate",
    "recovery-gate-zombie",
    "recovery-failed-permanent",
    "reducer-self-fence",
    "user-stop",
    "worker-body-parked",
    "worker-park-expired",
)

# THE TWO WORKER-PARK PATHS (), and why they sit on OPPOSITE sides of
# the notify split — the same split `user-stop` uses, for the same reason.
#
# A parked worker Body is quiet ON PURPOSE and RESUMES ITSELF: its Phase 0.5 poll
# re-runs hourly and un-parks the moment a reducer returns. So `worker-body-parked`
# is recorded with `--no-notify`. No human action exists to request — emailing one
# would be asking for a `/start` that is not needed, against the standing "no
# emails if you can handle it" directive, and a park IS the agent handling it.
#
# Recording it is NOT optional decoration, and this is the measured part: the
# sweeper's `classify()` returns EXPECTED_IDLE on this file BEFORE it reaches the
# heartbeat-age branch, and its `--stale-min` default is 45 while a park re-polls
# at 3600s (the ScheduleWakeup clamp). 60 > 45, so without this file every parked
# Body is classified DEAD_LOOP for ~15 minutes of every hour and the user is
# emailed that a healthy, deliberately-parked box is dead — the precise inversion
# ("workers parked awaiting reducer, not workers dead") this path exists to fix.
#
# `worker-park-expired` DOES notify: at PARK_MAX_HOURS the Body takes the genuine
# close path, and from there `/start` is the only way back — so it is a real
# deliberate-stop path in the original sense, and belongs with the other four.
#
# THE FILE IS A LATCH, SO THE PARK PATH OWNS ITS INVERSE (`--clear`). Every
# pre-existing writer here stops the loop for good and hands recovery to
# `/start`, which clears the file via `session-manifest-clear.sh`
# (`recovery_action: clear`). A park→resume never goes through `/start`, so
# nothing would ever remove it: the Body would resume, work normally for days,
# and keep reporting EXPECTED_IDLE — suppressing the alert for a LATER genuine
# death. A write with no inverse is not a smaller feature than one with an
# inverse; it is a permanently-disabled detector.
NO_NOTIFY_PATHS = ("worker-body-parked",)


def _now() -> datetime:
    return datetime.now()


def parse_reason_file(text: str) -> dict:
    """Parse the key=value reason format. Unknown keys are preserved."""
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def render_reason_file(fields: dict) -> str:
    """Stable key order so a diff between two stops is readable."""
    order = ("path", "stopped_at", "agent", "box", "reason",
             "user_initiated", "notified")
    lines = [f"{k}={fields[k]}" for k in order if k in fields]
    lines += [f"{k}={v}" for k, v in sorted(fields.items()) if k not in order]
    return "\n".join(lines) + "\n"


def should_throttle(prev: dict, path: str, now: datetime, minutes: int) -> bool:
    """Suppress a repeat notification for the SAME path inside the window.

    The previous reason file IS the throttle state — deliberately no second
    stamp file to drift out of sync with it (communication-clarity rule 5,
    single source of truth).

    Only a previously SENT notification throttles. A previous `failed` or
    `throttled` must not suppress the next attempt, or one transport blip
    silences the path for the whole window — which is the failure this whole
    script exists to prevent.
    """
    if not prev or prev.get("path") != path:
        return False
    if prev.get("notified") != "sent":
        return False
    raw = prev.get("stopped_at") or ""
    try:
        prev_ts = datetime.strptime(raw, TS_FMT)
    except (ValueError, TypeError):
        # Unparseable timestamp -> cannot prove we are inside the window, so
        # do NOT throttle. Fail toward telling the user.
        return False
    return (now - prev_ts) < timedelta(minutes=minutes)


def _send_notification(agent: str, subject: str, body: str, log) -> tuple[str, str]:
    """Build + send the email. Returns (status, detail).

    status is one of: sent | failed. NEVER raises — the caller is mid-stop.
    """
    core = Path(_paths.CORE_ROOT)
    builder = core / "scripts" / "notify-build-payload.py"
    world = Path(_paths.WORLD_DIR)
    sender = world / "scripts" / "email-send.sh"

    if not builder.is_file():
        return "failed", f"builder missing: {builder}"
    if not sender.is_file():
        return "failed", f"transport missing: {sender}"

    # Notification-routing gate (), BEFORE the builder invoke — there
    # is no point rendering a payload that will not be sent. Category `info`
    # matches what this function already passes to the builder below; a stop
    # reason is retrospective status, and the directive of 2026-08-10 asks for
    # exactly this class to stop reaching the user. The human-only override
    # still fires on the TEXT, so a stop caused by an expired credential or a
    # billing halt is sent — which is the case where an unexplained IDLE box
    # genuinely needs a human.
    _nrg = None
    try:
        import notification_routing_gate as _nrg  # type: ignore
        label, gate_reason, _dest = _nrg.decide_and_log(
            "info", subject, body, caller="stop-reason-record.py:_send_notification")
    except Exception as exc:  # noqa: BLE001 - inverted fail-safe: send
        label, gate_reason = ("send",
                              "routing gate unimportable (%s) — inverted "
                              "fail-safe sends" % type(exc).__name__)
    if label == "suppress":
        ok, bc_detail = _nrg.post_suppression_breadcrumb(
            subject, body, caller="stop-reason-record.py:_send_notification",
            reason=gate_reason, tags=["agent-stopped", agent or "unknown"])
        if ok:
            log(f"routing gate suppressed the stop notification ({gate_reason})")
            return "suppressed", gate_reason
        # Breadcrumb did not land -> suppressing would DELETE, not re-route.
        log(f"suppression breadcrumb FAILED ({bc_detail}) — sending instead")

    # guard-3737: log BEFORE the invoke. Without this line the caller's log
    # cannot distinguish fired-and-worked from never-reached.
    log(f"invoking notify transport (builder={builder.name}, agent={agent})")

    try:
        built = subprocess.run(
            [sys.executable, str(builder),
             "--agent", agent, "--category", "info",
             "--subject", subject, "--message", body],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:  # noqa: BLE001 - mid-stop, must not raise
        return "failed", f"builder raised {type(exc).__name__}: {exc}"
    if built.returncode != 0:
        # guard-1673: surface the consumer's ACTUAL error, never a bare
        # "(non-fatal)" with stderr discarded.
        return "failed", f"builder rc={built.returncode}: {built.stderr.strip()[:400]}"
    payload = built.stdout
    if not payload.strip():
        return "failed", "builder produced an empty payload"

    # guard-580 names the canonical resolver by name: never a bare "bash"
    # argv[0] (on Windows CreateProcess searches System32 first and finds the
    # WSL launcher, which blocks forever on a dead LxssManager). Imported HERE
    # rather than at module scope so a missing helper degrades to a reported
    # notification failure instead of making the whole recorder unimportable —
    # this module's contract is that it can never block a stop.
    try:
        from _runtime_bash import bash_cmd
    except Exception as exc:  # noqa: BLE001
        return "failed", f"cannot import _runtime_bash: {type(exc).__name__}: {exc}"

    try:
        sent = subprocess.run(
            bash_cmd(str(sender)), input=payload,
            capture_output=True, text=True, timeout=60,
        )
    except Exception as exc:  # noqa: BLE001
        return "failed", f"transport raised {type(exc).__name__}: {exc}"
    if sent.returncode != 0:
        return "failed", f"transport rc={sent.returncode}: {sent.stderr.strip()[:400]}"
    return "sent", (sent.stdout.strip()[:200] or "ok")


def record(path: str, reason: str, agent: str, *, user_initiated: bool = False,
           notify: bool = True, throttle_minutes: int = DEFAULT_THROTTLE_MINUTES,
           now: datetime | None = None, sender=_send_notification) -> dict:
    """Write the reason file and (conditionally) notify. Returns the fields written."""
    now = now or _now()
    adir = Path(_paths.agent_dir(agent))
    sdir = adir / "session"
    target = sdir / REASON_FILENAME

    prev = {}
    try:
        prev = parse_reason_file(target.read_text(encoding="utf-8"))
    except OSError:
        pass

    def log(msg: str) -> None:
        print(f"[stop-reason] {msg}", file=sys.stderr)

    status = "disabled"
    detail = ""
    subject = f"{agent} on {platform.node()} went IDLE ({path})"
    body = (
        f"{agent} stopped its autonomous loop at {now.strftime(TS_FMT)}.\n\n"
        f"Path:   {path}\n"
        f"Reason: {reason}\n\n"
        f"/start is user-only, so this box stays IDLE until someone restarts it.\n"
        f"Restart with:  /start {agent}\n"
    )

    if user_initiated:
        # The user issued /stop — they already know. Reason file still written so
        # the sweeper reads EXPECTED-IDLE rather than alerting on a healthy stop.
        status = "skipped-user-initiated"
    elif path in NO_NOTIFY_PATHS:
        # STRUCTURAL, not caller-supplied. This path's only call site is
        # LLM-executed prose in worker-loop/SKILL.md, so a forgotten
        # `--no-notify` is a live possibility — and its cost is an hourly email
        # about a box that is deliberately parked and self-resuming, which is
        # both wrong and the fastest way to train the user to ignore this
        # transport. Enforcing it here means the guarantee does not depend on a
        # call site remembering a flag.
        status = "skipped-path-never-notifies"
    elif not notify:
        status = "skipped-disabled"
    elif should_throttle(prev, path, now, throttle_minutes):
        status = "throttled"
        log(f"notification throttled — {path} already announced within "
            f"{throttle_minutes}m (prev stopped_at={prev.get('stopped_at')})")
    else:
        status, detail = sender(agent, subject, body, log)
        # guard-3737: log the VERDICT after the invoke, and log the failure
        # branch loudly — that branch is the one that makes an inert
        # notification look like a working one.
        if status == "sent":
            log(f"notification sent ({detail})")
        elif status == "suppressed":
            # NOT the failure branch (). Suppressed means the routing
            # gate re-routed this to the findings board and the breadcrumb was
            # confirmed posted — somebody HAS been told, just not the user. The
            # CRITICAL wording below would be a false alarm, and a false CRITICAL
            # in the one log a reader checks after an unexplained stop is worse
            # than no log at all.
            log(f"notification suppressed by routing gate ({detail}) — "
                f"re-routed to world/board/findings.jsonl, user not emailed")
        else:
            log(f"CRITICAL: notification FAILED ({detail}). "
                f"{agent} is IDLE and nobody has been told.")

    fields = {
        "path": path,
        "stopped_at": now.strftime(TS_FMT),
        "agent": agent,
        "box": platform.node(),
        "reason": " ".join(str(reason).split()),
        "user_initiated": "1" if user_initiated else "0",
        "notified": status,
    }
    if detail:
        fields["notify_detail"] = " ".join(str(detail).split())[:300]

    # guard-320: atomic .tmp + replace. The stop-hook and the fleet sweeper may
    # read this concurrently; a torn read would misclassify the box.
    try:
        sdir.mkdir(parents=True, exist_ok=True)
        tmp = sdir / f"{REASON_FILENAME}.tmp"
        tmp.write_text(render_reason_file(fields), encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        log(f"CRITICAL: could not write {target}: {exc}")
    return fields


def clear(agent: str) -> bool:
    """Remove the reason file. Idempotent; True when a file was actually removed.

    The inverse of `record`, owned by the single writer (guard-155) rather than
    by an `rm -f` at the call site — the file's lifecycle belongs to the module
    that defines its meaning. Called when a parked Body RESUMES: the box is no
    longer idle, so leaving the latch set would suppress the sweeper's alert for
    every later death.
    """
    target = Path(_paths.agent_dir(agent)) / "session" / REASON_FILENAME
    try:
        target.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        print(f"[stop-reason] WARN: could not clear {target}: {exc}",
              file=sys.stderr)
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    # --path/--reason are required for a RECORD but meaningless for a --clear,
    # so the enforcement moved below rather than staying on the arguments. A
    # required=True --reason would have made `--clear` unusable without passing
    # a reason for un-stopping, which is nonsense.
    ap.add_argument("--path", choices=VALID_PATHS)
    ap.add_argument("--reason")
    ap.add_argument("--clear", action="store_true",
                    help="remove the reason file (a parked Body resuming); "
                         "idempotent, never notifies")
    ap.add_argument("--agent", default=os.environ.get("MIND_AGENT", ""))
    ap.add_argument("--user-initiated", action="store_true")
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--throttle-minutes", type=int, default=DEFAULT_THROTTLE_MINUTES)
    args = ap.parse_args()

    if not args.agent:
        print("[stop-reason] no agent (pass --agent or set MIND_AGENT); "
              "reason NOT recorded", file=sys.stderr)
        return 0  # never block a stop

    if args.clear:
        removed = clear(args.agent)
        print(f"[stop-reason] cleared for {args.agent} "
              f"({'removed' if removed else 'no file present'})",
              file=sys.stderr)
        return 0

    if not args.path or not args.reason:
        # Loud, and NOT via argparse's required=True — see the --path/--reason
        # comment above. Still exit 0: this helper never blocks a stop, and a
        # malformed invocation must not become the reason a box fails to stop.
        print("[stop-reason] --path and --reason are required unless --clear; "
              "reason NOT recorded", file=sys.stderr)
        return 0

    try:
        fields = record(args.path, args.reason, args.agent,
                        user_initiated=args.user_initiated,
                        notify=not args.no_notify,
                        throttle_minutes=args.throttle_minutes)
        print(f"[stop-reason] {fields['path']} recorded "
              f"(notified={fields['notified']})", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        # A stop must complete even if its announcement is broken end to end.
        print(f"[stop-reason] CRITICAL: recorder raised "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
