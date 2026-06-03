"""Content-addressed snapshot store with binary-delta compression.

Stage 0 prototype (2026-05-22). NOT YET WIRED into save_history. See
the integration plan in conversation around fix-ballooning-history.

Design goals:
- OneDrive-safe: every storage file is write-once-immutable. Names are
  content-addressed (sha256) for blobs/patches and unique-per-write
  (timestamp + agent) for manifests. No file is ever rewritten in place.
- Pure stdlib: no git binary dep, no PyPI deps. hashlib + gzip + difflib.
- Multi-machine via OneDrive sync: two machines independently writing
  different content produce different filenames -> no conflict.
- Bounded restore latency: every Nth save forces a full blob (anchor),
  capping the patch chain length.
- Tunable freezing: vacuum can rewrite stale manifests to encoding=dropped
  to free the underlying blob/patch storage while preserving the
  audit-trail metadata forever.

Storage layout under <base_dir>/.history/:

    blobs/<hash[:2]>/<hash[2:]>.gz
        Content-addressed full snapshots. Filename = sha256(uncompressed
        content), payload = gzip(content). Idempotent: same content has
        the same filename, so duplicate writes are no-ops.

    patches/<hash[:2]>/<hash[2:]>.from.<base>.gz
        Binary delta from blob <base> to content <hash>. Payload =
        gzip(line-opcode JSON). Filename embeds both hashes so deltas
        between different (target, base) pairs never collide.

    snapshots/<rel-path-to-file>/<ts>_<agent>.yaml
        Per-snapshot manifest. Tiny (~250 bytes). Names sort lexically
        by timestamp; agent disambiguates within a single subsecond.

Each manifest YAML:

    hash: <sha256>           # 64 hex chars, identifies the content
    encoding: full | delta | dropped
    base: <sha256> | null    # only when encoding=delta
    size_bytes: <int>        # uncompressed size
    agent: <string>
    summary: <string>        # may be empty
    timestamp: <ISO 8601>
    chain_length: <int>      # 0 for full; N>0 for delta-chain depth

Notes:
- encoding=dropped means a vacuum pass deleted the blob/patch storage but
  left the manifest as audit-trail metadata.
- chain_length is read at save time: when prior.chain_length ==
  anchor_interval - 1, the next save MUST be a full blob (anchor).

Public API:
    save(file_path, content_bytes, base_dir, agent, summary='', anchor_interval=20)
        -> manifest_path
    restore(file_path, snapshot_id, base_dir) -> bytes
    list_snapshots(file_path, base_dir) -> list[dict]
    vacuum(base_dir, dry_run=True, metadata_only_after_days=None) -> dict
"""

import difflib
import gzip
import hashlib
import io
import json
import os
import sys
from datetime import datetime
from pathlib import Path


DEFAULT_ANCHOR_INTERVAL = 20
DEFAULT_DELTA_SAVINGS_THRESHOLD = 0.5  # delta must be < 50% of gzip'd full
DEFAULT_FULL_BLOB_MAX_SIZE = 5 * 1024 * 1024  # 5MB: skip delta attempt above
BINARY_SAMPLE_BYTES = 8192  # bytes to scan for null when detecting text


# ---------------------------------------------------------------------------
# Hashing + path helpers
# ---------------------------------------------------------------------------

def _hash(content_bytes):
    """sha256 hex digest of bytes."""
    return hashlib.sha256(content_bytes).hexdigest()


def _looks_like_text(content_bytes):
    """Heuristic: no null byte in first BINARY_SAMPLE_BYTES => probably text."""
    return b"\x00" not in content_bytes[:BINARY_SAMPLE_BYTES]


def _blob_path(base_dir, content_hash):
    return Path(base_dir) / ".history" / "blobs" / content_hash[:2] / f"{content_hash[2:]}.gz"


def _patch_path(base_dir, content_hash, base_hash):
    return Path(base_dir) / ".history" / "patches" / content_hash[:2] / f"{content_hash[2:]}.from.{base_hash}.gz"


