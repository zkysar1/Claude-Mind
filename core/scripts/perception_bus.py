"""In-process perception bus -- the decoupling boundary between perception and
cognition (M-10, g-319-04).

MECHANISM. A single in-process Python object holding per-module buffers and
queues. Decided in the goal, not here: a message broker adds an always-on infra
dependency and maps awkwardly onto latest-wins CONTINUOUS delivery, and a
file-based transport would adopt the unfenced-file-concurrency defect this repo
is mid-way through removing. In-process matches the convention's delivery
semantics 1:1. The bus is a PER-RUNTIME decoupling boundary, not a cross-runtime
transport -- one bus per cognition runtime, no wire protocol.

SSOT. `core/config/conventions/perception-module.md` owns every semantic here.
Section references in this file point at it; when the two disagree the
convention wins and this file is the bug.

WHY THE TYPE IS `Percept` AND NOT `Observation`. The goal that commissioned this
says "typed Observation pub/sub", but the convention -- the interface all three
runtimes build against -- names it `Percept` throughout (S1 "Percept", S5.1
"Bus buffers the latest percept per module"). Two names for one wire type is how
a shared interface forks. `Observation` in the goal text means this.

WHY TTL IS MEASURED FROM A BUS CLOCK, NOT FROM `percept.timestamp`. The
`timestamp` field is PRODUCER-supplied wall time (S1: "when the observation was
captured"), so a module with a skewed clock, or a HEARSAY percept relaying a
partner's foreign timestamp, would expire on arrival or never expire at all --
a producer would be deciding the bus's retention. The bus stamps its own
monotonic reading at publish and expires from that. Monotonic specifically:
wall clocks jump under NTP correction and would silently expire a live buffer.
`clock` is injectable so the tests are deterministic rather than sleep-based.

WHY ONE GLOBAL TOPOLOGICAL ORDER. S4 states the ordering contract three times,
once per cadence ("before A's perceive()" / "process B before A" / "gather B's
contribution before A's"). Sorting all registered modules once at start() and
filtering that order per invocation satisfies all three readings with one
mechanism, and lets a dependency cross cadence groups without a special case.

WHY PERCEIVE() ERRORS ARE ISOLATED. A perception module is a sensor. One broken
sensor must not blind the whole perception layer, so a raising `perceive()` is
recorded and skipped rather than propagated -- the bus keeps delivering every
other module's percepts. Errors are counted per module and retained (bounded) so
a module that is failing every tick is visible rather than merely quiet; a
silently-skipped sensor reads exactly like a sensor with nothing to report.

WHY THE FENCE IS COMPUTED INSIDE gather() AND IS NOT AN OPTIONAL SECOND STEP.
S5.3 makes REQUEST_SCOPED percepts untrusted data unconditionally ("regardless
of source"). An API that returns raw percepts and offers a separate fencing
helper makes the safe path the one a caller has to remember; here `gather()`
returns a batch whose fenced text is already built.
"""

from __future__ import annotations

import dataclasses
import datetime
import enum
import json
import re
import subprocess
import time


# ---------------------------------------------------------------------------
# Types (convention S1, S3)
# ---------------------------------------------------------------------------

class CadenceType(enum.Enum):
    """How often a module fires. Convention S3."""

    CONTINUOUS = "CONTINUOUS"
    EVENT_DRIVEN = "EVENT_DRIVEN"
    REQUEST_SCOPED = "REQUEST_SCOPED"


class ProvenanceTag(enum.Enum):
    """Evidential weight of an observation. Convention S1.

    The convention states an ordering (SYNTHESIZED "ranks between DIRECT and
    INFERRED", M-5 weight 0.8) but assigns numeric weight to no other tag, and
    S5.3 is explicit that provenance "conveys evidential weight, not trust
    level" and that "the cognition layer MAY weight retrieval and scoring by
    provenance". Weighting is therefore cognition's decision, not the bus's --
    no weight table is defined here, because inventing the three unpinned
    numbers would hand every consumer a fabricated scale that looks authored.
    """

    DIRECT = "DIRECT"
    SYNTHESIZED = "SYNTHESIZED"
    INFERRED = "INFERRED"
    HEARSAY = "HEARSAY"


