"""Tests for the incremental build cache.

The cache is only safe if its fingerprint covers every input that affects an
artifact. Most of these tests are therefore about *misses*: proving that a
changed input produces a different key, so a stale artifact is never served.
"""

from __future__ import annotations

import hashlib

import pytest

from opendocs.core.build_cache import (
    BuildCache,
    default_cache_dir,
    document_fingerprint,
    fingerprint,
    graph_fingerprint,
)
from opendocs.core.knowledge_graph import Entity, EntityType, KnowledgeGraph
from opendocs.core.parser import ReadmeParser
from opendocs.core.template_vars import TemplateVars

README = """# Project

A project that does something useful for the people who use it every day.

## Installation

```bash
pip install project
```

## License

MIT.
"""


def parse(md: str = README):
    return ReadmeParser().parse(md, repo_name="Project")


def kg_with(*names: str) -> KnowledgeGraph:
    kg = KnowledgeGraph()
    for i, name in enumerate(names):
        kg.add_entity(Entity(id=f"e{i}", name=name, entity_type=EntityType.TECHNOLOGY))
    return kg


def key(**overrides):
    base = {
        "doc": parse(),
        "kg": kg_with("Python"),
        "output_format": "word",
        "theme_name": "corporate",
        "template_vars": None,
        "extra": None,
    }
    base.update(overrides)
    return fingerprint(**base)


# ---------------------------------------------------------------------------
# Fingerprint stability
# ---------------------------------------------------------------------------


class TestFingerprintStability:
    def test_same_inputs_same_key(self):
        assert key() == key()

    def test_document_fingerprint_ignores_generation_timestamp(self):
        """Regression: generated_at changed per parse, so the cache never hit."""
        a, b = parse(), parse()
        # Force the timestamps apart so the test does not depend on clock
        # resolution making two parses tie.
        a.metadata.generated_at = "2020-01-01T00:00:00+00:00"
        b.metadata.generated_at = "2026-12-31T23:59:59+00:00"
        assert document_fingerprint(a) == document_fingerprint(b)

    def test_document_fingerprint_survives_an_explicit_timestamp_change(self):
        doc = parse()
        before = document_fingerprint(doc)
        doc.metadata.generated_at = "1999-01-01T00:00:00+00:00"
        assert document_fingerprint(doc) == before

    def test_graph_fingerprint_is_stable(self):
        assert graph_fingerprint(kg_with("Python")) == graph_fingerprint(kg_with("Python"))

    def test_graph_fingerprint_handles_none(self):
        assert graph_fingerprint(None) == "no-kg"


# ---------------------------------------------------------------------------
# Fingerprint sensitivity — every one of these must miss
# ---------------------------------------------------------------------------


class TestFingerprintSensitivity:
    def test_document_content(self):
        assert key() != key(doc=parse(README + "\n## Extra\n\nMore words.\n"))

    def test_knowledge_graph(self):
        assert key() != key(kg=kg_with("Python", "Rust"))

    def test_output_format(self):
        assert key() != key(output_format="pdf")

    def test_theme(self):
        assert key() != key(theme_name="aurora")

    def test_template_vars(self):
        assert key() != key(template_vars=TemplateVars(project_name="Renamed"))

    def test_template_var_field_change(self):
        a = key(template_vars=TemplateVars(project_name="A", author="X"))
        b = key(template_vars=TemplateVars(project_name="A", author="Y"))
        assert a != b

    def test_extra_options(self):
        assert key(extra={"sort_tables": "smart"}) != key(extra={"sort_tables": "alpha"})

    def test_opendocs_version_is_part_of_the_key(self, monkeypatch):
        """A code change must invalidate previously cached artifacts."""
        import opendocs.core.build_cache as bc

        before = key()
        monkeypatch.setattr(bc, "_opendocs_version", lambda: "99.99.99")
        assert key() != before


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


