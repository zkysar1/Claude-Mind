#!/usr/bin/env python3
# domain-leak-exempt: the dispatch table names literal AyoAI agent-ledger
# basenames (experience.jsonl, changelog.jsonl, experience-meta.json, ...) that
# this git merge driver routes by — they are real repo file names the driver
# operates on, not illustrative examples.
"""git merge driver for AyoAI append/RMW agent ledgers (merge=ayoai-ledger).

Resolves cross-box git conflicts on the record-structured agent ledgers
RECORD-LEVEL rather than line-level, instead of aborting iteration-push.sh's
integrate step. Mostly reuses the already-tested commutative primitives in
coordination_merge.py (the SAME functions the own-cloud S3 path uses).

CORRECTED 2026-09-06 (g-115-4357). This paragraph said "RECORD-LEVEL commutative
UNION" and "there is NO new merge logic here — only the git-driver protocol
glue". BOTH became false, and the second is why the first survived so long: a
reader who accepts "no new merge logic here" stops looking for merge logic to
audit, so the union rode in under coordination_merge.py's tested reputation
while git — unlike the mirror — was handing this driver a %O it discarded. There
IS new merge logic here now (_three_way_id_merge), it is git-lane-only by
necessity, and it is the piece that must be read. Do not restore either phrase.
(g-115-2767, from zeta's g-115-2727 investigation: cross-box conflicts on
experience.jsonl / experience-meta.json / changelog.jsonl aborted iteration-push
and stranded MIND commits for 2 consecutive iters g-335-157..g-335-159.)

Git invokes this as:  driver %O %A %B %P
  argv[1] = %O  ancestor/base file — READ, and load-bearing in TWO places: the
                id-union branch uses it to APPLY deletions (g-115-4357) and the
                text-merge fallback needs it. It was genuinely unused while the
                record handlers were plain 2-way unions; that is what made them
                resurrect.
  argv[2] = %A  "ours" file   — ALSO the OUTPUT path (driver writes merged here)
  argv[3] = %B  "theirs" file
  argv[4] = %P  pathname in the repo (basename drives dispatch)
Exit 0 = merged cleanly (result written to %A).
Exit 1 = could not merge -> git keeps the conflict (safe fallback; NEVER
         corrupts %A, so iteration-push surfaces it for manual union as before).

Dispatch by basename:
  experience.jsonl / experience-archive.jsonl / journal.jsonl
                        -> id-keyed 3-WAY record merge (_three_way_id_merge,
                           key=('id',); journal has no 'id' -> canon-keyed).
                           Union MINUS whatever the OTHER side deleted, decided
                           against %O; an empty base degrades exactly to the
                           historical union. NOT a plain union — that resurrected
                           sweep removals and journal rotations and reverted
                           archive edits (g-115-4357)
  experience-meta.json  -> _merge_counters (MAX-on-numeric; derived/regenerable
                           so a lossy-safe rollup self-heals on next experience-add)
  everything else       -> coordination_merge.merge_handler_for() registry
                           (changelog.jsonl, aspirations.jsonl, and any other
                           registered store share the S3-path handler), then,
                           path-scoped, a base-aware delete filter
                           (_drop_one_sided_deletes) for the agent ledgers and,
                           rotation-gated, for the world and meta changelog
  STILL unregistered    -> _validated_text_merge (see below), else exit 1

UNREGISTERED BASENAMES ARE THE COMMON CASE, NOT THE EDGE (g-115-4253). The
.gitattributes globs route .mind-data/{world,meta}/**/*.{jsonl,yaml,json} — a
population that GROWS on its own — into a dispatcher keyed by hand-enumerated
basename. Measured 2026-07-31: 167 of 253 routed files (66%) have no handler.
Each was a hard stop on first both-sides-touch, and a hard stop is not local to
the file: it leaves the path unmerged, so iteration-push.sh's integrate aborts
and the box stops integrating EVERYTHING. cc-06 stranded 54 commits for 6.2h
behind exactly one such file (backpressure.yaml) with no error anywhere.

The .gitattributes rationale claimed "AN UNREGISTERED BASENAME IS A STRICT
IMPROVEMENT, NOT A RISK ... no worse than today until then." Measured, that is
half right. Decision table for an unhandled basename (probe, 2026-07-31):

    case                        merge_rc  unmerged  markers  parses
    ROUTED   + disjoint edits      1         1         0      yes   <- REGRESSION
    UNROUTED + disjoint edits      0         0         0      yes   (both kept)
    ROUTED   + overlapping         1         1         0      yes   <- protection
    UNROUTED + overlapping         1         1         1      NO    (corrupt)

Routing is a strict improvement over the OVERLAPPING column (no marker
corruption) and a strict REGRESSION over the DISJOINT one (a clean merge became
an aborted integrate). The original measurement covered "handled / unhandled /
unrouted" but never the disjoint cell, so the regression was invisible.

WHY A VALIDATED TEXT MERGE AND NOT A DEFAULT COMMUTATIVE HANDLER. A blanket
union over unregistered stores is refuted by guard-1816 and by the registry's
own DELIBERATELY-NOT-REGISTERED list: dead-ends.jsonl and knowledge-graph.jsonl
are full-file REWRITES, so a union resurrects rows their writers deleted. Git's
3-way text merge has the opposite property — it APPLIES both sides' deletions —
which is precisely what those stores need. So the fallback is git's own merge,
accepted only when it is clean AND the result still parses, never a union.

This takes the best cell of each column: disjoint merges clean (integrate
proceeds), overlapping/add-add conflicts exit 1 with %A untouched (markers=0,
exactly today's behavior). It cannot be worse than the status quo in any
measured case.

KNOWN TRADE (guard-1871): for a DERIVED CACHE with mtime-staleness regeneration,
accepting a merge advances the cache mtime past its source and suppresses one
regeneration cycle. That window is bounded — the next source write re-fires it —
whereas the alternative it replaces strands the whole box. Recorded rather than
elided.
"""
import json
import os
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import coordination_merge as cm  # noqa: E402  (path insert must precede import)

