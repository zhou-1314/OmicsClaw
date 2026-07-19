# Store accepted inbound attachments as immutable per-Turn records

## Status

Accepted (2026-07-14).

Refines
[ADR 0045](0045-owner-identity-is-not-a-state-partition.md),
[ADR 0046](0046-normalize-all-conversational-ingress-before-dispatch.md),
[ADR 0047](0047-separate-inbound-envelope-from-dispatch-context.md),
[ADR 0049](0049-immutable-conversation-address-ephemeral-response-sink.md),
[ADR 0052](0052-bind-retried-ingress-to-one-turn-and-resume-observation.md),
[ADR 0053](0053-make-control-plane-state-authoritative-for-project-conversation-and-turn.md),
[ADR 0054](0054-persist-authoritative-control-state-in-backend-exclusive-sqlite.md), and
[ADR 0055](0055-model-project-lifecycle-as-reversible-archive-and-restore.md).

**Attachment-ID-minting refinement (2026-07-20):**
[ADR 0073](0073-mint-attachment-id-in-the-attachment-store.md) moves minting of
the opaque Attachment ID from the control plane into the Attachment Store during
staging. That successor decision preserves this ADR's opacity, uniqueness and
non-semantic guarantees; the original wording below is retained as the historical
record.

Every accepted inbound attachment becomes one immutable **Attachment Record**
owned by an **Attachment Store**, associated with exactly one accepted Turn and
backed by an immutable content-addressed **Attachment Blob**. Control Plane
State remains authoritative for whether the owning Turn exists; it does not
store attachment content. Duplicate ingress returns the original records,
while equal bytes in distinct Owner submissions remain distinct attachment
identities.

**Outbound-delivery refinement (2026-07-14):**
[ADR 0060](0060-deliver-terminal-channel-replies-through-a-persistent-outbox.md)
keeps inbound Attachment References distinct from outbound Delivery media or
artifact references. Outbound Delivery resolves durable content by reference
and digest and never reuses `pending_media`, Attachment display paths or
filesystem scans as a delivery plan.

## Implementation

Production vertical slices implemented (2026-07-16 and 2026-07-17).

The Backend now owns a deep Attachment Store Module with its own checksum-pinned
`attachments.db`, lifetime lock, opaque Store identity, owner-private staging
tree and content-addressed Blob tree. It validates JPEG, PNG, GIF and WebP
batches against count, declared/actual size, media type and SHA-256 limits;
publishes all provisional Records before control acceptance; and keeps equal
bytes shared only at the Blob layer while preserving a distinct opaque
Attachment ID for every novel Turn occurrence.

`control.db` remains content-free. It binds exactly one opaque Attachment Store
identity and atomically commits a Turn's batch ID, record count and ordered
Reference-manifest digest with the Turn Receipt and Ingress Idempotency Binding.
Startup verifies that binding, promotes committed provisional batches, rejects
missing or corrupt accepted content as an integrity incident, and abandons only
expired provisional batches with no authoritative Turn commitment. The normal
runtime never reconstructs an accepted Record from a Surface cache or provider
handle.

The production async Normalizer Interface is now
`accept_async(raw_inbound, attachment_source=...)`. Duplicate lookup and
same-address admission guards run before the process-local byte source is
opened; FIFO and Channel Delivery capacity are reserved before publication;
and control acceptance follows durable batch publication. Failure before the
control commit leaves only recoverable provisional state. Failure after the
commit terminalizes the accepted Turn with an explicit attachment-integrity
code and never runs the Agent on incomplete input.

Inbound Envelopes and canonical Transcripts persist exact structured
Attachment References. A Backend-owned content Adapter resolves and rechecks
accepted bytes immediately before every model call, renders only bounded image
data ephemerally, and restores exact marker/image pairs to References before
history persistence or compaction. The complete model request is preflighted
before any Blob read or Base64 encoding (default: eight images and 50 MiB
combined); unmarked data URIs, reserved markers, bare provider media blocks and
malformed References are rejected before the current user row is appended.
Provider file handles, temporary paths and mutable latest-file registries are
not durable contracts.

