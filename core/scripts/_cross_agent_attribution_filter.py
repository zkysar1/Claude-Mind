"""Cross-agent attribution filter for state-update scripts.

Drops paths attributable to non-self agents (partner WIP at neutral
paths) from a candidate file list.

Restored under g-115-741 after the original g-115-714 deliverables
disappeared before reaching git (deep-close 2026-05-14T02:16:13; .pyc
cache compiled 02:11 was the recovery source). Reconstructed verbatim
from the cpython-312 bytecode — same public API, constants, and 3-source
filter logic the post-state-update-gate.sh:122 call site expects.

Public API (stable; mirrored by iteration-commit.sh's filter stack —
g-248-87 + g-115-692 + g-115-697):
    filter_paths(paths, self_agent, project_root, world_dir) -> (kept, dropped)

Fail-open at every layer: any error retains the original list (biases
toward over-firing the gate, never silently dropping self-authored work).
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

#  / : force utf-8 on stdin/stdout/stderr (covers Windows
# cp1252 fallback when callers bypass the _platform.sh PYTHONIOENCODING=utf-8
# shim). Closes acceptance (4) of  — stdin-ingest sweep.
from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()

# Clock-skew tolerance (seconds) for mtime-vs-claim comparisons. Filesystem
# mtimes and ISO claim timestamps can disagree by a few seconds across
# processes/agents; absorb that rather than mis-attribute on the boundary.
CLOCK_SKEW_SEC = 5


def _iso_to_epoch(iso_str):
    if not iso_str or iso_str == "null":
        return 0
    try:
        return int(datetime.datetime.fromisoformat(iso_str).timestamp())
    except (ValueError, TypeError):
        return 0


def _max_age_sec():
    """Age cutoff (seconds) for partner uncommitted-edits entries. A claim
    older than this is STALE — not active between-claim work — and must NOT
    suppress the committer's own same-session edit to that path. Env-overridable
    via PARTNER_UNCOMMITTED_MAX_AGE_HOURS (default 48h). A value <= 0 disables
    the cutoff (every entry is considered fresh — the pre-g-115-752 behavior)."""
    try:
        hours = float(os.environ.get("PARTNER_UNCOMMITTED_MAX_AGE_HOURS", "48"))
    except (TypeError, ValueError):
        hours = 48.0
    return hours * 3600.0


def _entry_is_stale(rec, max_age_sec, now_epoch):
    """True ONLY when a parseable timestamp proves the uncommitted-edits entry
    is older than max_age_sec. An entry whose edit_ts/mtime is missing or
    unparseable is treated as NOT stale (returns False) so the pre-g-115-752
    filter behavior is preserved for malformed/legacy records.

    g-115-752 (from bravo insight msg-20260514-143816, hardened by alpha
    msg-1904): iteration-commit.sh and _read_uncommitted_log both built their
    partner-uncommitted path set from EVERY log entry with no age check, so a
    3-week-stale charlie .claude/settings.json entry (edit_ts 2026-05-20)
    dropped alpha's legitimate same-session edit (commit 33cad03d). The cutoff
    only ELIMINATES proven-stale false drops; it never widens a true-positive
    drop (uncertain entries keep the prior behavior). Direction aligns with this
    module's standing bias: never silently drop self-authored work."""
    if max_age_sec <= 0:
        return False
    ts = rec.get("edit_ts")
    if isinstance(ts, str) and ts and ts != "null":
        ep = _iso_to_epoch(ts)
        if ep > 0:
            return (now_epoch - ep) > max_age_sec
    mt = rec.get("mtime")
    if isinstance(mt, (int, float)) and not isinstance(mt, bool):
        try:
            return (now_epoch - float(mt)) > max_age_sec
        except (OverflowError, ValueError):
            return False
    return False


def _file_mtime(repo_root, path):
    try:
        full = Path(repo_root) / path
        if full.exists():
            return int(full.stat().st_mtime)
        return 0
    except (OSError, ValueError):
        return 0