def utc_stamp():
    """Naive ISO-8601 in UTC wall time -- the fleet-wide stamp format."""
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


@dataclasses.dataclass(frozen=True)
class ResourceBudget:
    """A module's declared cost envelope. Convention S1 `cost_estimate()`.

    The convention names the type and never defines its shape, so this carries
    only what S3.1's one consumer needs: a latency figure to spend against a
    per-tick budget, and a priority deciding who is shed when it runs out.
    Higher priority is shed LAST.
    """

    latency_ms: float = 0.0
    priority: int = 0


@dataclasses.dataclass(frozen=True)
class Trigger:
    """What caused a `perceive()` call. Convention S1 `perceive(trigger)`.

    `kind` is one of "tick" | "event" | "request", mirroring the three cadences.
    `data` carries the event payload for EVENT_DRIVEN, and is None otherwise.
    """

    kind: str
    tick: int = 0
    data: object = None


@dataclasses.dataclass(frozen=True)
class Percept:
    """A normalized observation. Convention S1.

    Field order differs from the convention's listing: the three fields with no
    sensible default (source_module, source_pack, payload) must precede the
    defaulted ones. The SET of fields is the contract; their declaration order
    is a Python constraint.
    """

    source_module: str
    source_pack: str
    payload: object
    provenance: ProvenanceTag = ProvenanceTag.DIRECT
    confidence: float = 1.0
    ttl: float | None = None     # seconds; None = valid until superseded (S1)
    timestamp: str = ""          # ISO-8601; auto-filled when left empty

    def __post_init__(self):
        if not isinstance(self.provenance, ProvenanceTag):
            raise ValueError(
                "provenance must be a ProvenanceTag, got %r" % (self.provenance,))
        try:
            conf = float(self.confidence)
        except (TypeError, ValueError):
            raise ValueError("confidence must be a float, got %r"
                             % (self.confidence,))
        if not 0.0 <= conf <= 1.0:
            raise ValueError("confidence must be within [0.0, 1.0], got %r"
                             % (self.confidence,))
        if self.ttl is not None and self.ttl <= 0:
            # A zero or negative TTL is expired-on-arrival, which is never what
            # a producer means; "no expiry" is spelled None (S1).
            raise ValueError("ttl must be positive or None, got %r" % (self.ttl,))
        if not self.timestamp:
            object.__setattr__(self, "timestamp", utc_stamp())


@dataclasses.dataclass(frozen=True)
class _Envelope:
    """Bus-internal wrapper. `received_at` is the BUS clock, never the producer's."""

    percept: Percept
    received_at: float


@dataclasses.dataclass(frozen=True)
class ContextBatch:
    """One REQUEST_SCOPED gather. Convention S5.1 batch + S5.3 trust boundary."""

    percepts: tuple
    fenced_text: str


class PerceptionCycleError(ValueError):
    """A pack's dependency graph contains a cycle. Convention S4."""


class UnknownDependencyError(ValueError):
    """A module depends on a module_id that was never registered.

    Not merely a typo check: the S4 ordering contract cannot be honored for a
    dependency the bus has never seen, and silently ignoring it would deliver
    A's percept while claiming B ran first.
    """


# ---------------------------------------------------------------------------
# The module interface (convention S1)
# ---------------------------------------------------------------------------

