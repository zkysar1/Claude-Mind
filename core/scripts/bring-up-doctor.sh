#!/usr/bin/env bash
# domain-leak-exempt: own-cloud infra diagnostic — boto3 references are functional (import probe + S3 head_bucket reachability check), same class as owncloud_backend.py / ready.py (g-328-06)
# bring-up-doctor.sh — ONE-COMMAND fresh-box diagnostic bundle (g-334-02, asp-334).
#
# Run this on any box where the Mind bring-up misbehaves (env created, agent
# created, but /start-to-autonomous fails — the Venheim shape) and paste the
# whole report back to the fleet. Every check is READ-ONLY and fail-soft; the
# script ALWAYS exits 0 (it is a diagnostic, not a gate).
#
# SECRET SAFETY (guard-724): prints key NAMES and presence/absence ONLY —
# never values. Bucket/table names are resource IDs, not secrets, and are
# printed (they come from the committed environment registry anyway).
#
# Usage: bash core/scripts/bring-up-doctor.sh
#
# Sections: host/tooling, git, environment registry, .env.local key presence,
# agent dirs + local-paths.conf, session state, daemon, own-cloud probe,
# line-ending spot-check, summary.

set -uo pipefail   # fail-soft by design: every probe guards its own failure

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$ROOT" || { echo "FATAL: cannot cd to repo root $ROOT"; exit 0; }

PASS=(); WARN=(); FAIL=()
ok()   { echo "  [ OK ] $*"; PASS+=("$*"); }
warn() { echo "  [WARN] $*"; WARN+=("$*"); }
bad()  { echo "  [FAIL] $*"; FAIL+=("$*"); }
hr()   { echo; echo "== $* =="; }

# Python launcher: Windows uses `py -3`, Linux `python3`. Report which works.
PY=""
if command -v py >/dev/null 2>&1 && py -3 -c "pass" >/dev/null 2>&1; then PY="py -3"
elif command -v python3 >/dev/null 2>&1 && python3 -c "pass" >/dev/null 2>&1; then PY="python3"
fi

echo "======================================================================"
echo " BRING-UP DOCTOR — $(hostname 2>/dev/null || echo unknown-host) — $(date +%Y-%m-%dT%H:%M:%S)"
echo " repo root: $ROOT"
echo "======================================================================"

# --- 1. Host + tooling ------------------------------------------------------
hr "host + tooling"
echo "  uname: $(uname -a 2>/dev/null | cut -c1-100)"
echo "  bash : $BASH_VERSION"
if command -v git >/dev/null 2>&1; then ok "git $(git --version | awk '{print $3}')"; else bad "git not found"; fi
if [ -n "$PY" ]; then ok "python via '$PY' ($($PY -c 'import sys;print(sys.version.split()[0])' 2>/dev/null))"; else bad "no working python launcher (tried 'py -3' and 'python3')"; fi
if [ -n "$PY" ]; then
  $PY -c "import yaml"  >/dev/null 2>&1 && ok "pyyaml importable" || bad "pyyaml MISSING (pip install pyyaml)"
  $PY -c "import boto3" >/dev/null 2>&1 && ok "boto3 importable"  || warn "boto3 missing (needed only for own-cloud backend)"
fi

# --- 2. Git state -----------------------------------------------------------
hr "git"
if git rev-parse --git-dir >/dev/null 2>&1; then
  BR="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
  echo "  branch: $BR @ $(git rev-parse --short HEAD 2>/dev/null)  ($(git log -1 --format=%cd --date=format:'%Y-%m-%d %H:%M' 2>/dev/null))"
  echo "  remote: $(git remote get-url origin 2>/dev/null || echo '(no origin)')"
  if GIT_TERMINAL_PROMPT=0 git ls-remote --heads origin >/dev/null 2>&1; then
    ok "origin reachable (authenticated fetch works)"
    git fetch origin "$BR" --quiet 2>/dev/null || true
    if git rev-parse --verify "origin/$BR" >/dev/null 2>&1; then
      LR="$(git rev-list --left-right --count "origin/$BR...$BR" 2>/dev/null || echo '? ?')"
      echo "  divergence vs origin/$BR: behind=$(echo "$LR" | awk '{print $1}') ahead=$(echo "$LR" | awk '{print $2}')"
    fi
  else
    bad "origin NOT reachable (network/credentials) — fresh box cannot fetch framework updates"
  fi
else
  bad "not a git repo"
fi

# --- 3. Environment registry ------------------------------------------------
hr "environment registry"
ENV_ID=""
if [ -f .env.local ]; then
  ENV_ID="$(grep -E '^\s*ENVIRONMENT_ID=' .env.local 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\r\n" ')"
