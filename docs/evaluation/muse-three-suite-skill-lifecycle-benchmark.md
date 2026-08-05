# MUSE-aligned three-suite Skill lifecycle benchmark pilot

> Status: measured on 2026-08-02. This is a partial-coverage Skill conformance
> pilot over OmicBench, scAgentBench, and BiomniBench-DA. It is not a completed
> Agent lifecycle Campaign and does not report a full-suite score.

## 1. What is being measured

MUSE-Autoskill separates Skill creation, memory, management, evaluation, and
refinement. Its main SkillsBench comparison uses `without skills`, `human
skills`, and `self-created skills` under one model/runtime, retains every task
in a fixed denominator, and scores missing, uncovered, or failed runs as zero.
Memory, transfer, and stability are reported as separate experiments in the
paper and appendices.

OmicsClaw therefore keeps two evidence planes separate:

| Plane | Question | May change one Skill's validation evidence? |
| --- | --- | --- |
| Per-Skill Evaluation Protocol | Does one exact Skill revision satisfy one declared scientific case and grader? | Yes, subject to declared-level and human-governance caps |
| Suite-level Benchmark Campaign | Does an Agent condition improve over a frozen case x condition x repeat matrix? | No; it is comparative Agent evidence |

All results currently in this document are on the first plane. No claim about
the benefit of `curated_skill`, `self_created_skill`, memory, refinement, or
transfer is supported until the second plane is executed.

## 2. Suite inventory and data-integrity policy

The selected scientific bytes were content-bound and re-verified before and
after every protocol. On this Linux host, the evaluator also mounted each
selected dataset path read-only with bubblewrap; platforms without a usable
bubblewrap fall back to the same before/after digest guard. Skill inputs and
generated artifacts were written to fresh scratch paths and the local evidence
store.

Some suite trees already contained historical `__pycache__` files from older
direct grader imports. Current evaluators set `PYTHONDONTWRITEBYTECODE=1`, and
generated `__pycache__/*.pyc` is excluded from the scientific digest. The claim
is therefore that the selected scientific digest did not change, not that no
filesystem metadata anywhere below a benchmark checkout has ever changed.

| Suite | Complete denominator | Local size | Case-list SHA-256 | Executed conformance slice |
| --- | ---: | ---: | --- | ---: |
| OmicBench | 44 | 10 GiB | `580871a6558800525c7eeaf8b3bd672abc01b0aa8c760b40e89b30f6122b5c77` | 2/44 |
| scAgentBench main | 50 | 68 GiB | `67bf9a368af2dfb1332d0e85f6fa1a0bbf5bd7770b331bce3f24b392a0acad3a` | 1/50 |
| BiomniBench-DA public tasks | 50 | 78 GiB | `38cab35b78d692e4ef23928f494d9a92f5f719da3927cbafdf114ff5be81c46f` | 1/50 |

The case-list hashes bind sorted case identifiers, not all suite bytes. Each
executed protocol separately binds and re-verifies its selected data, prompt,
oracle, and grader bytes before and after execution. Scores from the three
suites are not pooled because their graders measure different quantities.

## 3. Measured Skill conformance results

### 3.1 OmicBench: `sc-preprocessing`

Both cases ran in one governed evaluation batch. Native grader checks and
adapter-owned axis-identity invariants passed.

| Case | Native result | Dataset digest | Protocol digest | Result ID |
| --- | --- | --- | --- | --- |
| A02 normalize/log | 4/4, score 1.0 | `sha256:beddcd538b9584af7dac0be911ad265382b50ed8dcce89ca2456261297d4635e` | `sha256:ffe009cd1f5da0feb01d69ffbdafeeb7aa35b2f094da53d041588ddb82260601` | `b448bbccae97485896a7b54617c9d3b5` |
| A03 HVG | 3/3, score 1.0 | `sha256:acac3a34caf59663b62fe85d78dfdbc1e25e831e46b81f6f3c948f801b756b68` | `sha256:e577c7dbbfc4fa9a45fc11a83c4b4618088da1424c712154265ca18859b611d0` | `a9087e0bc40747f3b06ef5e6d16ea696` |

- Evaluation ID: `9512275bfe134a899a933420a63f8660`
- Environment: `sha256:a9155407f4b5022dc029e258649aa150835ecfffac76a1c64e6ac15fe9af95e6`
- Exact revision: manifest `sha256:cad704629fcc61d21bdf0e928a475d45118b494efbd3cafa2ffad873b2b65299`, source `sha256:ab46fe754c91eef975029cc7b5ff562cc64bf3a056eceec54207184329f3c2a6`
- Artifact bundles: `evaluation-artifact:sha256:9a12e8fe...705feb`
  and `evaluation-artifact:sha256:12b07d6f...38d655`
