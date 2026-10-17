"""Immutable framework state associated with one model request and its calls."""

from dataclasses import dataclass

from .message import Message, ToolCall


@dataclass(frozen=True, slots=True)
class ModelRequestContext:
    request_id: str
    state: tuple[tuple[str, str], ...] = ()

    def __post_init__(self):
        state = tuple(tuple(item) for item in self.state)
        if not isinstance(self.request_id, str) or not self.request_id:
            raise ValueError("request identity must be a nonempty string")
        if any(
            len(item) != 2 or any(not isinstance(value, str) for value in item)
            for item in state
        ):
            raise TypeError("request state must contain pairs of strings")
        if len({key for key, _ in state}) != len(state):
            raise ValueError("request state namespaces must be unique")
        object.__setattr__(self, "state", state)

    def value(self, key: str) -> str | None:
        return next((value for name, value in self.state if name == key), None)


@dataclass(frozen=True, slots=True)
class ToolInvocationContext:
    request: ModelRequestContext
    position: int
    call: ToolCall


@dataclass(frozen=True, slots=True)
class PreparedModelRequest:
    messages: tuple[Message, ...]
    context: ModelRequestContext
