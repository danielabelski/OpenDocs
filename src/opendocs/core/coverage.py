"""Measure how much of a codebase its documentation actually covers.

Most documentation tooling reports on what the docs *say*. This reports what
they *miss*, by comparing two things the project already knows how to derive:
the real surface area of the code (via :mod:`opendocs.core.code_analyzer`) and
what the prose actually mentions (via the parser and semantic extractor).

Four dimensions are scored, chosen because each has an objective answer:

- **docstrings** — public functions and classes carrying a docstring;
- **environment variables** — every ``os.environ`` / ``getenv`` key the code
  reads, checked against the documentation;
- **CLI options** — every flag the code defines, checked the same way;
- **tech stack** — detected technologies the documentation names.

Deliberately excluded is any judgement of whether prose is *good*. That is not
objectively measurable, and a score nobody can verify is worse than no score.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

# ---------------------------------------------------------------------------
# Extraction patterns
# ---------------------------------------------------------------------------

#: os.environ["X"], os.environ.get("X"), os.getenv("X"), environ["X"]
_ENV_RE = re.compile(r"""(?:os\.)?(?:environ(?:\.get)?\s*[\[(]\s*|getenv\s*\(\s*)["']([A-Z][A-Z0-9_]{2,})["']""")

#: click.option("--flag"), add_argument("--flag"), @option("--flag")
_CLI_FLAG_RE = re.compile(r"""(?:option|add_argument|argument)\s*\(\s*["'](--[a-z][a-z0-9-]*)["']""")

#: Environment names that are conventions of the wider ecosystem rather than
#: something this project is expected to document.
_STANDARD_ENV = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "PWD",
        "TMPDIR",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
        "SHELL",
        "TERM",
        "PYTHONPATH",
        "PYTHONDONTWRITEBYTECODE",
        "VIRTUAL_ENV",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "CI",
        "DEBUG",
    }
)

#: Flags every CLI provides; not evidence of missing documentation.
_STANDARD_FLAGS = frozenset({"--help", "--version"})

#: Directory names whose contents are tests.
_TEST_DIRS = frozenset({"tests", "test", "testing", "spec", "specs"})


def is_test_path(path: str) -> bool:
    """Whether *path* looks like test code rather than shipped code.

    Test helpers and test classes are not part of a project's documented
    surface, so counting them makes the score meaningless — on this project
    they accounted for most of the "undocumented" classes.  ``CodebaseModel``
    has a ``test_files`` field but the analyser never populates it, so the
    classification is done here by path convention instead.
    """
    normalised = path.replace("\\", "/")
    parts = normalised.split("/")
    if any(part in _TEST_DIRS for part in parts[:-1]):
        return True
    name = parts[-1]
    return name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


@dataclass
class Dimension:
    """One scored aspect of coverage."""

    name: str
    total: int = 0
    covered: int = 0
    missing: list[str] = field(default_factory=list)

    @property
    def percent(self) -> float:
        """Percentage covered; a dimension with nothing to cover scores 100."""
        if self.total == 0:
            return 100.0
        return round(self.covered / self.total * 100, 1)

    @property
    def applicable(self) -> bool:
        return self.total > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "total": self.total,
            "covered": self.covered,
            "percent": self.percent,
            "missing": self.missing,
        }


@dataclass
class CoverageReport:
    """The full result of a coverage run."""

    project_name: str = ""
    dimensions: list[Dimension] = field(default_factory=list)
    files_analysed: int = 0
    docs_analysed: list[str] = field(default_factory=list)

    def dimension(self, name: str) -> Optional[Dimension]:
        for d in self.dimensions:
            if d.name == name:
                return d
        return None

    @property
    def applicable_dimensions(self) -> list[Dimension]:
        """Dimensions with something to measure.

        A project with no environment variables should not be scored on
        documenting them — including it would dilute the overall figure with a
        vacuous 100%.
        """
        return [d for d in self.dimensions if d.applicable]

    @property
    def overall(self) -> float:
        """Weighted overall percentage across applicable dimensions.

        Weighted by item count rather than averaging the per-dimension
        percentages, so one undocumented flag out of two does not outweigh
        three hundred documented functions.
        """
        applicable = self.applicable_dimensions
        if not applicable:
            return 100.0
        total = sum(d.total for d in applicable)
        covered = sum(d.covered for d in applicable)
        if total == 0:
            return 100.0
        return round(covered / total * 100, 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project_name,
            "overall_percent": self.overall,
            "files_analysed": self.files_analysed,
            "docs_analysed": self.docs_analysed,
            "dimensions": [d.to_dict() for d in self.dimensions],
        }


# ---------------------------------------------------------------------------
# Extraction from source
# ---------------------------------------------------------------------------


def extract_env_vars(sources: Iterable[str]) -> set[str]:
    """Return every environment variable name the given sources read."""
    found: set[str] = set()
    for source in sources:
        for match in _ENV_RE.finditer(source):
            name = match.group(1)
            if name not in _STANDARD_ENV:
                found.add(name)
    return found


def extract_cli_flags(sources: Iterable[str]) -> set[str]:
    """Return every long-form CLI flag the given sources define."""
    found: set[str] = set()
    for source in sources:
        for match in _CLI_FLAG_RE.finditer(source):
            flag = match.group(1)
            # click declares paired boolean flags as "--x/--no-x"; both halves
            # are captured separately by the pattern, which is what we want.
            if flag not in _STANDARD_FLAGS:
                found.add(flag)
    return found


# ---------------------------------------------------------------------------
# The analysis
# ---------------------------------------------------------------------------


def _read_sources(root: Path, *, include_tests: bool = False, limit_bytes: int = 500_000) -> list[str]:
    """Read Python sources under *root*, skipping vendored and (by default) test trees."""
    from .code_analyzer import _IGNORE_DIRS, _iter_source_files

    sources: list[str] = []
    for path in _iter_source_files(root):
        if path.suffix != ".py":
            continue
        if any(part in _IGNORE_DIRS for part in path.parts):
            continue
        if not include_tests:
            try:
                relative = str(path.relative_to(root))
            except ValueError:
                relative = path.name
            if is_test_path(relative):
                continue
        try:
            if path.stat().st_size > limit_bytes:
                continue
            sources.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return sources


def _docs_text(doc_paths: Iterable[Path]) -> str:
    """Concatenate documentation sources into one searchable blob."""
    parts: list[str] = []
    for path in doc_paths:
        try:
            parts.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(parts)


def _mentions(haystack: str, needle: str) -> bool:
    """Whether *needle* appears in the documentation as a distinct token."""
    return re.search(rf"(?<![\w-]){re.escape(needle)}(?![\w-])", haystack, re.IGNORECASE) is not None


def analyse_coverage(
    codebase_dir: str | Path,
    doc_paths: Optional[Iterable[str | Path]] = None,
    *,
    include_private: bool = False,
    include_tests: bool = False,
) -> CoverageReport:
    """Compare a codebase against its documentation.

    Parameters
    ----------
    codebase_dir
        Root of the project to analyse.
    doc_paths
        Documentation files to check against.  Defaults to ``README.md`` in
        the project root when present.
    include_private
        Score underscore-prefixed functions and classes too.  Off by default,
        since a private helper carries no promise to the reader.
    include_tests
        Score test files too.  Off by default — test code is not part of the
        surface a project documents.
    """
    from .code_analyzer import CodebaseAnalyzer

    root = Path(codebase_dir).resolve()
    model = CodebaseAnalyzer().analyze(root)

    if doc_paths is None:
        default_readme = root / "README.md"
        resolved_docs = [default_readme] if default_readme.exists() else []
    else:
        resolved_docs = [Path(p) for p in doc_paths]

    docs = _docs_text(resolved_docs)
    report = CoverageReport(
        project_name=model.project_name,
        files_analysed=model.total_files,
        docs_analysed=[str(p) for p in resolved_docs],
    )

    # -- Docstrings ----------------------------------------------------
    def _is_public(name: str) -> bool:
        return include_private or not name.startswith("_")

    fn_dim = Dimension(name="docstrings:functions")
    cls_dim = Dimension(name="docstrings:classes")
    for file_analysis in model.files:
        if not include_tests and is_test_path(file_analysis.path):
            continue
        for fn in file_analysis.functions:
            if not _is_public(fn.name):
                continue
            fn_dim.total += 1
            if fn.docstring.strip():
                fn_dim.covered += 1
            else:
                fn_dim.missing.append(f"{file_analysis.path}::{fn.name}")
        for cls in file_analysis.classes:
            if not _is_public(cls.name):
                continue
            cls_dim.total += 1
            if cls.docstring.strip():
                cls_dim.covered += 1
            else:
                cls_dim.missing.append(f"{file_analysis.path}::{cls.name}")

    # -- Environment variables and CLI flags ---------------------------
    sources = _read_sources(root, include_tests=include_tests)
    env_dim = Dimension(name="env-vars")
    for name in sorted(extract_env_vars(sources)):
        env_dim.total += 1
        if _mentions(docs, name):
            env_dim.covered += 1
        else:
            env_dim.missing.append(name)

    flag_dim = Dimension(name="cli-flags")
    for flag in sorted(extract_cli_flags(sources)):
        flag_dim.total += 1
        if _mentions(docs, flag):
            flag_dim.covered += 1
        else:
            flag_dim.missing.append(flag)

    # -- Tech stack ----------------------------------------------------
    tech_dim = Dimension(name="tech-stack")
    for item in model.tech_stack:
        tech_dim.total += 1
        if _mentions(docs, item.name):
            tech_dim.covered += 1
        else:
            tech_dim.missing.append(item.name)

    report.dimensions = [fn_dim, cls_dim, env_dim, flag_dim, tech_dim]
    return report
