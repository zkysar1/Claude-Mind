"""Aspiration Trajectory View — compiles the full learning arc for an aspiration.

Inspired by NVIDIA AVO (arXiv:2603.24517) — gives the agent access to the full
lineage of prior work and scores, enabling trajectory-level reasoning about
progress shape, inflection points, and stagnation.

Usage:
    python aspiration-trajectory.py <asp-id> [asp-id ...]

    Single ID:  outputs a flat JSON trajectory object (backward compatible).
    Multiple IDs: loads shared data once, outputs {"asp-id": trajectory, ...}.

Output: JSON object with trajectory data including:
    - Completed goals in chronological order with learning artifacts
    - Capability level changes over time
    - Inflection points (goals that produced significant learning)
    - Current learning velocity
    - Plateau and diminishing returns detection
    - Credit-pending classification (g-306-518): a worker-closed goal whose
      learning has not reached the stores yet is reported, not scored 0
"""
import json
import sys
from datetime import datetime
from pathlib import Path

# --- Path setup ---
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import WORLD_DIR, AGENT_DIR, CONFIG_DIR, PROJECT_ROOT
from _long_path import open_long_path
import worker_retrospective as _retro

def load_jsonl(path):
    """Load a JSONL file, returning list of dicts."""
    records = []
    p = Path(path)
    if not p.exists():
        return records
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records

def load_yaml(path):
    """Load a YAML file. Crashes if yaml not installed or file is malformed."""
    import yaml
    p = Path(path)
    if not p.exists():
        return None
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def build_tree_attribution_map(tree_dir):
    """Scan tree .md files, extract goal-id attribution from front matter.

    Returns {goal_id: count_of_nodes_attributed}. A node attributes when either
    (a) front matter has a clean source_goal: 'g-NNN-NN' field, or (b) the free-
    text last_update_trigger.source starts with a goal-id (extract the prefix).

    Both schemas exist in world/knowledge/tree/. Files without a goal-attributable
    source (semantic descriptors like 'DECOMPOSE category', 'tree_growth') are
    skipped — those are tree-internal events, not goal-driven encoding.

    rb-601 fix — replaces the broken count_learning_artifacts() category-key
    proxy that read _tree.yaml node.last_retrieved instead of per-file
    last_updated provenance.
    """
    import re
    import yaml

    GOAL_ID_PFX = re.compile(r'^(g-\d+-\d+)\b')
    attribution = {}

    tree_root = Path(tree_dir)
    if not tree_root.is_dir():
        return attribution

    for md_path in tree_root.rglob("*.md"):
        try:
            # rb-450 / : tree nodes under deeply-nested categories can
            # exceed Windows MAX_PATH (260 chars). Path.read_text() doesn't
            # expose the long-path retry; route through open_long_path so
            # attribution counts don't silently drop those files. POSIX no-op.
            with open_long_path(md_path) as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        if not text.startswith("---"):
            continue
        end_idx = text.find("\n---", 4)
        if end_idx < 0:
            continue
        try:
            fm = yaml.safe_load(text[4:end_idx]) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(fm, dict):
            continue

        # Prefer clean source_goal field if present
        sg = fm.get("source_goal")
        if isinstance(sg, str):
            m = GOAL_ID_PFX.match(sg.strip())
            if m:
                gid = m.group(1)
                attribution[gid] = attribution.get(gid, 0) + 1
                continue

        # Fall back: extract leading goal-id from free-text source
        trigger = fm.get("last_update_trigger") or {}
        if not isinstance(trigger, dict):
            continue
        source = trigger.get("source", "")
        if not isinstance(source, str):
            continue
        m = GOAL_ID_PFX.match(source.strip())
        if m:
            gid = m.group(1)
            attribution[gid] = attribution.get(gid, 0) + 1

    return attribution


