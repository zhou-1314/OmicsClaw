#!/usr/bin/env python3
"""Build the bundled Python backend runtime for one (platform, arch).

This is the CI-side implementation of the contract documented in
`docs/bundled-backend.md`. It:

  1. Downloads a `python-build-standalone` (PBS) `install_only`
     tarball for the requested target triple.
  2. Extracts it into `backend-runtime/python/` (the outer `python/`
     subdir is PBS's own layout, preserved intact so its relocatable
     `sys.prefix` resolution keeps working).
  3. Uses the extracted interpreter to upgrade pip and install a
     **whitelisted** set of desktop-app dependencies, then installs
     `omicsclaw` itself with `--no-deps` so that the scientific
     stack that lives in `omicsclaw`'s Tier 1 `dependencies` (scanpy,
     numpy, pandas, scipy, ...) is **not** pulled into the bundle.
     Users who need the analysis stack install it themselves on
     first use — see `docs/bundled-backend.md` "Analysis packages
     are user-installed".
  4. Runs the smoke test — `python -m omicsclaw.surfaces.desktop.server --help`
     must exit 0. This is what catches a missing dep in the
     whitelist before the bundled runtime ships.

**No third-party dependencies.** The script runs on whatever Python
3.9+ the CI runner has (GitHub Actions' `setup-python` covers that
out of the box). All HTTP, tarball, and subprocess work uses the
stdlib so we don't need to `pip install` anything before we can
start building.

Run `--help` for the CLI contract. The pure URL-builder function
is exposed as `pbs_tarball_url()` and is unit-testable in isolation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


# ---------------------------------------------------------------------------
# Pins
# ---------------------------------------------------------------------------

#: python-build-standalone release tag. Pinned so reproducible. Bumping
#: this means verifying all 6 target triples still exist and that
#: `python -m omicsclaw.surfaces.desktop.server --help` still succeeds — the usual
#: CI run does that automatically.
DEFAULT_PBS_RELEASE = "20260408"

#: CPython version to bundle. The release above ships this exact
#: patch version for every target triple.
DEFAULT_PYTHON_VERSION = "3.11.15"

#: Default git ref of OmicsClaw to install. Can be a branch, tag, or
#: commit SHA. Overridden via `--omicsclaw-ref` when CI pins to a
#: specific build.
DEFAULT_OMICSCLAW_REF = "main"

#: Upstream git repo the runtime pulls omicsclaw from.
OMICSCLAW_GIT_URL = "https://github.com/zhou-1314/OmicsClaw.git"

#: Directory name inside the project root that electron-builder
#: packages via `extraResources`. Kept in sync with
#: `BUNDLED_RUNTIME_DIRNAME` in `src/lib/python-runtime.ts`.
BACKEND_RUNTIME_DIR = "backend-runtime"

#: Subdir inside `backend-runtime/` where the PBS tarball extracts.
#: Kept in sync with `BUNDLED_PYTHON_SUBDIR` in `python-runtime.ts`.
PBS_SUBDIR = "python"

#: Whitelist of Python packages the desktop app actually needs to
#: boot the app server + start a Jupyter kernel. Deliberately a
#: **manual copy** of the `[desktop]` extras set in
#: `OmicsClaw/pyproject.toml` (plus `pydantic`, which lives in Tier 1
#: upstream because scanpy-adjacent code reuses it — the app server
#: imports it directly, so we need it here explicitly).
#:
#: **Why duplicated instead of reading the extras set upstream**:
#: The install flow is `pip install <whitelist>` + `pip install
#: --no-deps omicsclaw`, so we can't just ask pip to resolve
#: `omicsclaw[desktop]` — that would pull in `omicsclaw`'s Tier 1
#: `dependencies` list (scanpy, anndata, squidpy, numpy, pandas,
#: scipy, scikit-learn, matplotlib, seaborn, Pillow, umap-learn,
#: igraph, leidenalg, scikit-misc), which is exactly the 1.5 GiB
#: we're cutting. Hard-coding the list here is the simplest way
#: to tell pip "install these specific packages, nothing else."
#:
#: **Sync contract**: if `OmicsClaw/pyproject.toml`'s `[desktop]`
#: extras list changes, this list must be updated to match. The
#: smoke test (`python -m omicsclaw.surfaces.desktop.server --help`) catches
#: missing deps at build time — e.g. `python-multipart` was added
#: to both this list and the upstream extras after a previous
#: smoke-test-detected regression. Version specifiers match the
#: upstream `[desktop]` extras exactly, so the resolver sees the
#: same constraints.
DESKTOP_DEPS: list[str] = [
    "aiosqlite>=0.19.0",
    "sqlalchemy>=1.4.0",
    # greenlet is required by SQLAlchemy's async-to-sync bridge when
    # using aiosqlite. On some platforms pip does not pull it in as a
    # transitive dep, causing "No module named 'greenlet'" at runtime
    # when MemoryClient tries to connect.
    "greenlet>=3.0.0",
    "fastapi>=0.100.0",
    "uvicorn>=0.23.0",
    # python-multipart is required by FastAPI's File/Form parsing.
    # The /notebook/files/upload endpoint in
    # omicsclaw.surfaces.desktop.notebook.router uses File(...),
    # so without this the server module
    # fails at MODULE LOAD TIME — caught by the smoke test.
    "python-multipart>=0.0.9",
    "cryptography>=41.0.0",
    "requests>=2.31.0",
    "openai>=1.0.0",
    "python-dotenv>=1.0.0",
    "pyyaml>=6.0",
    "nbformat>=5.9",
    "jupyter_client>=8.0",
    "ipykernel>=6.0",
    # pydantic lives in `omicsclaw`'s Tier 1 upstream, not in
    # [desktop] extras. We need it here because we install
    # omicsclaw with --no-deps and `omicsclaw.surfaces.desktop.server` imports
    # pydantic at module load time (FastAPI request/response
    # models). Version constraint matches Tier 1.
    "pydantic>=2.0.0,<3.0",
    # socksio is pulled in by `httpx[socks]` — required whenever the
    # user's environment has HTTPS_PROXY / ALL_PROXY pointing at a
    # SOCKS URL (common in China for GFW workarounds). Without it,
    # httpx raises ImportError from _init_proxy_transport during
    # AsyncOpenAI client construction, which is the first thing
    # bot.core.init() does — crashing lifespan startup. Adding
    # socksio directly (instead of via httpx[socks]) keeps the pip
    # plan simple because httpx itself is already a transitive dep
    # of openai. Phase 2 of the smoke test sets a bogus SOCKS env
    # var to force httpx through this code path — any future
    # regression where we forget to ship socksio fails CI before
    # the artifact is uploaded.
    "socksio>=1.0.0",
    # rich is required by the MCP module at import time. Without it
    # the backend logs "No module named 'rich'" and skips MCP support.
    "rich>=13.0.0",
]


# ---------------------------------------------------------------------------
# Target matrix
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """One (platform, arch) combo the desktop app is shipped for."""

    platform: str  # "linux" | "macos" | "windows"
    arch: str  # "x64" | "arm64"
    pbs_triple: str  # e.g. "x86_64-unknown-linux-gnu"
    python_rel_binary: str  # path to python binary inside extracted PBS tarball


#: The six targets the Electron release matrix covers. Matches
#: `build.yml` (Linux x64/arm64 native, mac x64/arm64, win x64/arm64).
SUPPORTED_TARGETS: dict[tuple[str, str], Target] = {
    ("linux", "x64"): Target(
        platform="linux",
        arch="x64",
        pbs_triple="x86_64-unknown-linux-gnu",
        python_rel_binary="bin/python3",
    ),
    ("linux", "arm64"): Target(
        platform="linux",
        arch="arm64",
        pbs_triple="aarch64-unknown-linux-gnu",
        python_rel_binary="bin/python3",
    ),
    ("macos", "x64"): Target(
        platform="macos",
        arch="x64",
        pbs_triple="x86_64-apple-darwin",
        python_rel_binary="bin/python3",
    ),
    ("macos", "arm64"): Target(
        platform="macos",
        arch="arm64",
        pbs_triple="aarch64-apple-darwin",
        python_rel_binary="bin/python3",
    ),
    ("windows", "x64"): Target(
        platform="windows",
        arch="x64",
        pbs_triple="x86_64-pc-windows-msvc",
        python_rel_binary="python.exe",
    ),
    ("windows", "arm64"): Target(
        platform="windows",
        arch="arm64",
        pbs_triple="aarch64-pc-windows-msvc",
        python_rel_binary="python.exe",
    ),
}


# ---------------------------------------------------------------------------
# Pure helpers (unit-testable without IO)
# ---------------------------------------------------------------------------


def pbs_tarball_url(
    release: str,
    python_version: str,
    pbs_triple: str,
    variant: str = "install_only",
) -> str:
    """Build the download URL for a PBS tarball.

    Pure function, no IO — the unit tests exercise this directly.
    The URL pattern matches what the astral-sh/python-build-standalone
    release naming scheme has been using since the project moved
    to the astral org. `variant` is ``install_only`` or its ~30%-smaller
    ``install_only_stripped`` sibling (lever A1 / ADR-0002).
    """
    filename = (
        f"cpython-{python_version}+{release}-{pbs_triple}-{variant}.tar.gz"
    )
    return (
        "https://github.com/astral-sh/python-build-standalone/releases/"
        f"download/{release}/{filename}"
    )


def pbs_url_exists(url: str) -> bool:
    """HEAD-probe a PBS asset URL. True only if it clearly resolves.

    Used purely to PREFER the stripped variant (lever A1); any uncertainty
    — a 404, a CDN that dislikes HEAD on the redirect target, a transient
    network blip — returns False so resolve_pbs_url falls back to
    install_only instead of hard-failing the build. A genuine network
    outage still surfaces later, with a clear message, when
    download_tarball runs against the chosen URL.
    """
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req) as resp:
            return 200 <= resp.status < 400
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            log(f"  HEAD {exc.code} on stripped variant — treating as unavailable")
        return False
    except urllib.error.URLError as exc:
        log(f"  HEAD failed ({exc}) — treating stripped variant as unavailable")
        return False


def resolve_pbs_url(release: str, python_version: str, pbs_triple: str) -> str:
    """Prefer the stripped PBS variant, fall back to install_only per-triple.

    install_only_stripped (lever A1 / ADR-0002) is ~30% smaller but PBS
    does not publish it for every triple/release. A cheap HEAD check keeps
    the build robust rather than hard-failing when stripped is absent.
    """
    stripped = pbs_tarball_url(
        release, python_version, pbs_triple, variant="install_only_stripped"
    )
    if pbs_url_exists(stripped):
        log(f"PBS variant: install_only_stripped (available for {pbs_triple})")
        return stripped
    log(
        f"PBS variant: install_only_stripped not published for {pbs_triple}; "
        "falling back to install_only"
    )
    return pbs_tarball_url(
        release, python_version, pbs_triple, variant="install_only"
    )


def resolve_target(platform: str, arch: str) -> Target:
    """Look up a Target by (platform, arch), raising a clear error.

    Pure function, exposed for callers and unit tests.
    """
    key = (platform, arch)
    if key not in SUPPORTED_TARGETS:
        supported = ", ".join(f"{p}/{a}" for p, a in sorted(SUPPORTED_TARGETS))
        raise ValueError(
            f"Unsupported target {platform}/{arch}. Supported: {supported}"
        )
    return SUPPORTED_TARGETS[key]


# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------


def log(msg: str) -> None:
    """Uniform prefixed logging so CI runs stay skimmable."""
    print(f"[build-backend-runtime] {msg}", flush=True)


def download_tarball(url: str, dest: Path) -> None:
    """Stream a file from `url` to `dest`.

    Uses stdlib `urllib` — no `requests` dependency. Follows
    redirects (urllib does by default). Writes to a `.part` file
    and renames on success so a partial download is never mistaken
    for a finished one. Hashes the content after download so a
    mid-transfer corruption gets caught before we try to extract.
    """
    part = dest.with_suffix(dest.suffix + ".part")
    log(f"Downloading {url}")
    try:
        with urllib.request.urlopen(url) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            with open(part, "wb") as f:
                # 1 MiB chunks — keeps memory bounded and gives the
                # CI log a sense of progress without drowning it.
                read = 0
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    read += len(chunk)
                    if total:
                        pct = (read / total) * 100
                        if read % (10 * 1024 * 1024) < (1024 * 1024):
                            log(f"  {read // (1024 * 1024)} MiB ({pct:.0f}%)")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"PBS tarball download failed: HTTP {exc.code} for {url}. "
            "Verify the release tag, Python version, and target triple "
            "are all valid for astral-sh/python-build-standalone."
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"PBS tarball download failed: {exc}") from exc

    part.rename(dest)
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    log(f"Downloaded {dest.stat().st_size // (1024 * 1024)} MiB")
    log(f"  sha256={digest}")


def extract_tarball(archive: Path, dest_dir: Path) -> None:
    """Extract a PBS tarball into `dest_dir`.

    The PBS install_only tarball has a top-level `python/` directory
    that contains the full runtime. After extraction we expect that
    `dest_dir / python` exists with the PBS layout intact.
    """
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    log(f"Extracting into {dest_dir}")
    with tarfile.open(archive, "r:gz") as tar:
        # `filter='data'` is the 3.12+ safe-extraction default; falls
        # back to the pre-3.12 behaviour on older runners. PBS tarballs
        # don't contain anything weird so the filter is purely
        # defensive.
        try:
            tar.extractall(dest_dir, filter="data")
        except TypeError:
            tar.extractall(dest_dir)
    python_root = dest_dir / PBS_SUBDIR
    if not python_root.is_dir():
        raise RuntimeError(
            f"Extracted PBS tarball did not produce the expected "
            f"{python_root}/ directory — layout may have changed upstream."
        )


def install_omicsclaw(
    python_binary: Path,
    omicsclaw_ref: str,
    omicsclaw_local: str | None = None,
    dry_run: bool = False,
) -> None:
    """Run `pip install` inside the bundled runtime in two steps.

    **Step 1** installs the ``DESKTOP_DEPS`` whitelist — fastapi,
    uvicorn, jupyter_client, ipykernel, pydantic and the handful
    of other packages the app server + notebook kernel actually
    need to start up. pip's normal resolver runs here, so all
    transitive framework dependencies (starlette, anyio, tornado,
    pyzmq, charset-normalizer, …) come along automatically.

    **Step 2** installs ``omicsclaw`` itself **with ``--no-deps``**.
    That flag is what makes the bundle slim: without it, pip
    follows ``omicsclaw``'s Tier 1 ``dependencies`` list and drags
    in scanpy / anndata / squidpy / numpy / pandas / scipy /
    scikit-learn / matplotlib / seaborn / Pillow / umap-learn /
    igraph / leidenalg / scikit-misc — the ~1.5 GiB scientific
    stack that we explicitly do NOT want in the desktop bundle.
    Users install those themselves on first use (see the
    "Analysis packages are user-installed" section of
    `docs/bundled-backend.md`).

    pip runs under the BUNDLED python binary (not the build runner's
    python), so resulting site-packages land inside the PBS
    directory and ship with the tarball.
    """
    # Only upgrade pip + setuptools. The standalone `wheel` package is
    # NOT required for our install flow: every DESKTOP_DEPS entry is
    # distributed as a prebuilt wheel on PyPI, and pip 20+ ships a
    # vendored wheel module that handles `.whl` installs on its own.
    # Keeping `wheel` in the list made builds fail whenever the
    # configured index mirror happened to be missing the package (seen
    # on pypi.tuna.tsinghua.edu.cn in 2026-04), for zero actual benefit.
    upgrade_pip_cmd = [
        str(python_binary),
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--disable-pip-version-check",
        "pip",
        "setuptools",
    ]
    desktop_install_cmd = [
        str(python_binary),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-warn-script-location",
        # Don't write .pyc at install time (lever A2 / ADR-0002). Bytecode
        # is purged wholesale after the smoke test anyway; PYTHONPYCACHEPREFIX
        # sends first-launch recompilation to a writable per-user cache.
        "--no-compile",
        *DESKTOP_DEPS,
    ]
    if omicsclaw_local:
        omicsclaw_spec = str(Path(omicsclaw_local).resolve())
    else:
        omicsclaw_spec = f"omicsclaw @ git+{OMICSCLAW_GIT_URL}@{omicsclaw_ref}"
    omicsclaw_install_cmd = [
        str(python_binary),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-warn-script-location",
        "--no-deps",
        "--no-compile",  # lever A2 / ADR-0002 — see desktop_install_cmd
        omicsclaw_spec,
    ]

    log("Upgrading pip / setuptools inside the runtime")
    if dry_run:
        log(f"  DRY RUN: {' '.join(upgrade_pip_cmd)}")
    else:
        subprocess.run(upgrade_pip_cmd, check=True)

    log(f"Installing desktop whitelist ({len(DESKTOP_DEPS)} packages)")
    log(f"  packages: {', '.join(dep.split('>=')[0].split('<')[0] for dep in DESKTOP_DEPS)}")
    if dry_run:
        log(f"  DRY RUN: {' '.join(desktop_install_cmd)}")
    else:
        subprocess.run(desktop_install_cmd, check=True)

    log(f"Installing {omicsclaw_spec} with --no-deps")
    log("  (Tier 1 scientific stack is DELIBERATELY skipped — user installs on demand)")
    if dry_run:
        log(f"  DRY RUN: {' '.join(omicsclaw_install_cmd)}")
    else:
        subprocess.run(omicsclaw_install_cmd, check=True)


def run_smoke_test(python_binary: Path, dry_run: bool = False) -> None:
    """End-to-end smoke test for the bundled runtime.

    Runs three phases:

    1. **Module-import check** — ``python -m omicsclaw.surfaces.desktop.server --help``.
       A clean exit proves the PBS interpreter is relocatable, the
       install placed omicsclaw where Python can import it, and the
       CLI argparse tree is intact. This is fast (~1 sec) and catches
       top-level import errors.

    2. **Lifespan check** — start ``uvicorn`` with a real FastAPI app,
       wait for ``/health`` to return 200 OK, then SIGTERM the process.
       This catches the class of bugs where module import works but
       FastAPI's lifespan startup fails — e.g. missing sibling package
       (``ModuleNotFoundError: No module named 'bot'`` in v0.1.2), a
       filesystem path that resolves inside a read-only .app bundle,
       or any other startup-time dependency the module-level test can't
       see. This is slower (~5-10 sec) but mandatory — v0.1.2 shipped
       a broken installer exactly because Phase 1 passed but Phase 2
       didn't exist.

    3. **Kernel-exec check** — start a real Jupyter kernel via
       ``jupyter_client`` and execute ``1 + 1``. Phases 1-2 never spawn
       a notebook kernel, so the ``pyzmq`` / ``ipykernel`` native
       extensions ship untested by them. Those are exactly the C
       extensions ``strip --strip-unneeded`` (ADR-0002) can break, so
       this phase exercises a ZMQ-backed kernel round-trip before the
       artifact is allowed to ship — the same "don't repeat v0.1.2"
       reasoning that motivated phase 2, applied to the kernel path.

    Any phase failing is a hard build failure — the runtime won't
    boot for the end user either, so the build should not ship.
    """
    # Phase 1 — module import
    help_cmd = [str(python_binary), "-m", "omicsclaw.surfaces.desktop.server", "--help"]
    log(f"Smoke test (phase 1 — module import): {' '.join(help_cmd)}")
    if dry_run:
        log("  DRY RUN: skipping phase 1")
    else:
        subprocess.run(help_cmd, check=True)
        log("Phase 1 passed")

    # Phase 2 — lifespan startup + /health probe
    log("Smoke test (phase 2 — lifespan + /health)")
    if dry_run:
        log("  DRY RUN: skipping phase 2")
        return

    _run_lifespan_probe(python_binary)
    log("Phase 2 passed")

    # Phase 3 — Jupyter kernel start + cell execution
    log("Smoke test (phase 3 — kernel start + execute)")
    _run_kernel_probe(python_binary)
    log("Phase 3 passed")


def _run_lifespan_probe(python_binary: Path) -> None:
    """Start uvicorn in a subprocess, probe /health, then terminate.

    Uses an ephemeral port (0) to avoid collisions on shared CI runners.
    Polls /health with a 30 s total budget — generous for cold starts
    inside a packaged Python runtime on slow arch-emulated runners.

    Point ``OMICSCLAW_DIR`` at a fresh tempdir so the bot/core.py
    workspace constants land somewhere writable and isolated from the
    user's ``~/.omicsclaw`` fallback. This keeps CI runs hermetic.
    """
    import http.client
    import signal
    import socket
    import tempfile

    # Find a free port on 127.0.0.1 by opening a socket with port=0,
    # reading the kernel-assigned number, and closing. Race-condition-y
    # on heavily-loaded shared runners but cheap, and a port collision
    # just shows up as a probe failure — which we'd catch anyway.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    with tempfile.TemporaryDirectory(prefix="omicsclaw-smoke-") as tmpdir:
        env = dict(os.environ)
        env["OMICSCLAW_DIR"] = tmpdir
        # Prevent the dev server from picking up a leftover .env in CWD
        env.setdefault("OMICSCLAW_APP_HOST", "127.0.0.1")

        # Regression guard for v0.1.3 crash-on-launch for users with a
        # SOCKS proxy in HTTPS_PROXY: force httpx to take the SOCKS
        # transport path during AsyncOpenAI client construction (which
        # bot.core.init() runs at lifespan startup). httpx calls
        # _init_proxy_transport synchronously in the client's __init__
        # — if socksio isn't installed it raises ImportError there,
        # crashing the whole server before it can bind /health. By
        # planting a bogus SOCKS URL we force that code path without
        # needing an actual proxy running. The URL is 127.0.0.1:1
        # (port 1 is ~always unreachable) — httpx never tries to
        # CONNECT during client construction, so the bogus URL is
        # fine. NO_PROXY=127.0.0.1,localhost ensures the health probe
        # itself (which goes straight to 127.0.0.1:<ephemeral>) isn't
        # accidentally routed through the bogus proxy.
        env["HTTPS_PROXY"] = "socks5://127.0.0.1:1/"
        env["HTTP_PROXY"] = "socks5://127.0.0.1:1/"
        env["ALL_PROXY"] = "socks5://127.0.0.1:1/"
        env["NO_PROXY"] = "127.0.0.1,localhost"

        cmd = [
            str(python_binary),
            "-m",
            "omicsclaw.surfaces.desktop.server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ]
        log(f"  Starting server: {' '.join(cmd)}")
        log(f"  OMICSCLAW_DIR: {tmpdir}")

        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            # Poll /health up to 30 s. On cold-start slow runners
            # (macOS arm64 emulated, PBS interpreter first boot)
            # the server can take 5-10 s to reach lifespan completion.
            deadline = 30
            healthy = False
            health_body = ""
            import time as _time

            for attempt in range(deadline):
                if proc.poll() is not None:
                    output = proc.stdout.read() if proc.stdout else ""
                    raise RuntimeError(
                        "Server exited before /health became ready "
                        f"(exit code {proc.returncode}):\n{output}"
                    )
                try:
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                    conn.request("GET", "/health")
                    resp = conn.getresponse()
                    body = resp.read().decode("utf-8", errors="replace")
                    conn.close()
                    if resp.status == 200:
                        healthy = True
                        health_body = body
                        break
                    log(f"  /health attempt {attempt + 1}: HTTP {resp.status}")
                except (ConnectionRefusedError, OSError):
                    # Not listening yet — keep polling.
                    pass
                _time.sleep(1)

            if not healthy:
                output = ""
                if proc.stdout:
                    # Best-effort drain without blocking; if the server
                    # is alive it may not emit EOF, so read what's
                    # buffered via a non-blocking trick: terminate
                    # first, then read.
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
                    try:
                        output = proc.stdout.read() or ""
                    except Exception:
                        output = "(stdout drain failed)"
                raise RuntimeError(
                    f"/health never returned 200 within {deadline}s.\n"
                    f"Server output:\n{output}"
                )

            log(f"  /health OK ({len(health_body)} bytes)")
            _validate_desktop_chat_health_contract(health_body)
            log("  Desktop chat request/SSE/interrupt contract v1 compatible")
            log(f"  body: {health_body[:200]}")

        finally:
            if proc.poll() is None:
                # Graceful shutdown first — lets lifespan's cleanup hooks
                # run (memory store close, kernel shutdown, etc.). Fall
                # back to SIGKILL if uvicorn hangs longer than 5 s.
                if os.name == "nt":
                    proc.terminate()
                else:
                    proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


def _validate_desktop_chat_health_contract(health_body: str) -> None:
    """Fail a bundled pair outside the App's current non-authoritative V1 stage."""

    try:
        payload = json.loads(health_body)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Backend /health did not return valid JSON") from exc
    status = payload.get("status") if isinstance(payload, dict) else None
    if not isinstance(status, str) or not status.strip():
        raise RuntimeError("Backend /health is missing a non-empty status")
    contracts = payload.get("contracts")
    desktop_chat = contracts.get("desktop_chat") if isinstance(contracts, dict) else None
    if not isinstance(desktop_chat, dict):
        raise RuntimeError("Backend /health is missing contracts.desktop_chat")
    if (
        not isinstance(desktop_chat.get("request_schema_version"), int)
        or isinstance(desktop_chat["request_schema_version"], bool)
        or desktop_chat["request_schema_version"] != 1
        or not isinstance(desktop_chat.get("sse_schema_version"), int)
        or isinstance(desktop_chat["sse_schema_version"], bool)
        or desktop_chat["sse_schema_version"] != 1
        or not isinstance(desktop_chat.get("interrupt_schema_version"), int)
        or isinstance(desktop_chat["interrupt_schema_version"], bool)
        or desktop_chat["interrupt_schema_version"] != 1
        or desktop_chat.get("authoritative_ingress") is not False
        or desktop_chat.get("durable_ingress_idempotency") is not False
    ):
        raise RuntimeError(
            "Backend contracts.desktop_chat is not compatible with the App's "
            "current non-authoritative request/SSE/interrupt V1 stage"
        )