# Basenames NOT registered in coordination_merge._HANDLERS that this driver
# handles via the lower-level primitives. Registered basenames (changelog.jsonl,
# aspirations.jsonl, ...) fall through to merge_handler_for below.
_JSONL_ID_UNION = {"experience.jsonl", "experience-archive.jsonl", "journal.jsonl"}
_COUNTER_JSON = {"experience-meta.json"}


def _parse_jsonl(b: bytes) -> list:
    """Parse jsonl bytes into a list of records (blank lines skipped)."""
    out = []
    for line in b.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


def _dump_jsonl(records: list) -> bytes:
    """Serialize records back to jsonl bytes (one compact object per line).

    The union keeps whole records unchanged (it does not re-key them), so each
    record's own key order is preserved by json.dumps(sort_keys=False).

    The bytes are coordination_merge's, which are those of every writer of these
    ledgers: json.dumps(rec, ensure_ascii=True) + "\\n" (experience_write.py,
    BYTE-COMPATIBILITY). Until 2026-10-07 this dumped ensure_ascii=False, so each
    git merge rewrote every non-ASCII record raw and the next store write escaped
    them all again. Measured: 37 counter bumps reached git as a 350-line diff of
    agents/alpha/experience.jsonl (rb-12090 measured the same re-serialization
    from the reading side)."""
    return cm._dump_jsonl(records)


# retrieval_stats fields every writer only ever RAISES (both callers of
# _experience_stats_spool.record, retrieve.py and utilization-feedback.py, pass
# delta=1; a new record starts at 0). A lower value on one side is that side being
# BEHIND, never an edit -> numeric MAX. Explicit on purpose (guard-1153): the
# 10,169-record census (14 agents/*/experience*.jsonl blobs at bcc7897930) found
# one-off audit ints stuffed into retrieval_stats (tree_nodes_loaded, guardrails,
# ...) that are snapshots, not counters, so they ride the picked record like
# every other field.
_STATS_MAX_FIELDS = ("retrieval_count", "times_useful", "times_noise",
                     "times_inferred_useful")


