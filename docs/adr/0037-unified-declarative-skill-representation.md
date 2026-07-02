# Unified declarative skill representation (`skill.yaml` v2)

## Status

**Proposed / Draft (2026-06-30).** Co-designed with the maintainer and
cross-validated by Codex (gpt-5.5, xhigh) against the live tree. Extends
[ADR 0030](0030-first-class-skill-type-system.md) (first-class skill types) and
supersedes the *frontmatter + `parameters.yaml` sidecar* split it inherited.
Companion to the lifecycle redesign proposal
[`docs/proposals/skill-lifecycle-redesign.md`](../proposals/skill-lifecycle-redesign.md)
(§1 「技能表示」). **Pilot implemented (2026-06-30):** `omicsclaw/skill/schema.py`
(pydantic v2 models = the single parser), `scripts/migrate_to_skill_yaml.py`
(v1 → `skill.yaml` scaffold), and `tests/test_skill_schema.py` (10 green). The
**spatial domain (19/19 skills)** migrates and validates clean into
`build/skillyaml_pilot/` (non-destructive; v1 files untouched). **v2 wiring landed
(dual-track):** `lazy_metadata.LazySkillMetadata` prefers `skill.yaml` behind its
existing property surface (so `registry.py` and all consumers get v2 transparently),
falling back to v1 when absent or invalid; discovery + `load_lightweight` accept a
`skill.yaml`-only dir; `scripts/validate_skill_yaml.py --check` is a CI gate;
`tests/test_registry_v2.py` covers v2-read / v2-wins / invalid-v2-fallback / registry
load. The one-way generators are now landed — catalog/INDEX/routing (dual-track) +
`references/parameters.md` (`generate_parameters_md.py`) + the **narrative SKILL.md
header & I/O summary** (`scripts/generate_skill_md.py` + `omicsclaw/skill/skill_md.py`),
fed by `interface` auto-population (`omicsclaw/skill/interface_extract.py`); all
`--check`-gated in CI. Still pending: the 3-layer `result_json.required_keys` check,
R/bash dep semantics, and completing per-domain in-place rollout.

## Context

OmicsClaw's skill is a **compound object**: an LLM-routable methodology card +
a local deterministic script + a bioinformatics output contract + a safety
boundary. The current representation has outgrown the shape ADR 0030 assumed:

1. **No single source of truth.** One fact (e.g. the flag set) is written in up
   to five places — `SKILL.md` frontmatter, `parameters.yaml`, the script
   `argparse`, `references/parameters.md`, `skills/catalog.json` — kept in sync
   only by tooling.
2. **Schema lives as hand-coded constants**, independently re-parsed by
   `scripts/skill_lint.py`, `omicsclaw/skill/lazy_metadata.py`,
   `scripts/generate_catalog.py`, and `omicsclaw/skill/execution/dep_spec.py`.
3. **M/R/C is only a convention**, not carried by structure; the survey's
   `S=(M,R,C)` model (arXiv:2605.07358) has no explicit home.
4. **The body is prose**, so routing / context-injection / drift-validation can
   only consume it best-effort (8000-char truncation; `Skip-when` as a bag of
   words).
5. **The form is not uniform.** Beyond `leaf`/`workflow`, the maintainer intends
   to integrate `knowledge_base/` — **28 workflows mined from other repos**
   (R/Python, "YAML metadata + execution guide" shape, **not** in the
   registry/catalog) — into the main library. There is no single target shape
   these heterogeneous skills can map into without loss.

The `frontmatter ↔ parameters.yaml` split is historical, not designed, and even
produced the documented `requires:` double-meaning (pip list vs
`{bins,env,config}`).

## Decision

Adopt a **single declarative machine contract per skill, `skill.yaml`**, as the
sole source of truth, with `SKILL.md` demoted to a **pure narrative methodology
card**. Concretely:

1. **`parameters.yaml` is renamed/upgraded to `skill.yaml`** (do **not** keep
   both — that would just be two machine contracts). `skill.yaml` carries
   identity + conditions(C) + interface(inputs/parameters/outputs) +
   runtime(execution part of M) + deps + resources(R) + lifecycle + validation +
   provenance + security + (optional) mcp.
