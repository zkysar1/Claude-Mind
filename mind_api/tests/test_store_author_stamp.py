"""Writing-agent provenance stamp at the store append chokepoint ().

Baseline that motivated this: authorship existed on 1 of 7,940 rows across the
three sibling knowledge stores (reasoning-bank 5,966 rows with `encoded_by` on
exactly one; guardrails 1,939 with no authorship key at all; pattern-signatures
79 with none). Every lesson the fleet encodes was anonymous.

The stamp is ONE emitter — `endpoints/store.py::append` reading
`StoreSpec.author_field` — plus one declaration per store. That split is what
these tests pin, and the reason each store gets its OWN end-to-end case rather
than a parametrized sweep: the three declarations are three separable mutation
sites (guard-1861), so deleting `author_field` from exactly one StoreSpec must
redden exactly one test. A parametrized case would blur which one.

Every positive case reads the record back OFF DISK rather than trusting the
response body. A stamp applied to the in-memory record and lost before the
atomic write would return a perfect 200 with the field present, which is the
failure this whole family of tests exists to catch.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def _post(port: int, path: str, query: dict = None, body: bytes = b"",
          *, agent: str = "alpha"):
    qs = urllib.parse.urlencode(query) if query else ""
    url = (f"http://127.0.0.1:{port}{path}?{qs}" if qs
           else f"http://127.0.0.1:{port}{path}")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if agent:
        req.add_header("X-Mind-Agent", agent)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _lines(path: Path):
    if not path.exists():
        return []
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _records(path: Path):
    return [json.loads(ln) for ln in _lines(path)]


def _by_id(path: Path, rec_id: str) -> dict:
    for r in _records(path):
        if r.get("id") == rec_id:
            return r
    raise AssertionError("%s not found on disk in %s" % (rec_id, path.name))


def _world(project_root: Path, name: str) -> Path:
    return project_root / "world" / name


def _rb_rec(**kw) -> dict:
    base = {"title": "stamp probe", "type": "success", "category": "test-cat",
            "content": "probe", "applies_to": "framework"}
    base.update(kw)
    return base


def _guard_rec(**kw) -> dict:
    base = {"rule": "stamp probe", "category": "test-guard",
            "trigger_condition": "probe", "source": "g-306-109-test"}
    base.update(kw)
    return base


def _patsig_rec(**kw) -> dict:
    base = {"name": "stamp probe", "description": "probe",
            "conditions": ["c-a"], "expected_outcome": "o-x"}
    base.update(kw)
    return base


# ===========================================================================
# End-to-end wiring — one per store == one mutation site per test
# ===========================================================================

def test_rb_append_stamps_writing_agent(running_daemon):
    """Mutation site 1: STORE_REGISTRY['reasoning-bank'].author_field."""
    project_root, port = running_daemon
    status, _ = _post(port, "/v1/store/append", {"store": "reasoning-bank"},
                      json.dumps(_rb_rec(id="rb-901")).encode("utf-8"),
                      agent="zeta")
    assert status == 200
    assert _by_id(_world(project_root, "reasoning-bank.jsonl"),
                  "rb-901")["encoded_by"] == "zeta"


def test_guard_append_stamps_writing_agent(running_daemon):
    """Mutation site 2: STORE_REGISTRY['guardrails'].author_field.

    Also the coupling test for the allowlist. guardrails is the only one of the
    three with a strict unknown-field gate, and `append` stamps BEFORE it
    validates — so removing `encoded_by` from GUARD_KNOWN_FIELDS does not
    merely drop the stamp, it 400s EVERY guardrail write. The status assertion
    is load-bearing for that half; the disk read is load-bearing for the other.
    """
    project_root, port = running_daemon
    status, body = _post(port, "/v1/store/append", {"store": "guardrails"},
                         json.dumps(_guard_rec(id="guard-901")).encode("utf-8"),
                         agent="zeta")
    assert status == 200, body
    assert _by_id(_world(project_root, "guardrails.jsonl"),
                  "guard-901")["encoded_by"] == "zeta"


def test_patsig_append_stamps_writing_agent(running_daemon):
    """Mutation site 3: STORE_REGISTRY['pattern-signatures'].author_field."""
    project_root, port = running_daemon
    status, body = _post(port, "/v1/store/append", {"store": "pattern-signatures"},
                         json.dumps(_patsig_rec(id="sig-901")).encode("utf-8"),
                         agent="zeta")
    assert status == 200, body
    assert _by_id(_world(project_root, "pattern-signatures.jsonl"),
                  "sig-901")["encoded_by"] == "zeta"


def test_stamp_uses_the_request_header_not_a_constant(running_daemon):
    """Two agents, two records, two different values.

    A hardcoded stamp — or one resolving from ambient env / the daemon's own
    identity instead of the caller's header — passes every test above and fails
    only here. The whole point of the field is telling writers APART.
    """
    project_root, port = running_daemon
    _post(port, "/v1/store/append", {"store": "reasoning-bank"},
          json.dumps(_rb_rec(id="rb-902")).encode("utf-8"), agent="alpha")
    _post(port, "/v1/store/append", {"store": "reasoning-bank"},
          json.dumps(_rb_rec(id="rb-903")).encode("utf-8"), agent="bravo")
    rb = _world(project_root, "reasoning-bank.jsonl")
    assert _by_id(rb, "rb-902")["encoded_by"] == "alpha"
    assert _by_id(rb, "rb-903")["encoded_by"] == "bravo"


# ===========================================================================
# Never-overwrite: the caller-wins contract
# ===========================================================================

def test_explicit_author_value_is_preserved(running_daemon):
    """A caller that knows better than the header wins — a backfill tool or a
    cross-agent relay writing on someone else's behalf."""
    project_root, port = running_daemon
    status, body = _post(
        port, "/v1/store/append", {"store": "reasoning-bank"},
        json.dumps(_rb_rec(id="rb-904", encoded_by="charlie")).encode("utf-8"),
        agent="zeta")
    assert status == 200, body
    assert _by_id(_world(project_root, "reasoning-bank.jsonl"),
                  "rb-904")["encoded_by"] == "charlie"


