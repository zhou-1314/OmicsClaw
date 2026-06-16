# Skill Architecture

This document describes how OmicsClaw's skill system is laid out, discovered,
routed, and kept lean under LLM context. It is the primary reference for:

- Adding, renaming, or removing a skill
- Debugging routing decisions
- Understanding why a given file, generator, or CI job exists
- Extending the bot's tool surface without inflating always-loaded context

> **Last reviewed: 2026-06-16.** Reflects three changes the older revision of
> this doc predated: (1) the core skill modules moved from `omicsclaw/core/` to
> **`omicsclaw/skill/`**; (2) per-skill machine metadata moved out of the
> `SKILL.md` frontmatter into a **`parameters.yaml` sidecar** (v2); and (3)
> **ADR 0030** added first-class skill `type` + `validation_level`. See
> `CONTRIBUTING.md` for the day-to-day contributor workflow and
> `docs/adr/0030-first-class-skill-type-system.md` for the type system.

---

## 1. What a skill is

A skill is a **single analytical capability** packaged as a directory under
`skills/`. At minimum it contains:

- `SKILL.md` — YAML frontmatter (a little) + markdown body (the human-readable
  methodology, six required sections)
- `parameters.yaml` — the **v2 sidecar**: the machine-readable metadata that the
  registry, lint, catalog, and runner all read
- A runnable entrypoint (`*.py`) invoked by `omicsclaw.py run <alias>`

Skills are dynamically discovered at startup by
`omicsclaw/skill/registry.py:OmicsRegistry.load_all()`. There is no
hand-maintained list — adding a directory with a valid `SKILL.md` is enough to
register it.

### Design principles

1. **Directory layout is the source of truth.** CLAUDE.md tables,
   `catalog.json`, `orchestrator/SKILL.md`, and per-domain `INDEX.md` are all
   *generated* from the filesystem + sidecars. Never hand-edit them.
2. **Sidecar serves machines; SKILL.md body serves humans.** Everything in
   `parameters.yaml` is parsed by the registry and surfaced to the LLM or
   autoagent. The markdown body is for contributors reading the repo.
3. **Keep always-loaded context small.** The LLM sees an 8-domain briefing plus
   per-tool specs every turn. Full per-skill detail is paged in on demand. See
   §5 and §8.
4. **Fail fast, with evidence.** Lint (`scripts/skill_lint.py`) and the
   catalog drift check (`generate_catalog.py --check`) reject silent drift. If
   you add a tool or rename a skill and don't regenerate the derived docs, the
   checks stop you.
5. **The contract dispatches on skill `type`.** Not every skill is a
   self-contained leaf script — `workflow` skills are thin shims over a shared
   runtime. Lint and the catalog pick a rule profile by `type` (ADR 0030, §3).

---

## 2. Directory layout

```
skills/
├── catalog.json                     # generated: full machine-readable index
├── _shared/                          # cross-domain helper libraries (optional)
├── spatial/
│   ├── INDEX.md                      # generated: lazy-load detail for LLM
│   ├── _lib/                         # shared spatial helpers
│   ├── spatial-preprocess/
│   │   ├── SKILL.md                  # frontmatter + methodology (6 sections)
│   │   ├── parameters.yaml           # v2 sidecar: machine metadata
│   │   ├── spatial_preprocess.py     # entrypoint (canonical *.py)
│   │   ├── references/               # generated parameters.md / methodology
│   │   ├── tests/
│   │   └── figure_data/              # (optional) artifacts for re-rendering
│   └── spatial-de/
│       └── …
├── singlecell/
│   ├── INDEX.md
│   ├── scrna/                        # nested subdomain
│   │   ├── sc-preprocessing/
│   │   └── …
│   └── scatac/                       # nested subdomain
│       └── scatac-preprocessing/
├── genomics/
├── proteomics/
├── metabolomics/
├── bulkrna/
├── orchestrator/
│   ├── INDEX.md
│   ├── SKILL.md                       # meta-skill — routes across domains
│   └── omics-skill-builder/
└── literature/                        # 8th domain — PDF/DOI/PubMed/GEO parsing
```

