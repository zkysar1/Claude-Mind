"""Uncommitted-work gate logic (pure-ish — explicit side effect on override).

Refuses goal closure when framework code is dirty in the repo. See the CLI
wrapper at core/scripts/uncommitted-work-gate.py for the original docstring
and the orphan-code-audit rationale.

Public API:
    evaluate(goal_id, override, repo_path, world_dir, agent_name) -> dict

Output dict shape (matches the legacy CLI's JSON payload byte-for-byte):
    {
      "would_block": bool,
      "dirty_framework_files": [str, ...],   # sorted, deduped, repo-relative
      "repo_path": str,
      "goal_id": str,
      "override_applied": str | None,
    }

Side effect:
    When `override` is a non-empty string AND dirty_framework_files is
    non-empty, this function appends one record to
    `<world_dir>/uncommitted-work-overrides.jsonl` via the shared
    `_fileops.locked_append_jsonl` primitive. The audit-log write is
    fail-open (errors print to stderr but never propagate).

    If `world_dir` is None (caller couldn't resolve WORLD_DIR), the
    override is still accepted at the gate level but no ledger entry is
    written — same fail-open behavior as the legacy CLI.

Daemon safety:
    - Reads no environment variables. MIND_AGENT is passed in via
      `agent_name`. WORLD_DIR is passed in via `world_dir`. The function
      is therefore safe to call from any daemon thread without racing
      on per-request state.
    - Imports `locked_append_jsonl` at module load. That import chain
      pulls in `_paths` which reads env vars at module-load time — but
      this gate doesn't consume any of those globals, so the daemon's
      single `_paths` import doesn't pollute per-request behavior here.
    - `subprocess.run(["git", ...])` runs as a child process and does
      not interact with daemon state.
"""
from __future__ import annotations

import datetime as dt
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

# _fileops lives at core/scripts/_fileops.py — our parent dir. The CLI
# wrapper has sys.path set up to find it; daemon callers do too via
# `mind_api/src/file_locks.py`'s sys.path insertion.
from _fileops import locked_append_jsonl  # type: ignore


# Framework-code patterns — paths matching ANY of these regexes block
# the close. Anything else is treated as agent state churn and ignored.
# This list is the SINGLE SOURCE OF TRUTH; the CLI wrapper imports
# evaluate() from this module and does not duplicate the patterns.
# DO NOT copy this list elsewhere — extend HERE and callers see it.
FRAMEWORK_INCLUDE_PATTERNS = [
    re.compile(r"^core/scripts/.+\.(py|sh)$"),
    re.compile(r"^core/config/.+\.(yaml|md)$"),
    re.compile(r"^\.claude/skills/[^/]+/.+\.(py|sh|md)$"),
    re.compile(r"^\.claude/rules/.+\.md$"),
    re.compile(r"^CLAUDE\.md$"),
]


def _is_framework_code(path: str) -> bool:
    norm = path.replace("\\", "/")
    return any(p.match(norm) for p in FRAMEWORK_INCLUDE_PATTERNS)


def _parse_porcelain_line(line: str) -> Optional[str]:
    """Extract the path from a `git status --porcelain` line.

    Format: 2 status chars + space + path. For renames (R), the format
    is 'R  old -> new' — we want the new path. Returns None on malformed.
    """
    if not line or len(line) < 4:
        return None
    rest = line[3:]
    if " -> " in rest:
        rest = rest.split(" -> ", 1)[1]
    return rest.strip().strip('"')


