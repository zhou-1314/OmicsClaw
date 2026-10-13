# Hosted live-app delivery — 2026-10-13

OmicsClaw now hosts an interactive HTML/JS application inside each Notebook. Chat publishes the app after approval, controls save structured state, and subsequent model requests receive that state alongside the shared Notebook execution history. The implementation uses the existing kernel, NotebookStore and Desktop transport.

## Delivered behavior

- One current app per Notebook, retained revisions, 256 KiB HTML limit and completed-execution association. Publishing waits for durable execution output and uses revision CAS.
- The `live_app` Agent tool reads the current document or requests approval to publish it. The tool derives the Notebook from the current chat binding.
- An opaque-origin iframe runs inline scripts. `window.omicsclaw.context()` supplies bounded Notebook previews and matching state; `publishState(kind, payload)` saves notes, selections or view parameters.
- Frame identity and app revision are checked before forwarding. State is stored transactionally with server-owned provenance. Superseded frames cannot write to the new app.
- The next model request receives `live_app_untrusted` data in a user-role context block. App interaction does not execute Python, send a chat turn or answer an approval.

API details and the SDK contract are in [Notebook documentation](core-features/notebook.md). App behavior and usage are documented in the App repository's project memory and Notebook documentation.

## Real-model acceptance

The production standalone App, production Desktop assembly and persistent Python kernel ran with `deepseek / deepseek-v4-flash`, using synthetic numbers only.

| Step | Observed result |
| --- | --- |
| Agent Python, approved in UI | `values = [2, 5, 9, 11]`; `kernel_marker = "shared-live-kernel"` |
| Agent `live_app`, approved in UI | Model-generated HTML published as app revision 1 |
| Browser control | Threshold 6; selected IDs `9`, `11`; durable state revision 1 |
| User Notebook cell | Appended 7 to the existing `values` |
| Agent continuation, approved in UI | `sum(v for v in values if v >= threshold)` printed `shared-live-kernel` and `27` |

The successful case made 6 actual model requests and recorded 3 approval actions and 3 completed durable executions (Agent, user, Agent). Provider usage totals were 53,036 input tokens, 3,476 output tokens and 45,312 cache-read tokens. Two later model requests contained threshold 6 as user-role, untrusted Notebook context. The acceptance test passed in 24.7 seconds against the production build.

The independent reviewer checked the actual Python source, model completions, incoming context, persisted app/state and execution rows. No blocking findings remain.

## Verification

| Check | Result |
| --- | --- |
| New backend acceptance cases, including independent review | 14 passed |
| Framework CI unit selection, including serial sandbox cases | 8,712 passed; 24 skipped; 3 xfailed; 3 xpassed |
| Scripted eval | 30 passed |
| Public Desktop contracts, included in framework selection | 154 passed |
| Known-v3 and current App compatibility | 6 scenarios passed |
| App unit tests | 2,307 passed; 4 skipped |
| App TypeScript / lint | Passed; 0 lint errors, 55 existing warnings |
| Production build and Electron bundler | Passed |
| Scripted model with real browser and kernel | Passed: approval, controls, durable reload, chat refeed, denied update, new revision, stale-frame rejection and CSP |
| Independent browser probe | Matching SDK context; sibling iframe could not forge a state write |
| Production standalone smoke | Cold page and iframe rendered; context and state persistence worked; no page errors |
| Real-model browser loop | Passed, result 27 in the shared kernel |

The backend feature commit is `e0211754`; the App feature commit is `905a39f`. The checks used the integrated workspace, including the user's existing uncommitted changes, which remain unmodified by these commits. The fixed App build source is `/tmp/omics-liveapp-ui-build`; the standalone runtime is `/tmp/omics-liveapp-ui-production-smoke`.

Verification followed the Level 4 contract-related selection: framework unit tests, scripted eval, Desktop compatibility and real-model UI acceptance. Unchanged scientific skill suites and native installer generation were not rerun.

## Reproduction

Use isolated workspace and App data directories. The opt-in server reads the backend repository's configured provider without printing credentials:

```bash
python -m tests.entry.notebook_live_model_server \
  --workspace /tmp/liveapp-acceptance-workspace \
  --port 18895 \
  --model-log /tmp/liveapp-acceptance-model.jsonl
```

Start the App against that backend with its own `OMICSCLAW_DATA_DIR`, remote connection mode and matching workspace. Then run from the App repository:

```bash
NOTEBOOK_LIVE_URL=http://127.0.0.1:3464 \
NOTEBOOK_LIVE_MODEL_WORKSPACE=/tmp/liveapp-acceptance-workspace \
NOTEBOOK_LIVE_MODEL_LOG=/tmp/liveapp-acceptance-model.jsonl \
  npx playwright test -c playwright.notebook.config.ts live-backend-live-app-model.spec.ts
```

The browser test approves the requested synthetic Python and app publication through the normal UI. Real credentials and network access are required. Final evidence is retained at `/workspace/dataset/private/zhouwg_data/OmicsClaw-deliveries/live-app-20261013`, including the successful request/completion log, result JSON, screenshots and independent review.

## Limits

Hosting supports self-contained HTML/CSS/JS, with one current app per Notebook. External assets, fetch/WebSocket connections and arbitrary server processes are outside this host. The iframe has no parent DOM, storage, kernel or approval capability. Browser self-navigation is not a complete network-isolation boundary.

Saved interaction state is available through the SDK; the app's own code controls how it restores its widgets. State becomes model input on the next chat request. The real-model result covers one provider and a synthetic workflow; it does not establish scientific-analysis accuracy or equivalence to Claude Science's private implementation.
