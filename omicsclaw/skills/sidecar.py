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
    skill_name: str | Sequence[tuple[str, Path | str] | tuple[str, Path | str, str]] = "",
    kernel_path: Path | str | None = None,
    src: str = "",
    *,
    skills_root: Path | None = None,
    workspace: Path | str | None = None,
) -> str:
    """Build self-contained Python code to inject one or more skill ``kernel.py`` files into a live kernel or replay script."""
    items: list[dict[str, str]] = []
    inferred_root: Path | None = skills_root
    ws_path: Path | None = Path(workspace) if workspace is not None else None

    def _add_item(s_name: str, k_path: Path | str | None, k_src: str) -> None:
        nonlocal inferred_root
        s_str = str(s_name or "").strip()
        if not s_str:
            return
        s_slug = re.sub(r"[^A-Za-z0-9_]", "_", s_str)
        p_obj = Path(k_path) if k_path is not None and str(k_path) != "" else None
        if p_obj is not None and not p_obj.is_file() and ws_path is not None and (ws_path / p_obj).is_file():
            p_obj = ws_path / p_obj
        if inferred_root is None and p_obj is not None:
            inferred_root = next(
                (parent for parent in p_obj.parents if parent.name == "skills"),
                None,
            )
        resolved_src = k_src
        if not resolved_src and p_obj is not None:
            try:
                if p_obj.is_file():
                    resolved_src = p_obj.read_text(encoding="utf-8")
            except OSError:
                resolved_src = ""
        if not resolved_src:
            search_bases: list[Path] = []
            if inferred_root is not None:
                search_bases.append(inferred_root)
            if ws_path is not None:
                search_bases.extend([ws_path / "provenance" / "sidecars", ws_path / "skills"])
            search_bases.append(DEFAULT_SKILLS_ROOT)
            for search_base in search_bases:
                if search_base.is_dir():
                    for cand in search_base.rglob("kernel.py"):
                        if cand.parent.name == s_str:
                            try:
                                resolved_src = cand.read_text(encoding="utf-8")
                                if p_obj is None:
                                    p_obj = cand
                            except OSError:
                                pass
                            break
                if resolved_src:
                    break
        if ws_path is not None and resolved_src:
            import hashlib

            k_sha = hashlib.sha256(resolved_src.encode("utf-8")).hexdigest()
            try:
                sdir = ws_path / "provenance" / "sidecars" / s_str
                sdir.mkdir(parents=True, exist_ok=True)
                (sdir / "kernel.py").write_text(resolved_src, encoding="utf-8")
                entry = {
                    "skill": s_str,
                    "kernel_path": str(p_obj) if p_obj is not None else f"provenance/sidecars/{s_str}/kernel.py",
                    "staged_path": f"provenance/sidecars/{s_str}/kernel.py",
                    "kernel_sha256": k_sha,
                }
                for pdir in (ws_path / "provenance", ws_path / ".omicsclaw" / "provenance"):
                    pdir.mkdir(parents=True, exist_ok=True)
                    mf = pdir / "skills_loaded.json"
                    cur: list[dict[str, str]] = []
                    if mf.is_file():
                        try:
                            raw = json.loads(mf.read_text(encoding="utf-8"))
                            if isinstance(raw, list):
                                cur = [x for x in raw if isinstance(x, dict)]
                        except Exception:
                            cur = []
                    cur = [x for x in cur if x.get("skill") != s_str]
                    cur.append(entry)
                    mf.write_text(json.dumps(cur, indent=2, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
        items.append(
            {
                "skill": s_str,
                "slug": s_slug,
                "path": str(p_obj) if p_obj is not None else "",
                "src": resolved_src,
            }
        )

    if isinstance(skill_name, str):
        if skill_name:
            _add_item(skill_name, kernel_path, src)
    elif isinstance(skill_name, Sequence):
        for entry in skill_name:
            if isinstance(entry, (tuple, list)) and len(entry) >= 2:
                s_n = str(entry[0])
                k_p = entry[1]
                k_s = str(entry[2]) if len(entry) >= 3 and entry[2] is not None else ""
                _add_item(s_n, k_p, k_s)

    root_str = str(inferred_root if inferred_root is not None else DEFAULT_SKILLS_ROOT)
    first_item = items[0] if items else {"skill": "", "slug": "", "path": "", "src": ""}
    header_label = ", ".join(it["skill"] for it in items) if items else "bootstrap"
    payload = json.dumps(
        {
            "skill": first_item["skill"],
            "slug": first_item["slug"],
            "path": first_item["path"],
            "src": first_item["src"],
            "root": root_str,
            "workspace": str(ws_path) if ws_path is not None else "",
            "items": items,
        }
    )
    return (
        f"# skill:{header_label} kernel.py (auto-injected on use_skill)\n"
        "def __omicsclaw_inject_sidecar():\n"
        "    import functools, hashlib, json, pathlib, re, time, types\n"
        f"    cfg = json.loads({payload!r})\n"
        "    g = globals()\n"
        "    embedded = g.setdefault('__omicsclaw_embedded_sidecars__', {})\n"
        "    g['__omicsclaw_register_sidecar__'] = embedded\n"
        "    for _it in cfg.get('items', ()):\n"
        "        if _it.get('skill'):\n"
        "            embedded[_it['skill']] = _it\n"
        "    def _summarize_arg(val):\n"
        "        tname = type(val).__name__\n"
        "        if hasattr(val, 'shape') and hasattr(val, 'obs'):\n"
        "            return f'AnnData(shape={tuple(int(x) for x in val.shape)}, obs={list(val.obs.columns)[:6]})'\n"
        "        if hasattr(val, 'shape') and hasattr(val, 'columns'):\n"
        "            return f'DataFrame(shape={tuple(int(x) for x in val.shape)}, cols={list(val.columns)[:6]})'\n"
        "        if isinstance(val, (str, int, float, bool)) or val is None:\n"
        "            return repr(val)[:80]\n"
        "        return f'{tname}({repr(val)[:60]})'\n"
        "    def _record_loaded(s_name, s_slug, k_path, k_sha, k_src=''):\n"
        "        info = {'skill': s_name, 'slug': s_slug, 'kernel_path': str(k_path), 'kernel_sha256': str(k_sha)}\n"
        "        loaded = g.setdefault('__omicsclaw_loaded_sidecars__', [])\n"
        "        if not any(isinstance(x, dict) and x.get('skill') == s_name for x in loaded):\n"
        "            loaded.append(info)\n"
        "        if k_src and s_name:\n"
        "            try:\n"
        "                ws_cfg = cfg.get('workspace') or ''\n"
        "                base_ws = pathlib.Path(ws_cfg) if ws_cfg else pathlib.Path.cwd()\n"
        "                is_repo_root = (base_ws / 'omicsclaw').is_dir() and (base_ws / 'pyproject.toml').is_file()\n"
        "                if ws_cfg or not is_repo_root:\n"
        "                    sdir = base_ws / 'provenance' / 'sidecars' / s_name\n"
        "                    sdir.mkdir(parents=True, exist_ok=True)\n"
        "                    (sdir / 'kernel.py').write_text(k_src, encoding='utf-8')\n"
        "                    entry = {'skill': s_name, 'kernel_path': str(k_path), 'staged_path': f'provenance/sidecars/{s_name}/kernel.py', 'kernel_sha256': str(k_sha)}\n"
        "                    for pdir in (base_ws / 'provenance', base_ws / '.omicsclaw' / 'provenance'):\n"
        "                        pdir.mkdir(parents=True, exist_ok=True)\n"
        "                        mf = pdir / 'skills_loaded.json'\n"
        "                        cur = []\n"
        "                        if mf.is_file():\n"
        "                            try:\n"
        "                                raw = json.loads(mf.read_text(encoding='utf-8'))\n"
        "                                if isinstance(raw, list):\n"
        "                                    cur = [x for x in raw if isinstance(x, dict)]\n"
        "                            except Exception:\n"
        "                                cur = []\n"
        "                        cur = [x for x in cur if x.get('skill') != s_name]\n"
        "                        cur.append(entry)\n"
        "                        mf.write_text(json.dumps(cur, indent=2, ensure_ascii=False), encoding='utf-8')\n"
        "            except Exception:\n"
        "                pass\n"
        "        rec_load = g.get('__omicsclaw_record_sidecar_loaded__')\n"
        "        if callable(rec_load):\n"
        "            try:\n"
        "                rec_load(info)\n"
        "            except Exception:\n"
        "                pass\n"
        "    def _wrap_fn(fn, s_name, s_slug, f_name, k_path, k_sha):\n"
        "        if not callable(fn) or getattr(fn, '__omicsclaw_wrapped__', False):\n"
        "            return fn\n"
        "        def _inspect_assumptions(f_name, args, kwargs, ret_val):\n"
        "            info = {}\n"
        "            try:\n"
        "                for kw_k in ('layer', 'method', 'group_by', 'groupby', 'batch_key', 'donor_key', 'use_raw'):\n"
        "                    if kw_k in kwargs and isinstance(kwargs[kw_k], (str, int, float, bool, type(None))):\n"
        "                        info[kw_k] = kwargs[kw_k]\n"
        "                        if kw_k == 'groupby' and 'group_by' not in info:\n"
        "                            info['group_by'] = kwargs[kw_k]\n"
        "                all_vals = list(args[:4]) + list(kwargs.values())[:4]\n"
        "                numeric_groups = []\n"
        "                for v in all_vals:\n"
        "                    if hasattr(v, 'shape') and hasattr(v, 'obs'):\n"
        "                        n_obs = int(v.shape[0]) if len(v.shape) >= 1 else 0\n"
        "                        info['n_obs'] = n_obs\n"
        "                        if len(v.shape) >= 2:\n"
        "                            info['n_vars'] = int(v.shape[1])\n"
        "                        obs_cols = [str(c) for c in getattr(v.obs, 'columns', ())]\n"
        "                        rep_cols = [c for c in obs_cols if any(tok in c.lower() for tok in ('sample', 'donor', 'patient', 'replicate', 'subject', 'batch'))]\n"
        "                        info['replicate_columns'] = rep_cols[:6]\n"
        "                        info['batch_keys'] = rep_cols[:6]\n"
        "                        if rep_cols:\n"
        "                            try:\n"
        "                                col_val = v.obs[rep_cols[0]]\n"
        "                                n_rep = int(col_val.nunique()) if hasattr(col_val, 'nunique') else len(set(col_val))\n"
        "                                info['n_replicates'] = n_rep\n"
        "                                info['n_donors'] = n_rep\n"
        "                            except Exception:\n"
        "                                pass\n"
        "                        info['pseudoreplication_risk'] = bool(n_obs > 50 and (not rep_cols or info.get('n_replicates', 2) < 2))\n"
        "                        x_mat = getattr(v, 'X', None)\n"
        "                        if x_mat is not None:\n"
        "                            arr = x_mat.toarray() if hasattr(x_mat, 'toarray') else x_mat\n"
        "                            flat_vals = []\n"
        "                            try:\n"
        "                                import numpy as _np\n"
        "                                sub = _np.asarray(arr[:min(100, arr.shape[0]), :min(100, arr.shape[1])], dtype=float)\n"
        "                                finite = sub[_np.isfinite(sub)]\n"
        "                                flat_vals = [float(x) for x in finite.ravel()]\n"
        "                            except Exception:\n"
        "                                try:\n"
        "                                    for r_item in list(arr)[:100]:\n"
        "                                        if isinstance(r_item, (list, tuple)):\n"
        "                                            for c_item in r_item[:100]:\n"
        "                                                if isinstance(c_item, (int, float)):\n"
        "                                                    flat_vals.append(float(c_item))\n"
        "                                        elif isinstance(r_item, (int, float)):\n"
        "                                            flat_vals.append(float(r_item))\n"
        "                                except Exception:\n"
        "                                    pass\n"
        "                            if flat_vals:\n"
        "                                vmin, vmax = min(flat_vals), max(flat_vals)\n"
        "                                is_int = all(abs(x - round(x)) < 1e-5 for x in flat_vals)\n"
        "                                info['raw_counts_looks_integer'] = bool(is_int and vmin >= 0)\n"
        "                                if vmin < 0:\n"
        "                                    info['input_scale'] = 'scaled_or_centered'\n"
        "                                elif is_int and vmax >= 20:\n"
        "                                    info['input_scale'] = 'raw_counts'\n"
        "                                elif vmax <= 30 and not is_int:\n"
        "                                    info['input_scale'] = 'log_normalized'\n"
        "                                else:\n"
        "                                    info['input_scale'] = 'continuous'\n"
        "                    elif hasattr(v, 'shape') and hasattr(v, 'columns'):\n"
        "                        n_rows = int(v.shape[0]) if len(v.shape) >= 1 else 0\n"
        "                        info['n_rows'] = n_rows\n"
        "                        cols = [str(c) for c in getattr(v, 'columns', ())]\n"
        "                        rep_cols = [c for c in cols if any(tok in c.lower() for tok in ('sample', 'donor', 'patient', 'replicate', 'subject', 'batch'))]\n"
        "                        if rep_cols:\n"
        "                            info['replicate_columns'] = rep_cols[:6]\n"
        "                            info['batch_keys'] = rep_cols[:6]\n"
        "                            try:\n"
        "                                col_val = v[rep_cols[0]]\n"
        "                                n_rep = int(col_val.nunique()) if hasattr(col_val, 'nunique') else len(set(col_val))\n"
        "                                info['n_replicates'] = n_rep\n"
        "                                info['n_donors'] = n_rep\n"
        "                            except Exception:\n"
        "                                pass\n"
        "                        try:\n"
        "                            import numpy as _np\n"
        "                            num_df = v.select_dtypes(include=['number'])\n"
        "                            if not num_df.empty:\n"
        "                                sub = _np.asarray(num_df.iloc[:min(100, len(num_df)), :min(50, num_df.shape[1])].to_numpy(), dtype=float)\n"
        "                                finite = sub[_np.isfinite(sub)]\n"
        "                                if finite.size > 0:\n"
        "                                    vmin, vmax = float(finite.min()), float(finite.max())\n"
        "                                    is_int = bool(_np.all(_np.abs(finite - _np.round(finite)) < 1e-5))\n"
        "                                    info['raw_counts_looks_integer'] = bool(is_int and vmin >= 0)\n"
        "                                    if vmin < 0:\n"
        "                                        info['input_scale'] = 'scaled_or_centered'\n"
        "                                    elif is_int and vmax >= 20:\n"
        "                                        info['input_scale'] = 'raw_counts'\n"
        "                                    elif vmax <= 30 and not is_int:\n"
        "                                        info['input_scale'] = 'log_normalized'\n"
        "                                    else:\n"
        "                                        info['input_scale'] = 'continuous'\n"
        "                        except Exception:\n"
        "                            pass\n"
        "                    elif isinstance(v, (list, tuple)) and len(v) >= 2 and all(isinstance(x, (int, float)) for x in v[:20]):\n"
        "                        numeric_groups.append([float(x) for x in v[:200]])\n"
        "                if len(numeric_groups) >= 2:\n"
        "                    try:\n"
        "                        vars_ = []\n"
        "                        for grp in numeric_groups:\n"
        "                            if len(grp) >= 2:\n"
        "                                mean_g = sum(grp) / len(grp)\n"
        "                                vars_.append(sum((x - mean_g) ** 2 for x in grp) / (len(grp) - 1))\n"
        "                        if len(vars_) >= 2 and min(vars_) > 0:\n"
        "                            v_ratio = round(max(vars_) / min(vars_), 4)\n"
        "                            info['variance_ratio'] = v_ratio\n"
        "                            info['is_homogeneous'] = bool(v_ratio <= 4.0)\n"
        "                    except Exception:\n"
        "                        pass\n"
        "                if ret_val is not None and hasattr(ret_val, 'shape') and hasattr(ret_val, 'columns'):\n"
        "                    info['output_rows'] = int(ret_val.shape[0])\n"
        "                    rcols = {str(c).lower(): c for c in getattr(ret_val, 'columns', ())}\n"
        "                    for pkey in ('p_adj', 'padj', 'qval', 'fdr', 'pvals_adj', 'p_val', 'pvalue', 'pvals'):\n"
        "                        if pkey in rcols:\n"
        "                            col_s = ret_val[rcols[pkey]]\n"
        "                            if hasattr(col_s, 'dropna'):\n"
        "                                col_s = col_s.dropna()\n"
        "                                info['n_tested'] = int(len(col_s))\n"
        "                                info['n_significant'] = int((col_s < 0.05).sum())\n"
        "                            elif isinstance(col_s, (list, tuple)):\n"
        "                                valid_p = [x for x in col_s if isinstance(x, (int, float))]\n"
        "                                info['n_tested'] = len(valid_p)\n"
        "                                info['n_significant'] = sum(1 for x in valid_p if x < 0.05)\n"
        "                            info['significance_column'] = str(rcols[pkey])\n"
        "                            break\n"
        "            except Exception:\n"
        "                pass\n"
        "            return info\n"
        "        @functools.wraps(fn)\n"
        "        def _wrapped(*args, **kwargs):\n"
        "            t0 = time.perf_counter()\n"
        "            _ret_val = None\n"
        "            try:\n"
        "                _ret_val = fn(*args, **kwargs)\n"
        "                return _ret_val\n"
        "            finally:\n"
        "                dt = round(time.perf_counter() - t0, 4)\n"
        "                parts = [_summarize_arg(a) for a in args[:4]]\n"
        "                parts.extend(f'{k}={_summarize_arg(v)}' for k, v in list(kwargs.items())[:4])\n"
        "                assumptions = _inspect_assumptions(f_name, args, kwargs, _ret_val)\n"
        "                entry = {\n"
        "                    'skill': s_name,\n"
        "                    'slug': s_slug,\n"
        "                    'function': f_name,\n"
        "                    'qualified': f'{s_slug}__{f_name}',\n"
        "                    'args_summary': ', '.join(parts)[:240],\n"
        "                    'elapsed_s': dt,\n"
        "                    'kernel_path': k_path,\n"
        "                    'kernel_sha256': k_sha,\n"
        "                    'execution_id': str(g.get('__omicsclaw_execution_id__', '')),\n"
        "                    'assumptions': assumptions,\n"
        "                }\n"
        "                g.setdefault('__omicsclaw_skill_ledger__', []).append(entry)\n"
        "                rec = g.get('__omicsclaw_record_skill_call__')\n"
        "                if callable(rec):\n"
        "                    try:\n"
        "                        rec(entry)\n"
        "                    except Exception:\n"
        "                        pass\n"
        "        _wrapped.__omicsclaw_wrapped__ = True\n"
        "        return _wrapped\n"
        "    g['__omicsclaw_wrap_skill_fn__'] = _wrap_fn\n"
        "    def _resolve_skill_source(name, default_root):\n"
        "        emb = g.get('__omicsclaw_embedded_sidecars__', {}).get(name)\n"
        "        if isinstance(emb, dict) and emb.get('src'):\n"
        "            return emb.get('path') or f'<sidecar:{name}>', emb['src']\n"
        "        cwd = pathlib.Path.cwd()\n"
        "        candidates = [\n"
        "            cwd / 'provenance' / 'sidecars' / name / 'kernel.py',\n"
        "            cwd / 'output' / 'sidecars' / name / 'kernel.py',\n"
        "            cwd / 'skills' / name / 'kernel.py',\n"
        "        ]\n"
        "        if '__file__' in g and g['__file__']:\n"
        "            fdir = pathlib.Path(g['__file__']).resolve().parent\n"
        "            candidates.append(fdir / 'sidecars' / name / 'kernel.py')\n"
        "            candidates.append(fdir.parent / 'provenance' / 'sidecars' / name / 'kernel.py')\n"
        "            candidates.append(fdir.parent / 'skills' / name / 'kernel.py')\n"
        "        for c in candidates:\n"
        "            if c.is_file():\n"
        "                return str(c), c.read_text(encoding='utf-8')\n"
        "        for root_cand in (cwd / 'skills', pathlib.Path(default_root)):\n"
        "            if not root_cand.is_dir():\n"
        "                continue\n"
        "            direct = root_cand / name / 'kernel.py'\n"
        "            if direct.is_file():\n"
        "                return str(direct), direct.read_text(encoding='utf-8')\n"
        "            matches = [p.parent / 'kernel.py' for p in root_cand.rglob('SKILL.md') if p.parent.name == name and (p.parent / 'kernel.py').is_file()]\n"
        "            if not matches:\n"
        "                matches = [p for p in root_cand.rglob('kernel.py') if p.parent.name == name]\n"
        "            if matches:\n"
        "                return str(matches[0]), matches[0].read_text(encoding='utf-8')\n"
        "        raise LookupError(f'Unknown skill: {name}')\n"
        "    if 'load_skill' not in g:\n"
        "        def load_skill(name, *, root=cfg['root']):\n"
        "            kp_str, k_src = _resolve_skill_source(name, root)\n"
        "            k_sha = hashlib.sha256(k_src.encode('utf-8')).hexdigest()\n"
        "            s_slug = re.sub(r'[^A-Za-z0-9_]', '_', name)\n"
        "            _record_loaded(name, s_slug, kp_str, k_sha, k_src)\n"
        "            mod = types.ModuleType(f'omicsclaw_skill_{s_slug}')\n"
        "            mod.__file__ = str(kp_str)\n"

        "            exec(compile(k_src, str(kp_str), 'exec'), mod.__dict__)\n"
        "            wrap = g.get('__omicsclaw_wrap_skill_fn__')\n"
        "            if callable(wrap):\n"
        "                for fn_name in getattr(mod, '__all__', ()):\n"
        "                    if hasattr(mod, fn_name):\n"
        "                        setattr(mod, fn_name, wrap(getattr(mod, fn_name), name, s_slug, fn_name, str(kp_str), k_sha))\n"
        "            return mod\n"
        "        g['load_skill'] = load_skill\n"
        "    for item in cfg.get('items', ()):\n"
        "        i_src = item.get('src') or ''\n"
        "        i_path = item.get('path') or ''\n"
        "        i_skill = item.get('skill') or ''\n"
        "        i_slug = item.get('slug') or re.sub(r'[^A-Za-z0-9_]', '_', i_skill)\n"
        "        if not i_src and i_path:\n"
        "            p_try = pathlib.Path(i_path)\n"
        "            if p_try.is_file():\n"
        "                i_src = p_try.read_text(encoding='utf-8')\n"
        "        if not i_src and i_skill:\n"
        "            try:\n"
        "                i_path, i_src = _resolve_skill_source(i_skill, cfg['root'])\n"
        "            except LookupError:\n"
        "                i_src = ''\n"
        "        if i_src:\n"
        "            k_sha = hashlib.sha256(i_src.encode('utf-8')).hexdigest()\n"
        "            _record_loaded(i_skill, i_slug, i_path, k_sha, i_src)\n"
        "            ns = {'__name__': f'omicsclaw_skill_{i_slug}', '__file__': i_path}\n"
        "            exec(compile(i_src, i_path or f'<sidecar:{i_skill}>', 'exec'), ns)\n"
        "            for fn_name in ns.get('__all__', ()):\n"
        "                if fn_name in ns:\n"
        "                    wrapped = _wrap_fn(ns[fn_name], i_skill, i_slug, fn_name, i_path, k_sha)\n"
        "                    g[fn_name] = wrapped\n"
        "                    g[f'{i_slug}__{fn_name}'] = wrapped\n"
        "__omicsclaw_inject_sidecar()\n"
        "del __omicsclaw_inject_sidecar\n"
    )


def load_skill(name: str, *, root: Path | str | None = None) -> types.ModuleType:
    """Load a skill's ``kernel.py`` sidecar as a module using pure stdlib ``compile`` + ``exec``."""
    cwd = Path.cwd()
    search_roots: list[Path] = []
    if root is not None:
        search_roots.append(Path(root))
    search_roots.extend(
        [
            cwd / "provenance" / "sidecars",
            cwd / "output" / "sidecars",
            cwd / "skills",
            DEFAULT_SKILLS_ROOT,
        ]
    )
    target_dir: Path | None = None
    for base in search_roots:
        if not base.is_dir():
            continue
        if (base / name / "SKILL.md").is_file() or (base / name / "kernel.py").is_file():
            target_dir = base / name
            break
        for md in sorted(base.rglob("SKILL.md")):
            if md.parent.name == name:
                target_dir = md.parent
                break
        if target_dir is not None:
            break
        for kp in sorted(base.rglob("kernel.py")):
            if kp.parent.name == name:
                target_dir = kp.parent
                break
        if target_dir is not None:
            break
    if target_dir is None:
        base_disp = Path(root) if root is not None else DEFAULT_SKILLS_ROOT
        raise LookupError(f"Skill {name!r} not found under {base_disp}")
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