# Framework code credited from git history (): the file classes the
# header scan covered, at ANY depth, so tests/ and gates/ count too.
FRAMEWORK_CODE_LANES = (
    ("core/scripts/", (".py", ".sh")),
    ("core/config/conventions/", (".md",)),
)
# A goal id keeps the one-letter suffix a split child carries (-a); a
# longer tail annotates the parent ("-followup" credits ).
_GOAL_ID = r"\bg-\d+-\d+(?:-[a-z])?\b"


def commit_author_goal_ids(subject, body=""):
    """Goal ids a commit message names in an AUTHORSHIP position. Pure.

    The subject's head -- the text before the first ": " -- wins:
    "fix(g-306-519): ...", "g-115-9750 outcome 3: ...", "B1b (g-353-82): ...".
    Only when the head names no goal does a trailing (...) or [...] group
    count: "feat(verify): ... (g-375-48)", "... [g-115-1570]",
    "... (g-373-16 R3)". Only when the subject names none does the body
    count, through three tags: a first line that OPENS with goal ids
    ("g-115-3289. Adds ..."), a paragraph that is ONLY goal ids, and a
    "Goal:" line. The paragraph test matters: a wrapped sentence can leave
    ids alone on a line ("...pre-existing, owned by / g-115-8170 /
    g-115-8140."), and those are owners, not authors. A "Refs:" list is not
    a tag: it mixes the goal with guards and parents. An id anywhere else is
    a citation ("fix(tests): the g-115-9588
    guard was vacuous"), and so is a trailing id beside a head id: in all 7
    such subjects it named a parent or companion goal. Census (2026-09-27,
    the 4,590 commits touching FRAMEWORK_CODE_LANES code): the subject names
    the author in 3,814, a body tag in 87 of the other 776.
    git's own `Revert "..."` / `Reapply "..."` subjects quote the reverted
    commit, so they credit nobody; a goal's own "revert(g-NNN-NN): ..." is
    its work.

    goal-pickup-coordination-check.py commit_goal_id() is a single-id,
    scope-first sibling built for overlap detection: it misses the trailing
    form and reads a scope-less subject's first id as its goal.
    """
    import re
    if re.match(r'(?:Revert|Reapply) "', subject):
        return set()
    head, sep, _ = subject.partition(": ")
    if sep:
        ids = set(re.findall(_GOAL_ID, head))
        if ids:
            return ids
    m = re.search(r"[(\[]([^()\[\]]*)[)\]]\s*$", subject)
    ids = set(re.findall(_GOAL_ID, m.group(1))) if m else set()
    if ids:
        return ids
    raw = body.splitlines()
    lines = [line.strip() for line in raw if line.strip()]
    m = re.match(rf"(?:{_GOAL_ID}[\s,/+&]*)+", lines[0]) if lines else None
    if m:
        ids.update(re.findall(_GOAL_ID, m.group(0)))
    for i, line in enumerate(raw):
        text = line.strip()
        opens_paragraph = i == 0 or not raw[i - 1].strip()
        if (opens_paragraph and re.fullmatch(rf"(?:{_GOAL_ID}[\s,/+&.;]*)+", text)) \
                or text.startswith(("Goal:", "Goals:")):
            ids.update(re.findall(_GOAL_ID, text))
    return ids


