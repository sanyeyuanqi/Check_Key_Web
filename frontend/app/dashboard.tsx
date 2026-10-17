'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { Checkbox, ConfigProvider, type ThemeConfig } from 'antd';
import { credentialStorageTypes, resultBucket } from '@/lib/credential-types';
import { apiFetch, apiUrl } from '@/lib/api-client';
import { EmptyState, Modal } from '@/app/workspace-ui';
import { platformByProvider } from '@/lib/platforms';
import { UploadDateTimeRangePicker } from '@/app/upload-date-picker';
import { CredentialStatusSelect } from '@/app/credential-status-select';
import { addUploadTimeRangeFilter, uploadTimeRangeError, type UploadTimeRange } from '@/lib/upload-time-range';
import { BulkCopyError, keySelectionId, selectedKeysText } from '@/lib/key-selection';
import { appliedExportRange, keyListQueryParams, type KeyListQuery } from '@/lib/key-list-query';

type CredentialStatus = {
  value: string; name: string; tone: 'valid' | 'recovery' | 'insufficient' | 'invalid' | 'neutral'; sortOrder: number; count: number;
};
type KeyRow = {
  id: number; keyPrefix: string; provider: string; storageType: string; masked: string; status: string; createdAt: string; updatedAt: string; ipAddress: string | null;
};

const selectionTheme: ThemeConfig = {
  components: { Checkbox: { colorPrimary: '#13795f', colorPrimaryHover: '#0e654f', colorBorder: '#c6d7ce', controlInteractiveSize: 14, borderRadiusSM: 3 } },
};

function fmt(value?: string | null) {
  if (!value) return '—';
  return new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(value));
}

function exportDownloadName(contentDisposition: string | null, fallback: string) {
  const encoded = contentDisposition?.match(/filename\*\s*=\s*UTF-8''([^;]+)/i);
  let name = '';
  if (encoded) {
    try { name = decodeURIComponent(encoded[1].trim().replace(/^"|"$/g, '')); } catch { /* Use the ordinary filename below. */ }
  }
  if (!name) {
    const ordinary = contentDisposition?.match(/filename\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^;]+))/i);
    name = (ordinary?.[1] ?? ordinary?.[2] ?? '').replace(/\\(.)/g, '$1').trim();
  }
  return name.split(/[\\/]/).pop()?.replace(/[\u0000-\u001f\u007f]/g, '').trim() || fallback;
}

function paginationItems(current: number, total: number): Array<number | 'start-gap' | 'end-gap'> {
  if (total <= 7) return Array.from({ length: total }, (_, index) => index + 1);
  if (current <= 4) return [1, 2, 3, 4, 5, 'end-gap', total];
  if (current >= total - 3) return [1, 'start-gap', total - 4, total - 3, total - 2, total - 1, total];
  return [1, 'start-gap', current - 1, current, current + 1, 'end-gap', total];
}

