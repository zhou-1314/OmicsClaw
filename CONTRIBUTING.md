# Contributing to OmicsClaw

We welcome contributions from anyone working in multi-omics analysis, bioinformatics, computational biology, or related fields.

---

## How to Contribute a Skill

### Overview

OmicsClaw uses **convention-over-configuration** for skill discovery. Place your files in the correct directory with the correct naming, and the system automatically handles registry, CLI, bot routing, and agent integration — no manual wiring needed.

```
skills/<domain>/<skill-name>/
├── SKILL.md              # Required — metadata + documentation
├── <skill_name>.py       # Required — entry script (hyphens → underscores)
└── tests/
    └── test_<skill_name>.py  # Required — at least demo mode test
```

### Step 1: Create the directory

```bash
# Pick your domain: spatial, singlecell, genomics, proteomics, metabolomics, bulkrna
mkdir -p skills/<domain>/<skill-name>/tests
```

**Naming rules:**
- Folder: lowercase, hyphens (`spatial-de`, `bulkrna-enrichment`)
- Script: folder name with hyphens replaced by underscores (`spatial_de.py`, `bulkrna_enrichment.py`)
- The script filename **must** match the folder name — this is how the registry finds it

Subdomain nesting is also supported (e.g., `singlecell/scrna/sc-qc/sc_qc.py`).

### Step 2: Write skill.yaml (SKILL.md is generated)

Copy and customize the template:

```bash
cp -r templates/skill skills/<domain>/<skill-name>     # then rename + fill placeholders
```

Under **ADR 0037**, a skill is defined by a single machine contract,
`skill.yaml`, and a narrative card, `SKILL.md` — which is **generated** from
`skill.yaml`, not hand-written field by field. You edit `skill.yaml` and
regenerate. `skill.yaml` is validated by `omicsclaw/skill/schema.py:SkillManifest`
(pydantic).

**`skill.yaml` (the single source of truth):**

```yaml
schema_version: 2                  # required — v2 contract marker
id: your-skill-name                # kebab-case, matches folder name
name: your-skill-name              # usually == id
domain: spatial                    # one of the 8 domain keys (matches skills/<domain>/)
type: leaf                         # leaf (default) | consensus | workflow
version: 0.1.0
author: OmicsClaw
license: MIT
emoji: "🔬"
summary:                           # applicability — drives routing + the generated description
  load_when: <the one situation this skill is for>
  skip_when:                       # >=1 rule (lint requires at least one)
  - condition: a sibling skill already covers the request
    use: neighbouring-skill
  trigger_keywords: [preprocess, QC]
  tags: [domain, analysis-type, method]
  aliases: []                      # legacy skill names this answers to
interface:
  inputs:
    modalities: [visium]
    file_types: [h5ad]             # extensions WITHOUT the dot
    preconditions:
      data_shape: {requires_preprocessed: false}   # needs preprocessed AnnData input?
  parameters:
    allowed_extra_flags:           # flags beyond --input/--output/--demo (must match argparse)
    - --method
    - --species
    hints: {}                      # per-method tuning hints (optional)
  outputs:
    files: [report.md, result.json]
    anndata: {saves_h5ad: false}   # does the script write processed.h5ad?
runtime:
  language: python                 # python | r | bash
  entry: your_skill_name.py        # runtime entrypoint (folder name with _)
deps:
  python: [pyyaml]                 # third-party imports as PyPI names
```

`schema.py:SkillManifest` also carries the top-level `compatibility`,
`resources`, `lifecycle`, `validation`, `provenance`, `security`, and `mcp`
sections — see `templates/skill/skill.yaml` for the full annotated template and
`skills/singlecell/scrna/sc-qc/skill.yaml` for a filled-in example.

`SKILL.md` is **generated** from `skill.yaml` by `scripts/generate_skill_md.py`:
its frontmatter header and the `## Inputs & Outputs` summary are auto-populated
(do **not** hand-edit them), while the narrative sections you author —
`## When to use`, `## Flow`, `## Gotchas`, `## Key CLI`, `## See also` — are
preserved verbatim. Regenerate after every `skill.yaml` edit:

```bash
python scripts/generate_skill_md.py       skills/<domain>/<skill-name>
python scripts/generate_parameters_md.py  skills/<domain>/<skill-name>
```

**Required SKILL.md sections (lint-enforced at scripts/skill_lint.py):**
`## When to use`, `## Inputs & Outputs`, `## Flow`, `## Gotchas`,
`## Key CLI`, `## See also`. Body capped at 200 lines.
`scripts/validate_skill_yaml.py` and `scripts/skill_lint.py` gate both the
manifest and the generated card.

### Step 3: Implement the script

Your script needs three things: a `main()` CLI entry, demo mode support, and standard output files.