class PerceptionModule:
    """Base class for a perception module. Convention S1.

    Subclasses set `module_id`, `pack`, `cadence` and implement `perceive()`.
    `throttle_ticks` implements the S3.1 internal-throttle pattern (a module
    with throttle_ticks=30 runs once per 30 ticks -- once per 10s at 3 Hz).
    """

    module_id = ""
    pack = ""
    cadence = CadenceType.CONTINUOUS
    throttle_ticks = 1

    def start(self, config=None):
        """Called once by PerceptionBus.start()."""

    def stop(self):
        """Called once by PerceptionBus.stop()."""

    def perceive(self, trigger):
        """Produce a Percept from current world state, or None if nothing new."""
        raise NotImplementedError

    def dependencies(self):
        """module_ids that must perceive() before this one. Convention S4."""
        return []

    def cost_estimate(self):
        """This module's cost envelope. Convention S1 / S3.1."""
        return ResourceBudget()


# ---------------------------------------------------------------------------
# Trust boundary (convention S5.3)
# ---------------------------------------------------------------------------

FENCE_OPEN = "<injected_context>"
FENCE_CLOSE = "</injected_context>"

# Matches an opening OR closing fence tag in any casing, with optional space
# after the slash. Anything that could terminate the fence must be caught here;
# a payload that closes its own fence escapes into instruction position, which
# is the entire failure this fence exists to prevent.
_FENCE_RE = re.compile(r"<(/?)\s*injected_context", re.IGNORECASE)


def neutralize_fence_sentinels(text):
    """Break any fence tag inside untrusted text so it cannot close the fence.

    The angle bracket is replaced with a paren, so `</injected_context>` becomes
    `(/injected_context>` -- readable, obviously-neutered, and unable to
    reconstitute a tag. Deliberately NOT a zero-width-space or homoglyph trick:
    those survive a copy-paste and can be re-normalized downstream.
    """
    return _FENCE_RE.sub(lambda m: "(" + m.group(1) + "injected_context", text)


def render_payload(payload):
    """Untrusted structured payload -> text, for fencing."""
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(payload)


def fence_percepts(percepts):
    """Render a REQUEST_SCOPED batch as sentinel-neutralized fenced text (S5.3)."""
    if not percepts:
        return ""
    lines = []
    for p in percepts:
        body = neutralize_fence_sentinels(render_payload(p.payload))
        head = neutralize_fence_sentinels(
            "[%s/%s %s confidence=%s]" % (p.source_pack, p.source_module,
                                          p.provenance.value, p.confidence))
        lines.append(head + "\n" + body)
    return FENCE_OPEN + "\n" + "\n\n".join(lines) + "\n" + FENCE_CLOSE


# ---------------------------------------------------------------------------
# The bus (convention S4, S5)
# ---------------------------------------------------------------------------

