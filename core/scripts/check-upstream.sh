#!/usr/bin/env bash
# check-upstream.sh — Fail-closed downstream listener for framework releases.
#
# Runs at a SEED or DOWNSTREAM repo: checks whether the upstream feed has newer
# releases, classifies them (chain-walk: any breaking in the chain => the whole
# update is breaking), enforces the frontier-invariant, and — for NON-BREAKING
# updates with --auto-apply — runs the agent-IDLE gate and takes a pre-update
# snapshot tag. Breaking updates NEVER auto-apply (human approval required).
# The frontier has no upstream — running here is a no-op success.
#
# Usage: bash core/scripts/check-upstream.sh [--auto-apply] [--diagnose] [--dry-run]
#
# Exit codes (the API):
#   0  up to date / non-breaking available (report-only) / auto-apply prep done
#   1  configuration error (no upstream configured, role not in chain)
#   2  fetch/parse failure — FAIL-CLOSED (treat as potential breaking; M1/CW5)
#   3  auto-apply prep failed (rolled back)
#   4  breaking update pending (human required)
#   5  invariant violation (local exceeds upstream latest)
#
# CHECK_UPSTREAM_URL env overrides the resolved upstream feed URL (staging/tests).
# Design: rails A.6 + omni deltas M1/H3 + guardrails CW2/CW5.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_paths.sh" 2>/dev/null || { echo "ERROR: failed to source _paths.sh" >&2; exit 1; }
LIB="$SCRIPT_DIR/_release_lib.py"
INIT_PY="$PROJECT_ROOT/mind_api/src/__init__.py"
WORLD="${WORLD_DIR:-${WORLD_PATH:-}}"
OVERLAY="$WORLD/config/compatibility.yaml"
FW_COMPAT="$CONFIG_DIR/compatibility.yaml"

AUTO_APPLY=0; DIAGNOSE=0; DRY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --auto-apply) AUTO_APPLY=1; shift;;
    --diagnose) DIAGNOSE=1; shift;;
    --dry-run) DRY=1; shift;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed 's/^# \?//;/^set -euo/d'; exit 0;;
    *) echo "ERROR: unknown argument: $1" >&2; exit 1;;
  esac
done

say() { echo "[check-upstream] $*"; }
fail() { echo "[check-upstream] ERROR: $*" >&2; exit 1; }

# --- Step 1: resolve identity (self role + upstream role + upstream URL) ----
[[ -f "$OVERLAY" ]] || fail "no upstream configured ($OVERLAY missing)"
# `import yaml` is INSIDE the try so a missing pyyaml is handled gracefully
# (prints ERR|, exit 0). The set +e / RC check converts a HARD failure (py -3
# not found, or a future SyntaxError in this source) from a silent set -e death
# into an actionable diagnostic. (review F5)
set +e
ID="$(OVERLAY="$OVERLAY" FW="$FW_COMPAT" py -3 -c '
import os
try:
    import yaml
    ov = yaml.safe_load(open(os.environ["OVERLAY"], encoding="utf-8")) or {}
    fw = yaml.safe_load(open(os.environ["FW"], encoding="utf-8")) or {}
    chain = [c.get("role") for c in (fw.get("promotion_chain") or [])]
    self_role = ov.get("self_role") or ""
    if not self_role or self_role not in chain:
        print("ERR|self_role missing or not in promotion_chain"); raise SystemExit(0)
    i = chain.index(self_role)
    if i == 0:
        print("FRONTIER|" + self_role); raise SystemExit(0)
    up = chain[i - 1]
    url = (((ov.get("sources") or {}).get(up) or {}).get("releases_url")) or ""
    print(f"{self_role}|{up}|{url}")
except SystemExit:
    raise
except Exception as e:
    print("ERR|" + str(e).replace("|", " "))
' 2>/dev/null)"
IDPY_RC=$?
set -e
[[ $IDPY_RC -eq 0 ]] || fail "identity-resolution python failed (exit $IDPY_RC) -- is 'py -3' + pyyaml available? cannot resolve upstream identity"