- Evidence-supported level: `benchmarked`; effective level: `smoke-only`
- Raw byte-tree hashes remained `d6d816b8...80460` for A02 and
  `9936e725...e8674` for A03.

Full diagnostic history is in
[the OmicBench baseline](omicbench-skill-lifecycle-baseline.md).

### 3.2 scAgentBench main PAGA: `sc-pseudotime`

The adapter converted the suite's legacy Scanpy HDF5 layout into the real
OmicsClaw AnnData/matrix contract, reproduced the Paul15 preprocessing and
root-cell choice, and invoked the shared runner with `method=dpt`.

| Metric | Value |
| --- | ---: |
| Clusters | 24 |
| Aligned row cosine | 0.9999983986 |
| Edge F1 at 0.03 | 1.0 |
| Relative Frobenius error | 0.0033050756 |
| Published all-row-pairs cosine | 0.1085998188 |

The published native calculation averages every gold row against every output
row. Gold compared with itself therefore also produces about 0.1086 instead of
1.0. OmicsClaw retains that value for comparability, but uses aligned cosine,
edge F1, shape, symmetry, finiteness, and zero diagonal as its conformance gate.
This is not an official scAgentBench suite score.

- Protocol: `scagentbench-paga-main-v1`
- Dataset bundle: `sha256:29c073c6ee4c4f2f62e295222463c2692b2550e235ac5c375b28798eb7cbc93a`
- Protocol digest: `sha256:a18261c265fea41e2d6d42c922408889b3f6b0cb9eda99375cc0f3460a644a6f`
- Evaluation/result IDs: `9ae436186c2b4b47aab702dfb4e715d5` / `d32999ffc8484a299436d35d0d5def5b`
- Environment: `sha256:46fac4f8fd8142073248a593b6a9b490cb380c04464965439f54ce8ae25c8ce2`
- Exact revision: manifest `sha256:cc910f6d6063277a4e7b460a5f47a140f32d07691cd742416576fe3f981b98bf`, source `sha256:9604db13a90d2dd3faacd9d06a1690fb532d5698281083a096804f8e9701c74a`
- Artifact bundle: `evaluation-artifact:sha256:84a775dc...356e1`
- Evidence-supported level: `benchmarked`; effective level: `smoke-only`

### 3.3 BiomniBench-DA 12-2: `bulkrna-enrichment`

The official verifier requires an external Gemini or Anthropic LLM judge. No
`GEMINI_API_KEY`, `GOOGLE_API_KEY`, or `ANTHROPIC_API_KEY` was configured, so
the official judge was not run and no official Biomni score is reported.
Instead, a deterministic, rubric-aware preflight was registered as `fixture`.

| Metric | Value |
| --- | ---: |
| Shared unique DEG symbols | 1543 |
| Supplied Hallmark sets / returned pathways | 50 / 49 |
| Explicit Hallmark universe / query in universe | 4384 / 400 |
| Significant pathways at FDR < 0.05 | 3 |
| G2M overlap | 37/200 |
| G2M raw p-value | 1.689555969e-05 |
| G2M Benjamini-Hochberg FDR | 2.759608083e-04 |
| G2M rank | 3/49 |

The adapter discloses that constant DE columns are membership sentinels needed
by the current Skill input Interface; they are not TS7 effect sizes or p-values.
The ORA universe is passed explicitly, so the result no longer depends on an
implicit GSEApy background default. The generated report records
`official_llm_judge_score: null`.

- Protocol: `biomnibench-da12-2-deterministic-preflight-v1` (`kind: fixture`)
- Dataset: `sha256:00c42c92536a615b7127e69c4691299e890a23264e503255cd5ba4081248e3e9`
- Protocol digest: `sha256:db7fa419a3bd3df5748a520f7abaf2fa8ec0d1752817d4c94ef43d167d0f63b3`
- Evaluation/result IDs: `f915e197948f4ac9877485ae73878961` / `a6848ede024c4612891f3b3bf6cc94f5`
- Environment: `sha256:091d48d83a88b940eff840de890e97a0d6b56478f52432f5e0c7db34a30be7dc`
- Exact revision: manifest `sha256:e68948ea892611a0565d92cc64029fa674af8d8509b86d43061faa05b2b86ed7`, source `sha256:b3c309dcf2496b26e3c90f2ef5ccfe88e1a7b15d478b2a46dcd6fb8ffa0000c7`
- Artifact bundle: `evaluation-artifact:sha256:baac06b1...9e30e`
- Checks: 10/10; evidence-supported level: `fixture-validated`; effective level: `smoke-only`

## 4. Lifecycle improvements made by this pilot

### Evaluation Dataset bundles

