#!/usr/bin/env python3
"""check-repo-root-entries.py — refuse a commit that INVENTS a new top-level entry.

WHY (measured 2026-08-30, coach@zc-03, a third Mind deployment run by small-model
Bodies): 8 top-level entries appeared in the repo that the upstream tree does not
have — `.busy`, `.mind-data` (a force-added file under an ignored root), `.zakcode`,
`temp`, `tests`, `wind` (an empty file), `yahoo`, plus untracked `world/` and
`coach/` — and 6 of the 8 were cruft. The worst was `yahoo/` + `tests/`: the domain
package the world already carried under `<world>/scripts/yahoo` was re-created at
the repo root by one goal (g-006-09, "Build yahoo/transactions.py"), and from then
on two copies diverged (six modules differ, each side holds tests the other lacks)
while the Bodies spent goals "syncing" them by hand.

The L1 path-resolution hook refuses an invented top-level entry under world/,
meta/ and the agent dir — but only for Write/Edit, and PROJECT_ROOT itself is not
a governed root, so a Bash `mkdir`/`touch`/`cp` walks straight past it. The commit
is the one chokepoint Bash cannot bypass (`--no-verify` is already gated), and a
refusal there can say WHERE the file belongs instead of letting the invention
harden into a second copy.

RULE: a staged ADDITION whose first path component is not already a top-level
entry of HEAD is refused. Modifications, deletions and renames inside existing
top-level entries never trip it. The initial commit (no HEAD) is exempt.

OVERRIDE: `REPO_ROOT_ENTRY_OVERRIDE="<why>" git commit ...` — for the genuine
framework extension. Logged as an `override` gate firing so the exception is
auditable rather than silent.

Exit 0 = clean (or overridden / nothing staged / no HEAD); 1 = refused.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

GATE_ID = "check-repo-root-entries"
OVERRIDE_ENV = "REPO_ROOT_ENTRY_OVERRIDE"


def _git(root: Path, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(["git", "-C", str(root), *args],
                              capture_output=True, text=True, timeout=30)
        return proc.returncode, proc.stdout
    except Exception as e:  # noqa: BLE001 — a broken git must fail OPEN here
        return 127, f"{type(e).__name__}: {e}"


def head_top_level(root: Path) -> set[str] | None:
    """Top-level entry names at HEAD, or None when there is no HEAD (initial commit)."""
    rc, out = _git(root, "ls-tree", "--name-only", "HEAD")
    if rc != 0:
        return None
    return {line.strip() for line in out.splitlines() if line.strip()}


def staged_additions(root: Path) -> list[str]:
    rc, out = _git(root, "diff", "--cached", "--name-only", "--diff-filter=A", "-z")
    if rc != 0:
        return []
    return [p for p in out.split("\0") if p]


def new_top_level(root: Path) -> dict[str, list[str]]:
    """{new top-level component: [staged paths under it]} — empty when clean."""
    existing = head_top_level(root)
    if existing is None:
        return {}
    offenders: dict[str, list[str]] = {}
    for path in staged_additions(root):
        top = path.split("/", 1)[0]
        if top and top not in existing:
            offenders.setdefault(top, []).append(path)
    return offenders


def _world_scripts_hint(root: Path) -> str:
    """The concrete domain-scripts home for this deployment, best effort."""
    try:
        sys.path.insert(0, str(root / "core" / "scripts"))
        from _paths import WORLD_DIR  # type: ignore
        if WORLD_DIR:
            return str(Path(WORLD_DIR) / "scripts")
    except Exception:  # noqa: BLE001
        pass
    return "$WORLD_PATH/scripts"


def _log(root: Path, decision: str, trigger: str, reason: str = "") -> None:
    """Telemetry, fail-open: a logging failure never changes the verdict."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
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
    offenders = new_top_level(root)
    if not offenders:
        return 0
    override = os.environ.get(OVERRIDE_ENV, "").strip()
    listing = "; ".join(f"{top}/ ({len(paths)} file(s): {', '.join(paths[:3])}"
                        f"{', ...' if len(paths) > 3 else ''})"
                        for top, paths in sorted(offenders.items()))
    if override:
        _log(root, "override", listing, override)
        print(f"[{GATE_ID}] OVERRIDDEN ({override}): new top-level entries {listing}",
              file=sys.stderr)
        return 0
    _log(root, "block", listing)
    hint = _world_scripts_hint(root)
    print(f"[{GATE_ID}] REFUSED: this commit would create a NEW top-level entry that "
          f"HEAD does not have — {listing}.", file=sys.stderr)
    print("  The repo root is not a place to invent directories. Route instead:", file=sys.stderr)
    print(f"    domain code / scripts / their tests -> {hint}/ (tests under {hint}/tests/)",
          file=sys.stderr)
    print("    scratch, drafts, logs, evidence      -> agents/<agent>/temp/ or "
          "agents/<agent>/sessions/<SID>/scratch/", file=sys.stderr)
    print("    framework code                       -> core/scripts/, core/config/, "
          ".claude/skills/, .claude/rules/", file=sys.stderr)
    print("  A copy at the root of something the world already carries becomes a "
          "second, diverging copy (measured: yahoo/ vs <world>/scripts/yahoo/).",
          file=sys.stderr)
    print(f"  Genuine framework extension: {OVERRIDE_ENV}=\"<why>\" git commit ... "
          "(logged as an override).", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
