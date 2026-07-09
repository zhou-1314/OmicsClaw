from __future__ import annotations

import logging
import re
from pathlib import Path
import yaml

logger = logging.getLogger(__name__)

# Fields that lazy_metadata exposes as properties.  When a v2 sidecar
# (`parameters.yaml`) is present, every field except name/description is read
# from the sidecar; otherwise we fall back to the legacy
# `metadata.omicsclaw` block in the SKILL.md frontmatter.
_RUNTIME_FIELDS = (
    "domain",
    "script",
    "type",
    "validation_level",
    "trigger_keywords",
    "allowed_extra_flags",
    "legacy_aliases",
    "saves_h5ad",
    "requires_preprocessed",
    "param_hints",
)

# Declared skill types (ADR 0030).  `type` is optional in the sidecar; a
# missing/blank value falls back to `leaf`, so the existing single-script
# skills need no edit.
SKILL_TYPES = ("leaf", "workflow", "consensus", "knowledge", "adapter")
_DEFAULT_SKILL_TYPE = "leaf"

# Scientific-validation maturity ladder (ADR 0030 §3).  Orthogonal to `status`
# (which records availability).  Optional in the sidecar; a missing/blank/unknown
# value falls back to `smoke-only` (the skill at least runs `--demo`).
VALIDATION_LEVELS = (
    "smoke-only",
    "demo-validated",
    "fixture-validated",
    "benchmarked",
    "production",
)
_DEFAULT_VALIDATION_LEVEL = "smoke-only"

# Governance lifecycle stage (skill.yaml `lifecycle.status`, ADR 0030/acquisition
# P0 contract). Orthogonal to *availability* (`has_script`/`has_demo` in
# generate_catalog.py) — a draft skill can already have a runnable placeholder
# script. v1 (pre-v2, hand-written) skills predate this field entirely, so they
# default to "mvp": already-shipped, not a fresh unproven scaffold.
LIFECYCLE_STATUSES = ("draft", "mvp", "stable", "deprecated")
_DEFAULT_LIFECYCLE_STATUS = "mvp"

# Authorship provenance (skill.yaml `provenance.origin`). v1 skills predate this
# field and were all hand-written, so they default to "human".
ORIGINS = ("human", "scaffolded", "promoted", "migrated", "corpus")
_DEFAULT_ORIGIN = "human"

_RUNTIME_DEFAULTS: dict[str, object] = {
    "domain": "",
    "script": "",
    "type": _DEFAULT_SKILL_TYPE,
    "validation_level": _DEFAULT_VALIDATION_LEVEL,
    "trigger_keywords": [],
    "allowed_extra_flags": [],
    "legacy_aliases": [],
    "saves_h5ad": False,
    "requires_preprocessed": False,
    "param_hints": {},
}


_GOTCHA_BOLD_LEAD = re.compile(r"^\*\*(.+?)\*\*")
_GOTCHA_PLACEHOLDER = re.compile(r"^_None\b", re.IGNORECASE)


def _extract_gotcha_leads(body: str) -> list[str]:
    """Extract the lead sentence of each `## Gotchas` bullet.

    Reads the body until the next `## ` heading.  Each bullet starts with
    `- ` at the line start (indented continuations are ignored).  The lead
    sentence is the bold-marked first sentence; missing bold falls back to
    the first '. '-terminated sentence.  Italic placeholder bullets like
    `- _None yet — append as failure modes are reported._` are filtered.
    """
    in_section = False
    bullets: list[str] = []
    for line in body.splitlines():
        stripped = line.lstrip()
        if line.startswith("## "):
            if in_section:
                break
            if line.startswith("## Gotchas"):
                in_section = True
            continue
        if not in_section:
            continue
        if not line.startswith("- "):
            continue
        # `- ` at start = new bullet; capture the rest of the line.
        bullets.append(stripped[2:].strip())

    leads: list[str] = []
    for bullet in bullets:
        if _GOTCHA_PLACEHOLDER.match(bullet):
            continue
        m = _GOTCHA_BOLD_LEAD.match(bullet)
        if m:
            leads.append(m.group(1).strip())
            continue
        # Fallback: first sentence on '. ' boundary, else whole bullet.
        first_period = bullet.find(". ")
        if first_period > 0:
            leads.append(bullet[: first_period + 1].strip())
        else:
            leads.append(bullet.rstrip("."))
    return leads


