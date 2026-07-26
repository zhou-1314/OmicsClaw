/**
 * Where the user actually types things, and what our console-script names
 * resolve to from there.
 *
 * Ported from the same layer in Kimi Code's postinstall, which solves an
 * identical problem: a CLI that used to be installed by a Python tool
 * (`pipx` / `uv tool` / `pip`) is now shipped through npm, and the two shims
 * collide on PATH. The structure is kept recognisably the same so fixes can be
 * carried across, with one substantive change: OmicsClaw publishes FOUR console
 * scripts, so every entry point here takes the binary name as a parameter
 * instead of closing over a single constant.
 *
 * Covers:
 *   - Package-manager detection and manager-specific command hints.
 *   - Global-install gating — what counts as global across npm / yarn classic
 *     / pnpm.
 *   - Own-package-root location.
 *   - User-shell PATH — spawns `$SHELL -l` so reachability is judged against
 *     the shell the user will actually type `omicsclaw` into, not the
 *     installer's environment.
 *   - The combined PATH dispatcher, called once so detection and reachability
 *     stay symmetric and the shell probe does not run twice.
 *   - The reachability check, which walks PATH pretending the to-be-removed
 *     shims are gone and reports what wins.
 *
 * Every function here is pure with respect to the filesystem: no mutations.
 */

import { spawn } from 'node:child_process';
import { promises as fs } from 'node:fs';
import { delimiter, dirname, join, sep } from 'node:path';

const IS_WINDOWS = process.platform === 'win32';

/**
 * Console scripts OmicsClaw's `pyproject.toml` declares, and therefore the
 * names that can collide between a pip install and this npm package. Kept in
 * PATH-shadowing priority order for reporting only; each is migrated
 * independently.
 */
export const CONSOLE_SCRIPTS = Object.freeze([
  'omicsclaw',
  'oc',
  'omicsclaw-chat',
  'oc-chat',
]);

/**
 * Expand a basename into the filenames the OS would actually match on PATH.
 *
 * POSIX: just the name. Windows: every extension in `PATHEXT`, because
 * `uv tool install` produces `omicsclaw.exe` and npm produces `omicsclaw.cmd` —
 * a bare-name walk would miss both.
 */
export function executableCandidates(basename) {
  if (!IS_WINDOWS) return [basename];
  const pathext = (process.env['PATHEXT'] ?? '.EXE;.CMD;.BAT;.COM')
    .toLowerCase()
    .split(';')
    .map((e) => e.trim())
    .filter(Boolean);
  return [basename, ...pathext.map((ext) => basename + ext)];
}

/**
 * Identify which package manager ran us. `npm_config_user_agent` is set by
 * npm, yarn (classic and berry), and pnpm, prefixed with the manager name.
 */
export function detectPackageManager() {
  const ua = process.env['npm_config_user_agent'] ?? '';
  if (ua.startsWith('pnpm/')) return 'pnpm';
  if (ua.startsWith('yarn/')) return 'yarn';
  return 'npm';
}

/** Manager-specific command that prints the global bin directory. */
export function pmGlobalBinCommand(pm) {
  switch (pm) {
    case 'pnpm':
      return 'pnpm bin -g';
    case 'yarn':
      return 'yarn global bin';
    default:
      return 'npm prefix -g';
  }
}

/** Manager-specific reinstall command, used in user-facing hints. */
export function pmGlobalInstallCommand(pm, pkg) {
  switch (pm) {
    case 'pnpm':
      return `pnpm add -g ${pkg}`;
    case 'yarn':
      return `yarn global add ${pkg}`;
    default:
      return `npm install -g ${pkg}`;
  }
}

/**
 * Did the user run a global install?
 *
 * Four signals, any of which means "this lands in the manager's global bin":
 *
 *   - `npm_config_global === 'true'`     — `npm install -g`, and pnpm for
 *                                          back-compat.
 *   - `pnpm_config_global === 'true'`    — pnpm's own flag.
 *   - `npm_config_location === 'global'` — npm 7+ with `--location=global`.
 *                                          npm deliberately does NOT also set
 *                                          `npm_config_global` here, so
 *                                          without this branch the migration
 *                                          silently no-ops for that form.
 *   - {@link isYarnClassicGlobalAdd}     — yarn classic sets neither.
 *
 * Local installs, `npx`, `pnpm dlx`, and workspace bootstraps leave all four
 * false, which is the desired no-op.
 */
export function isGlobalInstall() {
  return (
    process.env['npm_config_global'] === 'true' ||
    process.env['pnpm_config_global'] === 'true' ||
    process.env['npm_config_location'] === 'global' ||
    isYarnClassicGlobalAdd()
  );
}

// Yarn 1.x global subcommands. Only the install-class ones actually run our
// postinstall; the rest are listed so detection is consistent.
const YARN_GLOBAL_SUBCOMMANDS = new Set([
  'add',
  'remove',
  'upgrade',
  'upgrade-interactive',
  'list',
  'bin',
  'dir',
]);

