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
    "win32",            # a Windows runner (g-115-8826) -- platform-probed, never hand-declared
})

# Tokens that ARE in KNOWN_CAPABILITIES but that NO probe ever asserts, so a box
# provides them ONLY by hand-declaring `runner_capabilities.provides`. Validating
# a token against KNOWN_CAPABILITIES is therefore NECESSARY AND NOT SUFFICIENT
# (zeta, 2026-09-06, on this goal): a known-but-never-provided token is filtered
# out on EVERY runner exactly like an unknown one, and it passes any membership
# check. Kept as a separate set rather than dropped from KNOWN_CAPABILITIES
# because the token is legitimate -- it routes correctly the moment one box
# declares it -- so refusing it at write time would be wrong. Naming it lets the
# block_detail below tell a reader WHICH of the two invisibility modes they are
# looking at. guard-2937: two individually-correct mechanisms (a closed vocab and
# a deliberately-unprobed token) compose into a silent fleet-wide mute.
NEVER_AUTO_PROVIDED = frozenset({
    "studio-session",
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
        # AGENT_WRITE_PATH may name SEVERAL roots separated by ';' (g-321-05
        # multi-root, 2026-06-07); local-paths.conf documents it as
        # ';'-separated and REQUIRED-quoted. This probe was written for the
        # single-root form and never updated, so Path() was called on the WHOLE
        # separated string, is_dir() was False, and 'product-runtime' was never
        # added on exactly the boxes that DO have the product repo. The failure
        # was silent AND INVERTED: the capability filter hid product goals from
        # the runners best able to run them, presenting as a mysterious
        # not_my_lane rather than a probe bug (g-115-3078; observed on alpha's
        # multi-root host, which reported product-runtime ABSENT while holding
        # two product roots).
        # Split semantics deliberately mirror _path_roots.compute_allowed_roots
        # (split ';', strip, skip empties) so the two readers of this variable
        # cannot drift; ANY root being a dir is sufficient, since the capability
        # asserts the product runtime is reachable, not that every root exists.
        if awp and any(
            part.strip() and Path(part.strip()).is_dir()
            for part in awp.split(";")
        ):
            caps.add("product-runtime")
    except Exception:
        pass
    # win32: this runner is a Windows box (g-115-8826). PLATFORM-PROBED, never
    # hand-declared per box -- that is the whole point. The originating incident
    # was a goal declaring `win32` while the token was not in KNOWN_CAPABILITIES
    # and no probe asserted it, so it was filtered out on every runner in the
    # fleet for 5 days while an idle Windows box with the target repo sat next to
    # it. os.name is the cheap, side-effect-free discriminator ("nt" on CPython
    # for Windows including MSYS/Git-Bash-launched interpreters); sys.platform is
    # checked too so a non-CPython runtime reporting "win32" is still caught.
    try:
        import sys as _sys
        if os.name == "nt" or str(_sys.platform).startswith("win"):
            caps.add("win32")
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


# --- Per-box declaration surface (local-paths.conf) --------------------------
# `core/config/aspirations.yaml` is git-shared and `meta/config-overrides.yaml`
# is S3-shared -- BOTH apply fleet-wide, so neither can express "THIS box has a
# live Studio session" without wrongly claiming it for every other runner.
# `local-paths.conf` is the only genuinely per-box surface: gitignored AND in
# `owncloud_sync._EXCLUDE_NAMES`, so it never reaches git or S3. These keys are
# the conf's capability-declaration contract.
BOX_CONF_PROVIDES = "RUNNER_CAPABILITIES_PROVIDES"   # comma-separated tokens
BOX_CONF_LACKS = "RUNNER_CAPABILITIES_LACKS"         # comma-separated tokens
BOX_CONF_PROBE = "RUNNER_CAPABILITIES_PROBE"         # "false"/"0"/"no" => no probes


def _split_tokens(raw):
    """Comma-separated conf value -> clean token list (empty on None/blank)."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        parts = list(raw)
    else:
        parts = str(raw).split(",")
    return [t for t in (str(p).strip() for p in parts) if t]


def box_config_from_conf(conf):
    """Translate a parsed `local-paths.conf` dict into a runner_capabilities
    config block (same provides/lacks/probe shape as the aspirations.yaml
    block, so both feed `derive_runner_capabilities` unchanged).

    Returns {} when the conf declares none of the keys -- an absent declaration
    must be indistinguishable from "no per-box config", so the fleet default
    and probe behaviour are untouched on every box that never opts in.
    """
    if not isinstance(conf, dict):
        return {}
    out = {}
    provides = _split_tokens(conf.get(BOX_CONF_PROVIDES))
    lacks = _split_tokens(conf.get(BOX_CONF_LACKS))
    if provides:
        out["provides"] = provides
    if lacks:
        out["lacks"] = lacks
    if BOX_CONF_PROBE in conf:
        out["probe"] = str(conf.get(BOX_CONF_PROBE)).strip().lower() not in (
            "false", "0", "no", "off")
    return out


def merge_capability_config(fleet_cfg, box_cfg):
    """Merge the git-shared fleet block with the per-box declaration.

    Precedence follows the module's existing "lacks is the authoritative last
    word" rule, extended across the two layers:
      * provides -- UNION. The box adds what it alone can assert (studio-session);
        the fleet block keeps whatever it declared for everyone.
      * lacks    -- UNION. Removal is authoritative from EITHER layer, and is
        applied after provides by derive_runner_capabilities, so a token in
        either `lacks` wins over a `provides` in either layer. A box can never
        be wrongly forced to claim a capability it says it does not have.
      * probe    -- the BOX wins when it declares one. Whether probes are
        meaningful is a property of the physical box, not of the fleet.
    """
    fleet_cfg = fleet_cfg if isinstance(fleet_cfg, dict) else {}
    box_cfg = box_cfg if isinstance(box_cfg, dict) else {}
    merged = {}
    for key in ("provides", "lacks"):
        seen, union = set(), []
        for layer in (fleet_cfg, box_cfg):
            for tok in _split_tokens(layer.get(key)):
                if tok not in seen:
                    seen.add(tok)
                    union.append(tok)
        if union:
            merged[key] = union
    if "probe" in box_cfg:
        merged["probe"] = box_cfg["probe"]
    elif "probe" in fleet_cfg:
        merged["probe"] = fleet_cfg["probe"]
    return merged


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


def unknown_capability_tokens(tokens):
    """The subset of `tokens` that is OUTSIDE KNOWN_CAPABILITIES.

    A token here can never be satisfied by ANY runner: `goal_is_locally_executable`
    is a plain subset test against what a box provides, and nothing anywhere adds
    an off-contract token to a runner's set except a hand-written
    `runner_capabilities.provides` in a per-box, sync-excluded file. So a goal
    carrying one is filtered out of the ranked pool on every box in the fleet,
    permanently, with no error anywhere -- the failure this module's own
    docstring calls asymmetrically dangerous (rb-1028: a wrongly-hidden goal is
    invisible, un-actioned and un-learned-from).

    Accepts a goal dict or any token iterable, so the write-time validator and
    the selector's block_detail can share one predicate rather than drifting.
    """
    if isinstance(tokens, dict):
        tokens = goal_required_capabilities(tokens)
    elif isinstance(tokens, str):
        tokens = [tokens]
    try:
        toks = {str(t).strip() for t in (tokens or []) if str(t).strip()}
    except TypeError:
        return set()
    return toks - set(KNOWN_CAPABILITIES)


def capability_block_detail(missing, runner_caps):
    """Human-readable not_my_lane detail that says WHICH kind of block this is.

    The original message -- "Requires capability not on this runner: X (runner
    has: ...)" -- reads as "wrong box, some other runner will take it" in all
    three cases below, and only one of them is true. That wording is what let a
    revenue-bearing goal (27.6% of active customers unbillable) sit unread behind
    a well-formed-looking block: a wrong-locus POSITIVE reads as corroboration
    and never prompts a second look (guard-7132).

    Three cases, in decreasing severity:
      * UNKNOWN token  -> no runner can EVER provide it. Terminal without an edit.
      * NEVER_AUTO_PROVIDED -> only a box that hand-declares it provides it, so
        it is invisible fleet-wide until one does.
      * otherwise -> an ordinary per-box gap; another runner genuinely may take it.
    """
    missing = sorted({str(m).strip() for m in (missing or []) if str(m).strip()})
    have = ",".join(sorted(runner_caps or [])) or "none"
    if not missing:
        return "Requires capability not on this runner: (none) (runner has: {r})".format(r=have)
    unknown = sorted(unknown_capability_tokens(missing))
    unprovided = sorted(set(missing) & set(NEVER_AUTO_PROVIDED))
    base = "Requires capability not on this runner: {m} (runner has: {r})".format(
        m=",".join(missing), r=have)
    if unknown:
        return (base + " -- NO RUNNER CAN EVER PROVIDE {u}: not in KNOWN_CAPABILITIES"
                " ({k}). This goal is hidden fleet-wide, not routed elsewhere;"
                " fix the token or register it.").format(
                    u=",".join(unknown), k=",".join(sorted(KNOWN_CAPABILITIES)))
    if unprovided:
        return (base + " -- {p} is never auto-probed, so only a box that declares"
                " runner_capabilities.provides supplies it; if none does, this goal"
                " is hidden fleet-wide.").format(p=",".join(unprovided))
    return base


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
