'use client';

import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { apiFetch } from '@/lib/api-client';
import './access-gate.css';

export default function AccessGate({ children }: { children: ReactNode }) {
  const [state, setState] = useState<'checking' | 'allowed' | 'blocked' | 'unavailable'>('checking');
  const [checking, setChecking] = useState(false);
  const [retry, setRetry] = useState(0);
  const recheck = useCallback(() => setRetry((value) => value + 1), []);

  useEffect(() => {
    let mounted = true;
    let pending = false;
    const controller = new AbortController();
    async function checkAccess() {
      if (pending) return;
      pending = true;
      setChecking(true);
      try {
        const response = await apiFetch('/api/access', { cache: 'no-store', signal: controller.signal });
        if (!mounted) return;
        if (response.ok) setState('allowed');
        else {
          const body = await response.json().catch(() => null) as { code?: string } | null;
          if (body?.code === 'IP_BLOCKED') setState('blocked');
          else setState((current) => current === 'allowed' || current === 'blocked' ? current : 'unavailable');
        }
      } catch {
        if (mounted) setState((current) => current === 'allowed' || current === 'blocked' ? current : 'unavailable');
      } finally {
        pending = false;
        if (mounted) setChecking(false);
      }
    }
    const onBlocked = () => setState('blocked');
    const onVisible = () => { if (!document.hidden) void checkAccess(); };
    window.addEventListener('key-control:ip-blocked', onBlocked);
    window.addEventListener('focus', onVisible);
    document.addEventListener('visibilitychange', onVisible);
    const timer = window.setInterval(onVisible, 30_000);
    void checkAccess();
    return () => {
      mounted = false;
      controller.abort();
      window.clearInterval(timer);
      window.removeEventListener('key-control:ip-blocked', onBlocked);
      window.removeEventListener('focus', onVisible);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [retry]);

  if (state === 'allowed') return children;
  return <main className="access-screen">
    <section className="access-card" aria-live="polite">
      {state === 'checking' ? <p role="status">正在检查访问权限…</p> : <>
        <h1>{state === 'blocked' ? '访问已被禁止' : '暂时无法连接服务'}</h1>
        <p>{state === 'blocked' ? '当前 IP 地址已被禁止访问，请联系超级管理员。' : '请稍后重试。'}</p>
        <button type="button" disabled={checking} onClick={recheck} className="ui-button">{checking ? '正在检查…' : '重新检查'}</button>
      </>}
    </section>
  </main>;
}
