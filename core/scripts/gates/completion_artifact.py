"""Goal-completion artifact gate logic.

Refuses goal closure when the goal description references concrete file
artifacts that don't exist on disk. Catches the "goal marked complete
without artifact" class of bug — canonical incident: g-115-724
(2026-05-14), where `core/scripts/post-state-update-metric-gate.sh` was
marked complete but never committed; the missing script produced 17
stderr failures from iteration-close.sh's call before discovery.

Pattern parallel to `gates.uncommitted_work`: invoked from
aspirations.py cmd_update_goal when field=='status' and value=='completed',
fails the close with an override path for legitimate false-positives.

Algorithm:
    1. Action-prefix filter — only fires when title starts with one of
       (Apply|Create|Implement|Add|Build|Wire|Land). Investigate/Idea/
       Maintain goals often reference paths as context not as artifacts.
    2. Extract path patterns from title + description matching the
       repo's artifact-producing path roots.
    3. Virtual-prefix resolve each path:
         world/X  → world_dir / X
         meta/X   → meta_dir / X
         else     → project_root / X
    4. Check existence. Collect missing.
    5. If override given AND missing non-empty: log audit ledger.

Public API:
    evaluate(goal_id, goal_title, goal_description, override,
             project_root, world_dir, meta_dir, agent_name) -> dict

Output dict shape:
    {
      "would_block": bool,
      "missing_artifacts": [str, ...],   # virtual paths
      "near_misses": {str: str},          # virtual_path → existing_alt
      "checked_paths": int,
      "goal_id": str,
      "override_applied": str | None,
      "skipped_reason": str | None,       # set when not an action goal
    }

Side effect:
    On override + missing_artifacts non-empty, appends one record to
    `<world_dir>/missing-artifact-overrides.jsonl` via
    `_fileops.locked_append_jsonl`. Fail-open on ledger errors.

Daemon safety:
    - Reads no environment variables. All inputs passed in.
    - No subprocess calls. Pure file-existence checks.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple, Dict

from _fileops import locked_append_jsonl  # type: ignore


# Action prefixes that imply concrete artifact creation. Goals starting
# with these verbs are expected to produce files; their close-time
# verification can include an artifact-existence check.
#
# Excluded by design: Investigate, Idea, Maintain, Reflect, Batch,
# Recurring, Unblock. Those goal shapes reference paths as context, not
# as completion artifacts.
ACTION_PREFIX_RE = re.compile(
    r"^(Apply|Create|Implement|Add|Build|Wire|Land)[\s:]",
    re.IGNORECASE,
)

# Artifact path roots. A path mentioned in title/description that begins
# with one of these roots and ends in a known artifact extension is
# treated as a completion artifact.
#
# Restricted on purpose: only first-class artifact-producing roots. A
# generic regex would catch too many false positives (e.g., references
# to documentation file paths in prose).
ARTIFACT_PATH_RE = re.compile(
    r"(?:core/scripts|core/config|mind_api/src|"
    r"world/conventions|world/knowledge|"
    r"meta|\.claude/skills|\.claude/rules)"
    # Extension alternation MUST be longest-first (jsonl before json) AND
    # boundary-anchored. Python `re` alternation is leftmost-match, not
    # longest-match: a bare `(...|json|jsonl)` matched `.json` inside `.jsonl`
    # and truncated the path, so a real `meta/foo.jsonl` artifact resolved to
    # a non-existent `meta/foo.json` and produced a false-positive
    # missing-artifact block (5 / rb-1838 — the gate's own near_misses
    # then mapped `.json` back to the existing `.jsonl`, surfacing the
    # contradiction). The trailing `(?![A-Za-z0-9])` makes the full-extension
    # match order-independent so a future extension addition cannot re-introduce
    # the truncation class.
    r"/[\w/-]+\.(?:sh|py|md|yaml|yml|jsonl|json)(?![A-Za-z0-9])"
)


def _resolve(virtual_path: str, *, project_root: Path,
             world_dir: Optional[Path], meta_dir: Optional[Path]) -> Optional[Path]:
    """Map a virtual path to a Windows-native absolute Path, or None when
    the required virtual-prefix resolver is unavailable.

    world/X → world_dir / X (None if world_dir is None)
    meta/X  → meta_dir / X  (None if meta_dir is None)
    else    → project_root / X

    Returning None signals "skip this path — fail-open." A previous
    version fell back to project_root for None resolvers; that produced
    false-positive blocks when MIND_AGENT was unset because world/
    paths resolved to a non-existent PROJECT_ROOT/world/ tree
    (real WORLD_DIR lives at an external user-configured path).
    """
    if virtual_path.startswith("world/"):
        if world_dir is None:
            return None
        return world_dir / virtual_path[6:].replace("/", os.sep)
    if virtual_path.startswith("meta/"):
        if meta_dir is None:
            return None
        return meta_dir / virtual_path[5:].replace("/", os.sep)
    return project_root / virtual_path.replace("/", os.sep)


def _near_miss(missing_full: Path) -> Optional[str]:
    """Detect common extension typos (e.g., .json claimed but .jsonl exists).

    Returns the basename of the alternate file when found, else None.
    Helps the user see "did you mean .jsonl?" without auto-accepting
    the wrong path.
    """
    stem = missing_full.stem  # foo for foo.json
    parent = missing_full.parent
    if not parent.is_dir():
        return None
    alt_exts = {
        ".json": [".jsonl"],
        ".jsonl": [".json"],
        ".yaml": [".yml"],
        ".yml": [".yaml"],
        ".sh": [".py"],
        ".py": [".sh"],
    }
    for alt in alt_exts.get(missing_full.suffix, []):
        candidate = parent / (stem + alt)
        if candidate.exists():
            return candidate.name
    return None


def find_missing_artifacts(
    goal_title: str,
    goal_description: str,
    *,
    project_root: Path,
    world_dir: Optional[Path],
    meta_dir: Optional[Path],
) -> Tuple[List[str], Dict[str, str], int]:
    """Scan title + description for artifact paths and check existence.

    Returns (missing_virtual_paths, near_misses, total_checked).
    """
    text = (goal_title or "") + " " + (goal_description or "")
    paths = sorted(set(ARTIFACT_PATH_RE.findall(text)))
    missing: List[str] = []
    near_misses: Dict[str, str] = {}
    checked = 0
    for vp in paths:
        full = _resolve(vp, project_root=project_root,
                        world_dir=world_dir, meta_dir=meta_dir)
        if full is None:
            # Required virtual-prefix resolver unavailable — skip (fail-open).
            continue
        checked += 1
        if not full.exists():
            missing.append(vp)
            nm = _near_miss(full)
            if nm:
                near_misses[vp] = nm
    return missing, near_misses, checked


def _log_override(world_dir: Path, agent_name: str, goal_id: str,
                  justification: str, missing: List[str],
                  near_misses: Dict[str, str]) -> None:
    """Append override to <world_dir>/missing-artifact-overrides.jsonl.

    Fail-open: errors print to stderr; the gate never blocks on
    audit-log infrastructure problems.
    """
    ledger = world_dir / "missing-artifact-overrides.jsonl"
    record = {
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "agent": agent_name or "unknown",
        "goal_id": goal_id,
        "justification": justification,
        "missing_artifacts": missing,
        "near_misses": near_misses,
    }
    try:
        locked_append_jsonl(str(ledger), record)
    except Exception as exc:
        print(f"[completion-artifact-gate] override-log write failed: {exc}",
              file=sys.stderr)


def evaluate(*, goal_id: str, goal_title: str, goal_description: str,
             override: Optional[str], project_root: Path,
             world_dir: Optional[Path], meta_dir: Optional[Path],
             agent_name: str = "") -> dict:
    """Run the gate. Pure decision; explicit side effect on override.

    Args:
        goal_id: ID of the goal being closed (audit-log correlation).
        goal_title: Title from the goal record (used for action-prefix
            filter and regex scan).
        goal_description: Description from the goal record (also scanned).
        override: justification string, or None for no override. Empty/
            whitespace-only is coerced to None with a stderr warn —
            mirrors uncommitted-work-gate behavior.
        project_root: Repo root for non-virtual-prefix paths.
        world_dir: WORLD_DIR for `world/X` resolution and the override
            ledger destination.
        meta_dir: META_DIR for `meta/X` resolution.
        agent_name: MIND_AGENT value for the audit-log "agent" field.

    Returns dict matching the documented output shape.
    """
    # Empty override → no override + warn (parallel to uncommitted-work-gate).
    effective_override = override
    if effective_override is not None and effective_override.strip() == "":
        print("[completion-artifact-gate] WARN: --override is empty/"
              "whitespace-only — treating as no override.",
              file=sys.stderr)
        effective_override = None

    # Action-prefix gate. Investigate/Idea/Maintain goals reference paths
    # as context, not as completion artifacts.
    if not ACTION_PREFIX_RE.match(goal_title or ""):
        return {
            "would_block": False,
            "missing_artifacts": [],
            "near_misses": {},
            "checked_paths": 0,
            "goal_id": goal_id,
            "override_applied": effective_override,
            "skipped_reason": "non_action_goal",
        }

    missing, near_misses, checked = find_missing_artifacts(
        goal_title, goal_description,
        project_root=project_root, world_dir=world_dir, meta_dir=meta_dir,
    )

    would_block = bool(missing) and effective_override is None

    if effective_override is not None and missing:
        if world_dir is None:
            print("[completion-artifact-gate] WARN: no WORLD_DIR — "
                  "skipping override log", file=sys.stderr)
        else:
            _log_override(world_dir, agent_name, goal_id, effective_override,
                          missing, near_misses)

    return {
        "would_block": would_block,
        "missing_artifacts": missing,
        "near_misses": near_misses,
        "checked_paths": checked,
        "goal_id": goal_id,
        "override_applied": effective_override,
        "skipped_reason": None,
    }
