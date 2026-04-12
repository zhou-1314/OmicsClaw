---
phase: 19-scop-renderer
plan: 03
subsystem: r-enhanced-viz
tags: [registry, wiring, figure-data, r-enhanced]
dependency_graph:
  requires: [19-01, 19-02]
  provides: [all-8-renderers-wired, figure-data-gaps-fixed]
  affects: [sc-cell-communication, sc-differential-abundance, sc-cytotrace, sc-pseudotime, sc-cell-annotation, sc-batch-integration, sc-gene-programs, sc-de]
tech_stack:
  added: []
  patterns: [R_ENHANCED_PLOTS-dict-wiring, figure-data-csv-export]
key_files:
  created: []
  modified:
    - skills/singlecell/_lib/viz/r/registry.R
    - skills/singlecell/scrna/sc-cell-communication/sc_cell_communication.py
    - skills/singlecell/scrna/sc-differential-abundance/sc_differential_abundance.py
    - skills/singlecell/scrna/sc-cytotrace/sc_cytotrace.py
    - skills/singlecell/scrna/sc-pseudotime/sc_pseudotime.py
    - skills/singlecell/scrna/sc-cell-annotation/sc_annotate.py
    - skills/singlecell/scrna/sc-batch-integration/sc_integrate.py
    - skills/singlecell/scrna/sc-gene-programs/sc_gene_programs.py
    - skills/singlecell/scrna/sc-de/sc_de.py
decisions:
  - "No new dependencies needed -- all 4 new R files (density.R, sankey.R, correlation.R, cytotrace.R) already created in Wave 1"
  - "figure_data export for cytotrace uses X_umap > X_pca fallback for embedding coordinates"
metrics:
  duration: 206s
  completed: "2026-04-12T08:26:30Z"
  tasks_completed: 2
  tasks_total: 2
  files_modified: 9
---

# Phase 19 Plan 03: Registry Wiring + Figure Data Export Summary

Register 8 new R renderers in registry.R, wire R_ENHANCED_PLOTS in 7 Python skills, fix figure_data export gaps for proportion_test and cytotrace_embedding

## What Was Done

### Task 1: Update registry.R + fix figure_data export gaps (8e71153)

**registry.R updates:**
- Added 4 `source()` lines for new R files: density.R, sankey.R, correlation.R, cytotrace.R
- Registered 8 new renderers in R_PLOT_REGISTRY, bringing total from 22 to 30
- New entries: plot_cell_density, plot_proportion_test, plot_cell_sankey, plot_ccc_stat_bar, plot_ccc_stat_violin, plot_ccc_stat_scatter, plot_feature_cor, plot_cytotrace_boxplot

**sc-differential-abundance figure_data fix:**
- Added export of proportion_test_results.csv to figure_data/ directory (was only in tables/)
- Enables plot_proportion_test R renderer to find its input CSV

**sc-cytotrace figure_data fix:**
- Added new export block creating cytotrace_embedding.csv in figure_data/
- Merges embedding coordinates (X_umap or X_pca fallback) with cytotrace_score, cytotrace_potency, cytotrace_gene_count
- Includes cell_type column detection (cell_type > leiden > louvain > cluster)

### Task 2: Update R_ENHANCED_PLOTS in 7 Python skills (d2d8c4a)

Added new renderer entries to each skill's R_ENHANCED_PLOTS dict:

| Skill | New Renderers Added |
|-------|-------------------|
| sc-cell-communication | plot_ccc_stat_bar, plot_ccc_stat_violin, plot_ccc_stat_scatter |
| sc-differential-abundance | plot_proportion_test, plot_cell_density |
| sc-cytotrace | plot_cytotrace_boxplot, plot_cell_density |
| sc-pseudotime | plot_cell_density |
| sc-cell-annotation | plot_cell_sankey |
| sc-batch-integration | plot_cell_sankey |
| sc-gene-programs | plot_feature_cor |
| sc-de | plot_feature_cor |

No changes to _render_r_enhanced() functions -- the existing loop over R_ENHANCED_PLOTS dict handles new entries automatically.

## Deviations from Plan

None -- plan executed exactly as written.

## Verification Results

1. registry.R has 30 total renderers (22 existing + 8 new) -- PASS
2. All 8 new renderer names found in R_PLOT_REGISTRY -- PASS
3. 4 new source() lines present (density.R, sankey.R, correlation.R, cytotrace.R) -- PASS
4. sc-differential-abundance exports proportion_test_results.csv to figure_data/ -- PASS
5. sc-cytotrace exports cytotrace_embedding.csv to figure_data/ -- PASS
6. All 7 Python skills have correct new R_ENHANCED_PLOTS entries -- PASS

## Self-Check: PASSED

All 9 modified files exist. Both task commits (8e71153, d2d8c4a) verified in git log. SUMMARY.md created.
