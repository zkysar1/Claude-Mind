"""Tests for locus-sweep.py ().

The sweep measures the LOCUS-bound share of the deferred-goal queue. Its whole
value rests on two properties that are easy to lose silently, so both are
pinned here rather than asserted in prose:

  READ-ONLY. The population is other agents' live work. A sweep that mutated a
  defer would re-route a partner's queue, which is the failure the script's own
  posture exists to avoid. Tested structurally (no write helper reachable) AND
  empirically (the real corpus is byte-identical after a real run).

  THE CONTROL IS NOT DECORATIVE. An empty band must mean "nothing matched",
  never "the reader is broken" (guard-2421). So the control is mutation-tested:
  break the classifier and the control MUST go red. A positive control nobody
  has ever seen fail is indistinguishable from one that cannot.

Pattern: importlib + sys.path, same as test_defer_drift_check.py — the script
name has hyphens and cannot be a plain `import`.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
SCRIPT = SCRIPTS / "locus-sweep.py"


def _import():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("locus_sweep", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["locus_sweep"] = mod
    spec.loader.exec_module(mod)
    return mod


LS = _import()


# ── read-only, structurally ─────────────────────────────────────────────────

# Names that would make a write reachable. Checked against the SOURCE because
# this is a claim about what the module *can* do, not about one execution path.
_WRITE_MARKERS = (
    "_fileops",
    "locked_append_jsonl",
    "locked_rmw",
    "update-goal",
    "update_goal",
    "aspirations-update-goal",
    "--apply",
    "os.remove",
    "shutil.",
    ".write_text(",
    ".unlink(",
)


def test_no_write_helper_is_reachable_from_the_module():
    src = SCRIPT.read_text(encoding="utf-8")
    # Strip the docstring + comments: the module DISCUSSES --apply and mutation
    # deliberately (explaining why it has neither), and a naive substring scan
    # would fire on the prose that documents the guarantee.
    code = "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )
    code = re.sub(r'""".*?"""', "", code, flags=re.S)
    hits = [m for m in _WRITE_MARKERS if m in code]
    assert not hits, f"locus-sweep gained a write path: {hits}"


def test_no_file_is_opened_for_writing():
    code = SCRIPT.read_text(encoding="utf-8")
    assert not re.search(r"open\([^)]*['\"][wax]", code), "opens a file for writing"


# ── read-only, empirically (the goal's check 2) ─────────────────────────────


def _corpus_paths():
    """Every aspirations.jsonl the sweep's population reader touches."""
    sys.path.insert(0, str(SCRIPTS))
    from _paths import WORLD_DIR, agents_root  # noqa: E402

    paths = [Path(WORLD_DIR) / "aspirations.jsonl"]
    paths += sorted(Path(agents_root()).glob("*/aspirations.jsonl"))
    return [p for p in paths if p.exists()]


def _digest(paths):
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def test_a_real_run_leaves_every_deferred_goal_byte_unchanged():
    """The goal's check 2, run against the live corpus rather than a fixture.

    A fixture would prove the sweep does not write to a tmp file it was handed.
    This proves it does not write to the store it actually reads — which is the
    thing a partner agent is trusting.
    """
    paths = _corpus_paths()
    if not paths:
        pytest.skip("no aspirations corpus on this box")

    before = _digest(paths)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--output", "json"],
        capture_output=True, text=True, cwd=str(SCRIPTS.parent.parent),
    )
    assert proc.returncode == 0, f"sweep failed rc={proc.returncode}: {proc.stderr[-800:]}"
    after = _digest(paths)

    changed = [p for p in before if before[p] != after.get(p)]
    assert not changed, f"sweep MUTATED the corpus: {changed}"


# ── the positive control, mutation-tested ───────────────────────────────────


def test_positive_control_passes_on_the_shipped_classifier():
    ok, bad = LS.run_control()
    assert ok, f"control regressed: {bad}"


def test_control_goes_red_when_the_classifier_is_broken(monkeypatch):
    """Mutation test — the control must be able to FAIL.

    Blinding the `declared` branch is the exact regression that would silently
    flatten the floor to zero and make the sweep report a confident, wrong,
    much-narrower bracket.
    """
    monkeypatch.setattr(LS, "DECLARED_RE", re.compile(r"(?!x)x"))
    ok, bad = LS.run_control()
    assert not ok, "control passed against a blinded classifier — it proves nothing"
    assert any(f["expected"] == "declared" for f in bad)


