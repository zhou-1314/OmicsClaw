# Verify Skill output guarantees at the shared runner

## Status

Accepted (2026-07-15).

Refines
[ADR 0037](0037-unified-declarative-skill-representation.md) and
[ADR 0042](0042-governed-candidate-plan-execution-and-skill-evolution.md).

## Implementation

Implemented (2026-07-15) for result envelopes, top-level required result
keys, unconditional Semantic artifacts, and matching Method-scoped file and
artifact guarantees. Extended (2026-07-16) so `saves_h5ad: true` also requires
one unambiguous, owned primary AnnData output that the Skill's actual Python
runtime can open. Extended again (2026-07-17) so the pre-spawn claim is bound
to evidence from the actual selected producer executable, environment, and
working directory, and so execution/AutoAgent receipts bind the claim and
result bytes. Declared AnnData field/value semantics remain follow-up work.

The implementation is the `omicsclaw.skill.execution_contract` Module called
by the shared runner's completion Seam. The registry, generated catalog, and
Desktop Skill responses also expose whether security metadata is unreviewed or
an explicitly reviewed declarative statement.

## Context

`skill.yaml` already represented outputs, but a successful subprocess could be
reported as a successful Skill even when it omitted `result.json`, emitted a
non-standard payload, or failed to produce an artifact promised to a downstream
consumer. Candidate-plan execution had a narrower missing-artifact check, so
ordinary and planned execution did not share one authoritative Interface.

The representation also gave `security` apparently safe defaults. That made an
unreviewed Skill indistinguishable from one whose network, data-egress, and
write behavior had actually been inspected. These fields describe expected
capabilities; they do not prove OS confinement.

## Decision

### The shared runner owns post-run Skill contract verification

A subprocess exit code of zero is necessary but not sufficient for Skill
success. Before writing runner-owned guides, notebooks, completion metadata,
or a success event, the shared runner verifies the declared output guarantees.
All execution Surfaces that use the runner inherit this behavior without
surface-specific adapters.

For a v2 output contract the runtime verifier enforces:

- a declared `result.json` exists, parses, matches the shared result envelope,
  and is not a scaffold result;
- every top-level key named by `result_json.required_keys` exists;
- every unconditional `outputs.artifacts` path exists; and
- files and artifacts in the Method-scoped guarantee matching the method that
  actually ran exist. The result payload's actual method wins over the
  requested method so a recorded fallback is checked honestly; and
- when `outputs.anndata.saves_h5ad` is true, the current primary-output
  convention resolves to exactly one `.h5ad` inventory path, that path is an
  owned output file, and `anndata.read_h5ad(..., backed="r")` succeeds under
  the same Python executable and environment used for the Skill process. The
  probe uses Python safe-path mode (`-P`) and the selected runtime environment.
  Before launch, the runner removes every empty or resolved
  Backend-root-equivalent `PYTHONPATH` entry (including duplicates) while
  retaining unrelated runtime paths, site packages, virtual-environment state,
  and explicit runtime variables. This prevents the runner and AutoAgent from
  reintroducing Backend import authority into the verifier.

`outputs.files` remains an inventory, not an unconditional guarantee. It may
contain optional plots and branch-specific files. Treating the full inventory
as mandatory would reject valid Skills and erase the reason Method-scoped
guarantees exist.

Every enforced file must be a concrete output-relative, single-link file that
resolves under the run directory and is neither the internal Run claim nor an
alias to it. No existing lexical path component may be a POSIX symbolic link or
a Windows reparse/name-surrogate entry such as a junction, even when that alias
would resolve back inside the output tree. The historical documentation token
`output_dir/` is normalized at this compatibility Seam.

### Producer evidence is bound before spawn

