#!/usr/bin/env python3
# domain-leak-exempt: framework goal-execution-control infra — generic budget logic, no domain strings.
"""Goal budget — boxed, continuously-scored goal execution (Phase 4).

Part of the evaluative substrate; eval_harness.py is the keystone + in-code index of all seven.

WHY THIS EXISTS
---------------
The user's vision (#2): "as it's in the middle of running a goal, it should be fully boxed in and
recursively work until the goal is met, just like Claude Code's goal abstraction." And AutoLab
(2606.05080) found the DOMINANT failure of frontier agents on long-horizon iterate-and-improve
tasks is **premature termination / budget exhaustion** — they stop diagnosing-and-improving too
early. The fix is to box the goal: keep working until the goal is *met* or *definitively blocked*
or the *budget* is exhausted — and to score progress CONTINUOUSLY (0.0..1.0 against the goal's
verification criteria) rather than pass/fail only at the end.

This module is that control loop's bookkeeping: a `GoalBox` that records continuous progress
scores, enforces an attempt/wall-clock budget, and returns the next action. Its DEFAULT is
`continue` — it does not stop merely because an attempt was imperfect; it stops only on met /
blocked / budget. That default IS the anti-premature-termination guard. The per-attempt scores
it records are exactly the signal the Phase-0 eval harness consumes.

Pure, hermetic, domain-free. The clock is injectable so tests are deterministic; scoring a goal
against its criteria is the caller's (domain-specific) job — this owns the box, not the scorer.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

_ACTIONS = ("continue", "stop_met", "stop_blocked", "stop_budget")


def _check_score(s: float) -> float:
    if isinstance(s, bool) or not isinstance(s, (int, float)):
        raise ValueError(f"score must be a number in [0,1], got {s!r}")
    if not math.isfinite(s) or not (0.0 <= s <= 1.0):
        raise ValueError(f"score must be a finite number in [0,1], got {s!r}")
    return float(s)


@dataclass
class Attempt:
    score: float
    note: str = ""
    at: float = 0.0  # clock reading when recorded


@dataclass
class Decision:
    action: str
    reason: str
    best_score: float
    attempts_used: int

    def as_dict(self) -> dict:
        return {"action": self.action, "reason": self.reason,
                "best_score": round(self.best_score, 6), "attempts_used": self.attempts_used}


@dataclass
class GoalBox:
    """A boxed goal: work until met / blocked / budget-exhausted.

    target_score: the continuous score (<=1.0) at which the goal counts as MET.
    max_attempts: hard cap on iterations (>=1).
    max_seconds:  optional wall-clock cap (None = no time cap).
    epsilon:      dead-band so floating scores at the target still count as met.
    clock:        injectable monotonic clock (seconds) for deterministic tests.
    """
    goal_id: str
    max_attempts: int
    max_seconds: Optional[float] = None
    target_score: float = 1.0
    epsilon: float = 1e-9
    clock: Callable[[], float] = time.monotonic
    attempts: List[Attempt] = field(default_factory=list)
    _started_at: Optional[float] = None
    _blocked_reason: Optional[str] = None

    def __post_init__(self):
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.max_seconds is not None and self.max_seconds <= 0:
            raise ValueError("max_seconds must be > 0 when set")
        _check_score(self.target_score)

    def start(self) -> None:
        if self._started_at is None:
            self._started_at = self.clock()

    def record(self, score: float, note: str = "") -> None:
        """Record one attempt's continuous progress score (0..1)."""
        self.start()
        self.attempts.append(Attempt(_check_score(score), note, self.clock()))

    def mark_blocked(self, reason: str) -> None:
        """Signal the goal is DEFINITIVELY blocked (route to CREATE_BLOCKER).
        This is the only non-budget early stop — and it must be a real external
        blocker, not 'this attempt failed' (that is a `continue`)."""
        self._blocked_reason = reason

    @property
    def best_score(self) -> float:
        return max((a.score for a in self.attempts), default=0.0)

    @property
    def elapsed(self) -> float:
        return 0.0 if self._started_at is None else self.clock() - self._started_at

    def plateaued(self, window: int = 3, min_delta: float = 0.01) -> bool:
        """Advisory: is the recent window FLAT — its spread (max - min) below min_delta,
        so the last `window` attempts are neither improving nor regressing meaningfully?

        Range-based by design: it asks whether the window's total spread (max - min)
        is below min_delta. A dip-and-recover like [0.9, 0.1, 0.9] (spread 0.8) reads
        as NOT plateaued (volatile, not stuck), and a climb whose spread reaches
        min_delta is not a plateau. But a *slow crawl* whose total spread stays under
        min_delta (e.g. [0.500, 0.504, 0.508], spread 0.008 < 0.01) IS flagged — even
        though every step nudges upward. That is intentional: barely-moving progress is
        exactly the diminishing-returns signal this is meant to catch. (So "any steady
        climb is exempt" would be WRONG — only a climb whose spread clears min_delta is.)
        Advisory ONLY: a plateau NEVER forces a stop (per AutoLab — don't terminate
        early); the caller may use it to switch approach while still inside the box.
        """
        if window < 2 or len(self.attempts) < window:
            return False
        recent = [a.score for a in self.attempts[-window:]]
        return (max(recent) - min(recent)) < min_delta

    def decide(self) -> Decision:
        """The next action. Order matters: met first (success short-circuits),
        then a real block, then budget. Otherwise CONTINUE — the default that
        prevents premature termination."""
        n, best = len(self.attempts), self.best_score
        if self.attempts and best >= self.target_score - self.epsilon:
            return Decision("stop_met", f"best score {best:.4f} reached target "
                            f"{self.target_score}", best, n)
        if self._blocked_reason is not None:
            return Decision("stop_blocked", f"definitively blocked: {self._blocked_reason}",
                            best, n)
        if n >= self.max_attempts:
            return Decision("stop_budget", f"attempt budget exhausted ({n}/{self.max_attempts}) "
                            f"at best score {best:.4f}", best, n)
        if self.max_seconds is not None and self.elapsed >= self.max_seconds:
            return Decision("stop_budget", f"time budget exhausted "
                            f"({self.elapsed:.1f}s/{self.max_seconds}s) at best {best:.4f}",
                            best, n)
        return Decision("continue", f"under budget ({n}/{self.max_attempts}), best {best:.4f} "
                        f"< target {self.target_score} — keep working", best, n)
