# EVO-G2 independent Ask Codex review

Date: 2026-07-16

Scope: evidence-bound Gotcha governance from shared-runner evidence capture to
canonical `SKILL.md` writeback, live Registry publication, runtime retrieval,
and the Desktop Backend HTTP contract. OmicsClaw-App was deliberately out of
scope because it owns only thin proxy, view-model, and UI materialization.

## Round 1 — NO SHIP

- Model: `gpt-5.6-sol`, reasoning effort `xhigh`, read-only sandbox.
- Session: `019f6af5-2fd5-7422-b2d8-fe71921611cf`.
- Verdict: `NO SHIP` with 2 High and 5 Medium findings.

The review found that Evolution HTTP authorization inherited the remote
local-development no-token bypass; Registry reload could publish an empty or
partial candidate; unresolved caller text could enter `skill_id`; request
models ignored forbidden extra fields and narrative filtering missed quoted
paths, forward-slash Windows paths, and non-HTTP URI schemes; target and
non-routable provenance drift were incomplete; the HTTP tracer replaced the
production execution adapter; and the documentation consequently overstated
completion.

## Remediation

| Finding | Closed by | Regression evidence |
|---|---|---|
| Evolution auth bypass | A dedicated Evolution dependency requires an explicitly configured Bearer token. Unset/blank returns 503; missing/wrong credentials return 401. Other remote routes retain their local-first contract. | All 7 Evolution routes x 5 token states, plus the unchanged remote-route control. |
| Empty/partial Registry publication | Reload builds and validates a fresh candidate, rejects missing/non-directory/empty roots, unresolved declared entries, incomplete manifest inventory, and canonical/alias collisions, then publishes with one state swap. | Missing root, empty root, unrelated missing runtime, recursive inventory drift, duplicate canonical/alias, and old-state identity preservation. |
| Caller-controlled `skill_id` | Run events use a registry-derived canonical id; unresolved pre-registry values become a one-way `unresolved-<digest>` identity. | Direct event and public unknown-Skill runner tests prove paths and credential text are absent from the ledger. |
| HTTP/narrative fail-open | Every Evolution request model forbids extra fields. The narrative gate rejects generic `scheme://` URIs and POSIX/Windows absolute paths across quote forms. | Model and HTTP extra-field matrices plus quoted POSIX, `C:/`, `ftp://`, and `s3://` cases. |
| Incomplete Gotcha drift | Draft and pending proposals bind target path/content as well as manifest/source. Approved review snapshots include explicit non-routable, consensus, missing source, missing target, and missing manifest states. | Content drift, relocation, materialization bypass, deprecation, consensus, missing source/target/manifest, and idempotent review tests. |
| Fake public tracer | The tracer now uses the default Backend governance factory, the production `SharedRunnerEvolutionExecutionAdapter`, real shared-runner ordinary/demo executions with valid result envelopes, the durable ledger/store, and live singleton Registry publication. | Bearer HTTP `refresh -> materialize -> approve`, exact trace evidence, canonical write, live runtime context, audit snapshot, and byte-stable unrelated projections. |
| Documentation overclaim | ADR 0069, the design assessment, README milestone text, and this review record distinguish implemented evidence from remaining system scope and the non-green full repository run. | Documentation facts plus generated-artifact checks. |

The combined fixes exposed one additional interaction: because evidence drafts
are target-bound, an approved Gotcha changes its own target content. A stable
exact-source cluster identity now suppresses immediate re-nomination of that
already materialized cluster, while genuine source, manifest, environment,
error-kind, or trace-anchor drift remains reviewable. Stale unapproved target
revisions still receive a new target-bound evidence draft.

## Verification snapshot before Round 2

- 507 focused cross-layer tests passed.
- 95/95 `skill.yaml`, full Skill lint, and generated `SKILL.md` checks passed.
- `catalog.json` is current at 95 Skills; `skill_dag.json` is current at 95
  nodes and 74 edges.
- Requires audit reports 0 missing dependencies and 7 existing extra warnings.
- The eight-domain routing oracle passes every metric at 1.000 with zero alias
  hallucination.
- Relevant Ruff, compileall, generated catalog/DAG tests, and `git diff
  --check` passed.
- The prior repository-wide run selected 4,831 tests and reached 99% before a
  native segmentation fault in the optional spatial-registration scientific
  stack; it also contained unrelated control/provider/science failures. It is
  not represented as a green gate.

## Round 2 — NO SHIP

- Model: `gpt-5.6-sol`, reasoning effort `xhigh`, read-only sandbox.
- Session: `019f6b23-e500-7e40-a256-c107b20f5291`.
- Verdict: `NO SHIP` with 1 High and 1 Medium finding.

The High finding reproduced a provenance hole for all 34 nested single-cell
Skills: the source-identity helper looked only for
`singlecell/{scrna,scatac}/_lib`, while the real shared tree is
`singlecell/_lib`. A shared helper revision could therefore leave old evidence
apparently current. The Medium finding showed that punctuation-prefixed
POSIX/Windows/UNC paths, underscore-prefixed URIs, common normalized credential
keys, and Markdown underscore emphasis could still pass narrative validation.
The reviewer independently reproduced both behaviors with direct read-only
probes. Its focused pytest invocation could not collect because the review
sandbox exposed no writable temporary directory; no pass count from that
sandbox is used as evidence.

## Round 2 remediation

- Execution-source identity now walks outward from the Skill directory to the
  nearest ancestor-level `_lib`. Real nested `scrna` and `scatac` fixtures prove
  both the Skill Python tree and the actual domain library affect the digest.
- Governance regressions prove a nested domain-library revision stales a
  pending evidence draft and creates a linked, non-approvable provenance review
  for an already approved Gotcha.
- URI matching is token-independent; POSIX, UNC, and drive-qualified absolute
  paths are rejected across punctuation; credential keys are normalized by
  removing `_` and `-` before matching secret families; Markdown boundary
  underscores are rejected while scientific identifiers such as `HLA_DRA`
  remain valid.
- Policy-level and authenticated HTTP tests cover every reproduced bypass and
  require HTTP 422 before governance is called.

## Verification snapshot before Round 3

- 530 focused cross-layer tests passed, including the production shared runner,
  governance, Registry, Desktop HTTP, retrieval, lint, and documentation
  contracts.
- All 95 manifests, full Skill lint, generated `SKILL.md`, catalog at 95 Skills,
  compatibility DAG at 95 nodes/74 edges, and 44 generated-artifact/document
  tests passed.
- Requires remains at 0 missing and 7 existing extra warnings. The eight-domain
  routing oracle remains 1.000 on every positive metric with zero alias
  hallucination.
- Relevant Ruff checks passed. Compile and diff checks are run immediately
  before the Round 3 snapshot is frozen.

## Round 3 — NO SHIP

- Model: `gpt-5.6-sol`, reasoning effort `xhigh`, read-only sandbox.
- Session: `019f6b41-1ac4-7123-9255-11f877cc602a`.
- Verdict: `NO SHIP` with 2 Medium and 1 Low finding.