def _manifest_dir(base_dir, file_path):
    """Dir holding manifests for file_path. file_path may be absolute or relative."""
    file_path = Path(file_path)
    base_dir = Path(base_dir)
    try:
        rel = file_path.resolve().relative_to(base_dir.resolve())
    except (ValueError, OSError):
        # file_path is already relative or doesn't share base_dir's prefix --
        # treat as relative to base_dir.
        rel = file_path
    return base_dir / ".history" / "snapshots" / str(rel)


def _now_iso():
    """ISO 8601 timestamp with microsecond precision (local time, FS-safe colons)."""
    return datetime.now().strftime("%Y-%m-%dT%H-%M-%S.%f")


# ---------------------------------------------------------------------------
# Atomic write (idempotent for content-addressed names)
# ---------------------------------------------------------------------------

def _unique_tmp(target):
    """Return a unique-per-writer .tmp Path for target.

    Defends against concurrent writers racing on the same content-addressed
    blob (cross-file dedup case: two source files with identical content
    both write to the same blob in parallel). Deterministic tmp names
    collide; pid+random hex doesn't.
    """
    suffix = f".{os.getpid()}-{os.urandom(8).hex()}.tmp"
    return target.with_suffix(target.suffix + suffix)


def _atomic_write_bytes(target, content_bytes):
    """Write bytes atomically via unique .tmp + os.replace. Idempotent: if
    target already exists, skip (content-addressed names guarantee identical
    bytes).
    """
    target = Path(target)
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = _unique_tmp(target)
    try:
        tmp.write_bytes(content_bytes)
        os.replace(str(tmp), str(target))
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _atomic_write_text(target, text):
    """Write text atomically via unique .tmp + os.replace.

    Manifest filenames are unique by construction (ts + agent), but the
    .tmp suffix is still randomized as defense-in-depth: a same-agent
    same-microsecond race would otherwise collide on the .tmp.
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = _unique_tmp(target)
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(str(tmp), str(target))
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# gzip helpers
# ---------------------------------------------------------------------------

def _gzip_bytes(content_bytes):
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6, mtime=0) as gz:
        gz.write(content_bytes)
    return buf.getvalue()


def _gunzip_bytes(gzipped_bytes):
    return gzip.decompress(gzipped_bytes)


# ---------------------------------------------------------------------------
# Delta encode / decode (line-based opcodes, serialized as gzip'd JSON)
# ---------------------------------------------------------------------------

def _encode_delta(base_bytes, current_bytes):
    """Compute a line-based delta from base_bytes to current_bytes.

    Returns:
        bytes: gzip'd JSON encoding the opcodes + inserted line blocks.
        None: delta encoding not applicable (binary, decode error, etc.)
    """
    if not _looks_like_text(base_bytes) or not _looks_like_text(current_bytes):
        return None
    try:
        base_text = base_bytes.decode("utf-8")
        current_text = current_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None

    base_lines = base_text.splitlines(keepends=True)
    current_lines = current_text.splitlines(keepends=True)

    matcher = difflib.SequenceMatcher(None, base_lines, current_lines, autojunk=False)
    opcodes = matcher.get_opcodes()

    inserts = []
    op_list = []
    for op, i1, i2, j1, j2 in opcodes:
        op_list.append([op, i1, i2, j1, j2])
        if op in ("insert", "replace"):
            inserts.append(current_lines[j1:j2])

    delta_obj = {
        "format": "lines_v1",
        "opcodes": op_list,
        "inserts": inserts,
    }
    delta_json = json.dumps(delta_obj, ensure_ascii=False)
    return _gzip_bytes(delta_json.encode("utf-8"))


def _apply_delta(base_bytes, delta_bytes):
    """Apply a gzip'd-JSON delta to base_bytes; return reconstructed bytes."""
    delta_json = _gunzip_bytes(delta_bytes).decode("utf-8")
    delta = json.loads(delta_json)
    if delta.get("format") != "lines_v1":
        raise ValueError(f"Unknown delta format: {delta.get('format')!r}")

    base_text = base_bytes.decode("utf-8")
    base_lines = base_text.splitlines(keepends=True)

    result_lines = []
    insert_idx = 0
    for op, i1, i2, j1, j2 in delta["opcodes"]:
        if op == "equal":
            result_lines.extend(base_lines[i1:i2])
        elif op == "delete":
            pass
        elif op in ("insert", "replace"):
            result_lines.extend(delta["inserts"][insert_idx])
            insert_idx += 1
        else:
            raise ValueError(f"Unknown opcode: {op!r}")
    return "".join(result_lines).encode("utf-8")


