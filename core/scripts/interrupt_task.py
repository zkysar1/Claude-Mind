#!/usr/bin/env python3
"""Directive-hold pin: keep a user-interrupt task alive across loop turn boundaries.

WHY THIS EXISTS (g-306-386, measured twice on the coach deployment 2026-08-30 with a
human waiting LIVE): a mid-loop user directive was correctly handled in the turn it
arrived, then LOST to loop momentum at the next turn boundary — the reducer resumed
strategic-scan/precheck both times, once leaving a single-use consent code unconsumed
in the pane until an operator had to backstop it.

The asymmetry it corrects: `stop-requested` SURVIVES turn boundaries because it is a
file on disk that every re-entry checks. A directive does not survive, because nothing
writes it down. This is the same survival pattern with the OPPOSITE POLARITY —
do-this-first instead of stop.

WHY A WM SLOT AND NOT A SIGNAL FILE: session signals are payload-free marker files
behind a VALID_SIGNALS allowlist (session.py cmd_signal_set touches an empty file). A
pinned task needs its one-line text to survive with it, or the next turn knows only
THAT something is open and not WHAT. The slot is registered in
wm.RESET_SURVIVING_SLOTS so a consolidation-time wm-reset cannot silently drop a
standing user obligation.

FAIL-OPEN, DELIBERATELY, AND THE DIRECTION IS ARGUED: every error path here returns
"no pin" so the loop keeps running. That is the polarity that LOSES a task, which is
the very defect this module exists to fix — so it is a real cost, not a free default.
It is still correct: a plumbing fault in this module must not wedge a healthy loop
(guard-1562, and the reducer_self_fence HOLD-on-ambiguity precedent). The mitigation is
that `check` prints LOUDLY on a malformed slot rather than returning a silent clean, so
a broken pin is visible in the turn it breaks instead of being indistinguishable from
"nothing pinned".
"""
import json, os, sys
from datetime import datetime, timezone

SLOT = "interrupt_task_open"


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def decide(slot_value):
    """Pure. Given the raw slot value, return (verdict, payload, message).

    verdict is one of:
      "none"      -- nothing pinned; loop proceeds normally
      "pinned"    -- a task is open and MUST be dispositioned before normal phases
      "malformed" -- slot present but unreadable; treat as none, but say so LOUDLY
    """
    if slot_value in (None, "", "null"):
        return ("none", None, "no interrupt task pinned")
    if isinstance(slot_value, str):
        try:
            slot_value = json.loads(slot_value)
        except Exception:
            return ("malformed", None,
                    "interrupt_task_open is a non-JSON string; treating as UNPINNED so the "
                    "loop is not wedged, but a pinned task may have been LOST here")
    if not isinstance(slot_value, dict):
        return ("malformed", None,
                f"interrupt_task_open is {type(slot_value).__name__}, expected object; "
                "treating as UNPINNED, but a pinned task may have been LOST here")
    task = str(slot_value.get("task") or "").strip()
    if not task:
        return ("malformed", slot_value,
                "interrupt_task_open has no 'task' text; treating as UNPINNED, but "
                "something wrote this slot and the task text did not survive")
    return ("pinned", slot_value, task)


def render(verdict, payload, message):
    """Human/LLM-facing rendering. The imperative is the product, not the JSON."""
    if verdict == "none":
        return "[interrupt-task] none pinned"
    if verdict == "malformed":
        return f"[interrupt-task] ⚠ MALFORMED PIN — {message}"
    opened = payload.get("opened_at", "?")
    src = payload.get("source", "?")
    return (
        "[interrupt-task] ═══ PINNED USER TASK — DISPOSITION REQUIRED BEFORE NORMAL PHASES ═══\n"
        f"[interrupt-task]   task:      {message}\n"
        f"[interrupt-task]   opened:    {opened} (source: {src})\n"
        "[interrupt-task] This survived a turn boundary because a human is waiting on it.\n"
        "[interrupt-task] Do ONE of: (a) continue/complete the task now, then\n"
        "[interrupt-task]   `interrupt-task.sh release --reason \"<what happened>\"`;\n"
        "[interrupt-task]   (b) if it is genuinely blocked, say so TO THE USER and release with\n"
        "[interrupt-task]   that reason. Do NOT resume strategic-scan/precheck with this open —\n"
        "[interrupt-task]   that is the exact failure this pin exists to prevent (g-306-386)."
    )


