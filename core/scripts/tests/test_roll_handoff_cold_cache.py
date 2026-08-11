"""cmd_roll_handoff must not drop handoff keys on a COLD own-cloud cache ().

THE DEFECT. Under own-cloud the local tree is a read-through cache (guard-980), so a
handoff.yaml that exists REMOTELY reads as absent on a cold box. A bare
`Path(hp).is_file()` gate then fell through with `doc = {}`, and the locked write
persisted a document containing ONLY `pending_deploys` -- destroying every other key
the remote file carried. handoff.yaml is cross-session state read at the next boot.

WHY THE SHAPE MATTERS, and what these tests are really pinning: the surrounding code
ALREADY returns-without-writing for a non-dict handoff and for an unparseable one.
Those are the RARE loss paths. The cold-cache path is the COMMON one and was the only
one that reached the write. So the bug was not a missing guard in general -- it was a
guard placed on the two cheap cases and absent from the expensive one. A test that only
exercises a warm cache passes against the defect, which is why every case here controls
the BACKEND rather than the filesystem.

METHOD. `cmd_roll_handoff` does a lazy `from storage_backend import get_backend`, so a
stub module earlier on PYTHONPATH is imported instead of the real one. Each case picks
the stub's behaviour by env var, which lets us reproduce all three backend contracts
exactly: materialize (remote file pulled down), no-op (cold cache that never
materializes -- the PRE-FIX path), and raise (transport error).

The call under test is `refresh()` (force_fresh=True), NOT `ensure_local()`. The stub
deliberately implements ensure_local as a NEVER-materializing no-op, modelling its TTL
early-return, so swapping the production call back to it fails the materialize case
rather than passing on a coincidence.

The no-op / materialize PAIR is the both-directions proof the goal asks for: same
inputs, same code, and the non-pending_deploys key survives iff materialization
happened.

guard-1165: no module-level os.environ mutation. Fully hermetic -- `--handoff` and
`--store` are test overrides, so nothing resolves into a real agent dir.
Run: STORAGE_BACKEND=local python -m pytest \
     core/scripts/tests/test_roll_handoff_cold_cache.py -q
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
TRACKER = SCRIPTS / "pending-deploys.py"

# The stub the script imports instead of the real storage_backend. Behaviour is
# selected by STUB_MODE so one file covers all three backend contracts.
#   materialize -> writes STUB_REMOTE to the path (a warm/pulled cache)
#   noop        -> does nothing (cold cache that never materializes = PRE-FIX)
#   absent      -> does nothing, and the test asserts the fresh-agent path still works
#                  (mirrors _refresh's real 404 contract, identical for both
#                   force_fresh values: RETURNS, does not raise)
#   raise       -> raises (any non-404 ClientError; we must not write)
STUB = '''
import os, pathlib, importlib.util

# RE-EXPORT THE REAL MODULE, OVERRIDE ONLY get_backend. _fileops imports other names
# from storage_backend (LocalBackend, ...), so a stub that defines get_backend alone
# breaks the locked writer with "cannot import name 'LocalBackend'" -- which surfaces
# as a handoff-write error and looks like the code under test failing.
_spec = importlib.util.spec_from_file_location(
    "_real_storage_backend", os.environ["REAL_STORAGE_BACKEND"])
_real = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_real)
for _k in dir(_real):
    if not _k.startswith("__"):
        globals()[_k] = getattr(_real, _k)

# DELEGATE EVERY OTHER METHOD TO A REAL BACKEND. The locked writer calls
# get_backend().acquire_lock(...) among others, so a stub that implements
# ensure_local ALONE fails with "'_B' object has no attribute 'acquire_lock'" --
# again surfacing as a handoff-write error that looks like the code under test.
# STORAGE_BACKEND=local is pinned by the harness, so the inner backend is a
# LocalBackend: real bytes on the tmp filesystem, no network (guard-955).
_inner = _real.get_backend()


class _B:
    def __getattr__(self, name):        # only fires for names NOT defined below
        return getattr(_inner, name)

    def _mode(self):
        return os.environ.get("STUB_MODE", "noop")

    # ensure_local MODELS THE TTL EARLY-RETURN and therefore NEVER materializes.
    # That is not a shortcut -- it is the behavioural half of the pin. ensure_local
    # is _refresh(force_fresh=False) and its TTL early-return is gated on
    # `not force_fresh`, so it can legally answer from a stale/absent local cache
    # without contacting the store. Stubbing it as a no-op means a revert from
    # refresh() to ensure_local() FAILS the materialize case, instead of passing
    # because both happened to be stubbed alike.
    def ensure_local(self, p):
        if self._mode() == "raise":
            raise RuntimeError("simulated transport failure")
        return pathlib.Path(p)

    def refresh(self, p):
        mode = self._mode()
        if mode == "raise":
            raise RuntimeError("simulated transport failure")
        if mode == "materialize":
            pathlib.Path(p).parent.mkdir(parents=True, exist_ok=True)
            pathlib.Path(p).write_text(os.environ["STUB_REMOTE"], encoding="utf-8")
        return None

def get_backend():
    return _B()
'''


def _run(tmp_path, mode, remote=None, seed_local=None):
    """Invoke roll-handoff with the stub backend. Returns (parsed_json, handoff_text)."""
    # STAGE A COPY OF THE REAL SCRIPT BESIDE THE STUB. For `python3 /path/to/x.py`,
    # sys.path[0] is the SCRIPT'S OWN DIRECTORY -- it outranks PYTHONPATH -- so a stub
    # placed only on PYTHONPATH loses to the real core/scripts/storage_backend.py and
    # every case silently exercises the real backend instead. (Measured: with the stub
    # on PYTHONPATH alone, the raise-case never raised and the materialize-case never
    # materialized, so two tests failed while appearing to test the right thing.)
    # Copying the script makes stub_dir its sys.path[0]; PYTHONPATH then supplies the
    # real _fileops/_paths siblings. Same technique as the _runtime.sh stub in
    # test_clear_in_flight_call_site_scoping.py -- real bytes, no network.
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir(exist_ok=True)
    (stub_dir / "storage_backend.py").write_text(STUB, encoding="utf-8")
    tracker = stub_dir / "pending-deploys.py"
    tracker.write_text(TRACKER.read_text(encoding="utf-8"), encoding="utf-8")

    store = tmp_path / "pending-deploys.yaml"
    # _load (pending-deploys.py:157) parses a TOP-LEVEL LIST and returns [] for any
    # other shape -- so a dict-wrapped fixture silently yields zero entries and
    # roll-handoff early-returns rolled:0 before reaching the code under test.
    store.write_text(
        "- repo: acme/widget\n"
        "  sha: abc123\n"
        "  goal_id: g-1-1\n"
        "  dir: /tmp/x\n",
        encoding="utf-8",
    )
    handoff = tmp_path / "handoff.yaml"
    if seed_local is not None:
        handoff.write_text(seed_local, encoding="utf-8")

    env = dict(os.environ)
    # Real siblings (_fileops, _paths) resolve here; storage_backend does NOT, because
    # the staged copy's own directory precedes PYTHONPATH on sys.path.
    env["PYTHONPATH"] = str(SCRIPTS) + os.pathsep + env.get("PYTHONPATH", "")
    env["REAL_STORAGE_BACKEND"] = str(SCRIPTS / "storage_backend.py")
    env["STUB_MODE"] = mode
    env["STORAGE_BACKEND"] = "local"
    if remote is not None:
        env["STUB_REMOTE"] = remote

    # --handoff belongs to the roll-handoff SUBPARSER, not the top level.
    r = subprocess.run(
        [sys.executable, str(tracker), "--store", str(store),
         "roll-handoff", "--handoff", str(handoff)],
        capture_output=True, text=True, env=env, cwd=str(tmp_path),
    )
    assert r.returncode == 0, f"roll-handoff must fail-open (rc 0): {r.stderr}"
    out = json.loads(r.stdout.strip().splitlines()[-1])
    text = handoff.read_text(encoding="utf-8") if handoff.is_file() else ""
    return out, text


REMOTE = (
    "blockers:\n"
    "  - the-key-that-must-survive\n"
    "reasoning_trajectory: carried across the stop boundary\n"
)


def test_cold_cache_materializes_and_preserves_other_keys(tmp_path):
    """POST-FIX direction: the remote file is pulled down, so its keys survive.

    This is the docstring invariant ("preserves every other handoff key")
    demonstrated under a COLD cache rather than a warm one -- the distinction the
    defect lived in.
    """
    out, text = _run(tmp_path, "materialize", remote=REMOTE)
    assert out.get("rolled") == 1, out
    assert "the-key-that-must-survive" in text, (
        "a key present in the remote handoff was dropped despite materialization")
    assert "reasoning_trajectory" in text
    assert "pending_deploys" in text, "the mirror itself must still be written"


def test_cold_cache_without_materialization_drops_keys(tmp_path):
    """PRE-FIX direction: with a no-op backend the loss reproduces exactly.

    Same code, same inputs as the test above -- only the backend differs. That
    isolation is the point: it shows the materialize call is what preserves the
    keys, not some incidental property of the write path. If someone deletes the
    ensure_local call, the test above fails and this one keeps passing, which is
    how the pair localises the regression.
    """
    out, text = _run(tmp_path, "noop", remote=REMOTE)
    assert out.get("rolled") == 1, out
    assert "the-key-that-must-survive" not in text, (
        "expected the documented loss to reproduce with a non-materializing backend; "
        "if this now passes, the write no longer depends on the local read and this "
        "test should be re-derived rather than deleted")
    assert "pending_deploys" in text


def test_unmaterializable_skips_the_write_rather_than_clobbering(tmp_path):
    """A transport error must SKIP, never write.

    ensure_local raises only for non-404 errors; a 404 returns normally. So a raise
    means we cannot distinguish absent-remotely from cannot-reach. Writing under that
    ambiguity is the exact clobber this goal is about. Skipping costs one boot summary
    and self-heals next stop, because pending-deploys.yaml is NOT cleared here and
    remains the single source of truth.
    """
    seeded = "blockers:\n  - pre-existing\n"
    out, text = _run(tmp_path, "raise", seed_local=seeded)
    assert out.get("error") == "handoff-unmaterializable", out
    assert out.get("rolled") == 0, out
    assert text == seeded, "the handoff was modified despite an unreadable backend"


def test_genuinely_absent_handoff_still_rolls(tmp_path):
    """Non-regression: a fresh agent's first stop must still work.

    The real ensure_local RETURNS on 404 and leaves local absent, so a handoff that
    genuinely does not exist anywhere must still take the empty-doc path and write the
    mirror. A fix that skipped on every absent file would silently break first-stop for
    every new agent -- a fail-safe direction chosen too aggressively.
    """
    out, text = _run(tmp_path, "absent")
    assert out.get("rolled") == 1, out
    assert "pending_deploys" in text
    assert "acme/widget" in text


def test_site_still_materializes_before_the_presence_test(tmp_path):
    """Source pin on ORDER, which the behavioural tests above cannot see.

    The materializing call must run BEFORE `Path(hp).is_file()`. Reversed, every
    case above still passes on a warm cache and the cold-cache defect returns
    intact.

    It must also be refresh(), not ensure_local() -- the latter is
    _refresh(force_fresh=False) whose TTL early-return can answer from a stale
    cache without contacting the store (guard-980), and handoff.yaml has no merge
    handler, so nothing below the write would reconcile the lost key. The stub
    enforces that behaviourally too; this pin makes the intent legible at the
    source.
    """
    src = TRACKER.read_text(encoding="utf-8")
    body = src.split("def cmd_roll_handoff", 1)[1]
    # ANCHOR TO NON-COMMENT LINES (guard-1099). The fix's own explanatory comment
    # QUOTES `Path(hp).is_file()` while describing the defect, and that comment sits
    # ABOVE the ensure_local call -- so an unanchored find() matches the prose, reads
    # the presence test as earlier than the materialize, and fails against correct
    # code. Measured: this test did exactly that on its first run. The better the
    # code is commented, the more reliably an unanchored source pin misfires.
    code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
    materialize = code.find(".refresh(")
    presence = code.find("Path(hp).is_file()")
    assert materialize != -1, (
        "cmd_roll_handoff no longer calls backend.refresh(); if this was swapped for "
        "ensure_local(), that is the TTL-gated call and reintroduces the stale-read "
        "half of this defect (guard-980)")
    assert presence != -1, "the presence test moved; re-derive this pin"
    assert materialize < presence, (
        "refresh() must precede the is_file() gate, or the cold-cache read still "
        "misses and the keys are dropped again")
