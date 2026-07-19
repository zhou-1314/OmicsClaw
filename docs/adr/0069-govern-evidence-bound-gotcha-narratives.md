# Govern evidence-bound Gotcha narratives in the Backend

## Status

Accepted.

Implementation: Implemented for EVO-G2 (2026-07-16).

Refines
[ADR 0037](0037-unified-declarative-skill-representation.md),
[ADR 0066](0066-govern-earned-skill-validation-promotion-in-the-backend.md),
and [ADR 0067](0067-reconcile-interrupted-skill-evolution-approvals.md).

## Context

The Skill health ledger deliberately does not retain raw stderr, input paths,
user data, or secrets. Its structural evidence is sufficient to identify a
repeated exact-source failure cluster, but it is not sufficient to invent a
scientifically useful natural-language warning. The earlier low-level
`generate_evolution_proposals()` helper nevertheless emitted an approvable
Gotcha-shaped object without source identity, a canonical target hash, a
structured narrative, or the fixed governance transaction. That was a second,
unsafe policy path.

Gotchas are human narrative in canonical `SKILL.md`; they are not machine
contract fields in `skill.yaml`. They must reach retrieval and runtime context
without giving callers an arbitrary Markdown patch or moving governance into
the separate OmicsClaw-App repository.

## Decision

### Detection and wording are two separate states

`SkillEvolutionGovernance.refresh()` may create an immutable,
non-approvable `gotcha_evidence:draft`. A draft requires:

- a currently routable, non-consensus Skill;
- the exact Skill id, version, manifest hash, conservative execution-source
  hash, and environment fingerprint;
- at least three distinct ordinary `script_defect` or `contract_failure`
  executions by authoritative Run ID or privacy-safe execution fingerprint;
- one shared exact runtime-entry `trace:file.py:line` signature;
- at least one ordinary success counterexample for the same exact source and
  environment; and
- disjoint failure and success execution identities.

Generic `result.json` keys are not root-cause signatures. Demo defects continue
to drive demotion, and dependency, resource, timeout, cancellation,
framework-validator, unknown, or consensus evidence cannot create a Gotcha.

An authenticated maintainer materializes wording through
`POST /skill-evolution/proposals/gotcha`. The request contains only canonical
Skill id, proposer, reason, support event ids, and structured
`lead/condition/guidance/anchors`. It cannot contain a target path, file bytes,
Markdown patch, validator, manifest hash, or self-asserted source hash; unknown
request fields fail validation instead of being discarded. The dedicated
Evolution authorization boundary is unavailable until a shared Bearer token is
explicitly configured, even though unrelated local-first remote routes retain
their no-token development mode. ADR 0066 defines the frozen authority and the
dedicated local-App credential handoff; this ADR does not introduce a second
authorization policy.

The evidence-cluster candidate id remains immutable. A narrative proposal id
is a digest of that candidate id and the normalized entry digest. Exact retries
are idempotent; a different narrative cannot replace a pending revision, while
a rejected or rolled-back revision can be corrected under a new proposal id.

### Execution identity is captured before process spawn

The shared runner captures one mutually consistent manifest hash, environment
fingerprint, and conservative execution-source revision before spawning the
Skill process. The resolved `runtime.entry` is always included, regardless of
whether the manifest declares Python, R, or Bash. The runner dispatches those
languages through Python, `Rscript`, or Bash respectively; non-Python entries
do not enter the adaptive Python-environment probe, and a missing interpreter
is reported as `missing_dependency`.

For a formal manifest-backed Skill, the source revision currently covers:

- Python, R, and shell sources in the target Skill and canonical domain or
  subdomain `_lib` trees;
- the target Skill's bounded prompt/config/marker asset suffixes (`json`,
  `yaml`, `yml`, `tsv`, `tmpl`, `jinja`, and `j2`);
- the complete canonical Skills tree's executable sources and exact
  `skill.yaml` basenames, because orchestrator and integration Skills may
  dispatch sibling Skills at runtime;
- the sibling `omicsclaw/` runtime and `scripts/` trees, including the same
  bounded runtime-asset suffixes; and
- the repository-root `omicsclaw.py` entry.

Cache, output, test, and reference directories are excluded. The canonical
Skills root is explicit: `_lib` trees above it cannot enter the revision, and a
nearer subdomain `_lib` cannot shadow the real domain library. Nested runtime
entries remain bound to the Registry-identified Skill directory (or governance
manifest parent), so a decoy inner manifest cannot narrow the closure.