There are **8 domains and 94 skills** (see `skills/catalog.json:skill_count`).
Per-domain counts are computed live (§4), currently: spatial 19, singlecell 33,
genomics 10, proteomics 8, metabolomics 8, bulkrna 13, orchestrator 2,
literature 1.

### Flat vs nested subdomains

`skills/singlecell/` is nested (`scrna/` and `scatac/` subdirectories) because
the subdomains share preprocessing conventions but diverge in downstream
methods. Other domains are flat. The registry scanner handles both shapes — a
skill directory is anything containing a `SKILL.md`.

### Generated files (never hand-edit)

| Path | Generator | Purpose |
|---|---|---|
| `skills/catalog.json` | `scripts/generate_catalog.py` | Machine-readable listing of every skill |
| `skills/<domain>/INDEX.md` | `scripts/generate_domain_index.py` | Per-domain lazy-load detail (consumed by `list_skills_in_domain` tool and humans) |
| `skills/orchestrator/SKILL.md` (count fields) | `scripts/generate_orchestrator_counts.py` | The hardcoded skill-count passages kept in sync |
| `CLAUDE.md` (between routing markers) | `scripts/generate_routing_table.py` | Compact 8-domain briefing shown to Claude Code |

Run them all at once:

```bash
python scripts/sync_skill_docs.py --apply    # regenerate all four
python scripts/sync_skill_docs.py --check     # CI-style drift check
```

---

## 3. The skill contract: `SKILL.md` body + `parameters.yaml` sidecar

Per-skill metadata lives in **`parameters.yaml`** (the v2 sidecar). The parser is
`omicsclaw/skill/lazy_metadata.py:LazySkillMetadata` (lazy, read-only) and
`scripts/generate_catalog.py` (one-shot batch scan). The legacy
`metadata.omicsclaw` block in the `SKILL.md` frontmatter is **removed** — all 94
skills have migrated to the sidecar, and `scripts/skill_lint.py` errors if a
legacy block reappears.

### `SKILL.md` — frontmatter + six required body sections

The frontmatter now carries only display/identity fields (`name`,
`description`, `version`, `author`, `license`, `tags`). `LazySkillMetadata`
reads `name`/`description` from it and everything else from the sidecar.

The body must contain six sections, enforced by
`scripts/skill_lint.py:REQUIRED_SECTIONS`:

```
## When to use
## Inputs & Outputs
## Flow
## Gotchas
## Key CLI
## See also
```

