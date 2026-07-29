---
name: analyze-npc-behavior
forged: true
forged_by: bravo
forged_date: "2026-04-10"
description: "Runs autonomous behavioral analysis on recorded NPC game sessions via /state-replay: downloads session data from EFS, executes analysis modules, scores NPCs on the 13-criterion Overall Humanness Score (OHS) rubric, compares against baselines, and generates improvement goals. Use whenever the user says \"analyze NPC behavior\", \"check if NPCs feel human\", \"evaluate game session quality\", \"run behavioral analysis\", or the agent needs data-driven signal on NPC quality regressions or improvements."
minimum_mode: autonomous
user-invocable: true
conventions:
  - goal-schemas
  - infrastructure
triggers:
  - "analyze NPC behavior"
  - "check if NPCs feel human"
  - "evaluate game session quality"
  - "behavioral analysis"
  - "humanness evaluation"
  - "NPC quality check"
  - "replay session and evaluate"
  - "run behavioral analysis"
revision_id: "skill-bootstrap-analyze-npc-behavior-52367e"
previous_revision_id: null
---

# /analyze-npc-behavior — NPC Behavioral Analysis Workflow

Wraps state-replay into an autonomous behavioral analysis workflow. Downloads a game session,
runs analysis modules, scores NPCs on the 12-criterion humanness rubric, compares against
behavioral baselines, and generates improvement goals for deficits.

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter.

## Inputs

- `session_key` (optional): Specific game session to analyze. If omitted, uses most recent.
- `--update-baselines`: Flag to update baseline data after analysis.

## Step 1: Session Acquisition

```
# Box-portable resolution (g-318-40) — same probe order as
# world/scripts/ohs-unscored-sessions.sh (the detector mirrors this step).
# Env overrides win; else first-existing candidate root (Windows alpha-box
# path FIRST — unchanged there; then the Linux /opt/GitHub layout). PYEXE:
# the Windows CPython if it exists, else python3 (invoke as `py -3` from a
# raw Bash tool call per python-invocation.md). Every $PYEXE below means
# this resolved interpreter — do NOT paste a literal exe path.
REPLAY_DIR = $OHS_REPLAY_DIR if set, else first existing dir of:
    ["C:/ZakNoCloud/GitHub/Ayoai/Ayoai-State-Replay",
     "/opt/GitHub/Ayoai/Ayoai-State-Replay"]
PYEXE = $OHS_REPLAY_PYTHON if set;
        else "C:/Users/Zachary/AppData/Local/Programs/Python/Python312/python.exe" if it exists;
        else "$REPLAY_DIR/.venv/bin/python" if executable   # PEP-668 Linux boxes (g-326-11):
             # system python3 lacks python-dotenv -> cli.py fails; the repo-local venv
             # (python3 -m venv --system-site-packages .venv && .venv/bin/pip install -r
             # requirements.txt) carries the deps. Mirrors ohs-unscored-sessions.sh.
        else python3 (`py -3` from raw Bash)

IF args contains a session_key:
    session_key = args.session_key
ELSE:
    # Find most recent session (CLI requires --env; returns a table with
    # newest sessions at the top of the #-ordered list).
    Bash: cd $REPLAY_DIR && $PYEXE cli.py list-sessions --env NPCDemoExperiment
    session_key = parse the first data row (# 1) from the table output

# Check if session is already downloaded
session_dir = find local session directory for session_key (e.g. {REPLAY_DIR}/sessions/{session_key})
IF session_dir not found:
    # --dest MUST be absolute: cli.py's default is a CWD-relative ./sessions,
    # so a download run without/before the cd silently drops a multi-MB
    # sessions/ tree at the wrong root (2026-05-15 repo-root cruft incident).
    Bash: cd $REPLAY_DIR && $PYEXE cli.py download --env NPCDemoExperiment --session {session_key} --dest "{REPLAY_DIR}/sessions"
    session_dir = "{REPLAY_DIR}/sessions/{session_key}"
```

## Step 1.5: Server-Error Gate (do not judge behavior from a crashed server)

USER DIRECTIVE (g-115-1191, 2026-05-23): NPC behavior MUST NOT be scored when the
Ayoai-Environment-Server threw an error DURING the session window. A startup crash is
caught indirectly by the Step 2.45 envelope gate (0 data), but a MID-session Critical /
Driver crash (e.g. the getRootUnit NPE, commit 8951449 / rb-1236 / guard-627) produces
data and passes the envelope gate — yet the behavior recorded after the crash is invalid.
This gate runs BEFORE any analysis module so a crashed-server session costs zero analysis
compute. Foxtrot owns the hard-skip-vs-flag design call (decided by charlie pre-merge; Rationale below).

```
# ── Signal 1 (PRIMARY, alert-timing-independent): in-artifact termination record ──
# rb-1291: Signal 2 below derives the window from the session-key epoch prefix, but that
# epoch can PRECEDE the actual AWS server-run by hours (session 1779597988260_131: key
# epoch 04:46 UTC vs real run 09:47 UTC). When the crash alert falls outside the wrongly
# derived window the alert filter returns empty and the gate false-CLEARs a crashed
# session. The session's OWN TerminationNotes.json is the authoritative crash record and
# is independent of alert timing — check it FIRST.
Bash: cat "{session_dir}/TerminationNotes.json" 2>/dev/null
IF the file exists AND parses as JSON:
    term       = parse JSON
    err_source = term.get("errorSource")   # "AYOAI_INTERNAL_ERROR" (crash) | "NORMAL_SHUTDOWN" (clean) | null
    # Only a clean shutdown is acceptable. Treat any non-null errorSource other than a
    # NORMAL_SHUTDOWN marker as a crash (defensive against unknown error codes).
    IF err_source is not null AND err_source.upper() not in ("NORMAL_SHUTDOWN", "NORMAL", ""):
        # ── g-318-24 SALVAGE-SCORE: a crashed session ran REAL gameplay before it
        # degraded. Rather than discard it wholesale (the old binary SKIP), score the
        # HEALTHY PREFIX up to the degradation-onset boundary — the degraded tail scores
        # artificially low and would contaminate the axis, so it is EXCLUDED via the
        # cli.py analyze --until cutoff (Step 2 SCORE_WINDOW_FLAG). The prefix is
        # salvageable only if long enough to carry signal (>=60s, the user-directive
        # g-115-1191 intent preserved: never score DEGRADED/post-crash behavior).
        prefix = detect_healthy_prefix(session_dir, term)
        # detect_healthy_prefix DISPATCHES by crash class (g-318-25) — the degradation
        # boundary signal differs by HOW the server died:
        #
        # (a) StreamingUpdates-stuck — term.get("source") == "StreamingUpdatesAPI", OR
        #     term.terminationNotes matches "Step stuck"/"Updates=false". The DOMINANT crash
        #     class (charlie g-318-21: 2 of 3 newest crashes). The main loop stalls
        #     (completedAllUpdates false for 4 consecutive steps) but BitNet latency stays
        #     HEALTHY — so detector (b) finds NO onset and would wrongly SKIP a long healthy
        #     prefix (the g-318-24 production gap). Boundary = the LAST healthy update: each
        #     {session_dir}/SavedAyoStreamUpdates/*.jsonl record carries stepCount + an
        #     epoch-ms timestamp (monotonic with stepCount; records may span multiple
        #     *.jsonl files — read ALL). The stream advances cleanly until the stall then
        #     STOPS (Updates=false ⇒ nothing streamed). onset_ts = max(timestamp) over ALL
        #     records; first_ts = min(timestamp). VERIFIED on real session 1778881939405_165
        #     (g-318-24 SKIPPED it — no _llmt onset): boundary = the step-785 update; the
        #     ConflatedState timeline ends ~0.4s later, so the prefix IS essentially the full
        #     324s healthy run that was being discarded entirely (full==prefix==788 unique
        #     positions; 0 frames past the boundary here). The --until cutoff is the SAFETY
        #     BELT for crashes that DO leak frozen frames after the last update — a no-op when
        #     frame production stops at the stall (this session). onset_method="stream_stall".
        #
        # (b) BitNet-degradation (DEFAULT, g-318-24): scans the per-decision BitNet latency
        #     log (IntentEngineVerticle.seed-latency `_llmt`, g-268-09; {session_dir}/logs/*.jsonl).
        #     Onset = the FIRST `_llmt` sample that, with its successor, is SUSTAINED > 7000ms
        #     (a single >7s spike is NORMAL — rb-700: 14.3% of healthy calls exceed 7s — so
        #     onset requires TWO consecutive). prefix span measured from the log's OWN first
        #     decision ts, NOT the session-key epoch (rb-1291). onset_method="latency_onset".
        #
        # (c) USER_ENVIRONMENT_ERROR full-run salvage (g-318-26): err_source.upper() ==
        #     "USER_ENVIRONMENT_ERROR" — an EXTERNAL user-script kill (e.g. a Roblox
        #     Ice-Zombie Damage-Script bug), NOT an Ayoai cognition/stream failure. Such a
        #     session can be cognition-HEALTHY end-to-end (rb-2035, verified on g-250-136:
        #     110 _llmt samples, 0 sustained >7s onsets). For it, detector (b) finds NO latency
        #     onset and detector (a) finds no stream stall — so the prior code fail-safe-SKIPPED
        #     a FULLY scoreable session (the g-318-26 production gap: SKIPPED_SERVER_ERRORED on
        #     a healthy run). FIX: run detector (b)'s `_llmt` sustained-onset scan as the HEALTH
        #     GATE. If it finds 0 sustained >7s onsets (cognition healthy throughout), there is
        #     NO degraded tail to exclude — score the WHOLE run: onset_ts = termination_ts =
        #     term["terminationTime"] (the authoritative server-shutdown epoch-ms in
        #     TerminationNotes.json — VERIFIED present on real USER_ENVIRONMENT_ERROR sessions,
        #     e.g. 1777306125573_748: terminationTime=1777324212932). Fall back to max(timestamp)
        #     over ALL SavedAyoStreamUpdates records (detector (a)'s source), then max(`_llmt` ts),
        #     ONLY if terminationTime is absent. first_ts = the `_llmt` log's OWN first decision ts
        #     (rb-1291). prefix_sec is then the full healthy span. onset_method="user_env_full_run".
        #     GUARD-657 / g-115-1191
        #     PRESERVED: the 0-sustained-onset check IS the gate — if the `_llmt` scan DOES find a
        #     sustained onset, cognition degraded before the external kill, so this is NOT
        #     full-run salvage: fall through to (b)'s prefix-cutoff (onset_ts = first sustained
        #     onset, degraded tail excluded). DEGRADED cognition is still never scored.
        #     (Manually applied this way on g-250-136: C11 1.0 -> 2.67; rb-2035 / rb-2038.)
        #
        # Returns {onset_ts (epoch-ms), first_ts, prefix_sec = (onset_ts - first_ts)/1000,
        # healthy: prefix_sec >= 60, onset_method}. FAIL-SAFE: if the relevant log/stream is
        # absent/unreadable, or NO boundary is found (abrupt death — no clean boundary),
        # prefix.healthy = False → fall through to the SKIP below (current behavior preserved).
        # EXCEPTION (c, g-318-26): for err_source == USER_ENVIRONMENT_ERROR, "NO `_llmt` onset
        # found" is NOT a fail-safe skip — it is the HEALTHY-throughout signal, so onset_ts =
        # termination_ts and prefix.healthy = (full-run prefix_sec >= 60). The fail-safe skip
        # still applies to (c) ONLY when the `_llmt` log itself is absent/unreadable (cannot
        # verify health → cannot salvage).
        IF prefix.healthy:
            session_gate = "CLEAR_PREFIX"
            prefix_window = {"start_ts": prefix.first_ts, "onset_ts": prefix.onset_ts, "prefix_sec": prefix.prefix_sec, "onset_method": prefix.onset_method}
            Log: "SESSION GATE: {session_id} — CLEAR_PREFIX (crash errorSource={err_source} via {prefix.onset_method}; {prefix.prefix_sec:.0f}s scored window ending @ {prefix.onset_ts}; " + ("FULL healthy run — cognition healthy end-to-end, no degraded tail to exclude" if prefix.onset_method == "user_env_full_run" else "healthy prefix before degradation boundary, degraded tail excluded") + ") (g-318-24/g-318-25/g-318-26)"
            # DO NOT skip. Fall through to Step 2 with the --until cutoff; the trend row is
            # appended in Step 7 with crash_prefix=true + scored_window. For a prefix-CUT window
            # (onset_method latency_onset / stream_stall) the window is TRUNCATED → C11/C12/C13
            # interpret it as REDUCED EVIDENCE, do not penalize (Step 2). For user_env_full_run
            # (g-318-26) the window IS the whole healthy run — NOT truncated — so it carries FULL
            # evidence: C11/C12/C13 must NOT down-weight it as reduced (it is a complete session).
        ELSE:
            session_gate = "SKIPPED_SERVER_ERRORED"
            Log: "SESSION GATE: {session_id} — SKIPPED_SERVER_ERRORED (TerminationNotes.json errorSource={err_source}, source={term.get('source')}: '{(term.get('terminationNotes') or '')[:80]}'; no salvageable >=60s healthy prefix) — alert-timing-independent (rb-1291)"
            output = {
                "session_id": session_id,
                "gate": "SKIPPED_SERVER_ERRORED",
                "server_error": {"signal": "TerminationNotes.json", "errorSource": err_source,
                                 "source": term.get("source"), "notes": term.get("terminationNotes")},
                "reason": "session TerminationNotes.json records a non-NORMAL_SHUTDOWN termination (server crash) with no salvageable >=60s healthy prefix — behavior invalid for OHS scoring (rb-1291, g-318-24)"
            }
            Write per-NPC report as empty-with-reason; do NOT append to ohs-trend.jsonl; SKIP directly to Step 8 (summary).
    ELIF err_source is null AND (term.get("terminatedBy") or "").upper().startswith("EC2MONITOR"):
        # ── (d) EC2Monitor kill schema (g-318-48, rb-3864; observed session 1784292952441_359,
        # 2026-07-17). EC2Monitor kills write a DIFFERENT TerminationNotes schema:
        # {terminatedBy: "EC2Monitor", reason, monitorInstanceId, instanceId, timestamp} with
        # NO errorSource field — so the errorSource branch above never fires and the record
        # previously fell through to Signal 2 as if it were a clean finish. It is NOT clean:
        # the monitor SIGTERM'd the server externally. External-kill class — same salvage
        # doctrine as (c): cognition may be healthy end-to-end, so verify health, then score
        # the WHOLE run. CAUTION: the kill reason ("UNAUTHORIZED - No server data after Ns")
        # is the MONITOR's data-visibility claim, not a cognition verdict — _359 had 927
        # stream updates + an active client mid-conversation when killed (the g-335-87
        # ALB-8787 stream-refusal made the monitor blind). Judge health from the session's
        # OWN artifacts, never from the monitor's reason string.
        # HEALTH GATE (in order):
        #   1. IF `_llmt` samples exist: detector (b)'s sustained-onset scan — 0 sustained
        #      >7s onsets = healthy (the (c) recipe verbatim).
        #   2. IF `_llmt` is EMPTY (0 samples — BitNet not in the decision path this build):
        #      require 2 INDEPENDENT alternative signals BOTH healthy (verify-before-assuming):
        #        (i)  CrashDiagnostics.json: shutdown-hook fired WITHOUT criticalError, heap
        #             headroom sane (heapUsedMB well under heapMaxMB);
        #        (ii) SavedAyoStreamUpdates continuity: stepCount monotonic, max inter-update
        #             gap ~1-2s, updates flowing to within seconds of the kill timestamp.
        #      Empty `_llmt` also means the BitNet-degradation class is vacuously absent.
        #   3. IF health cannot be verified (no `_llmt` AND either alternative signal
        #      absent/unhealthy): fail-safe SKIP (SKIPPED_SERVER_ERRORED) — degraded or
        #      unverifiable cognition is never scored (g-115-1191 intent preserved).
        # On healthy: onset_ts = max(timestamp) over ALL SavedAyoStreamUpdates records (the
        # kill postdates the last update; term has no terminationTime in this schema — use
        # term.timestamp parsed to epoch-ms as fallback); first_ts = min(timestamp) over the
        # same records; prefix_sec = full healthy span; prefix.healthy = (prefix_sec >= 60);
        # onset_method = "monitor_kill_full_run". FULL evidence window (NOT truncated) —
        # C11/C12/C13 must not down-weight it, same as user_env_full_run.
        prefix = as computed above (health gate → full-run window)
        IF prefix.healthy: session_gate = "CLEAR_PREFIX" — same CLEAR_PREFIX flow as the
            errorSource crash branch (fall through to Step 2; Step 7 row carries
            crash_prefix=true + scored_window with onset_method monitor_kill_full_run).
        ELSE: session_gate = "SKIPPED_SERVER_ERRORED" — same SKIP flow as above, with
            server_error.signal = "TerminationNotes.json (EC2Monitor kill)" and
            terminatedBy/reason in place of errorSource.
    # ELSE errorSource is NORMAL_SHUTDOWN — clean finish; fall through to the alert
    # cross-check (Signal 2) as a secondary signal.
# ELSE TerminationNotes.json absent or unparseable — a crash hard enough to prevent
# writing the record may still have produced an alert; Signal 2 catches that.

# ── Signal 2 (SECONDARY, alert-based): cross-check failure alerts ──
# Derive the session window. session_key prefix is an epoch-ms start; duration from the
# same session-envelope source Step 2.45 uses.
session_start_ms = int(session_key.split("_")[0])          # e.g. 1778285495914
session_start    = epoch_ms_to_datetime(session_start_ms)
duration_sec     = session metadata duration (session envelope / NORMAL_SHUTDOWN log)
session_end      = session_start + duration_sec + 60s       # +60s grace for crash-then-shutdown

# Pull recent failure alerts. Size --max to cover the window (≈40 covers a multi-hour span).
Bash: source core/scripts/_paths.sh && bash "$WORLD_DIR/scripts/email-read.sh" check-alerts --json --max 40
IF the call fails (rc != 0) OR output is not parseable JSON:
    # FAIL-OPEN: a transient email/S3 outage must NOT halt all OHS scoring. Log and proceed.
    Log: "SERVER-ERROR GATE: alerts unreadable (email-read.sh check-alerts failed) — proceeding to score, gate could not run"
    server_error_gate = "UNCHECKED_ALERTS_UNREADABLE"
    SKIP the rest of this gate; continue to Step 2.
alerts = parse JSON array

# Subject format (live check-alerts probe 2026-05-23):
#   "Ayoai ❌: Ayoai-Environment-Server (<instance-id>)"
# where <instance-id> is a UUID (real instance) OR a labeled run
# ("verify-getrootunit-fix-zeta-...", "AlertTest" = test/diagnostic, not a real crash).
def instance_of(subject):  # text inside the trailing parentheses, or "" if none
    ...
TEST_MARKERS = ["alerttest"]  # also: parenthetical starting "verify-" / "diag-" = test run

# ── Signal 2a (alert-timing-independent): direct session-key match in alert subject ──
# rb-1291 fix (b): the alert subject parenthetical can be EITHER an opaque EC2 instance-id
# OR the SESSION-KEY itself (observed: the session-131 crash alert subject was
# "Ayoai-Environment-Server (1779597988260_131)" — the session-key). A session-key match
# is a DIRECT identity match, so it must NOT be gated by the (rb-1291-unreliable) time
# window — match it against the FULL alert list, not the window-filtered candidates below.
key_matched = [a for a in alerts if
    a.is_failure
    AND "Ayoai-Environment-Server" in a.subject
    AND NOT a.is_analytics_test
    AND a.subject.lower() does not contain any TEST_MARKERS
    AND not instance_of(a.subject).startswith(("verify-", "diag-"))
    AND instance_of(a.subject) == session_key]
IF key_matched is non-empty:
    session_gate = "SKIPPED_SERVER_ERRORED"
    Log: "SESSION GATE: {session_id} — SKIPPED_SERVER_ERRORED (alert subject session-key match, window-independent: '{key_matched[0].subject}' @ {key_matched[0].date}) (rb-1291)"
    output = {
        "session_id": session_id,
        "gate": "SKIPPED_SERVER_ERRORED",
        "server_error": {"signal": "alert-session-key-match", "alert_subject": key_matched[0].subject,
                         "alert_date": key_matched[0].date, "session_key": session_key},
        "reason": "a failure alert for this session-key was found regardless of time window — behavior invalid for OHS scoring (rb-1291)"
    }
    Write per-NPC report as empty-with-reason; do NOT append to ohs-trend.jsonl; SKIP directly to Step 8 (summary).

# ── Signal 2b (window-based instance-id match) ──
# Candidate = real Ayoai-Environment-Server failures inside the window (test noise excluded).
candidates = [a for a in alerts if
    a.is_failure
    AND "Ayoai-Environment-Server" in a.subject
    AND NOT a.is_analytics_test
    AND a.subject.lower() does not contain any TEST_MARKERS
    AND not instance_of(a.subject).startswith(("verify-", "diag-"))
    AND session_start <= parse_rfc2822(a.date) <= session_end]

IF candidates is empty:
    server_error_gate = "CLEAR"      # no in-window server crash — score normally (Step 2)
ELSE:
    # Tier the match against the RISK note ("require env+instance match, not just time overlap").
    # Try to confirm the crashed instance is THIS session's instance.
    session_instance = session metadata server instance / run id, if recorded
                       (look in the session envelope / manifest for the env-server run id)
    matched = [a for a in candidates
               if session_instance AND instance_of(a.subject) == session_instance]

    IF matched:
        # CONFIRMED env+instance match → HARD-SKIP (user directive: never judge crashed-server behavior).
        session_gate = "SKIPPED_SERVER_ERRORED"
        Log: "SESSION GATE: {session_id} — SKIPPED_SERVER_ERRORED (instance {session_instance} crashed in-window: '{matched[0].subject}' @ {matched[0].date})"
        output = {
            "session_id": session_id,
            "gate": "SKIPPED_SERVER_ERRORED",
            "server_error": {"alert_subject": matched[0].subject, "alert_date": matched[0].date,
                             "instance": session_instance, "window": [session_start, session_end]},
            "reason": "Ayoai-Environment-Server crashed on this session's instance during the session window — behavior invalid for OHS scoring"
        }
        Write per-NPC report as empty-with-reason; do NOT append to ohs-trend.jsonl; SKIP directly to Step 8 (summary).
    ELSE:
        # In-window server failure but instance NOT confirmed (session_instance unknown, or no
        # equality). Per the RISK note, DO NOT hard-skip on time-overlap alone (false-positives
        # on unrelated-instance crashes). FLAG: score normally but annotate unreliability.
        server_error_gate = "FLAGGED_POSSIBLE_SERVER_ERROR"
        Log: "SESSION GATE: {session_id} — FLAGGED_POSSIBLE_SERVER_ERROR ({count(candidates)} Ayoai-Environment-Server failure(s) in-window, instance unconfirmed: '{candidates[0].subject}' @ {candidates[0].date})"
        Carry server_error_gate + candidate alert subjects into the Step 8 summary as a
        prominent per-score caveat. Continue to Step 2 (score, flagged unreliable).
```

