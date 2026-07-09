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
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

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

# ``consensus`` — a shim over the consensus runtime (ADR 0016): one analysis
# type, many methods, scored + voted into a typed consensus (skills named
# ``consensus-*`` / ``sc-consensus-*``). ``workflow`` is RESERVED for a future
# composition type that chains DIFFERENT analysis skills into a pipeline; no
# skill uses it yet. ``leaf`` (default) is a single self-contained analysis.
SkillType = Literal["leaf", "workflow", "consensus"]
RuntimeLanguage = Literal["python", "r", "bash"]
ValidationLevel = Literal[
    "smoke-only", "demo-validated", "fixture-validated", "benchmarked", "production"
]
LifecycleStatus = Literal["draft", "mvp", "stable", "deprecated"]
Origin = Literal["human", "scaffolded", "promoted", "migrated", "corpus"]
RSource = Literal["cran", "bioc"]


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


class Preconditions(_Strict):
    data_shape: DataShape = Field(default_factory=DataShape)
    env: list[str] = Field(default_factory=list)      # required env vars (checked, never installed)
    config: list[str] = Field(default_factory=list)   # required config state (checked, never installed)


class Inputs(_Strict):
    modalities: list[str] = Field(default_factory=list)
    file_types: list[str] = Field(default_factory=list)
    preconditions: Preconditions = Field(default_factory=Preconditions)


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
    obs: list[str] = Field(default_factory=list)
    obsm: list[str] = Field(default_factory=list)
    var: list[str] = Field(default_factory=list)
    layers: list[str] = Field(default_factory=list)
    uns: list[str] = Field(default_factory=list)


class Outputs(_Strict):
    files: list[str] = Field(default_factory=list)
    result_json: ResultJson = Field(default_factory=ResultJson)
    anndata: Optional[AnnDataOutputs] = None


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
class Resources(_Strict):
    references: list[str] = Field(default_factory=list)
    figures: Optional[str] = None
    demo: Optional[str] = None
    tests: Optional[str] = None
    homepage: Optional[str] = None


# ── governance ───────────────────────────────────────────────────────────────
class Lifecycle(_Strict):
    status: LifecycleStatus = "mvp"
    superseded_by: Optional[str] = None


class Validation(_Strict):
    level: ValidationLevel = "smoke-only"
    evidence: list[str] = Field(default_factory=list)


class Provenance(_Strict):
    origin: Origin = "human"
    migrated_from: Optional[str] = None   # knowledge_base/<topic> when migrated
    source_hash: Optional[str] = None
    source_license: Optional[str] = None
    source_ref: Optional[str] = None      # DOI/URL/PMID (or filename fallback) when origin=="corpus"


class Security(_Strict):
    """Makes the local-first iron rules schema-enforceable + auditable."""

    data_egress: Literal["none", "optional"] = "none"
    network: Literal["none", "optional"] = "none"
    writes: Literal["output_dir_only", "workspace", "unrestricted"] = "output_dir_only"


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
    security: Security = Field(default_factory=Security)
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