The reviewer independently proved that the nearest-ancestor `_lib` heuristic
still lacked a canonical Skills-root/domain boundary: an unrelated outer
`_lib` affected the digest, while a nearer `singlecell/scrna/_lib` could shadow
the real `singlecell/_lib` and let genuine shared-source drift remain
approvable. It also demonstrated that Registry copy-on-write publication
covered `reload()` but not the initial `ensure_registry_loaded() -> load_all()`
path, where concurrent readers could observe one unvalidated entry. Finally,
unsorted domain/child/grandchild traversal made successful Registry ordering
filesystem-dependent.

Round 2 narrative privacy is independently closed: direct policy and ASGI
probes rejected all nine reproduced bypasses with HTTP 422, while `HLA_DRA`
remained valid. Strict Evolution auth produced the expected
`503/503/401/401/200` matrix across unset, blank, missing, wrong, and correct
credentials. The sandbox could not deliver even a trivial
`asyncio.to_thread()` completion, so the reviewer correctly treated its async
test timeout as an environment limitation rather than an OmicsClaw defect.

## Round 3 remediation

All three findings are closed. Source resolution now starts from the explicit
canonical Skills root and includes the real domain plus present subdomain
libraries without accepting an outer or shadowing `_lib`. Initial load,
lightweight discovery, reload, and DAG-cache creation all build a complete
candidate under one publication lock and publish one immutable state. Domain,
subdomain, and Skill traversal is path-sorted. Concurrent first use, root
changes, failed candidates, and stale DAG writers have dedicated regressions.

The same hardening exposed runtime consumers that read sibling Skill manifests
or dispatch through the repository entry. Formal execution revision therefore
also includes the canonical Skills tree's executable sources and manifests,
the sibling `omicsclaw/` and `scripts/` trees, and root `omicsclaw.py`. Stable
`lstat` inventories, no-follow descriptor double reads, symlink rejection,
manifest-before/source/manifest-after capture, and Registry-load manifest
revisions prevent torn or mixed identities. This project-wide revision is
intentionally conservative and can create false staleness.

## Round 4 — NO SHIP

- Model: `gpt-5.5`, reasoning effort `high`, read-only sandbox.
- Session: `019f6bbd-a5fd-7121-8a53-44dda59b9755`.
- Verdict: `NO SHIP` with 1 Blocker.

The reviewer proved that executable plan edges could come from a cached old
DAG while `graph_revision` was computed from fresh review bytes. The executor
also did not prove that the submitted graph-authority payload matched the
selected hash. A caller could therefore present execution order and authority
that never coexisted.

## Round 4 remediation and local adversarial audit

Candidate planning now parses one exact `skill_dag_reviews.yaml` payload,
builds the graph and selected plan from one frozen Registry snapshot, verifies
the bytes did not change, and hashes the normalized selected authority. Plan
schema v2 binds selected Skill id/version/manifest/source revisions, review
hash, selected graph payload, method bindings, and complete compute
reservations. The default executor verifies the submitted payload before
creating its output root and reconstructs current Skill/graph authority around
every step. Same-phase tasks are cancelled and drained after an authority
failure.

A separate local adversarial pass then reproduced seven adjacent defects:

1. rehashed resource-reservation downgrades were not part of graph authority;
2. invented methods and profiles without a real unified `--method` could run;
3. Project completion was persisted before the runner's final source fence;
4. a real `sc-clustering/tsne` request was reported as bound even though the
   runner silently filtered the unsupported `--method` argument;
5. promotion/demotion/deprecation fresh-demo source was not fenced to the
   final approval commit;
6. Bash entries and prompt/marker runtime assets were absent from source
   identity; and
7. deprecation could use defects from an older target source.

The fixes bind the full resource partition; require profile, unified flag, and
statically proven argparse value; fail closed instead of silently dropping a
method; move the runner's last source check before its terminal Project commit;
compute an expected post-manifest-transition source revision and compare it at
the final approval append; always include the runtime entry and bounded
Skill/project prompt/config assets; and bind deprecation support,
counterexamples, target, and replacement to stable source revisions. Python,
Bash, and R manifest languages now dispatch through Python, Bash, and
`Rscript`; non-Python execution skips the adaptive Python probe and reports a
missing interpreter as `missing_dependency`.

Regression coverage includes real positive `spatial-velocity/velovi` and
`sc-velocity/scvelo_dynamical` bindings, the invalid
`sc-integrate-cluster/default` profile, `sc-clustering/tsne` fail-closed
routing, resource rehash attacks, sync/async terminal persistence, Bash and
project-runtime templates, old-source deprecation evidence, exact-source
counterexamples, and Skill/domain/project source mutation during every
approval stage.

## Current verification snapshot before Round 5

- The six core code test files pass 322/322.
- The independent adversarial nine-file selection passes 392/392.
- The implementation subtask's expanded selection passes 398/398, including
  the complete 164-test evolution governance file.
- Relevant Ruff and diff checks pass on the frozen code snapshot.
- Generated representation, catalog/DAG, requires, routing oracle, compile,
  documentation, and final diff gates are rerun after this record is updated.

## Round 5 — NO SHIP

- Model: `gpt-5.6-sol`, reasoning effort `xhigh`, read-only sandbox.
- Session: `019f6c10-c5d2-7923-8fc4-86bb5b431fec`.
- Verdict: `NO SHIP` with 1 High and 1 Medium finding.

The reviewer proved that the candidate-plan method gate was sound but the
public shared-runner path still maintained two method facts. It read
`requested_method` from raw `extra_args`, filtered an unsupported `--method`
out of the subprocess argv, then reused the raw value for runtime selection,
output finalization, Project persistence, and `SkillRunResult`. Consequently,
`sc-clustering --method tsne` could execute its real UMAP default while being
reported as t-SNE. The reviewer also reproduced that the Gotcha privacy gate
accepted natural-language `API key = ...` and fullwidth
`ＡＰＩ＿ＫＥＹ＝...` credential assignments.

## Round 5 remediation

The shared runner now filters forwarded arguments before deriving method
identity. An explicit raw `--method` that does not survive the effective Skill
allow-list fails before output-directory creation or sync/async process spawn;
accepted repeated scalar flags use argparse's final occurrence. This does not
translate a generic method into `--embedding-method` or another Skill-specific
flag. Output naming, adaptive runtime, contract fallback, notebooks, Project
state, results, and audit evidence therefore share the exact argv fact.

Gotcha privacy validation now scans an NFKC-normalized shadow while preserving
the original narrative bytes. A bounded assignment-key context removes
punctuation and spacing for family comparison. Direct governance and HTTP 422
regressions cover ASCII, fullwidth, Unicode dash, and middle-dot variants, while
scientific assignments such as `HLA_DRA`, `p_value`, `secretory_marker`, and
`accessibility_score` remain accepted.

