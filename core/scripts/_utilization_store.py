"""Reader seam + counter spool for the reasoning-bank / guardrails split ().

THE READER HALF LANDED FIRST, AND THAT ORDERING IS THE POINT — exactly as
`_gate_log.firings_paths` did for gate-firings (g-328-38). Every reader below is
byte-identical to today's behaviour while no sidecar and no segments exist:
`store_paths` returns the single legacy file, `load_counters` returns {}, and
`utilization_of` falls through to the embedded field. A consumer could be
converted before any writer existed and behave exactly as it did before.

THE WRITER HALF (`record_increment` and friends, at the foot of this file) landed
2026-08-18 and is DEFAULT-OFF behind `UTILIZATION_COUNTERS_SPOOLED`. This
docstring opened with "READER-ONLY, AND THAT IS THE POINT" until then; that
sentence is now false and is corrected rather than deleted, because the ordering
it was defending is still the live constraint — the flag gates the FLIP, not the
BUILD, and nothing may flip it until `store-cutover-check.sh --store utilization`
reports SAFE on every box.

WHY THE WRITER LIVES HERE RATHER THAN IN THE FLUSH SCRIPT: this module already
argues the case for itself under `segment_name` — "the writer's filename and the
reader's matcher are two halves of one contract, and when they drift the failure
is silent." A spool basename chosen in one file and excluded from sync in another
is that same drift with a longer fuse, so the name, the flag and the append all
sit beside the matcher that has to agree with them. It is the shape `_gate_log`
already proves: `segment_name`, `segmented_enabled`, `store_name` and the `log()`
writer all share one module.

WHY THE SPLIT (g-358-02 feasibility read — cited, not re-derived):
  world/reasoning-bank.jsonl  1658 PUTs/day x 20.7MB = 34.39 GB/day, ~93% of
                              writes being utilization-counter increments
  world/guardrails.jsonl      1968 PUTs/day x  9.1MB = 17.97 GB/day, ~94%
Every increment goes POST /v1/store/increment -> endpoints/store.py increment()
-> file_locks.locked_rmw, a whole-file read-modify-write, which under own-cloud
is a whole-object S3 GET+PUT. Re-verified 2026-08-15 (alpha, cc-07): the objects
measure 20.46MB / 9.37MB, within a few percent of the figures above, so the
premise still holds (guard-3868 — a user-authorized goal's premise ages
independently of its authorization).

WHY NOT JUST BATCH THE INCREMENTS, which is the obvious cheaper fix: because the
fleet already ran that experiment and it is not sufficient. meta/gate-firings.jsonl
ALREADY spools and batches (g-115-2405) and still burns 16.15 GB/day at 247
flushes x 64.2MB, because batching divides the write COUNT and leaves the object
SIZE alone. Batching only pays when the object is small. Hence two independent
halves, each attacking one factor:
  (a) counters -> a SMALL sidecar (measured: rb id+utilization 2.14MB,
      guardrails 1.02MB), so the frequent write touches a small object;
  (b) content -> DATE SEGMENTS, so the ~112-117 adds/day append to a small live
      segment instead of rewriting the whole store.

TWO READERS, TWO RETURN SHAPES, deliberately:
  `store_paths` returns PATHS because the content-store consumers have genuinely
  different parse/filter needs and a shared record-reader would mean rewriting
  several working parse loops (the _gate_log rationale, which applies unchanged).
  `load_counters` returns a DICT because the counter join is the same lookup for
  every consumer — id -> counters — so sharing it removes work rather than
  creating it.

NO DAEMON TWIN, BY CONSTRUCTION. `mind_api/src` imports core/scripts modules
directly (the daemon already does this with `_gate_log`), so this file is the
single implementation for both sides. That satisfies guard-2323 by leaving
nothing to drift, rather than by maintaining a second copy in parity — and the
daemon IS a real consumer (endpoints/retrieve.py reads `rec.get("utilization")`).

DONE SINCE, and previously listed here as required-before-writer:
  * `coordination_merge.merge_handler_for` grew its PATH-PATTERN branch for
    segment basenames (g-358-05 step 2, 2026-08-16) — segment names are dynamic
    and so unenumerable in the basename dict, exactly like the team-state shard
    branch. Without it a segment would be an unregistered store that the backend
    safe-freezes (governed-store-write-classes.md class (b): no reconciler below
    the write, so a stale fence is a PERMANENT wedge). It imports `_segment_re`
    from here rather than re-typing the date shape.

NOT DONE HERE, and required before any writer flips:
  * the attestation gate — reader-capable code must reach EVERY box AND
    downstream Claude-Mind/ZDS-Mind via promotion before the writer moves, or a
    box running old code reads a short window and reports it as the whole store.
    THE TEMPLATE IS NOW A FUNCTION, NOT A FILE TO FORK (g-115-6589): run
    `bash core/scripts/store-cutover-check.sh --store utilization`. This store's
    parameters already live in store-cutover-check.py STORES["utilization"], so
    there is nothing to copy — and a box is proven from git ancestry + consumer
    byte-identity rather than by remembering to run a chore, which is what left
    the gate-firings cutover starving 3 days (g-115-6243, rb-8202).
"""

