#!/usr/bin/env bash
# _argv_strict.sh — shared strict-argv parser for the positional store wrappers.
#
# WHY THIS EXISTS ( / )
# Fifteen wrappers parsed their arguments with a bare `-*) shift;;` arm, which
# SILENTLY DISCARDED any unrecognized flag. So
#     guardrails-update-field.sh <id> <field> --value-file <path>
# slid the PATH into <value> and overwrote the record with rc=0 and no error.
# On 2026-08-01 that replaced guard-1615 (times_active 677, 1400+ chars) with an
# 87-char tmp path; only a mandated read-back caught it. Six of the fifteen
# MUTATE records, so the same shape was live on equally load-bearing stores.
#
# WHY SHARED RATHER THAN INLINED
# Seven call sites exist today, so the helper clears implementation-discipline
# rule 3's two-call-site bar with room to spare, and inlining seven copies of a
# safety guard is precisely the sig-21 "drift between two consumers of shared
# state" class. Sourcing costs one file read and happens BEFORE _runtime.sh, so
# the refusal stays cheap and cannot be masked by a daemon failure.
#
# CONTRACT
#   argv_strict_parse <script-name> <max-positionals> "$@"
# On success, exports:
#   ARGV_POS[]      the positionals, in order (0-indexed)
#   ARGV_VALUE_FILE path given to --value-file, or ""
#   ARGV_VALUE_STDIN 1 if --value-stdin was passed, else 0
# On refusal it prints a diagnostic to stderr and exits 2.
#
# EXIT 2 IS PART OF THE CONTRACT — not merely "non-zero". The wrappers' daemon
# path also exits non-zero on transport failure, so a test asserting `rc != 0`
# stays GREEN with this guard reverted (measured: the reverted unknown-flag path
# exits 1). Callers and tests MUST pin rc == 2 specifically.

argv_strict_usage() {
    local script="$1" maxpos="$2"
    {
        printf 'Usage: %s takes exactly %s positional argument(s).\n' "$script" "$maxpos"
        printf '  Optional: --value-file <path> | --value-stdin  (for long values)\n'
        printf '  There are no other flags; anything else is refused (exit 2).\n'
    } >&2
}

argv_strict_parse() {
    local script="$1" maxpos="$2"
    shift 2

    ARGV_POS=()
    ARGV_VALUE_FILE=""
    ARGV_VALUE_STDIN=0

    while [ $# -gt 0 ]; do
        case "$1" in
            --value-file)
                if [ $# -lt 2 ]; then
                    printf '%s: --value-file requires a path\n' "$script" >&2
                    argv_strict_usage "$script" "$maxpos"; exit 2
                fi
                ARGV_VALUE_FILE="$2"; shift 2;;
            --value-file=*)
                ARGV_VALUE_FILE="${1#--value-file=}"; shift;;
            --value-stdin)
                ARGV_VALUE_STDIN=1; shift;;
            -h|--help)
                argv_strict_usage "$script" "$maxpos"; exit 0;;
            --)
                shift;;
            -*)
                printf "%s: unknown option '%s' — refusing.\n" "$script" "$1" >&2
                printf '  An unrecognized flag used to be silently discarded, which slid the\n' >&2
                printf '  NEXT argument into a value slot and overwrote the record (g-115-4501).\n' >&2
                argv_strict_usage "$script" "$maxpos"; exit 2;;
            *)
                if [ "${#ARGV_POS[@]}" -ge "$maxpos" ]; then
                    printf "%s: unexpected extra argument '%s' (expected %s positional(s)) — refusing.\n" \
                        "$script" "$1" "$maxpos" >&2
                    argv_strict_usage "$script" "$maxpos"; exit 2
                fi
                ARGV_POS+=("$1"); shift;;
        esac
    done
}

