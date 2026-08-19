#!/usr/bin/env python3
# domain-leak-exempt: the remote collector reads literal fleet paths (.env.local,
# mind_api/state/daemon.pid, local-paths.conf) and the deploy-key check names the
# concrete repo — these strings are functional, not illustrative.
"""fleet-config-parity — per-node CONFIGURATION drift detection for the agent fleet.

THE GAP THIS CLOSES (g-115-3071). The fleet has strong LIVENESS observability
(liveness-check.sh, systemd watchdog, signal-trust ranking) and had effectively
ZERO CONFIGURATION observability. The last six fleet incidents were ALL config
drift and NONE were liveness failures; every one was found by a human or an agent
tripping over it rather than by a probe. The watchdog reported every node healthy
the whole time and was CORRECT — the nodes were alive, they were just wrong.

Compares every node in core/config/fleet-manifest.yaml against the manifest and
emits per-node PASS or DRIFT naming the specific key.

WHAT IT CHECKS
  (a) .env.local KEY SET      — names only; required keys present, unknown keys surfaced
  (b) toolchain               — node major, claude version, kernel
  (c) STORAGE_BACKEND et al.  — AS RESOLVED BY THE RUNNING DAEMON (/proc/<pid>/environ),
                                NOT as read from the file. cc-02 would have PASSED a
                                naive file check for four days while its live daemon
                                disagreed, so the FILE IS NOT THE AUTHORITY.
 (c2) LANE PARITY             — the same three keys as resolved by the BARE-SUBPROCESS
                                (CLI) lane, sampled with them unset. The daemon environ
                                is not the authority either: the registry derivation
                                lived only in the daemon's main(), so on cc-02 for 11
                                days the daemon resolved own-cloud while every bare CLI
                                subprocess resolved 'local'. DRIFT is (a) a lane
                                disagrees with the manifest, OR (b) THE LANES DISAGREE
                                WITH EACH OTHER — (b) being invisible to any single-lane
                                probe, and the shape that actually occurred.
  (h) CREDENTIAL VALUE SHAPE  — (a) reads key NAMES only, so a key that is PRESENT
                                but holds GARBAGE passes every other dimension.
                                2026-07-26: a ~/.bashrc bug exported the 158-char
                                ERROR TEXT of a failed wslvar call as OPENAI_API_KEY
                                — key present, toolchain fine, backend correct, and
                                nothing looked at the value. Sampled from the RUNNING
                                process environments (the (c)/(c2) lanes), never from
                                .env.local: a poisoned shell export is invisible in
                                the file and visible only in a live process. The
                                predicate runs ON THE REMOTE NODE; only a verdict
                                (ok|too_short|charset|absent) and a length cross the
                                wire. `absent` is NOT drift — a key set in-process
                                after exec is absent from /proc/environ for the
                                daemon's whole life (the g-115-3157 mechanism), and
                                presence is (a)'s job. Manifest key
                                `value_shape_expectations`. g-115-3344.
  (d) deploy key read_only    — from the GitHub API, matched by PUBLIC KEY BODY.
                                Never a title heuristic: zeta has both a read-only and
                                a read-write key registered, so only body-matching
                                identifies the one the box actually holds.
  (e) path config shape       — local-paths.conf key names + AGENT_WRITE_PATH root count
  (f) ROSTER PARITY           — the manifest node list vs the LIVE fleet roster.
                                (a)-(e) can only measure nodes the manifest LISTS, and
                                that list is hardcoded — so an agent that joins the
                                fleet is never iterated and the tool prints
                                "5 PASS / 0 DRIFT" while one node was never measured.
                                That is this checker's own defect class turned on
                                itself: an input that does not mean what the reader
                                assumes. A live agent with no manifest node is DRIFT
                                (exit 1); a manifest node absent from the roster is
                                INFO (it IS measured — a retired node still listed).
                                Skipped on --node runs, which measure a subset by
                                design. g-115-3160.
  (g) BOX PARITY              — the manifest HOST list vs the live BOX set, read from
                                body-heartbeat carriers in the authoritative store.
                                (f) closes the agent axis and CANNOT close this one:
                                both its sides are agent-keyed, and agent-set stopped
                                equalling box-set at the Mind/Body split — one agent
                                can occupy two boxes (a second body adds no
                                agent_status row) and an agent-agnostic EXTRA box has
                                no resident agent at all. So a box is structurally
                                invisible to (f) no matter how correctly it runs.
                                Measured 2026-08-07: three live boxes outside the
                                manifest, two of them on a node major the manifest
                                pins against — found by a human, not by this tool.
                                A live box (body-heartbeat < 24h) with no manifest
                                node is DRIFT (exit 1); an older carrier, or a
                                manifest node with no carrier, is INFO. Skipped on
                                --node runs. g-115-5172.

SECRETS CONTRACT (the property that must never regress)
  The remote collector is an ALLOWLIST emitter: it prints only the fields enumerated
  in _COLLECTOR, and the env-var line is passed through `grep -oE '^KEY='` + `tr -d '='`,
  which discards everything after the `=` before it is ever assigned. No secret value
  can reach stdout, the JSON output, the board, or a filed goal, because no code path
  reads one. Exactly three VALUES are read — STORAGE_BACKEND, ENVIRONMENT_ID,
  MACHINE_ID — non-secret configuration identifiers that ARE the audited subject
  (manifest key `value_visible_env_keys`). Public keys are emitted for API matching;
  a public key is public by definition. Verified by --self-test.

  Check (h) READS credential values and does not weaken this: the read happens
  entirely inside the remote node's own interpreter, and the only things printed are
  a verdict word and an integer length. No prefix is emitted even though the goal
  permitted up to 12 characters — nothing needs one (guard-724/1563, mask by
  default). --self-test asserts BOTH halves: that a poisoned value is caught, and
  that it never appears in the output.

Usage:
  bash fleet-config-parity.sh                     # check all nodes, human output
  bash fleet-config-parity.sh --json              # machine-readable
  bash fleet-config-parity.sh --node alpha        # one node
  bash fleet-config-parity.sh --file-investigate  # auto-file an Investigate on DRIFT
  bash fleet-config-parity.sh --self-test         # prove the secrets contract holds

Exit codes:
  0  all reachable nodes PASS
  1  at least one node DRIFT, a ROSTER DRIFT (a live agent with no manifest node —
     an unmeasured node is a coverage gap, and reporting it while exiting 0 would
     leave the vacuous pass intact for callers that read only the status code),
     a BOX DRIFT (the same gap on the box axis — see (g)), or a fleet BLACKOUT
     (see below)
  2  usage / manifest error
  (a MINORITY of unreachable nodes is reported as UNREACHABLE and does NOT set exit 1
   on its own — a blip is the watchdog's job, not this checker's; see --strict to fail
   on any. But when EVERY non-self node is unreachable the sweep measured nothing, so
   reporting success would be a failed measurement dressed as a passing one: that case
   exits 1 and files its own Investigate, separate from drift. Threshold:
   blackout_escalation in core/config/fleet-manifest.yaml. g-115-3162)
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

PROJECT_ROOT = SCRIPT_DIR.parent.parent
MANIFEST = PROJECT_ROOT / "core" / "config" / "fleet-manifest.yaml"

# g-115-4166: never hardcode the escalation aspiration — asp-115 is the UPSTREAM
# deployment's queue and does not exist elsewhere, so a literal files nothing.
try:
    from _paths import AGENT_DIR, CORE_ROOT, WORLD_DIR
    from _escalation_target import resolve as _resolve_asp, source_flag as _asp_source
    ESCALATION_ASP, _ESCALATION_ASP_VIA = _resolve_asp(CORE_ROOT, WORLD_DIR, AGENT_DIR)
    ESCALATION_SOURCE = _asp_source(ESCALATION_ASP, WORLD_DIR, AGENT_DIR)
except Exception:
    ESCALATION_ASP, _ESCALATION_ASP_VIA, ESCALATION_SOURCE = (
        "asp-115", "fallback:import-failed", "world")

SSH_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "ConnectTimeout=8",
    "-o", "LogLevel=ERROR",
]

# ── The remote collector ──────────────────────────────────────────────────────
# Runs on each node over ssh. Emits `key=value` lines — one field per line, all
# fields enumerated here. This is an ALLOWLIST: a field that is not printed below
# cannot appear in any output. The env-key line strips at `=` BEFORE assignment,
# so a secret value is never held in a variable, let alone printed.
_COLLECTOR = r"""
set -u
R=%(root)s
say() { printf '%%s=%%s\n' "$1" "$2"; }

say hostname "$(hostname 2>/dev/null || echo unknown)"
say root_exists "$([ -d "$R" ] && echo yes || echo no)"

# (a) env-var KEY NAMES ONLY. `grep -oE '^KEY='` keeps the name and the '=', tr drops
# the '='. Everything to the right of '=' is discarded by grep before assignment.
if [ -r "$R/.env.local" ]; then
  say env_keys "$(grep -oE '^[A-Za-z_][A-Za-z0-9_]*=' "$R/.env.local" 2>/dev/null | tr -d '=' | sort -u | paste -sd, -)"
  say env_file no_read_error
else
  say env_keys ""
  say env_file unreadable
fi

# (b) toolchain
say node_version "$(node --version 2>/dev/null || echo absent)"
say claude_version "$(claude --version 2>/dev/null | awk '{print $1}' || echo absent)"
say kernel "$(uname -r 2>/dev/null || echo unknown)"

# (c) resolved config from the RUNNING daemon's environ — the file is not the authority
DP=""
[ -r "$R/mind_api/state/daemon.pid" ] && DP="$(tr -dc '0-9' < "$R/mind_api/state/daemon.pid" 2>/dev/null)"
say daemon_pid "${DP:-none}"
if [ -n "$DP" ] && [ -r "/proc/$DP/environ" ]; then
  say daemon_environ_readable yes
  for K in STORAGE_BACKEND ENVIRONMENT_ID MACHINE_ID; do
    V="$(tr '\0' '\n' < "/proc/$DP/environ" 2>/dev/null | grep -m1 "^$K=" | cut -d= -f2-)"
    say "resolved_$K" "${V:-unset}"
  done
else
  say daemon_environ_readable no
