# Rationale: Capability Before User — incidents and measured cases

Referenced from `.claude/rules/capability-before-user.md`. That rule keeps the
imperatives; this file holds the incident narratives.

## The canonical incident behind "State a principled decline AS A CHOICE" and hand-command hygiene (2026-08-03)

Canonical incident (2026-08-03, production): a HIGH finding said a container
lacked its constitutional anchor. The agent could have written it — it had
ssh-ed into that container three times in the same conversation — and declined
on the reasoning that the anchor's value depends on the agent not having
written it. Sound in isolation, applied as an absolute the governing doc does
not state, while the principal was present offering the authorized path. The
agent then resolved "I am at that computer now" against its own last topic and
handed over container-specific paths; the principal was at a different machine,
different deployment, non-root. Permission denied → `sudo -i` → root shell →
"did I just break production". He had not. Cost: about an hour of his evening,
a real scare, and a two-day delay on a two-minute fix. All four hand-command
hygiene guards would have broken that chain, and the one-sentence voiced decline
would have prevented it entirely.

## Cross-references

- `.claude/rules/capability-before-user.md` — the imperatives this file explains
- g-353-151 — the context-floor diet that moved this text out of the always-loaded rule
