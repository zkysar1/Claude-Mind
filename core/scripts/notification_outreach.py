#!/usr/bin/env python3
"""notification_outreach.py -- fleet-wide "have we already told the user this?"

The user's complaint (2026-08-16, verbatim): "each time these agents reach out
to me they need to do a full scan of if anyone else has reached out to me about
this before, or ask themselves if they really need to send it. I'm getting the
same question three different times, sometimes from two different worlds."

Root cause, measured (rb-7986): the send site (world/scripts/email-send.sh)
wrote NO record of any kind, so the only dedup that existed was notify-user's
per-agent working-memory check -- exact subject, 30-minute window, invisible to
every other agent and to every other world. Three agents (or two worlds) each
passed their own check and each sent.

This module is the shared memory the send site lacked:

  record  -- append one row to world/notifications-sent.jsonl (S3-synced, so
             every agent on every box in THIS world reads the same ledger) and,
             best-effort, mirror a compact `user-outreach` line onto each
             reachable PEER world's coordination board so the other world's
             gate can see it too.
  check   -- before sending, look for prior outreach on the SAME TOPIC in the
             ledger AND in local board posts tagged `user-outreach` (which is
             where peer mirrors land), inside a category-specific window.
             exit 0 = no prior outreach (send) | exit 1 = duplicate (prior rows
             printed) | exit 2 = usage.
  list    -- print recent rows (audit).

Topic identity is deliberately FUZZY, because the same question rarely arrives
with an identical subject: normalized-subject equality, token-Jaccard on the
subject, shared entity ids (goal/guard/rb/pq ids), or a body-fingerprint
overlap all count. False positives cost one suppressed email that is still
recorded and re-routable; false negatives are exactly the complaint.

Windows: blocker 24h (a persistent outage may re-alert daily), everything
else 7 days (a question asked once a week is one question, not three).

PII: the recipient is the user's PERSONAL address (guard-4061) -- the ledger
stores it in SHAPE only (o***@e***.com), never in full.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _paths import WORLD_DIR, ENVIRONMENT_ID, PROJECT_ROOT  # noqa: E402

LEDGER_NAME = "notifications-sent.jsonl"
BOARD_TAG = "user-outreach"

WINDOW_HOURS = {
    "blocker": 24,
    "user-digest": 20,   # one fleet digest per day-or-two, whoever's cadence fires first
    "_default": 24 * 7,
}
# Digest categories are matched by CATEGORY alone, fleet-wide: any digest sent
# inside the window by ANY agent/world is the prior. And a digest names dozens
# of goal/guard ids, so it must never be the "shared ids"/"body overlap" prior
# for a specific ask (or every follow-up would be refused as a duplicate of the
# digest); nor may a specific ask suppress the next digest.
DIGEST_CATEGORIES = frozenset({"user-digest"})
SUBJECT_JACCARD = 0.6
BODY_JACCARD = 0.7
BODY_FP_CHARS = 400

_ID_RE = re.compile(r"\b(?:g-\d{1,3}-\d{1,4}|asp-\d+|guard-\d+|rb-\d+|pq-[a-z0-9-]+|sq-\d+|hyp-[a-z0-9-]+)\b", re.I)
_STOP = {
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "are",
    "was", "were", "be", "it", "this", "that", "with", "from", "by", "at", "as",
    "your", "you", "we", "i", "our", "has", "have", "had", "not", "no", "do",
    "does", "did", "please", "re", "fw", "fwd", "update", "report", "alert",
    "notification", "ayoai", "agent", "agents",
}
_TS_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[t ]\d{2}:\d{2}(?::\d{2})?)?\b", re.I)
_HEX_RE = re.compile(r"\b[0-9a-f]{7,40}\b", re.I)


# --------------------------------------------------------------------------
# normalisation
# --------------------------------------------------------------------------

def strip_agent_prefix(subject: str) -> str:
    """'[Alpha] Foo' -> 'Foo'. The transport prepends the sender tag; two agents
    asking the same thing differ ONLY by that tag."""
    return re.sub(r"^\s*(?:\[[^\]]{1,32}\]\s*)+", "", subject or "")


def normalize_subject(subject: str) -> str:
    s = strip_agent_prefix(subject).lower()
    s = _TS_RE.sub(" ", s)
    s = _HEX_RE.sub(" ", s)
    s = re.sub(r"[^a-z0-9\-]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def tokens(text: str) -> set:
    return {t for t in re.split(r"[^a-z0-9\-]+", (text or "").lower()) if len(t) > 2 and t not in _STOP}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def entity_ids(*texts: str) -> set:
    out = set()
    for t in texts:
        out.update(m.lower() for m in _ID_RE.findall(t or ""))
    return out


def identity_ids(subject: str, body: str = "") -> set:
    """Ids that IDENTIFY the message: SUBJECT-sourced only, never body citations.

    g-115-8026. `entity_ids` unions subject and body, so an id named in the body
    purely as provenance becomes the message's dedup identity. Measured
    2026-08-27 (g-115-8005, zeta, cc-02): agent-watchdog's memory-pressure alert
    cites (g-115-4699) in its BODY as an explanatory cross-reference, so all 675
    ledger records carried the identical entity_ids tuple and collapsed into ONE
    topic fleet-wide -- rc {0:1, 4:674}: one delivery in a week, for one box,
    while the SUBJECT was correctly box-scoped (21 distinct subject_norm) and
    was never consulted by the shared-ids leg.

    The perverse gradient is the point: the more carefully an alert cites its
    provenance, the more aggressively it is suppressed. This module already knew
    the shape -- DIGEST_CATEGORIES exists because "a digest names dozens of
    goal/guard ids, so it must never be the shared-ids prior". Same defect, a
    different trigger: one cited id rather than dozens.

    NO BODY FALLBACK, deliberately. A fallback ("subject ids, else body ids")
    was written first and measured as a NO-OP on the exact case above: neither
    subject contains an id, so both sides fall back to the body and collide
    identically. `body` is accepted and ignored to keep the call shape obvious
    at the call sites and to make that decision visible here rather than in a
    diff.
    """
    return entity_ids(subject)


def body_fingerprint(body: str) -> str:
    """Normalized head of the body -- enough to recognise the same message
    re-sent under a reworded subject, small enough to keep the ledger light."""
    b = (body or "")
    b = _TS_RE.sub(" ", b)
    b = _HEX_RE.sub(" ", b)
    b = re.sub(r"\s+", " ", b).strip()
    return b[:BODY_FP_CHARS]


def shape_email(addr: str) -> str:
    """operator@example.com -> o***@e***.com (final label only). PII guard-4061."""
    m = re.match(r"^\s*([^@\s]+)@([^.\s]+)(?:\.[^\s.]+)*(\.[^\s.]+)\s*$", addr or "")
    if not m:
        return "" if not addr else "***"
    return m.group(1)[0] + "***@" + m.group(2)[0] + "***" + m.group(3)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)


def _parse_ts(s: str):
    try:
        return datetime.fromisoformat(str(s)[:19])
    except Exception:
        return None


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

def ledger_path(world: Path | None = None) -> Path:
    return Path(world or WORLD_DIR) / LEDGER_NAME


def _read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    try:
        from _fileops import read_jsonl_with_recovery  # noqa: WPS433
        return list(read_jsonl_with_recovery(path) or [])
    except Exception:
        rows = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
        return rows


def _append(path: Path, row: dict) -> None:
    try:
        from _fileops import locked_append_jsonl  # noqa: WPS433
        locked_append_jsonl(path, row)
    except Exception as exc:  # never lose the record: raw append fallback
        print(f"[notification-outreach] WARN locked append failed ({exc}); raw append", file=sys.stderr)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _board_outreach_rows(world: Path, since: datetime) -> list:
    """Rows synthesised from board posts tagged `user-outreach` -- the surface
    where PEER worlds' mirrors land (and where our own mirror would land on
    theirs). Author `<agent>@<env>` is parsed back into env/agent."""
    out = []
    for name in ("coordination.jsonl", "findings.jsonl", "general.jsonl"):
        for m in _read_jsonl(world / "board" / name):
            tags = m.get("tags") or []
            if isinstance(tags, str):
                tags = re.findall(r"[a-z0-9\-]+", tags.lower())
            if BOARD_TAG not in [str(t).lower() for t in tags]:
                continue
            ts = _parse_ts(m.get("timestamp"))
            if not ts or ts < since:
                continue
            text = m.get("text") or ""
            subj = ""
            cat = ""
            mm = re.search(r"subject:\s*(.+?)(?:\n|$)", text)
            if mm:
                subj = mm.group(1).strip()
            mc = re.search(r"category:\s*([a-z\-]+)", text)
            if mc:
                cat = mc.group(1)
            author = str(m.get("author") or "")
            env, agent = "", author
            if "@" in author:
                agent, env = author.split("@", 1)
            out.append({
                "id": m.get("id"), "ts": ts.isoformat(), "env": env or "peer",
                "agent": agent, "category": cat, "subject": subj,
                "subject_norm": normalize_subject(subj),
                "entity_ids": sorted(entity_ids(subj, text)),
                "identity_ids": sorted(identity_ids(subj, text)),
                "body_fp": body_fingerprint(text), "source": f"board:{name}",
            })
    return out


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------

def window_for(category: str) -> timedelta:
    return timedelta(hours=WINDOW_HOURS.get((category or "").lower(), WINDOW_HOURS["_default"]))


def match_reason(cand: dict, subject_norm: str, subj_tokens: set, ids: set, body_fp: str,
                 ids_all: set | None = None) -> str | None:
    # `ids` = identity (subject-sourced). `ids_all` = subject+body union, used
    # only for the weak leg below; defaults to `ids` so older callers behave as
    # if no body citations existed (conservative: fewer weak matches).
    ids_all = ids if ids_all is None else ids_all
    cn = cand.get("subject_norm") or normalize_subject(cand.get("subject") or "")
    if subject_norm and cn == subject_norm:
        return "same subject"
    j = jaccard(subj_tokens, tokens(cn))
    if j >= SUBJECT_JACCARD:
        return f"subject overlap {j:.2f}"
    #  + guard-3152: `identity_ids` ships in the same change that
    # starts consuming it, so it is present on new rows and ABSENT on every row
    # written before -- the field cannot by itself mark its own boundary. The
    # remedy is NOT backfill (that manufactures readings nobody took): derive
    # the absent side from what its absence can only mean -- a pre-split row,
    # whose identity was the old subject+body union -- and LABEL the derivation
    # in the reason string so a reader is never handed an inference wearing the
    # costume of a reading.
    # STRONG identity: ids named in BOTH subjects. These bind regardless of
    # phrasing -- "Blocker on " and "Update re " are one
    # topic. guard-3152: identity_ids ships in the same change that consumes it,
    # so it is absent on every pre-existing row and cannot mark its own
    # boundary. Do NOT backfill (that manufactures readings nobody took);
    # derive the absent side from what its absence can only mean -- a pre-split
    # row whose identity was the subject+body union -- and LABEL the derivation
    # so a reader is never handed an inference dressed as a reading.
    cids_all = set(cand.get("entity_ids") or [])
    if "identity_ids" in cand:
        cand_identity = set(cand.get("identity_ids") or [])
        derived = ""
    else:
        cand_identity = set(cand.get("entity_ids") or [])
        derived = " (DERIVED — prior row predates identity_ids; used its subject+body union)"
    # Strong when the id is IDENTITY on EITHER side: this side's subject ids
    # against the candidate's full ids, or the candidate's subject ids against
    # this side's full ids. One party naming the id in its SUBJECT makes it the
    # topic, and the other party citing that same id in its body is then talking
    # about the same thing -- which is exactly the cross-agent case this module
    # exists for ("I'm getting the same question three different times"), where
    # one agent writes "Should we retire the legacy PK? ()" and another
    # writes "Your call: retiring the legacy identity PK" with the id in the body.
    # Only when the id is body-only on BOTH sides is it a mere citation.
    strong = (ids & cids_all) | (cand_identity & ids_all)
    if strong:
        return "shared ids " + ",".join(sorted(strong)[:4]) + derived

    # WEAK identity: ids that appear only in the BODIES.  -- a body
    # citation must not override a subject that has already said these are
    # different topics. `j` above is the subject Jaccard, so reaching here means
    # the subjects are neither equal nor similar. When BOTH sides carry a real
    # subject, that disagreement is the more specific signal and it wins: fall
    # through to the body-fingerprint leg, which still catches a genuine re-send
    # under a reworded subject. Without this the 674 suppressed memory-pressure
    # alerts stay suppressed -- neither subject names an id, and both bodies
    # cite the same tracking goal.
    cids = cids_all
    subjects_disagree = bool(subject_norm) and bool(cn) and j < SUBJECT_JACCARD
    shared = ids_all & cids
    if shared and not subjects_disagree:
        return "shared ids " + ",".join(sorted(shared)[:4]) + derived
    if shared and subjects_disagree:
        return None   # cited-only overlap under disagreeing subjects: not one topic
    if ids_all and cids and not shared:
        # Both sides name entities and none coincide: two DIFFERENT topics that
        # merely share phrasing (two outages, two goals). Body-shape overlap
        # must not override an explicit id disagreement.
        return None
    if body_fp and cand.get("body_fp"):
        jb = jaccard(tokens(body_fp), tokens(cand["body_fp"]))
        if jb >= BODY_JACCARD:
            return f"body overlap {jb:.2f}"
    return None


def find_prior(subject: str, body: str, category: str, *, world: Path | None = None,
               now: datetime | None = None, exclude_suppressed: bool = True) -> list:
    world = Path(world or WORLD_DIR)
    now = now or _now()
    since = now - window_for(category)
    subject_norm = normalize_subject(subject)
    subj_tokens = tokens(subject_norm)
    ids = identity_ids(subject)          # : subject-sourced identity
    ids_all = entity_ids(subject, body)  # full union, for the weak leg only
    fp = body_fingerprint(body)
    rows = []
    for r in _read_jsonl(ledger_path(world)):
        if exclude_suppressed and r.get("suppressed_duplicate_of"):
            continue
        ts = _parse_ts(r.get("ts"))
        if not ts or ts < since:
            continue
        rows.append(r)
    rows.extend(_board_outreach_rows(world, since))
    hits = []
    is_digest = (category or "").lower() in DIGEST_CATEGORIES
    for r in rows:
        cand_digest = (r.get("category") or "").lower() in DIGEST_CATEGORIES
        if is_digest != cand_digest:
            continue  # digests and specific asks never suppress each other
        if is_digest:
            why = "digest already sent this window"
        else:
            why = match_reason(r, subject_norm, subj_tokens, ids, fp, ids_all)
        if why:
            hits.append({**{k: r.get(k) for k in ("id", "ts", "env", "agent", "category", "subject", "source", "goal_id")}, "why": why})
    hits.sort(key=lambda h: h.get("ts") or "", reverse=True)
    return hits


# --------------------------------------------------------------------------
# record + mirror
# --------------------------------------------------------------------------

def build_record(*, agent: str, category: str, subject: str, body: str, goal_id: str = "",
                 transport: str = "", rc: int | None = None, to: str = "",
                 suppressed_duplicate_of: str = "", override_reason: str = "",
                 env: str = "", now: datetime | None = None) -> dict:
    now = now or _now()
    stamp = now.strftime("%Y%m%dT%H%M%S")
    digest = hashlib.sha1((subject or "").encode() + b"\0" + (body or "").encode()).hexdigest()
    rec = {
        "id": f"ntf-{stamp}-{agent or 'unknown'}-{digest[:6]}",
        "ts": now.isoformat(),
        "env": env or ENVIRONMENT_ID or "",
        "agent": agent or os.environ.get("MIND_AGENT", "") or "unknown",
        "category": category or "",
        "subject": strip_agent_prefix(subject or "")[:300],
        "subject_norm": normalize_subject(subject or ""),
        "entity_ids": sorted(entity_ids(subject, body)),
        "identity_ids": sorted(identity_ids(subject, body)),
        "goal_id": goal_id or "",
        "body_fp": body_fingerprint(body),
        "body_sha1": digest,
        "to": shape_email(to),
        "transport": transport or "",
        "rc": rc,
    }
    if suppressed_duplicate_of:
        rec["suppressed_duplicate_of"] = suppressed_duplicate_of
    if override_reason:
        rec["duplicate_override_reason"] = override_reason[:300]
    return rec


def _peer_ids() -> list:
    reg = PROJECT_ROOT / "core" / "config" / "environments"
    ids = []
    for f in sorted(reg.glob("*.yaml")):
        eid = f.stem
        if eid in ("local", (ENVIRONMENT_ID or "")):
            continue
        ids.append(eid)
    return ids


def mirror_to_peers(rec: dict, *, dry_run: bool = False) -> list:
    """Best-effort: one compact `user-outreach` line on each reachable peer's
    coordination board so THEIR gate can see this send. Unreachable peers
    (peer-board-post rc=3 -- no configured path from this box) are skipped
    silently; the mirror never affects the send's exit code."""
    text = (f"USER-OUTREACH env: {rec.get('env')} agent: {rec.get('agent')} "
            f"category: {rec.get('category')} subject: {rec.get('subject')}\n"
            f"ids: {' '.join(rec.get('entity_ids') or []) or '-'} ledger: {rec.get('id')}\n"
            f"(mirrored so peer worlds do not re-ask the user the same thing)")
    results = []
    script = HERE / "peer-board-post.sh"
    if not script.exists():
        return results
    try:
        from _runtime_bash import bash_cmd  # noqa: WPS433
    except Exception:
        return results
    for peer in _peer_ids():
        argv = bash_cmd(str(script), "--peer", peer, "--channel", "coordination",
                        "--type", "status", "--tags", f"{BOARD_TAG},{rec.get('category') or 'info'}")
        if dry_run:
            argv.append("--dry-run")
        try:
            p = subprocess.run(argv, input=text, capture_output=True, text=True, timeout=60)
            results.append({"peer": peer, "rc": p.returncode})
        except Exception as exc:
            results.append({"peer": peer, "rc": -1, "error": str(exc)[:120]})
    return results


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _read_body(args) -> str:
    if getattr(args, "body_file", None):
        p = Path(args.body_file)
        return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
    if getattr(args, "body", None) == "-":
        return sys.stdin.read()
    return getattr(args, "body", "") or ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--agent", default=os.environ.get("MIND_AGENT", ""))
        sp.add_argument("--category", default="")
        sp.add_argument("--subject", required=True)
        sp.add_argument("--body", default="", help="inline body, or '-' for stdin")
        sp.add_argument("--body-file", default="")
        sp.add_argument("--goal-id", default="")
        sp.add_argument("--world", default="", help="override world dir (tests)")
        sp.add_argument("--json", action="store_true")

    c = sub.add_parser("check", help="exit 1 if prior outreach on this topic exists")
    common(c)
    r = sub.add_parser("record", help="append a send (or suppressed duplicate) to the ledger")
    common(r)
    r.add_argument("--transport", default="")
    r.add_argument("--rc", type=int, default=None)
    r.add_argument("--to", default="", help="recipient; stored in SHAPE only")
    r.add_argument("--suppressed-duplicate-of", default="")
    r.add_argument("--override-reason", default="")
    r.add_argument("--mirror-peers", action="store_true")
    r.add_argument("--dry-run-mirror", action="store_true")
    ls = sub.add_parser("list")
    ls.add_argument("--since-hours", type=float, default=24 * 7)
    ls.add_argument("--world", default="")
    ls.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)
    world = Path(args.world) if getattr(args, "world", "") else None

    if args.cmd == "list":
        since = _now() - timedelta(hours=args.since_hours)
        rows = [x for x in _read_jsonl(ledger_path(world)) if (_parse_ts(x.get("ts")) or datetime.min) >= since]
        rows += _board_outreach_rows(Path(world or WORLD_DIR), since)
        rows.sort(key=lambda x: x.get("ts") or "")
        if args.json:
            print(json.dumps(rows, indent=1))
        else:
            for x in rows:
                print(f"{x.get('ts')}  {x.get('env') or '-':<12} {x.get('agent') or '-':<8} {x.get('category') or '-':<16} {x.get('subject')}"
                      + ("  [SUPPRESSED]" if x.get("suppressed_duplicate_of") else ""))
        return 0

    body = _read_body(args)
    if args.cmd == "check":
        hits = find_prior(args.subject, body, args.category, world=world)
        if args.json:
            print(json.dumps({"duplicate": bool(hits), "prior": hits, "window_hours": window_for(args.category).total_seconds() / 3600}, indent=1))
        elif hits:
            print(f"[notification-outreach] DUPLICATE: {len(hits)} prior outreach on this topic within "
                  f"{int(window_for(args.category).total_seconds() // 3600)}h:")
            for h in hits[:8]:
                print(f"  - {h['ts']}  {h.get('env') or '-'}/{h.get('agent') or '-'}  [{h.get('category') or '-'}]  "
                      f"{(h.get('subject') or '')[:90]}  ({h['why']}; {h.get('source') or 'ledger'} {h.get('id')})")
            print("  Ask: does my message ADD anything the user has not already been told? "
                  "If not, do not send. If yes, reference the prior note and send with "
                  "EMAIL_SEND_ALLOW_DUPLICATE='<what is new>'.")
        else:
            print("[notification-outreach] no prior outreach on this topic -- ok to send")
        return 1 if hits else 0

    if args.cmd == "record":
        rec = build_record(agent=args.agent, category=args.category, subject=args.subject, body=body,
                           goal_id=args.goal_id, transport=args.transport, rc=args.rc, to=args.to,
                           suppressed_duplicate_of=args.suppressed_duplicate_of,
                           override_reason=args.override_reason)
        _append(ledger_path(world), rec)
        mirror = []
        if args.mirror_peers and not rec.get("suppressed_duplicate_of"):
            mirror = mirror_to_peers(rec, dry_run=args.dry_run_mirror)
        if args.json:
            print(json.dumps({"record": rec, "mirror": mirror}, indent=1))
        else:
            print(f"[notification-outreach] recorded {rec['id']}" + (f" (mirror: {mirror})" if mirror else ""))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
