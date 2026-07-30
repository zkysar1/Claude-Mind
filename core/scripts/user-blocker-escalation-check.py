#!/usr/bin/env python3
"""User-Blocker Escalation — the delivery-channel sibling of the aged-blocker family ().

Scan the world + agent goal queues for non-terminal goals that carry `user` in
`participants` and have aged past `user_blocker_escalation.escalate_hours`
(default 48) with no recent escalation, and DELIVER ONE EMAIL TO THE USER per
aged goal — plus a coordination-board record that doubles as the shared cooldown.

WHY THIS EXISTS (the hole g-115-3926 measured, and why no sibling closes it)
---------------------------------------------------------------------------
Three escalators already sweep aged work: `dependency-timeout-check.py`
(blocked_by edges), `handoff-aging-check.py` (cross-agent handoff_to), and
`inbox-alert-age-check.py` (alert-derived Unblocks by origin_signal prefix).
Each is individually correct. EVERY ONE OF THEM POSTS TO THE COORDINATION
BOARD, which is agent-to-agent.

That is structurally incapable of discharging a goal whose blocking condition is
a HUMAN PHYSICAL ACTION: no agent reading the board can perform it. Measured
2026-07-29 — g-326-70 (HIGH, participants [agent, user], blocking g-326-63 and
g-250-227 under a ship-gate milestone) accumulated 10+ board posts from two
agents in one day while `proactive_escalation_log` stayed EMPTY and the user was
never told. The first user-facing notice that day was hand-written by an agent
that happened to pick the goal. Had it not, the block would have kept sitting.

The sibling sweeps miss it for reasons that are each correct in isolation:
  - 0.5b.1b matches an origin_signal prefix; this goal's is `unblock:g-326-63`
  - 0.5b.2  walks blocked_by edges; a physical human action has no goal-id to
            depend on, so there is no edge
  - 0.5b.2b matches handoff_to; unset on a human-blocked goal
This is the guard-1802 / guard-1890 family: a union of predicates strictly
narrower than the population, where every sweep reports clean forever.

DESIGN — family-conformant except where the family IS the defect (rb-5784)
-------------------------------------------------------------------------
A lone non-conforming member is a likelier defect shape than a family-wide
design error, so this script copies the siblings wherever they agree:

  - POPULATION: imported, never re-derived. `_find_user_participant_goals` from
    `audit-user-to-agent.py` is the single source of truth for "non-terminal
    goal carrying user" and its predicate was already widened from
    `participants == ["user"]` to `"user" in participants` (the guard-1802 fix;
    the narrow form had a live candidate set of ZERO against 28 real goals).
    Duplicating it here would re-open exactly that hole on a second predicate.
  - COOLDOWN: shared + durable board scan, copied from handoff-aging-check.py
    (g-115-1531). The escalation's board record IS the cooldown record — one
    artifact, no ledger to keep in sync (communication-clarity rule 5). This
    replaces per-agent WM `proactive_escalation_log`, which had two
    production-confirmed bugs: N-agent duplication (6 agents each kept their own
    log and all escalated the same item — ~30 posts for ~7 handoffs on
    2026-06-18) and non-durability (a WM reset wiped it and re-fired).
  - FAIL-OPEN at every layer, exit 0 always. Delivery is ADDITIVE, never a
    destructive mutation, so a half-view means fewer escalations this run,
    recoverable next sweep. Aborting the precheck would be strictly worse.

  - DELIVERY: **email, not a board post.** This is the deliberate divergence and
    the entire point of the script. The actionable party is the human.

Two things the delivery path must get right, both load-bearing:

  1. CATEGORY MUST BE `blocker`. notify-user Step 1.5 runs an approval-request
     gate that refuses sends whose text asks the user to do something the agent
     could do itself. This population asks the user to act BY CONSTRUCTION, so an
     `info`/`update` send would trip that gate and be refused — silently
     reproducing the exact silence this script exists to fix. `blocker` is
     exempt (it is a status report about a real block, and it routes through
     CREATE_BLOCKER's capability gate at creation instead). `blocker` also
     selects the SendErrorAlert shape, so email-send.sh needs `--error`.
  2. `deliberate` GOALS ARE REPORTED, NOT ESCALATED. A goal whose origin_signal
     marks deliberate user routing (e.g. asp-314's park, "DO-NOT-TOUCH") is
     counted and labelled but never emailed — emailing it would nag the user
     about a choice they made on purpose. Tagged rather than dropped, because a
     silent skip is indistinguishable from a clean sweep, which is the failure
     this whole lane exists to correct.

Called by aspirations-precheck. Dry-run by default; --apply to actually send.

Usage:
    py -3 user-blocker-escalation-check.py [--apply] [--escalate-hours N]
                                           [--agent <name>]                # default $MIND_AGENT
                                           [--board-escalation-log <path>] # tests only
                                           [--no-board] [--no-email]       # tests only
"""

