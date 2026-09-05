#!/usr/bin/env python3
"""Decide whether a notification reaches the USER or a FLEET-SIDE destination.

Implements the user directive of 2026-08-10 (g-115-5825, verified sender):
"I actually do not want any emails anymore, if you can handle them. Like, if
there us an error, you handle it, i do not need to know."

Notification becomes the EXCEPTION, not the default. The decision is keyed to
the EXISTING capability-routing taxonomy (world/conventions/capability-routing.md
"Human-Only" section) rather than a new vocabulary, per the goal's first outcome.

THE FAIL-SAFE DIRECTION IS INVERTED HERE, DELIBERATELY, AND IT IS THE WHOLE
DESIGN. Everywhere else in this framework an ambiguous signal resolves toward
refusing. Here it resolves toward SENDING. The asymmetry:

  over-send  -> the user gets an email he did not need. Cost: mild annoyance,
                self-correcting (he says so, we tighten a rule).
  under-send -> a genuinely human-only alarm reaches NOBODY. Cost: measured on
                the sibling goal g-335-1097 the same day -- a removal half
                landed without its replacement half and six security alarms
                notified nobody for five days.

So `suppress` is returned only on positive proof that the class is fleet-
handleable. Every unknown category, every unparseable input, every match
against a human-only pattern SENDS. "He asked not to be told about things we
can handle; he did not ask to be unreachable."

Exit codes (CLI): 0 = send, 1 = suppress.
"""

import argparse
import re
import sys
from pathlib import Path

# This module is imported by siblings in core/scripts (which already have this
# dir on sys.path) AND run as a CLI from anywhere. post_suppression_breadcrumb
# needs to resolve board-post.sh and import _runtime_bash in BOTH shapes, so
# anchor on the file rather than on cwd or on the caller's sys.path.
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

SEND = 0
SUPPRESS = 1

# Categories that are, by their own definition, the human channel. These are
# not "allowed through" -- they ARE the replacement destination the directive
# needs in order to be safe:
#   decision-needed : asks for a judgement no script can make.
#   user-digest     : the BATCHED "goals waiting on you" list. Individual sends
#                     collapse into this, which is what makes suppression a
#                     re-route rather than a deletion.
#   reply           : an ANSWER to something the user himself asked ().
#
# WHY `reply` IS HERE, stated against the directive's own words rather than
# around them. Every other category names a message the FLEET decided to send;
# the directive suppresses those because "if you can handle them ... i do not
# need to know". A reply is the one shape the fleet did not initiate, and the
# thing to be handled IS reaching him — so the directive's condition is not
# merely unmet, it is unmeetable. Measured (): he emailed on
# 2026-08-15 asking "send me an email with exact instructions"; the verified
# answer dispatched as `info`, suppressed to the findings board, and nine days
# later sat on a board he does not read. Suppressing that is not handling it
# ourselves — it is declining an instruction.
#
# THIS IS NOT A RE-SEND DOOR, AND THE DISTINCTION IS LOAD-BEARING (guard-4722).
# That guardrail records the gate CORRECTLY suppressing a reply whose closing
# sentence had turned it into a permission request for work a standing grant
# already authorized, and its remedy is explicit: file the work as agent work,
# "not to re-send with a different category". Two interlocks keep `reply` from
# becoming exactly that door, and NEITHER lives in this function:
#   1. the dispatcher REQUIRES --in-reply-to, so the claim "he asked for this"
#      is cited in the body, the email and the outreach ledger — auditable, and
#      visible to him, which is what makes a wrong claim self-correcting;
#   2. `reply` is deliberately NOT exempt from notify-user Step 1.5, so a reply
#      that reads as an ask still meets the approval-request gate and is refused
#      with guard-4722's remedy. `blocker`/`completion`/`user-digest` are exempt;
#      `reply` must never join them.
# Removing either interlock re-opens the bypass this comment exists to close.
ALWAYS_SEND_CATEGORIES = frozenset({"decision-needed", "user-digest", "reply"})

# Status-report categories. Fleet-handleable BY DEFAULT -- they retrospectively
# state what happened, and the fleet already records that on the board and in
# goal records, which are read.
FLEET_HANDLEABLE_CATEGORIES = frozenset({"info", "update", "completion", "blocker"})

