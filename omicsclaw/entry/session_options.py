"""Session options state, specialist profiles, reviewer model resolution, and prompt nudges.

Bridges the six OmicsClaw session controls from Desktop/SSH ingress into
OmicsClaw's runtime execution layers:
- ``ultra_mode``: multi-step decomposition & ``general-purpose`` subagent delegation gate
- ``verifier_mode``: background ``ShadowReview`` & ``module-reviewer`` subagent gate
- ``reviewer_model``: model alias/ID resolution & provider rebinding for review calls
- ``memory_mode``: persistent memory section, nudges, compaction extraction & tool gate
- ``specialist``: bioinformatics ecosystem profile injection (scanpy, seurat, etc.)
- ``reasoning_effort``: thinking depth directive complemented by provider wire parameters
"""

from __future__ import annotations

from dataclasses import dataclass, replace as _dc_replace
from typing import Any, Mapping

from omicsclaw.tools.context import current_context

REVIEWER_MODEL_ALIASES: dict[str, str] = {
    "sonnet": "claude-sonnet-4-6",
    "claude sonnet 4.6": "claude-sonnet-4-6",
    "claude-sonnet-4.6": "claude-sonnet-4-6",
    "claude sonnet 4.5": "claude-sonnet-4-5-20250929",
    "opus": "claude-opus-4-6",
    "claude opus 4.6": "claude-opus-4-6",
    "claude-opus-4.6": "claude-opus-4-6",
    "claude opus 4.7": "claude-opus-4-7",
    "claude-opus-4.7": "claude-opus-4-7",
    "haiku": "claude-haiku-4-5-20251001",
    "claude haiku 4.5": "claude-haiku-4-5-20251001",
    "deepseek r1": "deepseek-reasoner",
    "deepseek-r1": "deepseek-reasoner",
    "gpt-4.1": "gpt-4.1",
    "gpt 4.1": "gpt-4.1",
}

