# Skill v1→v2 in-place migration runbook (ADR 0037)

Validated on the first migration (`spatial-preprocess`, Codex-reviewed). Follow it
for every skill/batch so all 95 skills migrate identically and verifiably.

## Safety invariant — keep v1 files during transition

`migrate_to_skill_yaml.py --in-place` **only adds `skill.yaml`; it never deletes the
v1 files.** Every dual-track reader prefers `skill.yaml` (v2) and ignores
`parameters.yaml`, so migrating causes **zero functional regression**.

**`parameters.yaml` is then redundant and SHOULD BE DELETED** (ADR 0037 §Decision 1:
"do not keep both — that would just be two machine contracts") — with ONE exception:
the **consensus member skills** read `param_hints` *directly* from `parameters.yaml`
(not via `skill.yaml`), in `omicsclaw/runtime/consensus/sources.py`+`plan.py`. Those
are the only v1-only direct readers of a per-skill `parameters.yaml`. Audited list:

| Keep `parameters.yaml` (consensus members) | Delete `parameters.yaml` on migration |
|---|---|
| `spatial/spatial-domains`, `singlecell/scrna/sc-clustering`, `singlecell/scrna/sc-integrate-cluster`, `singlecell/scrna/sc-pseudotime` | **every other skill** |

Keep the 4 consensus members' `parameters.yaml` until `consensus/sources.py`+`plan.py`
are migrated to read `param_hints` via the registry / `skill.yaml.interface.parameters.hints`
(a §4.2 runtime face). For all other skills, `rm parameters.yaml` is part of step 2.
`SKILL.md` stays — it is the (now generated) narrative card.

## Batch granularity

Migrate **per domain**. The Skip-when eval snapshot is domain-keyed and the
catalog/INDEX regenerate globally, so a whole-domain batch amortizes the derived-artifact
regen and gives one focused Codex review.

## Per-skill / per-batch procedure

```bash
SK=skills/<domain>/<skill>          # or loop the domain
D=<domain>

# 1. write skill.yaml (keeps v1 files; auto-fills interface from output_contract.md
#    + the I&O table via omicsclaw.skill.interface_extract), then validate
python scripts/migrate_to_skill_yaml.py --skill $SK --in-place
python scripts/migrate_to_skill_yaml.py --skill $SK --in-place --validate-only   # ✅ valid
#    curate the 2 fields with no reliable v1 source if a consumer needs them:
#    interface.inputs.modalities is auto-filled from tags ∩ platform vocab;
#    interface.outputs.result_json.required_keys is left empty (forward field).

# 2. regenerate derived artifacts
python scripts/sync_skill_version.py $SK              # script SKILL_VERSION -> skill.yaml.version
                                                      # (frontmatter is authoritative; ~66/90 skills drift)
python scripts/generate_skill_md.py $SK               # narrative SKILL.md: generated header
                                                      # + generated I/O summary; narrative preserved
rm $SK/parameters.yaml                                # single machine contract (ADR §Decision 1)
                                                      # — SKIP for the 4 consensus members (see above)
python scripts/generate_parameters_md.py $SK          # v2 header, body unchanged
python scripts/generate_catalog.py --apply            # description (re)derived from summary
python scripts/extract_skip_when_cases.py --domain $D --stub --output tests/eval/skip_when_cases.json
python scripts/generate_domain_index.py --apply       # then `git checkout` any OTHER domain's
                                                      # INDEX.md it regenerated (pre-existing drift)

# 3. gates — all must pass (except the pre-existing singlecell/INDEX.md drift)
python scripts/skill_lint.py $SK                       # clean
python scripts/generate_skill_md.py --all --check      # exit 0 (SKILL.md regenerated)
python scripts/generate_parameters_md.py --all --check # exit 0
python scripts/audit_skill_requires.py --check         # exit 0
python scripts/generate_catalog.py --check             # exit 0
python -m pytest tests/test_routing_skip_when.py -q    # pass
python omicsclaw.py list | tail -1                     # 95 skills across 8 domains
```

## Review the migrator's warnings (every skill)

`migrate_to_skill_yaml.py` prints warnings — resolve each before accepting:

- **`install:` lists a package not in `deps.python`** — VERIFY it is not actually
  imported (e.g. `squidpy` in spatial-preprocess was only a version-report string, not an
  `import`). `grep -rn "import <pkg>"` the script + `_lib/`. Decorative → drop is correct;
  real import → the v1 `requires:` was wrong (fix upstream).
- **`skip_when: heuristic parse from prose`** — read the generated `summary.skip_when`
  against the v1 description; confirm conditions + `use:` targets are accurate.
- **`interface` detail** — now AUTO-FILLED by `omicsclaw.skill.interface_extract`:
  `outputs.files` ← `references/output_contract.md` (authoritative, non-drifting);
  `outputs.anndata.{obs,obsm,var}` + `inputs.preconditions.data_shape.obsm` ←
  the `## Inputs & Outputs` table; `inputs.file_types` ← the input table;
  `inputs.modalities` ← tags ∩ platform vocab. Spot-check these against the skill's
  reality. Only `outputs.result_json.required_keys` is left empty (forward field, no
  consumer) — curate if a downstream consumer appears.

## Codex review checklist (mandatory each batch)

