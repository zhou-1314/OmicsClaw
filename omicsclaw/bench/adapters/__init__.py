"""The adapter interface: how the harness starts an agent and reads it back.

An adapter knows one agent. It turns a staged run into a command line and
an environment, and after the process has exited it reads the files the
process left in the run's ``meta`` directory. Starting, timing and stopping
the process are the harness's job, the same for every adapter.

A manifest names an adapter by a built-in name (``omicsclaw``) or as
``module:attribute``, where the attribute is called with the arm to build
the adapter.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Protocol

from ..layout import RunPaths
from ..manifest import Arm, Budget, ManifestError, RunSpec
from ..outcome import Evidence, ProcessExit

__all__ = ["Adapter", "Launch", "build_adapter", "load_attribute"]


@dataclass(frozen=True)
class Launch:
    """How to start one run.

    :param argv: The command line.
    :param env: The complete environment of the process.
    :param cwd: Its working directory.
    :param harness_env: The variables in *env* the harness set itself,
        recorded with the run. Values the process merely inherited are not
        written anywhere.
    """

    argv: tuple[str, ...]
    env: Mapping[str, str]
    cwd: Path
    harness_env: Mapping[str, str]


class Adapter(Protocol):
    """One agent, as the harness drives it."""

    reserved_env: frozenset[str]
    """Variables the adapter sets itself; an arm may not set them."""

    workspace_state: tuple[str, ...]
    """Workspace-relative paths the agent keeps its own state under. The
    access audit does not read them."""

    def launch(
        self,
        run: RunSpec,
        paths: RunPaths,
        budget: Budget,
        base_env: Mapping[str, str],
    ) -> Launch:
        """The command and environment that run *run* in ``paths.workspace``.

        :param base_env: The environment the harness itself was given.
        """
        ...

    def collect(self, run: RunSpec, paths: RunPaths, exit: ProcessExit) -> Evidence:
        """Read back what the finished process left in ``paths.meta``.

        Must not raise on missing or malformed files: a run that left
        nothing readable is reported as evidence with empty fields.
        """
        ...


def load_attribute(reference: str, what: str) -> Any:
    """Import ``module:attribute``.

    :raises ManifestError: *reference* has no ``:``, or the module or the
        attribute does not exist.
    """
    module_name, separator, attribute = reference.partition(":")
    if not separator or not module_name or not attribute:
        raise ManifestError(f"{what} {reference!r} is not of the form module:attribute")
    try:
        return getattr(import_module(module_name), attribute)
    except (ImportError, AttributeError) as exc:
        raise ManifestError(f"cannot load {what} {reference!r}: {exc}") from exc


def build_adapter(arm: Arm) -> Adapter:
    """The adapter *arm* names, built for that arm.

    :raises ManifestError: The name is unknown, or the arm sets a variable
        the adapter reserves.
    """
    if arm.adapter == "omicsclaw":
        from .omicsclaw import OmicsClawAdapter

        factory: Any = OmicsClawAdapter
    elif ":" in arm.adapter:
        factory = load_attribute(arm.adapter, "adapter")
    else:
        raise ManifestError(
            f"arm {arm.id!r}: unknown adapter {arm.adapter!r} (built in: "
            "omicsclaw; or module:attribute)"
        )
    adapter: Adapter = factory(arm)
    reserved = sorted(set(arm.env) & set(adapter.reserved_env))
    if reserved:
        raise ManifestError(
            f"arm {arm.id!r}: env may not set {', '.join(reserved)}; the "
            f"{arm.adapter} adapter sets them"
        )
    return adapter