def test_every_control_fixture_is_reachable():
    """Each fixture must exercise a DISTINCT band, or the control has dead rows."""
    bands = {LS.classify(text) for text, _, _ in LS.CONTROL}
    assert bands == {"declared", "blocking", "provenance_only", "no_locus"}


# ── classification ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("text,want", [
    ("precondition_unmet:studio_session_required — probed today", "declared"),
    ("measure half requires Roblox Studio on Windows box (box-bounded)", "declared"),
    ("the dev bridge is host-bound to the Studio machine. MEASURED from cc-08", "declared"),
    ("measurement requires cc-04 local cache", "blocking"),
    ("RE-PROBED 2026-08-14T13:15 (zeta, cc-02, uname -r 6.8.0-137-generic)", "provenance_only"),
    ("3 of 17 stranded files remain undispositioned", "no_locus"),
    ("", "no_locus"),
    (None, "no_locus"),
])
def test_classify(text, want):
    assert LS.classify(text) == want


def test_declared_outranks_a_probe_citation_in_the_same_text():
    """The  regression: a self-declared block demoted by its own probe.

    This ordering is the difference between the floor being 15 and being 12, and
    it is exactly the kind of precedence that a later edit reorders by accident.
    """
    text = "the dev bridge is host-bound to the Studio machine. MEASURED 2026-08-16 from cc-08"
    assert LS.PROVENANCE_RE.search(text), "fixture must trip provenance too, or it tests nothing"
    assert LS.classify(text) == "declared"


# ── locus extraction ────────────────────────────────────────────────────────


def test_exclusion_spans_are_subtracted_from_required():
    """Role 3. Naive extraction reports this actionable on the one box its
    author said it cannot run on."""
    loci = LS.extract_loci(
        "measurement requires cc-04 local cache "
        "(legacy .history dirs not present on cc-07/cc-09)")
    assert "box:cc-04" in loci["required"]
    assert set(loci["excluded"]) == {"box:cc-07", "box:cc-09"}
    assert "box:cc-07" not in loci["required"]


def test_a_box_in_its_own_exclusion_set_is_never_a_candidate():
    loci = LS.extract_loci("requires cc-04 cache (not present on cc-07/cc-09)")
    for host in ("cc-07", "cc-09"):
        verdict, why = LS.evaluate_here(
            LS.box_profile(hostname=host, platform="linux"), loci)
        assert verdict == "elsewhere", f"{host} claimed a row that excludes it: {why}"


@pytest.mark.parametrize("text,tok", [
    ("not available on windows box", "os:windows"),
    ("cannot be satisfied from a roblox-studio host", "cap:roblox-studio"),
])
def test_os_and_capability_loci_are_excludable_too(text, tok):
    """Role 3 must cover EVERY token kind, not just hostnames.

    Handling exclusion for `box:` alone left os:/cap: inverted — "not available
    on windows box" put os:windows in REQUIRED with an empty exclusion set, so a
    Windows box read it as a candidate. That is the exact inversion the
    exclusion branch exists to prevent, reintroduced one token-type over.
    """
    loci = LS.extract_loci(text)
    assert tok in loci["excluded"]
    assert tok not in loci["required"]


def test_an_excluded_os_locus_is_not_a_candidate_on_that_os():
    verdict, why = LS.evaluate_here(
        LS.box_profile(hostname="DESKTOP-X", platform="win32"),
        LS.extract_loci("not available on windows box"))
    assert verdict != "candidate", f"inverted: claimed the box its author ruled out ({why})"


def test_capability_and_os_loci_are_extracted():
    loci = LS.extract_loci("needs a live Roblox Studio session on a windows box")
    assert loci["required"] == ["cap:roblox-studio", "os:windows"]


# ── per-box satisfaction ────────────────────────────────────────────────────


def test_studio_is_no_on_linux_and_unknown_on_windows():
    """The honest `unknown`. Linux decides it; Windows cannot, and guessing
    either way is worse than saying so."""
    linux = LS.box_profile(hostname="cc-09", platform="linux")
    win = LS.box_profile(hostname="DESKTOP-O91DLK2", platform="win32")
    assert LS.satisfies(linux, "cap:roblox-studio")[0] == "no"
    assert LS.satisfies(win, "cap:roblox-studio")[0] == "unknown"


