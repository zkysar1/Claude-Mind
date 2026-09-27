"""_release_train.py — the release train's time-push trigger ().

promotion-runbook.md "Who cuts, and WHEN" (g-373-82) makes two states
actionable, and until this module nothing detected either of them. This is
the TIME-PUSH half: a gap of >= `release_train.stale_hours` since the newest
v* tag, with framework commits on origin/main past it, is a FINDING someone
must dispose of — cut and promote, or record why not. It is NOT an automatic
cut. Measured before this existed (2026-09-26): a 38.8h gap went unflagged
until a goal that needed the cut happened to be picked up through a directive.

The DEMAND-PULL half (an open goal waiting on a dev commit that is in no tag)
is not detected here.

Two consumers, one implementation — a second predicate would be free to drift:
  - `agent-watchdog.py` ReleaseTrainProbe (reducer tick) — files, and later
    retires, the ONE fleet-wide Investigate goal for the stalled tag.
  - `release-train-check.py` — the read-only CLI a reader re-measures with,
    and the in-turn `--nudge` line iteration-close.sh prints while that goal
    is open.

WHAT COUNTS AS A COMMIT PAST THE TAG. Only non-merge commits that touch the
promotion's copy set: promotion-preflight.py FRAMEWORK_PATHS, read through
framework_pull.framework_paths and never re-declared here. Every box commits
its own agent-state churn to main continuously (measured 2026-09-27: 351
commits past the newest tag, 173 non-merge, 27 touching the copy set), so
counting all of them
would fire on every tag the moment it turned stale_hours old — and a finding
that always fires trains its reader to skip it (guard-5202).

FRONTIER ONLY. Releases are cut at the frontier role of the promotion chain
(core/config/compatibility.yaml). A deployment's role is the world overlay's
`self_role` — the same source check-upstream.sh, promote-to-upstream.sh and
check-releases-current.sh read. Anywhere else, or when the role is
unreadable, the train is not this deployment's to cut and callers stay
silent rather than guess: a downstream alarm about a train it cannot run
would be pure noise.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent

# The fleet-wide lease key. It names the TAG, never a box or an agent: the
# condition lives on origin/main, every frontier reducer measures the same
# thing, and a tag is unique across the fleet — the same scope as the
# world-queue dedup that enforces it (guard-2107). `investigate:` is a
# sanctioned prefix (gates/origin_signal.py ALLOWED_PREFIXES), so the stored
# signal is the one written and the dedup keyed on it is not vacuous
# (guard-2329).
SIGNAL_PREFIX = "investigate:release-train-stalled-past-"

# guard-308: a last-resort floor for a corrupt or missing config file, never
# a second copy of the policy. aspirations.yaml `release_train` carries the
# same values and documents WHY.
_CONFIG_FLOOR = {"stale_hours": 24, "ticks_to_file": 1, "ticks_to_revalidate": 50}


def signal_for(tag: str) -> str:
    return f"{SIGNAL_PREFIX}{tag}"


def config(config_path: Optional[Path] = None) -> dict:
    """The `release_train` block of core/config/aspirations.yaml, floored."""
    path = config_path or (SCRIPT_DIR.parent / "config" / "aspirations.yaml")
    try:
        with open(path, encoding="utf-8") as f:
            block = (yaml.safe_load(f) or {}).get("release_train") or {}
    except Exception:  # noqa: BLE001 — a config read must never kill a caller
        block = {}
    out = dict(_CONFIG_FLOOR)
    for key in out:
        val = block.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val >= 0:
            out[key] = val
    return out


def self_role(world_dir) -> Optional[str]:
    """This deployment's promotion-chain role from the world overlay, or None."""
    if not world_dir:
        return None
    try:
        text = (Path(world_dir) / "config" / "compatibility.yaml").read_text(encoding="utf-8")
        role = (yaml.safe_load(text) or {}).get("self_role")
    except Exception:  # noqa: BLE001 — unreadable role = not this deployment's train
        return None
    return role.strip() if isinstance(role, str) and role.strip() else None


def framework_paths() -> list:
    """The promotion copy set, via the one reader that already exists."""
    from framework_pull import framework_paths as _read
    return _read(SCRIPT_DIR)


