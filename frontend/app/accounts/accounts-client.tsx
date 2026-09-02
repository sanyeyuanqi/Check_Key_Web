'use client';

import { useCallback, useEffect, useState } from 'react';
import { apiFetch } from '@/lib/api-client';

type ManagedUserRow = {
  id: number;
  username: string;
  role: 'admin' | 'user';
  active: number | boolean;
  createdAt: string;
  lastLoginAt: string | null;
  lockedUntil: string | null;
};

function fmt(value?: string | null) {
  if (!value) return '—';
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  }).format(new Date(value));
}

export default function AccountsClient({ currentUserId }: { currentUserId: number }) {
  const [users, setUsers] = useState<ManagedUserRow[]>([]);
  const [username, setUsername] = useState('');
  const [role, setRole] = useState<'admin' | 'user'>('user');
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState('');
  const [generated, setGenerated] = useState<{ username: string; password: string; role: string; mode: 'created' | 'reset' } | null>(null);
  const [copied, setCopied] = useState(false);
  const [actingId, setActingId] = useState<number | null>(null);
  const [createOpen, setCreateOpen] = useState(false);

  const loadUsers = useCallback(async () => {
    setBusy(true);
    const response = await apiFetch('/api/admin/users', { cache: 'no-store' });
    if (response.status === 401) return window.location.replace('/login');
    if (response.ok) {
      const body = await response.json() as { users: ManagedUserRow[] };
      setUsers(body.users);
      setError('');
    } else setError('无法读取账号列表');
    setBusy(false);
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => { void loadUsers(); }, 0);
    return () => window.clearTimeout(timer);
  }, [loadUsers]);

  function openCreator() {
    setUsername('');
    setRole('user');
    setError('');
    setGenerated(null);
    setCreateOpen(true);
  }

  async function createAccount(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError('');
    setGenerated(null);
    const response = await apiFetch('/api/admin/users', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, role }),
    });
    const body = await response.json() as { account?: { username: string; password: string; role: string }; error?: string };
    if (!response.ok || !body.account) setError(body.error || '账号创建失败');
    else {
      setGenerated({ ...body.account, mode: 'created' });
      setCopied(false);
      setCreateOpen(false);
      setUsername('');
      await loadUsers();
    }
    setBusy(false);
  }

  async function copyGeneratedAccount() {
    if (!generated) return;
    await navigator.clipboard.writeText(`${generated.username}\n${generated.password}`);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1800);
  }

  async function resetPassword(user: ManagedUserRow) {
    if (!window.confirm(`确定要重置 ${user.username} 的密码吗？旧密码和现有登录会立即失效。`)) return;
    setActingId(user.id);
    setError('');
    setGenerated(null);
    const response = await apiFetch('/api/admin/users', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: user.id, action: 'reset_password' }),
    });
    if (response.status === 401) return window.location.replace('/login');
    const body = await response.json() as { account?: { username: string; password: string; role: string }; error?: string };
    if (!response.ok || !body.account) setError(body.error || '密码重置失败');
    else {
      setGenerated({ ...body.account, mode: 'reset' });
      setCopied(false);
      await loadUsers();
    }
    setActingId(null);
  }

  async function toggleAccount(user: ManagedUserRow) {
    const nextActive = !Boolean(user.active);
    if (!nextActive && !window.confirm(`确定要禁用 ${user.username} 吗？该账号的现有登录会立即失效。`)) return;
    setActingId(user.id);
    setError('');
    const response = await apiFetch('/api/admin/users', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: user.id, action: 'set_active', active: nextActive }),
    });
    if (response.status === 401) return window.location.replace('/login');
    const body = await response.json() as { error?: string };
    if (!response.ok) setError(body.error || '账号状态更新失败');
    else await loadUsers();
    setActingId(null);
  }

  function accountActions(user: ManagedUserRow) {
    const acting = actingId === user.id;
    if (user.id === currentUserId) return <span className="text-xs font-medium text-slate-400">当前账号</span>;
    return <div className="flex flex-wrap items-center gap-2"><button type="button" disabled={acting || busy} onClick={() => void resetPassword(user)} className="rounded-lg border border-indigo-200 bg-indigo-50 px-3 py-1.5 text-xs font-semibold text-indigo-700 transition hover:bg-indigo-100 disabled:opacity-50">{acting ? '处理中…' : '重置密码'}</button>{user.role === 'user' && <button type="button" disabled={acting || busy} onClick={() => void toggleAccount(user)} className={`rounded-lg border px-3 py-1.5 text-xs font-semibold transition disabled:opacity-50 ${user.active ? 'border-rose-200 bg-rose-50 text-rose-600 hover:bg-rose-100' : 'border-emerald-200 bg-emerald-50 text-emerald-700 hover:bg-emerald-100'}`}>{user.active ? '禁用' : '启用'}</button>}</div>;
  }

  return (
    <div className="mx-auto max-w-[1200px] px-4 py-6 sm:px-5 lg:px-8 lg:py-8">
      <div className="flex flex-wrap items-end justify-between gap-4 lg:min-h-16"><h1 className="text-2xl font-semibold tracking-tight">账号管理</h1><div className="flex items-center gap-2"><button type="button" onClick={openCreator} className="h-10 rounded-xl bg-indigo-600 px-4 text-sm font-semibold text-white shadow-sm shadow-indigo-200 transition hover:bg-indigo-700">＋ 生成账号</button><button type="button" onClick={() => window.location.reload()} className="hidden h-10 items-center rounded-xl border border-slate-200 bg-white px-4 text-sm font-semibold text-slate-600 shadow-sm transition hover:border-indigo-200 hover:bg-indigo-50 hover:text-indigo-600 lg:inline-flex">↻ 刷新数据</button></div></div>

      {error && !createOpen && <p role="alert" className="mt-5 rounded-xl bg-rose-50 px-4 py-3 text-sm text-rose-700">{error}</p>}
      <section className="mt-6 overflow-hidden rounded-2xl border border-slate-200/80 bg-white shadow-[0_10px_35px_rgba(15,23,42,0.04)]"><div className="border-b border-slate-100 px-4 py-4 sm:px-5"><h2 className="font-semibold">账号列表</h2><p className="mt-1 text-xs text-slate-400">共 {users.length} 个账号；重置后的密码只显示一次。</p></div>
        <div className="divide-y divide-slate-100 md:hidden">{busy && users.length === 0 ? <p className="px-4 py-14 text-center text-sm text-slate-400">正在读取账号…</p> : users.map((user) => <article key={user.id} className="p-4"><div className="flex items-start justify-between gap-3"><div className="min-w-0"><p className="truncate font-semibold text-slate-800">{user.username}</p><p className="mt-1 text-xs text-slate-400">{user.role === 'admin' ? '管理员' : '普通用户'} · 创建于 {fmt(user.createdAt)}</p></div><span className={`shrink-0 rounded-full px-2.5 py-1 text-xs font-semibold ${user.active ? 'bg-emerald-50 text-emerald-700' : 'bg-slate-100 text-slate-500'}`}>{user.lockedUntil && user.lockedUntil > new Date().toISOString() ? '临时锁定' : user.active ? '正常' : '已禁用'}</span></div><div className="mt-3 flex items-end justify-between gap-3"><p className="text-xs text-slate-400">最后登录：{fmt(user.lastLoginAt)}</p>{accountActions(user)}</div></article>)}</div>
        <div className="hidden overflow-x-auto md:block"><table className="w-full min-w-[920px] text-left text-sm"><thead className="bg-slate-50 text-xs text-slate-500"><tr><th className="px-5 py-3">账号</th><th className="px-5 py-3">角色</th><th className="px-5 py-3">状态</th><th className="px-5 py-3">创建时间</th><th className="px-5 py-3">最后登录</th><th className="px-5 py-3">操作</th></tr></thead><tbody>{busy && users.length === 0 ? <tr><td colSpan={6} className="px-5 py-16 text-center text-slate-400">正在读取账号…</td></tr> : users.map((user) => <tr key={user.id} className="border-t border-slate-100"><td className="px-5 py-4 font-semibold">{user.username}</td><td className="px-5 py-4">{user.role === 'admin' ? '管理员' : '普通用户'}</td><td className="px-5 py-4"><span className={`rounded-full px-2.5 py-1 text-xs font-semibold ${user.active ? 'bg-emerald-50 text-emerald-700' : 'bg-slate-100 text-slate-500'}`}>{user.lockedUntil && user.lockedUntil > new Date().toISOString() ? '临时锁定' : user.active ? '正常' : '已禁用'}</span></td><td className="px-5 py-4 text-xs text-slate-500">{fmt(user.createdAt)}</td><td className="px-5 py-4 text-xs text-slate-500">{fmt(user.lastLoginAt)}</td><td className="px-5 py-4">{accountActions(user)}</td></tr>)}</tbody></table></div>
      </section>

      {createOpen && <Modal title="生成登录账号" close={() => { setCreateOpen(false); setError(''); }}><p className="text-sm text-slate-500">账号可留空自动生成。选择角色后生成随机密码。</p><form onSubmit={createAccount} className="mt-5 space-y-4"><label className="block"><span className="mb-2 block text-sm font-medium text-slate-700">账号</span><input autoFocus value={username} onChange={(event) => setUsername(event.target.value)} maxLength={32} className="h-11 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 text-sm outline-none focus:border-indigo-500 focus:bg-white focus:ring-4 focus:ring-indigo-100" placeholder="留空则自动生成" /></label><div><span className="mb-2 block text-sm font-medium text-slate-700">角色</span><div className="grid h-11 grid-cols-2 rounded-xl border border-slate-200 bg-slate-50 p-1"><button type="button" onClick={() => setRole('user')} className={`rounded-lg px-3 text-sm font-medium ${role === 'user' ? 'bg-white text-indigo-700 shadow-sm' : 'text-slate-500'}`}>普通用户</button><button type="button" onClick={() => setRole('admin')} className={`rounded-lg px-3 text-sm font-medium ${role === 'admin' ? 'bg-white text-indigo-700 shadow-sm' : 'text-slate-500'}`}>管理员</button></div></div>{error && <p role="alert" className="rounded-xl bg-rose-50 px-4 py-3 text-sm text-rose-700">{error}</p>}<button type="submit" disabled={busy} className="h-11 w-full rounded-xl bg-indigo-600 text-sm font-semibold text-white shadow-sm shadow-indigo-200 transition hover:bg-indigo-700 disabled:cursor-wait disabled:opacity-60">{busy ? '正在生成…' : '生成账号'}</button></form></Modal>}
      {generated && <Modal title={generated.mode === 'reset' ? '密码重置成功' : '账号生成成功'} close={() => { setGenerated(null); setCopied(false); }}><div className="rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">请立即复制并妥善保存，关闭后将不再显示此密码。</div><div className="mt-5 space-y-3"><div><p className="mb-1.5 text-xs font-semibold text-slate-500">账号</p><code className="block break-all rounded-xl bg-slate-50 px-4 py-3 text-sm text-slate-800">{generated.username}</code></div><div><p className="mb-1.5 text-xs font-semibold text-slate-500">随机密码</p><code className="block break-all rounded-xl bg-slate-50 px-4 py-3 text-sm text-slate-800">{generated.password}</code></div></div><button type="button" onClick={() => void copyGeneratedAccount()} className="mt-5 h-11 w-full rounded-xl bg-indigo-600 text-sm font-semibold text-white shadow-sm shadow-indigo-200 transition hover:bg-indigo-700">{copied ? '账号密码已复制' : '复制账号密码'}</button></Modal>}
    </div>
  );
}

function Modal({ title, close, children }: { title: string; close: () => void; children: React.ReactNode }) {
  return <div className="fixed inset-0 z-[70] grid place-items-center bg-slate-950/35 p-4 backdrop-blur-sm" onMouseDown={(event) => { if (event.target === event.currentTarget) close(); }}><section role="dialog" aria-modal="true" aria-label={title} className="w-full max-w-md rounded-3xl border border-slate-200 bg-white p-5 shadow-2xl sm:p-6"><div className="mb-5 flex items-center justify-between gap-4"><h2 className="text-lg font-semibold tracking-tight text-slate-900">{title}</h2><button type="button" aria-label="关闭" onClick={close} className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-slate-100 text-xl text-slate-500 transition hover:bg-slate-200">×</button></div>{children}</section></div>;
}
