#!/usr/bin/env python3
"""Constitutional-anchor tripwire (Layer E — detection net, 2026-05-16).

Prevention layers (anchor self-deny, fail-closed validator, Gate-5
pre-commit) are A/B/D. This is the DETECTION layer: if any of them is
somehow defeated or drifts, this re-checks the invariants on a recurring
cadence and escalates so a human finds out within the interval rather
than days later. Also covers the Fork-1 surface (recovery rails left
editable on purpose) — drift there shows up here even though it is not
hard-denied.

Invariants checked (HARD = exit 1 on failure; ADVISORY = report only):

  H1  .claude/settings.local.json contains the self-anchor deny
      (Edit/Write/MultiEdit on settings.local.json) — the keystone.
  H2  .claude/settings.local.json contains the validator-guard deny
      (settings-structural-validator.py and .sh).
  H3  .claude/settings.json passes the structural validator's _validate()
      (A/C/D/E/F protected deny concepts + protected hooks + top-level
      keys) — reuses the validator (single source of truth, like Gate 5).
  A1  ~/.claude/settings.json carries the out-of-repo mirror (advisory:
      a missing mirror weakens defence-in-depth but the in-repo self
      anchor still holds).

Exit 0 = all hard invariants hold. Exit 1 = drift (caller files a HIGH
Investigate + notifies the user). Fail-closed: inability to evaluate a
hard invariant is treated as drift.

Cross-refs: rb-931, CLAUDE.md "two-file settings rule",
core/config/conventions/constitutional-rings.md Ring 0.
"""

import importlib.util
import json
import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
PROJ_SETTINGS = REPO / ".claude" / "settings.json"
LOCAL_SETTINGS = REPO / ".claude" / "settings.local.json"
USER_SETTINGS = pathlib.Path(os.path.expanduser("~/.claude/settings.json"))
VALIDATOR = REPO / "core" / "scripts" / "settings-structural-validator.py"


def _deny_list(path: pathlib.Path) -> "list[str]":
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        dn = (d.get("permissions") or {}).get("deny") or []
        return [x for x in dn if isinstance(x, str)]
    except Exception:
        return []


def main() -> int:
    failures: "list[str]" = []
    advisories: "list[str]" = []

    local_deny = _deny_list(LOCAL_SETTINGS)

    # H1 — self-anchor present (the keystone)
    if not any("settings.local.json" in s for s in local_deny):
        failures.append(
            "H1: .claude/settings.local.json missing the self-anchor deny "
            "(*settings.local.json) — the file is no longer tamper-proof"
        )

    # H2 — validator-guard present
    if not any("settings-structural-validator" in s for s in local_deny):
        failures.append(
            "H2: .claude/settings.local.json missing the validator-guard deny "
            "(settings-structural-validator.{py,sh}) — the validator can be "
            "neutered (rb-931 bootstrap paradox re-opened)"
        )

    # H3 — project settings.json still passes the structural validator
    try:
        spec = importlib.util.spec_from_file_location("ssv_tw", str(VALIDATOR))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        ok, reason = mod._validate(  # type: ignore[attr-defined]
            PROJ_SETTINGS.read_text(encoding="utf-8")
        )
        if not ok:
            failures.append(
                f"H3: .claude/settings.json fails structural validator: {reason}"
            )
    except Exception as exc:
        failures.append(
            f"H3: could not evaluate settings.json via validator ({exc!r}) "
            "— failing closed"
        )

    # A1 — out-of-repo mirror (advisory)
    user_deny = _deny_list(USER_SETTINGS)
    if not any("settings.local.json" in s for s in user_deny):
        advisories.append(
            "A1: ~/.claude/settings.json missing the out-of-repo mirror "
            "(defence-in-depth weakened; in-repo self-anchor still holds)"
        )

    report = {
        "tripwire": "constitutional-anchor",
        "status": "DRIFT" if failures else "OK",
        "hard_failures": failures,
        "advisories": advisories,
    }
    print(json.dumps(report, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