fi
# (c1) The daemon's OWN startup declaration (g-115-3410) — the AUTHORITY for which
# backend is actually in force. /proc/<pid>/environ above shows only the EXEC-TIME
# environment block; a daemon that derives its config IN-PROCESS at startup
# (_load_env_local -> _apply_environment_registry, mind_api/src/__main__.py:526-536)
# runs correctly while those keys stay ABSENT from /proc/environ for its entire life.
# Reading environ alone therefore reports a CORRECTLY-configured node as DRIFT.
# Observed on foxtrot 2026-07-25..27 (g-115-3157), 31h of false HIGH goals: environ
# ABSENT, .env.local correct, daemon log "resolved STORAGE_BACKEND=own-cloud", and
# its team-state shard reaching authoritative S3 through a daemon-ONLY writer
# (team-state-update.sh has no CLI fallback) — which is possible only on own-cloud.
# This does NOT blind the check to the real failure: a genuinely local-only daemon
# logs "<unset->local>", normalised to "local" by the reader and flagged as drift.
RL="$(grep -h 'resolved STORAGE_BACKEND=' "$R"/mind_api/state/*.log 2>/dev/null | tail -1)"
if [ -n "$RL" ]; then
  say daemon_logline_readable yes
  # NOTE: this whole collector is a %%-format template (_COLLECTOR %% {...}), so a
  # literal percent MUST be doubled — including inside COMMENTS like this one,
  # which is how this very line first broke the template. A single percent
  # raises "not enough arguments for format string" and kills collection for
  # EVERY node, not just the one being edited.
  say logline_STORAGE_BACKEND "$(printf '%%s\n' "$RL" | sed -n 's/.*resolved STORAGE_BACKEND=\([^ ]*\).*/\1/p')"
  say logline_ENVIRONMENT_ID "$(printf '%%s\n' "$RL" | sed -n 's/.*ENVIRONMENT_ID=\([^ ]*\).*/\1/p')"
  # MACHINE_ID (g-115-5433). _daemon_val has ALWAYS preferred logline_MACHINE_ID —
  # it reads "logline_%%s" %% key generically — but no producer ever emitted it, so
  # the MACHINE_ID verdict fell through to the exec-time /proc/environ read on every
  # node. That is the false positive the two lines above exist to prevent, left live
  # on the third audited key: `_load_env_local` sets MACHINE_ID in-process AFTER
  # exec, so a daemon that derives it that way shows it ABSENT from /proc/environ
  # for its whole life (identical mechanism to the foxtrot g-115-3157 case above).
  # THAT ABSENCE IS START-PATH DEPENDENT, NOT UNIVERSAL — corrected 2026-08-11 from
  # alpha's resolution of hyp 2026-08-09_machineid-logline-arm-clears-drift-on-restart,
  # which measured MACHINE_ID **PRESENT** in /proc/<pid>/environ on cc-03 (pid 1314666)
  # and cc-04 (pid 1233253), and absent only on foxtrot/WSL2. A daemon exec'd from a
  # shell that already exported MACHINE_ID lands it in the EXEC-TIME block, which is
  # what Linux /proc/<pid>/environ shows; an in-process putenv would not appear there.
  # So on those two boxes this arm is REDUNDANT-BUT-HARMLESS today, and the node it
  # was really needed for is the one still reading 'unset'. Do NOT read that as an
  # argument to remove it: it is the only arm that works when the daemon derives
  # config in-process, and which boxes those are is not a fixed property.
  # WHY THE EXEC ENV DIFFERS PER BOX IS NO LONGER UNMEASURED — and the answer is
  # that it is not a property of the BOX at all. Measured 2026-08-11T22:0x (echo,
  # hostname cc-03, uname -r 6.8.0-137-generic; hyp
  # 2026-08-04_machine-id-unset-follows-rt-spawn-lineage, resolved CONFIRMED),
  # reaching each node over THIS checker's own ssh transport with a CONTROL
  # hostname echo. The discriminator is whether the CURRENT daemon was started
  # through `_runtime.sh rt_spawn`, which unsets MACHINE_ID in the spawn subshell
  # before exec. Compare each node's daemon start time against the last
  # `rt_spawn -- attempting daemon start` stamp in its own mind_api/state/spawn.log:
  #   cc-05  daemon 18:50:38  rt_spawn 18:50:39 (+1s)  -> environ ABSENT  -> unset
  #   cc-02  daemon 19:01:09  rt_spawn 19:01:10 (+1s)  -> environ ABSENT  -> unset
  #   cc-04  daemon 19:50:49  rt_spawn 08-10T04:15    -> environ PRESENT -> cc-04
  #   cc-03  daemon 18:15:19  rt_spawn 08-11T05:46    -> environ PRESENT -> cc-03
  # Four for four, two positive and two negative controls, all on one kernel line.
  # So a node "flips" between PRESENT and ABSENT across its own restarts and the
  # per-box split above is a snapshot of launch lineage, not a stable fact about
  # the machine. Read a PRESENT/ABSENT reading as evidence about the last restart's
  # path, never as a box attribute.
  # THE ARM IS WIRED INTO THE VERDICT ONLY, NOT INTO THE JSON OUTPUT. `_daemon_val`
  # prefers logline_MACHINE_ID (so drift is correctly clean on all four nodes), but
  # the `machine_id_resolved` field emitted for the caller reads
  # fields.get("resolved_MACHINE_ID") DIRECTLY -- the exec-time environ read, with
  # no logline fallback. That is why cc-05 and cc-02 publish `unset` in --json while
  # simultaneously passing. Verified by hand on both boxes with the exact collector
  # line from this template: RL is non-empty and the sed yields cc-05 / cc-02, and
  # their state/*.log files carry zero NUL bytes, so nothing about the READ is
  # broken. Tracked separately; do not "fix" it by changing this say line.
  # Empty here on a daemon that started before the emitter landed, which falls
  # through to the old environ read — no regression, self-heals on next restart.
  say logline_MACHINE_ID "$(printf '%%s\n' "$RL" | sed -n 's/.*MACHINE_ID=\([^ ]*\).*/\1/p')"
else
  say daemon_logline_readable no
fi
# file-side values for the same three, to expose file-vs-daemon disagreement
if [ -r "$R/.env.local" ]; then
  for K in STORAGE_BACKEND ENVIRONMENT_ID MACHINE_ID; do
    V="$(grep -m1 "^$K=" "$R/.env.local" 2>/dev/null | cut -d= -f2- | tr -d '"'"'"'')"
    say "file_$K" "${V:-unset}"
  done
fi

# (c2) CLI lane — the BARE-SUBPROCESS resolution path (g-115-3168). The daemon
# environ is not the authority EITHER. The registry derivation
# (ENVIRONMENT_ID -> core/config/environments/<id>.yaml -> STORAGE_*) lived only in
# the daemon's main() (_apply_environment_registry); the bare-subprocess lane
# (storage_backend.get_backend -> _bootstrap_env_defaults) never consulted it. On
# cc-02 for 11 days the daemon resolved own-cloud (CORRECT) while every bare CLI
# subprocess resolved 'local' (BROKEN) — and BOTH single-lane checks passed the
# whole time, because each lane agreed with the manifest when asked alone. Sample
# this lane independently, with the three keys UNSET, so we measure what the lane
# RESOLVES rather than what happens to be ambient in the collector's own shell.
# Same three allowlisted non-secret identifiers as above — no other value is read.
# (h) CREDENTIAL VALUE SHAPE rides in this same snippet (g-115-3344), deliberately:
# the predicate must exist in exactly ONE implementation, and this is the only place
# on the remote node that can read BOTH lanes — its own os.environ (the CLI lane) and
# /proc/<pid>/environ (the daemon lane). A shell-side copy for one lane and a Python
# copy for the other would be two predicates free to disagree, which is the defect
# class this checker exists to find, committed by the checker itself.
# The VALUE never leaves this process: only a verdict and a length are printed.
SHAPE_SPEC='%(shape_spec)s'
CLI_PY='
import os, re, sys
r = os.environ.get("FCP_ROOT") or "."
sys.path.insert(0, os.path.join(r, "core", "scripts"))
try:
    import storage_backend as s
    s._bootstrap_env_defaults()
except Exception:
    pass
for k in ("STORAGE_BACKEND", "ENVIRONMENT_ID", "MACHINE_ID"):
    print(k + "=" + (os.environ.get(k) or "unset"))
_CLS = {"token": re.compile(r"^[A-Za-z0-9_-]+$"),
        "token_punct": re.compile(r"^[A-Za-z0-9_./+=:-]+$")}
def _verdict(v, mn, cls):
    if not v:
        return "absent"
    if len(v) < mn:
        return "too_short"
    rx = _CLS.get(cls)
    if rx is not None and not rx.match(v):
        return "charset"
    return "ok"
_denv = {}
_dp = os.environ.get("FCP_DAEMON_PID") or ""
if _dp.isdigit():
    try:
        with open("/proc/" + _dp + "/environ", "rb") as fh:
            for _part in fh.read().split(bytes(1)):
                if b"=" in _part:
                    _k, _, _v = _part.decode("utf-8", "replace").partition("=")
                    _denv[_k] = _v
    except OSError:
        _denv = {}
for _e in (os.environ.get("FCP_SHAPE_SPEC") or "").split():
    _p = _e.split(":")
    if len(_p) != 3:
        continue
    try:
        _mn = int(_p[1])
    except ValueError:
        continue
    _key, _cls = _p[0], _p[2]
    _cv = os.environ.get(_key) or ""
    print("cshape_" + _key + "=" + _verdict(_cv, _mn, _cls))
    print("clen_" + _key + "=" + str(len(_cv)))
    if _denv:
        _dv = _denv.get(_key) or ""
        print("dshape_" + _key + "=" + _verdict(_dv, _mn, _cls))
        print("dlen_" + _key + "=" + str(len(_dv)))