def _merge_retrieval_stats(pick: dict, a: dict, b: dict) -> dict:
    """Return ``pick`` with its retrieval_stats merged per field from a and b.

    counters in _STATS_MAX_FIELDS -> MAX; last_retrieved -> newest non-null;
    utility_ratio -> RECOMPUTED as times_useful / max(retrieval_count, 1), the
    rule the spool and update_field apply. It is derived and goes DOWN as
    retrieval_count grows, so it is never max-merged (guard-1153). Any other
    field keeps pick's value. Symmetric in (a, b), so both vantages still write
    the same bytes. A record without a retrieval_stats dict on both sides is
    returned unchanged (g-306-525).
    """
    sa, sb = a.get("retrieval_stats"), b.get("retrieval_stats")
    if not isinstance(sa, dict) or not isinstance(sb, dict):
        return pick
    picked = pick.get("retrieval_stats")
    merged = dict(picked) if isinstance(picked, dict) else {}
    for f in _STATS_MAX_FIELDS:
        nums = [v for v in (sa.get(f), sb.get(f))
                if isinstance(v, int) and not isinstance(v, bool)]
        if nums:
            merged[f] = max(nums)
    dates = [v for v in (sa.get("last_retrieved"), sb.get("last_retrieved"))
             if isinstance(v, str)]
    if dates:
        merged["last_retrieved"] = max(dates)
    rc, tu = merged.get("retrieval_count"), merged.get("times_useful")
    if (("utility_ratio" in sa or "utility_ratio" in sb)
            and isinstance(rc, int) and not isinstance(rc, bool)
            and isinstance(tu, int) and not isinstance(tu, bool)):
        merged["utility_ratio"] = round(tu / max(rc, 1), 4)
    if merged == picked:
        return pick
    out = dict(pick)
    out["retrieval_stats"] = merged
    return out


