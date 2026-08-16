"""Extraction-quality regression tests.

Entity names feed the knowledge graph, the interactive graph, the wiki, the
diff, and the query layer, so a naming defect degrades all of them at once.
These tests pin the two that did:

- feature bullets were kept whole and merely truncated, because the splitter
  did not know about the ASCII ``--`` separator;
- any number followed by an s-word was read as a duration, so "12 selectable
  formats" and "0 sections added" became metric entities.
"""

from __future__ import annotations

import pytest

from opendocs.core.knowledge_graph import EntityType
from opendocs.core.parser import ReadmeParser
from opendocs.core.semantic_extractor import _MAX_FEATURE_NAME, _METRIC_RE, SemanticExtractor, _feature_name


def extract(markdown: str):
    return SemanticExtractor().extract(ReadmeParser().parse(markdown, repo_name="Proj"))


def names_of(kg, entity_type: EntityType) -> list[str]:
    return [e.name for e in kg.entities if e.entity_type is entity_type]


# ---------------------------------------------------------------------------
# Feature names
# ---------------------------------------------------------------------------


class TestFeatureName:
    @pytest.mark.parametrize(
        ("bullet", "expected"),
        [
            ("15 Output Formats -- Word, PDF, PPTX, Blog Post and more", "15 Output Formats"),
            ("Interactive Graph — Single-file HTML visualization with search", "Interactive Graph"),
            ("Community Detection – Label propagation groups entities", "Community Detection"),
            ("Smart Table Sorting: 6 strategies for ordering rows", "Smart Table Sorting"),
            ("Knowledge Wiki - Wikipedia-style linked Markdown articles", "Knowledge Wiki"),
        ],
    )
    def test_separator_forms(self, bullet, expected):
        """Regression: only ':' and typographic dashes were handled, not '--'."""
        assert _feature_name(bullet) == expected

    def test_hyphenated_names_are_not_split(self):
        """A bare '-' must not cut 'Auto-PR' in half."""
        assert _feature_name("File Watcher + Auto-PR -- Monitor repos for changes") == "File Watcher + Auto-PR"

    def test_short_bullet_is_kept_whole(self):
        assert _feature_name("Fast and simple") == "Fast and simple"

    def test_prose_without_a_separator_is_trimmed_to_a_name(self):
        long_prose = "A feature described in a full sentence with no separator at all that rambles onward"
        result = _feature_name(long_prose)
        assert len(result) <= _MAX_FEATURE_NAME
        assert not result.endswith(" ")

    def test_trimming_falls_on_a_word_boundary(self):
        result = _feature_name("word " * 40)
        assert not result.endswith("wor")
        assert all(w == "word" for w in result.split())

    def test_stops_at_the_first_sentence(self):
        text = "Does one thing. Then it goes on to explain a great many other things at length."
        assert _feature_name(text) == "Does one thing"

    def test_never_exceeds_the_cap(self):
        for bullet in [
            "x" * 200,
            "Name -- " + "y" * 200,
            "word " * 100,
            "A: " + "z" * 200,
        ]:
            assert len(_feature_name(bullet)) <= _MAX_FEATURE_NAME


class TestFeatureExtraction:
    FEATURES_README = """# Proj

A project with several capabilities worth describing at some length here.

## Features

- **15 Output Formats** -- Word, PDF, PPTX, Blog Post, Jira Tickets, Changelog
- **Interactive Knowledge Graph** -- Single-file HTML visualization with search
- **File Watcher + Auto-PR** -- Monitor repos and create pull requests
"""

    def test_feature_names_are_concepts_not_sentences(self):
        kg = extract(self.FEATURES_README)
        features = names_of(kg, EntityType.FEATURE)
        assert "15 Output Formats" in features
        assert "Interactive Knowledge Graph" in features
        assert "File Watcher + Auto-PR" in features

    def test_no_entity_name_is_prose_length(self):
        kg = extract(self.FEATURES_README)
        oversized = [e.name for e in kg.entities if len(e.name) > _MAX_FEATURE_NAME]
        assert oversized == []

    def test_description_is_preserved_in_properties(self):
        """Trimming the name must not lose the detail."""
        kg = extract(self.FEATURES_README)
        feature = next(e for e in kg.entities if e.name == "15 Output Formats")
        assert "Word" in feature.properties.get("description", "")


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


class TestMetricRegex:
    @pytest.mark.parametrize(
        "text",
        [
            "all 12 selectable output formats",
            "5 stakeholder views",
            "0 sections added",
            "10 karat gold",
            "3 separate modules",
            "7 minor issues",
        ],
    )
    def test_number_followed_by_a_word_is_not_a_metric(self, text):
        """Regression: the bare 's'/'k'/'M' units matched any following word."""
        assert list(_METRIC_RE.finditer(text)) == []

    @pytest.mark.parametrize(
        ("text", "value", "unit"),
        [
            ("takes 30 seconds", "30", "seconds"),
            ("within 5 min", "5", "min"),
            ("latency of 250 ms", "250", "ms"),
            ("99.9% uptime", "99.9", "%"),
            ("handles 1000 req/s", "1000", "req/s"),
            ("cache is 512 MB", "512", "MB"),
            ("runs at 60 fps", "60", "fps"),
            ("a 10k user base", "10", "k"),
        ],
    )
    def test_real_metrics_still_match(self, text, value, unit):
        match = _METRIC_RE.search(text)
        assert match is not None, f"missed metric in {text!r}"
        assert (match.group(1), match.group(2)) == (value, unit)

    def test_longest_unit_wins(self):
        """'30 seconds' must not capture the unit as a bare 's'."""
        assert _METRIC_RE.search("takes 30 seconds").group(2) == "seconds"


class TestMetricExtraction:
    def test_counting_prose_produces_no_metrics(self):
        kg = extract("# Proj\n\nWe support 12 selectable formats and 5 stakeholder views in total here.\n")
        assert names_of(kg, EntityType.METRIC) == []

    def test_genuine_metrics_are_still_extracted(self):
        kg = extract("# Proj\n\nThe service responds in 250 ms and sustains 1000 req/s under load.\n")
        metrics = names_of(kg, EntityType.METRIC)
        assert any("250" in m for m in metrics)
        assert any("1000" in m for m in metrics)


# ---------------------------------------------------------------------------
# Dogfooding
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def kg():
    """The project's own README, extracted once for the whole module."""
    from pathlib import Path

    readme = Path(__file__).resolve().parent.parent / "README.md"
    return extract(readme.read_text(encoding="utf-8"))


class TestAgainstOwnReadme:
    def test_no_prose_length_entity_names(self, kg):
        oversized = [e.name for e in kg.entities if len(e.name) > _MAX_FEATURE_NAME]
        assert oversized == [], f"prose leaked into entity names: {oversized}"

    def test_no_bare_duration_metrics(self, kg):
        """The README counts things ("12 formats"); none of those are durations."""
        bogus = [e.name for e in kg.entities if e.entity_type is EntityType.METRIC and e.name.endswith(" s")]
        assert bogus == [], f"miscounted durations: {bogus}"

    def test_still_finds_real_entities(self, kg):
        """Precision must not have come at the cost of finding nothing."""
        assert len(kg.entities) > 15
        assert names_of(kg, EntityType.FEATURE)
