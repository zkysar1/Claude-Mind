#!/usr/bin/env python3
"""close-review-inputs.py — build the inputs of a close review from the goal itself ().

/fresh-eyes-close hands close-review-verdict.py two files: --source-file (the goal's title,
description and verification) and --artifact-file (its outcome_note plus the commits that
deliver it). Reviewers built both by hand for every review: gap-257 counted 11 hand-built
sets by 2026-10-02, and one came out WRONG when a commit search found nothing and the
fallback showed an unrelated merge instead. This script builds both from the live goal
record and the repo's history, prints where each commit stands against the target branch,
and writes nothing except the input files. It never writes a verdict.

It also writes the outcome_note alone, for check 1's q4 sample (q4-provenance-sample.sh).
The artifact file is the wrong input for q4, which grades each claim by its citations: a
commit message is prose that cites nothing, and a diff header names a/ and b/ paths that no
session can fetch. Run on g-375-44's own artifact file (hostname cc-14, 2026-10-04), q4
reported missing-citation on commit-message prose and decorative-citation on the diff
headers, and on the note alone neither.

USAGE
  close-review-inputs.py --goal <goal-id> --out-dir <dir> [--repo <path>]
                         [--target-ref origin/main] [--no-fetch] [--include <path> ...]

  --repo defaults to the framework checkout this script lives in. For a product-repo goal,
  pass that repo's clone and its landing branch as --target-ref.
  --include adds a deliverable that no commit carries, such as a world tree node a
  measurement goal wrote, to the artifact verbatim, and to the q4 command as an artifact of
  its own. A world/ or meta/ path resolves on the reviewer's own box. Measured on g-375-69:
  its outcome_note plus its tree node gave exactly the reviewer's hand-built fidelity diff,
  and the note alone left two ids missing.

WHICH COMMITS ARE THE GOAL'S OWN
  The search is the delivery probe /fresh-eyes-close prescribes: every ref except
  refs/stash (a churn stash quotes the HEAD commit's subject), message matching the goal id
  with a digit guard, so g-375-10 never matches g-375-100. A commit whose SUBJECT names the
  goal is its own work and is carried into the artifact. A commit that names it only in the
  body is usually another goal's work citing this one; it is listed as CITES and left out,
  so the reviewer decides (guard-3541: read the citation context first).
  An own commit with no change outside the agent-state directory is not a deliverable. It
  is STATE-ONLY when it changes files there and nowhere else, an iteration commit carrying
  the closer's state, and rarely it changes no file at all. It keeps its COMMIT line and
  stays out of the artifact and its count. A merge is judged by the files it brought in over
  its first parent. A goal whose deliverable lives under that directory, a self.md edit say,
  reads the same way: pass the file with --include, or read the commit with git show.

WHAT IT LEAVES OUT (g-375-124)
  Agent state. A loop commit carries the closer's agent-state churn beside its deliverable,
  and the ids in that churn swamp check 2's fidelity diff both ways: they read as invented,
  and they supply cited ids the deliverable never carries, which then stop reading as
  missing. The churn was 89% of g-115-4215's artifact. So each commit is shown without its
  diff under the agent-state directory, _paths.AGENTS_PARENT_DIR (agent-dir-resolution.md),
  and a LEFT OUT line says what that removed. An empty AGENTS_PARENT_DIR is the legacy
  layout, agent dirs at the root among everything else: nothing separates them, and nothing
  is left out.
  Size. g-306-284's 499 own commits built a 305 MB artifact. One commit's output now stops at
  COMMIT_CAP bytes, with a line saying where, and once the artifact would pass ARTIFACT_CAP
  the commits after that are counted in a closing line, not shown. The outcome_note and the
  --include'd files come first and are kept whole. Every own commit keeps its COMMIT line.

WHAT IT PRINTS (stdout, before any verdict exists)
  FETCH       whether the target branch and the worker refs were refreshed. A stale target
              reads landed work as stranded (guard-5797), so a failed fetch is said aloud.
  COMMIT      one per own commit: sha, delivery verdict (commit-reachability.py), and for a
              commit not on the target, whether git cherry finds a patch-equivalent commit
              there. A "no" is not "absent": a squash of several commits, or a carry that
              adjusted a hunk, matches no single commit (guard-4009), so look for the added
              lines on the target as /fresh-eyes-close check 1 says. A merge or a root
              commit reads "unknown", since neither has a patch of its own to compare.
              A commit that is not a deliverable says why after its verdict.
  CITES       one per distinct subject among the commits that name the goal only in the
              body, with how many there are and the newest sha.
  BREADCRUMB  the record's commit_sha. iteration-close stamps the closer's HEAD at close
              (g-306-442), so on a store-only or investigation close it names the previous
              unit's commit, or a merge. Printed with its own verdict and whether its
              subject names this goal, never counted as the goal's commit.
  LEFT OUT    one per commit in the artifact whose agent-state diff was left out: how many
              files it changes there, and how many bytes git prints for that diff. For a
              merge the files are counted against its first parent, and the bytes are what
              git's combined diff, the one the artifact shows, would have printed.
  SOURCE / ARTIFACT / NOTE  the files written.
  NEXT        two commands, in the order /fresh-eyes-close runs them: q4 on the note and
              each --include'd file (check 1), then the producer's read-only probe on the
              source and artifact (check 2).

EXIT
  0  the inputs written, with at least one commit that names the goal in its subject and
     changes a file outside the agent-state directory, or one --include'd file
  5  the inputs written, but no commit names the goal in its subject, or none that does
     changes a file outside the agent-state directory, and nothing was --include'd, so the
     artifact holds the outcome_note alone. A decision goal can be like that legitimately; the code says so instead of
     reaching for some other commit.
  3  no live goal record for that id
  4  --repo is not a readable git checkout
  2  usage, including an --include file that cannot be read
"""
from __future__ import annotations

