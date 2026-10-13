# Durable notebook contract

NotebookStore stores notebook cells and execution records in the existing SQLite Database. It is the durable source of truth for recovery, audit, and reconstruction. The in-memory TurnStream and Desktop SSE ring are delivery caches; a reconnect may rebuild their suffix from this store, but a missing frame is never treated as a missing execution.

A cell key is (notebook_id, cell_id, session_epoch). Writes use optimistic compare-and-swap revisions. A stale revision raises RevisionConflict; the write is not merged silently. A new kernel epoch has an isolated cell namespace.

An execution has a stable execution_id and an optional client_command_id. Replaying the exact same record is idempotent. Reusing either identifier with a different notebook, cell, epoch, status, or payload raises ExecutionConflict. A queued execution is completed with update_execution, which increments its revision; calling record_execution again is not a lifecycle update.

output, provenance, and trust_label are stored as data. This layer never evaluates output, turns it into a prompt instruction, or executes a replay. Live-app interactions publish structured state. Subsequent Agent kernel calls still require tool approval and create a new execution. App output may be rendered or attached as an artifact only after its schema and provenance are recorded.

The schema is additive: opening an older database creates the two notebook tables and indexes without changing existing rows. Prior to Stage 6, P0 did not expose .ipynb import/export or live-app RPC. P1 adds an optional durable event log for TurnStream recovery.



## P1 interactive recovery

The optional `DurableTurnEventLog` stores projected wire payloads for each `TurnStream` frame in SQLite, keyed by a collision-free `(session_id, turn_id)` stream key and sequence. The in-memory ring remains a delivery cache. A reconnect first replays durable rows when the ring no longer contains the requested cursor; if durable retention also expired, the client receives an explicit `GAP`. Payloads are untrusted data: replay reconstructs a frame for rendering and never evaluates or sends stored output back to the kernel.

`WorkspaceKernelBinding` can receive a `NotebookStore` and records `queued -> running -> completed/failed/interrupted` for persistent-kernel cells, including provenance and the `kernel_output_data` trust label. Existing callers without a store retain previous behavior.

Deployment can opt the chat registry into the durable sink with `OMICSCLAW_DURABLE_TURN_EVENTS=1`; the default keeps the existing in-memory delivery path until the async writer is explicitly enabled.

The registry keeps one owner for a live stream. The durable append is contiguous per stream; a duplicate sequence is idempotent and a competing or skipped sequence is rejected rather than silently accepted. Durable replay is a wire projection for reconnecting surfaces; same-process observers continue to receive the original typed `TurnEvent` objects from the ring.


Cross-process durable streams use a SQLite owner lease. A live competing owner is rejected, an expired lease can be taken over with a new epoch, stale epoch writes are rejected, and sequence gaps are rejected. `flush_durable()` is awaited before a turn is considered fully settled. It returns false and durable_error records the first sink failure.


## Notebook interchange and live-app refeed

export_ipynb writes code cells, source, execution status, provenance, and inert stream/error outputs in nbformat 4.5. import_ipynb accepts nbformat 4 documents, skips markdown/raw cells, stores code source as a new idle cell, and marks it imported_untrusted. Imported outputs are intentionally discarded: loading a notebook never creates an execution record and never evaluates a stored output.

execute_cell_live is the narrow live-app adapter. It sends a validated cell command to the existing kernel binding, converts callback text into ProgressUpdate data on the existing TurnStream, and emits a cell_completed progress marker only after queued cross-thread callbacks have been delivered. The callback payload is truncated for display and remains data; it is not appended to a prompt or treated as executable code. The adapter does not replace kernel permission checks or NotebookStore lifecycle recording, which remain the source of execution truth.


## Desktop interactive analysis API and UI

The source Desktop App now exposes `/notebooks` through its Notebook navigation item. It supports editable Python cells, explicit Save/Run/Cancel, stdout/stderr and PNG/JPEG figures, reconnect, `.ipynb` import/export, and execution-linked notes or selections. This requires matching backend and App source; an already installed v0.1.8 binary does not gain the screen automatically.

The backend mounts the following authenticated routes. Mutations require JSON and share the Desktop 2 MiB request limit. Sources are limited to 256 KiB and feedback payloads to 16 KiB.

