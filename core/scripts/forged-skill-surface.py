#!/usr/bin/env python3
"""Surface forged skills whose triggers match a goal's text — the Phase 4 reader.

WHY THIS EXISTS (g-115-3811, sig-48). `world/forged-skills.yaml` is the registry
of already-forged capabilities, and `.claude/rules/forged-skill-resolution.md`
tells the agent to consult it before hand-rolling a procedure. `guard-1516`
states the failure verbatim: "Nothing in Phase 4 surfaces the forged registry
automatically - retrieve.py does not index it and aspirations-execute never
reads it - so this check is entirely manual." Measured twice, ~5 weeks apart,
different agents, same double-miss shape (g-335-45 missed gap-019 + gap-017;
g-335-448 missed gap-011 + gap-019).

That is pattern signature sig-48 exactly — a correct, retrievable rule that
nothing READS at the moment of the action — and its prescription is to build a
reader at the action point rather than restate the rule. sig-48 is 7/7
CONFIRMED, and its strongest recorded evidence (g-335-409) is that the working
shape is an UNCONDITIONAL step, because then nothing has to remember.

MATCHING: whole-phrase containment, triggers of >=2 words. A skill matches when
one of its trigger phrases appears CONTIGUOUSLY in the goal text, after both
sides are lowercased and all non-alphanumerics collapse to single spaces. This
is what `.claude/rules/forged-skill-resolution.md` rule 1 literally asks for —
"a forged skill whose triggers match the PHRASE" — and triggers are authored as
natural-language phrases precisely so they can be phrase-matched.

MEASURED, because the first version of this file asserted precision it did not
have. That version required all TOKENS of a trigger to appear anywhere in the
goal's keywords, reusing `gates/capability.py::_extract_keywords`. It read as
strict and was not: that tokenizer strips stopwords hard, so `'clean up S3'`
reduces to `['clean']`, `'add a runtime endpoint'` to `['runtime']`, and
`'run tests'` to `['tests']`. A whole-phrase rule over one common word is
vacuous. Swept over all 4,154 goals in the world queue:

    token-subset (v1)          68.3% of goals fired, mean 1.43 skills
    phrase containment          26.4%, mean 0.33  (one degenerate 1-word
                                trigger, 'stale', caused 732 of 1,098 hits)
    phrase + >=2 words (this)   11.4%, mean 0.16, median 0

The >=2-word floor costs no coverage: exactly two triggers are single-word
('stale', 'reap', both on scan-stale-jobs, which keeps its other seven), and
the one skill left with no matchable trigger (run-game-session) has an EMPTY
triggers list and was unreachable under every rule including v1.

An advisory that fires on two thirds of goals is one nobody reads, which would
reproduce the exact miss this file exists to prevent. A miss is cheaper here
than noise, so the floor stays.

REUSE, NOT REBUILD. The registry loader is `gates/capability.py`'s, which
already reads this same file for the capability gate. Only the MATCHER is
local — deliberately, per the measurement above: the capability gate wants
recall, this advisory wants precision.

ADVISORY ONLY: always exits 0. This must never block goal execution.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _paths import PROJECT_ROOT, WORLD_DIR  # noqa: E402

try:
    from gates.capability import _load_forged_skills  # noqa: E402
except Exception:  # pragma: no cover - import guard, advisory must not break
    _load_forged_skills = None

# Minimum words in a normalized trigger for it to be matchable. See the
# MEASURED block in the module docstring: below 2, a trigger collapses to a
# single common word and the phrase rule stops discriminating.
MIN_TRIGGER_WORDS = 2


def _norm(s) -> str:
    """Lowercase, collapse non-alphanumerics to single spaces, pad with spaces.

    The padding makes `sub in text` a word-boundary test, so 'aws cli' does not
    match inside 'flaws climate'.
    """
    return " " + re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip() + " "


_rule_phrases_cache = None


def rule_name_phrases() -> set:
    """Normalized phrases that are the NAME of a framework rule file.

    A trigger equal to a rule's basename is CITATION-PRONE, and citations
    vastly outnumber invocations. Goal prose cites rules constantly, as a
    governing constraint and often while NOT doing the thing: measured
    verbatim in live goals -- "archive before delete MD applies", "so archive
    before delete APPLIES IF any row is removed", "INVERTS archive before
    delete md", "GOVERNED BY archive before delete md and the cost of that
    protocol EXCEEDS THE HARM". Whole-phrase containment cannot tell a
    citation from a request, so such a trigger fires on the rule's own
    readership rather than on the skill's work.

    This is a property of the trigger's WORDING, not of any goal, which is
    why it belongs here rather than in a polarity parser: the fix is
    registry-shaped and self-maintaining -- any future trigger named after a
    rule is caught with no further edit.

    MEASURED 2026-07-29 (g-115-3887), 663 live goals / 35 skills / 214
    matchable triggers:
      - EXACTLY 2 triggers equal a rule basename ('archive before delete' on
        archive-aws-graveyard and archive-efs-graveyard). Zero collateral --
        the rule touches only the two known-bad triggers.
      - That one phrase produced 52 of ~100 total trigger hits; 11 of 12
        sampled match contexts were citations.
      - Excluding it: fire rate 13.1% (87/663) -> 10.0% (66/663), i.e. 21
        goals whose ONLY signal was the citation.
      - No loss of genuine reach: "graveyard this stale environment directory
        and purge the bucket of dead objects" still matches both skills via
        their action-shaped triggers.
    Consistent with this module's stated tradeoff -- "a miss is cheaper here
    than noise" -- and with guard-1828 (sweep the real corpus, quote the
    number, never assert selectivity from design intent).
    """
    global _rule_phrases_cache
    if _rule_phrases_cache is None:
        out = set()
        try:
            for p in (PROJECT_ROOT / ".claude" / "rules").glob("*.md"):
                out.add(_norm(p.stem.replace("-", " ")).strip())
        except Exception:  # advisory must never break on a missing rules dir
            out = set()
        _rule_phrases_cache = out
    return _rule_phrases_cache


def match_skills(text: str, wdir) -> list:
    """Return [{skill, matched_triggers, scripts}] for skills triggered by text.

    A skill matches when one of its >=2-word trigger phrases appears
    contiguously in the normalized goal text, EXCLUDING triggers that are the
    name of a framework rule (see rule_name_phrases -- those fire on citations).
    """
    if not text or _load_forged_skills is None:
        return []
    try:
        entries = _load_forged_skills(wdir)
    except Exception:
        return []

    ntext = _norm(text)
    rule_names = rule_name_phrases()
    out = []
    for entry in entries:
        hits = []
        for trig in entry.get("triggers") or []:
            ntrig = _norm(trig)
            if len(ntrig.split()) < MIN_TRIGGER_WORDS:
                continue
            if ntrig.strip() in rule_names:
                continue
            if ntrig in ntext:
                hits.append(str(trig))
        if hits:
            out.append({
                "skill": entry.get("skill"),
                "matched_triggers": hits,
                "scripts": entry.get("scripts") or [],
            })
    out.sort(key=lambda e: (-len(e["matched_triggers"]), str(e["skill"])))
    return out


def _goal_text(goal_id: str, source: str) -> str:
    """Best-effort goal title+description lookup. Never raises."""
    try:
        import os
        from _paths import agent_dir  # noqa
        base = WORLD_DIR if source == "world" else agent_dir(os.environ.get("MIND_AGENT", ""))
        path = Path(base) / "aspirations.jsonl"
        if not path.is_file():
            return ""
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or goal_id not in line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            for g in rec.get("goals") or []:
                if g.get("id") == goal_id:
                    return f"{g.get('title') or ''}\n{g.get('description') or ''}"
    except Exception:
        return ""
    return ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--goal", help="goal id to look up")
    ap.add_argument("--source", default="world", choices=["world", "agent"])
    ap.add_argument("--text", help="raw goal text (overrides --goal lookup)")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args(argv)

    text = args.text or (_goal_text(args.goal, args.source) if args.goal else "")

    matches = match_skills(text, WORLD_DIR)

    if args.json:
        print(json.dumps({"matches": matches, "count": len(matches)}, indent=2))
        return 0

    if not matches:
        # Silent on the common case: an advisory that prints on every goal is
        # one that stops being read.
        return 0

    print("[forged-skill-surface] ▸ ALREADY FORGED — do not hand-roll these:")
    for m in matches:
        trg = ", ".join(m["matched_triggers"][:3])
        print(f"  /{m['skill']}  (triggers: {trg})")
        for s in (m["scripts"] or [])[:3]:
            print(f"      script: {s}")
    print("[forged-skill-surface] Registry: world/forged-skills.yaml — "
          ".claude/rules/forged-skill-resolution.md rule 2: check the registry, "
          "do not reason about whether a skill 'should' exist.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
