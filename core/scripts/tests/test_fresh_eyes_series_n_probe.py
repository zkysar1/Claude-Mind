""" — the Phase 2.0(a) series-index probe lives in a script and reads a row's OWN index.

WHY THIS FILE EXISTS. The probe that allocates the next series index N used to be one
line of `.claude/skills/fresh-eyes-review/SKILL.md`. A skill body is rewritten with its
invocation arguments before the model reads it, so under `--cadence` (the only automatic
path) the awk positional parameter in that line became `match(--cadence, ...)` — valid
awk, exit 0, no output. Branch 3 (the table-row branch) silently vanished and a shard whose
index lives only in table rows read 34 against a true 202, so the next pass would have
minted a slot that already holds a reading (guard-5508). The probe now lives in
`core/scripts/fresh-eyes-series-n.sh`, which is never rewritten.

THE SECOND DEFECT, FIXED IN THE SAME MOVE. Branch 3 used to take the FIRST `N=` in a
`|` row as that row's index. A row's first `N=` is often somebody else's: a cell reading
"carried from N=182" ahead of its own `**N=183` returned 182 (a COLLISION, the dangerous
direction), and a value row reading "carry it to N=196" returned 196 (a skipped index).
Both were held off only by a write-side convention (guard-5784). The script reads each
row's OWN index instead: the bold `N=k` that OPENS a cell, leftmost in the row. Measured on
the five live shards the new rule returned the same N as the old one on every shard (204,
200, 190, 139, 202) and corrected the two zeta rows whose first `N=` was a backward pointer.

WHAT IS PINNED, AND WHAT KILLS IT (mutation matrix in the design notes of g-115-10980):
  * no positional parameter anywhere in the SKILL.md          -> re-inlining the awk line
  * the SKILL.md calls the script, once                       -> deleting the call
  * a value row's forward pointer is not a series point       -> reverting to first-`N=`
  * a backward pointer ahead of the own index is not the row  -> reverting to first-`N=`
  * only a cell-HEAD bold token counts                        -> dropping the cell anchor
  * the row's FIRST cell-head bold token is its index         -> `grep -o` (every token)
  * a capital-N word ahead of the index cannot drop the row   -> a `[^N]*` prefix (g-115-10215)
  * "handoff to N=k" headings are excluded in any casing      -> dropping `-i`
  * a missing / empty authoritative read is fatal, rc 1       -> removing either guard

The seam: `FRESH_EYES_SERIES_N_SCRIPT` / `FRESH_EYES_SKILL_MD` point the suite at a mutated
copy. Unset (production), both resolve to the repo's own files.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_helpers import BASH  # noqa: E402  (needs the path insert above)

REPO = Path(__file__).resolve().parents[3]
SCRIPT = Path(os.environ.get("FRESH_EYES_SERIES_N_SCRIPT") or REPO / "core" / "scripts" / "fresh-eyes-series-n.sh")
SKILL = Path(os.environ.get("FRESH_EYES_SKILL_MD") or REPO / ".claude" / "skills" / "fresh-eyes-review" / "SKILL.md")
SHARD_DIR = "knowledge/tree/system/directive-lane-compliance"
CALL_LINE = "Bash: bash core/scripts/fresh-eyes-series-n.sh"

# --- the harness's own substitution, as guard-5508 measured it ----------------------------
_POSITIONAL = re.compile(r"\$(\d+)(?!\w)")
# --- the wider net OUTCOME 3 names: any $digit, $@, $* ------------------------------------
_POSITIONAL_ANY = re.compile(r"\$\d|\$@|\$\*")


def _inject(text: str, args: list[str]) -> str:
    """What `/fresh-eyes-review --cadence` delivers: every `$N` replaced by argument N."""
    return _POSITIONAL.sub(lambda m: args[int(m.group(1))] if int(m.group(1)) < len(args) else "", text)


def _run(tmp_path: Path, shard: str | None, *, agent: str | None = "alpha"):
    """Run the REAL script against a temp world on the local backend (the file IS the store)."""
    world = tmp_path / "world"
    shard_dir = world / SHARD_DIR
    shard_dir.mkdir(parents=True, exist_ok=True)
    if shard is not None:
        (shard_dir / "directive-lane-series-alpha.md").write_bytes(shard.encode("utf-8"))
    env = {k: v for k, v in os.environ.items() if k not in ("MIND_AGENT", "STORAGE_S3_ENDPOINT_URL")}
    env.update({"STORAGE_BACKEND": "local", "MIND_WORLD": str(world)})
    if agent is not None:
        env["MIND_AGENT"] = agent
    # BASH, never a bare "bash" argv[0] (guard-580); .as_posix(), never str(Path) (guard-581).
    return subprocess.run([BASH, SCRIPT.as_posix()], cwd=str(REPO), capture_output=True, text=True, env=env)


def _skill_lines() -> list[str]:
    return SKILL.read_text(encoding="utf-8").split("\n")


def _is_comment(line: str) -> bool:
    return line.lstrip().startswith("#")


# ─── SKILL.md: nothing the harness can rewrite ──────────────────────────────────────────

def test_skill_md_carries_no_positional_parameter():
    """Any `$N`, `$@` or `$*` in a skill body is at the mercy of the invocation arguments.
    Prose counts too: say "positional parameter", never the literal token."""
    hits = [(i + 1, m.group(0)) for i, l in enumerate(_skill_lines()) for m in _POSITIONAL_ANY.finditer(l)]
    assert not hits, f"positional parameter(s) in {SKILL.name}: {hits}"


def test_the_scan_can_fire_positive_control():
    """A scan that cannot fire proves nothing (guard-1760): the pre-fix awk line is a hit."""
    old = "awk 'match($0, /N=[0-9]+/) { print substr($0, RSTART, RLENGTH) }'"
    assert _POSITIONAL_ANY.search(old)
    assert "match(--cadence," in _inject(old, ["--cadence"])


def test_injection_under_cadence_is_the_identity_on_the_whole_skill():
    """The invariant itself: what the model reads under `--cadence` is what is on disk."""
    text = SKILL.read_text(encoding="utf-8")
    assert _inject(text, ["--cadence"]) == text


def test_the_skill_calls_the_probe_script_exactly_once():
    calls = [l for l in _skill_lines() if not _is_comment(l) and l.strip() == CALL_LINE]
    assert len(calls) == 1, f"expected one `{CALL_LINE}` line, found {len(calls)}"


def test_the_probe_is_not_inlined_back_into_the_skill():
    """The awk construct is what the harness corrupted; it belongs in the script only."""
    inline = [i + 1 for i, l in enumerate(_skill_lines()) if not _is_comment(l) and "match(" in l]
    assert not inline, f"inline awk match() in {SKILL.name} at lines {inline}"


# ─── the script: what it returns ────────────────────────────────────────────────────────

# A field|value shape (the index lives in headings and in a bold first cell). The value rows
# carry a FORWARD pointer, a stray large token, and a closing bold marker before an unbolded
# reference — none of them is a series point. Expected N: 202.
VALUE_ROWS = """\
# Series
### Reading at 2026-10-01 (agent, N=201, since 100)
| field | value |
|---|---|
| **N=201** | the 201st reading |
| cadence | series since 100 (the N=200 row) |
### Reading at 2026-10-02 (agent, N=202, since 140)
| field | value |
|---|---|
| **N=202** | the 202nd reading |
| carry | N=203 will re-score this window |
| note | **Population note:** N=250's figures differ |
### Handoff to N=203
"""

# A horizontal shape where the index sits in a LATER cell. Row N=183 opens with a backward
# pointer; row N=184 opens with two and carries a second bold index after its own. Expected: 184.
# Rows N=116, N=135 and N=164 are the  fixtures; N=183 and N=184 are modelled on the
# zeta pointer rows that  measured.
TABLE_ROWS = """\
# Series
| when | share | verdict |
|---|---|---|
| 2026-08-29 | **15.7%** (NEW lane) | **N=116 — NEW lane** |
| 2026-09-04 | **~15%** (UNCHANGED from last week) | **N=135 — held** |
| 2026-09-17 | **28.3%** (NOT comparable) | ** N=164 — spaced older style** |
| 2026-09-26 | carried from N=182 (diff 25) | **N=183 — POINTER ROW; a later cell names N=190** |
| 2026-09-27 | prior points N=181, N=182 | **N=184 — next** | **N=185 — predicted** |
"""

# The newest row has a capital-N word ahead of its own index. A `[^N]*` prefix drops it and
# the probe returns the row before it (). Expected: 164.
CAPITAL_N_NEWEST = """\
# Series
| when | share | verdict |
|---|---|---|
| 2026-09-16 | **28.0%** (steady) | **N=163 — steady** |
| 2026-09-17 | **28.3%** (NOT comparable) | **N=164 — NOT comparable to N=163** |
"""

# The live echo shard has no N= heading and opens every row with a bold N= (measured under ).
ECHO_SHAPE = """\
# Series
| point | share | verdict |
|---|---|---|
| **N=188** 2026-10-02 08:30 | 30% | a row |
| **N=189** 2026-10-03 02:10 | 31% | another row that names N=191 in its body |
| **N=190** 2026-10-03 08:30 | 32% | newest |
"""

HEADINGS_ONLY = """\
## N=137 — 2026-09-30
body text that cites nothing
## Notes without an index
## N=139 — 2026-10-02T17:2x
### Handoff to N=140
"""

CAPS_HANDOFF = """\
## N=55 — reading
## N=56 — reading
### HANDOFF to N=57
"""


@pytest.mark.parametrize(
    "shard, expected",
    [
        pytest.param(VALUE_ROWS, "202", id="value-row-forward-pointer-and-closing-bold-are-not-points"),
        pytest.param(TABLE_ROWS, "184", id="backward-pointer-ahead-of-own-index-and-first-bold-wins"),
        pytest.param(CAPITAL_N_NEWEST, "164", id="capital-n-word-ahead-of-the-index-does-not-drop-the-row"),
        pytest.param(ECHO_SHAPE, "190", id="rows-with-a-bold-first-cell-and-no-headings"),
        pytest.param(HEADINGS_ONLY, "139", id="headings-only-shard"),
        pytest.param(CAPS_HANDOFF, "56", id="handoff-heading-excluded-in-any-casing"),
    ],
)
def test_the_probe_returns_the_newest_own_index(tmp_path, shard, expected):
    r = _run(tmp_path, shard)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == expected, f"stdout={r.stdout!r} stderr={r.stderr!r}"


# ─── the script: how it fails ───────────────────────────────────────────────────────────

def test_a_missing_authoritative_read_is_fatal_not_empty(tmp_path):
    """No shard in the store: N is UNALLOCATABLE. rc 1, the reason on stderr, nothing on stdout —
    a caller capturing stdout must never read an error as a number."""
    r = _run(tmp_path, None)
    assert r.returncode == 1
    assert "FATAL: authoritative read of" in r.stderr and "UNALLOCATABLE" in r.stderr
    assert r.stdout.strip() == ""


def test_an_empty_authoritative_read_is_fatal(tmp_path):
    r = _run(tmp_path, "")
    assert r.returncode == 1
    assert "returned 0 bytes" in r.stderr
    assert r.stdout.strip() == ""


def test_an_unset_agent_is_fatal(tmp_path):
    r = _run(tmp_path, VALUE_ROWS, agent=None)
    assert r.returncode == 1
    assert "MIND_AGENT is unset" in r.stderr
    assert r.stdout.strip() == ""


def test_the_probe_reads_the_store_never_the_mirror():
    """Pinned at SOURCE level: under `STORAGE_BACKEND=local` the file IS the store, so a mirror
    read is indistinguishable from the authoritative one here (g-115-8055, guard-157)."""
    code = [l for l in SCRIPT.read_text(encoding="utf-8").split("\n") if not _is_comment(l) and not l.lstrip().startswith("echo")]
    assert any('backend-cat.sh" cat "$P"' in l for l in code), "no authoritative backend-cat read"
    assert not [l for l in code if "WORLD_PATH" in l or "WORLD_DIR" in l], "script reads the mirror path"