class PerceptionBus:
    """In-process perception bus.

    Producers call `tick()` / `emit()`; cognition calls `read_continuous()`,
    `drain()` and `gather()`. The cognition layer never touches a module.
    """

    MAX_RETAINED_ERRORS = 100

    def __init__(self, clock=time.monotonic, tick_budget_ms=None):
        self._clock = clock
        self._tick_budget_ms = tick_budget_ms
        self._modules = {}          # module_id -> module
        self._order = []            # topological order, set at start()
        self._started = False
        self._continuous = {}       # module_id -> _Envelope (latest-wins, S5.1)
        self._events = {}           # module_id -> list[_Envelope] (FIFO, S5.1)
        self._last_batch = None     # discarded each gather (S5.1)
        self._tick_no = 0
        self.errors = []            # bounded list of (module_id, phase, repr)
        self.error_counts = {}      # module_id -> int
        self.shed_counts = {}       # module_id -> int (S3.1 budget shedding)

    # -- registration / lifecycle -------------------------------------------

    def register(self, module):
        """Add a module. Must happen before start()."""
        if self._started:
            raise RuntimeError("cannot register after start(); stop() first")
        mid = getattr(module, "module_id", "")
        if not mid:
            raise ValueError("module_id is required")
        if mid in self._modules:
            raise ValueError("duplicate module_id: %s" % mid)
        if not isinstance(getattr(module, "cadence", None), CadenceType):
            raise ValueError("module %s has no valid CadenceType" % mid)
        self._modules[mid] = module
        return module

    def start(self, configs=None):
        """Topologically order the pack, then start every module.

        Ordering runs BEFORE any module is started: a pack with a cycle is
        rejected without having half-started its modules (S4).
        """
        configs = configs or {}
        self._order = self._topological_order()
        for mid in self._order:
            module = self._modules[mid]
            try:
                module.start(configs.get(mid))
            except Exception as exc:               # noqa: BLE001 -- isolation
                self._record_error(mid, "start", exc)
        self._started = True
        return list(self._order)

    def stop(self):
        for mid in reversed(self._order or list(self._modules)):
            try:
                self._modules[mid].stop()
            except Exception as exc:               # noqa: BLE001 -- isolation
                self._record_error(mid, "stop", exc)
        self._started = False

    def _topological_order(self):
        """Kahn's algorithm over the whole pack. Raises on cycle / unknown dep."""
        deps = {}
        for mid, module in self._modules.items():
            declared = list(module.dependencies() or [])
            for dep in declared:
                if dep not in self._modules:
                    raise UnknownDependencyError(
                        "module %s depends on unregistered module %s" % (mid, dep))
            deps[mid] = set(declared)

        # Deterministic order among independent modules: registration order is
        # arbitrary from the pack author's view, so ties break on module_id.
        pending = sorted(deps)
        ordered = []
        while pending:
            ready = [m for m in pending if not deps[m] - set(ordered)]
            if not ready:
                raise PerceptionCycleError(
                    "dependency cycle among: %s" % ", ".join(sorted(pending)))
            for mid in ready:
                ordered.append(mid)
                pending.remove(mid)
        return ordered

    # -- error isolation -----------------------------------------------------

    def _record_error(self, module_id, phase, exc):
        self.error_counts[module_id] = self.error_counts.get(module_id, 0) + 1
        self.errors.append((module_id, phase, "%s: %s" % (type(exc).__name__, exc)))
        if len(self.errors) > self.MAX_RETAINED_ERRORS:
            del self.errors[:-self.MAX_RETAINED_ERRORS]

    def _safe_perceive(self, module, trigger):
        """Invoke perceive() with the module's failure contained to itself."""
        try:
            return module.perceive(trigger)
        except Exception as exc:                   # noqa: BLE001 -- isolation
            self._record_error(module.module_id, "perceive", exc)
            return None

    def _validate(self, module_id, percept):
        """A non-null perceive() result must be a Percept from that module."""
        if percept is None:
            return None
        if not isinstance(percept, Percept):
            self._record_error(module_id, "perceive",
                               TypeError("returned %r, expected Percept or None"
                                         % (type(percept).__name__,)))
            return None
        return percept

    def _expired(self, env, now):
        ttl = env.percept.ttl
        return ttl is not None and (now - env.received_at) >= ttl

    def _ids_for(self, cadence):
        order = self._order or sorted(self._modules)
        return [m for m in order if self._modules[m].cadence is cadence]

    # -- CONTINUOUS (S5.1 row 1, S3.1 throttle + budget) ---------------------

    def _admitted(self, candidates):
        """Apply the S3.1 per-tick compute budget, shedding lowest priority first.

        A shed module's DEPENDENTS are shed too: running A when its declared
        dependency B did not run this tick would violate the S4 ordering
        contract while looking like a normal delivery.

        THROTTLE DOES NOT CASCADE THE SAME WAY, and the asymmetry is deliberate.
        A throttled-out B is the MODULE AUTHOR's declared choice (S3.1), visible
        in the pack, and an author who makes A depend on a 30-tick B authored
        that relationship knowingly. Budget shedding is the BUS's runtime
        decision, invisible to the author -- so the bus must not silently break
        the guarantee A was written to rely on. Ordering is all S4 promises;
        co-scheduling is not, which is why only the bus's own choice cascades.
        """
        if self._tick_budget_ms is None:
            return candidates
        by_priority = sorted(
            candidates,
            key=lambda m: (-self._modules[m].cost_estimate().priority, m))
        spent = 0.0
        admitted = set()
        for mid in by_priority:
            cost = float(self._modules[mid].cost_estimate().latency_ms or 0.0)
            if spent + cost > self._tick_budget_ms:
                continue
            spent += cost
            admitted.add(mid)
        # Transitively drop dependents of anything shed.
        changed = True
        while changed:
            changed = False
            for mid in list(admitted):
                deps = set(self._modules[mid].dependencies() or [])
                if deps & (set(candidates) - admitted):
                    admitted.discard(mid)
                    changed = True
        for mid in candidates:
            if mid not in admitted:
                self.shed_counts[mid] = self.shed_counts.get(mid, 0) + 1
        return [m for m in candidates if m in admitted]

    def tick(self, data=None):
        """Advance one CONTINUOUS tick. Returns the number of percepts buffered.

        Null percepts are DROPPED, never delivered (S5.1: "null = nothing
        changed"), and a new percept supersedes the previous one for that module.
        """
        self._tick_no += 1
        candidates = [m for m in self._ids_for(CadenceType.CONTINUOUS)
                      if self._tick_no % max(1, self._modules[m].throttle_ticks) == 0]
        published = 0
        trigger = Trigger(kind="tick", tick=self._tick_no, data=data)
        for mid in self._admitted(candidates):
            percept = self._validate(mid, self._safe_perceive(self._modules[mid],
                                                              trigger))
            if percept is None:
                continue
            self._continuous[mid] = _Envelope(percept, self._clock())
            published += 1
        return published

    def read_continuous(self):
        """Current CONTINUOUS buffer, expired entries dropped. NON-destructive.

        S5.1 has cognition reading "the current buffer at its own pace" and
        skipping intermediate percepts, so a read must not consume -- that is
        the difference between this and drain().
        """
        now = self._clock()
        live = {}
        for mid, env in list(self._continuous.items()):
            if self._expired(env, now):
                del self._continuous[mid]
                continue
            live[mid] = env.percept
        return live

    # -- EVENT_DRIVEN (S5.1 row 2) -------------------------------------------

    def emit(self, module_id, data=None):
        """Deliver an external event to one EVENT_DRIVEN module.

        Returns True when the module produced a percept. Percepts are queued
        FIFO and never superseded -- each discrete event is preserved (S5.1).
        """
        module = self._modules.get(module_id)
        if module is None:
            raise KeyError("no such module: %s" % module_id)
        if module.cadence is not CadenceType.EVENT_DRIVEN:
            raise ValueError("emit() targets EVENT_DRIVEN modules; %s is %s"
                             % (module_id, module.cadence.value))
        percept = self._validate(
            module_id,
            self._safe_perceive(module, Trigger(kind="event", data=data)))
        if percept is None:
            return False
        self._events.setdefault(module_id, []).append(
            _Envelope(percept, self._clock()))
        return True

    def drain(self, module_id=None):
        """Consume queued EVENT_DRIVEN percepts, dropping TTL-expired ON DEQUEUE.

        Expiry is applied here rather than at enqueue precisely because S5.1
        says so: a percept whose TTL elapses while queued was still a real
        event, and dropping it at dequeue keeps the at-least-once guarantee
        honest about its "within the TTL window" qualifier (S5.2).
        """
        now = self._clock()
        targets = [module_id] if module_id is not None else list(self._events)
        out = []
        for mid in targets:
            queue = self._events.get(mid) or []
            self._events[mid] = []
            for env in queue:
                if self._expired(env, now):
                    continue
                out.append(env.percept)
        return out

    def pending_event_count(self, module_id=None):
        """Queued percepts, expired ones excluded. Does not consume."""
        now = self._clock()
        targets = [module_id] if module_id is not None else list(self._events)
        return sum(1 for mid in targets for env in (self._events.get(mid) or [])
                   if not self._expired(env, now))

    # -- REQUEST_SCOPED (S5.1 row 3, S5.2 guarantee 3, S5.3) -----------------

    def gather(self, data=None):
        """Run one cognition cycle's REQUEST_SCOPED gather.

        Each REQUEST_SCOPED module is invoked exactly once (S5.2 guarantee 3),
        in dependency order, and the previous cycle's batch is discarded (S5.1).
        The returned batch's text is already fenced (S5.3).
        """
        self._last_batch = None
        trigger = Trigger(kind="request", tick=self._tick_no, data=data)
        collected = []
        for mid in self._ids_for(CadenceType.REQUEST_SCOPED):
            percept = self._validate(
                mid, self._safe_perceive(self._modules[mid], trigger))
            if percept is not None:
                collected.append(percept)
        batch = ContextBatch(percepts=tuple(collected),
                             fenced_text=fence_percepts(collected))
        self._last_batch = batch
        return batch

    @property
    def last_batch(self):
        """The current cycle's batch, or None before the first gather()."""
        return self._last_batch


