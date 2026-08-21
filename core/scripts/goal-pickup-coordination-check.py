#!/usr/bin/env python3
"""Goal-Pickup Coordination Check () — advisory same-surface-race probe.

THE GAP THIS FILLS (the precise complement of the two existing same-surface
guards):

  1. aspirations-select "Partner-claim filter" drops a candidate goal the
     partner is CURRENTLY `in_flight` on — but only while the partner holds the
     claim. Once the partner commits and releases, the in_flight signal is gone.
  2. iteration-commit.sh "concurrent-partner-filter" (g-115-692) drops a
     partner's WIP files at COMMIT time based on mtime vs. partner.claimed_at —
     it protects the commit, not the goal pickup.

NEITHER catches the canonical 2026-05-13 race: zeta SHIPPED g-115-697 at 13:20
(commit landed, no longer in_flight), and alpha claimed g-115-696 at 15:04 —
~4h later — covering the same surface. The in_flight filter passed (zeta idle),
the commit filter never ran (alpha hadn't committed yet). The work was already
done; alpha burned execution time rediscovering it.

This check is the missing third probe: at goal-pickup, BEFORE claiming, look at
what was actually COMMITTED in the last N hours and warn when a recent commit
touched the same surface (files or title-keywords) as the goal about to be
claimed. Two cheap probes — a bounded `git log` over the affected surface,
windowed on COMMITTER TIMESTAMP (see `_ct_cutoff`; NOT `git log --since`, which
is a traversal cutoff — g-115-6959 / guard-4539) + the partner `last_active`
snapshot — exactly as g-305-03 specifies.

UNCOMMITTED EXTENSION (g-115-1505): the committed-log probe above is blind to a
partner's IN-FLIGHT edit that has not been committed yet — the exact 2026-06-16
miss where delta claimed g-250-137 (IntentEngineVerticle) while alpha was
in_flight on g-181-27 editing IntentEngineVerticle.java, uncommitted. So when a
partner is currently in_flight (per team-state), this probe ALSO diffs the
shared working tree (`git diff HEAD` + untracked) and matches those files
against the claiming goal's surface — by path AND by basename-stem. The stem
match is the key: goal prose says "IntentEngineVerticle" with no ".java", so
extract_paths finds nothing and only the lowercased-basename-stem ∩ keywords
match fires. The uncommitted probe is GATED on partner-in_flight because the
working tree is shared (guard-741): with no partner in_flight, uncommitted files
are this agent's own WIP and flagging them would be a self-false-positive.

WHY SURFACE-BASED, NOT AUTHOR-BASED: every agent in this repo commits under the
SAME git identity (`zkysar1`); the agent is encoded in the commit-subject SCOPE
(`feat(g-115-697): ...`), never in `%an`. So author attribution cannot
distinguish a partner's commit from the agent's own. Surface overlap (committed
files ∩ goal-affected paths, or commit-subject keywords ∩ goal-title keywords)
is the durable signal. Commits whose subject scope IS the claiming goal's own id
are excluded — those are the agent's own in-progress work on THIS goal (e.g.
after an autocompact-resume re-claim), not a race.

ADVISORY, NOT A GATE. The affected-paths inference is heuristic (goals describe
their surface in prose, not a structured path list), so a hard block would
freeze legitimate work on a false positive. This script SURFACES the overlapping
commit(s) for the LLM to verify ("read the commit; is this goal already
shipped?") before claiming — the same detective posture as the Phase 0-pre.0c
stash probe and defer-drift-check.py. Exit is always 0; the only signal is the
JSON verdict on stdout.

BOARD EXTENSION (g-001-311): the three probes above (git log, uncommitted
tree, team-state) are ALL partition-blind — on multi-box own-cloud
deployments a partner box's aspirations/team-state uploads can silently stop
propagating (the 2026-07-09 g-115-1876 collision: alpha's 15:50:08 claim
never left its box; bravo's 15:56:34 claim legitimately read claimed_by=null
and both agents executed). The coordination BOARD was the one surface that
DID cross that partition (alpha's board posts arrived while its store writes
did not — guard-997 / rb-3296). So this probe ALSO scans recent coordination
posts from OTHER agents that name the claiming goal-id as a structured claim
("Claiming <id>" prefix, type=claim, or claim+id tags) or completion
("Completed" prefix / type=complete). Bare topical mentions are ignored
(findings routinely name goal ids — flagging them would flood). Recurring
goals skip completion-kind hits (a recurring goal completes every cycle; a
partner's past completion post is history, not a race).

PRODUCT-REPO EXTENSION (g-115-2428): every probe above looks at THIS repo —
but deliverables routinely ship in AGENT_WRITE_PATH product repos (sibling
clones, or a workspace CONTAINER holding N independent clones). The
g-115-2156 miss: the deliverable (an orchestrator-service PR, pushed ~24h
earlier) already existed at claim time, and this check flagged only a
sibling mind-repo commit — it could never see product commits/branches/PRs.
So when the goal prose names a product surface (a full repo name from the
domain's product-repos convention or from disk, a DISTINCTIVE repo-name
token — distinctiveness is frequency-derived, never a hardcoded vocabulary,
so the module stays domain-free — or a literal AGENT_WRITE_PATH path), this
probe ALSO scans each git repo reachable from AGENT_WRITE_PATH (parsed from
agents/<agent>/local-paths.conf, ';'-separated; a non-repo entry contributes
its depth-1 git children): a --product-since-hours git log classified like
the mind probe PLUS a force-include for commits whose subject names the
claiming goal-id (in a product repo that is the strongest already-shipped
evidence, not an own-WIP artifact — the exclusion is deliberately inverted
vs classify_overlap), a goal-id branch scan, and — matched-name repos only,
when gh is authed — a PR search. Commit hits land in overlapping_commits
with repo=<name> attribution; branch/PR hits in product_branch_hits /
product_pr_hits. Network is bounded: fetch + PR search fire only for the
<=3 repos the prose actually names; everything else is local. Fail-open at
every layer: missing conf, absent repos, unauthed gh, any git error — all
skip silently. rb-3743 is the behavioral twin (probe the deliverable
surface before re-implementing); this is its Layer-B automation.

JSON output:
  {
    "goal_id": str,
    "since_hours": float,
    "affected_paths": [str, ...],      # extracted from goal prose
    "keywords": [str, ...],            # significant tokens from goal title
    "race_risk": bool,                 # overlapping commit OR uncommitted OR SURVIVING board claim/complete
    # --- done-but-pending disposition ( / gap-100) ---
    # Answers "has THIS GOAL's own work already shipped?" — orthogonal to
    # race_risk, which answers "is a PARTNER on my surface?". Never folded
    # into race_risk: an already-shipped goal is not a collision.
    "shipped_verdict": "DONE-AND-MERGED" | "OPEN-PR-STALE"
                     | "WORK-EXISTS-UNMERGED" | "CANNOT-SEE"
                     | "GENUINELY-PENDING",
    "shipped_obliges": str,            # what THIS verdict obliges the caller to do
    "own_goal_commits": [              # commits whose SUBJECT names this goal id
      {"hash", "short", "date", "subject", "on_origin_main": True|False|None}
      # RECURRING goals: commits at or before lastAchievedAt are dropped —
      # a recurring id appears in every prior cycle's subject ()
      # None = UNDETERMINABLE (sha absent from this clone) — never "not merged"
    ],
    # product_pr_hits entries additionally carry "live_pr" when the PR is OPEN:
    #   {"mergeable", "mergeStateStatus", "headRefOid",
    #    "checks_state": "green"|"none"|"red"|"unknown"}
    "overlapping_commits": [
      {"hash", "short", "subject", "committed_goal_id",
       "matched_paths": [...], "matched_keywords": [...]}
      # product-repo hits () additionally carry
      # "repo": <name> and "matched_goal_id": bool
    ],
    "matched_uncommitted": [           # partner in-flight (uncommitted) overlaps ()
      {"file", "matched_paths": [...], "matched_stem": str}
    ],
    "board_partner_activity": [        # SURVIVING partner posts ()
      {"id", "author", "timestamp", "kind": "claim"|"complete"|"release", "text"}
    ],
    "board_superseded_claims": [       # claims cleared by that author's later
      {...}                            # explicit release ()
    ],
    "board_stale_claims": [            # claims live partner state contradicts;
      {..., "stale_reason": str}       # carries WHY ()
    ],
    "board_namespace_private": bool,   # true => board lane NOT consulted: this is a
                                       # per-agent record no partner can contend, so
                                       # an empty board_partner_activity above means
                                       # "not applicable", not "checked, clean"
                                       # ()
    "product_surfaces": [str, ...],    # product surfaces the goal prose names ()
    "product_repos_scanned": [str, ...],
    "product_branch_hits": [{"repo", "branch"}],
    "product_pr_hits": [{"repo", "number", "title", "state", "updatedAt"}],
    "partner_in_flight": {"name","goal_id","title","claimed_at"} | null,
    "recent_partners": [{"name", "last_active_minutes": float|None}],
    "advisory": str,                   # one-line human guidance (or "")
    "now": iso,
  }

Guards honored: guard-420 (datetime arithmetic — fromisoformat + Z-strip +
exception-tolerant), guard-645 (field reads default-guarded), guard-614
(structured JSON output), guard-365 (bash wrapper consolidation), guard-165
(no bash-var interpolation into python — args via argv/env), guard-741 (one
shared working tree — the uncommitted probe READS git state, never mutates).
Reference: g-305-03 (US-03), g-115-1505 (uncommitted in-flight extension),
g-001-311 (board probe), g-115-2428 (product-repo extension).
Sibling detective scripts: defer-drift-check.py, unblock-parent-status-sweep.py.
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
import _rt  # canonical Python -> daemon client (post-cutover; see _rt.py)

# Path-like tokens in goal prose: a governed-root-anchored path
# (core/..., .claude/..., world/..., mind_api/..., agents/..., meta/...) OR a
# bare filename with a known code/config extension. Bare extensions alone are
# NOT captured (would match too broadly) — a filename stem is required.
_PATH_RE = re.compile(
    r"(?:core|\.claude|world|mind_api|agents|meta)/[A-Za-z0-9._/-]+"
    r"|\b[A-Za-z0-9_][A-Za-z0-9_.-]*\.(?:py|sh|md|java|ts|tsx|js|lua|ya?ml|json)\b"
)

# Commit-subject scope carries the agent's goal id: feat(): ... or
# chore(gate-d): ... — capture the parenthesized scope token. Goal ids look
# like g-NNN-NN (2-4 trailing digits); the broader pattern also catches
# pt-/asp-/sq- scopes, harmless for the exclude-own-goal check.
_SCOPE_RE = re.compile(r"^[a-z]+\(([^)]+)\)")
_GOAL_ID_RE = re.compile(r"\bg-\d+-\d+\b")

# Generic verbs / articles / framework-filler that carry no surface identity.
# Kept deliberately small — over-stopping erodes the keyword signal.
_STOPWORDS = frozenset({
    "add", "fix", "fixes", "fixed", "the", "and", "for", "with", "from", "that",
    "this", "into", "onto", "over", "under", "before", "after", "when", "then",
    "prevent", "prevents", "check", "checks", "build", "builds", "update",
    "updates", "create", "creates", "make", "makes", "remove", "removes",
    "ensure", "ensures", "verify", "verifies", "goal", "goals", "new", "via",
    "use", "uses", "using", "so", "not", "but", "all", "any", "each", "per",
    "idea", "apply", "investigate", "recurring", "maintain", "unblock",
    "implement", "support", "enable", "handle", "wire", "wired",
    # : boilerplate goal-vocabulary that recurs across nearly every
    # goal title / commit subject and carries NO same-surface identity. The
    # canonical FP (): a goal whose title carried "participants:[agent,
    # user]" matched UNRELATED commit 286090d7d (merge-authority) on "agent" +
    # "user" alone (matched_paths empty) -> race_risk=true on pure goal-record
    # vocab. Sibling to goal-duplication-gate _STOPWORDS (, same FP
    # family). Each is a poor surface discriminator; dropping them keeps
    # race_risk firing on substantive file/topic overlap, not goal-record
    # boilerplate. Expand as new generic-token FP classes surface.
    "agent", "agents", "user", "users", "participant", "participants",
    "aspiration", "aspirations", "source", "status", "field", "fields",
    "verification", "priority", "category", "exists", "global", "populated",
    "class", "close",
})


def extract_paths(text):
    """Return the set of path-like tokens found in goal prose. Pure."""
    if not text:
        return set()
    out = set()
    for m in _PATH_RE.finditer(text):
        tok = m.group(0).rstrip(".,;:)\"'`")
        if tok:
            out.add(tok)
    return out


def extract_keywords(title):
    """Significant lowercase tokens from a goal title (or commit subject).

    Hyphenated compounds are kept whole (goal-pickup, same-surface). Goal-id
    tokens (g-305-03) are stripped before tokenizing so they never count as
    shared keywords. Tokens shorter than 4 chars or in the stopword set are
    dropped. Pure.
    """
    if not title:
        return set()
    s = title.lower()
    # Drop the leading conventional-commit scope and any goal ids first.
    s = _SCOPE_RE.sub("", s)
    s = _GOAL_ID_RE.sub(" ", s)
    # Tokenize on anything that is not a letter/digit/hyphen; keep hyphenated
    # compounds intact.
    raw = re.split(r"[^a-z0-9-]+", s)
    out = set()
    for tok in raw:
        tok = tok.strip("-")
        if len(tok) < 4:
            continue
        if tok in _STOPWORDS:
            continue
        if tok.isdigit():
            continue
        out.add(tok)
    return out


def commit_goal_id(subject):
    """Extract the goal id from a commit subject's scope, else None. Pure."""
    if not subject:
        return None
    m = _SCOPE_RE.match(subject)
    scope = m.group(1) if m else subject
    gm = _GOAL_ID_RE.search(scope)
    return gm.group(0) if gm else None


