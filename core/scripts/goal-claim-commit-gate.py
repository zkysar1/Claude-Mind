#!/usr/bin/env python3
"""Gate M2 — refuse a commit for a goal another SESSION currently holds.

THE GAP THIS CLOSES. `aspirations-claim.sh` already refuses a conflicting
claim (rc=2), and `claimed_by_sid` (g-115-3176) already separates two Bodies of
the SAME agent, which agent-name comparison structurally cannot. The protocol
works. It is simply never INVOKED by user-directed chat work: an agent told
"do this" in conversation executes, edits and commits without ever passing
through claim(). So the one execution path that most often runs beside an
autonomous loop is the one path with no coordination edge at all.

Measured 2026-08-18: two alpha Bodies independently built competing
implementations of g-115-6689 in the same evening — 27 markdown part files on
one side, a JSONL registry on the other. The other Body had claimed the goal on
the coordination board. Nothing connected "the user told me to do this" to "a
worker has claimed this", so neither Body could have known.

WHY THE COMMIT, AND NOT AN EARLIER MOMENT. Chat work has no phase structure to
hang a check on — there is no Phase 4, no selection step, no claim call. The
commit is the one point every execution path reaches, and goal ids are already
in commit messages by convention (measured: 32 of the last 60 commits carry
one). It is late — the work is already done — but a late warning that fires
beats an early one that has nowhere to live. This is a BACKSTOP; the claim call
remains the primary.

DECISION TABLE (first match wins):

  merge/cherry-pick/revert/rebase in progress -> allow (see in_replay: these
                                             REPLAY commits rather than author
                                             new work)
  no goal id in the message                -> allow  (nothing to check)
  MIND_SID unset                          -> allow  (see FAIL DIRECTION below)
  goal not in world or own-agent queue     -> allow  (abstain — unknown id)
  no claim on the goal                     -> allow  (nobody holds it)
  stored sid == my sid                     -> allow  (I hold it)
  stored sid absent                        -> allow  (abstain; matches aspirations.py:1697)
  claim older than STALE_GRACE_MINUTES     -> allow  (stranded-claim-sweep's territory)
  otherwise                                -> REFUSE (another live session holds it)

FAIL DIRECTION, stated rather than left to be rediscovered. `aspirations.py`
cmd_update_goal REFUSES when the request sid is absent but a stored one exists,
because there "unsetting MIND_SID defeats the guard". This gate ABSTAINS in
that case instead, deliberately:

  * A commit hook that refuses whenever an env var is missing wedges every
    commit on the box — including a human's, a CI runner's, and any recovery
    path. The blast radius of a false refusal here is the whole repo.
  * The bypass argument does not transfer. This gate already ships a sanctioned,
    audited override trailer, so a determined bypasser has a legitimate door and
    gains nothing from unsetting the variable. Refusing on absent-sid would buy
    no enforcement and cost fleet-wide commit availability.
  * It is a backstop. The primary enforcement (claim(), update_goal) keeps the
    strict semantics; a second net is allowed to be the loose one.

THREE RESIDUALS, named rather than left to be rediscovered as bugs:

  * A commit made with MIND_SID unset is not checked. Measured on this box,
    hook children DO inherit both MIND_AGENT and MIND_SID from the Bash tool
    call (bash-agent-inject.py injects them, git does not scrub the
    environment), so the abstain path is the exception rather than the rule.
  * Work that never COMMITS is unreachable from here. Chat work editing only
    world/ or meta/ (external, gitignored) produces no commit at all, so this
    gate can never fire on it. The adjacent cover there is store-dupe-warn,
    which catches the duplicate-ENCODING shape; the uncovered remainder is
    duplicated world/ prose, which is cheap to reconcile.
  * A commit that merely REFERENCES another session's live goal in its body
    ("follow-up to g-X") refuses like any other. This is the one residual that
    has actually been OBSERVED: `--audit` surfaced two such commits within an
    hour of shipping, both merely citing a goal a peer had since claimed. Both
    were replay artifacts (they predate the claim, so no refusal could have
    fired) — but they are exactly the shape that WILL fire eventually. The
    refusal text names this case as a legitimate override reason, so the cost
    is one trailer rather than a wrong decision.

Cost: ~39ms when the message carries no goal id (the short-circuit runs before
any store read), ~128ms when it does, against ~66ms for Gate M1 on the same
no-op. The world store is ~20MB across ~23 aspiration lines; a substring
pre-filter keeps json.loads off every line that mentions no wanted id.

Bypass:  goal-claim-override: <why this commit is correct anyway>
audited to world/override-bypass-ledger.jsonl, same shape as Gate M1.

CLI:
  (hook)  --commit-msg-file <path>   exit 1 = refuse, 0 = allow
  (audit) --audit [N]                replay the predicate over the last N
                                     commits. Read-only. Reports REACHABLE
                                     refusals separately from replay artifacts
                                     (a commit predating the claim it collides
                                     with could never have been refused); read
                                     the reachable count, not the raw one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

TAG = "[goal-claim-commit-gate]"
GATE_ID = "goal-claim-commit-gate"
DEFAULT_TRAILER = "goal-claim-override:"
MIN_JUSTIFICATION = 12

# A claim older than this is not treated as live. Deliberately equal to
# stranded-claim-sweep.py's DEFAULT_FOREIGN_SID_GRACE_MINUTES: that constant
# already answers "how long before a foreign session's claim stops counting",
# and a gate that refuses commits past the point the sweep would REAP the claim
# would wedge work on a dead holder. The two are pinned equal by
# test_goal_claim_commit_gate.py::test_stale_grace_matches_stranded_sweep, so a
# future divergence fails loudly instead of silently splitting the definition.
STALE_GRACE_MINUTES = 120

# Generous by design. Every match is resolved against the store by exact
# string, so an over-match costs one dict lookup and abstains; an UNDER-match
# silently skips the check. Covers , , -a and the
# g-xw-<ts>-NN cross-world form.
GOAL_ID_RX = re.compile(r"\bg-[A-Za-z0-9]+-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)?\b")


# ─── message parsing ─────────────────────────────────────────────────────────

def strip_comments(message: str) -> str:
    """Drop the `#` lines git appends; the hook sees the message pre-cleanup."""
    return "\n".join(ln for ln in message.splitlines() if not ln.startswith("#"))


def find_goal_ids(message: str) -> list:
    """Ordered-unique goal ids in the message body."""
    seen, out = set(), []
    for m in GOAL_ID_RX.finditer(strip_comments(message)):
        gid = m.group(0)
        if gid not in seen:
            seen.add(gid)
            out.append(gid)
    return out


def parse_override(message: str, trailer: str = DEFAULT_TRAILER):
    """Return (justification | None, note)."""
    rx = re.compile(r"^\s*" + re.escape(trailer) + r"\s*(.*?)\s*$", re.IGNORECASE)
    for line in message.splitlines():
        if line.startswith("#"):
            continue
        m = rx.match(line)
        if not m:
            continue
        just = m.group(1)
        if len(just) < MIN_JUSTIFICATION:
            return None, (f"trailer found but the justification is too short "
                          f"({len(just)} chars; need >= {MIN_JUSTIFICATION})")
        return just, ""
    return None, ""


# ─── store ───────────────────────────────────────────────────────────────────

def _queue_paths() -> list:
    """[(label, path)] for the queues a same-agent collision can appear in.

    World plus THIS agent's own queue. Two Bodies of one agent share an agent
    directory, so those two files cover the collision class exactly; a foreign
    agent's private queue is deliberately not scanned (it would need a
    cross-agent glob, and a foreign private goal is not a collision this gate
    can reason about).
    """
    out = []
    try:
        from _paths import WORLD_DIR, agent_dir  # type: ignore
    except Exception:
        return out
    if WORLD_DIR:
        out.append(("world", Path(WORLD_DIR) / "aspirations.jsonl"))
    name = (os.environ.get("MIND_AGENT") or "").strip()
    if name:
        try:
            out.append(("agent", Path(agent_dir(name)) / "aspirations.jsonl"))
        except Exception:
            pass
    return out


def load_claims(goal_ids, paths=None) -> dict:
    """{goal_id: {claimed_by, claimed_by_sid, claimed_at, status, title, source}}.

    Only goals that are actually present are returned — an absent key means
    "unknown id", which the caller treats as abstain. The substring pre-filter
    keeps this cheap: the world store is ~20 MB across ~23 aspiration lines, and
    only lines mentioning a wanted id are parsed.
    """
    wanted = set(goal_ids)
    found: dict = {}
    if not wanted:
        return found
    for label, path in (paths if paths is not None else _queue_paths()):
        try:
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            if not any(gid in line for gid in wanted - set(found)):
                continue
            try:
                asp = json.loads(line)
            except json.JSONDecodeError:
                continue
            for goal in asp.get("goals") or []:
                gid = goal.get("id")
                if gid in wanted and gid not in found:
                    found[gid] = {
                        "claimed_by": goal.get("claimed_by"),
                        "claimed_by_sid": goal.get("claimed_by_sid"),
                        "claimed_at": goal.get("claimed_at"),
                        "status": goal.get("status"),
                        "title": (goal.get("title") or "")[:80],
                        "source": label,
                    }
            if len(found) == len(wanted):
                return found
    return found


# ─── evaluation ──────────────────────────────────────────────────────────────

def _age_minutes(claimed_at, now=None):
    """Minutes since the claim, or None when unparseable/absent.

    Naive timestamps throughout — the fleet runs TZ=UTC by fiat (CLAUDE.md
    Naming Rules), so a naive claimed_at and a naive now() are the same clock.

    An AWARE claimed_at is normalised to UTC-naive rather than rejected. It
    parses fine and then explodes one line later on `now - then` with
    "can't subtract offset-naive and offset-aware datetimes" — a TypeError that
    escapes evaluate(), gets swallowed by run_gate's fail-open handler, and
    silently disables the gate for that commit. Since the fleet is UTC, the
    conversion is exact, so normalising is strictly better than discarding.
    A peer deployment or any non-framework writer can produce the aware form.
    """
    then = _to_naive(claimed_at)
    if then is None:
        return None
    if now is None:
        now = datetime.now()
    return (now - then).total_seconds() / 60.0


def _to_naive(stamp):
    """Parse an ISO stamp to a UTC-naive datetime, or None if unparseable.

    Two call sites: _age_minutes (claim age) and _predates (audit dating), and
    the audit's `%cI` git dates are ALWAYS offset-bearing, so the aware branch
    is exercised on every audit run rather than being defensive-only.
    """
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _predates(commit_iso, claimed_at) -> bool:
    """True when the commit was made BEFORE the claim it collides with.

    Such a hit is an artifact of replaying history against today's claim state,
    never a refusal the gate could have produced: it only ever sees a commit at
    the moment that commit is made.
    """
    a, b = _to_naive(commit_iso), _to_naive(claimed_at)
    if a is None or b is None:
        return False
    return a < b


def evaluate(goal_ids, claims, my_sid, grace_minutes=STALE_GRACE_MINUTES, now=None):
    """Return (conflicts, checked). A conflict is a live foreign-session claim."""
    conflicts, checked = [], 0
    if not my_sid:
        return conflicts, checked
    now = now or datetime.now()
    for gid in goal_ids:
        rec = claims.get(gid)
        if rec is None:
            continue
        checked += 1
        held_sid = (rec.get("claimed_by_sid") or "").strip()
        if not held_sid or held_sid == my_sid:
            continue
        age = _age_minutes(rec.get("claimed_at"), now)
        if age is not None and age > grace_minutes:
            continue
        conflicts.append({
            "goal_id": gid,
            "claimed_by": rec.get("claimed_by"),
            "claimed_by_sid": held_sid,
            "claimed_at": rec.get("claimed_at"),
            "age_minutes": None if age is None else round(age, 1),
            "status": rec.get("status"),
            "title": rec.get("title"),
            "source": rec.get("source"),
        })
    return conflicts, checked


def refusal_text(conflicts, my_sid, trailer=DEFAULT_TRAILER, note="") -> str:
    me = (my_sid or "")[:8]
    lines = [f"{TAG} REFUSED — another session holds a live claim on "
             f"{'this goal' if len(conflicts) == 1 else 'these goals'}.", ""]
    for c in conflicts:
        age = "unknown age" if c["age_minutes"] is None else f"{c['age_minutes']:.0f}m ago"
        lines.append(f"  {c['goal_id']}  [{c['source']} queue, status={c['status']}]")
        if c["title"]:
            lines.append(f"    {c['title']}")
        lines.append(f"    claimed by {c['claimed_by'] or '<unnamed>'} "
                     f"session {c['claimed_by_sid'][:8]} ({age}); this session is {me}")
    lines += [
        "",
        "A DIFFERENT session — possibly another Body of your own agent, which is why",
        "the agent NAME cannot tell you apart — is executing this goal right now.",
        "Committing here is how two Bodies build the same thing twice.",
        "",
        "Do one of these:",
        "  1. Check what the holder has done:  bash core/scripts/board-read.sh \\",
        "       --channel coordination --tag <goal-id> --since 24h",
        "  2. Work a different goal, and let the holder finish this one.",
        "  3. If the holder is genuinely dead, release the claim rather than",
        "     bypassing:  bash core/scripts/stranded-claim-sweep.py --help",
        "  4. If this commit is correct anyway (a doc fix, a shared dependency,",
        "     a merge of the holder's own work), add a trailer — it is audited to",
        "     world/override-bypass-ledger.jsonl:",
        f"       {trailer} <why this commit is correct despite the claim>",
        f"       (iteration-commit.sh: --message \"{trailer} <why>\")",
    ]
    if note:
        lines += ["", f"  note: {note}"]
    return "\n".join(lines)


# ─── ledger ──────────────────────────────────────────────────────────────────

def _ledger_path():
    from _paths import WORLD_DIR  # type: ignore
    if not WORLD_DIR:
        raise RuntimeError("WORLD_DIR unresolved")
    return Path(WORLD_DIR) / "override-bypass-ledger.jsonl"


def write_ledger(conflicts, justification: str, message: str) -> str:
    """Append the override record. Returns "" on success, else a WARN reason."""
    try:
        from _fileops import locked_append_jsonl  # type: ignore
        subject = next((ln.strip() for ln in message.splitlines()
                        if ln.strip() and not ln.startswith("#")), "")
        record = {
            "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "gate": GATE_ID,
            "override_token": hashlib.sha1(
                justification.encode("utf-8", errors="replace")).hexdigest()[:12],
            "justification": justification[:1000],
            "agent": os.environ.get("MIND_AGENT") or None,
            "session_id": os.environ.get("MIND_SID") or None,
            "context": {
                "caller": "core/githooks/commit-msg",
                "commit_subject": subject[:200],
                "conflicts": conflicts,
            },
        }
        locked_append_jsonl(_ledger_path(), record)
        return ""
    except Exception as e:  # never wedge the commit on the audit write
        return f"ledger write failed: {e}"


# ─── git ─────────────────────────────────────────────────────────────────────

_REPLAY_SENTINELS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD",
                     "rebase-merge", "rebase-apply")


def in_replay(repo: Path) -> bool:
    """True during a merge, cherry-pick, revert or rebase.

    All four REPLAY existing commits rather than author new work, so the
    question this gate asks — "are you about to duplicate work a peer holds?" —
    does not apply: the commit already exists, and its message carries whatever
    goal id the ORIGINAL author wrote. Gate M1 skips merges on the same
    reasoning ("merges combine already-gated commits"); the other three are the
    same shape. A revert is the sharpest case: refusing to let someone undo a
    commit because a peer holds that goal's claim is exactly backwards, and it
    would land during an incident, which is the worst possible moment.

    WHICH replay states actually reach this gate, measured rather than assumed:
    a CLEAN cherry-pick or revert never fires commit-msg at all (git reuses the
    original message), and a clean `cherry-pick -n` writes no sentinel — right,
    since the follow-up `git commit` is an ordinary commit the caller authors
    and SHOULD be checked. Only a CONFLICTED replay both leaves its sentinel and
    then fires commit-msg on the resolving commit. So this covers exactly the
    reachable set; pinning the clean form would have tested an unreachable path.

    Measured 0 occurrences across 300 commits, and this repo's push path never
    rebases — so this is FP-prevention for a class that has not yet fired, kept
    because the failure is silent-and-confusing rather than loud.

    One `rev-parse --git-dir` plus local stat()s: cheaper than the five
    subprocess calls a per-sentinel `rev-parse --verify` would need, and it
    handles worktrees, where .git is a file rather than a directory.
    """
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--git-dir"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return False
    git_dir = Path(r.stdout.strip())
    if not git_dir.is_absolute():
        git_dir = repo / git_dir
    return any((git_dir / name).exists() for name in _REPLAY_SENTINELS)


# ─── hook mode ───────────────────────────────────────────────────────────────

def run_gate(repo: Path, msg_file, out=sys.stdout) -> int:
    if in_replay(repo):
        return 0
    try:
        message = Path(msg_file).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"{TAG} WARN: message unreadable ({e}) — allowing commit", file=out)
        return 0

    goal_ids = find_goal_ids(message)
    if not goal_ids:
        return 0

    my_sid = (os.environ.get("MIND_SID") or "").strip()
    if not my_sid:
        return 0  # abstain — see FAIL DIRECTION in the module docstring

    try:
        claims = load_claims(goal_ids)
        conflicts, _checked = evaluate(goal_ids, claims, my_sid)
    except Exception as e:
        print(f"{TAG} WARN: could not evaluate claims ({e}) — allowing commit", file=out)
        return 0

    if not conflicts:
        return 0

    justification, note = parse_override(message)
    if justification:
        warn = write_ledger(conflicts, justification, message)
        print(f"{TAG} OVERRIDE accepted — "
              f"{', '.join(c['goal_id'] for c in conflicts)} "
              f"— recorded to override-bypass-ledger.jsonl"
              + (f" (WARN: {warn})" if warn else ""), file=out)
        return 0

    print(refusal_text(conflicts, my_sid, note=note), file=out)
    return 1


# ─── audit mode ──────────────────────────────────────────────────────────────

def run_audit(repo: Path, limit: int, out=sys.stdout) -> int:
    """Replay the predicate over recent commits — the false-positive measurement.

    Reports the survival count at each stage, not just the final refusal count:
    a refusal total is uninterpretable without the population it was drawn from
    (guard-4054).

    READ THE COUNT AS AN UPPER BOUND, NOT AN FP RATE. Old commits are judged
    against TODAY's claims, so a goal claimed after a commit landed scores as a
    would-refuse that could never have happened — the gate only ever sees a
    commit at the moment it is made. Measured live: the count went 0 -> 2 within
    one hour purely because a peer claimed a goal that two already-landed
    commits happened to mention. Each hit therefore prints the commit date
    beside the claim time, and a hit whose commit PREDATES its claim is marked
    `[pre-dates claim]` — it is an artifact of the replay, not a false positive
    the gate could ever have produced.
    """
    # %cI (committer date) rides along so each hit can be dated against the
    # claim it collides with — see the pre-dates-claim note in the docstring.
    r = subprocess.run(
        ["git", "-C", str(repo), "log", f"-{limit}",
         "--format=%H%x00%an%x00%cI%x00%s%x00%b%x1e"],
        capture_output=True, text=True)
    if r.returncode != 0:
        print(f"{TAG} audit: git log failed: {r.stderr.strip()}", file=out)
        return 1

    my_sid = (os.environ.get("MIND_SID") or "").strip()
    commits = [c for c in r.stdout.split("\x1e") if c.strip()]
    with_ids, all_ids = [], set()
    for c in commits:
        parts = c.strip("\n").split("\x00")
        if len(parts) < 5:
            continue
        sha, author, cdate, subject, body = (parts[0][:9], parts[1], parts[2],
                                             parts[3], parts[4])
        ids = find_goal_ids(subject + "\n" + body)
        if ids:
            with_ids.append((sha, author, cdate, subject, ids))
            all_ids.update(ids)

    claims = load_claims(sorted(all_ids))
    would_refuse, artifacts = [], 0
    for sha, author, cdate, subject, ids in with_ids:
        conflicts, _ = evaluate(ids, claims, my_sid or "AUDIT-SENTINEL-SID")
        if not conflicts:
            continue
        # TWO artifact classes, both "could never have been refused in
        # production", both measured live rather than theorised:
        #   pre-dates  — the commit is older than the claim it collides with;
        #                the gate only ever sees a commit at the moment it is made.
        #   own-work   — the commit's AUTHOR is the agent holding the claim, i.e.
        #                the holder committing its own work. In ITS session the
        #                sid matches and the gate allows; only this replay, which
        #                judges every commit against the CURRENT session's sid,
        #                scores it as a conflict.
        # Author-name matching is a heuristic and is stated as one: git records
        # an author, never a session id, so a same-agent SECOND Body is
        # indistinguishable here and gets excused with the holder. That biases
        # toward UNDER-reporting, which is the right direction for a number
        # whose job is to catch a noisy predicate.
        stale = all(_predates(cdate, c.get("claimed_at")) for c in conflicts)
        own = (not stale) and all(
            (c.get("claimed_by") or "") == author for c in conflicts)
        if stale or own:
            artifacts += 1
        kind = "pre-dates claim" if stale else ("holder's own commit" if own else "")
        would_refuse.append((sha, author, cdate, subject, conflicts, kind))

    real = len(would_refuse) - artifacts
    print(f"{TAG} audit over the last {len(commits)} commits", file=out)
    print(f"  carry a goal id      : {len(with_ids)}", file=out)
    print(f"  distinct goal ids    : {len(all_ids)}", file=out)
    print(f"  ids found in a queue : {len(claims)}", file=out)
    print(f"  would refuse (raw)   : {len(would_refuse)}", file=out)
    print(f"    of which artifacts : {artifacts}  (pre-dates the claim, or is "
          f"the holder's own commit — unreachable in production)", file=out)
    print(f"  REACHABLE REFUSALS   : {real}", file=out)
    for sha, author, cdate, subject, conflicts, kind in would_refuse:
        held = ", ".join(f"{c['goal_id']}@{c['claimed_by_sid'][:8]}"
                         f"({'?' if c['age_minutes'] is None else int(c['age_minutes'])}m)"
                         for c in conflicts)
        mark = f" [{kind}]" if kind else ""
        print(f"    {sha} {author[:12]:12s} {cdate[:19]} {subject[:44]:44s} "
              f"[{held}]{mark}", file=out)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Gate M2 — goal-claim commit backstop")
    ap.add_argument("--commit-msg-file", help="commit-msg hook shape: path to the message file")
    ap.add_argument("--audit", nargs="?", const=100, type=int,
                    help="replay over the last N commits (default 100) and report")
    args = ap.parse_args(argv)

    repo_r = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                            capture_output=True, text=True)
    if repo_r.returncode != 0:
        print(f"{TAG} WARN: not a git repo — allowing", file=sys.stdout)
        return 0
    repo = Path(repo_r.stdout.strip())

    if args.audit is not None:
        return run_audit(repo, args.audit)
    if args.commit_msg_file:
        return run_gate(repo, args.commit_msg_file)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
