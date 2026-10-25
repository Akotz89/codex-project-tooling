#!/usr/bin/env node
'use strict';

const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

function validateUrl(value) {
  let url;
  try { url = new URL(value); } catch { throw new Error('invalid status URL'); }
  const port = Number(url.port);
  if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !url.port || !Number.isInteger(port) || port < 1 || port > 65535
      || url.pathname !== '/' || url.username || url.password || url.search || url.hash)
    throw new Error('status URL must be an accepted loopback root URL');
  return url.href;
}

function validateDevToolsUrl(value, port, kind, targetId) {
  let url;
  try { url = new URL(value); } catch { throw new Error('invalid DevTools URL'); }
  const endpoint = kind === 'browser' ? /^\/devtools\/browser\/[A-Za-z0-9_-]+$/
    : new RegExp(`^/devtools/page/${targetId.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`);
  if (url.protocol !== 'ws:' || url.hostname !== '127.0.0.1' || url.port !== String(port)
      || url.username || url.password || url.search || url.hash || !endpoint.test(url.pathname))
    throw new Error('DevTools URL is outside the selected loopback endpoint');
  return url.href;
}

function normalizeProfile(value) {
  return path.win32.normalize(value.replaceAll('/', '\\')).toLowerCase();
}

function matchesProfile(commandLine, profile) {
  if (!Array.isArray(commandLine) || !path.win32.isAbsolute(profile)) return false;
  const arg = commandLine.find(value => value.toLowerCase().startsWith('--user-data-dir='));
  return !!arg && normalizeProfile(arg.slice('--user-data-dir='.length)) === normalizeProfile(profile);
}

function matchesProfileText(commandLine, profile) {
  const match = commandLine.match(/--user-data-dir=(?:"([^"]+)"|([^\s]+))/i);
  return !!match && matchesProfile([`--user-data-dir=${match[1] || match[2]}`], profile);
}

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i += 2) {
    const key = argv[i];
    if (!['--url', '--profile', '--chrome', '--debug-port'].includes(key) || !argv[i + 1] || argv[i + 1].startsWith('--'))
      throw new Error('invalid arguments');
    args[key.slice(2)] = argv[i + 1];
  }
  if (!args.url || !args.profile || !args.chrome || !path.win32.isAbsolute(args.profile))
    throw new Error('url, absolute profile path and chrome are required');
  args.url = validateUrl(args.url);
  args.debugPort = Number(args['debug-port'] || 9333);
  if (!Number.isInteger(args.debugPort) || args.debugPort < 1 || args.debugPort > 65535) throw new Error('invalid debug port');
  return args;
}

function readToken(input = process.stdin) {
  if (input.isTTY) return Promise.reject(new Error('token stdin must be piped'));
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    input.on('data', chunk => {
      size += chunk.length;
      if (size > 4096) { reject(new Error('token input too large')); input.destroy(); return; }
      chunks.push(chunk);
    });
    input.on('error', () => reject(new Error('token input failed')));
    input.on('end', () => {
      const token = Buffer.concat(chunks).toString('utf8').trim();
      if (Buffer.byteLength(token) < 32 || /[^\x21-\x7e]/.test(token)) reject(new Error('invalid token input'));
      else resolve(token);
    });
  });
}

function coordinatorResponseMatches(status, contentType, body) {
  if (status !== 401 || typeof contentType !== 'string' || contentType.split(';', 1)[0].trim().toLowerCase() !== 'application/json') return false;
  try {
    const result = JSON.parse(body);
    return Object.keys(result).length === 1 && result.error === 'unauthorized or unknown route';
  } catch { return false; }
}

async function preflightCoordinator(url, request = fetch) {
  const endpoint = new URL('api/status?offset=0&limit=100&report_offset=0&report_limit=100', url).href;
  const response = await request(endpoint, {
    method: 'GET', credentials: 'omit', headers: {Accept: 'application/json'},
    cache: 'no-store', redirect: 'error', signal: AbortSignal.timeout(3000),
  });
  if (!coordinatorResponseMatches(response.status, response.headers.get('content-type'), await response.text()))
    throw new Error('coordinator preflight failed');
}

function getJson(port, endpoint) {
  return fetch(`http://127.0.0.1:${port}/${endpoint}`, { signal: AbortSignal.timeout(1000) })
    .then(response => response.ok ? response.json() : Promise.reject(new Error('debug endpoint unavailable')));
}

function validateTarget(target, expectedUrl, port) {
  if (!target || target.type !== 'page' || target.url !== expectedUrl || !/^[A-Za-z0-9_-]+$/.test(target.id || ''))
    throw new Error('browser page target does not match caller URL');
  target.webSocketDebuggerUrl = validateDevToolsUrl(target.webSocketDebuggerUrl, port, 'page', target.id);
  return target;
}

function browserConnection(port) {
  return getJson(port, 'json/version').then(info => ({
    info, url: validateDevToolsUrl(info.webSocketDebuggerUrl, port, 'browser'),
  }));
}

