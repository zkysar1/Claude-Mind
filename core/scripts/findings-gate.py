#!/usr/bin/env python3
"""Phase 8.5 Actionable Findings Gate — bash-enforced keyword scan + dedup + goal creation.

Replaces the LLM-assembled keyword scan described in aspirations-state-update/SKILL.md
Step 8.5 with a single script call. The LLM residue that remains is ONLY the
investigation-override binary: when goal.title starts with "Investigate:" AND the
keyword scan produced zero signals, the LLM decides whether the finding is
informational (skip) or action-requiring (pass --investigation-needs-action).

Exit codes:
  0  — ran the gate (signals detected and dispatched, OR none detected); stdout
       includes one line per signal and a trailing `findings_count=N created=M`.
  2  — reserved for future use (no current path returns 2).
  1  — usage / argument error.

The `findings_count` line is stable machine-parseable output for downstream
audits (state-update-audit.sh run-all reads it).
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Unicode-safe stdout/stderr on Windows (cp1252).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# We need PROJECT_ROOT + AGENT_DIR to find aspirations-compact.json. Reuse
# the same _paths shim used across the rest of core/scripts/.
sys.path.insert(0, str(Path(__file__).parent))
from _paths import PROJECT_ROOT, AGENT_DIR, WORLD_DIR  # noqa: E402
from _gate_log import log as _gate_log  # noqa: E402  # gate-firing telemetry ()
from _runtime_bash import bash_cmd  # noqa: E402  # : Windows-safe bash resolution

RESOLUTION_SUPPRESSION_CHARS = 50  # window after a match to check for resolution language

# Negation disqualifier (). A trigger keyword sitting inside a NEGATED
# clause describes something that is explicitly NOT a finding. The fleet's
# standard exoneration idiom is "NOT caused by <goal-id>" — the way an agent
# records that a failure is pre-existing rather than from its own change.
#
# DELIBERATELY REQUIRES THE NEGATION TO BE THE IMMEDIATELY PRECEDING TOKEN.
# Measured over 13,486 real prose items (goal descriptions + experience files,
# 774 root_cause trigger matches): this form suppresses 53, and every one of the
# 53 was inspected individually and is a genuine negation. A 30-char window
# suppresses 74 — 21 more — and several of those extra 21 are GENUINE findings
# whose "not" negates a different verb ("Do NOT de-dupe before the root cause is
# fixed", "WHY I DIDN'T USE IT — the root cause, and it is mine").
#
# Under-reaching is the safe direction here (guard-958): a missed suppression
# costs one visible, skippable goal, while an over-suppression silently drops a
# real finding. Do not widen this to a character window without re-running that
# sample — the wide form was measured unsafe, not merely suspected.
NEGATION_RE = re.compile(r"\b(not|never|no|without|nor|neither)\s+$", re.IGNORECASE)

# Lines that carry no prose and therefore cannot name a finding.
_MARKDOWN_RULE_RE = re.compile(r"^[-=_*]{3,}$")


def _first_prose_fragment(text, limit=50):
    """First prose line of `text`, truncated to `limit`, or "" if none exists.

    The investigation-override path used to slice `text[:limit]` raw. On an
    insight that opens with a markdown heading that produced a title containing
    the heading marker AND two embedded newlines — g-335-709 was filed as
    "Unblock: Fix # g-335-536 - findings\\n\\nResolved hypoth...". Skip blank
    lines, headings and horizontal rules; the first real prose line names the
    finding. Bullets are deliberately KEPT: a findings list written entirely as
    bullets is prose-bearing, and skipping them would silently discard the
    caller's deliberate --investigation-needs-action signal.
    """
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#") or _MARKDOWN_RULE_RE.match(stripped):
            continue
        return stripped[:limit].rstrip() + ("…" if len(stripped) > limit else "")
    return ""


def _sanitize_fragment(fragment):
    """Collapse all whitespace in a title fragment to single spaces.

    Shared-surface invariant: no goal title may carry embedded newlines,
    whatever signal path produced it. Both the keyword scan and the
    investigation override converge on make_child_goal, so this is enforced
    there rather than at each producer.
    """
    return re.sub(r"\s+", " ", fragment or "").strip()


# Leading labels a deferred_idea match tends to open with. Stripping them yields
# a title that reads "Idea: create 3 feature goals" instead of the stutter
# "Idea: RECOMMENDATION: create 3 feature goals". No-op for the other four
# patterns (their matches never open with these labels).
_LEADING_LABEL_RE = re.compile(
    r"^(recommendations?|follow[- ]?ups?|next steps?|future work|todo)\s*:\s*",
    re.IGNORECASE,
)


def _strip_leading_label(reference):
    """Drop a leading 'RECOMMENDATION:'/'follow-up:'/'todo:' label from a title
    reference. Applied to every signal's reference — harmless where absent."""
    return _LEADING_LABEL_RE.sub("", reference or "").strip()