def build_framework_code_attribution(root=PROJECT_ROOT):
    """{goal_id: distinct FRAMEWORK_CODE_LANES files its commits changed}.

    Walks the non-merge history of HEAD once (~1s over 4.6k commits) and
    credits each commit's changed code files to the goals
    commit_author_goal_ids() reads from its message. Crediting what a goal
    CHANGED, not the ids a file MENTIONS, is what lets a Fix that edits an
    existing file, or only its tests, score -- and keeps a file that merely
    cites a goal from crediting it. A goal id is a topic (guard-5399): scoped
    follow-ups reuse it and credit it. A file whose blob did not change (a
    mode-only chmod) is not authored content: one normalization commit
    otherwise credited 434 files. Known limit: an iteration commit that
    sweeps other work's dirty files credits them to its own goal (1
    non-recurring such commit among 3,899 credited, 2026-09-27). Returns
    None, with the reason on stderr, when history is unreadable (no .git on
    a transplanted deployment, no git binary, a timeout).
    """
    import subprocess
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "-c", "core.quotePath=off", "log",
             "--no-merges", "--no-renames", "--format=%x1e%s%x1f%b%x1f",
             "--raw", "--no-abbrev", "--",
             *(lane for lane, _ in FRAMEWORK_CODE_LANES)],
            capture_output=True, encoding="utf-8", errors="replace",
            timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"aspiration-trajectory: git log failed: {exc}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        print(f"aspiration-trajectory: git log rc={proc.returncode}: "
              f"{proc.stderr.strip()[:200]}", file=sys.stderr)
        return None
    files_by_goal = {}
    for record in proc.stdout.split("\x1e")[1:]:
        subject, _, rest = record.partition("\x1f")
        body, _, raw = rest.partition("\x1f")
        goals = commit_author_goal_ids(subject.strip(), body)
        if not goals:
            continue
        code = set()
        for line in raw.splitlines():
            # ":<old mode> <new mode> <old blob> <new blob> <status>\t<path>"
            meta, _, path = line.partition("\t")
            fields = meta.split()
            if len(fields) == 5 and fields[2] != fields[3] and any(
                    path.startswith(lane) and path.endswith(sfx)
                    for lane, sfx in FRAMEWORK_CODE_LANES):
                code.add(path)
        for gid in goals:
            files_by_goal.setdefault(gid, set()).update(code)
    return {gid: len(files) for gid, files in files_by_goal.items() if files}


def build_script_convention_attribution_map():
    """{goal_id: count} of code artifacts -- scripts and conventions -- each
    goal authored.

    Closes the gap where development goals whose primary deliverable is
    code or convention text receive zero learning-artifact credit from
    count_learning_artifacts (which only counts rb/guard/pattern/tree
    encodings). Without this, aspirations like asp-282 — which produced
    capability-route-gate.py, agent-lanes.md, and the intended_agent
    schema — show velocity=0 and trip precheck-eval cycles detector's
    zero_learning_velocity false-positive (g-115-595 / rb-803 / g-115-596).

    Framework lane (FRAMEWORK_CODE_LANES): +1 per distinct file a goal's
    commits changed, read from git history by
    build_framework_code_attribution() (g-306-519). It replaced crediting
    each goal id in a file's first 4000 chars, which scored every Fix to an
    existing file, or to its tests, 0 and credited ids a header only cited.

    World lane ({WORLD_DIR}/scripts/*.sh,*.py, {WORLD_DIR}/conventions/*.md):
    the world dir is external and gitignored, so it has no history, and a
    file there still credits each goal id in its first 4000 chars. That is a
    MENTION, not authorship: a known limitation of this lane alone. When
    framework history is unreadable, the framework lane falls back to the
    same scan over its top-level files and says so on stderr.
    """
    import re
    pat = re.compile(r"\bg-\d+-\d+\b")
    attribution = build_framework_code_attribution()

    scan_targets = [
        (WORLD_DIR / "scripts", ("*.sh", "*.py")),
        (WORLD_DIR / "conventions", ("*.md",)),
    ]
    if attribution is None:
        print("aspiration-trajectory: git history unreadable -- framework "
              "code credited by header mention (the pre-g-306-519 scan)",
              file=sys.stderr)
        attribution = {}
        scan_targets[:0] = [
            (PROJECT_ROOT / "core" / "scripts", ("*.py", "*.sh")),
            (CONFIG_DIR / "conventions", ("*.md",)),
        ]

    for root, patterns in scan_targets:
        if not isinstance(root, Path) or not root.is_dir():
            continue
        for glob_pat in patterns:
            for path in root.glob(glob_pat):
                try:
                    text = path.read_text(encoding="utf-8")[:4000]
                except (OSError, UnicodeDecodeError):
                    continue
                for gid in set(pat.findall(text)):
                    attribution[gid] = attribution.get(gid, 0) + 1

    return attribution