import argparse
import datetime as dt
import importlib.util
import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _runtime_bash import bash_cmd  # guard-580: never hand-author a bare "bash" argv[0]

DEFAULT_ESCALATE_HOURS = 48.0
BOARD_TAG = "user-blocker-escalated"


def _load_population_predicate():
    """Import `_find_user_participant_goals` from audit-user-to-agent.py.

    The filename is hyphenated so a plain import cannot reach it; loading via
    spec_from_file_location is established house pattern here (anchor-tripwire,
    guardrail-check, bare-bash-authoring-gate and others do the same).

    Importing rather than re-implementing is deliberate: that function IS the
    single source of truth for this population, and a second copy of the
    predicate is precisely how guard-1802's narrow-predicate hole appeared in
    the first place.

    FAIL-OPEN: returns None if unavailable, and the caller degrades to an empty
    candidate set with a stderr note rather than raising.
    """
    target = SCRIPT_DIR / "audit-user-to-agent.py"
    try:
        spec = importlib.util.spec_from_file_location("_aut_population", target)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return getattr(mod, "_find_user_participant_goals", None)
    except Exception as exc:
        sys.stderr.write(
            "user-blocker-escalation: could not load population predicate from %s (%s) "
            "— fail-open, zero candidates this sweep\n" % (target, exc))
        return None


