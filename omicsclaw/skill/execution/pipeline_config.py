"""Load and validate ``pipelines/<name>.yaml`` chain definitions.

OMI-12 P2.7: pipelines were hard-coded as ``SPATIAL_PIPELINE = [...]`` inside
``pipeline_runner``. Adding a ``singlecell-pipeline`` / ``bulkrna-pipeline``
meant editing Python and recompiling reasoning about which constant to use.
This module loads YAML configs that the runner consumes, so new pipelines
are a one-file drop-in.

Schema (see ``pipelines/spatial-pipeline.yaml``):

    name                   str  required  pipeline alias (must end with ``-pipeline``)
    description            str  optional  one-line summary
    chain_output_basename  str  optional  safe basename the runner looks for in step N's
                                          output dir to feed step N+1 as ``--input``
                                          (default ``processed.h5ad``).
    steps                  list required  ordered list of ``{skill: <canonical-alias>}`` dicts

Steps run sequentially; the chain stops at the first failure. Each step's
result is recorded in the final ``pipeline_summary.json`` regardless of how
the chain terminates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

import yaml

from omicsclaw.common.output_claim import is_output_claim_path

from ..registry import OMICSCLAW_DIR
from ..strict_yaml import load_unique_yaml


_DEFAULT_CHAIN_OUTPUT = "processed.h5ad"
_SIMPLE_ALIAS_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_MAX_ALIAS_LENGTH = 128
_PIPELINE_FIELDS = frozenset(
    {"name", "description", "chain_output_basename", "steps"}
)
_PIPELINE_STEP_FIELDS = frozenset({"skill"})
_WINDOWS_RESERVED_FILENAME_CHARS = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED_STEMS = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
    | {f"com{index}" for index in ("¹", "²", "³")}
    | {f"lpt{index}" for index in ("¹", "²", "³")}
)

# Repository-root ``pipelines/`` directory. Shares the registry's
# ``OMICSCLAW_DIR`` resolution so the ``OMICSCLAW_DIR`` env override that the
# rest of the runtime honours (wheel installs, custom layouts) also routes
# pipeline lookup to the operator's actual data tree.
PIPELINES_DIR = OMICSCLAW_DIR / "pipelines"


@dataclass(frozen=True)
class PipelineStep:
    """One entry in a pipeline chain — currently just a skill alias."""

    skill: str


@dataclass(frozen=True)
class PipelineConfig:
    """In-memory view of a loaded ``pipelines/<name>.yaml`` file."""

    name: str
    description: str
    chain_output_basename: str
    steps: tuple[PipelineStep, ...]

    def __post_init__(self) -> None:
        # ``run_pipeline`` is a public programmatic entry point, so YAML-only
        # validation is insufficient.  Keep the constructor and runtime on the
        # same invariant set; the runner revalidates as defense in depth.
        validate_pipeline_config(self)

    @property
    def skill_names(self) -> tuple[str, ...]:
        """Convenience: list of skill aliases in run order."""
        return tuple(step.skill for step in self.steps)


class PipelineConfigError(ValueError):
    """Raised when a YAML config is malformed or missing required fields."""


def _is_simple_alias(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= _MAX_ALIAS_LENGTH
        and _SIMPLE_ALIAS_RE.fullmatch(value) is not None
    )


def _is_safe_file_basename(value: str) -> bool:
    if not value or len(value) > 255 or value in {".", ".."}:
        return False
    if is_output_claim_path(Path(value)):
        return False
    try:
        if len(value.encode("utf-8")) > 255:
            return False
    except UnicodeEncodeError:
        return False
    if value.endswith((".", " ")):
        return False
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return False
    if any(character in _WINDOWS_RESERVED_FILENAME_CHARS for character in value):
        return False
    reserved_stem = value.split(".", 1)[0].rstrip(" .").casefold()
    if reserved_stem in _WINDOWS_RESERVED_STEMS:
        return False
    # A drive-relative Windows path (for example ``C:result.csv``) contains no
    # separator, but it is still not a portable basename.
    return not PureWindowsPath(value).drive


def validate_pipeline_config(config: PipelineConfig) -> None:
    """Validate invariants shared by YAML and programmatic pipeline configs."""
    if not isinstance(config, PipelineConfig):
        raise PipelineConfigError("pipeline config must be a PipelineConfig")
    if (
        not isinstance(config.name, str)
        or not _is_simple_alias(config.name)
        or not config.name.endswith("-pipeline")
    ):
        raise PipelineConfigError(
            "pipeline ``name`` must be a canonical alias ending in '-pipeline'"
        )
    if not isinstance(config.description, str):
        raise PipelineConfigError("pipeline ``description`` must be a string")
    if not isinstance(config.chain_output_basename, str) or not _is_safe_file_basename(
        config.chain_output_basename
    ):
        raise PipelineConfigError(
            "pipeline ``chain_output_basename`` must be a safe file basename"
        )
    if not isinstance(config.steps, tuple) or not config.steps:
        raise PipelineConfigError("pipeline ``steps`` must be a non-empty tuple")

    seen_skills: set[str] = set()
    for idx, step in enumerate(config.steps):
        if not isinstance(step, PipelineStep):
            raise PipelineConfigError(
                f"pipeline step #{idx} must be a PipelineStep"
            )
        if not isinstance(step.skill, str) or not _is_simple_alias(step.skill):
            raise PipelineConfigError(
                f"pipeline step #{idx} ``skill`` must be a canonical skill alias"
            )
        if step.skill in seen_skills:
            raise PipelineConfigError(
                f"pipeline duplicate step skill {step.skill!r} is not supported"
            )
        seen_skills.add(step.skill)


def pipeline_config_path(name: str, *, pipelines_dir: Path | None = None) -> Path:
    """Return a contained, resolved YAML path for a canonical pipeline alias."""
    if (
        not isinstance(name, str)
        or not _is_simple_alias(name)
        or not name.endswith("-pipeline")
    ):
        raise PipelineConfigError(
            "pipeline lookup name must be a canonical pipeline alias ending in "
            "'-pipeline'"
        )

    try:
        root = Path(pipelines_dir or PIPELINES_DIR).resolve()
        path = (root / f"{name}.yaml").resolve()
        path.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PipelineConfigError(
            "pipeline config path must resolve inside the pipelines directory"
        ) from exc
    return path


def load_pipeline_config(
    name: str,
    *,
    pipelines_dir: Path | None = None,
) -> PipelineConfig | None:
    """Load ``pipelines/<name>.yaml``; return ``None`` when the file is absent.

    Validation errors raise ``PipelineConfigError`` — the runner converts those
    into stable error results so an operator typo in the YAML never crashes
    the process.
    """
    path = pipeline_config_path(name, pipelines_dir=pipelines_dir)
    if not path.exists():
        return None

    try:
        raw = load_unique_yaml(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PipelineConfigError(f"{path}: invalid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise PipelineConfigError(f"{path}: top-level must be a mapping")
    unknown_fields = set(raw) - _PIPELINE_FIELDS
    if unknown_fields:
        fields = ", ".join(sorted(repr(field) for field in unknown_fields))
        raise PipelineConfigError(
            f"{path}: unsupported top-level fields: {fields}"
        )

    config_name = raw.get("name")
    if not isinstance(config_name, str) or not config_name:
        raise PipelineConfigError(f"{path}: ``name`` must be a non-empty string")
    # Catch the silent-rename trap: if the operator renames the file but not
    # the ``name:`` field (or vice versa), the runner's dispatch table will
    # disagree with the config's self-identification.
    if config_name != name:
        raise PipelineConfigError(
            f"{path}: file alias {name!r} does not match ``name: {config_name!r}``"
        )
    if not config_name.endswith("-pipeline"):
        raise PipelineConfigError(
            f"{path}: ``name`` must end with ``-pipeline`` (got {config_name!r})"
        )

    description = raw.get("description", "")
    if not isinstance(description, str):
        raise PipelineConfigError(f"{path}: ``description`` must be a string")

    chain_basename = raw.get("chain_output_basename", _DEFAULT_CHAIN_OUTPUT)
    if not isinstance(chain_basename, str) or not _is_safe_file_basename(
        chain_basename
    ):
        raise PipelineConfigError(
            f"{path}: ``chain_output_basename`` must be a safe file basename"
        )

    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise PipelineConfigError(f"{path}: ``steps`` must be a non-empty list")

    steps: list[PipelineStep] = []
    seen_skills: set[str] = set()
    for idx, raw_step in enumerate(raw_steps):
        if not isinstance(raw_step, dict):
            raise PipelineConfigError(
                f"{path}: step #{idx} must be a mapping with a ``skill`` key"
            )
        skill = raw_step.get("skill")
        if not isinstance(skill, str) or not _is_simple_alias(skill):
            raise PipelineConfigError(
                f"{path}: step #{idx} ``skill`` must be a canonical skill alias"
            )
        unknown_step_fields = set(raw_step) - _PIPELINE_STEP_FIELDS
        if unknown_step_fields:
            fields = ", ".join(
                sorted(repr(field) for field in unknown_step_fields)
            )
            raise PipelineConfigError(
                f"{path}: step #{idx} has unsupported fields: {fields}"
            )
        if skill in seen_skills:
            raise PipelineConfigError(
                f"{path}: duplicate step skill {skill!r} is not supported"
            )
        seen_skills.add(skill)
        steps.append(PipelineStep(skill=skill))

    return PipelineConfig(
        name=config_name,
        description=description,
        chain_output_basename=chain_basename,
        steps=tuple(steps),
    )


def list_available_pipelines(*, pipelines_dir: Path | None = None) -> list[str]:
    """Return contained ``*.yaml`` files whose stems are canonical aliases."""
    target = Path(pipelines_dir or PIPELINES_DIR)
    if not target.exists():
        return []
    available: list[str] = []
    for candidate in target.glob("*.yaml"):
        try:
            path = pipeline_config_path(candidate.stem, pipelines_dir=target)
        except PipelineConfigError:
            continue
        if path.is_file():
            available.append(candidate.stem)
    return sorted(available)
