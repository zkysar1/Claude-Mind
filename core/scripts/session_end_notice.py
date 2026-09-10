"""Resolve the owner's shutdown-report knob: `session_end_notice.mode`.

WHY THIS EXISTS (g-115-9629, 2026-09-10).

The owner reported getting two summary emails per wind-down and answered the
design question himself, verbatim: *"as the agent is shutting down, it should
send [a] completion report naturally... that way the agent can choose not to do
it, or the user can alter how it does it"*.

The "agent can choose" half became judgement in `aspirations-consolidate`
Step 9.7. The "user can alter how it does it" half became
`session_end_notice.mode` in `core/config/aspirations.yaml` — and that half was
**declared but never wired**: measured 2026-09-10, the key appeared in exactly
three places (its own definition, one prose mention in the SKILL.md step, and
the rationale doc) and in ZERO code paths. Setting `mode: never` would have
changed nothing, which is strictly worse than having no knob at all, because the
owner would believe he had turned the report off.

That is rb-189's class (*"declared-but-unenforced capability: skill docs are not
workflow enforcement"*), and guard-399 prescribes the remedy: before an
`LLM must do X at step N` instruction is worth anything, write the bash path
that makes X mechanical. The adjacent `productivity_gate` block in the same
config file already states the principle a prior author used to REFUSE an
unreadable knob — *"a config key here would lie about what the script can do"* —
so this module exists to stop that key from lying.

`resolve_mode()` is deliberately pure: it takes an already-parsed config mapping
and touches no filesystem, environment, or clock, so every branch below is
reachable from a test without a config file (guard-1165).
"""

import sys
from pathlib import Path

VALID_MODES = ("auto", "always", "never")

# The documented default, and the fail-safe landing spot for every degraded
# read. Chosen deliberately over `never`: `auto` still applies Step 9.7's
# judgement (it declines when the owner has already been told), so a typo'd
# mode costs at most one redundant-but-considered email, which the owner will
# SEE and can correct. Falling back to `never` would silently suppress
# shutdown reports — a loss of information he would have no signal to notice.
DEFAULT_MODE = "auto"

CONFIG_BLOCK = "session_end_notice"


def resolve_mode(cfg):
    """Return ``(mode, degraded_reason)`` for a parsed aspirations.yaml mapping.

    ``mode`` is ALWAYS one of :data:`VALID_MODES` — callers never have to handle
    an error value, which is what lets the SKILL.md step consume stdout
    directly. ``degraded_reason`` is ``None`` when the config said something
    well-formed (including saying nothing at all, which the config documents as
    meaning ``auto``) and a short human-readable string otherwise.

    An ABSENT key is not degraded — `core/config/aspirations.yaml` states
    "absence of this key = auto" — so it returns ``(auto, None)``. A key that is
    PRESENT but unrecognisable IS degraded, because that is a person trying to
    say something the system failed to understand.
    """
    if not isinstance(cfg, dict):
        return DEFAULT_MODE, f"config root is {type(cfg).__name__}, not a mapping"

    if CONFIG_BLOCK not in cfg:
        return DEFAULT_MODE, None  # documented: absent block = auto

    block = cfg[CONFIG_BLOCK]
    if block is None:
        return DEFAULT_MODE, None  # `session_end_notice:` with an empty body
    if not isinstance(block, dict):
        return DEFAULT_MODE, f"{CONFIG_BLOCK} is {type(block).__name__}, not a mapping"

    if "mode" not in block:
        return DEFAULT_MODE, None  # documented: absent key = auto

    raw = block["mode"]
    if not isinstance(raw, str):
        return DEFAULT_MODE, f"{CONFIG_BLOCK}.mode is {type(raw).__name__}, not a string"

    # Case- and whitespace-forgiving on purpose. This is an OWNER-FACING knob
    # typed by hand; rejecting "Never" on case while the failure direction is
    # "keep emailing him anyway" would be pedantry with a cost attached.
    mode = raw.strip().lower()
    if mode not in VALID_MODES:
        return DEFAULT_MODE, (
            f"{CONFIG_BLOCK}.mode is {raw!r}, not one of {'/'.join(VALID_MODES)}"
        )

    return mode, None


def load_config():
    """Read and parse aspirations.yaml. Returns ``(cfg, error)``."""
    # Imported here, not at module scope, so `resolve_mode` stays importable
    # with no path/environment dependency at all.
    scripts_dir = Path(__file__).resolve().parent
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    try:
        import yaml

        import _paths

        path = Path(_paths.CONFIG_DIR) / "aspirations.yaml"
        with open(path, encoding="utf-8") as handle:
            return yaml.safe_load(handle), None
    except Exception as exc:  # noqa: BLE001 - any failure means "cannot read"
        return None, f"{type(exc).__name__}: {exc}"


def main(argv=None):
    """Print the resolved mode on stdout; explain any degradation on stderr.

    Exit 0 when the config was understood (explicitly or by documented
    absence), exit 3 when it was not and the default was substituted. stdout
    carries a usable mode in BOTH cases, so a caller that ignores the exit
    status still behaves safely — but one that checks it can tell "the owner
    chose auto" from "we could not tell what the owner chose".
    """
    cfg, error = load_config()
    if error is not None:
        print(DEFAULT_MODE)
        print(
            f"[session-end-notice] could not read aspirations.yaml ({error}) — "
            f"falling back to {DEFAULT_MODE}",
            file=sys.stderr,
        )
        return 3

    mode, degraded = resolve_mode(cfg)
    print(mode)
    if degraded is not None:
        print(
            f"[session-end-notice] {degraded} — falling back to {DEFAULT_MODE}",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
