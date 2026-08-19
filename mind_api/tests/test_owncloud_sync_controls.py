#!/usr/bin/env python3
"""test_owncloud_sync_controls.py — a documented control must actually be
reachable (g-115-5968).

THE DEFECT CLASS. `mind_api/src/__main__.py` documented TWO ways to pause the
own-cloud mirror sweep and BOTH were dead:

  - `OWNCLOUD_SYNC_DISABLE` had ZERO production reads anywhere in core/ or
    mind_api/, AND was absent from `_N3_ALLOWED_EXACT`, so setting it in
    .env.local never reached the process at all.
  - the docstring said "stop the daemon (mind-api-stop.sh)" and that script has
    never existed — only mind-api-start.sh and mind-api-code-changed.sh do.

That combination FAILS OPEN, which is what makes it worth a test rather than a
one-time fix: an operator sets the flag, gets no error, and may then run a
destructive S3 delete believing the writer is parked. It already cost a real
resurrection — one sweep re-wrote 1,328 of 1,338 orphan objects in a single
hour after an archived prefix was purged.

A THIRD instance was found by these tests while writing them, which is the
argument for the read⇔loadable pin below being general rather than a
regression-of-one: `OWNCLOUD_PULL_EVERY_N` IS read (0 disables the pull half)
and IS documented as an override, but was absent from `_N3_ALLOWED_EXACT` — so
the documented channel (.env.local) silently dropped it and it only ever worked
when exported into the launch env, which is not where any other daemon setting
lives.

WHY THESE TWO PREDICATES AND NOT A DOCSTRING SCAN. The obvious test — "every
env var named in the docstring must be live" — cannot work here, because the
docstring deliberately NAMES the dead controls as warnings so a future reader
does not resurrect them. A scan would fail on the documentation of the very
defect it is checking. Both predicates below are therefore prose-free: they read
what the CODE does, not what the prose says.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_PY = REPO_ROOT / "mind_api" / "src" / "__main__.py"
SCRIPTS_DIR = REPO_ROOT / "core" / "scripts"

SOURCE = MAIN_PY.read_text(encoding="utf-8")

# `_load_env_local` accepts a key iff it is in _N3_ALLOWED_EXACT, starts with
# MIND_AWS_, or is exactly AWS_DEFAULT_REGION. Mirrored from the function rather
# than imported: importing __main__ starts the daemon's module-level machinery,
# and this file must stay a pure static check.
_LOADABLE_PREFIXES = ("MIND_AWS_",)
_LOADABLE_EXTRA = {"AWS_DEFAULT_REGION"}


def _allowed_exact() -> set:
    """Parse the _N3_ALLOWED_EXACT literal out of the source."""
    m = re.search(r"_N3_ALLOWED_EXACT\s*=\s*frozenset\(\{(.*?)\}\)", SOURCE, re.S)
    assert m, "could not locate the _N3_ALLOWED_EXACT literal"
    return set(re.findall(r'"([A-Z0-9_]+)"', m.group(1)))


def _is_loadable(key: str, allowed: set) -> bool:
    return (key in allowed
            or key in _LOADABLE_EXTRA
            or any(key.startswith(p) for p in _LOADABLE_PREFIXES))


def test_every_owncloud_env_key_the_daemon_reads_is_loadable():
    """READ ⇔ LOADABLE parity for the OWNCLOUD_* control family.

    A key the daemon reads but cannot load is a control that silently does
    nothing through the documented channel. This is the predicate that caught
    OWNCLOUD_PULL_EVERY_N; it is prose-free, so it keeps working however the
    docstring is reworded.
    """
    allowed = _allowed_exact()
    read_keys = set(re.findall(r'os\.environ\.get\(\s*"(OWNCLOUD_[A-Z0-9_]+)"', SOURCE))
    assert read_keys, (
        "found ZERO OWNCLOUD_* os.environ.get reads — the regex has drifted from "
        "the source, so this test would pass vacuously (rb-245). Fix the regex."
    )
    unreachable = sorted(k for k in read_keys if not _is_loadable(k, allowed))
    assert not unreachable, (
        f"OWNCLOUD_* keys READ by the daemon but not loadable from .env.local: "
        f"{unreachable}. Either add each to _N3_ALLOWED_EXACT or stop reading it. "
        f"A read-but-unsettable knob is a control that fails silently open."
    )


def _world_scripts_dir():
    """Resolve $WORLD_PATH/scripts, or None when this box has no world configured.

    The module legitimately names DOMAIN scripts (email-send.sh and friends) that
    live at an external, per-box world path — not under core/scripts/. Checking
    only core/ reported three real scripts as missing on the first run of this
    test. Resolution is best-effort on purpose: mind_api/tests must not depend on
    any particular world's contents.
    """
    try:
        sys.path.insert(0, str(REPO_ROOT / "core" / "scripts"))
        from _paths import WORLD_DIR  # type: ignore
        d = Path(str(WORLD_DIR)) / "scripts"
        return d if d.is_dir() else None
    except Exception:
        return None


def test_every_shell_script_named_in_the_module_exists():
    """Any `<name>.sh` token in the source must resolve to a script that exists.

    This is the predicate that would have caught `mind-api-stop.sh`. The
    docstring now mentions that name WITHOUT a .sh suffix, because it never was a
    script — writing it as a bare name keeps the warning readable and keeps this
    check honest rather than allowlisted.

    Tokens are resolved against core/scripts/ AND the world scripts dir. When the
    world is not resolvable on this box, unresolved tokens are REPORTED, not
    failed — an unverifiable name is not evidence of a missing one, and failing
    there would make this test depend on which world happens to be mounted.
    """
    named = set(re.findall(r"\b([a-z0-9][a-z0-9._-]*\.sh)\b", SOURCE))
    assert named, (
        "found ZERO *.sh tokens in the source — the regex has drifted, so this "
        "test would pass vacuously (rb-245). Fix the regex."
    )
    world = _world_scripts_dir()

    def _resolves(n: str) -> bool:
        return (SCRIPTS_DIR / n).exists() or (world is not None and (world / n).exists())

    missing = sorted(n for n in named if not _resolves(n))
    if missing and world is None:
        # Cannot distinguish "domain script on an unmounted world" from "dead
        # name". Say so rather than passing silently or failing spuriously.
        print(f"[unverified — no world scripts dir on this box] {missing}")
        return
    assert not missing, (
        f"script name(s) referenced in {MAIN_PY.name} that resolve to no script "
        f"under core/scripts/ or the world scripts dir: {missing}. Naming a "
        f"nonexistent script as a remedy is how the sync-pause control failed "
        f"open (g-115-5968)."
    )


def test_the_dead_disable_flag_has_not_been_resurrected_half_wired():
    """OWNCLOUD_SYNC_DISABLE must be BOTH read and loadable, or NEITHER.

    It is currently neither, which is a coherent state: the working recipe is
    the interval. The failure this guards is someone re-adding one half — a read
    with no allowlist entry, or an allowlist entry nothing reads — which
    reproduces the original fail-open exactly.
    """
    key = "OWNCLOUD_SYNC_DISABLE"
    is_read = bool(re.search(r'os\.environ\.get\(\s*"%s"' % key, SOURCE))
    is_loadable = _is_loadable(key, _allowed_exact())
    assert is_read == is_loadable, (
        f"{key} is half-wired: read={is_read}, loadable={is_loadable}. "
        f"A control that is read but unsettable (or settable but unread) fails "
        f"silently OPEN — the operator gets no error and believes the sweep is "
        f"parked. Wire both halves or neither."
    )


def test_the_sync_thread_still_gates_only_on_storage_backend():
    """Pins the premise the pause recipe rests on.

    The interval recipe works BECAUSE the thread reads OWNCLOUD_SYNC_INTERVAL
    once at start and otherwise gates only on STORAGE_BACKEND. If an early-return
    on some other condition is added, the documented recipe may stop being the
    only lever and this docstring would need revisiting.
    """
    m = re.search(r"def _start_owncloud_sync_thread\(.*?\n(.*?)\n    scripts_dir",
                  SOURCE, re.S)
    assert m, "could not locate _start_owncloud_sync_thread's preamble"
    body = m.group(1)
    assert 'os.environ.get("STORAGE_BACKEND"' in body, (
        "the sync thread no longer gates on STORAGE_BACKEND — re-check the pause "
        "recipe documented in its docstring."
    )
    assert 'os.environ.get("OWNCLOUD_SYNC_INTERVAL"' in body, (
        "the sync thread no longer reads OWNCLOUD_SYNC_INTERVAL — the documented "
        "pause recipe (set a large interval + --restart) no longer works."
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(__import__("pytest").main([__file__, "-q"]))
