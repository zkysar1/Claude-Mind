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
