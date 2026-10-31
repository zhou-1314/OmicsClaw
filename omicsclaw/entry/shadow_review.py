"""Run shadow review when a checkpoint requests it.

Historical design notes: docs/engineering/runtime-additional-design-notes.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping, Sequence

import inspect

from omicsclaw.memory.artifacts import ArtifactRecord, ArtifactStore
from omicsclaw.schema import Message, Role

from .project_settings import resolve_project_instructions
from .session_options import bind_reviewer_provider, resolve_reviewer_model

if TYPE_CHECKING:  # pragma: no cover - a type-only import
    from omicsclaw.entry.assembly import AgentApp

__all__ = [
    "MAX_CONSECUTIVE_BOUNCES",
    "MAX_REVIEW_NOTE_CHARS",
    "SHADOW_REVIEW_ARTIFACT_THRESHOLD",
    "SHADOW_REVIEW_CELL_THRESHOLD",
    "STRUCTURED_ARTIFACT_KINDS",
    "ShadowReview",
    "ShadowReviewCheckpoint",
    "ShadowReviewNudge",
    "ShadowReviewVerdict",
    "parse_verdict",
]

_log = logging.getLogger(__name__)

SHADOW_REVIEW_ARTIFACT_THRESHOLD = 3
"""New artifacts of one session that trigger a review checkpoint."""

SHADOW_REVIEW_CELL_THRESHOLD = 40
"""Executed kernel cells since last review that trigger a fallback checkpoint."""

MAX_CONSECUTIVE_BOUNCES = 3
"""Maximum consecutive ``fail`` bounces before the convergence breaker trips."""

STRUCTURED_ARTIFACT_KINDS: frozenset[str] = frozenset({"report", "table"})
"""The artifact kinds that read as a structured block."""

MAX_REVIEW_NOTE_CHARS = 1000
"""Longest note a verdict may inject into the next turn's request."""

_REVIEW_LOOKBACK = 25
"""Newest artifact records one checkpoint evaluation reads per session."""

_MAX_ARTIFACT_LINE_CHARS = 200
_MAX_HISTORY_EXCERPT_CHARS = 4000
_MAX_TOKENS = 256

_VERDICT_LINE = re.compile(
    r"\A\s*(?:\*\*)?VERDICT\s*:\s*(PASS|WARN|FAIL)(?:\*\*)?(?=\s|$)",
    re.IGNORECASE,
)

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _strip_controls(text: str) -> str:
    return _CONTROL_CHARS.sub("", text)


@dataclass(frozen=True, slots=True)
class ShadowReviewVerdict:
    """One reviewer answer: a verdict word and a bounded note."""

    verdict: str
    note: str = ""


def parse_verdict(text: str) -> ShadowReviewVerdict:
    """Read a reviewer answer as a verdict, quietly."""
    raw = str(text or "")
    match = _VERDICT_LINE.match(raw)
    if match is None:
        if raw.strip():
            _log.info("shadow review: unreadable reviewer answer (%r)", raw[:120])
        return ShadowReviewVerdict("pass")
    note = raw.split("\n", 1)[1].strip() if "\n" in raw else ""
    return ShadowReviewVerdict(
        match[1].lower(),
        _strip_controls(note)[:MAX_REVIEW_NOTE_CHARS],
    )


