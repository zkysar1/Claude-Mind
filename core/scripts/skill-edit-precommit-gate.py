#!/usr/bin/env python3
# domain-leak-exempt: framework eval substrate — generic SKILL.md structure dims, no domain strings.
"""Skill-edit earn-the-keep chokepoint (g-115-1467, earn-the-keep Phase 1 / G3).

A pre-commit gate that makes a SKILL.md EDIT "earn its keep": the working-tree
version must not REGRESS the committed (HEAD) version on objective structural
signals. Compares HEAD vs working-tree per changed `.claude/skills/*/SKILL.md`
and routes the two score-sets through `eval_harness.gate(no_regression)` — the
same validation substrate the keystone self-check validated (G3). A commit that
silently guts a SKILL.md (drops the front matter, removes the Return Protocol
section, truncates the body, collapses the heading structure, or strips the
procedure) is BLOCKED.

WHY a content gate, not just structure validation
-------------------------------------------------
`skill-structure-gate.py` is a PreToolUse advisory that validates a SINGLE
version's invariants. This gate is the missing BEFORE/AFTER earn-the-keep
chokepoint at commit time, on the canonical regression surface: the 2026-05-11
incident where 7 SKILL.md files silently lost their YAML front matter (every
downstream parser broke) is exactly a HEAD->worktree structural REGRESSION this
gate blocks (.claude/rules/domain-free-examples.md "Why this matters"). The
no_regression policy means legitimate edits (adding content, fixing typos) pass
— only a strict drop in objective structural completeness blocks.

Distinct from the FORGE-time skill-quality gate (G2, forge-skill acceptance):
that judges a NEW/edited skill's 5-dim quality at forge time; this is the
commit-time structural-regression net. Both consume eval_harness.gate.

NOT in HEAD (brand-new skill) or deleted: SKIPPED — there is no "before" to
regress from (new skills are governed by the forge-time gate; deletions are
intentional).

Bypass: set SKILL_EDIT_GATE_OVERRIDE="<justification>" to allow a deliberate
structural reduction (echoed to stderr for audit), mirroring
marker-placement-gate's MARKER_PLACEMENT_OVERRIDE.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import eval_harness as eh  # noqa: E402

# Registered id in core/config/gates.yaml — NOT the filename. Counting this
# gate's firings by the script name returns a false zero (measured 2026-08-31:
# the sibling Tier-1 gate logs as `eval-harness-forge-accept`).
GATE_ID = "skill-edit-precommit"


def _log_firing(decision, path, verdict=None, override=None):
    """Telemetry (guard-502): lazy import, best-effort, never influences the gate."""
    try:
        import _gate_log
        _gate_log.log(GATE_ID, decision, caller=path,
                      override_reason=override or None,
                      extra=(verdict.as_dict() if verdict is not None else None))
    except Exception:
        return

# Objective structural dimensions — each maps a SKILL.md body to [0,1]. These
# are the signals real regressions have dropped (front-matter loss; Return
# Protocol loss; truncation). Weighted equally; the gate keys on the aggregate.
DIMS = ("front_matter", "return_protocol", "body_substance", "headings", "procedure")

_FRONT_MATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.DOTALL)
_RETURN_PROTO_RE = re.compile(r"(?m)^#{2,3}\s+Return Protocol\b")
_H2_RE = re.compile(r"(?m)^##\s+\S")
_PROCEDURE_RE = re.compile(r"(?m)(^|\s)(Step\s|Phase\s|Bash:|Skill\()")


def score_skill_md(text: str) -> Dict[str, float]:
    """Score one SKILL.md's text on the objective structural dims -> {dim: [0,1]}."""
    fm = _FRONT_MATTER_RE.match(text)
    body = text[fm.end():] if fm else text
    n_h2 = len(_H2_RE.findall(text))
    return {
        "front_matter": 1.0 if fm else 0.0,
        "return_protocol": 1.0 if _RETURN_PROTO_RE.search(text) else 0.0,
        # Cap at 500 chars: once a body is substantial, further length is not
        # "more structure". Only truncation toward empty drops this below 1.0.
        "body_substance": min(1.0, len(body.strip()) / 500.0),
        # Three H2 sections is a structurally complete skill; collapse drops it.
        "headings": min(1.0, n_h2 / 3.0),
        "procedure": 1.0 if _PROCEDURE_RE.search(body) else 0.0,
    }