# Human-only classes, keyed to capability-routing.md § Human-Only. A match on
# ANY of these overrides the category and SENDS, because these name things no
# script in this deployment can do.
HUMAN_ONLY_PATTERNS = (
    (r"\bcredential\b|\bapi[ -]?key\b|\bsecret\b|\btoken\b|\bpassword\b|\brotate\b",
     "credential grant"),
    (r"\bbilling\b|\bspend\b|\binvoice\b|\bpayment\b|\bquota increase\b|\bnew (?:paid )?account\b",
     "billing / spend"),
    (r"\bphysical\b|\breboot the (?:box|machine|laptop)\b|\bhardware token\b|\bplug\b|\bcable\b",
     "physical hardware"),
    # `in the (?:aws )?console` added . A cloud provider's web console
    # IS the GUI case capability-routing.md already names ("Opening a GUI
    # application when no headless/CLI/API alternative exists"), so it belongs
    # in this existing class rather than in a new one.
    # FP measured over 12,171 live board messages (all 4 channels): 4 hits, all
    # read, ZERO false positives -- two of them are the principal himself saying
    # he can only do IAM "in the console".
    # A `\bconsole step\b` alternative was MEASURED AND REJECTED: 2 hits, 1 a
    # clear FP of the form "<product>-console step-0 timeout", i.e. a compound
    # that reads human-only but is really a hyphenated product noun followed by
    # a step index. (The verbatim string is pinned in the test file.)
    (r"\bopen (?:roblox )?studio\b|\bstudio application\b|\bGUI\b|\bkeystroke\b|\bin-game\b|\bplay a session\b"
     r"|\bin the (?:aws )?console\b",
     "GUI / live interaction"),
    (r"\bproduct direction\b|\bstrategic\b|\bToS\b|\bterms of service\b|\blegal\b|\bcontract\b|\bsign\b",
     "product / legal judgement"),
    (r"\bapprove\b|\bapproval\b|\bpermission to\b|\bauthoriz\w*\b|\bconsent\b",
     "approval no script can give"),
    # A captcha is the PUREST member of this list: a control engineered so that
    # only a human can pass it. capability-routing.md's human-only definition
    # already covers it in substance ("no headless/CLI/API alternative exists")
    # -- a captcha is that condition made deliberate rather than incidental.
    # Defeating one is circumventing a third party's access control, so the
    # fleet cannot handle it even in principle, which is exactly the predicate
    # this tuple encodes.
    # FP measured before shipping (guard-1790: run the naive predicate over the
    # live corpus and READ the hits): 3 hits across ~40k board messages, of
    # which 1 was a genuine false positive -- a message naming "adding a
    # captcha" as out-of-scope WORK rather than as a gate blocking the agent.
    # Kept broad anyway: this module's asymmetry (see the docstring) prices an
    # over-send at mild annoyance and an under-send at an alarm reaching nobody.
    # guard-1378 (a bare word-boundary anchor is insufficient) was checked and
    # does NOT apply -- it governs VERBS with strong ordinary senses
    # (pushed/committed/ran); these are single-sense nouns.
    (r"\b(?:re|h)?captcha\b|\banti[- ]?bot\b|\bbot[- ]?detection\b"
     r"|\bprove (?:you are|you're) (?:a )?human\b|\bhuman verification\b",
     "captcha / anti-bot"),
    # ---- IAM, added  -------------------------------------------
    # capability-routing.md's FIRST human-only bullet is "Granting credentials
    # or API keys the agent does not possess", and an IAM grant is the canonical
    # member of that class -- but ONLY when it targets an IAM USER.
    #
    # THE DISCRIMINATOR IS USER-vs-ROLE, NOT "is it IAM" (guard-3763, measured
    # 2026-08-14). A grant that modifies a ROLE's inline policy is HELD by a
    # principal this fleet can reach, so it is agent-doable and the directive
    # says NOT to email about it. Every USER-scoped IAM action is denied to that
    # same principal, including the reads -- so a USER grant is genuinely
    # human-only. A pattern keyed on "IAM permission grant" would collapse the
    # two and mail the user about work the fleet should just do, which is the
    # exact behaviour the 2026-08-10 directive asks to stop. Hence every
    # alternative below requires a USER target and none matches `role/`.
    #
    # The AccessDenied SHAPE does not decide this either (guard-3779): shape (a)
    # "no identity-based policy allows the action" is a MISSING GRANT, i.e.
    # addable, so it is fleet-handleable on its own. It becomes human-only only
    # when the denied principal is a USER -- which the ARN in the message
    # supplies, and which is why an ARN alternative is here rather than a
    # denial-text one.
    #
    # FP measured over 12,171 live board messages (general/findings/coordination/
    # decisions), every hit READ, per guard-1790 and the captcha entry's method.
    # As SHIPPED: this entry 54 hits (0.44%), the boundary entry below 16 hits
    # (0.13%), union 65 (0.53%). All read; all genuine IAM-capability-boundary
    # content, no ordinary-English hit surviving the path-boundary fix.
    # Two broader candidates were MEASURED AND REJECTED for breadth rather than
    # argued about: a bare `user/<name>` (127 hits, 1.0% -- it matches ordinary
    # paths like /home/user/x) and a generic `service:Action` token (798 hits,
    # 6.6%). guard-1923: anchored compounds with per-token precision measured
    # over the real corpus, never bare words.
    #
    # CORPUS CAVEAT, stated because it bounds what these numbers prove: the
    # board is fleet-internal prose, NOT the notification stream this gate
    # actually sees. It bounds the ambient frequency of the VOCABULARY, which is
    # what the captcha entry above measured too -- it is not a direct
    # false-positive rate on notifications, and no such corpus exists to measure.
    (r"\biam:(?:Put|Attach|Detach|Delete)UserPolicy\b"
     r"|arn:aws:iam::\d{12}:user/"
     # The `(?<![\w/])` on the proximity alternatives is load-bearing, not
     # tidiness: without it "IAM audit complete; the log is at
     # /home/user/agent/out.log" MATCHED, because a filesystem path contains the
     # literal `user/`. Found by probing the pattern against ordinary prose
     # rather than only against the strings it was written for.
     # The proximity window is [\s\S], NOT [^\n] (, measured
     # 2026-09-03). A REAL notification body is multi-line -- it puts the
     # principal, the action and the resource on separate lines -- so with a
     # newline-excluding window `IAM` (in the subject or a heading) and
     # `user/<name>` (on its own line) never co-occur inside one line and the
     # entry MISSES the exact message it exists to catch. Reproduced against
     # this module: decide("blocker", <real subject>, <multi-line body naming
     # user/ayoai-fleet-agent>) returned SUPPRESS; the same content on ONE line
     # returned SEND. That suppression sent a "we are blocked, only you can
     # grant this" alert to the agent-facing findings board instead of the user.
     # FP COST MEASURED over 13,114 live board messages by the method this file
     # already uses (guard-1790 -- run it and READ the hits): 49 -> 53 hits,
     # 0.37% -> 0.40%. All FOUR new hits read; all four are genuine IAM
     # capability-boundary reports on user/ayoai-fleet-agent that the old window
     # was MISSING because the principal and the `iam` token sat on different
     # lines. Zero false positives introduced.
     # THE TWO NEGATIVE CONTROLS SURVIVE THE WIDENING, and that is the load-
     # bearing check rather than the hit count: `role/` still does not match
     # (guard-3763 / rb-7835 -- role-vs-user is the discriminator and a ROLE
     # grant is agent-doable), and the `(?<![\w/])` lookbehind still blocks the
     # filesystem-path form -- "IAM audit complete.\nThe log is at
     # /home/user/agent/out.log" does NOT match even now that the window spans
     # newlines, which is the new risk surface this change creates.
     r"|\bIAM\b[\s\S]{0,120}?(?<![\w/])user/[\w.+=,@-]+"
     r"|(?<![\w/])user/[\w.+=,@-]+[\s\S]{0,120}?\bIAM\b",
     "IAM grant on a USER principal"),
    # A permissions-boundary EXPLICIT DENY is a different human-only shape and
    # deserves its own label in the audit line: an identity-policy addition
    # cannot overcome it, so "grant me the permission" is the wrong ask and the
    # ACTION itself routes to a human (guard-3779, rb-5174). Unlike the entry
    # above this is target-agnostic -- a boundary fences roles and users alike.
    # FP measured on the same 12,171-message corpus, all hits read: 14 for the
    # boundary token, 9 for `explicit deny`, every one a genuine AWS
    # permissions-boundary discussion, no ordinary-English sense observed.
    (r"\bpermissions?[ -]boundar(?:y|ies)\b|\bexplicit deny\b",
     "permissions-boundary fence"),
)


