""": notification becomes the EXCEPTION, keyed to capability-routing.

The load-bearing test in this file is test_unknown_category_sends. The whole
policy is a suppression rule, and the failure that motivated it (g-335-1097 --
six security alarms notifying nobody for five days) came from a suppression
that had no replacement. So every ambiguity must SEND, and every SUPPRESS must
name a destination.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from notification_routing_gate import decide, SEND, SUPPRESS  # noqa: E402


# --------------------------------------------------------------------------- #
# the directive's own example
# --------------------------------------------------------------------------- #

def test_the_directive_example_an_error_the_fleet_handles_is_suppressed():
    """'if there us an error, you handle it, i do not need to know.'"""
    verdict, _reason, dest = decide(
        "blocker", "Lambda failing", "retry loop exhausted, restarting service")
    assert verdict == SUPPRESS
    assert dest, "a SUPPRESS with no destination is the g-335-1097 defect"


@pytest.mark.parametrize("cat", ["blocker", "completion", "update", "info"])
def test_status_categories_suppress_and_always_name_a_destination(cat):
    verdict, _reason, dest = decide(cat, "routine subject", "routine body")
    assert verdict == SUPPRESS
    assert dest


# --------------------------------------------------------------------------- #
# the human channel -- these ARE the replacement, not an exemption
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("cat", ["decision-needed", "user-digest", "reply"])
def test_human_channel_categories_always_send(cat):
    verdict, _reason, dest = decide(cat, "anything", "anything")
    assert verdict == SEND
    assert dest is None


def test_the_worked_example_an_answer_he_asked_for_reaches_him():
    """ / . He emailed on 2026-08-15 asking 'send me an
    email with exact instructions'; the verified answer went out as `info`,
    suppressed to the findings board, and sat nine days on a board he does not
    read. The SAME text under `reply` must send.

    Both halves are asserted deliberately. The `info` half is the positive
    control: without it a future change that made everything send would pass
    the reply half while silently repealing the 2026-08-10 directive.
    """
    subject = "The IAM grant you asked about"
    body = ("The grant was already applied on 2026-08-16 under policy "
            "ayoai-fleet-least-priv v7. No action needed on your side.")
    assert decide("info", subject, body)[0] == SUPPRESS
    assert decide("reply", subject, body)[0] == SEND


def test_reply_is_not_a_bypass_of_the_status_categories():
    """Scope control. Adding a third ALWAYS_SEND member must not widen the set
    by accident — the directive's whole value is that status reports stay
    suppressed (guard-4722: the gate refusing a reply-shaped message was RIGHT
    when the message was really an ask)."""
    from notification_routing_gate import ALWAYS_SEND_CATEGORIES, FLEET_HANDLEABLE_CATEGORIES
    assert ALWAYS_SEND_CATEGORIES == {"decision-needed", "user-digest", "reply"}
    assert FLEET_HANDLEABLE_CATEGORIES == {"info", "update", "completion", "blocker"}
    assert not (ALWAYS_SEND_CATEGORIES & FLEET_HANDLEABLE_CATEGORIES)


# --------------------------------------------------------------------------- #
# human-only override beats the category
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("body,label", [
    ("needs a new API key from you", "credential"),
    ("this will incur new monthly spend", "billing"),
    ("please reboot the box physically", "physical"),
    ("remaining step: open Roblox Studio", "GUI"),
    ("awaiting your approval to proceed", "approval"),
    ("this is a product direction call", "product"),
    ("the delisting form is gated by a reCAPTCHA", "captcha"),
    ("submission sits behind a captcha", "captcha-bare"),
    ("the page uses anti-bot protection", "anti-bot"),
    ("you must prove you are human to continue", "prove-human"),
])
def test_human_only_override_sends_even_on_a_suppressed_category(body, label):
    verdict, reason, _dest = decide("blocker", "subject", body)
    assert verdict == SEND, f"{label} must reach the user: {reason}"


def test_captcha_is_human_only_and_names_its_class():
    """A captcha is the purest human-only class: a control ENGINEERED so that
    only a human passes it, so the fleet cannot handle it even in principle.

    Regression pin for the live gap (g-335-1233, 2026-08-14): a revenue-blocking
    delisting whose ONLY barrier was a reCAPTCHA classified as a fleet-handleable
    `blocker` status report and was suppressed -- i.e. the one class that most
    certainly needed a human was the class routed away from him. The bug was an
    absent pattern, not a wrong verdict, so nothing looked broken.
    """
    verdict, reason, dest = decide(
        "blocker",
        "site blocked by an upstream filter",
        "the delisting form is gated by a reCAPTCHA, so this step is yours",
    )
    assert verdict == SEND
    assert dest is None
    assert "captcha" in reason.lower(), reason


def test_captcha_pattern_does_not_swallow_unrelated_status_reports():
    """The FP direction. Measured over the live board corpus before shipping
    (guard-1790): the one false positive found was a message naming "adding a
    captcha" as out-of-scope WORK rather than as a gate. That FP is accepted --
    this module prices an over-send at annoyance -- but the pattern must not be
    so loose that ordinary reports start matching it.
    """
    for body in [
        "cleaned up 3 stale locks, no action needed",
        "the bot posted its nightly summary",          # 'bot' alone is not anti-bot
        "human review of the changelog is complete",   # 'human' alone is not verification
    ]:
        verdict, reason, dest = decide("blocker", "routine sweep", body)
        assert verdict == SUPPRESS, f"{body!r} should stay fleet-side: {reason}"
        assert dest


# --------------------------------------------------------------------------- #
# THE INVERTED FAIL-SAFE -- the reason this file exists
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("cat", ["", None, "weird-new-category", "BLOCKER!!"])
def test_unknown_category_sends(cat):
    """Ambiguity resolves toward SENDING, not toward refusing.

    Inverted relative to every other gate in the framework. Over-sending costs
    annoyance; under-sending loses a real alarm.
    """
    verdict, _reason, _dest = decide(cat, "subject", "body")
    assert verdict == SEND


def test_no_suppress_verdict_ever_returns_a_null_destination():
    """Exhaustive over the suppressing categories -- the  invariant."""
    for cat in ("blocker", "completion", "update", "info"):
        verdict, _reason, dest = decide(cat, "s", "b")
        if verdict == SUPPRESS:
            assert dest, f"{cat} suppressed with no destination"


def test_case_insensitivity_does_not_leak_a_suppression():
    """An uppercase category must not fall through to SUPPRESS by accident."""
    verdict, _reason, _dest = decide("BLOCKER", "s", "b")
    assert verdict == SUPPRESS   # normalised, still a status report
    verdict2, _r2, _d2 = decide("Decision-Needed", "s", "b")
    assert verdict2 == SEND      # normalised, still the human channel


# --------------------------------------------------------------------------- #
# : IAM. THE DISCRIMINATOR IS USER-vs-ROLE, NOT "is it IAM".
#
# Read this block before changing any pattern in it. The filed goal asserted
# that an IAM permission grant is human-only as a CLASS, and that four probe
# strings therefore all had to flip to SEND. Two of them must NOT flip, and the
# reason is measured, not stylistic:
#   guard-3763 -- a grant modifying a ROLE's inline policy is HELD by a
#     principal this fleet can reach, so it is agent-doable and the 2026-08-10
#     directive says do not email about it. Only USER-scoped IAM is denied.
#   guard-3779 -- AccessDenied shape (a) "no identity-based policy allows the
#     action" is a MISSING GRANT, i.e. addable; only shape (b), an explicit deny
#     in a permissions boundary, is a fence.
# A pattern keyed on the words "IAM permission grant" collapses both splits and
# mails the user about work the fleet should simply do.
# --------------------------------------------------------------------------- #

# The goal's own five probe strings, pinned with the verdict each MUST produce.
# Four were measured returning None before this change; two of those four stay
# SUPPRESS on purpose -- see the block comment above.
GOAL_PROBES = [
    ("A", "IAM permission grant needed / one IAM permission grant remains: "
          "logs:PutMetricFilter to user/ayoai-fleet-agent", SEND),
    ("B", "logs:PutMetricFilter and logs:DescribeMetricFilters are needed", SUPPRESS),
    ("C", "no identity-based policy allows the action", SUPPRESS),
    ("D", "Please grant the permission in the console.", SEND),
    ("E", "User: arn:aws:iam::891377285145:user/ayoai-fleet-agent is not authorized "
          "to perform: logs:DescribeMetricFilters because no identity-based policy "
          "allows the action", SEND),
]


@pytest.mark.parametrize("tag,body,expected", GOAL_PROBES,
                         ids=[p[0] for p in GOAL_PROBES])
def test_goal_probe_strings_route_as_measured(tag, body, expected):
    """A and D flip to SEND; B and C deliberately do not; E already sent.

    B and C carry NO role-vs-user discriminator at all, so any pattern broad
    enough to send them also sends the agent-doable ROLE case. That trade is
    refused here: the whole point of the directive is to stop emailing about
    work the fleet can do. If you are here because you want B or C to send,
    add the discriminator to the MESSAGE (name the principal), not breadth to
    the pattern.
    """
    verdict, _reason, _dest = decide("blocker", "", body)
    assert verdict == expected, f"probe {tag} routed the wrong way"


def test_probe_A_sends_without_quoting_an_aws_traceback():
    """The goal's verification criterion 1, stated as its own pin.

    Before this change the gate rescued an IAM ask ONLY when the message pasted
    a raw AWS denial (the string "authorized" matching the approval pattern) --
    so the more human-readable the notification, the more likely it was silently
    suppressed. This pins the plain-English form.
    """
    body = GOAL_PROBES[0][1]
    assert "not authorized" not in body        # no traceback in the input
    verdict, reason, _dest = decide("blocker", "", body)
    assert verdict == SEND
    assert "IAM grant on a USER principal" in reason


ROLE_SHAPED = [
    "add the statement to arn:aws:iam::123456789012:role/some_test_role inline policy",
    "we need iam:PutRolePolicy to land the change",
    "iam:GetRolePolicy and iam:UpdateAssumeRolePolicy on the deploy role",
]


@pytest.mark.parametrize("body", ROLE_SHAPED)
def test_role_scoped_iam_stays_suppressed(body):
    """THE load-bearing pin of this block (guard-3763).

    A ROLE grant is agent-doable. Emailing the user about one is the exact
    behaviour the 2026-08-10 directive asks to stop, so a widening of the IAM
    pattern that starts matching these has regressed the feature even though
    every other test still passes.
    """
    verdict, _reason, _dest = decide("blocker", "", body)
    assert verdict == SUPPRESS


REJECTED_FOR_BREADTH = [
    # bare `user/<name>`: 127 hits / 1.0% of the live board corpus, because a
    # filesystem path contains it.
    "the log is at /home/user/agent/out.log and the run finished",
    "IAM audit complete; the log is at /home/user/agent/out.log",
    # generic `service:Action`: 798 hits / 6.6% of the same corpus.
    "deployed the change, s3:PutObject and logs:CreateLogStream both verified",
]


@pytest.mark.parametrize("body", REJECTED_FOR_BREADTH)
def test_patterns_rejected_for_breadth_stay_rejected(body):
    """Each of these matched a candidate pattern that was measured and dropped.

    The second case is the one worth keeping: it contains the literal word IAM
    AND the literal `user/`, and it matched until a `(?<![\\w/])` lookbehind was
    added. It is prose about a log file.
    """
    verdict, _reason, _dest = decide("completion", "", body)
    assert verdict == SUPPRESS


def test_permissions_boundary_fence_sends_without_the_word_authorized():
    """guard-3779 shape (b) must stand on its own.

    The raw AWS boundary denial already sent, but only incidentally -- it
    contains "authorized", which the approval pattern catches. A boundary
    reported in plain English had no path to SEND at all.
    """
    body = ("the minted-user-boundary permissions boundary carries an explicit "
            "deny for this action")
    assert "authoriz" not in body
    verdict, reason, _dest = decide("completion", "", body)
    assert verdict == SEND
    assert "permissions-boundary fence" in reason


def test_console_only_work_sends_under_the_existing_gui_class():
    """A cloud console IS the GUI case capability-routing.md already names.

    Pinned as GUI rather than as a new class on purpose -- adding a second class
    for the same condition is how two vocabularies drift apart.
    """
    verdict, reason, _dest = decide("blocker", "", "you will have to do it in the AWS console")
    assert verdict == SEND
    assert "GUI / live interaction" in reason


def test_console_step_is_not_a_console_gate():
    """A measured false positive, kept as a pin so it cannot come back.

    `\\bconsole step\\b` was a candidate alternative. Over the live board corpus
    it produced 2 hits, one of them "Roblox-console step-0 timeout" -- a
    hyphenated product noun followed by a step index, not a human-only gate.
    """
    verdict, _reason, _dest = decide(
        "completion", "", "corroborates the Roblox-console step-0 timeout at t=110")
    assert verdict == SUPPRESS
