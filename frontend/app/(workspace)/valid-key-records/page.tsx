'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { Checkbox, ConfigProvider, Select, type ThemeConfig } from 'antd';
import { useRouter } from 'next/navigation';
import { apiFetch } from '@/lib/api-client';
import { resultBucket } from '@/lib/credential-types';
import { platformOptions } from '@/lib/platforms';
import { Modal } from '@/app/workspace-ui';
import './valid-key-records.css';

type ValidKeyRecord = {
  credentialId: number;
  keyPrefix: string;
  masked: string;
  provider: string;
  storageType: string;
  status: string;
  statusName: string;
  statusTone: 'valid' | 'recovery' | 'insufficient' | 'invalid' | 'neutral';
  firstValidAt: string;
  lastValidAt: string;
  updatedAt: string;
  ipAddress: string | null;
};

type ValidKeyRecordsResponse = {
  records: ValidKeyRecord[];
  total: number;
  page: number;
  totalPages: number;
  pageSize: number;
};

type QueryMode = 'prefix' | 'ip';

const providerOptions = platformOptions.map((item) => ({ value: item.provider, label: item.provider }));
const selectionTheme: ThemeConfig = {
  token: { colorPrimary: '#13795f', colorText: '#385b4d', colorBorder: '#dfe7e1', controlItemBgActive: '#eaf4ef', controlItemBgActiveHover: '#dceee2', controlHeight: 36, borderRadius: 6, fontSize: 12, fontFamily: 'Segoe UI, Microsoft YaHei, sans-serif' },
  components: {
    Checkbox: { colorPrimary: '#13795f', colorPrimaryHover: '#0e654f', colorBorder: '#c6d7ce', controlInteractiveSize: 14, borderRadiusSM: 3 },
    Select: { selectorBg: '#fcfdfc', optionFontSize: 12, optionHeight: 30, optionPadding: '5px 7px', optionSelectedBg: '#eaf4ef', optionSelectedColor: '#13795f', optionActiveBg: '#f3f8f5' },
  },
};

function recordSelectionId(record: ValidKeyRecord): string {
  return JSON.stringify([record.storageType, record.credentialId]);
}

function formatTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
  }).format(date);
}

function statusBadge(record: ValidKeyRecord) {
  return <span className={`status-badge status-${record.statusTone || 'neutral'}`}>{record.statusName || record.status}</span>;
}