'
# `py -3` leads: on the fleet's Windows node a bare `python3 -c` hits the
# Microsoft Store stub (CLAUDE.md "Python Invocation"), and the .python-shim +
# PreToolUse defences do NOT apply here — this collector is a raw bash script
# piped over SSH, not a Claude-Code Bash call. Non-emptiness is NOT an accept
# condition: that stub can emit "Python was not found" text, which would sail
# through as a readable lane whose three values all parse to "unset" and fire a
# FALSE DRIFT on a healthy node. Gate on OUTPUT SHAPE instead — require all
# three expected KEY= lines — which defends against ANY interpreter emitting
# noise rather than special-casing one platform. stderr is folded into the
# capture so a failure is diagnosable, but only a failure CLASS is ever emitted
# (never the captured text): interpreter output is untrusted and must not reach
# stdout, the JSON, or the board. (g-115-3168 fresh-eyes, finding
# bravo-fec-cli-lane-dead-on-windows-node-202607260404)
CLI_RAW=""
CLI_ERRCLASS=""
for PYBIN in "py -3" python3 python; do
  # Deliberate word-split: "py -3" is two tokens.
  set -- $PYBIN
  command -v "$1" >/dev/null 2>&1 || continue
  OUT="$(env -u STORAGE_BACKEND -u ENVIRONMENT_ID -u MACHINE_ID FCP_ROOT="$R" \
      FCP_SHAPE_SPEC="$SHAPE_SPEC" FCP_DAEMON_PID="${DP:-}" "$@" -c "$CLI_PY" 2>&1)" || true
  SHAPE_OK=yes
  for K in STORAGE_BACKEND ENVIRONMENT_ID MACHINE_ID; do
    printf '%%s\n' "$OUT" | grep -q "^$K=" || SHAPE_OK=no
  done
  if [ "$SHAPE_OK" = yes ]; then
    CLI_RAW="$OUT"
    break
  fi
  CLI_ERRCLASS=bad_output_shape
done
if [ -n "$CLI_RAW" ]; then
  say cli_lane_readable yes
  for K in STORAGE_BACKEND ENVIRONMENT_ID MACHINE_ID; do
    V="$(printf '%%s\n' "$CLI_RAW" | grep -m1 "^$K=" | cut -d= -f2-)"
    say "cli_$K" "${V:-unset}"
  done
  # (h) credential VALUE SHAPE — pass through the verdict/length lines the snippet
  # emitted. Anchored to the four exact prefixes so nothing else in the capture can
  # ride out on this path; interpreter noise is still never emitted (g-115-3168).
  printf '%%s\n' "$CLI_RAW" | grep -E '^(cshape_|clen_|dshape_|dlen_)[A-Za-z0-9_]+=' || true
else
  say cli_lane_readable no
  say cli_lane_error "${CLI_ERRCLASS:-no_interpreter}"
fi

