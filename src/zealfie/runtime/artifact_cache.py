"""Shared verified artifact cache (ZA-M1-3A.3 — LOT C+D).

Content-addressed persistent store of immutable, already-verified
artifacts: product wheels (KEEP), dependency wheels (wheelhouse), and
accelerated GPU wheels.  The cache is shared between transactions and is
completely independent of slots (it lives under ``<runtime_root>/cache/``,
outside ``slots/``).

THE CACHE IS AN OPTIMIZATION ONLY — it is never a source of authority:

* every reuse is gated on byte-level digest re-verification against an
  identity the caller already holds (provenance wheel digest, resolved
  dependency identity, manifest sha256);
* an absent, corrupted, or digest-mismatching artifact is always a
  MISS → normal re-acquisition;
* a corrupted index is ignored and rebuilt from the content-addressed
  store — it can never cause an unverified artifact to be activated;
* a wheel is never reconstructed from an active slot's site-packages.

Storage layout (``<runtime_root>/cache/artifacts``)::

    wheels/<sha256>/<filename>   -- one immutable artifact per digest
    index.json                   -- rebuildable metadata index

Index schema::

    {
      "schema_version": 1,
      "wheels": {
        "<sha256>": {
          "distribution": "...",   # canonicalized (PEP 503)
          "version": "...",
          "filename": "...",
          "size": 123,
          "tags": ["py3", "none", "any"],
          "kind": "product" | "dependency" | "accelerated" | null
        }
      }
    }

Retention (LOT D): :func:`runtime_cache_gc` deletes only artifacts that
are NOT referenced by the persisted slot state stores (product provenance
wheel digests, installed-lock dependency identities, accelerated metadata
variant digests).  The ACTIVE/PREVIOUS protection set required by the
mission is a strict subset of that union, so the invariant "keep at least
ACTIVE + PREVIOUS + in-flight references" holds.  No fixed size quota is
introduced: retention is bounded by *state references*, and unreferenced
artifacts may become GC candidates (a later need is an ordinary cache
miss).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from packaging.utils import canonicalize_name, parse_wheel_filename

from .layout import RuntimeLayout
from .mutation_lock import OPERATION_RUNTIME_GC, RuntimeMutationLock

logger = logging.getLogger(__name__)

ARTIFACT_CACHE_SCHEMA_VERSION = 1

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CHUNK_SIZE = 1 << 20  # 1 MiB

#: State store filenames read (leniently) to collect the protected set.
_STATE_FILENAMES = (
    "product-provenance.json",
    "installed-lock.json",
    "accelerated-metadata.json",
)


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CachedArtifactEntry:
    """Metadata for one cached artifact.

    ``sha256`` is the content address; ``distribution`` / ``version`` /
    ``filename`` / ``size`` / ``tags`` describe the artifact.  ``kind`` is
    informational (``"product"`` / ``"dependency"`` / ``"accelerated"`` /
    ``None``) and is never used as an authority for reuse decisions.
    """

    sha256: str
    distribution: str
    version: str
    filename: str
    size: int
    tags: tuple[str, ...] = ()
    kind: str | None = None

    def __post_init__(self) -> None:
        sha = str(self.sha256 or "").strip().lower()
        if not _SHA256_RE.match(sha):
            raise ValueError(f"cache entry sha256 is invalid: {self.sha256!r}")
        object.__setattr__(self, "sha256", sha)
        if not str(self.distribution or "").strip():
            raise ValueError("cache entry distribution must not be empty")
        if not str(self.version or "").strip():
            raise ValueError("cache entry version must not be empty")


@dataclass(frozen=True, slots=True)
class CacheGcPlan:
    """Immutable artifact-cache GC plan (candidates are unreferenced files)."""

    cache_root: Path
    candidates: tuple[Path, ...]
    retained: int


@dataclass(frozen=True, slots=True)
class CacheGcResult:
    """Outcome of an applied artifact-cache GC (best-effort, never partial truth)."""

    deleted: tuple[Path, ...]
    reclaimed_bytes: int
    errors: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class ArtifactCacheStore:
    """Content-addressed verified-artifact cache.

    Bound to ``<runtime_root>/cache/artifacts``.  Thread-unsafe by design
    (single-owner at the service layer, mirroring the other runtime
    stores); writes are atomic (temp file + fsync + ``os.replace``).

    All reuse APIs verify the file digest against the *caller-supplied*
    identity before returning a path.  All fill APIs are best-effort and
    never raise.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root).resolve()

    # -- paths ----------------------------------------------------------------

    @property
    def root(self) -> Path:
        return self._root

    @property
    def wheels_dir(self) -> Path:
        return self._root / "wheels"

    @property
    def index_path(self) -> Path:
        return self._root / "index.json"

    # -- fill (best-effort, never raises) ------------------------------------

    def put(
        self,
        wheel_path: Path | str,
        *,
        kind: str | None = None,
        distribution: str | None = None,
        version: str | None = None,
    ) -> CachedArtifactEntry | None:
        """Copy *wheel_path* into the cache and index it.

        Identity: explicit *distribution* / *version* win; otherwise they
        are derived from the wheel filename (PEP 427).  *tags* are always
        derived from the filename.  The digest is computed from the actual
        file bytes — never from caller claims.  Returns the entry, or
        ``None`` on any failure (fill is an optimization; failures are
        logged and never raised).
        """
        try:
            source = Path(wheel_path).resolve(strict=True)
            size, sha256 = _file_size_and_sha256(source)

            parsed_name: str | None = None
            parsed_version: str | None = None
            tags: tuple[str, ...] = ()
            try:
                raw_name, raw_version, _, parsed_tags = parse_wheel_filename(
                    source.name
                )
                parsed_name = canonicalize_name(raw_name)
                parsed_version = str(raw_version)
                tags = tuple(sorted(str(t) for t in parsed_tags))
            except Exception:
                # Unparseable filename → cannot index reliably.  Never cache.
                logger.warning(
                    "artifact cache: refusing to cache %s (unparseable "
                    "wheel filename)",
                    source,
                )
                return None

            entry = CachedArtifactEntry(
                sha256=sha256,
                distribution=(
                    canonicalize_name(distribution)
                    if distribution
                    else parsed_name
                ),
                version=str(version) if version is not None else parsed_version,
                filename=source.name,
                size=size,
                tags=tags,
                kind=kind,
            )

            target_dir = self.wheels_dir / sha256
            target = target_dir / source.name
            if target.is_file():
                t_size, t_digest = _file_size_and_sha256(target)
                if t_size != size or t_digest != sha256:
                    # Corrupt bytes under a correct content address: replace.
                    target.unlink()
            if not target.is_file():
                target_dir.mkdir(parents=True, exist_ok=True)
                _atomic_copy(source, target)
            self._merge_index_entry(entry)
            return entry
        except Exception as exc:  # noqa: BLE001 - fill is best-effort
            logger.warning("artifact cache put failed for %s: %s", wheel_path, exc)
            return None

    # -- reuse (verify-then-use) ----------------------------------------------

    def cached_path_for_digest(
        self,
        sha256: str,
        *,
        expected_size: int | None = None,
    ) -> Path | None:
        """Return a cache file whose bytes hash to *sha256*, or ``None``.

        Content-addressed: scans ``wheels/<sha256>/`` and re-computes the
        digest of every candidate file.  Never trusts the index, the
        filename, or mere presence — a tampered or truncated file is
        always a MISS.
        """
        if not isinstance(sha256, str) or not _SHA256_RE.match(sha256.strip()):
            return None
        sha256 = sha256.strip().lower()
        candidate_dir = self.wheels_dir / sha256
        if not candidate_dir.is_dir():
            return None
        for candidate in sorted(candidate_dir.iterdir()):
            if not candidate.is_file() or candidate.name.endswith(".part"):
                continue
            try:
                size, digest = _file_size_and_sha256(candidate)
            except OSError:
                continue
            if digest == sha256 and (
                expected_size is None or size == expected_size
            ):
                return candidate
        return None

    def resolve_dependency(
        self,
        name: str,
        version: str,
        *,
        compatible_tags: frozenset | None = None,
    ) -> Path | None:
        """Return a verified cached wheel for dependency *(name, version)*.

        Fail-closed: exactly ONE index entry must match the identity, its
        file must exist with the recorded digest and size, and (when
        *compatible_tags* is provided) at least one wheel tag must be host
        compatible.  Zero matches, ambiguity, bad digest, or incompatible
        tags → ``None`` (fall back to normal acquisition; never invent a
        proof).
        """
        try:
            canon = canonicalize_name(str(name))
        except Exception:
            return None
        version = str(version)
        matches = [
            entry
            for entry in self.load_index().values()
            if entry.distribution == canon and entry.version == version
        ]
        if len(matches) != 1:
            return None
        entry = matches[0]
        path = self.cached_path_for_digest(
            entry.sha256, expected_size=entry.size
        )
        if path is None:
            return None
        if compatible_tags is not None:
            if not entry.tags:
                return None
            host_tags = frozenset(str(t) for t in compatible_tags)
            if not (set(entry.tags) & host_tags):
                return None
        return path

    def resolve_accelerated(
        self,
        *,
        sha256: str,
        distribution: str,
        version: str,
        filename: str,
        size: int,
    ) -> Path | None:
        """Return a verified cached accelerated wheel for a manifest entry.

        The manifest entry is the authority: the cached file must hash to
        *sha256* with the exact *size* and *filename*, and its wheel
        filename identity must match *distribution* / *version*.  Any
        mismatch → ``None`` (normal download).
        """
        path = self.cached_path_for_digest(sha256, expected_size=size)
        if path is None:
            return None
        if path.name != filename:
            return None
        try:
            raw_name, raw_version, _, _ = parse_wheel_filename(path.name)
        except Exception:
            return None
        try:
            same_name = canonicalize_name(raw_name) == canonicalize_name(
                distribution
            )
        except Exception:
            same_name = False
        if not same_name or str(raw_version) != str(version):
            return None
        return path

    # -- index ---------------------------------------------------------------

    def load_index(self) -> dict[str, CachedArtifactEntry]:
        """Load the index leniently.

        Missing file → ``{}``.  Corrupt file, unexpected schema, or
        malformed entries are dropped; a structurally corrupt index is
        rebuilt from the content-addressed store (the index is a cache of
        metadata, never an authority).  Never raises.
        """
        path = self.index_path
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(
                "artifact cache index unreadable (%s); rebuilding from "
                "content-addressed store",
                exc,
            )
            return self._rebuild_index_entries()
        if not isinstance(payload, dict) or (
            payload.get("schema_version") != ARTIFACT_CACHE_SCHEMA_VERSION
        ):
            logger.warning(
                "artifact cache index has unexpected schema; rebuilding "
                "from content-addressed store"
            )
            return self._rebuild_index_entries()
        wheels = payload.get("wheels")
        if not isinstance(wheels, dict):
            logger.warning(
                "artifact cache index has no 'wheels' object; rebuilding "
                "from content-addressed store"
            )
            return self._rebuild_index_entries()

        entries: dict[str, CachedArtifactEntry] = {}
        for sha, raw in wheels.items():
            if not isinstance(sha, str) or not _SHA256_RE.match(sha):
                continue
            if not isinstance(raw, dict):
                continue
            try:
                entries[sha] = _entry_from_payload(sha, raw)
            except Exception:
                continue
        return entries

    def rebuild_index(self) -> int:
        """Rebuild the index from the content-addressed store.

        Re-hashes every cached file and keeps only entries whose content
        matches their address.  Returns the number of entries rebuilt.
        Never raises.
        """
        try:
            entries = self._rebuild_index_entries()
            self._write_index(entries)
            return len(entries)
        except Exception as exc:  # noqa: BLE001
            logger.warning("artifact cache index rebuild failed: %s", exc)
            return 0

    def prune_index_entries(self) -> list[str]:
        """Drop index entries whose files no longer exist.

        Returns error strings (best-effort; a corrupt index is left for
        the next lazy rebuild).  Never raises.
        """
        try:
            if not self.index_path.is_file():
                return []
            entries = self.load_index()
            kept: dict[str, CachedArtifactEntry] = {}
            for sha, entry in entries.items():
                target = self.wheels_dir / sha / entry.filename
                if target.is_file():
                    kept[sha] = entry
            if len(kept) == len(entries):
                return []
            self._write_index(kept)
            return []
        except Exception as exc:  # noqa: BLE001
            return [f"index prune failed: {exc}"]

    # -- internals ------------------------------------------------------------

    def _merge_index_entry(self, entry: CachedArtifactEntry) -> None:
        """Merge one entry into the index and write it atomically."""
        entries = self.load_index()
        entries[entry.sha256] = entry
        self._write_index(entries)

    def _write_index(
        self, entries: dict[str, CachedArtifactEntry]
    ) -> None:
        wheels: dict[str, dict] = {}
        for sha in sorted(entries):
            entry = entries[sha]
            wheels[sha] = {
                "distribution": entry.distribution,
                "version": entry.version,
                "filename": entry.filename,
                "size": entry.size,
                "tags": list(entry.tags),
                "kind": entry.kind,
            }
        payload = {
            "schema_version": ARTIFACT_CACHE_SCHEMA_VERSION,
            "wheels": wheels,
        }
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        self._root.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            suffix=".json", prefix=".index-", dir=str(self._root)
        )
        try:
            os.write(fd, text.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_name, str(self.index_path))

    def _rebuild_index_entries(self) -> dict[str, CachedArtifactEntry]:
        """Scan the content-addressed store; keep only verified entries."""
        entries: dict[str, CachedArtifactEntry] = {}
        wheels_dir = self.wheels_dir
        if not wheels_dir.is_dir():
            return entries
        for sha_dir in sorted(wheels_dir.iterdir()):
            if not sha_dir.is_dir():
                continue
            sha = sha_dir.name
            if not _SHA256_RE.match(sha):
                continue
            for candidate in sorted(sha_dir.iterdir()):
                if not candidate.is_file() or candidate.name.endswith(".part"):
                    continue
                try:
                    size, digest = _file_size_and_sha256(candidate)
                except OSError:
                    continue
                if digest != sha:
                    continue
                try:
                    raw_name, raw_version, _, parsed_tags = parse_wheel_filename(
                        candidate.name
                    )
                    name = canonicalize_name(raw_name)
                    version = str(raw_version)
                    tags = tuple(sorted(str(t) for t in parsed_tags))
                except Exception:
                    continue
                entries[sha] = CachedArtifactEntry(
                    sha256=sha,
                    distribution=name,
                    version=version,
                    filename=candidate.name,
                    size=size,
                    tags=tags,
                    kind=None,
                )
        return entries


