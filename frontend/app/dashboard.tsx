'use client';

import { useCallback, useEffect, useState } from 'react';
import { credentialStorageTypes } from '@/lib/credential-types';
import { apiFetch, apiUrl } from '@/lib/api-client';

type Summary = { total: number; valid: number; recovery: number; insufficient: number; invalid: number };
type KeyRow = {
  id: number; keyPrefix: string; provider: string; masked: string; status: string; createdAt: string;
};

const statusTabs = [
  { label: '可用', value: 'valid' }, { label: '额度待恢复', value: 'usage_limit_reached' },
  { label: '余额不足', value: 'insufficient_credit' }, { label: '不可用', value: 'invalid' },
];
const exportOptions = [
  { label: '所有密钥', value: 'all' },
  { label: '所有可用密钥', value: 'valid' },
  { label: '所有额度待恢复', value: 'usage_limit_reached' },
  { label: '所有余额不足', value: 'insufficient_credit' },
  { label: '所有不可用', value: 'invalid' },
] as const;
type ExportStatus = (typeof exportOptions)[number]['value'];
const statusStyle: Record<string, string> = {
  pending: 'bg-amber-50 text-amber-700', valid: 'bg-emerald-50 text-emerald-700',
  usage_limit_reached: 'bg-violet-50 text-violet-700', invalid: 'bg-rose-50 text-rose-700',
  insufficient_credit: 'bg-amber-50 text-amber-700',
};
const statusLabel: Record<string, string> = {
  valid: '可用', usage_limit_reached: '额度待恢复', invalid: '无效',
  insufficient_credit: '余额不足', permission_error: '权限不足', rate_limited: '请求限速',
  billing_error: '账单异常', network_error: '网络异常', server_error: '服务异常',
  model_unavailable: '模型不可用', request_or_spend_limit_error: '请求/额度限制', http_error: '接口异常',
};

function fmt(value?: string | null) {
  if (!value) return '—';
  return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(value));
}

function paginationItems(current: number, total: number): Array<number | 'start-gap' | 'end-gap'> {
  if (total <= 7) return Array.from({ length: total }, (_, index) => index + 1);
  if (current <= 4) return [1, 2, 3, 4, 5, 'end-gap', total];
  if (current >= total - 3) return [1, 'start-gap', total - 4, total - 3, total - 2, total - 1, total];
  return [1, 'start-gap', current - 1, current, current + 1, 'end-gap', total];
}

