#!/usr/bin/env python3
"""Completed-Not-Committed Sweep — flag code-deliverable goals marked
status=completed whose commit never landed on origin.

THE GAP THIS FILLS (rb-3135 / g-115-2570, CONFIRMED hypothesis
2026-07-11_completed-not-committed-recurrence): 4 instances in ~1 week of
goals closed status=completed whose code deliverable never reached origin
(g-115-1938 census audit code, g-115-1987 read_authoritative_bytes,
g-115-2332 git-side stamps absent). Each was caught by hand, none by a gate.
Systemic cause: nothing verifies the commit landed on origin before (or
shortly after) a code-deliverable goal closes completed. world/conventions/
post-execution.md Step 2 makes commit+push the agent's responsibility, but
"push landed" was never verified — so a local commit that was never pushed
(cross-box throttle, a missed push, an aborted iteration-push) closes the
goal completed while origin lacks the deliverable.

DETECTIVE, NOT INLINE (the goal's own design guidance): cross-box push
throttles make close-time origin-absence NORMAL for ~20min, so an inline
verify-phase gate would spam false positives on every fresh close. A sweep
with an AGE THRESHOLD (default 30min via --min-age-minutes) only flags a
commit that has stayed off origin PAST the throttle window.

NOT FALSE-POSITIVE-FREE — and this paragraph used to claim it was
(g-115-6060, 2026-08-12). The age threshold defeats ONE false-positive
mechanism (the throttle window) and the docstring generalized that into "the
exact false-positive-free shape the goal calls for", a property of the whole
sweep that nothing had measured. It was wrong when written and it aged badly:
tier 2 arrived later and inherited the blanket claim without re-earning it.
Measured on a live fleet run: 36 of 51 stranded_no_pr entries were pull
requests that had ALREADY MERGED, 4 spot-verified by hand against the forge —
a 71% false-positive rate in that bucket, structural rather than incidental
(see all_merged_on_default: a squash-merge rewrites the sha, so the goal's own
commit can never appear on the default branch no matter how thoroughly the
work shipped). Those 36 are now carved out as benign_squash_merged.

An over-trusted detector is worse than a noisy one: a reader who believes
"false-positive-free" reads 51 entries as 51 problems, and a reader who then
discovers otherwise stops reading the sweep entirely. State each lane's
measured behavior; do not let one lane's discipline vouch for the others.

DETECTION SHAPE. For each recently-completed goal (completed_at within
--lookback-hours, older than --min-age-minutes) whose recorded evidence names
a commit-SHA-shaped token, resolve which candidate repo the SHA belongs to and
probe `git branch -r --contains <sha>`:
  - SHA is on a remote branch          -> landed, clean (the common case)
  - SHA exists locally but on NO remote -> committed_not_pushed       -> FLAG
  - SHA is not a valid commit anywhere  -> DROPPED, never flagged
There is exactly ONE flag class here, not two. This list claimed a second
("claimed commit missing -> FLAG") that no code path has ever emitted — grep
the file: `committed_not_pushed` is the only tier-1 reason string. classify_goal
keeps `st is False` and drops `st is None`, so a goal whose SHAs are valid
nowhere produces no entry at all (verified by direct call, g-115-6060).

And "exists locally" is itself three situations, not one — `cat-file -e`
validates an OBJECT while the question is REACHABILITY FROM A REF, so a
dangling post-rebase commit and a refs/workers/** commit both read as
"unpushed". apply_reachability re-asks commit-reachability.py's six-valued
verdict about the flagged SHAs and relabels those two (absent_unreachable,
stranded_worker_ref) so neither carries a "push it" remedy that cannot work.

The DROP is deliberate and should stay: on a fleet of boxes holding partial
clones, "valid nowhere" overwhelmingly means "this box never fetched that
repo", not "the deliverable is gone" — flagging it would fire on the observer's
own clone state rather than on the work (the same fetch-dependence the
g-115-2660 comments below guard against). What was wrong is the DOC, which
advertised a detection the sweep does not perform. Anyone hunting genuinely-gone
commits needs a reachability probe that separates "absent here" from "absent
everywhere" (core/scripts/commit-reachability.py, whose ABSENT verdict is
exactly that distinction); this sweep does not attempt it and must not be read
as covering it.

TIER 2 — STRANDED ON AN UNMERGED BRANCH (g-115-3471). `git branch -r --contains`
is satisfied by ANY remote branch, so tier 1's "landed" verdict was ALSO true of
work pushed to a feature branch whose pull request was then never merged. That
made a real false negative in a live gate: on 2026-07-23 a fleet-wide run
reported "0 flagged — every completed goal's work landed in git/origin" while
the oldest of eleven open Lodestar pull requests had been unmerged for eight
days. A gate that is merely absent emits no signal; this one emitted a positive
all-clear for work no user could see, and nothing downstream re-checks a goal
the sweep already blessed. So tier 2 re-examines exactly the goals tier 1
cleared:
  - landed SHA contained by the repo's DEFAULT branch  -> shipped, clean
  - off default + an OPEN pull request >= --min-pr-age-hours -> stranded_open_pr
  - off default + every PR MERGED with its merge commit on the default branch
                                                       -> benign_squash_merged
  - off default + no open pull request                 -> stranded_no_pr (weaker)
Tier 2 is conservative in the NO-FLAG direction, opposite to apply_superseded:
an unresolvable default branch or an unreachable forge degrades to clean plus a
stderr warning, never to a flag. Tier 1's all-clear is the loop's trust anchor,
so a wrong tier-2 flag would cost more than a missed one.

--apply files ONE dedup'd Investigate per flagged goal, routed to the aspiration
_escalation_target.resolve() picks (framework hygiene) — asp-115 upstream, whatever
exists locally elsewhere. Do NOT re-hardcode the id here: this docstring said
"routed to asp-115" after the code had already been wired to the resolver, which
reads as a live hardcode to anyone auditing by grep (g-001-273, 2026-08-05). The two lanes dedup on separate origin_signal keys
("investigate:completed-not-committed-<goal-id>" and
"investigate:stranded-unmerged-<goal-id>") because they prescribe different
remedies — push the commit vs merge the pull request. stranded_no_pr is
report-only (a branch with no PR may still be live work). Without --apply the
whole sweep is report-only (dry run).

PURE CORE. `extract_commit_shas`, `is_code_deliverable`, `classify_goal`,
`landed_shas`, and `classify_stranded` are pure (no git, no daemon, no forge) —
the origin status, default-branch containment and pull-request record for each
SHA are INJECTED as dicts, so the full eligibility ladder is unit-testable with
synthetic goals and synthetic probe maps. main() is the only impure part
(daemon read + real git/gh probes + Investigate filing).

Sibling pattern (rb-428 detective-sweep family): defer-drift-check.py,
unblock-parent-status-sweep.py, parent-supersession-sweep.py. Guards honored:
guard-420 (datetime arithmetic — fromisoformat + Z-strip + exception-tolerant),
guard-383 (per-source read error fatal — a silent empty-aggregate would hide
origin-absence behind a "0 flagged" lie), guard-645 (field reads .get() with
defaults), guard-614 (structured JSON output). Reference: g-115-2570.
"""

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from _dt import parse_naive_iso  # noqa: E402  (shared tzinfo-stripping naive-ISO parse, )
from _owner_qualified_signal import (  # noqa: E402  (guard-2107, )
    qualified_signal, signal_candidates)
import _rt  # canonical Python -> daemon client (post-cutover; see _rt.py)

# : never hardcode the escalation aspiration —  is the UPSTREAM
# deployment's queue and does not exist elsewhere, so a literal files nothing.
try:
    from _paths import AGENT_DIR, WORLD_DIR  # noqa: E402
    from _escalation_target import resolve as _resolve_asp, source_flag as _asp_source
    ESCALATION_ASP, _ESCALATION_ASP_VIA = _resolve_asp(CORE_ROOT, WORLD_DIR, AGENT_DIR)
    ESCALATION_SOURCE = _asp_source(ESCALATION_ASP, WORLD_DIR, AGENT_DIR)
except Exception:
    ESCALATION_ASP, _ESCALATION_ASP_VIA, ESCALATION_SOURCE = (
        "asp-115", "fallback:import-failed", "world")

# Commit SHA extraction is KEYWORD-ANCHORED for precision AND to bound the
# probe count. A hex token counts ONLY when it sits next to a commit keyword
# ("committed <sha>", "pushed <sha>", "merge <sha>", "origin/main now at <sha>")
# or is an endpoint of a push range "<sha>..<sha>". Free-floating hex tokens —
# dates ("20260711" is 8 valid hex digits), env ids ("a7cb5456"), session
# hashes — are NOT extracted: the first live run flagged 196/2293 goals
# precisely because UNANCHORED hex matched those. Anchoring + the cat-file
# validation in probe_sha_origin remove the intra-box false positives; the
# CROSS-box false positive (a commit on the remote but unfetched into THIS box's
# origin/* refs) is closed separately by the `git fetch origin` main() runs once
# before probing () — without that fetch `git branch -r --contains`
# reads stale local refs and flags already-shipped cross-box work.
_ANCHORED_SHA_RE = re.compile(
    r"(?:commit(?:ted)?|pushed?|merged?|\bsha\b)"
    r"[\s:=@,]*(?:now\s+at\s+)?([0-9a-f]{7,40})\b",
    re.IGNORECASE)
_ORIGIN_SHA_RE = re.compile(
    r"origin/\S+\s+(?:now\s+at\s+|->\s*)?([0-9a-f]{7,40})\b",
    re.IGNORECASE)
_RANGE_SHA_RE = re.compile(r"\b([0-9a-f]{7,40})\.\.([0-9a-f]{7,40})\b")

# Keywords that mark a goal as producing a code deliverable. Used both to
# gate is_code_deliverable and to prefer SHAs appearing near them.
_COMMIT_KEYWORDS = (
    "commit", "committed", "pushed", "push", "merged", "merge",
    "sha", "origin/", "gh pr", "pull request", "deployed",
)

# Goal string fields most likely to carry the commit evidence. We scan these
# by name first, then fall back to a full-record string walk so a SHA stored
# in an unexpected field is still found.
_EVIDENCE_FIELDS = (
    "outcome_note", "completion_summary", "verify_summary", "summary",
    "description", "notes", "result",
)


def _tolerant_decode(source, raw):
    """guard-383/ tolerant decode for the daemon aspirations_read body."""
    return _rt.tolerant_decode_aggregate(
        f"completed-not-committed-sweep: {source}", raw)


def _read_goals(source):
    """Read all goals (any status) from active aspirations in one queue.

    guard-383 fatal symmetry (rb-987): a per-source read error in an N>=2
    source aggregator MUST be fatal — a silent `return []` writes a
    complete-looking lie ("0 flagged") into the merged aggregate.
    """
    try:
        out = _rt.aspirations_read(source=source, active=True)
    except _rt.RtError as e:
        print(f"[completed-not-committed-sweep] {source} read failed: "
              f"{e.body or e}", file=sys.stderr)
        sys.exit(1)  # guard-383: source error fatal
    data = _tolerant_decode(source, out)
    if data is None:
        return []
    goals = []
    for asp in (data.get("aspirations") if isinstance(data, dict) else data) or []:
        for g in asp.get("goals", []) or []:
            g["_source"] = source
            g["_aspiration_id"] = asp.get("id")
            goals.append(g)
    return goals


def _parse_iso(ts):
    """Tolerant ISO parse (guard-420). Returns datetime or None — never raises."""
    if not ts:
        return None
    try:
        return parse_naive_iso(ts)
    except Exception:
        return None


