---
name: notify-user
forged: true
forged_by: alpha
forged_date: "2026-03-28"
description: "Use whenever the agent needs to send a user-facing notification — completion reports, new aspirations created, blocker escalations, decisions that need user review, forged-skill announcements, decomposition handoffs, or any other event the user should see. Fires when core or domain skills instruct to 'notify the user', 'reach out to the user', 'alert the user', 'inform the user', or 'email the user'. Sends email via agents@ayoai.com with agent self-identification; gracefully falls back to pending-questions or participant goals when email is unavailable. Prefer this skill over composing ad-hoc notifications — it handles rate limiting, self-identity, and fallback cascade."
user-invocable: false
# Internal category/event aliases only. User-facing natural-language phrases
# ("notify the user", "reach out to the user", …) live in world/forged-skills.yaml
# and are the single source of truth for prose-driven skill resolution. Do not
# duplicate them here — capability-gate.py merges both sources.
triggers: [send-info-alert, send-error-alert, user-notification, email-alert, blocker-notification, decision-needed-notification, completion-notification, aspiration-created-notification, forge-announcement, decomposition-handoff]
tools_used: [Bash]
companion_scripts: [core/scripts/notify-build-payload.py, world/scripts/email-send.sh]
conventions: [secrets, infrastructure]
minimum_mode: assistant
revision_id: "skill-bootstrap-notify-user-482bd0"
previous_revision_id: null
---

# /notify-user — User Notification

Central notification skill. Use this whenever you need to notify the user, reach out
to the user, or alert the user about something. Sends email via agents@ayoai.com with
self-identification, structured templates, and graceful fallback when email is unavailable.

## Companion Scripts

- `py -3 core/scripts/notify-build-payload.py --agent X --category Y --subject Z --message-file P` — Deterministic SendInfoAlert / SendErrorAlert JSON builder; pipes to stdout. Refuses payloads with empty Body (silent-empty-email guard, 2026-05-20).
- `world/scripts/email-send.sh` — Send info email (default) or `--error` for error alert. Consumes the BUILDER's stdout only: its provenance gate (g-115-2186) refuses hand-constructed JSON lacking the `XPayloadProvenance` stamp the builder adds (stripped again before the transport).

## Step 0: Load Conventions

Bash: `load-conventions.sh` with each name from the `conventions:` front matter.
Read only the paths returned (files not yet in context). If output is empty, proceed.

## Step 1: Pre-flight

```
# These env checks are NOT redundant with email-send.sh — the Lambda invoke is
# async (--invocation-type Event), so missing credentials produce exit 0 but
# silently fail to deliver. This pre-check is the only place we catch that.
Bash: bash core/scripts/env-read.sh has AGENTS_GROUP_EMAIL
Bash: bash core/scripts/env-read.sh has USER_EMAIL
IF either missing → skip to Step 4 (Fallback Cascade)
```

## Step 1.5: Approval-Request Gate

Third chokepoint in the capability-routing trio. The sibling gates fire at
`participants:[user]` (CREATE_BLOCKER Step 2.6) and `defer_reason`
(`aspirations.py cmd_update_goal`). This one catches the third leak path:
agents bypassing both by just emailing the user to ask for something the
agent can do itself.

Rule source: `.claude/rules/probe-before-defer.md` §Enforcement table,
row 3 ("Notification time").