def load_config():
    """Load plateau detection config from core/config/aspirations.yaml.

    This is the single source of truth for plateau detection parameters.
    If the config file is missing or malformed, the script crashes — that's
    intentional. Do not add fallback defaults here.
    """
    cfg = load_yaml(CONFIG_DIR / "aspirations.yaml")
    return cfg["plateau_detection"]

def find_aspiration(asp_id, asp_sources):
    """Find aspiration by ID across pre-loaded world and agent sources."""
    for source_records in asp_sources:
        for rec in source_records:
            if rec.get("id") == asp_id:
                return rec
    return None

# Fields carrying a real completion time, most precise first. `completed_at` is
# a full timestamp; `completed_date` is DATE-ONLY, so it is the fallback rather
# than the primary -- several goals routinely close on the same day and the
# consumers below care about their order. `started` is the last resort because a
# goal's claim time is not its completion time, but it is a real instant and so
# still orders correctly against the others far more often than not.
_COMPLETION_TIME_FIELDS = ("completed_at", "completed_date", "started")


def goal_completion_order_key(g):
    """Total order on real completion time ().

    THE BUG THIS REPLACES WAS A PARTITION WEARING AN ORDERING'S CLOTHES. The old
    key was `(0, started)` when `started` existed and `(1, epoch + seq_days)`
    otherwise, and its comment stated the intent as "goals with timestamps sort
    first; goals without sort after" -- which IS the defect, not a description of
    it. Every timestamped goal preceded every un-timestamped one regardless of
    when either actually completed, and the un-timestamped bucket was ordered by
    a FABRICATED date synthesized from the goal-id sequence number.

    That is fatal here specifically because every consumer of this array takes a
    trailing recency slice or walks it pairwise (see get_completed_goals). Since
    the daemon stamps `started` at CLAIM time, every newly-claimed goal joins
    bucket 0 and bucket 1 can never gain a member -- so the "last N goals" window
    was pinned to a frozen historical set that receded further from the present
    with every goal closed. Measured on ZDS asp-025: 96 completed, 58 timestamped
    and 38 not, so the last-5 window was drawn entirely from the un-timestamped
    tail and reported velocity=0.00 for an aspiration that was producing
    artifacts that week.

    THE RESIDUAL PARTITION HERE IS DELIBERATE, MEASURED, AND POINTS THE OTHER
    WAY. A goal carrying none of the three fields has no time information at all,
    so no key can place it honestly; this returns a sentinel that sorts it LAST
    rather than inventing an instant for it. Two things make that the safe
    direction rather than a smaller copy of the same mistake. It covers 5 of 4487
    live completed goals (0.11%) against the old key's 38-of-96 (40%) in the
    measured case. And those 5 are the NEWEST goals, not ancient ones -- the
    stamp is written by the daemon around close, so the unstamped population is
    whatever just closed. Sending them to the far past, which is the reflexive
    choice, would push the freshest work out of the recency window and reproduce
    the exact defect being fixed. Python's sort is stable, so they hold their
    file order among themselves.
    """
    for field in _COMPLETION_TIME_FIELDS:
        raw = g.get(field)
        if not raw:
            continue
        try:
            return (0, datetime.fromisoformat(str(raw)))
        except (ValueError, TypeError):
            continue
    return (1, datetime.min)


def get_completed_goals(asp):
    """Completed goals in true completion order, oldest first.

    ORDER IS LOAD-BEARING FOR FOUR CONSUMERS, so do not weaken this to a
    partition again: compute_learning_velocity and detect_diminishing_returns
    each take a trailing `[-window:]` slice, detect_plateau delegates to the
    former, and detect_inflection_points walks adjacent pairs (a discontinuity
    in the ordering manufactures a spurious jump at the seam). The commissioning
    goal named only the first three; the fourth is why the enumeration is
    written down here (guard-1737).
    """
    goals = asp.get("goals", [])
    completed = [g for g in goals if g.get("status") == "completed"]
    completed.sort(key=goal_completion_order_key)
    return completed