(`## Gotchas` is additionally checked: every code anchor it cites must
grep-resolve in the skill's script — see `_check_gotchas_anchors`.)

### `parameters.yaml` — machine metadata (all fields top-level)

```yaml
domain: spatial                    # REQUIRED: one of the 8 domain keys
type: leaf                         # ADR 0030: leaf|workflow|knowledge|adapter (default leaf)
validation_level: smoke-only       # ADR 0030: scientific-maturity ladder (default smoke-only)
script: spatial_de.py
trigger_keywords:                  # drives routing scoring
  - spatially variable genes
  - marker genes
allowed_extra_flags:               # SECURITY: the only CLI flags the bot may pass
  - "--method"
  - "--top-n"
legacy_aliases: [old-name]         # old names still route + run
saves_h5ad: true                   # advertised in prefetch; helps the LLM plan chains
requires_preprocessed: true        # prefetch warns the LLM about expected input
param_hints: { … }                 # method-level search/preview surface (below)
```

The fields exposed as `LazySkillMetadata` properties are listed in
`omicsclaw/skill/lazy_metadata.py:_RUNTIME_FIELDS`. When a sidecar is present it
wins per-field; otherwise the loader falls back to legacy frontmatter (the
fallback exists for safety — no shipped skill relies on it).

### `param_hints` — method-level search surface

For skills the **autoagent** can auto-tune or that the bot shows parameter
previews for, add one block per method:

```yaml
param_hints:
  pydeseq2:
    priority: "condition_key → reference_condition → pydeseq2_fit_type"
    params: ["condition_key", "sample_key", "pydeseq2_fit_type", "pydeseq2_alpha"]
    defaults: {condition_key: "condition", sample_key: "sample_id",
               pydeseq2_fit_type: "parametric", pydeseq2_alpha: 0.05}
    requires: ["raw_or_counts", "obs.condition_key", "obs.sample_key"]
    tips:
      - "--pydeseq2-fit-type: parametric or mean dispersion fit"
```

`param_hints` is not documentation; it is actively read by the autoagent search
space, the autoagent optimizability filter, the bot's parameter-preview block,
method-name → skill inference, the runtime input-suitability check (`requires`
tokens), and candidate scoring in `capability_resolver`. A skill without
`param_hints` still works but loses autoagent optimization, bot parameter
preview, and method-name-based routing.

### Legacy aliases

```yaml
legacy_aliases: [old-name, even-older-name]
```

The registry registers both the canonical alias and each legacy alias.
`omicsclaw.py run old-name` still works, and legacy aliases bubble into
`capability_resolver` scoring (an exact legacy-name hit is worth ~9 points) so
users who learned the old name still route correctly.

---

## 3.5. Skill types & validation levels (ADR 0030)

ADR 0030 makes **skill type** an explicit, declared dimension of the contract,
so the template, lint, registry, and catalog can **dispatch on type** instead of
assuming every skill is a leaf single-script. Both fields are optional in the
sidecar and default safely, so existing leaf skills needed no edit. Constants:
`omicsclaw/skill/lazy_metadata.py:SKILL_TYPES` and `:VALIDATION_LEVELS`.

### `type` — four execution shapes

| Type | Execution shape | Example |
|---|---|---|
| `leaf` (default) | one self-contained script, `--input/--output/--demo` | `spatial-de`, `sc-integrate-cluster` |
| `workflow` | thin shim over `omicsclaw/runtime/consensus/run.py`; fan-out / members / synthesis; **atomic to the LLM** | `consensus-domains`, `sc-consensus-integration` |
| `knowledge` | methodology / interpretation only; no execution | `consensus-interpret` (candidate) |
| `adapter` | wraps an external tool or remote capability | future R/CLI/remote wrappers |

A missing/unknown `type` falls back to `leaf`. `type` is read but not yet
*required* in the sidecar; it becomes required only once every consumer
understands it.

### Type-aware lint

`scripts/skill_lint.py` selects a rule profile by `type`:

- **`leaf`** — the full structural contract (frontmatter, six body sections,
  `allowed_extra_flags ↔ add_argument` match, output-contract substrings).
- **`workflow`** — the shim has no local `argparse` (its surface lives in the
  shared `runtime/consensus/run` parser), so instead lint **proves the shim
  delegates**: an AST check that the top-level `main` makes a reachable call of
  the exact shape `main(["--source", SOURCE, *argv])`, that `SOURCE` resolves in
  `CONSENSUS_SOURCES`, and that `allowed_extra_flags` ⊆ the generic run parser's
  flags. (See `_check_workflow_shim` / `_analyse_workflow_shim`.)
- **`knowledge` / `adapter`** — reserved profiles; `knowledge` needs the registry
  to first gain a scriptless registered-skill path, so it is not enabled yet.

### `validation_level` — scientific-maturity ladder

Orthogonal to `status` (which records *availability*). It answers "how much do we
trust the science," which structure alone cannot:

```
smoke-only        # at least runs --demo (default)
demo-validated    # demo output sanity-checked
fixture-validated # golden snapshot on a committed fixture
benchmarked       # real-data + statistical invariant + pinned external-tool version
production
```

Real-data benchmarks are required only at `benchmarked` and above. The level
gives the zero-test domains a concrete path off `smoke-only`, and lets the
router/UI distinguish "it runs" from "it is trusted."

---

## 4. Registry mechanism

### Discovery

`OmicsRegistry.load_all()` (in `omicsclaw/skill/registry.py`) scans `skills/`
recursively, yielding one entry per canonical alias plus one entry per legacy
alias (both pointing to the same info dict). That means `registry.skills` can
have more keys than there are skills — callers that want canonical-only results
use `iter_primary_skills()`.

### Domain metadata

`_HARDCODED_DOMAINS` (`omicsclaw/skill/registry.py`) declares the 8 domain keys
and their static metadata:

```python
"spatial": {
    "name": "Spatial Transcriptomics",
    "primary_data_types": ["h5ad", "h5", "zarr", "loom"],
    "summary": "Spatial transcriptomics for Visium/Xenium/MERFISH/...",
    "representative_skills": ["spatial-preprocess", "spatial-domains", ...],
},
```

`skill_count` is **not** declared there — it is computed from the live
filesystem by `_refresh_domain_skill_counts()` after every `load_all()`, so the
value cannot drift from the actual contents of `skills/`. The `summary` and
`representative_skills` fields drive the 8-domain briefing (§5).

### Lazy metadata

`LazySkillMetadata` (`omicsclaw/skill/lazy_metadata.py`) is the per-skill
metadata parser. It is *lazy* — a skill's `parameters.yaml` (and any frontmatter
fallback) is read only when that skill is actually accessed. The bot routing path
relies on this: touching the registry doesn't load all 94 skills' sidecars.

