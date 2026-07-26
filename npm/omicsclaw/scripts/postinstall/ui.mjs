/**
 * User-facing output for the postinstall migration.
 *
 * Two things make this less trivial than `console.log`:
 *
 * 1. **Package managers swallow stdout.** npm and pnpm capture lifecycle-script
 *    output and only replay it on failure — but this script never fails by
 *    design, so a plain write can vanish. {@link notify} therefore writes to
 *    `/dev/tty` when one is available, which reaches the user's terminal
 *    regardless of how the manager has redirected the pipes, and falls back to
 *    stderr otherwise.
 *
 * 2. **Silence must mean "nothing happened".** Every renderer below
 *    corresponds to an outcome the user needs to act on or at least know
 *    about. The clean fresh-install path prints nothing at all.
 */

import { openSync, writeSync, closeSync } from 'node:fs';

const IS_WINDOWS = process.platform === 'win32';

const useColour =
  !process.env['NO_COLOR'] && (process.stderr.isTTY || process.env['FORCE_COLOR']);
const BOLD = useColour ? '\x1b[1m' : '';
const DIM = useColour ? '\x1b[2m' : '';
const YELLOW = useColour ? '\x1b[33m' : '';
const GREEN = useColour ? '\x1b[32m' : '';
const RESET = useColour ? '\x1b[0m' : '';

/**
 * Write a line where the user will actually see it. Never throws: a failure to
 * report is not a reason to fail an install.
 */
export function notify(line) {
  const text = `${line}\n`;
  if (!IS_WINDOWS) {
    let fd;
    try {
      fd = openSync('/dev/tty', 'w');
      writeSync(fd, text);
      return;
    } catch {
      // No controlling terminal (CI, a daemon, a container). Fall through.
    } finally {
      if (fd !== undefined) {
        try {
          closeSync(fd);
        } catch {
          /* ignore */
        }
      }
    }
  }
  try {
    process.stderr.write(text);
  } catch {
    /* reporting must never throw */
  }
}

function block(title, lines, accent = YELLOW) {
  notify('');
  notify(`${accent}${BOLD}${title}${RESET}`);
  for (const line of lines) notify(line);
  notify('');
}

/**
 * Summarise everything the migration actually did. Only reached once the
 * reachability gate has certified that our shim wins PATH resolution, so the
 * takeover claim here is known-true rather than hopeful.
 */
export function logMigrationDone(outcomes, pm) {
  const { renames, consolidates, skippedForeignTarget, deletes, blockedHarmless, errors } =
    outcomes;

  const touched =
    renames.length + consolidates.length + skippedForeignTarget.length + deletes.length;
  if (touched === 0 && errors.length === 0) return;

  const lines = [];

  for (const c of renames) {
    lines.push(`  ${c.shimPath}`);
    lines.push(`    ${DIM}renamed to${RESET} ${c.target}`);
  }
  for (const c of consolidates) {
    lines.push(`  ${c.shimPath}`);
    lines.push(`    ${DIM}removed; ${c.target} already holds the previous CLI${RESET}`);
  }
  for (const c of skippedForeignTarget) {
    lines.push(`  ${c.shimPath}`);
    lines.push(
      `    ${DIM}removed, but ${c.target} is yours so the old CLI was not preserved${RESET}`,
    );
  }
  for (const c of deletes) {
    lines.push(`  ${c.shimPath}`);
    lines.push(`    ${DIM}removed (duplicate of an earlier entry on PATH)${RESET}`);
  }

  if (errors.length > 0) {
    lines.push('');
    lines.push(`${YELLOW}Some entries could not be changed:${RESET}`);
    for (const e of errors) {
      lines.push(`  ${e.shimPath} — ${e.message}`);
    }
  }

  if (blockedHarmless.length > 0) {
    lines.push('');
    lines.push(
      `${DIM}Left alone (not writable, and not shadowing the new CLI):${RESET}`,
    );
    for (const b of blockedHarmless) lines.push(`  ${DIM}${b.shimPath}${RESET}`);
  }

  lines.push('');
  lines.push(`${GREEN}omicsclaw and oc now run the npm-installed OmicsClaw.${RESET}`);
  if (renames.length > 0 || consolidates.length > 0) {
    const names = [...new Set([...renames, ...consolidates].map((c) => c.binName))];
    lines.push(
      `${DIM}The previous CLI is still available as ${names
        .map((n) => `${n}-legacy`)
        .join(', ')}.${RESET}`,
    );
  }

  block('OmicsClaw: migrated the previously installed CLI', lines);
}

