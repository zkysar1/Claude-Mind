# /aspirations Sub-Commands (Non-Loop Paths)

Loaded only when `Skill('aspirations')` is invoked with `args` ≠ `loop`.
The `loop` sub-command stays inline in `.claude/skills/aspirations/SKILL.md` —
it is the autonomous hot path. Everything else lives here.

Invocation dispatch sits at the top of `aspirations/SKILL.md`:

```
IF args != 'loop' AND args is non-empty:
    Read core/config/aspirations-subcommands-digest.md
    Follow the section matching <args>. RETURN.
```

---

## `status`

Display current aspiration state:

1. `aspirations-read.sh --active` + `--meta` (readiness gates, session_count)
2. Show readiness gates, aspirations, goals, recurring status, evolution log,
   user actions, meta-memory.

## `next`

Select and execute ONE goal, then return:

1. invoke /aspirations-precheck → invoke /aspirations-select → execute →
   verify → state update
2. Return result.

## `evolve`

invoke /aspirations-evolve with: fired_triggers, aspiration state.

## `complete <goal-id> [--permanent]`

1. If NOT recurring: set status `completed`. Update `completed_date`,
   `achievedCount`, streaks.
2. Recurring: NEVER set completed — update streaks/timestamps only.
   `--permanent`: set `recurring: false`.
3. Unblock dependent goals. Run spark check. Update readiness gates.

## `add <title>`

1. Gap analysis (overlap check).
2. Scope classification (sprint / project / initiative).
3. Sprint: auto-generate 2-5 goals via /decompose. Project+: invoke
   /create-aspiration --plan.
4. Enforce cap. Log. Create via `aspirations-add.sh`.
