// BGT-001 JSONL bridge. Python owns product assertions; Playwright owns
// Chromium, DOM execution, isolated profiles and failure diagnostics.
import { createRequire } from 'node:module';
import fs from 'node:fs/promises';
import path from 'node:path';
import readline from 'node:readline';

const runtime = process.env.BGT001_RUNTIME;
const profile = process.env.BGT001_PROFILE;
const artifacts = process.env.BGT001_ARTIFACTS;
const modules = process.env.BGT001_NODE_MODULES;
const packageName = process.env.BGT001_PLAYWRIGHT_PACKAGE;
if (!runtime || !profile || !artifacts || !modules || !packageName) {
  throw new Error('BGT-001 infrastructure failure: required runtime paths are not set');
}
const require = createRequire(import.meta.url);
const { chromium } = require(path.join(modules, packageName));
let context;
let page;
let tracing = false;
let failureSaved = false;
const consoleLines = [];
const networkLines = [];

function errorText(error) {
  return String(error?.stack || error);
}

function recordPageEvents(activePage) {
  activePage.on('console', message => consoleLines.push(message.type() + ' ' + message.text()));
  activePage.on('requestfailed', request => networkLines.push('FAILED ' + request.method() + ' ' + request.url()));
  activePage.on('response', response => networkLines.push(response.status() + ' ' + response.request().method() + ' ' + response.url()));
}

async function stopTrace(tracePath) {
  if (!context || !tracing) return;
  tracing = false;
  await context.tracing.stop(tracePath ? { path: tracePath } : undefined);
}

async function saveFailure(error) {
  if (failureSaved) return;
  failureSaved = true;
  await fs.mkdir(artifacts, { recursive: true });
  const writes = [
    fs.writeFile(path.join(artifacts, 'bridge-error.txt'), errorText(error)),
    fs.writeFile(path.join(artifacts, 'console.log'), consoleLines.join('\n') + '\n'),
    fs.writeFile(path.join(artifacts, 'network.log'), networkLines.join('\n') + '\n'),
  ];
  if (page) writes.push(page.screenshot({ path: path.join(artifacts, 'failure.png'), fullPage: true }));
  await Promise.allSettled(writes);
  try {
    await stopTrace(path.join(artifacts, 'trace.zip'));
  } catch (traceError) {
    await fs.writeFile(path.join(artifacts, 'trace-error.txt'), errorText(traceError));
  }
}

async function closeAll() {
  const cleanupErrors = [];
  try {
    // End page-owned fetch/SSE work before joining the in-process test server.
    await page?.close({ runBeforeUnload: false });
  } catch (error) {
    cleanupErrors.push(errorText(error));
  }
  try {
    await stopTrace();
  } catch (error) {
    cleanupErrors.push(errorText(error));
  }
  try {
    await context?.close();
  } catch (error) {
    cleanupErrors.push(errorText(error));
  } finally {
    context = undefined;
    page = undefined;
  }
  if (cleanupErrors.length) throw new Error('BGT-001 browser cleanup failed: ' + cleanupErrors.join('; '));
}

async function command({ method, params = {} }) {
  if (method === 'open') {
    await fs.mkdir(profile, { recursive: true });
    await fs.mkdir(artifacts, { recursive: true });
    context = await chromium.launchPersistentContext(profile, {
      headless: true,
      viewport: { width: 1440, height: 900 },
      recordVideo: { dir: artifacts },
    });
    await context.tracing.start({ screenshots: true, snapshots: true, sources: true });
    tracing = true;
    page = context.pages()[0] || await context.newPage();
    recordPageEvents(page);
    return null;
  }
  if (method === 'goto') return page.goto(params.url, { waitUntil: 'domcontentloaded' });
  if (method === 'viewport') return page.setViewportSize({ width: params.width, height: params.height || 900 });
  if (method === 'key') return page.keyboard.press(params.key);
  if (method === 'pointer') return page.mouse.move(params.x, params.y);
  if (method === 'pointerDown') return page.mouse.down();
  if (method === 'pointerUp') return page.mouse.up();
  if (method === 'click') return page.locator(params.selector).click({ timeout: params.timeoutMs });
  if (method === 'doubleClick') return page.locator(params.selector).dblclick({ timeout: params.timeoutMs });
  if (method === 'fill') return page.locator(params.selector).fill(params.value, { timeout: params.timeoutMs });
  if (method === 'evaluate') return page.evaluate(params.expression);
  if (method === 'wait') return page.waitForFunction(
    source => { try { return Boolean((0, eval)(source)); } catch { return false; } },
    params.expression,
    { timeout: params.timeoutMs },
  );
  if (method === 'close') {
    await closeAll();
    return null;
  }
  throw new Error('unknown bridge command: ' + method);
}

const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of input) {
  let request;
  try {
    request = JSON.parse(line);
    const result = await command(request);
    process.stdout.write(JSON.stringify({ id: request.id, ok: true, result }) + '\n');
  } catch (error) {
    await saveFailure(error);
    process.stdout.write(JSON.stringify({ id: request?.id, ok: false, error: errorText(error) }) + '\n');
  }
}
