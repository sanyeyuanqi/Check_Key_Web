import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { stripTypeScriptTypes } from 'node:module';
import { test } from 'node:test';

const source = stripTypeScriptTypes(readFileSync(new URL('../lib/valid-record-export.ts', import.meta.url), 'utf8'));
const { validRecordExportRequest, exportDownloadName } = await import(
  `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
);

const row = (credentialId = 1, storageType = 'openai_official') => ({ credentialId, storageType });
const query = (mode = 'prefix', value = '') => ({ mode, value });
const parameters = (request) => new URL(request.url, 'http://synthetic.test').searchParams;

test('selected export posts only provider and selected identities in display order', () => {
  const request = validRecordExportRequest('selected', ' OpenAI ', [
    { ...row(8, 'openrouter'), secret: 'synthetic-ignored-value', status: 'invalid', ipAddress: '192.0.2.1' },
    row(8), row(3),
  ], query('ip', '192.0.2.9'));
  assert.equal(request.url, '/api/valid-key-records/batch-export?prepare=1');
  assert.equal(request.init.method, 'POST');
  assert.equal(request.init.cache, 'no-store');
  assert.deepEqual(request.init.headers, { 'Content-Type': 'application/json' });
  assert.deepEqual(JSON.parse(request.init.body), {
    provider: 'OpenAI', keys: [{ id: 8, type: 'openrouter' }, { id: 8, type: 'openai_official' }, { id: 3, type: 'openai_official' }],
  });
  assert.equal(request.init.body.includes('synthetic-ignored-value'), false);
  assert.equal(request.init.body.includes('192.0.2.'), false);
});

test('selected exports accept one and one hundred unique identities, reject empty and oversized selections', () => {
  for (const count of [1, 100]) {
    const request = validRecordExportRequest('selected', 'OpenAI', Array.from({ length: count }, (_, i) => row(i + 1)), query());
    assert.equal(JSON.parse(request.init.body).keys.length, count);
  }
  for (const rows of [[], Array.from({ length: 101 }, (_, i) => row(i + 1)), null]) {
    assert.throws(() => validRecordExportRequest('selected', 'OpenAI', rows, query()), /1 至 100/);
  }
});

test('selected identities require safe positive integers and nonempty storage types', () => {
  for (const id of [0, -1, 1.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1, '1', null]) {
    assert.throws(() => validRecordExportRequest('selected', 'OpenAI', [row(id)], query()), /信息无效/);
  }
  for (const type of ['', '  \t', null, 1]) {
    assert.throws(() => validRecordExportRequest('selected', 'OpenAI', [row(1, type)], query()), /信息无效/);
  }
  assert.throws(() => validRecordExportRequest('selected', 'OpenAI', [null], query()), /信息无效/);
  assert.doesNotThrow(() => validRecordExportRequest('selected', 'OpenAI', [row(Number.MAX_SAFE_INTEGER)], query()));
});

test('duplicate selected identities fail while the same ID in different storage types remains distinct', () => {
  assert.throws(() => validRecordExportRequest('selected', 'OpenAI', [row(1), row(1)], query()), /重复/);
  const request = validRecordExportRequest('selected', 'OpenAI', [row(1), row(1, 'openrouter')], query());
  assert.equal(JSON.parse(request.init.body).keys.length, 2);
});

test('all export uses the applied prefix and no page or selected identities', () => {
  const applied = { ...query('prefix', '  synthetic-密钥 +/=&%#  '), ip: '192.0.2.8', page: 7, draft: 'ignored-draft' };
  const first = validRecordExportRequest('all', 'Synthetic & Platform', [row(1)], applied);
  const anotherPage = validRecordExportRequest('all', 'Synthetic & Platform', [row(90), row(91)], applied);
  assert.deepEqual(first, anotherPage);
  assert.equal(first.init.method, 'GET');
  assert.equal(first.init.cache, 'no-store');
  assert.equal(first.init.body, undefined);
  assert.deepEqual(Object.fromEntries(parameters(first)), { provider: 'Synthetic & Platform', prepare: '1', q: 'synthetic-密钥 +/=&%#' });
  for (const name of ['ip', 'page', 'keys', 'draft']) assert.equal(parameters(first).has(name), false);
  assert.match(first.url, /%2B%2F%3D%26%25%23/);
});

test('all IP export has only the applied IP, even with stale runtime prefix state', () => {
  const request = validRecordExportRequest('all', 'Azure', [], {
    ...query('ip', '  2001:db8::9  '), q: 'stale-prefix', prefix: 'stale-prefix', page: 10,
  });
  assert.deepEqual(Object.fromEntries(parameters(request)), { provider: 'Azure', prepare: '1', ip: '2001:db8::9' });
  assert.equal(parameters(request).has('q'), false);
});

test('all export permits no results on the current page and ignores invalid or excessive stale selections', () => {
  for (const rows of [[], [row(0)], null, Array.from({ length: 101 }, () => row())]) {
    const request = validRecordExportRequest('all', 'OpenAI', rows, query());
    assert.deepEqual(Object.fromEntries(parameters(request)), { provider: 'OpenAI', prepare: '1' });
    assert.equal(request.init.body, undefined);
  }
  for (const mode of ['prefix', 'ip']) {
    assert.deepEqual(Object.fromEntries(parameters(validRecordExportRequest('all', 'OpenAI', [], query(mode, ' \t\n ')))), { provider: 'OpenAI', prepare: '1' });
  }
});

test('invalid platform or export mode cannot accidentally create an unrestricted request', () => {
  for (const provider of ['', '  \t', null, 1]) {
    for (const mode of ['selected', 'all']) assert.throws(() => validRecordExportRequest(mode, provider, [row()], query()), /服务平台/);
  }
  for (const mode of ['', 'everything', null]) {
    assert.throws(() => validRecordExportRequest(mode, 'OpenAI', [row()], query()), /导出条件无效/);
  }
  for (const applied of [null, query('unknown', 'x'), query('prefix', null)]) {
    assert.throws(() => validRecordExportRequest('all', 'OpenAI', [], applied), /导出条件无效/);
  }
});

test('download filenames prefer UTF-8 extended names and preserve valid ordinary TXT names', () => {
  assert.equal(exportDownloadName("attachment; filename=ordinary.txt; filename*=UTF-8''%E6%9C%89%E6%95%88%E5%AF%86%E9%92%A5.txt", 'fallback.txt'), '有效密钥.txt');
  assert.equal(exportDownloadName('attachment; filename="synthetic keys.TXT"', 'fallback.txt'), 'synthetic keys.TXT');
  assert.equal(exportDownloadName('attachment; filename=synthetic.txt; size=5', 'fallback.txt'), 'synthetic.txt');
  assert.equal(exportDownloadName('attachment; filename="synthetic;keys.txt"', 'fallback.txt'), 'synthetic;keys.txt');
  assert.equal(exportDownloadName("attachment; filename*=UTF-8''%ZZ; filename=ordinary.txt", 'fallback.txt'), 'ordinary.txt');
});

test('download names remove paths and control characters, including encoded Windows paths', () => {
  assert.equal(exportDownloadName('attachment; filename="../../synthetic.txt"', 'fallback.txt'), 'synthetic.txt');
  assert.equal(exportDownloadName("attachment; filename*=UTF-8''C%3A%5Ctemp%5Csynthetic.txt", 'fallback.txt'), 'synthetic.txt');
  assert.equal(exportDownloadName("attachment; filename*=UTF-8''synthetic%00%0D%0A%7F%C2%85.txt", 'fallback.txt'), 'synthetic.txt');
  assert.equal(exportDownloadName("attachment; filename*=UTF-8''safe%E2%80%AE.txt", 'fallback.txt'), 'safe.txt');
  assert.equal(exportDownloadName('attachment; filename="synthetic?.txt"', 'fallback.txt'), 'synthetic.txt');
});

test('non-TXT or unusable downloaded names use a sanitized TXT fallback', () => {
  for (const header of [null, '', 'attachment', 'attachment; filename=""', 'attachment; filename=payload.exe', 'attachment; filename=.txt', 'attachment; filename=CON.txt']) {
    assert.equal(exportDownloadName(header, '../../synthetic-fallback.txt'), 'synthetic-fallback.txt');
  }
  assert.equal(exportDownloadName("attachment; filename*=UTF-8''payload.exe; filename=ordinary.txt", 'fallback.txt'), 'fallback.txt');
  for (const fallback of ['', '../', 'payload.exe', 'NUL.txt', '.txt', '\u0000']) {
    assert.equal(exportDownloadName(null, fallback), 'valid-keys.txt');
  }
});