#: Driver run *inside* the bundled interpreter for smoke phase 3. Starts a
#: real Jupyter kernel (exercising pyzmq's ZMQ sockets + ipykernel) and
#: executes one cell. ipykernel does not register a "python3" kernelspec on
#: pip install, so it plants one in a throwaway prefix and points jupyter at
#: it — no global side effects on the build runner. Exits non-zero (failing
#: the build) on any wrong/missing result.
_KERNEL_PROBE_DRIVER = textwrap.dedent(
    '''
    import os, queue, sys, tempfile

    prefix = tempfile.mkdtemp(prefix="omicsclaw-kspec-")
    from ipykernel.kernelspec import install
    install(prefix=prefix)
    os.environ["JUPYTER_PATH"] = os.path.join(prefix, "share", "jupyter")

    from jupyter_client.manager import start_new_kernel
    km, kc = start_new_kernel(kernel_name="python3")
    try:
        msg_id = kc.execute("print(1 + 1)")
        result = None
        while True:
            try:
                msg = kc.get_iopub_msg(timeout=60)
            except queue.Empty:
                break
            if msg.get("parent_header", {}).get("msg_id") != msg_id:
                continue
            mtype, content = msg["msg_type"], msg["content"]
            if mtype == "stream" and content.get("name") == "stdout":
                result = content.get("text", "").strip()
            elif mtype == "execute_result":
                result = content.get("data", {}).get("text/plain", "").strip()
            elif mtype == "error":
                sys.exit("kernel raised: " + " / ".join(content.get("traceback", [])))
            elif mtype == "status" and content.get("execution_state") == "idle":
                break
        if result != "2":
            sys.exit("unexpected kernel output %r (wanted '2')" % (result,))
        print("kernel exec OK: 1 + 1 -> %s" % result)
    finally:
        kc.stop_channels()
        km.shutdown_kernel(now=True)
    '''
)