fi
if [ -n "$ENV_ID" ]; then
  echo "  ENVIRONMENT_ID=$ENV_ID"
  REG="core/config/environments/${ENV_ID}.yaml"
  if [ -f "$REG" ]; then
    BACKEND="$(grep -E '^backend:' "$REG" | awk '{print $2}' | tr -d '\r')"
    ok "registry entry exists: $REG (backend: ${BACKEND:-unset})"
    [ "${BACKEND:-}" = "own-cloud" ] && grep -E '^(bucket|sessions_table|lock_table|region):' "$REG" | sed 's/^/    /'
  else
    bad "NO registry file $REG — ENVIRONMENT_ID points at nothing. Available: $(ls core/config/environments/ 2>/dev/null | tr '\n' ' ')"
    BACKEND=""
  fi
else
  bad "ENVIRONMENT_ID not set in .env.local — daemon cannot derive storage wiring (registry pattern, g-328-11)"
  BACKEND=""
fi
# legacy override detection
if [ -f .env.local ] && grep -qE '^\s*STORAGE_(BACKEND|S3_BUCKET|DDB_)' .env.local 2>/dev/null; then
  warn "legacy STORAGE_* vars present in .env.local — they OVERRIDE the registry (deprecation path); remove unless intentional"
fi

# commons egress dial -- the world-contract T3 gate (g-368-09, first live consumer).
# Resolved through the canonical SSOT in _paths.py, NEVER a bare shell env read:
# _paths also sources .env.local, so a shell-side ${COMMONS_POLICY:-private} would
# miss a configured value and print a confident wrong answer (guard-3726).
if [ -n "$PY" ]; then
  CP="$(PYTHONPATH="$SCRIPT_DIR" $PY -c 'import _paths;print(_paths.COMMONS_POLICY)' 2>/dev/null)"
  CP_RAW=""
  [ -f .env.local ] && CP_RAW="$(grep -E '^\s*COMMONS_POLICY=' .env.local 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\r\n" ' | tr '[:upper:]' '[:lower:]')"
  if [ -z "$CP" ]; then
    warn "COMMONS_POLICY unresolvable (could not import _paths) -- egress posture UNKNOWN"
  elif [ -z "$CP_RAW" ]; then
    ok "COMMONS_POLICY=$CP (undeclared -> fail-closed default; nothing crosses to the commons)"
  elif [ "$CP_RAW" != "$CP" ]; then
    bad "COMMONS_POLICY declared '$CP_RAW' but RESOLVES to '$CP' -- unrecognized value fails closed (world-contract.md Rule 4). The dial is NOT doing what .env.local says."
  else
    case "$CP" in
      private)   ok   "COMMONS_POLICY=private (declared) -- no knowledge egress" ;;
      selective) warn "COMMONS_POLICY=selective -- generalized patterns are declared to leave this world (verify intended)" ;;
      public)    warn "COMMONS_POLICY=public -- full knowledge tree declared exposed via read API (verify intended)" ;;
    esac
  fi
fi

# --- 4. .env.local key presence (names only, never values) -------------------
hr ".env.local key presence"
if [ -f .env.local ]; then
  ok ".env.local exists"
  KEYS="$(grep -E '^\s*[A-Za-z_][A-Za-z0-9_]*=' .env.local 2>/dev/null | cut -d= -f1 | sed 's/^\s*//' | sort -u | tr '\n' ' ')"
  echo "  keys present: ${KEYS:-'(none)'}"
  for k in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY; do
    if echo " $KEYS " | grep -q " $k "; then ok "$k present"; else
      if [ "${BACKEND:-}" = "own-cloud" ]; then bad "$k ABSENT — own-cloud backend cannot authenticate (provision-from-vault.sh incomplete?)"; else warn "$k absent (fine for local backend)"; fi
    fi
  done
else
  bad ".env.local MISSING — no environment id, no credentials (run the vault provisioning step)"
fi

