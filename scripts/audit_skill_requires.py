#!/usr/bin/env python3
"""Audit (and optionally regenerate) per-skill `requires:` frontmatter.

The `requires:` list in each ``SKILL.md`` declares the Python-package surface of
a skill.  It drifts: skills gain optional backends (cellrank, palantir, scvelo,
...) and plotting deps (matplotlib, seaborn, scipy) that live transitively in the
``_lib`` analysis modules, while the hand-written frontmatter lags behind.

This tool recovers the TRUE surface by static analysis and reconciles it:

  * AST-parse the skill's script(s) and transitively follow the ``skills.<domain>
    ._lib.*`` modules they import.
  * Classify each third-party import as **core** (module-level, always loaded) or
    **optional** (gated inside a function and/or ``try/except``).
  * Resolve shared ``_lib/viz`` imports by **imported symbol** (not whole-package),
    so a skill is not charged for backends it never drives via the eager
    ``viz/__init__`` re-export.
  * Fold in backends dispatched via ``scanpy.external`` (e.g. Palantir) that are
    invisible to static imports, using the per-method ``param_hints`` filtered to
    names known to ``_lib/dependency_manager.py``'s ``DEPENDENCY_REGISTRY``.
  * Canonicalise module names to PyPI package names via the registry (authoritative
    for optional backends) plus a small hand map for common libs.

Modes:
    --json [PATH]   write the full per-skill report (default: stdout summary)
    --check         exit non-zero if any skill is MISSING a real dependency
                    (extras are reported as warnings only)
    --write         rewrite each ``SKILL.md`` frontmatter `requires:` in place

Usage:
    python scripts/audit_skill_requires.py                 # summary
    python scripts/audit_skill_requires.py --check         # CI gate
    python scripts/audit_skill_requires.py --write          # regenerate
    python scripts/audit_skill_requires.py --json report.json
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent
SKILLS = _ROOT / "skills"

# --------------------------------------------------------------------------- #
# Module -> PyPI distribution name for common libs NOT in any backend registry. #
# Backend names come from DEPENDENCY_REGISTRY (authoritative, loaded below).    #
# --------------------------------------------------------------------------- #
COMMON_MODULE_TO_PKG = {
    "mpl_toolkits": "matplotlib",
    "sklearn": "scikit-learn",
    "skmisc": "scikit-misc",
    "skimage": "scikit-image",
    "cv2": "opencv-python",
    "PIL": "Pillow",
    "yaml": "PyYAML",
    "igraph": "python-igraph",
    "bs4": "beautifulsoup4",
    "dotenv": "python-dotenv",
    "ot": "POT",
    "NaiveDE": "SpatialDE",
}

INTERNAL_PREFIXES = ("omicsclaw", "skills")
SKIP_MODULES = {"conftest", "pytest", "_pytest"}
# importlib.metadata is stdlib on py>=3.11; the backport is never a real runtime
# dep here (requires-python>=3.11).
STDLIB_EQUIV = {"importlib_metadata"}
LOCAL_MODULES = {"scripts", "generate_demo_data", "conftest"}
STDLIB = set(sys.stdlib_module_names) | {"__future__"}

# Stems of every `_lib/*.py` (and `_lib/viz/*.py`) module across domains. A bare
# `import <name>` that matches one of these is an internal module reference (e.g.
# `import cnv` in viz/cnv.py points at `_lib/cnv.py`), not a third-party package.
LIB_STEMS = {
    p.stem
    for dm in SKILLS.rglob("_lib")
    if dm.is_dir()
    for p in list(dm.glob("*.py")) + list(dm.glob("viz/*.py"))
}


# --------------------------------------------------------------------------- #
# DEPENDENCY_REGISTRY parsing (single source of truth for optional backends).   #
# --------------------------------------------------------------------------- #
def _canonical_from_install(install_cmd: str, key: str) -> str:
    """Prefer the real PyPI name from a simple `pip install <name>` cmd.

    Extras-based hints (`pip install -e ".[extra]"`) carry no package name, so
    fall back to the registry key (which is itself the canonical name there).
    """
    m = re.fullmatch(r"pip install ([A-Za-z0-9_][\w.\-]*)", install_cmd.strip())
    return m.group(1) if m else key


def load_registry():
    """Return (module_to_canonical, canonical_set) merged across domains."""
    mod_to_canon: dict[str, str] = {}
    canon: set[str] = set()
    # The install_cmd arg may be single- OR double-quoted (entries with `.[extra]`
    # use single quotes because they embed double quotes), so match the outer
    # quote with a backreference and capture lazily.
    pat = re.compile(
        r'"([^"]+)"\s*:\s*DependencyInfo\(\s*"([^"]+)"\s*,\s*'
        r"(?P<q>[\"'])(?P<install>.*?)(?P=q)",
        re.S,
    )
    for dm in SKILLS.rglob("_lib/dependency_manager.py"):
        for m in pat.finditer(dm.read_text()):
            key, module, install = m.group(1), m.group(2), m.group("install")
            canonical = _canonical_from_install(install, key)
            canon.add(canonical)
            mod_to_canon[module] = canonical
            mod_to_canon[key] = canonical  # key may itself be used as a token
    return mod_to_canon, canon


REGISTRY_MOD_TO_CANON, REGISTRY_CANON = load_registry()
# case-insensitive canonical lookup so `spatialde` -> `SpatialDE`
_CANON_CI = {c.lower(): c for c in REGISTRY_CANON}
for _m, _c in COMMON_MODULE_TO_PKG.items():
    _CANON_CI.setdefault(_c.lower(), _c)


def pkg_name(mod: str) -> str:
    """Canonical PyPI name for an import module or a declared token."""
    if mod in REGISTRY_MOD_TO_CANON:
        return REGISTRY_MOD_TO_CANON[mod]
    if mod in COMMON_MODULE_TO_PKG:
        return COMMON_MODULE_TO_PKG[mod]
    # canonical-casing dedup (e.g. a declared lowercase `spatialde`)
    return _CANON_CI.get(mod.lower(), mod)


def is_thirdparty(mod: str, local_stems: set[str]) -> bool:
    top = mod.split(".")[0]
    if top in STDLIB or top in SKIP_MODULES or top in STDLIB_EQUIV:
        return False
    if top in INTERNAL_PREFIXES or top in LOCAL_MODULES or top in local_stems:
        return False
    if top in LIB_STEMS:        # bare import of a sibling _lib module (e.g. `cnv`)
        return False
    if top.startswith("_"):
        return False
    return True


# --------------------------------------------------------------------------- #
# viz re-export maps: symbol -> defining submodule file, per domain.            #
# --------------------------------------------------------------------------- #
_VIZ_MAPS: dict[str, dict[str, Path]] = {}


def viz_symbol_map(domain: str) -> dict[str, Path]:
    if domain in _VIZ_MAPS:
        return _VIZ_MAPS[domain]
    mapping: dict[str, Path] = {}
    init = SKILLS / domain / "_lib" / "viz" / "__init__.py"
    if init.exists():
        tree = ast.parse(init.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
                sub = SKILLS / domain / "_lib" / "viz" / f"{node.module}.py"
                if sub.exists():
                    for alias in node.names:
                        mapping[alias.name] = sub
    _VIZ_MAPS[domain] = mapping
    return mapping


def _domain_of(path: Path) -> str | None:
    try:
        rel = path.relative_to(SKILLS)
    except ValueError:
        return None
    return rel.parts[0] if rel.parts else None


# --------------------------------------------------------------------------- #
# AST import collection.                                                        #
# --------------------------------------------------------------------------- #
class ImportCollector(ast.NodeVisitor):
    def __init__(self):
        self.thirdparty: list[tuple[str, bool]] = []          # (top_module, gated)
        self.lib_from: list[tuple[str, list[str], bool]] = []  # (module, names, gated)
        self.rel_from: list[tuple[int, str | None, bool]] = []
        self._depth = 0
        self._in_try = 0

    @property
    def _gated(self) -> bool:
        return self._depth > 0 or self._in_try > 0

    def visit_FunctionDef(self, node):
        self._depth += 1
        self.generic_visit(node)
        self._depth -= 1

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Try(self, node):
        self._in_try += 1
        self.generic_visit(node)
        self._in_try -= 1

    def visit_Import(self, node):
        for a in node.names:
            top = a.name.split(".")[0]
            if top not in INTERNAL_PREFIXES:
                self.thirdparty.append((top, self._gated))

    def visit_ImportFrom(self, node):
        if node.level and node.level > 0:
            self.rel_from.append((node.level, node.module, self._gated))
            return
        mod = node.module or ""
        if mod.startswith("skills.") and "._lib" in mod:
            self.lib_from.append((mod, [a.name for a in node.names], self._gated))
        elif mod.split(".")[0] not in INTERNAL_PREFIXES:
            self.thirdparty.append((mod.split(".")[0], self._gated))


def _resolve_lib(module: str, names: list[str]) -> list[Path]:
    """Resolve an internal `from skills.<d>._lib... import names` to file(s).

    For the shared viz PACKAGE, resolve by imported symbol so only the submodules
    actually used are followed (not the whole eager re-export).
    """
    parts = module.split(".")
    domain = parts[1] if len(parts) > 1 else None
    # exact viz package: `skills.<d>._lib.viz`
    if domain and parts[-1] == "viz" and parts[-2] == "_lib":
        vmap = viz_symbol_map(domain)
        out = {vmap[n] for n in names if n in vmap}
        # symbols defined directly in viz/__init__ (none today) or unknown -> ignore
        return list(out)
    rel = Path(*parts[1:])  # drop leading "skills"
    cand = (SKILLS / rel).with_suffix(".py")
    if cand.exists():
        return [cand]  # `module` is a .py file; `names` are members of it
    pkg_dir = SKILLS / rel
    pkg_init = pkg_dir / "__init__.py"
    if pkg_init.exists():
        # `module` is a PACKAGE; `from <pkg> import <name>` may pull in child
        # MODULES (e.g. `from skills.singlecell._lib import trajectory`). Resolve
        # each name to its submodule file; also follow __init__ (it runs on
        # import and may re-export symbols from sibling modules).
        out = [pkg_init]
        for n in names:
            sub = pkg_dir / f"{n}.py"
            sub_pkg = pkg_dir / n / "__init__.py"
            if sub.exists():
                out.append(sub)
            elif sub_pkg.exists():
                out.append(sub_pkg)
        return out
    return []


def analyze_file(path: Path):
    try:
        tree = ast.parse(path.read_text(), filename=str(path))
    except Exception as exc:  # pragma: no cover - defensive
        return [], [], [f"parse-error {path}: {exc}"]
    c = ImportCollector()
    c.visit(tree)
    notes: list[str] = []
    lib_files: list[tuple[Path, bool]] = []
    for module, names, gated in c.lib_from:
        for f in _resolve_lib(module, names):
            lib_files.append((f, gated))
    for level, mod, gated in c.rel_from:
        base = path.parent
        for _ in range(level - 1):
            base = base.parent
        if mod:
            # a relative viz package import inside viz/__init__ is handled by the
            # package map; here resolve concrete sibling modules
            cand = (base / Path(*mod.split("."))).with_suffix(".py")
            if cand.exists():
                lib_files.append((cand, gated))
    return c.thirdparty, lib_files, notes


def skill_import_surface(skill_dir: Path, script_names: list[str]):
    """BFS the skill's scripts + transitively-used _lib modules.

    Returns (core_pkgs, optional_pkgs) as sorted lists of canonical names.
    """
    seen: set[Path] = set()
    queue: list[Path] = []
    for s in script_names:
        p = skill_dir / s
        if p.exists():
            queue.append(p)
    for p in skill_dir.glob("*.py"):
        if p not in queue:
            queue.append(p)
    local_stems = {p.stem for p in skill_dir.glob("*.py")}
    core: set[str] = set()
    optional: set[str] = set()
    notes: list[str] = []
    lib_used: set[str] = set()
    while queue:
        f = queue.pop()
        if f in seen:
            continue
        seen.add(f)
        thirdparty, lib_files, fnotes = analyze_file(f)
        notes += fnotes
        for top, gated in thirdparty:
            if not is_thirdparty(top, local_stems):
                continue
            (optional if gated else core).add(pkg_name(top))
        for lf, _gated in lib_files:
            try:
                lib_used.add(str(lf.relative_to(SKILLS)))
            except ValueError:
                pass
            if lf not in seen:
                queue.append(lf)
    optional -= core  # ever-core wins
    return sorted(core), sorted(optional), sorted(lib_used), notes


# --------------------------------------------------------------------------- #
# Per-skill report.                                                             #
# --------------------------------------------------------------------------- #
def parse_frontmatter(skill_md: Path) -> dict:
    if not skill_md.exists():
        return {}
    txt = skill_md.read_text()
    parts = txt.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        return yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}


def _skill_yaml_raw(skill_dir: Path) -> dict | None:
    """Raw parse of a v2 ``skill.yaml`` (ADR 0037), or None if absent/unreadable.

    Read raw (not via the pydantic schema) so the audit stays robust on a
    malformed skill.yaml — schema validity is enforced separately by
    ``scripts/validate_skill_yaml.py`` / ``skill_lint`` ``_lint_v2``.
    """
    sy = skill_dir / "skill.yaml"
    if not sy.exists():
        return None
    try:
        data = yaml.safe_load(sy.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def param_hint_backends(skill_dir: Path) -> set[str]:
    """Registry-known backend tokens from param hints (captures scanpy.external
    backends like palantir that never appear as a static import).

    Dual-track (ADR 0037): v2 hints live at
    ``skill.yaml.interface.parameters.hints``; v1 at ``parameters.yaml.param_hints``.
    """
    sy = _skill_yaml_raw(skill_dir)
    if sy is not None:
        hints = (((sy.get("interface") or {}).get("parameters") or {}).get("hints")) or {}
    else:
        pyaml = skill_dir / "parameters.yaml"
        if not pyaml.exists():
            return set()
        try:
            data = yaml.safe_load(pyaml.read_text()) or {}
        except yaml.YAMLError:
            return set()
        hints = data.get("param_hints") or {}
    out: set[str] = set()
    for info in hints.values():
        for tok in (info or {}).get("requires", []) or []:
            if isinstance(tok, str):
                canon = pkg_name(tok)
                if canon in REGISTRY_CANON:
                    out.add(canon)
    return out


def skill_script_names(skill_dir: Path) -> list[str]:
    # v2: runtime.entry; v1: parameters.yaml script.
    sy = _skill_yaml_raw(skill_dir)
    if sy is not None:
        entry = (sy.get("runtime") or {}).get("entry")
        return [entry] if entry else []
    pyaml = skill_dir / "parameters.yaml"
    if pyaml.exists():
        try:
            data = yaml.safe_load(pyaml.read_text()) or {}
            if data.get("script"):
                return [data["script"]]
        except yaml.YAMLError:
            pass
    return []


def audit_skill(sd: Path) -> dict:
    """Per-skill dependency audit (used by build_report and by skill_lint).

    Dual-track (ADR 0037): the declared Python surface is read from
    ``skill.yaml deps.python`` (v2) or ``SKILL.md requires:`` (v1). The static
    import analysis is unchanged. (deps.r is out of scope here — R dependency
    semantics are handled by the R subsystem.)
    """
    sd = Path(sd).resolve()  # callers may pass a relative skill dir
    # Contract is decided by the marker FILE's presence, not by whether it parses
    # (Codex cross-validation): a malformed v2 skill.yaml must STILL be treated as
    # v2 so --write routes to the v2 (no-op-on-invalid) writer instead of trying
    # to rewrite a (possibly absent) SKILL.md.
    has_v2 = (sd / "skill.yaml").exists()
    sy = _skill_yaml_raw(sd)
    contract = "v2" if has_v2 else "v1"
    core, optional, lib_used, notes = skill_import_surface(sd, skill_script_names(sd))
    b_backends = param_hint_backends(sd)
    recommended = set(core) | set(optional) | b_backends
    if "scanpy" in recommended:
        recommended.add("anndata")  # scanpy hard-depends on anndata
    if has_v2:
        raw_declared = ((sy or {}).get("deps") or {}).get("python") or []
    else:
        raw_declared = parse_frontmatter(sd / "SKILL.md").get("requires") or []
    declared = [str(r).strip() for r in raw_declared if str(r).strip()]
    declared_norm = {pkg_name(x) for x in declared}
    missing = sorted(recommended - declared_norm, key=str.lower)
    extra = sorted(declared_norm - recommended, key=str.lower)
    # `--write` is UNION-only: add what static analysis proves is needed, but
    # never drop a declared dep. Skills that delegate to `omicsclaw.*` runtime
    # (consensus, orchestrator) hide their real surface behind the package
    # boundary, so a declared extra is usually an analyzer blind spot, not a
    # stale entry. Extras are surfaced as warnings for manual pruning only.
    final = sorted(recommended | declared_norm, key=str.lower)
    try:
        skill_identity = sd.relative_to(SKILLS)
    except ValueError:
        # Lint/tests may audit a staged or quarantined skill outside the
        # repository's canonical SKILLS root. Dependency analysis is still
        # valid there; only the human-readable report identity differs.
        skill_identity = sd
    return {
        "skill": str(skill_identity),
        "contract": contract,
        "declared": declared,
        "recommended": sorted(recommended, key=str.lower),
        "final": final,
        "core": core,
        "optional": sorted(set(optional) | b_backends, key=str.lower),
        "missing": missing,
        "extra": extra,
        "lib_modules": lib_used,
        "notes": notes,
    }


def build_report() -> list[dict]:
    # Discover v1 (SKILL.md) and v2-only (skill.yaml) skill dirs alike.
    dirs = {p.parent for p in SKILLS.rglob("SKILL.md")}
    dirs |= {p.parent for p in SKILLS.rglob("skill.yaml")}
    return [audit_skill(d) for d in sorted(dirs)]


# --------------------------------------------------------------------------- #
# --write: rewrite ONLY the frontmatter `requires:` block, preserving the rest. #
# --------------------------------------------------------------------------- #
_REQUIRES_BLOCK = re.compile(
    r"(?ms)^requires:[ \t]*\n(?:[ \t]*-[ \t]*.*\n?)*"
)


def write_requires(skill_md: Path, packages: list[str]) -> bool:
    txt = skill_md.read_text()
    parts = txt.split("---", 2)
    if len(parts) < 3:
        return False
    fm = parts[1]
    if packages:
        block = "requires:\n" + "".join(f"- {p}\n" for p in packages)
    else:  # valid empty YAML list, never a bare `requires:` (parses as null)
        block = "requires: []\n"
    if _REQUIRES_BLOCK.search(fm):
        new_fm = _REQUIRES_BLOCK.sub(block, fm, count=1)
    else:  # insert before the closing of frontmatter
        new_fm = fm.rstrip("\n") + "\n" + block
    if new_fm == fm:
        return False
    skill_md.write_text(parts[0] + "---" + new_fm + "---" + parts[2])
    return True


def write_deps_python_v2(skill_dir: Path, packages: list[str]) -> bool:
    """Rewrite ``deps.python`` in a v2 skill.yaml via the schema (canonical re-dump).

    UNION-only like the v1 path: ``packages`` is the report's ``final`` list.
    No-op (returns False) when unchanged or the skill.yaml is invalid/unloadable.

    NOTE: this is a canonical re-dump, NOT a lossless text editor — it normalises
    field order and drops comments/anchors. That is fine for generated skill.yaml
    (the migrate scaffold already emits canonical YAML); hand-formatted files lose
    cosmetic formatting.
    """
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    try:
        from omicsclaw.skill.schema import load_skill_yaml, parse_skill_manifest
    except Exception:
        return False
    sy = skill_dir / "skill.yaml"
    try:
        manifest = load_skill_yaml(sy)
    except Exception:
        return False
    if list(manifest.deps.python) == list(packages):
        return False
    # Rebuild THROUGH the schema so the write is re-validated (deps cleaning,
    # field constraints) rather than mutated in place (validate_assignment is off).
    data = manifest.model_dump()
    data["deps"]["python"] = list(packages)
    try:
        rebuilt = parse_skill_manifest(data)
    except Exception:
        return False
    sy.write_text(rebuilt.to_yaml(), encoding="utf-8")
    return True


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", nargs="?", const="-", metavar="PATH",
                    help="write full JSON report (default stdout)")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if any skill is missing a real dependency")
    ap.add_argument("--write", action="store_true",
                    help="regenerate the declared dependency surface in place "
                         "(v2 skill.yaml deps.python / v1 SKILL.md requires)")
    args = ap.parse_args()

    report = build_report()

    if args.json is not None:
        blob = json.dumps(report, indent=2)
        if args.json == "-":
            print(blob)
        else:
            Path(args.json).write_text(blob)
            print(f"wrote {args.json} ({len(report)} skills)")
        return 0

    if args.write:
        changed = 0
        for r in report:
            sdir = SKILLS / r["skill"]
            if r["contract"] == "v2":
                ok = write_deps_python_v2(sdir, r["final"])
            else:
                ok = write_requires(sdir / "SKILL.md", r["final"])
            if ok:
                changed += 1
                print(f"updated {r['skill']} ({r['contract']}): +{r['missing']}")
        print(f"\n{changed}/{len(report)} skill(s) updated.")
        return 0

    miss = [r for r in report if r["missing"]]
    extra = [r for r in report if r["extra"]]
    for r in miss:
        print(f"MISSING  {r['skill']}: +{r['missing']}")
    for r in extra:
        print(f"warn:EXTRA {r['skill']}: -{r['extra']}")
    print(f"\n{len(report)} skills | {len(miss)} missing deps | {len(extra)} with extras")

    if args.check and miss:
        print("\nFAIL: skills above are missing real dependencies. "
              "Run `python scripts/audit_skill_requires.py --write`.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
