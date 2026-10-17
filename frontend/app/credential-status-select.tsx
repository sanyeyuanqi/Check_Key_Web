'use client';

import { useMemo } from 'react';
import { ConfigProvider, Select, type ThemeConfig } from 'antd';
import zhCN from 'antd/locale/zh_CN';

type StatusOption = { value: string; name: string; count: number };

type CredentialStatusSelectProps = {
  value: string;
  statuses: StatusOption[];
  total: number;
  disabled: boolean;
  onChange: (value: string) => void;
};

const theme: ThemeConfig = {
  token: { colorPrimary: '#13795f', colorText: '#385b4d', colorBorder: '#dfe7e1', controlItemBgActive: '#eaf4ef', controlItemBgActiveHover: '#dceee2', controlHeight: 36, borderRadius: 6, fontSize: 12, fontFamily: 'Segoe UI, Microsoft YaHei, sans-serif' },
  components: {
    Select: { selectorBg: '#fcfdfc', optionFontSize: 12, optionHeight: 30, optionPadding: '5px 7px', optionSelectedBg: '#eaf4ef', optionSelectedColor: '#13795f', optionActiveBg: '#f3f8f5' },
  },
};

export function CredentialStatusSelect({ value, statuses, total, disabled, onChange }: CredentialStatusSelectProps) {
  const options = useMemo(() => [
    { value: 'all', name: '全部状态', count: total, label: `全部状态（${total}）` },
    ...statuses.map((item) => ({ ...item, label: `${item.name}（${item.count}）` })),
  ], [statuses, total]);

  return (
    <ConfigProvider locale={zhCN} theme={theme}>
      <Select<string, typeof options[number]>
        className="key-status-select"
        classNames={{ popup: { root: 'key-status-popup' } }}
        styles={{ popup: { root: { border: '1px solid #dfe7e1', borderRadius: 6 } } }}
        aria-label="密钥状态"
        value={value}
        disabled={disabled}
        loading={disabled}
        options={options}
        showSearch={{ optionFilterProp: 'label' }}
        listHeight={240}
        popupMatchSelectWidth
        virtual={false}
        notFoundContent="暂无匹配状态"
        optionRender={(option) => (
          <span className="key-status-option">
            <span>{option.data.name}</span>
            <span className="key-status-option-count">{option.data.count}</span>
          </span>
        )}
        onChange={onChange}
      />
    </ConfigProvider>
  );
}
