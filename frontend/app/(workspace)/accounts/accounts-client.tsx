'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { apiFetch } from '@/lib/api-client';
import type { AuthRole } from '@/lib/auth-types';
import { Modal } from '@/app/workspace-ui';
import IpAccessManagement from './ip-access-management';
import './accounts.css';

type ManagedUserRow = {
  id: number;
  username: string;
  role: AuthRole;
  active: number | boolean;
  createdAt: string;
  lastLoginAt: string | null;
  lockedUntil: string | null;
};
type GeneratedAccount = { username: string; password: string; role: AuthRole; mode: 'created' | 'reset' };
type AccountResponse = { account?: Omit<GeneratedAccount, 'mode'>; error?: unknown; detail?: unknown };
type AccountMutation = { username: string; role: 'admin' | 'user' }
  | { id: number; action: 'reset_password' }
  | { id: number; action: 'set_active'; active: boolean };
type PendingAction = { user: ManagedUserRow; action: 'reset_password' | 'disable' };

const roleNames: Record<AuthRole, string> = { super_admin: '超级管理员', admin: '管理员', user: '普通用户' };

function fmt(value?: string | null) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  }).format(date);
}

function apiMessage(body: { error?: unknown; detail?: unknown }, fallback: string) {
  const message = typeof body.error === 'string' ? body.error : body.detail;
  return typeof message === 'string' && message.trim() ? message : fallback;
}

function generatedAccount(body: AccountResponse, mode: GeneratedAccount['mode']): GeneratedAccount {
  if (!body.account || typeof body.account.username !== 'string' || typeof body.account.password !== 'string'
    || !body.account.username || !body.account.password) throw new Error('Incomplete account response');
  return { ...body.account, mode };
}

