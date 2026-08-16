"""A documentation quality gate.

``opendocs lint`` checks a README (or any Markdown/Notebook source) against a
set of rules and reports findings with severities, so CI can fail a build when
documentation regresses.

Every rule works on the parsed :class:`~opendocs.core.models.DocumentModel`,
which means the linter sees exactly what the generators see.  Link checking is
opt-in because it is the only rule that touches the network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Optional

from .models import (
    CodeBlock,
    DocumentModel,
    HeadingBlock,
    ImageBlock,
    ListBlock,
    ParagraphBlock,
    TableBlock,
)


class Severity(str, Enum):
    """How seriously to take a finding."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"

    @property
    def rank(self) -> int:
        return {"error": 3, "warning": 2, "info": 1}[self.value]


@dataclass(frozen=True)
class Finding:
    """A single lint result."""

    rule: str
    severity: Severity
    message: str
    context: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity.value,
            "message": self.message,
            "context": self.context,
        }


@dataclass
class LintReport:
    """The result of a lint run."""

    findings: list[Finding] = field(default_factory=list)
    checked: int = 0

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.WARNING]

    @property
    def infos(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.INFO]

    def counts(self) -> dict[str, int]:
        return {
            "error": len(self.errors),
            "warning": len(self.warnings),
            "info": len(self.infos),
        }

    def exit_code(self, *, fail_on: Severity = Severity.ERROR) -> int:
        """Return 0 when nothing at or above *fail_on* was found, else 1."""
        return 1 if any(f.severity.rank >= fail_on.rank for f in self.findings) else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "checks_run": self.checked,
            "counts": self.counts(),
            "findings": [f.to_dict() for f in self.findings],
        }


# ---------------------------------------------------------------------------
# Rule helpers
# ---------------------------------------------------------------------------

#: Sections a README is generally expected to have.  Matched loosely, since
#: projects name them in many ways ("Setup" vs "Installation").
_EXPECTED_SECTIONS: dict[str, tuple[str, ...]] = {
    "installation": ("install", "setup", "getting started", "quick start", "quickstart"),
    "usage": ("usage", "example", "how to use", "quick start", "quickstart", "api"),
    "license": ("license", "licence", "licensing"),
}

_URL_RE = re.compile(r"https?://[^\s<>\)\]\"']+")
_TODO_RE = re.compile(r"\b(TODO|FIXME|XXX|TBD|WIP|COMING SOON)\b", re.IGNORECASE)
_PLACEHOLDER_RE = re.compile(
    r"(lorem ipsum|your-project-name|yourusername|<your[- ]|example\.com/your|CHANGEME|REPLACE_ME)",
    re.IGNORECASE,
)
#: Badge hosts — these links are decorative and change constantly, so they are
#: excluded from link checking to keep the signal useful.
_BADGE_HOSTS = (
    "img.shields.io",
    "badge.fury.io",
    "badgen.net",
    "codecov.io",
    "coveralls.io",
    "travis-ci",
    "circleci.com",
    "appveyor.com",
)


def _all_text(doc: DocumentModel) -> str:
    """Concatenate every piece of prose in the document."""
    parts: list[str] = []
    for block in doc.all_blocks:
        if isinstance(block, (ParagraphBlock, HeadingBlock)):
            parts.append(getattr(block, "text", ""))
        elif isinstance(block, ListBlock):
            parts.extend(block.items)
        elif isinstance(block, TableBlock):
            for row in block.rows:
                parts.extend(row)
    return "\n".join(parts)