```
# Only gate info / update / decision-needed.
# `blocker` AND `completion` are exempt -- both are STATUS reports, not
# approval requests, so the approval-request gate does not apply:
#   - `blocker` goes through CREATE_BLOCKER's capability-gate already;
#     double-gating would block legitimate infrastructure alerts.
#   - `completion` reports retrospectively state what was done and often
#     name a remaining manual step ("user must open Roblox Studio") that
#     matches an approval pattern ("user must") AND a capability keyword
#     (roblox/studio), a systematic FP that made delta's completion
#     emails undeliverable (g-115-1617). A completion report is not the
#     agent asking permission to do agent-work; it is a status report.
IF category not in ("blocker", "completion"):
    # Heuristic pre-filter: skip the gate unless the message contains
    # approval-request language. Avoids running the gate on benign
    # "completion report" emails that might happen to mention "push"
    # or "commit" in passing.
    # SSOT for additions to this list: world/conventions/capability-routing.md
    # → "User-Approval Narrative Patterns" (Forbidden narrative framings table).
    # Keep the explicit-approval block + the narrative-framing block in sync.
    # New framings discovered in audits MUST be appended to BOTH this list
    # and the capability-routing.md table; future maintainers should grep
    # both locations before declaring the catalog complete.
    approval_patterns = [
        # ─ Explicit approval-request language (original list) ─
        "please approve", "approval needed", "awaiting your approval",
        "need you to approve", "can you approve",
        "awaiting user approval", "waiting for you to",
        "please run", "could you run", "can you run",
        "please commit", "please push", "please deploy",
        "please start", "need you to commit", "need you to push",
        "need you to deploy", "need you to run", "need you to start",
        # ─ Narrative-framing variants (g-255-03; mirror of
        #   capability-routing.md "Forbidden narrative framings" table) ─
        "user approves", "user approved",
        "user authorizes", "user authorized",
        "waiting for user decision",
        "user-leg scope: approval", "user-leg: approval",
        "user must", "user needs to", "user should",
        "pending user sign-off", "pending user review",
        "blocked on user-initiated", "blocked on user action",
    ]
    combined = (subject + "\n" + message).lower()
    matched_pattern = first pattern from approval_patterns found in combined,
                      OR None

    IF matched_pattern is None:
        # Gate evaluated, no approval-request trigger → noop firing.
        Bash: bash core/scripts/gate-log.sh notify-user-approval-gate noop \
            --caller "notify-user/SKILL.md:Step 1.5" \
            --payload "{subject}"

    IF matched_pattern is not None:
        # Approval-request language detected. Run the capability gate on
        # the combined text to see if the underlying action is agent-
        # provisionable. Mirrors blocker-recheck.py's gate invocation
        # shape exactly.
        Bash: py -3 core/scripts/capability-gate.py \
            --failure-reason "{combined}" \
            --intended-participants user \
            --output json
        gate = parse JSON from stdout

        IF gate.would_block == true:
            match = (gate.matches or [{}])[0]
            matched_skill = match.get("skill") or match.get("row","")[:60] or "(unnamed)"
            matched_kw = match.get("matched_keyword", "")

            Log: "APPROVAL-REQUEST GATE: refused notification '{subject}' — "
                 "message asks user to do something agent-provisionable via "
                 "{matched_skill} (kw: {matched_kw})"

            # Telemetry firing — block decision (the FP-cost path; high cost if
            # this refuses a legitimate user-critical notification).
            Bash: bash core/scripts/gate-log.sh notify-user-approval-gate block \
                --caller "notify-user/SKILL.md:Step 1.5" \
                --trigger "{matched_pattern}" \
                --payload "{subject}" \
                --extra-json '{"matched_skill":"{matched_skill}","matched_kw":"{matched_kw}","would_block":true}'

            # Create an Investigate goal so the retrieval lapse gets learned
            # from. Same pattern as aspirations-precheck Phase 0.5b.4 and
            # blocker-recheck.py. Bounded: only one per session to avoid
            # flooding.
            IF no defer-rescue Investigate goal already created this session
               (check via wm-read.sh notification_log or session marker):
                goal_json = {
                    "title": "Investigate: /notify-user asked user to do agent-capable work",
                    "description": "Step 1.5 of /notify-user refused a notification "
                                   "with subject '{subject}' because it matched "
                                   "approval-request pattern '{matched_pattern}' AND "
                                   "capability-gate identified the underlying action "
                                   "as agent-provisionable via {matched_skill} "
                                   "(keyword {matched_kw}). Investigate: why did the "
                                   "caller reach for email instead of invoking the "
                                   "capability? Is there a guardrail gap? Should the "
                                   "caller's source skill add a capability-check step?",
                    "priority": "MEDIUM",
                    "participants": ["agent"],
                    "category": "framework-maintenance",
                    "tags": ["approval-request-refused", "retrieval-lapse", "learning"],
                    "origin_signal": "investigate:approval-request-refused",
                }
                echo '{goal_json}' | aspirations-add-goal.sh --source world asp-115

            # Refuse the send. The agent's next action must be to invoke the
            # matched capability, not to route around this gate.
            Output: "REFUSED: notification '{subject}' asks the user to do "
                    "something this agent can do itself via {matched_skill}. "
                    "Invoke that capability directly instead of emailing the "
                    "user. See .claude/rules/probe-before-defer.md §Enforcement."
            → DONE (no send, no fallback — this is a routing refusal, not a
                    delivery failure)
        # ELSE: approval language but no agent-capable match (e.g., "please
        # approve the deployment strategy" — human judgment). Proceed normally.
        ELSE:
            Bash: bash core/scripts/gate-log.sh notify-user-approval-gate pass \
                --caller "notify-user/SKILL.md:Step 1.5" \
                --trigger "{matched_pattern}" \
                --payload "{subject}" \
                --extra-json '{"would_block":false,"reason":"approval-language but no agent-capable match — human judgment needed"}'
# ELSE (category in blocker/completion): proceed. blocker is already gated
# by CREATE_BLOCKER; completion is a status report, not an approval request
# (g-115-1617). Neither is an approval-request leak path.
```