def test_explicit_author_value_is_preserved_on_guardrails(running_daemon):
    """Same contract on the allowlisted store — an explicit value must pass the
    unknown-field gate too, not just the emitter."""
    project_root, port = running_daemon
    status, body = _post(
        port, "/v1/store/append", {"store": "guardrails"},
        json.dumps(_guard_rec(id="guard-904", encoded_by="charlie")).encode("utf-8"),
        agent="zeta")
    assert status == 200, body
    assert _by_id(_world(project_root, "guardrails.jsonl"),
                  "guard-904")["encoded_by"] == "charlie"


def test_explicit_null_author_is_preserved(running_daemon):
    """THE discriminating case. The emitter must test PRESENCE, not truthiness.

    `if not rec.get(f)` passes every other test in this file and fails only
    here — and failing here means the endpoint silently overwrites a caller's
    deliberate "authorship unknown" with whichever agent happened to relay the
    write, manufacturing provenance that was explicitly disclaimed. Matches the
    caller-wins-including-null contract `_rb_inject_source_goal` documents.
    """
    project_root, port = running_daemon
    status, body = _post(
        port, "/v1/store/append", {"store": "reasoning-bank"},
        json.dumps(_rb_rec(id="rb-905", encoded_by=None)).encode("utf-8"),
        agent="zeta")
    assert status == 200, body
    assert _by_id(_world(project_root, "reasoning-bank.jsonl"),
                  "rb-905")["encoded_by"] is None


# ===========================================================================
# Negative controls
# ===========================================================================

def test_store_without_author_field_is_not_stamped(running_daemon):
    """Opt-in, not blanket. An emitter that stamped every store unconditionally
    passes all six tests above; this is the only one that catches it.

    spark-questions declares no `author_field` (and is a META store, so it also
    proves the emitter is not keyed on the world root).
    """
    project_root, port = running_daemon
    rec = {"type": "question", "text": "does the stamp leak?", "category": "surprise"}
    status, body = _post(port, "/v1/store/append", {"store": "spark-questions"},
                         json.dumps(rec).encode("utf-8"), agent="zeta")
    assert status == 200, body
    written = json.loads(body)["record"]
    on_disk = _by_id(project_root / "meta" / "spark-questions.jsonl", written["id"])
    assert "encoded_by" not in on_disk


def test_set_field_does_not_stamp_authorship(running_daemon):
    """Scoped to append. Whoever edits an existing record is an amender, not its
    author; stamping on set-field would rewrite history to name whoever last
    touched the row. `amended_fields` already carries the amendment trail."""
    project_root, port = running_daemon
    _post(port, "/v1/store/append", {"store": "reasoning-bank"},
          json.dumps(_rb_rec(id="rb-906")).encode("utf-8"), agent="alpha")
    status, body = _post(port, "/v1/store/set-field",
                         {"store": "reasoning-bank", "id": "rb-906",
                          "field": "status", "value": "retired"})
    assert status == 200, body
    rec = _by_id(_world(project_root, "reasoning-bank.jsonl"), "rb-906")
    assert rec["status"] == "retired"
    assert rec["encoded_by"] == "alpha"   # still the APPENDER, not the amender