# ---------------------------------------------------------------------------
# Manifest helpers (tiny flat YAML; no PyYAML dep)
# ---------------------------------------------------------------------------

_MANIFEST_KEYS = ("hash", "encoding", "base", "size_bytes",
                  "agent", "summary", "timestamp", "chain_length")


def _list_manifest_files(base_dir, file_path):
    """Return manifest files for file_path, newest-first by lex sort on name."""
    mdir = _manifest_dir(base_dir, file_path)
    if not mdir.exists():
        return []
    return sorted(
        [p for p in mdir.iterdir() if p.is_file() and p.name.endswith(".yaml")],
        reverse=True,
    )


def _read_manifest(manifest_path):
    """Parse a manifest YAML file into a dict. Fixed-schema, line-based parser."""
    result = {}
    for line in Path(manifest_path).read_text(encoding="utf-8").splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value == "null" or value == "":
            result[key] = None
            continue
        if value.lstrip("-").isdigit():
            result[key] = int(value)
            continue
        if value.startswith('"') and value.endswith('"'):
            result[key] = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            continue
        result[key] = value
    return result


def _serialize_manifest(data):
    """Serialize a manifest dict to flat YAML text. Single source of truth
    for the manifest wire format, shared by fresh writes and vacuum-drop
    rewrites so the two paths can't drift on schema changes."""
    lines = []
    for key in _MANIFEST_KEYS:
        value = data.get(key)
        if value is None:
            lines.append(f"{key}: null")
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, int):
            lines.append(f"{key}: {value}")
        else:
            text = str(value)
            if any(c in text for c in ':#\n"'):
                escaped = text.replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f'{key}: "{escaped}"')
            else:
                lines.append(f"{key}: {text}")
    return "\n".join(lines) + "\n"


def _write_manifest(manifest_path, data):
    """Serialize manifest dict to YAML and atomic-write to manifest_path.

    Used for both fresh writes (new snapshot) and vacuum-drop rewrites
    (encoding=dropped). The serializer is shared via _serialize_manifest;
    no second serializer can drift from this one.
    """
    _atomic_write_text(manifest_path, _serialize_manifest(data))


# ---------------------------------------------------------------------------
# Public API: save
# ---------------------------------------------------------------------------

def save(file_path, content_bytes, base_dir, agent, summary="",
         anchor_interval=DEFAULT_ANCHOR_INTERVAL):
    """Save a snapshot of content_bytes for file_path.

    Writes a content-addressed blob OR patch + a unique-named manifest.
    All file writes are write-once-immutable (idempotent on retry).

    Returns the manifest file Path that was written.
    """
    content_hash = _hash(content_bytes)

    # Find immediately-prior snapshot, if any.
    prior_manifests = _list_manifest_files(base_dir, file_path)
    prior_manifest = _read_manifest(prior_manifests[0]) if prior_manifests else None

    # Fast path: identical content to prior. Skip blob/patch write; just
    # add a new manifest pointing at the same storage.
    if prior_manifest is not None and prior_manifest.get("hash") == content_hash:
        return _write_new_manifest(
            base_dir, file_path, agent, summary, content_bytes,
            content_hash=content_hash,
            encoding=prior_manifest.get("encoding", "full"),
            base_hash=prior_manifest.get("base"),
            chain_length=prior_manifest.get("chain_length", 0),
        )

    # Decide encoding: full or delta?
    encoding = "full"
    base_hash = None
    chain_length = 0
    delta_bytes = None

    can_try_delta = (
        prior_manifest is not None
        and prior_manifest.get("encoding") in ("full", "delta")
        and prior_manifest.get("chain_length", 0) < anchor_interval - 1
        and len(content_bytes) <= DEFAULT_FULL_BLOB_MAX_SIZE
    )
    if can_try_delta:
        try:
            prior_content = _resolve_chain(prior_manifest["hash"], base_dir)
            candidate = _encode_delta(prior_content, content_bytes)
            if candidate is not None:
                full_gz_size = len(_gzip_bytes(content_bytes))
                if len(candidate) < full_gz_size * DEFAULT_DELTA_SAVINGS_THRESHOLD:
                    encoding = "delta"
                    base_hash = prior_manifest["hash"]
                    chain_length = prior_manifest.get("chain_length", 0) + 1
                    delta_bytes = candidate
        except Exception as e:
            # Fall back to full blob, but log so silent regressions in
            # _encode_delta / _resolve_chain don't degrade the new store
            # unnoticed (storage would grow like the old gzip tree if
            # every save silently fell back).
            print(
                f"[_history_store] delta-attempt failed for {file_path}: "
                f"{type(e).__name__}: {e} — falling back to full encoding",
                file=sys.stderr,
            )
            encoding = "full"

    # Write storage (idempotent).
    if encoding == "full":
        _atomic_write_bytes(_blob_path(base_dir, content_hash),
                            _gzip_bytes(content_bytes))
    else:
        _atomic_write_bytes(_patch_path(base_dir, content_hash, base_hash),
                            delta_bytes)

    return _write_new_manifest(
        base_dir, file_path, agent, summary, content_bytes,
        content_hash=content_hash, encoding=encoding,
        base_hash=base_hash, chain_length=chain_length,
    )


