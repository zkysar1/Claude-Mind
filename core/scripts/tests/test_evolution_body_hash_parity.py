"""3-way parity pin for the evolution body-hash implementations ().

g-115-2567 made hash EQUALITY across three hand-maintained copies load-bearing:
the sweep's live-dedup join skips a backfill entry only when the commit blob's
sweep-side hash (`body_hash`, evolution-git-sweep.py — takes a content STRING)
equals the live entry's record-side hash (`compute_body_hash`,
evolution-record.py / evolution-prepare.py — take file PATHS). If any copy
diverges, the join silently degrades to the pre-g-115-2567 double-entry
behavior — fail-open, no error, only visible as skipped(live-captured)
dropping to 0. Fresh-eyes F1 on commit c3d3b98.

THE CONTRACT IS BOUNDARY-PARITY, and the ingestion layers are load-bearing
normalizers (discovered by this test's first run, g-115-2568):
  - sweep side: get_blob() runs `git show` via subprocess text=True →
    UNIVERSAL NEWLINE translation (\r\n and lone \r → \n) before body_hash
    ever sees the string.
  - record/prepare side: compute_body_hash reads via Path.read_text
    (newline=None) → the SAME universal translation.
So CRLF never reaches either hash function raw, and body_hash's FM-regex
(`^---\n`, which fails on ---\r\n) is safe ONLY because of that layer. If a
future edit switches get_blob to bytes+decode (no translation) or read_text
to newline="", CRLF-FM files silently break the join — the boundary tests
below fail loudly instead.

Contract pinned here:
  A. record == prepare on EVERY case (they are claimed copies of each other).
  B. sweep == record at the production boundary for every corpus-realistic
     shape (plain, front matter, CRLF-on-disk, trailing whitespace,
     blank-line edges, FM-only, missing/None).
  C. Known divergence corners are pinned AS divergent: empty content
     (sweep None vs record hash) and leading-whitespace first content line
     (sweep str.strip() vs record pop-empty-lines); plus the OFF-boundary
     raw-CRLF-string case (unreachable in production, reachable by a future
     caller that skips the subprocess layer). All fail OPEN for the join
     (no match → entry backfills → old behavior). If a corner converges or
     drifts further, the failing assertion forces a deliberate decision.

Run: STORAGE_BACKEND=local py -3 -m pytest core/scripts/tests/test_evolution_body_hash_parity.py -q
"""
import importlib.util
import re
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))


def _load(name, fname):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / fname)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


SWEEP = _load("egs_parity", "evolution-git-sweep.py")
RECORD = _load("erec_parity", "evolution-record.py")
PREPARE = _load("eprep_parity", "evolution-prepare.py")


def _as_subprocess_text(s):
    """What subprocess(text=True) delivers for these bytes: universal-newline
    translation (\r\n and lone \r → \n). Mirrors get_blob()'s ingestion."""
    return re.sub(r"\r\n?", "\n", s)


def _three_way(content):
    """(sweep_hash_at_boundary, record_hash, prepare_hash) for one on-disk
    content. write_bytes keeps line endings byte-exact; the sweep side gets
    the subprocess-translated form, exactly as production does."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "case.md"
        p.write_bytes(content.encode("utf-8"))
        return (SWEEP.body_hash(_as_subprocess_text(content)),
                RECORD.compute_body_hash(p),
                PREPARE.compute_body_hash(p))


EQUAL_CASES = {
    "plain": "# Title\n\nbody text\n",
    "front_matter": "---\nkey: value\nother: 2\n---\n# Title\n\nbody\n",
    "crlf_with_fm": "---\r\nkey: value\r\n---\r\n# Title\r\n\r\nbody\r\n",
    "lone_cr_endings": "# T\rbody\r",
    "trailing_ws": "# T  \n\nbody   \n",
    "leading_blank_lines": "\n\n# T\nbody\n",
    "trailing_blank_lines": "# T\nbody\n\n\n",
    "fm_only_empty_body": "---\nk: v\n---\n",
}


def test_equal_on_all_corpus_realistic_shapes():
    for name, content in EQUAL_CASES.items():
        s, r, p = _three_way(content)
        assert r == p, f"{name}: record vs prepare diverged ({r} != {p})"
        assert s == r, (f"{name}: sweep vs record diverged ({s} != {r}) — "
                        f"live-dedup join silently broken for this shape")
        assert s is not None, f"{name}: unexpectedly un-hashable"


def test_crlf_fm_stripping_depends_on_ingestion_translation():
    # The load-bearing-normalizer proof, both sides:
    # 1. record on CRLF bytes == record on LF bytes (read_text translated,
    #    so the FM check `startswith('---\n')` still fires).
    # 2. sweep on the subprocess-translated form == the same hash.
    # 3. sweep on the RAW CRLF string (off-boundary) DIVERGES — the FM regex
    #    `^---\n` fails on ---\r\n, leaving FM in the hashed text. Reachable
    #    only by a caller that bypasses subprocess text-mode; pinned so a
    #    future get_blob bytes+decode refactor is caught by (2) failing.
    crlf = "---\r\nkey: v\r\n---\r\n# T\r\n\r\nbody\r\n"
    lf = crlf.replace("\r\n", "\n")
    with tempfile.TemporaryDirectory() as d:
        p_crlf = Path(d) / "crlf.md"
        p_crlf.write_bytes(crlf.encode("utf-8"))
        p_lf = Path(d) / "lf.md"
        p_lf.write_bytes(lf.encode("utf-8"))
        r_crlf = RECORD.compute_body_hash(p_crlf)
        r_lf = RECORD.compute_body_hash(p_lf)
    assert r_crlf == r_lf, "read_text universal-newline translation regressed"
    assert SWEEP.body_hash(_as_subprocess_text(crlf)) == r_crlf
    assert SWEEP.body_hash(crlf) != r_crlf, (
        "off-boundary raw-CRLF converged — body_hash FM regex now CRLF-aware? "
        "If deliberate, update this pin and the module docstring")


def test_front_matter_invariance_both_sides():
    # Stripping FM is what makes a live capture and a git blob hash identically
    # even when only FM fields (last_updated etc.) differ around the same body.
    body = "# Title\n\nbody\n"
    s_fm, r_fm, p_fm = _three_way("---\nkey: value\nother: 2\n---\n" + body)
    s_plain, r_plain, _ = _three_way(body)
    assert s_fm == s_plain, "sweep: FM presence changed the hash"
    assert r_fm == r_plain, "record: FM presence changed the hash"
    assert p_fm == r_fm


def test_missing_and_none_all_return_none():
    assert SWEEP.body_hash(None) is None
    missing = Path(tempfile.gettempdir()) / "parity-nonexistent-g1152568.md"
    assert not missing.exists()
    assert RECORD.compute_body_hash(missing) is None
    assert PREPARE.compute_body_hash(missing) is None


# ---- documented-divergent corners (pinned; all fail OPEN for the join) ----

def test_divergent_corner_empty_content():
    # sweep treats "" as falsy → None; record/prepare hash the normalized "\n".
    # Consequence: an EMPTY tracked file never live-dedups (its sweep entry
    # backfills) — acceptable, zero real-corpus instances.
    s, r, p = _three_way("")
    assert r == p and r is not None
    assert s is None


def test_divergent_corner_leading_ws_first_content_line():
    # sweep runs str.strip() on the joined text (removes the first content
    # line's leading indent); record/prepare pop only fully-empty lines.
    s, r, p = _three_way("  indented first line\nbody\n")
    assert r == p and r is not None and s is not None
    assert s != r, "corner converged — if deliberate, move this case to EQUAL_CASES"