export default function Dashboard({ initialProvider }: { initialProvider: string }) {
  const [statuses, setStatuses] = useState<CredentialStatus[]>([]);
  const [keys, setKeys] = useState<KeyRow[]>([]);
  const [statusDraft, setStatusDraft] = useState('all');
  const [uploadTimeRangeDraft, setUploadTimeRangeDraft] = useState<UploadTimeRange>({ start: '', end: '' });
  const [appliedQuery, setAppliedQuery] = useState<KeyListQuery>({ mode: 'filters', status: 'all', range: { start: '', end: '' } });
  const provider = initialProvider;
  const availableStorageTypes = credentialStorageTypes.filter((item) => item.provider === provider);
  const [storageType, setStorageType] = useState<string>(availableStorageTypes[0]?.id ?? '');
  const [queryInput, setQueryInput] = useState('');
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [totalPages, setTotalPages] = useState(1);
  const [busy, setBusy] = useState(true);
  const [reveal, setReveal] = useState<{ masked: string; secret: string } | null>(null);
  const [exportOpen, setExportOpen] = useState(false);
  const [exportStatuses, setExportStatuses] = useState<string[]>(['valid']);
  const [exporting, setExporting] = useState(false);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  const [selectedKeyIds, setSelectedKeyIds] = useState<Set<string>>(() => new Set());
  const [copyMode, setCopyMode] = useState<'single' | 'batch' | null>(null);
  const copying = copyMode !== null;
  const activeRequest = useRef<AbortController | null>(null);
  const activeCopy = useRef<AbortController | null>(null);

  const resetKeySelection = useCallback(() => {
    activeCopy.current?.abort();
    activeCopy.current = null;
    setCopyMode(null);
    setSelectedKeyIds(new Set());
  }, []);

  const loadKeys = useCallback(async () => {
    resetKeySelection();
    activeRequest.current?.abort();
    const controller = new AbortController();
    activeRequest.current = controller;
    setBusy(true);
    setError('');
    try {
      const params = keyListQueryParams(appliedQuery, { provider, storageType, page });
      const response = await apiFetch(`/api/keys?${params}`, { cache: 'no-store', signal: controller.signal });
      if (controller.signal.aborted) return;
      if (response.status === 401) {
        window.location.replace('/login');
        return;
      }
      if (!response.ok) throw new Error('Unable to load keys');
      const body = await response.json() as { keys: KeyRow[]; statuses: CredentialStatus[]; total: number; totalPages: number };
      if (controller.signal.aborted) return;
      const nextStatuses = [...body.statuses].sort((left, right) => left.sortOrder - right.sortOrder || left.value.localeCompare(right.value));
      setKeys(body.keys);
      setStatuses(nextStatuses);
      setExportStatuses((current) => current.filter((value) => nextStatuses.some((item) => item.value === value)));
      setTotal(body.total);
      setTotalPages(body.totalPages);
      if (page > body.totalPages) setPage(body.totalPages);
    } catch {
      if (!controller.signal.aborted) {
        setKeys([]);
        setStatuses([]);
        setTotal(0);
        setTotalPages(1);
        setError('无法读取密钥，请稍后刷新重试。');
      }
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }, [storageType, provider, appliedQuery, page, setExportStatuses, resetKeySelection]);

  useEffect(() => () => {
    activeCopy.current?.abort();
    activeCopy.current = null;
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => { void loadKeys(); }, 0);
    return () => {
      window.clearTimeout(timer);
      activeRequest.current?.abort();
    };
  }, [loadKeys]);

  function showNotice(message: string) { setNotice(message); setTimeout(() => setNotice(''), 2600); }

  function prepareFilterChange() {
    resetKeySelection();
    activeRequest.current?.abort();
    setBusy(true);
    setError('');
  }

  function changePage(nextPage: number) {
    if (nextPage === page) return;
    prepareFilterChange();
    setPage(nextPage);
  }

  function changeStorageType(nextType: string) {
    if (nextType === storageType) return;
    prepareFilterChange();
    setStatuses([]);
    setStatusDraft('all');
    setUploadTimeRangeDraft({ start: '', end: '' });
    setQueryInput('');
    setAppliedQuery({ mode: 'filters', status: 'all', range: { start: '', end: '' } });
    setExportStatuses(['valid']);
    setStorageType(nextType);
    setPage(1);
  }

  function changeUploadTimeRange(nextRange: UploadTimeRange) {
    if (nextRange.start === uploadTimeRangeDraft.start && nextRange.end === uploadTimeRangeDraft.end) return;
    const rangeError = uploadTimeRangeError(nextRange.start, nextRange.end);
    if (rangeError) return showNotice(rangeError);
    setUploadTimeRangeDraft(nextRange);
  }

  function submitFilterSearch(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const rangeError = uploadTimeRangeError(uploadTimeRangeDraft.start, uploadTimeRangeDraft.end);
    if (rangeError) return showNotice(rangeError);
    prepareFilterChange();
    setAppliedQuery({ mode: 'filters', status: statusDraft, range: { ...uploadTimeRangeDraft } });
    setPage(1);
  }

  function submitPrefixSearch(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const nextPrefix = queryInput.trim();
    prepareFilterChange();
    setQueryInput(nextPrefix);
    setAppliedQuery({ mode: 'prefix', prefix: nextPrefix });
    setPage(1);
  }

  async function revealKey(row: KeyRow) {
    const params = new URLSearchParams({ type: row.storageType, status: resultBucket(row.status) });
    const response = await apiFetch(`/api/keys/${row.id}/reveal?${params}`, { cache: 'no-store' });
    if (response.status === 401) return window.location.replace('/login');
    if (!response.ok) return showNotice('无法读取完整密钥');
    const body = await response.json() as { secret: string };
    setReveal({ masked: row.masked, secret: body.secret });
  }

  async function copyKey(row: KeyRow) {
    if (busy) return;
    await copyText((signal) => selectedKeysText([row], apiFetch, signal), 'single', '密钥已复制');
  }

  const selectedRows = keys.filter((row) => selectedKeyIds.has(keySelectionId(row)));
  const allPageSelected = keys.length > 0 && selectedRows.length === keys.length;
  const somePageSelected = selectedRows.length > 0 && !allPageSelected;

  function selectRow(row: KeyRow, checked: boolean) {
    if (busy || copying) return;
    setSelectedKeyIds((current) => {
      const next = new Set(current);
      if (checked) next.add(keySelectionId(row));
      else next.delete(keySelectionId(row));
      return next;
    });
  }

  function selectPage(checked: boolean) {
    if (busy || copying) return;
    setSelectedKeyIds(checked ? new Set(keys.map(keySelectionId)) : new Set());
  }

  async function copyText(readText: (signal: AbortSignal) => Promise<string>, mode: 'single' | 'batch', successMessage: string) {
    if (copying || activeCopy.current) return;
    const controller = new AbortController();
    activeCopy.current = controller;
    setCopyMode(mode);
    try {
      const text = await readText(controller.signal);
      if (controller.signal.aborted) return;
      await navigator.clipboard.writeText(text);
      if (!controller.signal.aborted) showNotice(successMessage);
    } catch (error) {
      if (controller.signal.aborted) return;
      if (error instanceof BulkCopyError && error.status === 401) return window.location.replace('/login');
      showNotice(mode === 'batch' ? '批量复制失败，请刷新后重新选择' : '复制失败，请稍后重试');
    } finally {
      if (activeCopy.current === controller) {
        activeCopy.current = null;
        setCopyMode(null);
      }
    }
  }

  async function copySelectedKeys() {
    if (busy || selectedRows.length === 0) return;
    await copyText((signal) => selectedKeysText(selectedRows, apiFetch, signal), 'batch', `已复制 ${selectedRows.length} 条密钥`);
  }

  const visiblePages = paginationItems(page, totalPages);
  const platform = platformByProvider(provider);
  const statusByValue = new Map(statuses.map((item) => [item.value, item]));
  const allStatusCount = statuses.reduce((count, item) => count + item.count, 0);
  const selectedExportStatuses = statuses.filter((item) => exportStatuses.includes(item.value)).map((item) => item.value);

  function statusBadge(value: string) {
    const item = statusByValue.get(value);
    return <span className={`status-badge status-${item?.tone ?? 'neutral'}`}>{item?.name ?? value}</span>;
  }

  function exportHref(targetStatuses: string[]) {
    const params = new URLSearchParams({ status: targetStatuses.join(','), status_mode: 'exact', provider });
    if (storageType) params.set('type', storageType);
    addUploadTimeRangeFilter(params, appliedExportRange(appliedQuery));
    return apiUrl(`/api/export?${params}`);
  }

  async function confirmExport() {
    if (exporting || busy || selectedExportStatuses.length === 0) return;
    setExporting(true);
    try {
      const response = await fetch(exportHref(selectedExportStatuses), { cache: 'no-store', credentials: 'include' });
      if (response.status === 401) return window.location.replace('/login');
      if (!response.ok) throw new Error('export failed');
      const blobUrl = URL.createObjectURL(await response.blob());
      const link = document.createElement('a');
      link.href = blobUrl;
      link.download = exportDownloadName(response.headers.get('Content-Disposition'), `${platform?.slug || 'keys'}-selected.txt`);
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(blobUrl);
      setExportOpen(false);
      showNotice('TXT 文件已开始下载');
    } catch {
      showNotice('导出失败，请稍后重试');
    } finally {
      setExporting(false);
    }
  }

  return (
    <ConfigProvider theme={selectionTheme}>
      <div className="dashboard">
        <div className="key-type-bar">
          <div role="group" aria-label="密钥类型" className="key-type-options">
            {availableStorageTypes.map((item) => <button key={item.id} type="button" aria-pressed={storageType === item.id} onClick={() => changeStorageType(item.id)} className={`key-type-button ${storageType === item.id ? 'is-selected' : ''}`}>{item.label}</button>)}
          </div>
          <div className="key-page-actions">
            <button type="button" disabled={busy || copying || selectedRows.length === 0} onClick={() => void copySelectedKeys()} className="ui-button ui-button--soft bulk-copy-button">{copyMode === 'batch' ? '复制中…' : selectedRows.length > 0 ? `批量复制（${selectedRows.length}）` : '批量复制'}</button>
            <button type="button" disabled={busy || statuses.length === 0} onClick={() => setExportOpen(true)} className="ui-button ui-button--primary">导出</button>
            <button type="button" disabled={busy} onClick={() => void loadKeys()} className="ui-button">刷新</button>
          </div>
        </div>
        <section aria-label="密钥列表" className="data-panel">
          <div className="key-list-toolbar">
            <form onSubmit={submitFilterSearch} onKeyDown={(event) => {
              if (event.key === 'Enter' && event.target instanceof HTMLElement && event.target.closest('.upload-time-range')) event.preventDefault();
            }} aria-label="状态和日期查询" className="key-filter-query">
              <div className="key-status-field"><CredentialStatusSelect value={statusDraft} disabled={busy} statuses={statuses} total={allStatusCount} onChange={setStatusDraft} /></div>
              <UploadDateTimeRangePicker value={uploadTimeRangeDraft} onChange={changeUploadTimeRange} />
              <button type="submit" aria-label="搜索状态和日期" disabled={busy} className="ui-button search-button">搜索</button>
            </form>
            <form onSubmit={submitPrefixSearch} aria-label="密钥前缀查询" className="key-prefix-query">
              <label className="search-field key-prefix-search"><input value={queryInput} onChange={(event) => setQueryInput(event.target.value)} placeholder="搜索密钥前缀" aria-label="搜索密钥前缀" maxLength={32} /></label>
              <button type="submit" aria-label="搜索密钥前缀" disabled={busy} className="ui-button search-button">搜索</button>
            </form>
          </div>

          <div className="mobile-key-list">
            {!busy && keys.length > 0 && <div className="mobile-selection-bar"><Checkbox checked={allPageSelected} indeterminate={somePageSelected} disabled={copying} aria-label="全选本页密钥" onChange={(event) => selectPage(event.target.checked)}>全选本页</Checkbox><span>已选 {selectedRows.length} 条</span></div>}
            {busy ? <p role="status" className="loading-message">正在读取密钥…</p> : error ? <p role="alert" className="loading-message">{error}</p> : keys.length === 0 ? <EmptyState /> : keys.map((row) => (
              <article key={keySelectionId(row)} className={`mobile-data-row${selectedKeyIds.has(keySelectionId(row)) ? ' is-selected' : ''}`}>
                <div className="mobile-row-heading"><div className="mobile-row-key"><Checkbox aria-label={`选择密钥 ${row.keyPrefix}`} checked={selectedKeyIds.has(keySelectionId(row))} disabled={copying} onChange={(event) => selectRow(row, event.target.checked)} /><code className="key-prefix">{row.keyPrefix}</code></div>{statusBadge(row.status)}</div>
                <div className="mobile-row-footer">
                  <div className="mobile-row-details"><p className="key-mask">{row.masked}</p><p className="key-upload-ip">IP 地址：{row.ipAddress ?? '—'}</p><p>上传时间：{fmt(row.createdAt)}</p><p>更新时间：{fmt(row.updatedAt)}</p></div>
                  <div className="row-actions"><button type="button" onClick={() => void revealKey(row)} className="text-action">查看</button><button type="button" disabled={copying} onClick={() => void copyKey(row)} className="text-action">复制</button></div>
                </div>
              </article>
            ))}
          </div>
          <div className="table-scroll">
            <table className="workspace-table dashboard-key-table">
              <thead><tr><th scope="col" className="key-select-cell"><Checkbox aria-label="全选本页密钥" checked={allPageSelected} indeterminate={somePageSelected} disabled={busy || copying || keys.length === 0} onChange={(event) => selectPage(event.target.checked)} /></th>{['密钥前32位', '状态', 'IP 地址', '上传时间', '更新时间', '操作'].map((head) => <th key={head} scope="col">{head}</th>)}</tr></thead>
              <tbody>
                {busy ? <tr className="empty-table-row"><td colSpan={7}><p role="status" className="loading-message">正在读取密钥…</p></td></tr> : error ? <tr className="empty-table-row"><td colSpan={7}><p role="alert" className="loading-message">{error}</p></td></tr> : keys.length === 0 ? <tr className="empty-table-row"><td colSpan={7}><EmptyState /></td></tr> : keys.map((row) => (
                  <tr key={keySelectionId(row)} className={selectedKeyIds.has(keySelectionId(row)) ? 'is-selected' : undefined}>
                    <td className="key-select-cell"><Checkbox aria-label={`选择密钥 ${row.keyPrefix}`} checked={selectedKeyIds.has(keySelectionId(row))} disabled={busy || copying} onChange={(event) => selectRow(row, event.target.checked)} /></td>
                    <td><code className="key-prefix">{row.keyPrefix}</code><code className="key-mask">{row.masked}</code></td>
                    <td>{statusBadge(row.status)}</td>
                    <td className="cell-muted key-upload-ip">{row.ipAddress ?? '—'}</td>
                    <td className="cell-muted">{fmt(row.createdAt)}</td>
                    <td className="cell-muted">{fmt(row.updatedAt)}</td>
                    <td><div className="row-actions"><button type="button" aria-label="查看密钥" title="查看密钥" onClick={() => void revealKey(row)} className="text-action">查看</button><button type="button" aria-label="复制密钥" title="复制密钥" disabled={copying} onClick={() => void copyKey(row)} className="text-action">复制</button></div></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="panel-footer">
            <span>显示 {total ? (page - 1) * 100 + 1 : 0}–{Math.min(page * 100, total)} 条，共 {total} 条</span>
            {totalPages > 1 && <nav aria-label="密钥列表分页" className="pagination">
              <button type="button" aria-label="上一页" disabled={page <= 1 || busy} onClick={() => changePage(Math.max(1, page - 1))}>上一页</button>
              {visiblePages.map((item) => typeof item === 'number' ? <button type="button" key={item} disabled={busy} aria-current={item === page ? 'page' : undefined} onClick={() => changePage(item)}>{item}</button> : <span key={item}>…</span>)}
              <button type="button" aria-label="下一页" disabled={page >= totalPages || busy} onClick={() => changePage(Math.min(totalPages, page + 1))}>下一页</button>
            </nav>}
          </div>
        </section>
      </div>

      {reveal && <Modal title="完整密钥" close={() => setReveal(null)}>
        <p className="modal-description">{reveal.masked}</p>
        <textarea readOnly value={reveal.secret} className="ui-input secret-textarea" aria-label="完整密钥" />
        <button type="button" disabled={copying} onClick={() => void copyText(async () => reveal.secret, 'single', '完整密钥已复制')} className="ui-button ui-button--primary ui-button--block">复制完整密钥</button>
      </Modal>}
      {exportOpen && <Modal title="导出密钥" showClose={false} close={() => { if (!exporting) setExportOpen(false); }}>
        <div role="group" aria-label="导出范围" className="export-options">
          {statuses.map((option) => <label key={option.value} className={`export-option ${exportStatuses.includes(option.value) ? 'is-selected' : ''} ${exporting ? 'is-disabled' : ''}`}><input type="checkbox" name="export-status" value={option.value} disabled={exporting || busy} checked={exportStatuses.includes(option.value)} onChange={(event) => {
            const checked = event.target.checked;
            setExportStatuses((current) => checked ? [...current, option.value] : current.filter((value) => value !== option.value));
          }} /><span className="export-option-name">{option.name}</span><span className="export-option-count">{option.count}</span></label>)}
        </div>
        {statuses.length === 0 && <p role="status" className="modal-description">{busy ? '正在读取导出范围…' : '暂无可导出的状态，请刷新重试。'}</p>}
        <div className="modal-actions"><button type="button" disabled={exporting} onClick={() => setExportOpen(false)} className="ui-button">取消</button><button type="button" disabled={exporting || busy || selectedExportStatuses.length === 0} onClick={() => void confirmExport()} className="ui-button ui-button--primary">{exporting ? '正在导出…' : '确认导出'}</button></div>
      </Modal>}
      {notice && <div role="status" className="toast">{notice}</div>}
    </ConfigProvider>
  );
}