def _write_new_manifest(base_dir, file_path, agent, summary, content_bytes,
                        *, content_hash, encoding, base_hash, chain_length):
    """Write a uniquely-named manifest. Returns the manifest Path."""
    timestamp = _now_iso()
    manifest_name = f"{timestamp}_{agent}.yaml"
    manifest_path = _manifest_dir(base_dir, file_path) / manifest_name
    _write_manifest(manifest_path, {
        "hash": content_hash,
        "encoding": encoding,
        "base": base_hash,
        "size_bytes": len(content_bytes),
        "agent": agent,
        "summary": summary,
        "timestamp": timestamp,
        "chain_length": chain_length,
    })
    return manifest_path


# ---------------------------------------------------------------------------
# Public API: restore
# ---------------------------------------------------------------------------

def restore(file_path, snapshot_id, base_dir):
    """Reconstruct content of file_path at snapshot_id (manifest filename).

    Returns the reconstructed bytes. Raises FileNotFoundError if snapshot_id
    is unknown, ValueError if the manifest has encoding=dropped.
    """
    manifest_path = _manifest_dir(base_dir, file_path) / snapshot_id
    if not manifest_path.exists():
        raise FileNotFoundError(f"Snapshot not found: {manifest_path}")
    manifest = _read_manifest(manifest_path)
    if manifest.get("encoding") == "dropped":
        raise ValueError(f"Snapshot {snapshot_id!r} content was vacuumed (metadata-only)")
    return _resolve_chain(manifest["hash"], base_dir)


def _resolve_chain(content_hash, base_dir, _seen=None):
    """Walk back to the closest full blob, then apply patches forward.

    _seen is a defense against accidental cycles (malformed patches that
    reference themselves transitively). Should never trigger in practice
    because content-addressed names cannot cycle.
    """
    if _seen is None:
        _seen = set()
    if content_hash in _seen:
        raise ValueError(f"Cycle detected resolving chain at {content_hash[:8]}")
    _seen.add(content_hash)

    blob = _blob_path(base_dir, content_hash)
    if blob.exists():
        return _gunzip_bytes(blob.read_bytes())

    patches_dir = Path(base_dir) / ".history" / "patches" / content_hash[:2]
    if not patches_dir.exists():
        raise FileNotFoundError(f"No blob or patch for hash {content_hash[:8]}")

    target_prefix = f"{content_hash[2:]}.from."
    for patch_file in patches_dir.iterdir():
        if not patch_file.name.startswith(target_prefix):
            continue
        if not patch_file.name.endswith(".gz"):
            continue
        # Filename: <62chars>.from.<64chars>.gz
        base_hash = patch_file.name[len(target_prefix):-len(".gz")]
        base_content = _resolve_chain(base_hash, base_dir, _seen=_seen)
        return _apply_delta(base_content, patch_file.read_bytes())

    raise FileNotFoundError(f"No reachable blob or patch for hash {content_hash[:8]}")


# ---------------------------------------------------------------------------
# Public API: list
# ---------------------------------------------------------------------------