The first enabled Surface slice is configured-Owner Telegram: one ordinary
photo, optionally with a caption, is described by stable `file_unique_id`,
bounded by the declared provider size before download, and fetched lazily only
after duplicate and admission checks. The second enabled Adapter is Desktop
`POST /v1/turns` multipart image submission. It requires one strict request
document, one exactly matched file part per 32-hex client attachment identity,
and a declared full SHA-256, size and JPEG/PNG/GIF/WebP media type. Novel input
returns `202` after durable acceptance; a matching `Idempotency-Key` returns
`200` and the original Turn without opening the upload source.

Desktop multipart parsing occurs manually after the configured conditional
bearer gate; default loopback single-Owner mode may intentionally have no token.
A counted ASGI stream bounds chunked as well as Content-Length requests; the
aggregate transport budget adds a 64 KiB overhead allowance to the 50 MiB batch
and 2 MiB request-document bounds; it is not a separate framing-byte quota. Two
pessimistic process-local slots and a 60-second
body-read deadline cap concurrent temporary spools. The request document is
strict UTF-8 with a fixed nesting bound. Successful parsing proves the terminal
multipart boundary and exact equality between every parser-created spool and
visible file part; every success, duplicate, conflict, rejection, timeout,
cancellation and malformed-prefix path closes every provisional spool. The
Adapter ignores multipart filename/media headers, never calls `.uploads` or
`received_files`, and never persists a path, Base64 payload or provider media
block. `ControlRuntime.submit()` returns after durable acceptance while the
runtime retains execution; local attachment input remains an explicit
composition choice. Post-control finalization failure, live-port registration
failure, or runner-wake failure commits a canonical failed Transcript reference
with `dispatch_enqueue_failed` and releases the waiting FIFO lease before
returning. OpenAPI declares the required multipart body, Idempotency-Key, and
actual 200/202 plus rejection response statuses.

Duplicate ingress now satisfies its full Decision requirement (2026-07-20).
`TurnAcceptanceResult` carries the Turn's ordered accepted Attachment
References, so the shared Normalizer — not a Surface-local re-query — is the one
authority: a novel acceptance reports the References it just published, and a
matching duplicate reads them back through the Attachment Store without opening
a byte source. `ControlRuntimeResult.attachment_refs` delegates to that same
value, and Desktop `POST /v1/turns` projects it as a bounded, byte-free
`attachments` array on both the 202 and the 200 response. A Store that cannot
produce the References of an existing Turn raises an integrity incident instead
of answering empty.

The legacy Desktop parallel ingress is retired (2026-07-20). The `.uploads`
writer, the `received_files` registration/reset helpers and the
`_attachments` module that implemented them have been deleted, and
`POST /chat/stream` now rejects attachment-shaped input unconditionally rather
than only when an authoritative runtime is bound — so neither a second durable
copy nor a silent file drop is reachable. `received_files` itself survives only
for Channel Adapters that have not been cut over, and is not attachment
authority.

Accepted Blobs also have durable external retention (2026-07-20). Migration 2
adds `attachment_blob_retention_claims` plus a `BEFORE DELETE` trigger, so a
governed holder — a Run input, a Transcript, or another external reference —
claims a Blob inside `attachments.db` before publishing its reference. Claims
are versioned, immutable, idempotent per `(holder_kind, holder_ref, digest)`
and enforced by the database, so the guarantee does not depend on any garbage
collection query remaining correct. This closes the ordering hazard before Run
Manifest attachment inputs or governed purge land, rather than after.

Telegram media groups, documents, audio, video and outbound media remain
fail-closed. CLI attachment input, every File Reference path, Desktop JSON
submission/options/Project commands, OmicsClaw-App adoption, Textual TUI, every
non-Telegram Channel Adapter, tool consumption, Run Manifest integration,
legacy attachment migration and governed purge are separate future slices.

