"""Deployment injection selects one provider per named caller; CLI defaults remain."""

from types import SimpleNamespace

import asyncio

from omicsclaw.entry.assembly import build_app, DeploymentCapabilities
from omicsclaw.entry.config import AppConfig
from omicsclaw.entry.desktop.title import generate_title
from omicsclaw.provider import Completion
from omicsclaw.schema import Message, Role


class Provider:
    name = "recording"

    def __init__(self, text="a title"):
        self.text = text
        self.seen = []

    def bind(self, **overrides):
        self.seen.append(("bind", overrides))
        return self

    async def generate(self, messages, tools=None):
        self.seen.append(("generate", messages))
        return Completion(message=Message(role=Role.ASSISTANT, content=self.text))


def test_composition_selects_summary_and_subagent_roles_without_ambient_config(
    tmp_path,
):
    selected = []
    main = Provider()
    other = Provider()

    def for_role(role):
        selected.append(role)
        return other

    app = build_app(
        AppConfig(workspace=tmp_path, subagents=True),
        provider=main,
        resolved_model="mac-model",
        capabilities=DeploymentCapabilities(notebook=False),
        provider_for_role=for_role,
    )
    try:
        assert app.provider is main
        assert selected == ["subagent", "summary"]
        assert app.provider_for_role("title") is other
        assert app.resolved_model == "mac-model"
    finally:
        asyncio.run(app.aclose())


def test_title_uses_named_injected_caller():
    main, title = Provider("wrong main"), Provider("selected title")
    app = SimpleNamespace(
        provider=main, provider_for_role=lambda role: title if role == "title" else main
    )
    result = asyncio.run(
        generate_title(
            app,
            {"schema_version": 1, "source_request_id": "a" * 32, "user_text": "hello"},
        )
    )
    assert result == (200, {"schema_version": 1, "title": "selected title"})
    assert not main.seen