IFS='|' read -r F1 F2 F3 <<< "$ID"
if [[ "$F1" == "FRONTIER" ]]; then say "this repo is the frontier ($F2) — no upstream to check"; exit 0; fi
if [[ "$F1" == "ERR" || -z "$F1" ]]; then fail "cannot resolve upstream identity: ${F2:-unknown}"; fi
SELF_ROLE="$F1"; UPSTREAM_ROLE="$F2"; UPSTREAM_URL="${CHECK_UPSTREAM_URL:-$F3}"
[[ -n "$UPSTREAM_URL" ]] || fail "no releases_url configured for upstream role '$UPSTREAM_ROLE'"
say "role=$SELF_ROLE  upstream=$UPSTREAM_ROLE"

# --- Step 2: local version (from disk) ------------------------------------
LOCAL="$(grep -E '^__version__' "$INIT_PY" | sed -E 's/.*"([^"]+)".*/\1/' || true)"
[[ -n "$LOCAL" ]] || fail "could not read local __version__ from $INIT_PY"
say "local version: $LOCAL"

# --- Step 3: fetch upstream RELEASES.json (FAIL-CLOSED — CW5) --------------
TMP="$(mktemp 2>/dev/null || echo "$PROJECT_ROOT/.upstream-releases.tmp")"
# Capture curl's stderr (2>&1) instead of discarding it, so the FAIL-CLOSED
# message carries the DNS/TLS/timeout/HTTP root cause (--show-error emits it).
# On success the body goes to $TMP via -o, so CURL_ERR stays empty. (review omni-fu3)
if ! CURL_ERR="$(curl --fail --silent --show-error --max-time 30 "$UPSTREAM_URL" -o "$TMP" 2>&1)"; then
  rm -f "$TMP"
  echo "[check-upstream] FAIL-CLOSED: cannot fetch upstream RELEASES.json from $UPSTREAM_URL." >&2
  [[ -n "$CURL_ERR" ]] && echo "[check-upstream] curl: $CURL_ERR" >&2
  echo "[check-upstream] Treating as a POTENTIAL BREAKING CHANGE — manual review required." >&2
  exit 2
fi

# --- Step 4+5: classify (parse-or-fail M1 + chain-walk) -------------------
set +e
CLS="$(UPSTREAM_RELEASES_PATH="$TMP" LOCAL_VERSION="$LOCAL" py -3 "$LIB" classify-chain)"
CLS_RC=$?
set -e
rm -f "$TMP"
if [[ $CLS_RC -eq 2 ]]; then
  echo "[check-upstream] FAIL-CLOSED: upstream RELEASES.json malformed/unparseable." >&2
  echo "[check-upstream] Treating as a POTENTIAL BREAKING CHANGE — manual review required." >&2
  exit 2
elif [[ $CLS_RC -ne 0 ]]; then
  fail "classification error (rc=$CLS_RC): $CLS"
fi
HAS_UPDATES="$(echo "$CLS" | sed -n 's/^HAS_UPDATES=//p')"
BREAKING="$(echo "$CLS"    | sed -n 's/^BREAKING=//p')"
COUNT="$(echo "$CLS"       | sed -n 's/^COUNT=//p')"
LATEST="$(echo "$CLS"      | sed -n 's/^LATEST=//p')"
UPSTREAM_NEWEST="$(echo "$CLS" | sed -n 's/^UPSTREAM_NEWEST=//p')"
VERSIONS="$(echo "$CLS"    | sed -n 's/^VERSIONS=//p')"
RECIPES="$(echo "$CLS"     | sed -n 's/^UPGRADE_RECIPES=//p')"

# --- Step 8: frontier-invariant (CW2) — local must not exceed upstream -----
if [[ -n "$UPSTREAM_NEWEST" ]]; then
  set +e; CMP_UP="$(py -3 "$LIB" compare "$LOCAL" "$UPSTREAM_NEWEST")"; CMP_RC=$?; set -e
  if [[ $CMP_RC -eq 0 && "$CMP_UP" == "1" ]]; then
    echo "[check-upstream] INVARIANT VIOLATION (CW2): local $LOCAL exceeds upstream $UPSTREAM_ROLE latest $UPSTREAM_NEWEST" >&2
    exit 5
  fi
fi

# --- --diagnose: report state and exit ------------------------------------
if [[ $DIAGNOSE -eq 1 ]]; then
  say "DIAGNOSE: local=$LOCAL upstream_newest=$UPSTREAM_NEWEST has_updates=$HAS_UPDATES breaking=$BREAKING count=$COUNT"
  [[ "$HAS_UPDATES" == "1" ]] && say "DIAGNOSE: pending versions: $VERSIONS"
  PRE="$(git -C "$PROJECT_ROOT" tag -l 'pre-update-*' 2>/dev/null | tail -1 || true)"
  [[ -n "$PRE" ]] && say "DIAGNOSE: latest pre-update snapshot tag: $PRE (rollback: git reset --hard $PRE)"
  exit 0
fi

# --- Step 4 (cont): up to date --------------------------------------------
if [[ "$HAS_UPDATES" == "0" ]]; then
  say "up to date (local $LOCAL == upstream $UPSTREAM_ROLE $UPSTREAM_NEWEST)"
  exit 0
fi
say "$COUNT update(s) available: $VERSIONS (latest $LATEST) — breaking=$BREAKING"

# --- Step 6: BREAKING -> human required (never auto-apply) -----------------
if [[ "$BREAKING" == "1" ]]; then
  echo "[check-upstream] BREAKING update(s) pending. Run these recipes IN ORDER after review:"
  echo "  $RECIPES"
  [[ $AUTO_APPLY -eq 1 ]] && echo "[check-upstream] --auto-apply is IGNORED for breaking changes; human approval required."
  exit 4
fi

# --- Step 7: NON-BREAKING ---------------------------------------------------
if [[ $AUTO_APPLY -ne 1 ]]; then
  say "non-breaking update(s) available. Re-run with --auto-apply to prep, or merge the upstream promotion PR."
  exit 0
fi

# --auto-apply (non-breaking): agent-IDLE gate (FAIL-CLOSED) + pre-update snapshot.
# Iterate agent DIRS (a local-paths.conf marks a real agent), NOT agent-state
# files: an agent-state glob silently SKIPS a RUNNING agent whose state file is
# missing/corrupted, passing the gate. Treat a missing/unreadable state as
# UNKNOWN and REFUSE — only an explicitly-non-RUNNING readable state is safe.
# (review omni-fu2)
for adir in "$(agents_root)"/*/; do
  [[ -f "$adir/local-paths.conf" ]] || continue   # not a real agent dir
  sf="$adir/session/agent-state"
  if [[ ! -r "$sf" ]]; then
    fail "agent '$(basename "$adir")' has no readable agent-state ($sf) — UNKNOWN state; refusing auto-apply. Stop all agents and verify state, then retry."
  fi
  st="$(cat "$sf" 2>/dev/null || true)"
  [[ "$st" == "RUNNING" ]] && fail "agent '$(basename "$adir")' is RUNNING — stop all agents before applying an update"
done
if [[ $DRY -eq 1 ]]; then
  say "[dry-run] would tag pre-update-$LATEST, then apply non-breaking update to $LATEST (merge upstream promotion PR)"
  say "[dry-run] OK"
  exit 0
fi
PRE_TAG="pre-update-$LATEST"
if ! git -C "$PROJECT_ROOT" tag -f "$PRE_TAG" >/dev/null 2>&1; then
  # Documented API: exit 3 = auto-apply prep failed (rolled back). Use an
  # explicit exit 3 here (NOT fail(), which exits 1) so the code honors the
  # exit-code contract at the top of this file. (review F1)
  echo "[check-upstream] ERROR: could not create pre-update snapshot tag $PRE_TAG -- auto-apply prep failed; nothing applied." >&2
  exit 3
fi
say "pre-update snapshot tagged: $PRE_TAG"
# The actual apply for a downstream is merging the upstream promotion PR (or a
# git pull of the published branch) — a human/CI action, consistent with the
# never-auto-merge philosophy (promote-to-upstream.sh also never merges). The
# safety prep (IDLE gate + snapshot) is done; finish by merging upstream.
say "ready to apply non-breaking update to $LATEST. Merge the upstream promotion PR / pull the published branch."
say "rollback if needed: git reset --hard $PRE_TAG"
exit 0