---

## 5. Three-layer routing architecture

OmicsClaw splits routing across three tiers so the LLM pays for detail only when
it needs detail. The tiers operate within a **single LLM turn** — this is not
two-stage LLM routing (see §5.6 for why).

```
┌──────────────────────────────────────────────────────────────┐
│  L0  capability_resolver (programmatic, 0 LLM tokens)         │
│      scores all 94 skills by trigger_keywords + description    │
│      token overlap + file extension + method mentions          │
│      → returns chosen_skill OR skill_candidates[:5]            │
└──────────────────────────────────────────────────────────────┘
              ▲  call with skill='auto' + query=...
┌──────────────────────────────────────────────────────────────┐
│  L1  domain briefing (always-loaded, ~500 tokens)             │
│      8 domain lines: summary + representative skills each      │
│      embedded in the omicsclaw tool description                │
└──────────────────────────────────────────────────────────────┘
              │  LLM needs a domain's full list
              ▼
┌──────────────────────────────────────────────────────────────┐
│  L2  list_skills_in_domain tool (lazy, 500–2000 tokens)       │
│      LLM calls with domain=... [filter=...]                    │
│      returns markdown: alias, desc, triggers                   │
└──────────────────────────────────────────────────────────────┘
              │  skill chosen → execute
              ▼
┌──────────────────────────────────────────────────────────────┐
│  L3  prefetch_skill_context (on-demand, ~500 tokens)          │
│      param_hints keys + requires flags + saves_h5ad            │
└──────────────────────────────────────────────────────────────┘
```

### 5.1 L0 — Programmatic capability resolution

`omicsclaw/skill/capability_resolver.py:resolve_capability()` scores every
canonical skill. Approximate signal weights (authoritative values live in the
resolver — read `decision.reasoning` to see the trace):

| Signal | Weight (approx.) |
|---|---|
| Alias exact match | +10 |
| Legacy alias mention | +9 |
| Trigger keyword match | +1.0–1.7 per hit (up to 3) |
| Description token overlap | +0.85 per matching token (cap 8) |
| Method name in `param_hints` | +3.0 |
| File extension → domain | filters candidate pool |

