"""Evaluate runtime input state against a skill's declared input contract.

This module deliberately does not rank skills or execute them.  It converts
the ``skill.yaml`` ``interface.inputs`` contract and an observed input profile
into a small, deterministic preflight decision that routing surfaces can share.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
import os
from pathlib import Path
from typing import Any, Mapping

from .registry import OmicsRegistry, ensure_registry_loaded


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
) -> InputProfile:
    """Inspect lightweight input metadata without loading the expression matrix.

    All paths yield at least filename-derived facts.  ``.h5ad`` uses AnnData's
    backed read mode to expose declared obs/var/layer/obsm/uns keys and the
    OmicsClaw input/matrix contracts.  Inspection failure is recorded rather
    than raised so callers can fail into preflight instead of crashing routing.
    """
    source = Path(path).expanduser()
    basic = {
        "file_type": _file_type_from_path(source),
        "modality": modality,
        "source_path": str(source),
        "env": set(os.environ),
    }
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
        metadata = _cached_h5ad_profile(
            str(resolved),
            stat.st_mtime_ns,
            stat.st_size,
        )
        profiled = dict(basic)
        profiled.update(metadata)
        profiled["modality"] = modality or str(metadata.get("modality") or "")
        return InputProfile(**profiled)
    except Exception as exc:
        return InputProfile(**basic, inspection_error=str(exc))


def _required_names(data_shape: Mapping[str, Any], key: str) -> set[str]:
    return _normalise_names(data_shape.get(key, [])) or set()


def evaluate_skill_preconditions(
    skill_name: str,
    profile: InputProfile | Mapping[str, Any],
    *,
    registry: OmicsRegistry | None = None,
) -> PreconditionAssessment:
    """Evaluate ``profile`` against ``skill_name``'s declared input contract."""
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

    profile = profile if isinstance(profile, InputProfile) else InputProfile.from_mapping(profile)
    contract = info.get("input_contract") or {}
    modalities = {str(value).strip().lower() for value in contract.get("modalities", [])}
    file_types = {_normalise_file_type(value) for value in contract.get("file_types", [])}
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
            missing.append("modality")
            preparation.append("input modality has not been verified")
        elif profile.modality not in concrete_modalities:
            missing.append("modality")
            blocked.append(
                f"modality '{profile.modality}' is incompatible; "
                f"expected one of {sorted(concrete_modalities)}"
            )

    if data_shape.get("requires_preprocessed") and profile.preprocessed is not True:
        missing.append("preprocessed")
        preparation.append("input has not been verified as preprocessed")

    for key in ("obs", "var", "layers", "obsm", "uns"):
        required = _required_names(data_shape, key)
        if not required:
            continue
        observed = getattr(profile, key)
        absent = required if observed is None else required - observed
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
        recommendation_map = _PREPARATION_SKILLS.get(
            str(info.get("domain") or ""), {}
        )
        recommendations = list(
            dict.fromkeys(
                recommendation_map[item]
                for item in missing
                if item in recommendation_map
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
    "probe_input_profile",
]