def _path_overlap(affected, committed_file):
    """True when an affected-path token and a committed file refer to the same
    surface. Match if: same basename, OR the affected token is a directory/path
    prefix of the committed file, OR the committed file ends with the affected
    token at a path boundary ("/" + token). Pure."""
    if not affected or not committed_file:
        return False
    a = affected.strip().rstrip("/")
    c = committed_file.strip()
    if not a or not c:
        return False
    a_base = a.rsplit("/", 1)[-1]
    c_base = c.rsplit("/", 1)[-1]
    # Same filename (both have an extension-bearing basename) is the strongest
    # signal. Guard against a bare directory token (no dot) matching by basename.
    if "." in a_base and a_base == c_base:
        return True
    if c == a or c.startswith(a + "/") or c.endswith("/" + a):
        return True
    return False


def classify_overlap(affected_paths, keywords, commits, own_goal_id,
                     min_shared_keywords=2, agent_queue_private=False, me=""):
    """Pure overlap classifier. `commits` is a list of dicts with keys
    {hash, subject, files}. Returns (race_risk, overlapping_commits) where each
    overlapping entry records WHY it matched. A commit whose own scope goal id
    equals `own_goal_id` is excluded (the agent's own work on this goal).

    THE PRIVATE-QUEUE CARVE-OUT (g-115-6353). The exclusion above says "my own
    in-progress commit", and for a private agent-queue id that premise is simply
    false for a partner's commit: the id collides across queues, so their
    `g-001-01` is a DIFFERENT goal and the exemption is silently swallowing a
    partner's work. Such a commit is re-admitted to classification — but on the
    PATH route ONLY, never the keyword route.

    That asymmetry is measured, not stylistic, and the filing goal prescribed
    plain re-admission. Over alpha's full agent queue against a 168h window
    (2026-08-15, cc-07): re-admitting everything surfaces **60 keyword-only and 1
    path-overlap** hit. The 60 are all one shape — five agents run the identically
    TITLED per-agent recurring goal, so `extract_keywords` matches its own title
    against itself every cycle, forever, on every agent. `race_risk` on g-001-01
    would flip False -> True permanently, fleet-wide, carrying zero information.
    The 1 is real (a partner commit touching `core/scripts/pipeline-archive.sh`,
    a path the goal itself named) and is exactly the silent miss worth closing:
    a SHARED path is contended no matter whose private queue the id lives in.
    60:1 says the keyword route must stay shut and the path route must open.
    """
    overlapping = []
    for cm in commits:
        subject = cm.get("subject") or ""
        cgid = commit_goal_id(subject)
        files = cm.get("files") or []
        keyword_route = True
        foreign = False
        if own_goal_id and cgid == own_goal_id:
            foreign = bool(agent_queue_private) and commit_is_foreign_agent_work(
                files, me)
            if not foreign:
                continue  # the agent's own in-progress commit on THIS goal
            keyword_route = False  # path evidence only — see docstring
        matched_paths = sorted({
            f for f in files
            if any(_path_overlap(ap, f) for ap in affected_paths)
        })
        commit_kw = extract_keywords(subject)
        matched_keywords = sorted(keywords & commit_kw)
        if matched_paths or (
                keyword_route and len(matched_keywords) >= min_shared_keywords):
            overlapping.append({
                "hash": cm.get("hash", ""),
                "short": (cm.get("hash", "") or "")[:9],
                "subject": subject[:100],
                "committed_goal_id": cgid,
                "matched_paths": matched_paths,
                "matched_keywords": matched_keywords,
                # Legible provenance: without it a re-admitted hit reads as an
                # ordinary overlap on a commit whose subject names THIS goal id,
                # which is the most confusing possible presentation (guard-1760).
                "foreign_agent_work": foreign,
            })
    return (len(overlapping) > 0, overlapping)


def _basename_stem(path):
    """Lowercased basename with a single trailing extension stripped. Pure.
    'src/.../IntentEngineVerticle.java' -> 'intentengineverticle';
    'core/scripts/goal-selector.py' -> 'goal-selector'."""
    if not path:
        return ""
    base = path.strip().rstrip("/").rsplit("/", 1)[-1]
    if "." in base:
        base = base.rsplit(".", 1)[0]
    return base.lower()


def classify_uncommitted_overlap(affected_paths, keywords, uncommitted_files):
    """Pure. Match the claiming goal's surface against currently-UNCOMMITTED
    files — a partner's in-flight edits not yet committed (so invisible to the
    git-log probe). Two signals per file:
      - path overlap: the goal named a path that resolves to the file
        (_path_overlap, same as the commit probe).
      - basename-stem match: the file's basename stem (final extension stripped,
        lowercased) appears verbatim in the goal's keyword set. This is the
        g-115-1505 fix — it catches the bare-class-name case where goal prose
        says 'IntentEngineVerticle' (no '.java'), so extract_paths returns
        nothing and a path probe alone would miss the concurrent edit.
    Returns [{file, matched_paths, matched_stem}]. A SINGLE match is sufficient
    (unlike the commit probe's 2-keyword threshold): a basename-stem identity
    against a file under live edit is a strong, specific same-surface signal,
    not fuzzy subject-keyword overlap. The keyword set already dropped stopwords
    and sub-4-char tokens in extract_keywords, so the stem path carries no
    goal-record boilerplate (g-115-1425)."""
    matched = []
    for f in uncommitted_files:
        mpaths = sorted({ap for ap in affected_paths if _path_overlap(ap, f)})
        stem = _basename_stem(f)
        stem_hit = bool(stem) and stem in keywords
        if mpaths or stem_hit:
            matched.append({
                "file": f,
                "matched_paths": mpaths,
                "matched_stem": stem if stem_hit else "",
            })
    return matched


# Goal-id shape (ID Formats: g-NNN-NN through g-NNN-NNNN, plus decomposition
# suffixes like -c). Used to extract WHICH goal a claim post claims.
_GOAL_ID_RE = re.compile(r"g-\d+-\d+(?:-[a-z0-9]+)*", re.IGNORECASE)
# Claim-announce text prefixes: "claim: <id> — <title>" (the  atomic
# announce shape) and the older "Claiming <id> ..." ceremony form.
_CLAIM_TEXT_PREFIX_RE = re.compile(
    r"^\s*claim(?:ing)?[:\s]+(?:goal\s+)?(g-\d+-\d+(?:-[a-z0-9]+)*)",
    re.IGNORECASE)


def _claimed_ids(mtype, text, tags):
    """The goal-id(s) a type=="claim" post STRUCTURALLY claims: every
    goal-id-shaped tag (the ceremony tags the claimed id — g-115-2123 atomic
    announce: tags [<goal-id>, <agent>, ...]) plus the id in a
    "claim: <id>" / "Claiming <id>" text prefix. Body mentions are NOT
    claims. Non-claim-typed posts return empty (their claim form is the
    legacy tag-pair / prefix legs in the caller). Empty set for an
    unparseable claim post — precision-first: an unattributable claim must
    not flip race_risk for every goal its body cites (g-115-2131)."""
    if mtype != "claim":
        return set()
    ids = {t.strip().lower() for t in tags if _GOAL_ID_RE.fullmatch(t.strip())}
    m = _CLAIM_TEXT_PREFIX_RE.match(text)
    if m:
        ids.add(m.group(1).lower())
    return ids


# Release-announce forms (). board.py VALID_MESSAGE_TYPES DOES carry a
# first-class "release" type ("Agent released a goal (failed/abandoned)"), so
# type=="release" + goal-id tags is the strongest leg — exactly symmetric to how
# _claimed_ids reads type=="claim". But agents do not reliably USE it: the
# canonical  release was posted as type=="status" with the id only in
# the prose. So the text-prefix and release-marker-tag legs must ALSO work on
# non-release types. A bare body mention of "releasing" never counts (the regex
# anchors at start-of-text), keeping the precision-first posture that stops
# _claimed_ids flipping race_risk on citations ().
_RELEASE_TEXT_PREFIX_RE = re.compile(
    r"^\s*(?:releas(?:e|ing|ed)|unclaim(?:ing|ed)?|abandon(?:ing|ed)?)"
    r"[:\s]+(?:goal\s+)?(g-\d+-\d+(?:-[a-z0-9]+)*)",
    re.IGNORECASE)
_RELEASE_TAG_MARKERS = frozenset({
    "release", "released", "releasing", "unclaim", "unclaimed", "abandon",
    "abandoned",
})


def _released_ids(mtype, text, tags):
    """The goal-id(s) a post structurally RELEASES — the supersede half of the
    claim/release pair that _claimed_ids opens (g-115-3459).

    Three structural legs, strongest first: (1) type=="release" with the id in
    goal-id-shaped TAGS — the first-class form, unambiguous, no prose needed;
    (2) a release-announce TEXT PREFIX ("RELEASING <id>", "release: <id>",
    "Unclaiming <id>", "Abandoning <id>") on ANY type, because agents commonly
    post releases as type=="status"; (3) a release-marker TAG paired with
    goal-id-shaped tags. A type=="claim" post can never be read as a release —
    the caller checks the claim legs first, so an incoherent post that both
    claims and releases the same id stays classified as a claim (the
    conservative direction: race_risk stays).
    """
    if mtype == "claim":
        return set()
    ids = set()
    if mtype == "release" or _RELEASE_TAG_MARKERS.intersection(
            {str(t).strip().lower() for t in tags}):
        ids |= {t.strip().lower() for t in tags
                if _GOAL_ID_RE.fullmatch(t.strip())}
    m = _RELEASE_TEXT_PREFIX_RE.match(text)
    if m:
        ids.add(m.group(1).lower())
    return ids


def is_private_agent_queue(source, cross_agent_owner=None):
    """Pure. True when this goal-id names a PER-AGENT record that no partner can
    contend, so a partner's board post naming the same id is about a DIFFERENT
    record (g-115-5570).

    Deliberately NOT `source == "agent"` alone. A cross-agent goal (selector
    source='cross-agent:<owner>') also resolves against an agent queue, but names
    ONE shared record that genuinely CAN be contended — blanket-exempting
    source=agent would silently drop the only real race this lane must still
    catch. World goals are never private: one id, one record, existing semantics
    unchanged.
    """
    return source == "agent" and not cross_agent_owner


def commit_is_foreign_agent_work(files, me):
    """Pure. True when this commit is PROVABLY another agent's work: it touches at
    least one `agents/<other>/` path and none of `agents/<me>/` (g-115-6353).

    The GIT companion of is_private_agent_queue above. That predicate established
    that a private agent-queue id names a per-agent record, so the SAME id in two
    queues is two different goals — measured on this box 2026-08-15: 29 of 73
    distinct agent-queue ids exist in more than one queue, and `g-001-01` exists in
    all five. Git history is keyed on that colliding id alone (`type(goal-id):`),
    so both git lanes read every agent's commits as if they were one goal's.

    DELIBERATELY CONSERVATIVE, and the conservatism is the design. Three cases
    return False: the commit touches my dir (mine, or a merge that includes mine);
    it touches NO agent dir at all (framework-only — genuinely unattributable, and
    4.8% of the live g-001-* population, measured over 1,268 commits/90d); or `me`
    is unknown. Only provable foreignness is acted on. The prescribed remedy in the
    filing goal was the mirror image — REQUIRE `agents/<me>/` — which drops that
    4.8% and converts a false DONE into a false PENDING (guard-2499: measure the
    changed predicate's recall over the live population before adopting it).

    The roster is derived from the paths themselves, never a hardcoded partner
    list: any `agents/<X>/` with X != me is someone else's dir, so this tracks
    fleet size instead of the era it was written in (guard-3611).
    """
    me = (me or "").strip()
    if not me or not files:
        return False
    others = False
    for f in files:
        m = _AGENT_DIR_RE.match(f or "")
        if not m:
            continue
        if m.group(1) == me:
            return False
        others = True
    return others