def _human_only_match(text):
    """Return the human-only class name matched by `text`, else None."""
    for pattern, label in HUMAN_ONLY_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return label
    return None


def decide(category, subject="", body=""):
    """Return (verdict, reason, destination).

    verdict     : SEND or SUPPRESS
    reason      : one line, suitable for a log or an audit ledger
    destination : where a suppressed notification must go instead. Never None
                  on SUPPRESS -- a suppression with no destination is the
                  g-335-1097 defect, so the caller can assert on this field.
    """
    cat = (category or "").strip().lower()
    combined = "%s\n%s" % (subject or "", body or "")

    if cat in ALWAYS_SEND_CATEGORIES:
        return (SEND, "category '%s' is the human channel by definition" % cat, None)

    human_class = _human_only_match(combined)
    if human_class is not None:
        return (SEND,
                "matched human-only class '%s' (capability-routing.md)" % human_class,
                None)

    if cat in FLEET_HANDLEABLE_CATEGORIES:
        return (SUPPRESS,
                "category '%s' is a status report and no human-only class matched" % cat,
                "world/board/findings.jsonl + a filed goal")

    # UNKNOWN category. Not in either set -- so nothing here proves it is
    # fleet-handleable, and the inverted fail-safe applies.
    return (SEND,
            "unknown category '%s' — cannot prove fleet-handleable, so sending"
            % (cat or "<empty>"),
            None)