Every source is inventoried with `lstat`, symbolic links are rejected, regular
files are read twice through no-follow descriptors, and the full inventory is
rechecked after all reads. Manifest bytes are captured before the source scan
and checked again afterwards. Registry publication stores the exact manifest
revision parsed during candidate construction; planning or execution refuses
to mix it with later bytes and requires an explicit reload. Completion,
contract failure, and sync/async driver failure reuse the frozen Registry-root
snapshot rather than inventing a later identity. Trace evidence retains only
the canonical runtime-entry basename, never arbitrary filenames from stderr.

This is deliberately a conservative project revision, not a minimal static
import closure. It can make an unrelated sibling source edit stale several
plans or Gotcha evidence buckets. Conversely, assets outside the bounded
suffix set are not proved. A strict manifest-level `runtime.assets` contract
and dependency-specific target/shared/cross-Skill revisions remain follow-up
work; the current implementation must not be described as a complete open-world
asset closure.

The environment fingerprint includes OS/runtime family, adaptive-runtime
provenance, Python implementation/version, and controller-visible installed
revisions (or `missing`/`unknown` sentinels) for the Skill's declared Python
dependencies. A content-addressed adaptive-runtime source separates overlay
families, but the fingerprint does not inspect a resolved overlay lock. It is a
privacy-safe environment-family identity for evidence partitioning, not a
reproducible environment lockfile.

### The only write target is canonical `SKILL.md` Gotchas

Backend renders exactly one single-line bullet in this form:

```text
- **Lead.** Condition Guidance Evidence: `entry.py:line`.
```

The renderer removes the empty placeholder, refuses duplicates, and changes no
section outside `## Gotchas`. Narrative rejects Markdown/HTML control
characters, Unicode control/format/line-separator characters, URLs, absolute
paths across punctuation and UNC/drive forms, and credential-like assignments
through an NFKC-normalized scan-only security view. Credential keys use a
bounded left-hand assignment context whose non-ASCII-alphanumeric punctuation
and spacing are removed for family comparison; fullwidth assignment characters
are normalized without rewriting the accepted narrative. URI detection is
token-independent; Markdown boundary
underscores are rejected while scientific identifiers such as `HLA_DRA`,
`p_value`, and `MS4A1` remain valid. This is a conservative privacy gate; the
ledger still never supplies prose automatically.

Run-event collection never copies `result.json` field names into the ledger.
Trace anchors are retained only after the complete traceback path resolves to
the exact canonical runtime entry; an unrelated file with the same basename
cannot manufacture an anchor. Only the canonical basename and line number are
persisted after that comparison. A pre-registry caller value is never persisted
as `skill_id`; unresolved identifiers use a one-way privacy-safe digest until a
canonical Registry identity exists.

The proposal binds the evidence manifest hash, source hash, target-relative
path hash, current `SKILL.md` content hash, and entry digest. Manifest parsing
and hashing use one captured payload, avoiding a double-read snapshot.

### Approval retains the fixed three-gate transaction

Human approval requires an approver and reason, then runs:

1. **representation** — render staged bytes, prove only the governed Gotchas
   edit, prove generator idempotence, resolve every entry-file line anchor, and
   run full targeted Skill lint against the staged document before live write;
2. **execution** — run a fresh shared-runner demo and recheck the exact
   manifest/source snapshot before and after it; and
3. **retrieval** — atomically reload a fully built registry snapshot, prove the
   exact full Gotcha detail is visible, and prove runtime Skill context consumes
   its lead, condition, guidance, and evidence anchor.

Gotchas do not project into `catalog.json` or `skill_dag.json`; their approval
must not rewrite or roll back those unrelated files. The governed target still
uses guarded compare-and-swap, durable recovery intent, exact-byte rollback,
and explicit reconciliation from ADR 0067. Runtime registry reload rejects a
missing, empty, unresolved, incomplete, or identity-colliding inventory while
the old snapshot remains published, then exposes a valid fresh registry with
one copy-on-write state publication. Concurrent chat or execution therefore
cannot observe the build's empty or half-loaded intermediate state.
Initial `load_all`, root changes, and explicit `reload` all use that same
private candidate builder under one process-local re-entrant publication lock;
domain, subdomain, and Skill discovery is path-sorted before registration.
Concurrent first-use calls therefore publish once, and production refresh
helpers call `reload` instead of clearing live dictionaries. Lightweight-only
discovery is atomically indexed before full load and refuses to mix another
Skills root into an already loaded snapshot. Compatibility-DAG cache creation
uses the same lock and one captured state, so a concurrent reload cannot write
an old graph into the replacement snapshot.

