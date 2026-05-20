# `consensus-interpret` — incremental implementation plan

Strict TDD (red → green → refactor) by slice. Each slice ships **runnable
artifacts + green tests** before the next is started; no slice depends on
unreviewed work from a later slice.

> Status legend: ☐ not started · ◐ in progress · ☑ green · ✗ blocked

## Precondition slice — persist adata path in `plan.json`

**Why first**: `consensus-interpret` defaults `--adata` to the path
recorded in `plan.json`. Today `plan.json` records `run_id`, `operator`,
`members[]`, scoring config — but **not the adata path**. Without this
slice, every test of `consensus-interpret` has to pass `--adata`
explicitly, and the SKILL.md contract is undeliverable.

### ☐ Slice 0 — `plan.json` carries `input_path`

| Step | Action | Artifact |
|---|---|---|
| 0.1 | Red — add `test_run_typed_consensus_persists_input_path` in `tests/runtime/consensus/test_driver.py` | failing test |
| 0.2 | Green — `runtime/consensus/driver.py`: extend `plan_audit` write to include `input_path: str(input_path.resolve())` | `plan.json` schema v2 |
| 0.3 | Update both thin skills' `plan_audit` dict to include the field (or move the population into the driver) | `consensus_domains.py`, `sc_consensus_clustering.py` |
| 0.4 | Backward compat: `consensus-interpret` Slice 1 reader treats `input_path` as optional with a clear T1 error message if missing | — |
| 0.5 | Refactor — extract a small `_build_plan_audit(...)` helper inside the driver if logic duplicates | — |

**Acceptance**: existing 125 tests still pass; new test passes; `plan.json`
written by both thin skills contains an absolute, existing `input_path`.

---

## Skill slices (every slice = one PR)

### ☐ Slice 1 — `TypedRunBundle` reader (T1 preflight)

Reads `<typed_run_dir>` and assembles a frozen dataclass containing
everything `consensus-interpret` will consume downstream.

| Step | Action | Artifact |
|---|---|---|
| 1.1 | Red — `tests/skills/spatial/consensus-interpret/test_run_reader.py` covering: valid dir loads; missing `plan.json` → `TypedRunInvalidError`; adata at recorded path absent → `AdataMismatchError`; `observation` column ⊄ `adata.obs` → `AdataMismatchError` | failing tests |
| 1.2 | Green — `skills/spatial/consensus-interpret/_run_reader.py`: `TypedRunBundle(plan, consensus_labels, member_scores, nmi_matrix, adata_path)` + `load_typed_run(dir, adata_override=None)` | passing tests |
| 1.3 | Wire `--adata` override into the loader; precedence: CLI > plan.json | — |

**Exit code mapping**: 3 (TypedRunInvalid), 4 (AdataMismatch).

### ☐ Slice 2 — Marker DB loader

Reads bundled or user-provided TSV; lookup `gene → [(cell_type, weight,
source)]`.

| Step | Action | Artifact |
|---|---|---|
| 2.1 | Red — `tests/skills/spatial/consensus-interpret/test_marker_db.py`: brain bundled loads; unknown `--tissue` no `--markers` → `MarkerDBUnavailableError`; user `--markers` overrides bundled; malformed TSV row skipped with warning | failing tests |
| 2.2 | Green — `_marker_db.py:MarkerDB.load(tissue=None, override_path=None) -> MarkerDB`; `MarkerDB.candidates(gene) -> list[Candidate]` | passing tests |
| 2.3 | Validate schema headers; reject silently mis-shaped CSV | — |

**Exit code mapping**: 5 (MarkerDBUnavailable).

### ☐ Slice 3 — Inline per-cluster DE

Wraps `scanpy.tl.rank_genes_groups` with consensus labels as `groupby`;
returns a tidy DataFrame.

| Step | Action | Artifact |
|---|---|---|
| 3.1 | Red — `test_inline_de.py`: 3-cluster synthetic adata returns top-K marker DataFrame; cluster with < 3 cells flagged `de_unavailable` (T2 degrade signal) | failing tests |
| 3.2 | Green — `_de.py:per_cluster_de(adata, consensus_labels, top_k=20) -> pd.DataFrame` + `cluster_de_status: dict[int, str]` | passing tests |
| 3.3 | Write `de_per_cluster.csv` schema with `cluster, rank, gene, score, pval_adj, in_marker_db` (last column joined in slice 4) | — |

