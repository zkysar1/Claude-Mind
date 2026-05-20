#!/usr/bin/env python3
"""Pre-commit Gate 5 — settings.json deny-baseline net.

Independent second net BEHIND the PreToolUse settings-structural-validator
(Layer D of the constitutional-anchor design, 2026-05-16). The PreToolUse
hook catches autonomous Edit/Write/MultiEdit; this gate catches a deny[]/
hooks/top-level-keys regression that reaches the index by any other path
(a tool the matcher doesn't cover, a future Claude Code change, a manual
edit, a merge).

Single source of truth: this gate does NOT re-declare the protected deny
baseline. It loads the validator module and calls its `_validate()` on the
STAGED `.claude/settings.json` blob — the PROTECTED_DENY / PROTECTED_HOOKS /
ALLOWED_TOP_LEVEL_KEYS constants live ONLY in
core/scripts/settings-structural-validator.py (guard-130 / guard-395:
no duplicated baseline across files).

Fail-closed: any inability to read the staged blob or load the validator
blocks the commit (a security-critical gate that cannot evaluate must not
silently pass). No-op when settings.json is not part of the commit.

Cross-refs: rb-931 (bootstrap paradox), CLAUDE.md "two-file settings rule",
core/config/conventions/constitutional-rings.md Ring 0.
"""

import importlib.util
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
VALIDATOR = REPO / "core" / "scripts" / "settings-structural-validator.py"
SETTINGS = ".claude/settings.json"


def _fail(msg: str) -> "None":
    print(f"[settings-deny-baseline] {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> int:
    # Only act when .claude/settings.json is staged for this commit.
    try:
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            capture_output=True, text=True, cwd=str(REPO),
        ).stdout.split()
    except Exception as exc:  # pragma: no cover - git always present in hook ctx
        _fail(f"could not list staged files ({exc!r}); failing closed")
    if SETTINGS not in staged:
        return 0  # settings.json untouched this commit — no-op

    blob = subprocess.run(
        ["git", "show", f":{SETTINGS}"],
        capture_output=True, text=True, cwd=str(REPO),
    )
    if blob.returncode != 0:
        _fail(f"could not read staged {SETTINGS} (rc={blob.returncode}); "
              "failing closed")

    if not VALIDATOR.is_file():
        _fail(f"validator missing at {VALIDATOR}; failing closed")

    # Hyphenated filename -> importlib (not a valid `import` name).
    spec = importlib.util.spec_from_file_location("ssv_baseline", str(VALIDATOR))
    if spec is None or spec.loader is None:
        _fail("could not build import spec for validator; failing closed")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception as exc:
        _fail(f"could not load validator module ({exc!r}); failing closed")

    try:
        ok, reason = mod._validate(blob.stdout)  # type: ignore[attr-defined]
    except Exception as exc:
        _fail(f"validator._validate raised ({exc!r}); failing closed")

    if not ok:
        print(
            "[settings-deny-baseline] BLOCKED: staged .claude/settings.json "
            f"fails the structural validator: {reason}",
            file=sys.stderr,
        )
        print(
            "  Independent pre-commit net behind the PreToolUse validator "
            "(constitutional anchor). Fix the deny[]/hooks/top-level-keys "
            "regression or `git restore --staged .claude/settings.json`.",
            file=sys.stderr,
        )
        return 1

    print(
        "[settings-deny-baseline] OK — staged .claude/settings.json passes "
        "the structural validator.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
