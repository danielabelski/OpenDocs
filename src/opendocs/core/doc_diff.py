"""Compare two versions of a document and report what changed.

Answers "what changed in the docs since the last release?" by diffing both
the prose structure (sections) and the extracted knowledge graph (entities and
relations) between two versions of a source, then rendering the delta as
release notes.

Sources can be Markdown/Notebook files, previously exported ``graph.json``
files, or two git revisions of the same path.  Everything is deterministic and
offline.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .knowledge_graph import KnowledgeGraph
from .models import DocumentModel, HeadingBlock

# ---------------------------------------------------------------------------
# Delta models
# ---------------------------------------------------------------------------


@dataclass
class EntityChange:
    """One entity that appeared, disappeared, or was reclassified."""

    name: str
    entity_type: str
    change: str  # added | removed | retyped
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.entity_type,
            "change": self.change,
            "detail": self.detail,
        }


@dataclass
class RelationChange:
    """One relation that appeared or disappeared."""

    source: str
    target: str
    relation: str
    change: str  # added | removed

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "relation": self.relation,
            "change": self.change,
        }


@dataclass
class SectionChange:
    """One documentation section that appeared or disappeared."""

    title: str
    level: int
    change: str  # added | removed

    def to_dict(self) -> dict[str, Any]:
        return {"title": self.title, "level": self.level, "change": self.change}


@dataclass
class DocDelta:
    """Everything that changed between two versions."""

    old_label: str = "old"
    new_label: str = "new"
    entities: list[EntityChange] = field(default_factory=list)
    relations: list[RelationChange] = field(default_factory=list)
    sections: list[SectionChange] = field(default_factory=list)
    old_counts: dict[str, int] = field(default_factory=dict)
    new_counts: dict[str, int] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not (self.entities or self.relations or self.sections)

    def added_entities(self) -> list[EntityChange]:
        return [e for e in self.entities if e.change == "added"]

    def removed_entities(self) -> list[EntityChange]:
        return [e for e in self.entities if e.change == "removed"]

    def retyped_entities(self) -> list[EntityChange]:
        return [e for e in self.entities if e.change == "retyped"]

    def counts(self) -> dict[str, int]:
        return {
            "entities_added": len(self.added_entities()),
            "entities_removed": len(self.removed_entities()),
            "entities_retyped": len(self.retyped_entities()),
            "relations_added": sum(1 for r in self.relations if r.change == "added"),
            "relations_removed": sum(1 for r in self.relations if r.change == "removed"),
            "sections_added": sum(1 for s in self.sections if s.change == "added"),
            "sections_removed": sum(1 for s in self.sections if s.change == "removed"),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "old": self.old_label,
            "new": self.new_label,
            "counts": self.counts(),
            "old_totals": self.old_counts,
            "new_totals": self.new_counts,
            "entities": [e.to_dict() for e in self.entities],
            "relations": [r.to_dict() for r in self.relations],
            "sections": [s.to_dict() for s in self.sections],
        }


# ---------------------------------------------------------------------------
# Loading a comparable snapshot
# ---------------------------------------------------------------------------


@dataclass
class Snapshot:
    """One side of a comparison: a parsed document plus its knowledge graph."""

    label: str
    entities: dict[str, str] = field(default_factory=dict)  # name -> type
    relations: set[tuple[str, str, str]] = field(default_factory=set)
    sections: dict[str, int] = field(default_factory=dict)  # title -> level

    @property
    def totals(self) -> dict[str, int]:
        return {
            "entities": len(self.entities),
            "relations": len(self.relations),
            "sections": len(self.sections),
        }

    @classmethod
    def from_document(cls, doc: DocumentModel, kg: KnowledgeGraph, label: str) -> Snapshot:
        entities = {e.name: e.entity_type.value for e in kg.entities}
        by_id = {e.id: e.name for e in kg.entities}
        relations = {
            (by_id[r.source_id], r.relation_type.value, by_id[r.target_id])
            for r in kg.relations
            if r.source_id in by_id and r.target_id in by_id
        }
        sections = {b.text.strip(): b.level for b in doc.all_blocks if isinstance(b, HeadingBlock) and b.text.strip()}
        return cls(label=label, entities=entities, relations=relations, sections=sections)

    @classmethod
    def from_graph_json(cls, payload: dict[str, Any], label: str) -> Snapshot:
        """Build a snapshot from an exported graph.json.

        Section titles are not stored in the export, so a graph-to-graph
        comparison reports entity and relation changes only.
        """
        nodes = payload.get("nodes", []) or []
        entities = {
            str(n.get("name", "")): str(n.get("type", "")) for n in nodes if isinstance(n, dict) and n.get("name")
        }
        by_id = {str(n.get("id", "")): str(n.get("name", "")) for n in nodes if isinstance(n, dict)}
        relations: set[tuple[str, str, str]] = set()
        for e in payload.get("edges", []) or []:
            if not isinstance(e, dict):
                continue
            src = by_id.get(str(e.get("source", "")))
            tgt = by_id.get(str(e.get("target", "")))
            if src and tgt:
                relations.add((src, str(e.get("relation", "")), tgt))
        return cls(label=label, entities=entities, relations=relations, sections={})


def _parse_source(content: str, *, is_notebook_source: bool, name: str) -> tuple[DocumentModel, KnowledgeGraph]:
    from .notebook_parser import NotebookParser
    from .parser import ReadmeParser
    from .semantic_extractor import SemanticExtractor

    if is_notebook_source:
        doc = NotebookParser().parse_content(content, repo_name=name)
    else:
        doc = ReadmeParser().parse(content, repo_name=name)
    return doc, SemanticExtractor().extract(doc)


#: Identity given to both sides of a comparison.  The extractor turns the
#: repo name into a PROJECT entity that everything else links to, so feeding it
#: a per-version label (a path or a git ref) would make that entity — and every
#: relation touching it — appear added on one side and removed on the other.
#: The display label is kept separate from this parsed identity.
_STABLE_PROJECT_NAME = "project"


def snapshot_from_path(
    path: str | Path,
    *,
    label: Optional[str] = None,
    repo_name: str = _STABLE_PROJECT_NAME,
) -> Snapshot:
    """Build a snapshot from a Markdown file, notebook, or exported graph.json.

    *label* names this side in the report; *repo_name* is the identity handed
    to the parser and must match on both sides of a comparison.
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    display = label or p.name

    if p.suffix.lower() == ".json":
        payload = json.loads(text)
        if not isinstance(payload, dict) or "nodes" not in payload:
            raise ValueError(f"{p} is not an opendocs graph.json (no 'nodes' array)")
        return Snapshot.from_graph_json(payload, display)

    doc, kg = _parse_source(text, is_notebook_source=p.suffix.lower() == ".ipynb", name=repo_name)
    return Snapshot.from_document(doc, kg, display)


