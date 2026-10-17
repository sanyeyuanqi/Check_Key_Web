import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { stripTypeScriptTypes } from 'node:module';
import { test } from 'node:test';

process.env.TZ = 'Asia/Shanghai';

const rangeSource = stripTypeScriptTypes(readFileSync(new URL('../lib/upload-time-range.ts', import.meta.url), 'utf8'));
const rangeModuleUrl = `data:text/javascript;base64,${Buffer.from(rangeSource).toString('base64')}`;
// Resolve the production bundler import into a data module for Node's isolated tests.
const querySource = readFileSync(new URL('../lib/key-list-query.ts', import.meta.url), 'utf8')
  .replace("'./upload-time-range'", JSON.stringify(rangeModuleUrl));
const { keyListQueryParams, appliedExportRange } = await import(
  `data:text/javascript;base64,${Buffer.from(stripTypeScriptTypes(querySource)).toString('base64')}`
);

const context = { provider: 'Anthropic', storageType: 'anthropic_official', page: 3 };
const range = { start: '2026-09-02T23:15', end: '2026-09-03T08:30' };
const emptyRange = { start: '', end: '' };
const filters = (status = 'valid', selectedRange = range) => ({ mode: 'filters', status, range: selectedRange });

test('filter queries preserve exact status, platform, type and pagination without prefix search', () => {
  const params = keyListQueryParams(filters('permission_error'), context);
  assert.deepEqual(Object.fromEntries(params), {
    status_mode: 'exact', status: 'permission_error', provider: 'Anthropic', type: 'anthropic_official', page: '3',
    created_from: '2026-09-02T15:15:00.000Z', created_before: '2026-09-03T00:31:00.000Z',
  });
  assert.equal(params.has('q'), false);
});

test('filter mode ignores a stale runtime prefix and supports all statuses with no date range', () => {
  const params = keyListQueryParams({ ...filters('all', emptyRange), prefix: 'stale-prefix', q: 'stale-q' }, context);
  assert.equal(params.get('status'), 'all');
  for (const name of ['q', 'created_from', 'created_before']) assert.equal(params.has(name), false);
});

test('same-minute ranges include the entire selected minute in Asia/Shanghai', () => {
  const minute = '2026-09-02T23:15';
  const params = keyListQueryParams(filters('all', { start: minute, end: minute }), context);
  assert.equal(params.get('created_from'), '2026-09-02T15:15:00.000Z');
  assert.equal(params.get('created_before'), '2026-09-02T15:16:00.000Z');
  assert.equal(Date.parse(params.get('created_before')) - Date.parse(params.get('created_from')), 60_000);
});

test('end-minute inclusion crosses midnight and year boundaries correctly', () => {
  const params = keyListQueryParams(filters('all', {
    start: '2026-12-31T23:58', end: '2026-12-31T23:59',
  }), context);
  assert.equal(params.get('created_from'), '2026-12-31T15:58:00.000Z');
  assert.equal(params.get('created_before'), '2026-12-31T16:00:00.000Z');
});

test('prefix queries trim and encode the prefix while discarding stale filter state', () => {
  const prefix = 'synthetic-密钥 +/=&%#';
  const params = keyListQueryParams({
    mode: 'prefix', prefix: `  ${prefix}  `, status: 'invalid',
    range: { start: 'invalid-stale-date', end: 'invalid-stale-date' },
  }, context);
  assert.deepEqual(Object.fromEntries(params), {
    status_mode: 'exact', status: 'all', provider: 'Anthropic', type: 'anthropic_official', page: '3', q: prefix,
  });
  assert.equal(new URLSearchParams(params.toString()).get('q'), prefix);
  assert.match(params.toString(), /%2B%2F%3D%26%25%23/);
  assert.equal(params.has('created_from'), false);
  assert.equal(params.has('created_before'), false);
});

test('an empty prefix omits q and still queries all statuses', () => {
  for (const prefix of ['', '  \t\n  ']) {
    const params = keyListQueryParams({ mode: 'prefix', prefix }, { ...context, page: 1 });
    assert.equal(params.get('status'), 'all');
    assert.equal(params.get('page'), '1');
    for (const name of ['q', 'created_from', 'created_before']) assert.equal(params.has(name), false);
  }
});

test('query context is URL encoded without changing platform or storage type', () => {
  const specialContext = { provider: 'Synthetic & Platform', storageType: 'synthetic/type+1', page: 2 };
  const params = keyListQueryParams({ mode: 'prefix', prefix: 'synthetic' }, specialContext);
  const decoded = new URLSearchParams(params.toString());
  assert.equal(decoded.get('provider'), specialContext.provider);
  assert.equal(decoded.get('type'), specialContext.storageType);
  assert.equal(decoded.get('page'), '2');
});

test('invalid filter ranges fail instead of generating a misleading query', () => {
  for (const invalidRange of [
    { start: '2026-02-30T10:00', end: '' },
    { start: '2026-09-03T00:00', end: '2026-09-02T23:59' },
  ]) assert.throws(() => keyListQueryParams(filters('all', invalidRange), context));
});

test('exports use the applied filter range and ignore all prefix-mode stale dates', () => {
  assert.deepEqual(appliedExportRange(filters()), range);
  assert.deepEqual(appliedExportRange(filters('all', emptyRange)), emptyRange);
  assert.deepEqual(appliedExportRange({ mode: 'prefix', prefix: 'synthetic', range, status: 'valid' }), emptyRange);
  assert.deepEqual(appliedExportRange({ mode: 'prefix', prefix: '' }), emptyRange);
});
