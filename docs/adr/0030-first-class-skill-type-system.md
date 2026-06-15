# First-class skill types for workflow-aware skill contracts

## Status

**Proposed (Draft, 2026-06-15).** Synthesises an in-session design diagnosis of
the skill architecture, fact-checked against the live tree. Extends ADR 0016
(consensus-as-workflow-runtime), which already introduced — but did not *name*
in the skill contract — the first non-leaf skill shape. The workflow runtime
(`omicsclaw/runtime/consensus/`, ADR 0016 L1) and the consensus shims are
already implemented; what is unbuilt is the **skill metadata / lint / registry
/ catalog** contract that should recognise them. This ADR records that decision
and a phased scope for review; nothing here is committed yet.

## Context

The skill design thesis is sound and is OmicsClaw's core asset: a skill binds
"an LLM-readable, contract-checked methodology card" (`SKILL.md`) to "a local,
deterministic executable" (one script, `--input/--output/--demo`), enforced by
`scripts/skill_lint.py` and the pytest contract suite. For a domain whose
headline failure mode is hallucinated parameters/thresholds, pinning the
agent's freedom to "read a vetted methodology + run a real script" is the right
bet, and more reliable than "thin tools + a clever agent."

But the contract layer assumes **every** skill is a single self-contained leaf
script, and three pressures have outgrown that assumption.

1. **Composition exists in the runtime but not in the contract.** ADR 0016
   established consensus as a *pre-written workflow* over a `runtime/consensus/`
   topology layer — "library, not generative." The consensus skills are
   deliberately ~24-line shims that call `omicsclaw.runtime.consensus.run.main`.
   Yet `skill_lint.py --all` still treats them as leaf scripts and currently
   **fails 5 skills with 101 errors**. Those five are *not* one kind of failure:
   - **3 are workflow shims** whose argparse surface lives in
     `runtime/consensus/run.py`, not the shim: `consensus-domains`,
     `sc-consensus-clustering`, `sc-consensus-integration`. The lint reports
     their `allowed_extra_flags` as "not declared via add_argument" — a false
     negative caused by the missing type, not a real defect.
   - **`consensus-interpret` is not a workflow** — ADR 0016 (§"Out of scope")
     classifies it as a downstream interpret **Skill**. Its lint failures are
     genuine leaf-contract gaps to fix normally (or to classify as a new
     `knowledge` type), not a shim mismatch.
   - **`sc-integrate-cluster` is a leaf member skill** of ADR 0029's
     `sc-consensus-integration`. It should satisfy the ordinary leaf contract;
     its failures are real and unrelated to types.

   So the decision (ADR 0016) exists; the *contract* lags it, and the lag is
   currently mislabelling three workflow shims as broken.

2. **Form is enforced far harder than substance.** The lint enforces
   description phrasing, six body sections, gotcha-anchoring, and the
   `allowed_extra_flags ↔ argparse` match — all *structural*. None of it
   touches whether the science is correct. Today **42/94 catalogued skills have
   `has_tests=false`**: `genomics`, `metabolomics`, `proteomics`, `literature`,
   and `orchestrator` have no per-skill test coverage; `bulkrna` is partial
   (8/13 untested) and `singlecell` is partial (5/33 untested). Maturity is also
   blind: `generate_catalog.py:88` hardcodes
   `status` to `mvp` (or `planned` when there is no script) purely from
   *availability*, so it says nothing about validation. A green lint reads as
   "trustworthy" when it only means "well-formed." Structure *is* a real safety
   boundary for an agent system (param allowlist, output location, error
   semantics = safe-to-invoke); the problem is that it is the *only* tier, with
   no scientific/behavioural validation tier above it.

