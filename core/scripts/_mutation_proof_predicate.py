"""Shared predicate: the hand-rolled mutation-proof shape ().

A mutation proof is: back up a file -> deliberately break it -> run the test ->
assert RED -> restore -> verify byte-identical. ``core/scripts/mutation-proof-test.sh``
mechanizes it with an EXIT/INT/TERM trap, byte-verification against the backup,
and a dedicated exit code for restore failure. Hand-rolling it drops the trap,
so a turn that dies between sabotage and restore leaves deliberately-broken code
on disk -- measured on a shared PRODUCTION file that four agents push (zeta,
g-335-336: ``synchronized (ROUTE_LOCK)`` left stripped across two tool calls).

WHY A GATE RATHER THAN A FIFTH PROSE ARTIFACT. Four artifacts already target
this exact decision point -- rb-5219, guard-1516, guard-1595, guard-1621, the
last naming "mutation-proof" verbatim -- and g-115-3494 recorded FOUR
hand-rolls across THREE agents anyway. Its verification criteria forbid closing
by writing a fifth. Measured 2026-09-06 (bravo, cc-05), the reason prose cannot
work here is structural, not attentional:

  - rb-5219 retrieval_count = 0, last_retrieved = None, ~6 weeks after creation.
    Never retrieved once. It influenced behaviour exactly once, while it still
    sat in the context window that wrote it.
  - The three guardrails ARE retrieved now (133 / 92 / 40) and DO fire
    (29 / 115 / 72), and the hand-rolls happened regardless.

Zeta diagnosed why, and the diagnosis is what this file acts on: the artifacts
that surface are keyed on the goal's SUBJECT; these are keyed on the METHOD
about to be chosen. A subject-keyed query cannot reach a method-keyed artifact,
and the agent cannot form the method-keyed query without already having had the
realization the artifact exists to supply. Self-defeating by construction. A
PreToolUse predicate does not need the realization -- it reads the command.

DO NOT read the near-zero ``times_helpful`` on those guardrails as evidence they
are dead weight. Corpus control, same run, over 5,694 ever-fired active
guardrails: 72.2% have times_helpful == 0 and corpus-wide helpful/active is
0.003. A zero there is a property of the INSTRUMENT (almost nobody records a
helpful mark), not of the artifact. Against that baseline guard-1621 (h/a 0.042)
runs 14x the corpus mean. Retiring on that field would have been a corpus-wide
artifact misread as a per-artifact verdict.

Shared verbatim between the gate and any future Layer-C detective so the two can
never drift about what the shape IS -- the arrangement
_embedded_block_predicate.py and _swakeup_predicate.py already use.
"""

import re

try:
    # Shared with the sibling predicate rather than re-derived: a heredoc body is
    # DATA, not a command, so prose ABOUT the hand-rolled shape (documentation, a
    # test fixture, this file's own docstring written via `cat > f <<EOF`) must not
    # fire the advisory. Measured (fresh-eyes F2): writing notes describing the
    # shape fired it. The sibling already solved this; dropping it on the way over
    # was the defect.
    from _embedded_block_predicate import _strip_heredoc_bodies
except Exception:  # pragma: no cover - fail open, never break the hook
    def _strip_heredoc_bodies(text):
        return text

HELPER = "core/scripts/mutation-proof-test.sh"
PARTITION_HELPER = "core/scripts/mutation-partition-proof.sh"

# Already reaching for the mechanized form -- say nothing.
_USES_HELPER = re.compile(r"mutation[-_](?:proof[-_]test|partition[-_]proof)")

# Taking a copy of a file aside so it can be put back. The `.bak/.orig/.backup`
# suffix family and a copy into a temp dir are the two forms all four recorded
# occurrences used.
_BACKUP = (
    (r"\bcp\s+[^\n|;&]*\.(?:bak|orig|backup|save)\b", "cp to a .bak/.orig/.backup copy"),
    (r"\bcp\s+[^\n|;&]*\s+/tmp/[^\n|;&]*", "cp into /tmp"),
    (r"\bgit\s+stash(?:\s+push)?\b", "git stash"),
)

