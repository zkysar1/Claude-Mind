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
claimed. Two cheap probes — `git log --since=<N>h` over the affected surface +
the partner `last_active` snapshot — exactly as g-305-03 specifies.

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

TERMINAL_STATUSES = ("completed", "archived", "skipped", "expired", "resolved")


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
                     min_shared_keywords=2):
    """Pure overlap classifier. `commits` is a list of dicts with keys
    {hash, subject, files}. Returns (race_risk, overlapping_commits) where each
    overlapping entry records WHY it matched. A commit whose own scope goal id
    equals `own_goal_id` is excluded (the agent's own work on this goal).
    """
    overlapping = []
    for cm in commits:
        subject = cm.get("subject") or ""
        cgid = commit_goal_id(subject)
        if own_goal_id and cgid == own_goal_id:
            continue  # the agent's own in-progress commit on THIS goal
        files = cm.get("files") or []
        matched_paths = sorted({
            f for f in files
            if any(_path_overlap(ap, f) for ap in affected_paths)
        })
        commit_kw = extract_keywords(subject)
        matched_keywords = sorted(keywords & commit_kw)
        if matched_paths or len(matched_keywords) >= min_shared_keywords:
            overlapping.append({
                "hash": cm.get("hash", ""),
                "short": (cm.get("hash", "") or "")[:9],
                "subject": subject[:100],
                "committed_goal_id": cgid,
                "matched_paths": matched_paths,
                "matched_keywords": matched_keywords,
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


def classify_board_mentions(goal_id, me, messages, goal_recurring=False,
                            goal_last_achieved=None):
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

    me is REQUIRED to be non-empty: with me falsy the self/partner distinction
    is impossible (every author passes `author != me`), so the agent's OWN
    claim post would flag as a partner claim on an autocompact re-claim probe
    and the advisory would wrongly say YIELD. MIND_AGENT injection is
    fail-open and observed to drop (2026-07-13 bravo-fec finding), so this
    returns [] when me is falsy — no-hits is the advisory-safe direction.
    """
    if not me:
        return []
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


def detect_product_surfaces(surface_text, repo_names, write_path_entries=()):
    """Pure. Which product surfaces (if any) does the goal prose name?
    Three match forms, all case-insensitive at token boundaries:
      - a FULL repo name (compound names are specific enough to match
        anywhere in prose);
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
    full-name matches FIRST, then token-family matches — because the caller
    spends its bounded network budget (fetch + PR search) on the first <=3,
    and a full-name match is the strongest statement of WHICH repo the goal
    means. An entry-path match adds a label only — it triggers the scan
    without singling out one repo for network work."""
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

    # len>=5 floor on FULL names too: convention-file extraction is regex-
    # loose, and a short generic backticked token (e.g. a table header word)
    # must never become a scan trigger. Real repo names clear 5 easily.
    names = [n for n in (repo_names or []) if n and len(n) >= 5]
    for name in names:
        if _bounded(name.lower()):
            labels.add(name)
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


def _since_arg(since_hours):
    """`--since` argument formatted with INTEGER minutes. Never interpolate a
    float into git approxidate: on git 2.43 `--since="2.0 hours ago"` silently
    parses as NO filter (full-history scan → 6-day-old commits flagged as
    2h-window races, observed live 2026-07-17 on g-115-817 pickup) while
    `--since="48.0 hours ago"` parses as an empty window (0 commits → the
    probe scans nothing). Same float, opposite failure directions — integer
    minutes preserve fractional hours and are unambiguous. Pure."""
    return f"--since={max(1, int(round(float(since_hours) * 60)))} minutes ago"


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


def _parse_name_only_log(out):
    """Parse RS/US-separated `git log --name-only` output into
    [{hash, subject, files}]. Pure; shared by the mind-repo and product-repo
    log probes (g-115-2428 extraction — behavior identical to the original
    inline parser)."""
    rs, us = "\x1e", "\x1f"
    commits = []
    for rec in out.split(rs):
        rec = rec.strip("\n")
        if not rec:
            continue
        head, _, body = rec.partition("\n")
        h, _, subject = head.partition(us)
        files = [ln.strip() for ln in body.split("\n") if ln.strip()]
        commits.append({"hash": h.strip(), "subject": subject, "files": files})
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
    fmt = f"{rs}%H{us}%s"
    try:
        out = subprocess.check_output(
            ["git", "log", "--all", _since_arg(since_hours),
             "--no-merges", "--name-only", f"--format={fmt}"],
            cwd=str(PROJECT_ROOT), stderr=subprocess.DEVNULL,
        ).decode("utf-8", "replace")
    except Exception as e:
        print(f"[goal-pickup-coord] git log failed: {e}", file=sys.stderr)
        return []
    return _parse_name_only_log(out)


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
    fmt = f"{rs}%H{us}%s"
    try:
        out = subprocess.check_output(
            ["git", "log", "--all", _since_arg(since_hours),
             "--no-merges", "--name-only", f"--format={fmt}"],
            cwd=str(repo_dir), stderr=subprocess.DEVNULL,
        ).decode("utf-8", "replace")
    except Exception:
        return []
    return _parse_name_only_log(out)


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
             "branch_hits": [], "pr_hits": []}
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
        labels, matched = detect_product_surfaces(
            surface_text, names, wp_entries)
        if not labels:
            return empty
        result = {"surfaces": sorted(labels), "repos_scanned": [],
                  "commits": [], "branch_hits": [], "pr_hits": []}
        # Network budget follows MATCH order (full-name matches first), not
        # disk order — the live 2026-07-17 replay showed a token-family leak
        # spending all 3 slots on alphabetically-early repos while the
        # full-name-matched repo (the one holding the deliverable PR) got no
        # fetch/PR search.
        by_name = {}
        for n, p in repos:
            by_name.setdefault(n, p)
        matched_on_disk = [(n, by_name[n]) for n in matched
                           if n in by_name][:3]
        gh_ok = _gh_available() if matched_on_disk else False
        for n, p in matched_on_disk:
            _git_fetch_remote(cwd=p)
            if gh_ok:
                for pr in _gh_pr_hits(p, goal_id):
                    result["pr_hits"].append({"repo": n, **pr})
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

    commits = _git_log_commits(args.since_hours)
    race_risk, overlapping = classify_overlap(
        affected_paths, keywords, commits, args.goal_id,
        min_shared_keywords=args.min_shared_keywords)
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
    me = os.environ.get("MIND_AGENT", "")
    board_hits = classify_board_mentions(
        args.goal_id, me, _board_recent_mentions(args.board_since_hours),
        goal_recurring=bool((goal or {}).get("recurring")),
        goal_last_achieved=(goal or {}).get("lastAchievedAt"))
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

    advisory = _build_advisory(args.goal_id, race_risk, overlapping, partners,
                               matched_uncommitted=matched_uncommitted,
                               partner_in_flight=partner_in_flight,
                               board_hits=board_hits, product=product)

    result = {
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
        "product_surfaces": product["surfaces"],
        "product_repos_scanned": product["repos_scanned"],
        "product_branch_hits": product["branch_hits"],
        "product_pr_hits": product["pr_hits"],
        "partner_in_flight": partner_in_flight,
        "recent_partners": partners,
        "advisory": advisory,
        "now": dt.datetime.now().isoformat(timespec="seconds"),
    }

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
        if advisory:
            print("  " + advisory)
    else:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
