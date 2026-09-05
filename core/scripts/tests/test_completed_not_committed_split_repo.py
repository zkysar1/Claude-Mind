"""test_completed_not_committed_split_repo.py — regression for .

`classify_stranded` decides LANDED by sha containment against the ref
`resolve_default_ref` returns, which is the repo's GitHub DEFAULT branch. On the
six repos in the Split-Repo Registry (world/conventions/sdlc-environments.md,
charter v1 OWNER-APPROVED 2026-08-27) that default branch is `main` — the PROD
branch. Feature PRs merge to `dev`; the charter states verbatim "Nothing merges
to `main` directly"; dev->main travels only on the weekly promotion PR owned by
asp-370. So on those repos a CORRECTLY-merged commit is uncontained by the
default branch for up to a week BY DESIGN, and the sweep filed an Investigate
whose prescribed remedy ("merge it") is a guard-5389 charter violation into an
Amplify auto-deploying PROD target.

Measured 2026-09-05 on zkysar1/Vinheim-Web-App: origin/dev 64 ahead / 0 behind
origin/main, and four goals (g-115-9025/9027/9028/9029) flagged as stranded
whose commits had all merged to dev via #426/#427/#429/#430. Two of the four
were independently investigated to the same false-positive verdict by separate
worker Bodies (cc-13 and cc-08) before the class was measured — the wasted work
this fix exists to stop. Encoded as guard-6045.

THE SUPPRESSION DIRECTION IS WHAT MAKES THESE LOAD-BEARING. Redirecting the
containment ref makes MORE commits read as landed, so the failure mode is
hiding a genuinely stranded deliverable — the defect the whole sweep exists to
catch. Hence every widening case below is paired with a control asserting the
OLD behaviour still holds where it should: an unlisted repo, an unreadable
registry, and a listed repo with no `dev` branch must each resolve to the
default branch exactly as before.

Tested as a PROPERTY over an injected registry, never against the six live repo
names (guard-3080) — the registry is read once and injected, so these stay true
when the sixth tier lands.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "cnc_split_repo", CORE_SCRIPTS / "completed-not-committed-sweep.py")
cnc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cnc)


REGISTRY = """# SDLC environments

Some prose that names `Decoy-Repo` without listing it.

## Which repos does the dev-branch target bind on?

| Repo | Note |
|---|---|
| `Decoy-Repo` | **YES** this table is not the registry |

## Split-Repo Registry (a repo is split ONLY when listed here)

| Repo | Split? | Since | Notes |
|---|---|---|---|
| `Alpha-Web-App` | **YES** | 2026-08-28 | dev -> a dev url |
| `Beta-Service` | **YES** | 2026-08-28 | dev -> a dev lane |
| `Gamma-Legacy` | no | | single-branch flow stands |
| (remaining tier — sidecar — lands here as its dev lane goes live) | | | |

## A later heading

