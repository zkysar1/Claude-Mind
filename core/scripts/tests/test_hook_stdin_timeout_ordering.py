#!/usr/bin/env python3
"""hook_helpers._read_stdin_bounded: ordering + wiring pins (guard-1737).

The bounded stdin read exists so a hook whose stdin never reaches EOF degrades
to approve-with-no-mutation instead of hanging every Bash call (guard-664).
Its bound is a SHARED constant: 41 hook entries in .claude/settings.json import
hook_helpers, and each is granted its own timeout by the harness. That makes it
exactly the shape guard-1737 governs -- "a shared constant can invert which
limit fires first, changing the reported CAUSE while nothing goes red."

The inversion here is not hypothetical; it happened. The bound shipped at 5s
while one bash-agent-inject.sh run was measured at 5073ms wall time, so the
bound sat inside the observed spread. When it tripped, the hook approved
WITHOUT injecting MIND_AGENT and a live goal-selector.sh run died with
"MIND_AGENT not set" -- while the reader was pointed at
core/logs/bash-inject-misses.jsonl, which that path never writes.

guard-1737 asks for three things, and this file supplies each as its own test
so a single mutation can prove which assertion is load-bearing (step 3b):

  1. ORDERING, with a strict `<` (step 3) -- test_bound_strictly_under_min_grant
  2. The two source literals agreeing        -- test_source_literals_agree
  3. WIRING: the constant reaches the read   -- test_env_override_reaches_read

Test 1 alone is blind to a call-site swap and test 3 alone is blind to an
inverted value, which is why both are here rather than one combined check.
"""

import json
import os
import re
import subprocess
import sys
import textwrap

import pytest

# This file lives at core/scripts/tests/, so the repo root is FOUR levels up.
# (hook_helpers.py sits one level shallower, at core/scripts/, and correctly
# uses three -- do not copy either count to the other file.)
REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
SCRIPTS = os.path.join(REPO_ROOT, "core", "scripts")
HELPERS = os.path.join(SCRIPTS, "hook_helpers.py")
SETTINGS = os.path.join(REPO_ROOT, ".claude", "settings.json")


def _helpers_source():
    with open(HELPERS, encoding="utf-8") as fh:
        return fh.read()


def _default_bound():
    """The default the env-var lookup falls back to, read from source.

    Read from SOURCE rather than by importing and calling: importing
    hook_helpers runs reconfigure_stdio() against the pytest-captured streams,
    and the value we want to pin is the literal a maintainer edits.
    """
    src = _helpers_source()
    m = re.search(r'os\.environ\.get\(\s*"HOOK_STDIN_TIMEOUT_S"\s*,\s*"([0-9.]+)"\s*\)', src)
    assert m, "could not find the HOOK_STDIN_TIMEOUT_S env lookup in hook_helpers.py"
    return float(m.group(1))


def _except_fallback_bound():
    """The second literal -- the `except Exception:` fallback beside it."""
    src = _helpers_source()
    m = re.search(
        r'os\.environ\.get\(\s*"HOOK_STDIN_TIMEOUT_S".*?except Exception:\s*\n\s*timeout_s\s*=\s*([0-9.]+)',
        src,
        re.S,
    )
    assert m, "could not find the except-branch fallback for HOOK_STDIN_TIMEOUT_S"
    return float(m.group(1))


def _hook_helper_grants():
    """Configured timeouts for every settings.json hook that imports hook_helpers.

    Returns {script_name: timeout}. A hook whose script cannot be resolved on
    disk is skipped rather than guessed -- an unresolvable entry must not
    silently lower the minimum and make the ordering assertion pass by
    accident.
    """
    with open(SETTINGS, encoding="utf-8") as fh:
        settings = json.load(fh)

    grants = {}
    for _event, matchers in (settings.get("hooks") or {}).items():
        for matcher in matchers or []:
            for hook in matcher.get("hooks") or []:
                command = str(hook.get("command") or "")
                timeout = hook.get("timeout")
                if not isinstance(timeout, (int, float)):
                    continue
                found = re.search(r"([A-Za-z0-9_.\-]+\.(?:sh|py))\s*$", command.strip())
                if not found:
                    continue
                name = found.group(1)
                stem = name.rsplit(".", 1)[0]
                # Check BOTH candidates. settings.json names the `.sh` wrapper,
                # but the wrapper is usually a thin shim and the import lives in
                # the sibling `.py` -- so breaking after the first candidate that
                # merely EXISTS excludes almost every real hook. That bug made an
                # earlier version of this file VACUOUS: it resolved 1 hook
                # (min grant 30s) instead of 41 (min grant 10s), so a sabotage
                # raising the bound to 15s still satisfied `15 < 30` and the
                # ordering test passed against sabotaged code. Caught by
                # mutation-proof-test.sh, not by reading it. Break only on a HIT.
                for candidate in (name, stem + ".py"):
                    path = os.path.join(SCRIPTS, candidate)
                    if not os.path.exists(path):
                        continue
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        body = fh.read()
                    if "hook_helpers" in body or "stdin_json_or_approve" in body:
                        prev = grants.get(name)
                        grants[name] = timeout if prev is None else min(prev, timeout)
                        break
    return grants


