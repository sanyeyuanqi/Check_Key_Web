import { resultBucket } from './credential-types';

export type SelectableKeyRow = { id: number; storageType: string; status: string };

export class BulkCopyError extends Error {
  readonly status?: number;

  constructor(message: string, status?: number) {
    super(message);
    this.name = 'BulkCopyError';
    this.status = status;
  }
}

export function keySelectionId(row: SelectableKeyRow): string {
  return JSON.stringify([row.storageType, resultBucket(row.status), row.id]);
}

export async function selectedKeysText(
  rows: SelectableKeyRow[],
  request: (path: string, init?: RequestInit) => Promise<Response>,
  signal?: AbortSignal,
): Promise<string> {
  if (rows.length < 1 || rows.length > 100) throw new BulkCopyError('请选择 1 至 100 条密钥');

  const identities = new Set<string>();
  const keys = rows.map((row) => {
    if (!Number.isSafeInteger(row.id) || row.id < 1 || typeof row.storageType !== 'string'
      || !row.storageType.trim() || typeof row.status !== 'string') {
      throw new BulkCopyError('所选密钥信息无效，请刷新后重试');
    }
    const identity = keySelectionId(row);
    if (identities.has(identity)) throw new BulkCopyError('所选密钥存在重复，请重新选择');
    identities.add(identity);
    return { id: row.id, type: row.storageType, status: resultBucket(row.status) };
  });

  signal?.throwIfAborted();
  const response = await request('/api/keys/batch-reveal', {
    method: 'POST',
    cache: 'no-store',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ keys }),
    signal,
  });
  signal?.throwIfAborted();
  if (!response.ok) {
    throw new BulkCopyError(response.status === 401 ? '登录已失效，请重新登录' : '无法批量读取密钥，请刷新后重试', response.status);
  }

  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw new BulkCopyError('密钥返回信息无效，请重试');
  }
  signal?.throwIfAborted();
  const secrets = body && typeof body === 'object' && 'secrets' in body ? body.secrets : null;
  if (!Array.isArray(secrets) || secrets.length !== rows.length
    || !secrets.every((secret): secret is string => typeof secret === 'string' && secret.trim().length > 0)) {
    throw new BulkCopyError('密钥返回信息不完整，请刷新后重试');
  }
  return secrets.join('\n');
}
