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

## Flags Are Not Fields — and the two ways this wedges

The sections above govern how to QUOTE the body. This one governs what
happens when a caller supplies no body at all. A stdin-body wrapper has
**two structurally independent failure doors**, and closing one does not
close the other (guard-3393):

| Door | Trigger | The only fix |
|---|---|---|
| **Flag door** | Caller passes field-shaped flags (`guardrails-add.sh --rule "..."`) instead of a JSON body. The arg loop's `*) shift;;` discards them, then `BODY="$(cat)"` runs with nothing piped. | An **argv-shape reject** in the `case` block. Never touches stdin, so it is safe for hook-wired scripts. |
| **Idle-stdin door** | stdin is open but never delivers EOF — a backgrounded task inherits a live descriptor. Argv is irrelevant. | A **bounded probe**: `IFS= read -r -t 2 first_chunk`, then degrade with a loud stderr note (the g-115-2291 guard). |

A no-flag call into a wedged stdin passes the flag guard untouched.

**`[ -t 0 ]` cannot gate either door.** Measured FALSE for both a
`/dev/null` stdin and a never-EOF socket stdin, so any guard built on a
tty test is inert by construction. `pipeline-move.sh` uses `! [ -t 0 ]`
only to *skip* the probe on a real tty, then does the bounded read
regardless — copy that structure, do not re-derive it.

**Whether the flag form hangs at all is box-dependent**, which is why
this class survives local testing. Same command, same repo, measured
2026-08-11: on a box whose harness hands the process a never-EOF socket,
processes stayed alive 7 and 4 days; on a box handing it `/dev/null`, the
identical call returned `{"error": "invalid_body"}` in 0s. Reproducing
"it errors, it does not hang" is evidence about your box's stdin, not
about the hazard. Check `readlink /proc/$$/fd/0`; do not time the call.

### Which wrappers are actually exposed to the flag door

**A mandatory subcommand is already a flag guard.** Where `argv[1]` must be
one of a fixed set, a flag-shaped first argument fails the subcommand check
before the arg loop or the slurp is ever reached. Measured 2026-08-11, each
called with `--bogus-flag zzz`:

| Wrapper | Result |
|---|---|
| `meta-dead-ends.sh` | `Usage: {add\|check\|read\|increment\|review}`, rc=1 |
| `skill-relations.sh` | `Error: unknown subcommand '--bogus-flag'`, rc=1 |
| `tree-update.sh` | `Specify an update subcommand`, rc=1 |
| `probe-staleness-leak.sh` | structured `{"status": "unverified", "reason": "no_chain"}`, rc=1 |

So the exposed set is **wrappers that take no subcommand**, where `argv[1]` is
free-form and a catch-all can swallow it — which is exactly the six
store-append wrappers. Scoping an audit by "has a `$(cat)` slurp" over-counts:
that predicate returned 10 silent-discard writers, of which only 6 could
actually be reached through the flag door.

Note what this does **not** establish. Those four were probed with
`< /dev/null`; `probe-staleness-leak.sh` in particular *did* reach its slurp
and handled the resulting EOF gracefully. Closing the flag door says nothing
about the idle-stdin door for any of them.

### Reference implementations

- **Flag door** — `aspirations-add-goal.sh` (the original) and the six
  store-append wrappers `experience-add.sh`, `guardrails-add.sh`,
  `journal-add.sh`, `pattern-signatures-add.sh`, `reasoning-bank-add.sh`,
  `spark-questions-add.sh`. Each also carries a `--help` branch, because a
  stdin-body reader otherwise **hangs on `--help`** rather than printing
  usage (guard-3145).
- **Idle-stdin door** — `pipeline-move.sh`, `aspirations-complete-intent.sh`.

Regression cover: `core/scripts/tests/test_store_append_flag_refusal.py`,
whose discrimination test re-introduces `*) shift;;` into a temp copy and
asserts the refusal stops firing — so its green is proven, not assumed.

**Exempt: hook-wired scripts.** `stop-hook.sh`, `stop-failure-hook.sh`,
`owncloud-push-on-write.sh`, `tree-sync-check.sh`,
`user-prompt-skill-record.sh` are invoked by the harness with a real
payload on stdin. Do not add either guard to them.

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
