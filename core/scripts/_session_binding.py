"""Session binding resolver — Phase 2.6 first-class session dirs.

Replaces the root-cruft `.active-agent-<SID>` file with a structured
`agents/<name>/sessions/<SID>/binding.yaml` document that carries full
session intel: agent name, mode, started_at, started_by.

Two resolution layers, in order:

1. **Phase 2.6 (new)**: glob `agents/*/sessions/<SID>/binding.yaml`. The
   agent dir IS the owner — no separate pointer file needed. Cost is
   O(agents) per resolve, which is fine for local-only agent lists
   (user-authorized cost).

2. **Legacy fallback**: `.active-agent-<SID>` at PROJECT_ROOT. Kept for
   the migration window so existing live sessions don't break when this
   ships. Will be retired in a follow-up once all live agents have been
   restarted on the new layout.

SAFETY: All resolution returns None / empty on any error. Caller decides
how to handle. No exception escapes a public function. Mirrors the
fail-open contract of `_resolve_agent_from_sid.py`.

Schema for `binding.yaml`:

```yaml
session_id: <sid>          # MUST match dir name
agent: <name>              # MUST match parent agent dir name
mode: reader|assistant|autonomous
started_at: <iso-timestamp>
started_by: claude-code|user|<other>
```

Additional keys are ignored (forward-compatible). `mode` may be absent or
empty (resolves to None) — callers MUST tolerate.
"""
from __future__ import annotations

from pathlib import Path
from typing import NamedTuple, Optional

# Phase 2.5.C sync invariant — see CLAUDE.md "Agent-dir Resolution".
_AGENTS_PARENT_DIR = "agents"

# Phase 2.6 sync invariant.
_SESSIONS_DIRNAME = "sessions"
_BINDING_FILENAME = "binding.yaml"

# Reserved agent names — same set used by _resolve_agent_from_sid.py.
_RESERVED_AGENT_NAMES = {"world", "meta", "core", "node_modules", "scripts"}


class SessionBinding(NamedTuple):
    """Resolved session binding intel.

    `mode` may be None when the binding is the legacy `.active-agent-<SID>`
    file (which only carried the agent name).
    """
    session_id: str
    agent: str
    mode: Optional[str]
    started_at: Optional[str]
    started_by: Optional[str]
    source: str  # "binding.yaml" | "legacy-active-agent"


def _valid_sid_shape(sid: str) -> bool:
    if not sid:
        return False
    if any(c in sid for c in ("/", "\\", "\n", "\r", " ", "\t")):
        return False
    if ".." in sid:
        return False
    return True


def _valid_agent_name(name: str) -> bool:
    if not name:
        return False
    if any(c in name for c in ("/", "\\", " ", "\t", "\n", "\r", ".")):
        return False
    if name in _RESERVED_AGENT_NAMES:
        return False
    return True


def _agents_root(project_root: Path) -> Path:
    if _AGENTS_PARENT_DIR:
        return project_root / _AGENTS_PARENT_DIR
    return project_root


def _agent_dir(project_root: Path, name: str) -> Path:
    return _agents_root(project_root) / name


def _coerce_str_field(value) -> Optional[str]:
    """Coerce a YAML-parsed scalar to a stripped string, or None.

    PyYAML parses unquoted ISO-8601 values as datetime/date objects, not
    strings. Bindings written by /start may emit `started_at: 2026-05-19T12:00:00`
    unquoted. Accept both shapes; emit ISO format for datetime, fall back
    to str() for anything else; treat empty strings as None.
    """
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        return s or None
    # datetime/date have isoformat(); other types fall back to str().
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        try:
            s = str(iso()).strip()
            return s or None
        except Exception:
            pass
    try:
        s = str(value).strip()
        return s or None
    except Exception:
        return None


def _parse_yaml_min(text: str) -> dict:
    """Minimal yaml.safe_load wrapper — falls back to empty dict on error.

    Uses PyYAML if available (it always is in this repo). Returns {} on
    parse failure so callers can treat malformed bindings as missing.
    """
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        return {}
    try:
        data = yaml.safe_load(text)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _try_phase26_binding(sid: str, project_root: Path) -> Optional[SessionBinding]:
    """Look for agents/*/sessions/<SID>/binding.yaml. Returns None if absent.

    Thin wrapper around `_try_phase26_binding_with_reason` that drops the
    diagnostic reason — kept for backward-compat callers of the original
    signature. New callers wanting the failure mode should call
    `_try_phase26_binding_with_reason` directly.
    """
    binding, _reason = _try_phase26_binding_with_reason(sid, project_root)
    return binding


