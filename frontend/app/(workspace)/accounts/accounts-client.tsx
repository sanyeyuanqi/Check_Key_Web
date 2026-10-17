'use client';

import { useCallback, useEffect, useState } from 'react';
import { apiFetch } from '@/lib/api-client';
import { Modal } from '@/app/workspace-ui';

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
    if (user.id === currentUserId) return <span className="panel-note">当前账号</span>;
    return <div className="row-actions">
      <button type="button" disabled={acting || busy} onClick={() => void resetPassword(user)} className="ui-button ui-button--small ui-button--soft">{acting ? '处理中…' : '重置密码'}</button>
      {user.role === 'user' && <button type="button" disabled={acting || busy} onClick={() => void toggleAccount(user)} className={`ui-button ui-button--small ${user.active ? 'ui-button--danger' : 'ui-button--success'}`}>{user.active ? '禁用' : '启用'}</button>}
    </div>;
  }

  function accountStatus(user: ManagedUserRow) {
    const locked = user.lockedUntil && user.lockedUntil > new Date().toISOString();
    return <span className={`status-badge ${locked ? 'status-insufficient' : user.active ? 'status-valid' : ''}`}>{locked ? '临时锁定' : user.active ? '正常' : '已禁用'}</span>;
  }

  return (
    <div className="accounts-page">
      {error && !createOpen && <p role="alert" className="inline-alert">{error}</p>}
      <section aria-label="账号列表" className="data-panel accounts-panel">
        <div className="panel-toolbar">
          <div className="panel-title"><h2>账号列表</h2><span className="count-badge">{users.length} 个账号</span></div>
          <div className="toolbar-actions"><button type="button" disabled={busy} onClick={() => void loadUsers()} className="ui-button">刷新数据</button><button type="button" onClick={openCreator} className="ui-button ui-button--primary">生成账号</button></div>
        </div>
        <div className="mobile-account-list">
          {busy && users.length === 0 ? <p role="status" className="loading-message">正在读取账号…</p> : users.map((user) => <article key={user.id} className="mobile-data-row">
            <div className="mobile-row-heading"><div><p className="cell-strong">{user.username}</p><p className="mobile-row-details">{user.role === 'admin' ? '管理员' : '普通用户'} · 创建于 {fmt(user.createdAt)}</p></div>{accountStatus(user)}</div>
            <div className="mobile-row-footer"><p className="mobile-row-details">最后登录：{fmt(user.lastLoginAt)}</p>{accountActions(user)}</div>
          </article>)}
        </div>
        <div className="table-scroll"><table className="workspace-table">
          <thead><tr>{['账号', '角色', '状态', '创建时间', '最后登录', '操作'].map((head) => <th key={head} scope="col">{head}</th>)}</tr></thead>
          <tbody>{busy && users.length === 0 ? <tr><td colSpan={6}><p role="status" className="loading-message">正在读取账号…</p></td></tr> : users.map((user) => <tr key={user.id}>
            <td className="cell-strong">{user.username}</td><td><span className="account-role">{user.role === 'admin' ? '管理员' : '普通用户'}</span></td><td>{accountStatus(user)}</td><td className="cell-muted">{fmt(user.createdAt)}</td><td className="cell-muted">{fmt(user.lastLoginAt)}</td><td>{accountActions(user)}</td>
          </tr>)}</tbody>
        </table></div>
        <div className="panel-footer"><span>共 {users.length} 个账号</span><span>生成或重置的密码只显示一次</span></div>
      </section>

      {createOpen && <Modal title="生成登录账号" close={() => { setCreateOpen(false); setError(''); }}>
        <p className="modal-description">填写账号并选择角色，系统将生成随机密码。账号留空可自动生成。</p>
        <form onSubmit={createAccount} className="form-stack">
          <label><span className="field-label">账号</span><input autoFocus value={username} onChange={(event) => setUsername(event.target.value)} maxLength={32} className="ui-input" placeholder="留空则自动生成" /></label>
          <div><span className="field-label">角色</span><div className="role-choices"><button type="button" aria-pressed={role === 'user'} onClick={() => setRole('user')} className={`type-choice ${role === 'user' ? 'is-selected' : ''}`}>普通用户</button><button type="button" aria-pressed={role === 'admin'} onClick={() => setRole('admin')} className={`type-choice ${role === 'admin' ? 'is-selected' : ''}`}>管理员</button></div></div>
          {error && <p role="alert" className="inline-alert">{error}</p>}
          <button type="submit" disabled={busy} className="ui-button ui-button--primary ui-button--block">{busy ? '正在生成…' : '生成账号'}</button>
        </form>
      </Modal>}
      {generated && <Modal title={generated.mode === 'reset' ? '密码重置成功' : '账号生成成功'} close={() => { setGenerated(null); setCopied(false); }}>
        <div className="inline-notice">请复制并妥善保存，关闭后将不再显示此密码。</div>
        <div className="generated-fields"><div><p className="field-label">账号</p><code>{generated.username}</code></div><div><p className="field-label">随机密码</p><code>{generated.password}</code></div></div>
        <button type="button" onClick={() => void copyGeneratedAccount()} className="ui-button ui-button--primary ui-button--block">{copied ? '账号密码已复制' : '复制账号密码'}</button>
      </Modal>}
    </div>
  );
}
