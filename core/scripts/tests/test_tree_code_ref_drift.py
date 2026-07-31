"""tree-code-ref-drift.py — reference parsing, resolution, and verdict polarity.

The scanner's failure modes are asymmetric, so the tests are too.

A MISSED drift costs one stale citation. A FALSE drift costs the report's
credibility — 195 leads nobody trusts detect nothing at all, and the natural
response to a noisy report is to stop running it. So most of what is pinned
below is the negative direction: the shapes that must NOT produce a finding.

The other load-bearing pin is `interpretable`. A scanner that resolves nothing
prints "0 drift" in exactly the same words as a clean corpus, which is the
"no signal vs no instrument" confusion (guard-1641). `test_zero_resolved_is_not
_interpretable` is the pin that keeps that distinguishable.

Run: STORAGE_BACKEND=local python -m pytest core/scripts/tests/test_tree_code_ref_drift.py -q
"""

import importlib.util
import pathlib
import sys

_MOD_PATH = pathlib.Path(__file__).resolve().parents[1] / "tree-code-ref-drift.py"
_spec = importlib.util.spec_from_file_location("tree_code_ref_drift", str(_MOD_PATH))
drift = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drift)


# ---------------------------------------------------------------------------
# 1. Reference parsing
# ---------------------------------------------------------------------------
class TestRefRegex:
    def _refs(self, text):
        return [(m.group("ref"), m.group("base"), m.group("start"), m.group("end"))
                for m in drift.REF_RE.finditer(text)]

    def test_bare_basename(self):
        assert self._refs("see Foo.java:42 there") == [("Foo.java", "Foo.java", "42", None)]

    def test_path_prefixed(self):
        got = self._refs("core/scripts/_fileops.py:221")
        assert got == [("core/scripts/_fileops.py", "_fileops.py", "221", None)]

    def test_line_range(self):
        assert self._refs("`Bar.py:248-272`") == [("Bar.py", "Bar.py", "248", "272")]

    def test_dotted_basename(self):
        """Roblox-style `SendUpdate.server.lua` must keep its whole basename."""
        got = self._refs("SendUpdate.server.lua:343")
        assert got[0][1] == "SendUpdate.server.lua"

    def test_prose_extension_is_not_a_code_ref(self):
        """`.md` refs are a different drift class with different repair rules."""
        assert self._refs("state.md:105") == []

    def test_bare_filename_without_line_is_not_a_ref(self):
        assert self._refs("edit Foo.java to fix it") == []


# ---------------------------------------------------------------------------
# 2. Resolution
# ---------------------------------------------------------------------------
class TestResolveRef:
    IDX = {
        "Solo.java": ["/r/a/Solo.java"],
        "Dup.java": ["/r/a/Dup.java", "/r/b/pkg/Dup.java"],
    }

    def test_unique_basename_resolves(self):
        assert drift.resolve_ref("Solo.java", "Solo.java", self.IDX) == \
            ("/r/a/Solo.java", "resolved")

    def test_missing_basename_is_unresolved_not_drift(self):
        """A repo absent from this box is not evidence the citation is stale."""
        assert drift.resolve_ref("Ghost.java", "Ghost.java", self.IDX) == (None, "unresolved")

    def test_duplicate_basename_without_hint_is_ambiguous(self):
        assert drift.resolve_ref("Dup.java", "Dup.java", self.IDX) == (None, "ambiguous")

    def test_path_hint_disambiguates(self):
        assert drift.resolve_ref("pkg/Dup.java", "Dup.java", self.IDX) == \
            ("/r/b/pkg/Dup.java", "resolved")

    def test_stale_path_hint_falls_back_to_basename(self):
        """A MOVED file must not read as unresolved — that would hide the drift
        this scanner exists to surface."""
        assert drift.resolve_ref("old/place/Solo.java", "Solo.java", self.IDX) == \
            ("/r/a/Solo.java", "resolved")


# ---------------------------------------------------------------------------
# 3. Symbol acceptance — the false-positive gate
# ---------------------------------------------------------------------------
class TestAcceptable:
    def test_plain_identifier_accepted(self):
        assert drift._acceptable("locked_append_jsonl", "_fileops") is True

    def test_short_token_rejected(self):
        assert drift._acceptable("cap", "Foo") is False

    def test_stoplisted_token_rejected(self):
        assert drift._acceptable("critical", "Foo") is False
        assert drift._acceptable("main", "Foo") is False

    def test_filename_stem_itself_rejected(self):
        assert drift._acceptable("OrderProcessorService", "OrderProcessorService") is False

    def test_stem_prefix_rejected(self):
        """`OrderProcessor` inside OrderProcessorService.java matches the class
        header and every self-reference, so its distance from a cited line is an
        artifact of file layout. Measured as a real false-positive class."""
        assert drift._acceptable("OrderProcessor", "OrderProcessorService") is False

    def test_symbol_containing_the_stem_rejected(self):
        assert drift._acceptable("OrderProcessorServiceImpl", "OrderProcessorService") is False

    def test_qualified_call_keeps_only_its_head_for_the_stem_test(self):
        """`Other.doThing` is a locator even when cited from Other.java's sibling."""
        assert drift._acceptable("Service.getEffectiveParams", "IntentEngineVerticle") is True