def get_dirty_framework_files(repo_path: Path) -> List[str]:
    """Return sorted/deduped list of dirty framework paths.

    Fail-open: subprocess errors → empty list (gate doesn't block when
    it can't probe).
    """
    try:
        # CRITICAL — --untracked-files=all preserves per-file rows for new
        # framework directories. See the legacy file's comment for why this
        # flag is load-bearing.
        result = subprocess.run(
            ["git", "-C", str(repo_path), "status", "--porcelain",
             "--untracked-files=all"],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        print(f"[uncommitted-gate] git status failed: {exc} — fail-open",
              file=sys.stderr)
        return []
    if result.returncode != 0:
        print(f"[uncommitted-gate] git status rc={result.returncode}: "
              f"{result.stderr.strip()} — fail-open", file=sys.stderr)
        return []
    dirty = []
    for line in result.stdout.splitlines():
        path = _parse_porcelain_line(line)
        if path and _is_framework_code(path):
            dirty.append(path)
    return sorted(set(dirty))


def get_undelivered_framework_files(repo_path: Path) -> List[str]:
    """Framework paths committed locally but NOT yet on HEAD's upstream.

    This is the DELIVERY half of "commit and push after every framework
    change". `get_dirty_framework_files` above enforces only the commit half,
    which is why the gate's own rationale block (the 2026-05-07 orphan-code
    audit: goals "status=completed ... but never shipped to git") stayed only
    half-enforced for ~3 months. A commit that is never pushed is still never
    shipped to git from every OTHER box's point of view, and it renders locally
    as an unremarkable `ahead N` — so `git status` cannot surface it and a clean
    tree passed the gate. Live instance: g-306-261 closed with its fix stranded
    on one box for ~15h after a non-fast-forward push rejection (rb-6868).

    Three-dot `@{u}...HEAD` (merge-base), not two-dot: when upstream has moved
    ahead — which is the normal state on a live fleet, and precisely the state
    a rejected push leaves behind — two-dot would also report the PARTNER's
    incoming files as though this box owed them.

    STALENESS IS DELIBERATELY NOT CORRECTED, and the direction is what makes
    that safe. `@{u}` is a local tracking ref, only as fresh as the last fetch
    (rb-4716). A stale ref can only make already-pushed commits look
    undelivered — it OVER-reports, never under-reports, so the error lands on
    the blocking side and is visible. Fetching here instead would put a network
    round-trip in the path of every goal close.

    Fail-open: any git error → []. Notably `@{u}` exits non-zero when the
    branch has no upstream (a fresh local branch, detached HEAD), which is not
    evidence of undelivered work.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "diff", "--name-only", "@{u}...HEAD"],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        print(f"[uncommitted-gate] git diff vs upstream failed: {exc} — fail-open",
              file=sys.stderr)
        return []
    if result.returncode != 0:
        # No upstream / detached HEAD / not a repo. Not evidence of a problem.
        return []
    undelivered = [p for p in (ln.strip() for ln in result.stdout.splitlines())
                   if p and _is_framework_code(p)]
    return sorted(set(undelivered))


def _delivery_repo_roots(world_dir: Optional[Path]) -> List[Path]:
    """Repos (beyond the framework repo) whose work must REACH THE DEFAULT
    BRANCH before a goal may close — the cross-repo half of the 2026-05-07
    orphan-code rationale that scope-v1 deliberately deferred.

    The list is DOMAIN STATE, so it lives in the world, not here:
    `<world_dir>/delivery-repos.yaml`, shape `roots: ["/abs/path-or-glob", ...]`
    (globs expand; non-repos and non-dirs are silently dropped). No file, or no
    world_dir, means no cross-repo scanning — the gate stays exactly as
    portable as before for domains that never declare delivery repos.

    Parsed with a deliberately narrow hand parser (a `roots:` list of quoted
    scalars) rather than importing yaml: this module is imported by daemon
    endpoints, and a parse failure of an OPTIONAL enrichment file must degrade
    to "feature off", never to an ImportError in the write path.
    """
    if world_dir is None:
        return []
    manifest = world_dir / "delivery-repos.yaml"
    try:
        if not manifest.is_file():
            return []
        raw = manifest.read_text(encoding="utf-8")
    except OSError:
        return []
    entries: List[str] = []
    in_roots = False
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("roots:"):
            in_roots = True
            continue
        if in_roots:
            if stripped.startswith("- "):
                entries.append(stripped[2:].strip().strip('"').strip("'"))
            else:
                in_roots = False
    roots: List[Path] = []
    import glob as _glob
    for e in entries:
        for hit in sorted(_glob.glob(e)):
            hp = Path(hit)
            if hp.is_dir() and (hp / ".git").exists():
                roots.append(hp)
    return roots


def _repo_default_ref(repo: Path) -> Optional[str]:
    """origin's default branch ref, e.g. 'refs/remotes/origin/master'."""
    r = subprocess.run(
        ["git", "-C", str(repo), "symbolic-ref", "refs/remotes/origin/HEAD"],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    # No origin/HEAD symref (common on plain clones that never ran
    # `remote set-head`): fall back to whichever conventional default exists.
    for cand in ("refs/remotes/origin/master", "refs/remotes/origin/main"):
        rc = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify",
                             "--quiet", cand], capture_output=True, text=True,
                            timeout=10)
        if rc.returncode == 0:
            return cand
    return None