# argv_strict_refuse_unknown <script-name> <flag>
#
# WHY A SECOND ENTRY POINT ()
# argv_strict_parse above owns the WHOLE argv for wrappers whose only flags are
# --value-file / --value-stdin. Four wrappers cannot use it: aspirations-update-goal,
# aspirations-update, aspirations-add-goal and pipeline-update-field each carry a real
# flag table of their own (--source, --override-*, --blocker-ref, --cross-lane, ...).
# They kept hand-rolled parsers and therefore kept the silent `-*)` arm the parser above
# was written to kill — the busiest write path in the loop was the half that was skipped.
#
# This gives them the REFUSAL without the parse. That split is deliberate rather than a
# shortcut:  tracks two live defects inside argv_strict_parse / _resolve_value
# (the `--` end-of-options latch and the empty `--value-file=` payload), and this goal is
# sequenced AFTER it precisely so adopting the parser does not inherit them. A refusal
# helper touches neither code path, so the four can be fixed now and adopt the full parser
# later if their flag tables ever collapse.
#
# Exit 2, same contract as argv_strict_parse — see the EXIT 2 note in the header. Tests
# MUST pin rc == 2, not merely non-zero: the daemon transport path also exits non-zero.
#
# THE THIRD ARGUMENT IS NOT DECORATION. Pass the wrapper's accepted flags,
# space-separated. Without it the refusal can only say "the flags in this script's
# case block", which sends the caller to read source — so the message names the
# defect but not the fix. A refusal is only better than a silent swallow if the
# caller can act on it. `argv_strict_help` prints the same list — so each adopting
# wrapper MUST hold it in a single `_ACCEPTED_FLAGS` variable and pass that to both,
# never two literals. (This comment originally ASSERTED the single-literal property
# as though it were already true; it was not, and fresh-eyes on this goal's own diff
# caught the claim before the duplication could drift. Two strings that must agree
# is precisely the failure class the refusal exists to remove.)
argv_strict_refuse_unknown() {
    local script="$1" flag="$2" accepted="${3-}"
    {
        printf "%s: unknown option '%s' — refusing.\n" "$script" "$flag"
        printf '  This flag used to be appended to a PASSTHROUGH array that nothing reads,\n'
        printf '  so it vanished silently and the NEXT argument slid into a positional slot\n'
        printf '  — writing the wrong value with exit status 0 (g-115-4733).\n'
        if [ -n "$accepted" ]; then
            printf '  Accepted flags: %s\n' "$accepted"
        else
            printf '  Accepted flags are exactly those named in this script'"'"'s case block.\n'
        fi
    } >&2
    exit 2
}

# argv_strict_refuse_extra_positional <script-name> <extra-arg> <max-positionals> [accepted-flags]
#
# WHY A THIRD ENTRY POINT ()
# argv_strict_refuse_unknown above gives a hand-rolled loop the FLAG half of the
# guarantee. It does not give the POSITIONAL half, and the two fail the same way:
# an argument the parser did not expect is dropped on the floor and the command
# succeeds against the wrong target. argv_strict_parse enforces a ceiling at
# L71-77, but it owns the whole argv and knows only --value-file/--value-stdin,
# so the five wrappers with real flag tables of their own cannot use it. They
# adopted the refusal, inherited the flag guarantee, and kept the swallow.
#
# MEASURED on aspirations-release.sh (cc-07, 2026-08-07), which is what this was
# written for. Its catch-all arm was `[ -z "$GOAL_ID" ] && GOAL_ID="$1"; shift`,
# so a queue name passed as a bare second positional vanished and the release went
# to the DEFAULT queue with no complaint:
#     aspirations-release.sh <id> agent          -> "not found in world queue"
#     aspirations-release.sh <id> --source agent -> "not found in agent queue"
# Same intent, opposite target, and only the second is what the caller meant. That
# mis-invocation became plausible only once --source existed () — the
# flag's existence is what teaches a caller the wrapper has a queue argument.
#
# ONLY aspirations-release.sh adopts this today; stating that plainly because the
# two-call-site bar (implementation-discipline rule 3) is met by CANDIDATES, not by
# current callers. It lives here rather than inline for the reason the header gives
# for the parser: aspirations-update.sh, aspirations-update-goal.sh,
# aspirations-add-goal.sh and pipeline-update-field.sh all carry the same
# catch-all shape today, and five hand-rolled copies of one safety refusal is the
# drift class this file exists to remove. Whether each of those four is actually
# WRONG to swallow was not measured here — several collect positionals into an
# array and may validate the arity later; check before adopting.
#
# Exit 2, same contract as the two helpers above — see the EXIT 2 note in the
# header. Tests MUST pin rc == 2, not merely non-zero.
argv_strict_refuse_extra_positional() {
    local script="$1" extra="$2" maxpos="$3" accepted="${4-}"
    {
        printf "%s: unexpected extra argument '%s' (expected %s positional(s)) — refusing.\n" \
            "$script" "$extra" "$maxpos"
        printf '  Extra positionals used to be discarded silently, so a value meant for a\n'
        printf '  flag was accepted, dropped, and the command ran against the DEFAULT\n'
        printf '  target with exit status 0 (g-306-259).\n'
        if [ -n "$accepted" ]; then
            printf '  Did you mean to pass it with a flag? Accepted flags: %s\n' "$accepted"
        fi
    } >&2
    exit 2
}

