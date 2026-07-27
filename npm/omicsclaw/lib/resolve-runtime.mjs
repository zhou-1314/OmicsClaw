/**
 * Locate the platform-specific runtime package for the host.
 *
 * The distribution follows the standard npm "thin wrapper + optional platform
 * packages" model (esbuild / swc / biome all use it): the `omicsclaw` package
 * itself carries no runtime, and lists one `@omicsclaw/runtime-<target>` per
 * supported host in `optionalDependencies`.
 * Each of those declares `os` / `cpu`, so npm refuses to install the ones that
 * do not match the host — and because they are *optional*, that refusal is a
 * silent skip rather than an install failure. Exactly one lands on disk.
 *
 * Everything here is pure except `resolveRuntime`, which touches the module
 * resolver and the filesystem through injected seams so it can be unit-tested.
 */

import { createRequire } from 'node:module';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';

/**
 * Hosts that ship a prebuilt runtime.
 *
 * This is deliberately NOT every platform npm runs on. Two targets are absent
 * because the upstream runtime cannot be built for them today — the same two
 * that `OmicsClaw-App/.github/workflows/backend-runtime.yml` marks
 * `skip-runtime: true`:
 *
 *   - darwin-x64  — llvmlite 0.47 (required by numba, required by scanpy) no
 *                   longer publishes macOS x86_64 wheels, and the source build
 *                   needs a pinned LLVM toolchain that is brittle in CI.
 *   - win32-arm64 — no native Windows arm64 hosted runner exists, and Windows
 *                   has no Rosetta equivalent for cross-arch execution.
 *
 * Users on those hosts fall back to a conda / BYO-Python install; see
 * `missingRuntimeMessage`.
 */
export const SUPPORTED_TARGETS = Object.freeze([
  'linux-x64',
  'linux-arm64',
  'darwin-arm64',
  'win32-x64',
]);

/** Directory inside a runtime package that holds the extracted interpreter. */
export const RUNTIME_SUBDIR = 'runtime';

/**
 * Subdirectory of `runtime/` where the python-build-standalone tarball is
 * extracted. PBS `install_only` archives contain a top-level `python/`, kept
 * intact so PBS's own relocatable `sys.prefix` resolution keeps working
 * without `PYTHONHOME` hacks. Mirrors `BUNDLED_PYTHON_SUBDIR` in
 * `OmicsClaw-App/src/lib/python-runtime.ts`.
 */
export const PYTHON_SUBDIR = 'python';

/** Target triple for a host, in the `<platform>-<arch>` form npm's own
 *  `process.platform` / `process.arch` produce. */
export function currentTarget(platform = process.platform, arch = process.arch) {
  return `${platform}-${arch}`;
}

export function isSupportedTarget(target) {
  return SUPPORTED_TARGETS.includes(target);
}

export function runtimePackageName(target) {
  return `@omicsclaw/runtime-${target}`;
}

/**
 * Path of the Python binary inside an extracted PBS runtime.
 *
 * PBS `install_only` puts `python.exe` at the root of `python/` on Windows
 * (`Scripts/` holds pip-installed console scripts instead), and
 * `python/bin/python3` on Unix — with no unversioned `python` symlink, so the
 * `3` suffix is load-bearing. Same two-branch rule as `bundledPythonPath()` in
 * `OmicsClaw-App/src/lib/python-runtime.ts`.
 */
export function pythonBinaryPath(runtimeRoot, platform = process.platform) {
  const pythonRoot = join(runtimeRoot, PYTHON_SUBDIR);
  return platform === 'win32'
    ? join(pythonRoot, 'python.exe')
    : join(pythonRoot, 'bin', 'python3');
}

/**
 * Resolve the installed runtime for a target, or `null` when it is absent.
 *
 * Absence is an expected state, not an error: an unsupported host, an
 * `--omit=optional` / `--no-optional` install, or an npm version too old to
 * honour `os` / `cpu` filtering all produce it. Callers render
 * `missingRuntimeMessage` and exit non-zero.
 */
export function resolveRuntime({
  target = currentTarget(),
  platform = process.platform,
  resolveFrom = import.meta.url,
  fileExists = existsSync,
  readManifest = (p) => JSON.parse(readFileSync(p, 'utf-8')),
} = {}) {
  if (!isSupportedTarget(target)) return null;

  const packageName = runtimePackageName(target);
  const requireFrom = createRequire(resolveFrom);

  let manifestPath;
  try {
    // Resolve the manifest rather than the package root: a package without an
    // `exports` map cannot be `require.resolve`d by bare name, but its
    // `package.json` always resolves.
    manifestPath = requireFrom.resolve(`${packageName}/package.json`);
  } catch {
    return null;
  }

  const packageRoot = dirname(manifestPath);
  const runtimeRoot = join(packageRoot, RUNTIME_SUBDIR);

  // Prefer the dereferenced interpreter path the build script recorded.
  //
  // `npm pack` drops symlinks, and on Unix the conventional
  // `python/bin/python3` IS a symlink to `python/bin/python3.<minor>` — so in a
  // published package it simply does not exist. The build script resolves the
  // link and writes the real name into `omicsclawRuntime.pythonRelPath`, which
  // makes resolution independent of whether postinstall got a chance to
  // recreate the links. The conventional path stays as the fallback for a
  // runtime built before this field existed, or one used straight from disk
  // where the symlinks are intact.
  let recorded;
  try {
    recorded = readManifest(manifestPath)?.omicsclawRuntime?.pythonRelPath;
  } catch {
    recorded = undefined;
  }

  const candidates = [];
  if (typeof recorded === 'string' && recorded.length > 0) {
    candidates.push(join(runtimeRoot, ...recorded.split('/')));
  }
  candidates.push(pythonBinaryPath(runtimeRoot, platform));

  const pythonPath = candidates.find((candidate) => fileExists(candidate));
  if (pythonPath === undefined) return null;

  return { packageName, packageRoot, runtimeRoot, pythonPath };
}

/** Operator-facing explanation for a host with no runtime package. */
export function missingRuntimeMessage(target = currentTarget()) {
  const supported = SUPPORTED_TARGETS.join(', ');

  if (!isSupportedTarget(target)) {
    return [
      `OmicsClaw does not ship a prebuilt runtime for ${target}.`,
      '',
      `Prebuilt runtimes exist for: ${supported}.`,
      '',
      'Install from source instead:',
      '',
      '  git clone https://github.com/zhou-1314/OmicsClaw.git',
      '  cd OmicsClaw && ./0_setup_env.sh',
      '',
      'That path builds the scientific stack with conda/mamba and works on',
      'every host OmicsClaw supports.',
    ].join('\n');
  }

  return [
    `The runtime package for ${target} is not installed.`,
    '',
    `Expected to find ${runtimePackageName(target)} next to this package.`,
    'This usually means the install skipped optional dependencies.',
    '',
    'Reinstall with optional dependencies enabled:',
    '',
    '  npm install -g omicsclaw',
    '',
    'If you installed with --omit=optional or --no-optional, drop that flag.',
  ].join('\n');
}