The focused RED set reproduced both findings and the extra punctuation bypass;
after remediation, its 58 cases pass. The complete runner/evolution/Desktop set
passes 339/339, and the expanded evolution/runner/DAG/plan/capability/Desktop
subset contributes 444 passing cases to the final cross-layer selection.
Relevant Ruff and diff checks pass. The final selection passes 951/951. All 95
manifests validate and pass Skill lint; requires has 0 missing and 7 declared
extras; generated SKILL.md/parameters/version, catalog, 95-node/74-edge DAG,
routing surfaces, routing budget, eight-domain oracle, and compile gates pass.

Two broader pre-existing repository gates are not represented as green: 15
dirty `skill.yaml` files are valid but not canonically serialized, and the
orchestrator-count generator expects markers absent even from the committed
`skills/orchestrator/SKILL.md`. The latter file also has an unrelated local
artifact-inventory edit. This remediation neither rewrites those Skill changes
nor treats those baseline maintenance findings as evidence for EVO-G2.

## Round 6 — incomplete review, no verdict

- Model: `gpt-5.6-sol`, reasoning effort `xhigh`, read-only sandbox.
- Session: `019f6c36-0122-75b1-b45c-7d488c165722`.
- Outcome: the session was interrupted during an HTTP probe and produced no
  final report, so it is not counted as `SHIP` or `NO SHIP`.

Before interruption the reviewer independently reproduced two additional
seams. A real YAML pipeline alias returned success for both `--method tsne`
and `--method=tsne` because pipeline dispatch occurred before the leaf Skill
argument gate and did not forward `extra_args` at all. The same mechanism also
silently discarded every other pipeline argument. The Gotcha credential-family
probe also showed that `SECRET_KEY = ...` and `secret-key: ...` were accepted:
normalization was already sound, but the canonical `secretkey` family was
absent. Local RED tests reproduced both findings at the public runner,
governance, and Desktop HTTP boundaries before implementation changed.

## Round 6 remediation

`run_skill()` now passes `extra_args` into pipeline lookup. A known pipeline is
loaded first, then rejects every non-empty forwarded-argument list before
preflight, output allocation, or step execution because the current pipeline
schema declares no typed pipeline-level or per-step binding. Unknown
`*-pipeline` aliases still fall through to the normal `Unknown skill` result;
raw argument values are never reflected in the error. The governance
credential family now includes canonical `secretkey`, covering spaced,
punctuated, prefixed, and fullwidth variants without adding a generic `key`
rule. Scientific fields such as `obs_key`, `cluster_key`, `feature_key`, and
`secret_key_gene` remain accepted.

The focused adversarial selection passes 75/75. The complete pipeline argv,
shared-runner, evolution-governance, and Desktop-evolution files pass 308/308.
An expanded 14-file cross-layer selection covering execution contracts,
evolution, DAG/plan/capability, Registry, preconditions, and scheduling passes
563/563. Relevant Ruff, compileall, documentation contracts (11/11), and
`git diff --check` pass on the new snapshot.

## Round 7 — NO SHIP

- Model: `gpt-5.6-sol`, reasoning effort `xhigh`, read-only sandbox.
- Session: `019f6c47-b0f1-79b3-84a8-03ed13b98609`.
- Verdict: `NO SHIP` with 1 Medium finding.

The reviewer confirmed the Round 6 argument and credential remediation, then
proved that a path-shaped pipeline alias could escape the canonical
`pipelines/` inventory. An absolute name ending in `-pipeline` discarded the
configured root during `Path` joining, loaded external YAML, and dispatched its
registered Skill chain under a fabricated identity. The read-only review found
no additional Blocker, High, or Low issue in its stated scope.

## Round 7 remediation and local authority audit

Pipeline lookup now accepts only bounded lowercase kebab aliases ending in
`-pipeline`, resolves the YAML path, and proves it remains under the canonical
pipeline root before reading. Inventory listing excludes noncanonical files and
symlinks resolving outside that root. Config self-identification, every step
Skill, and the chain-output basename are constrained to path-independent forms;
absolute, parent-relative, separator-bearing, uppercase, underscore, and
drive-qualified variants fail before configuration read or execution.

A separate local adversarial audit found a more serious in-process authority
gap that Round 7 did not report. `_RegistryState` and `RegistrySnapshot` shared
mutable nested dict/list/set objects, so a same-process caller could weaken a
confirmed Skill's output contract without changing its plan-bound manifest or
source revision. Registry publication now recursively clones execution
authority into standard `MappingProxyType`, tuple, and frozenset containers.
Initial, invalidated, full-load, lightweight, reload, and embedding/test
snapshot paths all cross the same deep-freeze boundary. `LazySkillMetadata`
cache objects are excluded from execution snapshots, and the compatibility DAG
cache lives outside authority state, is keyed by exact state identity, and
returns detached copies.

The publication check is not inferred from a shallow read-only wrapper. Only
the exact state object produced by the deep freezer is registered as published;
`dataclasses.replace()` and similar copies are unregistered and must be frozen
again. This preserves the shallow-wrapper regression while reducing repeated
95-Skill `snapshot()` checks from a recursive full-tree scan to constant-time
identity membership. Direct measurement on this snapshot was approximately
0.0033 ms per snapshot after the initial publication, versus approximately
9.7 ms for the discarded recursive hot-path check.

The same local authority audit then expanded beyond the original Round 7
finding. Pipeline and review YAML now reject duplicate keys at every mapping
depth and unknown schema fields. Programmatic `PipelineConfig` construction and
`run_pipeline()` share the loader invariants, every step must be a canonical
routable Registry member, duplicate steps fail before output allocation, and a
successful non-final step must produce a contained regular non-symlink baton.
The runner binds all steps to one initial `RegistrySnapshot` and complete
manifest/source revision map; the leaf runner checks that revision before
runtime resolution or spawn. `pipeline_summary.json` now persists an explicit
schema version, normalized bound revisions, deterministic pipeline-authority
digest, and each leaf's frozen audit identity. A success result without an
identity matching the bound revision fails the composite.

The external DAG cache is keyed by the exact published state identity *and* the
stable review-overlay byte hash. Review bytes changing during graph build
prevent publication, and a later review edit invalidates public DAG reads
without requiring an unrelated Registry reload. This closes the stale-DAG
variant introduced when the cache was correctly removed from immutable
authority state.

Finally, a true subprocess reproduction exposed a cross-Surface false-success
path: reusing an explicit non-empty output directory allowed an old
`result.json` with `status: ok` to override the current child's exit 7 and old
artifacts to satisfy the new contract. ADR 0070 now requires an absent or empty
directory plus a persistent mode-0600 `O_EXCL` claim before any sync/async
Skill spawn. Pipeline and candidate-plan roots are claimed as composites;
their leaves are claimed immediately before each execution, including custom
candidate runners. Stale files are never deleted. Claim markers are hidden
from public result inventories and generated README listings. This also closes
an earlier step planting a future sibling's baton/result after an initial
composite scan.

