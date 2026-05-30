from __future__ import annotations

from dataclasses import dataclass

from .engineering import build_engineering_tool_specs
from ..registry import ToolRegistry
from ..spec import (
    APPROVAL_MODE_ASK,
    PROGRESS_POLICY_ANALYSIS,
    RESULT_POLICY_INSPECTION_REFERENCE,
    RESULT_POLICY_KNOWLEDGE_REFERENCE,
    RESULT_POLICY_MEMORY_WRITE,
    RESULT_POLICY_SUMMARY_OR_MEDIA,
    RESULT_POLICY_WEB_REFERENCE,
    RISK_LEVEL_HIGH,
    RISK_LEVEL_MEDIUM,
    ToolSpec,
)

# OmicsClaw-KG wiki page types (mirror ``omicsclaw_kg.paths.WIKI_SUBDIRS``) — used
# in the ``kg_*`` read-tool schemas. Kept as a literal on purpose: importing the
# optional KG package at tool-spec build time would make the frozen tool list
# differ by environment, breaking the cache-stable prefix invariant (ADR 0024).
_KG_PAGE_TYPES: tuple[str, ...] = (
    "sources", "entities", "concepts", "methods", "comparisons",
    "syntheses", "hypotheses", "questions", "topics", "experiments",
)


@dataclass(frozen=True, slots=True)
class BotToolContext:
    skill_names: tuple[str, ...]
    # ``skill_desc_text`` is retained for backward compatibility with older
    # callers/tests, but is no longer embedded verbatim in the tool
    # description. The LLM now receives a compact 7-domain briefing built
    # from ``omicsclaw.skill.domain_briefing`` instead.
    skill_desc_text: str = ""
    domain_briefing: str = ""


def build_default_bot_tool_context() -> BotToolContext:
    """Production-shape ``BotToolContext`` with the full skill registry.

    Mirrors ``bot/core.py:_build_bot_tool_context`` so external callers
    (e.g. the behavioral-parity eval suite) can build the *same* tool
    list the production bot path sends to the LLM. Without this, eval
    callers tend to stub ``skill_names=("sc-de", "spatial-preprocess")``
    and end up testing a fictional tool surface where the ``omicsclaw``
    tool's ``skill`` parameter is restricted to two enums and the
    ``"auto"`` routing path is missing — fatal for routing parity.
    """
    from omicsclaw.skill.domain_briefing import build_domain_briefing
    from omicsclaw.skill.registry import ensure_registry_loaded

    registry = ensure_registry_loaded()
    skill_names = tuple(list(registry.skills.keys()) + ["auto"])
    briefing = build_domain_briefing(
        lead_in=(
            "OmicsClaw dispatches multi-omics analysis across 7 domains. "
            "Each line below summarizes a domain and lists a few representative skills."
        ),
        trailing_hint=(
            "The `skill` parameter accepts any canonical skill alias or legacy alias "
            "(resolved automatically). For the complete skill list of one domain, "
            "call the `list_skills_in_domain` tool (preferred, paginated) or read "
            "`skills/<domain>/INDEX.md` on disk. "
            "Prefer skill='auto' with a natural-language `query` to let the capability "
            "resolver pick the best match programmatically."
        ),
        ensure_loaded=False,
    )
    return BotToolContext(
        skill_names=skill_names,
        skill_desc_text="",
        domain_briefing=briefing,
    )


def _resolve_domain_briefing(context: BotToolContext) -> str:
    """Return the briefing text, computing it lazily if not preset.

    Allows tests to inject a pre-rendered briefing (cheap, deterministic),
    while production callers can omit it and let the registry render.
    """
    if context.domain_briefing:
        return context.domain_briefing
    try:
        from omicsclaw.skill.domain_briefing import build_domain_briefing
        return build_domain_briefing(
            lead_in=(
                "OmicsClaw dispatches multi-omics analysis across 8 domains. "
                "Each line below summarizes a domain and lists a few representative skills."
            ),
            trailing_hint=(
                "The `skill` parameter accepts any canonical skill alias or legacy alias "
                "(resolved automatically). For the complete skill list of one domain, "
                "call the `list_skills_in_domain` tool (preferred, paginated) or read "
                "`skills/<domain>/INDEX.md` on disk. "
                "Prefer skill='auto' with a natural-language `query` to let the capability "
                "resolver pick the best match programmatically."
            ),
        )
    except Exception:
        # Fallback when registry isn't importable (e.g. partial test envs).
        return context.skill_desc_text


