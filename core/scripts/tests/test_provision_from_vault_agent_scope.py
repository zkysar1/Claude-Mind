"""Acceptance tests for the per-agent scope contract in
core/scripts/provision-from-vault.sh (g-335-239).

Hermetic — no SSH, no network. One test seam: `ssh` is stubbed onto PATH and
emits the fixture vault. Unlike the sibling
(test_provision_github_from_vault.py, which needs a VAULT_FILE env seam
because it must also fake the GitHub API), PATH-stubbing needs NO production
change here and keeps the real `ssh ...` invocation line under test.

The property under test is SECURITY-CRITICAL, which is why it is a durable
regression test rather than a one-off harness: a vault entry scoped to one
agent must NEVER be provisioned onto another agent's box. The pre-g-335-239
mapping loop had no scope awareness and wrote every scoped entry to every box
as a literal container key — running as zeta it produced
LODESTAR_CONTRIBUTE_KEY__BRAVO in zeta's .env.local, silently (nothing errors
and the verify block reports all keys OK).

Scope rules covered:
  - `<PREFIX>_<BASE>__<AGENT>` resolves to `<BASE>` on that agent's box only
  - a scoped entry beats a generic sibling regardless of vault line order
  - another agent's scoped entry is absent (value AND `__SUFFIX` key name)
  - MIND_AGENT unset -> never guess; generic only
  - a vault with no scoped entries maps exactly as it did before
  - verify output stays values-blind (no fixture value in stderr)
"""
import os
import shutil
import subprocess
import pytest

from pathlib import Path

import sys
import pathlib
# guard-580: resolve bash explicitly — a bare 'bash' argv[0] hits System32 WSL on win32.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _bash_helpers import BASH  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "core" / "scripts" / "provision-from-vault.sh"

# Obviously-fake placeholders. Deliberately not AKIA-/token-shaped so the
# commit-time secret scan (iteration-commit.sh content_secret_regex) does not
# false-positive. The mapper treats values as opaque, so any distinctive
# string exercises the leak checks identically.
VAULT_BODY = """# comment line ignored
MIND_MIND_AWS_ACCESS_KEY_ID=GENERIC_AKID
MIND_STORAGE_BACKEND=own-cloud
MIND_LODESTAR_CONTRIBUTE_KEY__BRAVO=SCOPED_FOR_BRAVO
MIND_LODESTAR_CONTRIBUTE_KEY__ZETA=SCOPED_FOR_ZETA
MIND_LODESTAR_CONTRIBUTE_KEY=GENERIC_FALLBACK
MIND_OVERRIDE_ME=GENERIC_LOSES
MIND_OVERRIDE_ME__BRAVO=SCOPED_WINS
OTHERENV_MIND_AWS_ACCESS_KEY_ID=WRONG_ENV
"""
# Line order is load-bearing for the precedence tests: LODESTAR's generic sits
# AFTER its scoped sibling, OVERRIDE_ME's generic sits BEFORE. A correct
# two-pass implementation gives the same answer for both.

ALL_VALUES = [
    "GENERIC_AKID", "SCOPED_FOR_BRAVO", "SCOPED_FOR_ZETA",
    "GENERIC_FALLBACK", "SCOPED_WINS", "GENERIC_LOSES",
]


DEFAULT_ROSTER = ("alpha", "bravo", "echo", "foxtrot", "zeta")


