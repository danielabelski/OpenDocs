"""Tests for the ``opendocs`` command-line interface.

``cli.py`` previously had no test coverage, which is how several
user-facing defects (a crash in ``inspect``, an unselectable format, and a
flag that was accepted but never forwarded) went unnoticed.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from opendocs.cli import ALL_FORMATS, FORMAT_CHOICES, FORMAT_MAP, main
from opendocs.core.models import OutputFormat

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def notebook_with_output(tmp_path):
    """A minimal notebook whose single code cell has a recognisable output."""
    nb = {
        "cells": [
            {"cell_type": "markdown", "source": ["# Sample Notebook\n"]},
            {
                "cell_type": "code",
                "execution_count": 1,
                "source": ["print('hi')"],
                "outputs": [
                    {
                        "output_type": "stream",
                        "name": "stdout",
                        "text": ["UNIQUE_OUTPUT_MARKER\n"],
                    }
                ],
            },
        ],
        "metadata": {},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    path = tmp_path / "sample.ipynb"
    path.write_text(json.dumps(nb), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Format choices
# ---------------------------------------------------------------------------


class TestFormatChoices:
    def test_every_mapped_format_is_selectable(self, runner):
        """Regression: 'mindmap' was in FORMAT_MAP but missing from --format."""
        result = runner.invoke(main, ["generate", "--help"])
        assert result.exit_code == 0
        for key in FORMAT_MAP:
            assert key in result.output, f"--format is missing the '{key}' choice"

    def test_mindmap_is_accepted(self, runner, tmp_path):
        """Regression: `-f mindmap` used to be rejected by click."""
        readme = tmp_path / "README.md"
        readme.write_text("# Title\n\nSome text.\n", encoding="utf-8")
        result = runner.invoke(
            main,
            ["generate", str(readme), "--local", "-f", "mindmap", "-o", str(tmp_path / "out")],
        )
        assert "is not one of" not in result.output

    @pytest.mark.parametrize("command", ["generate", "watch", "codebase"])
    def test_all_commands_share_the_same_choices(self, runner, command):
        """The three --format options must not drift apart again."""
        result = runner.invoke(main, [command, "--help"])
        assert result.exit_code == 0
        for key in FORMAT_CHOICES:
            assert key in result.output, f"{command} --format is missing '{key}'"

    def test_all_formats_excludes_the_all_sentinel(self):
        assert OutputFormat.ALL not in ALL_FORMATS
        assert OutputFormat.MINDMAP in ALL_FORMATS
        assert len(ALL_FORMATS) == len(FORMAT_MAP) - 1


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------


class TestInspect:
    def test_inspect_notebook_does_not_crash(self, runner, notebook_with_output):
        """Regression: `name` was unbound on the notebook branch."""
        result = runner.invoke(main, ["inspect", str(notebook_with_output)])
        assert result.exit_code == 0, result.output
        assert result.exception is None

    def test_inspect_notebook_reports_the_file_stem(self, runner, notebook_with_output):
        result = runner.invoke(main, ["inspect", str(notebook_with_output)])
        assert "sample" in result.output

    def test_inspect_markdown_still_works(self, runner, tmp_path):
        readme = tmp_path / "README.md"
        readme.write_text("# Hello\n\nBody text.\n\n## Sub\n", encoding="utf-8")
        result = runner.invoke(main, ["inspect", str(readme), "--local"])
        assert result.exit_code == 0, result.output
        assert "Sections" in result.output


# ---------------------------------------------------------------------------
# Notebook output inclusion
# ---------------------------------------------------------------------------


class TestNotebookOutputFlag:
    def _blog_text(self, tmp_path, name):
        return "".join(p.read_text(encoding="utf-8") for p in (tmp_path / name).glob("*.md"))

    def test_outputs_included_by_default(self, runner, notebook_with_output, tmp_path):
        result = runner.invoke(
            main,
            ["generate", str(notebook_with_output), "--local", "-f", "blog", "-o", str(tmp_path / "inc")],
        )
        assert result.exit_code == 0, result.output
        assert "UNIQUE_OUTPUT_MARKER" in self._blog_text(tmp_path, "inc")

    def test_no_outputs_flag_is_honoured(self, runner, notebook_with_output, tmp_path):
        """Regression: --no-outputs was parsed but never reached the parser."""
        result = runner.invoke(
            main,
            [
                "generate",
                str(notebook_with_output),
                "--local",
                "-f",
                "blog",
                "-o",
                str(tmp_path / "exc"),
                "--no-outputs",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "UNIQUE_OUTPUT_MARKER" not in self._blog_text(tmp_path, "exc")


# ---------------------------------------------------------------------------
# Smoke tests for the remaining commands
# ---------------------------------------------------------------------------


class TestSmoke:
    def test_version(self, runner):
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert "0.9.0" in result.output

    def test_themes_lists_themes(self, runner):
        result = runner.invoke(main, ["themes"])
        assert result.exit_code == 0
        assert "corporate" in result.output

    @pytest.mark.parametrize("command", ["generate", "watch", "codebase", "inspect", "themes"])
    def test_help_is_available(self, runner, command):
        result = runner.invoke(main, [command, "--help"])
        assert result.exit_code == 0
