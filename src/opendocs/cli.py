"""opendocs CLI — generate documentation from GitHub READMEs, Markdown files, and Jupyter Notebooks."""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console

from .core.models import OutputFormat
from .core.template_vars import load_template_vars
from .generators.themes import list_themes
from .pipeline import Pipeline

console = Console()

BANNER = r"""
  ___                   ____
 / _ \ _ __   ___ _ __ |  _ \  ___   ___ ___
| | | | '_ \ / _ \ '_ \| | | |/ _ \ / __/ __|
| |_| | |_) |  __/ | | | |_| | (_) | (__\__ \
 \___/| .__/ \___|_| |_|____/ \___/ \___|___/
      |_|
  README → Docs Pipeline  v0.9.0
"""

FORMAT_MAP = {
    "word": OutputFormat.WORD,
    "pdf": OutputFormat.PDF,
    "pptx": OutputFormat.PPTX,
    "blog": OutputFormat.BLOG,
    "jira": OutputFormat.JIRA,
    "changelog": OutputFormat.CHANGELOG,
    "latex": OutputFormat.LATEX,
    "onepager": OutputFormat.ONEPAGER,
    "social": OutputFormat.SOCIAL,
    "faq": OutputFormat.FAQ,
    "architecture": OutputFormat.ARCHITECTURE,
    "mindmap": OutputFormat.MINDMAP,
    "all": OutputFormat.ALL,
}

# Derived from FORMAT_MAP so the CLI choices can never drift out of sync with
# the formats the pipeline actually knows how to build.
FORMAT_CHOICES = list(FORMAT_MAP)

# Every concrete format produced by `--format all`.
ALL_FORMATS = [fmt for key, fmt in FORMAT_MAP.items() if key != "all"]


@click.group()
@click.version_option(version="0.9.0", prog_name="opendocs")
def main():
    """opendocs — Convert GitHub READMEs, npm packages, Markdown files, and Jupyter Notebooks into multi-format documentation."""
    pass


