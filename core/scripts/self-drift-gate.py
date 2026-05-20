#!/usr/bin/env python3
"""Self-Drift Gate — file an Unblock goal when observed work drifts from the
agent's declared work_class targets.

Tranche C.5 (rb-390, Gap #1 follow-through). class_balance_bonus (scorer
criterion 7e) pulls under-represented classes UP at selection time. This
gate is the escalation path when the scorer's nudge is insufficient —
if a class stays below a critical fraction of its target for several
iterations, file an Unblock goal that forces the agent to either
(a) correct the work mix or (b) update the targets to match observed
reality. Either way, close the drift.

Reads:
  - core/config/aspirations.yaml → class_balance.targets (authoritative
    target distribution; empty map = natural gate, script no-ops)
  - core/config/aspirations.yaml → self_drift_gate.{window_size,
    critical_ratio, min_iterations_before_fire, cooldown_hours,
    target_aspiration_id} (empty target_aspiration_id = natural gate)
  - <agent>/session/working-memory.yaml → goals_completed_this_session
    (same data class_balance_bonus reads — shared signal lane)
  - <agent>/session/self-drift-log.jsonl → cooldown tracking

Writes (only when a class has drifted AND cooldown has elapsed):
  - <agent>/aspirations.jsonl → one Unblock goal per drifted class (via
    aspirations-add-goal.sh)
  - world/board/coordination.jsonl → status post announcing the escalation
    (via board-post.sh)
  - <agent>/session/self-drift-log.jsonl → audit entry for cooldown

Domain-agnostic: the gate operates ONLY on work_class names listed in
class_balance.targets. It does not hardcode any specific class name,
so a fresh world with its own class taxonomy works identically.

Exit codes:
  0 = completed (0 or N goals filed)
  1 = framework error (unreadable config, missing scripts)
  2 = dry-run with would-fire items (only when --dry-run supplied)
"""

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import _rt  # canonical Python -> daemon client (post-cutover; see _rt.py)

import yaml  # Required — tree.py already depends on PyYAML

from _fileops import locked_append_jsonl  # noqa: E402
from _paths import agent_dir as _resolve_agent_dir  # noqa: E402


def _read_local_paths_conf(agent_name):
    conf = _resolve_agent_dir(agent_name) / "local-paths.conf"
    out = {}
    if not conf.is_file():
        return out
    for line in conf.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _agent_name():
    return os.environ.get("MIND_AGENT", "").strip() or None


def _agent_dir(agent_name):
    return _resolve_agent_dir(agent_name) if agent_name else None


def _world_dir(agent_name):
    conf = _read_local_paths_conf(agent_name) if agent_name else {}
    if conf.get("WORLD_PATH"):
        return Path(conf["WORLD_PATH"])
    return None


