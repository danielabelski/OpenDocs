"""Tests for documentation coverage analysis.

The value of a coverage number is entirely in whether it can be trusted, so
most of these tests are about what must *not* be counted: test code, private
helpers, and standard environment variables. A score inflated or deflated by
noise is worse than no score.
"""

from __future__ import annotations

import json
import textwrap

import pytest
from click.testing import CliRunner

from opendocs.cli import main
from opendocs.core.coverage import (
    CoverageReport,
    Dimension,
    analyse_coverage,
    extract_cli_flags,
    extract_env_vars,
    is_test_path,
)


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def project(tmp_path):
    """A small project with known documented / undocumented surface."""
    src = tmp_path / "src" / "demo"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("", encoding="utf-8")
    (src / "core.py").write_text(
        textwrap.dedent(
            '''
            """Module docstring."""
            import os
            import click


            def documented(a, b):
                """This one has a docstring."""
                return a + b


            def undocumented(a):
                return a


            def _private_helper():
                return None


            class Documented:
                """A documented class."""


            class Undocumented:
                pass


            def read_config():
                """Read settings from the environment."""
                token = os.environ["DEMO_TOKEN"]
                region = os.getenv("DEMO_REGION")
                home = os.environ.get("HOME")
                return token, region, home


            @click.option("--documented-flag", help="x")
            @click.option("--secret-flag", help="y")
            def cli():
                """Entry point."""
            '''
        ).strip(),
        encoding="utf-8",
    )

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_core.py").write_text(
        textwrap.dedent(
            """
            import os


            def helper_without_docstring():
                return os.environ["TEST_ONLY_VAR"]


            class TestSomething:
                def test_it(self):
                    assert True
            """
        ).strip(),
        encoding="utf-8",
    )

    (tmp_path / "README.md").write_text(
        textwrap.dedent(
            """
            # Demo

            A demo project used for testing coverage analysis end to end.

            Set `DEMO_TOKEN` before running. Use `--documented-flag` to enable
            the documented behaviour.
            """
        ).strip(),
        encoding="utf-8",
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Test-path classification — the fix that makes the score meaningful
# ---------------------------------------------------------------------------


class TestIsTestPath:
    @pytest.mark.parametrize(
        "path",
        [
            "tests/test_core.py",
            "tests/helpers.py",
            "src/pkg/tests/thing.py",
            "test/foo.py",
            "spec/bar.py",
            "src/pkg/test_module.py",
            "src/pkg/module_test.py",
            "conftest.py",
            "tests/conftest.py",
        ],
    )
    def test_identifies_test_code(self, path):
        assert is_test_path(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "src/opendocs/cli.py",
            "src/pkg/latest.py",
            "src/pkg/contest.py",
            "src/protest/module.py",
            "README.md",
        ],
    )
    def test_leaves_real_code_alone(self, path):
        assert is_test_path(path) is False

    def test_handles_windows_separators(self):
        assert is_test_path("tests\\test_core.py") is True


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


class TestEnvExtraction:
    @pytest.mark.parametrize(
        "snippet",
        [
            'os.environ["MY_VAR"]',
            "os.environ['MY_VAR']",
            'os.environ.get("MY_VAR")',
            'os.getenv("MY_VAR")',
            'environ["MY_VAR"]',
            'os.environ.get(  "MY_VAR", "default")',
        ],
    )
    def test_recognises_access_forms(self, snippet):
        assert "MY_VAR" in extract_env_vars([snippet])

    def test_skips_standard_names(self):
        found = extract_env_vars(['os.environ["PATH"]', 'os.getenv("HOME")', 'os.environ["XDG_CACHE_HOME"]'])
        assert found == set()

    def test_ignores_lowercase_locals(self):
        assert extract_env_vars(['config["my_var"]']) == set()

    def test_deduplicates_across_sources(self):
        assert extract_env_vars(['os.getenv("A_VAR")', 'os.environ["A_VAR"]']) == {"A_VAR"}


class TestFlagExtraction:
    @pytest.mark.parametrize(
        "snippet",
        [
            '@click.option("--my-flag")',
            "click.option('--my-flag', is_flag=True)",
            'parser.add_argument("--my-flag")',
        ],
    )
    def test_recognises_declaration_forms(self, snippet):
        assert "--my-flag" in extract_cli_flags([snippet])

    def test_skips_universal_flags(self):
        assert extract_cli_flags(['@click.option("--help")', 'option("--version")']) == set()

    def test_ignores_usage_in_prose_strings(self):
        """A flag mentioned in help text is not a declaration."""
        assert extract_cli_flags(['help="pass --other-flag to enable"']) == set()


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


class TestDimension:
    def test_percent(self):
        assert Dimension(name="d", total=4, covered=3).percent == 75.0

    def test_empty_dimension_scores_full(self):
        assert Dimension(name="d").percent == 100.0

    def test_empty_dimension_is_not_applicable(self):
        assert Dimension(name="d").applicable is False