def count_learning_artifacts(goal, reasoning_bank, guardrails, pattern_sigs,
                             tree_data, tree_attribution=None,
                             script_convention_attribution=None):
    """Count learning artifacts produced by or attributable to a goal."""
    gid = goal.get("id", "")

    artifacts = {
        "reasoning_bank_entries": 0,
        "guardrails_created": 0,
        "pattern_signatures": 0,
        "tree_nodes_updated": 0,
        "scripts_conventions_authored": 0,
    }

    # Count reasoning bank entries sourced from this goal
    for rb in reasoning_bank:
        if rb.get("source_goal") == gid:
            artifacts["reasoning_bank_entries"] += 1

    # Count guardrails whose source mentions this goal ID.
    # Only match on goal ID — date-based matching over-counts when
    # multiple goals run the same day (inflates velocity, masks plateaus).
    for g in guardrails:
        source = g.get("source", "")
        if gid and gid in source:
            artifacts["guardrails_created"] += 1

    # Count pattern signatures from this goal
    for ps in pattern_sigs:
        if ps.get("source_goal") == gid:
            artifacts["pattern_signatures"] += 1

    # Tree-node attribution by goal_id from per-file front matter (rb-601 fix).
    # Prior implementation used goal.category as a tree-node-key proxy AND read
    # _tree.yaml node.last_retrieved (the field that fires on retrieval, not
    # update) — both incorrect, returning 0 for every goal. tree_attribution
    # is built once in load_shared_data() via build_tree_attribution_map().
    if tree_attribution and gid:
        artifacts["tree_nodes_updated"] = tree_attribution.get(gid, 0)

    # Script + convention attribution ( / rb-803).
    # Counts files in core/scripts, world/scripts, core/config/conventions,
    # and world/conventions whose header region mentions this goal-id.
    # Lifts the zero_learning_velocity false-positive on aspirations whose
    # primary deliverable is code or convention text rather than
    # rb/guard/tree entries —  was the canonical incident.
    if script_convention_attribution and gid:
        artifacts["scripts_conventions_authored"] = \
            script_convention_attribution.get(gid, 0)

    return artifacts


# The two capture lanes whose drain lands what count_learning_artifacts counts:
# spark_capture -> rb / guardrails / pattern signatures (Worker Spark Replay),
# encoding_capture -> tree nodes (worker_retrospective's encoding lane).
# exp_capture and hyp_capture feed stores this instrument does not count.
CREDIT_CAPTURE_SLOTS = ("spark_capture", _retro.ENC_SLOT)


def load_pending_capture_goal_ids(root=PROJECT_ROOT):
    """Goal ids with entries still undrained in a CREDIT_CAPTURE_SLOTS slot.

    Read through worker_retrospective's slot loader (wm-read.sh, never off disk:
    the slot is daemon-owned and BODY_WM_PATH makes its path role-dependent). On
    the reducer -- the Body that runs evolve -- that is the merged reducer WM.
    Returns None when a slot read FAILED (the UNREADABLE sentinel, g-306-348):
    "drained" and "unseen" are then indistinguishable, and classify_credit
    resolves that toward pending.
    """
    ids = set()
    for slot in CREDIT_CAPTURE_SLOTS:
        captures = _retro._load_capture_slot(Path(root), slot)
        if captures is _retro.UNREADABLE:
            return None
        ids.update(captures)
    return ids


