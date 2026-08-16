"""Content-addressed cache for generated documentation artifacts.

``opendocs watch`` re-runs the full pipeline whenever a watched file changes,
regenerating every format even when a given output would be byte-for-byte
identical.  This cache skips that work by fingerprinting everything an output
depends on and reusing the stored artifact when the fingerprint matches.

Correctness rests entirely on the fingerprint covering every input.  If an
input is missed, a stale artifact is served silently, which is worse than no
cache at all — so :func:`fingerprint` folds in the document content, the
knowledge graph, the theme, the template variables, the output format, and the
running opendocs version, and any unknown extra is included rather than
ignored.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Bumped when the cache layout itself changes, invalidating every entry.
CACHE_FORMAT_VERSION = 1

_ENV_CACHE_DIR = "OPENDOCS_CACHE_DIR"


def default_cache_dir() -> Path:
    """Return the directory used for cached build artifacts."""
    import os

    override = os.environ.get(_ENV_CACHE_DIR)
    if override:
        return Path(override).expanduser() / "build"
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "opendocs" / "build"


def _opendocs_version() -> str:
    """Version of the running package, so a code change invalidates the cache."""
    try:
        from .. import __version__  # type: ignore[attr-defined]

        return str(__version__)
    except Exception:
        pass
    try:
        from importlib.metadata import version

        return version("opendocs")
    except Exception:
        return "unknown"


def _stable(value: Any) -> Any:
    """Convert *value* into something JSON can serialise deterministically."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _stable(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_stable(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_stable(v) for v in value)
    if hasattr(value, "model_dump"):  # pydantic
        try:
            return _stable(value.model_dump())
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        return _stable(vars(value))
    return repr(value)


#: Metadata that changes on every parse without changing the content.
#: ``generated_at`` is a fresh timestamp each run, so including it would make
#: the fingerprint unique every time — the cache would never hit and would grow
#: a new entry per build.  The trade-off is that a cache hit keeps the
#: timestamp from when the artifact was first built, which is the usual and
#: desirable behaviour for a build cache: identical inputs, identical output.
_VOLATILE_METADATA = ("generated_at",)


def document_fingerprint(doc: Any) -> str:
    """Fingerprint a DocumentModel by its content, ignoring volatile metadata."""
    try:
        payload_obj = doc.model_dump(mode="json")
    except Exception:
        try:
            payload_obj = _stable(doc)
        except Exception:
            return hashlib.sha256(repr(doc).encode("utf-8")).hexdigest()

    metadata = payload_obj.get("metadata") if isinstance(payload_obj, dict) else None
    if isinstance(metadata, dict):
        for field_name in _VOLATILE_METADATA:
            metadata.pop(field_name, None)

    payload = json.dumps(payload_obj, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def graph_fingerprint(kg: Any) -> str:
    """Fingerprint a KnowledgeGraph.

    Included separately from the document because LLM mode enriches the graph
    with content the document alone does not determine.
    """
    if kg is None:
        return "no-kg"
    try:
        payload = kg.model_dump_json()
    except Exception:
        payload = json.dumps(_stable(kg), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def fingerprint(
    *,
    doc: Any,
    kg: Any,
    output_format: str,
    theme_name: str,
    template_vars: Any = None,
    extra: Optional[dict[str, Any]] = None,
) -> str:
    """Compute the cache key for one generated artifact."""
    key = {
        "cache_format": CACHE_FORMAT_VERSION,
        "opendocs": _opendocs_version(),
        "format": output_format,
        "theme": theme_name,
        "doc": document_fingerprint(doc),
        "kg": graph_fingerprint(kg),
        "template_vars": _stable(template_vars),
        "extra": _stable(extra or {}),
    }
    blob = json.dumps(key, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class CacheStats:
    """Tally of cache activity for one pipeline run."""

    hits: int = 0
    misses: int = 0
    stores: int = 0

    @property
    def total(self) -> int:
        return self.hits + self.misses

    def summary(self) -> str:
        if not self.total:
            return "cache not used"
        return f"{self.hits}/{self.total} cached"


class BuildCache:
    """Stores and retrieves generated artifacts by content fingerprint.

    Each entry is a directory named after the fingerprint containing the
    artifact plus a small manifest.  Artifacts may be single files or whole
    directories (the architecture generator produces a directory).
    """

    def __init__(self, cache_dir: str | Path | None = None, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.dir = Path(cache_dir) if cache_dir else default_cache_dir()
        self.stats = CacheStats()

    # -- Internal --------------------------------------------------------

    def _entry(self, key: str) -> Path:
        # Shard by the first two characters to avoid one enormous directory.
        return self.dir / key[:2] / key

    def _manifest(self, key: str) -> Path:
        return self._entry(key) / "manifest.json"

    # -- Public API ------------------------------------------------------

    def restore_into(self, key: str, output_dir: Path) -> Optional[Path]:
        """Restore a cached artifact into *output_dir*.

        The artifact's filename comes from the manifest, so callers do not need
        to know what a generator would have named its output — which is what
        makes this usable without touching every generator.

        Returns the restored path on a hit, or *None* on a miss.
        """
        if not self.enabled:
            return None

        manifest_path = self._manifest(key)
        if not manifest_path.exists():
            self.stats.misses += 1
            return None

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            name = manifest["name"]
            stored = self._entry(key) / name
            if not stored.exists():
                self.stats.misses += 1
                return None

            destination = Path(output_dir) / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if stored.is_dir():
                if destination.exists():
                    shutil.rmtree(destination)
                shutil.copytree(stored, destination)
            else:
                shutil.copy2(stored, destination)
        except Exception as exc:
            # A damaged entry must degrade to a miss, never break the build.
            logger.debug("Cache read failed for %s: %s", key, exc)
            self.stats.misses += 1
            return None

        self.stats.hits += 1
        logger.debug("Cache hit %s -> %s", key[:12], destination)
        return destination

    def put(self, key: str, artifact: Path) -> bool:
        """Store *artifact* under *key*. Returns True when it was stored."""
        if not self.enabled:
            return False

        artifact = Path(artifact)
        if not artifact.exists():
            return False

        entry = self._entry(key)
        try:
            if entry.exists():
                shutil.rmtree(entry)
            entry.mkdir(parents=True, exist_ok=True)

            target = entry / artifact.name
            if artifact.is_dir():
                shutil.copytree(artifact, target)
            else:
                shutil.copy2(artifact, target)

            self._manifest(key).write_text(
                json.dumps({"name": artifact.name, "is_dir": artifact.is_dir()}),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.debug("Cache write failed for %s: %s", key, exc)
            # Leave no half-written entry behind.
            shutil.rmtree(entry, ignore_errors=True)
            return False

        self.stats.stores += 1
        return True

    def clear(self) -> int:
        """Delete every cached entry. Returns how many were removed."""
        if not self.dir.exists():
            return 0
        removed = sum(1 for _ in self.dir.glob("*/*/manifest.json"))
        shutil.rmtree(self.dir, ignore_errors=True)
        return removed

    def size_bytes(self) -> int:
        """Total size of the cache on disk."""
        if not self.dir.exists():
            return 0
        return sum(p.stat().st_size for p in self.dir.rglob("*") if p.is_file())