# Five structural signal patterns. Each is (name, match_pattern, resolution_filter_pattern).
# match_pattern is compiled case-insensitive. resolution_filter_pattern is also
# case-insensitive and checked in the RESOLUTION_SUPPRESSION_CHARS window
# immediately after the match.
SIGNAL_PATTERNS = [
    (
        "root_cause",
        re.compile(
            r"\b(root cause|caused by|due to|because of|stems from)\b[^.!?\n]{0,200}",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(fixed|resolved|applied|addressed|patched|corrected|updated|removed)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "bug_identified",
        re.compile(
            r"\b(bug|defect|mismatch|incorrect|wrong|broken)\b\s+(in|at|of)\b[^.!?\n]{0,200}",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(fixed|resolved|patched|corrected)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "proposed_fix",
        re.compile(
            r"\b(fix by|should be changed|needs to be|replace with|update to)\b[^.!?\n]{0,200}",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(done|completed|applied|implemented|changed|updated)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "unimplemented_action",
        re.compile(
            r"\b(needs|requires|must|should)\s+(to be|updating|fixing|adding|removing)\b[^.!?\n]{0,200}",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(done|completed|applied|implemented|resolved)\b",
            re.IGNORECASE,
        ),
    ),
    (
        # deferred_idea (): the "here is future work to file" idiom —
        # an agent's OWN end-of-goal recommendation. The other four patterns all
        # miss it because they name a defect in the PRESENT (root cause / bug /
        # proposed fix / unimplemented action), whereas this names work to
        # capture as a NEW goal: "RECOMMENDATION: create 3 feature goals",
        # "follow-up: ...", "next steps: ...", "worth filing". This is the whole
        # point of the gate for the idea-capture lane (scan-outcome-note): the
        # recommendation an agent writes into outcome_notes and then forgets.
        #
        # Idea MEDIUM, never HIGH (make_child_goal: not in high_signal_types) —
        # a recommendation is a lead, not a confirmed bug.
        #
        # DELIBERATELY ANCHORED on an explicit label-colon, a "recommend +
        # gerund/that", an explicit "file/create/open/add ... goal(s)" verb
        # phrase, or "worth <verb>" — NOT a bare "should"/"could", which fire on
        # ordinary closing prose ("should be fine now"). `goals?(?![\w-])` keeps
        # "create a goal-selection cache" (goal-hyphen) out of the verb branch.
        # Under-reach is the safe direction here exactly as for the four above:
        # a missed recommendation costs a re-scan next close; an over-match costs
        # one visible, skippable Idea goal. Calibrated against the live
        # completed-goal corpus before promotion ( census).
        "deferred_idea",
        re.compile(
            r"(?:"
            r"\b(?:recommendations?|follow[- ]?ups?|next steps?|future work|todo)\s*:"
            r"|\brecommend(?:s|ed)?\s+(?:creating|filing|adding|opening|building|that\b)"
            # verb + REQUIRED article/number: "file a goal", "create 3+ goals".
            # The article/number is what separates a filing INTENT from the
            # adjectival "open goals"/"OPEN HIGH goals" (= pending goals) that
            # dominated the false positives in the  census. "the" is
            # deliberately excluded — "open the goal" usually references an
            # existing goal, not a new filing.
            r"|\b(?:file|create|open|add)\s+(?:an?|another|\d+\+?)\s+(?:new )?"
            r"(?:[\w-]+ ){0,3}goals?(?![\w-])"
            r"|\bworth\s+(?:a goal|filing|investigating|creating|doing)\b"
            r")[^.!?\n]{0,200}",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(already (?:filed|created|done|tracked)|filed as g-|"
            r"created goal g-|tracked (?:in|as) g-|no action needed)\b|→\s*g-\d",
            re.IGNORECASE,
        ),
    ),
]


def scan_signals(insight_text):
    """Return list of {type, match} dicts for signals present in insight_text.

    Each signal type is emitted at most once — the first non-suppressed match
    wins, then we move to the next pattern.
    """
    signals = []
    for name, match_re, resolution_re in SIGNAL_PATTERNS:
        for m in match_re.finditer(insight_text):
            # Negation disqualifier (). Look at the text immediately
            # before the match, confined to the CURRENT sentence — a negation in
            # a previous sentence says nothing about this clause. Runs before the
            # resolution filters because "is this even a claim?" is logically
            # prior to "was this claim already resolved?".
            sentence_head = re.split(r"[.!?\n]", insight_text[:m.start()])[-1]
            if NEGATION_RE.search(sentence_head):
                _gate_log(
                    "findings-gate",
                    "noop",
                    trigger_matched=f"{name}:negated",
                    caller="findings-gate.py:scan_signals",
                    extra={"decision_path": "negation-suppression"},
                )
                continue
            # DO NOT SIMPLIFY: the resolution-language filter MUST check
            # BOTH the match content AND the 50-char window after. Checking
            # only the window misses resolution language inside the greedy
            # [^.!?\n]{0,200} span (e.g., "root cause was X, fixed by Y" —
            # "fixed" lives inside the match, not after it). This was the
            # bug fixed during initial wrapper implementation. If you
            # tighten the match regex to non-greedy, re-verify the dual
            # check is still needed before removing.
            match_text = m.group(0)
            if resolution_re.search(match_text):
                # Resolution language INSIDE the match span — pattern fired but
                # is already-resolved. Telemetry: noop with decision_path so the
                # retirement evaluator can distinguish suppressed-by-resolution
                # from never-matched ().
                _gate_log(
                    "findings-gate",
                    "noop",
                    trigger_matched=f"{name}:resolution-in-match",
                    caller="findings-gate.py:scan_signals",
                    extra={"decision_path": "resolution-suppression-in-match"},
                )
                continue
            window = insight_text[m.end():m.end() + RESOLUTION_SUPPRESSION_CHARS]
            if resolution_re.search(window):
                _gate_log(
                    "findings-gate",
                    "noop",
                    trigger_matched=f"{name}:resolution-in-window",
                    caller="findings-gate.py:scan_signals",
                    extra={"decision_path": "resolution-suppression-in-window"},
                )
                continue
            # Extract a 50-char reference for the goal title.
            reference = _strip_leading_label(match_text.strip())
            # Degenerate-match guard (). A match that is ONLY a label —
            # "next step:" at end of line, its actionable text on the NEXT line
            # that the [^.!?\n] span cannot reach — strips to empty/near-empty
            # and would title a goal "Idea: " with no content. Require real
            # substance before emitting. Under-reach is the safe direction (the
            # four other patterns never produce a tiny match, so this is a no-op
            # for them; the newline-separated recommendation is simply re-scanned
            # next close if its content ever lands on the label line).
            if len(re.sub(r"[^A-Za-z0-9]", "", reference)) < 6:
                _gate_log(
                    "findings-gate",
                    "noop",
                    trigger_matched=f"{name}:degenerate",
                    caller="findings-gate.py:scan_signals",
                    extra={"decision_path": "degenerate-match"},
                )
                continue
            if len(reference) > 50:
                reference = reference[:50].rstrip() + "…"
            signals.append({"type": name, "match": reference})
            # Pattern matched + resolution filter passed → this becomes a
            # child goal. "block" in telemetry semantics = gate fired and
            # caller acted on it ().
            _gate_log(
                "findings-gate",
                "block",
                trigger_matched=name,
                caller="findings-gate.py:scan_signals",
                extra={"decision_path": "signal-found", "match_ref": reference},
            )
            break
    return signals


def _title_similar(a, b):
    """Rough similarity test for dedup. Titles match if one contains the other's
    core content (case-insensitive substring test on the match text)."""
    a_low = a.lower().strip()
    b_low = b.lower().strip()
    if not a_low or not b_low:
        return False
    if a_low == b_low:
        return True
    # If either is a substring of the other (minus the leading "Unblock: "/"Idea: "),
    # treat as duplicate.
    def strip_prefix(s):
        for pfx in ("unblock: ", "idea: ", "investigate: "):
            if s.startswith(pfx):
                return s[len(pfx):]
        return s
    a_core = strip_prefix(a_low)
    b_core = strip_prefix(b_low)
    if not a_core or not b_core:
        return False
    # Token-overlap dedup: >70% of tokens shared counts as duplicate
    a_tok = set(re.findall(r"[a-z0-9]+", a_core))
    b_tok = set(re.findall(r"[a-z0-9]+", b_core))
    if not a_tok or not b_tok:
        return False
    overlap = len(a_tok & b_tok) / max(len(a_tok), len(b_tok))
    return overlap >= 0.70


def load_dedup_titles(aspiration_id):
    """Build the dedup title set per SKILL.md Step 8.5:
      - All aspirations: goals with status pending/in-progress
      - Parent aspiration only: goals with status completed (sibling completed)
    Reads aspirations-compact.json via load-aspirations-compact.sh to get a fresh snapshot.
    """
    if AGENT_DIR is None:
        return []
    compact_path = AGENT_DIR / "session" / "aspirations-compact.json"
    # Regenerate compact if loader is available (best-effort; scanner still works
    # on stale data if the loader fails).
    loader = PROJECT_ROOT / "core" / "scripts" / "load-aspirations-compact.sh"
    if loader.exists():
        try:
            subprocess.run(
                bash_cmd(loader),
                env={**os.environ, "MIND_AGENT": os.environ.get("MIND_AGENT", "")},
                capture_output=True,
                timeout=10,
                check=False,
            )
        except Exception:
            pass
    if not compact_path.exists():
        return []
    try:
        with open(compact_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    # compact may be a list of aspirations OR a dict — handle both shapes.
    aspirations = data if isinstance(data, list) else data.get("aspirations", [])
    titles = []
    for asp in aspirations:
        if not isinstance(asp, dict):
            continue
        asp_id = asp.get("id")
        goals = asp.get("goals", []) or []
        for g in goals:
            if not isinstance(g, dict):
                continue
            title = g.get("title") or ""
            status = g.get("status") or ""
            if not title:
                continue
            # +candidate — §11b/ (world/conventions/goal-intake-management.md):
            # this builds the title corpus the duplication gate reads, so a candidate
            # absent here lets a duplicate of it pass the gate.
            if status in ("pending", "in-progress", "candidate"):
                titles.append(title)
            elif status == "completed" and asp_id == aspiration_id:
                titles.append(title)
    return titles


def make_child_goal(signal, source_goal, source_category, insight_text):
    """Assemble the child-goal JSON per SKILL.md Step 8.5."""
    # Shared-surface invariant (): every signal path converges here, so
    # whitespace collapse is enforced once rather than at each producer. A goal
    # title carrying an embedded newline breaks display, token-overlap dedup, and
    # every line-oriented consumer downstream.
    match = _strip_leading_label(_sanitize_fragment(signal["match"]))
    high_signal_types = {"root_cause", "bug_identified", "investigation_finding"}
    if signal["type"] in high_signal_types:
        title = f"Unblock: Fix {match}"
        priority = "HIGH"
        origin_prefix = "unblock:"
    else:
        title = f"Idea: {match}"
        priority = "MEDIUM"
        origin_prefix = "idea:"
    return {
        "title": title,
        "status": "pending",
        "priority": priority,
        "skill": None,
        "participants": ["agent"],
        "category": source_category,
        "description": (
            f"Found during {source_goal}: {match}\n\n"
            f"Source: {insight_text}\n\n"
            f"Discovered by: Step 8.5 Actionable Findings Gate"
        ),
        "verification": {
            "outcomes": ["Finding addressed — fix applied or determined not actionable with reasoning"],
            "checks": [],
        },
        "discovered_by": source_goal,
        "discovery_type": signal["type"],
        "origin_signal": f"{origin_prefix}{source_goal}",
    }


def dispatch_goal(goal_json, aspiration_id, source):
    """Invoke aspirations-add-goal.sh via subprocess. Returns True on success."""
    script = PROJECT_ROOT / "core" / "scripts" / "aspirations-add-goal.sh"
    if not script.exists():
        print(f"ERROR: aspirations-add-goal.sh not found at {script}", file=sys.stderr)
        return False
    cmd = bash_cmd(script, "--source", source, aspiration_id)
    try:
        result = subprocess.run(
            cmd,
            input=json.dumps(goal_json),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception as e:
        print(f"ERROR: aspirations-add-goal.sh subprocess failed: {e}", file=sys.stderr)
        return False
    if result.returncode != 0:
        print(f"ERROR: aspirations-add-goal.sh exit {result.returncode}: {result.stderr.strip()}",
              file=sys.stderr)
        return False
    return True


def read_outcome_note(goal_id):
    """Return a goal's outcome_note text via the aspirations-query.sh WRAPPER.

    The idea-capture lane (g-357-108): the recommendation an agent writes into
    outcome_notes at close is exactly the signal that was being lost — the tree
    insight Step 8.5 historically scanned rarely carries it. Reading the durable
    record's note (not an LLM-assembled temp file) also REPAIRS the mechanical
    worker_retrospective._lane_findings call, which passed no --insight-file and
    so died rc=2 at argparse before scanning anything (measured g-357-108).

    Store-parse discipline: aspirations.jsonl is NEVER read/parsed directly —
    aspirations-query.sh is the sanctioned wrapper and its JSON output is
    parseable by contract. Best-effort: any failure returns "" (the caller then
    falls back to --insight-file, or reports empty_insight).
    """
    if not goal_id:
        return ""
    script = PROJECT_ROOT / "core" / "scripts" / "aspirations-query.sh"
    if not script.exists():
        return ""
    try:
        result = subprocess.run(
            bash_cmd(script, "--goal-field", "goal_id", goal_id, "--full"),
            env={**os.environ, "MIND_AGENT": os.environ.get("MIND_AGENT", "")},
            capture_output=True, text=True, timeout=15, check=False,
        )
    except Exception:
        return ""
    if result.returncode != 0 or not result.stdout.strip():
        return ""
    try:
        records = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return ""
    if not isinstance(records, list):
        return ""
    for rec in records:
        if isinstance(rec, dict) and rec.get("goal_id") == goal_id:
            return (rec.get("outcome_note") or "").strip()
    # Fall back to the first record if the id field is projected differently.
    if records and isinstance(records[0], dict):
        return (records[0].get("outcome_note") or "").strip()
    return ""


def main():
    parser = argparse.ArgumentParser(
        description="Phase 8.5 Actionable Findings Gate — keyword scan + goal creation."
    )
    parser.add_argument("--goal", required=True, help="Source goal ID (discovering goal)")
    parser.add_argument("--insight-file", required=False, default=None,
                        help="Path to file containing the just-written insight text. "
                             "Optional when --scan-outcome-note is given; at least one "
                             "insight source is required.")
    parser.add_argument("--scan-outcome-note", action="store_true",
                        help="ALSO scan the source goal's durable outcome_note (read via "
                             "aspirations-query.sh). This is the idea-capture lane: it "
                             "catches an agent's own end-of-goal RECOMMENDATION/follow-up "
                             "that the tree-insight scan misses (g-357-108). Combined with "
                             "--insight-file when both are given.")
    parser.add_argument("--aspiration", required=True,
                        help="Parent aspiration ID (where child goals will be filed)")
    parser.add_argument("--category", required=True,
                        help="Category for child goals (usually source goal's category)")
    # WORLD_AGENT_ONLY: cross-agent routes via MIND_AGENT env override ()
    parser.add_argument("--source", default="world", choices=["world", "agent"],
                        help="Aspiration source for aspirations-add-goal.sh (default: world)")
    parser.add_argument("--is-investigation", action="store_true",
                        help="Set when the source goal's title starts with 'Investigate:'. "
                             "Enables the investigation-override path.")
    parser.add_argument("--investigation-needs-action", action="store_true",
                        help="LLM residue: when is-investigation AND keyword scan produced no "
                             "signals, caller passes this flag to convert the finding into an "
                             "investigation_finding signal. Omit to skip (informational).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Scan and report but do NOT create goals (for testing)")
    args = parser.parse_args()

    # Assemble the scanned text from up to two sources. At least one must be
    # requested — either an --insight-file (the tree-insight, Step 8.5's historical
    # input) or --scan-outcome-note (the durable outcome_note, the idea-capture
    # lane). Both are combined when both are given.
    if not args.insight_file and not args.scan_outcome_note:
        print("ERROR: supply --insight-file and/or --scan-outcome-note "
              "(at least one insight source is required)", file=sys.stderr)
        sys.exit(1)

    parts = []
    if args.insight_file:
        insight_path = Path(args.insight_file)
        if not insight_path.is_absolute():
            insight_path = PROJECT_ROOT / insight_path
        if not insight_path.exists():
            print(f"ERROR: insight-file does not exist: {insight_path}", file=sys.stderr)
            sys.exit(1)
        file_text = insight_path.read_text(encoding="utf-8").strip()
        if file_text:
            parts.append(file_text)

    if args.scan_outcome_note:
        note_text = read_outcome_note(args.goal)
        if note_text:
            parts.append(note_text)

    insight_text = "\n\n".join(parts).strip()
    if not insight_text:
        print("findings_count=0 created=0 reason=empty_insight")
        return

    # Step 1: keyword scan.
    signals = scan_signals(insight_text)

    # Step 2: investigation override.
    if args.is_investigation and len(signals) == 0 and args.investigation_needs_action:
        fragment = _first_prose_fragment(insight_text)
        if fragment:
            signals.append({"type": "investigation_finding", "match": fragment})
            _gate_log(
                "findings-gate",
                "block",
                trigger_matched="investigation_finding",
                caller="findings-gate.py:main",
                extra={"decision_path": "investigation-override"},
            )
        else:
            # No prose line anywhere in the insight, so nothing can name the
            # finding. Filing a goal titled from a heading marker is worse than
            # filing none — that was . Recorded as a distinct
            # decision_path so this is visible in telemetry rather than silent.
            print("▸ Step 8.5: investigation override requested but insight has no "
                  "prose line to name the finding — no goal created", file=sys.stderr)
            _gate_log(
                "findings-gate",
                "noop",
                trigger_matched="investigation_finding:no-prose",
                caller="findings-gate.py:main",
                extra={"decision_path": "investigation-override-unnameable"},
            )

    if len(signals) == 0:
        print("▸ Step 8.5: No actionable signals — passed")
        print("findings_count=0 created=0")
        # Whole-scan noop: zero signals after pattern scan + investigation
        # override. Counts toward invocation total but not as "fired"
        # (, retirement-evaluator semantics).
        _gate_log(
            "findings-gate",
            "noop",
            trigger_matched="no-actionable-signals",
            caller="findings-gate.py:main",
            extra={"decision_path": "scan-clean"},
        )
        return

    # Step 3: dedup.
    dedup_titles = load_dedup_titles(args.aspiration)

    created = 0
    for signal in signals:
        goal_json = make_child_goal(signal, args.goal, args.category, insight_text)
        if any(_title_similar(goal_json["title"], t) for t in dedup_titles):
            print(f"▸ Step 8.5: {signal['type']} detected but goal already exists — skipped")
            continue
        if args.dry_run:
            print(f"▸ DRY-RUN: would create {goal_json['title']} in {args.aspiration}")
            created += 1
            continue
        if dispatch_goal(goal_json, args.aspiration, args.source):
            print(f"▸ FINDINGS GATE: Created {goal_json['title']} in {args.aspiration} from {args.goal}")
            created += 1

    # Final machine-parseable summary line (state-update-audit reads this).
    print(f"findings_count={len(signals)} created={created}")


if __name__ == "__main__":
    main()
