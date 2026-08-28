#!/usr/bin/env python3
"""notify_dispatch.py -- the FRAMEWORK notification chokepoint.

User directive (2026-08-16): "I want the framework to want to notify the user,
and double-check before that notifying happens; each deployment figures out
what 'notify user' physically IS -- a text message, an email." So:

  * core owns the INTENT + every CHECK + the RECORD (this file);
  * the domain owns only the TRANSPORT, through one existence-gated executable
    slot: ``$WORLD_DIR/scripts/notify-transport.sh``. Core never names an
    email/SMS/webhook script (domain-hooks.md, Pattern B, executable form).

Pipeline (in order; each step is skippable only by an explicit, recorded flag):

  1. routing gate      -- notification_routing_gate.decide(): "is the fleet
                          telling him something it can handle itself?"
                          SUPPRESS -> re-route to the findings board, never
                          drop (g-335-1097). exit 3.
  2. prior-outreach    -- notification_outreach.find_prior(): "has ANY agent in
                          ANY world already told him this?" (rb-7986 ledger).
                          duplicate -> record the suppression, exit 4, unless
                          --allow-duplicate '<what is new>' (recorded).
  3. payload           -- notify-build-payload.py (identity line, empty-body
                          guard, finding-disproof gate) unless --payload-stdin
                          carries an already-built, provenance-stamped payload.
  4. transport slot    -- run the domain executable with the payload on stdin
                          and NOTIFY_DISPATCHED=1 in env. Missing/non-exec slot
                          -> exit 5 (caller falls back to pending question /
                          participant goal). Non-zero -> exit 6.
  5. record            -- append the send to world/notifications-sent.jsonl,
                          mirror a user-outreach line to reachable peer worlds.

Exit codes: 0 sent | 2 usage/build error | 3 suppressed by routing (re-routed)
| 4 duplicate | 5 no transport configured | 6 transport failed.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _paths import WORLD_DIR, PROJECT_ROOT  # noqa: E402
import notification_outreach as outreach  # noqa: E402

TRANSPORT_SLOT = "scripts/notify-transport.sh"

RC_SENT, RC_USAGE, RC_ROUTED, RC_DUP, RC_NO_TRANSPORT, RC_TRANSPORT_FAIL = 0, 2, 3, 4, 5, 6

# . `reply` is an ALWAYS_SEND category, so it is the one shape that
# can walk past the 2026-08-10 suppression directive. The citation is what keeps
# it from becoming a bypass: every reply must state, in the message itself, what
# the user asked and when. That reaches him (so a wrong claim is visible and he
# can say so), the routing gate's text, and the outreach ledger's stored body.
#
# APPENDED, NEVER PREPENDED, for two independent reasons. He asked a question:
# the answer belongs at the top of the mail, not behind a bookkeeping line. And
# notification_outreach.body_fingerprint() is the normalized HEAD of the body
# (BODY_FP_CHARS=400), so a fixed prefix on every reply would seed a shared
# fingerprint across unrelated replies and make the dedup gate refuse the second
# one as a duplicate of the first.
REPLY_CITATION_PREFIX = "Replying to what you asked: "


def _bash_argv(script: str, *args: str) -> list:
    # guard-580/581: never a bare "bash" argv[0]; _runtime_bash resolves it.
    from _runtime_bash import bash_cmd  # noqa: WPS433
    return list(bash_cmd(script, *args))


def _log(msg: str) -> None:
    print(f"[notify-dispatch] {msg}", file=sys.stderr)


def transport_path(world: Path | None = None) -> Path:
    return Path(world or WORLD_DIR) / TRANSPORT_SLOT


def _subject_body_from_payload(d: dict) -> tuple:
    subj = (d.get("Title") or d.get("InfoType") or d.get("ErrorFrom") or "")
    body = d.get("Body") or d.get("InfoMessage") or d.get("ErrorMessage") or ""
    if not isinstance(body, str):
        body = json.dumps(body)
    return str(subj)[:300].replace("\n", " "), body


def _category_from_payload(d: dict) -> str:
    import re
    it = re.sub(r"^\s*(\[[^\]]*\]\s*)+", "", d.get("InfoType") or "").strip().lower()
    m = {"notification": "info", "completion report": "completion", "aspiration update": "update",
         "infrastructure alert": "blocker", "decision needed": "decision-needed", "user digest": "user-digest",
         "fleet digest": "user-digest", "goals waiting on you": "user-digest",
         "reply": "reply"}
    if it in m:
        return m[it]
    return "blocker" if ("ErrorMessage" in d or "ErrorFrom" in d) else ""


def route_check(category: str, subject: str, body: str):
    """Returns (suppress: bool, reason, destination). The gate's verdict is an
    INT constant (SEND/SUPPRESS), so compare against nrg.SUPPRESS, never a
    string -- a string compare silently sends everything (caught 2026-08-17)."""
    try:
        import notification_routing_gate as nrg  # noqa: WPS433
        verdict, reason, destination = nrg.decide(category, subject, body)
        return (verdict == nrg.SUPPRESS), reason, destination
    except Exception as exc:  # fail-open: an unknown gate error must not make the user unreachable
        return False, f"routing gate unavailable ({exc}); fail-open SEND", None


def gate_log(gate: str, outcome: str, category: str, subject: str) -> None:
    """Best-effort telemetry (meta/gate-firings.jsonl) -- parity with the calls
    notify-user SKILL.md Step 1.5b used to make by hand."""
    script = HERE / "gate-log.sh"
    if not script.exists():
        return
    try:
        subprocess.run(_bash_argv(str(script), gate, outcome, "--caller", "notify_dispatch.py",
                                  "--trigger", category or "", "--payload", subject[:200]),
                       capture_output=True, text=True, timeout=30)
    except Exception:
        pass


def reroute_to_board(subject: str, body: str, category: str, reason: str) -> bool:
    """Best-effort re-route of a routing-suppressed message to the findings board."""
    script = HERE / "board-post.sh"
    if not script.exists():
        return False
    text = (f"{subject}\n\n{body}\n\n(suppressed from the user channel per the 2026-08-10 directive; "
            f"category={category}; reason: {reason})")
    try:
        p = subprocess.run(_bash_argv(str(script), "--channel", "findings", "--type", "finding",
                                      "--tags", f"suppressed-notification,{category or 'uncategorized'}"),
                           input=text, capture_output=True, text=True, timeout=60)
        return p.returncode == 0
    except Exception:
        return False


def build_payload(agent: str, category: str, subject: str, message: str | None, message_file: str | None,
                  builder_args: list) -> tuple:
    argv = [sys.executable, str(HERE / "notify-build-payload.py"), "--agent", agent, "--category", category,
            "--subject", subject]
    if message_file:
        argv += ["--message-file", message_file]
    else:
        argv += ["--message", message or ""]
    argv += builder_args
    p = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        return None, p.returncode, (p.stderr or p.stdout).strip()
    try:
        return json.loads(p.stdout), 0, ""
    except Exception as exc:
        return None, 2, f"builder emitted non-JSON: {exc}"


def run_transport(payload: dict, *, world: Path | None, env_extra: dict) -> tuple:
    slot = transport_path(world)
    if not slot.exists():
        return RC_NO_TRANSPORT, f"no transport configured: {slot} is absent"
    # Invoked via bash, so the slot's mode bits do not matter -- an own-cloud
    # pull does not preserve +x, and a "not executable" refusal would silence
    # every box that synced the file rather than authored it.
    env = dict(os.environ)
    env.update(env_extra)
    env["NOTIFY_DISPATCHED"] = "1"
    try:
        p = subprocess.run(_bash_argv(str(slot)), input=json.dumps(payload), env=env,
                           capture_output=True, text=True, timeout=300)
    except Exception as exc:
        return RC_TRANSPORT_FAIL, f"transport raised: {exc}"
    if p.stdout.strip():
        print(p.stdout.rstrip())
    if p.stderr.strip():
        print(p.stderr.rstrip(), file=sys.stderr)
    return (RC_SENT if p.returncode == 0 else RC_TRANSPORT_FAIL), f"transport rc={p.returncode}"


def dispatch(*, agent: str, category: str, subject: str = "", message: str | None = None,
             message_file: str | None = None, payload: dict | None = None, goal_id: str = "",
             allow_duplicate: str = "", builder_args: list | None = None, world: Path | None = None,
             mirror_peers: bool = True, dry_run: bool = False, in_reply_to: str = "") -> int:
    builder_args = builder_args or []
    to_shape_src = os.environ.get("USER_EMAIL", "")

    if payload is not None:
        subject = subject or _subject_body_from_payload(payload)[0]
        body = _subject_body_from_payload(payload)[1]
        category = category or _category_from_payload(payload)
    else:
        if message_file:
            body = Path(message_file).read_text(encoding="utf-8", errors="replace") if Path(message_file).exists() else ""
        else:
            body = message or ""

    # 0. reply citation (). One rule covers BOTH entry paths: a reply
    # either supplies --in-reply-to (composed here) or already carries the
    # citation in its body (a pre-built payload). Neither = refuse. Enforcing it
    # here rather than in main() is deliberate — the payload path derives its
    # category after main() has run, so a main()-only check would leave exactly
    # the shape that can bypass the directive unguarded.
    if (category or "").strip().lower() == "reply":
        if in_reply_to.strip():
            citation = f"\n\n{REPLY_CITATION_PREFIX}{in_reply_to.strip()}"
            body += citation
            if payload is None:
                # Route the augmented text to the builder as an inline message so
                # the citation reaches the EMAIL too, not just the gate and the
                # ledger. message_file is dropped on purpose: its content is
                # already folded into body above, and leaving it set would make
                # the builder re-read the un-augmented file and win.
                message, message_file = body, None
        elif REPLY_CITATION_PREFIX not in body:
            _log("--category reply requires --in-reply-to '<what he asked, and when>'. "
                 "reply is an ALWAYS_SEND category, so the citation is the only thing "
                 "separating an answer he asked for from a re-send of a message the "
                 "routing gate refused (guard-4722). If this is not an answer to "
                 "something he asked, use a different category.")
            return RC_USAGE

    # 1. routing gate
    suppress, reason, destination = route_check(category, subject, body)
    if not dry_run:
        gate_log("notify-user-routing-gate", "block" if suppress else "pass", category, subject)
    if suppress:
        ok = reroute_to_board(subject, body, category, reason) if not dry_run else True
        _log(f"SUPPRESSED by routing gate ({reason}) -> re-routed to {destination or 'findings board'}"
             + ("" if ok else " [board post FAILED -- file a goal so this is not lost]"))
        return RC_ROUTED

    # 2. prior outreach
    hits = outreach.find_prior(subject, body, category, world=world)
    if not dry_run:
        gate_log("notify-user-outreach-gate", "block" if (hits and not allow_duplicate) else "pass", category, subject)
    if hits and not allow_duplicate:
        _log(f"DUPLICATE: the user was already contacted about this topic ({len(hits)} prior within "
             f"{int(outreach.window_for(category).total_seconds() // 3600)}h):")
        for h in hits[:8]:
            _log(f"  - {h['ts']}  {h.get('env') or '-'}/{h.get('agent') or '-'}  [{h.get('category') or '-'}]  "
                 f"{(h.get('subject') or '')[:90]}  ({h['why']}; {h.get('source') or 'ledger'} {h.get('id')})")
        _log("Not sent. Does your message ADD anything he has not been told? If yes: say what is new, "
             "reference the earlier note, and re-run with --allow-duplicate '<what is new>'.")
        if not dry_run:
            rec = outreach.build_record(agent=agent, category=category, subject=subject, body=body, goal_id=goal_id,
                                        transport="none", rc=RC_DUP, to=to_shape_src,
                                        suppressed_duplicate_of=str(hits[0].get("id") or "unknown"))
            outreach._append(outreach.ledger_path(world), rec)
        return RC_DUP
    if hits and allow_duplicate:
        _log(f"duplicate ALLOWED by override: {allow_duplicate}")

    # 3. payload
    if payload is None:
        payload, rc, err = build_payload(agent, category, subject, message, message_file, builder_args)
        if payload is None:
            _log(f"payload builder refused (rc={rc}): {err}")
            return RC_USAGE

    if dry_run:
        print(json.dumps({"would_send": True, "category": category, "subject": subject,
                          "transport": str(transport_path(world)), "payload_keys": sorted(payload)}))
        return RC_SENT

    # 4. transport
    rc, note = run_transport(payload, world=world, env_extra={
        "NOTIFY_CATEGORY": category or "", "NOTIFY_GOAL_ID": goal_id or "", "NOTIFY_AGENT": agent or ""})
    if rc == RC_NO_TRANSPORT:
        _log(note + " -- fall back to a pending question / participant goal (notify-user Step 4)")
        return rc
    if rc != RC_SENT:
        _log(note)
        return rc

    # 5. record + mirror
    try:
        rec = outreach.build_record(agent=agent, category=category, subject=subject, body=body, goal_id=goal_id,
                                    transport=transport_path(world).name, rc=0, to=to_shape_src,
                                    override_reason=allow_duplicate)
        outreach._append(outreach.ledger_path(world), rec)
        if mirror_peers:
            # Capture the per-peer result. mirror_to_peers COMPUTES a
            # [{"peer","rc"}] list and returns it; discarding that return made a
            # silently-skipped unroutable peer (rc=3) indistinguishable from a
            # delivering one, which is why "outreach never reaches a peer" went
            # unnoticed across 12,077 board posts (). Measured
            # 2026-08-27 on this box: all 4 registered peers returned rc=3.
            # Log-only and best-effort by design -- the mirror must never affect
            # the send's exit code (notification_outreach.mirror_to_peers docstring).
            mirror = outreach.mirror_to_peers(rec)
            if mirror:
                _log("mirror: " + " ".join(
                    f"{m.get('peer')}=rc{m.get('rc')}" for m in mirror))
            else:
                _log("mirror: no peer attempted (no registry entries, "
                     "missing peer-board-post.sh, or bash helper unavailable)")
    except Exception as exc:
        _log(f"WARN: ledger record failed ({exc}) -- the notification WAS sent")
    return RC_SENT


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--agent", default=os.environ.get("MIND_AGENT", ""))
    ap.add_argument("--category", default="")
    ap.add_argument("--subject", default="")
    ap.add_argument("--message", default=None)
    ap.add_argument("--message-file", default=None)
    ap.add_argument("--payload-stdin", action="store_true",
                    help="stdin carries an already-built payload JSON (from notify-build-payload.py); "
                         "subject/body/category are derived from it")
    ap.add_argument("--goal-id", default="")
    ap.add_argument("--in-reply-to", default="",
                    help="REQUIRED with --category reply: what the user asked, and when "
                         "(e.g. \"your 2026-08-15 email 'send me an email with exact "
                         "instructions'\"). Appended to the message body, so it reaches him, "
                         "the routing gate and the outreach ledger.")
    ap.add_argument("--allow-duplicate", default=os.environ.get("EMAIL_SEND_ALLOW_DUPLICATE", ""),
                    help="send even if prior outreach exists; state WHAT IS NEW (recorded)")
    ap.add_argument("--builder-arg", action="append", default=[],
                    help="extra flag passed through to notify-build-payload.py (repeatable), e.g. "
                         "--builder-arg=--disproof-probe --builder-arg='<cmd>'")
    ap.add_argument("--no-mirror-peers", action="store_true")
    ap.add_argument("--world", default="", help="override world dir (tests)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if not args.agent:
        _log("--agent (or MIND_AGENT) is required -- every notification identifies its sender")
        return RC_USAGE
    payload = None
    if args.payload_stdin:
        try:
            payload = json.load(sys.stdin)
        except Exception as exc:
            _log(f"--payload-stdin: invalid JSON: {exc}")
            return RC_USAGE
    elif not args.category or not args.subject or (args.message is None and not args.message_file):
        _log("need --category, --subject and --message/--message-file (or --payload-stdin)")
        return RC_USAGE

    return dispatch(agent=args.agent, category=args.category, subject=args.subject, message=args.message,
                    message_file=args.message_file, payload=payload, goal_id=args.goal_id,
                    allow_duplicate=args.allow_duplicate, builder_args=args.builder_arg,
                    world=Path(args.world) if args.world else None,
                    mirror_peers=not args.no_mirror_peers, dry_run=args.dry_run,
                    in_reply_to=args.in_reply_to)


if __name__ == "__main__":
    sys.exit(main())