export default function AccountsClient({ currentUserId, currentUserRole }: { currentUserId: number; currentUserRole: AuthRole }) {
  const isSuperAdmin = currentUserRole === 'super_admin';
  const [view, setView] = useState<'accounts' | 'ip'>('accounts');
  const [users, setUsers] = useState<ManagedUserRow[]>([]);
  const [statusCheckedAt, setStatusCheckedAt] = useState(0);
  const [username, setUsername] = useState('');
  const [role, setRole] = useState<'admin' | 'user'>('user');
  const [busy, setBusy] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [generated, setGenerated] = useState<GeneratedAccount | null>(null);
  const [copied, setCopied] = useState(false);
  const [copying, setCopying] = useState(false);
  const [copyError, setCopyError] = useState('');
  const [actingId, setActingId] = useState<number | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [pendingAction, setPendingAction] = useState<PendingAction | null>(null);
  const listRequest = useRef<AbortController | null>(null);
  const mutationRequest = useRef<AbortController | null>(null);
  const copyTimer = useRef<number | null>(null);

  const loadUsers = useCallback(async () => {
    listRequest.current?.abort();
    const controller = new AbortController();
    listRequest.current = controller;
    setBusy(true);
    setError('');
    try {
      const response = await apiFetch('/api/admin/users', { cache: 'no-store', signal: controller.signal });
      if (controller.signal.aborted) return;
      if (response.status === 401) return window.location.replace('/login');
      if (!response.ok) {
        const body = await response.json().catch(() => ({})) as AccountResponse;
        if (!controller.signal.aborted) setError(apiMessage(body, response.status === 403 ? '没有账号管理权限' : '无法读取账号列表，请稍后重试'));
        return;
      }
      const body = await response.json() as { users: ManagedUserRow[] };
      if (controller.signal.aborted) return;
      if (!Array.isArray(body.users)) throw new Error('Invalid account list');
      setUsers(body.users);
      setStatusCheckedAt(Date.now());
    } catch {
      if (!controller.signal.aborted) setError('无法读取账号列表，请稍后重试');
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }, []);

  useEffect(() => {
    if (view !== 'accounts') return;
    const timer = window.setTimeout(() => { void loadUsers(); }, 0);
    return () => {
      window.clearTimeout(timer);
      listRequest.current?.abort();
    };
  }, [loadUsers, view]);

  useEffect(() => () => {
    mutationRequest.current?.abort();
    if (copyTimer.current !== null) window.clearTimeout(copyTimer.current);
  }, []);

  function canManage(user: ManagedUserRow) {
    if (user.id === currentUserId || user.role === 'super_admin') return false;
    return isSuperAdmin ? user.role === 'admin' || user.role === 'user' : currentUserRole === 'admin' && user.role === 'user';
  }

  function openCreator() {
    if (busy || saving) return;
    setUsername('');
    setRole('user');
    setError('');
    setGenerated(null);
    setCreateOpen(true);
  }

  async function mutateAccount(payload: AccountMutation, fallback: string, onSuccess: (body: AccountResponse) => void) {
    if (mutationRequest.current || busy) return;
    const controller = new AbortController();
    mutationRequest.current = controller;
    setSaving(true);
    setActingId('id' in payload ? payload.id : null);
    setError('');
    let failureMessage = fallback;
    try {
      const response = await apiFetch('/api/admin/users', {
        method: 'id' in payload ? 'PATCH' : 'POST',
        cache: 'no-store',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      if (response.status === 401) return window.location.replace('/login');
      const body = await response.json().catch(() => ({})) as AccountResponse;
      if (controller.signal.aborted) return;
      if (!response.ok) {
        failureMessage = apiMessage(body, response.status === 403 ? '没有操作该账号的权限' : fallback);
        throw new Error('Account operation failed');
      }
      onSuccess(body);
      await loadUsers();
    } catch {
      if (!controller.signal.aborted) setError(failureMessage);
    } finally {
      if (mutationRequest.current === controller) {
        mutationRequest.current = null;
        if (!controller.signal.aborted) {
          setSaving(false);
          setActingId(null);
        }
      }
    }
  }

  async function createAccount(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (currentUserRole !== 'admin' && !isSuperAdmin) return;
    await mutateAccount({ username: username.trim(), role: isSuperAdmin ? role : 'user' }, '账号创建失败，请稍后重试', (body) => {
      setGenerated(generatedAccount(body, 'created'));
      setCopied(false);
      setCopyError('');
      setCreateOpen(false);
      setUsername('');
    });
  }

  async function copyGeneratedAccount() {
    if (!generated || copying) return;
    setCopying(true);
    setCopyError('');
    try {
      await navigator.clipboard.writeText(`${generated.username}\n${generated.password}`);
      setCopied(true);
      if (copyTimer.current !== null) window.clearTimeout(copyTimer.current);
      copyTimer.current = window.setTimeout(() => setCopied(false), 1800);
    } catch {
      setCopyError('复制失败，请手动复制账号和密码');
    } finally {
      setCopying(false);
    }
  }

  function requestReset(user: ManagedUserRow) {
    if (!canManage(user) || busy || saving) return;
    setError('');
    setPendingAction({ user, action: 'reset_password' });
  }

  async function toggleAccount(user: ManagedUserRow) {
    if (!canManage(user) || busy || saving) return;
    setError('');
    if (user.active) setPendingAction({ user, action: 'disable' });
    else await mutateAccount({ id: user.id, action: 'set_active', active: true }, '账号状态更新失败，请稍后重试', () => {});
  }

  async function confirmPendingAction() {
    if (!pendingAction || !canManage(pendingAction.user)) return;
    const { user, action } = pendingAction;
    const payload: AccountMutation = action === 'reset_password'
      ? { id: user.id, action: 'reset_password' }
      : { id: user.id, action: 'set_active', active: false };
    await mutateAccount(payload, action === 'reset_password' ? '密码重置失败，请稍后重试' : '账号状态更新失败，请稍后重试', (body) => {
      if (action === 'reset_password') {
        setGenerated(generatedAccount(body, 'reset'));
        setCopied(false);
        setCopyError('');
      }
      setPendingAction(null);
    });
  }

  function accountActions(user: ManagedUserRow) {
    if (user.id === currentUserId) return <span className="panel-note">当前账号</span>;
    if (!canManage(user)) return <span className="panel-note">—</span>;
    const acting = actingId === user.id;
    return <div className="row-actions">
      <button type="button" disabled={busy || saving} onClick={() => requestReset(user)} className="ui-button ui-button--small ui-button--soft">{acting ? '处理中…' : '重置密码'}</button>
      <button type="button" disabled={busy || saving} onClick={() => void toggleAccount(user)} className={`ui-button ui-button--small ${user.active ? 'ui-button--danger' : 'ui-button--success'}`}>{user.active ? '禁用' : '启用'}</button>
    </div>;
  }

  function accountStatus(user: ManagedUserRow) {
    const locked = Boolean(user.lockedUntil && new Date(user.lockedUntil).getTime() > statusCheckedAt);
    return <span className={`status-badge ${locked ? 'status-insufficient' : user.active ? 'status-valid' : ''}`}>{locked ? '临时锁定' : user.active ? '正常' : '已禁用'}</span>;
  }

  const emptyMessage = busy ? '正在读取账号…' : error ? '账号列表读取失败' : '暂无账号';

  return (
    <div className="accounts-page accounts-management-page">
      {isSuperAdmin && <div role="group" aria-label="管理功能" className="accounts-management-tabs">
        <button type="button" disabled={saving} aria-pressed={view === 'accounts'} onClick={() => setView('accounts')} className={`accounts-management-tab ${view === 'accounts' ? 'is-selected' : ''}`}>账号管理</button>
        <button type="button" disabled={saving} aria-pressed={view === 'ip'} onClick={() => setView('ip')} className={`accounts-management-tab ${view === 'ip' ? 'is-selected' : ''}`}>IP 访问管理</button>
      </div>}
      {view === 'ip' && isSuperAdmin ? <IpAccessManagement /> : <>
        {error && !createOpen && !pendingAction && <p role="alert" className="inline-alert">{error}</p>}
        <section aria-label="账号列表" aria-busy={busy || saving} className="data-panel accounts-panel">
          <div className="panel-toolbar">
            <div className="panel-title"><h2>账号列表</h2><span className="count-badge">{users.length} 个账号</span></div>
            <div className="toolbar-actions"><button type="button" disabled={busy || saving} onClick={() => void loadUsers()} className="ui-button">刷新数据</button><button type="button" disabled={busy || saving} onClick={openCreator} className="ui-button ui-button--primary">生成账号</button></div>
          </div>
          <div className="mobile-account-list">
            {users.length === 0 ? <p role="status" className="loading-message">{emptyMessage}</p> : users.map((user) => <article key={user.id} className="mobile-data-row">
              <div className="mobile-row-heading"><div><p className="cell-strong">{user.username}</p><p className="mobile-row-details">{roleNames[user.role]} · 创建于 {fmt(user.createdAt)}</p></div>{accountStatus(user)}</div>
              <div className="mobile-row-footer"><p className="mobile-row-details">最后登录：{fmt(user.lastLoginAt)}</p>{accountActions(user)}</div>
            </article>)}
          </div>
          <div className="table-scroll"><table className="workspace-table accounts-management-table">
            <thead><tr>{['账号', '角色', '状态', '创建时间', '最后登录', '操作'].map((head) => <th key={head} scope="col">{head}</th>)}</tr></thead>
            <tbody>{users.length === 0 ? <tr><td colSpan={6}><p role="status" className="loading-message">{emptyMessage}</p></td></tr> : users.map((user) => <tr key={user.id}>
              <td className="cell-strong">{user.username}</td><td><span className="account-role">{roleNames[user.role]}</span></td><td>{accountStatus(user)}</td><td className="cell-muted">{fmt(user.createdAt)}</td><td className="cell-muted">{fmt(user.lastLoginAt)}</td><td>{accountActions(user)}</td>
            </tr>)}</tbody>
          </table></div>
          <div className="panel-footer"><span>共 {users.length} 个账号</span><span>生成或重置的密码只显示一次</span></div>
        </section>
      </>}

      {createOpen && <Modal title="生成登录账号" close={() => { if (!saving) { setCreateOpen(false); setError(''); } }}>
        <p className="modal-description">{isSuperAdmin ? '填写账号并选择角色，系统将生成随机密码。账号留空可自动生成。' : '填写账号，系统将生成普通用户和随机密码。账号留空可自动生成。'}</p>
        <form onSubmit={createAccount} className="form-stack">
          <label><span className="field-label">账号</span><input autoFocus value={username} onChange={(event) => setUsername(event.target.value)} disabled={saving} maxLength={32} className="ui-input" placeholder="留空则自动生成" /></label>
          {isSuperAdmin ? <div><span className="field-label">角色</span><div className="role-choices"><button type="button" disabled={saving} aria-pressed={role === 'user'} onClick={() => setRole('user')} className={`type-choice ${role === 'user' ? 'is-selected' : ''}`}>普通用户</button><button type="button" disabled={saving} aria-pressed={role === 'admin'} onClick={() => setRole('admin')} className={`type-choice ${role === 'admin' ? 'is-selected' : ''}`}>管理员</button></div></div> : <div><span className="field-label">角色</span><span className="account-role">普通用户</span></div>}
          {error && <p role="alert" className="inline-alert">{error}</p>}
          <button type="submit" disabled={busy || saving} className="ui-button ui-button--primary ui-button--block">{saving ? '正在生成…' : '生成账号'}</button>
        </form>
      </Modal>}
      {pendingAction && <Modal title={pendingAction.action === 'reset_password' ? '重置密码' : '禁用账号'} close={() => { if (!saving) { setPendingAction(null); setError(''); } }}>
        <p className="modal-description">{pendingAction.action === 'reset_password' ? `确定要重置 ${pendingAction.user.username} 的密码吗？旧密码和现有登录会立即失效。` : `确定要禁用 ${pendingAction.user.username} 吗？该账号的现有登录会立即失效。`}</p>
        {error && <p role="alert" className="inline-alert">{error}</p>}
        <div className="modal-actions"><button type="button" disabled={saving} onClick={() => { setPendingAction(null); setError(''); }} className="ui-button">取消</button><button type="button" disabled={busy || saving} onClick={() => void confirmPendingAction()} className="ui-button ui-button--primary">{saving ? '处理中…' : '确定'}</button></div>
      </Modal>}
      {generated && <Modal title={generated.mode === 'reset' ? '密码重置成功' : '账号生成成功'} close={() => { setGenerated(null); setCopied(false); setCopyError(''); }}>
        <div className="inline-notice">请复制并妥善保存，关闭后将不再显示此密码。</div>
        <div className="generated-fields"><div><p className="field-label">账号</p><code>{generated.username}</code></div><div><p className="field-label">随机密码</p><code>{generated.password}</code></div></div>
        {copyError && <p role="alert" className="inline-alert">{copyError}</p>}
        <button type="button" disabled={copying} onClick={() => void copyGeneratedAccount()} className="ui-button ui-button--primary ui-button--block">{copying ? '正在复制…' : copied ? '账号密码已复制' : '复制账号密码'}</button>
      </Modal>}
    </div>
  );
}
