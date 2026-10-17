import type { Metadata } from 'next';
import { AntdRegistry } from '@ant-design/nextjs-registry';
import './globals.css';
import './workspace-scrollbars.css';

export const metadata: Metadata = {
  title: 'Key Control — Remote API Key Workspace',
  description: 'Securely upload, classify, query, and monitor API key validation status.',
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body className="antialiased">
        <AntdRegistry>{children}</AntdRegistry>
      </body>
    </html>
  );
}
