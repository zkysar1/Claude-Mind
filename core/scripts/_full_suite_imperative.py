#!/usr/bin/env python3
"""Predicate + imperative text for the full-suite JIT advisory ().

SINGLE SOURCE OF TRUTH, in an underscore-named module so it is importable --
`full-suite-imperative-gate.py` is hyphenated and cannot be imported, which is
the same reason `_gradle_tests_predicate.py` exists next door. The gate and its
tests both import from here; neither re-types the predicate (rb-8183: a copied
predicate agrees on the day it is written and stops agreeing the first time
either side is edited, with nothing going red to say so).
"""

import re

OVERRIDE_TOKEN = "FULL_SUITE_IMPERATIVE_OVERRIDE"

# Trigger families, matched at COMMAND POSITION -- never as a bare mention.
#
# The naive form (`\bpytest\b` anywhere in the command) fires on `grep pytest
# notes.md`, on a heredoc documenting the runner, and on this module's own test
# file. The sibling gradle gate explicitly ACCEPTS that trade, and is right to:
# for a DENY, a false positive costs one refused token while a false negative
# costs a silent zero-test run. The trade INVERTS for an advisory -- nothing is
# refused, so a false positive is pure noise on a channel whose only job is to
# be read, and this gate fires on ordinary daily commands rather than on a
# mistake. Hence the statement-structure resolution below.

_SEPARATOR_RE = re.compile(r"(?:\|\||&&|[;|&\n])")
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Wrappers that take the real command as their tail.
#
# `bash` and `sh` are in here because `bash core/scripts/run-full-suite.sh` is
# the form this repo actually uses -- every invocation in the rules, the skills
# and the runbooks is written that way. Leaving them out made the gate miss its
# single most common trigger while every hand-written probe (`pytest ...`,
# `./gradlew ...`) still passed, which is the shape of a predicate that tests
# green and is inert in production (guard-920).
#
# `bash -c "..."` is NOT unwrapped: stripping the wrapper leaves `-c` as the
# head, which matches nothing, so it silently does not fire. That is deliberate
# -- unwrapping a quoted payload correctly is more machinery than an advisory
# justifies, and fail-quiet is the safe direction when nothing is being refused.
_WRAPPERS = frozenset(
    {
        "sudo", "env", "time", "nohup", "command", "exec", "xargs", "nice",
        "stdbuf", "bash", "sh", "zsh",
    }
)

_FRAMEWORK_HEAD_RE = re.compile(
    r"^(?:[\w./\\-]*[/\\])?(?:run-full-suite(?:\.sh|\.py)?|pytest)$"
)

# The EXPENSIVE form specifically. _FRAMEWORK_HEAD_RE deliberately lumps a
# 3-second `pytest test_one.py` together with a 3-5 HOUR full-suite launch.
# That is RIGHT for the imperative above -- both need the VERDICT-line and
# never-pipe discipline -- and WRONG for a consultation trigger: asking for two
# retrieval queries before every targeted pytest is how you train a reader to
# skip the whole banner. So the consult below is gated on the runner itself,
# where hours are at stake and the trigger fires rarely.
_FULL_SUITE_HEAD_RE = re.compile(
    r"^(?:[\w./\\-]*[/\\])?run-full-suite(?:\.sh|\.py)?$"
)
_PYTHON_HEAD_RE = re.compile(r"^(?:[\w./\\-]*[/\\])?(?:python3?(?:\.exe)?|py)$")
_GRADLE_HEAD_RE = re.compile(r"^(?:[\w./\\-]*[/\\])?gradlew(?:\.bat)?$")

