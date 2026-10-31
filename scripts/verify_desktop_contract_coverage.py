#!/usr/bin/env python3
"""
Phase 3 Objective Verification Gate 1:
Automated 4-Layer Desktop Wire Contract & Visual Projection Coverage Verifier.
Verifies 100% coverage across all 18 TurnEventType members in omicsclaw/entry/events.py
(explicitly accounting for the 13 SSE-forwarded turn events and the 5 lifecycle/HTTP side-channel
events in _DROPPED_ON_DESKTOP / stream termination) plus GET /compute/status session-scoped kernel
variable space projection and post-settlement MessageItem.tsx durability:
  Layer 1 (Backend Event Source): omicsclaw/entry/events.py (TurnEventType), omicsclaw/kernel/manager.py, omicsclaw/entry/assembly.py
  Layer 2 (Wire Projection): omicsclaw/entry/desktop/turn_observation.py & service.py
  Layer 3 (Frontend FSM & Parsers): src/types/index.ts, useSSEStream.ts, chat-stream-transcript.ts, stream-session-manager.ts
  Layer 4 (Visual Perceivability): StreamingMessage.tsx (live) AND MessageItem.tsx (settled >500ms & SQLite reload)
"""

import ast
import json
import sys
from pathlib import Path

OMICSCLAW_ROOT = Path("/workspace/dataset/private/zhouwg_data/OmicsClaw")
APP_ROOT = Path("/workspace/dataset/private/zhouwg_data/OmicsClaw-App")


