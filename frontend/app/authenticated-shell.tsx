'use client';

import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';
import { useRouter } from 'next/navigation';
import AppShell from '@/app/app-shell';
import { apiFetch } from '@/lib/api-client';
import type { AuthUser } from '@/lib/auth-types';

const AuthUserContext = createContext<AuthUser | null>(null);

export function useAuthUser(): AuthUser {
  const user = useContext(AuthUserContext);
  if (!user) throw new Error('useAuthUser must be used within AuthenticatedShell');
  return user;
}

export default function AuthenticatedShell({ children }: { children: ReactNode }) {
  const router = useRouter();
  const [user, setUser] = useState<AuthUser | null>(null);
  const [contentVersion, setContentVersion] = useState(0);

  useEffect(() => {
    let mounted = true;
    void apiFetch('/api/auth/me', { cache: 'no-store' }).then(async (response) => {
      if (!response.ok) {
        if (mounted) router.replace('/login');
        return;
      }
      const body = await response.json() as { user: AuthUser };
      if (mounted) setUser(body.user);
    }).catch(() => { if (mounted) router.replace('/login'); });
    return () => { mounted = false; };
  }, [router]);

  if (!user) {
    return <main className="grid min-h-screen place-items-center bg-background text-[13px] text-[#7b8798]">正在验证登录状态…</main>;
  }
  return <AuthUserContext.Provider value={user}><AppShell currentUser={user} onRefresh={() => setContentVersion((version) => version + 1)}><main id="main-content" key={contentVersion}>{children}</main></AppShell></AuthUserContext.Provider>;
}