class LazySkillMetadata:
    def __init__(self, skill_path: Path):
        self.path = skill_path
        self._basic = None
        self._full = None
        self._gotchas: list[str] | None = None
        self._source: str | None = None  # "v2" (skill.yaml) | "v1" (frontmatter+sidecar)

    def _parse_frontmatter(self) -> dict | None:
        skill_md = self.path / "SKILL.md"
        if not skill_md.exists():
            return None

        content = skill_md.read_text(encoding="utf-8")
        if not content.startswith("---"):
            return None

        parts = content.split("---", 2)
        if len(parts) < 3:
            return None

        try:
            return yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError as exc:
            logger.warning(
                "Failed to parse SKILL.md frontmatter at %s: %s",
                skill_md,
                exc,
            )
            return None

    def _load_sidecar(self) -> dict | None:
        sidecar = self.path / "parameters.yaml"
        if not sidecar.exists():
            return None
        try:
            data = yaml.safe_load(sidecar.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            logger.warning(
                "Failed to parse parameters.yaml at %s: %s",
                sidecar,
                exc,
            )
            return None
        return data if isinstance(data, dict) else None

    def _try_load_v2(self):
        """Load skill.yaml (v2) when present and valid; else None (fall back to v1).

        The schema/pydantic import is deferred to here so v1-only installs never
        pay for pydantic, and a malformed or unparseable skill.yaml degrades to
        the v1 path instead of breaking the registry (schema_version coexistence,
        ADR 0037).
        """
        sidecar = self.path / "skill.yaml"
        if not sidecar.exists():
            return None
        try:
            from .schema import load_skill_yaml
        except Exception as exc:  # pydantic/schema unavailable
            logger.warning(
                "skill.yaml present at %s but schema unavailable (%s); using v1 metadata",
                sidecar,
                exc,
            )
            return None
        try:
            return load_skill_yaml(sidecar)
        except Exception as exc:
            logger.warning(
                "invalid skill.yaml at %s (%s); falling back to v1 metadata", sidecar, exc
            )
            return None

    @staticmethod
    def _reconstruct_description(summary) -> str:
        """Rebuild the canonical 'Load when … Skip when …' description from v2 summary."""
        load = (summary.load_when or "").strip().rstrip(".")
        text = f"Load when {load}." if load else ""
        if summary.skip_when:
            clauses = []
            for rule in summary.skip_when:
                clause = (rule.condition or "").strip().rstrip(".")
                if rule.use:
                    clause += f" (use {rule.use})"
                clauses.append(clause)
            text = (text + " " if text else "") + "Skip when " + "; ".join(clauses) + "."
        return text

    def _effective_allowed_flags(self, m) -> list[str]:
        """Flags the runtime gate accepts: explicit override, else derived (ADR 0041).

        ``skill.yaml``'s ``allowed_extra_flags`` is now an optional narrowing
        override (kept only where a skill exposes fewer flags than its script
        accepts, e.g. consensus subsets). When empty/absent — the common leaf
        case — the accepted flags are derived from the script's argparse surface
        so the list is no longer a hand-maintained mirror.
        """
        from .execution.flag_introspection import effective_allowed_flags

        return sorted(
            effective_allowed_flags(
                m.interface.parameters.allowed_extra_flags,
                self.path,
                m.runtime.entry,
                m.type,
            )
        )

    def _basic_from_v2(self, m) -> dict:
        """Map a v2 SkillManifest onto the legacy property surface (zero consumer churn)."""
        anndata = m.interface.outputs.anndata
        return {
            "name": m.name,
            "description": self._reconstruct_description(m.summary),
            "requires": list(m.deps.python),
            "domain": m.domain,
            "script": m.runtime.entry,
            "type": m.type,
            "validation_level": m.validation.level,
            "origin": m.provenance.origin,
            "lifecycle_status": m.lifecycle.status,
            "trigger_keywords": list(m.summary.trigger_keywords),
            "allowed_extra_flags": self._effective_allowed_flags(m),
            "legacy_aliases": list(m.summary.aliases),
            "saves_h5ad": bool(anndata.saves_h5ad) if anndata else False,
            "requires_preprocessed": bool(
                m.interface.inputs.preconditions.data_shape.requires_preprocessed
            ),
            "param_hints": dict(m.interface.parameters.hints),
            # identity metadata (catalog / desktop / generators read these)
            "version": m.version,
            "tags": list(m.summary.tags),
            "author": m.author or "",
            "license": m.license or "",
            "emoji": m.emoji or "",
        }

    def _load_basic(self):
        # v2 first: skill.yaml is the single source of truth (ADR 0037). When it
        # is present and valid, every legacy field is sourced from it; otherwise
        # we fall back to the v1 frontmatter + parameters.yaml path below.
        manifest = self._try_load_v2()
        if manifest is not None:
            self._basic = self._basic_from_v2(manifest)
            self._source = "v2"
            return
        self._source = "v1"

        # Tolerate missing/malformed frontmatter — the sidecar may still hold
        # the runtime contract.  Identity fields default to safe empties.
        frontmatter = self._parse_frontmatter() or {}
        legacy = (frontmatter.get("metadata") or {}).get("omicsclaw") or {}
        sidecar = self._load_sidecar() or {}

        # Per-field merge: sidecar wins where it speaks, frontmatter fills
        # gaps, defaults backstop both.  A bare YAML key (`field:`) parses to
        # None — treat that as "field absent" so partial migration and
        # null-valued collections do not crash callers.
        runtime: dict[str, object] = {}
        for key in _RUNTIME_FIELDS:
            # `dict.get(missing_key)` already returns None, so this two-step
            # fallthrough handles both "key absent" and "value is None" (bare
            # YAML key) uniformly.
            value = sidecar.get(key)
            if value is None:
                value = legacy.get(key)
            if value is None:
                value = _RUNTIME_DEFAULTS[key]
            runtime[key] = value

        # `requires:` (frontmatter top-level) is the reconciled Python-package
        # surface of the skill, regenerated by scripts/audit_skill_requires.py
        # from a static AST scan. It is DISTINCT from the ``parameters.yaml``
        # ``requires:`` ({bins, env, config} system-level contract) — only the
        # frontmatter *list* form is the pip dependency surface, so we read it
        # straight from the frontmatter and coerce anything else to []. Consumed
        # by the adaptive env resolver (ADR: adaptive-environment-provisioning).
        fm_requires = frontmatter.get("requires")
        requires = (
            [str(pkg).strip() for pkg in fm_requires if str(pkg).strip()]
            if isinstance(fm_requires, list)
            else []
        )

        # Identity metadata: version/tags/author/license live in frontmatter;
        # emoji is a parameters.yaml/legacy field (sidecar wins, then legacy,
        # then frontmatter).
        self._basic = {
            "name": frontmatter.get("name", ""),
            "description": frontmatter.get("description", ""),
            "requires": requires,
            "version": frontmatter.get("version", ""),
            "tags": frontmatter.get("tags") or [],
            "author": frontmatter.get("author") or "",
            "license": frontmatter.get("license") or "",
            "emoji": sidecar.get("emoji") or legacy.get("emoji") or frontmatter.get("emoji") or "",
            **runtime,
        }

    def _ensure_basic(self):
        if self._basic is None:
            self._load_basic()

    @property
    def source(self) -> str:
        """Which contract this metadata came from: 'v2' (skill.yaml) or 'v1'."""
        self._ensure_basic()
        return self._source or "v1"

    @property
    def name(self) -> str:
        self._ensure_basic()
        return self._basic.get("name", "")

    @property
    def description(self) -> str:
        self._ensure_basic()
        return self._basic.get("description", "")

    @property
    def requires(self) -> list[str]:
        """Reconciled Python-package surface from SKILL.md frontmatter `requires:`.

        The canonical/PyPI package names a skill imports (core + optional
        backends), regenerated by ``scripts/audit_skill_requires.py``. Empty
        list when absent. The adaptive env resolver probes these for
        importability before deciding to run in-place vs provision a venv.
        """
        self._ensure_basic()
        return self._basic.get("requires", [])

    @property
    def version(self) -> str:
        self._ensure_basic()
        return self._basic.get("version", "")

    @property
    def tags(self) -> list[str]:
        self._ensure_basic()
        return self._basic.get("tags", []) or []

    @property
    def author(self) -> str:
        self._ensure_basic()
        return self._basic.get("author", "")

    @property
    def license(self) -> str:
        self._ensure_basic()
        return self._basic.get("license", "")

    @property
    def emoji(self) -> str:
        self._ensure_basic()
        return self._basic.get("emoji", "")

    @property
    def domain(self) -> str:
        self._ensure_basic()
        return self._basic.get("domain", "")

    @property
    def script(self) -> str:
        self._ensure_basic()
        return self._basic.get("script", "")

    @property
    def type(self) -> str:
        """Declared skill type (ADR 0030); `leaf` when unset or unknown."""
        self._ensure_basic()
        value = self._basic.get("type") or _DEFAULT_SKILL_TYPE
        return value if value in SKILL_TYPES else _DEFAULT_SKILL_TYPE

    @property
    def validation_level(self) -> str:
        """Validation maturity (ADR 0030); `smoke-only` when unset or unknown."""
        self._ensure_basic()
        value = self._basic.get("validation_level") or _DEFAULT_VALIDATION_LEVEL
        return value if value in VALIDATION_LEVELS else _DEFAULT_VALIDATION_LEVEL

    @property
    def origin(self) -> str:
        """Authorship provenance; `human` when unset, unknown, or pre-v2."""
        self._ensure_basic()
        value = self._basic.get("origin") or _DEFAULT_ORIGIN
        return value if value in ORIGINS else _DEFAULT_ORIGIN

    @property
    def lifecycle_status(self) -> str:
        """Governance lifecycle stage; distinct from availability (has_script/has_demo)."""
        self._ensure_basic()
        value = self._basic.get("lifecycle_status") or _DEFAULT_LIFECYCLE_STATUS
        return value if value in LIFECYCLE_STATUSES else _DEFAULT_LIFECYCLE_STATUS

    @property
    def trigger_keywords(self) -> list[str]:
        self._ensure_basic()
        return self._basic.get("trigger_keywords", [])

    @property
    def allowed_extra_flags(self) -> set[str]:
        self._ensure_basic()
        return set(self._basic.get("allowed_extra_flags", []))

    @property
    def legacy_aliases(self) -> list[str]:
        self._ensure_basic()
        return self._basic.get("legacy_aliases", [])

    @property
    def saves_h5ad(self) -> bool:
        self._ensure_basic()
        return self._basic.get("saves_h5ad", False)

    @property
    def requires_preprocessed(self) -> bool:
        self._ensure_basic()
        return self._basic.get("requires_preprocessed", False)

    @property
    def param_hints(self) -> dict:
        """Method-keyed parameter tuning hints declared in SKILL.md."""
        self._ensure_basic()
        return self._basic.get("param_hints", {})

    @property
    def gotchas(self) -> list[str]:
        """Lead sentences of each `## Gotchas` bullet from SKILL.md body.

        Loaded lazily on first access and cached.  Empty list when SKILL.md
        is missing, has no `## Gotchas` section, or only contains the
        template placeholder.
        """
        if self._gotchas is not None:
            return self._gotchas
        skill_md = self.path / "SKILL.md"
        if not skill_md.exists():
            self._gotchas = []
            return self._gotchas
        content = skill_md.read_text(encoding="utf-8")
        # Strip frontmatter so `## ` matching only operates on the body.
        if content.startswith("---"):
            parts = content.split("---", 2)
            body = parts[2] if len(parts) >= 3 else content
        else:
            body = content
        self._gotchas = _extract_gotcha_leads(body)
        return self._gotchas

    def _load_full(self):
        skill_md = self.path / "SKILL.md"
        if not skill_md.exists():
            self._full = {}
            return

        content = skill_md.read_text(encoding="utf-8")
        if not content.startswith("---"):
            self._full = {}
            return

        parts = content.split("---", 2)
        if len(parts) < 3:
            self._full = {}
            return

        try:
            self._full = yaml.safe_load(parts[1])
        except yaml.YAMLError as exc:
            logger.warning(
                "Failed to parse SKILL.md frontmatter at %s: %s",
                skill_md,
                exc,
            )
            self._full = {}

    def get_full(self) -> dict:
        if self._full is None:
            self._load_full()
        return self._full
