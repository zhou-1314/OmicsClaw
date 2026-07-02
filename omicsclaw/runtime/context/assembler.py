from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

from omicsclaw.skill.registry import ensure_registry_loaded

from ..context.layers import (
    ContextAssemblyRequest,
    ContextLayer,
    ContextLayerInjector,
    get_default_context_injectors,
)

LOGGER = logging.getLogger("omicsclaw.runtime.context")

_DOMAIN_KEYWORDS = {
    "bulkrna": [
        "deseq2", "edger", "limma", "bulk rna", "bulk-rna",
        "differential expression", "rnaseq", "rna-seq", "deg",
        "差异表达", "差异基因", "差异分析",
    ],
    "singlecell": [
        "single cell", "single-cell", "scrna", "scanpy",
        "seurat", "scvi", "cellranger", "10x",
        "单细胞", "单细胞测序",
    ],
    "spatial": [
        "spatial", "visium", "merfish", "slide-seq", "stereo-seq",
        "10x visium", "squidpy",
        "空间转录组", "空间组学",
    ],
    "genomics": [
        "variant", "gwas", "vcf", "plink", "crispr", "depmap",
        "essentiality", "genomic",
        "基因组", "变异", "遗传变异",
    ],
    "proteomics": [
        "proteomic", "mass spec", "peptide", "maxquant",
        "蛋白质组", "蛋白组学",
    ],
    "metabolomics": [
        "metabolomic", "xcms", "mzml", "metabolite",
        "代谢组", "代谢物",
    ],
}


@dataclass(frozen=True, slots=True)
class PromptContextAssembly:
    request: ContextAssemblyRequest
    layers: tuple[ContextLayer, ...]

    def layers_for(self, placement: str) -> tuple[ContextLayer, ...]:
        normalized = str(placement or "").strip().lower()
        return tuple(layer for layer in self.layers if layer.placement == normalized)

    @property
    def system_layers(self) -> tuple[ContextLayer, ...]:
        return self.layers_for("system")

    @property
    def message_layers(self) -> tuple[ContextLayer, ...]:
        return self.layers_for("message")

    @property
    def attachment_layers(self) -> tuple[ContextLayer, ...]:
        return self.layers_for("attachment")

    @property
    def system_prompt(self) -> str:
        return _render_layers(self.system_layers)

    @property
    def message_context(self) -> str:
        return _render_layers(self.message_layers)

    @property
    def attachment_context(self) -> str:
        return _render_layers(self.attachment_layers)

    @property
    def total_estimated_tokens(self) -> int:
        return sum(layer.estimated_tokens for layer in self.layers)

    @property
    def total_chars(self) -> int:
        return sum(layer.cost_chars for layer in self.layers)

    @property
    def layer_names(self) -> tuple[str, ...]:
        return tuple(layer.name for layer in self.layers)

    @property
    def layer_stats(self) -> dict[str, dict[str, Any]]:
        return {
            layer.name: {
                "placement": layer.placement,
                "order": layer.order,
                "cost_chars": layer.cost_chars,
                "estimated_tokens": layer.estimated_tokens,
            }
            for layer in self.layers
        }


@dataclass(frozen=True, slots=True)
class AssembledChatContext:
    session_id: str | None
    memory_context: str
    scoped_memory_context: str
    skill_context: str
    user_text: str
    user_message_content: str | list[dict[str, Any]]
    skill_hint: str
    domain_hint: str
    capability_context: str
    prompt_context: PromptContextAssembly
    system_prompt: str


def _render_layers(layers: tuple[ContextLayer, ...]) -> str:
    return "\n\n".join(layer.content for layer in layers if layer.content).strip()


def message_mentions_term(text: str, term: str) -> bool:
    term = (term or "").strip().lower()
    if not term:
        return False
    if len(term) <= 3 and term.replace("-", "").replace("_", "").isalnum():
        pattern = rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])"
        return bool(re.search(pattern, text))
    return term in text


