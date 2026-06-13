# Introduce `omicsclaw/runtime/consensus/` as a typed-then-narrative consensus layer, with thin `consensus-domains` and `sc-consensus-clustering` skills as the user-facing entries

## Status

Accepted (2026-05-18).

## Context

A grilling session on 2026-05-18 (`/grill-with-docs`, 9 rounds) examined
whether SACCELERATOR's expert-in-the-loop consensus methodology
(`/home/weige/project/repo_learn/SACCELERATOR`) could be lifted into
OmicsClaw as a cross-omics paradigm, with an in-process sub-agent
runtime layered underneath.

The session surfaced three findings that together cross the "real
concern emerged" bar for a new architectural layer:

**Finding 1 — SACCELERATOR's consensus is narrower than the README
suggests.** It is a 3-step pipeline (`consensus/01_Results_Aggregation`,
`02_BC_ranking`, `03_Consensus_{kmode,lca,weighted}`) that only works
on **categorical per-observation outputs** (cluster labels), because
the math depends on Hungarian alignment of label ids across methods.
The README itself frames it as "expert-in-the-loop, beyond traditional
ensemble/consensus methods" — the human, not the algorithm, is the
load-bearing piece. Naïve "fan out + LLM averages everything" is
explicitly rejected by SACCELERATOR's framing.

**Finding 2 — OmicsClaw's current skill outputs are already
alignment-ready.** `skills/spatial/spatial-domains/spatial_domains.py`
writes `adata.obs["spatial_domain"]` and `figure_data/spatial_*.csv`
with two fixed columns `(observation, spatial_domain)` *regardless of
which `--method` is selected* (`spatial_domains.py:413-416`). The
`SUPPORTED_METHODS` registry plus `param_hints` block in
`parameters.yaml` enumerates every method with its required arguments
and defaults. `skills/singlecell/scrna/sc-clustering/sc_cluster.py`
already runs a multi-resolution sweep with intrinsic silhouette
scoring (`sc_cluster.py:18, 203-319`) and `domain_local_purity` is
emitted by spatial-domains (`spatial_domains.py:148, 178`) — both
sides ship the cross-method ARI **and** intrinsic-quality signals
SACCELERATOR's `02_BC_ranking` requires.

**Finding 3 — OmicsClaw has no fan-out or sub-agent pattern today.**
A grep for `subagent | sub_agent | fan.out | parallel.skill` across
`omicsclaw/runtime/` and `omicsclaw/core/` returns zero hits.
The `orchestrator` skill (`skills/orchestrator/SKILL.md:48-55`) is
explicit: "No analysis is performed. This skill only emits a routing
decision." Anything that runs N methods in parallel is net-new
capability.

### Why not put it in a skill, the orchestrator, or `dispatch()` itself

Three rejected alternatives are worth remembering:

- **(b) Pure skill** `skills/consensus-categorical/` that vends its own
  fan-out internally. Rejected because (i) v2 RRA on differential
  expression and v3 interval consensus need the same fan-out machinery
  and would copy-paste it, and (ii) a skill that spawns its own
  sub-skill processes bypasses the `dispatch(envelope) -> AsyncIterator[Event]`
  contract that ADR 0006 just accepted.
- **(c) Extend `orchestrator`** with a `--consensus typed` mode.
  Rejected because the orchestrator's SKILL.md is explicit that no
  analysis runs there; adding fan-out would conflate routing with
  execution and re-introduce the same dispatch bypass as (b).
- **(d) Push fan-out into `dispatch()` itself** via an
  `envelope.consensus_plan` field. Rejected as over-engineering of a
  contract that landed three days ago (ADR 0006), and because no
  Surface today needs fan-out as a first-class concept.

### Why not "complete LLM agent per member"

An alternative subagent runtime pattern would treat each member as a
**complete LLM agent** with a prompt loop, tool-use, and per-member
agent profile / iteration budget. For categorical consensus v1 every
member is deterministic — `python spatial_domains.py --method <X>
--output <dir>` produces a fixed-schema `labels.tsv` and exits.
Per-member LLM cost ×N is wasted; the only valuable parts of an
external sub-agent runtime are the `asyncio.gather(_run_member)`
parallel skeleton (~50 lines) and the `cancel_event` propagation
pattern. Both can be lifted as **inspiration**, not as a vendored
dependency.

### Why not a `Gateway → Redis → Worker → EventBus` pipeline

ADR 0003 and ADR 0006 §3 already rejected this shape twice for being
"speculative infrastructure that no caller required" on a single-machine
single-user research tool with the explicit "Genetic data never leaves
this machine" constraint (CLAUDE.md §Safety Rules). The consensus
runtime is **in-process asyncio fan-out**, not cross-process.

## Decision

Introduce `omicsclaw/runtime/consensus/` as a new in-process runtime
sub-package, with two thin user-facing skills layered on top:

