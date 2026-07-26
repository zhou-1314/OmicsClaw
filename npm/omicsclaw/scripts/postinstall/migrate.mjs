/**
 * Detection of previously pip-installed OmicsClaw console scripts, plus the
 * filesystem operations that perform — or refuse — the takeover.
 *
 * Ported from Kimi Code's postinstall migrator, which faced the same shape of
 * problem (a Python-installed CLI shadowing the npm one). Adapted for
 * OmicsClaw's four console scripts: every function takes the binary name, and
 * the orchestrator runs the algorithm once per name.
 *
 * Detection:
 *   - {@link detectLegacyShims}: walks a PATH string and returns every legacy
 *     shim for one binary name, in PATH order. Returning ALL hits matters —
 *     a user with both `uv tool install` and `pipx install` has two shims in
 *     different directories, and renaming only the earlier one leaves the
 *     later one still shadowing us.
 *   - {@link isLegacyShim}: the same test standalone, used to decide whether
 *     an existing `<name>-legacy` is itself a legacy shim (safe to consolidate
 *     onto) or a user-managed file (must be preserved).
 *
 * Classify then execute, in two phases:
 *   - {@link classifyShim} reports what we COULD do, touching nothing.
 *   - {@link renameInPlace} / {@link deleteShim} actually mutate.
 *
 * Splitting them lets the orchestrator decide abort-or-proceed once against
 * the whole detected set, instead of discovering mid-loop that a write failed
 * and leaving the user with a "takeover succeeded" notice above a
 * "permission denied" one.
 */

import { constants as fsConstants, promises as fs } from 'node:fs';
import { delimiter, dirname, extname, join, sep } from 'node:path';

const LEGACY_SUFFIX = '-legacy';
const IS_WINDOWS = process.platform === 'win32';

/**
 * Import lines a setuptools-generated OmicsClaw console script contains.
 *
 * `omicsclaw.surfaces.cli` is the current entry-point module; `omicsclaw.cli`
 * is where it lived before the ADR 0005 surfaces carve-out, and a shim from
 * that era can still be sitting on a long-lived machine.
 */
const PYTHON_ENTRYPOINT_MARKERS = ['from omicsclaw.surfaces.cli', 'from omicsclaw.cli'];

/**
 * Substring that proves a file is a Node shim rather than a Python one.
 *
 * Defence in depth against a nasty self-inflicted failure: this npm package's
 * own `bin/omicsclaw.mjs` embeds the literal string
 * `from omicsclaw.surfaces.cli.launcher import main` in its Python bootstrap,
 * so it matches {@link PYTHON_ENTRYPOINT_MARKERS}. The own-package-root check
 * upstream already excludes it, but a shim we mis-identify is a shim we
 * DELETE, so a second independent signal is worth the four lines.
 */
const NODE_SHIM_MARKER = 'node_modules';

// Read window for the marker sniff.
//   POSIX:   setuptools entry-point scripts are a few hundred bytes; 4 KiB is
//            generous.
//   Windows: `uv tool install` emits a Rust-built launcher .exe of ~45 KiB
//            with the module name embedded near the END, so the whole file has
//            to be considered. Capped so a hostile or unexpectedly large file
//            cannot make us hold much memory.
const SHIM_SNIFF_BYTES_POSIX = 4096;
const SHIM_SNIFF_BYTES_WINDOWS_MAX = 256 * 1024;

function pathEntries(pathString) {
  if (!pathString) return [];
  const seen = new Set();
  const out = [];
  for (const entry of pathString.split(delimiter)) {
    if (!entry || seen.has(entry)) continue;
    seen.add(entry);
    out.push(entry);
  }
  return out;
}

function executableCandidates(basename) {
  if (!IS_WINDOWS) return [basename];
  const pathext = (process.env['PATHEXT'] ?? '.EXE;.CMD;.BAT;.COM')
    .toLowerCase()
    .split(';')
    .map((e) => e.trim())
    .filter(Boolean);
  return [basename, ...pathext.map((ext) => basename + ext)];
}

async function isExecutableFile(filePath) {
  try {
    const info = await fs.stat(filePath);
    if (!info.isFile()) return false;
    if (IS_WINDOWS) return true;
    return (info.mode & 0o111) !== 0;
  } catch {
    return false;
  }
}

async function readShimHead(filePath) {
  let handle;
  try {
    handle = await fs.open(filePath, 'r');
    const stat = await handle.stat();
    const limit = IS_WINDOWS ? SHIM_SNIFF_BYTES_WINDOWS_MAX : SHIM_SNIFF_BYTES_POSIX;
    const target = Math.min(stat.size, limit);
    const buffer = Buffer.alloc(target);
    const { bytesRead } = await handle.read(buffer, 0, target, 0);
    // latin1 is a 1:1 byte-to-char mapping. We are searching for an ASCII
    // substring inside what may be a binary launcher, and UTF-8 decoding would
    // mangle the bytes around it.
    return buffer.subarray(0, bytesRead).toString('latin1');
  } catch {
    return null;
  } finally {
    if (handle) await handle.close().catch(() => {});
  }
}

/** Does this file's content look like a pip-installed OmicsClaw entry point? */
function headLooksLegacy(head) {
  if (!head) return false;
  if (head.includes(NODE_SHIM_MARKER)) return false;
  return PYTHON_ENTRYPOINT_MARKERS.some((marker) => head.includes(marker));
}

/**
 * Every legacy shim for `binName` on `pathString`, in PATH order. An empty
 * array means "fresh install, nothing to migrate" — the common case.
 *
 * A shim qualifies when it realpath-resolves OUTSIDE our own package root and
 * its content carries a Python entry-point marker. Anything we cannot read, or
 * that resolves into our own package, is left strictly alone.
 */