```python
#!/usr/bin/env python3
"""One-line description of the skill."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Project root on sys.path for imports
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Import core analysis functions from _lib (recommended for complex skills)
from skills.<domain>._lib.<module> import core_function

# Import report utilities
from omicsclaw.common.report import (
    generate_report_header,
    generate_report_footer,
    write_result_json,
)


def generate_figures(output_dir: Path, summary: dict) -> list[str]:
    """Create analysis visualizations."""
    ...


def write_report(output_dir: Path, summary: dict, input_file, params: dict) -> None:
    """Generate report.md + result.json."""
    ...


def get_demo_data():
    """Return synthetic demo data."""
    ...


def main():
    parser = argparse.ArgumentParser(description="...")
    parser.add_argument("--input", dest="input_path")
    parser.add_argument("--output", dest="output_dir", required=True)
    parser.add_argument("--demo", action="store_true")
    # Add flags declared in SKILL.md allowed_extra_flags
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.demo:
        data = get_demo_data()
    elif args.input_path:
        data = load_data(args.input_path)
    else:
        print("ERROR: Provide --input or --demo", file=sys.stderr)
        sys.exit(1)

    # Run analysis → generate output
    summary = core_function(data, ...)
    generate_figures(output_dir, summary)
    write_report(output_dir, summary, args.input_path, vars(args))


if __name__ == "__main__":
    main()
```

**Standard output files** (in `--output` directory; describe yours
exhaustively in `references/output_contract.md` — `scripts/skill_lint.py`
verifies every claimed path appears in the script):

| File | Purpose | Optional? |
|------|---------|---|
| `report.md` | Analysis report with methodology, results, disclaimer | always written |
| `result.json` | Standardised envelope (`summary` + `data`) for programmatic access | always written |
| `tables/<name>.csv` | CSV data tables | per skill |
| `figures/<name>.png` | PNG/SVG visualizations | only if your script uses matplotlib |
| `reproducibility/{commands.sh,requirements.txt,checksums.sha256}` | Replay artifacts | written by common report helper when applicable |
| `processed.h5ad` | Output AnnData | only if `interface.outputs.anndata.saves_h5ad: true` in `skill.yaml` |

### Step 4: (Recommended) Use `_lib` for core logic

For complex skills, put core analysis functions in a shared `_lib/` module:

```
skills/<domain>/_lib/
    ├── __init__.py
    └── your_module.py    # run_analysis(), compute_metrics(), ...
```

Then import at the top level of your script:

```python
from skills.<domain>._lib.your_module import run_analysis
```

