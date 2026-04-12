---
phase: 21
plan: 21-02
title: "Wave 2: Analysis skills SKILL.md sync (11 skills)"
subsystem: documentation
tags: [skill-md, cli-params, r-enhanced, method-docs]
dependency_graph:
  requires: []
  provides: [skill-md-analysis-skills-complete]
  affects: [sc-de, sc-markers, sc-enrichment, sc-pseudotime, sc-cell-annotation, sc-cell-communication, sc-velocity, sc-differential-abundance, sc-cytotrace, sc-pathway-scoring, sc-drug-response]
tech_stack:
  added: []
  patterns: [CLI-params-table, R-Enhanced-plots-table, method-fallback-docs]
key_files:
  created: []
  modified:
    - skills/singlecell/scrna/sc-de/SKILL.md
    - skills/singlecell/scrna/sc-markers/SKILL.md
    - skills/singlecell/scrna/sc-enrichment/SKILL.md
    - skills/singlecell/scrna/sc-pseudotime/SKILL.md
    - skills/singlecell/scrna/sc-cell-annotation/SKILL.md
    - skills/singlecell/scrna/sc-cell-communication/SKILL.md
    - skills/singlecell/scrna/sc-velocity/SKILL.md
    - skills/singlecell/scrna/sc-differential-abundance/SKILL.md
    - skills/singlecell/scrna/sc-cytotrace/SKILL.md
    - skills/singlecell/scrna/sc-pathway-scoring/SKILL.md
    - skills/singlecell/scrna/sc-drug-response/SKILL.md
decisions:
  - "Documented liana->builtin fallback in sc-cell-communication (discovered in E2E testing)"
  - "Documented cadrres->simple_correlation fallback in sc-drug-response"
  - "Noted deviation: sc-cytotrace R_ENHANCED_PLOTS uses embedding renderers, not plot_cytotrace_boxplot"
metrics:
  duration: "~25 minutes"
  completed: "2026-04-12T12:06:28Z"
  tasks_completed: 5
  files_modified: 11
---

# Phase 21 Plan 02: Analysis Skills SKILL.md Sync Summary

Synced SKILL.md documentation for all 11 analysis-focused scRNA skills. Each file now has complete `## CLI Parameters`, `## R Enhanced Plots`, and where applicable `## Methods` / `## Method Fallback Behavior` sections derived directly from each skill's Python argparse and `R_ENHANCED_PLOTS` dict.

## What Was Done

### Skills Updated

| Skill | CLI flags added | R renderers documented | Notes |
|-------|-----------------|------------------------|-------|
| sc-de | 16 | 5 (volcano, heatmap, violin, cor, manhattan) | ParamValidator coverage noted |
| sc-markers | 12 | 2 (heatmap, violin) | COSG `--mu` param included |
| sc-enrichment | 25 | 7 (5 shared + 2 GSEA-only) | GSEA-only renderers separated in table |
| sc-pseudotime | 25 | 6 (lineage, dynamic, heatmap, embedding×2, density) | All 6 methods covered in param table |
| sc-cell-annotation | 17 | 5 (embedding×2, barplot, proportion, sankey) | ggalluvial dependency noted for sankey |
| sc-cell-communication | 23 | 8 (heatmap, network, chord, bar, violin, scatter, bipartite, diff_network) | liana fallback documented |
| sc-velocity | 5 | 2 (stream, embedding_discrete) | `--method`/`--mode` alias noted |
| sc-differential-abundance | 16 | 2 (embedding, barplot) | `--n-permutations` for proportion_test_r added |
| sc-cytotrace | 6 | 2 (embedding×2) | Deviation noted (see below) |
| sc-pathway-scoring | 16 | 2 (embedding×2) | `aucell_py` method and `--seed` param added |
| sc-drug-response | 9 | 0 (none, `--r-enhanced` is a no-op) | Methods table with fallback behavior |

**Total: 357 lines inserted across 11 files.**

## Deviations from Plan

### Auto-discovered Issues

**1. [Rule 2 - Missing docs] sc-cytotrace R_ENHANCED_PLOTS mismatch with plan**
- **Found during:** Task documenting sc-cytotrace R renderers
- **Issue:** Plan listed `plot_feature_boxplot` as the 1 R renderer. Actual `R_ENHANCED_PLOTS` dict in `sc_cytotrace.py` contains `plot_embedding_discrete` and `plot_embedding_feature`. The `plot_cytotrace_boxplot` renderer exists in `registry.R` but is not wired into the skill's `R_ENHANCED_PLOTS` dict.
- **Fix:** Documented what actually exists in `R_ENHANCED_PLOTS`. Added a note explaining the discrepancy so it is visible to the team.
- **Files modified:** `skills/singlecell/scrna/sc-cytotrace/SKILL.md`

**2. [Rule 2 - Missing docs] sc-pathway-scoring has a third method `aucell_py`**
- **Found during:** Reading `sc_pathway_scoring.py` argparse
- **Issue:** Plan listed 2 methods (`aucell_r`, `score_genes_py`). The script has a third method `aucell_py` (pure Python AUCell, no R required) with its own `--aucell-py-auc-threshold` and `--seed` params.
- **Fix:** Documented all three methods and their params.
- **Files modified:** `skills/singlecell/scrna/sc-pathway-scoring/SKILL.md`

**3. [Rule 2 - Missing docs] sc-cell-annotation has 5 R renderers, not 3**
- **Found during:** Reading `sc_annotate.py` R_ENHANCED_PLOTS dict
- **Issue:** Plan listed 3 R renderers (embedding×2, heatmap). Actual dict has 5: `plot_embedding_discrete`, `plot_embedding_feature`, `plot_cell_barplot`, `plot_cell_proportion`, `plot_cell_sankey`.
- **Fix:** Documented all 5. Added ggalluvial dependency note for sankey.
- **Files modified:** `skills/singlecell/scrna/sc-cell-annotation/SKILL.md`

## Method Fallback Behaviors Documented

- **sc-cell-communication / liana**: falls back to `builtin` heuristic (5 L-R pairs only) if `liana` package not installed. Report records both requested and executed methods.
- **sc-drug-response / cadrres**: falls back to `simple_correlation` with a warning if model files are missing from `--model-dir`.

## Self-Check

### Files modified exist:
- skills/singlecell/scrna/sc-de/SKILL.md — present
- skills/singlecell/scrna/sc-markers/SKILL.md — present
- skills/singlecell/scrna/sc-enrichment/SKILL.md — present
- skills/singlecell/scrna/sc-pseudotime/SKILL.md — present
- skills/singlecell/scrna/sc-cell-annotation/SKILL.md — present
- skills/singlecell/scrna/sc-cell-communication/SKILL.md — present
- skills/singlecell/scrna/sc-velocity/SKILL.md — present
- skills/singlecell/scrna/sc-differential-abundance/SKILL.md — present
- skills/singlecell/scrna/sc-cytotrace/SKILL.md — present
- skills/singlecell/scrna/sc-pathway-scoring/SKILL.md — present
- skills/singlecell/scrna/sc-drug-response/SKILL.md — present

### Commit exists: fc7c257 — FOUND

## Self-Check: PASSED
