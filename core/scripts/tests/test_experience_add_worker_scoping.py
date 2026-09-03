"""A worker Body may write the experience record of a goal IT HOLDS — and nothing else.

g-306-418. `experience-add.sh` used to carry a BLANKET g-306-125 refusal: any
worker Body was skipped outright, with `exit 0`. Two things were wrong with it.

  * It refused a write that is safe. An experience record is the raw TRACE of one
    goal's execution — the INPUT to reflection, not a learned artifact — and the
    store is append-only with locked appends. N workers appending the trace of the
    goal THEY executed do not become N encoders. (Tree / reasoning-bank / guardrail
    / journal writes stay refused; that half of the invariant is untouched, and
    `test-g3-worker-store-rails.sh` still pins it for the sibling writers.)
  * It exited 0 while refusing, so a caller could not tell a skip from a write.
    That is a measured defect class on the journal sibling (g-306-252) and the
    shape guard-5596 warns about. The gate now exits 3.

WHY THE STUB ROOT. The unit under test is the GATE, not the daemon. The gate runs
before any store write, so the test builds a project root holding the REAL
`experience-add.sh` and the REAL `experience.py` (whose `derive_goal_id_from_id`
the gate imports) beside a stubbed `_runtime.sh` and a stubbed
`aspirations-query.sh`. That makes the claim answer an INPUT rather than fixture
state, so every branch — including "the claim is unreadable" — is reachable and
fast. Nothing here touches a governed store: the per-session file the predicate
tests is an empty fixture inside pytest's tmp_path, created with touch(), exactly
as the sibling shell test creates it.

THE CASE THAT MATTERS MOST is `test_id_embedded_goal_is_scoped`. `goal_id` is an
OPTIONAL field and is routinely null on caller-formed records; the store backfills
it from an `exp-{goal-id}-{slug}` id (g-115-1917). A gate testing the FIELD alone
would refuse most real worker records as "unscoped" — present, correct-looking,
and inert on the population it governs (rb-9476).

DISCRIMINATION (rb-5828 / guard-1943): `test_neutralising_the_gate_reddens` rebuilds
the historical blanket skip FROM THE SCRIPT'S OWN PREDICATE LINE and asserts the
held-goal write stops landing — so this file is proven to redden, not assumed to.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_helpers import BASH  # noqa: E402

AGENT = "zeta"
SID = "SID-G306418"
BODY_WM_BASENAME = "working-" + "memory.yaml"   # the fixture the predicate tests

RUNTIME_STUB = """#!/usr/bin/env bash
# Stub of _runtime.sh: the gate needs only the launcher; a passing gate reaches
# rt_call, which prints a marker instead of writing anything.
rt_python_launcher() { echo "%(py)s"; }
rt_call() { echo '{"record":{"stub":"WROTE"}}'; return 0; }
rt_try_autospawn() { return 1; }
rt_no_daemon_error() { echo "no daemon: $1" >&2; exit 1; }
""" % {"py": Path(sys.executable).as_posix()}

QUERY_STUB = """#!/usr/bin/env bash
# Stub of aspirations-query.sh. Emits whatever CLAIM_FIXTURE holds, so the claim
# answer is an INPUT: a JSON array, or empty output meaning "unreadable".
printf '%s' "${CLAIM_FIXTURE:-}"
"""



def _real_derivation_source():
    """Extract the REAL derivation (regex + function) from experience.py.

    Never a hand-copy — the `test-g3-worker-store-rails.sh` pattern. Importing the
    module whole would drag in _stdio / _paths / _path_helpers and a GITIGNORED
    local-paths.conf, which a tmp root cannot supply; extracting keeps the stub
    root hermetic while still exercising the production regex, so a change to
    GOAL_ID_IN_EXP_ID_RE moves this test.
    """
    src = (SCRIPTS / "experience.py").read_text(encoding="utf-8")
    rx = re.search(r"^GOAL_ID_IN_EXP_ID_RE = .*$", src, re.M)
    fn = re.search(r"^def derive_goal_id_from_id\(rec_id\):.*?(?=^\S)", src, re.M | re.S)
    assert rx and fn, "could not extract the derivation from experience.py"
    return "import re\n" + rx.group(0) + "\n\n" + fn.group(0)


@pytest.fixture()
def root(tmp_path):
    """A project root holding the REAL gate beside stubbed siblings."""
    scripts = tmp_path / "core" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(SCRIPTS / "experience-add.sh", scripts / "experience-add.sh")
    (scripts / "experience.py").write_text(_real_derivation_source(), encoding="utf-8")
    (scripts / "_runtime.sh").write_text(RUNTIME_STUB, encoding="utf-8")
    (scripts / "aspirations-query.sh").write_text(QUERY_STUB, encoding="utf-8")
    (tmp_path / "agents" / AGENT / "sessions" / SID).mkdir(parents=True)
    return tmp_path


def run(root, record, *, sid=SID, agent=AGENT, worker=True, claim=None):
    """Invoke the gate. `worker` toggles the per-session fixture that IS the predicate."""
    marker = root / "agents" / agent / "sessions" / SID / BODY_WM_BASENAME
    if worker:
        marker.touch()
    elif marker.exists():
        marker.unlink()
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": "/tmp",
        "STORAGE_BACKEND": "local",
        "MIND_AGENT": agent,
        "CLAIM_FIXTURE": claim or "",
    }
    if sid is not None:
        env["MIND_SID"] = sid
    return subprocess.run(
        [BASH, (root / "core" / "scripts" / "experience-add.sh").as_posix()],
        cwd=str(root), input=record, capture_output=True, text=True, timeout=60, env=env,
    )


def held(sid=SID):
    return '[{"id":"g-306-418","claimed_by_sid":"%s"}]' % sid


REQ = '"type":"execution","category":"framework-architecture","summary":"s","content_path":"x.md"'
SCOPED_BY_FIELD = '{"id":"exp-anything-at-all","goal_id":"g-306-418",%s}' % REQ
SCOPED_BY_ID = '{"id":"exp-g-306-418-worker-loop",%s}' % REQ
UNSCOPED = '{"id":"exp-idle-playbook-churn-20260720",%s}' % REQ


# ---------------------------------------------------------------- accepts ----

def test_goal_id_field_is_scoped(root):
    r = run(root, SCOPED_BY_FIELD, claim=held())
    assert r.returncode == 0, r.stderr
    assert "WROTE" in r.stdout, r.stdout


def test_id_embedded_goal_is_scoped(root):
    """goal_id null but the id embeds the goal — the population rb-9476 warns about.

    This separates a working gate from an inert one: caller-formed records routinely
    omit goal_id and the store backfills it from the id (g-115-1917).
    """
    assert '"goal_id"' not in SCOPED_BY_ID
    r = run(root, SCOPED_BY_ID, claim=held())
    assert r.returncode == 0, r.stderr
    assert "WROTE" in r.stdout, r.stdout


# ---------------------------------------------------------------- refuses ----

def test_unscoped_worker_write_is_refused_rc3(root):
    r = run(root, UNSCOPED, claim=held())
    assert r.returncode == 3, (r.returncode, r.stderr)
    assert "WROTE" not in r.stdout
    assert "names no goal" in r.stderr


def test_goal_held_by_another_body_is_refused_rc3(root):
    """claimed_by_sid keys the check, never claimed_by — another SESSION of the
    same agent can hold a claim (guard-1460)."""
    r = run(root, SCOPED_BY_FIELD, claim=held("some-other-body-sid"))
    assert r.returncode == 3, (r.returncode, r.stderr)
    assert "WROTE" not in r.stdout
    assert "not held by this Body" in r.stderr


def test_unreadable_claim_refuses_rather_than_allows(root):
    """Refuse on unverifiability (reducer-promotion precedent): a claim that
    cannot be READ is not one that may be ASSUMED."""
    r = run(root, SCOPED_BY_FIELD, claim="")
    assert r.returncode == 3, (r.returncode, r.stderr)
    assert "WROTE" not in r.stdout


def test_refusal_is_not_exit_zero(root):
    """The point of rc 3: a caller must tell a skip from a write. An exit-0
    refusal is the g-306-252 / guard-5596 defect class."""
    assert run(root, UNSCOPED, claim=held()).returncode != 0


# ------------------------------------------------------------- unaffected ----

def test_reducer_is_unaffected(root):
    r = run(root, UNSCOPED, worker=False)
    assert r.returncode == 0, r.stderr
    assert "WROTE" in r.stdout


def test_unset_sid_fails_open_to_reducer(root):
    """An unset MIND_SID must never classify a reducer as a worker."""
    r = run(root, UNSCOPED, sid=None)
    assert r.returncode == 0, r.stderr
    assert "WROTE" in r.stdout


def test_predicate_does_not_key_on_body_role():
    """BODY_ROLE is present in Bash-tool context and inert everywhere else, so the
    predicate must derive the role from the per-session file (guard-2445).

    Asserted over CODE only: the gate's own commentary cites guard-2445 by name, so
    a whole-text grep would fail on the very comment that documents the rule.
    """
    gate = (SCRIPTS / "experience-add.sh").read_text(encoding="utf-8").split("g-306-418", 1)[1]
    code = "\n".join(l for l in gate.splitlines() if not l.lstrip().startswith("#"))
    assert BODY_WM_BASENAME in code, "predicate does not test the per-session file"
    assert "BODY_ROLE" not in code, "predicate keys on BODY_ROLE (guard-2445)"


# ---------------------------------------------------------- discrimination ----

def test_neutralising_the_gate_reddens(root):
    """Rebuild the historical blanket skip and assert the held write STOPS landing.

    The predicate line is taken from the script itself, so this test cannot drift
    from the real one. Without this arm, every assertion above could pass against
    a gate that never runs at all.
    """
    path = root / "core" / "scripts" / "experience-add.sh"
    src = path.read_text(encoding="utf-8")
    head, sep, tail = src.partition('if [ -n "${MIND_SID:-}" ]')
    assert sep, "could not locate the worker predicate to neutralise"
    predicate = (sep + tail).split("then", 1)[0] + "then\n"
    path.write_text(head + predicate + '    exit 0\nfi\n', encoding="utf-8")
    r = run(root, SCOPED_BY_FIELD, claim=held())
    assert "WROTE" not in r.stdout, "blanket rail restored but the write still landed"
