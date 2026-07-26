#!/usr/bin/env node
/**
 * `omicsclaw` / `oc` entrypoint.
 *
 * A thin shell: find the platform runtime package, hand every argument to its
 * Python interpreter, and propagate the exit status. No logic of its own — the
 * CLI lives in `omicsclaw.surfaces.cli._main` on the Python side.
 *
 * ## Why `-c` and not the installed console script
 *
 * The runtime package ships a pip-installed `bin/omicsclaw` console script,
 * and calling it directly would be the obvious move. It does not survive
 * relocation: pip bakes an ABSOLUTE interpreter path into the shebang at
 * install time, which is the CI build directory, not the user's
 * `node_modules`. So we invoke the interpreter explicitly and import the
 * entrypoint ourselves — the one form that is position-independent.
 *
 * ## Why PYTHONSAFEPATH
 *
 * `python -c` prepends the working directory to `sys.path`, which a pip
 * console script does not do. Without this, a stray `omicsclaw.py` in the
 * user's working directory would shadow the real package and break the CLI in
 * a baffling way. `PYTHONSAFEPATH=1` suppresses that prepend. It is used
 * instead of the equivalent `-P` flag because an unknown *flag* is a hard error
 * on Python < 3.11 while an unknown *env var* is simply ignored — this stays
 * correct if a runtime is ever built against an older interpreter.
 */

import { spawnSync } from 'node:child_process';
import { constants } from 'node:os';
import { delimiter, join } from 'node:path';

import {
  currentTarget,
  missingRuntimeMessage,
  resolveRuntime,
  PYTHON_SUBDIR,
} from '../lib/resolve-runtime.mjs';

const BOOTSTRAP = [
  'import sys',
  "sys.argv[0] = 'omicsclaw'",
  'from omicsclaw.surfaces.cli.launcher import main',
  'sys.exit(main())',
].join('; ');

/**
 * Directories to prepend to PATH so that tools installed alongside the
 * interpreter (uvicorn, jupyter, pip) win over any same-named binary on the
 * user's PATH. Mirrors `runtimeBinDirsForPython` in OmicsClaw-App.
 */
function runtimeBinDirs(runtimeRoot, platform) {
  const pythonRoot = join(runtimeRoot, PYTHON_SUBDIR);
  return platform === 'win32'
    ? [pythonRoot, join(pythonRoot, 'Scripts')]
    : [join(pythonRoot, 'bin')];
}

function buildEnv(baseEnv, runtimeRoot, platform) {
  const dirs = runtimeBinDirs(runtimeRoot, platform);
  const existing = baseEnv.PATH ?? baseEnv.Path ?? '';
  return {
    ...baseEnv,
    PYTHONSAFEPATH: '1',
    PATH: existing ? [...dirs, existing].join(delimiter) : dirs.join(delimiter),
  };
}

function run(argv, platform = process.platform) {
  const target = currentTarget(platform, process.arch);
  const runtime = resolveRuntime({ target, platform });

  if (runtime === null) {
    process.stderr.write(`${missingRuntimeMessage(target)}\n`);
    return 1;
  }

  const result = spawnSync(runtime.pythonPath, ['-c', BOOTSTRAP, ...argv], {
    stdio: 'inherit',
    env: buildEnv(process.env, runtime.runtimeRoot, platform),
  });

  if (result.error) {
    process.stderr.write(
      `Failed to start the OmicsClaw runtime at ${runtime.pythonPath}\n` +
        `${result.error.message}\n`,
    );
    return 1;
  }

  // A signalled child reports a null status. Translate to the shell's 128+N
  // convention so `omicsclaw ...; echo $?` after Ctrl-C reads the way users
  // expect (130 for SIGINT, 143 for SIGTERM, ...).
  if (result.signal) {
    const signum = constants.signals[result.signal];
    return signum === undefined ? 1 : 128 + signum;
  }

  return result.status ?? 1;
}

process.exit(run(process.argv.slice(2)));