A separate local reviewer re-ran the real stale-result harness, future-sibling
injection, sixteen concurrent claimants, empty-directory compatibility,
programmatic pipeline bypasses, pipeline audit reconstruction, and Registry
drift. Its final local verdict was `SHIP` with no reproducible Blocker, High, or
Medium; its focused selection passed 322 tests with one skip. This is supporting
evidence only and does not replace the required fresh Ask Codex session.

## Round 8 — incomplete review, no verdict

- Model: `gpt-5.6-sol`, reasoning effort `xhigh`, read-only sandbox.
- Session: `019f6cb1-1cce-7860-a39f-fe29a4b8f2d4`.
- Outcome: the platform safety filter terminated the session while it was
  running local read-only probes. It produced no final report, so it is not
  counted as `SHIP` or `NO SHIP`.

Before termination, the reviewer identified three concrete seams for local
reproduction. A `RegistrySnapshot` could retain a published `_state` while
`dataclasses.replace()` rebound its exposed authority fields. A custom
Candidate runner could report an output root different from the leaf claimed
by the executor. Finally, the internal output claim and same-name directories
could satisfy file-shaped scientific guarantees, while some public Desktop and
Memory inventories still exposed the claim marker.

## Round 8 findings remediation

Independent temporary-directory probes reproduced all three cases plus an
escaping artifact symlink. The schema now reserves the internal claim filename;
legacy/injected execution contracts require a real file and cannot count the
claim. Candidate plans reject the reserved handoff path, require the runner's
reported root to equal the claimed leaf, and propagate only files resolving
inside that leaf. A directory, external symlink, or alternate result root fails
closed. `RegistrySnapshot.__post_init__` now proves every exposed field remains
bound to the exact published state. Desktop recursive file listing and Memory
artifact capture use the same internal-marker filter as runner/README output.

The RED set reproduced eight bypass/presentation failures before remediation.
The affected nine-file cross-layer selection then passed 284 tests with one
skip. Relevant Ruff checks pass; the pre-existing broad lint debt in
`skill/orchestration.py` remains outside this change and is not represented as
green. Diff checks pass.

## Round 9 — NO SHIP

- Model: `gpt-5.6-sol`, reasoning effort `xhigh`, read-only sandbox.
- Session: `019f6cca-d53c-7f92-a5fe-51da92cad6db`.
- Verdict: `NO SHIP` with 1 High and 2 Medium findings.

The reviewer proved that a pipeline could select the automatically created
`.omicsclaw-run-claim.json` as its chain baton. It also found that the new
snapshot guard compared `loaded_dir` by equality rather than exact identity,
and that output-file reserved-name checks did not normalize backslashes. The
reviewer confirmed the Candidate alternate-root/containment changes, public
marker filtering, immutable nested publication, review-aware DAG caching, and
revision/audit fencing. Its sandbox had no writable temporary directory, so it
could not rerun pytest; the verdict rests on source tracing and read-only model
probes rather than a false green test claim.

## Round 9 remediation

The shared marker predicate now normalizes both path separators. Manifest file
inventories, Semantic artifacts, Candidate handoffs, and pipeline baton names
therefore share one reserved-name fact. Pipeline config rejects the marker for
YAML, normal programmatic construction, and tampered config revalidation; the
runtime baton gate independently rejects it. `RegistrySnapshot` now requires
the exact `loaded_dir` object published by its state rather than an equal Path.

Each finding was reproduced by a failing regression before the fix. The
affected ten-file cross-layer selection passes 337 tests with one skip; 95/95
manifests, full Skill lint, catalog 95, DAG 95/74, relevant Ruff, and diff checks
pass.

## Round 10 — NO SHIP

- Model: `gpt-5.6-sol`, reasoning effort `xhigh`, read-only sandbox.
- Session: `019f6cdb-3a5a-77a1-a01f-c13d5c89f11e`.
- Verdict: `NO SHIP` with 0 Blocker, 0 High, 1 Medium, and 0 Low findings.

The three direct Round 9 remediations were verified. The reviewer nevertheless
proved that a Candidate handoff could name an ordinary in-leaf symlink whose
resolved target was `.omicsclaw-run-claim.json`. The pre-resolution reserved
name check and post-resolution containment check both passed, so downstream
propagation could consume the claim JSON as scientific output. Existing tests
covered the direct marker and an escaping symlink but not this contained alias.

## Round 10 remediation and adjacent evidence-authority audit

Runtime output acceptance now shares two facts in
`omicsclaw.common.output_claim`: claim identity includes lexical name, resolved
name, and inode aliases; a scientific output must additionally be a contained
regular file with link count one. Candidate handoffs, the execution contract,
runner/pipeline inventories, Memory/Desktop/report/notebook projections,
AutoAgent evaluation and trace recovery, acquisition evidence, manifest and
completion evidence, and consensus evidence reuse that predicate. Consumers
that require a canonical evidence path additionally reject a direct symlink.

The follow-up diagnosis also closed adjacent false-authority paths rather than
limiting the change to the reported line:

- manifest and completion metadata reject alias destinations, write through an
  owned atomic replacement helper, and cannot record `complete` when required
  evidence is missing or errors are present;
- acquisition is bounded to the caller's output root and requires owned modern
  completion/manifest/producer evidence; legacy notebook import is explicit
  opt-in and always quarantined; committed scaffold lineage is rewritten from
  ephemeral staging paths to the final normal/quarantine destination under a
  cooperative publication lock;
- `saves_h5ad` requires one owned primary AnnData container readable by the
  actual Skill Python runtime; `python -P` avoids current-directory module
  shadowing while preserving that runtime's `PYTHONPATH` and site packages,
  and missing validator support stays a typed framework failure rather than a
  scientific defect;
- AutoAgent shape gates distinguish unknown dimensions from known zero-size
  output, derive the primary AnnData path from Registry metadata, and recover
  only the exact relative sandbox output;
- consensus integration rejects globally mismatched member lengths before an
  optional metric backend can emit a misleading finite score; persisted
  consensus plans use the actual output directory identity, bounded typed
  fields, and encoded single-segment Memory namespaces; and
- Desktop Run browsing/freshness and governed acquisition ignore claim,
  hard-link, escaping, and unowned evidence aliases while retaining ordinary
  trusted-input behavior; remote artifact and narrative-consensus readers now
  enforce the same boundary; and
- runner-owned result/status, README, notebook, manifest, completion, pipeline
  guide, autonomous result/summary, and replay-script writes use owned atomic
  replacement and reject symlinked parents. Autonomous `result.json` is a
  required artifact published before complete acquisition evidence, so a
  failed final marker cannot leave a promotable `complete` workspace.

These changes do not turn the cooperative claim into a same-user tamper seal:
the marker can still be replaced by a non-cooperative writer with filesystem
permission, and validation plus read is not one filesystem transaction. ADR
0070 records that boundary explicitly.

## Round 11 — NO SHIP

- Model: `gpt-5.6-sol`, reasoning effort `xhigh`, read-only sandbox.
- Session: `019f6d63-bfb0-7b53-979c-e4091a06873e`.
- Verdict: `NO SHIP` with 0 Blocker, 0 High, 6 Medium, and 0 Low findings.

