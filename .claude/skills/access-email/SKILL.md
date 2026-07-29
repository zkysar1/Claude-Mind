---
name: access-email
forged: true
forged_by: alpha
forged_date: "2026-03-28"
description: "Sends outbound email and reads the agent inbox (agents@ayoai.com) for deployment alerts, error notifications, and user replies. Use whenever the agent needs to check for new mail, verify a GitHub Actions deployment success email (guard-013), poll for a user answer to a pending question, or send a low-level transactional message. Prefer /notify-user for user-facing notifications — this skill is the lower-level transport used by SendInfoAlert / SendErrorAlert."
user-invocable: false
triggers: [email, smtp, imap, agents-email, mailbox, inbox, email-send, email-read, send-info-alert, send-error-alert]
tools_used: [Bash]
companion_scripts: [world/scripts/email-send.sh, world/scripts/email-read.sh]
conventions: [secrets, infrastructure]
minimum_mode: autonomous
revision_id: "skill-bootstrap-access-email-e924e3"
previous_revision_id: null
---

# /access-email — Email Communication & Monitoring

Send emails to user and monitor the agent inbox for alerts, deployment results, and messages.

**Email is the primary information channel.** All production systems emit emails via
SendInfoAlert/SendErrorAlert. The agent MUST monitor emails as a first-class information source.

## Companion Scripts

- `world/scripts/email-send.sh` — the transport (default SendInfoAlert; `--error` for SendErrorAlert). Do NOT hand-pipe inline JSON into it: its provenance gate (g-115-2186) REFUSES any payload lacking the `XPayloadProvenance` stamp that sanctioned builders add. Build payloads via `core/scripts/notify-build-payload.py` (or `world/scripts/notify-from-file.sh` for file-body emails) and pipe the builder's output in. Emergency-only bypass: `EMAIL_SEND_ALLOW_UNSTAMPED="<why>"`.
- `world/scripts/email-read.sh list [--max N]` — List agent inbox manifests
- `world/scripts/email-read.sh read <manifest-key>` — Read specific manifest
- `world/scripts/email-read.sh check-alerts` — Scan for error/deployment alerts

## Agent Inbox Architecture

```
User/System → email to agents@ayoai.com
  → SES receives, stores raw email in S3 bucket (AYO_SES_EMAIL_S3_BUCKET)
  → S3 trigger fires ForwardAllEmailReceivedFromAyoAiDotComDomain Lambda
  → Lambda checks: is_agent_email() → is_allowed_agent_sender()
  → Stores JSON manifest at s3://bucket/agent-inbox/{original_key}.json
  → Manifest: {original_key, from, subject, date, body_preview (500 chars), verified_sender}

Whitelisted senders: zkysar@gmail.com, agents@ayoai.com, alert@ayoai.com
Bash: echo "inbox architecture documented"
```

## SendInfoAlert Payload (3 modes)

### Structured (rich HTML)
```json
{
  "InfoMessage": "Summary text (required)",
  "InfoType": "Category label",
  "FromEmail": "agents@ayoai.com",
  "RecipientEmail": "user@email.com",
  "Title": "Email heading",
  "Body": "Main text",
  "Sections": [{"heading": "...", "items": ["..."], "style": "success|warning|danger|info"}],
  "Metrics": {"key": "value"},
  "NextSteps": ["Step 1", "Step 2"]
}
```

**WARNING — `Metrics` shape**: dict (`{"key": "value"}`) only. Passing a list
(e.g. `[{"label":"X","value":"Y"}, ...]`) crashes the Lambda at `metrics.items()`
(lambda_function.py:81/:176) and auto-fires SendErrorAlert, which alert-sweep
files as a fresh Investigate goal each cycle (g-115-911 canonical). Until
g-115-912 hardens the Lambda to accept both shapes, callers MUST use dict.

### HTML passthrough
If InfoMessage starts with `<html>` or `<!DOCTYPE`, sent as-is.

### Simple
Plain InfoMessage string wrapped in styled template.

## SendErrorAlert Payload
```json
{
  "ErrorMessage": "Error details",
  "ErrorFrom": "Source component",
  "RecipientEmail": "optional@override.com"
}
```
Note: SendErrorAlert also fetches last 200 Operator logs and includes them in the email.

## Mandatory Email Checks

1. **After every deployment** (commit → push → GH Actions):
   `email-read.sh check-alerts` — look for deployment success/failure emails
2. **After every live system interaction**:
   `email-read.sh check-alerts` — look for error alerts
3. **Recurring goal** (interval_hours: 1):
   Full inbox check — categorize all new manifests, route as needed
4. **During every full inbox check** (part of step 3):
   `bash world/scripts/escalation-reply-scan.sh` — parse user YES/NO replies to
   guard-33 self-escalation confirmation requests + run the 48h expire-sweep.
   Cheap no-op when nothing is awaiting. See
   `world/conventions/self-escalation-confirmation.md`.

## Email Categories and Actions

| Category | Subject patterns | Action |
|----------|-----------------|--------|
| Deployment success | "deploy", "build", "succeed" | Log, update knowledge |
| Deployment failure | "fail", "error" + "deploy"/"build" | Create HIGH Unblock goal |
| Error alert | "error", "alert" | Create Investigate goal |
| User message | From user email | Route to /respond |
| System info | "info" | Log if relevant |

## Inbound Signal Retrieval (G6 / R10)

Before acting on a non-trivial inbound email (anything beyond "log if relevant"),
retrieve context the message relates to so the routing decision is informed by
accumulated knowledge, not the subject line alone. Per
`.claude/rules/retrieve-before-deciding.md` decision point 6 ("acting on an
inbound signal").

```
For each new email manifest NOT in {System info, already-routed user-message}:

  # Build a query from the subject + first 200 chars of body_preview
  query = "{manifest.subject} {manifest.body_preview[:200]}"

  Bash: retrieve.sh --category "{query}" --depth shallow

  Use the returned JSON to inform routing:
    - Deployment failure: if reasoning_bank[] has prior failures matching the
      error signature, attach the rb-NNN IDs to the Unblock goal description
      so executor sees them up front
    - Error alert: if a guardrail describes how to handle this error class,
      include it in the Investigate goal's verification.checks
    - User message: pass loaded tree_nodes + recent rb-NNN IDs to /respond
      via working memory `inbound_signal_context` slot so /respond Step 4
      reuses them instead of re-retrieving

  Then proceed with the Action from the table above, but with the retrieved
  context as input to the action.

  Fail-open: if retrieve.sh errors, log and fall back to the table-based
  action without context enrichment. Inbound signal routing must not block.
```

## Security

- `AGENTS_GROUP_EMAIL` — shared group address for all agents (`agents@ayoai.com`). The "from" address.
- `USER_EMAIL` — user's personal email. The "to" address for outbound.
- `AYO_SES_EMAIL_S3_BUCKET` — the group mailbox. S3 bucket where SES stores all inbound `@ayoai.com` emails.
- Agent identity: alpha prefixes `[Alpha]` in InfoType/ErrorFrom so user knows which agent sent it.
- `AYO_SES_EMAIL_S3_BUCKET` for inbox access
- Credential values never persisted — loaded at runtime

## Infra-Health Components

- `email-send`: Probe via test Lambda invoke
- `email-inbox`: Probe via S3 list operation

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is the last `email-send.sh`, `email-read.sh`,
`aspirations-add-goal.sh`, or `board-post.sh` invocation in the flow. If none
fire (e.g., inbox was empty), end with `Bash: echo "access-email complete"`.