### Provenance drift remains visible

Refresh marks old draft or pending Gotcha candidates stale when their exact
manifest/source/target-path/target-content snapshot is no longer current.
When any approved provenance becomes different or unavailable—including
source, manifest, target, lifecycle, or supported Skill type—refresh creates a
non-approvable `gotcha_review:draft` linked to the approved proposal and both
old and current state. It never silently deletes the prose or treats the old
approval as proof for the new source revision. An approved cluster is not
re-nominated merely because its own governed write changed `SKILL.md`.

Candidate-plan execution uses the same authority rather than trusting a cached
DAG. A schema-v2 plan binds each selected Skill's id/version/manifest/source
revision, the exact review-overlay bytes and selected graph payload, method
bindings, and the full static compute reservation. The executor reconstructs
that authority from one frozen Registry snapshot before allocation and again
around every default-runner step. Invented method profiles, rehashed resource
downgrades, graph drift, and source drift therefore fail closed. Independently,
the shared runner derives method identity only from the allow-listed argv that
will reach the subprocess. An explicit raw `--method` that does not survive that
filter is rejected before output-directory creation or process spawn; repeated
scalar method flags follow argparse's last-value semantics. The shared runner
places its last provenance fence before Project `completed` persistence; that
persistence call is the terminal boundary, so a later edit does not create a
contradictory completed index plus failed ledger.

Promotion, demotion, and deprecation approval also fence the execution source
validated by the fresh demo through the guarded manifest publication and final
durable decision append. Deprecation support and counterexample events are
bound to the current target source, while replacement manifest/source identity
is rechecked around its demo and final commit. A planned post-transition hash
substitutes the governed manifest bytes when comparing after publication, so
the approval's own manifest edit is not mistaken for unrelated source drift.
These are point-in-time approval guarantees. The manifest does not yet persist
a separately comparable `validated_source_hash`; a later ordinary source or
replacement edit does not automatically create a durable `review-required`
validation state. That continuous-validity transition is a separate evolution
milestone.

### Cross-repository ownership remains unchanged

OmicsClaw Backend owns evidence policy, proposal state, authentication,
validation, file writeback, recovery, registry publication, and stable HTTP
contracts. OmicsClaw-App may add a thin proxy, runtime-validated view models,
and forms for materializing drafts, but it must not duplicate eligibility,
rendering, approval, or filesystem logic.

## Consequences

- Gotcha evolution is now a governed vertical slice from pre-spawn evidence to
  canonical representation and runtime consumption.
- Privacy-minimal evidence can nominate a cluster but cannot manufacture human
  scientific guidance.
- Corrected wording has auditable revision identity instead of overwriting a
  rejected decision.
- Approved prose has point-in-time source provenance and an explicit future
  review signal.
- Conservative project hashing may invalidate evidence when an unrelated
  sibling Skill, shared runtime, script, or canonical library changes. That
  false-stale bias is intentional until explicit cross-Skill dependencies and
  target/shared revision layers replace the project-wide fallback; libraries
  above `skills_root` never participate.
- Exact input replay is not retained; fresh demo success proves the Skill's
  governed baseline remains healthy, not that every ordinary failure input was
  reproduced.
- Explicit `runtime.assets`, declarative profile-to-argv binding, persisted
  post-approval validation drift, parameter revision, arbitrary code repair,
  automatic approval, OS sandboxing, full dependency lockfiles, and
  cryptographically verified human identity are outside EVO-G2.

## Alternatives considered

- **Persist raw stderr and let a model draft prose.** Rejected because it breaks
  the existing privacy boundary and still does not establish scientific truth.
- **Use any result-envelope key as a structural anchor.** Rejected because
  common keys such as `success`, `outputs`, and `artifacts` merge unrelated
  failures.
- **Write Gotchas into `skill.yaml`.** Rejected because ADR 0037 keeps machine
  contract and preserved human narrative separate.
- **Regenerate catalog and DAG after narrative edits.** Rejected because those
  projections do not consume Gotchas and would widen rollback hazards.
- **Implement policy in OmicsClaw-App.** Rejected because Backend, CLI, Channel,
  and remote operation would diverge.