def classify_board_mentions(goal_id, me, messages, goal_recurring=False,
                            goal_last_achieved=None, agent_queue_private=False):
    """Pure. Partner-authored board posts that STRUCTURALLY claim or complete
    this goal-id — the cross-box signal that survives store partitions
    (g-001-311: on 2026-07-09 alpha's aspirations-claim write never propagated
    off its box, but its board posts did; guard-997 / rb-3296 name the board as
    the freshest cross-agent surface).

    kind classification (high-precision by design):
      - "claim":    the post STRUCTURALLY CLAIMS this goal_id — the claimed id
                    is extracted from goal-id-shaped TAGS or a
                    "claim: <id>" / "Claiming <id>" TEXT PREFIX and must equal
                    goal_id. A type=="claim" post whose extracted claimed id is
                    a DIFFERENT goal is dropped even when its body cites this
                    goal_id (g-115-2131: alpha's atomic claim-announce for
                    g-115-2104 said "clears g-115-2084-c Layer 3 suite-green
                    gate" — a citation, not a claim; the pre-fix type-only leg
                    classified it claim-kind and the digest branch would have
                    wrongly yielded). Legacy tag-pair form (tags contain BOTH
                    "claim" and the goal_id) still accepted for non-claim-typed
                    posts.
      - "release":  the post structurally RELEASES this goal_id (see
                    _released_ids). Emitted as a hit rather than dropped so
                    supersede_released_claims can pair it against that same
                    author's earlier claim; a release alone never sets
                    race_risk (g-115-3459).
      - "complete": type == "complete", OR text starts "Completed". (Completes
                    citing other goals' ids share the mention-FP mechanism but
                    are judgment-routed by the digest consumer, not hard-yield
                    — left as-is deliberately.)
      - anything else naming the goal-id is a bare topical MENTION and is
        DROPPED — findings/insight posts routinely cite goal ids
        (affects:<id> tags), and flagging those would flood race_risk.

    Recurring goals skip completion-kind hits (goal_recurring=True): a
    recurring goal completes every cycle, so a partner's past completion post
    is history, not a race. Claims still count EXCEPT stale prior-cycle claims:
    when goal_last_achieved (lastAchievedAt) is passed, a recurring-goal CLAIM
    whose timestamp pre-dates lastAchievedAt was a claim for an already-completed
    cycle and is dropped (g-115-2978). Own-author posts are excluded
    (a prior same-agent session's stranded claim is the stranded-claim sweep's
    job, not a partner race). Returns [{id, author, timestamp, kind, text}].

    agent_queue_private=True SHORT-CIRCUITS to [] (g-115-5570). Agent-queue goal
    ids are NOT globally unique: `agents/<name>/aspirations.jsonl` is per-agent,
    so every agent carries its own g-001-01 with its own interval and its own
    lastAchievedAt. A partner's claim on that id names THAT PARTNER'S record and
    is structurally incapable of contending this one, so no partner post about it
    is evidence about this record — the honest hit count is zero, not "zero after
    filtering by kind". The caller decides privateness (see is_private_agent_queue);
    a CROSS-AGENT goal names one shared record that CAN be contended and is NOT
    private, which is why this is a caller-declared flag rather than
    `source == "agent"`.

    Why the pre-existing stale-prior-cycle drop below could not catch this: it
    compares the partner's claim against the lastAchievedAt of whichever record
    THE PROBER holds. For a world goal there is one record and that is sound; for
    an agent-queue id there are N records with N different lastAchievedAt values,
    so the comparison is against the wrong record by construction. Measured
    2026-08-09 (bravo, cc-05): echo's claim at 20:24:26 was older than echo's own
    lastAchievedAt (20:35:29) and would have been dropped, but was NEWER than the
    prober's (2026-08-02) and so was kept — a false hard-yield with every other
    evidence lane empty.

    me is REQUIRED to be non-empty: with me falsy the self/partner distinction
    is impossible (every author passes `author != me`), so the agent's OWN
    claim post would flag as a partner claim on an autocompact re-claim probe
    and the advisory would wrongly say YIELD. MIND_AGENT injection is
    fail-open and observed to drop (2026-07-13 bravo-fec finding), so this
    returns [] when me is falsy — no-hits is the advisory-safe direction.
    """
    if not me:
        return []
    if agent_queue_private:
        return []  # per-agent record — no partner post can name it ()
    hits = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        author = str(m.get("author") or "")
        if not author or author == me:
            continue
        text = str(m.get("text") or "")
        tags = [str(t) for t in (m.get("tags") or [])]
        if goal_id not in text and goal_id not in tags:
            continue
        mtype = str(m.get("type") or "")
        claim_prefix = re.match(
            r"^\s*claiming\s+(?:goal\s+)?" + re.escape(goal_id) + r"\b",
            text, re.IGNORECASE)
        if goal_id in _claimed_ids(mtype, text, tags) or claim_prefix or (
                mtype != "claim" and "claim" in tags and goal_id in tags):
            kind = "claim"
        elif mtype == "claim":
            # A claim post whose extracted claimed id is a DIFFERENT goal (or
            # unparseable) — this goal_id appears only as a body citation.
            # Not a race signal; dropping prevents the wrong digest yield
            # (, FP specimen msg-20260713-171224-alpha-5101).
            continue
        elif goal_id in _released_ids(mtype, text, tags):
            kind = "release"
        elif mtype == "complete" or text.lstrip().lower().startswith("completed"):
            kind = "complete"
        else:
            continue  # bare mention — dropped (see docstring)
        if kind == "complete" and goal_recurring:
            continue
        # Stale prior-cycle claim drop (): for a recurring goal, a
        # partner CLAIM whose timestamp PRE-DATES this goal's lastAchievedAt was
        # a claim for a now-completed prior cycle — history, not a live race.
        # Dropping it prevents an unnecessary yield of a due, collision-safe
        # recurring goal (incident 2026-07-23: echo claim 15:19:21 pre-dated
        #  lastAchievedAt 15:27:15, echo in_flight on a different goal).
        # Fail-safe: if either timestamp is unparseable, KEEP the hit — the
        # conservative direction leaves race_risk set (a false yield is safer
        # than a missed race).
        if kind == "claim" and goal_recurring and goal_last_achieved:
            try:
                claim_dt = parse_naive_iso(m.get("timestamp"))
                la_dt = parse_naive_iso(goal_last_achieved)
                if claim_dt < la_dt:
                    continue  # stale-cycle claim — not a live race
            except Exception:
                pass  # unparseable → keep (conservative: race_risk stays)
        hits.append({
            "id": m.get("id") or "",
            "author": author,
            "timestamp": m.get("timestamp") or "",
            "kind": kind,
            "text": text[:120],
        })
    return hits


def supersede_released_claims(hits):
    """Pure. Pair claim/release per AUTHOR and let the LATEST event win, so an
    explicit release CLEARS that author's claim (g-115-3459).

    Before this, the probe only ever ACCUMULATED yield signal: it had no notion
    of a release, so once any claim post existed race_risk was true forever, for
    every agent, permanently — an abandoned claim became a permanent lien on the
    goal. Measured cost on g-335-292 (2026-07-27): zeta claimed at 03:59/04:00
    and RELEASED explicitly at 06:18; alpha and bravo yielded four times across
    ~3h, and the probe still returned race_risk=true after both the release AND
    a deadlock-break post had landed.

    Only that author's OWN claims are superseded — a release by zeta cannot
    clear a live claim by bravo. Fail-safe in both directions: an unparseable
    timestamp on either side KEEPS the claim (a false yield is cheaper than a
    missed race, the same posture as the g-115-2978 stale-cycle drop), and a
    release with no preceding claim is simply informational.

    Returns (live_hits, superseded_hits). Order within live_hits is preserved.
    """
    latest_release = {}
    for h in hits or []:
        if h.get("kind") != "release":
            continue
        author = h.get("author") or ""
        ts = h.get("timestamp") or ""
        if not author or not ts:
            continue
        try:
            when = parse_naive_iso(ts)
        except Exception:
            continue  # unparseable release cannot supersede anything
        if author not in latest_release or when > latest_release[author]:
            latest_release[author] = when

    live, superseded = [], []
    for h in hits or []:
        rel = latest_release.get(h.get("author") or "")
        if h.get("kind") == "claim" and rel is not None:
            try:
                if parse_naive_iso(h.get("timestamp")) < rel:
                    superseded.append(h)
                    continue
            except Exception:
                pass  # unparseable claim timestamp → keep (conservative)
        live.append(h)
    return live, superseded


def corroborate_claims(goal_id, hits, live_state, freshness_minutes=60):
    """Pure. Downgrade a claim that LIVE PARTNER STATE positively contradicts
    (g-115-3459 outcome 2) — the second half of distinguishing a live claim from
    a dead one.

    On g-335-292 THREE independent signals said zeta's claim was dead and the
    probe consulted none: zeta's in_flight was null, its current_focus named a
    different goal, and it was demonstrably alive working elsewhere.

    CRITICAL — only POSITIVE evidence of being elsewhere downgrades a claim,
    never mere absence. This is the asymmetry that
    .claude/rules/check-team-state-before-silent.md rule 5 mandates and that
    guard-1560 protects: artifact-absence is NOT clearance, so a null in_flight
    leaves the claim standing. A claimant must ALSO be demonstrably alive within
    freshness_minutes — a stale row means the heartbeat may simply be broken (the
    2026-07-14 incident where two live agents read 59h and 66h stale), and stale
    state must never be read as "not working on it".

    in_flight is the ONLY positive signal consulted. current_focus is
    deliberately NOT used: its production format is "asp-NNN: <goal title>"
    (measured across all 5 live agents, 2026-07-27) and so it is STRUCTURALLY
    incapable of containing a goal-id. Testing `gid not in focus` therefore
    returned True unconditionally, and because in_flight is null for most of a
    goal's life (it is cleared at Phase 5 verify), that branch downgraded EVERY
    claim by every live agent — 5/5 in the measurement — clearing live claims and
    re-opening the double-pickup race this probe exists to prevent. It was caught
    by fresh-eyes minutes after landing because the unit tests fed a synthetic
    focus that DID contain a goal-id: guard-920 / rb-5346 (replicate the literal
    production shape, not the contract-ideal one). Re-adding a focus leg requires
    matching on the goal TITLE, not the id.

    live_state: {agent: {"in_flight_goal_id": str|None,
                         "last_active_minutes": float|None}}

    Returns (live_hits, stale_hits). Non-claim kinds always pass through.
    """
    live, stale = [], []
    for h in hits or []:
        if h.get("kind") != "claim":
            live.append(h)
            continue
        st = (live_state or {}).get(h.get("author") or "")
        if not isinstance(st, dict):
            live.append(h)
            continue
        mins = st.get("last_active_minutes")
        if mins is None or mins > freshness_minutes:
            live.append(h)   # not demonstrably alive → absence, not evidence
            continue
        gid = str(goal_id or "").lower()
        inflight = st.get("in_flight_goal_id")
        elsewhere = False
        why = ""
        if inflight and str(inflight).lower() != gid:
            elsewhere = True
            why = f"in_flight on {inflight}"
        if elsewhere:
            stale.append({**h, "stale_reason":
                          f"{h.get('author')} alive {mins:.0f}m ago but {why}"})
        else:
            live.append(h)
    return live, stale


# ── Product-repo surface probe: pure classifiers () ────────────────

# Repo-name tokens too generic to identify a product surface on their own
# (full repo names still match regardless). Deployment-specific prefixes need
# NO entry here: the frequency rule in detect_product_surfaces drops any token
# shared by many repo names (an org/brand prefix is non-distinctive BY
# CONSTRUCTION), which is what keeps this module domain-free per
# .claude/rules/domain-free-examples.md — no hardcoded brand vocabulary.
_GENERIC_SURFACE_TOKENS = frozenset({
    "server", "servers", "service", "services", "client", "clients", "common",
    "public", "integration", "environment", "environments", "state", "replay",
    "system", "systems", "lambda", "github", "data", "solutions", "research",
    "analyst", "email", "notifications", "notification", "confirmation",
    "request", "tracker", "usage", "instance", "batches", "startup",
    "historical", "saved", "updates", "prompts", "logs", "inference",
    "benchmarking", "assign", "collect", "deploy", "fetch", "forward",
    "manage", "process", "received", "account", "metadata", "stream", "web",
    "app", "api", "url", "status", "domain", "health", "monitor",
    "monitoring", "streaming", "billing", "credit", "precheck", "gate",
    "shutoff", "accept", "deletion", "logged",
})


def detect_product_surfaces(surface_text, repo_names, write_path_entries=(),
                            domain_repos=None):
    """Pure. Which product surfaces (if any) does the goal prose name?
    Four match forms, all case-insensitive at token boundaries:
      - a FULL repo name (compound names are specific enough to match
        anywhere in prose);
      - a SERVED DOMAIN of a repo (domain_repos: {domain -> {repo names}},
        from extract_domain_repos). Goal prose about a web surface routinely
        names the DOMAIN and never the repo — measured g-335-921, whose prose
        says two domains and neither web-app repo name;
      - a DISTINCTIVE single token of a repo name (camelCase + separator
        split; len>=5, not a stopword/generic term, and appearing in fewer
        than max(3, 12.5%) of the repo names — so goal prose that says just
        the orchestrator-service token with no full repo name still selects
        that repo family, while an org/brand prefix shared by many repos
        never triggers. 12.5% because 25% failed live: at a 56-name fleet
        the brand prefix owned 13 names vs a 14-name threshold — off by one,
        and the leaked brand token then burned the <=3 network budget on the
        wrong repos);
      - a literal AGENT_WRITE_PATH entry path appearing in the prose.
    Returns (labels, matched_repos): labels are human-readable surface names
    for the JSON verdict; matched_repos is an ORDERED, deduped list —
    full-name matches FIRST, then domain matches, then token-family matches —
    because the caller spends its bounded network budget (fetch + PR search)
    on the first <=3, and a full-name match is the strongest statement of
    WHICH repo the goal means. A domain ranks second because it names one
    deployed surface, so it is nearly as specific; the token family ranks
    last because it selects a whole family. An entry-path match adds a label
    only — it triggers the scan without singling out one repo for network
    work.

    ORDER IS THE WHOLE FIX for the g-335-921 miss, and the reason this is NOT
    a threshold retune: the brand token had ALREADY leaked (13 owners against
    a 14 threshold — the identical off-by-one the 12.5% retune above was
    written to close, re-opened because the caller unions ~104 convention
    names into the denominator and lifts it back to 14). The deliverable repo
    WAS in matched, at index 7, and [:3] truncated it away. Suppressing the
    leak alone would have made that case WORSE — it would have dropped the
    deliverable repo out of `matched` entirely instead of merely demoting it.
    A domain hit promotes the right repos past the leak without touching the
    threshold, so a leaked family costs ranking rather than correctness."""
    if not surface_text:
        return set(), []
    low = surface_text.lower()
    labels, matched = set(), []

    def _bounded(tok):
        return re.search(
            r"(?<![a-z0-9])" + re.escape(tok) + r"(?![a-z0-9])", low)

    def _add_matched(name):
        if name not in matched:
            matched.append(name)

    def _domain_bounded(dom):
        """`_bounded` is WRONG for a domain and the difference is not cosmetic.
        Its boundary class is [a-z0-9], which excludes letters but NOT '-' or
        '.', so 'brandx.com' matches inside BOTH 'evil-brandx.com' and
        'brandx.com.attacker.net' — different registrable domains that would
        each take one of the three network slots, which is the very failure
        this whole pass exists to fix. Measured on my own first draft.
        LEFT  (?<![a-z0-9-]) rejects a hyphen or letter before, and still
              ADMITS a leading '.', which is what lets one www-stripped key
              match 'example.com', 'www.example.com' and 'api.example.com'.
        RIGHT (?![a-z0-9-]) rejects more label characters; (?!\\.[a-z0-9])
              rejects a further dotted LABEL while still allowing a sentence
              period ('...at example.com.' matches, 'example.com.evil.net'
              does not)."""
        return re.search(
            r"(?<![a-z0-9-])" + re.escape(dom) + r"(?![a-z0-9-])(?!\.[a-z0-9])",
            low)

    # len>=5 floor on FULL names too: convention-file extraction is regex-
    # loose, and a short generic backticked token (e.g. a table header word)
    # must never become a scan trigger. Real repo names clear 5 easily.
    names = [n for n in (repo_names or []) if n and len(n) >= 5]
    for name in names:
        if _bounded(name.lower()):
            labels.add(name)
            _add_matched(name)
    # Served-domain pass. Runs AFTER full names and BEFORE the token family,
    # so a domain hit takes a network slot ahead of a leaked brand token.
    # Keys are already www-stripped by extract_domain_repos, and _bounded's
    # boundary classes are [a-z0-9] (a '.' is neither), so the registrable
    # form matches BOTH 'example.com' and 'www.example.com' in prose — one
    # key, both spellings, no second pattern to keep in sync.
    for dom in sorted(domain_repos or {}):
        d = str(dom).strip().lower()
        if not d or not _domain_bounded(d):
            continue
        labels.add(d)
        for name in sorted(domain_repos[dom]):
            _add_matched(name)
    # Distinctive-token pass: token -> owning repo names.
    tok_names = {}
    for name in names:
        for tok in re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]+|[a-z0-9]+",
                              name):
            tok_names.setdefault(tok.lower(), set()).add(name)
    thresh = max(3, (len(names) + 7) // 8)  # ceil(12.5%), small-list floor 3
    for tok, owners in sorted(tok_names.items()):
        if (len(tok) < 5 or tok in _STOPWORDS
                or tok in _GENERIC_SURFACE_TOKENS or tok.isdigit()
                or len(owners) >= thresh):
            continue
        if _bounded(tok):
            labels.add(tok)
            for name in sorted(owners):
                _add_matched(name)
    for entry in write_path_entries or ():
        e = str(entry).strip().lower()
        if e and e in low:
            base = e.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1]
            labels.add(base or e)
    return labels, matched


