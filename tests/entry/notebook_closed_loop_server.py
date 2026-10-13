"""Real Desktop/kernel fixture with a deterministic model for closed-loop E2E."""

from __future__ import annotations

import argparse
import json
from contextlib import asynccontextmanager
from pathlib import Path

from omicsclaw.entry.desktop import create_desktop_app
from omicsclaw.entry.session import attach_sessions
from omicsclaw.provider import Completion
from omicsclaw.schema import Message, Role, ToolCall
from tests.entry.test_turn_runner import Scripted, make_app


class NotebookProvider(Scripted):
    def __init__(self, log):
        super().__init__()
        self.log = log

    async def generate(self, messages, tools=None):
        with self.log.open("a") as f:
            f.write(
                json.dumps(
                    [{"role": m.role.value, "content": m.content} for m in messages]
                )
                + "\n"
            )
        # Sent-only augmentations may follow the real user/tool messages.
        prompt = next(
            (
                m.content
                for m in reversed(messages)
                if m.role is Role.USER
                and m.content in ("hello", "seed", "read", "state", "blocked")
            ),
            "hello",
        )
        user_index = max(
            (
                i
                for i, m in enumerate(messages)
                if m.role is Role.USER and m.content == prompt
            ),
            default=-1,
        )
        outputs = [m for m in messages[user_index + 1 :] if m.role is Role.TOOL]
        if outputs:
            reply = Message(
                role=Role.ASSISTANT, content="Result: " + str(outputs[-1].content)
            )
        elif prompt in ("seed", "read", "blocked"):
            code = {
                "seed": "value = 40\nprint(value)",
                "read": "print(value + 1)",
                "blocked": "value = -999\nprint(value)",
            }[prompt]
            self.calls += 1
            reply = Message(
                role=Role.ASSISTANT,
                tool_calls=(
                    ToolCall(
                        id=f"fixture-{self.calls}",
                        name="python",
                        arguments=json.dumps({"code": code}),
                    ),
                ),
            )
        elif prompt == "state":
            block = next(
                (
                    m.content
                    for m in reversed(messages)
                    if m.content and m.content.startswith("<notebook_context>")
                ),
                "",
            )
            reply = Message(
                role=Role.ASSISTANT, content="Notebook state received: " + block
            )
        else:
            reply = Message(
                role=Role.ASSISTANT, content="Hello. Ready for notebook binding."
            )
        return Completion(message=reply)


def main():
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--model-log", required=True, type=Path)
    args = parser.parse_args()
    args.workspace.mkdir(parents=True, exist_ok=True)
    app = make_app(args.workspace, NotebookProvider(args.model_log))
    api = create_desktop_app(attach_sessions(app, abandon_grace_s=30))
    desktop_lifespan = api.router.lifespan_context

    @asynccontextmanager
    async def lifespan(server):
        try:
            async with desktop_lifespan(server):
                yield
        finally:
            await app.aclose()

    api.router.lifespan_context = lifespan
    uvicorn.run(
        api,
        host="127.0.0.1",
        port=args.port,
        log_level="warning",
        ws="none",
    )


if __name__ == "__main__":
    main()
