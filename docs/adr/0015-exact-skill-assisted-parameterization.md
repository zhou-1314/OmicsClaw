# Exact-Skill Assisted Parameterization (Outer-Owned, Data-Grounded)

## Status

Proposed (2026-05-29). Amends [ADR 0013](0013-autonomous-analysis-path.md) and
[ADR 0014](0014-outer-owned-autonomous-understanding.md) (both remain Accepted).
Extends ADR 0014's outer-owned, data-grounded understanding from the No-skill /
Partial-skill autonomous routes to the **Exact skill match** route, and resolves
ADR 0014's "Auto mode" open question by defining `auto` as an explicit
no-understanding, run-as-typed mode.

## Context

`docs/CONTEXT.md` has long defined the Exact-skill execution rule as
**Deterministic route, assisted parameterization**: the Analysis Router chooses
the skill, while *"LLM-assisted preflight fills parameters and asks for
confirmation when needed"* (ADR 0013). The shipped implementation under-delivers
against that contract — the same doc-vs-code gap ADR 0014 found on the
autonomous path, now on the exact-skill path:

- **`auto` mode runs the default method with no LLM at all.**
  `build_analysis_tool_plan` (`omicsclaw/analysis_router/dispatcher.py`) builds
  `{skill, mode:"path", file_path, query}` — **no `method`, no preflight LLM
  step** — and executes it, yielding "Exact skill route completed." The skill
  falls back to its own default method.
- **A method named in the request is dropped.** The plan never extracts a
  method, so "对 …h5ad 用 CellCharter 进行 spatial niche 鉴定" routes to
  `spatial-domains` and still runs the default (leiden), ignoring CellCharter.
- **`assist` mode is not handed the method menu.** The injected route context
  (`_format_analysis_route_context` in `loop.py`) carries skill name, coverage,
  and confidence, but **not the matched skill's available methods**, so even the
  assisted path cannot reliably recommend a method.
- **Exact-skill gets no data inspection.**
  `_build_autonomous_understanding_context` (`loop.py`) deliberately returns
  `""` for `EXACT_SKILL`, so ADR 0014's data-grounded planning never reaches it.

Observed symptom: "对 slideseqv2_mouse_hippocampus.h5ad 进行 spatial niche 的鉴定"
maps to `spatial-domains` and silently runs the default method; the user is
never shown a recommendation grounded in their stated need and the skill's
capabilities, and an explicitly named method is ignored.

Raw material that already exists: method menus live as **SKILL.md prose** (e.g.,
`spatial-domains` documents `leiden` (default) plus `louvain / spagcn / stagate /
graphst / banksy / cellcharter`, with GPU needs and parameters); the registry
locates each skill's `SKILL.md`; `CapabilityDecision` exposes ranked skill
candidates with scores. There is **no** structured per-skill `methods` field.
Desktop already renders a structured preflight surface
(`pending_preflight_requests` on the backend; `src/lib/preflight-guidance.ts` on
the frontend, gating on `status == needs_user_input` / `confirmations`).

## Decision

Implement **Deterministic route, assisted parameterization** as the outer-loop,
data-grounded behavior for an **Exact skill match under the default `assist`
mode**. The Analysis Router keeps fixing *which* skill runs; the outer LLM owns
*how* — method and key parameters — strictly *within* that skill.

Guiding principle (inherited from ADR 0014): **deterministically guarantee the
inputs to the recommendation; let the LLM do the judging.**

When the Analysis Router returns an Exact skill match in `assist` mode:

1. **Data-grounded inputs (deterministic).** Inject the matched skill's
   `SKILL.md` (method menu, defaults, parameters, preconditions) and — when a
   trusted input file is present — an `inspect_data` schema
   (`obs/var/obsm/layers/uns`, shape, platform). Reuses ADR 0014's inspect
   machinery.
2. **Recommend (LLM).** The outer LLM emits a recommendation — chosen method +
   key parameters + rationale + any near-tied alternative skills — grounded in
   the SKILL.md menu and the schema. The recommendation is **always shown**.
3. **Assisted-parameterization rule** (the precise reading of "when needed"):
   1. a method explicitly named in the request is **used as-is**;
   2. a safe/clear choice **proceeds** with explicitly stated assumptions;
   3. a materially different, query-unresolved choice **asks exactly one focused
      question** via the structured preflight channel;
   4. a missing precondition (e.g. absent `obsm["X_pca"]`) **blocks with
      remediation** (run `spatial-preprocess` first) instead of running.
