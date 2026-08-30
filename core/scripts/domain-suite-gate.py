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
  3b. Credential tripwire (g-353-79): the credential-NAMED files directly under
     the world, its parent and the project root (.env* / *token* / *secret* /
     *credential* / *.pem / *.key / *.p12 / id_*) are snapshotted by size+mtime
     around the run; if any was rewritten the close is BLOCKED whatever the
     suite's rc, and no override lifts it (a live deployment lost its token to
     a mocked-refresh test on 2026-08-29).
  4. rc 0 → pass. rc 5 (pytest: nothing collected) → pass, noted. A timeout
     or a pytest internal/usage error (3/4) is a gate fault → fail-open with a
     warning. rc 2 (collection error) → BLOCK, always. rc 1 (red) → the
     RATCHET: the failing set from the previous run on this box
     (world/domain-suite-baseline.json) is the baseline; the first run seeds
     it and passes; later runs block only on a red NOT in it, and a run whose
     reds are a subset passes and shrinks the baseline. A block exits 1 with
     the touched files and the last lines on stderr. `--override "<why>"`
     turns a block into a pass and appends one row to
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
import re
import stat
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _paths import PROJECT_ROOT, WORLD_DIR  # noqa: E402
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

# ─── credential tripwire () ────────────────────────────────────────
# The suite this gate demands as the price of a close is also a process running
# with the Body's full environment. Measured 2026-08-29 23:46Z on a live
# deployment: the green re-run a close was waiting on persisted a MOCKED
# token-refresh response over the real token file (a save helper defaulting to a
# hardcoded absolute path), and the close then passed on that green. The gate
# cannot know a domain's credential paths, but it can know their SHAPE: a file
# directly under a governed root whose NAME says what it holds. Those are
# snapshotted (size, mtime) before the run and compared after; a rewrite is a
# BLOCK no override lifts — restoring the file and isolating the persistence
# path (guard-5541) is the only way through. Name only, not mode: measured on the
# deployment that motivated this, every bland-named 0600 file under the roots
# was a peer-written store or doc (forged-skills.yaml, program.md,
# requirements.txt) — under eight concurrent Bodies a mode heuristic blocks
# closes for writes no test made, and caught nothing a name does not. Stores
# and docs (.jsonl/.md) are skipped for the same reason even when named.
PRIVATE_NAME_RE = re.compile(r"(?i)^\.env|token|secret|credential|\.pem$|\.key$|\.p12$|^id_(rsa|ed25519|ecdsa)")
PRIVATE_SKIP_SUFFIXES = {".lock", ".pid", ".port", ".sock", ".log", ".tmp", ".bak", ".jsonl", ".md"}


def private_roots(world_dir: Path | None) -> list[Path]:
    """The world, its parent (where a deployment keeps .env.local beside .mind-data),
    and the project root — deduplicated, in that order."""
    roots: list[Path] = []
    candidates = ([Path(world_dir), Path(world_dir).parent] if world_dir else []) + [Path(PROJECT_ROOT)]
    for r in candidates:
        if r not in roots:
            roots.append(r)
    return roots


def private_files(roots: list[Path]) -> dict[str, tuple[int, int]]:
    """{path: (size, mtime_ns)} of the credential-shaped files DIRECTLY under each
    root. Shape only — the contents are never read."""
    out: dict[str, tuple[int, int]] = {}
    for root in roots:
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for p in entries:
            try:
                st = p.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode) or p.suffix.lower() in PRIVATE_SKIP_SUFFIXES:
                continue
            if PRIVATE_NAME_RE.search(p.name):
                out[str(p)] = (st.st_size, st.st_mtime_ns)
    return out


def rewritten_private_files(before: dict, after: dict) -> list[str]:
    """Paths whose size or mtime changed, or that vanished, across the suite run."""
    return sorted(p for p, sig in before.items() if after.get(p) != sig)


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


