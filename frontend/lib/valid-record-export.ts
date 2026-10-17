export type ValidRecordExportRow = { credentialId: number; storageType: string };
export type ValidRecordExportQuery = { mode: 'prefix' | 'ip'; value: string };

export function validRecordExportRequest(
  mode: 'selected' | 'all',
  provider: string,
  rows: ValidRecordExportRow[],
  query: ValidRecordExportQuery,
): { url: string; init: RequestInit } {
  if (typeof provider !== 'string' || !provider.trim()) {
    throw new Error('请选择服务平台');
  }
  const selectedProvider = provider.trim();

  if (mode === 'selected') {
    if (!Array.isArray(rows) || rows.length < 1 || rows.length > 100) {
      throw new Error('请选择 1 至 100 条密钥');
    }
    const identities = new Set<string>();
    const keys = rows.map((row) => {
      if (!row || !Number.isSafeInteger(row.credentialId) || row.credentialId < 1
        || typeof row.storageType !== 'string' || !row.storageType.trim()) {
        throw new Error('所选密钥信息无效，请刷新后重试');
      }
      const identity = JSON.stringify([row.storageType, row.credentialId]);
      if (identities.has(identity)) throw new Error('所选密钥存在重复，请重新选择');
      identities.add(identity);
      return { id: row.credentialId, type: row.storageType };
    });
    return {
      url: '/api/valid-key-records/batch-export?prepare=1',
      init: {
        method: 'POST', cache: 'no-store', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider: selectedProvider, keys }),
      },
    };
  }

  if (mode !== 'all' || !query || !['prefix', 'ip'].includes(query.mode) || typeof query.value !== 'string') {
    throw new Error('导出条件无效，请重新查询');
  }
  const params = new URLSearchParams({ provider: selectedProvider, prepare: '1' });
  const value = query.value.trim();
  if (value) params.set(query.mode === 'ip' ? 'ip' : 'q', value);
  return {
    url: `/api/valid-key-records/export?${params}`,
    init: { method: 'GET', cache: 'no-store' },
  };
}

function safeTextFilename(value: string): string | null {
  const basename = value.split(/[\\/]/).pop()
    ?.replace(/[\u0000-\u001f\u007f-\u009f<>:"|?*]/g, '')
    .replace(/[\u202a-\u202e\u2066-\u2069]/g, '').trim();
  if (!basename || basename.length <= 4 || !/\.txt$/i.test(basename)
    || /^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(basename)) return null;
  return basename;
}

export function exportDownloadName(contentDisposition: string | null, fallback: string): string {
  const encoded = contentDisposition?.match(/filename\*\s*=\s*UTF-8''([^;]+)/i);
  let name = '';
  if (encoded) {
    try { name = decodeURIComponent(encoded[1].trim().replace(/^"|"$/g, '')); } catch { /* Try the ordinary filename below. */ }
  }
  if (!name) {
    const ordinary = contentDisposition?.match(/filename\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^;]+))/i);
    name = (ordinary?.[1] ?? ordinary?.[2] ?? '').replace(/\\(.)/g, '$1').trim();
  }
  return safeTextFilename(name) || safeTextFilename(fallback) || 'valid-keys.txt';
}