# ---------------------------------------------------------------------------
# Materialization helper (cache → work dir), used by the pip + GPU paths
# ---------------------------------------------------------------------------


def materialize_cached(cached_path: Path | str, dest: Path | str) -> bool:
    """Copy a verified cached artifact to *dest* atomically (best-effort).

    Returns ``False`` on any failure — callers fall back to their normal
    acquisition path.  Never raises.
    """
    try:
        source = Path(cached_path).resolve(strict=True)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        _atomic_copy(source, dest)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "artifact cache materialize failed for %s: %s", cached_path, exc
        )
        return False


# ---------------------------------------------------------------------------
# GC (LOT D)
# ---------------------------------------------------------------------------


def build_cache_gc_plan(
    cache_root: Path | str,
    *,
    protected_digests: frozenset[str] = frozenset(),
    protected_dependency_ids: frozenset[tuple[str, str]] = frozenset(),
) -> CacheGcPlan:
    """Classify cached artifacts; never writes anything.

    A file is RETAINED when its content address is in *protected_digests*
    or its ``(canonical distribution, version)`` identity (derived from
    the wheel filename) is in *protected_dependency_ids*.  Everything else
    is a GC candidate.  Digest values are matched as directory names — no
    file hashing is needed to establish protection (a tampered protected
    file simply becomes a future cache miss; it is never activated).
    """
    root = Path(cache_root).resolve()
    wheels_dir = root / "wheels"
    candidates: list[Path] = []
    retained = 0
    if wheels_dir.is_dir():
        for sha_dir in sorted(wheels_dir.iterdir()):
            if not sha_dir.is_dir():
                continue
            sha = sha_dir.name
            for candidate in sorted(sha_dir.iterdir()):
                if not candidate.is_file() or candidate.name.endswith(".part"):
                    continue
                if sha in protected_digests:
                    retained += 1
                    continue
                dep_id: tuple[str, str] | None = None
                try:
                    raw_name, raw_version, _, _ = parse_wheel_filename(
                        candidate.name
                    )
                    dep_id = (
                        canonicalize_name(raw_name),
                        str(raw_version),
                    )
                except Exception:
                    dep_id = None
                if dep_id is not None and dep_id in protected_dependency_ids:
                    retained += 1
                else:
                    candidates.append(candidate)
    return CacheGcPlan(
        cache_root=root,
        candidates=tuple(candidates),
        retained=retained,
    )