The reviewer verified the Round 10 claim-alias fix, then found six adjacent
authority gaps: the owned writer accepted an output root below a symlink
ancestor; `pipeline_summary.json` bypassed that writer; a trailing empty
`PYTHONPATH` element defeated the intended `python -P` verifier isolation;
AutoAgent assumed `processed.h5ad`; Desktop Run discovery consumed symlinked
evidence and project/Run aliases; and remote artifact lookup erased the lexical
`jobs` alias before validating it.

## Round 11 remediation

The owned writer now rejects symlink ancestors, pipeline summary publication is
atomic and fail-closed, and the AnnData verifier receives the selected Skill
runtime environment without the runner-owned Backend import prefix or empty
path elements. AutoAgent derives its primary AnnData artifact from the frozen
Registry contract. Desktop, Run lookup, and remote artifacts retain lexical
paths long enough to reject aliased roots and evidence. These changes were
covered by child-planted symlink/hardlink, real subprocess import, legacy alias,
Desktop HTTP, and remote HTTP regressions.

## Round 12 — NO SHIP

- Model: `gpt-5.6-sol`, reasoning effort `xhigh`, read-only sandbox.
- Session: `019f6d84-e3cf-7140-811e-b400a458d548`.
- Verdict: `NO SHIP` with 0 Blocker, 0 High, 4 Medium, and 2 Low findings.

The reviewer proved that `symlink/..` could lose alias evidence before the
owned writer's `abspath` check; AutoAgent could inject a second Backend root
that survived verifier sanitization; an unknown, ambiguous, or failing Registry
lookup still fell back to undeclared `processed.h5ad`; and Desktop session
sidecars remained alias-following read/write evidence. The two Low findings
were symlinked Project discovery and contained directory aliases advertised by
generated output guides.

## Round 12 remediation and current verification

Raw path components are now inspected before normalization, so both tainted
output roots and destination parents fail before any temporary file or victim
write. Verifier sanitization removes every resolved Backend-root-equivalent and
empty `PYTHONPATH` entry while retaining unrelated runtime paths and variables;
the AutoAgent launcher also avoids creating duplicates. Unknown, ambiguous, or
exceptional Registry lookups return no AnnData evidence, so Evaluator and trace
recovery use only `result.json`; canonical and legacy aliases still resolve the
declared primary path.

Desktop sidecars use the owned atomic writer and accept only contained,
single-link regular-file evidence. Project enumeration and both single-Skill
and pipeline output guides reject directory symlinks. Remote artifact GETs now
construct their lexical read path without a `mkdir` side effect, so rejecting a
`.omicsclaw/remote` alias cannot materialize an outside `jobs` tree. The same
diagnosis restored message-derived error classification at `_err()` and updated
the obsolete mutable-alias Registry test to assert the stronger deeply
immutable publication contract.

The merged focused selection passes 671 tests with one skip. The expanded
representation/acquisition/retrieval/execution/evolution selection passes 2016
tests with three skips. Relevant Ruff, `py_compile`, and diff checks pass. All
95 manifests validate; catalog remains 95 Skills; the compatibility graph
remains 95 nodes/74 edges; generated SKILL.md/parameters/version/routing/index
surfaces are current; requires has 0 missing dependencies and 7 known extras;
Skill lint passes; routing budget remains within every ceiling; and the
29-case, eight-domain oracle remains 1.000 on all positive metrics with zero
hallucinated aliases.

## Round 13 — NO SHIP

- Model: `gpt-5.6-sol`, reasoning effort `xhigh`, read-only sandbox.
- Session: `019f6db9-8376-79f1-ab7b-55291db5a237`.
- Verdict: `NO SHIP` with 0 Blocker, 0 High, 2 Medium, and 1 Low findings.

The reviewer confirmed the Round 12 alias remediations, then found that an
AutoAgent child trial could construct Registry authority independently of the
parent's frozen snapshot and that `project_meta.json` accepted filesystem
aliases and non-mapping JSON. The Low finding was a cyclic/symlinked
`PYTHONPATH` traversal that lacked a deterministic fail-closed result.

## Round 13 remediation

Parent and child trials now share one frozen Registry publication and exact
Skill revision. Project metadata is read only from a plain, single-link regular
file whose JSON root is a mapping; Project and Run path discovery retain
lexical alias evidence. Runtime path sanitization has a bounded, visited
resolved-path walk so an alias loop cannot reintroduce Backend authority.

## Round 14 implementation closure and current verification

The adjacent AutoAgent admission audit now runs the same mandatory hard gates
for baseline and candidate parameter trials. Evidence reconstruction or durable
trace publication failure becomes a failed admission verdict instead of an
exception or a kept candidate. Trial records and traces carry the gate verdict
plus a receipt binding canonical Skill/version, manifest/source hashes,
non-unknown environment identity, runtime source, and claim/result byte
digests. The default edit surface is target-local: narrative `SKILL.md` content
and the primary regular single-link Python entry only; generated frontmatter
and Inputs/Outputs sections remain guarded.

Accepted code is not made authoritative from an in-memory member. Patch and
manifest artifacts are published before a compare-and-swap `git update-ref`,
and source promotion requires the exact durable `AcceptedPatchRecord` at the
accepted branch head. Promotion rechecks stable inode/content snapshots,
installs through no-clobber links, rolls back a multi-file partial application,
and records a schema-versioned interruption journal. This is a bounded
HarnessWorkspace mechanism, not the SkillEvolutionGovernance manifest
transaction and not a power-loss-atomic filesystem transaction.

The shared runner now derives environment evidence from the actual selected
producer executable under the exact child environment and working directory.
Python evidence includes executable content/selection identity, bounded
interpreter/host/prefix evidence, and actual versions for declared
dependencies. Probe or audit-binding failure is `contract_validator_failed`
and no Skill process is spawned. The existing exclusive claim receives a
second atomic audit binding before spawn. Filesystem policy now treats POSIX
symlinks and Windows reparse/name-surrogate entries (including junctions) as
aliases; contained aliases cannot satisfy result, artifact, AnnData, or
promotion evidence. Backend-owned text publication synchronizes the file and,
on POSIX, the containing directory.

A fresh combined selection covering runner/execution contract, ownership,
Run paths, pipelines, AutoAgent admission/promotion, schema/DAG, evolution,
Registry authority, and output UX passed 699 tests with one platform skip before
the Round 14 review. A second durability/execution-focused selection passed 172
tests with one platform skip.
All 95 manifests validate; catalog is 95 Skills; the compatibility graph is
95 nodes/74 edges; generated Skill/docs/index surfaces, requires, lint, routing
budget, and the 29-case eight-domain oracle pass. The repository-wide test run
is not green and is not presented as evidence for this closure: its cache
mixed stale node ids with optional-dependency/scientific-domain failures and
pre-existing contract drift.

## Round 14 attempt A — no verdict