@dataclass(slots=True)
class ShadowReviewCheckpoint:
    """One session's window of new artifacts and kernel cells since its last review."""

    session_id: str
    _watermark: ArtifactRecord | None = None
    _batch: list[ArtifactRecord] = field(default_factory=list)
    min_kernel_cells: int = SHADOW_REVIEW_CELL_THRESHOLD
    min_interval_s: float = 0.0
    _kernel_cells_since_review: int = 0
    _last_review_ts: float = 0.0

    def record_kernel_cells(self, count: int = 1) -> None:
        """Record executed kernel cells toward the fallback checkpoint threshold."""
        if count > 0:
            self._kernel_cells_since_review += int(count)

    def evaluate(self, records: Sequence[ArtifactRecord]) -> list[ArtifactRecord]:
        """Fold *records* (newest first) into the window and answer the batch a trigger fires."""
        now = time.monotonic()
        if not records:
            if (
                self.min_kernel_cells > 0
                and self._kernel_cells_since_review >= self.min_kernel_cells
                and self._batch
                and (self.min_interval_s <= 0 or self._last_review_ts <= 0 or (now - self._last_review_ts) >= self.min_interval_s)
            ):
                fired = self._batch
                self._batch = []
                self._kernel_cells_since_review = 0
                self._last_review_ts = now
                return fired
            return []
        newest = records[0]
        if self._watermark is None:
            self._watermark = newest
            if (
                self.min_kernel_cells > 0
                and self._kernel_cells_since_review >= self.min_kernel_cells
                and (self.min_interval_s <= 0 or self._last_review_ts <= 0 or (now - self._last_review_ts) >= self.min_interval_s)
            ):
                self._kernel_cells_since_review = 0
                self._last_review_ts = now
                return list(records[: min(len(records), SHADOW_REVIEW_ARTIFACT_THRESHOLD)])
            return []
        new = [r for r in records if _newer(r, self._watermark)]
        overflow = len(records) >= _REVIEW_LOOKBACK and len(new) == len(records)
        self._watermark = newest
        if new:
            self._batch[0:0] = new
        structured = any(record.kind in STRUCTURED_ARTIFACT_KINDS for record in new)
        cell_trigger = (
            self.min_kernel_cells > 0
            and self._kernel_cells_since_review >= self.min_kernel_cells
            and bool(self._batch or records)
        )
        if not new and not cell_trigger:
            return []
        if (
            self.min_interval_s > 0
            and self._last_review_ts > 0
            and (now - self._last_review_ts) < self.min_interval_s
            and not overflow
        ):
            return []
        if (
            overflow
            or structured
            or len(self._batch) >= SHADOW_REVIEW_ARTIFACT_THRESHOLD
            or cell_trigger
        ):
            fired = self._batch if self._batch else list(records[: min(len(records), SHADOW_REVIEW_ARTIFACT_THRESHOLD)])
            self._batch = []
            self._kernel_cells_since_review = 0
            self._last_review_ts = now
            return fired
        return []


def _newer(record: ArtifactRecord, watermark: ArtifactRecord) -> bool:
    return (record.created_at, record.id) > (
        watermark.created_at,
        watermark.id,
    )


class ShadowReviewNudge:
    """The one-shot context note a verdict left for a session's next turn."""

    __slots__ = ("_notes", "_session_id")

    def __init__(self, notes: "dict[str, str]", session_id: str) -> None:
        self._notes = notes
        self._session_id = session_id

    async def augment(
        self,
        history: Sequence[Message],
        tools: Sequence[Any] = (),
    ) -> tuple[Message, ...]:
        note = self._notes.pop(self._session_id, None)
        if not note:
            return ()
        return (Message(role=Role.USER, content=note),)


_ReviewCall = Callable[
    [str, Sequence[ArtifactRecord], Sequence[Message]],
    Awaitable[ShadowReviewVerdict],
]


