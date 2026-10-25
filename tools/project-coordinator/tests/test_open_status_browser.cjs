'use strict';

const assert = require('node:assert/strict');
const { PassThrough } = require('node:stream');
const { test } = require('node:test');
const { coordinatorResponseMatches, ensureTarget, matchesProfile, matchesProfileText, parseArgs, preflightCoordinator, readToken, statusPageMarkersMatch, statusRenderState, validateDevToolsUrl, validateTarget, validateUrl, waitForStatusRender } = require('../open_status_browser.cjs');

test('requires the coordinator unauthenticated 401 response without credentials', async () => {
  let request;
  await preflightCoordinator('http://127.0.0.1:19001/', async (url, options) => {
    request = {url, options};
    return {status:401, headers:{get:()=>'application/json; charset=utf-8'}, text:async()=>' {"error":"unauthorized or unknown route"} '};
  });
  assert.equal(request.url, 'http://127.0.0.1:19001/api/status?offset=0&limit=100&report_offset=0&report_limit=100');
  assert.equal(request.options.credentials, 'omit');
  assert.equal(request.options.method, 'GET');
  assert.deepEqual(request.options.headers, {Accept:'application/json'});
  assert.equal('Authorization' in request.options.headers, false);
  for (const response of [
    {status:200, contentType:'application/json', body:'{"error":"unauthorized or unknown route"}'},
    {status:401, contentType:'text/html', body:'{"error":"unauthorized or unknown route"}'},
    {status:401, contentType:'application/json', body:'{"error":"different service"}'},
    {status:401, contentType:'application/json', body:'{"error":"unauthorized or unknown route","extra":true}'},
  ]) assert.equal(coordinatorResponseMatches(response.status, response.contentType, response.body), false);
  await assert.rejects(preflightCoordinator('http://127.0.0.1:19001/', async () => ({
    status:200, headers:{get:()=>'application/json'}, text:async()=>'{}',
  })));
});

test('requires the exact status page title and token/refresh controls', () => {
  assert.equal(statusPageMarkersMatch({title:'Project coordinator status',token:{tagName:'INPUT',type:'password'},refresh:{tagName:'BUTTON'}}), true);
  assert.equal(statusPageMarkersMatch({title:'Other page',token:{tagName:'INPUT',type:'password'},refresh:{tagName:'BUTTON'}}), false);
  assert.equal(statusPageMarkersMatch({title:'Project coordinator status',refresh:{tagName:'BUTTON'}}), false);
  assert.equal(statusPageMarkersMatch({title:'Project coordinator status',token:{tagName:'INPUT',type:'text'},refresh:{tagName:'BUTTON'}}), false);
  assert.equal(statusPageMarkersMatch({title:'Project coordinator status',token:{tagName:'INPUT',type:'password'}}), false);
});

test('restricts status URLs to caller-supplied loopback HTTP root endpoints', () => {
  for (const port of [8765, 8766, 8767, 19001]) assert.equal(validateUrl(`http://127.0.0.1:${port}/`), `http://127.0.0.1:${port}/`);
  for (const value of [
    'https://127.0.0.1:8765/', 'http://localhost:8765/', 'http://127.0.0.1/', 'http://127.0.0.1:0/',
    'http://127.0.0.1:8765/other', 'http://127.0.0.1:8765/?x=1', 'http://127.0.0.1:8765/#fragment',
    'http://user@127.0.0.1:8765/', 'http://example.com:8765/',
  ]) assert.throws(() => validateUrl(value));
});