def _git(root: Path, *args: str, timeout: float = 20.0) -> tuple:
    """One git command in `root`. Never raises; a failure is (rc, '', err)."""
    try:
        proc = subprocess.run(["git", "-C", str(root), *args],
                              capture_output=True, text=True, timeout=timeout)
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except Exception as e:  # noqa: BLE001 — includes TimeoutExpired and FileNotFoundError
        return 127, "", f"{type(e).__name__}: {e}"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def measure(root: Path, paths: list, *, now: Optional[float] = None, sample: int = 5) -> dict:
    """The newest v* tag reachable from origin/main, its age, and the
    framework commits past it.

    Reads LOCAL refs only — origin/main and tags as of the last fetch, which a
    reducer's iteration-push refreshes every iteration. `fetch_age_minutes`
    says how old that basis is, so a reader can tell a fresh count from a
    cached one (guard-2311). Never raises: a git failure sets `error` and
    leaves the numeric fields None.
    """
    now = time.time() if now is None else now
    out = {"newest_tag": None, "tag_created": None, "tag_age_hours": None,
           "commits_past": None, "commit_sample": [], "fetch_age_minutes": None,
           "basis": "origin/main", "error": None}
    rc, git_path, _ = _git(root, "rev-parse", "--git-path", "FETCH_HEAD")
    if rc == 0 and git_path:
        fetch_head = Path(git_path) if os.path.isabs(git_path) else Path(root) / git_path
        try:
            out["fetch_age_minutes"] = round((now - fetch_head.stat().st_mtime) / 60.0, 1)
        except OSError:
            pass
    rc, tags, err = _git(root, "tag", "--merged", "origin/main", "--list", "v*",
                         "--sort=-v:refname")
    if rc != 0:
        out["error"] = f"git tag --merged origin/main rc={rc}: {err[:160]}"
        return out
    tag = next((t.strip() for t in tags.splitlines() if t.strip()), None)
    if tag is None:
        out["error"] = "no v* tag is reachable from origin/main"
        return out
    out["newest_tag"] = tag
    rc, created, err = _git(root, "for-each-ref", "--format=%(creatordate:unix)",
                            f"refs/tags/{tag}")
    try:
        ts = float(created.split()[0])
    except (IndexError, ValueError):
        out["error"] = f"creation date of {tag} unreadable (rc={rc}): {err[:120] or created[:80]!r}"
        return out
    out["tag_created"] = _iso(ts)
    out["tag_age_hours"] = round((now - ts) / 3600.0, 1)
    rc, count, err = _git(root, "rev-list", "--count", "--no-merges",
                          f"{tag}..origin/main", "--", *paths)
    if rc != 0 or not count.isdigit():
        out["error"] = f"rev-list {tag}..origin/main rc={rc}: {err[:160]}"
        return out
    out["commits_past"] = int(count)
    if out["commits_past"] and sample > 0:
        rc, log, _ = _git(root, "log", "--no-merges", f"--max-count={sample}",
                          "--format=%h %s", f"{tag}..origin/main", "--", *paths)
        if rc == 0:
            out["commit_sample"] = [ln[:120] for ln in log.splitlines() if ln.strip()]
    return out


def decide(m: dict, stale_hours: float) -> dict:
    """{"due": bool, "reason": str} — due only when BOTH halves hold."""
    if m.get("error"):
        return {"due": False, "reason": f"unmeasured: {m['error']}"}
    tag, age, n = m["newest_tag"], m["tag_age_hours"], m["commits_past"]
    if not n:
        return {"due": False, "reason": f"no framework commits past {tag} on origin/main"}
    if age < stale_hours:
        return {"due": False,
                "reason": f"{tag} is {age}h old (< {stale_hours}h) with {n} framework commit(s) past it"}
    return {"due": True,
            "reason": (f"{tag} is {age}h old with {n} framework commit(s) past it on "
                       f"origin/main (>= {stale_hours}h)")}


def open_release_goals(world_dir, agent_dir=None) -> list:
    """Every OPEN goal carrying a release-train signal, each with `_source`.

    Same contract and the same OPEN_STATUSES as
    pointer_freshness.open_goal_records (direct JSONL read, daemon-free,
    fail-open to fewer records), widened from one exact signal to the
    PREFIX: the retire path must find a lease filed for ANY older tag, by
    this box or another, or a filed goal outlives its condition forever
    (guard-3419).
    """
    from pointer_freshness import OPEN_STATUSES
    out = []
    queues = []
    if world_dir:
        queues.append(("world", Path(world_dir) / "aspirations.jsonl"))
    if agent_dir:
        queues.append(("agent", Path(agent_dir) / "aspirations.jsonl"))
    for source, path in queues:
        try:
            if not path.exists():
                continue
            with open(path, encoding="utf-8", errors="replace") as f:
                for raw in f:
                    if SIGNAL_PREFIX not in raw:
                        continue
                    try:
                        record = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    for goal in record.get("goals") or []:
                        signal = goal.get("origin_signal") or ""
                        if signal.startswith(SIGNAL_PREFIX) and goal.get("status") in OPEN_STATUSES:
                            out.append(dict(goal, _source=source))
        except OSError:
            continue
    return out
