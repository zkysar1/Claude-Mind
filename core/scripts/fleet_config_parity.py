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
  (d) deploy key read_only    — from the GitHub API, matched by PUBLIC KEY BODY.
                                Never a title heuristic: zeta has both a read-only and
                                a read-write key registered, so only body-matching
                                identifies the one the box actually holds.
  (e) path config shape       — local-paths.conf key names + AGENT_WRITE_PATH root count

SECRETS CONTRACT (the property that must never regress)
  The remote collector is an ALLOWLIST emitter: it prints only the fields enumerated
  in _COLLECTOR, and the env-var line is passed through `grep -oE '^KEY='` + `tr -d '='`,
  which discards everything after the `=` before it is ever assigned. No secret value
  can reach stdout, the JSON output, the board, or a filed goal, because no code path
  reads one. Exactly three VALUES are read — STORAGE_BACKEND, ENVIRONMENT_ID,
  MACHINE_ID — non-secret configuration identifiers that ARE the audited subject
  (manifest key `value_visible_env_keys`). Public keys are emitted for API matching;
  a public key is public by definition. Verified by --self-test.

Usage:
  bash fleet-config-parity.sh                     # check all nodes, human output
  bash fleet-config-parity.sh --json              # machine-readable
  bash fleet-config-parity.sh --node alpha        # one node
  bash fleet-config-parity.sh --file-investigate  # auto-file an Investigate on DRIFT
  bash fleet-config-parity.sh --self-test         # prove the secrets contract holds

Exit codes:
  0  all reachable nodes PASS
  1  at least one node DRIFT, or a fleet BLACKOUT (see below)
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
CLI_PY='
import os, sys
r = os.environ.get("FCP_ROOT") or "."
sys.path.insert(0, os.path.join(r, "core", "scripts"))
try:
    import storage_backend as s
    s._bootstrap_env_defaults()
except Exception:
    pass
for k in ("STORAGE_BACKEND", "ENVIRONMENT_ID", "MACHINE_ID"):
    print(k + "=" + (os.environ.get(k) or "unset"))
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
  OUT="$(env -u STORAGE_BACKEND -u ENVIRONMENT_ID -u MACHINE_ID FCP_ROOT="$R" "$@" -c "$CLI_PY" 2>&1)" || true
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


def _collect(node, timeout=45):
    """Collect one node's config shape. Runs LOCALLY for self, over ssh otherwise.

    Returns (fields, error). error is None on success.
    """
    root = node.get("root") or "/opt/ayoai-mind"
    script = _COLLECTOR % {"root": shlex.quote(root)}
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

    results = []
    for node in nodes:
        fields, cerr = _collect(node)
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
            },
        })

    drifted = [r for r in results if r["verdict"] == "DRIFT"]
    unreachable = [r for r in results if r["verdict"] == "UNREACHABLE"]
    # fleet_complete is REQUIRED and stated here, not defaulted inside the predicate:
    # _is_blackout cannot tell a whole-fleet result set from a --node-filtered one, and
    # a default would silently re-hide that (g-115-3198; the rb-5169 discipline applied
    # to its own fix).
    blackout = _is_blackout(results, manifest, fleet_complete=not nodes_filter)

    payload = {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "checked_from": os.uname().nodename if hasattr(os, "uname") else "unknown",
        "manifest_verified": manifest.get("last_verified"),
        "nodes_total": len(results),
        "pass": len(results) - len(drifted) - len(unreachable),
        "drift": len(drifted),
        "unreachable": len(unreachable),
        "blackout": blackout,
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
                mark, r["agent"], r["host"],
                r.get("detail") or (r["observed"].get("storage_backend_resolved") or "")))
            for d in r["drift"]:
                print("           - %s" % d)
        print("  -> %d PASS / %d DRIFT / %d UNREACHABLE"
              % (payload["pass"], payload["drift"], payload["unreachable"]))
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
        from _runtime_bash import BASH  # rb-1472: not bare "bash"
        q = subprocess.run(
            [BASH, str(SCRIPT_DIR / "aspirations-query.sh"),
             "--goal-status", "pending,in-progress",
             "--goal-field", "origin_signal", signal],
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
            lines.append("- %s (%s): %s" % (r["agent"], r["host"],
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
                % (r["agent"], r["host"], r.get("detail") or "UNREACHABLE"))
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
                lines.append("- %s (%s): %s" % (r["agent"], r["host"], d))
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
            sorted(r["agent"] for r in nodes))
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
        from _runtime_bash import BASH  # rb-1472: not bare "bash"
        return subprocess.run(
            [BASH, str(SCRIPT_DIR / "aspirations-add-goal.sh"),
             "--source", ESCALATION_SOURCE, ESCALATION_ASP] + extra,
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
        script = _COLLECTOR % {"root": shlex.quote(str(root))}
        from _runtime_bash import BASH  # rb-1472: not bare "bash"
        p = subprocess.run([BASH, "-s"], input=script, capture_output=True,
                           text=True, timeout=60)
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