def apply_cache_gc_plan(
    cache_root: Path | str, plan: CacheGcPlan
) -> CacheGcResult:
    """Delete the plan's candidate files (best-effort) and sync the index.

    Per-file failures are collected as errors (partial, non-destructive
    cleanup; the next GC retries).  Never raises.
    """
    root = Path(cache_root).resolve()
    deleted: list[Path] = []
    reclaimed = 0
    errors: list[str] = []
    for candidate in plan.candidates:
        try:
            size = candidate.stat().st_size
            candidate.unlink()
            deleted.append(candidate)
            reclaimed += size
        except OSError as exc:
            errors.append(f"cannot delete {candidate}: {exc}")
    # Best-effort: remove now-empty digest dirs.
    wheels_dir = root / "wheels"
    if wheels_dir.is_dir():
        for sha_dir in sorted(wheels_dir.iterdir()):
            if not sha_dir.is_dir():
                continue
            try:
                if not any(
                    p.is_file() for p in sha_dir.iterdir()
                ):
                    sha_dir.rmdir()
            except OSError:
                pass
    store = ArtifactCacheStore(root)
    errors.extend(store.prune_index_entries())
    return CacheGcResult(
        deleted=tuple(deleted),
        reclaimed_bytes=reclaimed,
        errors=tuple(errors),
    )