FRAMEWORK_IMPERATIVE = """\
run-full-suite / pytest is about to run. Six things decide whether its output
means anything (.claude/rules/run-full-suite-after-deep-code.md):

1. READ THE `VERDICT:` LINE FIRST, before any number above it. A run reporting
   `TOTAL: N passed, 0 failed, 0 errors` with every per-chunk line reading
   `0 failed` can still be `VERDICT: INVALID (contended)`, which means the
   number means NOTHING. Confirmed on four boxes.
2. `VERDICT: GENUINE` CAN BE FALSE. Failures confined to one chunk with later
   chunks clean is contention, however large or small the count -- a SMALL
   count is more suspicious, not less, because 14 failures look individually
   plausible enough to triage one by one. Re-run the worst-hit chunk's files
   solo first; `run-full-suite.sh --triage` chains that and also reports
   OWNERSHIP, so you do not file a duplicate against a tracked red.
3. THE CHUNK LADDER IS A RETRY PROTOCOL, NOT A SETTING. 8 -> 12 -> 16 -> 20 ->
   24 -> 28 -> 32. A rung is never inheritable -- not from another box, and not
   from your own earlier run on this one. Escalate only when the VERDICT says
   to, and never read a contended run's totals as a regression.
4. NEVER PIPE THE RUNNER. A trailing `| tail` replaces its exit code with the
   pipe's, destroying the exit-2 INVALID signal, and a bounded window discards
   the VERDICT line itself. Redirect to a file OUTSIDE any synced tree, then
   Read it.
5. `VERDICT: CLEAN` SCOPES TO THE CHUNKED PYTEST HALF ONLY. Also grep `^FAIL`
   for the invisible (main()-style + shell) suites and the domain half --
   each half reports separately and none may ride under the chunked verdict.
   (mind_api/tests folded into the chunked pool 2026-08-20, g-115-6942.)
6. ON A BUSY BOX, PIN THE TREE BEFORE YOU LAUNCH. `VERDICT: INVALID
   (tree-moved)` outranks every other verdict and voids the entire run, and
   HEAD moves for reasons that are not a peer merge: your OWN `git commit`
   counts, and so does your own loop's turn-end iteration-push merge. The tree
   lock does NOT cover you -- it returns 0 for your own sid, and a BACKGROUNDED
   run inherits no MIND_SID at all so it takes no lock while still printing
   authoritative-looking chunk counts. FIRST, THOUGH: IF THIS BOX HAS A LIVE
   mind_api DAEMON, DO NOT PIN A WORKTREE AT ALL (guard-5866) -- it is worse
   than the contention it avoids. The worktree spawns its OWN daemon, and
   mind-api-start.sh _sweep_orphan_daemons matches mind_api.src processes by
   COMMAND LINE with ZERO runtime-dir scoping, so its spawn-time sweep KILLS
   the fleet's live daemon (every agent on the box loses it, once per chunk
   gap). The copied daemon.port then goes stale on that recycle and every
   daemon-backed test fails `REFUSED: recycle/spawn requested from inside
   pytest` -- a large authoritative-looking count that is PURE ENVIRONMENT.
   Measured as a one-variable pre-registered control (bravo cc-05 2026-09-03:
   3 daemon kills + 12 stale-port errors in the worktree vs 0 and 0 for the
   SAME suite/commit/box in the main repo); reproduced alpha cc-04 2026-09-04,
   22 failures across 8 files, every one of them environment. Copying the port
   does NOT fix this and a symlink does not either -- the kill is the defect,
   the stale port is only its most visible symptom. Use the daemon-safe
   MAIN-REPO route: STORAGE_BACKEND=local, chunked, `-m 'not
   daemon_integration'`, and simply do not commit while it runs.
   OTHERWISE (no live daemon on this box), remedy: `git worktree add --detach
   /tmp/<name> <sha>`. FIRST copy BOTH gitignored runtime files INTO the
   worktree -- `cp agents/<you>/local-paths.conf` AND `cp
   mind_api/state/daemon.port` (mkdir -p its dir first) -- then run the suite
   THERE exporting MIND_AGENT, MIND_SID, STORAGE_BACKEND=local. TWO copies,
   not one: the daemon.port half was missing from this line until 2026-08-31
   and costs a chunk. Without it every daemon-only test fails
   `daemon not reachable: no mind_api/state/daemon.port`, and the run reports a
   large authoritative-looking failure count that is PURE ENVIRONMENT. Measured
   (bravo cc-05, g-115-5979): chunk 00 read 20 failed / 4486 passed, all 20 in
   ONE file; the SAME file gave 28 passed rc=0 in the main repo and 28 passed
   rc=0 in the worktree once the port was copied in. The file holds only a port
   number and the daemon listens on localhost, so copying it suffices -- do NOT
   start a second daemon. (guard-5702; sibling of guard-5365, which asks whether
   your CHANGE reached the worktree -- both required, neither implies the
   other.) The conf copy is load-bearing: it is
   GITIGNORED, so a worktree cannot inherit it and WORLD_DIR/META_DIR resolve
   EMPTY -- 0 passed / 106 errors, an INVALID run that reads as a
   catastrophic regression (alpha cc-04; zeta cc-02 2026-08-27). DO NOT reach
   for MIND_WORLD/MIND_META: this line prescribed exporting them until
   2026-08-27 and THEY CANNOT WORK -- core/scripts/tests/conftest.py pops
   BOTH at module import, deliberately; its own comment says the unset case
   resolves through the conf chain, measured in the MAIN repo where the conf
   EXISTS. A worktree breaks that premise, so the copied conf is the only
   channel surviving collection. That export remedy was itself a guard-2030
   defect -- a correctly-measured diagnosis with an unmeasured remedy beside
   it, believed by adjacency, costing a 30-min run.
   Measured 6 voided runs across 3 agents before this line
   existed (guard-4774, guard-4940, guard-5124).

On an own-cloud box `STORAGE_BACKEND=local` is MANDATORY for any test runner,
including bash aggregators and direct `python3 test_*.py` (guard-955): a
tmp-world write otherwise collides on the PRODUCTION S3 key. And a targeted run
is never sufficient for a deep-code closure claim."""