import datetime as _dt
import json as _json
import os as _os
import re as _re
import sys as _sys
from pathlib import Path as _Path

# Single source of truth for WORLD_DIR — same resolver every other script uses.
# Do NOT re-implement local-paths.conf parsing here.
from _paths import WORLD_DIR

# The two stores this seam covers. Both live at the top level of WORLD_DIR.
KINDS = ("reasoning-bank", "guardrails")


def _check_kind(kind):
    if kind not in KINDS:
        raise ValueError(
            "unknown kind %r — expected one of %s" % (kind, ", ".join(KINDS)))


# Segments are matched by a STRICT date-shaped pattern, never a loose
# `<kind>-*.jsonl` glob. This is not defensiveness, it is the only thing standing
# between this reader and the store's own archive: `reasoning-bank-archive.jsonl`
# and `guardrails-archive.jsonl` EXIST TODAY and both match the loose form. A
# loose glob would silently fold every archived record back into the live store
# — for reasoning-bank that is 306 retired records reappearing as active.
# Matching the exact segment shape excludes non-segments by construction, rather
# than by an enumerated denylist that has to be kept in sync with every future
# sibling file (the _gate_log spool lesson, where exactly such a denylist was
# structurally dead and nobody noticed).
def _segment_re(kind):
    _check_kind(kind)
    return _re.compile(r"^" + _re.escape(kind) + r"-\d{4}-\d{2}-\d{2}\.jsonl$")


def segment_name(kind, day):
    """Basename of the date segment covering `day`.

    Defined HERE, immediately beside `_segment_re`, rather than in the future
    writer: the writer's filename and the reader's matcher are two halves of one
    contract, and when they drift the failure is silent — the writer keeps
    emitting files the reader does not recognise, so consumers read a partial
    store and report it as the whole thing. One definition, imported by both,
    makes that drift impossible rather than merely unlikely.

    `day` is required rather than defaulting to today: this module is imported by
    the daemon, and a module-level "now" is the class of bug where a long-lived
    process keeps writing yesterday's segment. Callers pass an explicit date.
    Dates are UTC wall clock (TZ=UTC fleet-wide), matching the `ts` fields
    consumers window on.
    """
    _check_kind(kind)
    return "%s-%s.jsonl" % (kind, day.isoformat())


def counters_name(kind):
    """Basename of the counter sidecar for `kind`.

    Deliberately NOT date-shaped, so `_segment_re` can never match it — the
    sidecar is a single current-state file (id -> counters), not a time series.
    """
    _check_kind(kind)
    return "%s-utilization.jsonl" % kind


def _resolved_base(world_dir):
    resolved = world_dir if world_dir is not None else WORLD_DIR
    if resolved is None:
        # Say so on stderr rather than returning empty silently. A consumer that
        # reads zero records concludes the store is empty — for these two stores
        # that means "no guardrails apply" and "no prior reasoning exists", which
        # is the worst available failure direction: it reads as a clean all-clear.
        # Matching the _gate_log precedent means matching its loudness, not just
        # its return value.
        print("[_utilization_store] WORLD_DIR unresolved — store not "
              "enumerable; returning nothing", file=_sys.stderr)
        return None
    return _Path(resolved)


