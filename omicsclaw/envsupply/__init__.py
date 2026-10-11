"""P5: local Python environment supply — the installer, not the installed.

The plan's "只带安装器，不带被安装物" half that lives in the backend: the
same command vocabulary C3 established for remote kernel environments
(:mod:`omicsclaw.remote.kernel`), pointed at this machine — probe one
interpreter, build the idempotent first-use install script with the
local uv ladder (shipped binary → installers dir → PATH, no curl) and
the CN-friendly mirror defaults, run it streaming, and disclose the
manual commands on failure.

Mechanism, not policy, throughout: nothing in this package decides who
may install what. The approval lives in the ``manage_environments``
tool's gate (chat face) and in the person who posted the
``kind="env_install"`` job (job face); the composition root
(:func:`omicsclaw.entry.env_supply.build_env_supply`) binds the
configured mirrors — never a URL taken from a model's arguments.
"""

from .micromamba import (
    MicromambaLocation,
    build_probe_argv as build_micromamba_probe_argv,
    locate_micromamba,
    parse_micromamba_version,
    probe_micromamba,
)
from .pins import (
    CORE_STACK_PACKAGES,
    DEFAULT_ENV_NAME,
    DEFAULT_UV_INDEX_URL,
    DEFAULT_UV_PYTHON_MIRROR,
    ENV_ROOT,
    KERNEL_BASE_PACKAGES,
    PYTHON_VERSION,
    UV_INSTALL_URL,
)
from .probe import (
    DEFAULT_PROBE_PACKAGES,
    PROBE_SENTINEL,
    EnvProbe,
    ProbeOutcome,
    build_probe_argv,
    default_probe_runner,
    interpreter_in_env,
    probe_interpreter,
)
from .script import (
    DEFAULT_ENV_DIR,
    ENV_INSTALL_TIMEOUT_S,
    ENV_STEP_MARKER_PREFIX,
    UV_NOT_FOUND_EXIT,
    EnvSupplyError,
    build_env_install_script,
    env_step_line,
    manual_env_commands,
    parse_env_step,
    run_install_script,
)

__all__ = [
    "CORE_STACK_PACKAGES",
    "DEFAULT_ENV_DIR",
    "DEFAULT_ENV_NAME",
    "DEFAULT_PROBE_PACKAGES",
    "DEFAULT_UV_INDEX_URL",
    "DEFAULT_UV_PYTHON_MIRROR",
    "ENV_INSTALL_TIMEOUT_S",
    "ENV_ROOT",
    "ENV_STEP_MARKER_PREFIX",
    "EnvProbe",
    "EnvSupplyError",
    "KERNEL_BASE_PACKAGES",
    "MicromambaLocation",
    "PROBE_SENTINEL",
    "PYTHON_VERSION",
    "UV_INSTALL_URL",
    "UV_NOT_FOUND_EXIT",
    "build_env_install_script",
    "build_micromamba_probe_argv",
    "build_probe_argv",
    "default_probe_runner",
    "env_step_line",
    "interpreter_in_env",
    "locate_micromamba",
    "parse_micromamba_version",
    "probe_micromamba",
    "manual_env_commands",
    "parse_env_step",
    "probe_interpreter",
    "run_install_script",
]
