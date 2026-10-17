'use client';

import { useState, type ReactNode } from 'react';
import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { apiFetch } from '@/lib/api-client';
import type { AuthUser } from '@/lib/auth-types';
import { platformOptions } from '@/lib/platforms';
import { SidebarBrandIcon, SidebarUtilityIcon } from '@/app/sidebar-icon';
import './workspace-navigation.css';

export default function AppShell({ currentUser, onRefresh, children }: {
  currentUser: AuthUser;
  onRefresh: () => void;
  children: ReactNode;
}) {
  const pathname = usePathname();
  const router = useRouter();
  const activeItem = pathname === '/accounts' ? 'accounts' : pathname === '/export-records' ? 'export-records' : pathname === '/valid-key-records' ? 'valid-key-records' : pathname.split('/')[2];
  const [open, setOpen] = useState(false);
  const [signingOut, setSigningOut] = useState(false);

  async function logout() {
    setSigningOut(true);
    await apiFetch('/api/auth/logout', { method: 'POST' });
    router.replace('/login');
  }

  return (
    <div className="workspace">
      {open && <button type="button" aria-label="关闭导航" onClick={() => setOpen(false)} className="sidebar-backdrop" />}
      <aside id="primary-sidebar" className={`workspace-sidebar ${open ? 'is-open' : ''}`}>
        <button type="button" aria-label="关闭导航" onClick={() => setOpen(false)} className="ui-text-button sidebar-close">关闭</button>
        <nav aria-label="主导航" className="platform-navigation">
          <div role="group" aria-label="服务平台" className="navigation-group">
            {platformOptions.map((item) => {
              const selected = activeItem === item.slug;
              const href = `/platform/${item.slug}`;
              return (
                <Link key={item.slug} href={href} prefetch={false} aria-current={selected ? 'page' : undefined} onClick={() => setOpen(false)} onMouseEnter={() => { if (!pathname.startsWith('/platform/')) router.prefetch(href); }} onFocus={() => { if (!pathname.startsWith('/platform/')) router.prefetch(href); }} onNavigate={(event) => {
                  if (!pathname.startsWith('/platform/')) return;
                  // Platform pages share one client view; pathname changes update its provider immediately.
                  event.preventDefault();
                  if (!selected) window.history.pushState(null, '', href);
                }} className={`platform-link ${selected ? 'is-selected' : ''}`}>
                  <SidebarBrandIcon slug={item.slug} />
                  <span>{item.slug === 'aws' ? 'AWS Bedrock' : item.title}</span>
                </Link>
              );
            })}
          </div>
          <div role="group" aria-label="管理功能" className="navigation-group">
            {(currentUser.role === 'admin' || currentUser.role === 'super_admin') && <Link href="/accounts" prefetch={false} onMouseEnter={() => router.prefetch('/accounts')} onFocus={() => router.prefetch('/accounts')} aria-current={activeItem === 'accounts' ? 'page' : undefined} onClick={() => setOpen(false)} className={`platform-link accounts-link ${activeItem === 'accounts' ? 'is-selected' : ''}`}><SidebarUtilityIcon name="accounts" /><span>账号管理</span></Link>}
            <Link href="/export-records" prefetch={false} onMouseEnter={() => router.prefetch('/export-records')} onFocus={() => router.prefetch('/export-records')} aria-current={activeItem === 'export-records' ? 'page' : undefined} onClick={() => setOpen(false)} className={`platform-link ${activeItem === 'export-records' ? 'is-selected' : ''}`}><SidebarUtilityIcon name="export-records" /><span>导出记录</span></Link>
            <Link href="/valid-key-records" prefetch={false} onMouseEnter={() => router.prefetch('/valid-key-records')} onFocus={() => router.prefetch('/valid-key-records')} aria-current={activeItem === 'valid-key-records' ? 'page' : undefined} onClick={() => setOpen(false)} className={`platform-link ${activeItem === 'valid-key-records' ? 'is-selected' : ''}`}><SidebarUtilityIcon name="valid-key-records" /><span>有效密钥记录</span></Link>
          </div>
        </nav>
        <div className="sidebar-user">
          <span aria-hidden="true" className="sidebar-user-avatar">{Array.from(currentUser.username.trim())[0]?.toUpperCase() || 'U'}</span>
          <div className="user-copy"><p title={currentUser.username}>{currentUser.username}</p><span>{currentUser.role === 'super_admin' ? '超级管理员' : currentUser.role === 'admin' ? '工作空间管理员' : '工作空间成员'}</span></div>
          <button type="button" title="退出登录" aria-label="退出登录" disabled={signingOut} onClick={() => void logout()} className="ui-text-button logout-button">退出</button>
        </div>
      </aside>
      <div className="workspace-main">
        <header className="workspace-mobile-header">
          <button type="button" aria-label="打开导航" aria-controls="primary-sidebar" aria-expanded={open} onClick={() => setOpen(true)} className="ui-text-button">导航</button>
          <span>{activeItem === 'valid-key-records' ? '有效密钥记录' : activeItem === 'export-records' ? '导出记录' : platformOptions.find((item) => item.slug === activeItem)?.title ?? '账号管理'}</span>
          <button type="button" aria-label="刷新当前页面数据" onClick={onRefresh} className="ui-text-button">刷新</button>
        </header>
        {children}
      </div>
    </div>
  );
}
