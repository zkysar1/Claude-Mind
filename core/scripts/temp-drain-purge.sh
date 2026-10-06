#!/usr/bin/env bash
# temp-drain-purge.sh — canonical GUARDED purge of the bound agent's temp/ dir.
# It deletes ONLY what a review decided to discard (user directive 2026-10-05:
# nothing in temp/ is deleted until a review has seen it, and every decision and
# every deletion is recorded). The decisions live in temp/.temp-decisions.jsonl,
# owned by core/scripts/temp_decisions.py; this script asks it what is
# deletable, re-checks its own guards at the delete itself, and logs every
# deletion back to the same file. Exists so autonomous agents NEVER hand-roll an unguarded
# `rm` on a possibly-empty variable path — which triggers a Claude Code
# dangerous-rm permission dialog that HANGS the agent (even under
# --dangerously-skip-permissions, the fleet launch mode). Observed 2026-07-09:
# an agent hung 46+ min blocked on such a dialog ("Dangerous rm operation on
# possibly-empty variable path (TEMP_DIR/f), proceed?") during a temp-drain
# purge (, filed by the fleet operator). Eliminating the
# hand-rolled-rm class means giving every agent ONE guarded purge path.
#
# GUARDS (assert_safe_temp_dir) — ALL must pass before ANY deletion; any failure
# returns non-zero and deletes NOTHING (fail-loud is always safer than a
# dangerous rm):
#   1. agent_dir set + non-empty (the bound agent, via _paths.sh)
#   2. project_root set + non-empty (via _paths.sh)
#   3. temp_dir set + non-empty
#   4. temp_dir is an ABSOLUTE path
#   5. temp_dir is strictly UNDER "$project_root/" (never /, /temp, or a sibling)
#   6. basename(temp_dir) == "temp"
# THREE guarded deletion lanes — ALL bounded by the assert_safe_temp_dir guard
# above; NONE ever uses a per-file `rm` on an interpolated path. Plus Lane 0
# (report_unmanaged_dotfiles, ), which DELETES NOTHING in any mode and
# exists only to make the one file class no lane can see — hidden dotfiles —
# visible; it emits `unmanaged_dotfiles` / `unmanaged_dotfile_names` on the JSON
# and one stderr line per file, identically under --dry-run:
#   Lane 1 (decided files): each file `temp_decisions.py deletable` lists (a
#                        `discard` in force whose content fingerprint still
#                        matches) is deleted by `find "<file>" (the per-path
#                        predicate) -delete`, so the guards are re-read INSIDE
#                        the delete (guard-5952): a regular file, not a dotfile,
#                        not cited by a durable record, untouched for AGE min.
#                        SSOT predicate = _purge_find_predicate. An undecided
#                        file is never deleted, whatever its suffix or age.
#                        Deletes NOTHING when the decision lookup or the cited
#                        lookup fails (fail-closed; "decisions_lookup" /
#                        "citation_lookup" in the JSON say which).
#   Lane 2 (drained GC): `find "$TEMP_DIR/drained" -maxdepth 1 -type f
#                        -mtime +DRAINED_AGE_DAYS -delete` — prunes stale archived
#                        files (temp-store.md: drained/ contents >30d carry zero
#                        retrieval value). drained/ itself is preserved ().
#                        EXEMPTS git-tracked files () AND basenames cited
#                        by a durable record () — before  the
#                        citation exemption existed in Lane 1 only, so archiving a
#                        cited doc into drained/ STRIPPED its protection and made
#                        it age-deletable. SKIPPED ENTIRELY (deletes nothing, warns
#                        on stderr) when the cited set cannot be determined: unlike
#                        Lane 1 there is no allow-list to degrade to. Emits per-file
#                        basenames via "drained_gc_files" so the exemption is
#                        checkable from outside. Those files were reviewed when
#                        the drain moved them there; each deletion is logged.
#   Lane 3 (decided dirs): each folder `deletable` lists is deleted via
#                        `find "<dir>" -delete` (re-asserted strictly under
#                        TEMP_DIR/) only when NOTHING under it, at any depth, was
#                        touched within AGE min. An undecided folder is never
#                        deleted. PRESERVES receipted archives (top-level
#                        RECEIPT.* / .archive-marker) and git repos carrying
#                        unpushed commits or dirty tracked files () —
#                        sole-copy content a clean-worktree glance cannot see;
#                        fail-closed when git itself cannot answer.
# Every deletion (Lanes 1-3) is appended to the decision log after it happens
# (temp_decisions.py log-deleted), so `temp-decisions.sh show --deleted` answers
# "what was deleted, when and why" (guard-6105, guard-6063).
#
# Usage: temp-drain-purge.sh [--dry-run] [--age-min N] [--drained-age-days N]
#   --dry-run           list what WOULD purge/clean, delete nothing
#   --age-min           in-flight guard in minutes (default 120): a decided file,
#                       or a decided folder with ANY entry, touched more recently
#                       is skipped this run
#   --drained-age-days  drained/ GC age guard in days (default 30)
# Output (stdout, JSON): {"purged":N,"would_purge":N,"files":[...],
#   "drained_gc_purged":N,"drained_gc_would_purge":N,"drained_gc_files":[...],
#   "stray_purged":N,"stray_would_purge":N,"stray_dirs":[...],
#   "stray_preserved_git":N,"stray_preserved_git_dirs":[...],          ()
#   "decisions_lookup":"ok"|"failed"|"n/a","citation_lookup":"ok"|"failed"|"n/a",
#   "deletions_logged":N,"deletion_log":"ok"|"failed"|"n/a",
#   "age_skipped":N,"age_skipped_names":[...],                        ()
#   "stray_age_skipped":N,"stray_age_skipped_dirs":[...],             ()
#     — what the --age-min guard EXCLUDED FROM EVALUATION, per lane. Reported
#     as their OWN fields and never folded into would_purge/stray_would_purge
#     (guard-4178). Without them, a path absent from every lane is ambiguous
#     between "evaluated and preserved" and "never looked at", and the
#     ambiguity lands hardest on the operator who just MOVED a file to
#     protect it — the move refreshes the mtime, so the confirming re-run is
#     the one most likely to skip it, silently, in the safe-looking
#     direction. A nonzero age_skipped means: re-run with --age-min 0 to see
#     those paths' real lane verdicts before concluding anything.
#   "dry_run":bool,"age_min":N,"drained_age_days":N,
#   "temp_dir":"..."} — the no-temp-dir no-op path emits the SAME field set
#   (all-zero lane fields, lookups "n/a") so both exit paths share one
#   schema (fresh-eyes finding bravo-fec-noop-json-missing-lane-fields).
#   "files" is LANE 1; "drained_gc_files" is LANE 2 (); "stray_dirs"
#   is LANE 3 — names, not just a count (guard-6063).
#   decisions_lookup or citation_lookup "failed" means Lanes 1 and 3 deleted
#   nothing, and citation_lookup "failed" also skips Lane 2 — treat zeros under
#   either as unmeasured, not clean. deletion_log "failed" means deletions
#   happened that the log does not show: the stderr WARN names them.
# Exit: 0 on success (incl. no-temp-dir no-op); 1 on a guard refusal; 2 on bad args.
#
# assert_safe_temp_dir() + the lane functions (gc_drained_archive,
# cleanup_stray_dirs) + _purge_find_predicate are sourceable + unit-tested
# (test_temp_drain_purge.sh): `source temp-drain-purge.sh` does NOT run main()
# (guarded at the bottom), so a test can call each with hostile/synthetic inputs.
set -uo pipefail