def main(argv):
    import argparse
    p = argparse.ArgumentParser(prog="interrupt-task", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("open", help="Pin a user-interrupt task across turn boundaries")
    o.add_argument("task", help="One line: what the user asked for")
    o.add_argument("--source", default="user-directive")
    o.add_argument("--force", action="store_true", help="Replace an already-open pin")
    sub.add_parser("check", help="Exit 0 if a task is pinned, 1 if not (fail-open to 1)")
    r = sub.add_parser("release", help="Clear the pin")
    r.add_argument("--reason", required=True, help="What happened — completed, or why not")
    sub.add_parser("status", help="JSON status, never non-zero")
    a = p.parse_args(argv)

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import wm
    except Exception as e:
        print(f"[interrupt-task] wm import failed ({e}); failing open", file=sys.stderr)
        return 1

    def _read():
        # resolve_slot returns a LOCATOR (parent_dict, key, is_top_level), NOT the
        # value. Reading it as a value yields a 3-tuple, which decide() correctly
        # rejects as malformed — so the bug renders as "MALFORMED PIN" on a healthy
        # slot and, worse, made the post-write verify report WRITE DID NOT VERIFY on a
        # write that had actually landed. Caught by the lifecycle self-test, not by
        # inspection (guard-1755 class: the read-back instrument was the broken part).
        parent, key, _top = wm.resolve_slot(wm.read_wm() or {}, SLOT)
        if not isinstance(parent, dict):
            return None
        return parent.get(key)

    def _write(value):
        # locked read-modify-write: WM is shared with every other slot writer, so a
        # bare read+write here would clobber a concurrent set (governed-store rules).
        with wm.wm_lock():
            data = wm.read_wm() or {}
            data.setdefault("slots", {})[SLOT] = value
            wm.write_wm(data)

    try:
        cur = _read()
    except Exception as e:
        print(f"[interrupt-task] slot read failed ({e}); failing open", file=sys.stderr)
        return 1

    verdict, payload, message = decide(cur)

    if a.cmd == "status":
        print(json.dumps({"verdict": verdict, "payload": payload, "message": message}))
        return 0

    if a.cmd == "check":
        print(render(verdict, payload, message))
        return 0 if verdict == "pinned" else 1

    if a.cmd == "open":
        if verdict == "pinned" and not a.force:
            print(f"[interrupt-task] REFUSED: a task is already pinned: {message}\n"
                  "[interrupt-task] Release it first, or pass --force to replace it. Two "
                  "concurrent user obligations silently overwriting each other is worse "
                  "than one that has to be acknowledged.", file=sys.stderr)
            return 2
        rec = {"task": a.task, "opened_at": _now(), "source": a.source,
               "opened_by": os.environ.get("MIND_AGENT", "?"),
               "sid": os.environ.get("MIND_SID", "")}
        _write(rec)
        back = _read()
        v2, _, _ = decide(back)
        if v2 != "pinned":
            print("[interrupt-task] WRITE DID NOT VERIFY — the pin is NOT set. A user "
                  "obligation is unrecorded; handle it in THIS turn.", file=sys.stderr)
            return 3
        print(f"[interrupt-task] pinned: {a.task}")
        return 0

    if a.cmd == "release":
        if verdict != "pinned":
            print(f"[interrupt-task] nothing pinned to release ({verdict})")
            return 0
        _write(None)
        print(f"[interrupt-task] released: {message}\n[interrupt-task] reason: {a.reason}")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