_FENCE_RE = re.compile(r"^\s*```.*?^\s*```", re.MULTILINE | re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")


def _prose_only(doc: DocumentModel) -> str:
    """Return document text with all code removed.

    An unfinished marker only means something in running prose.  A README that
    *documents* markers — "`TODO` and `FIXME` are reported" — writes them as
    code, and flagging that is a false positive, so both fenced blocks and
    inline spans are stripped.
    """
    source = doc.raw_markdown
    if not source:
        return _all_text(doc)
    return _INLINE_CODE_RE.sub(" ", _FENCE_RE.sub(" ", source))


def _prose_and_blocks(doc: DocumentModel) -> str:
    """Return document text with only inline code spans removed.

    Placeholders are treated the other way round from markers: one left inside
    a fenced block is *worse* than one in prose, because that is the command a
    reader copies and runs.  Only inline spans are stripped, since those are
    how documentation refers to a placeholder rather than shipping one.
    """
    source = doc.raw_markdown
    if not source:
        return _all_text(doc)
    return _INLINE_CODE_RE.sub(" ", source)


def _headings(doc: DocumentModel) -> list[HeadingBlock]:
    return [b for b in doc.all_blocks if isinstance(b, HeadingBlock)]


def collect_links(doc: DocumentModel) -> list[str]:
    """Return every external URL referenced by the document, deduplicated."""
    seen: dict[str, None] = {}

    def add(url: str) -> None:
        url = url.rstrip(".,;:")
        if url.startswith("http"):
            seen.setdefault(url, None)

    for block in doc.all_blocks:
        if isinstance(block, ImageBlock):
            add(block.src)
        spans = getattr(block, "spans", None) or []
        for span in spans:
            if span.url:
                add(span.url)
        for item_spans in getattr(block, "rich_items", None) or []:
            for span in item_spans:
                if span.url:
                    add(span.url)
        for row in getattr(block, "rich_rows", None) or []:
            for cell in row:
                for span in cell:
                    if span.url:
                        add(span.url)

    # Bare URLs in prose that the parser did not turn into links.
    for match in _URL_RE.finditer(doc.raw_markdown or _all_text(doc)):
        add(match.group(0))

    return list(seen)


def is_badge(url: str) -> bool:
    """Whether *url* points at a decorative badge rather than real content."""
    return any(host in url.lower() for host in _BADGE_HOSTS)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

Rule = Callable[[DocumentModel], Iterable[Finding]]


def rule_has_title(doc: DocumentModel) -> Iterable[Finding]:
    """The document should open with a level-1 heading."""
    headings = _headings(doc)
    if not headings:
        yield Finding("no-title", Severity.ERROR, "Document has no headings at all")
        return
    if not any(h.level == 1 for h in headings):
        yield Finding(
            "no-title",
            Severity.ERROR,
            "Document has no level-1 heading to use as a title",
            context=f"first heading is H{headings[0].level}: {headings[0].text!r}",
        )


def rule_has_description(doc: DocumentModel) -> Iterable[Finding]:
    """There should be prose, not just headings and code."""
    paragraphs = [b for b in doc.all_blocks if isinstance(b, ParagraphBlock) and b.text.strip()]
    if not paragraphs:
        yield Finding("no-description", Severity.ERROR, "Document contains no prose paragraphs")
    elif len(_all_text(doc).split()) < 50:
        yield Finding(
            "thin-content",
            Severity.WARNING,
            f"Document is very short ({len(_all_text(doc).split())} words)",
        )


def rule_expected_sections(doc: DocumentModel) -> Iterable[Finding]:
    """Warn when a conventional README section is missing."""
    titles = " ".join(h.text.lower() for h in _headings(doc))
    for name, aliases in _EXPECTED_SECTIONS.items():
        if not any(alias in titles for alias in aliases):
            yield Finding(
                f"missing-{name}",
                Severity.WARNING,
                f"No '{name}' section found",
                context=f"looked for any of: {', '.join(aliases)}",
            )


def rule_heading_hierarchy(doc: DocumentModel) -> Iterable[Finding]:
    """Heading levels should not jump (H2 -> H4)."""
    previous = 0
    for heading in _headings(doc):
        if previous and heading.level > previous + 1:
            yield Finding(
                "heading-jump",
                Severity.INFO,
                f"Heading level jumps from H{previous} to H{heading.level}",
                context=heading.text[:80],
            )
        previous = heading.level


def rule_duplicate_headings(doc: DocumentModel) -> Iterable[Finding]:
    """Repeated headings at the same level make navigation ambiguous."""
    seen: dict[tuple[int, str], int] = {}
    for heading in _headings(doc):
        key = (heading.level, heading.text.strip().lower())
        seen[key] = seen.get(key, 0) + 1
    for (level, text), count in seen.items():
        if count > 1 and text:
            yield Finding(
                "duplicate-heading",
                Severity.INFO,
                f"Heading appears {count} times at H{level}",
                context=text[:80],
            )


def rule_unlabelled_code(doc: DocumentModel) -> Iterable[Finding]:
    """Fenced code without a language loses syntax highlighting everywhere."""
    unlabelled = [b for b in doc.all_blocks if isinstance(b, CodeBlock) and not b.language.strip()]
    if unlabelled:
        yield Finding(
            "unlabelled-code",
            Severity.INFO,
            f"{len(unlabelled)} code block(s) have no language annotation",
            context=unlabelled[0].code.strip().splitlines()[0][:80] if unlabelled[0].code.strip() else "",
        )


def rule_images_have_alt_text(doc: DocumentModel) -> Iterable[Finding]:
    """Images without alt text are inaccessible."""
    for block in doc.all_blocks:
        if isinstance(block, ImageBlock) and not block.alt.strip() and not is_badge(block.src):
            yield Finding(
                "image-no-alt",
                Severity.WARNING,
                "Image has no alt text",
                context=block.src[:100],
            )


def rule_todo_markers(doc: DocumentModel) -> Iterable[Finding]:
    """Unfinished markers left in published documentation."""
    prose = _prose_only(doc)
    for match in _TODO_RE.finditer(prose):
        yield Finding(
            "todo-marker",
            Severity.WARNING,
            f"Unfinished marker in documentation: {match.group(0)}",
            context=_snippet(prose, match.start()),
        )


def rule_placeholders(doc: DocumentModel) -> Iterable[Finding]:
    """Template placeholders that were never filled in."""
    text = _prose_and_blocks(doc)
    for match in _PLACEHOLDER_RE.finditer(text):
        yield Finding(
            "placeholder",
            Severity.ERROR,
            f"Unreplaced template placeholder: {match.group(0)!r}",
            context=_snippet(text, match.start()),
        )


def rule_table_shape(doc: DocumentModel) -> Iterable[Finding]:
    """Find pipe tables whose rows do not match their header width.

    A ragged table is worse than it looks: the Markdown parser rejects the
    whole block, so it silently renders as plain text in every output format
    rather than as a table.  Because the parser drops it, this rule has to
    work from the raw source rather than the parsed blocks.
    """
    source = doc.raw_markdown
    if not source:
        return

    def cells(line: str) -> list[str]:
        stripped = line.strip()
        if stripped.startswith("|"):
            stripped = stripped[1:]
        if stripped.endswith("|"):
            stripped = stripped[:-1]
        return [c.strip() for c in stripped.split("|")]

    lines = source.splitlines()
    in_fence = False
    block: list[tuple[int, str]] = []

    def flush(rows: list[tuple[int, str]]) -> Iterable[Finding]:
        # A table needs a header, a separator, and at least one body row.
        if len(rows) < 3:
            return
        separator = rows[1][1]
        if not re.fullmatch(r"\|?[\s:\-|]+\|?", separator.strip()) or "-" not in separator:
            return
        width = len(cells(rows[0][1]))
        for lineno, raw in rows[2:]:
            found = len(cells(raw))
            if found != width:
                yield Finding(
                    "ragged-table",
                    Severity.WARNING,
                    f"Table row has {found} cells but the header has {width}; the table will not render as a table",
                    context=f"line {lineno}: {raw.strip()[:70]}",
                )
                return  # one finding per table is enough

    for i, line in enumerate(lines, 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if "|" in line and line.strip():
            block.append((i, line))
        else:
            yield from flush(block)
            block = []
    yield from flush(block)


def _snippet(text: str, position: int, width: int = 60) -> str:
    start = max(0, position - width // 2)
    return text[start : start + width].replace("\n", " ").strip()


#: Rules that never touch the network.
OFFLINE_RULES: tuple[Rule, ...] = (
    rule_has_title,
    rule_has_description,
    rule_expected_sections,
    rule_heading_hierarchy,
    rule_duplicate_headings,
    rule_unlabelled_code,
    rule_images_have_alt_text,
    rule_todo_markers,
    rule_placeholders,
    rule_table_shape,
)


# ---------------------------------------------------------------------------
# Link checking (opt-in, network)
# ---------------------------------------------------------------------------


def check_links(
    doc: DocumentModel,
    *,
    timeout: float = 10.0,
    include_badges: bool = False,
) -> list[Finding]:
    """Request every external link and report the ones that fail.

    This is the only rule that uses the network, so it is never part of
    :data:`OFFLINE_RULES` and must be requested explicitly.
    """
    import httpx

    findings: list[Finding] = []
    urls = [u for u in collect_links(doc) if include_badges or not is_badge(u)]
    if not urls:
        return findings

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        for url in urls:
            try:
                # HEAD first; some hosts reject it, so fall back to GET.
                response = client.head(url)
                if response.status_code >= 400:
                    response = client.get(url)
            except Exception as exc:
                findings.append(
                    Finding("dead-link", Severity.ERROR, f"Link could not be reached: {type(exc).__name__}", url)
                )
                continue

            if response.status_code >= 500:
                findings.append(Finding("dead-link", Severity.WARNING, f"Server error {response.status_code}", url))
            elif response.status_code >= 400:
                findings.append(Finding("dead-link", Severity.ERROR, f"HTTP {response.status_code}", url))
    return findings


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def lint_document(
    doc: DocumentModel,
    *,
    rules: Optional[Iterable[Rule]] = None,
    check_links_too: bool = False,
    link_timeout: float = 10.0,
    include_badges: bool = False,
) -> LintReport:
    """Run the lint rules over *doc* and return a report."""
    active = tuple(rules) if rules is not None else OFFLINE_RULES
    report = LintReport(checked=len(active) + (1 if check_links_too else 0))

    for rule in active:
        try:
            report.findings.extend(rule(doc))
        except Exception as exc:  # a broken rule must not fail the whole run
            report.findings.append(
                Finding(
                    getattr(rule, "__name__", "rule"),
                    Severity.INFO,
                    f"Rule raised {type(exc).__name__}: {exc}",
                )
            )

    if check_links_too:
        report.findings.extend(check_links(doc, timeout=link_timeout, include_badges=include_badges))

    # Most severe first, then by rule for stable output.
    report.findings.sort(key=lambda f: (-f.severity.rank, f.rule, f.context))
    return report