SPECIALIST_PROFILES: dict[str, str] = {
    "scanpy": (
        "### Active Specialist Profile: Scanpy (Python Single-Cell Ecosystem)\n"
        "- Prefer Python-native single-cell workflows centered on `scanpy` (`sc`), `anndata` (`AnnData`), `scvi-tools`, and `squidpy`.\n"
        "- Store cell-level metadata in `adata.obs`, gene-level metadata in `adata.var`, raw counts in `adata.layers['counts']` (or `adata.raw`), embeddings in `adata.obsm` (`X_pca`, `X_umap`, `X_scVI`), and graph structures in `adata.obsp`.\n"
        "- Follow standard Scanpy QC and preprocessing conventions (`sc.pp.calculate_qc_metrics`, `sc.pp.normalize_total`, `sc.pp.log1p`, `sc.pp.highly_variable_genes`, `sc.tl.pca`, `sc.pp.neighbors`, `sc.tl.umap`, `sc.tl.leiden`).\n"
        "- Use `sc.tl.rank_genes_groups` or `pydeseq2` (pseudobulk) for differential expression and verify layer/matrix state (counts vs log-normalized) before running statistical models."
    ),
    "seurat": (
        "### Active Specialist Profile: Seurat (R Single-Cell Ecosystem)\n"
        "- Prefer R workflows centered on `Seurat` (v5 `Assay5` layer semantics), `SeuratObject`, `Signac`, and `patchwork`/`ggplot2`.\n"
        "- Respect Seurat v5 layer conventions (`LayerData(obj, assay = 'RNA', layer = 'counts')`, `JoinLayers()` before DE when multiple integration layers exist).\n"
        "- Follow canonical Seurat pipelines (`PercentageFeatureSet`, `NormalizeData` or `SCTransform`, `FindVariableFeatures`, `ScaleData`, `RunPCA`, `IntegrateLayers`, `FindNeighbors`, `FindClusters`, `RunUMAP`, `FindAllMarkers`/`FindMarkers`).\n"
        "- Always inspect `DefaultAssay(obj)` and `Layers(obj)` before running downstream marker detection or integration."
    ),
    "bioconductor": (
        "### Active Specialist Profile: Bioconductor (R Genomic & Statistical Ecosystem)\n"
        "- Prefer canonical Bioconductor S4 data containers: `SummarizedExperiment`, `SingleCellExperiment`, `RangedSummarizedExperiment`, `DESeqDataSet`, and `GenomicRanges`.\n"
        "- Use established Bioconductor statistical packages: `DESeq2`, `edgeR`, `limma`/`voom`, `scran`, `scater`, `batchelor`, `SingleR`, `clusterProfiler`, and `ComplexHeatmap`.\n"
        "- Access assays explicitly via `assay(sce, 'counts')` / `logcounts(sce)`, sample metadata via `colData(sce)`, and feature metadata via `rowData(sce)` / `rowRanges(sce)`.\n"
        "- Enforce rigorous statistical design formulas (`design = ~ batch + condition`), dispersion estimation, and multiple-testing correction (Benjamini-Hochberg FDR)."
    ),
    "spatial": (
        "### Active Specialist Profile: Spatial Omics (Visium / Xenium / MERFISH / Slide-seq)\n"
        "- Prefer spatial transcriptomics and proteomics ecosystems: `squidpy`, `spatialdata` (`SpatialData`), `scanpy` (Python) or `SpatialExperiment`, `Seurat` spatial assays, `Giotto`, `RCTD`/`spacexr` (R).\n"
        "- Preserve and validate spatial coordinates (`adata.obsm['spatial']`), tissue image scalefactors (`adata.uns['spatial']`), and spatial neighborhood graphs (`sq.gr.spatial_neighbors`).\n"
        "- Emphasize spatially variable genes (`sq.gr.spatial_autocorr` with Moran's I / Geary's C, `SpatialDE`, `SPARK-X`), cell-type deconvolution (`cell2location`, `RCTD`, `stereoscope`, `tangram`), and niche/neighborhood enrichment (`sq.gr.nhood_enrichment`, `sq.gr.ligrec`).\n"
        "- Check coordinate alignment against histology images and spot/cell resolution before interpreting spatial domains."
    ),
    "multiomics": (
        "### Active Specialist Profile: Multi-Omics Integration (RNA + ATAC / CITE-seq / Spatial Multi-Omics)\n"
        "- Prefer multimodal containers and integration frameworks: `muon` (`MuData`), `scvi-tools` (`MULTIVI`, `TOTALVI`, `PeakVI`), `mofapy2` (`MOFA+`) in Python, or `Seurat` (`Weighted Nearest Neighbor` / `FindMultiModalNeighbors`), `Signac`, and `MultiAssayExperiment` in R.\n"
        "- Keep per-modality preprocessing explicit (`rna` vs `atac` vs `prot` modalities) and verify shared barcode/sample alignment before joint embedding.\n"
        "- Use appropriate modality-specific normalization (e.g., log-normalization/scVI for RNA, TF-IDF + LSI for ATAC, CLR or totalVI for ADT protein counts).\n"
        "- Cross-validate biological conclusions across modalities (e.g., motif accessibility + TF expression + target gene expression)."
    ),
    "single-cell omics": (
        "### Active Specialist Profile: Single-Cell Omics (scRNA-seq / scATAC-seq)\n"
        "- Prefer canonical single-cell ecosystems (`scanpy`/`anndata`/`scvi-tools` in Python; `Seurat` v5/`Signac`/`SingleCellExperiment` in R).\n"
        "- Enforce rigorous single-cell QC (ambient RNA/doublet checks, mitochondrial/ribosomal thresholds, library complexity) and preserve raw counts in dedicated layers (`adata.layers['counts']` or `Assay5` counts layer).\n"
        "- Validate batch integration (`Harmony`, `scVI`, `Seurat IntegrateLayers`) and use replicate-aware pseudobulk differential expression (`pydeseq2`, `DESeq2`, `edgeR`) when biological replicates exist."
    ),
    "spatial transcriptomics": (
        "### Active Specialist Profile: Spatial Transcriptomics (Visium / Xenium / MERFISH / Slide-seq)\n"
        "- Prefer spatial transcriptomics and proteomics ecosystems: `squidpy`, `spatialdata` (`SpatialData`), `scanpy` (Python) or `SpatialExperiment`, `Seurat` spatial assays, `Giotto`, `RCTD`/`spacexr` (R).\n"
        "- Preserve and validate spatial coordinates (`adata.obsm['spatial']`), tissue image scalefactors (`adata.uns['spatial']`), and spatial neighborhood graphs (`sq.gr.spatial_neighbors`).\n"
        "- Emphasize spatially variable genes (`sq.gr.spatial_autocorr` with Moran's I / Geary's C, `SpatialDE`, `SPARK-X`), cell-type deconvolution (`cell2location`, `RCTD`, `stereoscope`, `tangram`), and niche/neighborhood enrichment (`sq.gr.nhood_enrichment`, `sq.gr.ligrec`).\n"
        "- Check coordinate alignment against histology images and spot/cell resolution before interpreting spatial domains."
    ),
    "multi-omics integration": (
        "### Active Specialist Profile: Multi-Omics Integration (RNA + ATAC / CITE-seq / Spatial Multi-Omics)\n"
        "- Prefer multimodal containers and integration frameworks: `muon` (`MuData`), `scvi-tools` (`MULTIVI`, `TOTALVI`, `PeakVI`), `mofapy2` (`MOFA+`) in Python, or `Seurat` (`Weighted Nearest Neighbor` / `FindMultiModalNeighbors`), `Signac`, and `MultiAssayExperiment` in R.\n"
        "- Keep per-modality preprocessing explicit (`rna` vs `atac` vs `prot` modalities) and verify shared barcode/sample alignment before joint embedding.\n"
        "- Use appropriate modality-specific normalization (e.g., log-normalization/scVI for RNA, TF-IDF + LSI for ATAC, CLR or totalVI for ADT protein counts).\n"
        "- Cross-validate biological conclusions across modalities (e.g., motif accessibility + TF expression + target gene expression)."
    ),
}