```
omicsclaw/runtime/consensus/
├── __init__.py
├── team.py                 ← parallel skill-subprocess fan-out (asyncio.gather)
├── member.py               ← ConsensusMember = (skill, params, expected_artifact, intrinsic_quality_key)
├── plan.py                 ← evaluation-chair LLM picks members from parameters.yaml param_hints
├── operators/
│   ├── __init__.py
│   ├── alignment.py        ← Hungarian via scipy.optimize.linear_sum_assignment
│   ├── categorical.py      ← kmode + weighted (pure Python; ~70 lines combined)
│   └── lca_r/
│       ├── consensus_lca.r ← ported from SACCELERATOR with author attribution
│       ├── env.yaml        ← conda recipe (klaR + poLCA)
│       └── wrapper.py      ← subprocess driver, TSV ↔ DataFrame
├── narrative/              ← B-path (exploratory) synthesis
│   ├── extractor.py        ← per-member LLM extraction → JSON with confidence field
│   ├── synthesizer.py      ← N JSONs → narrative report with contradiction annotation
│   └── prompts/
│       ├── extract.tmpl
│       └── synthesize.tmpl
├── dispatch.py             ← typed-vs-narrative router via TYPED_CONSENSUS_REGISTRY
└── scoring.py              ← composite member score (see ADR 0011)

skills/
├── spatial/consensus-domains/             ← thin CLI wrapper → runtime/consensus
└── singlecell/scrna/sc-consensus-clustering/  ← thin CLI wrapper, multi-resolution leiden/louvain
```

Plus an explicit allowlist:

```python
# omicsclaw/runtime/consensus/dispatch.py
TYPED_CONSENSUS_REGISTRY: set[str] = {
    "spatial-domains",        # v1
    "sc-clustering",          # v1
    # v2: "spatial-de", "sc-de", "bulkrna-de", "proteomics-de", "metabolomics-de"  (RRA)
    # v3: "genomics-variant-calling", "genomics-sv-detection"  (interval merge)
}

def select_consensus_mode(skill_name: str, force_mode: str | None) -> Literal["typed", "narrative"]:
    if force_mode:
        return force_mode
    return "typed" if skill_name in TYPED_CONSENSUS_REGISTRY else "narrative"
```

### Vocabulary (forward-declared; will migrate to `omicsclaw/runtime/consensus/CONTEXT.md` once code lands)

**Typed consensus (A path)**:
Statistical consensus over comparable per-observation outputs. The
LLM picks members and narrates results; a typed operator (kmode / LCA
/ weighted) does the math. The only path whose output is marked
"verified" in reports and graph memory.
_Avoid_: "strict consensus", "hard consensus".

**Narrative consensus (B path)**:
LLM-mediated synthesis of N free-form skill reports via per-member
extraction + cross-member synthesis with explicit contradiction
annotation. Output is marked "exploratory" and lives under a separate
graph-memory namespace from typed results.
_Avoid_: "soft consensus", "LLM consensus" (overloaded — the LLM is in
both paths but does different jobs).

**Consensus member**:
A `(skill, params, expected_artifact, intrinsic_quality_key)` tuple
representing one fan-out target. Each member runs as a deterministic
skill subprocess; not an LLM sub-agent.
_Avoid_: "sub-agent" (overloaded with the LLM-agent connotation common
in other agent frameworks).

**Evaluation chair**:
The LLM role responsible for (i) picking which members to fan out
based on `param_hints` + data features, and (ii) narrating the typed
operator's output. The chair has no statistical synthesis authority —
mode-voting math is delegated to the operator. Mirrors SACCELERATOR's
"expert-in-the-loop" with the LLM as the expert.
_Avoid_: "judge", "synthesizer", "orchestrator" (already used for the
routing skill).

**Base clusterings (BC)**:
The subset of members the user selects in the post-run interactive
step (CLI-only in v1) to feed into the typed operator. Direct
analogue of SACCELERATOR's `02_BC_ranking` output. On non-CLI
Surfaces, defaults to top-K by composite score (see ADR 0011).
_Avoid_: "selected methods", "chosen clusterings".

**`TYPED_CONSENSUS_REGISTRY`**:
The explicit allowlist of skill names that have a typed operator.
A skill not in this set is automatically routed to the B path.
New skills must be registered explicitly — there is no implicit
"output schema sniffing".
_Avoid_: "consensus-eligible flag", "consensus capability".

**`analysis://typed/<run_id>` vs `analysis://exploratory/<run_id>`**:
Graph-memory namespace split. Future meta-analysis and paper
reproductions default to reading only `typed/*`; `exploratory/*`
holds B-path output and is gated behind explicit user opt-in.
_Avoid_: collapsing the two; using `analysis://` without a sub-prefix.

### Operational defaults (locked in this ADR)