2. **One declarative schema, `omicsclaw/skill/schema.py` (pydantic), is the only
   parser.** `skill_lint.py`, `lazy_metadata.py`, `generate_catalog.py`, and
   `dep_spec.py` all validate/read through it (replacing their four private
   parsers).
3. **Generation is one-way:** `skill.yaml` → `catalog.json`,
   `references/parameters.md`, the CLAUDE.md routing table, per-domain
   `INDEX.md`, and the generated header of `SKILL.md`. All derived artifacts are
   `--check`-gated in CI.
4. **Hard boundary:** `SKILL.md` (v2) is narrative only — `When to use` (prose) /
   `Flow` / `Gotchas` (still file:line anchor-verified) / `Key CLI` / `See also`.
   The **facts** (inputs, outputs, skip conditions) live in `skill.yaml`; the
   former hand-written `Inputs & Outputs` section is **removed or replaced by a
   generated read-only summary** (otherwise the output contract keeps drifting).
5. **`S=(M,R,C)` is explicit:** M = `SKILL.md` narrative + `runtime.entry`
   script; R = `resources.*`; C = `summary` + `interface.inputs` (incl.
   `preconditions`).
6. **Heterogeneous skills are instances of one schema:** `type ∈ {leaf,
   workflow}` × `runtime.language ∈ {python, r, bash}`. The migrated `knowledge_base/`
   workflows become ordinary `leaf`/`workflow` skills with
   `runtime.language: r|python|bash` — no parallel skill type.
7. **`knowledge`/`adapter` types are NOT exposed in the v2 schema.** Deprecation
   is expressed via `lifecycle.status: deprecated` + `superseded_by`, not via a
   parallel agent-facing type. (Resolves the empty-slot ambiguity from ADR 0030
   and matches the maintainer's intent that `knowledge_base/` be *migrated in*,
   not embedded as a type.)
8. **`schema_version` enables coexistence:** `skill.yaml` present ⇒ v2 (sole
   truth); absent ⇒ v1 fallback (current frontmatter + `parameters.yaml`).
   `registry`/`lazy_metadata` prefer v2; CI runs both tracks; stock skills
   migrate per-domain.

### Target `skill.yaml` field tree

