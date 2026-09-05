"""MIND_API_TOKEN delivery to LOCAL daemon clients ().

Once MIND_API_TOKEN is set on the daemon, FR-4 (mind_api/src/server.py) demands a
matching `Authorization: Bearer` on EVERY request -- LOCAL callers included. The
ambient environment of an ad-hoc Bash tool call carries no such variable, and the
one surface that reaches every tool call (`.claude/settings.json` env) is
GIT-TRACKED and must never hold a credential (guard-724). So both daemon clients
fall back to reading the token from the gitignored, mode-600 `.env.local`.

The two clients are TWINS and must not drift: `_rt.py::_api_token` (python) and
`_runtime.sh::_rt_api_token` (bash). Every case below is asserted against BOTH,
from ONE table, so a fix applied to one and not the other fails here.

guard-920 / rb-9476: the bash half is exercised by SOURCING _runtime.sh and
calling the real function -- the literal production shape -- never by
re-implementing its sed in the test.
"""
from __future__ import annotations

import importlib
import os
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "core" / "scripts"))

from _runtime_bash import BASH  # noqa: E402  guard-580: never a bare "bash" argv[0]

# (description, .env.local content or None, ambient MIND_API_TOKEN, expected)
CASES = [
    ("absent-file-and-env", None, "", ""),
    ("bare-value", "MIND_API_TOKEN=fake-tok-123", "", "fake-tok-123"),
    ("double-quoted", 'MIND_API_TOKEN="fake-tok-dq"', "", "fake-tok-dq"),
    ("single-quoted", "MIND_API_TOKEN='fake-tok-sq'", "", "fake-tok-sq"),
    ("spaces-around-equals", "MIND_API_TOKEN =  fake-spaced", "", "fake-spaced"),
    ("ambient-env-wins", "MIND_API_TOKEN=from-file", "from-env", "from-env"),
    ("other-key-only", "OTHER_KEY=zzz", "", ""),
    # The two negative controls that make the parser's zero trustworthy: a key
    # that merely CONTAINS the name, and a commented-out one. A substring match
    # would hand a WRONG token to the daemon and 401 every store call on the box.
    ("substring-key-must-not-match", "XMIND_API_TOKEN=nope", "", ""),
    ("commented-key-must-not-match", "# MIND_API_TOKEN=commented", "", ""),
    ("crlf-line-ending", "MIND_API_TOKEN=crlf-tok\r", "", "crlf-tok"),
]

_IDS = [c[0] for c in CASES]


def _write_env_local(tmp_path, content):
    if content is not None:
        (tmp_path / ".env.local").write_text(content + "\n", encoding="utf-8")


@pytest.mark.parametrize("desc,content,envval,expected", CASES, ids=_IDS)
def test_python_client_resolves_token(tmp_path, monkeypatch, desc, content, envval, expected):
    _write_env_local(tmp_path, content)
    import _rt
    importlib.reload(_rt)  # drop the module-global file cache between cases
    monkeypatch.setattr(_rt, "_PROJECT_ROOT", tmp_path)
    if envval:
        monkeypatch.setenv("MIND_API_TOKEN", envval)
    else:
        monkeypatch.delenv("MIND_API_TOKEN", raising=False)
    assert _rt._api_token() == expected


@pytest.mark.parametrize("desc,content,envval,expected", CASES, ids=_IDS)
def test_bash_client_resolves_token(tmp_path, desc, content, envval, expected):
    _write_env_local(tmp_path, content)
    # PROJECT_ROOT is assigned AFTER the source on purpose: _runtime.sh resolves
    # PROJECT_ROOT itself when sourced, so a pre-set value is overwritten. The
    # function reads ${PROJECT_ROOT} at CALL time, which is the seam under test.
    script = (
        'source "%s/core/scripts/_runtime.sh" >/dev/null 2>&1 || true\n'
        'PROJECT_ROOT="%s"\n'
        "_rt_api_token\n" % (REPO, tmp_path)
    )
    env = dict(os.environ)
    env.pop("MIND_API_TOKEN", None)
    if envval:
        env["MIND_API_TOKEN"] = envval
    out = subprocess.run(
        [BASH, "-c", script], capture_output=True, text=True, env=env, cwd=str(REPO)
    )
    assert out.stdout == expected, (desc, out.stdout, out.stderr[-400:])


def test_python_caches_the_negative_result(tmp_path, monkeypatch):
    """The no-token case must cost ONE failed open per process, not one per call.

    Every store read/write on the box goes through rt_call and no box has a token
    configured today, so an uncached miss would put a filesystem open on the hot
    path of the entire framework.
    """
    import _rt
    importlib.reload(_rt)
    monkeypatch.setattr(_rt, "_PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("MIND_API_TOKEN", raising=False)
    assert _rt._api_token() == ""
    (tmp_path / ".env.local").write_text("MIND_API_TOKEN=appeared-later\n", encoding="utf-8")
    assert _rt._api_token() == "", "negative result not cached — file re-read on every call"


def test_env_local_is_gitignored_and_untracked():
    """The whole design rests on .env.local never entering git (guard-724)."""
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", ".env.local"], cwd=str(REPO), capture_output=True
    )
    assert ignored.returncode == 0, ".env.local is NOT gitignored — the token would be committable"
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env.local"],
        cwd=str(REPO), capture_output=True, text=True,
    )
    assert tracked.returncode != 0, ".env.local is TRACKED by git"
