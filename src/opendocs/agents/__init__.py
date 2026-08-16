"""Agentic documentation layer for opendocs.

.. warning::

   **Status: architectural scaffolding, not a working feature.** This package
   is excluded from the built wheel (see ``pyproject.toml``) and is not
   reachable from the CLI. Several key methods are stubs that return empty or
   pass-through results:

   - ``diff.DiffAgent._compute_diff`` returns an empty ``DiffSummary``
     regardless of the refs it is given, so the whole diff chain reports no
     changes even when a repository has them.
   - ``diff.RegenerationAgent._regenerate`` returns the requested format list
     without regenerating anything.
   - ``diff.ImpactAgent._compute_impact`` is implemented, but indexes entities
     by a ``source_file`` / ``file_path`` / ``path`` property that nothing in
     the extractor ever sets, so its file-to-entity map is always empty.

   Do not expose these through user-facing commands until those are
   implemented — they would silently succeed while doing nothing. For working
   change detection, see :mod:`opendocs.core.doc_diff`, which powers
   ``opendocs diff``.

This package sketches the Planner → Executor → Critic architecture
that differentiates opendocs from static doc generators like Mintlify,
GitBook AI, DocuWriter, and repo-diagram tools.

Key design principles:
    1. **Evidence-grounded** — every claim links to a source via EvidencePointer.
    2. **Tool-orchestrated** — MCP tool contracts serve as the generation bus.
    3. **Diff-aware** — only regenerate artifacts impacted by code changes.
    4. **Privacy-safe** — agents receive RepoProfile + KG, never raw code by default.

Modules
-------
base        — Agent base classes, plan/result models, evidence pointers
orchestrator — Planner → Executor → Critic loop
planner     — Step-by-step plan generation from RepoProfile + KG
executor    — Dispatches tool calls to specialized sub-agents
critic      — Validates evidence coverage, flags hallucinations
evidence    — Evidence pointer model, coverage scoring
privacy     — Privacy toggle, strict mode, snippet filtering
diff/       — Diff-aware continuous sync pipeline
specialized/ — Domain-specific sub-agents (microservices, ML, infra, etc.)
tools/      — MCP tool contracts and adapters
"""

from .base import AgentBase, AgentPlan, AgentResult, PlanStep, ToolCall
from .evidence import EvidenceCoverage, EvidencePointer
from .orchestrator import AgentOrchestrator
from .privacy import PrivacyGuard, PrivacyMode

__all__ = [
    "AgentBase",
    "AgentPlan",
    "AgentResult",
    "PlanStep",
    "ToolCall",
    "EvidencePointer",
    "EvidenceCoverage",
    "AgentOrchestrator",
    "PrivacyMode",
    "PrivacyGuard",
]
