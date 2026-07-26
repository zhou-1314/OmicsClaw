#!/usr/bin/env node
/**
 * Postinstall hook for the `omicsclaw` npm package.
 *
 * Two jobs, in this order:
 *
 * 1. **Publish the runtime descriptor.** Write `~/.omicsclaw/runtime.json` so
 *    OmicsClaw-App can discover this install without knowing anything about
 *    npm's global prefix layout. See `lib/runtime-descriptor.mjs` for the
 *    contract.
 *
 * 2. **Take over the console-script names.** OmicsClaw has been installable
 *    with `pip` / `pipx` / `uv tool` for a long time, and those install the
 *    same four commands — `omicsclaw`, `oc`, `omicsclaw-chat`, `oc-chat`.
 *    Whichever copy sits earlier on PATH wins, so a fresh npm install can be
 *    completely invisible. This hook renames the previous shim to
 *    `<name>-legacy` (preserving a fallback) and clears any duplicates.
 *
 * The migration logic is ported from Kimi Code's postinstall, which solved the
 * same Python-CLI-to-npm transition. Its hard rules are kept verbatim:
 *
 *   - **Global installs only.** `npx`, local project dependencies, and
 *     workspace bootstraps are silent no-ops.
 *   - **Never fail the install.** Every error is caught; this script always
 *     exits 0. A failed migration is a message, not a broken install.
 *   - **Never touch what we do not recognise.** A shim qualifies only when it
 *     realpath-resolves outside our package AND carries a Python entry-point
 *     marker. A user's own `oc` — the OpenShift client, say — is reported and
 *     left alone.
 *   - **Classify everything before changing anything.** The abort-or-proceed
 *     decision is made once against the whole detected set, so we never end up
 *     half-migrated with a misleading success notice.
 *   - **Only claim success when it is true.** Before touching a single file we
 *     verify that, with the actionable shims hypothetically gone, OUR shim is
 *     what the user's login shell would resolve. If anything else would win,
 *     we explain why and touch nothing.
 *
 * Each of the four names is migrated independently: `oc` being blocked by an
 * unrelated binary must not stop `omicsclaw` from being taken over.
 */

import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { buildDescriptor, writeDescriptor } from '../lib/runtime-descriptor.mjs';
import {
  currentTarget,
  missingRuntimeMessage,
  resolveRuntime,
  PYTHON_SUBDIR,
} from '../lib/resolve-runtime.mjs';
import { restoreSymlinks, symlinkManifestPath } from '../lib/restore-symlinks.mjs';
import {
  CONSOLE_SCRIPTS,
  detectPackageManager,
  findFirstResolvable,
  isGlobalInstall,
  ownPackageRoot,
  postinstallPaths,
} from './postinstall/reach.mjs';
import {
  classifyShim,
  deleteShim,
  detectLegacyShims,
  renameInPlace,
} from './postinstall/migrate.mjs';
import {
  logForeignInTheWay,
  logMigrationBlocked,
  logMigrationDone,
  logNotOnPath,
  logRuntimeUnavailable,
  notify,
} from './postinstall/ui.mjs';

/**
 * Ask the installed interpreter for the Python package version. Doubles as an
 * install-time smoke test: if the runtime cannot import `omicsclaw`, we learn
 * it here rather than the first time the user runs a command.
 *
 * Returns `undefined` on any failure — the descriptor treats the field as
 * optional precisely so a runtime that cannot execute on this host (a
 * cross-arch install, a hardened `noexec` mount) does not invalidate the rest.
 */
function probeOmicsclawVersion(pythonPath) {
  const result = spawnSync(
    pythonPath,
    ['-c', 'import omicsclaw; print(omicsclaw.__version__)'],
    { encoding: 'utf-8', timeout: 30_000, env: { ...process.env, PYTHONSAFEPATH: '1' } },
  );
  if (result.error || result.status !== 0) return undefined;
  const version = (result.stdout ?? '').trim();
  return version.length > 0 ? version : undefined;
}

function readOwnVersion(packageRoot) {
  try {
    return JSON.parse(readFileSync(join(packageRoot, 'package.json'), 'utf-8')).version;
  } catch {
    return '0.0.0';
  }
}

/**
 * Step 1: record where this install put its interpreter, for OmicsClaw-App.
 * Independent of the migration below — a runtime is usable through `npx` or an
 * absolute path even when the PATH takeover cannot proceed.
 */
function publishRuntimeDescriptor(ownRoot) {
  const target = currentTarget();
  const runtime = resolveRuntime({ target });
  if (runtime === null) {
    logRuntimeUnavailable(missingRuntimeMessage(target));
    return;
  }

  const descriptor = buildDescriptor({
    packageVersion: readOwnVersion(ownRoot),
    omicsclawVersion: probeOmicsclawVersion(runtime.pythonPath),
    target,
    pythonPath: runtime.pythonPath,
    prefix: join(runtime.runtimeRoot, PYTHON_SUBDIR),
    installedAt: new Date().toISOString(),
  });

  writeDescriptor(descriptor);
}

