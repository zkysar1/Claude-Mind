"""test_worker_closure_evidence.py —  regression suite.

WHAT BROKE. Closure evidence is the `outcome_note` field on a goal record. Until
this goal, exactly ONE thing produced it on the close path: `iteration-close.sh
do_verify` Step 3 (g-115-5157), which only the REDUCER ran. A worker Body
skipped verify entirely at the time — worker-loop Phase 4 named do_verify Step 3
by name — so no code produced a worker's evidence at all. (Since 2026-08-16 the
worker DOES call do_verify at Phase 4a for the status write, ordered after this
producer; see test_worker_loop_closes_through_do_verify_after_the_evidence_write.)

WHY THE MEASUREMENT LOOKED FINE, which is the part worth carrying. On 2026-08-09
the live worker sat at 48/48 notes and every other SID at 24/26, so the
asymmetry g-115-5158 was filed to prevent did not exist. That rate was
DISPOSITIONAL — one agent following a contract by hand — not mechanical. Nothing
would have caught the next Body that skipped it, and arming any enforcement gate
on outcome_note would have refused 100% of worker closures while passing reducer
ones, manufacturing the very disparity the goal exists to prevent.

FIFTH INSTANCE of the inheritance gap: the Mind/Body split created a second loop
orchestrator, and protections written when `aspirations` was the only loop key on
something a worker lacks (workers never pulled; skill-dedup listed only
aspirations*; deadman documented for /aspirations; agent-watchdog --tick had one
caller in iteration-close). All fail OPEN and QUIET. So does this one.

THE TWO HALVES, AND WHY BOTH ARE PINNED (guard-1943 — pinning the writer says
nothing about the wiring). A correct helper that nothing calls is exactly the
g-306-227 shape, where a fixed writer sat inert because no caller existed and its
own tests stayed green throughout. So this file asserts BEHAVIOUR of the shared
producer AND that both orchestrators actually invoke it.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from _bash_helpers import BASH  # rb-1472: bin-first, clean-PATH-safe

SCRIPTS = Path(__file__).resolve().parent.parent
HELPER = SCRIPTS / "closure-evidence-write.sh"
ITERATION_CLOSE = SCRIPTS / "iteration-close.sh"
WORKER_LOOP = (SCRIPTS.parent.parent / ".claude" / "skills" / "worker-loop" / "SKILL.md")

GID = "g-777-01"

#  — the provenance marker closure-evidence-write.sh appends to every
# note it writes on a RECURRING goal. Its ABSENCE is the discriminator: an
# unmarked note on a recurring goal was written by a human for THIS occurrence
# and must never be superseded.
#
# WHAT THE MARKER DOES AND DOES NOT SAY, because the fixtures below depend on the
# difference. It says "this script wrote this note". It does NOT say "at a PRIOR
# occurrence" — a worker's Phase 3.9 narrative is also written through this
# script and is therefore also marked. That case stays the job of --no-supersede,
# where the CALLER declares it is mid-occurrence. The two layer: the marker
# catches hand-written notes, the flag catches same-occurrence script writes.
# Fixtures representing script-written notes must therefore carry the marker, or
# they select the wrong branch and prove the wrong thing.
CE_AUTO_MARK = "[closure-evidence:auto]"
CE_DEFER_MARK = "[closure-evidence:deferred]"


def _marked(body, ach=1, when="2026-08-20T04:00:00"):
    """Render `body` as this script would have written it on a recurring goal."""
    return (f"{body}\n\n{CE_AUTO_MARK} written {when} by "
            f"closure-evidence-write.sh (achievedCount={ach}) - absence of this "
            f"line on a recurring goal means a human wrote the note, so it is "
            f"never superseded; see g-115-7733.")


def _deferred(body, ach=1, when="2026-08-25T04:00:00"):
    """Render `body` as the DECLINE path would have preserved it ().

    The deferral marker records the achievedCount it was stamped at, and that
    number is load-bearing rather than decorative: it is what separates a NEXT
    occurrence (stamped count < current) from a SAME-occurrence retry (stamped
    count == current). A presence-only fixture would pass both tests below and
    prove neither.

    THIS HELPER ARMS THE DISCRIMINATOR ON PURPOSE, and that is the distinction
    the goal's own check 1 is about. A fixture standing in for a real stamped
    note must match; what must NOT match is PROSE that merely mentions the
    token, which is why the matcher is anchored to line start plus the full
    written shape and why TestAnchoredMarkerMatch below exists.
    """
    return (f"{body}\n\n{CE_DEFER_MARK} written {when} by "
            f"closure-evidence-write.sh (achievedCount={ach}) - the note above "
            f"predates the provenance marker or was hand-written, so THIS "
            f"occurrence preserved it rather than superseding it. The NEXT "
            f"occurrence (achievedCount > {ach}) MAY supersede it. Prior text "
            f"is recoverable from world/.history/snapshots/aspirations.jsonl/; "
            f"see g-115-7853.")

STUB_UPDATE = """#!/usr/bin/env bash
{ for a in "$@"; do printf '%s\\n' "---ARG---"; printf '%s\\n' "$a"; done; } >> "$UPDATE_SINK"
exit 0
"""

# EXISTING_NOTE selects the never-clobber branch. Q_RECURRING / Q_ACHIEVED
# select the  recurring-supersede branch; their DEFAULTS reproduce the
# pre- record shape (one-shot, never closed), so every test written
# before that goal keeps exercising the never-clobber path unchanged.
STUB_QUERY = """#!/usr/bin/env bash
printf '[{"id":"%s","goal_id":"%s","recurring":%s,"achievedCount":%s,"outcome_note":%s}]\\n' \\
    "$QGID" "$QGID" "${Q_RECURRING:-false}" "${Q_ACHIEVED:-0}" "$(printf '%s' "${EXISTING_NOTE:-}" | "$PY_REAL" -c 'import json,sys; sys.stdout.write(json.dumps(sys.stdin.read()))')"
exit 0
"""

# The PRE- record shape: no `recurring`, no `achievedCount`. Used to
# prove the metadata read fails CLOSED — a record the probe cannot classify must
# refuse, never supersede.
STUB_QUERY_LEGACY = """#!/usr/bin/env bash
printf '[{"id":"%s","goal_id":"%s","outcome_note":%s}]\\n' \\
    "$QGID" "$QGID" "$(printf '%s' "${EXISTING_NOTE:-}" | "$PY_REAL" -c 'import json,sys; sys.stdout.write(json.dumps(sys.stdin.read()))')"
