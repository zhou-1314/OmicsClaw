"""``manage_environments`` — inspect and provision local environments.

P5's "环境即工具" half: the chat face of the supply plane. The model can
ask what environments exist, whether one can host the kernel, and
install (or finish installing) one — so "帮我装 squidpy" is a first-class
conversation instead of a README the person has to go run. The jobs
face of the same mechanism is ``POST /jobs {kind: "env_install"}``;
both bind the same entry-layer seam.

**Leaf-layer rule, like ``python``'s and ``save_artifact``'s**: this
module imports ``omicsclaw.schema`` and the tool layer's own modules,
nothing heavier. The supply itself is an :class:`EnvironmentSupply`
Protocol the composition root binds
(:func:`omicsclaw.entry.env_supply.build_env_supply` over the
``envsupply`` package); a tool constructed without one refuses at
execution time, because no argument a model can send could supply a
process.

**Approval follows the ``install_skill_deps`` category**: installing
packages is ``HIGH`` risk, ``ASK`` mode, and only the ``install``
action asks — ``list`` and ``probe`` read state and never trigger a
card. The reason names the environment, the packages, and the mirror
*domain* the configured URL will serve, without pasting the URL itself
onto the card (the mirror is configuration, not something the model
chose — see the whitelist note below).

**The mirror whitelist is structural.** The model names *packages*,
never URLs: ``packages`` entries must be bare distribution names
(letters, digits, dots, underscores, dashes — no specifiers, no
``pip install`` URL forms, no shell metacharacters), and the seam
receives only those names. Which index serves them is decided entirely
by the composition root's configuration; there is no argument path
from the model to a URL.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from omicsclaw.schema import ToolDefinition

from ..base import ApprovalMode, RiskLevel, ToolPolicy
from ..context import pause_tool_timeout, require_approval
from ..function_tool import ToolArgumentError, decode_arguments, validate_arguments

__all__ = [
    "MANAGE_ENV_SCHEMA",
    "TOOL_NAME",
    "EnvironmentSupply",
    "ManageEnvironmentsTool",
]

TOOL_NAME = "manage_environments"

_DESCRIPTION = (
    "Inspect and set up this machine's OmicsClaw Python environments"
    " (uv-managed, under ~/.omicsclaw/envs). `action`: 'list' names the"
    " environments that exist; 'probe' reports whether one (or an"
    " explicit `interpreter` path) can import ipykernel and which core"
    " packages it is missing; 'install' creates or completes an"
    " environment — idempotent, so re-running after a failure heals it —"
    " and may take several minutes with the person's approval. Packages"
    " come from the configured mirror; name bare distribution names"
    " only (e.g. 'squidpy'), never URLs or version specifiers. When an"
    " ImportError names a package this stack lacks, install it here"
    " rather than working around it."
)

MANAGE_ENV_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["list", "probe", "install"],
            "description": (
                "'list' the environments that exist; 'probe' one for"
                " readiness and gaps; 'install' (or finish) one."
            ),
        },
        "env": {
            "type": "string",
            "description": (
                "Named environment under ~/.omicsclaw/envs; 'main' (the"
                " default) is the analysis environment the App"
                " bootstraps. One word of letters, digits, dots,"
                " underscores, dashes."
            ),
        },
        "interpreter": {
            "type": "string",
            "description": (
                "probe only: an absolute interpreter path to inspect"
                " instead of a named environment (e.g. a conda env's"
                " python). Never installed into."
            ),
        },
        "packages": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "install only: extra distribution names to add on top of"
                " the core stack (ipykernel, scanpy, anndata, pandas,"
                " matplotlib, leidenalg). Bare names; no URLs, no"
                " version specifiers."
            ),
        },
    },
    "required": ["action"],
    "additionalProperties": False,
}

_POLICY = ToolPolicy(
    risk_level=RiskLevel.HIGH,
    approval_mode=ApprovalMode.ASK,
    prompts_for_itself=True,
    read_only=False,
    concurrency_safe=False,
    writes_workspace=False,
    touches_network=True,
    tags=frozenset({"environment", "network", "mutation"}),
)
"""The ``install_skill_deps`` claim, verbatim category: an installation
changes the machine's Python surface and reaches the network, so the
worst action sets the bar and only that action asks — the read actions
above never reach the irreversible part and never trigger a card."""

_ENV_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_PACKAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_INTERPRETER_PATH = re.compile(r"^(?:/|[A-Za-z]:[\\/])[^<>:|?*\x00-\x1f]*$")
"""A local filesystem path, not a URL and not shell text: absolute, and
free of the characters Windows reserves and sh treats specially. The
seam runs interpreters as an argv element (never through a shell), so
this is belt-and-braces on top of a non-shell splice."""


@runtime_checkable
class EnvironmentSupply(Protocol):
    """Where the environments actually live. Injected, never imported.

    The tools layer is a leaf; the supply mechanism is infrastructure.
    This three-method seam is the whole contract: enumerate, inspect,
    provision. ``install_environment`` streams installer output lines to
    *on_output* from a worker thread as they arrive, and answers a
    summary mapping the caller renders.
    """

    async def list_environments(self) -> Sequence[Mapping[str, Any]]: ...

    async def probe_environment(
        self, *, env: str = "main", interpreter: str | None = None
    ) -> Mapping[str, Any]: ...

    async def install_environment(
        self,
        *,
        env: str = "main",
        packages: Sequence[str] = (),
        on_output: Callable[[str], None] | None = None,
    ) -> Mapping[str, Any]: ...


class ManageEnvironmentsTool:
    """One action over the local environment supply.

    Satisfies :class:`~omicsclaw.tools.base.Tool` structurally, exactly
    as ``python`` and ``save_artifact`` do. ``supply`` is the injected
    seam; omitting it constructs a tool that refuses at execution time.
    """

    policy = _POLICY

    def __init__(self, *, supply: EnvironmentSupply | None = None) -> None:
        self._supply = supply
        self._definition = ToolDefinition(
            name=TOOL_NAME,
            description=_DESCRIPTION,
            input_schema=json.loads(json.dumps(MANAGE_ENV_SCHEMA)),
        )

    @property
    def name(self) -> str:
        return TOOL_NAME

    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(self, arguments: str) -> str:
        decoded = decode_arguments(arguments)
        issues = validate_arguments(decoded, self._definition.input_schema)
        if issues:
            listed = "\n".join("  - " + issue for issue in issues)
            raise ToolArgumentError(
                "the arguments do not match this tool's schema:\n"
                f"{listed}\nRe-send the call with all of these corrected."
            )
        action = str(decoded["action"])
        env = self._env_argument(decoded)
        interpreter = self._interpreter_argument(decoded, action)
        packages = self._packages_argument(decoded, action)

        if self._supply is None:
            raise RuntimeError(
                "manage_environments has no environment supply bound;"
                " the surface assembling this deployment must provide one"
            )

        if action == "list":
            found = await self._supply.list_environments()
            return _render_listing(found)
        if action == "probe":
            report = await self._supply.probe_environment(
                env=env, interpreter=interpreter
            )
            return _render_probe(report)
        await require_approval(
            self.name,
            arguments,
            policy=self.policy,
            reason=(
                f"set up the '{env}' OmicsClaw environment under"
                " ~/.omicsclaw/envs: ipykernel and the scanpy core stack"
                + (
                    " plus " + ", ".join(packages)
                    if packages
                    else ""
                )
                + ", installed with uv from the configured mirror (this"
                " machine's package index; not chosen by the model)."
                " Idempotent: safe to re-run after a failure."
            ),
        )
        # A first install runs minutes; the person's approval is one
        # decision, not a deadline the engine should charge against the
        # tool timeout — the ``install_skill_deps`` arithmetic.
        with pause_tool_timeout():
            report = await self._supply.install_environment(
                env=env, packages=packages
            )
        return _render_install(report)

    # ---- argument shaping ----

    def _env_argument(self, decoded: Mapping[str, Any]) -> str:
        """The named environment, ``main`` by default.

        The name becomes a directory under the env root, so it is held
        to the same closed alphabet the remote session slugs use — a
        name that could escape the root (``..``, slashes, spaces) is a
        model-facing error, not a surprise at filesystem time."""
        raw = decoded.get("env", "main")
        if raw is None:
            raw = "main"
        if not isinstance(raw, str) or not _ENV_NAME.fullmatch(raw):
            raise ToolArgumentError(
                "input.env must be one word of letters, digits, dots,"
                " underscores and dashes (no slashes, no '..'); got"
                f" {raw!r}"
            )
        return raw

    def _interpreter_argument(
        self, decoded: Mapping[str, Any], action: str
    ) -> str | None:
        """The probe-only interpreter override: inspected, never touched."""
        raw = decoded.get("interpreter")
        if raw is None:
            return None
        if action != "probe":
            raise ToolArgumentError(
                "input.interpreter is only meaningful with"
                f" action='probe'; action={action!r} addresses a named"
                " environment"
            )
        if not isinstance(raw, str) or not _INTERPRETER_PATH.fullmatch(raw):
            raise ToolArgumentError(
                "input.interpreter must be an absolute interpreter path"
                f" on this machine (not a URL); got {raw!r}"
            )
        return raw

    def _packages_argument(
        self, decoded: Mapping[str, Any], action: str
    ) -> tuple[str, ...]:
        """The install-only extras: bare distribution names, deduplicated.

        The mirror-whitelist boundary: this is the only channel the
        model has into the install, and it carries names that match a
        closed alphabet — no URL, no specifier, no flag can pass, so
        the seam cannot be pointed anywhere but the configured index."""
        raw = decoded.get("packages")
        if raw is None:
            return ()
        if action != "install":
            raise ToolArgumentError(
                "input.packages is only meaningful with"
                f" action='install'; action={action!r}"
            )
        if not isinstance(raw, list) or not all(
            isinstance(item, str) for item in raw
        ):
            raise ToolArgumentError(
                "input.packages must be an array of distribution names"
                " (strings)"
            )
        cleaned: list[str] = []
        for item in raw:
            name = item.strip()
            if not name:
                continue
            if not _PACKAGE_NAME.fullmatch(name):
                raise ToolArgumentError(
                    f"input.packages entries must be bare distribution"
                    f" names (letters, digits, dots, underscores,"
                    f" dashes) — no URLs, no version specifiers; got"
                    f" {item!r}"
                )
            if name not in cleaned:
                cleaned.append(name)
        return tuple(cleaned)


# ---- rendering -----------------------------------------------------------------


def _render_listing(found: Sequence[Mapping[str, Any]]) -> str:
    """The list answer: one line per environment, newest knowledge first."""
    if not found:
        return (
            "no OmicsClaw environments under ~/.omicsclaw/envs yet;"
            " action='install' creates the 'main' one"
        )
    lines = ["environments under ~/.omicsclaw/envs:"]
    for entry in found:
        name = str(entry.get("name", "?"))
        interpreter = str(entry.get("interpreter", ""))
        state = "ready" if entry.get("ready") else "present"
        lines.append(f"- {name} ({state}): {interpreter}")
    lines.append(
        "action='probe' inspects one for missing packages;"
        " action='install' creates or completes one"
    )
    return "\n".join(lines)


def _render_probe(report: Mapping[str, Any]) -> str:
    """The probe answer: what is there, what is missing, what to do."""
    interpreter = str(report.get("interpreter", "?"))
    if not report.get("exists"):
        return (
            f"{interpreter} does not exist here; action='install'"
            " builds the named environment (or pass another interpreter"
            " to probe)"
        )
    missing = [str(name) for name in report.get("missing", [])]
    if report.get("ready") and not missing:
        return (
            f"{interpreter} is ready: imports ipykernel and the core"
            " stack; nothing to install"
        )
    head = f"{interpreter} exists"
    if not report.get("ready"):
        head += " but cannot import ipykernel (not kernel-ready)"
    if missing:
        head += "; missing: " + ", ".join(missing)
    return (
        head
        + "; action='install' completes the named environment"
        " (idempotent, never touches an interpreter you pinned here)"
    )


def _render_install(report: Mapping[str, Any]) -> str:
    """The install answer: where the interpreter is, what landed."""
    interpreter = str(report.get("interpreter", ""))
    installed = tuple(report.get("packages", ()))
    return "\n".join(
        [
            f"environment ready: {interpreter}",
            "installed: "
            + (", ".join(installed) if installed else "(already complete)"),
        ]
    )