# (e) path config shape — key NAMES; AGENT_WRITE_PATH reported as a ROOT COUNT only
PC=""
for c in "$R"/agents/*/local-paths.conf; do [ -r "$c" ] && PC="$c" && break; done
if [ -n "$PC" ]; then
  say paths_conf found
  say paths_keys "$(grep -oE '^[A-Za-z_][A-Za-z0-9_]*=' "$PC" 2>/dev/null | tr -d '=' | sort -u | paste -sd, -)"
  AWP="$(grep -m1 '^AGENT_WRITE_PATH=' "$PC" 2>/dev/null | cut -d= -f2- | tr -d '"')"
  if [ -n "$AWP" ]; then
    say agent_write_roots "$(printf '%%s' "$AWP" | tr ':,' '\n\n' | grep -c .)"
  else
    say agent_write_roots 0
  fi
else
  say paths_conf missing
fi

# (d) deploy PUBLIC keys (public by definition) for authoritative API body-matching.
# TWO classes, and the distinction is load-bearing: a registered read-only key merely
# SITTING on disk does not break pushes — only the key git is CONFIGURED to use does.
# Measured 2026-07-25: echo and zeta both carry a stale `ayoai_deploy.pub` (registered
# read-only) alongside their per-node read-write key, while ~/.ssh/config IdentityFile
# names the read-write one. Matching every on-disk pubkey reported both as broken; they
# push fine. Emitting only `pubkey` would be a checker whose input does not mean what
# the check assumes.
CFGID=""
[ -r "$HOME/.ssh/config" ] && CFGID="$(grep -iE '^[[:space:]]*identityfile' "$HOME/.ssh/config" 2>/dev/null | awk '{print $2}')"
SSHCMD="$(cd "$R" 2>/dev/null && git config --get core.sshCommand 2>/dev/null || true)"
[ -n "${GIT_SSH_COMMAND:-}" ] && SSHCMD="$SSHCMD ${GIT_SSH_COMMAND}"
for p in "$HOME"/.ssh/*.pub; do
  [ -r "$p" ] || continue
  BODY="$(awk '{print $1" "$2}' "$p" 2>/dev/null)"
  say pubkey "$BODY"
  PRIV="${p%%.pub}"
  IS_CFG=no
  for f in $CFGID; do
    fe="$(eval echo "$f" 2>/dev/null)"
    [ "$fe" = "$PRIV" ] && IS_CFG=yes
  done
  case "$SSHCMD" in *"$PRIV"*) IS_CFG=yes ;; esac
  [ "$IS_CFG" = yes ] && say configured_pubkey "$BODY"
done
"""


def _load_manifest(path=MANIFEST):
    try:
        import yaml
    except ImportError:
        return None, "pyyaml not available"
    if not path.exists():
        return None, "manifest not found: %s" % path
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f), None
    except Exception as e:  # noqa: BLE001 - manifest errors must be reported, not raised
        return None, "manifest parse error: %s" % e


def _is_self(node):
    """True when `node` is the box we are running on.

    Without this the local node is probed over ssh BACK TO ITSELF, which fails
    `Permission denied (publickey)` on every box that does not authorise its own
    key — so the ONE node whose config we can always read reported UNREACHABLE
    (observed on cc-05, first live run). Hostname is the discriminator the rest of
    the fleet conventions already use (fleet-topology.md re-sync probe).

    platform.node(), NOT os.uname() (g-115-3085): os.uname does not exist on
    Windows, so the old `except AttributeError: return False` made this return
    False for EVERY node on a win32 box — self-detection silently off, the tool
    ssh'd back to itself, and the local node reported UNREACHABLE. That is the
    exact failure this function was written to prevent, reintroduced on half
    the fleet by a POSIX-only call. platform.node() returns the same nodename
    on POSIX, so the fix is behaviour-preserving there.

    Compared casefolded because hostnames are case-insensitive (DNS) and the
    platforms disagree in practice: Windows reports DESKTOP-O91DLK2 while
    manifests and POSIX boxes commonly carry lowercase.
    """
    mine = (platform.node() or "").strip()
    theirs = (node.get("host") or "").strip()
    return bool(mine) and mine.casefold() == theirs.casefold()


_SHAPE_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SHAPE_CLASSES = ("token", "token_punct")


def _shape_spec(manifest):
    """Build the collector's `KEY:MIN:CLASS ...` spec from the manifest.

    Validated strictly because the result is interpolated into a SINGLE-QUOTED
    shell string on the remote node: a key carrying a quote would break out of it
    and run as code. A malformed entry is DROPPED, never passed through — but the
    drop is not silent: `_check_node` (h) reports any declared key that produced no
    verdict, so a manifest typo surfaces as `shape unverified` instead of quietly
    shrinking the check (guard-1760 — a checker must not report only what it ran).
    """
    out = []
    for key, spec in sorted((manifest.get("value_shape_expectations") or {}).items()):
        # Validate and EMIT the same string. Validating a derived form (str(key))
        # while interpolating the raw one leaves the guard and the payload free to
        # differ — the shell only ever sees what is emitted, so that is what the
        # regex has to have approved.
        key_s = str(key)
        if not _SHAPE_KEY_RE.match(key_s) or not isinstance(spec, dict):
            continue
        charset_s = str(spec.get("charset") or "")
        if charset_s not in _SHAPE_CLASSES:
            continue
        try:
            min_len = int(spec.get("min_len"))
        except (TypeError, ValueError):
            continue
        if min_len < 0:
            continue
        out.append("%s:%d:%s" % (key_s, min_len, charset_s))
    return " ".join(out)


def _collect(node, timeout=45, shape_spec=""):
    """Collect one node's config shape. Runs LOCALLY for self, over ssh otherwise.

    Returns (fields, error). error is None on success.
    """
    root = node.get("root") or "/opt/ayoai-mind"
    script = _COLLECTOR % {"root": shlex.quote(root), "shape_spec": shape_spec}
    if _is_self(node):
        try:
            from _runtime_bash import BASH  # rb-1472: not bare "bash"
            p = subprocess.run([BASH, "-s"], input=script, capture_output=True,
                               text=True, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError) as e:
            return {}, "local collect failed: %s" % e
        if p.returncode != 0:
            return {}, "local collect rc=%d" % p.returncode
        return _parse_collector(p.stdout), None
    target = "%s@%s" % (node.get("user") or "root", node["addr"])
    cmd = ["ssh"] + SSH_OPTS + [target, "bash -s"]
    try:
        p = subprocess.run(cmd, input=script, capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {}, "ssh timeout after %ss" % timeout
    except OSError as e:
        return {}, "ssh spawn failed: %s" % e
    if p.returncode != 0:
        err = (p.stderr or "").strip().splitlines()
        return {}, "ssh rc=%d %s" % (p.returncode, err[-1] if err else "")
    return _parse_collector(p.stdout), None


def _parse_collector(stdout):
    fields, pubkeys, configured = {}, [], []
    for line in (stdout or "").splitlines():
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if k == "pubkey":
            pubkeys.append(v)
        elif k == "configured_pubkey":
            configured.append(v)
        else:
            fields[k] = v
    fields["pubkeys"] = pubkeys
    fields["configured_pubkeys"] = configured
    return fields


def _gh_deploy_keys(repo):
    """Return {pubkey_body: {"read_only": bool, "title": str}} or (None, err)."""
    try:
        p = subprocess.run(
            ["gh", "api", "repos/%s/keys" % repo, "--paginate"],
            capture_output=True, text=True, timeout=90)
    except (subprocess.TimeoutExpired, OSError) as e:
        return None, "gh api failed: %s" % e
    if p.returncode != 0:
        return None, "gh api rc=%d %s" % (p.returncode, (p.stderr or "").strip()[:120])
    try:
        rows = json.loads(p.stdout or "[]")
    except json.JSONDecodeError as e:
        return None, "gh api json: %s" % e
    out = {}
    for r in rows:
        body = (r.get("key") or "").strip()
        # normalise to "type base64" (GitHub may append a comment)
        parts = body.split()
        if len(parts) >= 2:
            out[parts[0] + " " + parts[1]] = {
                "read_only": bool(r.get("read_only")),
                "title": r.get("title") or "",
                "id": r.get("id"),
            }
    return out, None


def _cmp_version(got, floor):
    """True when `got` >= `floor` on a dotted numeric prefix. Unknown -> True (fail-open)."""
    def nums(s):
        m = re.findall(r"\d+", s or "")
        return [int(x) for x in m[:3]]
    g, f = nums(got), nums(floor)
    if not g or not f:
        return True
    g += [0] * (3 - len(g))
    f += [0] * (3 - len(f))
    return g >= f


def _check_node(node, fields, manifest, deploy_keys):
    """Return list of drift strings (empty == PASS)."""
    drift = []
    agent = node.get("agent")

    if fields.get("root_exists") == "no":
        drift.append("framework root %s does not exist" % node.get("root"))
        return drift  # everything else would be noise

    # (a) env key set — NAMES only
    if fields.get("env_file") == "unreadable":
        drift.append(".env.local unreadable")
    else:
        have = set(x for x in (fields.get("env_keys") or "").split(",") if x)
        required = set(manifest.get("required_env_keys") or [])
        optional = set(manifest.get("optional_env_keys") or [])
        missing = sorted(required - have)
        if missing:
            drift.append("missing required env key(s): %s" % ", ".join(missing))
        unknown = sorted(have - required - optional)
        if unknown:
            # informational, not drift — surfaced so a new key cannot blend in silently
            drift.append("INFO unrecognised env key(s) (not drift, update manifest if intended): %s"
                         % ", ".join(unknown))

    # (b) toolchain
    tc = manifest.get("toolchain") or {}
    nv = fields.get("node_version") or "absent"
    if nv == "absent":
        drift.append("node not installed")
    else:
        major = re.findall(r"\d+", nv)
        floor = tc.get("node_major_min")
        if major and floor and int(major[0]) < int(floor):
            drift.append("node %s below engine floor v%s" % (nv, floor))
    cv = fields.get("claude_version") or "absent"
    if cv == "absent":
        drift.append("claude CLI not installed")
    elif tc.get("claude_min") and not _cmp_version(cv, tc["claude_min"]):
        drift.append("claude %s below floor %s" % (cv, tc["claude_min"]))

    # (c) resolved config — the daemon environ is the authority, not the file
    exp = manifest.get("expected_resolved") or {}
    if fields.get("daemon_environ_readable") != "yes":
        drift.append("INFO daemon not running or environ unreadable (pid=%s) — "
                     "resolved-config check could not run; file values shown only"
                     % fields.get("daemon_pid"))
        for k, want in exp.items():
            got = fields.get("file_%s" % k)
            if got and got != want:
                drift.append("file %s=%s expected %s" % (k, got, want))
    else:
        # Daemon-lane value: prefer the daemon's OWN startup declaration
        # (g-115-3410) over /proc/<pid>/environ. environ exposes only the
        # EXEC-TIME block, so a daemon that derives its config in-process
        # (mind_api/src/__main__.py:526-536) reports every derived key as
        # "unset" there for its whole life, and a correctly-configured node
        # reads as DRIFT — g-115-3157, where foxtrot emitted 31h of false HIGH
        # goals while demonstrably writing to own-cloud. The logline states what
        # is actually in force. A genuinely local-only daemon logs
        # "<unset->local>", normalised to "local" here, so the failure this
        # check exists for (2026-07-26: 28 of 49 starts local-only, ~8 encodings
        # stranded) is still caught.
        def _daemon_val(key):
            if fields.get("daemon_logline_readable") == "yes":
                lv = fields.get("logline_%s" % key)
                if lv:
                    if lv.startswith("<unset"):
                        return "local" if key == "STORAGE_BACKEND" else "unset"
                    return lv
            return fields.get("resolved_%s" % key)

        for k, want in exp.items():
            got = _daemon_val(k)
            if got in (None, "unset"):
                drift.append("daemon has no %s (expected %s)" % (k, want))
            elif got != want:
                drift.append("daemon-resolved %s=%s expected %s" % (k, got, want))
            fileval = fields.get("file_%s" % k)
            if fileval and got and fileval != got:
                drift.append("%s disagrees: daemon=%s file=%s (daemon wins; file is stale)"
                             % (k, got, fileval))
        mid = _daemon_val("MACHINE_ID")
        if node.get("machine_id_check") != "skip":
            if mid in (None, "unset"):
                drift.append("daemon has no MACHINE_ID")
            elif mid != node.get("host"):
                drift.append("MACHINE_ID=%s but manifest host is %s" % (mid, node.get("host")))

    # (c2) LANE PARITY — g-115-3168. The daemon environ is not the authority either.
    # A single-lane probe is structurally blind to the shape that actually occurred
    # on cc-02 for 11 days: keys PRESENT, each lane internally self-consistent, but
    # the two lanes resolving DIFFERENTLY. DRIFT is therefore (a) a lane disagrees
    # with the manifest, OR (b) the lanes disagree with each other. (b) is the new
    # condition, and it is the one no single-lane check can ever see.
    if fields.get("cli_lane_readable") != "yes":
        # Report the observed CLASS, never a guessed cause. "no usable python3"
        # was the original text and it asserted something never measured: an
        # empty capture is equally consistent with the interpreter running and
        # the storage_backend import raising. The collector distinguishes
        # no_interpreter (nothing to run) from bad_output_shape (something ran
        # and did not produce the three KEY= lines — the Windows Store-stub
        # signature).
        why = fields.get("cli_lane_error") or "unreported"
        drift.append("INFO CLI lane unsampled (%s) — lane-parity check could "
                     "not run; daemon lane shown only" % why)
    else:
        for k, want in exp.items():
            got = fields.get("cli_%s" % k)
            if got in (None, "unset"):
                drift.append("CLI lane has no %s (expected %s)" % (k, want))
            elif got != want:
                drift.append("cli-resolved %s=%s expected %s" % (k, got, want))
        # (b) lane-vs-lane. Only meaningful when BOTH lanes were actually sampled;
        # a missing value is already reported by the per-lane checks above, so
        # skip it here rather than emitting a second, noisier line for one fault.
        if fields.get("daemon_environ_readable") == "yes":
            for k in ("STORAGE_BACKEND", "ENVIRONMENT_ID", "MACHINE_ID"):
                dv = fields.get("resolved_%s" % k)
                cv = fields.get("cli_%s" % k)
                if dv in (None, "unset") or cv in (None, "unset"):
                    continue
                if dv != cv:
                    drift.append(
                        "LANE DRIFT %s: daemon=%s cli=%s — the lanes resolve "
                        "differently; neither lane alone can see this" % (k, dv, cv))

    # (h) CREDENTIAL VALUE SHAPE — g-115-3344. (a) reads key NAMES only, so a key
    # that is PRESENT but holds garbage passes every check above. The 2026-07-26
    # incident: a ~/.bashrc bug exported the 158-char ERROR TEXT of a failed wslvar
    # call as OPENAI_API_KEY, and every dimension passed.
    #
    # Sampled from the RUNNING process environments — the CLI lane's own environ and
    # the daemon's /proc/<pid>/environ — NOT from .env.local. That is not stylistic:
    # a poisoned shell export is INVISIBLE in the file and visible only in a live
    # process, so a file check cannot see the incident class at all.
    shapes = manifest.get("value_shape_expectations") or {}
    if shapes:
        if fields.get("cli_lane_readable") != "yes":
            # ONE line, not one per key: the (c2) block above already named the lane
            # failure, and 10 identical INFO lines would bury the reason.
            drift.append("INFO credential value-shape unchecked (%d key(s)) — the "
                         "lane that evaluates them was unsampled" % len(shapes))
        else:
            for key in sorted(shapes):
                seen = False
                for lane, sp, lp in (("cli", "cshape_", "clen_"),
                                     ("daemon", "dshape_", "dlen_")):
                    verdict = fields.get(sp + key)
                    if verdict is None:
                        continue
                    seen = True
                    if verdict == "absent":
                        # NOT drift. A key set IN-PROCESS after exec is absent from
                        # /proc/environ for the daemon's whole life (the g-115-3157
                        # mechanism, measured live on cc-05 2026-08-09: ANTHROPIC_API_KEY
                        # absent in the daemon lane, present and ok in the CLI lane).
                        # Flagging it would reproduce the 31h of false HIGH goals this
                        # module already learned to avoid. Presence is (a)'s job.
                        continue
                    if verdict != "ok":
                        drift.append(
                            "%s value SHAPE fails in the %s lane: %s (length %s) — the "
                            "key is PRESENT, so every name-only check passes; its VALUE "
                            "is not credential-shaped" % (key, lane, verdict,
                                                          fields.get(lp + key, "?")))
                if not seen:
                    # Declared in the manifest but no verdict from either lane —
                    # a malformed entry dropped by _shape_spec, or a collector too
                    # old to emit it. Say so rather than counting it as passing.
                    drift.append("INFO %s declared in value_shape_expectations but no "
                                 "verdict returned — shape unverified" % key)

    # (e) path config shape
    if fields.get("paths_conf") == "missing":
        drift.append("no agents/*/local-paths.conf found")
    else:
        have = set(x for x in (fields.get("paths_keys") or "").split(",") if x)
        need = set(manifest.get("required_paths_keys") or [])
        miss = sorted(need - have)
        if miss:
            drift.append("local-paths.conf missing key(s): %s" % ", ".join(miss))
        if fields.get("agent_write_roots") == "0":
            drift.append("AGENT_WRITE_PATH empty or unset")

    # (d) deploy key read_only — matched by BODY, never by title
    dk_cfg = manifest.get("deploy_key") or {}
    if deploy_keys is None:
        drift.append("INFO deploy-key check skipped (GitHub API unavailable)")
    elif dk_cfg.get("require_write"):
        configured = fields.get("configured_pubkeys") or []
        present = fields.get("pubkeys") or []
        cfg_matched = [(b, deploy_keys[b]) for b in configured if b in deploy_keys]
        all_matched = [(b, deploy_keys[b]) for b in present if b in deploy_keys]
        if not all_matched:
            drift.append("INFO no local public key matches a registered deploy key "
                         "(node may authenticate by another path)")
        elif not cfg_matched:
            drift.append("INFO registered deploy key(s) present but none is the "
                         "configured git identity: %s"
                         % ", ".join(m["title"] for _b, m in all_matched))
        else:
            # DRIFT only on the key git is CONFIGURED to use. A read-only key merely
            # sitting on disk breaks nothing — reporting it as broken is a false
            # positive (echo/zeta both carry a stale registered read-only key).
            for _body, meta in cfg_matched:
                if meta["read_only"]:
                    drift.append("configured git deploy key '%s' (id %s) is READ-ONLY "
                                 "— pushes will fail" % (meta["title"], meta["id"]))
        stale = [m for b, m in all_matched
                 if b not in configured and m["read_only"]]
        if stale:
            drift.append("INFO stale registered READ-ONLY key(s) present on disk but not "
                         "the git identity: %s (harmless now; delete to avoid confusion)"
                         % ", ".join(m["title"] for m in stale))
    return drift


