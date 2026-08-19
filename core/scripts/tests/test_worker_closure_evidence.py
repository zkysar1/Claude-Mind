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

    NOTE = "prior occurrence narrative, 6 hours old"

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
                        existing_note="p", recurring=True, achieved=1)
        _, at = _run(tmp_path / "at", s2,
                     ["--goal", GID, "--source", "world", "--summary", "x"],
                     existing_note="p", recurring=True, achieved=2)
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
        already = ("[closure-evidence] SUPERSEDES a prior-occurrence note of 41 chars "
                   "— recurring occurrence achievedCount=4.\n\nthe summary this run "
                   "already wrote")
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
                            existing_note="[closure-evidence] SUPERSEDES ...\n\nlast run's text",
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

    RICH = "worker Phase 3.9 narrative, written seconds ago in THIS occurrence\n\nparagraph two"

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
