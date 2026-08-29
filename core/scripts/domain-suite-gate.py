#!/usr/bin/env python3
"""domain-suite-gate.py — refuse a status=completed close while the world's
domain test suite is red or uncollectable, IF a domain script changed since
the goal was claimed (g-353-75).

WHY. The domain half of the test population lives at $WORLD_PATH/scripts —
external, gitignored, invisible to full-suite-recommender.sh (it detects
changes through git, so it reports "no code changes" for every domain-script
edit ever made — guard-1947). run-full-suite-after-deep-code.md names the
command for that half, but naming a command is honor-system, and a small-model
Body never runs it. Measured 2026-08-29 on a live deployment: two test modules
imported symbols that later units had removed from the modules they test; the
domain suite ended in `Interrupted: 2 errors during collection`, and every
later goal in that lane "verified" against a suite that could not collect.
guard-399: an instruction the LLM must follow at a step needs the gate that
makes skipping it impossible. This is that gate.

WHAT IT DOES, in order (each step is cheap until the last):
  1. No $WORLD_PATH/scripts, no runner hook AND no test_*.py under it → noop.
     A world without domain tests is a supported configuration, not a breakage
     (same stance as run-full-suite.sh's domain block).
  2. No code file under scripts/ (.py .sh .bash .yaml .yml .toml .cfg .ini)
     modified at or after the goal's claim → noop. The claim time is
     `claimed_at` on the goal record (read through aspirations-read.sh, never
     the store directly); `--since <iso>` overrides it; with neither readable
     the window falls back to the last 6 hours and says so. mtime is the
     honest trigger: Edit-tool writes to world/scripts do not pass through
     _fileops, so the changelog cannot attribute them, and on a shared world
     several Bodies edit the same tree — a red suite blocks all of their
     verification regardless of who broke it, so the gate names the touched
     files and leaves attribution to the reader.
  3. Run the domain suite: the world-provided hook
     $WORLD_PATH/scripts/run-domain-tests.sh when present (Pattern B,
     domain-hooks.md — core names the slot, the world fills it), else
     `python3 -m pytest -q` with cwd=scripts so `from <pkg> import ...`
     resolves the way the rule's own command runs it. STORAGE_BACKEND=local is
     pinned (guard-955). Bounded at 900 s.
  4. rc 0 → pass. rc 5 (pytest: nothing collected) → pass, noted. Anything
     else → BLOCK: exit 1, the touched files and the last lines on stderr.
     `--override "<why>"` turns a block into a pass and appends one row to
     world/domain-suite-overrides.jsonl — the ledger is the audit, and a
     collection error is never a legitimate override (the message says so).

Every branch is reported to _gate_log so the pass/block/override split is
measurable (gate-stats.sh --gate domain-suite-gate); the gate itself fails
OPEN on its own errors (decision `error`, exit 0) — a broken gate must never
wedge a close.

Usage:
    python3 core/scripts/domain-suite-gate.py --goal <id> --source <world|agent>
        [--since <iso>] [--override "<justification>"] [--timeout <s>]

stdout: exactly one JSON line —
    {"gate": "domain-suite-gate", "decision": noop|pass|block|override|error,
     "reason": "...", "runner": "...", "touched": [...], "rc": N, "tail": [...]}
exit:   0 on noop/pass/override/error; 1 on block.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _paths import WORLD_DIR  # noqa: E402
from _gate_log import log as _gate_log  # noqa: E402
from _runtime_bash import bash_cmd  # noqa: E402  guard-580/581: never a bare "bash", never str(Path)

GATE_ID = "domain-suite-gate"  # MUST match the id in core/config/gates.yaml
RUNNER_HOOK = "run-domain-tests.sh"
CODE_SUFFIXES = {".py", ".sh", ".bash", ".yaml", ".yml", ".toml", ".cfg", ".ini"}
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".locks", ".git", "node_modules", ".venv", "venv"}
FALLBACK_WINDOW = timedelta(hours=6)
# One minute of slack under the claim stamp: a Body that edits in the same
# minute it claims must not slip under the window on clock granularity.
SLACK_SECONDS = 60
DEFAULT_TIMEOUT = 900
TAIL_LINES = 25


# ─── discovery ────────────────────────────────────────────────────────────

def _scripts_dir(world_dir: Path | None) -> Path | None:
    if world_dir is None:
        return None
    d = Path(world_dir) / "scripts"
    return d if d.is_dir() else None


def _walk(scripts_dir: Path):
    """Yield files under scripts_dir, pruning the cache/venv dirs."""
    for root, dirs, files in os.walk(scripts_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            yield Path(root) / name


def has_domain_tests(scripts_dir: Path) -> bool:
    if (scripts_dir / RUNNER_HOOK).is_file():
        return True
    return any(p.name.startswith("test_") and p.suffix == ".py" for p in _walk(scripts_dir))


def touched_since(scripts_dir: Path, since: datetime) -> list[tuple[str, str]]:
    """[(relative path, mtime iso)] for code files modified at/after `since`."""
    cutoff = since.timestamp() - SLACK_SECONDS
    out = []
    for p in _walk(scripts_dir):
        if p.suffix not in CODE_SUFFIXES:
            continue
        try:
            mt = p.stat().st_mtime
        except OSError:
            continue
        if mt >= cutoff:
            out.append((p.relative_to(scripts_dir).as_posix(),
                        datetime.fromtimestamp(mt).strftime("%Y-%m-%dT%H:%M:%S")))
    out.sort(key=lambda t: t[1])
    return out


# ─── claim time ───────────────────────────────────────────────────────────

def _parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text).strip()[:19])
    except ValueError:
        return None


def claimed_at(goal_id: str, source: str) -> datetime | None:
    """The goal's claimed_at through aspirations-read.sh (daemon-routed).

    Returns None when the record or the stamp is unreadable — the caller
    falls back to a bounded window and says so; the gate never reads the
    store file directly.
    """
    parts = goal_id.split("-")
    if len(parts) < 3 or parts[0] != "g":
        return None
    asp_id = "asp-" + parts[1]
    try:
        proc = subprocess.run(
            bash_cmd(SCRIPT_DIR / "aspirations-read.sh", "--source", source, "--id", asp_id),
            capture_output=True, text=True, timeout=60, check=False,
        )
        doc = json.loads(proc.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    for g in (doc.get("goals") or []) if isinstance(doc, dict) else []:
        if g.get("id") == goal_id:
            return _parse_iso(g.get("claimed_at"))
    return None


# ─── the run ──────────────────────────────────────────────────────────────

def runner_command(scripts_dir: Path) -> tuple[list[str], str]:
    hook = scripts_dir / RUNNER_HOOK
    if hook.is_file():
        return bash_cmd(hook), "scripts/" + RUNNER_HOOK
    return [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--color=no"], "python -m pytest (cwd=scripts)"


def run_suite(scripts_dir: Path, timeout: int) -> tuple[int | None, list[str]]:
    """(rc, last lines). rc None means the run exceeded `timeout`."""
    cmd, _ = runner_command(scripts_dir)
    env = dict(os.environ)
    env["STORAGE_BACKEND"] = "local"  # guard-955: any test runner, always
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("PYTEST_ADDOPTS", None)
    try:
        proc = subprocess.run(cmd, cwd=str(scripts_dir), env=env, capture_output=True,
                              text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"")
        text = out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)
        return None, text.splitlines()[-TAIL_LINES:]
    text = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    return proc.returncode, [ln for ln in text.splitlines() if ln.strip()][-TAIL_LINES:]


# ─── ledger + telemetry ───────────────────────────────────────────────────

def _log_override(world_dir: Path, payload: dict) -> None:
    ledger = Path(world_dir) / "domain-suite-overrides.jsonl"
    try:
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with open(ledger, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"domain-suite-gate: override ledger write failed: {e}", file=sys.stderr)


def _emit(decision: str, goal_id: str, override: str | None, **fields) -> dict:
    doc = {"gate": GATE_ID, "decision": decision, "goal_id": goal_id}
    doc.update(fields)
    try:
        _gate_log(GATE_ID, decision, caller="iteration-close.sh do_verify",
                  trigger_matched=bool(fields.get("touched")),
                  payload={"goal_id": goal_id, "runner": fields.get("runner"),
                           "rc": fields.get("rc"), "touched": len(fields.get("touched") or [])},
                  override_reason=override if decision == "override" else None)
    except Exception:  # noqa: BLE001 — telemetry must never break the gate
        pass
    print(json.dumps(doc, ensure_ascii=False))
    return doc


# ─── main ─────────────────────────────────────────────────────────────────

def evaluate(goal_id: str, source: str, since: datetime | None, override: str | None,
             timeout: int, world_dir: Path | None) -> int:
    scripts_dir = _scripts_dir(world_dir)
    if scripts_dir is None or not has_domain_tests(scripts_dir):
        _emit("noop", goal_id, override, reason="no domain test suite under world scripts")
        return 0

    since_note = ""
    if since is None:
        since = claimed_at(goal_id, source)
        if since is None:
            since = datetime.now() - FALLBACK_WINDOW
            since_note = " (claimed_at unreadable; used the last 6 hours)"
    touched = touched_since(scripts_dir, since)
    if not touched:
        _emit("noop", goal_id, override, reason="no domain script modified since "
              + since.strftime("%Y-%m-%dT%H:%M:%S") + since_note)
        return 0

    _, runner_label = runner_command(scripts_dir)
    rc, tail = run_suite(scripts_dir, timeout)
    if rc in (0, 5):
        note = "" if rc == 0 else " (pytest collected no tests)"
        _emit("pass", goal_id, override, reason="domain suite green" + note, runner=runner_label,
              rc=rc, touched=touched)
        return 0

    why = (f"domain suite exceeded {timeout}s" if rc is None
           else "domain suite could not COLLECT (rc=2: an import or syntax error in a test module)"
           if rc == 2 else f"domain suite RED (rc={rc})")
    if override:
        _log_override(world_dir, {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "goal_id": goal_id, "agent": os.environ.get("MIND_AGENT", "unknown"),
            "reason": override, "runner": runner_label, "rc": rc,
            "touched": [t[0] for t in touched], "why": why,
        })
        _emit("override", goal_id, override, reason=why + " — overridden: " + override,
              runner=runner_label, rc=rc, touched=touched, tail=tail)
        return 0

    _emit("block", goal_id, override, reason=why, runner=runner_label, rc=rc, touched=touched, tail=tail)
    print("", file=sys.stderr)
    print(f"[domain-suite-gate] ✖ REFUSED status=completed for {goal_id}: {why}, and "
          f"{len(touched)} domain script(s) changed since this goal's claim "
          f"({since.strftime('%Y-%m-%dT%H:%M:%S')}{since_note}):", file=sys.stderr)
    for rel, mt in touched[-12:]:
        print(f"    {rel}  ({mt})", file=sys.stderr)
    if len(touched) > 12:
        print(f"    ... and {len(touched) - 12} more", file=sys.stderr)
    print(f"  Runner: {runner_label}. Last lines of the run:", file=sys.stderr)
    for ln in tail[-12:]:
        print("    " + ln[:200], file=sys.stderr)
    print("  Fix the suite, then close again. A collection error is almost always an import that", file=sys.stderr)
    print("  no longer resolves: restore the symbol in the module, or update the test if the rename", file=sys.stderr)
    print("  was deliberate and every importer moved. Re-run it yourself first:", file=sys.stderr)
    print('    cd "$WORLD_PATH/scripts" && STORAGE_BACKEND=local python3 -m pytest -q', file=sys.stderr)
    print("  Only when the red is PRE-EXISTING and tracked by a goal this close did not cause, pass", file=sys.stderr)
    print('    --override-domain-suite "<goal-id>: <why this close is not the cause>"', file=sys.stderr)
    print("  (appended to world/domain-suite-overrides.jsonl). Never override a collection error.", file=sys.stderr)
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--goal", required=True)
    ap.add_argument("--source", default="world", choices=["world", "agent"])
    ap.add_argument("--since", default=None, help="ISO timestamp; overrides the goal's claimed_at")
    ap.add_argument("--override", default=None, help="justification; turns a block into a logged pass")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = ap.parse_args(argv)
    since = _parse_iso(args.since) if args.since else None
    if args.since and since is None:
        print(f"domain-suite-gate: --since {args.since!r} is not an ISO timestamp", file=sys.stderr)
        return 2
    try:
        return evaluate(args.goal, args.source, since, args.override, args.timeout, WORLD_DIR)
    except Exception as e:  # noqa: BLE001 — fail OPEN: a broken gate must not wedge a close
        _emit("error", args.goal, args.override, reason=f"gate error, fail-open: {type(e).__name__}: {e}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
