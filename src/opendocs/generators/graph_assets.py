"""Resolution of the vis-network bundle used by the interactive graph.

By default the generated page loads vis-network from a CDN, which keeps the
HTML small but means it will not render on a machine without network access.
``resolve_vis_network`` instead returns the library source so it can be
inlined, producing a page that is genuinely self-contained.

The source is looked up in three places, cheapest first:

1. ``OPENDOCS_VIS_NETWORK_JS`` — a path to a local copy.  Lets air-gapped
   builds work with no download at all.
2. The on-disk cache (``~/.cache/opendocs`` by default).
3. The CDN, which is then written to the cache for subsequent runs.

Only step 3 touches the network, and only once per machine.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

#: Pinned so a cached copy stays valid and the output is reproducible.
VIS_NETWORK_VERSION = "9.1.9"
VIS_NETWORK_URL = f"https://unpkg.com/vis-network@{VIS_NETWORK_VERSION}/standalone/umd/vis-network.min.js"

_ENV_LOCAL_PATH = "OPENDOCS_VIS_NETWORK_JS"
_ENV_CACHE_DIR = "OPENDOCS_CACHE_DIR"

_DOWNLOAD_TIMEOUT = 30.0
#: The real bundle is ~700 KB; anything far below that is a truncated download
#: or an error page rather than the library.
_MIN_PLAUSIBLE_SIZE = 100_000


def cache_dir() -> Path:
    """Return the directory used to cache downloaded front-end assets."""
    override = os.environ.get(_ENV_CACHE_DIR)
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "opendocs"


def _cached_bundle() -> Path:
    return cache_dir() / f"vis-network-{VIS_NETWORK_VERSION}.min.js"


def escape_for_inline_script(source: str) -> str:
    """Make JavaScript safe to place inside an inline ``<script>`` element.

    An HTML parser ends the element at the first ``</script`` regardless of
    JavaScript quoting, so that sequence must not appear literally.  Inside a
    string or regex literal ``<\\/script`` is equivalent, and it cannot occur
    anywhere else in valid JavaScript.
    """
    return source.replace("</script", "<\\/script")


def resolve_vis_network(*, allow_download: bool = True) -> str | None:
    """Return the vis-network source, or *None* if it cannot be obtained.

    Parameters
    ----------
    allow_download
        When *False*, only the environment override and the local cache are
        consulted — no network request is made.
    """
    # 1. Explicit local copy.
    local = os.environ.get(_ENV_LOCAL_PATH)
    if local:
        path = Path(local).expanduser()
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not read %s=%s: %s", _ENV_LOCAL_PATH, local, exc)
        else:
            logger.debug("Using vis-network from %s", path)
            return source

    # 2. Cache.
    cached = _cached_bundle()
    if cached.exists() and cached.stat().st_size >= _MIN_PLAUSIBLE_SIZE:
        try:
            return cached.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not read cached vis-network (%s); re-fetching", exc)

    if not allow_download:
        return None

    # 3. Download, then cache for next time.
    try:
        logger.info("Downloading vis-network %s for offline embedding", VIS_NETWORK_VERSION)
        with httpx.Client(timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
            response = client.get(VIS_NETWORK_URL)
            response.raise_for_status()
            source = response.text
    except Exception as exc:
        logger.warning("Could not download vis-network: %s", exc)
        return None

    if len(source) < _MIN_PLAUSIBLE_SIZE:
        logger.warning(
            "Downloaded vis-network looks truncated (%d bytes); falling back to the CDN",
            len(source),
        )
        return None

    try:
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(source, encoding="utf-8")
        logger.debug("Cached vis-network at %s", cached)
    except OSError as exc:
        # A read-only cache is not fatal — we still have the source in memory.
        logger.debug("Could not write vis-network cache: %s", exc)

    return source
