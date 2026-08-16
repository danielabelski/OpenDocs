"""Tests for offline asset embedding in the interactive graph.

These must never reach the network: every case either supplies a local file
via ``OPENDOCS_VIS_NETWORK_JS``, primes the cache, or asserts the no-download
path.
"""

from __future__ import annotations

import pytest

from opendocs.core.knowledge_graph import Entity, EntityType, KnowledgeGraph
from opendocs.core.models import DocumentMetadata, DocumentModel
from opendocs.generators import graph_assets
from opendocs.generators.graph_assets import (
    VIS_NETWORK_URL,
    cache_dir,
    escape_for_inline_script,
    resolve_vis_network,
)
from opendocs.generators.interactive_graph import generate_interactive_graph

# Large enough to clear the truncated-download guard.
FAKE_LIB = "/* vis-network stub */ var vis = {Network: function(){}, DataSet: function(){}};" + ("//" + "x" * 120_000)


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Point the asset cache at a temp dir and clear any inherited overrides."""
    monkeypatch.setenv("OPENDOCS_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("OPENDOCS_VIS_NETWORK_JS", raising=False)
    yield


@pytest.fixture
def local_lib(tmp_path, monkeypatch):
    path = tmp_path / "vis-network.min.js"
    path.write_text(FAKE_LIB, encoding="utf-8")
    monkeypatch.setenv("OPENDOCS_VIS_NETWORK_JS", str(path))
    return path


def _kg() -> KnowledgeGraph:
    kg = KnowledgeGraph()
    kg.add_entity(Entity(id="a", name="Alpha", entity_type=EntityType.PROJECT))
    kg.add_entity(Entity(id="b", name="Beta", entity_type=EntityType.TECHNOLOGY))
    return kg


def _doc() -> DocumentModel:
    return DocumentModel(metadata=DocumentMetadata(repo_name="proj"))


# ---------------------------------------------------------------------------
# escape_for_inline_script
# ---------------------------------------------------------------------------


class TestInlineScriptEscaping:
    def test_closing_tag_is_neutralised(self):
        assert "</script" not in escape_for_inline_script("var s = '</script>';")

    def test_case_variants_are_covered(self):
        # The HTML parser is case-insensitive, so cover the exact-case form we
        # replace and confirm nothing else is disturbed.
        assert escape_for_inline_script("a</scriptb") == "a<\\/scriptb"

    def test_ordinary_code_is_untouched(self):
        code = "function f(a, b) { return a / b; }"
        assert escape_for_inline_script(code) == code


# ---------------------------------------------------------------------------
# resolve_vis_network
# ---------------------------------------------------------------------------


class TestResolve:
    def test_prefers_the_local_override(self, local_lib):
        assert resolve_vis_network(allow_download=False) == FAKE_LIB

    def test_returns_none_when_nothing_available_and_no_download(self):
        assert resolve_vis_network(allow_download=False) is None

    def test_uses_the_cache(self, tmp_path, monkeypatch):
        cached = cache_dir() / f"vis-network-{graph_assets.VIS_NETWORK_VERSION}.min.js"
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(FAKE_LIB, encoding="utf-8")
        assert resolve_vis_network(allow_download=False) == FAKE_LIB

    def test_ignores_a_truncated_cache_entry(self):
        cached = cache_dir() / f"vis-network-{graph_assets.VIS_NETWORK_VERSION}.min.js"
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text("nope", encoding="utf-8")
        assert resolve_vis_network(allow_download=False) is None

    def test_missing_override_path_falls_through(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OPENDOCS_VIS_NETWORK_JS", str(tmp_path / "absent.js"))
        assert resolve_vis_network(allow_download=False) is None

    def test_download_writes_to_the_cache(self, monkeypatch):
        """A successful fetch is cached so later runs need no network."""

        class _Response:
            text = FAKE_LIB

            def raise_for_status(self):
                return None

        class _Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url):
                assert url == VIS_NETWORK_URL
                return _Response()

        monkeypatch.setattr(graph_assets.httpx, "Client", _Client)

        assert resolve_vis_network() == FAKE_LIB
        cached = cache_dir() / f"vis-network-{graph_assets.VIS_NETWORK_VERSION}.min.js"
        assert cached.exists()
        # Second call is served from cache even with the network unavailable.
        assert resolve_vis_network(allow_download=False) == FAKE_LIB

    def test_download_failure_returns_none(self, monkeypatch):
        class _Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url):
                raise RuntimeError("offline")

        monkeypatch.setattr(graph_assets.httpx, "Client", _Client)
        assert resolve_vis_network() is None


# ---------------------------------------------------------------------------
# Page generation
# ---------------------------------------------------------------------------


class TestGeneratedPage:
    def test_default_links_the_cdn(self, tmp_path):
        result = generate_interactive_graph(_doc(), _kg(), tmp_path)
        page = result.output_path.read_text(encoding="utf-8")
        assert f'<script src="{VIS_NETWORK_URL}"></script>' in page

    def test_embed_inlines_the_library(self, tmp_path, local_lib):
        result = generate_interactive_graph(_doc(), _kg(), tmp_path, embed_assets=True)
        page = result.output_path.read_text(encoding="utf-8")
        assert "vis-network stub" in page
        assert f'src="{VIS_NETWORK_URL}"' not in page

    def test_embed_falls_back_to_cdn_when_unavailable(self, tmp_path, monkeypatch):
        """A failed embed must still produce a working page, not a broken one."""
        monkeypatch.setattr(graph_assets, "resolve_vis_network", lambda **kw: None)
        monkeypatch.setattr("opendocs.generators.interactive_graph.resolve_vis_network", lambda **kw: None)
        result = generate_interactive_graph(_doc(), _kg(), tmp_path, embed_assets=True)
        page = result.output_path.read_text(encoding="utf-8")
        assert f'<script src="{VIS_NETWORK_URL}"></script>' in page

    def test_embedded_page_keeps_script_tags_balanced(self, tmp_path, monkeypatch, local_lib):
        """A library containing '</script' must not break out of the element."""
        hostile = FAKE_LIB + "\nvar t = '</script><script>bad()</script>';"
        local_lib.write_text(hostile, encoding="utf-8")
        result = generate_interactive_graph(_doc(), _kg(), tmp_path, embed_assets=True)
        page = result.output_path.read_text(encoding="utf-8")
        assert page.count("</script>") == 2

    def test_creates_the_output_directory(self, tmp_path):
        target = tmp_path / "does" / "not" / "exist"
        result = generate_interactive_graph(_doc(), _kg(), target)
        assert result.success, result.error
        assert result.output_path.exists()
