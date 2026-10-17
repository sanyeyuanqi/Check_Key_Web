'use client';

import { useMemo } from 'react';
import { ConfigProvider, DatePicker, Grid, type ThemeConfig } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import dayjs, { type Dayjs } from 'dayjs';
import 'dayjs/locale/zh-cn';
import { parseLocalDateTime, type UploadTimeRange } from '@/lib/upload-time-range';

dayjs.locale('zh-cn');

const { RangePicker } = DatePicker;
const minDate = dayjs(parseLocalDateTime('0002-01-01T00:00')!);
const maxDate = dayjs(parseLocalDateTime('9998-12-31T23:59')!);

type UploadDateTimeRangePickerProps = {
  value: UploadTimeRange;
  onChange: (range: UploadTimeRange) => void;
};

export function UploadDateTimeRangePicker({ value, onChange }: UploadDateTimeRangePickerProps) {
  const screens = Grid.useBreakpoint();
  const compact = screens.sm === false;
  const range = useMemo<[Dayjs, Dayjs] | null>(() => {
    const start = parseLocalDateTime(value.start);
    const end = parseLocalDateTime(value.end);
    return start && end ? [dayjs(start), dayjs(end)] : null;
  }, [value.start, value.end]);
  const theme = useMemo<ThemeConfig>(() => ({
    token: { colorPrimary: '#13795f', controlHeight: 36, borderRadius: 6, fontSize: 12, fontFamily: 'Segoe UI, Microsoft YaHei, sans-serif' },
    components: { DatePicker: { inputFontSize: 12, paddingInline: 8, ...(compact ? { cellWidth: 24, timeColumnWidth: 40 } : {}) } },
  }), [compact]);
  const timeOptions = useMemo(() => ({
    format: 'HH:mm',
    use12Hours: false,
    defaultOpenValue: [dayjs().startOf('day'), dayjs().hour(23).minute(59).second(0).millisecond(0)],
  }), []);

  return (
    <ConfigProvider locale={zhCN} theme={theme}>
      <RangePicker
        className="upload-time-range"
        classNames={{ popup: { root: 'upload-time-range-popup' } }}
        popupAlign={{ overflow: { adjustX: true, adjustY: true, shiftX: 12, shiftY: 12 } }}
        aria-label="上传时间范围"
        value={range}
        minDate={minDate}
        maxDate={maxDate}
        format="YYYY/MM/DD HH:mm"
        placeholder={['开始日期时间', '结束日期时间']}
        showTime={timeOptions}
        needConfirm={false}
        previewValue={false}
        allowClear
        allowEmpty={[false, false]}
        onChange={(dates) => {
          if (!dates) {
            onChange({ start: '', end: '' });
          } else if (dates[0] && dates[1]) {
            onChange({ start: dates[0].format('YYYY-MM-DDTHH:mm'), end: dates[1].format('YYYY-MM-DDTHH:mm') });
          }
        }}
      />
    </ConfigProvider>
  );
}
