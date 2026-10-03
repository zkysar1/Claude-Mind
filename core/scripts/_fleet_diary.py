"""Fleet-wide execution-diary enumeration + authoritative read ().

Replaces `agents_root().glob("*/session/execution-diary.jsonl")` at the call
sites that scan EVERY agent's diary. That glob has TWO independent defects on
an own-cloud box (g-115-4154, cc-02, 2026-07-31), and fixing either one alone
leaves the other live.

ENUMERATION. `execution-diary.jsonl` is `sync_tier: continuity`
(`core/config/session-manifest.yaml`) and `OwnCloudBackend._machine_local()`
returns False for it, so the authoritative copy is in S3 and the local tree is
a read-through cache. That cache is populated PER-AGENT: `owncloud-pull.sh` is
--agent-scoped and `/start` pulls only the bound agent, so a peer's diary is
simply ABSENT locally until something reads it. A filesystem glob enumerates by
what is on disk, so a cold box silently scans a subset of the fleet and reports
the subset as if it were everyone. Measured on cc-02 (Linux 6.8.0-136-generic)
2026-07-31 at filing time: 1 of 5 agents present locally while all 5 were live
in S3, every one written within the preceding 15 minutes (g-115-4154 filing).

STALENESS. Enumeration succeeding does NOT mean the content is right, and this
half is the one a warm cache hides. Measured on cc-02 2026-07-31T06:33
(g-115-4154 filing) — same box, cache since warmed — the glob found 5 of 5
and 4 of those 5 DIVERGED from S3 (local/S3 bytes): alpha 36801/39669,
bravo 59536/53382, echo 60782/55593, foxtrot 37796/36313. Three were LARGER
locally, so this is not lagging appends.
Both call sites then read those local bytes directly, so a full-enumeration run
still analyses stale content and looks perfectly healthy doing it.

WHY A GLOB CANNOT BE FIXED BY SWAPPING THE READ (g-115-4154). A filesystem
glob has no backend equivalent — it decides who EXISTS from the mirror. So
enumeration must come from a roster, and only then can each agent's diary
path be read through the backend. This helper does both halves.

ROSTER IS A UNION, DELIBERATELY. Team-state shard basenames (the live fleet
roster, the same primary `owncloud-pull.sh --all-agents` uses) UNION agent-dir
names. Under-enumeration is the entire defect being fixed, so the roster errs
wide: an agent that appears in only one source is still scanned, and a name with
no diary (a retired tombstone such as `meta-tiebreaker`, or a dir with no
session) costs one cheap fail-open skip. Narrowing this to a single source would
re-create the guard-1802 shape — a predicate narrower than the population it is
meant to cover, reporting clean forever.

ABSOLUTE PATHS ARE LOAD-BEARING, NOT COSMETIC. `read_authoritative_bytes`
derives its S3 key via `_s3_key`, which raises ValueError for a path not under a
configured root — and the method CATCHES that ValueError and silently returns
`self._local(path).read_bytes()` (owncloud_backend.py:890-892). That fallback is
intentional for genuinely out-of-root git-shipped paths, but a RELATIVE path is
indistinguishable from one: the caller gets local cache bytes from the single
method whose docstring promises it "NEVER touches the local mirror", with no
warning and the same return type. Measured while diagnosing this goal: a probe
built on `Path("agents")/name/...` reported local==authoritative for all 5
agents when 4 of 5 were in fact diverged — a false all-clear from the API built
to prevent exactly that. Hence `.resolve()` below, and hence the assertion.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, Optional

from _paths import WORLD_DIR, agents_root

DIARY_RELPATH = Path("session") / "execution-diary.jsonl"


def fleet_agent_names(base: Optional[Path] = None) -> list[str]:
    """Sorted roster of agent names whose diaries should be scanned.

    With an explicit `base` (the alternate-root seam the call sites expose —
    `--agents-root`, the daemon's `ctx.paths.agents_root`, hermetic tests) the
    roster is that root's directories alone: the live team-state shards say
    nothing about another root.
    """
    if base is not None:
        try:
            return sorted(d.name for d in Path(base).iterdir() if d.is_dir())
        except OSError:
            return []

    names: set[str] = set()
    try:
        shards = WORLD_DIR / "team-state" / "agents"
        names.update(p.stem for p in shards.glob("*.yaml"))
    except OSError:
        pass
    try:
        names.update(d.name for d in agents_root().iterdir() if d.is_dir())
    except OSError:
        pass
    return sorted(names)


def _get_backend():
    """The storage backend, or None when it cannot be constructed."""
    try:
        from storage_backend import get_backend

        return get_backend()
    except Exception:  # noqa: BLE001 — degrade to local reads, never abort
        return None


def read_agent_diary(
    agent: str,
    base: Optional[Path] = None,
    backend: object = "unset",
) -> tuple[Optional[str], str]:
    """Read ONE agent's diary, returning `(text, provenance)`.

    PROVENANCE IS THE POINT, and it is why this returns a tuple rather than the
    bare text `read_fleet_diaries` needed. A caller that merely ANALYSES diary
    content is fine either way — stale bytes make a slightly wrong report. A
    caller about to take a DESTRUCTIVE action on the strength of an ABSENCE is
    not: "the store of record says no work is happening" and "I could not reach
    the store of record, and the local cache is cold" are the same empty string,
    and they license opposite decisions (guard-980, rb-6650 — an unreadable
    store cannot authorise a destructive act). Without provenance the caller
    cannot tell them apart, so it silently treats the second as the first. The
    liveness helper carries the identical field for the identical reason
    (`authoritative_last_active_provenance`, guard-1753).

    Provenance values:
        authoritative  read through the backend from the store of record
        absent         the store of record positively reports no such object
        local-mirror   backend absent/erroring; bytes came from the local cache
        error          neither path produced bytes

    Fail-open: never raises. `absent` deliberately does NOT fall back to the
    local mirror — the store of record answering "no" is an answer, and reading
    around it would re-introduce the cache as an authority.
    """
    root = Path(base) if base is not None else agents_root()
    path = (root / agent / DIARY_RELPATH).resolve()
    if backend == "unset":
        backend = _get_backend()
    if backend is not None:
        # ABSOLUTE (resolved) path — see the module docstring: a relative
        # path silently degrades this to a local-mirror read.
        assert path.is_absolute(), f"non-absolute diary path: {path}"
        try:
            return (
                backend.read_authoritative_bytes(path).decode(
                    "utf-8", errors="replace"
                ),
                "authoritative",
            )
        except FileNotFoundError:
            return None, "absent"
        except Exception:  # noqa: BLE001 — S3/permission hiccup: fall back
            pass
    try:
        return path.read_text(encoding="utf-8", errors="replace"), "local-mirror"
    except OSError:
        return None, "error"


def _body_carrier_lines(agent_name: str, backend) -> list:
    """This agent's worker-Body diary rows (), from the carrier under
    `world/body-diaries/<agent>/`.

    Worker Bodies cannot push their agent-tree diary writes (the claim fence —
    see `body_diary_carrier`'s docstring), so each Body mirrors its rows to a
    Body-owned world carrier; this is the read side that unions those rows back
    into the agent's stream. FAIL-OPEN: any carrier problem returns `[]` — the
    carrier is additive, and a carrier hiccup must not cost the agent-wide read
    that this helper existed to make authoritative.

    The world root is this module's `WORLD_DIR` (the same name the roster half
    reads, so one patch/redirect covers both halves).
    """
    try:
        import body_diary_carrier as _bdc

        return _bdc.read_carrier_lines(
            agent_name, world_dir=WORLD_DIR, backend=backend)
    except Exception:  # noqa: BLE001 — additive lane, never fatal
        return []


def read_fleet_diaries(
    base: Optional[Path] = None,
) -> Iterator[tuple[str, str]]:
    """Yield `(agent_name, diary_text)` for every fleet agent with a diary.

    Fail-open PER AGENT: an agent with no diary, an unreadable one, or a
    backend error is skipped rather than aborting the sweep — one absent peer
    must never cost the whole scan. An agent yielding no text is skipped
    entirely, so callers see the same shape the old glob produced.

    ONE READ PATH, deliberately — `base` selects the ROOT and never the read
    mechanism. Under own-cloud against the real root this reads S3 (the point
    of the helper). Under a test root with `STORAGE_BACKEND=local` pinned as
    guard-955 requires, `LocalBackend.read_authoritative_bytes` is a plain read
    of the absolute tmp path, so tests stay hermetic without a second branch. A
    `local_only` flag would only add a way for the production path to be
    silently disabled.

    PROVENANCE IS DISCARDED HERE ON PURPOSE: this iterator's callers analyse
    content and have no destructive branch. `read_agent_diary` is the entry
    point for anyone who does — and its `(text, provenance)` contract is
    untouched by the carrier union below (peer_liveness and the sweep's
    keep-signal branch on it).

    WORKER-BODY ROWS ARE UNIONED IN (g-306-555), ON THE PRODUCTION PATH ONLY.
    The authoritative agent-wide diary holds only the reducer's rows: a worker
    Body's appends are stranded box-local by the agent-tree claim fence and
    mirror to `world/body-diaries/<agent>/<sid>.jsonl` instead. Without the
    union, every content consumer of this iterator (the scorer-override audit,
    skill-discovery) is blind to Body decisions — the measured incident is in
    `body_diary_carrier`'s docstring. Carrier rows are VERBATIM diary rows
    (identical serialization to the agent-wide file), so a row present in BOTH
    is skipped from the carrier half and can never double-count; the
    agent-wide half is always yielded exactly as read (no dedup there —
    changing it would alter consumers' retry-collapse accounting).

    THE UNION IS SKIPPED FOR A NON-DEFAULT `base` (g-306-575). The carrier root
    is `world/body-diaries/<agent>` and `world/` is NOT derivable from the
    agents root this `base` selects: in production the two are siblings under
    different parents (the agents tree vs the external `world/` path from
    `local-paths.conf`), and the `base` seam exists only for hermetic test
    roots. Until this fix the carrier half read this module's `WORLD_DIR`
    regardless of `base`, so a tmp-root read unioned the LIVE fleet's Body rows
    into every hermetic test that names a real agent ('alpha') — the 9 reds of
    g-306-575 (e.g. a test seeding 3 rows counting 49). A non-default `base`
    therefore yields the agent-wide text verbatim and skips the union. A test
    that needs the union exercises the production shape instead: `base=None`
    with `agents_root`/`WORLD_DIR` patched to the tmp root (the
    test_body_diary_carrier.py shape), which unions the carrier seeded under
    that same tmp world and nothing outside it.
    """
    backend = _get_backend()
    for name in fleet_agent_names(base):
        text, _provenance = read_agent_diary(name, base, backend=backend)
        if not text:
            continue
        if base is not None:
            # : a hermetic test root must not union live carrier rows
            # in — the carrier root cannot be derived from this agents root.
            yield name, text
            continue
        carrier = _body_carrier_lines(name, backend)
        if not carrier:
            yield name, text
            continue
        existing = {ln.strip() for ln in text.splitlines() if ln.strip()}
        extra = [ln for ln in carrier if ln not in existing]
        if not extra:
            yield name, text
            continue
        yield name, text.rstrip("\n") + "\n" + "\n".join(extra) + "\n"
