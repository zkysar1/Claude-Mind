"""Capture Insight blocks from assistant output via Stop hook.

Reads hook JSON from stdin, extracts ✶ Insight blocks from
last_assistant_message, appends to <agent>/insights.jsonl.
Always exits 0 — never blocks the stop.
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

# Windows cp1252 fix — must run before any I/O (stdin reads hook JSON with ✶/─)
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from _paths import AGENT_DIR

# --- Debug logging ---
# Writes to <agent>/session/capture-insights.log (auto-cleaned by boot).
# Falls back to core/scripts/.capture-insights.log if no agent bound.
_LOG_FILE = None

def _init_log():
    global _LOG_FILE
    if AGENT_DIR and AGENT_DIR.exists():
        session_dir = AGENT_DIR / "session"
        if session_dir.exists():
            _LOG_FILE = session_dir / "capture-insights.log"
            return
    _LOG_FILE = Path(__file__).resolve().parent / ".capture-insights.log"

def _log(msg: str):
    if _LOG_FILE is None:
        _init_log()
    try:
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat()}] {msg}\n")
    except Exception:
        pass  # Never block the stop hook


# --- Dash character normalization ---
# ─ (U+2500), ━ (U+2501), — (U+2014), – (U+2013), - (U+002D)
_DASH_RE = re.compile(r'[\u2500\u2501\u2014\u2013\u002D]')


def extract_insights(text: str) -> list[str]:
    """Extract content between Insight delimiters.

    Two-stage approach:
    1. Primary regex — handles the common multiline format
    2. Fallback line-based parser — handles edge cases (same-line content, etc.)
    """
    if not text or "Insight" not in text:
        _log(f"extract: early exit ({'empty' if not text else 'no Insight keyword'})")
        return []

    results = _extract_primary(text)
    _log(f"extract: primary regex found {len(results)} insight(s)")

    if not results:
        results = _extract_fallback(text)
        _log(f"extract: fallback found {len(results)} insight(s)")

    return results


def _extract_primary(text: str) -> list[str]:
    """Regex extraction — permissive pattern for standard Insight blocks."""
    # Normalize all dash-like characters to standard dash for matching
    normalized = _DASH_RE.sub('-', text)

    pattern = re.compile(
        r'`?[^\w\s`]{0,2}'   # Header: optional backtick, up to 2 symbol chars (✶, *, etc.)
        r'\s*Insight'          # Optional space, "Insight"
        r'[\s\-]*`?'           # Dash decoration + optional closing backtick
        r'\s+'                 # Whitespace/newline before content (1+ required)
        r'(.*?)'              # Content (non-greedy, DOTALL)
        r'\n\s*`?-{5,}`?',   # Closing: newline, optional ws/backtick, 5+ dashes
        re.DOTALL
    )
    return [m.group(1).strip() for m in pattern.finditer(normalized) if m.group(1).strip()]


def _extract_fallback(text: str) -> list[str]:
    """Line-based fallback extraction for non-standard Insight block formats."""
    lines = text.split('\n')
    insights = []
    capturing = False
    current: list[str] = []

    for line in lines:
        stripped = line.strip().strip('`')

        # Detect Insight header line (star/symbol char + "Insight" + dashes)
        if 'Insight' in stripped and re.search(r'[^\w\s`].*Insight', stripped):
            capturing = True
            current = []
            # Check for content after the header decoration on the same line
            after = re.sub(r'[`]*[^\w\s`]{0,2}\s*Insight[\u2500\u2501\u2014\u2013\u002D\s`]*', '', stripped).strip()
            if after:
                current.append(after)
            continue

        # Detect closing delimiter (5+ dash-like characters)
        if capturing and re.match(r'^[`\s]*[\u2500\u2501\u2014\u2013\u002D]{5,}[`\s]*$', stripped):
            content = '\n'.join(current).strip()
            if content:
                insights.append(content)
            capturing = False
            current = []
            continue

        if capturing:
            current.append(line)

    return insights


def next_id(filepath: Path) -> str:
    """Get next ins-NNN ID from existing file."""
    max_n = 0
    if filepath.exists():
        for line in filepath.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                n = int(rec["id"].split("-")[1])
                if n > max_n:
                    max_n = n
            except (json.JSONDecodeError, KeyError, ValueError, IndexError):
                continue
    return f"ins-{max_n + 1:03d}"


def main():
    _log("=== capture-insights.py invoked ===")

    if not AGENT_DIR:
        _log("AGENT_DIR is None — no agent bound. Exiting.")
        sys.exit(0)

    _log(f"AGENT_DIR = {AGENT_DIR}")

    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError) as e:
        _log(f"Failed to read hook JSON from stdin: {e}")
        sys.exit(0)

    message = hook_input.get("last_assistant_message", "")
    session_id = hook_input.get("session_id", "")

    _log(f"session_id={session_id}, message_length={len(message)}")
    _log(f"message contains 'Insight': {'Insight' in message}")

    # Log a snippet around "Insight" keyword for format debugging
    if "Insight" in message:
        idx = message.index("Insight")
        start = max(0, idx - 50)
        end = min(len(message), idx + 200)
        _log(f"snippet: ...{repr(message[start:end])}...")

    insights = extract_insights(message)
    if not insights:
        _log("No insights extracted. Exiting.")
        sys.exit(0)

    _log(f"Extracted {len(insights)} insight(s)")

    filepath = AGENT_DIR / "insights.jsonl"
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    start_id = next_id(filepath)
    start_n = int(start_id.split("-")[1])

    with open(filepath, "a", encoding="utf-8") as f:
        for i, content in enumerate(insights):
            entry = {
                "id": f"ins-{start_n + i:03d}",
                "timestamp": now,
                "content": content,
                "session_id": session_id,
                "processed": False,
            }
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    _log(f"Wrote {len(insights)} insight(s) to {filepath}")


if __name__ == "__main__":
    main()