def classify_product_overlap(affected_paths, keywords, commits, goal_id,
                             min_shared_keywords=2, goal_recurring=False):
    """Pure. Product-repo variant of classify_overlap (). Two
    deliberate differences from the mind-repo classifier:
      1. NO own-goal exclusion — inverted into a FORCE-INCLUDE: a product
         commit whose subject names THIS goal-id is the strongest
         already-shipped evidence (the g-115-2156 shape: the deliverable
         carries the claiming goal's id). In the mind repo the exclusion
         guards the autocompact-re-claim self-FP; in a product repo the
         advisory posture makes that FP cheap (read the commit, recognize
         own work) while blindness re-does shipped work. EXCEPT for
         recurring goals (goal_recurring=True): a recurring goal legitimately
         ships product commits under its own id every cycle, so the
         force-include would self-flag forever — keyword/path overlap still
         applies, matching the mind-repo posture.
      2. Entries carry matched_goal_id (bool) so the advisory ranks
         id-anchored hits above fuzzy keyword overlap.
    Returns overlapping entries (classify_overlap shape + matched_goal_id;
    the impure caller adds repo=<name>)."""
    _, overlapping = classify_overlap(
        affected_paths, keywords, commits, None,
        min_shared_keywords=min_shared_keywords)
    gid = (goal_id or "").lower()
    if gid and not goal_recurring:
        seen = {e.get("hash") for e in overlapping}
        for cm in commits:
            subject = cm.get("subject") or ""
            if gid in subject.lower() and cm.get("hash", "") not in seen:
                overlapping.append({
                    "hash": cm.get("hash", ""),
                    "short": (cm.get("hash", "") or "")[:9],
                    "subject": subject[:100],
                    "committed_goal_id": commit_goal_id(subject),
                    "matched_paths": [],
                    "matched_keywords": [],
                })
    for e in overlapping:
        e["matched_goal_id"] = bool(gid) and gid in (
            e.get("subject") or "").lower()
    return overlapping


# ── Impure helpers (git, daemon, team-state) ─────────────────────────────────

def _read_goal(source, goal_id):
    """Find the goal record in the given queue via the daemon. Returns the goal
    dict or None. Fail-open: a daemon error returns None (advisory probe must
    never block the claim)."""
    try:
        # active=True (non-terminal goals): a goal at pickup is pending or
        # in-progress, so it is always in this set. A full unfiltered read
        # (active=False) is rejected by the daemon (HTTP 400) as too broad.
        out = _rt.aspirations_read(source=source, active=True)
    except Exception as e:
        print(f"[goal-pickup-coord] {source} read failed: {e}", file=sys.stderr)
        return None
    try:
        data = _rt.tolerant_decode_aggregate(
            f"goal-pickup-coord: {source}", out)
    except Exception:
        data = None
    if data is None:
        return None
    asps = (data.get("aspirations") if isinstance(data, dict) else data) or []
    for asp in asps:
        for g in asp.get("goals", []) or []:
            if g.get("id") == goal_id:
                return g
    return None


# Bound for the count-limited walk that REPLACED `--since` in every probe
# below (). Sized against measured volume in this repo on 2026-08-20:
# 519 commits in the busiest 48h window, 1562 in 168h (the widest default,
# --shipped-since-hours), 12198 reachable from --all in total. 5000 leaves ~3x
# headroom over the widest window while still bounding a pathological repo.
# Cost is not the reason for the bound and never was: --max-count=2000 measured
# 0.290s against --since's 0.249s on the same tree.
_LOG_WALK_MAX = 5000


def _ct_cutoff(since_hours, now_epoch=None):
    """Epoch-seconds cutoff for a since_hours window. Pure.

    Replaces `--since` as this module's time bound. `git log --since` is a
    TRAVERSAL CUTOFF, not a filter: git walks from the tip and STOPS at the
    first commit dated before the cutoff, so ONE old-dated commit at the tip
    hides every recent commit behind it (guard-4539). Measured on a fixture
    2026-08-20: 7 commits 67 seconds old behind one old-dated tip returned
    EMPTY for `--since='60 minutes ago'`, and 7 for a %ct filter. Commit dates
    go non-monotonic in ordinary operation — rebase, cherry-pick,
    `--amend --date`, a merged long-lived branch, peer clock skew.

    Every probe in this module fails in the SILENT direction on an empty
    result: empty AUTHORIZES pickup (no overlap found) or reports
    GENUINELY-PENDING (no shipped work found). So a false empty here does not
    merely under-report — it green-lights duplicate work.

    The predecessor `_since_arg` was deleted rather than kept as a prefilter:
    a `--since` bound can only DROP rows a %ct filter would keep, so retaining
    a convenient helper would just invite the cutoff back in.
    """
    now = int(now_epoch if now_epoch is not None
              else dt.datetime.now().timestamp())
    return now - int(round(float(since_hours) * 3600))


def _within_cutoff(commits, cutoff, key="ct"):
    """Commits whose committer timestamp is at or after `cutoff`. Pure.

    A record missing/unparseable in `key` is KEPT, deliberately: this filter
    replaced a bound whose failure mode was dropping real work, and an
    unreadable timestamp must not silently reproduce that. Over-reporting an
    overlap costs a second look; under-reporting authorizes duplicate work.
    """
    kept = []
    for c in commits:
        raw = c.get(key)
        try:
            if int(raw) >= cutoff:
                kept.append(c)
        except (TypeError, ValueError):
            kept.append(c)
    return kept


def _warn_if_walk_truncated(walked, cutoff, where):
    """Announce on stderr when the count-bounded walk ran out of BUDGET before
    it ran out of WINDOW.

    The bound REPLACED a time bound whose defining flaw was silence: a
    `--since` walk cut short by one old-dated tip returned empty and looked
    exactly like 'nothing happened'. A count bound can under-report too, so the
    difference that matters is not that this bound cannot truncate — it is that
    truncation SAYS SO.

    Hitting the ceiling is NOT by itself truncation, and testing for that alone
    is a false-positive generator: `--max-count` always returns its full budget
    on a repo bigger than the budget, so `len(walked) >= _LOG_WALK_MAX` fires on
    EVERY run of a large repo and teaches the reader to ignore the warning.
    (Measured on the sibling gate while this was being written — it fired on a
    fully-covered window.) The real condition is that the OLDEST commit we
    managed to walk is still inside the window: then in-window commits exist
    beyond the budget that this probe never examined.

    Advisory only; never changes the result, never raises.
    """
    if len(walked) < _LOG_WALK_MAX:
        return
    cts = []
    for c in walked:
        try:
            cts.append(int(c.get("ct")))
        except (TypeError, ValueError):
            continue
    if not cts or min(cts) < cutoff:
        return          # the walk reached past the window — fully covered
    print(f"[goal-pickup-coord] WARNING: {where} walk hit the "
          f"--max-count={_LOG_WALK_MAX} ceiling while still INSIDE the "
          f"window, so in-window commits older than the {_LOG_WALK_MAX}th "
          f"were never examined. Overlap detection may under-report — widen "
          f"_LOG_WALK_MAX if this repo is genuinely this busy.",
          file=sys.stderr)


def _git_fetch_remote(timeout_s=10, cwd=None):
    """Refresh remote-tracking refs before the log scan (). Without
    this, the scan sees only the local clone's history: on 2026-07-15
    (g-335-65) the check ran BEFORE the pre-execution Pull Latest step, so a
    partner's fix pushed 20h earlier (8992abe) was invisible and the REAL
    overlap was missed while a spurious keyword match carried the verdict. A
    fetch updates refs/remotes only — never the shared working tree or index
    (guard-741-safe). Fail-open + bounded (guard-308: default 10s documented
    here): any error, missing remote, or a hang past timeout_s leaves the scan
    on existing local refs — exactly the pre-fix behavior, no regression.
    cwd (g-115-2428): fetch an AGENT_WRITE_PATH product repo instead of
    PROJECT_ROOT — resolved at call time so test monkeypatching of
    PROJECT_ROOT keeps working."""
    try:
        subprocess.run(
            ["git", "fetch", "--quiet"],
            cwd=str(cwd) if cwd else str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=timeout_s, check=False,
        )
    except Exception as e:
        print(f"[goal-pickup-coord] git fetch skipped: {e}", file=sys.stderr)