exit 0
"""


def _stage(tmp_path, query_stub=None):
    core = tmp_path / "core" / "scripts"
    core.mkdir(parents=True)
    (core / "closure-evidence-write.sh").write_text(
        HELPER.read_text(encoding="utf-8"), encoding="utf-8")
    (core / "aspirations-update-goal.sh").write_text(STUB_UPDATE, encoding="utf-8")
    (core / "aspirations-query.sh").write_text(
        query_stub or STUB_QUERY, encoding="utf-8")
    for f in core.iterdir():
        f.chmod(0o755)
    return core / "closure-evidence-write.sh"


def _env(tmp_path, existing_note="", recurring=False, achieved=0):
    import sys
    return {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "PY_REAL": sys.executable,
        "UPDATE_SINK": str(tmp_path / "update-argv.txt"),
        "QGID": GID,
        "EXISTING_NOTE": existing_note,
        "Q_RECURRING": "true" if recurring else "false",
        "Q_ACHIEVED": str(achieved),
    }


def _run(tmp_path, script, args, existing_note="", recurring=False, achieved=0):
    proc = subprocess.run(
        [BASH, Path(script).as_posix()] + args,
        capture_output=True, text=True,
        env=_env(tmp_path, existing_note, recurring, achieved),
        timeout=120)
    return proc, _note_writes(tmp_path)


def _note_writes(tmp_path):
    """argv lists for every aspirations-update-goal.sh call that set outcome_note.

    Split on the ---ARG--- sentinel, never on newlines: a real narrative is
    multi-paragraph and a line-based parse would shred one argv entry into many
    and then silently miscount the writes.
    """
    sink = tmp_path / "update-argv.txt"
    if not sink.exists():
        return []
    raw = sink.read_text(encoding="utf-8")
    argvs, cur = [], []
    for chunk in raw.split("---ARG---\n")[1:]:
        cur.append(chunk.rstrip("\n"))
    # every invocation ends when 'outcome_note' is followed by its value; the
    # stub appends all args of all calls in order, so group by the marker.
    if "outcome_note" in cur:
        argvs.append(cur)
    return argvs


# ─── the shared producer's behaviour ────────────────────────────────────


class TestProducerBehaviour:

    def test_writes_the_note_when_absent(self, tmp_path):
        """Positive control for every negative below — without this, a suite of
        'did not write' assertions passes just as well against a script that can
        never write at all (rb-4133 / guard-1220)."""
        script = _stage(tmp_path)
        note = tmp_path / "n.txt"
        note.write_text("worker closed this unit\nsecond paragraph", encoding="utf-8")
        proc, writes = _run(tmp_path, script,
                            ["--goal", GID, "--source", "world",
                             "--summary-file", str(note),
                             "--prefix", "[worker-loop] close:"])
        assert proc.returncode == 0, proc.stderr
        assert len(writes) == 1, f"expected exactly one outcome_note write: {writes}"
        argv = writes[0]
        assert argv[argv.index("outcome_note") + 1] == \
            "worker closed this unit\nsecond paragraph", \
            f"the narrative was altered on the way to the record: {argv!r}"
        assert GID in argv, f"the write targeted the wrong goal: {argv}"

    def test_never_clobbers_an_existing_note(self, tmp_path):
        """An agent who wrote a richer note by hand must not have it replaced —
        aspirations-update-goal.sh has no append mode, so a write is an
        overwrite."""
        script = _stage(tmp_path)
        note = tmp_path / "n.txt"
        note.write_text("short verify summary", encoding="utf-8")
        proc, writes = _run(tmp_path, script,
                            ["--goal", GID, "--source", "world",
                             "--summary-file", str(note)],
                            existing_note="a much longer hand-authored note")
        assert proc.returncode == 0, proc.stderr
        assert writes == [], f"an existing note was overwritten: {writes}"
        assert "already present" in proc.stderr, (
            "the skip must be ANNOUNCED, not silent — a silent decline is "
            "indistinguishable from a failed write")

    def test_the_clobber_guard_is_reachable_both_ways(self, tmp_path):
        """Two-way proof (guard-1220): a guard that only ever takes one branch
        is not a guard. Same inputs, only EXISTING_NOTE differs."""
        s1 = _stage(tmp_path / "absent")
        s2 = _stage(tmp_path / "present")
        n1 = tmp_path / "n1.txt"; n1.write_text("x", encoding="utf-8")
        _, absent = _run(tmp_path / "absent", s1,
                         ["--goal", GID, "--source", "world", "--summary-file", str(n1)])
        _, present = _run(tmp_path / "present", s2,
                          ["--goal", GID, "--source", "world", "--summary-file", str(n1)],
                          existing_note="already here")
        assert len(absent) == 1, f"absent-branch did not write: {absent}"
        assert present == [], f"present-branch wrote anyway: {present}"

    def test_empty_summary_writes_nothing_and_still_exits_zero(self, tmp_path):
        """Guarded on non-empty SUMMARY (guard-1423): a caller passing none must
        close exactly as before."""
        script = _stage(tmp_path)
        proc, writes = _run(tmp_path, script, ["--goal", GID, "--source", "world"])
        assert proc.returncode == 0, proc.stderr
        assert writes == [], f"wrote a note with no summary supplied: {writes}"

    def test_probe_only_returns_the_existing_note_and_writes_nothing(self, tmp_path):
        script = _stage(tmp_path)
        proc, writes = _run(tmp_path, script,
                            ["--goal", GID, "--source", "world", "--probe-only"],
                            existing_note="the note on the record")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == "the note on the record", repr(proc.stdout)
        assert writes == [], "probe-only must never write"

    def test_probe_only_is_empty_when_absent(self, tmp_path):
        """Fail-open: empty means 'unknown or absent', never 'verified absent'.
        Load-bearing in the safe direction only — empty => write."""
        script = _stage(tmp_path)
        proc, _ = _run(tmp_path, script,
                       ["--goal", GID, "--source", "world", "--probe-only"])
        assert proc.returncode == 0
        assert proc.stdout == "", repr(proc.stdout)

    def test_unknown_flag_is_refused_not_swallowed(self, tmp_path):
        """ / : a write-only PASSTHROUGH array is how a value
        meant for a flag gets dropped while the command still exits 0."""
        script = _stage(tmp_path)
        proc, writes = _run(tmp_path, script,
                            ["--goal", GID, "--source", "world", "--summry", "typo"])
        assert proc.returncode == 2, (
            f"an unknown flag must be refused, got rc={proc.returncode}")
        assert writes == []

    def test_missing_goal_is_refused(self, tmp_path):
        script = _stage(tmp_path)
        proc, _ = _run(tmp_path, script, ["--source", "world", "--summary", "x"])
        assert proc.returncode == 2, f"--goal is required, got rc={proc.returncode}"

    def test_the_probe_passes_no_flag_aspirations_query_refuses(self):
        """ port, and the reason it is pinned HERE rather than trusted.

        aspirations-query.sh REFUSES unknown flags (rc=2) as of 21c516981. It
        never parsed `--source` or `--json` — both landed in a write-only
        PASSTHROUGH array — so passing them now hard-fails the query. This
        probe suppresses the query's stderr and falls back to empty, and empty
        means 'no note -> safe to write', so a refusal here silently DISARMS
        the never-clobber guard above.

        g-115-5214 knew that and deliberately fixed its caller BEFORE landing
        the refusal. That care was defeated by a merge: this file was new and
        unconflicted, so it re-introduced the exact caller shape they had
        removed, and nothing complained. This assertion is what makes the next
        re-introduction fail loudly instead.

        Note --source IS still forwarded to the WRITE (aspirations-update-goal.sh
        parses it) — only the QUERY must not receive it."""
        src = (SCRIPTS / "closure-evidence-write.sh").read_text(encoding="utf-8")
        # isolate the query invocation, not the prose explaining it
        call = [ln for ln in src.splitlines() if "aspirations-query.sh" in ln
                and not ln.lstrip().startswith("#")]
        assert len(call) == 1, f"expected exactly one query invocation: {call}"
        line = call[0]
        for bad in ("--source", "--json"):
            assert bad not in line, (
                f"the probe passes {bad} to aspirations-query.sh, which REFUSES "
                f"it with rc=2 — the probe then returns empty, and empty means "
                f"'safe to write', silently disarming never-clobber (g-115-5214)")
        assert "--goal-field id" in line and "--full" in line, (
            f"the probe no longer asks the query it needs: {line}")
        # And the write half must STILL forward --source, or the port went too far.
        assert "aspirations-update-goal.sh" in src and "--source" in src, (
            "--source was stripped from the WRITE as well; that wrapper does "
            "parse it and the two halves are deliberately asymmetric")

    def test_a_flag_passed_last_with_no_value_terminates(self, tmp_path):
        """guard-1224 — and the reason this test exists is worth more than the
        test. The first version of this helper had FIVE arms with `${2:-}` +
        bare `shift 2`. With the flag passed LAST, `${2:-}` substitutes empty
        and `shift 2` FAILS at $#==1; the script sets -uo pipefail but not -e,
        so $1 never advances and the parse loop spins forever.

        The twelve tests above all passed against that version. Not one of them
        passed a flag last with no value — the shape is invisible unless you
        aim at it. The repo-wide scanner (test_shift2_argv_hang.py) is what
        caught it, on the first full-suite run after the file was written.

        A short explicit timeout is load-bearing: the defect's signature is a
        HANG, so the assertion has to be 'it came back', not 'it came back
        with rc N'."""
        script = _stage(tmp_path)
        for flag in ("--goal", "--source", "--summary", "--summary-file", "--prefix"):
            proc = subprocess.run(
                [BASH, Path(script).as_posix(), "--goal", GID, flag],
                capture_output=True, text=True, env=_env(tmp_path), timeout=15)
            assert proc.returncode in (0, 2), (
                f"{flag} passed last returned rc={proc.returncode}: {proc.stderr}")