def _walk_strings(obj):
    """Yield every string value nested anywhere in a JSON-ish structure."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk_strings(v)


def extract_commit_shas(goal):
    """Return a de-duplicated list of commit-SHA-shaped tokens found in the
    goal's evidence. PURE. Prefers the named evidence fields, then falls back
    to a full-record walk. The tokens are CANDIDATES — main() validates each
    against real repos before treating it as a commit (a random hex token
    that is not a real commit resolves to status None and is ignored unless
    the goal actually claims a commit; see classify_goal).
    """
    seen = []
    seen_set = set()

    def _collect(text):
        text = text or ""
        toks = []
        for m in _ANCHORED_SHA_RE.finditer(text):
            toks.append(m.group(1))
        for m in _ORIGIN_SHA_RE.finditer(text):
            toks.append(m.group(1))
        for m in _RANGE_SHA_RE.finditer(text):
            toks.append(m.group(1))
            toks.append(m.group(2))
        for tok in toks:
            if tok not in seen_set:
                seen_set.add(tok)
                seen.append(tok)

    for field in _EVIDENCE_FIELDS:
        val = goal.get(field)
        if isinstance(val, str):
            _collect(val)
    # verification sub-fields (summary lands here on some close paths)
    ver = goal.get("verification") or {}
    if isinstance(ver, dict):
        for s in _walk_strings(ver):
            _collect(s)
    return seen


# The iteration-commit scope marker: loop commits embed "(<goal-id>)" in the
# message (rb-3999), and conventional-commit subjects like "fix():"
# carry the same parenthesized form. resolve_shas_by_goal_id greps for exactly
# this needle in the FORWARD direction (goal-id -> shas); this is the INVERSE
# (sha -> the goal-ids its own message names).
_COMMIT_GOALID_RE = re.compile(r"\((g-[a-z0-9]+-\d+)\)")


def own_shas(goal_id, shas, sha_goalid_owners):
    """Drop SHAs whose OWN commit message names a different goal. PURE
    (ownership injected as {sha: [goal_id, ...]}). g-115-6115.

    THE DEFECT THIS CLOSES. extract_commit_shas scrapes sha-shaped tokens out of
    PROSE, anchored on a verb cue (commit/pushed/merged/sha). The anchor kills
    free-floating hex well, but a regex cannot distinguish an ASSERTION of
    authorship from its RETRACTION or from a CITATION of someone else's work —
    the same verb appears in all three. Two measured consequences:
      - A careful agent documenting a self-correction ("I read this commit as
        uncarried and was WRONG") plants a token that is then attributed to it.
        The better the write-up, the more likely the false attribution.
      - Worse, it is SELF-PERPETUATING. A stranded-commit Investigate cites the
        sha as evidence, thereby acquiring it in its own commit scope, and is
        flagged in turn — forever. Measured chain: g-350-187 -> g-115-6275 ->
        g-115-6359. Nothing ages it out, because each link regenerates its own
        in-window member.

    THE ASYMMETRY IS THE WHOLE DESIGN. We reject ONLY on POSITIVE evidence of
    different ownership — the commit's message names a goal-id, and ours is not
    among them. A message naming NO goal-id is left alone. That is deliberately
    weaker than the obvious "require the flagged commit to name this goal", and
    the weakness is the point: the hard form introduces a FALSE NEGATIVE for
    every hand-made commit, and a false negative here is far worse than the
    false positive being fixed. This sweep is the fleet's only automated guard
    against a goal closing `completed` while its code never reaches the default
    branch; silently missing one of those is the failure a user actually sees.

    MEASURED against the live estate 2026-08-16 (cc-07, 60 candidate repos,
    2,535 goals scanned): 17 unique (goal, sha) attributions across all report
    classes. 17 of 17 commits carried a parenthesized goal-id (so the
    no-goal-id population the caveat warns about is currently EMPTY, which is
    why both forms would reject the same 7 here — do not read that as the two
    forms being equivalent in general). 10 named the flagged goal and were kept;
    7 named a different goal. Of those 7, exactly 2 are in the ACTIONABLE
    classes that file Investigates — g-115-6275 (sha names g-350-187) and
    g-115-6280 (sha names g-335-1212) — and both are chain links, i.e. 100% of
    the actionable false positives with zero genuine attributions lost.

    NOT A DEDUP CHANGE, deliberately. Keying dedup on the SHA and matching only
    OPEN goals still releases when the investigation closes, so the chain
    continues; matching ANY status silences the detector for that sha forever
    (guard-3419, a lease with no release). This filter needs no lease semantics
    at all: it corrects WHO a commit belongs to, so nothing goes quiet.
    """
    if not sha_goalid_owners:
        return list(shas)
    kept = []
    for s in shas:
        owners = sha_goalid_owners.get(s)
        if owners and goal_id and goal_id not in owners:
            continue  # positively owned by another goal — not this goal's work
        kept.append(s)
    return kept


def commit_goalids(sha, candidate_repos):
    """Goal-ids named in `sha`'s own commit message, across candidate repos.
    Impure (git). Returns a sorted list; empty when the sha is unknown here or
    its message names none. Mirrors probe_sha_origin's repo walk."""
    for repo in candidate_repos:
        rc, out = _git(repo, "log", "-1", "--format=%s%n%b", sha)
        if rc == 0 and out:
            return sorted(set(_COMMIT_GOALID_RE.findall(out)))
    return []


def build_sha_goalid_owners(shas, candidate_repos):
    """{sha: [goal_id, ...]} for each sha, from its own commit message. Impure.

    Bounded to the sha set the caller already resolved, matching the staging
    discipline of the sibling build_* probes — this runs one `git log -1` per
    sha, not per goal, and only over SHAs that reached a report class."""
    return {s: commit_goalids(s, candidate_repos) for s in shas}


def is_code_deliverable(goal):
    """True when the goal produced (or claims to have produced) a code
    deliverable that SHOULD land on origin. PURE.

    Signals (any):
      - work_class names a code lane (framework / product / hygiene-with-code)
      - evidence text carries a commit keyword (committed/pushed/merged/...)
      - a commit-SHA-shaped token is present
    A goal with none of these is treated as non-code (docs/tree/journal-only)
    and skipped — we never want to flag a knowledge-only close for "no commit".

    NOT THE WHOLE GATE ANY MORE (g-115-3476). Every signal above is PROSE-shaped
    — it reads how the closer narrated, when the question is what git contains.
    Both consumers now also admit on `has_git_evidence`, so a tersely-closed goal
    whose id appears in a real commit message is no longer skipped. Measured
    2026-07-28 on the live queue: of the 136 in-window completed goals this
    predicate rejected, 127 (93%) had commits resolvable by goal-id. It was not
    filtering non-code goals; it was filtering terse ones.
    """
    wc = (goal.get("work_class") or "").lower()
    if wc in ("framework", "product"):
        return True
    blob = " ".join(_walk_strings({
        k: goal.get(k) for k in _EVIDENCE_FIELDS
    })).lower()
    ver = goal.get("verification") or {}
    if isinstance(ver, dict):
        blob += " " + " ".join(_walk_strings(ver)).lower()
    if any(kw in blob for kw in _COMMIT_KEYWORDS):
        return True
    if extract_commit_shas(goal):
        return True
    return False


def has_git_evidence(goal, goalid_status):
    """True when this goal's id appears in a real commit message. PURE — the git
    work already happened in build_goalid_status; this only reads its result.

    Whether a goal produced a code deliverable is a fact about GIT, not about how
    its closer chose to narrate, so this admits goals that is_code_deliverable's
    prose signals reject. sig-38: a detector predicate narrower than its
    motivating incident class ships blind to its own trigger. (g-115-3476)
    """
    return bool((goalid_status or {}).get(goal.get("id")))


def classify_goal(goal, now, sha_status, min_age_minutes=30.0,
                  lookback_hours=168.0, goalid_status=None,
                  sha_goalid_owners=None):
    """Pure eligibility test for ONE goal. Returns a flag entry dict when the
    goal is a completed code deliverable whose commit is absent from origin,
    else None.

    `sha_status` is the INJECTED probe map {sha: True|False|None}:
      True  -> SHA is on a remote branch (landed)
      False -> SHA exists locally but on no remote (committed, not pushed)
      None  -> SHA is not a valid commit in any candidate repo (missing)
    A goal is FLAGGED when it has >=1 extracted SHA and NONE of its SHAs are
    on origin (i.e. every extracted SHA is False or None). If ANY SHA landed,
    the deliverable shipped -> clean (a goal may name several SHAs; one landing
    is proof of the deliverable, so we only flag total origin-absence).

    `goalid_status` is the INJECTED goal-id-resolution map
    {goal_id: {sha: True|False|None}}, built by build_goalid_status for goals
    with ZERO extracted SHAs (g-115-2600). Loop-commit messages embed the
    goal-id, not a SHA (rb-3999), so the common phantom record shape carries no
    SHA token and the extracted-SHA path above is structurally blind to it. When
    a zero-SHA code goal's id resolves (via git log --grep '(<goal-id>)') to a
    local-only commit, the SAME landing check applies. None-status resolved
    tokens are dropped, exactly like the extracted-SHA path.

    Age gate: completed_at must be OLDER than min_age_minutes (push-throttle
    guard — no false positives on fresh closes) and WITHIN lookback_hours
    (keep the report recent + bounded).
    """
    if goal.get("status") != "completed":
        return None
    # Prose OR git. is_code_deliverable reads only how the close was narrated;
    # has_git_evidence reads what actually landed. Either admits. ()
    if not is_code_deliverable(goal) and not has_git_evidence(goal, goalid_status):
        return None
    completed_at = _parse_iso(goal.get("completed_at"))
    if completed_at is None:
        return None
    age_minutes = (now - completed_at).total_seconds() / 60.0
    if age_minutes < min_age_minutes:
        return None  # inside the push-throttle window — not yet actionable
    if age_minutes > lookback_hours * 60.0:
        return None  # too old — outside the actionable lookback window
    # Ownership filter () applies to the PROSE-scraped set only; the
    # goal-id fallback below is owned by construction. Note the interaction that
    # makes this safe rather than merely narrower: dropping a foreign sha can
    # empty `shas`, which routes the goal into the goal-id fallback — the same
    # path a record with no sha tokens already takes. So a goal whose only sha
    # was another goal's is judged on ITS OWN commits, not flagged for theirs.
    shas = own_shas(goal.get("id"), extract_commit_shas(goal), sha_goalid_owners)
    if not shas:
        # Zero SHA tokens in the record. Loop-commit messages embed the goal-id,
        # not a SHA (rb-3999), so the COMMON phantom shape carries no SHA and the
        # extracted-SHA path above never sees it (: both 2026-07-18
        # phantoms had zero SHA tokens). Fall back to goal-id resolution: main()
        # injects goalid_status[goal_id] = {sha: origin_status} for commits whose
        # message carries "(<goal-id>)". Apply the SAME landing check + None-drop.
        resolved = (goalid_status or {}).get(goal.get("id")) or {}
        if not resolved:
            # No commit found by id either -> genuinely out of scope (a legitimate
            # narrative/docs close names no SHA and produced no goal-id commit).
            return None
        if any(st is True for st in resolved.values()):
            return None  # a goal-id-resolved commit landed on origin -> clean
        absent = [s for s, st in resolved.items() if st is False]
        if not absent:
            return None  # only None-status tokens -> drop (same guard as SHA path)
        return {
            "goal_id": goal.get("id"),
            "source": goal.get("_source"),
            "aspiration_id": goal.get("_aspiration_id"),
            "completed_at": goal.get("completed_at"),
            "age_hours": round(age_minutes / 60.0, 1),
            "reason": "committed_not_pushed",
            "shas_absent_local_only": absent,
            "resolved_via": "goal-id",
            "title": (goal.get("title") or "")[:80],
        }
    statuses = [sha_status.get(s) for s in shas]
    if any(st is True for st in statuses):
        return None  # at least one SHA landed on origin — deliverable shipped
    # RELIABLE SIGNAL ONLY: a token that `git cat-file -e` VALIDATED as a real
    # local commit (status False) that is on NO remote branch. A None-status
    # token is almost always NOT a commit — a date ("20260711" is 8 valid hex
    # digits), an env/session id ("a7cb5456"), or a message hash — and flagging
    # those produced a 196/2293 false-positive flood in the first live run. So
    # None is dropped entirely; the "never-committed" case (no valid SHA at all)
    # is deliberately out of scope (undetectable false-positive-free from prose).
    absent = [s for s in shas if sha_status.get(s) is False]
    if not absent:
        return None
    return {
        "goal_id": goal.get("id"),
        "source": goal.get("_source"),
        "aspiration_id": goal.get("_aspiration_id"),
        "completed_at": goal.get("completed_at"),
        "age_hours": round(age_minutes / 60.0, 1),
        "reason": "committed_not_pushed",
        "shas_absent_local_only": absent,
        "title": (goal.get("title") or "")[:80],
    }


def landed_shas(goal, now, sha_status, goalid_status=None,
                min_age_minutes=30.0, lookback_hours=168.0,
                sha_goalid_owners=None):
    """For an ELIGIBLE completed code goal, return its SHAs that ARE on some
    remote branch (sha_status True). Empty list when the goal is ineligible or
    nothing landed. PURE. g-115-3471.

    UNION of both attribution paths — the SHAs named in the record AND the ones
    resolved from the goal-id in commit messages (rb-3999 loop commits embed
    "(<goal-id>)", not a SHA) — deliberately NOT classify_goal's either/or. The
    two lanes ask different questions and need different evidence. Tier 1 asks
    "did anything land?", so the first landing settles it and a partial view is
    harmless. Tier 2 asks "did everything reach the default branch?", where a
    partial view produces a FALSE CLEAN: a multi-repo goal whose named commit
    reached main in an unprotected repo while its other half waits on an open
    PR scores clean under either/or. That is not hypothetical — it is g-335-190
    (named 326bf09, on main; its Vinheim half 3b0b14ee sat on the branch of PR
    #53, open 5 days), and it was invisible until this lane was measured against
    the live estate rather than trusted. Dedup by set keeps the union honest
    when a SHA is reachable both ways.

    This lane is the exact complement of classify_goal: that function flags when
    NOTHING landed, this one only looks at goals where something DID. The two are
    mutually exclusive by construction and need no cross-dedup.
    """
    if goal.get("status") != "completed":
        return []
    # Prose OR git — same widening as classify_goal. This tier is where the
    # three measured counter-examples live:  /  /  all
    # sat on the branches of OPEN PRs in product repos and were invisible
    # because their closers happened to write no commit keyword.
    if not is_code_deliverable(goal) and not has_git_evidence(goal, goalid_status):
        return []
    completed_at = _parse_iso(goal.get("completed_at"))
    if completed_at is None:
        return []
    age_minutes = (now - completed_at).total_seconds() / 60.0
    if age_minutes < min_age_minutes or age_minutes > lookback_hours * 60.0:
        return []
    # The PROSE path is the one that can misattribute (), so the
    # ownership filter applies HERE and not below. The goal-id path that follows
    # resolves SHAs *from* "(<goal-id>)" in the commit message, so those are
    # owned by this goal by construction and filtering them would be circular.
    landed = [s for s in own_shas(goal.get("id"),
                                  extract_commit_shas(goal),
                                  sha_goalid_owners)
              if sha_status.get(s) is True]
    seen = set(landed)
    resolved = (goalid_status or {}).get(goal.get("id")) or {}
    for s, st in resolved.items():
        if st is True and s not in seen:
            seen.add(s)
            landed.append(s)
    return landed


def all_merged_on_default(records, merge_default_status):
    """True when EVERY pull-request record is MERGED and its merge commit is
    confirmed on the default branch — i.e. the work shipped under a REWRITTEN
    sha. PURE (containment injected). g-115-6060.

    THE FALSE POSITIVE THIS CLOSES. `classify_stranded` asks whether the goal's
    OWN sha is on the default branch. Under a squash- or rebase-merge the answer
    is permanently no, by construction: the forge discards the branch commit and
    writes a new one. So the single most conclusive evidence a product goal can
    emit — a merged pull request — was scored identically to an abandoned
    branch, and the goal landed in stranded_no_pr forever. Measured 2026-08-12:
    36 of 51 stranded_no_pr entries were merged pull requests, 4 spot-verified
    by hand against the forge. The residual 15 are the ones worth reading, and
    they were unreadable underneath the 36.

    CONSERVATIVE, matching classify_stranded's no-flag direction — but note the
    direction INVERTS here, because this predicate SUPPRESSES rather than flags.
    Every uncertainty must therefore resolve to False (stay stranded), never to
    True: an unresolvable merge commit, a merge commit not on the default
    branch, a null merge_commit_sha, a CLOSED-not-merged record, or an empty
    record set all decline to bless. Requiring ALL records (not any) mirrors
    apply_superseded — one unexplained sha keeps the whole entry visible.

    STALENESS CANNOT MANUFACTURE A BLESSING (rb-4716). The injected containment
    comes from probe_sha_on_default, which returns None when the sha is in no
    candidate repo — the exact shape a merge commit takes in an unfetched local
    clone. None is not True, so an out-of-date repo degrades to the status quo
    ante (still stranded_no_pr) rather than to a false all-clear."""
    if not records:
        return False
    for r in records:
        if r.get("state") != "MERGED":
            return False
        msha = r.get("merge_commit_sha")
        if not msha or merge_default_status.get(msha) is not True:
            return False
    return True


def _pr_repo(pr):
    """Repo NAME from a PR record's url ('.../OWNER/REPO/pull/N' -> 'REPO').

    The key for deploy_hold_status. Returns None when the url is missing or
    unparseable, which lands on the no-information branch rather than on a
    wrong lookup — an unparseable url must never read as CLEAR.
    """
    url = (pr or {}).get("url") or ""
    parts = [x for x in url.split("/") if x]
    if "pull" in parts:
        i = parts.index("pull")
        if i >= 1:
            return parts[i - 1]
    return None


def build_deploy_hold_status(pr_status, repo_roots, world_path=None):
    """{repo_name: {"held": True|False, "holders": [goal_id, ...]}} for every
    repo carrying an OPEN pull request. Impure: shells out to
    world/scripts/deploy-hold-check.sh, the canonical probe (guard-3139).

    A repo is OMITTED — not recorded False — when the checkout cannot be found,
    bash is unavailable, the probe errors (rc=1), or the payload will not parse.
    Omission lands the entry on the classifier's no-information branch, which
    keeps the pre-existing verdict. Recording False there would be a definite
    "no hold" verdict manufactured from a plumbing failure (guard-3616), and
    this lane files goals — so a wrong CLEAR is the expensive direction.

    rc contract, from the probe's own usage: 0 CLEAR, 3 HELD, 1 usage/plumbing.
    The 3-not-1 split exists precisely so a broken run can never read as a hold
    nor a hold as a broken run; preserve it — do not collapse to truthiness.
    """
    import glob as _glob
    import shutil as _shutil
    import subprocess as _sp

    out = {}
    if not repo_roots:
        return out
    # guard-580: never argv[0]="bash" — resolve the real binary or decline.
    bash = _shutil.which("bash")
    world = world_path or os.environ.get("WORLD_PATH")
    if not bash or not world:
        return out
    probe = os.path.join(world, "scripts", "deploy-hold-check.sh")
    if not os.path.exists(probe):
        return out

    repos = set()
    for rec in (pr_status or {}).values():
        if (rec or {}).get("state") == "OPEN":
            name = _pr_repo(rec)
            if name:
                repos.add(name)

    for name in sorted(repos):
        path = None
        for root in repo_roots:
            for cand in (os.path.join(root, name),
                         *sorted(_glob.glob(os.path.join(root, "*", name)))):
                if os.path.isdir(os.path.join(cand, ".git")):
                    path = cand
                    break
            if path:
                break
        if not path:
            continue
        try:
            r = _sp.run([bash, probe, "--repo", path, "--json"],
                        capture_output=True, text=True, timeout=120)
        except Exception:
            continue
        if r.returncode not in (0, 3):
            continue  # usage/plumbing — omit, never record a manufactured CLEAR
        holders = []
        try:
            payload = json.loads(r.stdout or "{}")
            # Holder ids come from the PROBE, never from PR text — they rotate
            # (measured 2026-08-26: PR title  vs live probe ).
            holders = [h.get("goal_id") for h in (payload.get("holds") or [])
                       if h.get("goal_id")]
        except Exception:
            if r.returncode == 3:
                continue  # HELD but unreadable holders — no information
        out[name] = {"held": r.returncode == 3, "holders": holders}
    return out


def classify_stranded(goal, now, sha_status, default_status, pr_status,
                      min_age_minutes=30.0, lookback_hours=168.0,
                      min_pr_age_hours=24.0, goalid_status=None,
                      merge_default_status=None, sha_goalid_owners=None,
                      deploy_hold_status=None):
    """Pure second-tier test: the goal's commit reached origin, but only on a
    NON-DEFAULT branch. Returns a stranded entry or None. g-115-3471.

    THE FALSE NEGATIVE THIS CLOSES. Tier 1 (classify_goal + probe_sha_origin)
    decides "landed" by `git branch -r --contains`, which is satisfied by ANY
    remote branch. So a deliverable pushed to a feature branch whose pull
    request is then never merged scores LANDED and CLEAN — the sweep emits a
    positive all-clear for work no user can see. Dated counter-example: on
    2026-07-23 a fleet-wide run reported "0 flagged — every completed goal's
    work landed in git/origin", while the oldest of eleven open Lodestar pull
    requests had been unmerged for eight days. Every one of those commits was on
    a remote branch, so tier 1 blessed all of them. A gate that is merely absent
    emits no signal; this one manufactured confidence.

    `default_status` is the INJECTED map {sha: True|False|None}:
      True  -> contained by the repo's default branch (genuinely shipped)
      False -> on a remote branch but NOT on the default branch
      None  -> default branch could not be resolved (undeterminable)
    `pr_status` is the INJECTED map {sha: {"state": ..., "number": ..., ...}}
    where state is OPEN | CLOSED | MERGED | NONE | UNAVAILABLE.

    CONSERVATIVE IN THE NO-FLAG DIRECTION, deliberately opposite to
    apply_superseded. Tier 1 is the source of the loop's "clean sweep" signal,
    so a wrong flag here degrades a trusted all-clear into noise. Therefore
    None-status default resolution does NOT flag, and — per this goal's explicit
    scope note — an UNAVAILABLE forge does NOT flag either: an unreachable API
    must never turn a clean sweep into a flagged one. Both degrade to clean and
    surface as warnings in the run report instead.

    Four classes, matching the goal's guidance that they carry different weight.
    THE LAST TWO ARE BENIGN AND MUST NOT BE SUMMED INTO A CONSUMER'S STRANDED
    COUNT — see the Step 8.79a advisory in iteration-close.sh (g-115-7881):
      stranded_open_pr -> commit off-default AND an open PR carries it, open at
                          least min_pr_age_hours. STRONG: the work is finished,
                          reviewed-or-not, and simply not merged. Investigate-filed.
      stranded_no_pr   -> commit off-default with no open PR (none found, closed,
                          or merged into a non-default base). WEAKER — could be a
                          live working branch — so it is report-only, never filed.
      benign_squash_merged -> every off-default sha belongs to a MERGED pull
                          request whose merge commit IS on the default branch.
                          NOT stranded at all: the work shipped under a rewritten
                          sha. Report-only, and carved out of stranded_no_pr
                          rather than added to it — see all_merged_on_default.
      stranded_deploy_held -> commit off-default WITH an open PR, but the repo
                          is under an ACTIVE deploy hold, so merging would fire
                          the deploy the hold exists to prevent. PARKED, not
                          stranded — report-only, never filed, and like
                          benign_squash_merged it must never be added to a
                          stranded warning: telling a closer to "land it" is
                          telling them to do the thing the hold forbids.
                          Decorated with a top-level deploy_holders field; holder
                          ids ROTATE, so they come from the probe payload and are
                          never read off the PR text.

    `merge_default_status` is the INJECTED map {merge_commit_sha: True|False|None}
    built by the same probe as `default_status`. Defaulting it to None (treated
    as {}) keeps every pre-g-115-6060 caller behaving exactly as before, which
    matters because the squash carve-out must never fire on a caller that did not
    supply the evidence for it.
    A PR younger than min_pr_age_hours suppresses the entry entirely rather than
    demoting it to stranded_no_pr: a freshly-opened PR is in flight, not stranded,
    and the existing age-threshold discipline (no false positives inside the
    normal settle window) is what keeps this sweep quiet enough to be trusted.
    """
    landed = landed_shas(goal, now, sha_status, goalid_status,
                         min_age_minutes, lookback_hours,
                         sha_goalid_owners=sha_goalid_owners)
    if not landed:
        return None  # tier-1's lane (nothing landed) or ineligible — not ours
    off_default = [s for s in landed if default_status.get(s) is False]
    if not off_default:
        # On the default branch, or undeterminable. Either way the deliverable
        # is not PROVABLY stranded — stay silent.
        return None

    # Pick the most informative PR record across the off-default SHAs: an open
    # one if any, else whatever was found, so the report still names the PR that
    # closed or merged elsewhere.
    #
    # A MISSING key defaults to UNAVAILABLE, never to {} (which would read as
    # "no PR" and land the goal in stranded_no_pr). "Not probed" and "probe
    # failed" are the same epistemic state, and only UNAVAILABLE suppresses.
    # main() currently probes every off-default SHA so the key is always present,
    # but the obvious future optimization — narrowing the gh probe set to bound
    # network calls — would otherwise silently convert unprobed SHAs into report
    # lines asserting a PR does not exist. Absence of a probe is not a negative
    # result (verify-before-assuming rule 4).
    records = [pr_status.get(s) or dict(_PR_UNAVAILABLE) for s in off_default]
    if any(r.get("state") == "UNAVAILABLE" for r in records):
        return None  # forge unreachable — never convert a clean sweep to a flag
    open_prs = [r for r in records if r.get("state") == "OPEN"]
    deploy_holders = []
    pr = None
    if open_prs:
        pr = open_prs[0]
        created = _parse_iso(pr.get("created_at"))
        pr_age_hours = (
            (now - created).total_seconds() / 3600.0 if created else None)
        # Unknown age suppresses, exactly like UNAVAILABLE. This is the only
        # branch that produces a WRITE (--apply files an Investigate), so an
        # unparseable created_at must not be the one input that walks past the
        # age gate — "cannot confirm the PR is old enough" is not "the PR is old
        # enough", and the whole lane is conservative in the no-flag direction.
        if pr_age_hours is None or pr_age_hours < min_pr_age_hours:
            return None  # in flight, or age unknown — not provably stranded
        # An open PR on an auto-deploying repo under an ACTIVE DEPLOY HOLD is
        # not stranded — it is correctly parked, and merging it would fire the
        # very deploy the hold exists to prevent. Filing an Investigate here
        # asks the fleet to re-derive what world/scripts/deploy-hold-check.sh
        # returns in seconds; it was answered identically three times
        # (, , ) before this branch existed.
        #
        # Three-way, deliberately not two-way (guard-4028 / guard-3616):
        #   held is True  -> DECISIVE hold. Reclassify benign; never swallow a
        #                    decisive signal into the flagged verdict.
        #   held is False -> DECISIVE clear. Keep stranded_open_pr and file.
        #   None/absent   -> NO information (caller did not probe, or the probe
        #                    errored). Keep the PRE-EXISTING verdict rather than
        #                    invent a new definite one — the same contract
        #                    merge_default_status documents above: a carve-out
        #                    must never fire on a caller that did not supply the
        #                    evidence for it.
        #
        # Holder ids ROTATE. Measured 2026-08-26: the PR title quoted 
        # while the live probe returned  for the same repo. The holders
        # below therefore come from the PROBE PAYLOAD and must never be read off
        # the PR text or hardcoded.
        _hold = (deploy_hold_status or {}).get(_pr_repo(pr))
        if isinstance(_hold, dict) and _hold.get("held") is True:
            reason = "stranded_deploy_held"
            deploy_holders = [h for h in (_hold.get("holders") or []) if h]
        else:
            reason = "stranded_open_pr"
    else:
        pr = next((r for r in records if r.get("number")), None)
        pr_age_hours = None
        if pr:
            created = _parse_iso(pr.get("created_at"))
            if created:
                pr_age_hours = (now - created).total_seconds() / 3600.0
        if all_merged_on_default(records, merge_default_status or {}):
            reason = "benign_squash_merged"
        else:
            reason = "stranded_no_pr"
    # A multi-repo goal can be stranded on SEVERAL pull requests at once, and
    # naming one sends the reader to half the remedy —  is stranded on
    # BOTH Vinheim #54 and Zak-Code #129, and the first live report named only
    # #54. Carry the rest disjointly rather than duplicating the primary.
    other_prs = []
    seen_numbers = {pr.get("number")} if pr else set()
    for r in records:
        n = r.get("number")
        if n and n not in seen_numbers:
            seen_numbers.add(n)
            other_prs.append({"number": n, "state": r.get("state"),
                              "url": r.get("url")})

    # completed_at is guaranteed parseable here: landed_shas returns [] when it
    # is None, and an empty landed list short-circuits above. Keep that coupling
    # in mind before reordering — an unguarded None reaches this subtraction.
    completed_at = _parse_iso(goal.get("completed_at"))
    age_minutes = (now - completed_at).total_seconds() / 60.0
    entry = {
        "goal_id": goal.get("id"),
        "source": goal.get("_source"),
        "aspiration_id": goal.get("_aspiration_id"),
        "completed_at": goal.get("completed_at"),
        "age_hours": round(age_minutes / 60.0, 1),
        "reason": reason,
        "shas_off_default": off_default,
        "resolved_via": (
            "record-sha" if any(s in extract_commit_shas(goal)
                                for s in off_default) else "goal-id"),
        "title": (goal.get("title") or "")[:80],
    }
    if reason == "stranded_deploy_held":
        # Top-level on purpose: the pull_request dict below is rebuilt FIELD BY
        # FIELD, and omitting a field there has silently made a whole fork inert
        # in production twice ( `draft`,  `body`). Keeping the
        # holders out of that rebuild keeps this tier out of that failure class.
        entry["deploy_holders"] = deploy_holders
    if pr and pr.get("number"):
        entry["pull_request"] = {
            "number": pr.get("number"),
            "state": pr.get("state"),
            "url": pr.get("url"),
            "title": (pr.get("title") or "")[:80],
            "created_at": pr.get("created_at"),
            "age_hours": (round(pr_age_hours, 1)
                          if pr_age_hours is not None else None),
            # : this dict is rebuilt FIELD BY FIELD from the probe
            # record, so a field the prober resolves but this list omits is
            # silently dropped — and `draft` was omitted, which made the entire
            #  draft fork INERT in production from the day it
            # shipped. _file_investigate reads `pr.get("draft")` off THIS dict,
            # so `_is_draft` could only ever be False. Its tests passed
            # throughout because they hand-build an entry WITH the key rather
            # than routing through classify_stranded — the contract-ideal arg
            # shape instead of the production one (guard-920 / rb-5235).
            # Measured 2026-08-15: the forge returned draft=true for PR #425
            # and all 8 live stranded entries still read draft=None.
            "draft": pr.get("draft"),
            # : added under the warning the comment above already
            # gives. `body` is the ONLY field that separates a deliberate hold
            # from a handoff artifact, so omitting it here would make the
            # gate-language fork inert in production exactly the way omitting
            # `draft` made the  fork inert — same dict, same
            # field-by-field rebuild, one year of the same lesson.
            "body": pr.get("body"),
        }
    else:
        entry["pull_request"] = None
    entry["other_pull_requests"] = other_prs
    return entry


# Gate language a deliberate hold declares in its own PR body. Deliberately
# GENEROUS, including weak terms like "until ", because the two errors are not
# symmetric ():
#   false GATE  (we say "deliberate hold" and it was an artifact) -> the PR
#       stays stranded, which is the status quo this fork improves on. Cheap.
#   false ARTIFACT (we say "nobody owns discharging this" and a real gate
#       exists) -> the remedy advises shipping a half-feature. Expensive.
# So any single hit is enough to call it a gate. Measured 2026-08-17 on the four
# PRs the originating goal named: the three handoff artifacts scored 0/6 and the
# one real hold scored 4/6 — a clean gap, no tuning needed.
_MERGE_GATE_MARKERS = (
    "merge gate", "do not merge", "draft on purpose",
    "blocked", "precondition", "until ",
)


def pr_declares_merge_gate(body):
    """True when a PR body states a reason it must not be merged yet.

    Returns None when BODY IS UNAVAILABLE (absent, non-string, or empty). None
    is NOT False and callers must not collapse the two: an unreadable body is
    "cannot tell", and the fail-safe answer to cannot-tell is the conservative
    deliberate-hold narrative, never "nobody owns this, go discharge it". Older
    sweep entries predate the body field entirely, so this case is the norm on
    historical data rather than an edge case.
    """
    if not isinstance(body, str) or not body.strip():
        return None
    low = body.lower()
    return any(m in low for m in _MERGE_GATE_MARKERS)


# A goal-id cited in PROSE anywhere in the body. Distinct from
# _COMMIT_GOALID_RE, which requires parentheses because it parses
# conventional-commit SUBJECTS (`feat(): ...`); a PR body cites the
# id bare ("Paired with "), so that pattern matches zero of them.
_BODY_GOALID_RE = re.compile(r"\b(g-[a-z0-9]+-\d+)\b")


def pr_cited_goal_ids(body):
    """Goal ids cited anywhere in a PR body, in first-appearance order.

    Used ONLY to make a token-miss remedy actionable: a body that names a goal
    is a body with a stated reason to exist, so the reader is pointed at that
    id instead of being told to read an unspecified wall of text. It is
    deliberately NOT used to classify the PR — resolving whether the cited goal
    is live is the reader's job, and a detector that resolved it here would be
    re-deriving the same positive conclusion guard-4432 forbids, one field over.
    """
    if not isinstance(body, str) or not body.strip():
        return []
    seen, out = set(), []
    for m in _BODY_GOALID_RE.findall(body.lower()):
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def apply_superseded(entry, superseded_status):
    """Mark a flagged committed_not_pushed entry benign_superseded when EVERY
    local-only SHA's changed files are already byte-identical in HEAD. PURE
    (git status injected via superseded_status={sha: True|False}). g-115-3032.

    This distinguishes a convergent-parallel-fix ORPHAN — a narrow local fix
    superseded+generalized by another box's commit at merge, so the SHA is
    absent from origin yet its DELIVERABLE is on origin under a DIFFERENT SHA —
    from a genuinely lost deliverable. The orphan is a benign false positive
    that cost an Investigate each run under SHA-absence-alone flagging
    (g-115-3031: fcb8dd05e superseded by g-115-3023/3024).

    A benign_superseded entry stays in the REPORT (log-only visibility) but is
    NOT Investigate-filed by main() --apply. An entry keeps
    reason=committed_not_pushed (and IS filed) when >=1 of its local-only SHAs
    is NOT superseded — i.e. >=1 changed file's content is genuinely absent from
    HEAD — preserving the g-115-2570 / rb-3135 real-loss detection this sweep
    exists for. Requiring ALL absent SHAs superseded (not any) is the
    conservative direction: one un-superseded SHA is enough to keep the flag.
    """
    absent = entry.get("shas_absent_local_only") or []
    benign = bool(absent) and all(
        superseded_status.get(s) is True for s in absent)
    entry["benign_superseded"] = benign
    if benign:
        entry["reason"] = "benign_superseded"
    return entry


def apply_landed_elsewhere(entry, landed_elsewhere_status):
    """Decorate a stranded_open_pr entry with DEFAULT-branch commits carrying
    THIS goal's conventional-commit scope — evidence the deliverable already
    shipped under a different commit, so "merge the pull request" is the wrong
    remedy. PURE (git injected). g-115-6295.

    THE INFERENCE THIS CORRECTS. Tier 2 observes "commits naming goal X sit on
    an unmerged branch" and concludes "goal X's deliverable has not reached the
    default branch, so no user can see it. Merge the pull request." A goal
    REDONE ON A SECOND BRANCH satisfies the premise and not the conclusion: the
    abandoned first attempt's commits are genuinely stranded while the
    deliverable shipped from elsewhere. Measured base rate over the five-goal
    cluster this fix was filed from: 4 of 5 remedies falsified against a
    diagnosis that was correct every single time. The diagnosis is not in
    question here and is NOT suppressed — only the remedy forks.

    NOT A SUPPRESSOR, deliberately. `reason` is unchanged and the entry is
    still filed. A goal that closed `completed` while its first attempt sits
    unlanded on an open PR is a genuine finding whether or not a second attempt
    shipped — the stranded branch still wants closing, and the goal still
    closed against evidence nobody checked.

    Writes TWO fields, because the probe is tri-state and an empty result has
    two different meanings. `landed_elsewhere` is always a list, so a missing
    key and an unrunnable probe both fall to the not-landed branch (fail-safe).
    `landed_elsewhere_probed` records whether that emptiness is a FINDING —
    False means the sweep never established it, and the remedy must not claim
    it did.
    """
    hits = landed_elsewhere_status.get(entry.get("goal_id"))
    entry["landed_elsewhere"] = list(hits or [])
    entry["landed_elsewhere_probed"] = hits is not None
    return entry


# Verdicts from commit-reachability.py that CONTRADICT the "push it" remedy, and
# the reason each becomes. Both are cases probe_sha_origin cannot distinguish
# from an unpushed commit, because `git cat-file -e` succeeds for any object in
# the database whether or not a ref reaches it.
_MISROUTED_VERDICTS = {
    "ABSENT": "absent_unreachable",
    "STRANDED_WORKER_REF": "stranded_worker_ref",
}


def apply_reachability(entry, reachability_status):
    """Correct a flagged committed_not_pushed entry whose SHAs are not actually
    unpushed. PURE (six-valued verdicts injected). g-115-6060.

    THE FALSE POSITIVE THIS CLOSES. probe_sha_origin answers a LOCAL-vs-REMOTE
    BOOLEAN: `cat-file -e` succeeds, `branch -r --contains` is empty, therefore
    "committed but not pushed — push it". That boolean silently merges three
    different situations, because cat-file validates an OBJECT while the real
    question is REACHABILITY FROM A REF:
      - genuinely unpushed        -> STRANDED_LOCAL_ONLY -> push it (correct)
      - dangling after rebase/amend -> ABSENT            -> pushing is impossible
      - carried on refs/workers/** -> STRANDED_WORKER_REF -> consume the ref
    Only the first is a push problem. Measured 2026-08-12: the run's single
    tier-1 flag (sha 679b9e7) was ABSENT — reachable from NO ref, local or
    remote — and was filed as a HIGH Investigate telling an agent to push a
    commit that cannot be pushed. Both of tier 1's stated guards passed on it:
    keyword-anchoring held, and the None-status drop held precisely BECAUSE the
    object still exists locally. guard-3320 documents that ABSENT's remedy is
    explicitly not "push".

    NOT A SUPPRESSOR. The entry stays in the report with a corrected reason and
    the verdict attached, so a genuinely-lost deliverable remains visible — it
    just stops carrying a remedy that cannot work. What it does prevent is the
    --apply write, since filing an Investigate with the wrong remedy costs an
    agent a wasted cycle and teaches the fleet to distrust the sweep.

    CONSERVATIVE: the flag SURVIVES unless every absent SHA agrees on the same
    misrouted verdict. STRANDED_LOCAL_ONLY, INCONCLUSIVE, an unprobed SHA, a
    mixed set, and an empty set all keep committed_not_pushed — an unavailable
    probe is not a negative result (verify-before-assuming rule 4), and this is
    the lane that detects real deliverable loss (rb-3135 / g-115-2570).

    Runs AFTER apply_superseded and defers to it: a superseded entry has its
    content in HEAD, which is a stronger and more useful statement than any
    reachability verdict about the orphaned sha."""
    if entry.get("reason") != "committed_not_pushed":
        return entry
    absent = entry.get("shas_absent_local_only") or []
    verdicts = {reachability_status.get(s) for s in absent}
    entry["reachability_verdicts"] = sorted(v for v in verdicts if v)
    if len(verdicts) == 1:
        only = verdicts.pop()
        if only in _MISROUTED_VERDICTS:
            entry["reason"] = _MISROUTED_VERDICTS[only]
    return entry


def apply_merged_pr_tier1(entry, pr_status, merge_default_status):
    """Correct a flagged committed_not_pushed entry whose absent SHAs ALL belong
    to a MERGED pull request whose merge commit is confirmed on the default
    branch — the work shipped under a rewritten sha. PURE (forge + containment
    injected). g-115-6873 / g-115-6834.

    THE GAP THIS CLOSES, which is a gap BETWEEN correct mechanisms rather than a
    bug in any one of them. `all_merged_on_default` already encodes exactly this
    carve-out, and until now it was reachable ONLY from classify_stranded — i.e.
    only for OFF-DEFAULT shas, commits still reachable from some remote branch.
    That premise ("which remote branch are these commits on?") has no answer once
    the branch is DELETED, and a forge deletes the source branch on merge BY
    DEFAULT. So the moment a squash-merged branch is auto-deleted its sha stops
    being off-default and becomes reachable from no remote ref at all — which is
    TIER 1, where no merged-pull-request check ran. The carve-out protected the
    TRANSIENT state (branch still present) and missed the STEADY one (branch
    cleaned up), so its coverage decayed to zero exactly as a repo tidies up
    after itself. The other two tier-1 correctors decline for reasons that are
    each correct: apply_reachability maps only ABSENT / STRANDED_WORKER_REF away
    from the push remedy, and a squash-merged-then-deleted sha is neither (it is
    still reachable from the pull-request ref); apply_superseded requires
    byte-identity in HEAD, which decays as later commits touch the same files.

    Measured (g-115-6781): a pull request MERGED as a squash (parent_count=1),
    head branch deleted, its head sha reachable from no remote branch, all five
    touched files byte-identical between that sha and the merge commit, and the
    merge commit an ancestor of the default branch. Tier 1 filed a HIGH
    Investigate reading "Push the commit (or re-do the work if it was lost)" — a
    remedy that is impossible (the branch is gone) and, on its second clause,
    destructive (the work is live).

    NOT A SUPPRESSOR, matching apply_reachability and apply_superseded: the entry
    keeps its SHAs and stays in the report under a corrected reason. What it
    stops is the --apply write of a remedy that cannot work.

    CONSERVATIVE — every uncertainty declines to bless, inherited wholesale from
    all_merged_on_default: an UNAVAILABLE forge probe, a CLOSED-unmerged record,
    a null merge_commit_sha, a merge commit not confirmed on the default branch,
    and an empty sha set all keep committed_not_pushed. A sha with NO record at
    all also declines, rather than being skipped over — blessing an entry on the
    strength of the shas that happened to be probed is the one direction this
    must never fail (verify-before-assuming rule 4). An unreachable forge must
    never convert a flagged sweep into a clean one (g-115-3471), and this is the
    lane that detects real deliverable loss (rb-3135 / g-115-2570).

    Runs AFTER apply_superseded and apply_reachability and defers to both: a
    superseded entry has its content in HEAD, and a misrouted verdict is a
    statement about the sha itself — both are more specific than "a pull request
    carrying it merged".
    """
    if entry.get("reason") != "committed_not_pushed":
        return entry
    absent = entry.get("shas_absent_local_only") or []
    records = [pr_status.get(s) for s in absent]
    if not absent or any(r is None for r in records):
        return entry
    if all_merged_on_default(records, merge_default_status):
        entry["reason"] = "benign_merged_pr"
        entry["merged_pull_requests"] = sorted(
            {r.get("number") for r in records if r.get("number")})
    return entry


# ─────────────────────────── impure git probe ───────────────────────────

def discover_candidate_repos():
    """Candidate git repos a completed goal's commit could live in: the MIND
    repo (PROJECT_ROOT) plus every 1-level git repo under each AGENT_WRITE_PATH
    parent (product estate). Impure (filesystem). Deduped, order-stable."""
    repos = []
    seen = set()

    def _add(p):
        rp = str(p)
        if rp not in seen and (p / ".git").exists():
            seen.add(rp)
            repos.append(p)

    _add(PROJECT_ROOT)
    for parent in _resolve_write_parents():
        pp = Path(parent)
        if not pp.is_dir():
            continue
        _add(pp)  # parent may itself be a repo
        try:
            for sub in sorted(pp.iterdir()):
                if sub.is_dir():
                    _add(sub)
        except OSError:
            continue
    return repos


def _resolve_write_parents():
    """Return the AGENT_WRITE_PATH parent dirs (';'-separated). Env first, then
    <agent>/local-paths.conf — the conf is the SSOT and env is usually unset in
    a subprocess (mirrors _target_state._resolve_search_roots). Never raises."""
    awp_env = os.environ.get("AGENT_WRITE_PATH", "")
    if awp_env.strip():
        return [p.strip() for p in awp_env.split(";") if p.strip()]
    try:
        import _paths
        agent = os.environ.get("MIND_AGENT")
        if not agent:
            return []
        conf = _paths.agent_dir(agent) / "local-paths.conf"
        if not conf.is_file():
            return []
        for line in conf.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("AGENT_WRITE_PATH=") and "=" in line:
                raw = line.split("=", 1)[1].strip().strip('"').strip("'")
                return [p.strip() for p in raw.split(";") if p.strip()]
    except Exception:
        pass
    return []


def _git(repo, *args, timeout=15):
    """Run a git command in repo; return (rc, stdout). Never raises."""
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip()
    except Exception:
        return 1, ""


def _fetch_origin(candidate_repos):
    """Refresh each candidate repo's origin/* remote-tracking refs BEFORE
    probe_sha_origin reads them (g-115-2660). probe_sha_origin resolves
    origin-landing via `git branch -r --contains <sha>`, which reads box-LOCAL
    origin/* refs. On a multi-box fleet a commit that landed on the remote from
    ANOTHER box but was never fetched here reads as local-only -> false-positive
    committed_not_pushed (observed g-315-320 / g-315-344 / g-001-49 flagged while
    already on origin/main; a bare `git fetch` flipped all three to clean; and
    g-115-2653's 512328649, which g-115-2659 was wrongly filed to "rebuild").
    One fetch per repo makes the refs current. `git fetch` only updates refs — it
    NEVER pushes and NEVER touches the working tree. Fail-open: a fetch failure
    (offline, no origin remote) is logged to stderr and the sweep proceeds with
    whatever refs exist (degrades to the pre-fix stale-ref behavior, never blocks).
    Returns {repo_str: 'ok'|'failed'|'no-origin'} for observability."""
    result = {}
    for repo in candidate_repos:
        rc_remote, remotes = _git(repo, "remote")
        if rc_remote != 0 or "origin" not in remotes.split():
            result[str(repo)] = "no-origin"
            continue
        rc, _ = _git(repo, "fetch", "origin", "--quiet", timeout=90)
        # : ALSO fetch the worker-Body carrier namespace. A default
        # `git fetch origin` uses the default refspec (+refs/heads/*:
        # refs/remotes/origin/*), so refs/workers/<agent>/<sid> — which
        # iteration-push.sh --push-worker-ref writes and worker-ref-consume.sh
        # reads — is NEVER fetched and never appears under refs/remotes. That is
        # why probe_sha_origin's `branch -r --contains` cannot see it: not a bug
        # in the probe's logic, an absence in what the probe is allowed to read.
        # Explicit refspec per guard-3213, which names this exact population.
        # Fail-open and deliberately SILENT on failure: a repo with no worker
        # refs returns non-zero here as a matter of course, and warning on it
        # would fire on every ordinary product repo every run.
        _git(repo, "fetch", "--prune", "origin",
             "+refs/workers/*:refs/workers/*", "--quiet", timeout=90)
        if rc == 0:
            result[str(repo)] = "ok"
        else:
            result[str(repo)] = "failed"
            print(f"[completed-not-committed-sweep] WARN: `git fetch origin` failed "
                  f"in {repo} (rc={rc}) — proceeding with STALE origin refs; cross-box "
                  f"false-positive committed_not_pushed possible this run (g-115-2660).",
                  file=sys.stderr)
    return result


def probe_sha_origin(sha, candidate_repos):
    """Resolve origin-landing status for one SHA across candidate repos.
    Returns True (on a remote branch), False (local-only, no remote), or
    None (not a valid commit in any candidate repo). Impure."""
    for repo in candidate_repos:
        rc, _ = _git(repo, "cat-file", "-e", f"{sha}^{{commit}}")
        if rc != 0:
            continue  # not in this repo
        rc2, out = _git(repo, "branch", "-r", "--contains", sha)
        if rc2 == 0 and out.strip():
            return True   # contained by >=1 remote branch — landed
        # : SECOND, NAMESPACE-AGNOSTIC READ before concluding
        # local-only. `branch -r` walks refs/remotes/* and NOTHING else, so the
        # Mind/Body architecture's normal delivery carrier —
        # refs/workers/<agent>/<sid> — is structurally invisible to it. Every
        # worker-Body-delivered commit was therefore classified tier-1
        # `committed_not_pushed`, whose prescribed remedy is "Push the commit":
        # a NO-OP, because the commit is already on origin. Measured (zeta,
        # cc-02, 2026-08-11): all three tier-1 goals in that lane
        # (f1297a42e / fd76ce84e / d007f9c73) were provably on
        # origin's refs/workers/alpha/<sid>, so all three premises were false
        # from one cause.
        # `for-each-ref --contains` rather than another `branch` call, because
        # branch/-r cannot address this namespace at all. Reads the LOCAL mirror
        # that _fetch_origin populates via the explicit refspec above; if that
        # fetch failed the mirror is stale or absent and this degrades to the
        # old behaviour rather than erroring (fail-open, matching _fetch_origin).
        rc3, out3 = _git(repo, "for-each-ref", "--contains", sha, "refs/workers/")
        if rc3 == 0 and out3.strip():
            return True   # delivered via a worker-Body carrier ref — landed
        return False      # exists locally, on no remote branch and no worker ref
    return None           # not a real commit anywhere we can see


def build_sha_status(goals, candidate_repos):
    """Probe every unique extracted SHA once (cache). Impure."""
    status = {}
    for g in goals:
        for sha in extract_commit_shas(g):
            if sha not in status:
                status[sha] = probe_sha_origin(sha, candidate_repos)
    return status


def resolve_default_ref(repo):
    """Remote-tracking ref of the repo's DEFAULT branch ("origin/main"), or None
    when it cannot be resolved locally. Impure (git), no network, no mutation.

    origin/HEAD is the authoritative pointer but is absent on some clones (shallow
    / CI / `--no-tags`), so fall back to probing the two conventional names. A
    None result deliberately makes classify_stranded stay silent for that repo
    rather than guess a default branch — guessing wrong would flag every commit
    in the repo as stranded."""
    rc, out = _git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    ref = out.strip()
    if rc == 0 and ref.startswith("refs/remotes/"):
        return ref[len("refs/remotes/"):]
    for cand in ("origin/main", "origin/master"):
        rc2, _ = _git(repo, "rev-parse", "--verify", "--quiet", f"{cand}^{{commit}}")
        if rc2 == 0:
            return cand
    return None


def probe_sha_on_default(sha, candidate_repos, default_refs):
    """True when SHA is contained by its repo's DEFAULT branch, False when it is
    on a remote branch but not the default, None when undeterminable. Impure.
    g-115-3471.

    Uses the same `git branch -r --contains` command family as probe_sha_origin,
    narrowed to the default ref via --list, so error (rc!=0) stays distinguishable
    from a genuine negative (rc==0, empty stdout). `merge-base --is-ancestor`
    would be equally exact but reports both "not an ancestor" and internal error
    as a nonzero rc, which would silently turn a probe failure into a flag."""
    for repo in candidate_repos:
        rc, _ = _git(repo, "cat-file", "-e", f"{sha}^{{commit}}")
        if rc != 0:
            continue  # not in this repo
        ref = default_refs.get(str(repo))
        if not ref:
            return None  # default branch unknown here — cannot judge
        rc2, out = _git(repo, "branch", "-r", "--contains", sha, "--list", ref)
        if rc2 != 0:
            return None  # probe error — undeterminable, never a flag
        return bool(out.strip())
    return None  # not a real commit anywhere we can see


def build_default_status(shas, candidate_repos, default_refs):
    """Probe each SHA once for default-branch containment. {sha: True|False|None}.
    Impure. g-115-3471."""
    status = {}
    for sha in shas:
        if sha not in status:
            status[sha] = probe_sha_on_default(sha, candidate_repos, default_refs)
    return status


def _scope_re(goal_id):
    """Conventional-commit SCOPE anchor: `type(<goal-id>):` / `type(<goal-id>)!:`.

    Deliberately NOT a free-text search for the goal id anywhere in the message.
    See probe_goalid_scoped_on_default for the two false-positive classes that
    distinction kills, both measured."""
    return re.compile(r"^[A-Za-z]+\(" + re.escape(goal_id) + r"\)!?:")


def probe_goalid_scoped_on_default(goal_id, shas, candidate_repos, default_refs):
    """Default-branch commits whose conventional-commit SCOPE is `goal_id`, in
    THE REPO HOLDING `shas`. Returns ["<sha> <subject>", ...]; empty keeps the
    flag. Impure (git), read-only, no network. g-115-6295.

    TWO DISCRIMINATORS, AND BOTH ARE LOAD-BEARING — measured over the five-goal
    cluster (2 whose deliverable had genuinely landed elsewhere, 3 that must
    keep their finding). A bare `git log <default> --grep=<goal-id>` across all
    repos scores 2 true positives and 2 FALSE positives, and the false ones are
    the two most dangerous members of the set:

    1. SCOPE-ANCHORED, not free-text. The Mind repo commits its own goal-queue
       writes as `docs(<flagging-goal>): <flagged-goal> closed completed but its
       commit is stranded...`, so the flagged goal's id appears in the BODY of
       the very commit that filed the Investigate. A free-text grep therefore
       makes this sweep suppress its own correct findings, and gets MORE likely
       to as an Investigate ages and accumulates filing commits. Reverts are the
       same class from the other side: `Revert "Merge pull request #176 from
       zkysar1/fix/g-250-362-obstacle-avoidance"` names the goal id and means
       the OPPOSITE of landed — free-text scored 5 hits on that goal, scope
       anchoring scores 0.
    2. SAME-REPO as the stranded commits. A goal can ship one deliverable and
       strand another: g-115-6217 landed its docs half in the Mind repo as
       `docs(g-115-6217): ...` while its UI half stayed stranded on a DRAFT PR
       in a product repo. That Mind-repo commit is a genuine scope match, so
       discriminator 1 alone still false-positives on it — and suppressing there
       would have told an agent to close a draft whose own body reads "not
       shippable alone". A hit in a different repo says nothing about whether
       THIS repo's stranded commits were superseded.

    With both applied the cluster scores 5 of 5 correct.

    TRI-STATE, following probe_sha_on_default's idiom in this same file: a list
    of hits, [] for "probed and found none", and None for UNDETERMINABLE
    (unlocatable repo, unknown default branch, git error). Both falsy values
    keep the flag — conservative in the same direction — but they are not the
    same claim, and collapsing them would reproduce here the exact defect this
    function exists to fix. The not-landed remedy states "no default-branch
    commit carries this goal's scope", and that is a finding the sweep has NOT
    established when the probe never ran (guard-1641: a zero is ambiguous
    between counted-zero and never-ran).
    """
    repo = next(
        (r for r in candidate_repos
         if any(_git(r, "cat-file", "-e", f"{s}^{{commit}}")[0] == 0
                for s in shas)),
        None)
    if repo is None:
        return None  # cannot locate the holding repo — undeterminable
    ref = default_refs.get(str(repo))
    if not ref:
        return None  # default branch unknown here — undeterminable
    # --grep narrows cheaply; the SUBJECT match below is what decides. Matching
    # in Python rather than with `git log -E --grep '^...'` also sidesteps git's
    # per-LINE anchoring, under which a `^`-anchored pattern can match a body
    # line of an unrelated commit.
    # The cap is NOT arbitrary and must not be silent (guard-1760). `git log`
    # returns NEWEST-FIRST, and discriminator 1's own premise is that
    # audit-trail commits mentioning a goal ACCUMULATE over time — so in this
    # repo a genuine early `fix(<goal-id>):` can be pushed out of the window by
    # later `docs(...)` filing commits, and the probe would silently report
    # not-landed. The failure direction is safe (keep the flag), but a
    # saturated window is not a measurement, so say so rather than let a
    # truncation read as a finding.
    cap = 200
    rc, out = _git(repo, "log", ref, "--grep", goal_id,
                   "--format=%h%x1f%s", "-n", str(cap))
    if rc != 0:
        return None  # probe error — undeterminable, keep the flag
    pat = _scope_re(goal_id)
    lines = out.splitlines()
    hits = []
    for line in lines:
        sha, _, subject = line.partition("\x1f")
        if subject and pat.match(subject):
            hits.append(f"{sha} {subject[:90]}")
    if not hits and len(lines) >= cap:
        # Window saturated with no scope match: the genuine commit may be just
        # past it. UNDETERMINABLE, not "none" — same distinction the tri-state
        # exists for.
        print(f"[completed-not-committed-sweep] WARN: supersession probe for "
              f"{goal_id} saturated its {cap}-commit window in {repo} with no "
              f"scope match — reporting UNDETERMINABLE rather than not-landed.",
              file=sys.stderr)
        return None
    return hits


def build_landed_elsewhere_status(entries, candidate_repos, default_refs):
    """{goal_id: ["<sha> <subject>", ...]} for stranded_open_pr entries. Impure.
    Staged narrowest-last like its sibling probes — only tier-2 stranded entries
    reach it, so a sweep with nothing stranded pays nothing. g-115-6295."""
    status = {}
    for e in entries:
        gid = e.get("goal_id")
        if not gid or gid in status:
            continue
        status[gid] = probe_goalid_scoped_on_default(
            gid, e.get("shas_off_default") or [], candidate_repos, default_refs)
    return status


def _gh(repo, *args, timeout=30):
    """Run a gh command with cwd=repo; return (rc, stdout). Never raises — a
    missing `gh` binary, an unauthenticated shell, or a non-GitHub remote all
    surface as rc!=0, which callers map to UNAVAILABLE (never to a flag)."""
    try:
        r = subprocess.run(
            ["gh", *args], cwd=str(repo),
            capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip()
    except Exception:
        return 1, ""


_PR_UNAVAILABLE = {"state": "UNAVAILABLE", "number": None, "url": None,
                   "title": None, "created_at": None, "merge_commit_sha": None,
                   "draft": None}


def probe_sha_pull_request(sha, candidate_repos):
    """Resolve the pull request carrying SHA. Impure (forge API via `gh`).
    Returns a record whose "state" is OPEN | CLOSED | MERGED | NONE |
    UNAVAILABLE. g-115-3471.

    Uses `repos/{owner}/{repo}/commits/<sha>/pulls`, whose gh placeholders
    resolve from cwd — so no origin-URL parsing and no owner/name bookkeeping.
    Prefers an OPEN pull request when several carry the commit; that is the one
    whose non-merge is the stranding.

    DEGRADES, NEVER ERRORS. Every failure mode — gh absent, not authenticated,
    remote not on a forge, API unreachable, 404 — returns UNAVAILABLE, which
    classify_stranded treats as "do not flag". This is the goal's explicit
    requirement: an unreachable forge must not turn a clean sweep into a flagged
    one."""
    for repo in candidate_repos:
        rc, _ = _git(repo, "cat-file", "-e", f"{sha}^{{commit}}")
        if rc != 0:
            continue  # not in this repo
        rc2, out = _gh(repo, "api",
                       f"repos/{{owner}}/{{repo}}/commits/{sha}/pulls")
        if rc2 != 0:
            return dict(_PR_UNAVAILABLE)
        try:
            prs = json.loads(out or "[]")
        except Exception:
            return dict(_PR_UNAVAILABLE)
        if not isinstance(prs, list) or not prs:
            return {"state": "NONE", "number": None, "url": None,
                    "title": None, "created_at": None, "merge_commit_sha": None,
                    "draft": None}

        def _norm(p):
            raw = (p.get("state") or "").lower()
            if raw == "open":
                state = "OPEN"
            elif p.get("merged_at"):
                state = "MERGED"
            else:
                state = "CLOSED"
            # merge_commit_sha is the REWRITTEN commit a squash- or rebase-merge
            # put on the base branch. It is the only link back from a merged PR
            # to the sha that actually shipped, and without it a squash-merged
            # goal is indistinguishable from an abandoned branch — see
            # all_merged_on_default. Absent on OPEN pull requests, and null on
            # some CLOSED ones; both degrade to "not benign" ().
            # draft is the author's DELIBERATE "not ready to merge" signal, and
            # without it the remedy below reads as an unconditional "merge this"
            # — advice that ships a half-feature. A draft is neither mergeable
            # nor abandoned, which are the only two cases the narrative used to
            # offer. Detection stays unchanged: a goal that closed completed
            # behind a draft PR is still a genuine premature-close and MUST keep
            # being flagged; only the prescription changes ().
            # body carries the ONLY evidence that separates a deliberate hold
            # from a handoff artifact. Without it `draft` alone says "the author
            # marked this not-ready", which is FALSE for the majority of drafts
            # this sweep flags — see pr_declares_merge_gate ().
            return {"state": state, "number": p.get("number"),
                    "url": p.get("html_url"), "title": p.get("title"),
                    "created_at": p.get("created_at"),
                    "merge_commit_sha": p.get("merge_commit_sha"),
                    "draft": p.get("draft"), "body": p.get("body")}

        norms = [_norm(p) for p in prs if isinstance(p, dict)]
        if not norms:
            return {"state": "NONE", "number": None, "url": None,
                    "title": None, "created_at": None, "merge_commit_sha": None,
                    "draft": None}
        return next((n for n in norms if n["state"] == "OPEN"), norms[0])
    return dict(_PR_UNAVAILABLE)  # SHA in no candidate repo — cannot query


def build_pr_status(shas, candidate_repos):
    """Probe each off-default SHA once for its pull request. {sha: record}.
    Impure. g-115-3471."""
    status = {}
    for sha in shas:
        if sha not in status:
            status[sha] = probe_sha_pull_request(sha, candidate_repos)
    return status


def sha_superseded(sha, candidate_repos):
    """True when SHA's changed files are byte-identical in HEAD — the deliverable
    is already present under HEAD's lineage (benign convergent-parallel-fix
    orphan, g-115-3032), NOT a lost deliverable. Impure (git).

    CONSERVATIVE by construction: any inability to determine (SHA in no candidate
    repo, root/merge parent-diff error, no changed files) returns False so the
    entry stays FLAGGED. The direction of safety is deliberate — a missed
    suppression costs one benign Investigate; a wrong suppression would hide a
    real lost deliverable (the exact failure g-115-2570 / rb-3135 exists to
    catch). We compare against HEAD (not origin/main) as the goal specifies:
    if local HEAD is behind origin the diff is non-empty -> the entry stays
    flagged, which is the safe direction (never a false suppression)."""
    for repo in candidate_repos:
        rc, _ = _git(repo, "cat-file", "-e", f"{sha}^{{commit}}")
        if rc != 0:
            continue  # not in this repo
        rc_f, files_out = _git(repo, "diff", "--name-only", f"{sha}^", sha)
        if rc_f != 0:
            return False  # root/merge/parent-resolve error — cannot determine
        files = [f for f in files_out.splitlines() if f.strip()]
        if not files:
            return False  # no changed files — nothing to prove present in HEAD
        # `git diff --quiet <sha> HEAD -- <files>` exits 0 when <sha> and HEAD
        # are identical on <files> (deliverable present in HEAD -> superseded),
        # 1 when >=1 file's content differs/absent (-> keep the flag).
        rc_d, _ = _git(repo, "diff", "--quiet", sha, "HEAD", "--", *files)
        return rc_d == 0
    return False  # SHA in no candidate repo — cannot check, keep the flag


def build_superseded_status(shas, candidate_repos):
    """Probe each flagged local-only SHA once for superseded-in-HEAD status.
    Returns {sha: bool}. Impure. g-115-3032."""
    status = {}
    for sha in shas:
        if sha not in status:
            status[sha] = sha_superseded(sha, candidate_repos)
    return status


def _load_reachability():
    """Import commit-reachability.py by path (its name is not a valid module
    identifier). Returns the module, or None if it cannot be loaded — in which
    case every verdict is unknown and apply_reachability keeps the flag, which
    is the fail-safe direction for a lane that detects real deliverable loss."""
    try:
        import importlib.util
        path = Path(__file__).resolve().parent / "commit-reachability.py"
        spec = importlib.util.spec_from_file_location("_commit_reachability", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def build_reachability_status(shas, candidate_repos, default_refs):
    """Ask the CANONICAL six-valued prober where each flagged SHA actually
    lives. Returns {sha: verdict-string}. Impure. g-115-6060.

    Deliberately routed through commit-reachability.py rather than a second
    local implementation: a boolean re-derived here is exactly the conflation
    apply_reachability exists to undo, and two probers answering the same
    question is how they drift apart.

    Runs only over the already-FLAGGED SHAs — a handful per run (1 on the
    2026-08-12 fleet run, 3 on 2026-08-12 cc-08) — so its per-SHA fetch is
    affordable where it would not be across the ~7k scanned goals. That fetch is
    also load-bearing rather than incidental: the worker-ref namespace is NOT
    fetched by this sweep's own pre-probe, and without it a refs/workers/**
    commit is indistinguishable from a dangling one (rb-4716 — an absence
    conclusion needs a fetch behind it)."""
    mod = _load_reachability()
    if mod is None:
        return {}
    status = {}
    for sha in shas:
        if sha in status:
            continue
        for repo in candidate_repos:
            rc, _ = _git(repo, "cat-file", "-e", f"{sha}^{{commit}}")
            if rc != 0:
                continue  # not in this repo
            target = default_refs.get(str(repo)) or "origin/main"
            try:
                status[sha] = (mod.triage(str(repo), sha, target_ref=target)
                               or {}).get("verdict")
            except Exception:
                status[sha] = None  # unknown -> keeps the flag
            break
    return status


def resolve_shas_by_goal_id(goal_id, candidate_repos):
    """Find commit SHAs whose message carries the iteration-commit scope
    "(<goal_id>)" across candidate repos. Impure (git). Loop-commit messages
    embed the goal-id, not a SHA (rb-3999), so a phantom record with zero SHA
    tokens is still resolvable by id. The PARENTHESIZED form is collision-safe:
    "(g-115-260)" does NOT match a commit tagged "(g-115-2600)" (contrast a bare
    id substring). Returns a deduped, order-stable list of full 40-char SHAs.
    g-115-2600."""
    if not goal_id:
        return []
    needle = f"({goal_id})"
    found = []
    seen = set()
    for repo in candidate_repos:
        rc, out = _git(repo, "log", "--all", "-F", f"--grep={needle}",
                       "--format=%H")
        if rc != 0 or not out:
            continue
        for line in out.splitlines():
            sha = line.strip()
            if sha and sha not in seen:
                seen.add(sha)
                found.append(sha)
    return found


def build_goalid_status(goals, candidate_repos, now,
                        min_age_minutes=30.0, lookback_hours=168.0):
    """For each completed code-deliverable goal that passes the age gate,
    resolve commits by goal-id and probe each. Returns
    {goal_id: {sha: origin_status}}. Impure. BOUNDED to the eligible subset so
    the per-goal `git log --grep` cost falls only on goals in the actionable
    window, not every goal (this sweep is a 24h detective, not a hot path).
    g-115-2600.

    Covers goals that DO name a SHA as well (g-115-3471). Tier 1 still consults
    this map only when the record names no SHA at all, so its behavior is
    unchanged; tier 2 needs both views because it asks a different question.
    Tier 1 asks "did anything land?", where one landing settles it. Tier 2 asks
    "did everything this goal produced reach the default branch?", and a partial
    view of a MULTI-REPO goal answers that wrongly: g-335-190 named one commit
    that reached main in an unprotected repo while its Vinheim half sat on the
    branch of open PR #53, so an extracted-SHA-only view scored it clean. Cost
    of the wider net measured at 70 extra goals / ~14s on a 3,632-goal queue."""
    result = {}
    for g in goals:
        if g.get("status") != "completed":
            continue
        # NO is_code_deliverable gate here (). This map IS the git
        # evidence both consumers consult, so gating it on the prose predicate
        # made the predicate self-confirming: a goal it rejected could never
        # acquire the evidence that would have admitted it. The age window below
        # is what bounds the cost, and it bounds it tightly — measured on the
        # live queue 2026-07-28: 136 goals added, 0.243s each, 33.1s total for a
        # sweep that runs on a 24h cadence. 127 of those 136 (93%) resolved to
        # real commits.
        completed_at = _parse_iso(g.get("completed_at"))
        if completed_at is None:
            continue
        age_minutes = (now - completed_at).total_seconds() / 60.0
        if age_minutes < min_age_minutes or age_minutes > lookback_hours * 60.0:
            continue
        gid = g.get("id")
        if not gid or gid in result:
            continue
        shas = resolve_shas_by_goal_id(gid, candidate_repos)
        if not shas:
            continue
        result[gid] = {s: probe_sha_origin(s, candidate_repos) for s in shas}
    return result


# ─────────────────────────── Investigate filing ───────────────────────────

STRANDED_SIGNAL_PREFIX = "investigate:stranded-unmerged-"


def _existing_investigate(goal_id, all_goals,
                          key_prefix="investigate:completed-not-committed-",
                          source="world"):
    """True when an open Investigate for this goal already exists (dedup on the
    stable origin_signal key). Scans the goals ALREADY read this run — the resolved
    escalation aspiration, where these Investigates are filed, is an active
    aspiration in whichever queue resolve() picked, so a prior
    Investigate is present in `all_goals`. In-memory (no extra daemon round-
    trip) and inherently fail-closed: _read_goals is guard-383 fatal on a read
    error, so we never reach here with a partial queue.

    `key_prefix` selects the class (g-115-3471): the stranded-on-an-unmerged-
    branch lane files under STRANDED_SIGNAL_PREFIX so it dedups independently
    of the committed-not-pushed lane. The two describe the same goal but
    prescribe DIFFERENT remedies — push the commit vs merge the pull request —
    so collapsing them onto one key would let whichever fired first suppress
    the other's actionable Investigate forever.

    `source` is the QUEUE THE FLAGGED GOAL LIVES IN, not the queue this
    Investigate is filed into (always the resolved escalation aspiration —
    world/asp-115 upstream). That asymmetry IS the
    bug guard-2107 names and g-115-4110 fixes: an agent-source `g-001-NN` id
    is unique only within its owner's queue, but the key built from it lands
    in WORLD, where every agent's dedup reads it — so the first agent to file
    would suppress every other agent's genuinely-different goal of the same
    id, forever. Matching BOTH forms keeps in-flight Investigates filed under
    the pre-fix legacy key deduping against themselves."""
    keys = signal_candidates(key_prefix, goal_id, source)
    for g in all_goals:
        if (g.get("origin_signal") in keys
                and g.get("status") in ("pending", "in-progress")):
            return True
    return False


def _file_investigate(entry):
    """File one Investigate goal into the resolved escalation aspiration
    (world/asp-115 upstream). Returns the filed goal
    id or None. Idempotent via _existing_investigate (checked by caller).

    Serves both flag classes. The daemon call, escalation-aspiration routing and error
    handling are identical, so only the title / origin_signal / description
    differ by reason (g-115-3471)."""
    gid = entry["goal_id"]
    # guard-2107 / : qualify the key with the flagged goal's OWNING
    # agent when it came from an agent queue, because these Investigates file
    # into WORLD where every agent's dedup reads them. World-source ids are
    # already globally unique and are left byte-identical.
    gsource = entry.get("source") or "world"
    if entry.get("reason") == "stranded_open_pr":
        pr = entry.get("pull_request") or {}
        # A DRAFT pull request is the author's deliberate "not ready" signal, so
        # neither of the two remedies below applies to it: it is not awaiting a
        # merge, and it is not abandoned. Prescribing "merge the pull request"
        # against a draft is advice to ship a half-feature — the draft is very
        # often the FRONTEND of a feature whose backend has not deployed, where
        # merging yields controls that call services that do not exist. The
        # FINDING is unchanged and still HIGH: closing a goal `completed` while
        # its deliverable cannot ship is a genuine premature-close, and that is
        # worth surfacing whether or not the PR is a draft. Only the remedy
        # forks. ( — measured on a draft PR whose own body said
        # verbatim "merging this alone gives buttons that fail".)
        _is_draft = pr.get("draft") is True
        # A REDONE goal is checked FIRST and outranks the draft fork: when the
        # deliverable is provably on the default branch already, "close it as
        # superseded" is right whether or not the stranded PR is a draft.
        # ( — 4 of 5 remedies in the originating cluster were
        # falsified this way, against a diagnosis correct every time.)
        _landed = entry.get("landed_elsewhere") or []
        if _landed:
            _remedy = (
                "DO NOT MERGE on the strength of this finding. This goal's "
                "deliverable is ALREADY on the default branch under a "
                "DIFFERENT commit — " + "; ".join(_landed[:3]) +
                " — matched by conventional-commit scope in the same repo as "
                "the stranded commits. The pull request below therefore "
                "carries an EARLIER attempt that was redone and abandoned. "
                "Verify that commit covers this goal's scope, then CLOSE the "
                "pull request as superseded. Merging it would re-introduce a "
                "superseded implementation alongside the one already shipped.")
        elif _is_draft:
            # THE DRAFT FLAG DOES NOT MEAN WHAT THIS BRANCH USED TO ASSUME.
            # Until  every draft got the deliberate-hold narrative
            # below, and for most flagged drafts that sentence is simply false:
            # a worker Body opens a draft as a HANDOFF to the reducer, the
            # reducer closes the goal `completed`, and nobody ever marks the PR
            # ready. The deliverable is verified, green, and invisible to users.
            # Measured 2026-08-17 on the four PRs the originating goal named:
            # three declared no gate at all and one declared four markers. The
            # pattern had been re-derived 26 times by this very sweep — once per
            # stranded PR — precisely because both cases produced the same
            # advice, so the classification never happened.
            _gate = pr_declares_merge_gate(pr.get("body"))
            if _gate is False:
                # 0-of-N IS NO EVIDENCE EITHER WAY (guard-4432). This branch
                # used to assert "HANDOFF ARTIFACT", assert the deliverable was
                # "verified and green", and instruct a merge — a positive
                # conclusion AND an action instruction derived from an ABSENCE,
                # on two repos that auto-deploy on merge to default. Measured
                # 2-of-2 counterexamples scored 0-of-6 while carrying gates
                # unmistakable to a reader, and 11 of 32 open drafts sat in the
                # trap fleet-wide. The green claim was never derived from the
                # token scan at all — nothing here reads CI — so it is simply
                # gone rather than softened ().
                _cited = pr_cited_goal_ids(pr.get("body"))
                if _cited:
                    _cite = (
                        " The body cites " + ", ".join(_cited[:3]) + " — "
                        "resolve that goal FIRST; a live or unresolvable "
                        "citation is positive evidence the draft has a stated "
                        "reason to exist.")
                else:
                    _cite = (
                        " The body cites no goal id, which is likewise not "
                        "evidence either way.")
                _remedy = (
                    "That pull request is a DRAFT and NO GATE TEXT MATCHED (" +
                    str(len(_MERGE_GATE_MARKERS)) + " tokens searched: "
                    "'merge gate' / 'do not merge' / 'draft on purpose' / "
                    "'blocked' / 'precondition' / 'until'). THAT IS NO "
                    "EVIDENCE EITHER WAY — it is not evidence the draft flag "
                    "is incidental, and not evidence it is a deliberate hold. "
                    "This scan therefore reaches NO conclusion about why the "
                    "pull request is a draft, makes NO claim about whether its "
                    "checks are green (nothing here reads CI), and issues NO "
                    "merge instruction." + _cite +
                    " REMEDY: a human or an LLM must READ the pull request "
                    "body and decide. If it states a gate this token list "
                    "missed, write that gate back into the body in the "
                    "vocabulary above so the next scan classifies it "
                    "correctly, and leave it draft. Do NOT mark it ready and "
                    "do NOT merge it on the strength of this goal.")
            elif _gate is True:
                _remedy = (
                    "That pull request is a DRAFT and its body DOES declare a "
                    "merge gate, so the author held it deliberately — do NOT "
                    "merge it on the strength of this goal. Read the stated "
                    "precondition (a draft is most often a frontend awaiting a "
                    "backend deploy, where merging ships controls that call "
                    "nothing). Then either find the goal already tracking that "
                    "precondition and confirm it is live and unblocked, or "
                    "file one if none exists — and re-check whether the "
                    "flagged goal should have closed `completed` at all, since "
                    "its deliverable cannot ship. Merge only once the "
                    "precondition lands.")
            else:
                # _gate is None — body unavailable, so the classification is
                # UNKNOWN. Fall back to the conservative deliberate-hold text:
                # advising a merge on a body we could not read is the expensive
                # error, and entries recorded before the body field existed all
                # land here.
                _remedy = (
                    "That pull request is a DRAFT and its body could not be "
                    "read, so whether the author declared a merge gate is "
                    "UNKNOWN — treat it as a deliberate hold and do NOT merge "
                    "it on the strength of this goal. Read the PR body for the "
                    "stated precondition (a draft is most often a frontend "
                    "awaiting a backend deploy, where merging ships controls "
                    "that call nothing). Then either find the goal already "
                    "tracking that precondition and confirm it is live and "
                    "unblocked, or file one if none exists — and re-check "
                    "whether the flagged goal should have closed `completed` "
                    "at all, since its deliverable cannot ship. If the body "
                    "turns out to declare NO gate at all, that is NO EVIDENCE "
                    "EITHER WAY (guard-4432) and NOT a licence to merge — "
                    "read it and decide; do NOT mark it ready on the strength "
                    "of this goal.")
        else:
            # Observation + a verification step, NOT a bare instruction. This
            # sweep reasons only about branch containment and cannot know a
            # target repo's DEPLOY constraints, so it must not prescribe a
            # merge it has no evidence is safe. : a merge on which
            # every conventional signal read green — 23/23 suite on the merge
            # result, 0 conflicts, MERGEABLE/CLEAN, CI's own tests job PASSED —
            # still broke a live place, because that repo's deploy path can
            # UPDATE an existing script but cannot CREATE a new one, so the
            # partial deploy left a top-level require pointing at nothing.
            # An empty supersession result has two meanings and only one of
            # them is a finding. Claiming "no commit carries this scope" when
            # the probe never ran would be this sweep's own defect, committed
            # by its own fix (guard-1641).
            _probed = entry.get("landed_elsewhere_probed") is True
            _remedy = (
                ("No default-branch commit carries this goal's scope, so the "
                 "deliverable does appear genuinely unlanded."
                 if _probed else
                 "The supersession probe could NOT run for this entry (the "
                 "holding repo, its default branch, or the git read was "
                 "unavailable), so whether this goal was redone on another "
                 "branch is UNKNOWN — establish that by hand first.") +
                " Before merging, "
                "verify the pull request is DEPLOYABLE for this repository — "
                "this sweep reasons only about branch containment and cannot "
                "see a repo's deploy constraints, and a merge that satisfies "
                "every conventional signal (tests, CI, MERGEABLE/CLEAN) can "
                "still half-deploy and break a live target. Once deployability "
                "is confirmed, merge it; if the work was abandoned instead, "
                "close the pull request and re-open the goal.")
        body = {
            "title": (f"Investigate: {gid} closed completed but its commit is "
                      f"stranded on an unmerged branch (PR #{pr.get('number')})"),
            "priority": "HIGH",
            "participants": ["agent"],
            "category": "framework-architecture",
            "origin_signal": qualified_signal(STRANDED_SIGNAL_PREFIX, gid, gsource),
            "description": (
                f"completed-not-committed-sweep tier 2 flagged {gid} "
                f"('{entry['title']}', source={entry['source']}, "
                f"completed {entry['age_hours']}h ago). "
                f"Commit(s) on a remote branch but NOT on the repository's "
                f"default branch: {entry['shas_off_default']}"
                f"{' (found by goal-id commit-scope match, not named in the goal record — rb-3999)' if entry.get('resolved_via') == 'goal-id' else ''}. "
                f"Pull request #{pr.get('number')} ({pr.get('url')}) is still "
                f"OPEN{' as a DRAFT' if _is_draft else ''} after "
                f"{pr.get('age_hours')}h."
                f"{' ALSO stranded on: ' + ', '.join('#%s (%s, %s)' % (o['number'], o['state'], o['url']) for o in entry['other_pull_requests']) + ' — merging only the first leaves the rest of this goal invisible.' if entry.get('other_pull_requests') else ''}"
                # States the OBSERVATION and stops. The old text asserted "the
                # deliverable has not reached the default branch, so no user
                # can see it" unconditionally — a conclusion about the
                # DELIVERABLE drawn from evidence about COMMITS, and false
                # exactly when the goal was redone on a second branch. The
                # remedy carries the framing now; this sentence carries facts.
                f" The goal closed status=completed and tier 1 scores it "
                f"landed — any remote branch satisfies that test."
                f"{'' if _landed else ' Those commits have not reached the default branch.'}"
                f" {_remedy}"
                f" g-115-3471 stranded-on-unmerged-branch class."),
        }
    else:
        body = {
            "title": f"Investigate: {gid} closed completed but commit absent from origin",
            "priority": "HIGH",
            "participants": ["agent"],
            "category": "framework-architecture",
            "origin_signal": qualified_signal(
                "investigate:completed-not-committed-", gid, gsource),
            "description": (
                f"completed-not-committed-sweep flagged {gid} "
                f"('{entry['title']}', source={entry['source']}, "
                f"completed {entry['age_hours']}h ago). Reason: {entry['reason']}. "
                f"Commit(s) present locally but on NO remote branch: "
                f"{entry['shas_absent_local_only']}"
                f"{' (found by goal-id commit-scope match, not named in the goal record — rb-3999)' if entry.get('resolved_via') == 'goal-id' else ''}. "
                f"The goal closed status=completed but its code deliverable is not "
                f"on origin past the push-throttle window. Push the commit (or re-do "
                f"the work if it was lost) and confirm origin landing. "
                f"rb-3135 / g-115-2570 completed!=committed class."),
        }
    try:
        # aspirations_add_goal(asp_id, record, source=...) — record is the dict
        # itself; the daemon returns the parsed created-goal record (a dict).
        out = _rt.aspirations_add_goal(ESCALATION_ASP, body, source=ESCALATION_SOURCE)
        if isinstance(out, dict):
            return out.get("id") or (out.get("goal") or {}).get("id")
        return None
    except Exception as e:
        print(f"[completed-not-committed-sweep] filing Investigate for {gid} "
              f"failed: {e}", file=sys.stderr)
        return None


def main():
    ap = argparse.ArgumentParser(
        description=("Flag code-deliverable goals closed status=completed whose "
                     "commit is absent from origin past an age threshold. "
                     "Detective sweep (rb-428 family). g-115-2570."))
    ap.add_argument("--output", choices=["json", "human"], default="json")
    ap.add_argument("--apply", action="store_true",
                    help="File a dedup'd Investigate per flagged goal (default: "
                         "report-only dry run).")
    ap.add_argument("--min-age-minutes", type=float, default=30.0,
                    help="Only flag goals completed at least this many minutes "
                         "ago (push-throttle false-positive guard; default 30).")
    ap.add_argument("--lookback-hours", type=float, default=168.0,
                    help="Only consider goals completed within this window "
                         "(default 168 = 7 days).")
    ap.add_argument("--min-pr-age-hours", type=float, default=24.0,
                    help="Tier 2 only flags a stranded commit whose open pull "
                         "request is at least this old, so a freshly-opened PR "
                         "still in flight is never flagged (default 24).")
    ap.add_argument("--goal", default=None,
                    help="Scope the sweep to ONE goal id. Turns the fleet-wide "
                         "detective sweep into a single-goal probe so a caller "
                         "at CLOSE time can ask 'did THIS goal's work reach the "
                         "default branch?' without re-implementing the tier-2 "
                         "ancestry logic (g-115-3838). Pair with "
                         "--min-age-minutes 0: a just-closed goal is younger "
                         "than the 30-min push-throttle guard and would "
                         "otherwise be filtered out before any check runs.")
    ap.add_argument("--repo-root", action="append", default=None,
                    help="Directory holding local product-repo checkouts, as "
                         "<root>/<repo> or <root>/<org>/<repo>. Repeatable. "
                         "Enables the stranded_deploy_held carve-out: an open "
                         "PR on a repo under an ACTIVE deploy hold is parked, "
                         "not stranded. Domain-free by construction — when "
                         "unpassed (and PRODUCT_REPO_ROOT is unset) the hold "
                         "map is empty, every entry takes the no-information "
                         "branch, and behavior is byte-identical to before.")
    ap.add_argument("--no-fetch", action="store_true",
                    help="Skip the pre-probe `git fetch` across candidate repos. "
                         "For the CLOSE-TIME caller only, where the agent just "
                         "pushed from this box so local origin/* refs are "
                         "already current. Do NOT use for the scheduled fleet "
                         "sweep: the refs a fetch adds are OTHER boxes' pushes, "
                         "and skipping it reintroduces the g-115-2660 cross-box "
                         "false positive (a commit landed elsewhere reads as "
                         "local-only). Measured cost of the fetch on cc-05: "
                         "~24s across 57 repos, unaffected by --goal.")
    args = ap.parse_args()

    now = dt.datetime.now()
    all_goals = _read_goals("world") + _read_goals("agent")
    # Single-goal scoping (). Applied to the goal POPULATION only —
    # every downstream stage (sha_status, goalid_status, both tiers) is left
    # untouched, so a scoped run and a fleet run evaluate the same goal through
    # exactly the same predicates. Filtering here rather than at report time is
    # what keeps that true: a report-time filter would still let OTHER goals'
    # SHAs into build_sha_status and change what this goal is compared against.
    if args.goal:
        all_goals = [g for g in all_goals
                     if str((g or {}).get("id") or "") == args.goal]
    candidate_repos = discover_candidate_repos()
    # : refresh origin/* refs ONCE before probing `branch -r --contains`,
    # else a cross-box commit (landed on the remote from another box, unfetched
    # here) reads local-only and false-positives as committed_not_pushed. Fetch
    # is refs-only (never pushes / never touches the tree) and fail-open.
    # : the fetch is the sweep's dominant cost and it does NOT scale
    # with --goal — it refreshes every discovered repo regardless of scope.
    # Measured on cc-05: a --goal-scoped run still fetched 57 repos and took
    # 24,131ms. That is fine for a 24h detective sweep and disqualifying for a
    # per-close advisory, so --no-fetch exists for the close-time caller ONLY.
    # It is sound exactly there and nowhere else: the closing agent has just
    # pushed FROM THIS BOX, so local origin/* refs already reflect that push by
    # construction. The refs a fetch would add are OTHER boxes' pushes, which
    # are what the cross-box  false-positive guard needs — hence the
    # scheduled sweep must keep fetching. Using --no-fetch for a fleet run would
    # reintroduce exactly that false positive.
    # str(r): candidate_repos holds Path objects and this dict is json.dumps'd
    # in the report — Path keys raise TypeError at serialization, which _fetch_origin
    # avoids by stringifying. Mirror that here or --no-fetch crashes at output time.
    fetch_status = ({str(r): "skipped_no_fetch" for r in candidate_repos}
                    if args.no_fetch else _fetch_origin(candidate_repos))
    sha_status = build_sha_status(all_goals, candidate_repos)
    # : who does each sha's own commit message say it belongs to?
    # Built over exactly the set build_sha_status already resolved, so it adds
    # one `git log -1` per KNOWN sha and none for tokens that are not commits.
    sha_goalid_owners = build_sha_goalid_owners(
        [s for s, st in sha_status.items() if st is not None], candidate_repos)
    goalid_status = build_goalid_status(all_goals, candidate_repos, now,
                                        args.min_age_minutes, args.lookback_hours)

    scanned = 0
    flagged = []
    for g in all_goals:
        scanned += 1
        entry = classify_goal(g, now, sha_status,
                              args.min_age_minutes, args.lookback_hours,
                              goalid_status=goalid_status,
                              sha_goalid_owners=sha_goalid_owners)
        if entry is not None:
            flagged.append(entry)

    flagged.sort(key=lambda e: e["age_hours"], reverse=True)

    # : distinguish a convergent-parallel-fix ORPHAN (benign — the
    # narrow local fix's changed files are already identical in HEAD, so the
    # DELIVERABLE is on origin under a different SHA) from a genuinely lost
    # deliverable. Probe each flagged local-only SHA for superseded-in-HEAD
    # status and mark benign entries; benign entries stay in the report
    # (benign_superseded, log-only) but are NOT Investigate-filed. Preserves the
    # real-loss detection (>=1 changed file's content genuinely absent from
    # HEAD) that  / rb-3135 targets, while cutting the false-positive
    # Investigate noise (: fcb8dd05e superseded by /3024).
    flagged_shas = sorted({
        s for e in flagged for s in (e.get("shas_absent_local_only") or [])})
    superseded_status = build_superseded_status(flagged_shas, candidate_repos)
    for e in flagged:
        apply_superseded(e, superseded_status)
    benign_superseded = [e for e in flagged if e.get("benign_superseded")]
    real_flagged = [e for e in flagged if not e.get("benign_superseded")]

    # ── Tier 2: stranded on an unmerged branch () ──────────────────
    # Tier 1 above scores a commit LANDED when any remote branch contains it, so
    # a deliverable pushed to a feature branch whose PR is never merged reads as
    # clean. Tier 2 re-checks the goals tier 1 blessed: resolve each repo's
    # default branch, keep the landed SHAs that missed it, then ask the forge
    # which pull request carries them. Probes are staged narrowest-last — the
    # network call runs only on the off-default subset, never on every SHA.
    default_refs = {str(r): resolve_default_ref(r) for r in candidate_repos}

    # : tier 1 asked a local-vs-remote BOOLEAN, which cannot tell an
    # unpushed commit from a dangling or worker-ref one — all three satisfy
    # "cat-file yes, remote-branch no". Re-ask the canonical six-valued prober
    # about the SHAs tier 1 flagged and correct the ones whose remedy would have
    # been wrong. Placed here, after the tier-2 preamble, only because it needs
    # default_refs as the ancestry target; it belongs to tier 1 and touches
    # nothing tier 2 produces. Scoped to entries still flagged after the
    # superseded pass, so a benign_superseded entry is never re-probed.
    reachability_status = build_reachability_status(
        sorted({s for e in real_flagged
                for s in (e.get("shas_absent_local_only") or [])}),
        candidate_repos, default_refs)
    for e in real_flagged:
        apply_reachability(e, reachability_status)
    misrouted = [e for e in real_flagged
                 if e.get("reason") in _MISROUTED_VERDICTS.values()]
    real_flagged = [e for e in real_flagged
                    if e.get("reason") == "committed_not_pushed"]

    landed_by_goal = {
        g.get("id"): landed_shas(g, now, sha_status, goalid_status,
                                 args.min_age_minutes, args.lookback_hours,
                                 sha_goalid_owners=sha_goalid_owners)
        for g in all_goals}
    default_status = build_default_status(
        sorted({s for shas in landed_by_goal.values() for s in shas}),
        candidate_repos, default_refs)
    pr_status = build_pr_status(
        sorted({s for s, st in default_status.items() if st is False}),
        candidate_repos)
    # A squash- or rebase-merge rewrites the sha, so an off-default commit whose
    # pull request MERGED is not stranded — its work is on the default branch
    # under merge_commit_sha. Ask the SAME containment prober about that sha
    # (). Staged narrowest-last like the two probes above: only merged
    # records reach it, so a fleet with no merged pull requests pays nothing.
    merge_default_status = build_default_status(
        sorted({r["merge_commit_sha"] for r in pr_status.values()
                if r.get("state") == "MERGED" and r.get("merge_commit_sha")}),
        candidate_repos, default_refs)

    # : give TIER 1 the merged-pull-request carve-out tier 2 has had
    # since . Its own probes are built here rather than reusing the two
    # above, because those are keyed on tier 2's OFF-DEFAULT shas and tier 1's
    # absent shas are a disjoint set by construction — reusing them would look
    # correct and silently return None for every tier-1 sha. Placed after the
    # tier-2 preamble only because it needs default_refs, the same staging reason
    # apply_reachability sits where it does; it belongs to tier 1 and touches
    # nothing tier 2 produces. Staged narrowest-last like every other probe in
    # this sweep: only entries STILL flagged after the superseded and
    # reachability passes reach the forge, so a clean sweep pays nothing.
    tier1_pr_status = build_pr_status(
        sorted({s for e in real_flagged
                for s in (e.get("shas_absent_local_only") or [])}),
        candidate_repos)
    tier1_merge_default_status = build_default_status(
        sorted({r["merge_commit_sha"] for r in tier1_pr_status.values()
                if r.get("state") == "MERGED" and r.get("merge_commit_sha")}),
        candidate_repos, default_refs)
    for e in real_flagged:
        apply_merged_pr_tier1(e, tier1_pr_status, tier1_merge_default_status)
    merged_pr = [e for e in real_flagged
                 if e.get("reason") == "benign_merged_pr"]
    real_flagged = [e for e in real_flagged
                    if e.get("reason") == "committed_not_pushed"]

    repo_roots = args.repo_root or (
        [os.environ["PRODUCT_REPO_ROOT"]]
        if os.environ.get("PRODUCT_REPO_ROOT") else [])
    deploy_hold_status = build_deploy_hold_status(pr_status, repo_roots)

    stranded_all = []
    for g in all_goals:
        entry = classify_stranded(
            g, now, sha_status, default_status, pr_status,
            args.min_age_minutes, args.lookback_hours,
            args.min_pr_age_hours, goalid_status=goalid_status,
            merge_default_status=merge_default_status,
            sha_goalid_owners=sha_goalid_owners,
            deploy_hold_status=deploy_hold_status)
        if entry is not None:
            stranded_all.append(entry)
    stranded_all.sort(key=lambda e: e["age_hours"], reverse=True)
    stranded = [e for e in stranded_all if e["reason"] == "stranded_open_pr"]
    # : tier 2's premise ("this goal's commits sit off-default") does
    # not entail its conclusion ("the deliverable is missing") — a goal redone
    # on a second branch satisfies the first and not the second. Ask whether a
    # default-branch commit carries this goal's scope, in the repo holding the
    # stranded commits, and fork the remedy on the answer. Decorates only; the
    # finding is still filed either way. Staged narrowest-last like every other
    # probe in this sweep: only tier-2 stranded entries reach it.
    landed_elsewhere_status = build_landed_elsewhere_status(
        stranded, candidate_repos, default_refs)
    for e in stranded:
        apply_landed_elsewhere(e, landed_elsewhere_status)
    stranded_no_pr = [e for e in stranded_all if e["reason"] == "stranded_no_pr"]
    squash_merged = [e for e in stranded_all
                     if e["reason"] == "benign_squash_merged"]
    deploy_held = [e for e in stranded_all
                   if e["reason"] == "stranded_deploy_held"]
    pr_probe_unavailable = sorted(
        s for s, r in pr_status.items() if r.get("state") == "UNAVAILABLE")
    if pr_probe_unavailable:
        print(f"[completed-not-committed-sweep] WARN: pull-request lookup "
              f"unavailable for {len(pr_probe_unavailable)} off-default commit(s) "
              f"— those goals are NOT flagged this run (an unreachable forge must "
              f"never turn a clean sweep into a flagged one, g-115-3471).",
              file=sys.stderr)

    investigate_created = []
    if args.apply:
        for entry in real_flagged:
            # source= must mirror what _file_investigate WRITES, or the dedup
            # looks for a key that is never minted and re-files every run.
            if _existing_investigate(entry["goal_id"], all_goals,
                                     source=entry.get("source") or "world"):
                continue
            gid = _file_investigate(entry)
            if gid:
                investigate_created.append(gid)
        # stranded_no_pr is deliberately NOT filed, and the reason is OWNERSHIP,
        # not WIP. This note used to read "a commit on a branch with no open PR
        # may simply be a live working branch"; that reason does not survive this
        # sweep's own population filter, which is already restricted to goals
        # closed status=completed — a completed goal's commit is by definition
        # not work-in-progress. The disposition is unchanged because the real
        # reason is stronger, and it is MEASURED: 2026-08-25 (alpha worker Body,
        # cc-08, ), one full scanned=3012 run held 11 stranded_no_pr
        # goals, and 9 of them sit on a refs/workers/<agent>/<sid> carrier ref —
        # the sanctioned mid-flight state of the worker->reducer handoff, which
        # already has TWO owners: worker-ref-consume prints the ref, its file
        # list, its age and an exact merge command at every iteration close, and
        #  is the recurring goal that disposes those refs. Filing from
        # here would duplicate that owner and open an Investigate against normal
        # operation — the over-filing  exists to bound.
        #
        # THE RESIDUAL IS THE PART WORTH READING. The other 2 in that run
        # ( and , which were also the two OLDEST at ~54h) carry
        # shas that resolve in NO local repo, so they belong to sibling product
        # repos and neither owner above covers them. Anyone wiring this bucket to
        # file should scope it to that class — sha resolves in no candidate repo
        # AND no worker ref contains it — rather than to the bucket as a whole.
        for entry in stranded:
            if _existing_investigate(entry["goal_id"], all_goals,
                                     STRANDED_SIGNAL_PREFIX,
                                     source=entry.get("source") or "world"):
                continue
            gid = _file_investigate(entry)
            if gid:
                investigate_created.append(gid)

    result = {
        "scanned": scanned,
        "candidate_repos": [str(r) for r in candidate_repos],
        "fetch_status": fetch_status,
        "default_refs": default_refs,
        "flagged_count": len(real_flagged),
        "flagged": real_flagged,
        "benign_superseded_count": len(benign_superseded),
        "benign_superseded": benign_superseded,
        "stranded_count": len(stranded),
        "stranded": stranded,
        "stranded_no_pr_count": len(stranded_no_pr),
        "stranded_no_pr": stranded_no_pr,
        "benign_squash_merged_count": len(squash_merged),
        "benign_squash_merged": squash_merged,
        "stranded_deploy_held_count": len(deploy_held),
        "stranded_deploy_held": deploy_held,
        # Every report key stranded_all is partitioned into, named for
        # consumers so a new partition surfaces instead of silently dropping
        # out of a hardcoded subset (guard-1802 / ). Keep in sync
        # with the four partition statements above; the coupling test
        # test_advisory_reads_every_partition_the_producer_emits fails loudly
        # if a consumer stops referencing one.
        "stranded_all_partitions": [
            "stranded", "stranded_no_pr",
            "benign_squash_merged", "stranded_deploy_held",
        ],
        "misrouted_reachability_count": len(misrouted),
        "misrouted_reachability": misrouted,
        "benign_merged_pr_count": len(merged_pr),
        "benign_merged_pr": merged_pr,
        "pr_probe_unavailable": pr_probe_unavailable,
        "investigate_created": investigate_created,
        "applied": bool(args.apply),
        "now": now.isoformat(timespec="seconds"),
    }

    if args.output == "human":
        print(f"scanned={scanned} flagged={len(real_flagged)} "
              f"stranded={len(stranded)} stranded_no_pr={len(stranded_no_pr)} "
              f"benign_superseded={len(benign_superseded)} "
              f"benign_squash_merged={len(squash_merged)} "
              f"stranded_deploy_held={len(deploy_held)} "
              f"misrouted_reachability={len(misrouted)} "
              f"benign_merged_pr={len(merged_pr)} "
              f"repos={len(candidate_repos)} applied={bool(args.apply)}")
        for e in real_flagged:
            print(f"  [{e['reason']}] {e['goal_id']} ({e['source']}): "
                  f"completed {e['age_hours']}h ago | "
                  f"local-only={e['shas_absent_local_only']} "
                  f"via={e.get('resolved_via', 'record-sha')} | {e['title']}")
        for e in stranded:
            pr = e.get("pull_request") or {}
            others = e.get("other_pull_requests") or []
            extra = (" +PRs " + ",".join(f"#{o['number']}" for o in others)) if others else ""
            print(f"  [stranded_open_pr] {e['goal_id']} ({e['source']}): "
                  f"completed {e['age_hours']}h ago | PR #{pr.get('number')}{extra} "
                  f"open {pr.get('age_hours')}h | off-default={e['shas_off_default']} "
                  f"via={e.get('resolved_via', 'record-sha')} | {e['title']}")
        for e in stranded_no_pr:
            print(f"  [stranded_no_pr log-only] {e['goal_id']} ({e['source']}): "
                  f"on a remote branch but not the default branch, no open PR | "
                  f"off-default={e['shas_off_default']} | {e['title']}")
        for e in benign_superseded:
            print(f"  [benign_superseded log-only] {e['goal_id']} ({e['source']}): "
                  f"deliverable present in HEAD under a different SHA | "
                  f"local-only={e['shas_absent_local_only']} | {e['title']}")
        for e in misrouted:
            print(f"  [{e['reason']} log-only] {e['goal_id']} ({e['source']}): "
                  f"tier-1 would have said 'push it', but the canonical prober "
                  f"returns {','.join(e.get('reachability_verdicts') or ['?'])} "
                  f"| local-only={e['shas_absent_local_only']} | {e['title']}")
        for e in merged_pr:
            nums = ",".join(f"#{n}" for n in (e.get("merged_pull_requests") or [])) or "?"
            print(f"  [benign_merged_pr log-only] {e['goal_id']} "
                  f"({e['source']}): tier-1 would have said 'push it', but PR "
                  f"{nums} merged and its merge commit is on the default branch "
                  f"— the sha is unreachable because the head branch is gone, "
                  f"not because the work is | "
                  f"local-only={e['shas_absent_local_only']} | {e['title']}")
        for e in deploy_held:
            pr = e.get("pull_request") or {}
            holders = ", ".join(e.get("deploy_holders") or []) or "unnamed"
            print(f"  [stranded_deploy_held log-only] {e['goal_id']} "
                  f"({e['source']}): PR #{pr.get('number')} is parked behind an "
                  f"ACTIVE deploy hold ({holders}); merging would fire the "
                  f"deploy the hold exists to prevent. Not stranded — no "
                  f"Investigate filed | {e['title']}")
        for e in squash_merged:
            pr = e.get("pull_request") or {}
            print(f"  [benign_squash_merged log-only] {e['goal_id']} "
                  f"({e['source']}): PR #{pr.get('number')} merged; work is on "
                  f"the default branch under the rewritten merge commit | "
                  f"off-default={e['shas_off_default']} | {e['title']}")
    else:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
