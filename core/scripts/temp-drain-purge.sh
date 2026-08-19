#!/usr/bin/env bash
# temp-drain-purge.sh — canonical GUARDED purge of pure-ephemera from the bound
# agent's temp/ dir. Exists so autonomous agents NEVER hand-roll an unguarded
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
#   Lane 1 (purge-by-default): `find "$TEMP_DIR" -maxdepth 1 -type f (EVERY
#                        file except: dotfiles; content-bearing .md/.json;
#                        basenames cited by a durable record) -mmin +AGE
#                        -delete`. SSOT glob = _purge_find_predicate (see its
#                        header for the three exemptions + why the class is
#                        bounded by the predicate rather than by an extension
#                        list,  / ). -maxdepth 1 leaves
#                        drained/ (a subdir) untouched. DEGRADES to the
#                        pre-inversion allow-list (_purge_find_predicate_legacy)
#                        when the cited set cannot be determined — see
#                        _cited_basenames; the JSON reports which via
#                        "citation_lookup".
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
#                        checkable from outside.
#   Lane 3 (stray dirs): `find "$TEMP_DIR" -mindepth 1 -maxdepth 1 -type d
#                        ! -name drained -mmin +AGE_MIN` → each match guarded-
#                        deleted via `find "$stray" -delete` (re-asserted strictly
#                        under TEMP_DIR/). Removes abandoned scratch subdirs the
#                        file lanes never touch (e.g. a leftover session subdir,
#                        ).
#
# Usage: temp-drain-purge.sh [--dry-run] [--age-min N] [--drained-age-days N]
#   --dry-run           list what WOULD purge/clean, delete nothing
#   --age-min           file + stray-dir age guard in minutes (default 120; skips
#                       actively-written logs and still-active scratch dirs)
#   --drained-age-days  drained/ GC age guard in days (default 30)
# Output (stdout, JSON): {"purged":N,"would_purge":N,"files":[...],
#   "drained_gc_purged":N,"drained_gc_would_purge":N,"drained_gc_files":[...],
#   "stray_purged":N,"stray_would_purge":N,"citation_lookup":"ok"|"failed"|"n/a",
#   "dry_run":bool,"age_min":N,"drained_age_days":N,
#   "temp_dir":"..."} — the no-temp-dir no-op path emits the SAME field set
#   (all-zero lane fields, citation_lookup "n/a") so both exit paths share one
#   schema (fresh-eyes finding bravo-fec-noop-json-missing-lane-fields).
#   "files" is LANE 1; "drained_gc_files" is LANE 2 (). Lane 3 remains
#   count-only — it deletes DIRS, so there is no file basename to intersect.
#   citation_lookup=="failed" means Lane 1 ran DEGRADED (legacy allow-list, third
#   class untouched) AND Lane 2 was skipped outright — treat a low would_purge or
#   a zero drained_gc_would_purge under it as unmeasured, not clean.
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
# PURGE_FIND_PRED array with the find predicate for the Lane-1 purge. SINGLE
# SOURCE OF TRUTH for the purge glob: main() uses it for BOTH the list pass and
# the -delete pass, and test_temp_drain_purge.sh sources it to assert lane
# behavior against a synthetic temp dir (so the test can never diverge from the
# real glob).
#
# PURGE-BY-DEFAULT WITH EXEMPTIONS (). This lane was an ALLOW-LIST of
# eight extensions until 2026-07-31. Drain matches .md/.json; purge matched
# those eight plus 0-byte — so temp/'s THIRD class (the complement of two
# enumerated sets) had no lifecycle at all and was unbounded BY CONSTRUCTION,
# not by oversight. An extension list can never close it: measured cc-02
# 2026-07-31, 70 third-class files carried 21 distinct suffixes, 8 of them
# one-offs invented by a single goal (.premutation, .pre2, .mutated, .mine,
# .bak-preiam-cutover, .12, .test, .patch). Enumerating those yields a fresh
# list that is stale on the next goal. Age cannot be the gate either
# (guard-2071): measured accrual is 6.1 files/day, so any age-only window W
# leaves ~6.1*W resident (~184 at 30 days). The bound must come from the
# PREDICATE. See core/config/conventions/temp-store.md § The third class for
# the D2 decision this implements.
#
# THREE EXEMPTIONS, in predicate order:
#   (i)   DOTFILES (! -name '.*') — temp/'s only git-TRACKED file is a 0-byte
#         `.gitkeep` (preserves the dir on a fresh clone — temp-store.md); the
#         -empty lane would otherwise delete it (and any 0-byte dotfile marker)
#         once past the age guard, and iteration-commit would commit that
#         deletion ( fresh-eyes catch).
#   (ii)  DRAINABLE WORKING DOCS — .md/.json WITH CONTENT. The `-o -empty`
#         disjunct deliberately re-admits 0-BYTE .md/.json: nothing was ever
#         written, so there is nothing to drain (the pre-inversion -empty
#         sub-lane, preserved exactly).
#   (iii) THE LOAD-BEARING SET — basenames passed by the caller, each cited by
#         at least one durable record (temp-store.md § The third class (a)(1);
#         source is temp-citation-ratchet.py --cited-paths, which already
#         computes the (record, path) pairs). D2's promotion path is "wrap the
#         file in a receipted dir", which Lane 3 then preserves — so this
#         exemption is what keeps a cited-but-not-yet-wrapped loose file alive
#         long enough for someone to wrap it.
#
# Caller passes basenames from EVERY agent's temp/, not just the bound one.
# Over-exemption is the fail-safe direction, and it removes a whole failure
# mode: an agent-resolution bug could otherwise silently un-protect a cited
# file, which deletes evidence, while the cost of the broader set is at worst
# retaining a same-named uncited file.
#
# -maxdepth 1 -type f leaves drained/ (a subdir) untouched. -empty works on bfs
# (this box's find) and GNU findutils alike.
# SYNC: any change to this glob MUST update the class table in
# core/config/conventions/temp-store.md (that file mandates the joint update).
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
# honoring it disables that extension in Lane 1 entirely. Dropped LOUDLY, same
# as the sentinel case; the two branches are kept separate so "*"/"*.*" keep
# their own message and the sentinel's test hook stays reachable.
_PURGE_OVERBROAD_SENTINEL='zzz-overbroad-sentinel-9f3a2c'
_purge_find_predicate() {
  local age_min="$1"; shift
  local _b
  PURGE_FIND_PRED=( -maxdepth 1 -type f ! -name '.*' \( ! \( -name '*.md' -o -name '*.json' \) -o -empty \) )
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
  PURGE_FIND_PRED+=( -mmin "+$age_min" )
}