# ─── the WIRING — the half that actually closes the gap ─────────────────


class TestBothOrchestratorsCallIt:
    """guard-1943: pinning the writer says nothing about the wiring. The
    g-306-227 defect was a CORRECT writer with no caller, whose own tests stayed
    green through the entire outage."""

    def test_worker_loop_invokes_the_shared_producer(self):
        src = WORKER_LOOP.read_text(encoding="utf-8")
        assert "closure-evidence-write.sh" in src, (
            "worker-loop no longer calls closure-evidence-write.sh — a worker "
            "Body has NO closure-evidence producer again (g-115-5158). This is "
            "the whole gap; the helper existing is not the fix.")

    def test_iteration_close_invokes_the_same_producer(self):
        src = ITERATION_CLOSE.read_text(encoding="utf-8")
        assert "closure-evidence-write.sh" in src, (
            "iteration-close no longer routes to closure-evidence-write.sh — the "
            "reducer and worker paths have diverged into two implementations, "
            "which is the transcription guard-2676 forbids")

    def test_worker_evidence_write_is_ordered_after_the_carrier_check(self):
        """Phase 3.7's STRANDED branch also writes outcome_note, and the helper
        is never-clobber. Ordering the evidence write BEFORE 3.7 would silently
        make a stranding unrecordable — the note would already exist."""
        src = WORKER_LOOP.read_text(encoding="utf-8")
        carrier = src.find("check-outputs")
        evidence = src.find("closure-evidence-write.sh")
        assert carrier != -1 and evidence != -1, "anchors missing from worker-loop"
        assert evidence > carrier, (
            "the closure-evidence write now runs BEFORE the Phase 3.7 carrier "
            "check; never-clobber then prevents a STRANDED output from ever "
            "reaching the goal record")

    def test_worker_loop_closes_through_do_verify_after_the_evidence_write(self):
        """REVERSED 2026-08-16 (). Outcome 2 of  pinned the
        OPPOSITE — "worker-loop must not call do_verify; a worker cannot run that
        phase" — and that pin encoded the assumption whose consequence was then
        measured: no reducer lane ever flipped a worker goal's status
        (worker_retrospective.py has no close lane, body-merge.py only names ids),
        so 360 of 361 open alpha claims were finished work left at in-progress
        forever, hidden from every selector by SKIP_STATUSES. The worker now
        records the status IT judged through the SHARED close writer (Phase 4a,
        a scoped call — guard-2676), ordered AFTER the rich closure-evidence
        write so do_verify's write-if-absent note write declines instead of
        winning. The LLM verify phase (/aspirations-verify) stays reducer-only;
        see the REDUCER_ONLY_PHASES 'verify' note in worker_execute.py.

        The second half of the old pin still stands: no hand-rolled in_flight
        clear on the worker path (g-306-132-d) — do_verify's own clear is
        goal-scoped (--if-goal), which is the only shape that cannot blank a
        live reducer's row."""
        src = WORKER_LOOP.read_text(encoding="utf-8")
        close = src.find("iteration-close.sh --phase verify")
        assert close != -1, (
            "worker-loop no longer calls iteration-close.sh --phase verify — a "
            "worker completion then stays at in-progress with nothing downstream "
            "to close it (g-115-6337: 360/361 open claims were finished work)")
        evidence = src.find("closure-evidence-write.sh")
        assert evidence != -1 and close > evidence, (
            "the status write must come AFTER the closure-evidence write: "
            "do_verify's note write is write-if-absent, so ordering it first "
            "would let the one-line --summary win over the rich narrative")
        assert "team-state-clear-in-flight" not in src, (
            "a second in_flight clear on the worker path defeats the "
            "claimed_by_sid ownership test (g-306-132-d)")

    def test_worker_loop_appends_the_hand_off_row(self):
        """body-merge.py `_completed_goal_ids` reads the Body WM's
        goals_completed_this_session slot to name merged_goal_ids, which is the
        ONLY input to the consolidate Step -0.9 worker retrospective. worker-loop
        carried zero references to that slot before 2026-08-16, so the lane had
        never fired once."""
        src = WORKER_LOOP.read_text(encoding="utf-8")
        assert "wm-append.sh goals_completed_this_session" in src, (
            "worker-loop no longer appends the goals_completed_this_session row — "
            "merged_goal_ids is then always empty and worker_retrospective.py never "
            "runs (g-306-198 lane dark)")

    def test_do_verify_speaks_to_the_body_role_that_called_it(self):
        """Fresh-eyes review of the 2026-08-16 Phase 4a change: two do_verify
        lines addressed the REDUCER unconditionally and became wrong the moment
        a worker Body started calling the same writer. (1) The uncommitted-work
        auto-override reason said "iteration-commit.sh scheduled in state-update"
        — a phase a worker never runs, so its audit-ledger row would be false.
        (2) The terminal "NEXT: Phase 6 spark REQUIRED — invoke
        Skill(aspirations-spark)" imperative contradicts worker-loop Phase 4c
        (spark is reducer-only; the worker's spark obligation is Phase 3.5
        spark_capture). Both now branch on the hook-injected BODY_ROLE env, and
        the reducer text is unchanged so the g-115-2416 pin still holds."""
        src = ITERATION_CLOSE.read_text(encoding="utf-8")
        start = src.find("do_verify() {")
        # Slice to the NEXT top-level function, not the first "\n}\n" — do_verify
        # embeds python heredocs whose dict literals close with a bare "}" line.
        end = src.find("\ndo_state_update() {", start)
        assert start != -1 and end != -1, "do_verify / do_state_update boundaries not found"
        body = src[start:end]
        assert 'BODY_ROLE:-}" == "worker"' in body, (
            "do_verify no longer branches on BODY_ROLE — a worker close gets the "
            "reducer's false override reason and the reducer's spark imperative")
        assert "worker Body — HEAD pushed to refs/workers" in body, (
            "worker-specific uncommitted-work override reason missing")
        assert "do NOT invoke Skill(aspirations-spark)" in body, (
            "worker-specific NEXT line missing — the reducer imperative would "
            "tell a worker to run a reducer-only phase")
        assert "Phase 6 spark REQUIRED" in body, (
            "reducer NEXT imperative must remain for the reducer path (g-115-2416)")