def decide_and_log(category, subject="", body="", caller=""):
    """decide() plus the gate-firing row, as ONE call. NEVER raises.

    Returns (label, reason, destination) where label is "send" | "suppress".

    Every direct caller of the user-email transport needs the same three
    things -- the verdict, an audit row under `notify-user-routing-gate`, and a
    destination string for the SUPPRESS branch -- so they live here rather than
    being re-typed at each site. The reference implementation
    (inbox-alert-age-check.py, the first caller wired) hand-rolled all three;
    the second and third callers would have copied it, and a decision procedure
    duplicated N times drifts N ways.

    THE FAIL-SAFE APPLIES TO THIS FUNCTION'S OWN PLUMBING, not just to the
    decision. A caller wraps this in a try/except that sends on ImportError, so
    the one failure mode that would defeat the whole design is this function
    raising AFTER being imported -- the caller's guard has already been passed,
    and an exception mid-send is a notification that reaches nobody, which is
    the g-335-1097 defect arriving by a different road. So every internal
    failure here resolves to SEND, and the logging is best-effort: an audit row
    is worth less than a delivered alarm.
    """
    try:
        verdict, reason, destination = decide(category, subject, body)
    except Exception as exc:  # noqa: BLE001 - inverted fail-safe, see docstring
        return ("send",
                "routing gate raised %s — inverted fail-safe sends"
                % type(exc).__name__,
                None)

    label = "suppress" if verdict == SUPPRESS else "send"

    # Best-effort audit row. A missing/broken _gate_log must never change the
    # verdict: the gate id is registered in core/config/gates.yaml so the
    # retirement evaluator sees these firings, but an unwritten row costs
    # observability while a swallowed alarm costs the user.
    try:
        import _gate_log  # type: ignore

        _gate_log.log(
            "notify-user-routing-gate",
            "block" if label == "suppress" else "pass",
            caller=caller or "notification_routing_gate.decide_and_log",
            trigger_matched=(category or "<empty>"),
            payload=subject,
        )
    except Exception:  # noqa: BLE001 - observability is not the contract
        pass

    return (label, reason, destination)


