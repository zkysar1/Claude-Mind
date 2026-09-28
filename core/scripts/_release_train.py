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

THE BASIS. measure() reads local refs; measure_with_basis() is what every
consumer acts on. The loop's fetch refreshes origin/main but never the tags,
so a box that did not cut the newest tag reads the previous one as newest.
Measured 2026-09-27 (g-115-11144): the check read DUE on v2.12.84 hours after
v2.12.85 was cut and promoted. Driven through the probe, that basis files a
false stall and retires the cutter's correct lease. So a refresh (origin/main
plus the v* tags) precedes any DUE verdict, and a lease is retired only for a
tag strictly older than a newer one the basis can see.

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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent

# The fleet-wide lease key. It names the TAG, never a box or an agent: the
# condition lives on origin/main and a tag is unique across the fleet — the
# same scope as the world-queue dedup that enforces it (guard-2107). Each
# reducer reads it on its OWN refs, so filing or retiring a lease waits for a
# basis refreshed from origin (measure_with_basis, ).
# `investigate:` is a sanctioned prefix (gates/origin_signal.py
# ALLOWED_PREFIXES), so the stored signal is the one written and the dedup
# keyed on it is not vacuous (guard-2329).
SIGNAL_PREFIX = "investigate:release-train-stalled-past-"

# guard-308: a last-resort floor for a corrupt or missing config file, never
# a second copy of the policy. aspirations.yaml `release_train` carries the
# same values and documents WHY.
_CONFIG_FLOOR = {"stale_hours": 24, "ticks_to_file": 1, "ticks_to_revalidate": 50,
                 "skip_hold_hours": 24}

# The refresh that makes a basis tag-bearing. `git fetch origin <branch>`
# brings NO tags: measured 2026-09-27 on git 2.45, not even when the tagged
# commit is new to the clone. Tags come in WITHOUT '+': a local v* tag that
# differs from origin's is refused ("would clobber existing tag") and the
# reading goes unmeasured, rather than silently rewriting a tag someone may be
# half-way through cutting.
_REFRESH_REFSPECS = ("+refs/heads/main:refs/remotes/origin/main",
                     "refs/tags/v*:refs/tags/v*")
# Anything but "" or "0" disables the refresh. A test that reaches this path
# on a real remote sets it rather than touch the network (guard-4582).
NO_FETCH_ENV = "RELEASE_TRAIN_NO_FETCH"

# How the note the probe writes when IT retires a lease opens. last_disposal()
# keys on it to tell "the condition moved on" (a probe retirement) from
# "someone decided about this tag" (a disposal). Change the writer
# (agent-watchdog.py _retire_release_goals) and this together.
PROBE_RETIRE_MARK = "agent-watchdog ReleaseTrainProbe re-measured the release train"
# The terminal statuses a hand disposal leaves: cut and promote, or close
# skipped with why not (promotion-runbook.md "Who cuts, and WHEN").
DISPOSAL_STATUSES = ("skipped", "completed")


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


def _git(root: Path, *args: str, timeout: float = 20.0, env: Optional[dict] = None) -> tuple:
    """One git command in `root`. Never raises; a failure is (rc, '', err)."""
    try:
        proc = subprocess.run(["git", "-C", str(root), *args],
                              capture_output=True, text=True, timeout=timeout,
                              env=None if env is None else {**os.environ, **env})
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except Exception as e:  # noqa: BLE001 — includes TimeoutExpired and FileNotFoundError
        return 127, "", f"{type(e).__name__}: {e}"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def measure(root: Path, paths: list, *, now: Optional[float] = None, sample: int = 5) -> dict:
    """The newest v* tag reachable from origin/main, its age, and the
    framework commits past it — on LOCAL refs, as they stand.

    Local refs alone are not a basis to act on: a reducer's iteration-push
    refreshes origin/main but never the tags, so on every box that did not cut
    the newest tag this names the PREVIOUS one (g-115-11144). A missing tag can
    only make the reading look MORE due. Anything that acts on a verdict goes
    through measure_with_basis(). `fetch_age_minutes` says how old the
    origin/main basis is (guard-2311); `tags_merged` lists every v* tag merged
    into origin/main, newest first by version. Never raises: a git failure sets
    `error` and leaves the numeric fields None.
    """
    now = time.time() if now is None else now
    out = {"newest_tag": None, "tag_created": None, "tag_age_hours": None,
           "commits_past": None, "commit_sample": [], "tags_merged": [],
           "fetch_age_minutes": None, "basis": "origin/main", "error": None}
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
    merged = [t.strip() for t in tags.splitlines() if t.strip()]
    out["tags_merged"] = merged
    if not merged:
        out["error"] = "no v* tag is reachable from origin/main"
        return out
    tag = merged[0]
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


def refresh_basis(root: Path, *, timeout: float = 30.0) -> Optional[str]:
    """Fetch origin/main AND the v* tags into `root`. None on success, else why not.

    Writes refs only (origin/main, v* tags), never the working tree or the
    index. It also writes FETCH_HEAD, which the loop's fetch throttles
    correctly read as "fetched just now". GIT_TERMINAL_PROMPT=0 makes an
    unattended loop fail fast instead of waiting on a credential prompt
    (rb-3231).
    """
    if os.environ.get(NO_FETCH_ENV, "").strip() not in ("", "0"):
        return f"basis refresh disabled ({NO_FETCH_ENV} is set)"
    rc, _, err = _git(root, "fetch", "--quiet", "origin", *_REFRESH_REFSPECS,
                      timeout=timeout, env={"GIT_TERMINAL_PROMPT": "0"})
    return None if rc == 0 else f"git fetch origin rc={rc}: {err[:160]}"


