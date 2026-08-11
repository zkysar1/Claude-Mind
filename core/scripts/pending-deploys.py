#!/usr/bin/env python3
"""pending-deploys.py — session-local tracker for pending deploy-verification obligations.

Part of the pending-deploys hard gate (g-115-2688 / SG-a g-115-2688-a). A
PostToolUse[Bash] hook (deploy-detect-hook.sh) appends an entry on every
successful `git push` in a CI repo while a goal is in-flight; the closure gate
(iteration-close.sh, SG-b) refuses clean-success goal closure while entries
exist for the goal; resolution runs deploy-verify.sh (the canonical guard-119
probe).

Store: agents/<agent>/session/pending-deploys.yaml — a YAML list of entries:
    - {repo, sha, goal_id, dir, ts}
Session-local, per-agent, machine-local (NOT synced — same class as
background-jobs.yaml / pending-agents.yaml: a deploy obligation tracks a push
made from THIS box's session, so it must not fan out cross-box).

Fail-open EVERYWHERE: a read error -> []; a write error is swallowed. A tracker
failure must NEVER block the LLM's command (the hook path) or the loop (the
gate path). Exit codes are meaningful only for `has-pending` (0 = pending
exist, 1 = none) and `resolve` (mirrors deploy-verify.sh); every other
subcommand exits 0.

Subcommands:
    add   --repo R --sha S [--goal-id G] [--dir D]   append (dedup repo+sha+goal_id)
    list  [--goal-id G] [--json]                      list all / for one goal
    has-pending [--goal-id G]                          exit 0 if any pending, 1 if none
    clear --repo R --sha S                             remove entries matching repo+sha
    resolve --repo R --sha S [--dir D]                run deploy-verify.sh; on ok/no_ci
                                                      (exit 0) clear the entry; on
                                                      failed(1)/unverified(2) keep it.
                                                      Prints the verdict JSON; SG-b's
                                                      gate reads rc/status and acts.

Agent resolution: --agent, else $MIND_AGENT, else --store (explicit path, for tests).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


try:
    import yaml
except ImportError:  # pragma: no cover — yaml is present in this repo
    yaml = None


def gh_bin():
    """The `gh` executable. Production default is bare "gh" — unchanged.

    GH_BIN overrides when SET, *even to the empty string*, matching the
    PROMOTE_GH_BIN seam (promote-to-upstream.sh) and VAULT_SSH_BIN
    (provision-from-vault.sh). Empty is a deliberate, useful value here: both
    callers below have a "gh unusable -> keep the entry" fail-safe, and setting
    GH_BIN="" is how a test exercises it.

    WHY A SEAM AND NOT PATH INTERCEPTION (g-115-3169): a PATH-prepended fake
    `gh` is an extensionless shell script. Bash executes it fine, so the
    bash-level gh calls in deploy-verify.sh hit the stub — but Windows
    CreateProcess CANNOT execute an extensionless script and, when resolving a
    BARE name, appends only ".exe" (it never consults PATHEXT — that is a
    cmd.exe concept). So subprocess.run(["gh", ...]) from Python silently fell
    through the stub to the REAL gh and made LIVE GitHub API CALLS from the
    hermetic test fixture. A real 404 was then read as a "ghost workflow"
    (definitively non-push), which produced no_ci and CLEARED entries the test
    expected to be reported failed. Measured, not inferred: the stub returned
    '{}' while Python received {"message":"Not Found","status":"404"}.

    GH_BIN may name a plain SHELL SCRIPT; run_gh() below handles making that
    executable from Python. Do NOT "solve" it by pointing GH_BIN at a Windows
    .cmd shim — that was tried and MEASURED BROKEN: cmd.exe re-parses the
    command line and cuts the runs query at its `&`.
    """
    return os.environ["GH_BIN"] if "GH_BIN" in os.environ else "gh"


def run_gh(args, timeout=30):
    """subprocess.run([gh_bin(), *args]) that tolerates GH_BIN naming a SHELL
    SCRIPT rather than a native executable.

    On Windows an extensionless shell script is not directly executable —
    CreateProcess raises OSError WinError 193 ("not a valid Win32 application").
    A .cmd shim is NOT a usable substitute: cmd.exe re-parses the command line
    and treats `&` as a command separator, so the runs query
    `actions/runs?head_sha=...&per_page=50` is TRUNCATED at the ampersand
    (measured: the shim received `...head_sha=bbb` and cmd.exe then reported
    "'per_page' is not recognized as an internal or external command").

    So: try direct exec, and on OSError fall back to invoking the script
    through bash, which handles both extensionless scripts and `&` correctly.
    This is not test-only scaffolding — it makes GH_BIN legitimately accept a
    wrapper script on any platform. Production default is bare "gh", which
    takes the direct path on the first try and never reaches the fallback.
    """
    argv = [gh_bin()] + list(args)
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except OSError:
        try:
            from _runtime_bash import BASH  # noqa: WPS433 — local, optional dep
        except Exception:
            raise
        return subprocess.run([BASH, gh_bin()] + list(args),
                              capture_output=True, text=True, timeout=timeout)


def _agent_dir(agent: str):
    """Resolve agents/<agent> via the _paths SSOT, with a layout-fallback."""
    try:
        from _paths import agent_dir  # type: ignore

        return Path(agent_dir(agent))
    except Exception:
        # Fallback matches the current AGENTS_PARENT_DIR=agents layout.
        try:
            root = SCRIPT_DIR.parent.parent  # core/scripts -> core -> PROJECT_ROOT
            return root / "agents" / agent
        except Exception:
            return None


def _store_path(args):
    store = getattr(args, "store", None)
    if store:
        return Path(store)
    agent = getattr(args, "agent", None) or os.environ.get("MIND_AGENT", "")
    if not agent:
        return None
    ad = _agent_dir(agent)
    if ad is None:
        return None
    return ad / "session" / "pending-deploys.yaml"


def _load(path):
    if path is None or not Path(path).is_file():
        return []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception:
        return []
    if not text.strip():
        return []
    try:
        data = yaml.safe_load(text) if yaml is not None else json.loads(text)
    except Exception:
        return []
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def _save(path, entries):
    if path is None:
        return False
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        if yaml is not None:
            text = yaml.safe_dump(entries, sort_keys=False, default_flow_style=False)
        else:
            text = json.dumps(entries)
        Path(path).write_text(text, encoding="utf-8")
        return True
    except Exception:
        return False


def _clear(path, repo, sha):
    """Pure remove-by-(repo,sha). Returns (cleared_count, remaining_count)."""
    entries = _load(path)
    kept = [e for e in entries if not (e.get("repo") == repo and e.get("sha") == sha)]
    if len(kept) != len(entries):
        _save(path, kept)
    return len(entries) - len(kept), len(kept)


def _landed_on_default(repo, sha, timeout=30):
    """Probe whether the sha's work reached the default branch by a path OTHER
    than an ok CI run for the exact sha. Returns (landed: bool, via: str).

    deploy-verify.sh classifies the EXACT sha -- its GitHub Actions runs, and
    (since g-335-848) the hosting-platform build for that same sha where the
    world supplies a platform hook. What it still has no awareness of is "did
    this land on the default branch?". Two poison sub-classes therefore strand
    ledger entries forever (g-115-2876, rb-4616):
      (a) ANCESTOR-LANDED -- the sha is an ancestor of the default branch
          (compare <default>...<sha> ahead_by == 0). CI may have run only on the
          branch HEAD, so deploy-verify returns `unverified` forever, yet the
          commit is already on the default branch (aa9a788 / 4adcc37).
      (b) SUPERSEDED-BY-MERGE -- the sha belongs to a MERGED pull request. A
          PR-branch sha whose CI FAILED, then got fixed + merged + branch-deleted;
          deploy-verify returns `failed` forever, but the work reached the
          default branch via a different (squash/merge) commit (bddb90c / PR#121,
          rb-4612).

    The default branch is resolved DYNAMICALLY (never hardcode 'main') and its
    read doubles as a positive control: an unusable `gh` yields (False, "")
    rather than a spurious clear (rb-4740 -- gate a destructive-on-absence
    conclusion behind a positive control; empty is not evidence).

    Fail-SAFE: any gh error, unreadable value, or not-landed reading returns
    (False, "") so the caller KEEPS the entry. NEVER loosen on ambiguity -- a
    false clear silently drops a genuine deploy obligation.
    """
    if not repo or not sha:
        return (False, "")

    def _gh(api_path, jq):
        try:
            proc = run_gh(["api", api_path, "-q", jq], timeout=timeout)
            if proc.returncode != 0:
                return None
            return (proc.stdout or "").strip()
        except Exception:
            return None

    # Positive control + dynamic default branch.
    default_branch = _gh("repos/%s" % repo, ".default_branch")
    if not default_branch:
        return (False, "")   # gh unusable for this repo -> cannot prove landing

    # (a) ancestor-landed: sha is an ancestor of the default branch (ahead_by 0).
    ahead = _gh("repos/%s/compare/%s...%s" % (repo, default_branch, sha), ".ahead_by")
    if ahead == "0":
        return (True, "ancestor:%s" % default_branch)

    # (b) superseded-by-merge: the sha belongs to a MERGED pull request.
    merged = _gh("repos/%s/commits/%s/pulls" % (repo, sha),
                 "[.[] | select(.merged_at != null)] | length")
    if merged and merged.isdigit() and int(merged) > 0:
        return (True, "merged-pr")

    return (False, "")


def _sha_absent_from_origin(repo, sha, timeout=30):
    """Return True ONLY when we can CONFIRM the sha is absent from origin — a
    REBASE ORPHAN: a locally-committed sha superseded by `git pull --rebase`
    that never reached origin (g-115-2925 / rb-4737; observed a8431e7 rebased
    into fc67bd5). deploy-verify.sh re-probes such a sha as `unverified` forever
    because no CI ever ran for a sha the remote never saw, so the entry strands
    and re-flags every future close.

    Positive control (same discipline as _landed_on_default): resolve the
    default branch FIRST; an unusable gh yields False -> KEEP (cannot prove
    absence, rb-4740 "empty is not evidence"). With gh proven usable, look the
    sha up on the remote: `gh api repos/<repo>/commits/<sha>` returns the commit
    when it exists on origin (reachable from ANY ref) and errors (404/422
    "No commit found for SHA") when the sha was never pushed. Because the
    positive control already proved gh works, a not-found here is a GENUINE
    absent-sha, not a gh failure.

    Fail-SAFE: any gh error on the positive control, or a found/ambiguous sha,
    returns False so the caller KEEPS the entry — never retire a genuine deploy
    obligation on a guess (a rebased-away sha is the ONLY thing this retires).
    """
    if not repo or not sha:
        return False

    def _gh(api_path, jq):
        try:
            proc = run_gh(["api", api_path, "-q", jq], timeout=timeout)
            if proc.returncode != 0:
                return None
            return (proc.stdout or "").strip()
        except Exception:
            return None

    if not _gh("repos/%s" % repo, ".default_branch"):
        return False   # gh unusable for this repo -> cannot prove absence
    # gh proven usable. Look the sha up on origin.
    if _gh("repos/%s/commits/%s" % (repo, sha), ".sha") is not None:
        return False   # commit present on origin -> keep the obligation
    # Commit-lookup returned nothing. RE-CONFIRM the positive control to rule out
    # a TRANSIENT error (rate-limit 403 / timeout) that hit only the commit-lookup:
    # _gh collapses every non-zero exit to None, so a 404 (genuine absence) and a
    # 403/timeout (transient) are indistinguishable from the return value alone —
    # and by this point gh has already served several calls (deploy-verify +
    # landed-detection), so a rate-limit on the next call is realistic. A gh that
    # can STILL read the default branch AND cannot find the sha proves GENUINE
    # absence; a gh that now fails too means the not-found was transient -> return
    # False (KEEP, fail-safe). (fresh-eyes  Finding 1.)
    return _gh("repos/%s" % repo, ".default_branch") is not None


def _push_branch_deploys(git_dir, timeout=10):
    """Return True if this commit's branch is the repo's DEPLOY branch (or if we
    cannot tell — fail-OPEN toward registering). Return False ONLY when we can
    CONFIDENTLY prove the current branch is NOT the deploy branch.

    The deploy branch is the repo's default branch (the standard GitHub Actions
    push-deploy trigger), resolved LOCALLY from origin/HEAD (no network). A
    commit on any OTHER branch (a docs / side branch) does not trigger the
    deploy workflow, so deploy-verify returns `unverified` forever and the entry
    never clears (g-115-2925 / rb-4737; observed 50fc8d1 on docs/operator-
    hardening). Skipping registration for such commits prevents the phantom
    entry at the source. (An explicit per-repo multi-branch allowlist would plug
    in here if a repo ever deploys from a non-default branch — none does today,
    so default-branch equality is the domain-agnostic instantiation; YAGNI.)

    Fail-OPEN: no dir, not a git repo, detached HEAD, origin/HEAD unset, or any
    git error -> True (register), so the gate never silently loosens a deploy
    obligation on ambiguity. Only a clean current!=default comparison returns
    False.
    """
    if not git_dir:
        return True

    def _git(*a):
        try:
            p = subprocess.run(["git", "-C", str(git_dir), *a],
                               capture_output=True, text=True, timeout=timeout)
            return (p.stdout or "").strip() if p.returncode == 0 else None
        except Exception:
            return None

    cur = _git("rev-parse", "--abbrev-ref", "HEAD")
    if not cur or cur == "HEAD":   # error or detached HEAD -> cannot tell
        return True
    dref = _git("rev-parse", "--abbrev-ref", "origin/HEAD")  # e.g. "origin/main"
    if not dref or "/" not in dref:
        return True   # default branch unresolvable -> fail-open register
    return cur == dref.split("/", 1)[1]


def cmd_add(args):
    path = _store_path(args)
    if path is None:
        return 0  # fail-open: no agent resolvable -> no-op
    repo, sha = (args.repo or ""), (args.sha or "")
    if not repo or not sha:
        return 0  # nothing trackable
    goal_id, d = (args.goal_id or ""), (args.dir or "")
    #  / rb-4737: skip commits on a non-deploying (non-default) branch —
    # they never trigger the deploy workflow, so deploy-verify returns `unverified`
    # forever and the entry never clears. Fail-open on any ambiguity (register).
    if not _push_branch_deploys(d):
        return 0  # non-deploying branch -> no deploy obligation to track
    entries = _load(path)
    for e in entries:  # dedup: a re-push of the same sha for the same goal is one obligation
        if e.get("repo") == repo and e.get("sha") == sha and e.get("goal_id") == goal_id:
            return 0
    entries.append({"repo": repo, "sha": sha, "goal_id": goal_id, "dir": d,
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})
    _save(path, entries)
    return 0


def cmd_list(args):
    entries = _load(_store_path(args))
    if getattr(args, "goal_id", None):
        entries = [e for e in entries if e.get("goal_id") == args.goal_id]
    if getattr(args, "json", False):
        print(json.dumps(entries))
    else:
        for e in entries:
            print(f"{e.get('repo','')}@{str(e.get('sha',''))[:7]} "
                  f"goal={e.get('goal_id','')} dir={e.get('dir','')} ts={e.get('ts','')}")
    return 0


def cmd_has_pending(args):
    entries = _load(_store_path(args))
    if getattr(args, "goal_id", None):
        entries = [e for e in entries if e.get("goal_id") == args.goal_id]
    return 0 if entries else 1


def cmd_clear(args):
    cleared, remaining = _clear(_store_path(args), args.repo or "", args.sha or "")
    print(json.dumps({"cleared": cleared, "remaining": remaining}))
    return 0


def cmd_resolve(args):
    """Run deploy-verify.sh for the entry; clear on ok/no_ci (rc 0). On a
    failed/unverified verdict, run landed-detection (_landed_on_default) before
    keeping: if the sha's work reached the default branch by another path
    (ancestor or merged PR) the entry is cleared (rc 0) with a landed_via note.

    Mirrors deploy-verify.sh exit codes so the SG-b closure gate can branch on
    rc/status without re-parsing: 0 = ok/no_ci OR landed-elsewhere (entry cleared
    here), 1 = failed AND not-landed (kept — gate files a HIGH Unblock + marks
    not-clean), 2 = unverified AND not-landed (kept — gate re-probes), 3 = usage
    error (kept).
    """
    repo, sha, d = (args.repo or ""), (args.sha or ""), (args.dir or "")
    dv = SCRIPT_DIR / "deploy-verify.sh"
    from _runtime_bash import bash_cmd  # guard-580 (bin-first, clean-PATH-safe) + guard-581
    cmd = bash_cmd(dv)
    if d:
        cmd += ["--dir", d]
    if repo:
        cmd += ["--repo", repo]
    if sha:
        cmd += ["--sha", sha]
    if getattr(args, "timeout_mins", None):
        cmd += ["--timeout-mins", str(args.timeout_mins)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=getattr(args, "subprocess_timeout", None))
        rc, out = proc.returncode, (proc.stdout or "").strip()
    except Exception as e:  # cannot run verifier -> unverified, keep the entry
        print(json.dumps({"status": "unverified", "cleared": False,
                          "detail": f"deploy-verify invocation error: {e}"}))
        return 2
    try:
        status = json.loads(out).get("status", "unverified") if out else "unverified"
    except Exception:
        status = "unverified"
    if rc == 0:  # ok or no_ci -> obligation satisfied
        cleared, _ = _clear(_store_path(args), repo, sha)
        print(json.dumps({"status": status, "cleared": True, "cleared_count": cleared, "detail": out}))
        return 0
    # deploy-verify could not confirm THIS sha's CI (failed / unverified). Before
    # KEEPING the entry, ask the orthogonal question deploy-verify never asks:
    # did the work LAND on the default branch by another path (ancestor / merged
    # PR)? That clears the two landed-but-unclearable poison sub-classes
    # (, rb-4612 / rb-4616) that would otherwise strand the entry
    # forever and re-flag every future close. Fail-SAFE: not-landed / any
    # ambiguity KEEPS the entry (never loosen a deploy obligation on a guess).
    landed, via = _landed_on_default(repo, sha)
    if landed:
        cleared, _ = _clear(_store_path(args), repo, sha)
        print(json.dumps({"status": status, "cleared": True, "cleared_count": cleared,
                          "landed_via": via, "detail": out}))
        return 0
    #  / rb-4737: REBASE ORPHAN — the sha never reached origin at all
    # (superseded by `git pull --rebase` before it was pushed). No CI can ever run
    # for a sha the remote never saw, so deploy-verify would re-probe it as
    # `unverified` forever and strand the entry. Retire it — positive-control
    # gated + fail-safe (only a CONFIRMED-absent sha is retired; any ambiguity
    # keeps the entry, same discipline as the landed check).
    if _sha_absent_from_origin(repo, sha):
        cleared, _ = _clear(_store_path(args), repo, sha)
        print(json.dumps({"status": status, "cleared": True, "cleared_count": cleared,
                          "retired_via": "rebased-away", "detail": out}))
        return 0
    print(json.dumps({"status": status, "cleared": False, "rc": rc, "detail": out}))
    return rc


def _handoff_path(args):
    """Resolve the agent-wide handoff.yaml (agents/<agent>/session/handoff.yaml),
    with a --handoff test override. handoff.yaml survives across sessions and is
    read by the next session's boot, so rolling unresolved deploys here surfaces
    them in the cross-session summary. Returns None if unresolvable (fail-open)."""
    ho = getattr(args, "handoff", None)
    if ho:
        return Path(ho)
    agent = getattr(args, "agent", None) or os.environ.get("MIND_AGENT", "")
    if not agent:
        return None
    ad = _agent_dir(agent)
    if ad is None:
        return None
    return ad / "session" / "handoff.yaml"


def cmd_roll_handoff(args):
    """Copy unresolved deploy obligations into handoff.yaml for cross-session
    visibility at graceful stop (SG-c, g-115-2688-c).

    Does NOT clear pending-deploys.yaml. That store lives in the AGENT-WIDE
    session dir (agents/<agent>/session/) and persists across sessions, so the
    next session's SG-b all-sweep keeps re-probing the entries — it stays the
    single source of truth. handoff.pending_deploys is a VISIBILITY MIRROR: it
    surfaces the carry-over in the boot summary so the agent is aware unverified
    deploys crossed the stop boundary. Merge is dedup-by-(repo,sha) and preserves
    every other handoff key. Fail-open: always prints a summary and returns 0,
    and NEVER overwrites a handoff it cannot first read whole — unparseable,
    non-dict, or (g-115-5199) unmaterializable from the read-through cache.
    That third case is the one this preserves-every-key claim used to be FALSE
    for: a cold own-cloud box read the remote file as absent and wrote a
    document containing only pending_deploys.
    """
    entries = _load(_store_path(args))
    hp = _handoff_path(args)
    if not entries or hp is None:
        print(json.dumps({"rolled": 0, "handoff": str(hp) if hp else None}))
        return 0
    # --- COLD-CACHE MATERIALIZE BEFORE THE PRESENCE TEST () --------
    # Under own-cloud the local tree is a READ-THROUGH CACHE (guard-980), so a
    # handoff.yaml that exists REMOTELY reads as absent on a cold box. A bare
    # Path(hp).is_file() then falls through with doc={}, and the locked write
    # below persists a document containing ONLY pending_deploys — destroying
    # every other key the remote file carried. handoff.yaml is cross-session
    # state read at the next boot, so those keys are not recoverable from the
    # store that produced them.
    #
    # Note what the surrounding code already defends, because the asymmetry is
    # the whole bug: the non-dict branch and the unparseable branch BOTH return
    # without writing. Those are the RARE loss paths. The cold-cache path is the
    # COMMON one and was the only one that fell through to the write.
    #
    # refresh(), NOT ensure_local() — the pre-apply consult (guard-980) is what
    # caught this, and the two are NOT interchangeable here. ensure_local is
    # _refresh(force_fresh=False), and that TTL early-return is gated on
    # `not force_fresh` (owncloud_backend L618-621), so it can return a
    # PRESENT-BUT-STALE local file without ever contacting S3 — which drops a
    # PEER's key by the same whole-file rewrite, just from a warm cache instead
    # of a cold one. refresh() is force_fresh=True, and its abstract docstring
    # names this exact caller: "call this before a raw in-lock read, so a
    # read-modify-write starts from the latest remote state. For a remote-only
    # file it materializes the local cache." handoff.yaml has NO merge handler
    # (coordination_merge.merge_handler_for -> None), i.e. it is a fence-only
    # store with no reconciler below the write, so the current-state read is the
    # only thing standing between a rewrite and a lost update.
    #
    # The 404 contract is what makes this a clean fix rather than a guess
    # (owncloud_backend._refresh L624-628, and it holds for BOTH force_fresh
    # values): a 404/NoSuchKey RETURNS normally, leaving local absent — so a
    # genuinely-absent handoff still takes the empty-doc path and a fresh
    # agent's first stop is unaffected. Any OTHER ClientError RAISES. So a
    # normal return makes the local answer authoritative; a raise means we
    # cannot distinguish absent-remotely from cannot-reach, and the only safe
    # move is to skip.
    #
    # RESIDUAL, named rather than silently closed: the read below and the write
    # at the end are not held under ONE lock, so a peer writing between them is
    # still a lost update. That window pre-dates this fix and closing it means
    # moving the whole body to locked_rmw — filed separately rather than
    # inlined here.
    #
    # SKIPPING IS CHEAP AND LOSS IS NOT — that asymmetry picks the direction.
    # This store is a VISIBILITY MIRROR (see the docstring): pending-deploys.yaml
    # is NOT cleared here and remains the single source of truth, so a skipped
    # roll costs one boot summary and self-heals on the next stop. A dropped key
    # is gone. Identity on LocalBackend, so this is a no-op off own-cloud.
    # Lazy import mirrors the _fileops import below — keeps the backend off the
    # hot add/has-pending paths.
    try:
        from storage_backend import get_backend  # type: ignore
        get_backend().refresh(hp)
    except Exception:
        print(json.dumps({"rolled": 0, "error": "handoff-unmaterializable"}))
        return 0

    # Read existing handoff, preserving all keys. On any parse ambiguity, SKIP
    # the write rather than clobber a file we cannot safely round-trip.
    doc = {}
    try:
        if Path(hp).is_file():
            txt = Path(hp).read_text(encoding="utf-8")
            if txt.strip():
                loaded = yaml.safe_load(txt) if yaml is not None else json.loads(txt)
                if not isinstance(loaded, dict):
                    print(json.dumps({"rolled": 0, "error": "handoff-not-dict"}))
                    return 0
                doc = loaded
    except Exception:
        print(json.dumps({"rolled": 0, "error": "handoff-unparseable"}))
        return 0
    existing = doc.get("pending_deploys")
    existing = existing if isinstance(existing, list) else []
    seen = {(e.get("repo"), e.get("sha")) for e in existing if isinstance(e, dict)}
    rolled_now = time.strftime("%Y-%m-%dT%H:%M:%S")
    added = 0
    for e in entries:
        key = (e.get("repo"), e.get("sha"))
        if key in seen:
            continue
        seen.add(key)
        existing.append({"repo": e.get("repo", ""), "sha": e.get("sha", ""),
                         "goal_id": e.get("goal_id", ""), "dir": e.get("dir", ""),
                         "rolled_at": rolled_now})
        added += 1
    doc["pending_deploys"] = existing
    try:
        Path(hp).parent.mkdir(parents=True, exist_ok=True)
        if yaml is not None:
            # Canonical locked writer — the SAME primitive the primary handoff
            # writer uses (handoff-yaml-build.py:186): gives a .history snapshot,
            # changelog audit, atomic rename, and surrogate validation that raw
            # write_text skips. handoff.yaml is agent-private and written
            # sequentially in the graceful-stop D4->stop-hook flow, so
            # locked_write_yaml — not the shared-file RMW locked_modify_yaml,
            # which its docstring reserves for multi-writer files. Lazy import
            # keeps _fileops off the hot add/has-pending paths. (,
            # fresh-eyes SG-c Finding 2.)
            from _fileops import locked_write_yaml  # type: ignore
            locked_write_yaml(hp, doc)
        else:
            Path(hp).write_text(json.dumps(doc), encoding="utf-8")
    except Exception as ex:
        print(json.dumps({"rolled": 0, "error": f"handoff-write: {ex}"}))
        return 0
    print(json.dumps({"rolled": added, "total_in_handoff": len(existing),
                      "handoff": str(hp)}))
    return 0


def _build_parser():
    p = argparse.ArgumentParser(description="Session-local pending-deploys tracker.")
    p.add_argument("--agent")
    p.add_argument("--store", help="Explicit store path (overrides --agent; for tests).")
    sub = p.add_subparsers(dest="subcommand", required=True)

    a = sub.add_parser("add")
    a.add_argument("--repo", required=True)
    a.add_argument("--sha", required=True)
    a.add_argument("--goal-id", dest="goal_id", default="")
    a.add_argument("--dir", default="")
    a.set_defaults(func=cmd_add)

    li = sub.add_parser("list")
    li.add_argument("--goal-id", dest="goal_id", default="")
    li.add_argument("--json", action="store_true")
    li.set_defaults(func=cmd_list)

    hp = sub.add_parser("has-pending")
    hp.add_argument("--goal-id", dest="goal_id", default="")
    hp.set_defaults(func=cmd_has_pending)

    c = sub.add_parser("clear")
    c.add_argument("--repo", required=True)
    c.add_argument("--sha", required=True)
    c.set_defaults(func=cmd_clear)

    r = sub.add_parser("resolve")
    r.add_argument("--repo", required=True)
    r.add_argument("--sha", required=True)
    r.add_argument("--dir", default="")
    r.add_argument("--timeout-mins", dest="timeout_mins", default=None)
    r.add_argument("--subprocess-timeout", dest="subprocess_timeout", type=float, default=None)
    r.set_defaults(func=cmd_resolve)

    rh = sub.add_parser("roll-handoff")
    rh.add_argument("--handoff",
                    help="Explicit handoff.yaml path (overrides --agent; for tests).")
    rh.set_defaults(func=cmd_roll_handoff)
    return p


def main(argv=None):
    args = _build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Exception:
        # Absolute fail-open backstop: never raise out of the tracker.
        return 0


if __name__ == "__main__":
    sys.exit(main())
