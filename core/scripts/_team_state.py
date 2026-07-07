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