def _load_config():
    """Read class_balance.targets + self_drift_gate from aspirations.yaml
    with the standard _config_overlay merge path."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_config_overlay", SCRIPT_DIR / "_config_overlay.py"
        )
        overlay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(overlay)
        asp = overlay.merged_config("aspirations.yaml")
    except Exception as e:
        return None, None, f"config load failed: {e}"

    class_balance = asp.get("class_balance", {}) or {}
    targets = class_balance.get("targets", {}) or {}

    defaults = {
        "window_size": 30,
        "critical_ratio": 0.50,          # observed < (target * ratio) → drifted
        "min_iterations_before_fire": 20,
        "cooldown_hours": 48,
        "target_aspiration_id": "",      # REQUIRED to activate: domain sets the
                                         # aspiration under which Unblock goals
                                         # get filed. Empty = natural gate, gate
                                         # no-ops. No default that would silently
                                         # route to the wrong place (SSOT, rb-395).
    }
    gate = asp.get("self_drift_gate", {}) or {}
    for k, d in defaults.items():
        v = gate.get(k)
        if v is not None:
            defaults[k] = type(d)(v)

    return targets, defaults, None


def _read_session_completions(agent_dir):
    wm_path = agent_dir / "session" / "working-memory.yaml"
    if not wm_path.is_file():
        return []
    try:
        with open(wm_path, encoding="utf-8") as f:
            wm = yaml.safe_load(f) or {}
    except Exception:
        return []
    sc = wm.get("goals_completed_this_session", [])
    return sc if isinstance(sc, list) else []


def _read_last_fire(agent_dir, work_class):
    """Return the ISO timestamp of the last self-drift-gate fire for this
    work_class, or None if never fired."""
    log_path = agent_dir / "session" / "self-drift-log.jsonl"
    if not log_path.is_file():
        return None
    last = None
    try:
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            # Only SUCCESSFUL fires should trigger cooldown. A failed fire
            # (subprocess error, schema rejection) has produced no goal, so
            # the drift is still un-addressed — suppressing retries on
            # failed fires lets bugs hide for cooldown_hours (48h default).
            # Check result.filed == True; fall back to "looks like success"
            # (no error key) for legacy entries written before this field.
            if entry.get("work_class") != work_class:
                continue
            if not entry.get("fired_at"):
                continue
            result = entry.get("result") or {}
            if result.get("filed") is False:
                continue  # failed fire — do not count toward cooldown
            ts = entry["fired_at"]
            if last is None or ts > last:
                last = ts
    except Exception:
        return None
    return last


def _cooldown_elapsed(last_fire_iso, cooldown_hours, now):
    if not last_fire_iso:
        return True
    try:
        last = dt.datetime.fromisoformat(last_fire_iso)
    except Exception:
        return True
    elapsed_hours = (now - last).total_seconds() / 3600.0
    return elapsed_hours >= cooldown_hours


def _compute_drift(session_completions, targets, window_size, critical_ratio):
    """Return a list of {work_class, observed_fraction, target_fraction,
    deficit} for classes whose observed fraction is below critical_ratio *
    target. Returns empty list if insufficient data."""
    recent = session_completions[-window_size:]
    classed = [s.get("work_class") for s in recent if s.get("work_class")]
    if not classed:
        return []
    total = len(classed)
    drifted = []
    for wc, target_fraction in targets.items():
        target_fraction = float(target_fraction)
        if target_fraction <= 0:
            continue
        count = sum(1 for c in classed if c == wc)
        observed = count / total
        critical = target_fraction * critical_ratio
        if observed < critical:
            drifted.append({
                "work_class": wc,
                "observed_fraction": round(observed, 3),
                "target_fraction": round(target_fraction, 3),
                "critical_fraction": round(critical, 3),
                "deficit": round(target_fraction - observed, 3),
                "observed_count": count,
                "window_total": total,
            })
    drifted.sort(key=lambda d: d["deficit"], reverse=True)
    return drifted


def _detect_aspiration_source(aspiration_id: str) -> Optional[str]:
    """Probe world then agent queue for the aspiration_id.

    Returns "world", "agent", or None. Used by _file_unblock_goal to route
    drift goals to the queue where the configured target_aspiration_id
    actually lives — the gate's target may be a shared aspiration (world)
    or an agent-local one (agent). Fail-open: read errors skip the queue,
    never crash the gate.
    """
    def _queue_contains(jsonl_path: Path) -> bool:
        if not jsonl_path.is_file():
            return False
        try:
            for line in jsonl_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("id") == aspiration_id and d.get("status") == "active":
                    return True
        except Exception:
            return False
        return False

    # Read external world path from agent's local-paths.conf.
    # agent_dir(<agent>)/local-paths.conf is the canonical location
    # (Phase 2.5.D: agents/<agent>/ via _paths.agent_dir helper);
    # fall back to alpha if MIND_AGENT not resolvable.
    agent = os.environ.get("MIND_AGENT", "")
    paths_file = _resolve_agent_dir(agent) / "local-paths.conf"
    world_dir = None
    if paths_file.is_file():
        for raw in paths_file.read_text(encoding="utf-8").splitlines():
            if raw.startswith("WORLD_PATH="):
                world_dir = raw.split("=", 1)[1].strip()
                break
    if world_dir and _queue_contains(Path(world_dir) / "aspirations.jsonl"):
        return "world"
    if _queue_contains(_resolve_agent_dir(agent) / "aspirations.jsonl"):
        return "agent"
    return None


def _file_unblock_goal(agent_name, aspiration_id, drift_info, dry_run):
    wc = drift_info["work_class"]
    obs_pct = int(drift_info["observed_fraction"] * 100)
    target_pct = int(drift_info["target_fraction"] * 100)
    title = f"Unblock: work_class '{wc}' drifted to {obs_pct}% (target {target_pct}%)"
    description = (
        f"Observed work_class distribution has drifted from declared targets.\n\n"
        f"  work_class: {wc}\n"
        f"  observed:   {obs_pct}% ({drift_info['observed_count']} of "
        f"{drift_info['window_total']} last completions)\n"
        f"  target:     {target_pct}% "
        f"(aspirations.yaml → class_balance.targets.{wc})\n"
        f"  critical:   below {int(drift_info['critical_fraction'] * 100)}% "
        f"(target × self_drift_gate.critical_ratio)\n\n"
        f"Resolve ONE of:\n"
        f"1. Correct the work mix — file or surface goals whose work_class is "
        f"'{wc}' so next iterations pull the fraction back up to target.\n"
        f"2. Retune the target — if the observed fraction reflects the real "
        f"priority now, update class_balance.targets in aspirations.yaml "
        f"(or the agent's Self declaration, if the Self calls out a "
        f"different emphasis than the targets reflect).\n"
        f"3. If the drift is expected short-term (burst of framework work, "
        f"etc.), suppress with a narrative note in working memory — the "
        f"gate respects the self_drift_gate.cooldown_hours cooldown after "
        f"firing.\n\n"
        f"Filed by self-drift-gate.py (Tranche C.5, rb-390)."
    )
    # Schema must match aspirations-add-goal.sh expectations (read by
    # aspirations.py cmd_add_goal from stdin). See core/config/conventions/
    # goal-schemas.md — id is assigned by the script, not by the caller.
    payload = {
        "title": title,
        "description": description,
        "priority": "HIGH",
        "participants": ["agent"],
        "verification": {
            "outcomes": [
                f"work_class '{wc}' observed fraction >= target × "
                f"critical_ratio, OR targets map updated to reflect new "
                f"priority, OR cooldown elapses with explicit accept-drift "
                f"note in working memory."
            ],
        },
        # work_class matches the drifted class so completing this goal
        # contributes to rebalancing (self-referential: the fix participates
        # in the metric it's correcting; cooldown prevents runaway).
        "work_class": wc,
        # origin-signal-gate requires this on every new agent-authored goal
        # (strict mode rejects missing values). Schema: core/scripts/
        # origin-signal-gate.py — drift-detected is a registered
        # framework-level signal for portfolio-steering Unblock goals.
        "origin_signal": f"drift_detected:{wc}",
    }
    if dry_run:
        return {"would_file": True, "title": title, "work_class": wc,
                "aspiration_id": aspiration_id}

    # File via the daemon. The aspirations.py add-goal CLI was deleted in the
    # 2026-05-14 cutover; _rt is the canonical Python -> daemon client. A
    # Python child cannot reach the .sh wrapper on Windows (bash resolves to
    # WSL bash, which cannot open a C:\ path — rb-225/rb-247, reproved
    # 2026-05-15), so HTTP-to-daemon is the only correct path. Daemon-only:
    # no CLI fallback (.claude/rules/no-python-cli-fallback.md).
    #
    # Auto-detect source: aspiration_id may live in the world queue (shared,
    # e.g.  portfolio-steering) OR the agent queue (e.g. ).
    # Hardcoding agent fails when the domain uses a world-queue aspiration.
    source = _detect_aspiration_source(aspiration_id)
    if source is None:
        return {"filed": False,
                "error": f"Aspiration {aspiration_id} not found in world or agent queues",
                "work_class": wc, "aspiration_id": aspiration_id}

    # --override-duplication justification: drift-correction goals are
    # structurally similar to prior drift goals (same work_class, same
    # correction-template language) BY DESIGN — the duplication gate's
    # similarity signal is exactly what the drift gate is supposed to fire
    # on. Cooldown_hours gates runaway firing; the duplication gate's
    # semantic-overlap check is the wrong layer for this pattern.
    override = (
        "Portfolio-drift-correction goal. Similarity to prior drift-correction "
        "goals in the same work_class is structural (same template, same "
        "correction shape) — duplication gate's semantic-overlap signal is the "
        "wrong layer for this pattern. Runaway prevention lives in "
        "self_drift_gate.cooldown_hours (48h default)."
    )
    # _rt reads MIND_AGENT from the env for the X-Mind-Agent header; set it
    # for this call and restore (in-process analog of the old subprocess
    # env={..., MIND_AGENT: agent_name}).
    _prev_agent = os.environ.get("MIND_AGENT")
    os.environ["MIND_AGENT"] = agent_name
    try:
        _rt.aspirations_add_goal(aspiration_id, payload, source=source,
                                 overrides={"Duplication": override})
    except _rt.RtError as e:
        return {
            "filed": False,
            "error": (e.body or str(e)).strip()[:400],
            "work_class": wc,
            "aspiration_id": aspiration_id,
        }
    finally:
        if _prev_agent is None:
            os.environ.pop("MIND_AGENT", None)
        else:
            os.environ["MIND_AGENT"] = _prev_agent
    return {"filed": True, "title": title, "work_class": wc,
            "aspiration_id": aspiration_id}


def _post_board(agent_name, drifted, dry_run):
    if dry_run or not drifted:
        return
    classes = ", ".join(
        f"{d['work_class']}={int(d['observed_fraction'] * 100)}%"
        for d in drifted[:3]
    )
    msg = (
        f"Self-drift gate fired: {len(drifted)} work_class(es) below critical "
        f"threshold [{classes}]. Unblock goal(s) filed."
    )
    script = CORE_ROOT / "scripts" / "board-post.sh"
    try:
        subprocess.run(
            ["bash", script.as_posix(), "--channel", "coordination",
             "--type", "status", "--tags", "self-drift-gate"],
            input=msg,
            capture_output=True,
            text=True,
            timeout=15,
            env={**os.environ, "MIND_AGENT": agent_name},
        )
    except Exception:
        pass  # board is advisory — never abort on its failure


def _log_fire(agent_dir, drift_info, result, now):
    log_path = agent_dir / "session" / "self-drift-log.jsonl"
    entry = {
        "fired_at": now.isoformat(timespec="seconds"),
        "work_class": drift_info["work_class"],
        "observed_fraction": drift_info["observed_fraction"],
        "target_fraction": drift_info["target_fraction"],
        "result": result,
    }
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        locked_append_jsonl(str(log_path), entry)
    except Exception:
        pass  # audit trail is advisory


def _pending_policy_review(agent_dir):
    """: Return pending review metadata if a fresh-eyes-review or
    fresh-eyes-program question is currently unresolved, else None.

    The drift-gate corrects work_class distribution toward class_balance.targets.
    If the USER is mid-review on whether those targets are even correct (via
    fresh-eyes-review or fresh-eyes-program), firing an Unblock goal preempts
    the user decision and produces the retroactive accept-note pattern
    documented in rb-452. Suppressing during review defers the correction
    until the user has answered.

    Natural-gate pattern (rb-395): fail-open on any read error — a broken
    probe MUST NOT block normal drift escalation. Returns None on parse
    error, missing file, or missing PyYAML.

    Currently suppresses on ANY pending fresh-eyes-review / fresh-eyes-program
    (conservative) rather than per-policy asks_about:* tags, because the
    current pending-questions schema does not carry tags. When the schema
    is extended to include asks_about:class_balance_targets etc., the
    filter can be narrowed.
    """
    pq_path = agent_dir / "session" / "pending-questions.yaml"
    if not pq_path.exists():
        return None
    try:
        import yaml  # PyYAML guaranteed by _load_config; ImportError is fail-open
        with open(pq_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or []
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    _REVIEW_TYPES = {"fresh-eyes-review", "fresh-eyes-program"}
    for q in data:
        if not isinstance(q, dict):
            continue
        if q.get("status") == "pending" and q.get("type") in _REVIEW_TYPES:
            return {
                "review_id": q.get("id", ""),
                "review_type": q.get("type", ""),
                "created": q.get("created", ""),
            }
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="detect drift but do not file goals or post to board")
    ap.add_argument("--output", choices=["human", "json"], default="human")
    args = ap.parse_args(argv)

    agent = _agent_name()
    if not agent:
        print("ERROR: MIND_AGENT not set", file=sys.stderr)
        return 1

    agent_dir = _agent_dir(agent)
    targets, cfg, err = _load_config()
    if err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1
    if not targets:
        if args.output == "json":
            print(json.dumps({"status": "no-targets"}))
        else:
            print("self-drift-gate: inactive (class_balance.targets is "
                  "empty — natural gate; domain must set targets to activate)")
        return 0
    target_aspiration_id = cfg["target_aspiration_id"]
    if not target_aspiration_id:
        # No aspiration configured = gate has nowhere to file Unblock goals.
        # Fail-open per SSOT principle: do not silently pick a default
        # aspiration; the domain must opt in explicitly.
        if args.output == "json":
            print(json.dumps({"status": "no-target-aspiration"}))
        else:
            print("self-drift-gate: self_drift_gate.target_aspiration_id "
                  "not set (required — domain must name the aspiration "
                  "under which Unblock goals get filed)")
        return 0

    session_completions = _read_session_completions(agent_dir)
    if len(session_completions) < cfg["min_iterations_before_fire"]:
        if args.output == "json":
            print(json.dumps({
                "status": "insufficient-data",
                "observed": len(session_completions),
                "required": cfg["min_iterations_before_fire"],
            }))
        else:
            print(f"self-drift-gate: insufficient data "
                  f"({len(session_completions)}/"
                  f"{cfg['min_iterations_before_fire']} iterations)")
        return 0

    drifted = _compute_drift(
        session_completions, targets,
        cfg["window_size"], cfg["critical_ratio"],
    )
    now = dt.datetime.now()
    fired_or_would_fire = []
    suppressed = []
    failed = []

    # : probe pending user-facing review ONCE per run (not per-class —
    # the review is agent-wide, not per-work-class). If a fresh-eyes-review or
    # fresh-eyes-program is pending, suppress ALL drift fires until resolved.
    pending_review = _pending_policy_review(agent_dir)

    for drift_info in drifted:
        wc = drift_info["work_class"]
        if pending_review is not None:
            suppressed.append({
                "work_class": wc,
                "reason": "policy_under_user_review",
                "review_id": pending_review["review_id"],
                "review_type": pending_review["review_type"],
                "suppressed_by_pending_review": True,
            })
            continue
        last_fire = _read_last_fire(agent_dir, wc)
        if not _cooldown_elapsed(last_fire, cfg["cooldown_hours"], now):
            suppressed.append({
                "work_class": wc,
                "reason": "cooldown",
                "last_fire": last_fire,
            })
            continue
        result = _file_unblock_goal(
            agent, target_aspiration_id, drift_info, args.dry_run,
        )
        if not args.dry_run:
            _log_fire(agent_dir, drift_info, result, now)
        # A result counts as a "fire" ONLY if the goal was actually filed
        # (or would be, in dry-run). A subprocess failure — duplication gate,
        # missing aspiration, argparse error — returns {"filed": False} and
        # MUST NOT inflate fired_count or trigger the board post that claims
        # "Unblock goal(s) filed". Failures go to failed[] for transparency.
        if result.get("filed") or result.get("would_file"):
            fired_or_would_fire.append({**drift_info, "result": result})
        else:
            failed.append({**drift_info, "result": result})

    if fired_or_would_fire and not args.dry_run:
        _post_board(agent, fired_or_would_fire, args.dry_run)

    summary = {
        "status": "ok" if not args.dry_run else "dry-run",
        "drifted_count": len(drifted),
        "fired_count": len(fired_or_would_fire),
        "suppressed_count": len(suppressed),
        "failed_count": len(failed),
        "fired": fired_or_would_fire,
        "suppressed": suppressed,
        "failed": failed,
    }
    if args.output == "json":
        print(json.dumps(summary, indent=2))
    else:
        if not drifted:
            print("self-drift-gate: no drift detected")
        else:
            print(f"self-drift-gate: {len(drifted)} drifted class(es), "
                  f"{len(fired_or_would_fire)} fired, "
                  f"{len(suppressed)} suppressed (cooldown), "
                  f"{len(failed)} failed")
            for d in fired_or_would_fire:
                print(f"  - {d['work_class']}: "
                      f"{int(d['observed_fraction'] * 100)}% "
                      f"(target {int(d['target_fraction'] * 100)}%)")
            for d in failed:
                err = d.get("result", {}).get("error", "unknown")
                print(f"  ! {d['work_class']} FAILED: {err[:120]}")

    if args.dry_run and fired_or_would_fire:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
