# Domain-Specific Verification Checklist (Framework Template)

This file is the **framework template** for deployment-specific verification
checks. It supplements `core/config/verification-checklist.md` (the framework
reference catalog).

## How the three-tier loading works

`/verify-learning` Step 1 loads three files in order:

1. `core/config/verification-checklist.md` — framework reference catalog
   (active checks live inline in `verify-learning/SKILL.md` Step 3)
2. `core/config/verification-checklist-domain-specific.md` — **this file**:
   universal starter checks + commented-out examples of deployment-specific
   shapes
3. `world/verification-checklist.md` — the per-deployment overlay (seeded
   empty by `init-world.sh`; deployments grow it over time)

Step 1.1 (load-time sanity) emits a SOFT WARNING if any loaded file
contributes zero `Check:` lines AND the deployment has ≥10 completed goals.
That assertion exists because this file was a 23-line stub from 2026-04-06
to 2026-05-17, contributing zero checks, and `/verify-learning` never
surfaced the rot.

## Section ZA: Universal Starter Checks

These checks apply to ANY deployment of the Mind framework. They verify
the basic "the loop is alive and learning" properties that survive across
all domains. Keep this section here — even if you customize Section Z/ZZ/ZZZ
below, Section ZA gives every deployment a baseline.

Check: `world/program.md` exists and is non-empty (The Program — shared purpose). Bash: `test -s "$WORLD_DIR/program.md" && echo PASS || echo "FAIL: world/program.md is empty or missing — no shared purpose defined"`

Check: at least one aspiration in `world/aspirations.jsonl` has `status: active`. Bash: `py -3 -c "import sys,json,pathlib; sys.path.insert(0,'core/scripts'); from _paths import WORLD_DIR; p=WORLD_DIR/'aspirations.jsonl'; n=sum(1 for line in p.read_text(encoding='utf-8').splitlines() if line.strip() and json.loads(line).get('status')=='active') if p.exists() else 0; print(f'PASS: {n} active aspirations') if n>=1 else (print('FAIL: no active aspirations — the loop has nothing to do') or sys.exit(1))"`

Check: the bound agent's `self.md` is non-empty and was updated within the last 90 days. Bash: `py -3 -c "import os,sys,pathlib,time; agent=os.environ.get('MIND_AGENT'); print('N/A: MIND_AGENT not set — skipping per-agent self.md check') or sys.exit(0) if not agent else None; sys.path.insert(0,'core/scripts'); from _paths import agent_dir; p=agent_dir(agent)/'self.md'; ok=p.exists() and p.stat().st_size>0; age_d=(time.time()-p.stat().st_mtime)/86400 if p.exists() else 999; print(f'PASS: {agent}/self.md present ({age_d:.0f}d since update)') if ok and age_d<=90 else (print(f'WARN: {agent}/self.md missing or {age_d:.0f}d stale (>90d) — agent identity may have drifted') if ok else print(f'FAIL: {agent}/self.md missing or empty'))"`

Check: at least one goal completed in the last 14 days (the agent is actually working, not dormant). Bash: `py -3 -c "import os,sys,pathlib,json; from datetime import datetime,timedelta; sys.path.insert(0,'core/scripts'); from _paths import WORLD_DIR; cutoff=datetime.now()-timedelta(days=14); recent=0; p=WORLD_DIR/'aspirations.jsonl'; [recent := recent+sum(1 for g in json.loads(line).get('goals',[]) if g.get('status')=='completed' and g.get('completed_at','') and datetime.fromisoformat(g['completed_at'].replace('Z','')[:19]) > cutoff) for line in p.read_text(encoding='utf-8').splitlines() if line.strip()] if p.exists() else None; print(f'PASS: {recent} goals completed in last 14d') if recent>=1 else print(f'WARN: zero goals completed in last 14 days — agent may be dormant or stuck')"`

Check: no aspiration has been `in-progress` continuously for more than 7 days (catches stuck claims). Bash: `py -3 -c "import sys,json,pathlib; from datetime import datetime,timedelta; sys.path.insert(0,'core/scripts'); from _paths import WORLD_DIR; cutoff=datetime.now()-timedelta(days=7); stuck=[]; p=WORLD_DIR/'aspirations.jsonl'; [stuck.append((a.get('id'), g.get('id'))) for line in p.read_text(encoding='utf-8').splitlines() if line.strip() for a in [json.loads(line)] for g in a.get('goals',[]) if g.get('status')=='in-progress' and g.get('claimed_at','') and datetime.fromisoformat(g['claimed_at'].replace('Z','')[:19]) < cutoff] if p.exists() else None; print(f'PASS: no goals stuck in-progress >7d') if not stuck else print(f'WARN: {len(stuck)} goals stuck in-progress >7d: {stuck[:3]} — investigate stale claims')"`

## Section Z: Domain Infrastructure (deployment-specific)

Add checks specific to YOUR deployment's infrastructure. Examples:

<!--
Example: build tool passes compilation. Bash: `<your-build-command> && agent-name PASS || agent-name FAIL`
Example: test suite passes (all existing + new tests). Bash: `<your-test-runner> && agent-name PASS || agent-name FAIL`
Example: `agents/<agent>/infra-health.yaml` has component entries for your infrastructure
Example: IF any goal was skipped due to infrastructure blocker, infra-health.sh check <component> was called BEFORE the blocker was accepted
-->

## Section ZZ: Domain Behavior (deployment-specific)

Add checks for behaviors specific to YOUR deployment's domain. Examples:

<!--
Example: domain-specific output artifact was produced for at least one completed goal in the last 7 days
Example: domain-specific service is reachable (replace with your own probe)
Example: deployment-specific naming convention is enforced
-->

## Section ZZZ: Domain Testing Infrastructure (deployment-specific)

Add checks for the testing infrastructure specific to YOUR domain. Examples:

<!--
Example: test data fixture is present and within size budget
Example: integration test environment is reachable
Example: regression test baseline file is up to date
-->

## How to add a deployment-specific check

Two locations, two intents:

- **Universal-but-this-file**: a check that applies to most or all
  deployments but isn't intrinsic to the framework itself — add to Section ZA
  above. Bias toward few, broadly-applicable checks.
- **Per-deployment**: a check specific to YOUR domain (your build tool, your
  service, your data shape) — add to `world/verification-checklist.md`
  (created by `init-world.sh`, customized by your deployment). Use the same
  `Check:` line format.

The shape of a check (this is documentation, not an executable check —
the leading "Format:" prefix prevents it counting toward Step 1.1's
rot-detection assertion):

```
Format: <one-line description>. Bash: `<bash command that prints PASS/FAIL/WARN and exits 0 or 1>`
```

Conventions:
- Exit 0 + print starting with `PASS:` → check passes
- Exit 1 + print starting with `FAIL:` → check fails (verification report flags it)
- Exit 0 + print starting with `WARN:` → soft warning (surfaces but doesn't fail)
- Resolve `$WORLD_DIR` via `sys.path.insert(0,'core/scripts'); from _paths import WORLD_DIR` in Python one-liners
- Use `py -3` (not `python3`) for Windows compatibility per `core/config/conventions/python-invocation.md`
