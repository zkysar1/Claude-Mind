# Secrets & Credentials Convention

## File Locations
- `.env.example` — Committed template at repo root. Key names + descriptions.
- `.env.local` — Gitignored values at repo root. User-managed. Survives factory reset.

## Access Pattern
- Check: `env-read.sh has KEY_NAME` (exit 0 = present)
- Read: `env-read.sh value KEY_NAME` (consume in same Bash call)
- Register new: `env-read.sh register KEY_NAME "description"`
- Status: `env-read.sh status` (JSON array of all keys)
- Missing: `env-read.sh missing` (JSON array of missing keys)

All backed by `core/scripts/env.py` (Python 3, stdlib only).

## Naming
- Key names: `UPPER_SNAKE_CASE`

## Security Rules
- Credential values MUST NEVER appear in journal, knowledge tree, working memory,
  handoff, or any file in `world/`, `<agent>/`, or `meta/`
- When using `value`, consume in the same Bash invocation (shell variable, not disk)
- If an API response echoes the credential, redact before writing to `world/`, `<agent>/`, or `meta/`

## Missing Credential Flow
1. Skill calls `env-read.sh has KEY` → missing
2. Skill creates `user_action` goal with `participants: [user]` (if not already exists)
3. Skill falls back to unauthenticated alternative or skips
4. Boot Phase -0.5 also detects missing keys and creates user goals
5. Session-end Step 8.7 recaps all user goals
6. Boot Step 0 auto-completes user credential goals when key appears in .env.local