async function findTarget(url, port, attempts = 1) {
  for (let i = 0; i < attempts; i++) {
    try {
      const targets = await getJson(port, 'json/list');
      const target = targets.find(item => item.type === 'page' && item.url === url);
      if (target) return validateTarget(target, url, port);
    } catch {}
    if (i + 1 < attempts) await new Promise(resolve => setTimeout(resolve, 250));
  }
  return null;
}

async function ensureTarget(browser, url, port, options = {}) {
  const find = options.findTarget || findTarget;
  const create = options.createTarget || (value => call(browser, 'Target.createTarget', {url: value}));
  const list = options.listTargets || (() => getJson(port, 'json/list'));
  const sleep = options.sleep || (ms => new Promise(resolve => setTimeout(resolve, ms)));
  const existing = await find(url, port);
  if (existing) return validateTarget(existing, url, port);

  const result = await create(url);
  const id = result?.targetId;
  if (typeof id !== 'string' || !/^[A-Za-z0-9_-]+$/.test(id)) throw new Error('new status page target unavailable');
  for (let i = 0; i < 40; i++) {
    const targets = await list().catch(() => []);
    const target = targets.find(item => item.id === id);
    if (target?.url === url) return validateTarget(target, url, port);
    if (target && target.url !== 'about:blank' && target.url !== url) throw new Error('new status page target URL mismatch');
    await sleep(100);
  }
  throw new Error('new status page target timed out');
}

function call(socket, method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = ++socket.sequence;
    const timer = setTimeout(() => { socket.pending.delete(id); reject(new Error('DevTools response timed out')); }, 10000);
    socket.pending.set(id, { resolve, reject, timer });
    socket.ws.send(JSON.stringify({ id, method, params }));
  });
}

function connect(url) {
  const ws = new WebSocket(url);
  const socket = { ws, sequence: 0, pending: new Map() };
  ws.onmessage = event => {
    const message = JSON.parse(event.data);
    const pending = socket.pending.get(message.id);
    if (!pending) return;
    socket.pending.delete(message.id);
    clearTimeout(pending.timer);
    if (message.error) pending.reject(new Error('DevTools command failed'));
    else pending.resolve(message.result);
  };
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { ws.close(); reject(new Error('DevTools connection timed out')); }, 10000);
    ws.onopen = () => { clearTimeout(timer); resolve(socket); };
    ws.onerror = () => { clearTimeout(timer); reject(new Error('DevTools connection failed')); };
  });
}