SPECIALIST_ALIASES: dict[str, str] = {
    "single-cell": "single-cell omics",
    "single_cell": "single-cell omics",
    "sc-omics": "single-cell omics",
    "spatial-transcriptomics": "spatial transcriptomics",
    "spatial omics": "spatial transcriptomics",
    "multi-omics": "multi-omics integration",
    "multi_omics": "multi-omics integration",
}

_REASONING_DIRECTIVES: dict[str, str] = {
    "low": (
        "- **Reasoning Effort (low)**: Prioritize concise, direct execution. Keep planning brief and avoid unnecessary exploratory branches."
    ),
    "medium": (
        "- **Reasoning Effort (medium)**: Balance analytical thoroughness with execution speed. Verify key assumptions before running expensive tools."
    ),
    "high": (
        "- **Reasoning Effort (high)**: Perform deep, systematic analysis. Explicitly verify data dimensions, statistical assumptions, and edge cases before concluding."
    ),
    "max": (
        "- **Reasoning Effort (max)**: Exhaustive scientific rigor. Formulate competing hypotheses, validate intermediate artifacts, check failure modes, and cross-verify quantitative claims before presenting conclusions."
    ),
}


def _parse_optional_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        norm = value.strip().lower()
        if norm in ("true", "1", "yes", "on", "enabled"):
            return True
        if norm in ("false", "0", "no", "off", "disabled"):
            return False
    return None


def _parse_optional_str(value: object) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    return None