`CapabilityDecision` carries `chosen_skill` (best match above the threshold),
`confidence`, `skill_candidates[:5]`, `coverage`
(`exact_skill`/`partial_skill`/`no_skill`), and a human-readable `reasoning[]`
trace. Domain pre-detection is `_detect_domain()`; the analysis-intent gate is
`_looks_like_analysis_request()`.

### 5.2 L1 — Domain briefing

Rendered by `omicsclaw/skill/domain_briefing.py:build_domain_briefing()`:

```markdown
OmicsClaw dispatches multi-omics analysis across 8 domains.

- **spatial** (19 skills — Spatial Transcriptomics)
  Spatial transcriptomics for Visium/Xenium/MERFISH/Slide-seq: QC, ...
  Key skills: spatial-preprocess, spatial-domains, spatial-de, ...
- **singlecell** (33 skills — Single-Cell Omics)
  scRNA-seq + scATAC-seq: FASTQ→counts, QC, filter, ...
  ...
```

The briefing is pre-rendered once per tool-registry build and passed into the
bot tool context, so the registry isn't re-scanned per `build_bot_tool_specs()`
call.

### 5.3 L2 — `list_skills_in_domain` tool

Implemented in `omicsclaw/skill/listing.py:list_skills_in_domain` and exposed as
a bot tool. When the LLM can't pick a skill from the briefing alone:

```json
{ "name": "list_skills_in_domain", "domain": "singlecell", "filter": "velocity" }
```

Returns a markdown block identical in shape to `skills/<domain>/INDEX.md` but
filtered live from the registry (not from disk, to avoid staleness).

### 5.4 L3 — Prefetched skill context

After the LLM picks a skill, the context layers inject a "Prefetched Skill
Context" block: selected alias + domain + summary, up to 4 `param_hints` method
keys, `requires_preprocessed` / `saves_h5ad` flags, and legacy aliases. This is
the closest the LLM gets to the full sidecar, and only after a skill is chosen.

### 5.5 Disambiguation gate

When `skill="auto"` is used, the executor checks the top-1 / top-2 score gap in
`decision.skill_candidates`. `_AUTO_DISAMBIGUATE_GAP`
(`omicsclaw/skill/orchestration.py`) defaults to `2.0`:

```python
if len(cands) >= 2 and (cands[0].score - cands[1].score) < _AUTO_DISAMBIGUATE_GAP:
    return _format_auto_disambiguation(decision, query)  # refuse to execute
```

When triggered, the bot returns a list of top candidates and asks the LLM to
re-invoke with an explicit `skill`, avoiding a multi-minute run on the wrong
skill.

**Known limitation:** a trigger-keyword hit adds several points to top-1, so
real queries rarely land in the disambiguation band. The gate is architecturally
correct but under-utilized; tune `_AUTO_DISAMBIGUATE_GAP` against real logs.

### 5.6 Why not two-stage LLM routing?

A tempting alternative — call the LLM to pick a domain, then again to pick a
skill — was rejected because:

1. **Domain boundaries are latent.** "find marker genes in tumor regions" routes
   by *data modality* (H&E + coordinates vs. h5ad counts vs. bulk CSV), inferred
   from context, not from asking "which domain?".
2. **2× P50 latency** per turn is painful in chat.
3. **Prompt cache eviction.** Two-stage prompts share no prefix, defeating the
   5-min TTL.
4. **Cross-domain semantic clusters.** `spatial-enrichment`, `bulkrna-enrichment`,
   `sc-enrichment`, and `metabolomics-pathway-enrichment` all answer "pathway
   analysis". Domain-first forces an early, possibly wrong, split.

---

## 6. Bot tool contract

