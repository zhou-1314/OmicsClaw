#!/usr/bin/env node
/**
 * Wrap a prebuilt Python runtime into a publishable `@omicsclaw/runtime-<target>`
 * npm package.
 *
 * This script deliberately does NOT build the runtime. That job belongs to
 * `OmicsClaw-App/scripts/build-backend-runtime.py`, which downloads a
 * python-build-standalone tarball, installs the desktop dependency whitelist,
 * installs `omicsclaw` with `--no-deps`, strips bytecode, and smoke-tests the
 * result. Reimplementing any of that here would create a second source of
 * truth for what "a runtime" contains — and the two would drift.
 *
 * So the contract is a handoff:
 *
 *     python OmicsClaw-App/scripts/build-backend-runtime.py \
 *         --platform linux --arch x64 --omicsclaw-local /path/to/OmicsClaw
 *     # → OmicsClaw-App/backend-runtime/python/...
 *
 *     node npm/build-runtime-package.mjs \
 *         --target linux-x64 \
 *         --runtime-dir OmicsClaw-App/backend-runtime \
 *         --out npm/dist
 *     # → npm/dist/@omicsclaw/runtime-linux-x64/
 *
 * ## Sync contract
 *
 * The target list below must stay in step with three other places:
 *   - `SUPPORTED_TARGETS`             in build-backend-runtime.py (its own
 *                                     platform/arch spelling — see NPM_TARGETS)
 *   - the CI matrix                   in OmicsClaw-App/.github/workflows/backend-runtime.yml
 *   - `SUPPORTED_TARGETS`             in npm/omicsclaw/lib/resolve-runtime.mjs
 *   - `optionalDependencies`          in npm/omicsclaw/package.json
 *
 * Adding a target means touching all five. `--check-only` exists so CI can
 * assert the wrapper is well-formed without publishing anything.
 */

import { cp, mkdir, readdir, readFile, readlink, realpath, rm, stat, writeFile }
  from 'node:fs/promises';
import { basename, join, relative, resolve, sep } from 'node:path';
import { parseArgs } from 'node:util';

/**
 * npm target → the `os` / `cpu` fields npm filters optional dependencies by,
 * plus the platform/arch spelling `build-backend-runtime.py` uses.
 *
 * Only four entries, not six: `darwin-x64` and `win32-arm64` are marked
 * `skip-runtime: true` in the App's CI because llvmlite dropped macOS x86_64
 * wheels and no native Windows arm64 runner exists. Users on those hosts get
 * the guidance in `missingRuntimeMessage`.
 */
const NPM_TARGETS = {
  'linux-x64': { os: 'linux', cpu: 'x64', pyPlatform: 'linux', pyArch: 'x64' },
  'linux-arm64': { os: 'linux', cpu: 'arm64', pyPlatform: 'linux', pyArch: 'arm64' },
  'darwin-arm64': { os: 'darwin', cpu: 'arm64', pyPlatform: 'macos', pyArch: 'arm64' },
  'win32-x64': { os: 'win32', cpu: 'x64', pyPlatform: 'windows', pyArch: 'x64' },
};

function fail(message) {
  process.stderr.write(`${message}\n`);
  process.exit(1);
}

/** Relative path of the interpreter inside a runtime dir, per platform. */
function pythonRelPath(npmOs) {
  return npmOs === 'win32' ? join('python', 'python.exe') : join('python', 'bin', 'python3');
}

/**
 * Every symlink under `root`, as `{ path, target }` with POSIX-style relative
 * paths.
 *
 * ## Why this exists
 *
 * `npm pack` silently DROPS symlinks. A python-build-standalone runtime
 * contains about a thousand of them, and the loss is not cosmetic: the tarball
 * ends up with `bin/python3.11` but no `bin/python3`, and no
 * `lib/libpython3.11.so` → `libpython3.11.so.1.0`. This is why the
 * single-self-contained-binary projects (esbuild, swc, omicos) never hit the
 * problem and we do.
 *
 * Recording the links here and recreating them in postinstall restores the
 * exact PBS layout. Most of the thousand are `share/terminfo` aliases that
 * only matter for exotic `TERM` values, but roughly seven are load-bearing.
 *
 * Note that `omicsclawRuntime.pythonRelPath` in the manifest independently
 * points at the REAL interpreter file, so the resolver keeps working even when
 * postinstall never ran (`--ignore-scripts`). Restoration improves fidelity; it
 * is not the only thing standing between the package and a working runtime.
 */
