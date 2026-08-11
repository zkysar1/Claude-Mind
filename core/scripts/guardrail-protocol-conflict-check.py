#!/usr/bin/env python3
"""Detect active guardrails that forbid a command an always-run protocol line invokes.

THE CLASS (g-115-4005, generalised from ZDS g-001-253). Guardrails and SKILL.md
protocol text have asymmetric reach: an always-run protocol line is executed every
iteration by every agent, while a guardrail is read only when retrieval happens to
surface it. When the two disagree the protocol wins silently, the guardrail becomes
decorative while still reading `active`, and the store shows a healthy rule whose
behaviour is the opposite of what it says.

WHY THE OBVIOUS PREDICATE DOES NOT WORK -- measured on this corpus 2026-08-01
(alpha, cc-04), NOT assumed. The originating spec proposed: extract command tokens
from guardrail rules, grep SKILL.md for the same token, report co-occurrence, on the
argument that "the false-positive cost is one human/LLM read". Measured against 1,969
active guardrails and 93 SKILL.md files:

    naive script-name co-occurrence .................... 1,137 rows
    + require a prohibition word anywhere in the rule ... 1,137 rows  (-3.6%)
    + require script+FLAG rather than bare script .........  443 rows
    + require the SKILL.md line be unconditional ..........  311 rows
    + require the prohibition to GOVERN the command .......   12 rows

The prohibition-word filter is worth almost nothing because "before" and "never" are
the native idiom of guardrail prose -- they are in the rules for reasons unrelated to
the command named. And the deeper inversion: guardrails overwhelmingly PRESCRIBE the
correct invocation rather than prohibit it (`guard-1036` teaches the right
`board-post.sh --channel` form; nine guardrails teach correct `iteration-close.sh
--phase` usage). Co-occurrence therefore points the WRONG WAY -- it scores evidence
that a command is being used correctly as evidence it is forbidden. See rb-6305.

WHAT ACTUALLY DISCRIMINATES, and why each part earns its place:
  1. The prohibition must GOVERN the command -- same sentence, marker positioned
     before it. This is the single biggest lever (311 -> 12).
  2. The signature must carry at least one flag, and extends to the following
     literal argument. `guard-531` forbids `tree-update.sh --set <key> last_updated`,
     NOT `--set` generally; matching on the flag alone reports every `--set
     growth_state` call in the framework.
  3. Compliance narration is RANKED, NOT DROPPED. A SKILL.md line citing the guard
     id, or negating the call ("No explicit ... call needed"), is usually the
     protocol OBEYING the guardrail. But suppressing those rows outright would hide
     a genuine violation that happens to cite its own guardrail, so they are
     reported last with `likely_compliance: true`. A filter that silently drops the
     true case is the failure mode this whole check exists to prevent.

Read-only. Exit 0 always unless --exit-on-hits is passed (then 1 when unreviewed
conflicts remain), so it is safe to wire into an advisory sweep.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# A command signature: script name followed by at least one flag, plus any
# literal (non-placeholder) argument tokens that follow. Placeholders like
# <key> are captured but normalised away -- they carry no matching power.
INVOCATION_RE = re.compile(
    r"\b([A-Za-z0-9_][A-Za-z0-9_.-]*\.(?:py|sh))"
    r"((?:\s+(?:--[a-z][a-z0-9-]*|<[^>]{1,40}>|[a-z][a-z0-9_.-]*))+)"
)

# Prohibition markers. Case-insensitive is load-bearing: guardrail prose opens
# sentences with "Never ..." and a case-sensitive matcher silently misses every
# one of them. A positive control caught exactly that bug during development.
PROHIBITION_RE = re.compile(
    r"\b(never|do\s+not|don'?t|must\s+not|refuse\s+to\s+run|forbid\w*|"
    r"prohibit\w*|not\s+be\s+run|avoid)\b",
    re.I,
)

SENTENCE_SPLIT_RE = re.compile(r"(?<=[.;!?])\s+|\n")

# Lines that are the protocol OBEYING the guardrail rather than violating it.
COMPLIANCE_RE = re.compile(
    r"(no\s+explicit|not\s+needed|no\s+longer|do\s+NOT\s+add|never\s+use|"
    r"guard-\d+|auto-fires|is\s+NOT\s+needed|MUST\s+NOT)",
    re.I,
)

# An explicit, author-supplied reconciliation escape hatch (per the goal spec):
# a guardrail carrying `reconciled: <reason>` is a known-and-accepted pairing.
RECONCILED_RE = re.compile(r"\breconciled:\s*(\S.*)", re.I)


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _world_path() -> Path:
    """Resolve world/ through the framework resolver, not a hardcoded guess.

    world/ is an EXTERNAL, per-agent-configured path (see
    core/config/conventions/external-paths.md) -- a `PROJECT_ROOT/.mind-data`
    literal happens to be right on this box and is wrong on any deployment
    that configures it elsewhere. _paths.WORLD_DIR is the single source of
    truth. Env override stays first so tests can point at a tmp corpus.
    """
    env = os.environ.get("WORLD_PATH") or os.environ.get("WORLD_DIR")
    if env:
        return Path(env)
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _paths import WORLD_DIR  # type: ignore

        if WORLD_DIR:
            return Path(WORLD_DIR)
    except Exception:
        pass
    return _project_root() / ".mind-data" / "world"


def load_guardrails(world: Path) -> list[dict]:
    path = world / "guardrails.jsonl"
    rows: list[dict] = []
    if not path.exists():
        return rows
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


# Where a command signature ends and prose resumes. Guardrail rules embed
# invocations mid-sentence ("... --set <key> last_updated after an Edit"), so
# without a boundary the trailing English is captured as arguments and the
# signature never matches a real SKILL.md line. Found by the test suite: two
# synthetic rules ending in prose extracted junk tuples while the live corpus
# happened not to, which is exactly the case a hand-check would miss.
_PROSE_STOP = frozenset("""
a an the and or but for that this these those it its to in on of as at by per via
from into with without after before when while if then so because which is are be
was were do does did not no use uses using run runs call calls calling should must
you your we our they their there here how why what who
""".split())


def normalise_args(raw: str) -> tuple[str, ...]:
    """Flags and literal arguments, truncated where prose resumes.

    Placeholder tokens (<key>) are dropped but do NOT terminate the sequence --
    `--set <key> last_updated` must still yield ('--set', 'last_updated').
    """
    out: list[str] = []
    for tok in raw.split():
        if tok.startswith("<"):
            continue
        if not tok.startswith("--") and tok.lower().strip(".,;:'\"") in _PROSE_STOP:
            break
        out.append(tok.strip(".,;:'\""))
    return tuple(t for t in out if t)


def constrained_signatures(rule: str) -> set[tuple[str, tuple[str, ...]]]:
    """Command signatures a prohibition GOVERNS in this rule text.

    Governed means: same sentence as a prohibition marker, and positioned
    after it. A signature must contain at least one flag -- without that,
    prose following a script name ("_env.sh must add the ...") is captured
    as if it were an argument list.
    """
    found: set[tuple[str, tuple[str, ...]]] = set()
    for sentence in SENTENCE_SPLIT_RE.split(rule or ""):
        marker = PROHIBITION_RE.search(sentence)
        if not marker:
            continue
        for match in INVOCATION_RE.finditer(sentence):
            if match.start() < marker.start():
                continue  # the command precedes the prohibition -- not governed
            args = normalise_args(match.group(2))
            if not any(a.startswith("--") for a in args):
                continue  # no flag -> prose, not a command signature
            found.add((match.group(1), args))
    return found


def build_index(guardrails: list[dict]) -> dict:
    """(script, args) -> {"owners": [...], "reconciled": {gid: reason}}"""
    index: dict = {}
    for g in guardrails:
        if (g.get("status") or "active") != "active":
            continue
        rule = g.get("rule") or ""
        gid = g.get("id")
        rec = RECONCILED_RE.search(rule)
        for sig in constrained_signatures(rule):
            entry = index.setdefault(sig, {"owners": [], "reconciled": {}})
            entry["owners"].append(gid)
            if rec:
                entry["reconciled"][gid] = rec.group(1)[:200]
    return index


def line_matches(line: str, script: str, args: tuple[str, ...]) -> bool:
    if script not in line:
        return False
    for tok in args:
        if not re.search(r"(?<![\w-])" + re.escape(tok) + r"(?![\w-])", line):
            return False
    return True


def scan(root: Path, index: dict) -> list[dict]:
    hits: list[dict] = []
    for skill_md in sorted(root.glob(".claude/skills/*/SKILL.md")):
        try:
            lines = skill_md.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for lineno, line in enumerate(lines, 1):
            # Compliance narration routinely spans a line break -- the negation
            # ("no explicit") lands on the previous comment line while the
            # command sits on this one. Measured on this corpus: a line-scoped
            # window mislabelled 2 of 6 actionable rows for exactly that reason.
            # Strip comment leaders before joining: prose in this corpus wraps
            # across comment lines, so "... — no" / "# explicit ... call needed"
            # puts the negation and its object on different lines with a `#`
            # between them. Joining raw would leave `no\n   # explicit`, which
            # no reasonable phrase regex matches.
            window = " ".join(
                re.sub(r"^\s*#+\s*", "", ln)
                for ln in lines[max(0, lineno - 3):lineno]
            )
            for (script, args), entry in index.items():
                if not line_matches(line, script, args):
                    continue
                hits.append(
                    {
                        "skill": skill_md.parent.name,
                        "file": f".claude/skills/{skill_md.parent.name}/SKILL.md",
                        "line": lineno,
                        "script": script,
                        "args": list(args),
                        "guardrails": entry["owners"],
                        "reconciled": entry["reconciled"],
                        "likely_compliance": bool(COMPLIANCE_RE.search(window)),
                        "text": line.strip()[:200],
                    }
                )
    # Real conflicts first; compliance narration last but never dropped.
    hits.sort(key=lambda h: (h["likely_compliance"], h["skill"], h["line"]))
    return hits


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--output", choices=["human", "json"], default="human")
    ap.add_argument("--world", default="", help="override WORLD_PATH (tests)")
    ap.add_argument("--root", default="", help="override project root (tests)")
    ap.add_argument(
        "--exit-on-hits",
        action="store_true",
        help="exit 1 when NEW (non-baselined) conflicts remain",
    )
    ap.add_argument(
        "--known",
        default="",
        help=(
            "comma-separated guardrail ids whose conflicts are already triaged. "
            "Rows owned entirely by known guardrails are still REPORTED but do "
            "not count as new. This is what makes --exit-on-hits mean 'a NEW "
            "guardrail contradicts a protocol line' (the goal's actual title) "
            "rather than 'the backlog is nonzero', which would be red forever "
            "and train readers to ignore it."
        ),
    )
    ap.add_argument(
        "--include-compliance",
        action="store_true",
        help="in human output, also print the likely-compliance rows",
    )
    args = ap.parse_args(argv)

    world = Path(args.world) if args.world else _world_path()
    root = Path(args.root) if args.root else _project_root()

    guardrails = load_guardrails(world)
    index = build_index(guardrails)
    hits = scan(root, index)

    actionable = [
        h for h in hits if not h["likely_compliance"] and not h["reconciled"]
    ]
    known = {k.strip() for k in args.known.split(",") if k.strip()}
    novel = [h for h in actionable if not set(h["guardrails"]) <= known]

    if args.output == "json":
        print(
            json.dumps(
                {
                    "active_guardrails": sum(
                        1 for g in guardrails if (g.get("status") or "active") == "active"
                    ),
                    "constrained_signatures": len(index),
                    "hits": len(hits),
                    "actionable": len(actionable),
                    "novel": len(novel),
                    "known_guardrails": sorted(known),
                    "rows": hits,
                }
            )
        )
    else:
        print(
            f"[guardrail-protocol-conflict] signatures={len(index)} "
            f"hits={len(hits)} actionable={len(actionable)} novel={len(novel)}"
        )
        for h in hits:
            if h["likely_compliance"] and not args.include_compliance:
                continue
            tag = " [likely compliance]" if h["likely_compliance"] else ""
            if h["reconciled"]:
                tag += f" [reconciled: {list(h['reconciled'])}]"
            print(
                f"  {h['file']}:{h['line']}{tag}\n"
                f"      {' '.join(h['guardrails'])} constrains "
                f"{h['script']} {' '.join(h['args'])}\n"
                f"      {h['text'][:130]}"
            )
        if not hits:
            print("  no conflicts detected")

    return 1 if (args.exit_on_hits and novel) else 0


if __name__ == "__main__":
    sys.exit(main())