# argv_strict_refuse_flaglike_value <script-name> <flag> <value> [accepted-flags]
#
# WHY A FOURTH ENTRY POINT ()
# The three helpers above guard the FLAG position and the POSITIONAL position.
# All three are structurally blind to the VALUE position, and not by oversight: a
# token consumed as a flag's value never reaches the `-*)` arm at all, so the
# unknown-flag refusal cannot see it no matter how strict it becomes. A
# value-taking flag whose value is MISSING therefore swallows the NEXT FLAG as its
# value, and the command answers a question nobody asked with exit 0.
#
# MEASURED on aspirations-query.sh (cc-07, uname -r 6.8.0-136-generic,
# 2026-08-10), same store, same term, varying only the trailing flag:
#     --title-contains 'directive-lane'                         -> n=20
#     --title-contains 'directive-lane' --goal-field status pending -> n=12  (composes)
#     --title-contains 'directive-lane' --goal-field id --full  -> n=0   (id == "--full")
#     --title-contains 'directive-lane' --goal-status --full    -> n=0   (status == "--full")
# Note the second line: the composition works. The zeros come from the value slot
# eating a flag, NOT from the filters conflicting — which is what  was
# filed believing, on a reading taken before the unknown-flag refusal existed.
#
# WHY IT REFUSES EVEN A PLAUSIBLE-LOOKING VALUE. 306 of 6086 live goal titles
# contain a `--token` (122 distinct), so `--title-contains '--apply'` is a real
# thing to want, and this refusal blocks it. That trade is deliberate and cheap in
# ONE direction only: --title-contains is a SUBSTRING match, so dropping the
# leading dashes never loses a match, while a silent zero is a false negative that
# ends an investigation. The message therefore names the workaround rather than
# just the rule.
#
# Exit 2, same contract as the three helpers above. Tests MUST pin rc == 2, not
# merely non-zero: the daemon transport path also exits non-zero.
argv_strict_refuse_flaglike_value() {
    local script="$1" flag="$2" value="$3" accepted="${4-}"
    # An EMPTY value is a different defect (value missing at end of argv) and is
    # already caught loudly downstream by each wrapper's "filter required" check —
    # do not claim it here, or that clearer error is replaced by a vaguer one.
    case "$value" in
        -?*) ;;
        *) return 0;;
    esac
    {
        printf "%s: %s was given '%s', which is a flag, not a value — refusing.\n" \
            "$script" "$flag" "$value"
        printf '  A value-taking flag whose value is MISSING swallows the NEXT FLAG as its\n'
        printf '  value, so the command silently answers a different question and still exits\n'
        printf '  0 (g-115-5128). A token eaten as a value never reaches the unknown-flag\n'
        printf '  refusal, so that guard structurally cannot catch this one.\n'
        printf '  If you did mean a literal value starting with "-": substring filters match\n'
        printf '  anywhere, so drop the leading dashes — "apply" matches "--apply".\n'
        if [ -n "$accepted" ]; then
            printf '  Accepted flags: %s\n' "$accepted"
        fi
    } >&2
    exit 2
}