def list_snapshots(file_path, base_dir):
    """Return snapshots for file_path, newest-first.

    Each entry: {snapshot_id, timestamp, agent, summary, encoding, size_bytes}.
    """
    out = []
    for manifest_path in _list_manifest_files(base_dir, file_path):
        m = _read_manifest(manifest_path)
        out.append({
            "snapshot_id": manifest_path.name,
            "timestamp": m.get("timestamp"),
            "agent": m.get("agent"),
            "summary": m.get("summary"),
            "encoding": m.get("encoding"),
            "size_bytes": m.get("size_bytes"),
        })
    return out


# ---------------------------------------------------------------------------
# Public API: vacuum
# ---------------------------------------------------------------------------

def vacuum(base_dir, dry_run=True, metadata_only_after_days=None):
    """Walk manifests, identify reachable blobs/patches, delete orphans.

    If metadata_only_after_days is given, manifests older than that whose
    encoding is full/delta are rewritten to encoding=dropped before the
    reachability scan, freeing their underlying storage.

    Returns dict with counts: manifests_dropped, blobs_deleted,
    patches_deleted, bytes_freed.
    """
    base_dir = Path(base_dir)
    history_dir = base_dir / ".history"
    result = {"manifests_dropped": 0, "blobs_deleted": 0,
              "patches_deleted": 0, "bytes_freed": 0}
    if not history_dir.exists():
        return result

    snapshots_root = history_dir / "snapshots"

    # Phase 1: optionally rewrite stale manifests to encoding=dropped.
    if metadata_only_after_days is not None and snapshots_root.exists():
        cutoff = datetime.now().timestamp() - metadata_only_after_days * 86400
        for m_path in snapshots_root.rglob("*.yaml"):
            try:
                if m_path.stat().st_mtime > cutoff:
                    continue
                m = _read_manifest(m_path)
                if m.get("encoding") not in ("full", "delta"):
                    continue
                if not dry_run:
                    m["encoding"] = "dropped"
                    m["base"] = None
                    _write_manifest(m_path, m)
                result["manifests_dropped"] += 1
            except OSError:
                continue

    # Phase 2a: validate all manifests BEFORE marking anything reachable.
    # A corrupt manifest (unknown encoding, missing hash/base) would
    # silently fall through the encoding switch below, its blobs/patches
    # would not be added to the reachable set, and Phase 3/4 would delete
    # them as orphans — silently destroying real snapshot content.
    # Fail-safe: refuse to vacuum until corrupt manifests are fixed.
    result["aborted"] = None
    result["corrupt_manifests"] = []
    if snapshots_root.exists():
        for m_path in snapshots_root.rglob("*.yaml"):
            try:
                m = _read_manifest(m_path)
            except OSError as e:
                result["corrupt_manifests"].append(
                    (str(m_path), f"OSError: {e}"))
                continue
            enc = m.get("encoding")
            if enc not in ("full", "delta", "dropped"):
                result["corrupt_manifests"].append(
                    (str(m_path), f"unknown encoding={enc!r}"))
                continue
            if enc in ("full", "delta") and not m.get("hash"):
                result["corrupt_manifests"].append(
                    (str(m_path), "missing hash"))
                continue
            if enc == "delta" and not m.get("base"):
                result["corrupt_manifests"].append(
                    (str(m_path), "missing base"))
                continue
    if result["corrupt_manifests"]:
        result["aborted"] = "corrupt_manifests_detected"
        print(
            f"[_history_store.vacuum] ABORTED: "
            f"{len(result['corrupt_manifests'])} corrupt manifest(s) "
            f"detected — refusing to delete anything until they are "
            f"fixed (otherwise their referenced blobs/patches would be "
            f"silently treated as orphans and deleted):",
            file=sys.stderr,
        )
        for p, reason in result["corrupt_manifests"][:10]:
            print(f"  {p}: {reason}", file=sys.stderr)
        return result

    # Phase 2b: collect reachable blob/patch identifiers. Safe to assume
    # well-formed manifests now (Phase 2a refused to proceed otherwise).
    reachable_blobs = set()
    reachable_patches = set()  # set of (target_hash, base_hash)
    if snapshots_root.exists():
        for m_path in snapshots_root.rglob("*.yaml"):
            try:
                m = _read_manifest(m_path)
            except OSError:
                continue  # Unreachable: Phase 2a would have aborted.
            enc = m.get("encoding")
            if enc == "full":
                reachable_blobs.add(m["hash"])
            elif enc == "delta":
                reachable_patches.add((m["hash"], m["base"]))
                _mark_chain_reachable(m["base"], base_dir,
                                      reachable_blobs, reachable_patches)
            # encoding=dropped: nothing to mark.

    # Phase 3: delete unreachable blobs.
    blobs_root = history_dir / "blobs"
    if blobs_root.exists():
        for blob_file in blobs_root.rglob("*.gz"):
            # Reconstruct hash from parent-dir + stem (strip .gz)
            blob_hash = blob_file.parent.name + blob_file.name[:-len(".gz")]
            if blob_hash in reachable_blobs:
                continue
            try:
                result["bytes_freed"] += blob_file.stat().st_size
                if not dry_run:
                    blob_file.unlink()
                result["blobs_deleted"] += 1
            except OSError:
                continue

    # Phase 4: delete unreachable patches.
    patches_root = history_dir / "patches"
    if patches_root.exists():
        for patch_file in patches_root.rglob("*.gz"):
            stem = patch_file.name[:-len(".gz")]
            if ".from." not in stem:
                continue
            target_part, _, base_hash = stem.partition(".from.")
            target_hash = patch_file.parent.name + target_part
            if (target_hash, base_hash) in reachable_patches:
                continue
            try:
                result["bytes_freed"] += patch_file.stat().st_size
                if not dry_run:
                    patch_file.unlink()
                result["patches_deleted"] += 1
            except OSError:
                continue

    return result