Rationale (charlie's design call — hard-skip vs flag): the user directive is unambiguous
that crashed-server behavior must not be judged, so a CONFIRMED instance match HARD-SKIPS
(no OHS score, no ohs-trend.jsonl pollution) exactly like the Step 2.45 envelope gate. But
the RISK note warns that matching on time-overlap alone false-positives on unrelated-instance
or test crashes that merely fall in the window — so when the crashed instance cannot be
confirmed as THIS session's instance, the gate DOWNGRADES to a FLAG (score proceeds, marked
unreliable) rather than silently discarding a possibly-valid session. Test/diagnostic
instances (AlertTest, "verify-*"/"diag-*" parentheticals, is_analytics_test) are excluded
outright. The gate FAILS OPEN on an alert-infra outage: a transient email failure must not
halt all OHS scoring. Subject-format basis: live check-alerts probe 2026-05-23 showed
"Ayoai <X>: Ayoai-Environment-Server (<instance-id>)" — server name and instance-id both in
the subject, so genuine instance matching (not just time overlap) is possible from the JSON.
This gate is the analyze-npc-behavior counterpart to run-game-session Step 5.5 (which reads
alert@ayoai.com but does not block); together they close the "no data flow from server
FAILED status into behavior scoring" gap zeta confirmed.

rb-1291 hardening (2026-05-24): the original gate hinged ENTIRELY on the alert time-window
filter, whose `session_start` is derived from the session-key epoch prefix. Session
1779597988260_131 exposed the flaw — its key epoch (04:46 UTC) preceded the actual AWS
server-run (09:47-09:52 UTC, per JournalLogs_OnTermination instanceId
i-00c511b822b68345b), so the BitNet-death crash alert (~09:5x UTC) fell OUTSIDE the
computed window, the candidate filter returned empty, and the gate would have false-CLEARED
a crashed session (caught manually via 3 signals, hard-skipped). The fix layers two
WINDOW-INDEPENDENT signals AHEAD of the window-based check: Signal 1 reads the session's
own `TerminationNotes.json` (the server's authoritative termination record — any non-
NORMAL_SHUTDOWN errorSource such as AYOAI_INTERNAL_ERROR = crash, no alert needed); Signal
2a matches the session-key in the alert subject parenthetical against the FULL alert list
(the subject can be a session-key, not just an EC2 instance-id, and a key match is a direct
identity match that must not be gated by the unreliable window). The original window-based
instance-id match is preserved as Signal 2b (a fallback for crashes where neither the
artifact nor a key-tagged alert exists). guard-625 (suppression-filter safety) does not
apply: these are inclusion/detection signals that route a session to HARD-SKIP, not
suppression filters that drop production alerts, and TerminationNotes.errorSource is a
source-of-truth termination code, not a downstream symptom. A future option (c) — deriving
the window from JournalLogs start time rather than the key epoch — would harden Signal 2b
itself but is not needed once Signals 1 + 2a cover the epoch-precedes-run class. Distinct
from alpha's g-115-1217 (server-side BitNet-death fix); this hardens the SCORING gate to
skip crashed sessions (board coordination msg-20260524-101735-charlie-7382).

## Step 2: Run Analysis Modules

```
# Summary (cell success rates, NPC counts)
Bash: cd $REPLAY_DIR && $PYEXE cli.py summary {session_dir}
summary = parse output

# CLI signature: cli.py analyze -a <module> <session_path> [-o text|json] [--until <epoch_ms>] [--since <epoch_ms>]
# `-a/--analysis` is REQUIRED and must come BEFORE the session_path positional.
# Available modules (see `cli.py analyze --help`): stuck_detector, movement,
# chat_repetition. Any other module name in -a fails with a choices error, so
# the graceful-skip loop below catches "not available" via exit code != 0.

# ── g-318-24 SALVAGE-SCORE WINDOW ──────────────────────────────────────────────
# If Step 1.5 set session_gate == "CLEAR_PREFIX", EVERY `cli.py analyze` invocation
# in this step MUST carry the prefix cutoff so only the healthy pre-degradation
# prefix is scored and the degraded tail is excluded:
SCORE_WINDOW_FLAG = "--until {prefix_window.onset_ts}" if session_gate == "CLEAR_PREFIX" else ""
# Append {SCORE_WINDOW_FLAG} to each `cli.py analyze ...` command below (the
# windowed timeline is empty-safe: a too-short prefix exits non-zero and the
# graceful-skip loop handles it). The LLM-eval fallback modules (C11 continuity,
# C12 grounding, C13 reaction) likewise score ONLY ticks with timestamp <=
# prefix_window.onset_ts. CRITICAL: when session_gate == "CLEAR_PREFIX", interpret
# the truncated window as REDUCED EVIDENCE — do NOT penalize C11/C12/C13 for having
# less continuity/grounding/reaction evidence than a full session (a 60s prefix
# legitimately holds fewer cross-utterance signals). Mark such scores low-sample.

# Stuck detection
Bash: cd $REPLAY_DIR && $PYEXE cli.py analyze -a stuck_detector {session_dir} -o json
stuck_results = parse JSON output

# Movement analysis
Bash: cd $REPLAY_DIR && $PYEXE cli.py analyze -a movement {session_dir} -o json
movement_results = parse JSON output

# Chat repetition (C8 sub-signal — g-226-52, session-51 iter-48)
# Detects NPCs saying the same phrase repeatedly. Returns
# chat_repetition_score in [0,1] or null (insufficient_data=true) for
# NPCs with < 3 chat intents. Treat null as "skip" for C8 automation,
# not as a zero score.
Bash: cd $REPLAY_DIR && $PYEXE cli.py analyze -a chat_repetition {session_dir} -o json
chat_repetition_results = parse JSON output

# Steering quality (C9 composite — g-250-15, g-250-16). Blends obstacle
# avoidance, npc_separation_consistency, and approach_directness with
# weights 0.40 / 0.30 / 0.30 per the g-250-13 spec. Output is a 0-1
# steering_quality_score per NPC; axis declaration = "C9". Analyzer emits
# per_unit entries that flow through to Step 3.5 aggregation.
Bash: cd $REPLAY_DIR && $PYEXE cli.py analyze -a steering_quality {session_dir} -o json
steering_quality_results = parse JSON output

# Path variety (C9 sub-signal parallel to steering_quality — g-250-29).
# PathVarietyAnalyzer emits a per-NPC path_variety_score composite of
# pathfinding_correctness (0.30, g-250-24), directional_entropy (0.25),
# turn_rate (0.20), area_coverage (0.15), (1 - revisit_ratio) (0.10).
# Analyzer self-declares axis="C9". Step 3 combines this with
# steering_quality's composite — both are legitimate C9 signals: steering
# measures "did you move smoothly toward goals", path_variety measures
# "did you cover the space with intentional navigation".
Bash: cd $REPLAY_DIR && $PYEXE cli.py analyze -a path_variety {session_dir} -o json
path_variety_results = parse JSON output

# Intent appropriateness (C3 action-selection composite — g-250-14, g-250-22,
# g-250-40). Four per-NPC sub-metrics: utility_scored_fraction (was
# context_match_rate before g-250-40), utility_match_rate (new),
# iaus_score_calibration, intent_churn_penalty (all [0, 1], higher = better).
# C3 composite per g-250-40 spec:
#   0.20*utility_scored_fraction + 0.40*utility_match_rate
#   + 0.20*iaus_score_calibration + 0.20*intent_churn_penalty.
# Analyzer self-declares axis="C3". Step 3 uses the composite formula to
# populate C3; raw field intent_churn_penalty is retained (higher=better
# despite "penalty" in the name — see note in C3 automation block below).
Bash: cd $REPLAY_DIR && $PYEXE cli.py analyze -a intent_appropriateness {session_dir} -o json
intent_appropriateness_results = parse JSON output

# Emotional expression (C6 composite — g-250-135, closes g-250-134).
# EmotionExpressionAnalyzer reads dominantEmotion/emotionalIntensity transitions
# from the reconstructed ConflatedState (Frame.npc_emotion) and emits a per-NPC
# c6_score in [0,1] = 0.40*state_change_diversity + 0.60*state_appropriateness,
# plus an insufficient_data flag (None score) for NPCs that carried no emotion
# state. Self-declares axis="C6". This closes the extraction-blind floor that
# pinned EVERY NPC at c6=0.3 ("Robotic") — the emotion data was in ConflatedState
# all along; no analyzer surfaced it (g-250-134 verdict: DATA ARTIFACT).
Bash: cd $REPLAY_DIR && $PYEXE cli.py analyze -a emotion_expression {session_dir} -o json
emotion_expression_results = parse JSON output
# Key the per_unit list by unit_key so the C6 automation block below can do
# emotion_expression_per_unit.get(ayo_key) -> {"score", "insufficient_data"}.
# (per_unit entries are {unit_key, axis:"C6", score (None on insufficient_data),
# insufficient_data}; score is excluded from the C6 mean when insufficient_data.)
emotion_expression_per_unit = {
    e["unit_key"]: {"score": e["score"], "insufficient_data": e["insufficient_data"]}
    for e in emotion_expression_results.get("per_unit", [])
}

# C7 decision_variety analyzer (g-250-144: mirrors the C6 emotion_expression
# fix). Computes Shannon entropy of each NPC's intent-identity sequence (same
# _intent_summary identity the C3 analyzer uses) and emits a per_unit C7 score
# in [0,1] = entropy_bits / log2(8) (a "rich repertoire" of 8 distinct intents
# normalises to 1.0; single-intent NPC -> 0 "perfectly predictable"), plus an
# insufficient_data flag (None score) for NPCs with < 5 intents. Closes the
# rb-1880 class-3 extraction-blind floor: C7 had NEITHER a module NOR a
# canonical block and fell through LLM-eval, which floored C7 var~0 in 7/13
# sessions. The intent streams are rich (positive control: intent_appropriateness
# /C3 scores per-NPC variance on the SAME streams). Honor insufficient_data.
# Run: cli.py analyze <session> -a decision_variety -o json
Bash: cd $REPLAY_DIR && $PYEXE cli.py analyze -a decision_variety {session_dir} -o json
decision_variety_results = parse JSON output
decision_variety_per_unit = {
    e["unit_key"]: {"score": e["score"], "insufficient_data": e["insufficient_data"]}
    for e in decision_variety_results.get("per_unit", [])
}

# C5 social_proximity analyzer (g-250-146: mirrors the C6/C7 fix). Computes, per
# NPC from per-frame positions, proximity_engagement_rate (close-pair frames
# within 16 studs / proximity-eligible frames) + approach_initiation_rate (self
# displacement projected onto the self->other direction, so "moves TOWARD" is
# attributable to THIS NPC) and emits a per_unit C5 score in [0,1] =
# 0.50*engagement + 0.50*approach, plus an insufficient_data flag (None score)
# for NPCs with < 10 co-present frames (single-NPC sessions, command_only NPCs
# that never co-locate). Closes the rb-1880 class-3 extraction-blind floor: the
# social_proximity module existed but was UNREGISTERED + UNINVOKED, so C5 fell
# through the LLM-eval pseudocode path and sat at neutral 3.0 / var 0.0 in
# multi-NPC sessions even though positions made proximity computable (positive
# control: C1 movement scores per-NPC var ~0.14 on the SAME positions; validated
# 2026-06-18: C5 now var ~0.06-0.09, range [0.0, 0.75] across 6-NPC sessions
# ...194 / ...399). Honor insufficient_data.
# Run: cli.py analyze <session> -a social_proximity -o json
Bash: cd $REPLAY_DIR && $PYEXE cli.py analyze -a social_proximity {session_dir} -o json
social_proximity_results = parse JSON output
social_proximity_per_unit = {
    e["unit_key"]: {"score": e["score"], "insufficient_data": e["insufficient_data"]}
    for e in social_proximity_results.get("per_unit", [])
}

# C11 memory_continuity analyzer (g-326-26: mirrors the C5/C6/C7 fix shape).
# MemoryContinuityAnalyzer landed 2026-06-22 (98fdbc4, g-316-08) and was
# registered in cli.py, but this SKILL.md never invoked it under ANY name — so
# C11 fell through to the LLM-eval path and the LLM applied the rubric row's
# documented "Baseline-floor 1.0 for fleet" default to every NPC. That is the
# rb-1992 defined-but-uncalled orphan class (dual of rb-1976's phantom). It cost
# four iterations of misdiagnosis (g-326-22..25), because ohs-trend then mixed
# TWO producers in one column: this module (INTEGER 1-5 by literal return) and
# the LLM (which wrote the 15 non-integer values, e.g. 1.2/1.5/2.4 — values the
# module cannot emit). Wiring it makes the producer deterministic.
# EXPECT NO STEP-CHANGE (this is the honest divergence from rb-1986): the usual
# "axis newly wired off a false floor jumps UP" pattern does NOT apply here,
# because both of the module's routes to utilization>0 are independently dead
# (g-326-24) — the inference path needs a PrivateNote timestamped before
# timeline.start_timestamp (= frames[0].timestamp), which in-session notes never
# satisfy, and the explicit references_prior_event_id pointer is absent from the
# session tree. So C11 stays at 1.0 after wiring; what changes is WHO produced
# the 1.0, not the value. Do NOT read post-wiring 1.0s as "the fix failed" —
# read them as the floor being real rather than documented.
# Run: cli.py analyze <session> -a memory_continuity -o json
Bash: cd $REPLAY_DIR && $PYEXE cli.py analyze -a memory_continuity {session_dir} -o json
memory_continuity_results = parse JSON output
memory_continuity_per_unit = {
    e["unit_key"]: {"score": e["score"], "insufficient_data": e.get("insufficient_data")}
    for e in memory_continuity_results.get("per_unit", [])
}

# C2 idle_naturalness analyzer (g-326-26). NAME-MISMATCH FIX: this loop used to
# invoke "idle_behavior", which is not a module — the shipped name is
# idle_naturalness (registered in cli.py by g-318-35). Every run failed into the
# ELSE branch below and logged "not available", so C2 silently used the LLM path
# forever while a working module sat one identifier away. Three sibling names in
# the old list (goal_diversity, movement_naturalness, environment_response) do
# not exist as modules either and are removed — a permanently-failing invocation
# is indistinguishable from a deliberate LLM fallback in the logs, which is
# exactly what hid this.
# Run: cli.py analyze <session> -a idle_naturalness -o json
Bash: cd $REPLAY_DIR && $PYEXE cli.py analyze -a idle_naturalness {session_dir} -o json
idle_naturalness_results = parse JSON output
idle_naturalness_per_unit = {
    e["unit_key"]: {"score": e["score"], "insufficient_data": e.get("insufficient_data")}
    for e in idle_naturalness_results.get("per_unit", [])
}

# Remaining optional modules (run if available, skip gracefully if not).
# ONLY names registered in cli.py belong in this list — see the C2 note above for
# why a phantom name here is worse than no name at all.
# (decision_variety + social_proximity + memory_continuity + idle_naturalness
#  pulled out above — they have dedicated modules + canonical blocks,
#  g-250-144 / g-250-146 / g-326-26.)
FOR EACH module_name in ["grounding_consistency"]:
    Bash: cd $REPLAY_DIR && $PYEXE cli.py analyze -a {module_name} {session_dir} -o json 2>&1
    IF exit code == 0: store results
    ELSE: log "Module {module_name} not available — using LLM evaluation fallback"
```