def _git_behind_count(repo_dir, timeout_s=10):
    """How many commits repo_dir's working tree is BEHIND its origin default
    branch, or None if it cannot be determined (g-115-3303).

    Reads ONLY refs — `git symbolic-ref` + `git rev-list --count` are both
    local, so this adds no network on top of the `_git_fetch_remote(cwd=p)`
    that already ran for this repo. It never touches the working tree or index,
    preserving the `_git_fetch_remote` restraint documented above (guard-741).

    WHY this exists: the "pull before premise-checking" lesson is encoded 78
    times (16 guardrails + 62 rb entries, e.g. guard-1361 / rb-3726) and every
    one is Layer A — the agent remembering. None of them SHOWS the number. A
    grep of a behind checkout returns zero hits and reads authoritative, which
    is how g-335-282's premise read FALSE against a tree 9 commits stale. This
    does not stop anyone grepping a stale tree; it removes not-knowing as the
    excuse.

    Fail-open to None at every layer: a detached HEAD, a repo with no origin,
    an unborn branch, a timeout, or any git error yields None (no advisory)
    rather than an exception — the probe must never block a claim."""
    def _run(args):
        return subprocess.check_output(
            ["git", *args], cwd=str(repo_dir), stderr=subprocess.DEVNULL,
            timeout=timeout_s,
        ).decode("utf-8", "replace").strip()

    try:
        # origin/HEAD is only present when the clone set it; fall back to the
        # two conventional names rather than guessing one.
        upstream = ""
        try:
            upstream = _run(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
        except Exception:
            for cand in ("origin/main", "origin/master"):
                try:
                    _run(["rev-parse", "--verify", "--quiet", cand])
                    upstream = cand
                    break
                except Exception:
                    continue
        if not upstream:
            return None
        out = _run(["rev-list", "--count", f"HEAD..{upstream}"])
        return int(out) if out.isdigit() else None
    except Exception:
        return None


def _parse_name_only_log(out):
    """Parse RS/US-separated `git log --name-only` output into
    [{hash, ct, subject, files}]. Pure; shared by the mind-repo and product-repo
    log probes (g-115-2428 extraction).

    The head is `%H US %ct US %s` (g-115-6959 added `%ct`): both callers dropped
    `--since` for a count-bounded walk plus a committer-timestamp filter, and
    that filter needs the timestamp to reach Python. A head carrying only two
    fields still parses — `ct` comes back None and `_within_cutoff` KEEPS the
    record rather than dropping it, so a format/parser skew degrades to
    over-reporting instead of to the silent empty this change exists to remove.
    """
    rs, us = "\x1e", "\x1f"
    commits = []
    for rec in out.split(rs):
        rec = rec.strip("\n")
        if not rec:
            continue
        head, _, body = rec.partition("\n")
        parts = head.split(us)
        h = parts[0].strip()
        ct = parts[1].strip() if len(parts) >= 3 else None
        subject = parts[2] if len(parts) >= 3 else (
            parts[1] if len(parts) == 2 else "")
        files = [ln.strip() for ln in body.split("\n") if ln.strip()]
        commits.append({"hash": h, "ct": ct, "subject": subject,
                        "files": files})
    return commits


def _git_log_commits(since_hours):
    """Return recent commits as [{hash, subject, files}] over the last
    since_hours, from PROJECT_ROOT. Fetches remote-tracking refs first and
    scans --all refs (g-115-2296): an overlap commit that exists ONLY on
    origin (partner pushed, this box never pulled) is exactly the race this
    probe exists to catch — HEAD-only history is blind to it. --all also
    covers local unpushed commits (same-box partner work), and git log unions
    refs so shared commits appear once. Uses RS/US separators so multi-line
    file lists parse unambiguously. Fail-open: any git error returns []."""
    _git_fetch_remote()
    rs, us = "\x1e", "\x1f"
    fmt = f"{rs}%H{us}%ct{us}%s"
    try:
        out = subprocess.check_output(
            ["git", "log", "--all", f"--max-count={_LOG_WALK_MAX}",
             "--no-merges", "--name-only", f"--format={fmt}"],
            cwd=str(PROJECT_ROOT), stderr=subprocess.DEVNULL,
        ).decode("utf-8", "replace")
    except Exception as e:
        print(f"[goal-pickup-coord] git log failed: {e}", file=sys.stderr)
        return []
    walked = _parse_name_only_log(out)
    cutoff = _ct_cutoff(since_hours)
    _warn_if_walk_truncated(walked, cutoff, "mind repo")
    return _within_cutoff(walked, cutoff)


# ── Product-repo probe: impure helpers () ──────────────────────────

def _parse_write_path_conf(conf_path):
    """AGENT_WRITE_PATH entries (';'-separated, optionally quoted) from a
    local-paths.conf. [] when the file is absent, unreadable, or carries no
    AGENT_WRITE_PATH line — fail-open, the probe must never block pickup on
    conf problems."""
    try:
        text = Path(conf_path).read_text(encoding="utf-8")
    except Exception:
        return []
    entries = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        key, _, val = ln.partition("=")
        if key.strip() != "AGENT_WRITE_PATH":  # exact key — not _EXTRA etc.
            continue
        val = val.strip().strip('"').strip("'")
        for part in val.split(";"):
            part = part.strip()
            if part:
                entries.append(part)
    return entries


def _agent_write_repos(entries):
    """[(name, Path)] of git repos reachable from AGENT_WRITE_PATH entries.
    An entry that IS a git repo contributes itself; an entry that is a plain
    directory (a workspace CONTAINER of independent clones — the documented
    layout where `git log` at the container fails 'not a git repository')
    contributes its depth-1 git children. `.git` may be a dir or a file
    (worktree/submodule) — .exists() covers both. Fail-open: nothing on
    disk → []."""
    repos = []
    for entry in entries or []:
        try:
            p = Path(entry)
            if (p / ".git").exists():
                repos.append((p.name, p))
            elif p.is_dir():
                for child in sorted(p.iterdir()):
                    try:
                        if (child / ".git").exists():
                            repos.append((child.name, child))
                    except Exception:
                        continue
        except Exception:
            continue
    return repos


_CONV_NAME_RE = re.compile(r"`([A-Za-z][A-Za-z0-9_-]{3,})`")
# Backticked org/repo form (`<org>/<repo>`), so a catalog row that writes the
# fully-qualified name still yields the bare repo name.
_CONV_QUALIFIED_RE = re.compile(r"`[A-Za-z0-9_.-]+/([A-Za-z][A-Za-z0-9_-]{3,})`")
_CONV_DOMAIN_RE = re.compile(
    r"(?<![\w.-])((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,})(?![\w-])")
# Closed TLD set, deliberately. An open `\.[a-z]{2,}$` reads env.json, ci.yml,
# main.py and smoke.mjs as domains — a catalog file is full of filenames, and
# every one of them would pair a junk key against a real repo.
_CONV_TLDS = frozenset(("com", "org", "net", "io", "ai", "dev", "wiki"))
# Max distinct known repos a single line may name and still be read as an
# ownership statement. Measured over this deployment's catalog: every hit
# named exactly 1, so 2 is headroom, not a widening. A prose paragraph or a
# contents row that sweeps a domain past several repos asserts no ownership,
# and admitting it would point network work at the wrong repo with the same
# confident tone as a real hit (guard-2860 — never relax an ownership
# predicate into a pattern).
_CONV_DOMAIN_MAX_REPOS = 2
_CAMEL_HUMP_RE = re.compile(r"[A-Z][a-z0-9]*")


def _is_repo_shaped(name):
    """Pure. Does `name` look like a repo name rather than an incidental
    backticked word? A separator, or >=2 CamelCase humps.

    `_CONV_NAME_RE` is deliberately loose — it feeds a NAME set where a stray
    word is inert, because a word that is not a repo simply never matches on
    disk. That tolerance does not survive here: a domain map turns one stray
    word into a LABEL, and a label is what decides whether the product scan
    runs at all. Measured on this deployment's catalog, the loose regex paired
    `github.com -> main` and `schema.org -> Organization` — and `github.com`
    appears in ordinary goal prose constantly, so that one label would have
    triggered a full product scan on unrelated framework goals. Same input,
    two consumers, different tolerance for junk (rb-245 shape: verify the
    field means what the new reader needs, not what the old reader tolerated).
    """
    n = str(name or "")
    if "-" in n or "_" in n:
        return True
    return len(_CAMEL_HUMP_RE.findall(n)) >= 2


def extract_domain_repos(text, known_repos):
    """Pure. {served-domain -> {repo names}} from a product-repo catalog.

    A catalog row states ownership by putting the domain and the repo on the
    SAME LINE ('| `Some-Web-App` | ... | Serves **`www.example.com`** ... |').
    Line scoping IS the predicate: whole-file co-occurrence would pair every
    domain with every repo the file mentions.

    Keys are www-stripped so one key matches both spellings in prose.
    Fail-open by construction: unparseable input yields {}, and the caller
    then behaves exactly as it did before this pass existed."""
    out = {}
    known = {r for r in (known_repos or ()) if r and _is_repo_shaped(r)}
    if not text or not known:
        return out
    for line in str(text).splitlines():
        doms = {d for d in _CONV_DOMAIN_RE.findall(line.lower())
                if d.rsplit(".", 1)[-1] in _CONV_TLDS}
        if not doms:
            continue
        repos = {n for n in _CONV_NAME_RE.findall(line) if n in known}
        repos |= {n for n in _CONV_QUALIFIED_RE.findall(line) if n in known}
        if not repos or len(repos) > _CONV_DOMAIN_MAX_REPOS:
            continue
        for d in doms:
            key = d[4:] if d.startswith("www.") else d
            if len(key) < 5:
                continue
            out.setdefault(key, set()).update(repos)
    return out


def _convention_domain_repos(known_repos):
    """Served-domain -> repo map from the same catalog `_convention_repo_names`
    reads. Empty on any error or when the domain has no such convention —
    fail-open, and domain-free (the glob names a generic convention shape,
    never a specific deployment)."""
    try:
        from _paths import WORLD_DIR
    except Exception:
        return {}
    if WORLD_DIR is None:
        return {}
    out = {}
    try:
        for f in sorted(Path(WORLD_DIR).glob("conventions/*product-repos*.md")):
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for d, rs in extract_domain_repos(text, known_repos).items():
                out.setdefault(d, set()).update(rs)
    except Exception:
        return out
    return out


def _convention_repo_names():
    """Repo names cataloged in the domain's product-repos convention file(s)
    (world/conventions/*product-repos*.md — a resource-locator table of
    backticked repo names per encode-stable-facts.md). Catches repos the
    domain tracks that are NOT cloned on this box. Empty set on any error or
    when the domain has no such convention — fail-open, and domain-free (the
    glob names a generic convention shape, never a specific deployment)."""
    try:
        from _paths import WORLD_DIR
    except Exception:
        return set()
    if WORLD_DIR is None:
        return set()
    names = set()
    try:
        for f in sorted(Path(WORLD_DIR).glob("conventions/*product-repos*.md")):
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for m in _CONV_NAME_RE.finditer(text):
                names.add(m.group(1))
    except Exception:
        return names
    return names


def _git_log_commits_at(repo_dir, since_hours):
    """Recent commits [{hash, subject, files}] in an arbitrary repo dir — the
    product-repo log probe. NO unconditional fetch here: the caller fetches
    ONLY matched-name repos, bounding network to a handful of remotes instead
    of every clone in a workspace container. Scans --all so fetched
    remote-tracking refs are visible. Fail-open: any git error → []."""
    rs, us = "\x1e", "\x1f"
    fmt = f"{rs}%H{us}%ct{us}%s"
    try:
        out = subprocess.check_output(
            ["git", "log", "--all", f"--max-count={_LOG_WALK_MAX}",
             "--no-merges", "--name-only", f"--format={fmt}"],
            cwd=str(repo_dir), stderr=subprocess.DEVNULL,
        ).decode("utf-8", "replace")
    except Exception:
        return []
    walked = _parse_name_only_log(out)
    cutoff = _ct_cutoff(since_hours)
    _warn_if_walk_truncated(walked, cutoff, f"product repo {repo_dir}")
    return _within_cutoff(walked, cutoff)


def _repo_branch_hits(repo_dir, goal_id):
    """Remote-tracking branches in repo_dir whose name carries the claiming
    goal-id — PR-flow work-in-progress on this very goal. Fail-open []."""
    if not goal_id:
        return []
    try:
        out = subprocess.check_output(
            ["git", "branch", "-r", "--list", f"*{goal_id}*"],
            cwd=str(repo_dir), stderr=subprocess.DEVNULL,
        ).decode("utf-8", "replace")
    except Exception:
        return []
    return [ln.strip().lstrip("* ").strip()
            for ln in out.splitlines() if ln.strip()]


def _gh_available(timeout_s=6):
    """One bounded probe: GitHub CLI present AND authenticated? False on any
    failure — the PR probe is optional by contract ('when gh is authed')."""
    try:
        rc = subprocess.run(
            ["gh", "auth", "status"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=timeout_s, check=False,
        ).returncode
        return rc == 0
    except Exception:
        return False


def _gh_pr_hits(repo_dir, goal_id, timeout_s=15):
    """PRs (any state) in repo_dir's GitHub project whose title/body mention
    the goal-id — gh resolves the project from the repo's origin remote, so
    this sees work that never reached the local clone (the g-115-2156 PR was
    authored on another box). Fail-open []."""
    if not goal_id:
        return []
    try:
        out = subprocess.check_output(
            ["gh", "pr", "list", "--state", "all", "--search", goal_id,
             "--limit", "10", "--json", "number,title,state,updatedAt"],
            cwd=str(repo_dir), stderr=subprocess.DEVNULL, timeout=timeout_s,
        ).decode("utf-8", "replace")
        prs = json.loads(out or "[]")
        return prs if isinstance(prs, list) else []
    except Exception:
        return []


def _scan_product_repos(goal_id, surface_text, affected_paths, keywords,
                        since_hours, min_shared_keywords,
                        goal_recurring=False):
    """Impure orchestrator for the product-repo probe (). Detects
    whether the goal prose names a product surface; when it does, scans every
    git repo reachable from AGENT_WRITE_PATH (windowed git log + goal-id
    branch scan), fetching + PR-searching ONLY the matched-name repos
    (bounded network: <=3 fetches + <=3 gh calls, each timeout-bounded;
    everything else is local). Ordinary framework goals that never name a
    product surface pay only a conf parse + dir listing. Fail-open at every
    layer — missing conf, absent repos, unauthed gh, any git error degrade to
    fewer signals, never an exception (advisory probe must never block the
    claim)."""
    empty = {"surfaces": [], "repos_scanned": [], "commits": [],
             "branch_hits": [], "pr_hits": [], "behind": []}
    try:
        me = os.environ.get("MIND_AGENT", "")
        wp_entries = []
        if me:
            try:
                from _paths import agent_dir
                wp_entries = _parse_write_path_conf(
                    agent_dir(me) / "local-paths.conf")
            except Exception:
                wp_entries = []
        repos = _agent_write_repos(wp_entries)
        names = sorted({n for n, _ in repos} | _convention_repo_names())
        # Served-domain map, same catalog as the names above. Keyed on `names`
        # (union, not on-disk) so a domain owned by a repo this box has not
        # cloned still produces a LABEL and triggers the scan; matched_on_disk
        # drops it from the network budget a few lines below, which is the
        # correct place for that filter to live.
        domain_repos = _convention_domain_repos(names)
        labels, matched = detect_product_surfaces(
            surface_text, names, wp_entries, domain_repos=domain_repos)
        if not labels:
            return empty
        result = {"surfaces": sorted(labels), "repos_scanned": [],
                  "commits": [], "branch_hits": [], "pr_hits": [],
                  "behind": []}
        # Network budget follows MATCH order (full-name first, then served
        # domain, then token family), not disk order — the live 2026-07-17
        # replay showed a token-family leak spending all 3 slots on
        # alphabetically-early repos while the full-name-matched repo (the one
        # holding the deliverable PR) got no fetch/PR search. The same leak
        # recurred on  (2026-08-07), which is why the domain tier
        # exists; see detect_product_surfaces for why ordering, not the
        # frequency threshold, is the fix.
        #
        # This gate is shared by THREE consumers, so the domain tier changes
        # all three together and the budget stays <=3 for each:
        #   _gh_pr_hits       — the measured miss. Now searches the repo the
        #                       goal is about instead of an alphabetical
        #                       neighbour.
        #   _git_fetch_remote — same slot count, better aimed. A fetch of the
        #                       deliverable repo is what makes the two reads
        #                       below mean anything.
        #   _git_behind_count — reads the fetch above, so its ADVISORY now
        #                       describes a repo the goal will actually be
        #                       graded on. Its own falsy-conflation defect
        #                       (None and 0 render alike) is untouched here
        #                       and stays with .
        by_name = {}
        for n, p in repos:
            by_name.setdefault(n, p)
        matched_on_disk = [(n, by_name[n]) for n in matched
                           if n in by_name][:3]
        gh_ok = _gh_available() if matched_on_disk else False
        for n, p in matched_on_disk:
            _git_fetch_remote(cwd=p)
            # Reuses the fetch above — ref-only reads, no extra network, no
            # working-tree mutation. Quiet on the common case (current repo →
            # 0 → nothing emitted); None means undeterminable, also quiet.
            _behind = _git_behind_count(p)
            if _behind:
                result["behind"].append({"repo": n, "count": _behind})
                print(
                    f"[goal-pickup-coord] ADVISORY: product repo '{n}' is "
                    f"{_behind} commit(s) behind origin — a grep of this "
                    f"working tree is reading a stale snapshot "
                    f"(guard-1361/rb-3726: pull before premise-checking).",
                    file=sys.stderr,
                )
            if gh_ok:
                for pr in _gh_pr_hits(p, goal_id):
                    entry = {"repo": n, **pr}
                    # : re-read the LIVE mergeable/checks state for
                    # OPEN PRs only. Gated on state so the common case (no PR,
                    # or a long-merged one) costs nothing, and so the extra
                    # network is spent exactly where the recorded verdict is
                    # known to expire under it (guard-3034).
                    if str(pr.get("state", "")).upper() == "OPEN":
                        live = _live_pr_state(p, pr.get("number"))
                        if live:
                            entry["live_pr"] = live
                    result["pr_hits"].append(entry)
        for n, p in repos:
            commits = _git_log_commits_at(p, since_hours)
            if commits:
                hits = classify_product_overlap(
                    affected_paths, keywords, commits, goal_id,
                    min_shared_keywords=min_shared_keywords,
                    goal_recurring=goal_recurring)
                for e in hits:
                    e["repo"] = n
                result["commits"].extend(hits)
            for br in _repo_branch_hits(p, goal_id):
                result["branch_hits"].append({"repo": n, "branch": br})
            result["repos_scanned"].append(n)
        return result
    except Exception as e:
        print(f"[goal-pickup-coord] product-repo scan failed: {e}",
              file=sys.stderr)
        return empty


def _git_uncommitted_files():
    """Files with uncommitted changes in the shared working tree — tracked
    modifications/staged (`git diff --name-only HEAD`) + untracked-not-ignored
    (`git ls-files --others --exclude-standard`), from PROJECT_ROOT. With a
    partner in_flight these are that partner's not-yet-committed edits
    (guard-741: one shared working tree across all agents). Paths are
    repo-relative with forward slashes, matching _git_log_commits' --name-only
    output so _path_overlap treats both identically. Fail-open: any git error
    contributes nothing."""
    files = set()
    for cmd in (
        ["git", "diff", "--name-only", "HEAD"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    ):
        try:
            out = subprocess.check_output(
                cmd, cwd=str(PROJECT_ROOT), stderr=subprocess.DEVNULL,
            ).decode("utf-8", "replace")
        except Exception as e:
            print(f"[goal-pickup-coord] {' '.join(cmd[:3])} failed: {e}",
                  file=sys.stderr)
            continue
        for ln in out.split("\n"):
            ln = ln.strip()
            if ln:
                files.add(ln)
    return sorted(files)


def _board_recent_mentions(since_hours):
    """Coordination-channel messages from the last since_hours via the daemon
    (GET /v1/board/read, JSONL mode). The board is the freshest cross-box
    surface: on partitioned multi-box deployments board writes propagate where
    aspirations/team-state writes lag or freeze (guard-997). Fail-open: any
    error returns [] (advisory probe must never block the claim)."""
    try:
        out = _rt.rt_call("GET", "/v1/board/read", query={
            "channel": "coordination",
            "since": f"{max(1, int(round(since_hours)))}h",
            "json": "1",
        })
    except Exception as e:
        print(f"[goal-pickup-coord] board read failed: {e}", file=sys.stderr)
        return []
    msgs = []
    for line in (out or "").splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            msgs.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return msgs


def _partner_in_flight():
    """Best-effort: the partner agent currently in_flight, from team-state.yaml.
    'Partner' = any agent_status entry other than $MIND_AGENT. Returns
    {name, goal_id, title, claimed_at} for the most-recently-claimed in_flight
    partner, or None. Fail-open: any error -> None."""
    try:
        from _paths import WORLD_DIR
        import yaml
    except Exception:
        return None
    if WORLD_DIR is None:
        return None
    ts_path = Path(WORLD_DIR) / "team-state.yaml"
    data = {}
    try:
        if ts_path.exists():
            data = yaml.safe_load(ts_path.read_text(encoding="utf-8")) or {}
    except Exception:
        data = {}
    #  sharding: overlay per-agent row files (rows win newest-wins)
    # so partner in_flight reads the sharded truth.
    try:
        from _team_state import compose_state
        data = compose_state(data, Path(WORLD_DIR))
    except Exception:
        pass
    if not data.get("agent_status"):
        return None
    import os
    me = os.environ.get("MIND_AGENT", "")
    candidates = []
    for name, info in (data.get("agent_status") or {}).items():
        if name == me:
            continue
        inf = (info or {}).get("in_flight")
        if isinstance(inf, dict) and inf.get("goal_id"):
            candidates.append({
                "name": name,
                "goal_id": inf.get("goal_id"),
                "title": inf.get("title") or "",
                "claimed_at": inf.get("claimed_at") or "",
            })
        # Body-keyed rows (). A partner running as a WORKER Body never
        # writes `in_flight` -- team-state-in-flight.sh skips the reducer stamp
        # for any non-reducer body -- so before this loop existed, a worker
        # partner was invisible here and this probe's whole in_flight GATE
        # (guard-741) silently opened: with no partner in_flight, uncommitted
        # files in the SHARED working tree read as unowned and get swept into
        # this agent's commit. That is the exact hazard the gate exists for, so
        # the miss is not cosmetic.
        #
        # Each body is a separate candidate rather than being merged: they are
        # genuinely concurrent claims on different goals, and the newest-wins
        # sort below already picks the most recent across both shapes. Non-dict
        # entries are skipped. As of  a cleared body row is DELETED
        # (POST /v1/team-state/clear-body-row), so the common case never reaches
        # this guard -- but keep it: pre-fix null residue survives on any box that
        # has not yet run a close, and a hand-edit can still leave one.
        bodies = (info or {}).get("in_flight_bodies")
        if isinstance(bodies, dict):
            for _sid, body in bodies.items():
                if not isinstance(body, dict) or not body.get("goal_id"):
                    continue
                candidates.append({
                    "name": name,
                    "goal_id": body.get("goal_id"),
                    "title": body.get("title") or "",
                    "claimed_at": body.get("claimed_at") or "",
                })
    if not candidates:
        return None
    candidates.sort(key=lambda d: d.get("claimed_at") or "", reverse=True)
    return candidates[0]


def _read_partners(since_minutes=360):
    """Best-effort partner last_active snapshot from team-state.yaml. Returns a
    list of {name, last_active_minutes}. Fail-open: any error returns []."""
    try:
        from _paths import WORLD_DIR
        import yaml
    except Exception:
        return []
    if WORLD_DIR is None:
        return []
    ts_path = Path(WORLD_DIR) / "team-state.yaml"
    data = {}
    try:
        if ts_path.exists():
            data = yaml.safe_load(ts_path.read_text(encoding="utf-8")) or {}
    except Exception:
        data = {}
    #  sharding: overlay per-agent row files (rows win newest-wins)
    # so partner liveness reads the sharded truth.
    try:
        from _team_state import compose_state
        data = compose_state(data, Path(WORLD_DIR))
    except Exception:
        pass
    if not data.get("agent_status"):
        return []
    import os
    me = os.environ.get("MIND_AGENT", "")
    now = dt.datetime.now()
    out = []
    for name, info in (data.get("agent_status") or {}).items():
        if name == me:
            continue
        la = (info or {}).get("last_active")
        mins = None
        if la:
            try:
                t = parse_naive_iso(la)
                # Cross-machine clock/TZ skew (): a partner whose
                # machine clock is ahead writes a future-dated last_active;
                # now (machine-local naive) - t goes negative. Clamp to 0 — a
                # future-dated liveness stamp means "just active". Skew-safe.
                mins = max(0.0, round((now - t).total_seconds() / 60, 0))
            except Exception:
                mins = None
        if mins is not None and mins <= since_minutes:
            out.append({"name": name, "last_active_minutes": mins})
    out.sort(key=lambda d: (d["last_active_minutes"] is None,
                            d["last_active_minutes"] or 0))
    return out


def _agent_live_state():
    """Best-effort per-agent live state for corroborate_claims ():
    {agent: {in_flight_goal_id, current_focus, last_active_minutes}}.

    Unlike _partner_in_flight (which returns only the single most-recently-
    claimed partner) this is keyed BY AGENT, because a claim must be corroborated
    against ITS OWN author's row. Fail-open: any error returns {} , which makes
    corroborate_claims a no-op and leaves every claim standing — the safe
    direction (guard-1560: absence is never clearance).

    NOTE: this is the third team-state loader in this module (alongside
    _partner_in_flight and _read_partners), all three duplicating the
    load+compose_state shape. Consolidating them is out of scope for this fix
    and is filed separately rather than inlined here.
    """
    try:
        from _paths import WORLD_DIR
        import yaml
    except Exception:
        return {}
    if WORLD_DIR is None:
        return {}
    ts_path = Path(WORLD_DIR) / "team-state.yaml"
    data = {}
    try:
        if ts_path.exists():
            data = yaml.safe_load(ts_path.read_text(encoding="utf-8")) or {}
    except Exception:
        data = {}
    try:
        from _team_state import compose_state
        data = compose_state(data, Path(WORLD_DIR))
    except Exception:
        pass
    if not data.get("agent_status"):
        return {}
    import os
    me = os.environ.get("MIND_AGENT", "")
    now = dt.datetime.now()
    out = {}
    for name, info in (data.get("agent_status") or {}).items():
        if name == me:
            continue
        info = info or {}
        inf = info.get("in_flight")
        mins = None
        la = info.get("last_active")
        if la:
            try:
                # Same clock-skew clamp as _read_partners ().
                mins = max(0.0, round(
                    (now - parse_naive_iso(la)).total_seconds() / 60, 0))
            except Exception:
                mins = None
        out[name] = {
            "in_flight_goal_id": (inf.get("goal_id")
                                  if isinstance(inf, dict) else None),
            "current_focus": info.get("current_focus"),
            "last_active_minutes": mins,
        }
    return out


# --- Done-but-pending DISPOSITION ( / gap-100) ------------------
#
# Everything above answers "is a PARTNER racing me on the same SURFACE?".
# gap-100 asks a different question at the same moment: "has THIS GOAL's own
# work already shipped?" — and after nine identical hand-runs the answer was
# still being re-derived from memory each time.
#
# WHY THIS EXTENDS THE RACE PROBE RATHER THAN FORGING A SEPARATE SKILL. The
# evidence is ALREADY being gathered here and then deliberately thrown away:
# classify_overlap skips every commit whose subject scope IS this goal's id
# ("the agent's own in-progress commit on THIS goal"), which is exactly right
# for the race question and exactly the evidence the shipped question needs.
# _repo_branch_hits and _gh_pr_hits are already goal-id-keyed. And this script
# already RUNS at Phase 4 pre-claim (aspirations-loop-digest.md), so the
# disposition arrives with nothing to remember — a separate skill would have
# reproduced the reach failure gap-100 was filed about.
#
# WHAT IS DELIBERATELY NOT DECIDED HERE. gap-100 names PARTIALLY-DONE as a
# fourth verdict (: a merged PR covering 5 of 6 verify steps). Judging
# verify-step coverage is reading, not matching, so this classifier does not
# emit it — it emits DONE-AND-MERGED and OBLIGES the caller to check coverage
# before closing. A fabricated PARTIALLY-DONE would be the more dangerous
# output: it reads as a measurement.

_RS, _US = "\x1e", "\x1f"

_EXTERNAL_PREFIXES = ("world/", "meta/")

# Routed through _paths so an AGENTS_PARENT_DIR rename tracks automatically —
# CLAUDE.md's "Agent-dir Resolution" audit greps cannot see a literal "agents/"
# baked into a prefix check, and this file would be invisible to all three.
try:
    from _paths import AGENTS_PARENT_DIR as _APD
except Exception:
    _APD = "agents"
_STORE_PREFIXES = ((_APD.rstrip("/") + "/"),) if _APD else ()

# Same routing, one capture group deeper: which agent's dir a file lives in.
# Consumed by commit_is_foreign_agent_work (defined earlier — Python resolves
# module globals at CALL time, and keeping every AGENTS_PARENT_DIR derivation in
# this one block is worth more than co-location with its single caller).
_AGENT_DIR_RE = re.compile(
    r"^" + re.escape(_APD.rstrip("/")) + r"/([^/]+)/") if _APD else re.compile(r"(?!)")


def _surface_is_external(affected_paths):
    """True when every path the goal prose names lives under an EXTERNAL,
    gitignored root (world/, meta/). Pure.

    Load-bearing, and the reason CANNOT-SEE exists as a verdict (g-335-986):
    when the deliverable is under world/scripts, `git log --grep <goal-id>`
    returns nothing for a goal that was fully executed and shipped. Git
    absence there is BLINDNESS, not evidence of absence — reporting it as
    GENUINELY-PENDING is how a finished goal gets re-implemented. Same class
    as guard-1947 (full-suite-recommender reports "no code changes" for every
    domain-script edit ever made, because it detects via git).

    ALL-not-ANY on purpose: one in-repo path makes the git rail informative
    again, so a mixed surface is seen, not blind."""
    paths = [p for p in (affected_paths or ()) if p]
    if not paths:
        return False
    return all(p.startswith(_EXTERNAL_PREFIXES) for p in paths)


def _is_store_churn(commit):
    """True when EVERY file a commit touched lives under the agent-store root.

    THE DOMINANT FALSE POSITIVE, and it is not a tail case. The loop stamps
    every commit `type(goal-id): title`, INCLUDING the bookkeeping commits that
    carry an agent's own journal / experience / changelog churn. Measured over
    the 200 most recent goal-id-named commits on this box: **141 of them (70%)
    touched nothing but agents/**. So for the majority of goals that have been
    worked on at all, the only commit naming them is bookkeeping — and counting
    it returns DONE-AND-MERGED for work that never shipped.

    Caught live on g-350-148, a `work_class: product` goal whose deliverable
    PR (#8, GetUserWebAppApiKey) was still OPEN and whose product-repo main had
    not moved since 2026-08-06: the verdict read DONE-AND-MERGED on the
    strength of ed8f9263c, a Mind-repo commit touching only
    agents/echo/{changelog,experience,experience-meta} plus one experience .md.

    A commit with NO file list (older probe shape, or a parse miss) is NOT
    treated as churn — absence of evidence about the files is not evidence
    that they were all bookkeeping. Pure."""
    files = commit.get("files")
    if not files:
        return False
    return all(f.startswith(_STORE_PREFIXES) for f in files) if _STORE_PREFIXES \
        else False


def classify_shipped(own_commits, pr_hits, branch_hits, external_surface,
                     goal_recurring=False, goal_last_achieved=None):
    """Pure disposition classifier. Returns (verdict, obliges).

    `own_commits` entries carry {"hash","subject","date","on_origin_main"}
    where on_origin_main is True / False / None (None = undeterminable, e.g.
    the sha is not in this clone — NEVER read as 'not merged').

    Precedence is evidence-first: positive evidence always outranks blindness,
    and blindness always outranks a clean report.

    RECURRING GOALS DROP PRIOR-CYCLE COMMITS, and without this the classifier
    is not merely imprecise on them — it is wrong every single time. A
    recurring goal's id appears in a commit subject on EVERY cycle it has ever
    run, so an unfiltered read returns DONE-AND-MERGED for a goal that is due
    right now. Caught on this function's second production invocation:
    g-115-105 (achievedCount 302, lastAchievedAt 2026-08-09T09:23:30) returned
    DONE-AND-MERGED on three commits dated 08-04, 08-06 and 08-08 — all prior
    cycles. Same class, same goal, as the g-115-2978 stale-claim incident that
    classify_board_mentions already guards twenty lines up.

    FAIL-SAFE DIRECTION IS DELIBERATELY OPPOSITE to that sibling, and the
    divergence is the point. There an unparseable timestamp KEEPS the hit,
    because a false yield is safer than a missed race. Here an unparseable
    timestamp DROPS the commit, because the two errors are not symmetric: a
    false DONE-AND-MERGED tells the reader the work is finished and ENDS the
    investigation, while a false GENUINELY-PENDING costs one redundant look.
    Fall toward doing the work."""
    own_commits = [c for c in (own_commits or ()) if not _is_store_churn(c)]
    if goal_recurring:
        kept = []
        for c in own_commits:
            try:
                if parse_naive_iso(c.get("date")) > parse_naive_iso(
                        goal_last_achieved):
                    kept.append(c)
            except Exception:
                continue  # unparseable -> drop (see docstring)
        own_commits = kept
    merged = [c for c in own_commits if c.get("on_origin_main") is True]
    if merged:
        return ("DONE-AND-MERGED",
                "Read the merged commit(s) against this goal's verification "
                "outcomes BEFORE closing. Full coverage -> close on evidence, "
                "zero code. A SUBSET is gap-100's PARTIALLY-DONE -> leave "
                "in-progress and execute only the remainder.")
    open_prs = [p for p in (pr_hits or [])
                if str(p.get("state", "")).upper() == "OPEN"]
    if open_prs:
        return ("OPEN-PR-STALE",
                "Do NOT re-implement. Re-read the LIVE mergeable/checks state "
                "(live_pr on each hit) — a recorded green is a timestampless "
                "fact about a moving target (guard-3034). Rebase/merge the PR "
                "instead of opening a competing one.")
    if own_commits or branch_hits:
        return ("WORK-EXISTS-UNMERGED",
                "Work naming this goal exists but nothing reached origin/main. "
                "Find it and finish it — do not start over. Unpushed local "
                "commits are invisible work (rb-6868).")
    if external_surface:
        return ("CANNOT-SEE",
                "This goal's surface is under an EXTERNAL gitignored root, so "
                "the git rail is BLIND, not negative (g-335-986). Read the "
                "goal's outcome_note and the coordination board before "
                "treating this as unstarted.")
    return ("GENUINELY-PENDING",
            "No shipped-work evidence on any rail. Execute normally.")


def _own_goal_commits(goal_id, since_hours, cwd=None,
                      agent_queue_private=False, me="", dropped_foreign=None):
    """Commits anywhere in the repo whose SUBJECT names this goal id — the
    loop stamps every commit `type(goal-id): title`, so history is the
    per-goal ledger (gap-100 step 1). Fail-open [].

    "whose SUBJECT names this goal id" is not the same as "this goal's work" when
    the id is a private agent-queue id, because that id collides across queues
    (g-115-6353). Provably-foreign commits are dropped here rather than in
    classify_shipped, because everything downstream — the verdict, `shipped_obliges`,
    the `own_goal_commits` payload a reader inspects — treats this list as THIS
    goal's ledger; filtering later would leave the payload lying while the verdict
    was right.

    LATENT, NOT LIVE, ON THIS BOX — stated because the numbers look alarming and
    two pre-existing filters are incidentally masking most of them. Live run
    2026-08-15 (alpha, cc-07) on `g-001-01 --source agent`: 15 own_goal_commits,
    only 1 of them mine, verdict nonetheless GENUINELY-PENDING. `_is_store_churn`
    drops 13 (pure `agents/**` bookkeeping) and the recurring `lastAchievedAt`
    filter absorbs the rest. Flip the one incidental mask and the defect is
    immediate: `classify_shipped(<those same real commits>, goal_recurring=False)`
    returns **DONE-AND-MERGED**, sourced entirely from echo's and zeta's work. 13
    of alpha's 23 agent-queue goals are non-recurring, and 12 foreign commits
    already survive the churn filter by touching one shared `.claude/` or
    `core/scripts/` file. Neither mask was designed for this; the filing agent hit
    it live on cc-03.
    """
    if not goal_id:
        return []
    try:
        out = subprocess.check_output(
            # NO `--since` here (). Two independent failure modes hit
            # this one argv, and only the second is fixed by formatting.
            # (1) TRAVERSAL CUTOFF: `--since` stops the walk at the first
            #     commit older than the cutoff, so ONE old-dated commit at the
            #     tip hides every recent commit behind it (guard-4539; measured
            #     on a fixture 2026-08-20 — 7 commits 67s old returned EMPTY).
            # (2) FLOAT APPROXIDATE: `--since="168.0 hours ago"` returned ZERO
            #     commits with rc=0 on a repo with thousands (measured
            #     2026-08-09 dogfooding this function), so every invocation
            #     reported GENUINELY-PENDING regardless of evidence — including
            #     for a goal committed 30 minutes earlier.
            # Both are SILENT and both fail in the same direction: no shipped
            # work found ⇒ the goal reads as genuinely pending ⇒ finished work
            # gets redone. `--grep` is highly selective, so `--max-count` alone
            # bounds the walk; the window is applied below against `%cI`, which
            # this format already carries. The nine unit tests stayed green
            # through (2), because they pin the pure classifier and this is the
            # impure probe that feeds it.
            ["git", "log", "--all",
             f"--grep={goal_id}", "--no-merges", "--name-only",
             f"--format={_RS}%H{_US}%cI{_US}%s", "--max-count=25"],
            cwd=(str(cwd) if cwd else None), stderr=subprocess.DEVNULL,
            timeout=20,
        ).decode("utf-8", "replace")
    except Exception:
        return []
    _shipped_cutoff = (dt.datetime.now().astimezone()
                       - dt.timedelta(hours=float(since_hours)))
    commits = []
    for rec in out.split(_RS):
        rec = rec.strip("\n")
        if not rec:
            continue
        head, _, body = rec.partition("\n")
        parts = head.split(_US)
        if len(parts) < 3 or not parts[0].strip():
            continue
        # The window, applied HERE rather than by `--since` in the argv above
        # (). %cI carries the offset, so parse tz-AWARE and compare
        # against an aware `now` — a naive strip would mis-window any commit
        # authored on a box in another zone. Fail-open on an unparseable stamp:
        # KEEP the commit. This filter replaced a bound whose failure was
        # dropping real shipped work, and re-creating that on a bad timestamp
        # would reintroduce the exact defect. Over-reporting shipped work costs
        # a second look; under-reporting redoes finished work.
        try:
            _committed = dt.datetime.fromisoformat(parts[1].strip())
            if _committed < _shipped_cutoff:
                continue
        except (TypeError, ValueError):
            pass
        subject = parts[2].strip()
        # --grep matches the WHOLE MESSAGE, so a commit that merely CITES this
        # goal id in its body arrives here as if it were this goal's work.
        # Measured live:  (a due recurring goal) read
        # WORK-EXISTS-UNMERGED purely because a DIFFERENT goal's commit
        # narrative named it. commit_goal_id reads the SUBJECT only —
        # preferring the `type(goal-id):` scope the loop stamps, falling back
        # to a bare goal id in the subject (measured; an earlier draft of this
        # comment claimed scope-only and was wrong). Either way it never reads
        # the body, and the body is where a citation lives.
        if commit_goal_id(subject) != goal_id:
            continue
        files = [ln.strip() for ln in body.split("\n") if ln.strip()]
        if agent_queue_private and commit_is_foreign_agent_work(files, me):
            # A partner's DIFFERENT goal wearing the same colliding id. Recorded
            # rather than silently dropped: on this box the live  lane
            # goes 15 commits -> 1, and an unexplained 14-commit shrink reads as
            # "git found nothing" (guard-1760).
            if dropped_foreign is not None:
                dropped_foreign.append({
                    "short": parts[0].strip()[:9],
                    "date": parts[1].strip(),
                    "subject": subject[:100],
                    "agent_dirs": sorted({
                        m.group(1) for f in files
                        for m in [_AGENT_DIR_RE.match(f)] if m}),
                })
            continue
        commits.append({"hash": parts[0].strip(), "short": parts[0].strip()[:9],
                        "date": parts[1].strip(),
                        "subject": subject[:100],
                        "files": files})
    return commits


def _sha_on_origin_main(sha, cwd=None):
    """True / False / None. None means UNDETERMINABLE — the object is not in
    this clone (rc=128), which is not an answer and must never be rendered as
    'not merged'. The rc=1-vs-anything-else split is the same distinction
    /is-change-live enforces, and collapsing it is how an unfetched sha gets
    reported as unshipped."""
    if not sha:
        return None
    try:
        rc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", sha, "origin/main"],
            cwd=(str(cwd) if cwd else None), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=15, check=False,
        ).returncode
    except Exception:
        return None
    if rc == 0:
        return True
    if rc == 1:
        return False
    return None


def _live_pr_state(repo_dir, number, timeout_s=20):
    """LIVE re-read of one PR's mergeability + checks (gap-100 step 6).

    The recorded '{ok: true, checks_state: green}' in a worker's note is a
    TIMESTAMPLESS fact about a moving target: on the g-335-972 encounter the
    note said green while the live probe read mergeable=CONFLICTING, because
    main had advanced under the open PR in the intervening 7 hours
    (guard-3034). So this never trusts a recorded verdict.

    `gh pr checks` exit codes are NOT portable — a PR with no CI returns 1 on
    some gh builds and 8 on others, so the '*' default renders a clean 0-check
    PR as red (rb-3541). Disambiguate on the MESSAGE, never the code.
    Fail-open: None on any error."""
    if not number:
        return None
    state = {}
    try:
        out = subprocess.check_output(
            ["gh", "pr", "view", str(number), "--json",
             "mergeable,mergeStateStatus,headRefOid"],
            cwd=str(repo_dir), stderr=subprocess.DEVNULL, timeout=timeout_s,
        ).decode("utf-8", "replace")
        got = json.loads(out or "{}")
        if isinstance(got, dict):
            state.update({k: got.get(k) for k in
                          ("mergeable", "mergeStateStatus", "headRefOid")})
    except Exception:
        return None
    try:
        proc = subprocess.run(
            ["gh", "pr", "checks", str(number)], cwd=str(repo_dir),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout_s, check=False,
        )
        blob = (proc.stdout or b"").decode("utf-8", "replace")
        if proc.returncode == 0:
            state["checks_state"] = "green"
        elif "no checks reported" in blob.lower():
            state["checks_state"] = "none"
        else:
            state["checks_state"] = "red"
    except Exception:
        state["checks_state"] = "unknown"
    return state


def _build_advisory(goal_id, race_risk, overlapping, partners,
                    matched_uncommitted=None, partner_in_flight=None,
                    board_hits=None, product=None):
    if not race_risk:
        return ""
    matched_uncommitted = matched_uncommitted or []
    board_hits = board_hits or []
    product = product or {}
    parts = []
    for c in overlapping[:3]:
        why = []
        if c["matched_paths"]:
            why.append("paths=" + ",".join(c["matched_paths"][:3]))
        if c["matched_keywords"]:
            why.append("kw=" + ",".join(c["matched_keywords"][:4]))
        if c.get("matched_goal_id"):
            why.append("goal-id-in-subject")
        if c.get("foreign_agent_work"):
            # Without this the reader sees a commit whose subject names THEIR OWN
            # goal id flagged as a race, which is the most confusing possible
            # presentation of a correct finding ().
            why.append("PARTNER's different goal, same colliding agent-queue id")
        repo_tag = f"[{c['repo']}] " if c.get("repo") else ""
        parts.append(
            f"{repo_tag}{c['short']} '{c['subject'][:60]}' ({'; '.join(why)})")
    commit_note = ""
    if parts:
        commit_note = (f"{len(overlapping)} recent commit(s) touched this goal's "
                       f"surface — " + " ; ".join(parts))
    uc_note = ""
    if matched_uncommitted:
        uc = []
        for m in matched_uncommitted[:4]:
            why = []
            if m["matched_paths"]:
                why.append("paths=" + ",".join(m["matched_paths"][:3]))
            if m["matched_stem"]:
                why.append("stem=" + m["matched_stem"])
            uc.append(f"{m['file']} ({'; '.join(why)})")
        owner = ""
        if partner_in_flight:
            owner = (f"partner {partner_in_flight.get('name', '?')} in_flight on "
                     f"{partner_in_flight.get('goal_id', '?')} "
                     f"'{(partner_in_flight.get('title') or '')[:50]}'; ")
        sep = " || " if commit_note else ""
        uc_note = (sep + "UNCOMMITTED working-tree edits touch this surface ("
                   + owner + " ; ".join(uc) + ")")
    board_note = ""
    if board_hits:
        bh = [f"{b['author']} {b['kind']} @ {b['timestamp']} "
              f"'{(b['text'] or '')[:50]}'" for b in board_hits[:3]]
        sep = " || " if (commit_note or uc_note) else ""
        board_note = (sep + "PARTNER BOARD POSTS claim/complete this goal ("
                      + " ; ".join(bh)
                      + ") — the board is the cross-box signal that survives "
                        "store partitions (guard-997/rb-3296): the shared "
                        "goal-store may not yet show the partner's claim")
    prod_note = ""
    if product.get("branch_hits") or product.get("pr_hits"):
        bits = []
        for pr in (product.get("pr_hits") or [])[:3]:
            bits.append(f"PR#{pr.get('number')} [{pr.get('repo')}] "
                        f"'{str(pr.get('title') or '')[:50]}' "
                        f"({pr.get('state', '')})")
        for bh2 in (product.get("branch_hits") or [])[:3]:
            bits.append(f"branch {bh2.get('branch')} [{bh2.get('repo')}]")
        sep = " || " if (commit_note or uc_note or board_note) else ""
        prod_note = (sep + "PRODUCT-REPO signals for this goal ("
                     + " ; ".join(bits)
                     + ") — the deliverable may already be shipped in the "
                       "product surface (g-115-2156 shape / rb-3743: probe "
                       "the deliverable surface before re-implementing)")
    pnote = ""
    if partners:
        pnote = " | recently active: " + ", ".join(
            f"{p['name']}({int(p['last_active_minutes'])}m)" for p in partners[:4])
    return (f"SAME-SURFACE RACE RISK for {goal_id}: "
            + commit_note
            + uc_note
            + board_note
            + prod_note
            + pnote
            + ". VERIFY before claiming — read the commit / check the partner's "
              "in-flight edit; a partner board CLAIM means yield (do not claim); "
              "if already shipped, mark superseded or coordinate instead of "
              "duplicating.")


def main():
    ap = argparse.ArgumentParser(
        description=("Advisory same-surface-race probe at goal-pickup: warns "
                     "when a goal's surface was committed in the last N hours "
                     "(catches the partner-already-shipped race the in_flight "
                     "filter misses). Detective only — never mutates, exit 0."),
    )
    ap.add_argument("--goal-id", required=True)
    # WORLD_AGENT_ONLY: cross-agent routes via MIND_AGENT env override ()
    ap.add_argument("--source", choices=["world", "agent"], default="world")
    ap.add_argument("--cross-agent-owner", default=None,
                    help="Owner agent when this is a CROSS-AGENT goal (selector "
                         "source='cross-agent:<owner>'). Declares that the "
                         "agent-queue id names ONE shared, contendable record, "
                         "so partner board claims stay in scope. Omit for your "
                         "own agent-queue goals, whose ids are per-agent and "
                         "cannot be contended (g-115-5570).")
    ap.add_argument("--since-hours", type=float, default=2.0,
                    help="git-log lookback window in hours (default 2).")
    ap.add_argument("--min-shared-keywords", type=int, default=2,
                    help="title↔subject keyword overlap needed to flag a "
                         "commit on keyword evidence alone (default 2).")
    ap.add_argument("--board-since-hours", type=float, default=6.0,
                    help="coordination-board lookback for partner claim/"
                         "completion posts naming this goal (default 6; wider "
                         "than the git window because cross-box store "
                         "propagation can lag the board by hours — g-001-311).")
    ap.add_argument("--product-since-hours", type=float, default=48.0,
                    help="git-log lookback for AGENT_WRITE_PATH product repos "
                         "(default 48; wider than the mind window because "
                         "product deliverables ship sparsely and the already-"
                         "shipped race spans days — the g-115-2156 PR was "
                         "pushed ~24h before the duplicate claim).")
    ap.add_argument("--shipped-since-hours", type=float, default=168.0,
                    help="lookback for commits whose SUBJECT names this goal "
                         "id — the done-but-pending probe (default 168 = 7d). "
                         "Much wider than the race window because the two "
                         "questions have different clocks: a race is minutes "
                         "to hours old, while already-shipped work sits "
                         "pending for as long as nothing writes back to the "
                         "goal record (measured 6h45m to >24h across gap-100's "
                         "nine encounters). Cheap — a subject grep, not a "
                         "tree walk.")
    ap.add_argument("--output", choices=["json", "human"], default="json")
    args = ap.parse_args()

    goal = _read_goal(args.source, args.goal_id)
    title = (goal or {}).get("title") or ""
    desc = (goal or {}).get("description") or ""
    verification = (goal or {}).get("verification")
    vtext = json.dumps(verification) if verification else ""
    surface_text = "\n".join([title, desc, vtext])

    affected_paths = extract_paths(surface_text)
    keywords = extract_keywords(title)

    # Resolved BEFORE the git lane, not just before the board lane: a private
    # agent-queue id is a property of the GOAL, and all three lanes key on it
    # (). It used to be computed between them, which is how the git
    # lanes ended up namespace-blind while the board lane was fixed.
    me = os.environ.get("MIND_AGENT", "")
    agent_queue_private = is_private_agent_queue(
        args.source, args.cross_agent_owner)

    commits = _git_log_commits(args.since_hours)
    race_risk, overlapping = classify_overlap(
        affected_paths, keywords, commits, args.goal_id,
        min_shared_keywords=args.min_shared_keywords,
        agent_queue_private=agent_queue_private, me=me)
    partners = _read_partners()

    # Uncommitted (partner in-flight) probe — . Gated on a partner
    # being in_flight: the working tree is shared (guard-741), so without a
    # partner in_flight any uncommitted files are this agent's own WIP and
    # flagging them would be a self-false-positive. When a partner IS in_flight,
    # their not-yet-committed edits are exactly the surface the git-log probe
    # cannot see.
    partner_in_flight = _partner_in_flight()
    matched_uncommitted = []
    if partner_in_flight:
        matched_uncommitted = classify_uncommitted_overlap(
            affected_paths, keywords, _git_uncommitted_files())
        if matched_uncommitted:
            race_risk = True

    # Board probe — . The only partition-surviving surface: consult it
    # unconditionally (NOT gated on partner_in_flight — the whole point is that
    # team-state can be frozen while a partner is mid-claim on another box).
    board_hits = classify_board_mentions(
        args.goal_id, me, _board_recent_mentions(args.board_since_hours),
        goal_recurring=bool((goal or {}).get("recurring")),
        goal_last_achieved=(goal or {}).get("lastAchievedAt"),
        agent_queue_private=agent_queue_private)
    # Claim/release pairing + live-state corroboration (). Without
    # these, the probe only ACCUMULATED yield signal and never cleared it, so a
    # single abandoned claim became a permanent lien on the goal for every agent.
    board_hits, superseded_claims = supersede_released_claims(board_hits)
    board_hits, stale_claims = corroborate_claims(
        args.goal_id, board_hits, _agent_live_state())
    # race_risk keys on SURVIVING claim/complete hits only. A release-kind hit is
    # informational — on its own it is evidence the goal is FREE, so counting it
    # would invert the signal it was added to carry.
    if any(h.get("kind") in ("claim", "complete") for h in board_hits):
        race_risk = True

    # Product-repo probe — . The mind-repo probes above are blind
    # to work shipped in AGENT_WRITE_PATH product repos (the 
    # shape: the deliverable PR existed ~24h before claim; only a sibling
    # mind commit was flagged). Gated on the goal prose naming a product
    # surface, so ordinary framework goals pay ~zero cost.
    product = _scan_product_repos(
        args.goal_id, surface_text, affected_paths, keywords,
        args.product_since_hours, args.min_shared_keywords,
        goal_recurring=bool((goal or {}).get("recurring")))
    if product["commits"] or product["branch_hits"] or product["pr_hits"]:
        race_risk = True
    overlapping.extend(product["commits"])

    # --- Done-but-pending disposition ( / gap-100) -------------
    # Deliberately does NOT touch race_risk. The two questions are separate
    # and fusing them would corrupt both: a goal whose OWN work already
    # shipped is not a partner race, and flipping race_risk here would make
    # every already-shipped goal read as a collision to the digest's yield
    # branch.
    dropped_foreign = []
    own_commits = _own_goal_commits(args.goal_id, args.shipped_since_hours,
                                    agent_queue_private=agent_queue_private,
                                    me=me, dropped_foreign=dropped_foreign)
    for c in own_commits:
        c["on_origin_main"] = _sha_on_origin_main(c["hash"])
    shipped_verdict, shipped_obliges = classify_shipped(
        own_commits, product["pr_hits"], product["branch_hits"],
        _surface_is_external(affected_paths),
        goal_recurring=bool((goal or {}).get("recurring")),
        goal_last_achieved=(goal or {}).get("lastAchievedAt"))

    advisory = _build_advisory(args.goal_id, race_risk, overlapping, partners,
                               matched_uncommitted=matched_uncommitted,
                               partner_in_flight=partner_in_flight,
                               board_hits=board_hits, product=product)

    result = {
        "shipped_verdict": shipped_verdict,
        "shipped_obliges": shipped_obliges,
        "own_goal_commits": own_commits,
        # Non-empty ONLY for a private agent-queue id: partners' commits wearing
        # the same colliding id, excluded from the ledger above ().
        "own_goal_commits_dropped_foreign": dropped_foreign,
        "goal_id": args.goal_id,
        "since_hours": args.since_hours,
        "affected_paths": sorted(affected_paths),
        "keywords": sorted(keywords),
        "race_risk": race_risk,
        "overlapping_commits": overlapping,
        "matched_uncommitted": matched_uncommitted,
        "board_partner_activity": board_hits,
        "board_superseded_claims": superseded_claims,
        "board_stale_claims": stale_claims,
        # Say that the board lane was NOT consulted, rather than reporting an
        # empty lane that reads identically to "consulted, found nothing"
        # (guard-1760). True only for a private agent-queue id ().
        # Key name is board-scoped for compatibility; the FLAG now governs all
        # three lanes — board (short-circuit), _own_goal_commits (drop foreign),
        # classify_overlap (re-admit foreign on path evidence) — see .
        "board_namespace_private": agent_queue_private,
        "product_surfaces": product["surfaces"],
        "product_repos_scanned": product["repos_scanned"],
        "product_branch_hits": product["branch_hits"],
        "product_pr_hits": product["pr_hits"],
        "partner_in_flight": partner_in_flight,
        "recent_partners": partners,
        "advisory": advisory,
        "now": dt.datetime.now().isoformat(timespec="seconds"),
    }

    # --- Advisory telemetry () ---------------------------------
    # Emit a firing on EVERY invocation, including the silent no-risk case.
    # Logging ONLY when race_risk fires would reproduce the exact gap this
    # closes: a zero count would stay ambiguous between "no races occurred"
    # and "the probe has not run in a month". One row per invocation makes
    # fired/invoked a measurable RATE, which is the question the goal asks
    # (is this LLM-invoked advisory actually being invoked?).
    #
    # The decision vocabulary is derived from THIS probe's own verdict, never
    # borrowed from another gate's semantics — an inherited mapping would
    # misreport, which is worse than no telemetry because it reads as
    # authoritative:
    #   noop  — nothing to compare (no affected paths AND no keywords)
    #   block — race_risk true (ADVISORY block; this probe never hard-blocks)
    #   pass  — scanned, and clean
    #
    # FAIL-OPEN: any telemetry failure leaves the advisory verdict byte-
    # identical. The verdict is computed above and is not read back from here.
    try:
        import _gate_log
        if not affected_paths and not keywords:
            _decision = "noop"
        elif race_risk:
            _decision = "block"
        else:
            _decision = "pass"
        _gate_log.log(
            "goal-pickup-coordination", _decision,
            caller="goal-pickup-coordination-check.main",
            payload={"goal_id": args.goal_id, "source": args.source,
                     "since_hours": args.since_hours},
            extra={# The guarded goal's id, so a firing can be JOINED to the
                   # pickup it guarded (). It was already passed
                   # above in `payload`, but _gate_log stores only
                   # `payload_hash` and discards the payload, so the id never
                   # reached the store. `extra` is persisted verbatim.
                   # Without it the pickup-coverage question is answerable
                   # only as a ratio of two independently-counted populations,
                   # which is biased UP by the duplicate-firing rate: measured
                   # 65.7% raw vs 48.2% de-duplicated, a 17.5-point
                   # overstatement in the flattering direction. Key is named
                   # `goal_id` deliberately — aspirations-read calls this field
                   # `id` and aspirations-query calls it `goal_id`, so the
                   # reader must not have to guess which surface produced it.
                   "goal_id": args.goal_id,
                   "race_risk": race_risk,
                   "affected_paths": len(affected_paths),
                   "keywords": len(keywords),
                   "overlapping_commits": len(overlapping),
                   "board_hits": len(board_hits),
                   "product_repos_scanned": len(product["repos_scanned"]),
                   "shipped_verdict": shipped_verdict,
                   "own_goal_commits": len(own_commits),
                   "partner_in_flight": bool(partner_in_flight)})
    except Exception:
        pass  # best-effort; telemetry must never change the advisory verdict

    if args.output == "human":
        print(f"goal={args.goal_id} race_risk={race_risk} "
              f"affected_paths={len(affected_paths)} keywords={len(keywords)} "
              f"overlapping={len(overlapping)} "
              f"uncommitted={len(matched_uncommitted)} "
              f"board={len(board_hits)} "
              f"superseded={len(superseded_claims)} "
              f"stale_claims={len(stale_claims)} "
              f"product={len(product['commits'])}c/"
              f"{len(product['branch_hits'])}b/{len(product['pr_hits'])}pr")
        # Printed on EVERY run, including GENUINELY-PENDING. A disposition
        # shown only when it is interesting teaches the reader that silence
        # means "not checked", which is the ambiguity the telemetry block
        # below was added to remove.
        print(f"  shipped_verdict={shipped_verdict} "
              f"(own_goal_commits={len(own_commits)}) — {shipped_obliges}")
        if advisory:
            print("  " + advisory)
    else:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
