export type PreparedExport = {
  downloadUrl: string;
  fileName: string;
  count: number;
  bytes: number;
  expiresIn: number;
};

const maximumResponseBytes = 16 * 1024;

export class ExportPreparationError extends Error {
  constructor(status: number) {
    super(status === 413 ? '导出文件过大，请缩小导出范围后重试'
      : status === 503 ? '导出任务较多，请稍后重试'
        : status === 404 ? '所选记录已变更，请刷新后重试' : '导出失败，请稍后重试');
    this.name = 'ExportPreparationError';
  }
}

export function parsePreparedExport(value: unknown): PreparedExport {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('导出文件信息无效');
  const body = value as Record<string, unknown>;
  if (typeof body.downloadUrl !== 'string' || !/^\/api\/exports\/download\/[a-f0-9]{64}$/.test(body.downloadUrl)
    || body.downloadUrl.length !== '/api/exports/download/'.length + 64
    || typeof body.fileName !== 'string' || body.fileName.length > 160
    || !/\.txt$/i.test(body.fileName) || body.fileName.length <= 4
    || /[\\/\u0000-\u001f\u007f-\u009f<>:"|?*\u202a-\u202e\u2066-\u2069]/.test(body.fileName)
    || /^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(body.fileName)
    || !Number.isSafeInteger(body.count) || (body.count as number) < 0
    || !Number.isSafeInteger(body.bytes) || (body.bytes as number) < 0
    || !Number.isSafeInteger(body.expiresIn) || (body.expiresIn as number) < 30 || (body.expiresIn as number) > 3600) {
    throw new Error('导出文件信息无效');
  }
  return {
    downloadUrl: body.downloadUrl,
    fileName: body.fileName,
    count: body.count as number,
    bytes: body.bytes as number,
    expiresIn: body.expiresIn as number,
  };
}

export async function handoffPreparedExport(
  response: Response,
  options: {
    signal: AbortSignal;
    isCurrent: () => boolean;
    download: (prepared: PreparedExport) => void;
  },
): Promise<PreparedExport | null> {
  const active = () => !options.signal.aborted && options.isCurrent();
  if (!active()) return null;
  if (!response.ok) {
    void response.body?.cancel().catch(() => undefined);
    throw new ExportPreparationError(response.status);
  }
  if (!/^application\/json(?:\s*;|\s*$)/i.test(response.headers.get('Content-Type') ?? '')) {
    void response.body?.cancel().catch(() => undefined);
    throw new Error('无法准备导出文件');
  }
  const contentLength = response.headers.get('Content-Length');
  if (contentLength && (!/^\d+$/.test(contentLength) || Number(contentLength) > maximumResponseBytes)) {
    void response.body?.cancel().catch(() => undefined);
    throw new Error('导出文件信息过大');
  }
  const reader = response.body?.getReader();
  if (!reader) throw new Error('导出文件信息无效');
  const cancel = () => { void reader.cancel().catch(() => undefined); };
  options.signal.addEventListener('abort', cancel, { once: true });
  const chunks: Uint8Array[] = [];
  let length = 0;
  try {
    for (;;) {
      const chunk = await reader.read();
      if (!active()) { cancel(); return null; }
      if (chunk.done) break;
      length += chunk.value.byteLength;
      if (length > maximumResponseBytes) {
        cancel();
        throw new Error('导出文件信息过大');
      }
      chunks.push(chunk.value);
    }
    const payload = new Uint8Array(length);
    let offset = 0;
    for (const chunk of chunks) { payload.set(chunk, offset); offset += chunk.byteLength; }
    const prepared = parsePreparedExport(JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(payload)));
    if (!active()) return null;
    options.download(prepared);
    return prepared;
  } catch (error) {
    if (!active()) return null;
    throw error;
  } finally {
    options.signal.removeEventListener('abort', cancel);
    reader.releaseLock();
  }
}

export function startBrowserExportDownload(prepared: PreparedExport, resolveUrl: (path: string) => string): void {
  const checked = parsePreparedExport(prepared);
  const link = document.createElement('a');
  link.href = resolveUrl(checked.downloadUrl);
  link.download = checked.fileName;
  link.rel = 'noopener';
  document.body.appendChild(link);
  try { link.click(); } finally { link.remove(); }
}