The bot-surface tool registry is built by
`omicsclaw/runtime/tools/builders/agent.py:build_bot_tool_specs()` and executed
via `omicsclaw/runtime/tools/registry.py`. The skill-execution executor lives in
`omicsclaw/runtime/tools/builders/agent_executors.py:execute_omicsclaw`, with the
routing helpers (`_build_param_hint`, `_infer_skill_for_method`,
`_AUTO_DISAMBIGUATE_GAP`, `_format_auto_disambiguation`) in
`omicsclaw/skill/orchestration.py`. (The legacy `bot/core.py` was carved out into
these modules per ADR 0001.)

Three tools form the routing triad:

| Tool | Purpose |
|---|---|
| `omicsclaw` | Execute a skill (primary action); description carries the domain briefing + routing policy |
| `list_skills_in_domain` | Page in one domain's full list (read-only) |
| `resolve_capability` | Programmatically inspect a query's routing decision |

### Routing policy embedded in the `omicsclaw` tool description

> PREFER `skill='auto'` together with `query=<user's request verbatim>`. The
> capability resolver scores all skills deterministically (no extra LLM call).
> Pass a specific `skill` name ONLY when the user named it, or a prior
> auto-routing result asked you to disambiguate.

This nudges the LLM toward the L0 path (free tokens, deterministic) rather than
guessing from the enum.

---

## 7. Doc generation pipeline

Four generators keep skill-derived documentation in sync with the filesystem.
All support `--apply` and `--check`.

```
        skills/**/parameters.yaml + SKILL.md (truth)
        _HARDCODED_DOMAINS (truth)
                    │ load_all()
                    ▼
              OmicsRegistry
        ┌─────────┼─────────┬───────────────┐
        ▼         ▼         ▼               ▼
 generate_   generate_   generate_      generate_
 routing_    orchestr…   catalog.py     domain_index.py
 table.py    counts.py
 CLAUDE.md   orchestr…   catalog.json   <domain>/INDEX.md ×8
 routing     SKILL.md
 block       counts
```

| Generator | Output | Consumers |
|---|---|---|
| `generate_routing_table.py` | `CLAUDE.md` between `<!-- ROUTING-TABLE-START/END -->` | Claude Code context |
| `generate_orchestrator_counts.py` | passages in `skills/orchestrator/SKILL.md` | Orchestrator skill body |
| `generate_catalog.py` | `skills/catalog.json` | External tooling, tests |
| `generate_domain_index.py` | `skills/<domain>/INDEX.md` ×8 | Humans + `list_skills_in_domain` |

`scripts/sync_skill_docs.py` drives all four (`--apply` / `--check`).

### CI enforcement

`.github/workflows/pr-ci.yml`'s **`core-test`** job runs
`python scripts/generate_catalog.py --check` (catalog drift), alongside the lint
and documentation-fact tests (`tests/test_documentation_facts.py` pins the exact
skill-count fragments across the public docs). Run `sync_skill_docs.py --check`
and `skill_lint.py --all` locally before pushing; a drift or lint failure should
be fixed with `sync_skill_docs.py --apply`.

> `scripts/check_routing_budget.py` and `measure_routing_tokens.py` (§8) still
> exist and are runnable locally, but are not wired into `pr-ci.yml` today.

---

## 8. Token budget policy

### What "always-loaded" means

Every bot turn the LLM receives: (1) the entire bot-surface tool spec registry,
(2) the system prompt, (3) any injected context (e.g. CLAUDE.md for Claude Code).
Items 1 + 3 are what we measure and budget.

### The policy

The routing refactor (the "Stage 2–4" work) cut the always-loaded surface
dramatically — `omicsclaw.description` and the CLAUDE.md routing block each
shrank ~80% versus the pre-refactor flat per-skill list — and pinned the result
with hard ceilings:

- `scripts/measure_routing_tokens.py` measures the live surface and can
  `--compare` against a saved baseline.
- `scripts/check_routing_budget.py` compares the live measurement against the
  ceilings in `build/routing-baselines/ceiling.json` and exits 1 if exceeded.

