"""Real Desktop and kernel with an interactive-app provider for browser acceptance."""

from __future__ import annotations

import argparse
import json
from contextlib import asynccontextmanager
from pathlib import Path

from omicsclaw.entry.desktop import create_desktop_app
from omicsclaw.entry.session import attach_sessions
from omicsclaw.provider import Completion
from omicsclaw.schema import Message, Role, ToolCall
from tests.entry.notebook_closed_loop_server import NotebookProvider
from tests.entry.test_turn_runner import make_app

APP_HTML = """<html><head><style>
body{margin:0;padding:24px;font:15px system-ui;color:#123;background:#f7fafc}h1{font-size:20px}button,input{padding:8px}svg{display:block;background:white;border:1px solid #ccd;border-radius:8px;width:100%;height:220px}circle{cursor:pointer;fill:#26817b}circle.selected{stroke:#f09838;stroke-width:5}label{display:block;margin:16px 0}td,th{padding:8px;text-align:left}#receipt{color:#28726b}
</style></head><body><h1>Notebook sample explorer VERSION</h1>
<label>Threshold <input id="threshold" aria-label="Threshold" type="range" min="0" max="1" step="0.1" value="0.2"><output id="value">0.2</output></label>
<svg viewBox="0 0 320 220" aria-label="Sample scatterplot"><circle aria-label="sample-1" data-id="sample-1" cx="75" cy="155" r="10"/><circle aria-label="sample-2" data-id="sample-2" cx="150" cy="90" r="10"/><circle aria-label="sample-3" data-id="sample-3" cx="245" cy="40" r="10"/></svg>
<table><thead><tr><th>Sample</th><th>Score</th></tr></thead><tbody><tr><td>sample-1</td><td>0.3</td></tr><tr><td>sample-2</td><td>0.6</td></tr><tr><td>sample-3</td><td>0.9</td></tr></tbody></table>
<p id="receipt">Ready</p><p id="context">Loading notebook context</p>
<script>
const selected=new Set(); const threshold=document.getElementById('threshold');
function share(){document.getElementById('value').textContent=threshold.value;return window.omicsclaw.publishState('view',{view_id:'sample-explorer',parameters:{threshold:Number(threshold.value)},selected_ids:[...selected]}).then(receipt=>{document.getElementById('receipt').textContent='Saved r'+receipt.revision}).catch(error=>{document.getElementById('receipt').textContent=error.message})}
threshold.addEventListener('input',share);
document.querySelectorAll('circle').forEach(circle=>circle.addEventListener('click',()=>{const id=circle.dataset.id;selected.has(id)?selected.delete(id):selected.add(id);circle.classList.toggle('selected',selected.has(id));share()}));
window.omicsclaw.context().then(context=>{document.getElementById('context').textContent='Notebook '+context.notebook_id+' · '+context.cells.length+' cell previews';if(context.state?.kind==='view'){threshold.value=context.state.payload.parameters.threshold;context.state.payload.selected_ids.forEach(id=>selected.add(id));document.getElementById('value').textContent=threshold.value;document.querySelectorAll('circle').forEach(circle=>circle.classList.toggle('selected',selected.has(circle.dataset.id)))}});
</script></body></html>"""


class LiveAppProvider(NotebookProvider):
    async def generate(self, messages, tools=None):
        prompts = ("app-create", "app-update")
        prompt = next(
            (
                m.content
                for m in reversed(messages)
                if m.role is Role.USER
                and not (m.content or "").startswith("<notebook_context>")
            ),
            "",
        )
        if prompt not in prompts:
            return await super().generate(messages, tools)
        with self.log.open("a") as f:
            f.write(
                json.dumps(
                    [{"role": m.role.value, "content": m.content} for m in messages]
                )
                + "\n"
            )
        user_index = max(
            i
            for i, m in enumerate(messages)
            if m.role is Role.USER and m.content == prompt
        )
        if any(m.role is Role.TOOL for m in messages[user_index + 1 :]):
            return Completion(
                message=Message(
                    role=Role.ASSISTANT,
                    content="Interactive app published. Open the notebook to explore samples.",
                )
            )
        block = next(
            m.content
            for m in reversed(messages)
            if m.content and m.content.startswith("<notebook_context>")
        )
        context = json.loads(block.split("\n")[2])
        execution = next(
            c["execution_id"]
            for c in context["recent_cells"]
            if c.get("execution_id") and c["status"] == "completed"
        )
        revision = (context.get("live_app") or {}).get("revision", 0)
        self.calls += 1
        return Completion(
            message=Message(
                role=Role.ASSISTANT,
                tool_calls=(
                    ToolCall(
                        id=f"live-app-fixture-{self.calls}",
                        name="live_app",
                        arguments=json.dumps(
                            {
                                "action": "publish",
                                "title": "Sample explorer",
                                "html": APP_HTML.replace("VERSION", f"v{revision + 1}"),
                                "execution_id": execution,
                                "expected_revision": revision,
                            }
                        ),
                    ),
                ),
            )
        )


def main():
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--model-log", required=True, type=Path)
    args = parser.parse_args()
    args.workspace.mkdir(parents=True, exist_ok=True)
    app = make_app(args.workspace, LiveAppProvider(args.model_log))
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
    uvicorn.run(api, host="127.0.0.1", port=args.port, log_level="warning", ws="none")


if __name__ == "__main__":
    main()