```yaml
schema_version: 2
id: spatial-preprocess
name: spatial-preprocess
domain: spatial                 # 8 domains (7 analysis/orchestration + literature)
type: leaf                      # leaf | workflow
version: 0.6.0
author: OmicsClaw               # preserved from v1 frontmatter (lossless)
license: MIT                    # preserved from v1 frontmatter (lossless)
emoji: 🔬                       # optional, cosmetic (preserved from parameters.yaml)

summary:                        # C — applicability (LLM-facing, machine-parsed)
  load_when: "..."
  skip_when:                    # structured but human-readable (Codex must-fix #4)
    - {condition: raw_fastq_input, use: spatial-raw-processing,
       rationale: matrix conversion needed first}
  trigger_keywords: [...]       # routing recall signal (consumed by capability_resolver)
  tags: [...]
  aliases: [...]                # was legacy_aliases

interface:
  inputs:                       # C — preconditions feed the generic preflight (§4)
    modalities: [...]
    file_types: [h5ad]
    preconditions:              # 3 kinds — env/config relocated here from deps (they are NOT installs)
      data_shape: {requires_preprocessed: false, obs: [], obsm: [spatial]}  # AnnData/data preconditions
      env: []                   # required env vars (provider/channel/LLM key) — checked, never installed
      config: []                # required config state (MCP server / workspace / token) — checked, never installed
  parameters:
    allowed_extra_flags: [...]  # name kept (Codex must-fix #1); --input/--output/--demo reserved
    hints: {...}
  outputs:                      # output contract — 3-layer check (Codex must-fix #3)
    files: [...]
    result_json: {required_keys: [...]}   # schema-declared + lint static + demo/fixture dynamic
    anndata: {saves_h5ad: true, obs: [...], obsm: [...], var: [...]}

runtime:                        # M (execution part)
  language: python              # python | r | bash
  entry: spatial_preprocess.py

deps:                           # ONLY external deps the resolver/probe/provision system handles distinctly
  python: []                    # the ONLY auto-provisioned channel; flat names; record import_name vs package
  # pip vs conda vs non-pip vs deny is DERIVED centrally by dep_spec.kind_of() — NOT a declared bucket.
  # Future extensions — add ONLY with a real consumer ("no consumer, no bucket"):
  #   r:    structured per-skill R deps for migrated R skills (shape below); R_TIER_PACKAGES becomes a derived view
  #   cli:  real external binaries (samtools/bcftools/cellranger) gated by a PATH-check consumer; NEVER python3
  #         (NB: "bash" is a runtime.language, not a dep bucket — the bucket lists external programs, not the shell)

compatibility:                  # was deps.os — a marker, not an install target (host platform derived at provision)
  platforms: [linux, macos]
  architectures: [x86_64, arm64]

resources:                      # R
  references: [methodology.md, output_contract.md]   # parameters.md still generated
  figures: r_visualization/
  demo: examples/...
  tests: tests/

lifecycle:
  status: stable                # draft | mvp | stable | deprecated
  superseded_by: null

validation:                     # enum unchanged from ADR 0030 (Codex must-fix #5)
  level: smoke-only             # smoke-only | demo-validated | fixture-validated | benchmarked | production
  evidence: [...]               # earned from CI signals

provenance:                     # defaults allowed — don't burden every author
  origin: human                 # human | scaffolded | promoted | migrated
  migrated_from: null           # knowledge_base/<topic> when migrated
  source_hash: null
  source_license: null

security:                       # makes the iron rules schema-enforceable + auditable
  data_egress: none             # none | optional
  network: none                 # none | optional  (string enum, not bool — clean YAML)
  writes: output_dir_only       # output_dir_only | workspace | unrestricted

mcp:                            # optional, GENERATED only — never a hand-written 2nd source
  expose: false
  # tool_name / input_schema_strategy when expose: true
```

### `deps` granularity (evidence-reviewed 2026-06-30, Codex-validated)

An earlier draft split `deps` into seven buckets
(`python/conda/r/bins/env/config/os`). A code-level audit (2-agent workflow) +
Codex (gpt-5.5, xhigh) review found that **over-models the surface and conflates
three concepts** — so it is collapsed:

- **`deps.python` is the only consumed install bucket.** 95/95 skills populate
  the flat `requires:` name list; it alone is probed and auto-provisioned
  (`lazy_metadata → dep_spec.required_packages → env_resolver.resolve_skill_runtime
  → venv_provision`).
- **`conda` is removed — it is a *derived* label, not a declaration.** pip vs
  conda vs non-pip vs deny is decided centrally by `dep_spec.kind_of()`
  (`_CONDA_PREFERRED` + `DEPENDENCY_REGISTRY` install_cmd regex), pinned by
  `tests/test_dep_spec.py`. A per-skill `conda:` field would create a second
  classification source of truth and break the "flat name list" contract.
- **`env`/`config` are runtime *preconditions*, not installs** → moved to
  `interface.inputs.preconditions` (3 kinds: data-shape / env-vars /
  config-state). They were always empty (`[]`) under the old `requires:` block
  and nothing installs them.
- **`os` is a compatibility *marker*, not an install target** → moved to
  top-level `compatibility:` (`platforms` / `architectures`). Real platform is
  derived from the host (`venv_provision._basis`), never from the declared list.
- **`r` and `cli` are documented future extensions, added only with a real
  consumer** — the old `parameters.yaml` `requires.bins` was decorative (always
  `[python3]`), the cautionary tale behind the **"no consumer, no bucket"**
  principle. Real binaries (samtools/cellranger/bcftools) currently hide in
  `trigger_keywords` / nested per-method `requires`. (The external-binary bucket
  is named **`cli`**, not `bash` — `bash` is a `runtime.language`, i.e. *how the
  entry script is run*, distinct from *which external programs it needs*.)