def snapshot_from_git(
    repo_dir: str | Path,
    ref: str,
    file_path: str,
    *,
    repo_name: str = _STABLE_PROJECT_NAME,
) -> Snapshot:
    """Build a snapshot from a file as it existed at a git revision."""
    result = subprocess.run(
        ["git", "show", f"{ref}:{file_path}"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise ValueError(f"Could not read {file_path} at {ref}: {result.stderr.strip()}")

    doc, kg = _parse_source(
        result.stdout,
        is_notebook_source=file_path.lower().endswith(".ipynb"),
        name=repo_name,
    )
    return Snapshot.from_document(doc, kg, f"{ref}:{file_path}")


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------


def diff_snapshots(old: Snapshot, new: Snapshot) -> DocDelta:
    """Compare two snapshots and return the delta."""
    delta = DocDelta(
        old_label=old.label,
        new_label=new.label,
        old_counts=old.totals,
        new_counts=new.totals,
    )

    old_names = set(old.entities)
    new_names = set(new.entities)

    for name in sorted(new_names - old_names):
        delta.entities.append(EntityChange(name=name, entity_type=new.entities[name], change="added"))
    for name in sorted(old_names - new_names):
        delta.entities.append(EntityChange(name=name, entity_type=old.entities[name], change="removed"))
    for name in sorted(old_names & new_names):
        if old.entities[name] != new.entities[name]:
            delta.entities.append(
                EntityChange(
                    name=name,
                    entity_type=new.entities[name],
                    change="retyped",
                    detail=f"{old.entities[name]} -> {new.entities[name]}",
                )
            )

    for src, rel, tgt in sorted(new.relations - old.relations):
        delta.relations.append(RelationChange(source=src, target=tgt, relation=rel, change="added"))
    for src, rel, tgt in sorted(old.relations - new.relations):
        delta.relations.append(RelationChange(source=src, target=tgt, relation=rel, change="removed"))

    old_sections = set(old.sections)
    new_sections = set(new.sections)
    for title in sorted(new_sections - old_sections):
        delta.sections.append(SectionChange(title=title, level=new.sections[title], change="added"))
    for title in sorted(old_sections - new_sections):
        delta.sections.append(SectionChange(title=title, level=old.sections[title], change="removed"))

    return delta


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_release_notes(delta: DocDelta, *, title: str = "Documentation Changes") -> str:
    """Render a delta as Markdown release notes."""
    lines: list[str] = [f"# {title}", ""]
    lines.append(f"Comparing `{delta.old_label}` -> `{delta.new_label}`.")
    lines.append("")

    if delta.is_empty:
        lines.append("No documentation changes detected.")
        lines.append("")
        return "\n".join(lines)

    counts = delta.counts()
    summary_bits = [
        f"{counts['sections_added']} section(s) added" if counts["sections_added"] else "",
        f"{counts['sections_removed']} section(s) removed" if counts["sections_removed"] else "",
        f"{counts['entities_added']} concept(s) added" if counts["entities_added"] else "",
        f"{counts['entities_removed']} concept(s) removed" if counts["entities_removed"] else "",
    ]
    summary = ", ".join(b for b in summary_bits if b)
    if summary:
        lines.append(f"**Summary:** {summary}.")
        lines.append("")

    added_sections = [s for s in delta.sections if s.change == "added"]
    if added_sections:
        lines.append("## New sections")
        lines.append("")
        for s in added_sections:
            lines.append(f"- {s.title}")
        lines.append("")

    removed_sections = [s for s in delta.sections if s.change == "removed"]
    if removed_sections:
        lines.append("## Removed sections")
        lines.append("")
        for s in removed_sections:
            lines.append(f"- {s.title}")
        lines.append("")

    added = delta.added_entities()
    if added:
        lines.append("## New concepts")
        lines.append("")
        by_type: dict[str, list[str]] = {}
        for e in added:
            by_type.setdefault(e.entity_type.replace("_", " ").title(), []).append(e.name)
        for type_name in sorted(by_type):
            lines.append(f"**{type_name}**")
            for name in sorted(by_type[type_name]):
                lines.append(f"- {name}")
            lines.append("")

    removed = delta.removed_entities()
    if removed:
        lines.append("## Concepts no longer mentioned")
        lines.append("")
        for e in removed:
            lines.append(f"- {e.name} _({e.entity_type.replace('_', ' ')})_")
        lines.append("")

    retyped = delta.retyped_entities()
    if retyped:
        lines.append("## Reclassified concepts")
        lines.append("")
        for e in retyped:
            lines.append(f"- {e.name}: {e.detail}")
        lines.append("")

    added_rel = [r for r in delta.relations if r.change == "added"]
    if added_rel:
        lines.append("## New relationships")
        lines.append("")
        for r in added_rel[:40]:
            lines.append(f"- {r.source} --{r.relation.replace('_', ' ')}--> {r.target}")
        if len(added_rel) > 40:
            lines.append(f"- _...and {len(added_rel) - 40} more_")
        lines.append("")

    return "\n".join(lines)


def impacted_formats(delta: DocDelta) -> list[str]:
    """Which output formats are worth regenerating for this delta.

    Deliberately conservative: any structural change invalidates the
    document-shaped outputs, while graph-shaped outputs only need rebuilding
    when the graph itself moved.
    """
    formats: set[str] = set()
    if delta.sections or delta.entities:
        formats.update({"word", "pdf", "pptx", "blog", "latex", "onepager", "faq"})
    if delta.entities or delta.relations:
        formats.update({"architecture", "mindmap", "graph", "wiki"})
    if delta.sections:
        formats.add("changelog")
    return sorted(formats)
