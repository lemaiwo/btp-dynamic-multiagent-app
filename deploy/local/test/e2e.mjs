// Browser test of the local stack: Keycloak sign-in, web chat, admin, a chat
// that delegates to a specialist with ARC-1 tools, and a user without roles.
//
//   STACK_TEST_MODEL=1 ./stack.sh up
//   node test/e2e.mjs            (Playwright from ../../ui5-ide/node_modules)
//
// Passwords are read from secrets/logins.txt and never printed.
import { readFileSync, mkdirSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const require = createRequire(join(here, '../../../ui5-ide/package.json'));
const { chromium } = require('@playwright/test');

const BASE = process.env.STACK_URL || 'http://localhost:4180';
const shots = join(here, '../secrets/e2e');
mkdirSync(shots, { recursive: true });
const logins = readFileSync(join(here, '../secrets/logins.txt'), 'utf8');
const password = (user) => logins.match(new RegExp(`^\\s*${user}\\s*/\\s*(\\S+)`, 'm'))[1];

const results = [];
const check = (name, ok, detail = '') => {
  results.push(ok);
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  (' + detail + ')' : ''}`);
};

async function signIn(browser, user) {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto(`${BASE}/chat`);
  check(`${user}: unauthenticated /chat goes to Keycloak`, page.url().includes(':8180/realms/agents'));
  await page.fill('#username', user);
  await page.fill('#password', password(user));
  await page.click('#kc-login');
  await page.waitForURL(`${BASE}/chat`);
  check(`${user}: signed in, back on the web chat`, page.url() === `${BASE}/chat`);
  return { context, page };
}

const api = (page, method, path, body) =>
  page.evaluate(async ([method, path, body]) => {
    const response = await fetch(path, {
      method,
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
      body: body === null ? undefined : JSON.stringify(body),
    });
    let json = null;
    try { json = await response.json(); } catch { /* no body */ }
    return { status: response.status, json };
  }, [method, path, body ?? null]);

async function say(page, text, expect, label) {
  // A turn that just ended needs a moment before the composer sends again.
  await page.waitForTimeout(1500);
  const box = page.locator('textarea').first();
  await box.click();
  await box.fill(text);
  await box.press('Enter');
  try {
    await page.getByText(expect, { exact: false }).last().waitFor({ timeout: 90_000 });
    check(label, true);
  } catch {
    check(label, false, 'expected text did not appear');
  }
}

// The installed Chrome (headless, a temporary profile): no browser download needed.
const browser = await chromium.launch({ channel: process.env.E2E_CHANNEL || 'chrome' });
try {
  // --- developer (roles user, developer, admin) -----------------------------
  const dev = await signIn(browser, 'developer');
  const me = await api(dev.page, 'GET', '/chat/api/me');
  check('developer: /chat/api/me names a principal and advanced access',
    me.status === 200 && !!me.json.principal && me.json.developer === true);
  check('developer: the model list is the stack\'s', (me.json.models || []).length > 0,
    (me.json.models || []).map((m) => m.id).join(', '));

  const model = await api(dev.page, 'PUT', '/admin/api/model', { model_name: me.json.models[0].id });
  check('admin API: set the active model', model.status === 200, `status ${model.status}`);

  const agent = await api(dev.page, 'POST', '/admin/api/agents', {
    name: 'arc1-reader',
    description: 'Reads ABAP objects through the ARC-1 of this stack, as the signed-in user.',
    instructions: 'You answer questions about ABAP objects with the ARC-1 tools.',
    mcp_servers: [{ url: 'https://arc1.local/mcp', auth_mode: 'destination',
      oauth: { destination: 'ARC1', user_context: true } }],
    enabled: true,
    expose_chat: true,
  });
  check('admin API: create an agent on the ARC1 destination (user context)',
    [200, 201, 409].includes(agent.status), `status ${agent.status}`);
  const reload = await api(dev.page, 'POST', '/admin/api/reload', {});
  check('admin API: reload the registry', reload.status === 200, `status ${reload.status}`);

  await dev.page.goto(`${BASE}/admin`);
  await dev.page.locator('#agents-tbody').getByText('arc1-reader').first().waitFor({ timeout: 20_000 })
    .then(() => check('admin page: lists the new agent', true))
    .catch(() => check('admin page: lists the new agent', false));
  await dev.page.screenshot({ path: join(shots, 'admin.png') });

  await dev.page.goto(`${BASE}/chat`);
  await say(dev.page, 'Hello from the browser test', 'Test model reply',
    'web chat: a message is answered (streamed through the proxy)');
  await say(dev.page, 'Please ask the arc1-reader which tools it has', 'The specialist answered',
    'web chat: the orchestrator delegates to the specialist and its answer comes back');
  const body = await dev.page.locator('body').innerText();
  check('web chat: the specialist had ARC-1\'s tools (its MCP session ran as the signed-in user)',
    /SAPRead/.test(body));
  await dev.page.screenshot({ path: join(shots, 'chat.png'), fullPage: true });

  const classic = await dev.page.goto(`${BASE}/`);
  check('classic chat page loads', classic.status() === 200);
  await dev.context.close();

  // --- basic (role user only) ------------------------------------------------
  const basic = await signIn(browser, 'basic');
  const basicMe = await api(basic.page, 'GET', '/chat/api/me');
  check('basic: signed in without advanced access', basicMe.status === 200 && basicMe.json.developer === false);
  const refused = await api(basic.page, 'GET', '/admin/api/agents');
  check('basic: the admin API answers 403', refused.status === 403, `status ${refused.status}`);
  const write = await api(basic.page, 'PUT', '/admin/api/model', { model_name: 'x' });
  check('basic: an admin write answers 403', write.status === 403, `status ${write.status}`);
  await say(basic.page, 'Hello as the basic user', 'Test model reply', 'basic: can chat');
  const own = await api(basic.page, 'GET', '/chat/api/conversations');
  const titles = JSON.stringify(own.json);
  check('basic: sees only their own conversations', own.status === 200 && !titles.includes('arc1-reader'));
  await basic.context.close();
} finally {
  await browser.close();
}
const failed = results.filter((ok) => !ok).length;
console.log(`\n${results.length - failed} passed, ${failed} failed; screenshots in secrets/e2e/`);
process.exit(failed ? 1 : 0);
