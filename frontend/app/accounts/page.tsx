'use client';

import AuthenticatedShell from '@/app/authenticated-shell';
import AccountsClient from './accounts-client';

export default function AccountsPage() {
  return <AuthenticatedShell activeItem="accounts" adminOnly>{(user) => <AccountsClient currentUserId={user.id} />}</AuthenticatedShell>;
}
