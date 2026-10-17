import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { stripTypeScriptTypes } from 'node:module';
import { test } from 'node:test';

const source = stripTypeScriptTypes(readFileSync(new URL('../lib/prepared-export.ts', import.meta.url), 'utf8'));
const { ExportPreparationError, parsePreparedExport, handoffPreparedExport, startBrowserExportDownload } = await import(
  `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
);
const valid = () => ({
  downloadUrl: `/api/exports/download/${'a8'.repeat(32)}`,
  fileName: 'synthetic-large-keys.txt', count: 1000000, bytes: 35000000, expiresIn: 600,
});
const responseFor = (body = valid(), headers = {}) => new Response(JSON.stringify(body), {
  headers: { 'Content-Type': 'application/json; charset=utf-8', ...headers },
});
const noDownload = () => { throw new Error('Unexpected download'); };

test('prepared exports validate bounded metadata without carrying full keys into page state', () => {
  const body = valid();
  assert.deepEqual(parsePreparedExport({ ...body, unrelated: 'ignored' }), body);
  assert.doesNotThrow(() => parsePreparedExport({ ...body, count: 0, bytes: 0, expiresIn: 30 }));
  assert.doesNotThrow(() => parsePreparedExport({ ...body, expiresIn: 3600 }));
});

test('download tickets must use the exact same-site opaque-token path', () => {
  for (const downloadUrl of [
    'https://external.test/download', '//external.test/download', 'javascript:alert(1)',
    '/api/keys/1/reveal', '/api/export', `${valid().downloadUrl}?other=1`,
    `${valid().downloadUrl}#hash`, `${valid().downloadUrl}/extra`, `${valid().downloadUrl}\n`,
    `/api/exports/download/${'A'.repeat(64)}`, `/api/exports/download/${'a'.repeat(63)}`,
    `/api/exports/download/${'a'.repeat(65)}`, '/api/exports/download/../secret',
  ]) assert.throws(() => parsePreparedExport({ ...valid(), downloadUrl }), /信息无效/);
});

test('unusable metadata cannot start a download or escape the intended TXT filename', () => {
  for (const value of [null, [], '', 0]) assert.throws(() => parsePreparedExport(value));
  for (const fileName of [
    '', '.txt', '../../keys.txt', 'C:\\temp\\keys.txt', 'keys.exe', 'keys\n.txt',
    'keys\u202e.txt', 'CON.txt', 'keys?.txt', `${'x'.repeat(157)}.txt`,
  ]) assert.throws(() => parsePreparedExport({ ...valid(), fileName }), /信息无效/);
  for (const field of ['count', 'bytes', 'expiresIn']) {
    for (const value of [-1, 1.5, Number.MAX_SAFE_INTEGER + 1, '1', null]) {
      assert.throws(() => parsePreparedExport({ ...valid(), [field]: value }), /信息无效/);
    }
  }
  for (const expiresIn of [0, 29, 3601]) assert.throws(() => parsePreparedExport({ ...valid(), expiresIn }));
});

test('only a small successful JSON preparation response is handed to native downloading', async () => {
  const calls = [];
  const response = responseFor();
  response.blob = noDownload;
  const result = await handoffPreparedExport(response, {
    signal: new AbortController().signal, isCurrent: () => true, download: (file) => calls.push(file),
  });
  assert.deepEqual(calls, [valid()]);
  assert.deepEqual(result, valid());
});

test('error, malformed JSON and plaintext responses never begin native downloading', async () => {
  const responses = [
    new Response('synthetic-error', { status: 503, headers: { 'Content-Type': 'application/json' } }),
    new Response('synthetic-secret-not-an-export-ticket', { headers: { 'Content-Type': 'text/plain' } }),
    new Response('{broken-json', { headers: { 'Content-Type': 'application/json' } }),
    responseFor({ ...valid(), downloadUrl: 'https://external.test/' }),
    new Response(new Uint8Array([0xff]), { headers: { 'Content-Type': 'application/json' } }),
  ];
  for (const response of responses) {
    await assert.rejects(handoffPreparedExport(response, {
      signal: new AbortController().signal, isCurrent: () => true, download: noDownload,
    }));
  }
});