def _is_real_drift(items):
    return [d for d in items if not d.startswith("INFO ")]


def _node_label(r):
    """Display label for a node row — NEVER read `agent` directly for rendering.

    A node may legitimately carry no `agent` key: a fleet-agnostic box has no
    resident agent, and _roster_parity already skips such rows by construction
    (`if n.get("agent")`). Every rendering site must therefore tolerate the
    absence, and one of them does not merely render badly — the drift-goal TITLE
    calls `sorted()` over these values, and sorting a mix of str and None raises
    TypeError. That is the worst possible placement: the crash fires only when
    drift HAS been found, i.e. exactly when the goal most needs filing, so it
    would present as "the checker stopped filing goals" rather than as a manifest
    problem. Falling back to the host keeps the label truthful (a box with no
    resident agent is best named by its box) and keeps the sort total.
    """
    return r.get("agent") or r.get("host") or "?"


def _live_roster(world_dir=None):
    """Composed ``agent_status`` keys — the live fleet roster. -> (set|None, err|None)

    Retirement is NOT reimplemented here: ``_team_state.compose_agent_status``
    already drops tombstoned rows (its ``_is_retired``), so a decommissioned
    agent never reaches this set and can never be reported as an unmeasured
    node. Same delegation liveness_check.py makes, for the same reason.
    """
    try:
        import yaml
        import _team_state
    except ImportError as e:  # noqa: BLE001 - must be reported, not raised
        return None, "roster unavailable (%s)" % e
    if world_dir is None:
        try:
            from _paths import WORLD_DIR as _wd
            world_dir = _wd
        except Exception as e:  # noqa: BLE001
            return None, "roster unavailable (world dir unresolved: %s)" % e
    wd = Path(world_dir)
    core_status = {}
    core_path = wd / "team-state.yaml"
    try:
        if core_path.exists():
            doc = yaml.safe_load(core_path.read_text(encoding="utf-8")) or {}
            core_status = doc.get("agent_status") or {}
    except Exception as e:  # noqa: BLE001
        return None, "team-state.yaml unreadable (%s)" % e
    try:
        rows = _team_state.load_rows(wd)
        return set(_team_state.compose_agent_status(core_status, rows)), None
    except Exception as e:  # noqa: BLE001
        return None, "team-state rows unreadable (%s)" % e


def _has_agent_identity(name, agents_root_dir=None):
    """True/False/None — does `name` carry an agent identity on THIS box?

    The discriminator for "is this roster row an agent at all". A roster is a
    HEARTBEAT TABLE: anything that ever stamped one appears in it, including
    test fixtures. Measured 2026-08-02 (bravo, hostname cc-05, uname -r
    6.8.0-136-generic): the composed roster carried `test-rb671` — no agent
    dir, no self.md, no team-state shard, last_active 3 months stale —
    alongside the five real agents. So the goal's own premise that manifest
    and roster "agree 5/5, this is LATENT" was already false, and reporting a
    bare set-difference as DRIFT would have emitted a false unmeasured-node on
    the very first run.

    rb-4246 prescribes exactly this check ("when a roster names an agent with
    NO local self.md, VERIFY its nature from independent signals before
    assigning a lane"); guard-1574 forbids resolving a fleet member by NAME
    alone. self.md is the evidence because every real agent has one and the
    identity file is what makes an agent an agent.

    Returns None when the check itself could not run — callers MUST NOT read
    None as either proof. LIMITATION, stated because a verdict must name its
    evidence: this is LOCAL-box evidence. A genuinely new agent whose dir has
    not synced here yet reads False, i.e. INFO instead of DRIFT — a false
    negative in the safe direction (under-claiming), never a false DRIFT.
    """
    if agents_root_dir is None:
        try:
            from _paths import agents_root
            agents_root_dir = agents_root()
        except Exception:  # noqa: BLE001
            return None
    try:
        root = Path(agents_root_dir)
        # Probe the ROOT before the per-agent file. Without this, a missing or
        # misresolved agents root makes EVERY lookup return a confident False —
        # N negatives instead of N unknowns — which silently downgrades every
        # would-be DRIFT to INFO. That is precisely the vacuous pass this
        # checker exists to prevent, reproduced inside its own evidence probe.
        # Found by the guard-343 fresh-eyes pass on this function's first
        # commit; measured alpha/bravo/sigma all False against a nonexistent
        # root, i.e. a roster check that can never report an unmeasured node
        # while appearing to have measured every one.
        if not root.is_dir():
            return None
        return (root / str(name) / "self.md").is_file()
    except OSError:
        return None


def _roster_parity(manifest, *, fleet_complete, world_dir=None, agents_root_dir=None):
    """Manifest node list vs the LIVE fleet roster (g-115-3160).

    THE VACUOUS PASS THIS CLOSES. The node list is HARDCODED in
    core/config/fleet-manifest.yaml, so an agent that joins the fleet is simply
    never iterated: the tool prints "5 PASS / 0 DRIFT" and a reader concludes
    THE FLEET is clean when one node was never measured. That is precisely the
    defect class this checker exists to catch, reproduced in the checker itself
    — an input (hardcoded roster) that does not mean what the reader assumes
    (the whole fleet).

    `fleet_complete` is REQUIRED and keyword-only for the same reason
    _is_blackout requires it (g-115-3198): a --node-filtered run deliberately
    measures a subset, and comparing that subset against the whole roster would
    report every unselected agent as an unmeasured node. This function CANNOT
    determine completeness from its inputs, so the caller must state it; a
    default would silently restore the failure.

    Deliberately does NOT auto-sync the manifest from the roster: the manifest
    carries per-node facts (host, ssh shape, root, machine_id_check) that no
    roster has, and appending a node with guessed fields would trade a visible
    gap for invisible wrong data.
    """
    out = {"checked": False, "reason": None, "drift": [], "info": [],
           "manifest_nodes": [], "roster": []}
    if not fleet_complete:
        out["reason"] = "node-filtered run — roster parity needs the whole fleet"
        return out
    roster, err = _live_roster(world_dir)
    if err:
        # Never silent: an unreadable roster is itself reportable. A roster we
        # could not read is NOT a roster that agrees.
        out["reason"] = err
        return out
    m_nodes = {n.get("agent") for n in (manifest.get("nodes") or []) if n.get("agent")}
    out["checked"] = True
    out["manifest_nodes"] = sorted(m_nodes)
    out["roster"] = sorted(roster)
    for name in sorted(roster - m_nodes):
        ident = _has_agent_identity(name, agents_root_dir)
        if ident is False:
            out["info"].append(
                "INFO roster row %s has no agent identity on this box (no self.md) "
                "— heartbeat-table residue, not an unmeasured node" % name)
        elif ident is True:
            out["drift"].append(
                "IN-ROSTER-NOT-IN-MANIFEST: %s is in the live roster AND carries an "
                "agent identity (self.md), but has no fleet-manifest node — this "
                "checker never measures it" % name)
        else:
            out["drift"].append(
                "IN-ROSTER-NOT-IN-MANIFEST: %s is in the live roster with no "
                "fleet-manifest node; its agent identity could not be checked on this "
                "box, so it is reported as unmeasured rather than assumed benign" % name)
    for name in sorted(m_nodes - roster):
        out["info"].append(
            "INFO manifest node %s is absent from the live roster — retired node still "
            "listed (it IS measured, so this is not a gap; drop it once the retirement "
            "is confirmed)" % name)
    return out


# A box whose most recent body-heartbeat is younger than this is a LIVE fleet box
# and must be measured. Older is reported as INFO (a box that has stopped), never
# as a coverage gap. 24h is deliberately far looser than worker_stall's 60-minute
# carrier threshold: that module asks "is this BODY ticking right now", which is a
# question about a unit of work; this one asks "is this BOX part of the fleet",
# which survives a box sitting idle between sessions. Reusing 60m here would flap
# every node into INFO overnight and re-hide the gap by morning.
_BOX_LIVE_WINDOW_HOURS = 24


def _live_boxes(agents_root_dir=None):
    """Live fleet BOXES, keyed by host. -> (dict host->latest_ts, meta)

    A scoped CALL to worker_stall.enumerate_carriers, never a second carrier
    reader (guard-2676). That helper already owns the two hard parts: it reads
    the AUTHORITATIVE store rather than the local read-through cache (guard-980 —
    a carrier written by another box may simply not exist locally), and it
    distinguishes "no carriers" from "I could not enumerate", which is the exact
    distinction this checker exists to preserve. WorkerStallProbe consumes the
    same source, so this is an established fleet signal, not a new one.

    WHY THE CARRIER AND NOT THE OBVIOUS ALTERNATIVES — measured 2026-08-07 on
    cc-08, because three sources look equally reasonable on paper and only one
    survives contact:
      - body-manifest.yaml (the shape g-115-5172 proposed): does NOT sync. The
        whole store holds exactly ONE, under zeta/temp/, and it is a test
        fixture. A parity check built on it would enumerate nothing, forever,
        and read as clean — the very failure being fixed.
      - the tailnet: genuinely box-keyed and live, but it enumerates a phone,
        a hypervisor host and an offline desktop alongside the agent boxes, so
        a bare set-difference reports the phone as an unmeasured fleet node.
        That is the false-DRIFT _has_agent_identity exists to prevent, moved to
        a new axis.
      - the carrier: 11 objects, 8 distinct hosts, zero false positives — a
        phone never writes one. It is also self-populating, which is the only
        property that matters (see _box_parity for why).
    """
    meta = {"complete": False, "read_via": "none", "reason": None}
    try:
        from pathlib import Path as _P
        from worker_stall import enumerate_carriers
        if agents_root_dir is None:
            from _paths import agents_root
            agents_root_dir = agents_root()
    except Exception as e:  # noqa: BLE001 - must be reported, not raised
        meta["reason"] = "carrier enumeration unavailable (%s)" % e
        return {}, meta
    try:
        rows, enum_meta = enumerate_carriers(_P(agents_root_dir))
    except Exception as e:  # noqa: BLE001
        meta["reason"] = "carrier enumeration failed (%s)" % e
        return {}, meta
    meta["complete"] = bool(enum_meta.get("complete"))
    meta["read_via"] = enum_meta.get("read_via") or "none"
    meta["reason"] = enum_meta.get("reason")
    boxes = {}
    for r in rows:
        doc = r.get("doc") or {}
        host = (doc.get("host") or "").strip()
        if not host:
            continue
        ts = doc.get("ts") or ""
        if ts > boxes.get(host, ""):
            boxes[host] = ts
    return boxes, meta