def _mark_chain_reachable(content_hash, base_dir, reachable_blobs, reachable_patches, _seen=None):
    """Mark all blobs/patches required to reconstruct content_hash."""
    if _seen is None:
        _seen = set()
    if content_hash in _seen:
        return
    _seen.add(content_hash)

    if _blob_path(base_dir, content_hash).exists():
        reachable_blobs.add(content_hash)
        return

    patches_dir = Path(base_dir) / ".history" / "patches" / content_hash[:2]
    if not patches_dir.exists():
        return  # orphan; nothing to mark
    target_prefix = f"{content_hash[2:]}.from."
    for patch_file in patches_dir.iterdir():
        if not patch_file.name.startswith(target_prefix):
            continue
        if not patch_file.name.endswith(".gz"):
            continue
        base_hash = patch_file.name[len(target_prefix):-len(".gz")]
        reachable_patches.add((content_hash, base_hash))
        _mark_chain_reachable(base_hash, base_dir,
                              reachable_blobs, reachable_patches, _seen=_seen)
        return  # one patch path is enough


# ---------------------------------------------------------------------------
# Tiny CLI for ad-hoc poking
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="CAS-delta history store (Stage 0)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    list_p = sub.add_parser("list", help="List snapshots for a file")
    list_p.add_argument("file")
    list_p.add_argument("--base-dir", required=True)

    vacuum_p = sub.add_parser("vacuum", help="Identify (and optionally delete) orphan storage")
    vacuum_p.add_argument("--base-dir", required=True)
    vacuum_p.add_argument("--apply", action="store_true",
                          help="Actually delete (default: dry-run)")
    vacuum_p.add_argument("--metadata-only-after-days", type=int, default=None)

    args = parser.parse_args()

    if args.cmd == "list":
        for s in list_snapshots(args.file, args.base_dir):
            print(f"  {s['snapshot_id']}  {s['timestamp']}  by {s['agent']}  "
                  f"({s['encoding']}, {s['size_bytes']} bytes)")
            if s.get("summary"):
                print(f"      {s['summary']}")
    elif args.cmd == "vacuum":
        r = vacuum(args.base_dir, dry_run=not args.apply,
                   metadata_only_after_days=args.metadata_only_after_days)
        verb = "would delete" if not args.apply else "deleted"
        print(f"manifests dropped: {r['manifests_dropped']}")
        print(f"blobs {verb}: {r['blobs_deleted']}")
        print(f"patches {verb}: {r['patches_deleted']}")
        print(f"bytes {('would be ' if not args.apply else '')}freed: {r['bytes_freed']:,}")


if __name__ == "__main__":
    main()