def _normalize_rel_path(p, project_root):
    """Canonicalize a recorded/candidate path to PROJECT_ROOT-relative POSIX
    form so Source 2 set-membership compares equal regardless of whether the
    path was stored absolute or relative.

    g-115-1180: uncommitted-edits.jsonl can hold BOTH relative paths (current
    uncommitted-edits-record.sh output, after its drive-letter normalization
    landed) AND absolute Windows paths (legacy entries written before that
    writer fix — observed C:/<WORKSPACE>/.../core/foo.py). The Source 2 lookup
    `path in partner_uncommitted` is an exact string match; a relative
    candidate (git diff output) silently misses an absolute log key for the
    same file, so partner WIP is not dropped and gets mis-attributed.

    SINGLE SOURCE OF TRUTH (rb-1405): iteration-commit.sh's partner-uncommitted
    extraction imports THIS function, so both consumers of uncommitted-edits.jsonl
    normalize identically and cannot drift.

    Forward-slashes all separators, then strips a leading PROJECT_ROOT prefix.
    Handles the Windows drive form (C:/root/...) and the MSYS/Git-Bash form
    (/c/root/...) since either can appear depending on the writing shell.
    Fail-open: returns the forward-slashed input unchanged on any mismatch (an
    unrecognized absolute path simply won't match relative candidates — same
    behavior as before, never raises into the filter)."""
    if not isinstance(p, str) or not p:
        return p
    s = p.replace("\\", "/")
    root = str(project_root or "").replace("\\", "/").rstrip("/")
    if not root:
        return s
    prefixes = [root]
    # Windows drive form <-> MSYS form: C:/foo and /c/foo both reachable
    # depending on whether the path was recorded by Python (C:/) or Git-Bash (/c/).
    if len(root) >= 2 and root[1] == ":":
        prefixes.append("/" + root[0].lower() + root[2:])
    elif len(root) >= 3 and root[0] == "/" and root[2] == "/":
        prefixes.append(root[1].upper() + ":" + root[2:])
    for pre in prefixes:
        if pre and s.startswith(pre + "/"):
            return s[len(pre) + 1:]
    return s