def _backend_names(base):
    """Basenames the STORE OF RECORD holds directly under `base`, else None.

    None means "no backend view available" — the caller falls back to the local
    filesystem, which under a read-through cache is a LOWER BOUND, never an
    authoritative enumeration.

    The two failure modes are deliberately NOT treated alike. A ValueError means
    `base` is not under a configured root, i.e. an explicit foreign base such as
    a tmp world in a test — expected, and silent, because a tmp world has no
    backend copy and the local view IS complete for it. Anything else (a missing
    ListBucket grant, a network fault) is an anomaly that makes the enumeration
    silently short, so it says so on stderr, matching `_resolved_base`'s loudness
    for the same class of "you are about to read less than the whole store".
    """
    try:
        from storage_backend import get_backend
        return set(get_backend().list_dir(base))
    except ValueError:
        return None
    except Exception as exc:  # noqa: BLE001 — any backend fault -> local view
        print("[_utilization_store] backend enumeration unavailable at %s "
              "(%s: %s) — falling back to the LOCAL view, which is a LOWER "
              "BOUND on the store under a read-through cache"
              % (base, type(exc).__name__, exc), file=_sys.stderr)
        return None


def store_paths(kind, world_dir=None):
    """Ordered paths comprising the content store for `kind`, oldest-first.

    Today this is the single legacy file, so callers are byte-identical to
    reading it directly. Once a writer emits `<kind>-YYYY-MM-DD.jsonl` segments
    they are appended in lexical (== chronological, ISO dates) order and
    consumers pick them up with no change.

    Excludes `<kind>-archive.jsonl` and the counter sidecar by construction —
    neither is date-shaped. See `_segment_re`.

    ENUMERATION IS BACKEND-FIRST, UNIONED WITH LOCAL, and that is load-bearing
    rather than defensive. Under own-cloud the local tree is a read-through
    CACHE: a file materialises locally only once something has read it, so a
    `base.glob(...)` enumerates what THIS BOX HAPPENS TO HOLD, not what the
    store contains. Measured 2026-08-16 (alpha, hostname cc-08, own-cloud) at
    this very directory: `list_dir(WORLD_DIR)` returned 62 `.jsonl` names and
    the local glob returned 60 — 4 present only in the store and 2 only on
    disk. That gap is why this is a UNION and not a replacement: a segment
    written HERE and not yet pushed is equally real, and a backend-only read
    would drop it.

    The consequence of getting this wrong is asymmetric, which is why it is
    fixed BEFORE the writer rather than after: a missed segment makes the store
    read SHORT, and for guardrails a short read is "these guardrails do not
    apply" — the same worst-direction failure `_resolved_base` refuses to make
    silently. It is currently LATENT for these two stems (no segments exist, so
    local and backend agree today); the gap above is the mechanism measured at
    the same path, not an outage in this store.

    CALLERS MUST READ THROUGH THE BACKEND. A returned path may name an object
    that is not materialised locally yet, so a bare `open()` can raise
    FileNotFoundError where it never used to. Use `get_backend().read_text` /
    `read_jsonl`, or the daemon's `jsonl_cache` — both call `ensure_local`
    first. This contract is set now precisely because the seam has no
    production callers yet; every future consumer inherits it for free.
    """
    _check_kind(kind)
    base = _resolved_base(world_dir)
    if base is None:
        return []

    legacy_name = "%s.jsonl" % kind
    local_names = set(p.name for p in base.glob("%s-*.jsonl" % kind)
                      if p.is_file())
    have_legacy = (base / legacy_name).is_file()

    remote_names = _backend_names(base)
    if remote_names is not None:
        # Unioned RAW, with no `<kind>-*.jsonl` prefilter: `seg_re` below is
        # already strictly narrower, and a second filter that must agree with it
        # is the drift shape this module exists to prevent. (Measured: adding
        # one back is mutation-invisible — every test still passes — which is
        # exactly why it should not be here.) No `.is_file()` either: not being
        # local yet is the case this covers, so testing for it would re-impose
        # the defect one line lower.
        local_names |= set(remote_names)
        have_legacy = have_legacy or (legacy_name in remote_names)

    paths = []
    if have_legacy:
        paths.append(base / legacy_name)
    seg_re = _segment_re(kind)
    for name in sorted(local_names):
        if seg_re.match(name):
            paths.append(base / name)
    return paths