/**
 * `yarn global add` runs lifecycle scripts but leaves both
 * `npm_config_global` and `npm_config_location` unset. The only in-band signal
 * is `npm_config_argv`, which yarn populates with the original command line as
 * JSON. Require both a `yarn/1.` user agent and a literal `global` token
 * followed by a known global subcommand — that accepts
 * `yarn --cwd /tmp global add foo` without having to model yarn's whole flag
 * table, and rejects `yarn add global`. Any residual false positive is caught
 * downstream by the reachability gate, which refuses to migrate when our own
 * shim is not what PATH resolves to.
 */
function isYarnClassicGlobalAdd() {
  const ua = process.env['npm_config_user_agent'] ?? '';
  if (!ua.startsWith('yarn/1.')) return false;
  const raw = process.env['npm_config_argv'];
  if (!raw) return false;
  let argv;
  try {
    argv = JSON.parse(raw);
  } catch {
    return false;
  }
  if (!Array.isArray(argv?.original)) return false;
  const globalIdx = argv.original.indexOf('global');
  if (globalIdx === -1) return false;
  const next = argv.original[globalIdx + 1];
  return typeof next === 'string' && YARN_GLOBAL_SUBCOMMANDS.has(next);
}

/**
 * Locate the realpath of our own installed package root by walking up from
 * `startDir` for the nearest `package.json`. Realpath matters because npm
 * symlinks global bin entries into the package, and the caller compares
 * resolved paths.
 *
 * Returns null when nothing is found within a few levels; callers treat that
 * as "cannot locate ourselves" and bail rather than guess.
 */
