#!/usr/bin/env python3
"""notify-build-payload.py — Deterministic payload builder for /notify-user.

Single source of truth for SendInfoAlert / SendErrorAlert JSON construction.
Refuses to emit payloads with empty Body (the silent-empty-email failure
mode observed 2026-05-20 — completion emails arriving with only Title +
UTC timestamp + reply-footer because the LLM-hand-constructed payload had
Body=empty).

The previous flow had /notify-user's SKILL.md instructing the LLM to
hand-write the SendInfoAlert JSON template at every call site. Any small
omission (forgetting Body, leaving the template's hardcoded "This is
Alpha" placeholder, dropping content into Sections that didn't render)
produced a silent-empty email. This helper removes the freeform-prose
layer: callers pass structured inputs, the helper builds the JSON
deterministically, validates the Body length, and emits to stdout.

Inputs (all flags; one of --message / --message-file is required):
  --agent <name>           Required (or falls back to MIND_AGENT env).
  --category <cat>         Required. info|completion|update|blocker|decision-needed
  --subject <text>         Required. Email subject.
  --message <text>         Inline body content.
  --message-file <path>    OR read body content from a file.
  --sections-json <json>   Optional. JSON array for Sections field.
  --next-steps-json <json> Optional. JSON array for NextSteps field.
  --project-root <path>    Override PROJECT_ROOT (default: derived from
                           script location). Test-only.

Output:
  JSON payload to stdout. Pipe to world/scripts/email-send.sh.

Exit codes:
  0  — payload valid, emitted to stdout
  1  — input validation error (missing flag, bad JSON, etc.)
  2  — payload Body too short (silent-empty-email guard)
  3  — agent self.md missing, unreadable, or has no '# Self' heading

Example:
  py -3 core/scripts/notify-build-payload.py \\
      --agent bravo --category completion \\
      --subject "Completion Report (31h, 6 goals, 1 deep)" \\
      --message-file agents/bravo/COMPLETION-REPORT.md \\
    | bash world/scripts/email-send.sh
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


VALID_CATEGORIES = ("info", "completion", "update", "blocker", "decision-needed")

CATEGORY_TO_INFOTYPE = {
    "info": "Notification",
    "completion": "Completion Report",
    "update": "Aspiration Update",
    "blocker": "Infrastructure Alert",
    "decision-needed": "Decision Needed",
}

# Minimum message length AFTER stripping. The silent-empty-email failure
# mode produced payloads with Body=empty or just the template placeholder.
# 20 chars is the floor — high enough to catch truly empty ("", placeholder
# "<message>" = 9 chars, "TODO" = 4 chars, "test send" = 9 chars) but low
# enough not to reject legitimate brief notifications callers actually send:
#   - "Bridge unreachable on dev" (blocker alert)      = 25 chars
#   - "Created asp-XXX: widget enhancements" (aspir.)  = 36 chars
#   - "Decomposed g-NNN into 3 subgoals" (decompose)   = 32 chars
#   - "Forged skill 'foo' for category bar" (forge)    = 35 chars
# Calibrated 2026-05-20 (N1 fresh-eyes finding) — original 50-char floor
# rejected all four of the above shapes.
MIN_MESSAGE_CHARS = 20


def extract_self_identity(agent: str, project_root: Path) -> str:
    """Read agents/<agent>/self.md and return the first sentence of the body.

    Anchors on the '# Self' heading — all agent self.md files use this
    convention per .claude/rules/self.md. Failing loud (exit 3) is
    intentional: a malformed self.md is itself the failure mode this
    helper exists to catch, and falling back silently to an empty
    identity reproduces the bug we're fixing.
    """
    self_md = project_root / "agents" / agent / "self.md"
    if not self_md.exists():
        print(
            f"ERROR: agents/{agent}/self.md does not exist (project_root={project_root})",
            file=sys.stderr,
        )
        sys.exit(3)

    try:
        text = self_md.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: failed to read {self_md}: {exc}", file=sys.stderr)
        sys.exit(3)

    # Anchor on '# Self' heading. Works for both single- and double-front-matter
    # self.md shapes (bravo has two YAML blocks; alpha/echo/charlie/zeta/delta
    # have one) — the heading comes after all front matter in every shape.
    heading = re.search(r"^#+\s*Self\s*$", text, re.MULTILINE)
    if not heading:
        print(
            f"ERROR: agents/{agent}/self.md has no '# Self' heading — "
            f"cannot extract self-identification. The body section MUST "
            f"start with '# Self' per .claude/rules/self.md schema.",
            file=sys.stderr,
        )
        sys.exit(3)

    body = text[heading.end():].lstrip()
    if not body:
        print(
            f"ERROR: agents/{agent}/self.md has no body content after '# Self' heading",
            file=sys.stderr,
        )
        sys.exit(3)

    # First sentence: lazy match until . ! or ? followed by whitespace or EOF.
    # re.DOTALL lets the lazy match span newlines (some agents wrap the first
    # sentence across two lines before the period).
    match = re.match(r"(.+?[.!?])(?:\s|$)", body, re.DOTALL)
    if not match:
        print(
            f"ERROR: agents/{agent}/self.md body has no sentence terminator (. ! ?) "
            f"in the first paragraph",
            file=sys.stderr,
        )
        sys.exit(3)

    sentence = match.group(1).strip()
    # Collapse internal whitespace/newlines (sentence may span 2 lines in source)
    sentence = " ".join(sentence.split())
    return sentence


def read_message(args) -> str:
    if args.message is not None:
        return args.message
    if args.message_file is not None:
        path = Path(args.message_file)
        if not path.exists():
            print(f"ERROR: --message-file does not exist: {path}", file=sys.stderr)
            sys.exit(1)
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"ERROR: failed to read --message-file {path}: {exc}", file=sys.stderr)
            sys.exit(1)
    print("ERROR: one of --message or --message-file is required", file=sys.stderr)
    sys.exit(1)


def parse_json_array(flag_name: str, raw: str):
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"ERROR: {flag_name} parse: {exc}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(value, list):
        print(f"ERROR: {flag_name} must be a JSON array, got {type(value).__name__}", file=sys.stderr)
        sys.exit(1)
    return value


def main():
    parser = argparse.ArgumentParser(
        description="Deterministic SendInfoAlert/SendErrorAlert payload builder",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--agent", help="Agent name; falls back to MIND_AGENT env")
    parser.add_argument("--category", required=True, choices=VALID_CATEGORIES)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--message", default=None)
    parser.add_argument("--message-file", default=None)
    parser.add_argument("--sections-json", default="[]")
    parser.add_argument("--next-steps-json", default="[]")
    parser.add_argument(
        "--project-root",
        default=None,
        help="Override PROJECT_ROOT (test only). Default: derived from script location.",
    )
    args = parser.parse_args()

    agent = args.agent or os.environ.get("MIND_AGENT")
    if not agent:
        print(
            "ERROR: --agent not provided and MIND_AGENT env not set",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.project_root:
        project_root = Path(args.project_root).resolve()
    else:
        # Script lives at PROJECT_ROOT/core/scripts/notify-build-payload.py
        project_root = Path(__file__).resolve().parent.parent.parent

    subject = args.subject.strip()
    if not subject:
        print("ERROR: --subject is empty", file=sys.stderr)
        sys.exit(1)

    message = read_message(args).strip()
    if len(message) < MIN_MESSAGE_CHARS:
        print(
            f"ERROR: message body is only {len(message)} chars, "
            f"minimum is {MIN_MESSAGE_CHARS}. "
            f"Silent-empty-email guard refuses send (2026-05-20 incident: "
            f"completion emails arrived with only Title + UTC + reply-footer "
            f"because Body was empty). The floor catches truly empty or "
            f"placeholder content; a real notification — even a brief one — "
            f"should easily exceed it. Investigate the caller.",
            file=sys.stderr,
        )
        sys.exit(2)

    sections = parse_json_array("--sections-json", args.sections_json)
    next_steps = parse_json_array("--next-steps-json", args.next_steps_json)

    identity = extract_self_identity(agent, project_root)
    body = f"{identity}\n\n{message}"

    if args.category == "blocker":
        payload = {
            "ErrorMessage": body,
            "ErrorFrom": subject,
        }
    else:
        payload = {
            "InfoMessage": f"{args.category}: {subject}",
            "InfoType": CATEGORY_TO_INFOTYPE[args.category],
            "Title": subject,
            "Body": body,
            "Sections": sections,
            "NextSteps": next_steps,
        }

    # ensure_ascii=False so em-dashes and other unicode in self.md don't
    # become escape sequences in the email body. The downstream transport accepts UTF-8.
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
