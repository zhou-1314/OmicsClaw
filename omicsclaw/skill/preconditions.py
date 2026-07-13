"""Evaluate runtime input state against a skill's declared input contract.

This module deliberately does not rank skills or execute them.  It converts
the ``skill.yaml`` ``interface.inputs`` contract and an observed input profile
into a small, deterministic preflight decision that routing surfaces can share.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from functools import lru_cache
import os
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.parse import urlparse

from .registry import OmicsRegistry, ensure_registry_loaded


_SUFFIX_TYPED_DIRECTORY_FORMATS = frozenset({"zarr"})


class PreconditionStatus(str, Enum):
    """Whether the selected skill can consume the observed input as-is."""

    ELIGIBLE = "eligible"
    BLOCKED = "blocked"
    NEEDS_PREPARATION = "needs_preparation"


def _normalise_names(values: object) -> set[str] | None:
    if values is None:
        return None
    if isinstance(values, str):
        values = [values]
    return {
        str(value).strip()
        for value in values  # type: ignore[union-attr]
        if str(value).strip()
    }


def _normalise_file_type(value: object) -> str:
    normalised = str(value or "").strip().lower().lstrip(".")
    for compression_suffix in (".bz2", ".gz", ".xz", ".zst"):
        if normalised.endswith(compression_suffix):
            return normalised[: -len(compression_suffix)]
    return normalised


@dataclass
class InputProfile:
    """Observed, domain-neutral facts about a candidate input.

    ``None`` means a collection was not inspected; an empty collection means
    it was inspected and the key was absent.  That distinction prevents a
    filename-only route from pretending it verified AnnData internals.
    """

    file_type: str = ""
    path_kind: str = ""
    modality: str = ""
    preprocessed: bool | None = None
    obs: set[str] | None = None
    var: set[str] | None = None
    layers: set[str] | None = None
    obsm: set[str] | None = None
    uns: set[str] | None = None
    env: set[str] | None = None
    config: set[str] | None = None
    source_path: str = ""
    inspection_error: str = ""

    def __post_init__(self) -> None:
        self.file_type = _normalise_file_type(self.file_type)
        self.path_kind = str(self.path_kind or "").strip().lower()
        if (
            self.path_kind == "directory"
            and self.file_type not in _SUFFIX_TYPED_DIRECTORY_FORMATS
        ):
            self.file_type = ""
        self.modality = str(self.modality or "").strip().lower()
        for field_name in ("obs", "var", "layers", "obsm", "uns", "env", "config"):
            setattr(self, field_name, _normalise_names(getattr(self, field_name)))

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "InputProfile":
        """Build a profile from JSON/tool arguments without leaking extras."""
        fields = cls.__dataclass_fields__
        return cls(**{key: value for key, value in values.items() if key in fields})


@dataclass(frozen=True)
class PreconditionAssessment:
    """Deterministic preflight result for one skill and one input profile."""

    skill: str
    status: PreconditionStatus
    evaluated: bool
    execution_ready: bool
    missing: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    recommended_preparation: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill": self.skill,
            "status": self.status.value,
            "evaluated": self.evaluated,
            "execution_ready": self.execution_ready,
            "missing": list(self.missing),
            "reasons": list(self.reasons),
            "recommended_preparation": list(self.recommended_preparation),
        }


_PREPARATION_SKILLS = {
    "singlecell": {
        "preprocessed": "sc-preprocessing",
        "obsm.X_pca": "sc-preprocessing",
    },
    "spatial": {
        "preprocessed": "spatial-preprocess",
        "obsm.X_pca": "spatial-preprocess",
        "obsm.spatial": "spatial-preprocess",
    },
}


_PREPROCESSED_MATRIX_KINDS = {
    "normalized_expression",
    "log1p_normalized_expression",
    "scaled_expression",
}


def _file_type_from_path(path: str | Path) -> str:
    suffixes = [suffix.lower().lstrip(".") for suffix in Path(path).suffixes]
    if not suffixes:
        return ""
    if len(suffixes) >= 2 and suffixes[-1] in {"bz2", "gz", "xz", "zst"}:
        return suffixes[-2]
    return suffixes[-1]


def _read_h5ad_profile(path: str) -> dict[str, Any]:
    """Read backed AnnData metadata; split out for cache/invalidation tests."""
    adata = None
    try:
        import anndata as ad

        adata = ad.read_h5ad(path, backed="r")
        obs = set(map(str, adata.obs.columns))
        var = set(map(str, adata.var.columns))
        layers = set(map(str, adata.layers.keys()))
        obsm = set(map(str, adata.obsm.keys()))
        uns = set(map(str, adata.uns.keys()))

        input_contract = adata.uns.get("omicsclaw_input_contract", {})
        matrix_contract = adata.uns.get("omicsclaw_matrix_contract", {})
        if not isinstance(input_contract, Mapping):
            input_contract = {}
        if not isinstance(matrix_contract, Mapping):
            matrix_contract = {}
        matrix_kind = str(matrix_contract.get("X") or "").strip().lower()
        return {
            "modality": str(input_contract.get("modality") or "").strip(),
            "preprocessed": bool(
                "X_pca" in obsm
                or matrix_kind in _PREPROCESSED_MATRIX_KINDS
                or input_contract.get("preprocessed") is True
            ),
            "obs": obs,
            "var": var,
            "layers": layers,
            "obsm": obsm,
            "uns": uns,
            "inspection_error": "",
        }
    except Exception as exc:
        return {"inspection_error": str(exc)}
    finally:
        file_manager = getattr(adata, "file", None)
        close = getattr(file_manager, "close", None)
        if callable(close):
            close()


@lru_cache(maxsize=64)
def _cached_h5ad_profile(
    resolved_path: str,
    mtime_ns: int,
    size: int,
) -> dict[str, Any]:
    """Cache metadata by file identity; mtime/size changes invalidate it."""
    del mtime_ns, size
    return _read_h5ad_profile(resolved_path)


def probe_input_profile(
    path: str | Path,
    *,
    modality: str = "",
    use_cache: bool = True,
) -> InputProfile:
    """Inspect lightweight input metadata without loading the expression matrix.

    All paths yield at least filename-derived facts.  ``.h5ad`` uses AnnData's
    backed read mode to expose declared obs/var/layer/obsm/uns keys and the
    OmicsClaw input/matrix contracts.  Inspection failure is recorded rather
    than raised so callers can fail into preflight instead of crashing routing.
    """
    source = Path(path).expanduser()
    path_kind = "directory" if source.is_dir() else "file"
    basic = {
        "file_type": _file_type_from_path(source),
        "path_kind": path_kind,
        "modality": modality,
        "source_path": str(source),
        "env": set(os.environ),
    }
    if not source.exists():
        return InputProfile(
            **basic,
            inspection_error=f"input path does not exist: {source}",
        )
    if path_kind == "directory":
        return InputProfile(**basic)
    if basic["file_type"] != "h5ad":
        return InputProfile(**basic)
    if not source.is_file():
        return InputProfile(
            **basic,
            inspection_error=f"input file does not exist: {source}",
        )

    try:
        resolved = source.resolve()
        stat = resolved.stat()
        metadata = (
            _cached_h5ad_profile(
                str(resolved),
                stat.st_mtime_ns,
                stat.st_size,
            )
            if use_cache
            else _read_h5ad_profile(str(resolved))
        )
        profiled = dict(basic)
        profiled.update(metadata)
        profiled["modality"] = modality or str(metadata.get("modality") or "")
        return InputProfile(**profiled)
    except Exception as exc:
        return InputProfile(**basic, inspection_error=str(exc))


_DOI_PATTERN = re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE)


def _execution_input_kind(
    value: str,
    *,
    declared_file_types: set[str],
    known_file_types: set[str],
    declared_path_kinds: set[str],
) -> str:
    """Classify one execution input as ``file``, ``directory``, or ``freeform``.

    Existing paths provide authoritative kinds. Missing explicit paths and
    known suffixes remain auditable so typos cannot bypass the gate. URLs,
    DOIs, accessions, and prose are freeform and are accepted only when the
    selected skill declares that kind.
    """
    stripped = value.strip()
    if not stripped:
        return "freeform"
    parsed = urlparse(stripped)
    if parsed.scheme.lower() in {"http", "https", "ftp", "s3", "gs"}:
        return "freeform"
    if _DOI_PATTERN.match(stripped):
        return "freeform"

    candidate = Path(stripped).expanduser()
    if candidate.is_dir():
        return "directory"
    if candidate.exists():
        return "file"
    explicit_path_syntax = bool(
        candidate.is_absolute()
        or stripped.startswith(("./", "../", "~"))
        or re.match(r"^[A-Za-z]:[\\/]", stripped)
    )
    if any(char.isspace() for char in stripped) and not explicit_path_syntax:
        return "freeform"
    file_type = _file_type_from_path(candidate)
    if file_type in declared_file_types or file_type in known_file_types:
        return "file"
    if explicit_path_syntax:
        if not file_type and declared_path_kinds == {"directory"}:
            return "directory"
        return "file"
    return "freeform"


def _blocked_execution_assessment(
    skill_name: str,
    info: Mapping[str, Any],
    *,
    missing: str,
    reason: str,
) -> PreconditionAssessment:
    return PreconditionAssessment(
        skill=str(info.get("alias") or skill_name),
        status=PreconditionStatus.BLOCKED,
        evaluated=True,
        execution_ready=False,
        missing=[missing],
        reasons=[reason],
    )


def _combine_execution_assessments(
    skill_name: str,
    assessments: list[PreconditionAssessment],
) -> PreconditionAssessment:
    """Collapse repeated-input assessments into one stable gate decision."""
    if len(assessments) == 1:
        return assessments[0]

    blocked = any(item.status is PreconditionStatus.BLOCKED for item in assessments)
    status = (
        PreconditionStatus.BLOCKED if blocked else PreconditionStatus.NEEDS_PREPARATION
    )
    missing: list[str] = []
    reasons: list[str] = []
    recommendations: list[str] = []
    for item in assessments:
        missing.extend(item.missing)
        reasons.extend(item.reasons)
        recommendations.extend(item.recommended_preparation)
    return PreconditionAssessment(
        skill=assessments[0].skill if assessments else skill_name,
        status=status,
        evaluated=True,
        execution_ready=False,
        missing=missing,
        reasons=reasons,
        recommended_preparation=list(dict.fromkeys(recommendations)),
    )


def preflight_skill_execution(
    skill_name: str,
    *,
    input_path: str | None = None,
    input_paths: list[str] | None = None,
    demo: bool = False,
    session_path: str | None = None,
    registry: OmicsRegistry | None = None,
) -> PreconditionAssessment | None:
    """Audit explicit execution inputs through the shared runner seam.

    ``None`` means the gate is not applicable: demo mode, no input, or a
    documented non-local/free-form input.  Any returned assessment was based
    on an observed local input and is therefore authoritative for execution.
    Callers must refuse execution when ``execution_ready`` is false.
    """
    if demo:
        return None

    registry = registry or ensure_registry_loaded()
    info = registry.skills.get(skill_name)
    if not info:
        return None
    contract = info.get("input_contract") or {}
    path_kinds = {
        str(value).strip().lower()
        for value in (contract.get("path_kinds") or ["file"])
    }
    declared_file_types = {
        _normalise_file_type(value)
        for value in contract.get("file_types", [])
        if _normalise_file_type(value) not in {"", "*"}
    }
    known_file_types = {
        _normalise_file_type(value)
        for candidate_info in registry.skills.values()
        for value in (candidate_info.get("input_contract") or {}).get("file_types", [])
        if _normalise_file_type(value) not in {"", "*"}
    }
    candidates = list(input_paths or []) or ([input_path] if input_path else [])
    session_supplied_input = False
    if not candidates and session_path:
        try:
            from omicsclaw.common.session import SpatialSession

            session = SpatialSession.load(session_path)
            if session.h5ad_path:
                candidates = [session.h5ad_path]
                session_supplied_input = True
            else:
                return _blocked_execution_assessment(
                    skill_name,
                    info,
                    missing="session",
                    reason="session does not contain a usable local input path",
                )
        except Exception as exc:
            return _blocked_execution_assessment(
                skill_name,
                info,
                missing="session",
                reason=f"session input inspection failed: {exc}",
            )

    classified_inputs: list[tuple[int, str, str]] = []
    for index, value in enumerate(candidates, start=1):
        if not value:
            continue
        input_kind = _execution_input_kind(
            value,
            declared_file_types=declared_file_types,
            known_file_types=known_file_types,
            declared_path_kinds=path_kinds,
        )
        if session_supplied_input and input_kind == "freeform":
            # A session field is a persisted local path, never literature text.
            input_kind = "file"
        classified_inputs.append((index, value, input_kind))

    if not classified_inputs:
        return None

    assessments: list[PreconditionAssessment] = []
    for input_index, value, input_kind in classified_inputs:
        if input_kind not in path_kinds:
            assessment = _blocked_execution_assessment(
                skill_name,
                info,
                missing="path_kind",
                reason=(
                    f"{input_kind} input is incompatible; "
                    f"declared path kinds are {sorted(path_kinds)}"
                ),
            )
            if len(candidates) > 1:
                assessment = replace(
                    assessment,
                    missing=[
                        f"input[{input_index}].{name}" for name in assessment.missing
                    ],
                    reasons=[
                        f"input[{input_index}]: {reason}"
                        for reason in assessment.reasons
                    ],
                )
            assessments.append(assessment)
            continue
        if input_kind == "freeform":
            continue

        is_directory = input_kind == "directory"
        # Execution checks always inspect the current file instead of reusing
        # a resolver cache entry.  A later path replacement is still an OS-level
        # TOCTOU concern, but stale cached metadata cannot authorize this run.
        profile = probe_input_profile(value, use_cache=False)
        # RET-04's structural probe is currently authoritative for AnnData.
        # For other declared formats it only knows the filename-derived type;
        # treating unobserved modality/columns as absent would disable valid
        # CSV/FASTQ/PDF executions.  Preserve those runs until a format probe
        # exists, while still rejecting an explicit file-type mismatch below.
        assessment = evaluate_skill_preconditions(
            skill_name,
            profile,
            registry=registry,
            require_verified_modality=False,
            require_verified_file_type=not is_directory,
            require_observed_data_shape=profile.file_type == "h5ad",
        )
        if len(candidates) > 1:
            assessment = replace(
                assessment,
                missing=[f"input[{input_index}].{name}" for name in assessment.missing],
                reasons=[
                    f"input[{input_index}]: {reason}" for reason in assessment.reasons
                ],
            )
        assessments.append(assessment)
    if not assessments:
        # Every supplied input was an explicitly allowed freeform value.
        return None
    if all(item.execution_ready for item in assessments):
        return (
            assessments[0]
            if len(assessments) == 1
            else PreconditionAssessment(
                skill=assessments[0].skill,
                status=PreconditionStatus.ELIGIBLE,
                evaluated=True,
                execution_ready=True,
            )
        )
    return _combine_execution_assessments(skill_name, assessments)


def format_precondition_failure(assessment: PreconditionAssessment) -> str:
    """Render a stable, actionable execution-gate diagnostic."""
    from omicsclaw.common.user_guidance import format_user_guidance_payload

    guidance = (
        [
            "Run one of these preparation skills first: "
            + ", ".join(assessment.recommended_preparation)
        ]
        if assessment.recommended_preparation
        else ["Prepare or replace the input, then retry the same skill."]
    )
    payload = format_user_guidance_payload(
        {
            "kind": "preflight",
            "status": "blocked",
            "skill": assessment.skill,
            "precondition_status": assessment.status.value,
            "missing_requirements": assessment.reasons or assessment.missing,
            "guidance": guidance,
        }
    )
    lines = [
        payload,
        f"Skill `{assessment.skill}` failed the execution precondition gate.",
        f"Precondition status: {assessment.status.value}",
    ]
    if assessment.missing:
        lines.append("Missing preconditions: " + ", ".join(assessment.missing))
    if assessment.reasons:
        lines.append("Reasons:")
        lines.extend(f"- {reason}" for reason in assessment.reasons)
    if assessment.recommended_preparation:
        lines.append(
            "Recommended preparation: "
            + ", ".join(f"`{skill}`" for skill in assessment.recommended_preparation)
        )
    lines.append(
        "Prepare or replace the input, then retry the same skill before execution."
    )
    return "\n".join(lines)


def _required_names(data_shape: Mapping[str, Any], key: str) -> set[str]:
    return _normalise_names(data_shape.get(key, [])) or set()


def evaluate_skill_preconditions(
    skill_name: str,
    profile: InputProfile | Mapping[str, Any],
    *,
    registry: OmicsRegistry | None = None,
    require_verified_modality: bool = True,
    require_verified_file_type: bool = True,
    require_observed_data_shape: bool = True,
) -> PreconditionAssessment:
    """Evaluate ``profile`` against ``skill_name``'s declared input contract.

    Semantic routing keeps ``require_verified_modality=True`` so an unknown
    input identity cannot masquerade as an eligible auto-route.  Explicit
    execution sets it false: choosing a named skill supplies user intent, while
    a positively observed incompatible modality still blocks execution.
    """
    registry = registry or ensure_registry_loaded()
    info = registry.skills.get(skill_name)
    if not info:
        return PreconditionAssessment(
            skill=skill_name,
            status=PreconditionStatus.BLOCKED,
            evaluated=True,
            execution_ready=False,
            missing=["skill"],
            reasons=[f"unknown skill '{skill_name}'"],
        )

    profile = (
        profile
        if isinstance(profile, InputProfile)
        else InputProfile.from_mapping(profile)
    )
    contract = info.get("input_contract") or {}
    modalities = {
        str(value).strip().lower() for value in contract.get("modalities", [])
    }
    file_types = {
        _normalise_file_type(value) for value in contract.get("file_types", [])
    }
    preconditions = contract.get("preconditions") or {}
    data_shape = preconditions.get("data_shape") or {}

    blocked: list[str] = []
    preparation: list[str] = []
    missing: list[str] = []

    if profile.inspection_error:
        missing.append("inspection")
        blocked.append(f"input inspection failed: {profile.inspection_error}")

    concrete_file_types = file_types - {"*"}
    if concrete_file_types:
        if not profile.file_type:
            if require_verified_file_type:
                missing.append("file_type")
                preparation.append("input file type has not been verified")
        elif profile.file_type not in concrete_file_types:
            missing.append("file_type")
            blocked.append(
                f"file type '{profile.file_type}' is incompatible; "
                f"expected one of {sorted(concrete_file_types)}"
            )
    concrete_modalities = modalities - {"*"}
    if concrete_modalities:
        if not profile.modality:
            if require_verified_modality:
                missing.append("modality")
                preparation.append("input modality has not been verified")
        elif profile.modality not in concrete_modalities:
            missing.append("modality")
            blocked.append(
                f"modality '{profile.modality}' is incompatible; "
                f"expected one of {sorted(concrete_modalities)}"
            )

    if data_shape.get("requires_preprocessed") and profile.preprocessed is not True:
        if profile.preprocessed is False or require_observed_data_shape:
            missing.append("preprocessed")
            preparation.append("input has not been verified as preprocessed")

    for key in ("obs", "var", "layers", "obsm", "uns"):
        required = _required_names(data_shape, key)
        if not required:
            continue
        observed = getattr(profile, key)
        absent = (
            set()
            if observed is None and not require_observed_data_shape
            else required
            if observed is None
            else required - observed
        )
        for name in sorted(absent):
            missing.append(f"{key}.{name}")
            preparation.append(f"required {key} key '{name}' is not available")

    for key in ("env", "config"):
        required = _normalise_names(preconditions.get(key, [])) or set()
        if not required:
            continue
        observed = getattr(profile, key)
        absent = required if observed is None else required - observed
        for name in sorted(absent):
            missing.append(f"{key}.{name}")
            blocked.append(f"required {key} value '{name}' is not available")

    canonical_skill = str(info.get("alias") or skill_name)
    if blocked:
        return PreconditionAssessment(
            skill=canonical_skill,
            status=PreconditionStatus.BLOCKED,
            evaluated=True,
            execution_ready=False,
            missing=missing,
            reasons=blocked + preparation,
        )
    if preparation:
        recommendation_map = _PREPARATION_SKILLS.get(str(info.get("domain") or ""), {})
        recommendations = list(
            dict.fromkeys(
                recommendation_map[item]
                for item in missing
                if item in recommendation_map
                and recommendation_map[item] != canonical_skill
            )
        )
        return PreconditionAssessment(
            skill=canonical_skill,
            status=PreconditionStatus.NEEDS_PREPARATION,
            evaluated=True,
            execution_ready=False,
            missing=missing,
            reasons=preparation,
            recommended_preparation=recommendations,
        )
    return PreconditionAssessment(
        skill=canonical_skill,
        status=PreconditionStatus.ELIGIBLE,
        evaluated=True,
        execution_ready=True,
    )


__all__ = [
    "InputProfile",
    "PreconditionAssessment",
    "PreconditionStatus",
    "evaluate_skill_preconditions",
    "format_precondition_failure",
    "preflight_skill_execution",
    "probe_input_profile",
]