# _purge_find_predicate_legacy <age_min> — the PRE-INVERSION allow-list glob.
# Used by main() ONLY when the cited set could not be determined (see
# _cited_basenames). Degrading to this is strictly no-worse-than-before: it
# deletes exactly what this lane deleted prior to  and never touches
# the third class, so an unreadable world can never cause a NEW deletion.
# Also exercised directly by test_temp_drain_purge.sh so the fallback cannot
# rot unnoticed.
_purge_find_predicate_legacy() {
  local age_min="$1"
  PURGE_FIND_PRED=( -maxdepth 1 -type f ! -name '.*' \( \( -name '*.log' -o -name '*.txt' -o -name '*.py' -o -name '*.sh' -o -name '*.err' -o -name '*.raw' -o -name '*.out' -o -name '*.bak' \) -o -empty \) -mmin "+$age_min" )
}

# _cited_basenames <script_dir> — echo one basename per line for every temp/
# path cited by a durable record. Returns NON-ZERO when the cited set is
# UNKNOWN (world unreadable, script missing, python unavailable) — the caller
# MUST treat that as "fall back to the legacy allow-list", never as "nothing is
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
    _is_cited=0
    _bn="$(basename "$f")"
    for _c in ${cited_arr[@]+"${cited_arr[@]}"}; do
      [ "$_c" = "$_bn" ] && { _is_cited=1; break; }
    done
    [ "$_is_cited" -eq 1 ] && continue            # cited → keep, by the invariant
    rel="${f#"$repo_root"/}"
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
      GC_DRAINED_FILES="${GC_DRAINED_FILES}$(basename "$f")"$'\n'
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
# The allowlist is the two LIFECYCLE markers this framework writes on purpose.
# DOTFILE_ALLOWLIST overrides it (space-separated basenames) for tests.
_DOTFILE_ALLOWLIST_DEFAULT='.gitkeep .archive-marker'
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