async function collectSymlinks(root) {
  const found = [];

  async function walk(dir) {
    let entries;
    try {
      entries = await readdir(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      const abs = join(dir, entry.name);
      if (entry.isSymbolicLink()) {
        found.push({
          path: relative(root, abs).split(sep).join('/'),
          target: (await readlink(abs)).split(sep).join('/'),
        });
      } else if (entry.isDirectory()) {
        await walk(abs);
      }
    }
  }

  await walk(root);
  return found.sort((a, b) => a.path.localeCompare(b.path));
}

/**
 * Resolve the interpreter to the concrete file it ultimately points at, as a
 * path relative to the runtime root. On Unix `python/bin/python3` is a symlink
 * to `python3.11`; recording the resolved name means the resolver never has to
 * depend on a link that `npm pack` may have thrown away.
 */
async function resolveRealPythonRelPath(runtimeDir, npmOs) {
  const declared = join(runtimeDir, pythonRelPath(npmOs));
  const real = await realpath(declared);
  return relative(runtimeDir, real).split(sep).join('/');
}

async function assertRuntimeLooksComplete(runtimeDir, npmOs) {
  const python = join(runtimeDir, pythonRelPath(npmOs));
  try {
    const info = await stat(python);
    if (!info.isFile()) fail(`Not a file: ${python}`);
  } catch {
    fail(
      `No interpreter at ${python}\n\n` +
        'Expected a directory produced by build-backend-runtime.py, whose layout is\n' +
        '  <runtime-dir>/python/bin/python3      (linux, macos)\n' +
        '  <runtime-dir>/python/python.exe       (windows)',
    );
  }

  // A SKIPPED marker means the App's CI intentionally shipped no runtime for
  // this target. Packaging that would produce an npm package containing an
  // interpreter with no `omicsclaw` in it — strictly worse than publishing
  // nothing, because the resolver would find the binary and then fail at
  // import time.
  try {
    await stat(join(runtimeDir, 'SKIPPED'));
    fail(
      `${runtimeDir} contains a SKIPPED marker — that target has no runtime.\n` +
        'Do not package it; the resolver is designed to fall back instead.',
    );
  } catch {
    // No marker: the expected case.
  }
}

function buildManifest({ target, version, spec, pythonRelPath: realPythonRelPath }) {
  return {
    name: `@omicsclaw/runtime-${target}`,
    version,
    description: `OmicsClaw prebuilt Python runtime for ${target}`,
    license: 'Apache-2.0',
    repository: {
      type: 'git',
      url: 'git+https://github.com/zhou-1314/OmicsClaw.git',
      directory: 'npm',
    },
    // The whole point of the platform-package split: npm refuses to install a
    // package whose os/cpu do not match the host, and because the wrapper
    // lists these as OPTIONAL dependencies that refusal is a silent skip. Every
    // host downloads exactly one runtime.
    os: [spec.os],
    cpu: [spec.cpu],
    files: ['runtime', 'runtime-symlinks.json'],
    preferUnplugged: true,
    publishConfig: { access: 'public' },
    // Consumed by `omicsclaw`'s resolver and postinstall. `pythonRelPath` is
    // the DEREFERENCED interpreter, so resolution survives `npm pack` dropping
    // symlinks; see `collectSymlinks` for the whole story.
    omicsclawRuntime: {
      target,
      pythonRelPath: realPythonRelPath,
      symlinkManifest: 'runtime-symlinks.json',
    },
  };
}

const { values } = parseArgs({
  options: {
    target: { type: 'string' },
    'runtime-dir': { type: 'string' },
    out: { type: 'string', default: 'npm/dist' },
    version: { type: 'string' },
    'check-only': { type: 'boolean', default: false },
    help: { type: 'boolean', default: false },
  },
});

if (values.help) {
  process.stdout.write(
    [
      'Usage: node npm/build-runtime-package.mjs --target <t> --runtime-dir <dir> [options]',
      '',
      `  --target <t>        one of: ${Object.keys(NPM_TARGETS).join(', ')}`,
      '  --runtime-dir <dir> directory produced by build-backend-runtime.py',
      '  --out <dir>         output root (default: npm/dist)',
      '  --version <v>       package version (default: read from npm/omicsclaw/package.json)',
      '  --check-only        validate inputs and print the plan; write nothing',
      '',
    ].join('\n'),
  );
  process.exit(0);
}

const target = values.target;
if (!target) fail('Missing --target. Pass --help for usage.');
const spec = NPM_TARGETS[target];
if (!spec) {
  fail(
    `Unknown target ${target}.\nSupported: ${Object.keys(NPM_TARGETS).join(', ')}\n\n` +
      'darwin-x64 and win32-arm64 are intentionally absent — see NPM_TARGETS.',
  );
}

const runtimeDir = values['runtime-dir'];
if (!runtimeDir) fail('Missing --runtime-dir. Pass --help for usage.');
const runtimeSource = resolve(runtimeDir);

const wrapperManifestPath = resolve(import.meta.dirname, 'omicsclaw', 'package.json');
const version =
  values.version ?? JSON.parse(await readFile(wrapperManifestPath, 'utf-8')).version;

const outRoot = resolve(values.out);
const packageDir = join(outRoot, '@omicsclaw', `runtime-${target}`);

await assertRuntimeLooksComplete(runtimeSource, spec.os);

process.stdout.write(
  [
    `target       ${target}  (os=${spec.os} cpu=${spec.cpu})`,
    `version      ${version}`,
    `runtime from ${runtimeSource}`,
    `package to   ${packageDir}`,
    '',
  ].join('\n'),
);

if (values['check-only']) {
  process.stdout.write('check-only: inputs look valid, nothing written.\n');
  process.exit(0);
}

await rm(packageDir, { recursive: true, force: true });
await mkdir(packageDir, { recursive: true });

// `verbatimSymlinks` keeps PBS's internal links (bin/python3 → python3.11 and
// friends) as links instead of expanding them into duplicate copies, which
// would both bloat the package and break the interpreter's own prefix logic.
await cp(runtimeSource, join(packageDir, 'runtime'), {
  recursive: true,
  verbatimSymlinks: true,
});

// Recorded from the SOURCE tree, before npm has a chance to drop them.
const symlinks = await collectSymlinks(runtimeSource);
const realPythonRelPath = await resolveRealPythonRelPath(runtimeSource, spec.os);

await writeFile(
  join(packageDir, 'runtime-symlinks.json'),
  `${JSON.stringify({ version: 1, links: symlinks }, null, 2)}\n`,
);

await writeFile(
  join(packageDir, 'package.json'),
  `${JSON.stringify(
    buildManifest({ target, version, spec, pythonRelPath: realPythonRelPath }),
    null,
    2,
  )}\n`,
);

await writeFile(
  join(packageDir, 'README.md'),
  [
    `# @omicsclaw/runtime-${target}`,
    '',
    `Prebuilt Python runtime for \`${target}\`, consumed by the [\`omicsclaw\`](https://www.npmjs.com/package/omicsclaw) package.`,
    '',
    'Do not install this directly. It is listed as an optional dependency of',
    '`omicsclaw`, and npm picks the one matching your platform automatically:',
    '',
    '```sh',
    'npm install -g omicsclaw',
    '```',
    '',
    'Built by `npm/build-runtime-package.mjs` from the output of',
    '`OmicsClaw-App/scripts/build-backend-runtime.py`.',
    '',
  ].join('\n'),
);

process.stdout.write(
  `Wrote ${basename(packageDir)} (interpreter ${realPythonRelPath}, ${symlinks.length} symlinks recorded)\n`,
);
