"""Research pipeline controller — multi-agent orchestration using deepagents.

This is the core module that builds and runs the multi-agent research
pipeline. It uses deepagents' ``create_deep_agent()`` with sub-agents
configured from ``config.yaml``.

The pipeline follows these stages:
    intake → plan → research → execute → analyze → write → review

If the reviewer requests revisions, the pipeline loops back to
the writing (or execution) stage.
"""

from __future__ import annotations

import logging
import os
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Suppress noisy HTTP logs from Langchain/OpenAI/httpx
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


@dataclass
class PipelineState:
    """Tracks pipeline progress across stages."""

    current_stage: str = "intake"
    completed_stages: list[str] = field(default_factory=list)
    stage_outputs: dict[str, str] = field(default_factory=dict)
    review_iterations: int = 0
    max_review_iterations: int = 3
    error: str = ""
    # Graph memory integration (optional)
    memory_client: Any = None

    async def persist_stage(self, stage: str, output: str):
        """Persist stage output to graph memory if client is available."""
        if self.memory_client is None:
            return
        try:
            await self.memory_client.remember(
                uri=f"session://pipeline/{stage}",
                content=output[:5000],  # Cap content size
                disclosure=f"Output from pipeline stage: {stage}",
            )
        except Exception as e:
            logger.warning(f"Failed to persist stage {stage} to memory: {e}")


# =========================================================================
# Config loading
# =========================================================================


def _load_agent_config() -> dict[str, Any]:
    """Load sub-agent config from config.yaml."""
    config_path = Path(__file__).parent / "config.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _resolve_prompt_refs(config: dict[str, Any]) -> dict[str, Any]:
    """Resolve system_prompt_ref → actual prompt text."""
    from .prompts import build_prompt_refs

    refs = build_prompt_refs()
    for agent_name, agent_def in config.items():
        ref = agent_def.get("system_prompt_ref")
        if ref and ref in refs:
            agent_def["system_prompt"] = refs[ref]
            del agent_def["system_prompt_ref"]
    return config


# =========================================================================
# Agent builder
# =========================================================================


def _build_subagent_configs(
    agent_config: dict[str, Any],
    tool_registry: dict,
) -> list[dict[str, Any]]:
    """Convert our YAML config into deepagents subagent format.

    Follows EvoScientist's pattern:
    - Each subagent gets its tools from the registry
    - Each subagent declares skills=['/skills/'] so it can read SKILL.md
    - Each subagent gets ToolErrorHandlerMiddleware for self-recovery

    Parameters
    ----------
    agent_config : dict
        Parsed config.yaml content.
    tool_registry : dict
        Mapping of tool name → tool object.

    Returns
    -------
    list of dicts in deepagents subagent format
    """
    from .middleware import ToolErrorHandlerMiddleware

    subagents = []
    for agent_name, agent_def in agent_config.items():
        tool_names = agent_def.get("tools", [])
        tools = [tool_registry[t] for t in tool_names if t in tool_registry]

        subagent: dict[str, Any] = {
            "name": agent_name,
            "description": agent_def.get("description", ""),
            "tools": tools,
        }
        if "system_prompt" in agent_def:
            subagent["system_prompt"] = agent_def["system_prompt"]

        # EvoScientist pattern: let subagents see /skills/ for SKILL.md
        if "skills" in agent_def:
            subagent["skills"] = agent_def["skills"]
        else:
            subagent["skills"] = ["/skills/"]

        # EvoScientist pattern: inject ToolErrorHandlerMiddleware
        # so tool errors become recoverable ToolMessages instead of crashes
        subagent["middleware"] = [ToolErrorHandlerMiddleware()]

        subagents.append(subagent)
    return subagents


# =========================================================================
# Research Pipeline
# =========================================================================


