r"""goal_id_patterns.py: ONE shared source for the goal-id regex (g-353-140).

The 2026-09-15 outage (g-115-9999) closed goal filing FLEET-WIDE because the
goal-id grammar was hand-copied at ~30 sites and every copy that bounded the
sequence group at a digit count expired the day asp-115 crossed the width.
g-306-486 then widened the ~30 copies instead of removing the copy surface —
which is exactly what a copy surface invites (g-353-140 description).

This module is the single place the SEQUENCE fragment is written. The composed
patterns are built from the fragments at import time; no composed pattern in
this file carries a written sequence quantifier, so the static sweep in
core/scripts/tests/test_goal_id_five_digit_seq.py has nothing to flag here and
the next author cannot re-introduce a bound by copying a sibling (guard-1161:
a digit-count bound on a counter is a dated assumption wearing a regex — every
bound placed on the counter has expired in production: g-115-999 on
2026-05-19 and g-115-9999 on 2026-09-15, each a fleet-wide filing outage
[UNVERIFIED -- carried from the g-353-140 description, not re-measured
this session]).

Importing from BOTH layers is the sanctioned pattern, not a boundary violation:
core/BOUNDARY.md lists 'aspirations.py validators/constants' as a LIBRARY
MODULE IMPORTED BY BOTH LAYERS. The forbidden direction is Layer 1 importing
mind_api.src (enforced by core/scripts/layer1-no-runtime-imports-gate.py,
the gate whose GATE OK line the commit hook prints); this module lives in
core/scripts and is imported by the daemon through the core/scripts
sys.path entry guard-2676's no-transcription call shape uses.

The SEQUENCE group is the ONLY fragment that is load-bearing (g-353-140
records it verbatim: "What is shared is the SEQUENCE FRAGMENT and the small
set of composed canonical patterns built from it — nothing more"): the
ASPIRATION half stays \d{3} (a different axis, nowhere near a ceiling), the
xw- cross-world branch and the -[a-z] suffix branch are stable grammar, and
the composed shapes differ per consumer by design (anchored full-match vs
in-text \b-scan vs suffix-bearing vs the xw- branch).
"""

from __future__ import annotations

import re

# THE FRAGMENTS. SEQUENCE is the only one guard-1161 governs: OPEN-ENDED, and
# it must stay open-ended. A capped capture on a longer id does not fail — it
# reads the leading digits as a smaller number, so max+1 re-mints a live id
# (guard-2414). Do NOT re-bound it.
ASP = r"\d{3}"
SEQUENCE = r"\d+"
XW = r"xw-\d{8}T\d{6}"
XW_GOAL = r"\d{2}"  # the g-xw-<ts>-NN branch's sequence is a fixed-width day number, not the counter
SUFFIX = r"(-[a-z])?"

# -- The canonical goal-id grammar, built from the fragments -------------------
# Full id: g-<asp>-<seq>[-a]  |  g-xw-<ts>-<NN>
_GOAL_ID_BODY = rf"({ASP}-{SEQUENCE}{SUFFIX}|{XW}-{XW_GOAL})"
_GOAL_ID_BODY_NC = rf"(?:{ASP}-{SEQUENCE}|{XW}-{XW_GOAL})"  # non-capturing: the exp-id twin's inner group is non-capturing in the original literal
GOAL_ID_PATTERN = rf"^g-{_GOAL_ID_BODY}$"

# The canonical patterns. Each is compiled ONCE here; consumers import the
# pattern object (or .pattern where they need the string form).
GOAL_ID_ANCHORED = re.compile(GOAL_ID_PATTERN)
GOAL_ID_MINT = re.compile(rf"^g-{ASP}-({SEQUENCE})")  # allocator capture: whole sequence, open-ended
GOAL_ID_EMBEDDED = re.compile(rf"\bg-{ASP}-{SEQUENCE}{SUFFIX}\b")
GOAL_ID_PLAIN = re.compile(rf"^g-{ASP}-{SEQUENCE}$")
GOAL_ID_ANYWHERE = re.compile(rf"\bg-{ASP}-{SEQUENCE}\b")
GOAL_ID_IN_TEXT = re.compile(rf"\bg-{ASP}-{SEQUENCE}\b")  # alias clarity for in-text scans

# The exp-<goal-id> derivation grammar (experience.py / experience_write.py twin).
GOAL_ID_IN_EXP_ID = re.compile(rf"^exp-(g-{_GOAL_ID_BODY_NC})(?:-|$)")

# The CLI-side allocator mint parse lives at two call sites in aspirations.py;
# they share this one object instead of two literals.
MINT_SEQ_RE = GOAL_ID_MINT
