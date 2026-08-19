"""_render_manifest round-trip tests (, asp-306).

Two defects, one fix site (`core/scripts/body-manifest.py::_render_manifest`),
both found by /fresh-eyes-code on g-306-119-a's own output:

  A. LOSSY. The renderer iterated `_FIELD_ORDER` and emitted `data.get(k)`, so
     it behaved as an ALLOWLIST: `set_state` loads the manifest and re-renders
     through it, silently dropping any key not in that tuple. Same class as
     guard-1900 — clean parse, zero errors, field simply not there.
  B. UNQUOTED. The fallback branch rendered `{k}: {v}` bare. `machine_id`
     resolves from an operator-set, unvalidated `MACHINE_ID`, so a value
     carrying a YAML metacharacter made the manifest unparseable — breaking
     read_manifest -> set_state -> close_body_on_genuine permanently for that
     Body, at CLOSE time, far from the write that caused it. guard-610.

MUTATION GUARDS — the two tests that make the rest mean something rather than
merely pass. Delete the `unknown` tail from `_render_manifest` and
`test_no_key_is_dropped_by_a_round_trip` fails while every field-specific test
still passes. Delete the `isinstance(v, str)` branch and
`test_hostile_machine_id_keeps_the_manifest_parseable` fails at parse, not at
assert. Both are written against the CLASS (arbitrary key, arbitrary
metacharacter), not against the two fields that happened to expose the bugs —
per guard-1900, "compare the YAML key set against the loaded key set, not just
the two keys you added".

Daemon-safe (pure path + file arithmetic; no daemon, no network).

Run:
  STORAGE_BACKEND=local python -m pytest \
      core/scripts/tests/test_body_manifest_render.py -q
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
from pathlib import Path

import pytest
import yaml

CORE_SCRIPTS = Path(__file__).resolve().parent.parent  # core/scripts/


def _load_body_manifest():
    """Load the hyphen-named module via importlib (not importable by name)."""
    spec = importlib.util.spec_from_file_location(
        "body_manifest", CORE_SCRIPTS / "body-manifest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bm = _load_body_manifest()

SID = "55555555-5555-4555-8555-555555555555"
SID_OTHER = "66666666-6666-4666-8666-666666666666"
WM_TEXT = "goals_completed: 3\n"


def _mk_box(tmp_path: Path, name: str = "alpha",
            running_sid: str | None = None) -> Path:
    adir = tmp_path / "agents" / name
    state = adir / "session"
    state.mkdir(parents=True, exist_ok=True)
    if running_sid is not None:
        (state / "running-session-id").write_text(running_sid, encoding="utf-8")
    (state / "working-memory.yaml").write_bytes(WM_TEXT.encode("utf-8"))
    return tmp_path


def _manifest_path(root: Path, sid: str = SID, name: str = "alpha") -> Path:
    return root / "agents" / name / "sessions" / sid / "body-manifest.yaml"


def _read_yaml(p: Path) -> dict:
    return yaml.safe_load(p.read_text(encoding="utf-8"))


# ── Defect A: lossy re-render ───────────────────────────────────────────────

def test_no_key_is_dropped_by_a_round_trip(tmp_path):
    """MUTATION GUARD + the guard-1900 generalized form.

    Asserts on the whole key SET, so it fails for ANY key the renderer forgets
    — including a field a future writer adds and never registers in
    _FIELD_ORDER. A test naming only `remote_body` and `machine_id` would pass
    against a renderer that still drops everything else.
    """
    root = _mk_box(tmp_path)
    path = bm.write_manifest(SID, "alpha", "local", "reducer", project_root=root)

    # A newer writer appended a field this version of _FIELD_ORDER knows nothing
    # about. Two of them, of different types, so the tail is exercised for more
    # than one branch.
    with path.open("a", encoding="utf-8") as fh:
        fh.write("future_field: 'from-a-newer-writer'\nfuture_count: 42\n")

    before = set(_read_yaml(path))
    bm.set_state(SID, "alpha", "closed-pending-merge", project_root=root)
    after = set(_read_yaml(path))

    assert after == before, (
        "set_state dropped {} — _FIELD_ORDER is acting as an allowlist "
        "again (guard-1900)".format(sorted(before - after)))


def test_unknown_values_survive_a_round_trip_with_their_types(tmp_path):
    """Presence is not enough: a str must come back a str and an int an int.
    Quoting everything indiscriminately would pass the set-equality test above
    while silently turning 42 into '42'.
    """
    root = _mk_box(tmp_path)
    path = bm.write_manifest(SID, "alpha", "local", "reducer", project_root=root)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("future_field: 'from-a-newer-writer'\nfuture_count: 42\n"
                 "future_flag: true\n")

    bm.set_state(SID, "alpha", "merged", project_root=root)
    data = _read_yaml(path)
    assert data["future_field"] == "from-a-newer-writer"
    assert data["future_count"] == 42
    assert data["future_flag"] is True


def test_a_key_added_to_field_order_later_does_not_null_an_older_manifest(tmp_path):
    """The second half of defect A. A manifest written BEFORE a field existed
    must not have that field materialise as an explicit `null` that overwrites a
    value a newer writer had already put there.
    """
    root = _mk_box(tmp_path)
    path = bm.write_manifest(SID, "alpha", "local", "reducer", project_root=root)
    # Simulate the pre-change manifest: strip the two -a lines.
    kept = [ln for ln in path.read_text(encoding="utf-8").splitlines()
            if not ln.startswith(("remote_body:", "machine_id:"))]
    kept.append("remote_body: true")          # written by the newer writer
    kept.append("machine_id: 'box-b'")
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")

    bm.set_state(SID, "alpha", "closed-pending-merge", project_root=root)
    data = _read_yaml(path)
    assert data["remote_body"] is True, "an existing value was re-rendered as null"
    assert data["machine_id"] == "box-b"


def test_the_unknown_tail_is_sorted_so_renders_stay_deterministic(tmp_path):
    """Field order is the reason this renderer is hand-rolled at all; the tail
    must not inherit dict insertion order or diffs stop being stable."""
    root = _mk_box(tmp_path)
    bm.write_manifest(SID, "alpha", "local", "reducer", project_root=root)
    data = bm.read_manifest(SID, "alpha", project_root=root)
    data.update({"zeta_key": "z", "alpha_key": "a", "mid_key": "m"})
    rendered = bm._render_manifest(data)
    tail = [ln.split(":", 1)[0] for ln in rendered.splitlines()
            if ln.split(":", 1)[0] not in bm._FIELD_ORDER]
    assert tail == ["alpha_key", "mid_key", "zeta_key"]


# ── Defect B: unquoted render ───────────────────────────────────────────────

@pytest.mark.parametrize("hostile", [
    "box: 1",      # ScannerError — a bare colon opens a nested mapping
    "*star",       # ComposerError — an alias that resolves to nothing
    "#prod",       # silent value loss — the rest of the line is a comment
    "&anchor",
    "!tag",
    "  leading",
    "yes",         # YAML 1.1 would coerce a bare `yes` to a bool
    "null",        # ...and a bare `null` to None
    "it's-box-b",  # exercises the '' escape
])
def test_hostile_machine_id_keeps_the_manifest_parseable(tmp_path, hostile,
                                                         monkeypatch):
    """MUTATION GUARD for the quoting branch. MACHINE_ID is operator-set and
    unvalidated, so every one of these is reachable in production. Each failure
    lands at CLOSE time (read_manifest -> set_state -> close_body_on_genuine),
    permanently for that Body.
    """
    monkeypatch.setattr(bm, "_resolve_machine_id", lambda: hostile)
    root = _mk_box(tmp_path)
    path = bm.write_manifest(SID, "alpha", "local", "reducer", project_root=root)

    data = _read_yaml(path)  # would raise ScannerError/ComposerError unquoted
    assert data["machine_id"] == hostile, (
        "machine_id did not survive the render verbatim — a value that parses "
        "is not the same as a value that round-trips")

    # ...and it must still round-trip through the load-modify-save path, which
    # is where the production failure actually surfaced.
    bm.set_state(SID, "alpha", "closed-pending-merge", project_root=root)
    assert _read_yaml(path)["machine_id"] == hostile


def test_every_string_field_is_quoted_not_just_machine_id(tmp_path):
    """env_id, mindKey and unitKey ride the same branch. Fixing the field
    instead of the class would leave three live instances of the same bug."""
    root = _mk_box(tmp_path)
    path = bm.write_manifest(SID, "alpha", "weird: env", "reducer",
                             project_root=root)
    assert _read_yaml(path)["env_id"] == "weird: env"


def test_apostrophes_are_escaped_by_doubling(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "_resolve_machine_id", lambda: "o'brien's box")
    root = _mk_box(tmp_path)
    path = bm.write_manifest(SID, "alpha", "local", "reducer", project_root=root)
    assert "machine_id: 'o''brien''s box'" in path.read_text(encoding="utf-8")
    assert _read_yaml(path)["machine_id"] == "o'brien's box"


# ── No-regression: the parts that were verified working ─────────────────────

def test_bool_still_renders_lowercase_for_non_pyyaml_parsers(tmp_path):
    """Asserted on RAW TEXT because yaml.safe_load accepts either form and so
    cannot distinguish them. the framework-ES reads this manifest too, and YAML 1.2
    parsers reject bare `True`. Mirrors the pin in
    test_body_manifest_remote_worker.py — the quoting change must not push
    booleans into the string branch."""
    root = _mk_box(tmp_path)
    path = bm.write_manifest(SID, "alpha", "local", "worker", project_root=root,
                             reducer_sid=bm.REMOTE_REDUCER_SENTINEL)
    raw = path.read_text(encoding="utf-8")
    assert "remote_body: true" in raw
    assert "remote_body: True" not in raw
    assert "remote_body: 'true'" not in raw, "a bool was quoted into a string"


def test_fresh_cross_box_round_trip_is_unchanged(tmp_path):
    """The goal states this lane was VERIFIED NOT BROKEN; pin it so the fix
    cannot regress what it was not meant to touch."""
    root = _mk_box(tmp_path)
    bm.write_manifest(SID, "alpha", "local", "worker", project_root=root,
                      reducer_sid=bm.REMOTE_REDUCER_SENTINEL)
    bm.set_state(SID, "alpha", "closed-pending-merge", project_root=root)
    data = _read_yaml(_manifest_path(root))
    assert data["remote_body"] is True
    assert data["reducer_sid"] == "remote"
    assert data["body_state"] == "closed-pending-merge"
    assert data["forked_wm_hash"] is not None


def test_null_still_renders_as_bare_null(tmp_path):
    root = _mk_box(tmp_path, running_sid=SID)
    path = bm.write_manifest(SID, "alpha", "local", "reducer", project_root=root)
    raw = path.read_text(encoding="utf-8")
    assert "reducer_sid: null" in raw
    assert "reducer_sid: 'null'" not in raw, (
        "None was routed through the string branch — a null became the literal "
        "string 'null', which every consumer would read as a real value")


def test_started_at_render_is_unchanged_by_removing_its_special_case(tmp_path):
    """The general string branch subsumed the former `elif k in ('started_at',)`
    line. Its output must be byte-identical, or the subtraction was not safe."""
    root = _mk_box(tmp_path)
    path = bm.write_manifest(SID, "alpha", "local", "reducer", project_root=root)
    data = _read_yaml(path)
    assert "started_at: '{}'".format(data["started_at"]) in \
        path.read_text(encoding="utf-8")


# ── Wiring: the raw-text shell consumer ─────────────────────────────────────

def test_the_grep_consumer_in_cleanup_stale_bindings_still_reads_both_fields(tmp_path):
    """guard-1943 — a green suite certifies the FUNCTION, never the WIRING.

    cleanup-stale-bindings.sh `_preserve_unmerged_body_wm` does NOT parse YAML:
    it greps `body_state:` / `forked_wm_hash:` and strips with bash parameter
    expansion (`${_STATE#*:}`, then whitespace, then `"`, then `'`). Quoting the
    string class changes those two lines' bytes, so replicate the consumer's
    exact strip chain here. It already tolerated quotes — this test pins that it
    still must, so a future 'simplify the renderer' change cannot break a
    consumer that lives in a different language and is invisible to pytest.
    """
    root = _mk_box(tmp_path, running_sid=SID_OTHER)
    path = bm.write_manifest(SID, "alpha", "local", "worker", project_root=root)

    def _bash_strip(line: str) -> str:
        v = line.split(":", 1)[1]                       # ${_STATE#*:}
        v = "".join(v.split())                          # ${_STATE//[[:space:]]/}
        return v.replace('"', "").replace("'", "")      # strip both quote forms

    lines = path.read_text(encoding="utf-8").splitlines()
    state = _bash_strip(next(l for l in lines if l.startswith("body_state:")))
    fhash = _bash_strip(next(l for l in lines if l.startswith("forked_wm_hash:")))

    assert state == "active"
    assert fhash and fhash != "null" and len(fhash) == 64, (
        "the consumer stages this as <unitKey>-wm.hash; a mangled value makes "
        "body-merge._consume_staged treat a diverged orphan as never-diverged")


# ── The park valve, producer -> consumer () ────────────────────────
#
# test_stop_hook_in_flight_integration.py CONSTRUCTS the manifest state the park
# predicate reads and then drives the hook, so it covers the HANDLER and the side
# effect (ALLOW gate=worker-net-body-parked) while leaving the TRIGGER untested:
# nothing asserts that `park_body` emits bytes the stop-hook predicate actually
# matches. "The branch is covered" and "the wiring is correct" are different
# claims, and an integration test that builds its own fixture necessarily
# bypasses the producer, so the suite cannot close this gap by construction.
#
# It matters more since  collapsed two park valves into one: there is no
# second valve to catch a miss. If this valve stops matching, a parked worker
# Body falls through to the BLOCK branch, whose payload orders it to write the
# body-closing sentinel — durably CLOSING the Body on the turn it parked, which
# is the exact failure  exists to prevent.

STOP_HOOK = CORE_SCRIPTS / "stop-hook.sh"


def _stop_hook_park_predicate() -> str:
    """Return the LIVE park predicate, read from stop-hook.sh's own bytes.

    guard-920 asks for the production shape verbatim, and the obvious reading is
    to hand-copy the literal into this file. That satisfies the rule at write
    time and then becomes a SECOND source of truth which drifts silently — the
    very class this test exists to close, reintroduced one layer up. Reading the
    real consumer's own line cannot drift from the real consumer.

    The `assert` is not defensive tidiness and must not be softened to a return
    of None or "": `grep -Eq ""` matches EVERY line, so an extraction that
    quietly failed would make every assertion below pass against a manifest
    containing anything at all — a vacuous green in exactly the direction that
    hides the defect (guard-2298 / guard-1641). A refactor of stop-hook.sh that
    moves this predicate must RED here, loudly, rather than disarm the pin.
    """
    src = STOP_HOOK.read_text(encoding="utf-8")
    found = re.findall(r'grep -Eq "([^"]*body_state[^"]*parked[^"]*)"', src)
    assert len(found) == 1, (
        f"expected exactly ONE park predicate in {STOP_HOOK}, found "
        f"{len(found)}: {found!r}.\n"
        "  0 — the grep for a parked body_state was moved, renamed or "
        "reformatted. This pin must not silently pass: an empty pattern "
        "matches every line, so a failed extraction would certify any manifest "
        "at all (guard-2298 / guard-1641).\n"
        "  2+ — a SECOND park valve exists. g-306-303 collapsed two into one "
        "precisely because both emitted the same gate name from different "
        "predicates, so the log could not say which one fired; asserting "
        "uniqueness here is what makes 'the park valve' a well-defined "
        "referent rather than whichever one this regex happened to reach "
        "first.")
    return found[0]


def _grep_matches(pattern: str, path: Path) -> bool:
    """Run the REAL grep the hook runs, not a Python re translation.

    The predicate is POSIX ERE and contains `[[:space:]]`, which Python's `re`
    does not implement — a hand-translated pattern would be testing a different
    language than the one production evaluates. argv form, so no shell quoting
    sits between this and the bytes on disk.

    rc 2 IS NOT rc 1, and collapsing them is the whole reason this is a function
    rather than an inline call. grep exits 0 on match, 1 on no-match, and **2 on
    ERROR** — unreadable file, malformed ERE (all three measured). A bare
    `returncode == 0` maps every one of those to False, which is the CORRECT
    answer for a no-match and a VACUOUS PASS for an error: every assertion of the
    form `assert not _grep_matches(...)` — the negative control plus all four
    drift cases below — would then pass while measuring nothing at all. That is
    the same vacuity class `_stop_hook_park_predicate` asserts against one
    function up, and it reappeared here in a different disguise: there the empty
    pattern matched everything, here the failed probe matches nothing.
    """
    r = subprocess.run(
        ["grep", "-Eq", pattern, str(path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    assert r.returncode in (0, 1), (
        f"grep exited {r.returncode} (not a match/no-match answer) for pattern "
        f"{pattern!r} against {path}: {(r.stderr or '').strip() or '<no stderr>'}\n"
        "Treating this as 'no match' would silently satisfy every negative "
        "assertion in this section.")
    return r.returncode == 0


def _parked_worker(tmp_path: Path):
    """A real worker Body, parked through the real writer. Returns its manifest."""
    root = _mk_box(tmp_path, running_sid=SID_OTHER)
    path = bm.write_manifest(SID, "alpha", "local", "worker", project_root=root)
    assert bm.park_body(SID, "alpha", project_root=root) == "parked", (
        "park_body refused to park a freshly-written worker Body — this "
        "fixture no longer reaches the code path under test")
    return path


def test_the_park_predicate_is_extractable_from_the_live_stop_hook():
    """Positive control for the extraction itself (guard-2421).

    Every assertion below is only as good as this one: without it, a broken
    extraction reads as a passing valve.
    """
    pattern = _stop_hook_park_predicate()
    assert "body_state" in pattern and "parked" in pattern
    assert pattern.startswith("^") and pattern.endswith("$"), (
        f"predicate {pattern!r} is no longer line-anchored — an unanchored "
        "match would fire on any line mentioning a parked state")


def test_the_park_writer_output_matches_the_stop_hook_predicate(tmp_path):
    """END-TO-END across the producer boundary: real writer -> real grep.

    This is the whole goal. body-manifest.py sets body_state = "parked" and
    _render_manifest decides how that renders; stop-hook.sh decides what it will
    accept. The two agree today, and nothing pinned them together, so a quoting
    or format change on either side broke the valve with a fully green suite.
    """
    path = _parked_worker(tmp_path)
    pattern = _stop_hook_park_predicate()

    assert _grep_matches(pattern, path), (
        "the stop-hook park predicate does NOT match what park_body rendered.\n"
        f"  predicate: {pattern}\n"
        f"  rendered:  "
        + next((l for l in path.read_text(encoding='utf-8').splitlines()
                if l.startswith('body_state:')), '<no body_state line>')
        + "\nA parked worker Body will fall through to the BLOCK branch and be "
          "told to write the body-closing sentinel — closing it on the turn it "
          "parked (g-306-291).")


def test_an_active_manifest_does_not_match_the_park_predicate(tmp_path):
    """NEGATIVE CONTROL — this is what gives the test above teeth.

    A predicate that matched everything would pass the E2E assertion while
    telling us nothing. The valve has to DISCRIMINATE, or the hook would park
    every Body it saw.
    """
    root = _mk_box(tmp_path, running_sid=SID_OTHER)
    path = bm.write_manifest(SID, "alpha", "local", "worker", project_root=root)

    assert not _grep_matches(_stop_hook_park_predicate(), path), (
        "the park predicate matched an ACTIVE manifest — the valve does not "
        "discriminate, so every Body would take the park branch")


@pytest.mark.parametrize("mutated,why", [
    ('body_state: "parked"', "renderer switched to double quotes"),
    ("body_state: parked   # resumable", "renderer began appending a comment"),
    ("body_state: Parked", "renderer changed the value's case"),
    ("  body_state: 'parked'", "renderer began indenting top-level keys"),
])
def test_a_drifted_rendering_would_miss_the_park_valve(tmp_path, mutated, why):
    """MUTATION CONTROL, permanent rather than hand-run.

    The goal's verification asks that this test fail when the predicate or the
    renderer is mutated. Mutating the renderer by hand proves it once; asserting
    the predicate REJECTS each drifted form proves it on every future run, and
    names the specific renderer changes that would silently break the valve.

    Each form below still parses as YAML and still reads back as a parked Body,
    so nothing else in this file or in body-merge would notice — which is
    precisely why the pin has to live at the byte level.
    """
    path = _parked_worker(tmp_path)
    text = path.read_text(encoding="utf-8")
    drifted = "\n".join(
        mutated if ln.startswith(("body_state:", "  body_state:")) else ln
        for ln in text.splitlines()) + "\n"
    path.write_text(drifted, encoding="utf-8")

    assert not _grep_matches(_stop_hook_park_predicate(), path), (
        f"the park predicate tolerates {mutated!r} ({why}). If that is a "
        "deliberate widening, say so here; otherwise this pin has lost its "
        "power to detect the drift it exists to detect.")
