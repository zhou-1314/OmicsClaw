#!/usr/bin/env python3
"""Migrate a v1 skill (SKILL.md frontmatter + parameters.yaml) to a v2 ``skill.yaml``.

ADR 0037. This is a SCAFFOLD: it does the mechanical, lossless-where-possible
mapping and flags every field that needs human attention (TODO / heuristic /
dropped). It never deletes v1 files — v1/v2 coexist (schema_version rule), so a
generated skill.yaml can be reviewed alongside the originals.

Usage:
  # Pilot a whole domain into a staging tree (non-destructive, reviewable):
  python scripts/migrate_to_skill_yaml.py --domain spatial

  # Write skill.yaml into the real skill dirs (the actual migration step):
  python scripts/migrate_to_skill_yaml.py --domain spatial --in-place

  # Validate already-generated skill.yaml files:
  python scripts/migrate_to_skill_yaml.py --domain spatial --validate-only --out-dir build/skillyaml_pilot
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from omicsclaw.skill.schema import (  # noqa: E402
    SkillManifest,
    parse_skill_manifest,
    validate_skill_yaml,
)
from omicsclaw.skill import interface_extract as ie  # noqa: E402
from omicsclaw.skill.skill_md import IO_MARKER  # noqa: E402

SKILLS_ROOT = REPO_ROOT / "skills"

_KNOWN_SKILLS_CACHE: set[str] | None = None


def known_skill_names() -> set[str]:
    """All real skill directory names (used to gate skip_when redirect targets)."""
    global _KNOWN_SKILLS_CACHE
    if _KNOWN_SKILLS_CACHE is None:
        _KNOWN_SKILLS_CACHE = {
            p.parent.name for p in SKILLS_ROOT.rglob("SKILL.md")
        }
    return _KNOWN_SKILLS_CACHE

# Runtime fields that v1 reads from parameters.yaml, falling back to the legacy
# `metadata.omicsclaw` frontmatter block (mirrors lazy_metadata._load_basic).
_LEGACY_RUNTIME_FIELDS = (
    "domain", "script", "type", "validation_level", "trigger_keywords",
    "allowed_extra_flags", "legacy_aliases", "saves_h5ad", "requires_preprocessed",
    "param_hints",
)

_INTERPRETER_BINS = {"python", "python3", "bash", "sh", "rscript"}

# Fields with no faithful v1 source — emitted empty and flagged for human fill.
# (modalities/file_types/data_shape.obsm/outputs.files/anndata are now
# auto-extracted by omicsclaw.skill.interface_extract — see build_manifest_dict.)
_TODO_NOTE = {
    "interface.outputs.result_json.required_keys": "no reliable v1 source; forward field — curate from script result.json writes if a consumer needs it",
    "compatibility.architectures": "no v1 source; default empty",
    "security": "defaulted to none/none/output_dir_only — VERIFY per skill",
}


def parse_frontmatter(skill_md: Path) -> dict:
    if not skill_md.exists():
        return {}
    content = skill_md.read_text(encoding="utf-8")
    if not content.startswith("---"):
        return {}
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        return yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}


def is_already_v2(skill_dir: Path) -> bool:
    """True once a skill is fully v2: it has a skill.yaml AND its SKILL.md has
    been regenerated (carries the generated I/O marker).

    The migrator is a ONE-TIME v1→v2 extraction: after ``generate_skill_md.py``
    replaces the hand-written ``## Inputs & Outputs`` table with a generated
    summary, the v1 facts (description prose + I/O table) are gone, so
    re-deriving them would corrupt the contract. skill.yaml is the SSOT now —
    edit it directly, then regenerate. So an in-place re-run is a no-op here.
    """
    if not (skill_dir / "skill.yaml").exists():
        return False
    return IO_MARKER in _skill_md_body(skill_dir / "SKILL.md")


def _skill_md_body(skill_md: Path) -> str:
    """Return the SKILL.md body (everything after the frontmatter), or ''."""
    if not skill_md.exists():
        return ""
    content = skill_md.read_text(encoding="utf-8")
    if not content.startswith("---"):
        return content
    parts = content.split("---", 2)
    return parts[2] if len(parts) >= 3 else content


def load_parameters(skill_dir: Path) -> dict:
    sidecar = skill_dir / "parameters.yaml"
    if not sidecar.exists():
        return {}
    try:
        data = yaml.safe_load(sidecar.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


_SKIP_SPLIT = re.compile(r"(?i)\bskip\s+(?:when|if|for)\b")
_LOAD_PREFIX = re.compile(r"(?i)^\s*load\s+when\b[:,]?\s*")
# A redirect inside a parenthetical names a sibling skill to run/use instead.
# Verbs (longest first so "go straight to" wins over "go to"); the FIRST skill
# token after the verb is the redirect target (e.g. "use a / b" -> a).
_REDIRECT_RE = re.compile(
    r"\b(?:use|go\s+straight\s+to|go\s+to|run|see)\s+`?(?P<skill>[a-z][a-z0-9-]*)`?",
    re.IGNORECASE,
)
# Split skip clauses on a top-level (paren-depth-0) separator: ' or [for|when] ',
# '; ', or a comma introducing the next clause (', when …' / ', for …'). The
# comma form is common in enumerated Skip-when prose ("… (use X), when Y (use Z),
# or when W …") — without it the middle clause merges in and its redirect is lost.
_CLAUSE_SEP = re.compile(r"(?i)\s+or\s+(?:for\s+|when\s+)?|\s*;\s+|,\s+(?:when|for)\s+")


def _split_top_level(skip_part: str) -> list[str]:
    """Split into clauses on ' or '/'; ' at paren depth 0 (so an ' or ' inside a
    redirect like '(run a or b first)' does not fabricate a spurious clause)."""
    clauses: list[str] = []
    depth = 0
    start = 0
    i = 0
    while i < len(skip_part):
        c = skip_part[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            m = _CLAUSE_SEP.match(skip_part, i)
            if m:
                clauses.append(skip_part[start:i])
                i = m.end()
                start = i
                continue
        i += 1
    clauses.append(skip_part[start:])
    return [c for c in (s.strip() for s in clauses) if c]


def _parse_clause(clause: str, known_skills: set[str] | None = None) -> dict:
    """Return ``{condition, use?}`` from one skip clause, keeping parens balanced.

    A parenthetical that contains a redirect verb yields ``use`` (first skill
    token) and the redirect phrase is stripped from the condition; the rest of
    that parenthetical (e.g. "no integration needed") is preserved. A
    parenthetical with no redirect (e.g. "(fix it first)") stays in the condition.

    ``known_skills`` (when provided) gates the redirect: the token after the verb
    is only accepted as ``use`` if it names a REAL skill. This rejects prose the
    regex would otherwise mis-grab — an article ("run **a** phaser first"), a
    determiner ("use **the** relevant downstream skill"), or a bare tool name
    ("use **MACS**", "use **Manta**"). A rejected parenthetical is kept verbatim in
    the condition so the actionable note survives. When ``known_skills`` is None
    (e.g. a direct unit-test call), no gating is applied (legacy behaviour).
    """
    use: str | None = None
    out: list[str] = []
    i = 0
    while i < len(clause):
        if clause[i] == "(":
            j = clause.find(")", i)
            if j == -1:
                out.append(clause[i:])
                break
            inner = clause[i + 1: j]
            rm = _REDIRECT_RE.search(inner)
            tok = rm.group("skill") if rm else None
            valid = rm is not None and (known_skills is None or tok in known_skills)
            if valid:
                if use is None:
                    use = tok
                kept = re.sub(r"[\s—–\-,;]+$", "", inner[: rm.start()])
                if kept:
                    out.append("(" + kept + ")")
                # else: drop the now-empty redirect-only parenthetical entirely
            else:
                # no redirect, or the target is not a real skill (article / tool /
                # prose) — keep the parenthetical verbatim in the condition.
                out.append(clause[i: j + 1])
            i = j + 1
        else:
            out.append(clause[i])
            i += 1
    condition = re.sub(r"\s+", " ", "".join(out)).strip().rstrip(".,; ").strip()
    rule: dict = {"condition": condition or clause.strip()}
    if use:
        rule["use"] = use
    return rule


def split_description(
    desc: str, known_skills: set[str] | None = None
) -> tuple[str, list[dict], list[str]]:
    """Best-effort split of 'Load when X. Skip when Y (use a) or for Z (use b).'

    Returns (load_when, skip_rules, warnings). Skip parsing is heuristic.
    ``known_skills`` gates redirect targets to real skill names (see
    ``_parse_clause``); callers should pass ``known_skill_names()``.
    """
    warnings: list[str] = []
    desc = (desc or "").strip()
    if not re.match(r"(?i)^\s*load\s+when\b", desc):
        warnings.append("description does not start with 'Load when' — non-canonical, review load_when")
    m = _SKIP_SPLIT.search(desc)
    load_part = desc[: m.start()] if m else desc
    skip_part = desc[m.end():] if m else ""

    load_when = _LOAD_PREFIX.sub("", load_part).strip().rstrip(".").strip()
    if not load_when:
        load_when = load_part.strip() or "TODO: describe when to load"
        warnings.append("load_when: empty after parsing — review")

    skip_rules: list[dict] = []
    if skip_part:
        for clause in _split_top_level(skip_part.strip().rstrip(".")):
            rule = _parse_clause(clause, known_skills)
            skip_rules.append(rule)
        warnings.append("skip_when: heuristic parse from prose — review conditions/rationale")
    return load_when, skip_rules, warnings


def detect_language(script: str) -> str:
    s = (script or "").lower()
    if s.endswith(".r"):
        return "r"
    if s.endswith((".sh", ".bash")):
        return "bash"
    return "python"


def discover_resources(skill_dir: Path, homepage: str | None) -> dict:
    res: dict = {}
    refs_dir = skill_dir / "references"
    if refs_dir.is_dir():
        refs = sorted(p.name for p in refs_dir.glob("*.md"))
        if refs:
            res["references"] = refs
    if (skill_dir / "r_visualization").is_dir():
        res["figures"] = "r_visualization/"
    if (skill_dir / "tests").is_dir():
        res["tests"] = "tests/"
    if homepage:
        res["homepage"] = homepage
    return res


def build_manifest_dict(skill_dir: Path, domain_hint: str) -> tuple[dict, list[str], list[str]]:
    """Return (manifest_dict, warnings, dropped). May still fail schema validation."""
    fm = parse_frontmatter(skill_dir / "SKILL.md")
    pm = load_parameters(skill_dir)
    legacy = (fm.get("metadata") or {}).get("omicsclaw") or {}
    warnings: list[str] = []
    dropped: list[str] = []

    def rt(key, default):
        """parameters.yaml → legacy metadata.omicsclaw → default (v1 semantics)."""
        v = pm.get(key)
        if v is None:
            v = legacy.get(key)
        return default if v is None else v

    if legacy and not pm:
        warnings.append("v1 used legacy metadata.omicsclaw (no parameters.yaml) — runtime fields read from it")

    name = fm.get("name") or skill_dir.name
    domain = rt("domain", "") or domain_hint
    load_when, skip_rules, w = split_description(
        fm.get("description", ""), known_skill_names()
    )
    warnings += w

    fm_requires = fm.get("requires")
    python_deps = (
        [str(p).strip() for p in fm_requires if str(p).strip()]
        if isinstance(fm_requires, list)
        else []
    )

    pm_requires = pm.get("requires") if isinstance(pm.get("requires"), dict) else {}
    env = pm_requires.get("env") or []
    config = pm_requires.get("config") or []
    bins = pm_requires.get("bins") or []

    # requires.bins handling: never silently drop real binaries (Codex must-fix).
    cli_bins: list[str] = []
    decorative = [b for b in bins if str(b).lower() in {"python3", "python"}]
    real_bins = [b for b in bins if str(b).lower() not in {"python3", "python"}]
    interp_bins = [b for b in real_bins if Path(str(b)).name.lower().removesuffix(".exe") in _INTERPRETER_BINS]
    cli_bins = [b for b in real_bins if b not in interp_bins]
    if decorative and not real_bins:
        dropped.append("requires.bins (only [python3] — decorative)")
    if cli_bins:
        warnings.append(
            f"requires.bins real binaries {cli_bins} → mapped to deps.cli; "
            "needs a PATH-check consumer before relied upon"
        )
    if interp_bins:
        warnings.append(
            f"requires.bins interpreters {interp_bins} suggest R/shell usage — "
            "handle via runtime.language / deps.r, NOT deps.cli (left out)"
        )

    # install: is decorative (deps.python + pyproject pins are SSOT) — but report
    # any install package missing from deps.python so a real omission is caught.
    install = pm.get("install") or []
    install_pkgs = [e.get("package") for e in install if isinstance(e, dict) and e.get("package")]
    if install:
        missing = sorted(set(install_pkgs) - set(python_deps))
        if missing:
            warnings.append(
                f"install: lists {missing} not in deps.python — VERIFY (deps.python from "
                "frontmatter requires is the SSOT; could be decorative or a real omission)"
            )
        dropped.append("install: (decorative; superseded by deps.python + pyproject pins)")

    emoji = pm.get("emoji") or legacy.get("emoji") or fm.get("emoji")
    homepage = pm.get("homepage") or legacy.get("homepage") or fm.get("homepage")

    saves_h5ad = bool(rt("saves_h5ad", False))
    script = rt("script", "")
    language = detect_language(script)
    if script and not (skill_dir / script).exists():
        warnings.append(f"runtime.entry {script!r} not found in {skill_dir.name}/ — verify")
    if language in ("r", "bash") and python_deps:
        warnings.append(
            f"runtime.language={language} but deps.python={python_deps} — likely mis-mapped "
            "R/CLI deps (frontmatter requires is Python-only); needs a per-language strategy"
        )

    # interface.inputs/outputs: recover what is RELIABLY extractable from the v1
    # sources (ADR 0037) instead of leaving the contract stranded in SKILL.md.
    # No reliable v1 source for modalities / result_json.required_keys → left for
    # human/Codex curation (the migrator warns below). The migrator is a ONE-TIME
    # v1→v2 extraction (see is_already_v2 / main): it never runs on an already
    # regenerated SKILL.md, so the table-derived facts below are always present.
    skill_md_body = _skill_md_body(skill_dir / "SKILL.md")
    output_contract = ""
    oc_path = skill_dir / "references" / "output_contract.md"
    if oc_path.exists():
        output_contract = oc_path.read_text(encoding="utf-8")

    in_file_types = ie.extract_input_file_types(skill_md_body)
    in_obsm = ie.extract_input_anndata_obsm(skill_md_body)
    out_files = ie.extract_output_files(output_contract) if output_contract else []
    out_anndata = ie.extract_anndata_keys(skill_md_body)

    data_shape: dict = {"requires_preprocessed": bool(rt("requires_preprocessed", False))}
    if in_obsm:
        data_shape["obsm"] = in_obsm

    interface: dict = {
        "inputs": {
            "modalities": ie.extract_modalities(list(fm.get("tags") or [])),
            "file_types": in_file_types,
            "preconditions": {
                "data_shape": data_shape,
                "env": list(env),
                "config": list(config),
            },
        },
        "parameters": {
            "allowed_extra_flags": list(rt("allowed_extra_flags", []) or []),
            "hints": rt("param_hints", {}) or {},
        },
        "outputs": {
            "files": out_files,
            "result_json": {"required_keys": []},
        },
    }
    if saves_h5ad:
        interface["outputs"]["anndata"] = {
            "saves_h5ad": True,
            "obs": out_anndata["obs"],
            "obsm": out_anndata["obsm"],
            "var": out_anndata["var"],
            "layers": out_anndata["layers"],
            "uns": out_anndata["uns"],
        }
    if not out_files:
        warnings.append("interface.outputs.files empty — no references/output_contract.md to extract from")

    deps: dict = {"python": python_deps}
    if cli_bins:
        deps["cli"] = cli_bins

    data: dict = {
        "schema_version": 2,
        "id": name,
        "name": name,
        "domain": domain,
        "type": rt("type", "leaf") or "leaf",
        "version": str(fm.get("version") or "0.0.0"),
        "author": fm.get("author"),
        "license": fm.get("license"),
        "emoji": emoji,
        "summary": {
            "load_when": load_when,
            "skip_when": skip_rules,
            "trigger_keywords": list(rt("trigger_keywords", []) or []),
            "tags": list(fm.get("tags") or []),
            "aliases": list(rt("legacy_aliases", []) or []),
        },
        "interface": interface,
        "runtime": {"language": language, "entry": script},
        "deps": deps,
        "compatibility": {"platforms": list(pm.get("os") or legacy.get("os") or []), "architectures": []},
        "resources": discover_resources(skill_dir, homepage),
        "lifecycle": {"status": "mvp"},
        "validation": {"level": rt("validation_level", "smoke-only") or "smoke-only"},
        "provenance": {"origin": "human"},
        "security": {"data_egress": "none", "network": "none", "writes": "output_dir_only"},
        "mcp": {"expose": False},
    }
    return data, warnings, dropped


def iter_skill_dirs(domain: str) -> list[Path]:
    base = SKILLS_ROOT / domain
    if not base.is_dir():
        return []
    return sorted(p.parent for p in base.rglob("SKILL.md"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Migrate v1 skills to v2 skill.yaml (ADR 0037)")
    ap.add_argument("--domain", help="domain to migrate (e.g. spatial)")
    ap.add_argument("--skill", help="single skill dir to migrate")
    ap.add_argument("--out-dir", default="build/skillyaml_pilot", help="staging output root")
    ap.add_argument("--in-place", action="store_true", help="write skill.yaml into the real skill dir")
    ap.add_argument("--validate-only", action="store_true", help="validate existing skill.yaml, no write")
    args = ap.parse_args()

    if args.skill:
        skill_dirs = [Path(args.skill).resolve()]
        domain = args.skill.rstrip("/").split("/")[-2] if "/" in args.skill else ""
    elif args.domain:
        skill_dirs = iter_skill_dirs(args.domain)
        domain = args.domain
    else:
        ap.error("provide --domain or --skill")
        return 2

    if not skill_dirs:
        print(f"no skills found for {args.domain or args.skill}", file=sys.stderr)
        return 1

    out_root = (REPO_ROOT / args.out_dir).resolve()
    mode = "validation" if args.validate_only else "migration"
    report: list[str] = [f"# skill.yaml {mode} report — {args.domain or args.skill}", ""]
    n_ok = n_fail = 0

    for sd in skill_dirs:
        rel = sd.relative_to(SKILLS_ROOT) if SKILLS_ROOT in sd.parents else Path(sd.name)
        target = (sd if args.in_place else out_root / rel) / "skill.yaml"

        if args.validate_only:
            if not target.exists():
                report.append(f"- ❓ {rel}: no skill.yaml at {target}")
                n_fail += 1
                continue
            errs = validate_skill_yaml(target)
            if errs:
                n_fail += 1
                report.append(f"- ❌ {rel}: " + "; ".join(errs))
            else:
                n_ok += 1
                report.append(f"- ✅ {rel}: valid")
            continue

        # One-time extraction: never re-derive a fully-v2 skill from its (now
        # generated) SKILL.md — skill.yaml is the SSOT. Prevents a re-run from
        # corrupting the contract once generate_skill_md has replaced the table.
        if args.in_place and is_already_v2(sd):
            n_ok += 1
            report.append(f"- ⏭️  {rel}: already v2 (regenerated SKILL.md) — skill.yaml left untouched")
            continue

        data, warnings, dropped = build_manifest_dict(sd, domain)
        try:
            manifest: SkillManifest = parse_skill_manifest(data)
        except Exception as exc:  # pydantic ValidationError or mapping error
            n_fail += 1
            report.append(f"- ❌ {rel}: schema validation FAILED: {exc}")
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(manifest.to_yaml(), encoding="utf-8")
        n_ok += 1
        report.append(f"- ✅ {rel} → {target.relative_to(REPO_ROOT)}")
        for w in warnings:
            report.append(f"    - ⚠️  {w}")
        for d in dropped:
            report.append(f"    - 🗑️  dropped {d}")

    report += [
        "",
        f"## Summary: {n_ok} ok, {n_fail} failed of {len(skill_dirs)} skills",
    ]
    if not args.validate_only:
        report += ["", "### Fields with no v1 source (emitted empty, need human fill)"]
        for k, why in _TODO_NOTE.items():
            report.append(f"- `{k}` — {why}")

    # Separate report files so validate-only never clobbers the migration report.
    report_path = out_root / f"REPORT-{mode}-{args.domain or 'skill'}.md"
    if not args.in_place:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("\n".join(report) + "\n", encoding="utf-8")
    print("\n".join(report))
    if not args.in_place:
        print(f"\nReport: {report_path.relative_to(REPO_ROOT)}")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
