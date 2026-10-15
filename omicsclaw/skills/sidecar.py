"""Claude Science skill sidecar gate, signature extraction, bootstrap generation, and skill validator.

Pure standard library implementation with zero dynamic importlib calls so
:mod:`omicsclaw.skills` remains a strict leaf layer.
"""

from __future__ import annotations

import ast
import builtins
from collections.abc import Sequence
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import types

__all__ = [
    "DEFAULT_SKILLS_ROOT",
    "REQUIRED_HEADINGS",
    "aggregate_benchmark",
    "build_sidecar_bootstrap_code",
    "extract_kernel_exports",
    "load_skill",
    "render_kernel_plugin_block",
    "scaffold_skill",
    "validate_kernel_ast",
    "validate_kernel_isolated",
    "validate_skill_dir",
]

DEFAULT_SKILLS_ROOT = Path(__file__).resolve().parents[2] / "skills"

REQUIRED_HEADINGS = (
    "## 1. Method Decision Matrix & When to Use",
    "## 2. How to Run (Idiomatic Upstream Patterns)",
    "## 3. Output Format & Verification Contract",
    "## 4. Gotchas & Troubleshooting",
    "## Dependencies",
)

_BUILTINS = set(dir(builtins)) | {"help", "exit", "quit", "copyright", "credits", "license"}
_DEP_LINE_RE = re.compile(r"^\s*`[A-Za-z0-9._-]+`(\s*,\s*`[A-Za-z0-9._-]+`)*\s*$")


def _is_literal(node: ast.AST | None) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_is_literal(elt) for elt in node.elts)
    if isinstance(node, ast.Dict):
        return all(k is not None and _is_literal(k) and _is_literal(v) for k, v in zip(node.keys, node.values))
    return isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)) and _is_literal(node.operand)


