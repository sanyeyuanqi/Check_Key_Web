'use client';

import { useEffect, useId, type ReactNode } from 'react';

export function PageHeader({ eyebrow, title, description, badge, children }: { eyebrow: string; title: string; description: string; badge?: ReactNode; children: ReactNode }) {
  return <header className="page-header"><div><p className="page-eyebrow">{eyebrow}</p><div className="page-title-row"><h1>{title}</h1>{badge}</div><p className="page-description">{description}</p></div><div className="page-actions">{children}</div></header>;
}

export function MetricCard({ label, count, note, tone, percentage }: { label: string; count: number; note: string; tone: string; percentage?: string }) {
  return <article className={`metric-card tone-${tone}`}><div className="metric-heading"><span>{label}</span></div><p className="metric-value">{count}</p><div className="metric-footer"><span>{note}</span>{percentage && <span className="metric-percentage">占比 {percentage}%</span>}</div></article>;
}

export function EmptyState() {
  return <div className="empty-state"><p>暂无符合条件的密钥</p><span>在检测器中完成检测并上传后，结果会显示在这里。</span></div>;
}

export function Modal({ title, close, children, showClose = true }: { title: string; close: () => void; children: ReactNode; showClose?: boolean }) {
  const titleId = useId();
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) { if (event.key === 'Escape') close(); }
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [close]);
  return <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) close(); }}><section role="dialog" aria-modal="true" aria-labelledby={titleId} className="workspace-modal"><div className="modal-heading"><h2 id={titleId}>{title}</h2>{showClose && <button type="button" aria-label="关闭" onClick={close} className="ui-text-button">关闭</button>}</div>{children}</section></div>;
}
