/**
 * The `~/.omicsclaw/runtime.json` contract between this npm package and
 * OmicsClaw-App.
 *
 * The desktop app needs to answer "did the user install OmicsClaw through npm,
 * and if so which interpreter is it?" without shelling out to `which` and
 * without knowing anything about npm's global prefix layout. So the postinstall
 * hook drops a small descriptor at a fixed location and the app reads it as one
 * more rung on the ladder in `OmicsClaw-App/src/lib/python-runtime.ts`.
 *
 * ## The descriptor can be stale, and readers must assume it is
 *
 * npm has no reliable uninstall hook for global packages — `preuninstall` does
 * not fire for `npm uninstall -g` — so this file OUTLIVES the runtime it points
 * at. Every reader MUST re-check that `pythonPath` still exists before trusting
 * the entry, and fall through to the next resolution rung when it does not.
 * `isDescriptorLive()` is that check; do not skip it.
 *
 * ## Location is deliberately not OMICSCLAW_DIR
 *
 * `omicsclaw.common.workspace.resolve_omicsclaw_dir` honours an `OMICSCLAW_DIR`
 * override, because a *workspace* is a per-project thing. An npm install is a
 * per-machine thing, so the descriptor always lives under the real home
 * directory. Pointing OMICSCLAW_DIR somewhere else must not hide it.
 */

import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';

export const DESCRIPTOR_SCHEMA_VERSION = 1;
export const DESCRIPTOR_DIRNAME = '.omicsclaw';
export const DESCRIPTOR_FILENAME = 'runtime.json';

export function descriptorPath(home = homedir()) {
  return join(home, DESCRIPTOR_DIRNAME, DESCRIPTOR_FILENAME);
}

/**
 * Build a descriptor. Pure — `installedAt` is injected so the result is
 * reproducible under test.
 */
export function buildDescriptor({
  packageVersion,
  omicsclawVersion,
  target,
  pythonPath,
  prefix,
  installedAt,
}) {
  return {
    schemaVersion: DESCRIPTOR_SCHEMA_VERSION,
    source: 'npm',
    packageVersion,
    omicsclawVersion,
    target,
    pythonPath,
    prefix,
    installedAt,
  };
}

/**
 * Validate a parsed descriptor's shape. Returns the descriptor, or `null` when
 * it is malformed or from a schema version this reader does not understand.
 *
 * Kept structural rather than schema-library-driven so this package stays
 * dependency-free — it runs inside `npm install`, where pulling dependencies
 * of our own would be a bootstrapping problem.
 */
export function parseDescriptor(raw) {
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) return null;
  if (raw.schemaVersion !== DESCRIPTOR_SCHEMA_VERSION) return null;
  if (raw.source !== 'npm') return null;

  const required = ['packageVersion', 'target', 'pythonPath', 'prefix', 'installedAt'];
  for (const key of required) {
    if (typeof raw[key] !== 'string' || raw[key].length === 0) return null;
  }
  // `omicsclawVersion` is optional: the postinstall probe that reads it from
  // the interpreter can legitimately fail (a runtime that cannot execute on
  // this host, say) without invalidating everything else in the descriptor.
  if (raw.omicsclawVersion !== undefined && typeof raw.omicsclawVersion !== 'string') {
    return null;
  }
  return raw;
}

/**
 * Whether a descriptor still points at something real. See the staleness note
 * at the top of this file — this is not an optional refinement.
 */
export function isDescriptorLive(descriptor, fileExists = existsSync) {
  if (descriptor === null) return false;
  return fileExists(descriptor.pythonPath);
}

export function readDescriptor(path = descriptorPath(), readFile = readFileSync) {
  let text;
  try {
    text = readFile(path, 'utf-8');
  } catch {
    return null;
  }
  try {
    return parseDescriptor(JSON.parse(text));
  } catch {
    return null;
  }
}

export function writeDescriptor(descriptor, path = descriptorPath()) {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, `${JSON.stringify(descriptor, null, 2)}\n`, 'utf-8');
}
