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

JSON output:
  {
    "goal_id": str,
    "since_hours": float,
    "affected_paths": [str, ...],      # extracted from goal prose
    "keywords": [str, ...],            # significant tokens from goal title
    "race_risk": bool,                 # any overlapping commit OR uncommitted match
    "overlapping_commits": [
      {"hash", "short", "subject", "committed_goal_id",
       "matched_paths": [...], "matched_keywords": [...]}
    ],
    "matched_uncommitted": [           # partner in-flight (uncommitted) overlaps (5)
      {"file", "matched_paths": [...], "matched_stem": str}
    ],
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
Reference: g-305-03 (US-03), g-115-1505 (uncommitted in-flight extension).
Sibling detective scripts: defer-drift-check.py, unblock-parent-status-sweep.py.
"""

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
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
    # 5: boilerplate goal-vocabulary that recurs across nearly every
    # goal title / commit subject and carries NO same-surface identity. The
    # canonical FP (): a goal whose title carried "participants:[agent,
    # user]" matched UNRELATED commit 286090d7d (merge-authority) on "agent" +
    # "user" alone (matched_paths empty) -> race_risk=true on pure goal-record
    # vocab. Sibling to goal-duplication-gate _STOPWORDS (5, same FP
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


def _git_log_commits(since_hours):
    """Return recent commits as [{hash, subject, files}] over the last
    since_hours, from PROJECT_ROOT. Uses RS/US separators so multi-line file
    lists parse unambiguously. Fail-open: any git error returns []."""
    rs, us = "\x1e", "\x1f"
    fmt = f"{rs}%H{us}%s"
    try:
        out = subprocess.check_output(
            ["git", "log", f"--since={since_hours} hours ago", "--no-merges",
             "--name-only", f"--format={fmt}"],
            cwd=str(PROJECT_ROOT), stderr=subprocess.DEVNULL,
        ).decode("utf-8", "replace")
    except Exception as e:
        print(f"[goal-pickup-coord] git log failed: {e}", file=sys.stderr)
        return []
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
                t = dt.datetime.fromisoformat(str(la).replace("Z", ""))
                mins = round((now - t).total_seconds() / 60, 0)
            except Exception:
                mins = None
        if mins is not None and mins <= since_minutes:
            out.append({"name": name, "last_active_minutes": mins})
    out.sort(key=lambda d: (d["last_active_minutes"] is None,
                            d["last_active_minutes"] or 0))
    return out


def _build_advisory(goal_id, race_risk, overlapping, partners,
                    matched_uncommitted=None, partner_in_flight=None):
    if not race_risk:
        return ""
    matched_uncommitted = matched_uncommitted or []
    parts = []
    for c in overlapping[:3]:
        why = []
        if c["matched_paths"]:
            why.append("paths=" + ",".join(c["matched_paths"][:3]))
        if c["matched_keywords"]:
            why.append("kw=" + ",".join(c["matched_keywords"][:4]))
        parts.append(f"{c['short']} '{c['subject'][:60]}' ({'; '.join(why)})")
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
    pnote = ""
    if partners:
        pnote = " | recently active: " + ", ".join(
            f"{p['name']}({int(p['last_active_minutes'])}m)" for p in partners[:4])
    return (f"SAME-SURFACE RACE RISK for {goal_id}: "
            + commit_note
            + uc_note
            + pnote
            + ". VERIFY before claiming — read the commit / check the partner's "
              "in-flight edit; if already shipped or actively in progress "
              "elsewhere, mark superseded or coordinate instead of duplicating.")


def main():
    ap = argparse.ArgumentParser(
        description=("Advisory same-surface-race probe at goal-pickup: warns "
                     "when a goal's surface was committed in the last N hours "
                     "(catches the partner-already-shipped race the in_flight "
                     "filter misses). Detective only — never mutates, exit 0."),
    )
    ap.add_argument("--goal-id", required=True)
    ap.add_argument("--source", choices=["world", "agent"], default="world")
    ap.add_argument("--since-hours", type=float, default=2.0,
                    help="git-log lookback window in hours (default 2).")
    ap.add_argument("--min-shared-keywords", type=int, default=2,
                    help="title↔subject keyword overlap needed to flag a "
                         "commit on keyword evidence alone (default 2).")
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

    # Uncommitted (partner in-flight) probe — 5. Gated on a partner
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

    advisory = _build_advisory(args.goal_id, race_risk, overlapping, partners,
                               matched_uncommitted=matched_uncommitted,
                               partner_in_flight=partner_in_flight)

    result = {
        "goal_id": args.goal_id,
        "since_hours": args.since_hours,
        "affected_paths": sorted(affected_paths),
        "keywords": sorted(keywords),
        "race_risk": race_risk,
        "overlapping_commits": overlapping,
        "matched_uncommitted": matched_uncommitted,
        "partner_in_flight": partner_in_flight,
        "recent_partners": partners,
        "advisory": advisory,
        "now": dt.datetime.now().isoformat(timespec="seconds"),
    }

    if args.output == "human":
        print(f"goal={args.goal_id} race_risk={race_risk} "
              f"affected_paths={len(affected_paths)} keywords={len(keywords)} "
              f"overlapping={len(overlapping)} "
              f"uncommitted={len(matched_uncommitted)}")
        if advisory:
            print("  " + advisory)
    else:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