def _run_kernel_probe(python_binary: Path) -> None:
    """Start a Jupyter kernel and execute a trivial cell (smoke phase 3).

    Writes ``_KERNEL_PROBE_DRIVER`` to a temp file and runs it under the
    bundled interpreter. A non-zero exit (NoSuchKernel, a dead pyzmq
    extension after over-stripping, a kernel that never reaches idle, or
    the wrong cell result) is a hard build failure.
    """
    with tempfile.TemporaryDirectory(prefix="omicsclaw-kernel-smoke-") as tmpdir:
        driver_path = Path(tmpdir) / "kernel_probe.py"
        driver_path.write_text(_KERNEL_PROBE_DRIVER)
        log(f"  Running kernel probe: {python_binary} {driver_path}")
        subprocess.run([str(python_binary), str(driver_path)], check=True)


def directory_size_mib(path: Path) -> float:
    """Rough on-disk size of an extracted runtime, in MiB.

    Reported so CI logs surface regressions against the 1 GB budget
    before anyone downloads a bloated artifact.
    """
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += (Path(dirpath) / f).stat().st_size
            except OSError:
                continue
    return total / (1024 * 1024)


def remove_bytecode(runtime_root: Path) -> None:
    """Delete every .pyc / __pycache__ from the runtime (lever A2 / ADR-0002).

    MUST run AFTER the smoke test: importing modules during phases 1-3
    regenerates __pycache__ inside the (CI-writable) runtime tree, so an
    earlier purge would be silently undone. This is the authoritative
    removal that guarantees the shipped artifact carries no precompiled
    bytecode; at runtime PYTHONPYCACHEPREFIX (set by the Electron launcher)
    sends first-launch recompilation to a writable per-user cache instead
    of the read-only install dir.
    """
    removed_files = 0
    removed_bytes = 0
    for dirpath, _dirs, files in os.walk(runtime_root):
        for f in files:
            if not f.endswith(".pyc"):
                continue
            p = Path(dirpath) / f
            try:
                removed_bytes += p.stat().st_size
                p.unlink()
                removed_files += 1
            except OSError:
                continue
    # Second pass (bottom-up) drops the now-empty __pycache__ dirs.
    for dirpath, _dirs, _files in os.walk(runtime_root, topdown=False):
        if Path(dirpath).name == "__pycache__":
            try:
                Path(dirpath).rmdir()
            except OSError:
                pass
    log(
        f"bytecode: removed {removed_files} .pyc files "
        f"({removed_bytes / (1024 * 1024):.1f} MiB)"
    )


