import { addUploadTimeRangeFilter, type UploadTimeRange } from './upload-time-range';

export type KeyListQuery =
  | { mode: 'filters'; status: string; range: UploadTimeRange }
  | { mode: 'prefix'; prefix: string };

export function keyListQueryParams(
  query: KeyListQuery,
  { provider, storageType, page }: { provider: string; storageType: string; page: number },
): URLSearchParams {
  const params = new URLSearchParams({
    status_mode: 'exact',
    status: query.mode === 'filters' ? query.status : 'all',
    provider,
    type: storageType,
    page: String(page),
  });
  if (query.mode === 'filters') {
    addUploadTimeRangeFilter(params, query.range);
  } else {
    const prefix = query.prefix.trim();
    if (prefix) params.set('q', prefix);
  }
  return params;
}

export function appliedExportRange(query: KeyListQuery): UploadTimeRange {
  return query.mode === 'filters' ? query.range : { start: '', end: '' };
}