# cleanup_stray_dirs <temp_dir> <age_min> <dry_run> — Lane 3. Remove dirs
# DIRECTLY under temp_dir that are NOT drained/ and untouched past <age_min>
# minutes (abandoned scratch subdirs the file lanes never reach). Echoes the
# match count. Each removal is bounded under temp_dir/ by a per-dir re-assert
# (defense-in-depth) then a guarded `find "$stray" -delete` — never a hand-rolled
# rm. Caller MUST have asserted temp_dir safe. Sourceable + unit-tested.
cleanup_stray_dirs() {
  local temp_dir="${1:-}" age_min="${2:-120}" dry_run="${3:-0}" list count=0 d
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
  [ -d "$temp_dir" ] || { echo 0; return 0; }
  list="$(find "$temp_dir" -mindepth 1 -maxdepth 1 -type d ! -name drained -mmin "+$age_min" 2>/dev/null || true)"
  [ -z "$list" ] && { echo 0; return 0; }
  # Iterate candidates: preserve archive-before-delete archives ();
  # `count` reflects ONLY dirs actually purged (or that WOULD purge under
  # --dry-run), never the preserved archives.
  while IFS= read -r d; do
    [ -z "$d" ] && continue
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
    count=$((count + 1))
    # Record the DIR (trailing slash = dir marker for --purge-propagate),
    # NEVER its file list: propagation resolves the dir by S3-prefix listing,
    # so the cost scales with what is actually in the store. A local
    # enumeration here is the wrong cost model — measured 153,453 files under
    # one scratch dir (npmci-probe/, worker-box local-only, 0 S3 objects),
    # where the first draft's per-file bash string append went O(N^2) and
    # hung the dry-run smoke at 99% CPU for 8+ minutes ().
    STRAY_PURGED_PATHS="${STRAY_PURGED_PATHS}${d%/}/"$'\n'
    if [ "$dry_run" -eq 0 ]; then
      case "$d" in "$temp_dir"/*) find "$d" -delete 2>/dev/null || true ;; esac
    fi
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
    # citation_lookup is "n/a" here (Lane 1 never ran) rather than omitted: the
    # field must exist on BOTH exit paths or a strict-field consumer KeyErrors
    # on a fresh agent — the same schema-parity finding the lane fields carry.
    printf '{"purged":0,"would_purge":0,"files":[],"drained_gc_purged":0,"drained_gc_would_purge":0,"drained_gc_files":[],"stray_purged":0,"stray_would_purge":0,"citation_lookup":"n/a","backend_propagation":{"skipped":"no-temp-dir"},"dry_run":%s,"age_min":%d,"drained_age_days":%d,"temp_dir":"%s","note":"temp dir does not exist"}\n' \
      "$([ "$DRY_RUN" -eq 1 ] && echo true || echo false)" "$AGE_MIN" "$DRAINED_AGE_DAYS" "$temp_dir"
    return 0
  fi

  # ── Lane 1 (purge-by-default, exemptions per _purge_find_predicate). List
  # purgeable files (for the caller's report), then delete (unless --dry-run).
  # The purge glob is the SSOT function _purge_find_predicate (see its header
  # for the three exemptions + the temp-store.md sync obligation) — used here
  # for BOTH passes so list and delete can never diverge. -maxdepth 1 -type f
  # leaves drained/ untouched.
  #
  # FAIL CLOSED on an unknown cited set. _cited_basenames returns non-zero when
  # it could not determine what is cited; degrading to the pre-inversion
  # allow-list means an unreadable world can only ever purge what this lane
  # already purged before  — never the third class. The alternative
  # (treating "unknown" as "nothing is cited") would delete cited evidence on
  # exactly the box least able to notice.
  local citation_lookup="ok"
  local _cited_raw
  local _cited_arr=() _cb
  if _cited_raw="$(_cited_basenames "$script_dir")"; then
    while IFS= read -r _cb; do [ -n "$_cb" ] && _cited_arr+=( "$_cb" ); done <<EOF
$_cited_raw
EOF
    _purge_find_predicate "$AGE_MIN" "${_cited_arr[@]+"${_cited_arr[@]}"}"
  else
    citation_lookup="failed"
    echo "temp-drain-purge: WARN — cited set UNKNOWN (temp-citation-ratchet.py --cited-paths failed); Lane 1 degraded to the pre-inversion allow-list, third class NOT purged this run." >&2
    _purge_find_predicate_legacy "$AGE_MIN"
  fi
  local ephemera_list count
  ephemera_list="$(find "$temp_dir" "${PURGE_FIND_PRED[@]}" 2>/dev/null || true)"
  if [ -z "$ephemera_list" ]; then count=0; else count="$(printf '%s\n' "$ephemera_list" | grep -c . || true)"; fi

  # Delete FROM THE CAPTURED LIST (per-file bounded find, the Lane-2 idiom),
  # not by re-running the predicate: the backend delete-propagation pass below
  # () consumes this same list, and a second predicate pass could
  # match a file that aged into eligibility between the two find runs —
  # locally deleted but never propagated, i.e. permanent remote residue by
  # construction. Driving both deletes from one list makes the local set and
  # the propagated set identical.
  if [ "$count" -gt 0 ] && [ "$DRY_RUN" -eq 0 ]; then
    local _pf
    while IFS= read -r _pf; do
      [ -n "$_pf" ] || continue
      find "$_pf" -maxdepth 0 -type f -delete 2>/dev/null || true
    done <<EOF
$ephemera_list
EOF
  fi

  # Build the files JSON array (basenames) in pure bash — temp ephemera names
  # are kebab-case with no JSON-hostile characters.
  local files_json='[' _first=1 _f _b
  if [ "$count" -gt 0 ]; then
    while IFS= read -r _f; do
      [ -z "$_f" ] && continue
      _b="$(basename "$_f")"
      [ "$_first" -eq 0 ] && files_json="$files_json,"
      files_json="$files_json\"$_b\""
      _first=0
    done <<EOF
$ephemera_list
EOF
  fi
  files_json="$files_json]"

  # ── Lanes 2 & 3 (extracted → gc_drained_archive / cleanup_stray_dirs, both
  # sourceable + unit-tested). Bounded by the assert_safe_temp_dir guard already
  # passed above for temp_dir; each echoes its match count (would-purge when
  # --dry-run, else purged).
  #
  # Lane 2 FAIL-CLOSED on an unknown cited set (), mirroring the policy
  # split Lane 1 already uses: the FUNCTION applies the exemption, the CALLER
  # decides what an unknown cited set means. Lane 1 can degrade to an allow-list
  # that is strictly no-worse-than-before; Lane 2 has no allow-list to fall back
  # to, so its only no-worse option is to delete nothing — which is also the
  # direction this lane's git-tracked guard already chose ("if git ls-files is
  # unavailable or errors, the lane deletes NOTHING"). Retaining junk for one
  # run is recoverable; deleting the evidence a durable record cites is not.
  local gc_count stray_count gc_files_json='[]'
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
    # Build the lane-2 array in pure bash, mirroring the Lane 1 idiom above
    # rather than extracting a shared helper — one call site each today, and
    # rewriting Lane 1's working builder is outside this goal.
    local _gf_first=1 _gf
    gc_files_json='['
    if [ -n "$GC_DRAINED_FILES" ]; then
      while IFS= read -r _gf; do
        [ -z "$_gf" ] && continue
        [ "$_gf_first" -eq 0 ] && gc_files_json="$gc_files_json,"
        gc_files_json="$gc_files_json\"$_gf\""
        _gf_first=0
      done <<EOF
$GC_DRAINED_FILES
EOF
    fi
    gc_files_json="$gc_files_json]"
  else
    gc_count=0
    echo "temp-drain-purge: WARN — cited set UNKNOWN; Lane 2 (drained/ GC) SKIPPED this run rather than deleting by age alone (g-306-102)." >&2
  fi
  # Called WITHOUT command substitution (Lane-2's documented idiom): a $(...)
  # subshell would discard the STRAY_PURGED_PATHS global the propagation pass
  # below consumes. Count comes back via the STRAY_COUNT global; the stdout
  # contract stays for the unit tests that capture it.
  cleanup_stray_dirs "$temp_dir" "$AGE_MIN" "$DRY_RUN" >/dev/null
  stray_count="$STRAY_COUNT"

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
  if [ -n "$all_purged_paths" ]; then
    local _prop_out _prop_dry=()
    [ "$DRY_RUN" -eq 1 ] && _prop_dry=( --dry-run )
    _prop_out="$(printf '%s' "$all_purged_paths" | python3 "$script_dir/owncloud_sync.py" --purge-propagate ${_prop_dry[@]+"${_prop_dry[@]}"} 2>/dev/null || true)"
    case "$_prop_out" in
      '{'*) backend_prop="$_prop_out" ;;
      *)    backend_prop='{"skipped":"backend-not-owncloud-or-cli-unavailable"}' ;;
    esac
  fi

  local purged gc_purged stray_purged
  if [ "$DRY_RUN" -eq 1 ]; then
    purged=0; gc_purged=0; stray_purged=0
  else
    purged="$count"; gc_purged="$gc_count"; stray_purged="$stray_count"
  fi
  printf '{"purged":%d,"would_purge":%d,"files":%s,"drained_gc_purged":%d,"drained_gc_would_purge":%d,"drained_gc_files":%s,"stray_purged":%d,"stray_would_purge":%d,"unmanaged_dotfiles":%d,"unmanaged_dotfile_names":%s,"citation_lookup":"%s","backend_propagation":%s,"dry_run":%s,"age_min":%d,"drained_age_days":%d,"temp_dir":"%s"}\n' \
    "$purged" "$count" "$files_json" "$gc_purged" "$gc_count" "$gc_files_json" "$stray_purged" "$stray_count" \
    "$dot_count" "$dot_files_json" "$citation_lookup" "$backend_prop" \
    "$([ "$DRY_RUN" -eq 1 ] && echo true || echo false)" "$AGE_MIN" "$DRAINED_AGE_DAYS" "$temp_dir"
  return 0
}

# Run main() ONLY when executed directly, not when sourced (so the guard is
# unit-testable via `source`).
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  main "$@"
  exit $?
fi