**R's eventual per-skill shape** (when R skills migrate in; `R_TIER_PACKAGES`
becomes a generated/compat *view*, not the source of truth — do not delete it at
once):

```yaml
deps:
  r:
    packages:
      - {name: Seurat, source: cran}
      - {name: DESeq2, source: bioc}
    required_for: [seurat, sctransform]   # which methods need them
```

Forward-compatibility comes from a reserved namespace + `schema_version` +
unknown-field policy + migration script + contract tests — **not** from
pre-seeding empty optional fields across 95 skills (which become de-facto
contracts and get misused).

**Follow-up task (coverage asymmetry):** `dep_spec._load_registries()` loads
`DEPENDENCY_REGISTRY` for only spatial/singlecell/proteomics/metabolomics;
genomics/bulkrna fall through to default-pip classification. Either extend the
registry to all domains or make "default pip" a documented, conscious fallback.

## Consequences

**Positive**
- One machine contract; derived artifacts generated and CI-checked → the
  five-place drift collapses to one source.
- `S=(M,R,C)` is explicit; heterogeneous skills (python/R, leaf/workflow,
  migrated) are one schema's instances → `knowledge_base/` migration becomes
  "fill one table", not "stuff into a fake shell".
- `security:` turns "data never leaves this machine" from prose into a
  schema-enforceable, auditable field.
- MCP export is *easier*, not harder: a structured contract generates a JSON
  tool schema instead of stripping YAML out of Markdown.
- The `requires:` double-meaning is resolved by splitting installs (`deps.python`) from
  runtime preconditions (`interface.inputs.preconditions.env/config`) — see "`deps` granularity" below.

**Negative / costs**
- Two files per skill (machine `skill.yaml` + narrative `SKILL.md`) add minor
  author cognitive load — mitigated by a template, schema validation, and
  generating the `SKILL.md` header + `parameters.md`.
- A migration is required (mitigated by `schema_version` coexistence + per-domain
  batches + CI dual-track).
- Tooling must add generation + drift checks (but replaces four private parsers
  with one pydantic schema → net complexity down).

## Alternatives considered

- **Single `SKILL.md` with everything in frontmatter (the mainstream template).**
  Rejected: for a compound skill carrying `allowed_extra_flags`/`deps`/`outputs`/
  `preconditions`/`validation`/`provenance`/MCP mapping, the frontmatter becomes
  a huge YAML head with poor editor/diff experience, still requires stripping
  YAML out of Markdown for schema validation and MCP export, and tempts authors
  to re-state facts in the prose body. Codex's explicit recommendation was *not*
  to return to single-`SKILL.md`.
- **Keep two files, ungoverned (status quo).** Rejected: that is exactly the
  five-place drift this ADR removes.

## Migration plan

1. Ship `omicsclaw/skill/schema.py` (pydantic) covering v2; wire all four
   readers through it.
2. New skills and all `knowledge_base/` migrations are authored as v2
   (`skill.yaml`).
3. Migrate stock 95 skills per-domain (`parameters.yaml` → `skill.yaml`,
   frontmatter facts → `skill.yaml`, body → narrative); CI validates both v1 and
   v2 until the last domain lands.
4. `knowledge_base/`'s 28 workflows are the first migration consumers (see
   proposal §1.5: dedup four-state list, per-skill acceptance checklist,
   migration manifest, local-first egress scan).

## Dual-track consistency — migrate before any `skill.yaml` enters the live tree

> Codex cross-validation (2026-06-30) flagged the core risk: registry/lazy_metadata
> now read v2 transparently, but **many paths still read `SKILL.md` frontmatter /
> `parameters.yaml` directly.** While the live tree is v1-only this is inert, but
> the moment a skill has BOTH a `skill.yaml` and v1 files these paths diverge (same
> skill shows different facts across CLI routing / catalog / desktop / CI). All of
> the following must read `skill.yaml` (or be one-way generated from it) **before
> per-domain in-place rollout**:

**Generators / validators (read v2 or regenerate from it)**
- ✅ `scripts/generate_catalog.py` — DONE: routes all metadata through `LazySkillMetadata` (dual-track), scans `SKILL.md` **or** `skill.yaml` (finds v2-only skills); byte-identical catalog on the live v1 tree; `LazySkillMetadata` extended with `version`/`tags`/`author`/`license`/`emoji`.
- ✅ `scripts/skill_lint.py` — DONE: split into v1-lint (byte-unchanged) + `_lint_v2` (schema-validate `skill.yaml` → `runtime.entry` exists [draft-exempt] → narrative/script checks: v2 body sections w/o Inputs&Outputs, gotcha anchors, `allowed_extra_flags`↔argparse, output_contract); "must contain a Skip clause" moved to a v2 lint over `summary.skip_when` (lint-level, not schema). `discover_skills` finds `skill.yaml`-only dirs. (`tests/test_skill_lint_v2.py`, 8 green.) **Caveat (Codex):** the leaf/workflow checks are Python/consensus-specific — dispatch by `runtime.language` before migrating R/bash or generic-workflow skills (Python `argparse` scan would misjudge an R/bash leaf's flags).
- ✅ `scripts/audit_skill_requires.py` — DONE: dual-track. `audit_skill` reads the declared surface from `skill.yaml deps.python` (v2) or `SKILL.md requires:` (v1) + a `contract` field; `skill_script_names`/`param_hint_backends` read the v2 `runtime.entry`/`interface.parameters.hints`; `build_report` discovers `skill.yaml`-only dirs; `--write` rewrites `deps.python` via the schema (canonical re-dump) for v2, `SKILL.md` for v1. v1 `--check` byte-unchanged on the live tree (0 missing). (`tests/test_audit_requires_v2.py`, 5 green.) Out of scope: `deps.r` (R dependency semantics handled by the R subsystem; needed before R-skill migration).
- ✅ `scripts/generate_parameters_md.py` + `omicsclaw/skill/parameters_md.py` — DONE: dual-track. `render_parameters_md(sidecar, *, source)` renders byte-identical body for v1 (`param_hints`, `parameters.yaml` header) and v2 (`hints`, new `skill.yaml` header) — only the provenance header differs. `render_for_skill` prefers `skill.yaml` (schema-validate → render `manifest.interface.parameters.model_dump()` source=v2; raises `ValueError` on an invalid manifest so a bad v2 is never silently rendered as v1) else the v1 sidecar; `discover_v2_skills`→`discover_skill_dirs` finds `skill.yaml` **or** `parameters.yaml` dirs. Also wired the deferred **`references/parameters.md` freshness** into `_lint_v2` (extracted `_check_parameters_md_fresh`, file-exists-guarded, source=v2) so a migrated skill's generated doc is kept fresh exactly like a v1 sidecar; generator output == lint expectation (end-to-end verified on real `spatial-de`). Live v1 tree: `generate_parameters_md --all --check` byte-identical (95 skills, exit 0). Body-parity holds across all 19 spatial pilots → at rollout each `parameters.md` diff is a single header line. (`tests/test_generate_parameters_md.py` 10 + `test_skill_lint_v2.py` +2 green.)
- ✅ `scripts/check_description_drift.py` — DONE: it's an orchestrator over `generate_catalog` / `generate_domain_index` / `generate_routing_table`, all of which read descriptions via the registry / `LazySkillMetadata` (dual-track), so the SSOT is now "v2 `skill.yaml summary` else v1 frontmatter". Updated the SSOT messaging (incl. `generate_routing_table` docstring). (Pre-existing `singlecell/INDEX.md` drift is unrelated — confirmed on HEAD.)
- ✅ `scripts/extract_skip_when_cases.py` — DONE: `_load_skill_entries` discovers `SKILL.md`/`skill.yaml` dirs and reads the description via `LazySkillMetadata` (v2 reconstructed `Load when…/Skip when…` preserves the skip clause the LLM extractor needs); unused `yaml` import removed. **Also fixed (Codex): `tests/test_routing_skip_when.py`** was itself a v1-only direct-frontmatter reader — its drift check now sources descriptions through the same dual-track `_load_skill_entries` (so v2/skill.yaml-only skills don't false-`DESCRIPTION_MISSING` and hashes match). (`tests/test_extract_skip_when_cases.py` + `test_routing_skip_when.py`, +2 v2 tests.) NOTE: the v2 reconstructed description differs from v1 prose → a one-time `skip_when_cases.json` snapshot re-extract is expected when a skill migrates (human-reviewed diff).

**Runtime / surfaces (direct readers)**
- ✅ `omicsclaw/surfaces/desktop/server.py` — DONE: dual-track. Replaced the v1-only `_skill_frontmatter()` parser with `_skill_metadata(skill_dir)` → constructs the SAME `LazySkillMetadata` the registry/catalog use, so `GET /skills` + `GET /skills/{domain}/{name}` source version/author/license/tags/requires from `skill.yaml` (v2) else SKILL.md frontmatter (v1). `_skill_diagnostics` takes a `version` arg and reports the runtime contract dual-track (label `skill.yaml` for v2, `parameters.yaml` for v1); `_skill_resources` lists `skill.yaml`. The response field `source` stays builtin/user (`_skill_source`) — NOT conflated with the v1/v2 contract source. v1 output byte-identical end-to-end (verified vs live `spatial-preprocess`: version/author/license/tags/requires/diagnostics/resources unchanged). (`tests/test_server_skill_detail.py` 13 green incl. 4 v2 tests.)
- `omicsclaw/runtime/consensus/sources.py` + `plan.py` — hardcoded `parameters.yaml` `param_hints` reads → registry or `skill.yaml.interface.parameters.hints`.
- `omicsclaw/skill/scaffolder.py` + `templates/` + the `create_omics_skill` tool text — generate/describe `SKILL.md + parameters.yaml` → `SKILL.md + skill.yaml`.
- `omicsclaw/surfaces/channels/telegram.py` + `omicsclaw/extensions/validators.py` — use `SKILL.md` to judge skill-dir/extension validity → accept `skill.yaml`.

**Hardening (this change)**
- ✅ `pydantic>=2,<3` promoted to pip core deps (was environment.yml-only) so the core skill-loading path never silently degrades.
- ✅ `source` ("v2"/"v1") exposed in registry `skill_info` for detection.
- ⬜ Add an `oc doctor` / CI check that FAILS when a `skill.yaml` is present but the skill loads as `source != "v2"` (invalid v2 silently treated as v1) — fallback-to-v1 is migration-period protection, not post-launch correctness.
- ✅ Settle `summary.skip_when` parity — enforced in `_lint_v2` (≥1 rule), mirroring v1's required Skip clause.
- ⬜ `_lint_v2` should call `_check_requires_complete` for v2 lint parity (audit is now dual-track) — deferred; standalone `audit_skill_requires --check` covers it meanwhile (and `_check_requires_complete` has a sandbox `relative_to(SKILLS)` entanglement to untangle first).

**Lower priority (already inherit v2 via the registry; only stale comments):** `capability_resolver`, `runtime/context/layers`, `dep_spec`.

**Pre-existing, unrelated to v2 (note for later):** `DOMAIN_ORDER` in `generate_domain_index.py` / `generate_routing_table.py` omits `literature` while `domain_briefing.py` includes it (a 7-vs-8 domain inconsistency, present before this work). Fix before claiming full 8-domain routing-surface coverage.

## Open items (maintainer to decide)

- **`adapter`**: if ever retained, its boundary vs runtime/tool adapters must be
  defined first; otherwise drop it from the enum entirely.
- **`mcp` export details** (`tool_name`, `input_schema_strategy`) deferred until
  the MCP-server work (proposal §5.4, P4) is scheduled.
- **`workflow` step declaration** (declarative pipeline inside `skill.yaml`)
  deferred; reuse existing `pipelines/*.yaml` for now.
