#!/usr/bin/env python3
# domain-leak-exempt: own-cloud mirror sweep — the S3 / DynamoDB references are
# functional infrastructure for the own-cloud storage tier (this IS the sweep that
# pushes governed files to S3), not a domain leak. Sibling of owncloud_backend.py,
# which carries the same exemption for the same reason; the abstract storage seam
# (storage_backend.py) stays domain-free.
"""Mirror local governed-dir files to S3 under own-cloud — the structural fix for
B15 (writes that bypass the storage backend never reach the cloud, so they don't
sync cross-machine).

THE GAP (audited 2026-06-01)
  Under STORAGE_BACKEND=own-cloud, the backend's write methods
  (atomic_write / write_text / write_bytes / append_jsonl_record / write_jsonl)
  and _fileops.locked_* push to S3 with an If-Match fence. But ~45 RAW write
  sites — `Path.write_text`, `open(...,'w'/'a')`, `shutil.copy`, `os.replace`,
  `echo >` — across the daemon and standalone scripts persist LOCAL-ONLY. So do
  every LLM Write/Edit-tool write (knowledge node bodies, self.md, program.md,
  conventions, journals, experience bodies, temp docs). On one machine that is
  invisible; the moment a SECOND machine reads from S3, that state is missing or
  stale. "The entire point of the cloud is that it syncs across machines."

WHY A MIRROR SWEEP, NOT 45 CALL-SITE CONVERSIONS (deeper redesign, not quick win)
  Converting 45 raw writes to backend calls is fragile (easy to miss one, and
  every NEW raw write re-opens the gap) AND still misses the LLM-tool writes
  entirely (those aren't in our code). One content-hash mirror sweep over the
  governed roots catches EVERY write path — daemon, script, LLM, and any future
  one — by construction. It is the closed-by-design answer. A PostToolUse hook
  (sync-governed-write.sh) calls this in --file mode for real-time push of LLM
  writes; the periodic --all sweep is the catch-all for daemon/script raw writes.

MECHANISM (fence-safe, never clobbers local OR a peer's S3)
  For each governed file (minus the machine-local exclusions below):
    1. md5(local bytes)  vs  S3 ETag (HEAD).  Equal -> in sync, skip.
    2. Differ / absent on S3 -> classify against the per-file content BASELINE
       (the md5 this machine last reconciled with S3, from the manifest) and act:
         S3 absent ............................. new local content -> push
         local != baseline, S3 == baseline ..... local changed only -> push
         local == baseline, S3 != baseline ..... peer moved S3 -> STALE -> skip
         local != baseline, S3 != baseline ..... concurrent divergence -> skip+warn
         diverged, no baseline (multi-machine) . cannot prove authority -> skip+warn
         diverged, no baseline (single-machine)  local IS authoritative -> push
       A push is backend.mirror_put(path, local_bytes, expected_version=etag): a
       fenced (If-Match) PUT that does NOT download first (downloading would
       overwrite the locally-NEWER file with the older remote — the exact wrong
       move). A concurrent backend write that moved the object raises
       ConflictError -> skip (next sweep reconciles).
  ETag==md5 only for single-part PUTs (all our files are small); a multipart
  ETag ("...-N") can't be compared so the file is treated as differing -> PUT
  (correct, just not skip-optimised).

  A machine-local manifest (RUNTIME_DIR/owncloud-sync-manifest.json) stores
  {rel_key: {mtime, md5}}: the mtime lets recurring sweeps skip files unchanged
  since their last confirmed sync (a 10-minute cadence does not HEAD thousands of
  files each time); the md5 is the content BASELINE the step-2 classifier uses to
  tell a genuine local write from a stale cache. Pre-H4 manifests stored a bare
  mtime int — read tolerantly (md5 absent -> no baseline). --full ignores the
  manifest (re-HEADs everything); the baseline run uses it implicitly (empty
  manifest -> HEADs all).

MULTI-MACHINE SAFETY (H4 — the machine-2 gate)
  The sweep is LOCAL-AUTHORITATIVE: correct on one machine, a silent-clobber risk
  on a second, where the local copy of a file this machine only READ is a CACHE of
  S3 that a peer may have moved. Two defenses, both off by default (single-machine
  back-compat) and armed by env:
    - MACHINE_OWNED_AGENTS="alpha,bravo"  (H4a) — the agents THIS machine runs. The
      sweep prunes agents/<name>/ for every name NOT listed: a peer agent's dir is
      a cache of the OWNING machine's writes, never to be pushed from here. Unset
      => own all agents (single-machine default).
    - the content baseline (H4b, above) guards world/ and meta/ (shared by all
      machines): a file at its baseline locally while S3 moved is a stale cache,
      skipped — not pushed over the peer's newer bytes. MACHINE_MULTI=1 (or
      any MACHINE_OWNED_AGENTS value) also makes a diverged file with NO baseline
      skip rather than push (cannot prove local authority); the PostToolUse
      single-file path still pushes genuine local writes in real time.
  The If-Match fence ALONE does not prevent stale-clobber: it fences on the
  just-observed CURRENT etag, so a stale-local PUT succeeds against current S3 and
  overwrites the newer remote. The baseline is what closes that hole.

MACHINE-LOCAL EXCLUSIONS (legitimately do NOT sync)
  - sessions/ (plural)        per-Claude-SID scratch dirs — walk-pruned; ephemeral
                              per-session working dirs cleaned by cleanup-stale-bindings.sh
  - session/ (singular)       NO LONGER blanket-excluded (session-continuity
                              redesign). Sync-by-default with a manifest-driven
                              machine-local denylist + continuity pull-set. SSOT is
                              core/config/session-manifest.yaml's sync_tier (see
                              _load_session_tiers / _session_file_machine_local):
                              liveness/identity files stay local; accumulated
                              knowledge (handoff, working-memory, pending-questions,
                              execution-diary, …) syncs and is pulled onto a new
                              machine at /start. session/scratch/ is walk-pruned.
  - .history/                 local corruption-recovery snapshots; S3 IS the
                              durable cross-machine copy (bucket versioning)
  - presence/ , *.lock        coordination is via DynamoDB, not S3 files
  - daemon.port/pid, *.sock   per-machine runtime
  - local-paths.conf, .env*   per-machine config (different external paths/creds)
  - changelog.jsonl, world/*-log.jsonl (log_script_decision), *-telemetry.jsonl,
    .fallback-stats.jsonl     per-machine append logs — multi-machine needs the
                              per-machine-suffix + aggregation design (deferred,
                              see lodestar-bug-master-list B15 / task). NOTE
                              meta/evolution-log.jsonl + meta/meta-log.jsonl are
                              DOMAIN audit and DO sync (the *-log.jsonl exclusion
                              is scoped to the world/ root only).

USAGE
  set -a; source .env.local; set +a            # STORAGE_BACKEND + scoped creds
  source core/scripts/_paths.sh                # exports WORLD_PATH/META_PATH
  export WORLD_PATH MIND_WORLD="$WORLD_PATH"   # from_env reads these
  # preview the full baseline:
  py -3 core/scripts/owncloud_sync.py --all --full --dry-run
  # push everything to S3:
  py -3 core/scripts/owncloud_sync.py --all --full
  # real-time single-file push (the PostToolUse hook):
  py -3 core/scripts/owncloud_sync.py --file "<abs governed path>"
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# --- machine-local exclusion policy ---------------------------------------
# Directory names pruned from the walk entirely (never descended into).
# NOTE: "session" (singular) is intentionally NOT excluded as of the session-
# continuity redesign — <agent>/session/ is now sync-by-default with a manifest-
# driven machine-local denylist (see _session_file_machine_local). "sessions"
# (plural, per-Claude-SID scratch dirs) STAYS excluded — those are ephemeral
# per-session working dirs cleaned by cleanup-stale-bindings.sh.
_EXCLUDE_DIRS = {
    "sessions", ".history", "presence",
    "__pycache__", ".git", "node_modules", ".locks", ".pytest_cache",
}
# Exact basenames never synced (per-machine config / runtime / append logs).
_EXCLUDE_NAMES = {
    "changelog.jsonl",
    ".fallback-stats.jsonl",
    "file-contention-telemetry.jsonl",
    "history-save-telemetry.jsonl",
    "local-paths.conf",
    "daemon.port", "daemon.pid", "daemon.lock",
    ".DS_Store", "Thumbs.db",
}
# Basename glob patterns never synced.
_EXCLUDE_GLOBS = ("*.lock", "*.pyc", "*.tmp", "*.swp", "*~", "*.sock",
                  "*.pid", "*.port", ".env*")


def _is_machine_local(basename: str, prefix: str, *, full_path=None, root_path=None) -> bool:
    """True iff this file is per-machine and must NOT be mirrored to S3.
    `prefix` is the logical root prefix ('world' / 'meta' / 'agents').
    `full_path` + `root_path` (optional) enable per-file <agent>/session/
    classification via the session manifest — callers that have the resolved
    path pass them; the 2-arg form keeps the basename-only policy (used by
    tests and any caller without path context)."""
    if basename in _EXCLUDE_NAMES:
        return True
    for g in _EXCLUDE_GLOBS:
        if fnmatch.fnmatch(basename, g):
            return True
    # log_script_decision writes world/<script-name>-log.jsonl per machine; scope
    # the *-log.jsonl exclusion to the world root so meta/evolution-log.jsonl and
    # meta/meta-log.jsonl (domain audit) still sync.
    if prefix == "world" and fnmatch.fnmatch(basename, "*-log.jsonl"):
        return True
    # Session-file policy (sync-by-default + manifest denylist). Only applies to
    # files under <agent>/session/ (singular) and only when the caller supplied
    # the resolved path so we can detect the session/ segment.
    if prefix == "agents" and full_path is not None and root_path is not None:
        try:
            rel_parts = Path(full_path).relative_to(root_path).parts
        except ValueError:
            rel_parts = ()
        # rel_parts like ('alpha', 'session', 'agent-state'). 'sessions' (plural)
        # never reaches here — it is walk-pruned via _EXCLUDE_DIRS.
        if len(rel_parts) >= 2 and rel_parts[1] == "session":
            return _session_file_machine_local(basename, rel_parts)
    return False


def refresh_would_clobber(be, target) -> bool:
    """True iff a backend refresh of `target` would overwrite a per-machine file
    with stale/empty remote data.

    A `backend.refresh()` force-pulls the remote copy over the local file before
    an in-lock read. That is correct for a SYNCED store (S3 holds the
    authoritative copy) but a DATA-LOSS path for a per-machine store: such files
    are never pushed to S3, so the pull overwrites the only good (local) copy
    with whatever stale/empty object the remote happens to hold (guard-881; the
    presence clobber, g-333-09). Callers guard `refresh()` with this predicate.

    A file is per-machine (=> refresh unsafe => return True) when, under its
    governing root, EITHER a directory segment is in `_EXCLUDE_DIRS` (presence/,
    .history/, sessions/, ... -- pruned from the sync walk, so never pushed) OR
    `_is_machine_local` matches the basename policy (EXCLUDE_NAMES / EXCLUDE_GLOBS
    / world *-log.jsonl / the <agent>/session manifest). That union is exactly
    the never-pushed set the sync walk (sync_all / sync_file) computes -- the
    SSOT for sync candidacy lives here, not in the caller. The dir-segment half
    is load-bearing: `_is_machine_local` alone returns False for
    `world/presence/<agent>.jsonl` (basename not excluded), so a refresh guard
    keyed only on `_is_machine_local` would still clobber the presence store
    (verified 2026-06-26: all 6 presence files classify dir-excluded but
    basename-synced).

    Returns False (refresh is safe) when: the active backend has no remote
    (LocalBackend -- `refresh` is a no-op, clobber impossible), or `target` is
    not under any governed root (core/, .claude/, product repos are git-synced,
    never S3). A PEER agent's file under a governed root is NOT skipped -- its
    local copy is a stale cache, so pulling the owner's authoritative S3 copy is
    the CORRECT outcome (H4a only blocks PUSHing a peer file, never the pull)."""
    roots = getattr(be, "_roots", None)
    if not roots:
        return False  # LocalBackend (no-op refresh) or no governed roots
    target = Path(target).resolve()
    for root_path, prefix in roots:
        try:
            rel = target.relative_to(root_path)
        except ValueError:
            continue
        # Directory-level exclusion (walk-pruned dirs are never pushed). Check
        # only the directory segments, never the basename.
        if any(seg in _EXCLUDE_DIRS for seg in rel.parts[:-1]):
            return True
        return _is_machine_local(target.name, prefix, full_path=target,
                                 root_path=Path(root_path))
    return False  # ungoverned root


# --- session-file sync policy (SSOT: core/config/session-manifest.yaml) -----
# <agent>/session/ is sync-by-default: each file's cross-machine disposition is
# read from the manifest's sync_tier (machine_local | continuity | ephemeral).
# UNREGISTERED session files fail SAFE — synced ONLY if they carry a known data
# extension; an extensionless/unknown-extension file (almost always a liveness
# signal) stays local, so a not-yet-classified file can never sync a phantom
# runner onto a second machine. If the manifest is unreadable/invalid, ALL
# session files are treated as machine-local (the safe pre-redesign behavior).
# The orphan detector in session_desync_check flags unregistered files for
# explicit classification (the loud half of the drift defense).
_SESSION_DATA_EXTS = {".yaml", ".yml", ".json", ".jsonl", ".txt", ".md", ".csv", ".tsv"}
_SESSION_TIERS = None            # cached (exact_dict, glob_list) | None (= fail-safe)
_SESSION_TIERS_LOADED = False


def _load_session_tiers():
    """Parse session-manifest.yaml -> ({basename: tier}, [(glob, tier)]).
    Returns None on ANY failure (unreadable, unparseable, or an entry missing a
    valid sync_tier) so callers fail closed -> machine_local. Cached per process
    (the manifest is a static framework file within a run)."""
    global _SESSION_TIERS, _SESSION_TIERS_LOADED
    if _SESSION_TIERS_LOADED:
        return _SESSION_TIERS
    _SESSION_TIERS_LOADED = True
    try:
        import yaml
        mpath = (Path(__file__).resolve().parents[2]
                 / "core" / "config" / "session-manifest.yaml")
        data = yaml.safe_load(mpath.read_text(encoding="utf-8"))
        exact, globs = {}, []
        for e in data["files"]:
            name = e.get("file")
            tier = e.get("sync_tier")
            if not name or tier not in ("machine_local", "continuity", "ephemeral"):
                _SESSION_TIERS = None     # untrustworthy manifest -> fail closed
                return None
            if e.get("glob"):
                globs.append((name, tier))
            else:
                exact[name] = tier
        _SESSION_TIERS = (exact, globs)
    except Exception:
        _SESSION_TIERS = None
    return _SESSION_TIERS


def _session_file_machine_local(basename: str, rel_parts) -> bool:
    """machine-local decision for a file under <agent>/session/.
    `rel_parts`: path parts below the agents root, e.g.
    ('alpha', 'session', 'agent-state') or (..., 'session', 'scratch', 'x.json')."""
    # scratch/ is the machine-local ad-hoc workspace (also walk-pruned in sweep).
    if "scratch" in rel_parts[2:]:
        return True
    tiers = _load_session_tiers()
    if tiers is None:
        return True                       # fail closed -> safe pre-redesign behavior
    exact, globs = tiers
    tier = exact.get(basename)
    if tier is None:
        for pat, t in globs:
            if fnmatch.fnmatch(basename, pat):
                tier = t
                break
    if tier == "machine_local":
        return True
    if tier in ("continuity", "ephemeral"):
        return False
    # UNREGISTERED -> fail-safe heuristic: sync only known data extensions; an
    # extensionless / unknown-extension file (signal-shaped) stays local.
    return Path(basename).suffix.lower() not in _SESSION_DATA_EXTS


# --- manifest (machine-local mtime cache to skip unchanged files) ----------
def _runtime_dir() -> Path:
    rd = os.environ.get("RUNTIME_DIR")
    if rd:
        return Path(rd)
    return Path(__file__).resolve().parents[2] / "mind_api" / "state"


def _manifest_path() -> Path:
    return _runtime_dir() / "owncloud-sync-manifest.json"


def _load_manifest() -> dict:
    p = _manifest_path()
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return {}


def _save_manifest(m: dict) -> None:
    # Atomic write (unique temp in the same dir + os.replace). The manifest now
    # has TWO concurrent in-daemon writers under own-cloud — the periodic sweep
    # thread (__main__._start_owncloud_sync_thread) and the on-demand flush
    # endpoint (POST /v1/admin/owncloud-flush), each on its own thread, both
    # calling sweep()->_save_manifest. A plain write_text() is non-atomic, so a
    # concurrent _load_manifest() could read a TRUNCATED file (corruption window)
    # and a second writer could collide mid-write on Windows. mkstemp gives each
    # writer a unique temp so they never clobber each other's temp; os.replace is
    # atomic on POSIX and Windows, so readers always see a COMPLETE manifest and
    # last-writer-wins cleanly (a few extra re-HEADs next tick at worst — the
    # manifest is a local mtime-skip cache, never the SSOT; S3 is). Pattern per
    # the `atomic-primitives` tree node (.tmp + replace for concurrently-read files).
    p = _manifest_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"[sync] WARN: could not create manifest dir {p.parent}: {e}",
              file=sys.stderr)
        return
    tmp_fd = None
    tmp_path = None
    try:
        tmp_fd, tmp_name = tempfile.mkstemp(
            dir=str(p.parent), prefix=".owncloud-sync-manifest.", suffix=".tmp")
        tmp_path = Path(tmp_name)
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            tmp_fd = None  # fdopen owns the descriptor now
            f.write(json.dumps(m))
        os.replace(tmp_name, p)
        tmp_path = None  # replaced successfully — nothing to clean up
    except OSError as e:
        print(f"[sync] WARN: could not persist manifest {p}: {e}",
              file=sys.stderr)
    finally:
        if tmp_fd is not None:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


# --- multi-machine ownership + freshness (H4 — machine-2 gate) -------------
# OWNERSHIP_STALE_SECONDS (lodestar §9 / guard-594): a RUNNING claim whose
# heartbeat_at is older than this is a CRASHED peer, not a live owner — so the
# sweep stops deferring to its (now-stale) S3 state and the peer-side reclaim
# (reclaim_if_stale, §5) can flip it to IDLE. Starting value = the architecture-doc
# H5 15-minute threshold (900s); env-overridable for calibration against observed
# heartbeat_at deltas (the heartbeat advances once per loop iteration via
# iteration-close.sh). Read at call-time so calibration needs no process restart.
_OWNERSHIP_STALE_SECONDS_DEFAULT = 900


def _owned_agents_static():
    """The set of agent names THIS machine runs, from MACHINE_OWNED_AGENTS
    (comma-separated). None => own ALL agents (single-machine default / unset).

    On a SECOND machine the local copy of a PEER agent's dir is a stale CACHE of
    the owning machine's S3 writes, so this local-authoritative mirror sweep must
    NOT push it — doing so clobbers the peer's newer S3 bytes (and the If-Match
    fence does not stop it: the sweep fences on the just-observed CURRENT etag, so
    a stale-local PUT succeeds). When set, the sweep prunes agents/<name>/ for
    every name NOT listed."""
    raw = os.environ.get("MACHINE_OWNED_AGENTS", "").strip()
    if not raw:
        return None
    return {a.strip() for a in raw.split(",") if a.strip()}


def _owned_agents_runner_token(be, root_path: Path):
    """Runner-token ownership detection for own-cloud multi-machine setups.

    Scans agents/<name>/session/runner-token locally. For each local token:
      1. Reads the S3 copy via be.read_bytes().
      2. If S3 is absent (bootstrap) or matches → this machine owns the agent;
         pushes the local token to S3 as the authoritative claim.
      3. If S3 has a different token → another machine started that agent last;
         skip (do not push, do not prune-protect).

    Returns:
      - None  if the agents dir doesn't exist (single-machine / pre-init layout).
      - None  if no runner-token files are found locally — can't determine
              multi-machine context; caller falls back to static
              MACHINE_OWNED_AGENTS (backward-compatible for IDLE agents and
              machines that have never run /start).
      - set() if at least one local runner-token was found but NONE matched S3
              (another machine owns all active agents; this machine owns none).
      - {names} if one or more agents' tokens match S3 (or S3 was absent).

    The "found any token" distinction matters: returning None vs set() controls
    whether the caller falls to static env-var ownership. Without a local token
    we have no multi-machine signal — returning None is safer than claiming
    zero ownership, which would prevent ANY agent dir from syncing on a single-
    machine setup where /start has never been run.

    The S3 comparison handles the concurrent-start race: the machine that wrote
    last to S3 wins. Fail-open on S3 I/O errors (claim ownership) so single-
    machine setups without MACHINE_OWNED_AGENTS keep working.

    runner-token is machine_local in session-manifest.yaml (the sweep never
    touches it), so this function handles its own S3 I/O via be.read_bytes /
    be.write_bytes outside the normal sweep path.

    root_path is the agents prefix root (the directory that contains alpha/,
    bravo/, etc. directly) — NOT the parent directory. Callers must pass the
    "agents"-prefixed root from _roots(), not the repo root."""
    if not root_path.is_dir():
        return None
    owned: set = set()
    found_any_token = False
    try:
        agent_dirs = sorted(root_path.iterdir())
    except OSError:
        return None
    for agent_dir in agent_dirs:
        if not agent_dir.is_dir():
            continue
        token_path = agent_dir / "session" / "runner-token"
        if not token_path.exists():
            continue
        try:
            local_token = token_path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not local_token:
            continue
        found_any_token = True
        agent_name = agent_dir.name
        s3_key = f"agents/{agent_name}/session/runner-token"
        try:
            s3_bytes = be.read_bytes(s3_key)
            if s3_bytes is not None:
                s3_token = s3_bytes.decode("utf-8", errors="replace").strip()
                if s3_token != local_token:
                    # Another machine has a newer claim — do not own.
                    continue
            # S3 absent (bootstrap) or matches → own it; push claim.
            be.write_bytes(s3_key, local_token.encode("utf-8"))
        except Exception:
            # S3 unavailable — fail-open (claim ownership).
            pass
        owned.add(agent_name)
    # No local tokens found → can't assert multi-machine context; caller falls
    # back to static MACHINE_OWNED_AGENTS (backward-compatible behavior).
    if not found_any_token:
        return None
    return owned


def _owned_agents(be=None):
    """Resolve the agent dirs this machine owns for the sweep. Gated by
    OWNERSHIP_MODE (default '' => runner-token when be is provided, else static):

    runner-token (default when OWNERSHIP_MODE is unset and be is available):
      Compare agents/<name>/session/runner-token to the S3 copy. Zero-config:
      no env list to maintain; /start writes the token, the sweep reads it.
      Eliminates MACHINE_OWNED_AGENTS for own-cloud setups.

    dynamic (OWNERSHIP_MODE='dynamic', INERT until g-115-1340 cutover):
      Live RUNNING DDB runner claims for this machine_id.

    static (OWNERSHIP_MODE='static'):
      Legacy MACHINE_OWNED_AGENTS env list.

    Returns:
      None  — single-machine / unset → own ALL agents.
      set   — names this machine currently runs (may be empty → own none).

    FAIL-SAFE: on any resolution error, falls back to static env list (never
    own-all on failure — conservative, never clobbers a peer)."""
    mode = os.environ.get("OWNERSHIP_MODE", "").strip().lower()

    if mode == "dynamic":
        # Dynamic path — INERT until g-115-1340 cutover. Own-cloud only.
        kind = os.environ.get("STORAGE_BACKEND", "local").strip().lower()
        if kind != "own-cloud":
            return None
        try:
            stale = int(os.environ.get("OWNERSHIP_STALE_SECONDS",
                                       _OWNERSHIP_STALE_SECONDS_DEFAULT))
        except (TypeError, ValueError):
            stale = _OWNERSHIP_STALE_SECONDS_DEFAULT
        try:
            from storage_backend import get_backend
            be_dyn = get_backend()
            # be_dyn.machine_id is the value acquire_runner stamped onto the claim
            # (SSOT), so the resolver's 'me' matches the claim's machine_id exactly.
            me = getattr(be_dyn, "machine_id", None)
            if not me or me == "unknown":
                return _owned_agents_static()
            claims = be_dyn.list_runner_claims()
        except Exception as exc:
            print(f"[sync] OWNERSHIP_MODE=dynamic: live claim read failed "
                  f"({type(exc).__name__}: {exc}); falling back to static "
                  "MACHINE_OWNED_AGENTS (conservative — never own-all on failure).",
                  file=sys.stderr)
            return _owned_agents_static()
        now = time.time()
        return {
            c.agent for c in claims
            if c.machine_id == me
            and c.agent_state == "RUNNING"
            and (now - c.heartbeat_at) < stale
        }

    if mode == "static":
        return _owned_agents_static()

    # Default / "runner-token": auto-detect via S3 runner-token comparison.
    if be is not None:
        for rp, pfx in _roots(be, None):
            if pfx == "agents":
                result = _owned_agents_runner_token(be, rp)
                if result is not None:
                    return result
                break  # agents root found but no runner-tokens → fall through

    # Fallback: static env list (covers MACHINE_OWNED_AGENTS legacy and the
    # no-be / local-backend case where runner-token comparison is impossible).
    return _owned_agents_static()


def _multi_machine() -> bool:
    """True when this process shares the env with other machines. Declaring
    MACHINE_OWNED_AGENTS is the signal (a single machine owns all agents and leaves
    it unset); MACHINE_MULTI=1 forces it on. Enables the conservative
    no-baseline stale-skip in _sync_one (cannot prove local authority => do not
    risk clobbering a peer)."""
    if os.environ.get("MACHINE_MULTI", "").strip().lower() in (
            "1", "true", "yes"):
        return True
    return _owned_agents() is not None


def _manifest_entry(v):
    """(mtime_ns, md5) from a manifest value, tolerant of the legacy int form
    (pre-H4 manifests stored just the mtime; treat md5 as absent => no
    baseline)."""
    if isinstance(v, dict):
        return v.get("mtime"), v.get("md5")
    if isinstance(v, int):
        return v, None
    return None, None


# --- backend wiring --------------------------------------------------------
def _require_owncloud_backend():
    kind = os.environ.get("STORAGE_BACKEND", "local").strip().lower()
    if kind != "own-cloud":
        print(f"[sync] backend is {kind!r}, not 'own-cloud' — nothing to mirror "
              "to S3 (the local files ARE the store under the local backend). "
              "No-op.", file=sys.stderr)
        return None
    from storage_backend import get_backend
    be = get_backend()
    if not hasattr(be, "mirror_put"):
        print("[sync] ERROR: backend has no mirror_put — wrong backend or stale "
              "owncloud_backend.py.", file=sys.stderr)
        sys.exit(2)
    return be


def _etag_matches(etag: str, local_md5: str) -> bool:
    """ETag == md5 only for single-part PUTs. A multipart ETag ('...-N') cannot
    be compared, so report mismatch (forces a PUT — safe, just not optimal)."""
    e = (etag or "").strip('"')
    if "-" in e:
        return False
    return e == local_md5


def _etag_is_multipart(etag: str) -> bool:
    """A multipart ETag ('<hex>-N') is not an md5 of the object, so it cannot be
    compared to a content md5. The baseline classifier must treat such an object
    as UNCOMPARABLE rather than 'differs from baseline' — otherwise a file whose
    S3 ETag merely became multipart (e.g. a server-side copy or replication, bytes
    unchanged) is misread as a STALE cache or a CONFLICT, silently dropping a
    genuine local write. Our own PUTs are always single-part (put_object), so this
    only fires on an externally-introduced multipart ETag."""
    return "-" in (etag or "").strip('"')


# --- core: one file --------------------------------------------------------
def _sync_one(be, full: Path, *, dry_run: bool, stats: dict,
              baseline_md5=None, multi_machine: bool = False):
    """HEAD-compare one governed file and decide push / skip.

    Returns the md5 to record as the new baseline (on push or in-sync), or None
    when nothing was pushed (skip / stale / conflict / error / dry-run).

    Machine-2 safety (H4): the sweep is LOCAL-AUTHORITATIVE — correct on one
    machine, a clobber risk on a second. A file this machine merely READ (cached
    from S3) that a peer then moved would otherwise be pushed STALE over the
    peer's newer bytes; the If-Match fence does NOT stop this (it fences on the
    just-observed CURRENT etag, so the stale PUT succeeds). The `baseline_md5`
    (content this machine last reconciled with S3, from the manifest)
    distinguishes the cases:

      local == S3                        -> in sync                 (record baseline)
      S3 absent                          -> new local content       (push)
      local != baseline, S3 == baseline  -> local changed only      (push)
      local == baseline, S3 != baseline  -> peer moved S3 -> STALE  (skip, no clobber)
      local != baseline, S3 != baseline  -> concurrent divergence   (skip + warn)
      diverged, no baseline:
          multi-machine -> skip + warn (cannot prove local authority)
          single-machine -> push (legacy; local IS authoritative here)
      S3 ETag multipart (uncomparable to an md5):
          multi-machine -> skip + warn (defer; never clobber)
          single-machine -> push (no peer to clobber)
    """
    try:
        local_bytes = full.read_bytes()
    except OSError as e:
        print(f"[sync] WARN: unreadable {full}: {e}", file=sys.stderr)
        stats["errors"] += 1
        return None
    local_md5 = hashlib.md5(local_bytes).hexdigest()
    try:
        st = be.stat(full)  # S3 HEAD; None if absent
    except Exception as e:  # noqa: BLE001 — network/credential issues -> count + go on
        print(f"[sync] WARN: stat failed for {full}: {e}", file=sys.stderr)
        stats["errors"] += 1
        return None

    # In sync -> nothing to push; record the agreed content as the new baseline.
    if st is not None and _etag_matches(st.version, local_md5):
        stats["in_sync"] += 1
        return local_md5

    # Diverged. A multipart S3 ETag is uncomparable to a content md5 — neither the
    # in-sync check above nor the baseline check below is valid for it. Treat it as
    # UNCOMPARABLE: on a multi-machine setup a peer may own the object, so DEFER
    # (never clobber) rather than mis-label it STALE/CONFLICT and drop a genuine
    # local write. Single-machine: no peer to clobber -> local is authoritative.
    if st is not None and _etag_is_multipart(st.version):
        if multi_machine:
            stats["multipart_deferred"] = stats.get("multipart_deferred", 0) + 1
            print(f"[sync] skip (S3 ETag is multipart — cannot classify vs "
                  f"baseline; deferring, no clobber): {full}", file=sys.stderr)
            return None
        # single-machine: fall through to push below (local authoritative)
    # When S3 is PRESENT (and comparable), classify against the baseline before
    # any push so a stale cache or a true conflict never clobbers the peer.
    elif st is not None:
        local_at_baseline = baseline_md5 is not None and local_md5 == baseline_md5
        s3_at_baseline = (baseline_md5 is not None
                          and _etag_matches(st.version, baseline_md5))
        if local_at_baseline and not s3_at_baseline:
            # Local untouched since last sync, S3 moved -> peer wrote -> STALE.
            stats["stale_skipped"] = stats.get("stale_skipped", 0) + 1
            print(f"[sync] skip (stale local vs newer S3 — peer wrote): {full}",
                  file=sys.stderr)
            return None
        if baseline_md5 is not None and not local_at_baseline and not s3_at_baseline:
            # Both moved since the baseline -> concurrent divergence. Do NOT
            # auto-clobber either side; surface it for reconciliation.
            stats["diverged_skipped"] = stats.get("diverged_skipped", 0) + 1
            print(f"[sync] skip (CONFLICT — local and S3 both changed since "
                  f"baseline): {full}", file=sys.stderr)
            return None
        if baseline_md5 is None and multi_machine:
            # No baseline to prove this machine authored the local content, and a
            # peer may own it. Conservative: do not push. A genuine local
            # raw-write is still pushed in real time by the PostToolUse
            # single-file path (sync_file), which KNOWS it was a local write.
            stats["nobaseline_skipped"] = stats.get("nobaseline_skipped", 0) + 1
            print(f"[sync] skip (diverged, no baseline, multi-machine — cannot "
                  f"prove local authority): {full}", file=sys.stderr)
            return None
        # else: (local changed, S3 still at baseline) OR (single-machine,
        #        no baseline) -> local-authoritative content -> push below.

    # Push: S3 absent (new content) or a confirmed local-authoritative change.
    if dry_run:
        stats["would_push"] += 1
        stats["push_paths"].append(str(full))
        return None
    expected = st.version if st is not None else None
    try:
        from owncloud_backend import ConflictError
    except Exception:  # pragma: no cover
        ConflictError = ()  # type: ignore
    try:
        be.mirror_put(full, local_bytes, expected_version=expected)
        stats["pushed"] += 1
        return local_md5
    except ConflictError:
        # Concurrent backend write moved the object between our HEAD and PUT —
        # it synced its own bytes; the next sweep reconciles ours if divergent.
        print(f"[sync] skip (concurrent write): {full}", file=sys.stderr)
        stats["conflicts"] += 1
        return None
    except Exception as e:  # noqa: BLE001
        print(f"[sync] WARN: PUT failed for {full}: {e}", file=sys.stderr)
        stats["errors"] += 1
        return None


# --- core: sweep -----------------------------------------------------------
def _roots(be, only_root: str | None):
    """[(abs_root_path, prefix)] from the backend, optionally filtered."""
    rs = [(Path(r), prefix) for r, prefix in be._roots]
    if only_root:
        rs = [(r, p) for r, p in rs if p == only_root]
    return rs


def sweep(be, *, only_root, dry_run, use_manifest, full, only_agent=None):
    # Load the manifest whenever it is enabled — even on --full. --full disables
    # only the MTIME-skip optimization (the `not full` guard below); it must keep
    # the per-file content BASELINE so the stale-cache classifier still works.
    # (--full is the dangerous path: it re-HEADs unchanged-local files, which on a
    # second machine are caches a peer may have moved — without the baseline it
    # would clobber them.)
    manifest = _load_manifest() if use_manifest else {}
    new_manifest = dict(manifest)
    owned = _owned_agents(be=be)   # H4a: agent dirs this machine owns (None=all)
    # H4b/H4c: the flag _sync_one reads as "cannot prove local authority". The
    # periodic sweep NEVER knows per-file authorship (unlike sync_file, which
    # fires right after THIS machine wrote the file). It must therefore DEFER
    # no-baseline / uncomparable content whenever local authority is unprovable:
    #   - multi-machine (original H4b): a peer may own the file; AND
    #   - own-cloud (H4c, g-115-1333): S3 is the authoritative store and local
    #     files are caches / lazy-rehydrations. A transplant re-runs init-mind,
    #     which writes ~29 default meta files locally with NO baseline; pushing
    #     those over the learned S3 state would CLOBBER it. S3-ABSENT still
    #     pushes (first bootstrap) — _sync_one's S3-absent branch never reaches
    #     this guard. The real-time sync_file path proves authorship and keeps
    #     passing multi_machine=False, so genuine local writes still mirror.
    mm = _multi_machine() or (
        os.environ.get("STORAGE_BACKEND", "local").strip().lower() == "own-cloud")
    stats = {"scanned": 0, "in_sync": 0, "pushed": 0, "would_push": 0,
             "conflicts": 0, "errors": 0, "skipped_unchanged": 0,
             "stale_skipped": 0, "diverged_skipped": 0, "nobaseline_skipped": 0,
             "multipart_deferred": 0, "pruned_agents": 0, "push_paths": []}
    for root_path, prefix in _roots(be, only_root):
        if not root_path.exists():
            print(f"[sync] root absent (skipped): {root_path}", file=sys.stderr)
            continue
        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIRS]
            # H4a: at the agents-root level, prune agent dirs this machine does
            # NOT own — the local copy of a peer's dir is a stale cache of THEIR
            # machine's S3 writes, and pushing it would clobber the peer.
            if (prefix == "agents" and Path(dirpath) == root_path
                    and (owned is not None or only_agent is not None)):
                keep = list(dirnames)
                # owned-prune: never push a peer's dir (None=own-all => no prune).
                if owned is not None:
                    keep = [d for d in keep if d in owned]
                # per-agent scope (§6 /stop flush): narrow to exactly one agent
                # dir. Applied AFTER the owned-prune so an unowned --agent target
                # yields an empty walk (it was already dropped above) — never a
                # peer clobber, even if --agent names a dir this machine does not
                # own. guard-675: agents/<X>/ subdirs in _EXCLUDE_DIRS are still
                # walk-pruned at deeper levels by the dirnames filter above.
                if only_agent is not None:
                    keep = [d for d in keep if d == only_agent]
                stats["pruned_agents"] += len(dirnames) - len(keep)
                dirnames[:] = keep
            # Session redesign: prune <agent>/session/scratch/ — the machine-local
            # ad-hoc workspace — so the walk never descends into it. session/
            # itself IS walked now (sync-by-default per the manifest).
            if prefix == "agents" and os.path.basename(dirpath) == "session":
                dirnames[:] = [d for d in dirnames if d != "scratch"]
            for fn in filenames:
                full_path = Path(dirpath) / fn
                if _is_machine_local(fn, prefix, full_path=full_path,
                                     root_path=root_path):
                    continue
                if full_path.is_symlink():
                    continue
                stats["scanned"] += 1
                rel_key = f"{prefix}/{full_path.relative_to(root_path).as_posix()}"
                try:
                    mtime_ns = full_path.stat().st_mtime_ns
                except OSError:
                    mtime_ns = None
                base_mtime, base_md5 = _manifest_entry(manifest.get(rel_key))
                # manifest skip: unchanged (same mtime) since last confirmed sync
                if (not full and use_manifest and mtime_ns is not None
                        and base_mtime == mtime_ns):
                    stats["skipped_unchanged"] += 1
                    continue
                new_md5 = _sync_one(be, full_path, dry_run=dry_run, stats=stats,
                                    baseline_md5=base_md5, multi_machine=mm)
                # Record {mtime, md5} only when a baseline was confirmed (pushed
                # or in_sync). Skips/conflicts return None -> keep the old
                # baseline so the next sweep re-evaluates instead of trusting a
                # stale cache.
                if not dry_run and mtime_ns is not None and new_md5 is not None:
                    new_manifest[rel_key] = {"mtime": mtime_ns, "md5": new_md5}
    if not dry_run and use_manifest:
        _save_manifest(new_manifest)
    return stats


# --- single-file mode (the PostToolUse hook) -------------------------------
def sync_file(be, target: Path, *, dry_run) -> int:
    target = target.resolve()
    # under a governed root?
    prefix = None
    matched_root = None
    for root_path, pfx in be._roots:
        try:
            target.relative_to(root_path)
            prefix = pfx
            matched_root = Path(root_path)
            break
        except ValueError:
            continue
    if prefix is None:
        # Not governed (core/, .claude/, product repos are git-synced, not S3).
        return 0
    if _is_machine_local(target.name, prefix, full_path=target, root_path=matched_root):
        return 0
    # H4a: never push a PEER agent's file — its local copy is a stale cache of
    # the owning machine's S3 writes (this machine does not run that agent).
    owned = _owned_agents(be=be)
    if prefix == "agents" and owned is not None and matched_root is not None:
        parts = target.relative_to(matched_root).parts
        if parts and parts[0] not in owned:
            return 0
    if not target.exists() or target.is_dir():
        return 0
    stats = {"scanned": 1, "in_sync": 0, "pushed": 0, "would_push": 0,
             "conflicts": 0, "errors": 0, "stale_skipped": 0,
             "diverged_skipped": 0, "nobaseline_skipped": 0,
             "multipart_deferred": 0, "push_paths": []}
    # Single-file mode fires from the PostToolUse hook AFTER this machine wrote
    # the file -> local IS authoritative -> push (multi_machine=False so the
    # no-baseline conservative skip never suppresses a genuine local write).
    _sync_one(be, target, dry_run=dry_run, stats=stats, multi_machine=False)
    if stats["pushed"]:
        print(f"[sync] pushed {target}")
    elif stats["would_push"]:
        print(f"[sync] would push {target}")
    return 1 if stats["errors"] else 0


# --- continuity pull (machine-move resume) ---------------------------------
def _pull_one(be, full: Path, *, dry_run: bool, stats: dict, baseline_md5=None):
    """Inverse of _sync_one — HEAD-compare one continuity file and decide
    pull (S3 -> local) / skip. Returns the md5 to record as the new baseline (on
    pull or in-sync), or None when nothing was written.

    The pull is the read-side complement of the H4b sweep: at /start on a new
    machine, S3 holds the last machine's flushed continuity state and the local
    copy is stale/absent. But we must NEVER clobber a local file that carries
    UNPUSHED local writes (the same-machine crash-restart case), so the
    `baseline_md5` (content this machine last reconciled with S3, from the
    manifest) gates the overwrite — symmetric to _sync_one's stale/conflict
    guards:

      S3 absent                          -> nothing to pull           (skip)
      local absent                       -> S3 is the only copy       (pull)
      local == S3                        -> already current           (skip, baseline)
      local == baseline, S3 != baseline  -> peer/other machine wrote  (PULL)
      local != baseline (any S3)         -> local has unpushed writes -> DO NOT
                                            clobber (the sweep pushes it)  (skip)
      no baseline + local present+differs-> S3-authoritative at bind   (PULL, logged)
      S3 ETag multipart (uncomparable)   -> cannot classify -> defer   (skip)

    CRITICAL ordering: we read LOCAL bytes + be.stat() (an S3 HEAD) only — never
    be.read_bytes(force_fresh)/refresh during classification, because the
    own-cloud backend's _refresh DOWNLOADS S3 over the local file. refresh() is
    called ONLY once we've decided to pull (local is absent or an untouched
    cache), so it can never destroy unpushed local content.
    """
    try:
        st = be.stat(full)  # S3 HEAD; None if absent
    except Exception as e:  # noqa: BLE001 — network/credential issue -> count + go on
        print(f"[pull] WARN: stat failed for {full}: {e}", file=sys.stderr)
        stats["errors"] += 1
        return None
    if st is None:
        stats["s3_absent"] += 1
        return None  # nothing on S3 to resume from

    if full.exists():
        try:
            local_md5 = hashlib.md5(full.read_bytes()).hexdigest()
        except OSError as e:
            print(f"[pull] WARN: unreadable local {full}: {e}", file=sys.stderr)
            stats["errors"] += 1
            return None
        if _etag_matches(st.version, local_md5):
            stats["in_sync"] += 1
            return local_md5  # already current
        if _etag_is_multipart(st.version):
            # Uncomparable S3 ETag — cannot classify vs baseline. Defer; never
            # risk clobbering local on an ambiguous compare.
            stats["multipart_deferred"] = stats.get("multipart_deferred", 0) + 1
            print(f"[pull] skip (S3 ETag multipart — cannot classify; no clobber): "
                  f"{full}", file=sys.stderr)
            return None
        local_at_baseline = baseline_md5 is not None and local_md5 == baseline_md5
        if baseline_md5 is not None and not local_at_baseline:
            # Local diverged from the last reconciled content -> unpushed local
            # writes -> local is authoritative. Do NOT pull (the sweep pushes it).
            stats["local_ahead_skipped"] = stats.get("local_ahead_skipped", 0) + 1
            print(f"[pull] skip (local has unpushed writes vs baseline — not "
                  f"clobbering; sweep will push): {full}", file=sys.stderr)
            return None
        if baseline_md5 is None:
            # No baseline to prove local authority. At /start the agent is being
            # bound here FROM elsewhere, so S3 is authoritative — pull, but log it
            # (the Phase-5 drift detector reconciles genuine surprises).
            print(f"[pull] pulling no-baseline local (S3-authoritative at bind): "
                  f"{full}", file=sys.stderr)
        # else local == baseline, S3 differs -> peer wrote -> fall through to pull

    if dry_run:
        stats["would_pull"] += 1
        return None
    try:
        be.refresh(full)  # GET S3 -> local cache (force_fresh; materializes)
    except Exception as e:  # noqa: BLE001
        print(f"[pull] WARN: refresh failed for {full}: {e}", file=sys.stderr)
        stats["errors"] += 1
        return None
    stats["pulled"] += 1
    try:
        return hashlib.md5(full.read_bytes()).hexdigest()  # new baseline
    except OSError:
        return None


def pull_continuity(be, agent: str, *, dry_run: bool = False) -> dict:
    """Pull every continuity-tier session file for `agent` from S3 to local,
    freshness-aware (never clobbering unpushed local writes). Called by the
    /start IDLE branch (via owncloud-pull.sh -> POST /v1/admin/owncloud-pull)
    so a machine-move resumes from the last machine's flushed handoff /
    working-memory / execution-diary / ... The continuity set is the SSOT
    session-manifest.yaml (sync_tier == continuity); fail-closed to an empty
    set if the manifest is untrustworthy (pull nothing rather than guess)."""
    stats = {"agent": agent, "scanned": 0, "pulled": 0, "in_sync": 0,
             "would_pull": 0, "s3_absent": 0, "local_ahead_skipped": 0,
             "multipart_deferred": 0, "errors": 0, "pulled_files": []}
    tiers = _load_session_tiers()
    if tiers is None:
        stats["error"] = "session-manifest untrustworthy — pulled nothing (fail-closed)"
        return stats
    exact, _globs = tiers
    continuity_names = sorted(n for n, t in exact.items() if t == "continuity")

    agents_roots = _roots(be, "agents")
    if not agents_roots:
        stats["error"] = "no agents root on backend"
        return stats
    agents_root = agents_roots[0][0]
    session_dir = agents_root / agent / "session"

    manifest = _load_manifest()
    new_manifest = dict(manifest)
    for name in continuity_names:
        full = session_dir / name
        stats["scanned"] += 1
        rel_key = f"agents/{agent}/session/{name}"
        _base_mtime, base_md5 = _manifest_entry(manifest.get(rel_key))
        before = stats["pulled"]
        new_md5 = _pull_one(be, full, dry_run=dry_run, stats=stats,
                            baseline_md5=base_md5)
        if stats["pulled"] > before:
            stats["pulled_files"].append(name)
        if not dry_run and new_md5 is not None:
            try:
                mtime_ns = full.stat().st_mtime_ns
            except OSError:
                mtime_ns = None
            if mtime_ns is not None:
                new_manifest[rel_key] = {"mtime": mtime_ns, "md5": new_md5}
    # Also resume the temp/ working-doc store. temp/ filenames are dynamic
    # timestamps (not manifest-enumerable like the session continuity set), so
    # pull_temp lists S3 by prefix and _pull_one()'s each, reusing the same
    # no-clobber baseline gate. Share this manifest load + the single
    # _save_manifest below: pull_temp updates new_manifest in place, so one save
    # persists BOTH session and temp baselines (an independent save inside
    # pull_temp would clobber the session set written here).
    temp_stats = pull_temp(be, agent, dry_run=dry_run,
                           _manifest=manifest, _new_manifest=new_manifest)
    stats["temp"] = temp_stats
    for _k in ("scanned", "pulled", "in_sync", "would_pull", "s3_absent",
               "local_ahead_skipped", "multipart_deferred", "errors"):
        stats[_k] += temp_stats.get(_k, 0)
    stats["pulled_files"].extend(temp_stats.get("pulled_files", []))
    if not dry_run:
        _save_manifest(new_manifest)
    return stats


def pull_temp(be, agent: str, *, dry_run: bool = False,
              _manifest=None, _new_manifest=None) -> dict:
    """Pull agents/<agent>/temp/ working docs (+ the drained/ subdir) from S3,
    freshness-aware, never clobbering unpushed local writes.

    Sibling to pull_continuity for the temp working-doc SSOT. Session continuity
    files are enumerated by manifest name; temp/ filenames are dynamic timestamps
    (temp/<type>-<ISO>.md, see core/config/conventions/temp-store.md), so we list
    S3 by prefix (be.list_dir) and _pull_one() each, reusing the identical
    no-clobber baseline gate. The temp/ layout is flat with one sanctioned subdir
    (drained/), so a single level of recursion covers it.

    Manifest handling: when called from pull_continuity the caller threads its
    _manifest/_new_manifest through so a SINGLE _save_manifest persists both the
    session and temp baselines. Called standalone (_manifest is None) it loads and
    saves the manifest itself. A temp-pull failure is non-fatal — session resume
    is the critical path; the error surfaces under stats and the rolled-up
    counters rather than failing the whole pull."""
    stats = {"agent": agent, "scanned": 0, "pulled": 0, "in_sync": 0,
             "would_pull": 0, "s3_absent": 0, "local_ahead_skipped": 0,
             "multipart_deferred": 0, "errors": 0, "pulled_files": []}
    agents_roots = _roots(be, "agents")
    if not agents_roots:
        stats["error"] = "no agents root on backend"
        return stats
    agents_root = agents_roots[0][0]
    temp_dir = agents_root / agent / "temp"

    standalone = _manifest is None
    manifest = _manifest if _manifest is not None else _load_manifest()
    new_manifest = _new_manifest if _new_manifest is not None else dict(manifest)

    # Enumerate S3 children of temp/ (flat working docs + the drained/ subdir).
    # A missing prefix lists empty (no error); a network/credential failure raises
    # -> count + surface, never crash the surrounding /start pull.
    try:
        children = be.list_dir(temp_dir)
    except Exception as e:  # noqa: BLE001
        stats["errors"] += 1
        stats["error"] = f"temp list_dir failed: {e}"
        return stats
    targets = []
    for name in children:
        # Route by extension: temp/ working docs are *.md / *.json (the file-naming
        # convention); anything else is a subdir (drained/, or a stray one the
        # convention discourages). Recurse ONE level into a subdir so its contents
        # still resume cross-machine — temp/ is flat-plus-one-subdir by convention,
        # so a single level suffices. Routing by extension (vs hardcoding "drained")
        # avoids mis-counting a subdir as a file (a spurious s3_absent) and future-
        # proofs against any subdir name.
        if name.endswith(".md") or name.endswith(".json"):
            targets.append(temp_dir / name)
        else:
            try:
                for dname in be.list_dir(temp_dir / name):
                    if dname.endswith(".md") or dname.endswith(".json"):
                        targets.append(temp_dir / name / dname)
            except Exception:  # noqa: BLE001 — best-effort on a subdir
                pass

    for full in targets:
        stats["scanned"] += 1
        rel_key = "agents/" + full.relative_to(agents_root).as_posix()
        _base_mtime, base_md5 = _manifest_entry(manifest.get(rel_key))
        before = stats["pulled"]
        new_md5 = _pull_one(be, full, dry_run=dry_run, stats=stats,
                            baseline_md5=base_md5)
        if stats["pulled"] > before:
            # report relative to the agent dir, e.g. "temp/design-x.md"
            stats["pulled_files"].append(
                full.relative_to(agents_root / agent).as_posix())
        if not dry_run and new_md5 is not None:
            try:
                mtime_ns = full.stat().st_mtime_ns
            except OSError:
                mtime_ns = None
            if mtime_ns is not None:
                new_manifest[rel_key] = {"mtime": mtime_ns, "md5": new_md5}

    if standalone and not dry_run:
        _save_manifest(new_manifest)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true",
                   help="sweep all governed roots (world/meta/agents)")
    g.add_argument("--file", metavar="PATH",
                   help="mirror a single governed file (PostToolUse hook mode)")
    g.add_argument("--agent", metavar="NAME",
                   help="flush a single OWNED agent dir (agents/<NAME>/) — the "
                        "per-agent /stop flush scope (design §6); forces the "
                        "agents root, prunes everything else")
    ap.add_argument("--root", choices=("world", "meta", "agents"),
                    help="limit --all to one root")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what WOULD push; no S3 writes")
    ap.add_argument("--full", action="store_true",
                    help="ignore the mtime manifest; HEAD every file")
    ap.add_argument("--no-manifest", action="store_true",
                    help="do not read or write the mtime manifest")
    args = ap.parse_args()

    be = _require_owncloud_backend()
    if be is None:
        return 0  # local backend — no-op

    if args.file:
        return sync_file(be, Path(args.file), dry_run=args.dry_run)

    if args.no_manifest and _multi_machine():
        print("[sync] WARNING: --no-manifest disables the content baseline; on a "
              "multi-machine setup this can clobber a peer's newer S3 bytes. "
              "Prefer a manifest-backed sweep (drop --no-manifest).",
              file=sys.stderr)

    # --agent <NAME> forces the agents root, scoped to that one dir (§6).
    if args.agent:
        sweep_root, sweep_agent = "agents", args.agent
    else:
        sweep_root, sweep_agent = args.root, None

    t0 = time.time()
    stats = sweep(be, only_root=sweep_root, dry_run=args.dry_run,
                  use_manifest=not args.no_manifest, full=args.full,
                  only_agent=sweep_agent)
    dt = time.time() - t0
    mode = "DRY-RUN" if args.dry_run else "APPLIED"
    print(f"[sync] {mode} in {dt:.1f}s — scanned {stats['scanned']}, "
          f"in-sync {stats['in_sync']}, "
          f"{'would-push' if args.dry_run else 'pushed'} "
          f"{stats['would_push'] if args.dry_run else stats['pushed']}, "
          f"unchanged-skip {stats.get('skipped_unchanged', 0)}, "
          f"stale-skip {stats.get('stale_skipped', 0)}, "
          f"conflict-skip {stats.get('diverged_skipped', 0)}, "
          f"nobaseline-skip {stats.get('nobaseline_skipped', 0)}, "
          f"multipart-skip {stats.get('multipart_deferred', 0)}, "
          f"pruned-agents {stats.get('pruned_agents', 0)}, "
          f"conflicts {stats['conflicts']}, errors {stats['errors']}")
    if args.dry_run and stats["push_paths"]:
        for p in stats["push_paths"][:40]:
            print(f"           would push: {p}")
        if len(stats["push_paths"]) > 40:
            print(f"           ... and {len(stats['push_paths']) - 40} more")
    return 1 if stats["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