Run via the stdin recipe (gpt-5.5 / xhigh, read-only). Have Codex check:
1. **Fidelity** — no v1 field lost/distorted that a consumer uses (trigger_keywords,
   aliases, allowed_extra_flags, hints, saves_h5ad, requires_preprocessed,
   compatibility.platforms ← v1 `parameters.yaml os:`, resources).
2. **`SKILL_VERSION` drift** — the script often hardcodes `SKILL_VERSION` separate from
   the frontmatter `version`. Confirm `<script>.SKILL_VERSION == skill.yaml.version` (it
   is emitted into `result.json` + the viz contract). spatial-preprocess had `0.5.0` vs
   `0.6.0` — synced to `0.6.0`. **Check this for every skill.**
3. **Consistency** — catalog.json / INDEX.md / parameters.md / skip_when hash all reflect
   `skill.yaml.summary`; no other repo artifact still shows the old v1 description.
4. **squidpy-style decorative deps** dropped correctly; `deps.python` == AST import surface.

Apply must-fixes, re-run the gates, then proceed to the next batch.

## Expected, reviewable diffs per migrated skill

- `+ skill.yaml` (with a populated `interface`)
- `SKILL.md` — frontmatter becomes the generated header (description re-derived from
  `summary`); the hand-written `## Inputs & Outputs` table is replaced by a generated
  read-only summary; the narrative (When to use / Flow / Gotchas / Key CLI / See also)
  is preserved verbatim. **Human-review the I/O summary + the description.**
- `references/parameters.md` — header line only (`parameters.yaml` → `skill.yaml`)
- `catalog.json` + `<domain>/INDEX.md` — description re-derived from `summary` ("Load
  when…/Skip when…"); usually punctuation-level (skip clauses joined with `; `). **Human-review.**
- `skip_when_cases.json` — `description_hash` for the migrated skill(s).
- occasionally the entry script — `SKILL_VERSION` sync (see Codex check #2).
```

## Deferred cleanup — `output_contract.md` accuracy (Bucket A)

The interface extractor faithfully propagates `references/output_contract.md`,
but that file is itself inaccurate for several skills (it is auto-generated by
scraping the script's string literals and loses the *write directory*). Known
pre-existing inaccuracies surfaced by the spatial fidelity review (in a
non-runtime-consumed field, `interface.outputs.files`):

- **`figure_data/` mislabelled as `tables/`** — spatial-annotate, spatial-cnv,
  spatial-communication, spatial-de, spatial-genes, spatial-register,
  spatial-statistics (4–5 CSVs each written to `figure_data/`, listed under `tables/`).
- **R-adapter temp files listed as outputs** — spatial-cnv
  (`numbat_input.h5ad`, `tables/allele_counts.csv`, `tables/numbat_results.csv`).
- **figures listed for a wrapper that writes none** — spatial-raw-processing
  (its Gotcha says "No tables/figures are written"; output_contract lists 6 figures).

Further sub-types surfaced by the singlecell/genomics/proteomics/metabolomics/bulkrna
reviews (same root cause — the auto-generator scrapes string literals without the
write-dir or `--demo`/tempfile context):

- **`reproducibility/commands.sh` write-dir lost** — listed at the output-tree top
  level (or omitted) instead of under `reproducibility/`. SYSTEMIC across proteomics
  (de/identification/ms-qc/quantification), metabolomics (normalization/statistics/xcms),
  bulkrna (de/deconvolution/enrichment/read-qc/splicing, …). NOTE: `skill.yaml` is
  already correct here — `commands.sh` is in `interface_extract._FRAMEWORK_SIDECARS`
  and never enters `outputs.files`; only `output_contract.md` is wrong.
- **R-subprocess `tempfile.TemporaryDirectory` files over-listed as `tables/` outputs**
  — bulkrna-batch-correction (`counts.csv`/`batch_info.csv`/`corrected_counts.csv`),
  bulkrna-coexpression (`counts.csv`/`gene_modules.csv`/`soft_power_table.csv`/`wgcna_info.json`),
  bulkrna-de (`counts.csv`/`deseq2_results.csv`), bulkrna-survival (`clinical.csv`/`expr.csv`/`km_data.csv`).
  These reach `outputs.files` (unlike commands.sh) and SHOULD be pruned by the cleanup.
- **real produced figures UNDER-listed** (templated / loop-written names the scraper
  misses) — bulkrna-survival `figures/km_<gene>.png` (per-gene KM curves, the primary
  output), bulkrna-batch-correction `figures/pca_before_correction.png` + `pca_after_correction.png`,
  sc-pseudotime `figures/pseudotime_embedding.png` + `figures/pseudotime_distribution_by_group.png`
  (both ALWAYS produced — the skill's primary visual deliverable — yet `outputs.files` lists only
  the conditional `--r-enhanced` / `monocle3_r` figures).
- **demo INPUT misclassified as an output** — bulkrna-deconvolution `demo_bulkrna_counts.csv`
  (read from `examples/`, not produced); genomics-vcf-operations `demo.vcf` was the same
  class but was hand-corrected during migration (single obvious case).

Fix by hardening the `output_contract.md` generator to resolve each output
file's write-directory via AST (`<dir_var> / "name"`), skip `tempfile.TemporaryDirectory`
writes, resolve loop/templated filenames, and classify `--demo`-only reads as inputs;
regenerate all `output_contract.md`, then re-migrate. Tracked separately from the v1→v2
structural migration.
