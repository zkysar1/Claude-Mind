#!/usr/bin/env python3
"""core/scripts/insight-trigger-sweep.py — Convert board insight_triggers
into goals.

Background
----------
Agents post insight_triggers to the board with tags like
`requires_action_by:alpha`, `action_type:extend-filter`, `severity:constrains`
when they notice cross-agent action items. The goal-selector reads goal
QUEUES, not BOARD posts, so these descriptive tags sit on the board with
no consumer. Canonical case: bravo's msg-20260514-143816-bravo-1073 posted
at 14:38 routed an action to alpha; 2h later no goal existed in any queue.

This sweeper closes the gap:
  1. Reads EVERY live board channel for the last 24h (see board_channels()).
  2. Filters to posts older than 1h (GRACE_HOURS) so authors who file
     their own goal-via-script first aren't pre-empted.
  3. Drops posts without `requires_action_by:<x>` AND `action_type:<y>` tags.
  4. Dedups against BOTH origin_signal formats in world + per-agent
     aspirations.jsonl: `insight_trigger:<msg_id>` (this sweeper) and
     `board_post:<msg_id>` (existing insight-trigger-gate.py). See
     core/config/conventions/board.md "Forward-Routing" for the asymmetry.
  5. Files an Apply goal under the RESOLVED escalation aspiration (see
     `_escalation_target`; asp-115 upstream, whatever exists locally elsewhere)
     with `intended_agent=<x>` and `origin_signal=insight_trigger:<msg_id>`.
  6. Caps filings at MAX_GOALS_PER_RUN to bound any spam blast radius.

Channel scope (g-115-3925, 2026-07-29)
--------------------------------------
This sweep read ONLY findings.jsonl from inception until 2026-07-29, so a
`requires_action_by:` tag posted to any other channel was structurally
invisible — it could never convert, at any age. Measured cost of that gap:
msg-20260728-194530-omni-5115 (omni -> alpha, coordination, action_type:revisit)
sat undelivered. See board_channels() for why the fix is auto-discovery rather
than a longer constant.

Modes
-----
  (default)            Read, dedup, file new goals.
  --dry-run            Same probe path; no aspirations writes.
  --json               Emit machine-readable output (used by /prime Step 5.5b).

Callers
-------
  - Recurring goal g-115-754 (every 1h, via insight-trigger-sweep.sh).
  - /prime Phase 2 Step 5.5b (with --dry-run --json) for visibility surface.
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Reuse _paths.py resolution (WORLD_DIR honors local-paths.conf + env overrides).
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "core" / "scripts"))
import _rt  # noqa: E402  — canonical Python -> daemon client (post-cutover; see _rt.py)
from _paths import AGENT_DIR, CORE_ROOT, WORLD_DIR, agents_root as _agents_root  # noqa: E402
# : never hardcode the escalation aspiration.  is the UPSTREAM
# deployment's recurring-infra queue; this is a framework file that travels the
# promotion chain, so a literal here files into a nonexistent aspiration on every
# other deployment. _escalation_target resolves to one that ACTUALLY EXISTS.
try:
    from _escalation_target import resolve as _resolve_asp, source_flag as _asp_source
    ESCALATION_ASP, _ESCALATION_ASP_VIA = _resolve_asp(CORE_ROOT, WORLD_DIR, AGENT_DIR)
    ESCALATION_SOURCE = _asp_source(ESCALATION_ASP, WORLD_DIR, AGENT_DIR)
except Exception:
    ESCALATION_ASP, _ESCALATION_ASP_VIA, ESCALATION_SOURCE = (
        "asp-115", "fallback:import-failed", "world")
# : reuse the canonical <agent>@<env-id> splitter — do NOT re-derive
# peer detection (cross-deployment-channel.md "Addressing an agent").
from peer_surface import split_author  # noqa: E402
# : the environments-registry READER is shared with
# peer-thread-relay-sweep so the peer name set cannot fork. Policy stays local.
from _peer_registry import load_env_registry as _shared_load_env_registry  # noqa: E402
# : the framework's own lock primitive, NOT a hand-rolled lock file.
# acquire_lock is an atomic O_CREAT|O_EXCL create (never exists()+write — TOCTOU)
# with a stale-break, and it routes through the storage backend, which is what
# makes the run mutex work ACROSS BOXES rather than only on this one.
from _fileops import acquire_lock, release_lock  # noqa: E402

BOARD_DIR = WORLD_DIR / "board"
WORLD_ASPS = WORLD_DIR / "aspirations.jsonl"
#  addressing-rule inputs. The registry is committed under core/ so it
# is always locally readable; team-state shards are the roster's durable form.
ENV_REGISTRY_DIR = PROJECT_ROOT / "core" / "config" / "environments"
ENV_LOCAL_FILE = PROJECT_ROOT / ".env.local"
TEAM_SHARDS_DIR = WORLD_DIR / "team-state" / "agents"

# Files under board/ that are NOT message channels. `<channel>-reads.jsonl` is
# the per-channel read-tracking sidecar (rows are {msg_id, reader_agent, ...},
# never messages); `<channel>-archive.jsonl` is a rotation of a channel this
# sweep already scans, and every row in it is older than the rotation — i.e.
# older than WINDOW_HOURS by construction, so reading 11MB of it per run buys
# nothing. The `-archive.jsonl` suffix also covers the double-rotated
# `<channel>-archive-archive.jsonl`. A channel legitimately named
# `<x>-archive` would be excluded; none exists and the tradeoff is documented
# here rather than guarded, because the alternative (an allowlist) is the
# construct this whole change removes.
CHANNEL_EXCLUDE_SUFFIXES = ("-reads.jsonl", "-archive.jsonl")

# Tunables — edit in place if the cadence/window/spam-cap needs adjustment.
# Single source of truth; no config-file indirection unless cross-agent
# variance becomes a real requirement.
GRACE_HOURS = 1.0
WINDOW_HOURS = 24.0
MAX_GOALS_PER_RUN = 10

#  — RUN MUTEX. load_converted_ids() is a pre-loop SNAPSHOT and the
# filing loop never adds a newly-filed msg_id back into it, so two OVERLAPPING
# runs both snapshot before either writes and both file the same trigger.
# MEASURED:  / , identical origin_signal
# insight_trigger:msg-20260811-230106-bravo-5014, filed 11 seconds apart from a
# board post that exists exactly once.
#
# WHY A LOCK RATHER THAN THE TWO CHEAPER SHAPES. Re-reading converted_ids
# immediately before each file only NARROWS the window (both runs can still
# re-read before either writes); adding each filed msg_id to the in-memory set
# fixes nothing here at all, because `pending` is already deduped against the
# snapshot so no single run files the same msg_id twice. Only serializing the
# snapshot-through-filing section ELIMINATES it: a second run cannot enter until
# the first has finished writing, so its snapshot necessarily SEES those writes.
#
# The lock lives under WORLD_DIR on purpose. The backend resolves a governed
# path to the distributed lock, so this excludes runs on OTHER BOXES — which is
# the case that matters, since any box may run this sweep. CAVEAT, stated
# because it is invisible at the call site: that holds when a governed root is
# in the env, which insight-trigger-sweep.sh guarantees by sourcing _paths.sh.
# A bare `py -3 core/scripts/insight-trigger-sweep.py` has no governed root, so
# _fileops._lock_backend() falls back to a LOCAL file lock (it warns once on
# stderr) and the mutex degrades to same-box-only. Prefer the wrapper.
SWEEP_LOCK = WORLD_DIR / "insight-trigger-sweep.lock"
# Short: a contending run should SKIP, not queue. The sweep is periodic and
# idempotent, so the run already holding the lock covers the same triggers —
# waiting buys nothing and risks two runs finishing back-to-back.
SWEEP_LOCK_TIMEOUT = 5
# Generous: the stale-break compares NOW against the lock file's mtime, which is
# stamped at CREATE and never refreshed, so this must exceed the whole critical
# section or a live holder gets its lock broken mid-filing and the race returns.
# Worst case is MAX_GOALS_PER_RUN daemon add-goal writes at the wrapper's
# RT_CURL_TIMEOUT=180s ceiling. 600s sits far above the realistic worst case and
# far below the sweep's cadence, so a crashed run costs at most one skipped tick.
SWEEP_LOCK_STALE_SECONDS = 600

#  outcome 2: the CONVERSION window above is 24h while this sweep's
# cadence is 5.33h, so a run that slips a day drops triggers with no trace —
# `scanned` counts only what the sweep chose to look at, so an out-of-window
# trigger is absent from the DENOMINATOR rather than skipped within it, and the
# run still reports clean conservation. AUDIT_WINDOW_HOURS is a strictly wider
# read-only lookback used to COUNT and NAME those, never to convert them.
# Deliberately not equal to WINDOW_HOURS: keeping the two separate is what lets
# conservation (outcome 1) stay exact while the audit half reports beyond it.
AUDIT_WINDOW_HOURS = 168.0

# Tag prefix for the out-of-window routing note. It doubles as the dedup key
# (guard-2177 / guard-1826): the out-of-window condition is MONOTONE — a post
# only gets older — so a stateless re-post would fire every 5.33h forever. The
# board note IS the cooldown record, matching handoff-aging-check and
# dependency-timeout-check, whose durable half is likewise a board post.
OOW_TAG_PREFIX = "insight-trigger-out-of-window:"

SEVERITY_PRIORITY = {
    "invalidates": "HIGH",
    "constrains": "MEDIUM",
    "enables": "MEDIUM",
    "informs": "LOW",
}

REQ_ACTION_RE = re.compile(r"^requires_action_by:(.+)$")
ACTION_TYPE_RE = re.compile(r"^action_type:(.+)$")
SEVERITY_RE = re.compile(r"^severity:(.+)$")
AFFECTS_RE = re.compile(r"^affects:(g-\d+-\d+)$")

# : terminal statuses that mean "no Apply needed — target already
# resolved". Mirrors unblock-parent-status-sweep.py:112 (rb-908 lineage).
# Audit-time -> apply-time staleness gap (rb-1150): zeta's 06:37 audit
# spawned a supersession-Apply at 18:11; the target had already closed at
# 15:32. Re-probe at filing time catches that drift.
#  (zeta allowlist audit D1): synced to the SSOT
# aspirations.TERMINAL_GOAL_STATUSES -- previously drifted (missing
# expired+decomposed, carried a bogus archived that is not a valid goal
# status). Effect of the drift: expired/decomposed targets were mis-read as
# non-terminal and still spawned supersession-Apply work. Parity enforced by
# tests/test_terminal_goal_states_parity.py.
TERMINAL_GOAL_STATES = {"completed", "skipped", "expired", "decomposed", "superseded"}


# ---------------------------------------------------------------------------
# Loading + filtering
# ---------------------------------------------------------------------------


def _refresh(path):
    """guard-980: force-pull the authoritative backend copy before a raw read.

    findings.jsonl and the world + per-agent aspirations.jsonl are
    backend-routed stores; on an own-cloud box a raw local read can be a stale
    git-sync mirror (rb-2855 / guard-980), so this sweep would file/skip goals
    off drifted data. refresh() force-fetches the S3 copy on OwnCloudBackend
    and is a no-op on LocalBackend. Fail-open: a bare subprocess without daemon
    env cannot resolve the backend — degrade to the raw read rather than abort
    this advisory routing sweep (matches core/scripts/aspirations-evict-completed.py).
    """
    try:
        from storage_backend import get_backend
        get_backend().refresh(path)
    except Exception as e:
        try:  # report, never raise — see note_swallowed_backend_error ()
            from storage_backend import note_swallowed_backend_error
            note_swallowed_backend_error("refresh", path, e)
        except Exception:
            pass


def _parse_ts(ts_str):
    """Parse the timestamp formats observed in findings.jsonl.

    Board entries write `2026-05-14T14:38:16` (local, no TZ suffix). We
    interpret as local naive — same convention as the rest of the framework
    (CLAUDE.md "ISO 8601 dates everywhere. Timestamps: ALWAYS local system
    time").
    """
    return datetime.fromisoformat(ts_str.rstrip("Z"))


def board_channels():
    """Every live board channel file, sorted. Auto-discovered, not enumerated.

    Discovery rather than an allowlist is deliberate. The defect this replaces
    was a single hardcoded `FINDINGS` path: a channel the constant did not name
    could never be swept, and nothing anywhere reported that it was being
    skipped. An allowlist has the same failure mode one entry later — it just
    moves the moment the next channel is forgotten. Globbing removes the class:
    a new channel file is swept the run after it appears, with no code change.

    Scanning every channel is also semantically right, not merely convenient.
    `requires_action_by:<agent>` + `action_type:<verb>` is an explicit routing
    request; a channel is a topic hint, not a permission boundary, so there is
    no channel where that pair should be deliberately ignored. Untagged posts
    cost one JSON parse and are dropped by the same filter that already drops
    the ~97% of findings.jsonl carrying no routing tags.

    Measured 2026-07-29 (alpha, cc-04), all-time counts of posts carrying BOTH
    required tags: findings 183, coordination 5, and general / decisions /
    reasoning / feedback 0 each. So the widening's live yield today is small —
    the value is that the next channel cannot go unswept, not a backlog.
    Note the goal that motivated this cited "102 of 142 inbound peer posts" on
    coordination; that counts `requires_action_by:` ALONE. Requiring the pair
    the sweep actually filters on drops it to 5 on the live channel.
    """
    if not BOARD_DIR.is_dir():
        return []
    return sorted(
        p for p in BOARD_DIR.glob("*.jsonl")
        if not p.name.endswith(CHANNEL_EXCLUDE_SUFFIXES)
    )


def load_triggers():
    """Read every live board channel, yield candidate insight_triggers.

    A candidate satisfies all of:
      - message has `requires_action_by:<agent>` tag
      - message has `action_type:<verb>` tag
      - message timestamp is within WINDOW_HOURS and older than GRACE_HOURS
    """
    now = datetime.now()
    win_cutoff = now - timedelta(hours=WINDOW_HOURS)
    grace_cutoff = now - timedelta(hours=GRACE_HOURS)
    out = []
    for channel_path in board_channels():
        # guard-980: avoid a stale git-sync mirror of the channel file.
        _refresh(channel_path)
        if not channel_path.is_file():
            continue
        channel = channel_path.stem
        for line in channel_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                ts = _parse_ts(msg["timestamp"])
            except (KeyError, ValueError):
                continue
            if ts < win_cutoff or ts > grace_cutoff:
                continue
            rec = _parse_trigger_msg(msg, channel, now, ts)
            if rec is None:
                continue
            out.append(rec)
    return out


def _parse_trigger_msg(msg, channel, now, ts):
    """Both-tagged? -> trigger dict. Else None.

    Extracted (g-115-754) so the CONVERSION scan (`load_triggers`) and the
    AUDIT scan (`load_out_of_window_triggers`) apply one identical definition
    of "is this an insight_trigger". Two live call sites today — the audit
    half is only trustworthy if it recognises exactly what the conversion half
    would have recognised, so a second copy of this parse would be the defect.
    """
    tags = msg.get("tags") or []
    target = None
    action = None
    severity = "informs"
    affects_goal = None
    for t in tags:
        m = REQ_ACTION_RE.match(t)
        if m:
            target = m.group(1).strip()
            continue
        m = ACTION_TYPE_RE.match(t)
        if m:
            action = m.group(1).strip()
            continue
        m = SEVERITY_RE.match(t)
        if m:
            severity = m.group(1).strip()
            continue
        m = AFFECTS_RE.match(t)
        if m:
            affects_goal = m.group(1).strip()
    if not target or not action:
        return None
    return {
        "msg_id": msg.get("id"),
        "author": msg.get("author"),
        # Where the trigger was READ from, not msg["channel"] — the board
        # pointer in the filed goal must name the file a reader can open. A
        # row's self-reported channel can disagree with the file holding it
        # (rotation, hand-edits), and the pointer is only useful if it resolves.
        "channel": channel,
        "target": target,
        "action": action,
        "severity": severity,
        "affects_goal": affects_goal,
        "text": msg.get("text", ""),
        "tags": tags,
        "timestamp": msg.get("timestamp"),
        "age_h": round((now - ts).total_seconds() / 3600, 1),
    }


def load_out_of_window_triggers():
    """The AUDIT half ( outcome 2). Returns (out_of_window, routed_ids, truncated).

    `load_triggers` above drops a both-tagged post the moment it is older than
    WINDOW_HOURS, and reports nothing — so a run that slips past the window
    silently loses work while still reporting exact conservation, because
    `scanned` is defined as what the sweep chose to LOOK AT. This scan covers
    [AUDIT_WINDOW_HOURS, WINDOW_HOURS) — strictly the band the conversion scan
    refuses — and only counts and names; it never converts.

    Returns:
      out_of_window — trigger dicts in that band, newest first.
      routed_ids    — msg_ids already carrying an OOW_TAG_PREFIX board note.
                      Harvested in this SAME pass (the board files are already
                      open) so routing is idempotent without a second scan.
      truncated     — count of both-tagged posts OLDER than the audit window.
                      Reported rather than swallowed: a bounded scan that does
                      not say what it declined to look at reads as coverage it
                      never had (guard-1760).
    """
    now = datetime.now()
    win_cutoff = now - timedelta(hours=WINDOW_HOURS)
    audit_cutoff = now - timedelta(hours=AUDIT_WINDOW_HOURS)
    out = []
    routed_ids = set()
    truncated = 0
    for channel_path in board_channels():
        _refresh(channel_path)  # guard-980
        if not channel_path.is_file():
            continue
        channel = channel_path.stem
        for line in channel_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Harvest prior routing notes from ANY age — a note posted 8 days
            # ago must still suppress a re-post today, so this lookup is
            # deliberately not bounded by the audit window.
            for tag in (msg.get("tags") or []):
                if tag.startswith(OOW_TAG_PREFIX):
                    routed_ids.add(tag[len(OOW_TAG_PREFIX):])
            try:
                ts = _parse_ts(msg["timestamp"])
            except (KeyError, ValueError):
                continue
            if ts >= win_cutoff:
                continue  # in-window (or in grace) — the conversion half owns it
            rec = _parse_trigger_msg(msg, channel, now, ts)
            if rec is None:
                continue
            if ts < audit_cutoff:
                truncated += 1
                continue
            out.append(rec)
    out.sort(key=lambda r: r["age_h"])
    return out, routed_ids, truncated


# ---------------------------------------------------------------------------
# Addressing resolution ( — enforces cross-deployment-channel.md
# "Addressing an agent: requires_action_by and the collision set")
# ---------------------------------------------------------------------------


def _load_env_registry():
    """{env_id: entry-dict} from core/config/environments/*.yaml.

    Each entry may carry an OPTIONAL `known_agents` list — agent names known to
    operate in that deployment (the durable half of the collision set). Absence
    of the field contributes no names. Fail-open: any read/parse error yields
    fewer entries, never an abort — a missing registry degrades to "no peers
    known", which preserves the bare-name-means-local installed base.
    """
    # Delegates to _peer_registry (), the SSOT for reading that
    # directory. The loader moved because a SECOND consumer
    # (peer-thread-relay-sweep) needed the same names and THIS module's filename
    # is hyphenated, so it cannot be imported — a copy would have been FORCED
    # rather than chosen, and  says why that is dangerous: "a second
    # copy would drift and getting it wrong pushes local work at a peer."
    #
    # ONLY the loader is shared. The POLICY built on it in resolve_addressing
    # below (which envs are peers when self_env is unresolvable) deliberately
    # DIVERGES from the relay sweep's and must not be unified — see the
    # "Deliberate non-sharing" note in _peer_registry.py.
    return _shared_load_env_registry(ENV_REGISTRY_DIR)


def _self_env():
    """This deployment's ENVIRONMENT_ID: env var first, then .env.local.

    Returns None when unresolvable. Callers treat None conservatively: an
    explicit @env target then REFUSES (recoverable, names the post) rather
    than guessing local — a wrong guess is the exact defect the rule bans.
    """
    v = (os.environ.get("ENVIRONMENT_ID") or "").strip()
    if v:
        return v
    try:
        for line in ENV_LOCAL_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("ENVIRONMENT_ID="):
                v = line.split("=", 1)[1].strip().strip('"').strip("'")
                if v:
                    return v
    except OSError:
        pass
    return None


def _local_roster():
    """Local agent names — team-state shard basenames (the roster's durable
    form), falling back to conf-bearing agent dirs. Fail-open to empty set,
    which yields an empty collision set (bare names keep routing local)."""
    try:
        if TEAM_SHARDS_DIR.is_dir():
            names = {p.stem for p in TEAM_SHARDS_DIR.glob("*.yaml")}
            if names:
                return names
    except OSError:
        pass
    try:
        return {
            d.name for d in _agents_root().iterdir()
            if d.is_dir() and (d / "local-paths.conf").is_file()
        }
    except OSError:
        return set()


def resolve_addressing(triggers):
    """Apply the cross-deployment addressing rule to each trigger's target.

    THE RULE (cross-deployment-channel.md, decided g-115-3929):
      1. `<agent>@<env-id>` is EXACT — @self-env resolves to the local agent
         (qualifier stripped); @peer-env is the PEER deployment's agent and
         must not convert into this deployment's queue; @unregistered-env
         cannot be vouched for.
      2. A bare name NOT in the collision set means the LOCAL agent (the
         87% installed base — preserved unchanged).
      3a. A bare name IN the collision set is AMBIGUOUS and FAILS LOUD. Never
         silently default to local: a wrong route passes validation cleanly;
         a refusal names the post so the author can qualify it.
      3b. EXCEPT when the AUTHOR is a non-colliding local (amended g-115-4980,
         2026-08-08, zeta/cc-02). An unqualified name means "in the speaker's
         namespace", so a bare target from an author who is unambiguously ours
         is ours. Tagged `addressing: author_scoped_local` and counted in the
         summary — distinguishable from a clause-2 resolve, never silent.

    WHY 3b IS SAFE, AND WHY THE AUTHOR-EXCLUSION IS LOAD-BEARING. The channel
    is read from BOTH deployments and each side's "local" differs, so the
    invariant that matters is that exactly ONE side routes any given post.
    Author-scoping satisfies it: alpha/bravo/echo/foxtrot are absent from the
    peer roster, so the same post the peer reads has a non-roster author there
    and refuses. But an author who is ITSELF in the collision set resolves
    locally on BOTH sides — a double-route, strictly worse than the refusal.
    Hence `a_name not in collision`. Measured 2026-08-08 over 10,324 board
    messages / 17 days: of 48 bare-`zeta` triggers, 45 carry non-colliding
    local authors (resolve), 2 are zeta-authored (refuse — double-route risk),
    1 is `omni`-authored (refuse — peer author, and `split_author('omni')` is
    `('omni', None)` because the peer operator posts unqualified, so it is NOT
    caught by the peer-env test above). Clause 3a had refused all 48 and
    prevented 0 wrong routes.

    Collision set = local roster ∩ (registry known_agents of peer envs ∪
    authors observed in explicit <agent>@<peer-env> form this window). The
    registry field is the durable source; the observation pass is additive
    evidence for peers nobody declared. Recomputed every run — never
    hardcoded (the convention forbids solving this by naming names in code).

    Returns (resolved, refused, collision_sorted): `resolved` triggers carry a
    LOCAL bare target; `refused` entries name msg_id + verdict + reason.
    """
    registry = _load_env_registry()
    self_env = _self_env()
    if self_env is None:
        print(
            "[insight-trigger-sweep] WARN: ENVIRONMENT_ID unresolvable — "
            "explicit @env targets will be refused, not guessed local",
            file=sys.stderr,
        )
    peer_envs = {e for e in registry if e != self_env}
    peer_agents = set()
    for env in peer_envs:
        for name in (registry.get(env, {}).get("known_agents") or []):
            name = str(name).strip()
            if name:
                peer_agents.add(name)
    for t in triggers:
        a_name, a_env = split_author(t.get("author"))
        if a_env and a_env in peer_envs and a_name:
            peer_agents.add(a_name)
    roster = _local_roster()
    collision = roster & peer_agents

    resolved = []
    refused = []
    for t in triggers:
        name, env = split_author(t.get("target"))
        if env is not None:
            if self_env is not None and env == self_env:
                local_t = dict(t)
                local_t["target"] = name  # explicit local — strip the qualifier
                resolved.append(local_t)
            elif env in peer_envs:
                refused.append({
                    "msg_id": t["msg_id"], "author": t["author"],
                    "target": t["target"], "verdict": "peer_addressed",
                    "reason": (
                        f"addressed to {name}@{env} — a peer deployment's "
                        "agent; not convertible into this deployment's queue"
                    ),
                })
            else:
                refused.append({
                    "msg_id": t["msg_id"], "author": t["author"],
                    "target": t["target"], "verdict": "unknown_env",
                    "reason": (
                        f"env-id '{env}' is not in core/config/environments/ "
                        "— cannot vouch for an unregistered deployment"
                    ),
                })
        elif name in collision:
            # clause 3b () — AUTHOR-SCOPED LOCAL. A bare name is
            # unqualified, and an unqualified name means "in the speaker's
            # namespace". When the AUTHOR is demonstrably one of ours, the
            # bare target is theirs-and-therefore-ours; refusing it protects
            # against nothing. See the symmetry proof below for why this is
            # not the silent local-default clause 3a bans.
            a_name, a_env = split_author(t.get("author"))
            if a_env is None and a_name and a_name in roster \
                    and a_name not in collision:
                local_t = dict(t)
                # Distinguishable from an ordinary clause-2 local resolve
                # (guard-2586 / guard-1753): a path that degrades to a
                # default must never emit the same signal as one that
                # resolved outright. Counted in the summary, not silent.
                local_t["addressing"] = "author_scoped_local"
                resolved.append(local_t)
            else:
                refused.append({
                    "msg_id": t["msg_id"], "author": t["author"],
                    "target": t["target"], "verdict": "ambiguous_collision",
                    "reason": (
                        f"bare '{name}' exists in BOTH the local roster and a "
                        f"peer deployment, and author '{t.get('author')}' is "
                        "not a non-colliding local — ambiguous; qualify as "
                        f"{name}@<env-id> (cross-deployment-channel.md "
                        "clause 3)"
                    ),
                })
        else:
            resolved.append(t)
    return resolved, refused, sorted(collision)


# ---------------------------------------------------------------------------
# Apply-time goal-status re-probe (, rb-1150)
# ---------------------------------------------------------------------------


def probe_goal_status(goal_id):
    """Return the current STATUS string of goal_id, or None when not found.

    Thin wrapper over probe_goal_record so the status-only callers keep their
    exact contract while the filing path can read the whole record.
    """
    g = probe_goal_record(goal_id)
    return g.get("status") if g else None


def probe_goal_record(goal_id):
    """Return the full goal RECORD for goal_id by scanning world + per-agent
    aspirations.jsonl. Returns the status string when found, None when the
    goal does not exist anywhere.

    Closes the audit-time -> apply-time staleness gap (rb-1150). The
    insight_trigger may have been authored hours earlier; the target goal
    can have transitioned to a terminal state in that window. Re-probing
    at filing time avoids spawning duplicate Apply work.

    Reads JSONL directly rather than _rt.aspirations_read so the function
    is testable against a sandbox WORLD_DIR via monkeypatch — _rt would
    require a running daemon. The dedup helper `load_converted_ids` above
    uses the same direct-read pattern.
    """
    paths = [WORLD_ASPS]
    for d in sorted(_agents_root().iterdir()):
        if d.is_dir() and (d / "local-paths.conf").is_file():
            paths.append(d / "aspirations.jsonl")
    for path in paths:
        _refresh(path)  # guard-980: avoid a stale git-sync mirror of aspirations.jsonl
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                asp = json.loads(line)
            except json.JSONDecodeError:
                continue
            for g in asp.get("goals", []) or []:
                if g.get("id") == goal_id:
                    return g
    return None


# Rank used ONLY to take a max; not a general priority ordering.
_PRIORITY_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


def inherit_priority(own, target):
    """Return the higher of the chore's own priority and the priority of the
    goal it unblocks.

    PURE, so the decision is testable without a store. g-115-6590 item (2):
    an Apply-chore is filed at a priority derived from the BOARD POST'S
    severity, which describes how important the FINDING is -- not how much
    work is waiting on it. The measured incident is g-115-6243: a one-command
    chore filed LOW while three HIGH goals sat blocked behind it, at rank 10
    for 36 hours. Severity and blocking-cost are different quantities and the
    filed priority has to carry the larger one.

    Unknown/absent values on EITHER side fall back to the other, and an
    unrecognised string never wins -- a typo must not silently promote a
    chore to HIGH.
    """
    if own not in _PRIORITY_RANK:
        return target if target in _PRIORITY_RANK else own
    if target not in _PRIORITY_RANK:
        return own
    return own if _PRIORITY_RANK[own] >= _PRIORITY_RANK[target] else target


def _emit_audit_stale_note(trigger, target_status):
    """Post a coordination-board status note for a skipped Apply.

    One short post per audit-stale finding. The text matches the
    acceptance criteria shape (`Audit-stale: <finding-id> targeted
    <goal-id> already <status>`) so future grepping picks them up.

    Fail-open: board-post.sh errors are logged to stderr but do not
    abort the sweep — the metric record (returned to summary) is the
    durable audit trail.
    """
    import subprocess
    text = (
        f"Audit-stale: insight_trigger {trigger['msg_id']} from "
        f"{trigger['author']} targeted {trigger['affects_goal']} "
        f"(action={trigger['action']}) -- already {target_status}; "
        "Apply spawn skipped (rb-1150 audit-time vs apply-time gap)."
    )
    tags = (
        f"audit-stale,insight-trigger:{trigger['msg_id']},"
        f"target:{trigger['affects_goal']},target_status:{target_status}"
    )
    try:
        # board.py is invoked directly via sys.executable instead of `bash
        # board-post.sh` to avoid the Windows bash-subprocess hazard
        # (rb-225/rb-247): bash from Python on Windows resolves to the WSL
        # stub when PATH is wrong, hanging silently. sys.executable +
        # board.py reaches the same code path (the bash wrapper exec's
        # `python3 board.py post`) without the bash layer.
        proc = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "core" / "scripts" / "board.py"),
             "post", "--channel", "coordination", "--type", "status", "--tags", tags],
            input=text, capture_output=True, text=True, timeout=10,
        )
        return {"posted": proc.returncode == 0, "msg_id": proc.stdout.strip() if proc.returncode == 0 else None}
    except Exception as e:
        print(f"[insight-trigger-sweep] WARN: audit-stale board-post failed: {e}", file=sys.stderr)
        return {"posted": False, "msg_id": None, "error": str(e)}


def _emit_out_of_window_digest(target, triggers):
    """Route a target agent's aged-out triggers as ONE digest ( outcome 3).

    Outcome 3 asks that an aged-out unconverted trigger reach its target agent
    by name rather than being dropped. One post PER TRIGGER satisfies that
    literally and defeats it in practice: the first live run had 23 of them, and
    23 posts in one cadence trains every reader to scroll past the tag — a
    louder version of the silence being fixed. Same call made for the
    user-blocker digest (aspirations-precheck 0.5b.1c). Grouping by target keeps
    the addressing exact while bounding volume by the ROSTER (~6) instead of by
    the backlog, so no per-run cap is needed.

    Every msg_id is carried as its own OOW_TAG_PREFIX tag, so dedup stays
    per-trigger: a target whose digest went out yesterday gets a new one only
    for triggers that aged out since.

    The digest does NOT re-file the aged triggers themselves — they are days
    old, so filing them past the conversion window would re-introduce exactly
    the rb-1150 audit-time/apply-time staleness the affects-probe exists to
    prevent. Instead the digest carries `requires_action_by:<target>` +
    `action_type:triage-aged-triggers`, which makes it an ordinary in-window
    trigger, so the NEXT run converts it into ONE triage goal. N possibly-spent
    Applies become one live decision, and the aged work reaches the SELECTOR
    rather than only the board — which is the gap this whole sweeper exists to
    close. Bounded: the digest converts once (msg_id dedup).

    THIS DOCSTRING USED TO SAY the digest's own conversion runs the
    cross-deployment addressing check, "so ambiguous targets are refused there
    rather than duplicated here." That was wrong, and the correction is the
    reason this function is now called only with a pre-resolved target
    (g-115-4684). Deferring the check to conversion is sufficient for a
    TRANSIENT refusal — overflow self-heals, since several conversion attempts
    fit inside WINDOW_HOURS at this cadence. It is exactly WRONG for a
    DETERMINISTIC one: an ambiguous bare name is a property of the STRING, so
    the refusal repeats on every run, the digest never converts, it ages out,
    and it is re-digested for the same target forever — one new post per audit
    cycle, each about the previous one, with the routed work never arriving.

    The caller (main) therefore runs resolve_addressing over the aged batch and
    passes only resolvable targets here. `target` is already qualifier-stripped
    and known-local; do NOT re-derive it from a trigger's raw `target` field.

    Fail-open like its sibling — a post failure leaves no tag, so the next run
    retries rather than losing the finding.
    """
    import subprocess
    # The tag written below is READ BACK by resolve_addressing on the NEXT run --
    # that read IS the conversion mechanism, so this function is a producer for a
    # validator living in the same file. `target` arrives bare and
    # qualifier-stripped (see above), and rule 3 REFUSES a bare name in the
    # collision set. So a digest for such a target could never convert, and
    # guard-2177 correctly forbids re-posting it, making the loss PERMANENT
    # rather than late. Both halves were individually right; the defect existed
    # only in their disagreement, which is why neither
    # test_insight_trigger_sweep_out_of_window.py nor
    # test_insight_trigger_sweep_addressing.py could see it. Emit rule 1's EXACT
    # form instead.
    #
    # Qualify UNCONDITIONALLY, not only on a collision: the collision set is
    # recomputed every run from the registry, so a name that is unambiguous today
    # joins it the moment a peer declares it -- and this post can never be
    # reissued to catch up. @self-env resolves to the local agent with the
    # qualifier stripped, so the non-colliding majority path is unchanged.
    # With ENVIRONMENT_ID unresolvable there is no qualified form to write: fall
    # back to bare rather than emitting `<target>@None`, which rule 1 would
    # refuse as unknown_env -- strictly worse than the status quo.
    _env = _self_env()
    req_target = f"{target}@{_env}" if _env else target
    lines = [
        f"{len(triggers)} insight_trigger(s) addressed to {target} aged out of the "
        f"{WINDOW_HOURS}h conversion window with no converting goal in any queue. "
        f"This sweep did NOT file them: at this age the premise may be spent, and "
        f"filing would re-introduce the rb-1150 audit-time/apply-time gap. "
        f"{target} decides — file the work, or reply that it is moot.",
        "",
    ]
    for t in sorted(triggers, key=lambda r: r["age_h"]):
        lines.append(
            f"  {t['msg_id']}  from {t['author']} on #{t['channel']}  "
            f"action={t['action']}  severity={t['severity']}  age={t['age_h']}h"
        )
    lines += [
        "",
        f"This post is itself tagged requires_action_by:{req_target} + "
        "action_type:triage-aged-triggers, so the NEXT run of this sweep converts "
        "it into one triage goal in your queue — the aged triggers reach the "
        "selector as a single decision, not as N re-filed stale Applies. It "
        "converts exactly once (msg_id dedup), and cross-deployment addressing is "
        "checked there, not here.",
        "",
        "No reminder follows: this note IS the dedup record. The sweep keeps "
        "reporting these under `audit:` every run, but a re-post would fire every "
        "cadence forever, because a post only gets older (guard-2177).",
    ]
    text = "\n".join(lines)
    tags = ",".join(
        ["insight-trigger-out-of-window", f"requires_action_by:{req_target}",
         "action_type:triage-aged-triggers"]
        + [f"{OOW_TAG_PREFIX}{t['msg_id']}" for t in triggers]
    )
    try:
        # sys.executable + board.py, never `bash` — rb-225/rb-247/guard-580.
        proc = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "core" / "scripts" / "board.py"),
             "post", "--channel", "coordination", "--type", "status", "--tags", tags],
            input=text, capture_output=True, text=True, timeout=15,
        )
        return {"posted": proc.returncode == 0,
                "msg_id": proc.stdout.strip() if proc.returncode == 0 else None,
                "count": len(triggers)}
    except Exception as e:
        print(f"[insight-trigger-sweep] WARN: out-of-window digest for {target} failed: {e}",
              file=sys.stderr)
        return {"posted": False, "msg_id": None, "count": len(triggers), "error": str(e)}



# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


_CONVERTED_ID_RE = re.compile(r'"(?:insight_trigger|board_post):([^"]+)"')


def load_converted_ids():
    """Single-pass scan of world + per-agent aspirations.jsonl. Returns the
    set of msg_ids already converted via EITHER origin_signal format:
      - insight_trigger:<msg_id> — this sweeper's convention
      - board_post:<msg_id> — existing insight-trigger-gate.py convention
    Dual recognition keeps the two routing mechanisms from filing duplicates
    if/when insight-trigger-gate.py gets fully wired into precheck.

    Returns a set, called ONCE per sweep run. Per-trigger dedup is then an
    O(1) set membership check in main() — single source of truth for the
    "what's already converted" view."""
    paths = [WORLD_ASPS]
    for d in sorted(_agents_root().iterdir()):
        if d.is_dir() and (d / "local-paths.conf").is_file():
            paths.append(d / "aspirations.jsonl")
    ids = set()
    for path in paths:
        _refresh(path)  # guard-980: avoid a stale git-sync mirror of aspirations.jsonl
        if not path.is_file():
            continue
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                for m in _CONVERTED_ID_RE.finditer(line):
                    ids.add(m.group(1))
    return ids


# ---------------------------------------------------------------------------
# Goal filing
# ---------------------------------------------------------------------------


def _build_goal_payload(trigger):
    """Build the JSON payload passed via stdin to aspirations.py add-goal.

    The optional `_target_record` key on `trigger` is the live goal record named
    by the trigger's `affects:<goal-id>` tag, stashed by the filing loop when it
    probes (one scan, two consumers). It is carried on the trigger rather than
    added as a parameter because file_goal is STUBBED by this sweep's own test
    suites — a new kwarg breaks every stub for no behavioural gain, and this file
    already treats the trigger dict as a mutable carrier (see the addressing
    code's local_t["target"] rewrite).
    """
    priority = SEVERITY_PRIORITY.get(trigger["severity"], "MEDIUM")
    #  item (2). Severity measures the FINDING; it says nothing about
    # how much work is queued behind the chore. Inherit upward so a chore that
    # unblocks a HIGH goal is not filed LOW (the  incident: LOW, rank
    # 10, 36h, three HIGH goals waiting).
    inherited_from = None
    target_record = trigger.get("_target_record")
    if target_record:
        promoted = inherit_priority(priority, target_record.get("priority"))
        if promoted != priority:
            inherited_from = target_record.get("id")
            priority = promoted
    title = f"Apply: {trigger['action']} (from {trigger['author']} insight_trigger {trigger['msg_id']})"
    # intended_agent vocabulary normalization (selection-stack review
    # 2026-08-21). resolve_addressing() settles WHICH DEPLOYMENT a target
    # belongs to but never checks roster MEMBERSHIP, so loose board tags
    # (requires_action_by:any / :reducer / :agent) used to be copied verbatim
    # — 5 such goals measured live. The daemon add path now REFUSES off-vocab
    # values (gates.intended_agent_vocab), so filing verbatim would error the
    # sweep on the first loose tag. Off-roster => "either", byte-identical to
    # how the read side already treats these values (); the
    # to:<target> tag below keeps the author's original addressing for
    # provenance. Empty/unreadable roster fails open to verbatim (rb-1028) —
    # matching the gate, which skips its check in the same condition.
    intended = trigger["target"]
    _roster = _local_roster()
    if _roster and intended not in _roster | {"either"}:
        print(f"[insight-trigger-sweep] NOTE: target {intended!r} "
              f"({trigger['msg_id']}) is not in the active roster; filing "
              f"intended_agent='either' (read-side equivalent, g-115-3482); "
              f"original kept in the to:{intended} tag.", file=sys.stderr)
        intended = "either"
    desc_parts = [
        f"Insight trigger from {trigger['author']} @ {trigger['timestamp']}.",
        "",
        trigger["text"],
        "",
        f"Tags: {', '.join(trigger['tags'])}",
        f"Board pointer: world/board/{trigger['channel']}.jsonl ({trigger['msg_id']})",
        "",
        "Filed automatically by core/scripts/insight-trigger-sweep.py — closes",
        "the routing gap where board action items did not reach the goal queue",
        "(canonical incident: msg-20260514-143816-bravo-1073).",
    ]
    if inherited_from:
        desc_parts[-1:] = desc_parts[-1:] + [
            "",
            f"Priority inherited: raised to {priority} from {inherited_from}, "
            f"which this chore unblocks (g-115-6590 item 2). The board post's "
            f"severity ({trigger['severity']}) would have filed this "
            f"{SEVERITY_PRIORITY.get(trigger['severity'], 'MEDIUM')}.",
        ]
    payload = {
        "title": title,
        "description": "\n".join(desc_parts),
        "priority": priority,
        "participants": ["agent"],
        "intended_agent": intended,
        "origin_signal": f"insight_trigger:{trigger['msg_id']}",
        "tags": ["insight-trigger-conversion", f"from:{trigger['author']}", f"to:{trigger['target']}"],
    }
    # Carry the target's category so the chore lands in the same lane as the
    # work it unblocks. Only when the target HAS one — never invent a category,
    # and never override a value the payload already carries.
    if target_record and target_record.get("category"):
        payload["category"] = target_record["category"]
    if inherited_from:
        payload["tags"].append(f"priority-inherited-from:{inherited_from}")
    return payload


def file_goal(trigger, *, dry_run=False):
    payload = _build_goal_payload(trigger)
    if dry_run:
        return {"would_file": True, "payload": payload}
    # --override-all: bypass every gate with one audited justification. This
    # conversion path has:
    #   - already dedup'd by msg_id substring (covers duplication-gate concern)
    #   - source board post IS the investigation artifact (covers
    #     scaffolded-exploration-gate concern)
    #   - origin_signal is correctly formed (covers origin-signal-gate)
    #   - no stale-read concern — we read findings.jsonl fresh each run
    # Override is logged to world/override-bypass-ledger.jsonl per the
    # override-bypass-ledger convention (blast radius bounded — one goal per
    # msg, msg dedup'd).
    #
    # Route via _rt.aspirations_add_goal — the canonical Python -> daemon
    # client identified by  (zeta investigation, 2026-05-17) and
    # adopted by the sibling insight-trigger-gate.py:386. The prior
    # subprocess.run(['bash', 'aspirations-add-goal.sh', ...]) shape resolved
    # 'bash' to the WSL stub on Windows (rc=127 / WinError 193) and inherited
    # CRLF under autocrlf=true. _rt skips bash entirely: pure urllib POST to
    # the local daemon. `overrides={"All": justification}` maps to the
    # X-Mind-Override-All header that --override-all used to set
    # (see _rt.py:117-128).
    justification = (
        f"insight-trigger conversion (sweep): {trigger['msg_id']} "
        f"from {trigger['author']} (msg-id dedup'd; source post is "
        "the investigation artifact)"
    )
    try:
        resp = _rt.aspirations_add_goal(
            ESCALATION_ASP, payload, source=ESCALATION_SOURCE,
            overrides={"All": justification},
        )
        return {
            "would_file": False,
            "rc": 0,
            "stdout": json.dumps(resp)[:500],
            "stderr": "",
        }
    except _rt.RtError as e:
        err_text = (e.body or str(e)).strip()
        return {
            "would_file": False,
            "rc": getattr(e, "status", None) or 1,
            "stdout": "",
            "stderr": err_text[:500],
        }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    # NOT "findings-channel" — this said so until 2026-08-19 () while the
    # module docstring above correctly said "EVERY live board channel", and the run
    # scans 9 (board_channels()). Measured that day: of 1853 insight_triggers on the
    # board, 497 are OUTSIDE findings (coordination 16, coordination-archive 36,
    # decisions 1, findings-archive 444). A reader trusting --help would conclude
    # those channels are uncovered and file work to "add" coverage that already
    # exists. --help is the surface a reader checks first; keep it in step with the
    # docstring.
    ap = argparse.ArgumentParser(description="Convert insight_triggers from every live board channel to goals.")
    ap.add_argument("--dry-run", action="store_true", help="Probe-only mode; no writes.")
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON output.")
    args = ap.parse_args()

    dry_run = args.dry_run

    raw_triggers = load_triggers()
    # : addressing resolution BEFORE dedup/filing — a refused target
    # must never reach the filing loop, and refusal-first beats dedup (the
    # safety verdict outranks the bookkeeping one).
    triggers, addressing_refused, collision_set = resolve_addressing(raw_triggers)
    #  CRITICAL SECTION opens here and closes after the filing loop.
    # It spans exactly snapshot -> file, because that pair IS the race; the
    # read-only work above (load_triggers, resolve_addressing) is deliberately
    # left outside so the lock is held for as little time as possible.
    #
    # --dry-run does NOT take the lock, and that is deliberate rather than an
    # oversight. A dry run writes nothing, so it cannot produce a duplicate; and
    # taking the lock would break this goal's own regression guard, which calls
    # for a --dry-run IMMEDIATELY AFTER a live run to confirm it reports filed=0
    # / skipped=N. If the dry run blocked on the live run's lock it could report
    # nothing at all, destroying the diagnostic. /prime Step 5.5b's
    # `--dry-run --json` consumer is unaffected for the same reason.
    sweep_lock_held = False
    if not dry_run:
        try:
            acquire_lock(SWEEP_LOCK, timeout=SWEEP_LOCK_TIMEOUT,
                         stale_seconds=SWEEP_LOCK_STALE_SECONDS)
            sweep_lock_held = True
        except TimeoutError:
            # Another live run holds the mutex. SKIP — do not wait, do not file.
            # This is a success, not a failure: the holder is sweeping the same
            # triggers, so there is no work to lose and rc=0 keeps the recurring
            # tick from reporting a phantom error.
            #
            # The output is deliberately LOUD and structurally distinct. A bare
            # `filed: 0` here is TRUE but reads exactly like a clean no-op run,
            # which is the false-clean shape this fleet keeps rediscovering
            # (guard-1760: report what was NOT looked at, never only what was).
            skip_summary = {
                "mode": "skipped-another-run-holds-lock",
                "lock_skipped": True,
                "lock_path": str(SWEEP_LOCK),
                "scanned": 0,
                "filed": 0,
                "skipped": 0,
                "reason": ("another insight-trigger-sweep run holds the run mutex; "
                           "this run filed nothing and inspected nothing"),
            }
            if args.json:
                print(json.dumps(skip_summary, indent=2))
            else:
                print("[insight-trigger-sweep] SKIPPED — another run holds the run "
                      f"mutex ({SWEEP_LOCK}). Nothing was scanned and nothing was "
                      "filed; the run that holds it covers these triggers. "
                      "This is not an error.")
            return 0
    try:
        converted_ids = load_converted_ids()
        pending = []
        skipped = []
        for t in triggers:
            if t["msg_id"] in converted_ids:
                skipped.append({"msg_id": t["msg_id"], "reason": "already_converted"})
                continue
            pending.append(t)

        filed = []
        overflow = []
        audit_stale = []  # : skipped because target already terminal
        affects_missing = []  # : filed-with-warning when target not found
        for t in pending:
            if len(filed) >= MAX_GOALS_PER_RUN:
                overflow.append(t)
                continue
            #  / rb-1150: re-probe affects:<goal-id> target status
            # before filing the Apply. Authors of insight_triggers tag with
            # `affects:<goal-id>` when the action points at a specific goal;
            # absent that tag we file unchanged (no probe target available).
            target_record = None
            if t.get("affects_goal"):
                # ONE scan, two consumers: the terminal check below and the
                # priority/category inheritance in _build_goal_payload. Calling
                # probe_goal_status here as well would scan every queue twice.
                target_record = probe_goal_record(t["affects_goal"])
                target_status = target_record.get("status") if target_record else None
                if target_status in TERMINAL_GOAL_STATES:
                    note_result = {"posted": False, "msg_id": None}
                    if not dry_run:
                        note_result = _emit_audit_stale_note(t, target_status)
                    audit_stale.append({
                        "msg_id": t["msg_id"],
                        "author": t["author"],
                        "affects_goal": t["affects_goal"],
                        "target_status": target_status,
                        "action": t["action"],
                        "note_result": note_result,
                    })
                    continue
                elif target_status is None:
                    # Target not found — file as-is with a warning (per
                    # acceptance criteria: "target missing -> file as-is with
                    # warning"). The warning lands in the affects_missing list
                    # and is surfaced in the summary.
                    affects_missing.append({
                        "msg_id": t["msg_id"],
                        "affects_goal": t["affects_goal"],
                        "warning": "affects target not found in any queue at filing time",
                    })
            if target_record:
                t["_target_record"] = target_record
            result = file_goal(t, dry_run=dry_run)
            filed.append({"trigger": t, "result": result})
    finally:
        # : released the instant filing ends, so the out-of-window
        # digest half below runs OUTSIDE the mutex. That half writes board
        # digests, not goals, and carries its own dedup (oow_routed_ids), so it
        # is not this goal's race — and holding the lock across it would extend
        # the critical section well past what the stale window is sized for.
        if sweep_lock_held:
            release_lock(SWEEP_LOCK)

    if dry_run:
        filed_count = len(filed)
        filing_failed_count = 0
    else:
        filed_count = sum(1 for f in filed if f["result"].get("rc") == 0)
        #  fresh-eyes F-2. Counted independently rather than derived as
        # `len(filed) - filed_count`, but be clear about what that does and does
        # NOT buy — the first draft of this comment claimed the stronger thing and
        # a positive control refuted it.
        #
        # MEASURED (both forms, same tests): IDENTICAL behavior, 5/5 green either
        # way. `rc == 0` and `rc != 0` are exact complements over one list, so the
        # two counts always sum to len(filed) however they are written. The change
        # is honesty-of-naming (this line counts what it says it counts, and a
        # third rc state would surface rather than hide), NOT a new guarantee.
        #
        # THE LIMITATION SURVIVES THE FIX, so do not read `conservation.holds` as
        # covering this: `filed + filing_failed` reconstructs `attempted` BY
        # CONSTRUCTION, so the identity below is structurally blind to a bug in
        # this split and no phrasing of these two lines can change that. The real
        # guarantee is a VALUE assertion in the tests — test_case_C's `filed == 1`
        # and test_split_is_exhaustive_and_valued's per-term checks, both of which
        # go red on a genuine miscount (measured). This is guard-3092 again: the
        # identity is necessary, never sufficient.
        filing_failed_count = sum(1 for f in filed if f["result"].get("rc") != 0)

    #  outcomes 2+3: the AUDIT half. Kept strictly out of the
    # conservation identity above — `scanned` is what the sweep chose to look
    # at, and folding aged-out triggers into it would silently redefine the one
    # number outcome 1 pins. Reported alongside, never inside.
    oow, oow_routed_ids, oow_truncated = load_out_of_window_triggers()
    oow_converted = [t for t in oow if t["msg_id"] in converted_ids]
    oow_unconverted = [t for t in oow if t["msg_id"] not in converted_ids]
    # Route as ONE digest per TARGET, not per trigger — see
    # _emit_out_of_window_digest for why (23 posts in one cadence on the first
    # live run). Already-routed triggers are excluded from the digest entirely,
    # so a target only ever hears about what aged out since its last digest.
    oow_unrouted = [t for t in oow_unconverted if t["msg_id"] not in oow_routed_ids]
    # Resolve the DIGEST'S OWN addressing before emitting it ().
    #
    # The digest is itself an in-window trigger by this sweep's own definition
    # (_parse_trigger_msg accepts anything with requires_action_by + action_type,
    # and there is no exclusion for action_type:triage-aged-triggers). That is
    # deliberate — it is what makes aged work reach the SELECTOR. But it means a
    # digest carrying an UNRESOLVABLE target can never convert: it ages out, its
    # own msg_id is not in oow_routed_ids (that set holds the ids it CARRIES),
    # so it is re-digested into a new digest for the same target, refused for
    # the same reason, forever — one new digest per audit cycle, each about the
    # previous one, with the routed work never arriving.
    #
    # Deferring the check to the digest's own conversion is sufficient only for
    # a TRANSIENT refusal (overflow self-heals: ~4 conversion attempts fit inside
    # WINDOW_HOURS at this cadence). It is exactly wrong for a DETERMINISTIC one
    # — an ambiguous bare name is a property of the STRING, so it repeats every
    # run and the refusal lands where nothing can act on it.
    #
    # resolve_addressing is the SSOT for the rule, so it is REUSED rather than
    # reimplemented. It is passed the FULL unconverted set (routed and unrouted)
    # plus raw_triggers, for two reasons:
    #
    #   (1) COLLISION SET. Its observation half keys on authors seen this window,
    #       and a narrower set means FEWER refusals — the unsafe direction. So it
    #       is computed over every trigger this run observed, in BOTH windows.
    #   (2) THE CLEANUP HALF DEPENDS ON IT. See below.
    #
    # The extra in-window entries are filtered back out by msg_id (the two
    # windows are disjoint by construction), so the conservation identity above
    # is untouched. Fail-open is inherited: an unreadable env registry yields an
    # empty peer set, hence an empty collision set, hence digests still emit
    # (guard-142 — a gate must not block work on its own dependency errors).
    #
    # CLEANUP for the debris this defect already produced, and the reason the
    # resolve runs over oow_unconverted rather than oow_unrouted. A digest
    # emitted BEFORE this fix carries OOW tags for the triggers it named, so
    # those ids sit in oow_routed_ids and are excluded from oow_unrouted —
    # "routed" into a post that can never convert. A target in that state has
    # NOTHING unrouted, therefore produces NO refusal. Keying the stranding
    # report off the refusal set would make it inert in precisely the case it
    # was written for: it would report clean forever while the debris sat there.
    # (Measured on the first dry-run of this fix — all 21 unconverted triggers
    # were already_routed, so a refusal-keyed report saw zero. That is the same
    # vacuous-bucket class as the omitted out-of-window count this whole goal
    # exists to close, reproduced inside its own remedy.)
    #
    # Resolving the WHOLE unconverted set instead splits the refusals by which
    # bucket the msg_id came from: unrouted -> blocked now; already-routed -> its
    # prior digest was unconvertible, so it is stranded (guard-1532 — a refusal
    # whose consequence is unnamed pushes the reader onto whatever exit remains).
    oow_ids = {t["msg_id"] for t in oow_unrouted}
    _oow_res, _oow_ref, oow_collision = resolve_addressing(oow_unconverted + raw_triggers)
    oow_resolved = [t for t in _oow_res if t["msg_id"] in oow_ids]
    oow_digest_refused = [r for r in _oow_ref if r["msg_id"] in oow_ids]
    oow_stranded_by_prior_digest = {}
    for r in _oow_ref:
        if r["msg_id"] in oow_ids or r["msg_id"] not in oow_routed_ids:
            continue
        oow_stranded_by_prior_digest.setdefault(r["target"], []).append(r["msg_id"])
    for v in oow_stranded_by_prior_digest.values():
        v.sort()
    by_target = {}
    for t in oow_resolved:
        by_target.setdefault(t["target"], []).append(t)
    oow_digests = []
    for target in sorted(by_target):
        batch = by_target[target]
        if dry_run:
            res = {"posted": False, "msg_id": None, "count": len(batch), "reason": "dry_run"}
        else:
            res = _emit_out_of_window_digest(target, batch)
        oow_digests.append({
            "target": target,
            "count": len(batch),
            "msg_ids": [t["msg_id"] for t in batch],
            "result": res,
        })
    oow_details = [
        {"msg_id": t["msg_id"], "author": t["author"], "channel": t["channel"],
         "target": t["target"], "action": t["action"], "severity": t["severity"],
         "age_h": t["age_h"], "already_routed": t["msg_id"] in oow_routed_ids}
        for t in oow_unconverted
    ]
    oow_newly_routed = sum(d["count"] for d in oow_digests if d["result"].get("posted"))

    #  fresh-eyes F-1: computed ONCE. The sum was written twice below
    # (for "sum" and again inside "holds"); an editor changing one and not the
    # other yields a `holds` that disagrees with the `sum` printed beside it,
    # and nothing would catch it. Asserted by test_terms_match_source, which
    # now also pins `holds == (sum == scanned)`.
    _conservation_sum = (
        filed_count + filing_failed_count + len(addressing_refused)
        + len(skipped) + len(audit_stale) + len(overflow)
    )

    summary = {
        "mode": "dry-run" if dry_run else "sweep",
        "window_hours": WINDOW_HOURS,
        "grace_hours": GRACE_HOURS,
        # : report the channels actually read. A silently-narrow
        # scan is the exact defect this replaces, so the scope must be
        # visible in the output rather than inferable from the source.
        "channels_scanned": [p.stem for p in board_channels()],
        "scanned": len(raw_triggers),
        # : refusals must be VISIBLE in the output, not inferable
        # from source — a silent skip is the defect class the rule bans.
        "collision_set": collision_set,
        "addressing_refused": len(addressing_refused),
        "addressing_refused_details": addressing_refused,
        # clause 3b (): a collision-set name that resolved because
        # its author is a non-colliding local. Surfaced for the same reason
        # refusals are — this is the ONE path that resolves a name the rule
        # otherwise refuses, so it must be countable, not inferable.
        "addressing_author_scoped": sum(
            1 for t in triggers if t.get("addressing") == "author_scoped_local"
        ),
        "skipped_already_converted": len(skipped),
        "audit_stale": len(audit_stale),
        # : affects_missing is an ANNOTATION on the filed set, NOT a
        # disposition. :936-945 appends here and then FALLS THROUGH to file_goal,
        # so its members are ALSO counted in attempted/filed. Including it as an
        # identity term double-counts (measured case A: scanned=1, sum=2). It is
        # reported here for visibility and deliberately excluded from
        # `conservation` below — see the key name, which now says so.
        "affects_missing_annotation": len(affects_missing),
        "filed": filed_count,
        "attempted": len(filed),
        # : an attempted filing that returned rc != 0 is in `filed` but
        # excluded from `filed_count` (:952), so before this key it landed in NO
        # bucket at all (measured case B: scanned=1, sum=0). The two errors
        # CANCEL when one of each occurs in the same run (measured case C:
        # scanned=2, sum=2, identity holds, goals actually created=1) — which is
        # why a bare `scanned` assertion could never catch either.
        "filing_failed": filing_failed_count,
        "overflow": len(overflow),
        # : the conservation identity, COMPUTED rather than asserted in
        # prose. Four prose defenses claimed this "stays exact" (:105-110, the
        # companion test docstring, 's acceptance criterion) and no test
        # ever summed the terms — so both defects below survived every green run.
        # Emitting `holds` makes the invariant checkable by the recurring goal
        # that depends on it, instead of hand-verified in close notes.
        # guard-3092: conservation is NECESSARY BUT NOT SUFFICIENT — a run where
        # the terms sum correctly can still have created zero goals (case C), so
        # read `filed` alongside `holds`, never `holds` alone.
        "conservation": {
            "terms": [
                "filed", "filing_failed", "addressing_refused",
                "skipped_already_converted", "audit_stale", "overflow",
            ],
            "sum": _conservation_sum,
            "scanned": len(raw_triggers),
            "holds": _conservation_sum == len(raw_triggers),
        },
        # --- audit half (). NOT terms of the conservation identity. ---
        "audit_window_hours": AUDIT_WINDOW_HOURS,
        "out_of_window": len(oow),
        "out_of_window_converted": len(oow_converted),
        "out_of_window_unconverted": len(oow_unconverted),
        "out_of_window_newly_routed": oow_newly_routed,
        "out_of_window_already_routed": sum(1 for d in oow_details if d["already_routed"]),
        "out_of_window_digests": oow_digests,
        # : digests REFUSED because their own target is unresolvable.
        # Reported as a first-class count including the healthy zero — the whole
        # lineage of this goal is that an omitted bucket reads identically to an
        # empty one, and this bucket names work that is now reaching NOBODY.
        "out_of_window_digest_refused": len(oow_digest_refused),
        "out_of_window_digest_refused_details": oow_digest_refused,
        # Collision set for the AUDIT pass. Deliberately separate from
        # `collision_set` above: this one is computed over both windows' authors,
        # so the two can legitimately differ and a reader must be able to see
        # which one produced a given refusal.
        "out_of_window_collision_set": oow_collision,
        # Triggers a PRE-FIX digest already claimed to have routed, whose digest
        # was unconvertible. Suppressed from re-routing by their own OOW tags.
        "out_of_window_stranded_by_prior_digest": oow_stranded_by_prior_digest,
        # Both-tagged posts older than AUDIT_WINDOW_HOURS. Named so a reader can
        # tell "none aged out" from "I stopped looking" (guard-1760).
        "audit_truncated_older_than_window": oow_truncated,
        "out_of_window_details": oow_details,
        "pending": [
            {
                "msg_id": t["msg_id"],
                "author": t["author"],
                "target": t["target"],
                "action": t["action"],
                "severity": t["severity"],
                "affects_goal": t.get("affects_goal"),
                "age_h": t["age_h"],
            }
            for t in pending
        ],
        "filed_details": filed,
        "audit_stale_details": audit_stale,
        "affects_missing_details": affects_missing,
        "overflow_details": [{"msg_id": t["msg_id"], "target": t["target"]} for t in overflow],
    }

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        # Human-readable
        print(f"[insight-trigger-sweep] mode={summary['mode']} "
              f"channels={','.join(summary['channels_scanned']) or 'none'} "
              f"scanned={summary['scanned']} "
              f"addressing_refused={summary['addressing_refused']} "
              f"addressing_author_scoped={summary['addressing_author_scoped']} "
              f"skipped={summary['skipped_already_converted']} filed={summary['filed']} "
              f"audit_stale={summary['audit_stale']} "
              f"filing_failed={summary['filing_failed']} "
              f"overflow={summary['overflow']} "
              # : rendered AFTER overflow and named as an annotation,
              # so the human line groups the six identity terms together and
              # cannot be misread as a seventh disposition (which is how the
              # double-count went unnoticed inline at the old position).
              f"| affects_missing_annotation={summary['affects_missing_annotation']} "
              f"conservation={'OK' if summary['conservation']['holds'] else 'BROKEN'}")
        for r in addressing_refused:
            tag = r["verdict"].upper().replace("_", "-")
            print(f"  REFUSED-{tag}: {r['msg_id']} from {r['author']} "
                  f"target={r['target']} -- {r['reason']}")
        for f in filed:
            t = f["trigger"]
            r = f["result"]
            status = "DRY-RUN-WOULD-FILE" if dry_run else ("OK" if r.get("rc") == 0 else f"FAIL rc={r.get('rc')}")
            print(f"  {status}: {t['msg_id']} {t['author']}->{t['target']} action={t['action']} severity={t['severity']}")
            if not dry_run and r.get("rc") != 0:
                print(f"    stderr: {r.get('stderr')[:200]}")
        for a in audit_stale:
            print(f"  AUDIT-STALE: {a['msg_id']} targeted {a['affects_goal']} (action={a['action']}) -- already {a['target_status']}")
        for am in affects_missing:
            print(f"  WARN: {am['msg_id']} affects_goal={am['affects_goal']} not found in any queue; filed as-is")
        if overflow:
            print(f"  WARN: {len(overflow)} additional triggers exceeded MAX_GOALS_PER_RUN={MAX_GOALS_PER_RUN}")
        # Audit half — printed on its own line so it can never be misread as a
        # conservation term. Always printed, including the zero case: "0 aged
        # out over a 168h audit" is the reassurance the sweep previously could
        # not give, and a line that appears only on trouble teaches readers that
        # its absence means nothing was checked.
        print(f"  audit: window={AUDIT_WINDOW_HOURS}h out_of_window={summary['out_of_window']} "
              f"(converted={summary['out_of_window_converted']} "
              f"unconverted={summary['out_of_window_unconverted']}) "
              f"routed_now={summary['out_of_window_newly_routed']} "
              f"already_routed={summary['out_of_window_already_routed']} "
              f"digest_refused={summary['out_of_window_digest_refused']} "
              f"older_than_audit_window={summary['audit_truncated_older_than_window']}")
        for r in oow_digest_refused:
            tag = r["verdict"].upper().replace("_", "-")
            print(f"  OUT-OF-WINDOW DIGEST REFUSED-{tag}: {r['msg_id']} from {r['author']} "
                  f"target={r['target']} -- {r['reason']}")
            print(f"      NOT ROUTED. This trigger now reaches nobody until its target is "
                  f"resolvable; emitting the digest anyway would recurse once per audit cycle.")
        for tgt, stranded in sorted(oow_stranded_by_prior_digest.items()):
            print(f"  OUT-OF-WINDOW STRANDED (pre-fix debris) target={tgt}: "
                  f"{len(stranded)} trigger(s) carry OOW tags from a digest that cannot "
                  f"convert, so re-routing is suppressed: {', '.join(stranded)}")
        for dg in oow_digests:
            state = ("DRY-RUN-WOULD-ROUTE" if dry_run
                     else "ROUTED" if dg["result"].get("posted")
                     else "ROUTE-FAILED")
            print(f"  OUT-OF-WINDOW DIGEST {state} -> {dg['target']}: {dg['count']} trigger(s) "
                  f"{dg['result'].get('msg_id') or ''}".rstrip())
            for mid in dg["msg_ids"]:
                d = next(x for x in oow_details if x["msg_id"] == mid)
                print(f"      {mid} from {d['author']} action={d['action']} "
                      f"severity={d['severity']} age={d['age_h']}h (#{d['channel']})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