def runtime_cache_gc(runtime_root: Path | str) -> CacheGcResult:
    """Collect the protected set from the runtime state stores and GC.

    Fail-closed: when any existing state store is unreadable or corrupt,
    the protected set cannot be established and NOTHING is deleted (the
    result carries the blocking reason in ``errors``).  The protected set
    is collected AND the deletion is planned/applied under the SAME
    runtime mutation lease (``OPERATION_RUNTIME_GC``), so an in-flight
    transaction's freshly-cached artifacts can never be collected (the
    transaction holds the lease between caching and its provenance
    commit, which closes the collect/delete TOCTOU window); raises
    :class:`~zealfie.runtime.mutation_lock.RuntimeMutationBusyError` when
    another writer holds the lease.
    """
    root = Path(runtime_root).resolve()
    layout = RuntimeLayout(root)
    cache_root = root / "cache" / "artifacts"
    if not cache_root.is_dir():
        return CacheGcResult(deleted=(), reclaimed_bytes=0)
    lock = RuntimeMutationLock(root)
    with lock.acquire(OPERATION_RUNTIME_GC):
        protected_digests, protected_dependency_ids, blocking = (
            _collect_state_protected_refs(layout.state_dir)
        )
        if blocking:
            return CacheGcResult(
                deleted=(),
                reclaimed_bytes=0,
                errors=("blocked: " + "; ".join(blocking),),
            )
        plan = build_cache_gc_plan(
            cache_root,
            protected_digests=protected_digests,
            protected_dependency_ids=protected_dependency_ids,
        )
        return apply_cache_gc_plan(cache_root, plan)