def test_an_fqdn_hostname_still_matches_a_short_locus():
    """Guards a CONFIDENT ZERO. Defers name boxes short ("cc-04"), so an FQDN
    `node()` would miss every comparison and report zero candidates — which
    reads as 'nothing frozen for me' and is the one answer nobody re-checks."""
    prof = LS.box_profile(hostname="cc-04.lxd", platform="linux")
    assert prof["hostname"] == "cc-04"
    verdict, _ = LS.evaluate_here(prof, LS.extract_loci("requires cc-04 local cache"))
    assert verdict == "candidate"


def test_unknown_never_upgrades_to_candidate():
    verdict, _ = LS.evaluate_here(
        LS.box_profile(hostname="DESKTOP-O91DLK2", platform="win32"),
        LS.extract_loci("needs a live Roblox Studio session"))
    assert verdict == "undeterminable"


def test_the_verdict_depends_on_the_box(monkeypatch):
    """The goal's check 1, made runnable from ONE box.

    'Run on two unlike boxes and confirm the sets DIFFER' is really testing
    whether the output depends on the box at all. A deterministic override
    falsifies that in one call instead of one round trip — and unlike a two-box
    run, it keeps failing if someone later hardcodes the answer.
    """
    fixture = [
        {"goal_id": "g-1", "agent": "world", "title": "t",
         "defer_reason": "precondition_unmet: requires cc-04 local cache"},
        {"goal_id": "g-2", "agent": "world", "title": "t",
         "defer_reason": "precondition_unmet: requires a windows box"},
    ]
    monkeypatch.setattr(LS, "_load_population", lambda: fixture)

    linux = LS.sweep(LS.box_profile(hostname="cc-04", platform="linux"))
    win = LS.sweep(LS.box_profile(hostname="DESKTOP-O91DLK2", platform="win32"))

    ids = lambda r: {c["goal_id"] for c in r["this_box"]["candidates"]}
    assert ids(linux) == {"g-1"}
    assert ids(win) == {"g-2"}
    assert ids(linux) != ids(win), "identical sets on unlike boxes — not reading locus"


# ── counts ──────────────────────────────────────────────────────────────────


def test_suppressed_counts_structured_heads_and_ignores_empties(monkeypatch):
    """`is_narrative_defer` answers a WRITE-time question and returns False for
    a CLEAR. Read as 'is it suppressed?' that False inverts, so an empty defer
    would inflate the count. Latently dead today (0 of 166) and pinned anyway —
    the population is live."""
    monkeypatch.setattr(LS, "_load_population", lambda: [
        {"goal_id": "a", "defer_reason": "precondition_unmet: x", "title": "", "agent": "w"},
        {"goal_id": "b", "defer_reason": "human_blocked: y", "title": "", "agent": "w"},
        {"goal_id": "c", "defer_reason": "waiting on the user to click approve",
         "title": "", "agent": "w"},
        {"goal_id": "d", "defer_reason": "   ", "title": "", "agent": "w"},
    ])
    res = LS.sweep(LS.box_profile(hostname="cc-04", platform="linux"))
    assert res["suppressed_from_every_selector"] == 2


def test_bands_partition_the_population(monkeypatch):
    """Every row lands in exactly one band — else the bracket is arithmetic on
    a set that double-counts."""
    monkeypatch.setattr(LS, "_load_population", lambda: [
        {"goal_id": f"g-{i}", "agent": "world", "title": "t", "defer_reason": t}
        for i, t in enumerate([
            "studio_session_required", "requires cc-04 cache",
            "probed on cc-02", "the box is odd", "nothing here"])
    ])
    res = LS.sweep(LS.box_profile(hostname="cc-04", platform="linux"))
    assert sum(res["counts"].values()) == res["population"] == 5


def test_under_matched_is_the_gap_between_floor_and_ceiling(monkeypatch):
    monkeypatch.setattr(LS, "_load_population", lambda: [
        {"goal_id": f"g-{i}", "agent": "world", "title": "t", "defer_reason": t}
        for i, t in enumerate(["studio_session_required", "requires cc-04 cache", "none"])
    ])
    res = LS.sweep(LS.box_profile(hostname="cc-04", platform="linux"))
    assert res["bracket"] == {"floor": 1, "ceiling": 2}
    assert res["under_matched"]["count"] == 1


# ── CLI ─────────────────────────────────────────────────────────────────────


def test_control_failure_exits_2_not_0(monkeypatch, capsys):
    """A broken reader must not exit 0 with an empty-looking census — that is
    precisely the confident zero the control exists to prevent."""
    monkeypatch.setattr(LS, "DECLARED_RE", re.compile(r"(?!x)x"))
    assert LS.main(["--output", "json"]) == 2