@main.command()
@click.argument("source", metavar="SOURCE")
@click.option(
    "-f",
    "--format",
    "fmt",
    type=click.Choice(FORMAT_CHOICES, case_sensitive=False),
    default="all",
    help="Output format (default: all).",
)
@click.option(
    "-o",
    "--output",
    "output_dir",
    type=click.Path(),
    default="./output",
    help="Output directory (default: ./output).",
)
@click.option(
    "--local",
    is_flag=True,
    default=False,
    help="Treat SOURCE as a local file path.",
)
@click.option(
    "--token",
    envvar="GITHUB_TOKEN",
    default=None,
    help="GitHub personal access token (or set GITHUB_TOKEN env var).",
)
@click.option(
    "--theme",
    "theme_name",
    type=click.Choice([t.name for t in list_themes()], case_sensitive=False),
    default="corporate",
    help="Color theme for generated documents.",
)
@click.option(
    "--mode",
    type=click.Choice(["basic", "llm", "template"], case_sensitive=False),
    default="basic",
    help="Mode: basic (minimal), template (rich docs, no LLM), or llm (AI-enhanced).",
)
@click.option(
    "--api-key",
    envvar="OPENAI_API_KEY",
    default=None,
    help="OpenAI API key for LLM mode (or set OPENAI_API_KEY env var).",
)
@click.option(
    "--model",
    default="gpt-4o-mini",
    help="LLM model name (default: gpt-4o-mini). Any OpenAI-compatible model.",
)
@click.option(
    "--base-url",
    default=None,
    help="Custom OpenAI-compatible API base URL (e.g. http://localhost:11434/v1 for Ollama).",
)
@click.option(
    "--provider",
    "llm_provider",
    type=click.Choice(["openai", "anthropic", "google", "ollama", "azure"], case_sensitive=False),
    default="openai",
    help="LLM provider: openai (default), anthropic (Claude), google (Gemini), ollama (local), azure.",
)
@click.option(
    "--sort-tables",
    "sort_tables",
    default="smart",
    help="Table sort strategy: smart (auto), alpha, numeric, column:N, column:N:desc, or none.",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True),
    default=None,
    help="Path to YAML/JSON config file with template variables (project_name, author, version, etc.).",
)
@click.option("--project-name", default=None, help="Project name for document titles and headers.")
@click.option("--author", default=None, help="Document author name.")
@click.option("--doc-version", "doc_version", default=None, help="Document / project version string.")
@click.option("--org", "organisation", default=None, help="Organisation name for headers and footers.")
@click.option("--department", default=None, help="Department / team name.")
@click.option(
    "--confidentiality",
    default=None,
    help="Classification label (e.g. Internal, Confidential, Public).",
)
@click.option(
    "--include-outputs/--no-outputs",
    "include_outputs",
    default=True,
    help="Include cell outputs when parsing Jupyter Notebooks (default: yes).",
)
@click.option(
    "--embed-graph-assets",
    is_flag=True,
    default=False,
    help="Inline vis-network into the interactive graph so it renders offline (~700 KB larger).",
)
@click.option(
    "--no-cache",
    "no_cache",
    is_flag=True,
    default=False,
    help="Ignore all caches (generated artifacts and LLM responses) and rebuild from scratch.",
)
@click.option(
    "--cache-dir",
    "cache_dir",
    type=click.Path(),
    default=None,
    help="Where to keep cached build artifacts (default: ~/.cache/opendocs/build).",
)
@click.option(
    "--folder-recursive/--no-folder-recursive",
    "folder_recursive",
    default=True,
    help="When SOURCE is a folder, scan sub-directories too (default: yes).",
)
@click.option(
    "--folder-title",
    default=None,
    help="Override the merged document title when SOURCE is a folder.",
)
# ---- Notion publish --------------------------------------------------
@click.option(
    "--publish-notion",
    "notion_page_id",
    default=None,
    envvar="NOTION_PAGE_ID",
    help="Publish generated docs to this Notion page ID or URL.",
)
@click.option(
    "--notion-token",
    envvar="NOTION_TOKEN",
    default=None,
    help="Notion integration token (or set NOTION_TOKEN env var).",
)
# ---- Confluence publish ----------------------------------------------
@click.option(
    "--publish-confluence",
    "confluence_space",
    default=None,
    envvar="CONFLUENCE_SPACE",
    help="Publish generated docs to this Confluence space key (e.g. PROJ).",
)
@click.option(
    "--confluence-url",
    envvar="CONFLUENCE_URL",
    default=None,
    help="Confluence base URL, e.g. https://yourorg.atlassian.net/wiki",
)
@click.option(
    "--confluence-user",
    envvar="CONFLUENCE_USER",
    default=None,
    help="Confluence account email (or set CONFLUENCE_USER env var).",
)
@click.option(
    "--confluence-token",
    envvar="CONFLUENCE_TOKEN",
    default=None,
    help="Atlassian API token (or set CONFLUENCE_TOKEN env var).",
)
@click.option(
    "--confluence-parent",
    default=None,
    help="Confluence parent page title to nest new page under.",
)
def generate(
    source: str,
    fmt: str,
    output_dir: str,
    local: bool,
    token: str | None,
    theme_name: str,
    mode: str,
    api_key: str | None,
    model: str,
    base_url: str | None,
    llm_provider: str,
    sort_tables: str,
    config_path: str | None,
    project_name: str | None,
    author: str | None,
    doc_version: str | None,
    organisation: str | None,
    department: str | None,
    confidentiality: str | None,
    include_outputs: bool,
    embed_graph_assets: bool,
    no_cache: bool,
    cache_dir: str | None,
    folder_recursive: bool,
    folder_title: str | None,
    notion_page_id: str | None,
    notion_token: str | None,
    confluence_space: str | None,
    confluence_url: str | None,
    confluence_user: str | None,
    confluence_token: str | None,
    confluence_parent: str | None,
):
    """Generate documentation from a GitHub README, npm package, local Markdown file,
    Jupyter Notebook, or an entire folder of .md/.ipynb files.

    SOURCE can be:
      - A GitHub URL        (e.g., https://github.com/owner/repo)
      - An npm package      (e.g., npm:axios  or  npm:@scope/pkg)
      - A local file/notebook  (use --local flag)
      - A local folder path — all .md/.ipynb files will be merged
    """
    console.print(BANNER)

    # Resolve template variables (config file + CLI overrides)
    tvars = load_template_vars(
        config_path,
        project_name=project_name,
        author=author,
        version=doc_version,
        organisation=organisation,
        department=department,
        confidentiality=confidentiality,
    )

    # Auto-detect notebooks
    from .core.notebook_parser import is_notebook

    if is_notebook(source) and not local:
        local = True  # Notebooks are always local files

    # Resolve formats
    chosen = FORMAT_MAP[fmt.lower()]
    if chosen == OutputFormat.ALL:
        formats = list(ALL_FORMATS)
    else:
        formats = [chosen]

    # Run pipeline — folder path or single file/URL
    from .core.build_cache import BuildCache
    from .llm.cache import LLMCache, reset_shared_cache

    build_cache = BuildCache(cache_dir, enabled=not no_cache)
    # --no-cache means "don't reuse anything", so it covers LLM responses too.
    reset_shared_cache(LLMCache(Path(cache_dir) / "llm" if cache_dir else None, enabled=not no_cache))

    pipeline = Pipeline(github_token=token)
    source_path = Path(source)

    if source_path.is_dir():
        # Multi-file folder mode
        result = pipeline.run_folder(
            source_path,
            output_dir=output_dir,
            formats=formats,
            title=folder_title,
            recursive=folder_recursive,
            theme_name=theme_name,
            mode=mode,
            api_key=api_key,
            model=model,
            base_url=base_url,
            sort_tables=sort_tables,
            provider=llm_provider,
            template_vars=tvars,
            embed_graph_assets=embed_graph_assets,
            cache=build_cache,
        )
    else:
        result = pipeline.run(
            source,
            output_dir=output_dir,
            formats=formats,
            local=local,
            theme_name=theme_name,
            mode=mode,
            api_key=api_key,
            model=model,
            base_url=base_url,
            sort_tables=sort_tables,
            provider=llm_provider,
            template_vars=tvars,
            include_outputs=include_outputs,
            embed_graph_assets=embed_graph_assets,
            cache=build_cache,
        )

    # ---- AI Reader files summary ------------------------------------
    if result.ai_reader_files:
        console.print("\n[bold cyan]AI Reader Files Generated:[/]")
        for af in result.ai_reader_files:
            console.print(f"  [green]✓[/] {af.name}")

    # ---- Post-generation publishing ------------------------------------
    # Find the best Markdown file to publish (blog_post.md preferred)
    def _find_markdown_output() -> Path | None:
        md_candidates = [r.output_path for r in result.results if r.success and r.output_path.suffix == ".md"]
        # Prefer blog_post over analysis_report over any other .md
        for candidate in md_candidates:
            if "blog" in candidate.stem.lower():
                return candidate
        return md_candidates[0] if md_candidates else None

    if notion_page_id:
        if not notion_token:
            console.print(
                "[yellow]WARNING: --publish-notion requires --notion-token (or NOTION_TOKEN env var). Skipping.[/]"
            )
        else:
            md_file = _find_markdown_output()
            if not md_file:
                console.print("[yellow]WARNING: No Markdown output found to publish to Notion.[/]")
            else:
                try:
                    from .publishers import NotionPublisher

                    console.print("[bold blue]Publishing to Notion...[/]")
                    pub = NotionPublisher(token=notion_token, page_id=notion_page_id)
                    url = pub.publish(md_file)
                    console.print(f"[green][OK][/] Notion page created → {url}")
                except ImportError:
                    console.print("[red]notion-client not installed. Run: pip install opendocs[publish][/]")
                except Exception as exc:
                    console.print(f"[red]Notion publish failed: {exc}[/]")

    if confluence_space:
        missing = [
            n
            for n, v in [
                ("--confluence-url", confluence_url),
                ("--confluence-user", confluence_user),
                ("--confluence-token", confluence_token),
            ]
            if not v
        ]
        if missing:
            console.print(f"[yellow]WARNING: Confluence publish requires {', '.join(missing)}. Skipping.[/]")
        else:
            md_file = _find_markdown_output()
            if not md_file:
                console.print("[yellow]WARNING: No Markdown output found to publish to Confluence.[/]")
            else:
                try:
                    from .publishers import ConfluencePublisher

                    console.print("[bold blue]Publishing to Confluence...[/]")
                    pub = ConfluencePublisher(
                        url=confluence_url,
                        username=confluence_user,
                        token=confluence_token,
                        space_key=confluence_space,
                        parent_page_title=confluence_parent,
                    )
                    url = pub.publish(md_file)
                    console.print(f"[green][OK][/] Confluence page created/updated → {url}")
                except ImportError:
                    console.print("[red]requests not installed. Run: pip install opendocs[publish][/]")
                except Exception as exc:
                    console.print(f"[red]Confluence publish failed: {exc}[/]")

    # Exit code
    if not any(r.success for r in result.results):
        raise SystemExit(1)


@main.command()
def themes():
    """List available document themes."""
    from rich.table import Table as RichTable

    table = RichTable(title="Available Themes", show_lines=False)
    table.add_column("Name", style="bold cyan")
    table.add_column("Primary Color", style="bold")
    table.add_column("Accent Color", style="bold")
    table.add_column("Heading Font")
    table.add_column("Body Font")

    for t in list_themes():
        p = t.colors.primary
        a = t.colors.accent
        p_hex = f"#{p[0]:02X}{p[1]:02X}{p[2]:02X}"
        a_hex = f"#{a[0]:02X}{a[1]:02X}{a[2]:02X}"
        table.add_row(
            t.name,
            f"[{p_hex}]██ {p_hex}[/]",
            f"[{a_hex}]██ {a_hex}[/]",
            t.fonts.heading,
            t.fonts.body,
        )

    console.print(table)


