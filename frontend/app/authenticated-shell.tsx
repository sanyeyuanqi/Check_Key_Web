'use client';

import { useEffect, useState, type ReactNode } from 'react';
import AppShell from '@/app/app-shell';
import { apiFetch } from '@/lib/api-client';
import type { AuthUser } from '@/lib/auth-types';

export default function AuthenticatedShell({
  activeItem,
  adminOnly = false,
  children,
}: {
  activeItem: string;
  adminOnly?: boolean;
  children: (user: AuthUser) => ReactNode;
}) {
  const [user, setUser] = useState<AuthUser | null>(null);

  useEffect(() => {
    let mounted = true;
    void apiFetch('/api/auth/me', { cache: 'no-store' }).then(async (response) => {
      if (!response.ok) {
        window.location.replace('/login');
        return;
      }
      const body = await response.json() as { user: AuthUser };
      if (adminOnly && body.user.role !== 'admin') {
        window.location.replace('/platform/aws');
        return;
      }
      if (mounted) setUser(body.user);
    }).catch(() => window.location.replace('/login'));
    return () => { mounted = false; };
  }, [adminOnly]);

  if (!user) {
    return <main className="grid min-h-screen place-items-center bg-[#f5f7fb] text-sm text-slate-400">正在验证登录状态…</main>;
  }
  return <AppShell currentUser={user} activeItem={activeItem}>{children(user)}</AppShell>;
}