export default function Dashboard({ initialProvider }: { initialProvider: string }) {
  const [summary, setSummary] = useState<Summary>({ total: 0, valid: 0, recovery: 0, insufficient: 0, invalid: 0 });
  const [keys, setKeys] = useState<KeyRow[]>([]);
  const [status, setStatus] = useState('valid');
  const provider = initialProvider;
  const [storageType, setStorageType] = useState(() => credentialStorageTypes.find((item) => item.provider === initialProvider)?.id ?? '');
  const [queryInput, setQueryInput] = useState('');
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [totalPages, setTotalPages] = useState(1);
  const [busy, setBusy] = useState(true);
  const [reveal, setReveal] = useState<{ masked: string; secret: string } | null>(null);
  const [exportOpen, setExportOpen] = useState(false);
  const [exportStatus, setExportStatus] = useState<ExportStatus>('all');
  const [exporting, setExporting] = useState(false);
  const [notice, setNotice] = useState('');

  const loadKeys = useCallback(async (silent = false) => {
    if (!silent) setBusy(true);
    const params = new URLSearchParams();
    params.set('status', status);
    if (provider) params.set('provider', provider);
    if (storageType) params.set('type', storageType);
    if (query.trim()) params.set('q', query.trim());
    params.set('page', String(page));
    const response = await apiFetch(`/api/keys?${params}`, { cache: 'no-store' });
    if (response.status === 401) {
      window.location.replace('/login');
      return;
    }
    if (response.ok) {
      const body = await response.json() as { keys: KeyRow[]; summary: Summary; total: number; totalPages: number };
      setKeys(body.keys);
      setSummary(body.summary);
      setTotal(body.total);
      setTotalPages(body.totalPages);
      if (page > body.totalPages) setPage(body.totalPages);
    }
    if (!silent) setBusy(false);
  }, [status, provider, storageType, query, page]);

  useEffect(() => {
    const timer = window.setTimeout(() => { void loadKeys(); }, 0);
    return () => window.clearTimeout(timer);
  }, [loadKeys]);
  function showNotice(message: string) { setNotice(message); setTimeout(() => setNotice(''), 2600); }

  function submitSearch(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const nextQuery = queryInput.trim();
    setPage(1);
    if (nextQuery === query && page === 1) void loadKeys();
    else setQuery(nextQuery);
  }

  async function revealKey(row: KeyRow) {
    const params = new URLSearchParams({ type: storageType, status });
    const response = await apiFetch(`/api/keys/${row.id}/reveal?${params}`, { cache: 'no-store' });
    if (response.status === 401) return window.location.replace('/login');
    if (!response.ok) return showNotice('无法读取完整密钥');
    const body = await response.json() as { secret: string };
    setReveal({ masked: row.masked, secret: body.secret });
  }

  async function copyKey(row: KeyRow) {
    const params = new URLSearchParams({ type: storageType, status });
    const response = await apiFetch(`/api/keys/${row.id}/reveal?${params}`, { cache: 'no-store' });
    if (response.status === 401) return window.location.replace('/login');
    if (!response.ok) return showNotice('无法读取完整密钥');
    const body = await response.json() as { secret: string };
    await navigator.clipboard.writeText(body.secret);
    showNotice('密钥已复制');
  }

  const cards = [
    ['可用密钥', summary.valid, '真实调用已通过', '✓', 'bg-emerald-50 text-emerald-600'],
    ['额度待恢复', summary.recovery, '等待额度恢复', '◷', 'bg-violet-50 text-violet-600'],
    ['余额不足', summary.insufficient, '等待补充余额', '!', 'bg-amber-50 text-amber-600'],
    ['不可用', summary.invalid, '验证未通过', '×', 'bg-rose-50 text-rose-600'],
  ];

  const availableTypes = credentialStorageTypes.filter((item) => item.provider === provider);
  const visiblePages = paginationItems(page, totalPages);

  function exportHref(targetStatus: ExportStatus) {
    const params = new URLSearchParams({ status: targetStatus, provider });
    if (storageType) params.set('type', storageType);
    return apiUrl(`/api/export?${params}`);
  }

  async function confirmExport() {
    setExporting(true);
    try {
      const response = await fetch(exportHref(exportStatus), { cache: 'no-store', credentials: 'include' });
      if (response.status === 401) return window.location.replace('/login');
      if (!response.ok) throw new Error('export failed');
      const blobUrl = URL.createObjectURL(await response.blob());
      const link = document.createElement('a');
      link.href = blobUrl;
      link.download = `${storageType || 'keys'}-${exportStatus}.txt`;
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
    <>
      <div className="mx-auto max-w-[1500px] px-4 py-6 sm:px-5 lg:px-8 lg:py-8">
        <section aria-label="密钥类型与数据操作" className="mb-5 flex items-end justify-between gap-4 lg:min-h-16">
          <div className="flex w-fit max-w-full gap-1 overflow-x-auto rounded-xl bg-slate-200/80 p-1">{availableTypes.map((item, index) => { const selected = storageType === item.id; return <button key={item.id} type="button" aria-pressed={selected} onClick={() => { setStorageType(item.id); setQueryInput(''); setQuery(''); setPage(1); }} className={`shrink-0 whitespace-nowrap rounded-lg border px-4 py-2 text-sm font-semibold transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 focus-visible:ring-offset-2 ${selected ? 'border-indigo-600 bg-indigo-600 text-white shadow-[0_4px_10px_rgba(79,70,229,0.28)]' : 'border-transparent bg-transparent text-slate-600 hover:bg-white/60 hover:text-slate-900'}`}>{item.label}{index === 0 && <span className="sr-only">（默认）</span>}</button>; })}</div>
          <button type="button" onClick={() => window.location.reload()} className="hidden h-10 shrink-0 items-center rounded-xl border border-slate-200 bg-white px-4 text-sm font-semibold text-slate-600 shadow-sm transition hover:border-indigo-200 hover:bg-indigo-50 hover:text-indigo-600 lg:inline-flex">↻ 刷新数据</button>
        </section>

        <section className="mt-5 grid grid-cols-2 gap-3 sm:gap-4 xl:grid-cols-4">
          {cards.map(([label, value, note, icon, tone]) => <article key={String(label)} className="rounded-2xl border border-slate-200/80 bg-white p-4 shadow-[0_10px_35px_rgba(15,23,42,0.04)] sm:p-5"><div className="flex items-start justify-between gap-2"><div><p className="text-xs font-medium text-slate-500 sm:text-sm">{label}</p><p className="mt-2 text-2xl font-semibold tracking-tight sm:text-3xl">{value}</p></div><span className={`grid h-8 w-8 shrink-0 place-items-center rounded-xl text-sm font-bold sm:h-10 sm:w-10 sm:text-base ${tone}`}>{icon}</span></div><p className="mt-4 hidden text-xs text-slate-400 sm:block">{note}</p></article>)}
        </section>

        <section className="mt-6 overflow-hidden rounded-2xl border border-slate-200/80 bg-white shadow-[0_10px_35px_rgba(15,23,42,0.04)]">
          <div className="border-b border-slate-100 px-4 pt-4 sm:px-5 sm:pt-5">
            <div className="flex flex-col gap-3 pb-3 lg:flex-row lg:items-center lg:justify-between">
              <nav className="flex h-11 gap-6 overflow-x-auto text-sm">{statusTabs.map((tab) => <button key={tab.value} onClick={() => { setStatus(tab.value); setPage(1); }} className={`whitespace-nowrap border-b-2 font-medium ${status === tab.value ? 'border-indigo-600 text-indigo-600' : 'border-transparent text-slate-500'}`}>{tab.label}</button>)}</nav>
              <form onSubmit={submitSearch} className="flex w-full items-center gap-2 lg:max-w-xl"><label className="flex h-10 min-w-0 flex-1 items-center gap-2 rounded-xl border border-slate-200 bg-white px-3 text-sm text-slate-400"><span>⌕</span><input value={queryInput} onChange={(event) => setQueryInput(event.target.value)} className="w-full bg-transparent text-slate-700 outline-none placeholder:text-slate-400" placeholder="搜索密钥前缀" aria-label="搜索密钥前缀" /></label><button type="submit" disabled={busy} className="h-10 shrink-0 rounded-xl border border-indigo-200 bg-indigo-50 px-4 text-sm font-semibold text-indigo-700 transition hover:bg-indigo-100 disabled:cursor-wait disabled:opacity-60">搜索</button><button type="button" onClick={() => setExportOpen(true)} className="inline-flex h-10 shrink-0 items-center gap-1.5 rounded-xl border border-slate-200 bg-white px-4 text-sm font-semibold text-slate-600 transition hover:border-indigo-200 hover:bg-indigo-50 hover:text-indigo-700"><span aria-hidden="true">↓</span>导出</button></form>
            </div>
          </div>

          <div className="max-h-[620px] divide-y divide-slate-100 overflow-auto overscroll-contain md:hidden">{busy ? <p className="px-4 py-20 text-center text-sm text-slate-400">正在读取密钥…</p> : keys.length === 0 ? <div className="px-4 py-20 text-center"><div className="mx-auto grid h-12 w-12 place-items-center rounded-2xl bg-indigo-50 text-xl text-indigo-500">◇</div><p className="mt-4 font-semibold text-slate-700">没有符合条件的密钥</p><p className="mt-1 text-sm text-slate-400">请在桌面端开始检测并上传结果。</p></div> : keys.map((row) => <article key={`${row.id}-${row.keyPrefix}`} className="p-4"><div className="flex items-start justify-between gap-3"><code className="min-w-0 break-all text-xs text-slate-700">{row.keyPrefix}</code><span className={`shrink-0 rounded-full px-2.5 py-1 text-xs font-semibold ${statusStyle[row.status] || 'bg-slate-100 text-slate-600'}`}>{statusLabel[row.status] || row.status}</span></div><div className="mt-3 flex items-end justify-between gap-3"><div className="min-w-0"><p className="truncate font-mono text-xs text-slate-500">{row.masked}</p><p className="mt-1 text-xs text-slate-400">{row.provider} · {fmt(row.createdAt)}</p></div><div className="flex shrink-0 items-center gap-3"><button onClick={() => void revealKey(row)} className="text-sm font-semibold text-indigo-600">查看</button><button onClick={() => void copyKey(row)} className="text-sm font-semibold text-slate-600">复制</button></div></div></article>)}</div>
          <div className="hidden max-h-[620px] overflow-auto overscroll-contain md:block"><table className="w-full min-w-[980px] text-left text-sm"><thead className="sticky top-0 z-10 border-b border-slate-100 bg-white text-xs font-semibold uppercase tracking-wide text-slate-400 shadow-[0_1px_0_rgba(226,232,240,0.8)]"><tr>{['密钥前32位', '平台', '密钥（掩码）', '状态', '创建时间', '操作'].map((head) => <th key={head} className="px-5 py-3.5">{head}</th>)}</tr></thead><tbody>
            {busy ? <tr><td colSpan={6} className="px-5 py-20 text-center text-slate-400">正在读取密钥…</td></tr> : keys.length === 0 ? <tr><td colSpan={6} className="px-5 py-20 text-center"><div className="mx-auto grid h-12 w-12 place-items-center rounded-2xl bg-indigo-50 text-xl text-indigo-500">◇</div><p className="mt-4 font-semibold text-slate-700">没有符合条件的密钥</p><p className="mt-1 text-sm text-slate-400">请在桌面端开始检测并上传结果。</p></td></tr> : keys.map((row) => <tr key={`${row.id}-${row.keyPrefix}`} className="border-b border-slate-100 last:border-0 hover:bg-slate-50/60"><td className="px-5 py-4 font-mono text-xs text-slate-700">{row.keyPrefix}</td><td className="px-5 py-4 font-medium">{row.provider}</td><td className="px-5 py-4 font-mono text-xs text-slate-600">{row.masked}</td><td className="px-5 py-4"><span className={`rounded-full px-2.5 py-1 text-xs font-semibold ${statusStyle[row.status] || 'bg-slate-100 text-slate-600'}`}>{statusLabel[row.status] || row.status}</span></td><td className="px-5 py-4 text-xs text-slate-500">{fmt(row.createdAt)}</td><td className="px-5 py-4"><div className="flex items-center gap-3"><button onClick={() => void revealKey(row)} className="font-semibold text-indigo-600">查看</button><button onClick={() => void copyKey(row)} className="font-semibold text-slate-600">复制</button></div></td></tr>)}
          </tbody></table></div>
          <div className="flex flex-col items-center justify-between gap-3 border-t border-slate-100 px-5 py-4 text-sm text-slate-500 sm:flex-row"><span>共 {total} 条，每页 100 条</span>{totalPages > 1 && <nav aria-label="密钥列表分页" className="flex items-center gap-1"><button aria-label="上一页" disabled={page <= 1 || busy} onClick={() => setPage((value) => Math.max(1, value - 1))} className="grid h-9 w-9 place-items-center rounded-lg border border-slate-200 bg-white text-base font-medium text-slate-600 disabled:cursor-not-allowed disabled:opacity-40">‹</button>{visiblePages.map((item) => typeof item === 'number' ? <button key={item} aria-current={item === page ? 'page' : undefined} onClick={() => setPage(item)} className={`grid h-9 min-w-9 place-items-center rounded-lg px-2 text-sm font-semibold ${item === page ? 'bg-indigo-600 text-white shadow-sm shadow-indigo-200' : 'border border-slate-200 bg-white text-slate-600 hover:border-indigo-300 hover:text-indigo-600'}`}>{item}</button> : <span key={item} className="grid h-9 w-8 place-items-center text-slate-400">…</span>)}<button aria-label="下一页" disabled={page >= totalPages || busy} onClick={() => setPage((value) => Math.min(totalPages, value + 1))} className="grid h-9 w-9 place-items-center rounded-lg border border-slate-200 bg-white text-base font-medium text-slate-600 disabled:cursor-not-allowed disabled:opacity-40">›</button></nav>}</div>
        </section>
      </div>

      {reveal && <Modal title="完整密钥" close={() => setReveal(null)}><p className="mb-3 text-xs text-slate-500">{reveal.masked}</p><textarea readOnly value={reveal.secret} className="h-40 w-full resize-none rounded-xl border border-slate-200 bg-slate-50 p-3 font-mono text-xs outline-none" /><button onClick={async () => { await navigator.clipboard.writeText(reveal.secret); showNotice('完整密钥已复制'); }} className="mt-4 w-full rounded-xl bg-indigo-600 py-2.5 text-sm font-semibold text-white">复制完整密钥</button></Modal>}
      {exportOpen && <Modal title="导出密钥" close={() => { if (!exporting) setExportOpen(false); }}><p className="mb-4 text-sm leading-6 text-slate-500">请选择要导出的密钥范围。导出内容仅包含当前平台和当前密钥类型，文件格式为 TXT。</p><div role="radiogroup" aria-label="导出范围" className="grid gap-2 sm:grid-cols-2">{exportOptions.map((option) => { const selected = exportStatus === option.value; return <label key={option.value} className={`flex cursor-pointer items-center gap-3 rounded-xl border px-4 py-3 text-sm font-semibold transition ${selected ? 'border-indigo-300 bg-indigo-50 text-indigo-700 ring-1 ring-indigo-200' : 'border-slate-200 text-slate-600 hover:border-slate-300 hover:bg-slate-50'}`}><input type="radio" name="export-status" value={option.value} checked={selected} onChange={() => setExportStatus(option.value)} className="sr-only" /><span className={`grid h-5 w-5 shrink-0 place-items-center rounded-full border text-[11px] ${selected ? 'border-indigo-600 bg-indigo-600 text-white' : 'border-slate-300 bg-white text-transparent'}`}>✓</span>{option.label}</label>; })}</div><div className="mt-6 flex justify-end gap-2"><button type="button" disabled={exporting} onClick={() => setExportOpen(false)} className="h-10 rounded-xl border border-slate-200 bg-white px-4 text-sm font-semibold text-slate-600 transition hover:bg-slate-50 disabled:opacity-50">取消</button><button type="button" disabled={exporting} onClick={() => void confirmExport()} className="h-10 rounded-xl bg-indigo-600 px-5 text-sm font-semibold text-white shadow-sm transition hover:bg-indigo-700 disabled:cursor-wait disabled:opacity-60">{exporting ? '正在导出…' : '确认导出'}</button></div></Modal>}
      {notice && <div role="status" className="fixed bottom-6 left-1/2 z-[60] -translate-x-1/2 rounded-full bg-slate-950 px-4 py-2.5 text-sm font-medium text-white shadow-xl">{notice}</div>}
    </>
  );
}

function Modal({ title, close, children }: { title: string; close: () => void; children: React.ReactNode }) {
  return <div className="fixed inset-0 z-50 grid place-items-center bg-slate-950/30 p-4 backdrop-blur-sm" onMouseDown={(event) => { if (event.target === event.currentTarget) close(); }}><section role="dialog" aria-modal="true" aria-label={title} className="w-full max-w-lg rounded-2xl bg-white p-6 shadow-2xl"><div className="mb-5 flex items-center justify-between"><h2 className="text-lg font-semibold">{title}</h2><button onClick={close} aria-label="关闭" className="grid h-8 w-8 place-items-center rounded-lg bg-slate-100 text-slate-500">×</button></div>{children}</section></div>;
}