def _run(tmp_path, *, agent, vault_body=VAULT_BODY, name="env",
         roster=DEFAULT_ROSTER):
    """Run the real script with `ssh` stubbed. Always writes to a tmp --out:
    the real .env.local is never a target.

    `roster` pins MIND_AGENTS_ROOT to a fixture tree (g-335-254) so scope
    classification never depends on which agent dirs happen to exist in the
    live repo. Without this the scope assertions silently track real directory
    contents and break when the fleet roster changes — an agent HAS been
    retired before (rb-2859). Pinned explicitly rather than inherited from the
    ambient environment, per rb-3208. Pass roster=() for the no-roster path."""
    vault = tmp_path / f"vault.{name}"
    vault.write_text(vault_body, encoding="utf-8")
    bindir = tmp_path / f"bin.{name}"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "ssh"
    stub.write_text(f'#!/usr/bin/env bash\ncat "{vault}"\n', encoding="utf-8")
    stub.chmod(0o755)
    bootstrap = tmp_path / "bootstrap.pem"
    bootstrap.touch()  # FROM-state guard only checks presence

    out = tmp_path / name
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    # PATH-prepend alone DOES NOT stub ssh on Git Bash (). MSYS bash
    # prepends its own /mingw64/bin:/usr/bin ahead of the inherited Windows PATH
    # at startup, so this bindir lands at PATH position 4 and `command -v ssh`
    # still resolves to /usr/bin/ssh. Real OpenSSH then ran and failed with
    # "Could not resolve hostname stub.invalid" — which looks like a product bug
    # and is not one. VAULT_SSH_BIN is the explicit seam; the PATH prepend is
    # kept because it is what makes the stub work on POSIX.
    env["VAULT_SSH_BIN"] = str(stub)
    env["BOOTSTRAP_KEY_PATH"] = str(bootstrap)
    env["VAULT_SSH_HOST"] = "stub.invalid"
    env["VAULT_REMOTE_PATH"] = "/stub/vault"
    # PIN the root input (keeps the derivation itself under test), then CLEAR
    # every override the script honors further DOWN the chain:
    #     ENVIRONMENT_ID -> VAULT_KEY_PREFIX -> prefix filter (:189)
    # Both later links are `: "${VAR:=default}"` / `if [ -z "${VAR:-}" ]` forms,
    # so an ambient value wins outright and the pin above is never consulted —
    # pinning only the first link defends against none of the others.
    # guard-1484: clear the value when the run must MEASURE resolution rather
    # than dictate it. Measured before this fix (): baseline 16 passed,
    # VAULT_KEY_PREFIX=BOGUS 13 FAILED / 3 passed.
    env["ENVIRONMENT_ID"] = "ayoai-mind"
    env.pop("VAULT_KEY_PREFIX", None)
    # VAULT_SSH_USER (:106, used at :145/:148) is the one remaining override this
    # test does not pin. It is inert HERE only because the VAULT_SSH_BIN stub above
    # ignores its argv — not because the script ignores it. Cleared so hermeticity
    # rests on the script's contract rather than on the stub's current shape.
    env.pop("VAULT_SSH_USER", None)
    roster_root = tmp_path / f"agents.{name}"
    roster_root.mkdir(exist_ok=True)
    for a in roster:
        (roster_root / a).mkdir(exist_ok=True)
    env["MIND_AGENTS_ROOT"] = str(roster_root)
    if agent is None:
        env.pop("MIND_AGENT", None)
    else:
        env["MIND_AGENT"] = agent

    proc = subprocess.run(
        [BASH, str(SCRIPT), "--out", str(out)],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
    )
    body = out.read_text(encoding="utf-8") if out.exists() else ""
    return proc, out, body


def _val(body, key):
    for line in body.splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1]
    return None


pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash required"
)


def test_scoped_entry_resolves_to_base_name_for_this_agent(tmp_path):
    _, _, body = _run(tmp_path, agent="bravo")
    assert _val(body, "LODESTAR_CONTRIBUTE_KEY") == "SCOPED_FOR_BRAVO"
    # generic + non-secret config still pass through untouched
    assert _val(body, "MIND_AWS_ACCESS_KEY_ID") == "GENERIC_AKID"
    assert _val(body, "STORAGE_BACKEND") == "own-cloud"
    # a foreign env-prefix is never provisioned
    assert "WRONG_ENV" not in body


def test_scoped_beats_generic_regardless_of_line_order(tmp_path):
    _, _, body = _run(tmp_path, agent="bravo")
    # generic AFTER scoped
    assert _val(body, "LODESTAR_CONTRIBUTE_KEY") == "SCOPED_FOR_BRAVO"
    # generic BEFORE scoped -- same answer, so precedence is not order-derived
    assert _val(body, "OVERRIDE_ME") == "SCOPED_WINS"


def test_other_agents_scoped_entry_never_lands(tmp_path):
    """The security-critical property. Pre-fix this failed both ways."""
    _, _, body = _run(tmp_path, agent="bravo")
    assert "SCOPED_FOR_ZETA" not in body, "another agent's scoped value leaked"
    assert "LODESTAR_CONTRIBUTE_KEY__" not in body, "raw __SUFFIX key written"


