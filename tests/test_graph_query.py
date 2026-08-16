"""Tests for querying an exported graph.json.

Everything here is offline and deterministic: the point of the feature is that
a graph exported weeks ago stays answerable with no LLM and no network.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from opendocs.cli import main
from opendocs.core.graph_query import GraphQuery, GraphQueryError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

GRAPH = {
    "version": "1.0",
    "generator": "opendocs",
    "generated_at": "2026-01-01T00:00:00",
    "project": {"name": "Demo", "url": "", "description": ""},
    "stats": {"total_nodes": 5, "total_edges": 4},
    "nodes": [
        {"id": "api", "name": "API Gateway", "type": "component", "degree": 3, "community": 0},
        {"id": "svc", "name": "Order Service", "type": "component", "degree": 3, "community": 0},
        {"id": "pg", "name": "PostgreSQL", "type": "database", "degree": 2, "community": 1},
        {"id": "redis", "name": "Redis", "type": "database", "degree": 1, "community": 1},
        {
            "id": "s3",
            "name": "S3",
            "type": "cloud_service",
            "degree": 1,
            "community": 2,
            "provenance": "AMBIGUOUS",
            "confidence": 0.4,
        },
    ],
    "edges": [
        {"source": "api", "target": "svc", "relation": "connects_to"},
        {"source": "svc", "target": "pg", "relation": "stores_in"},
        {"source": "svc", "target": "redis", "relation": "uses"},
        {"source": "api", "target": "s3", "relation": "uses"},
    ],
    "communities": [
        {"id": 0, "size": 2, "members": ["API Gateway", "Order Service"], "dominant_type": "Component"},
        {"id": 1, "size": 2, "members": ["PostgreSQL", "Redis"], "dominant_type": "Database"},
    ],
    "god_nodes": [{"name": "API Gateway", "type": "component", "degree": 3}],
    "surprising_connections": [{"source": "API Gateway", "target": "S3", "relation": "uses", "score": 0.9}],
    "suggested_questions": ["What role does API Gateway play?"],
}


@pytest.fixture
def graph_file(tmp_path):
    path = tmp_path / "demo_graph.json"
    path.write_text(json.dumps(GRAPH), encoding="utf-8")
    return path


@pytest.fixture
def q(graph_file) -> GraphQuery:
    return GraphQuery.load(graph_file)


@pytest.fixture
def runner():
    return CliRunner()


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


class TestLoading:
    def test_loads_metadata(self, q):
        assert q.project_name == "Demo"
        assert len(q.nodes) == 5
        assert len(q.edges) == 4

    def test_missing_file(self, tmp_path):
        with pytest.raises(GraphQueryError, match="Could not read"):
            GraphQuery.load(tmp_path / "absent.json")

    def test_invalid_json(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not json", encoding="utf-8")
        with pytest.raises(GraphQueryError, match="not valid JSON"):
            GraphQuery.load(p)

    def test_wrong_shape(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
        with pytest.raises(GraphQueryError, match="no 'nodes' array"):
            GraphQuery.load(p)

    def test_tolerates_missing_optional_sections(self, tmp_path):
        p = tmp_path / "min.json"
        p.write_text(json.dumps({"nodes": [{"id": "a", "name": "A", "type": "project"}]}), encoding="utf-8")
        loaded = GraphQuery.load(p)
        assert len(loaded.nodes) == 1
        assert loaded.edges == []
        assert loaded.communities() == []


# ---------------------------------------------------------------------------
# Lookup and search
# ---------------------------------------------------------------------------


class TestLookup:
    def test_get_by_id(self, q):
        assert q.get("pg").name == "PostgreSQL"

    def test_get_by_name_is_case_insensitive(self, q):
        assert q.get("postgresql").id == "pg"

    def test_search_ranks_exact_match_first(self, q):
        assert q.search("redis")[0].name == "Redis"

    def test_search_matches_substrings(self, q):
        names = {n.name for n in q.search("service")}
        assert "Order Service" in names

    def test_search_empty_term(self, q):
        assert q.search("   ") == []

    def test_resolve_falls_back_to_a_unique_search_hit(self, q):
        assert q.resolve("gateway").name == "API Gateway"

    def test_resolve_reports_unknown_terms(self, q):
        with pytest.raises(GraphQueryError, match="No entity matching"):
            q.resolve("nonexistent")

    def test_resolve_reports_ambiguity(self, q):
        # "e" matches several names; the error should list candidates.
        with pytest.raises(GraphQueryError, match="ambiguous"):
            q.resolve("e")

    def test_of_type(self, q):
        assert {n.name for n in q.of_type("database")} == {"PostgreSQL", "Redis"}

    def test_of_type_normalises_spaces(self, q):
        assert [n.name for n in q.of_type("cloud service")] == ["S3"]

    def test_by_provenance(self, q):
        assert [n.name for n in q.by_provenance("ambiguous")] == ["S3"]


# ---------------------------------------------------------------------------
# Relations
# ---------------------------------------------------------------------------


class TestRelations:
    def test_dependencies_are_outgoing(self, q):
        assert {n.name for n in q.dependencies_of("Order Service")} == {"PostgreSQL", "Redis"}

    def test_dependents_are_incoming(self, q):
        assert [n.name for n in q.dependents_of("PostgreSQL")] == ["Order Service"]

    def test_neighbors_span_both_directions(self, q):
        assert {n.name for n in q.neighbors("Order Service")} == {"API Gateway", "PostgreSQL", "Redis"}

    def test_leaf_has_no_dependencies(self, q):
        assert q.dependencies_of("Redis") == []


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


class TestPaths:
    def test_direct_edge(self, q):
        hops = q.path("API Gateway", "Order Service")
        assert len(hops) == 1
        assert hops[0].edge.relation == "connects_to"

    def test_multi_hop(self, q):
        hops = q.path("API Gateway", "PostgreSQL")
        assert [h.to.name for h in hops] == ["Order Service", "PostgreSQL"]

    def test_walks_edges_backwards_when_needed(self, q):
        """Redis -> API Gateway requires traversing two edges against their direction."""
        hops = q.path("Redis", "API Gateway")
        assert len(hops) == 2
        assert any(h.reversed_ for h in hops)

    def test_same_node(self, q):
        assert q.path("Redis", "Redis") == []

    def test_no_path(self, tmp_path):
        payload = {
            "nodes": [
                {"id": "a", "name": "A", "type": "project"},
                {"id": "b", "name": "B", "type": "project"},
            ],
            "edges": [],
        }
        p = tmp_path / "g.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        assert GraphQuery.load(p).path("A", "B") == []


# ---------------------------------------------------------------------------
# Natural-language routing
# ---------------------------------------------------------------------------


class TestAnswer:
    def test_depends_on_maps_to_dependents(self, q):
        answer = q.answer("what depends on PostgreSQL?")
        assert answer.kind == "dependents"
        assert [n.name for n in answer.nodes] == ["Order Service"]

    def test_connection_question_maps_to_path(self, q):
        answer = q.answer("how are API Gateway and PostgreSQL connected?")
        assert answer.kind == "path"
        assert len(answer.hops) == 2

    def test_importance_question_maps_to_god_nodes(self, q):
        answer = q.answer("what are the most important components?")
        assert answer.kind == "god_nodes"
        assert answer.nodes[0].name == "API Gateway"

    def test_type_question(self, q):
        answer = q.answer("which databases are used?")
        assert answer.kind == "by_type"
        assert {n.name for n in answer.nodes} == {"PostgreSQL", "Redis"}

    def test_unknown_question_falls_back_to_search(self, q):
        answer = q.answer("tell me about Redis")
        assert answer.kind in {"search", "dependents"}
        assert any(n.name == "Redis" for n in answer.nodes)

    def test_nonsense_question_is_handled(self, q):
        answer = q.answer("zzz")
        assert answer.kind == "empty"
        assert answer.nodes == []

    def test_answer_serialises(self, q):
        payload = q.answer("what depends on PostgreSQL?").to_dict()
        assert payload["kind"] == "dependents"
        assert payload["nodes"][0]["name"] == "Order Service"
        json.dumps(payload)  # must be JSON-serialisable


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestQueryCommand:
    def test_stats(self, runner, graph_file):
        r = runner.invoke(main, ["query", str(graph_file), "--stats"])
        assert r.exit_code == 0, r.output
        assert "Demo" in r.output

    def test_overview_with_no_options(self, runner, graph_file):
        r = runner.invoke(main, ["query", str(graph_file)])
        assert r.exit_code == 0, r.output
        assert "API Gateway" in r.output

    def test_search(self, runner, graph_file):
        r = runner.invoke(main, ["query", str(graph_file), "--search", "redis"])
        assert r.exit_code == 0
        assert "Redis" in r.output

    def test_dependents(self, runner, graph_file):
        r = runner.invoke(main, ["query", str(graph_file), "--dependents", "PostgreSQL"])
        assert r.exit_code == 0
        assert "Order Service" in r.output

    def test_path(self, runner, graph_file):
        r = runner.invoke(main, ["query", str(graph_file), "--path", "API Gateway", "PostgreSQL"])
        assert r.exit_code == 0
        assert "Order Service" in r.output

    def test_type_listing(self, runner, graph_file):
        r = runner.invoke(main, ["query", str(graph_file), "--type", "database"])
        assert r.exit_code == 0
        assert "PostgreSQL" in r.output

    def test_natural_language(self, runner, graph_file):
        r = runner.invoke(main, ["query", str(graph_file), "what depends on PostgreSQL?"])
        assert r.exit_code == 0
        assert "Order Service" in r.output

    def test_json_output_is_parseable(self, runner, graph_file):
        r = runner.invoke(main, ["query", str(graph_file), "--type", "database", "--json"])
        assert r.exit_code == 0
        payload = json.loads(r.output)
        assert {n["name"] for n in payload["results"]} == {"PostgreSQL", "Redis"}

    def test_unknown_entity_exits_1(self, runner, graph_file):
        r = runner.invoke(main, ["query", str(graph_file), "--entity", "Nope"])
        assert r.exit_code == 1
        assert "No entity matching" in r.output

    def test_malformed_graph_exits_2(self, runner, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"nope": True}), encoding="utf-8")
        r = runner.invoke(main, ["query", str(bad), "--stats"])
        assert r.exit_code == 2

    def test_limit_is_respected(self, runner, graph_file):
        r = runner.invoke(main, ["query", str(graph_file), "--type", "database", "--json", "--limit", "1"])
        assert len(json.loads(r.output)["results"]) == 1


# ---------------------------------------------------------------------------
# Round trip against a real generated graph
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_queries_a_graph_the_pipeline_produced(self, tmp_path, monkeypatch):
        """The export format and the query layer must stay in agreement."""
        monkeypatch.setenv("OPENDOCS_MERMAID_BACKEND", "none")
        from opendocs.core.parser import ReadmeParser
        from opendocs.core.semantic_extractor import SemanticExtractor
        from opendocs.generators.graph_export import generate_graph_json

        readme = "# Sample\n\nBuilt with Python and Docker.\n\n## Storage\n\nUses PostgreSQL for persistence.\n"
        doc = ReadmeParser().parse(readme, repo_name="Sample")
        kg = SemanticExtractor().extract(doc)
        result = generate_graph_json(doc, kg, tmp_path)
        assert result.success, result.error

        loaded = GraphQuery.load(result.output_path)
        assert loaded.project_name == "Sample"
        assert loaded.nodes
        # Every edge endpoint must resolve to a real node.
        ids = {n.id for n in loaded.nodes}
        for edge in loaded.edges:
            assert edge.source in ids and edge.target in ids