def dedup_by_id(items):
    """Collapse ids appearing in more than one content path. NEWEST WINS.

    THE COMPANION TO `store_paths`, AND THE HALF THAT IS EASY TO OMIT. That
    function returns paths oldest-first (legacy, then date segments ascending);
    a bare concatenation over them therefore emits a mutated record TWICE, and
    any `first match` lookup returns the OLDEST copy. Both reader call sites had
    exactly that shape. For guardrails the failure direction is the bad one:
    a guardrail retired in a segment keeps reading `status: active` from legacy,
    which is the same worst-direction failure `_resolved_base` and `store_paths`
    both refuse to make silently.

    WHY THE READER RECONCILES AT ALL, when a writer could instead guarantee
    single-file residency: the writer does not exist yet, and the economics of
    this cutover push against that guarantee. Segmenting exists to stop
    rewriting a ~20MB legacy object; a mutation that rewrites the record in
    place in legacy re-incurs exactly the PUT the split removes, so the
    cost-motivated writer appends the new version to the live segment and dual
    residency becomes the NORMAL state rather than an edge case. This function
    is correct under BOTH designs — it is a no-op when residency is single-file,
    since no id repeats — so it does not need that question settled first, and
    it does not quietly constrain the writer that has yet to be written.

    Note this is the READ-side twin of a reconcile that already exists BELOW
    the cross-box merge (segments route to the content-identity-keyed handler,
    for this same two-lines-per-mutated-record reason). Nothing sat below the
    read.

    POSITION IS FIRST-OCCURRENCE, CONTENT IS LAST. Keeping the position stable
    makes this a minimal delta over the previous concatenation for every caller
    that does not sort (`recent` sorts by `created` itself), while the content
    still comes from the newest path.

    Records with no usable id pass through untouched and are never collapsed
    into each other — a store with malformed lines must not silently lose them
    here, where the loss would look like a short read.

    Returns a FRESH list; the input is never mutated.
    """
    out = []
    index_of = {}
    for rec in items:
        rec_id = rec.get("id") if isinstance(rec, dict) else None
        if not isinstance(rec_id, str) or not rec_id:
            out.append(rec)
            continue
        prior = index_of.get(rec_id)
        if prior is None:
            index_of[rec_id] = len(out)
            out.append(rec)
        else:
            out[prior] = rec
    return out


def counters_path(kind, world_dir=None):
    """Path to the counter sidecar for `kind`, or None when unresolved.

    The file need not exist; `load_counters` treats absence as "no sidecar yet",
    which is the correct reading during the reader-before-writer window.
    """
    _check_kind(kind)
    base = _resolved_base(world_dir)
    if base is None:
        return None
    return base / counters_name(kind)