def _box_parity(manifest, *, fleet_complete, agents_root_dir=None, now=None):
    """Manifest HOST list vs the live BOX set (g-115-5172).

    THE DEFECT THIS CLOSES, and why _roster_parity above cannot close it. That
    function diffs manifest `agent` names against team-state `agent_status`
    keys — BOTH SIDES AGENT-KEYED. The surface it guards is keyed by BOX, and
    agent-set and box-set stopped being the same thing when the Mind/Body split
    landed: one agent can occupy two boxes (a second worker body contributes no
    new agent_status row), and an agent-agnostic EXTRA box has no resident agent
    at all (so it contributes none either). An agent-keyed enumeration therefore
    CANNOT enumerate boxes, no matter how correct it is on its own axis. That is
    rb-5514's move applied here: when an enumeration is stuck, change the AXIS it
    runs along rather than trying harder along the old one.

    Measured 2026-08-07 from cc-08: manifest hosts {cc-04, cc-05, cc-03, cc-02,
    LAPTOP-3IOFCNEO}; live boxes additionally {cc-07, cc-08, DESKTOP-O91DLK2}.
    Two of the three were running a node major the manifest pins against, which
    is precisely what this checker exists to catch and did not.

    WHY THIS IS NOT "just add the three hosts to the manifest". The node list has
    been edited EXACTLY ONCE — the commit that created it, 2026-07-25. Two
    commits have touched the file since; neither added or removed a host row.
    Three boxes were provisioned in that window and none was registered. A
    hand-maintained list that has never once been hand-maintained will not start
    being maintained because a fourth box needs adding, so a fix that depends on
    that happening is not a fix. Every input here is self-populating: a box that
    runs a Mind writes a carrier without anyone remembering to.

    Deliberately does NOT auto-append to the manifest, for the reason
    _roster_parity already gives: the manifest carries per-node facts (addr, ssh
    shape, user, root, machine_id_check) that no carrier has, and appending a row
    with guessed fields trades a visible gap for invisible wrong data.

    `fleet_complete` is REQUIRED and keyword-only for the same reason it is on
    _roster_parity and _is_blackout: a --node-filtered run deliberately measures
    a subset, and comparing that subset against the whole box set would report
    every unselected box as unmeasured.
    """
    out = {"checked": False, "reason": None, "drift": [], "info": [],
           "manifest_hosts": [], "boxes": [], "read_via": "none"}
    if not fleet_complete:
        out["reason"] = "node-filtered run — box parity needs the whole fleet"
        return out
    boxes, meta = _live_boxes(agents_root_dir)
    out["read_via"] = meta.get("read_via") or "none"
    if not meta.get("complete"):
        # Never silent, and never a pass. An enumeration that could not bound the
        # fleet is NOT an enumeration that agrees with the manifest — rendering
        # those two identically is the whole defect class (guard-1760).
        out["reason"] = (meta.get("reason")
                         or "carrier enumeration could not bound the fleet")
        return out
    m_hosts = {n.get("host") for n in (manifest.get("nodes") or []) if n.get("host")}
    out["checked"] = True
    out["manifest_hosts"] = sorted(m_hosts)
    out["boxes"] = sorted(boxes)
    now = now or datetime.now()
    for host in sorted(set(boxes) - m_hosts):
        age_h = _age_hours(boxes[host], now)
        if age_h is not None and age_h > _BOX_LIVE_WINDOW_HOURS:
            out["info"].append(
                "INFO box %s ran a Mind but its last body-heartbeat is %.0fh old — "
                "a box that has stopped, not an unmeasured live node" % (host, age_h))
        else:
            out["drift"].append(
                "LIVE-BOX-NOT-IN-MANIFEST: %s wrote a body-heartbeat %s but has no "
                "fleet-manifest node — this checker never measures it, and its "
                "config drift is invisible" % (
                    host,
                    "%.1fh ago" % age_h if age_h is not None
                    else "with an unparseable timestamp"))
    for host in sorted(m_hosts - set(boxes)):
        out["info"].append(
            "INFO manifest node %s has no body-heartbeat carrier — it IS measured by "
            "the per-node sweep, so this is not a coverage gap; it means no Mind has "
            "run there recently" % host)
    return out


def _age_hours(ts, now):
    """Hours between an ISO timestamp and `now`, or None if unparseable.

    Returns None rather than raising or defaulting to 0: a carrier with a
    malformed stamp must not silently become "fresh" (which would hide a real
    box) NOR "ancient" (which would demote a live box to INFO). The caller
    reports the unparseable case explicitly instead.
    """
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(str(ts).strip().replace("Z", ""))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    return (now - parsed).total_seconds() / 3600.0


def run(nodes_filter=None, want_json=False, file_investigate=False, strict=False):
    manifest, err = _load_manifest()
    if err:
        print("fleet-config-parity: %s" % err, file=sys.stderr)
        return 2
    nodes = manifest.get("nodes") or []
    if nodes_filter:
        nodes = [n for n in nodes if n.get("agent") in nodes_filter or n.get("host") in nodes_filter]
        if not nodes:
            print("fleet-config-parity: no manifest node matches %s" % nodes_filter,
                  file=sys.stderr)
            return 2

    deploy_keys, dk_err = _gh_deploy_keys((manifest.get("deploy_key") or {}).get("repo", ""))
    if dk_err:
        print("fleet-config-parity: %s" % dk_err, file=sys.stderr)

    shape_spec = _shape_spec(manifest)

    results = []
    for node in nodes:
        fields, cerr = _collect(node, shape_spec=shape_spec)
        if cerr:
            results.append({"agent": node.get("agent"), "host": node.get("host"),
                            "verdict": "UNREACHABLE", "detail": cerr, "drift": [],
                            "is_self": _is_self(node)})
            continue
        items = _check_node(node, fields, manifest, deploy_keys)
        real = _is_real_drift(items)
        results.append({
            "agent": node.get("agent"),
            "host": node.get("host"),
            "verdict": "DRIFT" if real else "PASS",
            "drift": items,
            "is_self": _is_self(node),
            "observed": {
                "node": fields.get("node_version"),
                "claude": fields.get("claude_version"),
                "kernel": fields.get("kernel"),
                # Report the value the VERDICT used (startup logline preferred over
                # exec-time environ, see _daemon_val). Displaying the raw environ
                # here printed "PASS sb=unset" for any daemon that derives its
                # config in-process — a contradiction on its face that invites a
                # re-investigation of the very false positive this fix removed.
                "storage_backend_resolved": (fields.get("logline_STORAGE_BACKEND")
                                             or fields.get("resolved_STORAGE_BACKEND")),
                "environment_id_resolved": (fields.get("logline_ENVIRONMENT_ID")
                                            or fields.get("resolved_ENVIRONMENT_ID")),
                "machine_id_resolved": fields.get("resolved_MACHINE_ID"),
                "storage_backend_cli": fields.get("cli_STORAGE_BACKEND"),
                "environment_id_cli": fields.get("cli_ENVIRONMENT_ID"),
                "machine_id_cli": fields.get("cli_MACHINE_ID"),
                "env_key_count": len([x for x in (fields.get("env_keys") or "").split(",") if x]),
                # Coverage counter for check (h), NOT a verdict list (g-115-3344).
                # A PASS node emits no shape drift, so without this a reader cannot
                # tell whether every value was ok or the check never ran — the two
                # look identical, which is this module's own defect class (guard-1760:
                # report what was DECLINED, not only what was run). Counts verdicts
                # actually returned; `absent` is tallied apart because it is
                # deliberately not drift.
                "value_shapes": {
                    "declared": len(manifest.get("value_shape_expectations") or {}),
                    "verdicts": sum(1 for k in fields
                                    if k.startswith(("cshape_", "dshape_"))),
                    "not_ok": sorted(k.split("_", 1)[1] for k, v in fields.items()
                                     if k.startswith(("cshape_", "dshape_"))
                                     and v not in ("ok", "absent")),
                },
            },
        })

    drifted = [r for r in results if r["verdict"] == "DRIFT"]
    unreachable = [r for r in results if r["verdict"] == "UNREACHABLE"]
    # fleet_complete is REQUIRED and stated here, not defaulted inside the predicate:
    # _is_blackout cannot tell a whole-fleet result set from a --node-filtered one, and
    # a default would silently re-hide that (g-115-3198; the rb-5169 discipline applied
    # to its own fix).
    blackout = _is_blackout(results, manifest, fleet_complete=not nodes_filter)
    # g-115-3160: the per-node loop above can only measure nodes the manifest
    # LISTS, so "N PASS / 0 DRIFT" is a statement about the manifest, not about
    # the fleet. Same fleet_complete contract as _is_blackout, same reason.
    roster_parity = _roster_parity(manifest, fleet_complete=not nodes_filter)
    roster_drift = roster_parity["drift"]
    # g-115-5172: roster parity above is AGENT-keyed on both sides, so no BOX can
    # ever appear in it. This is the same coverage question asked on the axis the
    # manifest is actually keyed by. Both run: an agent in the roster with no
    # manifest node and a live box with no manifest node are different gaps, and
    # neither subsumes the other.
    box_parity = _box_parity(manifest, fleet_complete=not nodes_filter)
    box_drift = box_parity["drift"]

    payload = {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "checked_from": os.uname().nodename if hasattr(os, "uname") else "unknown",
        "manifest_verified": manifest.get("last_verified"),
        "nodes_total": len(results),
        "pass": len(results) - len(drifted) - len(unreachable),
        "drift": len(drifted),
        "unreachable": len(unreachable),
        "blackout": blackout,
        "roster_parity": roster_parity,
        "box_parity": box_parity,
        "results": results,
    }

    if want_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print("fleet-config-parity  (from %s, manifest verified %s)"
              % (payload["checked_from"], payload["manifest_verified"]))
        for r in results:
            mark = {"PASS": "PASS ", "DRIFT": "DRIFT", "UNREACHABLE": "UNRCH"}[r["verdict"]]
            print("  [%s] %-8s %-16s %s" % (
                mark, _node_label(r), r["host"],
                r.get("detail") or (r["observed"].get("storage_backend_resolved") or "")))
            for d in r["drift"]:
                print("           - %s" % d)
        # Roster parity BEFORE the totals: the totals line counts MANIFEST nodes,
        # so a reader must learn whether the manifest enumerates the fleet before
        # being handed a number that silently assumes it does (g-115-3160).
        rp = roster_parity
        if not rp["checked"]:
            print("  [ROSTER] not checked — %s" % rp["reason"])
        else:
            for d in rp["drift"]:
                print("  [DRIFT] %s" % d)
            for i in rp["info"]:
                print("  [ INFO] %s" % i)
            if not rp["drift"] and not rp["info"]:
                print("  [ROSTER] manifest node list matches the live roster (%d agents)"
                      % len(rp["roster"]))
        # Box parity sits beside roster parity and before the totals, for the same
        # reason: the totals count MANIFEST nodes, and a reader must learn whether
        # the manifest enumerates the FLEET before being handed that number.
        bp = box_parity
        if not bp["checked"]:
            print("  [  BOX] not checked — %s" % bp["reason"])
        else:
            for d in bp["drift"]:
                print("  [DRIFT] %s" % d)
            for i in bp["info"]:
                print("  [ INFO] %s" % i)
            if not bp["drift"]:
                print("  [  BOX] every live box has a manifest node (%d boxes, read %s)"
                      % (len(bp["boxes"]), bp["read_via"]))
        # "%d PASS" counts MANIFEST nodes. Saying so is the whole point of this
        # goal: the unqualified form is what let a reader conclude THE FLEET was
        # clean when an unlisted node had never been measured.
        print("  -> %d PASS / %d DRIFT / %d UNREACHABLE  (of %d manifest nodes%s)"
              % (payload["pass"], payload["drift"], payload["unreachable"],
                 payload["nodes_total"],
                 # g-115-5172: box parity joins this condition. Without it a run
                 # with three unmeasured live BOXES still printed the unqualified
                 # form, which is the exact sentence that let a reader conclude the
                 # fleet was clean — the defect surviving in the summary line after
                 # being fixed everywhere above it.
                 "" if (rp["checked"] and not rp["drift"]
                        and bp["checked"] and not bp["drift"])
                 else "; see [ROSTER]/[BOX]/[DRIFT] above — this is NOT a whole-fleet verdict"))
        if roster_drift:
            print("  -> ROSTER DRIFT: %d live agent(s) have no manifest node and were "
                  "never measured (exit 1)" % len(roster_drift))
        if box_drift:
            print("  -> BOX DRIFT: %d live box(es) have no manifest node and were "
                  "never measured (exit 1)" % len(box_drift))
        if blackout:
            print("  -> BLACKOUT: every non-self node is UNREACHABLE — the sweep "
                  "measured nothing this run (exit 1)")

    if file_investigate and drifted:
        _file_investigate(drifted, payload, kind="drift")
    if file_investigate and blackout:
        # NON-self only: the blackout report calls these "peer nodes", and a
        # self-collect failure listed among them inflates the count and buries the
        # most diagnostic fact of an outage in the peer rows. _file_investigate
        # re-derives the self failure from payload["results"] and surfaces it on its
        # own line (g-115-3198).
        _file_investigate([r for r in unreachable if not r.get("is_self")],
                          payload, kind="blackout")

    if drifted:
        return 1
    if roster_drift:
        # An unmeasured live node is a real gap in the sweep's coverage, so it
        # must fail the same way a measured drift does — reporting it while
        # exiting 0 would leave the vacuous pass intact for every automated
        # caller that reads only the exit code (g-115-3160).
        return 1
    if box_drift:
        # Same contract on the box axis (g-115-5172). Printing BOX DRIFT while
        # exiting 0 would leave every exit-code-only caller — which is what the
        # sweep wiring reads — seeing a clean fleet with three boxes unmeasured.
        return 1
    if blackout:
        return 1
    if strict and unreachable:
        return 1
    return 0


