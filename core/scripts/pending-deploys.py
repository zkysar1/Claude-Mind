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


def cmd_add(args):
    path = _store_path(args)
    if path is None:
        return 0  # fail-open: no agent resolvable -> no-op
    repo, sha = (args.repo or ""), (args.sha or "")
    if not repo or not sha:
        return 0  # nothing trackable
    goal_id, d = (args.goal_id or ""), (args.dir or "")
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
    """Run deploy-verify.sh for the entry; clear on ok/no_ci (rc 0), keep otherwise.

    Mirrors deploy-verify.sh exit codes so the SG-b closure gate can branch on
    rc/status without re-parsing: 0 = ok/no_ci (entry cleared here), 1 = failed
    (kept — gate files a HIGH Unblock + marks not-clean), 2 = unverified (kept —
    gate re-probes), 3 = usage error (kept).
    """
    repo, sha, d = (args.repo or ""), (args.sha or ""), (args.dir or "")
    dv = SCRIPT_DIR / "deploy-verify.sh"
    cmd = ["bash", str(dv)]
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
    and NEVER overwrites an unparseable/non-dict handoff (that would lose content).
    """
    entries = _load(_store_path(args))
    hp = _handoff_path(args)
    if not entries or hp is None:
        print(json.dumps({"rolled": 0, "handoff": str(hp) if hp else None}))
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
            # keeps _fileops off the hot add/has-pending paths. (9,
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