/**
 * Migrate one console-script name. Returns the per-name outcome; every
 * filesystem write is gated on the reachability check first.
 */
async function migrateOne(binName, ownRoot, paths) {
  const detections = await detectLegacyShims(binName, ownRoot, paths.detection);
  if (detections.length === 0) return { binName, kind: 'nothing-to-do' };

  // Classify the whole set with no writes, so the decision below sees
  // everything at once.
  const classifications = await Promise.all(
    detections.map((d) => classifyShim(d.shimPath, binName)),
  );
  const actionable = classifications.filter((c) => c.kind !== 'blocked');
  const blocked = classifications.filter((c) => c.kind === 'blocked');

  // With the actionable shims hypothetically gone, what actually wins?
  const blocker = await findFirstResolvable(
    binName,
    ownRoot,
    paths.reachability,
    actionable.map((c) => c.shimPath),
    classifications.map((c) => c.shimPath),
  );

  if (blocker.kind !== 'own') {
    return { binName, kind: blocker.kind, blocker, blocked, actionable };
  }

  // Execute. The first shim in PATH order that we can touch becomes the
  // `-legacy` fallback, preserving what the name used to mean. Every later one
  // is simply removed: a dormant duplicate helps nobody.
  const outcome = {
    binName,
    kind: 'migrated',
    renames: [],
    consolidates: [],
    skippedForeignTarget: [],
    deletes: [],
    blockedHarmless: blocked,
    errors: [],
  };
  let preservedFirst = false;

  for (const c of classifications) {
    if (c.kind === 'blocked') continue;

    if (!preservedFirst) {
      preservedFirst = true;
      if (c.kind === 'renameable') {
        const r = await renameInPlace(c.shimPath, c.target);
        if (r.success) outcome.renames.push(c);
        else outcome.errors.push({ ...c, ...r });
        continue;
      }
      const r = await deleteShim(c.shimPath);
      if (!r.success) {
        outcome.errors.push({ ...c, ...r });
      } else if (c.kind === 'consolidate') {
        outcome.consolidates.push(c);
      } else {
        outcome.skippedForeignTarget.push(c);
      }
      continue;
    }

    const r = await deleteShim(c.shimPath);
    if (r.success) outcome.deletes.push(c);
    else outcome.errors.push({ ...c, ...r });
  }

  return outcome;
}

async function main() {
  // Repair the runtime package before anything else, and regardless of install
  // scope: `npm pack` drops symlinks, so a freshly extracted runtime is missing
  // `python/bin/python3`, `python/lib/libpython3.11.so`, and about a thousand
  // terminfo aliases. This is not gated on `isGlobalInstall()` because a local
  // or `npx` install deserves an intact runtime just as much.
  const runtime = resolveRuntime({});
  if (runtime !== null) {
    restoreSymlinks(runtime.runtimeRoot, symlinkManifestPath(runtime.packageRoot));
  }

  // From here down is the PATH takeover, which only makes sense for a global
  // install. npx, local project dependencies, and workspace bootstraps no-op.
  if (!isGlobalInstall()) return;

  const ownRoot = await ownPackageRoot(import.meta.dirname);
  if (ownRoot === null) return;
  const pm = detectPackageManager();

  publishRuntimeDescriptor(ownRoot);

  // One shell probe shared by detection and reachability, so the two stay
  // consistent and `$SHELL -l` is not spawned twice.
  const paths = await postinstallPaths();

  const outcomes = [];
  for (const binName of CONSOLE_SCRIPTS) {
    outcomes.push(await migrateOne(binName, ownRoot, paths));
  }

  // A name that resolved to nothing at all means our global bin directory is
  // not on the user's PATH. Report that once, not four times — and only when
  // it is the whole story, since a single missing name alongside successful
  // ones would be about that name, not about PATH.
  const relevant = outcomes.filter((o) => o.kind !== 'nothing-to-do');
  if (relevant.length > 0 && relevant.every((o) => o.kind === 'none')) {
    logNotOnPath(pm);
    return;
  }

  for (const o of relevant) {
    if (o.kind === 'foreign') logForeignInTheWay(o.binName, o.blocker.path, pm);
    else if (o.kind === 'blocked-legacy') logMigrationBlocked(o.blocked, o.actionable, pm);
  }

  const migrated = relevant.filter((o) => o.kind === 'migrated');
  if (migrated.length > 0) {
    logMigrationDone(
      {
        renames: migrated.flatMap((o) => o.renames),
        consolidates: migrated.flatMap((o) => o.consolidates),
        skippedForeignTarget: migrated.flatMap((o) => o.skippedForeignTarget),
        deletes: migrated.flatMap((o) => o.deletes),
        blockedHarmless: migrated.flatMap((o) => o.blockedHarmless),
        errors: migrated.flatMap((o) => o.errors),
      },
      pm,
    );
  }
}

main().catch((err) => {
  const message = err instanceof Error ? err.message : String(err);
  notify(`[omicsclaw] postinstall warning: ${message}`);
});