def _is_blackout(results, manifest, *, fleet_complete):
    """True when every non-self node is UNREACHABLE — a systemic blackout, not a blip.

    fleet_complete is REQUIRED (keyword-only, no default) because this function
    CANNOT determine it: `results` from a `--node`-filtered run is indistinguishable
    from a whole-fleet run, and reading a deliberately-chosen 2-node subset as "the
    fleet" declares a fleet blackout from a diagnostic. Probed pre-fix:
    [zeta UNREACHABLE, echo UNREACHABLE] -> True. Every caller must state whether its
    result set enumerates the fleet; a default would restore the silent failure.
    (g-115-3198 — the same defect class this function was written to fix, found by
    the guard-343 fresh-eyes pass on its own first commit.)

    Keyed on NON-SELF nodes: the self node is collected locally, so during a real
    outage it still reports PASS/DRIFT while every peer goes UNREACHABLE. Requiring
    ALL nodes would therefore be false exactly when the fleet is down.

    min_non_self_nodes exists so this cannot reintroduce the blip-fails-the-sweep
    problem the rc=0 tolerance deliberately avoids: with one peer, "100% of peers
    unreachable" is indistinguishable from a single blip. Thresholds live in
    core/config/fleet-manifest.yaml, not here. Fail-open — a malformed or absent
    config must never turn a working sweep into a failing one.
    """
    if not fleet_complete:
        return False
    cfg = (manifest.get("blackout_escalation") or {})
    if not cfg.get("enabled", True):
        return False
    try:
        fraction = float(cfg.get("unreachable_fraction", 1.0))
        min_peers = int(cfg.get("min_non_self_nodes", 2))
    except (TypeError, ValueError):
        print("fleet-config-parity: malformed blackout_escalation config — "
              "not escalating (fail-open)", file=sys.stderr)
        return False
    peers = [r for r in results if not r.get("is_self")]
    # `not peers` is checked independently of min_peers: a configured
    # min_non_self_nodes of 0 would otherwise pass the threshold test and divide
    # by zero below. No peers means nothing to escalate about either way.
    if not peers or len(peers) < min_peers:
        return False
    unreachable_peers = [r for r in peers if r["verdict"] == "UNREACHABLE"]
    return (len(unreachable_peers) / len(peers)) >= fraction