# Putting it back by hand. `git checkout --`/`git restore` count only when a
# backup shape is also present (see detect) -- on their own they are ordinary
# work, and git-restore-uncommitted-gate.sh already owns that risk.
_RESTORE = (
    (r"\b(?:cp|mv)\s+[^\n|;&]*\.(?:bak|orig|backup|save)\b", "cp/mv back from the copy"),
    # Destination may be absolute OR relative; only a /tmp destination is
    # excluded (that is a tmp-to-tmp copy, not a restore). The first shape
    # here forbade a leading "/" in the destination, which silently missed
    # every absolute path -- the common real-world form (fresh-eyes F1).
    (r"\b(?:cp|mv)\s+/tmp/[^\n|;&]*\s+(?!/tmp/)[^\n|;&]+", "cp/mv back out of /tmp"),
    (r"\bgit\s+stash\s+pop\b", "git stash pop"),
    (r"\bgit\s+(?:checkout|restore)\s+(?:--\s+|HEAD\s+)", "git checkout/restore of a path"),
)

# Corroborators -- what makes the backup/restore pair a mutation PROOF rather
# than an ordinary save-and-revert.
_TEST_RUN = re.compile(
    r"\b(?:pytest|py\.test|gradlew?|npm\s+(?:run\s+)?test|"
    r"python3?\s+-m\s+pytest|go\s+test|cargo\s+test)\b"
)
_SABOTAGE_MARKER = re.compile(r"MUTATION[-_ ]PROOF|sabotage", re.IGNORECASE)


def _matches(patterns, text):
    for pattern, label in patterns:
        if re.search(pattern, text):
            return label
    return None


def detect(text):
    """Return a finding dict when `text` hand-rolls a mutation proof.

    None means NO affirmative match -- callers must treat None as "say nothing",
    never as "inconclusive, warn anyway" (guard-2655).
    """
    if not text or not isinstance(text, str):
        return None
    text = _strip_heredoc_bodies(text)
    if _USES_HELPER.search(text):
        return None

    backup = _matches(_BACKUP, text)
    restore = _matches(_RESTORE, text)
    # The PAIR is the fingerprint. Either half alone is ordinary work: a bare
    # `cp x x.bak` is a save, a bare `git restore` is a revert. Only taking a
    # copy aside AND putting it back inside one command is the proof shape.
    if not (backup and restore):
        return None

    marker = _SABOTAGE_MARKER.search(text)
    if marker:
        return {
            "form": "backup+sabotage+restore",
            "backup": backup,
            "restore": restore,
            "corroborator": marker.group(0),
            "helper": HELPER,
            "guard": "guard-1621",
        }

    test = _TEST_RUN.search(text)
    if test:
        return {
            "form": "backup+testrun+restore",
            "backup": backup,
            "restore": restore,
            "corroborator": test.group(0).strip(),
            "helper": HELPER,
            "guard": "guard-1621",
        }
    return None


def advisory_text(finding):
    """One-paragraph advisory naming the helper. Shared by both consumers."""
    if finding.get("form") == "backup+sabotage+restore":
        seen = (f"taking a copy aside ({finding.get('backup')}), naming a deliberate "
                f"break ({finding.get('corroborator')}), then restoring "
                f"({finding.get('restore')})")
    else:
        seen = (f"taking a copy aside ({finding.get('backup')}), running "
                f"{finding.get('corroborator')}, then restoring ({finding.get('restore')})")
    return (
        f"[mutation-proof-hand-roll] ADVISORY ({finding.get('guard', 'guard-1621')}): this command is "
        f"{seen} -- the hand-rolled mutation-proof shape. Use "
        f"`bash {HELPER}` instead. It restores from an EXIT/INT/TERM trap and "
        f"byte-verifies against the backup, with a dedicated exit code for "
        f"restore failure; a hand-rolled restore depends on this turn surviving "
        f"long enough to run it. That turn has already failed once: a sabotage "
        f"left `synchronized (ROUTE_LOCK)` stripped from a shared production "
        f"file across two tool calls. Four prose artifacts target this exact "
        f"moment and four hand-rolls happened anyway (g-115-3494), which is why "
        f"the reminder now lives at the tool layer. ADVISORY ONLY: your command "
        f"is running."
    )