4. **Confirm (structured).** The focused question and recommendation ride the
   existing structured preflight channel (`pending_preflight_requests`, rendered
   by the desktop preflight-guidance surface), not ad-hoc chat text; resume via
   the existing pending-preflight path.
5. **Execute.** The chosen skill runs through the existing shared skill-runner
   path with the recommended method/parameters.

Recommendation scope stays **within** the chosen skill; it never reselects the
skill — a near-tied alternative is *surfaced*, not auto-substituted.

`auto` is defined as the **Run-as-typed route**: deterministic skill call, no
outer-LLM step, no inspection, no confirmation. It *honors a method explicitly
named in the request* (deterministic extraction in `build_analysis_tool_plan`)
but performs no recommendation. This resolves ADR 0014's "Auto mode" open
question in the explicit "no-understanding, run-as-typed" direction, keeping a
meaningful two-mode distinction: `assist` is intelligent (default); `auto` is
literal.

Canonical terms updated in `docs/CONTEXT.md`: **Deterministic route, assisted
parameterization** (sharpened), and **Run-as-typed route** (new).

## Considered alternatives

- **Cross-skill approach recommendation** (let the LLM reselect / expand which
  skills run). Rejected as primary: erodes ADR 0013's deterministic skill choice;
  the close-tie disambiguation already covers ambiguous matches. Near-tied
  alternatives are surfaced, not chosen.
- **Route `auto` through the same understanding preflight** (the other ADR 0014
  open-question option). Rejected: blurs `auto` vs `assist` and taxes the
  fast/literal path with an LLM round-trip. Keeping `auto` literal preserves a
  real distinction.
- **Query + SKILL.md only, no data inspection.** Rejected: method choice often
  depends on the data (size, platform, GPU, required keys); query-only
  recommendations regress to guessing — the failure ADR 0014 cautioned against.
- **Rely on the LLM's own knowledge of each skill's methods** (inject the skill
  name only). Rejected: risks hallucinated/invalid methods; violates "feed the
  inputs deterministically."
- **Structured `methods` frontmatter + registry field as the menu source.**
  Deferred: cleaner for UI/validation but a large authoring/migration cost across
  all skills and duplicates SKILL.md. SKILL.md injection is the
  single-source-of-truth starting point; a structured field is possible future
  hardening.
- **Always-confirm** (block every exact-skill run) / **recommend-and-go** (never
  block). Both rejected in favour of the conditional four-part rule — always
  show, block only on consequential ambiguity — faithful to CONTEXT.md's "when
  needed" and ADR 0014's one-focused-question rule.
- **Bespoke "analysis recommendation" payload + new desktop card.** Deferred:
  best UX but a new contract + UI component; reuse the existing preflight channel
  first.

## Consequences

- `omicsclaw/runtime/agent/loop.py`: the understanding preflight fires for
  `EXACT_SKILL` (assist) too; a new **assisted-parameterization directive**
  (analog of `_AUTONOMOUS_UNDERSTANDING_DIRECTIVE`) is injected together with the
  SKILL.md menu and the inspect_data schema.
- A helper resolves and loads the matched skill's `SKILL.md` content (the
  registry already knows each skill's directory).
- `omicsclaw/analysis_router/dispatcher.py` / `build_analysis_tool_plan`: `auto`
  gains deterministic method extraction (honor an explicitly named method);
  `assist` exact-skill no longer auto-executes a default-method plan — the outer
  loop owns parameterization.
- The structured preflight channel carries the method confirmation; the desktop
  preflight-guidance surface renders the blocking case with no new UI required.
- Latency / cost: in `assist`, one `inspect_data` round-trip plus the
  recommendation turn per exact-skill analysis (same order as ADR 0014's accepted
  cost). `auto` stays single-shot.
- Fixes the user-visible regressions: silent default-method execution and dropped
  explicitly-named methods.

## Open questions

- **Partial skill match.** Should the skill step of a Partial route also get
  assisted parameterization, in addition to ADR 0014's autonomous continuation?
  ADR 0014 left the partial inspection surface open.
- **Structured `methods` metadata.** Whether to later add a machine-readable
  per-skill method menu (frontmatter + registry) to drive a richer desktop
  method-picker and to validate `--method` before a run.
- **`auto` named-method extraction.** The exact parsing locus and how it
  reconciles with the close-tie disambiguation and `_infer_skill_for_method`.
- **Cost control.** Whether to make the `inspect_data` step conditional (skip
  when the skill has a single method or no data-dependent choice) if per-run
  latency proves heavy.