# assert_safe_temp_dir <candidate_temp_dir> <project_root> <agent_dir>
# Pure validation — echoes a REFUSED reason to stderr + returns 1 on any guard
# failure, returns 0 when the candidate is safe to purge. NEVER deletes.
assert_safe_temp_dir() {
  local temp_dir="${1:-}" project_root="${2:-}" agent_dir="${3:-}"
  if [ -z "$agent_dir" ]; then
    echo "temp-drain-purge.sh: REFUSED — AGENT_DIR empty/unset (agent binding failed). Purged nothing." >&2; return 1
  fi
  if [ -z "$project_root" ]; then
    echo "temp-drain-purge.sh: REFUSED — PROJECT_ROOT empty/unset. Purged nothing." >&2; return 1
  fi
  if [ -z "$temp_dir" ]; then
    echo "temp-drain-purge.sh: REFUSED — TEMP_DIR empty. Purged nothing." >&2; return 1
  fi
  case "$temp_dir" in
    /*) : ;;
    *) echo "temp-drain-purge.sh: REFUSED — TEMP_DIR '$temp_dir' is not absolute. Purged nothing." >&2; return 1 ;;
  esac
  case "$temp_dir" in
    "$project_root"/*) : ;;
    *) echo "temp-drain-purge.sh: REFUSED — TEMP_DIR '$temp_dir' is not under PROJECT_ROOT '$project_root'. Purged nothing." >&2; return 1 ;;
  esac
  if [ "$(basename "$temp_dir")" != "temp" ]; then
    echo "temp-drain-purge.sh: REFUSED — TEMP_DIR basename is not 'temp' ('$temp_dir'). Purged nothing." >&2; return 1
  fi
  return 0
}

# _purge_find_predicate <age_min> [cited_basename...] — populate the global
# PURGE_FIND_PRED array with the find predicate Lane 1 evaluates on ONE decided
# file: `find "<file>" "${PURGE_FIND_PRED[@]}" -delete`. SINGLE SOURCE OF TRUTH
# for the Lane-1 guards: main() uses it for the dry-run listing AND the delete,
# and test_temp_drain_purge.sh sources it, so a test can never diverge from the
# real predicate.
#
# WHAT IT NO LONGER DOES (2026-10-05). Until then this predicate CHOSE the
# candidates, by class: every depth-1 file except dotfiles, content-bearing
# .md/.json and cited basenames (), with every other suffix gated on a
# drain watermark. Scripts, logs and invented suffixes were deleted at bare age
# with nobody looking, and the watermark could not tell "seen and kept" from
# "seen and discarded" (guard-4864). Now the candidates come ONLY from recorded
# review decisions (temp_decisions.py deletable), and this predicate is the
# guard layer evaluated by the delete itself (guard-5952):
#   -maxdepth 0 -type f   the path itself, a regular file (never a dir or link)
#   ! -name '.*'          never a dotfile: temp/'s one git-tracked file is a
#                         0-byte .gitkeep (), and the decision log
#                         is a dotfile
#   ! -name <cited>...    never a basename a durable record cites. `decide`
#                         already refuses to discard one; this is the backstop
#   -mmin +AGE            never a file touched within AGE minutes (in flight)
#
# Caller passes basenames from EVERY agent's temp/, not just the bound one.
# Over-exemption is the fail-safe direction, and it removes a whole failure
# mode: an agent-resolution bug could otherwise silently un-protect a cited
# file, which deletes evidence, while the cost of the broader set is at worst
# retaining a same-named uncited file.
#
# CITED PATTERNS MAY CARRY WILDCARDS, AND THAT IS HONORED DELIBERATELY.
# Measured 2026-07-31 on the live corpus: 4 of 64 cited paths are wildcards
# ("…/temp/-*", "…/temp/mergeback-*.json", "…/temp/animate-Enemy*-original.lua",
# "…/temp/prune-probe*-.py") — durable records legitimately cite a
# FAMILY of artifacts, not one file. Escaping them to literals would match
# nothing, so the cited family would be deleted; honoring them is the safe
# direction, and it is what the citation actually asserts.
#
# The one case that must NOT be honored is a pattern broad enough to match ANY
# filename ("*", "*.*"): a single such citation would silently exempt every
# file and revert this whole change with no signal — the change would look
# installed while doing nothing. It is detected by testing each pattern against
# a sentinel name no real artifact carries, and dropped LOUDLY on stderr.
#
# CLASS vs FAMILY — the sentinel above is NECESSARY BUT NOT SUFFICIENT, measured
# 2026-08-16 (echo, cc-03). Every wildcard named above carries a literal STEM
# ("mergeback-", "-"), which is what makes it a family: it names a
# bounded set of artifacts an author actually produced. A pattern with NO literal
# stem ("*.raw") names an entire file CLASS instead, and the sentinel cannot see
# the difference — "*.raw" does not match the sentinel, so it passed through and
# exempted ALL 84 aged .raw files on this box, reporting would_purge:0 against a
# dir the lane was built to drain. 8 of 100 cited basenames carried glob
# metacharacters and all 8 passed the sentinel, shielding 86 files.
#
# The offending citation was scraped from guard-3510's rule TEXT, where "*.raw"
# appears as PROSE describing a redirect failure — not as an assertion that any
# .raw file is evidence. So the discriminator is on SHAPE, not on provenance: a
# citation that names a class is un-honorable no matter who wrote it, because
# honoring it exempts that whole extension from Lane 1. Dropped LOUDLY, same
# as the sentinel case; the two branches are kept separate so "*"/"*.*" keep
# their own message and the sentinel's test hook stays reachable.
_PURGE_OVERBROAD_SENTINEL='zzz-overbroad-sentinel-9f3a2c'

_purge_find_predicate() {
  local age_min="$1"; shift
  local _b
  PURGE_FIND_PRED=( -maxdepth 0 -type f ! -name '.*' )
  for _b in "$@"; do
    [ -n "$_b" ] || continue
    # Default-expanded: this function is documented as sourceable in isolation,
    # and `set -u` on an unset sentinel would abort inside a DELETE path.
    case "${_PURGE_OVERBROAD_SENTINEL:-zzz-overbroad-sentinel-9f3a2c}" in
      $_b) echo "temp-drain-purge: WARN — ignoring over-broad cited exemption '$_b' (matches any filename; honoring it would disable Lane 1 entirely)" >&2
           continue ;;
    esac
    # A cited pattern with NO literal stem (leading wildcard) names a file CLASS,
    # not a family of artifacts, so it is not a citation this lane can honor.
    # See the "CLASS vs FAMILY" note above the sentinel for the measurement.
    #
    # THIS BRANCH ALSO CATCHES '*.*', WHICH THE SENTINEL ABOVE NEVER DID — measured,
    # not assumed. The sentinel tests each pattern against a literal string that
    # contains no dot, so '*.*' does not match it and was HONORED before this
    # branch existed: a second silent lane-disabling pattern the over-broad guard
    # was believed to cover. Only a bare '*' reaches the sentinel. Do not "simplify"
    # by folding the two branches together on the assumption they overlap.
    #
    # KNOWN FALSE REJECT, latent: a pattern LEADING with a bracket expression
    # ('[abc]foo.txt') names a bounded 3-file family but strips to an empty stem,
    # so it is refused here and its cited evidence becomes purgeable. That is the
    # harmful direction, so it is stated rather than left implicit. Measured
    # 2026-08-16: 0 of 100 live cited basenames begin with '[' or '?'. If one ever
    # does, widen the strip to skip a leading bracket group — do NOT drop the
    # stem test.
    if [ -z "${_b%%[*?[]*}" ]; then
      echo "temp-drain-purge: WARN — ignoring class-wide cited exemption '$_b' (no literal stem; names a file CLASS, not an artifact family — honoring it would disable that whole extension in Lane 1)" >&2
      continue
    fi
    PURGE_FIND_PRED+=( ! -name "$_b" )
  done
  # : snapshot the predicate WITHOUT the age guard, so main() can
  # report what the guard EXCLUDED. Absence from every lane otherwise reads as
  # "safe" when it may mean "never evaluated" — and the reader most likely to
  # hit it is the operator who just refreshed an mtime by moving a file to
  # protect it (guard-2672 is the behavioral half; this is the tool half).
  PURGE_FIND_PRED_NOAGE=( "${PURGE_FIND_PRED[@]}" )
  PURGE_FIND_PRED+=( -mmin "+$age_min" )
}

# _decided_items <script_dir> <temp_dir> — echo `kind<TAB>name` (kind: file|dir)
# for every top-level item a review decided to discard and whose fingerprint
# still matches (temp_decisions.py deletable). Returns NON-ZERO when the
# decisions could not be read or a lookup they depend on failed: the caller
# deletes NOTHING in Lanes 1 and 3 then, never "whatever matched before".
_decided_items() {
  local script_dir="${1:-}" temp_dir="${2:-}"
  [ -f "$script_dir/temp_decisions.py" ] || return 1
  python3 "$script_dir/temp_decisions.py" --temp-dir "$temp_dir" deletable
}

# _log_deletions <script_dir> <temp_dir> <lane> [why] — append one deletion row
# per `kind<TAB>name` line on stdin to the decision log (the purge calls it
# AFTER deleting; a name that still exists is not recorded). Echoes the count
# logged; returns non-zero when the log could not be written.
_log_deletions() {
  local script_dir="${1:-}" temp_dir="${2:-}" lane="${3:-}" why="${4:-}" out
  local args=( --temp-dir "$temp_dir" log-deleted --lane "$lane" )
  [ -n "$why" ] && args+=( --why "$why" )
  out="$(python3 "$script_dir/temp_decisions.py" "${args[@]}")" || return 1
  printf '%s\n' "$out" | sed -n 's/.*"logged": *\([0-9][0-9]*\).*/\1/p'
}