- Model: `gpt-5.6-sol`, reasoning effort `high`, read-only sandbox.
- Session: `019f6f0d-80ba-7c01-a245-2da08122a703`.
- Result: the service-side safety filter interrupted the run before a report was
  produced. This attempt is neither a pass nor a failure verdict.

## Round 14 — NO SHIP

- Model: `gpt-5.6-sol`, reasoning effort `medium`, read-only sandbox.
- Session: `019f6f25-3e76-7dd3-89e3-8d98c072010b`.
- Verdict: `NO SHIP` with 0 Blocker, 0 High, 1 Medium, and 0 Low findings.

The reviewer independently passed all three Round 13 remediations and five of
the six Round 14 invariants. The remaining Medium finding showed that source
promotion checked only whether the supplied commit was the accepted branch
head. It did not read and authenticate the persisted accepted manifest or patch
artifact, and the manual API reconstructed authority from mutable result fields,
including a separately trusted `accepted_files` list.

## Round 14 remediation

Promotion now authenticates the complete linear accepted commit chain before
creating a source journal or reading source bytes. For each commit it derives the
canonical iteration from the canonical commit subject, reads the exact canonical
manifest and patch through bounded no-follow, regular, single-link, non-alias
readers, validates the canonical `AcceptedPatchRecord`, checks the full commit
message's evidence trailer, and compares the patch artifact with the commit's
generated patch. Missing, changed, linked, misnamed, oversized, non-canonical,
or chain-inconsistent evidence fails closed.

The final supplied record must exactly equal the authenticated durable record.
The promotable file set is derived from the baseline-to-accepted-head Git state;
only bounded regular modifications are supported. The manual promotion endpoint
now opens the expected HarnessWorkspace, derives the durable accepted-head record
itself, and does not use session-result `accepted_files` or `accepted_patches` as
promotion authority.
The existing stable source snapshots, no-clobber install, rollback, journal, and
recovery rules remain in force. New regressions cover missing/modified/aliased or
hard-linked artifacts, a forged in-memory record, a missing earlier-chain
manifest, Git-derived file authority, and forged API result fields.

Immediately after this Round 14 remediation, the then-current combined milestone
selection passed 743 tests with one platform skip. That number is retained as a
historical checkpoint; the larger pre-CAS/recovery selection below is the current
verification evidence.

## Post-Round-14 local adversarial hardening before Round 15

This section records local TDD and adversarial review performed after the external
Round 14 finding. It is not presented as an Ask Codex Round 14 finding.

Accepted commits now have a strict pre-CAS authority closure. `PatchPlan`
`target_files` must be an exact, canonical, unique set match for the `diffs` file
set; duplicate or aliased `FileDiff` entries fail before any write. The candidate
worktree must contain exactly the declared regular modifications with unchanged
file modes, and deterministic hunk replay must produce the exact candidate blobs.
The harness builds the tree and commit directly, binds the complete non-circular
accepted record plus plan in an evidence trailer, publishes canonical artifacts,
authenticates the complete candidate chain, and only then advances the accepted
ref with compare-and-swap. A standard Git `accepted.lock` covers reachability proof
and unreachable-evidence cleanup; post-CAS authentication failure attempts an
inverse CAS. Iterations are strictly monotonic, Git text handling is explicit
UTF-8, and sandbox `.git`/common-dir/linked-worktree aliases or hard links fail
closed.

Promotion recovery now durably binds each stage inode, expected mode, installed
inode, and the complete source-root-to-immediate-parent directory identity chain.
A visible stage without a durable identity has no cleanup authority. The
link-to-unlink interruption window accepts only the journal-bound inode with the
exact two-link shape; same-digest replacement inodes, third links, and mode drift
fail closed. Applied cleanup revalidates the installed target before deleting a
backup and again before reporting success. Stage, install, rollback, cleanup, and
recovery recheck the parent chain immediately before each path mutation; a parent
symlink or plain-directory replacement preserves external paths and records
`recovery_required`.

A subsequent fresh internal read-only adversarial review returned `NO SHIP` with
2 Medium and 1 Low findings. It showed that whitespace-normalized hunk fallback
silently chose the first of multiple equivalent blocks, source promotion did not
bind executable-mode class to the authenticated Git baseline, and an applied
journal did not require `stage_identity == installed_identity` after the stage name
was removed. The remediations use one exact-first shared hunk matcher that rejects
ambiguous exact or normalized occurrences; strictly parse the single regular
`100644`/`100755` entry from both baseline and accepted trees; compare Git's boolean
executable class while preserving full source POSIX modes such as `0600`/`0700`;
and require stage/installed identity equality in applied and interrupted recovery.
A fresh re-review returned `SHIP` with 0 Blocker/High/Medium/Low. This internal
review is regression evidence, not the external Round 15 verdict.

The fresh combined focused selection now passes 920 tests with one platform skip.
Relevant Ruff, `py_compile`, and `git diff --check` pass. All 13 deterministic
representation/routing gates pass: 95/95 manifests, catalog 95, DAG 95 nodes/74
edges, generated parameters/SKILL.md/versions/routing table/eight indexes,
description drift, requires (0 missing and 7 known extras), Skill lint, routing
budget, and the 29-case eight-domain oracle with every positive metric at 1.000
and no hallucinated alias. These scoped results do not turn the currently
non-green repository-wide suite into a green claim.

This remains cooperative, journal-led recovery. It is not one OS/power-loss-atomic
transaction, a same-UID tamper seal, or an OS sandbox. In particular, the final
parent-chain identity check and pathname mutation are not a `dirfd`/`openat` /
`linkat` / `renameat` transaction and retain an OS-level TOCTOU window.

## Round 15 — NO SHIP

- Model: `gpt-5.6-sol`, reasoning effort `medium`, read-only sandbox.
- Session: `019f6fd1-9eec-7670-8bfb-5e8015f84529`.
- Verdict: `NO SHIP` with 0 Blocker, 0 High, 1 Medium, and 0 Low findings.

The reviewer passed the Round 14 durable-chain/API remediation, deterministic
PatchPlan/commit evidence, ref CAS, and promotion-recovery invariants. The one
remaining Medium reproduced an ignored `runtime.cache` in the registered trial
worktree: `git status --untracked-files=all` omitted it, the accepted ref
advanced, and the sidecar remained present during evaluation while absent from
the accepted tree. This violated the claimed exact evaluated-state boundary.

## Round 15 remediation and pre-Round-16 adversarial closure

The baseline now runs only in a detached `iter_0000` worktree derived from the
baseline commit. Source-snapshot ignored bytes are not available to baseline
execution, and baseline admission additionally requires the complete persistent
worktree state to remain unchanged. Child execution pins `PYTHONPATH` to the
candidate root and forces `PYTHONDONTWRITEBYTECODE=1`.

