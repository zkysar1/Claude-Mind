# Stdin JSON Inputs — Canonical Call Pattern

## Principle

Scripts that accept a JSON payload on stdin must be invoked via a **quoted
heredoc** (`<<'EOF' ... EOF`), not via `echo` pipelines. The quoted
heredoc suppresses ALL shell expansion — backticks, `$(...)`, `$VAR` — so
the JSON reaches the script byte-identical to what the author wrote.

`echo` pipelines are historically tolerated with single quotes, but one
drift to double quotes turns them into silent shell-injection vectors
before the JSON ever reaches Python. Heredoc is unconditionally safe.

## Canonical Form

```bash
bash core/scripts/aspirations-add-goal.sh <aspiration-id> <<'EOF'
{"title": "Investigate: Use `foo` not `bar`", "priority": "HIGH"}
EOF
```

The single quotes around `EOF` are load-bearing. Without them, `<<EOF`
behaves like a double-quoted string and backticks expand as command
substitution.

## Forbidden

```bash
# NEVER — backticks and $(...) inside become command substitution.
echo "{\"description\": \"Use \`foo\`\"}" | bash core/scripts/aspirations-add-goal.sh asp-001
```

## Tolerated (legacy only)

```bash
# Safe ONLY if JSON contains zero single-quote characters. Brittle.
echo '{"description": "Use `foo` not `bar`"}' | bash core/scripts/aspirations-add-goal.sh asp-001
```

Existing call sites using this form are grandfathered in. Prefer heredoc
for any NEW call site — converting legacy sites is tracked separately.

## Applies To

Any `core/scripts/*.sh` wrapper or `core/scripts/*.py` handler that reads
JSON from stdin via `sys.stdin.read()`. Current list:
`aspirations.py`, `reasoning-bank.py`, `journal.py`, `tree.py`,
`pipeline.py`, `conclusion-record.py`, `wm.py`,
`blocker-create-gate.py`, `execution-diary.py`, `spark-questions.py`,
`skill-relations.py`, `board.py`, `experience.py`,
`meta-dead-ends.py`, `meta-yaml.py`, `pattern-signatures.py`,
`mind-yaml.py`, `reasoning-snapshot.py`, `iteration-close.sh`.

## Enforcement

`core/scripts/skill-stdin-json-lint.sh` greps `.claude/skills/` for
unsafe `echo "{...}"` and `echo "[...]"` patterns and exits 1 on match.
Wired into `PostToolUse[Edit|Write]` hooks in `.claude/settings.json`
— drift surfaces immediately to the author who introduced it.