# _json_names <newline-separated paths or names> — a JSON array of basenames,
# with backslash and double quote escaped (a name may legally contain either).
_json_names() {
  local out='[' first=1 line b
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    b="${line%/}"; b="${b##*/}"
    b="${b//\\/\\\\}"; b="${b//\"/\\\"}"
    [ "$first" -eq 0 ] && out="$out,"
    out="$out\"$b\""
    first=0
  done <<EOF
${1:-}
EOF
  printf '%s]' "$out"
}

# _kind_lines <kind> <name_prefix> <newline-separated paths> — one
# `kind<TAB>prefix+basename` line per path, the shape log-deleted reads.
_kind_lines() {
  local kind="${1:-}" prefix="${2:-}" line b
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    b="${line%/}"; b="${b##*/}"
    printf '%s\t%s%s\n' "$kind" "$prefix" "$b"
  done <<EOF
${3:-}
EOF
}

# _count_lines <newline-separated list> — number of non-empty lines.
_count_lines() {
  if [ -z "${1:-}" ]; then echo 0; else printf '%s\n' "$1" | grep -c . || true; fi
}

# _cited_basenames <script_dir> — echo one basename per line for every temp/
# path cited by a durable record. Returns NON-ZERO when the cited set is
# UNKNOWN (world unreadable, script missing, python unavailable) — the caller
# MUST treat that as "delete nothing in Lanes 1-3", never as "nothing is
# cited". The ratchet's --cited-paths mode exits 2 rather than printing an
# empty list for exactly this reason: on a box with an unmounted world, an
# empty-and-successful result would read as a licence to purge everything.
# Trailing slashes are stripped so a cited DIRECTORY contributes its own name;
# harmless against a -type f lane, and cheaper than special-casing it.
_cited_basenames() {
  local script_dir="${1:-}" out
  [ -f "$script_dir/temp-citation-ratchet.py" ] || return 1
  out="$(python3 "$script_dir/temp-citation-ratchet.py" --cited-paths 2>/dev/null)" || return 1
  printf '%s\n' "$out" | sed 's#/*$##; s#.*/##' | grep -v '^$' || true
  return 0
}

