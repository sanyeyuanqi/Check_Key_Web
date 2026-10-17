import assert from 'node:assert/strict';
import { test } from 'node:test';
import { apiFetch, markNextReadsFresh } from '../lib/api-client.ts';

function transport(t) {
  const requests = [];
  markNextReadsFresh([]);
  t.after(() => markNextReadsFresh([]));
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    requests.push({ url: new URL(url), init });
    if (init.signal?.aborted) throw init.signal.reason;
    return Response.json({ synthetic: true });
  });
  return requests;
}

test('normal GETs preserve query parameters and use the cache by default', async (t) => {
  const requests = transport(t);
  await apiFetch('/api/keys?provider=OpenAI&type=openai_official&page=2');
  assert.equal(requests[0].url.searchParams.has('refresh'), false);
  assert.equal(requests[0].url.searchParams.get('page'), '2');
  assert.equal(requests[0].init.credentials, 'include');
});

test('only the next matching GET for each marked endpoint bypasses the cache', async (t) => {
  const requests = transport(t);
  const endpoints = ['/api/keys', '/api/credential-statuses', '/api/valid-key-records', '/api/export-records'];
  markNextReadsFresh(endpoints);
  for (const endpoint of endpoints) {
    await apiFetch(`${endpoint}?provider=OpenAI`);
    await apiFetch(`${endpoint}?provider=Anthropic`);
  }
  for (let i = 0; i < requests.length; i += 2) {
    assert.equal(requests[i].url.searchParams.get('refresh'), '1');
    assert.equal(requests[i + 1].url.searchParams.has('refresh'), false);
  }
});

test('a page-specific marker never forces other record pages or sensitive endpoints', async (t) => {
  const requests = transport(t);
  markNextReadsFresh(['/api/keys', '/api/auth/me', '/api/keys/1/reveal', '/api/export']);
  for (const path of [
    '/api/auth/me', '/api/access', '/api/keys/1/reveal?type=openai_official',
    '/api/export?status=valid', '/api/admin/users', '/api/keys/batch-reveal',
    '/api/valid-key-records', '/api/export-records', '/api/credential-statuses', '/api/keys/',
  ]) await apiFetch(path);
  assert.equal(requests.every(({ url }) => !url.searchParams.has('refresh')), true);
  await apiFetch('/api/keys');
  assert.equal(requests.at(-1).url.searchParams.get('refresh'), '1');
});

test('POST, PATCH, DELETE and HEAD never consume or receive a GET refresh marker', async (t) => {
  const requests = transport(t);
  markNextReadsFresh(['/api/keys']);
  for (const method of ['POST', 'PATCH', 'DELETE', 'HEAD']) await apiFetch('/api/keys', { method });
  assert.equal(requests.every(({ url }) => !url.searchParams.has('refresh')), true);
  await apiFetch('/api/keys', { method: 'get' });
  assert.equal(requests.at(-1).url.searchParams.get('refresh'), '1');
});

test('unused markers expire after two seconds instead of leaking into later navigation', async (t) => {
  const requests = transport(t);
  let now = 10_000;
  t.mock.method(Date, 'now', () => now);
  markNextReadsFresh(['/api/keys', '/api/export-records']);
  now += 1999;
  await apiFetch('/api/keys');
  assert.equal(requests[0].url.searchParams.get('refresh'), '1');
  now += 1;
  await apiFetch('/api/export-records');
  assert.equal(requests[1].url.searchParams.has('refresh'), false);
});

test('repeated header refresh replaces old page markers and can clear them for uncached pages', async (t) => {
  const requests = transport(t);
  markNextReadsFresh(['/api/keys']);
  markNextReadsFresh(['/api/export-records']);
  await apiFetch('/api/keys');
  assert.equal(requests.at(-1).url.searchParams.has('refresh'), false);
  await apiFetch('/api/export-records');
  assert.equal(requests.at(-1).url.searchParams.get('refresh'), '1');
  markNextReadsFresh(['/api/keys']);
  markNextReadsFresh([]);
  await apiFetch('/api/keys');
  assert.equal(requests.at(-1).url.searchParams.has('refresh'), false);
});

test('aborted reads do not consume the next actual read marker', async (t) => {
  const requests = transport(t);
  const controller = new AbortController();
  controller.abort();
  markNextReadsFresh(['/api/keys']);
  await assert.rejects(apiFetch('/api/keys', { signal: controller.signal }), { name: 'AbortError' });
  assert.equal(requests[0].url.searchParams.has('refresh'), false);
  await apiFetch('/api/keys');
  assert.equal(requests[1].url.searchParams.get('refresh'), '1');
});

test('refresh preserves encoded filters, replaces duplicate refresh values and keeps explicit manual reads', async (t) => {
  const requests = transport(t);
  markNextReadsFresh(['/api/keys']);
  const query = new URLSearchParams({ provider: 'Synthetic & Provider', q: 'synthetic+prefix%_', page: '3' });
  query.append('refresh', '0');
  query.append('refresh', 'false');
  await apiFetch(`/api/keys?${query}`, { cache: 'no-store' });
  assert.equal(requests[0].url.searchParams.get('provider'), 'Synthetic & Provider');
  assert.equal(requests[0].url.searchParams.get('q'), 'synthetic+prefix%_');
  assert.deepEqual(requests[0].url.searchParams.getAll('refresh'), ['1']);
  assert.equal(requests[0].url.searchParams.get('page'), '3');
  assert.equal(requests[0].init.cache, 'no-store');
  await apiFetch('/api/keys?refresh=1');
  assert.equal(requests[1].url.searchParams.get('refresh'), '1');
});