export default function ValidKeyRecordsPage() {
  const router = useRouter();
  const [provider, setProvider] = useState('OpenAI');
  const [queryMode, setQueryMode] = useState<QueryMode>('prefix');
  const [queryInput, setQueryInput] = useState('');
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(1);
  const [data, setData] = useState<ValidKeyRecordsResponse>({ records: [], total: 0, page: 1, totalPages: 1, pageSize: 100 });
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [selectedRecordIds, setSelectedRecordIds] = useState<Set<string>>(() => new Set());
  const [actionId, setActionId] = useState<number | null>(null);
  const [reveal, setReveal] = useState<{ masked: string; secret: string } | null>(null);
  const activeRequest = useRef<AbortController | null>(null);
  const secretRequest = useRef<AbortController | null>(null);
  const noticeTimer = useRef<number | null>(null);

  const loadRecords = useCallback(async () => {
    setSelectedRecordIds(new Set());
    activeRequest.current?.abort();
    const controller = new AbortController();
    activeRequest.current = controller;
    setBusy(true);
    setError('');
    const params = new URLSearchParams({ page: String(page), provider });
    if (query) params.set(queryMode === 'ip' ? 'ip' : 'q', query);
    try {
      const response = await apiFetch(`/api/valid-key-records?${params}`, { cache: 'no-store', signal: controller.signal });
      if (controller.signal.aborted) return;
      if (response.status === 401) {
        router.replace('/login');
        return;
      }
      if (!response.ok) throw new Error('Unable to load valid key records');
      const body = await response.json() as ValidKeyRecordsResponse;
      if (controller.signal.aborted) return;
      setData(body);
      if (body.page !== page) setPage(body.page);
    } catch {
      if (!controller.signal.aborted) {
        setData({ records: [], total: 0, page: 1, totalPages: 1, pageSize: 100 });
        setError('无法读取有效密钥记录，请稍后刷新重试。');
      }
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }, [page, provider, queryMode, query, router]);

  useEffect(() => {
    const timer = window.setTimeout(() => { void loadRecords(); }, 0);
    return () => {
      window.clearTimeout(timer);
      activeRequest.current?.abort();
    };
  }, [loadRecords]);

  useEffect(() => () => {
    secretRequest.current?.abort();
    if (noticeTimer.current !== null) window.clearTimeout(noticeTimer.current);
  }, []);

  function prepareFilterChange() {
    setSelectedRecordIds(new Set());
    activeRequest.current?.abort();
    setBusy(true);
    setError('');
  }

  function changePage(nextPage: number) {
    if (nextPage === page) return;
    prepareFilterChange();
    setPage(nextPage);
  }

  function changeProvider(nextProvider: string) {
    if (nextProvider === provider) return;
    prepareFilterChange();
    setProvider(nextProvider);
    setPage(1);
  }

  function changeQueryMode(nextMode: QueryMode) {
    if (nextMode === queryMode) return;
    prepareFilterChange();
    setQueryMode(nextMode);
    setQueryInput('');
    setQuery('');
    setPage(1);
  }

  function submitSearch(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const nextQuery = queryInput.trim();
    if (nextQuery === query && page === 1) void loadRecords();
    else {
      prepareFilterChange();
      setQuery(nextQuery);
      setPage(1);
    }
  }

  function showNotice(message: string) {
    if (noticeTimer.current !== null) window.clearTimeout(noticeTimer.current);
    setNotice(message);
    noticeTimer.current = window.setTimeout(() => setNotice(''), 2600);
  }

  async function readKey(record: ValidKeyRecord, action: 'reveal' | 'copy') {
    secretRequest.current?.abort();
    const controller = new AbortController();
    secretRequest.current = controller;
    setActionId(record.credentialId);
    const params = new URLSearchParams({ type: record.storageType, status: resultBucket(record.status) });
    try {
      const response = await apiFetch(`/api/keys/${record.credentialId}/reveal?${params}`, { cache: 'no-store', signal: controller.signal });
      if (controller.signal.aborted) return;
      if (response.status === 401) {
        router.replace('/login');
        return;
      }
      if (!response.ok) throw new Error('Unable to read key');
      const body = await response.json() as { secret: string };
      if (controller.signal.aborted) return;
      if (action === 'reveal') setReveal({ masked: record.masked, secret: body.secret });
      else {
        await navigator.clipboard.writeText(body.secret);
        if (!controller.signal.aborted) showNotice('密钥已复制');
      }
    } catch {
      if (!controller.signal.aborted) showNotice(action === 'copy' ? '复制失败，请稍后重试' : '无法读取完整密钥，请稍后重试');
    } finally {
      if (!controller.signal.aborted) setActionId(null);
    }
  }

  function rowActions(record: ValidKeyRecord) {
    return <div className="row-actions"><button type="button" disabled={actionId === record.credentialId} aria-label="查看密钥" onClick={() => void readKey(record, 'reveal')} className="text-action">查看</button><button type="button" disabled={actionId === record.credentialId} aria-label="复制密钥" onClick={() => void readKey(record, 'copy')} className="text-action">复制</button></div>;
  }

  const selectedRows = data.records.filter((record) => selectedRecordIds.has(recordSelectionId(record)));
  const allPageSelected = data.records.length > 0 && selectedRows.length === data.records.length;
  const somePageSelected = selectedRows.length > 0 && !allPageSelected;

  function selectRecord(record: ValidKeyRecord, checked: boolean) {
    if (busy) return;
    setSelectedRecordIds((current) => {
      const next = new Set(current);
      if (checked) next.add(recordSelectionId(record));
      else next.delete(recordSelectionId(record));
      return next;
    });
  }

  function selectPage(checked: boolean) {
    if (busy) return;
    setSelectedRecordIds(checked ? new Set(data.records.map(recordSelectionId)) : new Set());
  }

  const hasRows = !busy && !error && data.records.length > 0;
  const hasQuery = Boolean(query);
  const stateMessage = <div role={error ? 'alert' : 'status'} className="valid-key-records-state"><p>{busy ? '正在读取有效密钥记录…' : error || (hasQuery ? '暂无符合条件的有效密钥记录' : '暂无有效密钥记录')}</p>{!busy && !error && <span>{hasQuery ? '请尝试其他查询条件。' : '密钥检测有效后会收录，即使后续状态变化也会保留。'}</span>}</div>;
  const firstRecord = data.total > 0 ? (data.page - 1) * data.pageSize + 1 : 0;
  const lastRecord = Math.min(data.page * data.pageSize, data.total);

  return (
    <ConfigProvider theme={selectionTheme}>
      <div className="accounts-page valid-key-records-page">
        <div role="group" aria-label="服务平台" className="valid-key-records-platforms">
          {providerOptions.map((item) => <button key={item.value} type="button" aria-pressed={provider === item.value} onClick={() => changeProvider(item.value)} className={`valid-key-platform-option ${provider === item.value ? 'is-selected' : ''}`}>{item.label}</button>)}
        </div>
        <section aria-label="有效密钥记录" aria-busy={busy} className="data-panel valid-key-records-panel">
          <div className="valid-key-records-filters">
            <span className="valid-key-selected-count" aria-live="polite">已选 {selectedRows.length} 条</span>
            <form onSubmit={submitSearch} aria-label="有效密钥记录查询" className="search-form">
              <Select<QueryMode> aria-label="查询方式" className="valid-key-query-mode" value={queryMode} options={[{ value: 'prefix', label: '密钥前缀' }, { value: 'ip', label: 'IP 地址' }]} popupMatchSelectWidth onChange={changeQueryMode} />
              <label className="search-field"><input value={queryInput} onChange={(event) => setQueryInput(event.target.value)} placeholder={queryMode === 'ip' ? '搜索 IP 地址' : '搜索密钥前缀'} aria-label={queryMode === 'ip' ? '搜索 IP 地址' : '搜索密钥前缀'} maxLength={queryMode === 'ip' ? 80 : 32} autoComplete="off" spellCheck={false} /></label>
              <button type="submit" disabled={busy} className="ui-button search-button">搜索</button>
            </form>
          </div>
          <div className="valid-key-records-mobile-list">
            <div className="valid-key-mobile-selection"><Checkbox aria-label="全选本页有效密钥" checked={allPageSelected} indeterminate={somePageSelected} disabled={busy || data.records.length === 0} onChange={(event) => selectPage(event.target.checked)}>全选本页</Checkbox><span aria-live="polite">已选 {selectedRows.length} 条</span></div>
            {hasRows ? data.records.map((record) => <article key={recordSelectionId(record)} className={`valid-key-record-mobile-row ${selectedRecordIds.has(recordSelectionId(record)) ? 'is-selected' : ''}`}>
              <div className="mobile-row-heading"><div className="valid-key-mobile-heading"><Checkbox aria-label={`选择有效密钥 ${record.keyPrefix}`} checked={selectedRecordIds.has(recordSelectionId(record))} disabled={busy} onChange={(event) => selectRecord(record, event.target.checked)} /><code className="key-prefix">{record.keyPrefix}</code></div>{statusBadge(record)}</div>
              <code className="key-mask">{record.masked}</code>
              <dl>
                <div><dt>首次有效</dt><dd>{formatTime(record.firstValidAt)}</dd></div>
                <div><dt>最近有效</dt><dd>{formatTime(record.lastValidAt)}</dd></div>
                <div><dt>IP 地址</dt><dd>{record.ipAddress ?? '—'}</dd></div>
              </dl>
              {rowActions(record)}
            </article>) : stateMessage}
          </div>
          <div className="table-scroll"><table className="workspace-table valid-key-records-table">
            <thead><tr><th scope="col" className="valid-key-select-cell"><Checkbox aria-label="全选本页有效密钥" checked={allPageSelected} indeterminate={somePageSelected} disabled={busy || data.records.length === 0} onChange={(event) => selectPage(event.target.checked)} /></th>{['密钥前32位', '当前状态', '首次有效', '最近有效', 'IP 地址', '操作'].map((heading) => <th key={heading} scope="col">{heading}</th>)}</tr></thead>
            <tbody>{hasRows ? data.records.map((record) => <tr key={recordSelectionId(record)} className={selectedRecordIds.has(recordSelectionId(record)) ? 'is-selected' : undefined}>
              <td className="valid-key-select-cell"><Checkbox aria-label={`选择有效密钥 ${record.keyPrefix}`} checked={selectedRecordIds.has(recordSelectionId(record))} disabled={busy} onChange={(event) => selectRecord(record, event.target.checked)} /></td>
              <td><code className="key-prefix">{record.keyPrefix}</code><code className="key-mask">{record.masked}</code></td>
              <td>{statusBadge(record)}</td>
              <td className="cell-muted valid-key-record-time">{formatTime(record.firstValidAt)}</td>
              <td className="cell-muted valid-key-record-time">{formatTime(record.lastValidAt)}</td>
              <td className="cell-muted">{record.ipAddress ?? '—'}</td><td>{rowActions(record)}</td>
            </tr>) : <tr className="empty-table-row"><td colSpan={7}>{stateMessage}</td></tr>}</tbody>
          </table></div>
          <div className="panel-footer"><span>{busy ? '正在读取…' : error ? '读取失败' : `显示 ${firstRecord}–${lastRecord} 条，共 ${data.total} 条`}</span>
            {data.totalPages > 1 && <nav aria-label="有效密钥记录分页" className="pagination"><button type="button" aria-label="上一页" disabled={busy || data.page <= 1} onClick={() => changePage(data.page - 1)}>上一页</button><span>第 {data.page} / {data.totalPages} 页</span><button type="button" aria-label="下一页" disabled={busy || data.page >= data.totalPages} onClick={() => changePage(data.page + 1)}>下一页</button></nav>}
          </div>
        </section>
      </div>
      {reveal && <Modal title="完整密钥" close={() => setReveal(null)}><p className="modal-description">{reveal.masked}</p><textarea readOnly value={reveal.secret} className="ui-input secret-textarea" aria-label="完整密钥" /><button type="button" onClick={async () => {
        try { await navigator.clipboard.writeText(reveal.secret); showNotice('完整密钥已复制'); } catch { showNotice('复制失败，请稍后重试'); }
      }} className="ui-button ui-button--primary ui-button--block">复制完整密钥</button></Modal>}
      {notice && <div role="status" className="toast">{notice}</div>}
    </ConfigProvider>
  );
}