# gc_drained_archive <drained_dir> <age_days> <dry_run> [cited_basename...] —
# Lane 2. Prune files DIRECTLY under drained/ older than <age_days>. Echoes the
# match count (the would-purge count when dry_run=1, else the purged count) and
# populates the global GC_DRAINED_FILES with one BASENAME per line for every
# file it would delete / did delete. find -maxdepth 1 -type f keeps -delete
# bounded to files (never the drained/ dir itself) — never a hand-rolled rm.
# Caller MUST have asserted drained_dir's parent temp_dir safe.
# Sourceable + unit-tested (test_temp_drain_purge.sh) with a synthetic drained/.
#
# CITED FILES ARE NEVER DELETED (). Lane 1 gained this exemption in
# ; Lane 2 did not, which made citation-protection a property of WHICH
# DIRECTORY a file happens to sit in rather than a property of the artifact. The
# moment /drain-temp archived a cited doc into drained/, its protection vanished
# and it became age-deletable with no reference check at all. Same variadic
# cited-basename shape as _purge_find_predicate, deliberately — one idiom.
#
# WHY THE FILE LIST IS PART OF THE FIX, not decoration: this lane returned a
# bare COUNT, so `durability-property-check.py cited-temp-not-purged` had
# nothing to intersect and was Lane-1-only BY CONSTRUCTION. An exemption nobody
# can verify is the conditionally-active-mechanism pattern this whole 
# body of work exists to kill (guard-1943: a green suite certifies the FUNCTION,
# never the WIRING). Emitting the list is what makes the exemption checkable
# from outside.
#
# The caller owns the UNKNOWN-cited-set policy, exactly as it does for Lane 1:
# passing zero basenames here means "nothing is cited", which is only true when
# the lookup SUCCEEDED and returned empty. main() skips this lane outright when
# citation_lookup=="failed" — see its call site.
#
# GIT-TRACKED FILES ARE NEVER DELETED (, authored at ZDS 2026-07-31,
# back-ported UP same day). WHY: a deployment's .gitignore can encode the
# biconditional
#   git-ignored  <==>  the guarded purge deletes it
# reasoning about Lane 1 only (depth-1 ephemera, by extension), concluding that
# `drained/` contents are NOT purged and therefore SHOULD be tracked. Lane 2
# purges them anyway — by AGE, at any extension — so the biconditional breaks in
# the one direction that loses data: 206 git-TRACKED files under a prod agent's
# temp/drained/ were scheduled for deletion, after which iteration-commit would
# record the removal and drop them from HEAD.
#
# Acute because mtimes there are PROVISIONING artifacts, not authoring times:
# 188 of the 206 shared one mtime (five seconds after `.git` birth), so they
# would all cross +30d on the SAME DAY rather than trickling. A trickle gets
# noticed; a synchronized mass deletion happens while nobody is looking.
#
# NO-OP where temp/ is fully git-ignored (this deployment: , justified
# by own-cloud S3 durability) — `git ls-files` returns nothing there and the
# filter removes nothing. Load-bearing only where temp/ is default-durable,
# i.e. under STORAGE_BACKEND=local.
#
# FAIL-SAFE DIRECTION: if `git ls-files` is unavailable or errors, the lane
# deletes NOTHING. Retaining junk is recoverable; deleting a tracked artifact is
# not.
gc_drained_archive() {
  local drained_dir="${1:-}" age_days="${2:-30}" dry_run="${3:-0}" list count=0
  local tracked f rel repo_root untracked=""
  # Explicit arity check, NOT `shift 3 || true`: under a short call that shift
  # fails and leaves the ORIGINAL positionals in "$@", which would then be read
  # as cited basenames. Guarding on $# keeps a 3-arg call (every existing unit
  # test) at an empty cited set, byte-identical to the pre- behaviour.
  local cited_arr=()
  if [ "$#" -gt 3 ]; then shift 3; cited_arr=( "$@" ); fi
  # Two globals so a caller can read BOTH results without a subshell (`$(...)`
  # would discard them). The stdout `echo "$count"` contract below is unchanged,
  # so existing 3-arg callers/tests that capture it keep working verbatim.
  GC_DRAINED_FILES=""               # basenames this lane would delete / deleted
  GC_DRAINED_PATHS=""               # ABSOLUTE paths, for backend delete-propagation ()
  GC_DRAINED_COUNT=0
  [ -d "$drained_dir" ] || { echo 0; return 0; }
  list="$(find "$drained_dir" -maxdepth 1 -type f -mtime "+$age_days" 2>/dev/null || true)"
  [ -z "$list" ] && { echo 0; return 0; }

  # Resolve the repo root once; ls-files paths are repo-relative.
  #
  # THE TWO GIT OUTCOMES ARE NOT THE SAME, and conflating them is a bug the unit
  # tests caught immediately (3 failures, first attempt): "this is not a git repo"
  # means NO file here can be tracked, so nothing is protected and the normal
  # age-based GC must proceed exactly as before. Only "this IS a repo but ls-files
  # ERRORED" is the unknowable case where deleting would be a guess — that one
  # deletes nothing. Treating not-a-repo as unknowable disables the lane entirely
  # for every caller outside a work-tree.
  repo_root="$(git -C "$drained_dir" rev-parse --show-toplevel 2>/dev/null || true)"
  if [ -z "$repo_root" ]; then
    tracked=""                       # not a work-tree → nothing can be tracked
  else
    tracked="$(git -C "$repo_root" ls-files -- "$drained_dir" 2>/dev/null)" || { echo 0; return 0; }
  fi

  local _bn _c _is_cited
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    # CITED exemption first — it is the cheaper test and the stronger claim.
    # Matched on BASENAME, same key _purge_find_predicate uses for Lane 1, so a
    # citation protects an artifact identically whether it sits in temp/ or has
    # already been archived into temp/drained/.
    # No subprocess per file: `basename` and a `grep` per file cost ~4 process
    # spawns each, which took 160 s for 875 files on a Windows box (2026-10-05).
    # The grep is skipped outright when nothing is tracked (temp/ is fully
    # gitignored here), the common case.
    _is_cited=0
    _bn="${f##*/}"
    for _c in ${cited_arr[@]+"${cited_arr[@]}"}; do
      [ "$_c" = "$_bn" ] && { _is_cited=1; break; }
    done
    [ "$_is_cited" -eq 1 ] && continue            # cited → keep, by the invariant
    rel="${f#"$repo_root"/}"
    if [ -z "$tracked" ]; then
      untracked="${untracked}${f}"$'\n'           # nothing tracked → disposable
      continue
    fi
    case "$(printf '%s\n' "$tracked" | grep -Fx -- "$rel" || true)" in
      "") untracked="${untracked}${f}"$'\n' ;;   # not tracked → disposable
      *)  : ;;                                    # tracked → keep, by the invariant
    esac
  done <<< "$list"

  [ -n "$untracked" ] && count="$(printf '%s' "$untracked" | grep -c . || true)"
  # Publish the basenames so an external checker can intersect them against the
  # cited set. Emitted for BOTH dry-run and real runs — a dry-run-only list would
  # leave the real deletion path unverifiable, which is the gap being closed.
  if [ -n "$untracked" ]; then
    while IFS= read -r f; do
      [ -n "$f" ] || continue
      GC_DRAINED_FILES="${GC_DRAINED_FILES}${f##*/}"$'\n'
      GC_DRAINED_PATHS="${GC_DRAINED_PATHS}${f}"$'\n'
    done <<< "$untracked"
  fi
  if [ "$count" -gt 0 ] && [ "$dry_run" -eq 0 ]; then
    # Per-file bounded guarded find — mirrors Lane 3's per-dir idiom; never a
    # hand-rolled rm.
    while IFS= read -r f; do
      [ -n "$f" ] || continue
      find "$f" -maxdepth 0 -type f -delete 2>/dev/null || true
    done <<< "$untracked"
  fi
  GC_DRAINED_COUNT="$count"
  echo "$count"
}

# _has_archive_receipt <dir> — exit 0 when <dir> carries an archive-before-delete
# receipt sentinel at its top level. SINGLE SOURCE OF TRUTH for the Lane-3
# preservation test; sourceable + unit-tested so the test can never diverge from
# the real predicate.
#
# EXTENSION- AND CASE-AGNOSTIC BY MEASUREMENT, NOT BY TASTE ().
# archive-before-delete.md step 6 mandates a receipt and deliberately names no
# filename, so the writer and the reader were free to disagree — and every
# producer in this tree landed on the far side of that disagreement. Measured
# 2026-08-08 (bravo, hostname cc-05, uname -r 6.8.0-136-generic):
#   _seed_engine.py:1207          writes  RECEIPT.json   (graveyard archives)
#   history_vacuum_archive.py:167 writes  receipt.json   (local, LOWERCASE)
#   history_vacuum_archive.py:228 writes  receipt.json   (S3, LOWERCASE)
# This predicate previously required RECEIPT.md exactly. ZERO producers write
# that name, so the preservation guard was structurally unreachable by every
# archive the framework actually creates — it could only ever fire on a receipt
# a human had hand-named. The originating case was live: a genuine
# archive-before-delete archive carrying RECEIPT.json was listed for deletion
# and survived only because it was hand-marked mid-drain.
#
# The widening is on the PRESERVE side only and can never delete something the
# old predicate kept, which is the correct asymmetry: a missed sentinel destroys
# a recovery layer (the exact anti-pattern archive-before-delete.md exists to
# forbid), while an over-match merely retains a stray dir until someone looks.
#
# ANCHORED `RECEIPT` / `RECEIPT.*`, never a bare `*receipt*` substring. A dir
# holding `old-receipt-notes.txt` is scratch, not an archive; matching it would
# make the guard unfalsifiable (guard-2860 — never relax an ownership predicate
# into a pattern). -iname is honored by GNU findutils and bfs alike, the same
# portability bar the Lane-1 -empty disjunct is held to.
_has_archive_receipt() {
  local d="${1:-}"
  [ -n "$d" ] || return 1
  if [ -e "$d/.archive-marker" ]; then return 0; fi
  [ -n "$(find "$d" -maxdepth 1 -type f \( -iname 'RECEIPT' -o -iname 'RECEIPT.*' \) 2>/dev/null | head -n 1)" ]
}