Candidate admission now uses a two-phase raw witness. Immediately after the
trusted patch application and before candidate execution, the harness binds the
exact PatchPlan digest and modified-file set, then freezes every persistent raw
worktree path: root/directories, regular files, symbolic links, inode identity,
full mode, link count, size, timestamps, and content or link-target digest. The
post-execution/pre-CAS state must equal that witness exactly. Deterministic
universal-newline replay also requires each raw target to equal PatchPlan output,
and the staged commit must contain those same bytes; Git clean/smudge or EOL
normalisation cannot substitute different accepted code. Git status still checks
ignored and untracked entries, while `ls-files -v/-f` rejects assume-unchanged,
skip-worktree, and fsmonitor-valid index state.

Before any Git command after candidate execution, the harness authenticates the
complete common Git control tree, linked-worktree marker, and a bounded persisted
Git-config witness. Drift writes a durable workspace-global compromise latch;
that latch prevents subsequent Git, rehydration, accepted-state reads, and
promotion until `create()` rebuilds the sandbox. Worktree cleanup is a mandatory
authority transition: a non-zero removal, premature disappearance, or a nominal
success that leaves either the path or its canonical Git registration latches
the workspace and raises. Promotion is evaluated only after the trial context has
completed that cleanup.

Finally, `AcceptedPatchRecord`, its strict manifest, the full evidence digest,
and commit trailer now bind the canonical source-project root and its directory
`(st_dev, st_ino)`. A rehydrated manual promotion using a foreign copy, a moved
root, or a replacement inode fails before source mutation. Normal rehydration
loads the bounded persisted Git-config witness and continues to authenticate the
complete accepted chain rather than session-result fields.

Local TDD and read-only review exposed the adjacent fsmonitor/config execution,
raw CRLF/clean-filter mismatch, missing-root cleanup, promotion-before-cleanup,
cleanup-failure, and foreign-root paths described above. After remediation, a
fresh internal read-only review reports 0 Blocker, 0 High, and 0 Medium findings.
The corrected 24-file focused selection collected 952 tests: 951 passed, one
platform skip, and two existing third-party deprecation warnings. Documentation
contracts pass 11 tests. Relevant Ruff, `py_compile`, and repository-wide
`git diff --check` pass. All 13 deterministic representation/routing gates pass:
95/95 manifests, catalog 95, DAG 95 nodes/74 edges, generated parameters,
SKILL.md, versions, routing table and eight indexes, description drift, requires
(0 missing and 7 known extras), Skill lint, routing budget, and the 29-case
eight-domain oracle with all positive metrics 1.000 and alias hallucination 0.

These witnesses close persistent endpoint state under the documented cooperative
model. They do not observe a same-UID process that creates, consumes, and deletes
transient state between witnesses, and they are not a read-only mount, OS sandbox,
same-UID tamper seal, or `dirfd`/`openat` transaction.

## Round 16 attempt A — no verdict

- Model: `gpt-5.6-sol`, reasoning effort `medium`, read-only review.
- Session: `019f7053-1f81-7080-b414-2930563d9773`.
- Result: interrupted by the review service's safety filter after extended source
  inspection. No verdict or finding set was produced, so this attempt is not
  release evidence.

## Round 16b — NO SHIP

- Model: `gpt-5.6-sol`, reasoning effort `medium`, read-only review.
- Session: `019f7057-807a-7602-85da-828c74c3a08b`.
- Verdict: `NO SHIP` with 0 Blocker, 0 High, 2 Medium, and 0 Low findings.

The first Medium showed that synthetic baseline construction copied the source
filesystem before creating a new repository. Ordinary untracked files and files
excluded only by source `.git/info/exclude` or global excludes could therefore
be committed; ignored files could at least remain available in `sandbox_repo`.
The detached `iter_0000` witness proved equality only to that contaminated
baseline, and unchanged helpers were not part of the source-promotion target CAS.

The second Medium showed that compromise-marker persistence was not sufficient
durable authority. If marker writing failed, a restart saw neither the in-memory
latch nor a complete persisted common-Git checkpoint and could authenticate the
mutated repository from the old config-only witness.

Round 14's durable-chain/API finding and Round 15's runtime-created ignored
sidecar finding were closed. Round 16b expanded the remaining boundary to source
baseline ingress and restart authority; EVO-G2 therefore remained open.

## Round 16b remediation and pre-Round-17 closure

Baseline construction now parses the source Git index's unique stage-zero
inventory and materializes only current stable bytes for tracked regular files.
It preserves deliberately dirty tracked bytes while rejecting non-Git roots,
unmerged stages, symlinks, gitlinks, missing files/objects, non-blob index
objects, aliases, unsupported modes, and source drift. Ordinary, info-excluded,
globally excluded, and `.gitignore`-ignored untracked paths are absent from both
the sandbox repository and its baseline tree. The synthetic tree is assembled
with no-filter object writes rather than `git add`.

Sandbox info attributes unset text/EOL, filter, ident, and
`working-tree-encoding` conversion. Immediately after checkout, raw seed bytes
are hashed in the repository's actual object format and compared to every parent
blob OID before that seed may become candidate authority. An index entry whose
OID is missing or is not a blob fails during source capture.

Git authority now uses a bounded durable `clean`/`trial_open` state machine.
`trial_open` is published before worktree creation and binds a process-only token;
a restarted process cannot adopt it. `clean` binds source/sandbox identities,
accepted commit, persisted config, and the complete common `.git` inventory.
Every Git subprocess requires `create()` or public `open_existing()`; the latter
authenticates rather than reseals. Missing/corrupt/legacy/open state, common-Git
drift, linked-worktree/lock residue, or a compromise marker rejects before Git.
Cleanup verifies two identical quiescent snapshots and the real accepted ref,
then publishes `clean` as its final commit point. Pre-publication verification or
write failure leaves `trial_open` or a durable latch. Round 17 found that an
atomic writer can instead report failure after replacement made exact `clean`
bytes visible; the post-Round-17 remediation below closes that outcome-unknown
case without weakening different/unstable-state rejection. Only a successful
full `create()` rebuild may reset a real failure or latch.

Promotion now rechecks HEAD, the stage-zero path set, and every non-target
tracked byte/Git executable class at initial admission, before the first source
install, after installs, after cleanup, and before interrupted/applied journal
recovery mutates or reports success. Targets are deliberately excluded from the
whole-tree read because their baseline/partial/accepted states have independent
journal and CAS authority. The clean checkpoint and durable accepted-head read
also require the true accepted ref to equal their recorded commit.

Post-review local adversarial work additionally found and closed: a public Git
read before `open_existing()`, checkout-time `working-tree-encoding`, a single
pre-promotion whole-tree observation, interrupted/applied recovery without
non-target revalidation, an accepted-ref/checkpoint mismatch, a missing index
blob, and a clean-before-verification plus marker-failure combination. The core
workspace/API set now passes 185 tests. The exact prior 24-file expanded scope
collected 977 tests: 976 passed and one platform skip, with only two existing
third-party deprecation warnings. Documentation contracts pass 11 tests. All 13
deterministic representation/routing gates pass: 95/95 manifests, catalog 95,
DAG 95 nodes/74 edges, generated parameters/SKILL.md/versions/routing/eight
indexes current, no description drift, requires with 0 missing and 7 known
extras, Skill lint, routing budget, and the 29-case eight-domain oracle with all
positive metrics 1.000 and alias hallucination 0. Scoped Ruff, `py_compile`,
`git diff --check`, and untracked-document whitespace checks pass. Fresh internal
read-only reviews report no remaining Blocker, High, or Medium finding. Routing
capacity remains close to its non-blocking ceiling (49/50 tools and 43,995/45,000
serialized tool characters), so later tool expansion requires budget work.

