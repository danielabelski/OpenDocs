"""Tests for the documentation linter.

All rules here are offline. Link checking is exercised with a stubbed HTTP
client so the suite never reaches the network.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from opendocs.cli import main
from opendocs.core.linter import (
    OFFLINE_RULES,
    Finding,
    LintReport,
    Severity,
    check_links,
    collect_links,
    is_badge,
    lint_document,
    rule_duplicate_headings,
    rule_has_title,
    rule_heading_hierarchy,
    rule_images_have_alt_text,
    rule_placeholders,
    rule_table_shape,
    rule_todo_markers,
    rule_unlabelled_code,
)
from opendocs.core.parser import ReadmeParser

GOOD_README = """# My Project

My Project is a small library that does one thing well. It is written in
Python and has no runtime dependencies beyond the standard library, which
keeps installation quick and predictable for everyone involved.

The library exposes a single entry point and is designed to be readable
end to end in a few minutes. Everything it does is deliberately boring:
no metaclasses, no global state, and no configuration files to discover
before the first call succeeds.

## Installation

```bash
pip install my-project
```

## Usage

```python
import my_project
my_project.run()
```

| Option | Type | Default |
|--------|------|---------|
| debug  | bool | False   |

## License

MIT.
"""


def parse(markdown: str):
    return ReadmeParser().parse(markdown, repo_name="test")


@pytest.fixture
def runner():
    return CliRunner()


# ---------------------------------------------------------------------------
# A healthy document
# ---------------------------------------------------------------------------


class TestCleanDocument:
    def test_no_errors_or_warnings(self):
        report = lint_document(parse(GOOD_README))
        assert report.errors == []
        assert report.warnings == []

    def test_exit_code_is_zero(self):
        assert lint_document(parse(GOOD_README)).exit_code() == 0

    def test_all_rules_ran(self):
        assert lint_document(parse(GOOD_README)).checked == len(OFFLINE_RULES)


# ---------------------------------------------------------------------------
# Individual rules
# ---------------------------------------------------------------------------


class TestTitleAndContent:
    def test_missing_h1_is_an_error(self):
        findings = list(rule_has_title(parse("## Only an H2\n\nSome text here.\n")))
        assert findings and findings[0].severity is Severity.ERROR

    def test_no_headings_at_all(self):
        findings = list(rule_has_title(parse("Just a paragraph.\n")))
        assert findings[0].rule == "no-title"

    def test_h1_passes(self):
        assert list(rule_has_title(parse("# Title\n\nBody.\n"))) == []

    def test_thin_content_warns(self):
        report = lint_document(parse("# T\n\nToo short.\n"))
        assert any(f.rule == "thin-content" for f in report.findings)

    def test_no_prose_is_an_error(self):
        report = lint_document(parse("# T\n\n```\ncode only\n```\n"))
        assert any(f.rule == "no-description" and f.severity is Severity.ERROR for f in report.findings)


class TestSections:
    def test_missing_sections_warn(self):
        report = lint_document(parse("# T\n\n" + "word " * 60))
        rules = {f.rule for f in report.findings}
        assert {"missing-installation", "missing-usage", "missing-license"} <= rules

    def test_aliases_are_accepted(self):
        md = "# T\n\n" + "word " * 60 + "\n\n## Setup\n\nx\n\n## Examples\n\ny\n\n## Licence\n\nz\n"
        report = lint_document(parse(md))
        rules = {f.rule for f in report.findings}
        assert "missing-installation" not in rules
        assert "missing-usage" not in rules
        assert "missing-license" not in rules


class TestStructure:
    def test_heading_jump(self):
        findings = list(rule_heading_hierarchy(parse("# A\n\n### C\n")))
        assert findings and findings[0].rule == "heading-jump"

    def test_sequential_headings_are_fine(self):
        assert list(rule_heading_hierarchy(parse("# A\n\n## B\n\n### C\n"))) == []

    def test_duplicate_headings(self):
        findings = list(rule_duplicate_headings(parse("# A\n\n## Dup\n\nx\n\n## Dup\n\ny\n")))
        assert findings and findings[0].rule == "duplicate-heading"

    def test_unlabelled_code(self):
        findings = list(rule_unlabelled_code(parse("# A\n\n```\nplain\n```\n")))
        assert findings and findings[0].rule == "unlabelled-code"

    def test_labelled_code_is_fine(self):
        assert list(rule_unlabelled_code(parse("# A\n\n```python\nx = 1\n```\n"))) == []


class TestRaggedTables:
    def test_detects_mismatched_row(self):
        md = "# T\n\n| A | B | C |\n|---|---|---|\n| 1 | 2 | 3 |\n| 4 | 5 |\n"
        findings = list(rule_table_shape(parse(md)))
        assert findings and findings[0].rule == "ragged-table"

    def test_valid_table_passes(self):
        md = "# T\n\n| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n"
        assert list(rule_table_shape(parse(md))) == []

    def test_ignores_pipes_inside_code_fences(self):
        md = "# T\n\n```\n| A | B | C |\n|---|---|---|\n| 1 | 2 |\n```\n"
        assert list(rule_table_shape(parse(md))) == []

    def test_ignores_prose_containing_pipes(self):
        md = "# T\n\nUse `a | b` to pipe things together in the shell.\n"
        assert list(rule_table_shape(parse(md))) == []

    def test_reports_only_once_per_table(self):
        md = "# T\n\n| A | B | C |\n|---|---|---|\n| 1 |\n| 2 |\n| 3 |\n"
        assert len(list(rule_table_shape(parse(md)))) == 1


class TestContentMarkers:
    def test_todo_marker(self):
        findings = list(rule_todo_markers(parse("# T\n\nTODO: finish this.\n")))
        assert findings and findings[0].severity is Severity.WARNING

    @pytest.mark.parametrize("marker", ["FIXME", "TBD", "XXX", "WIP"])
    def test_other_markers(self, marker):
        findings = list(rule_todo_markers(parse(f"# T\n\n{marker} something.\n")))
        assert findings

    def test_placeholder_in_prose_is_an_error(self):
        findings = list(rule_placeholders(parse("# T\n\nClone yourusername/repo to begin.\n")))
        assert findings and findings[0].severity is Severity.ERROR

    def test_placeholder_in_a_fenced_block_is_an_error(self):
        """This is the worst case: the command a reader copies and runs."""
        md = "# T\n\n```bash\npip install your-project-name\n```\n"
        findings = list(rule_placeholders(parse(md)))
        assert findings and findings[0].severity is Severity.ERROR

    def test_clean_prose_has_no_markers(self):
        assert list(rule_todo_markers(parse("# T\n\nAll finished.\n"))) == []

    def test_markers_in_inline_code_are_not_flagged(self):
        """Documentation *about* markers writes them as code, not as prose."""
        md = "# T\n\nThe linter reports `TODO` and `FIXME` markers found in prose.\n"
        assert list(rule_todo_markers(parse(md))) == []

    def test_markers_in_fenced_code_are_not_flagged(self):
        md = "# T\n\n```python\n# TODO: this is sample code\n```\n"
        assert list(rule_todo_markers(parse(md))) == []

    def test_placeholders_in_inline_code_are_not_flagged(self):
        md = "# T\n\nWe warn when `your-project-name` or `CHANGEME` survives.\n"
        assert list(rule_placeholders(parse(md))) == []

    def test_real_markers_still_flagged_alongside_code(self):
        """Stripping code must not hide a genuine marker elsewhere."""
        md = "# T\n\nSee `TODO` docs.\n\nTODO: actually write this section.\n"
        findings = list(rule_todo_markers(parse(md)))
        assert len(findings) == 1


class TestImages:
    def test_missing_alt_text_warns(self):
        findings = list(rule_images_have_alt_text(parse("# T\n\n![](https://example.com/a.png)\n")))
        assert findings and findings[0].rule == "image-no-alt"

    def test_alt_text_passes(self):
        assert list(rule_images_have_alt_text(parse("# T\n\n![A diagram](https://example.com/a.png)\n"))) == []

    def test_badges_are_exempt(self):
        md = "# T\n\n![](https://img.shields.io/pypi/v/x.svg)\n"
        assert list(rule_images_have_alt_text(parse(md))) == []


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


class TestLinkCollection:
    def test_collects_inline_links(self):
        links = collect_links(parse("# T\n\nSee [docs](https://example.com/docs).\n"))
        assert "https://example.com/docs" in links

    def test_collects_image_sources(self):
        links = collect_links(parse("# T\n\n![x](https://example.com/i.png)\n"))
        assert "https://example.com/i.png" in links

    def test_collects_bare_urls(self):
        links = collect_links(parse("# T\n\nGo to https://example.com/bare for more.\n"))
        assert "https://example.com/bare" in links

    def test_deduplicates(self):
        md = "# T\n\n[a](https://example.com/x) and [b](https://example.com/x)\n"
        assert collect_links(parse(md)).count("https://example.com/x") == 1

    def test_ignores_relative_links(self):
        assert collect_links(parse("# T\n\n[local](./CONTRIBUTING.md)\n")) == []

    @pytest.mark.parametrize(
        "url",
        ["https://img.shields.io/badge/x", "https://codecov.io/gh/a/b", "https://circleci.com/gh/a"],
    )
    def test_badge_detection(self, url):
        assert is_badge(url)


class TestLinkChecking:
    """The only networked rule — stubbed so the suite stays offline."""

    def _client(self, status_map, raise_for=None):
        class _Resp:
            def __init__(self, code):
                self.status_code = code

        class _Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def head(self, url):
                if raise_for and url in raise_for:
                    raise ConnectionError("boom")
                return _Resp(status_map.get(url, 200))

            def get(self, url):
                return self.head(url)

        return _Client

    def test_reports_404(self, monkeypatch):
        import httpx

        doc = parse("# T\n\n[gone](https://example.com/missing)\n")
        monkeypatch.setattr(httpx, "Client", self._client({"https://example.com/missing": 404}))
        findings = check_links(doc)
        assert findings and findings[0].severity is Severity.ERROR
        assert "404" in findings[0].message

    def test_server_error_is_only_a_warning(self, monkeypatch):
        import httpx

        doc = parse("# T\n\n[flaky](https://example.com/down)\n")
        monkeypatch.setattr(httpx, "Client", self._client({"https://example.com/down": 503}))
        findings = check_links(doc)
        assert findings and findings[0].severity is Severity.WARNING

    def test_unreachable_host(self, monkeypatch):
        import httpx

        doc = parse("# T\n\n[nope](https://example.invalid/x)\n")
        monkeypatch.setattr(httpx, "Client", self._client({}, raise_for={"https://example.invalid/x"}))
        findings = check_links(doc)
        assert findings and "could not be reached" in findings[0].message

    def test_healthy_links_produce_nothing(self, monkeypatch):
        import httpx

        doc = parse("# T\n\n[ok](https://example.com/fine)\n")
        monkeypatch.setattr(httpx, "Client", self._client({}))
        assert check_links(doc) == []

    def test_badges_skipped_by_default(self, monkeypatch):
        import httpx

        doc = parse("# T\n\n![](https://img.shields.io/badge/broken)\n")
        monkeypatch.setattr(httpx, "Client", self._client({"https://img.shields.io/badge/broken": 404}))
        assert check_links(doc) == []
        assert check_links(doc, include_badges=True)


# ---------------------------------------------------------------------------
# Report behaviour
# ---------------------------------------------------------------------------


class TestReport:
    def _report(self):
        return LintReport(
            findings=[
                Finding("a", Severity.INFO, "i"),
                Finding("b", Severity.ERROR, "e"),
                Finding("c", Severity.WARNING, "w"),
            ]
        )

    def test_counts(self):
        assert self._report().counts() == {"error": 1, "warning": 1, "info": 1}

    @pytest.mark.parametrize(
        ("fail_on", "expected"),
        [(Severity.ERROR, 1), (Severity.WARNING, 1), (Severity.INFO, 1)],
    )
    def test_exit_code_thresholds(self, fail_on, expected):
        assert self._report().exit_code(fail_on=fail_on) == expected

    def test_warning_only_report_passes_at_error_threshold(self):
        report = LintReport(findings=[Finding("c", Severity.WARNING, "w")])
        assert report.exit_code(fail_on=Severity.ERROR) == 0
        assert report.exit_code(fail_on=Severity.WARNING) == 1

    def test_findings_sorted_most_severe_first(self):
        report = lint_document(parse("## No title\n\nTODO: x\n"))
        ranks = [f.severity.rank for f in report.findings]
        assert ranks == sorted(ranks, reverse=True)

    def test_a_broken_rule_does_not_abort_the_run(self):
        def exploding(_doc):
            raise ValueError("bad rule")

        report = lint_document(parse(GOOD_README), rules=[exploding])
        assert any("bad rule" in f.message for f in report.findings)

    def test_serialises(self):
        json.dumps(lint_document(parse(GOOD_README)).to_dict())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestLintCommand:
    @pytest.fixture
    def good(self, tmp_path):
        p = tmp_path / "README.md"
        p.write_text(GOOD_README, encoding="utf-8")
        return p

    @pytest.fixture
    def bad(self, tmp_path):
        p = tmp_path / "BAD.md"
        p.write_text("## No title\n\nTODO: install your-project-name\n", encoding="utf-8")
        return p

    def test_clean_file_exits_zero(self, runner, good):
        r = runner.invoke(main, ["lint", str(good), "--local"])
        assert r.exit_code == 0, r.output
        assert "No issues found" in r.output

    def test_bad_file_exits_one(self, runner, bad):
        r = runner.invoke(main, ["lint", str(bad), "--local"])
        assert r.exit_code == 1
        assert "no-title" in r.output

    def test_fail_on_never_always_exits_zero(self, runner, bad):
        r = runner.invoke(main, ["lint", str(bad), "--local", "--fail-on", "never"])
        assert r.exit_code == 0

    def test_fail_on_warning_is_stricter(self, runner, tmp_path):
        p = tmp_path / "W.md"
        p.write_text("# T\n\n" + "word " * 60, encoding="utf-8")
        assert runner.invoke(main, ["lint", str(p), "--local"]).exit_code == 0
        assert runner.invoke(main, ["lint", str(p), "--local", "--fail-on", "warning"]).exit_code == 1

    def test_json_output(self, runner, bad):
        r = runner.invoke(main, ["lint", str(bad), "--local", "--json", "--fail-on", "never"])
        payload = json.loads(r.output)
        assert payload["counts"]["error"] >= 1
        assert any(f["rule"] == "no-title" for f in payload["findings"])

    def test_missing_file_exits_two(self, runner, tmp_path):
        r = runner.invoke(main, ["lint", str(tmp_path / "absent.md"), "--local"])
        assert r.exit_code == 2

    def test_lints_a_notebook(self, runner, tmp_path):
        nb = {
            "cells": [{"cell_type": "markdown", "source": ["## No title here\n"]}],
            "metadata": {},
            "nbformat": 4,
            "nbformat_minor": 5,
        }
        p = tmp_path / "n.ipynb"
        p.write_text(json.dumps(nb), encoding="utf-8")
        r = runner.invoke(main, ["lint", str(p), "--fail-on", "never"])
        assert r.exit_code == 0
        assert "no-title" in r.output

    def test_does_not_touch_the_network_by_default(self, runner, good, monkeypatch):
        import httpx

        def explode(*a, **k):
            raise AssertionError("lint made a network call without --check-links")

        monkeypatch.setattr(httpx, "Client", explode)
        assert runner.invoke(main, ["lint", str(good), "--local"]).exit_code == 0


# ---------------------------------------------------------------------------
# The project's own README should stay clean
# ---------------------------------------------------------------------------


def test_opendocs_readme_has_no_errors():
    """Dogfooding: our own README must pass the gate we ship."""
    from pathlib import Path

    readme = Path(__file__).resolve().parent.parent / "README.md"
    report = lint_document(parse(readme.read_text(encoding="utf-8")))
    assert report.errors == [], [f.message for f in report.errors]
    assert report.exit_code() == 0