class _FindingDict(dict):
    """Dict subclass supporting attribute access (.id, .resolution_note, .status, etc.)."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            if name in ("resolution_note", "addressed_note", "evidence", "category", "notebook_id", "cell_id", "execution_id"):
                return ""
            raise AttributeError(name) from exc


@dataclass(slots=True)
class ShadowReview:
    """The checkpoint detector and reviewer, one instance per deployment."""

    _app: "AgentApp"
    _model: str
    _timeout_s: float
    _review_call: _ReviewCall | None = None
    _store: ArtifactStore | None = None
    _checkpoints: dict[str, ShadowReviewCheckpoint] = field(default_factory=dict)
    _notes: dict[str, str] = field(default_factory=dict)
    _last_reviewer_model: str = ""
    _max_consecutive_bounces: int = MAX_CONSECUTIVE_BOUNCES
    _warn_mode: str = "nudge"
    _consecutive_bounces: dict[str, int] = field(default_factory=dict)
    _findings: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    _workspace: Path | None = None
    _min_interval_s: float = 0.0

    def __init__(
        self,
        app: "AgentApp" | None = None,
        *,
        workspace: Path | str | None = None,
        model: str = "",
        timeout_s: float = 30.0,
        review_call: _ReviewCall | None = None,
        max_consecutive_bounces: int = MAX_CONSECUTIVE_BOUNCES,
        warn_mode: str = "nudge",
        min_interval_s: float = 0.0,
    ) -> None:
        self._app = app
        self._workspace = Path(workspace) if workspace is not None else None
        self._model = model or getattr(getattr(app, "config", None), "shadow_review_model", "")
        self._timeout_s = timeout_s
        self._review_call = review_call
        self._store: ArtifactStore | None = None
        self._checkpoints: dict[str, ShadowReviewCheckpoint] = {}
        self._notes: dict[str, str] = {}
        self._last_reviewer_model: str = ""
        self._max_consecutive_bounces = max(1, int(max_consecutive_bounces))
        self._warn_mode = warn_mode
        self._consecutive_bounces: dict[str, int] = {}
        self._findings: dict[str, list[dict[str, Any]]] = {}
        self._min_interval_s = float(min_interval_s)

    def _resolve_workspace(self) -> Path | None:
        if self._workspace is not None:
            return self._workspace
        config = getattr(self._app, "config", None)
        workspace = getattr(config, "workspace", None) if config is not None else None
        return Path(workspace) if workspace else None

    def bind_store(self, store: ArtifactStore) -> None:
        """Point the detector at the desktop's artifact store."""
        self._store = store

    @property
    def _artifacts(self) -> ArtifactStore | None:
        return self._store

    @_artifacts.setter
    def _artifacts(self, store: ArtifactStore | None) -> None:
        self._store = store

    def record_kernel_cells(self, session_id: str, count: int = 1) -> None:
        """Record executed kernel cells for *session_id*."""
        checkpoint = self._checkpoints.get(session_id)
        if checkpoint is None:
            checkpoint = ShadowReviewCheckpoint(session_id, min_interval_s=self._min_interval_s)
            self._checkpoints[session_id] = checkpoint
        checkpoint.record_kernel_cells(count)

    def consecutive_bounces(self, session_id: str) -> int:
        """Return consecutive ``fail`` bounce count for *session_id*."""
        return self._consecutive_bounces.get(session_id, 0)

    def record_bounce(self, session_id: str) -> int:
        """Increment consecutive bounce count for *session_id* and return the new count."""
        cnt = self._consecutive_bounces.get(session_id, 0) + 1
        self._consecutive_bounces[session_id] = cnt
        return cnt

    def reset_bounces(self, session_id: str) -> None:
        """Reset consecutive bounce count for *session_id*."""
        self._consecutive_bounces[session_id] = 0

    def convergence_broken(self, session_id: str) -> bool:
        """Return True when *session_id* reached or exceeded ``max_consecutive_bounces``."""
        return self._consecutive_bounces.get(session_id, 0) >= self._max_consecutive_bounces

    def is_convergence_tripped(self, session_id: str) -> bool:
        """Alias for :meth:`convergence_broken`."""
        return self.convergence_broken(session_id)

    def _sync_findings_from_disk(self, session_id: str) -> None:
        ws = self._resolve_workspace()
        if ws is None or not ws.is_dir():
            return
        for rel in (".omicsclaw/provenance/findings.json", "provenance/findings.json"):
            fp = ws / rel
            if not fp.is_file():
                continue
            try:
                raw = json.loads(fp.read_text(encoding="utf-8"))
                disk_items = raw.get("findings", []) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
                by_id = {str(x.get("id")): x for x in disk_items if isinstance(x, dict) and x.get("id")}
                mem_items = self._findings.setdefault(session_id, [])
                mem_ids = {str(x.get("id")) for x in mem_items if isinstance(x, dict)}
                for item in mem_items:
                    did = str(item.get("id") or "")
                    if did in by_id:
                        d_item = by_id[did]
                        d_status = str(d_item.get("status") or "unresolved")
                        if d_status in ("addressed", "claimed", "resolved"):
                            item["status"] = "addressed"
                        else:
                            item["status"] = d_status
                        for k in ("resolution_note", "addressed_note", "addressed_at"):
                            if k in d_item:
                                item[k] = d_item[k]
                for did, d_item in by_id.items():
                    if did not in mem_ids and d_item.get("session_id", session_id) == session_id:
                        mem_items.append(_FindingDict(d_item))
                break
            except Exception:  # noqa: BLE001
                continue

    def findings_for(
        self,
        session_id: str,
        status: str | None = "unresolved",
    ) -> list[_FindingDict]:
        """Return reviewer findings for *session_id*, optionally filtered by *status*."""
        self._sync_findings_from_disk(session_id)
        items = [_FindingDict(f) for f in self._findings.get(session_id, [])]
        if status and status != "all":
            if status == "addressed":
                items = [f for f in items if f.get("status", "unresolved") in ("addressed", "claimed", "resolved")]
            elif status == "unresolved":
                items = [f for f in items if f.get("status", "unresolved") in ("unresolved", "open", "unaddressed")]
            else:
                items = [f for f in items if f.get("status", "unresolved") == status]
        return items

    def mark_findings_addressed(
        self,
        session_id: str,
        ids: Sequence[str] | str,
        note: str = "",
    ) -> list[_FindingDict]:
        """Mark one or more findings as addressed and sync to ``findings.json``."""
        self._sync_findings_from_disk(session_id)
        id_set = {ids} if isinstance(ids, str) else set(ids)
        items = self._findings.get(session_id, [])
        now = time.time()
        for item in items:
            if item.get("id") in id_set or "*" in id_set:
                item["status"] = "addressed"
                item["resolution_note"] = str(note or "")
                item["addressed_note"] = str(note or "")
                item["addressed_at"] = now
        self._persist_findings(session_id)
        return [_FindingDict(f) for f in items]

    def _record_finding(
        self,
        session_id: str = "default",
        verdict: ShadowReviewVerdict | None = None,
        records: Sequence[ArtifactRecord] = (),
        *,
        severity: str = "",
        summary: str = "",
        category: str = "",
        evidence: str = "",
        notebook_id: str = "",
        cell_id: str = "",
        execution_id: str = "",
    ) -> _FindingDict | None:
        if verdict is None:
            v_str = "fail" if severity in ("fail", "error", "critical") else ("warn" if severity in ("warn", "warning") else (severity or "warn"))
            verdict = ShadowReviewVerdict(verdict=v_str, note=summary)
        if verdict.verdict == "pass":
            self._consecutive_bounces[session_id] = 0
            return None

        finding_list = self._findings.setdefault(session_id, [])
        finding_id = f"finding_{len(finding_list) + 1}_{uuid.uuid4().hex[:6]}"
        bounces = self._consecutive_bounces.get(session_id, 0)
        convergence_tripped = False
        if verdict.verdict == "fail":
            bounces += 1
            self._consecutive_bounces[session_id] = bounces
            if bounces >= self._max_consecutive_bounces:
                convergence_tripped = True
        else:
            self._consecutive_bounces[session_id] = 0

        finding_entry = _FindingDict(
            {
                "id": finding_id,
                "session_id": session_id,
                "notebook_id": notebook_id,
                "cell_id": cell_id,
                "execution_id": execution_id,
                "category": category,
                "evidence": evidence,
                "severity": severity or verdict.verdict,
                "summary": summary or verdict.note or "no details were given",
                "status": "unresolved",
                "created_at": time.time(),
                "consecutive_bounces": self._consecutive_bounces.get(session_id, 0),
                "convergence_broken": convergence_tripped,
                "artifacts": [str(r.path) for r in records[:12]],
            }
        )
        finding_list.append(finding_entry)
        self._persist_findings(session_id)

        if convergence_tripped:
            self._notes.pop(session_id, None)
            _log.warning(
                "shadow review: convergence breaker tripped for session %s after %d consecutive fail verdicts",
                session_id,
                bounces,
            )
        elif verdict.verdict == "fail" or self._warn_mode != "findings_only":
            self._notes[session_id] = _verdict_note(verdict)
        return finding_entry

    def _persist_findings(self, session_id: str) -> None:
        ws = self._resolve_workspace()
        if ws is None or not ws.is_dir():
            return
        all_items = self._findings.get(session_id, [])
        for rel in (".omicsclaw/provenance/findings.json", "provenance/findings.json"):
            fp = ws / rel
            try:
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text(json.dumps(all_items, indent=2, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass

    def evaluate(self, session_id: str) -> list[ArtifactRecord]:
        """The triggering batch of new artifacts for *session_id*, if any."""
        if self._store is None:
            return []
        try:
            records = self._store.list_artifacts(
                session_id=session_id, limit=_REVIEW_LOOKBACK
            )
        except Exception as error:  # noqa: BLE001
            _log.info(
                "shadow review: checkpoint read failed for session %s (%s)",
                session_id,
                type(error).__name__,
            )
            return []
        checkpoint = self._checkpoints.get(session_id)
        if checkpoint is None:
            checkpoint = ShadowReviewCheckpoint(session_id, min_interval_s=self._min_interval_s)
            self._checkpoints[session_id] = checkpoint
        return checkpoint.evaluate(records)

    def _pick_provider(self, reviewer_model: str | None = None) -> Any:
        """Resolve the provider for a shadow review call, honouring ``reviewer_model``."""
        config = getattr(self._app, "config", None)
        fallback = (
            self._model
            or (getattr(config, "shadow_review_model", "") if config is not None else "")
        )
        resolved = resolve_reviewer_model(reviewer_model, fallback_model=fallback)
        self._last_reviewer_model = resolved

        role_selector = getattr(self._app, "provider_for_role", None)
        base_prov = None
        if callable(role_selector):
            try:
                base_prov = role_selector("shadow_review")
            except Exception:
                base_prov = None
        if base_prov is None:
            base_prov = getattr(self._app, "provider", None)

        if base_prov is not None:
            if resolved:
                return bind_reviewer_provider(base_prov, resolved)
            return base_prov

        from omicsclaw.provider.factory import provider_from_env

        if config is None:
            raise RuntimeError("shadow review has no provider or config")
        return provider_from_env(
            config.provider,
            model=resolved or config.model,
            max_tokens=_MAX_TOKENS,
        )

    def fire(
        self,
        session_id: str,
        records: Sequence[ArtifactRecord],
        *,
        history: Sequence[Message] = (),
        publisher: Callable[[ShadowReviewVerdict], None] | None = None,
        reviewer_model: str | None = None,
        project_instructions: str | None = None,
    ) -> "asyncio.Task[None]":
        """Schedule one review for *records* and return its task."""
        return asyncio.create_task(
            self._review(
                session_id,
                records,
                history,
                publisher,
                reviewer_model=reviewer_model,
                project_instructions=project_instructions,
            )
        )

    async def _review(
        self,
        session_id: str,
        records: Sequence[ArtifactRecord],
        history: Sequence[Message],
        publisher: Callable[[ShadowReviewVerdict], None] | None,
        *,
        reviewer_model: str | None = None,
        project_instructions: str | None = None,
    ) -> None:
        try:
            verdict = await self._run_review_call(
                session_id,
                records,
                history,
                reviewer_model=reviewer_model,
                project_instructions=project_instructions,
            )
        except Exception as error:  # noqa: BLE001
            _log.info(
                "shadow review: reviewer call failed for session %s (%s)",
                session_id,
                type(error).__name__,
            )
            return
        if verdict.verdict == "pass":
            self._consecutive_bounces[session_id] = 0
            return

        self._record_finding(session_id, verdict, records=records)

        if publisher is not None:
            try:
                publisher(verdict)
            except Exception as error:  # noqa: BLE001
                _log.info(
                    "shadow review: notice publish failed for session %s (%s)",
                    session_id,
                    type(error).__name__,
                )
        _log.info(
            "shadow review: %s for session %s after %d artifact(s)",
            verdict.verdict,
            session_id,
            len(records),
        )

    async def _run_review_call(
        self,
        session_id: str,
        records: Sequence[ArtifactRecord],
        history: Sequence[Message],
        *,
        reviewer_model: str | None = None,
        project_instructions: str | None = None,
    ) -> ShadowReviewVerdict:
        config = getattr(self._app, "config", None)
        fallback = (
            self._model
            or (getattr(config, "shadow_review_model", "") if config is not None else "")
        )
        resolved = resolve_reviewer_model(reviewer_model, fallback_model=fallback)
        self._last_reviewer_model = resolved
        workspace = getattr(config, "workspace", None) if config is not None else None
        resolved_instructions = resolve_project_instructions(
            workspace, override=project_instructions, include_preamble=False
        )

        if self._review_call is not None:
            try:
                sig = inspect.signature(self._review_call)
                params = sig.parameters
                if "reviewer_model" in params or any(
                    p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
                ):
                    return await self._review_call(
                        session_id, records, history, reviewer_model=resolved
                    )
            except (TypeError, ValueError):
                pass
            return await self._review_call(session_id, records, history)

        selected_provider = self._pick_provider(reviewer_model=resolved)
        return await review_with_provider(
            selected_provider,
            session_id,
            records,
            history,
            timeout_s=self._timeout_s,
            project_instructions=resolved_instructions,
        )

    def nudge_for(self, session_id: str) -> ShadowReviewNudge:
        """The augmentor that delivers this session's pending verdict note."""
        return ShadowReviewNudge(self._notes, session_id)


async def review_with_provider(
    provider,
    session_id,
    records,
    history,
    *,
    timeout_s=30.0,
    project_instructions: str = "",
):
    """The same review prompt and verdict codec for injected and ambient callers."""
    request = Message(
        role=Role.USER,
        content=_review_prompt(
            session_id,
            records,
            history,
            project_instructions=project_instructions,
        ),
    )
    async with asyncio.timeout(timeout_s):
        completion = await provider.generate([request], None)
    return parse_verdict(completion.message.content)


def _verdict_note(verdict: ShadowReviewVerdict) -> str:
    """The one-shot note a ``warn``/``fail`` verdict leaves for the model."""
    body = verdict.note or "no details were given"
    return (
        f"An automatic shadow review of this session's recent artifacts "
        f"returned a {verdict.verdict} verdict: {body} "
        f"Address the points it raises in your next work."
    )


def _extract_artifact_evidence(
    records: Sequence[ArtifactRecord],
    max_chars: int = 2400,
    *,
    max_bytes: int | None = None,
) -> list[str]:
    """Extract a bounded provenance and table/code evidence digest from on-disk artifacts."""
    snippets: list[str] = []
    budget = int(max_bytes) if max_bytes is not None else max_chars
    checked_dirs: set[Path] = set()
    for record in records[:8]:
        if budget <= 0:
            break
        p = Path(record.path)
        for parent in list(p.parents)[:4]:
            if parent in checked_dirs:
                continue
            checked_dirs.add(parent)
            for rel_brief in ("provenance/review_brief.md", "provenance/replay_receipt.json"):
                cand = parent / rel_brief
                try:
                    if cand.is_file():
                        raw = _strip_controls(cand.read_text(encoding="utf-8", errors="ignore"))[: min(budget, 900)]
                        if raw:
                            snippets.append(f"[{rel_brief}]\n{raw}")
                            budget -= len(raw)
                except OSError:
                    continue
        try:
            if p.is_file() and p.suffix.lower() in {".csv", ".tsv", ".json", ".md"} and budget > 120:
                head_lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()[:5]
                preview = _strip_controls("\n".join(head_lines))[: min(budget, 320)]
                if preview:
                    snippets.append(f"[{p.name} head]\n{preview}")
                    budget -= len(preview)
        except OSError:
            continue
    return snippets


def _review_prompt(
    session_id: str,
    records: Sequence[ArtifactRecord],
    history: Sequence[Message],
    *,
    project_instructions: str = "",
) -> str:
    """The bounded prompt the reviewer model is sent."""
    lines = [
        "You are a shadow reviewer for an omics data-analysis session.",
        "Review Principle: Trace, don't recompute. Trace every claim and figure back to the executed code cells, object_versions, and tabular outputs in the provenance digest.",
        "Review only the session's recent artifacts (below) for obvious "
        "problems: internally inconsistent results, missing provenance, "
        "a claim the listed artifacts cannot support, or broken {{artifact:<id>}} markers.",
        "Domain Statistical & Faithfulness Audit Checklist:",
        "1) Pseudoreplication: multi-donor/multi-slide condition DE must aggregate to donor_id/orig.ident/slide_id pseudobulk, never cell/spot-level Wilcoxon.",
        "2) Count Scale: DESeq2/edgeR/NB models must use raw integer counts, never log1p-normalized floats.",
        "3) Double Dipping: clustering + marker testing on the same matrix must verify genuine heterogeneity vs a single homogeneous population.",
        "4) Enrichment Background: GO/pathway enrichment must use detected/expressed genes as background universe.",
        "5) Claim Faithfulness & Artifact Markers: report numbers, gene signs, figures, and claimed methods must match executed code and CSV tables with zero orphan/stale outputs and valid {{artifact:<id>}} references (no unescaped {{!artifact:<id>}} in prose).",
        "Answer with a first line of exactly 'VERDICT: PASS', "
        "'VERDICT: WARN' or 'VERDICT: FAIL', then one short paragraph "
        "naming what you saw. PASS means nothing needs attention.",
        f"session_id: {session_id}",
    ]
    if project_instructions.strip():
        lines.append("project_instructions:")
        lines.append(_strip_controls(project_instructions.strip())[:1000])
    lines.append("artifacts:")
    for record in records[:60]:
        lines.append(
            f"- kind={record.kind} title={record.title[:80]!r} "
            f"path={record.path.split('/')[-1]!r} size={record.size}"
        )
        if len(lines[-1]) > _MAX_ARTIFACT_LINE_CHARS:
            lines[-1] = lines[-1][:_MAX_ARTIFACT_LINE_CHARS]
    evidence = _extract_artifact_evidence(records)
    if evidence:
        lines.append("artifact_evidence_digest:")
        lines.extend(evidence)
    for message in reversed(history):
        if message.role is Role.ASSISTANT and message.content:
            excerpt = message.content[:_MAX_HISTORY_EXCERPT_CHARS]
            lines.append("last assistant reply (excerpt):")
            lines.append(excerpt)
            break
    return "\n".join(lines)
