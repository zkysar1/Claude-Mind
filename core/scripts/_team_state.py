"""Sharded team-state helpers — single source of truth for row routing + composition.

g-328-27 (2026-07-07): world/team-state.yaml carried every agent's
agent_status row, making it the hottest cross-writer file in the fleet —
every agent bumps its own last_active/in_flight every iteration, so N
agents contended on one lock (local) / one CAS-fenced S3 object
(own-cloud). Sharding: each agent's row lives in its OWN file

    world/team-state/agents/<name>.yaml

written ONLY by that agent's sessions, so no two writers ever touch the
same object — contention disappears by construction, and on the own-cloud
backend the per-object IfMatch fences never cross agents (rb-2639: the
CAS deadlock class is per-object). Shared, rarely-written fields
(strategic_focus, critical_blockers, recent_completions, shared_cadences,
inbox_alert_backlog, ...) stay in world/team-state.yaml (the "core" file).

Both core/scripts/team-state.py (CLI) and mind_api/src/world/team_state*.py
(daemon) import THIS module for routing + composition, satisfying the
guard-742 dual-write parity rule by construction — there is no second
implementation to drift from.

Compose semantics mirror coordination_merge._merge_agent_status: per-agent
WHOLE-ROW newest-wins (never field-stitch — a partial merge could pair an
in_flight from one snapshot with a current_focus from another). The row
file wins ties: post-shard it is the source of truth; a core-file entry is
either a pre-migration residual or a mixed-version-fleet write, and the
newest-wins comparison keeps both roll-forward AND roll-back windows
correct (whichever side an agent's code version writes, readers follow the
freshest snapshot).

Migration is lazy + optional: the first row write for an agent self-seeds
from the core residual (``core_residual`` passed as ``initial`` to
locked_modify_yaml), so no coordinated fleet migration is required. The
one-shot ``team-state.py migrate-shard`` merely cleans residuals out of the
core file.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

# Path segments of the rows directory under WORLD_DIR. Inlined copies exist
# in import-cycle-proof modules (core/scripts/_agents.py) — keep in sync.
ROWS_SUBDIR = ("team-state", "agents")

# Cold archive for retired agent rows (retire_agent). Deliberately NOT under
# ROWS_SUBDIR ("team-state/agents") so load_rows()'s shard glob never sees it,
# and NOT the core file — the live team-state code never reads it. It IS a
# subpath of the existing "team-state" dir, so the L1 new-top-level-under-world
# cruft gate allows it (archive-before-delete.md step 3: a location the live
# system does not read + no retention clock touches).
GRAVEYARD_SUBDIR = ("team-state", ".graveyard")

# Row-write metadata stamps (row-scoped analog of the core file's
# last_updated/last_updated_by). Also feed the newest-wins comparison so a
# row write that does not bump last_active still beats a stale residual.
ROW_UPDATED_KEY = "row_updated"
ROW_UPDATED_BY_KEY = "row_updated_by"


def rows_dir(world_dir) -> Path:
    d = Path(world_dir)
    for seg in ROWS_SUBDIR:
        d = d / seg
    return d


def graveyard_dir(world_dir) -> Path:
    d = Path(world_dir)
    for seg in GRAVEYARD_SUBDIR:
        d = d / seg
    return d


def row_path(world_dir, agent: str) -> Path:
    """Path of one agent's row file. Validates the agent name at the file
    boundary — an empty/dotted/separator-bearing name would escape the rows
    dir or collide with directory entries."""
    name = (agent or "").strip()
    if not name or name in (".", "..") or any(c in name for c in "/\\"):
        raise ValueError(f"invalid agent name for team-state row: {agent!r}")
    return rows_dir(world_dir) / f"{name}.yaml"


def load_rows(world_dir) -> dict:
    """All row files as {agent_name: row_dict}. Unreadable/non-dict rows are
    skipped loudly on stderr (a corrupt row must not take down every
    composed read — the owning agent's next stamp rewrites it)."""
    out: dict = {}
    d = rows_dir(world_dir)
    try:
        entries = sorted(d.iterdir())
    except OSError:
        return out
    for p in entries:
        if p.suffix != ".yaml" or not p.is_file():
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                doc = yaml.safe_load(f)
        except Exception as e:  # noqa: BLE001 — one bad row must not break compose
            print(f"[_team_state] WARN: unreadable row {p.name}: {e}", file=sys.stderr)
            continue
        if isinstance(doc, dict) and doc:
            out[p.stem] = doc
    return out


def row_agent_names(world_dir) -> tuple:
    """Row-file stems only (no YAML parse) — cheap roster source."""
    d = rows_dir(world_dir)
    try:
        return tuple(sorted(p.stem for p in d.iterdir()
                            if p.suffix == ".yaml" and p.is_file()))
    except OSError:
        return ()


def _entry_ts(entry) -> str:
    """Newest-wins timestamp of one agent-status snapshot. ISO-8601 local
    strings compare correctly as strings. Missing timestamps sort oldest."""
    if not isinstance(entry, dict):
        return ""
    vals = [str(entry.get(k)) for k in ("last_active", ROW_UPDATED_KEY)
            if entry.get(k)]
    return max(vals) if vals else ""


def compose_agent_status(core_status: dict, rows: dict) -> dict:
    """Merge core-file residual rows with row-file rows: per-agent WHOLE-ROW
    newest-wins, row file winning ties (mirrors
    coordination_merge._merge_agent_status side-pick semantics). Keys sorted
    for deterministic output."""
    core_status = core_status if isinstance(core_status, dict) else {}
    out: dict = {}
    for name in sorted(set(core_status) | set(rows)):
        core_entry = core_status.get(name)
        row_entry = rows.get(name)
        if row_entry is None:
            out[name] = core_entry
        elif core_entry is None:
            out[name] = row_entry
        else:
            out[name] = core_entry if _entry_ts(core_entry) > _entry_ts(row_entry) else row_entry
    return out


def compose_state(state: dict, world_dir) -> dict:
    """Overlay row files onto a loaded core team-state document (in place).
    Also lifts last_updated/last_updated_by to the newest stamp across core
    + rows, so liveness dashboards keep working without core-file churn."""
    rows = load_rows(world_dir)
    # Only materialize the agent_status key when the core doc carries it or
    # rows exist — callers rely on `{} stays {}` for their fail-open
    # truthiness contracts (goal-selector rb-2429, status.py dict contract).
    if rows or "agent_status" in state:
        state["agent_status"] = compose_agent_status(state.get("agent_status") or {}, rows)
    best_ts = str(state.get("last_updated") or "")
    best_by = state.get("last_updated_by")
    for name, row in rows.items():
        ts = _entry_ts(row)
        if ts and ts > best_ts:
            best_ts = ts
            best_by = row.get(ROW_UPDATED_BY_KEY) or name
    if best_ts:
        state["last_updated"] = best_ts
        state["last_updated_by"] = best_by
    return state


def route_field(field: str):
    """Classify a dot-path write target.

    Returns ("row", <agent>, <subpath>) for agent_status.<name>[.<rest>]
    (subpath "" means whole-row), else ("core", None, field). The bare
    "agent_status" map has no live writers (read-only call sites) — it
    routes core so a hypothetical caller degrades to the legacy behavior
    instead of silently fanning out."""
    parts = (field or "").split(".")
    if len(parts) >= 2 and parts[0] == "agent_status" and parts[1]:
        return "row", parts[1], ".".join(parts[2:])
    return "core", None, field


def stamp_row_metadata(row: dict, author: str, now: str) -> dict:
    row[ROW_UPDATED_KEY] = now
    row[ROW_UPDATED_BY_KEY] = author
    return row


def core_residual(core_path, agent: str) -> dict:
    """One agent's entry from the CORE file's agent_status — the lazy-
    migration seed (passed as ``initial`` so the first row write starts from
    the pre-shard snapshot instead of {}). Read outside the row lock:
    seed-once semantics make a marginally stale seed harmless (the write
    that triggered seeding immediately overwrites the fields it targets)."""
    try:
        with open(core_path, "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
        entry = (doc.get("agent_status") or {}).get(agent)
        return dict(entry) if isinstance(entry, dict) else {}
    except Exception:  # noqa: BLE001 — missing/corrupt core just means empty seed
        return {}


def read_agent_row(world_dir, agent: str, core_path=None) -> dict:
    """One agent's current status: row file first, core residual fallback,
    newest-wins when both exist (same comparison as compose_agent_status).
    For hot single-agent lookups (in_flight inference) — avoids loading
    every row."""
    row_entry: dict = {}
    try:
        p = row_path(world_dir, agent)
    except ValueError:
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f)
        if isinstance(doc, dict):
            row_entry = doc
    except Exception:  # noqa: BLE001 — absent/corrupt row falls back to core
        row_entry = {}
    core_entry = core_residual(core_path, agent) if core_path is not None else {}
    if not core_entry:
        return row_entry
    if not row_entry:
        return core_entry
    return core_entry if _entry_ts(core_entry) > _entry_ts(row_entry) else row_entry


# ---------------------------------------------------------------------------
# Agent-row retirement (5) — the sanctioned REMOVE path.
#
# Signal gap 9 root-caused: team-state had NO way to remove an
# agent_status ROW. route_field sends agent_status.<name> to a per-agent
# SHARD (targeting a non-existent shard for un-sharded legacy agents);
# the generic --operation remove / _remove_nested only drops items from a
# LIST (agent_status is a DICT -> no-op); the daemon whole-row path refuses
# remove. So a user-retired agent's core-residual row could only be removed
# by forbidden raw-YAML surgery. retire_agent is that missing op — one place,
# imported by BOTH the CLI (team-state.py) and the daemon (team_state_write.py),
# so guard-742 parity holds by construction (no second implementation).
# ---------------------------------------------------------------------------

def enumerate_retire(world_dir, core_path, agent: str) -> dict:
    """Pure read: what retire_agent WOULD remove for <agent> — the core-file
    agent_status.<agent> residual and the per-agent shard (path + content).
    `present` is False when neither exists (retire is then an idempotent
    no-op). No mutation — the enumeration is also the integrity baseline the
    archive is verified against (archive-before-delete.md step 1)."""
    name = (agent or "").strip()
    core = core_residual(core_path, name) if core_path is not None else {}
    rp = None
    row_content = None
    try:
        rp = row_path(world_dir, name)
    except ValueError:
        rp = None
    row_exists = rp is not None and rp.exists()
    if row_exists:
        try:
            with open(rp, "r", encoding="utf-8") as f:
                doc = yaml.safe_load(f)
            if isinstance(doc, dict):
                row_content = doc
        except Exception:  # noqa: BLE001 — corrupt row still archived as None
            row_content = None
    return {
        "agent": name,
        "core_residual": core or None,
        "row_path": str(rp) if rp is not None else None,
        "row_content": row_content,
        "present": bool(core) or row_exists,
    }


def enumerate_belief_sweep(world_dir, core_path, retiree: str) -> dict:
    """Pure read: every live agent (core-resident agent_status row OR per-agent
    shard) holding a Theory-of-Mind belief whose `about` == retiree. Returns
    {"core": {holder: [belief, ...]}, "shards": {holder: [belief, ...]}}.

    Beliefs about a retiree are DEAD: the subject produces no further
    observations, so the holder's own supersede_beliefs hygiene (which fires
    only on a FRESH observation of the subject) never replaces them.
    Retirement must sweep them or they linger forever — the echo/zeta
    about:delta residue that survived the g-115-1965 row removal (g-115-2043,
    rb-3104 lineage). Enumeration is also the archive baseline
    (archive-before-delete.md step 1). No mutation."""
    name = (retiree or "").strip()
    out = {"core": {}, "shards": {}}
    if not name:
        return out

    def _hits(beliefs):
        return [b for b in (beliefs or [])
                if isinstance(b, dict) and str(b.get("about", "")).strip() == name]

    # core-resident beliefs (agent_status.<holder>.beliefs — legacy/un-sharded)
    if core_path is not None:
        try:
            with open(core_path, "r", encoding="utf-8") as f:
                doc = yaml.safe_load(f)
            ast = (doc or {}).get("agent_status") or {}
            for holder, row in ast.items():
                if holder == name or not isinstance(row, dict):
                    continue
                h = _hits(row.get("beliefs"))
                if h:
                    out["core"][holder] = h
        except Exception:  # noqa: BLE001 — a corrupt core file yields no sweep
            pass

    # shard-resident beliefs (per-agent shard, top-level `beliefs`)
    rdir = rows_dir(world_dir)
    if rdir.is_dir():
        for shard in sorted(rdir.glob("*.yaml")):
            holder = shard.stem
            if holder == name:
                continue
            try:
                with open(shard, "r", encoding="utf-8") as f:
                    doc = yaml.safe_load(f)
            except Exception:  # noqa: BLE001 — corrupt shard skipped
                continue
            if not isinstance(doc, dict):
                continue
            h = _hits(doc.get("beliefs"))
            if h:
                out["shards"][holder] = h
    return out


def retire_agent(world_dir, core_path, agent, author, now, *,
                 source=None, dry_run=False) -> dict:
    """Sanctioned removal of an agent's team-state presence: the core-file
    agent_status.<agent> residual AND the per-agent shard file, gated by
    archive-before-delete (.claude/rules/archive-before-delete.md).

    Protocol: ENUMERATE -> ARCHIVE (to world/team-state/.graveyard/, a path
    the live system never reads) -> VERIFY the archive against the
    enumeration -> DELETE (pop the core key under lock + unlink the shard)
    -> RECEIPT (the archive file IS the receipt). NEVER deletes on an
    unverified archive — raises RuntimeError instead.

    Idempotent: a re-run when the agent is already absent returns
    removed=False, reason="not_present" without error. dry_run reports the
    enumeration without writing or deleting. Returns a JSON-serializable
    result dict."""
    name = (agent or "").strip()
    if not name or name in (".", "..") or any(c in name for c in "/\\"):
        raise ValueError(f"invalid agent name for retire: {agent!r}")

    plan = enumerate_retire(world_dir, core_path, name)
    # 3: beliefs ABOUT the retiree are held by OTHER agents and persist
    # independently of the retiree's own row — enumerate them so retirement (or a
    # re-run after the row is already gone) sweeps the dead beliefs too.
    sweep = enumerate_belief_sweep(world_dir, core_path, name)
    has_beliefs = bool(sweep["core"] or sweep["shards"])
    if not plan["present"] and not has_beliefs:
        return {"ok": True, "agent": name, "removed": False,
                "reason": "not_present",
                "detail": f"{name} has no core residual, no shard, and no "
                          f"partner-beliefs — nothing to retire"}

    if dry_run:
        return {"ok": True, "agent": name, "removed": False, "dry_run": True,
                "would_remove": {
                    "core_residual": plan["core_residual"] is not None,
                    "shard": plan["row_content"] is not None,
                    "beliefs": {
                        "core": {h: len(bs) for h, bs in sweep["core"].items()},
                        "shards": {h: len(bs) for h, bs in sweep["shards"].items()}}},
                "plan": plan}

    # --- ARCHIVE (before any delete) ---------------------------------------
    payload = {
        "schema": "team-state-retire-archive/v1",
        "agent": name,
        "retired_at": now,
        "retired_by": author,
        "source": source,
        "core_residual": plan["core_residual"],
        "shard_content": plan["row_content"],
        "shard_path": plan["row_path"],
        "swept_beliefs": sweep,
        "restore": (
            f"Re-add the core residual via `team-state-update.sh --field "
            f"agent_status.{name} --value '<core_residual json>' --operation "
            f"set`; re-create the shard by writing shard_content to "
            f"{plan['row_path']}. Do NOT restore into a live session's row "
            f"(archive-before-delete.md — resurrection risk)."),
    }
    gyard = graveyard_dir(world_dir)
    gyard.mkdir(parents=True, exist_ok=True)
    safe_ts = now.replace(":", "").replace("-", "").replace("T", "-")
    archive_path = gyard / f"{safe_ts}-{name}.yaml"
    with open(archive_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(payload, f, sort_keys=False, allow_unicode=True)

    # --- VERIFY the archive (re-read, field-compare) -----------------------
    with open(archive_path, "r", encoding="utf-8") as f:
        verify = yaml.safe_load(f)
    if (not isinstance(verify, dict)
            or verify.get("agent") != name
            or verify.get("core_residual") != plan["core_residual"]
            or verify.get("shard_content") != plan["row_content"]
            or verify.get("swept_beliefs") != sweep):
        raise RuntimeError(
            f"retire {name}: archive verification FAILED at {archive_path} "
            f"— refusing to delete (archive-before-delete.md)")

    # --- DELETE (only now that the archive is verified) --------------------
    removed_core = False
    removed_shard = False
    beliefs_swept = {"core": {}, "shards": {}}
    from _fileops import locked_modify_yaml  # lazy: cycle-proof

    # Core file: pop the retiree's own row AND sweep every OTHER core-resident
    # row's beliefs about the retiree — one locked read-modify-write so the sweep
    # is atomic with the row removal. The single-writer invariant on
    # agent_status.<self>.beliefs is a CONCURRENT-write guard; a retiree-belief is
    # dead (never re-written by its holder, whose supersede fires only on a fresh
    # observation of a now-gone subject), so this administrative sweep under the
    # shared core lock is the sanctioned exception — same class as popping the row.
    if plan["core_residual"] is not None or sweep["core"]:
        def _modify_core(state):
            ast = state.get("agent_status")
            if isinstance(ast, dict):
                if name in ast:
                    ast.pop(name)
                for holder, row in ast.items():
                    if not isinstance(row, dict):
                        continue
                    bl = row.get("beliefs")
                    if not isinstance(bl, list):
                        continue
                    kept = [b for b in bl
                            if not (isinstance(b, dict)
                                    and str(b.get("about", "")).strip() == name)]
                    if len(kept) != len(bl):
                        row["beliefs"] = kept
                        beliefs_swept["core"][holder] = len(bl) - len(kept)
            state["last_updated"] = now
            state["last_updated_by"] = author
            return state

        locked_modify_yaml(Path(core_path), _modify_core,
                           initial={"agent_status": {}})
        removed_core = plan["core_residual"] is not None

    # Shard files: sweep each holder's top-level beliefs about the retiree under
    # that shard's own lock (serialized with the holder's live writes).
    for holder in sweep["shards"]:
        sp = row_path(world_dir, holder)

        def _modify_shard(state, _name=name, _holder=holder):
            bl = state.get("beliefs")
            if isinstance(bl, list):
                kept = [b for b in bl
                        if not (isinstance(b, dict)
                                and str(b.get("about", "")).strip() == _name)]
                if len(kept) != len(bl):
                    state["beliefs"] = kept
                    beliefs_swept["shards"][_holder] = len(bl) - len(kept)
            return state

        locked_modify_yaml(sp, _modify_shard, initial={})

    # Retiree's own shard unlink (unchanged path).
    if plan["row_content"] is not None and plan["row_path"]:
        try:
            Path(plan["row_path"]).unlink()
            removed_shard = True
        except FileNotFoundError:
            removed_shard = False

    any_removed = (removed_core or removed_shard
                   or bool(beliefs_swept["core"] or beliefs_swept["shards"]))
    return {"ok": True, "agent": name, "removed": any_removed,
            "removed_core_residual": removed_core,
            "removed_shard": removed_shard,
            "beliefs_swept": beliefs_swept,
            "archive": str(archive_path), "source": source}