@main.command()
@click.argument("source")
@click.option("--local", is_flag=True, default=False, help="Treat SOURCE as a local file.")
@click.option("--token", envvar="GITHUB_TOKEN", default=None)
def inspect(source: str, local: bool, token: str | None):
    """Fetch and parse a README or Jupyter Notebook, then display the structured representation."""
    from rich.tree import Tree

    from .core.fetcher import ReadmeFetcher
    from .core.notebook_parser import NotebookParser, is_notebook
    from .core.parser import ReadmeParser

    if is_notebook(source):
        name = Path(source).stem
        parser = NotebookParser()
        doc = parser.parse(source, repo_name=name)
    else:
        fetcher = ReadmeFetcher(github_token=token)
        if local:
            content, name = fetcher._fetch_local(source)
        else:
            content, name = fetcher.fetch(source)

        parser = ReadmeParser()
        doc = parser.parse(content, repo_name=name, repo_url=source if not local else "")

    tree = Tree(f"[bold]{name}[/bold]")
    tree.add(f"[dim]Blocks: {len(doc.all_blocks)}[/dim]")
    tree.add(f"[dim]Diagrams: {len(doc.mermaid_diagrams)}[/dim]")

    sections_node = tree.add("[bold]Sections[/bold]")
    for sec in doc.sections:
        _add_section_tree(sections_node, sec)

    console.print(tree)


@main.command()
@click.argument("repo_dir", type=click.Path(exists=True))
@click.option(
    "-o",
    "--output",
    "output_dir",
    default="./output",
    help="Output directory for generated docs (default: ./output).",
)
@click.option(
    "--interval",
    type=int,
    default=30,
    help="Seconds between change-detection checks (default: 30).",
)
@click.option(
    "--once",
    is_flag=True,
    default=False,
    help="Run a single check-and-regenerate cycle, then exit (for cron).",
)
@click.option(
    "--auto-pr",
    is_flag=True,
    default=False,
    help="Automatically create a git branch + pull request when docs are regenerated.",
)
@click.option(
    "--branch",
    "branch_name",
    default="docs/auto-update",
    help="Branch-name prefix for auto-PR; a UTC timestamp is appended (default: docs/auto-update).",
)
@click.option(
    "--patterns",
    default=None,
    help="Comma-separated file patterns to watch (e.g. 'README.md,docs/*.md,*.ipynb').",
)
@click.option(
    "-f",
    "--format",
    "fmt",
    type=click.Choice(FORMAT_CHOICES, case_sensitive=False),
    default="all",
    help="Output format (default: all).",
)
@click.option(
    "--theme",
    "theme_name",
    type=click.Choice([t.name for t in list_themes()], case_sensitive=False),
    default="corporate",
    help="Color theme for generated documents.",
)
@click.option(
    "--mode",
    type=click.Choice(["basic", "llm", "template"], case_sensitive=False),
    default="basic",
    help="Mode: basic (minimal), template (rich docs, no LLM), or llm (AI-enhanced).",
)
@click.option("--api-key", envvar="OPENAI_API_KEY", default=None)
@click.option("--model", default="gpt-4o-mini")
@click.option(
    "--provider",
    "llm_provider",
    type=click.Choice(["openai", "anthropic", "google", "ollama", "azure"], case_sensitive=False),
    default="openai",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True),
    default=None,
    help="Template variables config file.",
)
def watch(
    repo_dir: str,
    output_dir: str,
    interval: int,
    once: bool,
    auto_pr: bool,
    branch_name: str,
    patterns: str | None,
    fmt: str,
    theme_name: str,
    mode: str,
    api_key: str | None,
    model: str,
    llm_provider: str,
    config_path: str | None,
):
    """Watch a repository for changes and auto-regenerate documentation.

    REPO_DIR is the path to a local git repository to monitor.

    \b
    Examples:
      opendocs watch ./my-repo                    # continuous watch
      opendocs watch ./my-repo --once             # one-shot (for cron)
      opendocs watch ./my-repo --auto-pr          # watch + auto pull requests
      opendocs watch ./my-repo --interval 60      # check every 60 seconds
      opendocs watch ./my-repo --patterns "README.md,docs/*.md"
    """
    console.print(BANNER)

    from .core.watcher import FileWatcher

    # Parse patterns
    pattern_list = None
    if patterns:
        pattern_list = [p.strip() for p in patterns.split(",") if p.strip()]

    # Parse formats
    fmt_list = None
    if fmt.lower() != "all":
        fmt_list = [fmt.lower()]

    watcher = FileWatcher(
        repo_dir=repo_dir,
        output_dir=output_dir,
        interval=interval,
        patterns=pattern_list,
        auto_pr=auto_pr,
        branch_name=branch_name,
        formats=fmt_list,
        theme=theme_name,
        mode=mode,
        api_key=api_key,
        model=model,
        provider=llm_provider,
        config_path=config_path,
    )

    if once:
        changed = watcher.check_once()
        if not changed:
            console.print("[dim]No changes detected. Nothing to regenerate.[/]")
        raise SystemExit(0 if changed else 0)  # Success either way for cron
    else:
        watcher.watch()


def _add_section_tree(parent, section):
    """Recursively add sections to a Rich tree."""
    node = parent.add(f"[blue]H{section.level}:[/blue] {section.title} [dim]({len(section.blocks)} blocks)[/dim]")
    for sub in section.subsections:
        _add_section_tree(node, sub)