# ─── : the recurring-supersede branch ─────────────────────────
#
# WHAT BROKE. never-clobber is right for a one-shot goal and wrong for a
# recurring one: the goal RECORD persists across occurrences (status flips back
# to pending, lastAchievedAt is restamped) and NOTHING clears outcome_note, so
# occurrence N's note is still there when N+1 closes. The guard cannot tell that
# from a hand-written note, declines, and every occurrence after the first loses
# its evidence at rc=0 behind a message that reads as correct behaviour.
# Measured 2026-08-16 (guard-3983): of 56 recurring notes carrying a parseable
# date, 19 (34%) were STALE.
#
# WHY REPLACE AND NOT APPEND. guard-3626 forbids a bare set because the field is
# "accumulated evidence of every prior cycle"; guard-3983 forbids append because
# "a goal at achievedCount 308 cannot carry 308 appended notes". The HEADER is
# what reconciles them — guard-3626's real concern is silent destruction.

class TestRecurringSupersede:

    NOTE = _marked("prior occurrence narrative, 6 hours old")

    def test_recurring_at_achieved_two_supersedes_the_prior_note(self, tmp_path):
        """Verification outcome 1: a recurring goal closed twice in a row with a
        distinct --summary each time carries the SECOND summary."""
        script = _stage(tmp_path)
        note = tmp_path / "n.txt"
        note.write_text("second occurrence summary", encoding="utf-8")
        proc, writes = _run(tmp_path, script,
                            ["--goal", GID, "--source", "world",
                             "--summary-file", str(note)],
                            existing_note=self.NOTE, recurring=True, achieved=2)
        assert proc.returncode == 0, proc.stderr
        assert len(writes) == 1, (
            f"a recurring re-close still dropped its evidence: {writes!r} "
            f"stderr={proc.stderr!r}")
        argv = writes[0]
        written = argv[argv.index("outcome_note") + 1]
        assert "second occurrence summary" in written, (
            f"the fresh summary is not on the record: {written!r}")

    def test_the_supersede_is_announced_on_stdout_not_stderr(self, tmp_path):
        """guard-772: a stderr-only notice is invisible to a backgrounded or
        piped caller, which is exactly how the silent refusal stayed unnoticed
        through four guardrails. A successful write announces on stdout."""
        script = _stage(tmp_path)
        proc, _ = _run(tmp_path, script,
                       ["--goal", GID, "--source", "world",
                        "--summary", "fresh"],
                       existing_note=self.NOTE, recurring=True, achieved=7)
        assert "superseding" in proc.stdout, (
            f"supersede not announced on stdout: {proc.stdout!r}")
        assert "achievedCount=7" in proc.stdout, (
            f"the announcement does not name the occurrence: {proc.stdout!r}")

    def test_the_header_names_the_superseded_length(self, tmp_path):
        """guard-3983 requires the replacement to open with a header naming the
        run and the superseded length — that header is the ONLY thing standing
        between 'replace' and guard-3626's silent-destruction case."""
        script = _stage(tmp_path)
        proc, writes = _run(tmp_path, script,
                            ["--goal", GID, "--source", "world",
                             "--summary", "fresh"],
                            existing_note=self.NOTE, recurring=True, achieved=3)
        assert len(writes) == 1, f"expected a write: {writes!r} {proc.stderr!r}"
        argv = writes[0]
        written = argv[argv.index("outcome_note") + 1]
        assert "SUPERSEDES" in written, f"no supersede header: {written!r}"
        assert str(len(self.NOTE)) in written, (
            f"header does not name the superseded length {len(self.NOTE)}: {written!r}")
        assert written.startswith("["), (
            "guard-3626: aspirations-update-goal.sh parses a value beginning "
            f"with '-' as a flag and refuses it — header must not: {written[:12]!r}")

    def test_recurring_first_close_still_refuses(self, tmp_path):
        """Verification outcome 2, half one. cmd_complete_by bumps achievedCount
        BEFORE this runs on the reducer path, so achievedCount==1 means NO prior
        occurrence exists and an existing note can only be hand-written. The
        threshold fails CLOSED at exactly that boundary."""
        script = _stage(tmp_path)
        proc, writes = _run(tmp_path, script,
                            ["--goal", GID, "--source", "world",
                             "--summary", "short verify summary"],
                            existing_note="hand-authored before the first close",
                            recurring=True, achieved=1)
        assert proc.returncode == 0, proc.stderr
        assert writes == [], (
            f"a hand-written note was clobbered on a FIRST close: {writes!r}")
        assert "already present" in proc.stderr

    def test_one_shot_never_clobber_survives_at_any_achieved_count(self, tmp_path):
        """Verification outcome 2, half two — and the mutation-proof: this test
        FAILS if the `_rec -eq 1` conjunct is removed from the supersede
        condition. A high achievedCount on a NON-recurring record must not be
        enough on its own."""
        script = _stage(tmp_path)
        proc, writes = _run(tmp_path, script,
                            ["--goal", GID, "--source", "world",
                             "--summary", "short verify summary"],
                            existing_note="a much longer hand-authored note",
                            recurring=False, achieved=99)
        assert proc.returncode == 0, proc.stderr
        assert writes == [], (
            f"never-clobber was disarmed for a ONE-SHOT goal: {writes!r}")
        assert "never clobber" in proc.stderr

    def test_unclassifiable_record_fails_closed_to_refuse(self, tmp_path):
        """A record predating this fix carries neither `recurring` nor
        `achievedCount`. The probe must read that as one-shot and REFUSE — a
        broken or legacy metadata read can never turn the supersede branch on.
        This is the same asymmetry the file header states for the note itself:
        empty note => write, unreadable metadata => refuse."""
        script = _stage(tmp_path, query_stub=STUB_QUERY_LEGACY)
        proc, writes = _run(tmp_path, script,
                            ["--goal", GID, "--source", "world",
                             "--summary", "fresh"],
                            existing_note="legacy note", recurring=True, achieved=9)
        assert proc.returncode == 0, proc.stderr
        assert writes == [], (
            f"an unclassifiable record took the supersede branch: {writes!r}")

    def test_supersede_branch_is_reachable_both_ways(self, tmp_path):
        """guard-1220: a guard that only ever takes one branch is not a guard.
        Same inputs, only achievedCount differs across the threshold."""
        s1 = _stage(tmp_path / "below")
        s2 = _stage(tmp_path / "at")
        _, below = _run(tmp_path / "below", s1,
                        ["--goal", GID, "--source", "world", "--summary", "x"],
                        existing_note=_marked("p"), recurring=True, achieved=1)
        _, at = _run(tmp_path / "at", s2,
                     ["--goal", GID, "--source", "world", "--summary", "x"],
                     existing_note=_marked("p"), recurring=True, achieved=2)
        assert below == [], f"achievedCount=1 superseded anyway: {below!r}"
        assert len(at) == 1, f"achievedCount=2 did not supersede: {at!r}"

    def test_probe_only_contract_survives_the_metadata_line(self, tmp_path):
        """_probe_note is now implemented over _probe_record, which prints a
        metadata line FIRST. --probe-only must still emit ONLY the note — its
        consumer (iteration-close.sh _probe_goal_outcome_note) treats any
        non-empty output as 'a note exists', so a leaked metadata line would
        make every goal look like it already had one."""
        script = _stage(tmp_path)
        proc, _ = _run(tmp_path, script,
                       ["--goal", GID, "--source", "world", "--probe-only"],
                       existing_note="the note on the record",
                       recurring=True, achieved=5)
        assert proc.returncode == 0, proc.stderr
        assert "recurring=" not in proc.stdout, (
            f"the metadata line leaked into --probe-only: {proc.stdout!r}")
        assert proc.stdout.strip() == "the note on the record", repr(proc.stdout)

    def test_probe_only_still_empty_when_absent(self, tmp_path):
        """The metadata line must not make an ABSENT note look present — the
        fail-open contract's whole safe direction depends on empty meaning
        'unknown or absent'."""
        script = _stage(tmp_path)
        proc, _ = _run(tmp_path, script,
                       ["--goal", GID, "--source", "world", "--probe-only"],
                       recurring=True, achieved=42)
        assert proc.returncode == 0
        assert proc.stdout.strip() == "", repr(proc.stdout)

    def test_supersede_is_idempotent_on_a_retried_verify(self, tmp_path):
        """The refusal branch gets idempotency free ('a re-run finds the note it
        wrote and declines'); the supersede branch does NOT, and had to have it
        restored. recurring-close.sh documents retrying a failed verify by name,
        so without this a retry re-supersedes its own note and the header's
        'superseded N chars' counts its own previous header."""
        script = _stage(tmp_path)
        already = _marked(
            "[closure-evidence] SUPERSEDES a prior-occurrence note of 41 chars "
            "— recurring occurrence achievedCount=4.\n\nthe summary this run "
            "already wrote", ach=4)
        proc, writes = _run(tmp_path, script,
                            ["--goal", GID, "--source", "world",
                             "--summary", "the summary this run already wrote"],
                            existing_note=already, recurring=True, achieved=4)
        assert proc.returncode == 0, proc.stderr
        assert writes == [], (
            f"a retried verify re-superseded its own note: {writes!r}")
        assert "idempotent re-run" in proc.stdout, (
            f"the idempotent decline must be announced: {proc.stdout!r}")

    def test_a_DIFFERENT_summary_still_supersedes(self, tmp_path):
        """Two-way control for the idempotency check (guard-1220): the guard must
        not degrade into 'never supersede when any note exists', which would
        silently re-open the whole defect."""
        script = _stage(tmp_path)
        proc, writes = _run(tmp_path, script,
                            ["--goal", GID, "--source", "world",
                             "--summary", "a genuinely new occurrence summary"],
                            existing_note=_marked(
                                "[closure-evidence] SUPERSEDES ...\n\nlast run's text", ach=4),
                            recurring=True, achieved=4)
        assert len(writes) == 1, (
            f"the idempotency check swallowed a NEW summary: {writes!r} "
            f"stdout={proc.stdout!r}")


