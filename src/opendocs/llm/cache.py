"""Response cache for LLM calls.

``--mode llm`` re-issues every request on every run, so regenerating docs from
an unchanged README pays the full API cost again. This caches responses on
disk, keyed by everything that determines the answer, so a repeat run is free
and instant.

Caching a non-deterministic API is a deliberate trade: with ``temperature > 0``
a provider would have returned *a* different answer, and this returns the
earlier one. That is what makes repeated builds reproducible and cheap, and it
is why the temperature is part of the key and why the cache is easy to turn
off — see :func:`llm_cache_enabled`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Bumped when the on-disk entry layout changes.
CACHE_FORMAT_VERSION = 1

_ENV_ENABLED = "OPENDOCS_LLM_CACHE"
_ENV_CACHE_DIR = "OPENDOCS_CACHE_DIR"

_FALSEY = {"0", "false", "no", "off"}


def llm_cache_enabled() -> bool:
    """Whether the LLM cache is active.

    On by default; set ``OPENDOCS_LLM_CACHE=0`` to disable it globally.
    """
    return os.environ.get(_ENV_ENABLED, "1").strip().lower() not in _FALSEY


def llm_cache_dir() -> Path:
    """Directory holding cached LLM responses."""
    override = os.environ.get(_ENV_CACHE_DIR)
    if override:
        return Path(override).expanduser() / "llm"
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "opendocs" / "llm"


@dataclass
class LLMCacheStats:
    """Tally of cache activity for a process."""

    hits: int = 0
    misses: int = 0
    stores: int = 0

    @property
    def total(self) -> int:
        return self.hits + self.misses

    def summary(self) -> str:
        if not self.total:
            return "no LLM calls"
        return f"{self.hits}/{self.total} LLM responses cached"


class LLMCache:
    """Disk cache mapping a request fingerprint to a provider response."""

    def __init__(self, cache_dir: str | Path | None = None, *, enabled: Optional[bool] = None) -> None:
        self.enabled = llm_cache_enabled() if enabled is None else enabled
        self.dir = Path(cache_dir) if cache_dir else llm_cache_dir()
        self.stats = LLMCacheStats()

    # -- Keying ----------------------------------------------------------

    @staticmethod
    def make_key(
        *,
        provider: str,
        model: str,
        system: str,
        user: str,
        kind: str = "text",
        temperature: float = 0.0,
        max_tokens: int = 0,
        extra: Optional[dict[str, Any]] = None,
    ) -> str:
        """Fingerprint one request.

        Everything that could change the response belongs here — including the
        temperature and token ceiling, so tuning either re-asks the provider
        rather than replaying an answer generated under different settings.
        """
        payload = {
            "v": CACHE_FORMAT_VERSION,
            "provider": provider,
            "model": model,
            "kind": kind,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "system": system,
            "user": user,
            "extra": extra or {},
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    # -- Storage ---------------------------------------------------------

    def _path(self, key: str) -> Path:
        # Shard by the first two characters to keep directories small.
        return self.dir / key[:2] / f"{key}.json"

    def get(self, key: str) -> Optional[str]:
        """Return the cached response text, or *None* on a miss."""
        if not self.enabled:
            return None

        path = self._path(key)
        if not path.exists():
            self.stats.misses += 1
            return None

        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            response = entry["response"]
        except Exception as exc:
            # A damaged entry must behave as a miss, never break a run.
            logger.debug("LLM cache read failed for %s: %s", key[:12], exc)
            self.stats.misses += 1
            return None

        self.stats.hits += 1
        logger.debug("LLM cache hit %s", key[:12])
        return response

    def put(self, key: str, response: str, *, meta: Optional[dict[str, Any]] = None) -> bool:
        """Store *response* under *key*. Returns True when it was written."""
        if not self.enabled:
            return False

        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write via a temp file so a crash cannot leave a half-written
            # entry that would later parse as valid but truncated JSON.
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps({"response": response, "cached_at": time.time(), "meta": meta or {}}),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception as exc:
            logger.debug("LLM cache write failed for %s: %s", key[:12], exc)
            return False

        self.stats.stores += 1
        return True

    def clear(self) -> int:
        """Delete every cached response. Returns how many were removed."""
        if not self.dir.exists():
            return 0
        entries = list(self.dir.glob("*/*.json"))
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)
        return len(entries)

    def size_bytes(self) -> int:
        if not self.dir.exists():
            return 0
        return sum(p.stat().st_size for p in self.dir.rglob("*.json") if p.is_file())

    def entry_count(self) -> int:
        if not self.dir.exists():
            return 0
        return len(list(self.dir.glob("*/*.json")))


#: Process-wide cache shared by every provider instance, so separate
#: LLMExtractor / LLMSummarizer / LLMContentEnhancer objects benefit from each
#: other's results within a single run as well as across runs.
_SHARED: Optional[LLMCache] = None


def shared_cache() -> LLMCache:
    """Return the process-wide LLM cache, creating it on first use."""
    global _SHARED
    if _SHARED is None:
        _SHARED = LLMCache()
    return _SHARED


def reset_shared_cache(cache: Optional[LLMCache] = None) -> None:
    """Replace the process-wide cache (used by tests and by ``--no-cache``)."""
    global _SHARED
    _SHARED = cache
