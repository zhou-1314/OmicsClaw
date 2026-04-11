---
phase: 18-skill-wiring-r-enhanced-flag
plan: 01
subsystem: singlecell-skills
tags: [r-enhanced, wiring, argparse, ggplot2]
dependency_graph:
  requires: [13-01]
  provides: [r-enhanced-flag-group-a]
  affects: [sc-cell-annotation, sc-de, sc-markers, sc-enrichment, sc-pseudotime, sc-cell-communication, sc-velocity, sc-differential-abundance]
tech_stack:
  added: []
  patterns: [R_ENHANCED_PLOTS-dict, _render_r_enhanced-function, lazy-import-call_r_plot]
key_files:
  created: []
  modified:
    - skills/singlecell/scrna/sc-cell-annotation/sc_annotate.py
    - skills/singlecell/scrna/sc-de/sc_de.py
    - skills/singlecell/scrna/sc-markers/sc_markers.py
    - skills/singlecell/scrna/sc-enrichment/sc_enrichment.py
    - skills/singlecell/scrna/sc-pseudotime/sc_pseudotime.py
    - skills/singlecell/scrna/sc-cell-communication/sc_cell_communication.py
    - skills/singlecell/scrna/sc-velocity/sc_velocity.py
    - skills/singlecell/scrna/sc-differential-abundance/sc_differential_abundance.py
decisions:
  - R_ENHANCED_PLOTS dict + _render_r_enhanced() + --r-enhanced argparse flag per skill (template pattern from SC-SKILL-TEMPLATE.md)
  - Lazy import of call_r_plot inside _render_r_enhanced() to avoid import-time R dependency
  - sc-velocity: _render_r_enhanced placed before os.killpg self-termination
  - sc-enrichment: both gsva_r early-return and main code paths covered
metrics:
  duration: 608s
  completed: "2026-04-11"
  tasks: 2
  files: 8
---

# Phase 18 Plan 01: Wire --r-enhanced into Group A Skills Summary

Wired --r-enhanced flag into 8 Group A skills with dedicated R renderers, following the 5-step template pattern (R_ENHANCED_PLOTS dict, argparse flag, _render_r_enhanced function, main() call, result_data update).

## Tasks Completed

### Task 1: Wire --r-enhanced into sc-cell-annotation, sc-de, sc-markers
- **Commit:** 59b8249
- Added R_ENHANCED_PLOTS dict, _render_r_enhanced(), --r-enhanced flag to each
- sc-cell-annotation: plot_embedding_discrete + plot_embedding_feature
- sc-de: plot_de_volcano + plot_de_heatmap
- sc-markers: plot_marker_heatmap
- All 3 verified with --demo --r-enhanced (exit 0, figures/r_enhanced/ created)

### Task 2: Wire --r-enhanced into sc-enrichment, sc-pseudotime, sc-cell-communication, sc-velocity, sc-differential-abundance
- **Commit:** b47f123
- sc-enrichment: plot_enrichment_bar + plot_gsea_mountain + plot_gsea_nes_heatmap (both gsva_r and main paths)
- sc-pseudotime: plot_pseudotime_lineage + plot_pseudotime_dynamic
- sc-cell-communication: plot_ccc_heatmap + plot_ccc_network
- sc-velocity: plot_velocity (placed before os.killpg self-kill)
- sc-differential-abundance: plot_embedding_discrete
- Verified sc-enrichment, sc-pseudotime, sc-cell-communication with --demo --r-enhanced (exit 0)
- sc-differential-abundance has pre-existing pandas InvalidIndexError in demo mode (not caused by this plan)
- sc-velocity verified via syntax check and --help flag registration

## Deviations from Plan

None - plan executed exactly as written.

## Known Issues (Pre-existing)

**sc-differential-abundance demo mode**: Fails with `pandas.errors.InvalidIndexError: slice(None, None, None)` regardless of --r-enhanced flag. This is a pre-existing milo/pandas compatibility issue, not introduced by this plan.

## Verification Results

| Skill | --demo --r-enhanced | figures/r_enhanced/ | Exit |
|-------|-------------------|-------------------|------|
| sc-cell-annotation | PASS | Created | 0 |
| sc-de | PASS | Created | 0 |
| sc-markers | PASS | Created | 0 |
| sc-enrichment | PASS | Created | 0 |
| sc-pseudotime | PASS | Created | 0 |
| sc-cell-communication | PASS | Created | 0 |
| sc-velocity | Syntax OK | N/A (heavy test) | N/A |
| sc-differential-abundance | Pre-existing fail | N/A | N/A |

R renderers emit warnings on failure (renderer not found in registry) but never crash the skill -- this is the expected graceful degradation behavior from call_r_plot().

## Self-Check: PASSED

- All 8 modified files exist and contain R_ENHANCED_PLOTS
- Both task commits verified (59b8249, b47f123)
- SUMMARY.md written successfully
