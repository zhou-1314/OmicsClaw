# Require fresh, exclusively claimed Run output directories

## Status

Accepted.

Implementation: Implemented for the shared Skill runner, YAML pipelines, and
candidate-plan executor (2026-07-16). Extended with actual-producer audit
binding and Windows reparse/name-surrogate rejection (2026-07-17).

Refines
[ADR 0065](0065-verify-skill-output-guarantees-at-the-shared-runner.md)
and the bounded-execution priority in
[`skill-lifecycle-redesign.md`](../proposals/skill-lifecycle-redesign.md).

## Context

The shared runner previously treated an explicit, non-empty `--output` as an
overwrite warning rather than an authority conflict. Both subprocess drivers
inspect `result.json` after the child exits because a current Skill may record
an explicit terminal status there. Reusing a directory therefore allowed an
old `status: ok` envelope to override a new child exit code, and old declared
artifacts could satisfy the new execution contract. The same stale-artifact
problem applied to pipeline batons and candidate-plan step directories.

Deleting only `result.json`, comparing modification times, or checking one
known baton would not establish that every artifact belongs to this Run. Two
concurrent callers could also observe the same empty directory before either
started writing.

## Decision

### A Run adopts only an absent or empty output directory

Before subprocess spawn, OmicsClaw creates the requested directory when
needed, rejects any filesystem alias or existing artifact, and atomically
creates a mode-0600 `.omicsclaw-run-claim.json` file with `O_CREAT | O_EXCL`.
A filesystem alias includes a POSIX symbolic link and a Windows
reparse/name-surrogate entry such as a junction. The gate inspects lexical
components before normalization and checks the directory both before and after
claim creation. The file and, on POSIX, its directory entry are synchronized
before the gate returns. A second cooperative
claimant, a prior completed/partial Run, or a crashed Run whose claim remains
therefore fails closed. Existing user files are never deleted or rewritten.

The claim is intentionally durable after completion. It is internal execution
authority, so the manifest schema reserves its filename. Runtime consumers use
one scientific-output predicate that requires a contained regular file with
link count one and rejects the claim by lexical name, resolved name, or inode.
Consequently a direct marker, an in-tree symbolic-link alias, or a hard-link
alias cannot satisfy the execution contract, Candidate handoff, manifest or
completion evidence, acquisition source evidence, or the governed public
inventories wired to that predicate. Result, Memory, Desktop file inventories
and generated README listings hide it.
An already-created empty directory remains accepted for compatibility with the
project Run resolver; a directory containing any file, subdirectory, or earlier
claim does not.

### The claim receives a second actual-producer binding

The first phase reserves the output before adaptive runtime resolution. Once
the shared runner has selected the actual executable, child environment, and
working directory, it computes the Skill audit identity and atomically replaces
the owned claim with a mode-0600 record containing canonical Skill/version,
manifest/source hashes, environment identity, and runtime source. The new file
and, on POSIX, its containing directory are synchronized and reread before
spawn. A changed owner/inode, hard link, POSIX symlink, Windows reparse entry,
probe failure, or persistence failure prevents the Skill process from starting.

### The gate is below every production execution Surface

The shared `_prepare_skill_run` seam claims every leaf Skill output before
sync or async spawn. CLI, agent tools, Desktop Backend, Channel, remote jobs,
pipeline leaves, and production candidate-plan leaves therefore share one
rule.

Composite executors additionally claim their root before starting any step.
Pipelines validate the complete programmatic/YAML config and reject existing
step targets. Their chain-output basename reserves the internal claim filename,
and the runtime baton gate independently rejects it. Candidate plans claim
custom-runner leaves themselves; default leaves use the shared runner. This
prevents one earlier step from planting a future sibling's result or baton
after the composite's initial root check.
After a custom runner returns, its reported output root must still equal the
claimed leaf. Every propagated handoff must be a real file whose resolved path
remains in that leaf and whose lexical path contains no filesystem alias;
directories, internal claims, contained/escaping aliases, and hard links fail
the contract.

### Scope of the ownership claim

The claim establishes freshness and exclusive adoption among cooperative
OmicsClaw processes on a filesystem that implements exclusive file creation.
It is not a durable Run Assignment, restart/resume protocol, transaction over
scientific artifacts, filesystem quota, tamper-evident seal, or OS sandbox. A
non-cooperative process with write permission, including another process under
the same user identity, can unlink/recreate the marker or change output files.
Path validation and later reads are also not one filesystem transaction, so a
same-identity writer can race the check/read interval. Broader Run ownership,
immutable storage, and process isolation remain separate control-plane
concerns.

The actual-producer probe, second-phase binding, spawn, and later reads are
also separate operations. The binding strengthens audit correlation but does
not make the executable, environment, or output immutable. Stable lock files,
branch compare-and-swap, and promotion journals coordinate cooperative Backend
writers only; they do not remove the same-UID boundary.

The current-run `result.json` status contract is unchanged. The decision only
ensures that the driver cannot observe a status or artifact that predated this
execution.

### Cross-repository ownership remains unchanged

OmicsClaw Backend owns output admission, claim persistence, subprocess
execution, and artifact verification. OmicsClaw-App may display the Backend
conflict and let the user choose a new directory, but it must not pre-delete
files, manufacture claims, or implement a second freshness policy.

## Consequences

- Explicit output reuse is now a visible failure instead of an in-place
  overwrite. Callers must choose a fresh directory.
- A stale successful envelope or artifact cannot turn a new failed execution
  into success.
- Cooperative concurrent Runs cannot share one output target.
- Crashed directories remain non-reusable until an operator deliberately
  chooses a different target or performs an out-of-band archival decision;
  automatic cleanup would weaken the evidence boundary.
- Hidden claim files are additional on-disk audit metadata, not user-facing
  scientific artifacts.
- Backend-owned manifest/completion metadata uses temporary-file + file and
  POSIX-directory `fsync` + atomic replacement and refuses pre-existing
  filesystem-alias/hardlink destinations, any aliased ancestor or destination
  parent, and an alias component followed by `..` before lexical normalization
  can erase that evidence. The same writer
  protects runner-owned result, status, README, notebook, pipeline summary and
  guide, Desktop session sidecar, and autonomous result/replay records.
  Read-side Project/Run discovery and generated directory inventories also skip
  aliases; remote artifact GETs build lexical paths without creating storage
  through an alias. This prevents ordinary alias redirection but does not remove
  the same-identity race boundary above.

## Alternatives considered

- **Keep warning and overwrite.** Rejected because old evidence can satisfy a
  new Run and corrupt health/evolution decisions.
- **Delete only known result and baton files.** Rejected because unknown or
  method-scoped artifacts can remain stale.
- **Use timestamps or directory snapshots.** Rejected because clock and scan
  comparisons do not provide an exclusive concurrent owner.
- **Always stage elsewhere and atomically replace the requested directory.**
  Deferred: it adds promotion, cross-filesystem, recovery, and user-file
  preservation semantics that are unnecessary for the current fail-closed
  contract.