## Step 2.45: Session Envelope Gate (short-session / zero-data skip)

Before classifying NPCs, check if the session has enough data to score at all.
Short sessions (< 60s duration) and zero-activity sessions (0 cells AND 0 chat
events) produce noise — not signal — in ohs-trend.jsonl. Skip them entirely
rather than scoring them as OHS=1.0 "Broken" via naive evaluation.

```
duration_sec = session metadata duration (from session envelope / NORMAL_SHUTDOWN log)
cell_count = total CellExecutionLog entries across all NPCs
chat_count = total chat Intent events (from Intent/*.jsonl with type="chat")

IF duration_sec < 60 OR (cell_count == 0 AND chat_count == 0):
    session_gate = "SKIPPED_INSUFFICIENT_DATA"
    Log: "SESSION GATE: {session_id} (duration={duration_sec}s, cells={cell_count}, chat={chat_count}) — SKIPPED_INSUFFICIENT_DATA"

    # Record SKIPPED envelope in output but do NOT append to ohs-trend.jsonl
    output = {
        "session_id": session_id,
        "gate": "SKIPPED_INSUFFICIENT_DATA",
        "envelope": {"duration_sec": duration_sec, "cell_count": cell_count, "chat_count": chat_count},
        "reason": "short-session or zero-activity — insufficient data for OHS scoring"
    }
    Write per-NPC report as empty-with-reason; SKIP directly to Step 8 (summary).
```

Rationale: Session 1776781336726_796 (15.2s duration, 0 cells, 0 chat,
NORMAL_SHUTDOWN) would score OHS=1.0 "Broken" on naive evaluation. This gate
catches data-envelope misreads at the session level, mirror-symmetric with
Step 2.5's command_only gate (which catches them at the NPC level). Both
prevent false-positive "Broken" scores from polluting ohs-trend.jsonl — the
north-star time-series bravo reads for portfolio health. See g-226-58,
rb-447 (ceiling-optimism sibling failure mode).

## Step 2.5: Classify NPCs + Probe Chat Activity (command_only gate)

Before scoring, identify NPCs whose intelligence module is `command_only` and probe
the session for chat events. Chat-less `command_only` NPCs are idle-by-design (they
only act when a chat command is issued), not behaviorally broken — scoring them on
C1/C2/C5/C7 produces false-positive "Broken" verdicts that drag overall OHS down.

**Schema note (g-250-66 audit, g-250-68 fix)**: chat-intent records in
`{session_dir}/memory/Intent/*.jsonl` do NOT carry a `targetAyoKey` field.
The schema is:
```json
{"ayoKey": "<speaker>", "intent": {"source": "chat", "conflateStateIntent": {"chatDerived": true, ...}, ...}, ...}
```
The top-level `ayoKey` identifies the NPC that EMITTED the intent (the
speaker / responder), not the addressee. A `command_only` NPC that was
invoked by chat WILL emit a chat-derived response intent; one that was
never invoked won't. Counting chat-derived intents BY speaker is therefore
the correct `chat_events_received` proxy.

```
npc_classification = {}
FOR EACH ayo_key in session NPCs:
    module_type = lookup_intelligence_module(ayo_key, summary)
    npc_classification[ayo_key] = {module: module_type}

# Probe chat-derived response intents per NPC from session Intent logs.
# Path is {session_dir}/memory/Intent/ (not {session_dir}/Intent — that
# directory does not exist in EFS layout; g-250-66 path-confusion fix).
# Filter: intent.source == "chat" (chat-derived) OR
# intent.conflateStateIntent.chatDerived == true. Group by top-level
# ayoKey (the responder). Count rows per ayoKey.
Bash: find "{session_dir}/memory/Intent" -name '*.jsonl' -print 2>/dev/null \
    | xargs -r cat 2>/dev/null \
    | python3 -c "
import json, sys, collections
counts = collections.Counter()
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        d = json.loads(line)
    except Exception:
        continue
    intent = d.get('intent') or {}
    src = intent.get('source')
    chat_derived = (intent.get('conflateStateIntent') or {}).get('chatDerived', False)
    if src == 'chat' or chat_derived:
        speaker = d.get('ayoKey')
        if speaker:
            counts[speaker] += 1
print(json.dumps(dict(counts)))
"
chat_events_by_speaker = parse JSON from stdout  # ayoKey → chat-derived intent count

FOR EACH ayo_key, data in npc_classification:
    IF data.module == "command_only":
        data.chat_events_received = chat_events_by_speaker.get(ayo_key, 0)
        IF data.chat_events_received == 0:
            data.gate = "INACTIVE_BY_DESIGN"  # skip OHS scoring — not broken, just uninvoked
            Log: "OHS GATE: {ayo_key} (command_only, 0 chat-derived response intents) — INACTIVE_BY_DESIGN"
        ELSE:
            data.gate = "SCORE"  # chat-derived response intents present, evaluate responsiveness normally
    ELSE:
        # SKIPPED_NOT_SPAWNED gate (g-318-54, from g-318-52 finding): an autonomous
        # NPC listed in the roster (CharacterDefinitions) that has ZERO presence in
        # the session never spawned — scoring it 1.0 on every axis conflates roster
        # coverage with cognition health of PRESENT NPCs (session 1784285962532_123:
        # Sol/Tricks/Pip/Deb scored "fully INERT" while the ObserverModeModels
        # roster gap — fix g-318-53 — meant they were never in the world at all).
        # Presence probe (ANY one signal = present):
        #   (a) {session_dir}/memory/ConflatedState/{ayo_key}.jsonl exists with >=1 frame
        #   (b) any Intent rows with top-level ayoKey == this NPC
        #   (c) cells_completed > 0 in summary
        # Zero on ALL three → never spawned. Precedent: command_only exclusion above
        # (NPC-level) + SKIPPED_SERVER_ERRORED (session-level).
        IF no ConflatedState frames AND no Intent rows AND no completed cells for ayo_key:
            data.gate = "SKIPPED_NOT_SPAWNED"  # roster-coverage problem, not a cognition score
            Log: "OHS GATE: {ayo_key} (autonomous, zero presence: no ConflatedState frames / Intent rows / cells) — SKIPPED_NOT_SPAWNED (roster gap, not INERT)"
        ELSE:
            data.gate = "SCORE"

# Roster coverage (g-318-54): fraction of AUTONOMOUS roster NPCs actually present.
# Denominator = autonomous (non-command_only) NPCs in the roster; numerator = those
# NOT gated SKIPPED_NOT_SPAWNED. Surfaced alongside session OHS in Step 6 so the
# coverage signal stays loud while axis means stay honest about present NPCs.
auto_roster   = [k for k, d in npc_classification if d.module != "command_only"]
auto_present  = [k for k in auto_roster if npc_classification[k].gate != "SKIPPED_NOT_SPAWNED"]
roster_coverage = f"{len(auto_present)}/{len(auto_roster)}"  # e.g. "3/7" → 0.43
```

Rationale: validated empirically by g-226-50 (session 696 probe — chat-derived
response intents all from baconBob, zero from Ajax/Richmond/testChatNpc; naive
OHS scored them 1.0/Broken even though they were never invoked). Schema
correction validated by g-250-66 audit of sessions 1778581882342_483 +
1777119888638_271: AjaxKey produced 236 module-sourced intents (0 chat) but
was wrongly SCORED 2.12 SELECTION_STARVED under the prior `targetAyoKey`
detection (which always returned 0 because the field doesn't exist),
instead of correctly gated INACTIVE_BY_DESIGN. RichmondKey: 2 chat-derived
response intents → SCORE. testChatNpc: 0 intents → INACTIVE_BY_DESIGN.

## Step 2.6: Intent-Source Breakdown + Cell/Intent Ratio (g-250-46)

Bravo OHS report iter-41 (session 1777057851517_758) initially framed
"jose=0 cells, 0 chat, idle baseline movement" as "aspirational module
idle / generator not firing." Reality: jose emitted 1290 intents (most of
any NPC) at iausScore=0.0 placeholder, and cell selection is score-based,
so aspirational-source intents were structurally starved whenever any
module-source NPC was co-present at iausScore≈0.7. This drove ALPHA
down the wrong diagnostic path initially (check generator/evolver/
proactive_intent logs) when the actual root cause was selection-stage,
not generation-stage. The diagnostic gap was: cells=0 reported without
distinguishing 0-of-0-intents (true idle) from 0-of-1290-intents
(selection-starved).

This step computes the per-NPC intent-by-source breakdown AND
cell/intent ratio so the F-findings narrative routes to the correct
subsystem on subsequent analyses.

```
intents_by_source = {}        # ayo_key → {aspiration: N, module: N, proactive: N, current: N, chat: N, survival: N, unknown: N}
intents_total_by_npc = {}     # ayo_key → sum across sources
cells_per_npc = {}            # ayo_key → completed cell count
cell_per_intent_ratio = {}    # ayo_key → cells/intents (0.0 if no intents)

# Probe Intent files for source breakdown. Intent JSONL records carry a
# "source" field (aspiration|module|proactive|current|chat|survival) and
# a top-level "ayoKey" (the speaker for chat-source records, the actor
# for module/aspiration/etc — per g-250-66 schema audit, chat-source
# intents do NOT have targetAyoKey, so the fallback chain below lands
# on ayoKey which gives correct speaker-counting). Records lacking the
# source field are bucketed as "unknown" — silent absorption is the
# wrong fail-mode for a diagnostic signal (rb-245 family — schema-probe
# negation gate).
Bash: find "{session_dir}/memory/Intent" -name '*.jsonl' -print 2>/dev/null \
    | xargs -r cat 2>/dev/null \
    | python3 -c "
import json, sys, collections
counts = collections.defaultdict(lambda: collections.defaultdict(int))
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        d = json.loads(line)
    except Exception:
        continue
    npc = d.get('targetAyoKey') or d.get('ayoKey') or d.get('owner') or d.get('npc')
    # source nests under 'intent' per the Intent/*.jsonl schema (see Step 2.45
    # probe + schema example above); reading top-level d['source'] mis-buckets
    # every nested-source row as 'unknown' (g-250-96 root cause).
    # Population-rank order (rb-1293, zeta msg-20260524-112539-zeta-1644,
    # session 1778883505393_260 2540-record scan):
    #   intent.candidateSource = 2540/2540 (100%, universal classifier —
    #                            values: current 1586, module 324,
    #                            aspiration 323, proactive 254, survival 53)
    #   intent.source          =  323/2540  (12.7%, populated only when
    #                            candidateSource == 'aspiration' — jose only)
    #   d['source']            =    0/2540  ( 0%, legacy top-level schema)
    # Reading intent.source FIRST left 87pct (2217/2540) bucketed as 'unknown'
    # — fix per rb-1293 extends rb-245 (schema-probe-first) / rb-1254
    # (sibling-probe-consistency): a 'read the nested field' fix is only
    # correct if that nested field is actually POPULATED. candidateSource
    # subsumes intent.source on aspiration rows (both 'aspiration' when
    # present), so prepending it is safe — no row gets a different label.
    src = (d.get('intent') or {}).get('candidateSource') or (d.get('intent') or {}).get('source') or d.get('source') or d.get('intentSource') or 'unknown'
    if npc:
        counts[npc][src] += 1
print(json.dumps(counts))
"
intents_by_source = parse JSON from stdout

# Cell count per NPC. Prefer summary.npcs.{ayo_key}.cells_completed when
# present; otherwise grep CellExecutionLog files keyed on owner. Cells
# completed (not initiated) is the right denominator — partial-execution
# cells are not "cell success" for the ratio.
FOR EACH ayo_key in session NPCs:
    cells_per_npc[ayo_key] = (
        summary.get('npcs', {}).get(ayo_key, {}).get('cells_completed', 0)
    )
    intents_total = sum(intents_by_source.get(ayo_key, {}).values())
    intents_total_by_npc[ayo_key] = intents_total
    if intents_total > 0:
        cell_per_intent_ratio[ayo_key] = round(cells_per_npc[ayo_key] / intents_total, 4)
    else:
        cell_per_intent_ratio[ayo_key] = 0.0
```

