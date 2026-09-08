#!/usr/bin/env python3
"""inbound_directive_asp — resolve, or mint once, the vessel's directive aspiration.

(g-369-155) The inbound drain files a member's Assigned-tab directives as goals
under ONE aspiration titled "Assigned by the member". The provisioner
(Ayoai-Environment-Server ops/mind-sidecar/provision-env.sh, wire_assigned_lane)
creates the vessel's world queue and writes INBOUND_ENVIRONMENT_KEY and
INBOUND_SPOOL_ROOT into the workspace .env.local, but deliberately does NOT
mint that aspiration: aspiration creation is daemon-only, and no mind_api is
bound to the vessel at provision time (the operator box's single daemon serves
ANOTHER member's workspace, so minting there would file into the wrong queue).
It writes INBOUND_DIRECTIVE_ASP_ID only when the aspiration already exists. The
mint therefore lives HERE, in the vessel's own loop, where the daemon serves the
vessel's own WORLD_PATH.

Resolution order — first non-empty wins:
  1. $INBOUND_DIRECTIVE_ASP_ID               the daemon's EnvironmentFile carried it
  2. INBOUND_DIRECTIVE_ASP_ID in .env.local  written back by an earlier pass; the
                                             daemon has not restarted since
  3. the world queue, by EXACT title         a re-provision, or a failed write-back
  4. MINT through aspirations-add.sh         once per vessel, then write it back

Idempotent by title: step 3 always precedes step 4, and after a failed add the
queue is read once more before concluding — a timed-out add may have written
(rb-8227), and an add wrapper is never re-run (guard-751). Fail-open: every
failure yields an EMPTY id on stdout and the drain proceeds with directives left
UNCLAIMED — never lost — which the engine reports as unconfigured > 0.

The caller (inbound-drain-default.sh) invokes this AFTER its environment guards,
so a box with no INBOUND_ENVIRONMENT_KEY never reaches the mint and an operator
box can never mint into a fleet queue.

Test seams: INBOUND_ASP_READ_CMD / INBOUND_ASP_ADD_CMD (shell strings run via
the resolved bash) replace the two daemon wrappers; resolve() takes the same
two strings directly.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from _runtime_bash import BASH  # noqa: E402  (guard-580: never a bare "bash")

DIRECTIVE_ASP_ENV = "INBOUND_DIRECTIVE_ASP_ID"
DIRECTIVE_TITLE = "Assigned by the member"
READ_CMD_ENV = "INBOUND_ASP_READ_CMD"
ADD_CMD_ENV = "INBOUND_ASP_ADD_CMD"
_TIMEOUT_S = 120


# ── .env.local (systemd EnvironmentFile: KEY=VALUE lines) ─────────────────────

def read_env_local(path: Path, key: str = DIRECTIVE_ASP_ENV) -> str:
    """Value of KEY in the EnvironmentFile, or '' (an absent file included)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    value = ""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith(f"{key}="):
            # last one wins, as systemd reads it
            value = s[len(key) + 1:].strip().strip('"').strip("'")
    return value


def write_env_local(path: Path, asp_id: str, key: str = DIRECTIVE_ASP_ENV) -> None:
    """Set KEY=asp_id, replacing any existing KEY line and keeping every other line.

    Written to a temp file in the same directory and renamed over the original,
    so a failure part-way leaves the original untouched — the commit point
    provision-env.sh uses. Mode is preserved: the file is the daemon's
    EnvironmentFile and holds secrets (0600).
    """
    try:
        original = path.read_text(encoding="utf-8")
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        original, mode = "", 0o600
    kept = [ln for ln in original.splitlines() if not ln.strip().startswith(f"{key}=")]
    kept.append(f"{key}={asp_id}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".env.local.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(kept) + "\n")
        with contextlib.suppress(OSError):
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# ── the queue, through the daemon wrappers ────────────────────────────────────

def _run(cmd: str, stdin: str | None = None) -> tuple[int, str, str]:
    p = subprocess.run([BASH, "-c", cmd], input=stdin, capture_output=True,
                       text=True, timeout=_TIMEOUT_S)
    return p.returncode, p.stdout, p.stderr


def default_read_cmd() -> str:
    return f"{shlex.quote((SCRIPT_DIR / 'aspirations-read.sh').as_posix())} --active"