# ---------------------------------------------------------------------------
# Protected-set collection from the persisted slot state stores
# ---------------------------------------------------------------------------


def _collect_state_protected_refs(
    state_dir: Path,
) -> tuple[frozenset[str], frozenset[tuple[str, str]], list[str]]:
    """Collect (protected digests, protected dep identities, blocking reasons).

    Lenient per entry (a malformed value cannot reference a real cache
    artifact), strict per file (an unreadable/corrupt existing file blocks
    all deletion — the protected set cannot be established).
    """
    digests: set[str] = set()
    dep_ids: set[tuple[str, str]] = set()
    blocking: list[str] = []

    for filename in _STATE_FILENAMES:
        payload, error = _read_state_file(state_dir / filename)
        if error is not None:
            blocking.append(f"{filename}: {error}")
            continue
        if payload is None:
            continue
        slots = payload.get("slots")
        if not isinstance(slots, dict):
            blocking.append(f"{filename} has no 'slots' object")
            continue
        if filename == "product-provenance.json":
            for products in slots.values():
                if not isinstance(products, dict):
                    continue
                for entry in products.values():
                    if not isinstance(entry, dict):
                        continue
                    sha = entry.get("wheel_sha256")
                    if isinstance(sha, str) and _SHA256_RE.match(
                        sha.strip().lower()
                    ):
                        digests.add(sha.strip().lower())
        elif filename == "installed-lock.json":
            for lock in slots.values():
                if not isinstance(lock, dict):
                    continue
                dependencies = lock.get("dependencies")
                if not isinstance(dependencies, dict):
                    continue
                for dep in dependencies.values():
                    if not isinstance(dep, dict):
                        continue
                    name = dep.get("name")
                    version = dep.get("version")
                    if not isinstance(name, str) or not name.strip():
                        continue
                    if not isinstance(version, str) or not version.strip():
                        continue
                    try:
                        dep_ids.add((canonicalize_name(name), version))
                    except Exception:
                        continue
        elif filename == "accelerated-metadata.json":
            for metadata in slots.values():
                if not isinstance(metadata, dict):
                    continue
                variants = metadata.get("variants")
                if not isinstance(variants, list):
                    continue
                for variant in variants:
                    if not isinstance(variant, (list, tuple)):
                        continue
                    if len(variant) != 3:
                        continue
                    sha = variant[2]
                    if isinstance(sha, str) and _SHA256_RE.match(
                        sha.strip().lower()
                    ):
                        digests.add(sha.strip().lower())

    return frozenset(digests), frozenset(dep_ids), blocking


