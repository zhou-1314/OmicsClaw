# OmicsClaw Skill Template (Claude Science 4-Part Playbook + `kernel.py` Sidecar)

This directory is the starter template for OmicsClaw skills, aligned with the **Claude Science (`operon`)** progressive-disclosure skill architecture.

Copy this directory into `skills/<domain>/<my-new-skill>` and customize
`SKILL.md` and `kernel.py`. The `skill-creator` helper can validate the result.

## Layout

Each skill is a self-contained directory holding `SKILL.md` and a `<80`-line `kernel.py` micro-helper:

```
skills/<domain>/<skill>/
├── SKILL.md       4-part scientific playbook + YAML routing frontmatter + ## Dependencies
├── kernel.py      <80-line definition-only sidecar registered by use_skill for the persistent Python kernel
└── references/    (optional) deeper methodological notes or parameter references read on demand
```

## 1. `SKILL.md` Contract (4-Part Scientific Playbook)

`SKILL.md` is the single source of truth for discovery, dependency management, and methodology.

### Frontmatter Keys
Four YAML frontmatter keys are read by `omicsclaw.skills`:

| Key | Required | Purpose |
|---|---|---|
| `name` | yes | Unique hyphen-case identifier (`^[a-z0-9-]+$`, `<= 64` chars), matching the skill directory name. |
| `description` | yes | Routing description (`20..1024` chars, no `<` or `>`). Must state both `Load when ...` and `Skip ... (use <other-skill>)`. |
| `trigger` | no | Comma-separated discovery keywords indexed by `SkillDiscovery` (BM25 + RRF) and `/skills <query>`. |
| `tags` | no | Domain and modality tags weighted `2x` in `SkillDiscovery`. |

### Required Body Sections (`<= 200` lines total)
1. `# <Title>` (immediately following the YAML frontmatter)
2. `## 1. Method Decision Matrix & When to Use` — Markdown comparison table of upstream algorithms (`scanpy`, `squidpy`, `pydeseq2`, `scvi-tools`, `gseapy`, etc.), input assumptions, and when to prefer each.
3. `## 2. How to Run (Idiomatic Upstream Patterns)` — Copy-pasteable Python/R step code using idiomatic upstream libraries and the helpers registered by `use_skill`.
4. `## 3. Output Format & Verification Contract` — Expected `intermediate/` and `tables/` outputs, column schemas, and explicit scientific invariants.
5. `## 4. Gotchas & Troubleshooting` — 3–4 non-obvious biological and statistical pitfalls (matrix layer state, species prefixes, confounding, dispersion failure modes).
6. `## Dependencies` — Contains exactly one line of back-quoted, comma-separated PyPI package names (e.g., `` `numpy`, `pandas`, `scanpy` ``) read by `omicsclaw.skillenv`.

## 2. `kernel.py` Sidecar Contract (`sidecar_gate`)

`use_skill` registers sidecar helpers for the current session. The runtime
injects them before the next Python cell, without adding a hidden cell to its
execution history. Python clients can load a module with
`omicsclaw.skills.sidecar.load_skill`.

Every `kernel.py` must be **`< 80` lines** and pass the Claude Science `sidecar_gate` AST and isolated execution checks:

1. **Definition-only module top level**: Only the module docstring, `from __future__ import annotations`, `__all__ = [...]`, and undecorated `def` / `async def` functions are allowed at module top level. No `class`, no decorators, no `if __name__ == "__main__":`, and no top-level function calls (such as `re.compile(...)`).
2. **Literal default arguments only**: Every default parameter in `def f(x=...)` must be an AST literal (`Constant`, `Tuple`/`List`/`Set`/`Dict` of literals, or `UnaryOp` on a literal).
3. **No single-`_`-prefixed top-level names**: Move private helpers or lookup tables inside the function body that uses them.
4. **No shadowing Python `builtins`**: Top-level function names must not collide with `dir(builtins)`.
5. **Lazy function-scope imports**: Defer all imports (except `from __future__ import annotations`) into function bodies so `python3 -I -S kernel.py` (with site-packages disabled) exits `0` without importing scientific backends during loading.

## 3. Validation & Evaluation Workflow (`skill-creator`)

```bash
# Validate the corpus and refresh the domain index
OMICSCLAW_WRITE_SKILL_INDEX=1 pytest tests/skills/test_domain_index_is_current.py
pytest tests/skills/test_claude_science_contract.py
```

For end-to-end guidance on trigger evaluation (`SkillDiscovery`) and `with_skill` vs `without_skill` subagent benchmarking, load the `skill-creator` skill (`skills/skill-creator/SKILL.md`).