def test_each_agent_gets_only_its_own_scoped_value(tmp_path):
    _, _, bravo = _run(tmp_path, agent="bravo", name="env.bravo")
    _, _, zeta = _run(tmp_path, agent="zeta", name="env.zeta")
    assert _val(bravo, "LODESTAR_CONTRIBUTE_KEY") == "SCOPED_FOR_BRAVO"
    assert _val(zeta, "LODESTAR_CONTRIBUTE_KEY") == "SCOPED_FOR_ZETA"
    assert "SCOPED_FOR_BRAVO" not in zeta
    assert "SCOPED_FOR_ZETA" not in bravo
    # zeta has no OVERRIDE_ME scope -> falls back to the generic
    assert _val(zeta, "OVERRIDE_ME") == "GENERIC_LOSES"


def test_unset_agent_never_guesses_a_scope(tmp_path):
    _, _, body = _run(tmp_path, agent=None)
    assert _val(body, "MIND_AWS_ACCESS_KEY_ID") == "GENERIC_AKID"
    assert _val(body, "LODESTAR_CONTRIBUTE_KEY") == "GENERIC_FALLBACK"
    for v in ("SCOPED_FOR_BRAVO", "SCOPED_FOR_ZETA", "SCOPED_WINS"):
        assert v not in body, f"scoped value {v} resolved with no agent bound"


def test_vault_without_scoped_entries_maps_as_before(tmp_path):
    """Backward compatibility: the two-pass mapper is a no-op on old vaults."""
    plain = "\n".join(
        ln for ln in VAULT_BODY.splitlines() if "__" not in ln
    ) + "\n"
    proc, _, body = _run(tmp_path, agent="bravo", vault_body=plain)
    assert _val(body, "MIND_AWS_ACCESS_KEY_ID") == "GENERIC_AKID"
    assert _val(body, "LODESTAR_CONTRIBUTE_KEY") == "GENERIC_FALLBACK"
    assert _val(body, "OVERRIDE_ME") == "GENERIC_LOSES"
    assert "0 agent-scoped" in proc.stderr


def test_verify_output_is_values_blind(tmp_path):
    proc, out, _ = _run(tmp_path, agent="bravo")
    combined = proc.stdout + proc.stderr
    for v in ALL_VALUES:
        assert v not in combined, f"value {v} appeared in provisioner output"
    assert "agent-scoped: BRAVO" in proc.stderr, "scope tag missing from verify"
    # The 0600 guarantee is real and stays enforced on POSIX, where the fleet runs.
    # It is NOT ASSERTABLE on Windows: NTFS has no POSIX mode bits, so os.stat
    # synthesizes st_mode and reports 0o666 for any writable file regardless of what
    # chmod did. Asserting it there measures the platform, not the script — it can
    # never pass and never catches a real regression. Skipping is therefore strictly
    # more informative than a guaranteed red ().
    if os.name != "nt":
        assert oct(out.stat().st_mode)[-3:] == "600"


# ── : pass-2 scope-skip reporting ──────────────────────────────────────
# Dropping a foreign-scoped key is correct — that IS the security rule tested
# above. Dropping it SILENTLY is not: a dropped key is absent from the written
# env, absent from the verify listing, and absent from the counts, so the
# operator sees a clean run and a missing value with nothing connecting the two.
# The two skip reasons must stay distinguishable, because warning on the
# expected one would train operators to ignore the warning entirely.

UNKNOWN_SCOPE_BODY = """MIND_MIND_AWS_ACCESS_KEY_ID=GENERIC_AKID
MIND_SOME__CONFIG=STRAY_ONE
MIND_OTHER__TYPO__THING=STRAY_TWO
MIND_LODESTAR_CONTRIBUTE_KEY__BRAVO=SCOPED_FOR_BRAVO
"""


def test_unknown_scope_suffix_is_surfaced_by_name(tmp_path):
    """A key using the reserved `__` whose suffix matches no agent known here
    is the one worth surfacing — whoever trips a reserved separator is by
    definition someone who did not know the contract."""
    proc, _, body = _run(tmp_path, agent="zeta", vault_body=UNKNOWN_SCOPE_BODY)
    assert "NOTICE" in proc.stderr
    assert "SOME__CONFIG" in proc.stderr
    assert "OTHER__TYPO__THING" in proc.stderr
    # Reporting must not change WHAT gets written — these are still dropped.
    assert "SOME__CONFIG" not in body
    assert "OTHER__TYPO__THING" not in body