def test_default_json_omits_the_bulk_payload(monkeypatch, capsys):
    """The lane calling this runs EVERY precheck iteration and reads four
    summary fields. `bands` is 98% of the payload (199 KB of 202 KB), so
    shipping it by default would push ~200 KB into loop context per iteration
    to deliver ~2.7 KB of answer."""
    monkeypatch.setattr(LS, "_load_population", lambda: [
        {"goal_id": "g-1", "agent": "world", "title": "t",
         "defer_reason": "precondition_unmet: requires cc-04 cache"}])
    assert LS.main(["--output", "json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert "bands" not in out
    assert "bands_omitted" in out
    # The four fields the SKILL.md lane actually parses must survive the trim.
    for k in ("population", "bracket", "this_box", "under_matched"):
        assert k in out, f"summary lost {k} — the lane parses it"


def test_full_flag_restores_the_bulk_payload(monkeypatch, capsys):
    monkeypatch.setattr(LS, "_load_population", lambda: [
        {"goal_id": "g-1", "agent": "world", "title": "t",
         "defer_reason": "precondition_unmet: requires cc-04 cache"}])
    assert LS.main(["--output", "json", "--full"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert "bands" in out and "bands_omitted" not in out


def test_ceiling_composition_splits_specific_from_generic(monkeypatch):
    """A ceiling is only readable if you know what it is made of: '55%' invites
    the reading that half the queue names a real machine."""
    monkeypatch.setattr(LS, "_load_population", lambda: [
        {"goal_id": "g-1", "agent": "world", "title": "t",
         "defer_reason": "requires cc-04 cache"},
        {"goal_id": "g-2", "agent": "world", "title": "t",
         "defer_reason": "S1 claim falsified on this box but no code shipped"},
        {"goal_id": "g-3", "agent": "world", "title": "t",
         "defer_reason": "nothing locus-ish here"},
    ])
    res = LS.sweep(LS.box_profile(hostname="cc-09", platform="linux"))
    comp = res["ceiling_composition"]
    assert comp["names_a_specific_locus"] == 1
    assert comp["generic_word_only"] == 1
    assert comp["names_a_specific_locus"] + comp["generic_word_only"] == res["bracket"]["ceiling"]


def test_skip_control_is_opt_in_only():
    src = SCRIPT.read_text(encoding="utf-8")
    assert "--skip-control" in src
    assert 'default="json"' in src, "output default changed; report consumers assume json"


def test_a_double_negative_span_never_excludes(monkeypatch):
    """Corpus-verbatim (): "cannot be satisfied from a non-Studio box"
    is a REQUIREMENT for Studio. Excluding cap:roblox-studio there would mark
    the one capable box 'elsewhere' — the inversion, re-inverted. Harmless only
    because _STUDIO_RE does not currently match "non-Studio box", so this test
    also guards anyone who later widens that pattern."""
    text = ("precondition_unmet: outcome 2 requires a LIVE Roblox Studio dev PLAY "
            "session and cannot be satisfied from a non-Studio box")
    assert LS._EXCLUSION_RE.findall(text), "fixture must produce a span, or it tests nothing"
    loci = LS.extract_loci(text)
    assert "cap:roblox-studio" in loci["required"]
    assert "cap:roblox-studio" not in loci["excluded"]

    # And it must still hold if _STUDIO_RE is widened to match "Studio box".
    monkeypatch.setattr(LS, "_STUDIO_RE", re.compile(r"roblox[- ]?studio|studio\s+box", re.I))
    loci = LS.extract_loci(text)
    assert "cap:roblox-studio" not in loci["excluded"], "double negative inverted the answer"


def test_a_trailing_negated_clause_still_excludes_both_boxes():
    """Guards the narrowing of _NEGATED_SPAN_RE.

    "not present on cc-07 and not on cc-09" captures the span
    "cc-07 and not on cc-09" — which contains the word "not". A negation guard
    matching bare `\\bnot\\b` would skip it and silently lose a correct two-box
    exclusion, so the guard matches only the hyphenated negated noun.
    """
    loci = LS.extract_loci("requires cc-04 (not present on cc-07 and not on cc-09)")
    assert set(loci["excluded"]) == {"box:cc-07", "box:cc-09"}
    assert loci["required"] == ["box:cc-04"]