export async function detectLegacyShims(binName, ownRoot, pathString) {
  const ownRootPrefix = ownRoot ? ownRoot + sep : null;
  const candidates = executableCandidates(binName);
  const results = [];
  const seenShims = new Set();

  for (const dir of pathEntries(pathString)) {
    for (const name of candidates) {
      const shimPath = join(dir, name);
      if (seenShims.has(shimPath)) continue;
      if (!(await isExecutableFile(shimPath))) continue;

      let realPath;
      try {
        realPath = await fs.realpath(shimPath);
      } catch {
        continue;
      }

      // Never touch something that resolves into our own installed package.
      if (
        ownRootPrefix !== null &&
        (realPath === ownRoot || realPath.startsWith(ownRootPrefix))
      ) {
        continue;
      }

      if (!headLooksLegacy(await readShimHead(realPath))) continue;

      seenShims.add(shimPath);
      results.push({ binName, shimPath, realPath });
    }
  }
  return results;
}

/**
 * Is the file at `p` a legacy OmicsClaw shim? Used to decide whether an
 * existing `<name>-legacy` is one of ours (safe to drop the duplicate) or a
 * user-managed file we must not clobber.
 */
export async function isLegacyShim(p) {
  let real;
  try {
    real = await fs.realpath(p);
  } catch {
    return false;
  }
  return headLooksLegacy(await readShimHead(real));
}

async function pathExists(p) {
  try {
    // lstat, not access/stat: a dangling symlink at the target must still count
    // as existing. `fs.access` follows links and would report ENOENT, after
    // which `fs.rename` would silently replace the link.
    await fs.lstat(p);
    return true;
  } catch {
    return false;
  }
}

/**
 * Where a shim should be renamed to. The extension is preserved so a Windows
 * `omicsclaw.exe` becomes `omicsclaw-legacy.exe` rather than an extension-less
 * file the shell will not run.
 */
export function renameTargetFor(shimPath, binName) {
  const ext = extname(shimPath);
  return join(dirname(shimPath), binName + LEGACY_SUFFIX + ext);
}

/**
 * Is the shim's directory system-managed and therefore unwritable?
 *
 * POSIX: owned by uid 0, which is what `sudo pip install` into `/usr/local/bin`
 * produces. Windows: under a well-known system root. uv and pipx install into
 * user space, so on Windows this rarely fires — but it classifies the
 * admin-prefix case correctly when it does.
 *
 * Drives the notice text: a blocked system path needs sudo / admin advice, not
 * a bare "rename it yourself".
 */
async function isSystemOwnedDir(shimPath) {
  if (IS_WINDOWS) {
    const dir = dirname(shimPath).toLowerCase();
    const systemRoots = [
      'c:\\program files',
      'c:\\program files (x86)',
      'c:\\programdata',
      'c:\\windows',
    ];
    return systemRoots.some((root) => dir === root || dir.startsWith(root + '\\'));
  }
  try {
    const info = await fs.stat(dirname(shimPath));
    return info.uid === 0;
  } catch {
    return false;
  }
}

async function canWriteDir(dir) {
  try {
    // rename and unlink both need write+execute on the parent directory.
    await fs.access(dir, fsConstants.W_OK | fsConstants.X_OK);
    return true;
  } catch {
    return false;
  }
}

/**
 * Pre-flight inspection of one shim. Reports what we could do without doing
 * it. Result kinds:
 *
 *   - `renameable`  — the `<name>-legacy` slot is free; a clean rename works.
 *   - `consolidate` — the target exists and is itself a legacy shim, so we
 *                     unlink this duplicate and keep the existing one as the
 *                     fallback. Functionally equivalent: same upstream package.
 *   - `delete-only` — the target exists but is user-managed. We will not
 *                     clobber it. We can still unlink the shim to stop the
 *                     shadowing, but the "preserve a fallback" promise fails
 *                     for this directory and the user is told so.
 *   - `blocked`     — the parent directory is not writable. Carries
 *                     `isSystemPath` so the renderer can suggest the right
 *                     escalation.
 */
export async function classifyShim(shimPath, binName) {
  const target = renameTargetFor(shimPath, binName);
  const dir = dirname(shimPath);

  if (!(await canWriteDir(dir))) {
    return {
      kind: 'blocked',
      binName,
      shimPath,
      target,
      isSystemPath: await isSystemOwnedDir(shimPath),
    };
  }

  if (await pathExists(target)) {
    if (await isLegacyShim(target)) {
      return { kind: 'consolidate', binName, shimPath, target };
    }
    return { kind: 'delete-only', binName, shimPath, target };
  }

  return { kind: 'renameable', binName, shimPath, target };
}

/** Execute the rename. Classification already established it should work. */
export async function renameInPlace(shimPath, target) {
  try {
    await fs.rename(shimPath, target);
    return { success: true };
  } catch (err) {
    const code = err && typeof err === 'object' ? err.code : undefined;
    const message = err instanceof Error ? err.message : String(err);
    return { success: false, code, message };
  }
}

/**
 * Execute the unlink. Used when consolidating onto an existing legacy shim,
 * when the rename target is foreign but the shadow can still be cleared, or
 * for any shim after the first — a dormant duplicate adds nothing.
 */
export async function deleteShim(shimPath) {
  try {
    await fs.unlink(shimPath);
    return { success: true };
  } catch (err) {
    const code = err && typeof err === 'object' ? err.code : undefined;
    const message = err instanceof Error ? err.message : String(err);
    return { success: false, code, message };
  }
}
