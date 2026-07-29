---
name: scan-stale-jobs
forged: true
forged_by: alpha
forged_date: "2026-04-20"
forged_from: g-240-13
description: "Runs the stale background-process scanner: identifies long-running OS processes that exceed their type-specific lifetime threshold (Tier A registered jobs past lifetime; Tier B unregistered orphans matching known signatures) and — when invoked with --auto-kill, with a recent report present, and candidate count within the per-run cap — reaps them graceful-first. Use whenever the g-115-NN recurring goal 'Scan for stale background processes' fires (every 4h), or when the agent needs to diagnose whether a prior session left orphan SSH / Processor / llama-server / roblox-bridge processes behind."
user-invocable: false
triggers: [stale, stale-jobs, stale-processes, orphan-process, background-leak, process-cleanup, zombie-process, scan-stale, reap]
tools_used: [Bash]
companion_scripts: [world/scripts/stale-jobs-scan.sh]
conventions: [session-state]
minimum_mode: autonomous
revision_id: "skill-bootstrap-scan-stale-jobs-e4ce00"
previous_revision_id: null
---

# /scan-stale-jobs — Stale Background-Process Scanner

Scan the host OS for long-running processes that have exceeded their
type-specific lifetime threshold and (when configured) reap them safely.

## Why This Exists

Long autonomous sessions leak shell subprocesses: Processor runs whose watcher
subshell is disowned, SSH sessions that hang on mid-transfer, `llama-server.exe`
that survives its parent. The framework's `background-jobs.sh` *registers*
long-lived processes but does nothing to clean them up —
`core/config/conventions/session-state.md` explicitly says "cleanup is the
monitoring skill's responsibility."

This skill is that monitoring skill.

## Companion Script

- `world/scripts/stale-jobs-scan.sh [subcommand]` — canonical probe for this
  skill. Subcommands:
  - `report` (default) — dry-run; write candidates to
    `core/logs/stale-scanner-report.jsonl`.
  - `reconcile` — deregister dead-PID entries from every agent's
    `session/background-jobs.yaml`.
  - `scan --auto-kill` — kill candidates, gated by config + safety nets.

## Procedure

1. `Bash: world/scripts/stale-jobs-scan.sh report` — always first. Emits the
   current candidate list and do-not-kill set. The report file's mtime is
   read later as a freshness precondition for auto-kill.
2. `Bash: world/scripts/stale-jobs-scan.sh reconcile` — deregister dead-PID
   entries. Costs nothing when registries are clean; prevents stale
   `background-jobs.yaml` entries from accumulating.
3. `Bash: world/scripts/stale-jobs-scan.sh scan --auto-kill` — kill up to 3
   candidates per run. The script itself enforces every safety gate — the
   skill never makes the kill decision directly.

## Safety Model

The scanner protects:
- The scanner's own process ancestry (Win32_Process.ParentProcessId walk,
  validated by CreationDate ordering to detect recycled PIDs).
- Every PID registered in any agent's `session/background-jobs.yaml` — not
  just the current agent's. Cross-agent fratricide is impossible by design.
- Any process matching Claude-Code signatures (`@anthropic-ai/claude-code`,
  `node.*cli.mjs`, `\claude.exe`).

Auto-kill gates:
- A report file younger than 24h in `core/logs/stale-scanner-report.jsonl`.
- No more than 3 candidates per run (overage → report-only).
- Per-process cooldown: 10 minutes (newborn processes always skipped).
- Graceful `taskkill /PID` first, 30-second wait, then `/F` only if needed.
- Every kill logged to `core/logs/stale-scanner-kills.jsonl`.

## Known-Orphan Signatures

- `py main.py` → Processor (threshold 8h)
- `llama-server.exe` → GPU inference server (8h)
- `roblox-bridge.py` → Studio bridge HTTP server (24h)
- `ssh ... ec2-user@` → shared-filesystem SSH (1h — should be seconds)
- Unknown types fall back to `default: 12h`.

Thresholds live in `stale_scanner.thresholds` in
`core/config/aspirations.yaml` — edit there, not in script code.

## Expected Output

- Report subcommand exit 0, candidate list printed to stdout, JSONL entry
  appended to `core/logs/stale-scanner-report.jsonl`.
- Reconcile subcommand exit 0 with "Registries clean." or list of removed
  entries.
- Scan subcommand exit 0 with report + (when invoked with --auto-kill and
  safety gates pass) kill summaries.

## Failure Modes

- **WMI query fails** (rare; requires PowerShell): scanner reports zero
  processes, zero candidates. No kill actions taken. Benign.
- **No `_paths.py` resolution**: scanner imports `PROJECT_ROOT` directly
  from the module — independent of `AYOAI_AGENT`. Works standalone.
- **Concurrent modification of `background-jobs.yaml`**: scanner reads each
  file once; `background-jobs.sh deregister` performs its own atomic
  write. Race is a stale read at worst, not a corruption.

## Chaining

- **Called by**: the aspirations loop via recurring goal `g-115-106`
  ("Scan for stale background processes") under
  `asp-115 Recurring Infrastructure Monitoring` (interval 4h). Also valid
  as a one-shot diagnostic when the agent suspects orphan accumulation.
- **Calls**: `world/scripts/stale-jobs-scan.sh` (`report`, `reconcile`,
  `scan`).
- **Does NOT call**: `/boot`, `/aspirations`, `/respond`, or any other
  skill.
- **Modifies**: `core/logs/stale-scanner-report.jsonl` (append),
  `core/logs/stale-scanner-kills.jsonl` (append, only when kills fire).
  Deregistrations flow through `background-jobs.sh` which modifies each
  agent's own `session/background-jobs.yaml`.

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call,
not text. The terminal Bash call is the `scan` (or `reconcile`, or
`report`) invocation; never end the skill with a text summary paragraph.