## Context

ADR 0046 requires every conversational Surface to normalize and durably stage
accepted attachments. ADR 0052 then requires duplicate ingress lookup before
attachment staging so Channel redelivery or Desktop retry cannot download,
write or register the same logical attachment twice. The existing Session and
path-based side channels cannot satisfy either rule across Surface differences
or Backend restart.

Attachment identity and file content are also different concepts. The Owner
may intentionally attach the same file twice in separate Turns; those are two
provenance events even when their bytes are equal. Conversely, storing a full
copy for every occurrence wastes local disk and makes deletion inconsistent.
The architecture needs occurrence identity, immutable byte identity and
accepted Turn identity without collapsing them into one key.

`control.db` must remain a narrow identity/lifecycle store. Attachment names,
media metadata and bytes are content-store concerns, but an Attachment Store
must never create a Turn implicitly. Cross-store acceptance therefore needs a
publish-before-control protocol with conservative reconciliation rather than a
distributed transaction or a runtime path fallback.

## Decision

### Attachment Store owns records and immutable blobs

One Backend-owned **Attachment Store** is authoritative for:

- immutable Attachment Records;
- immutable Attachment Blobs;
- the relation from a Record to its Blob;
- provisional staging, integrity verification and orphan cleanup;
- reference-aware Blob retention.

Control Plane State remains authoritative for Conversation and Turn identity.
An Attachment Record carrying a Turn ID cannot establish that the Turn exists;
it becomes an accepted attachment only when the corresponding Turn Receipt is
authoritatively committed. Transcript, Memory, Workspace and Surface caches are
consumers or projections and cannot register attachments independently.

`control.db` stores neither Attachment Records nor Blobs. The Attachment Store
uses typed control-plane Interfaces for acceptance reconciliation and never
opens or treats `control.db` as its own content database.

### One accepted occurrence has one opaque Attachment ID

An **Attachment ID** is a globally unique opaque identity generated by the
control plane for one attachment occurrence proposed for one Turn. It contains
no Conversation, Turn, Project, Surface, filename, path, media type, content
digest, provider resource key or timestamp semantics.

Each accepted Attachment Record belongs to exactly one Turn and therefore to
that Turn's immutable Conversation. A Turn has zero or more ordered Attachment
Records. A later Turn or Run may cite an existing Attachment ID without moving,
copying, relabeling or changing its original ownership.

The Record contains the minimum durable content metadata required to identify,
verify and render the occurrence: Attachment ID, owning Conversation and Turn
IDs, ordinal, immutable Blob digest and byte size, safe display filename,
declared and/or detected media type, non-secret source descriptor, and creation
evidence. Exact columns and indexes belong in the Phase 2 control-plane
implementation design.

Attachment ID is not a Blob digest. Two distinct Owner submissions of equal
bytes create two Records and may share one Blob.

### Source descriptors identify retry input before staging

A **Source Attachment Descriptor** is serializable pre-acceptance data supplied
by a Surface for one declared attachment. It includes a Surface/source-scoped
stable attachment handle or client-generated upload identity, ordinal and
bounded declared metadata; a full declared digest is included when available.
It contains no provider credential, signed download URL, SDK object, local
temporary path or payload bytes.

The ordered Source Attachment Descriptors participate in ADR 0052's versioned
request fingerprint. They let ingress recognize a duplicate before provider
download or durable staging:

- Channel Adapters use the immutable provider message/resource identity in its
  source namespace;
- Desktop generates one opaque client attachment identity per user action and
  should provide a full-content digest for server verification;
- local CLI input explicitly uploaded into chat receives the same descriptor
  treatment rather than using the path as identity.

A declared digest is untrusted until staging verifies it. Reuse of one ingress
key with different descriptors remains `ingress_idempotency_conflict`.

