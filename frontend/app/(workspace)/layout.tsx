import type { ReactNode } from 'react';
import AuthenticatedShell from '@/app/authenticated-shell';

export default function WorkspaceLayout({ children }: { children: ReactNode }) {
  return <AuthenticatedShell>{children}</AuthenticatedShell>;
}