`EvaluationDatasetRef.members` can bind only the prompt, input, oracle, and
grader needed for one large-suite case. Members must be unique,
non-overlapping, present, regular files/directories, and symlink-free. Directory
enumeration errors fail closed. A joint before/after metadata snapshot now
prevents an earlier member from changing while a later member is being hashed.

### Benchmark Campaign Module

`omicsclaw.skill.benchmark_campaign` is a separate suite-level Module. Its
Interface freezes suite/subset identity, every selected case and grader digest,
an explicit `experiment_kind`, conditions, repeats, pass threshold,
model/runtime/Agent/environment/tool-policy/budget identities, per-condition
configuration digests, and a coverage-manifest digest plus exact anchor case
IDs. It then:

- retains every case x condition x repeat cell in the strict denominator;
- counts missing, uncovered, unsupported, timeout, setup, infrastructure, and
  other non-graded cells as zero;
- requires a real graded record before a zero-threshold score can pass;
- reports each condition's own coverage separately from strict score;
- computes anchor-covered scores from the pre-frozen case set rather than from
  whichever Phase-2 records happen to exist;
- rejects model/runtime/Agent/environment/tool-policy/budget/config drift and
  Skill revision drift across repeats;
- requires covered Skill failures as well as successes to bind exact revisions;
- requires each attempted run to bind unique trial/isolation identities and a
  content-addressed artifact bundle; graded runs also bind grader evidence;
- permits `creation_failed` only as a `self_created_skill` uncovered zero;
- requires memory on/off to retain the same exact Skill revision, and refinement
  R0/R1 to retain the same Skill IDs;
- preserves bounded failure reason codes for diagnose/refinement;
- reports measured-run counts beside latency, tokens, turns, and cost;
- reports repeat mean, population standard deviation, MAD, non-constant cases,
  and low-variance cases.

The main experiment accepts only `no_skill / curated_skill /
self_created_skill`; memory, refinement, and transfer use separate fixed
condition sets. Offline reports are explicitly labelled
`matrix-integrity-only` and `causal_claim_status=not-established...` because the
execution harness needed to prove those causal provenance chains is not yet
implemented. Campaign results never enter a Per-Skill Experience View as
validation evidence.

### Evaluation Artifact Store

Current governed evaluations copy bounded stdout, stderr, result envelopes, and
hash-matched verifier evidence into an immutable local content-addressed store
before scratch cleanup. The result row carries an opaque bundle reference, and
artifact capture failure fails the evaluation closed. The default store sits
beside the evaluation JSONL and may be overridden by
`OMICSCLAW_EVALUATION_ARTIFACT_STORE`. Capture is bounded to 64 MiB per object
and 4,096 enumerated files; oversized or unmatched requested evidence is marked
unresolved in the bundle. The r7 results above retain four verified bundles
containing stdout, stderr, result envelopes, and matching grader reports/traces;
all four report zero unresolved evidence references. The local ignored snapshot
is `output/skill-lifecycle-benchmark-20260802-governed-r7/`; its 33-file
`MANIFEST.json` has SHA-256
`4fad3edcd0409104ac17d7ddeedf691d46a8abdc468e0c546516c424993d6cfd`.
It was produced by `scripts/run_three_suite_skill_lifecycle_benchmark.py`, which
calls the existing governance evaluator and adds no second grading path.
This store is not yet a RunRuntime AuditOperation retention/GC or
cancel/resource Interface.

From an installed OmicsClaw environment, reproduce the fixed pilot from the
repository root with a fresh, non-existing output path:

```bash
PYTHONDONTWRITEBYTECODE=1 python scripts/run_three_suite_skill_lifecycle_benchmark.py \
  --output output/skill-lifecycle-benchmark-<run-id>
```

### Diagnose findings