# ── THE VALUE-POSITION RESIDUAL: DECIDED ONCE FOR THE SWEEP () ──
#
# argv_strict_refuse_flaglike_value above refuses a dash-prefixed VALUE. Whether
# every adopting wrapper should call it at every value-taking flag was left open
# per-site, which meant re-deriving it once per wrapper — 22 times as originally
# scoped. Decided here instead, so an adopter reads one rule.
#
# DECISION: refuse a dash-prefixed value at EVERY value-arg site. No numeric
# carve-out. Measured on the sweep's own value domains, 2026-08-22 (echo, cc-03):
#
#   enum   (--source: world|agent)      no legitimate dash value exists.
#   id     (--id: asp-NNN)              no legitimate dash value exists.
#   text   (--text/--find: substring)   already settled by the  header
#                                       above — substring match means dropping the
#                                       leading dashes never loses a match.
#   int    (--limit, --top)             THE ONLY ARGUABLE CLASS, and it resolves
#                                       the same way. `--limit -1` looks like an
#                                       "unlimited" idiom; it is not one here.
#                                       aspirations.py:199,206 does
#                                       `limit = int(q.get("limit","5"))` then
#                                       `archived[:limit]`, so a negative value
#                                       is a PYTHON SLICE — `archived[:-1]`
#                                       silently drops the last record and exits
#                                       0. Refusing converts a silent wrong
#                                       answer into a loud one, which is this
#                                       whole family's purpose.
#
# So the numeric class — the one that looked like it needed an exception — is the
# strongest case FOR the refusal, not against it. Do not add a numeric escape
# hatch without re-measuring the consumer: the question is never "is a negative
# number syntactically plausible" but "what does THIS endpoint do with it".
#
# argv_strict_help <script-name> <positional-form> <accepted-flags> [<extra-note>]
#
# <extra-note> is OPTIONAL and prints last, after the refusal explanation. It
# exists for facts a caller needs that are not flags — e.g. what keys a wrapper's
# default projection emits (), which is invisible from the flag list
# and is exactly what a caller guesses wrong. All 7 existing callers pass three
# args and are unaffected: the parameter defaults to empty and prints nothing.
# Deliberately NOT folded into <accepted-flags>: that literal is shared with the
# REFUSAL messages, so anything added there would print on every unknown-flag
# error too.
#
# WHY THIS SHIPS ALONGSIDE THE REFUSAL ()
# `--help` is a `-*` token, so the refusal above catches it too — and turning
# `--help` into an exit-2 error is a REGRESSION the refusal introduced, not a
# defect it fixed. It is also the worst possible token to regress: the
#  addendum measured that an unfamiliar wrapper's `--help` is the
# first thing a caller types, and on the stdin-reading members of that family it
# already HANGS for 120s. Refusing it merely swaps one unhelpful answer for
# another.
#
# So each wrapper adopting the refusal gets an `-h|--help)` arm BEFORE the `-*)`
# arm, calling this. Exits 0 — help is a successful invocation, not an error, and
# a caller piping `--help` into a script must not see rc=2.
argv_strict_help() {
    local script="$1" form="$2" accepted="$3" extra="${4:-}"
    printf 'Usage: %s %s\n' "$script" "$form"
    printf '  Accepted flags: %s\n' "$accepted"
    printf '  Any other flag is REFUSED with exit 2 (g-115-4733) — it is not silently\n'
    printf '  dropped, and the token after it is not promoted into a positional slot.\n'
    # An `if`, NOT `[ -n "$extra" ] && printf`: adopting wrappers run under
    # `set -e`, where that && list returns 1 whenever extra is empty (the case
    # for all 7 three-arg callers) and would kill the script before `exit 0` —
    # turning --help into a non-zero exit, which is precisely the regression
    # the block above says this function exists to prevent.
    if [ -n "$extra" ]; then printf '%s\n' "$extra"; fi
    exit 0
}

# argv_strict_resolve_value <script-name> <positional-value-or-empty>
# Echoes the resolved value on stdout. Refuses (exit 2) if more than one source
# was supplied, or if --value-file names a path that does not exist.
argv_strict_resolve_value() {
    local script="$1" positional="${2:-}"
    local srcs=0
    [ -n "$positional" ] && srcs=$((srcs + 1))
    [ -n "$ARGV_VALUE_FILE" ] && srcs=$((srcs + 1))
    [ "$ARGV_VALUE_STDIN" = 1 ] && srcs=$((srcs + 1))
    if [ "$srcs" -gt 1 ]; then
        printf '%s: give the value ONCE — positional, --value-file, or --value-stdin.\n' "$script" >&2
        exit 2
    fi
    if [ -n "$ARGV_VALUE_FILE" ]; then
        if [ ! -f "$ARGV_VALUE_FILE" ]; then
            printf '%s: --value-file not found: %s\n' "$script" "$ARGV_VALUE_FILE" >&2
            exit 2
        fi
        cat "$ARGV_VALUE_FILE"
    elif [ "$ARGV_VALUE_STDIN" = 1 ]; then
        cat
    else
        printf '%s' "$positional"
    fi
}