def build_bot_tool_specs(context: BotToolContext) -> list[ToolSpec]:
    briefing = _resolve_domain_briefing(context)
    specs = [
        ToolSpec(
            name="omicsclaw",
            description=(
                "Run an OmicsClaw multi-omics analysis skill.\n\n"
                f"{briefing}\n\n"
                "PREFER `skill='auto'` + `query=<user request>` "
                "(auto-routes deterministically). Pick a specific skill only "
                "if the user named it. `mode='demo'` ONLY when explicitly "
                "asked. For a server-side file, use `mode='path'` and pass "
                "the path at the top level as `file_path='/abs/path.h5ad'` — "
                "do NOT nest args under a `params` key. `mode='file'` is "
                "only for files the user uploaded via chat. `return_media` "
                "empty = text summary; pass a keyword "
                "('umap','qc','cluster',comma-sep) or 'all' for figures. "
                "For sc-batch-integration upstream-prep pause, rerun with "
                "`auto_prepare=true`. Preserve exact numbers, warnings, "
                "errors, and file paths when relaying results."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "skill": {
                        "type": "string",
                        "enum": list(context.skill_names),
                        "description": (
                            "Skill alias to run. Default to 'auto' and pass `query`; "
                            "use a specific alias only if the user named it or auto-routing "
                            "asked you to disambiguate."
                        ),
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["file", "demo", "path"],
                        "description": (
                            "'demo' = built-in synthetic data; "
                            "'file' = user uploaded a file via messaging; "
                            "'path' = user provided a file path on the server."
                        ),
                    },
                    "return_media": {
                        "type": "string",
                        "description": (
                            "Filter for which figures/tables to send back. "
                            "Omit or leave empty for text summary only (default). "
                            "'all' = send all figures and tables. "
                            "Otherwise a comma-separated list of keywords to match filenames "
                            "(e.g. 'umap', 'qc', 'violin', 'cluster', 'umap,qc'). "
                            "Only set when the user explicitly asks for visual results."
                        ),
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Server-side file path or filename for mode='path'.",
                    },
                    "query": {
                        "type": "string",
                        "description": "Natural language query for auto-routing.",
                    },
                    "extra_args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Additional CLI arguments (e.g. ['--method', 'spagcn']).",
                    },
                    "method": {
                        "type": "string",
                        "description": "Analysis method override passed as --method.",
                    },
                    "batch_key": {
                        "type": "string",
                        "description": (
                            "Batch/sample metadata column passed as --batch-key. "
                            "Especially important for sc-batch-integration. "
                            "If omitted for sc-batch-integration, OmicsClaw will inspect the AnnData object "
                            "and ask the user to choose before running."
                        ),
                    },
                    "confirm_workflow_skip": {
                        "type": "boolean",
                        "description": (
                            "Only for explicit user overrides. "
                            "If true, bypasses the standardize/preprocess workflow pause for sc-batch-integration "
                            "and runs direct integration anyway."
                        ),
                    },
                    "auto_prepare": {
                        "type": "boolean",
                        "description": (
                            "Only when the user explicitly agrees to preparatory steps. "
                            "For sc-batch-integration, automatically runs the recommended upstream workflow "
                            "(`sc-standardize-input` and/or `sc-preprocessing`) before the final integration step."
                        ),
                    },
                    "n_epochs": {
                        "type": "integer",
                        "description": (
                            "Number of training epochs for deep learning methods. "
                            "Defaults per method if omitted: "
                            "cell2location=30000, destvi=2500, stereoscope=150000, tangram=1000. "
                            "Only set when the user explicitly requests a custom epoch count."
                        ),
                    },
                    "data_type": {
                        "type": "string",
                        "description": "Data platform type passed as --data-type.",
                    },
                },
                "required": ["skill", "mode"],
            },
            surfaces=("bot",),
            # Bench (ADR 0018) — thread_id rides tool_runtime_context into
            # execute_omicsclaw to scope auto-captured analysis:// lineage.
            context_params=("session_id", "chat_id", "cancel_event", "thread_id"),
            read_only=False,
            concurrency_safe=False,
            result_policy=RESULT_POLICY_SUMMARY_OR_MEDIA,
            progress_policy=PROGRESS_POLICY_ANALYSIS,
            risk_level=RISK_LEVEL_MEDIUM,
            writes_workspace=True,
            policy_tags=("analysis", "workflow"),
        ),
        ToolSpec(
            name="replot_skill",
            description=(
                "Re-render R Enhanced (ggplot2) plots from a prior skill "
                "output WITHOUT re-running. Use for 're-draw with R', "
                "'make it prettier', 'replot', or to tune params. Skill "
                "must have run first via `omicsclaw`. ALWAYS call for R "
                "Enhanced — never assume lack of R support. Use `renderer` "
                "to pick one sub-plot; tune via `top-n`/`font-size`/"
                "`width`/`height`/`dpi`/`palette`/`title`. Omit "
                "`output_path` to auto-resolve. If R missing, relay "
                "returned install instructions; do NOT fall back to "
                "`custom_analysis_execute` or Python plotting."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "skill": {
                        "type": "string",
                        "description": "Skill alias to replot (e.g. 'sc-qc', 'sc-de', 'sc-markers').",
                    },
                    "output_path": {
                        "type": "string",
                        "description": (
                            "Full path to the output directory from the previous skill run. "
                            "Omit to auto-resolve from session history."
                        ),
                    },
                    "renderer": {
                        "type": "string",
                        "description": "Optional: run only a specific renderer (e.g. 'plot_feature_violin'). Omit to run all renderers.",
                    },
                    "return_media": {
                        "type": "string",
                        "description": "Filter for which R Enhanced figures to send. Default: 'all'. Use a keyword to filter (e.g. 'violin').",
                    },
                    "top_n": {"type": "integer", "description": "Number of top items to label/show."},
                    "font_size": {"type": "integer", "description": "Base font size in points."},
                    "width": {"type": "integer", "description": "Figure width in inches."},
                    "height": {"type": "integer", "description": "Figure height in inches."},
                    "dpi": {"type": "integer", "description": "Output resolution (default 300)."},
                    "palette": {"type": "string", "description": "Color palette name."},
                    "title": {"type": "string", "description": "Custom plot title."},
                },
                "required": ["skill"],
            },
            surfaces=("bot",),
            context_params=("session_id", "chat_id"),
            read_only=False,
            concurrency_safe=False,
            result_policy=RESULT_POLICY_SUMMARY_OR_MEDIA,
            progress_policy=PROGRESS_POLICY_ANALYSIS,
            risk_level=RISK_LEVEL_MEDIUM,
            writes_workspace=True,
            policy_tags=("analysis", "workflow"),
        ),
        ToolSpec(
            name="list_skills_in_domain",
            description=(
                "Lazy-load the full skill list for one OmicsClaw domain. "
                "Call this ONLY when the 8-domain briefing in the `omicsclaw` tool "
                "description isn't enough to pick a skill, or when the user asks "
                "'what tools do you have for <domain>?'. The result is a markdown "
                "block with each skill's alias, one-line description, and trigger "
                "keywords. For most routing, prefer `omicsclaw(skill='auto', query=...)` "
                "— the resolver picks the right skill without this extra round-trip."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "enum": [
                            "spatial", "singlecell", "genomics",
                            "proteomics", "metabolomics", "bulkrna",
                            "orchestrator", "literature",
                        ],
                        "description": "Which domain's skills to list.",
                    },
                    "filter": {
                        "type": "string",
                        "description": (
                            "Optional case-insensitive substring. Matches against "
                            "skill alias, description, and trigger keywords. "
                            "Omit to see the whole domain."
                        ),
                    },
                },
                "required": ["domain"],
            },
            surfaces=("bot",),
            read_only=True,
            concurrency_safe=True,
            result_policy=RESULT_POLICY_KNOWLEDGE_REFERENCE,
            policy_tags=("knowledge", "reference", "routing"),
        ),
        ToolSpec(
            name="save_file",
            description="Save a file that was sent via messaging to a specific folder. Default: OmicsClaw data/ directory.",
            parameters={
                "type": "object",
                "properties": {
                    "destination_folder": {"type": "string", "description": "Folder path (absolute)."},
                    "filename": {"type": "string", "description": "Optional filename."},
                },
                "required": ["destination_folder"],
            },
            surfaces=("bot",),
            read_only=False,
            concurrency_safe=False,
            risk_level=RISK_LEVEL_MEDIUM,
            writes_workspace=True,
            policy_tags=("workspace", "artifact"),
        ),
        ToolSpec(
            name="write_file",
            description="Create or overwrite a file with the given content. Files are saved to the output/ directory by default. ONLY use when user explicitly asks to create/save a file.",
            parameters={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Full text content."},
                    "filename": {"type": "string", "description": "Filename with extension."},
                    "destination_folder": {"type": "string", "description": "Folder path (absolute). Default: OmicsClaw data/."},
                },
                "required": ["content", "filename"],
            },
            surfaces=("bot",),
            read_only=False,
            concurrency_safe=False,
            risk_level=RISK_LEVEL_MEDIUM,
            writes_workspace=True,
            policy_tags=("workspace", "artifact"),
        ),
        ToolSpec(
            name="generate_audio",
            description="Generate an MP3 audio file from text using edge-tts.",
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to convert to speech."},
                    "filename": {"type": "string", "description": "Output MP3 filename."},
                    "voice": {"type": "string", "description": "TTS voice. Default: en-GB-RyanNeural."},
                    "rate": {"type": "string", "description": "Speech rate. Default: '-5%'."},
                    "destination_folder": {"type": "string", "description": "Output folder."},
                },
                "required": ["text", "filename"],
            },
            surfaces=("bot",),
            read_only=False,
            concurrency_safe=False,
            writes_workspace=True,
            policy_tags=("artifact",),
        ),
        ToolSpec(
            name="parse_literature",
            description="Parse scientific literature (PDF, URL, DOI, PubMed ID) to extract GEO accessions and metadata, then download datasets. Use when user mentions a paper, sends a PDF, or provides a literature reference.",
            parameters={
                "type": "object",
                "properties": {
                    "input_type": {
                        "type": "string",
                        "enum": ["auto", "url", "doi", "pubmed", "file", "text"],
                        "description": "Type of input (default: auto-detect)",
                    },
                    "input_value": {
                        "type": "string",
                        "description": "URL, DOI, PubMed ID, file path, or text content",
                    },
                    "auto_download": {
                        "type": "boolean",
                        "description": "Automatically download datasets (default: true)",
                    },
                },
                "required": ["input_value"],
            },
            surfaces=("bot",),
            # Bench (ADR 0021, Phase 3.3b) — the download is a permission-gated
            # PROPOSAL, never automatic. approval_mode=ASK routes the call through
            # request_tool_approval on surfaces that support it (desktop); on
            # surfaces without an approval callback (channels/CLI) it is blocked
            # with a message, consistent with the other write/network ASK tools
            # (move_file/remove_file/file_write). context_params deliver session +
            # thread so a downloaded dataset registers under dataset://<thread_id>/*.
            context_params=("session_id", "thread_id"),
            read_only=False,
            concurrency_safe=False,
            approval_mode=APPROVAL_MODE_ASK,
            risk_level=RISK_LEVEL_MEDIUM,
            writes_workspace=True,
            touches_network=True,
            policy_tags=("knowledge", "network"),
        ),
        ToolSpec(
            name="fetch_geo_metadata",
            description="Fetch metadata for a specific GEO accession (GSE, GSM, or GPL). Use when user asks to fetch, query, or get information about a specific GEO ID.",
            parameters={
                "type": "object",
                "properties": {
                    "accession": {
                        "type": "string",
                        "description": "GEO accession ID (e.g., GSE204716, GSM123456)",
                    },
                    "download": {
                        "type": "boolean",
                        "description": "Download the dataset after fetching metadata (default: false)",
                    },
                },
                "required": ["accession"],
            },
            surfaces=("bot",),
            read_only=False,
            concurrency_safe=False,
            risk_level=RISK_LEVEL_MEDIUM,
            writes_workspace=True,
            touches_network=True,
            policy_tags=("knowledge", "network"),
        ),
        ToolSpec(
            name="list_directory",
            description="List contents of a directory. Use when user wants to see files in a folder.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path (default: current data directory)"},
                },
            },
            surfaces=("bot",),
            read_only=True,
            concurrency_safe=True,
            policy_tags=("workspace", "inspection"),
        ),
        ToolSpec(
            name="inspect_file",
            description="Display contents of a CSV, JSON, or TXT file. Use when user wants to view file contents.",
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Path to file"},
                    "lines": {"type": "integer", "description": "Number of lines to show (default: 20)"},
                },
                "required": ["file_path"],
            },
            surfaces=("bot",),
            read_only=True,
            concurrency_safe=True,
            policy_tags=("workspace", "inspection"),
        ),
        ToolSpec(
            name="make_directory",
            description="Create a new directory under output/. ONLY use when user explicitly asks to create a folder.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path to create"},
                },
                "required": ["path"],
            },
            surfaces=("bot",),
            read_only=False,
            concurrency_safe=False,
            risk_level=RISK_LEVEL_MEDIUM,
            writes_workspace=True,
            policy_tags=("workspace", "artifact"),
        ),
        ToolSpec(
            name="move_file",
            description="Move or rename a file. ONLY use when user explicitly asks to move or rename files.",
            parameters={
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "Source file path"},
                    "destination": {"type": "string", "description": "Destination path"},
                },
                "required": ["source", "destination"],
            },
            surfaces=("bot",),
            read_only=False,
            concurrency_safe=False,
            risk_level=RISK_LEVEL_MEDIUM,
            approval_mode=APPROVAL_MODE_ASK,
            writes_workspace=True,
            allowed_in_background=False,
            policy_tags=("workspace", "mutation"),
        ),
        ToolSpec(
            name="remove_file",
            description="Delete a file or directory. ONLY use when user explicitly asks to remove files/folders.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File or directory path to remove"},
                },
                "required": ["path"],
            },
            surfaces=("bot",),
            read_only=False,
            concurrency_safe=False,
            risk_level=RISK_LEVEL_HIGH,
            approval_mode=APPROVAL_MODE_ASK,
            writes_workspace=True,
            allowed_in_background=False,
            policy_tags=("workspace", "destructive"),
        ),
        ToolSpec(
            name="get_file_size",
            description="Get file size in MB. Use when user asks about file size.",
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "File path"},
                },
                "required": ["file_path"],
            },
            surfaces=("bot",),
            read_only=True,
            concurrency_safe=True,
            policy_tags=("workspace", "inspection"),
        ),
        ToolSpec(
            name="remember",
            description=(
                "Save important information to persistent memory so you can recall it "
                "in future conversations. Call this (not `task_create`) for "
                "'记住 X' / 'remember X' requests. Use to remember: user preferences "
                "(language, default methods, DPI settings), biological insights "
                "(cell type annotations, spatial domains found), and project context "
                "(research goals, species, tissue type, disease model). "
                "Memory persists across conversations and bot restarts."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "memory_type": {
                        "type": "string",
                        "enum": ["preference", "insight", "project_context"],
                        "description": (
                            "Type of memory to save. "
                            "'preference' = user settings (language, default method, DPI). "
                            "'insight' = biological discovery (cell types, clusters). "
                            "'project_context' = research context (species, tissue, disease, goal)."
                        ),
                    },
                    "key": {
                        "type": "string",
                        "description": (
                            "For preference: setting name (e.g. 'language', 'default_method', 'dpi'). "
                            "For insight: entity ID (e.g. 'cluster_0', 'domain_3'). "
                            "For project_context: not used."
                        ),
                    },
                    "value": {
                        "type": "string",
                        "description": (
                            "For preference: setting value (e.g. 'Chinese', 'tangram', '300'). "
                            "For insight: biological label (e.g. 'T cells', 'tumor region'). "
                            "For project_context: not used."
                        ),
                    },
                    "domain": {
                        "type": "string",
                        "description": "For preference: scope of the setting (e.g. 'global', 'spatial-preprocess'). Default: 'global'.",
                    },
                    "entity_type": {
                        "type": "string",
                        "description": "For insight: type of entity (e.g. 'cluster', 'spatial_domain', 'cell_type').",
                    },
                    "source_analysis_id": {
                        "type": "string",
                        "description": "For insight: ID of the analysis that produced this insight (optional).",
                    },
                    "confidence": {
                        "type": "string",
                        "enum": ["user_confirmed", "ai_predicted"],
                        "description": "For insight: confidence level. Use 'user_confirmed' when user explicitly states a label.",
                    },
                    "project_goal": {
                        "type": "string",
                        "description": "For project_context: research goal/objective.",
                    },
                    "species": {
                        "type": "string",
                        "description": "For project_context: species (e.g. 'human', 'mouse').",
                    },
                    "tissue_type": {
                        "type": "string",
                        "description": "For project_context: tissue type (e.g. 'brain', 'liver', 'tumor').",
                    },
                    "disease_model": {
                        "type": "string",
                        "description": "For project_context: disease model (e.g. 'breast cancer', 'Alzheimer').",
                    },
                },
                "required": ["memory_type"],
            },
            surfaces=("bot",),
            context_params=("session_id",),
            read_only=False,
            concurrency_safe=False,
            result_policy=RESULT_POLICY_MEMORY_WRITE,
            risk_level=RISK_LEVEL_MEDIUM,
            policy_tags=("memory", "knowledge"),
        ),
        ToolSpec(
            name="recall",
            description=(
                "Retrieve information from persistent memory. Use this to recall: "
                "user preferences, biological insights, project context, dataset info, "
                "and analysis history saved in previous conversations. "
                "You can search by keyword or list memories by type."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Search query to find specific memories. "
                            "E.g. 'language preference', 'cluster annotations', 'species'."
                        ),
                    },
                    "memory_type": {
                        "type": "string",
                        "enum": ["preference", "insight", "project_context", "dataset", "analysis"],
                        "description": "Optional: filter by memory type.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of memories to return (default: 10).",
                    },
                },
            },
            surfaces=("bot",),
            context_params=("session_id",),
            read_only=True,
            concurrency_safe=True,
            policy_tags=("memory", "knowledge"),
        ),
        ToolSpec(
            name="forget",
            description=(
                "Remove a specific memory from persistent storage. "
                "Use when user explicitly asks to forget or remove a saved preference, "
                "insight, or other memory. Provide a query or memory_id to identify "
                "which memory to remove."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "memory_id": {
                        "type": "string",
                        "description": "The memory_id of the memory to forget (if known).",
                    },
                    "query": {
                        "type": "string",
                        "description": "Search query to find the memory to forget.",
                    },
                },
            },
            surfaces=("bot",),
            context_params=("session_id",),
            read_only=False,
            concurrency_safe=False,
            result_policy=RESULT_POLICY_MEMORY_WRITE,
            risk_level=RISK_LEVEL_MEDIUM,
            policy_tags=("memory", "knowledge"),
        ),
        ToolSpec(
            name="read_knowhow",
            description=(
                "Fetch the full markdown body of a Mandatory Scientific Constraint "
                "(KnowHow guard) by name. Call this when the headline-only "
                "Active Guards block in the system prompt does not give enough "
                "detail — for example, when the user asks WHY a guard requires a "
                "specific threshold, when a method name is ambiguous, or when "
                "the headline mentions parameters the user is uncertain about. "
                "Accepts the filename (e.g. KH-sc-de-guardrails.md), the doc_id "
                "(sc-de-guardrails), or the human-readable label. Returns the "
                "full markdown text, or an empty string if no guard matches."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "KH identifier. Accepts filename "
                            "(KH-sc-de-guardrails.md), doc_id (sc-de-guardrails), "
                            "or label (Single-Cell Differential Expression Guardrails)."
                        ),
                    },
                },
                "required": ["name"],
            },
            surfaces=("bot",),
            read_only=True,
            concurrency_safe=True,
            result_policy=RESULT_POLICY_KNOWLEDGE_REFERENCE,
            policy_tags=("knowledge", "knowhow", "reference"),
        ),
        ToolSpec(
            name="consult_knowledge",
            description=(
                "Query the OmicsClaw knowledge base for analysis guidance. "
                "Use this PROACTIVELY when: (1) user is unsure which analysis to run, "
                "(2) user asks about method selection or parameters, "
                "(3) an analysis fails and user needs troubleshooting, "
                "(4) user asks 'how to' or 'which method' questions, "
                "(5) before running complex analyses to check best practices. "
                "Returns relevant decision guides, best practices, or troubleshooting advice."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language question about methodology, parameters, or troubleshooting",
                    },
                    "category": {
                        "type": "string",
                        "enum": [
                            "decision-guide", "best-practices", "troubleshooting",
                            "workflow", "method-reference", "interpretation",
                            "preprocessing-qc", "statistics", "tool-setup",
                            "domain-knowledge", "knowhow", "reference-script", "all",
                        ],
                        "description": "Filter by document type. Default: 'all'",
                    },
                    "domain": {
                        "type": "string",
                        "enum": [
                            "spatial", "singlecell", "genomics", "proteomics",
                            "metabolomics", "bulkrna", "general", "all",
                        ],
                        "description": "Filter by omics domain. Default: 'all'",
                    },
                },
                "required": ["query"],
            },
            surfaces=("bot",),
            read_only=True,
            concurrency_safe=True,
            result_policy=RESULT_POLICY_KNOWLEDGE_REFERENCE,
            policy_tags=("knowledge", "reference"),
        ),
        # ---- OmicsClaw-KG read tools (Bench Phase 3.1, ADR 0019) -------------
        # Read-only knowledge-graph retrieval over the cross-research reading
        # base. Always registered; their executors soft-fail when the optional
        # ``omicsclaw_kg`` package is absent. Read-stage allow-listed (registry.py).
        ToolSpec(
            name="kg_search",
            description=(
                "Search the OmicsClaw knowledge graph — a wiki + graph built from "
                "previously-ingested papers and reading notes — with BM25 ranking. "
                "Use when the user asks what is already known about a topic, gene, "
                "method, or hypothesis, or to find source pages before reading them. "
                "Returns ranked page hits (page_type/slug, title, score)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Free-text search query.",
                    },
                    "page_type": {
                        "type": "string",
                        "enum": list(_KG_PAGE_TYPES),
                        "description": "Restrict to one wiki page type. Omit to search all.",
                    },
                    "state": {
                        "type": "string",
                        "description": "Restrict to pages with this lifecycle state.",
                    },
                    "status": {
                        "type": "string",
                        "enum": [
                            "draft", "submitted", "testing",
                            "validated", "refuted", "refined",
                        ],
                        "description": "Restrict hypothesis pages to this status.",
                    },
                    "field": {
                        "type": "string",
                        "enum": ["title", "body", "frontmatter"],
                        "description": "Restrict scoring to a single field.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results. Default 10.",
                    },
                },
                "required": ["query"],
            },
            surfaces=("bot",),
            read_only=True,
            concurrency_safe=True,
            result_policy=RESULT_POLICY_KNOWLEDGE_REFERENCE,
            policy_tags=("knowledge", "graph", "reference"),
        ),
        ToolSpec(
            name="kg_get_page",
            description=(
                "Fetch one knowledge-graph wiki page (frontmatter + body) by type "
                "and slug — typically a hit returned by `kg_search`. Use to read the "
                "full content of a source, concept, method, or hypothesis page."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "page_type": {
                        "type": "string",
                        "enum": list(_KG_PAGE_TYPES),
                        "description": "The page's wiki type.",
                    },
                    "slug": {
                        "type": "string",
                        "description": "The page slug (filename without .md).",
                    },
                    "include_notes": {
                        "type": "boolean",
                        "description": "Include the human-owned `## Notes` section. Default false.",
                    },
                },
                "required": ["page_type", "slug"],
            },
            surfaces=("bot",),
            read_only=True,
            concurrency_safe=True,
            result_policy=RESULT_POLICY_KNOWLEDGE_REFERENCE,
            policy_tags=("knowledge", "graph", "reference"),
        ),
        ToolSpec(
            name="kg_list_pages",
            description=(
                "List knowledge-graph pages of a given type, optionally filtered by "
                "lifecycle state or hypothesis status. Use to browse what exists in a "
                "category (e.g. all hypotheses still in `testing`)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "page_type": {
                        "type": "string",
                        "enum": list(_KG_PAGE_TYPES),
                        "description": "Which wiki page type to list.",
                    },
                    "state": {
                        "type": "string",
                        "description": "Filter by frontmatter `state`.",
                    },
                    "status": {
                        "type": "string",
                        "enum": [
                            "draft", "submitted", "testing",
                            "validated", "refuted", "refined",
                        ],
                        "description": "Filter hypothesis pages by status.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max entries. Default 50.",
                    },
                },
                "required": ["page_type"],
            },
            surfaces=("bot",),
            read_only=True,
            concurrency_safe=True,
            result_policy=RESULT_POLICY_KNOWLEDGE_REFERENCE,
            policy_tags=("knowledge", "graph", "reference"),
        ),
        ToolSpec(
            name="kg_graph_neighbors",
            description=(
                "Return the graph neighborhood of a knowledge-graph node (entities, "
                "concepts, methods and the typed edges between them). Use to explore "
                "how a gene/concept relates to others. `node_id` accepts a typed id "
                "('entity:tp53') or a bare slug ('tp53')."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "node_id": {
                        "type": "string",
                        "description": "Graph node id (typed 'entity:tp53' or bare slug 'tp53').",
                    },
                    "depth": {
                        "type": "integer",
                        "description": "Hop count, 1-3. Default 1.",
                    },
                },
                "required": ["node_id"],
            },
            surfaces=("bot",),
            read_only=True,
            concurrency_safe=True,
            result_policy=RESULT_POLICY_KNOWLEDGE_REFERENCE,
            policy_tags=("knowledge", "graph", "reference"),
        ),
        ToolSpec(
            name="kg_status",
            description=(
                "Report knowledge-graph size: wiki page counts per type, total graph "
                "nodes/edges, and node/edge breakdowns. Use to check whether the "
                "knowledge base has content before searching, or to summarize its scope."
            ),
            parameters={"type": "object", "properties": {}},
            surfaces=("bot",),
            read_only=True,
            concurrency_safe=True,
            result_policy=RESULT_POLICY_KNOWLEDGE_REFERENCE,
            policy_tags=("knowledge", "graph", "reference"),
        ),
        ToolSpec(
            name="kg_recent_log",
            description=(
                "Return recent knowledge-graph activity log entries (newest first) — "
                "ingests, handoffs, recorded results. Use to see what was recently "
                "added to or done in the knowledge base."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max entries. Default 20.",
                    },
                    "event_type": {
                        "type": "string",
                        "description": "Filter to a single event_type (e.g. ingest, handoff).",
                    },
                },
            },
            surfaces=("bot",),
            read_only=True,
            concurrency_safe=True,
            result_policy=RESULT_POLICY_KNOWLEDGE_REFERENCE,
            policy_tags=("knowledge", "graph", "reference"),
        ),
        ToolSpec(
            name="kg_communities",
            description=(
                "Detect knowledge clusters in the graph and return the largest N with "
                "their key nodes. Use to get a high-level map of the major themes in "
                "the knowledge base."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max communities (largest first). Default 10.",
                    },
                    "algorithm": {
                        "type": "string",
                        "enum": ["louvain", "greedy"],
                        "description": "Community detection algorithm. Default louvain.",
                    },
                },
            },
            surfaces=("bot",),
            read_only=True,
            concurrency_safe=True,
            result_policy=RESULT_POLICY_KNOWLEDGE_REFERENCE,
            policy_tags=("knowledge", "graph", "reference"),
        ),
        # KG ingest (Bench Phase 3.3c, RD-INGEST-9) — the one KG *write* tool in
        # Read: builds the citation substrate (Source pages) the agent cites.
        # AUTO-approved and thread-agnostic (ADR 0019: KG is shared reading
        # knowledge; the gated action is the dataset download, not ingest).
        # Soft-fails when KG/LLM is absent (kg_tools.execute_kg_ingest).
        ToolSpec(
            name="kg_ingest",
            description=(
                "Ingest a paper or source into the OmicsClaw knowledge graph, creating "
                "a Source page (and extracting entities / concepts / methods) that later "
                "answers can cite. Use when the user drops or links a paper to read. "
                "Pass `source` as a local file path or http(s) URL, or omit it to ingest "
                "a freshly dropped PDF. The knowledge base is shared across research "
                "(not thread-specific)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": (
                            "A local file path or http(s) URL to ingest. "
                            "Omit to use a dropped PDF."
                        ),
                    },
                },
            },
            surfaces=("bot",),
            context_params=("session_id",),
            read_only=False,
            concurrency_safe=False,
            result_policy=RESULT_POLICY_KNOWLEDGE_REFERENCE,
            risk_level=RISK_LEVEL_MEDIUM,
            writes_workspace=True,  # persists Source pages + raw store to the KG home
            touches_network=True,
            policy_tags=("knowledge", "graph", "ingest"),
        ),
        ToolSpec(
            name="resolve_capability",
            description=(
                "Resolve whether a user request is fully covered by an existing OmicsClaw skill, "
                "partially covered, or not covered. Use this before non-trivial analysis requests "
                "when skill coverage is uncertain."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The user's analysis request in natural language.",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Optional server-side data path or filename mentioned by the user.",
                    },
                    "domain_hint": {
                        "type": "string",
                        "enum": [
                            "spatial", "singlecell", "genomics", "proteomics",
                            "metabolomics", "bulkrna", "",
                        ],
                        "description": "Optional domain hint if the user has already narrowed the domain.",
                    },
                },
                "required": ["query"],
            },
            surfaces=("bot",),
            read_only=True,
            concurrency_safe=True,
            policy_tags=("planning", "routing"),
        ),
        ToolSpec(
            name="web_method_search",
            description=(
                "Search external web sources for up-to-date method documentation, papers, or workflow guidance. "
                "Use this when resolve_capability indicates no_skill or partial_skill and external references are needed."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query describing the method, package, or analysis goal.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of search results to fetch. Default: 3.",
                    },
                    "topic": {
                        "type": "string",
                        "enum": ["general", "news", "finance"],
                        "description": "Search topic. Default: general.",
                    },
                },
                "required": ["query"],
            },
            surfaces=("bot",),
            read_only=True,
            concurrency_safe=True,
            result_policy=RESULT_POLICY_WEB_REFERENCE,
            risk_level=RISK_LEVEL_MEDIUM,
            touches_network=True,
            policy_tags=("knowledge", "network"),
        ),
        ToolSpec(
            name="create_omics_skill",
            description=(
                "Create a new OmicsClaw-native skill scaffold under skills/<domain>/<skill-name>/ "
                "using the canonical v2 layout (SKILL.md + parameters.yaml sidecar + "
                "references/{methodology,output_contract,parameters,r_visualization}.md). "
                "The emitted skill is lint-clean against scripts/skill_lint.py and ready "
                "to register via the runtime registry. "
                "Use this only when the user explicitly wants a reusable new skill added "
                "to OmicsClaw. If a previous custom_analysis_execute run succeeded, you can "
                "promote that notebook into the new skill."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "request": {
                        "type": "string",
                        "description": "Original user request describing the desired reusable skill.",
                    },
                    "skill_name": {
                        "type": "string",
                        "description": "Preferred lowercase hyphenated skill alias.",
                    },
                    "domain": {
                        "type": "string",
                        "enum": [
                            "spatial", "singlecell", "genomics", "proteomics",
                            "metabolomics", "bulkrna", "orchestrator",
                        ],
                        "description": "Target OmicsClaw domain for the new skill. Optional if it can be inferred from a promoted autonomous analysis.",
                    },
                    "summary": {
                        "type": "string",
                        "description": "One-line summary of what the skill should do.",
                    },
                    "source_analysis_dir": {
                        "type": "string",
                        "description": "Optional path to a successful custom_analysis_execute output directory to promote into a skill.",
                    },
                    "promote_from_latest": {
                        "type": "boolean",
                        "description": "If true, promote the most recent successful autonomous analysis output when source_analysis_dir is not provided.",
                    },
                    "input_formats": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of domain-specific input format notes.",
                    },
                    "primary_outputs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of primary outputs the new skill should emit.",
                    },
                    "methods": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of primary methods/backends for the skill.",
                    },
                    "trigger_keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of routing keywords users may say naturally.",
                    },
                    "create_tests": {
                        "type": "boolean",
                        "description": "Whether to generate a minimal test scaffold. Default: true.",
                    },
                },
                "required": ["request"],
            },
            surfaces=("bot",),
            read_only=False,
            concurrency_safe=False,
            risk_level=RISK_LEVEL_HIGH,
            approval_mode=APPROVAL_MODE_ASK,
            writes_workspace=True,
            allowed_in_background=False,
            policy_tags=("repo", "skill", "workspace"),
        ),
        ToolSpec(
            name="autonomous_analysis_execute",
            description=(
                "Recommended generated-code path for partial/no-skill analysis. "
                "Provide the goal and optional local/web context; OmicsClaw writes, "
                "executes, and repairs bounded Python/R code in an isolated autonomous workspace."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "Short statement of the analysis objective.",
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional local context from the conversation or prior skill outputs.",
                    },
                    "web_context": {
                        "type": "string",
                        "description": "Optional external method notes already retrieved by web_method_search.",
                    },
                    "input_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional original data paths. They are referenced, not copied.",
                    },
                    "upstream_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional prior skill output directories to consume as references.",
                    },
                    "language": {
                        "type": "string",
                        "enum": ["python", "r"],
                        "description": "Generated script language. Default: python.",
                    },
                    "max_repair_attempts": {
                        "type": "integer",
                        "description": "Maximum evidence-bound repairs after the first execution. Default: 2, max: 2.",
                    },
                    "data_schema": {
                        "type": "string",
                        "description": (
                            "Inspected input data schema (obs/var/obsm/layers/uns keys, shape, "
                            "platform) already provided to you in context. Pass it through so "
                            "generated code and repairs use real keys instead of guessing."
                        ),
                    },
                    "analysis_plan": {
                        "type": "string",
                        "description": (
                            "Your concrete, data-grounded plan formed before delegating: steps, "
                            "chosen method, expected outputs, and any stated assumptions."
                        ),
                    },
                },
                "required": ["goal"],
            },
            surfaces=("bot",),
            context_params=(
                "session_id",
                "chat_id",
                "surface",
                "policy_state",
                "request_tool_approval",
                "model_override",
                "provider_override",
            ),
            read_only=False,
            concurrency_safe=False,
            result_policy=RESULT_POLICY_SUMMARY_OR_MEDIA,
            risk_level=RISK_LEVEL_HIGH,
            writes_workspace=True,
            allowed_in_background=False,
            policy_tags=("analysis", "workflow", "autonomous", "autonomous_code_runner"),
        ),
        ToolSpec(
            name="custom_analysis_execute",
            description=(
                "Legacy adapter for user-supplied Python snippets in a restricted notebook. "
                "Prefer autonomous_analysis_execute for new generated-code analysis because it owns code generation, "
                "execution, repair, and autonomous workspace reports."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "Short statement of the analysis objective.",
                    },
                    "analysis_plan": {
                        "type": "string",
                        "description": "Concise markdown plan describing what the custom analysis will do.",
                    },
                    "python_code": {
                        "type": "string",
                        "description": "A single self-contained Python snippet to execute inside the restricted notebook.",
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional local context from the conversation or prior skill outputs.",
                    },
                    "web_context": {
                        "type": "string",
                        "description": "Optional external method notes returned by web_method_search.",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Optional input file path to expose as INPUT_FILE inside the notebook.",
                    },
                    "sources": {
                        "type": "string",
                        "description": "Optional markdown list of cited URLs or source notes to persist alongside the notebook.",
                    },
                    "output_label": {
                        "type": "string",
                        "description": "Optional short label for the output directory prefix.",
                    },
                },
                "required": ["goal", "analysis_plan", "python_code"],
            },
            surfaces=("bot",),
            read_only=False,
            concurrency_safe=False,
            risk_level=RISK_LEVEL_HIGH,
            approval_mode=APPROVAL_MODE_ASK,
            writes_workspace=True,
            policy_tags=("analysis", "workflow", "notebook"),
        ),
        ToolSpec(
            name="inspect_data",
            description=(
                "Instantly inspect an AnnData (.h5ad) file's metadata — cell/gene counts, "
                "obs/var columns, embeddings (obsm), layers, uns keys — WITHOUT loading "
                "the expression matrix. Fast even for very large files. "
                "Also supports pre-run method suitability and parameter preview when "
                "`method` (and optionally `skill`) is provided. "
                "ALWAYS call this when the user asks to 'explore', 'inspect', "
                "'what can I do with', or vaguely 'analyze' a dataset WITHOUT specifying "
                "a concrete analysis pipeline. Returns a formatted summary and suggested "
                "analysis directions. Do NOT call omicsclaw for open-ended exploratory queries."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the .h5ad file to inspect.",
                    },
                    "skill": {
                        "type": "string",
                        "description": "Optional skill alias for preflight preview (e.g. spatial-domain-identification).",
                    },
                    "method": {
                        "type": "string",
                        "description": "Optional method name for suitability + parameter preview.",
                    },
                    "preview_params": {
                        "type": "boolean",
                        "description": "If true, include a pre-run method suitability and default parameter preview block.",
                    },
                },
                "required": ["file_path"],
            },
            surfaces=("bot",),
            read_only=True,
            concurrency_safe=True,
            result_policy=RESULT_POLICY_INSPECTION_REFERENCE,
            policy_tags=("inspection", "analysis"),
        ),
    ]
    specs.extend(build_engineering_tool_specs())
    # Phase 1 (tool-list-compression): attach the predicate field to each
    # lazy-load tool from the centralized TOOL_PREDICATE_MAP. The 8
    # always-on tools (omicsclaw, resolve_capability, consult_knowledge,
    # inspect_data, list_directory, glob_files, file_read, read_knowhow)
    # are absent from the map and keep predicate=None. The runtime
    # ``select_tool_specs`` filter then drops the lazy tools whose
    # predicate doesn't fire for the current request.
    from ..predicates import attach_predicates as _attach_predicates

    return list(_attach_predicates(tuple(specs)))


def build_bot_tool_registry(context: BotToolContext) -> ToolRegistry:
    return ToolRegistry(build_bot_tool_specs(context))