class TestReportScoring:
    def test_overall_is_weighted_by_item_count(self):
        """One missed flag out of two must not outweigh 99 documented symbols."""
        report = CoverageReport(
            dimensions=[
                Dimension(name="big", total=100, covered=99),
                Dimension(name="small", total=2, covered=1),
            ]
        )
        # Naive averaging would give (99 + 50) / 2 = 74.5
        assert report.overall == pytest.approx(98.0, abs=0.1)

    def test_inapplicable_dimensions_are_excluded(self):
        """A project with no env vars should not bank a vacuous 100%."""
        report = CoverageReport(
            dimensions=[
                Dimension(name="real", total=10, covered=5),
                Dimension(name="absent", total=0, covered=0),
            ]
        )
        assert report.overall == 50.0

    def test_all_empty_scores_full(self):
        assert CoverageReport(dimensions=[Dimension(name="a")]).overall == 100.0

    def test_serialises(self):
        report = CoverageReport(project_name="x", dimensions=[Dimension(name="a", total=2, covered=1)])
        json.dumps(report.to_dict())
        assert report.to_dict()["overall_percent"] == 50.0


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


class TestAnalyseCoverage:
    @pytest.fixture
    def report(self, project):
        return analyse_coverage(project)

    def test_finds_the_readme_by_default(self, report):
        assert any("README.md" in d for d in report.docs_analysed)

    def test_scores_undocumented_function(self, report):
        missing = report.dimension("docstrings:functions").missing
        assert any("undocumented" in m for m in missing)
        assert not any("::documented" in m for m in missing)

    def test_private_helpers_are_excluded(self, report):
        missing = report.dimension("docstrings:functions").missing
        assert not any("_private_helper" in m for m in missing)

    def test_test_files_are_excluded(self, report):
        """Regression: test classes dominated the score before this."""
        for name in ("docstrings:functions", "docstrings:classes"):
            missing = report.dimension(name).missing
            assert not any("test_core" in m for m in missing), missing

    def test_test_only_env_vars_are_excluded(self, report):
        assert "TEST_ONLY_VAR" not in report.dimension("env-vars").missing

    def test_documented_env_var_counts_as_covered(self, report):
        assert "DEMO_TOKEN" not in report.dimension("env-vars").missing

    def test_undocumented_env_var_is_reported(self, report):
        assert "DEMO_REGION" in report.dimension("env-vars").missing

    def test_standard_env_var_is_not_required(self, report):
        assert "HOME" not in report.dimension("env-vars").missing

    def test_documented_flag_counts_as_covered(self, report):
        assert "--documented-flag" not in report.dimension("cli-flags").missing

    def test_undocumented_flag_is_reported(self, report):
        assert "--secret-flag" in report.dimension("cli-flags").missing

    def test_include_tests_changes_the_result(self, project):
        without = analyse_coverage(project)
        with_tests = analyse_coverage(project, include_tests=True)
        assert with_tests.dimension("docstrings:classes").total > without.dimension("docstrings:classes").total

    def test_include_private_changes_the_result(self, project):
        without = analyse_coverage(project)
        with_private = analyse_coverage(project, include_private=True)
        assert with_private.dimension("docstrings:functions").total > without.dimension("docstrings:functions").total

    def test_explicit_doc_paths(self, project, tmp_path):
        other = tmp_path / "GUIDE.md"
        other.write_text("Mentions DEMO_REGION and --secret-flag here.", encoding="utf-8")
        report = analyse_coverage(project, [other])
        assert "DEMO_REGION" not in report.dimension("env-vars").missing
        assert "--secret-flag" not in report.dimension("cli-flags").missing

    def test_missing_docs_does_not_crash(self, tmp_path):
        (tmp_path / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        report = analyse_coverage(tmp_path)
        assert report.docs_analysed == []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCoverageCommand:
    def test_reports(self, runner, project):
        result = runner.invoke(main, ["coverage", str(project)])
        assert result.exit_code == 0, result.output
        assert "Documentation coverage" in result.output

    def test_json_output(self, runner, project):
        result = runner.invoke(main, ["coverage", str(project), "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert "overall_percent" in payload
        assert {d["name"] for d in payload["dimensions"]} >= {"env-vars", "cli-flags"}

    def test_fail_under_gate_passes(self, runner, project):
        result = runner.invoke(main, ["coverage", str(project), "--fail-under", "1"])
        assert result.exit_code == 0

    def test_fail_under_gate_fails(self, runner, project):
        result = runner.invoke(main, ["coverage", str(project), "--fail-under", "100"])
        assert result.exit_code == 1
        assert "below" in result.output

    def test_show_missing_lists_everything(self, runner, project):
        brief = runner.invoke(main, ["coverage", str(project), "--limit", "1"])
        full = runner.invoke(main, ["coverage", str(project), "--show-missing"])
        assert len(full.output) >= len(brief.output)

    def test_nonexistent_directory_is_rejected(self, runner, tmp_path):
        result = runner.invoke(main, ["coverage", str(tmp_path / "absent")])
        assert result.exit_code != 0

    def test_help(self, runner):
        result = runner.invoke(main, ["coverage", "--help"])
        assert result.exit_code == 0
        assert "--fail-under" in result.output