def post_suppression_breadcrumb(subject, body="", caller="", reason="", tags=None):
    """Deliver a SUPPRESSED notification to its fleet-side destination.

    Returns (ok, detail). NEVER raises.

    `decide()` returns a destination STRING; this writes to it. The pair exists
    because a suppression whose destination is only described is exactly the
    g-335-1097 defect -- six security alarms "re-routed" to a place nothing
    delivered to, unnoticed for five days. Keeping the write beside the verdict
    means a caller cannot suppress and forget to re-route: the two calls sit
    together at every site, and a reviewer sees the missing half immediately.

    Deliberately the findings channel, not coordination: coordination is for
    "someone should claim this" and the fleet already has age-escalation sweeps
    for that. A suppressed user-notification is a FINDING -- something happened
    that a human would once have been told about -- and the findings channel is
    what /aspirations-select Phase 2.07 already scans every iteration.
    """
    if tags is None:
        tags = []
    tag_str = ",".join(["notification-suppressed"] + [t for t in tags if t])
    lines = [
        "Notification suppressed by notify-user-routing-gate — routed here "
        "instead of to the user.",
        "",
        "  caller:  %s" % (caller or "<unknown>"),
        "  reason:  %s" % (reason or "<none recorded>"),
        "  subject: %s" % (subject or "<none>"),
    ]
    if body:
        lines += ["", (body or "")[:2000]]
    msg = "\n".join(lines)

    try:
        import subprocess

        from _runtime_bash import bash_cmd  # guard-580: never a bare "bash" argv[0]

        proc = subprocess.run(
            bash_cmd(str(_SCRIPT_DIR / "board-post.sh"),
                     "--channel", "findings",
                     "--type", "finding",
                     "--tags", tag_str),
            input=msg, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
    except Exception as exc:  # noqa: BLE001 - never raise into a send path
        sys.stderr.write(
            "notification_routing_gate: breadcrumb post raised %s — the "
            "suppressed notification reached NO destination\n" % type(exc).__name__)
        return (False, "breadcrumb_exception:%s" % type(exc).__name__)

    if proc.returncode == 0:
        return (True, "posted")
    # Loud on failure: this is the branch where a suppression silently becomes a
    # deletion, so it must never be swallowed (guard-1673 — surface the actual
    # error, never a bare "(non-fatal)").
    sys.stderr.write(
        "notification_routing_gate: board-post.sh exit=%d stderr=%s — the "
        "suppressed notification reached NO destination\n"
        % (proc.returncode, (proc.stderr or "").strip()[:300]))
    return (False, "breadcrumb_nonzero:%d" % proc.returncode)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--category", required=True)
    ap.add_argument("--subject", default="")
    ap.add_argument("--body", default="")
    ap.add_argument("--quiet", action="store_true")
    # Shell-caller parity (). The python callers get the audit row and
    # the re-route destination from decide_and_log/post_suppression_breadcrumb;
    # without these flags a .sh caller could only get the verdict, and would have
    # to hand-roll the other two — which is how the decision procedure drifts.
    ap.add_argument("--caller", default="",
                    help="call site, recorded on the gate-firing row")
    ap.add_argument("--breadcrumb", action="store_true",
                    help="on SUPPRESS, post the fleet-side breadcrumb and exit 1 "
                         "ONLY if it landed; a failed breadcrumb exits 0 (send), "
                         "because a suppression with no destination is a deletion")
    ap.add_argument("--tags", default="",
                    help="comma-separated extra tags for the breadcrumb")
    args = ap.parse_args(argv)

    label, reason, destination = decide_and_log(
        args.category, args.subject, args.body, caller=args.caller)
    verdict = SUPPRESS if label == "suppress" else SEND

    if verdict == SUPPRESS and args.breadcrumb:
        ok, detail = post_suppression_breadcrumb(
            args.subject, args.body, caller=args.caller, reason=reason,
            tags=[t.strip() for t in args.tags.split(",") if t.strip()])
        if not ok:
            # Inverted fail-safe, end to end: the caller is about to skip its
            # send on our exit code, so an unlanded breadcrumb must flip us back
            # to SEND rather than silently drop the notification.
            if not args.quiet:
                print("SEND: breadcrumb failed (%s) — falling back to send" % detail)
            return SEND

    if not args.quiet:
        print("%s: %s" % ("SEND" if verdict == SEND else "SUPPRESS", reason))
        if verdict == SUPPRESS and destination:
            print("  route to: %s" % destination)
    return verdict


if __name__ == "__main__":
    sys.exit(main())