Legitimate reasons to raise a ceiling: a genuinely new bot tool, or a description
expanded for a new mode/parameter. Not legitimate: inline-docs bloat, or
re-embedding a flat skill list (undoing the refactor). When raising, edit
`ceiling.json`, commit, and justify in the PR (ideally with a
`measure_routing_tokens.py --compare` diff).

---

## 9. How to add a new skill

Checklist for a brand-new leaf skill `spatial-foo`:

### 1. Create the directory

```
skills/spatial/spatial-foo/
├── SKILL.md
├── parameters.yaml
├── spatial_foo.py
└── tests/
    └── test_spatial_foo.py
```

Start from `templates/skill/` (it ships a `SKILL.md`, `parameters.yaml`,
`references/`, `tests/`, and a `replace_me.py` stub).

### 2. Write `SKILL.md` + `parameters.yaml`

- `SKILL.md`: frontmatter (`name`, `description`, …) + the six required body
  sections (§3).
- `parameters.yaml`: `domain` (required), `type` (omit for leaf),
  `validation_level`, `trigger_keywords`, `allowed_extra_flags`, `script`, and
  optional `param_hints`. Canonical examples:
  `skills/spatial/spatial-preprocess/`, `skills/singlecell/scrna/sc-de/`.

### 3. Implement the entrypoint

- `--input <path>` / `--output <dir>` contract
- `--demo` flag that runs with bundled synthetic data
- Persist results as `.h5ad` / `.csv` + `figure_data/` for replotting

### 4. Regenerate derived docs

```bash
python scripts/sync_skill_docs.py --apply   # catalog + INDEX + CLAUDE.md + orchestrator counts
```

### 5. Verify

```bash
python omicsclaw.py list                      # new skill should appear
python omicsclaw.py run spatial-foo --demo    # end-to-end smoke test
python -m pytest skills/spatial/spatial-foo/tests/ -v
python scripts/skill_lint.py skills/spatial/spatial-foo   # contract check (type-aware)
python scripts/sync_skill_docs.py --check     # should be green
```

For a `workflow` skill, write a ~24-line shim that imports
`omicsclaw.runtime.consensus.run.main`, defines `SOURCE = "<flavour>"`, and has
`main(argv)` delegate `main(["--source", SOURCE, *argv])` — register the flavour
in `CONSENSUS_SOURCES`. Lint validates the delegation by AST (§3.5).

### 6. (Optional) Add routing regression cases

If the skill introduces new keywords or competes with an existing skill, extend
the golden corpus (`tests/fixtures/golden_routing/`,
`tests/test_capability_resolver_golden.py`) and the Skip-when negative eval.

---

## 10. How to add a new bot tool

When the tool is not itself a skill:

1. Declare the `ToolSpec` in
   `omicsclaw/runtime/tools/builders/agent.py:build_bot_tool_specs()`
2. Write the executor (`async def execute_mytool(args, **kwargs) -> str`) in
   `omicsclaw/runtime/tools/builders/agent_executors.py`
3. Register it in the tool registry (`omicsclaw/runtime/tools/registry.py`)
4. Add tests: ToolSpec present, executor returns an error message (not an
   exception) on missing required params, happy path end-to-end
5. Run `python scripts/check_routing_budget.py` — new tools grow the tool-spec
   surface

Follow the pattern set by `list_skills_in_domain`
(`omicsclaw/skill/listing.py` pure function + ToolSpec + executor + tests).

---

## 11. Known limitations and roadmap

### Resolver weaknesses (tracked in the routing eval)

1. **Analysis-verb gate is too narrow.** `_looks_like_analysis_request` rejects
   queries that start with verbs like *call* ("call SNVs") or *identify*. Fix:
   expand the whitelist or accept any query that scores a non-zero candidate.
2. **File-extension domain isn't a hard filter.** `.vcf.gz` → genomics is
   detected, but candidates from other domains still surface. Fix: hard-filter
   the candidate pool when an extension pins the domain.
