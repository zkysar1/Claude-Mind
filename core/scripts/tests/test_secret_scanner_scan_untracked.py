"""Regression pins for `check-no-hardcoded-secrets.sh --scan-untracked` ().

The untracked mode scans the GITIGNORED surface `agents/*/temp/` by NAME shape,
never by content. Before g-374-61 it had two defects and no tests at all:

  1. It globbed the LOCAL tree. `agents/<agent>/temp` is an own-cloud
     read-through-cached surface, so one box saw a small part of the fleet's
     dotfile population (the goal record measured 17 of 187, 9%). It now also
     has a STORE lane that enumerates the authoritative store through the
     sibling lister's `list` subcommand and nothing else.
  2. The bare `session` token in the credential-name predicate flagged live
     cadence state (`.current-session-log`, `.notify-session-143-end.md`).

The scanner copies and runs the way production does -- as a script beside its
lister -- inside a throwaway git repo, with a stub lister that serves canned
listings and LOGS EVERY CALL. Expected values are constants written here or are
computed from the fixture lists, never read back from the code under test
(guard-1220), and every absence claim has a positive control in the same test
(guard-4166). One contract test runs the REAL lister so the stub is not the only
producer the scanner has ever been checked against (guard-920).

The names below are literal fixtures. No file content is read by the scanner or
by these tests; the files they create hold one byte.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _runtime_bash import BASH  # noqa: E402  guard-580: never a bare "bash" argv[0]

SCRIPTS = Path(__file__).resolve().parents[1]
SCANNER_SRC = SCRIPTS / "check-no-hardcoded-secrets.sh"
REAL_LISTER = SCRIPTS / "backend-cat.sh"

STUB = """#!/usr/bin/env bash
# Stub lister for scan-untracked tests. Serves canned listings, logs every call.
echo "$*" >> "{store}/calls.log"
[ "${{1:-}}" = "list" ] || {{ echo "stub: only list is implemented (got: $*)" >&2; exit 2; }}
key="${{2//\\//__}}"
if [ -f "{store}/$key.txt" ]; then cat "{store}/$key.txt"; exit 0; fi
if [ -f "{store}/$key.err" ]; then cat "{store}/$key.err" >&2; exit 1; fi
echo "backend-cat: empty or absent in stub store: $2" >&2
exit 1
"""

# Names the predicate must flag / must leave alone. Constants, not derived from
# the regex under test.
FLAGGED = [".tok", ".probe-pw", ".session-token", ".aws-session-key", ".api_key"]
NOT_FLAGGED = [
    ".current-session-log", ".notify-session-143-end.md", ".last-selector-path",
    ".cadence-ts", ".fe-ts",
]


class Box:
    """A throwaway repo that holds a copy of the scanner, a stub lister and a store."""

    def __init__(self, root: Path):
        self.repo = root / "r"
        self.store = root / "store"
        self.tmpdir = root / "tmpdir"
        for d in (self.repo / "core" / "scripts", self.store, self.tmpdir):
            d.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True, capture_output=True)
        self.scanner = self.repo / "core" / "scripts" / "check-no-hardcoded-secrets.sh"
        shutil.copy2(SCANNER_SRC, self.scanner)
        self.lister = self.repo / "core" / "scripts" / "backend-cat.sh"
        self.lister.write_text(STUB.format(store=self.store))

    # -- fixtures -----------------------------------------------------------
    def local(self, agent: str, *names: str, size: int = 1):
        d = self.repo / "agents" / agent / "temp"
        d.mkdir(parents=True, exist_ok=True)
        for n in names:
            (d / n).write_bytes(b"x" * size)

    def store_listing(self, path: str, names):
        key = path.replace("/", "__")
        (self.store / (key + ".err")).unlink(missing_ok=True)      # a path serves a listing OR an error, never both
        (self.store / (key + ".txt")).write_text("".join(n + "\n" for n in names))

    def store_error(self, path: str, text: str):
        key = path.replace("/", "__")
        (self.store / (key + ".txt")).unlink(missing_ok=True)
        (self.store / (key + ".err")).write_text(text + "\n")

    def calls(self):
        log = self.store / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    # -- run ----------------------------------------------------------------
    def scan(self, *, cwd=None, script=None, backend="own-cloud", env_extra=None):
        env = os.environ.copy()
        env.pop("ALLOW_SECRETS_IN_COMMIT", None)
        env["TMPDIR"] = str(self.tmpdir)
        if backend is None:
            env.pop("STORAGE_BACKEND", None)
        else:
            env["STORAGE_BACKEND"] = backend
        env.update(env_extra or {})
        return subprocess.run(
            [BASH, script or str(self.scanner), "--scan-untracked"],
            cwd=str(cwd or self.repo), capture_output=True, text=True, check=False, env=env,
        )


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


ROW = re.compile(
    r"^\s+(?P<agent>[\w.-]+): (?P<dots>\d+) dot-name\(s\) of (?P<tot>\d+) depth-1 entr(?:y|ies)$", re.M
)
HIT = re.compile(r"^  (?P<verdict>CRED-SHAPED|EMPTY-CRED-NAME) (?P<path>agents/\S+?)\|(?P<rest>.*)$", re.M)


def _fleet_fixture(box):
    """alpha + bravo have a temp subtree in the store; charlie and a stray file name do not."""
    box.store_listing("agents", ["alpha", "bravo", "charlie", "notes.jsonl"])
    alpha = [".gitkeep", ".cadence-ts", ".tok", ".last-selector-path", "a.md", "b.json"]
    bravo = [".gitkeep", ".probe-pw", "x.json"]
    box.store_listing("agents/alpha/temp", alpha)
    box.store_listing("agents/bravo/temp", bravo)
    return {"alpha": alpha, "bravo": bravo}


# ── outcome 1: the store lane enumerates the fleet, and the count is independent ──

def test_store_lane_counts_match_an_independent_listing(box):
    fleet = _fleet_fixture(box)
    box.local("alpha", ".gitkeep")
    r = box.scan()
    rows = {m["agent"]: (int(m["dots"]), int(m["tot"])) for m in ROW.finditer(r.stdout)}
    # Expected values come from the fixture lists, not from the scanner.
    expected = {a: (sum(n.startswith(".") for n in names), len(names)) for a, names in fleet.items()}
    assert expected == {"alpha": (4, 6), "bravo": (2, 3)}   # the fixture itself is what we think it is
    assert rows == expected, r.stdout
    assert "across 2 agent temp dir(s)" in r.stdout
    assert "6 dot-name(s)" in r.stdout            # 4 + 2, the fleet figure


def test_store_lane_finds_known_credential_names_not_an_empty_set(box):
    _fleet_fixture(box)
    box.local("alpha", ".gitkeep")
    r = box.scan()
    flagged = {m["path"] for m in HIT.finditer(r.stdout)}
    assert flagged == {"agents/alpha/temp/.tok", "agents/bravo/temp/.probe-pw"}, r.stdout
    assert r.returncode == 1


def test_agents_without_a_temp_subtree_are_named_not_counted(box):
    _fleet_fixture(box)
    box.local("alpha", ".gitkeep")
    out = box.scan().stdout
    line = [ln for ln in out.splitlines() if "NO ENTRIES UNDER temp IN THE STORE" in ln]
    assert len(line) == 1, out
    assert line[0].rstrip().endswith("charlie, notes.jsonl")
    assert not ROW.search("\n".join(ln for ln in out.splitlines() if "charlie" in ln and "dot-name" in ln))


def test_clean_run_cites_the_fleet_count_beside_the_box_count(box):
    box.store_listing("agents", ["alpha"])
    box.store_listing("agents/alpha/temp", [".gitkeep", ".cadence-ts", ".last-selector-path", ".fe-ts", "n.md"])
    box.local("alpha", ".gitkeep")
    r = box.scan()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "CLEAN" in r.stdout
    # check 2: both numbers and the ratio are on one line (4 store dot-names, 1 local = 25%).
    assert "Fleet vs box: the store lane saw 4 dot-name(s); this box's local lane scanned 1 (25% of that figure)" in r.stdout


# ── outcome 2: each surface states the fields it can supply ──

@pytest.mark.parametrize("shape", ["hits", "clean", "inconclusive"])
def test_report_states_fields_per_surface_in_every_outcome(box, shape):
    box.local("alpha", ".gitkeep")
    if shape == "hits":
        box.store_listing("agents", ["alpha"])
        box.store_listing("agents/alpha/temp", [".tok"])
    elif shape == "clean":
        box.store_listing("agents", ["alpha"])
        box.store_listing("agents/alpha/temp", [".cadence-ts"])
    else:
        box.lister.unlink()          # store lane unavailable
    r = box.scan()
    assert {"hits": 1, "clean": 0, "inconclusive": 2}[shape] == r.returncode, r.stdout + r.stderr
    assert "local lane : path, size, mode, mtime" in r.stdout
    assert "store lane : path ONLY" in r.stdout
    assert "no size, no mode, no mtime" in r.stdout


def test_local_hit_row_keeps_all_four_fields_and_store_hit_row_claims_only_the_path(box):
    box.local("alpha", ".gitkeep", ".tok", size=7)
    box.store_listing("agents", ["alpha"])
    box.store_listing("agents/alpha/temp", [".gitkeep", ".probe-pw"])
    out = box.scan().stdout
    hits = {m["path"]: m["rest"] for m in HIT.finditer(out)}
    assert re.fullmatch(r"7B\|mode \d{3,4}\|mtime \d{4}-\d{2}-\d{2}", hits["agents/alpha/temp/.tok"]), hits
    assert hits["agents/alpha/temp/.probe-pw"] == "path only (store lane)"


def test_store_lane_never_produces_the_size_based_empty_verdict(box):
    """A store name has no size. A default of 0 would trip EMPTY-CRED-NAME on every
    store hit, so the lane keeps its own loop: positive control = the LOCAL lane,
    given a real zero-byte file of the same name, does produce it."""
    box.local("alpha", ".gitkeep", ".tok2", size=0)
    box.store_listing("agents", ["alpha"])
    box.store_listing("agents/alpha/temp", [".gitkeep", ".tok2", ".probe-pw"])
    out = box.scan().stdout
    verdicts = [(m["verdict"], m["rest"].startswith("path only")) for m in HIT.finditer(out)]
    assert ("EMPTY-CRED-NAME", False) in verdicts, out              # local lane, size 0: the control fires
    store_verdicts = [v for v, path_only in verdicts if path_only]
    assert store_verdicts == ["CRED-SHAPED", "CRED-SHAPED"], out    # .tok2 and .probe-pw, never EMPTY


# ── outcome 3: the scan never opens a scanned file ──

def test_store_lane_uses_list_only_on_the_agents_root_and_each_temp_dir(box):
    _fleet_fixture(box)
    box.local("alpha", ".gitkeep")
    box.scan()
    calls = box.calls()
    assert calls, "the stub was never called -- the store lane did not run"
    assert {c.split()[0] for c in calls} == {"list"}, calls
    assert sorted(calls) == sorted(
        ["list agents"] + [f"list agents/{a}/temp" for a in ("alpha", "bravo", "charlie", "notes.jsonl")]
    )


def _untracked_block():
    text = SCANNER_SRC.read_text().splitlines()
    start = next(i for i, ln in enumerate(text) if ln.startswith('if [[ "$MODE" == "untracked" ]]'))
    end = next(i for i, ln in enumerate(text) if i > start and ln.startswith("# ─── Override path"))
    return text[start:end], text[:start] + text[end:]


CONTENT_READERS = re.compile(r"(^|[^a-z_-])(cat|head|awk|sed|od|strings)([^a-z_-]|$)|git |\$\(<")


def test_untracked_block_has_no_command_that_could_read_a_scanned_file():
    block, rest = _untracked_block()
    code = [ln for ln in block if not ln.strip().startswith("#")]
    assert code, "extracted an empty block -- the extraction is broken, not the scanner clean"
    offenders = [ln for ln in code if CONTENT_READERS.search(ln)]
    assert offenders == []
    # Positive control: the same pattern DOES fire on the rest of the script, whose staged and
    # head modes read content by design. A pattern that matched nothing anywhere proves nothing.
    assert any(CONTENT_READERS.search(ln) for ln in rest if not ln.strip().startswith("#"))


def test_every_lister_call_in_the_block_is_a_list_call():
    block, _ = _untracked_block()
    # The existence test `[[ ! -f "$store_lister" ]]` mentions the lister but invokes nothing.
    sites = [
        ln for ln in block
        if '"$store_lister"' in ln and '-f "$store_lister"' not in ln and not ln.strip().startswith("#")
    ]
    assert len(sites) >= 1, sites           # the single _store_list helper is the one door
    assert all('"$store_lister" list ' in ln for ln in sites), sites


# ── outcome 4: the bare `session` token, both directions, on both lanes ──

@pytest.mark.parametrize("lane", ["local", "store"])
def test_session_token_no_longer_flags_cadence_state_but_compound_names_still_match(box, lane):
    names = FLAGGED + NOT_FLAGGED
    if lane == "local":
        box.local("alpha", ".gitkeep", *names)
        box.store_listing("agents", ["alpha"])
        box.store_listing("agents/alpha/temp", [".gitkeep", ".cadence-ts"])   # keep the store lane benign
    else:
        box.local("alpha", ".gitkeep")
        box.store_listing("agents", ["alpha"])
        box.store_listing("agents/alpha/temp", [".gitkeep", *names])
    out = box.scan().stdout
    flagged = {Path(m["path"]).name for m in HIT.finditer(out)}
    assert flagged == set(FLAGGED), (lane, out)
    assert not flagged & set(NOT_FLAGGED)


# ── fail-visible: a scan that could not see the population never reads as clean ──

def test_unavailable_store_lane_is_inconclusive_and_says_the_run_covers_one_box(box):
    box.local("alpha", ".gitkeep", ".cadence-ts")
    box.lister.unlink()
    r = box.scan()
    assert r.returncode == 2
    assert "INCONCLUSIVE" in r.stderr and "NOT a clean result" in r.stderr
    assert "Store lane — UNAVAILABLE" in r.stdout and "covers THIS BOX only" in r.stdout
    assert "CLEAN" not in r.stdout + r.stderr


def test_a_local_hit_outranks_an_unavailable_store_lane(box):
    box.local("alpha", ".gitkeep", ".tok")
    box.lister.unlink()
    r = box.scan()
    assert r.returncode == 1
    assert "Store lane — UNAVAILABLE" in r.stdout


def test_a_listing_error_is_named_unreadable_and_never_folded_into_absence(box):
    _fleet_fixture(box)
    box.store_listing("agents/alpha/temp", [".cadence-ts"])              # benign, so only the error decides
    box.store_error("agents/bravo/temp", "botocore.exceptions.ClientError: AccessDenied")
    box.local("alpha", ".gitkeep")
    r = box.scan()
    assert r.returncode == 2, r.stdout + r.stderr
    unreadable = [ln for ln in r.stdout.splitlines() if "UNREADABLE" in ln]
    assert len(unreadable) == 1 and "bravo" in unreadable[0] and "AccessDenied" in unreadable[0]
    absent = [ln for ln in r.stdout.splitlines() if "NO ENTRIES UNDER temp" in ln][0]
    assert "bravo" not in absent and "charlie" in absent        # positive control: real absences still land here


def test_a_store_that_lists_no_dot_name_anywhere_is_not_clean(box):
    box.store_listing("agents", ["alpha"])
    box.store_listing("agents/alpha/temp", ["a.md", "b.json"])
    box.local("alpha", ".gitkeep")
    r = box.scan()
    assert r.returncode == 2, r.stdout + r.stderr
    assert "found no dot-name in any of them" in r.stderr


def test_nothing_enumerated_in_either_lane_is_a_broken_scan(box):
    (box.repo / "agents" / "alpha" / "temp").mkdir(parents=True)         # a local temp dir with no dotfile
    box.store_listing("agents", ["alpha"])                               # and an agent with nothing under temp
    r = box.scan()
    assert r.returncode == 2
    assert "FOUND NO DOTFILES AT ALL" in r.stderr


@pytest.mark.parametrize(
    "backend, expect",
    [
        ("own-cloud", "(backend: own-cloud)"),
        (None, "backend: not identified (STORAGE_BACKEND is unset in this shell"),
        ("local", "the store IS this box's filesystem"),
    ],
)
def test_the_report_names_the_backend_without_assuming_one(box, backend, expect):
    box.store_listing("agents", ["alpha"])
    box.store_listing("agents/alpha/temp", [".gitkeep", ".cadence-ts"])
    box.local("alpha", ".gitkeep")
    r = box.scan(backend=backend)
    assert r.returncode == 0, r.stdout + r.stderr
    assert expect in r.stdout


# ── mechanics ──

def test_a_relative_invocation_from_another_directory_still_finds_the_lister(box):
    """SCRIPT_DIR is resolved BEFORE the scanner cds to the repo root; resolved after, a
    relative ${BASH_SOURCE[0]} would point nowhere and the store lane would be UNAVAILABLE."""
    box.store_listing("agents", ["alpha"])
    box.store_listing("agents/alpha/temp", [".gitkeep", ".cadence-ts"])
    box.local("alpha", ".gitkeep")
    r = box.scan(cwd=box.repo / "core" / "scripts", script="check-no-hardcoded-secrets.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Store lane — authoritative store" in r.stdout and "UNAVAILABLE" not in r.stdout


@pytest.mark.parametrize("shape", ["hits", "clean", "inconclusive"])
def test_scanner_leaves_no_temp_residue_on_any_exit_path(box, shape):
    box.local("alpha", ".gitkeep")
    box.store_listing("agents", ["alpha"])
    box.store_listing("agents/alpha/temp", {"hits": [".tok"], "clean": [".cadence-ts"], "inconclusive": ["a.md"]}[shape])
    r = box.scan()
    assert r.returncode == {"hits": 1, "clean": 0, "inconclusive": 2}[shape]
    assert list(box.tmpdir.iterdir()) == []


# ── contract: the stub is only trustworthy if the REAL lister behaves the way it assumes ──

def test_real_lister_prints_one_name_per_line_and_the_absence_marker_the_scanner_reads(tmp_path):
    present = tmp_path / "present"
    present.mkdir()
    for n in (".b", ".a", "c.md"):
        (present / n).write_bytes(b"x")
    env = {**os.environ, "STORAGE_BACKEND": "local"}      # guard-955: never the box's own-cloud
    ok = subprocess.run([BASH, str(REAL_LISTER), "list", str(present)], capture_output=True, text=True, env=env)
    assert ok.returncode == 0, ok.stderr
    assert ok.stdout.splitlines() == sorted(os.listdir(present))     # expected from the filesystem, not the lister

    gone = subprocess.run([BASH, str(REAL_LISTER), "list", str(tmp_path / "absent")],
                          capture_output=True, text=True, env=env)
    assert gone.returncode == 1
    marker = "backend-cat: empty or absent"
    assert marker in gone.stderr
    # Both ends of the contract name the same string: the lister emits it, the scanner reads it.
    assert marker in REAL_LISTER.read_text() and marker in SCANNER_SRC.read_text()
