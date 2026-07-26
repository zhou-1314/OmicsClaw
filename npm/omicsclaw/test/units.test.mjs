/**
 * Unit tests for the pure parts of the wrapper.
 *
 * Everything here runs on stdlib `node:test` with no dependencies, because the
 * package itself must stay dependency-free — it executes inside
 * `npm install`, where having our own dependencies would be a bootstrapping
 * problem.
 *
 * The impure parts (PATH walking, shim renaming, the shell probe) are not
 * covered here; they are verified by installing the package into a sandboxed
 * npm prefix with a controlled PATH.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, symlinkSync, writeFileSync, lstatSync, readlinkSync }
  from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import {
  SUPPORTED_TARGETS,
  currentTarget,
  isSupportedTarget,
  missingRuntimeMessage,
  pythonBinaryPath,
  resolveRuntime,
  runtimePackageName,
} from '../lib/resolve-runtime.mjs';
import {
  DESCRIPTOR_SCHEMA_VERSION,
  buildDescriptor,
  isDescriptorLive,
  parseDescriptor,
} from '../lib/runtime-descriptor.mjs';
import { restoreSymlinks } from '../lib/restore-symlinks.mjs';

// ---------------------------------------------------------------------------
// resolve-runtime
// ---------------------------------------------------------------------------

test('currentTarget uses the same spelling as npm os/cpu filtering', () => {
  assert.equal(currentTarget('linux', 'x64'), 'linux-x64');
  assert.equal(currentTarget('darwin', 'arm64'), 'darwin-arm64');
  assert.equal(currentTarget('win32', 'x64'), 'win32-x64');
});

test('the supported-target list matches what actually gets built', () => {
  // darwin-x64 and win32-arm64 are absent on purpose: llvmlite stopped
  // publishing macOS x86_64 wheels, and there is no native Windows arm64
  // runner. If this list grows, the optionalDependencies in package.json and
  // NPM_TARGETS in build-runtime-package.mjs must grow with it.
  assert.deepEqual([...SUPPORTED_TARGETS].sort(), [
    'darwin-arm64',
    'linux-arm64',
    'linux-x64',
    'win32-x64',
  ]);
  assert.equal(isSupportedTarget('darwin-x64'), false);
  assert.equal(isSupportedTarget('win32-arm64'), false);
});

test('runtimePackageName is scoped per target', () => {
  assert.equal(runtimePackageName('linux-x64'), '@omicsclaw/runtime-linux-x64');
});

test('pythonBinaryPath follows the PBS layout on each platform', () => {
  // PBS install_only puts python.exe at the root of python/ on Windows, and
  // python/bin/python3 on Unix — with no unversioned `python` symlink, so the
  // `3` suffix is load-bearing.
  assert.equal(pythonBinaryPath('/r', 'linux'), join('/r', 'python', 'bin', 'python3'));
  assert.equal(pythonBinaryPath('/r', 'darwin'), join('/r', 'python', 'bin', 'python3'));
  assert.equal(pythonBinaryPath('/r', 'win32'), join('/r', 'python', 'python.exe'));
});

test('resolveRuntime returns null for an unsupported target without touching the resolver', () => {
  assert.equal(resolveRuntime({ target: 'darwin-x64' }), null);
  assert.equal(resolveRuntime({ target: 'sunos-sparc' }), null);
});

test('resolveRuntime returns null when the platform package is not installed', () => {
  // The wrapper is installed but the optional dependency was skipped — an
  // ordinary state (`--omit=optional`, an unsupported host), not an error.
  assert.equal(resolveRuntime({ target: 'linux-arm64', platform: 'linux' }), null);
});

test('missingRuntimeMessage distinguishes unsupported host from skipped optional dep', () => {
  const unsupported = missingRuntimeMessage('darwin-x64');
  assert.match(unsupported, /does not ship a prebuilt runtime/);
  assert.match(unsupported, /0_setup_env\.sh/); // points at the source install

  const skipped = missingRuntimeMessage('linux-x64');
  assert.match(skipped, /not installed/);
  assert.match(skipped, /--omit=optional/); // points at the likely cause
});

// ---------------------------------------------------------------------------
// runtime-descriptor
// ---------------------------------------------------------------------------

function sampleDescriptor(overrides = {}) {
  return {
    ...buildDescriptor({
      packageVersion: '0.1.2',
      omicsclawVersion: '0.1.2',
      target: 'linux-x64',
      pythonPath: '/opt/rt/python/bin/python3.11',
      prefix: '/opt/rt/python',
      installedAt: '2026-07-26T00:00:00.000Z',
    }),
    ...overrides,
  };
}

test('buildDescriptor stamps the schema version and npm source', () => {
  const d = sampleDescriptor();
  assert.equal(d.schemaVersion, DESCRIPTOR_SCHEMA_VERSION);
  assert.equal(d.source, 'npm');
});

test('parseDescriptor accepts a well-formed descriptor and rejects a bad one', () => {
  assert.notEqual(parseDescriptor(sampleDescriptor()), null);

  // A schema version we do not understand is treated as absent, never as
  // partially usable — that is what lets a future writer change field meanings.
  assert.equal(parseDescriptor(sampleDescriptor({ schemaVersion: 999 })), null);
  assert.equal(parseDescriptor(sampleDescriptor({ source: 'pip' })), null);
  assert.equal(parseDescriptor(sampleDescriptor({ pythonPath: '' })), null);
  assert.equal(parseDescriptor(null), null);
  assert.equal(parseDescriptor([]), null);
  assert.equal(parseDescriptor('nope'), null);
});

test('parseDescriptor treats omicsclawVersion as the one optional field', () => {
  const withoutVersion = sampleDescriptor();
  delete withoutVersion.omicsclawVersion;
  assert.notEqual(parseDescriptor(withoutVersion), null);

  assert.equal(parseDescriptor(sampleDescriptor({ omicsclawVersion: 42 })), null);
});

test('isDescriptorLive is the staleness guard, not a formality', () => {
  // npm cannot clean this file up on global uninstall, so readers MUST verify
  // the interpreter still exists.
  const d = sampleDescriptor();
  assert.equal(isDescriptorLive(d, () => true), true);
  assert.equal(isDescriptorLive(d, () => false), false);
  assert.equal(isDescriptorLive(null, () => true), false);
});

// ---------------------------------------------------------------------------
// restore-symlinks
// ---------------------------------------------------------------------------

test('restoreSymlinks recreates links npm pack dropped, and is idempotent', () => {
  const root = mkdtempSync(join(tmpdir(), 'omicsclaw-symlink-'));
  mkdirSync(join(root, 'python', 'bin'), { recursive: true });
  writeFileSync(join(root, 'python', 'bin', 'python3.11'), '#!/bin/true\n');

  const manifestPath = join(root, 'runtime-symlinks.json');
  writeFileSync(
    manifestPath,
    JSON.stringify({
      version: 1,
      links: [
        { path: 'python/bin/python3', target: 'python3.11' },
        { path: 'python/bin/python', target: 'python3.11' },
      ],
    }),
  );

  const first = restoreSymlinks(root, manifestPath);
  assert.equal(first.created, 2);
  assert.equal(readlinkSync(join(root, 'python', 'bin', 'python3')), 'python3.11');

  // Re-running an install must not churn the tree.
  const second = restoreSymlinks(root, manifestPath);
  assert.equal(second.created, 0);
  assert.equal(second.existing, 2);
});

test('restoreSymlinks refuses entries that would escape the runtime directory', () => {
  // The manifest travelled with a downloaded package, so it is untrusted: a
  // corrupted or malicious one must not be able to plant links anywhere the
  // installing user can write.
  const root = mkdtempSync(join(tmpdir(), 'omicsclaw-escape-'));
  const manifestPath = join(root, 'runtime-symlinks.json');
  writeFileSync(
    manifestPath,
    JSON.stringify({
      version: 1,
      links: [
        { path: '../evil', target: 'python3.11' },
        { path: 'ok', target: '../../../../etc/passwd' },
        { path: 'abs', target: '/etc/passwd' },
      ],
    }),
  );

  const result = restoreSymlinks(root, manifestPath);
  assert.equal(result.created, 0);
  assert.equal(result.skipped, 3);
  assert.throws(() => lstatSync(join(root, 'ok')));
});

test('restoreSymlinks tolerates a missing or malformed manifest', () => {
  const root = mkdtempSync(join(tmpdir(), 'omicsclaw-nomanifest-'));
  assert.deepEqual(restoreSymlinks(root, join(root, 'absent.json')), {
    created: 0,
    existing: 0,
    skipped: 0,
    failed: 0,
  });

  const bad = join(root, 'bad.json');
  writeFileSync(bad, '{ not json');
  assert.equal(restoreSymlinks(root, bad).created, 0);
});