/**
 * A legacy shim we cannot modify still wins PATH resolution. Nothing was
 * touched — tell the user exactly which file needs escalated removal.
 */
export function logMigrationBlocked(blocked, actionable, pm) {
  const lines = [
    'A previously installed OmicsClaw command is earlier on your PATH and',
    'could not be changed, so it would still shadow the npm install.',
    '',
    `${BOLD}Nothing was modified.${RESET}`,
    '',
    'Blocked:',
  ];

  for (const b of blocked) {
    lines.push(`  ${b.shimPath}`);
  }

  lines.push('');
  lines.push('Remove or rename it, then reinstall:');
  lines.push('');
  if (blocked.some((b) => b.isSystemPath)) {
    lines.push(
      IS_WINDOWS
        ? '  (from an Administrator PowerShell)'
        : `  sudo mv ${blocked[0].shimPath} ${blocked[0].target}`,
    );
  } else {
    lines.push(`  mv ${blocked[0].shimPath} ${blocked[0].target}`);
  }
  lines.push('');
  lines.push(`  ${pmGlobalInstallHint(pm)}`);

  if (actionable.length > 0) {
    lines.push('');
    lines.push(
      `${DIM}${actionable.length} other entr${
        actionable.length === 1 ? 'y was' : 'ies were'
      } left untouched too, since removing them alone would not help.${RESET}`,
    );
  }

  block('OmicsClaw: could not take over the command name', lines);
}

/**
 * Something we do not recognise owns the name and wins PATH resolution. This
 * is the branch that fires when a user has, say, the OpenShift client
 * installed as `oc`. It is their file and their decision — we only report.
 */
export function logForeignInTheWay(binName, foreignPath, pm) {
  block(`OmicsClaw: "${binName}" is already taken`, [
    `Another program earlier on your PATH owns ${BOLD}${binName}${RESET}:`,
    '',
    `  ${foreignPath}`,
    '',
    'It is not an OmicsClaw install, so it was left completely alone.',
    '',
    `Until it is moved or renamed, typing ${BOLD}${binName}${RESET} will keep running it`,
    'rather than OmicsClaw.',
    '',
    binName === 'oc'
      ? `${DIM}If that is the OpenShift client, prefer the "omicsclaw" command instead.${RESET}`
      : `${DIM}Use the full "omicsclaw" command, or rename the other program.${RESET}`,
  ]);
}

/**
 * Our own shim is not on the PATH the user's login shell sees, so the global
 * bin directory is not wired up. Migrating would make things worse, not
 * better, so nothing was touched.
 */
export function logNotOnPath(pm) {
  block('OmicsClaw: installed, but not on your PATH', [
    "The global bin directory is not on your login shell's PATH, so typing",
    `${BOLD}omicsclaw${RESET} will not find it yet.`,
    '',
    `${BOLD}Nothing was modified.${RESET}`,
    '',
    'Find the directory:',
    '',
    `  ${pmGlobalBinHint(pm)}`,
    '',
    'Add it to PATH in your shell profile, open a new terminal, then rerun',
    'the install so the migration can finish.',
  ]);
}

/** The runtime probe could not confirm a working interpreter. */
export function logRuntimeUnavailable(message) {
  block('OmicsClaw: runtime not ready', [
    message,
    '',
    `${DIM}The CLI will report the same problem when you run it.${RESET}`,
  ]);
}

function pmGlobalBinHint(pm) {
  switch (pm) {
    case 'pnpm':
      return 'pnpm bin -g';
    case 'yarn':
      return 'yarn global bin';
    default:
      return 'npm prefix -g';
  }
}

function pmGlobalInstallHint(pm) {
  switch (pm) {
    case 'pnpm':
      return 'pnpm add -g omicsclaw';
    case 'yarn':
      return 'yarn global add omicsclaw';
    default:
      return 'npm install -g omicsclaw';
  }
}
