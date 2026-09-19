# shellcheck shell=bash
# _repo_slug.sh — the ONE derivation of a GitHub owner/name from a clone ().
#
# WHY THIS FILE EXISTS. deploy-detect-hook.sh and deploy-verify.sh each carried
# the SAME inline sed -- `s#^(git@|https://)([^/:]+)[:/]##; s#\.git$##` -- which
# strips only git@host: and https://host/. It is not a GitHub test at all: it is
# a prefix strip that returns whatever is left, so a non-GitHub origin becomes a
# repo value that is not an owner/name and that `gh api repos/<value>` can never
# resolve. Two copies of one wrong rule is also why it stayed wrong in both.
#
# MEASURED on cc-07 2026-09-18 (alpha, uname -r 6.8.0-139-generic), and on cc-05
# 2026-09-16 (bravo) before it. This clone's remotes:
#   origin  rack:/srv/bulk/widget-service.git      -> old sed: "rack:/srv/bulk/widget-service"
#   github  https://github.com/<owner>/<name>.git -> old sed gives "<owner>/<name>"
# The malformed value then PASSES the write-time qualification check, because
# that check only tested for a "/" and "rack:/srv/bulk/widget-service" has three.
# Consequence measured on cc-04: 90 live obligations, 0 ever cleared, and
# not_clean set on every framework-push close fleet-wide.
#
# THE RULE: derive ONLY from a GitHub URL. A non-GitHub origin is not an error
# and must not be coerced -- the estate deliberately runs a rack bare repo as
# origin with GitHub as a push mirror (guard-6711), so the correct answer for
# such a clone is the MIRROR's slug, and the correct answer for a clone with no
# GitHub remote at all is EMPTY. Empty means "nothing gh can verify", which the
# callers turn into skip-registration / unverified-with-a-reason. It never means
# "guess".
#
# HOT-PATH CONTRACT. deploy-detect-hook.sh is IRREDUCIBLY LOCAL -- it runs on
# every Bash tool call -- so gh_slug_from_url uses ONLY parameter expansion and
# spawns nothing. gh_slug_for_dir tries origin FIRST and returns immediately when
# origin is already GitHub, so a normal clone pays exactly the one `git remote
# get-url origin` it paid before; only a non-GitHub origin pays the extra scan,
# and only on a command that already matched the push pre-filter.
#
# Host is GH_HOST-aware (gh's own env var) rather than hardcoded, so an
# Enterprise host works without editing this file.

# gh_slug_from_url <url> -> prints "owner/name" for a GitHub URL, nothing otherwise.
# Always returns 0: "not a GitHub URL" is a normal answer, not a failure.
gh_slug_from_url() {
    local url="${1:-}"
    local host="${GH_HOST:-github.com}"
    local rest=""
    [ -n "$url" ] || return 0
    case "$url" in
        "git@${host}:"*)       rest="${url#git@"${host}":}" ;;
        "ssh://git@${host}/"*) rest="${url#ssh://git@"${host}"/}" ;;
        "ssh://${host}/"*)     rest="${url#ssh://"${host}"/}" ;;
        "https://${host}/"*)   rest="${url#https://"${host}"/}" ;;
        "http://${host}/"*)    rest="${url#http://"${host}"/}" ;;
        "git://${host}/"*)     rest="${url#git://"${host}"/}" ;;
        *) return 0 ;;
    esac
    rest="${rest%/}"
    rest="${rest%.git}"
    rest="${rest%/}"
    # Exactly two non-empty segments. A deeper path is NOT an owner/name and must
    # not be truncated into one -- silently reshaping a URL nobody recognised is
    # the class of bug this file replaces.
    case "$rest" in
        */*/*) return 0 ;;
        */*)   : ;;
        *)     return 0 ;;
    esac
    [ -n "${rest%%/*}" ] || return 0
    [ -n "${rest#*/}" ] || return 0
    printf '%s' "$rest"
}

# gh_slug_for_dir <dir> -> prints "owner/name" for the clone's first GitHub
# remote (origin preferred), nothing when the clone has none.
# Always returns 0; callers branch on the EMPTY string, never on an rc.
gh_slug_for_dir() {
    local dir="${1:-.}"
    local url="" slug="" r=""
    url=$(git -C "$dir" remote get-url origin 2>/dev/null) || url=""
    slug=$(gh_slug_from_url "$url")
    if [ -n "$slug" ]; then
        printf '%s' "$slug"
        return 0
    fi
    for r in $(git -C "$dir" remote 2>/dev/null); do
        [ "$r" = "origin" ] && continue
        url=$(git -C "$dir" remote get-url "$r" 2>/dev/null) || continue
        slug=$(gh_slug_from_url "$url")
        if [ -n "$slug" ]; then
            printf '%s' "$slug"
            return 0
        fi
    done
    return 0
}