### Generator-State Classification

Each NPC is classified by the (intents, cells) signature:

| intents_total | cells_completed | generator_state    | Diagnostic routing                                                |
|---------------|-----------------|--------------------|-------------------------------------------------------------------|
| 0             | 0               | `GENERATOR_IDLE`   | Generator/Evolver/proactive_intent_generator pipeline (upstream)  |
| > 0           | 0               | `SELECTION_STARVED`| Intent-to-cell selection — iausScore, scheduler weighting (mid)   |
| > 0           | > 0             | `ACTIVE`           | Both stages functional — score normally                           |

```
FOR EACH ayo_key in session NPCs:
    intents_total = intents_total_by_npc.get(ayo_key, 0)
    cells = cells_per_npc.get(ayo_key, 0)
    if intents_total == 0:
        npc_classification[ayo_key].generator_state = "GENERATOR_IDLE"
    elif cells == 0:
        npc_classification[ayo_key].generator_state = "SELECTION_STARVED"
    else:
        npc_classification[ayo_key].generator_state = "ACTIVE"
```

### F-Findings Narrative Directive (LLM compose-time)

When composing the markdown OHS report's "Critical Findings" section, MUST
use generator_state to phrase the F-finding so the receiving agent routes
diagnostic effort to the correct subsystem. Required phrasings:

- **GENERATOR_IDLE** — example: "{npc} produced 0 intents — {module} module
  did not fire. Investigate Generator/Evolver/proactive_intent_generator
  pipeline."
- **SELECTION_STARVED** — example: "{npc} produced {N} intents but 0 cells.
  {module} module IS firing; intent-to-cell selection is starving
  {module}-source intents (likely iausScore weighting / scheduler priority).
  Investigate selection stage — NOT the generator."
- **ACTIVE** — score normally; no special routing.

Anti-pattern: writing "module idle / generator not firing" without
checking intents_total. The iter-41 incident drove a 25min routing
detour (alpha first checked generator logs, then re-routed to selection
analysis on iter-42) — encoding the classification here prevents
recurrence.

## Step 3: Score Each NPC on Humanness Rubric

Load rubric from knowledge tree:
```
node=$(bash core/scripts/tree-find-node.sh --text "npc-humanness-rubric" --leaf-only --top 1)
Read {node.file}
```

For each NPC in the session, score criteria C1-C13 (OHS v13 — extended from v12 by g-250-123 adding C13 Contextual Reaction Appropriateness; from v11 by g-316-16 adding C12 Grounding Consistency; from v10 by g-004-05 adding C11 Memory Continuity). Canonical weight + redistribution rationale lives in `world/knowledge/tree/intelligence/npc-intelligence/npc-learning-evaluation/npc-evaluation/evaluation-methods/npc-humanness-rubric/humanness-composite-scoring.md` (the rubric tree node). Weights here MUST match that node; any drift is a documentation bug. **Path corrected g-326-26**: this pointer named the pre-2026-07-16 location, which g-115-2318 retired to a tombstone during a tree reorg — `world/scripts/ohs-version-parity-guard.sh` hardcoded the same stale path, so it read a file with no OHS banner and returned rc=2 "probe error" instead of the rc=1 "DRIFT" that was true. **KNOWN OPEN DRIFT: this analyzer is v13 (C1-C13) while the canonical node is still v12 (`OHS_v12 = sum over C1..C12`) with C13 absent from its weight table** — a live recurrence of the g-250-165 class guard-927 exists to prevent. Reconciling the node is tracked separately; do not assume the node's weights cover C13.

