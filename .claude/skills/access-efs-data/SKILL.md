---
name: access-efs-data
forged: true
forged_by: alpha
forged_date: "2026-03-28"
description: "SSHes into the AyoAI EFS EC2 bastion to read NPC runtime data, execute remote shell commands, and download files via SCP. Use whenever the agent needs to inspect EFS contents, list NPC state directories, pull down game-session data, verify remote file existence, or execute any command that requires the EFS mount. MUST use world/scripts/efs-ssh.sh (canonical probe with StrictHostKeyChecking disabled) — never raw ssh, which trips on host-key rotation."
user-invocable: false
triggers: [efs-ssh, known_hosts, ssh, scp, ec2-user, efs-mount, efs-download, remote-ec2-command, strict-host-key-checking, host-key-rotation, ssh-host-key]
tools_used: [Bash]
companion_scripts: [world/scripts/efs-ssh.sh, world/scripts/efs-download.sh]
conventions: [secrets, infrastructure]
minimum_mode: autonomous
revision_id: "skill-bootstrap-access-efs-data-9f8b7e"
previous_revision_id: null
---

# /access-efs-data — EFS Data Access via SSH

Access NPC data on the EFS-mounted EC2 instance via SSH/SCP.

## Companion Scripts

- `world/scripts/efs-ssh.sh "<command>"` — Execute command on remote EC2, return stdout
- `world/scripts/efs-download.sh "<remote_path>" "<local_path>"` — Download file via SCP

## EFS Directory Structure

```
/home/ec2-user/AyoAi-Efs/mnt/AyoAi/
  Accounts/
    {account-uuid}/
      {environment-name}/
        {timestamp}_{id}/           # One per server session
          AyoServerEnvironment_OnStartup.json
          ServerGlobals.json
          TerminationNotes.json
          CharacterDefinitions.jsonl
          Tasks.jsonl
          Character_{id}_{id}_{timestamp}_OnTermination.json  # Per-character dump
          memory/
            BehaviorTrees/          # Per-character BT JSONL
            CellExecutionLog/       # Cell archive data
            ConflatedState/         # State snapshots
            Intent/                 # Intent decision logs
            PartialCells/           # In-progress cells
            PrivateNotes/           # Character notes
            SpatialMemory/          # Spatial memory maps
            StepTimestamp/          # Step timing data
            UnitStateChanges/       # State change events
            BTEvents/               # BT execution events
          logs/                     # Server logs
          SavedPrompts/             # LLM prompt/response pairs
          SavedAyoStreamUpdates/    # Roblox streaming data
  BitNet/                           # BitNet model files
  Jars/                             # Java JAR deployments
  Models/                           # ML models (RoBERTa, embeddings)
  Operator/                         # Operator data
```

## Usage Examples

```bash
# List accounts
Bash: world/scripts/efs-ssh.sh "ls /home/ec2-user/AyoAi-Efs/mnt/AyoAi/Accounts/"

# List server sessions for an environment
Bash: world/scripts/efs-ssh.sh "ls /home/ec2-user/AyoAi-Efs/mnt/AyoAi/Accounts/{account}/{env}/"

# Read character cell archive
Bash: world/scripts/efs-ssh.sh "cat /home/ec2-user/AyoAi-Efs/mnt/AyoAi/Accounts/{account}/{env}/{session}/memory/CellExecutionLog/{char}_CellArchive.jsonl | head -5"

# Download a file locally for analysis
Bash: world/scripts/efs-download.sh "/home/ec2-user/AyoAi-Efs/mnt/AyoAi/Accounts/{account}/{env}/{session}/memory/BehaviorTrees/{char}_BehaviorTrees.jsonl" "./data/bt.jsonl"
```

## Security

- Credentials loaded from `.env.local` via `world/scripts/_env.sh` (never written to disk)
- SSH key path: `EFS_SSH_KEY_PATH`, user: `EFS_SSH_USER`, host: `EFS_SSH_HOST`
- StrictHostKeyChecking=no (EC2 instances get new host keys on launch)

## Infra-Health Component

Component: `efs-ssh`. Probe: run `efs-ssh.sh "echo ok"`.

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The last `efs-ssh.sh`, `scp`, or `rsync` invocation is itself the terminal tool call
and satisfies this requirement. Never end with a text summary of what was fetched.