3. **One fact is hand-authored in several places.** A skill's flag set is
   written in the script `argparse`, `parameters.yaml::allowed_extra_flags`,
   `SKILL.md`, `references/parameters.md`, and `skills/catalog.json`, kept equal
   by lint + generators. Drift is therefore the default state, not an anomaly.
   Note `allowed_extra_flags` is a *security policy*, not a mirror of `argparse`
   — "the script accepts a flag" ≠ "the runner should allow it" — so the single
   source cannot simply be "argparse wins."

These are limits of a too-flat *type system*, not of the skill idea itself.

## Decision

Make **skill type** an explicit, declared dimension of the contract, and let
the template, lint, registry, and catalog **dispatch on type** instead of
assuming every skill is a leaf single-script.

### 1. Add `type` to `parameters.yaml` — optional, default `leaf`

`type` ∈ `leaf` | `workflow` | `knowledge` | `adapter`. It is **optional**:
`LazySkillMetadata` defaults a missing value to `leaf` (it is not in
`_RUNTIME_FIELDS` today, so this is a new read, not a rename). The first
migration annotates **only** the non-leaf skills explicitly; the other 91
current skills change nothing. `type` becomes required in the sidecar only after
`skill_lint.py --all` and `generate_catalog.py` both understand it.

| Type | Execution shape | Example |
|---|---|---|
| `leaf` | one self-contained script, `--input/--output/--demo` | `spatial-de`, `sc-integrate-cluster` |
| `workflow` | thin shim over `runtime/consensus/run.py`; fan-out / members / synthesis; **atomic to the LLM** | `consensus-domains`, `sc-consensus-integration` |
| `knowledge` | methodology / interpretation only; no execution | `consensus-interpret` (candidate) |
| `adapter` | wraps an external tool or remote capability | future R/CLI/remote wrappers |

### 2. Type-aware lint (closes the ADR-0016 lag)

`skill_lint.py` selects a rule profile by `type`:
- `leaf` — today's full structural contract, unchanged. `sc-integrate-cluster`
  and any leaf member skills are validated here; their current failures stay
  failures and get fixed normally.
- `workflow` — instead of requiring local `add_argument`s in the shim, verify
  the shim delegates to `omicsclaw.runtime.consensus.run.main(["--source",
  SOURCE, *argv])`, that `SOURCE` resolves in the consensus source registry,
  and that `allowed_extra_flags` is a subset of the generic `run` parser / the
  source contract. This un-breaks the three workflow shims without weakening
  leaf enforcement.
- `knowledge` — require `SKILL.md` + methodology; drop
  `script`/`allowed_extra_flags`/output-contract requirements. **Not enabled by
  this phase** unless the registry and runner first gain a scriptless
  registered-skill path (`registry._resolve_script_path` returns `None` with no
  script today). Until then, methodology-only entries stay docs or remain normal
  executable skills.
- `adapter` — require the dependency/`requires` manifest and a connectivity
  probe; relax in-process output-contract substring checks.

### 3. Add a `validation_level` field — keep `status` as availability

Do **not** retire `status` (it has a defined availability meaning in the
catalog). Add an orthogonal `validation_level`: `smoke-only` (runs `--demo`) →
`demo-validated` → `fixture-validated` (golden snapshot on a committed fixture)
→ `benchmarked` (real-data + statistical invariant + pinned external-tool
version) → `production`. Do not require real-data benchmarks for
`demo-validated`; require them only at `benchmarked` and above. The router and
UI surface the level so users can tell "it runs" from "it is trusted." This is
the scientific tier that structure alone cannot provide, and it gives the five
zero-test domains a concrete path off `smoke-only`.

### 4. Make `parameters.yaml` the single source — without generating argparse

Use `parameters.yaml` as the source for runner policy, UI parameter cards,
`references/parameters.md` (already generated from it), and catalog metadata.
**Do not** generate the skill's `argparse` from a schema in this ADR — that
fights ADR 0003's human-copy-template principle. Instead, lint compares the
sidecar policy against the executable's parser profile *for that type*.
`allowed_extra_flags` stays a policy view (sidecar ∩ runner policy), preserving
its security role while shrinking the drift surface.