class TestNoSupersedeFlag:
    """ — the supersede premise is FALSE on the worker path.

    The recurring-supersede branch (g-115-6527) reasons that on a recurring goal
    an existing outcome_note can only be a PRIOR-occurrence leftover. That holds
    on the REDUCER path, where do_verify's write is the first write of the
    occurrence. On the WORKER path it is false: worker-loop Phase 3.9 writes the
    rich narrative in THIS occurrence and Phase 4a calls do_verify seconds later,
    so the branch destroyed the richest artifact a worker produces. Measured 3x
    in one session on cc-07 (4916 chars -> 563 on g-115-105; 5454 -> 380 on
    g-115-1538), each at rc=0 behind a message that reads as correct behaviour.

    EVERY NEGATIVE ASSERTION HERE IS PAIRED WITH A REACHED-THE-PATH MARKER
    (guard-2536): "no write happened" is satisfied equally by the branch
    declining correctly and by the script never running at all, so each test
    also asserts rc==0 plus the branch's own distinct stdout text. The
    without-the-flag control is the other half — it proves the fixture really
    does select the supersede branch, so a decline is caused by the flag rather
    than by a record shape that never qualified.
    """

    # MARKED: Phase 3.9 writes this narrative BY CALLING this script, so a real
    # worker note carries the marker. Leaving it unmarked would make the
    # flag-declines test pass because of the MARKER rather than the FLAG — a
    # false pass that would hide a regression in --no-supersede itself.
    RICH = _marked("worker Phase 3.9 narrative, written seconds ago in THIS "
                   "occurrence\n\nparagraph two", ach=3)

    def test_flag_declines_the_supersede_and_preserves_the_note(self, tmp_path):
        script = _stage(tmp_path)
        proc, writes = _run(tmp_path, script,
                            ["--goal", GID, "--source", "world",
                             "--summary", "one-line 4a backfill",
                             "--no-supersede"],
                            existing_note=self.RICH, recurring=True, achieved=3)
        assert proc.returncode == 0, (
            f"a completed run must exit 0; got {proc.returncode} "
            f"stderr={proc.stderr!r}")
        assert "supersede DECLINED" in proc.stdout, (
            "the decline must announce ITSELF -- without this marker the "
            "no-write assertion below cannot tell 'declined here' from "
            f"'never ran': {proc.stdout!r}")
        assert writes == [], (
            f"--no-supersede must not replace this occurrence's note: {writes!r}")

    def test_without_the_flag_the_same_record_supersedes(self, tmp_path):
        """POSITIVE CONTROL. Identical record, flag omitted -> the supersede
        branch fires. Without this, the test above would pass just as happily
        against a fixture that never selected the branch at all."""
        script = _stage(tmp_path)
        proc, writes = _run(tmp_path, script,
                            ["--goal", GID, "--source", "world",
                             "--summary", "one-line 4a backfill"],
                            existing_note=self.RICH, recurring=True, achieved=3)
        assert proc.returncode == 0, proc.stderr
        assert "superseding prior-occurrence" in proc.stdout, (
            f"control failed to reach the supersede branch: {proc.stdout!r}")
        assert len(writes) == 1, (
            f"control expected exactly one supersede write: {writes!r}")

    def test_flag_does_not_block_a_first_write(self, tmp_path):
        """The flag suppresses REPLACEMENT, never the write itself. A worker
        whose Phase 3.9 did not run must still get its backfill note."""
        script = _stage(tmp_path)
        proc, writes = _run(tmp_path, script,
                            ["--goal", GID, "--source", "world",
                             "--summary", "the only note this occurrence gets",
                             "--no-supersede"],
                            existing_note="", recurring=True, achieved=3)
        assert proc.returncode == 0, proc.stderr
        assert len(writes) == 1, (
            f"--no-supersede wrongly blocked a FIRST write: {writes!r} "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}")

    def test_flag_on_a_one_shot_goal_still_never_clobbers(self, tmp_path):
        """The flag must not disturb the one-shot path, which was already
        correct: an existing note wins and the skip is announced on stderr."""
        script = _stage(tmp_path)
        proc, writes = _run(tmp_path, script,
                            ["--goal", GID, "--source", "world",
                             "--summary", "one-line", "--no-supersede"],
                            existing_note=self.RICH, recurring=False, achieved=0)
        assert proc.returncode == 0, proc.stderr
        assert "never clobber" in proc.stderr, (
            f"one-shot must take the never-clobber branch: {proc.stderr!r}")
        assert writes == [], f"one-shot must not write: {writes!r}"

    def test_iteration_close_passes_the_flag_only_on_the_worker_path(self):
        """THE WIRING HALF (guard-1943 — pinning the writer says nothing about
        the wiring). A correct flag nothing passes is the g-306-227 shape: an
        inert fix whose own tests stay green. Asserted at source level because
        the reducer/worker split is a BODY_ROLE branch, and this file already
        pins its sibling wiring the same way."""
        src = ITERATION_CLOSE.read_text(encoding="utf-8")
        assert "--no-supersede" in src, (
            "iteration-close.sh never passes --no-supersede, so the helper "
            "change is inert on the path it was written for")
        assert 'BODY_ROLE:-}" == "worker"' in src, (
            "the flag must be conditioned on BODY_ROLE, or the reducer's "
            "legitimate prior-occurrence supersede is disabled too")

    def test_worker_loop_contract_sentence_matches_the_code(self):
        """The goal required the stale SKILL.md sentence be corrected in the
        SAME change. It claimed 4a's write is 'write-if-absent', which the
        supersede branch had already falsified."""
        txt = WORKER_LOOP.read_text(encoding="utf-8")
        assert "--no-supersede" in txt, (
            "worker-loop SKILL.md must name the mechanism that actually "
            "protects the Phase 3.9 narrative")
        assert "its note write is write-if-absent" not in txt, (
            "the falsified contract sentence is still present")