export async function ownPackageRoot(startDir) {
  let dir = startDir;
  for (let i = 0; i < 6; i++) {
    try {
      await fs.access(join(dir, 'package.json'));
      try {
        return await fs.realpath(dir);
      } catch {
        return dir;
      }
    } catch {
      // No package.json here; walk up.
    }
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return null;
}

async function isExecutableFile(filePath) {
  try {
    const info = await fs.stat(filePath);
    if (!info.isFile()) return false;
    // Windows ACLs are not meaningfully visible through stat().mode, and
    // callers only pass candidates that already matched a PATHEXT extension,
    // so existence is the right test there.
    if (IS_WINDOWS) return true;
    return (info.mode & 0o111) !== 0;
  } catch {
    return false;
  }
}

/**
 * Substrings that identify a shim this npm package generated.
 *
 * Needed as a fallback where realpath alone cannot place the shim inside our
 * package root: Windows cmd-shims are literal `.cmd` / `.ps1` files rather
 * than symlinks, and pnpm's POSIX shims are `/bin/sh` scripts rather than
 * symlinks. Both embed the resolved package path, so the `node_modules`-
 * qualified form is specific enough not to false-match a Python shim that
 * merely mentions the word `omicsclaw`.
 */
const OWN_PACKAGE_MARKERS = ['node_modules/omicsclaw', 'node_modules\\omicsclaw'];

async function shimReferencesOwnPackage(shimPath) {
  try {
    const handle = await fs.open(shimPath, 'r');
    try {
      const buf = Buffer.alloc(4096);
      const { bytesRead } = await handle.read(buf, 0, 4096, 0);
      const text = buf.subarray(0, bytesRead).toString('latin1');
      return OWN_PACKAGE_MARKERS.some((m) => text.includes(m));
    } finally {
      await handle.close().catch(() => {});
    }
  } catch {
    return false;
  }
}

async function classifyOwnership(shim, ownRoot, ownPrefix) {
  let real;
  try {
    real = await fs.realpath(shim);
  } catch {
    return 'unreadable';
  }
  if (real === ownRoot || real.startsWith(ownPrefix)) return 'own';
  if (await shimReferencesOwnPackage(shim)) return 'own';
  return 'other';
}

/**
 * Walk `pathString` and report what the user's shell would resolve `binName`
 * to, AFTER the shims in `actionableShimPaths` are pretended gone. Returns the
 * first match in PATH order:
 *
 *   - `{ kind: 'own' }`                  — our shim wins; safe to proceed.
 *   - `{ kind: 'blocked-legacy', shim }` — a legacy shim we detected but
 *                                          cannot touch still wins.
 *   - `{ kind: 'foreign', path }`        — something we neither recognise as
 *                                          the legacy CLI nor generated wins,
 *                                          e.g. a user's own wrapper. Notably
 *                                          this is how an unrelated `oc` (the
 *                                          OpenShift client) surfaces.
 *   - `{ kind: 'none' }`                 — nothing resolves; our bin dir is
 *                                          not on the user's PATH.
 *
 * Each blocker needs different remediation, which is why they are distinct
 * rather than a boolean. `allDetectedShimPaths` lets us tell a surviving
 * blocked legacy apart from a completely unknown binary.
 */
export async function findFirstResolvable(
  binName,
  ownRoot,
  pathString,
  actionableShimPaths,
  allDetectedShimPaths,
) {
  if (!ownRoot || !pathString) return { kind: 'none' };
  const ownPrefix = ownRoot + sep;
  const candidates = executableCandidates(binName);
  const skipSet = new Set(actionableShimPaths ?? []);
  const knownLegacySet = new Set(allDetectedShimPaths ?? []);
  const seenDirs = new Set();

  for (const dir of pathString.split(delimiter)) {
    if (!dir || seenDirs.has(dir)) continue;
    seenDirs.add(dir);
    for (const name of candidates) {
      const shim = join(dir, name);
      if (skipSet.has(shim)) continue;
      if (!(await isExecutableFile(shim))) continue;
      const kind = await classifyOwnership(shim, ownRoot, ownPrefix);
      if (kind === 'unreadable') continue;
      if (kind === 'own') return { kind: 'own' };
      if (knownLegacySet.has(shim)) return { kind: 'blocked-legacy', shim };
      return { kind: 'foreign', path: shim };
    }
  }
  return { kind: 'none' };
}

/**
 * Read the user's default shell's view of PATH.
 *
 * `process.env.PATH` reflects whichever shell invoked the package manager,
 * which need not be the daily-driver shell the user will later type
 * `omicsclaw` into. A zsh-only PATH entry, a `.bash_profile` that never
 * sources `.bashrc`, or a sudo'd install that scrubs HOME all defeat the
 * installer's-PATH heuristic.
 *
 * `$SHELL -l -c` reads the login profile chain. `-i` would also pull in
 * interactive rc files but needs a tty for some shells and is far likelier to
 * have side effects, so the narrower view is taken and an unknown result falls
 * through to a gentler check.
 */
export async function userShellPath() {
  // Windows has no login-shell profile chain worth probing: cmd and PowerShell
  // take PATH from the persistent registry environment, which is exactly what
  // `process.env.PATH` already reflects.
  if (IS_WINDOWS) return { kind: 'unknown', reason: 'windows skip' };

  const shell = process.env['SHELL'];
  if (!shell) return { kind: 'unknown', reason: 'no SHELL env var' };

  return new Promise((resolve) => {
    let settled = false;
    const settle = (value) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };

    let stdout = '';
    // Delimit the value so anything else the profile prints (motd, prompt
    // redraw, version managers announcing themselves) can be parsed off.
    const probe =
      'printf "<<<OMICSCLAW_PATH_BEGIN>>>%s<<<OMICSCLAW_PATH_END>>>\\n" "$PATH"';
    const child = spawn(shell, ['-l', '-c', probe], {
      stdio: ['ignore', 'pipe', 'ignore'],
    });
    child.stdout.on('data', (chunk) => {
      stdout += chunk.toString('utf-8');
    });

    // Bound the wait: login profiles are normally fast, but a pathological one
    // could block forever and we must never hang an install.
    const timer = setTimeout(() => {
      child.kill('SIGKILL');
      settle({ kind: 'unknown', reason: 'shell spawn timed out' });
    }, 5000);

    child.on('close', (code) => {
      clearTimeout(timer);
      const match = stdout.match(
        /<<<OMICSCLAW_PATH_BEGIN>>>([\s\S]*?)<<<OMICSCLAW_PATH_END>>>/,
      );
      if (match && match[1].length > 0) {
        settle({ kind: 'ok', path: match[1] });
        return;
      }
      settle({ kind: 'unknown', reason: `no PATH printed (exit ${code})` });
    });
    child.on('error', (err) => {
      clearTimeout(timer);
      settle({ kind: 'unknown', reason: `spawn error: ${err.message}` });
    });
  });
}

/**
 * Compute the two PATH strings the postinstall consults, probing the shell
 * exactly once.
 *
 *   - `detection`    — union of the shell PATH and `process.env.PATH`. Either
 *                      may hold a legacy shim worth renaming, including one in
 *                      a directory the other cannot see.
 *   - `reachability` — the shell PATH alone when available, else the process
 *                      PATH. Deliberately NOT a union: a shim visible only in
 *                      the installer's environment does not help the user
 *                      afterwards, so unioning would call a broken install
 *                      reachable.
 */
export async function postinstallPaths() {
  const shellResult = await userShellPath();
  const processPath = process.env['PATH'] ?? '';
  const shellPathStr = shellResult.kind === 'ok' ? shellResult.path : null;
  return {
    detection: unionPaths(shellPathStr, processPath),
    reachability: shellPathStr ?? processPath,
  };
}

function unionPaths(...paths) {
  const seen = new Set();
  const out = [];
  for (const p of paths) {
    if (!p) continue;
    for (const entry of p.split(delimiter)) {
      if (!entry || seen.has(entry)) continue;
      seen.add(entry);
      out.push(entry);
    }
  }
  return out.join(delimiter);
}