Compatibility is intentionally stricter. Old output directories without the new
state must be rerun; external Git maintenance invalidates the checkpoint; crashed
trials are not resumed. This proves the tracked evaluated program plus target
CAS, not equality with source untracked/open-world files, same-UID tamper sealing,
or restart/TTL discovery of a manual-promotion HTTP session. The separate
OmicsClaw-App repository was not modified or reviewed.

## Round 17 — NO SHIP

- Model: `gpt-5.6-sol`, reasoning effort `medium`, read-only review.
- Session: `019f70f1-6855-7803-b2ab-992ab05195bf`.
- Verdict: `NO SHIP` with 0 Blocker, 0 High, 1 Medium, and 0 Low findings.

The reviewer confirmed properties 1–3 and 5–8, including tracked-only source
acquisition, filter-independent raw seed authentication, bounded durable
authority, accepted-ref agreement, repeated promotion/recovery source checks,
the existing accepted transaction chain, and HTTP `open_existing` → durable
read → promote ordering. Round 16b's contaminated baseline finding was closed.

The remaining Medium was a post-replacement outcome ambiguity. The shared
atomic writer calls `os.replace()` before directory `fsync`; if replacement made
a fully valid `clean` state visible but that later `fsync` raised, cleanup treated
the checkpoint as failed. If compromise-marker writing also failed before its
replacement, a new process could authenticate the already-visible `clean` state
even though the previous process reported cleanup failure. Existing tests only
failed before clean publication, so they did not cover this reachable ordering.
Property 4 therefore did not hold, the restart-authority Medium from Round 16b
was not fully closed, and the then-current checkpoint wording was overstated.

## Round 17 remediation and pre-Round-18 closure

`_write_git_control_state()` now reconciles an atomic-writer error with a bounded,
no-follow, single-link stable read of the visible state. Only byte-for-byte
equality with the canonical requested payload is classified as successful
publication under the explicitly non-power-loss-atomic model. Missing, aliased,
unstable, or different state rethrows the original writer error, so cleanup still
persists the compromise latch and restart rejects.

Two adversarial regressions inject failure specifically after clean replacement:
one also makes marker writing fail and proves exact visible clean is accepted
without attempting the marker; the other replaces the visible bytes with a
different prior `trial_open` payload and proves cleanup fails, the marker is
persisted, and `open_existing()` rejects. Both tests assert that the directory-
fsync fault was actually reached. The shared atomic writer is unchanged, so no
other Backend-owned output silently downgrades its durability error.

The refreshed five-file core contains 187 tests and all pass, with only the two
existing third-party deprecation warnings. The same 24-file expanded scope
contains 979 tests: 978 pass and one platform test skips. Documentation contracts
pass 11/11. All 13 deterministic representation/routing gates pass with 95 valid
manifests, catalog 95, DAG 95/74, generated artifacts current, 0 missing/7 known
extra requirements, Skill lint clean, the routing budget within every ceiling,
and all positive metrics 1.000 plus alias hallucination 0.000 on the 29-case,
eight-domain oracle. Scoped Ruff, `py_compile`, and `git diff --check` pass.

## Round 18 — SHIP

- Model: `gpt-5.6-sol`, reasoning effort `medium`, read-only review.
- Session: `019f7112-e7dc-7531-96b7-b7d879158e44`.
- Verdict: `SHIP` with 0 Blocker, 0 High, 0 Medium, and 0 Low findings.

The reviewer matched every frozen production, test, and release-truth hash and
found properties 1–8 to hold. It confirmed exact-byte reconciliation is bounded
by the existing stable reader; missing, aliased, hard-linked, unstable, or
different state retains the original failure; the shared atomic writer is
unchanged; and no fallible operation remains inside `_write_git_control_state()`
after exact visible state is accepted. The positive and negative post-replace
regressions exercise both sides of the decision, while prior checkpoint, marker,
restart, tracked-source, promotion/recovery, accepted-chain, and HTTP ordering
guarantees remain intact.

The independent reviewer additionally ran the three explicitly scoped test
files with cache and bytecode writes disabled: 157/157 passed, hashes remained
exact, and scoped `git diff --check` passed. It explicitly concluded that the
Round 17 Medium and both Round 16b Mediums are closed.

The narrow Backend EVO-G2 milestone is therefore closed. This verdict does not
complete the four-stage Skill audit system: M0 remains verified only in its
declared scope, M1/M2/M3 remain partial overall, and all boundaries below remain
open work.

## Explicit follow-up boundaries

- The source revision uses a bounded runtime-asset suffix policy. There is no
  strict manifest `runtime.assets` inventory yet, so an undeclared CSV/TXT or
  binary asset can remain outside the digest.
- Project-wide conservative hashing prevents known dynamic sibling/runtime
  omissions but can stale unrelated plans and Gotcha evidence. Explicit
  target/shared/cross-Skill revision layers remain future work.
- `param_hints` plus static argparse inspection is not a declarative
  profile-to-argv contract. A manifest binding schema is still required.
- Approval fencing is point-in-time. Promotion/demotion durable records do not
  yet persist the fresh validated source/event, and later validation or
  replacement source drift does not automatically create a durable
  `review-required` state.
- A durable approved decision is not retroactively revoked by recovery when
  source changes after approval; reconciliation only converges its journal.
- The output claim proves freshness only among cooperative OmicsClaw processes;
  it is not a persistent Run Assignment, restart protocol, or OS write sandbox.
- Python producer evidence is not a complete environment lockfile. It does not
  inventory every environment variable, transitive/native dependency, driver,
  or runtime asset. Non-Python runtimes currently expose bounded executable and
  host evidence but no dependency-version proof.
- The producer probe, claim binding, actual spawn, and later reads are not one
  OS/filesystem transaction. A non-cooperative same-UID writer can still race
  or replace executable/output state.
- HarnessWorkspace promotion has rollback and an interruption journal, but it
  is not one crash/power-loss-atomic multi-file transaction; an external writer
  can force `recovery_required`.
- `ExperimentLedger` is a process-local-lock JSONL history, not a cross-process
  or tamper-evident hash-chained governance ledger.
- Run index reads do not automatically rebuild on every manifest mtime change,
  and global Run lookup assumes Run IDs are unique across Projects.
- Parameter revision/writeback governance, the separate OmicsClaw-App Gotcha
  materialization UI, OS sandboxing, and full scientific-content validation
  remain outside this milestone.

OmicsClaw is a research and education tool, not a medical device. Engineering
verification does not replace domain-expert review of methods or results.