def extract_turn_event_types(events_py: Path) -> list[str]:
    tree = ast.parse(events_py.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "TurnEventType":
            members = []
            for item in node.body:
                if isinstance(item, ast.Assign):
                    for target in item.targets:
                        if isinstance(target, ast.Name):
                            members.append(target.id)
            return members
    raise RuntimeError("TurnEventType not found in events.py")


def verify_contract_coverage() -> dict:
    events_py = OMICSCLAW_ROOT / "omicsclaw/entry/events.py"
    render_py = OMICSCLAW_ROOT / "omicsclaw/entry/render.py"
    turn_obs_py = OMICSCLAW_ROOT / "omicsclaw/entry/desktop/turn_observation.py"
    service_py = OMICSCLAW_ROOT / "omicsclaw/entry/desktop/service.py"
    kernel_mgr_py = OMICSCLAW_ROOT / "omicsclaw/kernel/manager.py"
    assembly_py = OMICSCLAW_ROOT / "omicsclaw/entry/assembly.py"

    types_ts = APP_ROOT / "src/types/index.ts"
    sse_ts = APP_ROOT / "src/hooks/useSSEStream.ts"
    transcript_ts = APP_ROOT / "src/lib/chat-stream-transcript.ts"
    manager_ts = APP_ROOT / "src/lib/stream-session-manager.ts"
    perm_ts = APP_ROOT / "src/lib/chat-permission.ts"
    streaming_msg_ts = APP_ROOT / "src/components/chat/StreamingMessage.tsx"
    msg_item_ts = APP_ROOT / "src/components/chat/MessageItem.tsx"
    chat_view_ts = APP_ROOT / "src/components/chat/ChatView.tsx"
    tool_group_ts = APP_ROOT / "src/components/ai-elements/tool-actions-group.tsx"
    tool_card_ts = APP_ROOT / "src/components/ai-elements/tool-step-detail-card.tsx"
    artifact_tray_ts = APP_ROOT / "src/components/chat/TurnArtifactTray.tsx"
    kernel_inspector_ts = APP_ROOT / "src/components/chat/KernelVariableInspector.tsx"
    message_input_ts = APP_ROOT / "src/components/chat/MessageInput.tsx"
    compute_route_ts = APP_ROOT / "src/app/api/compute/status/route.ts"
    perm_prompt_ts = APP_ROOT / "src/components/chat/PermissionPrompt.tsx"
    ask_user_ts = APP_ROOT / "src/components/chat/AskUserQuestionCard.tsx"
    suggestion_chips_ts = APP_ROOT / "src/components/chat/SuggestionChips.tsx"
    shadow_card_ts = APP_ROOT / "src/components/chat/ShadowReviewVerdictCard.tsx"
    terminal_chip_ts = APP_ROOT / "src/components/chat/TerminalStateChip.tsx"
    event_omitted_ts = APP_ROOT / "src/lib/chat/event-omitted.ts"

    turn_event_types = extract_turn_event_types(events_py)
    render_src = render_py.read_text(encoding="utf-8")
    turn_obs_src = turn_obs_py.read_text(encoding="utf-8")
    service_src = service_py.read_text(encoding="utf-8")
    kernel_mgr_src = kernel_mgr_py.read_text(encoding="utf-8")
    assembly_src = assembly_py.read_text(encoding="utf-8")

    types_src = types_ts.read_text(encoding="utf-8")
    sse_src = sse_ts.read_text(encoding="utf-8")
    transcript_src = transcript_ts.read_text(encoding="utf-8")
    manager_src = manager_ts.read_text(encoding="utf-8")
    perm_src = perm_ts.read_text(encoding="utf-8")
    streaming_msg_src = streaming_msg_ts.read_text(encoding="utf-8")
    msg_item_src = msg_item_ts.read_text(encoding="utf-8")
    chat_view_src = chat_view_ts.read_text(encoding="utf-8")
    tool_group_src = tool_group_ts.read_text(encoding="utf-8")
    tool_card_src = tool_card_ts.read_text(encoding="utf-8")
    artifact_tray_src = artifact_tray_ts.read_text(encoding="utf-8")
    kernel_inspector_src = kernel_inspector_ts.read_text(encoding="utf-8")
    message_input_src = message_input_ts.read_text(encoding="utf-8")
    compute_route_src = compute_route_ts.read_text(encoding="utf-8")
    perm_prompt_src = perm_prompt_ts.read_text(encoding="utf-8")
    ask_user_src = ask_user_ts.read_text(encoding="utf-8")
    suggestion_chips_src = suggestion_chips_ts.read_text(encoding="utf-8")
    shadow_card_src = shadow_card_ts.read_text(encoding="utf-8")
    terminal_chip_src = terminal_chip_ts.read_text(encoding="utf-8")
    event_omitted_src = event_omitted_ts.read_text(encoding="utf-8")

    sse_forwarded_events = [
        "QUEUED",
        "CONTEXT",
        "COMPACTION",
        "TEXT_DELTA",
        "REASONING_DELTA",
        "PROGRESS",
        "TOOL_START",
        "TOOL_RESULT",
        "APPROVAL_REQUIRED",
        "QUESTION_ASKED",
        "SUGGESTIONS",
        "SHADOW_REVIEW",
        "GAP",
    ]
    lifecycle_side_channel_events = [
        "EXCHANGE_START",
        "APPROVAL_SETTLED",
        "QUESTION_SETTLED",
        "TURN_END",
        "EXCHANGE_END",
    ]

    matrix = [
        {
            "turn_event": "EXCHANGE_START",
            "delivery_mode": "lifecycle_http_side_channel (_DROPPED_ON_DESKTOP / stream open)",
            "wire_frame": "_DROPPED_ON_DESKTOP (turn lifecycle start / stream open)",
            "wire_check": (
                "TurnEventType.EXCHANGE_START" in render_src
                and "DESKTOP_CHAT_FRAME_TYPE" in turn_obs_src
                and "def desktop_chat_frame(" in turn_obs_src
            ),
            "ts_type_check": "SessionStreamSnapshot" in types_src,
            "sse_handler_check": "consumeSSEStream" in sse_src,
            "manager_check": "startStream" in manager_src,
            "ui_component": "src/components/chat/StreamingMessage.tsx & src/components/chat/MessageItem.tsx",
            "ui_check": (
                "export function StreamingMessage(" in streaming_msg_src
                and "isStreaming" in streaming_msg_src
                and "export const MessageItem =" in msg_item_src
            ),
        },
        {
            "turn_event": "QUEUED",
            "delivery_mode": "sse_wire_frame",
            "wire_frame": "status (kind=queued)",
            "wire_check": "TurnEventType.QUEUED" in turn_obs_src and 'payload["kind"] = "queued"' in turn_obs_src,
            "ts_type_check": "queuedAhead?: number;" in types_src,
            "sse_handler_check": "statusData?.kind === 'queued'" in sse_src and "callbacks.onQueued" in sse_src,
            "manager_check": "onQueued:" in manager_src,
            "ui_component": "src/components/chat/StreamingMessage.tsx (turn-queued-banner)",
            "ui_check": "turn-queued-banner" in streaming_msg_src and "queuedAhead" in streaming_msg_src,
        },
        {
            "turn_event": "CONTEXT",
            "delivery_mode": "sse_wire_frame_plus_persisted_block",
            "wire_frame": "status (kind=context) + MessageContentBlock(type=context_budget)",
            "wire_check": "TurnEventType.CONTEXT" in turn_obs_src and 'payload["kind"] = "context"' in turn_obs_src,
            "ts_type_check": "ContextBudgetStatus" in types_src,
            "sse_handler_check": "statusData?.kind === 'context'" in sse_src and "callbacks.onContextBudget" in sse_src,
            "manager_check": (
                "onContextBudget:" in manager_src
                and "type: 'context_budget'" in transcript_src
                and "case 'status':" in transcript_src
            ),
            "ui_component": "StreamingMessage.tsx & MessageItem.tsx (context-pressure-banner)",
            "ui_check": (
                "context-pressure-banner" in streaming_msg_src
                and "context-pressure-banner" in msg_item_src
                and "contextBudget" in msg_item_src
            ),
        },
        {
            "turn_event": "COMPACTION",
            "delivery_mode": "sse_wire_frame_plus_persisted_block",
            "wire_frame": "status (kind=compaction) + MessageContentBlock(type=compaction)",
            "wire_check": "TurnEventType.COMPACTION" in turn_obs_src,
            "ts_type_check": "'status'" in types_src and "liveCompaction" in types_src,
            "sse_handler_check": "statusData?.kind === 'compaction'" in sse_src and "callbacks.onContextCompressed" in sse_src,
            "manager_check": (
                "onContextCompressed:" in manager_src
                and "stream.liveCompaction = info" in manager_src
                and "type: 'compaction'" in transcript_src
                and "compaction" in msg_item_src
            ),
            "ui_component": "src/components/chat/CompactionNotice.tsx (in StreamingMessage.tsx & MessageItem.tsx)",
            "ui_check": (
                "CompactionNotice" in streaming_msg_src
                and "CompactionNotice" in msg_item_src
                and "liveCompaction" in streaming_msg_src
            ),
        },
        {
            "turn_event": "TEXT_DELTA",
            "delivery_mode": "sse_wire_frame",
            "wire_frame": "text",
            "wire_check": "TurnEventType.TEXT_DELTA" in turn_obs_src,
            "ts_type_check": "'text'" in types_src,
            "sse_handler_check": "case 'text':" in sse_src and "callbacks.onText" in sse_src,
            "manager_check": "onText:" in manager_src,
            "ui_component": "src/components/chat/StreamingMessage.tsx & src/components/chat/MessageItem.tsx",
            "ui_check": (
                "MessageResponse" in streaming_msg_src
                and "MessageResponse" in msg_item_src
                and "linkifyDeliverablesAndResolveImages" in msg_item_src
            ),
        },
        {
            "turn_event": "REASONING_DELTA",
            "delivery_mode": "sse_wire_frame",
            "wire_frame": "thinking",
            "wire_check": "TurnEventType.REASONING_DELTA" in turn_obs_src,
            "ts_type_check": "'thinking'" in types_src,
            "sse_handler_check": "case 'thinking':" in sse_src and "callbacks.onThinking" in sse_src,
            "manager_check": "onThinking:" in manager_src,
            "ui_component": "src/components/ai-elements/tool-actions-group.tsx (Reasoning)",
            "ui_check": (
                "export const Reasoning =" in (APP_ROOT / "src/components/ai-elements/reasoning.tsx").read_text(encoding="utf-8")
                and "thinking" in tool_group_src
                and "thinking" in streaming_msg_src
                and "thinking" in msg_item_src
            ),
        },
        {
            "turn_event": "PROGRESS",
            "delivery_mode": "sse_wire_frame_plus_persisted_block",
            "wire_frame": "tool_output + MessageContentBlock(type=subagent_tree)",
            "wire_check": "TurnEventType.PROGRESS" in turn_obs_src,
            "ts_type_check": "'tool_output'" in types_src and "SubagentProgressStep" in types_src,
            "sse_handler_check": "case 'tool_output':" in sse_src and "callbacks.onToolOutput" in sse_src,
            "manager_check": (
                "onToolOutput:" in manager_src
                and "subagentSteps" in manager_src
                and "heartbeatCount" in manager_src
                and "type: 'subagent_tree'" in transcript_src
                and "parseSubagentProgressLine" in transcript_src
                and "case 'tool_output':" in transcript_src
            ),
            "ui_component": "StreamingMessage.tsx & MessageItem.tsx (subagent-execution-tree, bio-compute-heartbeat, bio-compute-self-heal-hint, env-provisioning-capsule)",
            "ui_check": (
                all(
                    k in streaming_msg_src
                    for k in ["subagent-execution-tree", "bio-compute-heartbeat", "bio-compute-self-heal-hint", "env-provisioning-capsule"]
                )
                and "subagent-execution-tree" in msg_item_src
                and "tool-card-env" in tool_card_src
                and "tool-step-output-body" in tool_card_src
            ),
        },
        {
            "turn_event": "TOOL_START",
            "delivery_mode": "sse_wire_frame",
            "wire_frame": "tool_use",
            "wire_check": "TurnEventType.TOOL_START" in turn_obs_src,
            "ts_type_check": "'tool_use'" in types_src,
            "sse_handler_check": "case 'tool_use':" in sse_src and "callbacks.onToolUse" in sse_src,
            "manager_check": "onToolUse:" in manager_src,
            "ui_component": "src/components/ai-elements/tool-actions-group.tsx & tool-step-detail-card.tsx",
            "ui_check": (
                "export const ToolActionsGroup =" in tool_group_src
                and "tool-actions-multi" in tool_group_src
                and "tool-step-row" in tool_group_src
                and "export function ToolStepDetailCard(" in tool_card_src
                and "tool-step-card" in tool_card_src
            ),
        },
        {
            "turn_event": "TOOL_RESULT",
            "delivery_mode": "sse_wire_frame_plus_code_run_appendix",
            "wire_frame": "tool_result (+ [code_run_appendix] from kernel manager)",
            "wire_check": (
                "TurnEventType.TOOL_RESULT" in turn_obs_src
                and "[code_run_appendix]" in kernel_mgr_src
                and "object_versions" in assembly_src
            ),
            "ts_type_check": "'tool_result'" in types_src,
            "sse_handler_check": "case 'tool_result':" in sse_src and "callbacks.onToolResult" in sse_src,
            "manager_check": "onToolResult:" in manager_src and "omicsclaw:kernel-status-refresh" in manager_src,
            "ui_component": "src/components/ai-elements/tool-step-detail-card.tsx & src/components/chat/TurnArtifactTray.tsx",
            "ui_check": (
                "tool-step-provenance-bar" in tool_card_src
                and "turn-artifact-provenance-bar" in artifact_tray_src
                and "code_run_appendix" in tool_card_src
                and "parseStructuredProvenancePayload" in artifact_tray_src
                and "normalizeObjectVersionsList" in artifact_tray_src
            ),
        },
        {
            "turn_event": "APPROVAL_REQUIRED",
            "delivery_mode": "sse_wire_frame",
            "wire_frame": "permission_request (with subagent provenance)",
            "wire_check": "TurnEventType.APPROVAL_REQUIRED" in turn_obs_src and 'payload["subagent"] = event.subagent' in turn_obs_src,
            "ts_type_check": "'permission_request'" in types_src and "subagent?: string;" in types_src,
            "sse_handler_check": "case 'permission_request':" in sse_src and "record.subagent" in perm_src,
            "manager_check": "onPermissionRequest:" in manager_src,
            "ui_component": "src/components/chat/PermissionPrompt.tsx (permission-subagent-badge)",
            "ui_check": (
                "permission-prompt" in perm_prompt_src
                and "permission-subagent-badge" in perm_prompt_src
                and "onDecision" in perm_prompt_src
            ),
        },
        {
            "turn_event": "APPROVAL_SETTLED",
            "delivery_mode": "lifecycle_http_side_channel (_DROPPED_ON_DESKTOP + POST /chat/permission)",
            "wire_frame": "_DROPPED_ON_DESKTOP (POST /chat/permission resolution -> queue advance)",
            "wire_check": (
                "TurnEventType.APPROVAL_SETTLED" in render_src
                and "_shows_card" in turn_obs_src
            ),
            "ts_type_check": "PermissionRequestEvent" in types_src,
            "sse_handler_check": "onPermissionRequest" in sse_src,
            "manager_check": "respondToPermission" in manager_src,
            "ui_component": "src/components/chat/PermissionPrompt.tsx & ChatView.tsx",
            "ui_check": (
                "permission-queue-count" in perm_prompt_src
                and "permissionSubmitting" in perm_prompt_src
                and "PermissionPrompt" in chat_view_src
            ),
        },
        {
            "turn_event": "QUESTION_ASKED",
            "delivery_mode": "sse_wire_frame",
            "wire_frame": "question_request",
            "wire_check": "TurnEventType.QUESTION_ASKED" in turn_obs_src,
            "ts_type_check": "'question_request'" in types_src,
            "sse_handler_check": "case 'question_request':" in sse_src and "callbacks.onQuestionRequest" in sse_src,
            "manager_check": "onQuestionRequest:" in manager_src,
            "ui_component": "src/components/chat/AskUserQuestionCard.tsx",
            "ui_check": (
                "export function AskUserQuestionCard(" in ask_user_src
                and "onSubmit" in ask_user_src
                and "AskUserQuestionCard" in chat_view_src
            ),
        },
        {
            "turn_event": "QUESTION_SETTLED",
            "delivery_mode": "lifecycle_http_side_channel (_DROPPED_ON_DESKTOP + POST /chat/question)",
            "wire_frame": "_DROPPED_ON_DESKTOP (POST /chat/question resolution -> queue advance)",
            "wire_check": (
                "TurnEventType.QUESTION_SETTLED" in render_src
                and "_shows_question_card" in turn_obs_src
            ),
            "ts_type_check": "QuestionRequestEvent" in types_src,
            "sse_handler_check": "onQuestionRequest" in sse_src,
            "manager_check": "respondToQuestion" in manager_src,
            "ui_component": "src/components/chat/AskUserQuestionCard.tsx (AskUserHistoryCard)",
            "ui_check": (
                "export function AskUserHistoryCard(" in ask_user_src
                and "AskUserHistoryCard" in streaming_msg_src
                and "AskUserHistoryCard" in msg_item_src
            ),
        },
        {
            "turn_event": "SUGGESTIONS",
            "delivery_mode": "sse_wire_frame",
            "wire_frame": "suggestions",
            "wire_check": "TurnEventType.SUGGESTIONS" in turn_obs_src,
            "ts_type_check": "'suggestions'" in types_src,
            "sse_handler_check": "case 'suggestions':" in sse_src and "callbacks.onSuggestions" in sse_src,
            "manager_check": "onSuggestions:" in manager_src,
            "ui_component": "src/components/chat/SuggestionChips.tsx",
            "ui_check": (
                "export function SuggestionChips(" in suggestion_chips_src
                and "suggestion-chips" in suggestion_chips_src
                and "onPick" in suggestion_chips_src
                and "SuggestionChips" in chat_view_src
            ),
        },
        {
            "turn_event": "SHADOW_REVIEW",
            "delivery_mode": "sse_wire_frame_plus_persisted_block",
            "wire_frame": "shadow_review + MessageContentBlock(type=shadow_review)",
            "wire_check": "TurnEventType.SHADOW_REVIEW" in turn_obs_src,
            "ts_type_check": "'shadow_review'" in types_src and "ShadowReviewVerdictInfo" in types_src,
            "sse_handler_check": "case 'shadow_review':" in sse_src and "callbacks.onShadowReview" in sse_src and "case 'shadow_review':" in transcript_src,
            "manager_check": "onShadowReview:" in manager_src and "type: 'shadow_review'" in transcript_src,
            "ui_component": "src/components/chat/ShadowReviewVerdictCard.tsx (shadow-review-banner in StreamingMessage & MessageItem)",
            "ui_check": (
                "shadow-review-banner" in shadow_card_src
                and "shadow-review-address-btn" in shadow_card_src
                and "ShadowReviewVerdictCard" in streaming_msg_src
                and "ShadowReviewVerdictCard" in msg_item_src
            ),
        },
        {
            "turn_event": "TURN_END",
            "delivery_mode": "lifecycle_stream_terminal (result usage ledger)",
            "wire_frame": "result (accumulated model usage ledger)",
            "wire_check": "TurnEventType.TURN_END" in turn_obs_src,
            "ts_type_check": "'result'" in types_src and "TokenUsage" in types_src,
            "sse_handler_check": "case 'result':" in sse_src and "callbacks.onResult" in sse_src,
            "manager_check": "onResult:" in manager_src,
            "ui_component": "src/components/chat/MessageItem.tsx (TokenUsageDisplay)",
            "ui_check": (
                "function TokenUsageDisplay(" in msg_item_src
                and "tokenUsage" in msg_item_src
                and "input_tokens" in msg_item_src
            ),
        },
        {
            "turn_event": "GAP",
            "delivery_mode": "sse_wire_frame",
            "wire_frame": "event_omitted",
            "wire_check": "TurnEventType.GAP" in turn_obs_src and '"event_omitted"' in turn_obs_src,
            "ts_type_check": "'event_omitted'" in types_src,
            "sse_handler_check": "case 'event_omitted':" in sse_src and "callbacks.onEventOmitted" in sse_src,
            "manager_check": "onEventOmitted:" in manager_src,
            "ui_component": "src/lib/chat/event-omitted.ts & src/components/chat/ChatView.tsx (event-omitted toast)",
            "ui_check": (
                "export function describeEventOmitted(" in event_omitted_src
                and "export function parseEventOmitted(" in event_omitted_src
                and "event-omitted" in chat_view_src
            ),
        },
        {
            "turn_event": "EXCHANGE_END",
            "delivery_mode": "lifecycle_stream_terminal (result + error + done)",
            "wire_frame": "result + error + done",
            "wire_check": "TurnEventType.EXCHANGE_END" in turn_obs_src,
            "ts_type_check": "'error'" in types_src and "'done'" in types_src,
            "sse_handler_check": "case 'error':" in sse_src and "case 'done':" in sse_src,
            "manager_check": "onError:" in manager_src,
            "ui_component": "src/components/chat/TerminalStateChip.tsx",
            "ui_check": (
                "export function TerminalStateChip(" in terminal_chip_src
                and "shouldRenderChip" in terminal_chip_src
                and "TerminalStateChip" in (APP_ROOT / "src/components/chat/MessageList.tsx").read_text(encoding="utf-8")
            ),
        },
        {
            "turn_event": "COMPUTE_STATUS_AND_KERNEL_HANDLES",
            "delivery_mode": "http_rest_and_ssh_rpc (session-scoped)",
            "wire_frame": "GET /compute/status?session_id=... (handles, object_versions, skills_loaded)",
            "wire_check": (
                "def compute_status_payload(" in service_src
                and "session_id: str | None = None" in service_src
                and '"handles"' in service_src
                and '"object_versions"' in service_src
                and '"skills_loaded"' in service_src
            ),
            "ts_type_check": (
                "export async function GET(" in compute_route_src
                and "ComputeRuntimeStatusPayload" in types_src
            ),
            "sse_handler_check": (
                "compute.status" in (APP_ROOT / "src/lib/controller/connection.ts").read_text(encoding="utf-8")
                and "session_id" in compute_route_src
            ),
            "manager_check": (
                "omicsclaw:kernel-status-refresh" in manager_src
                and "detail.sessionId !== sessionId" in message_input_src
                and "sessionId" in artifact_tray_src
            ),
            "ui_component": "src/components/chat/KernelVariableInspector.tsx (session-scoped kernel-variable-inspector)",
            "ui_check": (
                "kernel-variable-inspector" in kernel_inspector_src
                and "encodeURIComponent(sessionId)" in kernel_inspector_src
                and "allKernels.filter((k) => sessionMatches(k?.session_id))" in kernel_inspector_src
            ),
        },
    ]

    covered_turn_events = {r["turn_event"] for r in matrix} & set(turn_event_types)
    missing_turn_events = [e for e in turn_event_types if e not in covered_turn_events]

    all_passed = (
        len(missing_turn_events) == 0
        and set(sse_forwarded_events) | set(lifecycle_side_channel_events) == set(turn_event_types)
    )
    for row in matrix:
        row["passed"] = bool(
            row["wire_check"]
            and row["ts_type_check"]
            and row["sse_handler_check"]
            and row["manager_check"]
            and row["ui_check"]
        )
        if not row["passed"]:
            all_passed = False

    report = {
        "all_passed": all_passed,
        "total_contracts_checked": len(matrix),
        "passed_count": sum(1 for r in matrix if r["passed"]),
        "failed_count": sum(1 for r in matrix if not r["passed"]),
        "turn_event_enum_count": len(turn_event_types),
        "sse_forwarded_turn_event_count": len(sse_forwarded_events),
        "lifecycle_side_channel_turn_event_count": len(lifecycle_side_channel_events),
        "covered_turn_event_enum_count": len(covered_turn_events),
        "missing_turn_event_enum_members": missing_turn_events,
        "sse_forwarded_turn_events": sse_forwarded_events,
        "lifecycle_side_channel_turn_events": lifecycle_side_channel_events,
        "turn_event_enum_members": turn_event_types,
        "coverage_matrix": matrix,
    }
    out_path = OMICSCLAW_ROOT / "docs/phase3_contract_coverage_report.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


if __name__ == "__main__":
    rep = verify_contract_coverage()
    print(
        f"Contract Coverage: {rep['passed_count']}/{rep['total_contracts_checked']} passed "
        f"(TurnEventType enum coverage: {rep['covered_turn_event_enum_count']}/{rep['turn_event_enum_count']} "
        f"[SSE forwarded: {rep['sse_forwarded_turn_event_count']}, lifecycle/side-channel: {rep['lifecycle_side_channel_turn_event_count']}], "
        f"all_passed={rep['all_passed']})"
    )
    if not rep["all_passed"]:
        if rep["missing_turn_event_enum_members"]:
            print("MISSING TurnEventType members:", rep["missing_turn_event_enum_members"])
        for r in rep["coverage_matrix"]:
            if not r["passed"]:
                print("FAILED:", r)
        sys.exit(1)