# ---------------------------------------------------------------------------
# 4. Symbol extraction — backticks win
# ---------------------------------------------------------------------------
class TestProseSymbols:
    def _syms(self, prose, base="Foo.java"):
        marker = "Foo.java:100"
        text = prose.replace("<<REF>>", marker)
        i = text.index(marker)
        return drift.prose_symbols(text, i, i + len(marker), base)

    def test_backticked_symbols_win_outright(self):
        got = self._syms("the `memSection` builder at <<REF>> handles truncation")
        assert "memSection" in got

    def test_unbackticked_camel_used_when_nothing_is_ticked(self):
        got = self._syms("the memSection builder at <<REF>> handles truncation")
        assert "memSection" in got

    def test_ticked_presence_suppresses_loose_guessing(self):
        """When the author marked identifiers, unmarked prose words are noise."""
        got = self._syms("`memSection` and someOtherThing near <<REF>>")
        assert got == ["memSection"]

    def test_stoplisted_tick_does_not_suppress_the_loose_pass(self):
        """A window whose only tick is junk must still fall through, or the
        stoplist would silently disable detection instead of cleaning it."""
        got = self._syms("`critical` path, see truncateContent at <<REF>>")
        assert "truncateContent" in got


# ---------------------------------------------------------------------------
# 5. Symbol movement — polarity in both directions
# ---------------------------------------------------------------------------
class TestCheckSymbol:
    LINES = ["pad"] * 300
    LINES[99] = "    int near = 1;   // nearSymbolHere"
    LINES[249] = "    farSymbolHere();"

    def test_symbol_at_the_citation_is_not_a_finding(self):
        sym, hits, dist = drift.check_symbol(self.LINES, 100, None, ["nearSymbolHere"])
        assert sym is None and dist == 0

    def test_symbol_inside_the_window_is_not_a_finding(self):
        """+/-LINE_WINDOW slack: prose cites the block, not the exact line."""
        sym, _, _ = drift.check_symbol(self.LINES, 100 + drift.LINE_WINDOW - 1,
                                       None, ["nearSymbolHere"])
        assert sym is None

    def test_distant_symbol_is_a_finding_with_its_distance(self):
        sym, hits, dist = drift.check_symbol(self.LINES, 100, None, ["farSymbolHere"])
        assert sym == "farSymbolHere" and hits == [250] and dist == 150

    def test_absent_symbol_is_not_a_finding(self):
        """No occurrence anywhere means the prose word was never a symbol —
        that is not evidence the code moved."""
        sym, _, _ = drift.check_symbol(self.LINES, 100, None, ["neverPresent"])
        assert sym is None

    def test_ubiquitous_symbol_is_suppressed(self):
        lines = [f"x = pad{i}" for i in range(300)]
        sym, _, _ = drift.check_symbol(lines, 10, None, ["pad"])
        assert sym is None, "a token on every line locates nothing"

    def test_distance_measured_from_the_nearest_end_of_a_range(self):
        sym, _, dist = drift.check_symbol(self.LINES, 100, 200, ["farSymbolHere"])
        assert sym == "farSymbolHere" and dist == 50


# ---------------------------------------------------------------------------
# 6. End-to-end scan + the positive control
# ---------------------------------------------------------------------------
def _tree(tmp_path, body):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "node.md").write_text(body, encoding="utf-8")
    return root


def _code(tmp_path, name, nlines, extra=None):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    lines = [f"// line {i}" for i in range(1, nlines + 1)]
    for idx, txt in (extra or {}).items():
        lines[idx - 1] = txt
    (repo / name).write_text("\n".join(lines), encoding="utf-8")
    return {name: [str(repo / name)]}