# ─── : the silent clobber and the lying failure message ───────────


# A stub that REFUSES the write exactly as field-shrink-guard does. The daemon
# shape is the live one (mind_api/src/endpoints/aspirations_write.py); the CLI
# path (core/scripts/aspirations.py) emits the same `field_shrink_blocked` token
# in prose, and the fix keys on the token so both shapes classify.
STUB_UPDATE_SHRINK = """#!/usr/bin/env bash
{ for a in "$@"; do printf '%s\\n' "---ARG---"; printf '%s\\n' "$a"; done; } >> "$UPDATE_SINK"
printf '%s\\n' '{"error":"field_shrink_blocked","gate":"field-shrink-guard","field":"outcome_note","old_len":2567,"new_len":407,"ratio":0.159,"detail":"field_shrink_blocked: writing `outcome_note` would shrink it from 2567 chars to 407 (16% of the original, floor is 25%). (goal g-777-01)"}'
exit 1
"""


def _stage_shrink(tmp_path):
    core = tmp_path / "core" / "scripts"
    core.mkdir(parents=True)
    (core / "closure-evidence-write.sh").write_text(
        HELPER.read_text(encoding="utf-8"), encoding="utf-8")
    (core / "aspirations-update-goal.sh").write_text(STUB_UPDATE_SHRINK, encoding="utf-8")
    (core / "aspirations-query.sh").write_text(STUB_QUERY, encoding="utf-8")
    for f in core.iterdir():
        f.chmod(0o755)
    return core / "closure-evidence-write.sh"


class TestSilentClobberOfAHandWrittenNote:
    """DEFECT 1 — the recurring branch had no never-clobber test and the one-shot
    branch did, so a note an agent hand-wrote for THIS occurrence was replaced.

    WHY THE SILENT CASE IS THE ONE THAT MATTERS. field-shrink-guard only refuses
    below 25%, so the LOUD case (a note >4x the summary) was already survivable
    by accident of length — measured 2026-08-25 on g-326-516, a 2,567-char note
    against a 407-char summary, ratio 0.159. A note merely LONGER but under 4x
    clears the floor, the write proceeds, and nothing is emitted on stdout,
    stderr, or the rc. That population was never measured because it leaves no
    trace at all.
    """

    # 300 chars against a 200-char summary: ratio 0.67, comfortably ABOVE the
    # 0.25 floor, so field-shrink-guard would NOT have refused this write.
    HAND_WRITTEN = "H" * 300
    SUMMARY = "S" * 200

    def test_hand_written_note_on_a_recurring_goal_survives(self, tmp_path):
        """Verification check 1. The stub update script ALWAYS exits 0, so a
        surviving note proves closure-evidence-write.sh itself declined — it
        cannot be a shrink gate saving it, because there is no gate in this
        fixture at all. That separation is the whole point: pre-fix, this exact
        record was replaced with no warning anywhere.

        ASSERTS SURVIVAL DIRECTLY, NOT VIA `writes == []` (g-115-7853). This
        test used to require that NO write occurred at all. That was a valid
        proxy while the decline path was a bare `exit 0`, and it is exactly what
        made the path permanently wedged: a note that is never written can never
        acquire a provenance marker, so every later occurrence re-entered this
        branch and dropped its evidence at rc=0. The decline now preserves the
        note AND stamps a deferral line, so the proxy is superseded — but the
        property it was defending is unchanged and is now checked head-on: the
        hand-written text must survive BYTE-EXACT, and the summary must not
        appear anywhere in the record. That is a strictly stronger claim than
        'nothing happened', because it would also catch a write that preserved
        the length while corrupting the content.
        """
        script = _stage(tmp_path)
        proc, writes = _run(tmp_path, script,
                            ["--goal", GID, "--source", "world",
                             "--summary", self.SUMMARY],
                            existing_note=self.HAND_WRITTEN,
                            recurring=True, achieved=6)
        assert proc.returncode == 0, proc.stderr
        assert len(writes) == 1, (
            f"expected exactly one preserving write: {writes!r} {proc.stderr!r}")
        argv = writes[0]
        written = argv[argv.index("outcome_note") + 1]
        assert written.startswith(self.HAND_WRITTEN), (
            "the hand-written note must survive BYTE-EXACT at the front of the "
            f"record — this is the silent clobber case: {written[:340]!r}")
        assert self.SUMMARY not in written, (
            "the summary reached the record: the note WAS clobbered, which is "
            f"the whole defect this class pins: {written!r}")
        assert CE_DEFER_MARK in written, (
            "the decline must stamp the deferral marker, or the goal stays "
            f"wedged forever (g-115-7853): {written!r}")
        assert "carries no [closure-evidence:auto]" in proc.stdout, (
            "guard-2536: the decline must announce ITSELF, or this test cannot "
            f"tell 'declined here' from 'never ran': {proc.stdout!r}")

    def test_a_marked_prior_note_with_the_same_shape_still_supersedes(self, tmp_path):
        """TWO-WAY CONTROL (guard-1220 / rb-4133). Byte-identical record except
        the marker, so the test above cannot be passing because the fixture
        never selected the supersede branch. Without this, 'never supersede when
        any note exists' would pass the sibling test and silently re-open
        g-115-6527."""
        script = _stage(tmp_path)
        proc, writes = _run(tmp_path, script,
                            ["--goal", GID, "--source", "world",
                             "--summary", self.SUMMARY],
                            existing_note=_marked(self.HAND_WRITTEN, ach=5),
                            recurring=True, achieved=6)
        assert proc.returncode == 0, proc.stderr
        assert len(writes) == 1, (
            f"a genuine prior-occurrence note stopped being superseded: {writes!r} "
            f"stdout={proc.stdout!r}")

    def test_one_shot_notes_are_still_written_byte_exact(self, tmp_path):
        """The marker is RECURRING-ONLY. A one-shot goal must still receive the
        narrative unaltered — the contract test_writes_the_note_when_absent
        pins. This asserts the narrowing from the other side."""
        script = _stage(tmp_path)
        proc, writes = _run(tmp_path, script,
                            ["--goal", GID, "--source", "world",
                             "--summary", "one-shot narrative"],
                            existing_note="", recurring=False, achieved=0)
        assert proc.returncode == 0, proc.stderr
        assert len(writes) == 1, f"expected a write: {writes!r}"
        argv = writes[0]
        assert argv[argv.index("outcome_note") + 1] == "one-shot narrative", (
            "a one-shot note must reach the record byte-exact; the provenance "
            f"marker leaked onto the one-shot path: {argv!r}")

    def test_a_recurring_first_write_is_marked_so_the_next_occurrence_can_tell(self, tmp_path):
        """The re-arm. `_rec` had to be hoisted out of the `if [[ -n $_existing ]]`
        block for this path to see it: this is the FIRST close of a recurring
        goal (no note yet), and if its write went out unmarked the next
        occurrence would read it as hand-written and never supersede again."""
        script = _stage(tmp_path)
        proc, writes = _run(tmp_path, script,
                            ["--goal", GID, "--source", "world",
                             "--summary", "first occurrence narrative"],
                            existing_note="", recurring=True, achieved=1)
        assert proc.returncode == 0, proc.stderr
        assert len(writes) == 1, f"expected a first write: {writes!r}"
        written = writes[0][writes[0].index("outcome_note") + 1]
        assert written.startswith("first occurrence narrative"), (
            f"the narrative must lead; the marker is TRAILING: {written!r}")
        assert CE_AUTO_MARK in written, (
            "a recurring first write went out UNMARKED, so the supersede branch "
            f"can never re-arm on this goal: {written!r}")

    def test_the_marker_is_trailing_so_it_does_not_eat_the_300_char_preview(self, tmp_path):
        """MEASURED PLACEMENT. aspirations.py:2810 previews the note as
        `outcome_note[:300]`. A leading marker is charged against that window,
        and the supersede header already spends ~200 of those 300 chars."""
        script = _stage(tmp_path)
        body = "N" * 280
        proc, writes = _run(tmp_path, script,
                            ["--goal", GID, "--source", "world", "--summary", body],
                            existing_note="", recurring=True, achieved=1)
        written = writes[0][writes[0].index("outcome_note") + 1]
        assert CE_AUTO_MARK not in written[:300], (
            "the provenance marker landed inside the 300-char preview window: "
            f"{written[:300]!r}")