The heuristic is deliberately conservative on the gated categories
(info / update / decision-needed): it only runs the gate when
approval-request language is present. A completion email that reports
"alpha committed and pushed 4 files" contains the word "commit" but not
any approval pattern, so it skips the gate entirely. `completion` and
`blocker` are exempt outright (see the Step 1.5 category check above): a
completion report is a retrospective status, not a request, and gating it
produced a systematic FP for agents whose domain keywords (e.g. delta's
Roblox/Studio) collide with the capability list (g-115-1617). False-positive
rate stays near zero while catching the canonical incident the user reported
("multiple times agents notify me to 'approve git commit and push'").

## Step 2: Build Payload

The caller provides:
- **category**: one of `info`, `completion`, `update`, `blocker`, `decision-needed`
- **subject**: short subject line (email-send.sh prepends `[Alpha]` automatically)
- **message** OR **message-file**: the body content (inline text OR a path to a
  markdown file whose content becomes the body — prefer file when the content
  already exists on disk, e.g., completion reports)

### Deterministic Payload Construction (MANDATORY)

The payload is built by `core/scripts/notify-build-payload.py` — a single source
of truth that handles:
- Self-identity extraction from `agents/<agent>/self.md` (the first sentence of
  the body after the `# Self` heading becomes the opening line)
- Category → InfoType mapping
- SendInfoAlert vs SendErrorAlert shape selection by category
- **Silent-empty-email guard**: refuses payloads whose message body is < 50 chars
  (rc=2), refuses self.md missing or malformed (rc=3), refuses invalid category
  or missing inputs (rc=1)

DO NOT hand-construct the JSON payload. The 2026-05-20 incident — completion
emails arriving with only Title + UTC timestamp + reply-footer because the
Body field was empty — was caused by every call site hand-writing the JSON
template with no validation. The helper closes that gap.

```bash
# Inline message variant
py -3 core/scripts/notify-build-payload.py \
    --agent "<agent-name>" \
    --category "<category>" \
    --subject "<subject>" \
    --message "<message-text>"

# File-backed message variant (preferred for completion reports — the body
# already exists on disk in rich form; reading from file avoids the LLM
# re-constructing it from prose).
py -3 core/scripts/notify-build-payload.py \
    --agent "<agent-name>" \
    --category "<category>" \
    --subject "<subject>" \
    --message-file "<path-to-md>"

# Optional structured fields (rarely used — most emails are plain Body):
#   --sections-json '<json-array>'    JSON array of section objects
#   --next-steps-json '<json-array>'  JSON array of next-step strings
```

### Category → InfoType Mapping (reference; the helper applies this)