def load_counters(kind, world_dir=None):
    """Map of record id -> counters dict from the sidecar. {} when absent.

    An empty result is the CORRECT and expected reading until the writer lands:
    `utilization_of` then falls through to the embedded field, so every consumer
    behaves exactly as it does today.

    Malformed lines are skipped rather than raised on, matching every other
    JSONL reader in the tree. A torn line loses one record's advisory counters —
    these are stats with no cross-box read-after-write need — whereas raising
    would take down a retrieval call for a cosmetic field.

    THE READ ROUTES THROUGH THE BACKEND, and this function used to be the one
    place in this module that broke the contract `store_paths` sets for everyone
    else ("CALLERS MUST READ THROUGH THE BACKEND ... a bare `open()` can raise
    FileNotFoundError where it never used to"). It did `path.is_file()` then
    `path.read_text()`. Under own-cloud the local tree is a read-through CACHE,
    so a sidecar this box has never fetched is ABSENT locally while present in
    the store — and that bare test then returned {} silently.

    That silence is the whole severity. {} is ALSO the legitimate reading during
    the reader-before-writer window, so the two are indistinguishable: every
    consumer falls through to the embedded field, every caller behaves plausibly,
    and nothing anywhere looks wrong while a box reports zero counters for a
    store that has them. guard-3992's quiet direction, in a function whose own
    docstring above declares an empty result CORRECT.

    `read_bytes`, not `read_text`: the backend's `read_text` takes no `errors=`
    argument, and decoding here preserves the `errors="replace"` tolerance this
    reader has always had. Neither forces `force_fresh` — the defect is ABSENCE,
    not staleness, and the two fail in opposite directions. A stale counter is a
    cosmetic scoring nuance; an absent sidecar zeroes EVERY record at once. A
    forced round-trip would also sit on the retrieval hot path, which
    `retrieve.py::_goal_id_is_terminal` declines for exactly this reason.

    A backend fault falls back to the LOCAL file rather than to {}, loudly —
    the same asymmetry `_backend_names` already uses in this module: an explicit
    foreign base (a tmp world in a test) is expected and silent, anything else
    says so on stderr. So this can only ever ADD a successful materialization;
    on every path where the backend is unavailable the behaviour is byte-for-byte
    what it was before.
    """
    _check_kind(kind)
    path = counters_path(kind, world_dir)
    if path is None:
        return {}
    text = None
    try:
        from storage_backend import get_backend
        text = get_backend().read_bytes(path).decode("utf-8", errors="replace")
    except FileNotFoundError:
        # Absent in the STORE is NOT the same as absent, and must not short-
        # circuit to {}. A sidecar written on THIS box and not yet pushed is
        # equally real — the same asymmetry `store_paths` unions for segments
        # ("a segment written HERE and not yet pushed is equally real, and a
        # backend-only read would drop it"). Fall through to the local read;
        # when the file is genuinely absent both ways, that path returns {}
        # anyway, so the legitimate "no sidecar yet" reading is preserved.
        pass
    except ValueError:
        pass                           # foreign base (tmp world): local view IS complete
    except Exception as exc:           # noqa: BLE001 — any backend fault -> local view
        print("[_utilization_store] backend read unavailable at %s (%s: %s) — "
              "falling back to the LOCAL file, which under a read-through cache "
              "may be ABSENT for a sidecar this box has never fetched"
              % (path, type(exc).__name__, exc), file=_sys.stderr)
    if text is None:
        if not path.is_file():
            return {}
        text = path.read_text(encoding="utf-8", errors="replace")
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = _json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        rec_id = rec.get("id")
        if not rec_id:
            continue
        counters = rec.get("utilization")
        if isinstance(counters, dict):
            out[rec_id] = counters
    return out


def utilization_of(rec, counters=None):
    """Counters for one record: the sidecar's entry when present, else embedded.

    THE SIDECAR WINS ON PURPOSE. During the cutover an id can carry both — the
    embedded field is a frozen snapshot from before the split, the sidecar is
    live. Preferring the embedded copy would silently pin every converted
    consumer to stale counts while looking entirely correct.

    Returns {} rather than None when neither side has counters, so callers can
    `.get("times_helpful", 0)` without a None check — matching how the embedded
    field is read at the existing call sites.

    THE REPLACE IS WHOLESALE, NOT PER-KEY, AND THAT IS DELIBERATE — read this
    before "fixing" it. A sidecar entry carrying only the counter that was
    incremented would make every OTHER counter for that record read as zero,
    and `endpoints/utilization.py::_is_candidate` turns a false zero into a
    RETIREMENT PROPOSAL (`_evidence(util) > 0` is what keeps a record). That
    hazard is real, and it is answered at the WRITER, not here:
    `utilization-flush.py::_seed_from_content` seeds a first-touch entry from
    the record's embedded counters, and `apply_deltas` materialises every
    `counter_names` key on any touched record. Two tests pin it —
    `test_first_touch_seeds_from_embedded_counters` and its explicit control
    `test_without_seeding_other_counters_would_read_as_zero`.

    That CONTROL is why a per-key merge here would be a net loss rather than
    belt-and-braces: it asserts this function's replace semantics precisely to
    document what the writer's seeding is FOR. Merging would delete the evidence
    that the seeding is load-bearing, and a later reader would find
    `_seed_from_content` looking like dead ceremony. A reader-side merge would
    also silently absorb a writer that stopped seeding, converting a loud,
    tested contract into an invisible one. (Raised independently as a
    fresh-eyes finding on the reader seam, 2026-08-18, and closed as
    already-defended after reading the writer — the cross-reference above is
    what was actually missing.)
    """
    if not isinstance(rec, dict):
        return {}
    if counters:
        found = counters.get(rec.get("id"))
        if isinstance(found, dict):
            return found
    embedded = rec.get("utilization")
    return embedded if isinstance(embedded, dict) else {}