def run_suite(scripts_dir: Path, timeout: int) -> tuple[int | None, list[str], set[str]]:
    """(rc, last lines, failing ids). rc None means the run exceeded `timeout`."""
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
        return None, text.splitlines()[-TAIL_LINES:], set()
    text = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return proc.returncode, lines[-TAIL_LINES:], failing_ids(lines)


_PYTEST_FAILED = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)")
# The runner's per-unit verdict: `[N/M] FAIL test_x.sh (rc=1)`, `[6/77] FAIL pytest batch (rc=1)`.
_RUNNER_UNIT_FAIL = re.compile(r"^\s*\[\d+/\d+\]\s+FAIL\s+(.+?)(?:\s+\(rc=\d+\))?\s*$")
# A bare `FAIL <unit>` only when <unit> looks like a file or a node id. A shell test's
# INNER assertion lines (`  FAIL the tag alone decides whether it retries`) must not
# become ids: measured on the first seed, they produced "the" — and a baseline entry
# "the" would have laundered every future inner failure that starts with that word.
_RUNNER_BARE_FAIL = re.compile(r"^\s*FAIL\s+(\S+(?:\.sh|\.py|\.bash)|\S+::\S+|\S+/\S+)\s*$")


def failing_ids(lines: list[str]) -> set[str]:
    """The failing units named in a run's output, as a set of identifiers.

    Two shapes are recognised, both stable: pytest's short summary
    (`FAILED path::test - msg`, `ERROR path::test`) and the domain runner's
    per-unit lines (`[N/M] FAIL file.sh (rc=1)`, or a bare `FAIL file.sh`).
    Everything else — including a shell test's inner `FAIL <sentence>` lines —
    is ignored, so an unrecognised runner yields an EMPTY set, and an empty set
    on a red run is treated as "cannot prove pre-existing", which blocks.
    """
    ids: set[str] = set()
    for ln in lines:
        m = _PYTEST_FAILED.match(ln)
        if m:
            ids.add(m.group(1))
            continue
        m = _RUNNER_UNIT_FAIL.match(ln) or _RUNNER_BARE_FAIL.match(ln)
        if m:
            ids.add(m.group(1).strip())
    return ids


# ─── the baseline (ratchet) ───────────────────────────────────────────────
#
# A world's suite may already be red before this gate exists (measured on the
# dev world: 2 real reds nobody had seen, in a 651 s run). Demanding GREEN would
# refuse every close on such a world until someone fixed reds that no close
# caused, so the verdict is a RATCHET, the audit-baselines shape: the failing
# set from the previous run is the baseline; a close blocks only on a red that
# is NOT in it (or on a collection error, which is never baseline-able); a run
# whose reds are a subset of the baseline passes AND shrinks the baseline to
# what still fails. The first run seeds it. The file is per box (a plain write
# to the world mirror; on a synced world it stays local), which is the honest
# scope — the baseline describes what THIS box last saw.

def _baseline_path(world_dir: Path) -> Path:
    return Path(world_dir) / "domain-suite-baseline.json"