@main.command()
@click.argument("codebase_dir", type=click.Path(exists=True))
@click.option(
    "-f",
    "--format",
    "fmt",
    type=click.Choice(FORMAT_CHOICES, case_sensitive=False),
    default="all",
    help="Output format (default: all).",
)
@click.option(
    "-o",
    "--output",
    "output_dir",
    type=click.Path(),
    default="./output",
    help="Output directory (default: ./output).",
)
@click.option(
    "--theme",
    "theme_name",
    type=click.Choice([t.name for t in list_themes()], case_sensitive=False),
    default="corporate",
    help="Color theme for generated documents.",
)
@click.option(
    "--mode",
    type=click.Choice(["basic", "llm", "template"], case_sensitive=False),
    default="template",
    help="Mode: basic (minimal), template (rich docs, no LLM), or llm (AI-enhanced).",
)
@click.option(
    "--api-key",
    envvar="OPENAI_API_KEY",
    default=None,
    help="OpenAI API key for LLM mode (or set OPENAI_API_KEY env var).",
)
@click.option(
    "--model",
    default="gpt-4o-mini",
    help="LLM model name (default: gpt-4o-mini).",
)
@click.option(
    "--base-url",
    default=None,
    help="Custom OpenAI-compatible API base URL.",
)
@click.option(
    "--provider",
    "llm_provider",
    type=click.Choice(["openai", "anthropic", "google", "ollama", "azure", "slm"], case_sensitive=False),
    default="openai",
    help="LLM provider. Use 'slm' for local Phi-3.5-mini model.",
)
@click.option(
    "--sort-tables",
    "sort_tables",
    default="smart",
    help="Table sort strategy: smart (auto), alpha, numeric, column:N, column:N:desc, or none.",
)
@click.option(
    "--adapter-path",
    "adapter_path",
    default=None,
    help="Path to a fine-tuned LoRA adapter directory (for --provider slm).",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True),
    default=None,
    help="Path to YAML/JSON config file with template variables.",
)
@click.option("--project-name", default=None, help="Project name for document titles and headers.")
@click.option("--author", default=None, help="Document author name.")
@click.option("--doc-version", "doc_version", default=None, help="Document / project version string.")
@click.option("--org", "organisation", default=None, help="Organisation name for headers and footers.")
def codebase(
    codebase_dir: str,
    fmt: str,
    output_dir: str,
    theme_name: str,
    mode: str,
    api_key: str | None,
    model: str,
    base_url: str | None,
    llm_provider: str,
    sort_tables: str,
    adapter_path: str | None,
    config_path: str | None,
    project_name: str | None,
    author: str | None,
    doc_version: str | None,
    organisation: str | None,
):
    """Analyze a codebase directory and generate documentation from source code.

    Unlike the 'generate' command which requires a README or Markdown file,
    this command walks the actual source code in CODEBASE_DIR, extracts
    structure, tech stack, architecture, classes, functions, and
    dependencies, then generates comprehensive documentation.

    The default mode is 'template' which generates rich documentation with
    architecture diagrams, pie charts, risk assessment, and data-driven
    prose — entirely from code analysis, no LLM required.

    Use --mode llm (or --provider slm) for AI-enhanced narrative prose.

    \b
    Examples:
      opendocs codebase ./my-project                              # rich template docs (default)
      opendocs codebase ./my-project -f word                      # just Word doc
      opendocs codebase ./my-project --mode basic                 # minimal report
      opendocs codebase ./my-project --mode llm --provider slm    # local AI model
      opendocs codebase ./my-project --theme ocean                # with theme
    """
    console.print(BANNER)

    # Resolve template variables
    tvars = load_template_vars(
        config_path,
        project_name=project_name,
        author=author,
        version=doc_version,
        organisation=organisation,
    )

    # Resolve formats
    chosen = FORMAT_MAP[fmt.lower()]
    if chosen == OutputFormat.ALL:
        formats = list(ALL_FORMATS)
    else:
        formats = [chosen]

    pipeline = Pipeline()

    # When using SLM provider with basic mode, auto-switch to LLM mode.
    # Template mode is the recommended no-LLM approach.
    effective_mode = mode
    if llm_provider == "slm" and mode == "basic":
        effective_mode = "llm"

    result = pipeline.run_codebase(
        codebase_dir,
        output_dir=output_dir,
        formats=formats,
        theme_name=theme_name,
        mode=effective_mode,
        api_key=api_key,
        model=model,
        base_url=base_url,
        sort_tables=sort_tables,
        provider=llm_provider,
        adapter_path=adapter_path,
        template_vars=tvars,
    )

    if not any(r.success for r in result.results):
        raise SystemExit(1)


# ─── SLM commands ────────────────────────────────────────────────────────


@main.command("download-model")
@click.option(
    "--model",
    default="microsoft/Phi-3.5-mini-instruct",
    help="Hugging Face model ID to download.",
)
@click.option(
    "--cache-dir",
    default=None,
    help="Directory to cache the model (default: ~/.cache/opendocs/models).",
)
def download_model(model: str, cache_dir: str | None):
    """Pre-download an SLM model so the first inference is fast.

    \b
    Examples:
      opendocs download-model
      opendocs download-model --model microsoft/Phi-3.5-mini-instruct
    """
    console.print(BANNER)
    console.print(f"[bold blue]Downloading model:[/] {model}")

    try:
        from .llm.slm_provider import SLMProvider

        path = SLMProvider.download_model(model, cache_dir=cache_dir)
        console.print(f"[green][OK][/] Model downloaded to: {path}")
    except ImportError:
        console.print("[bold red]SLM dependencies not installed.[/]\nRun: pip install opendocs[slm]")
        raise SystemExit(1)
    except Exception as exc:
        console.print(f"[bold red]Download failed:[/] {exc}")
        raise SystemExit(1)