def _three_way_id_merge(ours: list, theirs: list, base: list,
                        key_fields=("id",)) -> list:
    """3-way record merge: union MINUS the records the OTHER side deleted.

    Keep record r iff (r is on BOTH sides) OR (r is not in the base).
      * on both sides            -> keep (unchanged, or concurrently added)
      * on one side, NOT in base -> that side ADDED it        -> keep
      * on one side, IS in base  -> the other side DELETED it -> DROP

    RETRIEVAL_STATS IS MERGED PER FIELD on every both-sides arm (g-306-525).
    Each arm picks a WHOLE record, and the both-edited / no-base arm picks by
    `co > ct`. That is a commutative but semantically blind canonical compare:
    null sorts above any date, and "99" above "100". Measured over 69 carrier
    merges (2026-09-24..27): 164 both-edited experience records, every one of
    them differing ONLY in retrieval_stats; in one of them the pick lost counts
    (retrieval_count first). Separately, a Body's OWN integrate (e7cea9a844, no
    base) kept retrieval_count 0 / last_retrieved null over 1 / '2026-09-27', and
    the next carrier drain read that as a one-sided edit and applied it
    (rb-12157).
    _merge_retrieval_stats now decides those fields whichever side is picked:
      * _STATS_MAX_FIELDS -> max (writers only raise them)
      * last_retrieved    -> newest non-null
      * utility_ratio     -> recomputed; it is decrement-able, so never max
                             (guard-1153)
    Every other field still follows the arm's pick.

    WHY THIS IS NOT A PLAIN UNION (g-115-4357). A union keeps everything present
    on either side, so a record one box REMOVED and a stale peer still holds
    comes back -- silently, reporting success. That is true of an ID-keyed union
    exactly as much as of a line-keyed one: id-keying changes what counts as a
    DUPLICATE, never whether a one-sided record SURVIVES. The .gitattributes
    rationale for routing these basenames here asserted the opposite ("unions
    them by record/counter rather than by line -- so they self-heal ... without
    resurrecting pruned/edited history"); that premise was measured FALSE on all
    three routed basenames. archive_sweep removals and journal rotations both
    resurrected, and an experience-archive set_field edit was silently REVERTED
    to its pre-edit form. guard-1068 forbids exactly this ("a union RESURRECTS
    records archived on one side but still active on the other"); guard-1005
    prescribes the archival-paired remedy, which this implements within the
    per-file constraint git gives a merge driver.

    EMPTY BASE DEGRADES EXACTLY TO THE OLD UNION. git supplies an empty %O on an
    add/add, where no record can be in the base, so every record takes the
    "not in base" arm and nothing is dropped -- precisely the shape the original
    union was right about. That is what keeps this change ranking-neutral for
    every merge that does not involve a removal. ONE EXCEPTION (g-306-525): a
    record on both sides whose retrieval_stats differ gets them merged per field,
    because the union's pick could keep the side that was BEHIND. So
    byte-identity to the legacy union holds only where retrieval_stats agree.

    COMMUTATIVE AND BYTE-IDENTICAL (guard-907, analogous — scope note below;
    g-115-4357). Every branch is symmetric in the `ours` and `theirs`
    arguments and every tiebreak is a function of CONTENT, never of the
    local-vs-remote role. Output is sorted by canonical form, and the
    equal-canon case picks the smaller RAW serialization, so two records that
    are canonically equal but differ in key ORDER still yield identical bytes
    from either machine vantage (_canon sort_keys-normalises, _dump_jsonl does
    not, so canon-equality alone is not byte-equality).

    guard-907 states this requirement, but its scope is the MIRROR lane —
    verbatim, handlers "registered in core/scripts/coordination_merge.py
    _HANDLERS", triggered by editing or registering one. This helper is neither,
    so guard-907 is ANALOGOUS here, not governing; an earlier revision of this
    docstring cited it as though it governed. The git-lane reason is its own and
    is why the property is still mandatory: two boxes merge the same pair from
    opposite vantages, so asymmetric output means each box writes different bytes
    for the same merge and the file re-conflicts on the next exchange instead of
    converging — the same end state guard-907 describes for the ETag-fenced PUT
    loop, reached by a different route.
    """
    def _key(item):
        if isinstance(item, dict):
            for kf in key_fields:
                if kf in item:
                    return (kf, cm._canon(item[kf]))
        return ("_canon", cm._canon(item))

    def _raw(item):
        return json.dumps(item, ensure_ascii=False)

    ours_k = {_key(r): r for r in ours}
    theirs_k = {_key(r): r for r in theirs}
    base_k = {_key(r): r for r in base}

    out = []
    for k in set(ours_k) | set(theirs_k):
        o_has, t_has, b_has = k in ours_k, k in theirs_k, k in base_k
        if o_has and t_has:
            o, t = ours_k[k], theirs_k[k]
            co, ct = cm._canon(o), cm._canon(t)
            if co == ct:
                out.append(o if _raw(o) <= _raw(t) else t)
            elif b_has and cm._canon(base_k[k]) == ct:
                out.append(_merge_retrieval_stats(o, o, t))   # only OUR side edited it
            elif b_has and cm._canon(base_k[k]) == co:
                out.append(_merge_retrieval_stats(t, o, t))   # only THEIR side edited it
            else:   # both edited, or no base
                out.append(_merge_retrieval_stats(o if co > ct else t, o, t))
        elif b_has:
            continue                       # one side only AND in base -> deleted by the other
        else:
            out.append(ours_k[k] if o_has else theirs_k[k])   # genuine one-sided add
    return sorted(out, key=cm._canon)


