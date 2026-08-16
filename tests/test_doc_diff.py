"""Tests for documentation diffing and release-note generation."""

from __future__ import annotations

import json
import subprocess

import pytest
from click.testing import CliRunner

from opendocs.cli import main
from opendocs.core.doc_diff import (
    Snapshot,
    diff_snapshots,
    impacted_formats,
    render_release_notes,
    snapshot_from_git,
    snapshot_from_path,
)

V1 = """# Widget

Widget is a service for processing orders. It is written in Python and
deployed on AWS, where it handles a steady stream of incoming requests
without any manual intervention from the operations team.

## Installation

```bash
pip install widget
```

## Storage

Widget stores its data in PostgreSQL.

## License

MIT.
"""

V2 = """# Widget

Widget is a service for processing orders. It is written in Python and
deployed on AWS, where it handles a steady stream of incoming requests
without any manual intervention from the operations team.

## Installation

```bash
pip install widget
```

## Storage

Widget stores its data in PostgreSQL and caches results in Redis.

## Monitoring

Metrics are shipped to Prometheus.

## License

MIT.
"""


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def v1(tmp_path):
    p = tmp_path / "v1.md"
    p.write_text(V1, encoding="utf-8")
    return p


@pytest.fixture
def v2(tmp_path):
    p = tmp_path / "v2.md"
    p.write_text(V2, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


class TestSnapshots:
    def test_captures_sections(self, v1):
        snap = snapshot_from_path(v1)
        assert "Installation" in snap.sections
        assert "Storage" in snap.sections

    def test_captures_entities(self, v1):
        snap = snapshot_from_path(v1)
        assert any("postgres" in name.lower() for name in snap.entities)

    def test_uses_a_stable_project_identity(self, v1, v2):
        """Both sides must parse under the same name, or the PROJECT entity churns."""
        a = snapshot_from_path(v1)
        b = snapshot_from_path(v2)
        assert "v1.md" not in a.entities
        assert "v2.md" not in b.entities

    def test_graph_json_snapshot(self, tmp_path):
        payload = {
            "nodes": [
                {"id": "a", "name": "Alpha", "type": "component"},
                {"id": "b", "name": "Beta", "type": "database"},
            ],
            "edges": [{"source": "a", "target": "b", "relation": "stores_in"}],
        }
        p = tmp_path / "g.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        snap = snapshot_from_path(p)
        assert snap.entities == {"Alpha": "component", "Beta": "database"}
        assert ("Alpha", "stores_in", "Beta") in snap.relations

    def test_rejects_non_graph_json(self, tmp_path):
        p = tmp_path / "g.json"
        p.write_text(json.dumps({"nope": 1}), encoding="utf-8")
        with pytest.raises(ValueError, match="not an opendocs graph.json"):
            snapshot_from_path(p)


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------


class TestDiff:
    def test_identical_sources_produce_no_delta(self, v1):
        delta = diff_snapshots(snapshot_from_path(v1), snapshot_from_path(v1))
        assert delta.is_empty

    def test_detects_added_sections(self, v1, v2):
        delta = diff_snapshots(snapshot_from_path(v1), snapshot_from_path(v2))
        added = {s.title for s in delta.sections if s.change == "added"}
        assert "Monitoring" in added

    def test_detects_removed_sections(self, v1, v2):
        delta = diff_snapshots(snapshot_from_path(v2), snapshot_from_path(v1))
        removed = {s.title for s in delta.sections if s.change == "removed"}
        assert "Monitoring" in removed

    def test_detects_added_entities(self, v1, v2):
        delta = diff_snapshots(snapshot_from_path(v1), snapshot_from_path(v2))
        added = {e.name.lower() for e in delta.added_entities()}
        assert any("redis" in name for name in added)

    def test_direction_matters(self, v1, v2):
        forward = diff_snapshots(snapshot_from_path(v1), snapshot_from_path(v2))
        backward = diff_snapshots(snapshot_from_path(v2), snapshot_from_path(v1))
        assert len(forward.added_entities()) == len(backward.removed_entities())

    def test_detects_retyped_entities(self):
        old = Snapshot(label="a", entities={"Thing": "component"})
        new = Snapshot(label="b", entities={"Thing": "database"})
        delta = diff_snapshots(old, new)
        assert delta.retyped_entities()[0].detail == "component -> database"

    def test_relation_changes(self):
        old = Snapshot(label="a", entities={"A": "x", "B": "y"}, relations={("A", "uses", "B")})
        new = Snapshot(label="b", entities={"A": "x", "B": "y"}, relations={("A", "requires", "B")})
        delta = diff_snapshots(old, new)
        assert {r.change for r in delta.relations} == {"added", "removed"}

    def test_counts(self, v1, v2):
        counts = diff_snapshots(snapshot_from_path(v1), snapshot_from_path(v2)).counts()
        assert counts["sections_added"] >= 1
        assert counts["sections_removed"] == 0

    def test_serialises(self, v1, v2):
        payload = diff_snapshots(snapshot_from_path(v1), snapshot_from_path(v2)).to_dict()
        json.dumps(payload)
        assert payload["old"] == "v1.md"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestReleaseNotes:
    def test_empty_delta_says_so(self, v1):
        notes = render_release_notes(diff_snapshots(snapshot_from_path(v1), snapshot_from_path(v1)))
        assert "No documentation changes detected" in notes

    def test_lists_new_sections(self, v1, v2):
        notes = render_release_notes(diff_snapshots(snapshot_from_path(v1), snapshot_from_path(v2)))
        assert "## New sections" in notes
        assert "Monitoring" in notes

    def test_custom_title(self, v1, v2):
        notes = render_release_notes(
            diff_snapshots(snapshot_from_path(v1), snapshot_from_path(v2)), title="Release 2.0"
        )
        assert notes.startswith("# Release 2.0")

    def test_output_is_valid_markdown_structure(self, v1, v2):
        notes = render_release_notes(diff_snapshots(snapshot_from_path(v1), snapshot_from_path(v2)))
        assert notes.count("# ") >= 1
        assert "-> `" in notes or "->" in notes


class TestImpactedFormats:
    def test_empty_delta_impacts_nothing(self, v1):
        delta = diff_snapshots(snapshot_from_path(v1), snapshot_from_path(v1))
        assert impacted_formats(delta) == []

    def test_section_change_impacts_documents(self, v1, v2):
        formats = impacted_formats(diff_snapshots(snapshot_from_path(v1), snapshot_from_path(v2)))
        assert "word" in formats and "pdf" in formats

    def test_relation_only_change_impacts_graph_outputs(self):
        old = Snapshot(label="a", entities={"A": "x", "B": "y"}, relations=set())
        new = Snapshot(label="b", entities={"A": "x", "B": "y"}, relations={("A", "uses", "B")})
        formats = impacted_formats(diff_snapshots(old, new))
        assert "graph" in formats
        assert "word" not in formats


# ---------------------------------------------------------------------------
# Git mode
# ---------------------------------------------------------------------------


def _git_available() -> bool:
    return subprocess.run(["git", "--version"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _git_available(), reason="git is not available")
class TestGitMode:
    @pytest.fixture
    def repo(self, tmp_path):
        r = tmp_path / "repo"
        r.mkdir()
        for args in (
            ["init", "-b", "main"],
            ["config", "user.email", "t@example.com"],
            ["config", "user.name", "T"],
            ["config", "commit.gpgsign", "false"],
        ):
            subprocess.run(["git", *args], cwd=str(r), capture_output=True)
        (r / "README.md").write_text(V1, encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(r), capture_output=True)
        subprocess.run(["git", "commit", "-m", "v1"], cwd=str(r), capture_output=True)
        (r / "README.md").write_text(V2, encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(r), capture_output=True)
        subprocess.run(["git", "commit", "-m", "v2"], cwd=str(r), capture_output=True)
        return r

    def test_reads_a_past_revision(self, repo):
        snap = snapshot_from_git(repo, "HEAD~1", "README.md")
        assert "Monitoring" not in snap.sections

    def test_reads_head(self, repo):
        snap = snapshot_from_git(repo, "HEAD", "README.md")
        assert "Monitoring" in snap.sections

    def test_diff_across_revisions(self, repo):
        delta = diff_snapshots(
            snapshot_from_git(repo, "HEAD~1", "README.md"),
            snapshot_from_git(repo, "HEAD", "README.md"),
        )
        assert {s.title for s in delta.sections if s.change == "added"} == {"Monitoring"}

    def test_no_phantom_project_churn(self, repo):
        """Regression: labelling each side by ref made the PROJECT entity churn."""
        delta = diff_snapshots(
            snapshot_from_git(repo, "HEAD~1", "README.md"),
            snapshot_from_git(repo, "HEAD", "README.md"),
        )
        names = {e.name for e in delta.entities}
        assert not any(":" in n for n in names), f"ref labels leaked into entities: {names}"

    def test_unknown_ref_raises(self, repo):
        with pytest.raises(ValueError, match="Could not read"):
            snapshot_from_git(repo, "nosuchref", "README.md")

    def test_cli_git_mode(self, runner, repo):
        r = runner.invoke(main, ["diff", "HEAD~1", "HEAD", "--git", str(repo), "--path", "README.md"])
        assert r.exit_code == 0, r.output
        assert "Monitoring" in r.output


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestDiffCommand:
    def test_summary_output(self, runner, v1, v2):
        r = runner.invoke(main, ["diff", str(v1), str(v2)])
        assert r.exit_code == 0, r.output
        assert "Monitoring" in r.output

    def test_identical_reports_no_change(self, runner, v1):
        r = runner.invoke(main, ["diff", str(v1), str(v1)])
        assert r.exit_code == 0
        assert "No documentation changes" in r.output

    def test_markdown_output(self, runner, v1, v2):
        r = runner.invoke(main, ["diff", str(v1), str(v2), "--format", "markdown"])
        assert r.exit_code == 0
        assert "## New sections" in r.output

    def test_json_output(self, runner, v1, v2):
        r = runner.invoke(main, ["diff", str(v1), str(v2), "--format", "json"])
        assert r.exit_code == 0
        payload = json.loads(r.output)
        assert payload["counts"]["sections_added"] >= 1
        assert "impacted_formats" in payload

    def test_writes_to_a_file(self, runner, v1, v2, tmp_path):
        out = tmp_path / "NOTES.md"
        r = runner.invoke(main, ["diff", str(v1), str(v2), "--format", "markdown", "-o", str(out)])
        assert r.exit_code == 0
        assert "## New sections" in out.read_text(encoding="utf-8")

    def test_fail_on_change_gate(self, runner, v1, v2):
        assert runner.invoke(main, ["diff", str(v1), str(v1), "--fail-on-change"]).exit_code == 0
        assert runner.invoke(main, ["diff", str(v1), str(v2), "--fail-on-change"]).exit_code == 1

    def test_missing_file_exits_2(self, runner, v1, tmp_path):
        r = runner.invoke(main, ["diff", str(v1), str(tmp_path / "absent.md")])
        assert r.exit_code == 2

    def test_graph_json_comparison(self, runner, tmp_path):
        def write(name, names):
            p = tmp_path / name
            p.write_text(
                json.dumps({"nodes": [{"id": n, "name": n, "type": "component"} for n in names], "edges": []}),
                encoding="utf-8",
            )
            return p

        a = write("a.json", ["Alpha"])
        b = write("b.json", ["Alpha", "Beta"])
        r = runner.invoke(main, ["diff", str(a), str(b), "--format", "json"])
        assert r.exit_code == 0
        payload = json.loads(r.output)
        assert payload["counts"]["entities_added"] == 1