import argparse
import importlib.util
import re
import subprocess
import sys
import threading
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
# Where agent state lives is the framework's constant, never a literal path
# (agent-dir-resolution.md). The functions read it at call time, so a test can patch it.
from _paths import AGENTS_PARENT_DIR  # noqa: E402

EXIT_OK, EXIT_USAGE, EXIT_NO_GOAL, EXIT_NO_REPO, EXIT_NO_COMMIT = 0, 2, 3, 4, 5

#: Goal ids as the stores write them, including a lettered sub-goal (-a). Checked
#: before the id reaches a regex, so a malformed argument fails as usage, not as a search.
GOAL_ID_RE = re.compile(r"^g-\d+-\d+(?:-[a-z])?$")
#: A diff header line naming the before and after blobs, for example "index 19d155f..3b8a8e4 100644".
INDEX_LINE_RE = re.compile(r"^index [0-9a-f]{7,}\.\.[0-9a-f]{7,}(?: \d{6})?$")
#: The artifact's size caps (WHAT IT LEAVES OUT). Both stay under 1,000,000: the artifact
#: prints them, and seven digits in a row read as a git sha to the id regex check 2 diffs
#: with (goal_close_risk_tier.named_entities).
COMMIT_CAP, ARTIFACT_CAP = 250_000, 900_000
#: Bytes held back for the cut and closing lines, so the artifact stays inside ARTIFACT_CAP,
#: and the least room a commit is shown in, so a shown commit always carries its header.
CUT_RESERVE, MIN_ROOM = 1_000, 1_000


def _load(name: str, filename: str):
    """A sibling script loaded by path (the filenames are hyphenated), cached so each keeps
    ONE definition: the gate's goal lookup and the reachability verdicts are reused, never
    re-implemented here."""
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_goal(goal_id: str) -> dict:
    """The live goal record through close-review-gate.py's lookup, or {} when none."""
    return _load("close_review_gate", "close-review-gate.py").load_goal(goal_id, "world")


def _reach():
    return _load("commit_reachability", "commit-reachability.py")