def classify_credit(goal, total_artifacts, pending_capture_ids):
    """None when the goal's learning credit is SETTLED, else why it is pending.

    g-306-518. A worker Body never writes rb, guardrails or the tree: its
    learning arrives only when the reducer merges the Body's WM, runs the
    retrospective (which stamps the goal's _retro.MARKER_FIELD) and drains the
    capture slots -- measured lags run to weeks. Until then the goal counts 0 by
    construction, and a trailing window of such zeros reads as a plateau that
    arms evolve Step 1.5's pivot. Only a goal that could be a FALSE zero is ever
    pending:
      - not stamped completed_by_role=worker -> settled (reducer-or-unknown
        closes keep their old treatment, so detection there is unchanged);
      - any artifact already attributed -> settled (counting it cannot
        manufacture a false zero);
      - no retrospective marker -> pending (its Body is unmerged, or the
        retrospective has not landed for it; either way nothing has arrived);
      - slots unreadable -> pending (cannot tell drained from unseen);
      - id still in a capture slot -> pending (merged, not yet drained).
    A Body that dies without staging its WM leaves its goals pending for good
    (g-306-520 owns that loss); they are listed, never counted as zeros.
    """
    if str(goal.get("completed_by_role") or "").strip().lower() != "worker":
        return None
    if total_artifacts > 0:
        return None
    if not str(goal.get(_retro.MARKER_FIELD) or "").strip():
        return "no-retrospective-marker"
    if pending_capture_ids is None:
        return "capture-slots-unreadable"
    if goal.get("id") in pending_capture_ids:
        return "capture-undrained"
    return None


def compute_learning_velocity(goal_artifacts, window):
    """Compute learning velocity over the last N goals."""
    if len(goal_artifacts) < window:
        recent = goal_artifacts
    else:
        recent = goal_artifacts[-window:]

    if not recent:
        return 0.0

    total = 0
    for ga in recent:
        a = ga["artifacts"]
        total += (a["reasoning_bank_entries"] + a["guardrails_created"]
                  + a["pattern_signatures"] + a["tree_nodes_updated"]
                  + a.get("scripts_conventions_authored", 0))
    return total / len(recent)

def detect_inflection_points(goal_artifacts):
    """Find goals where learning yield jumped significantly."""
    if len(goal_artifacts) < 2:
        return []

    inflections = []
    for i in range(1, len(goal_artifacts)):
        prev = goal_artifacts[i - 1]
        curr = goal_artifacts[i]

        prev_total = sum(prev["artifacts"].values())
        curr_total = sum(curr["artifacts"].values())

        # Inflection = significant jump from low to high
        if curr_total >= 2 and curr_total >= prev_total + 2:
            inflections.append({
                "goal_id": curr["goal_id"],
                "title": curr["title"],
                "index": i,
                "artifacts_before": prev_total,
                "artifacts_at": curr_total,
                "description": f"Learning yield jumped from {prev_total} to {curr_total} artifacts"
            })

    return inflections

def detect_plateau(goal_artifacts, config):
    """Detect if learning velocity has plateaued."""
    window = config.get("velocity_window", 5)
    threshold = config.get("plateau_threshold", 0.2)

    if len(goal_artifacts) < window:
        return False

    velocity = compute_learning_velocity(goal_artifacts, window)
    return velocity < threshold

def detect_diminishing_returns(goal_artifacts, config):
    """Detect if learning yield is declining monotonically."""
    window = config.get("diminishing_returns_window", 5)

    if len(goal_artifacts) < window:
        return False

    recent = goal_artifacts[-window:]
    yields = [sum(ga["artifacts"].values()) for ga in recent]

    # Check monotonic decline (each value <= previous)
    for i in range(1, len(yields)):
        if yields[i] > yields[i - 1]:
            return False
    # Ensure it's actually declining (not just flat at zero)
    return yields[0] > yields[-1]

