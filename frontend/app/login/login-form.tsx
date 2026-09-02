'use client';

import { useState } from 'react';
import { apiFetch } from '@/lib/api-client';

export default function LoginForm() {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      const response = await apiFetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });
      const body = await response.json() as { error?: string };
      if (!response.ok) return setError(body.error || '登录失败，请稍后重试');
      window.location.replace('/');
    } catch {
      setError('网络异常，请稍后重试');
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="space-y-5 px-8 py-8">
      <label className="block">
        <span className="mb-2 block text-sm font-medium text-slate-700">账号</span>
        <input autoComplete="username" required maxLength={32} value={username} onChange={(event) => setUsername(event.target.value)} className="h-12 w-full rounded-xl border border-slate-200 bg-slate-50 px-4 text-sm outline-none transition focus:border-indigo-500 focus:bg-white focus:ring-4 focus:ring-indigo-100" placeholder="请输入管理员分配的账号" />
      </label>
      <label className="block">
        <span className="mb-2 block text-sm font-medium text-slate-700">密码</span>
        <input type="password" autoComplete="current-password" required minLength={8} maxLength={256} value={password} onChange={(event) => setPassword(event.target.value)} className="h-12 w-full rounded-xl border border-slate-200 bg-slate-50 px-4 text-sm outline-none transition focus:border-indigo-500 focus:bg-white focus:ring-4 focus:ring-indigo-100" placeholder="请输入密码" />
      </label>
      {error && <p role="alert" className="rounded-xl bg-rose-50 px-4 py-3 text-sm text-rose-700">{error}</p>}
      <button type="submit" disabled={busy} className="h-12 w-full rounded-xl bg-indigo-600 text-sm font-semibold text-white shadow-lg shadow-indigo-200 transition hover:bg-indigo-700 disabled:cursor-wait disabled:opacity-60">{busy ? '正在登录…' : '登录'}</button>
      <p className="text-center text-xs leading-5 text-slate-400">连续登录失败 5 次将临时锁定 15 分钟</p>
    </form>
  );
}