def validate_kernel_ast(src: str) -> list[str]:
    """Validate ``kernel.py`` source against Claude Science's ``sidecar_gate`` AST rules."""
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return [f"line {exc.lineno}: syntax error: {exc.msg}"]

    errors: list[str] = []
    names: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        annotations = []
        if isinstance(node, (ast.arg, ast.AnnAssign)):
            annotations.append(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            annotations.append(node.returns)
        for annotation in annotations:
            if annotation is not None and any(
                isinstance(part, (ast.Call, ast.Lambda, ast.NamedExpr, ast.ListComp,
                                  ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.Await))
                for part in ast.walk(annotation)
            ):
                errors.append(f"line {annotation.lineno}: executable annotation is not allowed")
    declared_all: list[str] | None = None
    defined_funcs: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}

    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            errors.append(
                f"line {node.lineno}: top-level import not allowed in kernel.py — "
                "defer imports inside function bodies"
            )
            continue
        if isinstance(node, ast.Pass):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.decorator_list:
                errors.append(f"line {node.lineno}: decorators on {node.name} are not allowed")
            for default in list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]:
                if not _is_literal(default):
                    errors.append(
                        f"line {default.lineno}: non-literal default in {node.name}() — "
                        "use a literal or None and resolve inside the body"
                    )
            names.append((node.lineno, node.name))
            defined_funcs[node.name] = node
            continue
        if isinstance(node, ast.Assign) and _is_literal(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.append((node.lineno, target.id))
                    if target.id == "__all__":
                        try:
                            value = ast.literal_eval(node.value)
                        except (ValueError, TypeError, SyntaxError):
                            value = None
                        if (isinstance(value, (list, tuple)) and value
                            and all(isinstance(item, str) for item in value)
                            and len(value) == len(set(value))):
                            declared_all = list(value)
                        else:
                            errors.append("__all__ must contain unique function names")
                else:
                    errors.append(f"line {node.lineno}: only simple NAME = LITERAL assignments at top level")
            continue
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and (
            node.value is None or _is_literal(node.value)
        ):
            names.append((node.lineno, node.target.id))
            continue
        errors.append(f"line {node.lineno}: top-level {type(node).__name__} not allowed (definitions only)")

    for lineno, name in names:
        if name != "__all__" and name.startswith("_"):
            errors.append(
                f"line {lineno}: {name!r} starts with '_' and is forbidden at top level — "
                "move inside the function body or rename"
            )
        if name in _BUILTINS:
            errors.append(f"line {lineno}: {name!r} shadows a Python builtin — rename")

    if not declared_all:
        errors.append("kernel.py must declare a non-empty __all__ list")
    else:
        for exported in declared_all:
            fn = defined_funcs.get(exported)
            if fn is None:
                errors.append(f"{exported!r} is listed in __all__ but is not a top-level function")
            elif not ast.get_docstring(fn):
                errors.append(f"function {exported!r} in __all__ has no docstring")
    return errors


def extract_kernel_exports(src: str) -> tuple[tuple[str, str, str], ...]:
    """Return ``(fn_name, signature_str, first_line_doc)`` for each function in ``__all__``."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return ()

    declared_all: list[str] = []
    defined_funcs: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and _is_literal(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    val = ast.literal_eval(node.value)
                    if isinstance(val, (list, tuple)):
                        declared_all = [str(x) for x in val]
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined_funcs[node.name] = node

    exports: list[tuple[str, str, str]] = []
    for name in declared_all:
        fn = defined_funcs.get(name)
        if fn is None:
            continue
        args_str = ast.unparse(fn.args)
        ret_str = f" -> {ast.unparse(fn.returns)}" if fn.returns is not None else ""
        sig = f"{name}({args_str}){ret_str}"
        doc = (ast.get_docstring(fn) or "").strip().splitlines()
        summary = doc[0].strip() if doc else ""
        exports.append((name, sig, summary))
    return tuple(exports)


def render_kernel_plugin_block(
    skill_name: str,
    exports: Sequence[tuple[str, str, str]],
) -> str:
    """Format the ``<kernel-plugin>`` block appended to ``use_skill`` results."""
    if not exports:
        return ""
    slug = re.sub(r"[^A-Za-z0-9_]", "_", skill_name)
    names_line = ", ".join(f"`{fn}` (`{slug}__{fn}`)" for fn, _, _ in exports)
    sig_lines = [
        f"- `{sig}` — {doc}" if doc else f"- `{sig}`"
        for _, sig, doc in exports
    ]
    return (
        '<kernel-plugin language="python" file="kernel.py">\n'
        f"Loaded into the python kernel: {names_line}.\n"
        "Exported functions:\n"
        + "\n".join(sig_lines)
        + "\nCall these directly in subsequent `python` tool calls without importing.\n"
        "</kernel-plugin>"
    )


def build_sidecar_bootstrap_code(
    skill_name: str,
    kernel_path: Path,
    src: str,
    *,
    skills_root: Path | None = None,
) -> str:
    """Build self-contained Python code to inject a skill's ``kernel.py`` into a live kernel."""
    slug = re.sub(r"[^A-Za-z0-9_]", "_", skill_name)
    root_str = str(skills_root or next(
        (parent for parent in kernel_path.parents if parent.name == "skills"),
        DEFAULT_SKILLS_ROOT,
    ))
    payload = json.dumps(
        {
            "skill": skill_name,
            "slug": slug,
            "path": str(kernel_path),
            "src": src,
            "root": root_str,
        }
    )
    return (
        f"# skill:{skill_name} kernel.py (auto-injected on use_skill)\n"
        "def __omicsclaw_inject_sidecar():\n"
        "    import json, pathlib, re, sys, types\n"
        f"    cfg = json.loads({payload!r})\n"
        "    g = globals()\n"
        "    if 'load_skill' not in g:\n"
        "        def load_skill(name, *, root=cfg['root']):\n"
        "            base = pathlib.Path(root)\n"
        "            matches = [p.parent for p in base.rglob('SKILL.md') if p.parent.name == name]\n"
        "            if not matches:\n"
        "                raise LookupError(f'Unknown skill: {name}')\n"
        "            kp = matches[0] / 'kernel.py'\n"
        "            mod = types.ModuleType(f\"omicsclaw_skill_{re.sub(r'[^A-Za-z0-9_]', '_', name)}\")\n"
        "            mod.__file__ = str(kp)\n"
        "            exec(compile(kp.read_text(encoding='utf-8'), str(kp), 'exec'), mod.__dict__)\n"
        "            return mod\n"
        "        g['load_skill'] = load_skill\n"
        "        for mod_name in ('skills', 'skills._sdk', 'skills._sdk.notebook'):\n"
        "            if mod_name not in sys.modules:\n"
        "                sys.modules[mod_name] = types.ModuleType(mod_name)\n"
        "        sys.modules['skills._sdk.notebook'].load_skill = load_skill\n"
        "    ns = {'__name__': f\"omicsclaw_skill_{cfg['slug']}\", '__file__': cfg['path']}\n"
        "    exec(compile(cfg['src'], cfg['path'], 'exec'), ns)\n"
        "    for fn_name in ns.get('__all__', ()):\n"
        "        if fn_name in ns:\n"
        "            g[fn_name] = ns[fn_name]\n"
        "            g[f\"{cfg['slug']}__{fn_name}\"] = ns[fn_name]\n"
        "__omicsclaw_inject_sidecar()\n"
        "del __omicsclaw_inject_sidecar\n"
    )


def load_skill(name: str, *, root: Path | str | None = None) -> types.ModuleType:
    """Load a skill's ``kernel.py`` sidecar as a module using pure stdlib ``compile`` + ``exec``."""
    base = Path(root) if root is not None else DEFAULT_SKILLS_ROOT
    target_dir: Path | None = None
    if (base / name / "SKILL.md").is_file():
        target_dir = base / name
    else:
        for md in sorted(base.rglob("SKILL.md")):
            if md.parent.name == name:
                target_dir = md.parent
                break
    if target_dir is None:
        raise LookupError(f"Skill {name!r} not found under {base}")
    kernel_path = target_dir / "kernel.py"
    if not kernel_path.is_file():
        raise FileNotFoundError(f"Skill {name!r} has no kernel.py at {kernel_path}")
    src = kernel_path.read_text(encoding="utf-8")
    errors = validate_kernel_ast(src)
    if errors:
        raise ValueError(f"Skill {name!r} kernel.py failed sidecar AST gate: {'; '.join(errors)}")
    slug = re.sub(r"[^A-Za-z0-9_]", "_", name)
    mod = types.ModuleType(f"omicsclaw_skill_{slug}")
    mod.__file__ = str(kernel_path)
    exec(compile(src, str(kernel_path), "exec"), mod.__dict__)
    return mod


def validate_kernel_isolated(kernel_path: Path) -> list[str]:
    """Execute *kernel_path* in an isolated ``python -I -S`` subprocess (site-packages disabled)."""
    proc = subprocess.run(
        [sys.executable, "-I", "-S", str(kernel_path)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ["non-zero exit"]
        return [f"python -I -S execution failed (exit {proc.returncode}): {detail[0]}"]
    return []


def _parse_frontmatter_fields(text: str) -> tuple[dict[str, str], str] | None:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end == -1:
        return None
    raw_header = text[4:end]
    body = text[end + 5:]
    fields: dict[str, str] = {}
    current_key: str | None = None
    for line in raw_header.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith((" ", "\t", "-")) and ":" in line:
            key, _, val = line.partition(":")
            current_key = key.strip()
            fields[current_key] = val.strip().strip("'\"")
        elif current_key and line.startswith((" ", "\t")):
            fields[current_key] = (fields[current_key] + " " + line.strip()).strip()
    return fields, body


def validate_skill_dir(skill_dir: Path, *, check_isolated: bool = True) -> list[str]:
    """Validate one skill directory against the Claude Science 4-part SKILL.md + kernel.py contract."""
    errors: list[str] = []
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return [f"{skill_dir}: missing SKILL.md"]

    text = skill_md.read_text(encoding="utf-8")
    lines = text.splitlines()
    if len(lines) > 200:
        errors.append(f"SKILL.md has {len(lines)} lines (must be <= 200 lines)")

    parsed = _parse_frontmatter_fields(text)
    if parsed is None:
        errors.append("SKILL.md is missing valid YAML frontmatter delimited by ---")
        return errors

    fields, body = parsed
    name = fields.get("name", "")
    description = fields.get("description", "")

    if not name or not re.fullmatch(r"[a-z0-9-]+", name) or len(name) > 64:
        errors.append(f"frontmatter name {name!r} must be hyphen-case (<= 64 chars)")
    if name and name != skill_dir.name:
        errors.append(f"frontmatter name {name!r} does not match directory name {skill_dir.name!r}")
    if len(description) < 20 or len(description) > 1024:
        errors.append(f"frontmatter description length ({len(description)}) must be between 20 and 1024 chars")
    if "<" in description or ">" in description:
        errors.append("frontmatter description must not contain angle brackets (< or >)")
    if not ("Load when" in description or "Load for" in description) or "Skip" not in description:
        errors.append("frontmatter description must state both when to Load and when to Skip")

    if not body.lstrip().startswith("# "):
        errors.append("SKILL.md body must start with a level-1 '# <Title>' heading")
    for heading in REQUIRED_HEADINGS:
        if heading not in body:
            errors.append(f"SKILL.md missing required section: {heading}")
    for legacy in ("<!-- api:begin", ":param "):
        if legacy in body:
            errors.append(f"SKILL.md contains legacy API docstring marker {legacy!r}")

    dep_section = body.split("## Dependencies", 1)[1] if "## Dependencies" in body else ""
    dep_lines = [line for line in dep_section.splitlines() if _DEP_LINE_RE.match(line)]
    if len(dep_lines) != 1:
        errors.append(f"## Dependencies must contain exactly 1 back-quoted package line (found {len(dep_lines)})")

    kernel_path = skill_dir / "kernel.py"
    if not kernel_path.is_file():
        errors.append("missing kernel.py micro-helper")
    else:
        ksrc = kernel_path.read_text(encoding="utf-8")
        klines = len(ksrc.splitlines())
        if klines >= 80:
            errors.append(f"kernel.py has {klines} lines (must be < 80 lines)")
        errors.extend(validate_kernel_ast(ksrc))
        if check_isolated and not errors:
            errors.extend(validate_kernel_isolated(kernel_path))

    return errors


def scaffold_skill(domain: str, name: str, *, skills_root: Path | None = None) -> Path:
    """Scaffold a new Claude Science-compliant skill under ``skills/<domain>/<name>``."""
    base = skills_root or DEFAULT_SKILLS_ROOT
    if not re.fullmatch(r"[a-z0-9-]+", name):
        raise ValueError(f"skill name {name!r} must be lowercase hyphen-case")
    target = base / domain / name
    if target.exists():
        raise FileExistsError(f"skill directory {target} already exists")
    target.mkdir(parents=True, exist_ok=False)

    title = " ".join(part.capitalize() for part in name.split("-"))
    skill_md_content = (
        f"---\n"
        f"name: {name}\n"
        f"description: Load when performing {title.lower()} analysis in {domain} workflows. "
        f"Skip when upstream preprocessing or downstream cross-modality integration is needed instead.\n"
        f"trigger: {name}, {domain} {name.replace('-', ' ')}\n"
        f"tags:\n"
        f"- {domain}\n"
        f"---\n\n"
        f"# {title}\n\n"
        f"## 1. Method Decision Matrix & When to Use\n\n"
        f"| Method / Library | Best For | Input Assumptions | Key Parameters |\n"
        f"|---|---|---|---|\n"
        f"| **Standard (`pandas` / `scipy`)** | Baseline {title.lower()} quantification | Normalized tabular or AnnData input | `alpha=0.05` |\n\n"
        f"## 2. How to Run (Idiomatic Upstream Patterns)\n\n"
        f"Calling `use_skill({name!r})` automatically loads `kernel.py` helpers into the persistent Python kernel:\n\n"
        f"```python\n"
        f"import pandas as pd\n\n"
        f"df = pd.read_csv('data/input.csv')\n"
        f"summary = summarize_metrics(df)\n"
        f"summary.to_csv('tables/summary.csv', index=False)\n"
        f"```\n\n"
        f"## 3. Output Format & Verification Contract\n\n"
        f"- `tables/summary.csv`: Summary table with columns `metric`, `mean`, `std`.\n\n"
        f"## 4. Gotchas & Troubleshooting\n\n"
        f"- **Input normalization state**: Verify whether raw counts or log-transformed values are expected before running statistical tests.\n"
        f"- **Missing values**: Drop or impute NaN entries prior to matrix decomposition.\n\n"
        f"## Dependencies\n\n"
        f"`numpy`, `pandas`\n"
    )
    kernel_content = (
        f'"""Lightweight kernel helpers for {name}."""\n'
        f"from __future__ import annotations\n\n"
        f'__all__ = ["summarize_metrics"]\n\n\n'
        f"def summarize_metrics(df) -> object:\n"
        f'    """Summarize numeric columns of a DataFrame with mean and standard deviation."""\n'
        f"    import numpy as np, pandas as pd\n"
        f"    num = df.select_dtypes(include=[np.number])\n"
        f'    return pd.DataFrame({{"metric": num.columns, "mean": num.mean().to_numpy(), "std": num.std(ddof=0).to_numpy()}})\n'
    )
    (target / "SKILL.md").write_text(skill_md_content, encoding="utf-8")
    (target / "kernel.py").write_text(kernel_content, encoding="utf-8")
    return target


def _stats(values: list[float]) -> dict[str, float]:
    if not values:
        return {"mean": 0.0, "stddev": 0.0, "min": 0.0, "max": 0.0}
    n = len(values)
    mean = sum(values) / n
    variance = sum((x - mean) ** 2 for x in values) / (n - 1) if n > 1 else 0.0
    return {
        "mean": round(mean, 4),
        "stddev": round(math.sqrt(variance), 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
    }


def aggregate_benchmark(runs_path: Path) -> dict:
    """Aggregate ``with_skill`` vs ``without_skill`` runs into a Claude Science benchmark summary."""
    payload = json.loads(runs_path.read_text(encoding="utf-8"))
    runs = payload.get("runs", payload if isinstance(payload, list) else [])
    by_config: dict[str, list[dict]] = {}
    for run in runs:
        cfg = str(run.get("configuration", run.get("arm", "with_skill")))
        by_config.setdefault(cfg, []).append(run)

    summary: dict[str, dict] = {}
    for cfg, items in by_config.items():
        summary[cfg] = {
            "n_runs": len(items),
            "pass_rate": _stats([float(r.get("pass_rate", 0.0)) for r in items]),
            "time_seconds": _stats([float(r.get("time_seconds", 0.0)) for r in items]),
            "tokens": _stats([float(r.get("tokens", 0.0)) for r in items]),
        }

    if "with_skill" in summary and "without_skill" in summary:
        w, b = summary["with_skill"], summary["without_skill"]
        summary["delta"] = {
            "pass_rate": round(w["pass_rate"]["mean"] - b["pass_rate"]["mean"], 4),
            "time_seconds": round(w["time_seconds"]["mean"] - b["time_seconds"]["mean"], 4),
            "tokens": round(w["tokens"]["mean"] - b["tokens"]["mean"], 4),
        }
    return {"summary": summary, "runs": runs}