def load_shared_data():
    """Load data stores shared across all aspirations (load once, use many).

    Returns a dict with config, reasoning_bank, guardrails, pattern_sigs,
    tree_data, and asp_sources (pre-parsed aspiration JSONL records).
    """
    asp_sources = []
    for source_path in [WORLD_DIR / "aspirations.jsonl",
                        AGENT_DIR / "aspirations.jsonl" if AGENT_DIR else None]:
        if source_path and source_path.exists():
            asp_sources.append(load_jsonl(source_path))
        else:
            asp_sources.append([])
    return {
        "config": load_config(),
        "reasoning_bank": load_jsonl(WORLD_DIR / "reasoning-bank.jsonl"),
        "guardrails": load_jsonl(WORLD_DIR / "guardrails.jsonl"),
        "pattern_sigs": load_jsonl(WORLD_DIR / "pattern-signatures.jsonl"),
        "tree_data": load_yaml(WORLD_DIR / "knowledge" / "tree" / "_tree.yaml"),
        "tree_attribution": build_tree_attribution_map(WORLD_DIR / "knowledge" / "tree"),
        "script_convention_attribution": build_script_convention_attribution_map(),
        "pending_capture_goal_ids": load_pending_capture_goal_ids(),
        "asp_sources": asp_sources,
    }