def _git(repo: str, *args: str, timeout: int = 120) -> tuple[int, str]:
    """(rc, stdout) of one git call. Decoded as UTF-8 with replacement, because commit
    messages and diffs carry arbitrary bytes and a locale decode error would read as a
    failed probe. rc -1 means git could not run at all."""
    try:
        p = subprocess.run(["git", "-C", repo, *args], capture_output=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return -1, ""
    return p.returncode, p.stdout


def _git_capped(repo: str, cap: int, *args: str, timeout: int = 120) -> tuple[int, str, int]:
    """(rc, the first `cap` bytes of stdout decoded as _git decodes them, the count of bytes
    after those) of one git call. Read in pieces, so a diff is never held whole however
    large it is. rc -1 means git could not run, or ran past the timeout and was killed."""
    try:
        p = subprocess.Popen(["git", "-C", repo, *args], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL)
    except OSError:
        return -1, "", 0
    killed = threading.Event()

    def kill():
        killed.set()
        p.kill()

    timer = threading.Timer(timeout, kill)
    timer.start()
    kept, past = bytearray(), 0
    try:
        while True:
            chunk = p.stdout.read(65536)
            if not chunk:
                break
            keep = chunk[:max(0, cap - len(kept))]
            kept += keep
            past += len(chunk) - len(keep)
        rc = p.wait()
    finally:
        timer.cancel()
        p.stdout.close()
    # The timer can fire in the instant after git exits and before it is cancelled. That
    # kill hit nothing, so only a kill that ended git, with a nonzero rc, is a timeout.
    rc = -1 if killed.is_set() and rc != 0 else rc
    return rc, kept.decode("utf-8", errors="replace"), past


def names_goal(text: str, goal_id: str) -> bool:
    """True when `text` names exactly this goal id: not a longer id that starts with it
    (g-375-1 in g-375-10) and not a lettered sub-goal (g-377-50 in g-377-50-a)."""
    return re.search(rf"(?<![\w-]){re.escape(goal_id)}(?![\w-])", text) is not None


def find_commits(repo: str, goal_id: str) -> tuple[list[dict], list[dict]] | None:
    """(own, cites): the commits whose subject names the goal, oldest first, and those
    that name it only in the body. None when the log cannot be read.

    Records end in NUL (-z) because git refuses a NUL in a commit message and allows every
    other control byte, so a 0x1e or 0x1f delimiter inside a message could split a record
    and drop or demote a commit. Inside a record, %s is the whole first paragraph on one
    line, so the first two newlines end the sha and the subject."""
    rc, out = _git(repo, "log", "--exclude=refs/stash", "--all", "-z", "-E",
                   f"--grep={goal_id}([^0-9]|$)", "--format=%H%n%s%n%b")
    if rc != 0:
        return None
    own, cites = [], []
    for rec in out.split("\0"):
        sha, _, rest = rec.lstrip("\n").partition("\n")
        subject, _, body = rest.partition("\n")
        if not sha.strip():
            continue
        row = {"sha": sha.strip(), "subject": subject}
        if names_goal(subject, goal_id):
            own.append(row)
        elif names_goal(body, goal_id):
            cites.append(row)
    own.reverse()  # git log lists newest first; the artifact reads in commit order
    cites.reverse()
    return own, cites


def agent_state_split(repo: str, sha: str) -> tuple[list[str], list[str]] | None:
    """(under, outside): the files one commit changes under the agent-state directory and
    outside it. A merge is read against its first parent, so it lists the files it brought
    in, and a root commit lists all of its own. None for the legacy empty AGENTS_PARENT_DIR,
    where no prefix separates agent state from the rest, and when git cannot say; either
    way the commit is then shown whole, as before agent state was left out."""
    if not AGENTS_PARENT_DIR:
        return None
    rc, out = _git(repo, "show", "--format=", "--name-only", "-z",
                   "--diff-merges=first-parent", sha)
    if rc != 0:
        return None
    prefix = f"{AGENTS_PARENT_DIR}/"
    files = [f for f in out.split("\0") if f]
    return ([f for f in files if f.startswith(prefix)],
            [f for f in files if not f.startswith(prefix)])


def patch_equivalent_on_target(repo: str, sha: str, target: str) -> str:
    """'yes' when git cherry finds a commit on the target with this commit's patch (a
    worker-ref carry lands the same change under a new sha, guard-3541), 'no' when it finds
    none, 'unknown' when it cannot say. Only cherry's line for THIS sha counts: on a merge,
    `sha^..sha` also reaches the commits the second parent brought in, and cherry lists
    those while skipping the merge itself; on a root commit `sha^` does not exist."""
    rc, out = _git(repo, "cherry", target, sha, f"{sha}^")
    if rc != 0:
        return "unknown"
    for ln in out.splitlines():
        sign, _, listed = ln.strip().partition(" ")
        if listed.startswith(sha):
            return "yes" if sign == "-" else "no" if sign == "+" else "unknown"
    return "unknown"


def delivery(repo: str, sha: str, target: str) -> dict:
    """The reachability verdict for one sha, plus the patch-equivalence check when not LANDED."""
    reach = _reach()
    t = reach.triage(repo=repo, sha=sha, target_ref=target, do_fetch=False)
    row = {"verdict": t.get("verdict"), "landing_path": t.get("landing_path") or ""}
    if row["verdict"] != reach.LANDED:
        row["patch_equivalent_on_target"] = patch_equivalent_on_target(repo, sha, target)
    return row


def refresh(repo: str) -> list[str]:
    """Fetch the target branches and mirror the worker refs, the two fetches the delivery
    probe needs. Returns the failures, said aloud by the caller, never fatal: the objects
    may already be here, and the verdicts say INCONCLUSIVE where they are not."""
    failed = []
    rc, _ = _git(repo, "fetch", "--quiet", "--prune", "origin",
                 "+refs/heads/*:refs/remotes/origin/*", timeout=180)
    if rc != 0:
        failed.append("target branches")
    ok, _detail = _reach().fetch_worker_refs(repo, "origin", "workers")
    if not ok:
        failed.append("worker refs")
    return failed


def source_text(goal: dict) -> str:
    """The goal's own text only. Its id stays out: close-review-verdict.py exempts the goal's
    id on the artifact side (self_reference) but not on the source side, so an id this file
    added would read as missing from an artifact that never names it."""
    v = goal.get("verification") or {}
    lines = [f"TITLE: {goal.get('title') or ''}", "",
             "DESCRIPTION:", str(goal.get("description") or ""), "", "VERIFICATION OUTCOMES:"]
    lines += [f"- {o}" for o in (v.get("outcomes") or [])]
    lines += ["", "VERIFICATION CHECKS:"]
    lines += [f"- {c}" for c in (v.get("checks") or [])]
    return "\n".join(lines) + "\n"


def include_path(path: str) -> Path:
    """Where one --include file is, as an absolute path, so the printed q4 command reads it
    from any directory. A world/ or meta/ path resolves through _paths, the way an
    outcome_note names it; anything else is taken as given."""
    p = Path(path)
    if not p.is_absolute() and path.split("/", 1)[0] in ("world", "meta"):
        from _paths import resolve_file_path  # type: ignore
        p = resolve_file_path(path)
    return p.absolute()


def artifact_text(repo: str, goal_id: str, goal: dict, own: list[dict],
                  includes: list[tuple[str, str]] = (), no_deliverable: int = 0
                  ) -> tuple[str, list[str]]:
    """The artifact, and the LEFT OUT lines for main to print. `own` holds the commits to
    show, oldest first; `no_deliverable` counts the own commits left out for changing
    nothing outside the agent-state directory."""
    parts = [f"OUTCOME NOTE of {goal_id}:", str(goal.get("outcome_note") or "(none)"), ""]
    for label, text in includes:
        parts += [f"INCLUDED FILE {label}:", text, ""]
    parts += [f"COMMITS WHOSE SUBJECT NAMES {goal_id}: {len(own)}"
              + (f" (and {no_deliverable} with no change outside {AGENTS_PARENT_DIR}/, left out)"
                 if no_deliverable else "")]
    used = sum(len(p.encode("utf-8")) + 1 for p in parts)  # each part ends in a newline
    left_out, unshown = [], 0
    for c in own:
        room = min(COMMIT_CAP, ARTIFACT_CAP - used - CUT_RESERVE)
        if unshown or room < MIN_ROOM:
            unshown += 1
            continue
        sha = c["sha"]
        spec = ("--", f":(top,exclude){AGENTS_PARENT_DIR}/") if c.get("state_files") else ()
        rc, out, past = _git_capped(repo, room, "show", "--no-color",
                                    "--format=commit %H%n%n%B", "--patch", sha, *spec)
        if rc == 0 and not out and not past:
            # Under a pathspec git prints nothing at all for a merge it reads as unchanged
            # against one parent, such as a no-ff merge of a branch cut from the target's
            # tip. The header and message then stand alone.
            rc, out, past = _git_capped(repo, room, "show", "--no-color",
                                        "--format=commit %H%n%n%B", "--no-patch", sha)
        # A diff's "index <blob>..<blob>" lines carry blob hashes, which the fidelity check
        # reads as invented commit ids; they tell a reviewer nothing, so they are dropped.
        text = "\n".join(ln for ln in out.splitlines() if not INDEX_LINE_RE.match(ln))
        if rc != 0:
            text = f"commit {sha}\n(git show failed rc={rc})"
        elif past or len(text.encode("utf-8")) > room:
            # Cut again on the text: a byte git printed that is not UTF-8 decodes to a
            # three-byte replacement character, which can grow the text past the room.
            text = (text.encode("utf-8")[:room].decode("utf-8", errors="ignore")
                    + f"\n[close-review-inputs: diff cut at {room} bytes; git show prints "
                    f"this commit whole]")
        parts += ["", text]
        used += len(text.encode("utf-8")) + 2
        if spec:
            rc, _out, size = _git_capped(repo, 0, "show", "--no-color", "--format=",
                                         "--patch", sha, "--", f":(top){AGENTS_PARENT_DIR}/")
            if rc != 0:
                left_out.append(f"LEFT OUT {sha[:12]} {c['state_files']} file(s) under "
                                f"{AGENTS_PARENT_DIR}/, size unread (git show rc={rc})")
            elif size:
                left_out.append(f"LEFT OUT {sha[:12]} {c['state_files']} file(s), {size} "
                                f"bytes under {AGENTS_PARENT_DIR}/")
    if unshown:
        parts += ["", f"[close-review-inputs: the artifact reached its {ARTIFACT_CAP}-byte "
                      f"cap; {unshown} more commit(s) naming {goal_id} are not shown. The "
                      f"COMMIT lines list them.]"]
    return "\n".join(parts) + "\n", left_out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Build a close review's --source-file and --artifact-file, and the "
                    "outcome_note its q4 sample reads, from the goal record and its commits, "
                    "and print each commit's delivery verdict. Read-only: writes the input "
                    "files and no verdict.")
    ap.add_argument("--goal", required=True)
    ap.add_argument("--out-dir", required=True, help="where the input files are written")
    ap.add_argument("--repo", default=str(SCRIPT_DIR.parent.parent),
                    help="the clone holding the goal's commits (default: this checkout)")
    ap.add_argument("--target-ref", default="origin/main")
    ap.add_argument("--no-fetch", action="store_true",
                    help="skip refreshing the target and worker refs (offline or just fetched)")
    ap.add_argument("--include", action="append", default=[], metavar="PATH",
                    help="a deliverable no commit carries (a world tree node, say), added to "
                         "the artifact verbatim and to the q4 command; world/ and meta/ paths "
                         "resolve on this box")
    args = ap.parse_args(argv)

    gid = args.goal.strip()
    if not GOAL_ID_RE.match(gid):
        print(f"close-review-inputs: not a goal id: {gid!r}", file=sys.stderr)
        return EXIT_USAGE
    rc, _ = _git(args.repo, "rev-parse", "--git-dir")
    if rc != 0:
        print(f"close-review-inputs: not a readable git checkout: {args.repo}", file=sys.stderr)
        return EXIT_NO_REPO
    goal = load_goal(gid)
    if not goal:
        print(f"close-review-inputs: no live goal record for {gid}", file=sys.stderr)
        return EXIT_NO_GOAL
    includes, include_paths = [], []
    for path in args.include:
        try:
            p = include_path(path)
            includes.append((path, p.read_text(encoding="utf-8", errors="replace")))
        except (OSError, RuntimeError) as exc:
            print(f"close-review-inputs: cannot read --include {path}: {exc}", file=sys.stderr)
            return EXIT_USAGE
        include_paths.append(p)

    if args.no_fetch:
        print("FETCH skipped (--no-fetch): verdicts read the refs as they are in this clone")
    else:
        failed = refresh(args.repo)
        print("FETCH ok" if not failed else
              f"FETCH FAILED for {' and '.join(failed)}: a verdict below may read stale refs")

    found = find_commits(args.repo, gid)
    if found is None:
        print(f"close-review-inputs: git log failed in {args.repo}", file=sys.stderr)
        return EXIT_NO_REPO
    own, cites = found
    deliverable = []
    for c in own:
        d = delivery(args.repo, c["sha"], args.target_ref)
        extra = (f" patch-equivalent-on-target={d['patch_equivalent_on_target']}"
                 if "patch_equivalent_on_target" in d else "")
        split = agent_state_split(args.repo, c["sha"])
        c["state_files"] = len(split[0]) if split else 0
        if split is not None and not split[1]:  # no path outside the agent-state directory
            extra += (f" state-only ({len(split[0])} file(s), all under {AGENTS_PARENT_DIR}/):"
                      if split[0] else " changes no file:") + " left out of the artifact"
        else:
            deliverable.append(c)
        print(f"COMMIT {c['sha'][:12]} {d['verdict']}{extra} | {c['subject'][:100]}")
    # One line per distinct subject: a recurring goal that cites this one in every cycle's
    # commit body would otherwise print a line per cycle (44 for  on 2026-10-04).
    by_subject: dict[str, list[str]] = {}
    for c in cites:
        by_subject.setdefault(c["subject"], []).append(c["sha"])
    for subject, shas in by_subject.items():
        print(f"CITES  {len(shas)} commit(s), newest {shas[-1][:12]}, name {gid} only in the "
              f"body, left out of the artifact | {subject[:100]}")

    crumb = str(goal.get("commit_sha") or "").strip()
    if crumb:
        rc, subject = _git(args.repo, "log", "-1", "--format=%s", crumb)
        if rc != 0:
            print(f"BREADCRUMB commit_sha={crumb[:12]} is not a commit in this clone")
        else:
            d = delivery(args.repo, crumb, args.target_ref)
            print(f"BREADCRUMB commit_sha={crumb[:12]} {d['verdict']} "
                  f"subject-names-goal={names_goal(subject, gid)} (the closer's HEAD at "
                  f"close, not necessarily this goal's commit) | {subject.strip()[:100]}")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    src, art = out_dir / f"{gid}-source.txt", out_dir / f"{gid}-artifact.txt"
    note = out_dir / f"{gid}-note.txt"
    text, left_out = artifact_text(args.repo, gid, goal, deliverable, includes,
                                   len(own) - len(deliverable))
    src.write_text(source_text(goal), encoding="utf-8")
    art.write_text(text, encoding="utf-8")
    # The note with no header line: q4 reads a run of non-blank lines as one claim, so a
    # header naming the goal would lend the note's first paragraph a token it never carried.
    note.write_text(str(goal.get("outcome_note") or "").rstrip("\n") + "\n", encoding="utf-8")
    for line in left_out:
        print(line)
    print(f"SOURCE {src}")
    print(f"ARTIFACT {art}")
    print(f"NOTE {note}")
    # The queue's definition of whose work this is, so the NEXT line names the same closer
    # the review request row does.
    closer = (_load("close_review_queue", "close-review-queue.py").executor_of(goal)
              or "<the closer>")
    q4_artifacts = " ".join(f"--artifact {p}" for p in (note, *include_paths))
    print(f"NEXT bash core/scripts/q4-provenance-sample.sh --goal {gid} {q4_artifacts} "
          f"--source-file {src} --json")
    print(f"NEXT py -3 core/scripts/close-review-verdict.py --goal {gid} --reviewer <you> "
          f"--closer {closer} --source-file {src} --artifact-file {art}")
    if not deliverable and not includes:
        state = (f", except {len(own)} commit(s) with no change outside {AGENTS_PARENT_DIR}/, "
                 f"which deliver nothing," if own else "")
        print(f"NO COMMIT names {gid} in its subject{state} and nothing was --include'd: the "
              f"artifact holds the outcome_note only. Do not substitute another commit. Pass "
              f"the deliverable with --include if it lives outside the repo (a tree node, "
              f"say), or add the goal's own commits by hand if they exist under another "
              f"subject.")
        return EXIT_NO_COMMIT
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