def _read_team_state(world_dir):
    try:
        import yaml

        ts_path = Path(world_dir) / "team-state.yaml"
        data = {}
        if ts_path.exists():
            data = yaml.safe_load(ts_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
        #  sharding: overlay per-agent row files (rows win
        # newest-wins) — partners' in_flight claims live in rows now; the
        # raw core file only carries pre-migration residuals.
        from _team_state import compose_state
        return compose_state(data, Path(world_dir))
    except Exception:
        return {}


def _read_uncommitted_log(agent_dir):
    paths = set()
    max_age_sec = _max_age_sec()
    now_epoch = int(datetime.datetime.now().timestamp())
    try:
        log_path = agent_dir / "session" / "uncommitted-edits.jsonl"
        if not log_path.exists():
            return paths
        for line in log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (ValueError, json.JSONDecodeError):
                continue
            p = rec.get("file") or rec.get("path")
            if not isinstance(p, str):
                continue
            if not p:
                continue
            # : a claim older than the age cutoff is stale and must
            # NOT suppress the committer's legitimate same-session edit.
            if _entry_is_stale(rec, max_age_sec, now_epoch):
                continue
            paths.add(p)
    except OSError:
        return paths
    return paths


def _discover_known_agents(project_root):
    """Directories with local-paths.conf under PROJECT_ROOT/agents/ are
    agent dirs (Phase 2.5.D layout)."""
    agents = set()
    agents_parent = Path(project_root) / "agents"
    if not agents_parent.is_dir():
        return agents
    try:
        for entry in agents_parent.iterdir():
            if not entry.is_dir():
                continue
            if not (entry / "local-paths.conf").exists():
                continue
            agents.add(entry.name)
    except OSError:
        return agents
    return agents


def filter_paths(paths, self_agent, project_root, world_dir):
    """Apply 3-source cross-agent attribution filter.

    Returns (kept, dropped) where dropped is a list of decision records
    with keys {path, reason, owner, claimed_at (optional)}.
    """
    if not paths:
        return [], []

    known_agents = _discover_known_agents(project_root)
    if self_agent not in known_agents:
        return list(paths), []

    partners = known_agents - {self_agent}
    team_state = _read_team_state(world_dir)
    agent_status = (
        team_state.get("agent_status", {})
        if isinstance(team_state.get("agent_status", None), dict)
        else {}
    )

    self_in_flight = (agent_status.get(self_agent) or {}).get("in_flight") or {}
    self_claimed_at = (
        _iso_to_epoch(self_in_flight.get("claimed_at"))
        if isinstance(self_in_flight, dict)
        else 0
    )

    # Source 1: partner in_flight claim timestamps (concurrent work).
    partner_epochs = {}
    partner_isos = {}
    for p in partners:
        in_flight = (agent_status.get(p) or {}).get("in_flight") or {}
        if not isinstance(in_flight, dict):
            continue
        iso = in_flight.get("claimed_at")
        ep = _iso_to_epoch(iso)
        if ep > 0:
            partner_epochs[p] = ep
            partner_isos[p] = iso

    # Source 2: partner uncommitted-edits logs (explicit authorship record).
    # Phase 2.5.D: agent dirs live under PROJECT_ROOT/agents/<name>/.
    # Keys are normalized to PROJECT_ROOT-relative POSIX form () so a
    # legacy absolute log entry and a relative git-diff candidate for the same
    # file compare equal. The owner value is the partner name (unchanged).
    partner_uncommitted = {}
    agents_parent = Path(project_root) / "agents"
    for p in partners:
        for path in _read_uncommitted_log(agents_parent / p):
            partner_uncommitted.setdefault(_normalize_rel_path(path, project_root), p)

    kept = []
    dropped = []
    for path in paths:
        if not path:
            continue

        # Source 2 wins first: an explicit partner authorship record.
        # Normalize the candidate to the same PROJECT_ROOT-relative POSIX form
        # the keys were built with () so an absolute-vs-relative
        # format mismatch does not cause a silent set-membership miss. The
        # original (unnormalized) path is preserved in the decision record so
        # downstream consumers see the path exactly as it was passed in.
        norm_path = _normalize_rel_path(path, project_root)
        if norm_path in partner_uncommitted:
            dropped.append(
                {
                    "path": path,
                    "reason": "partner-uncommitted-log",
                    "owner": partner_uncommitted[norm_path],
                }
            )
            continue

        mtime = _file_mtime(project_root, path)

        # If the file changed at/after THIS agent's own claim, it is
        # plausibly self-authored — do not attribute to a concurrent
        # partner (avoids dropping the agent's own in-claim work).
        skip_concurrent = (self_claimed_at > 0) and (
            mtime + CLOCK_SKEW_SEC >= self_claimed_at
        )

        # Source 1: file mtime at/after a partner's claim → concurrent partner.
        if mtime > 0 and partner_epochs and not skip_concurrent:
            matched_partner = None
            for partner, ep in partner_epochs.items():
                if mtime + CLOCK_SKEW_SEC >= ep:
                    matched_partner = partner
                    break
            if matched_partner:
                dropped.append(
                    {
                        "path": path,
                        "reason": "concurrent-partner",
                        "owner": matched_partner,
                        "claimed_at": partner_isos[matched_partner],
                    }
                )
                continue

        # Source 3: file mtime predates this agent's claim by more than the
        # skew tolerance → partner WIP that existed before we started.
        if mtime > 0 and self_claimed_at > 0:
            if self_claimed_at - mtime > CLOCK_SKEW_SEC:
                dropped.append(
                    {
                        "path": path,
                        "reason": "pre-claim-mtime",
                        "owner": "partner-wip-before-claim",
                    }
                )
                continue

        kept.append(path)

    return kept, dropped


def _resolve_paths():
    project_root = os.environ.get("PROJECT_ROOT", "")
    world_dir = os.environ.get("WORLD_DIR", "")
    if project_root and world_dir:
        return project_root, world_dir
    try:
        script_dir = Path(__file__).resolve().parent
        if str(script_dir) not in sys.path:
            sys.path.insert(0, str(script_dir))
        import _paths

        pr = str(getattr(_paths, "PROJECT_ROOT", "")) or project_root or os.getcwd()
        wd = str(getattr(_paths, "WORLD_DIR", "")) or world_dir or os.getcwd()
        return pr, wd
    except Exception:
        return (project_root or os.getcwd(), world_dir or os.getcwd())


def main(argv):
    argv = argv if argv is not None else sys.argv[1:]
    self_agent = os.environ.get("MIND_AGENT", "")
    project_root, world_dir = _resolve_paths()

    try:
        if argv and argv[0] not in ("-", ""):
            input_text = Path(argv[0]).read_text(encoding="utf-8")
        else:
            input_text = sys.stdin.read()
    except OSError:
        input_text = ""

    paths = [line.strip() for line in input_text.splitlines() if line.strip()]
    if not paths:
        return 0

    # No self identity → cannot attribute; emit input unchanged (fail-open).
    if not self_agent:
        for p in paths:
            sys.stdout.write(p + "\n")
        return 0

    kept, dropped = filter_paths(paths, self_agent, project_root, world_dir)
    for p in kept:
        sys.stdout.write(p + "\n")
    for d in dropped:
        sys.stderr.write(
            "[xagent-filter] DROPPED "
            f"{d['path']} ({d['reason']}, owner={d.get('owner', '?')})\n"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(None))