| Repo | Split? |
|---|---|
| `Delta-After-Section` | **YES** |
"""


def _write_registry(tmp_path, text=REGISTRY):
    conv = tmp_path / "conventions"
    conv.mkdir(parents=True, exist_ok=True)
    (conv / "sdlc-environments.md").write_text(text, encoding="utf-8")
    return tmp_path


# ── split_repo_names: the registry table is the ONLY test ──────────────────

def test_parses_only_the_registry_table(tmp_path):
    """Exactly the rows the registry marks YES — and nothing else in the file.

    `Decoy-Repo` sits in an EARLIER table that also says YES, and
    `Delta-After-Section` in a LATER one. Both must be excluded: g-370-15 exists
    because the split rule was stated in two places and found in neither, and
    "named somewhere in this file" is explicitly NOT the test."""
    assert cnc.split_repo_names(_write_registry(tmp_path)) == frozenset(
        {"Alpha-Web-App", "Beta-Service"})


def test_a_row_marked_no_is_not_split(tmp_path):
    """`Gamma-Legacy` is listed but not split — presence is not the predicate."""
    assert "Gamma-Legacy" not in cnc.split_repo_names(_write_registry(tmp_path))


def test_placeholder_row_is_not_a_repo_name(tmp_path):
    """The "(remaining tier ...)" row is prose in a table cell, not a repo."""
    names = cnc.split_repo_names(_write_registry(tmp_path))
    assert not any(n.startswith("(") for n in names)


def test_missing_world_path_fails_closed():
    """No world path -> EMPTY, never a guess. Fail-closed is the whole contract:
    an empty set makes resolve_landing_ref fall through to the default branch,
    i.e. the behaviour before this function existed."""
    assert cnc.split_repo_names("/nonexistent/world/xyz") == frozenset()


def test_unreadable_registry_fails_closed(tmp_path):
    """World path present but the convention absent -> EMPTY, not an error.

    A registry we cannot read must never widen what counts as landed."""
    (tmp_path / "conventions").mkdir()
    assert cnc.split_repo_names(tmp_path) == frozenset()


def test_registry_without_the_section_fails_closed(tmp_path):
    """The file exists but carries no registry section -> EMPTY."""
    assert cnc.split_repo_names(
        _write_registry(tmp_path, "# nothing here\n\ntext\n")) == frozenset()


# ── resolve_landing_ref: widen for split repos, hold everywhere else ───────

def _stub_git(monkeypatch, dev_exists):
    """Stub _git so `origin/dev` verification returns a chosen answer, and
    resolve_default_ref's own probes resolve to origin/main."""
    def fake_git(repo, *args, timeout=15):
        if args[:1] == ("symbolic-ref",):
            return (1, "")  # no origin/HEAD — exercise the fallback probe
        if args[:1] == ("rev-parse",):
            target = args[-1]
            if target.startswith("origin/dev"):
                return (0, "") if dev_exists else (1, "")
            return (0, "")  # origin/main verifies
        return (1, "")
    monkeypatch.setattr(cnc, "_git", fake_git)


def test_listed_repo_lands_on_dev(monkeypatch):
    """The fix: a registry-listed repo is judged against `origin/dev`."""
    _stub_git(monkeypatch, dev_exists=True)
    assert cnc.resolve_landing_ref(
        Path("/repos/Alpha-Web-App"), frozenset({"Alpha-Web-App"})) == "origin/dev"


def test_unlisted_repo_still_lands_on_default(monkeypatch):
    """CONTROL: an unlisted repo is untouched by this change."""
    _stub_git(monkeypatch, dev_exists=True)
    assert cnc.resolve_landing_ref(
        Path("/repos/Other-Repo"), frozenset({"Alpha-Web-App"})) == "origin/main"


def test_empty_registry_reproduces_the_old_behaviour(monkeypatch):
    """CONTROL for the fail-closed path: with no registry every repo resolves
    to the default branch, which is exactly the pre-fix behaviour. This is what
    makes an unreadable convention safe rather than silently permissive."""
    _stub_git(monkeypatch, dev_exists=True)
    assert cnc.resolve_landing_ref(
        Path("/repos/Alpha-Web-App"), frozenset()) == "origin/main"


def test_listed_repo_without_a_dev_branch_falls_back(monkeypatch):
    """CONTROL: listed in the registry before its `dev` branch is cut.

    Falls back to the default branch rather than returning a ref that does not
    exist — which probe_sha_on_default would read as undeterminable, making
    every commit in the repo unjudgeable."""
    _stub_git(monkeypatch, dev_exists=False)
    assert cnc.resolve_landing_ref(
        Path("/repos/Alpha-Web-App"), frozenset({"Alpha-Web-App"})) == "origin/main"


def test_trailing_separator_does_not_break_the_name_match(monkeypatch):
    """Repo roots arrive as strings from several call sites; a trailing
    separator must not turn a listed repo into an unlisted one."""
    _stub_git(monkeypatch, dev_exists=True)
    assert cnc.resolve_landing_ref(
        "/repos/Alpha-Web-App/", frozenset({"Alpha-Web-App"})) == "origin/dev"