class TestFieldShrinkRefusalMessage:
    """DEFECT 2 — the failure message asserted something the script could not
    know, and the remedy it printed destroyed the note the gate had just saved.

    When field-shrink-guard is the refuser, "the narrative is NOT on the record"
    is false BY CONSTRUCTION: the gate refused precisely BECAUSE a longer note is
    there. The prescribed re-run then either failed identically or, with
    --override-shrink, replaced the full note with the summary (guard-5049).
    """

    def test_the_refusal_does_not_claim_the_narrative_is_absent(self, tmp_path):
        """Verification check 2, half one."""
        script = _stage_shrink(tmp_path)
        proc, _ = _run(tmp_path, script,
                       ["--goal", GID, "--source", "world", "--summary", "short"],
                       existing_note="", recurring=False, achieved=0)
        assert proc.returncode == 0, "NON-FATAL BY CONTRACT — callers must not branch on rc"
        assert "is NOT on the record" not in proc.stderr, (
            "the message still asserts the narrative is absent while the gate "
            f"refused BECAUSE a longer one is present: {proc.stderr!r}")
        assert "NO ACTION IS NEEDED" in proc.stderr, (
            f"the refusal must say the note is safe: {proc.stderr!r}")
        assert "2567" in proc.stderr, (
            f"the surviving old_len must be reported: {proc.stderr!r}")

    def test_no_override_shrink_remedy_is_prescribed(self, tmp_path):
        """Verification check 2, half two, and outcome 3. --override-shrink may
        appear ONLY as a prohibition, never as an instruction."""
        script = _stage_shrink(tmp_path)
        proc, _ = _run(tmp_path, script,
                       ["--goal", GID, "--source", "world", "--summary", "short"],
                       existing_note="", recurring=False, achieved=0)
        for line in proc.stderr.splitlines():
            if "--override-shrink" in line:
                assert "Do NOT" in line or "do NOT" in line, (
                    f"--override-shrink is prescribed rather than forbidden: {line!r}")

    def test_the_gate_payload_is_surfaced_verbatim(self, tmp_path):
        """guard-3662 / guard-1007: the refusal JSON is the caller's only
        evidence for 'did this actually fail?'. The pre-fix code discarded it
        and then asserted an outcome it had no evidence for."""
        script = _stage_shrink(tmp_path)
        proc, _ = _run(tmp_path, script,
                       ["--goal", GID, "--source", "world", "--summary", "short"],
                       existing_note="", recurring=False, achieved=0)
        assert "field_shrink_blocked" in proc.stderr, (
            f"the gate's own payload was swallowed: {proc.stderr!r}")

    def test_an_unclassified_failure_still_warns_but_verifies_first(self, tmp_path):
        """TWO-WAY CONTROL: a non-shrink failure must still be loud. Otherwise
        the fix could degrade into 'never warn', which loses the real failures
        the original message existed to surface."""
        core = tmp_path / "core" / "scripts"
        core.mkdir(parents=True)
        (core / "closure-evidence-write.sh").write_text(
            HELPER.read_text(encoding="utf-8"), encoding="utf-8")
        (core / "aspirations-update-goal.sh").write_text(
            '#!/usr/bin/env bash\n'
            '{ for a in "$@"; do printf \'%s\\n\' "---ARG---"; printf \'%s\\n\' "$a"; done; } >> "$UPDATE_SINK"\n'
            'echo "daemon unreachable" >&2\nexit 7\n', encoding="utf-8")
        (core / "aspirations-query.sh").write_text(STUB_QUERY, encoding="utf-8")
        for f in core.iterdir():
            f.chmod(0o755)
        proc, _ = _run(tmp_path, core / "closure-evidence-write.sh",
                       ["--goal", GID, "--source", "world", "--summary", "x"],
                       existing_note="", recurring=False, achieved=0)
        assert proc.returncode == 0
        assert "write FAILED" in proc.stderr, (
            f"a genuine failure stopped being reported: {proc.stderr!r}")
        assert "VERIFY BEFORE WRITING" in proc.stderr, (
            f"even a genuine failure must not prescribe a blind re-run: {proc.stderr!r}")


def test_the_recurring_branch_reads_a_this_vs_prior_signal():
    """Verification check 3 — a source-level assertion, deliberately.

    The behavioural tests above prove the branch DECLINES; this proves it
    declines for the stated REASON. Without it the suite would still pass
    against a branch that refused for an unrelated coincidence.
    """
    src = HELPER.read_text(encoding="utf-8")
    assert 'CE_AUTO_MARK="[closure-evidence:auto]"' in src, \
        "the provenance marker constant is gone"
    assert 'CE_DEFER_MARK="[closure-evidence:deferred]"' in src, \
        "the deferral marker constant is gone — the decline path cannot stamp"
    assert "_ce_marker_ach" in src, \
        "the recurring branch no longer reads a THIS-vs-PRIOR occurrence signal"
    # NEGATIVE PIN ( outcome 3). The bare substring test is the
    # fragility, not an implementation detail: it matched the token ANYWHERE in
    # the note, so any prose mentioning it — a close note explaining the decline,
    # this very file's docstrings — reclassified a hand-written artifact as
    # script output and made it eligible for destruction. Asserting the anchored
    # matcher EXISTS does not prevent the substring test coming back beside it,
    # so its absence is pinned separately.
    #
    # SCOPED TO EXECUTABLE LINES, and the reason is worth the four extra lines:
    # written as a whole-file substring test, this assertion FAILED on its first
    # run — against the COMMENT in closure-evidence-write.sh that quotes the old
    # expression to explain what was removed. That is the defect under repair,
    # reproduced one level up: a bare substring test over a corpus that contains
    # prose about itself. De-arming the prose would have been the smaller edit
    # and the wrong lesson; the comment is load-bearing documentation and the
    # PIN is what was mis-shaped.
    live = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
    offenders = [ln for ln in live if '"$_existing" != *"$CE_AUTO_MARK"*' in ln]
    assert offenders == [], (
        "the bare-substring discriminator is back in executable code; prose "
        f"mentioning the token re-arms destruction of a hand-written note: {offenders}")
    emitters = [ln for ln in src.splitlines()
                if ln.strip().startswith(("echo", "printf"))
                and "the narrative is NOT on the record" in ln]
    assert emitters == [], f"the false assertion is still emitted: {emitters}"