#: File suffixes that count as compiled native extensions — the territory
#: `strip --strip-unneeded` reclaims debug symbols from (ADR-0002 / A3).
_NATIVE_EXT_SUFFIXES = (".so", ".pyd", ".dll", ".dylib")


def report_runtime_breakdown(path: Path) -> None:
    """Log a per-component size breakdown of the built runtime.

    Phase 0 instrumentation for the size-slimming effort (ADR-0002):
    surfaces exactly how many bytes live in each category the slimming
    levers target, so before/after numbers are *measured*, not estimated.
    Runs on every build (CI or local), so the sizing-history table in
    `docs/bundled-backend.md` can be filled from real CI logs.

    Buckets, computed in a single filesystem walk:
      - bytecode (.pyc): what `pip --no-compile` keeps out of the
        shipped bundle (lever A2).
      - native ext (.so/.pyd/.dll/.dylib): the territory
        `strip --strip-unneeded` operates on (lever A3).
      - site-packages vs interpreter+rest: whitelist deps + omicsclaw
        against the PBS interpreter/stdlib. `install_only_stripped`
        (lever A1) shrinks the latter.

    Pure logging — never raises; a stat() failure just skips that file.
    """
    total = 0
    bytecode = 0
    native = 0
    site_packages = 0
    site_marker = f"{os.sep}site-packages{os.sep}"
    for dirpath, _dirs, files in os.walk(path):
        in_site = site_marker in f"{dirpath}{os.sep}"
        for f in files:
            try:
                size = (Path(dirpath) / f).stat().st_size
            except OSError:
                continue
            total += size
            if in_site:
                site_packages += size
            lower = f.lower()
            if lower.endswith(".pyc"):
                bytecode += size
            elif lower.endswith(_NATIVE_EXT_SUFFIXES):
                native += size

    def mib(n: int) -> float:
        return n / (1024 * 1024)

    def pct(n: int) -> float:
        return (100.0 * n / total) if total else 0.0

    log("Runtime size breakdown (Phase 0 instrumentation):")
    log(f"  total             {mib(total):7.1f} MiB")
    log(f"  site-packages     {mib(site_packages):7.1f} MiB ({pct(site_packages):.0f}%)")
    log(f"  interpreter+rest  {mib(total - site_packages):7.1f} MiB ({pct(total - site_packages):.0f}%)  <- install_only_stripped (A1)")
    log(f"  bytecode (.pyc)   {mib(bytecode):7.1f} MiB ({pct(bytecode):.0f}%)  <- pip --no-compile (A2)")
    log(f"  native ext (.so)  {mib(native):7.1f} MiB ({pct(native):.0f}%)  <- strip --strip-unneeded (A3)")