def should_attach_capability_context(
    text: str,
    *,
    skill_aliases: tuple[str, ...] | None = None,
) -> bool:
    if not text:
        return False

    lower = text.lower()
    aliases = skill_aliases or tuple(ensure_registry_loaded().skills.keys())
    if any(message_mentions_term(lower, alias.lower()) for alias in aliases):
        return True

    return bool(
        re.search(r"\.(h5ad|h5|loom|mzml|fastq|fq|bam|vcf|csv|tsv)\b", lower)
        or any(
            kw in lower
            for kw in (
                "analy",
                "create skill",
                "add skill",
                "scaffold",
                "创建 skill",
                "封装成skill",
                "preprocess",
                "qc",
                "cluster",
                "differential",
                "deconvolution",
                "trajectory",
                "velocity",
                "enrichment",
                "survival",
                "空间",
                "单细胞",
            )
        )
    )


def extract_analysis_hints(
    text: str,
    *,
    skill_aliases: tuple[str, ...] | None = None,
) -> tuple[str, str]:
    if not text:
        return "", ""

    text_lower = text.lower()
    aliases = skill_aliases or tuple(ensure_registry_loaded().skills.keys())

    skill_hint = ""
    for alias in aliases:
        if message_mentions_term(text_lower, alias.lower()):
            skill_hint = alias
            break

    domain_hint = ""
    for domain, keywords in _DOMAIN_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            domain_hint = domain
            break

    return skill_hint, domain_hint


def extract_user_text(user_content: str | list[dict[str, Any]]) -> str:
    if isinstance(user_content, str):
        return user_content
    return " ".join(
        block.get("text", "")
        for block in (user_content or [])
        if isinstance(block, dict) and block.get("type") == "text"
    )


def build_user_message_content(
    user_content: str | list[dict[str, Any]],
    *,
    message_context: str = "",
) -> str | list[dict[str, Any]]:
    message_context = str(message_context or "").strip()

    if isinstance(user_content, str):
        if not message_context:
            return user_content
        return "\n\n".join(
            part
            for part in (
                message_context,
                "## User Request",
                user_content,
            )
            if part
        )

    oai_parts: list[dict[str, Any]] = []
    if message_context:
        oai_parts.append({"type": "text", "text": message_context})
    for block in user_content:
        if block.get("type") == "text":
            oai_parts.append({"type": "text", "text": block["text"]})
        elif block.get("type") == "image":
            src = block.get("source", {})
            data_uri = f"data:{src['media_type']};base64,{src['data']}"
            oai_parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": data_uri},
                }
            )
    return oai_parts


def _default_capability_resolver(query: str, *, domain_hint: str = ""):
    from omicsclaw.skill.capability_resolver import resolve_capability

    return resolve_capability(query, domain_hint=domain_hint)


