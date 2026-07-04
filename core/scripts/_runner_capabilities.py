#!/usr/bin/env python3
# domain-leak-exempt: capability tokens (aws/gpu/product-runtime) are functional
# identifiers of machine resources, not domain examples.
"""Per-runner capability model for goal-selector (g-115-1740).

A capability-constrained runner -- a box lacking ML deps / product runtime / GPU /
aws / a live studio session that OTHER agents in the fleet DO have -- must not keep
re-selecting goals it cannot execute. Doing so burns tokens with zero output and
never reaches quiescence: goal-selector.cmd_select returns scored candidates, so
aspirations-select never routes to all_blocked, blocked_sleep_until is never set,
and the RUNNING stop-hook forces immediate Skill(aspirations) re-entry every turn
(the ~15k-token/iter idle burn zeta observed on cc-02 across the ~100-agent
fresh-box launch -- Class C fresh-box gap, g-115-1740).

ROOT: "I cannot execute this" (a per-RUNNER capability gap) is distinct from
"this is globally blocked" (a per-GOAL blocker_ref). Only the latter reaches
quiescence today. This module supplies the missing per-runner axis so cmd_select
can (1) drop locally-unexecutable-but-globally-open goals from ranking and
(2) emit a `not_my_lane` all_blocked variant when EVERY candidate is
capability-blocked, so the box self-throttles into a sleep instead of hot-looping.

Design constraint (rb-1028 -- filtering candidates in a learning system is
asymmetrically dangerous: a wrongly-hidden goal is invisible, un-actioned, and
un-learned-from). The filter is therefore CONSERVATIVE:
  * A goal with NO `requires_capability` is ALWAYS executable -- never hidden.
    Untagged goals (the default) are unaffected; this is a strict opt-in gate.
  * Only an EXPLICIT `requires_capability` the runner cannot satisfy gates a goal.
  * Runner capabilities are config-overridable (`provides` / `lacks`) so a box can
    assert ground truth and never be wrongly throttled by a mis-probe.
  * Every probe fails SAFE (absent-on-error) -- but the config override is the
    authoritative escape hatch when a probe is wrong in either direction.
"""
import os
import shutil
from pathlib import Path

# Canonical capability tokens. A goal declares `requires_capability` as a subset;
# a runner PROVIDES a subset. Keep this set stable -- it is the cross-file contract
# between goal `requires_capability` values and the runner_capabilities config.
KNOWN_CAPABILITIES = frozenset({
    "ml-deps",          # numpy / torch importable (local embedding / ML model work)
    "aws",              # aws CLI on PATH (email / inbox / lambda / S3 ops)
    "product-runtime",  # product repo present on this box (npc / ohs / state-replay code+data)
    "gpu",              # a usable GPU (VRAM reclaim, local inference)
    "git-push",         # this runner can push commits (not a read-only clone)
    "studio-session",   # a live product Studio session -- see note in _probe_default_capabilities
})


def _probe_default_capabilities():
    """Cheap, side-effect-free probes of what THIS box can do.

    Each probe fails SAFE: a probe error or a negative result means "cannot
    confirm" -> capability absent (conservative -- better to route not-my-lane
    than to wrongly claim a capability and hot-loop failing to execute). The
    `runner_capabilities.provides` config override is how a box asserts a
    capability a probe cannot see.
    """
    caps = set()
    # aws CLI on PATH (credentials are a separate concern; PATH presence is the
    # cheap proxy -- a box without the CLI definitively cannot run aws-gated work).
    try:
        if shutil.which("aws"):
            caps.add("aws")
    except Exception:
        pass
    # ML deps importable (numpy is the base dependency of the embedding stack).
    try:
        import importlib.util
        if importlib.util.find_spec("numpy") is not None:
            caps.add("ml-deps")
    except Exception:
        pass
    # GPU present (nvidia-smi on PATH is a cheap, side-effect-free proxy).
    try:
        if shutil.which("nvidia-smi"):
            caps.add("gpu")
    except Exception:
        pass
    # Product runtime: the sibling product repo (AGENT_WRITE_PATH) present + a dir.
    try:
        awp = os.environ.get("AGENT_WRITE_PATH")
        if awp and Path(awp).is_dir():
            caps.add("product-runtime")
    except Exception:
        pass
    # git-push: default-PRESENT. A wrongly-claimed git-push only affects goals
    # that EXPLICITLY require it (rare), and a read-only clone (e.g. zeta@cc-02
    # Option-2) opts out via `runner_capabilities.lacks: [git-push]`. Probing a
    # real push (dry-run) would be a network side effect on the selection hot
    # path -- deliberately avoided; the config override is the authoritative
    # signal for read-only boxes.
    caps.add("git-push")
    # NOTE: "studio-session" is intentionally NOT probed. A live Studio session is
    # a transient RUNTIME resource on one specific host, never a static box
    # capability. A runner NEVER auto-provides it -> goals requiring it route
    # not-my-lane on every box unless a studio host asserts it via
    # `runner_capabilities.provides: [studio-session]`.
    return caps


