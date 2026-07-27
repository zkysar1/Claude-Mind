#!/usr/bin/env python3
"""Completed-Not-Committed Sweep — flag code-deliverable goals marked
status=completed whose commit never landed on origin.

THE GAP THIS FILLS (rb-3135 / g-115-2570, CONFIRMED hypothesis
2026-07-11_completed-not-committed-recurrence): 4 instances in ~1 week of
goals closed status=completed whose code deliverable never reached origin
(g-115-1938 census audit code, g-115-1987 read_authoritative_bytes,
g-115-2332 git-side stamps absent). Each was caught by hand, none by a gate.
Systemic cause: nothing verifies the commit landed on origin before (or
shortly after) a code-deliverable goal closes completed. world/conventions/
post-execution.md Step 2 makes commit+push the agent's responsibility, but
"push landed" was never verified — so a local commit that was never pushed
(cross-box throttle, a missed push, an aborted iteration-push) closes the
goal completed while origin lacks the deliverable.

DETECTIVE, NOT INLINE (the goal's own design guidance): cross-box push
throttles make close-time origin-absence NORMAL for ~20min, so an inline
verify-phase gate would spam false positives on every fresh close. A sweep
with an AGE THRESHOLD (default 30min via --min-age-minutes) only flags a
commit that has stayed off origin PAST the throttle window — the exact
false-positive-free shape the goal calls for.

DETECTION SHAPE. For each recently-completed goal (completed_at within
--lookback-hours, older than --min-age-minutes) whose recorded evidence names
a commit-SHA-shaped token, resolve which candidate repo the SHA belongs to and
probe `git branch -r --contains <sha>`:
  - SHA is on a remote branch          -> landed, clean (the common case)
  - SHA exists locally but on NO remote -> committed-but-not-pushed  -> FLAG
  - SHA is not a valid commit anywhere  -> claimed commit missing     -> FLAG
The two FLAG classes are exactly the completed!=committed failure the goal
targets. A clean origin-landing is proof the deliverable shipped.

--apply files ONE dedup'd Investigate per flagged goal
(origin_signal="investigate:completed-not-committed-<goal-id>"), routed to
asp-115 (framework hygiene). Without --apply it is report-only (dry run).

PURE CORE. `extract_commit_shas`, `is_code_deliverable`, and `classify_goal`
are pure (no git, no daemon) — the git origin-status of each SHA is INJECTED
as a dict, so the full eligibility ladder is unit-testable with synthetic
goals and a synthetic probe map. main() is the only impure part (daemon read
+ real git probe + Investigate filing).

Sibling pattern (rb-428 detective-sweep family): defer-drift-check.py,
unblock-parent-status-sweep.py, parent-supersession-sweep.py. Guards honored:
guard-420 (datetime arithmetic — fromisoformat + Z-strip + exception-tolerant),
guard-383 (per-source read error fatal — a silent empty-aggregate would hide
origin-absence behind a "0 flagged" lie), guard-645 (field reads .get() with
defaults), guard-614 (structured JSON output). Reference: g-115-2570.
"""

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from _dt import parse_naive_iso  # noqa: E402  (shared tzinfo-stripping naive-ISO parse, )
import _rt  # canonical Python -> daemon client (post-cutover; see _rt.py)

# Commit SHA extraction is KEYWORD-ANCHORED for precision AND to bound the
# probe count. A hex token counts ONLY when it sits next to a commit keyword
# ("committed <sha>", "pushed <sha>", "merge <sha>", "origin/main now at <sha>")
# or is an endpoint of a push range "<sha>..<sha>". Free-floating hex tokens —
# dates ("20260711" is 8 valid hex digits), env ids ("a7cb5456"), session
# hashes — are NOT extracted: the first live run flagged 196/2293 goals
# precisely because UNANCHORED hex matched those. Anchoring + the cat-file
# validation in probe_sha_origin remove the intra-box false positives; the
# CROSS-box false positive (a commit on the remote but unfetched into THIS box's
# origin/* refs) is closed separately by the `git fetch origin` main() runs once
# before probing () — without that fetch `git branch -r --contains`
# reads stale local refs and flags already-shipped cross-box work.
_ANCHORED_SHA_RE = re.compile(
    r"(?:commit(?:ted)?|pushed?|merged?|\bsha\b)"
    r"[\s:=@,]*(?:now\s+at\s+)?([0-9a-f]{7,40})\b",
    re.IGNORECASE)
