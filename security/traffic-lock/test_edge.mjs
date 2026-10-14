import assert from 'node:assert/strict';
import {test} from 'node:test';
import trafficLock from './traffic-lock.ts';
const originalFetch = globalThis.fetch;
function environment(values = {}) {
  globalThis.Netlify = {env: {get: name => values[name]}};
}
const configured = {
  TRAFFIC_LOCK_AUTHORIZER_URL: 'https://authority.example/_traffic_lock/authorize',
  TRAFFIC_LOCK_AUTHORIZER_KEY: 'x'.repeat(32), TRAFFIC_LOCK_PROJECT: 'test',
};
function fixture() {
  let calls = 0;
  return {context: {ip: '192.0.2.1', next: async () => {calls++; return new Response('origin', {headers: {'cache-control': 'public,max-age=3600'}});}}, calls: () => calls};
}
test('missing setup blocks page, asset, API and direct function without reaching origin', async () => {
  environment();
  const f = fixture();
  for (const path of ['/', '/assets/app.js', '/api/data', '/.netlify/functions/api']) {
    const response = await trafficLock(new Request('https://app.example' + path), f.context);
    assert.equal(response.status, 403);
    assert.equal(response.headers.get('cache-control'), 'no-store');
  }
  assert.equal(f.calls(), 0);
});
test('network failure, bad answer and redirect never fail open', async () => {
  environment(configured);
  const f = fixture();
  try {
    for (const fetcher of [async () => {throw new Error('offline');}, async () => new Response('{"allow":true}', {status: 403}), async () => new Response('invalid'), async () => new Response('{"allow":false}'), async () => new Response('', {status: 302})]) {
      globalThis.fetch = fetcher;
      assert.equal((await trafficLock(new Request('https://app.example/'), f.context)).status, 403);
    }
  } finally {globalThis.fetch = originalFetch;}
  assert.equal(f.calls(), 0);
});
test('uses trusted context IP, authorizes every request, strips cache permission', async () => {
  environment(configured);
  const f = fixture();
  let calls = 0;
  globalThis.fetch = async (url, options) => {
    calls++;
    assert.equal(url.href, configured.TRAFFIC_LOCK_AUTHORIZER_URL);
    assert.equal(options.redirect, 'error');
    assert.deepEqual(JSON.parse(options.body), {project: 'test', ip: '192.0.2.1', method: 'GET', path: '/api/data'});
    assert.ok(!options.body.includes('secret-cookie'));
    return new Response('{"allow":true}');
  };
  try {
    for (let i=0; i<2; i++) {
      const response = await trafficLock(new Request('https://app.example/api/data?nonce=' + i, {headers: {'x-forwarded-for': '203.0.113.1', cookie: 'secret-cookie'}}), f.context);
      assert.equal(response.status, 200);
      assert.equal(await response.text(), 'origin');
      assert.equal(response.headers.get('cache-control'), 'private, no-store');
    }
  } finally {globalThis.fetch = originalFetch;}
  assert.equal(calls, 2);
  assert.equal(f.calls(), 2);
});
test('non-HTTPS authority, missing trusted IP and short service key are rejected', async () => {
  const f = fixture();
  for (const values of [{...configured, TRAFFIC_LOCK_AUTHORIZER_URL: 'http://authority.example/_traffic_lock/authorize'}, {...configured, TRAFFIC_LOCK_AUTHORIZER_KEY: 'short'}]) {
    environment(values);
    assert.equal((await trafficLock(new Request('https://app.example/'), f.context)).status, 403);
  }
  environment(configured);
  assert.equal((await trafficLock(new Request('https://app.example/'), {...f.context, ip: ''})).status, 403);
  assert.equal(f.calls(), 0);
});
