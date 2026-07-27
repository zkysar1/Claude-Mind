"""Shared drain-action goal title signature — SSOT for the temp/-drain goal.

The drain-action goal filed by ``precheck-eval.cmd_temp_pressure`` carries a
templated title (prefix + count + infix). Two independent code paths must
recognize that EXACT goal and nothing else:

  - ``precheck-eval.py`` dedup — do NOT re-file a drain goal that is already open.
  - ``goal-selector.py`` ``_is_owner_scoped_goal`` — never cross-agent-reallocate
    the owner's own drain goal (an owner-scoped action goal stays with its owner).

Keying BOTH matchers on the same positive signature here means a title-template
edit can never silently desync either one. The old ``goal-selector.py`` fallback
(``"drain" in title and "temp" in title``) false-positived on any goal whose
title merely MENTIONS temp-drain — an ``Idea:``/``Investigate:`` analysis goal,
or a ``Maintain: add ... temp-drain ...`` goal ABOUT the drain — wrongly marking
it owner-scoped and non-reallocatable, so it stranded with a dormant owner.
(g-115-2983; mirrors the g-115-2981 precheck-eval dedup fix; rb-3452 "assert the
mechanism, not the case".)
"""

# Drain-action goal title template markers — the SINGLE SOURCE OF TRUTH shared by
# the suggested_goal template (precheck-eval.cmd_temp_pressure) and every
# positive-signature match. A prose edit to the title template changes these two
# constants and BOTH matchers follow automatically — they cannot drift.
_DRAIN_GOAL_TITLE_PREFIX = "Maintain: drain "
_DRAIN_GOAL_TITLE_INFIX = "accumulated temp/ working docs"


def is_drain_action_title(title):
    """True iff ``title`` is the templated drain-action goal title (prefix + infix),
    NOT merely a goal that MENTIONS temp-drain. Case-insensitive; a None/empty
    title is not a drain-action title.
    """
    t = (title or "").lower()
    return (
        t.startswith(_DRAIN_GOAL_TITLE_PREFIX.lower())
        and _DRAIN_GOAL_TITLE_INFIX.lower() in t
    )