def _refspec_covers_all_heads(repo: Path) -> bool:
    """Can this clone's refs/remotes/* actually SEE every remote branch?

    The local-only test below infers "no remote has this commit" from
    `rev-list --branches --not --remotes`, which walks the LOCAL
    refs/remotes/* cache. That inference is only sound when the fetch
    refspec populates that cache for every branch. On a single-branch
    clone (`remote.origin.fetch = +refs/heads/main:refs/remotes/origin/main`)
    it does not, and NO fetch can repair it -- fetch obeys the same refspec --
    so every commit on every non-default branch reads local-only forever.

    Measured 2026-08-29 (bravo, cc-05, g-115-3258): Vinheim-Web-App held 3
    local remote-tracking refs against 53 real branches on origin; a commit
    that had been on origin for days blocked EVERY goal close fleet-wide for
    ~3 days. guard-5538.

    This is the same mechanism guard-1250 already measured one namespace over
    -- there the default `+refs/heads/*:refs/remotes/origin/*` refspec does not
    cover refs/workers/*, so that namespace reads as a structural ZERO. Both
    are "the refspec does not reach it, so the local cache is not evidence
    about the remote"; guard-1250 assumes refs/heads IS covered, and this is
    the case where that assumption is false.

    Fail-CLOSED to True (assume coverage) on any git error: a probe failure
    must not silently disable the local-only block that g-115-6784/6785 exist
    to enforce. The unsound direction is only entered on a POSITIVE reading of
    a restricted refspec.
    """
    r = subprocess.run(
        ["git", "-C", str(repo), "config", "--get-all", "remote.origin.fetch"],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return True
    # A wildcard mapping of refs/heads/* is what populates refs/remotes/* for
    # every branch. Anything narrower (a single explicit branch) does not.
    return any("refs/heads/*" in ln for ln in r.stdout.splitlines())


def get_stranded_repos(roots: List[Path], fresh_hours: int = 48,
                       goal_id: str = "") -> List[dict]:
    """The 'built but never connected' probe ( /  class).

    For each delivery repo, two ways work can be DONE-looking without having
    shipped, both measured live twice in one six-hour window before this
    existed:

      dirty_tracked   — modified tracked files sitting in the working tree.
        Untracked files are deliberately ignored here (unlike the framework
        scan): product repos carry build output (.next/, dist/, venvs) whose
        presence is noise, and a NEW product file that matters is always
        accompanied by a tracked-file edit wiring it in.

      stranded_commits — the BLOCKING set. Commits younger than `fresh_hours`
        that are off origin's default branch AND are this close's own problem,
        which is two disjoint cases:

          (a) LOCAL-ONLY — reachable from a local branch but contained by NO
              remote ref (`--branches --not --remotes`). Only THIS box can
              deliver them, so they block regardless of which goal made them.
              Congruent with completed-not-committed-sweep's per-sha test
              (`git branch -r --contains <sha>` empty ⇒ local-only), so the
              two agree on one question instead of asking two.

          (b) PUSHED-BUT-UNMERGED AND ATTRIBUTED — on a side branch with a PR
              nobody merged, AND naming `goal_id` in the commit message. This
              is the g-115-6784 / g-115-6785 incident shape, preserved intact:
              a goal that closes with ITS OWN work stranded still refuses.

        Pushed-but-unmerged commits belonging to OTHER goals are reported in
        `unattributed_unmerged` (visibility without a veto). That split is the
        g-115-6851 fix and it exists because the un-attributed predicate had
        NO discriminating power left: measured 2026-08-20 (alpha worker, cc-08),
        10 of 10 delivery repos blocked, on 26 commits every one of which was a
        teammate's open PR from 4-34h earlier (g-363-09, g-326-422, g-350-194,
        g-335-1305 ...) on a fleet whose normal workflow IS branch+PR. A gate
        that refuses every close fleet-wide is not a gate — it trains an
        override reflex, which is a deleted gate with extra friction
        (guard-2273: when the filtered count equals the population, the
        predicate excluded nothing).

        Age-bounded so one crusty historical branch does not veto every close
        forever; older strandings are reported in `stale_stranded_commits`
        (visibility without a veto).

        NOT the `ahead`-count this goal originally prescribed. Measured the
        same run: `rev-list --count <default_ref>..HEAD` reads 1 on four of the
        ten repos whose HEAD sits on a pushed feature branch, so it would have
        kept false-blocking exactly the repos it was meant to release, while
        also dropping case (b) entirely.

    STALENESS HANDLING is the inverse of get_undelivered_framework_files: a
    stale origin/<default> UNDER-delivers (a PR merged remotely five minutes
    ago is not yet in the local ref), which would FALSE-BLOCK precisely the
    most diligent merge-then-close flow. So on a would-block hit — and only
    then — the ref is refreshed with one targeted fetch and the repo
    re-checked. The network round-trip is confined to the failing path.

    Fail-open per repo: any git error drops that repo from the result with a
    stderr note. A gate that cannot probe must not veto.
    """
    findings: List[dict] = []
    for repo in roots:
        try:
            finding = _check_one_repo(repo, fresh_hours, goal_id)
            if finding is not None and (finding["dirty_tracked"]
                                        or finding["stranded_commits"]):
                # One retry after a targeted default-branch refresh, so a
                # remotely-merged PR does not read as stranded (see docstring).
                subprocess.run(
                    # --prune covers guard-4463: a DELETED remote branch
                    # leaves a stale refs/remotes/* ref alive, and an unpruned
                    # fetch keeps the orphaned tip readable, so squash-merged
                    # work reads as stranded. Refs-only, safe to run anytime.
                    ["git", "-C", str(repo), "fetch", "origin",
                     "--quiet", "--no-tags", "--prune"],
                    capture_output=True, text=True, timeout=30,
                )
                finding = _check_one_repo(repo, fresh_hours, goal_id)
            if finding is not None and (finding["dirty_tracked"]
                                        or finding["stranded_commits"]
                                        or finding["stale_stranded_commits"]
                                        or finding["unattributed_unmerged"]):
                findings.append(finding)
        except (subprocess.TimeoutExpired, OSError) as exc:
            print(f"[uncommitted-gate] delivery-repo probe failed for "
                  f"{repo}: {exc} — fail-open for this repo", file=sys.stderr)
    return findings


def _check_one_repo(repo: Path, fresh_hours: int,
                    goal_id: str = "") -> Optional[dict]:
    default_ref = _repo_default_ref(repo)
    if default_ref is None:
        print(f"[uncommitted-gate] {repo}: no origin default ref — skipping",
              file=sys.stderr)
        return None

    st = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain",
         "--untracked-files=no"],
        capture_output=True, text=True, timeout=15,
    )
    if st.returncode != 0:
        return None
    dirty = sorted({p for p in (_parse_porcelain_line(l)
                                for l in st.stdout.splitlines()) if p})

    def _rev_list(args: List[str]) -> List[str]:
        r = subprocess.run(
            ["git", "-C", str(repo), "rev-list", *args],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            return []
        return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]

    # OFF-DEFAULT: reachable from any local or origin ref, not from the default
    # branch. The population both cases below are drawn from.
    off_default = ["--branches", "--remotes=origin", "--not", default_ref]
    # LOCAL-ONLY: contained by no remote ref at all. `--remotes` (no `=origin`)
    # walks refs/remotes/* entirely, matching the sibling sweep's `branch -r`.
    local_only_args = ["--branches", "--not", "--remotes"]
    since = [f"--since={fresh_hours}.hours.ago"]

    fresh_off_default = _rev_list(off_default + since)
    # The local-only inference is only sound when this clone actually fetches
    # every remote branch into refs/remotes/* (guard-5538). When it does not,
    # DROP that half rather than veto on it: the gate's own contract is "a gate
    # that cannot probe must not veto". Attributed commits (this goal's own
    # work, the /6785 shape) still block, and dirty_tracked still
    # blocks -- only the unsound half is dropped.
    refspec_complete = _refspec_covers_all_heads(repo)
    if refspec_complete:
        fresh_local_only = set(_rev_list(local_only_args + since))
    else:
        fresh_local_only = set()
        print(f"[uncommitted-gate] {repo}: remote.origin.fetch does not cover "
              f"refs/heads/* (single-branch clone) -- refs/remotes/* cannot see "
              f"non-default branches, so the local-only test is unsound here and "
              f"is SKIPPED. Delivered work on a side branch would otherwise read "
              f"as stranded forever; no fetch can repair it (guard-5538). "
              f"Verify with `git ls-remote origin`, or widen the refspec to "
              f"`+refs/heads/*:refs/remotes/origin/*`.", file=sys.stderr)
    # Attribution: the fleet's commit convention names the goal as the
    # conventional-commit SCOPE (`fix(): ...`). The needle is the
    # PARENTHESIZED form, byte-identical to completed-not-committed-sweep's
    # `resolve_shas_by_goal_id` (needle = f"({goal_id})"), so the gate and the
    # sweep attribute a commit the same way instead of two ways. A bare
    # substring would also match a prose mention ("supersedes ") and
    # re-introduce a false block — the exact failure being removed here.
    # -F keeps the parens literal rather than a regex group. No goal_id (a
    # bare probe) attributes nothing: the reporting-only direction, never a
    # wider block.
    fresh_attributed = set(
        _rev_list(off_default + since + ["-F", f"--grep=({goal_id})"])
    ) if goal_id else set()

    blocking = [c for c in fresh_off_default
                if c in fresh_local_only or c in fresh_attributed]
    unattributed = [c for c in fresh_off_default if c not in set(blocking)]
    stale = [c for c in _rev_list(off_default) if c not in set(fresh_off_default)]
    return {
        "repo": str(repo),
        "default_ref": default_ref,
        "dirty_tracked": dirty,
        "stranded_commits": blocking[:20],
        "stale_stranded_commits": stale[:20],
        "unattributed_unmerged": unattributed[:20],
        "refspec_complete": refspec_complete,
    }


