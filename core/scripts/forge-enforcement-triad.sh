#!/usr/bin/env bash
# CLI for the enforcement-triad scaffold (gap-035, ).
#
# The generator itself (forge_enforcement_triad.py) is PURE and does no file
# I/O -- all writing happens here. That split is deliberate: it is what lets
# the whole emitter be tested on fixtures with no filesystem.
#
# DRY-RUN IS THE DEFAULT. --write is required to touch the tree, and an
# existing file is NEVER overwritten (a triad is authored once; a second run
# that clobbered a hand-implemented predicate would destroy the only part of
# the shape a generator cannot reproduce).
set -uo pipefail

usage() {
  cat <<'USAGE'
Usage: forge-enforcement-triad.sh --name <kebab-name> --tool <Bash|Write|Edit|ScheduleWakeup>
                                  --purpose "<one line>" [--goal-id g-NNN-NN]
                                  [--rule-name <kebab>] [--write]

Emits the 6-file enforcement triad plus the .claude/settings.json hook entry.
Default is a DRY RUN (prints the manifest and the hook entry, writes nothing).

  --write        actually create the files; refuses to overwrite any that exist
  --rule-name    override the rule filename (default: <name>-pattern)

AFTER --write, three things remain and none of them are mechanical:
  1. implement decide() in core/scripts/_<slug>_predicate.py  (it raises until you do)
  2. replace the placeholder cases in the emitted test file with real ones
  3. paste the printed hook entry into .claude/settings.json PreToolUse
Until (1) is done the gate FAILS OPEN and reports every payload clean.
USAGE
}

NAME=""; TOOL=""; PURPOSE=""; GOAL=""; RULE=""; WRITE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --name) NAME="${2:-}"; shift 2 ;;
    --tool) TOOL="${2:-}"; shift 2 ;;
    --purpose) PURPOSE="${2:-}"; shift 2 ;;
    --goal-id) GOAL="${2:-}"; shift 2 ;;
    --rule-name) RULE="${2:-}"; shift 2 ;;
    --write) WRITE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [ -z "$NAME" ] || [ -z "$TOOL" ] || [ -z "$PURPOSE" ]; then
  echo "ERROR: --name, --tool and --purpose are all required" >&2
  usage >&2; exit 2
fi

source "$(cd "$(dirname "$0")" && pwd)/_paths.sh" || exit 1

NAME="$NAME" TOOL="$TOOL" PURPOSE="$PURPOSE" GOAL="$GOAL" RULE="$RULE" \
WRITE="$WRITE" PROJECT_ROOT="$PROJECT_ROOT" python3 - <<'PYEOF'
import os, sys, json
sys.path.insert(0, os.path.join(os.environ["PROJECT_ROOT"], "core", "scripts"))
import forge_enforcement_triad as F

spec = {"name": os.environ["NAME"], "tool": os.environ["TOOL"],
        "purpose": os.environ["PURPOSE"]}
if os.environ.get("GOAL"): spec["goal_id"] = os.environ["GOAL"]
if os.environ.get("RULE"): spec["rule_name"] = os.environ["RULE"]

try:
    files = F.render(spec)
except F.SpecError as e:
    print(f"ERROR: {e}", file=sys.stderr); sys.exit(2)

root = os.environ["PROJECT_ROOT"]
write = os.environ["WRITE"] == "1"
existing = [p for p in files if os.path.exists(os.path.join(root, p))]

if existing and write:
    print("REFUSING TO WRITE -- these already exist:", file=sys.stderr)
    for p in existing: print(f"  {p}", file=sys.stderr)
    print("A triad is authored once. Delete them deliberately, or pick a new "
          "--name; overwriting would destroy a hand-implemented predicate.",
          file=sys.stderr)
    sys.exit(3)

for path, content in sorted(files.items()):
    if write:
        dest = os.path.join(root, path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        if path.endswith(".sh"):
            os.chmod(dest, 0o755)
    mark = "WROTE" if write else "would write"
    flag = "  <-- EXISTS" if (path in existing and not write) else ""
    print(f"  {mark:11s} {len(content):6d}B  {path}{flag}")

print()
print("--- .claude/settings.json PreToolUse entry (apply deliberately) ---")
print(F.settings_hook_entry(spec))
if not write:
    print()
    print("DRY RUN -- nothing written. Re-run with --write.")
else:
    print()
    print("NEXT (none of these are mechanical):")
    print(f"  1. implement decide() in core/scripts/_{F.slug_of(spec['name'])}_predicate.py")
    print( "  2. replace the placeholder cases in the emitted test file")
    print( "  3. paste the hook entry above into .claude/settings.json")
    print( "  Until (1) lands the gate FAILS OPEN and reports every payload clean.")
PYEOF
