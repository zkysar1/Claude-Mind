""" — hand-rolled wrappers must REFUSE an unknown flag.

WHY THIS FILE EXISTS
Fifteen wrappers were converted to `_argv_strict.sh` under g-115-4501/4504. Four
were skipped because they carry real flag tables of their own, so they kept the
`-*) PASSTHROUGH+=("$1"); shift;;` arm the conversion existed to kill. `PASSTHROUGH`
has no reader in any of the four — so an unrecognised flag vanished AND the next
token slid one slot left into a positional, writing the WRONG VALUE with exit
status 0.

THE FIFTH CASE IS A READ WRAPPER, AND ITS FAILURE MODE IS THE DANGEROUS ONE
(g-115-5214). `aspirations-query.sh` joined this file after the same shape was
found on the read side, where there is no positional to slide into — so the
swallowed flag does not corrupt a record, it makes the query answer a BROADER
question than the caller asked and still exit 0. Measured costs of that silence,
all three on this one wrapper: `--source` returned the union whichever value was
passed, so a caller who ran both and summed them published 456 fleet closes
against a true 228 (guard-2986); `--asp-id` returned 1539 rows where 44 matched,
35x wrong, and the bogus figure was used to accuse a correctly-working
goal-selector of suppressing a lane; `--goal-title-contains` returned 942 rows
byte-identical to no filter at all, 157x over-broad (guard-694). A wrong write is
caught by a read-back; an over-broad READ never looks like a failure, which is
why the read side needed the refusal at least as much as the write side.

WHY rc == 2 AND NOT `rc != 0` (this is the load-bearing assertion)
`_argv_strict.sh`'s header states it outright: the daemon transport path also
exits non-zero, so a test asserting `rc != 0` stays GREEN with the guard reverted.
Every case below pins rc == 2 exactly. Verified by mutation on 2026-08-04
(foxtrot; hostname LAPTOP-3IOFCNEO, uname -r 6.6.87.2-microsoft-standard-WSL2).
A scratch copy of `aspirations-update-goal.sh` with the `-*)` arm restored to the
passthrough form, run with the first case's argv, exited **1** — so
`assert rc != 0` would have stayed GREEN. Its stderr is the defect itself,
verbatim:

    {"error": "invalid_goal_id", "detail": "expected g-NNN-NN[N[N]], got 'SLID'"}

`SLID` is the token that FOLLOWED the unknown flag. It reached the GOAL_ID slot,
one position left of where the caller put it. Only `aspirations-update-goal.sh`
was mutated; the other three share the identical arm shape, so their revert
behaviour is inferred rather than measured.

WHY THE STORE IS PROVABLY UNTOUCHED
The goal names "leaves the store untouched" as the load-bearing half, because
this bug's signature is a SUCCESSFUL write. Three properties give that, and the
third is the one that makes the test safe to RUN:

  1. Ordering (test_refusal_precedes_runtime_source): `_argv_strict.sh` is sourced
     BEFORE `_runtime.sh` in all four, and the refusal `exit 2`s from the arg loop.
     No daemon client exists in the process yet, so no write can be attempted.
  2. No daemon-path stderr: the refusal message is the ONLY thing on stderr. A
     reverted guard reaches the daemon and its stderr looks entirely different.
  3. Every command carries something that makes the REVERTED path harmless. For
     most cases that is a DELIBERATELY NONEXISTENT record id: if the guard is
     reverted and the daemon IS reached, the write fails as goal_not_found rather
     than mutating a live record. A regression test must not need to corrupt the
     store in order to detect corruption.

Point 3 is why these run against the real wrappers rather than a tmp world: the
production shape is what the defect lived in (guard-920), and the bogus id makes
that shape safe.

     ⚠ POINT 3 WAS WRITTEN AS "every command targets a nonexistent record id",
     and that phrasing is NOT universal — it was true of every case in the file
     until 2026-08-22, which is exactly what made it dangerous to inherit. Two
     members now carry no id to make bogus, so each needs its OWN safety
     property, and adding a third such wrapper without one would arm a
     destructive test that looks identical to the safe ones:
       * aspirations-add.sh — reads its body from STDIN; _run()'s input=""
         makes a reverted parse exit at the empty-stdin check, before the
         daemon.
       * aspirations-clear-stale-claims.sh — takes NO id and SWEEPS. A reverted
         guard reaching the daemon would CLEAR STALE CLAIMS ON THE LIVE WORLD
         STORE during an ordinary test run. Its argv therefore carries a real
         `--dry-run`, which the mutation never touches, so the reverted path is
         a PREVIEW. Measured 2026-08-22 on cc-07 with the daemon call stubbed:
         `--nonexistent-flag SLID --dry-run` -> source=world&dry_run=true, while
         the same argv WITHOUT --dry-run -> dry_run=false, i.e. a real clear.
     Before adding any case, ask what makes ITS reverted path harmless and say
     so at the entry. "It follows the pattern" is not an answer.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2].parent
SCRIPTS = PROJECT_ROOT / "core" / "scripts"

# conftest.py already puts core/scripts/ on sys.path for collected tests; this
# insert matches the sibling pattern (test_body_heartbeat_writer.py:65) so the
# file also imports when run directly via `py -3 <this file>`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _runtime_bash import bash_cmd  # noqa: E402

# Bogus ids — see docstring point 3. Nothing here can name a live record.
BOGUS_GOAL = "g-99999-99999"
BOGUS_ASP = "asp-99999"
BOGUS_PIPE = "9999-99-99_g-115-4733-refusal-fixture"

# (wrapper, argv-after-wrapper). The unknown flag is FIRST in each, so the token
# that would slide into a positional slot under the old behaviour is the very
# next one — the exact clobber shape rb-538 describes.
CASES = [
    (
        "aspirations-update-goal.sh",
        ["--nonexistent-flag", "SLID", BOGUS_GOAL, "status", "completed"],
    ),
    (
        "aspirations-update.sh",
        ["--nonexistent-flag", "SLID", BOGUS_ASP, "status", "completed"],
    ),
    (
        "aspirations-add-goal.sh",
        ["--nonexistent-flag", "SLID", BOGUS_ASP],
    ),
    (
        "pipeline-update-field.sh",
        ["--nonexistent-flag", "SLID", BOGUS_PIPE, "status", "archived"],
    ),
    # READ wrapper (). Docstring point 3 (bogus id) applies with the
    # same intent but a different mechanism: this one cannot mutate anything, so
    # the bogus id is here to keep the REVERTED path cheap — a swallowed flag
    # would fall through to a real `--goal-field id <bogus>` query returning [],
    # rather than dragging the whole completed set over the daemon on every run.
    (
        "aspirations-query.sh",
        ["--nonexistent-flag", "SLID", "--goal-field", "id", BOGUS_GOAL],
    ),
    # First of the 22-wrapper rollout (). Same read-wrapper reasoning as
    # aspirations-query.sh above: it cannot mutate, and the bogus id keeps the
    # REVERTED path cheap — a swallowed flag falls through to `--id <bogus>`
    # returning nothing rather than dragging the whole pipeline over the daemon.
    #
    # WHY THIS ONE FIRST, and it is not arbitrary: the  caller scan
    # (core/scripts/unknown-flag-caller-scan.py) reported pipeline-read.sh READY
    # — 0 blocking callers, 0 unresolved, 0 hazards across 5 roots. The goal's
    # order is load-bearing (enumerate callers -> fix them -> THEN refuse),
    # because 's single caller passed TWO unrecognised flags and
    # refusing first would have disarmed a never-clobber guard downstream.
    (
        "pipeline-read.sh",
        ["--nonexistent-flag", "SLID", "--id", BOGUS_PIPE],
    ),
    # Second of the 22-wrapper rollout (). Same read-wrapper reasoning:
    # it cannot mutate, and BOGUS_ASP keeps the REVERTED path cheap — a swallowed
    # flag falls through to `--id asp-99999`, which the daemon answers with
    # nothing, rather than dragging a live aspiration over the wire on every run.
    # That cheapness is not cosmetic HERE, because this wrapper is where the cost
    # of the swallow was actually measured (2026-08-22, cc-07, byte-compared
    # against the same call with the flag removed):
    #   --id asp-115 --goal <goal-id>  -> 12,351,575 bytes, rc=0, byte-identical
    #     to `--id asp-115`. A caller asking for ONE goal got 3,799 of them.
    #   --active --goal <goal-id>      -> 18,981,053 bytes, rc=0, likewise
    #     byte-identical to plain `--active`: the entire active corpus.
    # An unpinned reverted path would re-run those transfers on every suite pass.
    #
    # Note the two live callers this wrapper had were fixed FIRST, in their own
    # commit, before the refusal landed — both passed a `--json` flag the wrapper
    # has never accepted (its output is already JSON). The goal's order is
    # load-bearing: refusing first would have broken a working skill and a
    # documented invocation at rc=2.
    (
        "aspirations-read.sh",
        ["--nonexistent-flag", "SLID", "--id", BOGUS_ASP],
    ),
    # Third of the 22-wrapper rollout (), and the one that breaks the
    # pattern the two above established — worth reading before adopting the next.
    #
    # tree-read.sh has TWO paths and only ONE was defective. Measured 2026-08-22
    # on cc-07, byte-compared against the same call with the flag removed:
    #   DAEMON path    `--node root --bogus-flag XVAL` -> rc=0, BYTE-IDENTICAL to
    #     `--node root`. Silently answered a different question.
    #   FALLBACK path  `--validate --bogus-flag XVAL` -> ALREADY rc=2, refused by
    #     tree.py's argparse ("unrecognized arguments").
    # The asymmetry has one cause: this wrapper's PASSTHROUGH array is NOT dead.
    # It is tree.py's argv on the FORCE_FALLBACK branch, so the flag reached a
    # real parser there and reached nothing on the daemon path. Deleting the
    # array — the correct move on pipeline-read.sh and aspirations-read.sh, whose
    # arrays had no reader — would have stripped every argument from --validate,
    # --by-l1, --find, --active-content and the three *-candidates flags.
    # CHECK FOR A READER BEFORE APPLYING THE DELETE CONVENTION.
    #
    # The refusal returns the SAME rc=2 the fallback path already returned, so no
    # accepted invocation changes status; a 10-case matrix (including two
    # fallback cases) is byte-identical in both sha and rc across the change.
    #
    # No caller work was needed: unknown-flag-caller-scan.py reported READY over
    # 153 real call sites, and that READY was positive-controlled with a
    # synthetic bad caller before being trusted (an empty caller_hits list is
    # what a BLIND scanner also prints).
    (
        "tree-read.sh",
        ["--nonexistent-flag", "SLID", "--node", "refusal-fixture-nonexistent-node"],
    ),
    # Fourth of the rollout (), and the first MUTATING one in it —
    # this wrapper completes and ARCHIVES an aspiration, so the clobber the
    # refusal removes is not a wrong-answer defect but a wrong-TARGET one.
    #
    # MEASURED 2026-08-22 on cc-07 with deliberately bogus ids, before the fix:
    #   aspirations-complete.sh --reason asp-99998 asp-99999
    #     -> the daemon received asp-99998
    #   aspirations-complete.sh asp-99999               (control)
    #     -> the daemon received asp-99999
    # The unknown flag was dropped with a single shift, so the token AFTER it
    # won "first non-flag wins" and became the aspiration to archive. With two
    # real ids that archives the WRONG aspiration. After the fix the same call
    # is rc=2 and NO id reaches the daemon at all.
    #
    # Docstring point 3 (bogus id) is doing real work here rather than just
    # keeping the reverted path cheap: under a REVERT, "SLID" becomes the
    # asp_id, and the daemon rejects it as malformed before anything is
    # archived. Never put a well-formed asp id in this row.
    (
        "aspirations-complete.sh",
        ["--nonexistent-flag", "SLID", BOGUS_ASP],
    ),
    # Fifth of the rollout (). Like the stdin pipeline wrappers above
    # it relies on _run()'s input="" so a REVERTED parse falls through to the
    # empty-stdin refusal ("Error: expected JSON on stdin") instead of hanging.
    #
    # The swallow here is the  shape, NOT rb-538's slide: the aspiration
    # body comes from stdin, so there is no positional slot for the next token to
    # clobber. What a dropped flag changes is the TARGET or a GATE. MEASURED
    # 2026-08-22 on cc-07 with empty stdin, so nothing reached the daemon:
    #   --source agent    -> SOURCE_VAL=agent
    #   --sorce  agent    -> SOURCE_VAL stayed EMPTY, so the query carried no
    #                        source and the aspiration would have been created in
    #                        the daemon's DEFAULT queue
    #   --overide-all yes -> no X-Mind-Override-All header, so the gate fires
    # Typo and correct spelling were indistinguishable at the call site; only the
    # destination differed.
    #
    # PASSTHROUGH is KEPT here (unlike aspirations-complete.sh, where both arrays
    # were deleted): it is PARTIALLY live — the loop at the top of the file reads
    # it for the literal "--schema". Only the genuinely write-only
    # PASSTHROUGH_SOURCE was removed. Third distinct verdict across the four
    # wrappers adopted so far (deleted / fully live / deleted / partially live);
    # check for a reader per wrapper rather than inferring from the name.
    (
        "aspirations-add.sh",
        ["--nonexistent-flag", "SLID"],
    ),
    # Sixth of the rollout (), and the MOST DANGEROUS swallow measured
    # in it — read the docstring's amended point 3 before touching this entry.
    #
    # The discarded token here is a SAFETY flag, so the swallow fails OPEN into
    # DESTRUCTION rather than into a wrong answer. The old arm was a bare
    # `*) shift;;` — not even a passthrough, the token was simply dropped. This
    # wrapper takes NO record id and SWEEPS, so there is nothing to make bogus.
    # MEASURED 2026-08-22 on cc-07 with the daemon call stubbed, so nothing was
    # cleared:
    #   --dry-run   -> source=world&dry_run=true    (preview)
    #   --dryrun    -> source=world&dry_run=false   <- REAL CLEAR
    #   --dry_run   -> source=world&dry_run=false   <- REAL CLEAR
    #   --dry-runn  -> source=world&dry_run=false   <- REAL CLEAR
    #   --sorce agent --dry-run -> source=world&... <- WRONG QUEUE swept
    # Three plausible misspellings of --dry-run each turn a preview into a live
    # destructive mutation, with an identical exit status and no complaint.
    #
    # THE TRAILING `--dry-run` IS THE SAFETY PROPERTY OF THIS ROW — it is not
    # decoration and must not be dropped to "match the other entries". The
    # mutation never touches the `--dry-run)` arm, so under a REVERT the argv
    # still parses to dry_run=true and the daemon performs a PREVIEW. Remove it
    # and every reverted/mutated run of this case CLEARS STALE CLAIMS ON THE
    # LIVE WORLD STORE. Verified both ways at the stub (see the docstring).
    (
        "aspirations-clear-stale-claims.sh",
        ["--nonexistent-flag", "SLID", "--dry-run"],
    ),
    #  completion rollout (2026-08-20): the remaining 13 of the goal's
    # 15+1 population, guarded in one pass. Read wrappers carry a valid-but-bogus
    # secondary filter for the same reverted-path-cheapness reason as
    # aspirations-query.sh above; the three stdin pipeline wrappers rely on
    # _run()'s input="" so a REVERTED parse falls through to an empty-stdin
    # daemon refusal rather than the 120s stdin hang the fix removed.
    ("reasoning-bank-read.sh", ["--nonexistent-flag", "SLID", "--id", "rb-999999"]),
    ("guardrails-read.sh", ["--nonexistent-flag", "SLID", "--id", "guard-999999"]),
    ("board-read.sh", ["--nonexistent-flag", "SLID", "--channel", "refusal-fixture-nonexistent"]),
    ("journal-read.sh", ["--nonexistent-flag", "SLID", "--recent", "1"]),
    ("pattern-signatures-read.sh", ["--nonexistent-flag", "SLID", "--id", "sig-999999"]),
    ("spark-questions-read.sh", ["--nonexistent-flag", "SLID", "--id", "sq-999999"]),
    ("team-state-read.sh", ["--nonexistent-flag", "SLID", "--field", "refusal.fixture.nonexistent"]),
    ("experience-read.sh", ["--nonexistent-flag", "SLID", "--id", "exp-refusal-fixture-999999"]),
    ("wm-read.sh", ["--nonexistent-flag", "SLID", "refusal_fixture_slot"]),
    ("pipeline-add.sh", ["--nonexistent-flag", "SLID"]),
    ("pipeline-meta-update.sh", ["--nonexistent-flag", "SLID", "refusal-fixture-field", "v"]),
    ("pipeline-update.sh", ["--nonexistent-flag", "SLID", BOGUS_PIPE]),
    #  rollout, unit `unknown-flag-aspirations-read.sh` (2026-08-21).
    # BOGUS_ASP is doing MORE work here than in the rows above, and the numbers
    # are why: measured on the live store this box, a REVERTED parse falling
    # through to `--active` drags 18,479,266 bytes over the daemon, and even
    # `--id asp-115` is 12,084,637. The bogus id keeps the reverted path at a
    # few hundred bytes, so the mutation control stays cheap enough to actually
    # be run rather than skipped.
    #
    # Caller scan BEFORE the refusal (the goal's load-bearing ordering):
    # 0 blocking, 0 hazard across SEVEN roots — the five code roots plus world/
    # and meta/, which closes the earlier units' "world/ was NOT scanned"
    # residual. The 4 `unresolved` rows were read BY HAND rather than assumed
    # benign: two are real call sites whose values cannot be flag-shaped
    # (claim-liveness-check.sh passes SRC from a resolved world|agent and an
    # asp-NNN id; iteration-close.sh:177 iterates the literal `world agent`),
    # and TWO ARE NOT CALL SITES AT ALL — iteration-close.sh's `echo ... >&2`
    # help text quotes an example invocation, which passes the scanner's
    # invocation-prefix heuristic while being prose.
    ("aspirations-read.sh", ["--nonexistent-flag", "SLID", "--id", BOGUS_ASP]),
    #  rollout, unit `unknown-flag-tree-find-node.sh` (2026-08-22).
    # `--top 1` bounds the REVERTED path: without it a swallowed parse still
    # answers the query and drags the default 3 nodes, and the point of the
    # mutation control is that it stays cheap enough to actually be run.
    #
    # This wrapper differs from every row above in a way worth recording,
    # because it makes the refusal SAFER here than elsewhere: it already has a
    # required-arg guard (`if [ -z "$TEXT" ]` -> exit 1), so a caller passing a
    # bare positional fails LOUDLY today. The two documented
    # `tree-find-node.sh {artifact_or_topic}` sites (coordination.md:184,
    # aspirations-all-blocked/SKILL.md:122) are therefore ALREADY broken and are
    # NOT newly broken by this arm — measured rc=1 before and after.
    #
    # Caller scan BEFORE the refusal (the goal's load-bearing ordering), with
    # the Bash:-aware predicate this same goal fixed one unit earlier:
    # 0 blocking, 0 unresolved, 0 hazard over SEVEN roots. Positive-controlled
    # rather than taken at face value — 57 files MENTION the wrapper, and every
    # invocation among them uses only --text/--find/--top/--leaf-only. The one
    # historically-wrong caller (`tree-find-node.sh --node`) was already
    # repaired 2026-07-25 by commit 2a1894a6, which repointed it to
    # tree-read.sh --node.
    ("tree-find-node.sh",
     ["--nonexistent-flag", "SLID", "--text", "zzz-refusal-fixture", "--top", "1"]),
    #  rollout, unit `unknown-flag-aspirations-complete.sh` (2026-08-22).
    #
    # THE ARG SHAPE IS SAFETY-CRITICAL HERE AND NOWHERE ELSE IN THIS TABLE: this
    # wrapper COMPLETES AND ARCHIVES AN ASPIRATION. Under the mutation control
    # (refusal arm reverted to a silent shift) the wrapper actually RUNS, so the
    # row must be unable to complete anything real. It is safe twice over —
    # `SLID` is consumed by the `*)` arm as ASP_ID (first non-flag wins), so the
    # id that reaches the daemon is the literal "SLID", not BOGUS_ASP; and
    # BOGUS_ASP ("asp-99999") does not exist either. Both are rejected upstream
    # with invalid_asp_id. Never re-point this row at a real asp-NNN.
    #
    # accepted_flags for THIS wrapper were HAND-ENUMERATED, not taken from
    # unknown-flag-caller-scan.py: its arm parser mis-reads the rt_call
    # continuation lines as case arms and reports '--query "$QUERY' plus two
    # multi-line '--body-string' fragments. That is OVER-acceptance, which can
    # MASK a genuine unknown flag — so the scan could not be trusted for the one
    # thing this row pins. Real set, read off the arg loop: --source / --force /
    # --intent-satisfied, plus a positional asp_id.
    #
    # Caller scan BEFORE the refusal (the goal's load-bearing ordering):
    # 0 blocking, 0 unresolved, 0 hazard over SEVEN roots. Positive-controlled —
    # 37 files MENTION the wrapper and every invocation form uses only
    # `--source <val>` plus a positional id. The transitive path was enumerated
    # too: agent-aspirations-complete.sh execs this script with
    # `--source agent "$@"`, and its only two references are doc tables using
    # the bare `<asp-id>` form. The GOAL close path does NOT reach here at all —
    # iteration-close.sh:969 uses aspirations-complete-by.sh, a different script.
    ("aspirations-complete.sh", ["--nonexistent-flag", "SLID", BOGUS_ASP]),
    #
    # ---- CONCURRENT-UNIT MERGE NOTE (2026-08-22, alpha worker Body on cc-08) ----
    # The three rows above and the three below are the SAME goal executed twice,
    # on two boxes, at the same time: alpha/cc-08 worked wrapper-by-wrapper while
    # echo/cc-03 took a 6-wrapper tranche. Three wrappers (aspirations-read,
    # tree-find-node, aspirations-complete) were done by BOTH — a full duplicated
    # unit — and git raised a content conflict here and in all three wrappers.
    # Resolved as a UNION: alpha's rows kept for the overlapping three (they bound
    # the reverted path harder — `--top 1` on tree-find-node, and the argv-shape
    # analysis on aspirations-complete), echo's three NEW rows kept below. Echo's
    # duplicate rows for the overlapping three were dropped, not merged, because a
    # second row for the same wrapper pins nothing extra.
    # In the WRAPPERS themselves the resolution took alpha's side, which is a
    # strict superset: both sides add the identical refusal arm, but alpha's also
    # deletes the dead PASSTHROUGH array (declaration AND every append) rather
    # than leaving a write-only store behind, and passes the true positional slot
    # to argv_strict_help instead of re-listing the flags there.
    # THE PREVENTABLE HALF: this goal's text says one wrapper per unit, and Phase
    # 2.95 of /worker-loop exists to claim the UNIT so two Bodies cannot pick the
    # same one. Neither Body claimed a unit token. That is the defect to fix, not
    # the merge.
    #
    #  tranche (2026-08-22, echo/cc-03): the 6 wrappers that still had
    # a swallowing arm. The goal was filed naming 22; re-measurement found 13
    # already adopted since 2026-08-09, and reading the remaining 9 found
    # aspirations-claim.sh already refusing () and goal-field-append.sh
    # already routing its residual through argv_strict_parse. True population: 6.
    #
    # THE MUTATION CONTROL WAS RUN, NOT INFERRED, and it reproduced this file's
    # header warning on a second wrapper. `git show HEAD:...aspirations-read.sh`
    # was restored to core/scripts/ (production location, so `dirname` resolves
    # identically — guard-920) and given the first case's argv: it exited **1**,
    # not 0. The swallowed flag reached the daemon, which rejected it. So
    # `assert rc != 0` would have stayed GREEN against the unguarded wrapper here
    # exactly as it did for aspirations-update-goal.sh on 2026-08-04. rc == 2 is
    # the only assertion that discriminates.
    #
    # Two are READ wrappers (aspirations-read, tree-find-node) and carry the
    #  failure mode, which is the worse one: no positional to corrupt,
    # so a swallowed selector just answers a BROADER question and exits 0.
    # aspirations-read.sh is the framework's most-called wrapper (337 refs), and
    # its swallowing arm sat under the comment "ignored by daemon, kept for
    # completeness" — true, and precisely why it read as deliberate.
    #
    # Caller enumeration used core/scripts/unknown-flag-caller-scan.py with
    # --root on the EXTERNAL world/ and meta/ (they are NOT scanned by default).
    # 4 came back READY; aspirations-read (4) and aspirations-meta-update (1)
    # reported unresolved sites, all hand-read: every one is a VALUE position
    # with a safe default (world/agent), and two are not invocations at all but
    # `${SOURCE:-<world|agent>}` placeholders inside `echo ... >&2` usage text.
    # Zero callers pass a now-refused flag.
    ("aspirations-retire.sh", ["--nonexistent-flag", "SLID", BOGUS_ASP]),
    ("aspirations-meta-update.sh", ["--nonexistent-flag", "SLID", "refusal-fixture-field", "v"]),
    ("pipeline-move.sh", ["--nonexistent-flag", "SLID", BOGUS_PIPE, "archived"]),
    #
    # ---- aspirations-complete-by.sh (, 2026-09-05, alpha on cc-13) ----
    # The near-namesake aspirations-complete.sh was covered above from the start;
    # THIS wrapper was not, and it is the more dangerous of the pair. Its catch-all
    # was `*) POSITIONAL+=("$1")` with no `-*)` arm, and POSITIONAL[0] is the
    # GOAL-ID with [1] the agent-name — so a typo'd flag closed a DIFFERENT goal,
    # or closed the right goal as the wrong agent, and exited 0. It is also the
    # REQUIRED close path for recurring goals (the daemon refuses a direct
    # status=completed write on one), so iteration-close.sh routes every recurring
    # close through it.
    #
    # Caller scan BEFORE the refusal: unknown-flag-caller-scan.py over SEVEN roots
    # (core + .claude + the external world/ and meta/) reported blocking=0,
    # hazard=0, unresolved=1. The unresolved site was hand-read as the tool
    # requires: iteration-close.sh:1225, `--source "$SOURCE" "$GOAL_ID"`. Both
    # unresolved tokens sit in a VALUE and a POSITIONAL slot respectively, never in
    # a flag position, so no caller passes a now-refused flag. Every other
    # reference in the tree is a doc table using the same `[--source] <goal-id>`
    # form.
    #
    # The scanner's own accepted_flags output was NOT used and must not be: it
    # mis-parses the usage heredoc as case arms and reports fragments like
    # '--source        queue to write to (default: world' as accepted. That is
    # OVER-acceptance, which MASKS a genuine unknown flag — the same limitation
    # already recorded on the aspirations-complete.sh row. Real set, read off the
    # arg loop: --source / --key-finding, plus --help/-h and two positionals.
    ("aspirations-complete-by.sh", ["--nonexistent-flag", "SLID", BOGUS_GOAL]),
    # experience-update-field.sh is deliberately NOT here: its  fix
    # adopted argv_strict_parse, joining the four *-update-field siblings whose
    # refusal predates this file's message contract ( wording, usage
    # on stderr, no --help arm). It is pinned in
    # test_experience_update_field_argv.py against the parse-family contract
    # its siblings actually implement.
]

WRAPPERS = [c[0] for c in CASES]


def _run(wrapper, argv):
    env = dict(os.environ)
    # Force the local backend: guard-955 / rb-2983 — an own-cloud box derives the
    # S3 key from the env id, not from any tmp override, so a test write would
    # land on the PRODUCTION key. Nothing here should reach a backend at all; the
    # pin is the belt to the bogus-id braces.
    env["STORAGE_BACKEND"] = "local"
    # bash_cmd() and not a hand-built argv, because TWO guards apply and it is the
    # only form that satisfies both ():
    #   guard-580 — a .sh path handed to CreateProcess is not a valid Win32 image
    #     (OSError WinError 193). bash_cmd prepends the RESOLVED bash, never a bare
    #     "bash" argv[0], which CreateProcess would resolve to the System32 WSL
    #     launcher and block on forever (guard-1040's rc=124 hang — a strictly
    #     WORSE failure than the immediate error being fixed here).
    #   guard-581 — str(WindowsPath) yields backslashes, which bash treats as escape
    #     introducers and strips, silently producing a nonexistent path. bash_cmd
    #     passes the script through Path.as_posix().
    # A fix carrying only the first guard would trade WinError 193 for a silent
    # wrong-path failure; guard-581 names test harnesses as where that bites
    # hardest, because str() is already POSIX on Linux so the suite stays green.
    return subprocess.run(
        bash_cmd(SCRIPTS / wrapper, *argv),
        capture_output=True,
        text=True,
        input="",           # add-goal reads a JSON body from stdin; give it nothing
        env=env,
        cwd=str(PROJECT_ROOT),
        timeout=120,
    )


@pytest.mark.parametrize("wrapper,argv", CASES, ids=WRAPPERS)
def test_unknown_flag_exits_2(wrapper, argv):
    """rc is 2 EXACTLY, not merely non-zero (see module docstring)."""
    r = _run(wrapper, argv)
    assert r.returncode == 2, (
        f"{wrapper} returned {r.returncode}, expected 2.\n"
        f"rc=1 is what the REVERTED wrapper returns (daemon rejects the bogus "
        f"record) — pinning `!= 0` would not have caught that.\n"
        f"stdout={r.stdout!r}\nstderr={r.stderr!r}"
    )


@pytest.mark.parametrize("wrapper,argv", CASES, ids=WRAPPERS)
def test_unknown_flag_names_the_flag_and_the_defect(wrapper, argv):
    """The diagnostic must identify the offending token, not just fail."""
    r = _run(wrapper, argv)
    assert "unknown option" in r.stderr
    assert "--nonexistent-flag" in r.stderr, (
        "the refusal must echo the token the caller actually typed"
    )
    assert "g-115-4733" in r.stderr, (
        "the refusal cites its goal id so a future reader can find the mechanism"
    )


@pytest.mark.parametrize("wrapper,argv", CASES, ids=WRAPPERS)
def test_store_untouched_no_daemon_contact(wrapper, argv):
    """The refusal fires before any daemon client exists — nothing can be written.

    Asserted negatively: none of the daemon path's own markers appear. A reverted
    guard reaches `rt_call` and its stderr carries one of these.
    """
    r = _run(wrapper, argv)
    lowered = (r.stdout + r.stderr).lower()
    for marker in ("daemon", "rt_call", "goal_not_found", "not found", "http"):
        assert marker not in lowered, (
            f"{wrapper}: found daemon-path marker {marker!r} — the wrapper got past "
            f"the refusal and attempted a write.\nstderr={r.stderr!r}"
        )
    assert r.stdout == "", f"{wrapper} wrote to stdout: {r.stdout!r}"


@pytest.mark.parametrize("wrapper,argv", CASES, ids=WRAPPERS)
def test_refusal_enumerates_the_accepted_flags(wrapper, argv):
    """The refusal must name what IS accepted, not just what is not.

    A refusal that says "the flags in this script's case block" sends the caller
    to read source, which is barely better than the silent swallow it replaced.
    """
    r = _run(wrapper, argv)
    assert "Accepted flags:" in r.stderr, (
        f"{wrapper}: refusal does not enumerate the accepted set.\n{r.stderr}"
    )


@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_help_exits_0_and_is_not_refused(wrapper):
    """`--help` must not be caught by the refusal.

    `--help` is a `-*` token, so adding the refusal REGRESSED it from a silent
    no-op into an exit-2 error — a regression the fix introduced rather than a
    defect it removed, and on the worst possible token: the g-115-4428 addendum
    measured that `--help` is the first thing a caller types at an unfamiliar
    wrapper. Help is a successful invocation, so rc is 0, not 2.
    """
    r = _run(wrapper, ["--help"])
    assert r.returncode == 0, (
        f"{wrapper} --help returned {r.returncode}, expected 0.\n"
        f"stdout={r.stdout!r}\nstderr={r.stderr!r}"
    )
    assert "refusing" not in (r.stdout + r.stderr).lower(), (
        f"{wrapper}: --help fell through to the unknown-flag refusal."
    )
    assert "Usage:" in r.stdout


# Wrappers that accept at least one real flag, so `<flag> --help` is a valid
# invocation. pipeline-update-field takes three positionals and NO flags, so
# `--source world --help` correctly refuses at --source before reaching --help —
# excluding it here is a scope statement, not a waiver.
HELP_AFTER_FLAG = [
    ("aspirations-update-goal.sh", ["--source", "world", "--help"]),
    ("aspirations-update.sh", ["--source", "world", "--help"]),
    ("aspirations-add-goal.sh", ["--source", "world", "--help"]),
    # NOTE the flag differs deliberately: --source is exactly what
    # aspirations-query.sh now REFUSES, so reusing it here would assert the
    # opposite of this test's intent. Its real flags are the four below.
    ("aspirations-query.sh", ["--goal-status", "completed", "--help"]),
    # : this wrapper's help was a `case "${1-}"` pre-parse block testing
    # $1 ALONE — the exact shape whose regression this test was written for on
    # aspirations-add-goal.sh. Adding the -*) refusal would have made
    # `--source world --help` exit 2, so the same pre-scan loop was adopted there.
    # --source is genuinely accepted by this wrapper, so it is the right probe here
    # (unlike aspirations-query.sh above, which refuses it).
    ("aspirations-complete-by.sh", ["--source", "world", "--help"]),
]


@pytest.mark.parametrize(
    "wrapper,argv", HELP_AFTER_FLAG, ids=[c[0] for c in HELP_AFTER_FLAG]
)
def test_help_works_after_an_accepted_flag(wrapper, argv):
    """`--help` must work at ANY position, not only as argv[1].

    Found by fresh-eyes on this goal's own diff (F-001), because the test above
    only ever passed --help as the FIRST argument. aspirations-add-goal.sh keeps
    its help in a pre-parse block testing `$1` alone rather than in an
    `-h|--help)` case arm, so `--source world --help` fell through to the new
    refusal and exited 2. The $1-only form was harmless for as long as unknown
    flags were silently swallowed — the refusal is what converted a latent
    asymmetry into a live regression. That is the whole shape guard-2680 names:
    a refusal-only test suite cannot see what the refusal broke.
    """
    r = _run(wrapper, argv)
    assert r.returncode == 0, (
        f"{wrapper} {' '.join(argv)} returned {r.returncode}, expected 0.\n"
        f"stderr={r.stderr!r}"
    )
    assert "refusing" not in (r.stdout + r.stderr).lower()


@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_refusal_precedes_runtime_source(wrapper):
    """Structural: the guard must be sourced BEFORE _runtime.sh.

    Behavioural tests alone cannot pin this. If someone moves the source line
    below `_runtime.sh`, the refusal still fires on a healthy box and every test
    above stays green — but on a box where the daemon fails to spawn, the wrapper
    now dies at the daemon step and the caller sees a transport error instead of
    the refusal. Same class as the `argv_strict` header's exit-2 warning: a guard
    whose failure mode is masked by an unrelated error is not a guard.
    """
    text = (SCRIPTS / wrapper).read_text(encoding="utf-8")
    strict = text.index('source "$CORE_ROOT/scripts/_argv_strict.sh"')
    runtime = text.index('source "$CORE_ROOT/scripts/_runtime.sh"')
    assert strict < runtime, (
        f"{wrapper}: _argv_strict.sh is sourced AFTER _runtime.sh — a daemon "
        f"failure would mask the unknown-flag refusal."
    )


@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_no_silent_passthrough_arm_remains(wrapper):
    """No `-*)` arm may still append to PASSTHROUGH and shift.

    The defect is a SHAPE, not a message. This pins the shape so a future edit
    reintroducing a catch-all is caught even if it keeps the refusal text nearby.
    """
    text = (SCRIPTS / wrapper).read_text(encoding="utf-8")
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() != "-*)":
            continue
        # Walk to the arm terminator, ignoring comments.
        #
        # SKIP COMMENTS *DURING* THE WALK, not after it. This filtered them only
        # after breaking, so a `;;` appearing inside a COMMENT was treated as the
        # arm terminator — the walk stopped early, every collected line was a
        # comment, and `code` came out EMPTY. The assert below then reported
        # "does not refuse" against an arm that refuses perfectly well, naming
        # the wrapper rather than the comment. Measured 2026-08-22 on cc-07 while
        # adopting aspirations-clear-stale-claims.sh, whose `-*)` comment quoted
        # the old catch-all arm verbatim. Fails CLOSED (a false alarm, not a
        # miss), so nothing shipped behind it — but the message points at the
        # wrong thing, which costs the next adopter real time.
        body = []
        for follow in lines[i + 1 : i + 40]:
            if follow.strip().startswith("#"):
                continue
            body.append(follow)
            if ";;" in follow:
                break
        code = "\n".join(body)
        # An empty body is a DIFFERENT defect from a non-refusing one; say so
        # rather than letting it masquerade as the latter.
        assert code.strip(), (
            f"{wrapper}: the `-*)` arm at line {i+1} has no non-comment body "
            f"within 40 lines. Either the arm is empty or the walk never found "
            f"its terminator — this is NOT the same as 'does not refuse'."
        )
        assert "argv_strict_refuse_unknown" in code, (
            f"{wrapper}: the `-*)` arm at line {i+1} does not refuse.\n{code}"
        )
        assert "PASSTHROUGH" not in code, (
            f"{wrapper}: the `-*)` arm at line {i+1} still appends to PASSTHROUGH "
            f"— an UNKNOWN flag must never reach that array. (This says nothing "
            f"about whether the array itself is dead: on tree-read.sh it is LIVE "
            f"and feeds tree.py on the fallback branch. The arm is the defect, "
            f"not the array — see test_tree_read_passthrough_still_feeds_fallback.)"
        )


def test_tree_read_passthrough_still_feeds_fallback():
    """tree-read.sh's PASSTHROUGH is LIVE — the delete convention must not reach it.

    Two sibling wrappers in this rollout (pipeline-read.sh, aspirations-read.sh)
    had PASSTHROUGH arrays with NO reader, and the adoption correctly DELETED
    them; pipeline-read.sh's own comment says so verbatim ("now deleted — it was
    never read"). Applying that convention here would be silently destructive:
    this array is the argv handed to tree.py on the FORCE_FALLBACK branch, so
    deleting it strips every argument from --validate, --by-l1, --find,
    --active-content and the three *-candidates flags — and those flags would
    keep exiting 0 while answering the wrong question, which is the exact defect
    class the rollout exists to remove.

    MEASURED 2026-08-22 (cc-07), which is why the asymmetry is worth pinning:
    before the refusal landed, `--node root --bogus-flag XVAL` was rc=0 and
    byte-identical to `--node root` (daemon path, array unread), while
    `--validate --bogus-flag XVAL` was ALREADY rc=2 from tree.py's argparse
    (fallback path, array read). One wrapper, one array, opposite behaviour.

    Structural rather than behavioural on purpose: the failure being guarded is
    someone DELETING the array, and a behavioural test would need the fallback
    path's cross-file computation to notice.
    """
    text = (SCRIPTS / "tree-read.sh").read_text(encoding="utf-8")
    lines = [l for l in text.splitlines() if not l.strip().startswith("#")]
    code = "\n".join(lines)

    assert "declare -a PASSTHROUGH=()" in code, (
        "tree-read.sh no longer declares PASSTHROUGH. If this was a 'delete the "
        "dead array' cleanup, it is wrong here — the array is read on the "
        "FORCE_FALLBACK branch."
    )
    assert 'tree.py" read' in code or "tree.py' read" in code, (
        "tree-read.sh no longer execs tree.py — if the fallback branch was "
        "removed deliberately, retire this test with it."
    )
    # The exec must still forward the array; a bare `exec python3 tree.py read`
    # would run the fallback flags with no arguments at all.
    #
    # Anchor on the EXEC FORM (`tree.py" read`), not on the bare string
    # "tree.py". The first draft used the bare string and failed against correct
    # code: the wrapper's own --help text names tree.py while explaining the
    # fallback routing, that text is a shell string literal rather than a
    # comment so the filter above keeps it, and it sorts EARLIER in the file
    # than the exec. `.index()` returns the first match, so the window landed on
    # prose. A probe must not match text the probe's own subject merely talks
    # about (guard-1238 class).
    exec_idx = code.index('tree.py" read')
    tail = code[exec_idx : exec_idx + 400]
    assert "PASSTHROUGH" in tail, (
        "tree-read.sh execs tree.py WITHOUT forwarding PASSTHROUGH — every "
        "fallback flag (--validate, --by-l1, --find, --active-content, "
        "--*-candidates) would lose its arguments."
    )
    # And at least one ACCEPTED arm must still populate it, or the forward is empty.
    assert code.count("PASSTHROUGH+=") >= 5, (
        f"tree-read.sh populates PASSTHROUGH from only "
        f"{code.count('PASSTHROUGH+=')} arm(s) — the accepted-flag arms that feed "
        f"the fallback path appear to have been stripped."
    )


def test_run_never_hands_the_sh_path_to_the_os(monkeypatch):
    """ — the harness's OWN argv shape, pinned so a revert goes RED HERE.

    WHY THIS EXISTS, AND WHY THE OTHER 23 CASES CANNOT REPLACE IT
    This file spent its whole life exec'ing `.sh` paths directly. On Linux that
    works (the kernel honours the shebang), so every case above passed on the
    fleet boxes while all 23 died `OSError: [WinError 193]` on win32 — Windows
    has no shebang handling, so a shell script is not a valid process image.
    The suite was therefore GREEN ON THE DEFECT, and a green Linux run is not
    evidence the harness is correct (guard-1943: a suite certifies the FUNCTION,
    never the WIRING). Re-running the cases above on Linux after the fix proves
    nothing either — they passed before it. This case is the discrimination.

    It asserts the argv `_run` actually builds, not the outcome of running it,
    so it is platform-independent and fails on Linux the moment someone hands
    the script path back to the OS. Both guards are pinned separately because
    each has its own silent failure mode and satisfying only one is worse than
    obvious breakage:

      argv[0] — guard-580. A `.sh` is not a valid Win32 image; a BARE "bash"
        is worse still, because CreateProcess searches System32 before PATH and
        reaches the WSL launcher, which blocks forever on a dead LxssManager
        (guard-1040, rc=124 after the 120s timeout × 23 cases).
      argv[1] — guard-581. `str(WindowsPath)` yields backslashes, which bash
        treats as escape introducers and STRIPS, silently producing a path that
        does not exist. `.as_posix()` is the fix. On Linux `str()` and
        `as_posix()` are identical, which is exactly why this pin has to be an
        equality against `as_posix()` rather than a backslash scan — a scan
        would be vacuous on the only platform most runs happen on.
    """
    captured = {}

    class _Dummy:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _Dummy()

    # monkeypatch (not a module-level stub) per guard-1165 — this must not leak
    # into any other test in the session.
    monkeypatch.setattr(subprocess, "run", _fake_run)
    _run("aspirations-update-goal.sh", ["--help"])

    argv = captured["argv"]
    script = SCRIPTS / "aspirations-update-goal.sh"

    assert argv[0] != str(script), (
        "argv[0] is the .sh path itself — this is the g-306-231 defect verbatim. "
        "On win32 CreateProcess raises OSError [WinError 193]; on Linux it "
        "silently works, which is how this survived. Use bash_cmd()."
    )
    assert Path(argv[0]).name.startswith("bash"), (
        f"argv[0]={argv[0]!r} is not a bash binary — guard-580 requires the "
        "RESOLVED interpreter as argv[0]."
    )
    assert argv[1] == script.as_posix(), (
        f"argv[1]={argv[1]!r} is not the .as_posix() form of the script path. "
        "guard-581: backslashes reaching bash are stripped as escapes, yielding "
        "a nonexistent path. This assertion is identical to str() on Linux and "
        "only bites on Windows — that asymmetry is the point."
    )
    assert argv[2:] == ["--help"], (
        f"trailing args were not passed through verbatim: {argv[2:]!r}"
    )
