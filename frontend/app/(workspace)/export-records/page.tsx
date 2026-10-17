'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import { apiFetch } from '@/lib/api-client';
import { storageConfig } from '@/lib/credential-types';
import './export-records.css';

type ExportRecord = {
  id: number;
  createdAt: string;
  username: string;
  provider: string | null;
  status: string;
  statusNames: string[];
  storageType: string | null;
  count: number;
  fileName: string;
  ipAddress: string | null;
  ipRegion: string | null;
};

type ExportRecordsResponse = {
  records: ExportRecord[];
  total: number;
  page: number;
  totalPages: number;
  pageSize: number;
};

const dateTimeFormatter = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit',
});

function formatTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  return dateTimeFormatter.format(date);
}

function exportPlatform(record: ExportRecord) {
  const config = storageConfig(record.storageType);
  const provider = record.provider || config?.provider || '全部平台';
  let typeLabel = config?.label ?? record.storageType;
  if (!typeLabel || typeLabel === provider) return provider;
  if (typeLabel.startsWith(`${provider} `)) typeLabel = typeLabel.slice(provider.length).trim();
  if (provider === 'Azure') typeLabel = typeLabel.replace(/\s+/g, '');
  return `${provider}-${typeLabel}`;
}

function exportScope(record: ExportRecord) {
  return record.statusNames.join('、');
}

