---
name: skill-creator
description: Load when creating or refactoring an OmicsClaw skill, validating SKILL.md and kernel.py, or comparing skill routing and benchmark runs. Skip when analyzing biological data directly (use the relevant domain skill instead).
trigger: create skill, skill creator, refactor skill, validate skill, kernel sidecar gate, optimize skill description, skill benchmark
tags:
- skill-creator
- harness
- evaluation
---

# Skill Creator — Authoring, Validating & Benchmarking OmicsClaw Skills

Create, refactor, validate, and benchmark OmicsClaw skills using the Claude Science progressive-disclosure architecture: a concise 4-part `SKILL.md` scientific playbook paired with an optional `<80`-line definition-only `kernel.py` micro-helper.

## 1. Method Decision Matrix & When to Use

OmicsClaw skills use a three-level progressive disclosure system so the agent's context window stays lean while domain expertise and reusable helpers remain available on demand:

| Lifecycle Stage | Artifact / Mechanism | Target Budget | Purpose & Acceptance Bar |
|---|---|---|---|
| **L1: Routing Metadata** | YAML frontmatter (`name`, `description`, `trigger`, `tags`) | `< 100` words | Always in system prompt index (`~8.5k` tokens across corpus). `description` must state both `Load when ...` and `Skip ... (use <other-skill>)`. |
| **L2: Scientific Playbook** | 4-part `SKILL.md` body + `## Dependencies` | `80–150` lines (`<= 200` max) | Loaded on demand via `use_skill(name)`. Teaches decision matrices, idiomatic upstream Python/R patterns, output verification contracts, and biological gotchas. |
| **L3: Sidecar Micro-Kernel** | `kernel.py` (auto-injected on `use_skill(name)`) | `20–75` lines (`< 80` max) | Automatically injected into the persistent Python kernel when `use_skill(name)` is called. Pure function definitions with lazy in-body imports; passes `sidecar_gate` AST validation and `python3 -I -S` execution. |
| **Trigger Evaluation** | `SkillDiscovery` BM25 + RRF eval set | `10` positive + `10` near-miss queries | Verifies `should_trigger` queries surface the skill in top-3 and near-miss queries route to sibling skills without false positives. |
| **Notebook Benchmark Loop** | `with_skill` (`Arm K1`) vs `without_skill` (`Arm K0`) | Parallel `task` subagent runs | Measures pass-rate delta on output contract assertions, notebook provenance completeness, wall time, and token usage. |

### Sidecar `kernel.py` Structural Rules (`sidecar_gate`)
1. **Definition-only module top level**: Only module docstrings, `from __future__ import annotations`, literal assignments (`__all__ = [...]`), and undecorated `def` / `async def` statements are permitted at module top level. No `class`, no decorators, no `if __name__ == "__main__":`, and no top-level function calls (e.g., `re.compile(...)`).
2. **Literal default arguments only**: Every default parameter in `def f(x=...)` must be an AST literal (`Constant`, `Tuple`/`List`/`Set`/`Dict` of literals, or `UnaryOp` on a literal). Never use `range(...)` or mutable expressions as defaults.
3. **No single-`_`-prefixed top-level names**: Helper functions or lookup tables must live inside the function body rather than at module top level.
4. **No shadowing Python `builtins`**: Top-level function names must not collide with `dir(builtins)`.
5. **Defer third-party imports inside function bodies**: Keep `import numpy as np, pandas as pd`, `import scanpy as sc`, etc. inside each `def` body so `use_skill(name)` sidecar injection and `python3 -I -S` execute in `< 1 ms` without importing heavy C/numba extensions at module load time.

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A — Validate a Skill or the Full Corpus from Python / a Step
```python
# %% [markdown]
# Audit a newly authored skill or the full skills/ corpus against the Claude Science contract.

# %%
# Calling use_skill("skill-creator") auto-injects validate_skill, validate_kernel_ast, and summarize_corpus:
errors = validate_skill("skills/spatial/spatial-de")
assert errors == [], f"Validation failed: {errors}"

# Audit the entire skills/ corpus and inspect summary metrics
summary_df = summarize_corpus("skills")
assert (summary_df["gate_passed"]).all()
print(summary_df[["skill", "domain", "skill_md_lines", "kernel_lines", "gate_passed"]].head())
```