def _log_binding_shadow(
    sid: str, project_root: Path, chosen: str, all_agents: "list[str]"
) -> None:
    """Append a greppable record when 2+ agents carry a binding.yaml for one SID.

    Fully guarded — a logging failure MUST NEVER break identity resolution
    (preserves this module's "no exception escapes" contract). The shadow is
    the g-115-1814 "one binding.yaml per SID" invariant surviving /start's
    --retire-legacy retirement (e.g. a foreign binding.yaml synced in AFTER
    /start ran). resolve_binding now picks deterministically (freshest), so the
    misattribution is fixed; this record makes the creation-side gap visible.
    """
    try:
        import json
        from datetime import datetime
        log_dir = Path(project_root) / "core" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "event": "binding-shadow-resolved",
            "sid": sid,
            "chosen_agent": chosen,
            "shadow_agents": sorted(set(all_agents)),
        }
        with open(log_dir / "binding-shadow.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def _try_phase26_binding_with_reason(
    sid: str, project_root: Path
) -> tuple[Optional[SessionBinding], str]:
    """Phase 2.6 resolution that also returns a failure reason on miss.

    Returns (binding, "") on success, (None, reason) on failure. Reason is
    the MOST SPECIFIC failure encountered across the agent-dir scan (later
    matches override earlier ones, so a file-with-mismatched-agent-name
    reports `agent-name-mismatch` not `binding-yaml-missing`).

    Reasons:
      agents-root-missing        - agents/ dir absent
      binding-yaml-missing       - no binding.yaml for this SID under any agent
      binding-yaml-read-error    - file exists but read failed (OSError)
      binding-yaml-parse-failed  - file exists but YAML parse failed
      session-id-mismatch        - binding.yaml session_id != dir name
      agent-name-mismatch        - binding.yaml agent != parent dir name
      local-paths-conf-missing   - validated binding but agent dir has no local-paths.conf
    """
    ar = _agents_root(project_root)
    if not ar.is_dir():
        return None, "agents-root-missing"

    # Build candidate path directly per agent for efficiency.
    try:
        children = list(ar.iterdir())
    except OSError:
        return None, "agents-root-missing"

    reason = "binding-yaml-missing"
    # Collect ALL valid matches rather than returning the first (4).
    # Each element: (SessionBinding, candidate_path, started_at_for_sort).
    matches: "list[tuple[SessionBinding, Path, str]]" = []
    for child in children:
        if not child.is_dir():
            continue
        agent_name = child.name
        if not _valid_agent_name(agent_name):
            continue
        candidate = child / _SESSIONS_DIRNAME / sid / _BINDING_FILENAME
        if not candidate.is_file():
            continue

        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            reason = "binding-yaml-read-error"
            continue
        data = _parse_yaml_min(text)
        if not data:
            reason = "binding-yaml-parse-failed"
            continue

        # Cross-check: file values must agree with on-disk layout.
        if str(data.get("session_id", "")).strip() != sid:
            reason = "session-id-mismatch"
            continue
        if str(data.get("agent", "")).strip() != agent_name:
            reason = "agent-name-mismatch"
            continue
        if not (child / "local-paths.conf").is_file():
            reason = "local-paths-conf-missing"
            continue

        mode = _coerce_str_field(data.get("mode"))
        started_at = _coerce_str_field(data.get("started_at"))
        started_by = _coerce_str_field(data.get("started_by"))

        matches.append((
            SessionBinding(
                session_id=sid,
                agent=agent_name,
                mode=mode,
                started_at=started_at,
                started_by=started_by,
                source="binding.yaml",
            ),
            candidate,
            started_at or "",
        ))

    if not matches:
        return None, reason
    if len(matches) == 1:
        # Happy path — exactly one binding.yaml for this SID (the 4
        # invariant holds). Byte-identical to the pre-4 first-match
        # return.
        return matches[0][0], ""

    # SHADOW (4): 2+ agents carry a VALID binding.yaml for the SAME
    # SID — the 4 "one binding.yaml per SID" invariant is violated (a
    # stale cross-agent binding slipped past /start's --retire-legacy, e.g.
    # arrived via own-cloud sync AFTER /start ran). The pre-fix code returned
    # the FIRST match in ar.iterdir() order, which is filesystem-nondeterministic
    # — so identity resolution silently misattributed (observed 2026-07-19 02:30:
    # an alpha session resolved to echo → completed_by=echo). Resolve
    # DETERMINISTICALLY to the freshest session: the newest started_at is the
    # most recent /start, which --retire-legacy semantics make the current
    # owner. Tie-break by binding.yaml mtime then agent name so the choice NEVER
    # depends on iterdir order. Log the shadow so the creation-side gap stays
    # greppable.
    def _shadow_sort_key(m: "tuple[SessionBinding, Path, str]"):
        binding, path, started = m
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        return (started, mtime, binding.agent)

    matches.sort(key=_shadow_sort_key, reverse=True)
    winner = matches[0][0]
    _log_binding_shadow(
        sid, project_root, winner.agent, [m[0].agent for m in matches]
    )
    return winner, ""


def _try_legacy_active_agent(sid: str, project_root: Path) -> Optional[SessionBinding]:
    """Look for the pre-Phase 2.6 .active-agent-<SID> file at PROJECT_ROOT.

    Kept for the migration window. Mirrors `_resolve_agent_from_sid.resolve`
    validation. mode/started_at/started_by are None — that file only ever
    held the agent name.
    """
    binding = project_root / f".active-agent-{sid}"
    if not binding.is_file():
        return None
    try:
        agent = binding.read_text().replace("\r", "").replace("\n", "").strip()
    except OSError:
        return None
    if not _valid_agent_name(agent):
        return None
    adir = _agent_dir(project_root, agent)
    if not adir.is_dir():
        return None
    if not (adir / "local-paths.conf").is_file():
        return None
    return SessionBinding(
        session_id=sid,
        agent=agent,
        mode=None,
        started_at=None,
        started_by=None,
        source="legacy-active-agent",
    )