def default_add_cmd() -> str:
    why = "member Assigned lane (g-369-155): one directive aspiration per vessel, minted once"
    add = shlex.quote((SCRIPT_DIR / "aspirations-add.sh").as_posix())
    return f"{add} --override-supply {shlex.quote(why)}"


def find_by_title(read_cmd: str, title: str = DIRECTIVE_TITLE) -> str:
    """Id of the ACTIVE aspiration whose title equals `title` EXACTLY, else ''.

    Exact, through a JSON parser: a substring match would also hit the title
    inside a goal's prose (the provisioner's read-back says the same).
    """
    try:
        rc, out, _err = _run(read_cmd)
        data = json.loads(out) if rc == 0 and out.strip() else []
    except (subprocess.SubprocessError, ValueError, OSError):
        return ""
    if not isinstance(data, list):
        return ""
    for rec in data:
        if isinstance(rec, dict) and rec.get("title") == title and rec.get("id"):
            return str(rec["id"])
    return ""


def mint(add_cmd: str, title: str = DIRECTIVE_TITLE) -> str:
    """Create the directive aspiration ONCE. Returns its id, or '' on any failure.

    Fields per `aspirations-add.sh --schema`: title + priority required, id
    auto-assigned (never supplied — guard-4850). Stdout is captured and parsed
    from this single invocation (guard-751).
    """
    body = {
        "title": title,
        "priority": "HIGH",
        "status": "active",
        "scope": "project",
        "motivation": ("The member's Assigned tab: every bullet typed there is spooled "
                       "to this vessel and filed here as a goal by the inbound drain."),
        "description": ("Minted once per vessel by core/scripts/inbound_directive_asp.py "
                        "(g-369-155). Do not retire while the environment is watchable: "
                        "the drain files the member's directives under this id "
                        "(INBOUND_DIRECTIVE_ASP_ID in .env.local)."),
        "tags": ["assigned-lane", "member-directives", "inbound-drain"],
        "origin_signal": "user_directive",
        "goals": [],
    }
    try:
        rc, out, _err = _run(add_cmd, stdin=json.dumps(body))
        rec = json.loads(out) if rc == 0 and out.strip() else {}
    except (subprocess.SubprocessError, ValueError, OSError):
        return ""
    if not isinstance(rec, dict):
        return ""
    return str(rec.get("id") or "")


def resolve(env: dict, env_local: Path, *, read_cmd: str, add_cmd: str,
            title: str = DIRECTIVE_TITLE, log=None) -> tuple[str, str]:
    """Return (asp_id, source); source in env | env.local | queue | minted |
    queue-after-add | none. Writes the id back to `env_local` whenever it came
    from the queue or a mint, so the daemon's next start carries it."""
    def say(msg: str) -> None:
        if log:
            log(msg)

    v = (env.get(DIRECTIVE_ASP_ENV) or "").strip()
    if v:
        return v, "env"
    v = read_env_local(env_local)
    if v:
        return v, "env.local"
    v = find_by_title(read_cmd, title)
    source = "queue"
    if not v:
        v = mint(add_cmd, title)
        source = "minted"
        if not v:
            # never re-run the add: a timed-out add may have written (rb-8227)
            v = find_by_title(read_cmd, title)
            source = "queue-after-add"
    if not v:
        return "", "none"
    try:
        write_env_local(env_local, v)
    except OSError as exc:
        say(f"write-back to {env_local} failed: {exc}")
    return v, source


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--env-local", required=True,
                    help="the workspace .env.local to read and write back")
    ap.add_argument("--title", default=DIRECTIVE_TITLE)
    args = ap.parse_args(argv)
    read_cmd = os.environ.get(READ_CMD_ENV) or default_read_cmd()
    add_cmd = os.environ.get(ADD_CMD_ENV) or default_add_cmd()
    asp_id, source = resolve(dict(os.environ), Path(args.env_local),
                             read_cmd=read_cmd, add_cmd=add_cmd, title=args.title,
                             log=lambda m: sys.stderr.write(f"[inbound-drain] {m}\n"))
    sys.stderr.write(f"[inbound-drain] directive aspiration: {source} {asp_id or '(none)'}\n")
    sys.stdout.write(asp_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