_ORIGIN_SHA_RE = re.compile(
    r"origin/\S+\s+(?:now\s+at\s+|->\s*)?([0-9a-f]{7,40})\b",
    re.IGNORECASE)
_RANGE_SHA_RE = re.compile(r"\b([0-9a-f]{7,40})\.\.([0-9a-f]{7,40})\b")

# Keywords that mark a goal as producing a code deliverable. Used both to
# gate is_code_deliverable and to prefer SHAs appearing near them.
_COMMIT_KEYWORDS = (
    "commit", "committed", "pushed", "push", "merged", "merge",
    "sha", "origin/", "gh pr", "pull request", "deployed",
)

# Goal string fields most likely to carry the commit evidence. We scan these
# by name first, then fall back to a full-record string walk so a SHA stored
# in an unexpected field is still found.
_EVIDENCE_FIELDS = (
    "outcome_note", "completion_summary", "verify_summary", "summary",
    "description", "notes", "result",
)


def _tolerant_decode(source, raw):
    """guard-383/ tolerant decode for the daemon aspirations_read body."""
    return _rt.tolerant_decode_aggregate(
        f"completed-not-committed-sweep: {source}", raw)


def _read_goals(source):
    """Read all goals (any status) from active aspirations in one queue.

    guard-383 fatal symmetry (rb-987): a per-source read error in an N>=2
    source aggregator MUST be fatal — a silent `return []` writes a
    complete-looking lie ("0 flagged") into the merged aggregate.
    """
    try:
        out = _rt.aspirations_read(source=source, active=True)
    except _rt.RtError as e:
        print(f"[completed-not-committed-sweep] {source} read failed: "
              f"{e.body or e}", file=sys.stderr)
        sys.exit(1)  # guard-383: source error fatal
    data = _tolerant_decode(source, out)
    if data is None:
        return []
    goals = []
    for asp in (data.get("aspirations") if isinstance(data, dict) else data) or []:
        for g in asp.get("goals", []) or []:
            g["_source"] = source
            g["_aspiration_id"] = asp.get("id")
            goals.append(g)
    return goals


def _parse_iso(ts):
    """Tolerant ISO parse (guard-420). Returns datetime or None — never raises."""
    if not ts:
        return None
    try:
        return parse_naive_iso(ts)
    except Exception:
        return None