def load_all_counters(world_dir=None):
    """Merged id -> counters map across every KIND. {} while no sidecar exists.

    EXISTS BECAUSE SEVERAL CONSUMERS SEE BOTH KINDS IN ONE LIST and cannot pick
    a `kind` to load. `retrieve.py`'s `_key` sort and the daemon's
    `_times_active` both run over the supplementary result set, which mixes
    reasoning-bank and guardrail records; per-kind loading would force each of
    them to re-derive a record's store from its id, which is exactly the
    re-derivation this seam removes.

    MERGING IS COLLISION-FREE, AND THAT IS MEASURED RATHER THAN ASSUMED
    (2026-08-17, alpha/cc-07, against the live stores): all 7712 reasoning-bank
    ids carry the `rb-` prefix, all 3819 guardrail ids carry `guard-`, and the
    intersection of the two id sets is EMPTY. If a future store joins KINDS with
    an id space that is not disjoint from these, this function silently resolves
    the collision by KINDS order and must gain a namespace check first — so add
    the check with the store, not after a wrong count is noticed.

    Prefer `load_counters(kind)` when the caller already knows the kind: it
    reads one sidecar instead of every sidecar, and it fails loudly on a bad
    kind via `_check_kind`.
    """
    merged = {}
    for kind in KINDS:
        merged.update(load_counters(kind, world_dir))
    return merged


# ===========================================================================
# WRITER HALF () — the counter spool. DEFAULT-OFF.
# ===========================================================================
#
# WHAT THIS REPLACES, and why the saving is this large. Every counter increment
# today is POST /v1/store/increment -> endpoints/store.py increment() ->
# file_locks.locked_rmw on the CONTENT store, i.e. a whole-object read-modify-
# write of a 20.46MB / 9.37MB file to change one integer. Measured ():
# reasoning-bank 1658 PUTs/day x 20.7MB = 34.39 GB/day with ~93% of writes being
# these increments; guardrails 1968 x 9.1MB = 17.97 GB/day at ~94%. Combined
# ~51 GB/day of S3 PUT traffic to mutate advisory statistics.
#
# TWO INDEPENDENT FACTORS, and the spool only attacks one of them — say so
# plainly, because the fleet has already been burned by assuming otherwise.
# meta/gate-firings.jsonl ALREADY spools () and still burned 16.15
# GB/day, because batching divides the write COUNT and leaves the object SIZE
# alone. Batching pays only when the object is small. So the spool is paired with
# a SMALL target: increments land in `<kind>-utilization.jsonl` (measured 2.14MB
# / 1.02MB), never in the multi-megabyte content store. Count AND size, or the
# arithmetic does not close.
#
# WHY COUNTERS MAY SPOOL AT ALL, when the content store may not: these are
# advisory statistics with NO cross-box read-after-write requirement. Nothing
# blocks on a counter being current; the scoring consumers tolerate a stale read
# by construction (retrieve.py declines force_fresh on this very field). A
# CONTENT record cannot spool — a peer that cannot see a just-added guardrail is
# a correctness failure, not a cosmetic one.

# The flag is per-box on purpose: a box flips it only once the whole fleet's
# READERS understand the sidecar, which is what store-cutover-check.sh proves.
# Registered in store-cutover-check.py STORES["utilization"]["flag"] — if you
# rename it here, rename it there in the same edit or the gate silently proves
# the wrong thing.
SPOOLED_ENV = "UTILIZATION_COUNTERS_SPOOLED"

# Spool-lane basenames, ALL defined here rather than in the flush script.
# gate-firings does the opposite — gate-firings-flush.py re-declares its own
# SPOOL_NAME beside _gate_log._SPOOL_NAME — and that is a live drift waiting to
# happen, of exactly the class `segment_name` above refuses to allow. One
# definition; the flush script imports these.
#
# WHAT ACTUALLY EXCLUDES THE SPOOL FROM THE CONTENT STORE — measured, because
# the plausible answer is wrong. `store_paths` enumerates with a loose
# `<kind>-*.jsonl` glob and then filters through the STRICT `_segment_re`. It is
# tempting to claim the dotted `.spool.` infix keeps the spool out of the glob;
# it does NOT — fnmatch's `*` matches dots, so `guardrails-*.jsonl` DOES admit
# `guardrails-utilization.spool.jsonl` (measured 2026-08-18, returns True). The
# ONLY thing standing between a machine-local buffer and the shared content
# store is `_segment_re`'s exact date shape, which rejects every spool, flushing,
# sidecar and archive name (all measured False, with a real segment measured
# True as the positive control). So: never relax that matcher toward a glob, and
# never assume a naming convention is doing work the regex is actually doing.
# This is the _gate_log lesson recurring rather than inverted — THERE a
# name-prefix check keyed on the dotted spool while the glob produced the
# hyphenated form, so the check was structurally dead for months and the
# exclusion everyone believed in did not exist.
#
# The dotted infix still earns its place, for a different and smaller reason: it
# cannot collide with a date segment, so a reader that ever does key on the name
# shape cannot confuse the two.
def spool_name(kind):
    """Basename of this box's machine-local increment spool for `kind`."""
    _check_kind(kind)
    return "%s-utilization.spool.jsonl" % kind