### 5. Strengthen the routing eval that already exists — don't platform-ise

Routing is **not** resting on unverified prose: `capability_resolver` already
has a golden corpus (`tests/fixtures/golden_routing/snapshot.json`,
`test_capability_resolver_golden.py`) and a Skip-when negative eval
(`test_routing_skip_when.py`, `test_extract_skip_when_cases.py`). The action
here is to **extend that corpus** as the catalog grows. Structured capability
tags / top-k retrieval are **deferred** until the resolver shows *measured*
collision failures at a higher skill count — building them now is the
speculative infrastructure ADR 0006/0010 cautioned against.

### 6. Defer output-schema versioning; scope it to consensus artifacts first

Treating `result.json` keys / output filenames as a public API (Hyrum's Law) is
a real risk, but semver-ing all 94 skills at once is premature. **Deferred.**
The first candidate scope is the consensus workflow output artifacts — they
already have a downstream consumer (`consensus-interpret` programs against
them), so a breaking change there is concrete rather than hypothetical.

## Consequences

### Positive

- The three workflow shims become *correctly* contract-valid instead of
  force-failing leaf lint; ADR 0016's design is finally honoured by the contract
  layer, and the two genuine failures (`consensus-interpret`,
  `sc-integrate-cluster`) are no longer hidden in the same bucket.
- "Green" stops over-claiming: `validation_level` makes trust legible and gives
  the five zero-test domains a path off `smoke-only` without breaking the
  existing `status` semantics.
- Drift shrinks: one sidecar feeds policy, UI, docs, and catalog.

### Negative / costs

- A new `type` read touches `LazySkillMetadata`, the registry, `skill_lint.py`,
  and `generate_catalog.py` even while optional; risk of profile drift if a
  type's lint rules are under-specified.
- `knowledge` is not actually usable until the registry/runner gain a scriptless
  path — listing it now risks implying it works.
- Type-branched lint is more code than one flat profile and needs its own tests.

### Deferred / phased

- **P0** — Correct this ADR's baseline facts (done) and classify the five
  current lint failures into workflow-shim vs genuine-leaf/interpret gaps.
- **P1** — Add optional `type` to `parameters.yaml`, `LazySkillMetadata`,
  registry entries, and catalog generation; default missing → `leaf`; mark only
  the workflow shims explicitly.
- **P2** — Make `skill_lint.py` dispatch by type; for `workflow`, validate
  shim/source/run-parser wiring rather than local argparse.
- **P3** — Fix the non-workflow failures (`consensus-interpret`,
  `sc-integrate-cluster`) under the normal contract, or write a separate ADR if
  `consensus-interpret` warrants the `knowledge` type (which needs the
  registry/runner scriptless path first).
- **P4** — Add `validation_level` as a separate evidence field; do not replace
  `status`.
- **Later ADRs** — typed-parameter-schema expansion, retrieval routing
  substrate, and output-schema versioning, each gated on a measured trigger.

## Relationship to prior ADRs

- **ADR 0003 (`0003-skill-template-is-human-copy-only.md`)** — the skill
  template stays human-copy; this adds per-type template variants, not codegen.
  (Note: a second file, `0003-message-bus-decision.md`, shares the number; it is
  relevant only as an anti-speculative-infrastructure precedent, not as the
  skill-template ADR.)
- **ADR 0004** — the registry already ignores `_`-dirs and resolves scripts; it
  gains a `type` read and type-aware script resolution (shim vs leaf), plus the
  scriptless path that `knowledge` would require.
- **ADR 0016** — names and generalises in the *contract* the `workflow` type it
  created in the *runtime*; no change to 0016's runtime shape. `consensus-interpret`
  stays the non-workflow Skill 0016 declared it to be.
- **ADR 0029** — `sc-consensus-integration` is the newest `workflow` skill and
  `sc-integrate-cluster` its leaf member; together they validate the taxonomy
  against the latest flavour.