# ---------------------------------------------------------------------------
# Reference modules -- one listen-signal, one exec-script (convention S2)
# ---------------------------------------------------------------------------

class FileTouchModule(PerceptionModule):
    """EVENT_DRIVEN: reports that a watched file changed. Convention S2.1.

    The canonical listen-signal example the convention cites for claude-mind.
    The external poller decides WHEN to look (S3.2 lists "file-touch detection
    via poll loop" as a legitimate event source); this module decides whether
    anything actually happened, returning None when the mtime is unmoved --
    which is what keeps a poll-driven event source from manufacturing events.
    """

    cadence = CadenceType.EVENT_DRIVEN

    def __init__(self, module_id, pack, path, ttl=None):
        self.module_id = module_id
        self.pack = pack
        self.path = str(path)
        self.ttl = ttl
        self._last_mtime = None

    def start(self, config=None):
        # Baseline at start, so an already-existing file is not reported as a
        # change on the first event.
        self._last_mtime = self._mtime()

    def _mtime(self):
        try:
            import os
            return os.stat(self.path).st_mtime
        except OSError:
            return None

    def perceive(self, trigger):
        mtime = self._mtime()
        if mtime == self._last_mtime:
            return None
        previous, self._last_mtime = self._last_mtime, mtime
        return Percept(
            source_module=self.module_id, source_pack=self.pack,
            payload={"path": self.path, "mtime": mtime,
                     "previous_mtime": previous,
                     "exists": mtime is not None},
            provenance=ProvenanceTag.DIRECT, ttl=self.ttl)


