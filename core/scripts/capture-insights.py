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

#  / : force utf-8 on stdin/stdout/stderr (covers Windows
# cp1252 fallback when callers bypass the _platform.sh PYTHONIOENCODING=utf-8
# shim). Closes acceptance (4) of  — stdin-ingest sweep.
from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()

from _paths import AGENT_DIR

def extract_insights(text: str) -> list[str]:
    """Extract content between ✶ Insight delimiters."""
    if not text or "Insight" not in text:
        return []
    # Pattern: optional backtick + ✶ Insight + ─ chars + optional backtick,
    # then content, then a line of ─ chars (with optional backticks)
    pattern = re.compile(
        r'`?✶ Insight[─ ]+`?\n(.*?)\n\s*`?[─]{10,}`?',
        re.DOTALL
    )
    return [m.group(1).strip() for m in pattern.finditer(text)]


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
    if not AGENT_DIR:
        sys.exit(0)

    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    message = hook_input.get("last_assistant_message", "")
    session_id = hook_input.get("session_id", "")

    insights = extract_insights(message)
    if not insights:
        # Observability anchor ( / ): without this line, a
        # zero-extraction run is indistinguishable from a broken pipeline
        # for downstream observers. ✶ Insight blocks only emit under the
        # Explanatory output style — sessions in default/concise styles
        # produce zero captures by design, NOT by failure. The Stop hook
        # invocation suppresses stderr (2>/dev/null), so this line is
        # silent in normal operation; it surfaces only when the script is
        # invoked diagnostically without the suppression.
        print(
            "[capture-insights] zero Insight blocks extracted — likely the "
            "session output style is not 'Explanatory' (only that style "
            "emits the ✶ Insight format). Empty extraction is correct "
            "behavior for default/concise styles.",
            file=sys.stderr,
        )
        sys.exit(0)

    filepath = AGENT_DIR / "insights.jsonl"
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    # Get starting ID from existing file, then increment in memory
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


if __name__ == "__main__":
    main()