export default function ExportRecordsPage() {
  const router = useRouter();
  const [usernameInput, setUsernameInput] = useState('');
  const [ipInput, setIpInput] = useState('');
  const [filters, setFilters] = useState({ username: '', ip: '' });
  const [page, setPage] = useState(1);
  const [data, setData] = useState<ExportRecordsResponse>({ records: [], total: 0, page: 1, totalPages: 1, pageSize: 50 });
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState('');
  const activeRequest = useRef<AbortController | null>(null);

  const loadRecords = useCallback(async (fresh = false) => {
    activeRequest.current?.abort();
    const controller = new AbortController();
    activeRequest.current = controller;
    setBusy(true);
    setError('');
    const params = new URLSearchParams({ page: String(page) });
    if (fresh) params.set('refresh', '1');
    if (filters.username) params.set('username', filters.username);
    if (filters.ip) params.set('ip', filters.ip);
    try {
      const response = await apiFetch(`/api/export-records?${params}`, { cache: 'no-store', signal: controller.signal });
      if (controller.signal.aborted) return;
      if (response.status === 401) {
        router.replace('/login');
        return;
      }
      if (!response.ok) throw new Error('无法读取导出记录，请稍后刷新重试。');
      const body = await response.json() as ExportRecordsResponse;
      if (controller.signal.aborted) return;
      setData(body);
      if (body.page !== page) setPage(body.page);
    } catch {
      if (!controller.signal.aborted) setError('无法读取导出记录，请稍后刷新重试。');
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }, [page, filters.username, filters.ip, router]);

  useEffect(() => {
    const timer = window.setTimeout(() => { void loadRecords(); }, 0);
    return () => {
      window.clearTimeout(timer);
      activeRequest.current?.abort();
    };
  }, [loadRecords]);

  function prepareFilterChange() {
    activeRequest.current?.abort();
    setBusy(true);
    setError('');
  }

  function changePage(nextPage: number) {
    if (nextPage === page) return;
    prepareFilterChange();
    setPage(nextPage);
  }

  function submitSearch(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    const nextFilters = { username: usernameInput.trim(), ip: ipInput.trim() };
    setUsernameInput(nextFilters.username);
    setIpInput(nextFilters.ip);
    if (nextFilters.username === filters.username && nextFilters.ip === filters.ip && page === 1) {
      void loadRecords(true);
      return;
    }
    prepareFilterChange();
    setFilters(nextFilters);
    setPage(1);
  }

  function resetFilters() {
    if (busy) return;
    setUsernameInput('');
    setIpInput('');
    if (!filters.username && !filters.ip && page === 1) {
      void loadRecords(true);
      return;
    }
    prepareFilterChange();
    setFilters({ username: '', ip: '' });
    setPage(1);
  }

  const hasRows = !busy && !error && data.records.length > 0;
  const hasFilters = Boolean(filters.username || filters.ip);
  const message = busy ? '正在读取导出记录…' : error || (hasFilters ? '暂无符合条件的导出记录' : '暂无导出记录');
  const stateMessage = <div role={error ? 'alert' : 'status'} className="loading-message export-records-state"><p>{message}</p>{!busy && !error && <span>{hasFilters ? '请调整用户或 IP 地址后重试。' : '导出密钥后，记录会显示在这里。'}</span>}</div>;
  const firstRecord = data.total > 0 ? (data.page - 1) * data.pageSize + 1 : 0;
  const lastRecord = Math.min(data.page * data.pageSize, data.total);

  return (
    <div className="accounts-page export-records-page">
      <section aria-label="导出记录" aria-busy={busy} className="data-panel export-records-panel">
        <div className="panel-toolbar">
          <form onSubmit={submitSearch} aria-label="导出记录查询" className="export-records-filter-form">
            <label className="search-field export-record-query-field"><input value={usernameInput} onChange={(event) => setUsernameInput(event.target.value)} placeholder="搜索用户" aria-label="用户" maxLength={32} autoComplete="off" /></label>
            <label className="search-field export-record-query-field export-record-ip-field"><input value={ipInput} onChange={(event) => setIpInput(event.target.value)} placeholder="搜索 IP 地址" aria-label="IP地址" maxLength={80} autoComplete="off" spellCheck={false} /></label>
            <button type="submit" disabled={busy} className="ui-button">查询</button>
            <button type="button" disabled={busy} onClick={resetFilters} className="ui-button">重置</button>
            <button type="button" disabled={busy} onClick={() => void loadRecords(true)} className="ui-button export-record-refresh">刷新</button>
          </form>
        </div>
        <div className="export-records-mobile-list">
          {hasRows ? data.records.map((record) => <article key={record.id} className="export-record-mobile-row">
            <p className="cell-strong">{record.fileName}</p>
            <dl>
              <div><dt>用户</dt><dd>{record.username}</dd></div>
              <div><dt>服务平台</dt><dd>{exportPlatform(record)}</dd></div>
              <div><dt>数量</dt><dd>{record.count} 把密钥</dd></div>
              <div><dt>IP 地址</dt><dd>{record.ipAddress ?? '—'}</dd></div>
              <div><dt>地区</dt><dd>{record.ipRegion?.trim() || '—'}</dd></div>
              <div><dt>导出范围</dt><dd>{exportScope(record)}</dd></div>
              <div><dt>导出时间</dt><dd>{formatTime(record.createdAt)}</dd></div>
            </dl>
          </article>) : stateMessage}
        </div>
        <div className="table-scroll"><table className="workspace-table export-records-table">
          <thead><tr>{['用户', '服务平台', '数量', 'IP 地址', '地区', '导出范围', '导出时间', '文件名'].map((heading) => <th key={heading} scope="col">{heading}</th>)}</tr></thead>
          <tbody>{hasRows ? data.records.map((record) => <tr key={record.id}>
            <td className="cell-strong">{record.username}</td>
            <td>{exportPlatform(record)}</td>
            <td className="export-record-count">{record.count}</td>
            <td>{record.ipAddress ?? '—'}</td>
            <td>{record.ipRegion?.trim() || '—'}</td>
            <td>{exportScope(record)}</td>
            <td className="cell-muted export-record-time">{formatTime(record.createdAt)}</td>
            <td className="export-record-file-name">{record.fileName}</td>
          </tr>) : <tr className="empty-table-row"><td colSpan={8}>{stateMessage}</td></tr>}</tbody>
        </table></div>
        <div className="panel-footer">
          <span>{busy ? '正在读取…' : error ? '读取失败' : `显示 ${firstRecord}–${lastRecord} 条，共 ${data.total} 条`}</span>
          {data.totalPages > 1 && <nav aria-label="导出记录分页" className="pagination">
            <button type="button" aria-label="上一页" disabled={busy || data.page <= 1} onClick={() => changePage(data.page - 1)}>上一页</button>
            <span>第 {data.page} / {data.totalPages} 页</span>
            <button type="button" aria-label="下一页" disabled={busy || data.page >= data.totalPages} onClick={() => changePage(data.page + 1)}>下一页</button>
          </nav>}
        </div>
      </section>
    </div>
  );
}