The initial output claim is created before adaptive runtime resolution. After
the runner has selected the actual command, child environment, and working
directory, it computes an environment identity and atomically binds that
identity plus canonical Skill/version and manifest/source hashes to the claim.
A Python producer is probed through that selected executable in safe-path mode
with the exact child environment and cwd. The bounded probe records executable
content/selection identity, interpreter and host facts, private prefix-family
identities, and actual versions for dependencies declared by the Skill. Probe
or claim-binding failure is `contract_validator_failed`; the Skill process is
not spawned.

After completion, AutoAgent receipts additionally verify canonical
Skill/version, manifest/source hashes, a non-unknown environment identity,
runtime source, and SHA-256 digests of the persisted claim and result envelope.
The receipt and hard-gate verdict are propagated into TrialRecord, trace, and
experiment history rather than being reconstructed from a score alone.

This evidence is intentionally bounded. It is not a complete environment
lockfile and does not inventory every environment variable, transitive/native
dependency, driver, or undeclared runtime asset. Non-Python runtimes currently
provide limited executable/host evidence without dependency-version proof.
Probe, claim binding, spawn, and later reads are not one OS transaction; a
non-cooperative process with the same filesystem authority can still race the
boundary.

### Contract failure is a typed failed run

When verification fails, the runner returns exit code `1`, `success=false`,
and `error_kind=contract_failure`, and records one privacy-minimal execution
event. It preserves the Skill's raw output directory for diagnosis but does not
write runner-owned success guides or mark the Project run complete. An
unexpected verifier error fails closed as `contract_validator_failed`.

An unavailable AnnData validator in the Skill runtime is a verifier failure,
not proof of an invalid scientific file: it therefore follows the same
`contract_validator_failed` path. A present but unreadable `.h5ad` is a normal
`contract_failure` with `anndata_invalid` evidence.

Legacy registry entries with no output contract remain compatible. This ADR
does not infer guarantees that were never represented.

### Security metadata is reviewed declaration, not enforcement

The `security` block is optional. Absence means `reviewed=false` and
`enforcement=undeclared`; the system must not reconstruct safe defaults. When
present, all three fields are required and consumers report
`reviewed=true`, `enforcement=declarative`, plus the declared data-egress,
network, and write capabilities.

This status is propagated through registry, catalog, and Desktop responses. It
does not admit or deny execution and does not claim seccomp, namespace,
container, firewall, or filesystem confinement. OS-level enforcement remains a
separate acquisition/runtime security boundary.

## Consequences

- The shared runner becomes a deeper Module: one verification Interface gives
  high Leverage across CLI, agent, Channel, Desktop, Remote, and Candidate-plan
  callers while keeping failure semantics local to one completion Seam.
- Semantic handoff claims can no longer succeed solely because the producer
  process returned zero.
- Existing producers that wrote ad-hoc result dictionaries must adopt the
  shared result-envelope helper before they can succeed through the runner.
- Only explicitly audited Skills show reviewed security metadata. Coverage
  begins sparse and must expand through evidence-backed review, not bulk safe
  defaults.
- Primary AnnData existence and container readability are now verified for
  `saves_h5ad`; declared `obs`, `obsm`, `var`, shape/value invariants, and
  content-level scientific validity are not implied by this milestone.
- AutoAgent evaluation and trace recovery use the same Registry-derived primary
  path. Unknown, ambiguous, or failed Registry resolution yields no AnnData
  evidence and falls back to the result envelope; it never guesses
  `processed.h5ad`.
- Successful AutoAgent evidence is tied to the same actual-producer identity
  and output bytes used by the shared runner instead of a controller-only
  environment estimate.

## Alternatives considered

- **Verify only in Candidate-plan execution.** Rejected because direct Skill
  calls could still report false success and would preserve two execution
  contracts.
- **Require every path in `outputs.files`.** Rejected because that field is an
  inventory containing legitimate optional and branch-specific outputs.
- **Mark all existing Skills as `network:none` and `output_dir_only`.** Rejected
  because static defaults are not evidence of reviewed behavior or OS
  enforcement.
- **Delete failed outputs.** Rejected because the preserved directory is needed
  for reproducible diagnosis; only success projections are withheld.
