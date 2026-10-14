"""An injected SDK client factory follows every provider binding."""

import pytest

from omicsclaw.provider import ProviderConfig, ProviderDialect, provider_for


@pytest.mark.parametrize("dialect", list(ProviderDialect))
def test_public_client_factory_is_lazy_and_survives_bind(dialect):
    created = []

    def create(config):
        created.append(config)
        return object()

    original = provider_for(
        ProviderConfig(provider="custom", model="primary"),
        dialect=dialect,
        client_factory=create,
    )
    child = original.bind(model="summary", max_tokens=64)
    assert created == []
    get_client = (
        child._client_or_create
        if dialect == ProviderDialect.OPENAI_CHAT
        else child._client
    )
    assert get_client() is get_client()
    assert len(created) == 1
    assert created[0].model == "summary"
    assert created[0].max_tokens == 64