async def _call_sync_in_background(func, /, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


def assemble_prompt_context(
    *,
    request: ContextAssemblyRequest | None = None,
    injectors: tuple[ContextLayerInjector, ...] | None = None,
    **request_kwargs,
) -> PromptContextAssembly:
    current_request = request or ContextAssemblyRequest(**request_kwargs)
    selected_layers: list[ContextLayer] = []

    for injector in injectors or get_default_context_injectors():
        if not injector.applies(current_request):
            continue
        layer = injector.render(current_request)
        if layer is not None:
            selected_layers.append(layer)

    selected_layers.sort(key=lambda layer: (layer.order, layer.name))
    return PromptContextAssembly(
        request=current_request,
        layers=tuple(selected_layers),
    )


async def assemble_chat_context(
    *,
    chat_id: int | str,
    user_content: str | list[dict[str, Any]],
    user_id: str | None = None,
    platform: str | None = None,
    thread_id: str = "",
    session_manager=None,
    capability_resolver=None,
    skill_aliases: tuple[str, ...] | None = None,
    plan_context: str = "",
    transcript_context: str = "",
    workspace: str = "",
    pipeline_workspace: str = "",
    scoped_memory_scope: str = "",
    mcp_servers: tuple[str, ...] | None = None,
    output_style: str = "",
    omicsclaw_dir: str = "",
    context_injectors: tuple[ContextLayerInjector, ...] | None = None,
    scoped_memory_loader=None,
    research_stance_loader=None,
) -> AssembledChatContext:
    session_id = f"{platform}:{user_id}:{chat_id}" if user_id and platform else None
    background_tasks: list[asyncio.Task[Any]] = []

    def _spawn(coro):
        # F9: register every background task so a cancellation of this
        # coroutine reaps them in the finally below, rather than orphaning the
        # ones not currently being awaited (asyncio auto-cancels only the task
        # a coroutine is actively awaiting via its _fut_waiter, not its siblings).
        task = asyncio.create_task(coro)
        background_tasks.append(task)
        return task

    try:
        memory_context = ""
        project_state_context = ""
        memory_task = None
        if session_manager and session_id:
            async def _load_session_memory() -> tuple[str, str]:
                # Bench (ADR 0023 d3 / AN-CTXRECALL-11): stamp a freshly-created
                # session with the active thread (no-op for an already-bound one —
                # the binding is immutable) and scope the passive memory injection
                # to that thread. thread_id="" preserves the legacy un-scoped load.
                await session_manager.get_or_create(
                    user_id, platform, str(chat_id), thread_id=thread_id
                )
                # Decision-2: split durable identity (→system, cache-warm) from
                # volatile work-state (→message, ADR 0024 Volatile). Fall back to
                # the whole string in the system layer for session managers that
                # only implement load_context (byte-identical to legacy).
                if hasattr(session_manager, "load_context_layers"):
                    return await session_manager.load_context_layers(
                        session_id, thread_id=thread_id
                    )
                return (
                    await session_manager.load_context(session_id, thread_id=thread_id),
                    "",
                )

            memory_task = _spawn(_load_session_memory())

        user_text = extract_user_text(user_content)
        skill_hint, domain_hint = extract_analysis_hints(
            user_text,
            skill_aliases=skill_aliases,
        )

        capability_context = ""
        capability_decision = None
        capability_task = None
        if should_attach_capability_context(user_text, skill_aliases=skill_aliases):
            resolver = capability_resolver or _default_capability_resolver
            capability_task = _spawn(
                _call_sync_in_background(
                    resolver,
                    user_text,
                    domain_hint=domain_hint,
                )
            )

        if memory_task is not None:
            try:
                memory_context, project_state_context = await memory_task
            except Exception as exc:
                LOGGER.warning("Session memory context preparation failed (non-fatal): %s", exc)

        if capability_task is not None:
            try:
                capability_decision = await capability_task
                capability_context = capability_decision.to_prompt_block()
                if not skill_hint and getattr(capability_decision, "chosen_skill", ""):
                    skill_hint = capability_decision.chosen_skill
                if not domain_hint and getattr(capability_decision, "domain", ""):
                    domain_hint = capability_decision.domain
            except Exception as exc:
                LOGGER.warning("Capability resolution context failed (non-fatal): %s", exc)

        surface = "interactive" if platform in {"cli", "tui"} else "bot"
        prompt_pack_context = ""
        scoped_memory_context = ""
        skill_context = ""

        prompt_pack_task = None
        if omicsclaw_dir:
            def _load_prompt_pack() -> str:
                from omicsclaw.extensions import build_prompt_pack_context

                return build_prompt_pack_context(
                    omicsclaw_dir,
                    surface=surface,
                    skill=skill_hint,
                    query=user_text[:200] if user_text else "",
                    domain=domain_hint,
                )

            prompt_pack_task = _spawn(_call_sync_in_background(_load_prompt_pack))

        scoped_memory_task = None
        if workspace or pipeline_workspace:
            loader = scoped_memory_loader
            if loader is None:
                from omicsclaw.memory.scoped_memory_select import load_scoped_memory_context as _load_scoped_memory_context

                loader = _load_scoped_memory_context

            scoped_memory_task = _spawn(
                _call_sync_in_background(
                    loader,
                    query=user_text[:200] if user_text else "",
                    domain=domain_hint,
                    workspace=workspace,
                    pipeline_workspace=pipeline_workspace,
                    preferred_scope=scoped_memory_scope,
                )
            )

        skill_context_task = None
        skill_candidates = tuple(
            candidate.skill
            for candidate in getattr(capability_decision, "skill_candidates", [])[:3]
            if getattr(candidate, "skill", "")
        )
        if not skill_candidates and skill_hint:
            skill_candidates = (skill_hint,)
        if skill_hint or skill_candidates:
            def _load_skill_context() -> str:
                from .layers import load_skill_context

                return load_skill_context(
                    skill=skill_hint,
                    query=user_text[:200] if user_text else "",
                    domain=domain_hint,
                    candidate_skills=skill_candidates,
                )

            skill_context_task = _spawn(
                _call_sync_in_background(_load_skill_context)
            )

        if prompt_pack_task is not None:
            try:
                prompt_pack_context = await prompt_pack_task
            except Exception as exc:
                LOGGER.warning("Prompt-pack context preparation failed (non-fatal): %s", exc)

        if scoped_memory_task is not None:
            try:
                recall = await scoped_memory_task
                if recall is None:
                    scoped_memory_context = ""
                elif hasattr(recall, "to_context_text"):
                    scoped_memory_context = str(recall.to_context_text()).strip()
                else:
                    scoped_memory_context = str(recall).strip()
            except Exception as exc:
                LOGGER.warning("Scoped memory context preparation failed (non-fatal): %s", exc)

        if skill_context_task is not None:
            try:
                skill_context = str(await skill_context_task).strip()
            except Exception as exc:
                LOGGER.warning("Skill context prefetch failed (non-fatal): %s", exc)

        # Bench BE-PERSONA-7 — load the agent's research-stance persona layer (opt-in:
        # only when a loader is wired, e.g. the engine loop). Absent / empty → no-op,
        # so callers without a loader (and tests) stay byte-identical.
        # F3: loaded BEFORE the assembly so the stance rides the SINGLE injector pass;
        # the default (no custom builder) path no longer needs a redundant second
        # (legacy-builder) assembly to inject it.
        research_stance = ""
        if research_stance_loader is not None and session_id:
            try:
                research_stance = (await research_stance_loader(session_id)) or ""
            except Exception as exc:
                LOGGER.warning("Research-stance load failed (non-fatal): %s", exc)

        prompt_context = assemble_prompt_context(
            request=ContextAssemblyRequest(
                surface=surface,
                omicsclaw_dir=omicsclaw_dir,
                research_stance=research_stance,
                output_style=output_style,
                memory_context=memory_context,
                skill_context=skill_context,
                scoped_memory_context=scoped_memory_context,
                project_state_context=project_state_context,
                skill=skill_hint,
                skill_candidates=skill_candidates,
                query=user_text[:200] if user_text else "",
                domain=domain_hint,
                capability_context=capability_context,
                plan_context=plan_context,
                prompt_pack_context=prompt_pack_context,
                transcript_context=transcript_context,
                workspace=workspace,
                pipeline_workspace=pipeline_workspace,
                mcp_servers=tuple(mcp_servers or ()),
            ),
            injectors=context_injectors,
        )
        system_prompt = prompt_context.system_prompt

        return AssembledChatContext(
            session_id=session_id,
            memory_context=memory_context,
            scoped_memory_context=scoped_memory_context,
            skill_context=skill_context,
            user_text=user_text,
            user_message_content=build_user_message_content(
                user_content,
                message_context=prompt_context.message_context,
            ),
            skill_hint=skill_hint,
            domain_hint=domain_hint,
            capability_context=capability_context,
            prompt_context=prompt_context,
            system_prompt=system_prompt,
        )
    finally:
        # Only the not-yet-settled tasks need reaping. On the happy path every
        # spawned task was already awaited, so `pending` is empty and the finally
        # adds NO await point — the ready result is returned without a trailing
        # cancellation window (codex F9 P2). On cancel/early-exit, cancel the
        # orphaned siblings and await settlement so they're retrieved and can't
        # surface as "Task was destroyed but it is pending"; the sync to_thread
        # workers self-terminate (their OS threads can't be killed).
        pending = [task for task in background_tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
