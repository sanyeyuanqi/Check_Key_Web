const configuredBaseUrl = process.env.NEXT_PUBLIC_API_BASE_URL || 'http://localhost:8000';

export const apiBaseUrl = configuredBaseUrl.replace(/\/$/, '');

const cachedReadPaths = new Set(['/api/keys', '/api/credential-statuses', '/api/valid-key-records', '/api/export-records']);
const freshReadPaths = new Set<string>();
let freshReadsExpireAt = 0;

export function markNextReadsFresh(paths: readonly string[]): void {
  freshReadPaths.clear();
  for (const path of paths) if (cachedReadPaths.has(path)) freshReadPaths.add(path);
  freshReadsExpireAt = freshReadPaths.size > 0 ? Date.now() + 2000 : 0;
}

export function apiUrl(path: string): string {
  return `${apiBaseUrl}${path.startsWith('/') ? path : `/${path}`}`;
}

export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  let requestUrl = apiUrl(path);
  if (freshReadPaths.size > 0 && Date.now() >= freshReadsExpireAt) freshReadPaths.clear();
  if ((init.method ?? 'GET').toUpperCase() === 'GET' && !init.signal?.aborted && freshReadPaths.size > 0) {
    const endpoint = (path.startsWith('/') ? path : `/${path}`).split(/[?#]/, 1)[0];
    if (freshReadPaths.has(endpoint)) {
      const url = new URL(requestUrl);
      url.searchParams.set('refresh', '1');
      requestUrl = url.toString();
      freshReadPaths.delete(endpoint);
    }
  }
  const response = await fetch(requestUrl, {
    ...init,
    credentials: 'include',
  });
  if (response.status === 403 && typeof window !== 'undefined') {
    const body = await response.clone().json().catch(() => null) as { code?: string } | null;
    if (body?.code === 'IP_BLOCKED') window.dispatchEvent(new Event('key-control:ip-blocked'));
  }
  return response;
}
