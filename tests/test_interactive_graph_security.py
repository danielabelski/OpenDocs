"""Injection regression tests for the interactive HTML knowledge graph.

Entity names are not trusted input: mermaid node labels are lifted verbatim
out of README content, and in LLM mode names come straight from model output.
Pointing OpenDocs at an untrusted repository must not produce an HTML file
that executes attacker-controlled markup when opened.
"""

from __future__ import annotations

import pytest

from opendocs.core.knowledge_graph import (
    Entity,
    EntityType,
    KnowledgeGraph,
    Relation,
    RelationType,
)
from opendocs.core.models import DocumentMetadata, DocumentModel
from opendocs.core.parser import ReadmeParser
from opendocs.core.semantic_extractor import SemanticExtractor
from opendocs.generators.interactive_graph import _script_json, generate_interactive_graph

SCRIPT_BREAKOUT = "</script><script>alert(document.domain)</script>"
IMG_PAYLOAD = "<img src=x onerror=alert(1)>"


# ---------------------------------------------------------------------------
# The serialisation helper
# ---------------------------------------------------------------------------


class TestScriptJson:
    def test_closing_tag_is_escaped(self):
        assert "</" not in _script_json({"name": SCRIPT_BREAKOUT})

    def test_html_comment_open_is_escaped(self):
        assert "<!--" not in _script_json({"name": "<!--hide"})

    @pytest.mark.parametrize("sep", [" ", " "])
    def test_line_separators_are_escaped(self, sep):
        """U+2028/9 are newlines to a JS parser but legal inside a JSON string."""
        assert sep not in _script_json({"name": f"a{sep}b"})

    def test_round_trips_as_json(self):
        import json

        payload = _script_json({"name": SCRIPT_BREAKOUT})
        # The escaping must stay valid JSON once the JS engine parses it back.
        assert json.loads(payload.replace("<\\/", "</"))["name"] == SCRIPT_BREAKOUT


# ---------------------------------------------------------------------------
# The generated page
# ---------------------------------------------------------------------------


def _hostile_kg() -> KnowledgeGraph:
    kg = KnowledgeGraph()
    kg.add_entity(Entity(id="a", name=SCRIPT_BREAKOUT, entity_type=EntityType.PROJECT, source_section="S"))
    kg.add_entity(Entity(id="b", name=IMG_PAYLOAD, entity_type=EntityType.TECHNOLOGY, source_section="S"))
    kg.add_relation(Relation(source_id="a", target_id="b", relation_type=RelationType.USES))
    return kg


class TestGeneratedPage:
    @pytest.fixture
    def page(self, tmp_path):
        doc = DocumentModel(metadata=DocumentMetadata(repo_name="poc"))
        result = generate_interactive_graph(doc, _hostile_kg(), tmp_path)
        assert result.success, result.error
        return result.output_path.read_text(encoding="utf-8")

    def test_script_element_is_not_terminated_early(self, page):
        assert SCRIPT_BREAKOUT not in page

    def test_script_tag_count_is_balanced(self, page):
        """Exactly two script elements: the vis-network CDN tag and the app tag."""
        assert page.count("</script>") == 2

    def test_payload_survives_only_as_inert_json_data(self, page):
        # The name is still present (we escape, not drop) but in escaped form.
        assert "<\\/script>" in page

    def test_innerhtml_sinks_are_escaped(self, page):
        """Every panel that writes untrusted names must route through esc()."""
        for sink in ["esc(g.name)", "esc(s.source)", "esc(s.target)", "esc(q)"]:
            assert sink in page, f"unescaped innerHTML sink: {sink}"
        assert "c.members.slice(0,3).map(esc)" in page


# ---------------------------------------------------------------------------
# End-to-end: the payload reaches the graph through ordinary basic-mode parsing
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_mermaid_labels_cannot_break_out(self, tmp_path):
        """Mermaid node labels become entity names verbatim, with no LLM involved."""
        readme = f"# Demo\n\n```mermaid\ngraph LR\n    A[{SCRIPT_BREAKOUT}] --> B[{IMG_PAYLOAD}]\n```\n"
        doc = ReadmeParser().parse(readme, repo_name="demo")
        kg = SemanticExtractor().extract(doc)

        # Guard the premise: the payload really does become an entity name.
        assert any(SCRIPT_BREAKOUT in e.name for e in kg.entities)

        result = generate_interactive_graph(doc, kg, tmp_path)
        assert result.success, result.error
        page = result.output_path.read_text(encoding="utf-8")

        assert SCRIPT_BREAKOUT not in page
        assert page.count("</script>") == 2