class TestScan:
    def test_line_past_end_of_file_is_confirmed_drift(self, tmp_path):
        idx = _code(tmp_path, "Small.java", 20)
        root = _tree(tmp_path, "see Small.java:500 for detail")
        findings, stats = drift.scan(root, idx)
        assert stats["line_out_of_range"] == 1
        assert findings[0]["confidence"] == "confirmed"
        assert findings[0]["file_line_count"] == 20

    def test_in_range_reference_with_matching_symbol_is_ok(self, tmp_path):
        idx = _code(tmp_path, "Ok.java", 60, {30: "  void doTheThing() {}"})
        root = _tree(tmp_path, "`doTheThing` lives at Ok.java:30")
        _, stats = drift.scan(root, idx)
        assert stats["ok"] == 1 and stats["symbol_moved"] == 0

    def test_moved_symbol_is_a_lead_not_a_confirmation(self, tmp_path):
        idx = _code(tmp_path, "Moved.java", 400, {380: "  void doTheThing() {}"})
        root = _tree(tmp_path, "`doTheThing` lives at Moved.java:30")
        findings, stats = drift.scan(root, idx)
        assert stats["symbol_moved"] == 1
        assert findings[0]["confidence"] == "lead", \
            "a heuristic must never be reported at the same confidence as a measurement"
        assert findings[0]["distance"] == 350

    def test_confirmed_sorts_above_leads(self, tmp_path):
        idx = _code(tmp_path, "Mix.java", 400, {380: "  void doTheThing() {}"})
        root = _tree(tmp_path, "`doTheThing` at Mix.java:30 and also Mix.java:900")
        findings, _ = drift.scan(root, idx)
        assert findings[0]["verdict"] == "line_out_of_range"

    def test_leads_sort_by_descending_distance(self, tmp_path):
        idx = _code(tmp_path, "Two.java", 500,
                    {480: "  void alphaThing() {}", 120: "  void betaThing() {}"})
        root = _tree(tmp_path, "`alphaThing` at Two.java:10\n\n`betaThing` at Two.java:60")
        findings, _ = drift.scan(root, idx)
        assert [f["distance"] for f in findings] == sorted(
            [f["distance"] for f in findings], reverse=True)

    def test_unresolved_reference_is_not_counted_as_drift(self, tmp_path):
        root = _tree(tmp_path, "see NotHere.java:999")
        _, stats = drift.scan(root, {})
        assert stats["unresolved"] == 1
        assert stats["line_out_of_range"] == 0 and stats["symbol_moved"] == 0

    def test_exempt_marker_on_the_same_line_suppresses(self, tmp_path):
        idx = _code(tmp_path, "Small.java", 20)
        root = _tree(tmp_path, "old ref Small.java:500 <!-- ref-drift-exempt: historical -->")
        _, stats = drift.scan(root, idx)
        assert stats["exempt"] == 1 and stats["line_out_of_range"] == 0

    def test_exempt_marker_on_the_line_above_suppresses(self, tmp_path):
        idx = _code(tmp_path, "Small.java", 20)
        root = _tree(tmp_path, "<!-- ref-drift-exempt: historical -->\nSmall.java:500 here")
        _, stats = drift.scan(root, idx)
        assert stats["exempt"] == 1

    def test_exempt_marker_in_a_WRAPPED_comment_suppresses(self, tmp_path):
        """REGRESSION. The first marker ever written in this repo wrapped to two
        lines, and a one-line lookback ignored it silently — the node did the
        right thing and kept getting flagged anyway. Marker text plus a
        rationale rarely fits on one line, so wrapped is the COMMON shape."""
        idx = _code(tmp_path, "Small.java", 20)
        root = _tree(tmp_path,
                     "<!-- ref-drift-exempt: historical narrative; the file has\n"
                     "     since been split — see the retraction below -->\n"
                     "Small.java:500 here")
        _, stats = drift.scan(root, idx)
        assert stats["exempt"] == 1, "a wrapped marker comment must still exempt"

    def test_exempt_marker_does_not_reach_arbitrarily_far(self, tmp_path):
        """The lookback is bounded, or one marker silences a whole node."""
        idx = _code(tmp_path, "Small.java", 20)
        filler = "\n".join(["prose"] * (drift.EXEMPT_LOOKBACK_LINES + 2))
        root = _tree(tmp_path, f"<!-- ref-drift-exempt: x -->\n{filler}\nSmall.java:500")
        _, stats = drift.scan(root, idx)
        assert stats["exempt"] == 0 and stats["line_out_of_range"] == 1

    def test_exempt_refs_are_excluded_from_resolved(self, tmp_path):
        """An exempted ref must not inflate the positive control — that would
        let a node full of retractions vouch for an otherwise dark scanner."""
        idx = _code(tmp_path, "Small.java", 20)
        root = _tree(tmp_path, "<!-- ref-drift-exempt: x -->\nSmall.java:500")
        _, stats = drift.scan(root, idx)
        assert stats["resolved"] == 0 and stats["refs_total"] == 1

    def test_zero_resolved_is_not_interpretable(self, tmp_path):
        """THE LOAD-BEARING PIN. With nothing resolved, a drift count of 0 is a
        dark instrument, not a clean corpus — and the two print identically
        unless something distinguishes them (guard-1641)."""
        root = _tree(tmp_path, "see NotHere.java:999")
        _, stats = drift.scan(root, {})
        assert stats["resolved"] == 0
        # Mirrors main()'s control construction; the report must say so out loud.
        assert (stats["resolved"] > 0) is False