def _log_override(world_dir: Path, agent_name: str, goal_id: str,
                  justification: str, dirty_files: List[str]) -> None:
    """Append to <world_dir>/uncommitted-work-overrides.jsonl.

    Fail-open: errors print to stderr; the gate never blocks on
    audit-log infrastructure problems.
    """
    ledger = world_dir / "uncommitted-work-overrides.jsonl"
    record = {
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "agent": agent_name or "unknown",
        "goal_id": goal_id,
        "justification": justification,
        "dirty_files": dirty_files,
    }
    try:
        locked_append_jsonl(str(ledger), record)
    except Exception as exc:
        print(f"[uncommitted-gate] override-log write failed: {exc}",
              file=sys.stderr)


def evaluate(*, goal_id: str, override: Optional[str], repo_path: Path,
             world_dir: Optional[Path], agent_name: str = "",
             body_role: Optional[str] = None) -> dict:
    """Run the gate. Pure function for the decision; explicit side effect on override.

    Args:
        goal_id: ID of the goal being closed (audit-log correlation).
        override: justification string, or None for no override. Empty /
            whitespace-only strings are coerced to None with a stderr warn
            — matches the legacy CLI's behavior (an empty audit-log entry
            would defeat the audit's purpose).
        repo_path: Path to scan with `git status --porcelain`. Usually
            the framework repo.
        world_dir: WORLD_DIR for the override audit log. None disables
            the audit-log write (with a stderr warning).
        agent_name: MIND_AGENT value for the audit-log "agent" field.
            Empty string is recorded as "unknown" to match legacy.

    Returns:
        Dict with keys would_block, dirty_framework_files, repo_path,
        goal_id, override_applied. Byte-for-byte JSON-equivalent to the
        legacy CLI's payload.
    """
    # Empty/whitespace-only override → treat as no override + warn. This
    # mirrors the legacy CLI's `main()` block at L226-235; the rationale
    # there is that an empty-justification audit entry would defeat the
    # audit's purpose, and exiting non-zero would silently fail-open in
    # the subprocess caller (which fail-opens on non-JSON exits).
    effective_override = override
    if effective_override is not None and effective_override.strip() == "":
        print("[uncommitted-gate] WARN: --override is empty/whitespace-only "
              "— treating as no override. Provide a real justification or "
              "commit the dirty files.", file=sys.stderr)
        effective_override = None

    dirty = get_dirty_framework_files(repo_path)

    # DELIVERY half (). Enforced ONLY for the role contractually
    # responsible for pushing. A worker Body commits locally and deliberately
    # does NOT push — that is the  contract, not an oversight — so
    # blocking a worker here would refuse every legitimate worker close in the
    # fleet. Its undelivered files are still REPORTED, because the reducer that
    # consumes its carrier ref needs to know what is outstanding.
    #
    # An UNKNOWN role does not block either. BODY_ROLE is injected by the
    # PreToolUse bash hook, so it is reliably present on the CLI path and may be
    # absent on the daemon path; treating "absent" as "reducer" would make the
    # gate's behaviour depend on which of the two call paths a caller happened
    # to take. A false block stalls the loop for everyone, while the failure
    # this gate catches is already visible in the payload — so unknown reports
    # and warns rather than refusing.
    undelivered = get_undelivered_framework_files(repo_path)
    role = (body_role or "").strip().lower() or None
    delivery_blocks = bool(undelivered) and role == "reducer"
    if undelivered and role != "reducer":
        print(f"[uncommitted-gate] NOTE: {len(undelivered)} framework file(s) "
              f"committed but NOT pushed to upstream (body_role="
              f"{role or 'unknown'}; not blocking): {', '.join(undelivered[:5])}"
              + (" ..." if len(undelivered) > 5 else ""), file=sys.stderr)

    # CROSS-REPO delivery half ( / : two goals in one
    # six-hour window closed 'completed' with their product-repo commits
    # stranded on unmerged branches — built, reported done, never connected).
    # Scanned only when the domain declares delivery repos; blocks REGARDLESS
    # of body role: the worker commit-not-push contract () covers the
    # framework repo's carrier-ref flow, while product repos are push+PR+merge
    # for every role. Overridable like everything else here — a goal that
    # legitimately closes with a PR still open passes
    # --override-uncommitted "PR #N open", which puts the pointer on the audit
    # ledger instead of leaving the stranding silent.
    stranded = get_stranded_repos(_delivery_repo_roots(world_dir),
                                  goal_id=goal_id or "")
    stranded_blocks = any(f["dirty_tracked"] or f["stranded_commits"]
                          for f in stranded)
    for f in stranded:
        if f["stale_stranded_commits"] and not (f["dirty_tracked"]
                                                or f["stranded_commits"]):
            print(f"[uncommitted-gate] NOTE: {f['repo']} carries "
                  f"{len(f['stale_stranded_commits'])} stranded commit(s) "
                  f"older than the freshness window (reporting, not blocking)",
                  file=sys.stderr)
        # Released by the  attribution split. Reported every run so
        # the release is VISIBLE: a stranding that stops blocking silently is
        # indistinguishable from a gate someone deleted.
        if f["unattributed_unmerged"]:
            print(f"[uncommitted-gate] NOTE: {f['repo']} carries "
                  f"{len(f['unattributed_unmerged'])} fresh commit(s) pushed to "
                  f"an unmerged branch by OTHER goals (not {goal_id or 'this goal'}"
                  f") — reporting, not blocking", file=sys.stderr)

    would_block = (bool(dirty) or delivery_blocks or stranded_blocks) \
        and effective_override is None

    if effective_override is not None and (dirty or undelivered or stranded):
        if world_dir is None:
            print("[uncommitted-gate] WARN: no WORLD_DIR — skipping override log",
                  file=sys.stderr)
        else:
            _log_override(world_dir, agent_name, goal_id, effective_override,
                          sorted(set(dirty) | set(undelivered)))

    # The three pre-existing keys keep their exact names and semantics — the
    # daemon endpoints and aspirations.py read them positionally by name, and
    # `dirty_framework_files` must NOT start carrying undelivered paths (they
    # are a different failure with a different remedy: push, not commit).
    return {
        "would_block": would_block,
        "dirty_framework_files": dirty,
        "repo_path": str(repo_path),
        "goal_id": goal_id,
        "override_applied": effective_override,
        "undelivered_framework_files": undelivered,
        "delivery_would_block": delivery_blocks,
        "body_role": role,
        "stranded_repos": stranded,
        "stranded_would_block": stranded_blocks,
    }