@main.command("finetune")
@click.argument("codebase_dir", type=click.Path(exists=True))
@click.option(
    "--reference-doc",
    "reference_doc",
    type=click.Path(exists=True),
    default=None,
    help="Reference .docx or .md file as the target documentation style.",
)
@click.option(
    "--output-dir",
    "-o",
    default="./opendocs-adapter",
    help="Directory to save the trained LoRA adapter.",
)
@click.option(
    "--base-model",
    default="microsoft/Phi-3.5-mini-instruct",
    help="Hugging Face base model ID.",
)
@click.option("--epochs", default=3, help="Number of training epochs.")
@click.option("--batch-size", default=1, help="Per-device batch size (1 for 6-8 GB VRAM).")
@click.option("--lora-r", default=16, help="LoRA rank.")
@click.option("--lora-alpha", default=32, help="LoRA alpha scaling factor.")
@click.option("--learning-rate", default=2e-4, help="Training learning rate.")
@click.option(
    "--examples-file",
    type=click.Path(exists=True),
    default=None,
    help="JSONL file with additional training examples.",
)
def finetune(
    codebase_dir: str,
    reference_doc: str | None,
    output_dir: str,
    base_model: str,
    epochs: int,
    batch_size: int,
    lora_r: int,
    lora_alpha: int,
    learning_rate: float,
    examples_file: str | None,
):
    """Fine-tune Phi-3.5-mini on codebase-to-documentation examples.

    Analyzes CODEBASE_DIR and (optionally) pairs it with a reference
    document to create training data, then runs QLoRA fine-tuning.

    The resulting adapter (~50 MB) can be loaded with:
      opendocs codebase ./project --provider slm --adapter-path ./opendocs-adapter/adapter

    \b
    Examples:
      opendocs finetune ./my-project --reference-doc ./my-doc.docx
      opendocs finetune ./my-project -o ./my-adapter --epochs 5
      opendocs finetune ./my-project --examples-file ./training.jsonl
    """
    console.print(BANNER)

    try:
        from .llm.slm_finetune import SLMFineTuner, generate_training_data_from_codebase
    except ImportError:
        console.print("[bold red]SLM dependencies not installed.[/]\nRun: pip install opendocs[slm]")
        raise SystemExit(1)

    console.print(f"[bold blue]Preparing fine-tuning data from:[/] {codebase_dir}")

    tuner = SLMFineTuner(
        base_model=base_model,
        output_dir=output_dir,
        lora_r=lora_r,
        lora_alpha=lora_alpha,
        learning_rate=learning_rate,
    )

    # Generate training example from codebase + optional reference
    try:
        example = generate_training_data_from_codebase(codebase_dir, reference_doc)
        if example.documentation:
            tuner.add_example(
                code_context=example.code_context,
                documentation=example.documentation,
                project_name=example.project_name,
            )
            console.print(
                f"[green][OK][/] Created training pair from codebase{' + reference doc' if reference_doc else ''}"
            )
        else:
            console.print(
                "[bold yellow]Warning:[/] No reference document provided. "
                "Add examples via --examples-file or provide a --reference-doc."
            )
    except Exception as exc:
        console.print(f"[bold yellow]Warning:[/] Could not analyze codebase: {exc}")

    # Load additional examples if provided
    if examples_file:
        n = tuner.add_examples_from_file(examples_file)
        console.print(f"[green][OK][/] Loaded {n} additional examples from {examples_file}")

    if not tuner.examples:
        console.print("[bold red]No training examples available. Provide a --reference-doc or --examples-file.[/]")
        raise SystemExit(1)

    console.print(
        f"\n[bold blue]Starting QLoRA fine-tuning:[/]\n"
        f"  Base model: {base_model}\n"
        f"  Examples: {len(tuner.examples)}\n"
        f"  Epochs: {epochs}\n"
        f"  LoRA rank: {lora_r}, alpha: {lora_alpha}\n"
        f"  Output: {output_dir}"
    )

    try:
        adapter_path = tuner.train(epochs=epochs, batch_size=batch_size)
        console.print(f"\n[green][OK][/] Fine-tuning complete! Adapter saved to: {adapter_path}")
        console.print(
            f"\n[dim]Use it with:[/]\n  opendocs codebase ./your-project --provider slm --adapter-path {adapter_path}"
        )
    except Exception as exc:
        console.print(f"[bold red]Fine-tuning failed:[/] {exc}")
        raise SystemExit(1)


@main.command()
@click.argument("codebase_dir", type=click.Path(exists=True, file_okay=False), default=".")
@click.option(
    "--docs",
    "doc_paths",
    type=click.Path(exists=True),
    multiple=True,
    default=None,
    help="Documentation file(s) to check against (default: README.md in the project root).",
)
@click.option(
    "--fail-under",
    type=float,
    default=None,
    help="Exit non-zero if overall coverage is below this percentage.",
)
@click.option("--include-private", is_flag=True, default=False, help="Also score underscore-prefixed symbols.")
@click.option("--include-tests", is_flag=True, default=False, help="Also score test files (excluded by default).")
@click.option("--show-missing", is_flag=True, default=False, help="List every undocumented item, not just a sample.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the report as JSON.")
@click.option("--limit", type=int, default=10, show_default=True, help="Sampled missing items to show per dimension.")
def coverage(
    codebase_dir: str,
    doc_paths: tuple[str, ...],
    fail_under: float | None,
    include_private: bool,
    include_tests: bool,
    show_missing: bool,
    as_json: bool,
    limit: int,
):
    """Report how much of the codebase the documentation actually covers.

    Compares the real API surface against what the docs mention: docstrings on
    public symbols, environment variables the code reads, CLI flags it defines,
    and detected technologies. Entirely offline and deterministic.

    \b
    Examples:
      opendocs coverage .
      opendocs coverage . --docs README.md --docs docs/guide.md
      opendocs coverage . --show-missing
      opendocs coverage . --fail-under 80     # CI gate
      opendocs coverage . --json
    """
    import json as _json

    from .core.coverage import analyse_coverage

    try:
        report = analyse_coverage(
            codebase_dir,
            list(doc_paths) or None,
            include_private=include_private,
            include_tests=include_tests,
        )
    except Exception as exc:
        console.print(f"[bold red]Coverage analysis failed:[/] {exc}")
        raise SystemExit(2) from exc

    if as_json:
        console.print_json(_json.dumps(report.to_dict()))
    else:
        _print_coverage(report, show_missing=show_missing, limit=limit)

    if fail_under is not None and report.overall < fail_under:
        if not as_json:
            console.print(f"[bold red]FAIL[/] coverage {report.overall}% is below --fail-under {fail_under}%\n")
        raise SystemExit(1)


def _print_coverage(report, *, show_missing: bool, limit: int) -> None:
    """Render a coverage report as a table plus optional detail."""
    from rich.table import Table as RichTable

    def _colour(pct: float) -> str:
        return "green" if pct >= 80 else "yellow" if pct >= 50 else "red"

    console.print(f"\n[bold]Documentation coverage[/] [dim]{report.project_name}[/]")
    if report.docs_analysed:
        console.print(f"[dim]checked against: {', '.join(report.docs_analysed)}[/]")
    else:
        console.print("[yellow]No documentation files found — nothing to check against.[/]")

    table = RichTable(show_lines=False)
    table.add_column("Dimension", style="bold")
    table.add_column("Covered", justify="right")
    table.add_column("Total", justify="right")
    table.add_column("Coverage", justify="right")

    for dim in report.dimensions:
        if not dim.applicable:
            table.add_row(dim.name, "-", "0", "[dim]n/a[/]")
            continue
        table.add_row(
            dim.name,
            str(dim.covered),
            str(dim.total),
            f"[{_colour(dim.percent)}]{dim.percent}%[/]",
        )
    console.print(table)

    overall = report.overall
    console.print(f"  [bold]Overall: [{_colour(overall)}]{overall}%[/][/]\n")

    for dim in report.applicable_dimensions:
        if not dim.missing:
            continue
        shown = dim.missing if show_missing else dim.missing[:limit]
        console.print(f"[bold]Undocumented — {dim.name}[/] [dim]({len(dim.missing)})[/]")
        for item in shown:
            console.print(f"  [red]-[/] {item}")
        if len(dim.missing) > len(shown):
            console.print(f"  [dim]... {len(dim.missing) - len(shown)} more (use --show-missing)[/]")
        console.print()