def _file_investigate(nodes, payload, kind="drift"):
    """File ONE deduped Investigate naming the affected nodes (rb-548: conversation
    is not a queue). Dedup by exact origin_signal against open goals — never a title
    substring (g-115-2196: a prose-title search goes vacuous and re-files forever).

    kind="drift"    — nodes are ALIVE but misconfigured.
    kind="blackout" — every non-self node is UNREACHABLE (g-115-3162). Filed under a
    SEPARATE origin_signal so the two never dedup against each other: they have
    different causes and different fixes, and a standing drift Investigate must not
    swallow a blackout (nor the reverse).
    """
    signal = ("investigate:fleet-config-blackout" if kind == "blackout"
              else "investigate:fleet-config-drift")
    try:
        from _runtime_bash import bash_cmd  # guard-580 + guard-581
        q = subprocess.run(
            bash_cmd(SCRIPT_DIR / "aspirations-query.sh",
                     "--goal-status", "pending,in-progress",
                     "--goal-field", "origin_signal", signal),
            capture_output=True, text=True, timeout=180)
    except (subprocess.TimeoutExpired, OSError) as e:
        print("fleet-config-parity: dedup probe failed (%s) — NOT filing (guard-487 "
              "suppression gates fail CLOSED)" % e, file=sys.stderr)
        return
    if q.returncode != 0:
        print("fleet-config-parity: dedup probe rc=%d — NOT filing (fail closed)"
              % q.returncode, file=sys.stderr)
        return
    body = (q.stdout or "").strip()
    if body and body not in ("[]", "null"):
        print("fleet-config-parity: open %s Investigate already exists — not duplicating"
              % signal, file=sys.stderr)
        return

    lines = []
    if kind == "blackout":
        for r in nodes:
            lines.append("- %s (%s): %s" % (_node_label(r), r["host"],
                                            r.get("detail") or "UNREACHABLE"))
        # The self node is collected LOCALLY, so if it ALSO failed the cause is on
        # this box, not on five peers. Listing it as a peer row would inflate the
        # count and bury the single most diagnostic fact — while this same
        # description tells the reader to check THIS box first. Surface it as its
        # own line instead (g-115-3198). Derived from payload, not a parameter, so
        # the caller signature stays unchanged.
        self_down = [r for r in (payload.get("results") or [])
                     if r.get("is_self") and r.get("verdict") == "UNREACHABLE"]
        for r in self_down:
            lines.append(
                "- LOCAL COLLECTOR ALSO FAILED on %s (%s): %s  <- suspect THIS box "
                "first; this is not a peer outage"
                % (_node_label(r), r["host"], r.get("detail") or "UNREACHABLE"))
        desc = (
            "Filed automatically by core/scripts/fleet-config-parity.sh at %s from %s.\n\n"
            "FLEET BLACKOUT: every non-self node (%d) is UNREACHABLE. This is NOT config "
            "drift — nothing was compared, because nothing answered. The distinction "
            "matters for the fix: drift means a node is alive and wrong, blackout means "
            "the sweep has no measurement at all, so a clean-looking result proves "
            "nothing (a failed measurement is not a measurement of zero — guard-1091).\n\n"
            "%s\n\n"
            "Likely causes, cheapest first: the tunnel/VPN dropped on THIS box (check "
            "before suspecting five peers — one local cause explains all five, five "
            "simultaneous peer outages is the unlikely reading); the deploy key rotated; "
            "the manifest's addresses are stale; a genuine fleet-wide outage.\n\n"
            "Re-run: bash core/scripts/fleet-config-parity.sh --json\n"
            "Manifest: core/config/fleet-manifest.yaml (blackout_escalation tunes the "
            "threshold; update addresses there when a move is legitimate).\n"
            "No secret VALUE appears above or anywhere in this checker's output paths — "
            "env vars are compared by key NAME only."
            % (payload["checked_at"], payload["checked_from"], len(nodes),
               "\n".join(lines))
        )
        title = "Investigate: fleet blackout — all %d peer nodes UNREACHABLE" % len(nodes)
    else:
        for r in nodes:
            for d in _is_real_drift(r["drift"]):
                lines.append("- %s (%s): %s" % (_node_label(r), r["host"], d))
        desc = (
            "Filed automatically by core/scripts/fleet-config-parity.sh at %s from %s.\n\n"
            "CONFIGURATION DRIFT detected on %d node(s). These nodes are ALIVE — a liveness "
            "probe reports them healthy and is correct. They are misconfigured, which is the "
            "class that produced the last six fleet incidents and that liveness observability "
            "structurally cannot see.\n\n%s\n\n"
            "Re-run: bash core/scripts/fleet-config-parity.sh --json\n"
            "Manifest: core/config/fleet-manifest.yaml (update it when a difference is "
            "legitimate rather than silencing the check).\n"
            "No secret VALUE appears above or anywhere in this checker's output paths — "
            "env vars are compared by key NAME only."
            % (payload["checked_at"], payload["checked_from"], len(nodes),
               "\n".join(lines))
        )
        title = "Investigate: fleet config drift on %s" % ", ".join(
            sorted(_node_label(r) for r in nodes))
    goal = {
        "title": title,
        "description": desc,
        "status": "pending",
        "priority": "HIGH",
        "category": "infrastructure",
        "participants": ["agent"],
        "origin_signal": signal,
        "discovered_by": "fleet-config-parity",
        "discovery_type": "fix",
    }
    # The goal-duplication gate's structural_overlap check ALWAYS blocks this filing,
    # by construction and permanently — measured 2026-07-25 (g-115-3071): it matched
    # g-115-3071 (93.03) and g-353-02 (94.68) on file_path_hits for
    # `core/scripts/fleet-config-parity.sh`. A drift report necessarily names the script
    # that filed it and the manifest to update; g-353-02 is a RECURRING goal that is
    # permanently pending and permanently contains those same paths (step (f) invokes
    # them). So the overlap can never clear and the filer would be silently dead on
    # every future firing. The precise dedup for this filing is the exact-origin_signal
    # probe above (one open drift Investigate at a time) — which is strictly tighter
    # than structural_overlap here, not weaker. Override the gate, keep the real guard.
    justification = (
        "structural_overlap is inherent and permanent for this filer: the auto-filed "
        "%s report names core/scripts/fleet-config-parity.sh and "
        "core/config/fleet-manifest.yaml, which are ALSO named by the recurring sweep "
        "goal g-353-02 step (f) that invokes it — a permanently-pending match that can "
        "never clear. Real dedup is the exact origin_signal probe "
        "(%s) run immediately above, which allows at most "
        "one open %s Investigate. g-115-3071." % (kind, signal, kind)
    )
    def _add(extra):
        from _runtime_bash import bash_cmd  # guard-580 + guard-581
        return subprocess.run(
            bash_cmd(SCRIPT_DIR / "aspirations-add-goal.sh",
                     "--source", ESCALATION_SOURCE, ESCALATION_ASP, *extra),
            input=json.dumps(goal), capture_output=True, text=True, timeout=240)

    try:
        # rb-3835 pattern: attempt PLAIN first, then retry ONCE with a justified
        # override only if the duplication gate is what blocked. Not an unconditional
        # override — that would bypass the gate even on the day the collision clears,
        # and it would hide the block from gate telemetry. The first attempt's refusal
        # is a real, audited gate firing; the retry is the narrow, justified bypass.
        a = _add([])
        if a.returncode != 0 and "goal_duplication_blocked" in (a.stderr or ""):
            print("fleet-config-parity: duplication gate blocked (expected — structural); "
                  "retrying once with justified override (rb-3835)", file=sys.stderr)
            a = _add(["--override-duplication", justification])
        if a.returncode == 0:
            print("fleet-config-parity: filed %s Investigate for %d node(s)"
                  % (kind, len(nodes)), file=sys.stderr)
        else:
            # Name the FAILING checks, don't truncate the blob at 160 chars — diagnosing
            # the block above cost several probes because the reason was cut off mid-key.
            detail = (a.stderr or "").strip()
            try:
                failed = [c.get("name") for c in
                          json.loads(detail).get("gate_output", {}).get("checks", [])
                          if c.get("passed") is False]
                if failed:
                    detail = "failing checks: %s | %s" % (", ".join(failed), detail[:400])
            except Exception:
                detail = detail[:400]
            print("fleet-config-parity: filing failed rc=%d %s"
                  % (a.returncode, detail), file=sys.stderr)
    except (subprocess.TimeoutExpired, OSError) as e:
        print("fleet-config-parity: filing error %s" % e, file=sys.stderr)


def self_test():
    """Prove the secrets contract: the collector cannot emit a value for any key
    outside value_visible_env_keys. Runs entirely locally against a fixture."""
    import tempfile
    ok = True
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "mind_api" / "state").mkdir(parents=True)
        secret = "sk-THIS-MUST-NEVER-APPEAR-IN-OUTPUT"
        (root / ".env.local").write_text(
            "ANTHROPIC_API_KEY=%s\nAWS_SECRET_ACCESS_KEY=%s\n"
            "STORAGE_BACKEND=own-cloud\nENVIRONMENT_ID=ayoai-mind\nMACHINE_ID=test-box\n"
            % (secret, secret), encoding="utf-8")
        (root / "agents" / "x").mkdir(parents=True)
        (root / "agents" / "x" / "local-paths.conf").write_text(
            "WORLD_PATH=/w\nMETA_PATH=/m\nAGENT_WRITE_PATH=/a:/b\n", encoding="utf-8")
        # (h) value-shape fixture (g-115-3344). SYNTHETIC key names only — the
        # self-test must never need a real credential to prove the contract, and a
        # spec naming real keys would make this test's result depend on the box's
        # own .env.local. `poison` is modelled on the live incident: an error string
        # exported over a credential key name.
        poison = "wslvar: command not found - error text, not a credential"
        shape_spec = ("FCP_SELFTEST_GOOD:8:token FCP_SELFTEST_BAD:8:token "
                      "FCP_SELFTEST_SHORT:8:token FCP_SELFTEST_MISSING:8:token")
        script = _COLLECTOR % {"root": shlex.quote(str(root)),
                               "shape_spec": shape_spec}
        env = dict(os.environ)
        env["FCP_SELFTEST_GOOD"] = "abcd-1234-EFGH"
        env["FCP_SELFTEST_BAD"] = poison
        env["FCP_SELFTEST_SHORT"] = "abc"
        env.pop("FCP_SELFTEST_MISSING", None)
        from _runtime_bash import BASH  # rb-1472: not bare "bash"
        p = subprocess.run([BASH, "-s"], input=script, capture_output=True,
                           text=True, timeout=60, env=env)
        out = (p.stdout or "") + (p.stderr or "")
        if secret in out:
            print("SELF-TEST FAIL: a secret VALUE reached collector output", file=sys.stderr)
            ok = False
        else:
            print("self-test: secret value absent from collector output   OK")
        if "ANTHROPIC_API_KEY" not in out:
            print("SELF-TEST FAIL: key NAME missing — the check would be vacuous",
                  file=sys.stderr)
            ok = False
        else:
            print("self-test: key NAMES present (check is not vacuous)     OK")
        if "STORAGE_BACKEND=own-cloud" not in out.replace("file_", ""):
            print("SELF-TEST FAIL: allowlisted config value not read", file=sys.stderr)
            ok = False
        else:
            print("self-test: allowlisted config values readable           OK")
        if "agent_write_roots=2" not in out:
            print("SELF-TEST FAIL: AGENT_WRITE_PATH root count wrong", file=sys.stderr)
            ok = False
        else:
            print("self-test: AGENT_WRITE_PATH reported as count, not path OK")

        # (h) the shape check must DETECT garbage and never EMIT it. Both halves are
        # asserted: a check that leaks nothing because it evaluates nothing would
        # pass the leak test alone (rb-245 — verify the field exists before trusting
        # a clean count).
        if poison in out:
            print("SELF-TEST FAIL: a poisoned VALUE reached collector output",
                  file=sys.stderr)
            ok = False
        else:
            print("self-test: shape check emits no value, only verdicts  OK")
        expected = {
            "cshape_FCP_SELFTEST_GOOD=ok": "valid value verdicts ok",
            "cshape_FCP_SELFTEST_BAD=charset": "poisoned value caught as charset",
            "cshape_FCP_SELFTEST_SHORT=too_short": "short value caught",
            "cshape_FCP_SELFTEST_MISSING=absent": "unset key reads absent, not drift",
            "clen_FCP_SELFTEST_GOOD=14": "length reported",
        }
        missing = [line for line in expected if line not in out]
        if missing:
            print("SELF-TEST FAIL: shape verdict(s) missing/wrong: %s"
                  % ", ".join(sorted(missing)), file=sys.stderr)
            ok = False
        else:
            print("self-test: shape verdicts ok/charset/too_short/absent  OK")
        # No daemon.pid in the fixture, so the daemon lane must stay silent rather
        # than emit a fabricated verdict for a process that does not exist.
        if "dshape_" in out:
            print("SELF-TEST FAIL: daemon-lane verdict emitted with no daemon",
                  file=sys.stderr)
            ok = False
        else:
            print("self-test: daemon lane silent when no daemon runs        OK")
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--node", action="append", default=None,
                    help="limit to this agent or host (repeatable)")
    ap.add_argument("--json", action="store_true", dest="want_json")
    ap.add_argument("--file-investigate", action="store_true",
                    help="auto-file a deduped Investigate goal when DRIFT is found")
    ap.add_argument("--strict", action="store_true",
                    help="treat UNREACHABLE as failure (default: reachability is the "
                         "watchdog's job, not this checker's)")
    ap.add_argument("--self-test", action="store_true",
                    help="prove the secrets contract locally; no network, no ssh")
    args = ap.parse_args(argv)
    if args.self_test:
        return self_test()
    return run(nodes_filter=args.node, want_json=args.want_json,
               file_investigate=args.file_investigate, strict=args.strict)


if __name__ == "__main__":
    sys.exit(main())