| Concern | Default |
|---|---|
| Member planning | LLM picks 5 from `param_hints`; `--members` overrides; `--all` fans out everything |
| Expert-in-the-loop | CLI surface only (synchronous prompt after fan-out); Desktop/Channel use top-K by composite score |
| Pre-run plan confirm | Off by default; opt-in via `--confirm-plan` |
| `n_clusters` target | Operator returns however many clusters the math yields (`categorical` bounds it at `max(member ks)`). The `--n-clusters` flag is **reserved: accepted but not consumed** and overrides nothing today (disposition pending plan 0025 DEC-5) |
| `--llm-judge` | **Reserved: accepted but not consumed** — a chair-LLM veto/reweight is not wired today (disposition pending plan 0025 DEC-5) |
| Operator language | kmode + weighted in Python (scipy); LCA via R subprocess (port from SACCELERATOR) |
| Failure semantics | <2 surviving members → error; ≥2 → continue and annotate "N/M members failed" |
| Concurrency | `max_parallel = min(N, os.cpu_count() // 2, 4)` |
| Per-member timeout | 600s |
| Cancellation | `envelope.cancel_event` from ADR 0009 propagates into `team.run()` and on into each subprocess via `skill.runner.run_skill()` |
| Failure → B fallback | **Off**. A path is allowed to fail loudly; never silently downgrade to narrative |
| Output banner | Reports start with `[A: Verified consensus]` or `[B: Exploratory synthesis — NOT statistical consensus]`; not configurable |

## Consequences

### Positive

- v2 (DE-RRA) and v3 (interval merge) add a new operator + new thin
  skill but reuse `team.py` / `member.py` / `plan.py` / `dispatch.py`
  unchanged. Zero copy-paste growth.
- ADR 0009's cancel chain extends through `team.py` for free — each
  member is a `skill.runner.run_skill()` call which already does
  `killpg` on the process group.
- The `TYPED_CONSENSUS_REGISTRY` allowlist makes the "verified vs
  exploratory" boundary auditable from a single file.
- Reports and graph-memory writes carry the verified/exploratory
  banner explicitly — screenshots and downstream meta-analyses
  cannot lose the provenance.

### Negative

- A new layer to learn. Contributors writing a new skill must decide
  whether to register it in `TYPED_CONSENSUS_REGISTRY` and, if so,
  ensure their skill's output schema is consensus-compatible.
- **v1 operators are conceptually-compatible Python simplifications,
  not bit-exact ports.** kmode = per-row mode after Hungarian alignment
  (SACCELERATOR uses `diceR::k_modes`, an iterative refinement);
  weighted = weighted-majority vote (SACCELERATOR uses EnSDD, an
  NMF + Leiden ensemble). The headline OmicsClaw contribution (LLM
  evaluation chair + verified/exploratory namespace split) is independent
  of operator-level equivalence; a future ADR may revisit if a
  research finding requires diceR-fidelity.
- LCA path adds an R subprocess dependency (3-5s cold start). Mitigated
  by being the rare-path operator — kmode and weighted are pure Python.
- External sub-agent runtimes were considered as inspiration, not
  vendored — we own ~50 lines of asyncio.gather skeleton outright.
- Desktop and Channel Surfaces lose the interactive post-run BC picker
  in v1 (they fall back to top-K by score). v1.x will add this back
  via a new `consensus_plan_proposed` dispatch event type per ADR 0006.

### Open

- **SACCELERATOR upstream LICENSE check.** The R operator scripts
  (`consensus/03_Consensus_kmode/Consensus_kmode.r`,
  `Consensus_lca.r`, `Consensus_weighted.r`) and the BC-ranking script
  carry `Author_and_contribution: Jieran Sun & Mark Robinson` headers.
  `LICENSE.txt` at the SACCELERATOR root must be reviewed for
  GPL/MIT/Apache compatibility before porting. If incompatible,
  reimplement in Python from the algorithm description rather than
  porting code.
- **`sc-cell-annotation` consensus** is deferred to v2. Its three
  methods (markers / celltypist / knnpredict) take heterogeneous
  helper arguments (`--model`, `--reference`, `--marker-file`) that
  require non-trivial evaluation-chair work to auto-select. v2 will
  spend an ADR on that.

## Relationship to prior ADRs

- **ADR 0005** (Surfaces umbrella): the consensus runtime is invoked
  by the existing thin-skill subprocess path, which all three Surfaces
  already reach via `dispatch()`. No new Surface code in v1.
- **ADR 0006** (`dispatch(envelope) -> AsyncIterator[Event]`): v1 adds
  no new dispatch events; v1.x will add `consensus_plan_proposed`
  for Desktop/Channel interactive BC selection.
- **ADR 0009** (cancel_event wiring): `team.run()` accepts
  `envelope.cancel_event` and forwards it into each
  `skill.runner.run_skill()` call. No new cancellation infrastructure.