class TestDeclinePathStampsSoTheGoalCanUnwedge:
    """ — the decline branch preserved the note and dropped THIS
    occurrence's evidence, permanently.

    MEASURED TWICE against world/aspirations.jsonl before the fix: cc-08
    2026-08-26 read 90 recurring / 17 note-absent / 73 note-present / 0 marked /
    63 wedged; cc-07 the same day read 90 / 17 / 73 / 0 / 64 over a 19,263,903-byte
    file with 2,978 goals parsed as a positive control. The counts agree and the
    extra wedged goal is one that closed in between. Nothing could ever clear it:
    the marker is written at the BOTTOM of the script and this branch exited
    before reaching it, so the note stayed unmarked and every later occurrence
    re-entered the same branch. Skewed to the highest-frequency goals —
    achievedCount 349, 342, 277, 187.
    """

    HAND_WRITTEN = "H" * 300
    SUMMARY = "S" * 200

    def test_the_next_occurrence_may_supersede_a_deferred_note(self, tmp_path):
        """Outcome 2 + outcome 4. The unwedge itself: a note preserved and
        stamped at occurrence 6 is superseded at occurrence 7."""
        script = _stage(tmp_path)
        proc, writes = _run(tmp_path, script,
                            ["--goal", GID, "--source", "world",
                             "--summary", self.SUMMARY],
                            existing_note=_deferred(self.HAND_WRITTEN, ach=6),
                            recurring=True, achieved=7)
        assert proc.returncode == 0, proc.stderr
        assert len(writes) == 1, (
            f"the goal is still wedged at the next occurrence: {writes!r} "
            f"{proc.stdout!r} {proc.stderr!r}")
        argv = writes[0]
        written = argv[argv.index("outcome_note") + 1]
        assert "SUPERSEDES" in written, f"not a supersede: {written[:200]!r}"
        assert self.SUMMARY in written, (
            f"this occurrence's evidence still did not reach the record: {written!r}")

    def test_a_same_occurrence_retry_does_not_supersede(self, tmp_path):
        """THE HAZARD THE RECORDED COUNT EXISTS FOR. recurring-close.sh
        documents retrying a failed verify by name, so 'has a deferral marker'
        cannot by itself authorize a supersede — a retry would destroy the
        artifact inside the very occurrence that just preserved it. Stamped at 6,
        re-run at 6: decline, and write NOTHING (re-stamping would append a
        duplicate line on every retry and grow the note without bound)."""
        script = _stage(tmp_path)
        note = _deferred(self.HAND_WRITTEN, ach=6)
        proc, writes = _run(tmp_path, script,
                            ["--goal", GID, "--source", "world",
                             "--summary", self.SUMMARY],
                            existing_note=note, recurring=True, achieved=6)
        assert proc.returncode == 0, proc.stderr
        assert writes == [], (
            f"a same-occurrence retry wrote to the record: {writes!r}")
        assert "idempotent re-run" in proc.stdout, (
            f"guard-2536: the retry decline must announce itself: {proc.stdout!r}")

    def test_the_deferral_write_does_not_claim_authorship(self, tmp_path):
        """CE_AUTO_MARK asserts 'written by closure-evidence-write.sh'. The note
        on the decline path was written by someone else and merely appended to,
        so stamping it would be a false provenance claim — and would grant the
        next occurrence a supersede on evidence of the wrong thing."""
        script = _stage(tmp_path)
        proc, writes = _run(tmp_path, script,
                            ["--goal", GID, "--source", "world",
                             "--summary", self.SUMMARY],
                            existing_note=self.HAND_WRITTEN,
                            recurring=True, achieved=6)
        argv = writes[0]
        written = argv[argv.index("outcome_note") + 1]
        assert CE_AUTO_MARK not in written, (
            f"the decline path claimed authorship of a note it preserved: {written!r}")

    def test_the_deferral_is_trailing_so_the_preview_still_shows_the_note(self, tmp_path):
        """aspirations.py previews outcome_note[:300]. The preserved artifact is
        what a reader needs to see, so the stamp goes at the END — same reason
        the provenance marker is trailing."""
        script = _stage(tmp_path)
        proc, writes = _run(tmp_path, script,
                            ["--goal", GID, "--source", "world",
                             "--summary", self.SUMMARY],
                            existing_note=self.HAND_WRITTEN,
                            recurring=True, achieved=6)
        argv = writes[0]
        written = argv[argv.index("outcome_note") + 1]
        assert CE_DEFER_MARK not in written[:300], (
            f"the stamp ate the 300-char preview window: {written[:300]!r}")

    def test_first_occurrence_is_untouched_by_any_of_this(self, tmp_path):
        """TWO-WAY CONTROL. achievedCount==1 means no prior occurrence exists, so
        an existing note can only be hand-written for THIS one — it must not be
        stamped, deferred, or superseded. Without this the fix could pass every
        test above by simply stamping everything."""
        script = _stage(tmp_path)
        proc, writes = _run(tmp_path, script,
                            ["--goal", GID, "--source", "world",
                             "--summary", self.SUMMARY],
                            existing_note=self.HAND_WRITTEN,
                            recurring=True, achieved=1)
        assert proc.returncode == 0, proc.stderr
        assert writes == [], f"the first occurrence was written to: {writes!r}"


class TestAnchoredMarkerMatch:
    """ outcome 3 — the discriminator was a bare substring test.

    NOT HYPOTHETICAL. The worker that filed this goal quoted the marker token in
    its own close note to EXPLAIN why the supersede had declined. That armed the
    old test against both that evidence and a preserved 2026-06-30 artifact, and
    it was caught only at read-back. Any diagnostic note about this mechanism
    arms the substring form — which is why the fixtures here are prose, and why
    the check is that prose does NOT match.
    """

    SUMMARY = "S" * 200

    def test_prose_mentioning_the_token_does_not_arm_the_discriminator(self, tmp_path):
        """The exact shape that armed it live: a hand-written note whose text
        discusses the marker mid-sentence. It must still DECLINE."""
        note = ("Occurrence note: the supersede declined here because the "
                f"record carries no {CE_AUTO_MARK} anywhere in it, which is "
                "the provenance signal the branch reads.")
        script = _stage(tmp_path)
        proc, writes = _run(tmp_path, script,
                            ["--goal", GID, "--source", "world",
                             "--summary", self.SUMMARY],
                            existing_note=note, recurring=True, achieved=4)
        assert proc.returncode == 0, proc.stderr
        assert len(writes) == 1, f"expected a preserving write: {writes!r}"
        argv = writes[0]
        written = argv[argv.index("outcome_note") + 1]
        assert written.startswith(note), (
            "prose mentioning the token was read as script authorship and the "
            f"note was superseded: {written[:220]!r}")
        assert "SUPERSEDES" not in written, (
            f"the note was destroyed by a prose mention: {written!r}")

    def test_a_line_starting_with_the_token_still_needs_the_written_shape(self, tmp_path):
        """Anchoring alone is not enough. A note whose LINE begins with the
        token but which lacks the ' by closure-evidence-write.sh
        (achievedCount=N)' tail is still not this script's output."""
        note = f"{CE_AUTO_MARK} - I am pasting the marker at the start of a line."
        script = _stage(tmp_path)
        proc, writes = _run(tmp_path, script,
                            ["--goal", GID, "--source", "world",
                             "--summary", self.SUMMARY],
                            existing_note=note, recurring=True, achieved=4)
        argv = writes[0]
        written = argv[argv.index("outcome_note") + 1]
        assert "SUPERSEDES" not in written, (
            f"a bare token at line start was accepted as authorship: {written!r}")

    def test_a_genuinely_marked_note_still_matches(self, tmp_path):
        """TWO-WAY CONTROL (guard-1220). The tests above would all pass against
        a matcher that never matches anything, which would re-open g-115-6527 by
        making every recurring note permanently un-supersedable."""
        script = _stage(tmp_path)
        proc, writes = _run(tmp_path, script,
                            ["--goal", GID, "--source", "world",
                             "--summary", self.SUMMARY],
                            existing_note=_marked("prior occurrence body", ach=3),
                            recurring=True, achieved=4)
        argv = writes[0]
        written = argv[argv.index("outcome_note") + 1]
        assert "SUPERSEDES" in written, (
            f"a genuinely marked note stopped being supersedable: {written!r}")