FULL_SUITE_CONSULT = """\
THIS IS THE EXPENSIVE FORM (3-5h). CONSULT THE LIVE STORE BEFORE YOU LAUNCH.

Everything above is STATIC TEXT baked into core/scripts/_full_suite_imperative.py.
It carries only what someone hand-added to it, so a guardrail measured YESTERDAY
is not in it yet -- the text cannot tell you what it is missing, and it looks
equally authoritative either way. Two retrieval queries close that gap, because
retrieve.sh reads the store as it is right now. Run BOTH (subject, then
mechanism -- code-review-protocol.md step 4; a subject query systematically
misses guardrails indexed on the METHOD):

  bash core/scripts/retrieve.sh --category "running the full test suite on this box" --depth shallow --include-framework
  bash core/scripts/retrieve.sh --category "<the METHOD you are about to use: pinned worktree / background / chunked / solo>" --depth shallow --include-framework

Read the hits before launching, not after triaging. --include-framework is
REQUIRED: without it the response carries no framework_rules key at all.

MEASURED COST OF SKIPPING THIS (alpha, 2026-09-04): guard-5866 landed
2026-09-03 and had not yet been hand-added above. A pinned-worktree launch on a
box with a live daemon produced 111 failures, an unknown fraction manufactured
by the METHOD rather than by the code. Three runs, ~8h of wall clock, no valid
verdict -- and the second query above is the one that would have returned it."""

GRADLE_IMPERATIVE = """\
A gradle command is about to run (.claude/rules/run-full-suite-after-deep-code.md):

1. A `--tests` run that reports ZERO tests executed is a FAILED measurement,
   not a pass. Read the executed-test count before concluding anything --
   "BUILD SUCCESSFUL" with 0 selected tells you nothing about the code.
2. Closing a deep goal needs the FULL suite (`./gradlew test --no-daemon`), not
   `--tests <ChangedTestClass>`. The full run is what catches symmetry and
   contract tests in OTHER classes -- the canonical regression shipped exactly
   that way."""


def _module_target(rest):
    """The argument to `-m`, or "" when there is no module form.

    Keeps `python3 -c "print('pytest')"` from firing: only `-m pytest` counts.
    """
    for i, token in enumerate(rest):
        if token == "-m" and i + 1 < len(rest):
            return rest[i + 1]
        if token.startswith("-m") and len(token) > 2:
            return token[2:]
    return ""


def statement_heads(command):
    """Yield (head_token, remaining_tokens) per top-level statement, with env
    assignments and wrapper commands stripped.

    Deliberately NOT quote-aware. A quote-aware splitter is the right tool for a
    gate that REFUSES -- see `split_top_level` in trailing-echo-exit-gate.py,
    which returns None on unbalanced quotes precisely so its caller can fail
    open. Here a mis-split can only cost or spare one advisory, so the simpler
    function, whose behaviour a reader can predict without running it, wins.
    """
    for statement in _SEPARATOR_RE.split(command):
        tokens = statement.strip().split()
        i = 0
        while i < len(tokens) and (
            _ASSIGNMENT_RE.match(tokens[i]) or tokens[i] in _WRAPPERS
        ):
            i += 1
        if i < len(tokens):
            yield tokens[i], tokens[i + 1:]


def matched_families(command):
    """Return the trigger families INVOKED by `command`, deduplicated, in a
    stable order."""
    if not isinstance(command, str):
        return []
    found = set()
    for head, rest in statement_heads(command):
        if _FRAMEWORK_HEAD_RE.match(head):
            found.add("framework")
            if _FULL_SUITE_HEAD_RE.match(head):
                found.add("full_suite")
        elif _PYTHON_HEAD_RE.match(head) and (
            # `python3 -m pytest ...`
            "pytest" in _module_target(rest)
            # `py -3 core/scripts/run-full-suite.py --triage` -- the runner has
            # a .py entry point as well as the .sh wrapper, and the triage path
            # is normally reached through it.
            or any(_FRAMEWORK_HEAD_RE.match(tok) for tok in rest)
        ):
            found.add("framework")
            # Same interpreter form, but only when the RUNNER is the target --
            # `python3 -m pytest core/scripts/tests` is not the expensive form.
            if any(_FULL_SUITE_HEAD_RE.match(tok) for tok in rest):
                found.add("full_suite")
        elif _GRADLE_HEAD_RE.match(head):
            found.add("gradle")
    return [f for f in ("framework", "full_suite", "gradle") if f in found]


def build_message(families):
    parts = []
    if "framework" in families:
        parts.append(FRAMEWORK_IMPERATIVE)
    if "full_suite" in families:
        parts.append(FULL_SUITE_CONSULT)
    if "gradle" in families:
        parts.append(GRADLE_IMPERATIVE)
    return "\n\n".join(parts)
