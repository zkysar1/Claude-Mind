#!/usr/bin/env python3
"""embedding-index-freshness.py — per-box staleness tick for the retrieval
embedding index (g-306-84; wired into iteration-close.sh productivity-check
beside agent-watchdog --tick / monitor-tick).

Why a LOCAL tick and not a recurring goal: the index is PER-BOX state
(mind_api/state/retrieval-embedding-index/ — the daemon's cache), while
recurring goals are world-scoped and execute on exactly ONE box per firing.
A tick that runs wherever a loop runs keeps every box's own index fresh.

Behavior (all paths fail-open; this must never delay loop continuation):
  1. `embedding_blend_enabled` false (tree.yaml retrieval:) → exit 0 silent.
     The whole check costs one small YAML read while the feature is off.
  2. No index on disk → exit 0 silent. This default mode never builds one:
     only incremental freshness (--update: re-embeds changed docs only,
     typically seconds) runs from iteration-close and the daemon. The INITIAL
     full build (minutes to tens of minutes of CPU embedding) belongs to the
     --session-start mode below.
  3. Index fresh (meta.json mtime >= newest source-store mtime) AND the
     index's model matches tree.yaml `embedding_model_name` → exit 0.
  4. Stale OR model-drifted, debounce clear → record the attempt marker, spawn
     `embedding-index-build.py --update` DETACHED (never waits), print one
     JSON status line. Debounce is SUCCESS-AWARE (g-115-3684): after an attempt
     that landed (meta.json rewritten after the marker) the next spawn may come
     SUCCESS_INTERVAL_SECONDS later; after one that did not (failed, or still
     running) the window is DEBOUNCE_SECONDS — a persistently-failing update
     retries next window instead of storming, and a running one is never
     doubled.

Trigger sites: iteration-close.sh productivity-check, the SessionStart hook
(--session-start, below), AND the daemon's
/v1/retrieve endpoint (rate-limited there). Until 2026-09-29 the loop close was
the only one, under a flat 6h debounce — so a box in assistant mode, or a loop
between iterations, never refreshed at all, and a busy one refreshed at most
every 6h. A lesson written at 09:00 was invisible to semantic retrieval until
the next window; one written on a quiet box stayed invisible indefinitely.

Index dir precedence: EMBED_FRESHNESS_INDEX_DIR (this tick's own test seam) >
MIND_EMBEDDING_INDEX_DIR (the READER's seam, _embedding_retrieval) > the
per-box default. Honoring the reader's knob keeps the tick and the reader on
the SAME index: a process whose reader is redirected (the test suite points it
at a nonexistent dir) can never refresh the production index from its own
corpus.

MODEL DRIFT IS A STALENESS CONDITION IN ITS OWN RIGHT (cdb3288607,
2026-09-03), and the corpus-mtime test cannot see it. An index built on a
model other than the configured one scores on a different cosine
distribution, so the floors and `embedding_cosine_bonus_weight` — all
measured on `embedding_model_name` — are silently wrong; retrieve.py freezes
the embedding lanes while that holds. The heal is `--update`, but a QUIET box
(no recent store or tree writes) never reached step 4, so the freeze would
have been permanent there. Drift is therefore checked BEFORE the mtime test.
The debounce still applies: when the configured model cannot load, the
updater refuses rc=2 with the index untouched and this retries next window.

This docstring claimed until cdb3288607 that "the updater itself pins the
index's existing model (g-306-82), so a config model change can never be
half-applied by this tick." That pinning is exactly what let a drifted index
survive five weeks: the updater now rebuilds on the CONFIGURED model instead.

SESSION-START MODE (g-306-574). Until this mode, step 2 above was a rule that
the initial build is never spawned from a hook, and it left a box that never
runs the loop and never serves a retrieve on the token baseline for good: both
other triggers return at "no index". Measured by omni on a ZDS box that hosts
only chat sessions (cc-12; relayed in g-306-574's description, not re-measured
here): no index and every query token-only since the daemon started on
2026-09-02, with 16 of 119 known guardrails and reasoning-bank entries found in
the top 20 by their own words, against 110 of 119 once provisioned.
`--session-start`, called from
sessionstart-orchestrator.sh, does what the default mode may not:
  - blend off → silent. Blend on and THIS interpreter cannot import the encoder
    stack (_vendor_path.stack_absent_reason) → print ONE line saying so and
    naming the provisioning recipe (guard-1427), whatever the index state: a
    box with an index and no numpy serves token-only too.
  - Stack present, no meta.json → take the attempt claim with O_CREAT|O_EXCL
    (two sessions starting together must not start two builds), spawn
    `embedding-index-build.py --build` DETACHED, niced, and print ONE line
    saying it is building, where the log is, and that retrieval is token-only
    meanwhile. A claim younger than DEBOUNCE_SECONDS prints one line saying an
    attempt is recorded and has not landed. A claim older than that is taken
    over (the build never produced meta.json) by renaming it away, which has
    one winner like the create.
  - Index present → silent. Refreshing one stays with the two triggers above.
The spawn passes no --model: embedding_model_name is the calibration anchor
(guard-5905). It defaults HF_HUB_OFFLINE and TRANSFORMERS_OFFLINE to 0 for the
child, because the builder's own offline default cannot fetch the model on a
box whose cache is empty (guard-1427); a value the operator exported wins. The
child's output goes to UPDATE_LOG, and a build that is still running when its
claim expires would be doubled (g-115-11544 tracks serializing builds).
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

#  watch-set roots that live under the REPO (the WORLD_DIR
# conventions root joins them at call time inside _source_mtime). Hoisted to a
# module constant so tests can monkeypatch them away: sweeping the real repo
# from inside the function made every consumer test time-dependent -- red for
# exactly 1h after ANY commit touching these roots (measured 2026-08-21,
# cc-13: a convention refreshed at 08:31 by a fleet merge vs a fixture index
# aged to 08:12 flipped the REFUSE-case test to would_spawn=True).
_FRAMEWORK_MD_ROOTS = (
    SCRIPT_DIR.parent.parent / ".claude" / "rules",
    SCRIPT_DIR.parent.parent / "core" / "config" / "conventions",
)
sys.path.insert(0, str(SCRIPT_DIR))

DEBOUNCE_SECONDS = 6 * 3600  # after an attempt that did NOT land: one per 6h
SUCCESS_INTERVAL_SECONDS = 10 * 60  # after one that landed: next may spawn in 10 min
INDEX_DIR = SCRIPT_DIR.parent.parent / "mind_api" / "state" / "retrieval-embedding-index"
UPDATE_LOG = SCRIPT_DIR.parent / "logs" / "embedding-index-update.log"
INITIAL_BUILD_NICE = 10  # the first build pegs every core for minutes: yield to the session
STACK_RECIPE = "pip install --target ~/.ayoai-vendor/py fastembed (guard-1427)"


_CFG_CACHE = []


def _retrieval_cfg():
    """The `retrieval:` block of tree.yaml, or {} if unreadable.

    Memoized for the life of the process (this is a one-shot tick, so the
    config cannot change under it) so that serving both the flag read and the
    configured-model read costs ONE parse, not two — the drift check adds no
    YAML work to a tick that exists to be cheap. Fail-quiet by contract: every
    caller treats {} as "cannot tell", never as "disabled and undrifted"."""
    if _CFG_CACHE:
        return _CFG_CACHE[0]
    try:
        import yaml
        cfg_path = SCRIPT_DIR.parent / "config" / "tree.yaml"
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        out = cfg.get("retrieval") or {}
    except Exception:
        out = {}
    _CFG_CACHE.append(out)
    return out


def _blend_enabled():
    """Kept ZERO-ARG deliberately: every test in
    test_embedding_index_freshness.py monkeypatches this with `lambda: False`
    / `lambda: True`, so giving it a parameter that main() then passes would
    break 14 tests at the call site rather than at an assertion."""
    return bool(_retrieval_cfg().get("embedding_blend_enabled", False))


def _model_drifted(meta_path, cfg=None):
    """True when the index names a model other than the configured one.

    Reads only `meta.json` (already stat-ed by the caller) and the config dict
    the caller already holds. Fail-quiet: an unreadable meta, an absent
    `model` key, or an absent `embedding_model_name` returns False — this tick
    may never manufacture a rebuild out of a missing signal. `--stats` is the
    diagnostic surface; this is the automatic heal trigger.

    """
    cfg = _retrieval_cfg() if cfg is None else cfg
    configured = cfg.get("embedding_model_name")
    if not configured:
        return False
    try:
        indexed = (json.loads(meta_path.read_text(encoding="utf-8")) or {}).get("model")
    except Exception:
        return False
    return bool(indexed) and indexed != configured


def _source_mtime():
    """Newest mtime of the corpus source stores.

    The watch-set MUST mirror embedding-index-build.load_corpus, which indexes
    guardrails + reasoning-bank + KNOWLEDGE TREE NODES. It did not until
    g-115-3763: the tree was in the corpus but not here, so a tree-only
    encoding never marked the index stale and the new node stayed invisible to
    retrieve.sh until an unrelated rb/guardrail write happened to fire the
    tick. Measured (bravo, cc-05, 2026-07-28): 85 unindexed entries had
    accumulated, and three queries that should have matched a freshly-added
    node returned it in 0/15 results each — then at ranks 7/4/4 after a manual
    --update. The node content was fine; only the index was stale.

    BOTH tree surfaces are watched because the embedded text is
    humanized-key + _tree.yaml summary + the node .md's first body paragraph
    (build.tree_doc_text): a new node or a summary edit moves _tree.yaml, while
    a body edit moves only the .md. Watching either one alone leaves the other
    class of edit silently unindexed.

    The .md sweep is a stat-only rglob — 11.8ms over 1291 nodes measured on
    cc-04, against a tick that already runs once per iteration close. An
    over-trigger costs one incremental --update (seconds); an under-trigger
    costs invisibility, so the sweep deliberately does not filter out
    non-node .md files that may sit under the tree root."""
    try:
        from _paths import WORLD_DIR
    except Exception:
        return None
    newest = None
    # pattern-signatures.jsonl joined load_corpus 2026-09-29 (), so it
    # joins this list in the same change — the  rule.
    for name in ("reasoning-bank.jsonl", "guardrails.jsonl",
                 "pattern-signatures.jsonl"):
        p = Path(WORLD_DIR) / name
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        newest = m if newest is None else max(newest, m)
    tree_root = Path(WORLD_DIR) / "knowledge" / "tree"
    try:
        m = (tree_root / "_tree.yaml").stat().st_mtime
        newest = m if newest is None else max(newest, m)
    except OSError:
        pass
    try:
        for p in tree_root.rglob("*.md"):
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            newest = m if newest is None else max(newest, m)
    except OSError:
        pass
    # : framework docs joined the corpus, so they join the watch-set
    # in the SAME change — this is the  decision rule applied
    # prospectively rather than after the fact. _FRAMEWORK_MD_ROOTS + the
    # WORLD_DIR conventions root here mirror
    # retrieve._framework_file_sources, which is the SSOT; keep them in step.
    #
    # Hardcoded rather than imported ON PURPOSE, and the asymmetry with
    # embedding-index-build.py (which calls R._build_framework_index directly)
    # is deliberate: the builder ALREADY imports retrieve, so sharing there is
    # free, while this module is a per-iteration-close hook that imports only
    # _paths lazily. Pulling all of retrieve.py in for three directory names
    # would put a large import on a tick that exists to be cheap. A missed
    # framework edit costs one stale-index window; the import costs every tick.
    for root in _FRAMEWORK_MD_ROOTS + (Path(WORLD_DIR) / "conventions",):
        try:
            for p in root.rglob("*.md"):
                try:
                    m = p.stat().st_mtime
                except OSError:
                    continue
                newest = m if newest is None else max(newest, m)
        except OSError:
            continue
    return newest


def _index_dir():
    return Path(os.environ.get("EMBED_FRESHNESS_INDEX_DIR")
                or os.environ.get("MIND_EMBEDDING_INDEX_DIR") or INDEX_DIR)


def _spawn_detached(args, env=None, low_priority=False):
    """Start `args` detached (never waited on), its output appended to UPDATE_LOG.

    Raises on a spawn failure; the callers decide how loud that is."""
    UPDATE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(UPDATE_LOG, "ab") as log_f:
        kwargs = {"stdout": log_f, "stderr": log_f,
                  "cwd": str(SCRIPT_DIR.parent.parent)}
        if env is not None:
            kwargs["env"] = env
        if os.name == "nt":
            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: survives the
            # parent bash exiting (nohup/disown are flaky on Git Bash).
            kwargs["creationflags"] = 0x00000008 | 0x00000200
        else:
            kwargs["start_new_session"] = True
            if low_priority and hasattr(os, "nice"):
                kwargs["preexec_fn"] = lambda: os.nice(INITIAL_BUILD_NICE)
        subprocess.Popen(args, **kwargs)


def _claim_initial_build(marker, now):
    """Take the right to start THE initial build, atomically.

    Returns (True, None) when this call owns the attempt, else (False,
    attempted) with the claim's mtime (None when it could not be read). The
    create is the arbiter: O_CREAT|O_EXCL has one winner however many sessions
    start together. A claim past DEBOUNCE_SECONDS belongs to an attempt that
    never produced meta.json (that is what "initial" means here), so it is
    taken over by renaming it away, which also has exactly one winner."""
    marker.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(3):  # create; take over a stale claim; create again
        try:
            fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(time.strftime("%Y-%m-%dT%H:%M:%S"))
            return True, None
        try:
            attempted = marker.stat().st_mtime
        except OSError:
            continue  # the holder vanished between create and stat: create again
        if now - attempted < DEBOUNCE_SECONDS:
            return False, attempted
        stale = "%s.stale-%d" % (marker, os.getpid())
        try:
            os.rename(str(marker), stale)
        except OSError:
            return False, attempted  # another session took it over first
        try:
            os.unlink(stale)
        except OSError:
            pass
    return False, None


def _first_build_env():
    """The child's env for the initial build. The builder and the reader both
    setdefault HF_HUB_OFFLINE=1, right for the query path and wrong for the one
    run that must fetch the model into an empty cache (guard-1427). Defaulting
    the pair to 0 here lets that run work; an exported value wins."""
    env = dict(os.environ)
    env.setdefault("HF_HUB_OFFLINE", "0")
    env.setdefault("TRANSFORMERS_OFFLINE", "0")
    return env


def _stamp(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(epoch))


def session_start(dry_run=False):
    """The --session-start decision. Returns {"status": ..., "message": ...};
    message None means say nothing. Every path is fail-open, and "cannot tell"
    (a probe that raises, a claim that cannot be recorded) is silence, never a
    build."""
    if not _blend_enabled():
        return {"status": "off", "message": None}
    try:
        from _vendor_path import stack_absent_reason
        absent = stack_absent_reason()
    except Exception:
        return {"status": "probe-failed", "message": None}
    if absent:
        return {"status": "stack-absent", "message": (
            "[embedding-index] retrieval is TOKEN-ONLY on this box: the semantic blend is on "
            "fleet-wide but %s in this interpreter. Provision with: %s. The next session "
            "start then builds the index if there is none." % (absent, STACK_RECIPE))}
    index_dir = _index_dir()
    if (index_dir / "meta.json").exists():
        return {"status": "index-present", "message": None}
    try:
        won, attempted = _claim_initial_build(index_dir / ".last-update-attempt",
                                              time.time())
    except OSError:
        return {"status": "claim-failed", "message": None}
    if not won:
        when = ("at %s " % _stamp(attempted)) if attempted else ""
        until = ("; the next attempt is allowed after %s" % _stamp(attempted + DEBOUNCE_SECONDS)
                 if attempted else "")
        return {"status": "attempt-recorded", "message": (
            "[embedding-index] an initial index build was started %sand has not landed (still "
            "running, or failed): see %s. Retrieval is TOKEN-ONLY until meta.json exists%s."
            % (when, UPDATE_LOG, until))}
    building = (
        "[embedding-index] no index on this box, so retrieval is TOKEN-ONLY until one exists: "
        "building it now in the background (minutes to tens of minutes), log %s. The first "
        "build may download the model once; export HF_HUB_OFFLINE=1 to forbid that."
        % UPDATE_LOG)
    if dry_run:
        return {"status": "would-spawn", "message": building}
    args = [sys.executable, str(SCRIPT_DIR / "embedding-index-build.py"), "--build"]
    if index_dir != INDEX_DIR:
        args += ["--out", str(index_dir)]
    try:
        _spawn_detached(args, env=_first_build_env(), low_priority=True)
    except Exception as exc:
        try:
            (index_dir / ".last-update-attempt").unlink()  # no process: not an attempt
        except OSError:
            pass
        return {"status": "spawn-failed", "message": (
            "[embedding-index] no index on this box and the initial build could not be "
            "started: %s" % str(exc)[:160])}
    return {"status": "spawned", "message": building}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Per-box retrieval embedding index tick.")
    ap.add_argument("--session-start", action="store_true",
                    help="SessionStart hook mode: build the initial index, or say why "
                         "retrieval is token-only (g-306-574)")
    ns = ap.parse_args([] if argv is None else argv)
    if ns.session_start:
        message = session_start(os.environ.get("EMBED_FRESHNESS_DRYRUN") == "1")["message"]
        if message:
            print(message)
        return 0

    dry_run = os.environ.get("EMBED_FRESHNESS_DRYRUN") == "1"
    index_dir = _index_dir()
    meta = index_dir / "meta.json"

    if not _blend_enabled():
        return 0
    if not meta.exists():
        return 0  # initial build is deliberate, never hook-spawned

    # Drift outranks the mtime test and is checked first: a quiet box can be
    # drifted with a perfectly fresh index, and the embedding lanes stay
    # frozen until an --update rebuilds on the configured model.
    drifted = _model_drifted(meta)
    src_m = _source_mtime()
    if not drifted and (src_m is None or src_m <= meta.stat().st_mtime):
        return 0  # fresh (or sources unreadable — fail quiet)

    marker = index_dir / ".last-update-attempt"
    now = time.time()
    try:
        if marker.exists():
            attempted = marker.stat().st_mtime
            # --update rewrites meta.json on every successful run, so a meta at
            # least as new as the marker means the last attempt LANDED. Anything
            # else — it failed, or is still running — keeps the long window.
            landed = meta.stat().st_mtime >= attempted
            window = SUCCESS_INTERVAL_SECONDS if landed else DEBOUNCE_SECONDS
            if now - attempted < window:
                return 0  # attempted recently — wait out the window
    except OSError:
        pass

    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")
    except OSError:
        return 0  # can't record the attempt → don't risk a spawn storm

    if dry_run:
        print(json.dumps({"op": "freshness-tick", "would_spawn": True,
                          "reason": "model_drift" if drifted else "stale_sources",
                          "index_dir": str(index_dir)}))
        return 0

    try:
        args = [sys.executable, str(SCRIPT_DIR / "embedding-index-build.py"),
                "--update"]
        if index_dir != INDEX_DIR:
            args += ["--out", str(index_dir)]
        _spawn_detached(args)
        print(json.dumps({"op": "freshness-tick", "spawned": True,
                          "index_dir": str(index_dir)}))
    except Exception as exc:  # fail-open — never abort the caller's phase
        print(json.dumps({"op": "freshness-tick", "error": str(exc)[:200]}),
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