| Category | InfoType | When to use |
|----------|----------|-------------|
| `info` | Notification | New aspiration created, skill forged, general FYI |
| `completion` | Completion Report | Aspiration/goal completion review findings |
| `update` | Aspiration Update | Goals added to aspiration, decomposition done |
| `blocker` | Infrastructure Alert | Blocked goals, infrastructure error, cascade failure |
| `decision-needed` | Decision Needed | Agent made autonomous decision, user should review |

### Self-Identity Rule (enforced by the helper)

Every notification MUST identify who is sending it. The helper reads
`agents/<agent>/self.md` and extracts the first sentence of the body after
the `# Self` heading. Examples (per current self.md files):

- alpha → "I am Alpha, the developer for AyoAI — the hands that build."
- bravo → "I am the product manager and business analyst for AyoAI — the mind that drives the product forward."
- echo → "I am Echo — the ARC-AGI-3 Vertical Owner for AyoAI."

If Self changes, the opening line changes with it on the next send — no
template edits needed. The helper exits rc=3 if `# Self` is missing.

### Helper Exit Codes

| rc | Meaning | What to do |
|----|---------|-----------|
| 0  | Payload valid, emitted to stdout | Pipe stdout to email-send.sh (Step 3) |
| 1  | Input error (missing flag, bad JSON, message-file missing) | Fix the invocation, retry |
| 2  | Body too short — silent-empty-email guard refused | Investigate the caller; the message field was empty or trivial |
| 3  | self.md missing / malformed / no `# Self` heading | Fix self.md; the helper cannot proceed without identity |

On rc != 0, do NOT fall through to email-send.sh — fall through to the
Step 4 Fallback Cascade instead. A refused payload should never silently
become a Tier-1 send.

### Rate Awareness

Before sending, check for duplicate notifications in this session:

```
Bash: bash core/scripts/wm-read.sh notification_log
IF a notification with the same subject was sent within the last 30 minutes:
  Log: "Suppressed duplicate notification: <subject>"
  → DONE (skip send, no fallback needed — original was already sent)

Exception: blocker category is NEVER rate-limited.
```

## Step 3: Send

Pipe the helper's stdout (the validated JSON payload) directly into
`email-send.sh`. The `--error` flag is set automatically by category —
the helper emits SendErrorAlert shape for `blocker`, SendInfoAlert shape
for all others; pass `--error` to email-send.sh only when category is
`blocker`.

```bash
# Resolve $WORLD_PATH first — bare `world/...` Bash args are NOT hook-resolved
# on own-cloud (world/ lives at an external path, not PROJECT_ROOT), so a bare
# `bash world/scripts/email-send.sh` fails "No such file or directory" and the
# email silently never sends (path-resolution.md, g-115-2824).
source core/scripts/_paths.sh

# SendInfoAlert path (categories: info | completion | update | decision-needed)
py -3 core/scripts/notify-build-payload.py \
    --agent "<agent>" --category "<category>" \
    --subject "<subject>" --message-file "<path>" \
  | bash "$WORLD_PATH/scripts/email-send.sh"

# SendErrorAlert path (category: blocker)
py -3 core/scripts/notify-build-payload.py \
    --agent "<agent>" --category "blocker" \
    --subject "<subject>" --message "<message>" \
  | bash "$WORLD_PATH/scripts/email-send.sh" --error
```

IF helper exits non-zero (rc 1/2/3): do NOT pipe to email-send.sh; the
pipe will run anyway in shell semantics, but email-send.sh will receive
empty stdin and emit a malformed Lambda invocation. The helper's stderr
already explains the failure. Fall through to Step 4 Fallback Cascade.
To make this strict, prefer the two-step form:

```bash
source core/scripts/_paths.sh   # exports $WORLD_PATH (bare world/ args aren't hook-resolved — see note above)
PAYLOAD=$(py -3 core/scripts/notify-build-payload.py ...)
if [ $? -eq 0 ] && [ -n "$PAYLOAD" ]; then
    # Capture email-send.sh's REAL exit code. Do NOT append a trailing pipe
    # (`| tail`, `| head`, etc.) after email-send.sh — the pipe's exit would
    # mask a send failure as SEND_RC=0 and write a false notification_log
    # entry for an email that never sent (guard-696 class, g-115-2824).
    printf '%s' "$PAYLOAD" | bash "$WORLD_PATH/scripts/email-send.sh"
    SEND_RC=$?
else
    # Helper refused — go to Step 4
    SEND_RC=99
fi
```