def _is_agent_ledger_delete_aware(pathname: str) -> bool:
    """True for the two ledger-routed AGENT stores that reach the registry
    fall-through in ``merge_bytes`` rather than the base-aware branch above.

    Exactly ``agents/<name>/aspirations.jsonl`` and ``agents/<name>/changelog.jsonl``. Both are
    routed to this driver by .gitattributes, both have a REMOVAL path, and
    neither basename is in ``_JSONL_ID_UNION`` -- they resolve through
    ``cm.merge_handler_for``, whose signature is ``(local, remote) -> bytes`` and
    which therefore cannot see %O (g-115-9132).

    PATH-SCOPED, NOT BASENAME-SCOPED, and that is the whole reason this is a
    predicate rather than a set entry. ``aspirations.jsonl`` also names the FLEET's primary
    work queue, which reaches this driver through the
    ``.mind-data/world/**/*.jsonl`` route. Delete-propagation there would
    contradict a decision made deliberately and documented at length in
    ``coordination_merge.merge_aspirations``: the world queue represents a removal
    OUT of band -- archival writes the record into the archive store, and the
    daemon archive_sweep -> _reconcile_resurrected repairs from it on the
    reducer's cadence. Scoping to ``agents/`` keeps every world QUEUE merge
    byte-identical. The world and meta changelog are delete-aware through their
    own, rotation-gated predicate (``_is_rotated_changelog``), never this one.

    The agent-local queue has no such remedy WIRED. Measured 2026-09-06 (alpha
    worker Body, cc-10, g-115-9132), three signals: ``agent-aspirations-archive.sh`` -- the
    wrapper that forces ``--source agent`` -- has ZERO invoking callers anywhere
    outside itself, only a convention table row whose cadence column is ``--``
    and one agent's experience note; a repo-wide grep for any caller passing
    ``--source agent`` to the sweep returns none; and the positive control shows
    the WORLD wrapper invoked from aspirations-consolidate and
    aspirations-evolve. So for the agent queue the out-of-band remedy exists as a
    script and runs never, which is what makes repairing at merge time the only
    repair there is."""
    parts = pathname.replace("\\", "/").split("/")
    return (
        len(parts) >= 3
        and parts[-3] == "agents"
        and parts[-1] in ("aspirations.jsonl", "changelog.jsonl")
    )


def _is_rotated_changelog(pathname: str) -> bool:
    """True for the world and meta changelog, whose removals count only when they
    have a ROTATION's shape (``_drop_one_sided_deletes(rotation_only=True)``).

    Exactly ``.mind-data/world/changelog.jsonl`` and
    ``.mind-data/meta/changelog.jsonl``, both routed here by the
    ``.mind-data/{world,meta}/**/*.jsonl`` lines of .gitattributes. Their
    writer-side delete path is store-hygiene.yaml's rotation: the world log keeps
    its newest 20000 lines and archives the rest first, and the meta log caps at
    its newest 10000. The registry handler is a line union with no %O, so a merge
    with a peer still holding the pre-rotation copy put every rotated line back,
    and nothing repairs that out of band, because the rotation archive stays
    untracked by design (guard-2085). Measured on a downstream deployment that
    tracks its world in git (msg-20260929-075602-omni-3403): 380,253 of 493,178
    live rows predated the last rotation, and the file grew 2.4-2.8 MiB a day
    toward the push size limit. The world goal queue stays out: see
    ``_is_agent_ledger_delete_aware`` for why its removals are repaired out of band.

    WHY NOT THE AGENT RULE. It honours any one-sided removal of an untouched
    record, so it cannot tell a rotation from a truncation, and a failed integrate
    has committed a one-row stub of this very file (g-115-11674). The agent rule
    reads that stub as the deletion of the whole history and keeps it. A rotation
    has a shape a truncation lacks: it removes a FRONT SLICE of the base and keeps
    everything after the cut, starting at a base record, in a window no smaller
    than any rotation leaves. ``coordination_merge._front_evicted_lines`` tests
    exactly that shape for the board channels (g-358-81, g-358-119); here it runs
    against %O. Every other shape keeps the union, so a misread costs today's
    resurrection, never a loss."""
    parts = pathname.replace("\\", "/").split("/")
    return (
        len(parts) >= 3
        and parts[-3] == ".mind-data"
        and parts[-2] in ("world", "meta")
        and parts[-1] == "changelog.jsonl"
    )


