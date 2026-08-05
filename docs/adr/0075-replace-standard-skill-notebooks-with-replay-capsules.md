# Replace standard Skill notebooks with Replay Capsules

## Status

Accepted (2026-08-04).

Implementation: Implemented for shared-runner output generation, canonical
exact-demo replay, CLI/Agent output projection, Desktop path-free Run-ID replay,
and fresh-result verification.
Option-bearing and mapped-input replay retain the existing shared-runner
Adapter until those invocation families migrate to canonical `RunRuntime`.

Refines the shared-runner output contract in
[ADR 0065](0065-verify-skill-output-guarantees-at-the-shared-runner.md) and the
explicit fresh-Run rule in
[ADR 0057](0057-persist-minimal-run-lifecycle-receipts-in-control-plane-state.md).
It does not change the genuine generated-code Replay artifact owned by
[ADR 0032](0032-autonomous-code-mini-agent.md).

## Context

The shared Skill runner generated `reproducibility/analysis_notebook.ipynb`
after every successful standard Skill Run. That notebook was not the executed
program. It dynamically imported a Skill script, copied selected function
source into cells, and embedded the producer's absolute paths and shell
command. Skill source and notebook cells could drift, non-Python runtimes did
not fit the model, and a notebook could imply reproducibility without binding
the exact Skill revision, input content, environment or result evidence.

Standard Skills already have a governed execution Interface: the Registry,
shared runner, canonical Run Runtime and Run Manifest. Copying their source
into a second generated execution surface reduced Depth and made the output
look more reproducible than it was. By contrast, Autonomous Code Mini-Agent
runs genuinely create analysis code; their `analysis.py`, notebook view and
Replay artifact remain appropriate first-class outputs.

## Decision

### One runner-owned Skill Replay Capsule

Every successful standard Skill Run emits:

- `reproducibility/replay.json`, containing the exact Skill/version/manifest/
  source identity, content evidence for file, directory or free-form inputs,
  effective parameters, normalized allowlisted invocation, bounded environment
  identity, a result-semantic digest, and evidence for declared scientific
  artifacts;
- `reproducibility/environment.json`, describing the bounded producer evidence
  and explicitly declaring `reconstruction: evidence-only`; and
- `reproducibility/replay.sh`, a thin launcher that delegates to
  `oc replay <capsule>`.

The Capsule stores no absolute original input path. File and directory inputs
must be mapped in original ordinal order and match their frozen content
evidence. Free-form inputs are also mapped and digest-checked rather than
silently exposed in the Capsule.

The current ADR 0065 environment identity is evidence, not a complete
transitive Python/R/native lockfile. The Capsule must not describe it as a
portable environment reconstruction guarantee.

### Replay creates a fresh Run

`oc replay` is an explicit execution command, not a reader for a static report.
It validates the Capsule and current Skill revision before execution, creates a
fresh output, and compares the fresh Capsule with the original across Skill
revision, input evidence, effective parameters, environment identity, result
semantics and declared scientific artifacts.

The canonical exact-demo Adapter submits through `RunRuntime`, uses explicit
`UnassignedScope`, and therefore creates a fresh Run Submission ID and Run ID
without consuming mutable current-Project navigation. Existing option-bearing
or mapped-input forms use the shared legacy runner until their canonical Run
Adapters exist; their Capsule still produces a fresh output and verification,
but must not claim a canonical Control Run ID.

Replay never overwrites the source output, resumes an interrupted Run, mutates
a terminal Receipt or reconstructs executable work during restart. Automatic
recovery remains prohibited by ADR 0057. The Desktop exact-demo command names
only the opaque source Run ID; the Backend resolves its verified terminal
Capsule, creates the fresh Run through the same `RunRuntime`, and records the
source as `retry_of_run_id`. That relation enters the Run Request Fingerprint,
so an Idempotency-Key reused for another source conflicts instead of silently
replaying it. The wire result exposes only Skill ID, fresh Run ID, stable
verification status/code and mismatch codes—never local output/Capsule paths.
The original Receipt remains evidence, not executable replay authority.

### Keep real notebooks where code is genuinely authored

The standard runner no longer calls the synthesized notebook exporter and no
standard Run projection exposes `notebook_path`; it exposes `replay_path`.
Explicit Desktop Notebook workflows, legacy notebook import/scaffolding, and
the Autonomous Code Mini-Agent retain notebooks because those Interfaces
actually own user- or agent-authored code.

## Consequences

- Standard Skill reproducibility has one deep execution Interface instead of a
  drifting copy of Skill implementation code.
- Capsules are diffable, bounded, content-addressed evidence and work for
  Python, R and CLI-backed Skills.
- Reproduction is honest about environment limits and can report
  `NOT VERIFIED` with stable mismatch codes.
- A user needs OmicsClaw and a matching governed Skill revision to replay; the
  Capsule is not a standalone source distribution.
- Cross-machine bit-for-bit reconstruction remains future work requiring a
  real environment-lock and native-tool/container identity contract.

## Alternatives rejected

### Keep generated notebooks and improve their templates

Rejected because template quality cannot remove the second execution surface
or make copied Skill source authoritative.

### Generate both notebooks and Capsules for every standard Run

Rejected because it preserves the ambiguous artifact and implies two supported
reproduction paths. Genuine code-authoring workflows may still emit notebooks.

### Store only the original CLI command

Rejected because commands contain host paths and do not bind Skill source,
input content, environment, result semantics or artifacts.

### Put complete replay payloads in the Run Receipt

Rejected by ADR 0057. The Receipt remains minimal and non-replayable; scientific
provenance belongs to Run storage and the output Capsule.
