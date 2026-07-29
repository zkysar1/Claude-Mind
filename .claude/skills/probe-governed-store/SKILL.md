---
name: probe-governed-store
description: "Reads, lists, or HEAD-probes a governed-store file S3-authoritatively through the configured storage backend using core/scripts/backend-cat.sh (subcommands: cat, list, head). MUST use this skill — never a raw multi-line python -c get_backend() incantation and never plain cat/ls on a governed mirror — whenever the agent needs to read a governed store S3-authoritative, verify a write propagated to the own-cloud store (S3 HEAD: ETag/md5 + size vs local disk), list sibling shards in a per-writer sharded store (e.g. world/team-state/agents/), discriminate hook-fire vs manual-push vs daemon-sweep propagation, or a governed file looks frozen/stale locally (identical-stale-across-readers tell). Fires when execution mentions backend-cat, force_fresh, list_dir, S3 HEAD probe, own-cloud authoritative read, mirror drift, or stale shard. Accepts world/ and meta/ virtual prefixes and absolute paths."
forged: true
forged_by: zeta
forged_date: "2026-07-11"
forged_from: gap-12
user-invocable: false
triggers:
  - "read governed store S3-authoritative"
  - "backend-cat"
  - "s3 head probe"
  - "verify write propagated to own-cloud"
  - "list sharded store fresh"
  - "mirror drift check"
parameters:
  - name: sub-command
    description: "cat {path} [--local] | list {dir} | head {path}"
    required: true
tools_used: [Bash]
companion_scripts: [core/scripts/backend-cat.sh]
conventions: []
---

# /probe-governed-store — S3-Authoritative Governed-Store Probe

Forged from gap-12 (2× encountered: g-115-1926 write-propagation probe,
g-115-1979 sharded-store read forensics). Replaces the recurring ~10-line
`python3 -c` ceremony (source `_paths.sh` + `cd core/scripts` +
`get_backend()` + absolute-path assembly) with one companion-script call.

## Why this exists

Under `STORAGE_BACKEND=own-cloud`, local files under `world/` and `meta/`
are MIRRORS, not the store. The mirror sweep is push-only, so:

- A local read can compose clone-era fossils (g-115-1979: sibling
  team-state shards never land locally — each box frozen at its own clone
  date). "Identical-stale-across-readers" is the tell.
- A local write can look landed while the store PUT failed, or vice versa
  (g-115-1926: manifest baseline is NOT a valid propagation proxy —
  wrong-discriminator trap).

The ONLY authoritative answer is a PURE store read
(`read_authoritative_bytes` — S3 GetObject straight to memory) or a
backend stat (S3 HEAD → ETag/size). This skill makes that the one-call
default. Do NOT substitute `read_text(force_fresh=True)`: that routes
through `_refresh`, which (a) downloads the object INTO the local mirror
(the rb-3128 read-side clobber) and (b) in the both-diverged `no_clobber`
state returns the LOCAL bytes while claiming freshness — wrong exactly
when the diagnostic matters (proven live, g-115-1987 receipt:
`meta/experiments/exp-backend-cat-noclobber-20260711.md`).

## Restricted Operations

MUST use `core/scripts/backend-cat.sh` for governed-store probes — never
raw `aws s3api` calls (credential ceremony, key-derivation drift risk) and
never ad-hoc `python3 -c "from storage_backend import ..."` incantations
(the exact recurring toil this skill retires; they also break without the
`_paths.sh` env + `core/scripts` import path, a 2× trap in the encounter
log).

## Procedure

```
# 1. Read a governed file S3-authoritative (pure to-memory read — never
#    mutates the local mirror, never falls back to local bytes):
Bash: bash core/scripts/backend-cat.sh cat world/team-state/agents/alpha.yaml

# 2. List a governed dir from the store (per-writer sharded stores):
Bash: bash core/scripts/backend-cat.sh list world/team-state/agents

# 3. Verify a write propagated / detect mirror drift:
Bash: bash core/scripts/backend-cat.sh head world/team-state.yaml
#    -> backend, exists, version (S3 ETag = content md5 for single-part),
#       size, and a local-mirror line: [match] | [DRIFT ...] | (no local
#       mirror file) | multipart-ETag n/a.

# 4. Local-mirror read (the mirror AS-IS — plain fs read, no cache refresh),
#    e.g. to compare against the store copy from #1:
Bash: bash core/scripts/backend-cat.sh cat <path> --local
```

Path forms: absolute; `world/...` / `meta/...` virtual prefixes (resolved
via `_paths.sh` per `.claude/rules/path-resolution.md` — never
cwd-relative); anything else resolves from PROJECT_ROOT.

## Output contract

- `cat`: raw file content on stdout (pipe into `head`, `python3 -c`, `jq`
  as needed).
- `list`: one entry name per line, sorted.
- `head`: `key: value` lines (backend, path, exists, version, size,
  [mtime], local drift verdict).
- Exit codes: 0 ok · 1 not-found / empty-dir / probe-failed · 2 usage.

## Interpreting `head`

| Signal | Meaning |
|---|---|
| `local: ... [match]` | Write propagated; mirror current. |
| `local: ... [DRIFT ...]` | Mirror differs from store — read-side staleness (g-115-1979 class) or an unpushed local write (g-115-1971/rb-3105 class). Investigate before trusting either copy. |
| `local: (no local mirror file)` | Store-only object — normal for sibling-agent shards on this box. |
| `exists: false` + exit 1 | Object absent from the STORE (authoritative negative — but per verify-before-assuming, pair with a second signal before concluding "never written"). |
| backend `local` | LocalBackend deployment — the file IS the store; drift compare is moot. |

## Error handling

- Missing file: clean exit 1 with the backend name in the diagnostic — do
  NOT retry blindly; check the path (virtual-prefix typo is the common
  cause).
- `WORLD_PATH`/`META_PATH` unset: exit with an explicit message —
  environment problem, not a store problem.
- The local-drift probe inside `head` is best-effort: its failure never
  masks the authoritative HEAD result.

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is the `backend-cat.sh` Bash call (or a follow-up Bash
echo handing control back to the orchestrator when invoked mid-iteration).
Never end with a text summary.
