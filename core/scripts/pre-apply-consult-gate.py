#!/usr/bin/env python3
"""Pre-apply consult gate — force retrieve.sh consult before Phase-4 edits
when executing an inherited cross-agent spec that touches framework files.

Origin: g-115-826 (asp-115). Incident shape: g-115-796 / rb-987.

g-115-796 was an Apply of zeta's spec g-115-775. Phase-4 of aspirations-
execute skipped the code-review-protocol step-4 pre-apply retrieve.sh
consult; the corrupt spec ("empty body -> exec [] + stderr-only") shipped
in commit 8be45e1 pinned by 9/9 passing tests. The guard-383 violation
was caught retroactively by aspirations-learning-gate's retrieval-audit
safety net (fixed at 28a3b7a). A test suite pinning the spec pins the
violation too -- 9/9 green doesn't falsify a guard miss.

This gate is the post-hoc -> pre-hoc move: shift the consult-before-edit
discipline from after-the-fact audit to before-the-fact directive.

THIS DOCSTRING IS THE PREDICATE SSOT. Five other files describe this gate
(aspirations-execute/SKILL.md, execute-protocol-digest.md, aspirations-precheck/
SKILL.md, pre-apply-consult-gate.sh, pre-apply-consult-drift-gate.py). All five
carried the PRE-g-115-2201 predicate for 17 days after the code changed, and a
reader believed them over the code: g-115-4358 was filed HIGH to widen an
already-widened gate, and its "proof" was a hand-run with MIND_AGENT unset (the
fail-open path at main(), 0 bytes of stdout) read as "the gate is silent on
self-filed goals". When this predicate changes, re-sync all five. (g-115-4358)

Trigger conditions (g-115-2201 WIDENED authorship; g-115-4358 added 1c):
  1. the goal references framework code, by ANY of:
     a. a framework-file PATH in title/description: core/scripts/, core/config/,
        mind_api/src/, core/githooks/, core/logs/, .claude/skills/,
        .claude/rules/, .claude/settings, world/conventions/, SKILL.md, CLAUDE.md
     b. a framework `category` (framework-maintenance, framework-architecture)
     c. a BARE filename in title/description that resolves to a real file under
        core/scripts/, core/scripts/gates/, core/config/, or mind_api/src/
        ("deploy-verify.sh"). Measured: 1a+1b alone missed 19.9% of goals that
        actually edited framework files -- see BARE_FILENAME_RE for the numbers.
  2. no retrieval has ALREADY been recorded for this goal
     (retrieval-session.json -- the same artifact the learning gate audits)
  3. agent + goal record can be resolved

  `handoff_from` is NO LONGER a trigger condition. It is now an ESCALATOR: an
  inherited spec makes the banner louder (it carries the extra rb-987 hazard that
  a test suite pinning the spec pins its violation too), but an OWN-AUTHORED
  framework goal fires the gate just the same.

WHAT CHANGED AND WHY (g-115-2201)
  This gate used to `return 0` unless the goal was an inherited cross-agent spec.
  That scoped it to its originating incident and left the COMMON case entirely
  uncovered: an agent skipping the consult on its OWN framework goals.

  Measured (zeta, 2026-07-14): FOUR consecutive deep framework goals closed with
  `retrieval-summary: performed=false` (g-115-2194, g-115-2195, g-115-2179,
  g-115-2202). All four had handoff_from=None, so this gate was SILENT on every
  one of them. The 4/4 miss rate was never evidence that "an advisory doesn't
  work" -- the advisory never ran.

  Cost of the gap, measured: guard-1077 was written at 17:25 and the very incident
  it describes was then re-derived from scratch by an hour of git archaeology at
  20:42; a duplicate guardrail (guard-1089) was created and had to be retired,
  because the Phase-6.5 anti-duplication check ALSO depends on retrieving first.
  A guardrail only works if it is RETRIEVED.

  The suppression in (2) is what keeps the widening from becoming banner-fatigue:
  a gate that fires even when satisfied is one the agent learns to ignore -- the
  same habituation that let 8 red tests be waved through for days (guard-1090).
  A gate must be silent when satisfied, or it stops being a signal.

Posture: ADVISORY-LOUD (large banner to stdout) but NOT loop-blocking
(always exits 0). Fail-open on parse/path/env errors with a stderr note.
The LLM may proceed without retrieval and own the risk, but cannot later
claim the directive wasn't visible.

Usage:
  pre-apply-consult-gate.py <goal-id> [--queue-file PATH]

  --queue-file: testing-only override. Reads goal record from the given
                JSONL instead of the resolved world/agent queues.

Exit: always 0.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

sys.path.insert(0, str(SCRIPT_DIR))
from _paths import agent_dir as _agent_dir  # noqa: E402


# Substring needles checked case-insensitively against title + description.
# Substrings (not regex) keep the check robust against weird quoting in
# free-form goal descriptions. False positives only cost the LLM a 5-second
# retrieve.sh consultation; false negatives are the failure mode we are
# guarding against, so prefer over- to under-fire.
FRAMEWORK_FILE_NEEDLES = (
    "core/scripts/",
    "core/config/",
    "mind_api/src/",
    "core/githooks/",
    "core/logs/",
    ".claude/skills/",
    ".claude/rules/",
    ".claude/settings",
    "world/conventions/",
    "skill.md",
    "claude.md",
)

# Structured second trigger (). The needle scan above reads goal PROSE and
# therefore misses any goal that names its files bare rather than by full path --
#  ("session_artifacts_count.py", "productivity-stop-gate.sh") did exactly
# that and slipped through. `category` is a field on the goal record, so it holds
# regardless of how the author phrased the description.
FRAMEWORK_CATEGORIES = frozenset({
    "framework-maintenance",
    "framework-architecture",
})

# Third trigger (). The category field above was added to catch goals that
# name files bare rather than by path -- and MEASURED, it only partly does: over 1624
# completed goals scored against git ground truth (did the goal's commits actually
# touch a framework file?), the needle+category predicate still MISSED 103 of 518
# framework-editing goals, a 19.9% miss rate. Every miss looked the same: the goal
# names its file bare ("deploy-verify.sh", "fixture-leak-scan.py") and sits in a
# non-framework category, so neither existing trigger sees it.
#
# The fix resolves the bare name against the FILESYSTEM instead of guessing from
# prose shape -- a name only counts when a real framework file answers to it. Scored
# on the same ground truth before being applied (this gate's own goal was filed on an
# unmeasured premise, so measuring the remedy is the point, not ceremony):
#
#   current (needles + category)          FP 39.5%  recall 80.1%
#   + bare name that EXISTS on disk       FP 42.3%  recall 88.6%   <- applied
#   + ANY bare .sh/.py/.yaml mention      FP 54.9%  recall 92.7%   <- rejected
#
# The applied form closes 44 misses for 66 new banners (~1.5 banners per miss). That
# trade is favorable because the two sides are not symmetric: a new banner costs a
# 5-second retrieve.sh, while a miss ships a framework edit with no consult at all --
# the rb-987 / guard-383 shape. The rejected form buys 21 further misses for 313 more
# banners, which is the banner-fatigue that makes a gate ignorable (guard-1090).
# Most of the 66 are goals like "Investigate: owncloud_sync.py symmetry audit" that
# never edited a file but where consulting first was genuinely useful -- they are
# false only against the deliberately-conservative "did it EDIT one" ground truth.
BARE_FILENAME_RE = re.compile(r"\b([a-z0-9][a-z0-9._-]*\.(?:sh|py|yaml))\b")

# Names so generic that a match is evidence of nothing.
BARE_FILENAME_DENY = frozenset({
    "__init__.py", "conftest.py", "readme.md", "utils.py",
})


def _bare_search_dirs():
    """Directories a bare filename may resolve against to count as framework."""
    return (
        CORE_ROOT / "scripts",
        CORE_ROOT / "scripts" / "gates",
        CORE_ROOT / "config",
        PROJECT_ROOT / "mind_api" / "src",
    )


def _detect_bare_framework_files(title: str, description: str):
    """Bare filenames in the prose that resolve to a REAL framework file.

    Resolution against disk is what keeps this from degrading into candidate C:
    "deploy-verify.sh" fires because core/scripts/deploy-verify.sh exists;
    "package.json" or a product-repo script named in passing does not.
    """
    text = (title + "\n" + description).lower()
    dirs = _bare_search_dirs()
    hits = []
    # dict.fromkeys de-dupes while preserving first-seen order.
    for name in dict.fromkeys(BARE_FILENAME_RE.findall(text)):
        if name in BARE_FILENAME_DENY:
            continue
        for d in dirs:
            try:
                if (d / name).is_file():
                    hits.append(name)
                    break
            except OSError:
                # Unreadable dir -> just skip it. Fail-open matches the gate's
                # posture: a missed needle costs a banner, never a wedge.
                continue
    return hits


def _read_local_paths_conf(agent_name: str) -> dict:
    conf = _agent_dir(agent_name) / "local-paths.conf"
    out: dict = {}
    if not conf.is_file():
        return out
    try:
        for line in conf.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        return {}
    return out


def _resolve_queues(agent: str):
    """Return list of JSONL paths to scan (world first, then agent)."""
    paths = []
    conf = _read_local_paths_conf(agent)
    wp = conf.get("WORLD_PATH")
    if wp:
        p = Path(wp) / "aspirations.jsonl"
        if p.is_file():
            paths.append(p)
    agent_jsonl = _agent_dir(agent) / "aspirations.jsonl"
    if agent_jsonl.is_file():
        paths.append(agent_jsonl)
    return paths


def _find_goal(goal_id: str, jsonl: Path):
    """Scan a JSONL queue for the goal record. Returns the goal dict or None."""
    try:
        with jsonl.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    asp = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                goals = asp.get("goals", []) or []
                for g in goals:
                    if isinstance(g, dict) and g.get("id") == goal_id:
                        return g
    except OSError:
        return None
    return None


def _detect_framework_refs(title: str, description: str):
    """Case-insensitive substring scan; returns ordered list of matched needles."""
    text = (title + "\n" + description).lower()
    hits = []
    for needle in FRAMEWORK_FILE_NEEDLES:
        if needle in text:
            hits.append(needle)
    return hits


def _consult_already_done(agent: str, goal_id: str) -> bool:
    """True when a retrieval was already recorded FOR THIS GOAL ().

    `retrieval-session.json` is the same artifact iteration-close.sh's learning gate
    reads to emit `retrieval-summary: performed=<bool>` — so the gate that ASKS for the
    consult and the audit that MEASURES it now agree on one source of truth, instead of
    drifting apart.

    Fail-open: any error -> False -> the banner fires. An extra banner costs a 5-second
    retrieve.sh; a missed one costs an hour of re-deriving a lesson you already wrote
    (measured, g-115-2179).
    """
    try:
        p = _agent_dir(agent) / "session" / "retrieval-session.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        if d.get("goal_id") != goal_id:
            return False
        # `retrieval_performed` is NOT set to True by the real retrieve.sh path --
        # a genuine `retrieve.sh --goal <id>` records goal_id + counts and leaves this
        # field ABSENT (None). Only iteration-close.sh's no-retrieval STUB writes it
        # explicitly as False. So `bool(retrieval_performed)` -- the obvious check --
        # rejects every real consultation and accepts none: the banner would keep
        # nagging an agent that had just obeyed it, which is precisely the
        # banner-fatigue this suppression exists to prevent (guard-1090).
        #
        # Credit on goal_id match (the same signal iteration-close.sh:1398 uses), and
        # exclude ONLY the explicit-False stub. A real consult that happened to match
        # nothing still counts -- the agent DID consult; the store was just empty.
        return d.get("retrieval_performed") is not False
    except (OSError, ValueError, TypeError):
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Pre-apply consult gate (advisory, fail-open).",
        add_help=True,
    )
    ap.add_argument("goal_id", help="Goal ID to evaluate.")
    ap.add_argument("--queue-file",
                    help="Testing-only: read goal records from this JSONL "
                         "instead of the resolved world/agent queues.")
    args = ap.parse_args(argv)

    goal_id = (args.goal_id or "").strip()
    if not goal_id:
        return 0

    agent = os.environ.get("MIND_AGENT", "").strip()
    if not agent:
        print("[pre-apply-consult-gate] WARN: MIND_AGENT unset -- failing open",
              file=sys.stderr)
        return 0

    if args.queue_file:
        qpath = Path(args.queue_file)
        queues = [qpath] if qpath.is_file() else []
        if not queues:
            print(f"[pre-apply-consult-gate] WARN: --queue-file not found: "
                  f"{args.queue_file} -- failing open", file=sys.stderr)
            return 0
    else:
        queues = _resolve_queues(agent)

    goal = None
    for q in queues:
        goal = _find_goal(goal_id, q)
        if goal is not None:
            break

    if goal is None:
        # Goal record not found -- fail open silently. Most likely a queue
        # source mismatch or the daemon hasn't flushed the record yet;
        # Phase 4's `aspirations-update-goal.sh ... status in-progress`
        # already landed the record, so this case is rare.
        return 0

    #  — WIDENED. This used to `return 0` unless the goal was an INHERITED
    # cross-agent spec (handoff_from set and != agent). That scoped the gate to its
    # originating incident (, an Apply of another agent's spec) and left the
    # COMMON case completely uncovered: an agent skipping the consult on its OWN
    # framework goals.
    #
    # Measured (zeta, 2026-07-14): FOUR consecutive deep framework goals closed with
    # `retrieval-summary: performed=false` — , , ,
    # . All four have handoff_from=None, so this gate was SILENT on every
    # one. The 4/4 miss rate was never evidence that "the advisory doesn't work"; the
    # advisory never ran. Cost of the gap, measured: guard-1077 was written at 17:25
    # and the exact incident it describes was then re-derived from scratch by an hour
    # of git archaeology at 20:42, plus a duplicate guardrail (guard-1089) that had to
    # be retired — because the Phase-6.5 anti-duplication check ALSO depends on
    # retrieving first.
    #
    # The hazard does not care who authored the spec. Self-authored is arguably WORSE:
    # there is no second pair of eyes anywhere in the loop. So handoff is now an
    # ESCALATOR (it makes the banner louder), never a GATE.
    handoff_from = (goal.get("handoff_from") or "").strip()
    inherited = bool(handoff_from) and handoff_from != agent

    title = (goal.get("title") or "").strip()
    description = (goal.get("description") or "").strip()
    hits = _detect_framework_refs(title, description)

    #  — the needle scan reads the goal's PROSE, which is fragile: a goal
    # that names its files bare ("session_artifacts_count.py", "productivity-stop-
    # gate.sh") rather than by full path matches NOTHING. That is not hypothetical --
    #  was written exactly that way and slipped through the widened gate on
    # its first positive-control run. `category` is a STRUCTURED field on the goal
    # record, so it does not depend on how the author happened to phrase the prose.
    category = (goal.get("category") or "").strip().lower()
    if category in FRAMEWORK_CATEGORIES and "category" not in hits:
        hits = hits + [f"category:{category}"]

    #  — third trigger. The category field above was supposed to cover the
    # bare-filename shape; measured over 1624 goals it left a 19.9% miss rate, every
    # miss a goal naming its file bare in a non-framework category. Resolve bare names
    # against disk. See the BARE_FILENAME_RE block for the scored trade-off.
    for name in _detect_bare_framework_files(title, description):
        tag = f"file:{name}"
        if tag not in hits:
            hits = hits + [tag]

    if not hits:
        # No framework-file reference and not a framework category -- the consult is
        # not required. This is the condition that keeps the gate from becoming a tax
        # on every goal.
        return 0

    # Suppress if the consult ALREADY happened for THIS goal. Without this, the gate
    # would fire on every framework goal even when the agent did the right thing, and
    # a banner that fires unconditionally is one the agent learns to ignore — the
    # exact habituation dynamic that let 8 red tests be dismissed for days
    # (guard-1090). A gate must be silent when satisfied, or it stops being a signal.
    if _consult_already_done(agent, goal_id):
        return 0

    one_line = title.split("\n")[0]
    if len(one_line) > 120:
        one_line = one_line[:117] + "..."

    refs = ", ".join(sorted(set(hits)))

    out = sys.stdout
    out.write("\n")
    out.write("=== PRE-APPLY CONSULT GATE ===========================================\n")
    out.write(f"Goal:        {goal_id}\n")
    out.write(f"Title:       {one_line}\n")
    if inherited:
        # Escalator, not a gate (). An inherited spec carries the extra
        # hazard of rb-987: a test suite that pins the spec pins its violation too.
        out.write(f"INHERITED:   handoff_from={handoff_from} (current agent: {agent})\n")
        out.write("             ^ an inherited spec may CONTRADICT a guardrail you\n")
        out.write("               already hold. This is the rb-987 / g-115-796 shape.\n")
    else:
        out.write(f"Authored by: {agent} (own goal — the consult is NOT optional here;\n")
        out.write("             a self-authored framework fix has no second reader\n")
        out.write("             anywhere in the loop, so retrieval IS the review)\n")
    out.write(f"Refs:        {refs}\n")
    out.write("\n")
    out.write("Per .claude/rules/code-review-protocol.md step 4 + rb-987 (g-115-796\n")
    out.write("incident): BEFORE first Edit, run retrieve.sh to surface reasoning-bank\n")
    out.write("entries and guardrails that may CONTRADICT the inherited spec. A test\n")
    out.write("suite pinning the spec pins the violation too -- 9/9 passing tests do\n")
    out.write("NOT falsify a guard violation.\n")
    out.write("\n")
    out.write("Recommended invocation:\n")
    # --goal is LOAD-BEARING (). Without it, retrieve.sh does the retrieval
    # but never records it against THIS goal, so:
    #   * iteration-close.sh:1398 (which credits on goal_id match) writes a
    #     `performed=false` stub -- the consult you actually did is logged as a MISS;
    #   * this gate's own suppression never fires, so it nags you again next time;
    #   * any measurement of the consult miss-rate counts every consult as a failure.
    # The gate would have been recommending a command that could not satisfy the audit
    # measuring it. Keep --goal.
    # --include-framework is equally LOAD-BEARING (, measured 2026-07-29).
    # This gate fires on FRAMEWORK-file fixes, and without the flag retrieve.sh omits
    # the `framework_rules` key entirely -- not empty, ABSENT -- so .claude/rules/* and
    # core/config/conventions/* never reach the consult. That is precisely the corpus
    # most likely to already prescribe the fix being reviewed. Measured on "fix the
    # drain-temp purge glob so cited evidence files are not deleted": bare call = no
    # framework key; with the flag = temp-store.md, the governing convention.
    out.write(f'  bash core/scripts/retrieve.sh --category "{one_line[:80]}" \\\n')
    out.write(f'       --goal {goal_id} --depth shallow --include-framework\n')
    out.write("\n")
    out.write("Then read returned reasoning_bank + guardrails + framework_rules. If any CONTRADICTS the\n")
    out.write("intended fix: STOP -- re-evaluate. Apply the entry's pattern instead,\n")
    out.write("OR retire the entry with justification if genuinely stale. If an entry\n")
    out.write("REINFORCES the fix, proceed and increment utilization on the entry.\n")
    out.write("\n")
    out.write("Posture: advisory, not loop-blocking. Fail-open. LLM owns the risk.\n")
    out.write("======================================================================\n")
    out.write("\n")
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
