import assert from 'node:assert/strict';
import { test } from 'node:test';
import { spawnSync } from 'node:child_process';

const moduleUrl = new URL('../lib/upload-time-range.ts', import.meta.url).href;

function inTimeZone(timeZone, source) {
  const result = spawnSync(process.execPath, [
    '--experimental-strip-types', '--input-type=module', '-e',
    `import assert from 'node:assert/strict';
     import { parseLocalDateTime, uploadTimeRangeError, addUploadTimeRangeFilter } from ${JSON.stringify(moduleUrl)};
     ${source}`,
  ], { env: { ...process.env, TZ: timeZone }, encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr || result.stdout || String(result.error));
}

test('strict local minute parser validates calendar dates, bounds and format', () => {
  inTimeZone('UTC', `
    assert.equal(parseLocalDateTime('2024-02-29T12:34').toISOString(), '2024-02-29T12:34:00.000Z');
    assert.equal(parseLocalDateTime('0002-01-01T00:00').getFullYear(), 2);
    assert.equal(parseLocalDateTime('0099-12-31T23:59').getFullYear(), 99);
    assert.equal(parseLocalDateTime('9998-12-31T23:59').getFullYear(), 9998);
    for (const value of [
      '', '2026-02-29T12:00', '2026-04-31T12:00', '2026-00-01T12:00', '2026-13-01T12:00',
      '2026-01-00T12:00', '2026-01-32T12:00', '2026-01-01T24:00', '2026-01-01T12:60',
      '0000-01-01T00:00', '0001-01-01T00:00', '9999-01-01T00:00',
      '2026-1-01T12:00', '2026-01-01T1:00', '2026-01-01T12:0', '2026-01-01 12:00',
      '2026-01-01T12:00:00', '2026-01-01T12:00Z', '2026-01-01T12:00+08:00',
      ' 2026-01-01T12:00', '2026-01-01T12:00 ',
    ]) assert.equal(parseLocalDateTime(value), null, value);
  `);
});

test('range validation permits empty or same-minute boundaries and rejects invalid order', () => {
  inTimeZone('UTC', `
    const valid = '2026-10-17T12:34';
    assert.equal(uploadTimeRangeError('', ''), '');
    assert.equal(uploadTimeRangeError(valid, ''), '');
    assert.equal(uploadTimeRangeError('', valid), '');
    assert.equal(uploadTimeRangeError(valid, valid), '');
    assert.equal(uploadTimeRangeError('invalid', valid), '请选择有效的开始日期时间');
    assert.equal(uploadTimeRangeError(valid, 'invalid'), '请选择有效的结束日期时间');
    assert.equal(uploadTimeRangeError('invalid', 'invalid'), '请选择有效的开始日期时间');
    assert.equal(uploadTimeRangeError('2026-10-17T12:35', valid), '结束时间不能早于开始时间');
  `);
});

test('local UTC+08:00 boundaries become UTC instants and include the final minute', () => {
  inTimeZone('Asia/Shanghai', `
    const params = new URLSearchParams({ provider: 'OpenAI', status: 'valid' });
    addUploadTimeRangeFilter(params, { start: '2026-10-17T00:00', end: '2026-10-17T23:59' });
    assert.equal(params.get('created_from'), '2026-10-16T16:00:00.000Z');
    assert.equal(params.get('created_before'), '2026-10-17T16:00:00.000Z');
    assert.equal(params.get('provider'), 'OpenAI');
    assert.equal(params.get('status'), 'valid');
  `);
});

test('same-minute, midnight and year-end filters preserve half-open precision', () => {
  inTimeZone('UTC', `
    const params = new URLSearchParams();
    addUploadTimeRangeFilter(params, { start: '2026-12-31T23:59', end: '2026-12-31T23:59' });
    assert.equal(params.get('created_from'), '2026-12-31T23:59:00.000Z');
    assert.equal(params.get('created_before'), '2027-01-01T00:00:00.000Z');
    const crossDay = new URLSearchParams();
    addUploadTimeRangeFilter(crossDay, { start: '2026-10-16T23:58', end: '2026-10-17T00:01' });
    assert.equal(crossDay.get('created_from'), '2026-10-16T23:58:00.000Z');
    assert.equal(crossDay.get('created_before'), '2026-10-17T00:02:00.000Z');
  `);
});

test('one-sided and cleared ranges preserve unrelated query parameters', () => {
  inTimeZone('UTC', `
    const startOnly = new URLSearchParams('type=openai_official');
    addUploadTimeRangeFilter(startOnly, { start: '2026-10-17T12:00', end: '' });
    assert.equal(startOnly.get('created_from'), '2026-10-17T12:00:00.000Z');
    assert.equal(startOnly.has('created_before'), false);
    const endOnly = new URLSearchParams('type=openai_official');
    addUploadTimeRangeFilter(endOnly, { start: '', end: '2026-10-17T12:00' });
    assert.equal(endOnly.has('created_from'), false);
    assert.equal(endOnly.get('created_before'), '2026-10-17T12:01:00.000Z');
    const empty = new URLSearchParams('type=openai_official&created_from=existing');
    const before = empty.toString();
    addUploadTimeRangeFilter(empty, { start: '', end: '' });
    assert.equal(empty.toString(), before);
  `);
});

test('invalid ranges throw without partially mutating the query', () => {
  inTimeZone('UTC', `
    for (const [range, message] of [
      [{ start: 'invalid', end: '2026-10-17T12:00' }, '请选择有效的开始日期时间'],
      [{ start: '2026-10-17T12:00', end: '2026-02-29T12:00' }, '请选择有效的结束日期时间'],
      [{ start: '2026-10-17T12:01', end: '2026-10-17T12:00' }, '结束时间不能早于开始时间'],
    ]) {
      const params = new URLSearchParams('status=valid');
      assert.throws(() => addUploadTimeRangeFilter(params, range), { message });
      assert.equal(params.toString(), 'status=valid');
    }
  `);
});

test('DST gaps are rejected and valid local boundaries use their actual offsets', () => {
  inTimeZone('America/New_York', `
    assert.equal(parseLocalDateTime('2026-03-08T02:30'), null);
    assert.equal(uploadTimeRangeError('2026-03-08T02:30', ''), '请选择有效的开始日期时间');
    assert.equal(uploadTimeRangeError('', '2026-03-08T02:30'), '请选择有效的结束日期时间');
    assert.equal(parseLocalDateTime('2026-03-08T01:59').toISOString(), '2026-03-08T06:59:00.000Z');
    assert.equal(parseLocalDateTime('2026-03-08T03:00').toISOString(), '2026-03-08T07:00:00.000Z');
    const params = new URLSearchParams();
    addUploadTimeRangeFilter(params, { start: '2026-03-08T01:59', end: '2026-03-08T03:00' });
    assert.equal(params.get('created_from'), '2026-03-08T06:59:00.000Z');
    assert.equal(params.get('created_before'), '2026-03-08T07:01:00.000Z');
    assert.equal(parseLocalDateTime('2026-11-01T01:30').toISOString(), '2026-11-01T05:30:00.000Z');
  `);
});
