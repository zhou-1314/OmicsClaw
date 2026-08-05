# Separate run-derived Skills with a domain-first Inventory

## Status

Accepted (2026-08-05).

## Context

The first real No-Skill-to-Skill lifecycle produced
`bulkrna-cosinor-rhythm`, but published it beside maintainer-authored Bulk RNA
Skills. Its `skill.yaml.provenance` correctly recorded an Autonomous Run while
the filesystem gave operators, Desktop clients, and maintenance scripts no
stable way to distinguish the publication class. Discovery was also repeated
in Registry, Catalog, governance, validation scripts, and tests, with subtly
different depth and hidden-directory rules.

A top-level `skills/run-derived/<domain>/...` split would make collection the
first path concept and break the existing domain boundary used by `_lib`
source closures, domain documentation, and routing. Collection must remain a
physical/navigation property, not a new trust, lifecycle, or routing authority.

## Decision

### Domain-first layout

The canonical Skills tree admits these physical collections:

- curated: `skills/<domain>/<skill>` and the existing bounded
  `skills/<domain>/<subdomain>/<skill>` form;
- run-derived: `skills/<domain>/run-derived/<skill>`;
- user-installed: `skills/user/<skill>`;
- acquisition quarantine: `skills/.quarantine/<domain>/<skill>`, which is not
  discoverable or routable.

`skill.yaml` remains the machine-contract source of truth for canonical id,
domain, provenance, lifecycle, validation, runtime, and resources. The
collection only describes where a Skill is physically maintained. It cannot
grant validation, approval, routing eligibility, or security properties.

### One filesystem-facing Inventory

`omicsclaw.skill.inventory.SkillInventory` is the sole Module that understands
the layout grammar. It emits deterministic `SkillLocation` records and stops
descending after recognizing a Skill, so a Skill-owned `tests/` or helper
package cannot be misclassified as another Skill. It rejects duplicate
canonical identities across collections and requires a run-derived manifest's
domain to match its domain-first path.

Registry, Catalog, protocol validation, and Skill evolution governance consume
this Inventory. Existing private Registry traversal helpers remain temporary
compatibility code only and are not a discovery authority for new runtime or
projection consumers. Raw lint/migration utilities may still scan manifest
files directly when their purpose is to diagnose malformed or legacy trees.

### Backend-owned placement

The public authoring contract accepts a typed `SkillAuthoringRequest` and, for
run-derived creation, an opaque claimed `run_id`. Only the Backend resolves
that Run to an owned output. A successfully resolved `source.kind=run` is
published under the run-derived collection; intent and corpus scaffolds remain
curated. An Agent cannot supply a workspace path or collection name.

Run-derived demos copied from the claimed Run are repository-relative and
content-bound in the declared Evaluation Protocol. A staging smoke gate is
only admission evidence. The exact published revision still needs real shared
runner evaluation and human governance before it becomes routable, and its
Experience View must report `validation_state=current`.

### Relocation

A formal Skill relocation is an offline maintenance operation: stop execution
surfaces, move the directory, rebuild only the live workspace Manifest and
Completion Report with `rebuild_scaffold_publication_metadata`, refresh all
projections, evaluate the new exact revision, then reopen routing. Historical
`references/source_*` evidence remains unchanged. The move does not create a
new approval decision, but source/protocol drift makes old evaluation evidence
stale until the real Evaluation Protocol runs again.

Catalog and Desktop Skill responses expose `collection` so clients can present
the distinction without inferring provenance from a path.

## Consequences

- Domain ownership, routing names, CLI commands, and shared `_lib` closures stay
  stable while run-derived publications gain an explicit home.
- Every future filesystem consumer must use `SkillInventory`; recursive marker
  scans are no longer an acceptable authority.
- Moving a Skill while execution surfaces are live is unsupported because a
  Registry snapshot could otherwise route the old location.
- Collection and provenance deliberately remain separate: a malformed or
  manually copied directory cannot manufacture earned trust.

## Alternatives rejected

### `skills/run-derived/<domain>/<skill>`

Rejected for the current architecture because it makes collection the primary
boundary and complicates domain `_lib` source closure and documentation. It can
be reconsidered only with an explicit package-boundary migration.

### Encode run-derived state only in `skill.yaml`

Rejected because it preserves the operator/navigation ambiguity and leaves
filesystem scanners unable to apply one closed layout grammar.

### Let the Agent choose a target directory or collection

Rejected because arbitrary paths are not provenance and would let an LLM
bypass Backend ownership and placement policy.