| Method | Path | Contract |
| --- | --- | --- |
| GET | `/notebooks` | List notebook identifiers |
| GET | `/notebooks/{id}` | Cells, execution records, kernel status and feedback |
| PUT | `/notebooks/{id}/cells/{cell}` | `source` and `expected_revision`; stale writes return 409 |
| POST | `/notebooks/{id}/cells/{cell}/execute` | `client_command_id` and `expected_revision`; returns an execution |
| POST | `/notebooks/{id}/executions/{execution}/cancel` | Interrupt and wait for local finalization |
| GET | `/notebooks/{id}/executions/{execution}/events` | Durable SSE replay after `Last-Event-ID` or `after_seq` |
| POST | `/notebooks/{id}/import` | Import `{document: ipynb}` into an empty notebook |
| GET | `/notebooks/{id}/export` | Download current cells as nbformat 4.5 |
| POST | `/notebooks/{id}/feedback` | Store `{execution_id, kind: note or selection, payload}` |

Desktop keeps notebook records and events in the workspace's `.omicsclaw/notebooks.db`. The service uses the existing persistent kernel manager with session `notebook:<id>`, so cells in one notebook share variables. An unbound notebook has a separate namespace; an explicitly bound chat uses the same local Python namespace (see below). Notebook history survives process restarts; Python variables do not. Stored code is never rerun automatically.

An explicit authenticated Run is a user code command. A transaction claims the notebook and records the command before scheduling execution. A repeated command identifier with the same cell and source revision returns the existing execution; changing that identity returns 409. Only one execution may be active per notebook. Editing and starting another execution remain blocked until the terminal result and durable stream flush have both settled.

Notebook ownership uses a 30-second lease renewed every five seconds. After owner loss, a read recovers abandoned queued/running records as interrupted. Pending terminal results retain their output but report a durability error. A stale owner cannot replace recovered state or write output into a cell owned by another execution. Graceful shutdown waits for pending admissions and finalization before closing the database.

Notebook SSE uses the projected TurnEvent schema with monotonic sequence numbers, not the Chat v3 `{type,data}` envelope. The endpoint releases `exchange_end` only after final state and flush status are visible. A failed sink sends an explicit `durability_error`; the App stops reconnecting and refreshes the authoritative snapshot. Reconnect observes stored events and does not execute code. The App binds requests to the runtime target first observed by the page; a switched backend is rejected before forwarding old commands or cursors.

Imports preserve code source only, discard saved outputs, skip markdown/raw cells, and mark source `imported_untrusted`. Invalid imports are atomic and importing never runs cells. Exports contain current stored outputs; editing clears old output. This is a code-cell interchange format, not lossless preservation of arbitrary Jupyter metadata, widgets, markdown, or embedded applications.

Feedback is an immutable execution-linked data record labeled `live_app_untrusted`. The UI refreshes notebook state and displays the feedback count; full records are available through the snapshot API. It does not execute feedback, promote it to system instructions, or automatically submit it to a model. This legacy feedback endpoint remains inert. The separate live-state endpoint below is the explicit model-context publishing path. Text uses escaped rendering; only PNG/JPEG figure paths are served through the existing authenticated file boundary.

The route-baseline test compares declared paths with the actual mounted FastAPI routes. Chat v3 retains its exact two-key SSE envelope for existing clients; `/health` still supplies process identity, and Jobs/Run retain their own epoch handling.


## Shared analysis context and explicit live state

Create a chat and send its first message, then bind a notebook through the UI or authenticated `POST /notebooks/{id}/binding` with `{session_id}`. The service checks the chat exists and is idle. Each chat and notebook can have only one binding; retrying the same pair is idempotent. Rebinding returns 409. An existing local Python chat kernel can be adopted by an unexecuted notebook; if both sides already have state, the service refuses to merge them. Other languages and remote kernels are not adopted.

Approved Agent `python` calls and explicit user Run commands use the same kernel, execution admission transaction, notebook records, ownership lease, and durable event stream. Agent calls still pass through the tool permission gate. Denied calls produce no notebook execution. Cancel during admission, kernel startup, or lock waiting prevents code from being sent; cancel targets the matching execution and local Python kernel. Kernel death does not automatically replay notebook code. Process restart restores records and binding, but cannot restore Python variables.

Before each main-Agent model request, a bound notebook contributes a sent-only `Role.USER` reference block. It contains binding, current kernel status, at most eight recent cell previews, the current live-app metadata, and the latest shared live state. Source previews are limited to 1,024 characters, stdout to 2,048 and errors to 1,024; the entire block is at most 24,000 UTF-8 bytes. Preview truncation is explicit. The block is rebuilt for every request, including after compaction, and is not written into conversation history or long-term memory. Contents are untrusted reference data, with escaped delimiters and a fixed trust notice; they cannot grant tool approval.

`POST /notebooks/{id}/live-state` accepts:

```json
{"execution_id":"...","client_event_id":"...","expected_revision":0,"kind":"view","payload":{"view_id":"scatter","parameters":{"threshold":0.5},"selected_ids":["sample-1"]}}
```

The notebook must be bound and the referenced execution must belong to it and be settled. `kind` is `note` (text), `selection` (selected IDs and optional text), or `view` (view ID, flat JSON scalar parameters and optional selected IDs). Unknown fields and role/provenance overrides are rejected. Text is limited to 4,000 characters; selections to 100 IDs of 128 characters; views to 32 parameters; the serialized request is at most 16 KiB. This endpoint only records data and never executes code.

A SQLite transaction compares `expected_revision` and appends one immutable version. The same event ID and exact request return the original receipt; different content with the same event ID or stale revision returns 409. Replaying an old event cannot replace the latest state. Trust label `live_app_untrusted` and provenance are generated by the server. Snapshot includes `live_state` and `context_delivery`. A delivery receipt means revision X was **prepared** for model input, not that the provider consumed it or that the model followed it.

The App preserves the active runtime identity through Chat→Notebook→Chat navigation. It rejects a stale target before persisting a chat message. Live-state drafts retain their source execution and event ID across retries. The UI displays legacy feedback history read-only; publishing to Agent context uses the separate explicit action.

## Hosted live apps

A bound Notebook hosts one current HTML app. The Agent uses `live_app` with `action: "publish"`, a title, self-contained `html`, the Notebook's completed `execution_id`, and `expected_revision` (zero initially). Publication passes through the existing tool approval gate. The `read` action returns the current document and live state so the Agent can revise the app. It takes its chat session from the turn context; the model cannot choose another chat. HTML returned by the tool remains untrusted data.

Each publication retains an immutable revision in `.omicsclaw/notebooks.db`. The title is at most 120 characters and the UTF-8 HTML document at most 256 KiB. The referenced execution must belong to the Notebook, have completed successfully, and have flushed its event stream. Stale publication revisions return 409. After an uncertain response, read the current app before retrying a publication. The Notebook snapshot and model context contain app metadata; HTML is fetched separately.

| Method | Path | Contract |
| --- | --- | --- |
| GET | `/notebooks/{id}/live-app` | `{live_app: document or null}` |
| PUT | `/notebooks/{id}/live-app` | `{title, html, execution_id, expected_revision}` |
| POST | `/notebooks/{id}/live-app/state` | `{app_revision, client_event_id, expected_revision, kind, payload}` |

The App renders the document in an iframe with `sandbox="allow-scripts"`. Its content security policy blocks fetch and external assets. The parent supplies a small browser API:

```javascript
const context = window.omicsclaw.context();
const receipt = await window.omicsclaw.publishState("view", {
  view_id: "scatter",
  parameters: { threshold: 0.5 },
  selected_ids: ["sample-1"]
});
```

`context()` returns bounded Notebook cell previews and state belonging to the current app. `publishState` accepts the same note, selection and view shapes as `/live-state`; its promise resolves after persistence. The parent routes messages from the current iframe through the authenticated backend. An app never receives credentials or a kernel execution function. State becomes reference data in the next model request; a user sends the next chat message and approves any resulting Python call.

The state transaction checks that `app_revision` is current, resolves the execution from that app, checks the state revision, and records server provenance including `app_revision`. Every event from an older app revision returns 409. Repeating an exact event from the current app returns its original receipt without replacing newer state. App changes survive reload and backend restart; the Python namespace follows the kernel lifetime described above.

### Limitations

Apps contain their own HTML, CSS, JavaScript and data. Server processes, package installation and arbitrary web application hosting are not provided. Iframe sandboxing isolates the parent and its credentials; it is not a complete network isolation mechanism. The tool's existing mutable policy also blocks `live_app` in read-only permission mode.

## Verification

`tests/entry/test_notebook_apps.py` covers document persistence, publication and state revisions, execution ownership, authenticated routes and Agent approval. The acceptance suite also covers shared real-kernel state, permission denial, cancellation during admission/startup/lock waiting, exact cancellation scope, binding persistence, CAS conflicts, idempotent replay, untrusted-state escaping and per-request context injection. The deterministic model fixture in `tests/entry/notebook_closed_loop_server.py` runs the production Desktop API and real Python kernel for browser acceptance; it logs the actual model request. It does not call an external model.

Final immutable source identifiers, commands, counts, logs and any environment limitations are recorded in `docs/NOTEBOOK_DELIVERY.md`. Historical partial regression results are not evidence for that final source snapshot.
