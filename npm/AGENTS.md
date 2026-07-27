# npm distribution — agent guide

This subtree publishes OmicsClaw to npm so `npm install -g omicsclaw` yields a
working CLI with no Python prerequisite.

## Layout

- `omicsclaw/` — the wrapper package published as `omicsclaw`. Carries no
  runtime; ~20 KB. `bin/omicsclaw.mjs` locates the platform runtime and hands
  arguments to its interpreter.
- `build-runtime-package.mjs` — wraps a prebuilt runtime into a publishable
  `@omicsclaw/runtime-<target>` package. Does **not** build the runtime.
- `dist/` — build output, gitignored.

## The distribution model

The standard npm "thin wrapper + platform packages" pattern (esbuild, swc,
biome): the wrapper lists one `@omicsclaw/runtime-<target>` per host in
`optionalDependencies`, each declaring `os` / `cpu`. npm refuses to install
non-matching ones, and because they are *optional* that refusal is a silent
skip. Exactly one runtime lands on disk.

The runtime content itself comes from
`OmicsClaw-App/scripts/build-backend-runtime.py` — python-build-standalone plus
the desktop dependency whitelist plus `pip install --no-deps omicsclaw`. Do not
reimplement any of that here; there must be exactly one definition of what a
runtime contains.

## Hard constraints

**`npm pack` silently drops every symlink.** A PBS runtime has ~1048 of them.
The published tarball would otherwise arrive with `python/bin/python3.11` but no
`python/bin/python3`, and no `python/lib/libpython3.11.so`. Two independent
mitigations, both required:

1. `build-runtime-package.mjs` records the **dereferenced** interpreter path into
   `omicsclawRuntime.pythonRelPath` in the runtime package's manifest, and
   `resolveRuntime` prefers it. This is what makes the package work even under
   `--ignore-scripts`.
2. Every link is recorded into `runtime-symlinks.json` and recreated by
   `lib/restore-symlinks.mjs` during postinstall.

Never make resolution depend on a symlink existing.

**The postinstall entry point must be CommonJS.** `scripts/postinstall.cjs` is a
version-gated shim around `postinstall-main.mjs`. An ESM entry point is a *parse*
error on old Node, which kills the script before any `try` / `catch` and fails
the whole install with `ELIFECYCLE` — observed on npm 6.14.4 / Node 10.19.0. The
`engines` field does not prevent this; npm only warns. The dynamic `import()` is
built through `new Function` so the expression is never parsed by a runtime that
would reject it.

**The postinstall must never fail an install.** Every path exits 0. A migration
that could not run is a message, not a broken install.

**The migration must never touch what it cannot positively identify.** A shim
qualifies only when it realpath-resolves outside our package AND its content
carries a Python entry-point marker AND it is not a Node shim. Note that
`bin/omicsclaw.mjs` embeds the string
`from omicsclaw.surfaces.cli.launcher import main` in its Python bootstrap, so it
matches the marker — hence the `node_modules` counter-check in
`migrate.mjs`. `oc` is also the OpenShift client's command name; an unrecognised
`oc` is reported and left alone.

**The package must stay dependency-free.** It runs inside `npm install`, so
having dependencies of our own would be a bootstrapping problem. Tests use
stdlib `node:test` only.

## Sync contract — five places, all manual

Adding or removing a target means touching **all** of these:

1. `NPM_TARGETS` in `build-runtime-package.mjs`
2. `SUPPORTED_TARGETS` in `omicsclaw/lib/resolve-runtime.mjs`
3. `optionalDependencies` in `omicsclaw/package.json`
4. `SUPPORTED_TARGETS` in `OmicsClaw-App/scripts/build-backend-runtime.py`
5. the CI matrix in `OmicsClaw-App/.github/workflows/backend-runtime.yml`

`omicsclaw/test/units.test.mjs` asserts the current list, so (2) will fail loudly
if it drifts — nothing guards the other four.

Only four of six plausible targets ship a runtime. `darwin-x64` is out because
llvmlite stopped publishing macOS x86_64 wheels; `win32-arm64` is out because
there is no native runner and no Rosetta equivalent. Both are marked
`skip-runtime: true` in the App's CI. Never package a runtime directory
containing a `SKIPPED` marker — the build script refuses, because shipping an
interpreter without `omicsclaw` in it is worse than shipping nothing.

## The descriptor contract

Postinstall writes `~/.omicsclaw/runtime.json`; OmicsClaw-App reads it in
`src/lib/npm-runtime-descriptor.ts`. Both sides validate structurally and the
schema version must move together.

npm has **no working uninstall hook for global packages** — `preuninstall` does
not fire for `npm uninstall -g` — so this file outlives the runtime it
describes. Every reader must verify `pythonPath` still exists before trusting the
entry. That check is load-bearing, not defensive garnish.

## Releasing

`.github/workflows/npm-release.yml`, manual dispatch only. Four matrix cells
build a runtime and pack one platform package each; a separate `publish` job
(opt-in via the `publish` input, gated on the `npm-publish` environment) pushes
them to npm.

Two prerequisites, both one-time:

- **`secrets.APP_REPO_TOKEN`** — a PAT with `contents: read` on the *private*
  `OmicsClaw-App` repo. `build-backend-runtime.py` lives there and is not
  duplicated here, so the default `GITHUB_TOKEN` cannot reach it. The workflow
  fails fast with an explicit message when this is absent.
- **`secrets.NPM_TOKEN`** — an automation token for the `omicsclaw` org. npm's
  OIDC trusted publishing would remove the need for this; worth switching to.

Ordering is load-bearing: platform packages publish **before** the wrapper. The
wrapper pins exact versions in `optionalDependencies`, and an optional
dependency that fails to resolve fails *silently* — publishing the wrapper first
leaves a window where `npm i -g omicsclaw` installs something that cannot run.
The publish job asserts all four packages exist and match the wrapper version
before pushing anything.

**Never pipe the runtime builder through another command.** A shell pipeline
reports the last command's exit status, so `build-backend-runtime.py ... | tail`
returns 0 even when the smoke test raised — which is exactly how a broken
runtime gets shipped. (Observed: a phase-2 lifespan-probe failure hidden behind
`| tail -60`.)

The builder's phase-2 probe takes an exclusive lock on the control database
under the state dir, so the workflow points `XDG_STATE_HOME` at a scratch path.
Without that, any other OmicsClaw process on the machine makes the probe fail —
the normal outcome on a developer box or a self-hosted runner.

**Follow-up worth doing:** move `build-backend-runtime.py` into this repository.
It builds an environment defined by *this* project's dependencies, its
`DESKTOP_DEPS` whitelist is a hand-maintained copy of this repo's `[desktop]`
extras, and living in the app repo is what forces both that sync contract and
the cross-repo token. Moving it deletes all three.

## Verifying a change

```sh
node --test npm/omicsclaw/test/            # pure units
node npm/build-runtime-package.mjs --help  # CLI contract

# Full chain against a real runtime, sandboxed so it cannot touch your PATH:
python ../OmicsClaw-App/scripts/build-backend-runtime.py \
    --platform linux --arch x64 --omicsclaw-local . --project-root /tmp/rt
node npm/build-runtime-package.mjs --target linux-x64 \
    --runtime-dir /tmp/rt/backend-runtime --out /tmp/npmdist
```

Install tests must use a scratch `--prefix`, a scratch `HOME`, and a controlled
`PATH`, or the migrator will inspect (and potentially rename) the real shims on
the developer's machine. Note that `npm install -g <directory>` does not resolve
`file:` optional dependencies — pack to a tarball first, or the runtime silently
will not be installed.