class ResearchPipeline:
    """Multi-agent research pipeline using deepagents/LangGraph.

    Parameters
    ----------
    workspace_dir : str, optional
        Directory for pipeline workspace. Auto-created if needed.
    provider : str, optional
        LLM provider (uses .env default if empty).
    model : str, optional
        LLM model name override.
    """

    STAGES = [
        "intake", "plan", "research", "execute",
        "analyze", "write", "review",
    ]

    def __init__(
        self,
        workspace_dir: str | None = None,
        provider: str = "",
        model: str = "",
    ):
        from .tools import build_tool_registry
        from .prompts import get_system_prompt

        # Workspace
        if workspace_dir:
            self.workspace = Path(workspace_dir).resolve()
        else:
            self.workspace = Path.cwd() / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)

        # Config
        self.agent_config = _resolve_prompt_refs(_load_agent_config())
        self.tool_registry = build_tool_registry()
        self.system_prompt = get_system_prompt()
        self.state = PipelineState()

        # Notebook session — coding-agent uses this for experiment execution
        self.notebook_path = str(self.workspace / "analysis.ipynb")
        self._nb_session = None  # lazy-init on first run

        # LLM settings
        self.provider = provider
        self.model = model

    def _get_llm(self):
        """Build the LLM instance from provider/model settings.

        Resolution order (provider):
            1. Constructor ``provider`` arg  (e.g. from CLI ``--provider``)
            2. ``OC_LLM_PROVIDER`` env       (research-pipeline-specific override)
            3. ``LLM_PROVIDER`` env           (project-wide default, same as bot/interactive)
            4. Fallback: ``"deepseek"``

        Resolution order (api key):
            ``DEEPSEEK_API_KEY`` → ``LLM_API_KEY`` (for deepseek)
            ``OPENAI_API_KEY``  → ``LLM_API_KEY`` (for openai)
            ``ANTHROPIC_API_KEY`` → ``LLM_API_KEY`` (for anthropic)

        Resolution order (model):
            1. Constructor ``model`` arg
            2. ``OC_LLM_MODEL`` env
            3. ``OMICSCLAW_MODEL`` env
            4. Provider-specific default
        """
        provider = (
            self.provider
            or os.getenv("OC_LLM_PROVIDER")
            or os.getenv("LLM_PROVIDER", "deepseek")
        )
        model = (
            self.model
            or os.getenv("OC_LLM_MODEL")
            or os.getenv("OMICSCLAW_MODEL")
        )
        # Unified API key: provider-specific env → LLM_API_KEY fallback
        api_key = os.getenv("LLM_API_KEY", "")

        if provider == "deepseek":
            from langchain_openai import ChatOpenAI
            return ChatOpenAI(
                model=model or "deepseek-chat",
                openai_api_key=os.getenv("DEEPSEEK_API_KEY") or api_key,
                openai_api_base=os.getenv(
                    "DEEPSEEK_BASE_URL",
                    "https://api.deepseek.com/v1",
                ),
                temperature=0.3,
            )
        elif provider in ("openai", ""):
            from langchain_openai import ChatOpenAI
            return ChatOpenAI(
                model=model or "gpt-4o",
                openai_api_key=os.getenv("OPENAI_API_KEY") or api_key or None,
                temperature=0.3,
            )
        elif provider == "anthropic":
            from langchain_anthropic import ChatAnthropic
            return ChatAnthropic(
                model=model or "claude-sonnet-4-20250514",
                anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or api_key or None,
                temperature=0.3,
            )
        else:
            # Generic OpenAI-compatible
            from langchain_openai import ChatOpenAI
            return ChatOpenAI(
                model=model or "gpt-4o",
                openai_api_key=api_key or None,
                openai_api_base=os.getenv("LLM_BASE_URL"),
                temperature=0.3,
            )

    def _build_agent(self):
        """Build the deepagents agent with OmicsClaw sub-agents.

        Architecture (adapted from EvoScientist):
        - CompositeBackend routes /skills/ to read-only skills directory
        - ToolErrorHandlerMiddleware on orchestrator + all sub-agents
        - skills=["/skills/"] enables deepagents skill discovery
        """
        from deepagents import create_deep_agent
        from deepagents.backends import CompositeBackend

        from .backends import create_sandbox_backend, create_skills_backend
        from .middleware import ToolErrorHandlerMiddleware

        llm = self._get_llm()

        # Workspace backend (sandboxed read/write + shell)
        ws_backend = create_sandbox_backend(str(self.workspace))

        # Skills backend (read-only, maps /skills/ → OmicsClaw skills/)
        project_root = str(Path(__file__).resolve().parent.parent.parent)
        sk_backend = create_skills_backend(project_root)

        # Composite backend — like EvoScientist's multi-route FS
        backend = CompositeBackend(
            default=ws_backend,
            routes={
                "/skills/": sk_backend,
            },
        )

        subagents = _build_subagent_configs(
            self.agent_config, self.tool_registry
        )

        # Orchestrator middleware — error handler for main agent tools
        middleware = [ToolErrorHandlerMiddleware()]

        agent = create_deep_agent(
            model=llm,
            backend=backend,
            system_prompt=self.system_prompt,
            subagents=subagents,
            middleware=middleware,
            skills=["/skills/"],
        )
        return agent

    async def run(
        self,
        idea: str,
        pdf_path: str | None = None,
        h5ad_path: str | None = None,
        on_stage: Callable[[str, str], None] | None = None,
    ) -> dict[str, Any]:
        """Execute the full research pipeline.

        Parameters
        ----------
        idea : str
            User's research idea / hypothesis (always required).
        pdf_path : str, optional
            Path to the scientific paper PDF (Mode A/B).
            If omitted, runs in Mode C (idea-only).
        h5ad_path : str, optional
            Path to user-provided h5ad file (Mode B).
        on_stage : callable, optional
            Progress callback ``(stage_name, status_message)``.

        Returns
        -------
        dict with keys: success, report_path, review_path, workspace, error
        """
        def _notify_stage(stage: str, status: str):
            self.state.current_stage = stage
            if on_stage:
                on_stage(stage, status)
            logger.info("[%s] %s", stage, status)

        try:
            # ── Stage 1: Intake ────────────────────────────────────────
            if pdf_path:
                _notify_stage("intake", "Parsing PDF and assembling context...")
            else:
                _notify_stage("intake", "Assembling idea-only context (Mode C)...")

            from .intake import prepare_intake

            intake = prepare_intake(
                idea=idea,
                pdf_path=pdf_path,
                h5ad_path=h5ad_path,
                output_dir=str(self.workspace),
            )
            self.state.completed_stages.append("intake")
            self.state.stage_outputs["intake"] = intake.paper_md_path

            # ── Stage 2–7: Agent-driven stages ────────────────────────
            # Register workspace so notebook_create resolves relative paths
            from .tools import set_workspace_dir
            set_workspace_dir(str(self.workspace))

            _notify_stage("agent", "Building multi-agent graph...")
            agent = self._build_agent()

            # Construct the initial prompt for the orchestrator
            initial_prompt = self._build_initial_prompt(intake)

            _notify_stage("pipeline", "Running research pipeline...")

            # Stream the agent execution
            # Default recursion_limit=25 is far too low for a multi-agent
            # pipeline with 6 sub-agents and multiple tool calls per stage.
            recursion_limit = int(
                os.getenv("OC_RECURSION_LIMIT", "500")
            )
            config = {
                "recursion_limit": recursion_limit,
                "configurable": {
                    "thread_id": f"research-{self.workspace.name}",
                },
            }

            final_output = ""
            async for event in agent.astream_events(
                {"messages": [("human", initial_prompt)]},
                config=config,
                version="v2",
            ):
                kind = event.get("event", "")
                if kind == "on_chat_model_stream":
                    chunk = event.get("data", {}).get("chunk")
                    if chunk and hasattr(chunk, "content"):
                        content = chunk.content
                        if isinstance(content, str):
                            final_output += content
                elif kind == "on_tool_start":
                    tool_name = event.get("name", "")
                    task_input = event.get("data", {}).get("input", {})
                    
                    if tool_name == "task":
                        # Sub-agent delegation
                        agent_name = task_input.get("subagent", "sub-agent")
                        task_desc = task_input.get("task_description", "")
                        desc = (task_desc[:50] + "...") if len(task_desc) > 50 else task_desc
                        _notify_stage(
                            agent_name.replace("-agent", ""),
                            f"Delegating task: [italic]{desc}[/italic]"
                        )
                    elif tool_name == "execute":
                        cmd = task_input.get("command", "")
                        _notify_stage("shell", f"Running: [dim]{cmd[:60]}{'...' if len(cmd) > 60 else ''}[/dim]")
                    elif tool_name == "notebook_create":
                        _notify_stage("notebook", f"Creating notebook...")
                    elif tool_name == "write_file":
                        path = task_input.get("file_path", "")
                        _notify_stage("file", f"Writing [dim]{Path(path).name}[/dim]...")
                    elif tool_name == "read_file":
                        path = task_input.get("file_path", "")
                        _notify_stage("file", f"Reading [dim]{Path(path).name}[/dim]...")
                    elif tool_name == "tavily_search":
                        q = task_input.get("query", "")
                        _notify_stage("search", f"Searching web for: [dim]'{q}'[/dim]")
                    elif tool_name == "skill_search":
                        q = task_input.get("query", "")
                        _notify_stage("search", f"Searching local skills for: [dim]'{q}'[/dim]")
                    elif tool_name not in ("__start__", "agent"):
                        _notify_stage("tool", f"Using [bold]{tool_name}[/bold]...")


            # ── Check outputs ─────────────────────────────────────────
            report_path = self.workspace / "final_report.md"
            review_path = self.workspace / "review_report.json"

            return {
                "success": True,
                "report_path": str(report_path) if report_path.exists() else "",
                "review_path": str(review_path) if review_path.exists() else "",
                "notebook_path": self.notebook_path,
                "workspace": str(self.workspace),
                "intake": {
                    "mode": intake.input_mode,
                    "title": intake.paper_title,
                    "geo_accessions": intake.geo_accessions,
                },
                "final_output": final_output[:2000],
                "error": "",
            }

        except Exception as e:
            logger.error("Pipeline failed: %s", e, exc_info=True)
            self.state.error = str(e)
            return {
                "success": False,
                "report_path": "",
                "review_path": "",
                "notebook_path": self.notebook_path,
                "workspace": str(self.workspace),
                "error": str(e),
            }
        finally:
            # Clean up notebook kernel
            from .tools import _nb_session
            if _nb_session is not None:
                try:
                    _nb_session.shutdown()
                except Exception:
                    pass

    def _build_initial_prompt(self, intake) -> str:
        """Build the initial prompt that kicks off the orchestrator."""
        from .intake import IntakeResult

        workspace_abs = str(self.workspace.resolve())

        parts = [
            "# New Research Request\n",
            "The user wants to conduct a multi-omics research project. "
            "Start the research pipeline workflow.\n",
            f"## Workspace\n",
            f"All output files MUST be saved under: `{workspace_abs}`\n",
            f"This includes: plan.md, todos.md, notebooks (.ipynb), "
            f"artifacts/, final_report.md, review_report.json.\n",
            f"When using shell commands, always use absolute paths with "
            f"this workspace prefix. When creating notebooks, use simple "
            f"filenames (the system resolves them to the workspace).\n",
        ]

        # Mode C: idea only
        if intake.input_mode == "C":
            parts.append(f"## User's Research Idea\n\n{intake.idea}\n")
            parts.append(
                "## Data (Mode C: idea only)\n"
                "No reference paper was provided. Start by delegating to the "
                "research-agent to search for relevant literature, datasets, "
                "and methods. Then proceed to the planner-agent.\n"
            )
        else:
            # Mode A / B
            parts.extend([
                "## Paper\n",
                f"Paper summary (concise): {workspace_abs}/paper_summary.md\n",
                f"Full paper text (reference): {workspace_abs}/paper_fulltext.md\n",
                f"## User's Research Idea\n\n{intake.idea}\n",
            ])

            if intake.input_mode == "B":
                parts.append(
                    f"## Data (Mode B: user-provided)\n"
                    f"User provided h5ad file: {intake.h5ad_path}\n"
                )
                if intake.h5ad_metadata:
                    meta = intake.h5ad_metadata
                    parts.append(
                        f"- Cells: {meta.get('n_obs', 'N/A')}, "
                        f"Genes: {meta.get('n_vars', 'N/A')}\n"
                    )
                    if meta.get("has_spatial"):
                        parts.append("- Contains spatial coordinates\n")
            else:
                parts.append(
                    f"## Data (Mode A: from paper)\n"
                    f"GEO accessions found: {', '.join(intake.geo_accessions) or 'None'}\n"
                    f"Download datasets if accessions are available.\n"
                )

        parts.append(
            f"\n## Instructions\n"
            f"Follow the research pipeline workflow. Start with the "
            f"planner-agent to create an experimental plan, then proceed "
            f"through each stage.\n"
            f"\n"
            f"IMPORTANT: All generated files must go into `{workspace_abs}`.\n"
            f"Use this as the base directory for ALL read/write operations.\n"
        )

        return "\n".join(parts)
