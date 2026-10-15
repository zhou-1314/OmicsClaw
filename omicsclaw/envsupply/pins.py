"""The environment-supply pins, shared by the remote and local planes.

C3 (``omicsclaw/remote/kernel.py``) and P5 (this package) provision the
same kind of environment — one uv-managed Python with ipykernel and the
scanpy core stack — on two kinds of machine. The pins (which packages,
which interpreter version, where uv's installer lives) are one decision,
so they live in one module both import: forking them would let a remote
kernel and a local env drift apart the first time one side bumped a
version and forgot the other. The *differences* — the env directory, the
uv resolution ladder, the mirror defaults — stay in each plane, because
those are genuinely per-machine facts.

Local-only defaults live here too (the CN-friendly mirrors), because the
entry-layer binding that consumes them and the script builder that
injects them are in different modules of this one package; remote keeps
its own no-mirror-by-default posture, where a mirror is an explicit
per-deployment choice.
"""

from __future__ import annotations

KERNEL_BASE_PACKAGES: tuple[str, ...] = ("ipykernel", "jupyter_client")
"""What the wire needs before anything else: the kernel and its client.

Shared because a remote kernel and a local one speak the same jupyter
protocol; a stack that imports on one side and not the other is a pin
that drifted."""

CORE_STACK_PACKAGES: tuple[str, ...] = (
    "scanpy",
    "anndata",
    "pandas",
    "matplotlib",
    "leidenalg",
)
"""The plan's core layer (P5 §Tier 1), installed on first use on both
planes. The heavy tails (squidpy, R/IRkernel) are deliberate non-goals
of the core: the same package list is the extension point, and the
domain overlays ride the ``OMICSCLAW_SKILL_ENV`` seam instead."""

PYTHON_VERSION = "3.11"
"""The one interpreter version both planes pin. A range here would mean
two machines answering "3.11" with different minor behaviours; a pin
means the acceptance scenario transfers between them."""

UV_INSTALL_URL = "https://astral.sh/uv/install.sh"
"""The curl|sh installer, used by the **remote** ladder's last rung only.
The local ladder deliberately has no curl branch: the App ships the
uv binary (P5 §Tier 0), so a local machine without uv is a configuration
to report, not one to fix over the network unprompted."""

ENV_ROOT = "~/.omicsclaw/envs"
"""Where named local environments live. Under $HOME (not the workspace
and not the app bundle) on purpose: an environment is installed once and
reused for months, which is home-directory lifetime, and the CS-verified
rule that an app update must not re-download it (the bundle and the envs
stay strangers)."""

DEFAULT_ENV_NAME = "main"
"""The Tier-1 environment's name: the interpreter the App hands the
backend when the user configured nothing else."""

DEFAULT_UV_INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple"
"""The default PyPI mirror for local installs. The plan's biggest国情
correction over Claude-Science (which leans on official CDN直连): the
first-run experience on a CN network is the whole feature, so the mirror
is the default and going official is the override, never the reverse.
Configurable through ``OMICSCLAW_ENV_SUPPLY_INDEX_URL``."""

DEFAULT_UV_PYTHON_MIRROR = (
    "https://mirrors.tuna.tsinghua.edu.cn/github-release"
    "/astral-sh/python-build-standalone/releases/download"
)
"""The default mirror for python-build-standalone interpreters, the
``UV_PYTHON_INSTALL_MIRROR`` uv itself reads. Same reasoning as the
package mirror; overridable through
``OMICSCLAW_ENV_SUPPLY_PYTHON_MIRROR``."""


import os
from collections.abc import Mapping

CONTROL_CREDENTIAL_NAMES: frozenset[str] = frozenset({
    "OMICSCLAW_REMOTE_AUTH_TOKEN",
    "OMICSCLAW_API_KEY",
    "LLM_API_KEY",
    "ANTHROPIC_API_KEY",
    "DASHSCOPE_API_KEY",
    "DEEPSEEK_API_KEY",
    "GOOGLE_API_KEY",
    "MOONSHOT_API_KEY",
    "NVIDIA_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "SILICONFLOW_API_KEY",
    "VOLCENGINE_API_KEY",
    "ZHIPU_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "FEISHU_APP_SECRET",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "DISCORD_BOT_TOKEN",
    "DINGTALK_CLIENT_SECRET",
    "QQ_APP_SECRET",
    "SSH_AUTH_SOCK",
})


def without_control_credentials(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a copy of *source* (default ``os.environ``) without control credentials."""
    env = os.environ if source is None else source
    return {
        name: value
        for name, value in env.items()
        if name.upper() not in CONTROL_CREDENTIAL_NAMES
    }
