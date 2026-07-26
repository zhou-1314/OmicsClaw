# omicsclaw

> Multi-omics analysis agent — spatial, single-cell, genomics, proteomics, metabolomics, bulk RNA-seq

## Install

```sh
npm install -g omicsclaw
```

Then:

```sh
omicsclaw --version
omicsclaw list          # every available skill, by domain
omicsclaw --help
```

`oc` is a shorter alias for the same command.

Node.js 18 or newer is required to run the launcher. You do **not** need Python
installed — a self-contained interpreter ships with the platform package.

## How it works

The `omicsclaw` package itself contains no runtime. It declares one
`@omicsclaw/runtime-<platform>-<arch>` package per supported host in
`optionalDependencies`, each carrying `os` and `cpu` fields. npm refuses to
install the ones that do not match your machine, and because they are optional
that refusal is a silent skip — so exactly one runtime lands on disk. This is
the same layout esbuild, swc, and biome use.

Each runtime package holds a relocatable CPython build
([python-build-standalone](https://github.com/astral-sh/python-build-standalone))
with the `omicsclaw` Python package and its server dependencies already
installed. `bin/omicsclaw.mjs` is a thin shell that hands your arguments to that
interpreter and forwards the exit status.

### Supported platforms

| Platform | Architecture | Prebuilt runtime |
| --- | --- | --- |
| Linux | x64 | yes |
| Linux | arm64 | yes |
| macOS | Apple Silicon | yes |
| Windows | x64 | yes |
| macOS | Intel | no — see below |
| Windows | arm64 | no — see below |

Two hosts have no prebuilt runtime. `llvmlite` (required by `numba`, required by
`scanpy`) no longer publishes macOS x86_64 wheels, and there is no native
Windows arm64 CI runner nor any Windows equivalent of Rosetta. On those two,
install from source instead:

```sh
git clone https://github.com/zhou-1314/OmicsClaw.git
cd OmicsClaw && ./0_setup_env.sh
```

## Analysis packages are installed on demand

The bundled runtime carries what the agent and its server need to start. It does
**not** carry the scientific stack — `scanpy`, `numpy`, `torch` and friends run
to roughly 1.5 GiB, which does not belong in an npm tarball. Skills that need
them will tell you what to install, using the same interpreter's `pip`.

## Migrating from a pip install

OmicsClaw has long been installable with `pip`, `pipx`, and `uv tool`, all of
which create the same four commands: `omicsclaw`, `oc`, `omicsclaw-chat`,
`oc-chat`. Whichever copy sits earlier on your `PATH` wins, so a fresh npm
install could otherwise be invisible.

The install hook handles this. For each name it finds a previously
pip-installed shim, it renames the first one to `<name>-legacy` — so the old CLI
stays reachable — and removes any duplicates. It tells you exactly what it did.

It is deliberately conservative:

- It only acts on a **global** install. `npx`, local dependencies, and workspace
  bootstraps do nothing.
- It only touches files it can positively identify as a Python OmicsClaw entry
  point. Anything else keeps the name. If you have the OpenShift client
  installed as `oc`, it is reported and left alone — use `omicsclaw` instead.
- Before changing anything it confirms that our command would actually win
  `PATH` resolution afterwards. If it would not, it explains why and touches
  nothing.
- It never fails the install.

To undo the takeover, remove the npm package and rename `<name>-legacy` back.

## Desktop app

[OmicsClaw-App](https://github.com/zhou-1314/OmicsClaw-App) picks this runtime up
automatically. The install hook records the interpreter in
`~/.omicsclaw/runtime.json`, and the app prefers it over the runtime bundled in
its own installer — so upgrading the CLI upgrades the app's backend too.

## Links

- Source: https://github.com/zhou-1314/OmicsClaw
- Issues: https://github.com/zhou-1314/OmicsClaw/issues

## License

Apache-2.0