# report_unmanaged_dotfiles <temp_dir> — Lane 0. REPORT (never delete) hidden
# dotfiles sitting directly under temp/ that are not lifecycle markers. Echoes
# the count; names go to stderr. Sourceable + unit-tested.
#
# WHY REPORT AND NOT PURGE (). Lane 1 exempts dotfiles via
# `! -name '.*'` and the drain lane enumerates temp/*.md + temp/*.json, neither
# of which matches a leading dot — so a dotfile is never drained, never purged,
# and never counted by the temp-pressure metric. That is permanent invisible
# residue, and the originating case was a 221-byte .launch-payload.json holding
# an api_key, an account_id and two service keys, removed only because a human
# happened to look during a hand drain.
#
# Purging them is the WRONG correction, and this is measured rather than
# cautious. The Lane-1 dotfile exemption exists for a stated reason (it protects
# the git-tracked 0-byte .gitkeep from the -empty sub-lane — ), and
# the live population is working state, not litter: on this box temp/ carried
# .fresh-eyes-last-ts, .fe-ts and .-backup-path, all cadence markers
# a blanket purge would silently delete. The goal's own verification asks for
# "reported OR purged"; reporting is the branch that adds visibility without
# adding a new way to destroy live state.
#
# The allowlist is the LIFECYCLE markers this framework writes on purpose.
# DOTFILE_ALLOWLIST overrides it (space-separated basenames) for tests.
# .temp-decisions.jsonl (+ its transient .lock) joined 2026-10-05: the decision
# log every purge reads and appends to — MANAGED, not unmanaged residue (the
# guard-1581 distinction this lane reports on). .drain-watermark left the list
# the same day: the gate it fed is retired, and main() removes an old marker
# once, logged. Twin of temp_decisions.py MANAGED_DOTFILES; edit both (guard-130).
_DOTFILE_ALLOWLIST_DEFAULT='.gitkeep .archive-marker .temp-decisions.jsonl .temp-decisions.lock'
UNMANAGED_DOTFILES=""              # newline-separated basenames, for the caller
report_unmanaged_dotfiles() {
  local temp_dir="${1:-}" count=0 f b
  UNMANAGED_DOTFILES=""
  [ -d "$temp_dir" ] || { echo 0; return 0; }
  local allow=" ${DOTFILE_ALLOWLIST:-$_DOTFILE_ALLOWLIST_DEFAULT} "
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    b="$(basename "$f")"
    case "$allow" in *" $b "*) continue ;; esac
    echo "temp-drain-purge: UNMANAGED DOTFILE (never drained, never purged, uncounted): $f" >&2
    UNMANAGED_DOTFILES="${UNMANAGED_DOTFILES}${b}"$'\n'
    count=$((count + 1))
  done <<EOF
$(find "$temp_dir" -maxdepth 1 -type f -name '.*' 2>/dev/null || true)
EOF
  echo "$count"
}