| Criterion | Weight (v13) | Canonical Formula (1-5 transformation: `1.0 + 4.0 * composite`, except C13 banded) | Fallback |
|-----------|--------------|-----------------|----------|
| C1: Movement Naturalness | 0.02 | Canonical composite (g-115-859): `0.50*movement_smoothness + 0.30*stuck_freedom + 0.20*directional_entropy`, all sub-metrics in [0,1] higher=better. Source modules: MovementAnalyzer + StuckDetector. See C1 automation block below for the full pseudocode. | Path entropy from positions (when MovementAnalyzer unavailable, neutral=0.5) |
| C2: Idle Behavior Variety | 0.08 | Canonical composite (g-115-859): `0.60*idle_intent_diversity + 0.40*non_repetitive_actions` over idle windows (>=10-tick no-movement no-chat periods). The LLM computes this composite deterministically (see C2 automation block) — **the canonical formula is unchanged**. Correction (g-326-26): the old row claimed "No analyzer module exists"; that was FALSE — `replay/analysis/idle_naturalness.py` (IdleNaturalnessAnalyzer) ships and is cli.py-registered (g-318-35). It went uninvoked because this skill's optional-module loop asked for `idle_behavior`, a name that does not exist, so every run failed into the LLM-fallback branch. The invocation is fixed, but **the module's score is deliberately NOT substituted for C2**: it emits a DIFFERENT composite (idle_ratio, idle_movement_ratio, avg/max_idle_duration, idle_intent_variety) and supplies no `non_repetitive_actions` term, so swapping producers would silently redefine the axis and break ohs-trend comparability without a version bump. Use `idle_naturalness_per_unit` as CORROBORATION: its `idle_intent_variety` is the module-side analogue of `idle_intent_diversity` — a large divergence between them indicates one of the two is wrong and is worth investigating, not silently averaging. **Verified scale (g-326-26, by running the module): idle_naturalness per_unit `score` is a FLOAT in [0,1]** — so it WOULD need the `1.0 + 4.0 * score` transform if ever promoted to source C2, unlike memory_continuity's integer 1-5. It carries real per-NPC variance (observed 0.0–0.318 across 12 NPCs on session 1784348733966_310), i.e. a live discriminating signal is sitting unused here; if the LLM-computed C2 shows near-zero variance on the same session, prefer investigating that gap over trusting the flat value. Entries are `{unit_key, axis, score}` with NO `insufficient_data` key. | Neutral 0.5 when no idle periods exist (always-active session). |
| C3: Reaction Latency | 0.12 | Canonical composite (g-250-40): `0.20*utility_scored_fraction + 0.40*utility_match_rate + 0.20*iaus_score_calibration + 0.20*intent_churn_penalty`, all sub-metrics in [0,1] higher=better. Source module: IntentAppropriateness analyzer. See bravo/reports/g-250-35-jose-drift-findings.md and C3 automation block below. | Intent change frequency |
| C4: Goal-Directed Purposefulness | 0.09 | summary (intent coverage + cell success) | Direct computation |
| C5: Social Proximity Seeking | 0.10 | Canonical composite (g-115-859): `0.50*proximity_engagement_rate + 0.50*approach_initiation_rate` over 16-stud proximity events. **Source module: SocialProximityAnalyzer** — wired by g-250-146; the row previously still read "No analyzer module exists", pure docs-lag with no functional effect (the C5 automation block has invoked the module since that fix). Text corrected g-326-26. Single-NPC sessions: neutral 0.5. Personality config can shift interpretation (asocial characters score high by avoiding). See C5 automation block. | Position pairwise distances (legacy) |
| C6: Emotional Expression | 0.08 | Canonical composite (g-115-859): `0.40*state_change_diversity + 0.60*state_appropriateness`. **Source module: EmotionExpressionAnalyzer** (g-250-135) — reads dominantEmotion/emotionalIntensity transitions from the reconstructed ConflatedState (`Frame.npc_emotion`). state_appropriateness = emotion-activity coherence (non-neutral frames coinciding with an active intent), weighted 0.60 because grounded beats random-flipping. `insufficient_data` (NPC carried no emotion state) falls back to the c6=0.3 floor. Prior LLM-extraction path pinned EVERY NPC at the 0.3 floor because no code surfaced the transitions — a false zero-affect artifact (g-250-134; the data was in ConflatedState all along). See C6 automation block. | EmotionExpressionAnalyzer per_unit C6 |
| C7: Behavioral Unpredictability | 0.07 | decision_variety module | Intent sequence entropy |
| C8: Conversational Relevance | 0.06 | Canonical: chat_count>=3 uses chat_repetition module variety score (g-226-52); chat_count in [1,2] uses g-115-859 sparse formula `0.6*is_contextually_addressed + 0.4*inverse_length`; chat_count==0 is NOT EVALUABLE — LLM-fallback only with explicit "no chat observed" marker, score may be excluded from this NPC's OHS. C8 owns within-conversation coherence; C12 owns state-grounding (no overlap). See C8 + C8-sparse automation blocks. | LLM evaluation when chat_count == 0 |
| C9: Spatial Awareness | 0.02 | Canonical (g-250-15/16/24/29/31): mean of two C9-axis analyzers when both present — (a) SteeringQualityAnalyzer composite `0.40*obstacle_avoidance + 0.30*separation_consistency + 0.30*approach_directness`, (b) PathVarietyAnalyzer composite `0.30*pathfinding_correctness + 0.20*directional_entropy + 0.15*turn_rate + 0.15*memory_utilization + 0.12*area_coverage + 0.08*(1−revisit_ratio)`. Falls back to whichever is present. See C9 automation block. | StuckDetector + MovementAnalyzer (secondary) |
| C10: Personality Consistency | 0.10 | Canonical composite (g-115-859): `0.50*intent_concentration_score + 0.50*baseline_match_score`. intent_concentration_score = top-intent fraction clipped to [0.10, 0.60] then mapped linearly to [0, 1] (10%=no personality, 60%=highly consistent). baseline_match_score = cosine sim to per-character behavioral spec; 0.5 if no spec. See C10 automation block. | Neutral 0.5 when no baseline spec exists |
| C11: Memory Continuity | 0.12 | PrivateNotes utilization composite (g-004-05): proportion of recent observations the NPC stores and recalls across utterances. **Source module: MemoryContinuityAnalyzer** (landed 98fdbc4 2026-06-22 g-316-08; WIRED HERE by g-326-26 — it was registered in cli.py but never invoked by this skill for ~4 weeks, so C11 silently used LLM-eval and the LLM applied the old row's documented fleet floor to every NPC). Returns INTEGER 1-5 by literal return — a non-integer C11 in ohs-trend is therefore LLM-authored, which is how the two-producer mixing was caught (g-326-25). See C11 automation block. **The 1.0 floor is currently REAL, not a documentation default**: both routes to utilization>0 are dead (g-326-24) — the inference path requires a PrivateNote timestamped before `timeline.start_timestamp` (= `frames[0].timestamp`), which in-session notes never satisfy, and `references_prior_event_id` is absent from the session tree. Expect 1.0 until those routes are fixed; that is a live measurement, not a fallback. **STORE-vs-RECALL — do not misread the 1.0** (zeta g-326-15, msg-20260717-190205-zeta-3580, rb-3860): the old "PrivateNotes utilization 0% across 6,949 records" justification is SUPERSEDED and survives only as an April-2026 historical baseline. That 0% measured zero EXERCISE (recall), never zero storage — storage was always non-zero, and the recall pipeline (chatSummaries recall, g-250-133 observation channel, self-monitor summary, g-250-148 salience gate) was built AFTER that window, so the store is now populated at 30-40k lines/session. Therefore a module-measured C11=1.0 means **the analyzer cannot SEE recall**, NOT that the NPCs fail to remember. Measured 2026-07-19 on session 1784348733966_310: all 12 NPCs score 1, and `baconBob1Test` shows `chat_event_count: 314` with `prior_encounter_note_count: 0` and `note_utilization_rate: 0.0` — 314 chat events and not one prior-encounter note detected, which is a DETECTION failure signature, not an absence-of-memory signature. Fix the routes before treating C11 as evidence about NPC memory quality. | MemoryContinuityAnalyzer per_unit C11; LLM evaluation only when the module returns no per_unit row |
| C12: Grounding Consistency | 0.07 | Mean of four sub-signals (g-316-16 design doc, c12-grounding-design-2026-05-16.md): (1) state-grounding accuracy (dialogue claims vs ground-truth game state at utterance frame, from `intent.dialogue` + CellExecutionLog snapshot); (2) intra-utterance consistency (no self-contradiction within one chat); (3) cross-utterance consistency (no contradiction across NPC's session history); (4) persona-grounding alignment (dialogue claims consistent with character spec). C12 = mean({s1..s4} \| s_i != null), 0-1 scale, mapped to 1-5 via `1.0 + 4.0 * C12`. Module: **GroundingConsistencyAnalyzer** — SHIPS and is cli.py-registered; invoked by the optional-module loop in Step 2.4. The row previously read "(pending)"; corrected g-326-26 after an audit found four rows understating which modules exist. If its per_unit output is absent or thin, verify the module's own extraction rather than assuming no module exists (see design doc Outcome 3 for the sub-signal algorithms). **Chat-gate (g-250-147): chat_count==0 → C12 NOT EVALUABLE (None, excluded from the per-axis aggregate — no dialogue to ground-check, symmetric to the C8 chat_count==0 rule, module-agnostic); chat_count in [1,2] → low-sample LLM grounding; chat_count>=3 → analyzer/LLM grounding. See the C12 automation block in Step 3.** | LLM evaluation against state-replay snapshots when analyzer unavailable (chat_count>=1 only — chat_count==0 returns None) |
| C13: Contextual Reaction Appropriateness | 0.07 | SIGNED reaction-appropriateness axis (g-250-123, design g-250-122 Gap D in `client-signal-reconciliation.md`; closes asp-224 S2 Contextual Reactivity, P0 76% demand). `CRA = clamp01(Wr/max(R,1) − 0.5*Sp/max(VisibleReactions,1))` (0-1): **R** = salient context-change events the NPC SHOULD react to (a player enters the 27-stud social radius AND addresses/faces the NPC; a per-character-salient world-state change; a tracked player-state change — salience classified per-character via the per-character-behavioral-specs subtree); **Wr** = of those R, how many drew a visible reaction within the react window (reuses C3 dialogue/latency + C6 BarStateService 5-dim + BT-intent + gaze-orientation signals already read from frames); **Sp** = visible reactions fired at NON-salient stimuli (the canonical merchant_07 spurious-greeting restraint failure); **VisibleReactions** = Wr+Sp. **BANDED to 1-5** (NOT the `1.0+4.0*composite` linear transform): 5=CRA≥0.80, 4=0.60–0.80, 3=0.40–0.60, 2=0.20–0.40, 1=CRA<0.20 — score 1 catches BOTH under-reaction AND spurious-flooding (symmetric restraint). See C13 automation block. Module: ContextualReactionAnalyzer (pending — like C11/C12, LLM computes R/Wr/Sp from loaded intents+frames+per-character specs until module lands). | LLM evaluation of R/Wr/Sp from replay frames when no analyzer module present |

```
FOR EACH ayo_key in session NPCs:
    # Honor the Step 2.5 gate — skip scoring for inactive-by-design NPCs
    IF npc_classification[ayo_key].gate == "INACTIVE_BY_DESIGN":
        npc_scores[ayo_key] = {
            scores: null,
            ohs: null,
            label: "INACTIVE_BY_DESIGN",
            module: npc_classification[ayo_key].module,
            chat_events_received: 0,
            reason: "command_only NPC with zero chat events — idle-by-design, not scored"
        }
        continue

    scores = {}
    FOR EACH criterion C1..C13:
        IF automated module available:
            scores[criterion] = compute_from_module(criterion, ayo_key)
        ELSE:
            scores[criterion] = llm_evaluate(criterion, ayo_key, session_data)

    # C8 automation (chat_repetition sub-signal, g-226-52):
    # The analyzer returns a 0-1 variety score or null when chat_count < 3.
    # Map 0-1 → 1-5 (OHS scale); null means "insufficient chat data" — fall
    # back to LLM eval rather than anchoring C8 at 1.0 for silent NPCs.
    chat_rep = chat_repetition_results["npcs"].get(ayo_key, {})
    chat_score = chat_rep.get("chat_repetition_score")
    IF chat_score is not None:
        scores["C8"] = round(1.0 + 4.0 * chat_score, 2)  # 0.34 → 2.36 (Robotic)
        scores["_C8_source"] = "chat_repetition module"
    ELIF chat_rep.get("chat_count", 0) == 0:
        # No chat at all — C8 not evaluable; mark explicitly rather than guessing.
        scores["C8"] = llm_evaluate("C8", ayo_key, session_data)
        scores["_C8_source"] = "LLM (no chat observed)"
    ELSE:
        # 1-2 chats — too few for variety measurement; defer to LLM.
        scores["C8"] = llm_evaluate("C8", ayo_key, session_data)
        scores["_C8_source"] = "LLM (chat_count < 3)"

    # C12 automation (grounding-consistency chat-gate — g-250-147). C12 grounds
    # DIALOGUE claims against ground-truth game state, so it requires the NPC to
    # have PRODUCED chat to evaluate — symmetric to the C8 chat_count==0 rule
    # above (the goal cites that symmetry explicitly). Without this gate C12 fell
    # through the generic llm_evaluate loop to a NEUTRAL 3.0 for EVERY NPC
    # (ohs-trend row 1781723603748_196: C12 mean 3.0 / min 3.0 / var 0.0 / n=5) —
    # a false grounding score that polluted every multi-NPC OHS composite
    # containing a chat-less NPC. GroundingConsistencyAnalyzer is still pending
    # (see the C12 rubric row); until it lands, gate on chat_count so a chat-less
    # NPC is EXCLUDED (None sentinel), NOT false-scored — the g-250-146 rb-1880
    # class-3 recipe. Module-agnostic BY DESIGN: the invariant is "no dialogue =
    # nothing to ground-check", so an autonomous NPC with 0 chat is excluded too,
    # not just command_only (command_only is the goal's observed instance, not a
    # scope restriction). Reuses the same chat_rep the C8 block read above.
    c12_chat_count = chat_rep.get("chat_count", 0)
    IF c12_chat_count == 0:
        # No dialogue produced — nothing to ground-check. NOT EVALUABLE: exclude
        # from this NPC's OHS (the g-250-129 sentinel contract), never a neutral 3.0.
        scores["C12"] = None
        scores["_C12_source"] = "NOT EVALUABLE (chat_count==0: no dialogue to ground-check; excluded, g-250-147)"
    ELIF c12_chat_count in (1, 2):
        # 1-2 replies — score grounding on those replies, marked low-sample.
        scores["C12"] = llm_evaluate("C12", ayo_key, session_data)
        scores["_C12_source"] = "LLM low-sample grounding (chat_count in [1,2])"
    ELSE:
        # chat_count >= 3 — GroundingConsistencyAnalyzer four sub-signals when it
        # lands (C12 rubric row), else LLM grounding-eval vs state-replay snapshots.
        scores["C12"] = llm_evaluate("C12", ayo_key, session_data)
        scores["_C12_source"] = "LLM grounding-eval (chat_count >= 3; GroundingConsistencyAnalyzer pending)"

    # C1 automation (canonical formula — g-115-859 cross-agent divergence
    # closure; complements MovementAnalyzer + StuckDetector outputs).
    # Composite per g-115-859 spec:
    #   0.50 * movement_smoothness    (MovementAnalyzer composite, 0-1)
    # + 0.30 * stuck_freedom           (1.0 - stuck_rate from StuckDetector, 0-1)
    # + 0.20 * directional_entropy     (path entropy from positions, 0-1 normalized)
    # All three sub-metrics in [0, 1] HIGHER=better. stuck_freedom inverts
    # StuckDetector's stuck_rate so it aligns with the higher=better convention.
    # When MovementAnalyzer is unavailable, set movement_smoothness=0.5 (neutral)
    # and the formula degenerates to weighted stuck + entropy — still deterministic.
    # FIX g-250-151 (verified vs session 943): movement_results is
    # {"npcs": {ayo_key: {...}}, "per_unit": [...]} and stuck_results is
    # {"per_unit": [{unit_key, score}], "stuck_episodes":..., ...} with NO top-level
    # "npcs" key. The prior movement_results.get(ayo_key)/stuck_results.get(ayo_key)
    # returned {} for EVERY NPC -> mv_smooth/dir_entropy pinned 0.5 + stuck_free 1.0
    # -> C1 a constant 3.60 regardless of real movement (masked C1, asp-250's weakest
    # axis). Every other analyzer block drills into ["npcs"] first (steering, path_variety,
    # chat_repetition); C1 alone omitted it. rb-1831/rb-455/guard-645: read the correct
    # field path, gate per-unit on per-NPC data.
    mv_npcs = movement_results.get("npcs", {}) if movement_results else {}
    mv_data = mv_npcs.get(ayo_key, {})
    # stuck_detector emits per_unit [{unit_key, score}] where score IS stuck_freedom
    # (1 - stuck_rate; stuck_detector.py); there is no top-level "npcs" dict.
    stuck_pu = {e.get("unit_key"): e.get("score") for e in (stuck_results.get("per_unit", []) if stuck_results else [])}
    # movement_smoothness term = MovementAnalyzer composite (movement_naturalness_score),
    # per the C1 rubric row ("movement_smoothness (MovementAnalyzer composite, 0-1)").
    mv_smooth = float(mv_data.get("movement_naturalness_score", 0.5))   # neutral if missing
    sf = stuck_pu.get(ayo_key)
    stuck_free = float(sf) if sf is not None else 1.0   # per_unit score already = stuck_freedom
    dir_entropy = 0.5  # MovementAnalyzer emits no directional entropy; documented neutral fallback
    c1_composite = round(0.50 * mv_smooth + 0.30 * stuck_free + 0.20 * dir_entropy, 3)
    scores["C1"] = round(1.0 + 4.0 * c1_composite, 2)
    scores["_C1_source"] = "canonical (movement_naturalness/stuck_freedom/entropy-neutral, g-250-151 field-mapping fix)"

    # C2 automation (canonical formula — g-115-859 closure for the axis with
    # NO analyzer module). Composite per g-115-859 spec:
    #   0.60 * idle_intent_diversity   (distinct intent labels during idle periods / idle period count)
    # + 0.40 * non_repetitive_actions  (1.0 - max(action_freq_during_idle / idle_count))
    # An "idle period" is a window of >=10 ticks with no positional change AND
    # no chat intent. idle_intent_diversity uniquely names what the NPC did
    # *during* idle (e.g., emote, look-around, animation). The MAX-freq inversion
    # in non_repetitive_actions penalizes "always idle-animate" NPCs. Both
    # sub-metrics clamped to [0, 1]; the canonical formula consumes them
    # deterministically. When no idle periods exist (NPC always active),
    # set c2_composite=0.5 (neutral — variety is not evaluable, not bad).
    intents_for_npc = [i for i in all_intents if i.get("ayoKey") == ayo_key]
    idle_windows = compute_idle_windows(intents_for_npc, min_idle_ticks=10)
    IF len(idle_windows) == 0:
        c2_composite = 0.5  # neutral — variety not evaluable in always-active sessions
    ELSE:
        distinct_idle_intents = len(set(w.intent_label for w in idle_windows))
        idle_intent_diversity = min(1.0, distinct_idle_intents / max(1, len(idle_windows)))
        action_freqs = Counter(w.intent_label for w in idle_windows)
        max_freq_share = max(action_freqs.values()) / len(idle_windows)
        non_repetitive = 1.0 - max_freq_share
        c2_composite = round(0.60 * idle_intent_diversity + 0.40 * non_repetitive, 3)
    scores["C2"] = round(1.0 + 4.0 * c2_composite, 2)
    scores["_C2_source"] = "canonical (idle-diversity/non-repetitive composite)"

    # C3 automation (intent_appropriateness action-selection composite,
    # g-250-14, g-250-22, g-250-40). Composite per g-250-40 spec:
    #   0.20 * utility_scored_fraction
    # + 0.40 * utility_match_rate
    # + 0.20 * iaus_score_calibration
    # + 0.20 * intent_churn_penalty
    # All four sub-metrics in [0, 1] where HIGHER is better. g-250-40 split
    # the previous context_match_rate into utility_scored_fraction (was the
    # scoring subsystem engaged — fraction of intents with iausScore > 0)
    # and utility_match_rate (given a utility-scored subset, fraction above
    # 0.5 threshold). See bravo/reports/g-250-35-jose-drift-findings.md.
    # As before, intent_churn_penalty is a churn-FREE score (1.0 = no churn)
    # despite the "penalty" label.
    ia_npcs = intent_appropriateness_results.get("per_npc", {})
    ia_data = ia_npcs.get(ayo_key, {})
    IF ia_data.get("intent_count", 0) > 0:
        ia_scored = float(ia_data.get("utility_scored_fraction", 0.0))
        ia_match = float(ia_data.get("utility_match_rate", 0.5))
        ia_iaus = float(ia_data.get("iaus_score_calibration", 0.5))
        ia_churn_free = float(ia_data.get("intent_churn_penalty", 1.0))
        action_selection_composite = round(
            0.20 * ia_scored + 0.40 * ia_match + 0.20 * ia_iaus + 0.20 * ia_churn_free, 3
        )
        scores["C3"] = round(1.0 + 4.0 * action_selection_composite, 2)
        scores["_C3_source"] = "intent_appropriateness composite (scored-frac/match/IAUS/churn-free)"
    ELSE:
        # No intents observed — C3 not evaluable from this signal path.
        scores["_C3_source"] = "intent change frequency (fallback)"

    # C9 automation (blended composite from both C9-axis analyzers —
    # steering_quality g-250-15/g-250-16 AND path_variety g-250-29):
    # Both analyzers self-declare axis="C9" with their own 0-1 composite.
    # Blend by mean when both present; fall back to whichever is present.
    # Maps 0-1 → 1-5 via 1.0 + 4.0 * blended. A blended of 0.0 (both
    # analyzers collapsed to zero) maps to C9 = 1.0 "Broken" — signal, not
    # outage. Prefer this over StuckDetector heuristics that would paper
    # over absence of navigation data. Semantic complementarity: steering
    # measures "did you move smoothly toward goals", path_variety measures
    # "did you cover the space with intentional navigation (incl. pathfinding
    # correctness from g-250-24)".
    sq_npcs = steering_quality_results.get("npcs", {})
    sq_data = sq_npcs.get(ayo_key, {})
    sq_score = sq_data.get("steering_quality_score")
    # g-250-129: steering_quality emits steering_quality_score=None +
    # insufficient_data=True for NPCs whose movement data is too sparse to yield
    # a real obstacle/directness signal (the degenerate-uniform 0.19 case — the
    # composite collapsed to the SESSION-WIDE separation broadcast term and
    # reported one identical value for every NPC, masking real C9 differences;
    # rb-1811/rb-1831). Honor the EXPLICIT flag the analyzer emits, not just
    # None: the analyzer is the single source of truth for "is this score
    # trustworthy", so a (hypothetical) non-None-but-insufficient score must
    # STILL be excluded from C9. When steering is unusable, fall back to
    # path_variety (all-per-NPC, does not degenerate). Mirrors chat_repetition's
    # null-on-<3-intents contract for C8.
    sq_insufficient = sq_data.get("insufficient_data", False)
    sq_usable = sq_score is not None and not sq_insufficient
    pv_npcs = path_variety_results.get("npcs", {})
    pv_data = pv_npcs.get(ayo_key, {})
    pv_score = pv_data.get("path_variety_score")
    IF sq_usable AND pv_score is not None:
        blended = (float(sq_score) + float(pv_score)) / 2.0
        scores["C9"] = round(1.0 + 4.0 * blended, 2)
        scores["_C9_source"] = "mean(steering_quality, path_variety) composite"
    ELIF sq_usable:
        scores["C9"] = round(1.0 + 4.0 * float(sq_score), 2)
        scores["_C9_source"] = "steering_quality composite (path_variety unavailable)"
    ELIF pv_score is not None:
        # steering_quality unavailable OR insufficient_data — documented C9
        # fallback to path_variety (g-250-129 acceptance criterion #3), BUT
        # now gated by a near-stationary penalty (Remedy 2, g-318-30 / rb-2773).
        #
        # THE BUG (guard-838; s63 zombie1Test): path_variety stays HIGH for a
        # WEDGED explorer (pv 0.58, revisit_ratio 0.999 -> C9 3.32 "Competent")
        # despite 8 unique_positions / avg_speed 0.04 over an 815s window. When
        # steering could not be scored BECAUSE the NPC is near-stationary,
        # path_variety (inflated by in-place jitter-entropy) is NOT a trustworthy
        # C9 signal. The C9 rubric node's OWN threshold already says "Score 3+
        # requires stuck rate < 0.10"; the blind path_variety fallback violated
        # it. This branch ENFORCES that pre-existing gate — it invents no new rule.
        #
        # Near-stationarity is detected from the RAW movement signal avg_speed,
        # which is WINDOW-LENGTH-INDEPENDENT (a rate) — so it distinguishes a
        # genuinely wedged NPC from a merely short observation window (a raw
        # unique_positions count cannot). NEAR_STATIONARY_AVG_SPEED mirrors the C9
        # node's own stuck definition ("< 0.5 stud movement over 10+ frames").
        # Only the NEAR-STATIONARY sub-mode is targeted (rb-2773 decomposition):
        # a MOVING NPC whose steering is insufficient for OTHER reasons (the
        # degenerate-uniform session-wide broadcast, rb-1811/rb-1831) has
        # avg_speed >= 0.5 and keeps the documented path_variety fallback
        # UNCHANGED (no regression on the g-250-129 case). NOTE: C9 weight is only
        # 0.02, so this corrects the C9 AXIS + OHS_player_perceived (min-axis felt
        # score); the weighted-mean OHS barely moves — surfacing the wedged
        # explorer in the HEADLINE requires the deferred broader remedy (a
        # weighted coverage-breadth axis / purpose-failure cap), filed separately.
        NEAR_STATIONARY_AVG_SPEED = 0.5    # C9-node stuck definition (studs/frame)
        NEAR_STATIONARY_UNIQUE_POS = 20    # g-318-30 outcome-#1 wedge threshold (penalty scale)
        mv_c9 = (movement_results.get("npcs", {}) if movement_results else {}).get(ayo_key, {})
        c9_avg_speed = mv_c9.get("avg_speed")
        c9_uniq_pos  = mv_c9.get("unique_positions")
        IF c9_avg_speed is not None AND float(c9_avg_speed) < NEAR_STATIONARY_AVG_SPEED:
            # Wedged: penalize C9 to the Broken..Robotic band instead of the
            # path_variety fallback. Scale by the coverage deficit
            # (unique_positions / threshold), floored at Broken (1.0), capped at
            # Robotic (2.0) — a near-stationary explorer shows "no meaningful
            # navigation" (C9 band 1-2), never Competent (3+).
            cov = min(1.0, float(c9_uniq_pos) / float(NEAR_STATIONARY_UNIQUE_POS)) if c9_uniq_pos is not None else 0.5
            scores["C9"] = round(1.0 + 1.0 * cov, 2)
            scores["_C9_source"] = "near-stationary penalty: steering insufficient + avg_speed=%s < %.1f (unique_positions=%s) -> C9 capped Broken..Robotic; Remedy 2 stuck-rate gate (g-318-30/rb-2773/guard-838)" % (c9_avg_speed, NEAR_STATIONARY_AVG_SPEED, c9_uniq_pos)
        ELSE:
            # steering insufficient but NPC IS moving (avg_speed >= threshold or
            # unavailable) — documented path_variety fallback, unchanged.
            scores["C9"] = round(1.0 + 4.0 * float(pv_score), 2)
            scores["_C9_source"] = "path_variety composite (steering unavailable/insufficient; NPC not near-stationary)"
    ELSE:
        # Both composites unavailable (analyzers skipped/errored, or steering
        # insufficient AND path_variety absent) — fall back to legacy
        # StuckDetector + MovementAnalyzer path.
        scores["_C9_source"] = "StuckDetector + MovementAnalyzer (fallback)"

    # C5 automation (g-250-146: now SOURCED FROM the social_proximity analyzer,
    # not the LLM-eval pseudocode path). The analyzer computes, per NPC from
    # per-frame positions, proximity_engagement_rate (close-pair frames within
    # 16 studs / proximity-eligible frames) + approach_initiation_rate (self
    # displacement projected onto the self->other direction, so "moves TOWARD"
    # is attributable to THIS NPC, not passive proximity) and emits a per_unit
    # C5 score in [0,1] = 0.50*engagement + 0.50*approach, plus an
    # insufficient_data flag. This closes the rb-1880 class-3 extraction-blind
    # floor: the g-115-859 canonical block left the composite as pseudocode
    # helpers (count_proximity_eligible_frames etc.) that the LLM never actually
    # evaluated, so C5 sat at neutral 3.0 / var 0.0 in multi-NPC sessions even
    # though positions made proximity computable (positive control: C1 movement
    # scores per-NPC var ~0.14 on the SAME positions). The analyzer is the single
    # source of truth; honor its insufficient_data flag — single-NPC sessions and
    # command_only NPCs that never co-locate are NOT EVALUABLE for C5 (score None,
    # EXCLUDED from this NPC's OHS), NOT a false neutral 3.0 (the g-250-129
    # sentinel contract; same treatment as C7 < 5 intents). NOTE: this is a
    # re-baseline edge (rb-1986) — pre-2026-06-18 C5 rows are neutral-3.0
    # placeholders, NOT comparable to the analyzer-sourced values. Personality
    # config can still shift the interpretation downstream (an asocial-by-design
    # character that avoids proximity scores low-engagement here — a
    # per-character-spec layer, not the raw composite; see g-250-99).
    # Run: cli.py analyze <session> -a social_proximity -o json
    proximity = social_proximity_per_unit.get(ayo_key)  # {"score", "insufficient_data"}
    IF proximity is not None AND not proximity["insufficient_data"] AND proximity["score"] is not None:
        c5_composite = proximity["score"]
        scores["C5"] = round(1.0 + 4.0 * c5_composite, 2)
        scores["_C5_source"] = "social_proximity analyzer (positions->engagement+approach composite, g-250-146)"
    ELSE:
        # No other NPC ever co-present / < 10 co-present frames — C5 NOT EVALUABLE.
        # Exclude from this NPC's OHS (None) rather than a false neutral 3.0 — the
        # g-250-129 sentinel contract. (Was: neutral 0.5 -> 3.0, which diluted the
        # OHS mean toward 3.0 and produced the var-0 flat-neutral telemetry.)
        scores["C5"] = None
        scores["_C5_source"] = "NOT EVALUABLE (social_proximity insufficient_data: single-NPC or < 10 co-present frames)"

    # C6 automation (g-250-135: now SOURCED FROM the emotion_expression analyzer,
    # not LLM extraction). The analyzer reads dominantEmotion / emotionalIntensity
    # transitions from the reconstructed ConflatedState (Frame.npc_emotion) and
    # emits a per_unit C6 score in [0,1] = 0.40*state_change_diversity +
    # 0.60*state_appropriateness, plus an insufficient_data flag.
    #
    # This closes the g-250-134 extraction artifact: the prior LLM-extraction
    # path looked for EmotionalPostureOverlay v2 posture transitions that NO code
    # surfaced, so it found 0 state_changes and pinned c6_composite=0.3 (banded
    # 2.2 "Robotic") for EVERY NPC — a FALSE zero-affect floor. The emotion data
    # was present in ConflatedState the whole time (the autonomous NPCs transition
    # neutral->excited/confused at intensities up to 1.0). The analyzer is the
    # single source of truth; honor its insufficient_data flag (None means the
    # NPC carried no emotion state — pre-EmotionalPerception legacy session or a
    # unit the verticle never scored — which falls back to the documented floor,
    # NOT a false zero). state_appropriateness here is the emotion-activity
    # coherence proxy (non-neutral frames that coincide with an active intent);
    # the LLM's contextual judgement is no longer needed to lift the axis off the
    # floor, because the analyzer now supplies the transition evidence.
    # Run: cli.py analyze <session> -a emotion_expression -o json
    emotion = emotion_expression_per_unit.get(ayo_key)  # {"score", "insufficient_data"}
    IF emotion is not None AND not emotion["insufficient_data"] AND emotion["score"] is not None:
        c6_composite = emotion["score"]
        scores["_C6_source"] = "emotion_expression analyzer (dominantEmotion/intensity transitions, g-250-135)"
    ELSE:
        # Analyzer reports no usable emotion data for this NPC — documented floor
        # (genuine zero-affect OR a pre-EmotionalPerception legacy session).
        c6_composite = 0.3  # zero affect = mild penalty (Robotic floor, not Broken)
        scores["_C6_source"] = "zero-affect floor (emotion_expression insufficient_data)"
    scores["C6"] = round(1.0 + 4.0 * c6_composite, 2)

    # C7 automation (g-250-144: now SOURCED FROM the decision_variety analyzer,
    # not the generic LLM-eval path). The analyzer computes Shannon entropy of
    # the NPC's intent-identity sequence and emits a per_unit C7 score in [0,1]
    # (entropy_bits / log2(8): single-intent NPC -> 0 "perfectly predictable /
    # robotic"; a rich evenly-used repertoire -> 1 "unpredictable"). This closes
    # the rb-1880 class-3 extraction-blind floor: C7 was the ONLY OHS axis with
    # NEITHER an invoked module NOR a canonical block, so it fell through
    # llm_evaluate and floored var~0 in 7/13 sessions (mean-of-means 2.73). C7 is
    # ONE axis — a maximally-random NPC scores high here but is penalised by C10
    # (consistency) and C3 (appropriateness), so the composite stays balanced.
    # Run: cli.py analyze <session> -a decision_variety -o json
    variety = decision_variety_per_unit.get(ayo_key)  # {"score", "insufficient_data"}
    # g-250-150: single-intent-shape-by-design baseline/control modules are NOT
    # EVALUABLE for C7 — exclude from the axis mean (score None) BEFORE the entropy
    # branch. random_picker emits position-only intents, so the randomness lives in
    # the coordinate VALUE, not the intent SHAPE, and intent-identity entropy is
    # structurally 0 no matter how varied the movement is; command_only acts only on
    # chat command (no autonomous decision variety). Both are baseline/control
    # fixtures (the documented "1.39 Robotic" RandomPicker baseline + the command_only
    # test NPCs), NOT shipping characters — scoring their structural c7=0.0 into the
    # mean drags the ship-gate C7 below the 2.5 floor while the agentic NPCs already
    # clear it (session 1778986615496_577: baconBob random_picker c7=0.0 vs jose 0.376
    # / zombie 0.43; excluding baconBob lifts the C7 mean 0.269->0.403, axis 2.07->2.61).
    # Symmetric to the command_only C1/C9 floor-by-design rule + the C8/C12
    # chat_count==0 NOT-EVALUABLE rule; CODIFIES the previously LLM-applied command_only
    # C7 exclusion so it is deterministic. RISK GUARD: control fixtures ONLY — a future
    # SHIPPING NPC on these modules would need its monotony scored, but none exists
    # today (all current random_picker/command_only units are "*Test"/baseline fixtures).
    IF npc_classification[ayo_key].module in ("command_only", "random_picker"):
        scores["C7"] = None
        scores["_C7_source"] = "NOT EVALUABLE (" + npc_classification[ayo_key].module + " single-intent-shape-by-design control — floor-by-design, g-250-150)"
    ELIF variety is not None AND not variety["insufficient_data"] AND variety["score"] is not None:
        c7_composite = variety["score"]
        scores["C7"] = round(1.0 + 4.0 * c7_composite, 2)
        scores["_C7_source"] = "decision_variety analyzer (intent-sequence entropy, g-250-144)"
    ELSE:
        # < 5 intents — NOT EVALUABLE for C7. Exclude from this NPC's OHS (score
        # None) rather than floor as a false zero. A command_only NPC barely
        # invoked is predictable-BY-DESIGN, not behaviorally Broken — the
        # per-character-behavioral-specs command_only C1/C9/C7-floor rule + the
        # C8 chat_count==0 NOT-EVALUABLE pattern + the g-250-129 sentinel
        # contract all say mark None, never a false-positive deficit.
        scores["C7"] = None
        scores["_C7_source"] = "NOT EVALUABLE (decision_variety insufficient_data: < 5 intents)"

    # C8 chat-sparse canonicalization (g-115-859 closure for chat_count in [1, 2]).
    # The chat-rich path (chat_count >= 3) is already canonical via chat_repetition
    # module. For chat_count == 0, C8 is NOT EVALUABLE — exclude from this NPC's
    # OHS rather than LLM-fallback. For chat_count == 1 or 2, define a deterministic
    # signal from the message content itself:
    #   c8_composite = 0.6 * is_contextually_addressed + 0.4 * (1.0 / len_words_clipped)
    # where is_contextually_addressed is 1.0 if the chat message targets another
    # NPC or references session-context entities (proximity-aware), else 0.5.
    # The inverse-length term rewards SHORT relevant utterances over verbose
    # filler (clip len_words to [1, 20] so a single word doesn't max out). This
    # is a tiny-sample bridge; the canonical signal is still chat_repetition once
    # chat_count >= 3.
    IF chat_rep.get("chat_count", 0) in (1, 2):
        chat_msgs = chat_rep.get("messages", [])
        contextual = sum(1 for m in chat_msgs if is_contextually_addressed(m, ayo_key, session_data))
        contextual_rate = contextual / max(1, len(chat_msgs))
        # Inverse-length: clip to [1, 20] words; shorter is more efficient
        avg_words = sum(min(20, max(1, len(m.get("text", "").split()))) for m in chat_msgs) / max(1, len(chat_msgs))
        inverse_len = 1.0 / (avg_words / 20.0 + 0.5)  # normalized [0.5, 1.33] → clip to [0, 1]
        inverse_len = min(1.0, max(0.0, (inverse_len - 0.5) / 0.83))
        c8_sparse_composite = round(0.6 * contextual_rate + 0.4 * inverse_len, 3)
        scores["C8"] = round(1.0 + 4.0 * c8_sparse_composite, 2)
        scores["_C8_source"] = "canonical sparse-chat (contextual/inverse-length composite)"
    # (chat_count >= 3 path handled by chat_repetition module above; chat_count == 0
    # path remains LLM-fallback with explicit "no chat observed" marker.)

    # ── Per-Character Baseline Loader (g-250-145 — was a PHANTOM helper) ─────
    # load_per_character_baseline(ayo_key) is called by C10 (below) and C13
    # (further below) but was NEVER DEFINED — so it silently returned None,
    # pinning baseline_match to the 0.5 neutral default for EVERY NPC
    # (ohs-trend.jsonl: C10 var~0 in 7/13 sessions, mean-of-means 2.76). This is
    # the rb-1880 class-3 extraction-blind floor: the data (per-character specs)
    # is rich, but no loader surfaced it. Definition:
    #   1. Map ayo_key -> spec child-node slug via ROSTER_AYOKEY_TO_SPEC (the
    #      ayoKey column of the per-character-behavioral-specs PARENT node — the
    #      SSOT roster). When characters.json gains/loses an entry, update the
    #      parent node's roster table AND this map together.
    #   2. Read that child node's front-matter for an `expected_intent_distribution`
    #      dict {intent_label: expected_relative_freq} (mirror the line ~1005
    #      `tree-find-node.sh` node-read idiom; read the child .md front-matter).
    #   3. Return an object exposing .expected_distribution (and .character) iff
    #      the field is present and non-empty; else return None.
    # guard-645 / verify-before-assuming: a MISSING field path must surface as
    # spec-absent (None) — never as a silent neutral that downstream reads as a
    # real score. Authoring the per-character distributions is evidence-gated
    # spec-hardening (replay -> observe actual intent mix -> decide expected),
    # NOT this wiring goal: uncovered characters correctly return None and
    # _C10_source marks them "spec-absent (demand-a-spec, class-2)" per the
    # g-250-145 false-positive guard.
    ROSTER_AYOKEY_TO_SPEC = {
        "AjaxKey": "ajax", "RichmondKey": "richmond",
        "baconBob1Test": "bacon-bob", "zombie1Test": "ice-zombie",
        "jose": "jose3731", "testChatNpc": "chat-tester",
    }
    def load_per_character_baseline(ayo_key):
        slug = ROSTER_AYOKEY_TO_SPEC.get(ayo_key)
        IF slug is None:
            return None  # ayo_key not in current roster — spec-absent (unmapped)
        node = read_node_front_matter(f"per-character-behavioral-specs/{slug}")
        dist = (node or {}).get("expected_intent_distribution")
        IF not dist:        # field absent or empty dict
            return None     # spec exists but declares no distribution yet
        return Spec(expected_distribution=dist, character=slug)

    # C10 automation (canonical formula — g-115-859 closure for the personality-
    # consistency axis with NO analyzer module). Composite per g-115-859 spec:
    #   0.50 * intent_concentration_score   (top-intent_pct mapped to bell curve 10%-60%)
    # + 0.50 * baseline_match_score          (cosine sim to per-character intent baseline, or 0.5 if no baseline)
    # intent_concentration_score: top-intent fraction clipped to [0.10, 0.60]
    # mapped linearly to [0, 1] (10% = 0 "no personality", 35% = 0.5 "balanced",
    # 60% = 1 "consistent"). Above 60% caps at 1.0 (extreme consistency is still
    # OK but doesn't add credit); below 10% caps at 0 (uniform-random = no personality).
    # baseline_match_score: cosine similarity to the per-character behavioral spec's
    # expected intent distribution (per-character-behavioral-specs tree node).
    # When no spec exists, set 0.5 (neutral). Both sub-metrics in [0, 1].
    intent_counts = Counter(i.get("intent_label") for i in intents_for_npc if i.get("intent_label"))
    baseline_spec = None   # set in the ELSE branch; init here so _C10_source is always safe
    IF len(intents_for_npc) == 0:
        c10_composite = 0.5  # no intents = not evaluable, neutral
    ELSE:
        top_intent_pct = max(intent_counts.values()) / len(intents_for_npc)
        clipped = min(0.60, max(0.10, top_intent_pct))
        intent_concentration_score = (clipped - 0.10) / 0.50  # [0, 1]
        baseline_spec = load_per_character_baseline(ayo_key)  # may return None
        IF baseline_spec is None:
            baseline_match = 0.5  # no baseline, neutral — labeled in _C10_source below
        ELSE:
            baseline_match = cosine_similarity(intent_counts, baseline_spec.expected_distribution)
        c10_composite = round(0.50 * intent_concentration_score + 0.50 * baseline_match, 3)
    scores["C10"] = round(1.0 + 4.0 * c10_composite, 2)
    # _C10_source: distinguish spec-backed (real baseline-match) from spec-absent
    # (neutral-0.5 placeholder) so a C10 compressed to neutral is never mistaken
    # for a scored one (g-250-145 — the silent default hid this in 7/13 sessions).
    IF len(intents_for_npc) == 0:
        scores["_C10_source"] = "canonical (no intents — not evaluable, neutral)"
    ELIF baseline_spec is not None:
        scores["_C10_source"] = f"canonical spec-backed (concentration + baseline-match vs {baseline_spec.character} spec)"
    ELIF ayo_key in ROSTER_AYOKEY_TO_SPEC:
        scores["_C10_source"] = f"canonical concentration-only (baseline-match=0.5 NEUTRAL — {ROSTER_AYOKEY_TO_SPEC[ayo_key]} spec declares no expected_intent_distribution; demand-a-spec, class-2)"
    ELSE:
        scores["_C10_source"] = f"canonical concentration-only (baseline-match=0.5 NEUTRAL — ayo_key {ayo_key} not in current roster)"

    # C11 automation (Memory Continuity — g-326-26: SOURCED FROM the
    # memory_continuity analyzer, not LLM evaluation). Before this block C11 was
    # the ONLY axis in C1-C13 with no scores[] assignment anywhere: the rubric row
    # pointed at a "C11 automation block" that did not exist (rb-1976 phantom-
    # reference class), so every NPC's C11 came from llm_evaluate applying the
    # row's documented "Baseline-floor 1.0 for fleet" default. That produced a
    # TWO-PRODUCER column in ohs-trend — this module (integers only) mixed with
    # LLM-authored non-integers (1.2/1.5/1.8/2.2/2.4) — which is what made four
    # iterations of chronological C11 comparison unsound (g-326-22..25, guard-1252).
    #
    # SCALE TRAP — READ BEFORE EDITING. MemoryContinuityAnalyzer._score returns an
    # INTEGER 1-5 by literal return; it does NOT return a [0,1] composite. Do NOT
    # wrap it in the `1.0 + 4.0 * composite` transform used by C1/C2/C5/C6/C7/C10
    # — that maps the floored score 1 to 5.0 and would silently invert a dead axis
    # into a perfect one. Assign it directly, exactly as C13 does for its banded
    # score. Cheap check: a C11 that reads 5.0 across the whole fleet is this bug,
    # not good memory.
    #
    # A 1.0 here is a REAL measurement, not a fallback: both of the module's
    # routes to utilization>0 are currently dead (g-326-24) — the inference path
    # needs a PrivateNote timestamped before timeline.start_timestamp
    # (= frames[0].timestamp), which in-session notes never satisfy, and
    # references_prior_event_id is absent from the session tree. Per rb-1986 an
    # axis newly wired off a false floor normally shows an instrument-driven step
    # UP; C11 is the documented EXCEPTION — it stays at 1.0 and only the PRODUCER
    # changes. Annotate the producer switch in the ohs-trend row's scoring_context
    # so pre/post rows are not read as a behavioral trend (rb-1986 handling (1)).
    # Run: cli.py analyze <session> -a memory_continuity -o json
    # SHAPE NOTE (verified by running the module, g-326-26): memory_continuity's
    # per_unit entries are {"unit_key", "axis": "C11", "score": <int>} — they do
    # NOT carry an `insufficient_data` flag, unlike social_proximity/emotion_
    # expression. The lookup above uses .get() so the missing key degrades to
    # None (falsy) and the guard below still reads correctly; do not "fix" that
    # to ["insufficient_data"] or every NPC will KeyError.
    memory = memory_continuity_per_unit.get(ayo_key)  # {"score", "insufficient_data": None}
    IF memory is not None AND not memory["insufficient_data"] AND memory["score"] is not None:
        # ALREADY on the 1-5 scale — direct assign, no 1+4*x transform (see trap above).
        scores["C11"] = float(memory["score"])
        scores["_C11_source"] = "memory_continuity analyzer (PrivateNotes utilization + consolidated-memory hit rate, integer 1-5; wired g-326-26)"
    ELSE:
        # Module returned no usable per_unit row for this NPC. Fall back to LLM
        # evaluation, but LABEL it so a defaulted C11 is never mistaken for a
        # measured one (the guard-645 source-label discipline that C10 uses).
        scores["C11"] = llm_evaluate("C11", ayo_key, session_data)
        scores["_C11_source"] = "LLM evaluation (memory_continuity returned no per_unit row) — NOT analyzer-sourced"

    # C13 automation (Contextual Reaction Appropriateness — g-250-123, design
    # g-250-122 Gap D in client-signal-reconciliation.md). SIGNED reaction-
    # appropriateness axis operationalizing asp-224 S2 (Contextual Reactivity,
    # P0 76% demand) WITH charlie's restraint correction: CREDIT warranted
    # reactions, PENALIZE spurious ones. Offline; reuses replay signals already
    # read for C3 (dialogue/latency), C6 (BarStateService 5-dim), BT-intent, and
    # gaze-orientation — no new runtime instrumentation.
    #   R  = salient context-change events the NPC SHOULD react to:
    #        (a) a player enters the 27-stud social radius (ChatChannel speak-range)
    #            AND addresses/faces the NPC (gaze/facing toward NPC);
    #        (b) a perceived world-state change the character's spec marks salient
    #            (combat nearby, a relevant WorldFeature, a time/weather transition
    #            the character cares about);
    #        (c) a tracked player-state change the character would notice.
    #        Salience is classified PER-CHARACTER via the per-character behavioral
    #        specs subtree (a guard reacts to weapons, a merchant to customers).
    #   Wr = of those R events, how many drew a visible reaction within
    #        react_window_ticks — a dialogue/chat intent (C3 signal), a
    #        BarStateService / EmotionalPostureOverlay transition (C6 signal), a
    #        BT-intent change, or a gaze-orientation shift toward the stimulus.
    #   Sp = visible reactions fired at NON-salient stimuli (e.g. a greeting seed
    #        at an incidental passer-by who never addressed the NPC while it was
    #        mid-task — the canonical merchant_07 restraint failure).
    #   VisibleReactions = Wr + Sp.
    # CRA = clamp01( Wr/max(R,1) - 0.5 * Sp/max(VisibleReactions,1) )  (0-1)
    # BANDED to 1-5 per the g-250-122 design (NOT the 1.0 + 4.0*composite linear
    # transform the other axes use): 5: CRA>=0.80 ; 4: 0.60-0.80 ; 3: 0.40-0.60 ;
    # 2: 0.20-0.40 ; 1: CRA<0.20. Score 1 catches BOTH failure modes — a dead NPC
    # that ignores salient context AND a flooding NPC that fires spurious seeds.
    # ANALYZER MODULE PENDING (like C11/C12): until a dedicated
    # ContextualReactionAnalyzer module lands, the LLM computes R/Wr/Sp from the
    # already-loaded intents + frame data + per-character specs.
    react_window_ticks = 30   # reaction must fire within ~this of the salient event
    char_spec = load_per_character_baseline(ayo_key)  # may return None (no spec)
    salient_events = classify_salient_events(ayo_key, all_intents, frame_data,
                        social_radius_studs=27, char_spec=char_spec)
    visible_reactions = collect_visible_reactions(ayo_key, all_intents, frame_data)
    # ^ dialogue/chat intents + BarStateService/EmotionalPostureOverlay transitions
    #   + BT-intent changes + gaze-orientation shifts attributable to ayo_key.
    R = len(salient_events)
    Wr = count(e for e in salient_events
               if reaction_within_window(e, visible_reactions, react_window_ticks))
    Sp = count(vr for vr in visible_reactions
               if not matches_any_salient_event(vr, salient_events, react_window_ticks))
    VisibleReactions = max(len(visible_reactions), 1)
    IF R == 0 AND len(visible_reactions) == 0:
        # No salient context to react to AND no reactions fired — restraint is not
        # evaluable (distinct from a dead NPC, which has R>0 it ignored). Neutral.
        scores["C13"] = llm_evaluate("C13", ayo_key, session_data)
        scores["_C13_source"] = "LLM (no salient context + no reactions — CRA not evaluable)"
    ELSE:
        cra = clamp01(Wr / max(R, 1) - 0.5 * Sp / max(VisibleReactions, 1))
        c13_band = 5 if cra >= 0.80 else 4 if cra >= 0.60 else 3 if cra >= 0.40 else 2 if cra >= 0.20 else 1
        scores["C13"] = float(c13_band)   # already on the 1-5 scale (BANDED — not 1+4*composite)
        scores["_C13_source"] = "canonical (CRA signed reaction-appropriateness, banded)"
        scores["_C13_cra"] = round(cra, 3)   # retain the 0-1 sub-signal for diagnostics

    ohs = sum(weights[c] * scores[c] for c in C1..C13)  # OHS_v13 — sum of v13 weights = 1.00; canonical weights in humanness-composite-scoring.md tree node
    npc_scores[ayo_key] = {
        scores,
        ohs,
        label: interpret_ohs(ohs),
        chat_repetition: chat_rep,
        steering_quality: sq_data,
        path_variety: pv_data,
        intent_appropriateness: ia_data,
    }
```

Aggregate OHS calculation: exclude INACTIVE_BY_DESIGN and SKIPPED_NOT_SPAWNED NPCs
from `overall_ohs` (the mean across scored NPCs) so session-wide OHS is not dragged
down by uninvoked command_only NPCs or never-spawned roster NPCs. Report their counts
separately under `inactive_by_design_count` / `skipped_not_spawned_count`, and always
report `roster_coverage` (Step 2.5) beside `overall_ohs` — a high present-NPC OHS with
low roster coverage is a spawn-pipeline problem, not a cognition win (g-318-54).

### OHS Interpretation Scale

| OHS Range | Label |
|-----------|-------|
| 1.0 - 1.9 | Broken |
| 2.0 - 2.4 | Robotic |
| 2.5 - 2.9 | Scripted |
| 3.0 - 3.4 | Competent |
| 3.5 - 3.9 | Believable |
| 4.0 - 4.4 | Human-like |
| 4.5 - 5.0 | Uncanny |

## Step 3.5: Per-Axis Aggregation + Zombie-Unit Detection

Session-level mean hides single-actor failure. One NPC scoring 0.2 on C1 while four
score 3.5 reads as OHS ~2.9 "Competent" but actually means one NPC is catastrophically
broken. Operationalizes rb-455 (generalized from rb-451 unit-2979 cell-success pattern).

```
# Build per-axis aggregation from the scored NPCs in npc_scores.
# Inputs: npc_scores (keyed by ayo_key) with scores dict (C1..C13, 1-5 scale)
# Exclude INACTIVE_BY_DESIGN and SKIPPED_NOT_SPAWNED NPCs — their scores are null
# (never-spawned NPCs are a roster_coverage fact, not an axis observation; g-318-54).
scored_npcs = [(k, data) for k, data in npc_scores.items() if data.label not in ("INACTIVE_BY_DESIGN", "SKIPPED_NOT_SPAWNED")]

per_axis = {}   # {criterion: {mean, min, variance}}
FOR EACH criterion in C1..C13:
    values = [data.scores[criterion] for k, data in scored_npcs if data.scores[criterion] is not None]
    IF len(values) == 0:
        per_axis[criterion] = {mean: null, min: null, variance: null, n: 0}
        continue
    mean = sum(values) / len(values)
    mn = min(values)
    var = sum((v - mean)**2 for v in values) / len(values)  # population variance
    per_axis[criterion] = {
        mean: round(mean, 3),
        min: round(mn, 3),
        variance: round(var, 3),
        n: len(values),
    }

# Zombie detection: any unit whose score on any axis drops below 0.5 * session_mean
# on that axis. Emits one entry per (unit, axis) violation — a unit broken on
# multiple axes surfaces multiple entries so the viewer sees breadth of failure.
zombie_units = []
FOR EACH ayo_key, data in scored_npcs:
    FOR EACH criterion in C1..C13:
        axis_score = data.scores[criterion]
        axis_mean = per_axis[criterion].mean
        IF axis_score is None OR axis_mean is None OR axis_mean == 0:
            continue
        threshold = 0.5 * axis_mean
        IF axis_score < threshold:
            zombie_units.append({
                unit_key: ayo_key,
                axis: criterion,
                score: round(axis_score, 3),
                session_mean: round(axis_mean, 3),
                threshold: round(threshold, 3),
            })

# Attach to the outgoing analysis data so Step 6's report + Step 7's JSONL
# emit can surface both per_axis stats and the zombie list.
session_aggregation = {
    per_axis: per_axis,
    zombie_units: zombie_units,
}
```

A unit appearing in zombie_units on ANY axis means the overall OHS average is
misleading for that session — one (or more) actors are catastrophically failing.
Downstream reports must surface zombies rather than hiding them in the mean.

## Step 4: Compare Against Baselines

```
node=$(bash core/scripts/tree-find-node.sh --text "behavioral-baselines" --leaf-only --top 1)
Read {node.file}

FOR EACH ayo_key, data in npc_scores:
    IF data.label == "INACTIVE_BY_DESIGN": continue  # no scores to compare
    module_type = lookup_intelligence_module(ayo_key, summary)
    baseline = baselines.get(module_type, baselines.get("aspirational"))

    data.baseline_comparison = {}
    FOR EACH criterion, score in data.scores:
        IF baseline has criterion:
            delta = score - baseline[criterion]
            data.baseline_comparison[criterion] = {score, baseline: baseline[criterion], delta}

    data.ohs_delta = data.ohs - baseline.ohs if baseline.ohs else null
```

## Step 5: Generate Improvement Goals

```
FOR EACH ayo_key, data in npc_scores:
    IF data.label == "INACTIVE_BY_DESIGN": continue  # no goals generated for idle-by-design NPCs
    IF data.label == "SKIPPED_NOT_SPAWNED": continue # roster gap has its own spawn-fix lane (g-318-53) — per-NPC cognition goals would misdiagnose absence as brokenness

    # Critical deficit: OHS below 2.5
    IF data.ohs < 2.5:
        lowest = min(data.scores, key=data.scores.get)
        echo '{"title":"Investigate: {ayo_key} critical behavioral deficit in {lowest} (OHS {data.ohs:.1f})","description":"...","priority":"HIGH","category":"npc-cognition","participants":["alpha"],"origin_signal":"investigate:ohs-deficit-{ayo_key}-{lowest}"}' | bash core/scripts/aspirations-add-goal.sh --source world asp-226

    # Broken dimension: any criterion = 1
    FOR EACH criterion, score in data.scores:
        IF score == 1:
            echo '{"title":"Fix: {ayo_key} broken {criterion} dimension (score 1/5)","description":"...","priority":"HIGH","category":"npc-cognition","participants":["alpha"],"origin_signal":"unblock:ohs-broken-{ayo_key}-{criterion}"}' | bash core/scripts/aspirations-add-goal.sh --source world asp-226

    # Significant regression from baseline
    IF data.ohs_delta and data.ohs_delta < -0.3:
        echo '{"title":"Investigate: {ayo_key} OHS regression ({data.ohs_delta:+.1f})","description":"...","priority":"MEDIUM","category":"npc-cognition","participants":["bravo"],"origin_signal":"investigate:ohs-regression-{ayo_key}"}' | bash core/scripts/aspirations-add-goal.sh --source world asp-226
```

## Step 6: Produce Structured Report

```yaml
behavioral_analysis_report:
  session: {session_key}
  date: "YYYY-MM-DD"
  npcs_evaluated: N           # count of NPCs actually scored (excludes INACTIVE_BY_DESIGN + SKIPPED_NOT_SPAWNED)
  inactive_by_design_count: N # command_only NPCs with zero chat events — skipped
  skipped_not_spawned_count: N # autonomous roster NPCs with zero session presence — roster gap, not scored (g-318-54)
  roster_coverage: "3/7"      # present autonomous / roster autonomous (Step 2.5) — ALWAYS reported beside overall_ohs
  overall_ohs: X.X            # mean across npcs_evaluated only
  ohs_label: "Competent"
  per_axis_stats:             # from Step 3.5 session_aggregation.per_axis
    C1: {mean: X.X, min: X.X, variance: X.X, n: N}
    C2: {mean: X.X, min: X.X, variance: X.X, n: N}
    # ... through C10
  zombie_units:               # from Step 3.5 session_aggregation.zombie_units
    - {unit_key: "...", axis: "C1", score: X.X, session_mean: X.X, threshold: X.X}
    # may be empty list when no zombies detected
  per_npc:
    ayo_key:
      module: aspirational
      ohs: X.X
      label: "Competent"            # or "INACTIVE_BY_DESIGN" / "SKIPPED_NOT_SPAWNED" when a gate fired
      gate: "SCORE"                  # SCORE | INACTIVE_BY_DESIGN | SKIPPED_NOT_SPAWNED
      chat_events_received: N        # only present for command_only NPCs
      generator_state: "ACTIVE"      # ACTIVE | SELECTION_STARVED | GENERATOR_IDLE (Step 2.6, g-250-46)
      intents_by_source:             # Step 2.6 — per-source intent counts (diagnostic routing)
        aspiration: N
        module: N
        proactive: N
        current: N
        chat: N
        survival: N
        unknown: N                   # records lacking a "source" field
      intents_total: N               # sum of intents_by_source values
      cells_completed: N             # from summary; denominator for ratio
      cell_per_intent_ratio: 0.XXXX  # cells/intents (0.0 when intents_total == 0)
      scores: {C1: X, C2: X, ..., C10: X}   # null when INACTIVE_BY_DESIGN
      lowest: "C2"                          # null when INACTIVE_BY_DESIGN
      highest: "C4"                         # null when INACTIVE_BY_DESIGN
      vs_baseline: {C1: +0.3, C2: -0.1, ...}
  improvement_goals_created: N
  modules_available: [stuck_detector, movement, ...]
  modules_missing: [goal_diversity, social_proximity, ...]
```

Note the distinction: `per_npc.*.scores` holds the 1-5 OHS-scale per-NPC
criteria, whereas `per_axis_stats` holds session-level moments of those same
scores grouped by axis. `zombie_units` surfaces any (unit, axis) pair where
the NPC's score dropped below `0.5 * session_mean` on that axis (rb-455 /
rb-451 threshold). A non-empty `zombie_units` list means overall_ohs is
misleading and downstream readers MUST treat the session as having
single-actor failure, not uniform mediocrity.

## Step 7: Post Summary + Encode

```
# Post to coordination board
echo "Behavioral analysis: session {session_key}, {N} NPCs, avg OHS {overall_ohs:.1f} ({label})" | board-post.sh --channel findings --type finding --tags behavioral-analysis,{session_key}

# Append to OHS time-series (north-star metric stub — see plan: alter-bravo-a-little).
# The ohs-trend-add.sh wrapper is a planned follow-up; until then, append raw JSONL
# directly. Bravo reads this file every idle cycle (Step 0 of its idle playbook).
#
# Schema (extended for per-unit aggregation retrofit, g-226-60 chain / rb-455;
# infrastructure-correlation fields added g-115-185):
#   timestamp              : ISO 8601 write time
#   session_id             : session_key
#   ohs_overall            : session-wide mean OHS (excludes INACTIVE_BY_DESIGN)
#   per_axis               : {C1..C13: mean}                          -- LEGACY field, kept for readers (extended C10→C11→C12→C13 by g-004-05 / g-316-16 / g-250-123)
#   per_axis_stats         : {C1..C13: {mean, min, variance, n}}      -- from session_aggregation.per_axis
#   per_unit               : [{unit_key, axis, score}]                -- concatenation of all analyzer per_unit arrays
#   zombie_units           : [{unit_key, axis, score, session_mean, threshold}] -- from session_aggregation.zombie_units
#   source_goal_ids        : originating goal IDs (audit trail)
#   bitnet_health_fraction : fraction of session duration BitNet was healthy (0.0-1.0, or null)
#                            Source: operator-health metrics correlated to session time window.
#                            Populate when AyoAI-Processor-Operator has data for the session's
#                            [start_time, end_time] window; null when unfetched.
#                            Enables resolution of hypothesis
#                            2026-04-21_llm-task-selection-bitnet-health-threshold (g-115-185).
#   llm_flag_state         : {"on"|"off"|"mixed"|"unknown"}           -- LLM task-selection A/B flag state
#                            Source: runtime env var or character-definition metadata
#                            (currently not persisted to session artifacts — requires
#                             Environment-Server instrumentation; scoping pending).
#                            Default "unknown" until instrumentation lands.
#   crash_prefix           : bool (g-318-24) — true when this row scores ONLY the
#                            healthy pre-degradation prefix of a CRASHED session
#                            (Step 1.5 gate=CLEAR_PREFIX). false for normal sessions.
#   scored_window          : null for normal sessions; {start_ts, onset_ts, prefix_sec,
#                            onset_method} (g-318-24/g-318-25) when crash_prefix=true — the
#                            epoch-ms window the score covers. onset_method is
#                            "stream_stall" (StreamingUpdates-stuck crash, boundary from
#                            SavedAyoStreamUpdates) or "latency_onset" (BitNet-degradation,
#                            boundary from _llmt). Trend consumers: a crash_prefix row is a
#                            truncated window; C11/C12/C13 are LOW-SAMPLE on it, not a
#                            regression — do NOT read a prefix-row dip as a quality drop.
#
# Backward compat: the legacy `per_axis` stays shallow (axis → mean) so bravo's
# idle-playbook reader and any ohs-trend consumers keep working unmodified.
# New consumers should prefer `per_axis_stats` for moments beyond the mean.
# The new fields are OPTIONAL — readers must tolerate missing keys on pre-g-115-185 rows.
#
# Best-effort population (writer logic):
#   1. bitnet_health_fraction: scan AyoServerEnvironment_OnStartup + any
#      session logs in {session_dir}/logs/ for BitNet health timestamps
#      overlapping the session window. Compute fraction = healthy_duration_ms /
#      total_session_ms. If no data: null.
#   2. llm_flag_state: grep CharacterDefinitions.jsonl + AyoServerEnvironment_OnStartup
#      for tokens {"LlmTaskSelection","useLLMTaskSelector","taskSelector","Strategy4"};
#      if present → inspect value; if absent → "unknown".
# --- Compute optional infrastructure-correlation fields (g-115-185) ---
#
# llm_flag_state: best-effort extract from session artifacts.
#   Grep CharacterDefinitions.jsonl, AyoServerEnvironment_OnStartup.json, and
#   ServerGlobals.json for tokens matching the LLM A/B flag.
#
# g-318-63 — "unsampled" is now the default, NOT "unknown". These two are
# different claims and collapsing them cost a real diagnosis:
#   unsampled = we looked in the canonical artifacts and the flag is not there
#               (the CONFIRMED current state per g-115-185 — Environment-Server
#               does not persist it yet). A KNOWN, explainable absence.
#   unknown   = indeterminate; reserved for a genuine read failure (artifact
#               missing/unreadable), i.e. we could not even look.
# Why it matters: llm_flag_state read "unknown" on 29/32 rows, and per
# guard-1091 a field that ALWAYS reads unknown is not weak evidence, it is
# ZERO evidence. Because "unknown" is also what a read failure looks like,
# nobody could tell "the pipeline never captured this" from "this session's
# artifacts were unreadable" — so the blind spot read as ordinary missing data
# and got treated as "probably fine". Naming the absence makes it actionable:
# a wall of "unsampled" points at the Environment-Server persistence gap,
# while any "unknown" now means a real read problem worth chasing.
llm_flag_state="unsampled"
_llm_artifact_readable=0
for f in "{session_dir}/CharacterDefinitions.jsonl" \
         "{session_dir}/AyoServerEnvironment_OnStartup.json" \
         "{session_dir}/ServerGlobals.json"; do
    [ -f "$f" ] || continue
    _llm_artifact_readable=1
    if grep -iqE 'LlmTaskSelection|useLLMTaskSelector|llm_task_selection|taskSelectionStrategy' "$f" 2>/dev/null; then
        # Found a flag token — parse the nearest value.
        # Placeholder: set to "on" if any truthy value appears near the token,
        # otherwise "off". Refine when canonical field name is known.
        llm_flag_state="on"
        break
    fi
done
# No artifact was even readable -> we could not look. That is the ONLY case
# that earns "unknown" (see the unsampled-vs-unknown split above).
[ "$_llm_artifact_readable" -eq 1 ] || llm_flag_state="unknown"
#
# bitnet_health_fraction: fraction [0,1] of the SCORED window where BitNet was
# healthy. Computed from world/bitnet-health-samples.jsonl, the timestamped
# series appended by world/scripts/probe-bitnet-prod.sh (g-318-63).
#
# Why a new series rather than reading infra-health.yaml: that file stores only
# last_success / last_failure / consecutive_failures — a point-in-time
# SNAPSHOT. A fraction over a window cannot be integrated from a signal that
# keeps no history, so the previous "wire it up" framing was not achievable;
# the series had to exist first.
#
# THREE-WAY result, and the distinction is the whole point (guard-1091):
#   null  -> NO samples overlap the window. Not "0% healthy" — no coverage.
#            Expected when the session is shorter than one probe interval, or
#            predates this series. Backfill is deliberately not attempted.
#   0.0   -> samples EXIST and every one reported unhealthy. Real evidence.
#   0<f<=1-> healthy_samples / total_samples in-window.
# Emitting 0.0 for the no-coverage case would manufacture the exact false
# certainty this goal was filed to remove.
#
# Window substitution (both are "%Y-%m-%dT%H:%M:%S" strings):
#   IF scored_window_json != "null"  (CLEAR_PREFIX / crash-truncated session)
#       -> use prefix_window[0] and prefix_window[1]. Health OUTSIDE the scored
#          prefix is irrelevant: the axes were scored only on the prefix, so
#          including post-crash health would attribute the wrong conditions to
#          the score.
#   ELSE
#       -> use session_start and session_end (Step 1, lines ~238-241; derived
#          from the session-key epoch prefix, end = start + duration + 60s).
# Both blank/unparseable -> the snippet prints null (no false coverage).
BN_SAMPLES="$WORLD_DIR/bitnet-health-samples.jsonl" \
BN_WIN_START="{scored_window_start_else_session_start}" \
BN_WIN_END="{scored_window_end_else_session_end}" \
bitnet_health_fraction_json=$(py -3 -c '
import json, os, datetime
path = os.environ["BN_SAMPLES"]
def parse(s):
    try: return datetime.datetime.strptime(s.strip(), "%Y-%m-%dT%H:%M:%S")
    except Exception: return None
start, end = parse(os.environ.get("BN_WIN_START","")), parse(os.environ.get("BN_WIN_END",""))
if start is None or end is None or not os.path.exists(path):
    print("null"); raise SystemExit
tot = ok = 0
with open(path, encoding="utf-8") as fh:
    for line in fh:
        line = line.strip()
        if not line: continue
        try: rec = json.loads(line)
        except Exception: continue          # a corrupt line must not poison the count
        ts = parse(rec.get("ts",""))
        if ts is None or not (start <= ts <= end): continue
        tot += 1
        ok += 1 if rec.get("healthy") else 0
print("null" if tot == 0 else "%.4f" % (ok / tot))
' 2>/dev/null || echo "null")

# Build per_unit_json deterministically from scored_npcs (g-307-35).
# Concatenate per-NPC per-criterion scores for ALL evaluated NPCs across
# C1..C13. This is the SINGLE source for per_unit emission — do NOT also
# concatenate analyzer-module per_unit arrays (analyzers emit RAW 0-1
# scores which would mix with the 1-5 OHS scale producing inconsistent
# data). INACTIVE_BY_DESIGN NPCs already filtered upstream (Step 3.5).
# Pre-fix evidence: 14/18 ohs-trend.jsonl sessions had per_unit C5
# missing because the LLM's interpretation of "concatenation" varied
# session-to-session — some only included analyzer-backed axes
# (C1/C3/C4/C9), others uniformly emitted all 12. Deterministic
# construction below closes that gap.
per_unit = []
for ayo_key, data in scored_npcs:
    # C13 included since g-250-123 (v13) — this tuple lagged at C12 while the
    # schema comment above promised C1..C13; recent rows carried C13 only
    # because executors followed the comment (g-318-24 validation, 2026-07-11).
    for criterion in ("C1","C2","C3","C4","C5","C6","C7","C8","C9","C10","C11","C12","C13"):
        score = data.scores.get(criterion)
        if score is not None:
            per_unit.append({
                "unit_key": ayo_key,
                "axis": criterion,
                "score": round(float(score), 2),
            })
per_unit_json = json.dumps(per_unit)

# Build per_axis_json (LEGACY shallow shape: {C1..C13: mean}) and
# per_axis_stats_json (full shape: {C1..C13: {mean, min, variance, n}})
# deterministically from session_aggregation.per_axis (Step 3.5).
# Mirror-symmetric with the per_unit_json block above (g-307-36, sibling
# fix for g-307-35 echo-template-binding-determinism rb-1182). Without
# explicit binding the same per_unit-style drift manifests here — each
# bash placeholder must be bound by upstream Python before the
# ohs-trend.jsonl echo below to keep schema stable across sessions.
per_axis = {}        # legacy shape — axis → mean only (kept for compat)
per_axis_stats = {}  # full shape — axis → {mean, min, variance, n}
for criterion in ("C1","C2","C3","C4","C5","C6","C7","C8","C9","C10","C11","C12","C13"):
    stats = session_aggregation.per_axis.get(criterion, {})
    per_axis[criterion] = stats.get("mean")           # null when n==0
    per_axis_stats[criterion] = {
        "mean":     stats.get("mean"),
        "min":      stats.get("min"),
        "variance": stats.get("variance"),
        "n":        stats.get("n", 0),
    }
per_axis_json       = json.dumps(per_axis)
per_axis_stats_json = json.dumps(per_axis_stats)

# Build zombie_units_json from session_aggregation.zombie_units (Step 3.5).
# Empty list when no zombies — distinct from missing data; readers can rely
# on shape stability (always a JSON array, never absent).
zombie_units_json = json.dumps(session_aggregation.zombie_units)

# g-318-24 salvage-score markers. For a normal (CLEAR / FLAGGED) session:
# crash_prefix=false, scored_window=null. For a CLEAR_PREFIX session (Step 1.5):
# crash_prefix=true and scored_window = the prefix_window dict {start_ts, onset_ts,
# prefix_sec} so trend consumers know this row scores ONLY the healthy pre-crash
# prefix (truncated window — C11/C12/C13 are low-sample, NOT regressions). Both
# OPTIONAL: readers tolerate missing keys on pre-g-318-24 rows.
if session_gate == "CLEAR_PREFIX":
    crash_prefix_json = "true"
    scored_window_json = json.dumps(prefix_window)
else:
    crash_prefix_json = "false"
    scored_window_json = "null"

# g-318-46 legitimacy stamp. Persists the gate verdicts this skill ALREADY
# computed (Step 1.5 session_gate; Step 2.45 envelope numbers) into the row so
# trend consumers can filter illegitimate rows inline instead of cross-
# referencing external investigations (the g-001-70 2026-07-17 confound: a
# clientless session passes crash+envelope gates, gets scored, and floors the
# social/interaction axes — the tier-crossing looked like a regression).
# EVIDENCE-shaped, not verdict-shaped: chat_count > 0 does NOT prove a human
# client (NPC-NPC chat counts — rb-196), so client_evidence records what was
# observed; the authoritative no-client discriminator lands with g-326-14 and
# can then upgrade this field. OPTIONAL field: readers tolerate missing keys
# on pre-g-318-46 rows (same contract as the g-318-24 markers above).
legitimacy = {
    "session_gate": session_gate,   # CLEAR | FLAGGED | CLEAR_PREFIX (Step 1.5)
    "envelope": {"duration_sec": duration_sec, "cell_count": cell_count, "chat_count": chat_count},  # Step 2.45
    "client_evidence": "chat-activity" if chat_count > 0 else "none-observed",
}
legitimacy_json = json.dumps(legitimacy)

# HARDENING (g-318-33): emit the echo below VERBATIM -- all fields, timestamp FIRST.
# Do NOT hand-reconstruct this JSON. Session 1783154504176_63 was appended WITHOUT a
# timestamp (an execution deviation from this template), which crashed ohs-trend.py
# until the reader was guarded (rb-2850). The $(date ...) stamp is MANDATORY on every
# row -- same echo-template-binding-determinism class as g-307-36 (per_axis/per_unit
# drift): the WHOLE row must be emitted deterministically, not just the aggregates.
WORLD_DIR=$(source core/scripts/_paths.sh && echo "$WORLD_DIR")
echo "{\"timestamp\":\"$(date +%Y-%m-%dT%H:%M:%S)\",\"session_id\":\"{session_key}\",\"ohs_overall\":{overall_ohs},\"per_axis\":{per_axis_json},\"per_axis_stats\":{per_axis_stats_json},\"per_unit\":{per_unit_json},\"zombie_units\":{zombie_units_json},\"source_goal_ids\":[\"{goal_id}\"],\"bitnet_health_fraction\":${bitnet_health_fraction_json},\"llm_flag_state\":\"${llm_flag_state}\",\"crash_prefix\":${crash_prefix_json},\"scored_window\":${scored_window_json},\"legitimacy\":${legitimacy_json}}" >> "$WORLD_DIR/ohs-trend.jsonl"

# Add experience record
experience-add.sh with: behavioral analysis report

# Optionally update baselines
IF args.update_baselines:
    Edit behavioral-baselines.md with new per-module-type metrics
```

## Graceful Degradation

When new analysis modules (g-226-04) are NOT yet implemented:
- C1, C4, C9: Fully computable from existing modules (movement, stuck, summary)
- C6 (emotion_expression, g-250-135), C7 (decision_variety, g-250-144): now
  SOURCED from analyzer modules with canonical blocks — no longer LLM-fallback
- C2, C3, C5: Fall back to LLM evaluation by stepping through frame data
- C8, C10: canonical blocks where data permits (C8 chat_repetition / sparse
  formula; C10 concentration + spec-backed baseline-match, g-250-145), else LLM

The skill is USABLE immediately. Automated modules improve precision but are not required.

## Chaining

- **Called by**: `/aspirations` loop (via recurring goal g-226-08), user invocation
- **Calls**: `cli.py` (state-replay), `tree-find-node.sh`, `aspirations-add-goal.sh`, `board-post.sh`, `experience-add.sh`
- **Reads**: npc-humanness-rubric tree node, behavioral-baselines tree node, session data
- **Writes**: Experience records, improvement goals, baseline updates (optional)

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal call is `experience-add.sh`, `aspirations-add-goal.sh`, or `board-post.sh`
— whichever fires last in the flow. Never end with a text summary of scores or findings.
