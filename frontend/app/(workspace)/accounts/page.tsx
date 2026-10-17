'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { useAuthUser } from '@/app/authenticated-shell';
import AccountsClient from './accounts-client';

export default function AccountsPage() {
  const user = useAuthUser();
  const router = useRouter();

  useEffect(() => {
    if (user.role !== 'admin') router.replace('/platform/aws');
  }, [router, user.role]);

  if (user.role !== 'admin') return null;
  return <AccountsClient currentUserId={user.id} />;
}
