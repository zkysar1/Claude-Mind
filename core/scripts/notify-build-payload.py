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
  NOTE: --category blocker emits the SendErrorAlert shape (ErrorMessage/
  ErrorFrom); all other categories emit the SendInfoAlert shape.
  email-send.sh auto-routes by shape (g-115-2434), so --error is optional —
  but the shape split is load-bearing: SendInfoAlert refuses a payload with
  only ErrorMessage/ErrorFrom keys as empty-content.

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
import subprocess  # finding-disproof gate ()
import sys
from pathlib import Path


VALID_CATEGORIES = ("info", "completion", "update", "blocker", "decision-needed",
                    "user-digest", "reply")

CATEGORY_TO_INFOTYPE = {
    "info": "Notification",
    "completion": "Completion Report",
    "update": "Aspiration Update",
    "blocker": "Infrastructure Alert",
    "decision-needed": "Decision Needed",
    # . A batched list of goals waiting on a human. It exists because
    # the set a correct digest needed was EMPTY, and that emptiness is the whole
    # defect — two independent, locally-correct decisions whose intersection
    # nobody checked:
    #   gate-exempt in notify-user Step 1.5 = {blocker, completion}
    #   SendInfoAlert-shaped (the ONLY shape with a pretty renderer) = all but blocker
    #   intersection = {completion}, which is semantically wrong for a digest.
    # So the digest took `blocker`, which is gate-exempt for a real reason (it
    # quotes arbitrary goal descriptions, any of which could contain "user must"
    # and refuse the whole send, wedging the lane — the  shape). The
    # cost was invisible at the call site: `blocker` routes to SendErrorAlert,
    # which has NO render_structured at all, so a routine to-do list arrived
    # headed "AyoAi Error Alert" in a red-bordered white-space:pre-wrap box.
    # That is the D1 "raw text" complaint, and it is very likely also the
    #  "caused anxiety" complaint, which was read as a content problem
    # and answered by reordering the body.
    # This category is SendInfoAlert-shaped (so it renders) AND must be listed in
    # notify-user Step 1.5's exempt tuple (so it never wedges). Adding one
    # WITHOUT the other re-breaks it — keep the two in sync.
    "user-digest": "Fleet Digest",
    # . An ANSWER to something the user himself asked. Every other
    # category above names a message the FLEET decided to send; this one names
    # the reply half of an exchange he started, which is why it is the third
    # member of the routing gate's ALWAYS_SEND set (the reasoning lives there).
    #
    # SendInfoAlert-shaped, deliberately and for the reason the digest comment
    # above learned the hard way: `blocker` is the one shape with no
    # render_structured, so anything routed through it arrives as raw text under
    # an "AyoAi Error Alert" heading. An answer to a direct question is the last
    # message that should look like an error.
    #
    # IT DIVERGES FROM THE DIGEST ON STEP 1.5, ON PURPOSE — do not "sync" it.
    # The digest MUST be gate-exempt because it quotes arbitrary goal
    # descriptions it did not author, so one quoted "user must" would wedge the
    # whole lane. A reply is authored deliberately, one message at a time, and
    # guard-4722 records the case that decides this: a reply whose closing
    # sentence had become a permission request for already-granted work SHOULD
    # have been refused. Exempting `reply` would delete exactly that catch and
    # turn this category into the re-send door that guardrail forbids.
    "reply": "Reply",
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
    # Relaxed from the strict `^#+\s*Self\s*$` to also accept a labeled
    # `# Self - <agent-name>` heading (some agents suffix the heading with the
    # agent name); the strict form hard-exits(3) on a labeled heading, silently
    # breaking that agent's notifications.
    heading = re.search(r"^#+\s*Self(\s.*)?$", text, re.MULTILINE)
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
    # --- finding-disproof gate () -------------------------------
    # Fires for `blocker` and `decision-needed` only — the two categories that
    # carry FINDINGS to the user (info/completion/update are status reports).
    # See finding-disproof-gate.py for the incident: a blocker email told the
    # user the fleet's storage layer was broken on every box. It was false, and
    # the disproof was ten seconds away inside the evidence being cited.
    parser.add_argument("--disproof-probe", default="",
                        help="The command that would FALSIFY this finding's central claim.")
    parser.add_argument("--disproof-result", default="",
                        help="What that command actually printed (a probe with no result "
                             "is a plan, not evidence).")
    parser.add_argument("--disproof-waived", default="",
                        help="Ship a universal/causal claim WITHOUT a disproof probe. "
                             "Justification required; echoed to stderr for audit. Use only "
                             "when the alert is genuinely time-critical and unfalsifiable-"
                             "by-command — never to route around the question.")
    parser.add_argument("--html-file", default="",
                        help="Path to a complete HTML document to send AS the email body "
                             "(transport HTML-passthrough mode). --message/--message-file "
                             "stays REQUIRED: it is the plain text the gates, ledger and "
                             "dedup read. Not allowed for --category blocker.")
    parser.add_argument("--fenced-quotes", action="store_true",
                        help="This message QUOTES text this agent did not author, fenced "
                             "with '> '. Those lines are excluded from the disproof gate's "
                             "universal/causal scan — a report is not an assertion "
                             "(g-115-4594). Opt-in: agent-authored prose stays fully scanned, "
                             "so a markdown blockquote used for emphasis keeps its coverage.")
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

    # --- finding-disproof gate (, 2026-07-14) --------------------
    # Enforced HERE, not in notify-user/SKILL.md, because the incident email
    # BYPASSED the skill entirely — the agent called this script directly. A
    # gate in a SKILL.md is honor-system; the script is the real chokepoint,
    # and honor-system is precisely what already failed (guard-1065 + rb-3408 +
    # rb-3410 + rb-3419 all said "mechanism is not case" and did not stop it).
    #
    # Fail-OPEN on any gate error (rc=2 / missing gate / exception): a bug here
    # must never silence a real alert. Fail-CLOSED only on a matched claim with
    # no disproof — that refusal IS the feature.
    # `user-digest` is in this tuple for a reason that is easy to lose: the digest
    # USED to be `blocker` and was covered here by accident of that choice. Moving
    # it to its own category to fix rendering () would have silently
    # dropped it out of this gate — a protection lost as a side effect of an
    # unrelated fix, with nothing failing to announce it. It carries findings to a
    # human and quotes goal text, so it belongs here on the merits too.
    if args.category in ("blocker", "decision-needed", "user-digest"):
        if args.disproof_waived.strip():
            print("[notify] disproof-gate WAIVED: {r}".format(r=args.disproof_waived.strip()),
                  file=sys.stderr)
        else:
            gate = Path(__file__).resolve().parent / "finding-disproof-gate.py"
            if gate.exists():
                try:
                    gate_argv = [sys.executable, str(gate),
                                 "--claim", subject + "\n" + message,
                                 "--disproof-probe", args.disproof_probe,
                                 "--disproof-result", args.disproof_result]
                    if args.fenced_quotes:
                        gate_argv.append("--fenced-quotes")
                    proc = subprocess.run(
                        gate_argv, capture_output=True, text=True, timeout=20,
                    )
                    if proc.stderr:
                        print(proc.stderr.rstrip(), file=sys.stderr)
                    if proc.returncode == 1:
                        print("\n[notify] REFUSED to build payload — see the disproof gate "
                              "above. Run the falsifying command, then re-invoke with "
                              "--disproof-probe/--disproof-result. If the claim is genuinely "
                              "unfalsifiable-by-command AND time-critical, use "
                              "--disproof-waived \"<why>\".", file=sys.stderr)
                        sys.exit(4)
                    # rc 0 (pass / not gated) and rc 2 (gate input error) both proceed.
                except Exception as exc:  # noqa: BLE001 — fail-open, never block a real alert
                    print("[notify] disproof-gate error (failing open): {e!r}".format(e=exc),
                          file=sys.stderr)

    sections = parse_json_array("--sections-json", args.sections_json)
    next_steps = parse_json_array("--next-steps-json", args.next_steps_json)

    # Validate self.md structure (fails loud rc=3 on missing/malformed) but do
    # NOT prepend the extracted identity to the body —  plain-language
    # contract (user directive 2026-07-20 "PLEASE lower the cognitive load
    # for me here"). Agent→user emails must read like a competent human
    # assistant wrote them, not open with the second-person system-prompt block
    # "## Identity You are **<Agent>** — ..." (the exact text flagged in the
    # 2026-07-20 16:52 jargon-email incident). Identity is carried by the
    # transport subject prefix + domain sign-off convention instead. The
    # validation call is retained so a malformed self.md still fails loud (the
    # rc=3 guard this helper exists for; pinned by test_missing_self_md /
    # test_malformed_self_md).
    extract_self_identity(agent, project_root)
    body = message

    html_doc = ""
    if args.html_file:
        hp = Path(args.html_file)
        if not hp.exists():
            print(f"[notify] --html-file not found: {args.html_file}", file=sys.stderr)
            sys.exit(1)
        html_doc = hp.read_text(encoding="utf-8", errors="replace").strip()
        if not html_doc.lower().startswith(("<html", "<!doctype")):
            print("[notify] --html-file must be a complete HTML document (starts with <html> or <!DOCTYPE)",
                  file=sys.stderr)
            sys.exit(1)
        if args.category == "blocker":
            print("[notify] --html-file is not supported for --category blocker", file=sys.stderr)
            sys.exit(1)

    if args.category == "blocker":
        payload = {
            "ErrorMessage": body,
            "ErrorFrom": subject,
        }
    elif html_doc:
        # HTML passthrough: the transport sends InfoMessage as-is when it is a
        # complete document, and builds the subject from InfoType -- so the
        # subject IS the InfoType here. No Title key (that would select the
        # structured template and escape the HTML into a pre-wrap div).
        payload = {
            "InfoMessage": html_doc,
            "InfoType": subject,
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

    # Provenance stamp — email-send.sh refuses payloads without it ()
    # and pops it before the transport sees the payload.
    payload["XPayloadProvenance"] = "notify-build-payload/v1"

    # ensure_ascii=False so em-dashes and other unicode in self.md don't
    # become escape sequences in the email body. The downstream transport accepts UTF-8.
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
