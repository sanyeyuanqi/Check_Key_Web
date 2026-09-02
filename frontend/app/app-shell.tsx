'use client';

import { useState, type ReactNode } from 'react';
import { apiFetch } from '@/lib/api-client';
import type { AuthUser } from '@/lib/auth-types';
import { platformOptions } from '@/lib/platforms';

export default function AppShell({
  currentUser,
  activeItem,
  children,
}: {
  currentUser: AuthUser;
  activeItem: string;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const [signingOut, setSigningOut] = useState(false);

  async function logout() {
    setSigningOut(true);
    await apiFetch('/api/auth/logout', { method: 'POST' });
    window.location.replace('/login');
  }

  return (
    <div className="min-h-screen bg-[#f5f7fb] text-slate-950">
      {open && <button type="button" aria-label="关闭导航" onClick={() => setOpen(false)} className="fixed inset-0 z-40 bg-slate-950/30 backdrop-blur-sm lg:hidden" />}
      <aside id="primary-sidebar" className={`fixed inset-y-0 left-0 z-50 flex w-72 flex-col border-r border-slate-200 bg-white px-4 py-5 shadow-[12px_0_35px_rgba(15,23,42,0.05)] transition-transform duration-200 lg:translate-x-0 ${open ? 'translate-x-0' : '-translate-x-full'}`}>
        <div className="flex items-center gap-3 px-2 pb-5">
          <span className="grid h-11 w-11 shrink-0 place-items-center rounded-2xl bg-gradient-to-br from-indigo-600 to-violet-600 text-base font-bold text-white shadow-lg shadow-indigo-200">K</span>
          <div className="min-w-0"><p className="font-semibold tracking-tight text-slate-900">Key Control</p><p className="truncate text-xs text-slate-400">密钥管理中心</p></div>
          <button type="button" aria-label="关闭导航" onClick={() => setOpen(false)} className="ml-auto grid h-9 w-9 place-items-center rounded-xl text-xl text-slate-400 hover:bg-slate-100 lg:hidden">×</button>
        </div>

        <nav aria-label="主导航" className="min-h-0 flex-1 overflow-y-auto px-1">
          <p className="px-3 pb-2 pt-2 text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-400">密钥平台</p>
          <div className="space-y-1.5">
            {platformOptions.map((item) => {
              const selected = activeItem === item.slug;
              return <a key={item.slug} href={`/platform/${item.slug}`} aria-current={selected ? 'page' : undefined} onClick={() => setOpen(false)} className={`group flex items-center gap-3 rounded-2xl border px-3 py-2.5 transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 ${selected ? 'border-indigo-200 bg-indigo-50 text-indigo-700 shadow-sm' : 'border-transparent text-slate-600 hover:border-slate-200 hover:bg-slate-50'}`}>
                <span className={`grid h-9 w-9 shrink-0 place-items-center rounded-xl text-sm font-bold text-white shadow-md ${item.tone}`}>{item.mark}</span>
                <span className="min-w-0"><span className="block text-sm font-semibold">{item.title}</span><span className={`block truncate text-[11px] ${selected ? 'text-indigo-400' : 'text-slate-400'}`}>{item.subtitle}</span></span>
              </a>;
            })}
          </div>

          {currentUser.role === 'admin' && <div className="mt-5 border-t border-slate-100 pt-4"><p className="px-3 pb-2 text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-400">系统管理</p><a href="/accounts" aria-current={activeItem === 'accounts' ? 'page' : undefined} onClick={() => setOpen(false)} className={`flex items-center gap-3 rounded-2xl border px-3 py-3 text-sm font-semibold transition ${activeItem === 'accounts' ? 'border-indigo-200 bg-indigo-50 text-indigo-700' : 'border-transparent text-slate-600 hover:border-slate-200 hover:bg-slate-50'}`}><span className="grid h-9 w-9 place-items-center rounded-xl bg-slate-100 text-base">♙</span><span>账号管理</span></a></div>}
        </nav>

        <div className="mt-4 border-t border-slate-100 pt-4">
          <div className="flex items-center gap-3 rounded-2xl bg-slate-50 px-3 py-3"><span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-indigo-100 text-sm font-bold text-indigo-700">{currentUser.username.slice(0, 1).toUpperCase()}</span><div className="min-w-0 flex-1"><p className="truncate text-sm font-semibold text-slate-700">{currentUser.username}</p><p className="text-xs text-slate-400">{currentUser.role === 'admin' ? '管理员' : '普通用户'}</p></div><button type="button" title="退出" aria-label="退出登录" disabled={signingOut} onClick={() => void logout()} className="grid h-9 w-9 shrink-0 place-items-center rounded-xl border border-rose-200 bg-white text-rose-500 shadow-sm transition hover:bg-rose-50 hover:text-rose-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-rose-400 disabled:cursor-wait disabled:opacity-50"><svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={`h-4 w-4 ${signingOut ? 'animate-pulse' : ''}`}><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" /><path d="m16 17 5-5-5-5" /><path d="M21 12H9" /></svg></button></div>
        </div>
      </aside>

      <div className="relative min-h-screen lg:pl-72">
        <header className="sticky top-0 z-30 grid h-16 grid-cols-[2.5rem_1fr_auto] items-center gap-3 border-b border-slate-200 bg-white/95 px-4 backdrop-blur lg:hidden">
          <button type="button" aria-label="打开导航" aria-controls="primary-sidebar" aria-expanded={open} onClick={() => setOpen(true)} className="grid h-10 w-10 place-items-center rounded-xl border border-slate-200 text-xl text-slate-600">☰</button>
          <div className="min-w-0 text-center"><p className="truncate text-sm font-semibold text-slate-800">Key Control</p><p className="truncate text-[11px] text-slate-400">{currentUser.username}</p></div>
          <button type="button" aria-label="刷新全部数据" onClick={() => window.location.reload()} className="grid h-10 w-10 place-items-center rounded-xl border border-slate-200 bg-white text-base font-semibold text-slate-600">↻</button>
        </header>
        {children}
      </div>
    </div>
  );
}
