---
phase: 19-scop-renderer
plan: 01
subsystem: viz/r
tags: [r-enhanced, density, sankey, proportion-test, ggridges, ggalluvial]
dependency_graph:
  requires: [common.R, stat.R]
  provides: [density.R, sankey.R, plot_proportion_test]
  affects: [registry.R (Plan 03)]
tech_stack:
  added: [ggalluvial]
  patterns: [geom_density_ridges, geom_alluvium, geom_pointrange]
key_files:
  created:
    - skills/singlecell/_lib/viz/r/density.R
    - skills/singlecell/_lib/viz/r/sankey.R
  modified:
    - skills/singlecell/_lib/viz/r/stat.R
decisions:
  - "Used requireNamespace guard for ggridges/ggalluvial with graceful fallback"
  - "Did NOT modify registry.R -- deferred to Plan 03 per wave conflict avoidance"
  - "Dynamic plot sizing based on number of groups/clusters"
metrics:
  duration: 272s
  completed: 2026-04-12
  tasks: 2
  files: 3
---

# Phase 19 Plan 01: Density, Sankey, and Proportion Test Renderers Summary

Three R Enhanced renderers using ggridges ridgeline, ggalluvial alluvial, and geom_pointrange with FDR significance coloring

## What Was Done

### Task 1: Install ggalluvial + create density.R and sankey.R (007b549)

**density.R** -- New file with `plot_cell_density` renderer:
- Reads pseudotime_points.csv or annotation_embedding_points.csv
- Auto-detects feature column (pseudotime, cytotrace_score, dpt_pseudotime, velocity_pseudotime, dim1)
- Auto-detects group column (cell_type, group, cluster, leiden, louvain)
- Uses ggridges::geom_density_ridges for ridgeline layout (falls back to geom_density if ggridges missing)
- Dynamic height: max(4, n_groups * 0.6 + 1.5) inches
- Supports flip parameter for coord_flip

**sankey.R** -- New file with `plot_cell_sankey` renderer:
- Reads cell_type_transitions.csv or annotation_embedding_points.csv
- Auto-detects left column (cluster, leiden, louvain, batch, sample) and right column (cell_type, annotation, group)
- Special handling for "from"/"to" columns in transitions CSV
- Uses ggalluvial::geom_alluvium + geom_stratum for alluvial diagram
- Falls back to stacked bar chart if ggalluvial not installed
- Dynamic sizing based on number of strata

**ggalluvial** installed via CRAN to user R library.

### Task 2: Extend stat.R with plot_proportion_test (0918299)

**plot_proportion_test** appended to stat.R:
- Reads proportion_test_results.csv from figure_data/
- Required columns: clusters, obs_log2FD, boot_CI_2.5, boot_CI_97.5, FDR
- Computes significance: FDR < threshold AND abs(log2FD) > log2(fold_threshold)
- geom_pointrange with color by significance (red = Significant, grey = n.s.)
- Dashed threshold lines at +/- log2(fold_threshold), solid line at 0
- coord_flip for horizontal orientation
- Auto-detects comparison/group info for subtitle
- Dynamic height based on number of clusters

**registry.R NOT modified** -- Plan 03 (Wave 2) will add all source() lines and registry entries to avoid Wave 1 merge conflicts with Plan 02.

## Deviations from Plan

None -- plan executed exactly as written.

## Decisions Made

1. **requireNamespace guard pattern**: Both density.R (ggridges) and sankey.R (ggalluvial) use requireNamespace() checks with graceful fallbacks, matching the project convention from common.R
2. **Registry deferred to Plan 03**: Following the plan's revised Step B, registry.R updates are deferred to avoid Wave 1 file conflicts between Plan 01 and Plan 02

## Smoke Test Results

All 3 renderers tested with mock CSV data -- PNG output verified visually:
- density: ridgeline plot with 4 cell types along pseudotime
- sankey: alluvial diagram mapping 5 clusters to 5 cell types
- proportion_test: pointrange with FDR significance coloring, threshold lines, comparison subtitle

## Self-Check: PASSED

All files exist, all functions load without error, all commits verified, all smoke tests produce valid PNG output.
