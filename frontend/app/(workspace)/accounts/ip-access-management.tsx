'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { apiFetch } from '@/lib/api-client';
import './ip-access-management.css';

type IpBlock = {
  ipAddress: string;
  active: boolean;
  reason: string;
  createdAt: string;
  updatedAt: string;
  createdBy: string | null;
};

type IpBlockChange = { ip: string; reason: string } | { ip: string; active: boolean };

function formatTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  }).format(date);
}

async function responseMessage(response: Response, fallback: string): Promise<string> {
  try {
    const body: unknown = await response.json();
    const detail = body && typeof body === 'object'
      ? 'error' in body && typeof body.error === 'string' ? body.error : 'detail' in body ? body.detail : null
      : null;
    if (typeof detail === 'string' && detail.trim() && detail.length <= 300 && !/<[^>]*>/.test(detail)) {
      return detail.trim();
    }
  } catch { /* Use the local message for non-JSON responses. */ }
  return fallback;
}

export default function IpAccessManagement() {
  const [blocks, setBlocks] = useState<IpBlock[]>([]);
  const [clientIp, setClientIp] = useState<string | null>(null);
  const [ip, setIp] = useState('');
  const [reason, setReason] = useState('');
  const [loading, setLoading] = useState(true);
  const [actingIp, setActingIp] = useState<string | null>(null);
  const [forbidden, setForbidden] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const activeRead = useRef<AbortController | null>(null);
  const activeWrite = useRef<AbortController | null>(null);
  const working = actingIp !== null;

  const loadBlocks = useCallback(async () => {
    activeRead.current?.abort();
    const controller = new AbortController();
    activeRead.current = controller;
    setLoading(true);
    setError('');
    try {
      const response = await apiFetch('/api/admin/ip-blocks', { cache: 'no-store', signal: controller.signal });
      if (controller.signal.aborted) return;
      if (response.status === 401) return window.location.replace('/login');
      if (response.status === 403) {
        setForbidden(true);
        setBlocks([]);
        setClientIp(null);
        setError('没有权限管理 IP 地址');
        return;
      }
      if (!response.ok) {
        const message = await responseMessage(response, '无法读取 IP 封禁记录，请稍后重试');
        if (!controller.signal.aborted) setError(message);
        return;
      }
      const body = await response.json() as { blocks: IpBlock[]; clientIp: string | null };
      if (controller.signal.aborted) return;
      if (!Array.isArray(body.blocks)) throw new Error('Invalid IP blocks response');
      setBlocks(body.blocks);
      setClientIp(typeof body.clientIp === 'string' ? body.clientIp : null);
      setForbidden(false);
    } catch {
      if (!controller.signal.aborted) setError('无法读取 IP 封禁记录，请稍后重试');
    } finally {
      if (activeRead.current === controller && !controller.signal.aborted) {
        activeRead.current = null;
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => { void loadBlocks(); }, 0);
    return () => {
      window.clearTimeout(timer);
      activeRead.current?.abort();
      activeWrite.current?.abort();
      activeRead.current = null;
      activeWrite.current = null;
    };
  }, [loadBlocks]);

  async function changeBlock(change: IpBlockChange) {
    if (activeWrite.current || loading || forbidden) return;
    const creating = !('active' in change);
    const controller = new AbortController();
    activeWrite.current = controller;
    setActingIp(creating ? 'create' : change.ip);
    setError('');
    setNotice('');
    try {
      const response = await apiFetch('/api/admin/ip-blocks', {
        method: creating ? 'POST' : 'PATCH',
        cache: 'no-store',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(change),
        signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      if (response.status === 401) return window.location.replace('/login');
      if (response.status === 403) {
        setForbidden(true);
        setError('没有权限管理 IP 地址');
        return;
      }
      if (!response.ok) {
        const message = await responseMessage(response, 'IP 状态更新失败，请稍后重试');
        if (!controller.signal.aborted) setError(message);
        return;
      }
      if (creating) {
        setIp('');
        setReason('');
        setNotice('IP 地址已封禁');
      } else {
        setNotice(change.active ? 'IP 地址已重新封禁' : '已解除封禁');
      }
      await loadBlocks();
    } catch {
      if (!controller.signal.aborted) setError('IP 状态更新失败，请稍后重试');
    } finally {
      if (activeWrite.current === controller && !controller.signal.aborted) {
        activeWrite.current = null;
        setActingIp(null);
      }
    }
  }

  async function submitBlock(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (activeWrite.current || loading || forbidden) return;
    const address = ip.trim();
    if (!address) {
      setError('请输入 IP 地址');
      return;
    }
    if (address === clientIp) return;
    await changeBlock({ ip: address, reason: reason.trim() });
  }

  function blockAction(block: IpBlock) {
    const currentIp = clientIp === block.ipAddress;
    return <button
      type="button"
      disabled={loading || working || forbidden || (!block.active && currentIp)}
      title={!block.active && currentIp ? '当前访问 IP' : undefined}
      onClick={() => {
        if (!block.active && currentIp) return;
        void changeBlock({ ip: block.ipAddress, active: !block.active });
      }}
      className={`ui-button ui-button--small ${block.active ? 'ui-button--soft' : 'ui-button--danger'}`}
    >{actingIp === block.ipAddress ? '处理中…' : block.active ? '解除封禁' : '重新封禁'}</button>;
  }

  function blockStatus(block: IpBlock) {
    return <span className={`status-badge ${block.active ? 'status-invalid' : 'status-valid'}`}>{block.active ? '已封禁' : '已解除'}</span>;
  }

  const stateMessage = loading ? '正在读取 IP 封禁记录…' : error ? '暂时无法显示 IP 封禁记录' : '暂无 IP 封禁记录';
  const activeCount = blocks.filter((block) => block.active).length;

  return (
    <section aria-label="IP 访问管理" aria-busy={loading || working} className="data-panel ip-access-panel">
      <div className="panel-toolbar">
        <div className="panel-title"><h2>IP 访问管理</h2><span className="count-badge">{activeCount} 个已封禁</span></div>
        <div className="ip-access-toolbar-actions"><span className="ip-access-client">当前访问 IP：<code>{loading && !clientIp ? '读取中…' : clientIp || '—'}</code></span><button type="button" disabled={loading || working} onClick={() => void loadBlocks()} className="ui-button">刷新</button></div>
      </div>
      <form onSubmit={submitBlock} aria-label="封禁 IP 地址" className="ip-access-form">
        <label className="ip-access-field"><span>IP 地址</span><input value={ip} onChange={(event) => setIp(event.target.value)} disabled={loading || working || forbidden} required maxLength={80} autoComplete="off" spellCheck={false} autoCapitalize="none" placeholder="输入 IPv4 或 IPv6 地址" className="ui-input" /></label>
        <label className="ip-access-field ip-access-reason-field"><span>备注</span><input value={reason} onChange={(event) => setReason(event.target.value)} disabled={loading || working || forbidden} maxLength={200} autoComplete="off" placeholder="填写备注（可选）" className="ui-input" /></label>
        <button type="submit" disabled={loading || working || forbidden || !ip.trim() || ip.trim() === clientIp} title={ip.trim() && ip.trim() === clientIp ? '当前访问 IP' : undefined} className="ui-button ui-button--primary">{actingIp === 'create' ? '正在封禁…' : '封禁 IP'}</button>
      </form>
      {error && <p role="alert" className="inline-alert ip-access-feedback">{error}</p>}
      {notice && <p role="status" className="ip-access-notice ip-access-feedback">{notice}</p>}
      <div role="region" aria-label="IP 封禁列表" tabIndex={0} className="table-scroll ip-access-table-scroll">
        <table className="workspace-table ip-access-table">
          <thead><tr>{['IP 地址', '备注', '状态', '更新时间', '操作'].map((heading) => <th key={heading} scope="col">{heading}</th>)}</tr></thead>
          <tbody>{blocks.length > 0 ? blocks.map((block) => <tr key={block.ipAddress}>
            <td><code className="ip-access-address">{block.ipAddress}</code>{clientIp === block.ipAddress && <span className="ip-access-current-mark">当前访问 IP</span>}</td>
            <td className="ip-access-reason">{block.reason || '—'}</td>
            <td>{blockStatus(block)}</td>
            <td className="cell-muted ip-access-time">{formatTime(block.updatedAt)}</td>
            <td>{blockAction(block)}</td>
          </tr>) : <tr><td colSpan={5}><p role="status" className="ip-access-state">{stateMessage}</p></td></tr>}</tbody>
        </table>
      </div>
      <div className="ip-access-mobile-list">
        {blocks.length > 0 ? blocks.map((block) => <article key={block.ipAddress} className="ip-access-mobile-row">
          <div className="ip-access-mobile-heading"><div><code className="ip-access-address">{block.ipAddress}</code>{clientIp === block.ipAddress && <span className="ip-access-current-mark">当前访问 IP</span>}</div>{blockStatus(block)}</div>
          <p className="ip-access-reason">{block.reason || '—'}</p>
          <div className="ip-access-mobile-footer"><span>更新于 {formatTime(block.updatedAt)}</span>{blockAction(block)}</div>
        </article>) : <p role="status" className="ip-access-state">{stateMessage}</p>}
      </div>
      <div className="panel-footer"><span>共 {blocks.length} 条记录</span></div>
    </section>
  );
}