def _read_state_file(path: Path) -> tuple[dict | None, str | None]:
    """Read a JSON state store leniently: (payload, error)."""
    if not path.is_file():
        return None, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"unreadable: {exc}"
    if not isinstance(payload, dict):
        return None, "root must be a JSON object"
    return payload, None


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _entry_from_payload(sha: str, raw: dict) -> CachedArtifactEntry:
    """Build a cache entry from a raw index object (raises on malformed)."""
    distribution = raw.get("distribution")
    version = raw.get("version")
    filename = raw.get("filename")
    size = raw.get("size")
    if not isinstance(distribution, str) or not distribution.strip():
        raise ValueError("entry distribution is missing")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("entry version is missing")
    if (
        not isinstance(filename, str)
        or not filename.strip()
        or not filename.endswith(".whl")
        or "/" in filename
        or "\\" in filename
    ):
        raise ValueError("entry filename is invalid")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise ValueError("entry size is invalid")
    tags_raw = raw.get("tags")
    if tags_raw is None:
        tags = ()
    elif isinstance(tags_raw, list) and all(
        isinstance(t, str) for t in tags_raw
    ):
        tags = tuple(tags_raw)
    else:
        raise ValueError("entry tags are invalid")
    kind = raw.get("kind")
    if kind is not None and not isinstance(kind, str):
        raise ValueError("entry kind is invalid")
    return CachedArtifactEntry(
        sha256=sha,
        distribution=distribution,
        version=version,
        filename=filename,
        size=size,
        tags=tags,
        kind=kind,
    )


def _file_size_and_sha256(path: Path) -> tuple[int, str]:
    """Return ``(size, sha256_hex)`` of a file (chunked read)."""
    sha = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK_SIZE):
            size += len(chunk)
            sha.update(chunk)
    return size, sha.hexdigest()


def _atomic_copy(source: Path, dest: Path) -> None:
    """Copy *source* to *dest* atomically (temp + fsync + os.replace)."""
    fd, tmp_name = tempfile.mkstemp(
        suffix=".part", prefix=".artifact-", dir=str(dest.parent)
    )
    try:
        with os.fdopen(fd, "wb") as out, open(source, "rb") as inp:
            shutil.copyfileobj(inp, out, length=_CHUNK_SIZE)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp_name, str(dest))
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