def derive_runner_capabilities(config=None, probe_fn=None):
    """Return the set of capability tokens THIS runner provides.

    config: the `runner_capabilities` block from aspirations.yaml (or None):
      - config["provides"]: capabilities to FORCE-ADD  (box asserts ground truth)
      - config["lacks"]:    capabilities to FORCE-REMOVE (overrides a probe)
      - config["probe"]:    bool, default True. False => config-only (no probes),
                            for a fully-declared box or a hermetic test.
    provides/lacks are applied AFTER probing; `lacks` wins over both probe and
    `provides` (removal is the last, authoritative word for a read-only box).
    probe_fn: injectable for tests; defaults to _probe_default_capabilities.
    """
    config = config or {}
    if not isinstance(config, dict):
        config = {}
    probe_fn = probe_fn or _probe_default_capabilities
    caps = set()
    if config.get("probe", True):
        try:
            caps |= set(probe_fn())
        except Exception:
            pass
    for c in (config.get("provides") or []):
        caps.add(str(c).strip())
    for c in (config.get("lacks") or []):
        caps.discard(str(c).strip())
    caps.discard("")
    return caps


def goal_required_capabilities(goal):
    """Capabilities a goal REQUIRES. Conservative: EXPLICIT `requires_capability`
    only (a list, or a single string). A goal with none returns the empty set and
    is therefore NEVER filtered (universally executable)."""
    if not isinstance(goal, dict):
        return set()
    req = goal.get("requires_capability")
    if req is None:
        return set()
    if isinstance(req, str):
        req = [req]
    if not isinstance(req, (list, tuple, set)):
        return set()
    return {str(c).strip() for c in req if str(c).strip()}


def goal_is_locally_executable(goal, runner_caps):
    """True if this runner can execute the goal: the goal's required capabilities
    are a subset of the runner's. Empty requirement -> always True (never hidden)."""
    req = goal_required_capabilities(goal)
    if not req:
        return True
    return req.issubset(set(runner_caps))


def apply_capability_filter(candidates, runner_caps, goal_key="goal"):
    """Drop candidates the runner cannot execute, with a NO-REGRESSION guard.

    candidates: list of goal-selector candidate dicts, each carrying its goal
      under `goal_key` (default "goal"). A candidate whose goal has no
      requires_capability is ALWAYS kept (untagged = universally executable). A
      candidate requiring a capability the runner lacks is dropped -- UNLESS
      dropping would EMPTY the set (a fully capability-constrained box), in which
      case ALL candidates are kept so the caller's existing all-unexecutable path
      is unchanged (the not_my_lane -> quiescence routing for that case is a
      separate, tracked change; this filter never makes it worse than today).

    Returns (filtered_candidates, dropped_count). dropped_count == 0 means either
    nothing was filterable or the no-regression guard kept everything.
    """
    def _goal_of(c):
        return c.get(goal_key, {}) if isinstance(c, dict) else {}
    executable = [c for c in candidates
                  if goal_is_locally_executable(_goal_of(c), runner_caps)]
    if executable and len(executable) < len(candidates):
        return executable, len(candidates) - len(executable)
    return candidates, 0
