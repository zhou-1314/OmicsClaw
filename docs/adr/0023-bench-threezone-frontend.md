# Bench (v1): three-zone frontend architecture

**Status:** accepted (2026-05-30)

The `/bench` page is a three-zone workspace (thread rail | chat | stage rail) layered onto
OmicsClaw-App's existing shell (Electron + Next.js 16 + React 19). Eight decisions:

**1. Shell — auto-collapse global nav to 64px on /bench.** The always-docked `ChatListPanel`
(`AppShell.tsx:161`, no longer route-gated) would make Bench's three zones a *fourth* column; on
`/bench` it auto-collapses to the existing 64px icon rail (reusing `toggleChatListCollapsed`), reading
as a nav gutter rather than a content column. Bench owns three flex zones in the main area; global nav
stays one click away (plan §9). No ChatListPanel fork. (Rejected: hide nav entirely = /chat unreachable
from Bench; fold threads into ChatListPanel = couples thread logic into the global session list.)

**2. Conversation — one thread = one conversation; stage is a per-turn lens.** `thread_id ↔ session_id`
is 1:1; switching stage keeps the same message list, re-sends `stage` next turn, and swaps the
right-rail panel — mirroring the existing `mode` lens (`ChatView.tsx:112/193`). Cross-stage recall is
automatic (AN-CTXRECALL-11). `stage` is client-side for v1. Two refinements: each message carries a
**stage badge** (the transcript spans stages with shifting capabilities), and reload restores the last
stage. (Rejected: session-per-stage = breaks recall + the permissive switch; persisted stage column =
premature.)

**3. Binding — thread is backend-authoritative; `thread_id` is anchored on the session row.** Thread
metadata lives at `project://<thread_id>` (plan §3); the chat session is created lazily on first
message and **stamped with `thread_id` server-side**. The backend resolves
`thread_id = request.thread_id ?? session.thread_id`, so a turn that omits the field (e.g. a plain
/chat turn on a thread-bound session) still lands artifacts under `analysis://<thread_id>`. `thread_id`
= durable (session-anchored); `stage` = ephemeral (per-request). (Rejected: pre-create empty session;
client-only localStorage map = invisible to backend lineage.)

**4. Stage switcher in the composer; permissive switch is proposed, not silent.** A Radix-Tabs stage
strip sits beside the existing `ModeIndicator` in the composer action bar (`ChatView.tsx:696`). The
permissive "switch to Analyze?" is a one-click **proposal card** (AskUserCard/TodoPlanCard precedent),
NOT the existing silent `onModeChanged` auto-apply (`stream-session-manager.ts:591`) — ADR 0020
requires proposal + consent. **`mode` is hidden in Bench**; each stage sets a default behavioral posture
(one control, no mode/stage conflict). Accepting a proposal **re-runs the triggering message** under
the new stage. (Rejected: silent auto-switch; two visible segmented controls.)

**5. Stage rail — bench-owned, exclusive-by-stage.** A new `src/components/bench/StageRail` swaps a
per-stage panel (Read/Ideate/Analyze/Write), mirroring `OptimizeRightPanel`'s exclusive-tab pattern —
NOT `RightPanel` (gated to /chat, mixed exclusive/composable logic). File artifacts embed the
standalone `FilePreview` via **prop**, not the global `PanelContext.previewFile` singleton. v1 is
exclusive-by-stage; **cross-stage card pinning is a fast-follow**. (Rejected: extend RightPanel =
coupling + isChatRoute fork; artifacts inside the transcript = violates three-zone.)

**6. Card→composer injection — a structured in-page callback.** Because the thread rail, chat, and
stage rail share one `BenchChatArea` wrapper, a card action calls a typed
`onCardAction({text, stage, systemPromptAppend, displayOverride, threadId})` into the existing
`sendMessage(...)` (`ChatView.tsx:488`) — NOT the global, lossy `fill-message-input` window event
(`MessageInput.tsx:106`, a multi-composer collision hazard). An **explicit card click is consent** (it
may switch stage directly, skipping the decision-4 proposal card, which is only for *inferred* typed
intent); the injection **prefills the composer and switches stage but does not auto-send**. A
`sessionId`-scoped event is reserved only for the cross-surface bridge (decision 8). (Rejected: global
event; auto-send.)

**7. Route shape — `/bench/[threadId]?stage=…`.** Thread (durable) is a path segment, matching the
`/chat/[sessionId]` convention; stage (ephemeral lens) is a query param. Both restore on reload;
localStorage is only the bare-`/bench` last-thread fallback. `stage` in the URL serves **single-user
deep-linking only** — threads are per-user (namespace `app/<user_id>`), so a URL is **not cross-user
shareable**. (Amends the plan's earlier `?threadId=` query form. Rejected: stage in localStorage = not
deep-linkable.)

**8. Bridge & soft-fail.** *Run-in-Chat* = `router.push` to `/chat/[sessionId]` (same session); it drops
the stage lens (full-power console) but **keeps the thread binding** (anchored on the session, decision
3), so artifacts still roll up to the thread; the stage badge marks the transition. Thread-bound
sessions are **hidden from the /chat default list** (reachable via Bench + the bridge link).
*Save-to-Bench* from /chat **files the artifact/result reference** into a picked/created thread — it does
**not** rebind the whole conversation (adoption is v1.5). *KG-dark* = Read/Ideate panels disabled with a
one-click install prompt while **Analyze runs unaffected**; the paper-drop input is disabled while dark
(nothing is queued or lost); `useKGStatus` (the `hasProvider` degraded-gate pattern, `chat/page.tsx:67`)
auto-dismisses on KG-live. Per-stage empty states (empty thread, empty Ideate-no-corpus, KG-dark) are
enumerated copy. (Rejected: fork-the-conversation; always-new-thread; queue-drops-while-dark.)

## Consequences

- `chat_sessions` gains a `thread_id` column + backend resolve (`request ?? session`) — amends plan §3 /
  `BE-THREAD-CRUD-2`.
- The route is `/bench/[threadId]` (a dynamic route folder) — amends `FE-THREADID-URL-BINDING`'s
  `?threadId=` query form.
- Bench hides `mode`; `stage` drives the default posture.
- Leaf reuse: `ModeIndicator` shape (stage switcher), `FilePreview` (prop-driven), AskUserCard/
  TodoPlanCard (proposal card), `hasProvider` gate (`useKGStatus`). No `RightPanel`/`SplitContext` reuse.