def _drop_one_sided_deletes(merged: bytes, ours: bytes, theirs: bytes,
                            base: bytes, key_fields=("id",),
                            rotation_only=False) -> bytes:
    """Filter a two-way handler's OUTPUT so a one-sided removal is not undone.

    The registry handlers this composes with are unions with no %O: they keep
    everything either side holds, so a record one box REMOVED and a stale peer
    still carries comes back. This restores delete-propagation WITHOUT replacing
    the handler, so its field-level merge semantics survive intact --
    merge_aspirations still reconciles aspirations and their goals field by
    field, merge_append_only_jsonl still sorts chronologically. That composition
    is the point: the handler decides what each surviving record LOOKS like, this
    decides which records survive at all.

    THE RULE IS STRICTER THAN ``_three_way_id_merge``'s, deliberately. That one
    drops any record present on exactly one side and in the base. This one also
    requires the SURVIVING side's record to be canonically IDENTICAL TO BASE --
    genuinely untouched. When the surviving side EDITED the record, the edit wins
    over the removal. The asymmetry is not a stylistic preference: an aspiration
    may legitimately REOPEN after archival, and
    ``_aspirations_resurrection.classify`` exempts exactly that case as
    ``post_archive_work`` (asp-328 is a measured live instance). A blanket drop
    would silently discard a reopen made concurrently with another box's
    archival, which is the unrecoverable direction.

    KEYING MATCHES EACH HANDLER'S OWN DEDUP GRANULARITY BY CONSTRUCTION, via the
    same ``_key`` shape ``_three_way_id_merge`` uses: an id when the record
    carries one, else the whole record's canonical form. That is not a
    convenience -- the two stores dedup differently and both are correct. The
    agent queue's records are id-keyed (``asp-NNN``). The changelog's records
    carry NO id at all (measured: the fields are timestamp / agent / file /
    action / summary / lines_changed) and merge_append_only_jsonl dedups by
    SERIALIZED LINE, so the whole-record fallback IS its key. One key function
    therefore serves both, and a future store keyed either way needs no change
    here.

    EMPTY OR UNPARSEABLE BASE DEGRADES EXACTLY TO THE HANDLER'S OWN OUTPUT. git
    supplies an empty %O on an add/add, where no record can be in the base, so
    the drop set is empty and this returns ``merged`` unchanged -- the same
    ranking-neutral property ``_three_way_id_merge`` documents. Any parse failure
    on any input takes the same path: this filter can only ever REMOVE records,
    so failing toward removing NOTHING is the conservative direction.

    SURVIVING LINES PASS THROUGH VERBATIM, never re-serialized. The handler's
    exact bytes for each kept record are preserved, so the fenced-PUT
    byte-identity and commutativity properties the handlers guarantee are not
    disturbed. Commutative itself: the drop set is a function of (base, ours,
    theirs) computed symmetrically in ours/theirs, and the output order is the
    handler's.

    ROTATION_ONLY (g-115-11474) narrows the drop set to what a ROTATION removed:
    base records inside a front slice that ``cm._front_evicted_lines`` accepts
    for the side no longer holding them. Records are compared in canonical form,
    so a writer's key order or escaping cannot hide a cut. A removal of any other
    shape (a stub, a lost tail, a short copy) drops nothing."""
    def _key(item):
        if isinstance(item, dict):
            for kf in key_fields:
                if kf in item:
                    return (kf, cm._canon(item[kf]))
        return ("_canon", cm._canon(item))

    try:
        base_recs = _parse_jsonl(base)
    except (ValueError, UnicodeDecodeError):
        return merged
    if not base_recs:
        return merged
    try:
        ours_recs = _parse_jsonl(ours)
        theirs_recs = _parse_jsonl(theirs)
    except (ValueError, UnicodeDecodeError):
        return merged
    ours_k = {_key(r): r for r in ours_recs}
    theirs_k = {_key(r): r for r in theirs_recs}
    base_k = {_key(r): r for r in base_recs}

    drop = set()
    for k, brec in base_k.items():
        o_has, t_has = k in ours_k, k in theirs_k
        if o_has == t_has:
            continue                       # on both sides, or gone from both
        survivor = ours_k[k] if o_has else theirs_k[k]
        if cm._canon(survivor) == cm._canon(brec):
            drop.add(k)                    # untouched on one side, removed on the other
    if rotation_only and drop:
        base_lines = [cm._canon(r) for r in base_recs]
        rotated_out = set()
        for side in (ours_recs, theirs_recs):
            rotated_out |= cm._front_evicted_lines(
                base_lines, [cm._canon(r) for r in side])
        drop &= {_key(json.loads(line)) for line in rotated_out}
    if not drop:
        return merged

    kept = []
    for line in merged.decode("utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            kept.append(line)              # not ours to judge -- pass it through
            continue
        if _key(rec) not in drop:
            kept.append(line)
    return ("\n".join(kept) + ("\n" if kept else "")).encode("utf-8")


def _wellformed(pathname: str, data: bytes) -> bool:
    """True when ``data`` is still well-formed for ``pathname``'s format.

    A textually-clean 3-way merge can be SEMANTICALLY wrong, so a clean rc from
    git is not sufficient evidence to accept the result. Two shapes are checked:

      * it must parse at all (a merge that produced invalid JSON/YAML is a
        corruption we must not write);
      * for YAML, no DUPLICATE mapping key anywhere. Both sides inserting the
        same key at different offsets merges cleanly at line level, and
        yaml.safe_load silently keeps the LAST — a one-side write dropped with
        no sound. compose_all walks the node tree without constructing objects,
        so this stays cheap and alias-safe.

    An extension with no validator returns False: refusing is the conservative
    direction and costs nothing, since the routing globs only cover these three.
    """
    ext = os.path.splitext(pathname)[1].lower()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if ext == ".jsonl":
        for line in text.splitlines():
            if line.strip():
                try:
                    json.loads(line)
                except ValueError:
                    return False
        return True
    if ext == ".json":
        try:
            json.loads(text or "{}")
        except ValueError:
            return False
        return True
    if ext in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError:
            return False
        try:
            docs = list(yaml.compose_all(text))
        except Exception:  # noqa: BLE001 - any parse failure disqualifies
            return False

        def _no_dupe_keys(node) -> bool:
            if isinstance(node, yaml.MappingNode):
                seen = set()
                for k, v in node.value:
                    if isinstance(k, yaml.ScalarNode):
                        if k.value in seen:
                            return False
                        seen.add(k.value)
                    if not _no_dupe_keys(v):
                        return False
            elif isinstance(node, yaml.SequenceNode):
                for child in node.value:
                    if not _no_dupe_keys(child):
                        return False
            return True

        return all(_no_dupe_keys(d) for d in docs if d is not None)
    return False


def _validated_text_merge(pathname: str, base: bytes, ours: bytes,
                          theirs: bytes) -> "bytes | None":
    """Git's own 3-way text merge, or None when it must not be accepted.

    None on ANY doubt — conflicts, a git failure, or a result that no longer
    parses — so the caller falls through to the historical exit-1 and %A is
    left untouched.

    %O was "finally load-bearing HERE, and the record handlers above are 2-way
    commutative and never needed it" until g-115-4357. The second clause was the
    false half: they were 2-way, they DID need a base, and not having one is what
    made them resurrect. `_three_way_id_merge` is now base-aware too, so %O is
    load-bearing in BOTH places (see the module docstring's argv[1] entry)."""
    try:
        with tempfile.TemporaryDirectory() as d:
            paths = {}
            for name, blob in (("ours", ours), ("base", base), ("theirs", theirs)):
                paths[name] = os.path.join(d, name)
                with open(paths[name], "wb") as f:
                    f.write(blob)
            # rc is the CONFLICT COUNT (255 on error); only 0 is acceptable.
            proc = subprocess.run(
                ["git", "merge-file", "-p", paths["ours"], paths["base"],
                 paths["theirs"]],
                capture_output=True,
            )
    except Exception:  # noqa: BLE001 - git missing/unusable -> fall through
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout if _wellformed(pathname, proc.stdout) else None


def merge_bytes(pathname: str, ours: bytes, theirs: bytes,
                base: bytes = b"") -> bytes:
    """Return the merged bytes for ``pathname`` given the ours/theirs contents.

    Raises when no handler matches AND the text-merge fallback declines — the
    caller maps any exception to exit 1 (git keeps the conflict; %A is never
    overwritten)."""
    bn = os.path.basename(pathname)
    if bn in _JSONL_ID_UNION:
        # %O is load-bearing HERE too, not only in the text-merge fallback: it is
        # the only signal that separates "the peer never had this record" from
        # "the peer DELETED it". An unparseable/absent base degrades to [] , i.e.
        # exactly the historical union -- the conservative direction (g-115-4357).
        try:
            base_recs = _parse_jsonl(base)
        except ValueError:
            base_recs = []
        merged = _three_way_id_merge(
            _parse_jsonl(ours), _parse_jsonl(theirs), base_recs,
            key_fields=("id",)
        )
        return _dump_jsonl(merged)
    if bn in _COUNTER_JSON:
        a = json.loads(ours.decode("utf-8") or "{}")
        b = json.loads(theirs.decode("utf-8") or "{}")
        # The writers' bytes: json.dump(meta, indent=2, ensure_ascii=True) + "\n"
        # (experience_write.py, experience.py write_json), as for the jsonl above.
        return (
            json.dumps(cm._merge_counters(a, b), ensure_ascii=True, indent=2) + "\n"
        ).encode("utf-8")
    handler = cm.merge_handler_for(pathname)
    if handler is not None:
        merged = handler(ours, theirs)
        # The registry handlers take (local, remote) and never see %O, so
        # they cannot tell "the peer never had this record" from "the peer
        # DELETED it" -- the guard-1068 resurrection class, still live on the
        # two ledger-routed AGENT stores that reach this fall-through rather
        # than the base-aware branch above (g-115-9132), and on the world and
        # meta changelog, where only a rotation counts (g-115-11474). Compose
        # rather than replace: the handler keeps its field-level merge, this
        # restores the delete.
        if _is_agent_ledger_delete_aware(pathname):
            merged = _drop_one_sided_deletes(merged, ours, theirs, base)
        elif _is_rotated_changelog(pathname):
            merged = _drop_one_sided_deletes(merged, ours, theirs, base,
                                             rotation_only=True)
        return merged
    merged = _validated_text_merge(pathname, base, ours, theirs)
    if merged is not None:
        return merged
    raise ValueError(f"no ayoai-ledger handler for basename {bn!r}")


def main(argv) -> int:
    if len(argv) < 4:
        sys.stderr.write("git-merge-ayoai-ledger: expected args %O %A %B [%P]\n")
        return 1
    base_path, ours_path, theirs_path = argv[1], argv[2], argv[3]
    pathname = argv[4] if len(argv) > 4 else ours_path
    try:
        with open(ours_path, "rb") as f:
            ours = f.read()
        with open(theirs_path, "rb") as f:
            theirs = f.read()
        # %O is absent/empty on an add/add. Read it best-effort — the fallback
        # treats an empty base as "both sides added everything", which conflicts
        # and declines, i.e. exactly the pre-fix behavior for that shape.
        try:
            with open(base_path, "rb") as f:
                base = f.read()
        except OSError:
            base = b""
        merged = merge_bytes(pathname, ours, theirs, base)
        # Write ONLY after a successful merge — on any failure %A is untouched,
        # so git keeps the conflict rather than us corrupting the ledger.
        with open(ours_path, "wb") as f:
            f.write(merged)
        return 0
    except Exception as e:  # noqa: BLE001 — never corrupt; signal conflict instead
        sys.stderr.write(f"git-merge-ayoai-ledger: {pathname}: {e}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