@dataclass(frozen=True)
class SessionOptionsState:
    """Typed runtime representation of the OmicsClaw session options."""

    ultra_mode: bool | None = None
    verifier_mode: bool | None = None
    reviewer_model: str | None = None
    memory_mode: bool | None = None
    specialist: str | None = None
    reasoning_effort: str | None = None
    project_instructions: str | None = None

    @classmethod
    def from_mapping(cls, values: Mapping[str, object] | None) -> SessionOptionsState:
        if not values:
            return cls()
        return cls(
            ultra_mode=_parse_optional_bool(values.get("ultra_mode")),
            verifier_mode=_parse_optional_bool(values.get("verifier_mode")),
            reviewer_model=_parse_optional_str(values.get("reviewer_model")),
            memory_mode=_parse_optional_bool(values.get("memory_mode")),
            specialist=_parse_optional_str(values.get("specialist")),
            reasoning_effort=_parse_optional_str(values.get("reasoning_effort")),
            project_instructions=_parse_optional_str(values.get("project_instructions")),
        )

    @classmethod
    def from_context(cls) -> SessionOptionsState:
        ctx = current_context()
        return cls.from_mapping(ctx.values)

    def is_empty(self) -> bool:
        return (
            self.ultra_mode is None
            and self.verifier_mode is None
            and self.reviewer_model is None
            and self.memory_mode is None
            and self.specialist is None
            and self.reasoning_effort is None
            and self.project_instructions is None
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {}
        if self.ultra_mode is not None:
            out["ultra_mode"] = self.ultra_mode
        if self.verifier_mode is not None:
            out["verifier_mode"] = self.verifier_mode
        if self.reviewer_model is not None:
            out["reviewer_model"] = self.reviewer_model
        if self.memory_mode is not None:
            out["memory_mode"] = self.memory_mode
        if self.specialist is not None:
            out["specialist"] = self.specialist
        if self.reasoning_effort is not None:
            out["reasoning_effort"] = self.reasoning_effort
        if self.project_instructions is not None:
            out["project_instructions"] = self.project_instructions
        return out


# Session-keyed ledger of prior SessionOptionsState for transition detection.
_SESSION_OPTIONS_LEDGER: dict[str, SessionOptionsState] = {}


def clear_session_options_ledger(session_id: str | None = None) -> None:
    """Clear stored session option state (for one session or all sessions in tests)."""
    if session_id is None:
        _SESSION_OPTIONS_LEDGER.clear()
    else:
        _SESSION_OPTIONS_LEDGER.pop(session_id, None)


def get_recorded_session_options(session_id: str) -> SessionOptionsState | None:
    """Return the last recorded SessionOptionsState for ``session_id``, if any."""
    return _SESSION_OPTIONS_LEDGER.get(session_id)


def compute_session_option_transitions(
    previous: SessionOptionsState | None,
    current: SessionOptionsState,
) -> tuple[str, ...]:
    """Return human-readable transition descriptions when options change mid-session."""
    if previous is None:
        return ()

    notes: list[str] = []

    if current.ultra_mode != previous.ultra_mode and current.ultra_mode is not None:
        if current.ultra_mode:
            notes.append(
                "Ultra mode was turned ON for this turn: decompose complex multi-stage "
                "analyses into explicit steps and use `task` (`general-purpose`) subagent "
                "delegation when parallel or isolated exploration is beneficial."
            )
        else:
            notes.append(
                "Ultra mode was turned OFF for this turn: stop delegating to `general-purpose` "
                "subagents and execute steps directly in the main session."
            )

    if current.verifier_mode != previous.verifier_mode and current.verifier_mode is not None:
        if current.verifier_mode:
            notes.append(
                "Verifier mode (Auto-review) was turned ON for this turn: independent verification "
                "is active; validate statistical assumptions and invoke `module-reviewer` or "
                "self-audit before presenting final scientific conclusions."
            )
        else:
            notes.append(
                "Verifier mode (Auto-review) was turned OFF for this turn: background shadow "
                "review and automatic `module-reviewer` audits are disabled unless explicitly requested."
            )

    if current.reviewer_model is not None:
        prev_rev = resolve_reviewer_model(previous.reviewer_model)
        curr_rev = resolve_reviewer_model(current.reviewer_model)
        if curr_rev != prev_rev:
            if curr_rev:
                notes.append(
                    f"Reviewer model changed to `{curr_rev}` (from `{prev_rev or 'default'}`)."
                )
            else:
                notes.append(
                    f"Reviewer model reset to `default` (from `{prev_rev}`)."
                )

    if current.memory_mode != previous.memory_mode and current.memory_mode is not None:
        if current.memory_mode:
            notes.append(
                "Memory mode was turned ON for this turn: persistent memory (`MEMORY.md`, "
                "`memory_search`, `memory_write`) is now active."
            )
        else:
            notes.append(
                "Memory mode was turned OFF for this turn: persistent memory injection, "
                "compaction memory extraction, and `memory_search`/`memory_write` tools are disabled."
            )

    if current.specialist is not None:
        prev_spec = (previous.specialist or "").strip().lower()
        curr_spec = (current.specialist or "").strip().lower()
        if curr_spec in ("none", "off", "default"):
            curr_spec = ""
        if prev_spec in ("none", "off", "default"):
            prev_spec = ""
        if curr_spec != prev_spec:
            if curr_spec:
                prev_clause = f" (previously `{previous.specialist}`)" if prev_spec else ""
                notes.append(
                    f"Specialist focus changed to `{current.specialist}`{prev_clause}: apply `{current.specialist}` "
                    "ecosystem conventions for subsequent analysis steps."
                )
            else:
                notes.append(
                    "Specialist focus was cleared: revert to general multi-ecosystem bioinformatics guidance."
                )

    if (
        current.reasoning_effort != previous.reasoning_effort
        and current.reasoning_effort is not None
    ):
        notes.append(
            f"Reasoning effort changed to `{current.reasoning_effort}`."
        )

    return tuple(notes)


def record_session_options_turn(
    session_id: str,
    current: SessionOptionsState,
    *,
    previous_override: SessionOptionsState | None = None,
) -> tuple[str, ...]:
    """Record ``current`` for ``session_id`` and return any mid-session transition notes."""
    if not session_id:
        return ()
    previous = previous_override if previous_override is not None else _SESSION_OPTIONS_LEDGER.get(session_id)
    transitions = compute_session_option_transitions(previous, current)
    if previous is None:
        _SESSION_OPTIONS_LEDGER[session_id] = current
    else:
        merged = SessionOptionsState(
            ultra_mode=current.ultra_mode if current.ultra_mode is not None else previous.ultra_mode,
            verifier_mode=current.verifier_mode if current.verifier_mode is not None else previous.verifier_mode,
            reviewer_model=current.reviewer_model if current.reviewer_model is not None else previous.reviewer_model,
            memory_mode=current.memory_mode if current.memory_mode is not None else previous.memory_mode,
            specialist=current.specialist if current.specialist is not None else previous.specialist,
            reasoning_effort=(
                current.reasoning_effort
                if current.reasoning_effort is not None
                else previous.reasoning_effort
            ),
            project_instructions=(
                current.project_instructions
                if current.project_instructions is not None
                else previous.project_instructions
            ),
        )
        _SESSION_OPTIONS_LEDGER[session_id] = merged
    return transitions


def resolve_reviewer_model(
    reviewer_model: str | None,
    fallback_model: str | None = None,
) -> str:
    """Resolve a short reviewer model alias (e.g. ``sonnet``) or full model ID."""
    raw = (reviewer_model or "").strip()
    if not raw or raw.lower() in ("default", "inherit", "auto", "none"):
        return (fallback_model or "").strip()
    alias = REVIEWER_MODEL_ALIASES.get(raw.lower())
    if alias is not None:
        return alias
    return raw


def bind_reviewer_provider(
    base_provider: Any,
    reviewer_model: str | None,
    *,
    fallback_model: str | None = None,
) -> Any:
    """Return a provider bound to the resolved reviewer model when possible."""
    target_model = resolve_reviewer_model(reviewer_model, fallback_model=fallback_model)
    if not target_model or base_provider is None:
        return base_provider

    factory = getattr(base_provider, "_client_factory", None)
    snapshot = getattr(factory, "snapshot", None)
    allowed_models = getattr(snapshot, "allowed_models", None)
    if allowed_models and target_model not in allowed_models:
        target_low = target_model.lower()
        matched_allowed = next(
            (
                m
                for m in allowed_models
                if isinstance(m, str)
                and (
                    m.lower() == target_low
                    or m.lower().endswith("/" + target_low)
                    or m.lower().startswith(target_low + "-")
                )
            ),
            None,
        )
        if matched_allowed is not None:
            target_model = matched_allowed
        else:
            fallback_snap = getattr(snapshot, "model", None)
            if fallback_snap and fallback_snap in allowed_models:
                target_model = fallback_snap
            else:
                return base_provider

    bind_fn = getattr(base_provider, "bind", None)
    if callable(bind_fn):
        try:
            return bind_fn(model=target_model)
        except Exception:
            pass

    factory = getattr(base_provider, "_client_factory", None)
    cfg = getattr(base_provider, "_config", None)
    if cfg is not None:
        if getattr(cfg, "model", None) == target_model:
            return base_provider
        try:
            return type(base_provider)(_dc_replace(cfg, model=target_model), client_factory=factory)
        except Exception:
            return base_provider

    return base_provider


def resolve_specialist_profile(specialist: str | None) -> str:
    """Return the domain specialist prompt block for ``specialist`` (or empty string)."""
    if not specialist:
        return ""
    key = specialist.strip().lower()
    if not key or key in ("none", "off", "default"):
        return ""
    canonical_key = SPECIALIST_ALIASES.get(key, key)
    profile = SPECIALIST_PROFILES.get(canonical_key)
    if profile is not None:
        return profile
    return (
        f"### Active Specialist Profile: {specialist.strip()}\n"
        f"- Prioritize `{specialist.strip()}` conventions, canonical data containers, "
        f"and validated statistical workflows throughout this session."
    )


def render_tools_session_options_suffix(options: SessionOptionsState) -> str:
    """Render dynamic suffix for the ``tools`` system section based on active session options."""
    lines: list[str] = []
    if options.ultra_mode is True:
        lines.append(
            "- **Ultra Mode (ACTIVE)**: Decompose complex multi-step scientific tasks, "
            "parallelize independent investigations, and proactively delegate isolated "
            "sub-tasks via the `task` (`general-purpose`) subagent tool."
        )
    elif options.ultra_mode is False:
        lines.append(
            "- **Ultra Mode (OFF)**: Execute tasks directly in the main session using primary "
            "tools (`bash`, `read`, `write`, `edit`, `glob`, `grep`). Do NOT delegate work "
            "to the `general-purpose` subagent."
        )

    if options.verifier_mode is True:
        reviewer_label = resolve_reviewer_model(options.reviewer_model) or "configured reviewer model"
        lines.append(
            f"- **Verifier / Auto-Review Mode (ACTIVE, reviewer={reviewer_label})**: "
            "Rigorous verification is enabled. Before finalizing major analytical conclusions "
            "or module outputs, verify statistical assumptions, check artifact integrity, "
            "and invoke `module-reviewer` when appropriate."
        )
    elif options.verifier_mode is False:
        lines.append(
            "- **Verifier / Auto-Review Mode (OFF)**: Background shadow review is disabled "
            "for this turn; do not invoke `module-reviewer` unless the user explicitly asks."
        )

    if options.memory_mode is False:
        lines.append(
            "- **Memory Mode (OFF)**: Persistent session memory (`memory_search` and "
            "`memory_write`) is disabled for this session. Do not call memory tools."
        )

    if not lines:
        return ""
    return "\n\n### Session Control Directives\n" + "\n".join(lines)


def render_planning_session_options_suffix(options: SessionOptionsState) -> str:
    """Render dynamic suffix for the ``planning`` system section based on active session options."""
    blocks: list[str] = []

    if options.ultra_mode is True:
        blocks.append(
            "### Ultra Mode Decomposition Protocol\n"
            "1. Break multi-stage bioinformatics workflows into explicit, verifiable milestones.\n"
            "2. Formulate clear hypotheses and success criteria before executing compute-heavy steps.\n"
            "3. Synthesize findings across steps with quantitative checkpoints."
        )
    elif options.ultra_mode is False:
        blocks.append(
            "### Direct Execution Protocol (Ultra Mode Off)\n"
            "- Prefer direct, linear execution in the primary session without subagent fan-out."
        )

    if options.verifier_mode is True:
        blocks.append(
            "### Scientific Verification Protocol (Auto-Review Active)\n"
            "- Cross-check sample sizes, normalization states (raw counts vs log-normalized), "
            "multiple-testing corrections, and file/artifact existence before reporting results."
        )

    if options.reasoning_effort:
        effort_key = options.reasoning_effort.strip().lower()
        directive = _REASONING_DIRECTIVES.get(
            effort_key,
            f"- **Reasoning Effort ({options.reasoning_effort.strip()})**: calibrate analytical depth accordingly.",
        )
        blocks.append(f"### Reasoning Depth\n{directive}")

    specialist_block = resolve_specialist_profile(options.specialist)
    if specialist_block:
        blocks.append(specialist_block)

    if not blocks:
        return ""
    return "\n\n" + "\n\n".join(blocks)


def render_transition_nudges(transitions: tuple[str, ...] | list[str]) -> str:
    """Format mid-session option transitions as a ``<system-reminder>`` block."""
    if not transitions:
        return ""
    bullet_lines = "\n".join(f"- {item}" for item in transitions)
    return (
        "<system-reminder>\n"
        "Session Options Updated Mid-Session:\n"
        f"{bullet_lines}\n"
        "</system-reminder>"
    )


__all__ = [
    "REVIEWER_MODEL_ALIASES",
    "SPECIALIST_PROFILES",
    "SessionOptionsState",
    "bind_reviewer_provider",
    "clear_session_options_ledger",
    "compute_session_option_transitions",
    "get_recorded_session_options",
    "record_session_options_turn",
    "render_planning_session_options_suffix",
    "render_tools_session_options_suffix",
    "render_transition_nudges",
    "resolve_reviewer_model",
    "resolve_specialist_profile",
]
