# No Auto-Memory

This project has its own knowledge persistence. NEVER write to the platform
auto-memory directory (~/.claude/projects/*/memory/). All persistence uses:

- User corrections / behavioral rules → guardrails (guardrails-add.sh)
- Domain knowledge / facts → knowledge tree (/tree add or Edit node .md)
- Lessons learned / failure analysis → reasoning bank (reasoning-bank-add.sh)
- Session-scoped info → working memory (<agent>/session/working-memory.yaml)

When the user says "remember": use the knowledge tree, not auto-memory.
When you learn something new: use guardrails or reasoning bank, not auto-memory.
The MEMORY.md content injected by the platform is legacy — ignore it.
