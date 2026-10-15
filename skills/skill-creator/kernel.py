from __future__ import annotations
__all__ = ["validate_kernel_ast", "validate_skill", "summarize_corpus"]
def validate_kernel_ast(src: str) -> list[str]:
    """Validate kernel.py source against Claude Science sidecar_gate AST & lazy-import rules."""
    import ast, builtins
    bset = set(dir(builtins)) | {"help", "exit", "quit", "copyright", "credits", "license"}
    def is_lit(n: ast.AST | None) -> bool:
        if isinstance(n, ast.Constant):
            return True
        if isinstance(n, (ast.Tuple, ast.List, ast.Set)):
            return all(is_lit(e) for e in n.elts)
        if isinstance(n, ast.Dict):
            return all(k is not None and is_lit(k) and is_lit(v) for k, v in zip(n.keys, n.values))
        return isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.UAdd, ast.USub)) and is_lit(n.operand)
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return [f"line {exc.lineno}: syntax error: {exc.msg}"]
    errs, names = [], []
    for n in ast.walk(tree):
        annotations = [n.annotation] if isinstance(n, (ast.arg, ast.AnnAssign)) else [n.returns] if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) else []
        if any(a is not None and any(isinstance(x, (ast.Call, ast.Lambda, ast.NamedExpr, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.Await)) for x in ast.walk(a)) for a in annotations):
            errs.append(f"line {n.lineno}: executable annotation is not allowed")
    for n in tree.body:
        if isinstance(n, ast.ImportFrom) and n.module == "__future__":
            continue
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            errs.append(f"line {n.lineno}: move top-level import inside function body")
        elif isinstance(n, ast.Pass) or (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)):
            continue
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if n.decorator_list:
                errs.append(f"line {n.lineno}: decorators on {n.name} are not allowed")
            errs.extend(f"line {d.lineno}: non-literal default in {n.name}()" for d in list(n.args.defaults) + [x for x in n.args.kw_defaults if x is not None] if not is_lit(d))
            names.append((n.lineno, n.name))
        elif isinstance(n, ast.Assign) and is_lit(n.value):
            for t in n.targets:
                names.append((n.lineno, t.id)) if isinstance(t, ast.Name) else errs.append(f"line {n.lineno}: invalid assign target")
        else:
            errs.append(f"line {n.lineno}: top-level {type(n).__name__} not allowed (definitions only)")
    errs.extend(f"line {l}: {nm!r} starts with '_' at top level" for l, nm in names if nm != "__all__" and nm.startswith("_"))
    errs.extend(f"line {l}: {nm!r} shadows a Python builtin" for l, nm in names if nm in bset)
    declarations = [n.value for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in n.targets)]
    try: exports = ast.literal_eval(declarations[-1]) if declarations else None
    except (ValueError, TypeError, SyntaxError): exports = None
    if not isinstance(exports, (list, tuple)) or not exports or not all(isinstance(x, str) for x in exports) or len(exports) != len(set(exports)):
        errs.append("__all__ must contain unique function names")
    return errs
def validate_skill(skill_dir: str) -> list[str]:
    """Validate SKILL.md structure and kernel.py sidecar for a skill directory."""
    import pathlib, re
    p = pathlib.Path(skill_dir)
    md, kp, errs = p / "SKILL.md", p / "kernel.py", []
    if not md.is_file():
        return [f"{p}: missing SKILL.md"]
    txt = md.read_text(encoding="utf-8")
    if not txt.startswith("---\n") or "\n---\n" not in txt[4:]:
        errs.append("missing YAML frontmatter")
    req_headings = ("## 1. Method Decision Matrix & When to Use", "## 2. How to Run (Idiomatic Upstream Patterns)", "## 3. Output Format & Verification Contract", "## 4. Gotchas & Troubleshooting", "## Dependencies")
    errs.extend(f"missing heading: {h}" for h in req_headings if h not in txt)
    if not re.search(r"^`[A-Za-z0-9._-]+`", txt, re.M):
        errs.append("missing package line under ## Dependencies")
    if not kp.is_file(): errs.append("missing kernel.py")
    if kp.is_file():
        ksrc = kp.read_text(encoding="utf-8")
        if len(ksrc.splitlines()) >= 80:
            errs.append(f"kernel.py has {len(ksrc.splitlines())} lines (must be < 80)")
        errs.extend(validate_kernel_ast(ksrc))
    return errs
def summarize_corpus(skills_root: str = "skills"):
    """Audit all skills under skills_root and return a summary DataFrame (or list of dicts)."""
    import pathlib
    root = pathlib.Path(skills_root)
    rows = [{"skill": m.parent.name, "domain": m.relative_to(root).parts[0], "skill_md_lines": len(m.read_text(encoding="utf-8").splitlines()), "kernel_lines": len((m.parent / "kernel.py").read_text(encoding="utf-8").splitlines()) if (m.parent / "kernel.py").is_file() else 0, "gate_passed": len(e := validate_skill(str(m.parent))) == 0, "errors": "; ".join(e)} for m in sorted(root.rglob("SKILL.md"))]
    try:
        import pandas as pd
        return pd.DataFrame(rows)
    except ImportError:
        return rows
