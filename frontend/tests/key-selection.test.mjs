import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { stripTypeScriptTypes } from 'node:module';
import { test } from 'node:test';

// Keep production's bundler import unchanged while running the TypeScript helper in Node.
const source = readFileSync(new URL('../lib/key-selection.ts', import.meta.url), 'utf8')
  .replace("'./credential-types'", JSON.stringify(new URL('../lib/credential-types.ts', import.meta.url).href));
const moduleSource = stripTypeScriptTypes(source);
const { BulkCopyError, keySelectionId, selectedKeysText } = await import(
  `data:text/javascript;base64,${Buffer.from(moduleSource).toString('base64')}`
);

const key = (id = 1, storageType = 'openai_official', status = 'valid') => ({ id, storageType, status });
const response = (secrets) => Response.json({ secrets });

test('selection identity distinguishes storage tables and buckets, with exact invalid results in one bucket', () => {
  assert.notEqual(keySelectionId(key()), keySelectionId(key(1, 'openrouter')));
  assert.notEqual(keySelectionId(key()), keySelectionId(key(1, 'openai_official', 'invalid')));
  assert.notEqual(keySelectionId(key()), keySelectionId(key(2)));
  assert.equal(keySelectionId(key(1, 'openai_official', 'permission_denied')),
    keySelectionId(key(1, 'openai_official', 'network_error')));
});

test('a single no-store POST reveals exactly the selected identities in display order', async () => {
  const controller = new AbortController();
  let calls = 0;
  const text = await selectedKeysText([
    key(8, 'openrouter', 'request_limit'), key(8, 'openai_official', 'valid'),
    key(3, 'openai_official', 'usage_limit_reached'),
  ], async (path, init) => {
    calls += 1;
    assert.equal(path, '/api/keys/batch-reveal');
    assert.equal(init.method, 'POST');
    assert.equal(init.cache, 'no-store');
    assert.equal(init.headers['Content-Type'], 'application/json');
    assert.equal(init.signal, controller.signal);
    assert.deepEqual(JSON.parse(init.body), { keys: [
      { id: 8, type: 'openrouter', status: 'invalid' },
      { id: 8, type: 'openai_official', status: 'valid' },
      { id: 3, type: 'openai_official', status: 'usage_limit_reached' },
    ] });
    return response(['synthetic-first', ' synthetic-second ', 'synthetic-third']);
  }, controller.signal);
  assert.equal(calls, 1);
  assert.equal(text, 'synthetic-first\n synthetic-second \nsynthetic-third');
});

test('selection limits, duplicate identities and malformed records fail before sending a request', async () => {
  let calls = 0;
  const request = async () => { calls += 1; return response([]); };
  for (const rows of [
    [], Array.from({ length: 101 }, (_, i) => key(i + 1)),
    [key(), key()],
    [key(1, 'openai_official', 'invalid'), key(1, 'openai_official', 'network_error')],
    [key(0)], [key(-1)], [key(1.5)], [key(NaN)], [key(Number.MAX_SAFE_INTEGER + 1)],
    [key(1, '')], [key(1, ' ')], [key(1, null)], [key(1, 'openai_official', null)],
  ]) await assert.rejects(selectedKeysText(rows, request), BulkCopyError);
  assert.equal(calls, 0);
  assert.equal(await selectedKeysText(Array.from({ length: 100 }, (_, i) => key(i + 1)),
    async () => response(Array.from({ length: 100 }, (_, i) => `synthetic-${i}`))),
  Array.from({ length: 100 }, (_, i) => `synthetic-${i}`).join('\n'));
});

test('authorization and server failures expose their status and never return response secrets', async () => {
  for (const status of [401, 403, 404, 409, 500]) {
    await assert.rejects(selectedKeysText([key()], async () => Response.json({ secrets: ['synthetic-partial'] }, { status })),
      (error) => error instanceof BulkCopyError && error.status === status);
  }
});

test('malformed JSON, partial payloads, extra records and empty secrets reject atomically', async () => {
  for (const body of [
    null, {}, { secrets: 'synthetic-first' }, { secrets: [] },
    { secrets: ['synthetic-first'] }, { secrets: ['synthetic-first', 'synthetic-second', 'extra'] },
    { secrets: ['synthetic-first', null] }, { secrets: ['synthetic-first', 7] },
    { secrets: ['synthetic-first', ''] }, { secrets: ['synthetic-first', '   '] },
  ]) await assert.rejects(selectedKeysText([key(1), key(2)], async () => Response.json(body)), BulkCopyError);
  await assert.rejects(selectedKeysText([key()], async () => new Response('invalid-json')), BulkCopyError);
});

test('an aborted or outdated request cannot return copied text, even when a transport ignores abort', async () => {
  const before = new AbortController();
  before.abort();
  let calls = 0;
  await assert.rejects(selectedKeysText([key()], async () => { calls += 1; return response(['synthetic']); }, before.signal),
    { name: 'AbortError' });
  assert.equal(calls, 0);

  const duringRequest = new AbortController();
  await assert.rejects(selectedKeysText([key()], async () => {
    duringRequest.abort();
    return response(['synthetic']);
  }, duringRequest.signal), { name: 'AbortError' });

  const duringBody = new AbortController();
  const delayedBody = response(['synthetic']);
  delayedBody.json = async () => { duringBody.abort(); return { secrets: ['synthetic'] }; };
  await assert.rejects(selectedKeysText([key()], async () => delayedBody, duringBody.signal), { name: 'AbortError' });
});

test('network failures are propagated without producing any partial text', async () => {
  const error = new TypeError('synthetic-network-failure');
  await assert.rejects(selectedKeysText([key()], async () => { throw error; }), (actual) => actual === error);
});