def test_known_agent_scope_is_counted_quietly_not_surfaced(tmp_path):
    """Another agent's credential is an EXPECTED skip: counted, never flagged."""
    proc, _, _ = _run(tmp_path, agent="zeta", vault_body=UNKNOWN_SCOPE_BODY)
    assert "1 key(s) scoped to another agent" in proc.stderr
    tail = proc.stderr.split("NOTICE", 1)[1] if "NOTICE" in proc.stderr else ""
    assert "LODESTAR_CONTRIBUTE_KEY__BRAVO" not in tail


def test_roster_is_injectable_and_changes_classification(tmp_path):
    """The hermeticity proof (): the SAME vault classifies differently
    under two different pinned rosters. If this passes, the scope assertions are
    reading the injected roster and not the live agents/ dir."""
    with_bravo, _, _ = _run(tmp_path, agent="zeta", vault_body=UNKNOWN_SCOPE_BODY,
                            name="withbravo", roster=("bravo", "zeta"))
    without, _, _ = _run(tmp_path, agent="zeta", vault_body=UNKNOWN_SCOPE_BODY,
                         name="nobravo", roster=("zeta",))
    assert "1 key(s) scoped to another agent" in with_bravo.stderr
    assert "scoped to another agent" not in without.stderr
    # Dropping is identical either way — only the LABEL moves.
    assert "LODESTAR_CONTRIBUTE_KEY__BRAVO" in without.stderr


def test_absent_fleet_agent_is_not_accused_of_a_contract_violation(tmp_path):
    """Finding 1 (): a real agent whose dir has not landed on this box
    must not be reported as a reserved-separator misuse. The message has to
    offer the 'suffix names a real agent absent here' reading, and name the
    roster it actually checked, or the notice over-claims."""
    proc, _, _ = _run(tmp_path, agent="zeta", vault_body=UNKNOWN_SCOPE_BODY,
                      name="partial", roster=("zeta",))
    assert "not matching any agent known on this box" in proc.stderr
    assert "not present on this box" in proc.stderr
    assert "Roster checked:" in proc.stderr


def test_empty_roster_reports_inability_to_classify(tmp_path):
    """With no roster at all, say so — do not accuse every scoped key."""
    proc, _, _ = _run(tmp_path, agent="zeta", vault_body=UNKNOWN_SCOPE_BODY,
                      name="noroster", roster=())
    assert "could not classify" in proc.stderr
    assert "NOTICE" not in proc.stderr


def test_own_scoped_entry_is_not_counted_as_foreign(tmp_path):
    """Pass 2 re-scans the WHOLE vault, so this agent's own scoped entries
    reappear after pass 1 already resolved them. They are not skips and must
    not inflate the foreign count (which would read as a false leak signal)."""
    proc, _, _ = _run(tmp_path, agent="bravo")
    # VAULT_BODY scopes: __BRAVO x2 (ours), __ZETA x1 (foreign) -> exactly 1.
    assert "1 key(s) scoped to another agent" in proc.stderr


def test_unset_agent_explains_why_every_scope_was_skipped(tmp_path):
    """MIND_AGENT unset is fail-safe, but silently yielding only generics is
    the same invisible-drop problem — say why."""
    proc, _, _ = _run(tmp_path, agent=None)
    assert "MIND_AGENT is unset" in proc.stderr


def test_no_scope_report_when_vault_has_no_scoped_entries(tmp_path):
    """Backward compatibility: an old vault emits neither new line."""
    plain = "\n".join(ln for ln in VAULT_BODY.splitlines() if "__" not in ln) + "\n"
    proc, _, _ = _run(tmp_path, agent="bravo", vault_body=plain)
    assert "scoped to another agent" not in proc.stderr
    assert "WARNING" not in proc.stderr


def test_scope_skip_report_is_values_blind(tmp_path):
    """The new reporting prints key NAMES only — invariant 5 holds for it too."""
    proc, _, _ = _run(tmp_path, agent="zeta", vault_body=UNKNOWN_SCOPE_BODY)
    combined = proc.stdout + proc.stderr
    for v in ("STRAY_ONE", "STRAY_TWO", "SCOPED_FOR_BRAVO", "GENERIC_AKID"):
        assert v not in combined, f"value {v} leaked into the scope-skip report"
