#!/usr/bin/env bash
# audit-paths.sh — Audit and optionally prune .claude/settings.local.json write allowlist.
#
# L2 defense (per  / path-resolution.md): generates the canonical set of
# Write/Edit/MultiEdit permission rules from each agent's local-paths.conf, diffs
# against the current .claude/settings.local.json allowlist, and reports (or applies)
# changes that remove historical/orphaned path rules.
#
# Usage:
#   audit-paths.sh            # dry-run: print canonical allowlist + diff
#   audit-paths.sh --apply    # write canonical allowlist to settings.local.json
#   audit-paths.sh --json     # dry-run, emit machine-readable JSON
#
# Exit: 0 always (observability, not enforcement — see rb-226).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"

APPLY=false
EMIT_JSON=false
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=true ;;
    --json)  EMIT_JSON=true ;;
    *) echo "Unknown arg: $arg" >&2; exit 1 ;;
  esac
done

SETTINGS_FILE=".claude/settings.local.json"
if [[ ! -f "$SETTINGS_FILE" ]]; then
  echo "WARN: $SETTINGS_FILE not found" >&2
  exit 0
fi

# Collect canonical write roots from every agent/local-paths.conf.
# Repo root is always canonical (it's where the framework lives).
declare -A canonical_roots
canonical_roots["$PROJECT_ROOT"]=1

shopt -s nullglob
# Glob via agents_root() (PROJECT_ROOT/$AGENTS_PARENT_DIR) so the two-level
# agents/<name>/local-paths.conf layout is matched. A bare */local-paths.conf is
# one level relative to PROJECT_ROOT and matched ZERO confs under
# AGENTS_PARENT_DIR=agents, silently collapsing canonical_roots to {PROJECT_ROOT}
# and skipping every agent WORLD/META root in the permission audit (4).
for conf in "$(agents_root)"/*/local-paths.conf; do
  while IFS='=' read -r key value; do
    value="${value%%$'\r'}"   # strip trailing CR on Windows
    case "$key" in
      WORLD_PATH|META_PATH)
        [[ -n "$value" ]] && canonical_roots["$value"]=1
        ;;
    esac
  done < "$conf"
done
shopt -u nullglob

# Build canonical allow entries (Edit/MultiEdit/Write for each root).
# Path format matches the existing settings.local.json style: //C:/path/**
canonical_allow=()
for root in "${!canonical_roots[@]}"; do
  # Normalize to Claude Code's //DRIVE:/... allowlist format.
  # Inputs may be MSYS2 style (/c/<WORKSPACE>/...), Windows (C:/Users/...),
  # or already-normalized (//C:/...). Output always //C:/....
  case "$root" in
    //[a-zA-Z]:/*)
      norm="$root"  # already normalized
      ;;
    /[a-zA-Z]/*)
      # MSYS2 form: /c/<WORKSPACE> -> //C:/<WORKSPACE>
      drive="${root:1:1}"
      rest="${root:2}"
      norm="//$(echo "$drive" | tr '[:lower:]' '[:upper:]'):$rest"
      ;;
    [a-zA-Z]:/*)
      # Windows form: C:/Users -> //C:/Users
      drive="${root:0:1}"
      rest="${root:2}"
      norm="//$(echo "$drive" | tr '[:lower:]' '[:upper:]'):$rest"
      ;;
    *)
      norm="$root"
      ;;
  esac
  canonical_allow+=("Edit($norm/**)")
  canonical_allow+=("MultiEdit($norm/**)")
  canonical_allow+=("Write($norm/**)")
done

# Non-path allow rules that should be preserved unchanged.
preserved_allow=(
  "Bash(gh *)"
  "Bash(*)"
  "Read(*)"
  "Glob(*)"
  "Grep(*)"
  "WebSearch(*)"
  "WebFetch(*)"
)

# Build the canonical allowlist (order: preserved first, then path rules sorted).
full_allow=()
for rule in "${preserved_allow[@]}"; do full_allow+=("$rule"); done
# Sort path rules for deterministic output
IFS=$'\n' sorted_paths=($(printf '%s\n' "${canonical_allow[@]}" | sort))
unset IFS
for rule in "${sorted_paths[@]}"; do full_allow+=("$rule"); done

# Read current allowlist and compute diff.
current_allow_json=$(python3 -c "
import json, sys
with open('$SETTINGS_FILE', 'r', encoding='utf-8') as f:
    s = json.load(f)
print(json.dumps(s.get('permissions', {}).get('allow', []), ensure_ascii=False))
")

diff_out=$(python3 -c "
import json, sys
current = json.loads('''$current_allow_json''')
canonical = $(printf '%s\n' "${full_allow[@]}" | python3 -c 'import sys,json; print(json.dumps([l.rstrip() for l in sys.stdin if l.strip()]))')
to_remove = [r for r in current if r not in canonical]
to_add    = [r for r in canonical if r not in current]
print(json.dumps({
  'current_count': len(current),
  'canonical_count': len(canonical),
  'to_remove': to_remove,
  'to_add': to_add,
  'canonical': canonical,
}, indent=2, ensure_ascii=False))
")

if [[ "$EMIT_JSON" == "true" ]]; then
  echo "$diff_out"
  exit 0
fi

echo "=== audit-paths.sh ==="
echo "Current agents with local-paths.conf:"
for conf in "$(agents_root)"/*/local-paths.conf; do echo "  $conf"; done
echo ""
echo "Canonical write roots (derived from configs + PROJECT_ROOT):"
for root in "${!canonical_roots[@]}"; do echo "  $root"; done
echo ""
echo "Diff vs .claude/settings.local.json:"
echo "$diff_out" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(f'  current_count: {d[\"current_count\"]}')
print(f'  canonical_count: {d[\"canonical_count\"]}')
if d['to_remove']:
    print('  TO REMOVE:')
    for r in d['to_remove']: print(f'    - {r}')
else:
    print('  TO REMOVE: (none — allowlist already narrowed)')
if d['to_add']:
    print('  TO ADD:')
    for r in d['to_add']: print(f'    + {r}')
else:
    print('  TO ADD: (none)')
"
echo ""

if [[ "$APPLY" != "true" ]]; then
  echo "Dry-run only. Re-run with --apply to update $SETTINGS_FILE."
  exit 0
fi

# Apply: rewrite permissions.allow in-place, preserving all other settings fields.
# Write diff JSON to a temp file so the apply-python only needs one quoting level.
tmp_diff=$(mktemp)
printf '%s' "$diff_out" > "$tmp_diff"
AUDIT_SETTINGS="$SETTINGS_FILE" AUDIT_DIFF="$tmp_diff" python3 -c '
import json, os
settings_path = os.environ["AUDIT_SETTINGS"]
diff_path = os.environ["AUDIT_DIFF"]
with open(diff_path, "r", encoding="utf-8") as f:
    diff = json.load(f)
with open(settings_path, "r", encoding="utf-8") as f:
    s = json.load(f)
s.setdefault("permissions", {})["allow"] = diff["canonical"]
with open(settings_path, "w", encoding="utf-8", newline="\n") as f:
    json.dump(s, f, indent=2, ensure_ascii=False)
    f.write("\n")
print(f"Applied canonical allowlist to {settings_path}")
'
rm -f "$tmp_diff"