def _git_show_head(path: str) -> Optional[str]:
    """HEAD blob of `path`, or None if the file is new (not tracked at HEAD)."""
    r = subprocess.run(["git", "show", f"HEAD:{path}"],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def _staged_skill_mds() -> List[str]:
    """Staged (index) `.claude/skills/*/SKILL.md` paths, added/modified only.

    --diff-filter=d excludes deletions (an intentional delete is not a
    regression). Pre-commit semantics: the index is what is about to commit.
    """
    r = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=d"],
        capture_output=True, text=True)
    if r.returncode != 0:
        return []
    out = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if re.match(r"\.claude/skills/[^/]+/SKILL\.md$", line):
            out.append(line)
    return out


def _dim_cases():
    return [eh.EvalCase(id=d, weight=1.0) for d in DIMS]


def evaluate_path(path: str, *, head_text: Optional[str] = None,
                  new_text: Optional[str] = None,
                  epsilon: float = 0.0) -> Optional[eh.Verdict]:
    """Gate one SKILL.md edit. Returns a Verdict, or None if SKIPPED (new file).

    `head_text` / `new_text` override the git/on-disk reads (test seams).
    Compares HEAD (before) vs working-tree (after) under no_regression: the edit
    must not lower the aggregate structural score.
    """
    head = head_text if head_text is not None else _git_show_head(path)
    if head is None:
        return None  # brand-new skill — no before to regress from; SKIP
    after_text = new_text if new_text is not None else Path(path).read_text(encoding="utf-8")
    before = score_skill_md(head)
    after = score_skill_md(after_text)
    return eh.gate(before, after, _dim_cases(), policy="no_regression",
                   epsilon=epsilon, split="all")


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Explicit paths may be passed (test/CI); default to the staged set.
    paths = [a for a in argv if not a.startswith("-")] or _staged_skill_mds()
    if not paths:
        return 0  # nothing to gate — no SKILL.md staged

    override = os.environ.get("SKILL_EDIT_GATE_OVERRIDE", "").strip()
    regressions: List[Tuple[str, eh.Verdict]] = []
    for p in paths:
        v = evaluate_path(p)
        if v is None:
            print(f"[skill-edit-gate] SKIP {p} (new skill — forge-time gate governs)",
                  file=sys.stderr)
            _log_firing("noop", p)
            continue
        if not v.passed:
            regressions.append((p, v))
        else:
            _log_firing("pass", p, v)

    if not regressions:
        return 0

    for p, v in regressions:
        print(f"[skill-edit-gate] STRUCTURAL REGRESSION: {p}", file=sys.stderr)
        print(f"    {v.reason} (before={v.before:.3f} after={v.after:.3f} "
              f"delta={v.delta:.3f})", file=sys.stderr)

    if override:
        for p, v in regressions:
            _log_firing("override", p, v, override)
        print(f"[skill-edit-gate] OVERRIDE accepted: {override!r} — allowing "
              f"{len(regressions)} structural regression(s) (audit: stderr).",
              file=sys.stderr)
        return 0

    for p, v in regressions:
        _log_firing("block", p, v)
    print("[skill-edit-gate] BLOCKED: a SKILL.md edit regressed structural "
          "completeness (front matter / Return Protocol / body / headings / "
          "procedure). Fix the edit, or set "
          'SKILL_EDIT_GATE_OVERRIDE="<justification>" for a deliberate reduction.',
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