def resolve_binding(sid: str, project_root: Path) -> Optional[SessionBinding]:
    """Resolve a SID to a full SessionBinding, or None if unresolvable.

    Tries the Phase 2.6 layout first, falls back to the legacy
    `.active-agent-<SID>` file at PROJECT_ROOT. Returns None on any
    validation failure or missing-binding case.
    """
    binding, _reason = resolve_binding_with_diagnostics(sid, project_root)
    return binding


def resolve_binding_with_diagnostics(
    sid: str, project_root: Path
) -> tuple[Optional[SessionBinding], str]:
    """Resolve a SID and also return a failure reason on miss.

    Returns (binding, "") on success, (None, reason) on failure. The reason
    string is the most-specific failure mode encountered; callers consume it
    for diagnostic logging (e.g. bash-agent-inject's miss log). Used by the
    bash-agent-inject hook (g-115-1187) so silent mid-session binding drops
    become greppable by failure mode rather than collapsing into a single
    `no_active_agent_binding` line per SID.

    Reason value set: see `_try_phase26_binding_with_reason` docstring plus
    "invalid-sid" (SID failed shape check). Legacy-active-agent path is a
    fallback only — when Phase 2.6 produces a specific failure reason that
    reason is preserved (legacy is treated as an additional attempt, not a
    reason source).
    """
    if not _valid_sid_shape(sid):
        return None, "invalid-sid"
    project_root = Path(project_root)
    try:
        new, reason = _try_phase26_binding_with_reason(sid, project_root)
    except Exception:
        new, reason = None, "phase26-exception"
    if new is not None:
        return new, ""
    try:
        legacy = _try_legacy_active_agent(sid, project_root)
    except Exception:
        legacy = None
    if legacy is not None:
        return legacy, ""
    return None, reason


def resolve_agent_name(sid: str, project_root: Path) -> str:
    """Backward-compatible resolver returning just the agent name.

    Drop-in replacement for `_resolve_agent_from_sid.resolve`. Returns ""
    when no binding can be resolved.
    """
    b = resolve_binding(sid, project_root)
    return b.agent if b is not None else ""


def list_active_bindings(project_root: Path) -> list:
    """Enumerate all currently-active session bindings across all agents.

    Returns a list of SessionBinding instances for every
    `agents/*/sessions/*/binding.yaml` on disk. Used by housekeeping
    scripts (`cleanup-stale-bindings.sh`) and intel queries
    ("how many sessions has this agent had?").

    Includes legacy `.active-agent-<SID>` files at PROJECT_ROOT — they
    appear with `source="legacy-active-agent"` so callers can distinguish.
    """
    project_root = Path(project_root)
    out: list = []
    seen_sids = set()

    # Phase 2.6 bindings first.
    ar = _agents_root(project_root)
    if ar.is_dir():
        try:
            for child in ar.iterdir():
                if not child.is_dir():
                    continue
                if not _valid_agent_name(child.name):
                    continue
                sessions_dir = child / _SESSIONS_DIRNAME
                if not sessions_dir.is_dir():
                    continue
                try:
                    sids = list(sessions_dir.iterdir())
                except OSError:
                    continue
                for sd in sids:
                    if not sd.is_dir():
                        continue
                    sid = sd.name
                    if not _valid_sid_shape(sid):
                        continue
                    if sid in seen_sids:
                        continue
                    b = resolve_binding(sid, project_root)
                    if b is not None:
                        out.append(b)
                        seen_sids.add(sid)
        except OSError:
            pass

    # Legacy bindings — only emit if not already covered.
    try:
        for entry in project_root.iterdir():
            name = entry.name
            if not name.startswith(".active-agent-"):
                continue
            if not entry.is_file():
                continue
            sid = name[len(".active-agent-"):]
            if not _valid_sid_shape(sid):
                continue
            if sid in seen_sids:
                continue
            b = resolve_binding(sid, project_root)
            if b is not None:
                out.append(b)
                seen_sids.add(sid)
    except OSError:
        pass

    return out


def main():
    """CLI entry: `py -3 _session_binding.py <sid>` prints binding as JSON."""
    import json
    import sys
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: _session_binding.py <sid>"}))
        sys.exit(0)
    sid = sys.argv[1]
    project_root = Path(__file__).resolve().parent.parent.parent
    b = resolve_binding(sid, project_root)
    if b is None:
        print(json.dumps({"resolved": False}))
    else:
        print(json.dumps({
            "resolved": True,
            "session_id": b.session_id,
            "agent": b.agent,
            "mode": b.mode,
            "started_at": b.started_at,
            "started_by": b.started_by,
            "source": b.source,
        }))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
