#!/usr/bin/env python3
"""outbound_data_class.py -- the outbound DATA-CLASS gate ().

WHY IT EXISTS. Two standing rules were honor-system, retrieved by the LLM and
checked by nothing: guard-4061 (the owner's PERSONAL email address is PII --
write it only in shape, `o***@e***.com`, never in full) and guard-4525 (the
owner's standing directive to watch for credentials reaching logs and
messages). No outbound chokepoint inspected the BODY: board-post.sh and
peer-board-post.sh had no content check at all, and the notification
dispatcher's checks decide whether the owner should hear from us, never what
the text carries. Origin: the Meta Muse address-leak audit (tree node
muse-address-leak-purpose-bound-permission, principle 1). This module is the
one shared checker the three chokepoints call:

    core/scripts/board-post.sh        --surface board-post       (subprocess)
    core/scripts/peer_board_post.py   --surface peer-board-post  (subprocess)
    core/scripts/notify_dispatch.py   surface "notify-user"      (in-process)

email-send.sh needs no call of its own: it is transport only and re-enters the
dispatcher, so every send except the audited EMAIL_SEND_ALLOW_DIRECT waiver
(which skips ALL checks by design) passes the dispatcher's call.

TWO CLASSES, nothing wider (the goal: "do not widen to arbitrary PII
detection"):

  owner-email  the owner's personal address (USER_EMAIL: the environment, then
               .env.local), in full,
               case-insensitive, UNANCHORED -- the predicate
               alert_dedup.redact_user_email applies at the alert goal writer
               (test_outbound_data_class.py pins the two to the same answers).
               The owner's work/role address and the fleet's own mailboxes
               are not matched, exactly as guard-4061 permits them in full.
  credential   a credential SHAPE, prefix plus length anchored: AWS access key
               id, GitHub token, product API key (lod_/vin_/ayo_ + 40 hex),
               the legacy ayo + 32 hex key, a Bearer token, a private key
               block, a model-provider key. The pre-commit scanner
               (check-no-hardcoded-secrets.sh) and alert_dedup carry the
               ancestors of these shapes. A token whose body is ONE repeated
               character (vin_ + 40 zeros) or that says EXAMPLE is a
               placeholder, not a credential: the fleet probes with
               fabricated correct-prefix keys on purpose (rb-11146).

FOUR VERDICTS, NOT TWO (guard-5287): allow / refuse / override / unavailable. A refusal
blocks work, so the gate's OWN dependency failure fails OPEN (guard-142) and
LOUDLY (guard-3737): USER_EMAIL unset means the owner-email class was NOT
checked, and that is printed and logged as fail_open rather than read as
"clean". A real match refuses. The CLI exits 0 for allow and unavailable and
REFUSAL_RC for a refusal, and nothing else may be read as a refusal -- an
uncaught traceback exits 1, so a callable bug must never look like a verdict.

THE REFUSAL TEXT IS A TEMPLATE FOR THE JUSTIFICATION IT RECEIVES (guard-5593,
rb-9686). It names the class and the fix first. It advertises the override only on
a branch where the invited justification can be TRUE: a credential-only refusal
(a documentation example or a fixture is a real false positive). An exact match of
the owner's address IS the address, so that refusal names the fix and no waiver --
the flag still works, ledgered, but a deny that names its bypass is taken at a
measurable rate. It prints the SHAPE of what matched, never the value (guard-4525):
the refusal lands in a transcript, so echoing a credential would leak it a second
time. The override records the finding classes and a hash of the body beside the
free-text reason (itself redacted), so a later reader can test the claim.

KNOWN LIMITS (g-306-571), stated so a clean verdict is not over-read: a body that
spells the address out ("at"/"dot"), URL-encodes it, or splits it across fields is
not matched; a credential with no recognisable prefix is not matched; a board write
that does not go through board-post.sh (g-306-571 scope: the daemon endpoint itself)
is not covered; the owner's address inside the envelope of an email (RecipientEmail,
g-306-571) is the destination, not content, and is skipped.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import os
import re
import sys
from dataclasses import dataclass, field

GATE_ID = "outbound-data-class-gate"
REFUSAL_RC = 5                  # CLI exit: refused. 0 = allow OR unavailable (fail-open).
OVERRIDE_FLAG = "--override-data-class"

CLASS_OWNER_EMAIL = "owner-email"
CLASS_CREDENTIAL = "credential"

# Payload keys that are the ENVELOPE of a message, not its content. The owner's
# address sits in RecipientEmail whenever the owner is mailed, by design.
ENVELOPE_KEYS = frozenset({"RecipientEmail", "FromEmail", "XPayloadProvenance"})

_MAX_LISTED = 8                 # findings named in a refusal before "(+N more)"

# Order matters for overlap suppression: the more specific shape is listed first,
# so "Bearer <product key>" reports the product key and not the Bearer wrapper.
_CREDENTIAL_SHAPES = (
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}(?![0-9A-Z])")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("product API key", re.compile(r"\b(?:lod|vin|ayo)_[0-9a-f]{40}(?![0-9a-f])")),
    ("legacy Ayoai API key", re.compile(r"ayo[0-9a-f]{32}", re.I)),   # == alert_dedup._API_KEY_SHAPE_RE
    ("model-provider API key", re.compile(r"\b(?:sk-[A-Za-z0-9_\-]{20,}|gsk_[A-Za-z0-9]{20,})")),
    ("private key block", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")),
    ("bearer token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=\-]{20,}")),
)
_PREFIX_RE = re.compile(r"^(?:AKIA|ASIA|gh[pousr]_|github_pat_|(?:lod|vin|ayo)_?|sk-|gsk_|Bearer\s+)", re.I)


@dataclass(frozen=True)
class Finding:
    """What matched and where -- never the matched value."""
    cls: str            # CLASS_OWNER_EMAIL | CLASS_CREDENTIAL
    kind: str           # "owner personal address" or the shape's name
    line: int           # 1-based line within the scanned text
    length: int         # length of the matched span
    where: str = ""     # the field it was found in ("" for a plain body)


@dataclass
class Verdict:
    decision: str                       # allow | refuse | override | unavailable
    findings: list = field(default_factory=list)
    message: str = ""                   # refusal text, ready for stderr
    notes: list = field(default_factory=list)   # WARN lines (a class that was NOT checked)


def shape_email(addr):
    """`operator@example.com` -> `o***@e***.com`; anything that is not a bare
    address comes back unchanged. Same algorithm as alert_dedup.shape_email --
    the ONLY form in which the owner's personal address may enter a durable
    record (guard-4061)."""
    m = re.match(r"^\s*([^@\s]+)@([^.\s]+)(?:\.[^\s.]+)*(\.[^\s.]+)\s*$", addr or "")
    if not m:
        return addr
    return m.group(1)[0] + "***@" + m.group(2)[0] + "***" + m.group(3)


def _is_placeholder(token):
    """A fabricated or documentation key: one repeated character after the
    prefix (vin_ + 40 zeros), or the word EXAMPLE (the AWS docs key)."""
    if "EXAMPLE" in token.upper():
        return True
    body = _PREFIX_RE.sub("", token, count=1)
    return bool(body) and len(set(body.lower())) == 1


def _owner_address(user_email):
    ue = (user_email or "").strip()
    return ue if "@" in ue else ""


def scan(text, *, user_email="", where=""):
    """Every finding in `text`, as shapes. Pure: no I/O, no environment."""
    if not text:
        return []
    findings = []
    owner = _owner_address(user_email)
    if owner:
        for m in re.finditer(re.escape(owner), text, flags=re.I):
            findings.append(Finding(CLASS_OWNER_EMAIL, "owner personal address",
                                    text.count("\n", 0, m.start()) + 1, len(m.group(0)), where))
    covered = []
    for kind, rx in _CREDENTIAL_SHAPES:
        for m in rx.finditer(text):
            if _is_placeholder(m.group(0)):
                continue
            if any(m.start() < e and s < m.end() for s, e in covered):
                continue
            covered.append((m.start(), m.end()))
            findings.append(Finding(CLASS_CREDENTIAL, kind,
                                    text.count("\n", 0, m.start()) + 1, len(m.group(0)), where))
    return findings


def redact(text, *, user_email=""):
    """`text` with every owner address shaped and every credential replaced by
    `<redacted:len=N>` -- for a free-text field that is about to be written to a
    durable ledger (an override justification naturally quotes its match)."""
    if not text:
        return text
    spans = []
    owner = _owner_address(user_email)
    if owner:
        spans += [(m.start(), m.end(), shape_email(owner)) for m in re.finditer(re.escape(owner), text, flags=re.I)]
    for _kind, rx in _CREDENTIAL_SHAPES:
        spans += [(m.start(), m.end(), "<redacted:len=%d>" % len(m.group(0)))
                  for m in rx.finditer(text) if not _is_placeholder(m.group(0))]
    out, pos = [], 0
    for s, e, rep in sorted(spans):
        if s < pos:
            continue
        out.append(text[pos:s])
        out.append(rep)
        pos = e
    out.append(text[pos:])
    return "".join(out)


def iter_strings(obj, path="", skip_keys=ENVELOPE_KEYS):
    """(field path, string) for every string VALUE in a nested payload. Keys are
    field names, not content, and envelope keys are skipped whole."""
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k in skip_keys:
                continue
            yield from iter_strings(v, f"{path}.{k}" if path else str(k), skip_keys)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from iter_strings(v, f"{path}[{i}]", skip_keys)


def refusal_message(findings, *, surface, user_email=""):
    lines = [f"{surface}: REFUSED by the outbound data-class gate (g-306-571) -- nothing was sent."]
    for f in findings[:_MAX_LISTED]:
        at = f"{f.where} line {f.line}" if f.where else f"line {f.line}"
        if f.cls == CLASS_OWNER_EMAIL:
            lines.append(f"  {f.cls:<12} {at}: the owner's personal address, in full -> write it in shape: "
                         f"{shape_email(user_email)}   (guard-4061)")
        else:
            lines.append(f"  {f.cls:<12} {at}: {f.kind}, {f.length} chars -> <redacted:len={f.length}>   (guard-4525)")
    if len(findings) > _MAX_LISTED:
        lines.append(f"  (+{len(findings) - _MAX_LISTED} more)")
    fixes = []
    if any(f.cls == CLASS_OWNER_EMAIL for f in findings):
        fixes.append("write the owner's address in shape")
    if any(f.cls == CLASS_CREDENTIAL for f in findings):
        fixes.append("name a credential by its variable NAME, never its value")
    lines.append("Fix the text and send it again: " + "; ".join(fixes) + ".")
    # The waiver is advertised only where its rationale can be TRUE (guard-5593). A credential-shaped
    # string can be a documentation example or a fixture. An exact match of the owner's address IS the
    # address, so no "it is not one" justification is reachable there: that branch names the fix and no
    # waiver (rb-9686: a bypass named in a deny is taken at a measurable rate). The flag still works.
    if all(f.cls == CLASS_CREDENTIAL for f in findings):
        lines.append(f"If a match is a FALSE POSITIVE -- not a live credential (a documentation example, a fixture) -- "
                     f"send it again with {OVERRIDE_FLAG} \"<why it is not live>\". The use is ledgered with the "
                     f"finding classes and a hash of the text. A live credential is never waived: rotate it.")
    return "\n".join(lines)


def _body_hash(texts):
    h = hashlib.sha256()
    for where, text in texts:
        h.update(where.encode("utf-8", errors="replace") + b"\0" + text.encode("utf-8", errors="replace") + b"\0")
    return h.hexdigest()[:12]


def _telemetry(decision, surface, findings, body_hash, override, error=None):
    """One gate-firings row for EVERY verdict, the clean ones included: a gate
    wrapped in a fail-open handler is indistinguishable from an inert one unless
    the call itself leaves a trace (guard-3737). Best-effort, never raises."""
    try:
        import _gate_log
        _gate_log.log(
            GATE_ID, {"allow": "noop", "refuse": "block", "override": "override", "unavailable": "fail_open"}[decision],
            caller=surface,
            trigger_matched=",".join(sorted({f.cls for f in findings})) or None,
            override_reason=override or None, gate_error=error,
            extra={"surface": surface, "finding_count": len(findings), "body_sha12": body_hash,
                   "kinds": sorted({f.kind for f in findings})})
    except Exception:
        pass


def _ledger_override(surface, justification, findings, body_hash):
    """One record in world/override-bypass-ledger.jsonl, the single-gate shape
    audit_cross_lane_claim writes (a `gate` field and no `slots_filled`)."""
    try:
        from _fileops import locked_append_jsonl
        from _paths import WORLD_DIR
        record = {
            "ts": _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "override_token": hashlib.sha1(justification.encode("utf-8", errors="replace")).hexdigest()[:12],
            "justification": justification[:1000],
            "gate": GATE_ID,
            "agent": os.environ.get("MIND_AGENT", "") or None,
            "session_id": os.environ.get("MIND_SID", "") or None,
            "context": {"surface": surface, "classes": sorted({f.cls for f in findings}),
                        "kinds": sorted({f.kind for f in findings}), "finding_count": len(findings),
                        "body_sha12": body_hash},
        }
        locked_append_jsonl(WORLD_DIR / "override-bypass-ledger.jsonl", record)
    except Exception as exc:
        print(f"[outbound-data-class] WARN: override ledger write failed: {exc}", file=sys.stderr)


def owner_email():
    """The owner's personal address, or "". The ENVIRONMENT first -- even when EMPTY, which
    means "deliberately none" -- then .env.local through the framework's own parser: tool
    shells do not auto-source .env.local (peer_board_post.py measured that for ENVIRONMENT_ID),
    and a gate whose main class silently does not run on such a box is a gate that is off there.
    Never raises."""
    if "USER_EMAIL" in os.environ:
        return os.environ["USER_EMAIL"].strip()
    try:
        import env as _env      # core/scripts/env.py -- the one .env.local parser
        return (_env.parse_local().get("USER_EMAIL") or "").strip()
    except Exception:
        return ""


def check(surface, texts, *, override="", user_email=None, record=True):
    """Judge `texts` (an iterable of (field path, text)) and leave the trail.

    Returns a Verdict. `refuse` means the caller must stop and print
    verdict.message; every other decision means proceed, and verdict.notes are
    WARN lines for a class that was not checked. `record=False` (a dry run) leaves
    no gate-firings row and no override-ledger row, and still returns the verdict.
    A text that appears under two field names is judged once, under the first."""
    seen, kept = set(), []
    for w, t in texts:
        if t and t not in seen:
            seen.add(t)
            kept.append((w, t))
    texts = kept
    if user_email is None:
        user_email = owner_email()
    notes = []
    if not _owner_address(user_email):
        notes.append(f"[{surface}] WARN: no USER_EMAIL in the environment or .env.local -- the outbound "
                     f"data-class gate did NOT check the owner-email class (credentials were checked).")
    findings = []
    for where, text in texts:
        findings += scan(text, user_email=user_email, where=where)
    body_hash = _body_hash(texts)
    override = (override or "").strip()
    if not findings:
        verdict = Verdict("unavailable" if notes else "allow", [], notes=notes)
        if record:
            _telemetry(verdict.decision, surface, [], body_hash, "",
                       error="user_email_unset: owner-email class not checked" if notes else None)
        return verdict
    if override:
        justification = redact(override, user_email=user_email)
        if record:
            _ledger_override(surface, justification, findings, body_hash)
            _telemetry("override", surface, findings, body_hash, justification)
        return Verdict("override", findings, notes=notes)
    if record:
        _telemetry("refuse", surface, findings, body_hash, "")
    return Verdict("refuse", findings, refusal_message(findings, surface=surface, user_email=user_email), notes)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="outbound_data_class", description="Refuse an outbound text that carries the owner's personal "
        "address or a credential. Text on stdin. Exit 0 = send it (allow, or the gate was unavailable); "
        f"{REFUSAL_RC} = refused.")
    ap.add_argument("--surface", required=True, help="the chokepoint calling (board-post, peer-board-post, ...)")
    ap.add_argument("--override", default="", help="justification: the match is a FALSE POSITIVE; ledgered")
    ap.add_argument("--no-record", action="store_true", help="a dry run: leave no firing row and no ledger row")
    args = ap.parse_args(argv)
    try:
        text = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        verdict = check(args.surface, [("", text)], override=args.override, record=not args.no_record)
    except Exception as exc:        # the gate's own failure fails OPEN, and says so (guard-142, guard-3737)
        print(f"[{args.surface}] WARN: outbound data-class gate UNAVAILABLE ({type(exc).__name__}: {exc}) "
              f"-- sending UNCHECKED.", file=sys.stderr)
        return 0
    for note in verdict.notes:
        print(note, file=sys.stderr)
    if verdict.decision == "refuse":
        print(verdict.message, file=sys.stderr)
        return REFUSAL_RC
    if verdict.decision == "override":
        print(f"[{args.surface}] outbound data-class gate OVERRIDDEN ({len(verdict.findings)} finding(s), "
              f"ledgered): sending.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