| Suite/module | Reproduced failure | Fix or disposition |
| --- | --- | --- |
| OmicBench A03 | categorical fill, AnnData index-name collision, and grader-blind feature-axis replacement | nullable string handling, axis preservation option, strict adapter invariants |
| scAgentBench PAGA | legacy non-h5ad HDF5 and `louvain` importing removed `pkg_resources` | dedicated loader; `setuptools>=64,<82`; real matrix contract |
| BiomniBench-DA | custom GMT could silently enter an incompatible R path; R fallback could change ORA into GSEA; implicit background and malformed report overlap | custom-set backend seam, method-preserving fallback, explicit universe, FDR sorting, correct `k/K` rendering |
| Dataset bundle | unreadable subtrees and cross-member drift could escape a digest | fail-closed enumeration and joint selection snapshot |
| Evaluator import/runtime | command graders failed outside a manually exported `PYTHONPATH`; PAGA produced wrong metrics under base Python | governance-owned framework root; exact Skill-runner environment identity; correct OmicsClaw conda rerun |
| R dependency probe | the Seurat test inspected base `Rscript` while production used the OmicsClaw R environment, so missing `rhdf5` surfaced only inside `zellkonverter` | probe through `RScriptRunner`, require `rhdf5` in preprocessing preflight/tier metadata, and classify it as Bioconductor |
| Evaluator cancellation | parent Ctrl-C could leave a protocol process group running | real-process regression; TERM/KILL cleanup before re-raising the interrupt |
| Campaign analysis | zero threshold marked failures as pass; covered scores used different/post-hoc subsets; failed Skill runs could omit revisions | graded-only pass, pre-frozen anchor manifest/cases, exact attempt evidence, and revision-bound failures |
| Benchmark CLI | direct `python scripts/...` execution could not import `omicsclaw` without an editable install | repository-root bootstrap plus direct-help regression tests; because project scripts are part of the exact source closure, the final governed evidence was rerun as r7 |

## 5. Pre-registered Agent Campaign design

The next Agent-level experiment must freeze a Campaign before execution. A
bounded pilot may reuse the 2/44, 1/50, and 1/50 slices above, but must call them
`preregistered_subset`, not full-suite runs.

### Main Skill effect

- Conditions: `no_skill`, `curated_skill`, `self_created_skill`.
- Same model, Agent revision, runtime, tool policy, environment, budget, and
  grader per suite.
- Five independent repeats for stochastic Agent runs; deterministic command
  graders may declare one repeat.
- Phase 1 creates a candidate only from a successful source trajectory. Phase 2
  evaluates it in a fresh environment. Creation failure or no source trajectory
  remains an uncovered zero.
- OmicsClaw adds held-out cases where available to reduce the same-task
  overfitting risk acknowledged by MUSE.
- The `self_created_skill` condition is the coverage anchor. Every condition's
  anchor-covered score uses that exact common subset; it never replaces the
  strict full selected denominator.
- Phase 2 must bind a creation manifest generated after Phase 1, not infer
  coverage from Phase-2 success rows. The future harness must also prove the
  source trajectory, creation operation, exact package, and fresh trial.

### Orthogonal experiments

| Experiment | Required comparison | Separation rule |
| --- | --- | --- |
| Memory | `memory_off` vs `memory_on`, two-pass task-scoped sequence | No oracle, expected answer, final output, raw omics data, or patient data in memory |
| Refinement | exact R0 vs evidence-bound R1 on held-out + regression cases | OmicsClaw extension; MUSE did not publish a standalone refinement ablation |
| Transfer | local package-only source runtime -> target runtime | No Experience View or raw data transfer; model changes reported separately from runtime changes |
| Cost/stability | creation cost plus per-reuse cost; repeat std/MAD | Token data is diagnostic when provider traces are incomplete |

Suite-native scores remain separate. A later normalized macro summary must be
defined before execution and cannot replace any suite's primary table.

The current analyzer can verify the frozen matrix, identities, artifact
commitments, denominator, and allowed experiment axes. Before any causal result
is published, the execution harness still must add typed provenance for the
Phase-1-to-Phase-2 creation chain, memory round/state chain and leakage policy,
R0-to-R1 refinement parent/evidence/held-out relation, and transfer
source-package-to-target relation. Opaque condition digests alone are not proof
that only the intended experimental variable changed.

## 6. What is not complete

- No main Agent Campaign matrix has been executed, so all Agent-condition lift,
  memory, refinement, and transfer effects are `not measured`.
- Coverage remains 2/44, 1/50, and 1/50; unexecuted suite tasks have no implied
  positive score.
- Biomni's official LLM judge is blocked by missing judge credentials. The
  deterministic DA-12-2 preflight cannot earn `benchmarked`.
- The r7 rows retain bounded artifacts, but AuditOperation-owned retention/GC
  and observation remain deferred.
- Dataset-backed command protocols use a bubblewrap read-only dataset mount and
  PID namespace on this Linux host, with a before/after digest fallback where
  bubblewrap is unavailable. This is not a complete OS sandbox or a RunRuntime
  AuditOperation.
- Campaign typed causal provenance and its execution harness remain incomplete;
  offline summaries are matrix-integrity analyses only.

## 7. Interpretation

The pilot verifies that four selected cases across three heterogeneous suites
can flow through one evidence-bound lifecycle without changing benchmark data
or bypassing the shared runner. It does not yet demonstrate that Skills improve
Agent task performance. That claim requires the pre-registered Campaign
conditions above.

OmicsClaw is a research and educational tool for multi-omics analysis. It is
not a medical device and does not provide clinical diagnoses. Consult a domain
expert before making decisions based on these results.