async function evaluate(target, expression, port) {
  const url = validateDevToolsUrl(target.webSocketDebuggerUrl, port, 'page', target.id);
  const socket = await connect(url);
  try {
    const result = await call(socket, 'Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
    if (result.exceptionDetails) throw new Error('browser request failed');
    return result.result.value;
  } finally { socket.ws.close(); }
}

function statusPageMarkersMatch(markers) {
  return markers?.title === 'Project coordinator status'
    && markers.token?.tagName === 'INPUT' && markers.token.type === 'password'
    && markers.refresh?.tagName === 'BUTTON';
}

function statusRenderState(markers) {
  const freshness = typeof markers?.freshness === 'string' ? markers.freshness.trim() : '';
  const projectHeading = typeof markers?.projectHeading === 'string' ? markers.projectHeading.trim() : '';
  if (freshness.startsWith('Checkpoint ') && /^Project\s+\S+/.test(projectHeading)) return 'success';
  if (freshness && freshness !== 'Not yet loaded.' && !freshness.startsWith('Checkpoint ')) return 'error';
  return 'pending';
}

async function waitForStatusRender(readState, timeoutMs = 15000, intervalMs = 250) {
  const deadline = Date.now() + timeoutMs;
  do {
    const state = statusRenderState(await readState());
    if (state === 'success') return true;
    if (state === 'error') throw new Error('status page displayed an error');
    if (Date.now() >= deadline) break;
    await new Promise(resolve => setTimeout(resolve, intervalMs));
  } while (Date.now() <= deadline);
  throw new Error('status page render timed out');
}

async function verifyStatusPage(target, url, port) {
  const expression = `(() => {
    const token = document.querySelector('#token'), refresh = document.querySelector('#refresh');
    return {title: document.title,
      token: token && {tagName: token.tagName, type: token.type},
      refresh: refresh && {tagName: refresh.tagName}};
  })()`;
  for (let i = 0; i < 40; i++) {
    if (target.url !== url) throw new Error('status page target changed');
    try {
      if (statusPageMarkersMatch(await evaluate(target, expression, port))) return;
    } catch {}
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error('status page identity check failed');
}

async function verifyProfile(browser, args, port) {
  try {
    const result = await call(browser, 'Browser.getBrowserCommandLine');
    if (matchesProfile(result.arguments, args.profile)) return;
    throw new Error('browser profile does not match supplied profile');
  } catch (error) {
    if (error.message === 'browser profile does not match supplied profile') throw error;
  }

  const result = await call(browser, 'Target.createTarget', { url: 'chrome://version', background: true });
  const id = result.targetId;
  if (!id) throw new Error('profile inspection target unavailable');
  try {
    let target;
    for (let i = 0; i < 40; i++) {
      const targets = await getJson(args.debugPort, 'json/list').catch(() => []);
      target = targets.find(item => item.id === id);
      if (target) break;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    validateTarget(target, 'chrome://version/', args.debugPort);
    const commandLine = await evaluate(target, `document.querySelector('#command_line')?.innerText || ''`, args.debugPort);
    if (!matchesProfileText(commandLine, args.profile)) throw new Error('browser profile does not match supplied profile');
  } finally {
    await call(browser, 'Target.closeTarget', { targetId: id }).catch(() => {});
  }
}

async function connectBrowser(args) {
  let version;
  let launched = false;
  try { version = await getJson(args.debugPort, 'json/version'); }
  catch { version = null; }
  let browser;
  if (version) {
    const url = validateDevToolsUrl(version.webSocketDebuggerUrl, args.debugPort, 'browser');
    browser = await connect(url);
  } else {
    launched = true;
    const child = spawn(args.chrome, [
      `--user-data-dir=${args.profile}`, `--remote-debugging-port=${args.debugPort}`,
      '--remote-debugging-address=127.0.0.1', '--no-first-run', '--no-default-browser-check', args.url,
    ], { detached: true, stdio: 'ignore', windowsHide: true });
    child.on('error', () => {});
    child.unref();
    for (let i = 0; i < 40 && !browser; i++) {
      try { browser = await connect((await browserConnection(args.debugPort)).url); } catch {}
      if (!browser) await new Promise(resolve => setTimeout(resolve, 250));
    }
    if (!browser) throw new Error('isolated browser did not start');
  }
  try {
    await verifyProfile(browser, args, args.debugPort);
    const target = await ensureTarget(browser, args.url, args.debugPort);
    return { browser, target, launched };
  } catch (error) {
    browser.ws.close();
    throw error;
  }
}

async function main(argv = process.argv.slice(2), input = process.stdin) {
  const args = parseArgs(argv);
  await preflightCoordinator(args.url);
  const { browser, target, launched } = await connectBrowser(args);
  try {
    await verifyStatusPage(target, args.url, args.debugPort);
    const token = await readToken(input);
    if (process.argv.some(value => value.includes(token)) || Object.values(process.env).some(value => value.includes(token)))
      throw new Error('secret transport violation');
    const expression = `(async () => {
    const input = document.querySelector('#token');
    const refresh = document.querySelector('#refresh');
    if (location.href !== ${JSON.stringify(args.url)} || document.title !== 'Project coordinator status'
        || input?.tagName !== 'INPUT' || input.type !== 'password' || refresh?.tagName !== 'BUTTON')
      throw new Error('unexpected status page');
    const visibleBefore = document.body.innerText;
    const freshness = document.querySelector('#freshness')?.innerText || '';
    const projectHeading = document.querySelector('#view h2')?.innerText || '';
    const response = await fetch('/api/status?offset=0&limit=100&report_offset=0&report_limit=100', {
      headers: {Authorization: 'Bearer ' + ${JSON.stringify(token)}}, credentials: 'omit', cache: 'no-store', redirect: 'error'
    });
    if (!response.ok) throw new Error('status request rejected');
    const stored = [localStorage, sessionStorage].some(storage =>
      Array.from({length: storage.length}, (_, i) => storage.key(i)).some(key =>
        (key + storage.getItem(key)).includes(${JSON.stringify(token)})));
    if (stored || location.href.includes(${JSON.stringify(token)})) throw new Error('secret persistence violation');
    input.value = ${JSON.stringify(token)};
    const successfulView = freshness.trim().startsWith('Checkpoint ') && /^Project\\s+\\S+/.test(projectHeading.trim());
    if (successfulView) return {alreadyLoaded:true, preserved:document.body.innerText === visibleBefore};
    refresh.click();
    return {alreadyLoaded:false, preserved:false};
    })()`;
    const render = await evaluate(target, expression, args.debugPort);
    if (render?.alreadyLoaded) {
      if (!render.preserved) throw new Error('loaded status view changed during re-authentication');
    } else {
      const stateExpression = `(() => ({freshness:document.querySelector('#freshness')?.innerText || '', projectHeading:document.querySelector('#view h2')?.innerText || ''}))()`;
      await waitForStatusRender(() => evaluate(target, stateExpression, args.debugPort));
    }
    process.stdout.write('status view authenticated\n');
  } finally { browser.ws.close(); }
}

if (require.main === module) main().catch(() => {
  process.stderr.write('status browser helper failed\n');
  process.exitCode = 1;
});

module.exports = { coordinatorResponseMatches, ensureTarget, matchesProfile, matchesProfileText, parseArgs, preflightCoordinator, readToken, statusPageMarkersMatch, statusRenderState, validateDevToolsUrl, validateTarget, validateUrl, waitForStatusRender };
