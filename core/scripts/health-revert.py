#!/usr/bin/env python3
"""health-revert.py — tiered, calibration-gated self-health revert (Phase 3).

Spec: core/config/conventions/health-ledger.md §10 (calibration), §11 (tiered
revert by constitutional ring). Design: workflow wf_b82a26e9-958.

This is the "Run" phase: when the detection sweep (Phase 2) attributes a health
regression to a recent file change, this module decides — by the candidate's
constitutional ring and revert-eligibility — whether to AUTO-revert (Ring 3 +
high confidence), file an AGENT-gated Unblock (Ring 1.5/2), file a USER Unblock
(Ring 1), or skip (Ring 0 / not eligible). A revert is FILE-GRANULAR (restore
one file to its pre-regression content via `git show`, never `git revert` of a
whole commit), committed with a `Health-Revert` git trailer so attribution can
never re-blame it (feedback-loop break). Every auto/agent revert is recorded as
PENDING and verified `verification_iterations` later: if the composite improved
the revert is KEPT; otherwise it is UNDONE and the (file, signal) pair is
written to the per-agent dead-end registry so it is never tried again.

SAFETY: the whole pipeline is gated by `route_candidate` on
(mode == "full") AND (calibration AND-gate satisfied). Until BOTH hold, every
candidate routes to "not-eligible" and nothing is touched. Ring 0 is skipped
unconditionally. Fail-loud on git errors (a half-applied revert must be visible).

CLI:
  python3 health-revert.py route  --verdict <json> [--dry-run] [--json]
  python3 health-revert.py verify [--dry-run] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import _health_ledger as hl  # noqa: E402

_PENDING_FILE = "health-pending-reverts.yaml"
_DEAD_END_FILE = "health-dead-ends.yaml"
_TRAILER = "Health-Revert"


def _eprint(msg):
    print(f"[health-revert] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Routing — pure decision (independently unit-tested).
# ---------------------------------------------------------------------------
def route_candidate(candidate, *, mode, calibrated):
    """Decide what to do with one attribution candidate. Returns one of:
    'not-eligible' (mode != full OR not calibrated — the master safety gate),
    'skip-ring0'   (Ring 0 anchor — never touch; should already be filtered),
    'auto-revert'  (Ring 3 AND authority == 'auto'),
    'agent-unblock'(Ring 1.5 / 2 / unknown / Ring 3 demoted to agent),
    'user-unblock' (Ring 1 — user owns intent)."""
    if not (mode == "full" and calibrated):
        return "not-eligible"
    ring = candidate.get("ring")
    authority = candidate.get("authority")
    if ring == "0":
        return "skip-ring0"
    if ring == "1" or authority == "user":
        return "user-unblock"
    if ring == "3" and authority == "auto":
        return "auto-revert"
    return "agent-unblock"   # 1.5, 2, unknown, or Ring 3 demoted below the gate


# ---------------------------------------------------------------------------
# Git file-granular revert mechanics (fail-loud).
# ---------------------------------------------------------------------------
def _git(args, repo_root, *, check=True, capture=True):
    r = subprocess.run(["git", "-C", str(repo_root), *args],
                       capture_output=capture, text=True,
                       encoding="utf-8", errors="replace", timeout=30)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} -> rc={r.returncode}: {(r.stderr or '').strip()[:200]}")
    return r


def _file_existed_at(commit, path, repo_root):
    """True iff `path` existed in the tree at `commit` (rc 0 from cat-file)."""
    r = _git(["cat-file", "-e", f"{commit}:{path}"], repo_root, check=False)
    return r.returncode == 0


def _content_at(commit, path, repo_root):
    """Bytes of `path` at `commit` (text), or None if it did not exist there."""
    if not _file_existed_at(commit, path, repo_root):
        return None
    return _git(["show", f"{commit}:{path}"], repo_root).stdout


def revert_file(path, bad_commit, repo_root, signal, *, dry_run=False,
                now_iso=None, undo=False):
    """Restore `path` to its content at the PARENT of `bad_commit` (undo=False)
    or re-apply the bad-commit version (undo=True), then commit with a
    Health-Revert trailer. File-granular — never `git revert` a whole commit.

    A file CREATED in bad_commit reverts to deletion (its parent had no such
    file). Returns a result dict {ok, action, path, restored_from, commit, msg}.
    Fail-loud: git errors set ok=False with the message; the caller surfaces it."""
    repo_root = Path(repo_root)
    parent = f"{bad_commit}~1"
    # Source commit for the desired content:
    #   normal revert -> parent of bad_commit (the pre-regression content)
    #   undo          -> bad_commit itself (re-apply what we had reverted)
    src = bad_commit if undo else parent
    direction = "undo" if undo else "revert"
    try:
        target = (repo_root / path)
        # Guard: src must resolve to a real commit. When bad_commit is the repo
        # ROOT, `bad_commit~1` is invalid; without this check _content_at would
        # return None and the revert would WRONGLY DELETE the file instead of
        # aborting. (Near-impossible in practice — attribution targets recent
        # commits, never the root — but a wrong delete is severe, so guard it.)
        if _git(["rev-parse", "--verify", "--quiet", f"{src}^{{commit}}"],
                repo_root, check=False).returncode != 0:
            return {"ok": False, "action": direction, "path": path,
                    "msg": f"source ref {src} does not resolve — aborting (no wrong-delete)"}
        desired = _content_at(src, path, repo_root)
        if dry_run:
            return {"ok": True, "action": f"{direction}-dryrun", "path": path,
                    "restored_from": src,
                    "msg": f"would restore {path} to {src}"
                           + (" (delete — absent at source)" if desired is None else "")}
        if desired is None:
            # The file did not exist at the source commit → restore = delete it.
            if target.exists():
                _git(["rm", "--quiet", "--", path], repo_root)
            else:
                return {"ok": True, "action": f"{direction}-noop", "path": path,
                        "restored_from": src, "msg": "already absent"}
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(desired, encoding="utf-8")
            _git(["add", "--", path], repo_root)

        now_iso = now_iso or datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        msg = (f"fix(health-{direction}): restore {path} to {src[:12]}\n\n"
               f"Automated self-health {direction} (health-ledger Phase 3): the "
               f"'{signal}' signal regressed and was attributed to {bad_commit[:12]}. "
               f"Restoring the pre-regression content of this one file.\n\n"
               f"{_TRAILER}: {signal} {bad_commit}\n"
               f"Health-Revert-At: {now_iso}\n")
        # --no-verify: the Health-Revert is a mechanical restore of already-reviewed
        # content; pre-commit framework gates ran when it was first authored.
        # Pathspec `-- path` makes this a PARTIAL commit of ONLY this file — it
        # never sweeps in other changes the concurrent autonomous loop may have
        # staged in the shared index. (Critical-bug fix: a bare `git commit`
        # captures the ENTIRE index, so an auto-revert would have bundled
        # unrelated loop-staged files into the Health-Revert commit.)
        r = _git(["commit", "--no-verify", "-m", msg, "--", path], repo_root, check=False)
        if r.returncode != 0:
            # No net change (content identical / nothing staged) is not a hard failure.
            _blob = (r.stdout + r.stderr).lower()
            if any(s in _blob for s in ("nothing to commit", "no changes", "did not match")):
                return {"ok": True, "action": f"{direction}-noop", "path": path,
                        "restored_from": src, "msg": "no change to commit"}
            # Commit failed (most likely index.lock contention with the concurrent
            # autonomous loop). Restore the file to HEAD so we leave NO staged or
            # working-tree residue: otherwise the loop's next commit would land our
            # revert content WITHOUT the Health-Revert trailer, defeating the
            # feedback-loop filter and re-blaming a phantom change. Best-effort.
            _git(["checkout", "HEAD", "--", path], repo_root, check=False)
            return {"ok": False, "action": direction, "path": path,
                    "msg": f"commit failed (residue restored to HEAD): {(r.stderr or r.stdout).strip()[:200]}"}
        sha = _git(["rev-parse", "HEAD"], repo_root).stdout.strip()
        return {"ok": True, "action": direction, "path": path, "restored_from": src,
                "commit": sha, "msg": f"{direction} committed {sha[:12]}"}
    except Exception as e:  # noqa: BLE001 — fail-loud (surface to caller)
        _eprint(f"{direction} of {path} failed: {e}")
        return {"ok": False, "action": direction, "path": path, "msg": str(e)}


# ---------------------------------------------------------------------------
# Pending-reverts tracker + dead-end registry (per-agent YAML).
# ---------------------------------------------------------------------------
def _load_yaml(path):
    try:
        import yaml
        if not Path(path).exists():
            return {}
        return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001 — tolerant
        _eprint(f"yaml read failed ({path}): {e}")
        return {}


def _dump_yaml(path, data):
    import yaml
    Path(path).write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
                          encoding="utf-8")


def load_pending(agent_dir):
    return list((_load_yaml(Path(agent_dir) / _PENDING_FILE)).get("pending") or [])


def append_pending(agent_dir, entry):
    p = Path(agent_dir) / _PENDING_FILE
    data = _load_yaml(p)
    data.setdefault("pending", []).append(entry)
    _dump_yaml(p, data)


def remove_pending(agent_dir, key):
    p = Path(agent_dir) / _PENDING_FILE
    data = _load_yaml(p)
    data["pending"] = [e for e in (data.get("pending") or []) if e.get("key") != key]
    _dump_yaml(p, data)


def append_dead_end(agent_dir, file, signal, *, now_iso, expiry_days, direction="revert"):
    p = Path(agent_dir) / _DEAD_END_FILE
    data = _load_yaml(p)
    exp = (datetime.strptime(now_iso, "%Y-%m-%dT%H:%M:%S")
           + timedelta(days=int(expiry_days))).strftime("%Y-%m-%dT%H:%M:%S")
    data.setdefault("dead_ends", []).append({
        "file": file, "signal": signal, "direction": direction,
        "recorded_at": now_iso, "expires_at": exp,
    })
    _dump_yaml(p, data)


# ---------------------------------------------------------------------------
# Verification: keep-or-undo pending reverts whose window has elapsed.
# ---------------------------------------------------------------------------
def verify_pending(agent_dir, *, current_composite, current_iteration, repo_root,
                   improve_delta, verification_iterations, expiry_days,
                   chain_depth_cap=1, now_iso=None, dry_run=False):
    """For each pending revert whose verification window (verification_iterations)
    has elapsed: KEEP it if the composite improved by >= improve_delta since the
    revert; otherwise UNDO it (re-apply the bad-commit content) and record a
    dead end so the pair is never retried. Returns a list of outcome dicts.

    chain_depth_cap bounds revert<->undo oscillation: an undo is itself NOT
    re-tracked as pending (depth 1), so the verifier cannot ping-pong."""
    now_iso = now_iso or datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    outcomes = []
    for e in load_pending(agent_dir):
        try:
            elapsed = current_iteration - int(e.get("revert_iteration"))
        except (TypeError, ValueError):
            # Malformed/missing revert_iteration: cannot know if the window
            # elapsed → treat as NOT elapsed so a corrupt entry can NEVER
            # trigger a premature undo (was: "evaluate now", the wrong default).
            elapsed = -1
        if elapsed < verification_iterations:
            outcomes.append({"key": e.get("key"), "decision": "pending",
                             "elapsed": elapsed})
            continue
        baseline = e.get("composite_at_revert")
        improved = (isinstance(current_composite, (int, float))
                    and isinstance(baseline, (int, float))
                    and (current_composite - baseline) >= improve_delta)
        if improved:
            if not dry_run:
                remove_pending(agent_dir, e.get("key"))
            outcomes.append({"key": e.get("key"), "decision": "keep",
                             "delta": (round(current_composite - baseline, 4)
                                       if isinstance(baseline, (int, float)) else None)})
        else:
            # UNDO: re-apply the bad-commit content, dead-end the pair, untrack.
            res = {"ok": True, "action": "undo-dryrun"}
            if not dry_run:
                res = revert_file(e.get("file"), e.get("bad_commit"), repo_root,
                                  e.get("signal"), now_iso=now_iso, undo=True)
                append_dead_end(agent_dir, e.get("file"), e.get("signal"),
                                now_iso=now_iso, expiry_days=expiry_days,
                                direction="revert")
                remove_pending(agent_dir, e.get("key"))
            outcomes.append({"key": e.get("key"), "decision": "undo",
                             "file": e.get("file"), "signal": e.get("signal"),
                             "undo_ok": res.get("ok"), "msg": res.get("msg")})
    return outcomes


def _verdict_to_action(verdict, *, agent_dir, repo_root, calibrated, now_iso, dry_run):
    """Execute the routing decision for a detection verdict's TOP candidate.
    Returns an action result dict (also describes any Unblock spec to file)."""
    mode = verdict.get("mode")
    candidates = verdict.get("candidates") or []
    if not candidates:
        return {"acted": False, "reason": "no candidates"}
    top = candidates[0]
    decision = route_candidate(top, mode=mode, calibrated=calibrated)
    signal = verdict.get("signal")
    result = {"acted": False, "decision": decision, "candidate": top, "signal": signal}

    if decision in ("not-eligible", "skip-ring0"):
        result["reason"] = decision
        return result

    if decision == "user-unblock":
        result["unblock"] = {
            "participants": ["user"],
            "title": f"Unblock: review Ring-1 health-regression revert ({top['path']})",
            "notify": True,
            "body": f"Health regression on '{signal}' attributed to {top.get('commit')} "
                    f"({top['path']}, Ring 1 — user owns intent). Proposed: file-granular "
                    f"revert to the pre-regression content. Approve to proceed.",
        }
        result["acted"] = True
        return result

    if decision == "agent-unblock":
        result["unblock"] = {
            "participants": ["agent"],
            "title": f"Unblock: agent-gated health-regression revert ({top['path']})",
            "body": f"Health regression on '{signal}' attributed to {top.get('commit')} "
                    f"({top['path']}, ring {top.get('ring')}). Agent-gated: revert this one "
                    f"file (git show {top.get('commit')}~1:{top['path']}), then verify the "
                    f"composite improves within the verification window or undo + dead-end.",
        }
        result["acted"] = True
        return result

    # auto-revert (Ring 3, authority == auto)
    rev = revert_file(top["path"], top.get("commit"), repo_root, signal,
                      dry_run=dry_run, now_iso=now_iso)
    result["revert"] = rev
    result["acted"] = bool(rev.get("ok"))
    if rev.get("ok") and not dry_run and rev.get("action") == "revert":
        append_pending(agent_dir, {
            "key": verdict.get("dedup_key") or f"{signal}:{top['path']}",
            "file": top["path"], "signal": signal,
            "bad_commit": top.get("commit"),
            "revert_commit": rev.get("commit"),
            "revert_iteration": verdict.get("iteration"),
            "composite_at_revert": verdict.get("composite"),
            "recorded_at": now_iso,
        })
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _load_revert_cfg(config_dir):
    cfg = {"mode": "collect-only",
           "calibration": {"min_days": 30, "min_records": 50},
           "revert": {"confidence_gate": 0.70, "verification_iterations": 5,
                      "improve_delta": 0.05, "dead_end_expiry_days": 30,
                      "chain_depth_cap": 1}}
    try:
        import yaml
        with open(Path(config_dir) / "aspirations.yaml", encoding="utf-8") as f:
            hr = (yaml.safe_load(f) or {}).get("health_regression") or {}
        if hr.get("mode"):
            cfg["mode"] = hr["mode"]
        for sub in ("calibration", "revert"):
            if isinstance(hr.get(sub), dict):
                cfg[sub].update(hr[sub])
    except Exception as e:  # noqa: BLE001 — tolerant defaults
        _eprint(f"config read failed ({e}) — defaults")
    return cfg


def main():
    ap = argparse.ArgumentParser(description="Tiered, calibration-gated self-health revert (Phase 3).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("route", help="route + act on a detection verdict")
    rp.add_argument("--verdict", required=True, help="verdict JSON (string) from health-regression-check")
    rp.add_argument("--dry-run", action="store_true")
    rp.add_argument("--json", action="store_true")
    vp = sub.add_parser("verify", help="keep-or-undo pending reverts whose window elapsed")
    vp.add_argument("--dry-run", action="store_true")
    vp.add_argument("--json", action="store_true")
    args = ap.parse_args()

    agent = os.environ.get("MIND_AGENT", "").strip()
    if not agent:
        print(json.dumps({"error": "no agent"}))
        return 0
    try:
        import _paths
        config_dir = _paths.CONFIG_DIR
        agent_dir = _paths.agent_dir(agent)
        repo_root = _paths.PROJECT_ROOT
    except Exception as e:  # noqa: BLE001 — fail-open
        print(json.dumps({"error": str(e)}))
        return 0

    cfg = _load_revert_cfg(config_dir)
    # `records` is the recent WINDOW, consumed only by the verify path below
    # (its latest non-warmup record). It must NOT gate calibration: a bounded
    # window under-counts calendar days on a busy agent, which is exactly the
    # form spec §10 forbids and the  defect. Calibration routes
    # through the shared full-history helper so this half cannot drift from
    # health-regression-check.py's again.
    records = hl.recent_records(Path(agent_dir) / "health", 200)
    calibrated, _ = hl.calibration_state(
        Path(agent_dir) / "health",
        cfg["calibration"]["min_days"], cfg["calibration"]["min_records"])
    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    if args.cmd == "route":
        try:
            verdict = json.loads(args.verdict)
        except json.JSONDecodeError as e:
            print(json.dumps({"error": f"bad verdict json: {e}"}))
            return 0
        out = _verdict_to_action(verdict, agent_dir=agent_dir, repo_root=repo_root,
                                 calibrated=calibrated, now_iso=now_iso,
                                 dry_run=args.dry_run)
        print(json.dumps(out, ensure_ascii=True, indent=2 if args.json else None))
        return 0

    # verify
    latest = hl.latest_nonwarmup(records)
    rv = cfg["revert"]
    outcomes = verify_pending(
        agent_dir,
        current_composite=(latest or {}).get("composite"),
        current_iteration=(latest or {}).get("iteration", 0),
        repo_root=repo_root,
        improve_delta=rv["improve_delta"],
        verification_iterations=rv["verification_iterations"],
        expiry_days=rv["dead_end_expiry_days"],
        chain_depth_cap=rv["chain_depth_cap"],
        now_iso=now_iso, dry_run=args.dry_run)
    print(json.dumps({"calibrated": calibrated, "outcomes": outcomes},
                     ensure_ascii=True, indent=2 if args.json else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