test('pins every used DevTools socket and proves the exact browser profile', () => {
  const page = 'ws://127.0.0.1:9333/devtools/page/target-1';
  assert.equal(validateDevToolsUrl(page, 9333, 'page', 'target-1'), page);
  assert.equal(validateDevToolsUrl('ws://127.0.0.1:9333/devtools/browser/session-1', 9333, 'browser'), 'ws://127.0.0.1:9333/devtools/browser/session-1');
  for (const value of [
    'wss://127.0.0.1:9333/devtools/page/target-1', 'ws://localhost:9333/devtools/page/target-1',
    'ws://127.0.0.1:9334/devtools/page/target-1', 'ws://127.0.0.1:9333/devtools/page/target-2',
    'ws://192.168.1.10:9333/devtools/page/target-1',
    'ws://127.0.0.1:9333/devtools/browser/target-1', 'ws://user@127.0.0.1:9333/devtools/page/target-1',
  ]) assert.throws(() => validateDevToolsUrl(value, 9333, 'page', 'target-1'));
  const profile = 'C:/Users/Example/Documents/Codex/private-profile';
  assert.equal(matchesProfile(['--user-data-dir=C:\\Users\\Example\\Documents\\Codex\\private-profile'], profile), true);
  assert.equal(matchesProfile(['--user-data-dir=C:\\Users\\Example\\Documents\\Codex\\other-profile'], profile), false);
  assert.equal(matchesProfileText('Path --user-data-dir="C:\\Users\\Example\\Documents\\Codex\\private-profile"', profile), true);
  assert.equal(matchesProfileText('Path --user-data-dir="C:\\Users\\Example\\Documents\\Codex\\other-profile"', profile), false);
  assert.equal(matchesProfile([], profile), false);
  assert.throws(() => validateTarget({type:'page', id:'target-1', url:'http://127.0.0.1:19001/', webSocketDebuggerUrl:page}, 'http://127.0.0.1:9333/', 9333));
  assert.throws(() => validateTarget({type:'page', id:'target-1', url:'http://127.0.0.1:19001/', webSocketDebuggerUrl:'ws://127.0.0.1:9334/devtools/page/target-1'}, 'http://127.0.0.1:19001/', 9333));
});

test('creates and validates the requested page target when it is absent', async () => {
  const url = 'http://127.0.0.1:19001/';
  const page = {type:'page', id:'new-target', url, webSocketDebuggerUrl:'ws://127.0.0.1:9333/devtools/page/new-target'};
  let listed = 0;
  let createdUrl;
  const target = await ensureTarget({}, url, 9333, {
    findTarget:async()=>null,
    createTarget:async value=>{createdUrl=value;return {targetId:'new-target'};},
    listTargets:async()=>++listed === 1 ? [] : [page],
    sleep:async()=>{},
  });
  assert.equal(createdUrl, url);
  assert.equal(listed, 2);
  assert.equal(target.id, 'new-target');
  await assert.rejects(ensureTarget({}, url, 9333, {
    findTarget:async()=>null,
    createTarget:async()=>({targetId:'new-target'}),
    listTargets:async()=>[{...page, url:'http://127.0.0.1:19002/'}],
    sleep:async()=>{},
  }), /URL mismatch/);
  await assert.rejects(ensureTarget({}, url, 9333, {
    findTarget:async()=>null,
    createTarget:async()=>({targetId:'new-target'}),
    listTargets:async()=>[],
    sleep:async()=>{},
  }), /timed out/);
});

test('waits for a successful status render and fails visible errors and timeouts', async () => {
  assert.equal(statusRenderState({freshness:'Checkpoint abc · observed now',projectHeading:'Project P-CPI-10'}), 'success');
  assert.equal(statusRenderState({freshness:'API returned 401',projectHeading:''}), 'error');
  assert.equal(statusRenderState({freshness:'Not yet loaded.',projectHeading:''}), 'pending');
  let reads = 0;
  assert.equal(await waitForStatusRender(async()=>++reads === 1
    ? {freshness:'Not yet loaded.',projectHeading:''}
    : {freshness:'Checkpoint abc · observed now',projectHeading:'Project P-CPI-10'}, 100, 0), true);
  await assert.rejects(waitForStatusRender(async()=>({freshness:'API returned 401',projectHeading:''}), 100, 0), /displayed an error/);
  await assert.rejects(waitForStatusRender(async()=>({freshness:'Not yet loaded.',projectHeading:''}), 0, 0), /timed out/);
});

test('reads a bounded bearer secret from stdin and rejects token arguments', async () => {
  const token = 'x'.repeat(64);
  const input = new PassThrough();
  const received = readToken(input);
  input.end(`${token}\n`);
  assert.equal(await received, token);
  const short = new PassThrough();
  const rejected = readToken(short);
  short.end('short\n');
  await assert.rejects(rejected);
  const terminal = new PassThrough();
  terminal.isTTY = true;
  await assert.rejects(readToken(terminal));
  assert.throws(() => parseArgs([
    '--url', 'http://127.0.0.1:8765/', '--profile', 'C:/private-profile', '--chrome', 'chrome.exe', '--token', token,
  ]));
});