class TestStorage:
    @pytest.fixture
    def cache(self, tmp_path):
        return BuildCache(tmp_path / "cache")

    @pytest.fixture
    def artifact(self, tmp_path):
        p = tmp_path / "src" / "report.docx"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"generated content")
        return p

    def test_miss_on_empty_cache(self, cache, tmp_path):
        assert cache.restore_into("nosuchkey", tmp_path / "out") is None
        assert cache.stats.misses == 1

    def test_round_trip(self, cache, artifact, tmp_path):
        assert cache.put("k1", artifact)
        out = tmp_path / "out"
        restored = cache.restore_into("k1", out)
        assert restored is not None
        assert restored.name == "report.docx"
        assert restored.read_bytes() == b"generated content"
        assert cache.stats.hits == 1

    def test_restored_bytes_are_identical(self, cache, artifact, tmp_path):
        cache.put("k1", artifact)
        restored = cache.restore_into("k1", tmp_path / "out")
        assert hashlib.sha256(restored.read_bytes()).digest() == hashlib.sha256(artifact.read_bytes()).digest()

    def test_directory_artifacts(self, cache, tmp_path):
        """The architecture generator emits a directory, not a single file."""
        src = tmp_path / "architecture"
        src.mkdir()
        (src / "a.mmd").write_text("graph LR", encoding="utf-8")
        (src / "nested").mkdir()
        (src / "nested" / "b.png").write_bytes(b"img")

        assert cache.put("dirkey", src)
        out = tmp_path / "out"
        restored = cache.restore_into("dirkey", out)
        assert restored is not None
        assert (restored / "a.mmd").read_text(encoding="utf-8") == "graph LR"
        assert (restored / "nested" / "b.png").read_bytes() == b"img"

    def test_overwrites_existing_destination(self, cache, artifact, tmp_path):
        cache.put("k1", artifact)
        out = tmp_path / "out"
        out.mkdir()
        (out / "report.docx").write_bytes(b"stale")
        restored = cache.restore_into("k1", out)
        assert restored.read_bytes() == b"generated content"

    def test_put_ignores_missing_artifact(self, cache, tmp_path):
        assert cache.put("k", tmp_path / "absent.docx") is False

    def test_disabled_cache_is_inert(self, tmp_path, artifact):
        cache = BuildCache(tmp_path / "cache", enabled=False)
        assert cache.put("k", artifact) is False
        assert cache.restore_into("k", tmp_path / "out") is None
        assert cache.stats.hits == 0 and cache.stats.misses == 0

    def test_damaged_entry_degrades_to_a_miss(self, cache, artifact, tmp_path):
        """A corrupt cache must never break a build."""
        cache.put("k1", artifact)
        (cache._entry("k1") / "report.docx").unlink()
        assert cache.restore_into("k1", tmp_path / "out") is None

    def test_corrupt_manifest_degrades_to_a_miss(self, cache, artifact, tmp_path):
        cache.put("k1", artifact)
        cache._manifest("k1").write_text("{not json", encoding="utf-8")
        assert cache.restore_into("k1", tmp_path / "out") is None

    def test_clear(self, cache, artifact):
        cache.put("k1", artifact)
        cache.put("k2", artifact)
        assert cache.clear() == 2
        assert cache.size_bytes() == 0

    def test_size_bytes(self, cache, artifact):
        assert cache.size_bytes() == 0
        cache.put("k1", artifact)
        assert cache.size_bytes() > 0

    def test_stats_summary(self, cache, artifact, tmp_path):
        assert cache.stats.summary() == "cache not used"
        cache.put("k1", artifact)
        cache.restore_into("k1", tmp_path / "o1")
        cache.restore_into("missing", tmp_path / "o2")
        assert cache.stats.summary() == "1/2 cached"


class TestCacheLocation:
    def test_env_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENDOCS_CACHE_DIR", str(tmp_path / "custom"))
        assert default_cache_dir() == tmp_path / "custom" / "build"

    def test_xdg_fallback(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OPENDOCS_CACHE_DIR", raising=False)
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
        assert default_cache_dir() == tmp_path / "xdg" / "opendocs" / "build"


# ---------------------------------------------------------------------------
# End to end through the pipeline
# ---------------------------------------------------------------------------


class TestPipelineIntegration:
    @pytest.fixture(autouse=True)
    def offline(self, monkeypatch):
        monkeypatch.setenv("OPENDOCS_MERMAID_BACKEND", "none")

    @pytest.fixture
    def readme(self, tmp_path):
        p = tmp_path / "README.md"
        p.write_text(README, encoding="utf-8")
        return p

    def _run(self, readme, out, cache):
        from opendocs.core.models import OutputFormat
        from opendocs.pipeline import Pipeline

        return Pipeline().run(
            str(readme),
            local=True,
            formats=[OutputFormat.BLOG],
            output_dir=str(out),
            cache=cache,
        )

    def test_second_run_hits_the_cache(self, readme, tmp_path):
        cache = BuildCache(tmp_path / "cache")
        self._run(readme, tmp_path / "o1", cache)
        assert cache.stats.stores >= 1

        cache2 = BuildCache(tmp_path / "cache")
        self._run(readme, tmp_path / "o2", cache2)
        assert cache2.stats.hits >= 1

    def test_cached_output_is_byte_identical(self, readme, tmp_path):
        cache = BuildCache(tmp_path / "cache")
        first = self._run(readme, tmp_path / "o1", cache)
        fresh_bytes = first.blog_path.read_bytes()

        cache2 = BuildCache(tmp_path / "cache")
        second = self._run(readme, tmp_path / "o2", cache2)
        assert cache2.stats.hits >= 1
        assert second.blog_path.read_bytes() == fresh_bytes

    def test_changed_content_is_not_served_from_cache(self, readme, tmp_path):
        """The safety property: a changed source must not reuse an old artifact."""
        cache = BuildCache(tmp_path / "cache")
        self._run(readme, tmp_path / "o1", cache)

        readme.write_text(README + "\n## Monitoring\n\nWe ship metrics to Prometheus.\n", encoding="utf-8")
        cache2 = BuildCache(tmp_path / "cache")
        second = self._run(readme, tmp_path / "o2", cache2)

        assert cache2.stats.hits == 0
        assert "Monitoring" in second.blog_path.read_text(encoding="utf-8")

    def test_disabled_cache_still_generates(self, readme, tmp_path):
        cache = BuildCache(tmp_path / "cache", enabled=False)
        result = self._run(readme, tmp_path / "o1", cache)
        assert result.blog_path is not None and result.blog_path.exists()

    def test_no_cache_argument_is_optional(self, readme, tmp_path):
        """Passing no cache at all must keep working."""
        from opendocs.core.models import OutputFormat
        from opendocs.pipeline import Pipeline

        result = Pipeline().run(str(readme), local=True, formats=[OutputFormat.BLOG], output_dir=str(tmp_path / "o"))
        assert result.blog_path is not None and result.blog_path.exists()