def flushing_name(kind):
    """Basename of the rotated spool being drained (crash-residue marker)."""
    _check_kind(kind)
    return "%s-utilization.spool.flushing.jsonl" % kind


def flush_stamp_name(kind):
    """Basename of the last-flush timestamp stamp for `kind`."""
    _check_kind(kind)
    return "%s-utilization.spool.last-flush" % kind


def flush_lock_name(kind):
    """Basename of the flush lock for `kind` (covered by the *.lock glob)."""
    _check_kind(kind)
    return "%s-utilization.spool.flush.lock" % kind


def spooled_enabled():
    """True when THIS box routes counter increments to the spool.

    Read as a plain env flag, NOT resolved from the storage backend the way
    `_gate_log._spool_active` does. The two are answering different questions
    and the difference matters: _gate_log asks "is a direct append expensive
    HERE?", which is a property of the backend, so it must resolve the backend.
    This asks "has this box been CLEARED to write the new store shape?", which is
    a cutover decision a human or the cutover gate makes — and it must be false
    on a local-backend box too, because flipping the write target is what the
    attestation gate exists to sequence. Deriving it from the backend would flip
    every own-cloud box the moment this code landed, which is precisely the
    unsequenced flip `store-cutover-check.sh` was built to prevent.
    """
    return _os.environ.get(SPOOLED_ENV, "").strip().lower() in ("1", "true", "yes")


def spool_path(kind, world_dir=None):
    """Path to this box's increment spool for `kind`, or None when unresolved."""
    _check_kind(kind)
    base = _resolved_base(world_dir)
    if base is None:
        return None
    return base / spool_name(kind)


def record_increment(kind, rec_id, counter, delta=1, world_dir=None):
    """Append one counter delta to the machine-local spool. Never raises.

    Returns True when a line was written, False otherwise — callers use the
    False to fall back to the legacy in-record increment, so this MUST NOT
    swallow a failure into a silent no-op that also reports success.

    O(1) HOT PATH BY CONSTRUCTION: one lockless O_APPEND of a sub-200-byte line,
    the same idiom as `_gate_log.log` and `_fileops._record_fallback_hit`. No
    lock is taken and none is needed — POSIX guarantees atomicity for a single
    small O_APPEND write, and a torn line (possible only if the process dies
    mid-write) is skipped by the flusher's lossy parser. Losing one advisory
    increment to a crash is immaterial; taking a lock on this path would
    reintroduce the contention the whole change exists to remove.

    DELTAS, NOT ABSOLUTE VALUES. The spool records "+1 to times_helpful on
    rb-123", never "times_helpful is now 7". Absolute values would make two
    boxes' spools destructive to each other — last-writer-wins on a counter both
    incremented — whereas deltas SUM, which is what a counter means. It is also
    what makes the sidecar's merge handler correct: `merge_utilization_counters`
    takes a per-counter MAX across boxes, which is a safe reconciliation for
    monotonically-increasing values and a lossy one for anything else.
    """
    try:
        _check_kind(kind)
        if not rec_id or not counter:
            return False
        path = spool_path(kind, world_dir)
        if path is None:
            return False
        line = _json.dumps({
            "id": rec_id,
            "counter": counter,
            "delta": int(delta),
            "ts": _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        }, ensure_ascii=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return True
    except Exception:
        # Best-effort by contract. A failed spool append must never take down a
        # retrieval or a guardrail check — the caller falls back to the legacy
        # in-record increment, so the counter is preserved either way.
        return False