class ScriptPollModule(PerceptionModule):
    """CONTINUOUS: runs a command each admitted tick. Convention S2.2.

    argv is an explicit list -- never a shell string -- so the command is not
    re-parsed by a shell and argv[0] is resolved as given (a bare interpreter
    name resolved through a search path is its own class of platform bug).
    A timeout returns None: a command that never answered observed nothing.
    """

    cadence = CadenceType.CONTINUOUS

    def __init__(self, module_id, pack, argv, throttle_ticks=1, timeout=10.0,
                 ttl=None, cost=None):
        self.module_id = module_id
        self.pack = pack
        self.argv = list(argv)
        self.throttle_ticks = max(1, int(throttle_ticks))
        self.timeout = timeout
        self.ttl = ttl
        self._cost = cost or ResourceBudget()

    def cost_estimate(self):
        return self._cost

    def perceive(self, trigger):
        try:
            proc = subprocess.run(self.argv, capture_output=True, text=True,
                                  timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return None
        return Percept(
            source_module=self.module_id, source_pack=self.pack,
            payload={"argv": self.argv, "rc": proc.returncode,
                     "stdout": proc.stdout, "stderr": proc.stderr},
            provenance=ProvenanceTag.DIRECT,
            # A non-zero rc is still a DIRECT observation of the world; it is
            # reported with reduced confidence rather than swallowed, because
            # "the probe failed" is itself something cognition needs to see.
            confidence=1.0 if proc.returncode == 0 else 0.5,
            ttl=self.ttl)