test('HTTP failures show fixed actionable text without reading or reflecting the response body', async () => {
  for (const [status, message] of [
    [413, '导出文件过大，请缩小导出范围后重试'],
    [503, '导出任务较多，请稍后重试'],
    [404, '所选记录已变更，请刷新后重试'],
    [500, '导出失败，请稍后重试'],
  ]) {
    let cancelled = false;
    const response = new Response(new ReadableStream({
      start(stream) { stream.enqueue(new TextEncoder().encode('ignored-private-error-detail')); },
      cancel() { cancelled = true; },
    }), { status });
    await assert.rejects(handoffPreparedExport(response, {
      signal: new AbortController().signal, isCurrent: () => true, download: noDownload,
    }), (error) => error instanceof ExportPreparationError && error.message === message);
    assert.equal(cancelled, true);
  }
});

test('metadata size limits prevent inadvertently buffering the exported key file', async () => {
  for (const response of [
    responseFor(valid(), { 'Content-Length': '16385' }),
    responseFor(valid(), { 'Content-Length': 'invalid' }),
    responseFor({ ...valid(), unexpectedData: 'x'.repeat(16384) }),
  ]) {
    await assert.rejects(handoffPreparedExport(response, {
      signal: new AbortController().signal, isCurrent: () => true, download: noDownload,
    }), /过大/);
  }
});

test('aborted or replaced requests never read or download a late preparation result', async () => {
  const controller = new AbortController();
  controller.abort();
  const unreadable = { get ok() { throw new Error('An obsolete response must not be read'); } };
  assert.equal(await handoffPreparedExport(unreadable, {
    signal: controller.signal, isCurrent: () => true, download: noDownload,
  }), null);
  assert.equal(await handoffPreparedExport(unreadable, {
    signal: new AbortController().signal, isCurrent: () => false, download: noDownload,
  }), null);
});

test('cancellation during metadata reading stops the stream and prevents downloading', async () => {
  const controller = new AbortController();
  let release;
  let cancelled = false;
  const response = new Response(new ReadableStream({
    start(stream) { release = stream; },
    cancel() { cancelled = true; },
  }), { headers: { 'Content-Type': 'application/json' } });
  const result = handoffPreparedExport(response, { signal: controller.signal, isCurrent: () => true, download: noDownload });
  release.enqueue(new TextEncoder().encode('{"downloadUrl":'));
  controller.abort();
  assert.equal(await result, null);
  assert.equal(cancelled, true);
});

test('replacement during metadata reading discards the response before native downloading', async () => {
  let current = true;
  let release;
  const response = new Response(new ReadableStream({ start(stream) { release = stream; } }), {
    headers: { 'Content-Type': 'application/json' },
  });
  const result = handoffPreparedExport(response, {
    signal: new AbortController().signal, isCurrent: () => current, download: noDownload,
  });
  current = false;
  release.enqueue(new TextEncoder().encode(JSON.stringify(valid())));
  release.close();
  assert.equal(await result, null);
});

test('a chunked small response is decoded correctly before starting a native download', async () => {
  const bytes = new TextEncoder().encode(JSON.stringify({ ...valid(), fileName: '合成测试.txt' }));
  const response = new Response(new ReadableStream({
    start(stream) {
      for (let index = 0; index < bytes.length; index += 7) stream.enqueue(bytes.slice(index, index + 7));
      stream.close();
    },
  }), { headers: { 'Content-Type': 'application/json' } });
  const calls = [];
  await handoffPreparedExport(response, {
    signal: new AbortController().signal, isCurrent: () => true, download: (file) => calls.push(file),
  });
  assert.equal(calls[0].fileName, '合成测试.txt');
});

test('browser download uses the ticket URL directly and removes the temporary anchor', () => {
  const previousDocument = globalThis.document;
  const events = [];
  const anchor = { click() { events.push(['click', this.href, this.download]); }, remove() { events.push(['remove']); } };
  globalThis.document = {
    createElement(tag) { assert.equal(tag, 'a'); return anchor; },
    body: { appendChild(element) { assert.equal(element, anchor); events.push(['append']); } },
  };
  try {
    startBrowserExportDownload(valid(), (path) => `http://synthetic-api.test${path}`);
    assert.deepEqual(events, [
      ['append'], ['click', `http://synthetic-api.test${valid().downloadUrl}`, valid().fileName], ['remove'],
    ]);
  } finally { globalThis.document = previousDocument; }
});
