"""Declarative skill representation — the single source of truth (ADR 0037).

A v2 skill is described by one machine contract, ``skill.yaml``, validated by the
pydantic models below. ``SKILL.md`` is a pure narrative card; all derived
artifacts (catalog.json, references/parameters.md, routing table, INDEX.md,
SKILL.md header) are generated one-way from ``skill.yaml``.

This module is the ONLY parser of the skill contract — it replaces the
hand-coded constants previously re-implemented in scripts/skill_lint.py,
omicsclaw/skill/lazy_metadata.py, scripts/generate_catalog.py, and
omicsclaw/skill/execution/dep_spec.py (migration is gradual; see
scripts/migrate_to_skill_yaml.py and the schema_version coexistence rule).

Design decisions captured here (see ADR 0037 "deps granularity"):
- ``deps`` lists only install/provision channels. ``deps.python`` is the only
  bucket consumed today; ``deps.r`` / ``deps.cli`` are forward extensions added
  only with a real consumer ("no consumer, no bucket").
- pip/conda/non-pip/deny is DERIVED centrally by dep_spec.kind_of() — it is NOT
  a declared bucket, so there is no ``deps.conda``.
- ``env`` / ``config`` are runtime PRECONDITIONS (checked, never installed) and
  live under ``interface.inputs.preconditions``, not ``deps``.
- ``os`` is a compatibility MARKER (``compatibility.platforms``), not an install
  target.
- ``bash`` is a ``runtime.language`` (how the entry runs), not a dep bucket; the
  external-binary bucket is ``deps.cli``.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Literal, Optional

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from omicsclaw.common.output_claim import is_output_claim_path

SCHEMA_VERSION = 2

# Framework-reserved CLI flags that the runner always injects; a skill must not
# declare them in allowed_extra_flags (argv_builder blocks them).
RESERVED_FLAGS = frozenset({"--input", "--output", "--demo"})

# The 8 OmicsClaw domains (7 analysis/orchestration + literature).
DOMAINS = frozenset(
    {
        "spatial",
        "singlecell",
        "genomics",
        "proteomics",
        "metabolomics",
        "bulkrna",
        "orchestrator",
        "literature",
    }
)
PLATFORMS = frozenset({"linux", "macos", "windows"})
ARCHITECTURES = frozenset({"x86_64", "arm64"})

# Interpreter names that must never appear in deps.cli (language goes in
# runtime.language). Matched after normalising path/case/.exe/version suffix.
_INTERPRETERS = frozenset({"python", "bash", "sh", "rscript"})
_PYTHON_VER_RE = re.compile(r"python\d+(\.\d+)*$")
_ARTIFACT_KIND_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_ARTIFACT_FORMAT_RE = re.compile(r"^[a-z0-9]+(?:[._+-][a-z0-9]+)*$")

# ``consensus`` — a shim over the consensus runtime (ADR 0016): one analysis
# type, many methods, scored + voted into a typed consensus (skills named
# ``consensus-*`` / ``sc-consensus-*``). ``workflow`` is RESERVED for a future
# composition type that chains DIFFERENT analysis skills into a pipeline; no
# skill uses it yet. ``leaf`` (default) is a single self-contained analysis.
SkillType = Literal["leaf", "workflow", "consensus"]
RuntimeLanguage = Literal["python", "r", "bash"]
InputPathKind = Literal["file", "directory", "freeform"]
DirectorySignature = Literal[
    "fastq-collection",
    "paired-fastq",
    "tenx-matrix",
    "cellranger-output",
    "starsolo-output",
    "starsolo-velocity",
    "pseudoalign-output",
]
ValidationLevel = Literal[
    "smoke-only", "demo-validated", "fixture-validated", "benchmarked", "production"
]
LifecycleStatus = Literal["draft", "mvp", "stable", "deprecated"]
Origin = Literal["human", "scaffolded", "promoted", "migrated", "corpus"]
RSource = Literal["cran", "bioc"]
AnnDataProcessingState = Literal["raw", "standardized", "preprocessed"]


def _clean_str_list(v: list[str]) -> list[str]:
    """Drop empty/whitespace items and de-duplicate, preserving order."""
    seen: set[str] = set()
    out: list[str] = []
    for item in v:
        s = str(item).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


class _Strict(BaseModel):
    """Base: reject unknown keys so schema drift fails loudly."""

    model_config = ConfigDict(extra="forbid")


# ── C: applicability conditions ──────────────────────────────────────────────
class SkipRule(_Strict):
    condition: str = Field(min_length=1)
    use: Optional[str] = None        # the skill to use instead, if any
    rationale: Optional[str] = None


class Summary(_Strict):
    load_when: str = Field(min_length=1)
    skip_when: list[SkipRule] = Field(default_factory=list)
    trigger_keywords: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)

    @field_validator("trigger_keywords", "tags", "aliases")
    @classmethod
    def _clean(cls, v: list[str]) -> list[str]:
        return _clean_str_list(v)


class DataShape(BaseModel):
    """AnnData/data-shape preconditions. Open-ended (extra allowed) because
    different omics carry different obs/obsm/uns/var/layers expectations."""

    model_config = ConfigDict(extra="allow")
    requires_preprocessed: bool = False
    obs: list[str] = Field(default_factory=list)
    obsm: list[str] = Field(default_factory=list)


class TabularContent(_Strict):
    """Bounded header facts required from CSV/TSV inputs."""

    min_columns: Optional[int] = Field(default=None, ge=1)
    required_columns: list[str] = Field(default_factory=list)

    @field_validator("required_columns")
    @classmethod
    def _clean_columns(cls, values: list[str]) -> list[str]:
        return _clean_str_list(values)

    @model_validator(mode="after")
    def _require_a_constraint(self):
        if self.min_columns is None and not self.required_columns:
            raise ValueError("tabular content requires at least one constraint")
        return self


class VcfContent(_Strict):
    """Header facts required from a bounded VCF metadata probe."""

    require_fileformat_header: bool = False
    required_columns: list[str] = Field(default_factory=list)
    required_info_ids: list[str] = Field(default_factory=list)
    required_format_ids: list[str] = Field(default_factory=list)
    min_samples: Optional[int] = Field(default=None, ge=1)

    @field_validator(
        "required_columns",
        "required_info_ids",
        "required_format_ids",
    )
    @classmethod
    def _clean_header_names(cls, values: list[str]) -> list[str]:
        return _clean_str_list(values)

    @model_validator(mode="after")
    def _require_a_constraint(self):
        if not any(
            (
                self.require_fileformat_header,
                self.required_columns,
                self.required_info_ids,
                self.required_format_ids,
                self.min_samples is not None,
            )
        ):
            raise ValueError("VCF content requires at least one constraint")
        return self


class FastqContent(_Strict):
    """Record and mate-layout facts required from FASTQ inputs."""

    require_valid_record: bool = False
    pairing: Literal["any", "single", "paired"] = "any"

    @model_validator(mode="after")
    def _require_a_constraint(self):
        if not self.require_valid_record and self.pairing == "any":
            raise ValueError("FASTQ content requires at least one constraint")
        return self


class DirectoryContent(_Strict):
    """One-of semantic layouts required from a bounded directory probe."""

    any_of_signatures: list[DirectorySignature] = Field(min_length=1)

    @field_validator("any_of_signatures")
    @classmethod
    def _deduplicate_signatures(
        cls,
        values: list[DirectorySignature],
    ) -> list[DirectorySignature]:
        return list(dict.fromkeys(values))


class ContentPreconditions(_Strict):
    """Format-specific structure checked by lightweight content probes."""

    tabular: Optional[TabularContent] = None
    vcf: Optional[VcfContent] = None
    fastq: Optional[FastqContent] = None
    directory: Optional[DirectoryContent] = None

    @model_validator(mode="after")
    def _require_a_probe(self):
        if all(
            probe is None
            for probe in (self.tabular, self.vcf, self.fastq, self.directory)
        ):
            raise ValueError("content preconditions require at least one format probe")
        return self


class Preconditions(_Strict):
    data_shape: DataShape = Field(default_factory=DataShape)
    env: list[str] = Field(default_factory=list)      # required env vars (checked, never installed)
    config: list[str] = Field(default_factory=list)   # required config state (checked, never installed)
    content: Optional[ContentPreconditions] = None


class InputArtifact(_Strict):
    """A semantic artifact type this skill can consume.

    ``kind`` carries workflow meaning; ``formats`` prevents a shared semantic
    label from connecting physically incompatible files. An empty format list
    means the consumer accepts every representation of that exact kind.
    """

    kind: str = Field(min_length=1)
    formats: list[str] = Field(default_factory=list)

    @field_validator("kind")
    @classmethod
    def _check_kind(cls, value: str) -> str:
        value = value.strip().lower()
        if not _ARTIFACT_KIND_RE.fullmatch(value):
            raise ValueError("artifact kind must be a lowercase dotted identifier")
        return value

    @field_validator("formats")
    @classmethod
    def _check_formats(cls, values: list[str]) -> list[str]:
        cleaned = [value.lower().lstrip(".") for value in _clean_str_list(values)]
        bad = [value for value in cleaned if not _ARTIFACT_FORMAT_RE.fullmatch(value)]
        if bad:
            raise ValueError(f"artifact formats must be normalized identifiers: {bad}")
        return cleaned


class OutputArtifact(_Strict):
    """A semantic artifact type produced at one declared output path."""

    kind: str = Field(min_length=1)
    path: str = Field(min_length=1)
    format: str = Field(min_length=1)

    @field_validator("kind")
    @classmethod
    def _check_kind(cls, value: str) -> str:
        return InputArtifact._check_kind(value)

    @field_validator("path")
    @classmethod
    def _check_path(cls, value: str) -> str:
        value = value.strip().replace("\\", "/")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("artifact path must stay relative to the skill output directory")
        if is_output_claim_path(Path(value)):
            raise ValueError("artifact path uses a reserved internal output filename")
        return value

    @field_validator("format")
    @classmethod
    def _check_format(cls, value: str) -> str:
        value = value.strip().lower().lstrip(".")
        if not _ARTIFACT_FORMAT_RE.fullmatch(value):
            raise ValueError("artifact format must be a normalized identifier")
        return value


class Inputs(_Strict):
    modalities: list[str] = Field(default_factory=list)
    file_types: list[str] = Field(default_factory=list)
    path_kinds: list[InputPathKind] = Field(default_factory=lambda: ["file"])
    artifacts: list[InputArtifact] = Field(default_factory=list)
    preconditions: Preconditions = Field(default_factory=Preconditions)

    @field_validator("path_kinds")
    @classmethod
    def _check_path_kinds(cls, v: list[InputPathKind]) -> list[InputPathKind]:
        cleaned = list(dict.fromkeys(v))
        if not cleaned:
            raise ValueError("path_kinds must declare at least one input kind")
        return cleaned

    @model_validator(mode="after")
    def _content_probes_match_declared_inputs(self):
        content = self.preconditions.content
        if content is None:
            return self
        file_types = {value.lower().lstrip(".") for value in self.file_types}
        accepts_any_file = "*" in file_types
        if content.tabular and not (
            accepts_any_file or file_types.intersection({"csv", "tsv"})
        ):
            raise ValueError("tabular content probe requires csv/tsv file_types")
        if content.vcf and not (accepts_any_file or "vcf" in file_types):
            raise ValueError("VCF content probe requires vcf file_types")
        if content.fastq and not (
            accepts_any_file or file_types.intersection({"fastq", "fq"})
        ):
            raise ValueError("FASTQ content probe requires fastq/fq file_types")
        if content.directory and "directory" not in self.path_kinds:
            raise ValueError(
                "directory content probe requires directory in path_kinds"
            )
        return self


class Parameters(_Strict):
    allowed_extra_flags: list[str] = Field(default_factory=list)
    hints: dict = Field(default_factory=dict)

    @field_validator("allowed_extra_flags")
    @classmethod
    def _check_flags(cls, v: list[str]) -> list[str]:
        v = _clean_str_list(v)
        bad = sorted(RESERVED_FLAGS.intersection(v))
        if bad:
            raise ValueError(
                f"reserved framework flags must not be declared: {bad} "
                "(--input/--output/--demo are injected by the runner)"
            )
        nonkebab = [f for f in v if not re.fullmatch(r"--[a-z0-9]+(-[a-z0-9]+)*", f)]
        if nonkebab:
            raise ValueError(f"allowed_extra_flags must be --kebab-case: {nonkebab}")
        return v


class ResultJson(_Strict):
    required_keys: list[str] = Field(default_factory=list)


class AnnDataOutputs(_Strict):
    saves_h5ad: bool = False
    # Explicit postcondition consumed by the compatibility graph. A filename
    # such as ``processed.h5ad`` is not evidence that preprocessing occurred.
    processing_state: Optional[AnnDataProcessingState] = None
    obs: list[str] = Field(default_factory=list)
    obsm: list[str] = Field(default_factory=list)
    var: list[str] = Field(default_factory=list)
    layers: list[str] = Field(default_factory=list)
    uns: list[str] = Field(default_factory=list)


class ScopedAnnDataOutputs(_Strict):
    """AnnData fields guaranteed only by a selected runtime method."""

    obs: list[str] = Field(default_factory=list)
    obsm: list[str] = Field(default_factory=list)
    var: list[str] = Field(default_factory=list)
    layers: list[str] = Field(default_factory=list)
    uns: list[str] = Field(default_factory=list)


class MethodScopedOutputs(_Strict):
    """Additional output guarantees shared by one or more ``--method`` values."""

    methods: list[str] = Field(min_length=1)
    files: list[str] = Field(default_factory=list)
    anndata: Optional[ScopedAnnDataOutputs] = None
    artifacts: list[OutputArtifact] = Field(default_factory=list)

    @field_validator("files")
    @classmethod
    def _reject_reserved_files(cls, values: list[str]) -> list[str]:
        if any(is_output_claim_path(Path(value)) for value in values):
            raise ValueError("files contains a reserved internal output filename")
        return values

    @field_validator("methods")
    @classmethod
    def _check_methods(cls, values: list[str]) -> list[str]:
        cleaned = _clean_str_list(values)
        bad = [
            value
            for value in cleaned
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", value)
        ]
        if bad:
            raise ValueError(
                f"methods must be canonical method identifiers: {bad}"
            )
        return cleaned

    @model_validator(mode="after")
    def _require_a_guarantee(self):
        anndata_fields = bool(
            self.anndata
            and any(
                getattr(self.anndata, collection)
                for collection in ("obs", "obsm", "var", "layers", "uns")
            )
        )
        if not self.files and not self.artifacts and not anndata_fields:
            raise ValueError(
                "method-scoped outputs require at least one output guarantee"
            )
        return self


class Outputs(_Strict):
    files: list[str] = Field(default_factory=list)
    result_json: ResultJson = Field(default_factory=ResultJson)
    anndata: Optional[AnnDataOutputs] = None
    artifacts: list[OutputArtifact] = Field(default_factory=list)
    method_scopes: list[MethodScopedOutputs] = Field(default_factory=list)

    @field_validator("files")
    @classmethod
    def _reject_reserved_files(cls, values: list[str]) -> list[str]:
        if any(is_output_claim_path(Path(value)) for value in values):
            raise ValueError("files contains a reserved internal output filename")
        return values

    @model_validator(mode="after")
    def _artifacts_reference_declared_files(self):
        declared = {path.replace("\\", "/") for path in self.files}
        missing = [artifact.path for artifact in self.artifacts if artifact.path not in declared]
        if missing:
            raise ValueError(
                f"artifact output paths must also appear in outputs.files: {missing}"
            )
        scoped_paths = [
            path.replace("\\", "/")
            for scope in self.method_scopes
            for path in scope.files
        ] + [
            artifact.path
            for scope in self.method_scopes
            for artifact in scope.artifacts
        ]
        missing_scoped = sorted(set(scoped_paths) - declared)
        if missing_scoped:
            raise ValueError(
                "method-scoped output paths must also appear in outputs.files: "
                f"{missing_scoped}"
            )
        if any(scope.anndata is not None for scope in self.method_scopes) and not (
            self.anndata is not None and self.anndata.saves_h5ad
        ):
            raise ValueError(
                "method-scoped AnnData fields require outputs.anndata.saves_h5ad=true"
            )
        scoped_methods = [
            method
            for scope in self.method_scopes
            for method in scope.methods
        ]
        duplicate_methods = sorted(
            method
            for method in set(scoped_methods)
            if scoped_methods.count(method) > 1
        )
        if duplicate_methods:
            raise ValueError(
                "each method may appear in only one output scope: "
                f"{duplicate_methods}"
            )
        kinds = [artifact.kind for artifact in self.artifacts] + [
            artifact.kind
            for scope in self.method_scopes
            for artifact in scope.artifacts
        ]
        if len(kinds) != len(set(kinds)):
            raise ValueError(
                "each output artifact kind may be declared only once across "
                "global and method-scoped outputs"
            )
        return self


class Interface(_Strict):
    inputs: Inputs = Field(default_factory=Inputs)
    parameters: Parameters = Field(default_factory=Parameters)
    outputs: Outputs = Field(default_factory=Outputs)


# ── M: execution ─────────────────────────────────────────────────────────────
class Runtime(_Strict):
    language: RuntimeLanguage = "python"
    entry: str = Field(min_length=1)


# ── deps: install/provision channels only ────────────────────────────────────
class RPackage(_Strict):
    name: str = Field(min_length=1)
    source: RSource = "cran"


class RDeps(_Strict):
    packages: list[RPackage] = Field(default_factory=list)
    required_for: list[str] = Field(default_factory=list)


class Deps(_Strict):
    python: list[str] = Field(default_factory=list)   # the only auto-provisioned channel
    r: Optional[RDeps] = None                          # future extension (R skills)
    cli: list[str] = Field(default_factory=list)       # future extension (external binaries)

    @field_validator("python", "cli")
    @classmethod
    def _clean(cls, v: list[str]) -> list[str]:
        return _clean_str_list(v)

    @field_validator("cli")
    @classmethod
    def _reject_interpreter(cls, v: list[str]) -> list[str]:
        bad = []
        for name in v:
            norm = Path(name).name.lower()
            if norm.endswith(".exe"):
                norm = norm[:-4]
            if norm in _INTERPRETERS or _PYTHON_VER_RE.fullmatch(norm):
                bad.append(name)
        if bad:
            raise ValueError(
                f"deps.cli must list real external binaries, not interpreters: {sorted(bad)} "
                "(language goes in runtime.language)"
            )
        return v


class Compatibility(_Strict):
    platforms: list[str] = Field(default_factory=list)
    architectures: list[str] = Field(default_factory=list)

    @field_validator("platforms")
    @classmethod
    def _check_platforms(cls, v: list[str]) -> list[str]:
        v = _clean_str_list(v)
        bad = sorted(set(v) - PLATFORMS)
        if bad:
            raise ValueError(f"unknown platforms {bad}; allowed: {sorted(PLATFORMS)}")
        return v

    @field_validator("architectures")
    @classmethod
    def _check_arch(cls, v: list[str]) -> list[str]:
        v = _clean_str_list(v)
        bad = sorted(set(v) - ARCHITECTURES)
        if bad:
            raise ValueError(f"unknown architectures {bad}; allowed: {sorted(ARCHITECTURES)}")
        return v


# ── R: resources ─────────────────────────────────────────────────────────────
class ComputeResources(_Strict):
    """Static admission reservation for one skill process.

    These values let the Candidate plan scheduler avoid declared host
    overcommit.  They are not OS-level quotas and do not predict
    data-size-dependent peak usage.
    """

    cpu_cores: int = Field(ge=1, strict=True)
    memory_mib: int = Field(ge=1, strict=True)
    gpu_devices: int = Field(ge=0, strict=True)
    threads: int = Field(ge=1, strict=True)
    temporary_disk_mib: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def _threads_fit_reserved_cpu(self):
        if self.threads > self.cpu_cores:
            raise ValueError("threads cannot exceed reserved cpu_cores")
        return self


class Resources(_Strict):
    references: list[str] = Field(default_factory=list)
    figures: Optional[str] = None
    demo: Optional[str] = None
    tests: Optional[str] = None
    homepage: Optional[str] = None
    compute: Optional[ComputeResources] = None


# ── governance ───────────────────────────────────────────────────────────────
class Lifecycle(_Strict):
    status: LifecycleStatus = "mvp"
    superseded_by: Optional[str] = None

    @model_validator(mode="after")
    def _replacement_matches_deprecated_state(self):
        replacement = str(self.superseded_by or "").strip()
        if self.status == "deprecated" and not replacement:
            raise ValueError("deprecated lifecycle requires superseded_by")
        if self.status != "deprecated" and replacement:
            raise ValueError("superseded_by is only valid for deprecated skills")
        self.superseded_by = replacement or None
        return self


ProtocolKind = Literal["demo", "fixture", "benchmark", "stability"]


class EvaluationProtocol(_Strict):
    """A versioned, digestible evaluation declaration (ADR 0074 §6.1).

    Only a declared protocol can earn a validation level above ``smoke-only``.
    The executable lives under ``tests/`` (an implementation resource); the
    manifest declares the stable id, kind, entry, durable dataset reference and
    repeat policy. ``repeats`` bounds the stability repetitions a protocol asks
    for — there is no universal five-run rule.
    """

    id: str = Field(min_length=1)
    kind: ProtocolKind
    entry: str = Field(min_length=1)
    dataset_ref: Optional[str] = None
    repeats: int = Field(default=1, ge=1, le=100)
    # Allowlist of scientific metric names this protocol may publish into the
    # Experience View's stability dispersion. ONLY these names are captured from
    # a run's output (ADR 0074 §5.2/§11.5); a runner cannot inject arbitrary
    # metric keys into the public read model.
    metrics: list[str] = Field(default_factory=list)

    @field_validator("metrics")
    @classmethod
    def _clean_metrics(cls, v: list[str]) -> list[str]:
        # Canonical (sorted, de-duplicated, bounded) so the protocol digest is
        # allowlist-order-agnostic and the read model stays bounded.
        cleaned = sorted({s.strip() for s in v if s and s.strip()})
        if len(cleaned) > 32:
            raise ValueError("metrics allowlist exceeds 32 entries")
        if any(len(name) > 64 for name in cleaned):
            raise ValueError("metric name exceeds 64 characters")
        return cleaned


class Validation(_Strict):
    level: ValidationLevel = "smoke-only"
    evidence: list[str] = Field(default_factory=list)
    protocols: list[EvaluationProtocol] = Field(default_factory=list)


class Provenance(_Strict):
    origin: Origin = "human"
    migrated_from: Optional[str] = None   # knowledge_base/<topic> when migrated
    source_hash: Optional[str] = None
    source_license: Optional[str] = None
    source_ref: Optional[str] = None      # DOI/URL/PMID (or filename fallback) when origin=="corpus"


class Security(_Strict):
    """An explicitly reviewed declarative security capability statement.

    These fields describe expected behavior; they are not proof of OS-level
    network or filesystem confinement.
    """

    data_egress: Literal["none", "optional"]
    network: Literal["none", "optional"]
    writes: Literal["output_dir_only", "workspace", "unrestricted"]


class Mcp(_Strict):
    expose: bool = False
    tool_name: Optional[str] = None
    input_schema_strategy: Optional[str] = None


class SkillManifest(_Strict):
    """The complete declarative skill contract (``skill.yaml`` v2)."""

    schema_version: int                       # required (no default) — coexistence marker
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    type: SkillType = "leaf"
    version: str = Field(min_length=1)
    # identity metadata preserved from v1 frontmatter (kept lossless;
    # matches the ADR 0037 field tree author/license/emoji fields):
    author: Optional[str] = None
    license: Optional[str] = None
    emoji: Optional[str] = None

    summary: Summary
    interface: Interface = Field(default_factory=Interface)
    runtime: Runtime
    deps: Deps = Field(default_factory=Deps)
    compatibility: Compatibility = Field(default_factory=Compatibility)
    resources: Resources = Field(default_factory=Resources)
    lifecycle: Lifecycle = Field(default_factory=Lifecycle)
    validation: Validation = Field(default_factory=Validation)
    provenance: Provenance = Field(default_factory=Provenance)
    # Absence means "not reviewed".  Do not reconstruct apparently safe
    # defaults for skills whose network/write behavior has never been audited.
    security: Optional[Security] = None
    mcp: Mcp = Field(default_factory=Mcp)

    @field_validator("schema_version")
    @classmethod
    def _is_v2(cls, v: int) -> int:
        if v != SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {SCHEMA_VERSION}, got {v!r}")
        return v

    @field_validator("domain")
    @classmethod
    def _known_domain(cls, v: str) -> str:
        if v not in DOMAINS:
            raise ValueError(f"unknown domain {v!r}; allowed: {sorted(DOMAINS)}")
        return v

    @model_validator(mode="after")
    def _lifecycle_cannot_replace_self(self):
        if self.lifecycle.superseded_by == self.id:
            raise ValueError("a skill cannot supersede itself")
        return self

    @model_validator(mode="after")
    def _method_scopes_reference_parameter_hints(self):
        scoped_methods = {
            method
            for scope in self.interface.outputs.method_scopes
            for method in scope.methods
        }
        unknown = sorted(scoped_methods - set(self.interface.parameters.hints))
        if unknown:
            raise ValueError(
                "method-scoped outputs must reference canonical "
                f"interface.parameters.hints keys: {unknown}"
            )
        return self

    def to_yaml(self) -> str:
        """Serialize to a deterministic, ordered YAML document for ``skill.yaml``.

        ``exclude_defaults`` omits every field that equals its schema default —
        the governance/security/mcp blocks when unset, ``type: leaf``,
        ``runtime.language: python``, empty lists, etc. Those are reconstructed
        identically on load, so the authored file carries only what actually
        varies. ``exclude_none`` additionally drops optional-None fields.
        Round-trip identity holds: ``parse_skill_manifest(yaml.safe_load(m.to_yaml())) == m``.
        """
        data = self.model_dump(exclude_defaults=True, exclude_none=True)
        return yaml.safe_dump(
            data, sort_keys=False, default_flow_style=False, allow_unicode=True, width=100
        )


def parse_skill_manifest(data: dict) -> SkillManifest:
    """Validate a raw dict into a SkillManifest (raises pydantic ValidationError)."""
    return SkillManifest.model_validate(data)


def load_skill_yaml(path: Path) -> SkillManifest:
    """Load and validate a ``skill.yaml`` file."""
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: skill.yaml must be a mapping, got {type(raw).__name__}")
    return parse_skill_manifest(raw)


def validate_skill_yaml(path: Path) -> list[str]:
    """Return a list of human-readable validation errors (empty == valid)."""
    try:
        load_skill_yaml(path)
        return []
    except (ValidationError, ValueError, yaml.YAMLError) as exc:
        if isinstance(exc, ValidationError):
            return [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        return [str(exc)]


def is_v2_skill(skill_dir: Path) -> bool:
    """True when a skill directory uses the v2 contract (``skill.yaml`` present)."""
    return (Path(skill_dir) / "skill.yaml").exists()