def build_trajectory(asp_id, shared=None):
    """Build the full trajectory view for an aspiration.

    Args:
        asp_id: Aspiration ID to build trajectory for.
        shared: Pre-loaded shared data from load_shared_data().
                If None, loads fresh (single-ID backward compat).
    """
    if shared is None:
        shared = load_shared_data()

    asp = find_aspiration(asp_id, asp_sources=shared["asp_sources"])
    if not asp:
        return {"error": f"Aspiration {asp_id} not found"}

    config = shared["config"]
    completed = get_completed_goals(asp)

    reasoning_bank = shared["reasoning_bank"]
    guardrails = shared["guardrails"]
    pattern_sigs = shared["pattern_sigs"]
    tree_data = shared["tree_data"]
    tree_attribution = shared.get("tree_attribution", {})
    script_convention_attribution = shared.get("script_convention_attribution", {})
    # Absent key = no slot data supplied = unreadable (resolves toward pending).
    pending_capture_ids = shared.get("pending_capture_goal_ids")

    # Build per-goal artifact counts
    goal_artifacts = []
    for g in completed:
        artifacts = count_learning_artifacts(g, reasoning_bank, guardrails,
                                            pattern_sigs, tree_data,
                                            tree_attribution,
                                            script_convention_attribution)
        total = sum(artifacts.values())
        pending_reason = classify_credit(g, total, pending_capture_ids)
        goal_artifacts.append({
            "goal_id": g.get("id", "unknown"),
            "title": g.get("title", ""),
            "category": g.get("category", ""),
            "started": g.get("started"),
            "priority": g.get("priority", "MEDIUM"),
            "completed_by_role": g.get("completed_by_role"),
            "outcome_class": g.get("outcome_class"),
            "artifacts": artifacts,
            "total_artifacts": total,
            "credit_pending": pending_reason is not None,
            "credit_pending_reason": pending_reason,
        })

    # Every detector below runs over the SETTLED series (): a
    # credit-pending goal's 0 means "not arrived yet", not "learned nothing".
    # The filter keeps completion order, so the window is still the most recent
    # settled goals; inflection `index` values are positions in this series.
    settled = [ga for ga in goal_artifacts if not ga["credit_pending"]]
    pending = [ga for ga in goal_artifacts if ga["credit_pending"]]

    # Compute metrics
    velocity_window = config.get("velocity_window", 5)
    # None, not 0.0, when nothing is settled: a 0.0 here is what precheck's
    # zero_learning_velocity cycle detector fires on.
    current_velocity = (compute_learning_velocity(settled, velocity_window)
                        if settled else None)
    inflection_points = detect_inflection_points(settled)
    # Record-level exemption (): maintenance-scope queues (recurring
    # upkeep aspirations) legitimately run at ~0 learning velocity — that is
    # their normal operating point, not a stalled learning direction. An
    # aspiration carrying plateau_exempt: true suppresses BOTH flags so evolve
    # Step 1.5 stops re-making the same skip-judgment every cadence pass.
    # Velocity is still computed and reported (informative); only the flags
    # are suppressed. Set via: aspirations-update.sh <asp-id> plateau_exempt true
    # Strict-boolean contract (fresh-eyes finding 2026-07-16): only a real JSON
    # boolean true exempts. A truthy STRING ("False", "no", string-typed "true")
    # keeps detection ON — fail-safe: malformed values stay visible via the
    # flag rather than silently suppressing detection.
    plateau_exempt = asp.get("plateau_exempt") is True
    is_plateau = (not plateau_exempt) and detect_plateau(settled, config)
    is_diminishing = (not plateau_exempt) and detect_diminishing_returns(settled, config)

    # Determine primary category (most common across goals)
    cat_counts = {}
    for ga in goal_artifacts:
        c = ga.get("category", "")
        if c:
            cat_counts[c] = cat_counts.get(c, 0) + 1
    primary_category = max(cat_counts, key=cat_counts.get) if cat_counts else ""

    # Goals since last inflection -- SETTLED goals only, so credit-pending closes
    # cannot arm evolve Step 1.5's prolonged (pivot) branch.
    if inflection_points:
        last_inflection_idx = inflection_points[-1]["index"]
        goals_since_inflection = len(settled) - last_inflection_idx - 1
    else:
        goals_since_inflection = len(settled)

    # Every stratum's size (guard-7449). The three role strata partition the
    # completed population; `routine` cuts across the settled ones.
    window_goals = settled[-velocity_window:]

    def _strata(series):
        worker = sum(1 for ga in series
                     if str(ga["completed_by_role"] or "").strip().lower() == "worker")
        return {"worker": worker, "unstamped": len(series) - worker,
                "routine": sum(1 for ga in series if ga["outcome_class"] == "routine")}

    settled_strata = _strata(settled)
    credit_strata = {
        "completed": len(goal_artifacts),
        "credit_pending": len(pending),
        "settled_worker": settled_strata["worker"],
        "settled_unstamped": settled_strata["unstamped"],
        "settled_routine": settled_strata["routine"],
        "window": dict(size=len(window_goals), **_strata(window_goals)),
    }

    # Build summary
    total_artifacts = sum(ga["total_artifacts"] for ga in goal_artifacts)
    velocity_text = (f"{current_velocity:.2f}/goal" if current_velocity is not None
                     else "n/a (no settled goals)")
    summary = (
        f"{len(completed)} goals completed ({len(pending)} credit-pending, "
        f"{len(settled)} settled), {total_artifacts} learning artifacts produced, "
        f"velocity={velocity_text} over last {len(window_goals)} settled"
    )

    return {
        "aspiration_id": asp_id,
        "title": asp.get("title", ""),
        "status": asp.get("status", ""),
        "primary_category": primary_category,
        "completed_goals_count": len(completed),
        "total_goals_count": len(asp.get("goals", [])),
        "summary": summary,
        "goals": goal_artifacts,
        "inflection_points": inflection_points,
        "last_inflection_point": inflection_points[-1] if inflection_points else None,
        "goals_since_inflection": goals_since_inflection,
        "current_velocity": current_velocity,
        "velocity_window": velocity_window,
        "plateau_detected": is_plateau,
        "diminishing_returns": is_diminishing,
        "plateau_exempt": plateau_exempt,
        "credit_pending_count": len(pending),
        "credit_pending_goal_ids": [ga["goal_id"] for ga in pending],
        "capture_slots_readable": pending_capture_ids is not None,
        "credit_strata": credit_strata,
        "config": config,
    }

def main():
    if len(sys.argv) < 2:
        print("Usage: aspiration-trajectory.py <asp-id> [asp-id ...]", file=sys.stderr)
        sys.exit(1)

    asp_ids = sys.argv[1:]

    if len(asp_ids) == 1:
        # Single ID — backward-compatible flat JSON object
        result = build_trajectory(asp_ids[0])
        print(json.dumps(result, indent=2, default=str))
    else:
        # Multiple IDs — load shared data once, output keyed object
        shared = load_shared_data()
        results = {}
        for asp_id in asp_ids:
            results[asp_id] = build_trajectory(asp_id, shared=shared)
        print(json.dumps(results, indent=2, default=str))

if __name__ == "__main__":
    main()
