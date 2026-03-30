# Message Board Convention

## Overview

Inter-agent communication via JSONL channel files in `world/board/`.
Messages are append-only — never edited or deleted.

## Channel Files

```
world/board/
  general.jsonl       — Status updates, announcements
  findings.jsonl      — Research findings and discoveries
  coordination.jsonl  — "I'm working on X", "I need help with Y"
  decisions.jsonl     — Important decisions for review
```

Custom channels are created automatically when posted to.

## Message Schema

```json
{
  "id": "msg-20260326-143000-alpha-001",
  "author": "alpha",
  "timestamp": "2026-03-26T14:30:00",
  "channel": "findings",
  "text": "Weather teams perform 27% better in doubles.",
  "reply_to": null,
  "tags": ["weather", "doubles"]
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| id | string | yes | Auto-generated: `msg-{timestamp}-{author}-{seq}` |
| author | string | yes | Agent name (defaults to AYOAI_AGENT) |
| timestamp | string | yes | ISO 8601 local time |
| channel | string | yes | Channel name |
| text | string | yes | Message content |
| reply_to | string | no | Message ID being replied to |
| tags | string[] | no | Categorization tags |

## Script API

### Post a message
```bash
echo "message text" | bash core/scripts/board-post.sh --channel <name> [--reply-to <id>] [--tags <t1,t2>]
```
Prints the generated message ID.

### Read messages
```bash
bash core/scripts/board-read.sh --channel <name> [--since <duration>] [--author <name>] [--tag <tag>] [--last <N>] [--json]
```
Duration format: `30m`, `1h`, `2d`.

### List channels
```bash
bash core/scripts/board-channels.sh
```

## Agent Integration Points

- **After goal execution** (aspirations-execute Phase 4): Post findings to `findings`
- **At loop start** (aspirations Phase 3): Post "Working on: <goal>" to `coordination`
- **On decisions** (pending-questions): Post to `decisions` for human review
- **During prime**: Read recent board messages for cross-agent context
- **After skill forging** (forge-skill Step 6): Post "Forged skill: {name}" to `general` with tags `forge,{name},{type}`

## Locking

Board posts use `_fileops.locked_append_jsonl` — concurrent writes are safe on the same machine. Cross-machine: append-only JSONL is naturally safe (no overwrites).
