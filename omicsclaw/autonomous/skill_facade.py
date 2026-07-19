"""The ``oc`` skill-handle facade injected into the mini-agent kernel.

ADR 0032 §3 (NESTED code surface): generated glue composes *vetted skills*, not
raw scanpy, so heavy/parameter-sensitive steps never get hand-rolled. v1 handles
shell out through the existing shared ``run_skill`` (Model A) — there is no
in-process skill API — materialise the in-kernel object to a temp ``.h5ad``, run
the vetted subprocess, reload the declared primary artifact, and append an
ordered ``skill_calls.jsonl`` provenance record.

This module is *trusted injected code*: it runs inside the kernel but is not the
LLM-authored cell, so it is exempt from the AST blocklist and may legitimately
spawn the skill subprocess. The raw LLM cell may call only this facade for skill
execution (enforced by :func:`omicsclaw.autonomous.validation.validate_generated_code`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import threading
import time
from typing import Any, Callable

from omicsclaw.common.output_claim import (
    OutputClaimIdentity,
    collect_output_claim_identities,
    is_scientific_output_file,
)

from . import run_layout

PRIMARY_H5AD_NAME = "processed.h5ad"
SKILL_CALLS_LOG = run_layout.relpath("skill_calls_log")


@dataclass(slots=True)
class SkillHandleResult:
    """What a facade skill call returns to the generated code."""

    skill: str
    success: bool
    output_dir: str
    method: str | None = None
    primary_artifact: str = ""
    adata: Any = None
    tables: list[str] = field(default_factory=list)
    figures: list[str] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    duration_seconds: float = 0.0

    def __bool__(self) -> bool:
        return self.success


class SkillBudgetError(RuntimeError):
    """Raised when the generated code exceeds the nested skill-call budget."""


class SkillNotFoundError(ValueError):
    """Raised when ``oc.run()`` is given a name that is not a registered analysis skill.

    Subclasses ``ValueError`` so callers that already catch ValueError still handle
    it. The message lists close matches and points at ``oc.skills()`` so a category
    error (e.g. ``oc.run('list-skills')``, a meta op that is not an analysis skill)
    becomes a cheap, self-correcting signal instead of a confusing downstream fail.
    """


class SkillFacade:
    """Registry-backed ``oc`` facade. Lives inside the kernel for one run."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        max_skill_calls: int = 20,
        skill_timeout_seconds: int = 1800,
        run_skill: Callable[..., Any] | None = None,
        skill_catalog: Callable[[], dict[str, str]] | None = None,
    ) -> None:
        self.workspace = Path(workspace_root)
        self.max_skill_calls = int(max_skill_calls)
        self.skill_timeout_seconds = int(skill_timeout_seconds)
        self._run_skill = run_skill
        # Provider of {skill_name: domain}; defaults to the live registry. Injected
        # in tests so facade unit tests need not load the full registry.
        self._skill_catalog_provider = skill_catalog
        self._catalog_cache: dict[str, str] | None = None
        self._n = 0
        self.calls_log = self.workspace / SKILL_CALLS_LOG

    # -- public API the generated code calls ----------------------------- #

    def run(
        self,
        skill: str,
        data: Any = None,
        *,
        input_path: str | Path | None = None,
        method: str | None = None,
        timeout: int | None = None,
        **params: Any,
    ) -> SkillHandleResult:
        """Run a vetted skill on *data* (an AnnData) or *input_path*.

        Returns a :class:`SkillHandleResult`; ``.adata`` is the reloaded primary
        artifact when the skill declares one, else ``None``.
        """
        # Reject an unknown skill name + a missing input up front — before counting
        # against the budget or materialising anything — so a self-correctable
        # mistake (a category error like oc.run('list-skills'), or a call with no
        # data) costs a clear message, not a budget slot, a call number, or a
        # leftover dir (#3, RC3). Returns the canonical skill name.
        resolved = self._validate_skill(skill)
        if data is None and not input_path:
            raise ValueError(f"oc.run({skill!r}) needs either data (an AnnData) or input_path=")
        self._n += 1
        if self._n > self.max_skill_calls:
            raise SkillBudgetError(
                f"nested skill-call budget exhausted ({self.max_skill_calls}); "
                "compose fewer skills or ReturnAnswer with what you have."
            )
        runner = self._run_skill or _default_run_skill()
        call_dir = self.workspace / run_layout.relpath("skill_calls") / f"{self._n:02d}_{_safe(resolved)}"
        call_dir.mkdir(parents=True, exist_ok=True)

        in_path = self._resolve_input(data, input_path, call_dir)
        out_dir = call_dir / "out"
        flags = _to_flags(method, params)

        t0 = time.time()
        result = self._invoke(runner, resolved, in_path, out_dir, flags, timeout)
        duration = time.time() - t0

        real_out = Path(getattr(result, "output_dir", "") or out_dir)
        success = bool(getattr(result, "success", False))
        claim_identities = collect_output_claim_identities(real_out)
        primary = (
            self._find_primary_h5ad(
                real_out,
                claim_identities=claim_identities,
            )
            if success
            else None
        )
        adata = self._reload(primary) if primary else None

        handle = SkillHandleResult(
            skill=resolved,
            success=success,
            output_dir=str(real_out),
            method=getattr(result, "method", None) or method,
            primary_artifact=str(primary) if primary else "",
            adata=adata,
            tables=_list_dir(
                real_out / "tables",
                output_root=real_out,
                claim_identities=claim_identities,
            ),
            figures=_list_dir(
                real_out / "figures",
                output_root=real_out,
                claim_identities=claim_identities,
            ),
            stdout=str(getattr(result, "stdout", "") or "")[-2000:],
            stderr=str(getattr(result, "stderr", "") or "")[-2000:],
            error="" if success else str(getattr(result, "stderr", "") or "")[-2000:],
            duration_seconds=round(duration, 2),
        )
        self._record(handle, in_path, flags)
        return handle

    def skills(self, domain: str | None = None) -> list[str]:
        """List the analysis skills ``oc`` can run, optionally filtered by *domain*.

        The in-kernel discovery path: generated code calls this instead of guessing
        a name (or misusing ``oc.run`` for a meta op like list-skills).
        """
        catalog = self._catalog()
        if domain is None:
            return sorted(catalog)
        want = str(domain).strip().lower()
        return sorted(name for name, dom in catalog.items() if str(dom).strip().lower() == want)

    def _catalog(self) -> dict[str, str]:
        """``{skill_name: domain}`` for every runnable analysis skill (cached)."""
        if self._catalog_cache is None:
            provider = self._skill_catalog_provider or _default_skill_catalog
            self._catalog_cache = dict(provider())
        return self._catalog_cache

    def _validate_skill(self, skill: str) -> str:
        """Resolve *skill* to a runnable registry name, or raise SkillNotFoundError."""
        catalog = self._catalog()
        if skill in catalog:
            return skill
        if self._skill_catalog_provider is None:
            # Production: resolve a legacy alias via the registry before rejecting.
            from omicsclaw.skill.runner import resolve_skill_alias

            resolved = resolve_skill_alias(skill)
            if resolved in catalog:
                return resolved
        import difflib

        close = difflib.get_close_matches(skill, list(catalog), n=5)
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        raise SkillNotFoundError(
            f"'{skill}' is not an analysis skill.{hint} "
            f"Call oc.skills() to list the {len(catalog)} available skills."
        )

    def __getattr__(self, name: str) -> Callable[..., SkillHandleResult]:
        """Sugar: ``oc.spatial_preprocess(adata, ...)`` -> ``run('spatial-preprocess', ...)``."""
        if name.startswith("_"):
            raise AttributeError(name)
        skill_name = name.replace("_", "-")

        def _call(data: Any = None, **kwargs: Any) -> SkillHandleResult:
            return self.run(skill_name, data, **kwargs)

        return _call

    # -- internals ------------------------------------------------------- #

    def _resolve_input(self, data: Any, input_path: str | Path | None, call_dir: Path) -> Path:
        if data is not None:
            target = call_dir / "input.h5ad"
            self._write_adata(data, target)
            return target
        if input_path:
            return Path(input_path)
        raise ValueError("oc.run() needs either data (an AnnData) or input_path=")

    def _invoke(self, runner, skill, in_path, out_dir, flags, timeout):
        cancel = threading.Event()
        limit = int(timeout or self.skill_timeout_seconds)
        timer = threading.Timer(limit, cancel.set)
        timer.start()
        try:
            return runner(
                skill,
                input_path=str(in_path),
                output_dir=str(out_dir),
                extra_args=flags,
                cancel_event=cancel,
            )
        finally:
            timer.cancel()

    @staticmethod
    def _write_adata(data: Any, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        writer = getattr(data, "write_h5ad", None) or getattr(data, "write", None)
        if writer is None:
            raise TypeError("oc.run(data=...) expects an AnnData-like object with .write_h5ad")
        writer(target)

    @staticmethod
    def _find_primary_h5ad(
        out_dir: Path,
        *,
        claim_identities: frozenset[OutputClaimIdentity],
    ) -> Path | None:
        primary = out_dir / PRIMARY_H5AD_NAME
        if is_scientific_output_file(
            primary,
            output_root=out_dir,
            claim_identities=claim_identities,
        ):
            return primary
        candidates = sorted(
            candidate
            for candidate in out_dir.glob("*.h5ad")
            if is_scientific_output_file(
                candidate,
                output_root=out_dir,
                claim_identities=claim_identities,
            )
        )
        return candidates[0] if len(candidates) == 1 else None

    @staticmethod
    def _reload(path: Path) -> Any:
        import anndata

        return anndata.read_h5ad(path)

    def _record(self, handle: SkillHandleResult, in_path: Path, flags: list[str]) -> None:
        record = {
            "index": self._n,
            "skill": handle.skill,
            "method": handle.method,
            "params": _flags_to_params(flags),
            "flags": flags,
            "input_artifact": str(in_path),
            "output_dir": handle.output_dir,
            "primary_artifact": handle.primary_artifact,
            "status": "succeeded" if handle.success else "failed",
            "manifest_path": _manifest_path(handle.output_dir),
            "duration_seconds": handle.duration_seconds,
        }
        with self.calls_log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def build_facade(
    workspace_root: str | Path,
    *,
    max_skill_calls: int = 20,
    skill_timeout_seconds: int = 1800,
    run_skill: Callable[..., Any] | None = None,
    skill_catalog: Callable[[], dict[str, str]] | None = None,
) -> SkillFacade:
    """Construct the ``oc`` facade for one autonomous run."""
    return SkillFacade(
        workspace_root,
        max_skill_calls=max_skill_calls,
        skill_timeout_seconds=skill_timeout_seconds,
        run_skill=run_skill,
        skill_catalog=skill_catalog,
    )


def _default_run_skill() -> Callable[..., Any]:
    from omicsclaw.skill.runner import run_skill

    return run_skill


def _default_skill_catalog() -> dict[str, str]:
    """``{skill_name: domain}`` from the live registry — the production catalog.

    Canonical skills only: the registry indexes each skill under its canonical name
    AND its legacy / directory aliases (181 keys, 95 canonical). Exposing the alias
    keys would make ``oc.skills()`` a noisy list of duplicates; excluding them also
    means an alias input misses this catalog and is canonicalized via
    ``resolve_skill_alias`` in :meth:`SkillFacade._validate_skill`.
    """
    from omicsclaw.skill.registry import ensure_registry_loaded

    registry = ensure_registry_loaded()
    return {
        name: str(info.get("domain", ""))
        for name, info in registry.skills.items()
        if name == info.get("alias", name)
    }


def _to_flags(method: str | None, params: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    if method:
        flags += ["--method", str(method)]
    for key, value in params.items():
        flag = "--" + str(key).replace("_", "-")
        if isinstance(value, bool):
            if value:
                flags.append(flag)
        elif isinstance(value, (list, tuple)):
            flags += [flag, ",".join(str(item) for item in value)]
        elif value is not None:
            flags += [flag, str(value)]
    return flags


def _list_dir(
    path: Path,
    *,
    output_root: Path,
    claim_identities: frozenset[OutputClaimIdentity],
) -> list[str]:
    if not path.is_dir():
        return []
    return sorted(
        candidate.name
        for candidate in path.iterdir()
        if is_scientific_output_file(
            candidate,
            output_root=output_root,
            claim_identities=claim_identities,
        )
    )


def _flags_to_params(flags: list[str]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    index = 0
    while index < len(flags):
        flag = flags[index]
        if not flag.startswith("--"):
            index += 1
            continue
        key = flag[2:].replace("-", "_")
        if index + 1 < len(flags) and not flags[index + 1].startswith("--"):
            params[key] = flags[index + 1]
            index += 2
        else:
            params[key] = True
            index += 1
    return params


def _manifest_path(output_dir: str) -> str:
    root = Path(output_dir)
    candidate = root / "manifest.json"
    claim_identities = collect_output_claim_identities(root)
    return (
        str(candidate)
        if is_scientific_output_file(
            candidate,
            output_root=root,
            claim_identities=claim_identities,
        )
        else ""
    )


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in str(name))


__all__ = [
    "PRIMARY_H5AD_NAME",
    "SKILL_CALLS_LOG",
    "SkillBudgetError",
    "SkillFacade",
    "SkillHandleResult",
    "SkillNotFoundError",
    "build_facade",
]