@main.command()
@click.option("--clear", "do_clear", is_flag=True, default=False, help="Delete every cached artifact.")
@click.option("--cache-dir", "cache_dir", type=click.Path(), default=None, help="Cache location to inspect.")
def cache(do_clear: bool, cache_dir: str | None):
    """Inspect or clear the incremental build cache.

    \b
    Examples:
      opendocs cache            # show location and size
      opendocs cache --clear    # empty it
    """
    from .core.build_cache import BuildCache
    from .llm.cache import LLMCache

    store = BuildCache(cache_dir)
    llm_store = LLMCache(Path(cache_dir) / "llm" if cache_dir else None)

    if do_clear:
        artifacts = store.clear()
        responses = llm_store.clear()
        console.print(f"[green][OK][/] Cleared {artifacts} artifact(s) and {responses} LLM response(s)")
        return

    def _describe(label: str, path, entries: int, size: int) -> None:
        console.print(f"\n[bold]{label}[/] [dim]{path}[/]")
        console.print(f"  entries: {entries}")
        console.print(f"  size:    {size / 1024 / 1024:.1f} MB" if size else "  size:    empty")

    build_entries = len(list(store.dir.glob("*/*/manifest.json"))) if store.dir.exists() else 0
    _describe("Build cache", store.dir, build_entries, store.size_bytes())
    _describe("LLM response cache", llm_store.dir, llm_store.entry_count(), llm_store.size_bytes())
    console.print("\n[dim]Clear both with `opendocs cache --clear`.[/]\n")


@main.command()
@click.argument("old", metavar="OLD")
@click.argument("new", metavar="NEW")
@click.option(
    "--git",
    "git_repo",
    type=click.Path(exists=True, file_okay=False),
    default=None,
    help="Treat OLD and NEW as git revisions of --path inside this repository.",
)
@click.option(
    "--path",
    "git_file",
    default="README.md",
    show_default=True,
    help="File to compare when --git is used.",
)
@click.option(
    "--format",
    "out_format",
    type=click.Choice(["markdown", "json", "summary"], case_sensitive=False),
    default="summary",
    show_default=True,
    help="Output style: a short summary, full release notes, or JSON.",
)
@click.option("-o", "--output", "output_file", type=click.Path(), default=None, help="Write the result to a file.")
@click.option("--title", default="Documentation Changes", help="Heading for generated release notes.")
@click.option(
    "--fail-on-change",
    is_flag=True,
    default=False,
    help="Exit non-zero when anything changed (for CI drift detection).",
)
def diff(
    old: str,
    new: str,
    git_repo: str | None,
    git_file: str,
    out_format: str,
    output_file: str | None,
    title: str,
    fail_on_change: bool,
):
    """Report what changed between two versions of documentation.

    OLD and NEW are Markdown files, notebooks, or exported graph.json files.
    With --git they are treated as git revisions of --path instead.

    \b
    Examples:
      opendocs diff old/README.md new/README.md
      opendocs diff v1_graph.json v2_graph.json
      opendocs diff v0.8.0 HEAD --git . --path README.md
      opendocs diff v0.8.0 HEAD --git . --format markdown -o RELEASE_NOTES.md
      opendocs diff a.md b.md --fail-on-change      # CI drift gate
    """
    import json as _json

    from .core.doc_diff import (
        diff_snapshots,
        impacted_formats,
        render_release_notes,
        snapshot_from_git,
        snapshot_from_path,
    )

    try:
        if git_repo:
            old_snap = snapshot_from_git(git_repo, old, git_file)
            new_snap = snapshot_from_git(git_repo, new, git_file)
        else:
            old_snap = snapshot_from_path(old)
            new_snap = snapshot_from_path(new)
    except Exception as exc:
        console.print(f"[bold red]Could not load sources:[/] {exc}")
        raise SystemExit(2) from exc

    delta = diff_snapshots(old_snap, new_snap)

    if out_format.lower() == "json":
        payload = {**delta.to_dict(), "impacted_formats": impacted_formats(delta)}
        rendered = _json.dumps(payload, indent=2)
        if output_file:
            Path(output_file).write_text(rendered, encoding="utf-8")
            console.print(f"[green][OK][/] Wrote {output_file}")
        else:
            console.print_json(rendered)
    elif out_format.lower() == "markdown":
        rendered = render_release_notes(delta, title=title)
        if output_file:
            Path(output_file).write_text(rendered, encoding="utf-8")
            console.print(f"[green][OK][/] Wrote {output_file}")
        else:
            console.print(rendered)
    else:
        _print_diff_summary(delta, impacted_formats(delta))
        if output_file:
            Path(output_file).write_text(render_release_notes(delta, title=title), encoding="utf-8")
            console.print(f"[green][OK][/] Wrote {output_file}")

    if fail_on_change and not delta.is_empty:
        raise SystemExit(1)


def _print_diff_summary(delta, formats: list[str]) -> None:
    """Render a delta as a compact terminal summary."""
    console.print(f"\n[bold]{delta.old_label}[/] [dim]->[/] [bold]{delta.new_label}[/]")

    if delta.is_empty:
        console.print("[green]No documentation changes detected.[/]\n")
        return

    counts = delta.counts()
    rows = [
        ("Sections added", counts["sections_added"], "green"),
        ("Sections removed", counts["sections_removed"], "red"),
        ("Concepts added", counts["entities_added"], "green"),
        ("Concepts removed", counts["entities_removed"], "red"),
        ("Concepts reclassified", counts["entities_retyped"], "yellow"),
        ("Relations added", counts["relations_added"], "green"),
        ("Relations removed", counts["relations_removed"], "red"),
    ]
    console.print()
    for label, count, colour in rows:
        if count:
            console.print(f"  [{colour}]{count:>4}[/] {label}")

    for section in [s for s in delta.sections if s.change == "added"][:10]:
        console.print(f"    [green]+[/] section: {section.title}")
    for section in [s for s in delta.sections if s.change == "removed"][:10]:
        console.print(f"    [red]-[/] section: {section.title}")

    if formats:
        console.print(f"\n[dim]Worth regenerating:[/] {', '.join(formats)}")
    console.print()


@main.command()
@click.argument("source", metavar="SOURCE")
@click.option("--local", is_flag=True, default=False, help="Treat SOURCE as a local file path.")
@click.option("--token", envvar="GITHUB_TOKEN", default=None, help="GitHub token for remote sources.")
@click.option(
    "--fail-on",
    type=click.Choice(["error", "warning", "info", "never"], case_sensitive=False),
    default="error",
    show_default=True,
    help="Lowest severity that should make the command exit non-zero.",
)
@click.option("--check-links", is_flag=True, default=False, help="Also request every external link (uses network).")
@click.option("--include-badges", is_flag=True, default=False, help="Include badge/shield URLs in link checking.")
@click.option("--link-timeout", type=float, default=10.0, show_default=True, help="Per-link timeout in seconds.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit findings as JSON.")
def lint(
    source: str,
    local: bool,
    token: str | None,
    fail_on: str,
    check_links: bool,
    include_badges: bool,
    link_timeout: float,
    as_json: bool,
):
    """Check documentation quality and exit non-zero when it regresses.

    SOURCE is a GitHub URL, a local Markdown file, or a Jupyter Notebook.
    All rules run offline unless --check-links is passed.

    \b
    Examples:
      opendocs lint ./README.md --local
      opendocs lint ./README.md --local --fail-on warning
      opendocs lint https://github.com/owner/repo --check-links
      opendocs lint ./README.md --local --json
    """
    import json as _json

    # -- Load and parse the source -------------------------------------
    from .core.fetcher import ReadmeFetcher, is_github_url
    from .core.linter import Severity, lint_document
    from .core.notebook_parser import NotebookParser, is_notebook
    from .core.parser import ReadmeParser

    try:
        if is_notebook(source):
            doc = NotebookParser().parse(source, repo_name=Path(source).stem)
        else:
            fetcher = ReadmeFetcher(github_token=token)
            if local or not is_github_url(source):
                content, name = fetcher._fetch_local(source)
            else:
                content, name = fetcher.fetch(source)
            doc = ReadmeParser().parse(content, repo_name=name)
    except Exception as exc:
        console.print(f"[bold red]Could not read {source}:[/] {exc}")
        raise SystemExit(2) from exc

    report = lint_document(
        doc,
        check_links_too=check_links,
        link_timeout=link_timeout,
        include_badges=include_badges,
    )

    if as_json:
        console.print_json(_json.dumps(report.to_dict()))
    else:
        _print_lint_report(report, source)

    if fail_on.lower() == "never":
        return
    raise SystemExit(report.exit_code(fail_on=Severity(fail_on.lower())))