# --- 5. Agent dirs + local-paths.conf ----------------------------------------
hr "agents"
FOUND_AGENT=0
for d in agents/*/; do
  [ -d "$d" ] || continue
  a="$(basename "$d")"
  FOUND_AGENT=1
  lp="no local-paths.conf"; st="no agent-state"; md="no agent-mode"
  if [ -f "$d/local-paths.conf" ]; then
    lp="local-paths.conf ok"
    while IFS='=' read -r k v; do
      case "$k" in WORLD_DIR|META_DIR)
        v="$(echo "$v" | tr -d '\r')"
        [ -d "$v" ] || lp="$k -> MISSING DIR ($v)";;
      esac
    done < "$d/local-paths.conf"
  fi
  [ -f "$d/session/agent-state" ] && st="state=$(tr -d '\r\n' < "$d/session/agent-state")"
  [ -f "$d/session/agent-mode" ]  && md="mode=$(tr -d '\r\n' < "$d/session/agent-mode")"
  echo "  $a: $lp | $st | $md"
  case "$lp" in *MISSING*) bad "agent $a: $lp";; esac
done
[ "$FOUND_AGENT" = 0 ] && warn "no agent dirs under agents/ (agent creation did not persist?)"

# --- 6. Daemon ---------------------------------------------------------------
hr "daemon (mind-api)"
DPID=""; DPORT=""
[ -f mind_api/state/daemon.pid ]  && DPID="$(tr -d '\r\n ' < mind_api/state/daemon.pid)"
[ -f mind_api/state/daemon.port ] && DPORT="$(tr -d '\r\n ' < mind_api/state/daemon.port)"
echo "  pid file: ${DPID:-absent} | port file: ${DPORT:-absent}"
# Health endpoint is the AUTHORITATIVE liveness signal (kill -0 can't see
# native-Windows pids from git-bash's MSYS pid namespace — false negative).
HEALTHY=0
if [ -n "$DPORT" ] && command -v curl >/dev/null 2>&1; then
  if curl -sf --max-time 5 "http://127.0.0.1:${DPORT}/v1/admin/health" >/dev/null 2>&1; then
    ok "health endpoint responds on :$DPORT (daemon alive)"
    HEALTHY=1
  else
    warn "health endpoint NOT responding on :$DPORT"
  fi
fi
if [ "$HEALTHY" = 0 ]; then
  if [ -n "$DPID" ] && kill -0 "$DPID" 2>/dev/null; then ok "daemon process $DPID alive (but health probe failed/skipped above)"
  else warn "daemon not confirmably alive (wrappers auto-spawn on demand — only a problem if spawn fails)"; fi
fi
if [ -f mind_api/state/daemon.log ]; then
  echo "  --- last 15 daemon.log lines ---"
  tail -15 mind_api/state/daemon.log | sed 's/^/    /'
else
  warn "no mind_api/state/daemon.log — daemon has never started here"
fi

# --- 7. Own-cloud reachability (read-only) -----------------------------------
hr "own-cloud probe"
if [ "${BACKEND:-}" = "own-cloud" ] && [ -n "$PY" ]; then
  REG="core/config/environments/${ENV_ID}.yaml"
  PROBE_OUT="$(DOCTOR_REG="$REG" $PY - <<'PYEOF' 2>&1
import os, sys
try:
    import yaml, boto3
    from botocore.config import Config
    reg = yaml.safe_load(open(os.environ["DOCTOR_REG"], encoding="utf-8"))
    # creds: from env or .env.local (daemon normally loads these; mimic minimally)
    if not os.environ.get("AWS_ACCESS_KEY_ID") and os.path.exists(".env.local"):
        for line in open(".env.local", encoding="utf-8", errors="replace"):
            line = line.strip()
            if line.startswith(("AWS_ACCESS_KEY_ID=", "AWS_SECRET_ACCESS_KEY=", "AWS_SESSION_TOKEN=")):
                k, _, v = line.partition("=")
                os.environ.setdefault(k, v.strip().strip('"'))
    s3 = boto3.client("s3", region_name=reg.get("region"),
                      config=Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 1}))
    s3.head_bucket(Bucket=reg["bucket"])
    print("OK head_bucket " + reg["bucket"])
except Exception as e:
    print("FAIL " + type(e).__name__ + ": " + str(e)[:160])
PYEOF
)"
  case "$PROBE_OUT" in
    OK*)   ok "own-cloud S3 reachable: $PROBE_OUT";;
    *)     bad "own-cloud S3 probe failed: $PROBE_OUT";;
  esac
elif [ "${BACKEND:-}" = "local" ]; then
  ok "backend is 'local' — no cloud probe needed"
else
  warn "own-cloud probe skipped (backend='${BACKEND:-unknown}', python='${PY:-none}')"
fi

# --- 8. Line-ending spot-check (fresh-clone CRLF class, g-115-869) ------------
hr "line endings"
for f in core/scripts/_runtime.sh core/githooks/pre-commit; do
  [ -f "$f" ] || continue
  if grep -q $'\r' "$f" 2>/dev/null; then bad "$f contains CRLF — bash will fail rc=127 (re-clone with .gitattributes honored, or dos2unix)"; else ok "$f is LF-clean"; fi
done

# --- 9. Summary ---------------------------------------------------------------
hr "SUMMARY"
echo "  PASS: ${#PASS[@]}   WARN: ${#WARN[@]}   FAIL: ${#FAIL[@]}"
if [ "${#FAIL[@]}" -gt 0 ]; then
  echo "  Failures (fix top-to-bottom — earlier layers gate later ones):"
  for f in "${FAIL[@]}"; do echo "    - $f"; done
else
  echo "  No hard failures found — if /start still fails, capture its exact"
  echo "  output and the last 50 lines of mind_api/state/daemon.log."
fi
echo "======================================================================"
echo " Paste this ENTIRE report back to the fleet (board, email, or chat)."
echo "======================================================================"
exit 0