IF send succeeds (SEND_RC == 0):
  Record in working memory:
  ```bash
  echo '{"subject":"<subject>","category":"<category>","sent_at":"<timestamp>"}' \
    | bash core/scripts/wm-append.sh notification_log
  ```
  Blocker-coverage append (g-115-2400 part b): a sent notification that NAMES a
  standing blocker IS inbox visibility — record it so aspirations-precheck Phase
  0.5b.1's re-send cooldown (`re_escalation_hours`) counts it and does not
  double-send a separate reminder. Fail-open: any error here must not affect
  the send result (it already succeeded).
  ```bash
  bash core/scripts/wm-read.sh known_blockers --json
  ```
  FOR EACH known blocker whose `blocker_id` appears verbatim in the sent
  subject or message body:
  ```bash
  echo '{"blocker_id":"<blocker_id>","sent_at":"<timestamp>","channel":"notify-user:<category>","coverage":"named-in-outbound-notification"}' \
    | bash core/scripts/wm-append.sh proactive_escalation_log
  ```
  → DONE

IF send fails (SEND_RC != 0):
  → Fall through to Step 4

## Step 4: Fallback Cascade

When email is unavailable or fails, degrade gracefully. Never block on notification failure.

### Tier 2: Pending Question (decision-needed category only)

IF category is `decision-needed`:
  Write to `agents/<agent>/session/pending-questions.yaml`:
  ```yaml
  - id: pq-NNN
    date: "<today>"
    context: "<category>"
    question: "<subject>: <message>"
    default_action: "Informational — no action required unless you disagree"
    status: pending
  ```
  The user sees these via `/open-questions` or when the agent surfaces them in `/respond`.

### Tier 3: Participant Goal (universal fallback)

For all categories when email and Tier 2 are not applicable or insufficient:

Create a `participants: [agent, user]` goal in the relevant aspiration:
```
echo '<goal_json>' | bash core/scripts/aspirations-add-goal.sh <aspiration_id>
```

Goal fields:
- title: `"User Notice: <subject (50 chars max)>"`
- description: `"<message>"`
- participants: `[agent, user]`  # NOT [user] — agent must still see and act on this
- priority: `HIGH` if blocker, `MEDIUM` otherwise
- tags: `["notification-fallback"]`
- origin_signal: `"idea:notify-user-fallback"` (notification-generated informational goal)

## Chaining

- **Called by**: Any skill that needs to notify, reach out to, or alert the user.
  Known callers: `/create-aspiration` (Step 8.5), `/aspirations` (aspiration update notification),
  `/aspirations-complete-review` (Step 7.5.6), `/aspirations-execute` (CREATE_BLOCKER Step 6),
  `/decompose` (Step 8), `/forge-skill` (Step 8 forge announcement + Step 9 test goal)
- **Uses**: `core/scripts/notify-build-payload.py` (Step 2 payload builder),
  `world/scripts/email-send.sh` (Step 3 send), `core/scripts/env-read.sh`,
  `core/scripts/wm-read.sh`, `core/scripts/wm-append.sh`,
  `core/scripts/aspirations-add-goal.sh`
- **Reads**: `agents/<agent>/self.md` (via the helper — first sentence of body
  after `# Self` heading becomes the email opening line)
- **Writes**: working memory `notification_log` slot,
  `pending-questions.yaml` (Tier 2 fallback), aspirations (Tier 3 fallback)

## Note on Delivery Confirmation

`email-send.sh` uses `--invocation-type Event` (async Lambda). A successful exit code
means AWS accepted the request, NOT that the email was delivered. The Step 1 env var
check is the only guard against silent non-delivery from empty credentials.

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The `email-send.sh` call (Tier 1) or the pending-question/aspirations write (Tier 2/3
fallback) is itself the terminal tool call. Never end with a text summary.
