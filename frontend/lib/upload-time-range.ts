export type UploadTimeRange = { start: string; end: string };

export function parseLocalDateTime(value: string): Date | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value);
  if (!match) return null;

  const [, yearText, monthText, dayText, hourText, minuteText] = match;
  const [year, month, day, hour, minute] = [yearText, monthText, dayText, hourText, minuteText].map(Number);
  if (year < 2 || year > 9998 || month < 1 || month > 12 || day < 1 || day > 31 || hour > 23 || minute > 59) {
    return null;
  }

  const date = new Date(0);
  // setFullYear preserves years 0002–0099 rather than interpreting them as 1902–1999.
  date.setFullYear(year, month - 1, day);
  date.setHours(hour, minute, 0, 0);
  if (
    date.getFullYear() !== year || date.getMonth() !== month - 1 || date.getDate() !== day ||
    date.getHours() !== hour || date.getMinutes() !== minute
  ) {
    return null;
  }
  return date;
}

export function uploadTimeRangeError(start: string, end: string): string {
  const startDate = start ? parseLocalDateTime(start) : null;
  const endDate = end ? parseLocalDateTime(end) : null;
  if (start && !startDate) return '请选择有效的开始日期时间';
  if (end && !endDate) return '请选择有效的结束日期时间';
  if (startDate && endDate && startDate.getTime() > endDate.getTime()) {
    return '结束时间不能早于开始时间';
  }
  return '';
}

export function addUploadTimeRangeFilter(params: URLSearchParams, range: UploadTimeRange): void {
  const error = uploadTimeRangeError(range.start, range.end);
  if (error) throw new Error(error);

  const start = parseLocalDateTime(range.start);
  const end = parseLocalDateTime(range.end);
  if (start) params.set('created_from', start.toISOString());
  // The interface selects whole minutes; the exclusive upper bound includes the end minute.
  if (end) params.set('created_before', new Date(end.getTime() + 60_000).toISOString());
}
