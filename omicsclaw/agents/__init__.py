"""OmicsClaw Multi-Agent Research Pipeline.

Integrates EvoScientist-inspired multi-agent architecture into OmicsClaw,
enabling autonomous end-to-end scientific research workflows:

    idea → plan → research → execute → analyze → write → review

Requires optional ``[research]`` dependencies::

    pip install -e ".[research]"

Usage::

    from omicsclaw.agents import run_research_pipeline

    # Mode A: PDF + idea
    run_research_pipeline(idea="...", pdf_path="paper.pdf")

    # Mode C: idea only (like EvoScientist)
    run_research_pipeline(idea="Investigate TME heterogeneity")

Or via CLI::

    oc interactive
    > /research --idea "..."                                  # Mode C
    > /research paper.pdf --idea "..."                        # Mode A
    > /research paper.pdf --idea "..." --h5ad d               # Mode B
    > /research --idea "..." --output /path/to/output         # custom output dir
"""

from __future__ import annotations

__all__ = [
    "run_research_pipeline",
    "ResearchPipeline",
    "prepare_intake",
]


def _check_research_deps() -> None:
    """Verify that the research optional dependencies are installed."""
    missing = []
    for pkg in ("deepagents", "langchain", "langchain_openai", "langgraph"):
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        raise ImportError(
            f"Research pipeline requires: {', '.join(missing)}. "
            f'Install with: pip install -e ".[research]"'
        )


def run_research_pipeline(
    idea: str,
    pdf_path: str | None = None,
    h5ad_path: str | None = None,
    workspace_dir: str | None = None,
    provider: str = "",
    model: str = "",
    on_stage: object = None,
):
    """Entry point — run full research pipeline (sync wrapper).

    Parameters
    ----------
    idea : str
        User's research idea / hypothesis (always required).
    pdf_path : str, optional
        Path to the scientific paper PDF (Mode A/B). If omitted,
        runs in Mode C (idea-only, like EvoScientist).
    h5ad_path : str, optional
        Path to user-provided h5ad data (Mode B).
    workspace_dir : str, optional
        Working directory for pipeline outputs.
    provider : str, optional
        LLM provider name (deepseek, openai, anthropic, ...).
    model : str, optional
        LLM model name override.
    on_stage : callable, optional
        Callback ``(stage_name, status)`` for progress updates.
    """
    import asyncio

    _check_research_deps()
    from .pipeline import ResearchPipeline

    pipeline = ResearchPipeline(
        workspace_dir=workspace_dir,
        provider=provider,
        model=model,
    )
    return asyncio.get_event_loop().run_until_complete(
        pipeline.run(idea, pdf_path=pdf_path, h5ad_path=h5ad_path, on_stage=on_stage)
    )


# Lazy imports to avoid loading heavy deps at package import time
def __getattr__(name: str):
    if name == "ResearchPipeline":
        _check_research_deps()
        from .pipeline import ResearchPipeline
        return ResearchPipeline
    if name == "prepare_intake":
        from .intake import prepare_intake
        return prepare_intake
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