def _walk_strings(obj):
    """Yield every string value nested anywhere in a JSON-ish structure."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk_strings(v)


def extract_commit_shas(goal):
    """Return a de-duplicated list of commit-SHA-shaped tokens found in the
    goal's evidence. PURE. Prefers the named evidence fields, then falls back
    to a full-record walk. The tokens are CANDIDATES — main() validates each
    against real repos before treating it as a commit (a random hex token
    that is not a real commit resolves to status None and is ignored unless
    the goal actually claims a commit; see classify_goal).
    """
    seen = []
    seen_set = set()

    def _collect(text):
        text = text or ""
        toks = []
        for m in _ANCHORED_SHA_RE.finditer(text):
            toks.append(m.group(1))
        for m in _ORIGIN_SHA_RE.finditer(text):
            toks.append(m.group(1))
        for m in _RANGE_SHA_RE.finditer(text):
            toks.append(m.group(1))
            toks.append(m.group(2))
        for tok in toks:
            if tok not in seen_set:
                seen_set.add(tok)
                seen.append(tok)

    for field in _EVIDENCE_FIELDS:
        val = goal.get(field)
        if isinstance(val, str):
            _collect(val)
    # verification sub-fields (summary lands here on some close paths)
    ver = goal.get("verification") or {}
    if isinstance(ver, dict):
        for s in _walk_strings(ver):
            _collect(s)
    return seen


def is_code_deliverable(goal):
    """True when the goal produced (or claims to have produced) a code
    deliverable that SHOULD land on origin. PURE.

    Signals (any):
      - work_class names a code lane (framework / product / hygiene-with-code)
      - evidence text carries a commit keyword (committed/pushed/merged/...)
      - a commit-SHA-shaped token is present
    A goal with none of these is treated as non-code (docs/tree/journal-only)
    and skipped — we never want to flag a knowledge-only close for "no commit".
    """
    wc = (goal.get("work_class") or "").lower()
    if wc in ("framework", "product"):
        return True
    blob = " ".join(_walk_strings({
        k: goal.get(k) for k in _EVIDENCE_FIELDS
    })).lower()
    ver = goal.get("verification") or {}
    if isinstance(ver, dict):
        blob += " " + " ".join(_walk_strings(ver)).lower()
    if any(kw in blob for kw in _COMMIT_KEYWORDS):
        return True
    if extract_commit_shas(goal):
        return True
    return False


def classify_goal(goal, now, sha_status, min_age_minutes=30.0,
                  lookback_hours=168.0, goalid_status=None):
    """Pure eligibility test for ONE goal. Returns a flag entry dict when the
    goal is a completed code deliverable whose commit is absent from origin,
    else None.

    `sha_status` is the INJECTED probe map {sha: True|False|None}:
      True  -> SHA is on a remote branch (landed)
      False -> SHA exists locally but on no remote (committed, not pushed)
      None  -> SHA is not a valid commit in any candidate repo (missing)
    A goal is FLAGGED when it has >=1 extracted SHA and NONE of its SHAs are
    on origin (i.e. every extracted SHA is False or None). If ANY SHA landed,
    the deliverable shipped -> clean (a goal may name several SHAs; one landing
    is proof of the deliverable, so we only flag total origin-absence).

    `goalid_status` is the INJECTED goal-id-resolution map
    {goal_id: {sha: True|False|None}}, built by build_goalid_status for goals
    with ZERO extracted SHAs (g-115-2600). Loop-commit messages embed the
    goal-id, not a SHA (rb-3999), so the common phantom record shape carries no
    SHA token and the extracted-SHA path above is structurally blind to it. When
    a zero-SHA code goal's id resolves (via git log --grep '(<goal-id>)') to a
    local-only commit, the SAME landing check applies. None-status resolved
    tokens are dropped, exactly like the extracted-SHA path.

    Age gate: completed_at must be OLDER than min_age_minutes (push-throttle
    guard — no false positives on fresh closes) and WITHIN lookback_hours
    (keep the report recent + bounded).
    """
    if goal.get("status") != "completed":
        return None
    if not is_code_deliverable(goal):
        return None
    completed_at = _parse_iso(goal.get("completed_at"))
    if completed_at is None:
        return None
    age_minutes = (now - completed_at).total_seconds() / 60.0
    if age_minutes < min_age_minutes:
        return None  # inside the push-throttle window — not yet actionable
    if age_minutes > lookback_hours * 60.0:
        return None  # too old — outside the actionable lookback window
    shas = extract_commit_shas(goal)
    if not shas:
        # Zero SHA tokens in the record. Loop-commit messages embed the goal-id,
        # not a SHA (rb-3999), so the COMMON phantom shape carries no SHA and the
        # extracted-SHA path above never sees it (: both 2026-07-18
        # phantoms had zero SHA tokens). Fall back to goal-id resolution: main()
        # injects goalid_status[goal_id] = {sha: origin_status} for commits whose
        # message carries "(<goal-id>)". Apply the SAME landing check + None-drop.
        resolved = (goalid_status or {}).get(goal.get("id")) or {}
        if not resolved:
            # No commit found by id either -> genuinely out of scope (a legitimate
            # narrative/docs close names no SHA and produced no goal-id commit).
            return None
        if any(st is True for st in resolved.values()):
            return None  # a goal-id-resolved commit landed on origin -> clean
        absent = [s for s, st in resolved.items() if st is False]
        if not absent:
            return None  # only None-status tokens -> drop (same guard as SHA path)
        return {
            "goal_id": goal.get("id"),
            "source": goal.get("_source"),
            "aspiration_id": goal.get("_aspiration_id"),
            "completed_at": goal.get("completed_at"),
            "age_hours": round(age_minutes / 60.0, 1),
            "reason": "committed_not_pushed",
            "shas_absent_local_only": absent,
            "resolved_via": "goal-id",
            "title": (goal.get("title") or "")[:80],
        }
    statuses = [sha_status.get(s) for s in shas]
    if any(st is True for st in statuses):
        return None  # at least one SHA landed on origin — deliverable shipped
    # RELIABLE SIGNAL ONLY: a token that `git cat-file -e` VALIDATED as a real
    # local commit (status False) that is on NO remote branch. A None-status
    # token is almost always NOT a commit — a date ("20260711" is 8 valid hex
    # digits), an env/session id ("a7cb5456"), or a message hash — and flagging
    # those produced a 196/2293 false-positive flood in the first live run. So
    # None is dropped entirely; the "never-committed" case (no valid SHA at all)
    # is deliberately out of scope (undetectable false-positive-free from prose).
    absent = [s for s in shas if sha_status.get(s) is False]
    if not absent:
        return None
    return {
        "goal_id": goal.get("id"),
        "source": goal.get("_source"),
        "aspiration_id": goal.get("_aspiration_id"),
        "completed_at": goal.get("completed_at"),
        "age_hours": round(age_minutes / 60.0, 1),
        "reason": "committed_not_pushed",
        "shas_absent_local_only": absent,
        "title": (goal.get("title") or "")[:80],
    }


def apply_superseded(entry, superseded_status):
    """Mark a flagged committed_not_pushed entry benign_superseded when EVERY
    local-only SHA's changed files are already byte-identical in HEAD. PURE
    (git status injected via superseded_status={sha: True|False}). g-115-3032.

    This distinguishes a convergent-parallel-fix ORPHAN — a narrow local fix
    superseded+generalized by another box's commit at merge, so the SHA is
    absent from origin yet its DELIVERABLE is on origin under a DIFFERENT SHA —
    from a genuinely lost deliverable. The orphan is a benign false positive
    that cost an Investigate each run under SHA-absence-alone flagging
    (g-115-3031: fcb8dd05e superseded by g-115-3023/3024).

    A benign_superseded entry stays in the REPORT (log-only visibility) but is
    NOT Investigate-filed by main() --apply. An entry keeps
    reason=committed_not_pushed (and IS filed) when >=1 of its local-only SHAs
    is NOT superseded — i.e. >=1 changed file's content is genuinely absent from
    HEAD — preserving the g-115-2570 / rb-3135 real-loss detection this sweep
    exists for. Requiring ALL absent SHAs superseded (not any) is the
    conservative direction: one un-superseded SHA is enough to keep the flag.
    """
    absent = entry.get("shas_absent_local_only") or []
    benign = bool(absent) and all(
        superseded_status.get(s) is True for s in absent)
    entry["benign_superseded"] = benign
    if benign:
        entry["reason"] = "benign_superseded"
    return entry


# ─────────────────────────── impure git probe ───────────────────────────

def discover_candidate_repos():
    """Candidate git repos a completed goal's commit could live in: the MIND
    repo (PROJECT_ROOT) plus every 1-level git repo under each AGENT_WRITE_PATH
    parent (product estate). Impure (filesystem). Deduped, order-stable."""
    repos = []
    seen = set()

    def _add(p):
        rp = str(p)
        if rp not in seen and (p / ".git").exists():
            seen.add(rp)
            repos.append(p)

    _add(PROJECT_ROOT)
    for parent in _resolve_write_parents():
        pp = Path(parent)
        if not pp.is_dir():
            continue
        _add(pp)  # parent may itself be a repo
        try:
            for sub in sorted(pp.iterdir()):
                if sub.is_dir():
                    _add(sub)
        except OSError:
            continue
    return repos


def _resolve_write_parents():
    """Return the AGENT_WRITE_PATH parent dirs (';'-separated). Env first, then
    <agent>/local-paths.conf — the conf is the SSOT and env is usually unset in
    a subprocess (mirrors _target_state._resolve_search_roots). Never raises."""
    awp_env = os.environ.get("AGENT_WRITE_PATH", "")
    if awp_env.strip():
        return [p.strip() for p in awp_env.split(";") if p.strip()]
    try:
        import _paths
        agent = os.environ.get("MIND_AGENT")
        if not agent:
            return []
        conf = _paths.agent_dir(agent) / "local-paths.conf"
        if not conf.is_file():
            return []
        for line in conf.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("AGENT_WRITE_PATH=") and "=" in line:
                raw = line.split("=", 1)[1].strip().strip('"').strip("'")
                return [p.strip() for p in raw.split(";") if p.strip()]
    except Exception:
        pass
    return []


def _git(repo, *args, timeout=15):
    """Run a git command in repo; return (rc, stdout). Never raises."""
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip()
    except Exception:
        return 1, ""


def _fetch_origin(candidate_repos):
    """Refresh each candidate repo's origin/* remote-tracking refs BEFORE
    probe_sha_origin reads them (g-115-2660). probe_sha_origin resolves
    origin-landing via `git branch -r --contains <sha>`, which reads box-LOCAL
    origin/* refs. On a multi-box fleet a commit that landed on the remote from
    ANOTHER box but was never fetched here reads as local-only -> false-positive
    committed_not_pushed (observed g-315-320 / g-315-344 / g-001-49 flagged while
    already on origin/main; a bare `git fetch` flipped all three to clean; and
    g-115-2653's 512328649, which g-115-2659 was wrongly filed to "rebuild").
    One fetch per repo makes the refs current. `git fetch` only updates refs — it
    NEVER pushes and NEVER touches the working tree. Fail-open: a fetch failure
    (offline, no origin remote) is logged to stderr and the sweep proceeds with
    whatever refs exist (degrades to the pre-fix stale-ref behavior, never blocks).
    Returns {repo_str: 'ok'|'failed'|'no-origin'} for observability."""
    result = {}
    for repo in candidate_repos:
        rc_remote, remotes = _git(repo, "remote")
        if rc_remote != 0 or "origin" not in remotes.split():
            result[str(repo)] = "no-origin"
            continue
        rc, _ = _git(repo, "fetch", "origin", "--quiet", timeout=90)
        if rc == 0:
            result[str(repo)] = "ok"
        else:
            result[str(repo)] = "failed"
            print(f"[completed-not-committed-sweep] WARN: `git fetch origin` failed "
                  f"in {repo} (rc={rc}) — proceeding with STALE origin refs; cross-box "
                  f"false-positive committed_not_pushed possible this run (g-115-2660).",
                  file=sys.stderr)
    return result


def probe_sha_origin(sha, candidate_repos):
    """Resolve origin-landing status for one SHA across candidate repos.
    Returns True (on a remote branch), False (local-only, no remote), or
    None (not a valid commit in any candidate repo). Impure."""
    for repo in candidate_repos:
        rc, _ = _git(repo, "cat-file", "-e", f"{sha}^{{commit}}")
        if rc != 0:
            continue  # not in this repo
        rc2, out = _git(repo, "branch", "-r", "--contains", sha)
        if rc2 == 0 and out.strip():
            return True   # contained by >=1 remote branch — landed
        return False      # exists locally, on no remote branch
    return None           # not a real commit anywhere we can see


def build_sha_status(goals, candidate_repos):
    """Probe every unique extracted SHA once (cache). Impure."""
    status = {}
    for g in goals:
        for sha in extract_commit_shas(g):
            if sha not in status:
                status[sha] = probe_sha_origin(sha, candidate_repos)
    return status


def sha_superseded(sha, candidate_repos):
    """True when SHA's changed files are byte-identical in HEAD — the deliverable
    is already present under HEAD's lineage (benign convergent-parallel-fix
    orphan, g-115-3032), NOT a lost deliverable. Impure (git).

    CONSERVATIVE by construction: any inability to determine (SHA in no candidate
    repo, root/merge parent-diff error, no changed files) returns False so the
    entry stays FLAGGED. The direction of safety is deliberate — a missed
    suppression costs one benign Investigate; a wrong suppression would hide a
    real lost deliverable (the exact failure g-115-2570 / rb-3135 exists to
    catch). We compare against HEAD (not origin/main) as the goal specifies:
    if local HEAD is behind origin the diff is non-empty -> the entry stays
    flagged, which is the safe direction (never a false suppression)."""
    for repo in candidate_repos:
        rc, _ = _git(repo, "cat-file", "-e", f"{sha}^{{commit}}")
        if rc != 0:
            continue  # not in this repo
        rc_f, files_out = _git(repo, "diff", "--name-only", f"{sha}^", sha)
        if rc_f != 0:
            return False  # root/merge/parent-resolve error — cannot determine
        files = [f for f in files_out.splitlines() if f.strip()]
        if not files:
            return False  # no changed files — nothing to prove present in HEAD
        # `git diff --quiet <sha> HEAD -- <files>` exits 0 when <sha> and HEAD
        # are identical on <files> (deliverable present in HEAD -> superseded),
        # 1 when >=1 file's content differs/absent (-> keep the flag).
        rc_d, _ = _git(repo, "diff", "--quiet", sha, "HEAD", "--", *files)
        return rc_d == 0
    return False  # SHA in no candidate repo — cannot check, keep the flag


def build_superseded_status(shas, candidate_repos):
    """Probe each flagged local-only SHA once for superseded-in-HEAD status.
    Returns {sha: bool}. Impure. g-115-3032."""
    status = {}
    for sha in shas:
        if sha not in status:
            status[sha] = sha_superseded(sha, candidate_repos)
    return status


def resolve_shas_by_goal_id(goal_id, candidate_repos):
    """Find commit SHAs whose message carries the iteration-commit scope
    "(<goal_id>)" across candidate repos. Impure (git). Loop-commit messages
    embed the goal-id, not a SHA (rb-3999), so a phantom record with zero SHA
    tokens is still resolvable by id. The PARENTHESIZED form is collision-safe:
    "(g-115-260)" does NOT match a commit tagged "(g-115-2600)" (contrast a bare
    id substring). Returns a deduped, order-stable list of full 40-char SHAs.
    g-115-2600."""
    if not goal_id:
        return []
    needle = f"({goal_id})"
    found = []
    seen = set()
    for repo in candidate_repos:
        rc, out = _git(repo, "log", "--all", "-F", f"--grep={needle}",
                       "--format=%H")
        if rc != 0 or not out:
            continue
        for line in out.splitlines():
            sha = line.strip()
            if sha and sha not in seen:
                seen.add(sha)
                found.append(sha)
    return found


def build_goalid_status(goals, candidate_repos, now,
                        min_age_minutes=30.0, lookback_hours=168.0):
    """For each completed code-deliverable goal with ZERO extracted SHAs that
    passes the age gate, resolve commits by goal-id and probe each. Returns
    {goal_id: {sha: origin_status}}. Impure. BOUNDED to the eligible subset so
    the per-goal `git log --grep` cost falls only on the real zero-SHA phantom
    candidates, not every goal (this sweep is a 24h detective, not a hot path).
    g-115-2600."""
    result = {}
    for g in goals:
        if g.get("status") != "completed":
            continue
        if not is_code_deliverable(g):
            continue
        if extract_commit_shas(g):
            continue  # the extracted-SHA path already covers this goal
        completed_at = _parse_iso(g.get("completed_at"))
        if completed_at is None:
            continue
        age_minutes = (now - completed_at).total_seconds() / 60.0
        if age_minutes < min_age_minutes or age_minutes > lookback_hours * 60.0:
            continue
        gid = g.get("id")
        if not gid or gid in result:
            continue
        shas = resolve_shas_by_goal_id(gid, candidate_repos)
        if not shas:
            continue
        result[gid] = {s: probe_sha_origin(s, candidate_repos) for s in shas}
    return result


# ─────────────────────────── Investigate filing ───────────────────────────

def _existing_investigate(goal_id, all_goals):
    """True when an open Investigate for this goal already exists (dedup on the
    stable origin_signal key). Scans the goals ALREADY read this run — asp-115,
    where these Investigates are filed, is an active aspiration, so a prior
    Investigate is present in `all_goals`. In-memory (no extra daemon round-
    trip) and inherently fail-closed: _read_goals is guard-383 fatal on a read
    error, so we never reach here with a partial queue."""
    key = f"investigate:completed-not-committed-{goal_id}"
    for g in all_goals:
        if (g.get("origin_signal") == key
                and g.get("status") in ("pending", "in-progress")):
            return True
    return False


def _file_investigate(entry):
    """File one Investigate goal into  (world). Returns the filed goal
    id or None. Idempotent via _existing_investigate (checked by caller)."""
    gid = entry["goal_id"]
    body = {
        "title": f"Investigate: {gid} closed completed but commit absent from origin",
        "priority": "HIGH",
        "participants": ["agent"],
        "category": "framework-architecture",
        "origin_signal": f"investigate:completed-not-committed-{gid}",
        "description": (
            f"completed-not-committed-sweep flagged {gid} "
            f"('{entry['title']}', source={entry['source']}, "
            f"completed {entry['age_hours']}h ago). Reason: {entry['reason']}. "
            f"Commit(s) present locally but on NO remote branch: "
            f"{entry['shas_absent_local_only']}"
            f"{' (found by goal-id commit-scope match, not named in the goal record — rb-3999)' if entry.get('resolved_via') == 'goal-id' else ''}. "
            f"The goal closed status=completed but its code deliverable is not "
            f"on origin past the push-throttle window. Push the commit (or re-do "
            f"the work if it was lost) and confirm origin landing. "
            f"rb-3135 / g-115-2570 completed!=committed class."),
    }
    try:
        # aspirations_add_goal(asp_id, record, source=...) — record is the dict
        # itself; the daemon returns the parsed created-goal record (a dict).
        out = _rt.aspirations_add_goal("asp-115", body, source="world")
        if isinstance(out, dict):
            return out.get("id") or (out.get("goal") or {}).get("id")
        return None
    except Exception as e:
        print(f"[completed-not-committed-sweep] filing Investigate for {gid} "
              f"failed: {e}", file=sys.stderr)
        return None


def main():
    ap = argparse.ArgumentParser(
        description=("Flag code-deliverable goals closed status=completed whose "
                     "commit is absent from origin past an age threshold. "
                     "Detective sweep (rb-428 family). g-115-2570."))
    ap.add_argument("--output", choices=["json", "human"], default="json")
    ap.add_argument("--apply", action="store_true",
                    help="File a dedup'd Investigate per flagged goal (default: "
                         "report-only dry run).")
    ap.add_argument("--min-age-minutes", type=float, default=30.0,
                    help="Only flag goals completed at least this many minutes "
                         "ago (push-throttle false-positive guard; default 30).")
    ap.add_argument("--lookback-hours", type=float, default=168.0,
                    help="Only consider goals completed within this window "
                         "(default 168 = 7 days).")
    args = ap.parse_args()

    now = dt.datetime.now()
    all_goals = _read_goals("world") + _read_goals("agent")
    candidate_repos = discover_candidate_repos()
    # : refresh origin/* refs ONCE before probing `branch -r --contains`,
    # else a cross-box commit (landed on the remote from another box, unfetched
    # here) reads local-only and false-positives as committed_not_pushed. Fetch
    # is refs-only (never pushes / never touches the tree) and fail-open.
    fetch_status = _fetch_origin(candidate_repos)
    sha_status = build_sha_status(all_goals, candidate_repos)
    goalid_status = build_goalid_status(all_goals, candidate_repos, now,
                                        args.min_age_minutes, args.lookback_hours)

    scanned = 0
    flagged = []
    for g in all_goals:
        scanned += 1
        entry = classify_goal(g, now, sha_status,
                              args.min_age_minutes, args.lookback_hours,
                              goalid_status=goalid_status)
        if entry is not None:
            flagged.append(entry)

    flagged.sort(key=lambda e: e["age_hours"], reverse=True)

    # : distinguish a convergent-parallel-fix ORPHAN (benign — the
    # narrow local fix's changed files are already identical in HEAD, so the
    # DELIVERABLE is on origin under a different SHA) from a genuinely lost
    # deliverable. Probe each flagged local-only SHA for superseded-in-HEAD
    # status and mark benign entries; benign entries stay in the report
    # (benign_superseded, log-only) but are NOT Investigate-filed. Preserves the
    # real-loss detection (>=1 changed file's content genuinely absent from
    # HEAD) that  / rb-3135 targets, while cutting the false-positive
    # Investigate noise (: fcb8dd05e superseded by /3024).
    flagged_shas = sorted({
        s for e in flagged for s in (e.get("shas_absent_local_only") or [])})
    superseded_status = build_superseded_status(flagged_shas, candidate_repos)
    for e in flagged:
        apply_superseded(e, superseded_status)
    benign_superseded = [e for e in flagged if e.get("benign_superseded")]
    real_flagged = [e for e in flagged if not e.get("benign_superseded")]

    investigate_created = []
    if args.apply:
        for entry in real_flagged:
            if _existing_investigate(entry["goal_id"], all_goals):
                continue
            gid = _file_investigate(entry)
            if gid:
                investigate_created.append(gid)

    result = {
        "scanned": scanned,
        "candidate_repos": [str(r) for r in candidate_repos],
        "fetch_status": fetch_status,
        "flagged_count": len(real_flagged),
        "flagged": real_flagged,
        "benign_superseded_count": len(benign_superseded),
        "benign_superseded": benign_superseded,
        "investigate_created": investigate_created,
        "applied": bool(args.apply),
        "now": now.isoformat(timespec="seconds"),
    }

    if args.output == "human":
        print(f"scanned={scanned} flagged={len(real_flagged)} "
              f"benign_superseded={len(benign_superseded)} "
              f"repos={len(candidate_repos)} applied={bool(args.apply)}")
        for e in real_flagged:
            print(f"  [{e['reason']}] {e['goal_id']} ({e['source']}): "
                  f"completed {e['age_hours']}h ago | "
                  f"local-only={e['shas_absent_local_only']} "
                  f"via={e.get('resolved_via', 'record-sha')} | {e['title']}")
        for e in benign_superseded:
            print(f"  [benign_superseded log-only] {e['goal_id']} ({e['source']}): "
                  f"deliverable present in HEAD under a different SHA | "
                  f"local-only={e['shas_absent_local_only']} | {e['title']}")
    else:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
