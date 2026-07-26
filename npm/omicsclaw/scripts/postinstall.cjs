/**
 * CommonJS entry point for the postinstall hook.
 *
 * The real work is in `postinstall-main.mjs`. This shim exists for one reason:
 * **a postinstall script must never be able to fail the install**, and an ESM
 * entry point can fail in a way no `try` / `catch` inside it can reach.
 *
 * On Node < 12 the `import ... from` syntax is a *parse* error, so the script
 * dies before its first statement runs, npm reports `ELIFECYCLE`, and the whole
 * `npm install -g omicsclaw` fails — even though the package itself installed
 * fine and the migration is entirely optional. The `engines` field does not
 * save us: npm only *warns* on an engine mismatch unless the user has opted
 * into `engine-strict`, which nobody does. Observed for real on npm 6.14.4 /
 * Node 10.19.0.
 *
 * So: a `.cjs` file (parsed as CommonJS by every Node that has ever shipped,
 * including versions predating the `.cjs` extension, which fall back to the
 * `.js` loader), an explicit version gate, and a dynamic `import()` built
 * through `new Function` so the expression is never *parsed* by a runtime that
 * would reject it.
 *
 * The CLI itself does require a modern Node, and says so through `engines` and
 * through `bin/omicsclaw.mjs`. This shim only ensures the failure mode is a
 * clear message rather than a broken install.
 */

'use strict';

const MINIMUM_NODE_MAJOR = 18;

function nodeMajor() {
  const raw = String(process.versions && process.versions.node).split('.')[0];
  const parsed = Number.parseInt(raw, 10);
  return Number.isNaN(parsed) ? 0 : parsed;
}

if (nodeMajor() < MINIMUM_NODE_MAJOR) {
  process.stderr.write(
    'omicsclaw: Node ' +
      process.versions.node +
      ' is too old (need >= ' +
      MINIMUM_NODE_MAJOR +
      ').\n' +
      'The package installed, but the "omicsclaw" command will not run until\n' +
      'you upgrade Node. Nothing was changed on your PATH.\n',
  );
  process.exit(0);
}

// `new Function` keeps the dynamic-import expression out of this file's parse
// tree. Node 10 would reject `import()` at parse time even inside a branch that
// never executes, which would defeat the entire point of the gate above.
const dynamicImport = new Function('specifier', 'return import(specifier)');

dynamicImport('./postinstall-main.mjs').catch(function (err) {
  const message = err && err.message ? err.message : String(err);
  process.stderr.write('omicsclaw: postinstall skipped (' + message + ')\n');
  // Exit 0 regardless: a migration that could not run is a message, never a
  // failed install.
  process.exit(0);
});