def measure_with_basis(root: Path, paths: list, stale_hours: float, *,
                       always: bool = False, now: Optional[float] = None) -> dict:
    """measure(), on a basis refreshed from origin before a DUE reading counts.

    always=False is for the per-iteration callers (the probe, --nudge). It
    refreshes only when the local reading is DUE or unmeasured. A local
    not-due reading needs no network, because a missing tag can only make a
    reading look MORE due. Each refresh also writes the tags, so a box pays
    once per tag it did not cut, plus once per tick while the train is truly
    stalled.

    always=True is for a reader re-measuring by hand. It refreshes first, so
    the tag it names is origin's newest.

    `tag_basis` is "fetched" or "local[ (refresh failed: ...)]". When the
    refresh fails, a DUE or unmeasured reading comes back UNMEASURED with the
    reason, because acting on the stale reading is exactly the defect
    (g-115-11144).
    """
    m = measure(root, paths, now=now)
    if not always and not m.get("error") and not decide(m, stale_hours)["due"]:
        m["tag_basis"] = "local"
        return m
    why = refresh_basis(root)
    if why is None:
        fresh = measure(root, paths, now=now)
        fresh["tag_basis"] = "fetched"
        return fresh
    m["tag_basis"] = f"local (refresh failed: {why})"
    if m.get("error"):
        m["error"] = f"{m['error']}; the basis refresh also failed: {why}"
    elif decide(m, stale_hours)["due"]:
        m["error"] = (f"basis refresh failed ({why}), so the local reading ({m['newest_tag']} "
                      f"due) is unconfirmed: local refs name the previous tag on any box "
                      f"that did not cut the newest one")
    return m


def lease_tag(goal) -> Optional[str]:
    """The tag a release-train lease is keyed on, or None."""
    signal = goal.get("origin_signal") if isinstance(goal, dict) else None
    if isinstance(signal, str) and signal.startswith(SIGNAL_PREFIX):
        return signal[len(SIGNAL_PREFIX):] or None
    return None


def superseded_leases(goals: list, tags_merged: list) -> list:
    """The leases a NEWER tag on this basis has made moot: those keyed on a tag
    strictly older, in git's version order, than the newest merged tag.

    Never a lease for the newest tag itself, and never one for a tag this basis
    cannot see (not fetched here, or not merged into origin/main). Retiring
    those acts on what this box does not know: driven through the probe, a box
    missing the newest tag closed the cutter's correct lease as "a newer tag has
    been cut" (g-115-11144).
    """
    older = set(tags_merged[1:])
    return [g for g in goals if lease_tag(g) in older]


def _naive_utc(stamp) -> Optional[datetime]:
    """A goal timestamp on the fleet's naive-UTC clock, parsed; None if unreadable."""
    if not isinstance(stamp, str) or not stamp.strip():
        return None
    try:
        dt = datetime.fromisoformat(stamp.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def held_until(prior: Optional[dict], hold_hours: float, *,
               now: Optional[datetime] = None) -> Optional[str]:
    """Until when a hand disposal of the tag still holds re-filing, or None.

    None when there is no disposal, the hold is 0, or the close time is
    unreadable. A detector with an undatable reason to stay quiet stays LOUD:
    it re-files, and the new lease quotes the note.
    """
    if not prior or not hold_hours:
        return None
    closed = _naive_utc(prior.get("completed_at"))
    if closed is None:
        return None
    until = closed + timedelta(hours=hold_hours)
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    return until.strftime("%Y-%m-%dT%H:%M:%S") if now < until else None


def _release_goal_records(world_dir, agent_dir=None):
    """(source, goal) for every goal carrying a release-train signal.

    Direct JSONL read, daemon-free, fail-open to fewer records (the contract of
    pointer_freshness.open_goal_records). A line that is not an aspiration
    object is skipped rather than raised on: a corrupt store must not kill the
    per-iteration --nudge.
    """
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
                    goals = record.get("goals") if isinstance(record, dict) else None
                    if not isinstance(goals, list):
                        continue
                    for goal in goals:
                        if lease_tag(goal):
                            yield source, goal
        except OSError:
            continue


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
    return [dict(goal, _source=source)
            for source, goal in _release_goal_records(world_dir, agent_dir)
            if goal.get("status") in OPEN_STATUSES]


def last_disposal(world_dir, agent_dir, signal: str) -> Optional[dict]:
    """The most recent lease for `signal` closed BY HAND, with `_source`, or None.

    A disposal is a lease left skipped or completed by whoever disposed of it.
    The probe's own retirements (outcome_note opening with PROBE_RETIRE_MARK)
    are not disposals: they record that the condition moved on, not a decision
    about the tag. Ordered by `completed_at`, which the terminal-status
    transition stamps (g-115-661). Reads the LIVE store, and eviction removes a
    terminal goal after aspirations_eviction.age_days, so that age must stay
    longer than release_train.skip_hold_hours (pinned by a test).
    """
    best, best_at = None, None
    for source, goal in _release_goal_records(world_dir, agent_dir):
        if goal.get("origin_signal") != signal or goal.get("status") not in DISPOSAL_STATUSES:
            continue
        if str(goal.get("outcome_note") or "").startswith(PROBE_RETIRE_MARK):
            continue
        at = _naive_utc(goal.get("completed_at"))
        if best is None or (at is not None and (best_at is None or at > best_at)):
            best, best_at = dict(goal, _source=source), at
    return best