**Exit code mapping**: 8 (CoverageBelowThreshold) when > 50% clusters degrade.

### ☐ Slice 4 — Marker → cell-type pre-LLM ranking

Deterministic candidate generator the LLM will choose from. Crucial
because it bounds LLM hallucination at the input level.

| Step | Action | Artifact |
|---|---|---|
| 4.1 | Red — `test_celltype_candidates.py`: per cluster, candidates ranked by `Σ db.weight × 1/de_rank`; top-K returned; ties broken by alphabetical cell_type | failing tests |
| 4.2 | Green — `_candidates.py:rank_celltype_candidates(de_df, marker_db, top_k=5) -> dict[int, list[Candidate]]` | passing tests |

### ☐ Slice 5 — LLM grounded interpretation (γ + β) using `providers/chat_completion`

| Step | Action | Artifact |
|---|---|---|
| 5.1 | Red — `test_llm_prompts.py` with **recorded LLM fixtures** (do NOT call real LLM in default CI): prompt template embeds cluster context + DB candidates; LLM returns JSON conforming to schema | failing tests |
| 5.2 | Green — `_llm.py:annotate_cluster(cluster_ctx, candidates) -> ClusterAnnotation` + `synthesize_next_steps(all_annotations, nmi_matrix) -> list[NextStep]`; reuses `omicsclaw.providers.chat_completion.call_chat_completion` | passing tests |
| 5.3 | Prompt templates in `skills/spatial/consensus-interpret/prompts/{annotate.tmpl, next_steps.tmpl}` mirror `narrative/prompts/` structure | — |
| 5.4 | Schema validation: parse LLM response; if JSON malformed → 1 retry; if still malformed → `InvariantViolationError` | exit 7 |

**Exit code mapping**: 6 (LLMUnavailable), 7 (InvariantViolation).

### ☐ Slice 6 — Invariant enforcement (T3) + grep-tested

| Step | Action | Artifact |
|---|---|---|
| 6.1 | Red — `test_interpreted_invariants.py` (the three grep tests promised in ADR 0012): no cell_type claim without `evidence.markers`; no next_step without `evidence_refs`; banner exactly one of the two allowed values | failing tests |
| 6.2 | Green — `_invariants.py:enforce_invariants(interpreted_assignments) -> None` raises `InvariantViolationError` | passing tests |
| 6.3 | Banner enforcement lives **only** in `_report.py:format_interpreted_report` (mirror `runtime/consensus/report.py` discipline — single source) | — |

### ☐ Slice 7 — Report writers (`interpreted_report.md` + `interpreted_assignments.json` + `audit.json`)

| Step | Action | Artifact |
|---|---|---|
| 7.1 | Red — `test_report_writers.py`: report contains banner first, anchor links per cluster, audit footer cites typed run namespace; JSON conforms to schema_version "0.1" | failing tests |
| 7.2 | Green — `_report.py:format_interpreted_report(bundle, annotations, next_steps, audit) -> str` + `_artifacts.py:write_artifacts(...)` | passing tests |
| 7.3 | `contradiction_regions.csv` written from cross_method_nmi rows where pairwise NMI < 0.65 (configurable threshold) | — |

### ☐ Slice 8 — Thin CLI wrapper + failure semantics

| Step | Action | Artifact |
|---|---|---|
| 8.1 | Red — `tests/test_cli_smoke.py` (mirroring `consensus-domains/tests/test_cli_smoke.py`): full pipeline on stubbed LLM; exits 0; all 5 artifacts present | failing tests |
| 8.2 | Red — exit-code smoke tests: T1 missing plan.json → 3; missing adata → 4; unknown tissue no --markers → 5; LLM disabled but called → 6; invariant violation → 7; coverage floor → 8 | failing tests |
| 8.3 | Green — `consensus_interpret.py` CLI wrapper with full argparse + exit code mapping | passing tests |
| 8.4 | `--no-llm` degrade path: skip slices 4–6 LLM steps; banner switches to `[I-noLLM: ...]`; `interpreted_assignments.json` has empty `clusters[*].cell_type` and empty `next_steps[]` | passing tests |

### ☐ Slice 9 — Evaluation panel tests (ADR 0012)