### Novel attachment acceptance uses publish-before-control

After Owner admission and duplicate lookup, a novel request passes its current
Project/Conversation lifecycle gate and reserves Turn Sequencer capacity before
expensive attachment work. The control plane may generate proposed opaque
Conversation, Turn and Attachment IDs at this point, but none exists
authoritatively until the control transaction commits.

For the complete declared attachment batch, the Ingress Normalizer then:

1. streams or copies every attachment into owner-private provisional storage;
2. enforces count, byte-size and accepted-input constraints;
3. computes the full byte length and SHA-256 digest;
4. verifies any declared digest and rejects unsafe or incomplete input;
5. atomically publishes immutable content-addressed Blobs and provisional
   Attachment Records in the Attachment Store;
6. verifies every published Record and Blob is durably readable;
7. commits the Conversation/binding changes, queued Turn Receipt and Ingress
   Idempotency Binding in the authoritative control transaction;
8. recognizes the provisional Records as accepted for that committed Turn and
   enqueues Turn Execution.

The whole declared attachment batch is fail-closed. If any attachment cannot be
downloaded, bounded, verified or published, no Turn Receipt or Ingress Binding
is created and no Agent, Transcript, Memory, tool, Run or reply side effect
occurs. A Surface may report a typed ingress rejection but must not silently
drop the failed file and execute the text alone.

Publishing content before the control transaction ensures an accepted Turn
never intentionally points to missing bytes. It also creates recoverable orphan
windows:

- crash or rejection before control commit leaves provisional Records/Blobs;
  after a safety grace period reconciliation removes Records whose Turn does
  not exist and garbage-collects unreferenced Blobs;
- crash after control commit but before Attachment finalization recognizes the
  committed Turn Receipt and promotes the same provisional Records;
- missing, corrupted or digest-conflicting content for an accepted Record is an
  integrity incident, never permission to re-download different bytes or erase
  the Turn.

No filesystem scan or Surface cache may invent an accepted Attachment Record
after cutover.

### Duplicate ingress never stages another occurrence

ADR 0052's duplicate-first rule applies to the complete attachment batch:

- same Ingress Idempotency Key and same fingerprint returns the original Turn
  and its original ordered Attachment Records in every Turn state;
- same key with different Source Attachment Descriptors is an idempotency
  conflict and creates no Record or Blob;
- different ingress keys are distinct Owner intent and create distinct
  Attachment Records even when the full Blob digest is equal;
- redelivery after provisional publication but before Turn acceptance may stage
  again, but content addressing prevents a second physical Blob and orphan
  reconciliation removes the abandoned provisional occurrence.

Content equality is storage deduplication only, never submission identity.

### Envelopes and Transcripts carry references, not paths or bytes

Inbound Envelope and Transcript content represent an accepted attachment with
an **Attachment Reference** containing Attachment ID plus bounded display facts.
They do not persist Base64 payloads, provider download handles, signed URLs,
temporary paths, Workspace `.uploads` paths or process-global registry keys.

Prompt assembly and authorized tools resolve Attachment References through the
Attachment Store Interface. A bounded text preview or image payload rendered
for one provider call is ephemeral derived content; every accepted attachment,
including small text and images, still has its immutable Blob.

Tools receive explicit current or historical Attachment References. There is no
mutable "latest Session file", implicit primary file or global scan fallback.
Multi-file operations must select or accept an explicit ordered set.

### Attachment and File Reference remain distinct

A transport-delivered or explicitly uploaded file is an Attachment and must be
copied into the Attachment Store. A **File Reference** is an authorized
reference to a pre-existing local Workspace file that the Owner deliberately
selected without uploading it. It is not an Attachment ID, does not become
durable merely because a prompt mentions its path, and is validated through the
normal file-access boundary.

When scientific execution consumes either form, the Run Manifest records the
resolved immutable input identity and digest. A mutable local path, provider
resource key or Attachment display name is never sufficient scientific
provenance.

