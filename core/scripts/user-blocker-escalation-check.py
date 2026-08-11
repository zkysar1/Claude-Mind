#!/usr/bin/env python3
"""User-Blocker Escalation — the delivery-channel sibling of the aged-blocker family ().

Scan the world + agent goal queues for non-terminal goals that carry `user` in
`participants` and DELIVER ONE DIGEST EMAIL TO THE USER ON A FIXED CADENCE
(`user_blocker_escalation.cadence_hours`, default 72) — plus one
coordination-board record per digest that doubles as the shared schedule marker.

THE CADENCE IS A SCHEDULE, NOT A THRESHOLD (D2, g-115-4963 / g-115-4903)
-----------------------------------------------------------------------
Until 2026-08-08 this script fired whenever a goal CROSSED an age threshold
(`escalate_hours`, 48). The user replaced that by directive, verbatim: "I really
like the idea of once a week, but it may be too long. MAybe every 3 days?" — and
the stated reason is the whole design constraint: "Predictable is usually less
stressful than rare."

So the trigger is now THE CLOCK SINCE THE LAST DIGEST, and nothing else. Goal age
no longer gates membership at all; it is rendered per item because the reader
wants it, and it orders the list. Re-introducing ANY age predicate on membership
rebuilds the unpredictable ping in slower clothing, which is the specific failure
the directive names.

Two consequences that are easy to get wrong, both load-bearing:

  - THE PER-GOAL COOLDOWN IS GONE, deliberately. Under a schedule it would be
    actively harmful: a goal escalated in digest N would be suppressed from
    digest N+1 while STILL waiting on the user, so the list would drain toward
    empty and the all-clear below would fire while real work sat blocked. That
    converts a comfort signal into a false one. The SCHEDULE is the rate limit;
    one rate limiter, not two (communication-clarity rule 5). The user expects a
    set each time: "if we switch to every 3 days, then I presume there will be a
    set of these goals."
  - AN UNCOMPUTABLE AGE NO LONGER DISQUALIFIES. With no threshold there is
    nothing for a null age to fail, so those goals are now IN the digest with the
    age line saying so. This retires the g-115-4084 hole for this lane at the
    root rather than by naming it: 16 of 796 open world goals carry no
    created_at, 3 of them HIGH.

AN EMPTY LIST IS A SEND, NOT A SKIP (D3, same directive)
--------------------------------------------------------
VERBATIM: "And yes, I do like this, it would give me comfort". When the list is
empty the script sends the SHORT all-clear. This is a deliberate reversal of the
no-news-is-good-news default — the user has said silence is worse for them, so
the quiet case is the one that must still arrive.

This is the branch that silently regresses: every instinct in a sweep script says
"nothing to report, return early", and the regression is invisible because a
skipped send and a healthy quiet week produce the same empty inbox. `main` has no
`if batch:` guard around delivery for exactly that reason, and
`test_empty_list_sends_an_all_clear_not_a_noop` exists to keep one from
reappearing.

NOT BUILT, BY DIRECTIVE (D4): reply-to-close, or any per-item reply affordance.
VERBATIM: "No, I do not want this, because I want more than one goal per email".
The batch is designed around MANY goals per email; do not narrow it toward one.

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
    py -3 user-blocker-escalation-check.py [--apply] [--cadence-hours N]
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

DEFAULT_CADENCE_HOURS = 72.0  # D2: every 3 days, fixed schedule
BOARD_TAG = "user-blocker-escalated"   # kept so existing peer greps still match
DIGEST_TAG = "user-digest-sent"        # the schedule marker — see _read_last_digest_age
# Bound on how many goal ids ride along as board tags. The ids are also in the
# post BODY (unbounded), so this caps tag sprawl without dropping information.
MAX_TAGGED_GOAL_IDS = 25


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


def _read_cadence_hours(cli_value) -> float:
    """CLI wins; else config; else DEFAULT_CADENCE_HOURS. Never raises.

    Reads `cadence_hours`. The predecessor key `escalate_hours` is deliberately
    NOT accepted as a fallback: it named an age threshold, and this number is a
    schedule interval, so honouring the old key would silently run the new
    mechanism on a value chosen for the old one (48h instead of 72h) with
    nothing to show that it happened. A stale config should land on the stated
    default, not on a number that means something else.
    """
    if cli_value is not None:
        return float(cli_value)
    try:
        import yaml  # noqa: PLC0415
        cfg_path = CORE_ROOT / "config" / "aspirations.yaml"
        with open(cfg_path, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        block = cfg.get("user_blocker_escalation") or {}
        val = block.get("cadence_hours")
        if val is not None:
            return float(val)
    except Exception:
        pass
    return DEFAULT_CADENCE_HOURS


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


def _cadence_window_str(cadence_hours: float) -> str:
    """board-read --since window, rounded UP so it covers the full cadence.

    cadence+1h is deliberate: a digest OLDER than the window falls out of the
    scan and reads as "never sent", which returns DUE — and at that age it is
    genuinely due, so the rounding error cannot manufacture an early send.
    """
    return "%dh" % (int(math.ceil(cadence_hours)) + 1)


def _schedule_verdict(read_ok: bool, hours_since_last, cadence_hours: float):
    """Pure. -> (due: bool, reason: str). The whole trigger lives here.

    Kept pure and separate so every branch is directly testable without a board,
    an email, or a queue — same shape as `reducer_self_fence.decide`.

    THE FAIL DIRECTION IS INVERTED HERE, AND ONLY HERE. Every other layer of
    this script fails OPEN (a broken layer means fewer emails, never an aborted
    precheck) because delivery is additive. A SCHEDULE gate cannot inherit that:
    "I could not read when I last sent" fails open to "so send now", and this
    script runs from aspirations-precheck on EVERY loop iteration — so a
    persistent board-read failure would mail the user on every iteration, all
    day. That is unbounded, outward-facing and irreversible.

    Failing closed costs one sweep, recovered on the next (minutes, not days),
    and the miss is announced on stderr rather than swallowed. The asymmetry is
    decisive, so the schedule refuses to send on an unreadable clock.
    """
    if not read_ok:
        return False, "schedule_unreadable"
    if hours_since_last is None:
        return True, "no_prior_digest"
    if hours_since_last < cadence_hours:
        return False, "within_cadence"
    return True, "cadence_elapsed"


def _read_last_digest_age(cadence_hours: float, now: dt.datetime,
                          board_log_path: Path = None):
    """Age in hours of the most recent digest send. -> (read_ok, age_or_None).

    The board post made by `_post_digest_board_record` IS the schedule marker —
    shared (all agents read one board) and durable (survives WM resets, and
    survives the reducer moving between boxes, which a machine-local marker file
    would not). See the module docstring for the two per-agent-WM bugs this
    shape fixes.

    `read_ok=False` means the board could not be read at all, which is NOT the
    same as "no digest has been sent" — see `_schedule_verdict` for why that
    distinction is the one thing standing between a board outage and a mailbox
    full of duplicate digests.

    Keys on DIGEST_TAG, not BOARD_TAG. The predecessor design posted one
    BOARD_TAG record per escalated GOAL, and those posts are still in board
    history — reusing that tag would read a pre-cutover per-goal post as "a
    digest was already sent" and suppress the first digest under the new cadence
    for up to a full window, silently.
    """
    posts = []
    read_ok = True
    if board_log_path is not None:
        # Test seam: a missing/empty file means "no prior digest", not a failed
        # read. Tests that need the failed-read branch call _schedule_verdict.
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
                         "--since", _cadence_window_str(cadence_hours),
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
                read_ok = False
                sys.stderr.write(
                    "user-blocker-escalation: board-read.sh exit=%d stderr=%s — "
                    "SCHEDULE UNREADABLE, no digest this sweep (retries next)\n"
                    % (proc.returncode, (proc.stderr or "").strip()[:200]))
        except Exception as exc:
            read_ok = False
            sys.stderr.write(
                "user-blocker-escalation: board-read.sh exception (%s) — "
                "SCHEDULE UNREADABLE, no digest this sweep (retries next)\n" % exc)

    newest = None
    for p in posts:
        if not isinstance(p, dict):
            continue
        if DIGEST_TAG not in (p.get("tags") or []):
            continue
        age = _age_hours(p.get("timestamp") or p.get("ts"), now)
        if age is None:
            # An unparseable timestamp on a digest record cannot date the last
            # send. Treating it as "no send" would return DUE and could mail on
            # every sweep, so it degrades the READ instead — fail-closed, same
            # reasoning as _schedule_verdict.
            read_ok = False
            sys.stderr.write(
                "user-blocker-escalation: digest board record has an unparseable "
                "timestamp (%r) — SCHEDULE UNREADABLE, no digest this sweep\n"
                % (p.get("timestamp") or p.get("ts")))
            continue
        if newest is None or age < newest:
            newest = age
    return read_ok, newest


def _compose_all_clear_body(cadence_hours: float) -> str:
    """The SHORT all-clear (D3). Sent when the list is empty — never skipped.

    Deliberately short, because its whole job is to be reassuring at a glance:
    "And yes, I do like this, it would give me comfort". A long empty-state email
    makes the reader hunt for the ask that is not there, which is the opposite of
    the effect asked for.

    It says WHEN the next one arrives, because the value the user named is
    predictability — an all-clear that does not date the next check-in leaves
    them wondering whether the schedule is still running.

    WORDING CONSTRAINT: this body must not read as a request. /notify-user Step
    1.5 refuses sends whose text asks the user to do something the agent could do
    itself ("waiting for you to", "user must", "please approve", ...). That gate
    is skill pseudocode and does not execute on this script's direct
    notify-build-payload path, so it is not what stops us today — but the phrasing
    stays clear of those shapes anyway, because a category or transport change
    later must not turn the comfort email into a refused one.
    """
    return "\n".join([
        "Nothing needs you right now.",
        "",
        "No open goal is waiting on a decision, an approval, or an unblock from",
        "you. This is the every-%.0f-hour check-in, and it goes out on that"
        % cadence_hours,
        "schedule whether the list has items or is empty — so a short note like",
        "this one means the queue is genuinely clear, not that the check was",
        "skipped.",
        "",
        "Next check-in in about %.0f hours." % cadence_hours,
    ])


def _compose_digest_body(batch: list, cadence_hours: float) -> str:
    """ONE body covering every goal waiting on the user, oldest first.

    ORDER IS THE POINT: the asks come first and the framework background last.
    That is not styling — the user's 2026-08-03 reply (g-115-4815) said this
    email "caused anxiety" and that they could not tell "what you need the user
    to do", and the body opened with six lines of our own archaeology before a
    single actionable word.

    Per-goal shape, in this order: title, NEEDS FROM YOU, age + creation date,
    what it blocks, quoted description. Detail stays bounded — a digest that
    reproduces 14 full descriptions is not a digest, and the goal_id is the
    handle for the full record — but bounded now means a 1200-char clip that
    reports how much it dropped, not a 400-char clip that silently landed in the
    middle of the background.

    Descriptions are clipped but never paraphrased — a paraphrase is where a
    concrete "connect the plugin on DEV" ask degrades into something the reader
    cannot act on, which is the failure mode that produced this whole lane.
    """
    ordered = sorted(batch, key=lambda t: -(t[2] or 0.0))
    lines = [
        "%d goal(s) need something from you. Oldest first."
        % len(ordered),
    ]
    for idx, (cand, goal, age, age_field) in enumerate(ordered, 1):
        gid = goal.get("id", "") or "(unknown)"
        desc = (goal.get("description") or "").strip()
        blocks = goal.get("blocks") or goal.get("blocking") or []
        lines += [
            "",
            "%d. [%s] %s" % (idx, gid, (goal.get("title") or "").strip()),
        ]
        # NEEDS-FROM-YOU line, FIRST per item and never omitted ( A4:
        # "I am unable to tell what you need the user to do"). `user_leg_scope`
        # is the field that answers it and it was rendered NOWHERE — measured
        # 2026-08-03: populated on 16 of 45 live user-carrying goals, with
        # exactly the right vocabulary (credential-grant, architecture-decision,
        # deployment-approval, restart-timing-approval-for-live-agent). When it
        # is ABSENT the line still prints and says so, rather than silently
        # dropping out: a missing answer the reader can see is actionable
        # ("nobody recorded why I am on this"), a missing LINE is not.
        scope = (goal.get("user_leg_scope") or "").strip()
        if scope:
            lines += ["   NEEDS FROM YOU: %s" % scope]
        else:
            lines += ["   NEEDS FROM YOU: not recorded on this goal — if the "
                      "description below does not make it obvious, that is our "
                      "bug, not yours; say so and we will fix the goal."]
        # AGE-vs-NOVELTY (A5: "are these newly assigned to me, or just old
        # ones"). Under the fixed cadence a goal that still needs the user
        # REAPPEARS in every digest until it is discharged, so an item may be
        # months old and also have been listed last time — the reader cannot tell
        # that from an age figure alone. Render the creation date beside the
        # aged-from clock. Deliberately NOT claiming "first time you have seen
        # this": that would need a full board-history scan this function does not
        # run, and an unmeasured claim here is exactly what the reader would rely
        # on (verify-before-assuming.md).
        created = str(goal.get("created_at") or goal.get("created") or "")[:10]
        # A NULL AGE IS RENDERED AS UNKNOWN, NEVER AS ZERO. `age or 0.0` would
        # print "waiting 0h" for a goal carrying no parseable timestamp, which
        # reads as brand-new and sorts the reader's attention away from it —
        # the same null-fused-into-a-substantive-verdict shape as ,
        # moved from the skip counter into the rendering.
        if age is None:
            clock = "   waiting (age unknown — no parseable timestamp on this goal)"
        else:
            clock = "   waiting %.0fh (aged-from=%s)" % (age, age_field or "?")
        lines += [
            "%s%s | aspiration=%s priority=%s" % (
                clock,
                (" | goal first created %s" % created) if created else "",
                cand.get("aspiration_id") or "?",
                goal.get("priority") or "unset"),
        ]
        if blocks:
            lines += ["   blocks: %s" % ", ".join(str(b) for b in blocks)]
        if desc:
            # `> ` marks this as QUOTED goal text, not agent assertion. Two
            # jobs, one character: the human reader sees whose words these are,
            # and finding-disproof-gate's strip_quoted() excludes it from the
            # universal/causal scan. Without it, a marker inside ANY member's
            # description refuses the whole digest and — because the caller
            # records no cooldowns on failure — wedges this entire lane on
            # every retry ().
            #
            # CLIP BUDGET RAISED 400 -> 1200 (A4: "these goals seem to be cut
            # off and not have all the information"). Descriptions in this fleet
            # routinely OPEN with diagnosis and context and reach the ask later,
            # so a 400-char head-clip was landing on the background and cutting
            # before the request. Still clipped — the docstring's point that a
            # digest reproducing full descriptions is not a digest still holds —
            # but the truncation now SAYS how much it dropped instead of a bare
            # ellipsis, so the reader knows whether to open the goal.
            budget = 1200
            flat = " ".join(desc.split())
            clipped = flat[:budget]
            if len(flat) > budget:
                lines += ["   > %s" % clipped,
                          "   > [...%d more characters — read the full goal by id "
                          "above]" % (len(flat) - budget)]
            else:
                lines += ["   > %s" % clipped]
    lines += [
        "",
        "This goes out every %.0f hours on a fixed schedule, whether the list has"
        % cadence_hours,
        "items or is empty. Anything still waiting on you will be on the next one",
        "too, so nothing here quietly falls off the list by being ignored.",
        "",
        "If any of these no longer needs you, say so and the `user` participant",
        "gets dropped — that is a one-way door inside the loop, so it is not done",
        "automatically (reclaim-routed-work.md lane P).",
        "",
        "--",
        "WHY YOU ARE HEARING ABOUT IT NOW (background, moved below the asks",
        "2026-08-03 — you told us this email led with our archaeology instead of",
        "your action):",
        "These goals carry `user` in participants, so part of each needs a human.",
        "Until g-115-3926 no escalation path covered this population at all — the",
        "three existing aged-work sweeps all post to the coordination board, which",
        "is agent-to-agent, so a block whose condition is a HUMAN action could",
        "accumulate board traffic for days without ever reaching you. It did.",
    ]
    return "\n".join(lines)


def _send_digest_email(agent: str, batch: list, cadence_hours: float,
                       no_email: bool) -> tuple:
    """Deliver ONE digest — or the all-clear when the batch is empty.

    Returns (ok, detail). THERE IS NO EARLY RETURN ON AN EMPTY BATCH: D3 makes
    the quiet case a send. The caller must not guard this with `if batch`.

    Category is `user-digest` for a populated digest and `info` for the
    all-clear. Both are SendInfoAlert-shaped, so `--error` never fires below.

    It was `blocker` for both until 2026-08-08, on the reasoning that `blocker`
    is exempt from notify-user Step 1.5's approval-request gate and this digest
    quotes arbitrary goal descriptions — one containing "user must" would refuse
    the whole send, and this caller records no cooldowns on failure, so the lane
    would wedge on every retry (the g-115-4594 shape). TWO SEPARATE THINGS WERE
    WRONG WITH THAT. They were found independently, on different boxes, and both
    are measured:

      1. That gate does not run on this path AT ALL (g-115-4963). Step 1.5 is
         pseudocode in notify-user/SKILL.md; this script invokes
         notify-build-payload.py and email-send.sh directly and never enters the
         skill, so the exemption was never the binding constraint it was taken
         for. The all-clear body is still written clear of approval-request
         phrasing, so a future transport change that DOES route through the
         skill cannot turn it into a refused send.
      2. `blocker` carries a second effect nobody weighed at this call site
         (g-115-4962): it is the ONE category emitting the SendErrorAlert shape,
         and SendErrorAlert has no render_structured at all. So the user's
         routine to-do list arrived under "AyoAi Error Alert" / "An error has
         been detected from <agent>", entire body in a red-bordered pink
         `white-space: pre-wrap` box. That IS user directive D1's "they come
         across as raw text", and very likely also the g-115-4815 "caused
         anxiety" report — which was read as a content-ordering problem and
         answered by reordering the body while the framing went unexamined.

    Finding 1 alone would leave `blocker` looking merely unnecessary; finding 2
    is what makes it actively wrong for this population. `user-digest` is
    SendInfoAlert-shaped like everything but `blocker`, AND is listed in Step
    1.5's exempt tuple — so it stays correct under either transport. The
    category and the `--error` flag must move together; see the comment on
    CATEGORY_TO_INFOTYPE.

    NOT DONE HERE, deliberately: the body is still prose in one Body field, so
    render_structured renders it as escaped text inside its card frame. Emitting
    per-goal `Sections` would produce real cards, but that changes WHAT the
    digest says and overlaps directive D5 ("shrink the input"), which is a
    separate goal. This change is the transport/routing defect only.
    """
    if batch:
        oldest = max((t[2] or 0.0) for t in batch)
        subject = "%d goal(s) waiting on you (oldest %.0fh)" % (len(batch), oldest)
        category, fenced = "user-digest", True
    else:
        subject = "Nothing waiting on you"
        category, fenced = "info", False
    if no_email:
        return True, "no_email"

    body = (_compose_digest_body(batch, cadence_hours) if batch
            else _compose_all_clear_body(cadence_hours))
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(body)
            tmp_path = fh.name

        # A populated digest QUOTES each goal's description verbatim (see
        # _compose_digest_body). Declare it so the disproof gate scans only what
        # THIS script authored — . The all-clear quotes nothing, so it
        # does not claim to.
        argv = ([sys.executable, str(SCRIPT_DIR / "notify-build-payload.py"),
                 "--agent", agent, "--category", category]
                + (["--fenced-quotes"] if fenced else [])
                + ["--subject", subject, "--message-file", tmp_path])
        built = subprocess.run(
            argv,
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

        # `--error` selects the SendErrorAlert transport and MUST track the
        # category the payload was built with — the builder emits the
        # SendErrorAlert shape only for `blocker`. NEITHER branch above uses
        # `blocker` any more (), so this never fires today. It is kept
        # as the invariant rather than deleted, because the mismatch it guards
        # fails SILENTLY: an info-shaped payload posted to the error endpoint
        # still reports a successful send.
        sender_argv = [sender] + (["--error"] if category == "blocker" else [])
        sent = subprocess.run(
            bash_cmd(*sender_argv),
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


def _post_digest_board_record(batch: list, cadence_hours: float,
                              no_board: bool) -> tuple:
    """ONE coordination-board record per digest. It IS the schedule marker.

    Posted even though delivery is by email, for two jobs in one artifact:
    peers must be able to see the human was already told (otherwise every agent
    re-escalates), and `_read_last_digest_age` reads this post's timestamp to
    decide when the next digest is due.

    ONE post, not one per goal — the predecessor posted per escalated goal, which
    made N board records per sweep AND made the schedule a per-goal question. The
    schedule is a property of the digest, so it gets exactly one record.

    Posted on the ALL-CLEAR too (empty batch). Skipping it there would leave the
    schedule marker unwritten on every quiet sweep, so the next sweep would read
    "no prior digest", find DUE, and send again — the all-clear would fire on
    every precheck iteration instead of every cadence window.
    """
    gids = [(g.get("id") or "") for _c, g, _a, _f in batch if g.get("id")]
    if gids:
        oldest = max((t[2] or 0.0) for t in batch)
        msg = ("User-participant digest SENT (%d goal(s), oldest %.0fh) — emailed "
               "the user; next digest due in %.0fh. Goals: %s"
               % (len(gids), oldest, cadence_hours, ", ".join(gids)))
    else:
        msg = ("User-participant digest SENT (all-clear, 0 goals waiting) — "
               "emailed the user; next digest due in %.0fh." % cadence_hours)
    if no_board:
        return True, "no_board"
    tags = [DIGEST_TAG, BOARD_TAG] + gids[:MAX_TAGGED_GOAL_IDS]
    try:
        proc = subprocess.run(
            bash_cmd(SCRIPT_DIR / "board-post.sh",
                     "--channel", "coordination",
                     "--type", "status",
                     "--tags", ",".join(tags)),
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
    # Renamed from --escalate-hours, which named an age threshold. The number
    # means something genuinely different now (a schedule interval), so the old
    # spelling is gone rather than aliased — an alias would let a caller keep
    # passing a threshold value and silently get a cadence.
    ap.add_argument("--cadence-hours", type=float, default=None)
    ap.add_argument("--agent", default=os.environ.get("MIND_AGENT", ""))
    ap.add_argument("--board-escalation-log", default=None, help="tests only")
    ap.add_argument("--no-board", action="store_true", help="tests only")
    ap.add_argument("--no-email", action="store_true", help="tests only")
    ap.add_argument("--world-aspirations", default=None, help="tests only")
    ap.add_argument("--agent-aspirations", default=None, help="tests only")
    args = ap.parse_args()

    now = dt.datetime.now()
    cadence_hours = _read_cadence_hours(args.cadence_hours)
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

    # THE SCHEDULE GATE — the only trigger. Read BEFORE the population loop so
    # the JSON reports it even on a not-due sweep, which is the common case.
    read_ok, hours_since_last = _read_last_digest_age(
        cadence_hours, now,
        Path(args.board_escalation_log) if args.board_escalation_log else None)
    due, schedule_reason = _schedule_verdict(read_ok, hours_since_last,
                                             cadence_hours)

    scanned = len(candidates)
    eligible, applied, results = 0, 0, []
    skipped_deliberate = 0
    unknown_age = 0
    batch = []  # every goal waiting on the user — delivered as ONE digest

    for cand in candidates:
        goal = cand.get("goal") or {}
        gid = goal.get("id") or ""
        age, age_field = _goal_age_hours(goal, now)

        if cand.get("deliberate"):
            # Reported, never emailed — nagging a deliberate choice is the wrong
            # correction. Counted so the skip is visible, not silent. This is the
            # ONLY membership filter left: under a fixed cadence, age decides
            # nothing (D2), so there is no threshold and no per-goal cooldown for
            # a goal to fall through.
            skipped_deliberate += 1
            results.append({"goal_id": gid, "action": "skip",
                            "reason": "deliberate_user_routing",
                            "age_hours": age})
            continue

        if age is None:
            # INCLUDED, not skipped. The predecessor dropped these because they
            # could never reach the age threshold; with no threshold there is
            # nothing left for a null age to fail, so the goal belongs in the
            # digest and the age line says the age is unknown. Retires the
            #  hole for this lane at the root (16 of 796 open world
            # goals carry no created_at, 3 of them HIGH) rather than by naming
            # it in a skip counter nobody reads.
            unknown_age += 1

        eligible += 1
        results.append({"goal_id": gid,
                        "age_hours": None if age is None else round(age, 1),
                        "age_field": age_field,
                        "aspiration_id": cand.get("aspiration_id"),
                        "shape": cand.get("shape"),
                        "action": "would_escalate" if due else "would_wait"})
        batch.append((cand, goal, age, age_field))

    # ONE DIGEST, NOT N EMAILS (reclaim-routed-work.md rule 5: "Batch them into
    # a digest for the next user check-in"). The first live dry-run returned 14
    # eligible goals aged 78-103h — a per-goal send would have delivered 14
    # separate emails in one sweep. For a sweep whose entire purpose is to make
    # the user aware of a backlog, that volume is self-defeating: it trains the
    # recipient to filter the sender, which is a louder version of the silence
    # this script exists to fix. D4 reinforces it from the user's side: "I want
    # more than one goal per email".
    #
    # THE CONDITION IS `due`, AND DELIBERATELY NOT `due and batch` (D3). An empty
    # batch sends the all-clear. Adding `and batch` here is the one-token change
    # that silently reverts this goal, because the regression is invisible from
    # the outside: a skipped send and a genuinely quiet window produce the same
    # empty inbox. `test_empty_list_sends_an_all_clear_not_a_noop` is the pin.
    ok_mail = None
    mail_detail = board_detail = None
    ok_board = None
    if args.apply and due:
        ok_mail, mail_detail = _send_digest_email(agent, batch, cadence_hours,
                                                  args.no_email)
        if ok_mail:
            # The board record is the schedule marker, so it is written ONLY on
            # successful delivery: marking the schedule for an email that never
            # sent would start the next window from a send that did not happen
            # and suppress the retry for a full cadence — the exact silence this
            # lane exists to fix.
            ok_board, board_detail = _post_digest_board_record(
                batch, cadence_hours, args.no_board)
            applied = len(batch)
            for r in results:
                if r.get("action") == "would_escalate":
                    r.update({"action": "escalated", "email": mail_detail,
                              "board": board_detail, "board_ok": ok_board})
        else:
            board_detail = "not_posted_no_schedule_marker_recorded"
            for r in results:
                if r.get("action") == "would_escalate":
                    r.update({"action": "failed", "email": mail_detail,
                              "board": board_detail})
            sys.stderr.write(
                "user-blocker-escalation: digest delivery FAILED (%s) — schedule "
                "marker NOT recorded for %d goal(s), will retry next sweep\n"
                % (mail_detail, len(batch)))

    print(json.dumps({
        "agent": agent,
        "now": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "cadence_hours": cadence_hours,
        "schedule": {
            "due": due,
            "reason": schedule_reason,
            "read_ok": read_ok,
            "hours_since_last_digest": (None if hours_since_last is None
                                        else round(hours_since_last, 1)),
            "hours_until_next": (
                None if (not read_ok or hours_since_last is None or due)
                else round(cadence_hours - hours_since_last, 1)),
        },
        "dry_run": not args.apply,
        "predicate_loaded": find_pop is not None,
        "scanned": scanned,
        "eligible": eligible,
        "applied": applied,
        "all_clear": due and not batch,
        # DELIVERY IS REPORTED SEPARATELY FROM `applied`, which counts GOALS.
        # On a sent all-clear `applied` is 0 — correct (zero goals escalated) and
        # indistinguishable from "nothing was sent" if it were the only signal.
        # For a lane whose entire defect was a send that nobody could tell had
        # not happened, that ambiguity is not acceptable in its own output.
        "delivery": {
            "attempted": args.apply and due,
            "shape": None if not (args.apply and due) else (
                "digest" if batch else "all_clear"),
            "ok": ok_mail,
            "detail": mail_detail,
            "board": board_detail,
            "board_ok": ok_board,
        },
        "skipped": {"deliberate": skipped_deliberate},
        "unknown_age": unknown_age,
        "results": results,
    }, indent=2))
    # A verdict buried in a JSON blob is still invisible — this lane exists
    # because a goal that cannot reach the user goes unnoticed until someone
    # looks. Say the two non-obvious outcomes on stderr, where the precheck
    # operator actually reads.
    if not read_ok:
        sys.stderr.write(
            "user-blocker-escalation: SCHEDULE UNREADABLE — no digest sent this "
            "sweep. This fails CLOSED on purpose (see _schedule_verdict); it "
            "retries next sweep, but a PERSISTENT read failure means the user "
            "stops hearing from this lane entirely.\n")
    if unknown_age:
        sys.stderr.write(
            "user-blocker-escalation: %d goal(s) carry no parseable timestamp — "
            "included in the digest with age reported as unknown: %s\n"
            % (unknown_age,
               ", ".join(r["goal_id"] for r in results
                         if r.get("action") != "skip"
                         and r.get("age_hours") is None)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
