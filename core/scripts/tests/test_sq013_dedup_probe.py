"""Regression test for : the sq-013 replay dedup must see COMPLETED
owners, not only open ones.

THE MEASURED INCIDENT this pins (2026-08-27, first-hand, twice in one session):
g-326-711 completed 01:37:19 and g-326-712 completed 02:01:19; at 02:12:21 the
reducer spark replay filed g-326-714 as their duplicate, and a worker skipped it
as MOOT ON ARRIVAL at 02:20:54. The dedup run before filing was CORRECT and its
answer was TRUE — zero LIVE owners — because both owners had already completed.
The race was ELEVEN MINUTES, so the fix is not "a longer window", it is "a
window that reaches terminal statuses at all".

guard-4166 governs the shape of this file: a fix whose effect is that something
STOPS APPEARING needs a positive control that does NOT flip, and the mutation
proof must show that control holding. Every DECLINE assertion below is therefore
paired with a FILE assertion, so an over-broad implementation that declined
everything would fail here rather than passing silently — which is the failure
mode a decline-only test cannot distinguish from a working fix.
"""

import json
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "sq013_dedup_probe", CORE_SCRIPTS / "sq013-dedup-probe.py")
sq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sq)

NOW = datetime(2026, 8, 27, 2, 12, 21)          # the moment  was filed
SESSION_START = datetime(2026, 8, 27, 1, 0, 0)

# The relay subject, in the worker's own phrasing — deliberately NOT the
# completed goal's wording, so this also exercises the phrasing axis the block
# already covered (guard-1204 / guard-2228).
RELAY = ("pickNearbyPlayer returns null without instrumentation so the "
         "denominator for nearby-player selection is unmeasured")

COMPLETED_OWNER = {
    "id": "g-326-711",
    "status": "completed",
    "completed_date": "2026-08-27T01:37:19",
    "title": "Instrument the pickNearbyPlayer denominator",
    "description": ("The previously-prescribed null-branch log would "
                    "instrument dead code: the composer's player==null branch "
                    "is unreachable because the composer is entered only when "
                    "the scorer already found a nearby player."),
}

# Positive control: same corpus, a subject nothing owns. Must stay FILE in every
# assertion below, including under mutation.
UNOWNED = ("warm pool scaling emits no telemetry when the breach path "
           "reports success")


def _corpus(*extra):
    return [COMPLETED_OWNER, *extra]


# --- the fix: a terminal owner inside the window is SEEN --------------------

def test_completed_minutes_earlier_declines_while_control_still_files():
    """THE REGRESSION. Both assertions run against the SAME corpus and the SAME
    call parameters, so a blanket-decline implementation fails the second."""
    declined = sq.decide(RELAY, _corpus(), NOW, SESSION_START)
    assert declined["decision"] == "DECLINE", declined
    filed = sq.decide(UNOWNED, _corpus(), NOW, SESSION_START)
    assert filed["decision"] == "FILE", filed          # control must NOT flip


def test_decline_cites_the_completed_goal_id():
    """Outcome 3: a reader must be able to tell 'already done' from 'never
    filed'. A bare DECLINE cannot carry that distinction."""
    r = sq.decide(RELAY, _corpus(), NOW, SESSION_START)
    assert r["cited_goal_id"] == "g-326-711", r
    assert r["cited_status"] == "completed", r
    assert "g-326-711" in r["reason"], r


def test_skipped_status_is_scanned_too():
    """`skipped` is terminal and means the work was considered. Same-call
    control."""
    owner = dict(COMPLETED_OWNER, id="g-326-712", status="skipped")
    assert sq.decide(RELAY, [owner], NOW, SESSION_START)["decision"] == "DECLINE"
    assert sq.decide(UNOWNED, [owner], NOW, SESSION_START)["decision"] == "FILE"


# --- the window is REAL, not "always decline" ------------------------------

def test_terminal_owner_outside_the_window_does_not_decline():
    """A goal closed months ago is not evidence the work was JUST done. Without
    this the probe would decline on any historical overlap and be useless."""
    stale = dict(COMPLETED_OWNER, completed_date="2026-04-01T00:00:00")
    assert sq.decide(RELAY, [stale], NOW, SESSION_START)["decision"] == "FILE"


def test_open_owner_declines_regardless_of_age():
    """Pre-existing behaviour preserved: an OPEN goal owns its work however old
    it is, so the window must not be applied to it."""
    old_open = dict(COMPLETED_OWNER, id="g-326-700", status="pending",
                    completed_date=None, created="2026-01-05T00:00:00")
    assert sq.decide(RELAY, [old_open], NOW, SESSION_START)["decision"] == "DECLINE"


def test_undated_terminal_goal_is_counted_in_not_out():
    """An undated terminal record is AMBIGUOUS, not old. Counting it out would
    reproduce the original defect on exactly the records whose timestamps a
    writer forgot to stamp."""
    undated = {k: v for k, v in COMPLETED_OWNER.items()
               if k != "completed_date"}
    assert sq.decide(RELAY, [undated], NOW, SESSION_START)["decision"] == "DECLINE"


# --- window arithmetic ------------------------------------------------------

def test_window_takes_the_earlier_of_session_start_and_fixed_lookback():
    """Both are FLOORS. A long session must not lose its own early completions,
    and a short session must not become blinder than the plain lookback — a
    single anchor cannot satisfy both, which is why this is a min()."""
    long_session = NOW - timedelta(hours=200)
    assert sq.window_start(NOW, long_session, 72) == long_session
    short_session = NOW - timedelta(hours=2)
    assert sq.window_start(NOW, short_session, 72) == NOW - timedelta(hours=72)
    assert sq.window_start(NOW, None, 72) == NOW - timedelta(hours=72)


# --- an unusable corpus is never a FILE ------------------------------------

def test_empty_and_garbage_corpora_refuse_rather_than_file(monkeypatch, capsys):
    """verify-before-assuming rule 4 / guard-2298: a silently-failed read that
    returns nothing has told you nothing. Reporting FILE on it would convert a
    broken probe into confident permission to duplicate."""
    for payload in ("", "   ", "not json at all", "[]"):
        monkeypatch.setattr(sys, "stdin", _FakeStdin(payload))
        rc = sq.main(["--subject", RELAY])
        assert rc == 2, (payload, rc)
        capsys.readouterr()


def test_exit_codes_separate_decline_from_breakage(monkeypatch, capsys):
    """3 = an owner exists, 0 = file, 2 = the probe could not run. Collapsing
    DECLINE and breakage onto one non-zero code makes each readable as the
    other (deploy-hold-check.sh precedent)."""
    import json as _json
    monkeypatch.setattr(sys, "stdin", _FakeStdin(_json.dumps(_corpus())))
    assert sq.main(["--subject", RELAY, "--now", NOW.isoformat(),
                    "--session-start", SESSION_START.isoformat()]) == 3
    capsys.readouterr()
    monkeypatch.setattr(sys, "stdin", _FakeStdin(_json.dumps(_corpus())))
    assert sq.main(["--subject", UNOWNED, "--now", NOW.isoformat(),
                    "--session-start", SESSION_START.isoformat()]) == 0
    capsys.readouterr()


class _FakeStdin:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload


# --- the rarity gate, exercised at a corpus size where IDF is LIVE ----------
#
# Everything above runs on a 1-2 goal corpus, which is BELOW MIN_IDF_CORPUS, so
# IDF is inert and the rarity gate cannot fire there. That is precisely the
# layer a fixture cannot reach by accident (guard-1462), and it is where the
# live dogfood found a real false positive: against the 3,140-goal queue the
# probe first cited an unrelated goal on five generic tokens. These two tests
# build a corpus big enough for document frequencies to exist, so the gate is
# under test rather than merely documented.

def _big_corpus(with_owner=True):
    """A corpus that REPRODUCES THE LIVE DOCUMENT-FREQUENCY DISTRIBUTION.

    This shape is load-bearing and the first version of it was wrong, which the
    mutation proof caught: with generic tokens in ~59 of 60 records their idf is
    0, so the WEIGHT threshold rejected the decoy and the rarity gate never
    fired — the test passed, for the wrong reason, and removing the rarity gate
    did not flip it. A test green by default is worse than no test (guard-2903).

    Live measurement being reproduced: n=3,140 with `returns` at df=799 (25%),
    idf 1.37, and five such tokens summing to weight 11.04 — comfortably above
    any sane weight floor. So here ~25% of a 201-record corpus carries the
    generic vocabulary, giving idf log(201/52) ~ 1.35 per token and a decoy
    weight ~6.7. Weight alone therefore CANNOT reject the decoy, and the rarity
    gate is the only thing that can — which is exactly the condition under test.
    """
    unrelated = [{
        "id": "g-900-%03d" % i,
        "status": "completed",
        "completed_date": "2026-08-27T01:00:00",
        "title": "Unrelated maintenance record %d about caching layers" % i,
        "description": ("Housekeeping concerning cache eviction, retention "
                        "windows and archival snapshots. Record %d." % i),
    } for i in range(150)]

    generic = [{
        "id": "g-000-%03d" % i,
        "status": "completed",
        "completed_date": "2026-08-27T01:00:00",
        "title": "Generic goal %d returns selection without denominator" % i,
        "description": ("This record returns a selection without a denominator "
                        "and is unmeasured, like its siblings."),
    } for i in range(49)]

    decoy = {
        "id": "g-115-3421",
        "status": "completed",
        "completed_date": "2026-08-27T01:30:00",
        "title": "pipeline-archive has no scheduled caller",
        "description": ("It returns a selection without a denominator and the "
                        "prune cadence is unmeasured."),
    }
    owner = {
        "id": "g-326-711",
        "status": "completed",
        "completed_date": "2026-08-27T01:37:19",
        "title": "Instrument the pickNearbyPlayer denominator",
        "description": ("pickNearbyPlayer instrumentation: the null branch is "
                        "unreachable, so the denominator is unmeasured."),
    }
    corpus = unrelated + generic + [decoy]
    return corpus + [owner] if with_owner else corpus


