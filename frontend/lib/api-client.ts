const configuredBaseUrl = process.env.NEXT_PUBLIC_API_BASE_URL || 'http://localhost:8000';

export const apiBaseUrl = configuredBaseUrl.replace(/\/$/, '');

export function apiUrl(path: string): string {
  return `${apiBaseUrl}${path.startsWith('/') ? path : `/${path}`}`;
}

export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const response = await fetch(apiUrl(path), {
    ...init,
    credentials: 'include',
  });
  if (response.status === 403 && typeof window !== 'undefined') {
    const body = await response.clone().json().catch(() => null) as { code?: string } | null;
    if (body?.code === 'IP_BLOCKED') window.dispatchEvent(new Event('key-control:ip-blocked'));
  }
  return response;
}