# cleanup_stray_dirs <temp_dir> <age_min> <dry_run> [decided_dir_name...] —
# Lane 3. Remove the named dirs DIRECTLY under temp_dir — the folders a review
# decided to discard (main() passes `temp_decisions.py deletable`'s dir names).
# With no names it removes NOTHING: until 2026-10-05 this lane took every dir
# untouched for <age_min> minutes, recursively, with nobody looking. Echoes the
# match count. A named dir is skipped when it is not a real dir directly under
# temp_dir, when ANY entry under it was touched within <age_min> minutes (the
# folder's own mtime misses edits to nested files), when it carries a receipt,
# or when it holds git work only it has. Each removal is bounded under
# temp_dir/ by a per-dir re-assert (defense-in-depth) then a guarded
# `find "$stray" -delete` — never a hand-rolled rm. Caller MUST have asserted
# temp_dir safe. Sourceable + unit-tested.
cleanup_stray_dirs() {
  local temp_dir="${1:-}" age_min="${2:-120}" dry_run="${3:-0}" list="" count=0 d _n
  if [ "$#" -gt 3 ]; then
    shift 3
    for _n in "$@"; do
      case "$_n" in ''|.|..|drained|*/*) continue ;; esac
      list="${list}${temp_dir}/${_n}"$'\n'
    done
  fi
  # Count ALSO published as a global (STRAY_COUNT) so main() can call this
  # WITHOUT command substitution — a $(...) subshell would discard the
  # STRAY_PURGED_PATHS global below, exactly the trap Lane 2's call site
  # documents for GC_DRAINED_FILES (and which the first draft of the
  # propagation wiring fell into: stray_would=3 against dirs=0, measured).
  # The stdout `echo "$count"` contract is unchanged for existing unit tests.
  STRAY_COUNT=0
  # ABSOLUTE dir paths (one per line, TRAILING SLASH) of purged / would-purge
  # stray dirs, for the backend delete-propagation pass () — same
  # no-subshell global idiom as GC_DRAINED_FILES. The slash tells
  # --purge-propagate to resolve the dir by S3-prefix listing rather than
  # expecting a file key; the dir's files are deliberately NOT enumerated
  # here (see the collection site below for the measured 153k-file reason).
  STRAY_PURGED_PATHS=""
  # Basenames of git dirs PRESERVED by the  guard below (newline-
  # separated; same no-subshell global idiom as UNMANAGED_DOTFILES). main()
  # surfaces them as stray_preserved_git / stray_preserved_git_dirs so the
  # preservation is checkable from outside, not just asserted.
  STRAY_PRESERVED_GIT=""
  STRAY_AGE_SKIPPED_DIRS=""
  STRAY_AGE_SKIPPED=0
  [ -d "$temp_dir" ] || { echo 0; return 0; }
  [ -z "$list" ] && { echo 0; return 0; }
  # Iterate the decided dirs: preserve archive-before-delete archives
  # (); `count` reflects ONLY dirs actually purged (or that WOULD
  # purge under --dry-run), never the preserved or skipped ones.
  while IFS= read -r d; do
    [ -z "$d" ] && continue
    # A real dir (a symlink's -delete would walk its target's contents).
    if [ ! -d "$d" ] || [ -L "$d" ]; then continue; fi
    # In-flight guard at ANY depth: a dir's own mtime moves only when an entry
    # directly in it is added or removed, so an edit further down would not
    # show. Reported in its own field, never merged into the purge counts
    # (, guard-4178): a dir absent from every lane is otherwise
    # ambiguous between "evaluated and kept" and "never looked at".
    if [ -n "$(find "$d" -mmin "-$age_min" -print -quit 2>/dev/null)" ]; then
      STRAY_AGE_SKIPPED_DIRS="${STRAY_AGE_SKIPPED_DIRS}${d}"$'\n'
      STRAY_AGE_SKIPPED=$((STRAY_AGE_SKIPPED + 1))
      continue
    fi
    # archive-before-delete guard (): NEVER purge a stray dir that is an
    # archive-before-delete archive. A top-level RECEIPT.* (any extension, any
    # case — see _has_archive_receipt, ) or .archive-marker
    # sentinel marks a retention-immune recovery layer; destroying it as a drain
    # side-effect is the exact anti-pattern archive-before-delete.md forbids
    # (nearly lost -zeta-orphan-archive-20260713, a completed-S3-deletion
    # recovery layer). Preserve + report on stderr; do NOT count as purged.
    if _has_archive_receipt "$d"; then
      echo "temp-drain-purge: PRESERVING archive dir (RECEIPT.*/.archive-marker present): $d" >&2
      continue
    fi
    # UNPUSHED-WORK guard (): a stray GIT REPO whose worktree is
    # clean looks maximally safe by every signal this lane read before — no
    # dirty files, no RECEIPT — yet `git log --branches --not --remotes` can
    # reveal commits that exist NOWHERE else. A clean-worktree check is the
    # intuitive and precisely wrong probe. Preserve when the repo has (a) any
    # unpushed commit, (b) any dirty TRACKED file (`--porcelain -uno`:
    # untracked-only dirt is the same class as a plain stray dir's content,
    # which this lane deletes by design — and -uno keeps the probe cheap on
    # huge scratch trees, the 153k-file npmci lesson), or (c) git itself
    # cannot answer (corrupt repo — fail-closed: retaining junk is
    # recoverable, deleting sole-copy commits is not). `-e` catches both .git
    # dirs and .git files (worktrees/submodules). --no-optional-locks keeps the
    # probe a pure read: a plain `git status` rewrites .git/index whenever the
    # cached stat info is stale (measured 2026-10-05, git 2.45), which made the
    # repo "touched" for the in-flight guard above and changed the folder's
    # review fingerprint, so a decided repo could never be deleted.
    if [ -e "$d/.git" ]; then
      local _gu="" _gd="" _gbad=0
      _gu="$(git -C "$d" log --branches --not --remotes -1 --format=%H 2>/dev/null)" || _gbad=1
      _gd="$(git -C "$d" --no-optional-locks status --porcelain -uno 2>/dev/null)" || _gbad=1
      if [ "$_gbad" -eq 1 ] || [ -n "$_gu" ] || [ -n "$_gd" ]; then
        echo "temp-drain-purge: PRESERVING git dir (unpushed commits, dirty tracked files, or unreadable repo — g-115-3648): $d" >&2
        STRAY_PRESERVED_GIT="${STRAY_PRESERVED_GIT}$(basename "$d")"$'\n'
        continue
      fi
    fi
    if [ "$dry_run" -eq 0 ]; then
      case "$d" in "$temp_dir"/*) find "$d" -delete 2>/dev/null || true ;; esac
      # Counted, logged and propagated only when it is really gone: a partly
      # deleted dir would otherwise be recorded as deleted while files remain.
      if [ -e "$d" ]; then
        echo "temp-drain-purge: WARN — decided dir only partly deleted (still present): $d" >&2
        continue
      fi
    fi
    count=$((count + 1))
    # Record the DIR (trailing slash = dir marker for --purge-propagate),
    # NEVER its file list: propagation resolves the dir by S3-prefix listing,
    # so the cost scales with what is actually in the store. A local
    # enumeration here is the wrong cost model — measured 153,453 files under
    # one scratch dir (npmci-probe/, worker-box local-only, 0 S3 objects),
    # where the first draft's per-file bash string append went O(N^2) and
    # hung the dry-run smoke at 99% CPU for 8+ minutes ().
    STRAY_PURGED_PATHS="${STRAY_PURGED_PATHS}${d%/}/"$'\n'
  done <<EOF
$list
EOF
  STRAY_COUNT="$count"
  echo "$count"
}

main() {
  local DRY_RUN=0 AGE_MIN=120 DRAINED_AGE_DAYS=30
  while [ $# -gt 0 ]; do
    case "$1" in
      --dry-run) DRY_RUN=1; shift ;;
      --age-min) AGE_MIN="${2:?temp-drain-purge.sh: --age-min needs a value}"; shift 2 ;;
      --drained-age-days) DRAINED_AGE_DAYS="${2:?temp-drain-purge.sh: --drained-age-days needs a value}"; shift 2 ;;
      -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; return 0 ;;
      *) echo "temp-drain-purge.sh: unknown arg '$1'" >&2; return 2 ;;
    esac
  done
  case "$AGE_MIN" in
    ''|*[!0-9]*) echo "temp-drain-purge.sh: --age-min must be a non-negative integer, got '$AGE_MIN'" >&2; return 2 ;;
  esac
  case "$DRAINED_AGE_DAYS" in
    ''|*[!0-9]*) echo "temp-drain-purge.sh: --drained-age-days must be a non-negative integer, got '$DRAINED_AGE_DAYS'" >&2; return 2 ;;
  esac

  # Resolve paths via the canonical helper (never a caller-supplied var).
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  # shellcheck disable=SC1091
  source "$script_dir/_paths.sh"

  local temp_dir="${AGENT_DIR:-}/temp"
  # An empty AGENT_DIR yields temp_dir="/temp"; the under-PROJECT_ROOT + empty
  # agent_dir guards both catch that before any deletion.
  [ -z "${AGENT_DIR:-}" ] && temp_dir=""

  if ! assert_safe_temp_dir "$temp_dir" "${PROJECT_ROOT:-}" "${AGENT_DIR:-}"; then
    return 1
  fi

  # Soft guard: no temp dir = nothing to purge — clean no-op (fresh agent).
  # Emits the FULL field set (all-zero lane fields) so this exit path shares one
  # schema with the main path below (fresh-eyes finding: a consumer of the lane
  # fields must not KeyError on the no-temp-dir branch).
  if [ ! -d "$temp_dir" ]; then
    # The lookups are "n/a" here (no lane ran) rather than omitted: every
    # field must exist on BOTH exit paths or a strict-field consumer KeyErrors
    # on a fresh agent — the same schema-parity finding the lane fields carry.
    printf '{"purged":0,"would_purge":0,"files":[],"drained_gc_purged":0,"drained_gc_would_purge":0,"drained_gc_files":[],"stray_purged":0,"stray_would_purge":0,"stray_dirs":[],"stray_preserved_git":0,"stray_preserved_git_dirs":[],"decisions_lookup":"n/a","unmanaged_dotfiles":0,"unmanaged_dotfile_names":[],"citation_lookup":"n/a","deletions_logged":0,"deletion_log":"n/a","backend_propagation":{"skipped":"no-temp-dir"},"age_skipped":0,"age_skipped_names":[],"stray_age_skipped":0,"stray_age_skipped_dirs":[],"dry_run":%s,"age_min":%d,"drained_age_days":%d,"temp_dir":"%s","note":"temp dir does not exist"}\n' \
      "$([ "$DRY_RUN" -eq 1 ] && echo true || echo false)" "$AGE_MIN" "$DRAINED_AGE_DAYS" "$temp_dir"
    return 0
  fi

  # ── Lane 1 (decided files). Candidates come ONLY from the decision log:
  # `temp_decisions.py deletable` lists what a review decided to discard and
  # whose content still matches what was reviewed. Each file is then deleted by
  # `find "<file>" PURGE_FIND_PRED -delete`, so the guards (regular file, not a
  # dotfile, not cited, untouched for AGE_MIN) are evaluated by the delete
  # itself (guard-5952), and only what is really gone is reported, logged and
  # propagated.
  #
  # FAIL CLOSED: an unknown cited set or an unreadable decision log means Lanes
  # 1 and 3 delete NOTHING this run. Treating "unknown" as "nothing is cited"
  # or "no decisions" would delete evidence on exactly the box least able to
  # notice.
  local citation_lookup="ok" decisions_lookup="ok"
  local _cited_raw _decided_raw=""
  local _cited_arr=() _cb _dfiles=() _ddirs=() _kind _name
  if _cited_raw="$(_cited_basenames "$script_dir")"; then
    while IFS= read -r _cb; do [ -n "$_cb" ] && _cited_arr+=( "$_cb" ); done <<EOF
$_cited_raw
EOF
  else
    citation_lookup="failed"
    echo "temp-drain-purge: WARN — cited set UNKNOWN (temp-citation-ratchet.py --cited-paths failed); Lanes 1-3 delete NOTHING this run." >&2
  fi
  _purge_find_predicate "$AGE_MIN" "${_cited_arr[@]+"${_cited_arr[@]}"}"
  if _decided_raw="$(_decided_items "$script_dir" "$temp_dir")"; then
    while IFS=$'\t' read -r _kind _name; do
      [ -n "$_name" ] || continue
      case "$_kind" in
        file) _dfiles+=( "$_name" ) ;;
        dir)  _ddirs+=( "$_name" ) ;;
      esac
    done <<EOF
$_decided_raw
EOF
  else
    decisions_lookup="failed"
    echo "temp-drain-purge: WARN — decision log unreadable or a lookup it needs failed (temp_decisions.py deletable); Lanes 1 and 3 delete NOTHING this run." >&2
  fi
  if [ "$citation_lookup" != "ok" ]; then _dfiles=(); _ddirs=(); fi

  # : what the AGE GUARD held back is reported in its OWN fields,
  # never folded into `would_purge`/`files` (guard-4178: a not-applicable
  # outcome summed into the evaluated one is invisible on BOTH axes).
  local ephemera_list="" age_skipped_list="" _p
  for _name in "${_dfiles[@]+"${_dfiles[@]}"}"; do
    _p="$temp_dir/$_name"
    [ -e "$_p" ] || continue
    if [ "$DRY_RUN" -eq 0 ]; then
      find "$_p" "${PURGE_FIND_PRED[@]}" -delete 2>/dev/null || true
      if [ ! -e "$_p" ] && [ ! -L "$_p" ]; then
        ephemera_list="${ephemera_list}${_p}"$'\n'
        continue
      fi
    elif [ -n "$(find "$_p" "${PURGE_FIND_PRED[@]}" -print 2>/dev/null)" ]; then
      ephemera_list="${ephemera_list}${_p}"$'\n'
      continue
    fi
    if [ -n "$(find "$_p" "${PURGE_FIND_PRED_NOAGE[@]}" -print 2>/dev/null)" ]; then
      age_skipped_list="${age_skipped_list}${_p}"$'\n'
    fi
  done
  local count age_skipped_count files_json
  count="$(_count_lines "$ephemera_list")"
  age_skipped_count="$(_count_lines "$age_skipped_list")"
  files_json="$(_json_names "$ephemera_list")"

  # ── Retire the third-class watermark marker (2026-10-05). The gate it fed
  # is gone, and Lane 0 would report a leftover marker forever. Removed once,
  # by this guarded path, and logged like every other deletion. Only a small
  # regular file whose first line is an ISO timestamp qualifies, so a
  # same-named file holding anything else is left for a person.
  local wm_retired="" _wm="$temp_dir/.drain-watermark"
  if [ "$DRY_RUN" -eq 0 ] && [ -f "$_wm" ] && [ ! -L "$_wm" ] \
     && [ "$(wc -c < "$_wm" 2>/dev/null || echo 999)" -le 64 ]; then
    case "$(head -n 1 "$_wm" 2>/dev/null | tr -d ' \t\r')" in
      [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9])
        find "$_wm" -maxdepth 0 -type f -delete 2>/dev/null || true
        [ -e "$_wm" ] || wm_retired="$_wm" ;;
    esac
  fi

  # ── Lanes 2 & 3 (extracted → gc_drained_archive / cleanup_stray_dirs, both
  # sourceable + unit-tested). Bounded by the assert_safe_temp_dir guard already
  # passed above for temp_dir; each echoes its match count (would-purge when
  # --dry-run, else purged).
  #
  # Lane 2 FAIL-CLOSED on an unknown cited set (): the FUNCTION
  # applies the exemption, the CALLER decides what an unknown cited set means,
  # and with no way to tell cited from uncited the only safe answer is to
  # delete nothing — the direction this lane's git-tracked guard already chose
  # ("if git ls-files is unavailable or errors, the lane deletes NOTHING").
  # Retaining junk for one run is recoverable; deleting the evidence a durable
  # record cites is not.
  local gc_count stray_count gc_files_json='[]'
  GC_DRAINED_PATHS=""
  if [ "$citation_lookup" = "ok" ]; then
    # Called WITHOUT command substitution, deliberately: `$(...)` forks a
    # subshell, so the GC_DRAINED_FILES global set inside would be discarded and
    # this lane's file list would be silently empty forever — the exact
    # unverifiable-exemption shape being fixed. The count comes from the
    # GC_DRAINED_COUNT global for the same reason; the stdout `echo "$count"`
    # contract is preserved unchanged for the unit tests that capture it.
    gc_drained_archive "$temp_dir/drained" "$DRAINED_AGE_DAYS" "$DRY_RUN" \
      "${_cited_arr[@]+"${_cited_arr[@]}"}" >/dev/null
    gc_count="$GC_DRAINED_COUNT"
    gc_files_json="$(_json_names "$GC_DRAINED_FILES")"
  else
    gc_count=0
    echo "temp-drain-purge: WARN — cited set UNKNOWN; Lane 2 (drained/ GC) SKIPPED this run rather than deleting by age alone (g-306-102)." >&2
  fi
  # Called WITHOUT command substitution (Lane-2's documented idiom): a $(...)
  # subshell would discard the STRAY_PURGED_PATHS global the propagation pass
  # below consumes. Count comes back via the STRAY_COUNT global; the stdout
  # contract stays for the unit tests that capture it. Only the decided dirs
  # are passed: with none, the lane deletes nothing.
  cleanup_stray_dirs "$temp_dir" "$AGE_MIN" "$DRY_RUN" "${_ddirs[@]+"${_ddirs[@]}"}" >/dev/null
  stray_count="$STRAY_COUNT"

  # ── Record every deletion in the decision log (guard-6105: a destructive
  # operation's provenance goes to a durable ledger in the same run). Logged
  # AFTER the delete, from the sets that are really gone; a log failure does
  # not undo anything, so it is reported loudly instead of silently. The
  # decision row written at review time already holds the who and why for
  # Lanes 1 and 3; Lane 2 and the marker retirement get their reason here.
  local deletions_logged=0 deletion_log="n/a" _dl="" _lg
  if [ "$DRY_RUN" -eq 0 ]; then
    [ -n "$ephemera_list" ] && _dl="$(_kind_lines file '' "$ephemera_list")"$'\n'
    [ -n "${STRAY_PURGED_PATHS:-}" ] && _dl="${_dl}$(_kind_lines dir '' "$STRAY_PURGED_PATHS")"$'\n'
    if [ -n "$_dl" ]; then
      deletion_log="ok"
      if _lg="$(printf '%s' "$_dl" | _log_deletions "$script_dir" "$temp_dir" decided)"; then
        deletions_logged=$((deletions_logged + ${_lg:-0}))
      else deletion_log="failed"; fi
    fi
    if [ -n "${GC_DRAINED_PATHS:-}" ]; then
      [ "$deletion_log" = "n/a" ] && deletion_log="ok"
      if _lg="$(_kind_lines file 'drained/' "$GC_DRAINED_PATHS" | _log_deletions "$script_dir" "$temp_dir" drained-gc "drained/ retention: older than $DRAINED_AGE_DAYS days, uncited, untracked")"; then
        deletions_logged=$((deletions_logged + ${_lg:-0}))
      else deletion_log="failed"; fi
    fi
    if [ -n "$wm_retired" ]; then
      [ "$deletion_log" = "n/a" ] && deletion_log="ok"
      if _lg="$(printf 'file\t.drain-watermark\n' | _log_deletions "$script_dir" "$temp_dir" migration "retired marker: the drain-watermark gate was replaced by this decision log (2026-10-05)")"; then
        deletions_logged=$((deletions_logged + ${_lg:-0}))
      else deletion_log="failed"; fi
    fi
    if [ "$deletion_log" = "failed" ]; then
      echo "temp-drain-purge: WARN — deletions happened that the decision log does not fully record (temp_decisions.py log-deleted failed). Deleted this run: $(printf '%s' "$ephemera_list${STRAY_PURGED_PATHS:-}${GC_DRAINED_PATHS:-}" | tr '\n' ' ')" >&2
    fi
  fi

  # ── Lane 0 (REPORT-ONLY, ). Deletes nothing in either mode, so it is
  # unaffected by --dry-run and its count is emitted identically on both paths.
  # Called WITHOUT command substitution for the same reason Lane 2 is: `$(...)`
  # forks a subshell, so the UNMANAGED_DOTFILES global set inside would be
  # discarded and the names array would be silently empty forever.
  local dot_count dot_files_json='[' _df_first=1 _df
  report_unmanaged_dotfiles "$temp_dir" >/dev/null
  dot_count=0
  if [ -n "$UNMANAGED_DOTFILES" ]; then
    while IFS= read -r _df; do
      [ -z "$_df" ] && continue
      [ "$_df_first" -eq 0 ] && dot_files_json="$dot_files_json,"
      dot_files_json="$dot_files_json\"$_df\""
      _df_first=0
      dot_count=$((dot_count + 1))
    done <<EOF
$UNMANAGED_DOTFILES
EOF
  fi
  dot_files_json="$dot_files_json]"

  # ── Backend delete-propagation (). agents/<agent>/temp is a
  # configured sync root (OwnCloudBackend._roots; guard-3422), so a local-only
  # purge leaves every deleted file as an object in the authoritative store —
  # measured 23,125 objects / 3.33 GB for one agent against a 31-file local
  # tree. Feed the exact union of the three lanes' deleted sets to
  # owncloud_sync --purge-propagate, which delete_object()s each key
  # (guard-1493: the S3 lane; the local unlinks above are the other lane; the
  # versioned bucket is the recovery layer). FAIL-SOFT: a failed or
  # unavailable propagation degrades to the pre-fix local-only behavior and
  # is recorded in the report — it never blocks the purge, whose primary job
  # is local pressure relief. Under STORAGE_BACKEND=local the CLI no-ops
  # with empty stdout and the report records the skip. In --dry-run the CLI
  # runs with --dry-run (would_delete counts, zero backend writes).
  local all_purged_paths="" backend_prop='{"skipped":"nothing-purged"}'
  [ -n "$ephemera_list" ] && all_purged_paths="${ephemera_list}"$'\n'
  [ -n "${GC_DRAINED_PATHS:-}" ] && all_purged_paths="${all_purged_paths}${GC_DRAINED_PATHS}"
  [ -n "${STRAY_PURGED_PATHS:-}" ] && all_purged_paths="${all_purged_paths}${STRAY_PURGED_PATHS}"
  [ -n "$wm_retired" ] && all_purged_paths="${all_purged_paths}${wm_retired}"$'\n'
  if [ -n "$all_purged_paths" ]; then
    local _prop_out _prop_dry=()
    [ "$DRY_RUN" -eq 1 ] && _prop_dry=( --dry-run )
    _prop_out="$(printf '%s' "$all_purged_paths" | python3 "$script_dir/owncloud_sync.py" --purge-propagate ${_prop_dry[@]+"${_prop_dry[@]}"} 2>/dev/null || true)"
    case "$_prop_out" in
      '{'*) backend_prop="$_prop_out" ;;
      *)    backend_prop='{"skipped":"backend-not-owncloud-or-cli-unavailable"}' ;;
    esac
  fi

  # : surface the git dirs Lane 3 preserved — same builder idiom as
  # the dotfile names above; the preservation must be checkable from outside.
  local git_kept_json='[' _gk_first=1 _gk git_kept_count=0
  if [ -n "${STRAY_PRESERVED_GIT:-}" ]; then
    while IFS= read -r _gk; do
      [ -z "$_gk" ] && continue
      [ "$_gk_first" -eq 0 ] && git_kept_json="$git_kept_json,"
      git_kept_json="$git_kept_json\"$_gk\""
      _gk_first=0
      git_kept_count=$((git_kept_count + 1))
    done <<EOF
$STRAY_PRESERVED_GIT
EOF
  fi
  git_kept_json="$git_kept_json]"

  # : age-EXCLUDED sets as JSON arrays. These answer "what did the
  # guard never look at?" — the question a reader cannot otherwise ask,
  # because a skipped path is absent from every lane and absence reads as
  # safety.
  local age_skipped_json stray_age_json stray_dirs_json
  age_skipped_json="$(_json_names "$age_skipped_list")"
  stray_age_json="$(_json_names "${STRAY_AGE_SKIPPED_DIRS:-}")"
  stray_dirs_json="$(_json_names "${STRAY_PURGED_PATHS:-}")"

  local purged gc_purged stray_purged
  if [ "$DRY_RUN" -eq 1 ]; then
    purged=0; gc_purged=0; stray_purged=0
  else
    purged="$count"; gc_purged="$gc_count"; stray_purged="$stray_count"
  fi
  printf '{"purged":%d,"would_purge":%d,"files":%s,"drained_gc_purged":%d,"drained_gc_would_purge":%d,"drained_gc_files":%s,"stray_purged":%d,"stray_would_purge":%d,"stray_dirs":%s,"stray_preserved_git":%d,"stray_preserved_git_dirs":%s,"decisions_lookup":"%s","unmanaged_dotfiles":%d,"unmanaged_dotfile_names":%s,"citation_lookup":"%s","deletions_logged":%d,"deletion_log":"%s","backend_propagation":%s,"age_skipped":%d,"age_skipped_names":%s,"stray_age_skipped":%d,"stray_age_skipped_dirs":%s,"dry_run":%s,"age_min":%d,"drained_age_days":%d,"temp_dir":"%s"}\n' \
    "$purged" "$count" "$files_json" "$gc_purged" "$gc_count" "$gc_files_json" "$stray_purged" "$stray_count" \
    "$stray_dirs_json" "$git_kept_count" "$git_kept_json" "$decisions_lookup" \
    "$dot_count" "$dot_files_json" "$citation_lookup" "$deletions_logged" "$deletion_log" "$backend_prop" \
    "$age_skipped_count" "$age_skipped_json" "${STRAY_AGE_SKIPPED:-0}" "$stray_age_json" \
    "$([ "$DRY_RUN" -eq 1 ] && echo true || echo false)" "$AGE_MIN" "$DRAINED_AGE_DAYS" "$temp_dir"
  return 0
}

# Run main() ONLY when executed directly, not when sourced (so the guard is
# unit-testable via `source`).
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  main "$@"
  exit $?
fi
