#!/usr/bin/env python3
"""Mechanize the decidable half of fresh-eyes Phase 2.3b board-signal filtering.

Phase 2.3b of `.claude/skills/fresh-eyes-review/SKILL.md` filters findings-board
posts down to this agent's `board_signals`, and every filter in that chain was
prose an LLM had to apply by hand. Four separate near-misses have been fixed in
that step, all inflating the count in the SAME direction — toward a false
`act_later` — and the fourth (g-115-9566) was found only because it flipped a
43-pass streak. guard-399: before adding another "the LLM must do X here"
instruction, write the bash baseline that does the decidable part, and let the
LLM step be enrichment on top of it.

WHAT THIS DECIDES, and it is deliberately less than the whole chain:

  (a-pre) CADENCE-RECEIPT EXCLUSION — fully decidable. The SKILL.md publishes the
          predicate as a literal regex; it is applied here verbatim so the ritual
          can never read its own mandated receipts as external signal.
  (a0)    ROUTING-TAG ARITHMETIC — fully decidable, and delegated to
          `peer_surface.routing_tag_targets_agent` rather than re-derived, so the
          bare / `agent:` / `<name>@<env>` forms cannot disagree between here and
          the four other consumers of that predicate (g-115-4188, guard-2860).

WHAT THIS REFUSES TO DECIDE, which is the point:

  The SUBJECT TEST. A routing tag says who should READ a post; it says nothing
  about whose identity the post is EVIDENCE about, and only reading the post
  answers that. So this script does not guess. It NAMES the population that owes
  a subject verdict — every post that reached (a0)'s `directed` branch under
  another agent's authorship — and reports it as `subject_test_required`. A
  skipped subject test is then a visibly un-adjudicated list rather than a silent
  +1 on `self_evolution_signals_count`.

  (a1)/(b) likewise stay with the reader: they are reachable only for posts with
  NO agent tag, and those arrive here as `untagged`.

`board_signals_upper_bound` is `len(directed)` — the count when EVERY subject
verdict comes back "about me". It is an upper bound, never the answer, and is
named that way so it cannot be pasted into a signals envelope as though it were.

Usage:
  board-read.sh --channel findings --since 30d --unread-only --json \
    | py -3 core/scripts/board-signal-classify.py --agent alpha
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _stdio import reconfigure_stdio  # noqa: E402
import peer_surface as ps  # noqa: E402

reconfigure_stdio()

# Verbatim from fresh-eyes-review/SKILL.md Phase 2.3b (a-pre). Anchored on the
# OPENING TOKEN, not on a suffixed form: the bare `Fresh-eyes N=<k>` shape
# carries no suffix at all, so a suffix-requiring pattern misses most of what
# this exists to catch. The `correction…` alternative and the leading `⚠` are
# load-bearing — a correction to a ritual post is a second receipt for one fire.
RECEIPT_RE = re.compile(
    r"^\s*(?:⚠\s*)?(?:fresh[- ]eyes\b|sq-012\s+tentative\b|n=\d+\b"
    r"|correction\b[^\n]{0,80}?(?:fresh[- ]eyes|n=\d+))",
    re.IGNORECASE,
)

SIGNAL_TAGS = ("self_evolution", "self-drift")


def _roster():
    """Local agent names, via the agent-dir helper (never a hardcoded join)."""
    try:
        from _paths import enumerate_agent_confs
    except ImportError:
        return []
    return sorted({os.path.basename(os.path.dirname(str(c)))
                   for c in enumerate_agent_confs()})


def classify(rows, agent, self_env, roster):
    roster_ci = {a.lower() for a in roster}
    out = {
        "agent": agent,
        "self_env": self_env,
        "roster": sorted(roster),
        "total": len(rows),
        # A COUNT, not a list: on a live 30d window this bucket is thousands of
        # ids and nothing downstream adjudicates them, so listing them buried the
        # five buckets that matter under 225 KB of noise (measured on this box
        # before the field was narrowed). Every OTHER bucket names its ids,
        # because every other bucket is either an audit trail or a work queue.
        "not_a_signal_tag": 0,
        "receipts_dropped": [],
        "excluded_other_agents_signal": [],
        "untagged": [],
        "directed": [],
        "subject_test_required": [],
    }
    for row in rows:
        rid = row.get("id")
        tags = [str(t) for t in (row.get("tags") or [])]
        # The body key is `text`. Reading `content`/`body`/`message` yields ""
        # for every record and silently disables (a-pre) (measured: 28 receipts
        # dropped on `text`, 0 on the wrong key, with no error either way).
        text = str(row.get("text") or "")
        author, _ = ps.split_author(row.get("author"))

        if not any(t in tags for t in SIGNAL_TAGS):
            out["not_a_signal_tag"] += 1
            continue
        if RECEIPT_RE.match(text):
            out["receipts_dropped"].append(rid)
            continue

        directed = any(ps.routing_tag_targets_agent(t, agent, self_env)
                       for t in tags)
        # A tag NAMES AN AGENT only when it parses to a roster name AND is not
        # qualified to a peer deployment: `<name>@<peer-env>` is that peer's
        # same-named agent, neither this agent nor a local partner.
        names_an_agent = False
        for t in tags:
            who, env = ps.parse_routing_tag(t)
            if not who or who.lower() not in roster_ci:
                continue
            if env and self_env and env != self_env:
                continue
            names_an_agent = True
            break

        if not names_an_agent:
            out["untagged"].append(rid)
            continue
        if not directed:
            out["excluded_other_agents_signal"].append(rid)
            continue

        needs_subject = author.lower() != agent.lower()
        out["directed"].append({
            "id": rid,
            "author": author,
            "tags": tags,
            "subject_test_required": needs_subject,
        })
        if needs_subject:
            out["subject_test_required"].append(rid)

    out["board_signals_upper_bound"] = len(out["directed"])
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--agent", default=os.environ.get("MIND_AGENT", ""),
                    help="the reading agent (default: $MIND_AGENT)")
    ap.add_argument("--self-env", default=None,
                    help="this deployment's ENVIRONMENT_ID (default: resolved)")
    args = ap.parse_args()

    if not args.agent:
        print("board-signal-classify: --agent is required (or set MIND_AGENT)",
              file=sys.stderr)
        return 2

    self_env = args.self_env
    if self_env is None:
        try:
            from _paths import ENVIRONMENT_ID
            self_env = ENVIRONMENT_ID
        except Exception:
            self_env = None

    result = classify(ps.parse_jsonl(sys.stdin), args.agent, self_env, _roster())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
