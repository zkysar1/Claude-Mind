"""Wake-signal perception modules — the `mind-signal` pack (M-11, ).

Wraps the `_wake_signals.py` file-touch signals in the PerceptionModule
interface so the cognition layer reads board posts and inbound email as
Percepts off the PerceptionBus instead of polling. Spec:
`core/config/conventions/perception-module.md` S6.2 (the listen-signal table),
which already names both modules, their cadence and their wake classes — this
file implements that table rather than inventing a shape for it.

WHY THIS IS A SEPARATE FILE FROM perception_bus.py, where FileTouchModule and
ScriptPollModule live. The bus is stdlib-only on purpose: the convention calls
it "the portable cognition core" built against by three runtimes (ayoai,
claude-mind, zak-code), and FileTouchModule takes an explicit path precisely so
the bus never learns an agent layout. These modules MUST resolve
`agents/<agent>/session/<signal>`, so importing `_paths` into the bus would
trade that portability for two classes. The pack boundary is the natural seam.

────────────────────────────────────────────────────────────────────────────
THE OBSERVER RULE: THIS MODULE NEVER DELETES A SIGNAL FILE.
────────────────────────────────────────────────────────────────────────────
Wake signals are ONE-SHOT: `interruptible-sleep.sh` polls at 1s granularity,
exits 2, and DELETES the file. It is the consumer. A perception module that
also deleted would race it and silently swallow wakes — a sleeping loop that
never wakes, with no error anywhere. So `perceive()` only ever stats.

This is why the arrival test is mtime-based rather than existence-based, and
why it is deliberately NOT symmetric:

    file appears, or mtime advances   -> ARRIVAL. Emit a Percept.
    mtime unchanged                   -> nothing new. None.
    file disappears                   -> the CONSUMER ran (or recovery cleared
                                         it). NOT an event. None, and the
                                         baseline resets so the next touch is
                                         seen as a fresh arrival.

────────────────────────────────────────────────────────────────────────────
guard-1504 — EVERY WRITER OF THE FILE, ENUMERATED BEFORE TRUSTING ITS mtime.
────────────────────────────────────────────────────────────────────────────
That guardrail disqualifies an mtime probe when a second, cheaper, more
frequent write-purpose exists (a counter, an access stamp, a cache fill),
because the frequent write drowns the rare meaningful one and the probe then
fails in the safe-looking direction. Enumerated here, that condition does NOT
hold — recorded so the next reader does not have to re-derive it:

  board-activity   WRITE  board.py -> _wake_signals.touch_peer_signals(), after
                          a board append. This IS the event.
                   WRITE  session-signal-set.sh / session.py (generic setter;
                          `board-activity` is in VALID_SIGNALS).
                   DELETE interruptible-sleep.sh (one-shot consume)
                   DELETE session-manifest-clear.sh (recovery_action: clear,
                          core/config/session-manifest.yaml)
  email-received   WRITE  world/scripts/email-read.sh ->
                          _wake_signals.touch_self_signal(), after an inbox
                          poll finds mail. This IS the event.
                   WRITE  the same generic setter.
                   DELETE the same two deleters.

Every writer writes EXACTLY the event we care about; there is no cheap
frequent second purpose. So mtime is real evidence here — which is a
property of these two files, not of mtime, and it is why the enumeration is
written down instead of the conclusion alone.

────────────────────────────────────────────────────────────────────────────
guard-4886 — THE SIGNAL IS DECISIVE IN ONE DIRECTION ONLY.
────────────────────────────────────────────────────────────────────────────
A Percept means: a signal was raised, and no consumer had removed it when we
looked. Decisive.

The absence of a Percept means NOTHING about whether a board post or an email
happened. Three benign generators produce the identical observable (guard-2111
— the confounder set is plural):
  1. genuinely no event,
  2. an event whose signal `interruptible-sleep.sh` consumed between polls,
  3. an event whose signal a recovery/manifest-clear removed.
So absence is UNKNOWN, never a negative. Do NOT build a "no board activity
since T" claim on a quiet bus; that requires the board store itself.
These modules are a WAKE path, not an audit trail — the durable record of a
board post is `world/board/*.jsonl`, and of an email the agent inbox.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from perception_bus import (  # noqa: E402
    CadenceType,
    PerceptionModule,
    Percept,
    ProvenanceTag,
    ResourceBudget,
)

PACK = "mind-signal"

# Wake classes, from interruptible-sleep.sh via the convention's S6.2 table.
# BLOCKER always exits 2; INFORMATIONAL is consumed but does not break a
# quiescence-approved sleep (QUIESCENCE_SLEEP=1). Carried in the payload so a
# consumer can triage without re-reading the sleep script.
WAKE_BLOCKER = "BLOCKER"
WAKE_INFORMATIONAL = "INFORMATIONAL"


def _resolve_agent(agent=None):
    """Agent whose session dir holds the signals. Explicit arg wins over env."""
    name = (agent or os.environ.get("MIND_AGENT") or "").strip()
    return name or None


class WakeSignalModule(PerceptionModule):
    """EVENT_DRIVEN observer of one `agents/<agent>/session/<signal>` file.

    Base for the two modules M-11 names. Concrete per-signal subclasses exist
    (rather than one parameterised instance) because the convention assigns
    each signal a distinct module_id and wake class, and the bus keys
    registration, dependencies and emit() on module_id.

    Never deletes. See the module docstring for the observer rule, the
    guard-1504 writer enumeration, and the one-directional reading.
    """

    cadence = CadenceType.EVENT_DRIVEN
    signal_name = ""
    wake_class = WAKE_INFORMATIONAL

    def __init__(self, agent=None, ttl=None, module_id=None, pack=PACK):
        self.agent = _resolve_agent(agent)
        self.pack = pack
        self.ttl = ttl
        if module_id:
            self.module_id = module_id
        self._last_mtime = None

    # -- path -------------------------------------------------------------

    def signal_path(self):
        """Absolute path to this module's signal file, or None if unresolvable.

        Routed through `_paths.agent_state_dir`, never a
        `PROJECT_ROOT / agent` join — CLAUDE.md "Agent-dir Resolution" makes
        that mandatory, because the layout moves by flipping constants and a
        hand-rolled join silently keeps pointing at the old shape.

        Imported lazily: `_paths` reads `local-paths.conf` and env, and a
        module whose import can fail on an unbound box would take the whole
        pack down with it. An unresolvable path is a quiet no-op instead.
        """
        if not self.agent:
            return None
        try:
            from _paths import agent_state_dir
            return Path(agent_state_dir(self.agent)) / self.signal_name
        except Exception:
            return None

    def _mtime(self):
        path = self.signal_path()
        if path is None:
            return None
        try:
            return os.stat(path).st_mtime
        except OSError:
            return None

    # -- lifecycle --------------------------------------------------------

    def start(self, config=None):
        """Baseline at start so a signal already on disk is not replayed.

        Same reason FileTouchModule baselines: a file left over from before
        this process existed is not an observation this process made, and
        emitting it would hand the cognition layer a wake it cannot date.
        """
        self._last_mtime = self._mtime()

    # -- the contract -----------------------------------------------------

    def perceive(self, trigger):
        mtime = self._mtime()

        if mtime is None:
            # Consumed, cleared, or never present. Reset the baseline so the
            # NEXT touch reads as an arrival, and report nothing: a
            # disappearance is the consumer doing its job, not an event.
            self._last_mtime = None
            return None

        if mtime == self._last_mtime:
            return None

        previous, self._last_mtime = self._last_mtime, mtime
        return Percept(
            source_module=self.module_id,
            source_pack=self.pack,
            payload={
                "signal": self.signal_name,
                "agent": self.agent,
                "path": str(self.signal_path()),
                "wake_class": self.wake_class,
                "mtime": mtime,
                "previous_mtime": previous,
            },
            provenance=ProvenanceTag.DIRECT,
            ttl=self.ttl,
        )

    def cost_estimate(self):
        # One stat() per event. Declared explicitly rather than inherited so
        # the bus's tick budget sees a real number for this pack.
        return ResourceBudget()


class BoardSignalModule(WakeSignalModule):
    """A coordination/findings post was appended to `world/board/*.jsonl`.

    Writer: `board.py` -> `touch_peer_signals("board-activity")`. Fans out to
    every PEER, so this fires for a partner's post, never for the agent's own.
    """

    module_id = "board-signal"
    signal_name = "board-activity"
    wake_class = WAKE_INFORMATIONAL


class EmailSignalModule(WakeSignalModule):
    """Inbound mail arrived at the agent inbox.

    Writer: `world/scripts/email-read.sh` -> `touch_self_signal("email-received")`,
    so this is a SELF signal — the polling and sleeping loops are one agent.
    Wake class BLOCKER: user communication always breaks the sleep.
    """

    module_id = "email-signal"
    signal_name = "email-received"
    wake_class = WAKE_BLOCKER


def register_signal_modules(bus, agent=None, ttl=None):
    """Register the whole pack on `bus`; returns the modules in order.

    The M-11 verification is "both modules registered on the bus", so the
    registration is a named operation rather than two call sites a caller has
    to remember to keep in step.
    """
    modules = [BoardSignalModule(agent=agent, ttl=ttl),
               EmailSignalModule(agent=agent, ttl=ttl)]
    for module in modules:
        bus.register(module)
    return modules