def test_replace_preserves_authorship_it_did_not_write(running_daemon):
    """A full replace must not erase or reassign the original author.

    `replace` substitutes the record wholesale, so a caller body that simply
    omits `encoded_by` would drop it — making the stamp reliably written and
    unreliably retained. Live path, not theoretical: pattern-signatures-update.sh
    drives this endpoint against a stamped store. Same immutability the endpoint
    already grants `created` three lines above, for the same reason.
    """
    project_root, port = running_daemon
    _post(port, "/v1/store/append", {"store": "pattern-signatures"},
          json.dumps(_patsig_rec(id="sig-908")).encode("utf-8"), agent="alpha")

    body = _patsig_rec(id="sig-908", name="renamed by someone else")
    status, resp = _post(port, "/v1/store/replace",
                         {"store": "pattern-signatures", "id": "sig-908"},
                         json.dumps(body).encode("utf-8"), agent="bravo")
    assert status == 200, resp
    rec = _by_id(_world(project_root, "pattern-signatures.jsonl"), "sig-908")
    assert rec["name"] == "renamed by someone else"   # the replace did happen
    assert rec["encoded_by"] == "alpha"               # ...but authorship held


def test_replace_does_not_invent_authorship_on_an_unstamped_record(running_daemon):
    """The mirror constraint, and the reason the preserve uses a membership test
    rather than `.get()`. A historical record has no author; copying an absent
    key back as null would write `encoded_by: None` onto exactly the rows this
    change promises to leave alone — a backfill smuggled in through the replace
    path while the append path stayed clean."""
    project_root, port = running_daemon
    path = _world(project_root, "pattern-signatures.jsonl")
    seeded = [r for r in _records(path) if "encoded_by" not in r]
    assert seeded, "fixture seeded no unstamped row to protect"
    victim = seeded[0]

    # The seeded rows predate the current required-field set, and `replace`
    # validates the full body — so fill the gaps to get past validation. That
    # does not weaken the test: the condition under test is that the EXISTING
    # record carries no author, and it still does.
    body = dict(_patsig_rec(), **victim)
    body["description"] = "touched by a replace"
    assert "encoded_by" not in body
    status, resp = _post(port, "/v1/store/replace",
                         {"store": "pattern-signatures", "id": victim["id"]},
                         json.dumps(body).encode("utf-8"), agent="bravo")
    assert status == 200, resp
    rec = _by_id(path, victim["id"])
    assert rec["description"] == "touched by a replace"
    assert "encoded_by" not in rec


def test_append_does_not_backfill_historical_rows(running_daemon):
    """Pre-existing rows are unchanged, and in particular gain no `encoded_by`.

    The whole store is rewritten on every append (read -> append -> atomic
    write), so a stamp implemented as a normalization pass over `items` — or an
    `encoded_by: None` added to a *_DEFAULT_FIELDS map, which is the idiom this
    store family otherwise uses and which flows into the allowlist for free —
    would silently rewrite thousands of historical records while every positive
    test above still passed. That is why the field is allowlisted directly in
    GUARD_KNOWN_FIELDS instead.

    Compares parsed records PLUS key ORDER (dict `==` ignores order, so a
    reordering rewrite would otherwise slip through). It deliberately does NOT
    compare raw bytes: `_atomic_write_jsonl` re-serializes every item with a
    plain `json.dumps`, so a fixture seeded with compact separators comes back
    with `", "` / `": "` on the first append to that store — unconditionally,
    for every store, stamp or no stamp. Verified by reading the writer, not
    inferred from the diff. A raw-byte assertion here would be permanently red
    for a reason unrelated to this contract.
    """
    project_root, port = running_daemon
    for name, store, rec in (
            ("reasoning-bank.jsonl", "reasoning-bank", _rb_rec(id="rb-907")),
            ("guardrails.jsonl", "guardrails", _guard_rec(id="guard-907")),
            ("pattern-signatures.jsonl", "pattern-signatures", _patsig_rec(id="sig-907")),
    ):
        path = _world(project_root, name)
        before = _records(path)
        assert before, "%s: fixture seeded no historical rows to protect" % name
        status, body = _post(port, "/v1/store/append", {"store": store},
                             json.dumps(rec).encode("utf-8"), agent="zeta")
        assert status == 200, body
        after = _records(path)

        assert len(after) == len(before) + 1
        assert after[:len(before)] == before, "%s: append mutated a prior row" % name
        assert [list(r) for r in after[:len(before)]] == [list(r) for r in before], (
            "%s: append reordered keys on a prior row" % name)
        stamped = [r.get("id") for r in after[:len(before)] if "encoded_by" in r]
        assert not stamped, "%s: backfilled encoded_by onto %s" % (name, stamped)
