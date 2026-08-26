/**
 * Guards the UI5 admin app against running framework JS and framework CSS from
 * two different SAPUI5 versions.
 *
 * This is not hypothetical. On 2026-08-26 the deployed admin at
 * /cominfrabelagentadmin/index.html rendered its side navigation as a bare
 * bulleted <ul>, because:
 *
 *   - `ui5.yaml` bundled the framework INTO the app (`resolve: true`), so the
 *     controls were SAPUI5 1.120.20 and emitted `.sapTntNavLI` class names; and
 *   - `ui5-admin/xs-app.json` served /resources/ from the UNVERSIONED
 *     https://ui5.sap.com/resources/, which by then had rolled to 1.151.0,
 *     whose sap.tnt stylesheet only knows the renamed `.sapTntNL` classes.
 *
 * Every SideNavigation rule missed, so the list fell back to browser defaults.
 * sap.m Dialogs clipped their first form row for the same reason.
 *
 * The two failures below are the two halves of that mismatch. Either one alone
 * is enough to reintroduce it, so both are asserted.
 *
 * Run: node tests/test_ui5_asset_versioning.mjs
 */
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');

let failed = 0;
const check = (label, cond, detail = '') => {
  if (cond) { console.log(`  PASS  ${label}`); }
  else { failed++; console.log(`  FAIL  ${label}   ${detail}`); }
};

/** Pull the target of the route whose `source` regex matches a resources path. */
function resourcesTarget(xsApp) {
  const route = (xsApp.routes || []).find(
    (r) => typeof r.source === 'string' && /resources/.test(r.source)
  );
  return route ? route.target : null;
}

const PIN = /\/(\d+\.\d+\.\d+)\/resources\//;

console.log('\n== UI5 asset versioning ==\n');

const appRoutes = JSON.parse(readFileSync(join(root, 'ui5-admin/xs-app.json'), 'utf8'));
const approuterRoutes = JSON.parse(readFileSync(join(root, 'approuter/xs-app.json'), 'utf8'));

const appTarget = resourcesTarget(appRoutes);
const approuterTarget = resourcesTarget(approuterRoutes);

check('the app has a /resources/ route at all', !!appTarget, String(appTarget));
check('the approuter has a /resources/ route at all', !!approuterTarget, String(approuterTarget));

// The app-repo entry point (/cominfrabelagentadmin/...) uses the app's OWN
// xs-app.json, not the approuter's. An unpinned target there floats to whatever
// SAPUI5 ships today, independently of what the app was built and tested against.
const appPin = PIN.exec(appTarget || '');
check('the app pins a SAPUI5 version for /resources/', !!appPin,
      `target is ${appTarget!==null?JSON.stringify(appTarget):'missing'} — unversioned means "whatever SAP ships today"`);

const approuterPin = PIN.exec(approuterTarget || '');
check('the approuter pins a SAPUI5 version for /resources/', !!approuterPin,
      `target is ${JSON.stringify(approuterTarget)}`);

// Two entry points reach the same app. If they disagree on the version, the bug
// reproduces on one URL and not the other, which is exactly what made it so
// confusing to diagnose the first time.
check('both entry points pin the SAME version',
      !!appPin && !!approuterPin && appPin[1] === approuterPin[1],
      `app=${appPin ? appPin[1] : 'none'} approuter=${approuterPin ? approuterPin[1] : 'none'}`);

console.log('');

// `resolve: true` makes `ui5 build` inline the framework's own modules into
// Component-preload.js. The app then runs bundled controls of the BUILD-time
// version against stylesheets of the SERVE-time version.
const ui5Yaml = readFileSync(join(root, 'ui5-admin/ui5.yaml'), 'utf8');
const bundlesBlock = ui5Yaml.slice(ui5Yaml.indexOf('bundles:'));
check('the Component-preload bundle does not resolve framework dependencies',
      !/^\s*resolve:\s*true\s*$/m.test(bundlesBlock),
      "ui5.yaml sets `resolve: true`, which bundles SAPUI5's own modules into the app");

// The behavioural half: assert against the artifact that actually ships.
const preload = join(root, 'ui5-admin/dist/Component-preload.js');
if (existsSync(preload)) {
  const text = readFileSync(preload, 'utf8');
  const mods = [...text.matchAll(/"((?:sap|com)\/[^"]+?\.js)":/g)].map((m) => m[1]);
  const framework = mods.filter((m) => m.startsWith('sap/'));
  check('the built preload bundles no framework modules', framework.length === 0,
        `${framework.length} of ${mods.length} bundled modules are SAPUI5's own ` +
        `(e.g. ${framework.slice(0, 3).join(', ') || 'n/a'})`);
  check('the built preload still bundles the app itself',
        mods.some((m) => m.startsWith('com/infrabel/agentadmin/')),
        'no app modules found — the bundle filter is wrong');
} else {
  console.log('  ....  build output absent; run `npm run build:ui5` in ui5-admin/ to cover it');
}

console.log(failed ? `\n${failed} check(s) FAILED\n` : '\nAll checks passed\n');
process.exit(failed ? 1 : 0);
