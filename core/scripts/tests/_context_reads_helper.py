"""Shared access to context-reads.py's REAL normalize_path ().

Four test files each hand-rolled the normalizer as
`str(Path(p).resolve()).replace("\\\\", "/")` — resolve-then-replace, the exact
ordering g-240-105 proved wrong — and two of them carried the comment
"Match context-reads.py normalize_path" while not matching it. This module
exists so there is ONE definition and the copies cannot silently desync
(rb-1915: pin both sides to a shared definition rather than re-verify copies).

WHY THE COPIES PASSED. Production replaces separators BEFORE resolve();
the copies resolved first. On forward-slash input the two agree, and every
test that used the copies fed forward-slash paths. They diverge only where
`resolve()` must SEE separators to do its job — measured on POSIX, cc-03:

    r"core\\..\\core\\scripts\\context-reads.py"
        hand -> /opt/ayoai-mind/core/../core/scripts/context-reads.py
        prod -> /opt/ayoai-mind/core/scripts/context-reads.py

i.e. the hand-rolled form leaves `..` and `.` UNCOLLAPSED, because a
backslashed string is one filename component to a POSIX Path and there is
nothing for resolve() to normalize. The trailing replace then repairs the
separators and hides it. That is the guard-920 shape: the helper matches the
hand-test shape, and the defect appears only in the production shape.
"""

import importlib.util
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = TESTS_DIR.parent            # core/scripts

_CR = None


def load_context_reads():
    """Import context-reads.py (hyphenated — not importable by name).

    MUST cancel the module-level self-destruct timer, or importing this file
    silently kills the whole pytest session. context-reads.py arms
    `threading.Timer(10, lambda: os._exit(0))` at import — a watchdog for the
    short-lived PostToolUse hook subprocess, where a killed bash parent can
    strand the Python child (Windows does not propagate SIGTERM). That is
    correct for a process that lives milliseconds. Imported into a LONG-RUNNING
    pytest process it fires ~10s after collection and calls `os._exit(0)`,
    which terminates the interpreter immediately: no traceback, no pytest
    epilogue, no summary line — and exit status **0**, i.e. "success".

    The failure that produces is uniquely deceptive, so it is worth naming
    (measured g-240-105, 2026-07-31): the death point is determined by the
    CLOCK, not by any test, so it lands on a different test each run and reads
    as flaky/contended. `run-full-suite.py` reported `VERDICT: INVALID
    (contended)` with `chunk 04 stopped at 13%` across two runs — and the chunk
    reproduced it running SOLO, exit 0, an 84-byte log with zero NUL bytes.
    Small batches pass (they finish inside 10s), which is why a file's own few
    tests are green alone and green paired. Only a batch running >10s dies.

    Memoized: five call sites now share this, and re-importing would re-arm a
    fresh timer each time. Cancelling N timers is not worse than cancelling
    one, but importing once is cheaper and keeps a single module object.
    """
    global _CR
    if _CR is not None:
        return _CR
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    spec = importlib.util.spec_from_file_location(
        "_cr_under_test", str(SCRIPT_DIR / "context-reads.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    timer = getattr(mod, "_timer", None)
    assert timer is not None, (
        "context-reads.py no longer exposes `_timer`. If the self-destruct "
        "watchdog was renamed or moved, cancel the new one HERE — otherwise "
        "this import re-arms a 10s os._exit(0) inside pytest and the suite "
        "dies mid-run with exit status 0 (see the docstring above)."
    )
    timer.cancel()
    _CR = mod
    return mod


def norm_path(p):
    """THE production normalizer, not a copy of it.

    Do not re-implement this body in a test file. That is what g-115-4301
    existed to remove, and a copy that is correct today desyncs on the next
    change to context-reads.normalize_path with nothing to catch it.
    """
    mod = load_context_reads()
    fn = getattr(mod, "normalize_path", None)
    assert fn is not None, (
        "context-reads.py no longer exposes `normalize_path`. Point this "
        "helper at the new name — do NOT re-inline a copy of the body, "
        "which is the duplication g-115-4301 removed. Without this assert "
        "a rename surfaces as AttributeError inside each individual test, "
        "once per call site, instead of once here with the reason."
    )
    return fn(p)
