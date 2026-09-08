#!/usr/bin/env bash
# inbound-drain-default.sh — the DEFAULT `inbound-drain` hook slot.
#
# Pattern B executable slot (core/config/conventions/domain-hooks.md). A world
# that fills $WORLD_PATH/scripts/inbound-drain.sh OVERRIDES this file; the
# dispatcher (inbound-drain-run.py) tries the world slot first and falls back
# here. This exists because `world/` is excluded from the seed and is not in git,
# so a world-only slot never reaches a seeded environment host — measured 0 of 45
# live workspaces carried it ().
#
# CONTRACT: args pass through to the engine; one JSON object on stdout; exit 0
# always (an always-run precheck lane must never block the loop, guard-614). Run
# via `bash` — a sync pull does not preserve +x.
#
# THE ENVIRONMENT GUARD IS THE LOAD-BEARING PART, and it is why this is not a
# one-line exec. The drain must run where the spool and the goal queue belong to
# the SAME environment. An operator box may mount every environment's spool at
# once, so draining there would file many members' instructions into ONE queue —
# the drain working perfectly and doing something badly wrong. So: drain only the
# environment this box declares itself to be. No key, no drain.
#
# ABSENCE IS NOT ZERO. A box with no key or no root emits not_a_vessel rather
# than a zero count: most boxes are not environment hosts, and a silent 0 there
# is indistinguishable from a host whose spool is genuinely empty.
#
# THIS GUARD IS SOURCE-SIDE ONLY and is deliberately not the last line of
# defence. It decides WHICH SPOOL IS READ; it cannot constrain where the engine
# FILES, because filing goes through the local runtime which takes no destination
# argument. A box with both variables set would pass this guard. The engine's
# own `_destination_fence` is what refuses that case, and it fails closed.
set -uo pipefail
_SELF="$(cd "$(dirname "$0")" && pwd)"
_ENGINE="$_SELF/inbound_drain.py"

_emit_skip() { printf '{"not_a_vessel":true,"reason":"%s"}\n' "$1"; exit 0; }

if [ ! -f "$_ENGINE" ]; then _emit_skip "drain engine absent"; fi

# Environment key. `<envId>+<instanceToken>` is a common systemd instance shape;
# the spool is per-ENVIRONMENT, so the left half is the answer.
_KEY="${INBOUND_ENVIRONMENT_KEY:-}"
if [ -z "$_KEY" ]; then _emit_skip "no INBOUND_ENVIRONMENT_KEY — this box is not an environment host"; fi
_ENVKEY="${_KEY%%+*}"
if [ -z "$_ENVKEY" ]; then _emit_skip "malformed environment key '$_KEY'"; fi

# Root: generic env var only. NO built-in candidate paths — guessing a mount
# point is how a core file acquires a domain name, and a wrong guess reports
# "no environment", which reads as an empty spool rather than as missing config.
_ROOT="${INBOUND_SPOOL_ROOT:-}"
if [ -z "$_ROOT" ]; then _emit_skip "INBOUND_SPOOL_ROOT unset — no spool configured on this box"; fi
if [ ! -d "$_ROOT" ]; then _emit_skip "spool root does not exist: $_ROOT"; fi
if [ ! -d "$_ROOT/$_ENVKEY" ]; then _emit_skip "no spool dir for environment '$_ENVKEY' under $_ROOT"; fi

# ── The directive aspiration () ─────────────────────────────────
# Resolved HERE, after the guards above, so only a box that declares itself
# an environment host can ever mint: the env var, then .env.local, then the
# world queue by exact title, then a one-time mint through this vessel's own
# daemon (inbound_directive_asp.py). The provisioner cannot do it — no daemon
# is bound to the vessel at provision time (provision-env.sh,
# wire_assigned_lane) — so it writes INBOUND_DIRECTIVE_ASP_ID only when the
# aspiration already exists, and the first loop pass on a fresh vessel is
# where it comes to exist. Passed to the engine EXPLICITLY: the daemon loaded
# .env.local at ITS start, so a write-back made this pass is not in this
# process's environment yet. An empty id means "still unwired": the engine
# leaves directives UNCLAIMED and reports unconfigured>0 — never a guessed
# aspiration. A caller's own --aspiration still wins (argparse keeps the last).
_ROOTDIR="$(cd "$_SELF/../.." && pwd)"
_ENVLOCAL="${INBOUND_ENV_LOCAL:-$_ROOTDIR/.env.local}"
_ASP="$(python3 "$_SELF/inbound_directive_asp.py" --env-local "$_ENVLOCAL" || true)"
if [ -n "$_ASP" ]; then set -- --aspiration "$_ASP" "$@"; fi

python3 "$_ENGINE" --root "$_ROOT" --environment-key "$_ENVKEY" "$@" || true
exit 0