### Accepted attachment history is immutable in v1

Project archive/restore, `/new`, Active Conversation Binding replacement, SSE
disconnect, Turn cancellation and ordinary Transcript compaction do not delete
or rewrite accepted Attachment Records or Blobs.

v1 has no ordinary individual accepted-attachment delete. Accepted Record
retention follows its originating Turn/Conversation, while a Blob remains until
no accepted Attachment Record, Run input or other governed durable reference
requires it. Blob garbage collection is reference-aware and uses a safety grace
period; it never treats path age, current Conversation navigation or LRU cache
eviction as proof of unreachability.

Permanent erasure is part of a future explicit cross-store purge workflow with
dry-run inventory, Owner confirmation, interruption recovery and surviving
audit/tombstone rules. Legacy `/clear` or `/forget` cannot silently become such
a purge. Unaccepted staging artifacts and proven orphan provisional Records are
operational cleanup, not deletion of accepted history.

### Migration preserves only provable attachments

Migration inventories legacy Workspace `.uploads`, process-global
`received_files`, Transcript path annotations and Surface temporary files. It
imports an Attachment Record only when a surviving immutable file can be fully
hashed and associated unambiguously with an imported Conversation and Turn.

Legacy Session/Chat keys, filenames, timestamps, prompt annotations and paths
are retained as migration evidence or display metadata, never canonical
Attachment ID. Missing `/tmp` files, ambiguous "latest file" entries and
unreferenced directory contents are reported, not fabricated or attached to a
guessed Turn. After cutover there is no runtime fallback to legacy registries or
paths.

## Consequences

- All conversational Surfaces must emit Source Attachment Descriptors and use
  the shared Ingress Normalizer rather than downloading/registering files as an
  Adapter side effect.
- A new owner-private Attachment Store, provisional staging area, immutable
  Blob layout, reconciliation process and reference-aware GC are required.
- Attachment identity and Blob deduplication become Surface-independent and
  restart-resilient.
- Transcript/provider rendering and tool inputs must resolve structured
  Attachment References; `received_files`, embedded absolute paths and
  Surface-specific Base64 blocks leave the domain contract.
- Storage may reuse equal bytes without merging distinct Owner intent or
  scientific provenance.
- Accepted attachment storage may grow until explicit retention/purge policy is
  implemented; this is preferred to silent evidence loss.

## Rejected alternatives

- **Store attachment bodies in `control.db`.** Rejected because it turns the
  narrow control authority into a content database and duplicates specialized
  integrity/streaming concerns.
- **Let Transcript own attachments.** Rejected because compaction, rendering or
  Transcript deletion must not silently destroy scientific inputs.
- **Use Conversation, Session, Chat or Turn ID as the attachment identity.**
  Rejected because each Turn may contain several attachments and historical
  occurrences must remain individually addressable.
- **Use SHA-256 as Attachment ID.** Rejected because equal content does not mean
  equal Owner intent; the digest identifies Blob bytes, not occurrence.
- **Keep one file copy per occurrence.** Rejected because immutable equal bytes
  can be safely shared while Records preserve distinct provenance.
- **Persist absolute paths or provider file keys.** Rejected because paths are
  mutable and provider handles are scoped transport locators, not durable
  content identity.
- **Keep only a latest-file pointer.** Rejected because it overwrites Turn
  provenance, makes multi-file selection arbitrary and breaks restart recovery.
- **Silently continue when one attachment fails.** Rejected because scientific
  work could execute on incomplete input without Owner awareness.
- **Delete attachments on `/new`, archive, cancel or LRU eviction.** Rejected
  because navigation, lifecycle and cache policy do not prove that scientific
  or historical references are gone.
- **Attempt a distributed transaction across control and content stores.**
  Rejected because publish-before-control plus conservative reconciliation
  gives the required invariant without coupling SQLite and filesystem commits
  into a false atomicity claim.