| Step | Action | Default CI | Gated |
|---|---|---|---|
| 9.1 | `test_interpretation_faithfulness.py` — regex-tag verbatim citations; assert ratio == 1.00 on recorded LLM fixture | ✓ always | — |
| 9.2 | `test_marker_grounding_rate_stubbed.py` — recorded LLM output + recorded DE output → assert Jaccard ≥ 0.60 | ✓ always (stubbed) | `RUN_INTERPRET_LLM=1` for real |
| 9.3 | `test_interpret_self_consistency.py` — 3 LLM seeds on 8-cluster synthetic fixture; majority agreement ≥ 0.70 | — | `RUN_INTERPRET_CONSISTENCY=1` |
| 9.4 | `test_expert_concordance_dlpfc.py` — DLPFC 151673 typed run → consensus-interpret → cluster_to_layer map → ARI ≥ 0.45 | — | `RUN_INTERPRET_DLPFC=1` |

### ☐ Slice 10 — Agent-loop integration (β coverage)

Per ADR 0012 §"β does not duplicate orchestrator", surfacing
recommendations into the chat session:

| Step | Action | Artifact |
|---|---|---|
| 10.1 | `routing/llm_router.py` recognises `*/consensus_labels.tsv` in recent tool outputs as a hint to suggest `consensus-interpret` | — |
| 10.2 | When chat LLM calls `run_skill("consensus-interpret", ...)`, the resulting `interpreted_report.md` is streamed back as tool result; banner survives the streaming | — |
| 10.3 | `surfaces/cli/_session_command_support.py` adds shortcut `/interpret <typed_run_dir>` | — |

> Slice 10 is **post-scaffold**; can land after Slices 0–9 are green and
> the skill is shipped on its own. The skill must work standalone via CLI
> first.

---

## Test-count delta (target on completion of Slices 0–9)

| Suite | Pre | Post | Delta |
|---|---|---|---|
| `tests/runtime/consensus/` | 80 | 81 | +1 (Slice 0) |
| `tests/providers/` | 8 | 8 | — |
| `skills/spatial/consensus-domains/tests/` | 4 | 4 | — |
| `skills/singlecell/scrna/sc-consensus-clustering/tests/` | 4 | 4 | — |
| `skills/spatial/consensus-interpret/tests/` | 0 | **~45** | +45 |
| **Total** | **125 + 2 skipped** | **~171 + 4 skipped** | **+46 + 2 skipped** |

Two new skipped (gated): `test_interpret_self_consistency`,
`test_expert_concordance_dlpfc`.

---

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| LLM cost spikes from per-cluster annotate call | Slice 5 caches by `(cluster_top_markers, candidates)` hash; same input → same output |
| Marker DB drift breaks `marker_grounding_rate` on real-LLM CI | `data/markers/` is versioned; gated CI fixture is recorded against a specific DB version recorded in `audit.json` |
| Schema v2 `plan.json` breaks legacy typed runs | Slice 0 §0.4: `consensus-interpret` falls back to `--adata` CLI override with a clear T1 message when `input_path` absent |
| Bundled marker DB scaffold (this commit) is small | Acceptable for scaffold; full curation tracked separately; ADR 0012 §"Bundled marker DBs" lists target sizes for publish readiness |
| Slice 10 (agent surface) requires touching `routing/` | Defer to a separate PR after Slices 0–9 land; standalone CLI must work first per SKILL.md contract |

---

## Definition of Done (for the whole feature)

A `consensus-interpret` PR is mergeable when:

1. Slices 0–9 are green (Slice 10 may follow in a separate PR).
2. Default CI passes: faithfulness invariant + marker-grounding-stubbed +
   3 grep tests + CLI smoke + all exit-code smoke tests.
3. Gated CI runs (locally with env vars) all pass:
   `RUN_INTERPRET_LLM=1`, `RUN_INTERPRET_CONSISTENCY=1`,
   `RUN_INTERPRET_DLPFC=1`.
4. Documentation: ADR 0012 marked Accepted (not Proposed); CONTEXT.md
   updated; `docs/architecture/2026-05-18-current-architecture.md` §11
   gains §11.9 "Interpreted layer"; falsifiability table §11.5 grows by 4 rows.
5. `consensus-interpret` runs end-to-end on the Slide-seq mouse
   hippocampus subsample used in this conversation's verification run
   and produces a non-degenerate interpreted report.
