#!/usr/bin/env python3
"""check-framework-origin-writes.py — pre-commit Gate 15: on a deployment that takes
its framework from a `framework_origin`, refuse a commit that changes framework files.

WHY: see _framework_origin.py. The L1 hook refuses the Write/Edit at write time;
this gate is the backstop for the shell shapes L1 cannot see (`cat > file <<EOF`,
`sed -i`, `cp`), because the commit is the one chokepoint Bash cannot skip. A
refusal names the files and the revert, so the working tree does not stay dirty
against the next upstream merge.

RULE: when core/config/environments/<ENVIRONMENT_ID>.yaml carries
`framework_origin: <other-env>`, ANY staged change (add/modify/delete/rename) under
core/, .claude/, mind_api/ or to CLAUDE.md is refused. Deployments without the
field (every framework origin) are untouched — the gate exits 0 before touching git.

OVERRIDE: `FRAMEWORK_WRITE_OVERRIDE="<why>" git commit ...` — the promotion train's
own plant commit (seed-transplant.sh) sets it, so the sanctioned writer passes and
the exception is logged as an `override` gate firing rather than silent.

Exit 0 = clean (origin deployment / nothing framework staged / overridden); 1 = refused.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _framework_origin import framework_origin, is_framework_path, self_env_id  # noqa: E402

GATE_ID = "check-framework-origin-writes"
OVERRIDE_ENV = "FRAMEWORK_WRITE_OVERRIDE"


def _git(root: Path, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(["git", "-C", str(root), *args],
                              capture_output=True, text=True, timeout=30)
        return proc.returncode, proc.stdout
    except Exception as e:  # noqa: BLE001 — a broken git must fail OPEN here
        return 127, f"{type(e).__name__}: {e}"


def staged_framework_paths(root: Path) -> list[str]:
    """Every staged path (any status) the promotion train owns."""
    rc, out = _git(root, "diff", "--cached", "--name-only", "-z")
    if rc != 0:
        return []
    return [p for p in out.split("\0") if p and is_framework_path(p)]


def _log(root: Path, decision: str, trigger: str, reason: str = "") -> None:
    """Telemetry, fail-open: a logging failure never changes the verdict."""
    try:
        from _runtime_bash import bash_cmd  # guard-580/581: never a bare "bash" argv[0]
        extra = ["--override-reason", reason[:300]] if reason else []
        subprocess.run(bash_cmd(root / "core" / "scripts" / "gate-log.sh", GATE_ID, decision,
                                "--caller", "core/githooks/pre-commit",
                                "--trigger", trigger[:200], *extra),
                       capture_output=True, text=True, timeout=30)
    except Exception:  # noqa: BLE001
        pass


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path.cwd()
    rc, top = _git(root, "rev-parse", "--show-toplevel")
    if rc == 0 and top.strip():
        root = Path(top.strip())
    origin = framework_origin(root)
    if not origin:
        return 0
    offenders = staged_framework_paths(root)
    if not offenders:
        return 0
    env_id = self_env_id() or "this deployment"
    listing = ", ".join(offenders[:6]) + (", ..." if len(offenders) > 6 else "")
    override = os.environ.get(OVERRIDE_ENV, "").strip()
    if override:
        _log(root, "override", listing, override)
        print(f"[{GATE_ID}] OVERRIDDEN ({override}): framework files committed on "
              f"{env_id} (origin {origin}): {listing}", file=sys.stderr)
        return 0
    _log(root, "block", listing)
    print(f"[{GATE_ID}] REFUSED: {len(offenders)} framework file(s) staged on {env_id}, "
          f"which takes its framework from {origin} through the promotion train "
          f"(`framework_origin: {origin}` in core/config/environments/{env_id}.yaml): "
          f"{listing}", file=sys.stderr)
    print("  Framework files (core/, .claude/, mind_api/, CLAUDE.md) are not edited here — "
          "not even to record step results; a SKILL.md is instructions, not a worksheet.",
          file=sys.stderr)
    print("  Revert them now so the tree stays mergeable:  git checkout HEAD -- <path>   "
          "(a NEW file: git rm --cached -- <path>, then delete it)", file=sys.stderr)
    print(f"  Send the improvement UP instead:  bash core/scripts/cross-world-inject-goal.sh "
          f"--target {origin} --title \"Idea: ...\" --description \"...\" --reason \"...\" --shared",
          file=sys.stderr)
    print(f"  Sanctioned framework writer (the promotion plant): {OVERRIDE_ENV}=\"<why>\" "
          "git commit ... (logged as an override).", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