def test_the_fixture_actually_reproduces_the_live_idf_shape():
    """Guards the guard. If the corpus drifts back to df~n the generic tokens
    weigh 0, weight alone rejects the decoy, and the two tests below stop
    testing the rarity gate while still passing."""
    corpus = _big_corpus()
    docs = [sq._tokens(" ".join(str(g.get(f) or "")
                                for f in ("title", "description")))
            for g in corpus]
    idf, n = sq._compute_idf(docs, sq._tokens(RELAY))
    assert n >= sq.MIN_IDF_CORPUS, n            # IDF must be LIVE, not inert
    decoy_tokens = sq._tokens(RELAY) & docs[-2]
    decoy_weight = sum(idf[t][1] for t in decoy_tokens)
    assert decoy_weight > sq.WEIGHT_THRESHOLD, (
        "decoy weight %.2f must EXCEED the weight floor, or the rarity gate is "
        "not what rejects it" % decoy_weight)
    assert not [t for t in decoy_tokens if idf[t][0] <= max(2, n // sq.RARE_DF_DIVISOR)], \
        "decoy must carry NO rare token"


def test_generic_token_overlap_alone_does_not_decline():
    """THE LIVE-FOUND FALSE POSITIVE, pinned. The decoy shares four generic
    tokens with the subject and no rare one; it must not be cited."""
    r = sq.decide(RELAY, _big_corpus(with_owner=False), NOW, SESSION_START)
    assert r["decision"] == "FILE", r
    assert r["cited_goal_id"] != "g-115-3421", r


def test_rare_token_owner_is_cited_over_the_generic_decoy():
    """Same corpus WITH the true owner present: it wins, and the decoy is not
    the citation. Ranked by IDF weight, not raw overlap count — by count the
    decoy would have tied or beaten it, which is how the live miss happened."""
    r = sq.decide(RELAY, _big_corpus(), NOW, SESSION_START)
    assert r["decision"] == "DECLINE", r
    assert r["cited_goal_id"] == "g-326-711", r
    assert "picknearbyplayer" in r["matches"][0]["rare_tokens"], r["matches"][0]


# ── LENGTH NORMALISATION () ────────────────────────────────────────
# The corpus above is length-UNIFORM by construction, so it cannot reach the
# defect these tests pin: on the live 2,883-goal queue the median blob is 127
# unique tokens and the largest is 1,544, and `weight` is an unnormalised SUM
# over the overlap. A record 12x the median therefore wins on SIZE -- it has
# more tokens, so more chances to overlap the subject AND more chances to hold
# some rare token by coincidence. Measured live: a merge-wedge relay was
# DECLINED citing  (1,107 tokens, an unrelated sweep goal) on the lone
# coincidental token `granularity`, while the genuine owner  (147
# tokens, sharing `ayoai-journal-md` df=2 and `same-heading` df=10) ranked below
# it. Same class as the generic-decoy false positive above, one axis over:
# there the decoy won on COUNT, here the sponge wins on LENGTH.

def _sponge_record():
    """A record that qualifies on LENGTH ALONE.

    It contains every token of RELAY -- which is what a 71,504-char description
    does in practice, it mentions everything -- plus 900 distinct filler tokens
    nothing else shares. So it overlaps the subject maximally while being about
    nothing in particular, exactly the live shape.
    """
    filler = " ".join("fillertoken%04d" % i for i in range(900))
    return {
        "id": "g-999-SPONGE",
        "status": "pending",
        "title": "Sponge record that mentions everything",
        "description": RELAY + " " + filler,
    }


def test_sponge_does_not_outrank_the_genuine_owner():
    """THE ASSERTION UNDER TEST. A record whose only claim is length must not
    take the citation away from the goal that actually owns the work."""
    r = sq.decide(RELAY, _big_corpus() + [_sponge_record()], NOW, SESSION_START)
    assert r["decision"] == "DECLINE", r
    assert r["cited_goal_id"] == "g-326-711", (
        "the sponge took the citation from the true owner: %s" % r["cited_goal_id"])


def test_sponge_test_is_not_green_by_default(monkeypatch):
    """MUTATION PROOF (guard-2903). Setting b=0 disables length normalisation
    and nothing else; the sponge must then WIN, or the test above is passing for
    some reason other than the fix it exists to pin."""
    monkeypatch.setattr(sq, "LENGTH_NORM_B", 0.0)
    r = sq.decide(RELAY, _big_corpus() + [_sponge_record()], NOW, SESSION_START)
    assert r["cited_goal_id"] == "g-999-SPONGE", (
        "with normalisation OFF the sponge should win; it did not, so the "
        "fixture no longer reproduces the defect: %s" % r["cited_goal_id"])


def test_length_norm_is_unity_at_average_length():
    """WHY BM25 AND NOT sqrt/log: the factor is exactly 1.0 at |D| == avgdl, so
    an average-length record scores as it always did and the calibrated
    WEIGHT_THRESHOLD keeps its meaning. sqrt/log rescale every record and would
    move that boundary silently -- which g-115-8036 explicitly warned against."""
    assert sq._length_norm(157.0, 157.0) == 1.0
    assert sq._length_norm(1570.0, 157.0) > 1.0     # sponge is penalised
    assert sq._length_norm(15.0, 157.0) < 1.0       # short record is favoured
    assert sq._length_norm(100.0, 0) == 1.0         # empty corpus fails open


# ── ALL-COMMON-VOCABULARY TRUE DUPLICATES () ───────────────────────
# The rarity gate requires a RARE token in the overlap. That premise is VACUOUS
# when the SUBJECT itself carries no rare token -- then the gate is unsatisfiable
# and silently drops true duplicates whose vocabulary is entirely common.
# Measured on the live 2,836-goal queue:  ("Idea: deep audits closing
# with empty outcome_note ...") self-matched at weight 3.55, subject-coverage
# 1.0, yet FILEd -- an invisible false negative. The fix waives the veto ONLY
# for an all-common subject a candidate restates VERBATIM at the TITLE level; a
# generic-token or description-only collision must still FILE. These tests pin
# BOTH directions so a later swing back is caught.

ALLCOMMON_SUBJECT = "returns selection denominator unmeasured"  # all tokens common in _big_corpus, none rare


def _allcommon_owner():
    """A goal whose TITLE is token-identical to ALLCOMMON_SUBJECT."""
    return {
        "id": "g-703-ALLCOMMON",
        "status": "pending",
        "title": ALLCOMMON_SUBJECT,
        "description": "The genuine owner of this all-common-vocabulary work.",
    }


def test_allcommon_fixture_reproduces_the_shape():
    """Guards the guard (guard-2903 sibling). The subject must carry NO rare
    token (or the waiver never engages), AND the owner must clear the WEIGHT
    floor on live IDF (or the weight gate rejects it before the rarity path is
    reached), AND the owner must share NO rare overlap token (or the OLD gate
    would already have cited it and the fix is not what is under test)."""
    corpus = _big_corpus() + [_allcommon_owner()]
    docs = [sq._tokens(" ".join(str(g.get(f) or "")
                                for f in ("title", "description")))
            for g in corpus]
    subj = sq._tokens(ALLCOMMON_SUBJECT)
    idf, n = sq._compute_idf(docs, subj)
    assert n >= sq.MIN_IDF_CORPUS, n            # IDF must be LIVE, not inert
    rare_ceil = max(2, n // sq.RARE_DF_DIVISOR)
    assert [t for t in subj if idf[t][0] <= rare_ceil] == [], "subject must be ALL-COMMON"
    owner_blob = docs[-1]
    avgdl = sum(len(d) for d in docs) / float(len(docs))
    overlap = subj & owner_blob
    weight = sum(idf[t][1] for t in overlap) / sq._length_norm(len(owner_blob), avgdl)
    assert weight >= sq.WEIGHT_THRESHOLD, (
        "owner weight %.2f must clear the floor, or the weight gate rejects it "
        "before the rarity path" % weight)
    assert [t for t in overlap if idf[t][0] <= rare_ceil] == [], \
        "owner must share NO rare token, else the OLD gate would already cite it"


def test_allcommon_true_duplicate_is_declined():
    """CATCH DIRECTION. An all-common subject token-identical to an existing
    goal's TITLE is a duplicate and must be cited -- the g-115-3755 live false
    negative, pinned."""
    r = sq.decide(ALLCOMMON_SUBJECT, _big_corpus() + [_allcommon_owner()],
                  NOW, SESSION_START)
    assert r["decision"] == "DECLINE", r
    assert r["cited_goal_id"] == "g-703-ALLCOMMON", r


def test_allcommon_description_collision_still_files():
    """DO-NOT-OVERFIRE DIRECTION. Without the title-identical owner, the same
    all-common subject overlaps the generic records' and the decoy's
    DESCRIPTIONS in full (subject-coverage 1.0) -- but none RESTATES it at the
    TITLE level, so the newly-enabled decline path must NOT fire. A false
    DECLINE is the silent, permanent failure direction (guard-5147)."""
    r = sq.decide(ALLCOMMON_SUBJECT, _big_corpus(with_owner=False),
                  NOW, SESSION_START)
    assert r["decision"] == "FILE", r


def test_allcommon_waiver_is_not_green_by_default(monkeypatch):
    """MUTATION PROOF (guard-2903). Raise the all-common min-overlap above the
    subject's token count and the waiver can no longer admit the owner; the true
    duplicate must then FILE, proving the waiver -- not some other path -- is
    what declines it."""
    monkeypatch.setattr(sq, "ALLCOMMON_TITLE_DUP_MIN_OVERLAP", 99)
    r = sq.decide(ALLCOMMON_SUBJECT, _big_corpus() + [_allcommon_owner()],
                  NOW, SESSION_START)
    assert r["decision"] == "FILE", (
        "with the waiver disabled the all-common owner should not be cited; it "
        "was: %s" % r["cited_goal_id"])


# --- BATCH MODE (): ONE corpus read, N records, and a DECLINE
#     against a terminal-but-not-done owner is a READING ASSIGNMENT ----------
#
# gap-162's encounter log states the requirement this section pins, verbatim:
# "a DECLINE has to be re-read against the cited owner's STATUS and CLAIM,
# never accepted on rc=3 alone - both real work items were hiding behind a
# well-formed decline". In that encounter two live observations were suppressed
# by a decline citing , whose own outcome_note asserts the OPPOSITE
# claim to the observations it was suppressing; both were real, unowned work
# (filed as  and ). guard-5147: a false DECLINE is the
# silent, PERMANENT failure direction -- nothing ever re-opens it.
#
# guard-4166 governs here exactly as it does above: every MUST-READ assertion
# runs in the SAME batch against the SAME corpus as a FILE control, so an
# implementation that flagged everything would fail these tests rather than
# pass them silently.

SKIPPED_OWNER = dict(COMPLETED_OWNER, id="g-115-7803", status="skipped")


# --- extract_subject: the slot has NO schema (guard-4044) -------------------

def test_extract_subject_reads_every_alternate_key():
    """The spark_capture slot is schemaless, so `observation` is one of several
    shapes that actually occur. Reading the literal key only is how records go
    missing without anything erroring -- an empty sweep and a blind one are
    indistinguishable afterwards."""
    for key in sq.OBSERVATION_KEYS:
        text, used = sq.extract_subject({key: "  a real observation  "})
        assert text == "a real observation", (key, text)
        assert used == key, (key, used)


def test_extract_subject_prefers_the_declared_key_order():
    """Deterministic precedence, so the reported key distribution is a fact
    about the records rather than about dict iteration order."""
    rec = {"content": "second choice", "observation": "first choice"}
    assert sq.extract_subject(rec) == ("first choice", "observation")


def test_extract_subject_handles_bare_string_and_unreadable_shapes():
    """A bare string IS its own subject (that shape occurs in the slot). Blank
    and non-mapping shapes return (None, None) so the caller counts them as
    unreadable instead of scoring an empty subject against the whole corpus."""
    assert sq.extract_subject("bare relay text") == ("bare relay text",
                                                     "<bare-string>")
    assert sq.extract_subject({"goal_id": "g-1"}) == (None, None)
    assert sq.extract_subject({"observation": "   "}) == (None, None)
    assert sq.extract_subject("   ") == (None, None)
    assert sq.extract_subject(None) == (None, None)
    assert sq.extract_subject(42) == (None, None)


# --- the reading assignment -------------------------------------------------

def test_batch_flags_terminal_owner_must_read_while_control_still_files():
    """THE REGRESSION ( outcome 3 / check 2). Both records ride the
    SAME batch against the SAME corpus, so an implementation that flagged
    everything fails on the second row rather than passing silently."""
    res = sq.batch_decide([{"observation": RELAY}, {"observation": UNOWNED}],
                          [SKIPPED_OWNER], NOW, SESSION_START)
    flagged, control = res["rows"]
    assert flagged["verdict"] == "MUST-READ", flagged
    assert flagged["must_read"] is True, flagged
    assert control["verdict"] == "FILE", control
    assert control["must_read"] is False, control
    assert res["must_read_count"] == 1, res
    assert res["file_count"] == 1, res


def test_batch_must_read_row_carries_owner_status_and_title():
    """Outcome 3 literally: the output "surfaces each cited owner's STATUS and
    TITLE". A bare file/decline verdict FAILS check 2 of the goal -- the reader
    cannot re-derive the owner's claim from a verdict alone."""
    row = sq.batch_decide([{"observation": RELAY}], [SKIPPED_OWNER],
                          NOW, SESSION_START)["rows"][0]
    assert row["cited_goal_id"] == "g-115-7803", row
    assert row["cited_status"] == "skipped", row
    assert row["cited_title"] == SKIPPED_OWNER["title"], row


def test_batch_must_read_test_is_not_green_by_default(monkeypatch):
    """MUTATION PROOF (guard-2903). Empty the must-read set and the same row
    must fall back to a plain DECLINE -- proving the flag, and not some other
    path, is what produced MUST-READ in the two tests above."""
    monkeypatch.setattr(sq, "MUST_READ_STATUSES", ())
    row = sq.batch_decide([{"observation": RELAY}], [SKIPPED_OWNER],
                          NOW, SESSION_START)["rows"][0]
    assert row["verdict"] == "DECLINE", (
        "with the must-read set emptied this row should be a plain DECLINE; it "
        "was %s, so the tests above pass for some other reason" % row["verdict"])


def test_batch_completed_owner_declines_without_a_reading_assignment():
    """The boundary that keeps MUST-READ meaningful. A COMPLETED owner is a
    LEGITIMATE decline -- that is the g-115-8007 fix this file opens with -- so
    only terminal-but-NOT-done statuses become reading assignments. Widening
    must-read to every terminal status would re-open the duplicate-filing hole
    that fix closed."""
    row = sq.batch_decide([{"observation": RELAY}], [COMPLETED_OWNER],
                          NOW, SESSION_START)["rows"][0]
    assert row["verdict"] == "DECLINE", row
    assert row["must_read"] is False, row
    assert row["cited_status"] == "completed", row


# --- the walk survives its own inputs --------------------------------------

def test_batch_malformed_record_is_counted_and_the_walk_continues():
    """guard-1512: one malformed record must not abort the store walk. The
    record AFTER the bad one is the assertion that matters -- a walk that dies
    mid-list silently disables the sweep for everything downstream, and reports
    a shorter table rather than an error."""
    res = sq.batch_decide([{"goal_id": "g-bad"}, {"observation": UNOWNED}],
                          [SKIPPED_OWNER], NOW, SESSION_START)
    bad, good = res["rows"]
    assert bad["verdict"] == "UNREADABLE", bad
    assert good["verdict"] == "FILE", good
    assert res["population"]["records_unreadable"] == 1, res["population"]
    assert res["population"]["records_scored"] == 1, res["population"]


def test_batch_emits_one_unclipped_row_per_input_record():
    """guard-5893: a dedup probe clipped to the first N is not a dedup probe --
    the records it drops are exactly the ones nobody then reads."""
    records = [{"observation": UNOWNED + " variant %d" % i} for i in range(25)]
    res = sq.batch_decide(records, [SKIPPED_OWNER], NOW, SESSION_START)
    assert len(res["rows"]) == 25, len(res["rows"])
    assert [r["index"] for r in res["rows"]] == list(range(25))


def test_render_batch_prints_every_row_and_states_the_population():
    """guard-5893 (no clipping) + guard-3696 (state the POPULATION behind a
    corpus measurement). Without the population line a clean sweep and a sweep
    that scanned nothing render identically."""
    records = [{"observation": UNOWNED + " variant %d" % i} for i in range(25)]
    res = sq.batch_decide(records, [SKIPPED_OWNER], NOW, SESSION_START)
    text = sq.render_batch(res)
    for i in range(25):
        assert ("[%d]" % i) in text, "row %d missing from the rendered table" % i
    assert "POPULATION: 25 record(s) in, 25 scored, 0 unreadable" in text, text
    assert "against 1 corpus goal(s)" in text, text


def test_render_batch_names_the_owner_and_the_reading_obligation():
    """The rendered table is what a reader actually acts on, so the STATUS, the
    TITLE and the obligation must survive rendering -- not merely exist in the
    returned dict."""
    res = sq.batch_decide([{"observation": RELAY}], [SKIPPED_OWNER],
                          NOW, SESSION_START)
    text = sq.render_batch(res)
    assert "MUST-READ" in text, text
    assert "g-115-7803" in text, text
    assert "STATUS=skipped" in text, text
    assert SKIPPED_OWNER["title"] in text, text
    assert "guard-5147" in text, text


# --- the positive control must be ALIEN, not merely odd-sounding -----------

def test_positive_control_subject_is_alien_not_merely_nonsense_sounding():
    """guard-5889: ordinary English like 'resurfacing' or 'audit' IS corpus
    vocabulary and will match. The control only proves this probe can still say
    FILE if its tokens cannot appear in any corpus -- so it must FILE even
    against the sponge, a record built to overlap everything."""
    r = sq.decide(sq.POSITIVE_CONTROL_SUBJECT,
                  _big_corpus() + [_sponge_record()], NOW, SESSION_START)
    assert r["decision"] == "FILE", r


# --- exit codes -------------------------------------------------------------

def test_batch_exit_code_4_separates_must_read_from_decline(monkeypatch,
                                                            capsys, tmp_path):
    """4 = "N records need READING before any disposition", distinct from 3 =
    "an owner exists" and 0 = file. Collapsing them makes each readable as the
    other -- the same reason DECLINE is 3 and not 1 above."""
    import json as _json
    subjects = tmp_path / "subjects.json"

    subjects.write_text(_json.dumps([{"observation": RELAY}]), encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", _FakeStdin(_json.dumps([SKIPPED_OWNER])))
    assert sq.main(["--subjects-file", str(subjects), "--now", NOW.isoformat(),
                    "--session-start", SESSION_START.isoformat()]) == 4
    capsys.readouterr()

    subjects.write_text(_json.dumps([{"observation": UNOWNED}]),
                        encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", _FakeStdin(_json.dumps([SKIPPED_OWNER])))
    assert sq.main(["--subjects-file", str(subjects), "--now", NOW.isoformat(),
                    "--session-start", SESSION_START.isoformat()]) == 0
    capsys.readouterr()


def test_main_requires_exactly_one_subject_source(capsys, tmp_path):
    """required=True on --subject would make batch mode unreachable; no check
    at all lets a caller silently take the single-subject path while believing
    it ran a batch, and get one verdict where it expected N rows. The refusal
    fires BEFORE stdin is read, so neither call needs a corpus."""
    import json as _json
    subjects = tmp_path / "s.json"
    subjects.write_text(_json.dumps([{"observation": UNOWNED}]),
                        encoding="utf-8")
    assert sq.main([]) == 2
    capsys.readouterr()
    assert sq.main(["--subject", RELAY, "--subjects-file", str(subjects)]) == 2
    capsys.readouterr()


def test_batch_unreadable_or_empty_subjects_file_refuses_to_report_clean(
        monkeypatch, capsys, tmp_path):
    """verify-before-assuming rule 4. An unreadable input has told you nothing;
    rendering it as a clean sweep would convert a broken probe into confident
    permission to duplicate every record it failed to read."""
    import json as _json
    corpus = _json.dumps([SKIPPED_OWNER])

    missing = tmp_path / "does-not-exist.json"
    monkeypatch.setattr(sys, "stdin", _FakeStdin(corpus))
    assert sq.main(["--subjects-file", str(missing)]) == 2
    capsys.readouterr()

    empty = tmp_path / "empty.json"
    empty.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", _FakeStdin(corpus))
    assert sq.main(["--subjects-file", str(empty)]) == 2
    capsys.readouterr()

    garbage = tmp_path / "garbage.json"
    garbage.write_text("not json at all", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", _FakeStdin(corpus))
    assert sq.main(["--subjects-file", str(garbage)]) == 2
    capsys.readouterr()


def test_key_widening_is_proven_not_assumed():
    """guard-2041: an unchanged count cannot distinguish "widened correctly"
    from "did nothing at all" — both produce the identical number. So prove a
    widening with a DISCRIMINATING probe: the OLD form here was a literal
    record["observation"] read, and two numbers that differ are the evidence.
    Without this, every test above would still pass if extract_subject only
    ever looked at `observation`, because that is the key the other fixtures
    use."""
    records = [{"observation": UNOWNED}, {"content": UNOWNED},
               {"finding": UNOWNED}, {"proposed_work": UNOWNED}]
    old_form = [r for r in records if r.get("observation")]
    new_form = [r for r in records if sq.extract_subject(r)[0]]
    assert len(old_form) == 1, old_form
    assert len(new_form) == 4, new_form
    # ...and the newly-visible records reach a real verdict, not merely a parse
    res = sq.batch_decide(records, [SKIPPED_OWNER], NOW, SESSION_START)
    assert res["population"]["records_scored"] == 4, res["population"]
    assert res["population"]["records_unreadable"] == 0, res["population"]


# ─── proposed_work is APPENDED, not shadowed by the envelope () ───
#
# MEASURED, not invented (guard-645): over the live 2,082-record capture corpus
# on 2026-09-21, `observation` is present on 2,074 records and `proposed_work`
# on exactly THREE — one list[dict] with title/detail/priority, two plain str.
# Because `observation` came FIRST in OBSERVATION_KEYS and extract_subject
# returned on first match, any record carrying both scored on `observation`
# alone. The fixture below is the SHAPE of the real guard-7119 record: a
# contentless envelope sentence over a list-shaped proposed_work.

ENVELOPE = ("Two NEW defects found while resolving outcome 3. Worker Body "
            "cannot file (Case B, observable by any Body) -- relaying for "
            "the reducer to file.")


def test_envelope_observation_no_longer_shadows_proposed_work():
    """THE DEFECT, pinned. The old form scored 156 chars of envelope, matched
    nothing, and returned FILE with an empty candidate list — which is
    indistinguishable from honest novelty (guard-7119)."""
    rec = {"observation": ENVELOPE,
           "proposed_work": [{"title": "Unblock: " + UNOWNED[:40],
                              "detail": UNOWNED, "priority": "HIGH"}]}
    subject, key = sq.extract_subject(rec)
    assert key == "observation+proposed_work", key
    assert ENVELOPE in subject, "the envelope must be kept, not replaced"
    assert UNOWNED in subject, "the ITEM content is what was missing"
    # the discriminator: the old first-match-wins form saw only the envelope
    assert len(subject) > len(ENVELOPE)


def test_contentless_envelope_over_an_owned_item_returns_DECLINE():
    """The goal's own check. Scoring the envelope alone returns FILE; scoring
    the ITEM finds the owner. Both directions are asserted, because only the
    pair shows the fix did something."""
    owned_detail = COMPLETED_OWNER["title"]
    envelope_only = {"observation": ENVELOPE}
    with_items = {"observation": ENVELOPE,
                  "proposed_work": [{"title": owned_detail,
                                     "detail": owned_detail}]}
    before = sq.batch_decide([envelope_only], [COMPLETED_OWNER], NOW, SESSION_START)
    after = sq.batch_decide([with_items], [COMPLETED_OWNER], NOW, SESSION_START)
    assert before["rows"][0]["verdict"] == "FILE", before["rows"][0]
    assert after["rows"][0]["verdict"] == "DECLINE", after["rows"][0]


def test_subject_keys_used_still_enumerates_every_key_consumed():
    """Outcome 3. `subject_keys_used` is the field that made this diagnosable —
    it reported {'observation': 44, 'text': 1} and no proposed_work key at all.
    The composite must appear there, or the next reader loses the same tell."""
    rec = {"observation": ENVELOPE, "proposed_work": UNOWNED}
    res = sq.batch_decide([rec], [SKIPPED_OWNER], NOW, SESSION_START)
    assert res["population"]["subject_keys_used"] == {"observation+proposed_work": 1}, \
        res["population"]


def test_flatten_proposed_work_handles_every_measured_and_malformed_shape():
    """`priority` is excluded deliberately: it is routing metadata, and folding
    it in would inject the corpus-common token HIGH into every subject, which a
    token-overlap scorer reads as signal. Malformed shapes render empty rather
    than raising — one bad record must not abort the walk (guard-1512).

    A plain loop, not @pytest.mark.parametrize: this module imports no pytest
    (it is run both by pytest and directly), and adding the import here to gain
    a decorator would be a new dependency for cosmetics."""
    cases = [
        ("a plain string", "a plain string"),
        ([{"title": "T", "detail": "D"}], "T D"),
        ([{"title": "T", "detail": "D", "priority": "HIGH"}], "T D"),  # priority NOT rendered
        (["one", "two"], "one two"),
        ([], ""),
        (None, ""),
        (42, ""),                                   # malformed: empty, never raises
        ([{"priority": "HIGH"}], ""),
    ]
    for pw, expected in cases:
        assert sq.flatten_proposed_work(pw) == expected, pw


def test_the_2079_records_without_proposed_work_are_byte_identical():
    """THE BLAST-RADIUS CONTROL, and the reason this fix is APPEND rather than
    PROMOTE. A change to what a scoring analyzer observes changes the metric's
    semantics (rb-4988), so the 99.6% of records carrying no proposed_work must
    score on exactly the bytes they scored on before."""
    for rec in ({"observation": UNOWNED}, {"content": UNOWNED},
                {"finding": UNOWNED}, {"text": UNOWNED}):
        subject, key = sq.extract_subject(rec)
        assert subject == UNOWNED, (key, subject)
        assert "+" not in key, "no composite key when proposed_work is absent"


# ── : subject headline cap kills coincidental body collisions ─────
# A relay observation is a 1-2 KB blob whose cited-evidence tail carries rare
# identifiers (goal ids, filenames) that let an UNRELATED owner win the rare
# gate by coincidence -- the silent false-DECLINE direction (guard-5147).
# decide() now scores only the relay's HEADLINE. These tests pin the MECHANISM
# at a LIVE-IDF corpus size (reusing _big_corpus's df shape so the rare gate is
# real). The named pairs from the goal description are pinned on their real
# relay texts further down (fixtures/sq013_relay_pairs.json).

def test_headline_is_a_noop_for_short_subjects():
    """Every short subject -- the test corpus, the positive control -- passes
    through unchanged, which is why every decide() test above still holds."""
    for s in (RELAY, UNOWNED, COMPLETED_OWNER["title"], "", "one two three"):
        assert sq._headline(s) == (s or "").strip()


def test_headline_caps_a_long_relay_and_drops_the_evidence_tail():
    head = ("mutation-backup leak: the seed-transplant orphan sweep deletes "
            "destination files with a bare unlink while a working backup "
            "routine sits unused in the same script.")
    tail = (" Evidence spans many goals. The zakpodmonitor liveness alerts "
            "cited here are coincidental token collisions, not the real owner. "
            + "padding detail sentence about unrelated matters. " * 8)
    capped = sq._headline(head + tail)
    assert len(capped) <= sq.SUBJECT_HEADLINE_MAX_CHARS
    assert "zakpodmonitor" not in capped.lower()      # the tail identifier is gone
    assert "mutation-backup" in capped.lower()         # the aboutness survives


def test_headline_never_cuts_on_a_dot_inside_a_token():
    """A filename dot (`x.j2`) or version (`6.06x`) is not a sentence boundary,
    so a long run-on first line is capped at the char ceiling, not shredded at
    the first internal dot."""
    s = "fix aspiration_evolution.j2 rendering and the 6.06x overdue math " * 20
    h = sq._headline(s)
    assert len(h) >= sq.SUBJECT_HEADLINE_MIN_CHARS
    assert h.startswith("fix aspiration_evolution.j2")


# An unrelated owner that shares ONLY a rare identifier that appears in a relay's
# evidence tail. Reuses _big_corpus for a live-IDF df distribution.
_ALIEN_OWNER = {
    "id": "g-115-10641",
    "status": "completed",
    "completed_date": "2026-08-27T01:30:00",
    "title": "zakpod1 monitor liveness alerting",
    "description": ("Monitors the zakpodmonitor process and alerts on "
                    "staleness in the liveness lane."),
}

_BIG_RELAY_COINCIDENTAL = (
    # HEADLINE: a backup-sweep subject nothing in the corpus owns.
    "mutation-backup leak: the seed-transplant orphan sweep deletes destination "
    "files with a bare unlink while a working backup routine sits unused in the "
    "same script."
    # TAIL: coincidentally names the alien owner's rare identifier.
    " Evidence spans many goals. The zakpodmonitor liveness alerts cited by the "
    "scorer here are a coincidental token collision, not the real owner of this "
    "backup defect. " + "further unrelated detail. " * 8
)


def test_the_collision_is_real_a_short_subject_with_the_tail_token_declines():
    """Control: the coincidental collision IS real. A SHORT subject (below the
    cap) carrying the alien owner's rare identifier DECLINEs -- this is what the
    big relay's tail does when it reaches the scorer uncapped."""
    corpus = _big_corpus(with_owner=False) + [_ALIEN_OWNER]
    short_collision = "the zakpodmonitor liveness alerts flagged in this note"
    assert len(short_collision) <= sq.SUBJECT_HEADLINE_MAX_CHARS   # not capped
    r = sq.decide(short_collision, corpus, NOW, SESSION_START)
    assert r["decision"] == "DECLINE", r
    assert r["cited_goal_id"] == "g-115-10641", r


def test_big_relay_files_because_the_cap_removes_the_collision_tail():
    """THE FIX for . The big relay's HEADLINE is about a backup
    defect nothing owns; its TAIL coincidentally names the alien owner's rare
    identifier. Uncapped it would DECLINE (see the short-subject control);
    decide() caps to the headline, so it FILEs."""
    corpus = _big_corpus(with_owner=False) + [_ALIEN_OWNER]
    r = sq.decide(_BIG_RELAY_COINCIDENTAL, corpus, NOW, SESSION_START)
    assert r["decision"] == "FILE", r


def test_genuine_headline_match_still_declines_at_large_subject_scale():
    """The SAFE-DIRECTION control: the cap must not turn a genuine duplicate
    into a false FILE. A large relay whose HEADLINE is about the real owner
    still finds and cites that owner after capping -- a genuine duplicate names
    its subject up front, so its shared rare token survives the cap. The
    headline's extra clause dilutes the owner's coverage to the multi-rare band
    (0.32), so since g-115-11127 the verdict is MUST-READ rather than DECLINE;
    what this test pins is the cap property: never FILE, same owner cited."""
    big_genuine = (
        "pickNearbyPlayer returns null without instrumentation so the "
        "denominator for nearby-player selection is unmeasured and the scorer "
        "path cannot be verified against real play."
        + " Supporting evidence follows across many goals and files. " * 8
    )
    corpus = _big_corpus(with_owner=True)
    r = sq.decide(sq._headline(big_genuine), corpus, NOW, SESSION_START)
    assert r["decision"] in ("DECLINE", "MUST-READ"), r
    assert r["cited_goal_id"] == "g-326-711", r


def test_positive_control_still_files_under_the_cap():
    """The alien subject stays FILE: the cap never manufactures a match."""
    corpus = _big_corpus(with_owner=False) + [_ALIEN_OWNER]
    r = sq.decide(UNOWNED, corpus, NOW, SESSION_START)
    assert r["decision"] == "FILE", r


# ──  second half: the coverage floor, pinned on the REAL relays ───
# fixtures/sq013_relay_pairs.json holds the archived relay texts named in the
# goal description, the title+description of each cited owner, and the live
# corpus statistics (n, avgdl, per-token df) measured 2026-09-25. Scoring runs
# at those live document frequencies, because the incident only exists there:
# a fixture-sized corpus has no rare tokens to collide on (see _big_corpus).
# Each pair is scored against a corpus holding ONLY its cited owner plus filler
# records sized to the live avgdl, so a FILE can only mean "that owner was not
# cited" and the BM25 length factor is the one the live probe applied.

_FIXTURE = json.loads((SCRIPT_DIR / "fixtures" / "sq013_relay_pairs.json")
                      .read_text(encoding="utf-8"))
_PAIRS = {p["label"]: p for p in _FIXTURE["pairs"]}
_FALSE = [p for p in _FIXTURE["pairs"] if p["kind"] == "false"]
_GENUINE = [p for p in _FIXTURE["pairs"] if p["kind"] == "genuine"]
_LIVE_NOW = datetime(2026, 9, 25, 17, 0, 0)


def _live_idf(monkeypatch, fixture=_FIXTURE):
    """Replace the corpus-derived IDF with the measured live one."""
    n, df = fixture["n"], fixture["df"]

    def live(docs, terms):
        return {t: (df.get(t, 0), max(0.0, math.log(n / (1 + df.get(t, 0)))))
                for t in terms}, n
    monkeypatch.setattr(sq, "_compute_idf", live)


def _owner_corpus(owner_id, fillers=49, fixture=_FIXTURE):
    owner = fixture["owners"][owner_id]
    dl = len(sq._tokens(owner["title"] + " " + owner["description"]))
    width = round((fixture["avgdl"] * (fillers + 1) - dl) / fillers)
    text = " ".join("fillerqq%04d" % j for j in range(width))
    return [owner] + [{"id": "g-999-%03d" % i, "status": "completed",
                       "title": "filler", "description": text}
                      for i in range(fillers)]


def _score(pair):
    return sq.decide(pair["relay"], _owner_corpus(pair["owner_id"]),
                     _LIVE_NOW, None, 900.0)


def test_fixture_covers_eight_false_pairs_and_three_genuine_relays():
    assert len(_FALSE) == 8 and len(_GENUINE) == 3, _FIXTURE["pairs"]
    assert _FIXTURE["n"] >= 20 * sq.MIN_IDF_CORPUS    # a LIVE-sized corpus


def test_false_pairs_no_longer_decline_citing_the_unrelated_owner(monkeypatch):
    """Outcome 1 of : each relay FILEs against its coincidental
    owner instead of declining on one shared identifier."""
    _live_idf(monkeypatch)
    for pair in _FALSE:
        r = _score(pair)
        assert r["decision"] == "FILE", (pair["label"], pair["about"], r)
        assert r["cited_goal_id"] != pair["owner_id"], (pair["label"], r)


def test_genuine_pairs_still_decline_citing_their_owner(monkeypatch):
    """Outcome 2: the coverage floor must not turn a real owner into a FILE."""
    _live_idf(monkeypatch)
    for pair in _GENUINE:
        r = _score(pair)
        assert r["decision"] == "DECLINE", (pair["label"], r)
        assert r["cited_goal_id"] == pair["owner_id"], (pair["label"], r)
        assert r["matches"][0]["coverage"] >= sq.SUBJECT_COVERAGE_MIN, r


def test_the_false_pairs_are_the_measured_incident_not_a_green_default(
        monkeypatch):
    """With BOTH fixes off, all 8 relays DECLINE citing the named owner (the
    incident). With only the headline cap on, 5 still do: the cap alone was
    not the fix, and the coverage floor is what flips them."""
    _live_idf(monkeypatch)
    monkeypatch.setattr(sq, "SUBJECT_COVERAGE_MIN", 0.0)
    monkeypatch.setattr(sq, "SUBJECT_COVERAGE_MIN_MULTI_RARE", 0.0)
    cap_survivors = {p["label"] for p in _FALSE
                     if _score(p)["cited_goal_id"] == p["owner_id"]}
    assert cap_survivors == {"F2", "F4", "F5", "F8", "F9"}, cap_survivors
    monkeypatch.setattr(sq, "_headline", lambda s: str(s or "").strip())
    for pair in _FALSE:
        r = _score(pair)
        assert r["decision"] == "DECLINE", (pair["label"], r)
        assert r["cited_goal_id"] == pair["owner_id"], (pair["label"], r)


def test_positive_control_files_at_live_idf(monkeypatch):
    """The alien subject stays FILE against every owner corpus above."""
    _live_idf(monkeypatch)
    for pair in _FIXTURE["pairs"]:
        r = sq.decide(UNOWNED, _owner_corpus(pair["owner_id"]), _LIVE_NOW,
                      None, 900.0)
        assert r["decision"] == "FILE", (pair["label"], r)


def test_owner_coverage_is_read_from_its_opening_not_its_tail(monkeypatch):
    """A record that restates the subject only past OWNER_HEAD_CHARS (the
    accumulated-tail sponge shape) does not own it. The control proves the head
    cap is what flips it: read whole, the same record is cited."""
    _live_idf(monkeypatch)
    pair = _PAIRS["G1"]
    real = _FIXTURE["owners"][pair["owner_id"]]
    pad = " ".join("fillerqq%04d" % j
                   for j in range(sq.OWNER_HEAD_CHARS // 12 + 1))
    sponge = {"id": "g-999-999", "status": "pending",
              "title": "Recurring: unrelated maintenance chore",
              "description": pad + " " + real["description"]}
    corpus = [sponge] + _owner_corpus(pair["owner_id"])[1:]
    r = sq.decide(pair["relay"], corpus, _LIVE_NOW, None, 900.0)
    assert r["decision"] == "FILE", r
    monkeypatch.setattr(sq, "OWNER_HEAD_CHARS", 10 ** 6)
    r = sq.decide(pair["relay"], corpus, _LIVE_NOW, None, 900.0)
    assert r["decision"] == "DECLINE", r
    assert r["cited_goal_id"] == "g-999-999", r


# -- : the terminal window anchors on the RELAY's own _item_ts -------
# A BACKLOG relay is SCORED weeks after it was captured (the reducer spark
# replay drains the oldest of a large capture slot). Its owner most likely went
# terminal near the relay's OWN capture time, not near replay-time, so a window
# anchored on `now` (replay-time) is structurally blind to it and every
# backlog-age relay reads FILE. MEASURED at the  close (2026-09-26):
# 25 backlog relays (2-5 weeks old), ALL FILE, including a security relay
# already fixed upstream. decide() now anchors the terminal window on a
# per-record reference_time; batch_decide reads it from each record's _item_ts
# (wm.py stamps that on every capture append) and falls back to `now` when
# absent, so every test above is unchanged. These fixtures still HOLD the old
# owner; the live queue usually has evicted it (see the eviction-horizon
# section below, ).
#
# guard-4166 governs: the fix makes a false FILE STOP APPEARING, so every
# DECLINE assertion is paired with an alien-token FILE control that must NOT
# flip, and the mutation proof shows the now-anchored path still FILEs.
# guard-2613: _item_ts and the owner's completed_date are on the SAME clock
# (both naive UTC wall time by fleet convention), so _parse_ts on one is
# comparable to _goal_time on the other.

BACKLOG_NOW = datetime(2026, 9, 26, 12, 0, 0)          # replay-time: weeks later
# The owner went terminal ~a month before replay-time -- far past the 72h
# lookback from `now`, so a now-anchored window cannot see it.
BACKLOG_OWNER = dict(COMPLETED_OWNER, completed_date="2026-08-28T00:00:00")
# The relay was CAPTURED 6h after that owner closed: within 72h of the owner,
# ~29 days before replay-time.
BACKLOG_ITEM_TS = "2026-08-28T06:00:00"


def test_backlog_relay_declines_on_its_own_item_ts_while_control_still_files():
    """THE  REGRESSION. A relay captured near its owner's terminal date
    but SCORED weeks later must still see that owner. Both rows ride the SAME
    batch against the SAME corpus and the SAME replay-time `now`, so an
    implementation that widened the window for everything would fail on the
    control row. FAILS on the pre-fix probe: there batch_decide anchored on
    `now`, the owner fell ~29 days outside the 72h window, and this row FILEd."""
    flagged = {"observation": RELAY, "_item_ts": BACKLOG_ITEM_TS}
    control = {"observation": UNOWNED, "_item_ts": BACKLOG_ITEM_TS}
    res = sq.batch_decide([flagged, control], [BACKLOG_OWNER], BACKLOG_NOW)
    frow, crow = res["rows"]
    assert frow["verdict"] == "DECLINE", frow
    assert frow["cited_goal_id"] == BACKLOG_OWNER["id"], frow
    assert crow["verdict"] == "FILE", crow             # control must NOT flip


def test_backlog_decline_is_the_item_ts_anchor_not_a_widened_default():
    """MUTATION PROOF (guard-4166 / guard-2903). The SAME owner and subject
    scored at replay-time WITHOUT the relay's _item_ts -- decide()'s now-anchored
    path, which every non-batch caller still takes -- FILEs. So it is the
    per-record _item_ts anchor, not a general widening, that produces the
    DECLINE. Asserts BEHAVIOUR, not source text (guard-6333)."""
    now_anchored = sq.decide(RELAY, [BACKLOG_OWNER], BACKLOG_NOW)   # reference_time=None
    assert now_anchored["decision"] == "FILE", now_anchored
    ts_anchored = sq.decide(RELAY, [BACKLOG_OWNER], BACKLOG_NOW,
                            reference_time=sq._parse_ts(BACKLOG_ITEM_TS))
    assert ts_anchored["decision"] == "DECLINE", ts_anchored
    assert ts_anchored["cited_goal_id"] == BACKLOG_OWNER["id"], ts_anchored


def test_recent_relay_with_a_stale_owner_still_files():
    """DO-NOT-OVERFIRE (guard-5147: a false DECLINE is the silent, permanent
    failure direction). Per-record anchoring widens the window for OLD relays
    only. A relay captured RECENTLY does not reach an owner that closed months
    before it, so the window stays REAL rather than reaching back forever."""
    stale_owner = dict(COMPLETED_OWNER, completed_date="2026-04-01T00:00:00")
    recent = {"observation": RELAY, "_item_ts": "2026-09-26T09:00:00"}
    row = sq.batch_decide([recent], [stale_owner], BACKLOG_NOW)["rows"][0]
    assert row["verdict"] == "FILE", row


def test_backlog_relay_without_item_ts_falls_back_to_now():
    """BACKWARD COMPAT. A record with no _item_ts is anchored on `now` exactly as
    before the fix, so every batch test above (whose records carry no _item_ts)
    is preserved: the same old owner that DECLINEd WITH the anchor FILEs
    without it."""
    no_ts = {"observation": RELAY}                       # no _item_ts key
    row = sq.batch_decide([no_ts], [BACKLOG_OWNER], BACKLOG_NOW)["rows"][0]
    assert row["verdict"] == "FILE", row


# --- the eviction horizon () ----------------------------------------
#
# The anchor tests above run over a corpus that still HOLDS the owner. The live
# queue does not: it evicts terminal non-recurring goals after
# aspirations_eviction.age_days (3), leaving only a bare id in the aspiration's
# census. On the  occ227 replay (2026-09-27) 8 of 16 aged FILE relays
# needed an owner only the census holds, and two of them CITED it. So a FILE
# whose subject cites an evicted goal id must be MUST-READ, and FILE output
# must state the horizon.
#
# guard-3292: each census assertion is paired with the SAME input run without
# the census, where the two paths must disagree. guard-4166: the fix makes a
# bare FILE stop appearing, so an uncited control rides the same batch and must
# NOT flip.

EVICTED_ID = "g-363-72"
CITING_RELAY = UNOWNED + " -- the retry fix from %s never covered it" % EVICTED_ID
# The live shape: `aspirations-read.sh --source world --active-compact` is a
# JSON list of aspiration records carrying archived_census.evicted_ids.
CENSUS = [{"id": "asp-363", "goals": [],
           "archived_census": {"evicted_ids": {"completed": [EVICTED_ID]}}}]


def test_relay_citing_an_evicted_owner_is_must_read_while_control_files():
    citing, control = {"observation": CITING_RELAY}, {"observation": UNOWNED}
    res = sq.batch_decide([citing, control], [COMPLETED_OWNER], NOW,
                          SESSION_START, census=CENSUS)
    crow, ctl = res["rows"]
    assert crow["verdict"] == "MUST-READ", crow
    assert crow["evicted_citations"] == [
        {"goal_id": EVICTED_ID, "status": "completed", "via": "subject"}], crow
    assert EVICTED_ID in crow["reason"], crow
    assert ctl["verdict"] == "FILE", ctl                # control must NOT flip
    assert ctl["evicted_citations"] == [], ctl          # checked, none
    assert (res["must_read_count"], res["file_count"]) == (1, 1), res
    text = sq.render_batch(res)
    assert "cites EVICTED: %s (completed)" % EVICTED_ID in text, text
    assert "guard-7398" in text and "guard-5278" in text, text
    assert "EVICTION CENSUS: 1 aspiration record(s), 1 evicted id(s)" in text


def test_relay_whose_own_source_goal_is_evicted_is_must_read():
    """The  instance (progress_note, cc-10, 2026-09-27): the fix had
    shipped under the relay's OWN source goal g-363-72, which is
    evicted-completed, and the subject cites no id at all. A source goal the
    census does not hold must not flip, and without the census the same
    record FILEs."""
    evicted_src = {"goal_id": EVICTED_ID, "observation": UNOWNED}
    other_src = {"goal_id": COMPLETED_OWNER["id"], "observation": UNOWNED}
    res = sq.batch_decide([evicted_src, other_src], [COMPLETED_OWNER], NOW,
                          SESSION_START, census=CENSUS)
    erow, orow = res["rows"]
    assert erow["verdict"] == "MUST-READ", erow
    assert erow["evicted_citations"] == [
        {"goal_id": EVICTED_ID, "status": "completed", "via": "source"}], erow
    assert "the relay's source goal" in sq.render_batch(res)
    assert orow["verdict"] == "FILE", orow              # control must NOT flip
    bare = sq.batch_decide([evicted_src], [COMPLETED_OWNER], NOW, SESSION_START)
    assert bare["rows"][0]["verdict"] == "FILE", bare["rows"][0]


def test_without_a_census_the_same_relay_files_and_says_not_checked():
    """The disagreeing path (guard-3292): the pre-fix behaviour, now labelled.
    None is NOT CHECKED, which must never read as "checked, none"
    (guard-1753)."""
    res = sq.batch_decide([{"observation": CITING_RELAY}], [COMPLETED_OWNER],
                          NOW, SESSION_START)
    row = res["rows"][0]
    assert row["verdict"] == "FILE", row
    assert row["evicted_citations"] is None, row
    assert res["population"]["census_aspirations"] is None, res["population"]
    assert "EVICTION CENSUS: NOT CHECKED" in sq.render_batch(res)


def test_a_cited_id_the_corpus_holds_was_scored_and_is_not_flagged():
    """Discriminating probe of the live filter: the cited id sits in BOTH the
    corpus and the census, so only the filter keeps this row FILE."""
    cites_live = UNOWNED + " -- see g-326-711"          # COMPLETED_OWNER's id
    census = [{"id": "asp-326", "goals": [], "archived_census": {
        "evicted_ids": {"completed": [COMPLETED_OWNER["id"]]}}}]
    row = sq.batch_decide([{"observation": cites_live}], [COMPLETED_OWNER],
                          NOW, SESSION_START, census=census)["rows"][0]
    assert row["verdict"] == "FILE", row
    assert row["evicted_citations"] == [], row


def test_goal_id_citations_skip_board_ids_and_other_prefixes():
    subj = "see msg-20260928-053714-alpha-173, sig-12-3 and (g-363-72)"
    assert sq._GOAL_ID_RE.findall(subj) == ["g-363-72"]


def test_single_mode_returns_4_with_the_census_and_0_without(monkeypatch,
                                                             capsys, tmp_path):
    census = tmp_path / "census.json"
    census.write_text(json.dumps(CENSUS), encoding="utf-8")
    corpus = json.dumps([COMPLETED_OWNER])
    base = ["--subject", CITING_RELAY, "--now", NOW.isoformat(),
            "--session-start", SESSION_START.isoformat()]

    monkeypatch.setattr(sys, "stdin", _FakeStdin(corpus))
    assert sq.main(base + ["--census-file", str(census)]) == 4
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "MUST-READ", out
    assert out["evicted_citations"][0]["goal_id"] == EVICTED_ID, out
    assert "terminal_horizon" in out, out

    monkeypatch.setattr(sys, "stdin", _FakeStdin(corpus))
    assert sq.main(base) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "FILE", out
    assert out["evicted_citations"] is None, out


def test_an_unreadable_or_empty_census_refuses_rather_than_filing(
        monkeypatch, capsys, tmp_path):
    """A census the caller asked for but could not read would otherwise let a
    FILE pass as census-checked. `{"error": ...}` is the measured shape of a
    failed aspirations-read.sh call."""
    corpus = json.dumps([COMPLETED_OWNER])
    for name, body in (("missing.json", None), ("garbage.json", "not json"),
                       ("empty.json", "[]"),
                       ("error.json", json.dumps({"error": "missing_flag"}))):
        path = tmp_path / name
        if body is not None:
            path.write_text(body, encoding="utf-8")
        monkeypatch.setattr(sys, "stdin", _FakeStdin(corpus))
        assert sq.main(["--subject", CITING_RELAY,
                        "--census-file", str(path)]) == 2, name
        capsys.readouterr()


def test_file_output_states_the_terminal_coverage_horizon():
    h = sq.terminal_horizon(NOW, {"enabled": True, "apply": True,
                                  "age_days": 3})
    assert (h["state"], h["horizon"]) == ("active", "2026-08-24T02:12:21"), h
    aged = {"observation": UNOWNED, "_item_ts": "2026-08-20T00:00:00"}
    res = sq.batch_decide([aged, {"observation": UNOWNED}], [COMPLETED_OWNER],
                          NOW, SESSION_START)
    res["terminal_horizon"] = h
    text = sq.render_batch(res)
    assert "TERMINAL-COVERAGE HORIZON: 2026-08-24T02:12:21" in text, text
    assert "AGED: captured 2026-08-20T00:00:00" in text, text
    assert text.count("AGED:") == 1, text       # the undated row is not aged


def test_horizon_is_unknown_never_a_default_and_off_when_eviction_is_off(
        tmp_path):
    block, err = sq.load_eviction_config(str(tmp_path / "absent.yaml"))
    assert block is None and err, (block, err)
    h = sq.terminal_horizon(NOW, block, err)
    assert (h["state"], h["horizon"]) == ("unknown", None), h
    assert h["detail"].startswith("UNKNOWN"), h
    off = sq.terminal_horizon(NOW, {"enabled": False, "apply": True,
                                    "age_days": 3})
    assert (off["state"], off["horizon"]) == ("inactive", None), off


def test_the_shipped_config_supplies_the_horizon():
    """The default path resolves to the file the evictor's tick reads."""
    block, err = sq.load_eviction_config()
    assert err is None and "age_days" in block, (block, err)


# -- : the multi-rare band is MUST-READ, not a terminal DECLINE ----
# fixtures/sq013_same_topic_pairs.json holds the relay texts of the 
# occ228 replay batch (S1, S2, T1-T5) and of the occ238 wrong-owner DECLINEs
# (S3-S6), each cited owner's title+description, and the live corpus statistics
# (n, avgdl, per-token df) measured 2026-09-28. kind=false: the replay DECLINED
# to a same-topic owner tracking a DIFFERENT defect; kind=genuine: a correct
# DECLINE from the same batch. Scored like the  pairs above: the
# cited owner alone plus fillers sized to the live avgdl.
#
# guard-4166: the fix makes a terminal DECLINE stop appearing, so the genuine
# pairs must NOT flip and the alien-token control must stay FILE. guard-4315:
# narrowing a predicate drops cases nobody inventoried, so the survivors are
# pinned by name, not by a count.

_TOPIC = json.loads((SCRIPT_DIR / "fixtures" / "sq013_same_topic_pairs.json")
                    .read_text(encoding="utf-8"))
_TOPIC_PAIRS = {p["label"]: p for p in _TOPIC["pairs"]}
_TOPIC_FALSE = [p for p in _TOPIC["pairs"] if p["kind"] == "false"]
_TOPIC_GENUINE = [p for p in _TOPIC["pairs"] if p["kind"] == "genuine"]
_REPLAY_NOW = datetime(2026, 9, 27, 9, 10, 0)            # the occ228 replay


def _topic_score(pair, corpus=None):
    return sq.decide(pair["relay"],
                     corpus or _owner_corpus(pair["owner_id"], fixture=_TOPIC),
                     _REPLAY_NOW, None, 900.0)


def test_same_topic_fixture_holds_the_measured_cases():
    """The two cases the goal names, the five correct DECLINEs it names, and
    the four occ238 wrong owners, each against the owner the replay cited."""
    assert {(p["label"], p["owner_id"]) for p in _TOPIC["pairs"]} == {
        ("S1", "g-115-8349"), ("S2", "g-115-10745"), ("S3", "g-115-10131"),
        ("S4", "g-115-7235"), ("S5", "g-115-8716"), ("S6", "g-249-50"),
        ("T1", "g-115-7455"), ("T2", "g-115-7455"), ("T3", "g-374-59"),
        ("T4", "g-115-7466"), ("T5", "g-115-7976")}
    assert _TOPIC_PAIRS["S1"]["source_goal"] == "g-115-7319"
    assert _TOPIC_PAIRS["S2"]["source_goal"] == "g-363-75"
    assert _TOPIC["n"] >= 20 * sq.MIN_IDF_CORPUS         # a LIVE-sized corpus


def test_same_topic_declines_are_the_measured_incident_not_a_green_default(
        monkeypatch):
    """With the band routed back to DECLINE, every false pair DECLINEs citing
    the owner the replay cited, from inside the multi-rare band -- the
    incident, reproduced at live document frequencies."""
    band_top = sq.SUBJECT_COVERAGE_MIN
    _live_idf(monkeypatch, _TOPIC)
    monkeypatch.setattr(sq, "SUBJECT_COVERAGE_MIN",
                        sq.SUBJECT_COVERAGE_MIN_MULTI_RARE)
    for pair in _TOPIC_FALSE:
        r = _topic_score(pair)
        assert r["decision"] == "DECLINE", (pair["label"], r)
        assert r["cited_goal_id"] == pair["owner_id"], (pair["label"], r)
        top = r["matches"][0]
        assert sq.SUBJECT_COVERAGE_MIN_MULTI_RARE <= top["coverage"] < band_top
        assert len(top["rare_tokens"]) >= 2, (pair["label"], top)


def test_same_topic_owner_is_must_read_and_still_cited(monkeypatch):
    """Outcome 1: no terminal DECLINE. The owner stays CITED -- it may still be
    the owner -- so the reader has one goal to open, not a search to redo."""
    _live_idf(monkeypatch, _TOPIC)
    for pair in _TOPIC_FALSE:
        r = _topic_score(pair)
        assert r["decision"] == "MUST-READ", (pair["label"], r)
        assert r["cited_goal_id"] == pair["owner_id"], (pair["label"], r)
        assert r["matches"][0]["weak"] is True, (pair["label"], r)
        assert r["reason"].startswith("same topic, not proven"), r


def test_the_batch_correct_declines_still_decline(monkeypatch):
    """Outcome 2: the five correct DECLINEs of the same batch survive."""
    _live_idf(monkeypatch, _TOPIC)
    for pair in _TOPIC_GENUINE:
        r = _topic_score(pair)
        assert r["decision"] == "DECLINE", (pair["label"], r)
        assert r["cited_goal_id"] == pair["owner_id"], (pair["label"], r)
        assert r["matches"][0]["coverage"] >= sq.SUBJECT_COVERAGE_MIN, r
        assert r["matches"][0]["weak"] is False, r


def test_positive_control_files_against_every_same_topic_owner(monkeypatch):
    _live_idf(monkeypatch, _TOPIC)
    for pair in _TOPIC["pairs"]:
        r = sq.decide(UNOWNED, _owner_corpus(pair["owner_id"], fixture=_TOPIC),
                      _REPLAY_NOW, None, 900.0)
        assert r["decision"] == "FILE", (pair["label"], r)


def test_a_title_token_rule_would_have_broken_two_correct_declines():
    """The direction the goal proposed, measured and rejected: T1 and T5 are
    correct DECLINEs whose owner's TITLE shares no token with the relay's
    scored headline (T1 writes `closure-evidence-write`, the title says
    `closure-evidence`), so requiring one would turn them into MUST-READ."""
    for label in ("T1", "T5"):
        pair = _TOPIC_PAIRS[label]
        title = _TOPIC["owners"][pair["owner_id"]]["title"]
        head = sq._tokens(sq._headline(pair["relay"]))
        assert head & sq._tokens(title) == set(), (label, head & sq._tokens(title))


def test_a_full_floor_owner_outranks_a_heavier_same_topic_one(monkeypatch):
    """A weak candidate must never hide a real owner: an owner restating the
    relay in its title is cited over S1's same-topic owner although its long
    record weighs less. Control: without it, the same relay is MUST-READ."""
    _live_idf(monkeypatch, _TOPIC)
    pair = _TOPIC_PAIRS["S1"]
    head = sq._headline(pair["relay"])
    solid = {"id": "g-999-900", "status": "pending", "title": head[:200],
             "description": head + " " + " ".join(
                 "solidpad%04d" % j for j in range(1500))}
    corpus = [solid] + _owner_corpus(pair["owner_id"], fixture=_TOPIC)
    r = _topic_score(pair, corpus)
    assert r["decision"] == "DECLINE", r
    assert r["cited_goal_id"] == "g-999-900", r
    weak = [m for m in r["matches"] if m["goal_id"] == pair["owner_id"]]
    assert weak and weak[0]["weak"], r["matches"]          # listed, not hidden
    assert weak[0]["weight"] > r["matches"][0]["weight"], r["matches"]
    assert _topic_score(pair)["decision"] == "MUST-READ"


def test_batch_row_names_the_same_topic_reading_and_the_matched_span(
        monkeypatch):
    """Batch shape: the S1 row is MUST-READ with its own instruction (not the
    skipped-owner text) and counts toward rc 4; the T1 row DECLINEs, and every
    cited owner's row says what the match rested on -- T1's is description
    only, which is why the band, not the title, decides."""
    _live_idf(monkeypatch, _TOPIC)
    s1, t1 = _TOPIC_PAIRS["S1"], _TOPIC_PAIRS["T1"]
    corpus = (_owner_corpus(s1["owner_id"], fixture=_TOPIC)
              + [_TOPIC["owners"][t1["owner_id"]]])
    res = sq.batch_decide([{"goal_id": s1["source_goal"],
                            "observation": s1["relay"]},
                           {"goal_id": t1["source_goal"],
                            "observation": t1["relay"]}],
                          corpus, _REPLAY_NOW, None, 900.0)
    srow, trow = res["rows"]
    assert (srow["verdict"], srow["weak_owner"], srow["must_read"]) == (
        "MUST-READ", True, True), srow
    assert srow["cited_goal_id"] == s1["owner_id"], srow
    assert (trow["verdict"], trow["weak_owner"]) == ("DECLINE", False), trow
    assert (res["must_read_count"], res["decline_count"]) == (1, 1), res
    text = sq.render_batch(res)
    assert text.count("SAME TOPIC, NOT PROVEN THE SAME DEFECT") == 1, text
    assert "TERMINAL-BUT-NOT-DONE" not in text, text
    assert text.count("matched on:") == 2, text
    assert "owner TITLE shares NOTHING (description only)" in text, text