### Pattern B — Scaffold, Validate & Refresh Domain Index via CLI
```bash
# 1. Validate one skill or all skills via omicsclaw.skills.sidecar
python -c "from pathlib import Path; from omicsclaw.skills.sidecar import validate_skill_dir; assert not validate_skill_dir(Path('skills/spatial/spatial-de'))"

# 2. Scaffold a new skill under skills/<domain>/<skill-name>
python -c "from omicsclaw.skills.sidecar import scaffold_skill; scaffold_skill('spatial', 'spatial-niches')"

# 3. Refresh skills/<domain>/INDEX.md and run the contract test suite
OMICSCLAW_WRITE_SKILL_INDEX=1 pytest tests/skills/test_domain_index_is_current.py
pytest tests/skills/test_claude_science_contract.py
```

### Pattern C — Evaluate Trigger Routing & With-Skill vs. Baseline Runs
```python
from omicsclaw.skills import SkillDiscovery, load_skills
from omicsclaw.schema import Message, Role, ToolDefinition

index = load_skills("skills")
discovery = SkillDiscovery(index)
tools = (ToolDefinition(name="use_skill", description="", input_schema={}),)

# Check whether a realistic user prompt surfaces the target skill in the top-3 discovery block
msgs = [Message(role=Role.USER, content="Find spatially variable genes on Visium slide using Moran's I")]
block = await discovery.augment(msgs, tools)
assert any("spatial-genes" in m.content for m in block)
```

## 3. Output Format & Verification Contract

When auditing or benchmarking skills in an analysis module, produce:
- `tables/skill_audit_summary.csv`: Columns `skill`, `domain`, `skill_md_lines`, `kernel_lines`, `n_exports`, `gate_passed`, `errors`.
- `tables/skill_benchmark_comparison.csv` (for `Arm K1` vs `Arm K0` evaluations): Columns `eval_id`, `arm` (`with_skill` | `without_skill`), `checks_passed`, `total_checks`, `pass_rate`, `duration_seconds`, `skill_calls_recorded`.

**Verification Invariants**:
- `validate_skill(skill_dir)` must return `[]` (zero structural, frontmatter, or AST gate errors).
- `python3 -I -S <skill_dir>/kernel.py` must exit `0` with empty stderr.
- `OMICSCLAW_WRITE_SKILL_INDEX=1 pytest tests/skills/test_domain_index_is_current.py` must pass with zero drift.

## 4. Gotchas & Troubleshooting

- **Top-level third-party imports in `kernel.py`**: Placing `import scanpy as sc` or `import pandas as pd` at module top level breaks `python3 -I -S` isolated validation and slows down sidecar injection. Always place `from __future__ import annotations` at the top of `kernel.py` (so type annotations are not evaluated at `def` time) and move library imports inside each function body.
- **Non-literal default arguments & top-level `re.compile`**: Expressions like `def f(powers=range(1, 13)):` or `_RE = re.compile(...)` at module scope are rejected by `validate_kernel_ast()`. Use a literal tuple `(1, 2, ..., 12)` or `None` resolved inside the function body.
- **One-sided `description` without counter-indications**: A `description` that only says when to load causes false-positive routing across modalities (e.g., loading `bulkrna-de` for scRNA pseudobulk or `spatial-integrate` for physical slice registration). Always include a `Skip ... (use <other-skill>)` clause.
- **Over-specifying rigid procedures in `SKILL.md`**: Explain the *why* behind biological invariants (such as raw integer counts for NB-GLM or per-sample doublet scoring) and show idiomatic upstream library patterns (`scanpy`, `squidpy`, `pydeseq2`) rather than hardcoding brittle multi-page wrappers.

## Dependencies

`numpy`, `pandas`