def _print_lint_report(report, source: str) -> None:
    """Render a lint report as grouped, colour-coded output."""
    styles = {"error": "bold red", "warning": "yellow", "info": "dim cyan"}
    labels = {"error": "ERROR", "warning": "WARN ", "info": "INFO "}

    console.print(f"\n[bold]Linting[/] {source}")

    if not report.findings:
        console.print(f"[green]No issues found[/] [dim]({report.checked} checks)[/]\n")
        return

    for finding in report.findings:
        sev = finding.severity.value
        console.print(f"  [{styles[sev]}]{labels[sev]}[/] [bold]{finding.rule}[/]  {finding.message}")
        if finding.context:
            console.print(f"          [dim]{finding.context}[/]")

    counts = report.counts()
    summary = ", ".join(f"{n} {name}{'s' if n != 1 else ''}" for name, n in counts.items() if n)
    console.print(f"\n[bold]{summary or 'no issues'}[/] [dim]({report.checked} checks)[/]\n")


@main.command()
@click.argument("graph_path", type=click.Path(exists=True), metavar="GRAPH_JSON")
@click.argument("question", required=False, default=None)
@click.option("--search", "search_term", default=None, help="Substring search over entity names.")
@click.option("--entity", default=None, help="Show one entity with its incoming and outgoing relations.")
@click.option("--neighbors", "neighbors_of", default=None, help="Entities directly connected to this one.")
@click.option("--dependents", default=None, help="Entities that point at this one (what would be affected).")
@click.option("--dependencies", default=None, help="Entities this one points at (what it relies on).")
@click.option("--path", "path_ends", nargs=2, default=None, help="Shortest path between two entities.")
@click.option("--type", "entity_type", default=None, help="List all entities of a type (e.g. database).")
@click.option("--community", "community_id", type=int, default=None, help="List members of a community.")
@click.option("--provenance", default=None, help="Filter by EXTRACTED, INFERRED, or AMBIGUOUS.")
@click.option("--god-nodes", "show_god_nodes", is_flag=True, help="Highest-degree hub entities.")
@click.option("--list-types", "list_types", is_flag=True, help="Show entity and relation type counts.")
@click.option("--questions", "show_questions", is_flag=True, help="Show the stored suggested questions.")
@click.option("--stats", "show_stats", is_flag=True, help="Show graph statistics.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON instead of tables.")
@click.option("--limit", type=int, default=25, show_default=True, help="Maximum rows to display.")
def query(
    graph_path: str,
    question: str | None,
    search_term: str | None,
    entity: str | None,
    neighbors_of: str | None,
    dependents: str | None,
    dependencies: str | None,
    path_ends: tuple[str, str] | None,
    entity_type: str | None,
    community_id: int | None,
    provenance: str | None,
    show_god_nodes: bool,
    list_types: bool,
    show_questions: bool,
    show_stats: bool,
    as_json: bool,
    limit: int,
):
    """Query an exported graph.json without re-processing the source.

    GRAPH_JSON is a graph file produced by a previous `opendocs generate` run.
    Everything here works offline — no LLM, no API key, no network.

    \b
    Examples:
      opendocs query graph.json --stats
      opendocs query graph.json --search redis
      opendocs query graph.json --entity "PostgreSQL"
      opendocs query graph.json --dependents "PostgreSQL"
      opendocs query graph.json --path "API" "S3"
      opendocs query graph.json --type database
      opendocs query graph.json "what depends on Redis?"
    """
    from .core.graph_query import GraphQuery, GraphQueryError, nodes_to_dicts

    try:
        graph = GraphQuery.load(graph_path)
    except GraphQueryError as exc:
        console.print(f"[bold red]Error:[/] {exc}")
        raise SystemExit(2) from exc

    try:
        _run_query(
            graph,
            question=question,
            search_term=search_term,
            entity=entity,
            neighbors_of=neighbors_of,
            dependents=dependents,
            dependencies=dependencies,
            path_ends=path_ends,
            entity_type=entity_type,
            community_id=community_id,
            provenance=provenance,
            show_god_nodes=show_god_nodes,
            list_types=list_types,
            show_questions=show_questions,
            show_stats=show_stats,
            as_json=as_json,
            limit=limit,
            nodes_to_dicts=nodes_to_dicts,
        )
    except GraphQueryError as exc:
        console.print(f"[bold red]Error:[/] {exc}")
        raise SystemExit(1) from exc


def _node_table(title: str, nodes, limit: int):
    """Render entities as a Rich table."""
    from rich.table import Table as RichTable

    table = RichTable(title=title, show_lines=False, title_justify="left")
    table.add_column("Name", style="bold cyan")
    table.add_column("Type", style="magenta")
    table.add_column("Degree", justify="right")
    table.add_column("Community", justify="right")
    table.add_column("Provenance", style="dim")
    for n in nodes[:limit]:
        table.add_row(n.name, n.type.replace("_", " "), str(n.degree), str(n.community), n.provenance)
    return table


