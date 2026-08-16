"""Query a previously exported ``graph.json`` without re-processing the source.

The graph export already carries everything needed to answer structural
questions about a project — entities, typed relations, communities, degrees
and provenance.  This module loads that file and answers those questions
offline, so a graph produced weeks ago stays useful with no LLM, no API key
and no network access.

    from opendocs.core.graph_query import GraphQuery

    q = GraphQuery.load("output/myproject_graph.json")
    q.dependents_of("Redis")       # what points at Redis
    q.path("API Gateway", "S3")    # how two concepts connect
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional


class GraphQueryError(RuntimeError):
    """Raised when a graph file cannot be loaded or a query cannot be answered."""


@dataclass(frozen=True)
class Node:
    """One entity from the exported graph."""

    id: str
    name: str
    type: str
    confidence: float = 1.0
    provenance: str = "EXTRACTED"
    extraction_method: str = "deterministic"
    community: int = -1
    degree: int = 0
    source_section: str = ""
    properties: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Node:
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            type=str(data.get("type", "")),
            confidence=float(data.get("confidence", 1.0)),
            provenance=str(data.get("provenance", "EXTRACTED")),
            extraction_method=str(data.get("extraction_method", "deterministic")),
            community=int(data.get("community", -1)),
            degree=int(data.get("degree", 0)),
            source_section=str(data.get("source_section", "")),
            properties=data.get("properties") or {},
        )


@dataclass(frozen=True)
class Edge:
    """One typed relation from the exported graph."""

    source: str
    target: str
    relation: str
    confidence: float = 1.0
    provenance: str = "EXTRACTED"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Edge:
        return cls(
            source=str(data.get("source", "")),
            target=str(data.get("target", "")),
            relation=str(data.get("relation", "")),
            confidence=float(data.get("confidence", 1.0)),
            provenance=str(data.get("provenance", "EXTRACTED")),
        )


@dataclass(frozen=True)
class Hop:
    """A single step along a path between two entities."""

    frm: Node
    edge: Edge
    to: Node
    #: True when the edge is stored target -> source and we walked it backwards.
    reversed_: bool = False


class GraphQuery:
    """Read-only queries over an exported ``graph.json``."""

    def __init__(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise GraphQueryError("Graph file must contain a JSON object")

        raw_nodes = payload.get("nodes")
        if not isinstance(raw_nodes, list):
            raise GraphQueryError("Graph file has no 'nodes' array — is this an opendocs graph.json?")

        self.payload = payload
        self.nodes: list[Node] = [Node.from_dict(n) for n in raw_nodes if isinstance(n, dict)]
        self.edges: list[Edge] = [Edge.from_dict(e) for e in payload.get("edges", []) if isinstance(e, dict)]

        self._by_id: dict[str, Node] = {n.id: n for n in self.nodes}
        # Names are not guaranteed unique; keep every match.
        self._by_name: dict[str, list[Node]] = {}
        for n in self.nodes:
            self._by_name.setdefault(n.name.lower(), []).append(n)

        self._out: dict[str, list[Edge]] = {}
        self._in: dict[str, list[Edge]] = {}
        for e in self.edges:
            self._out.setdefault(e.source, []).append(e)
            self._in.setdefault(e.target, []).append(e)

    # -- Loading ---------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> GraphQuery:
        """Load a graph from *path*."""
        p = Path(path)
        try:
            raw = p.read_text(encoding="utf-8")
        except OSError as exc:
            raise GraphQueryError(f"Could not read {p}: {exc}") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GraphQueryError(f"{p} is not valid JSON: {exc}") from exc
        return cls(payload)

    # -- Metadata --------------------------------------------------------

    @property
    def project_name(self) -> str:
        return str(self.payload.get("project", {}).get("name", "unknown"))

    @property
    def generated_at(self) -> str:
        return str(self.payload.get("generated_at", ""))

    @property
    def stats(self) -> dict[str, Any]:
        return dict(self.payload.get("stats", {}))

    def entity_types(self) -> dict[str, int]:
        """Return ``{type: count}``, most common first."""
        counts: dict[str, int] = {}
        for n in self.nodes:
            counts[n.type] = counts.get(n.type, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    def relation_types(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for e in self.edges:
            counts[e.relation] = counts.get(e.relation, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    # -- Lookup ----------------------------------------------------------

    def get(self, id_or_name: str) -> Optional[Node]:
        """Resolve an entity by exact id, then exact name (case-insensitive)."""
        if id_or_name in self._by_id:
            return self._by_id[id_or_name]
        matches = self._by_name.get(id_or_name.lower())
        return matches[0] if matches else None

    def resolve(self, term: str) -> Node:
        """Resolve *term* to a node, falling back to search.

        Raises ``GraphQueryError`` with near-miss suggestions when the term
        does not identify anything, which is far more useful at a CLI than an
        empty result.
        """
        node = self.get(term)
        if node is not None:
            return node

        candidates = self.search(term)
        if len(candidates) == 1:
            return candidates[0]
        if candidates:
            names = ", ".join(repr(c.name) for c in candidates[:8])
            raise GraphQueryError(f"{term!r} is ambiguous — did you mean one of: {names}?")
        raise GraphQueryError(f"No entity matching {term!r}. Try `--search` or `--list-types`.")

    def search(self, term: str, *, limit: int = 25) -> list[Node]:
        """Case-insensitive substring search over entity names.

        Exact matches sort first, then prefix matches, then the rest — each
        group by descending degree so hubs surface above leaves.
        """
        needle = term.strip().lower()
        if not needle:
            return []

        def rank(n: Node) -> tuple[int, int, str]:
            lowered = n.name.lower()
            if lowered == needle:
                tier = 0
            elif lowered.startswith(needle):
                tier = 1
            else:
                tier = 2
            return (tier, -n.degree, lowered)

        hits = [n for n in self.nodes if needle in n.name.lower()]
        return sorted(hits, key=rank)[:limit]

    def of_type(self, entity_type: str) -> list[Node]:
        """All entities of a given type, highest degree first."""
        wanted = entity_type.strip().lower().replace(" ", "_")
        found = [n for n in self.nodes if n.type.lower() == wanted]
        return sorted(found, key=lambda n: (-n.degree, n.name.lower()))

    # -- Relations -------------------------------------------------------

    def _node_or_none(self, node_id: str) -> Optional[Node]:
        return self._by_id.get(node_id)

    def outgoing(self, term: str) -> list[tuple[Edge, Node]]:
        """Edges leaving the entity, paired with the entity they point at."""
        node = self.resolve(term)
        pairs = []
        for e in self._out.get(node.id, []):
            target = self._node_or_none(e.target)
            if target is not None:
                pairs.append((e, target))
        return sorted(pairs, key=lambda p: (p[0].relation, p[1].name.lower()))

    def incoming(self, term: str) -> list[tuple[Edge, Node]]:
        """Edges arriving at the entity, paired with their origin."""
        node = self.resolve(term)
        pairs = []
        for e in self._in.get(node.id, []):
            source = self._node_or_none(e.source)
            if source is not None:
                pairs.append((e, source))
        return sorted(pairs, key=lambda p: (p[0].relation, p[1].name.lower()))

    def neighbors(self, term: str) -> list[Node]:
        """Every entity directly connected, in either direction."""
        seen: dict[str, Node] = {}
        for _edge, other in self.outgoing(term) + self.incoming(term):
            seen.setdefault(other.id, other)
        return sorted(seen.values(), key=lambda n: (-n.degree, n.name.lower()))

    def dependents_of(self, term: str) -> list[Node]:
        """Entities that point at this one — "what would break if it changed"."""
        return sorted(
            {s.id: s for _e, s in self.incoming(term)}.values(),
            key=lambda n: (-n.degree, n.name.lower()),
        )

    def dependencies_of(self, term: str) -> list[Node]:
        """Entities this one points at — "what it relies on"."""
        return sorted(
            {t.id: t for _e, t in self.outgoing(term)}.values(),
            key=lambda n: (-n.degree, n.name.lower()),
        )

    # -- Paths -----------------------------------------------------------

    def path(self, start: str, end: str, *, max_depth: int = 8) -> list[Hop]:
        """Shortest undirected path between two entities.

        Relations are treated as undirected here: the question "how are these
        two things connected?" rarely cares which way an edge was recorded.
        Returns an empty list when no path exists within *max_depth*.
        """
        src = self.resolve(start)
        dst = self.resolve(end)
        if src.id == dst.id:
            return []

        # BFS, recording how each node was reached.
        came_from: dict[str, tuple[str, Edge, bool]] = {}
        queue: deque[tuple[str, int]] = deque([(src.id, 0)])
        visited = {src.id}

        while queue:
            current, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for edge in self._out.get(current, []):
                if edge.target not in visited:
                    visited.add(edge.target)
                    came_from[edge.target] = (current, edge, False)
                    queue.append((edge.target, depth + 1))
            for edge in self._in.get(current, []):
                if edge.source not in visited:
                    visited.add(edge.source)
                    came_from[edge.source] = (current, edge, True)
                    queue.append((edge.source, depth + 1))
            if dst.id in visited:
                break

        if dst.id not in came_from:
            return []

        # Walk back to the start, then reverse.
        hops: list[Hop] = []
        cursor = dst.id
        while cursor != src.id:
            prev_id, edge, was_reversed = came_from[cursor]
            frm = self._by_id.get(prev_id)
            to = self._by_id.get(cursor)
            if frm is None or to is None:  # pragma: no cover - defensive
                break
            hops.append(Hop(frm=frm, edge=edge, to=to, reversed_=was_reversed))
            cursor = prev_id
        hops.reverse()
        return hops

    # -- Structural views ------------------------------------------------

    def god_nodes(self, top_n: int = 5) -> list[Node]:
        """Highest-degree entities, recomputed from the edges we actually hold."""
        return sorted(self.nodes, key=lambda n: (-n.degree, n.name.lower()))[:top_n]

    def communities(self) -> list[dict[str, Any]]:
        raw = self.payload.get("communities", [])
        return [c for c in raw if isinstance(c, dict)]

    def community_members(self, community_id: int) -> list[Node]:
        found = [n for n in self.nodes if n.community == community_id]
        return sorted(found, key=lambda n: (-n.degree, n.name.lower()))

    def surprising_connections(self) -> list[dict[str, Any]]:
        raw = self.payload.get("surprising_connections", [])
        return [s for s in raw if isinstance(s, dict)]

    def suggested_questions(self) -> list[str]:
        raw = self.payload.get("suggested_questions", [])
        return [str(q) for q in raw]

    def by_provenance(self, provenance: str) -> list[Node]:
        """Entities carrying a given provenance label (EXTRACTED/INFERRED/AMBIGUOUS)."""
        wanted = provenance.strip().upper()
        found = [n for n in self.nodes if n.provenance.upper() == wanted]
        return sorted(found, key=lambda n: (-n.degree, n.name.lower()))

    # -- Natural-language routing ---------------------------------------

    def answer(self, question: str) -> QueryAnswer:
        """Answer a natural-language question using structural heuristics.

        This deliberately avoids an LLM: it recognises a handful of question
        shapes and maps them onto the structural queries above, so it works
        offline and gives reproducible answers.  Unrecognised questions fall
        back to a name search over the terms in the question.
        """
        text = question.strip()
        lowered = text.lower()

        def _subject(*markers: str) -> str | None:
            for marker in markers:
                if marker in lowered:
                    tail = lowered.split(marker, 1)[1]
                    return tail.strip(" ?.,'\"")
            return None

        # "what depends on X" / "what uses X" -> incoming edges
        subject = _subject("depends on", "depend on", "uses ", "use ", "relies on")
        if subject:
            try:
                node = self.resolve(subject)
            except GraphQueryError:
                pass
            else:
                return QueryAnswer(
                    kind="dependents",
                    question=text,
                    subject=node,
                    nodes=self.dependents_of(node.name),
                    summary=f"Entities pointing at {node.name}",
                )

        # "what does X need / require / connect to" -> outgoing edges
        subject = _subject("what does ", "what do ")
        if subject:
            for verb in (" need", " require", " connect to", " use", " depend on"):
                if verb in f" {subject}":
                    subject = subject.split(verb.strip(), 1)[0].strip()
                    break
            try:
                node = self.resolve(subject)
            except GraphQueryError:
                pass
            else:
                return QueryAnswer(
                    kind="dependencies",
                    question=text,
                    subject=node,
                    nodes=self.dependencies_of(node.name),
                    summary=f"Entities {node.name} points at",
                )

        # "how are X and Y connected" -> path
        if " and " in lowered and any(w in lowered for w in ("connect", "related", "relate", "link")):
            head = lowered
            for prefix in ("how are ", "how is ", "how do ", "are "):
                if head.startswith(prefix):
                    head = head[len(prefix) :]
                    break
            for suffix in ("connected", "related", "relate", "linked", "connect"):
                head = head.split(suffix)[0]
            parts = [p.strip(" ?.,'\"") for p in head.split(" and ", 1)]
            if len(parts) == 2 and all(parts):
                try:
                    hops = self.path(parts[0], parts[1])
                except GraphQueryError:
                    pass
                else:
                    a = self.resolve(parts[0])
                    b = self.resolve(parts[1])
                    return QueryAnswer(
                        kind="path",
                        question=text,
                        subject=a,
                        hops=hops,
                        summary=(
                            f"{a.name} -> {b.name} in {len(hops)} hop(s)"
                            if hops
                            else f"No path found between {a.name} and {b.name}"
                        ),
                    )

        # "what are the most important / central things" -> god nodes
        if any(w in lowered for w in ("most important", "most central", "hub", "god node", "key concept")):
            return QueryAnswer(
                kind="god_nodes",
                question=text,
                nodes=self.god_nodes(top_n=10),
                summary="Highest-degree entities",
            )

        # Type-scoped: "which databases", "what technologies"
        for type_name in self.entity_types():
            label = type_name.replace("_", " ")
            if label in lowered or f"{label}s" in lowered:
                return QueryAnswer(
                    kind="by_type",
                    question=text,
                    nodes=self.of_type(type_name),
                    summary=f"Entities of type {label}",
                )

        # Fall back to searching the longest word in the question.
        words = sorted(
            (w.strip(" ?.,'\"") for w in text.split()),
            key=len,
            reverse=True,
        )
        for word in words:
            if len(word) < 3:
                continue
            hits = self.search(word)
            if hits:
                return QueryAnswer(
                    kind="search",
                    question=text,
                    nodes=hits,
                    summary=f"Entities matching {word!r}",
                )

        return QueryAnswer(kind="empty", question=text, summary="No matching entities")


@dataclass
class QueryAnswer:
    """The result of :meth:`GraphQuery.answer`."""

    kind: str
    question: str
    summary: str = ""
    subject: Optional[Node] = None
    nodes: list[Node] = field(default_factory=list)
    hops: list[Hop] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "question": self.question,
            "summary": self.summary,
            "subject": _node_dict(self.subject) if self.subject else None,
            "nodes": [_node_dict(n) for n in self.nodes],
            "hops": [
                {
                    "from": _node_dict(h.frm),
                    "relation": h.edge.relation,
                    "reversed": h.reversed_,
                    "to": _node_dict(h.to),
                }
                for h in self.hops
            ],
        }


def _node_dict(node: Node) -> dict[str, Any]:
    return {
        "id": node.id,
        "name": node.name,
        "type": node.type,
        "degree": node.degree,
        "community": node.community,
        "provenance": node.provenance,
        "confidence": node.confidence,
    }


def nodes_to_dicts(nodes: Iterable[Node]) -> list[dict[str, Any]]:
    """Public helper for serialising query results."""
    return [_node_dict(n) for n in nodes]