3. **Cross-domain queries collapse to one domain.** `_detect_domain`
   over-commits on keywords like *pathway* (→ spatial). The candidate list should
   span ≥2 domains so the disambiguation gate can fire.
4. **`_AUTO_DISAMBIGUATE_GAP` is rarely triggered** (§5.5) — needs calibration
   against real traffic.

### ADR 0030 follow-ups

- **`knowledge` / `adapter` profiles are reserved, not enabled.** `knowledge`
  needs the registry/runner to gain a scriptless registered-skill path first;
  until then, methodology-only entries stay normal executable skills or docs.
- **Output-schema versioning is deferred.** The first candidate scope is the
  consensus workflow output artifacts (already consumed by `consensus-interpret`).

### Unfinished cleanups

- **`param_hints` coverage is uneven** across skills; backfill skeletons where
  the autoagent would benefit.
- **`param_hints.defaults` can desync from argparse defaults.** Planned:
  introspection-based diff.
- **No explicit input/output schemas** between chained skills (preprocess →
  domains → de assume specific `obs`/`obsm` keys). Planned: per-skill
  `inputs:`/`outputs:` + a contract test for the core chains.

---

## 12. Where to look when something is wrong

| Symptom | First file to read |
|---|---|
| New skill not found by `omicsclaw.py run` | `omicsclaw/skill/registry.py` (discovery scan) |
| CLAUDE.md routing table out of date | `scripts/generate_routing_table.py` |
| Bot tool description missing fields | `omicsclaw/runtime/tools/builders/agent.py:build_bot_tool_specs` |
| Routing sends to wrong skill | `omicsclaw/skill/capability_resolver.py:resolve_capability` (read `decision.reasoning`) |
| Bot refuses an obviously-valid query | `_looks_like_analysis_request` in `capability_resolver.py` |
| Workflow shim fails lint | `scripts/skill_lint.py:_check_workflow_shim` (delegation/SOURCE/flags) |
| Autoagent says "no optimizable methods" | `param_hints` missing or missing `defaults` |
| Catalog / doc counts drift | Run `python scripts/sync_skill_docs.py --apply` locally |

---

## Appendix A — File map

Skill core (`omicsclaw/skill/`):

- `registry.py` — skill discovery, alias resolution, `_HARDCODED_DOMAINS`
- `lazy_metadata.py` — per-skill lazy sidecar parser; `SKILL_TYPES`,
  `VALIDATION_LEVELS`, `_RUNTIME_FIELDS`
- `capability_resolver.py` — programmatic routing (L0)
- `domain_briefing.py` — L1 briefing renderer
- `listing.py` — L2 `list_skills_in_domain`
- `orchestration.py` — routing helpers + disambiguation gate

Runtime / bot surface (`omicsclaw/runtime/`):

- `tools/builders/agent.py` — LLM-facing tool specs (`build_bot_tool_specs`)
- `tools/builders/agent_executors.py` — `execute_omicsclaw` and other executors
- `tools/registry.py` — spec → executor wiring
- `consensus/run.py` — generic typed-consensus entry (`workflow` skills shim here)

Scripts:

- `scripts/sync_skill_docs.py` — one-shot wrapper over the four generators
- `scripts/generate_routing_table.py`, `generate_orchestrator_counts.py`,
  `generate_catalog.py`, `generate_domain_index.py`
- `scripts/skill_lint.py` — type-aware contract lint
- `scripts/measure_routing_tokens.py`, `check_routing_budget.py`

Tests (routing / contract-adjacent):

- `tests/test_registry.py`, `tests/test_lazy_metadata.py`
- `tests/test_capability_resolver*.py`, `tests/test_routing_*` (golden + skip-when)
- `tests/test_skill_lint.py`, `tests/test_generate_catalog.py`
- `tests/test_documentation_facts.py` — pins skill-count fragments across docs