def test_bound_strictly_under_min_grant():
    """guard-1737 step 3: state the ordering, then assert it.

    STRICT `<`, never `<=`. Equality would mean the bound and the harness kill
    race, and which one wins decides whether the operator gets a diagnostic
    (our WARN + the timeout log) or silence.
    """
    grants = _hook_helper_grants()

    # POSITIVE CONTROL BEFORE THE PREDICATE (guard-2421). The assertion below
    # compares against min(grants), so anything that silently SHRINKS this
    # population raises the minimum and makes the whole test pass vacuously --
    # which is exactly how it failed its first mutation proof. A truthiness
    # check ("assert grants") does not catch that: one resolved hook is truthy.
    # 20 is a deliberately loose floor against a measured 23 distinct scripts;
    # it is here to catch a collapse to ~1, not to pin the exact roster.
    assert len(grants) >= 20, (
        "only %d hook_helpers-importing hooks resolved from settings.json "
        "(expected 23). The resolver is under-matching, so min() below would "
        "be computed over the wrong population and the ordering assertion "
        "would pass vacuously. Fix the resolver, do not lower this floor. "
        "Resolved: %s" % (len(grants), sorted(grants))
    )

    min_grant = min(grants.values())
    bound = _default_bound()

    assert bound < min_grant, (
        "HOOK_STDIN_TIMEOUT_S default (%ss) must be STRICTLY under the smallest "
        "harness grant among hooks that import hook_helpers (%ss, from %s). "
        "At or above it the harness kills the hook mid-read and the fail-open "
        "produces no diagnostic at all."
        % (
            bound,
            min_grant,
            ", ".join(sorted(n for n, t in grants.items() if t == min_grant)),
        )
    )


def test_source_literals_agree():
    """The default appears TWICE -- the env fallback and the except branch.

    Two literals encoding one intent is a drift hazard: a maintainer who edits
    only the env-lookup default leaves the except branch silently enforcing the
    old bound on any box where the env var is set to garbage.
    """
    assert _default_bound() == _except_fallback_bound(), (
        "the two HOOK_STDIN_TIMEOUT_S literals in hook_helpers.py disagree "
        "(env-lookup default %s vs except-branch fallback %s)"
        % (_default_bound(), _except_fallback_bound())
    )


def test_env_override_reaches_read():
    """guard-1737 step 3a: WIRING -- the constant reaches its own call site.

    The ordering test above compares two numbers and is structurally blind to a
    call site that ignores them. This drives the real code path: a child whose
    stdin is a pipe nobody ever closes, which is precisely the non-EOF stdin
    guard-664 describes. If HOOK_STDIN_TIMEOUT_S is honoured the child returns
    empty and exits promptly; if the bound is not wired, it hangs until the
    test's own timeout kills it.
    """
    program = textwrap.dedent(
        """
        import sys
        sys.path.insert(0, %r)
        import hook_helpers
        data = hook_helpers._read_stdin_bounded()
        sys.stderr.write("RETURNED:%%d\\n" %% len(data))
        """
        % SCRIPTS
    )

    env = dict(os.environ)
    env["HOOK_STDIN_TIMEOUT_S"] = "1"

    proc = subprocess.Popen(
        [sys.executable, "-c", program],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    # Write a COMPLETE payload but never close the pipe: the g-115 signature is
    # a hook blocked on EOF with the bytes already delivered.
    try:
        proc.stdin.write(b'{"tool_name":"Bash"}')
        proc.stdin.flush()
    except Exception:
        pass

    # NEVER call communicate() here: it CLOSES stdin, which delivers the very
    # EOF this test exists to withhold. Doing so makes the child read the
    # payload normally and return 20 bytes, and the test then "fails" against a
    # perfectly wired bound. Wait on the PROCESS and leave the pipe open.
    try:
        proc.wait(timeout=25)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        pytest.fail(
            "HOOK_STDIN_TIMEOUT_S=1 did not bound the read -- the constant is "
            "not reaching _read_stdin_bounded's join(); this is the hang "
            "guard-664 exists to prevent."
        )
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass

    err = proc.stderr.read()
    try:
        proc.stdout.close()
        proc.stderr.close()
    except Exception:
        pass

    text = err.decode("utf-8", "replace")
    assert "RETURNED:0" in text, (
        "bounded read did not degrade to empty on a non-EOF stdin; stderr was: %s" % text[:400]
    )
    assert "did not reach EOF" in text, (
        "the timeout path did not emit its WARN, so an operator gets no signal; "
        "stderr was: %s" % text[:400]
    )
