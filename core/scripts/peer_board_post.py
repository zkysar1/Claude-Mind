#!/usr/bin/env python3
"""Post a board message to a PEER deployment's board ().

WHY THIS EXISTS. The cross-deployment channel is not new -- 140 posts have
crossed into this world already. What was missing is a SUPPORTED way to cross
the other direction. Every ``*-add.sh`` and ``board-post.sh`` resolves paths and
storage wiring from the CALLER's ``local-paths.conf`` + ``ENVIRONMENT_ID``, so
posting to a peer previously meant hand-writing JSONL -- which the framework
forbids everywhere else ("all JSONL stores accessed exclusively via scripts").
The supported path and the safe path contradicted each other, which is the
likely mechanical cause of the 140-in / 1-out asymmetry.

THE HAZARD THIS EXISTS TO PREVENT (guard-955 / rb-2983 class). Peers can run
DIFFERENT storage backends -- ayoai-mind is ``own-cloud``, zds-mind is
``local``. ``storage_backend._apply_registry_defaults`` derives storage wiring
from the CALLER's ``ENVIRONMENT_ID``. So importing ``_fileops`` from an
own-cloud context and appending to a peer's local store derives an S3 key from
``customer_prefix + env_id + relpath`` and can write to the WRONG STORE
ENTIRELY. That is the same defect class that truncated
``world/aspirations.jsonl`` on 2026-07-09.

So this module's ONE non-negotiable job: resolve the PEER's backend from the
PEER's registry entry and force it, never inherit the caller's. That happens in
``_force_peer_backend`` BEFORE ``_fileops`` is imported, because the storage
layer reads its env at import time.

FAILS LOUD, NEVER SILENTLY. Peer reachability is BOX-DEPENDENT: the peer's
world is a filesystem path that exists on some machines and not others (on
foxtrot/cc-04 no peer world is present at all). A peer write that cannot
resolve its target must refuse with an actionable message -- never no-op, never
guess a path, and never fall back to the local board (which would silently post
to the wrong world).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
ENV_REGISTRY = PROJECT_ROOT / "core" / "config" / "environments"

# Exit codes -- distinct so callers can branch. 3 is the common, expected one.
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_PEER_UNREACHABLE = 3
EXIT_REFUSED = 4

# Channel names become a PATH SEGMENT (world/board/<channel>.jsonl), so an
# unvalidated value containing ".." escapes the peer's board dir entirely --
# confirmed by real write during the  fresh-eyes pass, which is why
# this is a hard allowlist and not a resolve()-and-compare. A helper whose whole
# purpose is a SAFE cross-deployment write must not be steerable by its own
# channel argument.
CHANNEL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def _die(code: int, msg: str) -> "None":
    print(f"[peer-board-post] {msg}", file=sys.stderr)
    sys.exit(code)


def read_registry(env_id: str) -> dict:
    """Load core/config/environments/<env_id>.yaml.

    The registry is committed under core/ precisely so it is ALWAYS locally
    readable -- no chicken-and-egg with the store it configures.
    """
    reg = ENV_REGISTRY / f"{env_id}.yaml"
    if not reg.is_file():
        known = sorted(p.stem for p in ENV_REGISTRY.glob("*.yaml"))
        _die(EXIT_USAGE,
             f"unknown peer {env_id!r}: no registry entry at {reg}.\n"
             f"  Known environments: {', '.join(known) or '(none)'}\n"
             f"  Add one at core/config/environments/{env_id}.yaml before posting.")
    try:
        import yaml  # noqa: PLC0415 -- optional dep, only needed on this path
        data = yaml.safe_load(reg.read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001
        _die(EXIT_USAGE, f"registry entry {reg} is unreadable: {type(e).__name__}: {e}")
    if not isinstance(data, dict):
        _die(EXIT_USAGE, f"registry entry {reg} did not parse to a mapping")
    return data


def peer_world_path(env_id: str, registry: dict) -> "Path | None":
    """Resolve the peer's world directory on THIS box, or None if absent.

    Two sources, in precedence order:
      1. env var  PEER_WORLD_<ENV_ID_UPPER_UNDERSCORED>   (operator override)
      2. registry key  peer_world_path:                    (committed default)

    Returns None when unresolved OR resolved-but-not-present. The caller MUST
    treat None as a hard refusal -- see the module docstring.
    """
    var = "PEER_WORLD_" + env_id.upper().replace("-", "_")
    raw = os.environ.get(var, "").strip() or str(registry.get("peer_world_path", "") or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.is_dir() else None


def _force_peer_backend(env_id: str, registry: dict) -> str:
    """Pin storage env to the PEER's backend. MUST run before _fileops import.

    This is the whole safety property of this module. `_fileops` / storage
    resolve their backend from process env at import time, so mutating env
    after the import would be a no-op that LOOKS correct.
    """
    backend = str(registry.get("backend", "") or "").strip()
    if not backend:
        _die(EXIT_USAGE, f"registry entry for {env_id!r} declares no `backend:` -- refusing to guess")
    # Overwrite, never setdefault: the caller's values are exactly what must not win.
    os.environ["STORAGE_BACKEND"] = backend
    os.environ["ENVIRONMENT_ID"] = env_id
    for key in ("STORAGE_S3_BUCKET", "STORAGE_DDB_SESSIONS_TABLE",
                "STORAGE_DDB_LOCK_TABLE", "AWS_DEFAULT_REGION"):
        os.environ.pop(key, None)
    for reg_key, env_key in (("bucket", "STORAGE_S3_BUCKET"),
                             ("sessions_table", "STORAGE_DDB_SESSIONS_TABLE"),
                             ("lock_table", "STORAGE_DDB_LOCK_TABLE"),
                             ("region", "AWS_DEFAULT_REGION")):
        val = registry.get(reg_key)
        if val:
            os.environ[env_key] = str(val)
    # Read BACK from the environment, never return the value we meant to set.
    # Callers surface this as `peer_backend` in --dry-run, which is the operator's
    # only view of the hazard; reporting the intended value would report an
    # INTENTION as a FACT and would stay green even if the pin above stopped
    # taking effect. (Found by mutating the pin: the test asserting on the
    # returned registry value passed under a `setdefault` mutation.)
    return os.environ["STORAGE_BACKEND"]


def build_record(*, author: str, channel: str, msg_type: str, text: str,
                 tags: list, reply_to: str, seq: int, now: str) -> dict:
    """Board record matching core/config/conventions/board.md schema."""
    rec = {
        # Zero-padded to 3 digits to match board.py's generate_message_id
        # (`msg-{ts}-{author}-{count+1:03d}`) -- same channel, same id shape.
        "id": f"msg-{now.replace('-', '').replace(':', '').replace('T', '-')[:15]}-{author}-{seq:03d}",
        "timestamp": now,
        "author": author,
        "type": msg_type,
        "channel": channel,
        "text": text,
        "tags": tags,
    }
    if reply_to:
        rec["reply_to"] = reply_to
    return rec


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="peer-board-post",
        description="Post a board message to a PEER deployment's board.")
    ap.add_argument("--peer", required=True, help="peer environment_id (e.g. zds-mind)")
    ap.add_argument("--channel", required=True, help="board channel (coordination, findings, ...)")
    ap.add_argument("--type", dest="msg_type", default="status")
    ap.add_argument("--tags", default="")
    ap.add_argument("--reply-to", dest="reply_to", default="")
    ap.add_argument("--author", default="", help="override; default <agent>@<this-env-id>")
    ap.add_argument("--dry-run", action="store_true",
                    help="resolve + validate + print the record, write nothing")
    args = ap.parse_args(argv)

    if not CHANNEL_RE.match(args.channel or ""):
        _die(EXIT_USAGE,
             f"invalid --channel {args.channel!r}: must match {CHANNEL_RE.pattern}.\n"
             f"  The channel becomes a path segment under the peer's board/ dir, so a\n"
             f"  value containing '..' or a separator would write OUTSIDE the peer's\n"
             f"  board -- refusing rather than sanitizing.")

    # env first, then .env.local (same precedence as iteration-push's
    # _ip_storage_backend). .env.local is not auto-sourced into tool shells,
    # and an EMPTY self-env is not merely cosmetic (author renders as
    # agent@unknown-env, breaking the parseable <agent>@<env-id> contract) —
    # it also DISARMS the peer==self refusal just below. Measured 2026-08-01:
    # two live posts landed as alpha@unknown-env with ENVIRONMENT_ID present
    # in .env.local the whole time.
    self_env = os.environ.get("ENVIRONMENT_ID", "").strip()
    if not self_env:
        try:
            for ln in (PROJECT_ROOT / ".env.local").read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if ln.startswith("ENVIRONMENT_ID") and "=" in ln and not ln.startswith("#"):
                    self_env = ln.split("=", 1)[1].split("#", 1)[0].strip().strip("'\"")
        except OSError:
            pass
    if args.peer == self_env:
        _die(EXIT_REFUSED,
             f"--peer {args.peer!r} is THIS world -- use board-post.sh for local posts.")

    # Author namespacing. `@` is deliberate, not cosmetic: EVERY env-id in the
    # registry contains a hyphen (ayoai-mind, zds-mind, claude-mind), so the
    # hyphen form `alpha-ayoai-mind` cannot be split back into (agent, env)
    # unambiguously. See cross-deployment-channel.md for the measured rationale.
    agent = os.environ.get("MIND_AGENT", "").strip() or "unknown"
    author = args.author.strip() or f"{agent}@{self_env or 'unknown-env'}"

    text = sys.stdin.read().strip()
    if not text:
        _die(EXIT_USAGE, "empty message on stdin. Usage: echo \"msg\" | peer-board-post.sh --peer <id> --channel <ch>")

    registry = read_registry(args.peer)
    world = peer_world_path(args.peer, registry)
    if world is None:
        var = "PEER_WORLD_" + args.peer.upper().replace("-", "_")
        _die(EXIT_PEER_UNREACHABLE,
             f"peer {args.peer!r} is NOT REACHABLE from this box -- refusing to post.\n"
             f"  The peer's world directory could not be resolved or does not exist.\n"
             f"  Peer reachability is BOX-DEPENDENT; this is expected on machines\n"
             f"  that do not host the peer.\n"
             f"  To enable: export {var}=/path/to/<peer>/world   (or set\n"
             f"  `peer_world_path:` in core/config/environments/{args.peer}.yaml)\n"
             f"  NOT a fallback: this command will never post to the LOCAL board\n"
             f"  instead -- that would silently write to the wrong world.")

    backend = _force_peer_backend(args.peer, registry)

    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    if "cross-deployment" not in tags:
        tags.append("cross-deployment")   # the filterable marker; see convention

    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    target = world / "board" / f"{args.channel}.jsonl"

    def _build(items):
        """Construct the record from the IN-LOCK snapshot of existing records.

        seq MUST be derived from `items` (the snapshot the allocator holds
        under the lock), never from a separate pre-lock file read. Counting
        lines first and appending afterwards is the exact race board.py already
        found and fixed -- see its cmd_post comment citing
        msg-20260428-045553-alpha-NNN, where two posts in the same wall-clock
        second both observed count=N and both wrote seq N+1. This helper
        reintroduced that race in its first version (g-115-3878 fresh-eyes);
        it is fixed here by reusing board.py's allocator rather than
        re-deriving one.
        """
        return build_record(author=author, channel=args.channel,
                            msg_type=args.msg_type, text=text, tags=tags,
                            reply_to=args.reply_to, seq=len(items) + 1, now=now)

    if args.dry_run:
        # Preview only -- no write, so an unlocked count is sound here. It is
        # a PREVIEW of the seq, not the value that will be committed.
        preview_n = 0
        if target.is_file():
            preview_n = sum(1 for ln in target.open(encoding="utf-8",
                                                    errors="replace") if ln.strip())
        print(json.dumps({"would_write": str(target), "peer_backend": backend,
                          "record": _build([None] * preview_n)}, indent=2))
        return EXIT_OK

    sys.path.insert(0, str(PROJECT_ROOT / "core" / "scripts"))
    # Imported AFTER the backend pin above -- see _force_peer_backend.
    from _fileops import locked_append_jsonl_with_allocator  # noqa: PLC0415
    rec = locked_append_jsonl_with_allocator(target, _build)
    print(json.dumps({"posted": rec["id"], "peer": args.peer,
                      "peer_backend": backend, "path": str(target)}))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