def load_baseline(world_dir: Path) -> dict | None:
    try:
        doc = json.loads(_baseline_path(world_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and isinstance(doc.get("failing"), list) else None


def save_baseline(world_dir: Path, failing: set[str], rc: int, runner: str) -> None:
    payload = {"recorded_at": datetime.now().isoformat(timespec="seconds"),
               "agent": os.environ.get("MIND_AGENT", "unknown"),
               "runner": runner, "rc": rc, "failing": sorted(failing)}
    try:
        _baseline_path(world_dir).write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
    except OSError as e:
        print(f"domain-suite-gate: baseline write failed: {e}", file=sys.stderr)


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
    roots = private_roots(world_dir)
    before = private_files(roots)
    rc, tail, failing = run_suite(scripts_dir, timeout)
    clobbered = rewritten_private_files(before, private_files(roots))
    if clobbered:
        why = (f"the domain suite REWROTE {len(clobbered)} credential-shaped file(s) outside its own tree: "
               + ", ".join(clobbered[:4]) + (" ..." if len(clobbered) > 4 else "")
               + " — a test wrote through to a live credential path")
        _emit("block", goal_id, override, reason=why, runner=runner_label, rc=rc, touched=touched,
              clobbered=clobbered, tail=tail)
        print("", file=sys.stderr)
        print(f"[domain-suite-gate] ✖ REFUSED status=completed for {goal_id}: {why}.", file=sys.stderr)
        print("  Restore each file from its backup or upstream source of truth FIRST (the run may have", file=sys.stderr)
        print("  replaced a live token with a fixture), then make the persistence path overridable and", file=sys.stderr)
        print("  point the tests at a tmp path (guard-5541). --override-domain-suite does not apply here:", file=sys.stderr)
        print("  a rewritten credential is never pre-existing. (If another process legitimately", file=sys.stderr)
        print("  refreshed the file during the run, the file is intact — just re-run the close.)", file=sys.stderr)
        return 1
    if rc in (0, 5):
        note = "" if rc == 0 else " (pytest collected no tests)"
        save_baseline(world_dir, set(), rc, runner_label)
        _emit("pass", goal_id, override, reason="domain suite green" + note, runner=runner_label,
              rc=rc, touched=touched)
        return 0

    # Gate faults fail OPEN: a suite that cannot finish in `timeout`, a pytest
    # internal error (3) or usage error (4) say nothing about THIS close.
    if rc is None or rc in (3, 4):
        why = f"domain suite exceeded {timeout}s" if rc is None else f"pytest rc={rc} (internal/usage error)"
        _emit("error", goal_id, override, reason=why + " — not a verdict on this close; fail-open",
              runner=runner_label, rc=rc, touched=touched, tail=tail)
        print(f"[domain-suite-gate] WARN {why}; the domain suite was NOT verified for {goal_id}", file=sys.stderr)
        return 0

    baseline = load_baseline(world_dir)
    if rc == 2 or not failing:
        why = ("domain suite could not COLLECT (rc=2: an import or syntax error in a test module)"
               if rc == 2 else f"domain suite RED (rc={rc}) and no failing unit could be identified from its output")
    elif baseline is None:
        # First run on this box: what is red now predates this gate — EXCEPT a
        # red sitting in a file THIS unit touched, which the seed must not
        # launder. Measured on the first live seed of a deployment (2026-08-29):
        # 63 reds recorded, 2 of them in a test file the closing unit had
        # written 20 minutes earlier. Those block; the rest are recorded and
        # the ratchet starts here, not at green.
        touched_files = {t[0] for t in touched}
        touched_names = {Path(t[0]).name for t in touched}
        own = sorted(f for f in failing
                     if f.split("::")[0] in touched_files or Path(f.split("::")[0]).name in touched_names)
        if not own:
            save_baseline(world_dir, failing, rc, runner_label)
            _emit("pass", goal_id, override, reason=f"seeded baseline: {len(failing)} pre-existing red(s) recorded, "
                  "later closes block only on NEW reds", runner=runner_label, rc=rc, touched=touched,
                  failing=sorted(failing))
            return 0
        why = (f"no baseline yet, and {len(own)} red(s) sit in files THIS unit touched, so they cannot be "
               "called pre-existing: " + ", ".join(own[:6]) + (" ..." if len(own) > 6 else "")
               + f" ({len(failing) - len(own)} other red(s) will be recorded once these are fixed)")
    else:
        new_reds = sorted(failing - set(baseline["failing"]))
        if not new_reds:
            save_baseline(world_dir, failing, rc, runner_label)  # ratchet: only what still fails
            _emit("pass", goal_id, override, reason=f"{len(failing)} pre-existing red(s), none new since "
                  f"{baseline.get('recorded_at', '?')} (baseline ratcheted)", runner=runner_label, rc=rc,
                  touched=touched, failing=sorted(failing))
            return 0
        why = (f"domain suite has {len(new_reds)} NEW red(s) not in the baseline of "
               f"{baseline.get('recorded_at', '?')}: " + ", ".join(new_reds[:6])
               + (" ..." if len(new_reds) > 6 else ""))
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