def _run_query(graph, **opts):
    """Dispatch a single query mode and print the result."""
    import json as _json

    as_json = opts["as_json"]
    limit = opts["limit"]
    nodes_to_dicts = opts["nodes_to_dicts"]

    def emit_nodes(title, nodes):
        if as_json:
            console.print_json(_json.dumps({"query": title, "results": nodes_to_dicts(nodes[:limit])}))
        else:
            if not nodes:
                console.print(f"[yellow]No results for:[/] {title}")
                return
            console.print(_node_table(title, nodes, limit))
            if len(nodes) > limit:
                console.print(f"[dim]... {len(nodes) - limit} more (raise --limit to see them)[/dim]")

    # -- Metadata modes -------------------------------------------------
    if opts["show_stats"]:
        stats = {"project": graph.project_name, "generated_at": graph.generated_at, **graph.stats}
        if as_json:
            console.print_json(_json.dumps(stats))
        else:
            from rich.table import Table as RichTable

            table = RichTable(title=f"{graph.project_name} — graph statistics", title_justify="left")
            table.add_column("Metric", style="bold")
            table.add_column("Value", justify="right")
            for key, value in stats.items():
                table.add_row(str(key).replace("_", " "), str(value))
            console.print(table)
        return

    if opts["list_types"]:
        payload = {"entity_types": graph.entity_types(), "relation_types": graph.relation_types()}
        if as_json:
            console.print_json(_json.dumps(payload))
        else:
            from rich.table import Table as RichTable

            table = RichTable(title="Entity types", title_justify="left")
            table.add_column("Type", style="bold cyan")
            table.add_column("Count", justify="right")
            for key, count in payload["entity_types"].items():
                table.add_row(key.replace("_", " "), str(count))
            console.print(table)

            table2 = RichTable(title="Relation types", title_justify="left")
            table2.add_column("Relation", style="bold magenta")
            table2.add_column("Count", justify="right")
            for key, count in payload["relation_types"].items():
                table2.add_row(key.replace("_", " "), str(count))
            console.print(table2)
        return

    if opts["show_questions"]:
        questions = graph.suggested_questions()
        if as_json:
            console.print_json(_json.dumps({"suggested_questions": questions}))
        else:
            console.print("[bold]Questions this graph can answer:[/]")
            for i, q in enumerate(questions, 1):
                console.print(f"  [cyan]{i}.[/] {q}")
        return

    # -- Entity modes ---------------------------------------------------
    if opts["show_god_nodes"]:
        emit_nodes("God nodes (highest degree)", graph.god_nodes(top_n=limit))
        return

    if opts["search_term"]:
        emit_nodes(f"Search: {opts['search_term']!r}", graph.search(opts["search_term"], limit=limit))
        return

    if opts["entity_type"]:
        emit_nodes(f"Type: {opts['entity_type']}", graph.of_type(opts["entity_type"]))
        return

    if opts["provenance"]:
        emit_nodes(f"Provenance: {opts['provenance'].upper()}", graph.by_provenance(opts["provenance"]))
        return

    if opts["community_id"] is not None:
        emit_nodes(f"Community {opts['community_id']}", graph.community_members(opts["community_id"]))
        return

    if opts["neighbors_of"]:
        emit_nodes(f"Neighbors of {opts['neighbors_of']!r}", graph.neighbors(opts["neighbors_of"]))
        return

    if opts["dependents"]:
        emit_nodes(f"Depends on {opts['dependents']!r}", graph.dependents_of(opts["dependents"]))
        return

    if opts["dependencies"]:
        emit_nodes(f"{opts['dependencies']!r} relies on", graph.dependencies_of(opts["dependencies"]))
        return

    if opts["entity"]:
        node = graph.resolve(opts["entity"])
        outgoing = graph.outgoing(node.name)
        incoming = graph.incoming(node.name)
        if as_json:
            console.print_json(
                _json.dumps(
                    {
                        "entity": nodes_to_dicts([node])[0],
                        "outgoing": [{"relation": e.relation, "target": t.name} for e, t in outgoing],
                        "incoming": [{"relation": e.relation, "source": s.name} for e, s in incoming],
                    }
                )
            )
        else:
            console.print(f"\n[bold cyan]{node.name}[/]  [dim]({node.type.replace('_', ' ')})[/]")
            console.print(
                f"[dim]degree {node.degree} | community {node.community} | "
                f"{node.provenance} | confidence {node.confidence:.0%}[/]"
            )
            if node.source_section:
                console.print(f"[dim]found in section: {node.source_section}[/]")
            if outgoing:
                console.print("\n[bold]Points at:[/]")
                for e, t in outgoing[:limit]:
                    console.print(f"  [green]--{e.relation.replace('_', ' ')}-->[/] {t.name}")
            if incoming:
                console.print("\n[bold]Pointed at by:[/]")
                for e, s in incoming[:limit]:
                    console.print(f"  {s.name} [green]--{e.relation.replace('_', ' ')}-->[/]")
            if not outgoing and not incoming:
                console.print("\n[yellow]No relations recorded for this entity.[/]")
        return

    if opts["path_ends"]:
        start, end = opts["path_ends"]
        hops = graph.path(start, end)
        if as_json:
            console.print_json(
                _json.dumps(
                    {
                        "start": start,
                        "end": end,
                        "hops": [{"from": h.frm.name, "relation": h.edge.relation, "to": h.to.name} for h in hops],
                    }
                )
            )
        elif not hops:
            console.print(f"[yellow]No path found between {start!r} and {end!r}.[/]")
        else:
            console.print(f"\n[bold]Path ({len(hops)} hop(s)):[/]")
            console.print(f"  [cyan]{hops[0].frm.name}[/]")
            for h in hops:
                arrow = "<--" if h.reversed_ else "-->"
                console.print(f"    [green]{arrow} {h.edge.relation.replace('_', ' ')} {arrow}[/] [cyan]{h.to.name}[/]")
        return

    # -- Natural-language fallback --------------------------------------
    if opts["question"]:
        answer = graph.answer(opts["question"])
        if as_json:
            console.print_json(_json.dumps(answer.to_dict()))
            return
        if answer.hops:
            console.print(f"\n[dim]{answer.summary}[/]")
            console.print(f"  [cyan]{answer.hops[0].frm.name}[/]")
            for h in answer.hops:
                arrow = "<--" if h.reversed_ else "-->"
                console.print(f"    [green]{arrow} {h.edge.relation.replace('_', ' ')} {arrow}[/] [cyan]{h.to.name}[/]")
        elif answer.nodes:
            # The summary is the table title; printing it separately duplicates it.
            console.print(_node_table(answer.summary, answer.nodes, limit))
        else:
            console.print(f"\n[dim]{answer.summary}[/]")
            console.print("[yellow]No matching entities. Try --search or --list-types.[/]")
        return

    # -- Nothing selected: show an overview ------------------------------
    console.print(f"\n[bold]{graph.project_name}[/] [dim]graph exported {graph.generated_at or 'unknown'}[/]")
    console.print(f"[dim]{len(graph.nodes)} entities, {len(graph.edges)} relations[/]\n")
    console.print(_node_table("Top entities", graph.god_nodes(top_n=limit), limit))
    questions = graph.suggested_questions()
    if questions:
        console.print("\n[bold]Try asking:[/]")
        for q in questions[:3]:
            console.print(f"  [dim]-[/] {q}")
    console.print("\n[dim]Run `opendocs query --help` for all query modes.[/]")


if __name__ == "__main__":
    main()
