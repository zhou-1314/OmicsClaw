/**
 * Recreate the symlinks `npm pack` threw away.
 *
 * ## The problem
 *
 * npm strips symlinks when packing a tarball. A python-build-standalone
 * runtime has roughly a thousand of them, so a published
 * `@omicsclaw/runtime-<target>` arrives with `python/bin/python3.11` but no
 * `python/bin/python3`, and no `python/lib/libpython3.11.so` pointing at
 * `libpython3.11.so.1.0`.
 *
 * Most of the missing links are `share/terminfo` aliases that only matter for
 * unusual `TERM` values. About seven are load-bearing. Rather than reason about
 * which, the build script records every link into `runtime-symlinks.json` and
 * this module puts them all back.
 *
 * ## What this is NOT responsible for
 *
 * Finding the interpreter. `omicsclawRuntime.pythonRelPath` in the runtime
 * package's manifest names the real, dereferenced binary, so `resolveRuntime`
 * works whether or not this ever ran — which matters, because a user can
 * install with `--ignore-scripts`. Restoration raises fidelity; it is not the
 * difference between working and broken.
 *
 * ## Safety
 *
 * A manifest is data that travelled with a downloaded package, so it is
 * treated as untrusted: any entry whose link path or resolved target would
 * land outside the runtime directory is skipped. Without that check a
 * malicious or corrupted manifest could plant a link anywhere the installing
 * user can write.
 */

import { existsSync, lstatSync, mkdirSync, readFileSync, symlinkSync } from 'node:fs';
import { dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';

/** Is `candidate` inside `root` (or root itself)? */
function isInside(root, candidate) {
  const rel = relative(root, candidate);
  return rel === '' || (!rel.startsWith('..' + sep) && rel !== '..' && !isAbsolute(rel));
}

/**
 * Recreate every link in the manifest under `runtimeRoot`.
 *
 * Idempotent: an entry whose path already exists is left alone, so re-running
 * an install never churns the tree. Returns counts rather than throwing —
 * postinstall must not fail an install over this.
 */
export function restoreSymlinks(runtimeRoot, manifestPath) {
  const result = { created: 0, existing: 0, skipped: 0, failed: 0 };

  let manifest;
  try {
    manifest = JSON.parse(readFileSync(manifestPath, 'utf-8'));
  } catch {
    return result;
  }
  if (!manifest || !Array.isArray(manifest.links)) return result;

  const root = resolve(runtimeRoot);

  for (const entry of manifest.links) {
    if (
      entry === null ||
      typeof entry !== 'object' ||
      typeof entry.path !== 'string' ||
      typeof entry.target !== 'string' ||
      entry.path.length === 0 ||
      entry.target.length === 0
    ) {
      result.skipped += 1;
      continue;
    }

    const linkPath = resolve(root, ...entry.path.split('/'));
    if (!isInside(root, linkPath)) {
      result.skipped += 1;
      continue;
    }

    // The target is stored exactly as `readlink` reported it — usually
    // relative, and interpreted relative to the link's own directory.
    const targetAbs = isAbsolute(entry.target)
      ? entry.target
      : resolve(dirname(linkPath), ...entry.target.split('/'));
    if (!isInside(root, targetAbs)) {
      result.skipped += 1;
      continue;
    }

    try {
      // lstat, not existsSync: a link whose target is missing still counts as
      // present and must not be recreated on top of.
      lstatSync(linkPath);
      result.existing += 1;
      continue;
    } catch {
      // Not there — create it below.
    }

    try {
      mkdirSync(dirname(linkPath), { recursive: true });
      // 'junction' is ignored on POSIX and is the only Windows link type that
      // does not require Developer Mode or elevation. It applies to
      // directories; file links fall back to 'file'.
      const type = existsSync(targetAbs) && lstatSync(targetAbs).isDirectory()
        ? 'junction'
        : 'file';
      symlinkSync(entry.target, linkPath, type);
      result.created += 1;
    } catch {
      result.failed += 1;
    }
  }

  return result;
}

/** Conventional manifest location inside a runtime package. */
export function symlinkManifestPath(packageRoot) {
  return join(packageRoot, 'runtime-symlinks.json');
}
