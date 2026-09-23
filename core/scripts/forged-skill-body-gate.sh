#!/usr/bin/env bash
# Forged-skill BODY gate ( item 3) — refuse to register a forged skill
# whose body is not present and loadable at the path the runtime will load.
#
# THE DEFECT THIS CATCHES (measured 2026-09-05 11:55Z on a downstream clone):
# /forge-skill wrote the registry row into world/forged-skills.yaml, the skill
# DIRECTORY was empty, a test goal was filed to exercise a skill with no body,
# two "forge-skill,complete" board posts went out, and THE MODEL DECLARED
# SUCCESS. Nothing in the procedure ever asked whether the thing being
# registered exists. That is guard-2242's class exactly — a pointer field is
# not evidence until its REFERENT is confirmed to exist — with the registry row
# as the pointer and the SKILL.md body as the absent referent. rb-10227.
#
# WHY A GATE RATHER THAN A SKILL.md SENTENCE: guard-399. A new "the LLM must
# check X at step N" instruction that has no bash gate behind it does not fire.
# Its audit recipe is the tell — grep the script name across core/ + .claude/;
# if it appears only in its own test, it was never wired. So this script is
# invoked from forge-skill Step 4 (before the registry write) AND from its
# Return Protocol (before the forge may report done).
#
# WHAT IT DELIBERATELY DOES *NOT* CHECK, and why (guard-2335): whether the
# runtime's CATALOG lists the skill. 's description asks the Return
# Protocol to "verify the body loads (catalog lists it)", and on Claude Code
# that is STRUCTURALLY UNSATISFIABLE in the forging session — skill
# descriptions are loaded at STARTUP, so a skill forged mid-session cannot
# appear in its own session's catalog no matter how correct the body is. A gate
# asserting catalog presence would therefore refuse every correct forge on
# Claude Code. Disk presence at the load path is the checkable half, and it is
# the half the coach incident actually violated.
#
# EXIT CONTRACT (a CALLABLE gate, not a PreToolUse hook — it must be able to
# refuse, so it does NOT fail open):
#   0  body present and loadable  -> registration may proceed
#   1  REFUSE                     -> body absent, empty, or not loadable
#   2  usage error                -> caller passed no skill name
#
# Override: --override-body-gate "<justification>" (audited to stderr). Use
# only for a body that lives somewhere this gate cannot see; never to get past
# a genuinely missing file.

set -u

PREFIX="[forged-skill-body-gate]"
SKILL_NAME=""
OVERRIDE=""
ROOT=""

while [ $# -gt 0 ]; do
    case "$1" in
        --skill) SKILL_NAME="${2:-}"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --project-root) ROOT="${2:-}"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --override-body-gate) OVERRIDE="${2:-}"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        -h|--help)
            echo "Usage: forged-skill-body-gate.sh --skill <name> [--project-root <path>] [--override-body-gate <why>]" >&2
            exit 2 ;;
        *)
            if [ -z "$SKILL_NAME" ]; then SKILL_NAME="$1"; shift
            else echo "$PREFIX unknown argument '$1'" >&2; exit 2; fi ;;
    esac
done

if [ -z "$SKILL_NAME" ]; then
    echo "$PREFIX Error: --skill <name> is required." >&2
    echo "  Usage: forged-skill-body-gate.sh --skill <name> [--project-root <path>]" >&2
    exit 2
fi

# Reject a name that would escape the skills dir. A registry row naming
# "../../etc" must not cause this gate to stat something outside the tree.
case "$SKILL_NAME" in
    */*|.|..|"") echo "$PREFIX Error: skill name '$SKILL_NAME' is not a bare directory name." >&2; exit 2 ;;
esac

if [ -z "$ROOT" ]; then
    ROOT="$(cd "$(dirname "$0")/../.." 2>/dev/null && pwd)" || ROOT="."
fi

BODY="$ROOT/.claude/skills/$SKILL_NAME/SKILL.md"

if [ -n "$OVERRIDE" ]; then
    echo "$PREFIX OVERRIDE applied for '$SKILL_NAME': $OVERRIDE" >&2
    echo "$PREFIX (audit) body path checked was: $BODY" >&2
    exit 0
fi

_refuse() {
    echo "$PREFIX REFUSE: $1" >&2
    echo "  skill : $SKILL_NAME" >&2
    echo "  body  : $BODY" >&2
    echo "  Do NOT append a row to world/forged-skills.yaml and do NOT report the" >&2
    echo "  forge complete. A registry row pointing at an absent body is a PHANTOM" >&2
    echo "  registration (guard-2242, rb-10227): it advertises a trigger fleet-wide" >&2
    echo "  that dispatches to nothing, and every box reads it as a real skill." >&2
    echo "  Write the body first, then re-run this gate." >&2
    exit 1
}

[ -e "$BODY" ] || _refuse "no SKILL.md at the path the runtime loads"
[ -f "$BODY" ] || _refuse "the load path exists but is not a regular file"
[ -s "$BODY" ] || _refuse "SKILL.md is present but EMPTY (0 bytes)"

# Loadable, not merely present. parse_front_matter (core/scripts/_skill_md.py)
# anchors on '---' at line 1; a body whose front matter does not open there is
# invisible to every consumer, which is the failure domain-free-examples.md
# records for 7 SKILL.md files on 2026-05-11 (rb-840 / guard-518).
FIRST_LINE="$(head -n 1 "$BODY" 2>/dev/null | tr -d '\r')"
[ "$FIRST_LINE" = "---" ] || _refuse "front matter does not open with '---' on line 1, so no consumer can parse it (rb-840/guard-518)"

# Claude Code discovers a skill by its front-matter `description`. A body with
# none is loadable but can never be selected, which is the same dead end one
# layer down.
grep -q "^description:" "$BODY" 2>/dev/null || _refuse "front matter carries no 'description:' key — the runtime has nothing to match a trigger against"

BYTES="$(wc -c < "$BODY" 2>/dev/null | tr -d ' ')"
echo "$PREFIX OK: '$SKILL_NAME' body present and loadable ($BYTES bytes) at $BODY"
exit 0