**Why this matters:** The `skill_search()` tool (used by the research pipeline's coding-agent) performs AST scanning to discover callable functions. It specifically extracts functions imported from `_lib` and marks them as **core functions** (`▶`), displayed prominently to the coding-agent. Functions defined directly in your script are shown as helpers.

If your domain doesn't have `_lib` yet, that's fine — all functions defined in your script will still be discovered and shown to agents.

### Step 5: Write tests

```python
# tests/test_<skill_name>.py
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "<skill_name>.py"

def test_demo_mode(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--demo", "--output", str(tmp_path)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0
    assert (tmp_path / "report.md").exists()
    assert (tmp_path / "result.json").exists()
```

### Step 6: Verify integration

```bash
# 1. Registry discovers your skill
python omicsclaw.py list | grep <skill-name>

# 2. Demo mode works
python omicsclaw.py run <skill-name> --demo --output /tmp/test_output

# 3. Tests pass
python -m pytest skills/<domain>/<skill-name>/tests/ -v
```

### Step 7: Submit

```bash
git checkout -b add-<skill-name>
git add skills/<domain>/<skill-name>/
git commit -m "feat(<domain>): add <skill-name> skill"
git push -u origin add-<skill-name>
# Open PR on GitHub
```

---

## How Auto-Discovery Works

You don't need to register your skill anywhere. The system discovers it automatically:

```
You create files                          System does the rest
─────────────────                         ────────────────────
skills/<domain>/<name>/                 → registry.load_all() discovers the directory
    SKILL.md (with frontmatter)         → LazySkillMetadata parses trigger_keywords, domain, flags
    <name>.py (with main())             → registry.skills[alias] = {script, domain, description, ...}
                                          │
                                          ├→ CLI: `omicsclaw.py run <name>` works
                                          ├→ Bot: NLP routing via trigger_keywords
                                          ├→ skill_search(): AST extracts functions for coding-agent
                                          └→ load_skill(): dynamic import in notebook kernel
```

---

## Skill Guidelines

1. **Local-first**: All data processing happens locally. No mandatory cloud uploads.
2. **Reproducible**: Generate reports with version info and run commands.
3. **Single responsibility**: Each skill does one analysis task well.
4. **Documented**: SKILL.md with methodology, examples, and safety disclaimer.
5. **Standardized output**: Follow the output structure (report.md, result.json, figures/).
6. **Demo mode**: `--demo` must work without `--input` — essential for testing and user onboarding.

## Code Standards

- Python 3.11+
- Type hints encouraged
- Use `pathlib` for file paths
- No hardcoded absolute paths
- Tests with pytest
- Follow existing skill patterns (read 2-3 skills in the same domain before starting)

## Supported Domains

For an always-current count, see the auto-generated sections in
[`CLAUDE.md`](CLAUDE.md) (between `<!-- ROUTING-TABLE-START -->` markers)
and [`skills/orchestrator/SKILL.md`](skills/orchestrator/SKILL.md).

| Domain | Directory |
|--------|-----------|
| Spatial Transcriptomics | `skills/spatial/` |
| Single-Cell Omics | `skills/singlecell/` |
| Genomics | `skills/genomics/` |
| Proteomics | `skills/proteomics/` |
| Metabolomics | `skills/metabolomics/` |
| Bulk RNA-seq | `skills/bulkrna/` |

### Keeping skill-derived docs in sync

After adding, renaming, or removing a skill (or editing SKILL.md frontmatter
that appears in routing tables), regenerate the derived docs so humans and
LLMs see consistent numbers:

```bash
python scripts/sync_skill_docs.py --apply     # regenerate all four
python scripts/sync_skill_docs.py --check     # CI-style drift check
```

This wraps four generators:
- `generate_routing_table.py` → `CLAUDE.md` routing table (compact 7-domain briefing)
- `generate_orchestrator_counts.py` → `skills/orchestrator/SKILL.md`
- `generate_catalog.py` → `skills/catalog.json`
- `generate_domain_index.py` → `skills/<domain>/INDEX.md` (lazy-load detail)

The `docs-consistency` CI job runs `--check` on every PR and will fail
if any of these files are stale.

### Keeping skill `requires:` complete

A skill's `requires:` frontmatter must list every Python package its script
needs — including optional backends reached transitively through `_lib`
(e.g. `cellrank`/`palantir` for `spatial-trajectory`). These drift easily,
so they are **generated and checked**, not hand-maintained:

```bash
python scripts/audit_skill_requires.py            # report gaps
python scripts/audit_skill_requires.py --write    # regenerate frontmatter in place
make audit-requires FIX=1                          # same, via Make
```

The auditor statically analyses each script + the `_lib` modules it imports,
resolves the shared `_lib/viz` re-export **by imported symbol** (so a skill is
not charged for backends it never drives), and canonicalises optional-backend
names against each domain's `_lib/dependency_manager.py` `DEPENDENCY_REGISTRY`
(the single source of truth — see AGENTS.md). `--write` is **union-only**: it
adds missing deps but never drops a declared one (skills that delegate to
`omicsclaw.*` runtime hide their surface behind the package boundary).

**When you add an algorithm/backend to a skill:**
1. Register it in the domain's `DEPENDENCY_REGISTRY` with an `install_cmd`.
2. Add it to the right `pyproject.toml` extra or `environment.yml` Tier 4.
3. Run `python scripts/audit_skill_requires.py --write` to refresh frontmatter.

CI runs `audit_skill_requires.py --check` (also wired into `skill_lint.py`) and
**fails on any skill missing a real dependency**.

### Routing-context token budget

The bot's LLM-facing tool registry ships with every turn. To prevent slow
growth, the repo pins a ceiling per-metric:

```bash
python scripts/measure_routing_tokens.py                 # report sizes
python scripts/measure_routing_tokens.py --save X.json   # snapshot
python scripts/check_routing_budget.py                   # fail if over ceiling
```

CI runs `check_routing_budget.py`. If you add a new bot tool or expand an
existing tool's description, the check may fail — in that case:

1. Run `measure_routing_tokens.py` locally and eyeball the diff vs
   `build/routing-baselines/after_stage4.json`.
2. If the new cost is justified, raise the relevant ceiling in
   `build/routing-baselines/ceiling.json` and explain why in the PR.
3. If the growth is accidental (forgot to trim a description), fix it.

See `docs/` and the Stage 2-4 refactor comments in `omicsclaw/runtime/`
for the 3-layer routing architecture (domain briefing → per-domain index →
chosen-skill prefetch) that keeps this budget achievable.

## For AI Agents Contributing Skills

AI coding agents should follow the same workflow, plus:

1. Read [`README.md`](README.md) first for project context on complex repository tasks
2. Read [`SPEC.md`](SPEC.md) for the repository maintenance and AI development contract
3. Read [`AGENTS.md`](AGENTS.md) for project structure and conventions
4. Read the target skill's `SKILL.md` before modifying code
5. Use a concise plan, root-cause debugging, focused tests, and verification evidence for non-trivial repository changes.
6. Use `python omicsclaw.py list` to verify skills load correctly
7. Run `python -m pytest -v` to confirm all tests pass
8. Update `README.md` if the work introduces an important decision, milestone, or lasting contributor workflow change

## Skill Ideas We Need

**Spatial Transcriptomics:** 3D tissue reconstruction, multi-slice alignment

**Single-Cell:** Multi-modal integration (RNA + ATAC + protein), rare cell type detection

**Genomics:** Long-read variant calling, population genetics analysis

**Proteomics:** DIA-NN integration, PTM site prediction

**Metabolomics:** Compound identification, flux balance analysis

**Multi-Omics:** Cross-omics integration, multi-view factor analysis

## Questions?

Open an issue on [GitHub](https://github.com/TianGzlab/OmicsClaw/issues) or check the documentation.
