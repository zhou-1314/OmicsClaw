"""Environment policy shared by Backend-owned child-process launchers."""

from __future__ import annotations

from collections.abc import Mapping


_INTERNAL_CONTROL_CREDENTIALS = frozenset(
    {
        "OMICSCLAW_REMOTE_AUTH_TOKEN",
        "OMICSCLAW_SKILL_EVOLUTION_TOKEN",
        "OMICSCLAW_SKILL_EVOLUTION_TOKEN_FD",
    }
)


def is_internal_control_credential_name(name: object) -> bool:
    """Return whether ``name`` denotes Backend control authority material."""

    return str(name).upper() in _INTERNAL_CONTROL_CREDENTIALS


def scrub_internal_control_credentials(
    env: Mapping[str, str],
) -> dict[str, str]:
    """Return a copy without Backend control-plane credential material."""

    return {
        key: value
        for key, value in env.items()
        if not is_internal_control_credential_name(key)
    }


__all__ = [
    "is_internal_control_credential_name",
    "scrub_internal_control_credentials",
]
