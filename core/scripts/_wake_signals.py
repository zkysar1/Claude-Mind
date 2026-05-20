"""Wake-signal touch helpers — break partner agents out of long sleeps when
external state changes (board posts, claim releases, email arrivals).

Used by board.py, aspirations.py:cmd_release, and world/scripts/email-read.sh
(via `py -3 -c "from _wake_signals import ..."`). The receiver side is
`core/scripts/interruptible-sleep.sh` which polls `<agent>/session/<name>`
files and exits 2 (light-precheck branch) when any of the three wake signals
are present, then deletes the file (one-shot consume).

Contract: best-effort, NEVER raises. Wake-signal write failure must not
break the originating action (a board post must not fail because we
couldn't touch a partner's session directory). All filesystem errors are
caught and silently dropped — the cost of a missed wake is one extra
sleep cycle, not a workflow break.

Signal contract (must match interruptible-sleep.sh + session.py VALID_SIGNALS):
  board-activity         coordination-channel post detected
  email-received         inbox poll found new mail
  goal-claim-released    aspirations cmd_release ran

Renaming any of these requires coordinated edits to:
  - this file
  - core/scripts/interruptible-sleep.sh
  - core/scripts/session.py VALID_SIGNALS
  - core/config/session-manifest.yaml (recovery_action: clear entry)
"""

import os as _os
from pathlib import Path as _Path

_SCRIPT_DIR = _Path(__file__).resolve().parent
# core/scripts/ → core/ → project root.
# Mirror the resolution used in capability-gate.py / blocker-create-gate.py
# so a future move of these helpers keeps the project-root invariant.
_CORE_ROOT = _SCRIPT_DIR.parent
_PROJECT_ROOT = _CORE_ROOT.parent

# Names of root-level directories that are NOT agents. Used to filter the
# project-root scan when team-state is unavailable. Keep narrow — anything
# missing here just produces a stat-failure (handled silently).
_NON_AGENT_DIRS = frozenset({
    "core", ".claude", ".git", ".github", ".history", ".vscode",
    "node_modules", "__pycache__",
})

_AGENTS_PARENT_DIR = "agents"  # Phase 2.5.C: sync with _paths.py AGENTS_PARENT_DIR
_AGENTS_ROOT = _PROJECT_ROOT / _AGENTS_PARENT_DIR if _AGENTS_PARENT_DIR else _PROJECT_ROOT


def _peer_agent_names(self_agent):
    """Return list of non-self agent names, discovered via project-root scan.

    Only includes directories that look like agents (have a session/ subdir).
    Filesystem scan is more conservative than reading team-state.yaml because
    a fresh agent that hasn't written team-state yet still has a session/ dir.
    """
    peers = []
    try:
        for d in _AGENTS_ROOT.iterdir():
            if not d.is_dir():
                continue
            if d.name.startswith(".") or d.name in _NON_AGENT_DIRS:
                continue
            if d.name == self_agent:
                continue
            if (d / "session").is_dir():
                peers.append(d.name)
    except OSError:
        return []
    return peers


def touch_peer_signals(signal_name):
    """Touch <peer>/session/<signal_name> for every non-self peer agent.

    'self' = MIND_AGENT env var. When MIND_AGENT is unset, every discovered
    agent is treated as a peer — conservative default so a system process
    (e.g., scheduled poller) without an agent binding still fans the signal
    to all agents.

    Returns the count of files successfully touched (informational; callers
    typically ignore the return value).
    """
    self_agent = (_os.environ.get("MIND_AGENT") or "").strip()
    peers = _peer_agent_names(self_agent)
    n = 0
    for peer in peers:
        try:
            # MUST use _AGENTS_ROOT, not _PROJECT_ROOT — peer dirs live under
            # _AGENTS_PARENT_DIR. Writing to _PROJECT_ROOT/<peer>/ silently
            # creates root cruft AND the receiver (interruptible-sleep.sh,
            # which reads from $AGENT_DIR via _paths.sh) never sees the signal.
            target = _AGENTS_ROOT / peer / "session" / signal_name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
            n += 1
        except OSError:
            # Best-effort. A peer's session dir may be locked, mounted on a
            # network drive that's unavailable, or non-existent. Wake-signal
            # failure must NEVER break the originating action.
            continue
    return n


def touch_self_signal(signal_name):
    """Touch <self>/session/<signal_name>. Used for same-agent signals like
    email-received where the polling loop and the sleeping loop are the same
    agent (cross-thread / cross-process within the same binding).

    Best-effort. Returns True on success, False on any filesystem error.
    """
    self_agent = (_os.environ.get("MIND_AGENT") or "").strip()
    if not self_agent:
        return False
    try:
        # MUST use _AGENTS_ROOT — see touch_peer_signals() for rationale.
        target = _AGENTS_ROOT / self_agent / "session" / signal_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
        return True
    except OSError:
        return False


def _cli(argv):
    """CLI entry — `py -3 _wake_signals.py <peer|self> <signal-name>`.

    Exposed so bash callers (e.g., world/scripts/email-read.sh) can write
    signals without re-implementing the discovery logic.
    """
    if len(argv) < 2:
        print("Usage: _wake_signals.py {peer|self} <signal-name>", file=__import__("sys").stderr)
        return 2
    mode, name = argv[0], argv[1]
    if mode == "peer":
        n = touch_peer_signals(name)
        print(n)
        return 0
    if mode == "self":
        ok = touch_self_signal(name)
        print("1" if ok else "0")
        return 0 if ok else 1
    print(f"unknown mode: {mode}", file=__import__("sys").stderr)
    return 2


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(_cli(_sys.argv[1:]))