def strip_native_extensions(platform: str, runtime_root: Path) -> None:
    """Strip debug/local symbols from native extensions (lever A3 / ADR-0002).

    MUST run before the smoke test so phase 3 validates the *stripped*
    runtime — otherwise CI would green-light an unstripped tree while
    shipping the stripped one.

    Linux uses GNU binutils ``strip --strip-unneeded``. macOS uses
    ``strip -x``: its strip has no ``--strip-unneeded``, and ``-x`` keeps
    the globally-visible ``PyInit_*`` symbols ``dlopen`` needs to import
    the extension while dropping local symbols. Windows is skipped — PBS
    ships its ``.pyd`` / ``.dll`` already stripped and there's no reliable
    ``strip`` on a stock windows runner.

    Best-effort per file: a strip failure (already stripped, unreadable,
    or not a real object) is logged and skipped, never fatal. A genuinely
    broken extension surfaces in smoke phase 3.
    """
    if platform == "windows":
        log("strip: skipped on Windows (PBS .pyd/.dll already stripped)")
        return
    strip_tool = shutil.which("strip")
    if strip_tool is None:
        log("strip: `strip` not found on PATH — skipping (no symbol trim)")
        return
    if platform == "macos":
        strip_args = ["-x"]
        suffixes = (".so", ".dylib")
    else:  # linux
        strip_args = ["--strip-unneeded"]
        suffixes = (".so",)

    processed = 0
    before = 0
    after = 0
    for dirpath, _dirs, files in os.walk(runtime_root):
        for f in files:
            if not f.lower().endswith(suffixes):
                continue
            target = Path(dirpath) / f
            try:
                pre = target.stat().st_size
            except OSError:
                continue
            result = subprocess.run(
                [strip_tool, *strip_args, str(target)],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                log(f"  strip skipped {f}: {result.stderr.strip()[:120]}")
                continue
            try:
                post = target.stat().st_size
            except OSError:
                post = pre
            processed += 1
            before += pre
            after += post
    saved = (before - after) / (1024 * 1024)
    log(f"strip: processed {processed} native extensions, reclaimed {saved:.1f} MiB")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def build(
    *,
    platform: str,
    arch: str,
    omicsclaw_ref: str,
    omicsclaw_local: str | None = None,
    pbs_release: str,
    python_version: str,
    project_root: Path,
    dry_run: bool = False,
    skip_smoke_test: bool = False,
) -> None:
    """End-to-end build for one target. Raises on any failure."""
    target = resolve_target(platform, arch)

    runtime_root = project_root / BACKEND_RUNTIME_DIR
    python_binary = runtime_root / PBS_SUBDIR / target.python_rel_binary

    log(f"Target: {target.platform}/{target.arch} ({target.pbs_triple})")
    log(f"PBS release: {pbs_release}, CPython {python_version}")
    if omicsclaw_local:
        log(f"OmicsClaw local: {omicsclaw_local}")
    else:
        log(f"OmicsClaw ref: {omicsclaw_ref}")
    log(f"Runtime root: {runtime_root}")

    if dry_run:
        stripped_url = pbs_tarball_url(
            pbs_release, python_version, target.pbs_triple,
            variant="install_only_stripped",
        )
        log("DRY RUN — no network or filesystem work will happen")
        log(f"  Would download (stripped if published, else install_only): {stripped_url}")
        log(f"  Would extract to: {runtime_root}")
        log(f"  Would run: {python_binary} -m pip install --no-compile <{len(DESKTOP_DEPS)} desktop whitelist pkgs>")
        log(f"  Would run: {python_binary} -m pip install --no-deps --no-compile omicsclaw@{omicsclaw_ref}")
        log(f"  Would strip native extensions ({target.platform})")
        log(f"  Would smoke test phase 1: {python_binary} -m omicsclaw.surfaces.desktop.server --help")
        log(f"  Would smoke test phase 2: uvicorn lifespan + GET /health")
        log(f"  Would smoke test phase 3: jupyter_client kernel + execute 1+1")
        log(f"  Would remove all .pyc / __pycache__ from the runtime")
        return

    # Clean slate — remove anything except the README placeholder.
    # `electron-builder.yml` ships this whole directory, so a stale
    # build leaking through would bloat the installer.
    if runtime_root.exists():
        for entry in runtime_root.iterdir():
            if entry.name == "README.md":
                continue
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
    else:
        runtime_root.mkdir(parents=True)

    # Download + extract into a temp dir, then swing the extracted
    # tree over in one step. Keeps the output directory consistent
    # if we fail mid-download.
    url = resolve_pbs_url(pbs_release, python_version, target.pbs_triple)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        tarball = tmp_path / "pbs.tar.gz"
        download_tarball(url, tarball)
        extract_tarball(tarball, runtime_root)

    if not python_binary.is_file():
        raise RuntimeError(
            f"PBS extraction produced no binary at {python_binary}. "
            "The tarball layout may have changed — check "
            "docs/bundled-backend.md and update SUPPORTED_TARGETS."
        )

    # Installed packages land inside the PBS site-packages, which is
    # under `runtime_root / python / lib / ...` on Unix or
    # `runtime_root / python / Lib / ...` on Windows. Either way
    # they ship with the tarball going into electron-builder.
    if skip_smoke_test:
        # Cross-arch host: the PBS binary is for an arch this runner
        # can't execute, so both the pip install and the smoke test
        # would segfault. Ship the raw tarball only — the user who
        # integrates this into a release still needs to smoke-test on
        # real hardware before shipping, and `docs/bundled-backend.md`
        # notes this follow-up.
        log(
            "Skipping pip install + smoke test "
            "(--skip-smoke-test: cross-arch host)"
        )
        return
    install_omicsclaw(python_binary, omicsclaw_ref, omicsclaw_local=omicsclaw_local, dry_run=dry_run)
    # Strip BEFORE the smoke test so phase 3 validates the stripped runtime.
    strip_native_extensions(target.platform, runtime_root)
    run_smoke_test(python_binary, dry_run=dry_run)
    # Purge bytecode AFTER the smoke test — the smoke run regenerates .pyc.
    remove_bytecode(runtime_root)

    size_mib = directory_size_mib(runtime_root)
    log(f"Final runtime size: {size_mib:.0f} MiB")
    report_runtime_breakdown(runtime_root)
    # Soft warn threshold. The post-slim target is ~450-500 MiB
    # extracted (PBS interpreter ~300 + whitelist deps ~150-200).
    # A warn at 700 gives headroom for pip resolver bringing in
    # a few more transitive packages than we estimated, while
    # still flagging a regression if the scientific stack creeps
    # back in (that would land us at 1.5+ GiB again).
    if size_mib > 700:
        log("WARN: runtime is larger than 700 MiB — consider whether the")
        log("      DESKTOP_DEPS whitelist has grown, or whether omicsclaw")
        log("      Tier 1 deps leaked in via a missing --no-deps flag.")


def parse_argv(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build the bundled Python backend runtime for a given "
            "(platform, arch). See docs/bundled-backend.md for the "
            "contract this script implements."
        ),
    )
    parser.add_argument(
        "--platform",
        choices=["linux", "macos", "windows"],
        required=True,
        help="Target OS family",
    )
    parser.add_argument(
        "--arch",
        choices=["x64", "arm64"],
        required=True,
        help="Target CPU architecture",
    )
    parser.add_argument(
        "--omicsclaw-ref",
        default=DEFAULT_OMICSCLAW_REF,
        help=f"Git ref of OmicsClaw to install (default: {DEFAULT_OMICSCLAW_REF})",
    )
    parser.add_argument(
        "--omicsclaw-local",
        default=None,
        help="Install omicsclaw from a local path instead of git. "
             "Useful when GitHub is unreachable (e.g. behind GFW).",
    )
    parser.add_argument(
        "--pbs-release",
        default=DEFAULT_PBS_RELEASE,
        help=f"python-build-standalone release tag (default: {DEFAULT_PBS_RELEASE})",
    )
    parser.add_argument(
        "--python-version",
        default=DEFAULT_PYTHON_VERSION,
        help=f"CPython version inside the PBS release (default: {DEFAULT_PYTHON_VERSION})",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Root the runtime is written under (default: script's parent)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan without doing any network / filesystem work",
    )
    parser.add_argument(
        "--skip-smoke-test",
        action="store_true",
        help=(
            "Skip `pip install` + `python -m omicsclaw.surfaces.desktop.server --help`. "
            "Used by the CI cell where the target arch can't execute on "
            "the runner (e.g. Windows arm64 built on an x64 host); the "
            "raw PBS tarball is still uploaded for downstream packaging."
        ),
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_argv(argv)
    try:
        build(
            platform=args.platform,
            arch=args.arch,
            omicsclaw_ref=args.omicsclaw_ref,
            omicsclaw_local=args.omicsclaw_local,
            pbs_release=args.pbs_release,
            python_version=args.python_version,
            project_root=args.project_root,
            dry_run=args.dry_run,
            skip_smoke_test=args.skip_smoke_test,
        )
    except subprocess.CalledProcessError as exc:
        log(f"FAILED: subprocess returned {exc.returncode}: {exc.cmd}")
        return exc.returncode or 1
    except Exception as exc:  # noqa: BLE001 — top-level CLI safety net
        log(f"FAILED: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
