---
name: access-operator-api
forged: true
forged_by: alpha
forged_date: "2026-03-28"
description: "Calls the AyoAI Operator REST API at operator.ayoai.com:8080 for health checks, scheduled task status, audit trails, EC2 state, and log retrieval. Use whenever the agent needs to check Operator service health, read task-event history, pull audit logs, inspect Operator-managed EC2, or confirm a scheduled job ran. MUST use world/scripts/operator-api.sh (injects AYOAI-API-KEY header) — raw curl returns 401."
user-invocable: false
triggers: [operator-api, operator.ayoai.com, 8080, audit-trail, task-schedule, operator-health, operator-rest, ayoai-api-key, operator-logs]
tools_used: [Bash]
companion_scripts: [world/scripts/operator-api.sh]
conventions: [secrets, infrastructure]
minimum_mode: autonomous
revision_id: "skill-bootstrap-access-operator-api-bbd9b9"
previous_revision_id: null
---

# /access-operator-api — Operator REST API Access

Access the AyoAI Operator service at `operator.ayoai.com:8080`.

## Companion Script

`world/scripts/operator-api.sh <METHOD> <endpoint-path> [request-body]`

## Endpoints (7 total)

| Method | Path | Description |
|--------|------|-------------|
| GET | /status | Health score, JVM metrics, platform health, task schedule |
| GET | /tasks | List all 11 scheduled tasks with status |
| POST | /tasks/{name}/execute | Manually trigger a task |
| GET | /audit-trail | Query task execution history (7-day TTL) |
| GET | /ec2-instances | List EC2 instances (operator, warm pool, game servers) |
| GET | /logs/recent?lines=N | Last N log lines (default 200) |
| GET | /logs/search?query=X&lines=N | Search logs by keyword |

## Usage Examples

```bash
# Check overall health
Bash: world/scripts/operator-api.sh GET /status

# List all tasks
Bash: world/scripts/operator-api.sh GET /tasks

# Trigger a health check
Bash: world/scripts/operator-api.sh POST /tasks/HealthCheck/execute

# Get recent audit trail
Bash: world/scripts/operator-api.sh GET /audit-trail

# List EC2 instances
Bash: world/scripts/operator-api.sh GET /ec2-instances

# Search logs for errors
Bash: world/scripts/operator-api.sh GET "/logs/search?query=ERROR&lines=50"

# Get last 100 log lines
Bash: world/scripts/operator-api.sh GET "/logs/recent?lines=100"
```

## Response Format

All endpoints return JSON: `{"success": true, "data": {...}, "timestamp": ...}`
Error: `{"success": false, "error": "message"}`

## Rate Limit

100 requests/minute per API key. Auth: `AYOAI-API-KEY` header with `AYO_OPERATOR_KEY`.

## Security

- `AYO_OPERATOR_KEY` from `.env.local` — admin API key
- HTTPS on port 8080 with TLS certificates from EFS

## Infra-Health Component

Component: `operator-api`. Probe: `operator-api.sh GET /status`.

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The last `operator-api.sh` invocation is itself the terminal tool call. Never end
with a text summary of the API response.