def _read_escalate_hours(cli_value) -> float:
    """CLI wins; else config; else DEFAULT_ESCALATE_HOURS. Never raises."""
    if cli_value is not None:
        return float(cli_value)
    try:
        import yaml  # noqa: PLC0415
        cfg_path = CORE_ROOT / "config" / "aspirations.yaml"
        with open(cfg_path, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        block = cfg.get("user_blocker_escalation") or {}
        val = block.get("escalate_hours")
        if val is not None:
            return float(val)
    except Exception:
        pass
    return DEFAULT_ESCALATE_HOURS


def _age_hours(ts, now: dt.datetime):
    """Hours between an ISO-ish timestamp and now. None when unparseable.

    Naive timestamps throughout this framework are UTC wall time by fiat
    (CLAUDE.md Naming Rules), so a naive-vs-naive subtraction is correct here.
    """
    if not ts or not isinstance(ts, str):
        return None
    raw = ts.strip().replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            parsed = dt.datetime.strptime(raw[:len(fmt) + 2].rstrip("T"), fmt)
            return (now - parsed).total_seconds() / 3600.0
        except Exception:
            continue
    try:
        parsed = dt.datetime.fromisoformat(raw)
        if parsed.tzinfo is not None:
            parsed = parsed.replace(tzinfo=None)
        return (now - parsed).total_seconds() / 3600.0
    except Exception:
        return None


def _goal_age_hours(goal: dict, now: dt.datetime):
    """Age of the BLOCK, preferring blocked_since over creation.

    A goal blocked yesterday but filed a month ago has been *waiting on the
    user* for a day, not a month — escalating on creation age would fire
    instantly on every long-lived goal the moment it first blocks.
    """
    for field in ("blocked_since", "blocked_at", "created_at", "created"):
        age = _age_hours(goal.get(field), now)
        if age is not None:
            return age, field
    return None, None


def _escalate_window_str(escalate_hours: float) -> str:
    """board-read --since window, rounded UP so it covers the full cooldown."""
    return "%dh" % (int(math.ceil(escalate_hours)) + 1)


def _read_recent_escalations(escalate_hours: float, now: dt.datetime,
                             board_log_path: Path = None) -> set:
    """Goal_ids already escalated within the window, from ANY agent.

    The board post made by `_post_board` IS the cooldown record — shared (all
    agents read one board) and durable (survives WM resets). See the module
    docstring for the two per-agent-WM bugs this shape fixes.

    FAIL-OPEN: any read failure yields an empty set, so everything is eligible.
    Additive over-delivery beats silent suppression for THIS sweep specifically,
    because silent suppression is the defect being fixed.
    """
    posts = []
    if board_log_path is not None:
        try:
            with open(board_log_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            posts = data if isinstance(data, list) else []
        except Exception:
            posts = []
    else:
        try:
            proc = subprocess.run(
                bash_cmd(SCRIPT_DIR / "board-read.sh",
                         "--channel", "coordination",
                         "--type", "status",
                         "--since", _escalate_window_str(escalate_hours),
                         "--json"),
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=30,
            )
            if proc.returncode == 0:
                for line in (proc.stdout or "").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        posts.append(json.loads(line))
                    except Exception:
                        continue
            else:
                sys.stderr.write(
                    "user-blocker-escalation: board-read.sh exit=%d stderr=%s — "
                    "fail-open (no cooldown this sweep)\n"
                    % (proc.returncode, (proc.stderr or "").strip()[:200]))
        except Exception as exc:
            sys.stderr.write(
                "user-blocker-escalation: board-read.sh exception (%s) — fail-open\n" % exc)

    recent = set()
    for p in posts:
        if not isinstance(p, dict):
            continue
        tags = p.get("tags") or []
        if BOARD_TAG not in tags:
            continue
        age = _age_hours(p.get("timestamp") or p.get("ts"), now)
        if age is None or age >= escalate_hours:
            continue  # outside the window (or unparseable) — does not suppress
        for t in tags:
            if isinstance(t, str) and t.startswith("g-"):
                recent.add(t)
    return recent


def _compose_digest_body(batch: list, escalate_hours: float) -> str:
    """ONE body covering every aged goal, oldest first.

    Per-goal detail is deliberately SHORT (title + what it blocks + a clipped
    description). A digest that reproduces 14 full goal descriptions is not a
    digest; the goal_id is the handle for anyone who wants the full record.

    Descriptions are clipped but never paraphrased — a paraphrase is where a
    concrete "connect the plugin on DEV" ask degrades into something the reader
    cannot act on, which is the failure mode that produced this whole lane.
    """
    ordered = sorted(batch, key=lambda t: -(t[2] or 0.0))
    lines = [
        "%d goal(s) have been waiting on you past %.0fh with no prior escalation."
        % (len(ordered), escalate_hours),
        "",
        "WHY YOU ARE HEARING ABOUT IT NOW:",
        "These goals carry `user` in participants, so part of each needs a human.",
        "Until g-115-3926 no escalation path covered this population at all — the",
        "three existing aged-work sweeps all post to the coordination board, which",
        "is agent-to-agent, so a block whose condition is a HUMAN action could",
        "accumulate board traffic for days without ever reaching you. It did.",
        "",
        "Oldest first:",
    ]
    for idx, (cand, goal, age, age_field) in enumerate(ordered, 1):
        gid = goal.get("id", "") or "(unknown)"
        desc = (goal.get("description") or "").strip()
        blocks = goal.get("blocks") or goal.get("blocking") or []
        lines += [
            "",
            "%d. [%s] %.0fh — %s" % (idx, gid, age or 0.0,
                                     (goal.get("title") or "").strip()),
            "   aspiration=%s priority=%s aged-from=%s" % (
                cand.get("aspiration_id") or "?",
                goal.get("priority") or "unset", age_field or "?"),
        ]
        if blocks:
            lines += ["   blocks: %s" % ", ".join(str(b) for b in blocks)]
        if desc:
            clipped = " ".join(desc.split())[:400]
            lines += ["   %s%s" % (clipped, "..." if len(desc) > 400 else "")]
    lines += [
        "",
        "Each goal above is now on a %.0fh per-goal cooldown, so this digest will"
        % escalate_hours,
        "not repeat them. Newly aged goals will appear in a future digest.",
        "",
        "If any of these no longer needs you, say so and the `user` participant",
        "gets dropped — that is a one-way door inside the loop, so it is not done",
        "automatically (reclaim-routed-work.md lane P).",
    ]
    return "\n".join(lines)


def _send_digest_email(agent: str, batch: list, escalate_hours: float,
                       no_email: bool) -> tuple:
    """Deliver ONE digest covering the whole batch. Returns (ok, detail).

    Category is `blocker` — REQUIRED, not stylistic. See docstring note 1: any
    other category is refused by notify-user Step 1.5's approval-request gate
    for this population, which would silently recreate the original silence.
    `blocker` also selects SendErrorAlert, hence `--error` on the sender.
    """
    oldest = max((t[2] or 0.0) for t in batch)
    subject = "%d goal(s) waiting on you (oldest %.0fh)" % (len(batch), oldest)
    if no_email:
        return True, "no_email"

    body = _compose_digest_body(batch, escalate_hours)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(body)
            tmp_path = fh.name

        built = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "notify-build-payload.py"),
             "--agent", agent, "--category", "blocker",
             "--subject", subject, "--message-file", tmp_path],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60,
        )
        if built.returncode != 0 or not (built.stdout or "").strip():
            return False, "payload_build_rc=%d %s" % (
                built.returncode, (built.stderr or "").strip()[:200])

        # world/ is an external path; resolve it rather than passing a bare
        # world/... arg, which is NOT hook-rewritten for Bash (path-resolution.md).
        world = os.environ.get("WORLD_PATH") or os.environ.get("MIND_WORLD") or ""
        if not world:
            try:
                probe = subprocess.run(
                    bash_cmd("-c", 'source "%s/_paths.sh" >/dev/null 2>&1; printf "%%s" "$WORLD_PATH"'
                             % SCRIPT_DIR),
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=30,
                )
                world = (probe.stdout or "").strip()
            except Exception:
                world = ""
        sender = Path(world) / "scripts" / "email-send.sh" if world else None
        if sender is None or not sender.exists():
            return False, "email-send.sh not resolvable (WORLD_PATH=%r)" % world

        sent = subprocess.run(
            bash_cmd(sender, "--error"),
            input=built.stdout, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
        if sent.returncode != 0:
            return False, "email_rc=%d %s" % (
                sent.returncode, (sent.stderr or "").strip()[:200])
        return True, "sent"
    except Exception as exc:
        return False, "exception:%s" % exc
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def _post_board(goal: dict, age_hours: float, no_board: bool) -> tuple:
    """Post the coordination-board record that doubles as the shared cooldown.

    Posted even though delivery is by email: peers must be able to see the human
    was already told (otherwise every agent re-escalates), and this post IS the
    cooldown record read by `_read_recent_escalations`.
    """
    gid = goal.get("id", "") or ""
    msg = ("User-blocker escalated %.0fh: %s [%s] — emailed the user; "
           "no re-send this cooldown window." % (
               age_hours, (goal.get("title") or "")[:90], gid))
    if no_board:
        return True, "no_board"
    try:
        proc = subprocess.run(
            bash_cmd(SCRIPT_DIR / "board-post.sh",
                     "--channel", "coordination",
                     "--type", "status",
                     "--tags", "%s,%s" % (BOARD_TAG, gid)),
            input=msg, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
        if proc.returncode != 0:
            return False, "board_rc=%d %s" % (
                proc.returncode, (proc.stderr or "").strip()[:200])
        return True, (proc.stdout or "").strip()[:80]
    except Exception as exc:
        return False, "exception:%s" % exc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true",
                    help="actually send + post (default is dry-run)")
    ap.add_argument("--escalate-hours", type=float, default=None)
    ap.add_argument("--agent", default=os.environ.get("MIND_AGENT", ""))
    ap.add_argument("--board-escalation-log", default=None, help="tests only")
    ap.add_argument("--no-board", action="store_true", help="tests only")
    ap.add_argument("--no-email", action="store_true", help="tests only")
    ap.add_argument("--world-aspirations", default=None, help="tests only")
    ap.add_argument("--agent-aspirations", default=None, help="tests only")
    args = ap.parse_args()

    now = dt.datetime.now()
    escalate_hours = _read_escalate_hours(args.escalate_hours)
    agent = args.agent or "unknown"

    find_pop = _load_population_predicate()
    candidates = []
    if find_pop is not None:
        sources = []
        if args.world_aspirations:
            sources.append(("world", Path(args.world_aspirations)))
        if args.agent_aspirations:
            sources.append(("agent", Path(args.agent_aspirations)))
        if not sources:
            try:
                import _paths  # noqa: PLC0415
                sources.append(("world", Path(_paths.WORLD_DIR) / "aspirations.jsonl"))
                sources.append(("agent", Path(_paths.AGENT_DIR) / "aspirations.jsonl"))
            except Exception as exc:
                sys.stderr.write(
                    "user-blocker-escalation: path resolution failed (%s) — fail-open\n" % exc)
        for label, path in sources:
            try:
                candidates.extend(find_pop(label, path))
            except Exception as exc:
                sys.stderr.write(
                    "user-blocker-escalation: population scan failed for %s (%s) — "
                    "continuing with other sources\n" % (label, exc))

    recent = _read_recent_escalations(
        escalate_hours, now,
        Path(args.board_escalation_log) if args.board_escalation_log else None)

    scanned = len(candidates)
    eligible, applied, results = 0, 0, []
    skipped_deliberate = skipped_cooldown = skipped_young = 0
    skipped_uncomputable = 0
    batch = []  # eligible goals, delivered as ONE digest (see below)

    for cand in candidates:
        goal = cand.get("goal") or {}
        gid = goal.get("id") or ""
        age, age_field = _goal_age_hours(goal, now)

        if cand.get("deliberate"):
            # Reported, never emailed — nagging a deliberate choice is the wrong
            # correction. Counted so the skip is visible, not silent.
            skipped_deliberate += 1
            results.append({"goal_id": gid, "action": "skip",
                            "reason": "deliberate_user_routing",
                            "age_hours": age})
            continue
        if age is None:
            # AN UNCOMPUTABLE AGE IS NOT A YOUNG ONE. Folding it into
            # below_threshold is the single most misleading label available: a
            # reader scanning the output sees "too new to escalate yet" and moves
            # on, while the goal is structurally incapable of EVER reaching the
            # threshold — it carries no parseable timestamp in any of the four
            # fields _goal_age_hours tries, so its age is not small, it is
            # undefined. Skipping is still correct (guard-420: no datetime
            # arithmetic on a null; fail-open), but the skip must be NAMED.
            # Measured 2026-07-30 (): 16 of 796 open world goals carry
            # no created_at — 3 HIGH, one of them the unblocking goal for a live
            # outage — and this branch reported every one of them as fine.
            # guard-1986: a not-checkable case folded into a substantive verdict
            # is an all-clear whose cleanliness has nothing to do with the data.
            skipped_uncomputable += 1
            results.append({"goal_id": gid, "action": "skip",
                            "reason": "age_uncomputable",
                            "detail": "no parseable blocked_since / blocked_at / "
                                      "created_at / created — age is undefined, "
                                      "so this goal can never age into escalation",
                            "age_hours": None})
            continue
        if age < escalate_hours:
            skipped_young += 1
            results.append({"goal_id": gid, "action": "skip",
                            "reason": "below_threshold", "age_hours": age})
            continue
        if gid in recent:
            skipped_cooldown += 1
            results.append({"goal_id": gid, "action": "skip",
                            "reason": "cooldown_active", "age_hours": age})
            continue

        eligible += 1
        rec = {"goal_id": gid, "age_hours": round(age, 1),
               "age_field": age_field,
               "aspiration_id": cand.get("aspiration_id"),
               "shape": cand.get("shape"), "action": "would_escalate"}
        batch.append((cand, goal, age, age_field))
        results.append(rec)

    # ONE DIGEST, NOT N EMAILS (reclaim-routed-work.md rule 5: "Batch them into
    # a digest for the next user check-in"). The first live dry-run returned 14
    # eligible goals aged 78-103h — a per-goal send would have delivered 14
    # separate emails in one sweep. For a sweep whose entire purpose is to make
    # the user aware of a backlog, that volume is self-defeating: it trains the
    # recipient to filter the sender, which is a louder version of the silence
    # this script exists to fix. Cooldown stays PER-GOAL (one board record each)
    # so a goal escalated today is excluded from tomorrow's digest while newly
    # aged goals still surface.
    if args.apply and batch:
        ok_mail, mail_detail = _send_digest_email(agent, batch, escalate_hours,
                                                  args.no_email)
        for cand, goal, age, age_field in batch:
            gid = goal.get("id") or ""
            target = next((r for r in results if r.get("goal_id") == gid), None)
            # Record the per-goal cooldown ONLY on successful delivery: a cooldown
            # for an email that never sent would suppress the retry and reproduce
            # exactly the silence being fixed.
            if ok_mail:
                ok_board, board_detail = _post_board(goal, age, args.no_board)
                if target is not None:
                    target.update({"action": "escalated", "email": mail_detail,
                                   "board": board_detail, "board_ok": ok_board})
                applied += 1
            else:
                if target is not None:
                    target.update({"action": "failed", "email": mail_detail,
                                   "board": "not_posted_no_cooldown_recorded"})
        if not ok_mail:
            sys.stderr.write(
                "user-blocker-escalation: digest delivery FAILED (%s) — no cooldowns "
                "recorded for %d goal(s), will retry next sweep\n"
                % (mail_detail, len(batch)))

    print(json.dumps({
        "agent": agent,
        "now": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "escalate_hours": escalate_hours,
        "dry_run": not args.apply,
        "predicate_loaded": find_pop is not None,
        "scanned": scanned,
        "eligible": eligible,
        "applied": applied,
        "skipped": {"deliberate": skipped_deliberate,
                    "cooldown": skipped_cooldown,
                    "below_threshold": skipped_young,
                    "age_uncomputable": skipped_uncomputable},
        "results": results,
    }, indent=2))
    # A count buried in a JSON blob is still a silent skip — this lane exists
    # because a goal that cannot reach the user is invisible until someone
    # looks. Say it on stderr, where the precheck operator actually reads.
    if skipped_uncomputable:
        sys.stderr.write(
            "user-blocker-escalation: %d goal(s) have an UNCOMPUTABLE age (no "
            "parseable timestamp) and can never age into escalation — %s\n"
            % (skipped_uncomputable,
               ", ".join(r["goal_id"] for r in results
                         if r.get("reason") == "age_uncomputable")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
